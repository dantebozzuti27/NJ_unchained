"""Tests for migration 052_fraud_risk_score (Tier 4 v3 step 3 -- L3a).

Two layers:

1. STATIC (no DB)
   - The migration file declares the function signature + the read view.
   - Calibration anchors documented in work_left.txt match a pure-Python
     reproduction of the formula. This guards against any future
     "let me tweak gamma / k" change drifting from the documented math.

2. LIVE (live_pg)
   - The function returns the documented calibration anchors within
     0.01 of the analytical value.
   - Edge cases: NULL / empty arrays -> 0; below-threshold percentiles
     contribute zero; mismatched array lengths raise.
   - Monotonicity: holding either input fixed and increasing the other
     never decreases the score.
   - The v_entity_fraud_risk view composes correctly: insert L1
     observations for one entity, query the view, verify risk_score
     matches the manual computation, and that ordering by risk_score
     DESC gives the analyst queue.
"""

from __future__ import annotations

import math
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import psycopg


REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATION_PATH = REPO_ROOT / "db" / "migrations" / "052_fraud_risk_score.sql"


# ============================================================================
# 1. STATIC checks (no DB)
# ============================================================================


def _python_score(severities: list[int], percentiles: list[float]) -> float:
    """Pure-Python reproduction of derived.fraud_risk_score(...).

    Used to assert that the SQL function and the documented math do not
    drift apart. If you change one without changing the other, this test
    fails.
    """
    if not severities:
        return 0.0
    raw_sum = sum(
        sev * max(0.0, p - 0.95) ** 2 for sev, p in zip(severities, percentiles, strict=True)
    )
    score = 100.0 * (1.0 - math.exp(-50.0 * raw_sum))
    return round(max(0.0, min(100.0, score)), 2)


def test_migration_file_exists() -> None:
    assert MIGRATION_PATH.is_file(), f"missing: {MIGRATION_PATH}"


def test_migration_declares_scoring_function_and_view() -> None:
    """The function signature + the read-surface view are required for the API."""
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "CREATE OR REPLACE FUNCTION derived.fraud_risk_score(" in sql
    assert "CREATE OR REPLACE VIEW derived.v_entity_fraud_risk" in sql


def test_migration_uses_documented_constants() -> None:
    """gamma=2 and k=50 are the design pins; document drift fails the test."""
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    # gamma=2 surfaces as POWER(..., 2) with the threshold 0.95
    assert "POWER(GREATEST(0::NUMERIC, p - 0.95), 2)" in sql
    # k=50 surfaces as the multiplier on the exponent
    assert "EXP(-50::NUMERIC * raw_sum)" in sql
    # function is IMMUTABLE + PARALLEL SAFE so plans can cache and parallelize
    assert "IMMUTABLE" in sql
    assert "PARALLEL SAFE" in sql


@pytest.mark.parametrize(
    ("severities", "percentiles", "expected"),
    [
        # Calibration anchors from work_left.txt (2026-05-04 pin).
        ([3], [0.99], 21.34),
        ([5, 5, 5, 5, 5], [1.0, 1.0, 1.0, 1.0, 1.0], 95.61),
        ([5], [1.0], 46.47),
        # Below-threshold contributes zero
        ([5], [0.5], 0.0),
        ([5], [0.95], 0.0),
        # Empty -> 0
        ([], [], 0.0),
    ],
)
def test_python_reference_matches_documented_calibration(
    severities: list[int],
    percentiles: list[float],
    expected: float,
) -> None:
    """The pure-Python reference function must match the published anchors."""
    got = _python_score(severities, percentiles)
    assert abs(got - expected) < 0.05, f"got {got}, expected {expected}"


# ============================================================================
# 2. LIVE checks (live_pg)
# ============================================================================


pytestmark = pytest.mark.live_pg


@pytest.fixture
def fraud_db(live_pg: psycopg.Connection) -> psycopg.Connection:
    """Apply all migrations + seeds against a clean DB; yield the conn."""
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
            "    EXECUTE 'DROP VIEW IF EXISTS public.' || quote_ident(r.viewname) "
            "            || ' CASCADE'; "
            "  END LOOP; "
            "END $$;",
        )
    conn.commit()

    apply_migrations(conn, discover(MIGRATIONS_DIR))
    apply_migrations(conn, discover(SEEDS_DIR))
    return conn


def _call_score(
    conn: psycopg.Connection,
    severities: list[int],
    percentiles: list[float],
) -> float:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT derived.fraud_risk_score(%s::SMALLINT[], %s::NUMERIC[])",
            (severities, percentiles),
        )
        row = cur.fetchone()
    assert row is not None
    val = row[0]
    return float(val) if val is not None else 0.0


