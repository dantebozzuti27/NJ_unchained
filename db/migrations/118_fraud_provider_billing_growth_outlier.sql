-- ============================================================================
-- Migration: 118_fraud_provider_billing_growth_outlier
--
-- FRAUD-F8 prospective (pre-enforcement) TEMPORAL detector:
-- provider_billing_growth_outlier -- the "bust-out" / NPI-takeover signature.
--
-- THE SIGNAL
-- ----------
-- A CMS Medicare Part B practitioner whose year-over-year Medicare-paid amount
-- (Tot_Mdcr_Pymt_Amt this year / last year) sits in the extreme upper tail
-- (top 1%) of its OWN specialty peer group, gated by an absolute current-year
-- payment floor (material exposure) and a prior-year denominator floor (so the
-- ratio is meaningful, not a divide-by-tiny artifact).
--
-- WHY THIS IS A DIFFERENT KIND OF SIGNAL (and a NEW family)
-- --------------------------------------------------------
-- Every other CMS behavioral detector (opioid, services-per-beneficiary,
-- elderly-antipsychotic) is a LEVEL outlier in a single year. This one is a
-- DYNAMICS outlier across years. A sudden multi-fold ramp in Medicare billings
-- -- or a dormant/new NPI that suddenly bills at scale -- is the classic
-- "bust-out" pattern: a provider identity is acquired or activated, billed
-- aggressively for a short window, then abandoned before enforcement catches up.
-- Because it measures a fundamentally different mechanism (temporal change, not
-- cross-sectional level), it is epistemically INDEPENDENT of the level outliers
-- and earns its own signal_family, 'cms_temporal'. That independence is the
-- point: the diversity bonus in derived.fraud_risk_score (mig 061) and the
-- n_families tiebreak in v_high_value_leads (mig 116) both reward a provider
-- that trips a temporal AND a level signal -- two conditionally-independent
-- lines of evidence corroborate far more than two correlated ones.
--
-- WHY SPECIALTY-RELATIVE + ABSOLUTE FLOORS
-- ----------------------------------------
-- Baseline growth/churn differs by specialty (a specialty adopting a new
-- procedure code grows faster across the board), so we rank growth WITHIN
-- specialty (CUME_DIST partitioned by prvdr_type) -- the same peer-relative
-- discipline as the level detectors. The absolute current-year floor ensures
-- the flagged ramp is to a MATERIAL dollar amount (a 10x jump from $300 to
-- $3,000 is not a fraud lead); the prior-year floor keeps the ratio honest.
--
-- WHY THIS IS AN "UNDETECTED" LEAD
-- -------------------------------
-- It fires on providers with NO exclusion/debarment on record -- the cases that
-- haven't happened yet. raw_value is a growth RATIO, not USD, so the lead
-- ranking treats it as a corroborating count signal, not a dollar exposure
-- (reportability raw_value_is_usd = FALSE).
--
-- NO MAGIC NUMBERS (verifiable-data invariant)
-- --------------------------------------------
-- Tuning constants live in ref.platform_constants (cited, versioned), read at
-- refresh time via derived.f_platform_constant():
--   * billing_growth_tail_pctile   = 0.99   -- top 1% within specialty
--   * billing_growth_min_curr_pymt = 50000  -- material current-year $ floor
--   * billing_growth_min_prev_pymt = 1000   -- prior-year denominator floor
--   * billing_growth_min_bucket    = 50     -- min same-specialty peers (both yrs)
--
-- ENTITY / BUCKET / SEVERITY
-- --------------------------
-- entity_kind='provider' (NPI). cycle = CMS data_year (the LATER year). prev =
-- cycle-1. peer_bucket = 'specialty=<prvdr_type>'. peer_percentile = within-
-- specialty CUME_DIST of the growth ratio. raw_value = the growth ratio
-- (curr/prev). severity 4 (HIGH lead), basis empirical_pctile.
--
-- WHAT THIS MIGRATION SHIPS
-- -------------------------
-- 0. ref.formula_version row.
-- 1. signal_family CHECK widened: + 'cms_temporal' (eight families).
-- 2. ref.platform_constants: four tuning constants (provenanced).
-- 3. derived.refresh_signal_provider_billing_growth_outlier(CHAR(4)).
-- 4. derived.fraud_signal_config row (family = cms_temporal).
-- 5. Master refresher rewired: new TIER 10 (cms_temporal); 27 -> 28.
--
-- Substrate is raw.cms_physician_provider (Part B), already loaded for 2023 and
-- 2024. The refresher needs BOTH the cycle year and cycle-1 present; against a
-- single-year or empty substrate it safely returns 0. Companion seed 052 ships
-- the evidence-card + reportability-channel rows. IDEMPOTENT. Safe to re-run.
-- ============================================================================

