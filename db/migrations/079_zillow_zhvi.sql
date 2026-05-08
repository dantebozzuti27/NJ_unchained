-- ============================================================================
-- Migration: 079_zillow_zhvi
--
-- VISION_2026 §6 PHASE 6 -- ZILLOW ZHVI (cross-source housing index).
--
-- The platform's primary housing index is FHFA HPI (raw.fhfa_hpi_county,
-- migration 023). FHFA is a repeat-sales index built from Fannie/Freddie
-- conforming mortgage data; it controls for compositional change but is
-- structurally weighted toward the conforming-loan slice of the market
-- and lags by roughly one quarter.
--
-- Spec §3.2 explicitly names Zillow ZHVI / Redfin as PREFERRED housing
-- indices for the platform ("Index-based, not raw listings. Zillow ZHVI /
-- Redfin county series."), with the rationale that they cover the entire
-- transacted housing stock (not just conforming) and publish monthly with
-- a ~one-month lag.
--
-- Spec §8.1 then asks for cross-source validation: "Census income vs BLS
-- wage" is one example, but the same discipline applies here -- when two
-- different methodologies (FHFA repeat-sales vs Zillow's smoothed,
-- seasonally-adjusted typical-home-value series) disagree on a (county,
-- year), that disagreement is itself a signal worth surfacing.
--
-- This migration ships the substrate for both objectives:
--
--   1. raw.zillow_zhvi_county -- monthly ZHVI per NJ county, long format
--      (one row per (county_fips, observation_month)). Source provenance
--      stamped on every row (URL, SHA-256, HTTP Last-Modified header,
--      vintage tag).
--
--   2. derived.v_zhvi_county_annual -- annual aggregation of the monthly
--      series. Mirrors the shape of derived.fred_annual so downstream
--      consumers can compose with FHFA / FRED uniformly.
--
--   3. derived.f_zhvi_county_indexed(base_year) -- re-indexed series so
--      ZHVI(base_year) = 100 per county. Mirrors derived.f_fhfa_hpi_indexed
--      so the frontend can plot both indices on a common 100-baseline.
--
--   4. derived.f_housing_index_cross_source(base_year) -- per-(county,
--      year) row joining the FHFA and ZHVI re-indexed series, with a
--      divergence column. This is the SQL surface Phase 7 asset checks
--      will read from to flag (county, year) pairs where the two
--      indices disagree by more than a calibrated threshold.
--
-- ZHVI series choice: "uc_sfrcondo_tier_0.33_0.67_sm_sa_month".
--    uc       = unsmoothed conditional? -- actually Zillow's TYPICAL VALUE
--    sfrcondo = single-family residence + condominium (the headline
--               typical-home definition)
--    tier_0.33_0.67 = mid-tier (33rd-67th percentile by home value)
--    sm       = smoothed (3-month moving average to dampen noise)
--    sa       = seasonally adjusted
--    month    = monthly observation cadence
-- This is the headline series Zillow Research publishes. URL:
--    https://files.zillowstatic.com/research/public_csvs/zhvi/
--      County_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv
--
-- Provenance: every row carries source_url + source_sha256 of the wide
-- CSV that produced it + source_modified_at (HTTP Last-Modified, the
-- closest thing Zillow gives us to a vintage stamp on the file itself).
--
-- Substrate honesty: this migration creates ZERO derived rows. It only
-- creates the table + the views/functions. Loading is a separate step
-- run from the ingester (nj-ingest-zhvi) against the populated raw table.
-- ============================================================================

BEGIN;

INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '1.6.0-zhvi-cross-source-v1',
    'Phase 6 cross-source housing index: adds Zillow ZHVI county-level '
    'monthly substrate (raw.zillow_zhvi_county) plus annual mean / '
    're-indexed views and a FHFA-vs-ZHVI cross-source divergence '
    'function for spec §8.1 cross-source validation. The platform now '
    'has TWO independent county-level housing indices that can be '
    'plotted side by side and asset-checked for divergence.',
    '2026-05-08'::DATE,
    NULL
)
ON CONFLICT (formula_version) DO NOTHING;


-- ----------------------------------------------------------------------------
-- raw.zillow_zhvi_county
--
-- Long-format storage of the monthly ZHVI series. One row per (county,
-- observation_month). 21 NJ counties x ~313 months (2000-01 .. 2026-03)
-- = ~6,573 rows after a clean load from the current Zillow vintage.
-- ----------------------------------------------------------------------------

