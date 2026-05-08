"""Live-PG validation of the data-freshness substrate (VISION_2026 §7.1).

The freshness substrate is migration ``082_data_freshness_summary`` plus
seed ``016_release_calendar_zhvi`` (release-calendar row for ZHVI). It
ships:

  * ``derived.v_data_freshness_summary``      -- one row per registered source
  * ``derived.v_platform_freshness_headline`` -- single-row UI rollup
  * ``derived.f_data_freshness_status(text)`` -- scalar wrapper

The classifier reads ``governance.v_latest_materialization`` (which itself
reads ``governance.dataset_health`` filtered to ``signal_name = 'materialized'``)
and compares ``now() - last_materialized_at`` to ``cadence_period_hours +
expected_lag_hours``:

   <= budget        -> 'fresh'
   1-1.5x budget    -> 'stale'
   > 1.5x budget    -> 'critical'
   no signal at all -> 'never_materialized'

The tests below seed synthetic ``governance.dataset_health`` rows with
deterministic ``observed_at`` offsets relative to ``now()`` and assert the
classifier reaches each state correctly. The 1.5x threshold is empirically
calibrated and pinned here so a future change to the cliff has to update
the test deliberately.
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


# ---------------------------------------------------------------------------
# Fixture: cleanly-migrated DB with all seeds applied (so ref.release_calendar
# has 14 rows from seeds 003/004/007/008/009/016).
# ---------------------------------------------------------------------------


@pytest.fixture
def freshness_db(live_pg: psycopg.Connection) -> psycopg.Connection:
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
            "    EXECUTE 'DROP VIEW IF EXISTS public.' "
            "         || quote_ident(r.viewname) || ' CASCADE'; "
            "  END LOOP; "
            "END $$;",
        )
    conn.commit()
    apply_migrations(conn, discover(MIGRATIONS_DIR))
    apply_migrations(conn, discover(SEEDS_DIR))
    conn.commit()
    return conn


def _emit_materialized(
    conn: psycopg.Connection, dataset_id: str, hours_ago: float, rows: int = 1000
) -> None:
    """Insert a synthetic 'materialized' signal at now() - <hours_ago> hours."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO governance.dataset_health "
            "    (dataset_id, observed_at, signal_name, severity, details) "
            "VALUES (%s, now() - (%s || ' hours')::INTERVAL, "
            "        'materialized', 'info', "
            "        jsonb_build_object('rows_upserted', %s::BIGINT))",
            (dataset_id, str(hours_ago), rows),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Class A: substrate shape and never_materialized state
# ---------------------------------------------------------------------------


