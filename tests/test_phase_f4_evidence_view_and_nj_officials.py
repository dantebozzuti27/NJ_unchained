"""Live-PG tests for migration 088 + seed 020.

VISION_2026 Pillar 2 (civic integrity) -- Phase F-UX work items F4 + F6/F7
substrate. The substrate this module tests:

    * ref.fraud_signal_evidence_url_template -- per-signal_id upstream-verify
      URL template registry (17 rows, FK to derived.fraud_signal_config).
    * derived.v_entity_fraud_evidence -- canonical join from observation
      to rendered plain-English + citation + severity precedent + display
      metadata + NJ relevance + upstream-verify URL.
    * derived.v_nj_federal_officials -- curated NJ federal incumbent
      roster (cand_office_st=NJ, cand_office IN (S,H), cand_ici=I,
      cand_status=C).

What this module pins:
    * Coverage: every fraud_signal_config row has a corresponding URL
      template row.
    * URL template format: every url_template starts with https:// and
      passes the length check; every upstream_source is in the whitelist.
    * v_entity_fraud_evidence column shape (the UI consumes this set).
    * Plain-English template substitution leaves NO {{...}} residue
      after rendering -- if a template references a token the view
      doesn't substitute, the test fails LOUDLY.
    * Upstream-verify URL substitution leaves no {{...}} residue.
    * is_nj column correctness across all four entity_kinds.
    * v_nj_federal_officials filter: only NJ federal incumbents
      (cand_office_st=NJ, cand_office IN (S,H), cand_ici=I, cand_status=C).
    * v_nj_federal_officials Senate-first ordering.
    * Score aggregation: officials with no firing signals carry
      risk_score=0 and n_signals=0 (substrate-honesty: green-check
      rendering, not silent absence).
    * formula_version stamping on every new row.
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


# EXPECTED_FORMULA_VERSION (singular): the BASE formula_version used by
# synthetic-data setup inside the table-contract tests (insert/select
# tests that fabricate rows for FK / CHECK / type validation, not
# seed-completeness assertions).
EXPECTED_FORMULA_VERSION = "2.2.0-fraud-evidence-view-v1"

# EXPECTED_FORMULA_VERSIONS (plural set): all formula_versions present
# in seed 020 (URL templates). The original 17 signals landed under
# 2.2.0-fraud-evidence-view-v1; entity_on_leie_strict_address (mig 092
# / seed 021) under 2.3.0-fraud-strict-address-v1; nj_state_candidate_on_leie
# (mig 098 / seed 023) under 2.7.1-fraud-nj-state-candidate-on-leie-v1.
EXPECTED_FORMULA_VERSIONS = frozenset({
    "2.2.0-fraud-evidence-view-v1",                # original 17 URL templates
    "2.3.0-fraud-strict-address-v1",               # entity_on_leie_strict_address
    "2.7.1-fraud-nj-state-candidate-on-leie-v1",   # nj_state_candidate_on_leie
    "2.8.1-fraud-provider-excluded-billing-v1",    # provider_excluded_billing
    "2.8.5-fraud-state-excluded-provider-billing-v1",  # state_excluded_provider_billing
    "2.8.6-fraud-opioid-prescribing-outlier-v1",   # opioid_prescribing_outlier
    "2.8.7-fraud-services-per-beneficiary-outlier-v1",  # services_per_beneficiary_outlier
    "2.8.9-fraud-provider-excluded-billing-partb-v1",  # provider_excluded_billing_partb
    "2.9.0-fraud-name-resolved-excluded-provider-billing-v1",  # name_resolved_excluded_provider_billing
    "2.9.1-fraud-excluded-provider-received-open-payments-v1",  # excluded_provider_received_open_payments
})

# All 20 fraud signals seeded across migrations 060-066, 092, 098, 101. Every
# one MUST have a URL-template row -- the substrate is incomplete otherwise.
EXPECTED_SIGNAL_IDS: frozenset[str] = frozenset({
    "candidate_no_pcc",
    "candidate_broken_pcc",
    "candidate_multiple_pccs",
    "candidate_namesakes",
    "committee_name_collisions",
    "committee_address_clusters",
    "treasurer_concentration",
    "treasurer_is_candidate",
    "entity_on_leie",
    "entity_on_leie_strict_address",  # mig 092 / seed 021
    "donor_on_leie",
    "candidate_funded_by_excluded_donors",
    "entity_excluded_via_sam_uei",
    "donor_on_sam",
    "candidate_funded_by_sam_excluded_donors",
    "entity_funded_and_excluded",
    "candidate_funded_by_nj_contractor_employees",
    "donor_employed_by_nj_contractor",
    "nj_state_candidate_on_leie",      # mig 098 / seed 023
    "provider_excluded_billing",       # mig 101 / seed 041
    "state_excluded_provider_billing", # mig 105 / seed 043
    "opioid_prescribing_outlier",      # mig 106 / seed 044
    "services_per_beneficiary_outlier",  # mig 107 / seed 045
    "provider_excluded_billing_partb",  # mig 109 / seed 046
    "name_resolved_excluded_provider_billing",  # mig 110 / seed 048
    "excluded_provider_received_open_payments",  # mig 111 / seed 049
})


@pytest.fixture
def evidence_db(live_pg: psycopg.Connection) -> psycopg.Connection:
    """Cleanly-migrated DB; raw tables empty, fraud_signal_config seeded."""
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


def _seed_fec_synthetic(conn: psycopg.Connection) -> None:
    """Insert a 4-candidate + 4-committee NJ-leaning synthetic FEC sample.

    Designed to exercise:
      * 1 NJ Senate incumbent (Booker analog), 1 NJ Senate non-incumbent
      * 1 NJ House incumbent, 1 NJ House non-incumbent
      * 1 TX House incumbent (must NOT show in v_nj_federal_officials)
      * Each NJ-incumbent paired with a registered committee at NJ address
      * 1 committee at a non-NJ (TX) address (must read is_nj=FALSE)
    """
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO raw.fec_candidate (
                cycle, cand_id, cand_name, cand_office, cand_office_st,
                cand_office_district, cand_pty_affiliation, cand_ici,
                cand_status, cand_election_yr, cand_pcc, cand_st1, cand_st2,
                cand_city, cand_st, cand_zip, source_url, source_sha256,
                source_vintage
            ) VALUES
            ('2024', 'S4NJ00185', 'BOOKER, CORY A.', 'S', 'NJ', '00',
             'DEM', 'I', 'C', 2026, 'C00554962',
             '102 PARK AVE', NULL, 'NEWARK', 'NJ', '07102',
             'https://www.fec.gov/files/bulk-downloads/2024/cn24.zip',
             '0000000000000000000000000000000000000000000000000000000000000001',
             '2024-cn'),
            ('2024', 'S0NJ99999', 'CHALLENGER, NJ SEN', 'S', 'NJ', '00',
             'REP', 'C', 'C', 2024, NULL,
             '1 CHALLENGER WAY', NULL, 'TRENTON', 'NJ', '08608',
             'https://www.fec.gov/files/bulk-downloads/2024/cn24.zip',
             '0000000000000000000000000000000000000000000000000000000000000001',
             '2024-cn'),
            ('2024', 'H8NJ11142', 'SHERRILL, MIKIE', 'H', 'NJ', '11',
             'DEM', 'I', 'C', 2024, 'C00633774',
             '8 MOUNTAIN VIEW BLVD', NULL, 'WAYNE', 'NJ', '07470',
             'https://www.fec.gov/files/bulk-downloads/2024/cn24.zip',
             '0000000000000000000000000000000000000000000000000000000000000001',
             '2024-cn'),
            ('2024', 'H0NJ11999', 'CHALLENGER, NJ HOUSE', 'H', 'NJ', '11',
             'REP', 'C', 'C', 2024, NULL,
             '1 CHALLENGER WAY', NULL, 'WAYNE', 'NJ', '07470',
             'https://www.fec.gov/files/bulk-downloads/2024/cn24.zip',
             '0000000000000000000000000000000000000000000000000000000000000001',
             '2024-cn'),
            ('2024', 'H2TX99999', 'TEXAS, REP', 'H', 'TX', '01',
             'REP', 'I', 'C', 2024, NULL,
             '1 ALAMO ST', NULL, 'AUSTIN', 'TX', '78701',
             'https://www.fec.gov/files/bulk-downloads/2024/cn24.zip',
             '0000000000000000000000000000000000000000000000000000000000000001',
             '2024-cn');
        """)

        cur.execute("""
            INSERT INTO raw.fec_committee (
                cycle, cmte_id, cmte_nm, tres_nm, cmte_st1, cmte_st2,
                cmte_city, cmte_st, cmte_zip, cmte_dsgn, cmte_tp,
                cmte_pty_affiliation, cmte_filing_freq, org_tp,
                connected_org_nm, cand_id, source_url, source_sha256,
                source_vintage
            ) VALUES
            ('2024', 'C00554962', 'CORY 2020', 'TREASURER, NJ',
             '102 PARK AVE', NULL, 'NEWARK', 'NJ', '071020001', 'P', 'P',
             'DEM', 'M', NULL, NULL, 'S4NJ00185',
             'https://www.fec.gov/files/bulk-downloads/2024/cm24.zip',
             '0000000000000000000000000000000000000000000000000000000000000002',
             '2024-cm'),
            ('2024', 'C00633774', 'SHERRILL FOR CONGRESS', 'TREASURER, NJ',
             '8 MOUNTAIN VIEW BLVD', NULL, 'WAYNE', 'NJ', '07470', 'P', 'H',
             'DEM', 'Q', NULL, NULL, 'H8NJ11142',
             'https://www.fec.gov/files/bulk-downloads/2024/cm24.zip',
             '0000000000000000000000000000000000000000000000000000000000000002',
             '2024-cm'),
            ('2024', 'C00111111', 'TEXAS PAC', 'AUSTIN, JIM',
             '1 ALAMO ST', NULL, 'AUSTIN', 'TX', '78701', 'U', 'O',
             NULL, 'M', NULL, NULL, NULL,
             'https://www.fec.gov/files/bulk-downloads/2024/cm24.zip',
             '0000000000000000000000000000000000000000000000000000000000000002',
             '2024-cm');
        """)
    conn.commit()


