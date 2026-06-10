-- ============================================================================
-- Migration: 109_fraud_provider_excluded_billing_partb
--
-- TIER 5 / FRAUD-F7 (signal slice): provider_excluded_billing_partb.
--
-- THE SIGNAL
-- ----------
-- The Part-B companion to provider_excluded_billing (mig 101). An
-- HHS-OIG-excluded provider (active LEIE entry with a real NPI) who is
-- nonetheless present in CMS Medicare Physician & Other Practitioners
-- (Part B) data for a year in which the exclusion was already in effect.
-- Same payment-prohibition logic, same exact-NPI precision, different
-- billing roster:
--
--   * mig 101 covers PRESCRIBERS  (raw.cms_partd_prescriber, Part D).
--   * mig 109 covers PRACTITIONERS (raw.cms_physician_provider, Part B).
--
-- Together they give the federal-exclusion-billing signal full biller
-- coverage, exactly as mig 102's substrate header anticipated. A provider
-- who is excluded AND bills under BOTH parts legitimately fires both
-- signals; the L3 engine stacks them (worse conduct -> higher score).
--
-- THE PRECISION GUARD (identical to mig 101)
-- ------------------------------------------
-- The exclusion must have been in effect by the END of the data year and
-- not yet reinstated:
--   excldate_d <= make_date(year, 12, 31)
--   AND (reindate_d IS NULL OR reindate_d > make_date(year, 12, 31))
-- and we consider only LEIE rows STILL active today (derived.v_leie_active).
--
-- ENTITY / RAW_VALUE / SEVERITY
-- -----------------------------
-- entity_kind='provider' (NPI). cycle = CMS data_year. peer_bucket =
-- 'kind=provider'. peer_percentile = 1 - (n_matched / n_part_b_billers),
-- mirroring mig 101's rate-based binary. raw_value = Tot_Mdcr_Pymt_Amt
-- (Medicare PAID amount) -- the Part-B analog of Part-D's gross drug cost;
-- it is the federal-payment exposure for analyst triage. severity 5
-- (payment-prohibition overlap, 42 USC 1320a-7a).
--
-- WHAT THIS MIGRATION SHIPS
-- -------------------------
-- 0. ref.formula_version row.
-- 1. derived.refresh_signal_provider_excluded_billing_partb(CHAR(4)).
-- 2. derived.fraud_signal_config row (family = leie_bearing, exists).
-- 3. Master refresher rewired: TIER 5 now 2 signals; 23 -> 24.
-- 4. derived.v_entity_fraud_evidence: provider_meta widened to resolve a
--    provider's display name + NJ flag from EITHER CMS roster (Part D
--    preferred, Part B fallback) so Part-B-only providers (this signal,
--    services_per_beneficiary_outlier) render a real name, not a bare NPI.
--    DISTINCT ON keeps exactly one identity row per (npi, data_year), so
--    the "one row per fired signal" contract is preserved.
--
-- entity_kind 'provider' and signal_family 'leie_bearing' already exist
-- (mig 101). Companion seed 046 ships the evidence-card reference rows.
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
    '2.8.9-fraud-provider-excluded-billing-partb-v1',
    'Pillar 2 (civic integrity) FRAUD-F7 signal slice. Adds '
    'provider_excluded_billing_partb: an active HHS-OIG LEIE exclusion '
    '(with a real NPI) present in CMS Medicare Physician & Other '
    'Practitioners (Part B) data for a year in which the exclusion was '
    'already in effect. Exact NPI equijoin; severity 5 (payment-prohibition '
    'overlap, 42 USC 1320a-7a). raw_value = Tot_Mdcr_Pymt_Amt (Medicare '
    'paid amount). Part-B companion to 2.8.1 provider_excluded_billing. '
    'Also widens provider_meta in v_entity_fraud_evidence to resolve '
    'provider identity from Part D OR Part B.',
    '2026-06-09'::DATE,
    'Stacks on 2.8.8-nppes-substrate-v1.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ----------------------------------------------------------------------------