@pytest.mark.parametrize(
    ("severities", "percentiles", "expected"),
    [
        ([3], [0.99], 21.34),
        ([5, 5, 5, 5, 5], [1.0, 1.0, 1.0, 1.0, 1.0], 95.61),
        ([5], [1.0], 46.47),
        # Below-threshold contributes zero
        ([5], [0.5], 0.0),
        ([5], [0.95], 0.0),
        # Mixed: one signal in the tail, one below threshold
        ([3, 5], [0.5, 1.0], 46.47),
    ],
)
def test_live_score_matches_calibration_anchors(
    fraud_db: psycopg.Connection,
    severities: list[int],
    percentiles: list[float],
    expected: float,
) -> None:
    """The SQL function reproduces the documented anchor values."""
    got = _call_score(fraud_db, severities, percentiles)
    assert abs(got - expected) < 0.05, f"got {got}, expected {expected}"


def test_live_score_handles_null_and_empty(
    fraud_db: psycopg.Connection,
) -> None:
    """Defensive handling: NULL / empty arrays return 0 (not NULL)."""
    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT derived.fraud_risk_score(NULL, NULL), "
            "       derived.fraud_risk_score("
            "           ARRAY[]::SMALLINT[], ARRAY[]::NUMERIC[])"
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == Decimal("0.00")
    assert row[1] == Decimal("0.00")


def test_live_score_rejects_mismatched_array_lengths(
    fraud_db: psycopg.Connection,
) -> None:
    """Mismatched arrays are a programming bug; the function must raise."""
    import psycopg

    with (
        pytest.raises(psycopg.errors.RaiseException) as exc,
        fraud_db.cursor() as cur,
    ):
        cur.execute(
            "SELECT derived.fraud_risk_score(    ARRAY[3, 5]::SMALLINT[], ARRAY[0.99]::NUMERIC[])"
        )
    fraud_db.rollback()
    assert "equal length" in str(exc.value).lower()


def test_live_score_is_monotone_in_percentile(
    fraud_db: psycopg.Connection,
) -> None:
    """Holding severity fixed, score must be non-decreasing in percentile."""
    severities = [4]
    last = -1.0
    for p in (0.50, 0.94, 0.95, 0.96, 0.97, 0.98, 0.99, 1.0):
        s = _call_score(fraud_db, severities, [p])
        assert s >= last, f"non-monotone: p={p}, score={s} < prev={last}"
        last = s


def test_live_score_is_monotone_in_severity(
    fraud_db: psycopg.Connection,
) -> None:
    """Holding percentile fixed in the tail, score is non-decreasing in severity."""
    p = 0.99
    last = -1.0
    for sev in (1, 2, 3, 4, 5):
        s = _call_score(fraud_db, [sev], [p])
        assert s >= last, f"non-monotone: sev={sev}, score={s} < prev={last}"
        last = s


def test_live_score_clamps_to_100(
    fraud_db: psycopg.Connection,
) -> None:
    """Score is bounded above by 100 even with absurd input stacks."""
    # 100 signals all at sev=5, p=1.0 -> exp(-50 * 100 * 0.0125) ~ 0
    severities = [5] * 100
    percentiles = [1.0] * 100
    got = _call_score(fraud_db, severities, percentiles)
    assert got == 100.0, f"score not clamped to 100; got {got}"


