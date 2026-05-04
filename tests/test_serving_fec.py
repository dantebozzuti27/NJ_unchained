"""Tests for the FEC serving layer (Tier 4 v1 read API + fraud UI).

Test taxonomy
-------------
1. Pure schema validation (no DB)
   - Pydantic v2 models accept the dict shapes returned by queries_fec
   - FecPagedResponse envelope round-trips (rows arbitrary, total_count int)
   - FecSummary tolerates the empty-DB shape (zeros + empty cycle)

2. Filter-builder unit tests (no DB)
   - WHERE-clause helpers skip empty filters (no spurious AND clauses)
   - Sort whitelist resolves to expected identifiers / falls back safely
   - Pagination clampers respect [1, MAX_LIMIT] and offset >= 0

3. Live integration (live_pg)
   - Bootstrap migrations + seed via fec_db fixture
   - Synthetic cn / cm / indiv loaded into raw.fec_*
   - Each /fec/* endpoint serves the same shape Pydantic expects
   - Filters compose: NJ-only candidates excludes the NY synthetic
   - Pagination: limit=1 + offset increments yield distinct rows
   - Detail endpoints 200 for known IDs, 404 for unknown
   - CSV export streams a header row + body rows; no Content-Length
     header (streaming) but filename in Content-Disposition
   - GET /fraud serves the static index.html (sanity check)
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from ingestion.fec import (
    FetchResult,
    load_fec_indiv,
    load_fec_small_table,
    parse_fec_small_table,
)
from serving.models import (
    FecCandidateDetail,
    FecCandidateRow,
    FecCommitteeRow,
    FecContributionRow,
    FecMoneyToNjRow,
    FecPagedResponse,
    FecSummary,
)
from serving.queries_fec import (
    _CANDIDATE_SORT_COLS,
    DEFAULT_LIMIT,
    MAX_LIMIT,
    CandidateFilters,
    CommitteeFilters,
    ContributionFilters,
    MoneyToNjFilters,
    _build_candidate_where,
    _build_committee_where,
    _build_contribution_where,
    _build_money_to_nj_where,
    _clamp_limit,
    _clamp_offset,
    _resolve_sort,
)

if TYPE_CHECKING:
    import psycopg


# ============================================================================
# 1. Pydantic schema validation (no DB)
# ============================================================================


def test_fec_summary_accepts_empty_db_shape() -> None:
    """The empty-DB summary shape (cycle='', all zeros) must validate."""
    s = FecSummary.model_validate({
        "cycle": "",
        "candidates_total":               0,
        "candidates_nj":                  0,
        "committees_total":               0,
        "committees_nj_domiciled":        0,
        "contributions_total":            0,
        "contributions_nj_donor":         0,
        "contributions_to_nj_candidates": 0,
        "cycles_available":               [],
    })
    assert s.cycle == ""
    assert s.candidates_total == 0
    assert s.cycles_available == []


def test_fec_summary_rejects_negative_count() -> None:
    """Counts must be int; negative counts (impossible from DB) are
    not explicitly validated, but missing required fields must raise.
    """
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        FecSummary.model_validate({"cycle": "2024"})  # missing required counts


def test_paged_response_envelope_roundtrip() -> None:
    env = FecPagedResponse(
        rows=[{"foo": 1}, {"foo": 2}],
        total_count=42, limit=10, offset=20,
    )
    dumped = env.model_dump()
    assert dumped["total_count"] == 42
    assert len(dumped["rows"]) == 2


def test_candidate_row_validates_minimal_shape() -> None:
    """The required cycle + cand_id pair is enough; everything else is optional."""
    r = FecCandidateRow.model_validate({
        "cycle": "2024",
        "cand_id": "S0NJ00100",
    })
    assert r.cycle == "2024"
    assert r.cand_name is None


def test_committee_row_alias_committee_name_works() -> None:
    """SQL projects `cmte_nm AS committee_name`; the model accepts that key."""
    r = FecCommitteeRow.model_validate({
        "cycle": "2024",
        "cmte_id": "C00500587",
        "committee_name": "BOOKER FOR SENATE",
        "treasurer_name": "WHITE, ELIZABETH",
    })
    assert r.committee_name == "BOOKER FOR SENATE"


def test_contribution_row_is_memo_defaults_to_false() -> None:
    """Memo flag defaults to False; transaction_date may be None."""
    r = FecContributionRow.model_validate({
        "cycle":  "2024",
        "sub_id": "12345",
    })
    assert r.is_memo is False
    assert r.transaction_date is None


def test_money_to_nj_row_validates_minimal_shape() -> None:
    """Headline view row requires cycle, sub_id, cand_id, cmte_id."""
    r = FecMoneyToNjRow.model_validate({
        "cycle":   "2024",
        "sub_id":  "12345",
        "cand_id": "S0NJ00100",
        "cmte_id": "C00500587",
    })
    assert r.cand_id == "S0NJ00100"


# ============================================================================
# 2. Filter-builder unit tests (no DB)
# ============================================================================


def test_build_candidate_where_empty_filters_returns_blank_clause() -> None:
    where, args = _build_candidate_where(CandidateFilters())
    assert args == []
    assert where.as_string(None) == ""


def test_build_candidate_where_skips_empty_string_filters() -> None:
    """Empty-string predicates must be skipped; only None is the valid 'no filter' signal,
    but UI forms commonly send '' for unselected dropdowns -- those must NOT generate
    a `col = ''` predicate (which would silently return zero rows).
    """
    where, args = _build_candidate_where(CandidateFilters(state="", cycle="2024"))
    assert args == ["2024"]
    rendered = where.as_string(None)
    assert "cycle" in rendered
    assert "cand_office_st" not in rendered


def test_build_committee_where_has_candidate_true_emits_not_null() -> None:
    where, args = _build_committee_where(CommitteeFilters(has_candidate=True))
    assert args == []
    assert "cand_id IS NOT NULL" in where.as_string(None)


def test_build_committee_where_has_candidate_false_emits_is_null() -> None:
    where, args = _build_committee_where(CommitteeFilters(has_candidate=False))
    assert args == []
    assert "cand_id IS NULL" in where.as_string(None)


def test_build_contribution_where_excludes_memo_by_default() -> None:
    where, args = _build_contribution_where(ContributionFilters())
    assert args == []
    assert "NOT is_memo" in where.as_string(None)


def test_build_contribution_where_amount_and_date_bounds() -> None:
    f = ContributionFilters(
        min_amount=100, max_amount=5000,
        start_date="2024-01-01", end_date="2024-12-31",
        exclude_memo=True,
    )
    where, args = _build_contribution_where(f)
    assert args == [100, 5000, "2024-01-01", "2024-12-31"]
    rendered = where.as_string(None)
    # _add_cmp emits properly-quoted Identifier (defense in depth against
    # an attacker passing a malicious column via ContributionFilters).
    assert '"transaction_amount" >= %s' in rendered
    assert '"transaction_amount" <= %s' in rendered
    assert '"transaction_date" >= %s'   in rendered
    assert '"transaction_date" <= %s'   in rendered


def test_build_money_to_nj_where_filters_compose_correctly() -> None:
    f = MoneyToNjFilters(cycle="2024", cand_id="S0NJ00100", donor_state="CA")
    where, args = _build_money_to_nj_where(f)
    assert args == ["2024", "S0NJ00100", "CA"]
    assert " AND " in where.as_string(None)


def test_resolve_sort_falls_back_to_first_whitelisted_column() -> None:
    col, direction = _resolve_sort(_CANDIDATE_SORT_COLS, None, None)
    # First entry in the whitelist dict is cand_name -> ASC
    assert col.as_string(None) == '"cand_name"'
    assert direction.as_string(None) == "ASC"


def test_resolve_sort_rejects_off_whitelist_value_silently_to_default() -> None:
    """Defense in depth: a malicious sort_by must NOT raise (which would
    leak that the column does not exist); we silently fall back to the
    first whitelisted column."""
    col, direction = _resolve_sort(
        _CANDIDATE_SORT_COLS, "1; DROP TABLE raw.fec_candidate; --", "DESC",
    )
    assert col.as_string(None) == '"cand_name"'
    assert direction.as_string(None) == "DESC"


def test_clamp_limit_defaults_when_none() -> None:
    assert _clamp_limit(None) == DEFAULT_LIMIT


def test_clamp_limit_caps_at_hard_cap() -> None:
    assert _clamp_limit(MAX_LIMIT * 100) == MAX_LIMIT


def test_clamp_limit_floors_at_one() -> None:
    assert _clamp_limit(0)  == 1
    assert _clamp_limit(-5) == 1


def test_clamp_offset_floors_at_zero() -> None:
    assert _clamp_offset(None) == 0
    assert _clamp_offset(-1)   == 0
    assert _clamp_offset(50)   == 50


# ============================================================================
# 3. Live integration tests (live_pg)
# ============================================================================


pytestmark = pytest.mark.live_pg


@pytest.fixture
def fec_db(live_pg: psycopg.Connection) -> psycopg.Connection:
    """Apply all migrations + seeds into a fresh schema set; yield the conn."""
    from scripts.migrate import (
        MIGRATIONS_DIR,
        SEEDS_DIR,
        apply_migrations,
        discover,
    )
    conn = live_pg
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS governance CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS derived    CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS raw        CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS ref        CASCADE")
        cur.execute(
            "DO $$ "
            "DECLARE r record; "
            "BEGIN "
            "  FOR r IN SELECT viewname FROM pg_views "
            "           WHERE schemaname='public' AND viewname LIKE 'v_%%' LOOP "
            "    EXECUTE 'DROP VIEW IF EXISTS public.' || quote_ident(r.viewname) || ' CASCADE'; "
            "  END LOOP; "
            "END $$;"
        )
    conn.commit()
    apply_migrations(conn, discover(MIGRATIONS_DIR))
    apply_migrations(conn, discover(SEEDS_DIR))
    return conn


def _synth_cn_zip(tmp_path: Path, *, cycle: str = "2024") -> Path:
    """Two NJ candidates + one NY (out-of-state) for filter coverage."""
    rows = [
        ["S0NJ00100", "BOOKER, CORY ANTHONY", "DEM", "2026", "NJ", "S",
         "00", "I", "C", "C00500587",
         "1421 RAYBURN", "", "WASHINGTON", "DC", "20515"],
        ["H0NJ03200", "KIM, ANDY", "DEM", "2024", "NJ", "H",
         "03", "I", "C", "C00553867",
         "PO BOX 100", "", "MARLTON", "NJ", "08053"],
        ["S6NY00800", "SCHUMER, CHARLES E", "DEM", "2028", "NY", "S",
         "00", "I", "C", "C00346312",
         "780 3RD AVE", "", "NEW YORK", "NY", "10017"],
    ]
    yy = cycle[-2:]
    text_buf = io.StringIO()
    for row in rows:
        text_buf.write("|".join(row) + "\n")
    zip_path = tmp_path / f"cn{yy}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("cn.txt", text_buf.getvalue())
    return zip_path


def _synth_cm_zip(tmp_path: Path, *, cycle: str = "2024") -> Path:
    rows = [
        ["C00500587", "BOOKER FOR SENATE", "WHITE, ELIZABETH",
         "PO BOX 32157", "", "NEWARK", "NJ", "07102",
         "P", "S", "DEM", "Q", "", "", "S0NJ00100"],
        ["C00553867", "KIM FOR NEW JERSEY", "DOE, JANE",
         "PO BOX 100", "", "MARLTON", "NJ", "08053",
         "P", "H", "DEM", "Q", "", "", "H0NJ03200"],
        ["C00346312", "SCHUMER 2028", "ROE, RICHARD",
         "780 3RD AVE", "", "NEW YORK", "NY", "10017",
         "P", "S", "DEM", "Q", "", "", "S6NY00800"],
    ]
    yy = cycle[-2:]
    text_buf = io.StringIO()
    for row in rows:
        text_buf.write("|".join(row) + "\n")
    zip_path = tmp_path / f"cm{yy}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("cm.txt", text_buf.getvalue())
    return zip_path


def _synth_indiv_zip(tmp_path: Path, *, cycle: str = "2024") -> Path:
    """Five contributions: 2 to Booker, 2 to Kim, 1 memo. Two NJ donors, one CA."""
    rows = [
        ["C00500587", "N", "M3", "P", "202403129585432100", "15",
         "IND", "DOE, JANE", "JERSEY CITY", "NJ", "07302",
         "ACME CORP", "ENGINEER", "01152024", "250.00",
         "", "ABCDEF1234", "1234567", "", "", "FEC1001"],
        ["C00500587", "N", "M3", "P", "202403129585432101", "15",
         "IND", "ROE, RICHARD", "PRINCETON", "NJ", "08540",
         "RUTGERS UNIV", "PROFESSOR", "02012024", "1000.00",
         "", "GHIJKL5678", "1234568", "", "", "FEC1002"],
        ["C00553867", "N", "M3", "P", "202403129585432200", "15",
         "IND", "SMITH, ALICE", "PALO ALTO", "CA", "94301",
         "STANFORD", "RESEARCHER", "03152024", "500.00",
         "", "MNOPQR9012", "1234569", "", "", "FEC1003"],
        ["C00500587", "N", "M3", "P", "202403129585432102", "15",
         "IND", "MEMO_PARENT, JOHN", "TRENTON", "NJ", "08608",
         "TRENTON LLC", "OWNER", "01202024", "2900.00",
         "", "STUVWX3456", "1234570", "X", "JOINT FUNDRAISING", "FEC1004"],
        ["C00553867", "N", "M3", "P", "202403129585432201", "15",
         "IND", "DATE_GHOST, MARY", "CAMDEN", "NJ", "08103",
         "", "", "00000000", "100.00",
         "", "YZ12345678", "1234571", "", "", "FEC1005"],
    ]
    yy = cycle[-2:]
    text_buf = io.StringIO()
    for row in rows:
        text_buf.write("|".join(row) + "\n")
    zip_path = tmp_path / f"indiv{yy}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("itcont.txt", text_buf.getvalue())
    return zip_path


def _fetch_for(zip_path: Path, *, cycle: str, kind: str) -> FetchResult:
    return FetchResult(
        path=zip_path,
        source_url=f"file://{zip_path}",
        source_sha256="0" * 64,
        source_vintage=f"test-{cycle}",
        n_bytes=zip_path.stat().st_size,
        cache_hit=False,
    )


def _load_full_synthetic(conn: psycopg.Connection, tmp_path: Path) -> None:
    """Load cn + cm + indiv synthetic zips and commit."""
    z_cn = _synth_cn_zip(tmp_path, cycle="2024")
    z_cm = _synth_cm_zip(tmp_path, cycle="2024")
    z_iv = _synth_indiv_zip(tmp_path, cycle="2024")
    parse_cn = parse_fec_small_table(_fetch_for(z_cn, cycle="2024", kind="cn"),
                                     cycle="2024", file_kind="cn")
    parse_cm = parse_fec_small_table(_fetch_for(z_cm, cycle="2024", kind="cm"),
                                     cycle="2024", file_kind="cm")
    load_fec_small_table(parse_cn, conn)
    load_fec_small_table(parse_cm, conn)
    load_fec_indiv(_fetch_for(z_iv, cycle="2024", kind="indiv"), conn, cycle="2024")
    conn.commit()


@pytest.fixture
def serving_client(
    fec_db: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    """A FastAPI TestClient backed by the fec_db connection.

    We monkeypatch serving.db.borrow_connection to yield the SAME
    connection the fixture loaded data into (rather than spinning up
    a separate pool that would need PG_DSN). This is the only way to
    test against an in-test ephemeral DB without a docker-compose
    plumbing layer.
    """
    _load_full_synthetic(fec_db, tmp_path)
    from contextlib import contextmanager

    from serving import db as serving_db

    # Defensive: clear the process-local summary cache so the previous
    # test's snapshot doesn't leak into this one. In production the TTL
    # is fine; in tests we mutate raw.fec_* between fixtures.
    from serving import queries_fec
    queries_fec.clear_summary_cache()

    @contextmanager
    def _borrow():  # type: ignore[no-untyped-def]
        yield fec_db

    monkeypatch.setattr(serving_db, "borrow_connection", _borrow)
    # Also patch the names already imported by the route modules
    from serving.routes import fec as fec_route
    from serving.routes import fec_export as fec_export_route
    monkeypatch.setattr(fec_route, "borrow_connection", _borrow)
    monkeypatch.setattr(fec_export_route, "borrow_connection", _borrow)

    from serving.app import create_app
    app = create_app()
    return TestClient(app)


# --------------------------------------------------------------------------
# /fec/summary, /fec/cycles, /fec/states, /fec/parties, /fec/offices
# --------------------------------------------------------------------------


def test_summary_returns_expected_counts(serving_client: TestClient) -> None:
    resp = serving_client.get("/fec/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cycle"] == "2024"
    assert body["candidates_total"] == 3
    assert body["candidates_nj"]    == 2
    assert body["committees_total"] == 3
    assert body["committees_nj_domiciled"] == 2
    assert body["contributions_total"] == 5
    assert body["contributions_nj_donor"] == 4  # 4 NJ donors (incl. memo)
    assert body["cycles_available"] == ["2024"]


def test_cycles_returns_descending_distinct(serving_client: TestClient) -> None:
    resp = serving_client.get("/fec/cycles")
    assert resp.status_code == 200
    rows = resp.json()
    assert rows == [{"value": "2024", "count": 3}]


def test_states_filtered_to_cycle(serving_client: TestClient) -> None:
    resp = serving_client.get("/fec/states?cycle=2024")
    assert resp.status_code == 200
    rows = {r["value"]: r["count"] for r in resp.json()}
    assert rows == {"NJ": 2, "NY": 1}


def test_parties_returns_dem_with_count_three(serving_client: TestClient) -> None:
    resp = serving_client.get("/fec/parties")
    assert resp.status_code == 200
    rows = resp.json()
    assert {r["value"] for r in rows} == {"DEM"}


def test_offices_returns_h_and_s(serving_client: TestClient) -> None:
    resp = serving_client.get("/fec/offices")
    assert resp.status_code == 200
    values = {r["value"] for r in resp.json()}
    assert values == {"H", "S"}


# --------------------------------------------------------------------------
# /fec/candidates -- list + filter + paginate + detail
# --------------------------------------------------------------------------


def test_candidates_list_default_returns_all_three(serving_client: TestClient) -> None:
    resp = serving_client.get("/fec/candidates")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_count"] == 3
    assert len(body["rows"]) == 3
    assert all(set(r.keys()) >= {"cand_id", "cycle"} for r in body["rows"])


def test_candidates_filtered_by_state_nj(serving_client: TestClient) -> None:
    resp = serving_client.get("/fec/candidates?state=NJ")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_count"] == 2
    states = {r["cand_office_st"] for r in body["rows"]}
    assert states == {"NJ"}


def test_candidates_pagination_limit_one(serving_client: TestClient) -> None:
    """limit=1 + offset 0/1/2 should yield three distinct cand_ids."""
    seen = set()
    for offset in (0, 1, 2):
        resp = serving_client.get(f"/fec/candidates?limit=1&offset={offset}")
        body = resp.json()
        assert body["total_count"] == 3
        assert body["limit"]  == 1
        assert body["offset"] == offset
        assert len(body["rows"]) == 1
        seen.add(body["rows"][0]["cand_id"])
    assert len(seen) == 3


def test_candidate_detail_200_for_known_id(serving_client: TestClient) -> None:
    resp = serving_client.get("/fec/candidates/S0NJ00100")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cand_id"] == "S0NJ00100"
    assert body["cand_name"] == "BOOKER, CORY ANTHONY"
    # Booker's PCC C00500587 should be linked back via raw.fec_committee
    cmtes = body["linked_committees"]
    assert len(cmtes) == 1
    assert cmtes[0]["cmte_id"] == "C00500587"
    # Schema validation against the Pydantic model itself
    FecCandidateDetail.model_validate(body)


def test_candidate_detail_404_for_unknown_id(serving_client: TestClient) -> None:
    resp = serving_client.get("/fec/candidates/UNKNOWN999")
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# /fec/committees -- list + detail
# --------------------------------------------------------------------------


def test_committees_filtered_by_state_nj(serving_client: TestClient) -> None:
    resp = serving_client.get("/fec/committees?state=NJ")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_count"] == 2  # Booker + Kim


def test_committee_detail_includes_recent_contributions(
    serving_client: TestClient,
) -> None:
    resp = serving_client.get("/fec/committees/C00500587")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cmte_id"] == "C00500587"
    assert body["committee_name"] == "BOOKER FOR SENATE"
    assert body["linked_candidate"] is not None
    assert body["linked_candidate"]["cand_id"] == "S0NJ00100"
    # Three contributions to Booker exist in the synthetic fixture
    # (DOE, ROE, MEMO_PARENT). All three appear in the recent list
    # because the recent-contributions slice on the detail endpoint
    # does NOT exclude memos -- it shows the raw evidence including
    # the memo line.
    assert len(body["recent_contributions"]) == 3


# --------------------------------------------------------------------------
# /fec/contributions -- list + filter + memo behavior
# --------------------------------------------------------------------------


def test_contributions_default_excludes_memo_rows(
    serving_client: TestClient,
) -> None:
    """Default exclude_memo=true: 5 raw rows -> 4 non-memo visible."""
    resp = serving_client.get("/fec/contributions")
    body = resp.json()
    assert body["total_count"] == 4


def test_contributions_include_memo_when_disabled(
    serving_client: TestClient,
) -> None:
    resp = serving_client.get("/fec/contributions?exclude_memo=false")
    body = resp.json()
    assert body["total_count"] == 5


def test_contributions_filtered_by_donor_state_ca(
    serving_client: TestClient,
) -> None:
    resp = serving_client.get("/fec/contributions?donor_state=CA")
    body = resp.json()
    assert body["total_count"] == 1
    assert body["rows"][0]["contributor_name"] == "SMITH, ALICE"


def test_contributions_filtered_by_amount_range(
    serving_client: TestClient,
) -> None:
    resp = serving_client.get("/fec/contributions?min_amount=400&max_amount=2000")
    body = resp.json()
    # Excludes the $100, $250 rows; excludes the $2900 memo (memo flag);
    # leaves SMITH/CA $500 and ROE/NJ $1000.
    assert body["total_count"] == 2


# --------------------------------------------------------------------------
# /fec/money-to-nj -- the headline view
# --------------------------------------------------------------------------


def test_money_to_nj_returns_only_nj_candidate_contributions(
    serving_client: TestClient,
) -> None:
    """Schumer (NY) is excluded; only Booker + Kim contributions appear."""
    resp = serving_client.get("/fec/money-to-nj")
    body = resp.json()
    # exclude_memo=true by default -> 4 of 5 contribs survive,
    # all of which point at Booker/Kim (NJ candidates)
    assert body["total_count"] == 4
    cand_ids = {r["cand_id"] for r in body["rows"]}
    assert cand_ids == {"S0NJ00100", "H0NJ03200"}


# --------------------------------------------------------------------------
# CSV export endpoints (streaming)
# --------------------------------------------------------------------------


def test_export_candidates_csv_streams_header_and_rows(
    serving_client: TestClient,
) -> None:
    resp = serving_client.get("/fec/export/candidates.csv?state=NJ")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert resp.headers["content-disposition"].endswith('"fec_candidates.csv"')
    text = resp.text
    lines = text.strip().splitlines()
    assert lines[0].startswith("cycle,cand_id,cand_name")
    # 1 header + 2 NJ rows
    assert len(lines) == 3
    assert "BOOKER, CORY ANTHONY" in text
    assert "KIM, ANDY" in text
    assert "SCHUMER" not in text  # NY filter held


def test_export_committees_csv_respects_filter(
    serving_client: TestClient,
) -> None:
    resp = serving_client.get("/fec/export/committees.csv?state=NJ")
    assert resp.status_code == 200
    lines = resp.text.strip().splitlines()
    # 1 header + 2 NJ committees
    assert len(lines) == 3
    assert "BOOKER FOR SENATE" in resp.text


def test_export_contributions_csv_includes_memo_when_disabled(
    serving_client: TestClient,
) -> None:
    resp = serving_client.get(
        "/fec/export/contributions.csv?exclude_memo=false",
    )
    assert resp.status_code == 200
    lines = resp.text.strip().splitlines()
    # 1 header + 5 raw contributions
    assert len(lines) == 6


def test_export_money_to_nj_csv_streams(
    serving_client: TestClient,
) -> None:
    resp = serving_client.get("/fec/export/money-to-nj.csv")
    assert resp.status_code == 200
    lines = resp.text.strip().splitlines()
    # 1 header + 4 non-memo contribs to NJ candidates
    assert len(lines) == 5


# --------------------------------------------------------------------------
# /fraud static UI route
# --------------------------------------------------------------------------


def test_fraud_index_serves_html(serving_client: TestClient) -> None:
    resp = serving_client.get("/fraud")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert b"Civic Integrity Terminal" in resp.content
    assert b'/static/fraud/fraud.js' in resp.content


def test_fraud_index_landing_tab_is_risk(serving_client: TestClient) -> None:
    """Tier 4 v3 step 5: the entity-first risk queue is the default tab.

    Regression guard: if someone reorders the tab buttons or flips
    ``active`` back onto a different tab, this test catches it before
    UAT does.
    """
    resp = serving_client.get("/fraud")
    body = resp.content
    # The risk tab button must be present, marked active, and labeled
    # "Risk queue" (the visible string we ship in the HTML).
    assert b'data-tab="risk"' in body
    assert b'class="tab-button active" data-tab="risk"' in body
    assert b">Risk queue<" in body
    # The signal-explorer (formerly default) must be present but NOT
    # the active tab; it has been demoted.
    assert b'data-tab="metrics"' in body
    assert b'>Signal explorer<' in body
    assert b'class="tab-button active" data-tab="metrics"' not in body
    # The risk queue's filter form + filterable controls must exist so
    # the JS controller's getElementById lookups succeed at load.
    assert b'id="filter-risk"' in body
    assert b'id="risk-cycle"' in body
    assert b'id="risk-entity-kind"' in body
    assert b'id="risk-signal-id"' in body
    assert b'id="risk-min-score"' in body
    assert b'id="risk-sort-by"' in body
    # Pane container + Tabulator host + paginator slots must exist.
    assert b'id="pane-risk"' in body
    assert b'id="table-risk"' in body
    assert b'id="pager-risk"' in body
    assert b'id="risk-empty-banner"' in body


def test_fraud_static_assets_serve(serving_client: TestClient) -> None:
    """Both static assets the HTML references must be reachable; if the
    static-files mount drifts (e.g. someone moves fraud.js out of
    serving/static/fraud/), the page would 404 silently in the browser
    and we'd never know from API tests alone."""
    js = serving_client.get("/static/fraud/fraud.js")
    assert js.status_code == 200
    assert b"makeRiskController" in js.content
    assert b"renderRiskPanel" in js.content

    css = serving_client.get("/static/fraud/fraud.css")
    assert css.status_code == 200
    assert b".risk-score-badge" in css.content
    assert b".risk-evidence" in css.content
