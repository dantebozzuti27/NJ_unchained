"""Tests for the Tier 4 v3 fraud-risk read API.

Test taxonomy
-------------
1. Schema validation + decomposition unit tests (no DB)
   * VALID_ENTITY_KINDS matches the L1 CHECK constraint domain.
   * SORT_COLS contract: contains risk_score; default is risk_score.
   * _phi: matches the SQL formula at the documented anchors.
   * _decompose_score: shares sum to 100 when raw_sum > 0;
     all zero (and total raw_sum == 0) when no signal exceeds the floor.
   * _validate_entity_kind / _validate_score_range raise on bad input.
   * RiskQueueRow / RiskEntityPanel / RiskSignalObservation accept the
     dict shapes the query layer returns.

2. Live integration (live_pg)
   * Bootstrap migrations 050-052; load the same synthetic
     anomaly fixture from test_serving_fec_metrics.py; invoke the
     L1 dispatcher; then exercise both routes.
   * GET /fec/risk/entities -> sorted DESC by risk_score, filterable
     by cycle / entity_kind / signal_id / min_score / max_score.
   * Bad sort_by -> 400; bad entity_kind -> 400; out-of-range
     min_score / max_score -> 400.
   * GET /fec/risk/entities/{kind}/{id} -> evidence panel with
     observations sorted by score_share_pct DESC; sum of shares is
     100 (within rounding); unknown entity -> 404.
"""

from __future__ import annotations

import io
import math
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
    RiskEntityPanel,
    RiskQueueResponse,
    RiskQueueRow,
    RiskSignalObservation,
)
from serving.queries_fec_risk import (
    DEFAULT_LIMIT,
    DEFAULT_SORT_BY,
    EVIDENCE_CSV_COLUMNS,
    MAX_LIMIT,
    SCORE_GAMMA,
    SCORE_K,
    SCORE_PCT_FLOOR,
    SORT_COLS,
    VALID_ENTITY_KINDS,
    _clamp_limit,
    _clamp_offset,
    _decompose_score,
    _phi,
    _validate_entity_kind,
    _validate_score_range,
    evidence_csv_rows,
)

if TYPE_CHECKING:
    import psycopg


# ============================================================================
# 1. Constants + helpers + decomposition (no DB)
# ============================================================================


def test_valid_entity_kinds_match_l1_check_constraint() -> None:
    """KEEP IN SYNC with derived.fraud_signal_observation's CHECK clause
    (migration 050). If the SQL domain expands, this test must update."""
    expected = frozenset({
        "committee", "candidate", "treasurer", "address", "donor_cluster",
    })
    assert expected == VALID_ENTITY_KINDS


def test_sort_cols_contains_risk_score_and_default_is_risk_score() -> None:
    assert "risk_score" in SORT_COLS
    assert DEFAULT_SORT_BY == "risk_score"


def test_pagination_constants_are_sane() -> None:
    assert DEFAULT_LIMIT >= 1
    assert MAX_LIMIT >= DEFAULT_LIMIT


def test_score_constants_match_migration_052() -> None:
    """KEEP IN SYNC with derived.fraud_risk_score (migration 052)."""
    assert SCORE_GAMMA == 2
    assert SCORE_K == 50
    assert SCORE_PCT_FLOOR == 0.95


def test_phi_zero_at_or_below_floor() -> None:
    assert _phi(5, 0.50) == 0.0
    assert _phi(5, 0.95) == 0.0   # at the floor, contributes nothing
    assert _phi(1, 0.94) == 0.0


def test_phi_quadratic_above_floor() -> None:
    # severity=3, p=0.99 -> 3 * (0.04)^2 = 3 * 0.0016 = 0.0048
    assert math.isclose(_phi(3, 0.99), 3 * 0.04 ** 2, abs_tol=1e-12)


