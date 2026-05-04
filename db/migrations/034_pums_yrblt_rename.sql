-- ============================================================================
-- Migration: 034_pums_yrblt_rename
--
-- Rename `raw.acs_pums_housing.ybl` -> `yrblt`, change semantics + check.
--
-- WHY
-- ---
-- Migration 026 originally projected the housing variable `YBL`, which in
-- pre-2019 ACS PUMS was a 1-22 code mapping to a year-built bin
-- (1=1939_or_earlier, 2=1940-1949, ..., 22=2014_or_later). Starting with
-- the 2019 vintage, the Census Bureau replaced YBL with `YRBLT`, which
-- stores a 4-digit year value drawn from a fixed bin set:
--
--      1939, 1940, 1950, 1960, 1970, 1980, 1990, 2000, 2010,
--      2020, 2021, 2022                       (additional years per vintage)
--
-- 1939 is the "1939 or earlier" bin; 2020+ is "exact year" (granular at
-- year boundaries because PUMS no longer top-codes the recent past).
--
-- We never staged any YBL data into the platform (the raw table was empty
-- when this migration ran, confirmed end-to-end during the live-ingest
-- bring-up of 2022). Therefore we DROP the old column and ADD a new one
-- rather than ALTER + UPDATE: there is no data to preserve, and the
-- value semantics are different enough that any artifact in flight
-- should be considered corrupt rather than translated.
--
-- INVARIANTS
-- ----------
-- * `yrblt` is NULL when not collected; otherwise BETWEEN 1939 AND
--   <future_max>. We bound at 2099 to allow several decades of forward
--   coverage without future migrations.
-- * The column rename does NOT change the table primary key or any
--   foreign keys (no FK references the column).
-- * Any future ingester run for 2017-2018 vintages MUST translate YBL
--   bin codes -> YRBLT bin midpoints before staging. (We don't currently
--   ingest those vintages; document the requirement and short-circuit.)
--
-- DOWNSTREAM IMPACT
-- -----------------
-- * `derived.*` tables: none consume ybl/yrblt today.
-- * `public.v_pums_nj_recent`: does NOT project ybl/yrblt currently
--   (verified at migration time), so no view rebuild needed.
-- * Ingester `ingestion/census_acs_pums.py`: HOUSING_VARS, column
--   rename map, and dest-column tuple all updated in the same change-set.
-- ============================================================================

ALTER TABLE raw.acs_pums_housing
    DROP COLUMN IF EXISTS ybl;

ALTER TABLE raw.acs_pums_housing
    ADD COLUMN IF NOT EXISTS yrblt SMALLINT
        CHECK (yrblt IS NULL OR yrblt BETWEEN 1939 AND 2099);

COMMENT ON COLUMN raw.acs_pums_housing.yrblt IS
    'Year structure first built. ACS PUMS 2019+ encoding: 4-digit year at '
    'bin start. 1939 = "1939 or earlier"; 2020+ = each individual year. '
    'Replaced legacy YBL (1-22 code). NULL when not collected.';
