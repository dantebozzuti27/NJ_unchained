-- ============================================================================
-- Migration: 055_usaspending_award
--
-- TIER 4 v3 / FRAUD-F1 (substrate slice): USAspending.gov federal
-- award data with place of performance in NJ.
--
-- WHAT IT IS
-- ----------
-- USAspending.gov is the federal authoritative source for awards
-- (contracts, grants, loans, IDV) under the DATA Act (P.L. 113-101).
-- Free public REST API, no authentication, well-documented field
-- shapes since the 2018 v2 API release. This migration ships the
-- substrate for federal CONTRACT awards (award_type_codes A/B/C/D)
-- whose place of performance is in New Jersey.
--
-- WHY CONTRACTS-ONLY FOR V1
-- -------------------------
-- Three reasons for the narrowed scope:
--   1. Contracts have the cleanest field semantics (recipient,
--      dollar amount, period of performance, awarding agency) and the
--      highest fraud-relevance per row. Grants, loans, and IDV
--      (indefinite-delivery vehicle) have award-type-specific fields
--      that benefit from per-type tables once the contract path is
--      proven.
--   2. The fraud-engine join motive is to surface FEC donors and LEIE-
--      excluded entities who are also receiving federal money. That
--      join works best at the recipient-name level, where contract
--      data is densest and most reliable.
--   3. A contracts-only schema lands and stabilizes in one session;
--      a unified-all-types schema is a multi-session design with
--      legitimate normalization questions about how to handle
--      type-specific columns. Substrate-honesty: ship the narrow
--      scope first, document what's missing, expand deliberately.
--
-- WHY PLACE-OF-PERFORMANCE = NJ AS THE FILTER DIMENSION
-- -----------------------------------------------------
-- "Federal money flowing to New Jersey" can be cut multiple ways:
--   * Place of performance = NJ: where the work is done. NJ-physical
--     economic impact (the operative cut for "is the platform tracking
--     federal dollars in New Jersey").
--   * Recipient state = NJ: where the awardee is headquartered. NJ
--     companies winning federal money regardless of where they perform
--     (the operative cut for "are NJ companies receiving federal
--     awards").
-- For this v1 substrate we filter on PLACE OF PERFORMANCE; the
-- recipient state is captured as a separate column on every row, so
-- the second cut is recoverable by a downstream WHERE clause without
-- a second API pull. A future iteration may add a second pull for
-- recipient_state=NJ ∧ pop ≠ NJ to capture awards by NJ companies
-- doing work elsewhere (e.g., Bergen-based defence contractor working
-- at Camp Lejeune); for v1, those rows are intentionally absent.
--
-- WHAT'S NOT IN THIS MIGRATION
-- ----------------------------
-- 1. Grants / loans / IDV award types -- separate raw tables, separate
--    sessions. The schema below contains a few contract-specific
--    columns (PIID, period_of_performance_*) that would be NULL for
--    other award types; better to keep them populated than retrofit a
--    type-discriminator + nullable shape later.
-- 2. Sub-awards (recipients of a prime recipient's pass-through
--    funding). Sub-award data has a separate API endpoint and a much
--    larger row count; a separate ingester slice.
-- 3. Per-transaction modifications. The API exposes both award-level
--    aggregates and transaction-level deltas. v1 captures the AWARD
--    AGGREGATE (current_total_value); transaction history is a
--    follow-on table.
-- 4. Pre-2007 awards. The /search/spending_by_award endpoint is
--    capped at 2007-10-01 by USAspending; older data requires the
--    bulk-download endpoint, which produces multi-GB CSVs. Out of
--    scope until those backfill volumes are needed.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- raw.usaspending_award
-- ----------------------------------------------------------------------------
-- One row per federal contract award with place of performance in NJ.
-- Primary key is USAspending's stable generated_unique_award_id (the
-- string identifier they use as the URL slug for an award page;
-- canonical and immutable across modifications to a given award).
--
-- last_seen_at + UPSERT semantics mirror raw.hhs_oig_leie: any pull
-- bumps last_seen_at on rows present in the new fetch; rows that fall
-- behind are awards that aged off the platform's pull window (a
-- closed contract no longer in the date range, or a USAspending
-- correction that retired the row).
-- ----------------------------------------------------------------------------
CREATE TABLE raw.usaspending_award (
    -- USAspending's stable URL-slug identifier; canonical PK.
    generated_unique_award_id  TEXT          NOT NULL
        CHECK (generated_unique_award_id <> ''),

    -- Award-shape identifiers. award_id_piid is the contract Procurement
    -- Instrument Identifier; nullable because the API occasionally
    -- redacts it for sensitive awards.
    award_id_piid              TEXT,
    award_internal_id          BIGINT,

    -- Award type. The API filter constrains to {A, B, C, D} for this
    -- table -- a CHECK enforces that contract-only invariant at the
    -- raw layer. award_type_description (e.g., 'BPA Call', 'Definitive
    -- Contract') is human-readable.
    award_type_code            CHAR(1)       NOT NULL
        CHECK (award_type_code IN ('A', 'B', 'C', 'D')),
    award_type_description     TEXT,

    -- Recipient identity. UEI replaced DUNS in 2022; both can be NULL
    -- (UEI for pre-2022 awards, DUNS for post-2022 awards), and SOME
    -- awards historically have neither (small-purchase exemptions). We
    -- preserve all three columns rather than coalescing.
    recipient_name             TEXT,
    recipient_uei              TEXT,
    recipient_duns             TEXT,
    recipient_country_code     TEXT,
    recipient_state            TEXT,
    recipient_city             TEXT,
    recipient_county_name      TEXT,
    recipient_zip5             TEXT,
    recipient_zip4             TEXT,
    recipient_congressional_district TEXT,

    -- Place of performance. Even though the API filter pinned
    -- pop_state=NJ, we capture all the API-returned pop fields so
    -- downstream queries can roll up by county / congressional
    -- district within NJ.
    pop_country_code           TEXT,
    pop_state                  TEXT,
    pop_city                   TEXT,
    pop_county_name            TEXT,
    pop_zip5                   TEXT,
    pop_zip4                   TEXT,
    pop_congressional_district TEXT,

    -- Money. NUMERIC(20, 2) gives 18 digits of integer precision (up
    -- to ~10^16 cents = $100T) which exceeds any single award. The
    -- API returns dollars-with-cents; we store dollars-with-cents.
    award_amount               NUMERIC(20, 2),
    description                TEXT,

    -- Agencies. We capture the full chain (agency / sub-agency / funding-
    -- agency) because procurement-fraud patterns often involve
    -- relationships between awarding-sub-agency and recipient that
    -- span multiple sub-agencies under the same parent agency.
    awarding_agency_name       TEXT,
    awarding_subagency_name    TEXT,
    funding_agency_name        TEXT,
    funding_subagency_name     TEXT,
    awarding_agency_id         INT,
    agency_slug                TEXT,

    -- Time period of performance. The API offers two date-shape pairs:
    -- (Start Date, End Date) and (Period of Performance Start Date,
    -- Period of Performance Current End Date). The first pair is
    -- always populated for contract awards; the second is sometimes
    -- NULL even when the first is populated. We capture the first as
    -- the canonical period and add the second as overrides_*
    -- alternatives when populated.
    period_start               DATE,
    period_end                 DATE,
    period_pop_start           DATE,
    period_pop_current_end     DATE,
    last_modified_at           TIMESTAMPTZ,

    -- Provenance. fiscal_year_pulled is the FY the operator filtered
    -- for; an award whose period_start spans multiple FYs may appear
    -- once per FY pull. UPSERT semantics on the PK collapse those to
    -- one row, with the latest pull's fiscal_year_pulled "winning"
    -- on conflict.
    fiscal_year_pulled         SMALLINT      NOT NULL
        CHECK (fiscal_year_pulled BETWEEN 2008 AND 2100),
    -- Filter fingerprint: SHA-256 of the JSON filter object that
    -- produced this row. A change in the filter shape (new state, new
    -- award-type set) produces a different fingerprint, so the
    -- operator can verify which pull's filter generated which rows.
    api_query_filter_sha256    CHAR(64)      NOT NULL
        CHECK (api_query_filter_sha256 ~ '^[0-9a-f]{64}$'),
    page_number                INT,

    fetched_at                 TIMESTAMPTZ   NOT NULL DEFAULT now(),
    last_seen_at               TIMESTAMPTZ   NOT NULL DEFAULT now(),

    PRIMARY KEY (generated_unique_award_id),

    -- An award row should always have either a recipient name or a
    -- recipient UEI. All-NULL recipient fields would mean USAspending
    -- redacted the entire identity, which is rare and worth surfacing
    -- as a parser bug rather than silently loading.
    CHECK (
        COALESCE(NULLIF(recipient_name, ''),
                 NULLIF(recipient_uei, ''),
                 NULLIF(recipient_duns, '')) IS NOT NULL
    ),

    -- Sanity: amount is non-negative when present. USAspending
    -- occasionally publishes negative-amount rows for de-obligations
    -- (a contract modification reducing the total value); those are
    -- legitimately negative on the AWARD aggregate when total
    -- de-obligations exceed obligations. We accept them but use
    -- raw_amount on the analytics layer with separate filters.
    -- (No CHECK constraint here: legitimate values can be negative.)
    CHECK (TRUE)
);

COMMENT ON TABLE raw.usaspending_award IS
    'Federal contract awards (award_type_codes A/B/C/D) with place of '
    'performance in New Jersey. Pulled from '
    'POST /api/v2/search/spending_by_award/. UPSERT-by-'
    'generated_unique_award_id; last_seen_at advances on every pull '
    'for present rows. Recipient state may be != NJ (out-of-state '
    'company performing NJ work).';

COMMENT ON COLUMN raw.usaspending_award.generated_unique_award_id IS
    'USAspending''s stable URL-slug identifier (e.g., '
    '''CONT_AWD_WE31_9700_N6247016D9008_9700''). Immutable across '
    'modifications to a given award; canonical PK.';

COMMENT ON COLUMN raw.usaspending_award.award_amount IS
    'Current total value of the award (the sum of all transactions); '
    'CAN be negative if cumulative de-obligations exceed obligations.';

COMMENT ON COLUMN raw.usaspending_award.fiscal_year_pulled IS
    'Federal fiscal year the operator filtered for when ingesting this '
    'row. Awards spanning multiple FYs are returned in each FY''s '
    'pull; UPSERT collapses them, with the latest pull winning.';

COMMENT ON COLUMN raw.usaspending_award.api_query_filter_sha256 IS
    'SHA-256 of the JSON filter object used to fetch this row; lets '
    'the operator audit which pull contributed each row''s data.';


-- Indexes:
--   * recipient lookups (FEC / LEIE join motive)
--   * agency lookups (per-agency rollups in analyst views)
--   * period rollups (last 12 months / specific FY)
CREATE INDEX raw_usaspending_award_recipient_uei_idx
    ON raw.usaspending_award (recipient_uei)
    WHERE recipient_uei IS NOT NULL AND recipient_uei <> '';

CREATE INDEX raw_usaspending_award_recipient_name_idx
    ON raw.usaspending_award (recipient_name)
    WHERE recipient_name IS NOT NULL AND recipient_name <> '';

CREATE INDEX raw_usaspending_award_pop_state_idx
    ON raw.usaspending_award (pop_state, pop_county_name);

CREATE INDEX raw_usaspending_award_period_idx
    ON raw.usaspending_award (period_start, period_end);

CREATE INDEX raw_usaspending_award_fy_idx
    ON raw.usaspending_award (fiscal_year_pulled);

CREATE INDEX raw_usaspending_award_last_seen_idx
    ON raw.usaspending_award (last_seen_at);


-- ----------------------------------------------------------------------------
-- Canonical view: only NJ-pop awards within the recent active window
-- ----------------------------------------------------------------------------
-- Filters to currently-active rows (last_seen_at within 35 days of the
-- max -- a wider window than LEIE's 7 days because USAspending pulls
-- are typically scheduled monthly). Also surfaces the recipient name
-- in the platform's canonical "LAST|FIRST" form for FEC / LEIE
-- joining (when the recipient is a person, not a corp). Corporate
-- recipients yield NULL canonical name -- they will join via a
-- separate corporate-name canonicalization layer that's out of scope
-- for v1.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.v_usaspending_award_active AS
WITH latest AS (
    SELECT MAX(last_seen_at) AS most_recent FROM raw.usaspending_award
)
SELECT
    a.generated_unique_award_id,
    a.award_id_piid,
    a.award_type_code,
    a.award_type_description,

    a.recipient_name,
    a.recipient_uei,
    a.recipient_duns,
    a.recipient_state,
    a.recipient_city,
    a.recipient_zip5,
    a.recipient_county_name,
    a.recipient_congressional_district,
    -- Canonical individual-name key when the recipient name parses as
    -- "LAST, FIRST [MIDDLE]". NULL for corporate recipients (most of
    -- them). The fraud-engine entity-match layer (separate session)
    -- joins this against derived.v_leie_individual_canonical and
    -- against canonicalized FEC donor / treasurer names. NULL means
    -- "no individual match attempt"; corporate-name matching is a
    -- separate canonicalization function.
    derived.f_canonical_lastfirst_from_fec(a.recipient_name)
                                                    AS recipient_canonical_individual,

    a.pop_state,
    a.pop_city,
    a.pop_county_name,
    a.pop_zip5,
    a.pop_congressional_district,

    a.award_amount,
    a.description,
    a.awarding_agency_name,
    a.awarding_subagency_name,
    a.funding_agency_name,

    a.period_start,
    a.period_end,
    a.last_modified_at,
    a.fiscal_year_pulled,
    a.last_seen_at
FROM raw.usaspending_award a, latest
WHERE a.last_seen_at >= latest.most_recent - INTERVAL '35 days';

COMMENT ON VIEW derived.v_usaspending_award_active IS
    'Currently-active USAspending NJ-pop contract awards (last_seen_at '
    'within 35 days of freshest pull). Adds a recipient_canonical_'
    'individual column derived via derived.f_canonical_lastfirst_from_fec '
    'so the cross-source signal layer can join NJ-recipient awards '
    'against FEC donors and LEIE-excluded individuals on the same '
    'canonical key. Corporate recipients yield NULL canonical_individual.';
