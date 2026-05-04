-- ============================================================================
-- Migration: 035_pums_puma_vintage
--
-- Add `puma_vintage` to raw.acs_pums_person / raw.acs_pums_housing.
--
-- WHY
-- ---
-- PUMA boundaries are revised after each decennial Census. The 2020
-- Census triggered a revision; ACS PUMS files starting from 2022 use
-- 2020-vintage PUMAs ("PUMA20"). 5-year ACS PUMS files spanning the
-- decennial boundary (e.g., 2018-2022) carry BOTH PUMA10 and PUMA20
-- columns and populate exactly one per row depending on the survey
-- year of that record:
--
--   2018-2019 records:  PUMA10 populated, PUMA20 = empty
--   2020-2022 records:  PUMA20 populated, PUMA10 = empty
--
-- The platform's PUMA->county crosswalk (`ref.puma2020_county_xwalk`)
-- is keyed on 2020-vintage PUMAs only. Loading 5-year PUMS without
-- distinguishing the two vintages would silently:
--
--   * Mis-join 2018-2019 records (their PUMA10 codes happen to overlap
--     numerically with PUMA20 codes from different geographies).
--   * Or, with an inner join, silently drop them with no audit trail.
--
-- Both are quiet correctness hazards. The fix: tag each raw row with
-- the vintage it was sampled under, and have the derived layer filter
-- on `puma_vintage = '2020'` until a `ref.puma2010_county_xwalk` exists.
--
-- DEFAULT VALUE
-- -------------
-- '2020' is the operationally correct default for any data ingested
-- against the modern (post-2022) Census PUMS substrate. The ingester
-- always emits an explicit value; the DEFAULT here is a safety net
-- for any backfill or hand-edit that bypasses the ingester. Existing
-- rows (zero in production at migration time) get '2020' applied.
--
-- DOWNSTREAM IMPACT
-- -----------------
-- * Indexes: PUMA-based queries should now include puma_vintage in the
--   filter to avoid mixing geographies. We do NOT modify the existing
--   (state_fips, puma, year) indexes; the planner will use them and
--   then filter on puma_vintage in-memory. If query patterns shift,
--   we can ALTER INDEX ... INCLUDE (puma_vintage) later.
-- * View `public.v_pums_nj_recent`: project the new column so
--   downstream consumers see vintage explicitly.
-- ============================================================================

ALTER TABLE raw.acs_pums_person
    ADD COLUMN IF NOT EXISTS puma_vintage CHAR(4) NOT NULL DEFAULT '2020'
        CHECK (puma_vintage IN ('2010', '2020', '2030'));

ALTER TABLE raw.acs_pums_housing
    ADD COLUMN IF NOT EXISTS puma_vintage CHAR(4) NOT NULL DEFAULT '2020'
        CHECK (puma_vintage IN ('2010', '2020', '2030'));

COMMENT ON COLUMN raw.acs_pums_person.puma_vintage IS
    'Decennial vintage of the puma column. ''2020'' = post-2020 Census '
    'boundaries (PUMA20 in upstream files). ''2010'' = pre-2020 '
    'boundaries (PUMA10). 5-Year ACS PUMS files spanning the boundary '
    'carry both vintages; ingester tags each row with the source '
    'column it came from.';

COMMENT ON COLUMN raw.acs_pums_housing.puma_vintage IS
    'See raw.acs_pums_person.puma_vintage.';

-- Rebuild the convenience view to project puma_vintage. Postgres won't
-- allow CREATE OR REPLACE to add a column to the SELECT list mid-stream,
-- so we DROP and recreate.
DROP VIEW IF EXISTS public.v_pums_nj_recent;
CREATE VIEW public.v_pums_nj_recent AS
WITH latest AS (
    SELECT MAX(year) AS year, product
    FROM raw.acs_pums_person
    WHERE state_fips = '34'
    GROUP BY product
)
SELECT
    p.year, p.product, p.puma, p.puma_vintage,
    p.serialno, p.sporder,
    p.agep, p.sex, p.rac1p, p.hisp, p.cit, p.nativity,
    p.schl, p.esr, p.wagp, p.pincp, p.pwgtp,
    h.ten, h.bdsp, h.rmsp,
    h.valp, h.grntp, h.smocp,
    h.hincp, h.wgtp
FROM raw.acs_pums_person p
JOIN latest USING (year, product)
LEFT JOIN raw.acs_pums_housing h
       ON h.year     = p.year
      AND h.product  = p.product
      AND h.serialno = p.serialno
WHERE p.state_fips = '34';

COMMENT ON VIEW public.v_pums_nj_recent IS
    'NJ-only most-recent-year ACS PUMS persons joined to housing. '
    'Always apply pwgtp weighting in analytical queries. Filter on '
    'puma_vintage = ''2020'' before joining ref.puma2020_county_xwalk.';
