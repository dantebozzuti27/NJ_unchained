-- ============================================================================
-- Migration: 107_fraud_services_per_beneficiary_outlier
--
-- FRAUD-F7 Phase-2 (signal slice): services_per_beneficiary_outlier.
--
-- THE SIGNAL
-- ----------
-- A CMS Medicare Part B practitioner whose services-per-beneficiary ratio
-- (Tot_Srvcs / Tot_Benes from raw.cms_physician_provider) sits in the
-- extreme upper tail of its OWN specialty peer group for the data year.
-- This is the Part-B overutilization companion to the Part-D opioid
-- outlier (mig 106): an abnormally high number of billed services per
-- distinct patient is a classic phantom-billing / churning / upcoding
-- indicator. Same distributional (CUME_DIST, specialty-relative)
-- machinery and the same no-magic-numbers constant discipline.
--
-- WHY SPECIALTY-RELATIVE + A BENEFICIARY FLOOR
-- --------------------------------------------
-- Services-per-beneficiary varies enormously by specialty (an
-- ophthalmologist or physical therapist legitimately renders many more
-- services per patient than a primary-care visit). Ranking WITHIN
-- specialty (CUME_DIST partitioned by prvdr_type) is the only honest
-- comparison. A beneficiary-count floor suppresses small-denominator
-- artifacts (a provider with 3 patients and 60 services is noise, not a
-- mill), and a minimum specialty-peer count keeps the top-1% tail
-- statistically meaningful.
--
-- NO MAGIC NUMBERS (verifiable-data invariant)
-- --------------------------------------------
-- Tuning constants live in ref.platform_constants (citation_text +
-- formula_version), read at refresh time via derived.f_platform_constant():
--   * spb_outlier_tail_pctile = 0.99  -- top 1% within specialty
--   * spb_outlier_min_benes   = 50    -- beneficiary-count floor
--   * spb_outlier_min_bucket  = 100   -- min same-specialty peers
--
-- ENTITY / BUCKET / SEVERITY
-- --------------------------
-- entity_kind='provider' (NPI). cycle = CMS data_year. peer_bucket =
-- 'specialty=<prvdr_type>'. peer_percentile = within-specialty CUME_DIST of
-- the ratio. raw_value = the services-per-beneficiary ratio. severity 4
-- (HIGH lead), basis empirical_pctile -- a strong overutilization lead, not
-- an adjudicated violation.
--
-- WHAT THIS MIGRATION SHIPS
-- -------------------------
-- 0. ref.formula_version row.
-- 1. ref.platform_constants: three tuning constants (provenanced).
-- 2. derived.refresh_signal_services_per_beneficiary_outlier(CHAR(4)).
-- 3. derived.fraud_signal_config row (family = cms_utilization, exists).
-- 4. Master refresher rewired: TIER 7 now 2 signals; 22 -> 23.
--
-- signal_family cms_utilization and upstream_source CMS.gov already exist
-- (mig 106). Companion seed 045 ships the evidence-card reference rows.
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
    '2.8.7-fraud-services-per-beneficiary-outlier-v1',
    'Pillar 2 (civic integrity) FRAUD-F7 Phase-2 signal slice. Adds the '
    'services_per_beneficiary_outlier distributional signal: a CMS Part B '
    'practitioner in the extreme upper tail (top 1%) of its specialty peer '
    'group on Tot_Srvcs/Tot_Benes for the data year, with a beneficiary-count '
    'floor and a minimum specialty-peer count. Part-B overutilization '
    'companion to opioid_prescribing_outlier; cms_utilization family. Tuning '
    'constants in ref.platform_constants. Severity 4, basis empirical_pctile.',
    '2026-06-09'::DATE,
    'Stacks on 2.8.6-fraud-opioid-prescribing-outlier-v1.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ----------------------------------------------------------------------------
-- 1. Tuning constants in ref.platform_constants (provenanced, versioned)
-- ----------------------------------------------------------------------------
INSERT INTO ref.platform_constants
    (constant_id, value, description, source_url, citation_text,
     formula_version, effective_date)
