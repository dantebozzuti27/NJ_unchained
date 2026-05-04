-- ============================================================================
-- Seed: 008_release_calendar_leie
--
-- TIER 4 v3 / FRAUD-F5 substrate: HHS-OIG LEIE release schedule.
--
-- HHS-OIG publishes the full LEIE database CSV monthly, by the 10th
-- of the month. The same URL (oig.hhs.gov/exclusions/downloadables/
-- UPDATED.csv) is reused; the file content changes each month.
--
-- expected_lag_hours = 480 (= 20 days) absorbs the publication
-- window: HHS guarantees "by the 10th" but their actual publish day
-- has historically slipped to the 12th or 13th. Adding ~10 days of
-- buffer past the 10th gives a fail-safe budget without forcing
-- spurious staleness alarms.
-- ============================================================================

INSERT INTO ref.release_calendar
    (source_id,           cadence,    schedule_label,                                                            day_of_week, day_of_month, month_of_year, time_of_day_local, timezone,           expected_lag_hours, notes)
VALUES
    ('raw.hhs_oig_leie',  'monthly',  '~10th of month (HHS-OIG full LEIE database)',                                    NULL,           10,          NULL,         '00:00:00', 'America/New_York',                480, 'Source: https://oig.hhs.gov/exclusions/downloadables/UPDATED.csv. The file is replaced monthly with the entire current dataset; reinstated entities silently disappear. The platform tracks reinstatements via raw.hhs_oig_leie.last_seen_at falling behind.')
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
