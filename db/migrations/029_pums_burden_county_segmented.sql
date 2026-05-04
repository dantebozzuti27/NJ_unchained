-- ============================================================================
-- Migration: 029_pums_burden_county_segmented
--
-- TIER 3 derived table: county-level housing burden ratios from ACS
-- PUMS, segmented by tenure x demographic dimension. Mirror of
-- derived.pums_burden_segmented (027) but keyed on county_fips
-- instead of puma.
--
-- WHY THIS EXISTS (and why not just roll up PUMA medians)
-- -------------------------------------------------------
-- PUMS reports geography at the PUMA grain. Most consumer-facing
-- analytics (and our existing aggregate burden table,
-- derived.housing_burden_ratio) work at COUNTY grain. So we need a
-- county-grain segmented burden table.
--
-- The naive approach is to take the PUMA-level cells from
-- derived.pums_burden_segmented and aggregate them up via the
-- crosswalk. That fails methodologically: median-of-medians is not
-- a valid statistical operation. Two PUMAs with median income $50K
-- and $80K do NOT have a combined median of $65K -- you cannot
-- recover the true county median from PUMA medians without the raw
-- distribution.
--
-- The correct approach (taken here): re-aggregate from raw PUMS,
-- allocating each person's PWGTP across counties via the crosswalk.
-- For a PUMA wholly within county X (allocation_factor = 1.0), this
-- is identical to "use PUMS observations from this PUMA". For a
-- multi-county PUMA, each person contributes a fractional weight
-- to each county. The weighted percentile is then computed across
-- all observations now allocated to the county, which IS a valid
-- statistical estimator.
--
-- This costs about 2x compute vs. roll-up (we re-do the percentile
-- math) but produces statistically defensible numbers.
--
-- SCHEMA SHAPE
-- ------------
-- Same long-format segmentation as derived.pums_burden_segmented
-- (long-format on (segment_dim, segment_value)). The only difference
-- is `puma` -> `county_fips` and one new column: `n_pumas_contributing`
-- which records how many distinct PUMAs allocate population to this
-- county-cell. Useful for debugging and for understanding the
-- allocation precision.
--
-- SUPPRESSION
-- -----------
-- Same threshold as PUMA-level (weighted_n < 1000). With county-
-- level aggregation, more cells will exceed the threshold (counties
-- have larger populations than individual PUMAs), so the county
-- table will have a LOWER suppression rate than the PUMA table.
-- This is a feature, not a bug -- it lets us answer demographic
-- questions for smaller counties that were too sparse at PUMA grain.
-- ============================================================================


CREATE TABLE derived.pums_burden_county_segmented (
    year             SMALLINT  NOT NULL CHECK (year BETWEEN 2017 AND 2099),
    product          TEXT      NOT NULL CHECK (product IN ('acs1', 'acs5')),
    state_fips       CHAR(2)   NOT NULL CHECK (state_fips ~ '^[0-9]{2}$'),
    county_fips      CHAR(5)   NOT NULL CHECK (county_fips ~ '^[0-9]{5}$'),

    tenure_class     TEXT      NOT NULL
        CHECK (tenure_class IN ('renter', 'owner_w_mtg', 'owner_no_mtg')),

    segment_dim      TEXT      NOT NULL
        CHECK (segment_dim IN (
            'overall', 'race', 'hispanic', 'citizenship', 'age_band'
        )),
    segment_value    TEXT      NOT NULL,

    -- Weighted population in this county-cell. Sum of (pwgtp *
    -- allocation_factor) across all PUMS person rows whose PUMAs
    -- include this county.
    weighted_n       INTEGER   NOT NULL CHECK (weighted_n >= 0),

    -- Sample size: count of distinct (serialno, sporder) records
    -- whose PUMA touches this county. Note: a person in a multi-
    -- county PUMA contributes 1 to the sample_n of EACH county the
    -- PUMA spans (we count physical observations, not allocated
    -- weights). This is a slight overcount for multi-county PUMAs
    -- but matches how SE estimation downstream will interpret it.
    sample_n         INTEGER   NOT NULL CHECK (sample_n >= 0),

    household_income_p50  NUMERIC(12, 2)
        CHECK (household_income_p50 IS NULL OR household_income_p50 >= 0),
    monthly_cost_p50      NUMERIC(10, 2)
        CHECK (monthly_cost_p50 IS NULL OR monthly_cost_p50 >= 0),
    burden_ratio_p50      NUMERIC(8, 4)
        CHECK (burden_ratio_p50 IS NULL OR burden_ratio_p50 >= 0),

    suppressed       BOOLEAN   NOT NULL DEFAULT FALSE,

    -- Provenance + transparency.
    n_pumas_contributing  SMALLINT  NOT NULL CHECK (n_pumas_contributing >= 1),
    formula_version       TEXT      NOT NULL,
    input_vintage_hash    CHAR(64)  NOT NULL,
    computed_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (year, product, county_fips, tenure_class, segment_dim, segment_value)
);

COMMENT ON TABLE derived.pums_burden_county_segmented IS
    'County-grain housing burden from ACS PUMS, segmented by tenure x '
    'demographic dimension. Aggregated from raw PUMS via population-'
    'weighted PUMA-county allocation (ref.puma2020_county_xwalk). NOT '
    'computed by rolling up derived.pums_burden_segmented (median-of-'
    'medians is invalid). Burden ratio = median_cost*12 / median_income, '
    'computed across all PUMS observations allocated to the county. '
    'Cells with weighted_n < 1000 suppressed.';

CREATE INDEX pums_burden_county_seg_dim_value_idx
    ON derived.pums_burden_county_segmented (year, segment_dim, segment_value);

CREATE INDEX pums_burden_county_seg_county_year_idx
    ON derived.pums_burden_county_segmented (county_fips, year);


-- ----------------------------------------------------------------------------
-- public.v_pums_burden_county_overall
--   NJ-only, 'overall' baseline by (county, year, tenure). The
--   PUMS counterpart to derived.housing_burden_ratio (which is ACS
--   tabular). Useful for sanity-checking: PUMS-derived county burden
--   should track the ACS tabular burden within ~5% (modulo year +
--   methodology differences).
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW public.v_pums_burden_county_overall AS
SELECT
    year, product, county_fips, tenure_class,
    weighted_n, sample_n,
    household_income_p50, monthly_cost_p50, burden_ratio_p50,
    suppressed, n_pumas_contributing
FROM derived.pums_burden_county_segmented
WHERE state_fips    = '34'
  AND segment_dim   = 'overall'
  AND segment_value = 'overall';

COMMENT ON VIEW public.v_pums_burden_county_overall IS
    'NJ baseline county burden from PUMS (overall segment). The counterpart '
    'to derived.housing_burden_ratio (ACS tabular). Should agree within '
    '~5% modulo year and methodology differences.';
