-- ============================================================================
-- Seed: 007_release_calendar_fec
--
-- TIER 4 v1: FEC bulk-data release schedule.
--
-- FEC's bulk download cadence is well-documented but cycle-dependent:
--   * During an active election cycle: bi-weekly refresh of all three
--     bulk file kinds (cn, cm, indiv) on the FEC's S3 mirror.
--   * Off-cycle: monthly refresh.
--
-- The schedule below uses the BI-WEEKLY label as the canonical
-- cadence; the longer monthly off-cycle window is absorbed by setting
-- expected_lag_hours to 14 days. That's the same pattern used for
-- raw.lca_disclosure (quarterly with a wider lag tolerance to absorb
-- the publication's actual irregularity).
--
-- All three FEC raw assets share the same release calendar entry
-- (registered as 'raw.fec' rather than per-table) because they are
-- always published TOGETHER -- the cn/cm/indiv files for a cycle
-- are part of one canonical update.
-- ============================================================================

INSERT INTO ref.release_calendar
    (source_id,           cadence,    schedule_label,                           day_of_week, day_of_month, month_of_year, time_of_day_local, timezone,           expected_lag_hours, notes)
VALUES
    -- cadence='monthly' is the conservative closest fit; the actual
    -- in-cycle cadence is bi-weekly. expected_lag_hours=336 (= 14d)
    -- accommodates the bi-weekly truth without forcing a CHECK
    -- constraint widening on ref.release_calendar.
    ('raw.fec',           'monthly',  'Bi-weekly during cycle, monthly off-cycle (FEC bulk)', NULL,        NULL,          NULL,         '00:00:00', 'America/New_York',                336, 'FEC bulk refreshes every two weeks during an active cycle (cn/cm/indiv published together) and monthly off-cycle. Source: https://www.fec.gov/files/bulk-downloads/')
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
