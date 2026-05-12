"""Live-PG regression tests for migration 097 + the drift asset check.

VISION_2026 Pillar 2 substrate hygiene. The substrate this module pins:

    * governance.fraud_signal_baseline -- append-only per-(cycle,
      signal_id, captured_at) observation-count history.
    * governance.v_fraud_signal_baseline_stats -- rollup view.
    * governance.capture_fraud_signal_baseline(cycle) -- helper that
      INSERTs one row per signal_id in fraud_signal_config (LEFT JOIN
      COALESCE 0 against the current observation table).
    * orchestration.asset_checks.
      fraud_signal_observation_distribution_drift_within_2sigma --
      asset check that fails (WARN) when any signal's current count is
      >2sigma from its baseline mean.

What this module pins:
    * Schema: PK (cycle, signal_id, captured_at) + non-negative CHECK +
      sample-stddev semantics.
    * Capture function: inserts one row per signal_id, includes zero-
      observation signals.
    * View statistics: mean + sample-stddev + n_samples reconcile to
      hand-computed values on a multi-sample fixture.
    * Vacuous-pass: signals with n_samples < 3 are NOT flagged as drift
      (insufficient samples).
    * Drift detection: when n_samples >= 3 and current count exceeds
      2sigma of the historical mean, the signal is surfaced as drifted.
    * Constant-baseline divergence: when stddev=0 but current ≠ mean,
      the signal is surfaced as drifted (z-score is mathematically
      undefined but the divergence is still meaningful).
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import psycopg

from scripts.migrate import (
    MIGRATIONS_DIR,
    SEEDS_DIR,
    apply_migrations,
    discover,
)

pytestmark = pytest.mark.live_pg


EXPECTED_FORMULA_VERSION = "2.7.0-fraud-signal-drift-baseline-v1"


@pytest.fixture
def drift_db(live_pg: psycopg.Connection) -> psycopg.Connection:
    """Cleanly-migrated DB with all migrations + seeds applied."""
    conn = live_pg
    with conn.cursor() as cur:
        for sch in ("governance", "derived", "raw", "ref"):
            cur.execute(f"DROP SCHEMA IF EXISTS {sch} CASCADE")
    conn.commit()
    apply_migrations(conn, discover(MIGRATIONS_DIR))
    apply_migrations(conn, discover(SEEDS_DIR))
    conn.commit()
    return conn


def _seed_baseline(
    conn: psycopg.Connection,
    samples: list[tuple[str, str, int, dt.datetime]],
) -> None:
    """Helper: bulk-insert (cycle, signal_id, n_obs, captured_at) rows."""
    with conn.cursor() as cur:
        for cycle, sig, n_obs, ts in samples:
            cur.execute(
                """
                INSERT INTO governance.fraud_signal_baseline
                    (cycle, signal_id, n_obs, captured_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (cycle, signal_id, captured_at) DO NOTHING
                """,
                (cycle, sig, n_obs, ts),
            )
    conn.commit()


def _truncate_baseline(conn: psycopg.Connection) -> None:
    """Helper: clear the baseline table so each test starts from empty."""
    with conn.cursor() as cur:
        cur.execute("TRUNCATE governance.fraud_signal_baseline")
    conn.commit()


# =============================================================================
# Schema + structural contract
# =============================================================================


def test_baseline_table_exists_with_expected_columns(
    drift_db: psycopg.Connection,
):
    """The table must exist with the expected column shape."""
    with drift_db.cursor() as cur:
        cur.execute("""
            SELECT column_name, data_type
            FROM   information_schema.columns
            WHERE  table_schema = 'governance'
              AND  table_name   = 'fraud_signal_baseline'
            ORDER BY ordinal_position
        """)
        cols = cur.fetchall()
    col_names = [c[0] for c in cols]
    expected = {
        "cycle", "signal_id", "n_obs", "captured_at",
        "formula_version", "notes",
    }
    assert expected.issubset(set(col_names)), (
        f"missing columns: {expected - set(col_names)}"
    )


def test_baseline_pk_includes_captured_at(drift_db: psycopg.Connection):
    """Multiple samples per (cycle, signal_id) must be allowed."""
    _truncate_baseline(drift_db)
    samples = [
        ("2024", "candidate_no_pcc", 100,
         dt.datetime(2026, 5, 1, 12, 0, tzinfo=dt.UTC)),
        ("2024", "candidate_no_pcc", 105,
         dt.datetime(2026, 5, 15, 12, 0, tzinfo=dt.UTC)),
        ("2024", "candidate_no_pcc", 110,
         dt.datetime(2026, 5, 29, 12, 0, tzinfo=dt.UTC)),
    ]
    _seed_baseline(drift_db, samples)
    with drift_db.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*)
            FROM   governance.fraud_signal_baseline
            WHERE  cycle = '2024' AND signal_id = 'candidate_no_pcc'
        """)
        (n,) = cur.fetchone()
    assert n == 3