VALUES
(
    'spb_outlier_tail_pctile',
    0.99,
    'Within-specialty CUME_DIST cutoff above which a Part B practitioner is '
    'flagged as a services-per-beneficiary overutilization outlier (top 1%).',
    'https://data.cms.gov/provider-summary-by-type-of-service/medicare-physician-other-practitioners',
    'Empirical platform calibration (analyst-set). Flags the top 1% of the '
    'Tot_Srvcs/Tot_Benes ratio WITHIN each specialty (prvdr_type) peer group, '
    'so high-service-intensity specialties (e.g. ophthalmology, PT) are judged '
    'against their own peers rather than an absolute ratio. Severity basis '
    'empirical_pctile; reviewed against the CMS Medicare Physician & Other '
    'Practitioners by-Provider distribution.',
    '2.8.7-fraud-services-per-beneficiary-outlier-v1',
    '2026-06-09'
),
(
    'spb_outlier_min_benes',
    50,
    'Minimum distinct Medicare beneficiaries for a practitioner to be '
    'eligible for the services-per-beneficiary outlier signal (suppresses '
    'small-denominator ratio artifacts).',
    'https://data.cms.gov/provider-summary-by-type-of-service/medicare-physician-other-practitioners',
    'Empirical platform calibration (analyst-set). Below ~50 beneficiaries '
    'the services-per-beneficiary ratio is a small-denominator artifact. The '
    'floor restricts the signal to practitioners with a stable enough patient '
    'panel. Basis empirical_pctile.',
    '2.8.7-fraud-services-per-beneficiary-outlier-v1',
    '2026-06-09'
),
(
    'spb_outlier_min_bucket',
    100,
    'Minimum number of same-specialty peers for a specialty bucket to be '
    'eligible (so a top-1% tail is statistically meaningful).',
    'https://data.cms.gov/provider-summary-by-type-of-service/medicare-physician-other-practitioners',
    'Empirical platform calibration (analyst-set). A specialty with fewer '
    'than ~100 eligible practitioners cannot yield a meaningful top-1% tail; '
    'such buckets are skipped. Basis empirical_pctile.',
    '2.8.7-fraud-services-per-beneficiary-outlier-v1',
    '2026-06-09'
)
ON CONFLICT (constant_id, formula_version) DO UPDATE SET
    value          = EXCLUDED.value,
    description    = EXCLUDED.description,
    source_url     = EXCLUDED.source_url,
    citation_text  = EXCLUDED.citation_text,
    effective_date = EXCLUDED.effective_date;


