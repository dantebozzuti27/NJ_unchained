"""Tests for the Tier 4 v2 fraud-detection metric layer.

Test taxonomy
-------------
1. Schema validation (no DB)
   - FraudMetricCatalogEntry / FraudMetricResult / FraudMetricSummary
     accept the dict shapes returned by queries_fec_metrics.

2. Catalog + helper unit tests (no DB)
   - Catalog ordering: structural before contribution; alphabetical within tier.
   - get_metric raises KeyError for unknown ids.
   - _clamp_limit / _clamp_offset / _resolve_sort_dir behave as documented.

3. Live integration (live_pg)
   - Bootstrap migrations + load synthetic cn / cm rows with KNOWN
     anomalies (multi-committee treasurer, no-PCC candidate, address
     cluster, name collision).
   - /fec/metrics catalog endpoint returns 8 entries.
   - /fec/metrics/_summary returns positive counts for the metrics
     the synthetic fixture is engineered to trigger; zero for the rest.
   - /fec/metrics/{id} returns paginated flagged rows.
   - 404 for unknown metric id; 400 for unsafe sort_by.
   - CSV export streams header + rows + array fields rendered as
     {a,b,c} (Postgres array-literal style).
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
    load_fec_small_table,
    parse_fec_small_table,
)
from serving.models import (
    FraudMetricCatalogEntry,
    FraudMetricResult,
    FraudMetricSummary,
)
from serving.queries_fec_metrics import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    _clamp_limit,
    _clamp_offset,
    _resolve_sort_dir,
    get_catalog,
    get_metric,
)

if TYPE_CHECKING:
    import psycopg


# ============================================================================
# 1. Schema validation
# ============================================================================


def test_catalog_entry_schema_round_trips() -> None:
    e = FraudMetricCatalogEntry(
        id="x", name="X", tier="structural",
        description="desc", threshold_note="t",
        sort_default="severity_score",
        primary_key_cols=["a", "b"],
    )
    assert e.id == "x"
    assert e.primary_key_cols == ["a", "b"]


def test_summary_schema_accepts_empty_counts() -> None:
    s = FraudMetricSummary(cycle="", counts={})
    assert s.counts == {}


def test_result_schema_accepts_empty_rows() -> None:
    r = FraudMetricResult(
        metric=FraudMetricCatalogEntry(
            id="y", name="Y", tier="structural", description="d",
            threshold_note=None, sort_default="cycle",
            primary_key_cols=[],
        ),
        rows=[], total_count=0, limit=100, offset=0,
    )
    assert r.total_count == 0


# ============================================================================
# 2. Catalog + helper unit tests
# ============================================================================


def test_catalog_lists_eight_structural_metrics() -> None:
    catalog = get_catalog()
    assert len(catalog) == 8
    assert all(m.tier == "structural" for m in catalog)


def test_catalog_is_sorted_structural_then_alpha() -> None:
    catalog = get_catalog()
    ids = [m.id for m in catalog]
    structural_ids = [m.id for m in catalog if m.tier == "structural"]
    assert structural_ids == sorted(structural_ids)
    contribution_ids = [m.id for m in catalog if m.tier == "contribution"]
    # Structural first, then contribution -- once Tier B lands.
    assert ids == structural_ids + contribution_ids


def test_get_metric_raises_for_unknown_id() -> None:
    with pytest.raises(KeyError):
        get_metric("does_not_exist")


def test_get_metric_returns_spec_for_known_id() -> None:
    spec = get_metric("treasurer_concentration")
    assert spec.tier == "structural"
    assert spec.view == "derived.fec_treasurer_concentration"
    assert "n_committees" in spec.sort_cols


def test_clamp_limit_default_when_none() -> None:
    assert _clamp_limit(None) == DEFAULT_LIMIT


def test_clamp_limit_caps_at_hard_cap() -> None:
    assert _clamp_limit(MAX_LIMIT * 50) == MAX_LIMIT


def test_clamp_limit_floors_at_one() -> None:
    assert _clamp_limit(0)  == 1
    assert _clamp_limit(-3) == 1


def test_clamp_offset_floors_at_zero() -> None:
    assert _clamp_offset(None) == 0
    assert _clamp_offset(-1)   == 0


def test_resolve_sort_dir_defaults_to_desc() -> None:
    assert _resolve_sort_dir(None).as_string(None)   == "DESC"
    assert _resolve_sort_dir("").as_string(None)     == "DESC"


def test_resolve_sort_dir_normalizes_case() -> None:
    assert _resolve_sort_dir("asc").as_string(None)  == "ASC"
    assert _resolve_sort_dir("DESC").as_string(None) == "DESC"


def test_resolve_sort_dir_rejects_garbage_to_default() -> None:
    """Defense in depth: a malicious sort_dir does not raise; we
    silently coerce to DESC. The caller should also enforce a
    pattern at the route layer (we already do)."""
    assert _resolve_sort_dir("'; DROP TABLE foo;").as_string(None) == "DESC"


# ============================================================================
# 3. Live integration tests
# ============================================================================


pytestmark = pytest.mark.live_pg


@pytest.fixture
def fec_db(live_pg: psycopg.Connection) -> psycopg.Connection:
    """Apply all migrations + seeds; yield the conn (drop everything first)."""
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
            "END $$;",
        )
    conn.commit()
    apply_migrations(conn, discover(MIGRATIONS_DIR))
    apply_migrations(conn, discover(SEEDS_DIR))
    return conn


def _synth_anomaly_cn_zip(tmp_path: Path) -> Path:
    """Five candidates, engineered to trip:

    - candidate_no_pcc            -> KAPLAN (no PCC), VANN (empty PCC)
    - candidate_broken_pcc        -> JONES (PCC C99999999 not in cm)
    - candidate_namesakes         -> two SMITH, JOHN in same state/office
    - treasurer_is_candidate      -> ADAMS (candidate name matches their PCC's tres_nm)
    """
    rows = [
        # PCC OK; treasurer normal
        ["S0NJ00001", "BOOKER, CORY ANTHONY", "DEM", "2024", "NJ", "S",
         "00", "I", "C", "C00500001",
         "1421 RAYBURN", "", "WASHINGTON", "DC", "20515"],
        # No PCC
        ["S0NJ00002", "KAPLAN, KENNETH",      "REP", "2024", "NJ", "S",
         "00", "C", "C", "",
         "PO BOX 1",   "", "TRENTON",   "NJ", "08608"],
        # Broken PCC: C99999999 does not exist in cm
        ["H0NJ00003", "JONES, MARY",          "DEM", "2024", "NJ", "H",
         "01", "C", "C", "C99999999",
         "5 MAIN ST",  "", "PRINCETON", "NJ", "08540"],
        # Namesake: same canonical name, same state, same office
        ["H0NJ00004", "SMITH, JOHN",          "DEM", "2024", "NJ", "H",
         "02", "C", "C", "C00500004",
         "10 OAK ST",  "", "MARLTON",   "NJ", "08053"],
        ["H0NJ00005", "SMITH, JOHN",          "REP", "2024", "NJ", "H",
         "02", "C", "C", "C00500005",
         "12 OAK ST",  "", "MARLTON",   "NJ", "08053"],
        # Treasurer-is-candidate: PCC C00500006's tres_nm == cand_name
        ["S0NJ00006", "ADAMS, JANE",          "IND", "2024", "NJ", "S",
         "00", "O", "C", "C00500006",
         "1 ELM ST",   "", "MONTCLAIR", "NJ", "07042"],
    ]
    text_buf = io.StringIO()
    for row in rows:
        text_buf.write("|".join(row) + "\n")
    zip_path = tmp_path / "cn24.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("cn.txt", text_buf.getvalue())
    return zip_path


def _synth_anomaly_cm_zip(tmp_path: Path) -> Path:
    """Eight committees, engineered to trip:

    - treasurer_concentration       -> "DOE, JOHN" listed on 3 different cmtes
    - committee_address_clusters    -> 3 cmtes at "PO BOX 99" / NJ / 12345
    - committee_name_collisions     -> two cmtes named "FRIENDS OF JOHN"
    - candidate_multiple_pccs       -> SMITH (H0NJ00004) has TWO 'P' cmtes
    - treasurer_is_candidate        -> ADAMS PCC tres_nm = "ADAMS, JANE"
    """
    rows = [
        ["C00500001", "BOOKER FOR SENATE",      "WHITE, ELIZABETH",
         "PO BOX 32157", "", "NEWARK", "NJ", "07102",
         "P", "S", "DEM", "Q", "", "", "S0NJ00001"],
        # Three cmtes share treasurer "DOE, JOHN" + share PO BOX 99 cluster
        ["C00500010", "FRIENDS OF JOHN",        "DOE, JOHN",
         "PO BOX 99", "", "TRENTON", "NJ", "12345",
         "U", "N", "DEM", "Q", "", "", ""],
        ["C00500011", "JOHN VICTORY FUND",      "DOE, JOHN",
         "PO BOX 99", "", "TRENTON", "NJ", "12345",
         "U", "N", "DEM", "Q", "", "", ""],
        ["C00500012", "JOHN LEADERSHIP PAC",    "DOE, JOHN",
         "PO BOX 99", "", "TRENTON", "NJ", "12345",
         "D", "N", "DEM", "Q", "", "", ""],
        # Name collision with C00500010
        ["C00500013", "FRIENDS OF JOHN",        "ROE, RICHARD",
         "5 MAIN ST", "", "PRINCETON", "NJ", "08540",
         "U", "N", "DEM", "Q", "", "", ""],
        # SMITH multi-PCC: both designated P, both linked to H0NJ00004
        ["C00500004", "SMITH FOR HOUSE",        "SMITH, JANE",
         "10 OAK ST", "", "MARLTON", "NJ", "08053",
         "P", "H", "DEM", "Q", "", "", "H0NJ00004"],
        ["C00500014", "JOHN SMITH HOUSE 2024",  "SMITH, JANE",
         "10 OAK ST", "", "MARLTON", "NJ", "08053",
         "P", "H", "DEM", "Q", "", "", "H0NJ00004"],
        # Treasurer-is-candidate
        ["C00500006", "ADAMS FOR SENATE",       "ADAMS, JANE",
         "1 ELM ST", "", "MONTCLAIR", "NJ", "07042",
         "P", "S", "IND", "Q", "", "", "S0NJ00006"],
        # And SMITH JOHN namesake's other PCC for completeness
        ["C00500005", "SMITH FOR CONGRESS",     "MILLER, BOB",
         "12 OAK ST", "", "MARLTON", "NJ", "08053",
         "P", "H", "REP", "Q", "", "", "H0NJ00005"],
    ]
    text_buf = io.StringIO()
    for row in rows:
        text_buf.write("|".join(row) + "\n")
    zip_path = tmp_path / "cm24.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("cm.txt", text_buf.getvalue())
    return zip_path


def _fetch_for(zip_path: Path, *, cycle: str) -> FetchResult:
    return FetchResult(
        path=zip_path,
        source_url=f"file://{zip_path}",
        source_sha256="0" * 64,
        source_vintage=f"test-{cycle}",
        n_bytes=zip_path.stat().st_size,
        cache_hit=False,
    )


def _load_anomaly_synthetic(conn: psycopg.Connection, tmp_path: Path) -> None:
    z_cn = _synth_anomaly_cn_zip(tmp_path)
    z_cm = _synth_anomaly_cm_zip(tmp_path)
    parse_cn = parse_fec_small_table(_fetch_for(z_cn, cycle="2024"),
                                     cycle="2024", file_kind="cn")
    parse_cm = parse_fec_small_table(_fetch_for(z_cm, cycle="2024"),
                                     cycle="2024", file_kind="cm")
    load_fec_small_table(parse_cn, conn)
    load_fec_small_table(parse_cm, conn)
    conn.commit()


@pytest.fixture
def serving_client(
    fec_db: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    """A FastAPI TestClient backed by the fec_db connection.

    Same monkeypatch dance as test_serving_fec.py: substitute
    borrow_connection across every imported route module so all
    queries hit the same in-test ephemeral connection.
    """
    _load_anomaly_synthetic(fec_db, tmp_path)
    from contextlib import contextmanager

    from serving import db as serving_db

    @contextmanager
    def _borrow():  # type: ignore[no-untyped-def]
        yield fec_db

    monkeypatch.setattr(serving_db, "borrow_connection", _borrow)
    from serving.routes import fec as fec_route
    from serving.routes import fec_export as fec_export_route
    from serving.routes import fec_metrics as fec_metrics_route
    monkeypatch.setattr(fec_route,         "borrow_connection", _borrow)
    monkeypatch.setattr(fec_export_route,  "borrow_connection", _borrow)
    monkeypatch.setattr(fec_metrics_route, "borrow_connection", _borrow)

    from serving.app import create_app
    return TestClient(create_app())


# --------------------------------------------------------------------------
# /fec/metrics -- catalog + summary
# --------------------------------------------------------------------------


def test_metrics_catalog_returns_eight_entries(serving_client: TestClient) -> None:
    resp = serving_client.get("/fec/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 8
    ids = {m["id"] for m in body}
    expected = {
        "treasurer_concentration", "candidate_no_pcc",
        "candidate_broken_pcc", "candidate_multiple_pccs",
        "committee_address_clusters", "committee_name_collisions",
        "candidate_namesakes", "treasurer_is_candidate",
    }
    assert ids == expected


def test_metrics_summary_returns_engineered_counts(
    serving_client: TestClient,
) -> None:
    resp = serving_client.get("/fec/metrics/_summary?cycle=2024")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cycle"] == "2024"
    counts = body["counts"]

    # Engineered anomalies in the synthetic fixture:
    assert counts["treasurer_concentration"]    >= 1   # DOE, JOHN x 3 cmtes
    assert counts["candidate_no_pcc"]           == 1   # KAPLAN
    assert counts["candidate_broken_pcc"]       == 1   # JONES -> C99999999
    assert counts["candidate_multiple_pccs"]    == 1   # SMITH x 2 PCCs
    assert counts["committee_address_clusters"] >= 1   # PO BOX 99 x 3
    assert counts["committee_name_collisions"]  == 1   # FRIENDS OF JOHN x 2
    assert counts["candidate_namesakes"]        == 1   # SMITH, JOHN x 2
    assert counts["treasurer_is_candidate"]     == 1   # ADAMS, JANE


def test_metrics_summary_without_cycle_returns_global_counts(
    serving_client: TestClient,
) -> None:
    resp = serving_client.get("/fec/metrics/_summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cycle"] == ""
    # Unscoped counts are >= cycle-scoped counts (no other cycle loaded).
    assert body["counts"]["candidate_no_pcc"] >= 1


# --------------------------------------------------------------------------
# /fec/metrics/{id} -- per-metric paginated rows
# --------------------------------------------------------------------------


def test_metric_treasurer_concentration_returns_doe_at_top(
    serving_client: TestClient,
) -> None:
    resp = serving_client.get(
        "/fec/metrics/treasurer_concentration",
        params={"cycle": "2024", "sort_by": "n_committees", "sort_dir": "DESC"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_count"] >= 1
    top = body["rows"][0]
    assert top["tres_nm_canonical"] == "DOE, JOHN"
    assert top["n_committees"]      == 3
    # severity_score == n_committees (capped at 1000)
    assert top["severity_score"]    == 3
    # Schema validation
    FraudMetricResult.model_validate(body)


def test_metric_committee_address_clusters_returns_po_box_99(
    serving_client: TestClient,
) -> None:
    resp = serving_client.get(
        "/fec/metrics/committee_address_clusters",
        params={"cycle": "2024"},
    )
    body = resp.json()
    assert body["total_count"] >= 1
    addrs = {r["address_canonical"] for r in body["rows"]}
    assert "PO BOX 99" in addrs


def test_metric_candidate_namesakes_returns_smith_john(
    serving_client: TestClient,
) -> None:
    resp = serving_client.get(
        "/fec/metrics/candidate_namesakes",
        params={"cycle": "2024"},
    )
    body = resp.json()
    assert body["total_count"] == 1
    row = body["rows"][0]
    assert row["cand_name_canonical"] == "SMITH, JOHN"
    assert row["n_cand_ids"]          == 2


def test_metric_treasurer_is_candidate_returns_adams(
    serving_client: TestClient,
) -> None:
    resp = serving_client.get(
        "/fec/metrics/treasurer_is_candidate",
        params={"cycle": "2024"},
    )
    body = resp.json()
    assert body["total_count"] == 1
    row = body["rows"][0]
    assert row["cand_name"]      == "ADAMS, JANE"
    assert row["treasurer_name"] == "ADAMS, JANE"


def test_metric_unknown_id_returns_404_with_catalog_hint(
    serving_client: TestClient,
) -> None:
    resp = serving_client.get("/fec/metrics/does_not_exist")
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert "Unknown fraud metric" in detail["error"]
    assert "treasurer_concentration" in detail["available_metrics"]


def test_metric_bad_sort_returns_400_with_allowed_columns(
    serving_client: TestClient,
) -> None:
    resp = serving_client.get(
        "/fec/metrics/treasurer_concentration",
        params={"sort_by": "1; DROP TABLE foo"},
    )
    assert resp.status_code == 400
    msg = resp.json()["detail"]
    assert "not allowed" in msg
    assert "n_committees" in msg


def test_metric_pagination_limit_one(serving_client: TestClient) -> None:
    """Validate limit/offset behavior on a metric with multiple rows."""
    resp1 = serving_client.get(
        "/fec/metrics/treasurer_concentration",
        params={"cycle": "2024", "limit": 1, "offset": 0},
    )
    body1 = resp1.json()
    assert len(body1["rows"]) == 1
    assert body1["limit"]  == 1
    assert body1["offset"] == 0


# --------------------------------------------------------------------------
# /fec/metrics/{id}/csv -- streaming export
# --------------------------------------------------------------------------


def test_metric_csv_streams_header_and_rows(
    serving_client: TestClient,
) -> None:
    resp = serving_client.get(
        "/fec/metrics/treasurer_concentration/csv?cycle=2024",
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="metric_treasurer_concentration_2024.csv"' \
        in resp.headers["content-disposition"]
    body = resp.text
    lines = [ln for ln in body.splitlines() if ln]
    assert lines[0].startswith("cycle,tres_nm_canonical,n_committees")
    assert any("DOE, JOHN" in ln for ln in lines[1:])
    # Array fields rendered as Postgres-style {a,b,c}
    assert any(ln.endswith("}") or ln.endswith('}"') for ln in lines[1:])


def test_metric_csv_unknown_id_returns_404(
    serving_client: TestClient,
) -> None:
    resp = serving_client.get("/fec/metrics/does_not_exist/csv")
    assert resp.status_code == 404


def test_metric_csv_bad_sort_returns_400(
    serving_client: TestClient,
) -> None:
    resp = serving_client.get(
        "/fec/metrics/treasurer_concentration/csv",
        params={"sort_by": "1; DROP TABLE foo"},
    )
    assert resp.status_code == 400