CREATE TABLE raw.zillow_zhvi_county (
    -- Zillow's stable region identifier. Useful for joining to other
    -- Zillow Research datasets (ZORI, ZHVF). Kept alongside FIPS so a
    -- consumer who only has one identifier can still resolve.
    region_id           INTEGER       NOT NULL,

    -- Canonical join key on every county-level NJ table.
    county_fips         CHAR(5)       NOT NULL
        REFERENCES ref.county(county_fips),

    -- Zillow's RegionName (e.g. "Bergen County"). Stored verbatim so
    -- a row's provenance is self-contained without an additional
    -- ref.county lookup.
    region_name         TEXT          NOT NULL,

    -- Two-letter state abbreviation, redundant with county_fips but
    -- useful as a sanity check and for future multi-state expansion
    -- (Phase 9). NJ rows always have state_code = 'NJ'.
    state_code          CHAR(2)       NOT NULL,

    -- Zillow's Metro field (e.g. "New York-Newark-Jersey City, NY-NJ-PA").
    -- Nullable because some rural counties have NULL Metro at Zillow.
    metro               TEXT,

    -- Calendar end-of-month date Zillow associates with the observation.
    -- Constrained to the closed range [2000-01-01, 2099-12-31] -- ZHVI
    -- starts at 2000-01-31 and we don't expect the platform to outlive
    -- the 21st century.
    observation_month   DATE          NOT NULL
        CHECK (observation_month >= DATE '2000-01-01'
           AND observation_month <  DATE '2100-01-01'),

    -- Smoothed, seasonally-adjusted typical home value for the
    -- (county, month). Zillow publishes this with full floating-point
    -- precision; we store NUMERIC(14,4) to capture the published value
    -- exactly without rounding (12 digits before the decimal handles
    -- counties up to $99B median, comfortably above any plausible
    -- county-level value).
    zhvi                NUMERIC(14,4) NOT NULL CHECK (zhvi > 0),

    -- Provenance.
    source_url          TEXT          NOT NULL,
    source_sha256       CHAR(64)      NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    source_modified_at  TIMESTAMPTZ,
    source_vintage      TEXT          NOT NULL,
    ingested_at         TIMESTAMPTZ   NOT NULL DEFAULT now(),

    PRIMARY KEY (county_fips, observation_month)
);

COMMENT ON TABLE raw.zillow_zhvi_county IS
    'Monthly Zillow Home Value Index (ZHVI) per county. Long-format '
    'storage of the wide CSV Zillow Research publishes at '
    'https://files.zillowstatic.com/research/public_csvs/zhvi/ -- '
    'specifically the uc_sfrcondo_tier_0.33_0.67_sm_sa_month series '
    '(mid-tier single-family + condo, smoothed, seasonally adjusted).';

CREATE INDEX raw_zhvi_county_year_idx
    ON raw.zillow_zhvi_county
    ((EXTRACT(YEAR FROM observation_month)::SMALLINT), county_fips);


-- ----------------------------------------------------------------------------
-- derived.v_zhvi_county_annual
--
-- Annual aggregation: mean ZHVI across the 12 calendar months of each
-- year, plus the December (year-end) value. Mirrors the shape of
-- derived.fred_annual.
-- ----------------------------------------------------------------------------

CREATE OR REPLACE VIEW derived.v_zhvi_county_annual AS
WITH monthly AS (
    SELECT
        county_fips,
        EXTRACT(YEAR FROM observation_month)::SMALLINT AS year,
        observation_month,
        zhvi
    FROM raw.zillow_zhvi_county
),
year_end AS (
    SELECT DISTINCT ON (county_fips, year)
        county_fips, year, zhvi AS zhvi_year_end_unrounded,
        observation_month        AS year_end_month
    FROM monthly
    ORDER BY county_fips, year, observation_month DESC
)
SELECT
    m.county_fips,
    m.year,
    AVG(m.zhvi)::NUMERIC(14,4)   AS zhvi_annual_mean,
    ye.zhvi_year_end_unrounded   AS zhvi_year_end,
    ye.year_end_month            AS year_end_month,
    COUNT(*)::SMALLINT           AS n_months
FROM monthly m
JOIN year_end ye USING (county_fips, year)
GROUP BY m.county_fips, m.year, ye.zhvi_year_end_unrounded, ye.year_end_month;

COMMENT ON VIEW derived.v_zhvi_county_annual IS
    'Annual ZHVI aggregation per (county, year): mean across 12 months '
    'plus the December (year-end) observation. Use n_months to filter '
    'partial-year vintages -- the most recent year always has < 12 '
    'months until Zillow publishes the December observation.';


-- ----------------------------------------------------------------------------
-- derived.f_zhvi_county_indexed
--
-- Re-indexes ZHVI so that ZHVI(base_year) = 100 per county. Companion to
-- derived.f_fhfa_hpi_indexed; the two functions return identical row
-- shapes so a UNION ALL or FULL OUTER JOIN composes cleanly.
-- ----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION derived.f_zhvi_county_indexed(p_base_year SMALLINT)
RETURNS TABLE (
    county_fips     CHAR(5),
    year            SMALLINT,
    zhvi_indexed    NUMERIC,
    zhvi_raw        NUMERIC,
    base_year_used  SMALLINT
)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    WITH base AS (
        SELECT county_fips, zhvi_annual_mean AS base_zhvi
        FROM derived.v_zhvi_county_annual
        WHERE year = p_base_year
    )
    SELECT
        v.county_fips,
        v.year,
        ROUND((v.zhvi_annual_mean / b.base_zhvi) * 100.0, 3) AS zhvi_indexed,
        v.zhvi_annual_mean                                    AS zhvi_raw,
        p_base_year                                           AS base_year_used
    FROM derived.v_zhvi_county_annual v
    JOIN base b ON b.county_fips = v.county_fips
    WHERE b.base_zhvi IS NOT NULL AND b.base_zhvi <> 0
