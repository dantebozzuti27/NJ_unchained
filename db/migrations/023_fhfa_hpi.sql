-- ============================================================================
-- Migration: 023_fhfa_hpi
--
-- TIER 2: FHFA House Price Index, county-level annual all-transactions.
--
-- WHY THIS EXISTS
-- ---------------
-- ACS B25077 reports the median value of owner-occupied units, but it is
-- a stock measure (snapshot at survey time) and is sensitive to changes in
-- the underlying composition of the housing stock. The FHFA HPI is a
-- repeat-sales index: it tracks price changes for the SAME homes over
-- time, controlling for compositional change. The two sources answer
-- different questions:
--
--   * B25077 = "what does a typical owned home in this county SELL for?"
--   * FHFA HPI = "how much have prices APPRECIATED for the same homes?"
--
-- For burden-ratio LONGITUDINAL comparisons, FHFA HPI is the right
-- deflator (it isolates price change from composition change). For
-- LEVELS comparisons, B25077 is the right answer.
--
-- The all-transactions HPI uses both purchase-money and refinance
-- appraisals; the purchase-only series excludes refis. We track the
-- all-transactions county series because purchase-only is published only
-- at the MSA level.
--
-- VINTAGE
-- -------
-- FHFA re-bases the HPI annually (typically to the most recent year).
-- The index series itself is vintage-stable (HPI level for a given
-- county-year does not change with re-basing -- only the SCALE changes,
-- and only relative to the new base).
-- ============================================================================

CREATE TABLE raw.fhfa_hpi_county (
    county_fips      CHAR(5)       NOT NULL CHECK (county_fips ~ '^[0-9]{5}$'),
    year             SMALLINT      NOT NULL CHECK (year BETWEEN 1975 AND 2099),

    -- All-transactions index value. FHFA's published value, in their
    -- current vintage's base. Comparable cross-year via ratios; not
    -- comparable across vintage re-bases unless re-anchored.
    hpi_at           NUMERIC(8,3)  NOT NULL CHECK (hpi_at > 0),

    -- FHFA also publishes annual percent change; we store it both ways
    -- so consumers can choose. annual_change is NULL for the first year
    -- a county has data.
    annual_change    NUMERIC(8,4),

    -- Number of repeat-sales transactions used to estimate this index
    -- value. Below ~25 the estimate is statistically thin; FHFA flags
    -- those years but still publishes them. We keep the count and let
    -- downstream filters apply their own thresholds.
    n_transactions   INTEGER       CHECK (n_transactions IS NULL OR n_transactions >= 0),

    source_url       TEXT          NOT NULL,
    source_sha256    CHAR(64)      NOT NULL,
    source_vintage   TEXT          NOT NULL,   -- e.g. "2024Q4"
    ingested_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),

    PRIMARY KEY (county_fips, year)
);

COMMENT ON TABLE raw.fhfa_hpi_county IS
    'FHFA House Price Index, county-level, annual, all-transactions. '
    'Repeat-sales index controlling for compositional change in the '
    'housing stock. Cross-year comparable via ratios.';

CREATE INDEX raw_fhfa_hpi_year_idx ON raw.fhfa_hpi_county (year);


-- Convenience view: FHFA HPI growth indexed to a base year, computed
-- on the fly via a stable SQL function. Returns the index value RELATIVE
-- to the chosen base year (i.e. base_year always = 100.000).
CREATE OR REPLACE FUNCTION derived.f_fhfa_hpi_indexed(base_year SMALLINT)
RETURNS TABLE (
    county_fips     CHAR(5),
    year            SMALLINT,
    hpi_indexed     NUMERIC,
    hpi_raw         NUMERIC,
    base_year_used  SMALLINT
)
LANGUAGE sql STABLE
AS $$
    WITH base AS (
        SELECT county_fips, hpi_at AS base_hpi
        FROM   raw.fhfa_hpi_county
        WHERE  year = base_year
    )
    SELECT
        h.county_fips,
        h.year,
        round((h.hpi_at / b.base_hpi) * 100.0, 3) AS hpi_indexed,
        h.hpi_at                                   AS hpi_raw,
        base_year                                  AS base_year_used
    FROM   raw.fhfa_hpi_county h
    JOIN   base b USING (county_fips);
$$;

COMMENT ON FUNCTION derived.f_fhfa_hpi_indexed(SMALLINT) IS
    'FHFA HPI re-indexed so that base_year = 100.000 for each county. '
    'Use this view to compare price growth across counties on a common scale.';


-- NJ-only convenience view, joined to ref.county.
CREATE VIEW public.v_fhfa_hpi_nj AS
SELECT
    c.county_id,
    c.name           AS county_name,
    h.year,
    h.hpi_at,
    h.annual_change,
    h.n_transactions
FROM raw.fhfa_hpi_county h
JOIN ref.county         c ON c.county_fips = h.county_fips
WHERE c.state_code = 'NJ';
