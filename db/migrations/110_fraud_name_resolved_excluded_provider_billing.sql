-- ============================================================================
-- Migration: 110_fraud_name_resolved_excluded_provider_billing
--
-- TIER 8 / FRAUD-F7 Phase-3 (identity-spine RECALL): the payoff of NPPES.
--
-- THE PROBLEM THIS SOLVES
-- -----------------------
-- The exact-NPI exclusion-billing signals (mig 101 Part D, mig 109 Part B)
-- can only fire when the HHS-OIG LEIE row CARRIES an NPI. A large fraction
-- of LEIE individual exclusions publish a name but a BLANK NPI -- those
-- excluded providers are invisible to an exact-NPI join even if they are
-- actively billing Medicare under an NPI that NPPES knows about. This is a
-- recall gap: the worst actors (long-excluded, pre-NPI-era) are exactly the
-- ones with no NPI on the list.
--
-- THE RESOLUTION (NPPES as the identity spine)
-- --------------------------------------------
-- NPPES maps every NPI -> legal name + practice state. We canonicalize both
-- sides to the platform's existing 'LAST|FIRST' key
-- (derived.f_canonical_lastfirst_split, mig 054) and resolve a name-only
-- LEIE exclusion to a concrete NPI when the (canonical_key, practice_state)
-- maps to EXACTLY ONE NPPES individual. That uniqueness guard is the whole
-- precision story:
--
--   * If two NJ providers share "SMITH|JOHN", the resolution is AMBIGUOUS
--     and we emit NOTHING -- we never guess which John Smith.
--   * State must match (LEIE.state == NPPES practice_state) so a national
--     name collision cannot resolve across states.
--   * NPPES individuals only (entity_type_code = 1); org names use a
--     different canonicalization and are out of scope here.
--
-- INFERRED IDENTITY => LOWER SEVERITY (honest provenance)
-- ------------------------------------------------------
-- This match is NAME+STATE inferred, not an exact NPI equijoin. Identity is
-- therefore lower-confidence than the severity-5 exact-match signals. The
-- signal is SEVERITY 3 (MODERATE lead) with a NEW calibration_basis
-- 'inferred_identity' -- the verifiable-data invariant forbids dressing an
-- inferred match up as an adjudicated one. raw_value carries the combined
-- Part D + Part B Medicare exposure for analyst triage.
--
-- WHAT THIS MIGRATION SHIPS
-- -------------------------
-- 0. ref.formula_version row.
-- 1. derived.v_leie_name_resolved_npi -- REUSABLE resolution view (name-only
--    active LEIE individuals -> unique NPPES NPI + state).
-- 2. calibration_basis CHECK widened: + 'inferred_identity'.
-- 3. derived.refresh_signal_name_resolved_excluded_provider_billing(CHAR(4)).
-- 4. derived.fraud_signal_config row (family = leie_bearing).
-- 5. Master refresher rewired: NEW TIER 8 (identity-resolution recall);
--    24 -> 25 signals.
--
-- entity_kind 'provider' and signal_family 'leie_bearing' already exist.
-- The evidence view needs NO change: the resolved NPI is (by construction) a
-- CMS biller, so provider_meta (mig 109) resolves its display name + NJ flag
-- from CMS. Companion seed ships the evidence-card reference rows.
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
    '2.9.0-fraud-name-resolved-excluded-provider-billing-v1',
    'Pillar 2 (civic integrity) FRAUD-F7 Phase-3 identity-spine recall signal. '
    'Adds derived.v_leie_name_resolved_npi (name-only active LEIE individual '
    'exclusions resolved to a UNIQUE NPPES NPI by canonical LAST|FIRST + '
    'practice state) and the name_resolved_excluded_provider_billing signal: '
    'a name+state-resolved excluded provider present in CMS Medicare billing '
    '(Part D OR Part B) for a year in which the exclusion was in effect. '
    'Inferred identity (NOT an exact NPI match) -> SEVERITY 3, NEW '
    'calibration_basis inferred_identity. raw_value = combined Part D + Part B '
    'Medicare exposure.',
    '2026-06-09'::DATE,
    'Stacks on 2.8.9-fraud-provider-excluded-billing-partb-v1. Requires the '
    'NPPES substrate (mig 108).'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ----------------------------------------------------------------------------
