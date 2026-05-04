-- ============================================================================
-- Migration: 030_release_calendar
--
-- TIER 0.5: Per-source release schedule. The Bloomberg ECO <GO>
-- equivalent: "what releases when" for every dataset the platform
-- tracks. Three uses:
--
--   1. Sensors compare actual ingest time vs expected release time;
--      late-by-N-hours fires a freshness violation.
--
--   2. Schedules are derived from this table (one ScheduleDefinition
--      per row), so adding a new source is INSERT-data, not deploy-code.
--
--   3. The /release-calendar API endpoint serves this table directly,
--      letting the front-end render an upcoming-releases panel without
--      hard-coded knowledge of cadences.
--
-- CADENCE SEMANTICS
-- -----------------
-- We support three cadence shapes that cover all observed sources:
--
--   * 'weekly'    : day_of_week (1=Mon..7=Sun), time_of_day_local
--   * 'monthly'   : day_of_month_pattern ('5', '5-15', 'last_thursday'),
--                   time_of_day_local
--   * 'quarterly' : month_offset_in_quarter, day_of_month_pattern,
--                   time_of_day_local
--   * 'annual'    : month_of_year, day_of_month_pattern,
--                   time_of_day_local
--   * 'on_event'  : externally triggered (e.g. operator-staged HUD).
--                   schedule columns are NULL; sensors must observe
--                   the staging directory.
--
-- We store the human-readable schedule as a single TEXT column (e.g.
-- "2nd Tuesday at 08:30 ET") so the calendar table is self-documenting.
-- The structured columns drive automation; the text column is the
-- source of truth for how a human reads it.
-- ============================================================================

CREATE TABLE ref.release_calendar (
    source_id          TEXT          PRIMARY KEY
        CHECK (source_id ~ '^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$'),
        -- e.g. "raw.fred_observation", "raw.cpi_u". Matches the
        -- @asset key naming convention.

    cadence            TEXT          NOT NULL
        CHECK (cadence IN ('daily', 'weekly', 'monthly', 'quarterly', 'annual', 'on_event')),

    -- Human-readable schedule. Authoritative for the UI/docs;
    -- structured fields below drive the scheduler.
    schedule_label     TEXT          NOT NULL,

    -- Cron-like fields. Nullable when cadence='on_event' or when a
    -- given dimension does not apply (day_of_week is null for monthly).
    -- We do NOT store a literal crontab string because the cadence
    -- shapes we support (e.g. "last Tuesday of February/May/Aug/Nov")
    -- are not expressible in standard cron syntax.
    day_of_week        SMALLINT      CHECK (day_of_week IS NULL OR day_of_week BETWEEN 1 AND 7),
    day_of_month       SMALLINT      CHECK (day_of_month IS NULL OR day_of_month BETWEEN 1 AND 31),
    month_of_year      SMALLINT      CHECK (month_of_year IS NULL OR month_of_year BETWEEN 1 AND 12),
    time_of_day_local  TIME,
    timezone           TEXT          NOT NULL DEFAULT 'America/New_York',

    -- Slack on top of the official cadence before declaring stale.
    -- E.g. BLS sometimes publishes 1-2 days late; default 48h buffer.
    -- A FreshnessPolicy on the corresponding asset uses this value.
    expected_lag_hours INTEGER       NOT NULL DEFAULT 48
        CHECK (expected_lag_hours >= 0),

    -- Free-form notes (publication delays, known issues, link to
    -- the official release calendar page).
    notes              TEXT,

    created_at         TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ   NOT NULL DEFAULT now()
);

COMMENT ON TABLE ref.release_calendar IS
    'Per-source publication schedule. Drives Dagster ScheduleDefinitions '
    'and FreshnessPolicy lag budgets. The /release-calendar API serves '
    'this table directly (Bloomberg ECO <GO> equivalent).';

CREATE INDEX ref_release_calendar_cadence_idx ON ref.release_calendar (cadence);


-- Convenience view: NEXT expected release per source, computed from
-- (cadence, day_of_week, day_of_month, month_of_year). Returns NULL
-- next_expected_at for cadence='on_event' (no schedule).
--
-- This is intentionally approximate: "next Thursday at noon" is
-- straightforward; "last Tuesday of next quarter month" needs more
-- date arithmetic than we want in pure SQL. The orchestration layer
-- computes the precise next-release timestamp; this view is for
-- human/UI consumption.
CREATE VIEW public.v_next_release AS
SELECT
    source_id,
    cadence,
    schedule_label,
    expected_lag_hours,
    timezone,
    notes,
    -- Coarse approximation; refine in code.
    CASE cadence
        WHEN 'daily'     THEN now()::date + INTERVAL '1 day'
        WHEN 'weekly'    THEN now()::date + INTERVAL '7 days'
        WHEN 'monthly'   THEN now()::date + INTERVAL '30 days'
        WHEN 'quarterly' THEN now()::date + INTERVAL '90 days'
        WHEN 'annual'    THEN now()::date + INTERVAL '365 days'
        WHEN 'on_event'  THEN NULL
    END AS next_expected_at_approx
FROM ref.release_calendar
ORDER BY source_id;
