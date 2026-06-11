-- ============================================================================
-- Migration: 120_fraud_leads_snapshot_meta
--
-- Companion to 119. The snapshot table holds only the TOP-N leads (what the
-- /leads queue renders). But the page header reports population-level totals
-- ("N undetected leads", "largest scale", tier counts). Those totals must
-- describe the FULL national population, not the 75-row snapshot -- otherwise
-- the headline would silently undercount. This one-row-per-scope meta table
-- carries the honest national aggregates, computed by the loader from the full
-- source v_high_value_leads at snapshot time.
--
-- Provenance: same (formula_version, source_vintage_hash, snapshot_at,
-- data_quality) discipline as the snapshot rows. IDEMPOTENT.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS derived.leads_snapshot_meta (
    source_scope                       TEXT        PRIMARY KEY
        CHECK (source_scope IN ('national', 'nj')),
    formula_version                    TEXT        NOT NULL
        REFERENCES ref.formula_version (formula_version),
    source_vintage_hash                TEXT        NOT NULL,
    snapshot_at                        TIMESTAMPTZ NOT NULL DEFAULT now(),
    data_quality                       TEXT        NOT NULL DEFAULT 'computed'
        CHECK (data_quality IN ('measured', 'computed', 'modeled')),

    -- Population-level totals over the FULL source ranking ---------------------
    n_total                            INTEGER     NOT NULL,
    n_undetected                       INTEGER     NOT NULL,
    n_already_caught                   INTEGER     NOT NULL,
    n_multi_source                     INTEGER     NOT NULL,
    n_repeat_violators                 INTEGER     NOT NULL,
    n_reward_eligible                  INTEGER     NOT NULL,
    max_undetected_scale_usd           NUMERIC,
    max_exposure_usd                   NUMERIC,
    total_reward_eligible_exposure_usd NUMERIC,
    count_by_tier                      JSONB       NOT NULL DEFAULT '{}'::jsonb,

    -- How many rows the companion snapshot actually holds (for "showing X").
    n_shown_undetected                 INTEGER     NOT NULL DEFAULT 0,
    n_shown_caught                     INTEGER     NOT NULL DEFAULT 0
);

COMMENT ON TABLE derived.leads_snapshot_meta IS
    'FRAUD-F8 serving cache: one row per source_scope with population-level '
    'lead totals (full source ranking) so the /leads header reports honest '
    'national aggregates while the snapshot table holds only the top-N rows. '
    'Loader-populated; formula 3.7.0-fraud-national-leads-snapshot-v1.';

COMMIT;
