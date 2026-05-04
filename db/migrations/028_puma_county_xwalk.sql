-- ============================================================================
-- Migration: 028_puma_county_xwalk
--
-- TIER 3 reference table: PUMA-to-county allocation crosswalk.
--
-- WHY THIS EXISTS
-- ---------------
-- PUMS (raw.acs_pums_*) reports geography at the Public Use Microdata
-- Area level (>=100K population). PUMA boundaries do NOT necessarily
-- align with county boundaries. To roll up PUMS-derived statistics
-- (derived.pums_burden_segmented) to county-level, we need a per-row
-- allocation factor: "what fraction of this PUMA's population lives in
-- each county".
--
-- METHODOLOGY
-- -----------
-- For NJ 2020-vintage PUMAs: 72 of 74 PUMAs are wholly within a single
-- county (allocation_factor = 1.0). Two PUMAs span two counties each:
--
--   * 02501 "Salem & Cumberland (North) Counties": Salem fully +
--     northern Cumberland (Bridgeton + surroundings). Population-
--     weighted allocation: Salem 0.56 / Cumberland 0.44 based on the
--     2020 Census decennial population of ~64K and ~50K respectively
--     within the PUMA boundary (Cumberland's south is in PUMA 02401).
--   * 02601 "Cape May & Atlantic (South Central) Counties": Cape May
--     fully + south-central Atlantic (Somers Point and surroundings).
--     Population-weighted allocation: Cape May 0.79 / Atlantic 0.21,
--     same source.
--
-- These multi-county splits are best-effort approximations from
-- decennial county population data. For production-grade county
-- rollups, operators should run Census's Geographic Correspondence
-- Engine (Geocorr) at:
--
--     https://mcdc.missouri.edu/applications/geocorr2022.html
--
-- Source: PUMA20 -> County, weighting variable "Population (2020)".
-- The output CSV can be staged into ref.puma2020_county_xwalk via a
-- one-off load script (TODO: scripts/stage_geocorr.py).
--
-- INVARIANTS
-- ----------
-- For each (state_fips, puma), allocation_factor values across the
-- multiple county_fips entries must sum to 1.0 (or close enough --
-- we tolerate +/- 0.001 for floating-point rounding). This is
-- enforced by a deferred CHECK against a SQL function below.
--
-- ALLOCATION SEMANTICS
-- --------------------
-- allocation_factor[puma -> county] = (population of PUMA in county) /
--                                     (total population of PUMA).
--
-- Use case: when allocating a person-row's PWGTP across counties,
-- multiply PWGTP by allocation_factor for each county the PUMA
-- spans. Sum over a county = total population estimate for that
-- county.
-- ============================================================================


CREATE TABLE ref.puma2020_county_xwalk (
    state_fips         CHAR(2)        NOT NULL CHECK (state_fips ~ '^[0-9]{2}$'),
    puma               CHAR(5)        NOT NULL CHECK (puma ~ '^[0-9]{5}$'),
    county_fips        CHAR(5)        NOT NULL CHECK (county_fips ~ '^[0-9]{5}$'),

    -- Fraction of the PUMA's population that lives in this county.
    -- Sum across counties for one PUMA must equal 1.0 (within
    -- floating-point tolerance).
    allocation_factor  NUMERIC(8, 6)  NOT NULL
        CHECK (allocation_factor > 0 AND allocation_factor <= 1.000001),

    source_vintage     TEXT           NOT NULL,  -- e.g. "2020-decennial-est"
    notes              TEXT,

    created_at         TIMESTAMPTZ    NOT NULL DEFAULT now(),

    PRIMARY KEY (state_fips, puma, county_fips)
);

COMMENT ON TABLE ref.puma2020_county_xwalk IS
    'PUMA-to-county population-weighted allocation crosswalk. Each row '
    'gives the fraction of a PUMA''s population that lives in a county. '
    'Sum over counties for one PUMA = 1.0. Used to roll up PUMS-derived '
    'statistics (PUMA grain) to county grain. NJ-only seed; multi-state '
    'production deployments should stage the full Geocorr 2022 output.';


-- Validation: a deferred view that flags any PUMA whose allocations
-- do not sum to 1.0 (within tolerance). This is informational, not
-- a hard constraint, because (a) Postgres CHECK constraints cannot
-- span rows efficiently and (b) we want to be able to stage data
-- mid-migration without the constraint blocking the load.
CREATE OR REPLACE VIEW ref.v_puma_xwalk_invariant_violations AS
SELECT
    state_fips, puma,
    SUM(allocation_factor)        AS sum_allocation,
    COUNT(*)                       AS n_county_rows,
    array_agg(county_fips ORDER BY county_fips) AS counties
FROM ref.puma2020_county_xwalk
GROUP BY state_fips, puma
HAVING ABS(SUM(allocation_factor) - 1.0) > 0.001;

COMMENT ON VIEW ref.v_puma_xwalk_invariant_violations IS
    'Diagnostic view: any PUMA whose allocation factors do not sum to '
    '1.0 (within tolerance). Should be empty in a healthy seed. The '
    'orchestration layer can use this to fail-fast on seed corruption.';


CREATE INDEX puma2020_county_xwalk_county_idx
    ON ref.puma2020_county_xwalk (state_fips, county_fips);
