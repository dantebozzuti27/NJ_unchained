-- ============================================================================
-- Migration: 027_pums_burden_segmented
--
-- TIER 3 derived view: person-level housing burden ratios from ACS PUMS,
-- segmented by tenure x demographic dimension.
--
-- WHY THIS EXISTS
-- ---------------
-- The platform's headline aggregate burden (derived.housing_burden_ratio)
-- answers "is Bergen burdened?" but cannot answer the BBG-terminal-style
-- question:
--
--     "Are Hispanic renters in Bergen aged 25-34 burdened more than
--      white renters? More than non-citizens? More than 5 years ago?"
--
-- This table provides the substrate for that question. It is the platform's
-- first MATERIALIZED derived table (every previous derived asset is a SQL
-- view). The materialization is justified because:
--
--   * Computing weighted percentiles across ~100K rows x 80 replicate
--     weights is expensive (~2-5 seconds per cell). Doing it on every
--     read would make the API unusable for any non-trivial dashboard.
--   * The inputs (raw.acs_pums_*) refresh annually. Computing once at
--     refresh and storing the result wastes nothing.
--   * The output is small (~4K rows for NJ), so storage cost is trivial.
--
-- SCHEMA SHAPE: LONG-FORMAT SEGMENTATION
-- --------------------------------------
-- We use a "long-format" schema where each row carries explicit
-- (segment_dim, segment_value) columns rather than wide columns per
-- segment dimension. Why:
--
--   * Adding a new segment dimension (e.g. educational attainment) is a
--     compute-side change only -- no schema migration.
--   * API queries are uniformly shaped: WHERE segment_dim = 'race'.
--   * Aggregation across segments is trivial in SQL.
--
-- The cost is that one row per (puma, year, tenure, dim, value) triples
-- the row count compared to a wide schema, but the data is so small that
-- this doesn't matter (~4-10K rows for NJ).
--
-- GEOGRAPHY: PUMA, NOT COUNTY
-- ---------------------------
-- We expose at PUMA grain because PUMS reports PUMA, not county. PUMA-
-- to-county allocation is a separate concern (a future ref.puma_county_xwalk
-- migration). Consumers who want county-level aggregates today can
-- approximate via "PUMA mostly in county X" mapping; the derived table
-- documents this limitation explicitly.
--
-- SUPPRESSION
-- -----------
-- Cells with weighted_n < 1000 are SUPPRESSED: the burden_ratio columns
-- are NULL and `suppressed = TRUE`. This mirrors ACS / Census disclosure-
-- avoidance practice. Consumers must check `suppressed` before plotting.
-- The 1000 floor is explicit, configurable in the derived/pums_burden.py
-- compute module, and re-evaluated annually as PUMS sample sizes change.
-- ============================================================================

CREATE TABLE derived.pums_burden_segmented (
    -- Natural key
    year             SMALLINT  NOT NULL CHECK (year BETWEEN 2017 AND 2099),
    product          TEXT      NOT NULL CHECK (product IN ('acs1', 'acs5')),
    state_fips       CHAR(2)   NOT NULL CHECK (state_fips ~ '^[0-9]{2}$'),
    puma             CHAR(5)   NOT NULL CHECK (puma ~ '^[0-9]{5}$'),

    -- Tenure class (renter / owner-with-mtg / owner-no-mtg). Person
    -- records inherit tenure from their housing record via SERIALNO.
    tenure_class     TEXT      NOT NULL
        CHECK (tenure_class IN ('renter', 'owner_w_mtg', 'owner_no_mtg')),

    -- Segment dimension + value. 'overall' produces one row per
    -- (puma, year, tenure) with no demographic split.
    segment_dim      TEXT      NOT NULL
        CHECK (segment_dim IN (
            'overall', 'race', 'hispanic', 'citizenship', 'age_band'
        )),
    segment_value    TEXT      NOT NULL,

    -- Weighted population estimate. Sum of pwgtp across the cell's
    -- person rows. INTEGER because PUMS weights are integers and the
    -- sum stays well under 2^31 even for the largest cells.
    weighted_n       INTEGER   NOT NULL CHECK (weighted_n >= 0),

    -- Sample size (un-weighted). For SE estimation downstream and for
    -- transparency about how many actual respondents the cell summarizes.
    sample_n         INTEGER   NOT NULL CHECK (sample_n >= 0),

    -- Weighted median household income for the cell.
    -- Income comes from the housing record's HINCP (household total).
    -- NULL when the cell is suppressed.
    household_income_p50  NUMERIC(12, 2)
        CHECK (household_income_p50 IS NULL OR household_income_p50 >= 0),

    -- Weighted median monthly housing cost for the cell.
    -- Renters: GRNTP. Owners: SMOCP. NULL when suppressed.
    monthly_cost_p50      NUMERIC(10, 2)
        CHECK (monthly_cost_p50 IS NULL OR monthly_cost_p50 >= 0),

    -- Burden ratio = (annualized cost) / (household income), computed at
    -- the cell level as median_cost*12 / median_income. NOT a median of
    -- per-row ratios -- that's noisier and methodologically inferior.
    -- This matches how Census / HUD compute headline burden numbers.
    burden_ratio_p50      NUMERIC(8, 4)
        CHECK (burden_ratio_p50 IS NULL OR burden_ratio_p50 >= 0),

    -- Suppression flag: TRUE if cell weighted_n < disclosure floor.
    suppressed       BOOLEAN   NOT NULL DEFAULT FALSE,

    -- Provenance
    formula_version  TEXT          NOT NULL,
    input_vintage_hash CHAR(64)    NOT NULL,
    computed_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),

    PRIMARY KEY (year, product, puma, tenure_class, segment_dim, segment_value)
);

COMMENT ON TABLE derived.pums_burden_segmented IS
    'Person-level housing burden ratios derived from ACS PUMS, segmented '
    'by tenure x demographic dimension at PUMA grain. Materialized table '
    '(not view) because computing weighted percentiles is expensive but '
    'the output is small. burden_ratio_p50 is median_cost*12 / median_income, '
    'NOT median of per-row ratios. Cells with weighted_n < 1000 are '
    'suppressed (suppressed=TRUE, ratio columns NULL).';

CREATE INDEX pums_burden_segmented_dim_value_idx
    ON derived.pums_burden_segmented (year, segment_dim, segment_value);

CREATE INDEX pums_burden_segmented_puma_year_idx
    ON derived.pums_burden_segmented (puma, year);


-- ----------------------------------------------------------------------------
-- public.v_pums_burden_overall
--   Convenience view: NJ-only, 'overall' segment_dim, by PUMA + year +
--   tenure. The "everyone in this PUMA" baseline that more granular
--   segments are compared against.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW public.v_pums_burden_overall AS
SELECT
    year, product, puma, tenure_class,
    weighted_n, sample_n,
    household_income_p50, monthly_cost_p50, burden_ratio_p50,
    suppressed
FROM derived.pums_burden_segmented
WHERE state_fips    = '34'
  AND segment_dim   = 'overall'
  AND segment_value = 'overall';

COMMENT ON VIEW public.v_pums_burden_overall IS
    'NJ baseline burden by (PUMA, year, tenure). The denominator for '
    'segmented comparisons (e.g. is renter_burden(race=hispanic) > '
    'renter_burden(overall)?).';