def test_phi_severity_linear_at_fixed_percentile() -> None:
    """At a fixed peer_percentile, doubling severity doubles phi.

    Tests the 'severity is a multiplier' contract of the formula.
    """
    p = 0.99
    excess = (p - SCORE_PCT_FLOOR) ** SCORE_GAMMA
    for sev in (1, 2, 3, 4, 5):
        assert math.isclose(_phi(sev, p), sev * excess, abs_tol=1e-12)


def test_decompose_zero_when_no_signal_exceeds_floor() -> None:
    per_signal, raw_sum = _decompose_score(
        signal_ids=["a", "b"],
        severities=[5, 3],
        percentiles=[0.50, 0.94],
    )
    assert raw_sum == 0.0
    # All shares are exactly 0 -- not NaN, not 50/50.
    assert all(p["score_share_pct"] == 0.0 for p in per_signal)
    assert all(p["phi_contribution"] == 0.0 for p in per_signal)


def test_decompose_shares_sum_to_100_when_any_signal_fires() -> None:
    per_signal, raw_sum = _decompose_score(
        signal_ids=["a", "b", "c"],
        severities=[3, 5, 1],
        percentiles=[0.99, 0.97, 0.96],
    )
    assert raw_sum > 0
    total = sum(p["score_share_pct"] for p in per_signal)
    assert math.isclose(total, 100.0, abs_tol=1e-9)


def test_decompose_share_proportional_to_phi() -> None:
    """Share is exactly phi_s / raw_sum * 100. Two signals -> ratios match."""
    per_signal, _raw_sum = _decompose_score(
        signal_ids=["a", "b"],
        severities=[5, 5],
        percentiles=[0.99, 0.97],
    )
    phi_a = _phi(5, 0.99)
    phi_b = _phi(5, 0.97)
    assert math.isclose(per_signal[0]["phi_contribution"], phi_a, abs_tol=1e-12)
    assert math.isclose(per_signal[1]["phi_contribution"], phi_b, abs_tol=1e-12)
    expected_a_share = phi_a / (phi_a + phi_b) * 100.0
    assert math.isclose(per_signal[0]["score_share_pct"], expected_a_share, abs_tol=1e-9)


def test_decompose_raises_on_array_length_mismatch() -> None:
    with pytest.raises(ValueError, match="length mismatch"):
        _decompose_score(["a", "b"], [3, 1], [0.99])


def test_validate_entity_kind_accepts_none() -> None:
    _validate_entity_kind(None)  # no exception


def test_validate_entity_kind_accepts_each_known_kind() -> None:
    for k in VALID_ENTITY_KINDS:
        _validate_entity_kind(k)


def test_validate_entity_kind_rejects_unknown() -> None:
    with pytest.raises(KeyError, match="entity_kind"):
        _validate_entity_kind("politician")
    with pytest.raises(KeyError, match="entity_kind"):
        _validate_entity_kind("")


def test_validate_score_range_accepts_legal_ranges() -> None:
    _validate_score_range(None, None)
    _validate_score_range(0.0, 100.0)
    _validate_score_range(60.0, 60.0)


def test_validate_score_range_rejects_out_of_bounds() -> None:
    for bad in (-0.001, 100.001, -10, 1000):
        with pytest.raises(KeyError, match="out of range"):
            _validate_score_range(bad, None)
        with pytest.raises(KeyError, match="out of range"):
            _validate_score_range(None, bad)


def test_validate_score_range_rejects_inverted() -> None:
    with pytest.raises(KeyError, match="empty range"):
        _validate_score_range(80.0, 50.0)


def test_clamp_limit_default_when_none() -> None:
    assert _clamp_limit(None) == DEFAULT_LIMIT


def test_clamp_limit_caps_at_hard_cap() -> None:
    assert _clamp_limit(MAX_LIMIT * 50) == MAX_LIMIT


def test_clamp_offset_floors_at_zero() -> None:
    assert _clamp_offset(None) == 0
    assert _clamp_offset(-7) == 0


# ============================================================================
# 2. Pydantic schema acceptance
# ============================================================================


