-- ============================================================================
-- Migration: 073_disposable_income_aei
--
-- PHASE 3 of VISION_2026.md.
--
-- Stacks the next two named outputs from idea spec §1 onto the
-- Phase-1 tax engine (070) and the Phase-2 affordability engine (072):
--
--   idea §5.3  Disposable income trajectory
--              "Income - Taxes - Housing, CPI-adjusted to 2026"
--   idea §5.5  Affordability Erosion Index (AEI)
--              "HBR_2026 / HBR_anchor. How many times harder it is
--               to afford housing today vs the baseline year."
--
-- Plus the underlying Housing Burden Ratio (HBR, idea §5.1) that AEI
-- depends on, so a future audit can compare every county's HBR
-- against the spec definition byte-for-byte.
--
-- WHAT'S IN THIS MIGRATION
-- ------------------------
-- Five SQL surfaces, all stamped with formula version
-- '1.3.0-disposable-income-erosion-v1':
--
--   derived.f_disposable_income_annual
--       gross income MINUS federal+NJ+FICA tax MINUS PITI.
--       Composes f_household_taxes (070) + f_piti_annual (072).
--       NULL bubbles through if any input missing.
--
--   derived.f_household_burden_ratio
--       PITI(median_home, year, county) / median_household_income.
--       The spec's Housing Burden Ratio (HBR) per §5.1.
--       Uses the representative MFJ-1-1 household by default so it is
--       comparable to v_affordability_gap; takes a household profile
--       so the Phase-4 personalization engine can compute per-user.
--
--   derived.f_affordability_erosion_index
--       HBR(year) / HBR(anchor_year). The spec's AEI per §5.5.
--       Returns NULL when either HBR is unavailable. Anchor-year
--       discovery is left to the caller (typically the earliest year
--       for which the county has all four substrates).
--
--   derived.v_disposable_income_trajectory
--       Per (county, year) DI for the representative household, both
--       nominal and CPI-deflated to a configurable base year via the
--       existing derived.cpi_u_headline_annual deflator. The view
--       itself is NOMINAL; a parameterized function variant deflates
--       on the fly so the frontend can choose any base year.
--
--   derived.v_aei_by_county
--       Per county, current AEI vs the EARLIEST year where the
--       county has a non-NULL HBR. The anchor-year choice is
--       documented in the row so the UI can show "vs YYYY" without
--       hardcoding 1990 (which is unreachable until pre-2009 income
--       substrate is loaded). Substrate honesty: when the anchor
--       year has not enough seeded substrate, AEI is NULL and the
--       row labels the missing year explicitly.
--
-- DEPENDS ON:
--   * derived.f_household_taxes(...)         (migration 070)
--   * derived.f_piti_annual(...)             (migration 072)
--   * derived.f_county_avg_home_price(...)   (migration 072)
--   * derived.cpi_u_headline_annual          (migration 020)
--   * raw.acs_median_household_income        (migration 021)
--   * raw.nj_property_tax_county             (migration 025)
--
-- DESIGN NOTE: representative-household profile
-- ---------------------------------------------
-- Every per-county aggregate uses MFJ + 1 dependent + 1 qualifying
-- child as the canonical "median NJ owner-occupied household" per
-- ACS B11005. This is the SAME profile v_affordability_gap uses
-- (Phase 2). The personalization engine (Phase 4) overrides per
-- user-supplied profile.
-- ============================================================================

BEGIN;

INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '1.3.0-disposable-income-erosion-v1',
    'Phase 3 disposable-income + erosion-index engine: '
    'DI = gross - federal - NJ - FICA - PITI; '
    'HBR = PITI(median_home) / median_income; '
    'AEI = HBR(year) / HBR(anchor_year). '
    'Stacks on 1.2.0-affordability-engine-v1 (PITI + required-income) '
    'which stacks on 1.1.0-tax-engine-v1 (federal + NJ + FICA).',
    '2026-05-05',
    'Per VISION_2026.md Phase 3 / idea §5.3 + §5.5. CPI deflation '
    'available via derived.cpi_u_headline_annual; spec-mandated 2026 '
    'baseline available once BLS publishes 2026 annual M13 (~Dec '
    '2026). Anchor-year discovery for AEI is auto: earliest year '
    'with a non-NULL HBR for the given county.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ============================================================================