$$;

COMMENT ON FUNCTION derived.f_zhvi_county_indexed(SMALLINT) IS
    'Zillow ZHVI re-indexed so that base_year = 100 per county. Companion '
    'to derived.f_fhfa_hpi_indexed; same row shape for cross-source '
    'composition. Returns one row per (county_fips, year) where both the '
    'base year and the value year have a non-NULL annual ZHVI.';


-- ----------------------------------------------------------------------------
-- derived.f_housing_index_cross_source
--
-- Joins FHFA HPI and Zillow ZHVI on a common base year and exposes the
-- divergence between the two methodologies for each (county, year).
-- This is the substrate Phase 7 asset checks will read to flag suspect
-- rows -- e.g. "FHFA says Bergen 2024 grew 4.2% from base, ZHVI says
-- 12.1%; flag for manual review".
--
-- Substrate honesty: rows where one source has data and the other does
-- not are still emitted (FULL OUTER JOIN), with the missing column NULL
-- and the divergence columns NULL. The frontend / asset checks decide
-- what to do with those.
-- ----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION derived.f_housing_index_cross_source(p_base_year SMALLINT)
RETURNS TABLE (
    county_fips                CHAR(5),
    year                       SMALLINT,
    fhfa_hpi_indexed           NUMERIC,
    zillow_zhvi_indexed        NUMERIC,
    divergence_indexed_points  NUMERIC,
    divergence_pct_of_fhfa     NUMERIC,
    base_year_used             SMALLINT
)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT
        COALESCE(f.county_fips, z.county_fips)          AS county_fips,
        COALESCE(f.year, z.year)                        AS year,
        f.hpi_indexed                                   AS fhfa_hpi_indexed,
        z.zhvi_indexed                                  AS zillow_zhvi_indexed,
        CASE
            WHEN f.hpi_indexed IS NOT NULL
             AND z.zhvi_indexed IS NOT NULL
            THEN ROUND((z.zhvi_indexed - f.hpi_indexed)::NUMERIC, 4)
        END                                              AS divergence_indexed_points,
        CASE
            WHEN f.hpi_indexed IS NOT NULL
             AND z.zhvi_indexed IS NOT NULL
             AND f.hpi_indexed > 0
            THEN ROUND(
                ((z.zhvi_indexed - f.hpi_indexed) / f.hpi_indexed)::NUMERIC, 5
            )
        END                                              AS divergence_pct_of_fhfa,
        p_base_year                                      AS base_year_used
    FROM derived.f_fhfa_hpi_indexed(p_base_year) f
    FULL OUTER JOIN derived.f_zhvi_county_indexed(p_base_year) z
      ON z.county_fips = f.county_fips
     AND z.year        = f.year
$$;

COMMENT ON FUNCTION derived.f_housing_index_cross_source(SMALLINT) IS
    'FHFA HPI vs Zillow ZHVI cross-source divergence per (county, year). '
    'Both indices re-anchored to base_year = 100. divergence_indexed_points '
    'is signed (ZHVI - FHFA in index points); divergence_pct_of_fhfa is '
    'signed (positive = ZHVI growth above FHFA). NULL when either source '
    'is missing for the row. Substrate for spec §8.1 cross-source '
    'validation asset checks (Phase 7).';


-- ----------------------------------------------------------------------------
-- public.v_zhvi_nj_recent
--
-- Convenience view for the (eventual) /housing methodology page or admin
-- dashboard. Most-recent 12 months per NJ county, joined to ref.county
-- for the human-readable name + county_id slug.
-- ----------------------------------------------------------------------------

CREATE OR REPLACE VIEW public.v_zhvi_nj_recent AS
SELECT
    c.county_id,
    c.name              AS county_name,
    z.county_fips,
    z.observation_month,
    z.zhvi,
    z.source_vintage
FROM raw.zillow_zhvi_county z
JOIN ref.county            c ON c.county_fips = z.county_fips
WHERE z.observation_month >= (
    SELECT MAX(observation_month) - INTERVAL '11 months'
    FROM raw.zillow_zhvi_county
)
ORDER BY z.county_fips, z.observation_month DESC;

COMMENT ON VIEW public.v_zhvi_nj_recent IS
    'NJ counties, most-recent 12 months of ZHVI. Use for the methodology '
    'page''s freshness panel and as a smoke-test query after a fresh load.';

COMMIT;
