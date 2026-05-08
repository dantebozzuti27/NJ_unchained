-- ============================================================================
-- Migration: 077_nj_municipality_substrate
--
-- PHASE 8a of VISION_2026.md (idea spec §11 -- "relocation decision tools").
--
-- The county-level engine (Phases 1-4) answers "what's the median NJ home
-- look like in Bergen County?" -- but most NJ counties are 30-50 munis
-- wide, and the variance within a county dwarfs the variance across
-- counties. A user looking at Tenafly vs. Lyndhurst (both Bergen) faces
-- a $1.4M vs $480K median-home spread; the county-average $850K hides
-- both signals.
--
-- This migration introduces the municipality substrate so the
-- personalization engine can drill from county -> town:
--
--   ref.nj_municipality        Canonical 4-digit MuniCode list (DCA's
--                              own coding) joined to ref.county FIPS.
--   raw.nj_property_tax_muni   Annual per-muni DCA property tax rows
--                              (avg_residential_value, cy_total_rate,
--                              levies, etc.) -- same shape as
--                              raw.nj_property_tax_county but keyed by
--                              muni_code instead of county_fips.
--
-- ARCHITECTURE: muni-level data is a STRICT EXTENSION of the existing
-- county-level engine. Every county-level function and view stays
-- unchanged; the muni-level companions live alongside in migration 078.
-- This is composition, not replacement; it preserves all 1156 existing
-- tests and keeps the existing /housing/[id]/collapse pages working
-- without modification.
--
-- DCA MUNICODE FORMAT
-- -------------------
-- 4 digits: first 2 are the DCA county code (01..21, alphabetical;
-- mapping in ref.nj_dca_county); last 2 are the municipal index within
-- the county (01..NN). Suffix '00' is reserved for the county-level
-- summary row, which is intentionally excluded from this table (the
-- county summary lives in raw.nj_property_tax_county).
--
-- POPULATION
-- ----------
-- The 2024 NJ DCA workbook ('24taxes.xls', 'Municipal Tax Summary'
-- sheet) lists 564 incorporated municipalities (post-2013 Princeton
-- merger). The seed file db/seeds/040_nj_municipality.sql ships those
-- 564 rows. Future workbooks may differ if more munis consolidate;
-- the ON CONFLICT (muni_code) clause makes the seed re-runnable.
--
-- DEPENDS ON:
--   * ref.county (migration 001-or-similar)
--   * ref.nj_dca_county (migration 025)
-- ============================================================================

BEGIN;


-- ============================================================================
-- 1. ref.nj_municipality -- the canonical NJ municipality list
-- ============================================================================

CREATE TABLE ref.nj_municipality (
    muni_code        CHAR(4)   PRIMARY KEY
        -- 4 digits, last two MUST NOT be '00' (county summaries live
        -- elsewhere). The leading 2 digits must be a valid DCA county
        -- code (01..21).
        CHECK (muni_code ~ '^[0-2][0-9][0-9][0-9]$'
            AND substring(muni_code, 3, 2) <> '00'),

    muni_name        TEXT      NOT NULL
        CHECK (length(muni_name) > 0 AND length(muni_name) <= 80),

    -- The county this muni belongs to. NJ munis don't change county;
    -- updates to this column would indicate a data error.
    county_fips      CHAR(5)   NOT NULL
        REFERENCES ref.county(county_fips),

    -- Provenance, identical pattern to other ref.* tables.
    source_url       TEXT      NOT NULL,
    source_citation  TEXT      NOT NULL,

    -- Within a county, every muni name is unique. Across counties,
    -- many names repeat (e.g. 'Washington Township' exists in 5
    -- different NJ counties), so the unique key is per-county.
    UNIQUE (county_fips, muni_name)
);

COMMENT ON TABLE ref.nj_municipality IS
    'Canonical list of NJ incorporated municipalities, keyed by DCA '
    '4-digit MuniCode. First 2 digits = DCA county code, last 2 = '
    'municipal index within county; suffix "00" (county summaries) '
    'is excluded by CHECK constraint. Seeded from the 2024 DCA '
    'workbook (564 munis post-Princeton-merger).';

CREATE INDEX ref_nj_municipality_county_idx
    ON ref.nj_municipality (county_fips);


-- Coverage view: how many munis we have per county. The 2024 workbook
-- gives Bergen 70, Burlington 40, etc.; this is a quick sanity check
-- that the seed loaded completely.
CREATE OR REPLACE VIEW ref.v_nj_municipality_coverage AS
SELECT
    c.county_fips,
    c.name                                  AS county_name,
    count(m.muni_code)                      AS n_munis,
    min(m.muni_code)                        AS first_muni_code,
    max(m.muni_code)                        AS last_muni_code