def _seed_observation(
    conn: psycopg.Connection,
    *,
    signal_id: str,
    entity_kind: str,
    entity_id: str,
    raw_value: float = 1.0,
    severity: int = 3,
    peer_bucket: str = "state=NJ",
    peer_percentile: float = 0.95,
) -> None:
    """Insert a single fraud_signal_observation row for testing."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO derived.fraud_signal_observation (
                cycle, entity_kind, entity_id, signal_id, raw_value,
                severity, peer_bucket, peer_percentile, evidence_url
            )
            VALUES ('2024', %s, %s, %s, %s, %s, %s, %s,
                    '/fec/metrics/' || %s || '?cycle=2024')
            ON CONFLICT (cycle, entity_kind, entity_id, signal_id)
                DO UPDATE SET
                  raw_value       = EXCLUDED.raw_value,
                  severity        = EXCLUDED.severity,
                  peer_bucket     = EXCLUDED.peer_bucket,
                  peer_percentile = EXCLUDED.peer_percentile,
                  evidence_url    = EXCLUDED.evidence_url
            """,
            (
                entity_kind, entity_id, signal_id, raw_value,
                severity, peer_bucket, peer_percentile, signal_id,
            ),
        )
    conn.commit()


# ===========================================================================
# Class A: ref.fraud_signal_evidence_url_template -- table contract
# ===========================================================================