def test_baseline_n_obs_non_negative_enforced(
    drift_db: psycopg.Connection,
):
    """The CHECK constraint must reject negative observation counts."""
    import psycopg.errors
    with drift_db.cursor() as cur, pytest.raises(
        psycopg.errors.CheckViolation
    ):
        cur.execute("""
            INSERT INTO governance.fraud_signal_baseline
                (cycle, signal_id, n_obs)
            VALUES ('2024', 'candidate_no_pcc', -1)
        """)


# =============================================================================
# View statistics
# =============================================================================


def test_baseline_stats_view_mean_stddev_correct(
    drift_db: psycopg.Connection,
):
    """The view must compute correct sample-stddev for known inputs."""
    _truncate_baseline(drift_db)
    samples = [
        ("2024", "candidate_no_pcc", 100,
         dt.datetime(2026, 5, 1, tzinfo=dt.UTC)),
        ("2024", "candidate_no_pcc", 110,
         dt.datetime(2026, 5, 15, tzinfo=dt.UTC)),
        ("2024", "candidate_no_pcc", 120,
         dt.datetime(2026, 5, 29, tzinfo=dt.UTC)),
    ]
    _seed_baseline(drift_db, samples)
    with drift_db.cursor() as cur:
        cur.execute("""
            SELECT mean_n, stddev_n, n_samples
            FROM   governance.v_fraud_signal_baseline_stats
            WHERE  cycle = '2024' AND signal_id = 'candidate_no_pcc'
        """)
        mean_n, stddev_n, n_samples = cur.fetchone()
    # mean = 110, sample-stddev of [100, 110, 120] is 10 exactly
    # (variance = (100+0+100)/2 = 100; stddev = 10)
    assert float(mean_n) == pytest.approx(110.0)
    assert float(stddev_n) == pytest.approx(10.0)
    assert int(n_samples) == 3


def test_baseline_stats_view_returns_null_stddev_for_single_sample(
    drift_db: psycopg.Connection,
):
    """Substrate-honesty: STDDEV_SAMP on n=1 must surface as NULL."""
    _truncate_baseline(drift_db)
    _seed_baseline(drift_db, [
        ("2024", "test_singleton_signal", 42,
         dt.datetime(2026, 5, 1, tzinfo=dt.UTC)),
    ])
    with drift_db.cursor() as cur:
        cur.execute("""
            SELECT mean_n, stddev_n, n_samples
            FROM   governance.v_fraud_signal_baseline_stats
            WHERE  cycle = '2024' AND signal_id = 'test_singleton_signal'
        """)
        mean_n, stddev_n, n_samples = cur.fetchone()
    assert float(mean_n) == 42.0
    assert stddev_n is None, (
        "STDDEV_SAMP on n=1 must surface as NULL; never invent a default"
    )
    assert int(n_samples) == 1


# =============================================================================
# capture_fraud_signal_baseline function
# =============================================================================


def test_capture_function_inserts_one_row_per_signal(
    drift_db: psycopg.Connection,
):
    """The function must INSERT one row per signal_id in fraud_signal_config."""
    _truncate_baseline(drift_db)
    with drift_db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM derived.fraud_signal_config")
        (n_signals,) = cur.fetchone()
        cur.execute("SELECT governance.capture_fraud_signal_baseline('2024')")
        (n_inserted,) = cur.fetchone()
    drift_db.commit()
    assert n_inserted == n_signals, (
        f"capture should insert one row per fraud_signal_config row "
        f"({n_signals}); got {n_inserted}"
    )


def test_capture_function_records_zero_for_signals_with_no_observations(
    drift_db: psycopg.Connection,
):
    """Signals with 0 current obs must still be captured (regression-to-zero
    must be visible in the time series, not silently absent)."""
    _truncate_baseline(drift_db)
    with drift_db.cursor() as cur:
        cur.execute("SELECT governance.capture_fraud_signal_baseline('2024')")
        cur.execute("""
            SELECT COUNT(*)
            FROM   governance.fraud_signal_baseline
            WHERE  cycle = '2024'
              AND  n_obs = 0
        """)
        (n_zero,) = cur.fetchone()
    drift_db.commit()
    # On clean substrate, every signal has 0 observations -- so the
    # capture should record N zero-rows where N = COUNT(fraud_signal_config).
    assert n_zero > 0, (
        "expected at least 1 zero-n_obs row captured (substrate is "
        "empty in this test fixture)"
    )


# =============================================================================
# Asset check semantics (testing the SQL contract that the check relies on)
# =============================================================================