-- 1. Refresher: derived.refresh_signal_provider_excluded_billing_partb
--
-- Structurally identical to refresh_signal_provider_excluded_billing (mig
-- 101) but joins LEIE against raw.cms_physician_provider (Part B) and uses
-- tot_mdcr_pymt_amt as the dollar exposure carried in raw_value.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_signal_provider_excluded_billing_partb(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
    v_year     INT  := CAST(p_cycle AS INT);
    v_year_end DATE := make_date(CAST(p_cycle AS INT), 12, 31);
BEGIN
    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'provider_excluded_billing_partb';

    WITH leie AS (
        -- Active LEIE exclusions with a real NPI, in effect by year-end and
        -- not yet reinstated. DISTINCT ON collapses the (rare) case of
        -- multiple active rows sharing one NPI to the freshest exclusion.
        SELECT DISTINCT ON (npi)
            npi,
            record_hash,
            excldate_d,
            excltype
        FROM derived.v_leie_active
        WHERE npi ~ '^[0-9]{10}$'
          AND npi <> '0000000000'
          AND excldate_d IS NOT NULL
          AND excldate_d <= v_year_end
          AND (reindate_d IS NULL OR reindate_d > v_year_end)
        ORDER BY npi, excldate_d DESC NULLS LAST
    ),
    partb AS (
        -- Part B practitioner population for the data year (valid NPI only).
        SELECT npi, tot_mdcr_pymt_amt
        FROM raw.cms_physician_provider
        WHERE data_year = v_year
          AND npi ~ '^[0-9]{10}$'
          AND npi <> '0000000000'
    ),
    matches AS (
        SELECT
            partb.npi,
            partb.tot_mdcr_pymt_amt,
            leie.record_hash
        FROM partb
        JOIN leie USING (npi)
    ),
    pop AS (
        SELECT COUNT(*)::NUMERIC AS n_in_bucket FROM partb
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
        'provider_excluded_billing_partb',
        COALESCE(m.tot_mdcr_pymt_amt, 0::NUMERIC),
        5::SMALLINT,
        'kind=provider',
        GREATEST(
            0::NUMERIC,
            1::NUMERIC - (f.n_flagged / NULLIF(pop.n_in_bucket, 0))
        ),
        '/risk/provider/' || m.npi
            || '?signal=provider_excluded_billing_partb&leie=' || m.record_hash
    FROM matches m
    CROSS JOIN pop
    CROSS JOIN flag f;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_signal_provider_excluded_billing_partb(CHAR(4)) IS
    'FRAUD-F7: emit provider_excluded_billing_partb observations for the '
    'given cycle (= CMS data_year). Exact NPI join between active LEIE '
    'exclusions (in effect by year-end, not reinstated) and '
    'raw.cms_physician_provider (Part B). Part-B companion to '
    'refresh_signal_provider_excluded_billing. Idempotent DELETE+INSERT on '
    'its own (cycle, signal_id) slice. Returns rows inserted; 0 when no CMS '
    'Part B data is loaded for the cycle.';


-- ----------------------------------------------------------------------------
-- 2. fraud_signal_config row (leie_bearing family already whitelisted)
-- ----------------------------------------------------------------------------
INSERT INTO derived.fraud_signal_config
    (signal_id, signal_family, min_actionable_threshold, comment)
VALUES (
    'provider_excluded_billing_partb',
    'leie_bearing',
    0,
    'HHS-OIG-excluded provider (active LEIE entry with a real NPI) present '
    'in CMS Medicare Part B (Physician & Other Practitioners) data for a '
    'year in which the exclusion was in effect. Exact NPI match; severity 5 '
    '(payment-prohibition overlap, 42 USC 1320a-7a). raw_value = '
    'Tot_Mdcr_Pymt_Amt (Medicare paid amount). Part-B companion to '
    'provider_excluded_billing.'
)
ON CONFLICT (signal_id) DO UPDATE SET
    signal_family            = EXCLUDED.signal_family,
    min_actionable_threshold = EXCLUDED.min_actionable_threshold,
    comment                  = EXCLUDED.comment,
    updated_at               = now();


-- ----------------------------------------------------------------------------
-- 3. Master refresher: TIER 5 now 2 signals; 23 -> 24
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

    -- New as of mig 109: Part-B companion (LEIE x CMS Part B practitioners).
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

    RETURN n_total;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_all_fraud_signal_observations(CHAR(4)) IS
'Master fraud-signal refresher. Invokes all 24 seeded signal refreshers in '
'substrate-dependency tier order (FEC -> LEIE -> SAM -> USAspending -> CMS '
'federal-exclusion -> NJ-state-exclusion -> CMS-utilization) for the given '
'cycle. Each per-signal refresher is an idempotent DELETE+INSERT slice '
'returning INT (rows inserted); the master returns SUM. Refreshers against '
'empty/cycle-mismatched substrate safely return 0. Mig 109 raises the count '
'from 23 to 24 by adding provider_excluded_billing_partb (TIER 5, CMS '
'Part-B federal-exclusion overlap).';


-- ----------------------------------------------------------------------------
-- 4. derived.v_entity_fraud_evidence: provider_meta resolves identity from
--    Part D OR Part B (Part D preferred). Only the provider_meta CTE changes;
--    the SELECT output column list/order is byte-for-byte identical to mig 101
--    (CREATE OR REPLACE VIEW forbids column reordering).
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.v_entity_fraud_evidence AS
WITH cand_meta AS (
    SELECT
        cycle,
        cand_id,
        cand_name,
        cand_office,
        cand_office_st,
        cand_office_district,
        cand_pty_affiliation,
        cand_ici,
        cand_status,
        cand_election_yr,
        (cand_office_st = 'NJ')                                  AS is_nj
    FROM raw.fec_candidate
),
cmte_meta AS (
    SELECT
        cycle,
        cmte_id,
        cmte_nm,
        cmte_st,
        cmte_city,
        cmte_zip,
        tres_nm,
        cand_id                                                  AS pcc_cand_id,
        (cmte_st = 'NJ')                                         AS is_nj
    FROM raw.fec_committee
),
treas_meta AS (
    SELECT
        cycle,
        UPPER(TRIM(tres_nm))                                     AS treasurer_id,
        BOOL_OR(cmte_st = 'NJ')                                  AS is_nj,
        COUNT(DISTINCT cmte_id)                                  AS n_committees_treasured,
        COUNT(DISTINCT cmte_id) FILTER (WHERE cmte_st = 'NJ')    AS n_nj_committees_treasured
    FROM raw.fec_committee
    WHERE tres_nm IS NOT NULL AND tres_nm <> ''
    GROUP BY 1, 2
),
nj_state_meta AS (
    SELECT
        candidate_id                                              AS nj_candidate_id,
        full_name                                                 AS nj_full_name,
        TRUE                                                      AS is_nj
    FROM ref.nj_state_candidate
),
provider_meta AS (
    -- NPI-keyed healthcare provider identity. Resolve display_name + is_nj
    -- from EITHER CMS roster keyed on (data_year, npi): Part D prescribers
    -- and Part B practitioners. Part D is preferred (pref=1) when an NPI
    -- appears in both for the year; Part B (pref=2) is the fallback so a
    -- Part-B-only excluded biller / utilization outlier still renders a
    -- real name. DISTINCT ON keeps exactly ONE identity row per
    -- (npi, data_year), preserving "one row per fired signal".
    SELECT DISTINCT ON (provider_npi, provider_data_year)
        provider_npi,
        provider_data_year,
        provider_name,
        is_nj
    FROM (
        SELECT
            npi                                                   AS provider_npi,
            data_year                                             AS provider_data_year,
            NULLIF(TRIM(
                COALESCE(prscrbr_first_name, '') || ' ' ||
                COALESCE(prscrbr_last_org_name, '')
            ), '')                                                AS provider_name,
            (prscrbr_state_abrvtn = 'NJ')                         AS is_nj,
            1                                                     AS pref
        FROM raw.cms_partd_prescriber
        UNION ALL
        SELECT
            npi                                                   AS provider_npi,
            data_year                                             AS provider_data_year,
            NULLIF(TRIM(
                COALESCE(prvdr_first_name, '') || ' ' ||
                COALESCE(prvdr_last_org_name, '')
            ), '')                                                AS provider_name,
            (prvdr_state_abrvtn = 'NJ')                           AS is_nj,
            2                                                     AS pref
        FROM raw.cms_physician_provider
    ) u
    ORDER BY provider_npi, provider_data_year, pref
)
SELECT
    o.cycle,
    o.entity_kind,
    o.entity_id,
    o.signal_id,
    o.raw_value,
    COALESCE(sc.severity_level, o.severity)                      AS severity,
    o.peer_bucket,
    o.peer_percentile,
    o.materialized_at,

    CASE o.entity_kind
        WHEN 'candidate'          THEN COALESCE(cand.is_nj,  FALSE)
        WHEN 'committee'          THEN COALESCE(cmte.is_nj,  FALSE)
        WHEN 'treasurer'          THEN COALESCE(treas.is_nj, FALSE)
        WHEN 'address'            THEN (SPLIT_PART(o.entity_id, '|', 3) = 'NJ')
        WHEN 'nj_state_candidate' THEN COALESCE(nj.is_nj, TRUE)
        WHEN 'provider'           THEN COALESCE(prov.is_nj, FALSE)
        ELSE FALSE
    END                                                          AS is_nj,

    CASE o.entity_kind
        WHEN 'candidate'          THEN cand.cand_name
        WHEN 'committee'          THEN cmte.cmte_nm
        WHEN 'treasurer'          THEN o.entity_id
        WHEN 'address'            THEN SPLIT_PART(o.entity_id, '|', 1)
                                       || COALESCE(', ' || SPLIT_PART(o.entity_id, '|', 2), '')
                                       || COALESCE(' '  || SPLIT_PART(o.entity_id, '|', 3), '')
                                       || COALESCE(' '  || SPLIT_PART(o.entity_id, '|', 4), '')
        WHEN 'nj_state_candidate' THEN nj.nj_full_name
        WHEN 'provider'           THEN COALESCE(prov.provider_name, o.entity_id)
        ELSE o.entity_id
    END                                                          AS display_name,

    cand.cand_office                                             AS office_code,
    cand.cand_office_st                                          AS office_state,
    cand.cand_office_district                                    AS office_district,
    cand.cand_pty_affiliation                                    AS office_party,
    cand.cand_ici                                                AS office_incumbent_status,
    cand.cand_election_yr                                        AS office_election_year,

    treas.n_committees_treasured                                 AS treasurer_n_committees,
    treas.n_nj_committees_treasured                              AS treasurer_n_nj_committees,

    cmte.cmte_st                                                 AS committee_state,
    cmte.cmte_city                                               AS committee_city,
    cmte.tres_nm                                                 AS committee_treasurer_name,
    cmte.pcc_cand_id                                             AS committee_pcc_candidate_id,

    he.rule_text                                                 AS rule_text,
    he.citation_authority                                        AS citation_authority,
    he.citation_section                                          AS citation_section,
    he.citation_url                                              AS citation_url,

    REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
        COALESCE(he.plain_english_template, ''),
        '{{entity_id}}',       o.entity_id),
        '{{cycle}}',           o.cycle),
        '{{raw_value}}',       COALESCE(o.raw_value::TEXT, '')),
        '{{peer_percentile}}', COALESCE(ROUND(o.peer_percentile * 100, 1)::TEXT, '')),
        '{{entity_kind}}',     COALESCE(o.entity_kind, '')),
        '{{peer_bucket}}',     COALESCE(o.peer_bucket, ''))
                                                                 AS rendered_explanation,

    sc.calibration_basis                                         AS severity_basis,
    sc.precedent_url                                             AS severity_precedent_url,
    sc.precedent_summary                                         AS severity_precedent_summary,

    REPLACE(REPLACE(
        COALESCE(eut.url_template, o.evidence_url),
        '{{entity_id}}', o.entity_id),
        '{{cycle}}',     o.cycle)
                                                                 AS upstream_verify_url,
    eut.button_label                                             AS upstream_verify_label,
    eut.upstream_source                                          AS upstream_source,

    he.formula_version                                           AS formula_version