class TestEvidenceUrlTemplateTableContract:
    def test_table_exists_with_expected_columns(
        self, evidence_db: psycopg.Connection,
    ) -> None:
        with evidence_db.cursor() as cur:
            cur.execute("""
                SELECT column_name, is_nullable
                FROM information_schema.columns
                WHERE table_schema='ref'
                  AND table_name='fraud_signal_evidence_url_template'
                ORDER BY ordinal_position
            """)
            cols = {r[0]: r[1] for r in cur.fetchall()}
        for required in (
            "signal_id",
            "url_template",
            "button_label",
            "upstream_source",
            "formula_version",
            "effective_date",
            "created_at",
            "updated_at",
        ):
            assert required in cols, f"missing column: {required!r}"
            assert cols[required] == "NO" or required in (
                "created_at", "updated_at",
            ), f"{required} should be NOT NULL"

    def test_signal_id_is_pk_and_fk_to_signal_config(
        self, evidence_db: psycopg.Connection,
    ) -> None:
        with evidence_db.cursor() as cur:
            cur.execute("""
                SELECT
                    tc.constraint_type,
                    ccu.table_schema || '.' || ccu.table_name AS referenced
                FROM information_schema.table_constraints tc
                LEFT JOIN information_schema.constraint_column_usage ccu
                       ON tc.constraint_name = ccu.constraint_name
                WHERE tc.table_schema='ref'
                  AND tc.table_name='fraud_signal_evidence_url_template'
            """)
            constraint_kinds = {row[0] for row in cur.fetchall()}
        assert "PRIMARY KEY" in constraint_kinds
        assert "FOREIGN KEY" in constraint_kinds

    def test_url_must_be_https(
        self, evidence_db: psycopg.Connection,
    ) -> None:
        import psycopg.errors as pg_errors

        with (
            evidence_db.cursor() as cur,
            pytest.raises(pg_errors.CheckViolation),
        ):
            cur.execute("""
                INSERT INTO ref.fraud_signal_evidence_url_template
                    (signal_id, url_template, button_label, upstream_source,
                     formula_version, effective_date)
                VALUES ('candidate_no_pcc', 'http://insecure.example/x',
                        'verify', 'FEC.gov',
                        %s, '2026-05-08')
                ON CONFLICT (signal_id) DO UPDATE
                  SET url_template = EXCLUDED.url_template
            """, (EXPECTED_FORMULA_VERSION,))


# ===========================================================================
# Class B: seed 020 coverage + URL well-formedness
# ===========================================================================


class TestSeed020Coverage:
    def test_every_signal_has_url_template(
        self, evidence_db: psycopg.Connection,
    ) -> None:
        """The substrate is INCOMPLETE if any signal lacks a verify URL."""
        with evidence_db.cursor() as cur:
            cur.execute("""
                SELECT cfg.signal_id
                FROM derived.fraud_signal_config cfg
                LEFT JOIN ref.fraud_signal_evidence_url_template eut
                       ON eut.signal_id = cfg.signal_id
                WHERE eut.signal_id IS NULL
            """)
            missing = [r[0] for r in cur.fetchall()]
        assert missing == [], (
            f"signals without URL template (substrate incomplete): {missing}"
        )

    def test_seeded_signal_ids_match_expected_set(
        self, evidence_db: psycopg.Connection,
    ) -> None:
        with evidence_db.cursor() as cur:
            cur.execute(
                "SELECT signal_id FROM ref.fraud_signal_evidence_url_template"
            )
            actual = {r[0] for r in cur.fetchall()}
        assert actual == EXPECTED_SIGNAL_IDS, (
            f"missing: {EXPECTED_SIGNAL_IDS - actual}, "
            f"extra: {actual - EXPECTED_SIGNAL_IDS}"
        )

    def test_every_url_starts_with_https(
        self, evidence_db: psycopg.Connection,
    ) -> None:
        with evidence_db.cursor() as cur:
            cur.execute("""
                SELECT signal_id, url_template
                FROM ref.fraud_signal_evidence_url_template
            """)
            for sid, url in cur.fetchall():
                assert url.startswith("https://"), (
                    f"signal {sid} has non-https URL: {url!r}"
                )

    def test_every_upstream_source_in_whitelist(
        self, evidence_db: psycopg.Connection,
    ) -> None:
        whitelist = {
            "FEC.gov", "OIG.gov", "SAM.gov", "USAspending.gov",
            "platform-internal", "NJ.gov", "CMS.gov",
        }
        with evidence_db.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT upstream_source
                FROM ref.fraud_signal_evidence_url_template
            """)
            actual = {r[0] for r in cur.fetchall()}
        assert actual <= whitelist, (
            f"upstream_source values outside whitelist: {actual - whitelist}"
        )

    def test_formula_version_stamped(
        self, evidence_db: psycopg.Connection,
    ) -> None:
        """Every formula_version present in seed 020 must be in the
        expected set (catches accidental drift; new signals under new
        versions must register in EXPECTED_FORMULA_VERSIONS).
        """
        with evidence_db.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT formula_version
                FROM ref.fraud_signal_evidence_url_template
            """)
            versions = {r[0] for r in cur.fetchall()}
        assert versions == EXPECTED_FORMULA_VERSIONS, (
            f"unexpected versions: only-in-actual="
            f"{versions - EXPECTED_FORMULA_VERSIONS} "
            f"only-in-expected="
            f"{EXPECTED_FORMULA_VERSIONS - versions}"
        )


# ===========================================================================
# Class C: derived.v_entity_fraud_evidence -- shape + token substitution
# ===========================================================================


