-- ============================================================================
-- Migration: 111_fraud_excluded_provider_received_open_payments
--
-- TIER 9 / FRAUD-F7 (conflict-of-interest lead): the FIRST signal mined from
-- the CMS Open Payments substrate (mig 103).
--
-- THE SIGNAL
-- ----------
-- An HHS-OIG-excluded provider (active LEIE entry with a real NPI) who is
-- nonetheless a COVERED RECIPIENT in CMS Open Payments General Payments for a
-- program year in which the exclusion was already in effect. Open Payments
-- records every transfer of value (consulting fees, speaker fees, meals,
-- travel, royalties) an applicable manufacturer or GPO makes to a physician /
-- non-physician practitioner, keyed by NPI.
--
-- WHY IT MATTERS (and why it is NOT severity 5)
-- ---------------------------------------------
-- Receiving an industry transfer of value is NOT itself a federal-program
-- payment, so this is NOT a 42 USC 1320a-7a payment-prohibition breach (those
-- are the severity-5 provider_excluded_billing[_partb] signals against
-- Medicare Part D / Part B). It is a CORROBORATING conflict-of-interest lead:
-- an OIG-excluded provider is still professionally active enough that industry
-- is courting them. Stacked with an exact-match billing signal it strengthens
-- the picture; standing alone it is a MODERATE lead -> SEVERITY 3.
--
-- IDENTITY CONFIDENCE
-- -------------------
-- The match is an EXACT NPI equijoin (LEIE.npi = Open Payments
-- covered_recipient_npi), so identity is high-confidence -- unlike the
-- name-resolved recall signal (mig 110). The lower severity reflects the
-- nature of the CONDUCT (lawful-but-suspicious receipt of value), not low
-- identity confidence. calibration_basis is therefore 'oig_report' (the OIG
-- exclusion is the authority establishing the entity is excluded).
--
-- THE PRECISION GUARD (identical window logic to mig 101 / 109)
-- ------------------------------------------------------------
-- The exclusion must have been in effect by the END of the program year and
-- not yet reinstated:
--   excldate_d <= make_date(year, 12, 31)
--   AND (reindate_d IS NULL OR reindate_d > make_date(year, 12, 31))
-- and we consider only LEIE rows STILL active today (derived.v_leie_active).
--
-- ENTITY / RAW_VALUE / PEER
-- -------------------------
-- entity_kind='provider' (NPI). cycle = Open Payments program_year (CHAR(4)).
-- peer_bucket = 'kind=provider'. peer_percentile = 1 - (n_matched /
-- n_recipients), mirroring the rate-based binary of the exact-match family.
-- raw_value = SUM(payment_amount) -- total transfers of value received that
-- year, the dollar exposure for analyst triage.
--
-- WHAT THIS MIGRATION SHIPS
-- -------------------------
-- 0. ref.formula_version row.
-- 1. derived.refresh_signal_excluded_provider_received_open_payments(CHAR(4)).
-- 2. derived.fraud_signal_config row (family = leie_bearing, exists).
-- 3. Master refresher rewired: NEW TIER 9 (Open-Payments conflict-of-interest);
--    25 -> 26 signals.
-- 4. derived.v_entity_fraud_evidence: provider_meta widened to ALSO resolve a
--    provider's display name + NJ flag from CMS Open Payments (pref=3) so an
--    Open-Payments-ONLY recipient (this signal's natural case -- excluded, not
--    billing Medicare) renders a real name, not a bare NPI. Part D (pref=1) and
--    Part B (pref=2) still win when present. DISTINCT ON keeps exactly one
--    identity row per (npi, data_year).
--
-- entity_kind 'provider' and signal_family 'leie_bearing' already exist.
-- Companion seed 049 ships the evidence-card reference rows.
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
    '2.9.1-fraud-excluded-provider-received-open-payments-v1',
    'Pillar 2 (civic integrity) FRAUD-F7 signal slice. Adds '
    'excluded_provider_received_open_payments: an active HHS-OIG LEIE '
    'exclusion (with a real NPI) present as a covered recipient in CMS Open '
    'Payments General Payments for a program year in which the exclusion was '
    'already in effect. Exact NPI equijoin; conflict-of-interest lead '
    '(industry transfer of value to an excluded provider is NOT a federal '
    'payment) -> SEVERITY 3, calibration_basis oig_report. raw_value = total '
    'payment_amount received. First signal mined from the Open Payments '
    'substrate. Also widens provider_meta in v_entity_fraud_evidence to '
    'resolve provider identity from Open Payments (pref 3, after Part D / B).',
    '2026-06-09'::DATE,
    'Stacks on 2.9.0-fraud-name-resolved-excluded-provider-billing-v1. '
    'Requires the CMS Open Payments substrate (mig 103).'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ----------------------------------------------------------------------------
