"""Live-PG tests for migrations 119 + 120 (national leads serving snapshot).

VISION_2026 Pillar 2 (civic integrity) FRAUD-F8 SERVING layer. The snapshot
decouples serving from substrate size: rank leads where the national CMS data
lives, push only the top-N (pre-resolved) rows + scope-level totals to a
free-tier serving DB.

What this module pins:
    * formula_version 3.7.0-fraud-national-leads-snapshot-v1 is registered.
    * derived.high_value_leads_snapshot: columns, PK, provenance + CHECK
      constraints (source_scope, data_quality), FK to ref.formula_version.
    * derived.leads_snapshot_meta: columns, PK, CHECK constraints.
    * The loader's SQL (imported from the module) executes against the live
      view and returns exactly the projected column set.
"""

from __future__ import annotations

import psycopg
import pytest

from scripts.load_national_leads_snapshot import (
    _LEAD_COLUMNS,
    _META_SQL,
    _SOURCE_SQL,
    SNAPSHOT_FORMULA_VERSION,
)

pytestmark = pytest.mark.live_pg

# Both CHECK and FK violations surface as psycopg IntegrityError subclasses.
_INTEGRITY = psycopg.errors.IntegrityError


def _cols(conn: psycopg.Connection, table: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'derived' AND table_name = %s
        """,
        (table,),
    ).fetchall()
    return {r[0] for r in rows}


def test_formula_version_registered(live_pg: psycopg.Connection) -> None:
    row = live_pg.execute(
        "SELECT 1 FROM ref.formula_version WHERE formula_version = %s",
        (SNAPSHOT_FORMULA_VERSION,),
    ).fetchone()
    assert row is not None, "snapshot formula_version must be registered"


def test_snapshot_table_columns(live_pg: psycopg.Connection) -> None:
    cols = _cols(live_pg, "high_value_leads_snapshot")
    # Provenance discipline (verifiable-data invariant).
    for c in (
        "source_scope", "formula_version", "source_vintage_hash",
        "snapshot_at", "data_quality",
    ):
        assert c in cols, f"missing provenance column {c}"
    # Every field the loader projects must exist on the table.
    for c in _LEAD_COLUMNS:
        assert c in cols, f"missing lead column {c}"


def test_meta_table_columns(live_pg: psycopg.Connection) -> None:
    cols = _cols(live_pg, "leads_snapshot_meta")
    for c in (
        "source_scope", "formula_version", "source_vintage_hash", "snapshot_at",
        "data_quality", "n_total", "n_undetected", "n_already_caught",
        "n_multi_source", "n_repeat_violators", "n_reward_eligible",
        "max_undetected_scale_usd", "max_exposure_usd",
        "total_reward_eligible_exposure_usd", "count_by_tier",
        "n_shown_undetected", "n_shown_caught",
    ):
        assert c in cols, f"missing meta column {c}"


def test_source_scope_check_rejects_bad_value(live_pg: psycopg.Connection) -> None:
    live_pg.execute("SAVEPOINT s")
    with pytest.raises(_INTEGRITY):
        live_pg.execute(
            """
            INSERT INTO derived.high_value_leads_snapshot
                (source_scope, formula_version, source_vintage_hash,
                 lead_rank, entity_kind, entity_id, latest_cycle, n_cycles,
                 n_signals, n_families, max_severity, best_reward_tier,
                 reward_eligible, has_prior_sanction, repeat_violator,
                 multi_source, driver_signal_id)
            VALUES ('XX', %s, 'h', 1, 'provider', 'TESTNPI', '2024', 1, 1, 1,
                    3, 3, FALSE, FALSE, FALSE, FALSE, 'd')
            """,
            (SNAPSHOT_FORMULA_VERSION,),
        )
    live_pg.execute("ROLLBACK TO SAVEPOINT s")


def test_data_quality_check_rejects_bad_value(live_pg: psycopg.Connection) -> None:
    live_pg.execute("SAVEPOINT s")
    with pytest.raises(_INTEGRITY):
        live_pg.execute(
            """
            INSERT INTO derived.high_value_leads_snapshot
                (source_scope, formula_version, source_vintage_hash, data_quality,
                 lead_rank, entity_kind, entity_id, latest_cycle, n_cycles,
                 n_signals, n_families, max_severity, best_reward_tier,
                 reward_eligible, has_prior_sanction, repeat_violator,
                 multi_source, driver_signal_id)
            VALUES ('nj', %s, 'h', 'guessed', 1, 'provider', 'TESTNPI', '2024',
                    1, 1, 1, 3, 3, FALSE, FALSE, FALSE, FALSE, 'd')
            """,
            (SNAPSHOT_FORMULA_VERSION,),
        )
    live_pg.execute("ROLLBACK TO SAVEPOINT s")


def test_formula_version_fk_enforced(live_pg: psycopg.Connection) -> None:
    live_pg.execute("SAVEPOINT s")
    with pytest.raises(_INTEGRITY):
        live_pg.execute(
            """
            INSERT INTO derived.high_value_leads_snapshot
                (source_scope, formula_version, source_vintage_hash,
                 lead_rank, entity_kind, entity_id, latest_cycle, n_cycles,
                 n_signals, n_families, max_severity, best_reward_tier,
                 reward_eligible, has_prior_sanction, repeat_violator,
                 multi_source, driver_signal_id)
            VALUES ('nj', 'no-such-formula-version', 'h', 1, 'provider',
                    'TESTNPI', '2024', 1, 1, 1, 3, 3, FALSE, FALSE, FALSE,
                    FALSE, 'd')
            """,
        )
    live_pg.execute("ROLLBACK TO SAVEPOINT s")


def test_snapshot_roundtrip(live_pg: psycopg.Connection) -> None:
    live_pg.execute("SAVEPOINT s")
    live_pg.execute(
        """
        INSERT INTO derived.high_value_leads_snapshot
            (source_scope, formula_version, source_vintage_hash,
             lead_rank, entity_kind, entity_id, display_name, provider_state,
             is_nj, latest_cycle, n_cycles, n_signals, n_families, max_severity,
             best_reward_tier, reward_eligible, has_prior_sanction,
             repeat_violator, multi_source, provider_scale_usd, driver_signal_id)
        VALUES ('nj', %s, 'h', 7, 'provider', 'RT_TEST_NPI', 'Jane Doe', 'NJ',
                TRUE, '2024', 1, 2, 2, 4, 3, FALSE, FALSE, FALSE, TRUE,
                1234567, 'opioid_prescribing_outlier')
        """,
        (SNAPSHOT_FORMULA_VERSION,),
    )
    row = live_pg.execute(
        """
        SELECT display_name, provider_state, n_families, data_quality
        FROM derived.high_value_leads_snapshot
        WHERE source_scope='nj' AND entity_kind='provider'
          AND entity_id='RT_TEST_NPI'
        """
    ).fetchone()
    assert row == ("Jane Doe", "NJ", 2, "computed")
    live_pg.execute("ROLLBACK TO SAVEPOINT s")


def test_loader_source_sql_executes_and_projects_columns(
    live_pg: psycopg.Connection,
) -> None:
    """The loader's SELECT must run against the live view and project the
    identity + lead columns the INSERT expects (order-sensitive contract)."""
    cur = live_pg.execute(_SOURCE_SQL, {"n_undetected": 1, "n_caught": 1})
    names = [d.name for d in (cur.description or [])]
    # The projection is: entity identity + the shared _LEAD_COLUMNS set.
    assert names[0] == "lead_rank"
    assert set(_LEAD_COLUMNS).issubset(set(names))


def test_loader_meta_sql_executes(live_pg: psycopg.Connection) -> None:
    cur = live_pg.execute(_META_SQL)
    names = [d.name for d in (cur.description or [])]
    for c in (
        "n_total", "n_undetected", "n_already_caught", "n_multi_source",
        "n_repeat_violators", "n_reward_eligible", "max_undetected_scale_usd",
        "max_exposure_usd", "total_reward_eligible_exposure_usd", "count_by_tier",
    ):
        assert c in names
    row = cur.fetchone()
    assert row is not None
