-- ============================================================================
-- Seed: 016_release_calendar_zhvi
--
-- VISION_2026 §7.1 cleanup: Phase 6 (Migration 079) registered Zillow ZHVI
-- as a first-class raw asset (raw.zillow_zhvi_county) and Phase 7
-- (orchestration/asset_checks.py) registered three asset checks against it,
-- but the corresponding ref.release_calendar row was never seeded. Without
-- the row, derived.v_data_freshness_summary cannot classify ZHVI freshness
-- (the LEFT JOIN drops it) and the UI badge would silently treat ZHVI as
-- "no schedule = no opinion." This seed closes the gap.
--
-- ZHVI publication semantics (https://www.zillow.com/research/data/):
--   * Monthly cadence: Zillow publishes a single full-history CSV ~7-21
--     days after the end of each calendar month. The CSV REPLACES the
--     prior version (no incremental file); the ingester computes the
--     observation_month from the file content and UPSERTs by
--     (region_id, observation_month).
--   * Historical CDN slip observed empirically:
--       2026-04-16 release covered 2026-03-31 observation_month (16 days)
--       2026-03-13 release covered 2026-02-28 observation_month (13 days)
--       2026-02-19 release covered 2026-01-31 observation_month (19 days)
--     Window: ~13 to ~21 days. Setting expected_lag_hours = 21 days
--     (504 hours) absorbs the upper end of the observed window.
--   * day_of_month: pinned to 15 as a midpoint of the empirical [13, 21]
--     window. The Phase 7 ZHVI_FRESHNESS asset check (45-day warn / 60-day
--     fail) is the operational enforcement; this calendar row is the
--     human-readable schedule for the UI.
-- ============================================================================

INSERT INTO ref.release_calendar
    (source_id,                  cadence,    schedule_label,                                                          day_of_week, day_of_month, month_of_year, time_of_day_local, timezone,           expected_lag_hours, notes)
VALUES
    ('raw.zillow_zhvi_county',   'monthly',  '~13-21 days after month-end (Zillow Research full-history CSV)',               NULL,           15,          NULL,         '00:00:00', 'America/New_York',                504, 'Source: https://files.zillowstatic.com/research/public_csvs/zhvi/County_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv. Full-history file is replaced monthly; ingester computes observation_month from file content and UPSERTs (region_id, observation_month). 504h = 21 days absorbs the empirically-observed [13, 21]-day publish window. Pairs with FHFA HPI for cross-source validation per VISION_2026 §8.1; deviation budget enforced by the housing_index_cross_source_divergence_plausible asset check.')
ON CONFLICT (source_id) DO UPDATE SET
    cadence            = EXCLUDED.cadence,
    schedule_label     = EXCLUDED.schedule_label,
    day_of_week        = EXCLUDED.day_of_week,
    day_of_month       = EXCLUDED.day_of_month,
    month_of_year      = EXCLUDED.month_of_year,
    time_of_day_local  = EXCLUDED.time_of_day_local,
    timezone           = EXCLUDED.timezone,
    expected_lag_hours = EXCLUDED.expected_lag_hours,
    notes              = EXCLUDED.notes,
    updated_at         = now();