-- 1. derived.f_disposable_income_annual
--
-- The headline DI calculation per idea §5.3:
--
--   DI = gross - tax(gross, year, status, deps, kids) - PITI(home, year, county)
--
-- Composes the Phase-1 tax engine and the Phase-2 PITI engine. NULL
-- bubbles through every missing-input path -- substrate honesty.
--
-- Returns NOMINAL dollars. CPI deflation is the caller's
-- responsibility (see f_disposable_income_real below) so callers can
-- choose their own base year.
-- ============================================================================

CREATE OR REPLACE FUNCTION derived.f_disposable_income_annual(
    p_gross_income       NUMERIC,
    p_year               SMALLINT,
    p_county_fips        CHAR(5),
    p_filing_status      TEXT,
    p_dependents         INT,
    p_qualifying_children INT,
    p_home_price         NUMERIC
) RETURNS NUMERIC
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    -- gross - tax - PITI. Each component returns NULL when its
    -- substrate is incomplete; the outer expression bubbles NULL
    -- via standard SQL NULL semantics on subtraction.
    SELECT CASE
        WHEN p_gross_income IS NULL              THEN NULL
        WHEN p_home_price   IS NULL              THEN NULL
        ELSE
            p_gross_income
            - (
                SELECT total_tax
                FROM derived.f_household_taxes(
                    p_gross_income, p_gross_income, p_year, p_filing_status,
                    p_dependents, p_qualifying_children, 0::NUMERIC)
              )
            - derived.f_piti_annual(
                p_home_price, p_year, p_county_fips)
    END;
$$;

COMMENT ON FUNCTION derived.f_disposable_income_annual(NUMERIC, SMALLINT, CHAR, TEXT, INT, INT, NUMERIC) IS
    'Annual disposable income (idea §5.3): gross - federal - NJ - '
    'FICA - PITI. Returns NULL when any input substrate missing -- '
    'never silently substitutes zero for missing tax tables, never '
    'silently uses a different year''s rate. The Phase-4 '
    'personalization engine calls this with user-supplied (gross, '
    'status, deps, kids, home); per-county aggregates use the '
    'representative MFJ-1-1 profile.';