class TestEvidenceViewShape:
    def test_view_columns_match_ui_contract(
        self, evidence_db: psycopg.Connection,
    ) -> None:
        """The UI's evidence-card type reads these exact columns."""
        with evidence_db.cursor() as cur:
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema='derived'
                  AND table_name='v_entity_fraud_evidence'
                ORDER BY ordinal_position
            """)
            cols = [r[0] for r in cur.fetchall()]
        for required in (
            "cycle", "entity_kind", "entity_id", "signal_id",
            "raw_value", "severity", "peer_bucket", "peer_percentile",
            "is_nj", "display_name",
            "office_code", "office_state", "office_district",
            "office_party", "office_incumbent_status",
            "rule_text", "citation_authority", "citation_section",
            "citation_url", "rendered_explanation",
            "severity_basis", "severity_precedent_url",
            "severity_precedent_summary",
            "upstream_verify_url", "upstream_verify_label",
            "upstream_source", "formula_version",
        ):
            assert required in cols, (
                f"v_entity_fraud_evidence missing column: {required}"
            )


class TestEvidenceViewTokenSubstitution:
    def test_rendered_explanation_has_no_placeholder_residue_for_candidate(
        self, evidence_db: psycopg.Connection,
    ) -> None:
        """A candidate observation's rendered_explanation must contain
        no {{...}} placeholder tokens after view rendering."""
        _seed_fec_synthetic(evidence_db)
        _seed_observation(
            evidence_db,
            signal_id="candidate_no_pcc",
            entity_kind="candidate",
            entity_id="H0NJ11999",
            raw_value=1.0,
            severity=1,
            peer_bucket="state=NJ",
            peer_percentile=0.97,
        )
        with evidence_db.cursor() as cur:
            cur.execute("""
                SELECT rendered_explanation
                FROM derived.v_entity_fraud_evidence
                WHERE entity_id = 'H0NJ11999'
            """)
            row = cur.fetchone()
        assert row is not None
        assert row[0] is not None and len(row[0]) > 0
        residue = re.findall(r"\{\{[^}]+\}\}", row[0])
        assert residue == [], (
            f"unsubstituted placeholders in rendered_explanation: {residue}; "
            f"full text: {row[0]!r}"
        )

    def test_upstream_verify_url_has_no_placeholder_residue(
        self, evidence_db: psycopg.Connection,
    ) -> None:
        _seed_fec_synthetic(evidence_db)
        _seed_observation(
            evidence_db,
            signal_id="candidate_no_pcc",
            entity_kind="candidate",
            entity_id="H0NJ11999",
        )
        with evidence_db.cursor() as cur:
            cur.execute("""
                SELECT upstream_verify_url
                FROM derived.v_entity_fraud_evidence
                WHERE entity_id = 'H0NJ11999'
            """)
            row = cur.fetchone()
        assert row is not None
        residue = re.findall(r"\{\{[^}]+\}\}", row[0])
        assert residue == [], (
            f"unsubstituted placeholders in upstream_verify_url: {residue}; "
            f"full URL: {row[0]!r}"
        )
        assert "H0NJ11999" in row[0]
        assert "2024" in row[0]

    def test_no_template_token_residue_for_any_seeded_signal(
        self, evidence_db: psycopg.Connection,
    ) -> None:
        """Fire every signal_id from seed 018 and assert NO {{...}} residue.

        Regression test for the production bug discovered during the 088
        deploy: rendered_explanation leaked literal "{{peer_bucket}}" and
        "{{entity_kind}}" because the chained REPLACE in the view didn't
        substitute those tokens. This test loops over EVERY signal in
        ref.fraud_signal_human_explanation, fires a synthetic observation
        on the appropriate entity_kind, and asserts that the rendered
        explanation contains no placeholder residue.
        """
        _seed_fec_synthetic(evidence_db)

        # Static signal_id -> entity_kind map (the substrate's emit-side
        # contract; see migrations 050-066 for the refresher INSERTs that
        # encode this). fraud_signal_config does NOT carry entity_kind --
        # it lives on the OBSERVATION row, written by the refresher.
        signal_to_kind: dict[str, str] = {
            # FEC-active (firing today)
            "candidate_no_pcc":            "candidate",
            "candidate_broken_pcc":        "candidate",
            "candidate_multiple_pccs":     "candidate",
            "candidate_namesakes":         "candidate",
            "committee_name_collisions":   "committee",
            "committee_address_clusters":  "address",
            "treasurer_concentration":     "treasurer",
            "treasurer_is_candidate":      "committee",
            # LEIE-bearing
            "entity_on_leie":                       "candidate",
            # mig 092 / seed 021: name + city + zip5 strict variant.
            # Same valid entity_kinds as the loose variant (candidate,
            # treasurer); the test picks "candidate" as the canonical
            # exemplar for token-substitution validation.
            "entity_on_leie_strict_address":        "candidate",
            "donor_on_leie":                        "donor",
            "candidate_funded_by_excluded_donors":  "candidate",
            # SAM-bearing (deferred to F8 ingest)
            "entity_excluded_via_sam_uei":               "committee",
            "donor_on_sam":                              "donor",
            "candidate_funded_by_sam_excluded_donors":   "candidate",
            # USAspending-bearing (deferred to F8 ingest)
            "entity_funded_and_excluded":                  "committee",
            "candidate_funded_by_nj_contractor_employees": "candidate",
            "donor_employed_by_nj_contractor":             "donor",
            # NJ-state-roster x LEIE cross-source (mig 098 / seed 023). The
            # only signal whose entity_kind is nj_state_candidate; the L3
            # evidence view's CASE-on-entity_kind branch resolves display_name
            # and is_nj from ref.nj_state_candidate (seeded by 022).
            "nj_state_candidate_on_leie":                  "nj_state_candidate",
            # CMS-Medicare x LEIE cross-source (mig 101 / seed 041). The only
            # signal whose entity_kind is provider; the L3 evidence view's
            # provider_meta CTE resolves display_name + is_nj from
            # raw.cms_partd_prescriber (empty here, so display_name falls back
            # to the NPI and is_nj=FALSE -- no token residue either way).
            "provider_excluded_billing":                   "provider",
            # NJ-Medicaid-exclusion x CMS-billing cross-source (mig 105 /
            # seed 043). Also entity_kind=provider; shares the provider eid
            # branch below.
            "state_excluded_provider_billing":             "provider",
            # CMS Part D opioid-prescribing distributional outlier (mig 106 /
            # seed 044). Also entity_kind=provider.
            "opioid_prescribing_outlier":                  "provider",
            # CMS Part B services-per-beneficiary distributional outlier
            # (mig 107 / seed 045). Also entity_kind=provider.
            "services_per_beneficiary_outlier":            "provider",
            # HHS-OIG-excluded provider x CMS Part B exact-NPI overlap
            # (mig 109 / seed 046). Also entity_kind=provider.
            "provider_excluded_billing_partb":             "provider",
            # Name-only LEIE exclusion resolved via NPPES to a billing NPI
            # (mig 110 / seed 048). Also entity_kind=provider.
            "name_resolved_excluded_provider_billing":     "provider",
            # HHS-OIG-excluded provider receiving CMS Open Payments transfers
            # of value (mig 111 / seed 049). Also entity_kind=provider.
            "excluded_provider_received_open_payments":    "provider",
        }

        with evidence_db.cursor() as cur:
            cur.execute(
                "SELECT signal_id FROM derived.fraud_signal_config "
                "ORDER BY signal_id"
            )
            seed_signal_ids = [r[0] for r in cur.fetchall()]

        missing_in_map = set(seed_signal_ids) - set(signal_to_kind)
        assert not missing_in_map, (
            "test signal_to_kind map missing entries for: "
            f"{sorted(missing_in_map)}"
        )

        for signal_id in seed_signal_ids:
            entity_kind = signal_to_kind[signal_id]
            if entity_kind == "candidate":
                eid = "H8NJ11142"
            elif entity_kind == "committee":
                eid = "C00633774"
            elif entity_kind == "treasurer":
                eid = "TREASURER, NJ"
            elif entity_kind == "address":
                eid = "1 PARK AVE|NEWARK|NJ|07102"
            elif entity_kind == "donor":
                eid = "SMITH, JOHN"
            elif entity_kind == "nj_state_candidate":
                # ref.nj_state_candidate (seed 022) has 10 candidates;
                # picking Sherrill as the canonical exemplar. The L3
                # evidence view's nj_state_meta CTE (mig 098) resolves
                # full_name + is_nj=TRUE from this candidate_id.
                eid = "NJ-STATE-SHERRILL-MIKIE-2025-GOVERNOR"
            elif entity_kind == "provider":
                # NPI-keyed. raw.cms_partd_prescriber is empty in this
                # fixture, so the provider_meta CTE finds no row and the
                # view falls back to entity_id for display_name.
                eid = "1234567893"
            else:
                pytest.fail(f"unexpected entity_kind in seed: {entity_kind!r}")

            _seed_observation(
                evidence_db,
                signal_id=signal_id,
                entity_kind=entity_kind,
                entity_id=eid,
                raw_value=42.5,
                severity=3,
                peer_bucket="state=NJ",
                peer_percentile=0.85,
            )

            with evidence_db.cursor() as cur:
                cur.execute(
                    """
                    SELECT rendered_explanation, upstream_verify_url
                    FROM derived.v_entity_fraud_evidence
                    WHERE signal_id = %s AND entity_id = %s
                    """,
                    (signal_id, eid),
                )
                row = cur.fetchone()

            assert row is not None, (
                f"no v_entity_fraud_evidence row for signal {signal_id}"
            )
            explanation, url = row

            assert explanation is not None, (
                f"signal {signal_id}: rendered_explanation is NULL"
            )
            tokens = re.findall(r"\{\{[^}]+\}\}", explanation)
            assert tokens == [], (
                f"signal {signal_id}: unsubstituted tokens "
                f"{tokens} in rendered_explanation: {explanation!r}"
            )

            assert url is not None, (
                f"signal {signal_id}: upstream_verify_url is NULL"
            )
            url_tokens = re.findall(r"\{\{[^}]+\}\}", url)
            assert url_tokens == [], (
                f"signal {signal_id}: unsubstituted tokens "
                f"{url_tokens} in upstream_verify_url: {url!r}"
            )

    def test_rendered_explanation_substitutes_entity_id_and_cycle(
        self, evidence_db: psycopg.Connection,
    ) -> None:
        _seed_fec_synthetic(evidence_db)
        _seed_observation(
            evidence_db,
            signal_id="candidate_broken_pcc",
            entity_kind="candidate",
            entity_id="H8NJ11142",
            severity=2,
        )
        with evidence_db.cursor() as cur:
            cur.execute("""
                SELECT rendered_explanation
                FROM derived.v_entity_fraud_evidence
                WHERE entity_id = 'H8NJ11142'
            """)
            row = cur.fetchone()
        assert row is not None
        assert "H8NJ11142" in row[0]
        assert "2024" in row[0]


# ===========================================================================
# Class D: derived.v_entity_fraud_evidence -- is_nj column correctness
# ===========================================================================


class TestEvidenceViewIsNj:
    def test_nj_candidate_is_nj_true(
        self, evidence_db: psycopg.Connection,
    ) -> None:
        _seed_fec_synthetic(evidence_db)
        _seed_observation(
            evidence_db,
            signal_id="candidate_no_pcc",
            entity_kind="candidate",
            entity_id="H8NJ11142",
            severity=1,
        )
        with evidence_db.cursor() as cur:
            cur.execute("""
                SELECT is_nj FROM derived.v_entity_fraud_evidence
                WHERE entity_id='H8NJ11142'
            """)
            row = cur.fetchone()
        assert row is not None and row[0] is True

    def test_tx_candidate_is_nj_false(
        self, evidence_db: psycopg.Connection,
    ) -> None:
        """A Texas candidate firing a signal MUST read is_nj=FALSE."""
        _seed_fec_synthetic(evidence_db)
        _seed_observation(
            evidence_db,
            signal_id="candidate_no_pcc",
            entity_kind="candidate",
            entity_id="H2TX99999",
            severity=1,
        )
        with evidence_db.cursor() as cur:
            cur.execute("""
                SELECT is_nj FROM derived.v_entity_fraud_evidence
                WHERE entity_id='H2TX99999'
            """)
            row = cur.fetchone()
        assert row is not None and row[0] is False

    def test_nj_committee_is_nj_true(
        self, evidence_db: psycopg.Connection,
    ) -> None:
        _seed_fec_synthetic(evidence_db)
        _seed_observation(
            evidence_db,
            signal_id="committee_name_collisions",
            entity_kind="committee",
            entity_id="C00633774",
            severity=3,
        )
        with evidence_db.cursor() as cur:
            cur.execute("""
                SELECT is_nj FROM derived.v_entity_fraud_evidence
                WHERE entity_id='C00633774'
            """)
            row = cur.fetchone()
        assert row is not None and row[0] is True

    def test_tx_committee_is_nj_false(
        self, evidence_db: psycopg.Connection,
    ) -> None:
        _seed_fec_synthetic(evidence_db)
        _seed_observation(
            evidence_db,
            signal_id="committee_name_collisions",
            entity_kind="committee",
            entity_id="C00111111",
            severity=3,
        )
        with evidence_db.cursor() as cur:
            cur.execute("""
                SELECT is_nj FROM derived.v_entity_fraud_evidence
                WHERE entity_id='C00111111'
            """)
            row = cur.fetchone()
        assert row is not None and row[0] is False

    def test_nj_address_is_nj_true(
        self, evidence_db: psycopg.Connection,
    ) -> None:
        """Address entity_ids encode state in token 3 of address|city|state|zip5."""
        _seed_observation(
            evidence_db,
            signal_id="committee_address_clusters",
            entity_kind="address",
            entity_id="102 PARK AVE|NEWARK|NJ|07102",
            severity=4,
            raw_value=5.0,
        )
        with evidence_db.cursor() as cur:
            cur.execute("""
                SELECT is_nj FROM derived.v_entity_fraud_evidence
                WHERE entity_id LIKE '102 PARK AVE|%'
            """)
            row = cur.fetchone()
        assert row is not None and row[0] is True

    def test_non_nj_address_is_nj_false(
        self, evidence_db: psycopg.Connection,
    ) -> None:
        _seed_observation(
            evidence_db,
            signal_id="committee_address_clusters",
            entity_kind="address",
            entity_id="1 ALAMO ST|AUSTIN|TX|78701",
            severity=4,
            raw_value=5.0,
        )
        with evidence_db.cursor() as cur:
            cur.execute("""
                SELECT is_nj FROM derived.v_entity_fraud_evidence
                WHERE entity_id LIKE '1 ALAMO ST|%'
            """)
            row = cur.fetchone()
        assert row is not None and row[0] is False

    def test_nj_treasurer_is_nj_true(
        self, evidence_db: psycopg.Connection,
    ) -> None:
        """Treasurer NJ-relevance flips TRUE if they treasure any NJ committee."""
        _seed_fec_synthetic(evidence_db)
        _seed_observation(
            evidence_db,
            signal_id="treasurer_concentration",
            entity_kind="treasurer",
            entity_id="TREASURER, NJ",
            severity=3,
            raw_value=2.0,
        )
        with evidence_db.cursor() as cur:
            cur.execute("""
                SELECT is_nj FROM derived.v_entity_fraud_evidence
                WHERE entity_id='TREASURER, NJ'
            """)
            row = cur.fetchone()
        assert row is not None and row[0] is True


# ===========================================================================
# Class E: derived.v_entity_fraud_evidence -- candidate office context
# ===========================================================================


class TestEvidenceViewOfficeContext:
    def test_candidate_office_metadata_populated(
        self, evidence_db: psycopg.Connection,
    ) -> None:
        _seed_fec_synthetic(evidence_db)
        _seed_observation(
            evidence_db,
            signal_id="candidate_no_pcc",
            entity_kind="candidate",
            entity_id="H8NJ11142",
            severity=1,
        )
        with evidence_db.cursor() as cur:
            cur.execute("""
                SELECT display_name, office_code, office_state,
                       office_district, office_party, office_incumbent_status
                FROM derived.v_entity_fraud_evidence
                WHERE entity_id='H8NJ11142'
            """)
            row = cur.fetchone()
        assert row is not None
        name, code, state, district, party, ici = row
        assert name == "SHERRILL, MIKIE"
        assert code == "H"
        assert state == "NJ"
        assert district == "11"
        assert party == "DEM"
        assert ici == "I"

    def test_treasurer_office_metadata_null(
        self, evidence_db: psycopg.Connection,
    ) -> None:
        """Treasurer rows must NOT carry office metadata; the UI keys on
        office_code IS NULL to suppress the office-context badge."""
        _seed_fec_synthetic(evidence_db)
        _seed_observation(
            evidence_db,
            signal_id="treasurer_concentration",
            entity_kind="treasurer",
            entity_id="TREASURER, NJ",
            severity=3,
        )
        with evidence_db.cursor() as cur:
            cur.execute("""
                SELECT office_code, office_state, office_party
                FROM derived.v_entity_fraud_evidence
                WHERE entity_id='TREASURER, NJ'
            """)
            row = cur.fetchone()
        assert row is not None
        assert row[0] is None
        assert row[1] is None
        assert row[2] is None


# ===========================================================================
# Class F: derived.v_nj_federal_officials -- filter + ordering
# ===========================================================================


class TestNjFederalOfficialsView:
    def test_filters_to_nj_federal_incumbents_only(
        self, evidence_db: psycopg.Connection,
    ) -> None:
        """Only NJ incumbents with office IN (S,H) and status=C survive."""
        _seed_fec_synthetic(evidence_db)
        with evidence_db.cursor() as cur:
            cur.execute("""
                SELECT entity_id, office_code, incumbent_status
                FROM derived.v_nj_federal_officials
                WHERE cycle='2024'
            """)
            rows = cur.fetchall()
        ids = {r[0] for r in rows}
        assert ids == {"S4NJ00185", "H8NJ11142"}, (
            f"v_nj_federal_officials returned wrong set: {ids}"
        )
        for _, code, ici in rows:
            assert code in ("S", "H")
            assert ici == "I"

    def test_excludes_non_nj_incumbents(
        self, evidence_db: psycopg.Connection,
    ) -> None:
        """Texas incumbent must NOT appear (substrate-honesty: NJ scope)."""
        _seed_fec_synthetic(evidence_db)
        with evidence_db.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM derived.v_nj_federal_officials
                WHERE entity_id='H2TX99999'
            """)
            assert cur.fetchone() is None

    def test_excludes_nj_challengers(
        self, evidence_db: psycopg.Connection,
    ) -> None:
        """NJ candidates with cand_ici='C' must not appear (incumbents only)."""
        _seed_fec_synthetic(evidence_db)
        with evidence_db.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM derived.v_nj_federal_officials
                WHERE entity_id IN ('S0NJ99999', 'H0NJ11999')
            """)
            assert cur.fetchone() is None

    def test_senate_first_ordering(
        self, evidence_db: psycopg.Connection,
    ) -> None:
        """View ORDER BY puts Senate before House (S DESC after H lex)."""
        _seed_fec_synthetic(evidence_db)
        with evidence_db.cursor() as cur:
            cur.execute("""
                SELECT office_code FROM derived.v_nj_federal_officials
                WHERE cycle='2024'
            """)
            offices = [r[0] for r in cur.fetchall()]
        assert offices == ["S", "H"], (
            f"v_nj_federal_officials ordering broken: {offices}"
        )

    def test_no_signals_renders_zero_score(
        self, evidence_db: psycopg.Connection,
    ) -> None:
        """An incumbent with no firing signals must read risk_score=0
        (the UI uses risk_score=0 to render a green check)."""
        _seed_fec_synthetic(evidence_db)
        with evidence_db.cursor() as cur:
            cur.execute("""
                SELECT risk_score, n_signals_fired
                FROM derived.v_nj_federal_officials
                WHERE entity_id='S4NJ00185'
            """)
            row = cur.fetchone()
        assert row is not None
        assert row[0] == 0
        assert row[1] == 0

    def test_office_label_human_readable(
        self, evidence_db: psycopg.Connection,
    ) -> None:
        """office_label maps S->'U.S. Senator', H->'U.S. Representative'."""
        _seed_fec_synthetic(evidence_db)
        with evidence_db.cursor() as cur:
            cur.execute("""
                SELECT entity_id, office_label
                FROM derived.v_nj_federal_officials
                WHERE cycle='2024'
                ORDER BY entity_id
            """)
            rows = dict(cur.fetchall())
        assert rows["S4NJ00185"] == "U.S. Senator"
        assert rows["H8NJ11142"] == "U.S. Representative"


# ===========================================================================
# Class G: derived.v_nj_federal_officials -- tenure-aware dedup (mig 090)
#
# Real FEC data has misfilers: candidates who self-declare ici='I' on Form 2
# but are not the actual sitting incumbent. The most extreme real-world case
# in cycle 2026 is NJ-11 where both Sherrill (true sitting Rep, ici='I'
# status='N' because she's running for governor not re-election) and Mejia
# (challenger, ici='I' status='C' -- misfiled) appear.
#
# Migration 090 disambiguates by counting prior cycles where the cand_id
# ran as a true incumbent (ici='I' AND status='C'). Pin that behavior.
# ===========================================================================


def _seed_tenure_disambiguation_scenario(conn: psycopg.Connection) -> None:
    """NJ-11, cycle 2026: tenure-collision scenario.

    Plants:
      - cycle 2020: Sherrill ici='I' status='C' (true incumbent, ran)
      - cycle 2022: Sherrill ici='I' status='C' (true incumbent, ran)
      - cycle 2024: Sherrill ici='I' status='C' (true incumbent, ran)
      - cycle 2026: Sherrill ici='I' status='N' (sitting but not running)
      - cycle 2026: Mejia    ici='I' status='C' (challenger, MISFILED 'I')

    Plus an ici='I' status='C' Senate seed for ordering tests. Plus a
    NJ-3 newcomer scenario (Conaway analog, replaces a moved-up
    representative): cycle 2026 ici='I' status='C' with prior=0.
    """
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO raw.fec_candidate (
                cycle, cand_id, cand_name, cand_office, cand_office_st,
                cand_office_district, cand_pty_affiliation, cand_ici,
                cand_status, cand_election_yr, cand_pcc, cand_st1, cand_st2,
                cand_city, cand_st, cand_zip, source_url, source_sha256,
                source_vintage
            ) VALUES
            ('2020', 'H8NJ11142', 'SHERRILL, MIKIE', 'H', 'NJ', '11',
             'DEM', 'I', 'C', 2020, 'C00633774',
             '8 MOUNTAIN VIEW BLVD', NULL, 'WAYNE', 'NJ', '07470',
             'synthetic://test', 'aa', '2020-cn'),
            ('2022', 'H8NJ11142', 'SHERRILL, MIKIE', 'H', 'NJ', '11',
             'DEM', 'I', 'C', 2022, 'C00633774',
             '8 MOUNTAIN VIEW BLVD', NULL, 'WAYNE', 'NJ', '07470',
             'synthetic://test', 'aa', '2022-cn'),
            ('2024', 'H8NJ11142', 'SHERRILL, MIKIE', 'H', 'NJ', '11',
             'DEM', 'I', 'C', 2024, 'C00633774',
             '8 MOUNTAIN VIEW BLVD', NULL, 'WAYNE', 'NJ', '07470',
             'synthetic://test', 'aa', '2024-cn'),
            ('2026', 'H8NJ11142', 'SHERRILL, MIKIE', 'H', 'NJ', '11',
             'DEM', 'I', 'N', 2026, 'C00633774',
             '8 MOUNTAIN VIEW BLVD', NULL, 'WAYNE', 'NJ', '07470',
             'synthetic://test', 'aa', '2026-cn'),
            ('2026', 'H6NJ11286', 'MEJIA, ANALILIA', 'H', 'NJ', '11',
             'DEM', 'I', 'C', 2026, NULL,
             '1 CHALLENGER WAY', NULL, 'VERONA', 'NJ', '07044',
             'synthetic://test', 'aa', '2026-cn'),
            -- NJ-3, cycle 2026: newcomer ici='I' status='C', no prior cycles
            ('2026', 'H4NJ03080', 'CONAWAY, HERB MD', 'H', 'NJ', '03',
             'DEM', 'I', 'C', 2026, NULL,
             '1 STATE HOUSE', NULL, 'TRENTON', 'NJ', '08608',
             'synthetic://test', 'aa', '2026-cn'),
            -- US Senate, cycle 2026: both senators (Booker has prior, Kim is new)
            ('2020', 'S4NJ00185', 'BOOKER, CORY A.', 'S', 'NJ', '00',
             'DEM', 'I', 'C', 2020, 'C00554962',
             '102 PARK AVE', NULL, 'NEWARK', 'NJ', '07102',
             'synthetic://test', 'aa', '2020-cn'),
            ('2026', 'S4NJ00185', 'BOOKER, CORY A.', 'S', 'NJ', '00',
             'DEM', 'I', 'C', 2026, 'C00554962',
             '102 PARK AVE', NULL, 'NEWARK', 'NJ', '07102',
             'synthetic://test', 'aa', '2026-cn'),
            ('2026', 'S4NJ00466', 'KIM, ANDY', 'S', 'NJ', '00',
             'DEM', 'I', 'C', 2030, NULL,
             '1 SENATE WAY', NULL, 'MOORESTOWN', 'NJ', '08057',
             'synthetic://test', 'aa', '2026-cn');
        """)
    conn.commit()


