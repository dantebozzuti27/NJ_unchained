-- ============================================================================
-- Migration: 105_fraud_state_excluded_provider_billing
--
-- FRAUD-F7 Phase-3 (signal slice): state_excluded_provider_billing.
--
-- THE SIGNAL
-- ----------
-- A provider on NJ's currently-active Medicaid/OSC exclusion list (with a
-- real NPI) who is nonetheless present in CMS Medicare billing (Part D
-- prescriber OR Part B practitioner) for the cycle's data year. This is
-- the state-exclusion mirror of provider_excluded_billing (mig 101): same
-- exact-NPI equijoin, but the exclusion authority is the NJ Office of the
-- State Comptroller, not federal HHS-OIG.
--
-- WHY SEVERITY 4 (HIGH), NOT 5 (CRITICAL)
-- ---------------------------------------
-- The federal LEIE signal is severity 5 because a federal exclusion
-- carries a statutory federal-payment prohibition (42 USC 1320a-7a) that
-- the Medicare billing overlap directly violates. A NJ *Medicaid*
-- debarment does NOT itself bar *Medicare* (a different, federal program)
-- billing -- the two programs are administered separately. The overlap is
-- therefore an extremely strong LEAD (NJ debarments are frequently
-- reciprocal to OIG action and to conduct that warrants federal scrutiny),
-- but it is not a per-se statutory violation. Severity 4 reflects "high-
-- priority lead, not adjudicated prohibition." No magic number: the
-- calibration row (seed 043) anchors this to the NJ OSC debarment
-- authority with a written rationale.
--
-- ENTITY / BUCKET / PERCENTILE
-- ----------------------------
-- entity_kind = 'provider' (NPI-keyed; introduced mig 101). cycle = CMS
-- data_year. peer_bucket = 'kind=provider'; peer_percentile is rate-based
-- binary over the combined Part D + Part B biller population for the year
-- (rarity of the overlap). raw_value = the provider's gross Medicare
-- exposure for the year (Part D drug cost + Part B Medicare paid) for
-- analyst triage by dollar magnitude.
--
-- WHAT THIS MIGRATION SHIPS
-- -------------------------
-- 0. ref.formula_version row.
-- 1. fraud_signal_config signal_family CHECK widened: + 'state_exclusion'.
-- 2. severity_calibration calibration_basis CHECK widened: + 'state_exclusion'.
-- 3. derived.refresh_signal_state_excluded_provider_billing(CHAR(4)).
-- 4. derived.fraud_signal_config row (family = state_exclusion).
-- 5. Master refresher rewired: TIER 6 (NJ-state-exclusion-bearing); 20 -> 21.
--
-- The evidence view is NOT rewritten: its entity_kind='provider' branches
-- (mig 101) already resolve display_name / is_nj for any provider
-- observation regardless of signal_id. Companion seed 043 ships the three
-- evidence-card reference rows.
--
-- IDEMPOTENT VIA CREATE OR REPLACE + ON CONFLICT + the schema_migrations
-- sha256 ledger. Safe to re-run.
-- ============================================================================

BEGIN;


-- ----------------------------------------------------------------------------
-- 0. Formula version registration
-- ----------------------------------------------------------------------------
INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '2.8.5-fraud-state-excluded-provider-billing-v1',
    'Pillar 2 (civic integrity) FRAUD-F7 Phase-3 signal slice. Adds the '
    'state_excluded_provider_billing cross-source signal: a currently-active '
    'NJ Medicaid/OSC exclusion (with a real NPI) present in CMS Medicare '
    'billing (Part D prescriber OR Part B practitioner) for the cycle data '
    'year. Exact NPI equijoin. State-exclusion mirror of '
    'provider_excluded_billing; severity 4 (HIGH lead -- NJ Medicaid '
    'debarment is not a per-se Medicare-payment prohibition). raw_value = '
    'combined Part D + Part B Medicare exposure; peer_percentile rate-based '
    'binary over the combined biller population. Introduces signal_family '
    'and calibration_basis value state_exclusion.',
    '2026-06-09'::DATE,
    'Stacks on 2.8.4-nj-medicaid-exclusion-substrate-v1.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ----------------------------------------------------------------------------