-- ----------------------------------------------------------------------------
-- 2. Refresher: derived.refresh_signal_services_per_beneficiary_outlier
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_signal_services_per_beneficiary_outlier(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted   INT;
    v_year       INT     := CAST(p_cycle AS INT);
    v_tail       NUMERIC := derived.f_platform_constant('spb_outlier_tail_pctile');
    v_min_benes  NUMERIC := derived.f_platform_constant('spb_outlier_min_benes');
    v_min_bucket NUMERIC := derived.f_platform_constant('spb_outlier_min_bucket');
BEGIN
    IF v_tail IS NULL OR v_min_benes IS NULL OR v_min_bucket IS NULL THEN
        RAISE EXCEPTION
            'services_per_beneficiary_outlier: missing ref.platform_constants '
            '(tail=%, min_benes=%, min_bucket=%). Seed mig 107 constants.',
            v_tail, v_min_benes, v_min_bucket
        USING ERRCODE = 'no_data_found';
    END IF;

    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'services_per_beneficiary_outlier';

    WITH src AS (
        SELECT
            npi,
            prvdr_type,
            (tot_srvcs / NULLIF(tot_benes, 0))::NUMERIC AS ratio
        FROM raw.cms_physician_provider
        WHERE data_year = v_year
          AND npi ~ '^[0-9]{10}$'
          AND npi <> '0000000000'
          AND prvdr_type IS NOT NULL
          AND prvdr_type <> ''
          AND tot_srvcs IS NOT NULL
          AND tot_benes IS NOT NULL
          AND tot_benes >= v_min_benes
    ),
    ranked AS (
        SELECT
            npi,
            prvdr_type,
            ratio,
            CUME_DIST() OVER (
                PARTITION BY prvdr_type ORDER BY ratio
            ) AS peer_percentile,
            COUNT(*) OVER (PARTITION BY prvdr_type) AS bucket_n
        FROM src
        WHERE ratio IS NOT NULL
    )
    INSERT INTO derived.fraud_signal_observation (
        cycle, entity_kind, entity_id, signal_id,
        raw_value, severity, peer_bucket, peer_percentile, evidence_url
    )
    SELECT
        p_cycle,
        'provider',
        r.npi,
        'services_per_beneficiary_outlier',
        r.ratio,
        4::SMALLINT,
        'specialty=' || r.prvdr_type,
        r.peer_percentile,
        '/risk/provider/' || r.npi
            || '?signal=services_per_beneficiary_outlier'
    FROM ranked r
    WHERE r.bucket_n >= v_min_bucket
      AND r.peer_percentile >= v_tail;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_signal_services_per_beneficiary_outlier(CHAR(4)) IS
    'FRAUD-F7 Phase-2: emit services_per_beneficiary_outlier observations for '
    'the given cycle (= CMS data_year). Flags Part B practitioners in the '
    'extreme upper tail (CUME_DIST >= spb_outlier_tail_pctile) of their '
    'specialty peer group on Tot_Srvcs/Tot_Benes, gated by a beneficiary floor '
    'and a minimum specialty-peer count (all from ref.platform_constants). '
    'Idempotent DELETE+INSERT on its own (cycle, signal_id) slice. Returns '
    'rows inserted; 0 when no CMS Part B data is loaded for the cycle.';


-- ----------------------------------------------------------------------------
-- 3. fraud_signal_config row (cms_utilization family already whitelisted)
-- ----------------------------------------------------------------------------
INSERT INTO derived.fraud_signal_config
    (signal_id, signal_family, min_actionable_threshold, comment)
VALUES (
    'services_per_beneficiary_outlier',
    'cms_utilization',
    0,
    'CMS Part B practitioner in the extreme upper tail (top 1%) of its '
    'specialty peer group on services-per-beneficiary (Tot_Srvcs/Tot_Benes), '
    'with a beneficiary floor and minimum specialty-peer count (all from '
    'ref.platform_constants). Distributional (CUME_DIST) overutilization '
    'signal; severity 4 (HIGH lead). raw_value = the services-per-beneficiary '
    'ratio.'
)
ON CONFLICT (signal_id) DO UPDATE SET
    signal_family            = EXCLUDED.signal_family,
    min_actionable_threshold = EXCLUDED.min_actionable_threshold,
    comment                  = EXCLUDED.comment,
    updated_at               = now();


-- ----------------------------------------------------------------------------
-- 4. Master refresher: TIER 7 now 2 signals; 22 -> 23
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
    SELECT derived.refresh_signal_state_excluded_provider_billing(p_cycle)  INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    -- ----------------------------------------------------------------
    -- TIER 7: CMS-utilization (peer-relative outlier) signals (2)
    -- ----------------------------------------------------------------
    SELECT derived.refresh_signal_opioid_prescribing_outlier(p_cycle)       INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    -- New as of mig 107: Part B services-per-beneficiary overutilization tail.
    SELECT derived.refresh_signal_services_per_beneficiary_outlier(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    RETURN n_total;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_all_fraud_signal_observations(CHAR(4)) IS
'Master fraud-signal refresher. Invokes all 23 seeded signal refreshers in '
'substrate-dependency tier order (FEC -> LEIE -> SAM -> USAspending -> CMS '
'federal-exclusion -> NJ-state-exclusion -> CMS-utilization) for the given '
'cycle. Each per-signal refresher is an idempotent DELETE+INSERT slice '
'returning INT (rows inserted); the master returns SUM. Refreshers against '
'empty/cycle-mismatched substrate safely return 0. Mig 107 raises the count '
'from 22 to 23 by adding services_per_beneficiary_outlier (TIER 7, '
'CMS-utilization).';


COMMIT;