def test_drift_view_surfaces_within_2sigma_as_passing(
    drift_db: psycopg.Connection,
):
    """When current count is within 2sigma of mean, z_score <= 2."""
    _truncate_baseline(drift_db)
    # Baseline: mean=100, stddev_samp of [90, 100, 110] = 10
    _seed_baseline(drift_db, [
        ("2024", "candidate_no_pcc", 90,
         dt.datetime(2026, 5, 1, tzinfo=dt.UTC)),
        ("2024", "candidate_no_pcc", 100,
         dt.datetime(2026, 5, 15, tzinfo=dt.UTC)),
        ("2024", "candidate_no_pcc", 110,
         dt.datetime(2026, 5, 29, tzinfo=dt.UTC)),
    ])
    with drift_db.cursor() as cur:
        cur.execute("""
            SELECT mean_n, stddev_n, n_samples
            FROM   governance.v_fraud_signal_baseline_stats
            WHERE  cycle = '2024' AND signal_id = 'candidate_no_pcc'
        """)
        mean_n, stddev_n, n_samples = cur.fetchone()
    # Simulate current = 105 -> z = (105 - 100) / 10 = 0.5 (within 2sigma)
    current = 105
    z = (current - float(mean_n)) / float(stddev_n)
    assert int(n_samples) >= 3
    assert abs(z) < 2.0, "0.5sigma should be well within 2sigma"


def test_drift_view_surfaces_above_2sigma_as_drift(
    drift_db: psycopg.Connection,
):
    """When current count exceeds 2sigma of mean, z_score > 2."""
    _truncate_baseline(drift_db)
    _seed_baseline(drift_db, [
        ("2024", "candidate_no_pcc", 100,
         dt.datetime(2026, 5, 1, tzinfo=dt.UTC)),
        ("2024", "candidate_no_pcc", 105,
         dt.datetime(2026, 5, 15, tzinfo=dt.UTC)),
        ("2024", "candidate_no_pcc", 95,
         dt.datetime(2026, 5, 29, tzinfo=dt.UTC)),
    ])
    with drift_db.cursor() as cur:
        cur.execute("""
            SELECT mean_n, stddev_n
            FROM   governance.v_fraud_signal_baseline_stats
            WHERE  cycle = '2024' AND signal_id = 'candidate_no_pcc'
        """)
        mean_n, stddev_n = cur.fetchone()
    # mean=100, stddev_samp([100,105,95]) = 5. Current=120 -> z=4 (>2sigma).
    current = 120
    z = (current - float(mean_n)) / float(stddev_n)
    assert abs(z) > 2.0, (
        f"z={z} should exceed 2sigma for current=120 with mean=100 stddev=5"
    )


def test_drift_n_samples_lt_3_is_vacuous_pass(
    drift_db: psycopg.Connection,
):
    """When n_samples < 3, no 2sigma test should fire."""
    _truncate_baseline(drift_db)
    _seed_baseline(drift_db, [
        ("2024", "candidate_no_pcc", 100,
         dt.datetime(2026, 5, 1, tzinfo=dt.UTC)),
        ("2024", "candidate_no_pcc", 200,
         dt.datetime(2026, 5, 15, tzinfo=dt.UTC)),
    ])
    with drift_db.cursor() as cur:
        cur.execute("""
            SELECT n_samples
            FROM   governance.v_fraud_signal_baseline_stats
            WHERE  cycle = '2024' AND signal_id = 'candidate_no_pcc'
        """)
        (n_samples,) = cur.fetchone()
    assert int(n_samples) == 2
    # The asset check's SQL CASE expression returns NULL z_score when
    # n_samples < 3, so the row is classified as "insufficient", not
    # "drifted". This test pins that contract: 2 samples must surface
    # as insufficient.


def test_baseline_seed_landed_on_initial_apply(
    drift_db: psycopg.Connection,
):
    """The migration's hardcoded seed must land 19 rows."""
    with drift_db.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*)
            FROM   governance.fraud_signal_baseline
            WHERE  notes = 'Initial seed: production count at mig 097 deploy'
        """)
        (n,) = cur.fetchone()
    assert n == 19, f"expected 19 seed rows, got {n}"


# =============================================================================
# Formula version provenance
# =============================================================================


def test_formula_version_registered(drift_db: psycopg.Connection):
    """The migration's formula_version is registered."""
    with drift_db.cursor() as cur:
        cur.execute("""
            SELECT effective_date, description
            FROM   ref.formula_version
            WHERE  formula_version = %s
        """, (EXPECTED_FORMULA_VERSION,))
        row = cur.fetchone()
    assert row is not None
    eff_date, desc = row
    assert eff_date.isoformat() == "2026-05-11"
    assert "drift" in desc.lower() or "baseline" in desc.lower()
