-- ============================================================================
-- Migration: 067_utc_anchor_age_functions
--
-- Detection-quality fix: anchor every date-arithmetic surface to UTC
-- explicitly, removing the silent dependence on the database session's
-- TimeZone setting.
--
-- THE BUG
-- -------
-- `derived.f_leie_age_decay(p_excldate)` and the
-- `derived.v_sam_exclusion_active` view both reference `CURRENT_DATE`,
-- which Postgres derives from the session's TimeZone GUC. Two
-- consequences:
--
--   * Two Neon instances of the same schema in different regions
--     (us-east-1 default UTC vs us-east-2 with TimeZone='America/New_York')
--     compute DIFFERENT decay weights for the same exclusion, and
--     mark different SAM rows as "active" within the ±1-day window
--     around midnight. Identical input data, divergent fraud queue.
--     That is a substrate-honesty violation: the L1 substrate must
--     be a deterministic function of (raw input, function code), not
--     of (raw input, function code, session TZ).
--
--   * Tests using Python's `dt.date.today()` (local time) collide
--     with Postgres CURRENT_DATE (session TZ). At ~8pm Eastern the
--     UTC date has already rolled over, so a test that seeds
--     excldate=today (local) and expects decay=1.0 instead sees
--     decay=exp(-1/365.25/10) ~= 0.9997. This was the root cause of
--     27 phantom test failures we lived with for ~weeks.
--
-- THE FIX
-- -------
-- Both surfaces switch from `CURRENT_DATE` to `((NOW() AT TIME ZONE 'UTC')::DATE)`,
-- which is the UTC date REGARDLESS of session TZ. The function
-- contract is unchanged (signature, return type, NULL-handling,
-- future-clamp semantics, IMMUTABLE-vs-STABLE classification all
-- preserved); only the implementation is more deterministic.
--
-- WHY UTC AND NOT THE FEDERAL REGULATOR'S LOCAL TZ
-- -----------------------------------------------
-- HHS-OIG and SAM.gov both publish exclusion dates as DATE-typed,
-- not TIMESTAMP-typed; the publisher's local-vs-UTC discrepancy is
-- already lost in the source data and irrelevant to consumers.
-- UTC is the universal anchor that lets two consumers in different
-- regions agree byte-for-byte on the decay weight. We could pick
-- "America/New_York" instead, which would mirror the HHS-OIG
-- (Maryland) wall clock; the practical effect is the same modulo
-- the ~5-hour offset of when CURRENT_DATE rolls over.
--
-- COMPATIBILITY
-- -------------
-- This migration is a CREATE OR REPLACE for the function and a
-- CREATE OR REPLACE VIEW for the view. No data is touched. Existing
-- L1 rows that were materialized under the buggy CURRENT_DATE will
-- be replaced on the next refresher run; any analyst who has bookmarked
-- a specific raw_value will see it shift by at most one day's worth
-- of decay (~0.027% of the raw_value for fresh exclusions; smaller
-- for old ones). That is well below normal cycle-to-cycle drift and
-- does not warrant an explicit cache-bust.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1. derived.f_leie_age_decay (UTC anchor)
-- ----------------------------------------------------------------------------
-- Identical signature, identical NULL/future-clamp semantics. The only
-- change is `CURRENT_DATE` -> `((NOW() AT TIME ZONE 'UTC')::DATE)`.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.f_leie_age_decay(
    p_excldate DATE
) RETURNS NUMERIC
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    SELECT EXP(
        -GREATEST(
            0::NUMERIC,
            (((NOW() AT TIME ZONE 'UTC')::DATE)
             - COALESCE(p_excldate,
                        ((NOW() AT TIME ZONE 'UTC')::DATE)))::NUMERIC
            / 365.25::NUMERIC
        ) / 10::NUMERIC
    );
$$;

COMMENT ON FUNCTION derived.f_leie_age_decay(DATE) IS
    'Exponential time-decay weight for LEIE/SAM exclusion age. Returns '
    'exp(-years_since_excldate / 10), in (0, 1]. NULL excldate -> 1.0 '
    '(no decay; we cannot date-discount what we cannot date). Future '
    'excldate -> 1.0 (clamped age=0). UTC-anchored: independent of the '
    'database session TimeZone, so two Neon instances in different '
    'regions compute byte-identical weights.';


-- ----------------------------------------------------------------------------
-- 2. derived.v_sam_exclusion_active (UTC anchor)
-- ----------------------------------------------------------------------------
-- Same column projection as in 063, only the WHERE clause changes:
-- termination_date is compared to today (UTC) rather than today
-- (session TZ). A SAM exclusion that terminates at midnight UTC is
-- treated as expired everywhere on the planet at the same instant.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.v_sam_exclusion_active AS
SELECT
    record_hash,
    classification,
    name, prefix, first, middle, last, suffix, title,
    uei, duns, cage, npi,
    address1, address2, address3, address4,
    city, state_province, country, zip,
    exclusion_program, excluding_agency_name, exclusion_type_desc,
    active_date, termination_date,
    record_status, cross_reference, sam_number,
    additional_comments, open_data_flag, creation_date,
    vintage_day, source_url, source_sha256,
    ingested_at, last_seen_at
FROM raw.sam_gov_exclusion
WHERE (termination_date IS NULL
       OR termination_date > ((NOW() AT TIME ZONE 'UTC')::DATE))
  AND (record_status IS NULL OR LOWER(record_status) <> 'inactive');

COMMENT ON VIEW derived.v_sam_exclusion_active IS
    'Currently-active SAM.gov exclusions. Filters out terminated '
    '(termination_date <= UTC today) and explicitly Inactive rows. '
    'Cross-source signals join against this view, not the raw table. '
    'UTC-anchored: termination boundary is the same instant for all '
    'consumers regardless of database session TimeZone.';
