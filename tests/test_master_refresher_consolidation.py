"""Live-PG regression tests for migration 094.

VISION_2026 Pillar 2 (civic integrity) substrate hygiene. The substrate
this module pins:

    * derived.refresh_all_fraud_signal_observations -- the master fraud-
      signal orchestrator. After mig 094, invokes ALL 18 seeded signal
      refreshers; before mig 094, invoked only 8 of them.

What this module pins:
    * Coverage contract: every signal_id present in derived.fraud_signal_config
      MUST appear as a `derived.refresh_signal_*` (or `derived.refresh_*_observations`)
      invocation in the master function body. If a future signal is added to
      fraud_signal_config without wiring its refresher into the master, this
      test fails LOUDLY -- substrate-honesty rule 4 (no shadow code paths).
    * Empty-substrate safety: calling the master against a freshly-migrated
      database (no raw.fec_*, no raw.hhs_oig_leie, no raw.sam_gov_exclusion,
      no raw.usaspending_award) returns 0 cleanly without raising. Refreshers
      MUST be idempotent no-ops against empty substrate.
    * Per-tier coverage: 8 FEC-only + 4 LEIE + 3 SAM + 3 USAspending = 18
      signals. Drift between this expected map and fraud_signal_config fails
      the test.
    * Idempotency: calling the master twice in succession produces the same
      total return value.
    * Formula-version stamping: 2.5.0-master-refresher-consolidation-v1
      is registered in ref.formula_version with the expected effective_date.
"""

from __future__ import annotations

import re
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


EXPECTED_FORMULA_VERSION = "2.5.0-master-refresher-consolidation-v1"

# Substrate-coverage map: every signal_id MUST appear in
# fraud_signal_config AND have a matching invocation in the master
# function body. The map groups by substrate-dependency tier purely
# for documentation; the test asserts the UNION matches.
EXPECTED_SIGNAL_TO_REFRESHER = {
    # -------------------------------------------------------------
    # TIER 1: FEC-bulk-only structural (8)
    # -------------------------------------------------------------
    "treasurer_concentration": "derived.refresh_treasurer_concentration_observations",
    "candidate_no_pcc": "derived.refresh_candidate_no_pcc_observations",
    "candidate_broken_pcc": "derived.refresh_candidate_broken_pcc_observations",
    "candidate_multiple_pccs": "derived.refresh_candidate_multiple_pccs_observations",
    "committee_address_clusters": "derived.refresh_committee_address_clusters_observations",
    "committee_name_collisions": "derived.refresh_committee_name_collisions_observations",
    "candidate_namesakes": "derived.refresh_candidate_namesakes_observations",
    "treasurer_is_candidate": "derived.refresh_treasurer_is_candidate_observations",
    # -------------------------------------------------------------
    # TIER 2: LEIE-bearing (5 -- nj_state_candidate_on_leie added in mig 098)
    # -------------------------------------------------------------
    "entity_on_leie": "derived.refresh_signal_entity_on_leie",
    "entity_on_leie_strict_address":
        "derived.refresh_signal_entity_on_leie_strict_address",
    "donor_on_leie": "derived.refresh_signal_donor_on_leie",
    "candidate_funded_by_excluded_donors":
        "derived.refresh_signal_candidate_funded_by_excluded_donors",
    "nj_state_candidate_on_leie":
        "derived.refresh_signal_nj_state_candidate_on_leie",
    # -------------------------------------------------------------
    # TIER 3: SAM-bearing (3)
    # -------------------------------------------------------------
    "entity_excluded_via_sam_uei": "derived.refresh_signal_entity_excluded_via_sam_uei",
    "donor_on_sam": "derived.refresh_signal_donor_on_sam",
    "candidate_funded_by_sam_excluded_donors":
        "derived.refresh_signal_candidate_funded_by_sam_excluded_donors",
    # -------------------------------------------------------------
    # TIER 4: USAspending-bearing (3)
    # -------------------------------------------------------------
    "entity_funded_and_excluded":
        "derived.refresh_signal_entity_funded_and_excluded",
    "candidate_funded_by_nj_contractor_employees":
        "derived.refresh_signal_candidate_funded_by_nj_contractor_employees",
    "donor_employed_by_nj_contractor":
        "derived.refresh_signal_donor_employed_by_nj_contractor",
    # -------------------------------------------------------------
    # TIER 5: CMS-Medicare-bearing federal-exclusion (2 -- mig 101, 109)
    # -------------------------------------------------------------
    "provider_excluded_billing":
        "derived.refresh_signal_provider_excluded_billing",
    "provider_excluded_billing_partb":
        "derived.refresh_signal_provider_excluded_billing_partb",
    # -------------------------------------------------------------
    # TIER 6: NJ-state-exclusion-bearing (1 -- mig 105)
    # -------------------------------------------------------------
    "state_excluded_provider_billing":
        "derived.refresh_signal_state_excluded_provider_billing",
    # -------------------------------------------------------------
    # TIER 7: CMS-utilization peer-relative outliers (2 -- mig 106, 107)
    # -------------------------------------------------------------
    "opioid_prescribing_outlier":
        "derived.refresh_signal_opioid_prescribing_outlier",
    "services_per_beneficiary_outlier":
        "derived.refresh_signal_services_per_beneficiary_outlier",
    "antipsychotic_elderly_outlier":
        "derived.refresh_signal_antipsychotic_elderly_outlier",
    # -------------------------------------------------------------
    # TIER 8: NPPES identity-resolution recall (1 -- mig 110)
    # -------------------------------------------------------------
    "name_resolved_excluded_provider_billing":
        "derived.refresh_signal_name_resolved_excluded_provider_billing",
    # -------------------------------------------------------------
    # TIER 9: Open-Payments conflict-of-interest (1 -- mig 111)
    # -------------------------------------------------------------
    "excluded_provider_received_open_payments":
        "derived.refresh_signal_excluded_provider_received_open_payments",
}


