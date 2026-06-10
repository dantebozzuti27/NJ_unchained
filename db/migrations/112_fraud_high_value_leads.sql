-- ============================================================================
-- Migration: 112_fraud_high_value_leads
--
-- TIER 9 / FRAUD-F8: "highest-value fraud" lead-ranking substrate.
--
-- WHAT THIS SOLVES
-- ----------------
-- The engine emits 26 signals across 7 families, but every signal is treated
-- as an equal row in derived.fraud_signal_observation. An analyst with finite
-- time needs the leads ranked by VALUE -- where "value" = financial scale +
-- reportability REWARD potential, biased toward repeat violators (prior
-- sanction failed to deter) and multi-source corroboration.
--
-- The hard constraint (verifiable-data invariants §1/§4): the ranking must not
-- invent a magic weighted score. It is built from (a) measured dollars carried
-- on the observation (raw_value), and (b) a CITED, versioned reference table
-- that maps each signal to the enforcement channel that can act on it and the
-- STATUTORY whistleblower/relator reward band where one exists. The ordering is
-- LEXICOGRAPHIC over those grounded columns, never a fabricated linear blend.
--
-- WHAT IT SHIPS
-- -------------
-- 0. ref.formula_version row.
-- 1. ref.fraud_reportability_channel -- reference table (one row per signal):
--    recovery program + channel + governing statute + URL + reward eligibility
--    + statutory relator-share band + an ordinal reward_tier (1 = highest
--    reportability reward potential) + raw_value_is_usd + is_prior_sanction.
--    Seeded by db/seeds/050 (data lives in seeds per the migration/seed split).
-- 2. derived.v_high_value_leads -- one ranked row per (entity_kind, entity_id),
--    aggregating that entity's observations across ALL cycles. Surfaces peak
--    and total USD exposure, the statutory relator-share floor, multi-source
--    breadth (distinct families), and cross-cycle recurrence of a prior-sanction
--    signal (the demonstrable "penalty failed to deter"). Carries the driver
--    signal's recovery channel/statute for one-glance triage. lead_rank encodes
--    the full lexicographic order so a caller need only ORDER BY lead_rank.
--
-- The view depends ONLY on fraud_signal_observation + fraud_signal_config +
-- this ref table (all small), so it materializes in <150ms -- Vercel-safe with
-- no name resolution inside the view (callers resolve display names for the
-- top-N only). IDEMPOTENT via CREATE TABLE IF NOT EXISTS + CREATE OR REPLACE
-- VIEW + the schema_migrations sha256 ledger. Safe to re-run.
-- ============================================================================

BEGIN;


-- ----------------------------------------------------------------------------
-- 0. Formula version registration
-- ----------------------------------------------------------------------------
INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '3.0.0-fraud-high-value-leads-v1',
    'Pillar 2 (civic integrity) FRAUD-F8 lead-ranking substrate. Adds '
    'ref.fraud_reportability_channel (signal -> enforcement channel + governing '
    'statute + statutory relator-share band + ordinal reward_tier) and '
    'derived.v_high_value_leads, a per-entity ranking by reportability reward '
    'tier, measured USD exposure, cross-cycle prior-sanction recurrence, and '
    'multi-source breadth. Lexicographic ordering over measured/cited columns; '
    'no fabricated composite score.',
    '2026-06-09',
    'Stacks on 2.9.1-fraud-excluded-provider-received-open-payments-v1.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ----------------------------------------------------------------------------
-- 1. ref.fraud_reportability_channel
--
-- One row per signal_id. This is REFERENCE DATA (verifiable-data §4): the
-- program-tier classification and the relator-share band are codified law, not
-- application constants. The FK to fraud_signal_config guarantees a channel can
-- only describe a real, configured signal.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ref.fraud_reportability_channel (
    signal_id            TEXT        PRIMARY KEY
                                     REFERENCES derived.fraud_signal_config (signal_id),
    -- The enforcement / reward program that can act on this signal.
    recovery_program     TEXT        NOT NULL,
    -- Where a tip / complaint is actually filed.
    recovery_channel     TEXT        NOT NULL,
    recovery_channel_url TEXT        NOT NULL,
    -- The governing statute the predicate maps to.
    statute_citation     TEXT        NOT NULL,
    statute_url          TEXT        NOT NULL,
    -- Is there a STATUTORY monetary bounty (relator/whistleblower share)?
    reward_eligible      BOOLEAN     NOT NULL,
    -- Statutory relator-share band [low, high] as a fraction in [0,1].
    -- NULL exactly when reward_eligible is FALSE.
    relator_share_low    NUMERIC     CHECK (relator_share_low  >= 0 AND relator_share_low  <= 1),
    relator_share_high   NUMERIC     CHECK (relator_share_high >= 0 AND relator_share_high <= 1),
    -- Ordinal reportability-reward tier: 1 = highest reward potential
    -- (reward-eligible + adjudicable exposure), 5 = lowest (no bounty).
    reward_tier          SMALLINT    NOT NULL CHECK (reward_tier BETWEEN 1 AND 5),
    -- Whether observation.raw_value for this signal is a USD exposure (so the
    -- ranking can sum/peak dollars only where dollars are what raw_value means).
    raw_value_is_usd     BOOLEAN     NOT NULL,
    -- Whether the predicate INHERENTLY means a prior sanction was already
    -- imposed (an active exclusion/debarment) -- i.e., continued conduct is the
    -- "penalty failed to deter" case.
    is_prior_sanction    BOOLEAN     NOT NULL,
    -- verifiable-data §1(a) citation_text.
    citation_text        TEXT        NOT NULL,
    formula_version      TEXT        NOT NULL REFERENCES ref.formula_version (formula_version),
    effective_date       DATE        NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- A bounty implies a non-null, well-ordered share band; no bounty implies
    -- both shares are NULL. This is the integrity tie between the two columns.
    CONSTRAINT reward_share_consistency CHECK (
        (reward_eligible
            AND relator_share_low IS NOT NULL
            AND relator_share_high IS NOT NULL
            AND relator_share_low <= relator_share_high)
        OR
        (NOT reward_eligible
            AND relator_share_low IS NULL
            AND relator_share_high IS NULL)
    )
);

