-- ============================================================================
-- Migration: 113_fraud_leads_undetected_first
--
-- FRAUD-F8 reframe: rank UNDETECTED fraud, not already-caught actors.
--
-- THE FLAW THIS FIXES
-- -------------------
-- v_high_value_leads (mig 112) ranked exclusion×billing leads at the top. But
-- an entity on the HHS-OIG LEIE / NJ-Medicaid / SAM list is, by definition,
-- ALREADY CAUGHT -- the exclusion IS a prior enforcement action, and several of
-- those providers are already federally prosecuted. Worse for the stated goal
-- (whistleblower reward), the FCA public-disclosure / first-to-file bar
-- (31 U.S.C. § 3730(e)(4)) makes an already-excluded provider derivable from
-- PUBLIC lists weak relator material. We were surfacing solved cases.
--
-- THE REFRAME
-- -----------
-- Enforcement status becomes the primary ranking axis:
--   * UNDETECTED  = no prior-sanction signal fired (not on any exclusion list)
--                   but a behavioral/statistical fraud pattern did. These are
--                   the prospective, original-source, not-yet-caught leads.
--   * KNOWN       = already on an exclusion/debarment list (has_prior_sanction).
--                   Demoted to the bottom (the page shows them in a separate
--                   "already on the enforcement radar" lane -- visible, not the
--                   headline).
--
-- Within the undetected lane, rank by FINANCIAL SCALE -- but a behavioral
-- outlier's raw_value is a rate/count, not dollars. So this migration adds the
-- provider's real Medicare dollar volume (Part B Tot_Mdcr_Pymt_Amt + Part D
-- Tot_Drug_Cst, the loaded NJ substrate) as provider_scale_usd, and ranks on
-- COALESCE(peak_exposure_usd, provider_scale_usd). All ordering keys remain
-- measured dollars / counts -- still lexicographic, still no magic score.
--
-- WHAT THIS SHIPS
-- ---------------
-- 0. ref.formula_version row.
-- 1. CREATE OR REPLACE VIEW derived.v_high_value_leads:
--    - appends provider_scale_usd (NULL for non-providers),
--    - lead_rank ORDER BY now: has_prior_sanction ASC (undetected first) →
--      financial scale DESC → multi-source breadth DESC → severity DESC.
--    All pre-existing columns are preserved in order/type (append-only), so
--    CREATE OR REPLACE is legal. IDEMPOTENT; safe to re-run.
-- ============================================================================

BEGIN;


INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '3.1.0-fraud-leads-undetected-first-v1',
    'Pillar 2 (civic integrity) FRAUD-F8 reframe. v_high_value_leads now ranks '
    'UNDETECTED leads (no prior exclusion/debarment) first, by Medicare dollar '
    'scale (Part B payment + Part D drug cost) and multi-source breadth; '
    'already-excluded ("already caught") entities are demoted. Adds '
    'provider_scale_usd. Lexicographic ordering preserved; no composite score.',
    '2026-06-09',
    'Stacks on 3.0.0-fraud-high-value-leads-v1.'
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
-- Per-NPI total Medicare dollar volume across loaded years (peak single year):
-- Part B Medicare payment + Part D total drug cost. This is the financial-scale
-- yardstick for providers whose firing signal is a non-dollar behavioral outlier.
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
        -- For the DRIVER (the card's headline channel) prefer, among UNDETECTED
        -- signals, the most actionable; prior-sanction signals sort last so an
        -- undetected provider's card leads with its behavioral pattern.
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
            a.n_families DESC,                          -- multi-source corroboration
            a.max_severity DESC,
            a.entity_id ASC
    )                                                  AS lead_rank,
    -- NEW (append-only) column:
    ps.provider_scale_usd
FROM agg a
JOIN driver d
    ON d.entity_kind = a.entity_kind AND d.entity_id = a.entity_id
LEFT JOIN prov_scale ps
    ON a.entity_kind = 'provider' AND ps.entity_id = a.entity_id;

COMMENT ON VIEW derived.v_high_value_leads IS
    'FRAUD-F8 highest-value-fraud queue, reframed (mig 113): UNDETECTED leads '
    '(no prior exclusion/debarment) rank first, by Medicare dollar scale '
    '(provider_scale_usd = Part B payment + Part D drug cost) and multi-source '
    'breadth; already-excluded entities are demoted. Lexicographic, no composite.';


COMMIT;
