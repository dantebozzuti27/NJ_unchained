"""Live-PG regression tests for migration 095.

VISION_2026 Pillar 2 substrate hygiene. The substrate this module pins:

    * ref.f_signal_severity(p_signal_id) -- scalar lookup, RAISES on
      missing.
    * derived.v_entity_fraud_features -- L2 view, severity now sourced
      from calibration via COALESCE(sc.severity_level, o.severity).
    * derived.v_entity_fraud_evidence -- L3 evidence panel view,
      severity sourced identically.
    * derived.audit_severity_drift(p_cycle) -- audit function.

What this module pins:
    * Single-source-of-truth contract: L2/L3 view severities reflect
      ref.fraud_signal_severity_calibration values, NOT the
      base-table severity column. Mutating the calibration table value
      changes the view value; mutating the base column value does NOT
      (so long as a calibration row exists).
    * COALESCE fallback: if the calibration table has no row for a
      signal_id, the L2/L3 views fall back to the base-column severity
      so a new-signal rollout does not blank out the UI mid-transition.
    * f_signal_severity RAISES on missing signal_id (substrate-honest:
      never silently invents a default).
    * audit_severity_drift correctly surfaces drift when present and
      returns zero rows when no drift.
    * Formula version 2.6.0-severity-dependency-inversion-v1 registered.
"""

from __future__ import annotations

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


EXPECTED_FORMULA_VERSION = "2.6.0-severity-dependency-inversion-v1"


@pytest.fixture
def sev_db(live_pg: psycopg.Connection) -> psycopg.Connection:
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


