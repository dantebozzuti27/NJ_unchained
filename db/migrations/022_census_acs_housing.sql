-- ============================================================================
-- Migration: 022_census_acs_housing
--
-- TIER 2: ACS housing-cost variables -- the numerator of the headline burden
-- ratio. Paired with ACS B19013 (the denominator) and CPI-U (the deflator).
--
-- WHY A MULTI-VARIABLE TABLE
-- --------------------------
-- B19013 is a one-variable table because median household income is a
-- single concept. Housing is irreducibly multi-variable: rent, owner cost,
-- home value, tenure mix all matter, and they participate together in the
-- burden-ratio derivations. Per-variable tables would balloon (one per
-- B25xxx_yyyE), each with the same provenance/suppression/year apparatus.
--
-- We therefore use ONE table keyed by `variable_id`, validated against a
-- canonical allowlist (`ref.acs_housing_variable`), and let derived views
-- project specific variables out into the shapes downstream metrics want.
--
-- This is the Tall Skinny vs Wide Fat tradeoff. We choose tall:
--   * adding a new ACS variable is INSERT data, not migrate schema
--   * the suppression CHECK constraint applies uniformly
--   * a single COPY can stage many variables for one (county, year)
--
-- The cost is that downstream queries need to PIVOT to get the wide shape.
-- We absorb that cost in the derived layer (one view per use case) and
-- expose convenient pre-pivoted views for the most common slices.
--
-- VARIABLE CATALOG
-- ----------------
-- See ref.acs_housing_variable below. We start narrow:
--   B25064_001  median gross rent
--   B25077_001  median home value
--   B25088_002  median monthly owner costs (with mortgage)
--   B25088_003  median monthly owner costs (without mortgage)
--   B25003_001  total occupied housing units
--   B25003_002  owner-occupied units
--   B25003_003  renter-occupied units
--
-- Adding a new variable is a one-row INSERT to ref.acs_housing_variable
-- (a follow-up migration), then a re-load.
-- ============================================================================

CREATE TABLE ref.acs_housing_variable (
    variable_id      TEXT          PRIMARY KEY
        CHECK (variable_id ~ '^B[0-9]{5}_[0-9]{3}$'),
    canonical_name   TEXT          NOT NULL UNIQUE,
    description      TEXT          NOT NULL,
    unit             TEXT          NOT NULL CHECK (unit IN (
        'usd_monthly', 'usd_annual', 'usd_total', 'count', 'percent', 'ratio'
    )),
    -- Whether this variable is a count (no MOE matters in dollars) or a
    -- dollar amount (deflate via CPI). Used by derived views.
    deflatable       BOOLEAN       NOT NULL
);

COMMENT ON TABLE ref.acs_housing_variable IS
    'Allowlist of ACS housing variables the platform tracks. Foreign-keyed '
    'from raw.acs_housing.';

INSERT INTO ref.acs_housing_variable
    (variable_id,  canonical_name,                  description, unit,         deflatable) VALUES
    ('B25064_001', 'median_gross_rent',             'Median gross rent (rent + utilities)',                  'usd_monthly', TRUE),
    ('B25077_001', 'median_home_value',             'Median value of owner-occupied housing units',          'usd_total',   TRUE),
    ('B25088_002', 'median_owner_cost_w_mortgage',  'Median monthly owner costs (with mortgage)',            'usd_monthly', TRUE),
    ('B25088_003', 'median_owner_cost_no_mortgage', 'Median monthly owner costs (without mortgage)',         'usd_monthly', TRUE),
    ('B25003_001', 'occupied_units_total',          'Total occupied housing units',                          'count',       FALSE),
    ('B25003_002', 'occupied_units_owner',          'Owner-occupied units',                                  'count',       FALSE),
    ('B25003_003', 'occupied_units_renter',         'Renter-occupied units',                                 'count',       FALSE);


