-- ============================================================================
-- Migration: 082_data_freshness_summary
--
-- VISION_2026 §7.1 — Data freshness substrate.
--
-- The user explicitly asked for a "click a button on my computer and it
-- automatically start refreshing to make sure all data has the most current
-- available data" capability. The design has two halves:
--
--   1. THIS MIGRATION (substrate): a single SQL surface that, for every
--      ingester registered in ref.release_calendar, joins against the most
--      recent 'materialized' health-signal in governance.dataset_health and
--      classifies the source as one of:
--
--         fresh                — within (cadence_period + publisher_lag_hours)
--         stale                — within 1.5x of the budget (publisher delayed)
--         critical             — over 1.5x (cadence may have changed OR ingester broken)
--         never_materialized   — release_calendar registers the source but
--                                governance.dataset_health has no signal
--                                (substrate-honest signal that this DB has
--                                never seen this ingester run)
--
--      The 1.5x threshold is conservative; it accommodates the empirical
--      observation that publishers (BLS, FHFA, Census) sometimes slip a
--      release by half a cadence period without intent to revise the
--      schedule. Anything past 1.5x is operationally significant.
--
--   2. The COMPLEMENTARY ops layer (next migrations / app code):
--      - lib/freshness.ts: typed fetcher
--      - <FreshnessBadge /> component: surfaces `overall_status` on the
--        housing/personalize pages
--      - nj-cli refresh --since 7d: kicks off the global_refresh_all
--        Dagster job (deferred; substrate is the prerequisite)
--
-- WHY THE SCHEDULE IS A LOOKUP TABLE, NOT A CASE STATEMENT
-- --------------------------------------------------------
-- We map cadence -> nominal hours via a CTE/lookup so adding a new cadence
-- shape (e.g. 'biweekly') is INSERT-data, not migration-deploy. This is the
-- same pattern as ref.release_calendar itself.
--
-- WHY THIS IS A VIEW, NOT A MATERIALIZED TABLE
-- --------------------------------------------
-- ref.release_calendar has ~10 rows; governance.v_latest_materialization is
-- DISTINCT-ON over governance.dataset_health which is small (~10s of rows
-- per day). The whole join is a few hundred rows at steady state and costs
-- < 1 ms. Materializing it would add staleness without observable benefit.
--
-- Schema: derived (not public). The serving API may project this through
-- public.v_data_freshness_public if/when the analyst-facing surface needs
-- it; we keep derived-vs-public separation per AGENTS.md.
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- Formula version registration. Stacks on 1.7.1-tier-bands-v1.
-- ----------------------------------------------------------------------------
INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '1.8.0-data-freshness-v1',
    'Data freshness substrate (VISION_2026 §7.1): joins ref.release_calendar '
    'against governance.v_latest_materialization to produce one row per '
    'registered source with last_materialized_at, hours_since_materialized, '
    'and a fresh/stale/critical/never_materialized classification. Plus a '
    'platform-wide single-row rollup for the UI badge. The 1.5x threshold '
    'between stale and critical is empirically calibrated: BLS/FHFA/Census '
    'occasionally slip releases by ~half a cadence period without revising '
    'their schedule; anything beyond 1.5x is operationally significant. '
    'Substrate-honest: a source that exists in release_calendar but has '
    'never raised a materialized signal in this DB returns '
    'never_materialized, not silent NULL.',
    '2026-05-09'::DATE,
    'Stacks on 1.7.1-tier-bands-v1.'
)
ON CONFLICT (formula_version) DO NOTHING;


-- ----------------------------------------------------------------------------
-- derived.v_data_freshness_summary
--
-- One row per source registered in ref.release_calendar. Adding a new
-- ingester is automatic: insert into ref.release_calendar and (eventually)
-- have the ingester emit a 'materialized' signal into governance.dataset_health
-- — this view picks it up without further DDL.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.v_data_freshness_summary AS
WITH cadence_hours(cadence, period_hours) AS (
    -- Nominal cadence period in hours.
    --   on_event: no scheduled cadence; the ENTIRE budget is expected_lag_hours
    --             so we use 0 here and let the budget formula below handle it.
    VALUES
        ('daily',         24),
        ('weekly',       168),
        ('monthly',      720),
        ('quarterly',   2160),
        ('annual',      8760),
        ('on_event',       0)
),
budgeted AS (
    SELECT
        rc.source_id,
        rc.cadence,
        rc.schedule_label,
        rc.expected_lag_hours,
        rc.timezone                    AS publisher_timezone,
        rc.notes                       AS publisher_notes,
        ch.period_hours                AS cadence_period_hours,
        -- Total expected age budget. For scheduled cadences:
        --   period + publisher_lag (e.g. monthly = 720 + 48 = 768h ~= 32 days)
        -- For on_event sources we have no nominal period; the publisher_lag
        -- is the entire budget.
        CASE
            WHEN rc.cadence = 'on_event' THEN rc.expected_lag_hours
            ELSE ch.period_hours + rc.expected_lag_hours
        END                            AS expected_max_age_hours,
        lm.last_materialized_at,
        lm.rows_upserted,
        lm.last_severity               AS last_signal_severity,
        lm.details                     AS last_signal_details
    FROM ref.release_calendar rc
    LEFT JOIN cadence_hours ch                            USING (cadence)
    LEFT JOIN governance.v_latest_materialization lm
           ON lm.dataset_id = rc.source_id
)
SELECT
    source_id,
    cadence,
    schedule_label,
    expected_lag_hours,
    cadence_period_hours,
    expected_max_age_hours,
    publisher_timezone,
    publisher_notes,
    last_materialized_at,
    rows_upserted,
    last_signal_severity,
    last_signal_details,
    -- Hours since last materialized; NULL if never.
    CASE
        WHEN last_materialized_at IS NULL THEN NULL
        ELSE EXTRACT(EPOCH FROM (now() - last_materialized_at)) / 3600.0
    END                                AS hours_since_materialized,
    -- Classifier:
    CASE
        WHEN last_materialized_at IS NULL                 THEN 'never_materialized'
        WHEN expected_max_age_hours = 0                   THEN
            -- on_event with zero lag budget: any non-zero age is critical.
            -- Edge case; included for completeness.
            CASE WHEN now() = last_materialized_at THEN 'fresh' ELSE 'critical' END
        WHEN EXTRACT(EPOCH FROM (now() - last_materialized_at)) / 3600.0
                <= expected_max_age_hours                 THEN 'fresh'
        WHEN EXTRACT(EPOCH FROM (now() - last_materialized_at)) / 3600.0
                <= 1.5 * expected_max_age_hours           THEN 'stale'
        ELSE                                                   'critical'
    END                                AS freshness_status