COMMENT ON TABLE ref.fraud_reportability_channel IS
    'Reference data (verifiable-data invariants): maps each fraud signal to the '
    'enforcement channel that can act on it and the statutory relator/whistleblower '
    'reward band where one exists. Drives derived.v_high_value_leads ranking. '
    'reward_tier 1=highest reportability reward potential .. 5=lowest (no bounty).';


-- ----------------------------------------------------------------------------
-- 2. derived.v_high_value_leads
--
-- One ranked row per (entity_kind, entity_id), aggregating across ALL cycles.
-- lead_rank encodes the full lexicographic order:
--   best_reward_tier ASC   (reportability reward potential; 1 first)
--   peak_exposure_usd DESC  (financial scale, measured dollars)
--   repeat_violator   DESC  (prior-sanction signal recurred across >=2 cycles)
--   n_families        DESC  (multi-source corroboration)
--   max_severity      DESC
--   entity_id         ASC   (stable tiebreak)
-- ----------------------------------------------------------------------------
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
        COALESCE(ch.reward_tier, 5)        AS reward_tier,
        COALESCE(ch.raw_value_is_usd, FALSE) AS raw_value_is_usd,
        COALESCE(ch.is_prior_sanction, FALSE) AS is_prior_sanction
    FROM derived.fraud_signal_observation o
    LEFT JOIN derived.fraud_signal_config cfg
        ON cfg.signal_id = o.signal_id
    LEFT JOIN ref.fraud_reportability_channel ch
        ON ch.signal_id = o.signal_id
),
agg AS (
    SELECT
        entity_kind,
        entity_id,
        COUNT(*)                                                       AS n_observations,
        COUNT(DISTINCT signal_id)                                      AS n_signals,
        COUNT(DISTINCT signal_family)                                  AS n_families,
        COUNT(DISTINCT cycle)                                          AS n_cycles,
        MAX(cycle)                                                     AS latest_cycle,
        MIN(cycle)                                                     AS earliest_cycle,
        MAX(severity)                                                  AS max_severity,
        MIN(reward_tier)                                              AS best_reward_tier,
        BOOL_OR(reward_eligible)                                       AS reward_eligible,
        BOOL_OR(is_prior_sanction)                                    AS has_prior_sanction,
        -- distinct cycles in which a PRIOR-SANCTION signal fired: >=2 is the
        -- demonstrable "penalty failed to deter over time".
        COUNT(DISTINCT cycle) FILTER (WHERE is_prior_sanction)        AS n_sanction_cycles,
        MAX(raw_value) FILTER (WHERE raw_value_is_usd)                AS peak_exposure_usd,
        SUM(raw_value) FILTER (WHERE raw_value_is_usd)                AS total_exposure_usd,
        -- statutory relator-share floor applied to the PEAK single-cycle
        -- exposure (a conservative single-damages proxy; FCA damages can treble).
        MAX(raw_value * relator_share_low)
            FILTER (WHERE raw_value_is_usd AND reward_eligible)       AS reward_low_usd,
        MAX(raw_value * relator_share_high)
            FILTER (WHERE raw_value_is_usd AND reward_eligible)       AS reward_high_usd
    FROM enriched
    GROUP BY entity_kind, entity_id
),
driver AS (
    -- The single signal that justifies the lead's placement: best tier, then
    -- biggest dollar, then highest severity. Carries the recovery channel shown
    -- on the card so the analyst sees WHERE to report at a glance.
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
            a.best_reward_tier ASC,
            a.peak_exposure_usd DESC NULLS LAST,
            (a.n_sanction_cycles >= 2) DESC,
            a.n_families DESC,
            a.max_severity DESC,
            a.entity_id ASC
    )                                                  AS lead_rank
FROM agg a
JOIN driver d
    ON d.entity_kind = a.entity_kind AND d.entity_id = a.entity_id;

COMMENT ON VIEW derived.v_high_value_leads IS
    'FRAUD-F8 highest-value-fraud queue: one ranked row per entity across all '
    'cycles. lead_rank = lexicographic order over reportability reward tier, '
    'measured USD exposure, cross-cycle prior-sanction recurrence, and '
    'multi-source breadth. No fabricated composite score.';


COMMIT;