-- 1. Refresher: derived.refresh_signal_excluded_provider_received_open_payments
--
-- Exact NPI join between active LEIE exclusions (in effect by program-year-end,
-- not reinstated) and CMS Open Payments recipients. raw_value carries the
-- summed transfers of value for that recipient in the program year.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_signal_excluded_provider_received_open_payments(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
    v_year     INT  := CAST(p_cycle AS INT);
    v_year_end DATE := make_date(CAST(p_cycle AS INT), 12, 31);
BEGIN
    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'excluded_provider_received_open_payments';

    WITH leie AS (
        -- Active LEIE exclusions with a real NPI, in effect by program-year-end
        -- and not yet reinstated. DISTINCT ON collapses the (rare) case of
        -- multiple active rows sharing one NPI to the freshest exclusion.
        SELECT DISTINCT ON (npi)
            npi,
            record_hash,
            excldate_d
        FROM derived.v_leie_active
        WHERE npi ~ '^[0-9]{10}$'
          AND npi <> '0000000000'
          AND excldate_d IS NOT NULL
          AND excldate_d <= v_year_end
          AND (reindate_d IS NULL OR reindate_d > v_year_end)
        ORDER BY npi, excldate_d DESC NULLS LAST
    ),
    recipients AS (
        -- Open Payments recipient population for the program year, aggregated
        -- to one row per recipient NPI (valid NPI only). total_received is the
        -- summed transfer of value; blank amounts loaded as NULL -> COALESCE 0.
        SELECT
            covered_recipient_npi                            AS npi,
            SUM(COALESCE(payment_amount, 0::NUMERIC))        AS total_received
        FROM raw.cms_open_payments_general
        WHERE program_year = v_year
          AND covered_recipient_npi ~ '^[0-9]{10}$'
          AND covered_recipient_npi <> '0000000000'
        GROUP BY covered_recipient_npi
    ),
    matches AS (
        SELECT
            r.npi,
            r.total_received,
            leie.record_hash
        FROM recipients r
        JOIN leie USING (npi)
    ),
    pop AS (
        SELECT COUNT(*)::NUMERIC AS n_in_bucket FROM recipients
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
        'excluded_provider_received_open_payments',
        m.total_received,
        3::SMALLINT,
        'kind=provider',
        GREATEST(
            0::NUMERIC,
            1::NUMERIC - (f.n_flagged / NULLIF(pop.n_in_bucket, 0))
        ),
        '/risk/provider/' || m.npi
            || '?signal=excluded_provider_received_open_payments&leie='
            || m.record_hash
    FROM matches m
    CROSS JOIN pop
    CROSS JOIN flag f;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_signal_excluded_provider_received_open_payments(CHAR(4)) IS
    'FRAUD-F7: emit excluded_provider_received_open_payments observations for '
    'the given cycle (= Open Payments program_year). Exact NPI join between '
    'active LEIE exclusions (in effect by year-end, not reinstated) and CMS '
    'Open Payments recipients. Conflict-of-interest lead (severity 3): an '
    'excluded provider receiving industry transfers of value is NOT a federal '
    'payment-prohibition breach but corroborates ongoing professional '
    'activity. raw_value = summed payment_amount. Idempotent DELETE+INSERT on '
    'its own (cycle, signal_id) slice. Returns rows inserted; 0 when no Open '
    'Payments data is loaded for the cycle.';


-- ----------------------------------------------------------------------------
-- 2. fraud_signal_config row (leie_bearing family already whitelisted)
-- ----------------------------------------------------------------------------
INSERT INTO derived.fraud_signal_config
    (signal_id, signal_family, min_actionable_threshold, comment)
VALUES (
    'excluded_provider_received_open_payments',
    'leie_bearing',
    0,
    'HHS-OIG-excluded provider (active LEIE entry with a real NPI) present as '
    'a covered recipient in CMS Open Payments General Payments for a program '
    'year in which the exclusion was in effect. Exact NPI match; '
    'conflict-of-interest lead (industry transfer of value is NOT a federal '
    'payment) -> severity 3. raw_value = total payment_amount received. First '
    'signal on the Open Payments substrate; corroborates the exact-match '
    'Medicare billing signals.'
)
ON CONFLICT (signal_id) DO UPDATE SET
    signal_family            = EXCLUDED.signal_family,
    min_actionable_threshold = EXCLUDED.min_actionable_threshold,
    comment                  = EXCLUDED.comment,
    updated_at               = now();


-- ----------------------------------------------------------------------------
-- 3. Master refresher: NEW TIER 9 (Open-Payments conflict-of-interest); 25->26
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

    -- ----------------------------------------------------------------
    -- TIER 9: Open-Payments conflict-of-interest signals (1 -- mig 111)
    -- ----------------------------------------------------------------
    SELECT derived.refresh_signal_excluded_provider_received_open_payments(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    RETURN n_total;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_all_fraud_signal_observations(CHAR(4)) IS
'Master fraud-signal refresher. Invokes all 26 seeded signal refreshers in '
'substrate-dependency tier order (FEC -> LEIE -> SAM -> USAspending -> CMS '
'federal-exclusion -> NJ-state-exclusion -> CMS-utilization -> NPPES '
'identity-resolution recall -> Open-Payments conflict-of-interest) for the '
'given cycle. Each per-signal refresher is an idempotent DELETE+INSERT slice '
'returning INT (rows inserted); the master returns SUM. Refreshers against '
'empty/cycle-mismatched substrate safely return 0. Mig 111 raises the count '
'from 25 to 26 by adding excluded_provider_received_open_payments (TIER 9, '
'CMS Open Payments conflict-of-interest lead).';


-- ----------------------------------------------------------------------------
-- 4. derived.v_entity_fraud_evidence: provider_meta resolves identity from
--    Part D OR Part B OR Open Payments (preference Part D > Part B > Open
--    Payments). Only the provider_meta CTE changes; the SELECT output column
--    list/order is byte-for-byte identical to mig 109 (CREATE OR REPLACE VIEW
--    forbids column reordering).
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
    -- from one of THREE CMS rosters keyed on (data_year, npi): Part D
    -- prescribers (pref=1), Part B practitioners (pref=2), Open Payments
    -- recipients (pref=3). Part D is preferred when an NPI appears in more
    -- than one for the year; Open Payments is the last fallback so an
    -- Open-Payments-ONLY recipient (excluded_provider_received_open_payments:
    -- excluded, not billing Medicare) still renders a real name instead of a
    -- bare NPI. DISTINCT ON keeps exactly ONE identity row per
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
        UNION ALL
        SELECT
            covered_recipient_npi                                 AS provider_npi,
            program_year                                          AS provider_data_year,
            NULLIF(TRIM(
                COALESCE(recipient_first_name, '') || ' ' ||
                COALESCE(recipient_last_name, '')
            ), '')                                                AS provider_name,
            (recipient_state = 'NJ')                              AS is_nj,
            3                                                     AS pref
        FROM raw.cms_open_payments_general
        WHERE covered_recipient_npi ~ '^[0-9]{10}$'
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
    'Mig 111 widens provider_meta to resolve provider identity from Part D OR '
    'Part B OR Open Payments (Part D preferred; DISTINCT ON per (npi, '
    'data_year)).';


COMMIT;