class TestNjFederalOfficialsTenureDedup:
    def test_misfiler_does_not_beat_true_incumbent(
        self, evidence_db: psycopg.Connection,
    ) -> None:
        """For NJ-11 cycle 2026: must pick Sherrill (3 prior incumbent
        cycles, status='N') OVER Mejia (0 prior cycles, status='C').

        This is the substrate-honesty regression: pre-090 the view
        filtered on status='C' which dropped Sherrill and surfaced
        the misfiling challenger Mejia.
        """
        _seed_tenure_disambiguation_scenario(evidence_db)
        with evidence_db.cursor() as cur:
            cur.execute("""
                SELECT entity_id, official_name, prior_incumbent_cycles
                FROM derived.v_nj_federal_officials
                WHERE cycle='2026' AND office_district='11'
            """)
            rows = cur.fetchall()
        assert len(rows) == 1, f"NJ-11 cycle 2026 must have exactly 1 row, got {rows}"
        assert rows[0][0] == "H8NJ11142", (
            f"NJ-11 cycle 2026 must be Sherrill, got {rows[0]}"
        )
        assert rows[0][2] == 3, (
            f"Sherrill must report prior_incumbent_cycles=3, got {rows[0][2]}"
        )

    def test_newcomer_with_no_prior_still_appears_when_alone(
        self, evidence_db: psycopg.Connection,
    ) -> None:
        """NJ-3 cycle 2026: Conaway has prior=0 (new appointee). With no
        competitor in the district he must surface. The 'NEW THIS CYCLE'
        UI badge keys off prior_incumbent_cycles=0."""
        _seed_tenure_disambiguation_scenario(evidence_db)
        with evidence_db.cursor() as cur:
            cur.execute("""
                SELECT entity_id, prior_incumbent_cycles
                FROM derived.v_nj_federal_officials
                WHERE cycle='2026' AND office_district='03'
            """)
            rows = cur.fetchall()
        assert rows == [("H4NJ03080", 0)]

    def test_senate_partition_returns_both_seats(
        self, evidence_db: psycopg.Connection,
    ) -> None:
        """Senate has 2 seats per state both at office_district='00'. The
        view must partition by cand_id (not district) for office='S' so
        that BOTH senators surface, not just one."""
        _seed_tenure_disambiguation_scenario(evidence_db)
        with evidence_db.cursor() as cur:
            cur.execute("""
                SELECT entity_id, prior_incumbent_cycles
                FROM derived.v_nj_federal_officials
                WHERE cycle='2026' AND office_code='S'
                ORDER BY entity_id
            """)
            rows = cur.fetchall()
        assert len(rows) == 2, (
            f"NJ Senate cycle 2026 must surface BOTH senators, got {rows}"
        )
        ids = {r[0] for r in rows}
        assert ids == {"S4NJ00185", "S4NJ00466"}
        # Booker has prior cycle (2020), Kim is new
        rows_by_id = dict(rows)
        assert rows_by_id["S4NJ00185"] >= 1
        assert rows_by_id["S4NJ00466"] == 0

    def test_status_n_incumbent_not_running_is_included(
        self, evidence_db: psycopg.Connection,
    ) -> None:
        """status='N' (not yet a candidate this cycle) must NOT be
        excluded if the candidate has prior incumbent cycles. This is
        the Sherrill-case: she's still serving but not running for
        re-election."""
        _seed_tenure_disambiguation_scenario(evidence_db)
        with evidence_db.cursor() as cur:
            cur.execute("""
                SELECT incumbent_status FROM derived.v_nj_federal_officials
                WHERE cycle='2026' AND entity_id='H8NJ11142'
            """)
            row = cur.fetchone()
        assert row is not None
        assert row[0] == "I"

    def test_prior_incumbent_cycles_column_exists_and_is_int(
        self, evidence_db: psycopg.Connection,
    ) -> None:
        """The view must expose prior_incumbent_cycles as INT4 so the
        TypeScript layer can rely on Number()."""
        with evidence_db.cursor() as cur:
            cur.execute("""
                SELECT data_type FROM information_schema.columns
                WHERE table_schema='derived'
                  AND table_name='v_nj_federal_officials'
                  AND column_name='prior_incumbent_cycles'
            """)
            row = cur.fetchone()
        assert row is not None, (
            "v_nj_federal_officials.prior_incumbent_cycles must exist"
        )
        assert row[0] == "integer", (
            f"prior_incumbent_cycles must be integer, got {row[0]}"
        )