-- 1. signal_family CHECK widening: + 'state_exclusion'
--    (six families; the existing five from mig 064 stay valid)
-- ----------------------------------------------------------------------------
ALTER TABLE derived.fraud_signal_config
DROP CONSTRAINT IF EXISTS fraud_signal_config_signal_family_check;

ALTER TABLE derived.fraud_signal_config
ADD  CONSTRAINT fraud_signal_config_signal_family_check
CHECK (signal_family IN (
    'leie_bearing',
    'sam_bearing',
    'workforce',
    'address',
    'structural',
    'state_exclusion'
));

COMMENT ON CONSTRAINT fraud_signal_config_signal_family_check
ON derived.fraud_signal_config IS
    'Whitelist of signal_family values. Six families as of migration 105: '
    'leie_bearing (HHS-OIG federal healthcare exclusion), sam_bearing '
    '(SAM.gov federal-contracting exclusion), workforce (federal-contractor '
    'employee donations), address (residential/committee address '
    'clustering), structural (intra-FEC schema anomalies), state_exclusion '
    '(NJ state-level exclusion lists, e.g. NJ Medicaid/OSC debarment). The '
    'diversity bonus in derived.fraud_risk_score rewards entities firing on '
    'signals across distinct families.';


-- ----------------------------------------------------------------------------
-- 2. calibration_basis CHECK widening: + 'state_exclusion'
--    A NJ OSC debarment is not an HHS-OIG report; mislabeling it 'oig_report'
--    would violate the provenance invariant. Add an honest category.
-- ----------------------------------------------------------------------------
ALTER TABLE ref.fraud_signal_severity_calibration
DROP CONSTRAINT IF EXISTS fraud_signal_severity_calibration_basis_chk;

ALTER TABLE ref.fraud_signal_severity_calibration
ADD  CONSTRAINT fraud_signal_severity_calibration_basis_chk
CHECK (calibration_basis IN (
    'fec_mur',          -- FEC Matter Under Review (enforcement action)
    'oig_report',       -- HHS-OIG report or audit
    'doj_filing',       -- DOJ enforcement filing or settlement
    'crs_analysis',     -- Congressional Research Service analysis
    'far_authority',    -- FAR debarment / suspension authority
    'fec_advisory',     -- FEC Advisory Opinion
    'empirical_pctile', -- analyst calibration vs. NJ-cycle anomaly distribution
    'state_exclusion'   -- state-level exclusion authority (e.g. NJ OSC debarment)
));


-- ----------------------------------------------------------------------------
-- 2b. citation_authority CHECK widening: + 'NJ-OSC'
--     The platform was federal-only; the rule this signal codifies is
--     anchored to the NJ Office of the State Comptroller (the body that
--     maintains the NJ Medicaid debarment list). Mislabeling it as a
--     federal authority would violate the provenance invariant.
-- ----------------------------------------------------------------------------
ALTER TABLE ref.fraud_signal_human_explanation
DROP CONSTRAINT IF EXISTS fraud_signal_human_explanation_authority_chk;

ALTER TABLE ref.fraud_signal_human_explanation
ADD  CONSTRAINT fraud_signal_human_explanation_authority_chk
CHECK (citation_authority IN (
    'FEC',          -- Federal Election Commission
    'HHS-OIG',      -- HHS Office of Inspector General (LEIE)
    'GSA-SAM',      -- GSA SAM.gov (federal exclusion list)
    'FAR-Council',  -- Federal Acquisition Regulation Council
    'DOJ',          -- Department of Justice
    'CRS',          -- Congressional Research Service
    'platform',     -- structural/empirical anomaly with no direct authority
    'NJ-OSC'        -- NJ Office of the State Comptroller (Medicaid debarment)
));


-- ----------------------------------------------------------------------------
-- 2c. evidence_url_template upstream_source CHECK widening: + 'NJ.gov'
--     The upstream-verify button for this signal links to the NJ OSC
--     debarment list on nj.gov.
-- ----------------------------------------------------------------------------
ALTER TABLE ref.fraud_signal_evidence_url_template
DROP CONSTRAINT IF EXISTS fraud_signal_evidence_url_template_upstream_chk;

