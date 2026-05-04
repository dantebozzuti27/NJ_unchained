-- ============================================================================
-- Seed: 004_release_calendar_pums
--
-- Adds the ACS PUMS person + housing rows to ref.release_calendar. Lives
-- in a separate seed file (rather than appended to 003) so the platform's
-- drift-detection invariant (a seed file is never edited after deploy)
-- is preserved.
--
-- Schedule: ACS 1-year PUMS for survey year Y publishes ~mid-October of
-- Y+1, ~6-8 weeks AFTER the tabular ACS releases. Census's exact PUMS
-- publication date drifts within October; the orchestration layer polls
-- daily through October.
-- ============================================================================

INSERT INTO ref.release_calendar
    (source_id,                  cadence,     schedule_label,                                              day_of_week, day_of_month, month_of_year, time_of_day_local, timezone,            expected_lag_hours, notes)
VALUES
    ('raw.acs_pums_person',      'annual',    'October (ACS 1-year PUMS, ~6 weeks after tabular release)',          NULL,           15,            10,         '00:00:00', 'America/New_York',                720, 'PUMS 1-year for survey year Y publishes mid-October of Y+1. Drift +/- 2 weeks; orchestration polls daily through October.'),
    ('raw.acs_pums_housing',     'annual',    'October (ACS 1-year PUMS, paired with person file)',                 NULL,           15,            10,         '00:00:00', 'America/New_York',                720, 'Same calendar as raw.acs_pums_person; published as a paired drop. Loaded as a side-effect of the person asset (single fetch, two COPYs).')
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