FROM ref.county c
LEFT JOIN ref.nj_municipality m ON m.county_fips = c.county_fips
WHERE c.state_code = 'NJ'
GROUP BY c.county_fips, c.name
ORDER BY c.name;

COMMENT ON VIEW ref.v_nj_municipality_coverage IS
    'Per-county muni count + min/max muni_code. Asset-check substrate: '
    'every NJ county should have n_munis >= 1.';


-- ============================================================================
-- 2. raw.nj_property_tax_muni -- annual per-muni DCA tax data
--
-- Same column shape as raw.nj_property_tax_county, keyed by muni_code
-- instead of county_fips. The redundant FK on muni_code -> ref.nj_municipality
-- is intentional: it forces us to seed the muni dimension before we can
-- load any property-tax row, which is the right ordering invariant.
-- ============================================================================

CREATE TABLE raw.nj_property_tax_muni (
    muni_code                    CHAR(4)        NOT NULL
        REFERENCES ref.nj_municipality(muni_code),
    year                         SMALLINT       NOT NULL
        CHECK (year BETWEEN 1998 AND 2099),

    net_valuation_taxable        NUMERIC(20,2)
        CHECK (net_valuation_taxable IS NULL OR net_valuation_taxable >= 0),

    total_county_levy            NUMERIC(20,2),
    total_school_levy            NUMERIC(20,2),
    total_municipal_levy         NUMERIC(20,2),
    total_levy                   NUMERIC(20,2),

    cy_total_rate                NUMERIC(8,5),
    cy_county_rate               NUMERIC(8,5),
    cy_school_rate               NUMERIC(8,5),
    cy_municipal_rate            NUMERIC(8,5),

    avg_residential_value        NUMERIC(14,2),
    avg_total_property_taxes     NUMERIC(12,2),
    avg_county_taxes             NUMERIC(12,2),
    avg_school_taxes             NUMERIC(12,2),
    avg_municipal_taxes          NUMERIC(12,2),

    cy_equalized_property_value  NUMERIC(20,2),
    cy_total_eq_rate             NUMERIC(8,5),

    source_url                   TEXT           NOT NULL,
    source_sha256                CHAR(64)       NOT NULL,
    source_vintage               TEXT           NOT NULL,
    ingested_at                  TIMESTAMPTZ    NOT NULL DEFAULT now(),

    PRIMARY KEY (muni_code, year)
);

COMMENT ON TABLE raw.nj_property_tax_muni IS
    'Per-municipality NJ DCA annual property tax statistics. Same '
    'columns as raw.nj_property_tax_county; the muni_code FK forces '
    'the muni dimension to be seeded first. Headline columns: '
    'avg_residential_value (median-home proxy at the muni level) and '
    'cy_total_rate (effective tax rate, percent of assessed value).';

CREATE INDEX raw_nj_property_tax_muni_year_idx
    ON raw.nj_property_tax_muni (year);

-- Per-county pivot index: muni rows are most often filtered by their
-- containing county and year (e.g. "all 70 Bergen munis in 2024").
-- The natural join goes through ref.nj_municipality.county_fips.
CREATE INDEX raw_nj_property_tax_muni_year_idx_byprefix
    ON raw.nj_property_tax_muni (substring(muni_code, 1, 2), year);


-- Coverage view: per-(county, year) muni-load completeness. Drives the
-- "how many of Bergen's 70 munis are in 2024" sanity check that the
-- ingester ran end-to-end.
CREATE OR REPLACE VIEW raw.v_nj_property_tax_muni_coverage AS
SELECT
    c.county_fips,
    c.name                              AS county_name,
    p.year,
    count(p.muni_code)                  AS n_munis_loaded,
    cov.n_munis                         AS n_munis_total,
    round(
        100.0 * count(p.muni_code) / NULLIF(cov.n_munis, 0), 2
    )                                   AS pct_loaded
FROM ref.county c
JOIN ref.v_nj_municipality_coverage cov ON cov.county_fips = c.county_fips
LEFT JOIN ref.nj_municipality m ON m.county_fips = c.county_fips
LEFT JOIN raw.nj_property_tax_muni p ON p.muni_code = m.muni_code
WHERE c.state_code = 'NJ'
GROUP BY c.county_fips, c.name, p.year, cov.n_munis
ORDER BY p.year DESC NULLS LAST, c.name;

COMMENT ON VIEW raw.v_nj_property_tax_muni_coverage IS
    'Per-(county, year) muni-load completeness. n_munis_loaded should '
    'equal n_munis_total when a year is fully ingested; pct_loaded < '
    '100% surfaces partial-load anomalies.';


COMMIT;