ALTER TABLE ref.fraud_signal_evidence_url_template
ADD  CONSTRAINT fraud_signal_evidence_url_template_upstream_chk
CHECK (upstream_source IN (
    'FEC.gov',
    'OIG.gov',
    'SAM.gov',
    'USAspending.gov',
    'platform-internal',
    'NJ.gov'
));


-- ----------------------------------------------------------------------------
-- 3. Refresher: derived.refresh_signal_state_excluded_provider_billing
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_signal_state_excluded_provider_billing(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
    v_year     INT := CAST(p_cycle AS INT);
BEGIN
    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'state_excluded_provider_billing';

    WITH excl AS (
        -- Currently-active NJ Medicaid exclusions with a real NPI. DISTINCT ON
        -- collapses the (possible) case of multiple active rows per NPI to the
        -- freshest-seen exclusion record.
        SELECT DISTINCT ON (npi)
            npi,
            record_hash
        FROM derived.v_nj_medicaid_exclusion_active
        WHERE npi ~ '^[0-9]{10}$'
          AND npi <> '0000000000'
        ORDER BY npi, last_seen_at DESC
    ),
    billing AS (
        -- Combined Medicare biller population for the data year, valid NPI
        -- only. One row per NPI carrying total Medicare exposure (Part D drug
        -- cost + Part B Medicare paid). A provider billing both programs is
        -- summed; a provider in neither is absent (so cannot be flagged).
        SELECT
            npi,
            SUM(mag)::NUMERIC AS medicare_dollars
        FROM (
            SELECT npi, COALESCE(tot_drug_cst, 0)::NUMERIC AS mag
            FROM raw.cms_partd_prescriber
            WHERE data_year = v_year
              AND npi ~ '^[0-9]{10}$'
              AND npi <> '0000000000'
            UNION ALL
            SELECT npi, COALESCE(tot_mdcr_pymt_amt, 0)::NUMERIC AS mag
            FROM raw.cms_physician_provider
            WHERE data_year = v_year
              AND npi ~ '^[0-9]{10}$'
              AND npi <> '0000000000'
        ) b
        GROUP BY npi
    ),
    matches AS (
        SELECT
            billing.npi,
            billing.medicare_dollars,
            excl.record_hash
        FROM billing
        JOIN excl USING (npi)
    ),
    pop AS (
        SELECT COUNT(*)::NUMERIC AS n_in_bucket FROM billing
    ),
    flag AS (
        SELECT COUNT(*)::NUMERIC AS n_flagged FROM matches
    )
    INSERT INTO derived.fraud_signal_observation (
        cycle, entity_kind, entity_id, signal_id,
        raw_value, severity, peer_bucket, peer_percentile, evidence_url
    )
    SELECT
        p_cycle,
        'provider',
        m.npi,
        'state_excluded_provider_billing',
        COALESCE(m.medicare_dollars, 0::NUMERIC),
        4::SMALLINT,
        'kind=provider',
        GREATEST(
            0::NUMERIC,
            1::NUMERIC - (f.n_flagged / NULLIF(pop.n_in_bucket, 0))
        ),
        '/risk/provider/' || m.npi
            || '?signal=state_excluded_provider_billing&njx=' || m.record_hash
    FROM matches m
    CROSS JOIN pop
    CROSS JOIN flag f;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_signal_state_excluded_provider_billing(CHAR(4)) IS
    'FRAUD-F7 Phase-3: emit state_excluded_provider_billing observations for '
    'the given cycle (= CMS data_year). Exact NPI join between active NJ '
    'Medicaid/OSC exclusions (v_nj_medicaid_exclusion_active) and combined '
    'CMS Part D + Part B billers for the year. Idempotent DELETE+INSERT on '
    'its own (cycle, signal_id) slice. Returns rows inserted; 0 when no CMS '
    'data is loaded for the cycle or no exclusion has an NPI.';


-- ----------------------------------------------------------------------------
-- 4. fraud_signal_config row
-- ----------------------------------------------------------------------------
INSERT INTO derived.fraud_signal_config
    (signal_id, signal_family, min_actionable_threshold, comment)
VALUES (
    'state_excluded_provider_billing',
    'state_exclusion',
    0,
    'Currently-active NJ Medicaid/OSC-excluded provider (with a real NPI) '
    'present in CMS Medicare billing (Part D OR Part B) for the cycle data '
    'year. Exact NPI match; severity 4 (HIGH lead -- NJ Medicaid debarment '
    'is not a per-se Medicare-payment prohibition). raw_value = combined '
    'Part D + Part B Medicare exposure.'
)
ON CONFLICT (signal_id) DO UPDATE SET
    signal_family            = EXCLUDED.signal_family,
    min_actionable_threshold = EXCLUDED.min_actionable_threshold,
    comment                  = EXCLUDED.comment,
    updated_at               = now();


-- ----------------------------------------------------------------------------
-- 5. Master refresher: add TIER 6 (NJ-state-exclusion-bearing); 20 -> 21
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_all_fraud_signal_observations(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_total INT := 0;
    n_each  INT;
BEGIN
    -- ----------------------------------------------------------------
    -- TIER 1: FEC-bulk-only structural signals (8)
    -- ----------------------------------------------------------------
    SELECT derived.refresh_treasurer_concentration_observations(p_cycle)    INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_candidate_no_pcc_observations(p_cycle)           INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_candidate_broken_pcc_observations(p_cycle)       INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_candidate_multiple_pccs_observations(p_cycle)    INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_committee_address_clusters_observations(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_committee_name_collisions_observations(p_cycle)  INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_candidate_namesakes_observations(p_cycle)        INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_treasurer_is_candidate_observations(p_cycle)     INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    -- ----------------------------------------------------------------
    -- TIER 2: LEIE-bearing signals (5)
    -- ----------------------------------------------------------------
    SELECT derived.refresh_signal_entity_on_leie(p_cycle)                   INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_entity_on_leie_strict_address(p_cycle)    INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_donor_on_leie(p_cycle)                    INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_candidate_funded_by_excluded_donors(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_nj_state_candidate_on_leie(p_cycle)       INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    -- ----------------------------------------------------------------
    -- TIER 3: SAM-bearing signals (3)
    -- ----------------------------------------------------------------
    SELECT derived.refresh_signal_entity_excluded_via_sam_uei(p_cycle)      INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_donor_on_sam(p_cycle)                     INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_candidate_funded_by_sam_excluded_donors(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    -- ----------------------------------------------------------------
    -- TIER 4: USAspending-bearing signals (3)
    -- ----------------------------------------------------------------
    SELECT derived.refresh_signal_entity_funded_and_excluded(p_cycle)       INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_candidate_funded_by_nj_contractor_employees(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_donor_employed_by_nj_contractor(p_cycle)  INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    -- ----------------------------------------------------------------
    -- TIER 5: CMS-Medicare-bearing (federal-exclusion) signals (1)
    -- ----------------------------------------------------------------
    SELECT derived.refresh_signal_provider_excluded_billing(p_cycle)        INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    -- ----------------------------------------------------------------
    -- TIER 6: NJ-state-exclusion-bearing signals (1)
    -- ----------------------------------------------------------------
    -- New as of mig 105: exact-NPI NJ-Medicaid-exclusion x CMS-billing
    -- overlap. Returns 0 when no CMS data is loaded for the cycle or no
    -- active NJ exclusion carries an NPI.
    SELECT derived.refresh_signal_state_excluded_provider_billing(p_cycle)  INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    RETURN n_total;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_all_fraud_signal_observations(CHAR(4)) IS
'Master fraud-signal refresher. Invokes all 21 seeded signal refreshers in '
'substrate-dependency tier order (FEC -> LEIE -> SAM -> USAspending -> CMS '
'federal-exclusion -> NJ-state-exclusion) for the given cycle. Each '
'per-signal refresher is an idempotent DELETE+INSERT slice returning INT '
'(rows inserted); the master returns SUM. Refreshers against empty/cycle-'
'mismatched substrate safely return 0. Mig 105 raises the count from 20 to '
'21 by adding state_excluded_provider_billing (TIER 6, NJ-state-exclusion).';


COMMIT;
