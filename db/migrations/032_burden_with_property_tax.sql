-- ============================================================================
-- Migration: 032_burden_with_property_tax
--
-- Surface annual property tax data alongside the housing burden ratios.
--
-- *** CRITICAL ANALYTICAL NOTE -- read before changing this view. ***
--
-- ACS B25088 ("Median selected monthly owner costs WITH mortgage") and
-- ACS B25089 ("...WITHOUT mortgage") BOTH ALREADY INCLUDE:
--   * Mortgage principal + interest (B25088 only)
--   * Real estate taxes
--   * Fire / hazard / flood insurance
--   * Utilities (electricity, gas, water/sewer)
--   * Heating fuel
--   * Condominium fees and mobile home costs (where applicable)
-- See https://www.census.gov/acs/www/data/data-tables-and-tools/subject-tables/
-- (subject table S2506 documents the exact composition).
--
-- Therefore: adding NJ DCA property tax to renter_burden_ratio,
-- owner_burden_w_mtg_ratio, owner_burden_no_mtg_ratio, OR
-- blended_burden_ratio would DOUBLE-COUNT property taxes for owner
-- households. The existing ratios are already correct; we MUST NOT
-- modify their computation.
--
-- What this migration DOES change:
--   * Adds three NEW columns to derived.housing_burden_ratio that
--     surface NJ DCA property tax alongside the existing burden
--     ratios. They support counterfactual analysis ("what fraction
--     of owner cost is property tax in this county?") and are NULL
--     for non-NJ counties.
--   * The existing burden ratio columns (renter_*, owner_*, blended_*)
--     are unchanged in semantics; only the surrounding row gains
--     property-tax context.
--
-- The asset graph dep raw.nj_property_tax_county -> derived.housing_burden_ratio
-- is now genuine (not aspirational): the view's SQL references that
-- table, and a refresh of property tax produces a different fingerprint
-- on the derived asset.
-- ============================================================================

DROP VIEW IF EXISTS public.v_housing_burden_nj_5yr;
DROP VIEW IF EXISTS derived.housing_burden_ratio;

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

    -- ---------------------------------------------------------------------
    -- BURDEN RATIOS (UNCHANGED -- see migration header note).
    -- B25088/B25089 already include property tax; do NOT add it here.
    -- ---------------------------------------------------------------------
    CASE WHEN inc.household_income > 0 AND h.median_gross_rent IS NOT NULL
         THEN round((h.median_gross_rent * 12.0) / inc.household_income, 4)
    END AS renter_burden_ratio,

    CASE WHEN inc.household_income > 0 AND h.median_owner_cost_w_mortgage IS NOT NULL
         THEN round((h.median_owner_cost_w_mortgage * 12.0) / inc.household_income, 4)
    END AS owner_burden_w_mtg_ratio,

    CASE WHEN inc.household_income > 0 AND h.median_owner_cost_no_mortgage IS NOT NULL
         THEN round((h.median_owner_cost_no_mortgage * 12.0) / inc.household_income, 4)
    END AS owner_burden_no_mtg_ratio,

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
    END AS blended_burden_ratio,

    -- ---------------------------------------------------------------------
    -- NEW: NJ DCA property tax CONTEXT (informational only).
    -- LEFT JOIN -> NULL for non-NJ counties or years before
    -- nj_dca_property_tax.DCA_EARLIEST_YEAR (2016).
    -- ---------------------------------------------------------------------
    p.avg_total_property_taxes                 AS property_tax_amount_avg,
    p.cy_total_rate                            AS property_tax_effective_rate_pct,

    CASE WHEN inc.household_income > 0
          AND p.avg_total_property_taxes IS NOT NULL
         THEN round(p.avg_total_property_taxes / inc.household_income, 4)
    END                                        AS property_tax_share_of_income,

    -- The implied owner-cost-w-mtg portion that is property tax.
    -- Useful for counterfactual: "if this county had the state-median
    -- tax rate, what would owner cost be?"
    CASE WHEN h.median_owner_cost_w_mortgage > 0
          AND p.avg_total_property_taxes IS NOT NULL
         THEN round(
             (p.avg_total_property_taxes / 12.0)
                 / h.median_owner_cost_w_mortgage,
             4
         )
    END AS property_tax_share_of_owner_cost_w_mtg

FROM   derived.acs_housing_wide                h
JOIN   inc                                     USING (county_fips, year, product)
LEFT JOIN raw.nj_property_tax_county           p
       ON p.county_fips = h.county_fips
      AND p.year        = h.year;

COMMENT ON VIEW derived.housing_burden_ratio IS
    'Headline housing burden ratios per (county, year, product): renter, '
    'owner-with-mortgage, owner-without-mortgage, tenure-weighted blended. '
    'Burden ratios already include property tax via ACS B25088/B25089. '
    'NJ DCA property-tax columns are added as informational context for '
    '(NJ counties, year >= 2016); they are NULL elsewhere. Unitless. '
    '>= 0.30 is HUD-defined "cost burdened".';


-- Re-create the public-facing convenience view to surface the new columns.
CREATE VIEW public.v_housing_burden_nj_5yr AS
SELECT
    c.county_id,
    c.name                                  AS county_name,
    b.county_fips,
    b.year,
    b.household_income,
    b.median_gross_rent,
    b.median_owner_cost_w_mortgage,
    b.renter_burden_ratio,
    b.owner_burden_w_mtg_ratio,
    b.owner_burden_no_mtg_ratio,
    b.blended_burden_ratio,
    b.property_tax_amount_avg,
    b.property_tax_effective_rate_pct,
    b.property_tax_share_of_income,
    b.property_tax_share_of_owner_cost_w_mtg
FROM derived.housing_burden_ratio b
JOIN ref.county                   c ON c.county_fips = b.county_fips
WHERE b.product   = 'acs5'
  AND c.state_code = 'NJ';

COMMENT ON VIEW public.v_housing_burden_nj_5yr IS
    'NJ counties only, ACS 5-year, with human-readable names. The '
    'shorthand view for the headline burden-ratio panel. Includes '
    'property-tax context columns from raw.nj_property_tax_county.';