BEGIN;


-- ----------------------------------------------------------------------------
-- 0. Formula version
-- ----------------------------------------------------------------------------
INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '3.6.0-fraud-provider-billing-growth-outlier-v1',
    'Pillar 2 (civic integrity) FRAUD-F8 prospective TEMPORAL detector. Adds '
    'provider_billing_growth_outlier: a CMS Part B practitioner in the top 1% '
    'of its specialty peer group on year-over-year Medicare-paid growth '
    '(Tot_Mdcr_Pymt_Amt cycle / cycle-1), gated by a material current-year '
    'payment floor and a prior-year denominator floor. The "bust-out" / '
    'NPI-takeover signature, flagged before any enforcement action. First '
    'cms_temporal family (a dynamics outlier, independent of the single-year '
    'level outliers, so it corroborates them). Severity 4, basis '
    'empirical_pctile. Tuning constants in ref.platform_constants.',
    '2026-06-10',
    'Substrate: raw.cms_physician_provider for the cycle year AND cycle-1 '
    '(both 2023 and 2024 loaded). Returns 0 when the prior year is absent.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ----------------------------------------------------------------------------
-- 1. signal_family CHECK widening: + 'cms_temporal' (eight families)
--    (the existing seven from mig 106 stay valid)
-- ----------------------------------------------------------------------------
ALTER TABLE derived.fraud_signal_config
    DROP CONSTRAINT IF EXISTS fraud_signal_config_signal_family_check;

ALTER TABLE derived.fraud_signal_config
    ADD CONSTRAINT fraud_signal_config_signal_family_check
    CHECK (signal_family IN (
        'leie_bearing',     -- HHS-OIG LEIE exclusion list
        'workforce',        -- federal-contractor employee donations
        'address',          -- residential-address clustering
        'structural',       -- intra-FEC schema anomalies
        'sam_bearing',      -- SAM.gov debarment list
        'state_exclusion',  -- NJ-state Medicaid exclusion list
        'cms_utilization',  -- peer-relative CMS single-year level outliers
        'cms_temporal'      -- CMS year-over-year billing-dynamics outliers
    ));

COMMENT ON COLUMN derived.fraud_signal_config.signal_family IS
    'Whitelist of signal_family values. Eight families as of migration 118. '
    'cms_temporal (added mig 118) covers CMS Medicare year-over-year '
    'billing-dynamics outliers (e.g. bust-out / sudden-ramp). It is held '
    'epistemically independent of cms_utilization (single-year level outliers), '
    'so a provider firing both contributes a multi-family diversity bonus in '
    'derived.fraud_risk_score and an n_families tiebreak in v_high_value_leads.';


-- ----------------------------------------------------------------------------
-- 2. Tuning constants
-- ----------------------------------------------------------------------------
INSERT INTO ref.platform_constants
    (constant_id, value, description, source_url, citation_text,
     formula_version, effective_date)