def _insert_obs(
    conn: psycopg.Connection,
    *,
    cycle: str = "2024",
    entity_kind: str = "candidate",
    entity_id: str = "H4NJ09999",
    signal_id: str = "candidate_no_pcc",
    severity: int = 1,
    peer_percentile: float = 0.99,
    peer_bucket: str = "kind=candidate",
    raw_value: float = 1.0,
    evidence_url: str = "/test/evidence",
) -> None:
    """Helper to seed a single fraud_signal_observation row."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO derived.fraud_signal_observation (
                cycle, entity_kind, entity_id, signal_id,
                raw_value, severity, peer_bucket, peer_percentile,
                evidence_url
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                cycle, entity_kind, entity_id, signal_id,
                raw_value, severity, peer_bucket, peer_percentile,
                evidence_url,
            ),
        )
    conn.commit()


# =============================================================================
# ref.f_signal_severity
# =============================================================================


def test_f_signal_severity_returns_calibration_value(
    sev_db: psycopg.Connection,
):
    """For every seeded signal, the function returns the calibration value."""
    with sev_db.cursor() as cur:
        cur.execute("""
            SELECT signal_id, severity_level
            FROM   ref.fraud_signal_severity_calibration
            ORDER BY signal_id
        """)
        rows = cur.fetchall()
    assert len(rows) >= 17, (
        f"expected >= 17 calibration rows (seed 019 has 17, mig 092 may "
        f"add strict_address), got {len(rows)}"
    )
    for signal_id, expected_sev in rows:
        with sev_db.cursor() as cur:
            cur.execute(
                "SELECT ref.f_signal_severity(%s)", (signal_id,)
            )
            (got,) = cur.fetchone()
        assert got == expected_sev, (
            f"f_signal_severity({signal_id!r}) returned {got}, "
            f"calibration says {expected_sev}"
        )


def test_f_signal_severity_raises_on_unknown(sev_db: psycopg.Connection):
    """An unknown signal_id MUST raise no_data_found, not return NULL."""
    import psycopg.errors
    with sev_db.cursor() as cur, pytest.raises(
        psycopg.errors.NoDataFound
    ):
        cur.execute(
            "SELECT ref.f_signal_severity(%s)",
            ("nonexistent_signal_xyz",),
        )


def test_f_signal_severity_is_stable(sev_db: psycopg.Connection):
    """Function MUST be marked STABLE so the planner can optimize."""
    with sev_db.cursor() as cur:
        cur.execute("""
            SELECT provolatile
            FROM   pg_proc p
            JOIN   pg_namespace n ON n.oid = p.pronamespace
            WHERE  n.nspname = 'ref'
              AND  p.proname = 'f_signal_severity'
        """)
        (volatility,) = cur.fetchone()
    assert volatility == 's', (
        f"f_signal_severity volatility should be 's' (STABLE), "
        f"got {volatility!r}"
    )


# =============================================================================
# Single-source-of-truth contract (the migration's core claim)
# =============================================================================


def test_l2_severities_reflect_calibration_not_base_column(
    sev_db: psycopg.Connection,
):
    """
    The headline correctness test for mig 095.

    Setup: insert an observation with base-column severity = 99 (a
    nonsense value that disagrees with any calibration). Expectation:
    derived.v_entity_fraud_features.severities[] returns the
    CALIBRATION value (which is 1 for candidate_no_pcc per seed 019),
    NOT the 99 from the base column.
    """
    _insert_obs(
        sev_db,
        signal_id="candidate_no_pcc",
        severity=99,  # nonsense value that disagrees with calibration
    )

    with sev_db.cursor() as cur:
        cur.execute("""
            SELECT severities[1] AS first_severity
            FROM   derived.v_entity_fraud_features
            WHERE  cycle = '2024'
              AND  entity_kind = 'candidate'
              AND  entity_id = 'H4NJ09999'
        """)
        (sev,) = cur.fetchone()
    assert sev == 1, (
        f"L2 severities[1] returned {sev}; expected 1 (calibration "
        f"value for candidate_no_pcc per seed 019). The base column "
        f"had 99 -- if L2 returned 99 the migration failed to invert "
        f"the dependency."
    )


def test_l3_evidence_severity_reflects_calibration_not_base_column(
    sev_db: psycopg.Connection,
):
    """L3 evidence panel surface MUST also pull from calibration."""
    _insert_obs(
        sev_db,
        signal_id="entity_on_leie",
        severity=99,  # nonsense value
    )

    with sev_db.cursor() as cur:
        cur.execute("""
            SELECT severity
            FROM   derived.v_entity_fraud_evidence
            WHERE  cycle = '2024'
              AND  signal_id = 'entity_on_leie'
              AND  entity_id = 'H4NJ09999'
        """)
        (sev,) = cur.fetchone()
    assert sev == 5, (
        f"v_entity_fraud_evidence.severity returned {sev}; expected 5 "
        f"(calibration value for entity_on_leie per seed 019). The base "
        f"column had 99 -- if the view returned 99 the migration failed."
    )


def test_calibration_update_propagates_to_l2_without_refresh(
    sev_db: psycopg.Connection,
):
    """
    The single-source-of-truth test: changing the calibration value
    changes the L2 severities WITHOUT re-running any refresher. This is
    the dependency-inversion's core promise -- update calibration, the
    view reflects the new value immediately.
    """
    _insert_obs(
        sev_db,
        signal_id="candidate_no_pcc",
        severity=1,  # value matches calibration today
    )

    with sev_db.cursor() as cur:
        cur.execute("""
            SELECT severities[1]
            FROM   derived.v_entity_fraud_features
            WHERE  cycle = '2024'
              AND  entity_id = 'H4NJ09999'
        """)
        (sev_before,) = cur.fetchone()
    assert sev_before == 1

    with sev_db.cursor() as cur:
        cur.execute(
            "UPDATE ref.fraud_signal_severity_calibration "
            "SET severity_level = 4 "
            "WHERE signal_id = 'candidate_no_pcc'"
        )
    sev_db.commit()

    with sev_db.cursor() as cur:
        cur.execute("""
            SELECT severities[1]
            FROM   derived.v_entity_fraud_features
            WHERE  cycle = '2024'
              AND  entity_id = 'H4NJ09999'
        """)
        (sev_after,) = cur.fetchone()
    assert sev_after == 4, (
        f"L2 severities[1] returned {sev_after} after calibration "
        f"updated to 4; expected 4. The base column still has 1 -- "
        f"if the view returned 1 the migration failed to invert the "
        f"dependency at the read layer."
    )


def test_max_severity_uses_calibration(sev_db: psycopg.Connection):
    """The MAX(severity) aggregation must also use calibration values."""
    _insert_obs(
        sev_db,
        signal_id="entity_on_leie",
        severity=99,
    )

    with sev_db.cursor() as cur:
        cur.execute("""
            SELECT max_severity
            FROM   derived.v_entity_fraud_features
            WHERE  cycle = '2024'
              AND  entity_id = 'H4NJ09999'
        """)
        (max_sev,) = cur.fetchone()
    assert max_sev == 5, (
        f"max_severity returned {max_sev}; expected 5 (entity_on_leie "
        f"calibration). The base column had 99."
    )


# =============================================================================
# COALESCE fallback for unregistered signals
# =============================================================================


def test_l2_falls_back_to_base_severity_when_calibration_missing(
    sev_db: psycopg.Connection,
):
    """
    When a signal_id has NO calibration row (transient new-signal
    rollout), the COALESCE fallback uses the base-column value so the
    view doesn't return NULL for the new signal.
    """
    with sev_db.cursor() as cur:
        cur.execute("""
            ALTER TABLE derived.fraud_signal_observation
                DROP CONSTRAINT IF EXISTS fraud_signal_observation_signal_id_check
        """)
        cur.execute("""
            ALTER TABLE derived.fraud_signal_config DROP CONSTRAINT IF EXISTS
                fraud_signal_config_signal_id_key CASCADE
        """)
    sev_db.commit()

    with sev_db.cursor() as cur:
        cur.execute("""
            INSERT INTO derived.fraud_signal_config
                (signal_id, signal_family, description, signal_family_anchor)
            VALUES
                ('test_new_signal', 'structural', 'test', FALSE)
            ON CONFLICT (signal_id) DO NOTHING
        """)
    sev_db.commit()

    _insert_obs(
        sev_db,
        signal_id="test_new_signal",
        severity=3,  # value the refresher would have hardcoded
    )

    with sev_db.cursor() as cur:
        cur.execute("""
            SELECT severities[1]
            FROM   derived.v_entity_fraud_features
            WHERE  cycle = '2024'
              AND  entity_id = 'H4NJ09999'
        """)
        (sev,) = cur.fetchone()
    assert sev == 3, (
        f"COALESCE fallback returned {sev}; expected 3 (base-column "
        f"value, since test_new_signal has no calibration row)"
    )


# =============================================================================
# Audit drift function
# =============================================================================


def test_audit_drift_returns_empty_on_clean_substrate(
    sev_db: psycopg.Connection,
):
    """When refresher emits values matching calibration, drift = 0 rows."""
    _insert_obs(
        sev_db,
        signal_id="candidate_no_pcc",
        severity=1,  # matches calibration
    )
    _insert_obs(
        sev_db,
        entity_id="H4NJ09998",
        signal_id="entity_on_leie",
        severity=5,  # matches calibration
    )

    with sev_db.cursor() as cur:
        cur.execute(
            "SELECT * FROM derived.audit_severity_drift(%s)",
            ('2024',),
        )
        rows = cur.fetchall()
    assert rows == [], (
        f"audit_severity_drift returned drift on clean substrate: {rows}"
    )


def test_audit_drift_surfaces_disagreements(sev_db: psycopg.Connection):
    """When refresher disagrees with calibration, the function surfaces it."""
    _insert_obs(
        sev_db,
        signal_id="candidate_no_pcc",
        severity=99,  # disagrees with calibration's value of 1
    )

    with sev_db.cursor() as cur:
        cur.execute(
            "SELECT signal_id, n_obs, hardcoded_severity, "
            "       calibration_severity, drifted "
            "FROM derived.audit_severity_drift(%s)",
            ('2024',),
        )
        rows = cur.fetchall()
    assert len(rows) == 1, f"expected 1 drift row, got {len(rows)}: {rows}"
    signal_id, n_obs, hardcoded, calibration, drifted = rows[0]
    assert signal_id == "candidate_no_pcc"
    assert n_obs == 1
    assert hardcoded == 99
    assert calibration == 1
    assert drifted is True


# =============================================================================
# Formula version provenance
# =============================================================================


def test_formula_version_registered(sev_db: psycopg.Connection):
    """The migration's formula_version is registered in ref.formula_version."""
    with sev_db.cursor() as cur:
        cur.execute("""
            SELECT effective_date, description
            FROM   ref.formula_version
            WHERE  formula_version = %s
        """, (EXPECTED_FORMULA_VERSION,))
        row = cur.fetchone()
    assert row is not None, (
        f"formula_version {EXPECTED_FORMULA_VERSION} not registered"
    )
    eff_date, desc = row
    assert eff_date.isoformat() == "2026-05-11"
    lower = desc.lower()
    assert "dependency" in lower or "inversion" in lower or "severity" in lower


def test_l2_view_comment_documents_inversion(sev_db: psycopg.Connection):
    """The L2 view's comment must mention the calibration source."""
    with sev_db.cursor() as cur:
        cur.execute("""
            SELECT obj_description(
                'derived.v_entity_fraud_features'::regclass,
                'pg_class'
            )
        """)
        (comment,) = cur.fetchone()
    assert comment is not None
    lower = comment.lower()
    assert "calibration" in lower, (
        f"L2 view comment does not document calibration source: "
        f"{comment!r}"
    )
