"""Tests for migration 050_fraud_signal_observation (Tier 4 v3 L1 + L2).

Two layers:

1. STATIC checks (no DB)
   - The migration file exists at the expected path and parses as text.
   - Required identifiers (table name, view name, both indexes, all CHECK
     constraints we depend on downstream) are present in the SQL.

2. LIVE checks (live_pg, gated on PG_TEST_DSN)
   - All migrations + seeds apply against an ephemeral Postgres.
   - The L1 table accepts a well-formed observation.
   - Each CHECK constraint rejects the malformed inputs it is meant to
     reject (cycle regex, severity range, percentile range, entity_kind
     whitelist, empty entity_id / signal_id / peer_bucket / evidence_url).
   - The L2 pivot view aggregates correctly: a single entity that fires
     two signals collapses to one row with parallel arrays in
     ``signal_id`` order, ``n_signals_fired`` matches the row count,
     ``primary_peer_bucket`` is the higher-severity signal's bucket,
     and ``last_observation_at`` is the max of the per-signal timestamps.

The static layer guards against regressions where someone reorganizes
the SQL but drops a CHECK we relied on; the live layer guards against
SQL that parses but does not enforce the contract.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import psycopg


REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATION_PATH = REPO_ROOT / "db" / "migrations" / "050_fraud_signal_observation.sql"


# ============================================================================
# 1. STATIC checks (no DB)
# ============================================================================


def test_migration_file_exists() -> None:
    """The migration ships at the canonical path."""
    assert MIGRATION_PATH.is_file(), f"missing: {MIGRATION_PATH}"


def test_migration_declares_l1_table_and_l2_view() -> None:
    """Top-level identifiers L3+ depends on must appear in the SQL."""
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS derived.fraud_signal_observation" in sql
    assert "CREATE OR REPLACE VIEW derived.v_entity_fraud_features" in sql


def test_migration_declares_required_indexes() -> None:
    """Both access-pattern indexes must be created (PK covers per-entity drill)."""
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "fraud_signal_observation_signal_pct_idx" in sql
    assert "fraud_signal_observation_bucket_idx" in sql


def test_migration_declares_required_check_constraints() -> None:
    """The contract guarantees we make to L3 must be enforced as CHECKs."""
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    # cycle regex
    assert "cycle ~ '^[0-9]{4}$'" in sql
    # entity_kind whitelist (5 values)
    assert "entity_kind IN (" in sql
    for kind in ("'committee'", "'candidate'", "'treasurer'", "'address'", "'donor_cluster'"):
        assert kind in sql, f"missing entity_kind value {kind} in CHECK"
    # severity range
    assert "severity BETWEEN 1 AND 5" in sql
    # peer_percentile range
    assert "peer_percentile >= 0 AND peer_percentile <= 1" in sql


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


def _insert_obs(
    conn: psycopg.Connection,
    *,
    cycle: str = "2024",
    entity_kind: str = "committee",
    entity_id: str = "C00500001",
    signal_id: str = "treasurer_concentration",
    raw_value: float | None = 17.0,
    severity: int = 3,
    peer_bucket: str = "office=H|state=NJ|incumbent=challenger",
    peer_percentile: float = 0.99,
    evidence_url: str = ("/fec/metrics/treasurer_concentration?cycle=2024"),
    materialized_at: dt.datetime | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO derived.fraud_signal_observation ("
            "    cycle, entity_kind, entity_id, signal_id, raw_value, "
            "    severity, peer_bucket, peer_percentile, evidence_url, "
            "    materialized_at"
            ") VALUES ("
            "    %s, %s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, now())"
            ")",
            (
                cycle,
                entity_kind,
                entity_id,
                signal_id,
                raw_value,
                severity,
                peer_bucket,
                peer_percentile,
                evidence_url,
                materialized_at,
            ),
        )


def test_l1_accepts_well_formed_observation(
    fraud_db: psycopg.Connection,
) -> None:
    """Happy path: a row that satisfies every CHECK is accepted."""
    _insert_obs(fraud_db)
    fraud_db.commit()
    with fraud_db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM derived.fraud_signal_observation")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 1


@pytest.mark.parametrize(
    ("kwarg", "value", "expected_substr"),
    [
        ("cycle", "20XX", "cycle"),
        ("entity_kind", "company", "entity_kind"),
        ("entity_id", "", "entity_id"),
        ("signal_id", "", "signal_id"),
        ("severity", 0, "severity"),
        ("severity", 6, "severity"),
        ("peer_bucket", "", "peer_bucket"),
        ("peer_percentile", -0.01, "peer_percentile"),
        ("peer_percentile", 1.01, "peer_percentile"),
        ("evidence_url", "", "evidence_url"),
    ],
)
def test_l1_rejects_constraint_violations(
    fraud_db: psycopg.Connection,
    kwarg: str,
    value: object,
    expected_substr: str,
) -> None:
    """Each CHECK constraint must reject the input it was written for."""
    import psycopg

    overrides: dict[str, object] = {kwarg: value}
    with pytest.raises(psycopg.errors.CheckViolation) as exc:
        _insert_obs(fraud_db, **overrides)  # type: ignore[arg-type]
        fraud_db.commit()
    fraud_db.rollback()
    # The constraint name carries the column it guards; assert it appears in
    # the diagnostic message so the test fails loudly if a CHECK gets renamed
    # silently (the L2/L3 layers depend on these column names).
    assert expected_substr in str(exc.value).lower()


def test_l1_pk_rejects_duplicate_signal_per_entity(
    fraud_db: psycopg.Connection,
) -> None:
    """The PK (cycle, entity_kind, entity_id, signal_id) is unique."""
    import psycopg

    _insert_obs(fraud_db)
    fraud_db.commit()
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_obs(fraud_db)
        fraud_db.commit()
    fraud_db.rollback()


def test_l2_pivot_aggregates_two_signals_into_one_row(
    fraud_db: psycopg.Connection,
) -> None:
    """The view collapses N (entity, signal) rows into one row per entity."""
    t1 = dt.datetime(2026, 5, 4, 10, 0, 0, tzinfo=dt.UTC)
    t2 = dt.datetime(2026, 5, 4, 11, 0, 0, tzinfo=dt.UTC)

    # Same entity, two signals; pick (severity, percentile) so we can
    # uniquely identify which is "primary":
    #   committee_address_clusters: severity=4, p=0.95
    #   treasurer_concentration: severity=3, p=0.99
    # Higher severity wins -> primary_peer_bucket is the address bucket.
    # NOTE: signal_ids must match derived.fraud_signal_config rows; the
    # L2 view INNER-JOINs against config (migration 061) and silently
    # drops orphan signals.
    _insert_obs(
        fraud_db,
        signal_id="treasurer_concentration",
        severity=3,
        peer_percentile=0.99,
        peer_bucket="office=H|state=NJ",
        evidence_url="/fec/metrics/treasurer_concentration?cycle=2024",
        materialized_at=t1,
    )
    _insert_obs(
        fraud_db,
        signal_id="committee_address_clusters",
        severity=4,
        peer_percentile=0.95,
        peer_bucket="state=NJ|zip=12345",
        evidence_url="/fec/metrics/committee_address_clusters?cycle=2024",
        materialized_at=t2,
    )
    fraud_db.commit()

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT cycle, entity_kind, entity_id, n_signals_fired, "
            "       max_severity, max_peer_percentile, primary_peer_bucket, "
            "       signals_fired, severities, peer_percentiles, "
            "       last_observation_at "
            "FROM derived.v_entity_fraud_features "
            "WHERE entity_id = %s",
            ("C00500001",),
        )
        rows = cur.fetchall()

    assert len(rows) == 1
    (
        cycle,
        entity_kind,
        entity_id,
        n,
        max_sev,
        max_pct,
        primary_bucket,
        signals,
        severities,
        percentiles,
        last_obs,
    ) = rows[0]

    assert cycle == "2024"
    assert entity_kind == "committee"
    assert entity_id == "C00500001"
    assert n == 2
    assert max_sev == 4
    assert float(max_pct) == pytest.approx(0.99)
    # higher severity wins primary, even though its percentile is lower
    assert primary_bucket == "state=NJ|zip=12345"
    # parallel arrays sorted by signal_id alphabetically:
    #   "committee_address_clusters" < "treasurer_concentration"
    assert list(signals) == [
        "committee_address_clusters", "treasurer_concentration",
    ]
    assert list(severities) == [4, 3]
    assert [float(p) for p in percentiles] == pytest.approx([0.95, 0.99])
    assert last_obs == t2  # MAX over per-signal timestamps


def test_l2_pivot_excludes_entities_with_no_observations(
    fraud_db: psycopg.Connection,
) -> None:
    """Absent rows mean signal-did-not-fire; entities with zero signals
    are absent from the pivot. n_signals_fired is therefore strictly >= 1."""
    _insert_obs(fraud_db, entity_id="C_HAS_SIGNAL")
    fraud_db.commit()

    with fraud_db.cursor() as cur:
        cur.execute("SELECT MIN(n_signals_fired) FROM derived.v_entity_fraud_features")
        row = cur.fetchone()
        assert row is not None
        assert row[0] >= 1
