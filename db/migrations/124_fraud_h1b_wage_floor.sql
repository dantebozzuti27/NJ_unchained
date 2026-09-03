-- ============================================================================
-- Migration: 124_fraud_h1b_wage_floor
--
-- FRAUD-V1c: wage-at-prevailing-floor share tail + LCA willful
-- self-attestation. RATIONALE is in work_left.txt (session 2026-09-02c).
-- Stacks on mig 122/123. No new raw tables.
--
-- SIGNALS (two new; 36 -> 38)
-- ---------------------------
-- 9. employer_wage_at_pw_floor_share_outlier (family h1b_wage, severity 3)
--    Share of CERTIFIED NJ H-1B LCAs whose filed offered wage equals the
--    filed prevailing wage in the same unit. Lawful (20 CFR 655.731 is
--    >= PW). The lead is the extreme peer-tail of that share.
--
-- 10. employer_lca_willful_attestation (family h1b_enforcement, severity 5)
--    At least one CERTIFIED NJ H-1B LCA with WILLFUL_VIOLATOR = 'Y'.
--    Self-attestation on ETA-9035, distinct from the WHD official list.
--
-- NO MAGIC NUMBERS: floor equality is the filed pair; tail/min-cell
-- live in ref.platform_constants.
-- ============================================================================

BEGIN;

INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '3.9.0-fraud-h1b-wage-floor-v1',
    'Pillar 2 FRAUD-V1c. Adds employer_wage_at_pw_floor_share_outlier '
    '(empirical tail of CERTIFIED LCAs filed exactly at prevailing wage) '
    'and employer_lca_willful_attestation (ETA-9035 WILLFUL_VIOLATOR=Y). '
    'No new raw tables. Scores remain peer-percentile composites.',
    '2026-09-02',
    'Stacks on 3.8.0-fraud-h1b-attestation-enforcement-v1. Master '
    'refresher 36 -> 38 (TIER 11).'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


INSERT INTO ref.platform_constants
    (constant_id, value, description, source_url, citation_text,
     formula_version, effective_date)
VALUES
(
    'h1b_floor_tail_pctile',
    0.99,
    'CUME_DIST cutoff (top 1%) for employer_wage_at_pw_floor_share_outlier '
    'among NJ H-1B employers in a fiscal year.',
    'https://www.ecfr.gov/current/title-20/chapter-V/part-655/subpart-H/section-655.731',
    'Empirical platform calibration. 20 CFR 655.731 requires offered wage '
    '>= prevailing wage; filing exactly at PW is lawful. Only the extreme '
    'share tail is a lead.',
    '3.9.0-fraud-h1b-wage-floor-v1',
    '2026-09-02'
),
(
    'h1b_floor_min_cases',
    10,
    'Minimum CERTIFIED NJ H-1B LCAs with both offered wage and prevailing '
    'wage present for an employer to enter the at-PW-floor share ranking.',
    'https://www.dol.gov/agencies/eta/foreign-labor/performance',
    'Empirical platform calibration. Same cell-size rationale as '
    'h1b_level1_min_cases.',
    '3.9.0-fraud-h1b-wage-floor-v1',
    '2026-09-02'
)
ON CONFLICT (constant_id, formula_version) DO UPDATE SET
    value          = EXCLUDED.value,
    description    = EXCLUDED.description,
    source_url     = EXCLUDED.source_url,
    citation_text  = EXCLUDED.citation_text,
    effective_date = EXCLUDED.effective_date;