def test_risk_queue_row_accepts_minimum_dict() -> None:
    import datetime as dt
    row = RiskQueueRow(
        cycle="2024", entity_kind="treasurer", entity_id="DOE, JOHN",
        risk_score=72.50, n_signals_fired=2, max_severity=3,
        max_peer_percentile=0.99, avg_peer_percentile=0.97,
        primary_peer_bucket="kind=treasurer", signals_fired=["a", "b"],
        last_observation_at=dt.datetime(2026, 5, 4, tzinfo=dt.UTC),
    )
    assert row.risk_score == 72.50


def test_risk_signal_observation_accepts_floor_zero() -> None:
    """A signal at the floor with raw_value=NULL still validates."""
    obs = RiskSignalObservation(
        signal_id="no_pcc",
        severity=4, peer_percentile=0.96, peer_bucket="state=NJ",
        raw_value=None, evidence_url="/fec/metrics/no_pcc?cycle=2024",
        phi_contribution=0.0, score_share_pct=0.0,
    )
    assert obs.raw_value is None


def test_risk_queue_response_envelope() -> None:
    r = RiskQueueResponse(
        rows=[], total_count=0, limit=100, offset=0, filters={"cycle": None},
    )
    assert r.total_count == 0


# ============================================================================
# 3. Live integration tests
# ============================================================================


pytestmark = pytest.mark.live_pg


@pytest.fixture
def fec_db(live_pg: psycopg.Connection) -> psycopg.Connection:
    """Apply migrations 001..052; drop everything first to keep tests
    hermetic (other tests in the same suite may have left state).
    """
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