-- ============================================================================
-- 2. derived.f_disposable_income_real
--
-- CPI-deflated DI per idea §3.4 ("all values converted to 2026 real
-- dollars baseline"). Multiply nominal DI by CPI(base_year) /
-- CPI(value_year). Returns NULL when CPI for either year is missing.
--
-- We do NOT default base_year -- caller must specify. Why: the spec
-- says 2026 baseline, but BLS won't publish the 2026 annual average
-- until Dec 2026. Until then, a caller might want to use the
-- latest-available year (currently 2024) explicitly with a
-- "displayed in 2024 dollars" label. No silent fallback.
-- ============================================================================

CREATE OR REPLACE FUNCTION derived.f_disposable_income_real(
    p_gross_income        NUMERIC,
    p_year                SMALLINT,
    p_county_fips         CHAR(5),
    p_filing_status       TEXT,
    p_dependents          INT,
    p_qualifying_children INT,
    p_home_price          NUMERIC,
    p_base_year           SMALLINT
) RETURNS NUMERIC
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    -- nominal_DI * (CPI_base / CPI_year).
    -- Both CPI lookups must hit; either NULL bubbles to NULL.
    WITH
        nominal AS (
            SELECT derived.f_disposable_income_annual(
                p_gross_income, p_year, p_county_fips, p_filing_status,
                p_dependents, p_qualifying_children, p_home_price) AS v
        ),
        cpi_base AS (
            SELECT cpi_u_all_items AS v
            FROM derived.cpi_u_headline_annual
            WHERE year = p_base_year
        ),
        cpi_value AS (
            SELECT cpi_u_all_items AS v
            FROM derived.cpi_u_headline_annual
            WHERE year = p_year
        )
    SELECT CASE
        WHEN (SELECT v FROM nominal)   IS NULL THEN NULL
        WHEN (SELECT v FROM cpi_base)  IS NULL THEN NULL
        WHEN (SELECT v FROM cpi_value) IS NULL THEN NULL
        WHEN (SELECT v FROM cpi_value) = 0     THEN NULL  -- defensive
        ELSE round(
            (SELECT v FROM nominal)
            * (SELECT v FROM cpi_base)
            / (SELECT v FROM cpi_value), 2)
    END;
$$;

COMMENT ON FUNCTION derived.f_disposable_income_real(NUMERIC, SMALLINT, CHAR, TEXT, INT, INT, NUMERIC, SMALLINT) IS
    'CPI-deflated disposable income. Multiplies nominal DI by '
    'CPI(base_year)/CPI(value_year) using derived.cpi_u_headline_annual. '
    'Returns NULL when either CPI year is missing -- the spec-mandated '
    '2026 baseline (idea §3.4) returns NULL until BLS publishes the '
    '2026 M13 annual average. Caller picks the base year; we do not '
    'silently fall back.';


-- ============================================================================
-- 3. derived.f_household_burden_ratio (the spec's HBR per §5.1)
--
-- HBR(county, year) = PITI(median_home_price(county, year), year, county)
--                     / median_household_income(county, year)
--
-- The unitless ratio of housing cost to gross income. Used directly
-- by the AEI calculation below and exposed as a pure function so the
-- frontend can call it for any (county, year) without traversing the
-- v_affordability_gap view.
--
-- Substrate honesty: NULL when home price OR median income missing.
-- ============================================================================

CREATE OR REPLACE FUNCTION derived.f_household_burden_ratio(
    p_year                SMALLINT,
    p_county_fips         CHAR(5)
) RETURNS NUMERIC
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    WITH
        home AS (
            SELECT derived.f_county_avg_home_price(p_county_fips, p_year) AS v
        ),
        income AS (
            SELECT estimate AS v
            FROM raw.acs_median_household_income
            WHERE county_fips = p_county_fips
              AND year        = p_year
              AND product     = 'acs5'
              AND estimate   IS NOT NULL
        ),
        piti AS (
            SELECT derived.f_piti_annual(
                (SELECT v FROM home), p_year, p_county_fips) AS v
        )
    SELECT CASE
        WHEN (SELECT v FROM income) IS NULL OR (SELECT v FROM income) = 0 THEN NULL
        WHEN (SELECT v FROM piti)   IS NULL                                THEN NULL
        ELSE round(((SELECT v FROM piti) / (SELECT v FROM income))::NUMERIC, 6)
    END;
$$;

COMMENT ON FUNCTION derived.f_household_burden_ratio(SMALLINT, CHAR) IS
    'Spec §5.1 Housing Burden Ratio: annual PITI on the county-year '
    'median home divided by the county-year ACS5 median household '
    'income. Unitless. Inputs to the PITI calc default to assumption-'
    'table values (20% down, 30-yr term, 0.35% insurance). NULL when '
    'home price or median income missing; AEI propagates that NULL.';


-- ============================================================================
-- 4. derived.f_affordability_erosion_index (the spec's AEI per §5.5)
--
-- AEI(county, year, anchor) = HBR(county, year) / HBR(county, anchor)
--
-- "How many times harder it is to afford housing today vs the
-- baseline year." Spec calls for 1990 baseline. We can't reach 1990
-- without pre-2009 income substrate (Decennial 2000 / 1990 + careful
-- methodology labeling per idea §3.1) so anchor-year discovery is
-- delegated to the caller. Use the EARLIEST year for which the county
-- has a non-NULL HBR as the natural fallback.
--
-- AEI = 1.0 means today's burden equals the anchor year (no erosion).
-- AEI = 2.0 means housing is twice as burdensome today as at anchor.
-- AEI < 1.0 (rare today) means burden has actually decreased.
-- ============================================================================

CREATE OR REPLACE FUNCTION derived.f_affordability_erosion_index(
    p_county_fips  CHAR(5),
    p_year         SMALLINT,
    p_anchor_year  SMALLINT
) RETURNS NUMERIC
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    WITH
        hbr_now AS (
            SELECT derived.f_household_burden_ratio(p_year, p_county_fips) AS v
        ),
        hbr_anchor AS (
            SELECT derived.f_household_burden_ratio(p_anchor_year, p_county_fips) AS v
        )
    SELECT CASE
        WHEN (SELECT v FROM hbr_now)    IS NULL                THEN NULL
        WHEN (SELECT v FROM hbr_anchor) IS NULL                THEN NULL
        WHEN (SELECT v FROM hbr_anchor) = 0                    THEN NULL
        ELSE round(
            ((SELECT v FROM hbr_now) / (SELECT v FROM hbr_anchor))::NUMERIC, 4)
    END;
$$;

COMMENT ON FUNCTION derived.f_affordability_erosion_index(CHAR, SMALLINT, SMALLINT) IS
    'Spec §5.5 Affordability Erosion Index: HBR(year)/HBR(anchor_year). '
    '">1 = housing today is N× as burdensome as the anchor year." '
    'Spec asks for 1990 anchor; we use whatever the caller provides '
    '(typically the earliest year with non-NULL HBR for the county) '
    'because pre-2009 income substrate is not loaded. NULL bubbles '
    'through when either HBR is missing -- substrate honesty.';


-- ============================================================================
-- 5. derived.v_disposable_income_trajectory
--
-- Per (county, year) for the representative household, both nominal
-- and CPI-deflated to a "current dollars" baseline. The base year is
-- the LATEST CPI year available (operational reality given that 2026
-- CPI isn't published yet); the row labels which year was used so
-- the UI can be honest.
--
-- DI_real = DI_nominal * CPI(base_year) / CPI(year)
-- ============================================================================

CREATE OR REPLACE VIEW derived.v_disposable_income_trajectory AS
WITH owner_profile AS (
    SELECT
        'mfj'::TEXT AS filing_status,
        1::INT      AS dependents,
        1::INT      AS qualifying_children
),
-- Base year = the latest CPI year available. Single-row CTE so the
-- choice is consistent across every row in the view. Two-step CTE
-- to keep the SQL straightforward (a nested aggregate over a
-- correlated subquery is hard to read and brittle across PG versions).
base AS (
    SELECT
        b.base_year,
        c.cpi_u_all_items                                    AS base_cpi
    FROM (
        SELECT max(year)::SMALLINT AS base_year
        FROM derived.cpi_u_headline_annual
    ) b
    JOIN derived.cpi_u_headline_annual c ON c.year = b.base_year
),
src AS (
    SELECT
        p.county_fips,
        p.year::SMALLINT                                       AS year,
        derived.f_county_avg_home_price(p.county_fips, p.year::SMALLINT)
                                                               AS home_price,
        i.estimate                                             AS median_income_nominal
    FROM raw.nj_property_tax_county p
    LEFT JOIN raw.acs_median_household_income i
           ON i.county_fips = p.county_fips
          AND i.year        = p.year
          AND i.product     = 'acs5'
          AND i.estimate   IS NOT NULL
)
SELECT
    s.county_fips,
    s.year,
    s.home_price,
    s.median_income_nominal,
    derived.f_disposable_income_annual(
        s.median_income_nominal, s.year, s.county_fips,
        op.filing_status, op.dependents, op.qualifying_children,
        s.home_price)                                          AS di_nominal,
    -- Real DI in base-year dollars. f_disposable_income_real already
    -- bubbles NULL through every missing-substrate path (no gross,
    -- no home, no CPI for value-year, no CPI for base-year).
    derived.f_disposable_income_real(
        s.median_income_nominal, s.year, s.county_fips,
        op.filing_status, op.dependents, op.qualifying_children,
        s.home_price, b.base_year)                             AS di_real,
    b.base_year                                                AS real_dollars_base_year,
    op.filing_status                                           AS profile_filing_status,
    op.dependents                                              AS profile_dependents,
    op.qualifying_children                                     AS profile_qualifying_children,
    '1.3.0-disposable-income-erosion-v1'::TEXT                 AS formula_version
FROM src s
CROSS JOIN owner_profile op
CROSS JOIN base b;

COMMENT ON VIEW derived.v_disposable_income_trajectory IS
    'Per-(county, year) disposable income (idea §5.3). di_nominal '
    'is income-tax-PITI in current dollars; di_real deflates to '
    'real_dollars_base_year (the latest CPI year available -- '
    'currently 2024, will become 2026 once BLS publishes that '
    'year''s M13). Spec asks for 2026 baseline (idea §3.4); we use '
    'the latest available year and label it explicitly rather than '
    'silently substituting. Representative MFJ-1-1 household; '
    'personalization engine (Phase 4) computes per-user.';


-- ============================================================================
-- 6. derived.v_aei_by_county
--
-- Per county, current AEI vs the EARLIEST year for which the county
-- has a non-NULL HBR. The anchor-year choice is recorded in the row
-- so the UI can show "AEI 1.65× vs YYYY" without hardcoding 1990
-- (which is unreachable until pre-2009 income substrate lands).
-- ============================================================================

CREATE OR REPLACE VIEW derived.v_aei_by_county AS
WITH hbr_per_year AS (
    SELECT
        p.county_fips,
        p.year::SMALLINT                                       AS year,
        derived.f_household_burden_ratio(
            p.year::SMALLINT, p.county_fips)                   AS hbr
    FROM raw.nj_property_tax_county p
),
populated AS (
    SELECT *
    FROM hbr_per_year
    WHERE hbr IS NOT NULL
),
anchors AS (
    SELECT DISTINCT ON (county_fips)
        county_fips,
        year                                                   AS anchor_year,
        hbr                                                    AS anchor_hbr
    FROM populated
    ORDER BY county_fips, year ASC
),
latests AS (
    SELECT DISTINCT ON (county_fips)
        county_fips,
        year                                                   AS latest_year,
        hbr                                                    AS latest_hbr
    FROM populated
    ORDER BY county_fips, year DESC
)
SELECT
    a.county_fips,
    a.anchor_year,
    a.anchor_hbr,
    l.latest_year,
    l.latest_hbr,
    -- AEI = HBR(latest) / HBR(anchor). Per-row arithmetic; NULL bubbles
    -- automatically because populated CTE filters out the NULL HBRs.
    round((l.latest_hbr / NULLIF(a.anchor_hbr, 0))::NUMERIC, 4) AS aei,
    l.latest_year - a.anchor_year                              AS years_observed,
    '1.3.0-disposable-income-erosion-v1'::TEXT                 AS formula_version
FROM anchors a
JOIN latests l ON l.county_fips = a.county_fips
WHERE l.latest_year > a.anchor_year;  -- AEI vs itself is always 1.0; useless.

COMMENT ON VIEW derived.v_aei_by_county IS
    'Per-county Affordability Erosion Index (idea §5.5). Anchor year '
    'is the EARLIEST year for which the county has a non-NULL HBR; '
    'latest year is the freshest. AEI > 1 means housing today is '
    'more burdensome than at anchor. Spec asks for 1990 anchor; we '
    'auto-discover the earliest available year because pre-2009 '
    'income substrate is not loaded -- the row exposes anchor_year '
    'so the UI can show "vs YYYY" honestly. Counties with only one '
    'populated year are excluded (AEI vs itself is trivially 1.0).';


COMMIT;