-- 1. Resolution view: derived.v_leie_name_resolved_npi
--
-- Name-only active LEIE individual exclusions resolved to a UNIQUE NPPES NPI.
-- The HAVING COUNT(*) = 1 on the NPPES side is the precision guard: a
-- (canonical_key, state) that maps to more than one NPPES individual is
-- ambiguous and contributes NOTHING.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.v_leie_name_resolved_npi AS
WITH leie_nonpi AS (
    -- Active LEIE individuals WITHOUT a usable NPI (the recall gap), with a
    -- canonical name key and a non-blank state.
    SELECT
        record_hash                                              AS leie_record_hash,
        excldate_d,
        reindate_d,
        UPPER(TRIM(state))                                       AS leie_state,
        derived.f_canonical_lastfirst_split(lastname, firstname) AS canonical_key
    FROM derived.v_leie_individuals_active
    WHERE (npi IS NULL OR npi !~ '^[0-9]{10}$' OR npi = '0000000000')
      AND state IS NOT NULL AND TRIM(state) <> ''
      AND derived.f_canonical_lastfirst_split(lastname, firstname) IS NOT NULL
),
nppes_canon AS (
    -- Active NPPES individuals with a canonical name key + practice state.
    SELECT
        npi,
        UPPER(TRIM(practice_state))                              AS practice_state,
        derived.f_canonical_lastfirst_split(
            provider_last_name, provider_first_name)             AS canonical_key
    FROM derived.v_nppes_provider_active
    WHERE entity_type_code = 1
      AND practice_state IS NOT NULL AND TRIM(practice_state) <> ''
      AND derived.f_canonical_lastfirst_split(
              provider_last_name, provider_first_name) IS NOT NULL
),
nppes_unique AS (
    -- PRECISION GUARD: keep only (canonical_key, state) pairs that resolve to
    -- exactly ONE NPPES individual. Ambiguous names are dropped, never guessed.
    SELECT
        canonical_key,
        practice_state,
        MIN(npi)                                                 AS resolved_npi
    FROM nppes_canon
    GROUP BY canonical_key, practice_state
    HAVING COUNT(*) = 1
)
SELECT
    le.leie_record_hash,
    le.excldate_d,
    le.reindate_d,
    le.canonical_key,
    le.leie_state,
    nu.resolved_npi
FROM leie_nonpi le
JOIN nppes_unique nu
  ON nu.canonical_key  = le.canonical_key
 AND nu.practice_state = le.leie_state;

COMMENT ON VIEW derived.v_leie_name_resolved_npi IS
    'FRAUD-F7 Phase-3 identity spine. Name-only active LEIE individual '
    'exclusions (blank/invalid NPI) resolved to a UNIQUE NPPES NPI by '
    'canonical LAST|FIRST key + practice state. The HAVING COUNT(*)=1 guard '
    'drops ambiguous name+state collisions (no guessing). Carries the LEIE '
    'exclusion dates so consumers can apply a billing-year window guard.';


-- ----------------------------------------------------------------------------
-- 2. calibration_basis CHECK widening: + 'inferred_identity'
--
-- The severity of a name+state-resolved match is set by the INFERRED nature
-- of the identity, not by an enforcement precedent. Reusing 'oig_report'
-- (the exact-match basis) would overstate confidence; the provenance
-- invariant requires an honest, distinct category.
-- ----------------------------------------------------------------------------
ALTER TABLE ref.fraud_signal_severity_calibration
DROP CONSTRAINT IF EXISTS fraud_signal_severity_calibration_basis_chk;

ALTER TABLE ref.fraud_signal_severity_calibration
ADD  CONSTRAINT fraud_signal_severity_calibration_basis_chk
CHECK (calibration_basis IN (
    'fec_mur',           -- FEC Matter Under Review (enforcement action)
    'oig_report',        -- HHS-OIG report or audit
    'doj_filing',        -- DOJ enforcement filing or settlement
    'crs_analysis',      -- Congressional Research Service analysis
    'far_authority',     -- FAR debarment / suspension authority
    'fec_advisory',      -- FEC Advisory Opinion
    'empirical_pctile',  -- analyst calibration vs. NJ-cycle anomaly distribution
    'state_exclusion',   -- state-level exclusion authority (e.g. NJ OSC debarment)
    'inferred_identity'  -- name+state-resolved (NPPES) match; identity inferred
));