CREATE OR REPLACE FUNCTION derived.refresh_signal_employer_wage_at_pw_floor_share_outlier(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
    v_year     INT     := CAST(p_cycle AS INT);
    v_tail     NUMERIC := derived.f_platform_constant('h1b_floor_tail_pctile');
    v_min      NUMERIC := derived.f_platform_constant('h1b_floor_min_cases');
BEGIN
    IF v_tail IS NULL OR v_min IS NULL THEN
        RAISE EXCEPTION
            'employer_wage_at_pw_floor_share_outlier: missing platform_constants'
            USING ERRCODE = 'no_data_found';
    END IF;

    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'employer_wage_at_pw_floor_share_outlier';

    WITH emp AS (
        SELECT
            employer_canonical_name,
            COUNT(*) FILTER (
                WHERE wage_rate_of_pay_from IS NOT NULL
                  AND prevailing_wage IS NOT NULL
            ) AS n_compared,
            COUNT(*) FILTER (
                WHERE wage_rate_of_pay_from IS NOT NULL
                  AND prevailing_wage IS NOT NULL
                  AND wage_rate_of_pay_from = prevailing_wage
                  AND wage_unit_of_pay IS NOT DISTINCT FROM pw_unit_of_pay
            ) AS n_at_floor
        FROM derived.v_lca_nj_h1b
        WHERE fiscal_year = v_year
          AND case_status = 'CERTIFIED'
        GROUP BY 1
    ),
    ranked AS (
        SELECT
            employer_canonical_name,
            n_at_floor::NUMERIC / n_compared AS floor_share,
            CUME_DIST() OVER (
                ORDER BY n_at_floor::NUMERIC / n_compared
            ) AS pctile
        FROM emp
        WHERE n_compared >= v_min
          AND n_compared > 0
    )
    INSERT INTO derived.fraud_signal_observation (
        cycle, entity_kind, entity_id, signal_id,
        raw_value, severity, peer_bucket, peer_percentile, evidence_url
    )
    SELECT
        p_cycle,
        'employer',
        r.employer_canonical_name,
        'employer_wage_at_pw_floor_share_outlier',
        r.floor_share,
        3::SMALLINT,
        'kind=employer|visa=H-1B|pw_floor|fy=' || p_cycle,
        r.pctile,
        '/risk/employer/' || replace(r.employer_canonical_name, ' ', '%20')
            || '?signal=employer_wage_at_pw_floor_share_outlier&cycle=' || p_cycle
    FROM ranked r
    WHERE r.pctile >= v_tail;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_signal_employer_wage_at_pw_floor_share_outlier(CHAR) IS
    'FRAUD-V1c: NJ H-1B employers in the top tail of CERTIFIED LCA share '
    'filed exactly at prevailing wage (same unit). Lawful floor; the tail '
    'is the lead. Empirical.';


CREATE OR REPLACE FUNCTION derived.refresh_signal_employer_lca_willful_attestation(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
    v_year     INT := CAST(p_cycle AS INT);
BEGIN
    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'employer_lca_willful_attestation';

    WITH src AS (
        SELECT
            employer_canonical_name,
            COUNT(*) AS n_willful
        FROM derived.v_lca_nj_h1b
        WHERE fiscal_year = v_year
          AND case_status = 'CERTIFIED'
          AND willful_violator = 'Y'
        GROUP BY 1
    )
    INSERT INTO derived.fraud_signal_observation (
        cycle, entity_kind, entity_id, signal_id,
        raw_value, severity, peer_bucket, peer_percentile, evidence_url
    )
    SELECT
        p_cycle,
        'employer',
        s.employer_canonical_name,
        'employer_lca_willful_attestation',
        s.n_willful,
        5::SMALLINT,
        'kind=employer|src=lca_willful|fy=' || p_cycle,
        1::NUMERIC,
        '/risk/employer/' || replace(s.employer_canonical_name, ' ', '%20')
            || '?signal=employer_lca_willful_attestation&cycle=' || p_cycle
    FROM src s;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_signal_employer_lca_willful_attestation(CHAR) IS
    'FRAUD-V1c: CERTIFIED NJ H-1B LCA with WILLFUL_VIOLATOR = Y. ETA-9035 '
    'self-attestation, distinct from the WHD official list. Boolean count.';


-- Include the new floor tail as a corroborator for the dependent compound.
CREATE OR REPLACE FUNCTION derived.refresh_signal_employer_h1b_dependent_plus_anomaly(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
    v_year     INT := CAST(p_cycle AS INT);
BEGIN
    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'employer_h1b_dependent_plus_anomaly';

    WITH dependent AS (
        SELECT DISTINCT employer_canonical_name
        FROM derived.v_lca_nj_h1b
        WHERE fiscal_year   = v_year
          AND case_status   = 'CERTIFIED'
          AND h1b_dependent = 'Y'
    ),
    corroborating AS (
        SELECT
            entity_id,
            COUNT(*)::INT AS n_anomalies
        FROM derived.fraud_signal_observation
        WHERE cycle = p_cycle
          AND entity_kind = 'employer'
          AND signal_id IN (
                'employer_below_prevailing_wage',
                'employer_lca_uscis_volume_gap',
                'employer_level1_wage_share_outlier',
                'employer_secondary_entity_share_outlier',
                'employer_wage_at_pw_floor_share_outlier'
          )
        GROUP BY 1
    ),
    hit AS (
        SELECT
            d.employer_canonical_name,
            c.n_anomalies
        FROM dependent d
        JOIN corroborating c
          ON c.entity_id = d.employer_canonical_name
    )
    INSERT INTO derived.fraud_signal_observation (
        cycle, entity_kind, entity_id, signal_id,
        raw_value, severity, peer_bucket, peer_percentile, evidence_url
    )
    SELECT
        p_cycle,
        'employer',
        h.employer_canonical_name,
        'employer_h1b_dependent_plus_anomaly',
        h.n_anomalies,
        4::SMALLINT,
        'kind=employer|h1b_dependent|fy=' || p_cycle,
        1::NUMERIC,
        '/risk/employer/' || replace(h.employer_canonical_name, ' ', '%20')
            || '?signal=employer_h1b_dependent_plus_anomaly&cycle=' || p_cycle
    FROM hit h;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_signal_employer_h1b_dependent_plus_anomaly(CHAR) IS
    'FRAUD-V1b/c: H-1B_DEPENDENT = Y AND at least one corroborating H-1B '
    'anomaly (below-PW, volume-gap, Level I, secondary-entity, or at-PW '
    'floor tail). Must run after those refreshers. 20 CFR 655.736 bucket.';


INSERT INTO derived.fraud_signal_config
    (signal_id, signal_family, min_actionable_threshold, comment)
VALUES
(
    'employer_wage_at_pw_floor_share_outlier',
    'h1b_wage',
    0,
    'NJ H-1B employer in the top 1% of CERTIFIED LCA share filed exactly '
    'at prevailing wage (same unit). Empirical. raw_value = share. Severity 3.'
),
(
    'employer_lca_willful_attestation',
    'h1b_enforcement',
    1,
    'CERTIFIED NJ H-1B LCA with WILLFUL_VIOLATOR = Y. ETA-9035 '
    'self-attestation. raw_value = count of such cases. Severity 5.'
)
ON CONFLICT (signal_id) DO UPDATE SET
    signal_family            = EXCLUDED.signal_family,
    min_actionable_threshold = EXCLUDED.min_actionable_threshold,
    comment                  = EXCLUDED.comment,
    updated_at               = now();


DROP VIEW IF EXISTS derived.v_h1b_employer_leads;
CREATE VIEW derived.v_h1b_employer_leads AS
SELECT
    o.cycle,
    o.entity_id,
    MAX(e.display_name)                                          AS display_name,
    BOOL_OR(COALESCE(e.is_nj, FALSE))                            AS is_nj,
    MAX(r.risk_score)                                            AS risk_score,
    COUNT(DISTINCT o.signal_id)::INT                             AS n_signals,
    MAX(o.severity)                                              AS max_severity,
    MAX(o.raw_value) FILTER (
        WHERE o.signal_id = 'employer_below_prevailing_wage'
    )                                                            AS below_pw_gap_usd,
    MAX(o.raw_value) FILTER (
        WHERE o.signal_id = 'employer_h1b_denial_rate_outlier'
    )                                                            AS denial_rate,
    MAX(o.raw_value) FILTER (
        WHERE o.signal_id = 'employer_lca_uscis_volume_gap'
    )                                                            AS lca_uscis_gap_ratio,
    MAX(o.raw_value) FILTER (
        WHERE o.signal_id = 'employer_certified_withdrawn_rate_outlier'
    )                                                            AS certified_withdrawn_rate,
    MAX(o.raw_value) FILTER (
        WHERE o.signal_id = 'employer_on_whd_willful_or_debarred'
    )                                                            AS on_whd_list,
    MAX(o.raw_value) FILTER (
        WHERE o.signal_id = 'employer_level1_wage_share_outlier'
    )                                                            AS level1_wage_share,
    MAX(o.raw_value) FILTER (
        WHERE o.signal_id = 'employer_secondary_entity_share_outlier'
    )                                                            AS secondary_entity_share,
    MAX(o.raw_value) FILTER (
        WHERE o.signal_id = 'employer_h1b_dependent_plus_anomaly'
    )                                                            AS dependent_anomaly_count,
    MAX(o.raw_value) FILTER (
        WHERE o.signal_id = 'employer_wage_at_pw_floor_share_outlier'
    )                                                            AS at_pw_floor_share,
    MAX(o.raw_value) FILTER (
        WHERE o.signal_id = 'employer_lca_willful_attestation'
    )                                                            AS lca_willful_count,
    MAX(o.signal_id) FILTER (
        WHERE o.severity = (
            SELECT MAX(o2.severity)
            FROM derived.fraud_signal_observation o2
            WHERE o2.cycle = o.cycle
              AND o2.entity_kind = 'employer'
              AND o2.entity_id = o.entity_id
        )
    )                                                            AS preview_signal_id
FROM derived.fraud_signal_observation o
LEFT JOIN derived.v_entity_fraud_evidence e
       ON e.cycle = o.cycle
      AND e.entity_kind = o.entity_kind
      AND e.entity_id = o.entity_id
      AND e.signal_id = o.signal_id
LEFT JOIN derived.v_entity_fraud_risk r
       ON r.cycle = o.cycle
      AND r.entity_kind = o.entity_kind
      AND r.entity_id = o.entity_id
WHERE o.entity_kind = 'employer'
  AND o.signal_id IN (
        'employer_below_prevailing_wage',
        'employer_h1b_denial_rate_outlier',
        'employer_lca_uscis_volume_gap',
        'employer_certified_withdrawn_rate_outlier',
        'employer_on_whd_willful_or_debarred',
        'employer_level1_wage_share_outlier',
        'employer_secondary_entity_share_outlier',
        'employer_h1b_dependent_plus_anomaly',
        'employer_wage_at_pw_floor_share_outlier',
        'employer_lca_willful_attestation'
  )
GROUP BY o.cycle, o.entity_id;

COMMENT ON VIEW derived.v_h1b_employer_leads IS
    'One row per (cycle, employer) with stacked H-1B FRAUD-V1..V1c '
    'signals. Read surface for /h1b. formula 3.9.0-fraud-h1b-wage-floor-v1.';


CREATE OR REPLACE FUNCTION derived.refresh_all_fraud_signal_observations(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_total INT := 0;
    n_each  INT;
BEGIN
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

    SELECT derived.refresh_signal_entity_excluded_via_sam_uei(p_cycle)      INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_donor_on_sam(p_cycle)                     INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_candidate_funded_by_sam_excluded_donors(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_entity_funded_and_excluded(p_cycle)       INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_candidate_funded_by_nj_contractor_employees(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_donor_employed_by_nj_contractor(p_cycle)  INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_provider_excluded_billing(p_cycle)        INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_provider_excluded_billing_partb(p_cycle)  INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_state_excluded_provider_billing(p_cycle)  INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_opioid_prescribing_outlier(p_cycle)       INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_services_per_beneficiary_outlier(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_antipsychotic_elderly_outlier(p_cycle)    INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_name_resolved_excluded_provider_billing(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_excluded_provider_received_open_payments(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_provider_billing_growth_outlier(p_cycle)  INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    -- TIER 11: H-1B employer visa-fraud leads (10 -- mig 121 + 122 + 124)
    SELECT derived.refresh_signal_employer_below_prevailing_wage(p_cycle)   INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_employer_h1b_denial_rate_outlier(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_employer_lca_uscis_volume_gap(p_cycle)    INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_employer_certified_withdrawn_rate_outlier(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_employer_on_whd_willful_or_debarred(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_employer_level1_wage_share_outlier(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_employer_secondary_entity_share_outlier(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_employer_wage_at_pw_floor_share_outlier(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_employer_lca_willful_attestation(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);
    SELECT derived.refresh_signal_employer_h1b_dependent_plus_anomaly(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    RETURN n_total;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_all_fraud_signal_observations(CHAR(4)) IS
    'Master fraud-signal refresher. Invokes all 38 seeded signal refreshers '
    'in substrate-dependency tier order. Mig 124 raises 36 -> 38 by adding '
    'at-PW-floor share and LCA willful attestation in TIER 11.';


COMMIT;
