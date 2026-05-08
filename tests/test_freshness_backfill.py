"""Live-PG validation of the freshness-backfill bridge (mig 083).

The bridge surfaces are:

  * derived.v_freshness_backfill_candidates  -- read-only inspection view
  * derived.f_backfill_freshness_from_ingested_at()  -- side-effecting

The function emits synthetic 'materialized' signals into
governance.dataset_health from each raw table's MAX(ingested_at), so the
freshness-summary view can classify sources whose data was bulk-loaded
without the ingester emitting a Dagster materialization signal.

The tests below pin every action branch (inserted / skipped_empty_table /
skipped_already_recorded) and the integration with
derived.v_data_freshness_summary's classifier.
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


@pytest.fixture
def backfill_db(live_pg: psycopg.Connection) -> psycopg.Connection:
    """Cleanly-migrated DB with all seeds applied; raw tables empty."""
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


def _seed_zhvi_row(
    conn: psycopg.Connection, ingested_hours_ago: float
) -> None:
    """Insert one synthetic raw.zillow_zhvi_county row at a known ingested_at.

    Uses a unique region_id so repeated calls accumulate rather than
    UPSERT-collapse onto the same key. Provenance fields are filled with
    deterministic stand-ins.
    """
    with conn.cursor() as cur:
        # Pick a region_id that is unlikely to collide; we just want SOME row.
        cur.execute(
            "INSERT INTO raw.zillow_zhvi_county "
            "  (region_id, county_fips, region_name, state_code, "
            "   metro, observation_month, zhvi, "
            "   source_url, source_sha256, source_vintage, ingested_at) "
            "VALUES (99999, '34003', 'Test', 'NJ', 'NYC', "
            "        '2024-12-31'::DATE, 500000, "
            "        'http://test/zhvi', %s, 'test', "
            "        now() - (%s || ' hours')::INTERVAL) "
            "ON CONFLICT DO NOTHING",
            ("0" * 64, str(ingested_hours_ago)),
        )
    conn.commit()


def _signal_count(conn: psycopg.Connection, dataset_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM governance.dataset_health "
            "WHERE dataset_id = %s AND signal_name = 'materialized'",
            (dataset_id,),
        )
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


# ---------------------------------------------------------------------------
# Class A: candidate view shape and predicted_action correctness
# ---------------------------------------------------------------------------


class TestBackfillCandidateView:
    def test_shape_has_one_row_per_known_source(
        self, backfill_db: psycopg.Connection
    ) -> None:
        """The hardcoded UNION-ALL covers exactly the 14 sources we map."""
        with backfill_db.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM derived.v_freshness_backfill_candidates"
            )
            row = cur.fetchone()
            assert row is not None
            n = int(row[0])
        # The mapping covers every source registered in seeds 003..009 + 016.
        assert n >= 14, (
            f"expected >= 14 backfill candidates; got {n}. Add a UNION-ALL "
            f"branch to derived.v_freshness_backfill_candidates if the "
            f"release_calendar grew."
        )

    def test_empty_raw_table_is_skipped_empty(
        self, backfill_db: psycopg.Connection
    ) -> None:
        """A source whose raw table has zero rows -> predicted_action =
        'skipped_empty_table'. Brand-new DB after migrations: every raw
        table is empty, so every candidate predicts that branch."""
        with backfill_db.cursor() as cur:
            cur.execute(
                "SELECT predicted_action, COUNT(*) "
                "FROM derived.v_freshness_backfill_candidates "
                "GROUP BY predicted_action"
            )
            rows = cur.fetchall()
        actions = {r[0]: int(r[1]) for r in rows}
        assert actions.get("skipped_empty_table", 0) >= 14, actions
        assert actions.get("would_insert", 0) == 0, actions

    def test_seeded_table_predicts_would_insert(
        self, backfill_db: psycopg.Connection
    ) -> None:
        """A raw row with ingested_at flips the prediction to 'would_insert'."""
        _seed_zhvi_row(backfill_db, ingested_hours_ago=2)
        with backfill_db.cursor() as cur:
            cur.execute(
                "SELECT predicted_action, rows_in_table "
                "FROM derived.v_freshness_backfill_candidates "
                "WHERE source_id = 'raw.zillow_zhvi_county'"
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == "would_insert"
        assert int(row[1]) == 1


# ---------------------------------------------------------------------------
# Class B: function action branches
# ---------------------------------------------------------------------------


class TestBackfillFunction:
    def test_inserts_on_first_run(
        self, backfill_db: psycopg.Connection
    ) -> None:
        """Seed one ZHVI row; first invocation must INSERT a 'materialized'
        signal at the row's ingested_at."""
        _seed_zhvi_row(backfill_db, ingested_hours_ago=3)
        before = _signal_count(backfill_db, "raw.zillow_zhvi_county")
        with backfill_db.cursor() as cur:
            cur.execute(
                "SELECT action FROM "
                "derived.f_backfill_freshness_from_ingested_at() "
                "WHERE source_id = 'raw.zillow_zhvi_county'"
            )
            row = cur.fetchone()
        backfill_db.commit()
        after = _signal_count(backfill_db, "raw.zillow_zhvi_county")
        assert row is not None
        assert row[0] == "inserted"
        assert after == before + 1

    def test_idempotent_on_second_run(
        self, backfill_db: psycopg.Connection
    ) -> None:
        """A second invocation immediately after must skip with reason
        'skipped_already_recorded'; no new row is appended."""
        _seed_zhvi_row(backfill_db, ingested_hours_ago=3)
        with backfill_db.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM "
                "derived.f_backfill_freshness_from_ingested_at()"
            )
        backfill_db.commit()
        before_2nd = _signal_count(backfill_db, "raw.zillow_zhvi_county")

        with backfill_db.cursor() as cur:
            cur.execute(
                "SELECT action FROM "
                "derived.f_backfill_freshness_from_ingested_at() "
                "WHERE source_id = 'raw.zillow_zhvi_county'"
            )
            row = cur.fetchone()
        backfill_db.commit()
        after_2nd = _signal_count(backfill_db, "raw.zillow_zhvi_county")
        assert row is not None
        assert row[0] == "skipped_already_recorded"
        assert after_2nd == before_2nd  # no double-insert

    def test_emits_metadata_marking_backfill(
        self, backfill_db: psycopg.Connection
    ) -> None:
        """The synthetic signal must be distinguishable from an organic
        Dagster materialization signal so a future operator / sensor can
        opt to ignore the synthetic ones."""
        _seed_zhvi_row(backfill_db, ingested_hours_ago=3)
        with backfill_db.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM "
                "derived.f_backfill_freshness_from_ingested_at()"
            )
            cur.execute(
                "SELECT details->>'backfill', "
                "       details->>'backfill_source', "
                "       details->>'formula_version' "
                "FROM governance.dataset_health "
                "WHERE dataset_id = 'raw.zillow_zhvi_county' "
                "  AND signal_name = 'materialized' "
                "ORDER BY observed_at DESC LIMIT 1"
            )
            row = cur.fetchone()
        backfill_db.commit()
        assert row is not None
        is_backfill, source, version = row
        assert is_backfill == "true"
        assert source == "f_backfill_freshness_from_ingested_at"
        assert version == "1.8.1-freshness-backfill-v1"

    def test_organic_signal_takes_precedence_after_backfill(
        self, backfill_db: psycopg.Connection
    ) -> None:
        """A real Dagster materialization signal emitted AFTER the backfill
        must dominate: v_latest_materialization picks the most recent. The
        backfill signal stays in the audit log but no longer drives the
        freshness classifier."""
        _seed_zhvi_row(backfill_db, ingested_hours_ago=10)
        with backfill_db.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM "
                "derived.f_backfill_freshness_from_ingested_at()"
            )
            # Dagster comes along an hour later and emits its own signal.
            cur.execute(
                "INSERT INTO governance.dataset_health "
                "  (dataset_id, observed_at, signal_name, severity, details) "
                "VALUES ('raw.zillow_zhvi_county', "
                "        now() - INTERVAL '9 hours', "
                "        'materialized', 'info', "
                "        jsonb_build_object('rows_upserted', 6610))"
            )
        backfill_db.commit()
        with backfill_db.cursor() as cur:
            cur.execute(
                "SELECT details->>'backfill' FROM "
                "governance.v_latest_materialization "
                "WHERE dataset_id = 'raw.zillow_zhvi_county'"
            )
            row = cur.fetchone()
        assert row is not None
        # The latest signal is the organic one, which has no 'backfill' key
        assert row[0] is None

    def test_empty_table_returns_skipped_action(
        self, backfill_db: psycopg.Connection
    ) -> None:
        """Brand-new DB: every action returned should be 'skipped_empty_table'
        because every raw table has zero rows. The function returns one
        row per source regardless."""
        with backfill_db.cursor() as cur:
            cur.execute(
                "SELECT action, COUNT(*) FROM "
                "derived.f_backfill_freshness_from_ingested_at() "
                "GROUP BY action"
            )
            rows = cur.fetchall()
        actions = {r[0]: int(r[1]) for r in rows}
        assert actions.get("skipped_empty_table", 0) >= 14, actions
        assert actions.get("inserted", 0) == 0, actions