def _synth_cn_zip(tmp_path: Path) -> Path:
    """Same fixture shape as test_serving_fec_metrics; engineered to fire
    every Tier-A structural signal. We rebuild it locally rather than
    importing to avoid coupling test files."""
    rows = [
        ["S0NJ00001", "BOOKER, CORY ANTHONY", "DEM", "2024", "NJ", "S",
         "00", "I", "C", "C00500001",
         "1421 RAYBURN", "", "WASHINGTON", "DC", "20515"],
        ["S0NJ00002", "KAPLAN, KENNETH",      "REP", "2024", "NJ", "S",
         "00", "C", "C", "",
         "PO BOX 1",   "", "TRENTON",   "NJ", "08608"],
        ["H0NJ00003", "JONES, MARY",          "DEM", "2024", "NJ", "H",
         "01", "C", "C", "C99999999",
         "5 MAIN ST",  "", "PRINCETON", "NJ", "08540"],
        ["H0NJ00004", "SMITH, JOHN",          "DEM", "2024", "NJ", "H",
         "02", "C", "C", "C00500004",
         "10 OAK ST",  "", "MARLTON",   "NJ", "08053"],
        ["H0NJ00005", "SMITH, JOHN",          "REP", "2024", "NJ", "H",
         "02", "C", "C", "C00500005",
         "12 OAK ST",  "", "MARLTON",   "NJ", "08053"],
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


def _synth_cm_zip(tmp_path: Path) -> Path:
    rows = [
        ["C00500001", "BOOKER FOR SENATE",      "WHITE, ELIZABETH",
         "PO BOX 32157", "", "NEWARK", "NJ", "07102",
         "P", "S", "DEM", "Q", "", "", "S0NJ00001"],
        ["C00500010", "FRIENDS OF JOHN",        "DOE, JOHN",
         "PO BOX 99", "", "TRENTON", "NJ", "12345",
         "U", "N", "DEM", "Q", "", "", ""],
        ["C00500011", "JOHN VICTORY FUND",      "DOE, JOHN",
         "PO BOX 99", "", "TRENTON", "NJ", "12345",
         "U", "N", "DEM", "Q", "", "", ""],
        ["C00500012", "JOHN LEADERSHIP PAC",    "DOE, JOHN",
         "PO BOX 99", "", "TRENTON", "NJ", "12345",
         "D", "N", "DEM", "Q", "", "", ""],
        ["C00500013", "FRIENDS OF JOHN",        "ROE, RICHARD",
         "5 MAIN ST", "", "PRINCETON", "NJ", "08540",
         "U", "N", "DEM", "Q", "", "", ""],
        ["C00500004", "SMITH FOR HOUSE",        "SMITH, JANE",
         "10 OAK ST", "", "MARLTON", "NJ", "08053",
         "P", "H", "DEM", "Q", "", "", "H0NJ00004"],
        ["C00500014", "JOHN SMITH HOUSE 2024",  "SMITH, JANE",
         "10 OAK ST", "", "MARLTON", "NJ", "08053",
         "P", "H", "DEM", "Q", "", "", "H0NJ00004"],
        ["C00500006", "ADAMS FOR SENATE",       "ADAMS, JANE",
         "1 ELM ST", "", "MONTCLAIR", "NJ", "07042",
         "P", "S", "IND", "Q", "", "", "S0NJ00006"],
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
        path=zip_path, source_url=f"file://{zip_path}",
        source_sha256="0" * 64, source_vintage=f"test-{cycle}",
        n_bytes=zip_path.stat().st_size, cache_hit=False,
    )


def _load_synthetic_and_run_dispatcher(
    conn: psycopg.Connection, tmp_path: Path,
) -> None:
    z_cn = _synth_cn_zip(tmp_path)
    z_cm = _synth_cm_zip(tmp_path)
    parse_cn = parse_fec_small_table(_fetch_for(z_cn, cycle="2024"),
                                     cycle="2024", file_kind="cn")
    parse_cm = parse_fec_small_table(_fetch_for(z_cm, cycle="2024"),
                                     cycle="2024", file_kind="cm")
    load_fec_small_table(parse_cn, conn)
    load_fec_small_table(parse_cm, conn)
    # Materialize L1 from the structural metric views.
    with conn.cursor() as cur:
        cur.execute("SELECT derived.refresh_all_fraud_signal_observations(%s)", ["2024"])
    conn.commit()


@pytest.fixture
def serving_client(
    fec_db: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    """A FastAPI TestClient backed by the fec_db connection.

    Same monkeypatch dance as test_serving_fec_metrics: substitute
    borrow_connection across every relevant route module so all
    queries hit the same in-test ephemeral connection.
    """
    _load_synthetic_and_run_dispatcher(fec_db, tmp_path)
    from contextlib import contextmanager

    from serving import db as serving_db

    @contextmanager
    def _borrow():  # type: ignore[no-untyped-def]
        yield fec_db

    monkeypatch.setattr(serving_db, "borrow_connection", _borrow)
    from serving.routes import fec as fec_route
    from serving.routes import fec_export as fec_export_route
    from serving.routes import fec_metrics as fec_metrics_route
    from serving.routes import fec_risk as fec_risk_route
    monkeypatch.setattr(fec_route,         "borrow_connection", _borrow)
    monkeypatch.setattr(fec_export_route,  "borrow_connection", _borrow)
    monkeypatch.setattr(fec_metrics_route, "borrow_connection", _borrow)
    monkeypatch.setattr(fec_risk_route,    "borrow_connection", _borrow)

    from serving.app import create_app
    return TestClient(create_app())


# --------------------------------------------------------------------------
# /fec/risk/entities -- queue
# --------------------------------------------------------------------------


def test_risk_queue_returns_entities_sorted_by_risk_desc(
    serving_client: TestClient,
) -> None:
    resp = serving_client.get("/fec/risk/entities", params={"cycle": "2024"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    RiskQueueResponse.model_validate(body)
    rows = body["rows"]
    assert body["total_count"] >= 1
    # DESC-sorted by risk_score (default)
    scores = [r["risk_score"] for r in rows]
    assert scores == sorted(scores, reverse=True)
    # Filters echo back what the request asked for
    assert body["filters"]["cycle"] == "2024"
    assert body["filters"]["sort_by"] == DEFAULT_SORT_BY
    assert body["filters"]["sort_dir"] == "DESC"


def test_risk_queue_filter_by_entity_kind(serving_client: TestClient) -> None:
    """Only treasurer entities come back when entity_kind=treasurer."""
    resp = serving_client.get(
        "/fec/risk/entities",
        params={"cycle": "2024", "entity_kind": "treasurer"},
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()["rows"]
    assert rows  # synthetic data has DOE, JOHN as a treasurer
    assert {r["entity_kind"] for r in rows} == {"treasurer"}
    # DOE, JOHN should be in the result -- 3 PCCs share them in synthetic data
    treasurer_ids = {r["entity_id"] for r in rows}
    assert "DOE, JOHN" in treasurer_ids


def test_risk_queue_filter_by_signal_id(serving_client: TestClient) -> None:
    """signal_id=committee_address_clusters -> only entities that fired
    the address-cluster signal are returned (i.e. 'address' entities)."""
    resp = serving_client.get(
        "/fec/risk/entities",
        params={"cycle": "2024", "signal_id": "committee_address_clusters"},
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()["rows"]
    assert rows
    for r in rows:
        assert "committee_address_clusters" in r["signals_fired"]


def test_risk_queue_filter_by_min_score(serving_client: TestClient) -> None:
    """min_score=0 returns everything; min_score=99.99 typically returns
    fewer rows. The exact threshold depends on the fixture, but the
    monotonicity invariant holds."""
    full = serving_client.get(
        "/fec/risk/entities", params={"cycle": "2024", "min_score": 0},
    ).json()
    high = serving_client.get(
        "/fec/risk/entities", params={"cycle": "2024", "min_score": 99.99},
    ).json()
    assert high["total_count"] <= full["total_count"]
    for r in high["rows"]:
        assert r["risk_score"] >= 99.99


def test_risk_queue_pagination_limit_one(serving_client: TestClient) -> None:
    resp = serving_client.get(
        "/fec/risk/entities", params={"cycle": "2024", "limit": 1, "offset": 0},
    )
    body = resp.json()
    assert len(body["rows"]) <= 1
    assert body["limit"]  == 1
    assert body["offset"] == 0


def test_risk_queue_bad_sort_returns_400(serving_client: TestClient) -> None:
    resp = serving_client.get(
        "/fec/risk/entities", params={"sort_by": "1; DROP TABLE foo"},
    )
    assert resp.status_code == 400
    msg = resp.json()["detail"]
    assert "not allowed" in msg
    assert "risk_score" in msg


def test_risk_queue_bad_entity_kind_returns_400(
    serving_client: TestClient,
) -> None:
    resp = serving_client.get(
        "/fec/risk/entities", params={"entity_kind": "politician"},
    )
    assert resp.status_code == 400
    assert "Unknown entity_kind" in resp.json()["detail"]


def test_risk_queue_inverted_score_range_returns_400(
    serving_client: TestClient,
) -> None:
    resp = serving_client.get(
        "/fec/risk/entities", params={"min_score": 80, "max_score": 50},
    )
    assert resp.status_code == 400
    assert "empty range" in resp.json()["detail"]


def test_risk_queue_score_out_of_range_returns_422_or_400(
    serving_client: TestClient,
) -> None:
    """FastAPI rejects min_score=200 at the Query(...) layer with 422
    before our handler runs. Either 400 or 422 is acceptable; what
    matters is that we never silently accept it."""
    resp = serving_client.get(
        "/fec/risk/entities", params={"min_score": 200},
    )
    assert resp.status_code in (400, 422)


# --------------------------------------------------------------------------
# /fec/risk/entities/{kind}/{id} -- evidence panel
# --------------------------------------------------------------------------


def test_risk_panel_doe_john_treasurer_decomposes_score(
    serving_client: TestClient,
) -> None:
    """DOE, JOHN is a treasurer for 3 committees in the synthetic
    fixture -> fires treasurer_concentration. Panel must:

    * return at least one observation
    * each observation has phi_contribution >= 0
    * if any signal exceeds the percentile floor, score_share_pct sums
      to 100 (within rounding); else sum is 0.
    """
    resp = serving_client.get(
        "/fec/risk/entities/treasurer/DOE, JOHN",
        params={"cycle": "2024"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    RiskEntityPanel.model_validate(body)
    assert body["entity_kind"] == "treasurer"
    assert body["entity_id"]   == "DOE, JOHN"
    assert body["cycle"]       == "2024"
    obs = body["observations"]
    assert obs
    sids = [o["signal_id"] for o in obs]
    assert "treasurer_concentration" in sids
    # Phi >= 0 always
    for o in obs:
        assert o["phi_contribution"] >= 0
    # Shares are sorted DESC
    shares = [o["score_share_pct"] for o in obs]
    assert shares == sorted(shares, reverse=True)
    if any(o["phi_contribution"] > 0 for o in obs):
        total_share = sum(o["score_share_pct"] for o in obs)
        assert math.isclose(total_share, 100.0, abs_tol=0.05)


def test_risk_panel_observation_severity_and_evidence_url_shapes(
    serving_client: TestClient,
) -> None:
    resp = serving_client.get("/fec/risk/entities/treasurer/DOE, JOHN")
    body = resp.json()
    obs = body["observations"]
    assert obs
    for o in obs:
        assert 1 <= o["severity"] <= 5
        assert 0.0 <= o["peer_percentile"] <= 1.0
        assert o["evidence_url"].startswith("/fec/metrics/")
        assert "?cycle=" in o["evidence_url"]


def test_risk_panel_unknown_entity_returns_404(serving_client: TestClient) -> None:
    resp = serving_client.get(
        "/fec/risk/entities/treasurer/THIS_TREASURER_DOES_NOT_EXIST",
    )
    assert resp.status_code == 404
    assert "No fired signals" in resp.json()["detail"]


def test_risk_panel_bad_entity_kind_returns_400(serving_client: TestClient) -> None:
    resp = serving_client.get("/fec/risk/entities/politician/whoever")
    assert resp.status_code == 400
    assert "Unknown entity_kind" in resp.json()["detail"]


def test_risk_panel_omitted_cycle_returns_latest(serving_client: TestClient) -> None:
    """When cycle is omitted, the panel returns the most recent
    observation set. Synthetic fixture only has 2024 loaded, so we get
    that cycle."""
    resp = serving_client.get("/fec/risk/entities/treasurer/DOE, JOHN")
    assert resp.status_code == 200
    assert resp.json()["cycle"] == "2024"


# --------------------------------------------------------------------------
# Evidence-trail CSV: unit (no DB) + live route
# --------------------------------------------------------------------------


def test_evidence_csv_columns_contract_is_stable() -> None:
    """The column list is the public CSV contract -- analysts pivot
    spreadsheets against it. Pin the order so a refactor that reorders
    columns trips this test."""
    assert EVIDENCE_CSV_COLUMNS == (
        "cycle", "entity_kind", "entity_id", "risk_score",
        "n_signals_fired", "max_severity", "max_peer_percentile",
        "avg_peer_percentile", "primary_peer_bucket", "last_observation_at",
        "signal_id", "severity", "peer_percentile", "peer_bucket",
        "raw_value", "phi_contribution", "score_share_pct", "evidence_url",
    )


def test_evidence_csv_rows_flattens_observations() -> None:
    """One CSV row per observation; entity columns repeated on every row."""
    import datetime as dt
    panel = {
        "cycle":               "2024",
        "entity_kind":         "treasurer",
        "entity_id":           "DOE, JOHN",
        "risk_score":          72.50,
        "n_signals_fired":     2,
        "max_severity":        3,
        "max_peer_percentile": 0.99,
        "avg_peer_percentile": 0.97,
        "primary_peer_bucket": "kind=treasurer",
        "last_observation_at": dt.datetime(2026, 5, 4, 12, 0, tzinfo=dt.UTC),
        "observations":        [
            {
                "signal_id":         "treasurer_concentration",
                "severity":          3,
                "peer_percentile":   0.99,
                "peer_bucket":       "kind=treasurer",
                "raw_value":         3.0,
                "evidence_url":      "/fec/metrics/treasurer_concentration?cycle=2024",
                "phi_contribution":  0.0048,
                "score_share_pct":   60.0,
            },
            {
                "signal_id":         "no_pcc",
                "severity":          4,
                "peer_percentile":   0.96,
                "peer_bucket":       "state=NJ",
                "raw_value":         None,
                "evidence_url":      "/fec/metrics/no_pcc?cycle=2024",
                "phi_contribution":  0.0032,
                "score_share_pct":   40.0,
            },
        ],
    }
    rows = evidence_csv_rows(panel)
    assert len(rows) == 2
    # First row: signal-level cols start at index 10 (after entity cols).
    assert rows[0][10] == "treasurer_concentration"
    assert rows[1][10] == "no_pcc"
    # Entity-level columns are repeated identically.
    assert rows[0][:10] == rows[1][:10]
    # raw_value None for binary signal -- preserved for downstream CSV
    # writer to render as empty string.
    assert rows[1][14] is None
    # last_observation_at is ISO-8601.
    assert rows[0][9].startswith("2026-05-04T12:00:00")


def test_evidence_csv_rows_empty_observations_yields_zero_rows() -> None:
    """A panel with no observations -- defensive: shouldn't happen via
    get_risk_entity (it returns None instead) but the helper should
    handle it gracefully."""
    import datetime as dt
    panel = {
        "cycle":               "2024",
        "entity_kind":         "treasurer",
        "entity_id":           "X",
        "risk_score":          0.0,
        "n_signals_fired":     0,
        "max_severity":        1,
        "max_peer_percentile": 0.0,
        "avg_peer_percentile": 0.0,
        "primary_peer_bucket": "",
        "last_observation_at": dt.datetime(2026, 5, 4, tzinfo=dt.UTC),
        "observations":        [],
    }
    assert evidence_csv_rows(panel) == []


def test_risk_panel_csv_streams_header_and_rows(
    serving_client: TestClient,
) -> None:
    """Live: GET /fec/risk/entities/treasurer/DOE, JOHN/csv must return
    CSV with the documented column header and one row per fired signal."""
    resp = serving_client.get(
        "/fec/risk/entities/treasurer/DOE, JOHN/csv?cycle=2024",
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    cd = resp.headers["content-disposition"]
    assert "attachment;" in cd
    assert "risk_treasurer_DOE__JOHN_2024.csv" in cd
    body = resp.text
    lines = [ln for ln in body.splitlines() if ln]
    # Header: comma-joined column list.
    assert lines[0] == ",".join(EVIDENCE_CSV_COLUMNS)
    # At least one observation row.
    assert len(lines) >= 2
    # treasurer_concentration appears in at least one data row.
    assert any("treasurer_concentration" in ln for ln in lines[1:])
    # entity_id with embedded comma is quoted by csv.QUOTE_MINIMAL.
    assert any('"DOE, JOHN"' in ln for ln in lines[1:])


def test_risk_panel_csv_404_for_unknown_entity(
    serving_client: TestClient,
) -> None:
    resp = serving_client.get(
        "/fec/risk/entities/treasurer/THIS_TREASURER_DOES_NOT_EXIST/csv",
    )
    assert resp.status_code == 404
    assert "No fired signals" in resp.json()["detail"]


def test_risk_panel_csv_400_for_unknown_kind(
    serving_client: TestClient,
) -> None:
    resp = serving_client.get("/fec/risk/entities/politician/whoever/csv")
    assert resp.status_code == 400
    assert "Unknown entity_kind" in resp.json()["detail"]
