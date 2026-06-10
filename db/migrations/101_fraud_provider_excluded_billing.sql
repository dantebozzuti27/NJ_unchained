-- ============================================================================
-- Migration: 101_fraud_provider_excluded_billing
--
-- TIER 4 v3 / FRAUD-F7 (signal slice): provider_excluded_billing.
--
-- THE SIGNAL
-- ----------
-- An HHS-OIG-excluded provider (active LEIE entry with a real NPI) who is
-- nonetheless present in CMS Medicare Part D prescriber data for a year in
-- which the exclusion was already in effect. This is the canonical, highest-
-- precision healthcare-fraud signal on the platform:
--
--   * EXACT NPI equijoin -- unlike every prior LEIE signal (mig 054/059/098),
--     which canonicalize names because FEC/NJ rosters carry no NPI, this joins
--     on the NPI both sides publish. No name collision, no fuzzy match.
--   * Federal payment prohibition: per the OIG Exclusions FAQ and 42 USC
--     1320a-7a, NO federal health-care program payment may be made for items
--     or services furnished, ordered, or prescribed by an excluded individual.
--     An excluded NPI appearing in Part D data overlapping its exclusion
--     window is a payment-prohibition overlap on its face -- severity 5.
--
-- THE ONE PRECISION GUARD (date alignment)
-- ----------------------------------------
-- A provider excluded AFTER a billing year was not excluded during that year,
-- so flagging that year would be a false positive. The refresher therefore
-- requires the exclusion to have been in effect by the END of the data year:
--   excldate_d <= make_date(year, 12, 31)
--   AND (reindate_d IS NULL OR reindate_d > make_date(year, 12, 31))
-- and only considers LEIE rows that are STILL active today (v_leie_active),
-- which is the conservative, high-precision posture.
--
-- ENTITY MODEL
-- ------------
-- Introduces entity_kind = 'provider', keyed on NPI (entity_id = the 10-digit
-- NPI). cycle = the CMS data_year (e.g. '2023'). When the master refresher is
-- invoked for an FEC even-year cycle (2024/2026) with no CMS data loaded for
-- that year, the refresher returns 0 -- substrate-honest.
--
-- WHAT THIS MIGRATION SHIPS
-- -------------------------
-- 1. ref.formula_version row for the signal.
-- 2. entity_kind CHECK widening: + 'provider'.
-- 3. derived.refresh_signal_provider_excluded_billing(CHAR(4)).
-- 4. derived.fraud_signal_config row (family = leie_bearing -- the signal's
--    evidentiary core IS the LEIE exclusion; CMS supplies the "still active"
--    half. Future CMS-utilization outlier signals -- opioid rate, upcoding --
--    will introduce a distinct family in a later migration).
-- 5. Master refresher rewired: TIER 5 (CMS-Medicare-bearing); 19 -> 20 signals.
-- 6. derived.v_entity_fraud_evidence widened with a provider_meta CTE
--    (display_name from CMS prescriber name; is_nj from prescriber state).
--
-- NOT IN THIS MIGRATION: lib/types.ts / lib/queries.ts / app/risk UI + the
-- serving VALID_ENTITY_KINDS are updated in companion code commits; the
-- companion seed 041 ships the three evidence-card reference rows.
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
    '2.8.1-fraud-provider-excluded-billing-v1',
    'Pillar 2 (civic integrity) FRAUD-F7 signal slice. Adds entity_kind '
    'provider (NPI-keyed) and the provider_excluded_billing cross-source '
    'signal: an active HHS-OIG LEIE exclusion (with a real NPI) that is '
    'present in CMS Medicare Part D prescriber data for a year in which the '
    'exclusion was already in effect (excldate <= year-end AND not yet '
    'reinstated). Exact NPI equijoin (no name canonicalization). Severity 5 '
    '(payment-prohibition overlap under 42 USC 1320a-7a). raw_value carries '
    'the gross Part D drug cost for analyst triage; peer_percentile is '
    'rate-based binary within the provider population (rarity of the overlap). '
    'First signal against entity_kind=provider and first CMS-substrate signal.',
    '2026-06-08'::DATE,
    'Stacks on 2.8.0-cms-medicare-substrate-v1.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ----------------------------------------------------------------------------
-- 1. entity_kind CHECK widening: + 'provider'
--
-- DROP + ADD is the only Postgres path to redefine a CHECK constraint. Atomic
-- inside this transaction, so no observation row can be inserted under the
-- old (narrower) constraint and orphaned by the stricter rebuild.
-- ----------------------------------------------------------------------------
ALTER TABLE derived.fraud_signal_observation
    DROP CONSTRAINT IF EXISTS fraud_signal_observation_entity_kind_check;

ALTER TABLE derived.fraud_signal_observation
    ADD CONSTRAINT fraud_signal_observation_entity_kind_check
    CHECK (entity_kind = ANY (ARRAY[
        'committee'::TEXT,
        'candidate'::TEXT,
        'treasurer'::TEXT,
        'address'::TEXT,
        'donor_cluster'::TEXT,
        'contractor'::TEXT,
        'donor'::TEXT,
        'nj_state_candidate'::TEXT,
        'provider'::TEXT
    ]));

COMMENT ON CONSTRAINT fraud_signal_observation_entity_kind_check
    ON derived.fraud_signal_observation IS
    'Whitelist of entity_kind values. provider added by mig 101 (NPI-keyed '
    'healthcare provider) to support the FRAUD-F7 CMS-Medicare cross-source '
    'signal provider_excluded_billing. Nine kinds as of mig 101.';