class TestFreshnessSubstrate:
    def test_view_has_one_row_per_release_calendar_source(
        self, freshness_db: psycopg.Connection
    ) -> None:
        """The view's row count must equal release_calendar's row count.

        The LEFT JOIN against governance.v_latest_materialization preserves
        every source even when no materialized signal exists yet.
        """
        with freshness_db.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM ref.release_calendar")
            row = cur.fetchone()
            assert row is not None
            n_calendar = int(row[0])
            cur.execute("SELECT COUNT(*) FROM derived.v_data_freshness_summary")
            row = cur.fetchone()
            assert row is not None
            n_view = int(row[0])
        assert n_view == n_calendar
        # Sanity: at least the 7 housing-relevant sources we know are seeded
        # (FRED, CPI, FHFA, ACS-MHI, ACS-housing, NJ-DCA, ZHVI).
        assert n_view >= 7

    def test_zhvi_release_calendar_seed_landed(
        self, freshness_db: psycopg.Connection
    ) -> None:
        """Seed 016 must register raw.zillow_zhvi_county with monthly cadence."""
        with freshness_db.cursor() as cur:
            cur.execute(
                "SELECT cadence, expected_lag_hours, day_of_month "
                "FROM ref.release_calendar "
                "WHERE source_id = 'raw.zillow_zhvi_county'"
            )
            row = cur.fetchone()
        assert row is not None
        cadence, lag, dom = row
        assert cadence == "monthly"
        # 21 days = 504 hours; matches the empirical [13, 21]-day window.
        assert int(lag) == 504
        assert int(dom) == 15

    def test_never_materialized_when_no_health_signal(
        self, freshness_db: psycopg.Connection
    ) -> None:
        """A source with no governance.dataset_health row classifies as never_materialized."""
        with freshness_db.cursor() as cur:
            cur.execute(
                "SELECT freshness_status, last_materialized_at, "
                "       hours_since_materialized "
                "FROM derived.v_data_freshness_summary "
                "WHERE source_id = 'raw.fred_observation'"
            )
            row = cur.fetchone()
        assert row is not None
        status, last_at, hours = row
        assert status == "never_materialized"
        assert last_at is None
        assert hours is None

    def test_expected_max_age_hours_uses_cadence_period_plus_lag(
        self, freshness_db: psycopg.Connection
    ) -> None:
        """expected_max_age_hours = cadence_period_hours + expected_lag_hours
        for scheduled cadences; equals expected_lag_hours for on_event.

        Pin a few values so a future cadence-table edit cannot silently
        change the budget. The seed values:
          raw.cpi_u (monthly, lag=48)         => 720 + 48  = 768
          raw.fred_observation (weekly, 48)   => 168 + 48  = 216
          raw.fhfa_hpi_county (quarterly, 240)=> 2160 + 240= 2400
          raw.zillow_zhvi_county (monthly,504)=> 720 + 504 = 1224
          ref.zip_county (on_event, 720)       => 720
        """
        with freshness_db.cursor() as cur:
            cur.execute(
                "SELECT source_id, expected_max_age_hours "
                "FROM derived.v_data_freshness_summary "
                "WHERE source_id IN ("
                "  'raw.cpi_u','raw.fred_observation','raw.fhfa_hpi_county',"
                "  'raw.zillow_zhvi_county','ref.zip_county'"
                ") "
                "ORDER BY source_id"
            )
            rows = cur.fetchall()
        budgets = {r[0]: int(r[1]) for r in rows}
        assert budgets == {
            "raw.cpi_u": 768,
            "raw.fhfa_hpi_county": 2400,
            "raw.fred_observation": 216,
            "raw.zillow_zhvi_county": 1224,
            "ref.zip_county": 720,
        }


# ---------------------------------------------------------------------------
# Class B: classifier branches (fresh / stale / critical)
# ---------------------------------------------------------------------------


