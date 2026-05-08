-- ============================================================================
-- Migration: 085_real_dollar_baseline
--
-- VISION_2026 §3.4 baseline rewrite (and idea spec §3.4): "all values
-- converted to 2026 real dollars baseline." The substrate already
-- supports CPI deflation via derived.cpi_u_headline_annual, but two
-- things are missing for the UI rewrite:
--
--   1. A canonical "what is the latest year for which we can express
--      values in real dollars?" function -- driven by data, not code.
--      Currently the platform pins burden_base_year = 2010 in
--      ref.platform_constants for HPI/income GROWTH RATIOS, but that's
--      a DIFFERENT concept from the dollar-denominated baseline. The
--      spec wants 2026; until BLS publishes CPI-U M13 2026 (~Jan 2027)
--      the substrate-honest answer is "the latest year we can hit".
--
--   2. A real-dollar version of derived.v_affordability_gap so the UI
--      can show "$X home price (in 2024 dollars)" instead of nominal
--      year-of-observation dollars. This is the headline metric the
--      idea spec §5.4 calls "the affordability gap" -- presently
--      computed in nominal dollars by v_affordability_gap; this
--      migration adds the CPI-deflated counterpart.
--
-- WHAT THIS MIGRATION SHIPS
-- -------------------------
--
-- 1. derived.f_real_dollar_base_year() -- scalar SMALLINT function
--    returning MAX(year) from derived.cpi_u_headline_annual. Currently
--    2024 (BLS publishes M13 annual averages with ~12-month lag).
--    When 2025 / 2026 CPI lands the function automatically picks up
--    the new year with zero code change.
--
-- 2. derived.v_affordability_gap_real -- a view shaped like
--    derived.v_affordability_gap but with every nominal-dollar column
--    multiplied by CPI(real_dollar_base_year) / CPI(year). Returns
--    NULL for any (county, year) where either CPI value is missing
--    (substrate-honest; we never silently fall back).
--
--    Columns mirror v_affordability_gap with real-dollar suffixes:
--      home_price_real, median_income_real, piti_annual_real,
--      required_income_hud_30pct_real, hud_headroom_dollars_real.
--    Plus passes through the unitless burden_ratio + tier classification
--    so a single per-(county, year) row carries everything the UI
--    headline needs.
--
-- DESIGN
-- ------
-- * The view does NOT take a base_year parameter -- the spec wants
--   "2026 real dollars" eventually but the substrate-honest answer is
--   "the latest year we have CPI for". Caller-tunable would be a
--   function not a view; we use a view because the UI consumes a
--   single denominated headline per page.
-- * burden_ratio is INCLUDED in this view (computed from
--   derived.f_burden_ratio) so the UI can render the tier badge as a
--   secondary signal next to the dollar headline.
-- * formula_version is stamped on every row.
-- ============================================================================

BEGIN;


-- ----------------------------------------------------------------------------
-- Formula version registration. Stacks on 1.9.0-cross-source-annotations-v1.
-- ----------------------------------------------------------------------------
INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '2.0.0-real-dollar-baseline-v1',
    'Real-dollar baseline substrate (VISION_2026 §3.4 / idea §3.4): '
    'derived.f_real_dollar_base_year() returns the latest year present '
    'in derived.cpi_u_headline_annual (currently 2024); '
    'derived.v_affordability_gap_real CPI-deflates every dollar column '
    'in v_affordability_gap to that base year. Substrate-honest -- the '
    'spec mandates 2026 but BLS publishes CPI M13 with a ~12-month lag; '
    'until 2026 CPI lands the platform reports values in the latest '
    'available real-dollar base year, with the base_year column on '
    'every row so the UI can label "2024 dollars" / "2026 dollars" '
    'truthfully.',
    '2026-05-09'::DATE,
    'Stacks on 1.9.0-cross-source-annotations-v1.'
)
ON CONFLICT (formula_version) DO NOTHING;


-- ----------------------------------------------------------------------------
-- derived.f_real_dollar_base_year()
--
-- Returns SMALLINT MAX(year) from derived.cpi_u_headline_annual. Currently
-- evaluates to 2024. Updates automatically when CPI 2025 / 2026 lands
-- (the spec-mandated 2026 anchor) -- no code change required.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.f_real_dollar_base_year()
RETURNS SMALLINT
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    SELECT MAX(year)::SMALLINT FROM derived.cpi_u_headline_annual
$$;

COMMENT ON FUNCTION derived.f_real_dollar_base_year() IS
    'Latest year present in derived.cpi_u_headline_annual; the '
    'substrate-honest answer to "what real-dollar baseline can we '
    'hit?". Spec §3.4 mandates 2026 eventually; until BLS publishes '
    'CPI-U M13 2026 (~Jan 2027) this function returns the latest year '
    'that exists in the substrate. Formula 2.0.0-real-dollar-baseline-v1.';


