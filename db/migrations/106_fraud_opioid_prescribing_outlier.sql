-- ============================================================================
-- Migration: 106_fraud_opioid_prescribing_outlier
--
-- FRAUD-F7 Phase-2 (signal slice): opioid_prescribing_outlier.
--
-- THE SIGNAL
-- ----------
-- A CMS Medicare Part D prescriber whose opioid-prescribing rate
-- (CMS-published Opioid_Prscrbr_Rate = opioid claims / total claims, %)
-- sits in the extreme upper tail of its OWN SPECIALTY peer group for the
-- data year. This is the platform's first *distributional* (peer-relative)
-- healthcare signal -- as opposed to the binary exclusion-overlap signals
-- (mig 101 / 105) -- and the first whose evidentiary weight is statistical
-- rarity rather than an exact list match. Extreme opioid prescribing is a
-- recognized pill-mill / diversion / patient-harm lead.
--
-- WHY SPECIALTY-RELATIVE (not an absolute rate cutoff)
-- ---------------------------------------------------
-- Opioid prescribing rates vary by an order of magnitude across
-- specialties: an interventional-pain-management physician legitimately
-- prescribes opioids far more often than a pediatrician. An absolute-rate
-- threshold would drown the queue in pain specialists and miss a
-- pediatrician at a pill mill. The signal therefore ranks each prescriber
-- WITHIN its specialty bucket (CUME_DIST partitioned by prscrbr_type) and
-- flags only the extreme tail. This is the same "is this entity in the
-- extreme tail of its peers" semantics the FEC distributional signals use
-- (mig 051): CUME_DIST, not PERCENT_RANK.
--
-- NO MAGIC NUMBERS (verifiable-data invariant)
-- --------------------------------------------
-- The three tuning constants are NOT inlined. They live in
-- ref.platform_constants with citation_text + formula_version + effective
-- date, read at refresh time via derived.f_platform_constant():
--   * opioid_outlier_tail_pctile = 0.99  -- top 1% within specialty
--   * opioid_outlier_min_claims  = 50    -- volume floor (rate stability)
--   * opioid_outlier_min_bucket  = 100   -- min same-specialty peers
-- Changing any of these is a new platform_constants row under a new
-- formula_version, never an edit to this function body.
--
-- ENTITY / BUCKET / PERCENTILE / SEVERITY
-- ---------------------------------------
-- entity_kind = 'provider' (NPI). cycle = CMS data_year. peer_bucket =
-- 'specialty=<prscrbr_type>'. peer_percentile = within-specialty CUME_DIST
-- of the opioid rate. raw_value = the opioid rate (%) itself for analyst
-- triage. severity 4 (HIGH), basis empirical_pctile -- a strong lead, not
-- an adjudicated violation (legit high-volume pain specialists exist).
--
-- WHAT THIS MIGRATION SHIPS
-- -------------------------
-- 0. ref.formula_version row.
-- 1. ref.platform_constants: three tuning constants (provenanced).
-- 2. fraud_signal_config signal_family CHECK widened: + 'cms_utilization'.
-- 3. evidence_url_template upstream_source CHECK widened: + 'CMS.gov'.
-- 4. derived.refresh_signal_opioid_prescribing_outlier(CHAR(4)).
-- 5. derived.fraud_signal_config row (family = cms_utilization).
-- 6. Master refresher rewired: TIER 7 (CMS-utilization); 21 -> 22.
--
-- Companion seed 044 ships the evidence-card reference rows.
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
    '2.8.6-fraud-opioid-prescribing-outlier-v1',
    'Pillar 2 (civic integrity) FRAUD-F7 Phase-2 signal slice. Adds the '
    'opioid_prescribing_outlier distributional signal: a CMS Part D '
    'prescriber in the extreme upper tail (top 1%) of its specialty peer '
    'group for the data year on the CMS-published opioid-prescribing rate, '
    'subject to a claim-volume floor and a minimum specialty-peer count. '
    'First peer-relative (CUME_DIST) healthcare signal; first cms_utilization '
    'family. Tuning constants live in ref.platform_constants (no inline '
    'magic numbers). Severity 4 (HIGH lead), basis empirical_pctile.',
    '2026-06-09'::DATE,
    'Stacks on 2.8.5-fraud-state-excluded-provider-billing-v1.'
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
    'opioid_outlier_tail_pctile',
    0.99,
    'Within-specialty CUME_DIST cutoff above which a Part D prescriber is '
    'flagged as an extreme opioid-prescribing outlier (top 1%).',
    'https://data.cms.gov/provider-summary-by-type-of-service/medicare-part-d-prescribers',
    'Empirical platform calibration (analyst-set). Flags the top 1% of the '
    'CMS-published Opioid_Prscrbr_Rate WITHIN each specialty (prscrbr_type) '
    'peer group, so that high-baseline specialties (e.g. pain management) '
    'are judged against their own peers rather than an absolute rate. '
    'Severity basis empirical_pctile; reviewed against the CMS Medicare '
    'Part D Prescribers opioid-rate distribution.',
    '2.8.6-fraud-opioid-prescribing-outlier-v1',
    '2026-06-09'
),
(
    'opioid_outlier_min_claims',
    50,
    'Minimum total Part D claims for a prescriber to be eligible for the '
    'opioid-outlier signal (suppresses small-denominator rate artifacts).',
    'https://data.cms.gov/provider-summary-by-type-of-service/medicare-part-d-prescribers',
    'Empirical platform calibration (analyst-set). Below ~50 total claims '
    'the opioid-prescribing rate is a small-denominator artifact (a handful '
    'of opioid claims can yield a 100% rate). The floor restricts the signal '
    'to prescribers with a stable enough denominator. Basis empirical_pctile.',
    '2.8.6-fraud-opioid-prescribing-outlier-v1',
    '2026-06-09'
),
(
    'opioid_outlier_min_bucket',
    100,
    'Minimum number of same-specialty peers for a specialty bucket to be '
    'eligible (so a top-1% tail is statistically meaningful, not a '
    'tiny-sample artifact).',
    'https://data.cms.gov/provider-summary-by-type-of-service/medicare-part-d-prescribers',
    'Empirical platform calibration (analyst-set). A specialty with fewer '
    'than ~100 eligible prescribers cannot yield a meaningful top-1% tail '
    '(CUME_DIST on a handful of rows is noise). Buckets below this count are '
    'skipped entirely. Basis empirical_pctile.',
    '2.8.6-fraud-opioid-prescribing-outlier-v1',
    '2026-06-09'
)
ON CONFLICT (constant_id, formula_version) DO UPDATE SET
    value         = EXCLUDED.value,
    description   = EXCLUDED.description,
    source_url    = EXCLUDED.source_url,
    citation_text = EXCLUDED.citation_text,
    effective_date = EXCLUDED.effective_date;