VALUES
(
    'billing_growth_tail_pctile',
    0.99,
    'Within-specialty CUME_DIST cutoff above which a Part B practitioner is '
    'flagged as an extreme year-over-year Medicare-billing growth outlier '
    '(top 1%).',
    'https://data.cms.gov/provider-summary-by-type-of-service/medicare-physician-other-practitioners',
    'Empirical platform calibration (analyst-set). Flags the top 1% of the '
    'year-over-year Tot_Mdcr_Pymt_Amt growth ratio WITHIN each specialty '
    '(prvdr_type), so specialties with systematically higher baseline growth '
    'are judged against their own peers. Mirrors the level-outlier tail cutoffs '
    '(opioid, services-per-beneficiary). Severity basis empirical_pctile; the '
    'sudden-ramp / bust-out pattern is a recognized HHS-OIG / DOJ healthcare-'
    'fraud typology (provider-identity takeover, then abandon before audit).',
    '3.6.0-fraud-provider-billing-growth-outlier-v1',
    '2026-06-10'
),
(
    'billing_growth_min_curr_pymt',
    50000,
    'Minimum current-year Medicare-paid amount (Tot_Mdcr_Pymt_Amt) for a '
    'practitioner to be eligible (so a flagged ramp is to a MATERIAL dollar '
    'amount, not a trivial one).',
    'https://data.cms.gov/provider-summary-by-type-of-service/medicare-physician-other-practitioners',
    'Empirical platform calibration (analyst-set). A large growth multiple is '
    'only a fraud lead if the absolute current-year exposure is material; a 10x '
    'jump from $300 to $3,000 is noise. The $50,000 floor restricts the signal '
    'to providers whose ramped billing is itself a meaningful Medicare outlay. '
    'Basis empirical_pctile.',
    '3.6.0-fraud-provider-billing-growth-outlier-v1',
    '2026-06-10'
),
(
    'billing_growth_min_prev_pymt',
    1000,
    'Minimum prior-year Medicare-paid amount for the growth denominator (so the '
    'ratio is meaningful and not a divide-by-tiny artifact).',
    'https://data.cms.gov/provider-summary-by-type-of-service/medicare-physician-other-practitioners',
    'Empirical platform calibration (analyst-set). The growth ratio '
    'curr/prev is undefined/explosive when the prior year is near zero; '
    'requiring at least $1,000 of prior-year Medicare payment keeps the ratio '
    'interpretable while still admitting genuine bust-outs (a dormant NPI '
    'billing a small base then ramping hard). Providers truly absent in the '
    'prior year are out of scope for this ratio detector (a sudden-onset '
    'companion is a separate future signal). Basis empirical_pctile.',
    '3.6.0-fraud-provider-billing-growth-outlier-v1',
    '2026-06-10'
),
(
    'billing_growth_min_bucket',
    50,
    'Minimum same-specialty peers (present in BOTH years above the floors) for a '
    'specialty bucket to be ranked (so a top-1% tail is statistically '
    'meaningful).',
    'https://data.cms.gov/provider-summary-by-type-of-service/medicare-physician-other-practitioners',
    'Empirical platform calibration (analyst-set). A specialty with fewer than '
    '~50 eligible two-year peers cannot yield a meaningful top-1% growth tail '
    '(CUME_DIST on a handful of rows is noise); such buckets are skipped. The '
    'two-year intersection is smaller than the single-year base, so the floor '
    'is 50 rather than the services-per-beneficiary signal''s 100. Basis '
    'empirical_pctile.',
    '3.6.0-fraud-provider-billing-growth-outlier-v1',
    '2026-06-10'
)
ON CONFLICT (constant_id, formula_version) DO UPDATE SET
    value          = EXCLUDED.value,
    description    = EXCLUDED.description,
    source_url     = EXCLUDED.source_url,
    citation_text  = EXCLUDED.citation_text,
    effective_date = EXCLUDED.effective_date;


