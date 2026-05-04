-- ============================================================================
-- Migration: 010_hud_zip_county
--
-- HUD USPS ZIP <-> County crosswalk. The authoritative way to allocate
-- ZIP-keyed observations to county geographies when a ZIP straddles a county
-- boundary.
--
-- Source:
--   HUD USPS ZIP Code Crosswalk Files
--   https://www.huduser.gov/portal/datasets/usps_crosswalk.html
--   We track quarterly vintages (Q1, Q2, Q3, Q4) since they update each
--   quarter as USPS data is refreshed.
--
-- THE FOUR RATIOS -- WHICH ONE TO USE
-- -----------------------------------
-- Each (zip5, county_fips, vintage) row carries four ratios. They each sum
-- to 1.0 across counties for a given (zip, vintage) but they apportion
-- different address-type populations:
--
--   res_ratio  Share of *residential* addresses in this ZIP that fall in
--              this county. Use for population denominators, household-keyed
--              data (decennial counts, ACS person/household microdata).
--
--   bus_ratio  Share of *business* addresses in this ZIP that fall in this
--              county. Use for worksite-keyed data:
--                - DOL OFLC LCA worksites    (POP-2)
--                - HMDA loan property addr   (housing flow)
--                - SAM.gov entity locations  (FRAUD-F2)
--                - USAspending recipient ZIP (FRAUD-F1)
--              The default for ANY worksite/business-address allocation.
--
--   oth_ratio  PO Boxes and other non-residential, non-business address
--              points. Used for: nothing in v1. Retained for completeness.
--
--   tot_ratio  All address types combined. Used as a tie-breaker when the
--              three above are zero (which happens for very-low-population
--              ZIPs where the address counts are themselves suppressed).
--
-- The methodological rule the platform enforces: a worksite-keyed dataset
-- MUST allocate via bus_ratio, not via population-derived heuristics. The
-- distinction matters in NJ specifically because Newark, Jersey City, and
-- Edison contain large business districts whose ZIPs straddle municipal
-- boundaries; using res_ratio would systematically misattribute those jobs
-- to bedroom communities.
--
-- LICENSE
-- -------
-- HUD USPS Crosswalk: public domain. We attribute the source in
-- sources_manifest.toml [hud_zip_county] and in any derived publication.
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- ref.zip_county
--
-- One row per (zip5, county_fips, vintage_year, vintage_quarter). We
-- retain ALL vintages so a recompute of (e.g.) FY2020 LCA data uses the
-- FY2020 crosswalk, not the latest one. Reproducibility requires it.
--
-- WHY county_fips, NOT county_id
-- ------------------------------
-- HUD publishes a national crosswalk -- every US ZIP, every county. For
-- the row-sum invariant (bus_ratio sums to 1.0 across counties for each
-- (zip, vintage)) to hold, we MUST load the full national table.
-- Restricting to NJ-only would break the invariant for any NJ ZIP that
-- straddles into NY/PA. So this table:
--   * stores plain 5-digit FIPS strings, NOT FK-validated against
--     ref.county (which is intentionally a curated subset).
--   * is joined to ref.county at *consumption* time
--     (ref.zip_county.county_fips = ref.county.county_fips) -- giving us
--     correct national ratios with NJ-only downstream allocation.
-- ----------------------------------------------------------------------------
CREATE TABLE ref.zip_county (
    zip5             CHAR(5)       NOT NULL CHECK (zip5 ~ '^[0-9]{5}$'),
    county_fips      CHAR(5)       NOT NULL CHECK (county_fips ~ '^[0-9]{5}$'),
    vintage_year     SMALLINT      NOT NULL CHECK (vintage_year BETWEEN 2008 AND 2099),
    vintage_quarter  SMALLINT      NOT NULL CHECK (vintage_quarter BETWEEN 1 AND 4),

    res_ratio        NUMERIC(7,6)  NOT NULL CHECK (res_ratio BETWEEN 0 AND 1),
    bus_ratio        NUMERIC(7,6)  NOT NULL CHECK (bus_ratio BETWEEN 0 AND 1),
    oth_ratio        NUMERIC(7,6)  NOT NULL CHECK (oth_ratio BETWEEN 0 AND 1),
    tot_ratio        NUMERIC(7,6)  NOT NULL CHECK (tot_ratio BETWEEN 0 AND 1),

    -- Provenance
    source_url       TEXT          NOT NULL,
    source_sha256    CHAR(64)      NOT NULL,
    ingested_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),

    PRIMARY KEY (zip5, county_fips, vintage_year, vintage_quarter)
);

CREATE INDEX idx_zip_county_zip_vintage
    ON ref.zip_county (zip5, vintage_year, vintage_quarter);
