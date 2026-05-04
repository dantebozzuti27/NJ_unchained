-- ============================================================================
-- Migration: 031_governance_views
--
-- BBG-style observability views over governance.dataset_health.
--
-- Why these are views, not materialized:
--   governance.dataset_health is small (one row per materialization +
--   per check + per freshness violation; ~10s of rows per day at
--   steady state). Views over it cost <1 ms; materialization adds
--   complexity for zero observable benefit.
--
-- Why they live in the `governance` schema, not `public`:
--   They are operational metadata. Putting them in public would
--   imply they are part of the analyst-facing query surface; they
--   are not. The serving API is the consumer; analysts use the
--   per-domain views in public (v_housing_burden_nj_5yr, etc.).
-- ============================================================================


-- ----------------------------------------------------------------------------
-- governance.v_latest_materialization
--
-- For every dataset_id we have ever observed a 'materialized' signal
-- for, the most recent such signal. Surfaces:
--   * when each asset was last refreshed (the BBG-style "last update")
--   * the row count from that materialization
--   * the structured details payload (per-series, fetch window, ...)
--
-- This is the canonical "is X fresh?" query for the API and dashboards.
-- It does NOT compute a freshness STATE (PASS/WARN/FAIL); that requires
-- joining against the asset's FreshnessPolicy, which lives in code,
-- not the database. The serving layer composes them.
-- ----------------------------------------------------------------------------

CREATE OR REPLACE VIEW governance.v_latest_materialization AS
SELECT DISTINCT ON (dataset_id)
    dataset_id,
    observed_at                                  AS last_materialized_at,
    (details ->> 'rows_upserted')::BIGINT        AS rows_upserted,
    details                                      AS details,
    severity                                     AS last_severity
FROM governance.dataset_health
WHERE signal_name = 'materialized'
ORDER BY dataset_id, observed_at DESC;

COMMENT ON VIEW governance.v_latest_materialization IS
    'Per dataset_id, the most recent materialization signal. The serving '
    'API joins this against ref.release_calendar to compute "fresh / stale" '
    'state without reading Dagster internal tables.';


-- ----------------------------------------------------------------------------
-- governance.v_dataset_health_summary
--
-- Per-dataset rollup of the last 30 days of signals: counts by severity,
-- count by signal_name, last error, last warning. Drives the asset-detail
-- pane in the API (and eventually the front-end "what has been wrong
-- recently" panel).
--
-- 30 days is a defensible default: long enough to see a quarterly
-- release cycle, short enough that the JSONB scan stays cheap.
-- ----------------------------------------------------------------------------

CREATE OR REPLACE VIEW governance.v_dataset_health_summary AS
WITH window_signals AS (
    SELECT
        dataset_id,
        observed_at,
        signal_name,
        severity,
        details
    FROM governance.dataset_health
    WHERE observed_at >= now() - INTERVAL '30 days'
),
agg AS (
    SELECT
        dataset_id,
        COUNT(*)                                              AS n_signals_30d,
        COUNT(*) FILTER (WHERE severity = 'info')             AS n_info_30d,
        COUNT(*) FILTER (WHERE severity = 'warn')             AS n_warn_30d,
        COUNT(*) FILTER (WHERE severity IN ('error','fatal')) AS n_error_30d,
        MAX(observed_at) FILTER (WHERE severity IN ('error','fatal'))
                                                              AS last_error_at,
        MAX(observed_at) FILTER (WHERE severity = 'warn')     AS last_warn_at
    FROM window_signals
    GROUP BY dataset_id
)
SELECT * FROM agg;

COMMENT ON VIEW governance.v_dataset_health_summary IS
    'Per-dataset 30-day rollup: signal counts by severity, last error, '
    'last warn. The serving API exposes this verbatim.';
