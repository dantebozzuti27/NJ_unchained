-- ============================================================================
-- Migration: 116_fraud_leads_multi_pattern_corroboration
--
-- FRAUD-F8 ranking refinement: reward MULTI-PATTERN corroboration.
--
-- WHY
-- ---
-- The undetected behavioral detectors (opioid, services-per-beneficiary,
-- antipsychotic-in-elderly) all belong to ONE signal family (cms_utilization),
-- because they share one substrate (CMS Medicare utilization data). So
-- n_families never exceeds 1 for a provider flagged only by behavioral
-- detectors, and the multi_source tiebreak in lead_rank (mig 113) gives no
-- credit when a single provider trips, say, BOTH the opioid AND the
-- antipsychotic detector. But a provider exhibiting several DISTINCT abnormal
-- prescribing patterns is a materially stronger lead than one with a single
-- pattern.
--
-- WHAT THIS SHIPS
-- ---------------
-- CREATE OR REPLACE VIEW derived.v_high_value_leads with one change: lead_rank
-- gains n_signals (distinct signal_ids) as a corroboration key, applied AFTER
-- cross-substrate breadth (n_families) and BEFORE severity. New order:
--   has_prior_sanction ASC        (undetected first)
--   financial scale DESC          (peak exposure, else provider Medicare volume)
--   n_families DESC               (cross-substrate corroboration)
--   n_signals DESC                (multi-pattern corroboration -- NEW)
--   max_severity DESC
--   entity_id ASC
-- Columns are unchanged (append-only contract preserved); only the lead_rank
-- expression changes. All ordering keys remain measured counts/dollars -- still
-- lexicographic, still no composite score. IDEMPOTENT. Safe to re-run.
-- ============================================================================

BEGIN;


INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '3.4.0-fraud-leads-multi-pattern-corroboration-v1',
    'Pillar 2 (civic integrity) FRAUD-F8 ranking refinement. v_high_value_leads '
    'lead_rank now rewards multi-pattern corroboration: n_signals (distinct '
    'detectors fired) is a tiebreak after n_families and before severity, so a '
    'provider tripping several distinct behavioral detectors (all in the single '
    'cms_utilization family) outranks a single-pattern peer. Lexicographic '
    'ordering preserved; no composite score.',
    '2026-06-09',
    'Stacks on 3.1.0-fraud-leads-undetected-first-v1.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