FROM   derived.fraud_signal_observation        o
LEFT JOIN cand_meta                            cand
       ON o.entity_kind = 'candidate'
      AND cand.cycle    = o.cycle
      AND cand.cand_id  = o.entity_id
LEFT JOIN cmte_meta                            cmte
       ON o.entity_kind = 'committee'
      AND cmte.cycle    = o.cycle
      AND cmte.cmte_id  = o.entity_id
LEFT JOIN treas_meta                           treas
       ON o.entity_kind     = 'treasurer'
      AND treas.cycle       = o.cycle
      AND treas.treasurer_id = UPPER(TRIM(o.entity_id))
LEFT JOIN nj_state_meta                        nj
       ON o.entity_kind     = 'nj_state_candidate'
      AND nj.nj_candidate_id = o.entity_id
LEFT JOIN provider_meta                        prov
       ON o.entity_kind          = 'provider'
      AND prov.provider_npi      = o.entity_id
      AND prov.provider_data_year = o.cycle::INT
LEFT JOIN ref.fraud_signal_human_explanation        he   ON he.signal_id  = o.signal_id
LEFT JOIN ref.fraud_signal_severity_calibration     sc   ON sc.signal_id  = o.signal_id
LEFT JOIN ref.fraud_signal_evidence_url_template    eut  ON eut.signal_id = o.signal_id;

COMMENT ON VIEW derived.v_entity_fraud_evidence IS
    'Canonical join from fraud_signal_observation -> rendered plain-English '
    'explanation + federal-authority citation + severity precedent + display '
    'metadata + NJ-relevance + upstream-verify URL. One row per fired signal. '
    'Mig 109 widens provider_meta to resolve provider identity from Part D OR '
    'Part B (Part D preferred; DISTINCT ON per (npi, data_year)).';


COMMIT;
