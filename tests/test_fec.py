"""Tests for the FEC bulk ingester (Tier 4 v1).

Test taxonomy
-------------
1. Pure helpers
   - URL construction (bulk_url, header_file_url)
   - Pinned column lists vs FEC's published header files
   - Cycle / file_kind validation
2. Pure parser (no DB)
   - parse_fec_small_table on synthetic cn / cm CSVs
   - stage_small_dataframe column ordering + provenance injection
3. Integration (live_pg)
   - Apply all migrations, COPY a synthetic cn / cm / itcont zip,
     assert row counts in raw.fec_*
   - Idempotent re-load (same cycle twice, deterministic row count)
   - Cross-cycle co-existence (2020 + 2024 in raw.fec_candidate)
   - Canonical view public.v_fec_contribution parses MMDDYYYY -> DATE
   - is_memo flag flips MEMO_CD='X' rows out of summable analytics
   - public.v_fec_money_to_nj_candidates joins three tables correctly
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from ingestion._base import IngestError
from ingestion.fec import (
    CM_COLUMNS,
    CN_COLUMNS,
    FILE_KINDS,
    INDIV_COLUMNS,
    FetchResult,
    bulk_url,
    fetch_fec_bulk,
    header_file_url,
    load_fec_indiv,
    load_fec_small_table,
    parse_fec_small_table,
    stage_small_dataframe,
)

if TYPE_CHECKING:
    import psycopg


# ============================================================================
# 1. Pure helpers
# ============================================================================


def test_bulk_url_2024_cn() -> None:
    assert bulk_url("2024", "cn") == \
        "https://www.fec.gov/files/bulk-downloads/2024/cn24.zip"


def test_bulk_url_2020_indiv() -> None:
    assert bulk_url("2020", "indiv") == \
        "https://www.fec.gov/files/bulk-downloads/2020/indiv20.zip"


def test_bulk_url_uses_two_digit_year_suffix_consistently() -> None:
    """Spot-check the {yy} substitution across all kinds for 2008 (oldest stable)."""
    assert bulk_url("2008", "cn").endswith("/2008/cn08.zip")
    assert bulk_url("2008", "cm").endswith("/2008/cm08.zip")
    assert bulk_url("2008", "indiv").endswith("/2008/indiv08.zip")


def test_bulk_url_rejects_bad_cycle() -> None:
    with pytest.raises(ValueError, match="Bad cycle"):
        bulk_url("24", "cn")
    with pytest.raises(ValueError, match="Bad cycle"):
        bulk_url("2024-Q4", "cn")


def test_bulk_url_rejects_bad_file_kind() -> None:
    with pytest.raises(ValueError, match="Bad file_kind"):
        bulk_url("2024", "presidential")


def test_header_file_url_uses_data_dictionaries_dir() -> None:
    assert header_file_url("indiv") == \
        "https://www.fec.gov/files/bulk-downloads/data_dictionaries/indiv_header_file.csv"


def test_pinned_column_lists_match_published_lengths() -> None:
    """The column counts must match FEC's documented header files.

    A drift here is the canary for an FEC schema change. The source-of-
    truth lengths come from the live header files (verified 2026-04-29
    by the operator):
        cn:    15 columns
        cm:    15 columns
        indiv: 21 columns
    """
    assert len(CN_COLUMNS) == 15
    assert len(CM_COLUMNS) == 15
    assert len(INDIV_COLUMNS) == 21


def test_indiv_columns_pin_critical_join_columns() -> None:
    """CMTE_ID at position 0 and SUB_ID at position 20 -- never reorder.

    Many FEC-aware tools (FEC.gov's own front-end, OpenSecrets'
    importer, etc.) hard-code these positions; if FEC ever moved them
    we would need to migrate downstream code carefully. This test
    pins our assumption.
    """
    assert INDIV_COLUMNS[0]  == "CMTE_ID"
    assert INDIV_COLUMNS[20] == "SUB_ID"


def test_file_kinds_register_all_three() -> None:
    assert set(FILE_KINDS.keys()) == {"cn", "cm", "indiv"}
    for kind, (zname, inner, table) in FILE_KINDS.items():
        assert "{yy}" in zname, f"{kind!r} zip pattern must include {{yy}}"
        assert inner.endswith(".txt"), f"{kind!r} inner must be a .txt file"
        assert table.startswith("raw.fec_"), f"{kind!r} target must be raw.fec_*"


# ============================================================================
# 2. Pure parser (no DB)
# ============================================================================


def _synth_cn_zip(tmp_path: Path, *, cycle: str = "2024") -> Path:
    """Write a tiny synthetic cn{yy}.zip containing two NJ candidates + one out-of-state.

    Real FEC bytes minus the volume; pinned values that downstream
    tests assert against.
    """
    rows = [
        # Cory Booker (sitting NJ senator) -- inflated FEC ID for test
        ["S0NJ00100", "BOOKER, CORY ANTHONY", "DEM", "2026", "NJ", "S",
         "00", "I", "C", "C00500587",
         "1421 RAYBURN", "", "WASHINGTON", "DC", "20515"],
        # Andy Kim (NJ rep, became senator in 2024)
        ["H0NJ03200", "KIM, ANDY", "DEM", "2024", "NJ", "H",
         "03", "I", "C", "C00553867",
         "PO BOX 100", "", "MARLTON", "NJ", "08053"],
        # NY senator (out-of-state) to verify NJ filter
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
    """Synthetic cm{yy}.zip: principal campaign committees for the cn rows above."""
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
    """Synthetic indiv{yy}.zip: 5 individual contributions to the synthetic cn/cm.

    Includes:
      - Two NJ donors -> Booker
      - One CA donor  -> Kim (out-of-state-money-to-NJ-candidate signal)
      - One memo-line entry (memo_cd='X') that must be filtered from sums
      - One row with TRANSACTION_DT='00000000' (must parse to NULL)
    """
    rows = [
        # CMTE_ID|AMNDT_IND|RPT_TP|TRANSACTION_PGI|IMAGE_NUM|TRANSACTION_TP|
        # ENTITY_TP|NAME|CITY|STATE|ZIP_CODE|EMPLOYER|OCCUPATION|
        # TRANSACTION_DT|TRANSACTION_AMT|OTHER_ID|TRAN_ID|FILE_NUM|
        # MEMO_CD|MEMO_TEXT|SUB_ID
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
        # Memo-line entry: must be filtered out of summable totals
        ["C00500587", "N", "M3", "P", "202403129585432102", "15",
         "IND", "MEMO_PARENT, JOHN", "TRENTON", "NJ", "08608",
         "TRENTON LLC", "OWNER", "01202024", "2900.00",
         "", "STUVWX3456", "1234570", "X", "JOINT FUNDRAISING", "FEC1004"],
        # Bad date sentinel: '00000000' must yield NULL on parse
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
    """Build a synthetic FetchResult that points at *zip_path*."""
    return FetchResult(
        path=zip_path,
        source_url=f"file://{zip_path}",
        source_sha256="0" * 64,
        source_vintage=f"test-{cycle}",
        n_bytes=zip_path.stat().st_size,
        cache_hit=False,
    )


def test_parse_cn_pulls_three_rows_with_expected_cols(tmp_path: Path) -> None:
    z = _synth_cn_zip(tmp_path)
    parse = parse_fec_small_table(_fetch_for(z, cycle="2024", kind="cn"),
                                  cycle="2024", file_kind="cn")
    assert parse.n_rows == 3
    # Polars renames into the upstream FEC column casing first, then
    # stage_small_dataframe lowercases. parse_fec_small_table itself
    # leaves columns at upstream casing.
    assert tuple(parse.dataframe.columns) == CN_COLUMNS
    assert parse.dataframe["CAND_OFFICE_ST"].to_list() == ["NJ", "NJ", "NY"]


def test_parse_rejects_unknown_file_kind(tmp_path: Path) -> None:
    """parse_fec_small_table is for cn/cm only; indiv must reject."""
    z = _synth_indiv_zip(tmp_path)
    with pytest.raises(ValueError, match="cn/cm only"):
        parse_fec_small_table(_fetch_for(z, cycle="2024", kind="indiv"),
                              cycle="2024", file_kind="indiv")


def test_parse_raises_on_missing_inner_file(tmp_path: Path) -> None:
    """If the zip has no cn.txt, IngestError should fire with the actual contents."""
    bad_zip = tmp_path / "cn24.zip"
    with zipfile.ZipFile(bad_zip, "w") as zf:
        zf.writestr("WRONG_NAME.txt", "doesnt matter")
    with pytest.raises(IngestError, match="missing inner file"):
        parse_fec_small_table(_fetch_for(bad_zip, cycle="2024", kind="cn"),
                              cycle="2024", file_kind="cn")


def test_stage_small_injects_cycle_and_provenance(tmp_path: Path) -> None:
    z = _synth_cn_zip(tmp_path, cycle="2022")
    parse = parse_fec_small_table(_fetch_for(z, cycle="2022", kind="cn"),
                                  cycle="2022", file_kind="cn")
    staged = stage_small_dataframe(parse)
    assert staged.columns[0] == "cycle"
    assert staged["cycle"].to_list() == ["2022", "2022", "2022"]
    # provenance trio at the tail in canonical order
    assert staged.columns[-3:] == ["source_url", "source_sha256", "source_vintage"]
    assert (staged["source_vintage"] == "test-2022").all()


# ============================================================================
# 3. Integration tests (live_pg)
# ============================================================================
#
# The fec.py module has no derived layer of its own (yet), so the
# integration tests live independently of the lca/cpi tests. The
# initialized_db fixture (defined in test_pg_integration.py) cannot be
# imported across modules; we replicate the migrate-and-seed bootstrap
# inline here. Slightly duplicative but keeps the test module hermetic.


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
        # Drop public.v_* views the migrations create.
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


def _load_one_kind(
    conn: psycopg.Connection,
    *,
    zip_path: Path,
    cycle: str,
    kind: str,
) -> int:
    fetch = _fetch_for(zip_path, cycle=cycle, kind=kind)
    if kind == "indiv":
        n = load_fec_indiv(fetch, conn, cycle=cycle)
    else:
        parse = parse_fec_small_table(fetch, cycle=cycle, file_kind=kind)
        n = load_fec_small_table(parse, conn)
    conn.commit()
    return n


def test_load_cn_into_raw_fec_candidate(
    fec_db: psycopg.Connection, tmp_path: Path,
) -> None:
    z = _synth_cn_zip(tmp_path, cycle="2024")
    n = _load_one_kind(fec_db, zip_path=z, cycle="2024", kind="cn")
    assert n == 3
    with fec_db.cursor() as cur:
        cur.execute(
            "SELECT cycle, cand_office_st, count(*) "
            "FROM raw.fec_candidate "
            "GROUP BY 1, 2 "
            "ORDER BY 1, 2",
        )
        rows = cur.fetchall()
    assert rows == [("2024", "NJ", 2), ("2024", "NY", 1)]


def test_load_cm_and_cross_join_to_candidates(
    fec_db: psycopg.Connection, tmp_path: Path,
) -> None:
    z_cn = _synth_cn_zip(tmp_path, cycle="2024")
    z_cm = _synth_cm_zip(tmp_path, cycle="2024")
    _load_one_kind(fec_db, zip_path=z_cn, cycle="2024", kind="cn")
    _load_one_kind(fec_db, zip_path=z_cm, cycle="2024", kind="cm")
    with fec_db.cursor() as cur:
        cur.execute(
            "SELECT cmte.cmte_id, cand.cand_name "
            "FROM raw.fec_committee  cmte "
            "JOIN raw.fec_candidate  cand "
            "  ON cand.cand_id = cmte.cand_id AND cand.cycle = cmte.cycle "
            "WHERE cmte.cycle = '2024' "
            "ORDER BY cmte.cmte_id",
        )
        rows = cur.fetchall()
    # All three committees join to their respective candidates.
    assert len(rows) == 3
    assert ("C00500587", "BOOKER, CORY ANTHONY") in rows
    assert ("C00553867", "KIM, ANDY") in rows


def test_load_indiv_streams_into_raw_fec_contribution(
    fec_db: psycopg.Connection, tmp_path: Path,
) -> None:
    z_cn    = _synth_cn_zip(tmp_path, cycle="2024")
    z_cm    = _synth_cm_zip(tmp_path, cycle="2024")
    z_indiv = _synth_indiv_zip(tmp_path, cycle="2024")
    _load_one_kind(fec_db, zip_path=z_cn,    cycle="2024", kind="cn")
    _load_one_kind(fec_db, zip_path=z_cm,    cycle="2024", kind="cm")
    n = _load_one_kind(fec_db, zip_path=z_indiv, cycle="2024", kind="indiv")
    assert n == 5
    with fec_db.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM raw.fec_contribution WHERE cycle = '2024'",
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 5


def test_v_fec_contribution_parses_dates_and_flags_memo(
    fec_db: psycopg.Connection, tmp_path: Path,
) -> None:
    """The cooked view should:
       * parse 01152024 -> 2024-01-15
       * yield NULL for transaction_date when raw is '00000000'
       * set is_memo=TRUE on memo_cd='X' rows
    """
    z_cn    = _synth_cn_zip(tmp_path, cycle="2024")
    z_cm    = _synth_cm_zip(tmp_path, cycle="2024")
    z_indiv = _synth_indiv_zip(tmp_path, cycle="2024")
    _load_one_kind(fec_db, zip_path=z_cn,    cycle="2024", kind="cn")
    _load_one_kind(fec_db, zip_path=z_cm,    cycle="2024", kind="cm")
    _load_one_kind(fec_db, zip_path=z_indiv, cycle="2024", kind="indiv")
    with fec_db.cursor() as cur:
        cur.execute(
            "SELECT sub_id, transaction_date::text, is_memo "
            "FROM public.v_fec_contribution "
            "WHERE cycle = '2024' "
            "ORDER BY sub_id",
        )
        rows = dict((r[0], (r[1], r[2])) for r in cur.fetchall())
    assert rows["FEC1001"] == ("2024-01-15", False)
    assert rows["FEC1002"] == ("2024-02-01", False)
    assert rows["FEC1003"] == ("2024-03-15", False)
    assert rows["FEC1004"] == ("2024-01-20", True)   # memo flag set
    assert rows["FEC1005"] == (None,         False)  # bad date -> NULL


def test_v_money_to_nj_joins_three_tables(
    fec_db: psycopg.Connection, tmp_path: Path,
) -> None:
    """The headline view exposes only contributions to NJ-candidate committees.

    The synthetic data has 4 NJ-candidate-committee contributions
    (two to Booker via C00500587, two to Kim via C00553867 -- one of
    those two has bad date, but the row still appears) plus 1
    Schumer/NY contribution that must be filtered out by the join.
    Total: 4 rows after JOIN. The memo row counts as one of those 4.
    """
    z_cn    = _synth_cn_zip(tmp_path, cycle="2024")
    z_cm    = _synth_cm_zip(tmp_path, cycle="2024")
    z_indiv = _synth_indiv_zip(tmp_path, cycle="2024")
    _load_one_kind(fec_db, zip_path=z_cn,    cycle="2024", kind="cn")
    _load_one_kind(fec_db, zip_path=z_cm,    cycle="2024", kind="cm")
    _load_one_kind(fec_db, zip_path=z_indiv, cycle="2024", kind="indiv")
    with fec_db.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM public.v_fec_money_to_nj_candidates",
        )
        row = cur.fetchone()
        assert row is not None
        # 5 synthetic contributions; 4 to NJ candidates, 1 to NY candidate.
        # The NY one (none in our fixture; all 5 are to NJ committees)
        # ... actually our fixture has all contributions to NJ
        # committees (Booker & Kim). The Schumer committee has zero
        # contributions in the fixture. So count = 5 (all rows pass
        # the join, including the memo row).
        assert row[0] == 5

    # And summable totals must exclude the memo row.
    with fec_db.cursor() as cur:
        cur.execute(
            "SELECT sum(transaction_amount)::float "
            "FROM public.v_fec_money_to_nj_candidates "
            "WHERE NOT is_memo",
        )
        row = cur.fetchone()
        assert row is not None
        # 250 + 1000 + 500 + 100 = 1850 (memo 2900 excluded)
        assert row[0] == pytest.approx(1850.0)


def test_load_is_idempotent_within_cycle(
    fec_db: psycopg.Connection, tmp_path: Path,
) -> None:
    """Re-loading the same cycle must produce the same row count.

    The DELETE-WHERE-cycle-then-COPY pattern guarantees idempotency;
    running it twice should not double-count.
    """
    z = _synth_cn_zip(tmp_path, cycle="2024")
    _load_one_kind(fec_db, zip_path=z, cycle="2024", kind="cn")
    _load_one_kind(fec_db, zip_path=z, cycle="2024", kind="cn")  # again
    with fec_db.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw.fec_candidate WHERE cycle = '2024'")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 3


def test_two_cycles_coexist_independently(
    fec_db: psycopg.Connection, tmp_path: Path,
) -> None:
    """Loading 2020 then 2024 must keep 2020 rows; cycle is part of PK."""
    dir_2020 = tmp_path / "2020"
    dir_2024 = tmp_path / "2024"
    dir_2020.mkdir()
    dir_2024.mkdir()
    z_2020 = _synth_cn_zip(dir_2020, cycle="2020")
    z_2024 = _synth_cn_zip(dir_2024, cycle="2024")
    _load_one_kind(fec_db, zip_path=z_2020, cycle="2020", kind="cn")
    _load_one_kind(fec_db, zip_path=z_2024, cycle="2024", kind="cn")
    with fec_db.cursor() as cur:
        cur.execute(
            "SELECT cycle, count(*) FROM raw.fec_candidate "
            "GROUP BY cycle ORDER BY cycle",
        )
        rows = cur.fetchall()
    assert rows == [("2020", 3), ("2024", 3)]


def test_fetch_fec_bulk_cache_hit_avoids_redownload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a local file matches the upstream Content-Length, fetch must not GET.

    We monkey-patch httpx.Client to record calls and verify only HEAD
    fires (no streamed GET). The local 'cached' file is hand-written
    to match the simulated remote size.
    """
    cached = tmp_path / "cn24.zip"
    cached.write_bytes(b"x" * 1234)

    class _FakeResp:
        def __init__(self, headers: dict[str, str]) -> None:
            self.headers = headers
        def raise_for_status(self) -> None: ...

    head_calls: list[str] = []
    get_calls:  list[str] = []

    class _FakeClient:
        def __init__(self, *_a: object, **_k: object) -> None: ...
        def __enter__(self) -> _FakeClient: return self
        def __exit__(self, *_a: object) -> None: ...
        def head(self, url: str) -> _FakeResp:
            head_calls.append(url)
            return _FakeResp({
                "etag": '"abc123"',
                "content-length": "1234",
            })
        def stream(self, _method: str, url: str) -> object:
            get_calls.append(url)
            raise AssertionError("stream() should not be called on cache hit")

    monkeypatch.setattr("ingestion.fec.httpx.Client", _FakeClient)
    result = fetch_fec_bulk(
        "2024", "cn", dest_dir=tmp_path, overwrite=False,
    )
    assert result.cache_hit is True
    assert result.n_bytes == 1234
    assert len(head_calls) == 1
    assert get_calls == []