CREATE TABLE raw.acs_housing (
    county_fips      CHAR(5)       NOT NULL CHECK (county_fips ~ '^[0-9]{5}$'),
    year             SMALLINT      NOT NULL CHECK (year BETWEEN 2005 AND 2099),
    product          TEXT          NOT NULL CHECK (product IN ('acs1', 'acs5')),

    -- Allowlisted variable; FK ensures we cannot ingest a variable we have
    -- not catalogued. Adding a new variable is a deliberate two-step
    -- (catalog INSERT, then re-load).
    variable_id      TEXT          NOT NULL REFERENCES ref.acs_housing_variable(variable_id),

    estimate         NUMERIC(14,2) CHECK (estimate IS NULL OR estimate >= 0),
    margin_of_error  NUMERIC(14,2) CHECK (margin_of_error IS NULL OR margin_of_error >= 0),
    dollar_year      SMALLINT      NOT NULL CHECK (dollar_year BETWEEN 2005 AND 2099),

    suppression_code TEXT          CHECK (
        suppression_code IS NULL
        OR suppression_code IN ('confidentiality', 'too_small', 'other')
    ),

    source_url       TEXT          NOT NULL,
    source_sha256    CHAR(64)      NOT NULL,
    ingested_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),

    -- Same null-OR-suppression invariant as B19013.
    CHECK (
        (estimate IS NOT NULL AND suppression_code IS NULL)
        OR
        (estimate IS NULL     AND suppression_code IS NOT NULL)
    ),

    PRIMARY KEY (county_fips, year, product, variable_id)
);

COMMENT ON TABLE raw.acs_housing IS
    'ACS housing variables (B25064, B25077, B25088, B25003), one row per '
    '(county, year, product, variable). Tall-skinny shape; pivot in derived.';

CREATE INDEX raw_acs_housing_year_product_idx
    ON raw.acs_housing (year, product);
CREATE INDEX raw_acs_housing_variable_idx
    ON raw.acs_housing (variable_id);


-- Wide-pivoted view of the canonical housing variables, one row per
-- (county, year, product). NULLs propagate from suppressed/missing rows.
-- This is the shape downstream burden-ratio derivations want.
CREATE VIEW derived.acs_housing_wide AS
SELECT
    county_fips,
    year,
    product,
    dollar_year,
    max(estimate) FILTER (WHERE variable_id = 'B25064_001') AS median_gross_rent,
    max(estimate) FILTER (WHERE variable_id = 'B25077_001') AS median_home_value,
    max(estimate) FILTER (WHERE variable_id = 'B25088_002') AS median_owner_cost_w_mortgage,
    max(estimate) FILTER (WHERE variable_id = 'B25088_003') AS median_owner_cost_no_mortgage,
    max(estimate) FILTER (WHERE variable_id = 'B25003_001') AS occupied_units_total,
    max(estimate) FILTER (WHERE variable_id = 'B25003_002') AS occupied_units_owner,
    max(estimate) FILTER (WHERE variable_id = 'B25003_003') AS occupied_units_renter
FROM raw.acs_housing
GROUP BY county_fips, year, product, dollar_year;

COMMENT ON VIEW derived.acs_housing_wide IS
    'Pivoted housing variables, one row per (county, year, product). NULL '
    'where the source row is suppressed.';


