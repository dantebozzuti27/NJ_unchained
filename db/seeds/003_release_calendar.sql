-- ============================================================================
-- Seed: 003_release_calendar
--
-- The platform's known release schedule per source. Each row is one
-- TIER 1 ingester. UPSERT-able so vintage updates to the schedule
-- (e.g. BLS shifts CPI release to 09:00 ET) are a one-row INSERT.
--
-- Schedule sources of truth:
--   BLS CPI:       https://www.bls.gov/schedule/news_release/cpi.htm
--   FRED MORTGAGE: Freddie Mac PMMS releases Thursdays at noon ET
--   FRED Treasury: published next-business-day
--   FHFA HPI:      https://www.fhfa.gov/data/hpi (quarterly)
--   ACS 1-yr:      September each year
--   ACS 5-yr:      December each year
--   DOL OFLC LCA:  ~3 months after fiscal-quarter end
--   NJ DCA tax:    ~January each year
--   HUD ZIP:       quarterly, operator-staged (auth required)
-- ============================================================================

INSERT INTO ref.release_calendar
    (source_id,                  cadence,     schedule_label,                                      day_of_week, day_of_month, month_of_year, time_of_day_local, timezone,            expected_lag_hours, notes)
VALUES
    ('raw.fred_observation',     'weekly',    'Thursdays at 12:00 ET (Freddie Mac PMMS)',                    4,         NULL,          NULL,         '12:00:00', 'America/New_York',                 48, 'MORTGAGE30US releases Thu noon. DGS10/FEDFUNDS daily/monthly; weekly is the bottleneck cadence.'),
    ('raw.cpi_u',                'monthly',   '~10th of month at 08:30 ET (BLS CPI release)',              NULL,           10,          NULL,         '08:30:00', 'America/New_York',                 48, 'BLS CPI release. Day-of-month varies (10th-15th); see BLS release calendar.'),
    ('raw.fhfa_hpi_county',      'quarterly', 'Last Tuesday of Feb/May/Aug/Nov at 09:00 ET',               NULL,         NULL,             2,         '09:00:00', 'America/New_York',                240, 'FHFA HPI quarterly release. Last-Tuesday-of-quarter-month logic in scheduler.'),
    ('raw.acs_median_household_income', 'annual', 'December (ACS 5-year) and September (ACS 1-year)',       NULL,         15,            12,         '00:00:00', 'America/New_York',                720, 'ACS 5-yr: December. 1-yr: September. 2020 ACS 1-yr was suppressed (COVID).'),
    ('raw.acs_housing',          'annual',    'December (ACS 5-year) and September (ACS 1-year)',          NULL,         15,            12,         '00:00:00', 'America/New_York',                720, 'Same calendar as raw.acs_median_household_income.'),
    ('raw.lca_disclosure',       'quarterly', '~3 months after fiscal-quarter end (DOL OFLC)',             NULL,         NULL,          NULL,         '00:00:00', 'America/New_York',                720, 'DOL fiscal year is Oct-Sep. FY24Q3 (Apr-Jun 2024) released ~Sep 2024.'),
    ('raw.nj_property_tax_county', 'annual',  'January (NJ DCA Property Tax Tables)',                      NULL,           15,             1,         '00:00:00', 'America/New_York',                720, 'NJ DCA publishes prior-year tables in early-mid January.'),
    ('ref.zip_county',           'on_event',  'Operator-staged (HUD requires auth login)',                 NULL,         NULL,          NULL,             NULL, 'America/New_York',                720, 'huduser.gov requires registered account; auto-fetch not possible.')
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
