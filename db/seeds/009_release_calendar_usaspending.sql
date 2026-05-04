-- ============================================================================
-- Seed: 009_release_calendar_usaspending
--
-- TIER 4 v3 / FRAUD-F1: USAspending federal-award API release schedule.
--
-- USAspending publishes a "weekly snapshot" of federal awards from
-- the upstream FPDS / FAADS feeds, with rolling intra-week updates
-- as agency systems push transactions. The functional cadence for a
-- platform analyst is therefore:
--
--   * Daily: small deltas (transaction modifications, late-reported
--     contract details).
--   * Weekly: the major refresh window. Most "new awards" appear
--     within 7-14 days of the contract obligation date.
--   * Monthly: comfortable interval to re-pull a wide time window
--     for substrate freshness without burning the API quota.
--
-- We register the cadence as 'monthly' because that's the platform's
-- intended pull cadence (one monthly Dagster materialization per
-- fiscal-year-to-date window). expected_lag_hours = 720 (= 30 days)
-- absorbs a missed weekly tick without flapping. Operators backfilling
-- older fiscal years run nj-ingest-usaspending fetch-and-load --fiscal-
-- year YYYY directly, which bypasses the calendar.
-- ============================================================================

INSERT INTO ref.release_calendar
    (source_id,                  cadence,    schedule_label,                                                            day_of_week, day_of_month, month_of_year, time_of_day_local, timezone,           expected_lag_hours, notes)
VALUES
    ('raw.usaspending_award',    'monthly',  'Weekly snapshots upstream; platform pulls monthly',                              NULL,           15,          NULL,         '00:00:00', 'America/New_York',                720, 'Source: POST https://api.usaspending.gov/api/v2/search/spending_by_award/. Filter: place_of_performance state=NJ, award_type_codes=[A,B,C,D]. Pulls fiscal-year-to-date on each tick. Anonymous rate limit ~60 req/min; ~1000 pages per FY at limit=100 = ~17 min per FY pull.')
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