# ---------------------------------------------------------------------------
# Class C: end-to-end integration with the freshness classifier
# ---------------------------------------------------------------------------


class TestBackfillIntegrationWithFreshnessClassifier:
    def test_backfill_flips_status_from_never_to_fresh(
        self, backfill_db: psycopg.Connection
    ) -> None:
        """Before backfill: never_materialized. After: fresh (recent
        ingested_at). End-to-end confirmation that mig 083 closes the
        gap mig 082 left open."""
        _seed_zhvi_row(backfill_db, ingested_hours_ago=2)
        with backfill_db.cursor() as cur:
            cur.execute(
                "SELECT freshness_status FROM derived.v_data_freshness_summary "
                "WHERE source_id = 'raw.zillow_zhvi_county'"
            )
            before = cur.fetchone()
            assert before is not None
            assert before[0] == "never_materialized"

            cur.execute(
                "SELECT 1 FROM derived.f_backfill_freshness_from_ingested_at()"
            )
        backfill_db.commit()

        with backfill_db.cursor() as cur:
            cur.execute(
                "SELECT freshness_status FROM derived.v_data_freshness_summary "
                "WHERE source_id = 'raw.zillow_zhvi_county'"
            )
            after = cur.fetchone()
        assert after is not None
        assert after[0] == "fresh"

    def test_old_ingested_at_classifies_as_critical(
        self, backfill_db: psycopg.Connection
    ) -> None:
        """A raw row whose ingested_at is far older than ZHVI's 1224h budget
        x 1.5 = 1836h must classify as 'critical' after backfill. This is
        the safety property: the bridge does NOT lie about freshness; it
        just makes the timestamp visible to the classifier."""
        _seed_zhvi_row(backfill_db, ingested_hours_ago=2500)
        with backfill_db.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM derived.f_backfill_freshness_from_ingested_at()"
            )
        backfill_db.commit()

        with backfill_db.cursor() as cur:
            cur.execute(
                "SELECT freshness_status FROM derived.v_data_freshness_summary "
                "WHERE source_id = 'raw.zillow_zhvi_county'"
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == "critical"