CREATE OR REPLACE VIEW derived.v_high_value_leads AS
WITH enriched AS (
    SELECT
        o.entity_kind,
        o.entity_id,
        o.cycle,
        o.signal_id,
        o.severity,
        o.raw_value,
        cfg.signal_family,
        ch.recovery_program,
        ch.recovery_channel,
        ch.recovery_channel_url,
        ch.statute_citation,
        ch.statute_url,
        ch.reward_eligible,
        ch.relator_share_low,
        ch.relator_share_high,
        COALESCE(ch.reward_tier, 5)          AS reward_tier,
        COALESCE(ch.raw_value_is_usd, FALSE) AS raw_value_is_usd,
        COALESCE(ch.is_prior_sanction, FALSE) AS is_prior_sanction
    FROM derived.fraud_signal_observation o
    LEFT JOIN derived.fraud_signal_config cfg
        ON cfg.signal_id = o.signal_id
    LEFT JOIN ref.fraud_reportability_channel ch
        ON ch.signal_id = o.signal_id
),
prov_scale AS (
    SELECT npi AS entity_id, MAX(year_usd) AS provider_scale_usd
    FROM (
        SELECT npi, data_year, SUM(amt) AS year_usd
        FROM (
            SELECT npi, data_year, COALESCE(tot_drug_cst, 0)        AS amt
            FROM raw.cms_partd_prescriber
            UNION ALL
            SELECT npi, data_year, COALESCE(tot_mdcr_pymt_amt, 0)   AS amt
            FROM raw.cms_physician_provider
        ) u
        GROUP BY npi, data_year
    ) y
    GROUP BY npi
),
agg AS (
    SELECT
        entity_kind,
        entity_id,
        COUNT(*)                                                AS n_observations,
        COUNT(DISTINCT signal_id)                               AS n_signals,
        COUNT(DISTINCT signal_family)                           AS n_families,
        COUNT(DISTINCT cycle)                                   AS n_cycles,
        MAX(cycle)                                              AS latest_cycle,
        MIN(cycle)                                              AS earliest_cycle,
        MAX(severity)                                           AS max_severity,
        MIN(reward_tier)                                       AS best_reward_tier,
        BOOL_OR(reward_eligible)                                AS reward_eligible,
        BOOL_OR(is_prior_sanction)                            AS has_prior_sanction,
        COUNT(DISTINCT cycle) FILTER (WHERE is_prior_sanction) AS n_sanction_cycles,
        MAX(raw_value) FILTER (WHERE raw_value_is_usd)         AS peak_exposure_usd,
        SUM(raw_value) FILTER (WHERE raw_value_is_usd)         AS total_exposure_usd,
        MAX(raw_value * relator_share_low)
            FILTER (WHERE raw_value_is_usd AND reward_eligible) AS reward_low_usd,
        MAX(raw_value * relator_share_high)
            FILTER (WHERE raw_value_is_usd AND reward_eligible) AS reward_high_usd
    FROM enriched
    GROUP BY entity_kind, entity_id
),
driver AS (
    SELECT DISTINCT ON (entity_kind, entity_id)
        entity_kind,
        entity_id,
        signal_id            AS driver_signal_id,
        signal_family        AS driver_signal_family,
        cycle                AS driver_cycle,
        recovery_program,
        recovery_channel,
        recovery_channel_url,
        statute_citation,
        statute_url
    FROM enriched
    ORDER BY
        entity_kind,
        entity_id,
        is_prior_sanction ASC,
        reward_tier ASC,
        raw_value DESC NULLS LAST,
        severity DESC
)
SELECT
    a.entity_kind,
    a.entity_id,
    a.latest_cycle,
    a.earliest_cycle,
    a.n_cycles,
    a.n_observations,
    a.n_signals,
    a.n_families,
    a.max_severity,
    a.best_reward_tier,
    a.reward_eligible,
    a.has_prior_sanction,
    (a.n_families >= 2)                                AS multi_source,
    (a.n_sanction_cycles >= 2)                         AS repeat_violator,
    a.peak_exposure_usd,
    a.total_exposure_usd,
    a.reward_low_usd,
    a.reward_high_usd,
    d.driver_signal_id,
    d.driver_signal_family,
    d.driver_cycle,
    d.recovery_program,
    d.recovery_channel,
    d.recovery_channel_url,
    d.statute_citation,
    d.statute_url,
    ROW_NUMBER() OVER (
        ORDER BY
            a.has_prior_sanction ASC,                  -- UNDETECTED first
            COALESCE(a.peak_exposure_usd, ps.provider_scale_usd, 0) DESC,  -- financial scale
            a.n_families DESC,                          -- cross-substrate corroboration
            a.n_signals DESC,                           -- multi-pattern corroboration (NEW)
            a.max_severity DESC,
            a.entity_id ASC
    )                                                  AS lead_rank,
    ps.provider_scale_usd
FROM agg a
JOIN driver d
    ON d.entity_kind = a.entity_kind AND d.entity_id = a.entity_id
LEFT JOIN prov_scale ps
    ON a.entity_kind = 'provider' AND ps.entity_id = a.entity_id;

COMMENT ON VIEW derived.v_high_value_leads IS
    'FRAUD-F8 highest-value-fraud queue (mig 116): UNDETECTED leads rank first, '
    'by Medicare dollar scale, then cross-substrate breadth (n_families), then '
    'multi-pattern corroboration (n_signals), then severity. Already-excluded '
    'entities demoted. Lexicographic over measured counts/dollars; no composite.';


COMMIT;
