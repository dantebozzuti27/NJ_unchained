-- ============================================================================
-- Migration: 083_freshness_backfill
--
-- VISION_2026 §7.1 (continued from migration 082): bridge the gap between
-- "data is in the database" and "governance.dataset_health knows when it
-- arrived." Without this bridge, the v_data_freshness_summary classifier
-- correctly reports "never_materialized" for every source on a fresh
-- production deploy -- because the bulk-load scripts in
-- scripts/deploy_neon_substrate.sh do not emit 'materialized' health
-- signals; that's a Dagster-shaped concern that hasn't shipped yet.
--
-- WHAT THIS MIGRATION SHIPS
-- -------------------------
--   1. derived.v_freshness_backfill_candidates  -- read-only view: per
--      source, MAX(ingested_at) + COUNT(*) from the corresponding raw
--      table. Lets an operator see what WOULD be inserted before doing it.
--
--   2. derived.f_backfill_freshness_from_ingested_at() -- side-effecting
--      function: for every source where (a) the raw table has rows AND
--      (b) no 'materialized' signal at or after MAX(ingested_at) exists,
--      INSERT one synthetic signal at MAX(ingested_at) into
--      governance.dataset_health. Returns a per-source action report
--      (inserted / skipped_empty_table / skipped_already_recorded).
--
-- IDEMPOTENCY
-- -----------
-- Re-running the backfill is safe: the "skipped_already_recorded" guard
-- compares against MAX(observed_at) for that dataset_id, so a second
-- invocation immediately after the first is a no-op. A subsequent
-- ingester run that emits its own 'materialized' signal at a NEWER
-- timestamp will correctly take precedence (DISTINCT-ON-DESC in
-- v_latest_materialization picks the most recent).
--
-- WHY THIS IS DERIVED-NOT-RAW
-- ---------------------------
-- The function and view live in `derived` because they read FROM raw and
-- emit derived metadata to governance. They are not themselves raw data.
--
-- WHY HARDCODED UNION-ALL OVER A CONFIG TABLE
-- -------------------------------------------
-- The mapping (source_id -> table_name -> timestamp_column) is small,
-- changes only when a new raw asset ships, and IS code in the asset
-- graph (orchestration/assets.py). Co-locating it as code-shaped SQL
-- here keeps the deploy idempotent and avoids dynamic SQL inside a
-- function (PL/pgSQL EXECUTE format(...) is fragile vs. the explicit
-- UNION ALL pattern). A future ref.freshness_source_map config table
-- can refactor this if the source list grows past ~30 entries.
--
-- raw.usaspending_award uses `last_seen_at` (not `ingested_at`) because
-- it is daily-replace UPSERT semantics; last_seen_at is the freshness-
-- relevant timestamp.
-- raw.fec is an aggregate dataset_id covering cn/cm/indiv files; we
-- take the MAX across the three raw tables.
-- ref.zip_county is on_event (operator-staged) and uses ingested_at.
-- ============================================================================

BEGIN;


