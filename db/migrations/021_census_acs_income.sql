-- ============================================================================
-- Migration: 021_census_acs_income
--
-- TIER 2: American Community Survey (ACS) median household income (table
-- B19013). The denominator of the headline burden ratio.
--
-- WHY THIS EXISTS
-- ---------------
-- Median household income at county level is the methodological floor for
-- "share of income spent on housing" metrics. Mean income overstates the
-- typical household's burden (top-coded high earners pull the mean up);
-- median is robust to that and is what HUD and Census use for their own
-- affordability indices.
--
-- ACS publishes B19013 annually in two products:
--
--   * ACS 1-year   -- only for geographies with population >= 65,000.
--                     For NJ counties, all 21 qualify (smallest is Salem
--                     at ~64K, just under threshold; in some years Salem
--                     is suppressed, others published. We accept the
--                     vintage-specific availability and tag missing rows
--                     in governance.dataset_health).
--   * ACS 5-year   -- 5-year overlapping average, available for all
--                     geographies, lower margin of error, but smooths
--                     across 5 years (e.g. ACS 5y for 2022 covers
--                     2018-2022). For burden-ratio time series the
--                     smoothing is a feature, not a bug.
--
-- We store BOTH products and let downstream queries pick. Default for
-- the headline burden ratio is ACS 5-year (lower MOE, all counties).
--
-- DATA QUALITY
-- ------------
-- Census assigns each estimate a margin of error (MOE) at the 90% level.
-- We store MOE as-published. Downstream metrics that compute a ratio of
-- two ACS estimates must propagate MOE via the standard ACS combined-MOE
-- formula (sqrt(sum of squared MOEs)) -- this is enforced in derived.*,
-- not here.
--
-- DEFLATION
-- ---------
-- ACS releases all dollar amounts in current-year dollars (the survey
-- year, not the release year). ACS 5-year amounts are inflation-adjusted
-- by Census to the END year of the 5-year window using ACS-specific
-- deflators. We DO NOT re-deflate at ingest time -- we preserve the
-- as-published value plus a `dollar_year` column so the consumer can
-- reason about it. The CPI-U table (migration 020) provides the stable
-- deflation lookup when needed.
--
-- NATURAL KEY
-- -----------
-- (county_fips, year, product). product is one of 'acs1', 'acs5'.
-- ============================================================================

CREATE TABLE raw.acs_median_household_income (
    county_fips      CHAR(5)       NOT NULL CHECK (county_fips ~ '^[0-9]{5}$'),
    year             SMALLINT      NOT NULL CHECK (year BETWEEN 2005 AND 2099),
    product          TEXT          NOT NULL CHECK (product IN ('acs1', 'acs5')),

    estimate         NUMERIC(12,2) CHECK (estimate IS NULL OR estimate > 0),
    margin_of_error  NUMERIC(12,2) CHECK (margin_of_error IS NULL OR margin_of_error >= 0),

    -- ACS 5y dollar_year is the END year of the window (e.g. 2018-2022 -> 2022).
    -- ACS 1y dollar_year equals the survey year.
    dollar_year      SMALLINT      NOT NULL CHECK (dollar_year BETWEEN 2005 AND 2099),

    -- Census's null sentinels: -666666666 (suppressed for confidentiality),
    -- -222222222 (annotation: estimate too small to display). When we see
    -- those, we set estimate/MOE to NULL and stash the original code here.
    suppression_code TEXT          CHECK (
        suppression_code IS NULL
        OR suppression_code IN ('confidentiality', 'too_small', 'other')
    ),

    source_url       TEXT          NOT NULL,
    source_sha256    CHAR(64)      NOT NULL,
    ingested_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),

    -- Either we have an estimate OR a suppression code, never both NULL.
    -- (Both populated is also disallowed: a row with a suppression_code
    --  must have estimate IS NULL.)
    CHECK (
        (estimate IS NOT NULL AND suppression_code IS NULL)
        OR
        (estimate IS NULL     AND suppression_code IS NOT NULL)
    ),

    PRIMARY KEY (county_fips, year, product)
);

COMMENT ON TABLE raw.acs_median_household_income IS
    'ACS B19013 median household income, by county_fips x year x product. '
    'Estimate in current-year dollars (or end-year dollars for ACS 5y).';

CREATE INDEX raw_acs_mhi_year_product_idx
    ON raw.acs_median_household_income (year, product);


-- Real-dollar view: median household income deflated to a base year via
-- CPI-U All Items annual average. Computes constant-dollar values on the
-- fly; we store nominal values only and let users specify their base year
-- via a stored function (kept simple: a parameterized view via SQL function).
CREATE OR REPLACE FUNCTION derived.f_acs_mhi_real(base_year SMALLINT)
RETURNS TABLE (
    county_fips      CHAR(5),
    year             SMALLINT,
    product          TEXT,
    estimate_real    NUMERIC,
    estimate_nominal NUMERIC,
    deflator         NUMERIC,
    base_year_used   SMALLINT
)
LANGUAGE sql STABLE
AS $$
    WITH base AS (
        SELECT cpi_u_all_items AS base_cpi
        FROM   derived.cpi_u_headline_annual
        WHERE  year = base_year
    )
    SELECT
        m.county_fips,
        m.year,
        m.product,
        round(m.estimate * (b.base_cpi / cy.cpi_u_all_items), 2) AS estimate_real,
        m.estimate                                                AS estimate_nominal,
        round(b.base_cpi / cy.cpi_u_all_items, 6)                 AS deflator,
        base_year                                                 AS base_year_used
    FROM raw.acs_median_household_income m
    JOIN derived.cpi_u_headline_annual    cy ON cy.year = m.dollar_year
    CROSS JOIN base b
    WHERE m.estimate IS NOT NULL;
$$;

COMMENT ON FUNCTION derived.f_acs_mhi_real(SMALLINT) IS
    'Median household income deflated to base_year dollars via CPI-U '
    'All Items. Returns NULL/empty rows for years where base_year CPI is '
    'not loaded; suppressed estimates are excluded.';


-- NJ-only convenience view, joined to ref.county for human-readable names.
-- Defaults to ACS 5-year (lower MOE).
CREATE VIEW public.v_acs_mhi_nj_5yr AS
SELECT
    c.county_id,
    c.name              AS county_name,
    m.year,
    m.estimate          AS median_household_income,
    m.margin_of_error,
    m.dollar_year,
    m.suppression_code
FROM raw.acs_median_household_income m
JOIN ref.county                       c ON c.county_fips = m.county_fips
WHERE m.product = 'acs5'
  AND c.state_code = 'NJ';

COMMENT ON VIEW public.v_acs_mhi_nj_5yr IS
    'NJ counties only, ACS 5-year median household income. Shorthand view '
    'for the 99% of dashboards that use this exact slice.';
