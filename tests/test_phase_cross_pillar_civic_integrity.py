"""Regression tests for migration 091: derived.v_nj_civic_integrity_state_summary.

Pins the cross-pillar substrate that powers the CivicIntegrityCallout
component on /housing/[id]. The view is the canonical source of truth
for the state-wide NJ civic-integrity headline numbers; if the column
shape or aggregation arithmetic drifts, the callout would silently lie
about the platform's posture.
"""

from __future__ import annotations

import logging
from pathlib import Path

import psycopg
import pytest

from scripts.migrate import apply_migrations, discover

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = ROOT / "db" / "migrations"
SEEDS_DIR = ROOT / "db" / "seeds"

logging.basicConfig(level=logging.WARNING)


@pytest.fixture
def cross_pillar_db(live_pg: psycopg.Connection) -> psycopg.Connection:
    """Cleanly-migrated DB scoped per-test; raw tables empty, ref seeded.

    Mirrors the evidence_db fixture in test_phase_f4_evidence_view_and_nj_officials
    -- drops every schema, re-applies all migrations + seeds. Cycle-2026
    NJ-keyed test data is planted explicitly by the test that needs it.
    """
    conn = live_pg
    with conn.cursor() as cur:
        for sch in ("governance", "derived", "raw", "ref"):
            cur.execute(f"DROP SCHEMA IF EXISTS {sch} CASCADE")
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


def _seed_synthetic_nj_substrate(conn: psycopg.Connection) -> None:
    """Plant a minimal NJ-keyed FEC sample plus one fraud_signal_observation
    per entity-kind so the view has rows to aggregate over."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO raw.fec_candidate (
                cycle, cand_id, cand_name, cand_office, cand_office_st,
                cand_office_district, cand_pty_affiliation, cand_ici,
                cand_status, cand_election_yr, source_url, source_sha256,
                source_vintage
            ) VALUES
            ('2026', 'H8NJ_T01', 'NJ HOUSE TEST', 'H', 'NJ', '11',
             'DEM', 'I', 'C', 2026, 'synthetic://test', 'a' || repeat('0',63),
             '2026-cn'),
            ('2026', 'H8NJ_T02', 'NJ HOUSE TEST 2', 'H', 'NJ', '12',
             'REP', 'C', 'C', 2026, 'synthetic://test', 'a' || repeat('0',63),
             '2026-cn'),
            ('2026', 'H8TX_OOS', 'OUT OF STATE', 'H', 'TX', '01',
             'REP', 'I', 'C', 2026, 'synthetic://test', 'a' || repeat('0',63),
             '2026-cn');

            INSERT INTO raw.fec_committee (
                cycle, cmte_id, cmte_nm, tres_nm, cmte_st1, cmte_city,
                cmte_st, cmte_zip, cmte_dsgn, cmte_tp, cmte_filing_freq,
                source_url, source_sha256, source_vintage
            ) VALUES
            ('2026', 'C00_NJ01', 'NJ TEST CMTE', 'TREASURER A',
             '1 NJ ST', 'NEWARK', 'NJ', '07102', 'P', 'P', 'M',
             'synthetic://test', 'b' || repeat('0',63), '2026-cm'),
            ('2026', 'C00_NJ02', 'NJ TEST CMTE 2', 'TREASURER B',
             '2 NJ ST', 'TRENTON', 'NJ', '08608', 'U', 'O', 'Q',
             'synthetic://test', 'b' || repeat('0',63), '2026-cm'),
            ('2026', 'C00_TXOOS', 'OOS CMTE', 'TREASURER C',
             '1 TX ST', 'AUSTIN', 'TX', '78701', 'U', 'O', 'M',
             'synthetic://test', 'b' || repeat('0',63), '2026-cm');

            INSERT INTO derived.fraud_signal_observation (
                cycle, entity_kind, entity_id, signal_id, raw_value,
                severity, peer_bucket, peer_percentile, evidence_url
            ) VALUES
            ('2026', 'candidate', 'H8NJ_T01', 'candidate_namesakes', 2.0,
             3, 'state=NJ', 0.95, '/fec/test'),
            ('2026', 'committee', 'C00_NJ01', 'committee_name_collisions', 3,
             3, 'state=NJ', 0.93, '/fec/test'),
            ('2026', 'address', '1 NJ ST|NEWARK|NJ|07102', 'committee_address_clusters', 4,
             4, 'state=NJ', 0.99, '/fec/test'),
            -- Out-of-state observations that MUST NOT be counted in the NJ rollup
            ('2026', 'candidate', 'H8TX_OOS', 'candidate_namesakes', 5.0,
             3, 'state=TX', 0.99, '/fec/test'),
            ('2026', 'address', '1 TX ST|AUSTIN|TX|78701', 'committee_address_clusters', 9,
             4, 'state=TX', 0.99, '/fec/test')
            ON CONFLICT (cycle, entity_kind, entity_id, signal_id)
                DO UPDATE SET raw_value = EXCLUDED.raw_value;
        """)
    conn.commit()