-- ----------------------------------------------------------------------------
-- Formula version registration. Stacks on 1.8.0-data-freshness-v1.
-- ----------------------------------------------------------------------------
INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '1.8.1-freshness-backfill-v1',
    'Bridge layer (VISION_2026 §7.1 continued): emits synthetic '
    '''materialized'' health signals into governance.dataset_health '
    'from each raw.* table''s MAX(ingested_at). Closes the gap between '
    'bulk-loaded substrate and freshness reporting until the Dagster '
    'global_refresh_all job ships. Idempotent: skips per-source if a '
    'more recent signal already exists. Read-only companion view '
    'derived.v_freshness_backfill_candidates supports dry-run inspection.',
    '2026-05-09'::DATE,
    'Hardcoded UNION-ALL of source-to-table mappings; refactor to '
    'ref.freshness_source_map if the source list grows past ~30.'
)
ON CONFLICT (formula_version) DO NOTHING;


-- ----------------------------------------------------------------------------
-- derived.v_freshness_backfill_candidates
--
-- Read-only inspection view. One row per source whose raw table has at
-- least one row. Operators run `SELECT * FROM
-- derived.v_freshness_backfill_candidates;` before invoking the function
-- so they know exactly what timestamps would be backfilled.
--
-- The view performs the same MAX(ingested_at) + COUNT(*) computation the
-- function uses, but does NOT consult governance.dataset_health -- it
-- shows the candidate set, not the post-skip-guard set. The function
-- returns the post-skip-guard set as its action report.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.v_freshness_backfill_candidates AS
WITH src AS (
    SELECT 'raw.fhfa_hpi_county'::TEXT             AS source_id,
           MAX(ingested_at)                        AS max_ingested_at,
           COUNT(*)::BIGINT                        AS rows_in_table
    FROM raw.fhfa_hpi_county
    UNION ALL
    SELECT 'raw.cpi_u',                            MAX(ingested_at), COUNT(*)::BIGINT
    FROM raw.cpi_u
    UNION ALL
    SELECT 'raw.fred_observation',                 MAX(ingested_at), COUNT(*)::BIGINT
    FROM raw.fred_observation
    UNION ALL
    SELECT 'raw.acs_median_household_income',      MAX(ingested_at), COUNT(*)::BIGINT
    FROM raw.acs_median_household_income
    UNION ALL
    SELECT 'raw.acs_housing',                      MAX(ingested_at), COUNT(*)::BIGINT
    FROM raw.acs_housing
    UNION ALL
    SELECT 'raw.acs_pums_person',                  MAX(ingested_at), COUNT(*)::BIGINT
    FROM raw.acs_pums_person
    UNION ALL
    SELECT 'raw.acs_pums_housing',                 MAX(ingested_at), COUNT(*)::BIGINT
    FROM raw.acs_pums_housing
    UNION ALL
    SELECT 'raw.lca_disclosure',                   MAX(ingested_at), COUNT(*)::BIGINT
    FROM raw.lca_disclosure
    UNION ALL
    SELECT 'raw.nj_property_tax_county',           MAX(ingested_at), COUNT(*)::BIGINT
    FROM raw.nj_property_tax_county
    UNION ALL
    SELECT 'raw.zillow_zhvi_county',               MAX(ingested_at), COUNT(*)::BIGINT
    FROM raw.zillow_zhvi_county
    UNION ALL
    -- raw.fec is an aggregate dataset_id over the three FEC bulk tables
    -- (cn/cm/indiv); they are always published together. Take the MAX
    -- across all three so a partial publish doesn't lie about freshness.
    SELECT 'raw.fec',
           GREATEST(
               (SELECT MAX(ingested_at) FROM raw.fec_candidate),
               (SELECT MAX(ingested_at) FROM raw.fec_committee),
               (SELECT MAX(ingested_at) FROM raw.fec_contribution)
           ),
           (SELECT COUNT(*) FROM raw.fec_candidate)
         + (SELECT COUNT(*) FROM raw.fec_committee)
         + (SELECT COUNT(*) FROM raw.fec_contribution)
    UNION ALL
    SELECT 'raw.hhs_oig_leie',                     MAX(ingested_at), COUNT(*)::BIGINT
    FROM raw.hhs_oig_leie
    UNION ALL
    -- raw.usaspending_award uses last_seen_at (daily-replace UPSERT
    -- semantics); ingested_at exists but tracks first-observed not
    -- most-recent-confirmed.
    SELECT 'raw.usaspending_award',                MAX(last_seen_at), COUNT(*)::BIGINT
    FROM raw.usaspending_award
    UNION ALL
    SELECT 'ref.zip_county',                       MAX(ingested_at), COUNT(*)::BIGINT
    FROM ref.zip_county
)
SELECT
    src.source_id,
    src.max_ingested_at,
    src.rows_in_table,
    -- Did this DB already record a signal at or after max_ingested_at?
    EXISTS (
        SELECT 1 FROM governance.dataset_health
        WHERE dataset_id  = src.source_id
          AND signal_name = 'materialized'
          AND observed_at >= src.max_ingested_at
    )                                               AS already_recorded,
    -- Convenience: classify the candidate's action without inserting.
    CASE
        WHEN src.max_ingested_at IS NULL OR src.rows_in_table = 0
            THEN 'skipped_empty_table'
        WHEN EXISTS (
            SELECT 1 FROM governance.dataset_health
            WHERE dataset_id  = src.source_id
              AND signal_name = 'materialized'
              AND observed_at >= src.max_ingested_at
        )   THEN 'skipped_already_recorded'
        ELSE 'would_insert'
    END                                             AS predicted_action
FROM src;

COMMENT ON VIEW derived.v_freshness_backfill_candidates IS
    'Per-source MAX(ingested_at) + COUNT(*) inspection view. Companion '
    'to derived.f_backfill_freshness_from_ingested_at(); use this to '
    'preview which sources would emit a backfilled materialized signal '
    'before invoking the function. Formula 1.8.1-freshness-backfill-v1.';


-- ----------------------------------------------------------------------------
-- derived.f_backfill_freshness_from_ingested_at()
--
-- Idempotent: re-running emits no new signals if the prior run completed.
--
-- Returns one row per source with the action taken:
--   inserted                   -- a new 'materialized' signal was emitted
--   skipped_empty_table        -- raw table has no rows; nothing to backfill
--   skipped_already_recorded   -- governance already has a >= signal
--
-- The function is side-effecting; it MUST NOT be marked STABLE. We mark
-- it VOLATILE (the default) to signal to the planner that subsequent
-- queries should not assume any prior state.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.f_backfill_freshness_from_ingested_at()
RETURNS TABLE(
    source_id       TEXT,
    action          TEXT,
    max_ingested_at TIMESTAMPTZ,
    rows_in_table   BIGINT
)
LANGUAGE plpgsql
AS $$
DECLARE
    cand RECORD;
BEGIN
    FOR cand IN
        SELECT v.source_id, v.max_ingested_at, v.rows_in_table,
               v.predicted_action
        FROM derived.v_freshness_backfill_candidates v
        ORDER BY v.source_id
    LOOP
        IF cand.predicted_action = 'would_insert' THEN
            INSERT INTO governance.dataset_health
                (dataset_id, observed_at, signal_name, severity, details)
            VALUES (
                cand.source_id,
                cand.max_ingested_at,
                'materialized',
                'info',
                jsonb_build_object(
                    'rows_upserted',     cand.rows_in_table,
                    'backfill',          true,
                    'backfill_source',   'f_backfill_freshness_from_ingested_at',
                    'formula_version',   '1.8.1-freshness-backfill-v1'
                )
            );
            source_id       := cand.source_id;
            action          := 'inserted';
            max_ingested_at := cand.max_ingested_at;
            rows_in_table   := cand.rows_in_table;
        ELSE
            source_id       := cand.source_id;
            action          := cand.predicted_action;
            max_ingested_at := cand.max_ingested_at;
            rows_in_table   := cand.rows_in_table;
        END IF;
        RETURN NEXT;
    END LOOP;
END;
$$;

COMMENT ON FUNCTION derived.f_backfill_freshness_from_ingested_at() IS
    'Side-effecting backfill: emits synthetic ''materialized'' signals '
    'into governance.dataset_health from each raw table''s MAX(ingested_at). '
    'Idempotent. Returns per-source action report. '
    'Formula 1.8.1-freshness-backfill-v1.';

COMMIT;
