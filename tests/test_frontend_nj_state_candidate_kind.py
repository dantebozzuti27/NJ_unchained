"""Frontend-wiring contract tests for the new ``nj_state_candidate`` entity_kind.

Pillar 2 (civic integrity) Phase F8.5-frontend. These tests pin the
contract that the Next.js ``/risk/[kind]/[id]`` URL space depends on,
specifically for the entity_kind that was added by mig 098 + seed 023:

    * lib/queries.ts ``isValidKind('nj_state_candidate')`` returns true.
      We do not import the TS source here -- instead we pin the symmetric
      property at the database level: every entity_kind that the L1 CHECK
      constraint accepts is a valid URL-space kind, and the ``nj_state_candidate``
      kind is in that whitelist.

    * lib/queries.ts ``getEntityHeader({kind: 'nj_state_candidate', id, cycle})``
      reads ``derived.v_nj_state_candidates`` filtered by
      ``entity_id = :id AND election_year::text = :cycle`` and expects
      five columns: ``entity_id``, ``full_name`` (-> display_name),
      ``party`` (-> office_party), ``office_label`` (-> office_code),
      and the implicit ``election_year`` filter. We pin the existence
      and shape of those columns so a future schema change to the view
      breaks this test BEFORE it breaks the production /risk page.

    * The L3 ``derived.v_entity_fraud_evidence`` CASE-on-entity_kind
      branch (added by mig 098) resolves both ``display_name`` and
      ``is_nj`` for a synthetic ``nj_state_candidate`` observation. This
      is the exact code path the page uses when an evidence card exists.

These are LIVE-PG tests that bootstrap the full migration + seed chain,
synthesize a single observation against ref.nj_state_candidate (the
seed-022 cohort is loaded by the fixture; seed 023 wires the URL
template), and assert the join + CASE branches produce the rows the
page would render. No Python -> TypeScript import; the contract is
codified at the SQL boundary the TypeScript layer reads.
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


# Canonical exemplar from seed 022: 10 NJ gubernatorial candidates were
# seeded with election_year=2025; this one is the highest-name-recognition
# Democratic candidate (Mikie Sherrill, NJ-11 incumbent).
EXEMPLAR_CANDIDATE_ID = "NJ-STATE-SHERRILL-MIKIE-2025-GOVERNOR"
EXEMPLAR_FULL_NAME = "Mikie Sherrill"
EXEMPLAR_PARTY = "DEM"
EXEMPLAR_OFFICE_LABEL = "Governor of New Jersey"
EXEMPLAR_CYCLE = "2025"


@pytest.fixture
def fe_db(live_pg: "psycopg.Connection") -> "psycopg.Connection":
    """Cleanly-migrated DB with all seeds applied."""
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


# =============================================================================
# 1. URL-space validity contract: nj_state_candidate is in the L1 whitelist
# =============================================================================


def test_nj_state_candidate_in_l1_entity_kind_check(
    fe_db: "psycopg.Connection",
) -> None:
    """The fraud_signal_observation.entity_kind CHECK constraint must
    accept 'nj_state_candidate' -- this is what makes the URL path
    /risk/nj_state_candidate/<id> a valid surface in the first place.
    Asserted via pg_constraint inspection so a future constraint
    rewrite that drops the value fails this test loudly."""
    with fe_db.cursor() as cur:
        cur.execute(
            "SELECT pg_get_constraintdef(oid) "
            "FROM pg_constraint "
            "WHERE conname = 'fraud_signal_observation_entity_kind_check'"
        )
        row = cur.fetchone()
    assert row is not None, (
        "fraud_signal_observation_entity_kind_check constraint missing"
    )
    constraint_def = row[0]
    assert "'nj_state_candidate'" in constraint_def, (
        f"L1 entity_kind CHECK does NOT accept 'nj_state_candidate'. "
        f"Constraint: {constraint_def}"
    )


# =============================================================================
# 2. getEntityHeader query contract: derived.v_nj_state_candidates exposes
# the columns the new TS branch reads.
# =============================================================================


def test_v_nj_state_candidates_exposes_required_header_columns(
    fe_db: "psycopg.Connection",
) -> None:
    """The TS getEntityHeader('nj_state_candidate', ...) query reads
    five columns from derived.v_nj_state_candidates. Pin existence so
    a view rewrite that drops one of them fails this test BEFORE it
    breaks the prod /risk page."""
    required = {
        "entity_id",
        "full_name",
        "party",
        "office_label",
        "election_year",
    }
    with fe_db.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='derived' "
            "  AND table_name='v_nj_state_candidates'"
        )
        cols = {r[0] for r in cur.fetchall()}
    missing = required - cols
    assert not missing, (
        f"derived.v_nj_state_candidates missing required columns: {missing}"
    )


def test_get_entity_header_query_resolves_seed_022_exemplar(
    fe_db: "psycopg.Connection",
) -> None:
    """Mirror of the new TS query in lib/queries.ts getEntityHeader().
    A clean (no-signals) NJ state candidate URL must return the
    seed-022 row -- otherwise /risk/nj_state_candidate/<id> 404s
    even though the candidate is seeded."""
    with fe_db.cursor() as cur:
        cur.execute(
            """
            SELECT entity_id, full_name, party, office_label
            FROM derived.v_nj_state_candidates
            WHERE entity_id        = %s
              AND election_year::text = %s
            LIMIT 1
            """,
            (EXEMPLAR_CANDIDATE_ID, EXEMPLAR_CYCLE),
        )
        row = cur.fetchone()
    assert row is not None, (
        f"seed-022 candidate {EXEMPLAR_CANDIDATE_ID} not found via "
        f"v_nj_state_candidates with election_year::text={EXEMPLAR_CYCLE!r}"
    )
    entity_id, full_name, party, office_label = row
    assert entity_id == EXEMPLAR_CANDIDATE_ID
    assert full_name == EXEMPLAR_FULL_NAME
    assert party == EXEMPLAR_PARTY
    assert office_label == EXEMPLAR_OFFICE_LABEL


def test_get_entity_header_query_returns_no_row_for_unknown_id(
    fe_db: "psycopg.Connection",
) -> None:
    """Substrate-honesty: an unknown candidate_id MUST return zero rows
    so the TS branch returns null and the page renders the explicit
    'Entity not found' surface (NOT a half-populated header)."""
    with fe_db.cursor() as cur:
        cur.execute(
            """
            SELECT entity_id FROM derived.v_nj_state_candidates
            WHERE entity_id = %s AND election_year::text = %s
            """,
            ("NJ-STATE-NONEXISTENT-CANDIDATE-2025-GOVERNOR", EXEMPLAR_CYCLE),
        )
        rows = cur.fetchall()
    assert rows == []


def test_get_entity_header_query_isolates_by_election_year(
    fe_db: "psycopg.Connection",
) -> None:
    """A candidate seeded for election_year=2025 MUST NOT match a query
    for cycle='2027'. Cycle isolation is the same contract as the
    fraud-observation refresher (mig 098); the page-side query honors
    the same boundary."""
    with fe_db.cursor() as cur:
        cur.execute(
            """
            SELECT entity_id FROM derived.v_nj_state_candidates
            WHERE entity_id = %s AND election_year::text = %s
            """,
            (EXEMPLAR_CANDIDATE_ID, "2027"),
        )
        rows = cur.fetchall()
    assert rows == [], (
        f"cycle isolation broken: {EXEMPLAR_CANDIDATE_ID} (election_year=2025) "
        f"unexpectedly matched cycle='2027'"
    )


# =============================================================================
# 3. L3 evidence view: nj_state_meta CTE resolves display_name + is_nj
# =============================================================================


def _seed_synthetic_observation(
    conn: "psycopg.Connection",
    *,
    cycle: str,
    entity_id: str,
    signal_id: str = "nj_state_candidate_on_leie",
    severity: int = 5,
    raw_value: float = 1.0,
    peer_percentile: float = 0.95,
) -> None:
    """Insert one synthetic fraud_signal_observation row for the
    nj_state_candidate kind. Used to exercise the L3 evidence view's
    nj_state_meta CTE join (mig 098)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO derived.fraud_signal_observation (
                cycle, entity_kind, entity_id, signal_id,
                raw_value, severity, peer_bucket, peer_percentile,
                evidence_url, materialized_at
            ) VALUES (
                %s, 'nj_state_candidate', %s, %s,
                %s, %s, 'cycle=' || %s, %s,
                'https://oig.hhs.gov/exclusions/exclusions_list.asp',
                now()
            )
            ON CONFLICT (cycle, entity_kind, entity_id, signal_id)
            DO UPDATE SET
                raw_value         = EXCLUDED.raw_value,
                severity          = EXCLUDED.severity,
                peer_bucket       = EXCLUDED.peer_bucket,
                peer_percentile   = EXCLUDED.peer_percentile,
                materialized_at   = now()
            """,
            (cycle, entity_id, signal_id, raw_value, severity,
             cycle, peer_percentile),
        )
    conn.commit()


def test_l3_evidence_view_resolves_display_name_for_nj_state_candidate(
    fe_db: "psycopg.Connection",
) -> None:
    """The L3 view's CASE-on-entity_kind branch (added in mig 098) must
    set display_name from ref.nj_state_candidate.full_name when the
    observation's entity_kind is 'nj_state_candidate'."""
    _seed_synthetic_observation(
        fe_db,
        cycle=EXEMPLAR_CYCLE,
        entity_id=EXEMPLAR_CANDIDATE_ID,
    )
    with fe_db.cursor() as cur:
        cur.execute(
            """
            SELECT display_name, is_nj, entity_kind
            FROM   derived.v_entity_fraud_evidence
            WHERE  cycle       = %s
              AND  entity_kind = 'nj_state_candidate'
              AND  entity_id   = %s
              AND  signal_id   = 'nj_state_candidate_on_leie'
            """,
            (EXEMPLAR_CYCLE, EXEMPLAR_CANDIDATE_ID),
        )
        row = cur.fetchone()
    assert row is not None, (
        "v_entity_fraud_evidence did NOT return a row for the synthetic "
        f"nj_state_candidate observation ({EXEMPLAR_CANDIDATE_ID}). The "
        "nj_state_meta CTE / LEFT JOIN added by mig 098 may be broken."
    )
    display_name, is_nj, entity_kind = row
    assert display_name == EXEMPLAR_FULL_NAME, (
        f"display_name not resolved from ref.nj_state_candidate. "
        f"Expected {EXEMPLAR_FULL_NAME!r}, got {display_name!r}"
    )
    assert is_nj is True, (
        "is_nj must be TRUE for nj_state_candidate (the entire ref table "
        "is by construction NJ state-level). The L3 view's CASE branch "
        "for this kind is broken."
    )
    assert entity_kind == "nj_state_candidate"


def test_l3_evidence_view_returns_empty_for_clean_nj_state_candidate(
    fe_db: "psycopg.Connection",
) -> None:
    """Substrate-honesty: a NJ state candidate with NO firing signals
    MUST return zero rows from v_entity_fraud_evidence -- the page-
    side header path then falls back to getEntityHeader, which reads
    ref.nj_state_candidate directly. This dual-source pattern is the
    same one used by sitting-incumbent FEC candidates."""
    with fe_db.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) FROM derived.v_entity_fraud_evidence
            WHERE cycle = %s AND entity_kind = 'nj_state_candidate'
            """,
            (EXEMPLAR_CYCLE,),
        )
        (n,) = cur.fetchone()
    assert n == 0, (
        f"Expected zero firing signals for cycle={EXEMPLAR_CYCLE} "
        f"NJ state candidates on a fresh substrate; got {n}. The "
        "fraud_signal_observation table should be empty after "
        "migrations + seeds (no synthetic observation inserted)."
    )