class TestStateSummaryViewShape:
    def test_view_columns_match_typescript_contract(
        self, cross_pillar_db: psycopg.Connection,
    ) -> None:
        """The lib/types.ts NjCivicIntegritySummary interface lists 11
        fields. The view must expose all of them so the TS layer can
        rely on Number() + String() coercion."""
        with cross_pillar_db.cursor() as cur:
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema='derived'
                  AND table_name='v_nj_civic_integrity_state_summary'
                ORDER BY ordinal_position
            """)
            cols = [r[0] for r in cur.fetchall()]
        expected = {
            "cycle",
            "n_candidates_total", "n_candidates_with_signals",
            "max_candidate_risk_score",
            "n_committees_total", "n_committees_with_signals",
            "max_committee_risk_score",
            "n_addresses_with_signals", "max_address_risk_score",
            "total_nj_entities_with_signals", "max_nj_risk_score",
        }
        assert set(cols) == expected, (
            f"v_nj_civic_integrity_state_summary column drift: "
            f"missing {expected - set(cols)}, "
            f"unexpected {set(cols) - expected}"
        )


class TestStateSummaryAggregation:
    def test_nj_only_rollup(
        self, cross_pillar_db: psycopg.Connection,
    ) -> None:
        """Only NJ-keyed entities should appear in the aggregate. The
        out-of-state seeds (TX) must NOT be counted."""
        _seed_synthetic_nj_substrate(cross_pillar_db)
        with cross_pillar_db.cursor() as cur:
            cur.execute("""
                SELECT
                    n_candidates_total, n_candidates_with_signals,
                    n_committees_total, n_committees_with_signals,
                    n_addresses_with_signals,
                    total_nj_entities_with_signals
                FROM derived.v_nj_civic_integrity_state_summary
                WHERE cycle = '2026'
            """)
            row = cur.fetchone()
        assert row is not None, "v_nj_civic_integrity_state_summary empty for cycle 2026"
        n_cand_total, n_cand_signals, n_cmte_total, n_cmte_signals, \
            n_addr_signals, total = row
        # 2 NJ candidates total; 1 with a firing signal (the other has none)
        assert n_cand_total == 2, f"NJ candidate total wrong: {n_cand_total}"
        assert n_cand_signals == 1, f"NJ candidate-with-signals wrong: {n_cand_signals}"
        # 2 NJ committees total; 1 with a firing signal
        assert n_cmte_total == 2, f"NJ committee total wrong: {n_cmte_total}"
        assert n_cmte_signals == 1, f"NJ committee-with-signals wrong: {n_cmte_signals}"
        # 1 NJ address-cluster with a firing signal
        assert n_addr_signals == 1, f"NJ address-with-signals wrong: {n_addr_signals}"
        # Aggregate headline = 1 + 1 + 1 = 3
        assert total == 3, f"total_nj_entities_with_signals wrong: {total}"

    def test_address_filter_uses_split_part(
        self, cross_pillar_db: psycopg.Connection,
    ) -> None:
        """The address-kind filter is SPLIT_PART(entity_id, '|', 3) = 'NJ'.
        The TX address ('1 TX ST|AUSTIN|TX|78701') must NOT count."""
        _seed_synthetic_nj_substrate(cross_pillar_db)
        with cross_pillar_db.cursor() as cur:
            cur.execute("""
                SELECT n_addresses_with_signals
                FROM derived.v_nj_civic_integrity_state_summary
                WHERE cycle='2026'
            """)
            n = cur.fetchone()[0]
        assert n == 1, (
            f"NJ address-cluster filter did not exclude TX entity: got {n}"
        )

    def test_max_score_is_max_across_kinds(
        self, cross_pillar_db: psycopg.Connection,
    ) -> None:
        """max_nj_risk_score must be GREATEST() across the three per-kind
        maxes. The address signal seeded above (severity=4 peer=0.99) is
        the highest-severity NJ signal, so the headline max should be
        non-zero and >= the per-kind candidate/committee maxes."""
        _seed_synthetic_nj_substrate(cross_pillar_db)
        with cross_pillar_db.cursor() as cur:
            cur.execute("""
                SELECT
                    max_candidate_risk_score::FLOAT8,
                    max_committee_risk_score::FLOAT8,
                    max_address_risk_score::FLOAT8,
                    max_nj_risk_score::FLOAT8
                FROM derived.v_nj_civic_integrity_state_summary
                WHERE cycle='2026'
            """)
            row = cur.fetchone()
        assert row is not None
        cand_max, cmte_max, addr_max, headline = row
        assert headline == max(cand_max, cmte_max, addr_max), (
            f"headline max={headline} != GREATEST({cand_max}, {cmte_max}, {addr_max})"
        )
        assert headline > 0, "headline must be > 0 when any signal fires"

    def test_treasurers_excluded_from_rollup(
        self, cross_pillar_db: psycopg.Connection,
    ) -> None:
        """Treasurer entities are intentionally NOT counted in the
        state-level rollup (a single treasurer can serve both NJ and
        non-NJ committees). The view must not surface a treasurer
        column or include treasurer signals in candidate/committee
        counts."""
        with cross_pillar_db.cursor() as cur:
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema='derived'
                  AND table_name='v_nj_civic_integrity_state_summary'
                  AND column_name LIKE '%treasurer%'
            """)
            assert cur.fetchall() == [], (
                "Treasurer columns must NOT appear in the state-level "
                "rollup (multi-state treasurers create dedup ambiguity)"
            )


class TestStateSummaryEmpty:
    def test_unloaded_cycle_returns_no_rows(
        self, cross_pillar_db: psycopg.Connection,
    ) -> None:
        """A cycle with no NJ-keyed FEC data must NOT appear in the
        view at all (substrate-honest: don't fabricate empty rows that
        the UI would treat as 'no signals' and render an all-zeros
        callout)."""
        with cross_pillar_db.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM derived.v_nj_civic_integrity_state_summary
                WHERE cycle='1980'
            """)
            assert cur.fetchone() is None