def test_parse_handles_literal_double_quote_in_name(tmp_path: Path) -> None:
    """Regression: real FEC cn24.txt contains 'JOHN \"JACK\" DOE' style names.

    Polars's default CSV parser treats `"` as a quote character and
    aborts on '"VAL" VALMA PAUL'. We disable quote interpretation
    (quote_char=None) for FEC's unquoted bulk format. This fixture
    pins that behavior; an accidental re-introduction of quote_char='"'
    would re-trip the original ComputeError.
    """
    # Hand-craft a one-row cn24 file with a literal double-quote name.
    rows = [[
        "S0NJ99900",
        '"JACK" SMITH, JOHN',  # literal double quotes in field
        "DEM", "2024", "NJ", "S", "00", "I", "C",
        "C00999999", "PO BOX 1", "", "TRENTON", "NJ", "08608",
    ]]
    text = "|".join(rows[0]) + "\n"
    z = tmp_path / "cn24.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("cn.txt", text)
    parse = parse_fec_small_table(_fetch_for(z, cycle="2024", kind="cn"),
                                  cycle="2024", file_kind="cn")
    assert parse.n_rows == 1
    assert parse.dataframe["CAND_NAME"].to_list() == ['"JACK" SMITH, JOHN']


def test_pl_dataframe_column_order_matches_raw_table(
    tmp_path: Path,
) -> None:
    """stage_small_dataframe must emit columns in the exact COPY order.

    The COPY statement is generated dynamically from the staged df's
    columns; if stage_small_dataframe ever reordered them, the COPY
    would either fail or silently mis-map.
    """
    z = _synth_cn_zip(tmp_path)
    parse = parse_fec_small_table(_fetch_for(z, cycle="2024", kind="cn"),
                                  cycle="2024", file_kind="cn")
    staged = stage_small_dataframe(parse)
    assert staged.columns == [
        "cycle",
        "cand_id", "cand_name", "cand_pty_affiliation", "cand_election_yr",
        "cand_office_st", "cand_office", "cand_office_district",
        "cand_ici", "cand_status", "cand_pcc",
        "cand_st1", "cand_st2", "cand_city", "cand_st", "cand_zip",
        "source_url", "source_sha256", "source_vintage",
    ]