-- ----------------------------------------------------------------------------
-- 2. Refresher: derived.refresh_signal_provider_excluded_billing
--
-- Bucket / percentile semantics (rate-based binary, mirroring entity_on_leie):
--   peer_bucket     = 'kind=provider'
--   peer_percentile = 1 - (n_matched / n_prescribers_in_year)
-- The Part D prescriber population is ~1.1M, so even tens of matches yield a
-- percentile far above the 0.95 tail-floor used by fraud_risk_score. The
-- dollar magnitude (gross Part D drug cost) is carried in raw_value so the
-- analyst queue can triage by exposure, not just rarity.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_signal_provider_excluded_billing(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
    v_year     INT  := CAST(p_cycle AS INT);
    v_year_end DATE := make_date(CAST(p_cycle AS INT), 12, 31);
BEGIN
    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'provider_excluded_billing';

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
    rx AS (
        -- Part D prescriber population for the data year (valid NPI only).
        SELECT npi, tot_drug_cst
        FROM raw.cms_partd_prescriber
        WHERE data_year = v_year
          AND npi ~ '^[0-9]{10}$'
          AND npi <> '0000000000'
    ),
    matches AS (
        SELECT
            rx.npi,
            rx.tot_drug_cst,
            leie.record_hash
        FROM rx
        JOIN leie USING (npi)
    ),
    pop AS (
        SELECT COUNT(*)::NUMERIC AS n_in_bucket FROM rx
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
        'provider_excluded_billing',
        COALESCE(m.tot_drug_cst, 0::NUMERIC),
        5::SMALLINT,
        'kind=provider',
        GREATEST(
            0::NUMERIC,
            1::NUMERIC - (f.n_flagged / NULLIF(pop.n_in_bucket, 0))
        ),
        '/risk/provider/' || m.npi
            || '?signal=provider_excluded_billing&leie=' || m.record_hash
    FROM matches m
    CROSS JOIN pop
    CROSS JOIN flag f;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_signal_provider_excluded_billing(CHAR(4)) IS
    'FRAUD-F7: emit provider_excluded_billing observations for the given '
    'cycle (= CMS data_year). Exact NPI join between active LEIE exclusions '
    '(in effect by year-end, not reinstated) and raw.cms_partd_prescriber. '
    'Idempotent DELETE+INSERT on its own (cycle, signal_id) slice. Returns '
    'rows inserted; 0 when no CMS data is loaded for the cycle.';


-- ----------------------------------------------------------------------------
-- 3. fraud_signal_config row
--
-- family = leie_bearing: the signal's evidentiary weight comes from the LEIE
-- federal exclusion (CMS supplies the "still billing" half). Threshold 0 --
-- it is a binary overlap; every match is actionable.
-- ----------------------------------------------------------------------------
INSERT INTO derived.fraud_signal_config
    (signal_id, signal_family, min_actionable_threshold, comment)
VALUES (
    'provider_excluded_billing',
    'leie_bearing',
    0,
    'HHS-OIG-excluded provider (active LEIE entry with a real NPI) present in '
    'CMS Medicare Part D prescriber data for a year in which the exclusion '
    'was in effect. Exact NPI match; severity 5 (payment-prohibition overlap, '
    '42 USC 1320a-7a). raw_value = gross Part D drug cost.'
)
ON CONFLICT (signal_id) DO UPDATE SET
    signal_family            = EXCLUDED.signal_family,
    min_actionable_threshold = EXCLUDED.min_actionable_threshold,
    comment                  = EXCLUDED.comment,
    updated_at               = now();


-- ----------------------------------------------------------------------------
-- 4. Master refresher: add TIER 5 (CMS-Medicare-bearing); 19 -> 20 signals
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
    -- TIER 5: CMS-Medicare-bearing signals (NOW 1 -- provider_excluded_billing)
    -- ----------------------------------------------------------------
    -- New as of mig 101: exact-NPI LEIE x CMS Part D overlap. Returns 0 when
    -- no CMS data is loaded for the cycle (e.g. FEC even-year cycles).
    SELECT derived.refresh_signal_provider_excluded_billing(p_cycle)        INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    RETURN n_total;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_all_fraud_signal_observations(CHAR(4)) IS
'Master fraud-signal refresher. Invokes all 20 seeded signal refreshers in '
'substrate-dependency tier order (FEC -> LEIE -> SAM -> USAspending -> CMS) '
'for the given cycle. Each per-signal refresher is an idempotent DELETE+INSERT '
'slice returning INT (rows inserted); the master returns SUM. Refreshers '
'against empty/cycle-mismatched substrate safely return 0. Mig 101 raises the '
'count from 19 to 20 by adding provider_excluded_billing (TIER 5, CMS).';


-- ----------------------------------------------------------------------------
-- 5. derived.v_entity_fraud_evidence widening
--
-- Adds a provider_meta CTE so the evidence card resolves display_name (CMS
-- prescriber name) and is_nj (CMS prescriber state) for entity_kind=provider.
-- Column order preserved (Postgres rejects reordering in CREATE OR REPLACE
-- VIEW); only a JOIN + two CASE branches are added.
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
    -- NPI-keyed healthcare provider. display_name from the CMS prescriber
    -- name columns; is_nj from the prescriber's practice state. Keyed on
    -- (data_year, npi) which matches (o.cycle::INT, o.entity_id) for
    -- entity_kind='provider'.
    SELECT
        data_year                                                 AS provider_data_year,
        npi                                                       AS provider_npi,
        NULLIF(TRIM(
            COALESCE(prscrbr_first_name, '') || ' ' ||
            COALESCE(prscrbr_last_org_name, '')
        ), '')                                                    AS provider_name,
        (prscrbr_state_abrvtn = 'NJ')                             AS is_nj
    FROM raw.cms_partd_prescriber
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
    'Mig 101 widens entity_kind handling to include provider (LEFT JOIN '
    'raw.cms_partd_prescriber via provider_meta CTE on (data_year, npi)).';


COMMIT;