-- ----------------------------------------------------------------------------
-- 2. signal_family CHECK widening: + 'cms_utilization'
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
    'state_exclusion',
    'cms_utilization'
));

COMMENT ON CONSTRAINT fraud_signal_config_signal_family_check
ON derived.fraud_signal_config IS
    'Whitelist of signal_family values. Seven families as of migration 106. '
    'cms_utilization (added mig 106) covers peer-relative CMS Medicare '
    'utilization-outlier signals (e.g. extreme opioid prescribing). The '
    'diversity bonus in derived.fraud_risk_score rewards entities firing on '
    'signals across distinct families.';


-- ----------------------------------------------------------------------------
-- 3. evidence_url_template upstream_source CHECK widening: + 'CMS.gov'
--    The opioid signal's upstream-verify button links to the CMS Part D
--    Prescribers data so an analyst can confirm the underlying rate.
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
    'NJ.gov',
    'CMS.gov'
));


-- ----------------------------------------------------------------------------
-- 4. Refresher: derived.refresh_signal_opioid_prescribing_outlier
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_signal_opioid_prescribing_outlier(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted   INT;
    v_year       INT     := CAST(p_cycle AS INT);
    v_tail       NUMERIC := derived.f_platform_constant('opioid_outlier_tail_pctile');
    v_min_claims NUMERIC := derived.f_platform_constant('opioid_outlier_min_claims');
    v_min_bucket NUMERIC := derived.f_platform_constant('opioid_outlier_min_bucket');
BEGIN
    -- Substrate-honest: a missing constant is loud, never a silent default.
    IF v_tail IS NULL OR v_min_claims IS NULL OR v_min_bucket IS NULL THEN
        RAISE EXCEPTION
            'opioid_prescribing_outlier: missing ref.platform_constants '
            '(tail=%, min_claims=%, min_bucket=%). Seed mig 106 constants.',
            v_tail, v_min_claims, v_min_bucket
        USING ERRCODE = 'no_data_found';
    END IF;

    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'opioid_prescribing_outlier';

    WITH src AS (
        -- Eligible prescribers: valid NPI, a real specialty, a non-null
        -- opioid rate, and at least the claim-volume floor.
        SELECT
            npi,
            prscrbr_type,
            opioid_prscrbr_rate AS rate
        FROM raw.cms_partd_prescriber
        WHERE data_year = v_year
          AND npi ~ '^[0-9]{10}$'
          AND npi <> '0000000000'
          AND prscrbr_type IS NOT NULL
          AND prscrbr_type <> ''
          AND opioid_prscrbr_rate IS NOT NULL
          AND tot_clms IS NOT NULL
          AND tot_clms >= v_min_claims
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
        'opioid_prescribing_outlier',
        r.rate,
        4::SMALLINT,
        'specialty=' || r.prscrbr_type,
        r.peer_percentile,
        '/risk/provider/' || r.npi || '?signal=opioid_prescribing_outlier'
    FROM ranked r
    WHERE r.bucket_n >= v_min_bucket
      AND r.peer_percentile >= v_tail;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_signal_opioid_prescribing_outlier(CHAR(4)) IS
    'FRAUD-F7 Phase-2: emit opioid_prescribing_outlier observations for the '
    'given cycle (= CMS data_year). Flags Part D prescribers in the extreme '
    'upper tail (CUME_DIST >= opioid_outlier_tail_pctile) of their specialty '
    'peer group on the CMS opioid-prescribing rate, gated by a claim-volume '
    'floor and a minimum specialty-peer count (all from ref.platform_constants). '
    'Idempotent DELETE+INSERT on its own (cycle, signal_id) slice. Returns '
    'rows inserted; 0 when no CMS data is loaded for the cycle.';


-- ----------------------------------------------------------------------------
-- 5. fraud_signal_config row
--
-- family = cms_utilization. min_actionable_threshold 0: the actionable gate
-- (specialty-relative tail + volume + bucket-size) is applied at refresh
-- time, so every emitted row is already in the actionable tail.
-- ----------------------------------------------------------------------------
INSERT INTO derived.fraud_signal_config
    (signal_id, signal_family, min_actionable_threshold, comment)
VALUES (
    'opioid_prescribing_outlier',
    'cms_utilization',
    0,
    'CMS Part D prescriber in the extreme upper tail (top 1%) of its '
    'specialty peer group on the CMS opioid-prescribing rate, with a '
    'claim-volume floor and minimum specialty-peer count (all from '
    'ref.platform_constants). Distributional (CUME_DIST) signal; severity 4 '
    '(HIGH lead). raw_value = the opioid-prescribing rate (%).'
)
ON CONFLICT (signal_id) DO UPDATE SET
    signal_family            = EXCLUDED.signal_family,
    min_actionable_threshold = EXCLUDED.min_actionable_threshold,
    comment                  = EXCLUDED.comment,
    updated_at               = now();


-- ----------------------------------------------------------------------------
-- 6. Master refresher: add TIER 7 (CMS-utilization); 21 -> 22
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
    -- TIER 7: CMS-utilization (peer-relative outlier) signals (1)
    -- ----------------------------------------------------------------
    -- New as of mig 106: specialty-relative opioid-prescribing tail outlier.
    -- Returns 0 when no CMS Part D data is loaded for the cycle.
    SELECT derived.refresh_signal_opioid_prescribing_outlier(p_cycle)       INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    RETURN n_total;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_all_fraud_signal_observations(CHAR(4)) IS
'Master fraud-signal refresher. Invokes all 22 seeded signal refreshers in '
'substrate-dependency tier order (FEC -> LEIE -> SAM -> USAspending -> CMS '
'federal-exclusion -> NJ-state-exclusion -> CMS-utilization) for the given '
'cycle. Each per-signal refresher is an idempotent DELETE+INSERT slice '
'returning INT (rows inserted); the master returns SUM. Refreshers against '
'empty/cycle-mismatched substrate safely return 0. Mig 106 raises the count '
'from 21 to 22 by adding opioid_prescribing_outlier (TIER 7, CMS-utilization).';


COMMIT;
