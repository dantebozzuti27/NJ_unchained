-- ============================================================================
-- Seed: 047_release_calendar_nppes
--
-- FRAUD-F7 Phase-3 substrate: NPPES NPI Registry release schedule.
--
-- CMS (NPPES / National Plan and Provider Enumeration System) publishes the
-- FULL-replace monthly dissemination file once per month, in the second week
-- of the month. The monthly file REPLACES the prior version (the ingester
-- TRUNCATEs + COPYs -- see ingestion/nppes.py and
-- db/migrations/108_raw_nppes_provider.sql); weekly incremental files exist
-- between monthly releases but the platform pulls the monthly V.2 full file.
-- Source landing page (lists every monthly + weekly file):
--   https://download.cms.gov/nppes/NPI_Files.html
--
-- Like the HHS-OIG LEIE (db/seeds/008_release_calendar_leie.sql), this is a
-- monthly full-replace snapshot fetched via conditional-GET; staleness is
-- measured against "did this month's file land", not a per-year partition.
--
-- day_of_month = 14: CMS posts the monthly file in the second week; pin to
-- the close of the second full week as the human-readable scheduled day.
-- expected_lag_hours = 480 (= 20 days): mirrors LEIE's monthly buffer --
-- absorbs CMS's second-week publication window plus a typical few-day slip
-- without spurious staleness alarms. The NPPES_FRESHNESS asset check
-- (25-day fail / 20-day warn, orchestration/assets.py) is the operational
-- enforcement; this calendar row is the human-readable schedule for the UI
-- and derived.v_data_freshness_summary.
-- ============================================================================

INSERT INTO ref.release_calendar
    (source_id,                cadence,    schedule_label,                                            day_of_week, day_of_month, month_of_year, time_of_day_local, timezone,           expected_lag_hours, notes)
VALUES
    ('raw.nppes_provider',     'monthly',  'Second week of month (NPPES monthly full dissemination)',        NULL,           14,          NULL,         '00:00:00', 'America/New_York',                480, 'Source: https://download.cms.gov/nppes/NPI_Files.html. Full-replace monthly ZIP (~10 GB national); ingester (ingestion/nppes.py) NJ-filters by default and TRUNCATE+COPYs one row per NPI into raw.nppes_provider. 480h = 20 days mirrors the LEIE monthly buffer and absorbs CMS''s second-week publication window. Identity spine for resolving name-only LEIE / NJ-Medicaid exclusions to a concrete NPI + NJ practice location (mig 108).')
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
