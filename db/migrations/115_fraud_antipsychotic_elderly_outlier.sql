-- ============================================================================
-- Migration: 115_fraud_antipsychotic_elderly_outlier
--
-- FRAUD-F8 prospective (pre-enforcement) detector: antipsychotic_elderly_outlier.
--
-- THE SIGNAL
-- ----------
-- A CMS Medicare Part D prescriber whose share of elderly (>=65) beneficiaries
-- receiving an antipsychotic -- antpsyct_ge65_tot_benes / ge65_tot_benes -- sits
-- in the extreme upper tail (top 1%) of its OWN specialty peer group for the
-- data year, subject to an elderly-population floor and a minimum specialty-peer
-- count.
--
-- WHY THIS IS AN "UNDETECTED FRAUD" LEAD
-- --------------------------------------
-- Antipsychotic over-prescribing to the elderly -- especially dementia patients
-- in nursing facilities -- is a well-documented harm and fraud pattern
-- ("chemical restraint": sedating residents for staff convenience, and billing
-- medically-unnecessary drugs). CMS runs the National Partnership to Improve
-- Dementia Care precisely to drive these rates DOWN, and antipsychotics carry an
-- FDA boxed warning for increased mortality in elderly dementia patients. Unlike
-- the exclusion-billing signals, this flags providers with NO enforcement action
-- on record -- the cases that haven't happened yet.
--
-- WHY SPECIALTY-RELATIVE (not an absolute cutoff)
-- -----------------------------------------------
-- Psychiatrists legitimately prescribe antipsychotics far more than internists.
-- An absolute rate would drown the queue in psychiatry and miss a family-
-- practice prescriber running a nursing-home chemical-restraint operation. So
-- the signal ranks each prescriber WITHIN its specialty (CUME_DIST partitioned
-- by prscrbr_type) -- identical semantics to the opioid outlier (mig 106).
--
-- NO MAGIC NUMBERS (verifiable-data invariant)
-- --------------------------------------------
-- Tuning constants live in ref.platform_constants (cited, versioned), read at
-- refresh time via derived.f_platform_constant():
--   * antipsychotic_elderly_tail_pctile  = 0.99  -- top 1% within specialty
--   * antipsychotic_elderly_min_ge65_benes = 50  -- elderly-denominator floor
--   * antipsychotic_elderly_min_bucket   = 50    -- min same-specialty peers
--
-- ENTITY / BUCKET / SEVERITY
-- --------------------------
-- entity_kind = 'provider' (NPI). cycle = CMS data_year. peer_bucket =
-- 'specialty=<prscrbr_type>'. peer_percentile = within-specialty CUME_DIST of
-- the elderly-antipsychotic rate. raw_value = that rate as a PERCENT (0-100, to
-- match the opioid signal's convention). severity 4 (HIGH lead), basis
-- empirical_pctile -- a strong lead, not an adjudicated violation.
--
-- WHAT THIS MIGRATION SHIPS
-- -------------------------
-- 0. ref.formula_version row.
-- 1. ref.platform_constants: three tuning constants (provenanced).
-- 2. derived.refresh_signal_antipsychotic_elderly_outlier(CHAR(4)).
-- 3. derived.fraud_signal_config row (family = cms_utilization).
-- 4. Master refresher rewired: TIER 7 gains a 3rd signal; 26 -> 27.
--
-- Companion seed 051 ships the evidence-card + reportability-channel rows.
-- signal_family 'cms_utilization' and upstream_source 'CMS.gov' already exist
-- (mig 106), so no CHECK widening is needed. IDEMPOTENT. Safe to re-run.
-- ============================================================================

BEGIN;


-- ----------------------------------------------------------------------------
-- 0. Formula version
-- ----------------------------------------------------------------------------
INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '3.3.0-fraud-antipsychotic-elderly-outlier-v1',
    'Pillar 2 (civic integrity) FRAUD-F8 prospective detector. Adds '
    'antipsychotic_elderly_outlier: a CMS Part D prescriber in the top 1% of '
    'its specialty peer group on the share of elderly (>=65) beneficiaries '
    'receiving antipsychotics, gated by an elderly-population floor and a '
    'minimum specialty-peer count. Flags nursing-home "chemical restraint" / '
    'medically-unnecessary prescribing BEFORE any enforcement action. '
    'cms_utilization family; severity 4, basis empirical_pctile. Tuning '
    'constants in ref.platform_constants.',
    '2026-06-09',
    'Stacks on 3.2.0-cms-partd-behavioral-columns-v1 (needs the GE65 + '
    'antipsychotic columns from mig 114, populated by an ingester reload).'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ----------------------------------------------------------------------------
-- 1. Tuning constants
-- ----------------------------------------------------------------------------
INSERT INTO ref.platform_constants
    (constant_id, value, description, source_url, citation_text,
     formula_version, effective_date)
VALUES
(
    'antipsychotic_elderly_tail_pctile',
    0.99,
    'Within-specialty CUME_DIST cutoff above which a Part D prescriber is '
    'flagged as an extreme elderly-antipsychotic outlier (top 1%).',
    'https://data.cms.gov/provider-summary-by-type-of-service/medicare-part-d-prescribers',
    'Empirical platform calibration (analyst-set). Flags the top 1% of the '
    'elderly-antipsychotic rate (Antpsyct_GE65_Tot_Benes / GE65_Tot_Benes) '
    'WITHIN each specialty (prscrbr_type), so high-baseline specialties (e.g. '
    'psychiatry) are judged against their own peers. Mirrors the opioid-outlier '
    'tail cutoff. Severity basis empirical_pctile. Motivated by the CMS '
    'National Partnership to Improve Dementia Care and the FDA boxed warning on '
    'antipsychotics in elderly dementia patients.',
    '3.3.0-fraud-antipsychotic-elderly-outlier-v1',
    '2026-06-09'
),
(
    'antipsychotic_elderly_min_ge65_benes',
    50,
    'Minimum elderly (>=65) beneficiary count for a prescriber to be eligible '
    '(suppresses small-denominator rate artifacts).',
    'https://data.cms.gov/provider-summary-by-type-of-service/medicare-part-d-prescribers',
    'Empirical platform calibration (analyst-set). Below ~50 elderly '
    'beneficiaries the antipsychotic share is a small-denominator artifact (a '
    'few patients can yield an extreme rate). CMS also suppresses any cell with '
    '<11 beneficiaries (loaded as NULL), so this floor stacks on top of CMS '
    'suppression. Basis empirical_pctile.',
    '3.3.0-fraud-antipsychotic-elderly-outlier-v1',
    '2026-06-09'
),
(
    'antipsychotic_elderly_min_bucket',
    50,
    'Minimum same-specialty eligible peers for a specialty bucket to be ranked '
    '(so a top-1% tail is statistically meaningful).',
    'https://data.cms.gov/provider-summary-by-type-of-service/medicare-part-d-prescribers',
    'Empirical platform calibration (analyst-set). A specialty with fewer than '
    '~50 eligible prescribers cannot yield a meaningful top-1% tail (CUME_DIST '
    'on a handful of rows is noise); such buckets are skipped. The elderly-'
    'antipsychotic eligible population is smaller than the all-prescriber base, '
    'so the floor is 50 rather than the opioid signal''s 100. Basis '
    'empirical_pctile.',
    '3.3.0-fraud-antipsychotic-elderly-outlier-v1',
    '2026-06-09'
)
ON CONFLICT (constant_id, formula_version) DO UPDATE SET
    value          = EXCLUDED.value,
    description    = EXCLUDED.description,
    source_url     = EXCLUDED.source_url,
    citation_text  = EXCLUDED.citation_text,
    effective_date = EXCLUDED.effective_date;


-- ----------------------------------------------------------------------------
-- 2. Refresher
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_signal_antipsychotic_elderly_outlier(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted     INT;
    v_year         INT     := CAST(p_cycle AS INT);
    v_tail         NUMERIC := derived.f_platform_constant('antipsychotic_elderly_tail_pctile');
    v_min_benes    NUMERIC := derived.f_platform_constant('antipsychotic_elderly_min_ge65_benes');
    v_min_bucket   NUMERIC := derived.f_platform_constant('antipsychotic_elderly_min_bucket');
BEGIN
    IF v_tail IS NULL OR v_min_benes IS NULL OR v_min_bucket IS NULL THEN
        RAISE EXCEPTION
            'antipsychotic_elderly_outlier: missing ref.platform_constants '
            '(tail=%, min_benes=%, min_bucket=%). Seed mig 115 constants.',
            v_tail, v_min_benes, v_min_bucket
        USING ERRCODE = 'no_data_found';
    END IF;

    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'antipsychotic_elderly_outlier';

    WITH src AS (
        -- Eligible peer group = providers who ACTUALLY prescribe antipsychotics
        -- to the elderly (antpsyct_ge65_tot_benes > 0), with both the numerator
        -- and the elderly denominator present (not CMS-suppressed) and at least
        -- the elderly-population floor. The rate>0 restriction is essential, not
        -- cosmetic: the metric is heavily zero-inflated (most providers
        -- correctly prescribe ZERO antipsychotics to the elderly), and CUME_DIST
        -- over a >99%-zero bucket would push the entire zero-mode to percentile
        -- >= 0.99 and flag non-prescribers -- the exact opposite of the signal.
        -- An over-prescriber is only meaningful relative to OTHER prescribers.
        -- CMS suppresses <11-beneficiary cells to NULL, so a non-null, >0 value
        -- already means >=11 elderly antipsychotic beneficiaries. rate as a
        -- PERCENT to match the opioid signal's convention.
        SELECT
            npi,
            prscrbr_type,
            (antpsyct_ge65_tot_benes / ge65_tot_benes) * 100.0 AS rate
        FROM raw.cms_partd_prescriber
        WHERE data_year = v_year
          AND npi ~ '^[0-9]{10}$'
          AND npi <> '0000000000'
          AND prscrbr_type IS NOT NULL
          AND prscrbr_type <> ''
          AND antpsyct_ge65_tot_benes IS NOT NULL
          AND antpsyct_ge65_tot_benes > 0
          AND ge65_tot_benes IS NOT NULL
          AND ge65_tot_benes >= v_min_benes
    ),
    ranked AS (
        SELECT
            npi,
            prscrbr_type,
            rate,
            CUME_DIST() OVER (
                PARTITION BY prscrbr_type ORDER BY rate
            ) AS peer_percentile,
            COUNT(*) OVER (PARTITION BY prscrbr_type) AS bucket_n
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
        'antipsychotic_elderly_outlier',
        r.rate,
        4::SMALLINT,
        'specialty=' || r.prscrbr_type,
        r.peer_percentile,
        '/risk/provider/' || r.npi || '?signal=antipsychotic_elderly_outlier'
    FROM ranked r
    WHERE r.bucket_n >= v_min_bucket
      AND r.peer_percentile >= v_tail;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_signal_antipsychotic_elderly_outlier(CHAR(4)) IS
    'FRAUD-F8 prospective detector: emit antipsychotic_elderly_outlier '
    'observations for the cycle (= CMS data_year). Flags Part D prescribers in '
    'the extreme upper tail (CUME_DIST >= antipsychotic_elderly_tail_pctile) of '
    'their specialty peer group on the share of elderly (>=65) beneficiaries '
    'receiving antipsychotics, gated by an elderly-population floor and a '
    'minimum specialty-peer count (all from ref.platform_constants). Idempotent '
    'DELETE+INSERT on its own (cycle, signal_id) slice. Returns rows inserted; '
    '0 when no enriched CMS Part D data is loaded for the cycle.';


-- ----------------------------------------------------------------------------
-- 3. fraud_signal_config row
-- ----------------------------------------------------------------------------
INSERT INTO derived.fraud_signal_config
    (signal_id, signal_family, min_actionable_threshold, comment)
VALUES (
    'antipsychotic_elderly_outlier',
    'cms_utilization',
    0,
    'CMS Part D prescriber in the extreme upper tail (top 1%) of its specialty '
    'peer group on the share of elderly (>=65) beneficiaries receiving '
    'antipsychotics, with an elderly-population floor and minimum specialty-peer '
    'count (all from ref.platform_constants). Distributional (CUME_DIST) signal; '
    'severity 4 (HIGH lead). raw_value = the elderly-antipsychotic rate (%). '
    'Nursing-home chemical-restraint / medically-unnecessary-prescribing lead.'
)
ON CONFLICT (signal_id) DO UPDATE SET
    signal_family            = EXCLUDED.signal_family,
    min_actionable_threshold = EXCLUDED.min_actionable_threshold,
    comment                  = EXCLUDED.comment,
    updated_at               = now();


-- ----------------------------------------------------------------------------
-- 4. Master refresher: TIER 7 gains a 3rd CMS-utilization signal; 26 -> 27
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
    -- TIER 7: CMS-utilization (peer-relative outlier) signals (3)
    -- ----------------------------------------------------------------
    SELECT derived.refresh_signal_opioid_prescribing_outlier(p_cycle)       INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_services_per_beneficiary_outlier(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    -- New as of mig 115: elderly-antipsychotic chemical-restraint outlier.
    -- Returns 0 until the enriched CMS Part D columns are loaded for the cycle.
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

    RETURN n_total;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_all_fraud_signal_observations(CHAR(4)) IS
'Master fraud-signal refresher. Invokes all 27 seeded signal refreshers in '
'substrate-dependency tier order (FEC -> LEIE -> SAM -> USAspending -> CMS '
'federal-exclusion -> NJ-state-exclusion -> CMS-utilization -> NPPES '
'identity-resolution recall -> Open-Payments conflict-of-interest) for the '
'given cycle. Each per-signal refresher is an idempotent DELETE+INSERT slice '
'returning INT (rows inserted); the master returns SUM. Refreshers against '
'empty/cycle-mismatched substrate safely return 0. Mig 115 raises the count '
'from 26 to 27 by adding antipsychotic_elderly_outlier (TIER 7, '
'CMS-utilization elderly chemical-restraint lead).';


COMMIT;
