-- ============================================================================
-- Migration: 037_pums_burden_trend_view
--
-- TIER 3 read surface: time-series view of county-grain burden ratio
-- with year-over-year delta and a naive-independence SE for the delta.
--
-- WHY
-- ---
-- The platform's mental model is "Bloomberg Terminal for civic data";
-- a terminal without time series is a snapshot, not a terminal. With
-- multi-year PUMS now in raw (2018-2022, 9 (year, product) pairs),
-- the natural read surface is "for each county-cell, what was its
-- burden ratio this year vs last year, and is the change statistically
-- distinguishable from sampling noise".
--
-- METHODOLOGY
-- -----------
-- For independent samples (different ACS 1-Year vintages, e.g., 2021
-- 1-Year vs 2022 1-Year), the variance of the difference is the sum
-- of variances:
--
--     Var(burden_2022 - burden_2021) = Var(burden_2022) + Var(burden_2021)
--     SE(delta) = sqrt(SE_2022^2 + SE_2021^2)
--
-- For OVERLAPPING 5-Year samples (e.g., 2021 5-Year [2017-2021] vs
-- 2022 5-Year [2018-2022], which share 4 of 5 years' sample), the
-- two estimates are correlated and the variance of the difference is:
--
--     Var(diff) = V_1 + V_2 - 2*Cov(theta_1, theta_2)
--
-- Census's documented method estimates Cov via cross-period replicate
-- weighting; we do not implement that here yet (deferred to a follow-up
-- migration that materializes a properly variance-corrected
-- ``derived.pums_burden_yoy`` table). This view labels the SE field
-- ``burden_ratio_delta_se_naive`` to make the methodological caveat
-- visible. For consumers comparing 1-Year-to-1-Year (independent),
-- the naive SE is exact.
--
-- WHAT IT DOES
-- ------------
-- For each (county, tenure, product) the view returns one row per
-- (current_year, prior_year) pair, where prior_year is the most
-- recent year strictly less than current_year for the same product.
-- This handles the COVID gap in 1-Year (no 2020 data) cleanly:
-- 2021's prior_year is 2019 for acs1, 2020 for acs5.
--
-- USAGE
-- -----
--   SELECT * FROM public.v_pums_burden_county_yoy_overall
--   WHERE state_fips='34' AND county_fips='34003'
--     AND product='acs1' ORDER BY year;
-- ============================================================================


DROP VIEW IF EXISTS public.v_pums_burden_county_yoy_overall;
CREATE VIEW public.v_pums_burden_county_yoy_overall AS
WITH overall AS (
    SELECT
        year, product, state_fips, county_fips, tenure_class,
        burden_ratio_p50    AS burden_ratio,
        burden_ratio_p50_se AS burden_ratio_se,
        weighted_n, sample_n, suppressed
    FROM derived.pums_burden_county_segmented
    WHERE segment_dim = 'overall' AND segment_value = 'overall'
),
ranked AS (
    SELECT
        o.*,
        LAG(year)             OVER w AS prior_year,
        LAG(burden_ratio)     OVER w AS prior_burden_ratio,
        LAG(burden_ratio_se)  OVER w AS prior_burden_ratio_se
    FROM overall o
    WINDOW w AS (
        PARTITION BY product, state_fips, county_fips, tenure_class
        ORDER BY year
    )
)
SELECT
    year, product, state_fips, county_fips, tenure_class,
    burden_ratio,    burden_ratio_se,
    prior_year,
    prior_burden_ratio, prior_burden_ratio_se,
    (burden_ratio - prior_burden_ratio)::numeric(12, 6)
        AS burden_ratio_delta,
    -- Independence assumption is exact for ACS 1-Year-to-1-Year and
    -- approximate (overstates uncertainty) for 5-Year-to-5-Year.
    sqrt(
        coalesce(burden_ratio_se, 0)^2
      + coalesce(prior_burden_ratio_se, 0)^2
    )::numeric(12, 6) AS burden_ratio_delta_se_naive,
    weighted_n,
    sample_n,
    suppressed
FROM ranked
WHERE prior_year IS NOT NULL;

COMMENT ON VIEW public.v_pums_burden_county_yoy_overall IS
    'Time-series view: county-tenure-overall burden ratio with prior-'
    'year delta and naive-independence SE. Independence assumption is '
    'exact for ACS 1-Year-to-1-Year comparisons; for 5-Year-to-5-Year '
    'it overstates uncertainty (the two periods share 4 years of '
    'sample). A future migration will replace this with a properly '
    'variance-corrected derived.pums_burden_yoy materialized table.';