@pytest.fixture
def master_db(live_pg: psycopg.Connection) -> psycopg.Connection:
    """Cleanly-migrated DB with all migrations + seeds applied."""
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


def _read_master_function_source(conn: psycopg.Connection) -> str:
    """Return the body text of derived.refresh_all_fraud_signal_observations."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT prosrc
            FROM   pg_proc p
            JOIN   pg_namespace n ON n.oid = p.pronamespace
            WHERE  n.nspname = 'derived'
              AND  p.proname = 'refresh_all_fraud_signal_observations'
        """)
        row = cur.fetchone()
    assert row is not None, (
        "derived.refresh_all_fraud_signal_observations not found"
    )
    return row[0]


# =============================================================================
# Coverage contract
# =============================================================================


def test_every_seeded_signal_appears_in_master(
    master_db: psycopg.Connection,
):
    """
    The master function body MUST reference the refresher function name
    for every signal_id present in fraud_signal_config. Drift here means
    a signal is registered but silently un-orchestrated.
    """
    body = _read_master_function_source(master_db)

    with master_db.cursor() as cur:
        cur.execute("""
            SELECT signal_id
            FROM   derived.fraud_signal_config
        """)
        seeded = {r[0] for r in cur.fetchall()}

    missing_from_map = seeded - set(EXPECTED_SIGNAL_TO_REFRESHER)
    extra_in_map = set(EXPECTED_SIGNAL_TO_REFRESHER) - seeded
    assert not missing_from_map, (
        f"signal_id seeded in fraud_signal_config but not in test's "
        f"expected map: {sorted(missing_from_map)}"
    )
    assert not extra_in_map, (
        f"signal_id in test's expected map but not seeded: "
        f"{sorted(extra_in_map)}"
    )

    missing_from_body = []
    for signal_id, expected_refresher in EXPECTED_SIGNAL_TO_REFRESHER.items():
        if expected_refresher not in body:
            missing_from_body.append((signal_id, expected_refresher))
    assert not missing_from_body, (
        "master function body is missing invocations for these "
        f"(signal_id, expected_refresher) pairs: {missing_from_body}"
    )


def test_master_invokes_at_least_18_refreshers(
    master_db: psycopg.Connection,
):
    """
    The master must contain at least 18 SELECT derived.refresh_*
    invocations (it can have more if future signals are added but never
    less than the current substrate footprint).
    """
    body = _read_master_function_source(master_db)
    invocations = re.findall(
        r"SELECT\s+derived\.refresh_\w+\s*\(",
        body,
        flags=re.IGNORECASE,
    )
    assert len(invocations) >= 18, (
        f"expected >= 18 refresher invocations in master body, "
        f"got {len(invocations)}: {invocations}"
    )


