-- ============================================================================
-- Migration: 024_fred_rates
--
-- TIER 2: Federal Reserve Economic Data (FRED) rate series. Generic
-- single-value time series keyed by series_id.
--
-- WHY THIS EXISTS
-- ---------------
-- The headline burden ratio for OWNER households is sensitive to the
-- mortgage rate at the time of purchase: a $500K home at 3% has a very
-- different monthly cost than at 7%. To produce counterfactual ratios
-- ("what would the burden be at today's prices but 2021's rates?") the
-- platform needs a clean, vintaged time series of the relevant rates.
--
-- We track a small canonical set:
--
--   MORTGAGE30US  Freddie Mac 30-year fixed mortgage average (weekly)
--   DGS10         10-year Treasury constant maturity (daily)
--   FEDFUNDS      Effective Federal Funds Rate (monthly)
--
-- All three are public, no auth required. FRED also offers an API key
-- for higher-volume access; we use the public CSV download which has no
-- rate limit at our volumes.
--
-- VINTAGING
-- ---------
-- FRED revises some series; the "vintage" of a FRED observation is the
-- date FRED last touched it (alfred.stlouisfed.org tracks revisions in
-- detail). For our purposes -- macro rate series for affordability
-- contextualization -- the latest-revision-wins UPSERT is sufficient.
-- ============================================================================

CREATE TABLE raw.fred_observation (
    series_id        TEXT          NOT NULL CHECK (series_id ~ '^[A-Z0-9_]{2,40}$'),
    observation_date DATE          NOT NULL,
    value            NUMERIC(14,6),  -- NULL is meaningful (FRED uses '.' for missing)

    source_url       TEXT          NOT NULL,
    source_sha256    CHAR(64)      NOT NULL,
    ingested_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),

    PRIMARY KEY (series_id, observation_date)
);

COMMENT ON TABLE raw.fred_observation IS
    'Federal Reserve Economic Data observations. One row per (series, date). '
    'value is NULL when FRED publishes "." (missing observation).';

CREATE INDEX raw_fred_observation_date_idx ON raw.fred_observation (observation_date);


-- Annual averages for the canonical rate series, computed on the fly.
-- Restricted to series_ids the platform actively uses; adding a new
-- series implies a deliberate review (rate semantics differ).
CREATE VIEW derived.fred_annual AS
SELECT
    series_id,
    extract(year FROM observation_date)::SMALLINT AS year,
    round(avg(value), 4)                          AS annual_avg,
    count(value)                                  AS n_obs,
    min(observation_date)                         AS first_obs_date,
    max(observation_date)                         AS last_obs_date
FROM raw.fred_observation
WHERE value IS NOT NULL
  AND series_id IN ('MORTGAGE30US', 'DGS10', 'FEDFUNDS')
GROUP BY series_id, extract(year FROM observation_date);

COMMENT ON VIEW derived.fred_annual IS
    'Annual mean of canonical FRED rate series. Excludes nulls (missing '
    'observations are not zero). n_obs lets callers filter incomplete years.';
