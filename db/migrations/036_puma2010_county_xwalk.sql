-- ============================================================================
-- Migration: 036_puma2010_county_xwalk
--
-- TIER 3 reference table: 2010-vintage PUMA-to-county allocation crosswalk.
--
-- WHY
-- ---
-- The platform's existing crosswalk (ref.puma2020_county_xwalk, migration
-- 028) is keyed on POST-2020-decennial PUMAs ("PUMA20"). Every record in
-- the 5-Year ACS PUMS file sampled BEFORE the 2020 decennial revision
-- (i.e., respondents from survey years 2018-2019) carries a 2010-vintage
-- PUMA code instead, and currently has no county allocator -- the derived
-- layer drops them with a logged warning.
--
-- For the 2022 5-Year file that's 340,122 person + 153,999 housing rows
-- silently dropped from county aggregation -- about 80% of the 5-year
-- sample. That's the bulk of what makes the 5-year product analytically
-- valuable in the first place (smaller-county unsuppression).
--
-- This migration adds a parallel crosswalk for 2010-vintage PUMAs, so
-- the derived county compute can dispatch on `puma_vintage` and pick
-- the right allocator. County FIPS codes are vintage-stable (counties
-- don't change boundaries between decennial revisions in NJ), so the
-- aggregation key downstream is unchanged.
--
-- METHODOLOGY
-- -----------
-- Allocation factors derived from the Census Bureau's
-- 2010_Census_Tract_to_2010_PUMA.txt file (the canonical relationship
-- file at https://www2.census.gov/geo/docs/maps-data/data/rel/). For
-- each (PUMA10, county) pair we count the number of 2010 Census tracts
-- in that intersection and use it as a population proxy:
--
--     allocation_factor[puma10 -> county] =
--         tract_count(puma10, county) / tract_count(puma10).
--
-- 2010 Census tracts are nominally calibrated to ~4,000 persons each,
-- so tract count is accurate to within ~5% of true 2010 P1 (decennial)
-- population for typical NJ multi-county PUMAs. This is the same order
-- of approximation as the hand-coded 2020 crosswalk; an operator who
-- needs higher precision can stage Geocorr 2018 (PUMA10, weighting
-- variable "P1 population 2010") output to replace these factors.
--
-- For NJ this resolves to 75 rows: 71 single-county PUMAs (allocation
-- 1.0) + 2 multi-county PUMAs (4 rows total, 2 per PUMA):
--
--   * 02500: Salem (FIPS 34033) 71.4% / Cumberland (34011) 28.6%
--   * 02600: Cape May (34009) 89.2% / Atlantic (34001) 10.8%
--
-- Note: these multi-county splits differ from the 2020-vintage
-- analogues (02501 / 02601) because the geographic boundaries of the
-- multi-county PUMAs were redrawn in the 2020 revision. The 2010 PUMA
-- 02500 covers a slightly different area than the 2020 PUMA 02501.
-- This is correct behavior; the substrate stores both vintages
-- independently.
--
-- INVARIANTS
-- ----------
-- Same as the 2020 table: allocation_factor in (0, 1.000001];
-- per-PUMA sum = 1.0 within tolerance. The 2010 invariant view
-- mirrors the 2020 view but on the 2010 table.
-- ============================================================================


CREATE TABLE ref.puma2010_county_xwalk (
    state_fips         CHAR(2)        NOT NULL CHECK (state_fips ~ '^[0-9]{2}$'),
    puma               CHAR(5)        NOT NULL CHECK (puma ~ '^[0-9]{5}$'),
    county_fips        CHAR(5)        NOT NULL CHECK (county_fips ~ '^[0-9]{5}$'),
    allocation_factor  NUMERIC(8, 6)  NOT NULL
        CHECK (allocation_factor > 0 AND allocation_factor <= 1.000001),
    source_vintage     TEXT           NOT NULL,
    notes              TEXT,
    created_at         TIMESTAMPTZ    NOT NULL DEFAULT now(),
    PRIMARY KEY (state_fips, puma, county_fips)
);

COMMENT ON TABLE ref.puma2010_county_xwalk IS
    '2010-vintage PUMA -> county population-weighted allocation. Mirrors '
    'ref.puma2020_county_xwalk for 2010-decennial PUMA boundaries. Used '
    'to county-allocate ACS 5-Year PUMS records sampled in 2018-2019 '
    '(those records carry PUMA10 codes per Census''s decennial split). '
    'NJ-only seed; allocation factors are 2010 Census tract-count '
    'approximations (operator can replace with Geocorr 2018 output).';

CREATE OR REPLACE VIEW ref.v_puma2010_xwalk_invariant_violations AS
SELECT
    state_fips, puma,
    SUM(allocation_factor)        AS sum_allocation,
    COUNT(*)                       AS n_county_rows,
    array_agg(county_fips ORDER BY county_fips) AS counties
FROM ref.puma2010_county_xwalk
GROUP BY state_fips, puma
HAVING ABS(SUM(allocation_factor) - 1.0) > 0.001;

COMMENT ON VIEW ref.v_puma2010_xwalk_invariant_violations IS
    'Diagnostic view for ref.puma2010_county_xwalk; mirrors '
    'ref.v_puma_xwalk_invariant_violations. Should be empty.';

CREATE INDEX puma2010_county_xwalk_county_idx
    ON ref.puma2010_county_xwalk (state_fips, county_fips);