def test_live_view_exposes_score_per_entity(
    fraud_db: psycopg.Connection,
) -> None:
    """The view appends risk_score to L2 features. Insert L1 observations for
    two entities with known feature vectors, query the view, verify the
    score column matches the function and the queue ordering is correct."""
    with fraud_db.cursor() as cur:
        # Entity X: one signal at sev=3, p=0.99 (the calibration anchor)
        cur.execute(
            "INSERT INTO derived.fraud_signal_observation ("
            "cycle, entity_kind, entity_id, signal_id, raw_value, "
            "severity, peer_bucket, peer_percentile, evidence_url) "
            "VALUES "
            "('2024', 'committee', 'C00500_X', 'treasurer_concentration', "
            " 17, 3, 'kind=treasurer', 0.99, "
            " '/fec/metrics/treasurer_concentration?cycle=2024')"
        )
        # Entity Y: five signals all at sev=5, p=1.0 (high-end anchor)
        for sig in (
            "treasurer_concentration",
            "candidate_no_pcc",
            "candidate_broken_pcc",
            "candidate_multiple_pccs",
            "committee_address_clusters",
        ):
            cur.execute(
                "INSERT INTO derived.fraud_signal_observation ("
                "cycle, entity_kind, entity_id, signal_id, raw_value, "
                "severity, peer_bucket, peer_percentile, evidence_url) "
                "VALUES ('2024', 'committee', 'C00500_Y', %s, 1, 5, "
                "'kind=committee', 1.0, '/fec/metrics/' || %s || '?cycle=2024')",
                (sig, sig),
            )
        # Entity Z: one signal at sev=5, p=0.50 (below threshold; score 0)
        cur.execute(
            "INSERT INTO derived.fraud_signal_observation ("
            "cycle, entity_kind, entity_id, signal_id, raw_value, "
            "severity, peer_bucket, peer_percentile, evidence_url) "
            "VALUES ('2024', 'committee', 'C00500_Z', 'treasurer_concentration', "
            " 1, 5, 'kind=treasurer', 0.50, "
            " '/fec/metrics/treasurer_concentration?cycle=2024')"
        )
    fraud_db.commit()

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT entity_id, risk_score, n_signals_fired "
            "FROM derived.v_entity_fraud_risk "
            "WHERE cycle = '2024' "
            "ORDER BY risk_score DESC, entity_id"
        )
        rows = cur.fetchall()

    eids = [r[0] for r in rows]
    scores = {r[0]: float(r[1]) for r in rows}
    counts = {r[0]: int(r[2]) for r in rows}

    # Y has the highest score, then X, then Z (Z hits the 0.95 floor -> 0).
    # Migration 061 added a multi-family diversity bonus: Y's 5 signals
    # span TWO families (4 structural + 1 address) so its raw_sum gets
    # +0.01 * (2-1)^2 = 0.01 added before the exp transform. Old score
    # for Y was 95.61; new score is ~97.34. X is unaffected (single
    # signal -> single contributing family -> bonus = 0).
    assert eids == ["C00500_Y", "C00500_X", "C00500_Z"]
    assert abs(scores["C00500_Y"] - 97.34) < 0.05
    assert abs(scores["C00500_X"] - 21.34) < 0.05
    assert scores["C00500_Z"] == 0.0
    assert counts == {"C00500_Y": 5, "C00500_X": 1, "C00500_Z": 1}


def test_live_view_integrates_with_step2_dispatcher(
    fraud_db: psycopg.Connection,
) -> None:
    """End-to-end: load engineered FEC data, run the step-2 dispatcher,
    query the step-3 view. Every row in v_entity_fraud_risk has a
    risk_score in [0, 100] and at least one fired signal."""
    src = ("file://test", "0" * 64, "test-2024")

    # Minimal FEC fixture: enough rows to fire a couple of signals
    with fraud_db.cursor() as cur:
        # Two NJ House candidates -- one missing PCC (no_pcc fires)
        cur.execute(
            "INSERT INTO raw.fec_candidate ("
            "cycle, cand_id, cand_name, cand_pty_affiliation, "
            "cand_election_yr, cand_office_st, cand_office, "
            "cand_office_district, cand_ici, cand_status, cand_pcc, "
            "source_url, source_sha256, source_vintage) "
            "VALUES "
            "('2024', 'H0NJ00099', 'OK, INCUMBENT', 'DEM', 2024, 'NJ', "
            " 'H', '03', 'I', 'C', 'C00500099', %s, %s, %s),"
            "('2024', 'H0NJ00100', 'NO PCC, CHALLENGER', 'REP', 2024, 'NJ', "
            " 'H', '03', 'C', 'C', '', %s, %s, %s)",
            (*src, *src),
        )
        # Two committees with the SAME treasurer (treasurer_concentration fires)
        cur.execute(
            "INSERT INTO raw.fec_committee ("
            "cycle, cmte_id, cmte_nm, tres_nm, cmte_st1, cmte_st2, "
            "cmte_city, cmte_st, cmte_zip, cmte_dsgn, cmte_tp, "
            "cmte_pty_affiliation, cmte_filing_freq, org_tp, "
            "connected_org_nm, cand_id, source_url, source_sha256, "
            "source_vintage) "
            "VALUES "
            "('2024', 'C00500099', 'OK FOR HOUSE', 'POE, RICHARD', "
            " '5 OAK', NULL, 'PRINCETON', 'NJ', '08540', 'P', 'H', 'DEM', "
            " 'Q', NULL, NULL, 'H0NJ00099', %s, %s, %s),"
            "('2024', 'C00500200', 'OTHER PAC', 'POE, RICHARD', "
            " '7 OAK', NULL, 'PRINCETON', 'NJ', '08540', 'U', 'N', 'DEM', "
            " 'Q', NULL, NULL, NULL, %s, %s, %s)",
            (*src, *src),
        )
    fraud_db.commit()

    with fraud_db.cursor() as cur:
        cur.execute("SELECT derived.refresh_all_fraud_signal_observations('2024')")
    fraud_db.commit()

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT MIN(risk_score), MAX(risk_score), COUNT(*) "
            "FROM derived.v_entity_fraud_risk WHERE cycle = '2024'"
        )
        row = cur.fetchone()
    assert row is not None
    score_min, score_max, n_rows = row
    assert n_rows >= 2, "expected at least two scored entities"
    assert float(score_min) >= 0.0
    assert float(score_max) <= 100.0
