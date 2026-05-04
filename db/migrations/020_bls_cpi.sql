-- ============================================================================
-- Migration: 020_bls_cpi
--
-- TIER 2: U.S. Bureau of Labor Statistics Consumer Price Index for All Urban
-- Consumers (CPI-U). Public, monthly, no auth.
--
-- WHY THIS EXISTS
-- ---------------
-- Every nominal-dollar series in the platform must be deflatable to a stable
-- base-year basis to permit longitudinal comparisons. The headline burden
-- ratio (housing_cost / household_income) is invariant to the choice of base
-- year because CPI cancels in the ratio, BUT:
--
--   * absolute-dollar reporting (UI, "in 2020 dollars X = today $Y")
--   * decomposition of the burden ratio into nominal vs real components
--   * cross-comparison with other deflators (FHFA HPI, Zillow ZHVI) where
--     the alternative deflator's growth is itself the metric of interest
--
-- all require the underlying CPI series. We store the raw monthly index and
-- expose a curated annual-average view that the rest of the platform should
-- prefer. Monthly access remains available for narrow needs (e.g. matching
-- a survey month exactly).
--
-- SERIES SCOPE
-- ------------
-- We track the small canonical set of national CPI-U series:
--
--   CUUR0000SA0       All items (NSA, headline)
--   CUSR0000SA0       All items (SA)
--   CUUR0000SA0L1E    All items less food and energy ("core", NSA)
--   CUUR0000SAH       Shelter (NSA) -- direct cross-check on housing-only deflation
--   CUUR0000SAH1      Rent of primary residence (NSA)
--   CUUR0000SAH2      Owners' equivalent rent of residences (NSA)
--
-- Regional / size-class series (CUURA101SA0 etc.) are deferred until the
-- platform actually needs sub-national CPI; the BLS regional series are
-- noisier and less suitable for longitudinal NJ-county work than the
-- headline national series with a separate housing index (FHFA, ZHVI) for
-- locality.
--
-- NATURAL KEY
-- -----------
-- (series_id, year, period). period is BLS's standard:
--   M01..M12   monthly observations
--   M13        annual average (BLS-published; we DO NOT recompute this)
--   S01, S02   semi-annual averages
--   Q01..Q04   not used by CPI-U (BLS uses M*, but we allow the column shape)
--
-- VINTAGING
-- ---------
-- BLS re-references the index every ~10 years (currently 1982-84=100 for
-- most series). Re-references invalidate cross-vintage absolute-level
-- comparisons but preserve year-over-year ratios; we therefore store the
-- BLS-published index unchanged and let downstream queries do the
-- chain-deflation as needed. Re-reference events are signaled in
-- governance.dataset_health.
-- ============================================================================

CREATE TABLE raw.cpi_u (
    series_id        TEXT          NOT NULL CHECK (series_id ~ '^[A-Z0-9]{8,30}$'),
    year             SMALLINT      NOT NULL CHECK (year BETWEEN 1913 AND 2099),
    period           TEXT          NOT NULL
        CHECK (period ~ '^(M(0[1-9]|1[0-3])|S0[12])$'),

    value            NUMERIC(10,3) NOT NULL CHECK (value > 0),

    -- BLS does not version individual observations, but they do retroactively
    -- revise (typically the SA series; NSA is final on first release).
    -- We track the latest revision by ingest time; idempotent reloads with
    -- changed values UPDATE in place via the ON CONFLICT clause used by
    -- the ingester (see ingestion.bls_cpi).
    source_url       TEXT          NOT NULL,
    source_sha256    CHAR(64)      NOT NULL,
    ingested_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),

    PRIMARY KEY (series_id, year, period)
);

COMMENT ON TABLE  raw.cpi_u IS
    'BLS Consumer Price Index for All Urban Consumers, monthly observations. '
    'Index level (NOT inflation rate). 1982-84=100 reference for most series.';
COMMENT ON COLUMN raw.cpi_u.period IS
    'M01..M12 = monthly, M13 = BLS-published annual average, S01/S02 = semi-annual.';

CREATE INDEX raw_cpi_u_year_idx ON raw.cpi_u (year);


-- Curated annual-average view. Prefers BLS's M13 if present (which is the
-- BLS-published annual average, not a recomputation), otherwise computes
-- the unweighted mean of M01..M12 IFF all twelve months are present.
-- Returning NULL when the year is incomplete is preferable to silently
-- understating the average.
CREATE VIEW derived.cpi_u_annual AS
WITH m13 AS (
    SELECT series_id, year, value AS m13_value
    FROM raw.cpi_u
    WHERE period = 'M13'
),
monthly_avg AS (
    SELECT
        series_id,
        year,
        avg(value)        AS computed_value,
        count(*)          AS n_months
    FROM raw.cpi_u
    WHERE period BETWEEN 'M01' AND 'M12'
    GROUP BY series_id, year
)
SELECT
    coalesce(m13.series_id, ma.series_id)             AS series_id,
    coalesce(m13.year,      ma.year)                  AS year,
    coalesce(
        m13.m13_value,
        CASE WHEN ma.n_months = 12 THEN ma.computed_value END
    )                                                  AS annual_value,
    CASE
        WHEN m13.m13_value IS NOT NULL THEN 'bls_published'
        WHEN ma.n_months = 12          THEN 'computed_12mo_avg'
        ELSE                                'incomplete_year'
    END                                                AS source_label,
    coalesce(ma.n_months, 0)                           AS n_monthly_obs
FROM m13
FULL OUTER JOIN monthly_avg ma USING (series_id, year);

COMMENT ON VIEW derived.cpi_u_annual IS
    'Annual CPI-U index by series_id. annual_value is NULL when the year is '
    'incomplete and BLS has not yet published M13. source_label distinguishes '
    'BLS-published annual averages from our 12-month means.';


-- Convenience view: pairwise deflation factors against the headline series
-- (CUUR0000SA0). To deflate a year-Y nominal value to year-B base dollars,
-- multiply by deflator_to_base_year(B) / deflator_to_base_year(Y) -- or
-- equivalently look up the factor in this view directly.
--
-- We omit a stored base year because every consumer of this view has its
-- own preferred base; we expose ratios on the fly.
CREATE VIEW derived.cpi_u_headline_annual AS
SELECT year, annual_value AS cpi_u_all_items
FROM   derived.cpi_u_annual
WHERE  series_id    = 'CUUR0000SA0'
  AND  annual_value IS NOT NULL;

COMMENT ON VIEW derived.cpi_u_headline_annual IS
    'CPI-U All Items (NSA, CUUR0000SA0) by year. Use as the canonical '
    'deflator for housing-burden longitudinal comparisons.';