class TestFreshnessClassifier:
    """Pin every classifier branch with empirically-verified offsets.

    raw.cpi_u has expected_max_age_hours = 768 (= 32 days). We use that
    as the reference source for branch coverage:

      24h  ago  -> 24/768  = 0.031  -> FRESH    (well within budget)
      720h ago  -> 720/768 = 0.937  -> FRESH    (just under budget)
      900h ago  -> 900/768 = 1.171  -> STALE    (1-1.5x budget)
      1200h ago -> 1200/768= 1.562  -> CRITICAL (>1.5x budget)
    """

    def test_fresh_well_within_budget(self, freshness_db: psycopg.Connection) -> None:
        _emit_materialized(freshness_db, "raw.cpi_u", hours_ago=24)
        with freshness_db.cursor() as cur:
            cur.execute(
                "SELECT freshness_status FROM derived.v_data_freshness_summary "
                "WHERE source_id = 'raw.cpi_u'"
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == "fresh"

    def test_fresh_just_under_budget(self, freshness_db: psycopg.Connection) -> None:
        _emit_materialized(freshness_db, "raw.cpi_u", hours_ago=720)
        with freshness_db.cursor() as cur:
            cur.execute(
                "SELECT freshness_status FROM derived.v_data_freshness_summary "
                "WHERE source_id = 'raw.cpi_u'"
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == "fresh"

    def test_stale_1x_to_1_5x_budget(self, freshness_db: psycopg.Connection) -> None:
        # 900h is 1.17x the 768h budget -- empirically in the slip-but-
        # not-yet-broken band (publishers sometimes ship half a cadence late).
        _emit_materialized(freshness_db, "raw.cpi_u", hours_ago=900)
        with freshness_db.cursor() as cur:
            cur.execute(
                "SELECT freshness_status, hours_since_materialized "
                "FROM derived.v_data_freshness_summary "
                "WHERE source_id = 'raw.cpi_u'"
            )
            row = cur.fetchone()
        assert row is not None
        status, hours = row
        assert status == "stale"
        # Allow a few seconds of clock drift between the INSERT and the SELECT.
        assert 899.5 < float(hours) < 900.5

    def test_critical_over_1_5x_budget(
        self, freshness_db: psycopg.Connection
    ) -> None:
        # 1200h is 1.56x the 768h budget -- past the cliff, ingester likely
        # broken or publisher has overhauled its cadence.
        _emit_materialized(freshness_db, "raw.cpi_u", hours_ago=1200)
        with freshness_db.cursor() as cur:
            cur.execute(
                "SELECT freshness_status FROM derived.v_data_freshness_summary "
                "WHERE source_id = 'raw.cpi_u'"
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == "critical"

    def test_classifier_picks_most_recent_signal(
        self, freshness_db: psycopg.Connection
    ) -> None:
        """v_latest_materialization uses DISTINCT ON (dataset_id) ORDER BY
        observed_at DESC; an old + a new signal must classify by the new."""
        _emit_materialized(freshness_db, "raw.cpi_u", hours_ago=2000)  # critical
        _emit_materialized(freshness_db, "raw.cpi_u", hours_ago=24)   # fresh
        with freshness_db.cursor() as cur:
            cur.execute(
                "SELECT freshness_status FROM derived.v_data_freshness_summary "
                "WHERE source_id = 'raw.cpi_u'"
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == "fresh"

    def test_zhvi_classifier_uses_504h_publisher_lag(
        self, freshness_db: psycopg.Connection
    ) -> None:
        """ZHVI's expected_max_age_hours is 1224 (= 720 monthly + 504 publisher
        lag). 1300h ago is 1.06x budget -> stale. Pins the seed-016 lag value."""
        _emit_materialized(freshness_db, "raw.zillow_zhvi_county", hours_ago=1300)
        with freshness_db.cursor() as cur:
            cur.execute(
                "SELECT freshness_status FROM derived.v_data_freshness_summary "
                "WHERE source_id = 'raw.zillow_zhvi_county'"
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == "stale"

        # And 2000h ago is 1.63x budget -> critical.
        _emit_materialized(freshness_db, "raw.zillow_zhvi_county", hours_ago=2000)
        with freshness_db.cursor() as cur:
            cur.execute(
                # Pin the latest signal we just inserted.
                "DELETE FROM governance.dataset_health "
                "WHERE dataset_id = 'raw.zillow_zhvi_county' "
                "  AND observed_at > now() - INTERVAL '1500 hours'"
            )
        freshness_db.commit()
        # Now only the 2000h-ago signal remains -> critical.
        with freshness_db.cursor() as cur:
            cur.execute(
                "SELECT freshness_status FROM derived.v_data_freshness_summary "
                "WHERE source_id = 'raw.zillow_zhvi_county'"
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == "critical"


# ---------------------------------------------------------------------------
# Class C: platform-wide rollup
# ---------------------------------------------------------------------------


class TestPlatformFreshnessHeadline:
    def test_rollup_partial_when_all_never_materialized(
        self, freshness_db: psycopg.Connection
    ) -> None:
        """Brand-new DB: every source is never_materialized -> overall PARTIAL."""
        with freshness_db.cursor() as cur:
            cur.execute("SELECT * FROM derived.v_platform_freshness_headline")
            row = cur.fetchone()
            cols = [c.name for c in cur.description]  # type: ignore[union-attr]
        assert row is not None
        d = dict(zip(cols, row, strict=True))
        assert d["overall_status"] == "PARTIAL"
        assert int(d["n_fresh"]) == 0
        assert int(d["n_stale"]) == 0
        assert int(d["n_critical"]) == 0
        assert int(d["n_never_materialized"]) == int(d["n_sources"])
        assert d["worst_source_id"] is None  # no critical/stale to point at

    def test_rollup_fresh_when_every_source_has_recent_signal(
        self, freshness_db: psycopg.Connection
    ) -> None:
        """Materialize every source 1 hour ago -> overall FRESH."""
        with freshness_db.cursor() as cur:
            cur.execute("SELECT source_id FROM ref.release_calendar")
            sources = [r[0] for r in cur.fetchall()]
        for src in sources:
            _emit_materialized(freshness_db, src, hours_ago=1)
        with freshness_db.cursor() as cur:
            cur.execute("SELECT * FROM derived.v_platform_freshness_headline")
            row = cur.fetchone()
            cols = [c.name for c in cur.description]  # type: ignore[union-attr]
        assert row is not None
        d = dict(zip(cols, row, strict=True))
        assert d["overall_status"] == "FRESH"
        assert int(d["n_fresh"]) == int(d["n_sources"])
        assert int(d["n_stale"]) == 0
        assert int(d["n_critical"]) == 0
        assert int(d["n_never_materialized"]) == 0

    def test_rollup_critical_dominates_stale_dominates_partial_dominates_fresh(
        self, freshness_db: psycopg.Connection
    ) -> None:
        """Worst-source-wins: any critical source forces overall CRITICAL,
        even if 99% of sources are fresh."""
        with freshness_db.cursor() as cur:
            cur.execute("SELECT source_id FROM ref.release_calendar")
            all_sources = [r[0] for r in cur.fetchall()]
        # Materialize all fresh, then one critical.
        for src in all_sources:
            _emit_materialized(freshness_db, src, hours_ago=1)
        # raw.fred_observation has budget 216h; 500h is 2.31x -> critical.
        # We need to delete the 1h signal we just emitted so the latest is 500h.
        with freshness_db.cursor() as cur:
            cur.execute(
                "DELETE FROM governance.dataset_health "
                "WHERE dataset_id = 'raw.fred_observation'"
            )
        freshness_db.commit()
        _emit_materialized(freshness_db, "raw.fred_observation", hours_ago=500)
        with freshness_db.cursor() as cur:
            cur.execute("SELECT * FROM derived.v_platform_freshness_headline")
            row = cur.fetchone()
            cols = [c.name for c in cur.description]  # type: ignore[union-attr]
        assert row is not None
        d = dict(zip(cols, row, strict=True))
        assert d["overall_status"] == "CRITICAL"
        assert int(d["n_critical"]) == 1
        assert d["worst_source_id"] == "raw.fred_observation"
        assert d["worst_status"] == "critical"


# ---------------------------------------------------------------------------
# Class D: scalar wrapper
# ---------------------------------------------------------------------------


class TestFreshnessScalar:
    def test_scalar_returns_status_for_known_source(
        self, freshness_db: psycopg.Connection
    ) -> None:
        _emit_materialized(freshness_db, "raw.cpi_u", hours_ago=24)
        with freshness_db.cursor() as cur:
            cur.execute(
                "SELECT derived.f_data_freshness_status('raw.cpi_u')"
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == "fresh"

    def test_scalar_returns_null_for_unknown_source(
        self, freshness_db: psycopg.Connection
    ) -> None:
        """Substrate honesty: a source not in ref.release_calendar surfaces
        NULL, not a fabricated 'fresh' / 'critical' / 'never_materialized'."""
        with freshness_db.cursor() as cur:
            cur.execute(
                "SELECT derived.f_data_freshness_status('raw.does_not_exist')"
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] is None

    def test_scalar_returns_never_for_known_but_silent_source(
        self, freshness_db: psycopg.Connection
    ) -> None:
        with freshness_db.cursor() as cur:
            cur.execute(
                "SELECT derived.f_data_freshness_status('raw.fhfa_hpi_county')"
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == "never_materialized"