def test_fraud_signal_config_has_exactly_27_signals(
    master_db: psycopg.Connection,
):
    """
    Pin the current 27-signal taxonomy (18 pre-mig 098;
    nj_state_candidate_on_leie added 2026-05-12 -> 19;
    provider_excluded_billing added 2026-06-08 by mig 101 -> 20;
    state_excluded_provider_billing added 2026-06-09 by mig 105 -> 21;
    opioid_prescribing_outlier added 2026-06-09 by mig 106 -> 22;
    services_per_beneficiary_outlier added 2026-06-09 by mig 107 -> 23;
    provider_excluded_billing_partb added 2026-06-09 by mig 109 -> 24;
    name_resolved_excluded_provider_billing added 2026-06-09 by mig 110 -> 25;
    excluded_provider_received_open_payments added 2026-06-09 by mig 111 -> 26;
    antipsychotic_elderly_outlier added 2026-06-09 by mig 115 -> 27). If the
    count changes again, EXPECTED_SIGNAL_TO_REFRESHER MUST be updated alongside.
    """
    with master_db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM derived.fraud_signal_config")
        (n,) = cur.fetchone()
    assert n == 27, (
        f"fraud_signal_config has {n} rows; expected 27. If you added a "
        f"new signal, update EXPECTED_SIGNAL_TO_REFRESHER and re-run."
    )


# =============================================================================
# Empty-substrate safety
# =============================================================================


def test_master_against_empty_substrate_returns_zero(
    master_db: psycopg.Connection,
):
    """
    Calling the master against a freshly-migrated DB (no raw data) must
    return 0 without raising. Refreshers MUST tolerate empty raw tables.
    """
    with master_db.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_all_fraud_signal_observations(%s)",
            ('2024',),
        )
        (n,) = cur.fetchone()
    assert n == 0, (
        f"expected 0 observations on empty raw substrate, got {n}"
    )


def test_master_is_idempotent_on_empty_substrate(
    master_db: psycopg.Connection,
):
    """Re-running the master with no raw data must still return 0."""
    with master_db.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_all_fraud_signal_observations(%s)",
            ('2024',),
        )
        (first,) = cur.fetchone()
        cur.execute(
            "SELECT derived.refresh_all_fraud_signal_observations(%s)",
            ('2024',),
        )
        (second,) = cur.fetchone()
    assert first == second == 0, f"non-idempotent: {first} != {second}"


def test_master_does_not_leak_observations_from_other_cycles(
    master_db: psycopg.Connection,
):
    """Cycle isolation: refreshing 2024 must not delete 2022 observations."""
    with master_db.cursor() as cur:
        cur.execute("""
            INSERT INTO derived.fraud_signal_observation (
                cycle, entity_kind, entity_id, signal_id,
                raw_value, severity, peer_bucket, peer_percentile,
                evidence_url
            ) VALUES (
                '2022', 'candidate', 'H4NJ09999', 'candidate_no_pcc',
                1, 1, 'kind=candidate', 0.99, '/test/evidence'
            )
        """)
        master_db.commit()

        cur.execute(
            "SELECT derived.refresh_all_fraud_signal_observations(%s)",
            ('2024',),
        )

        cur.execute("""
            SELECT COUNT(*)
            FROM   derived.fraud_signal_observation
            WHERE  cycle = '2022'
        """)
        (n_2022,) = cur.fetchone()
    assert n_2022 == 1, (
        f"refreshing cycle 2024 leaked into cycle 2022: "
        f"{n_2022} rows remain (expected 1)"
    )


# =============================================================================
# Formula-version provenance
# =============================================================================


def test_formula_version_registered(master_db: psycopg.Connection):
    """The migration's formula_version is registered in ref.formula_version."""
    with master_db.cursor() as cur:
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
    desc_lower = desc.lower()
    assert "rewritten" in desc_lower and "fraud signal" in desc_lower, (
        f"description does not document the consolidation rewrite: {desc!r}"
    )


def test_master_function_comment_documents_consolidation(
    master_db: psycopg.Connection,
):
    """The function's pg_description must reflect the 26-signal scope."""
    with master_db.cursor() as cur:
        cur.execute("""
            SELECT obj_description(
                'derived.refresh_all_fraud_signal_observations(CHAR(4))'
                ::regprocedure,
                'pg_proc'
            )
        """)
        (comment,) = cur.fetchone()
    assert comment is not None, "function has no COMMENT ON"
    assert "26" in comment, (
        "function comment does not document the 26-signal scope: "
        f"{comment!r}"
    )