CREATE INDEX idx_zip_county_county
    ON ref.zip_county (county_fips, vintage_year, vintage_quarter);

COMMENT ON TABLE ref.zip_county IS
    'HUD USPS ZIP <-> County crosswalk, retained per vintage so per-FY '
    'recomputes use the contemporaneous crosswalk, not the latest.';
COMMENT ON COLUMN ref.zip_county.bus_ratio IS
    'Share of *business* addresses in this ZIP that fall in this county. '
    'Default factor for ANY worksite/business-address allocation. Using '
    'res_ratio for worksite data systematically misattributes employment '
    'to bedroom communities -- see file header.';

-- ----------------------------------------------------------------------------
-- Per-vintage row-sum invariant
--
-- For any (zip5, vintage_year, vintage_quarter), the sum of bus_ratio
-- across counties should equal 1.0 (within rounding). This trigger checks
-- the invariant at COMMIT time and raises if it drifts more than 0.01.
--
-- We use a CONSTRAINT TRIGGER (DEFERRABLE INITIALLY DEFERRED) so bulk loads
-- can stage all rows for a vintage and only validate once per zip+vintage.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ref.check_zip_county_ratio_sums()
    RETURNS TRIGGER
    LANGUAGE plpgsql
AS $$
DECLARE
    bad_row RECORD;
BEGIN
    FOR bad_row IN
        SELECT zip5, vintage_year, vintage_quarter,
               sum(bus_ratio) AS sum_bus,
               sum(res_ratio) AS sum_res,
               sum(tot_ratio) AS sum_tot
        FROM ref.zip_county
        GROUP BY zip5, vintage_year, vintage_quarter
        HAVING abs(sum(bus_ratio) - 1.0) > 0.01
            OR abs(sum(res_ratio) - 1.0) > 0.01
            OR abs(sum(tot_ratio) - 1.0) > 0.01
    LOOP
        RAISE EXCEPTION
            'ZIP % vintage %Q% ratio sums out of tolerance: '
            'bus=%, res=%, tot= % (each must be 1.0 ± 0.01)',
            bad_row.zip5, bad_row.vintage_year, bad_row.vintage_quarter,
            bad_row.sum_bus, bad_row.sum_res, bad_row.sum_tot;
    END LOOP;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER zip_county_ratio_sum_check
    AFTER INSERT OR UPDATE ON ref.zip_county
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION ref.check_zip_county_ratio_sums();

COMMENT ON FUNCTION ref.check_zip_county_ratio_sums IS
    'Asserts at COMMIT that bus_ratio/res_ratio/tot_ratio each sum to 1.0 '
    '(±0.01) across counties for every (zip5, vintage). Catches partial '
    'loads where a ZIP straddles counties but only one row was inserted.';

-- ----------------------------------------------------------------------------
-- Helper view: latest vintage per ZIP
--
-- For interactive analytics that does not care about per-FY reproducibility.
-- Pipelines should NEVER use this view -- they should bind a specific
-- (vintage_year, vintage_quarter) for every computation.
-- ----------------------------------------------------------------------------
CREATE VIEW public.v_zip_county_latest AS
SELECT DISTINCT ON (zip5, county_fips) *
FROM ref.zip_county
ORDER BY zip5, county_fips, vintage_year DESC, vintage_quarter DESC;

COMMENT ON VIEW public.v_zip_county_latest IS
    'Most recent vintage per (zip5, county_fips). For ad-hoc analytics only. '
    'Pipelines must bind a specific vintage for reproducibility.';

-- ----------------------------------------------------------------------------
-- Helper view: ref.zip_county joined to ref.county
--
-- The aggregator-friendly view that returns NJ-only allocations
-- (or, more generally, only the counties that ref.county knows about).
-- Pipelines can use this directly and never have to write the JOIN
-- predicate themselves.
-- ----------------------------------------------------------------------------
CREATE VIEW ref.v_zip_known_counties AS
SELECT
    zc.zip5,
    zc.county_fips,
    c.county_id,
    c.state_code,
    zc.vintage_year,
    zc.vintage_quarter,
    zc.res_ratio,
    zc.bus_ratio,
    zc.oth_ratio,
    zc.tot_ratio
FROM ref.zip_county zc
JOIN ref.county c ON c.county_fips = zc.county_fips;

COMMENT ON VIEW ref.v_zip_known_counties IS
    'Inner-joined ref.zip_county x ref.county. Returns only allocations '
    'whose county_fips appears in ref.county (i.e. NJ-only in v1). '
    'Aggregators should JOIN against this view rather than the underlying '
    'tables, so curated-county changes propagate automatically.';

COMMIT;