-- ----------------------------------------------------------------------------
-- 3. Refresher
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_signal_provider_billing_growth_outlier(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted   INT;
    v_year       INT     := CAST(p_cycle AS INT);
    v_prev       INT     := CAST(p_cycle AS INT) - 1;
    v_tail       NUMERIC := derived.f_platform_constant('billing_growth_tail_pctile');
    v_min_curr   NUMERIC := derived.f_platform_constant('billing_growth_min_curr_pymt');
    v_min_prev   NUMERIC := derived.f_platform_constant('billing_growth_min_prev_pymt');
    v_min_bucket NUMERIC := derived.f_platform_constant('billing_growth_min_bucket');
BEGIN
    IF v_tail IS NULL OR v_min_curr IS NULL OR v_min_prev IS NULL
       OR v_min_bucket IS NULL THEN
        RAISE EXCEPTION
            'provider_billing_growth_outlier: missing ref.platform_constants '
            '(tail=%, min_curr=%, min_prev=%, min_bucket=%). Seed mig 118 '
            'constants.', v_tail, v_min_curr, v_min_prev, v_min_bucket
        USING ERRCODE = 'no_data_found';
    END IF;

    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'provider_billing_growth_outlier';

    WITH curr AS (
        -- Current-year Part B providers above the material-exposure floor.
        SELECT
            npi,
            prvdr_type,
            tot_mdcr_pymt_amt AS pymt
        FROM raw.cms_physician_provider
        WHERE data_year = v_year
          AND npi ~ '^[0-9]{10}$'
          AND npi <> '0000000000'
          AND prvdr_type IS NOT NULL
          AND prvdr_type <> ''
          AND tot_mdcr_pymt_amt IS NOT NULL
          AND tot_mdcr_pymt_amt >= v_min_curr
    ),
    prev AS (
        -- Prior-year payment for the same NPI, above the denominator floor.
        SELECT
            npi,
            tot_mdcr_pymt_amt AS pymt
        FROM raw.cms_physician_provider
        WHERE data_year = v_prev
          AND npi ~ '^[0-9]{10}$'
          AND npi <> '0000000000'
          AND tot_mdcr_pymt_amt IS NOT NULL
          AND tot_mdcr_pymt_amt >= v_min_prev
    ),
    src AS (
        SELECT
            c.npi,
            c.prvdr_type,
            (c.pymt / p.pymt)::NUMERIC AS growth
        FROM curr c
        JOIN prev p USING (npi)
    ),
    ranked AS (
        SELECT
            npi,
            prvdr_type,
            growth,
            CUME_DIST() OVER (
                PARTITION BY prvdr_type ORDER BY growth
            ) AS peer_percentile,
            COUNT(*) OVER (PARTITION BY prvdr_type) AS bucket_n
        FROM src
    )
    INSERT INTO derived.fraud_signal_observation (
        cycle, entity_kind, entity_id, signal_id,
        raw_value, severity, peer_bucket, peer_percentile, evidence_url
    )
    SELECT
        p_cycle,
        'provider',
        r.npi,
        'provider_billing_growth_outlier',
        r.growth,
        4::SMALLINT,
        'specialty=' || r.prvdr_type,
        r.peer_percentile,
        '/risk/provider/' || r.npi
            || '?signal=provider_billing_growth_outlier'
    FROM ranked r
    WHERE r.bucket_n >= v_min_bucket
      AND r.peer_percentile >= v_tail;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_signal_provider_billing_growth_outlier(CHAR(4)) IS
    'FRAUD-F8 prospective TEMPORAL detector: emit provider_billing_growth_outlier '
    'observations for the cycle (= CMS data_year, the LATER year). Flags Part B '
    'practitioners in the extreme upper tail (CUME_DIST >= '
    'billing_growth_tail_pctile) of their specialty peer group on year-over-year '
    'Tot_Mdcr_Pymt_Amt growth (cycle / cycle-1), gated by a current-year payment '
    'floor, a prior-year denominator floor, and a minimum specialty-peer count '
    '(all from ref.platform_constants). The bust-out / NPI-takeover signature. '
    'Idempotent DELETE+INSERT on its own (cycle, signal_id) slice. Returns rows '
    'inserted; 0 when the cycle or its prior year is not loaded.';


-- ----------------------------------------------------------------------------
-- 4. fraud_signal_config row (new cms_temporal family)
-- ----------------------------------------------------------------------------
INSERT INTO derived.fraud_signal_config
    (signal_id, signal_family, min_actionable_threshold, comment)
VALUES (
    'provider_billing_growth_outlier',
    'cms_temporal',
    0,
    'CMS Part B practitioner in the extreme upper tail (top 1%) of its specialty '
    'peer group on year-over-year Medicare-paid growth (Tot_Mdcr_Pymt_Amt cycle '
    '/ cycle-1), with a current-year payment floor, a prior-year denominator '
    'floor, and a minimum specialty-peer count (all from ref.platform_constants). '
    'Distributional (CUME_DIST) temporal-dynamics signal; severity 4 (HIGH '
    'lead). raw_value = the growth ratio. Bust-out / NPI-takeover lead. First '
    'cms_temporal family -- independent of the single-year level outliers, so it '
    'corroborates them (diversity bonus + n_families tiebreak).'
)
ON CONFLICT (signal_id) DO UPDATE SET
    signal_family            = EXCLUDED.signal_family,
    min_actionable_threshold = EXCLUDED.min_actionable_threshold,
    comment                  = EXCLUDED.comment,
    updated_at               = now();


-- ----------------------------------------------------------------------------
-- 5. Master refresher: new TIER 10 (cms_temporal); 27 -> 28
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
    -- TIER 5: CMS-Medicare-bearing (federal-exclusion) signals (2)
    -- ----------------------------------------------------------------
    SELECT derived.refresh_signal_provider_excluded_billing(p_cycle)        INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_provider_excluded_billing_partb(p_cycle)  INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    -- ----------------------------------------------------------------
    -- TIER 6: NJ-state-exclusion-bearing signals (1)
    -- ----------------------------------------------------------------
    SELECT derived.refresh_signal_state_excluded_provider_billing(p_cycle)  INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    -- ----------------------------------------------------------------
    -- TIER 7: CMS-utilization (single-year peer-relative level) signals (3)
    -- ----------------------------------------------------------------
    SELECT derived.refresh_signal_opioid_prescribing_outlier(p_cycle)       INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_services_per_beneficiary_outlier(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_antipsychotic_elderly_outlier(p_cycle)    INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    -- ----------------------------------------------------------------
    -- TIER 8: NPPES identity-resolution recall signals (1 -- mig 110)
    -- ----------------------------------------------------------------
    SELECT derived.refresh_signal_name_resolved_excluded_provider_billing(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    -- ----------------------------------------------------------------
    -- TIER 9: Open-Payments conflict-of-interest signals (1 -- mig 111)
    -- ----------------------------------------------------------------
    SELECT derived.refresh_signal_excluded_provider_received_open_payments(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    -- ----------------------------------------------------------------
    -- TIER 10: CMS-temporal (year-over-year billing-dynamics) signals
    --          (1 -- mig 118). Returns 0 unless BOTH the cycle year and
    --          cycle-1 Part B data are loaded.
    -- ----------------------------------------------------------------
    SELECT derived.refresh_signal_provider_billing_growth_outlier(p_cycle)  INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    RETURN n_total;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_all_fraud_signal_observations(CHAR(4)) IS
'Master fraud-signal refresher. Invokes all 28 seeded signal refreshers in '
'substrate-dependency tier order (FEC -> LEIE -> SAM -> USAspending -> CMS '
'federal-exclusion -> NJ-state-exclusion -> CMS-utilization -> NPPES '
'identity-resolution recall -> Open-Payments conflict-of-interest -> '
'CMS-temporal billing-dynamics) for the given cycle. Each per-signal refresher '
'is an idempotent DELETE+INSERT slice returning INT (rows inserted); the master '
'returns SUM. Refreshers against empty/cycle-mismatched substrate safely return '
'0. Mig 118 raises the count from 27 to 28 by adding '
'provider_billing_growth_outlier (TIER 10, the first cms_temporal '
'billing-dynamics / bust-out detector).';


COMMIT;