-- ----------------------------------------------------------------------------
-- 3. Refresher: derived.refresh_signal_name_resolved_excluded_provider_billing
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_signal_name_resolved_excluded_provider_billing(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
    v_year     INT  := CAST(p_cycle AS INT);
    v_year_end DATE := make_date(CAST(p_cycle AS INT), 12, 31);
BEGIN
    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'name_resolved_excluded_provider_billing';

    WITH resolved AS (
        -- Name-resolved excluded NPIs whose exclusion was in effect by
        -- year-end and not yet reinstated. DISTINCT ON collapses multiple
        -- LEIE records resolving to one NPI to the freshest exclusion.
        SELECT DISTINCT ON (resolved_npi)
            resolved_npi   AS npi,
            leie_record_hash,
            excldate_d
        FROM derived.v_leie_name_resolved_npi
        WHERE excldate_d IS NOT NULL
          AND excldate_d <= v_year_end
          AND (reindate_d IS NULL OR reindate_d > v_year_end)
        ORDER BY resolved_npi, excldate_d DESC NULLS LAST
    ),
    partd AS (
        SELECT npi, COALESCE(tot_drug_cst, 0::NUMERIC) AS amt
        FROM raw.cms_partd_prescriber
        WHERE data_year = v_year
          AND npi ~ '^[0-9]{10}$' AND npi <> '0000000000'
    ),
    partb AS (
        SELECT npi, COALESCE(tot_mdcr_pymt_amt, 0::NUMERIC) AS amt
        FROM raw.cms_physician_provider
        WHERE data_year = v_year
          AND npi ~ '^[0-9]{10}$' AND npi <> '0000000000'
    ),
    billers AS (
        SELECT npi, SUM(amt) AS exposure
        FROM (SELECT npi, amt FROM partd
              UNION ALL
              SELECT npi, amt FROM partb) u
        GROUP BY npi
    ),
    matches AS (
        SELECT b.npi, b.exposure, r.leie_record_hash
        FROM billers b
        JOIN resolved r USING (npi)
    ),
    pop AS (
        SELECT COUNT(*)::NUMERIC AS n_in_bucket FROM billers
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
        'name_resolved_excluded_provider_billing',
        m.exposure,
        3::SMALLINT,
        'kind=provider',
        GREATEST(
            0::NUMERIC,
            1::NUMERIC - (f.n_flagged / NULLIF(pop.n_in_bucket, 0))
        ),
        '/risk/provider/' || m.npi
            || '?signal=name_resolved_excluded_provider_billing&leie='
            || m.leie_record_hash
    FROM matches m
    CROSS JOIN pop
    CROSS JOIN flag f;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_signal_name_resolved_excluded_provider_billing(CHAR(4)) IS
    'FRAUD-F7 Phase-3: emit name_resolved_excluded_provider_billing '
    'observations for the given cycle (= CMS data_year). Resolves name-only '
    'LEIE individual exclusions to a UNIQUE NPPES NPI '
    '(derived.v_leie_name_resolved_npi), then flags those NPIs present in CMS '
    'Part D or Part B billing within the exclusion window. Inferred identity, '
    'severity 3. Idempotent DELETE+INSERT on its own (cycle, signal_id) slice. '
    'Returns rows inserted; 0 when NPPES or CMS substrate is absent for the '
    'cycle.';


-- ----------------------------------------------------------------------------
-- 4. fraud_signal_config row (leie_bearing family already whitelisted)
-- ----------------------------------------------------------------------------
INSERT INTO derived.fraud_signal_config
    (signal_id, signal_family, min_actionable_threshold, comment)
VALUES (
    'name_resolved_excluded_provider_billing',
    'leie_bearing',
    0,
    'Name-only HHS-OIG LEIE individual exclusion resolved via NPPES (unique '
    'canonical LAST|FIRST + practice state) to a concrete NPI that is present '
    'in CMS Medicare billing (Part D OR Part B) within the exclusion window. '
    'Inferred-identity recall companion to the exact-NPI '
    'provider_excluded_billing signals; severity 3 (identity is name+state '
    'inferred, not an exact NPI match). raw_value = combined Medicare exposure.'
)
ON CONFLICT (signal_id) DO UPDATE SET
    signal_family            = EXCLUDED.signal_family,
    min_actionable_threshold = EXCLUDED.min_actionable_threshold,
    comment                  = EXCLUDED.comment,
    updated_at               = now();


-- ----------------------------------------------------------------------------
-- 5. Master refresher: NEW TIER 8 (identity-resolution recall); 24 -> 25
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
    -- TIER 7: CMS-utilization (peer-relative outlier) signals (2)
    -- ----------------------------------------------------------------
    SELECT derived.refresh_signal_opioid_prescribing_outlier(p_cycle)       INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_services_per_beneficiary_outlier(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    -- ----------------------------------------------------------------
    -- TIER 8: NPPES identity-resolution recall signals (1 -- mig 110)
    -- ----------------------------------------------------------------
    SELECT derived.refresh_signal_name_resolved_excluded_provider_billing(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    RETURN n_total;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_all_fraud_signal_observations(CHAR(4)) IS
'Master fraud-signal refresher. Invokes all 25 seeded signal refreshers in '
'substrate-dependency tier order (FEC -> LEIE -> SAM -> USAspending -> CMS '
'federal-exclusion -> NJ-state-exclusion -> CMS-utilization -> NPPES '
'identity-resolution recall) for the given cycle. Each per-signal refresher '
'is an idempotent DELETE+INSERT slice returning INT (rows inserted); the '
'master returns SUM. Refreshers against empty/cycle-mismatched substrate '
'safely return 0. Mig 110 raises the count from 24 to 25 by adding '
'name_resolved_excluded_provider_billing (TIER 8, NPPES identity recall).';


COMMIT;