-- Headline housing burden ratio. Joins ACS housing wide-view to
-- ACS B19013 (median household income), computes per-tenure annual cost
-- divided by annual income, and a tenure-weighted blended ratio.
--
-- Definitions (industry standard, matches HUD's ACS-derived burden):
--   renter_burden       = annual_gross_rent / household_income
--   owner_burden_w_mtg  = annual_owner_cost_w_mortgage / household_income
--   owner_burden_no_mtg = annual_owner_cost_no_mortgage / household_income
--
-- A burden of 0.30+ is "cost burdened" and 0.50+ is "severely cost
-- burdened" in HUD's terminology.
--
-- The view returns NOMINAL ratios (no deflation). Ratios are unitless;
-- deflation cancels out. Use this view directly for cross-county and
-- cross-year burden-ratio comparisons.
CREATE VIEW derived.housing_burden_ratio AS
WITH inc AS (
    SELECT county_fips, year, product, estimate AS household_income
    FROM raw.acs_median_household_income
    WHERE estimate IS NOT NULL
)
SELECT
    h.county_fips,
    h.year,
    h.product,
    inc.household_income,
    h.median_gross_rent,
    h.median_owner_cost_w_mortgage,
    h.median_owner_cost_no_mortgage,
    h.occupied_units_owner,
    h.occupied_units_renter,
    h.occupied_units_total,

    CASE WHEN inc.household_income > 0 AND h.median_gross_rent IS NOT NULL
         THEN round((h.median_gross_rent * 12.0) / inc.household_income, 4)
    END AS renter_burden_ratio,

    CASE WHEN inc.household_income > 0 AND h.median_owner_cost_w_mortgage IS NOT NULL
         THEN round((h.median_owner_cost_w_mortgage * 12.0) / inc.household_income, 4)
    END AS owner_burden_w_mtg_ratio,

    CASE WHEN inc.household_income > 0 AND h.median_owner_cost_no_mortgage IS NOT NULL
         THEN round((h.median_owner_cost_no_mortgage * 12.0) / inc.household_income, 4)
    END AS owner_burden_no_mtg_ratio,

    -- Tenure-weighted blended burden. Owner cost components are blended
    -- 50/50 between with-mortgage and without-mortgage when both present
    -- (TODO: replace with B25081 mortgage-status counts once that
    -- variable is catalogued). This is a defensible-but-coarse blend.
    CASE
        WHEN inc.household_income > 0
         AND h.occupied_units_total IS NOT NULL
         AND h.occupied_units_total > 0
        THEN round(
            (
                coalesce(h.median_gross_rent * 12.0, 0)
                    * coalesce(h.occupied_units_renter, 0)
                + coalesce(
                    (
                        coalesce(h.median_owner_cost_w_mortgage * 12.0, 0)
                      + coalesce(h.median_owner_cost_no_mortgage * 12.0, 0)
                    ) / NULLIF(
                        (CASE WHEN h.median_owner_cost_w_mortgage  IS NOT NULL THEN 1 ELSE 0 END
                       + CASE WHEN h.median_owner_cost_no_mortgage IS NOT NULL THEN 1 ELSE 0 END), 0
                    ), 0
                )   * coalesce(h.occupied_units_owner, 0)
            )
            / NULLIF(h.occupied_units_total, 0)
            / inc.household_income,
            4
        )
    END AS blended_burden_ratio

FROM derived.acs_housing_wide h
JOIN inc USING (county_fips, year, product);

COMMENT ON VIEW derived.housing_burden_ratio IS
    'Headline housing burden ratios per (county, year, product): renter, '
    'owner-with-mortgage, owner-without-mortgage, tenure-weighted blended. '
    'Unitless. >= 0.30 is HUD-defined "cost burdened".';


-- NJ-only, ACS5, with human-readable county names. Convenience view for
-- the dashboard UI -- defaults to the slice 99% of NJ analyses use.
CREATE VIEW public.v_housing_burden_nj_5yr AS
SELECT
    c.county_id,
    c.name              AS county_name,
    b.year,
    b.household_income,
    b.median_gross_rent,
    b.median_owner_cost_w_mortgage,
    b.renter_burden_ratio,
    b.owner_burden_w_mtg_ratio,
    b.owner_burden_no_mtg_ratio,
    b.blended_burden_ratio
FROM derived.housing_burden_ratio b
JOIN ref.county                   c ON c.county_fips = b.county_fips
WHERE b.product   = 'acs5'
  AND c.state_code = 'NJ';

COMMENT ON VIEW public.v_housing_burden_nj_5yr IS
    'NJ counties only, ACS 5-year, with human-readable names. The '
    'shorthand view for the headline burden-ratio panel.';