FROM budgeted;

COMMENT ON VIEW derived.v_data_freshness_summary IS
    'Per-source freshness: joins ref.release_calendar against '
    'governance.v_latest_materialization. One row per registered source. '
    'Status: fresh / stale (1-1.5x of cadence+lag budget) / critical '
    '(>1.5x) / never_materialized (no materialized signal yet). '
    'Formula version 1.8.0-data-freshness-v1; spec VISION_2026 §7.1.';


-- ----------------------------------------------------------------------------
-- derived.v_platform_freshness_headline
--
-- Single-row rollup for UI badge consumption. Lists overall status,
-- per-bucket counts, and the source most in need of attention.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.v_platform_freshness_headline AS
WITH worst AS (
    SELECT source_id, freshness_status, hours_since_materialized
    FROM derived.v_data_freshness_summary
    WHERE freshness_status IN ('critical', 'stale')
    ORDER BY
        CASE freshness_status
            WHEN 'critical' THEN 0
            WHEN 'stale'    THEN 1
            ELSE                 2
        END,
        hours_since_materialized DESC NULLS LAST
    LIMIT 1
)
SELECT
    COUNT(*)                                                          AS n_sources,
    COUNT(*) FILTER (WHERE freshness_status = 'fresh')                 AS n_fresh,
    COUNT(*) FILTER (WHERE freshness_status = 'stale')                 AS n_stale,
    COUNT(*) FILTER (WHERE freshness_status = 'critical')              AS n_critical,
    COUNT(*) FILTER (WHERE freshness_status = 'never_materialized')    AS n_never_materialized,
    MAX(last_materialized_at)                                          AS most_recent_materialization,
    MIN(last_materialized_at) FILTER (WHERE last_materialized_at IS NOT NULL)
                                                                       AS oldest_materialization,
    (SELECT source_id        FROM worst)                               AS worst_source_id,
    (SELECT freshness_status FROM worst)                               AS worst_status,
    -- Single-string overall verdict for the UI badge:
    --   FRESH    -> all sources within budget
    --   PARTIAL  -> some sources never_materialized, none critical/stale
    --   STALE    -> at least one stale, no critical
    --   CRITICAL -> at least one critical
    CASE
        WHEN COUNT(*) FILTER (WHERE freshness_status = 'critical') > 0           THEN 'CRITICAL'
        WHEN COUNT(*) FILTER (WHERE freshness_status = 'stale') > 0              THEN 'STALE'
        WHEN COUNT(*) FILTER (WHERE freshness_status = 'never_materialized') > 0 THEN 'PARTIAL'
        ELSE                                                                          'FRESH'
    END                                                                AS overall_status
FROM derived.v_data_freshness_summary;

COMMENT ON VIEW derived.v_platform_freshness_headline IS
    'Single-row platform-wide freshness rollup for the UI badge. '
    'Overall status follows worst-source dominance: any critical -> CRITICAL; '
    'any stale -> STALE; any never_materialized -> PARTIAL; else FRESH. '
    'Formula version 1.8.0-data-freshness-v1.';


-- ----------------------------------------------------------------------------
-- derived.f_data_freshness_status(source_id) -> TEXT
--
-- Scalar convenience for one-row callers. Returns NULL if the source_id
-- is not in ref.release_calendar (substrate-honest).
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.f_data_freshness_status(p_source_id TEXT)
RETURNS TEXT
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    SELECT freshness_status FROM derived.v_data_freshness_summary
    WHERE source_id = p_source_id
$$;

COMMENT ON FUNCTION derived.f_data_freshness_status(TEXT) IS
    'Scalar wrapper: freshness_status for one source_id, or NULL if the '
    'source is not registered in ref.release_calendar.';

COMMIT;
