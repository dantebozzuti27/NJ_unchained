-- ============================================================================
-- Seed: 042_release_calendar_cms_partd
--
-- FRAUD-F7 substrate: CMS Medicare Part D Prescribers -- by Provider
-- release schedule.
--
-- CMS (Office of Enterprise Data & Analytics) publishes the Part D
-- Prescribers-by-Provider file ANNUALLY, one file per calendar year, with
-- an intrinsic ~1.5-2 year lag (claims must mature and be adjudicated):
-- e.g. the CY2023 file published mid-2025. The exact publication day drifts
-- within the mid-year window, so the orchestration layer polls and the
-- freshness budget below absorbs the slip.
--
-- Unlike the monthly LEIE full-replace, each annual file is a NEW partition
-- (data_year) -- old years are never revised in place, so staleness is
-- measured against "did this year's file land in its release window", not
-- "is the single mutable file current".
--
-- expected_lag_hours = 1440 (= 60 days) absorbs CMS's mid-year publication
-- drift without spurious staleness alarms; a file more than ~2 months past
-- its historical release window warrants operator investigation.
-- ============================================================================

INSERT INTO ref.release_calendar
    (source_id,                    cadence,   schedule_label,                                          day_of_week, day_of_month, month_of_year, time_of_day_local, timezone,           expected_lag_hours, notes)
VALUES
    ('raw.cms_partd_prescriber',   'annual',  'Mid-year (CMS Medicare Part D Prescribers - by Provider)',       NULL,           15,             6,         '00:00:00', 'America/New_York',               1440, 'Source: data.cms.gov, resolved via the data.json catalog. One CSV per calendar year, published ~1.5-2 years after the data year (e.g. CY2023 released mid-2025). Each year is a new data_year partition; old years are not revised. Substrate for the provider_excluded_billing signal (mig 101).')
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