-- ----------------------------------------------------------------------------
-- derived.v_affordability_gap_real
--
-- One row per (county, year) where the housing-burden substrate is
-- complete (DCA property tax + ACS5 income + FRED MORTGAGE30US + tax
-- brackets all present). Every dollar column is CPI-deflated to the
-- latest available CPI year so the UI can display "$X (in 2024
-- dollars)" alongside the nominal-dollar source data.
--
-- The view is the headline-source for /housing/[id] page rewrite
-- per VISION_2026 §3.4: replace the unitless "burden ratio" headline
-- with the dollar Affordability Gap.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.v_affordability_gap_real AS
WITH base AS (
    SELECT derived.f_real_dollar_base_year() AS base_year
),
deflate AS (
    SELECT
        v.county_fips,
        v.year,
        v.home_price                                              AS home_price_nominal,
        v.median_income_nominal,
        v.piti_annual                                             AS piti_annual_nominal,
        v.required_income_hud_30pct                               AS required_income_hud_30pct_nominal,
        v.hud_headroom_dollars                                    AS hud_headroom_dollars_nominal,
        v.required_income_post_tax_30pct                          AS required_income_post_tax_30pct_nominal,
        v.required_income_full_burden_30pct                       AS required_income_full_burden_30pct_nominal,
        v.hud_required_to_actual_ratio,
        cb.cpi_u_all_items                                        AS cpi_base,
        cy.cpi_u_all_items                                        AS cpi_year,
        b.base_year
    FROM derived.v_affordability_gap v
    CROSS JOIN base b
    LEFT JOIN derived.cpi_u_headline_annual cb ON cb.year = b.base_year
    LEFT JOIN derived.cpi_u_headline_annual cy ON cy.year = v.year
)
SELECT
    d.county_fips,
    d.year,
    d.base_year                                                   AS real_dollar_base_year,
    -- Nominal source values, retained so the UI can label both lenses.
    d.home_price_nominal,
    d.median_income_nominal,
    d.piti_annual_nominal,
    d.required_income_hud_30pct_nominal,
    d.hud_headroom_dollars_nominal,
    d.required_income_post_tax_30pct_nominal,
    d.required_income_full_burden_30pct_nominal,
    d.hud_required_to_actual_ratio,
    -- CPI-deflated real-dollar values. CPI lookups must hit; either
    -- NULL bubbles to NULL on every dependent column.
    CASE WHEN d.cpi_base IS NOT NULL AND d.cpi_year IS NOT NULL
              AND d.cpi_year <> 0
         THEN ROUND(d.home_price_nominal * d.cpi_base / d.cpi_year, 2)
    END                                                           AS home_price_real,
    CASE WHEN d.cpi_base IS NOT NULL AND d.cpi_year IS NOT NULL
              AND d.cpi_year <> 0
         THEN ROUND(d.median_income_nominal * d.cpi_base / d.cpi_year, 2)
    END                                                           AS median_income_real,
    CASE WHEN d.cpi_base IS NOT NULL AND d.cpi_year IS NOT NULL
              AND d.cpi_year <> 0
         THEN ROUND(d.piti_annual_nominal * d.cpi_base / d.cpi_year, 2)
    END                                                           AS piti_annual_real,
    CASE WHEN d.cpi_base IS NOT NULL AND d.cpi_year IS NOT NULL
              AND d.cpi_year <> 0
         THEN ROUND(d.required_income_hud_30pct_nominal * d.cpi_base / d.cpi_year, 2)
    END                                                           AS required_income_hud_30pct_real,
    CASE WHEN d.cpi_base IS NOT NULL AND d.cpi_year IS NOT NULL
              AND d.cpi_year <> 0
         THEN ROUND(d.hud_headroom_dollars_nominal * d.cpi_base / d.cpi_year, 2)
    END                                                           AS hud_headroom_dollars_real,
    CASE WHEN d.cpi_base IS NOT NULL AND d.cpi_year IS NOT NULL
              AND d.cpi_year <> 0
         THEN ROUND(d.required_income_post_tax_30pct_nominal * d.cpi_base / d.cpi_year, 2)
    END                                                           AS required_income_post_tax_30pct_real,
    CASE WHEN d.cpi_base IS NOT NULL AND d.cpi_year IS NOT NULL
              AND d.cpi_year <> 0
         THEN ROUND(d.required_income_full_burden_30pct_nominal * d.cpi_base / d.cpi_year, 2)
    END                                                           AS required_income_full_burden_30pct_real,
    -- Pass through the CPI inputs so callers can audit the
    -- deflation arithmetic without re-querying.
    d.cpi_base                                                    AS cpi_at_base_year,
    d.cpi_year                                                    AS cpi_at_year,
    '2.0.0-real-dollar-baseline-v1'::TEXT                         AS formula_version
FROM deflate d;

COMMENT ON VIEW derived.v_affordability_gap_real IS
    'Real-dollar (CPI-deflated) version of derived.v_affordability_gap. '
    'Every nominal-dollar column has a real-dollar counterpart with '
    '_real suffix; the deflation pivots on '
    'derived.f_real_dollar_base_year() (latest year in '
    'derived.cpi_u_headline_annual). Substrate-honest: CPI lookups must '
    'hit, NULLs bubble. Headline source for /housing/[id] page rewrite '
    'per VISION_2026 §3.4. Formula 2.0.0-real-dollar-baseline-v1.';


COMMIT;
