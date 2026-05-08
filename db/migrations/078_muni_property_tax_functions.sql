-- ============================================================================
-- Migration: 078_muni_property_tax_functions
--
-- PHASE 8a of VISION_2026.md (idea spec §11). Companion to migration 077
-- (the muni substrate tables). This migration introduces the derived
-- functions that compose the muni-level affordability engine on top of
-- the unchanged county-level Phase 1-4 surfaces.
--
-- DESIGN: every function below is a STRICT EXTENSION of an existing
-- county-level function with the same shape. The rules are:
--
--   1. The same closed-form math and substrate-honesty contract apply.
--   2. The muni functions look up muni-level home price / tax rate from
--      raw.nj_property_tax_muni (NOT the county table). Everything else
--      (FRED rate, insurance default, mortgage term, federal/state tax
--      engine) is shared.
--   3. NULL bubbles when the muni has no DCA row for the requested year,
--      same as the county-level NULL contract.
--   4. The county-level functions are unchanged; this migration adds
--      sibling functions only.
--
-- WHY A SIBLING NAMESPACE INSTEAD OF AN OVERLOAD: the existing functions
-- have wide signatures (up to 14 parameters); adding an optional
-- muni_code argument would require dropping and recreating each one,
-- which would CASCADE through every dependent function and view (the
-- ARPA migration learned this lesson). Sibling _muni functions preserve
-- the existing API, keep all 1156 county-level tests green, and let the
-- frontend dispatch on whether the user picked a town.
--
-- FORMULA VERSION
-- ---------------
-- '1.5.0-municipality-drill-down-v1'. Bumps when (a) muni-level PITI
-- composition changes, (b) the muni verdict label rules change, or
-- (c) the muni-level rate / home-price source changes.
--
-- DEPENDS ON:
--   * raw.nj_property_tax_muni       (migration 077)
--   * ref.nj_municipality            (migration 077)
--   * derived.f_mortgage_pi_monthly  (migration 072)
--   * derived.f_fred_30yr_annual_rate(migration 072)
--   * derived.f_household_taxes      (migration 070)
--   * ref.f_assumption_value         (migration 071)
-- ============================================================================

BEGIN;

INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '1.5.0-municipality-drill-down-v1',
    'Phase 8a municipality drill-down: muni-level companions to the '
    'Phase-2 PITI / required-income engine and the Phase-4 personalization '
    'engine. Same closed-form math, swaps DCA county-average residential '
    'value + cy_total_rate for the muni-level equivalents from '
    'raw.nj_property_tax_muni. Stacks on 1.4.0-personalization-engine-v1 '
    '+ 1.3.0-disposable-income-erosion-v1 + 1.2.0-affordability-engine-v1 '
    '+ 1.1.0-tax-engine-v1.',
    '2026-05-08',
    'Per VISION_2026.md Phase 8 / idea §11. Sibling _muni functions '
    'instead of signature changes to preserve all 1156 existing tests. '
    'Substrate-honest NULL on every missing-substrate path.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ============================================================================
-- 1. derived.f_muni_avg_home_price -- per-muni median-home proxy
-- ============================================================================

CREATE OR REPLACE FUNCTION derived.f_muni_avg_home_price(
    p_muni_code CHAR(4),
    p_year      SMALLINT
) RETURNS NUMERIC
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    SELECT avg_residential_value
    FROM raw.nj_property_tax_muni
    WHERE muni_code = p_muni_code
      AND year      = p_year;
$$;

COMMENT ON FUNCTION derived.f_muni_avg_home_price(CHAR, SMALLINT) IS
    'DCA-published average residential property value for (muni, '
    'year). The muni-level analog of f_county_avg_home_price; same '
    'NULL-on-missing contract.';


-- ============================================================================
-- 2. derived.f_muni_property_tax_rate -- per-muni effective rate (decimal)
-- ============================================================================

CREATE OR REPLACE FUNCTION derived.f_muni_property_tax_rate(
    p_muni_code CHAR(4),
    p_year      SMALLINT
) RETURNS NUMERIC
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    -- DCA reports rates as percent (e.g. 2.85 = 2.85%); divide by 100
    -- to return decimal form, matching the rest of the platform.
    SELECT round(cy_total_rate / 100, 6)
    FROM raw.nj_property_tax_muni
    WHERE muni_code = p_muni_code
      AND year      = p_year;
$$;

COMMENT ON FUNCTION derived.f_muni_property_tax_rate(CHAR, SMALLINT) IS
    'Effective property-tax rate for (muni, year) as decimal '
    'fraction (0.0285 = 2.85%). NULL when DCA has not published yet.';


-- ============================================================================
-- 3. derived.f_piti_annual_muni -- composite PITI at muni granularity
--
-- Mirrors derived.f_piti_annual but sources prop_tax_rate from the muni
-- table. Mortgage rate (FRED), insurance default, down-pct, and term
-- come from the same shared substrate as the county-level function.
-- ============================================================================

CREATE OR REPLACE FUNCTION derived.f_piti_annual_muni(
    p_home_price     NUMERIC,
    p_year           SMALLINT,
    p_muni_code      CHAR(4),
    p_down_pct       NUMERIC DEFAULT NULL,
    p_term_years     INTEGER DEFAULT NULL,
    p_insurance_rate NUMERIC DEFAULT NULL,
    p_rate_override  NUMERIC DEFAULT NULL
) RETURNS NUMERIC
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    WITH inputs AS (
        SELECT
            COALESCE(p_down_pct,
                     ref.f_assumption_value('mortgage_default_down_pct', p_year))
                AS down_pct,
            COALESCE(p_term_years,
                     ref.f_assumption_value('mortgage_default_term_years', p_year)::INT)
                AS term_years,
            COALESCE(p_insurance_rate,
                     ref.f_assumption_value('homeowners_insurance_annual_rate_default', p_year))
                AS insurance_rate,
            COALESCE(p_rate_override,
                     derived.f_fred_30yr_annual_rate(p_year))
                AS mortgage_rate,
            derived.f_muni_property_tax_rate(p_muni_code, p_year)
                AS prop_tax_rate
    )
    SELECT
        CASE
            WHEN p_home_price IS NULL                            THEN NULL
            WHEN (SELECT down_pct       FROM inputs) IS NULL     THEN NULL
            WHEN (SELECT term_years     FROM inputs) IS NULL     THEN NULL
            WHEN (SELECT insurance_rate FROM inputs) IS NULL     THEN NULL
            WHEN (SELECT mortgage_rate  FROM inputs) IS NULL     THEN NULL
            WHEN (SELECT prop_tax_rate  FROM inputs) IS NULL     THEN NULL
            ELSE
                12 * derived.f_mortgage_pi_monthly(
                    p_home_price * (1 - (SELECT down_pct FROM inputs)),
                    (SELECT mortgage_rate FROM inputs),
                    (SELECT term_years    FROM inputs)
                )
                + p_home_price * (SELECT prop_tax_rate  FROM inputs)
                + p_home_price * (SELECT insurance_rate FROM inputs)
        END;
$$;

COMMENT ON FUNCTION derived.f_piti_annual_muni(NUMERIC, SMALLINT, CHAR, NUMERIC, INTEGER, NUMERIC, NUMERIC) IS
    'Annual PITI for a home of given price in given (muni, year). '
    'Mirror of f_piti_annual but with muni-level property-tax rate. '
    'Same NULL-bubble contract on every missing input.';


-- ============================================================================
-- 4. derived.f_piti_coefficient_muni -- closed-form PITI per dollar
-- ============================================================================

CREATE OR REPLACE FUNCTION derived.f_piti_coefficient_muni(
    p_year           SMALLINT,
    p_muni_code      CHAR(4),
    p_down_pct       NUMERIC DEFAULT NULL,
    p_term_years     INTEGER DEFAULT NULL,
    p_insurance_rate NUMERIC DEFAULT NULL,
    p_rate_override  NUMERIC DEFAULT NULL
) RETURNS NUMERIC
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    WITH inputs AS (
        SELECT
            COALESCE(p_down_pct,
                     ref.f_assumption_value('mortgage_default_down_pct', p_year))
                AS down_pct,
            COALESCE(p_term_years,
                     ref.f_assumption_value('mortgage_default_term_years', p_year)::INT)
                AS term_years,
            COALESCE(p_insurance_rate,
                     ref.f_assumption_value('homeowners_insurance_annual_rate_default', p_year))
                AS insurance_rate,
            COALESCE(p_rate_override,
                     derived.f_fred_30yr_annual_rate(p_year))
                AS mortgage_rate,
            derived.f_muni_property_tax_rate(p_muni_code, p_year)
                AS prop_tax_rate
    )
    SELECT CASE
        WHEN (SELECT down_pct       FROM inputs) IS NULL THEN NULL
        WHEN (SELECT term_years     FROM inputs) IS NULL THEN NULL
        WHEN (SELECT insurance_rate FROM inputs) IS NULL THEN NULL
        WHEN (SELECT mortgage_rate  FROM inputs) IS NULL THEN NULL
        WHEN (SELECT prop_tax_rate  FROM inputs) IS NULL THEN NULL
        ELSE
            12::NUMERIC
            * (1 - (SELECT down_pct FROM inputs))
            * derived.f_mortgage_pi_monthly(
                1::NUMERIC,
                (SELECT mortgage_rate FROM inputs),
                (SELECT term_years    FROM inputs))
            + (SELECT prop_tax_rate  FROM inputs)
            + (SELECT insurance_rate FROM inputs)
    END;
$$;

COMMENT ON FUNCTION derived.f_piti_coefficient_muni(SMALLINT, CHAR, NUMERIC, INTEGER, NUMERIC, NUMERIC) IS
    'Annual PITI per dollar of home price for given (muni, year). '
    'PITI(H) = H * f_piti_coefficient_muni(...). The closed-form '
    'max-affordable-home-price functions divide by this. NULL bubbles '
    'through every missing-substrate path.';


-- ============================================================================
-- 5. derived.f_user_max_affordable_home_price_dti_muni
-- ============================================================================

CREATE OR REPLACE FUNCTION derived.f_user_max_affordable_home_price_dti_muni(
    p_gross_income       NUMERIC,
    p_year               SMALLINT,
    p_muni_code          CHAR(4),
    p_other_monthly_debt NUMERIC DEFAULT 0,
    p_dti_front          NUMERIC DEFAULT NULL,
    p_dti_back           NUMERIC DEFAULT NULL,
    p_down_pct           NUMERIC DEFAULT NULL,
    p_term_years         INTEGER DEFAULT NULL,
    p_insurance_rate     NUMERIC DEFAULT NULL,
    p_rate_override      NUMERIC DEFAULT NULL
) RETURNS NUMERIC
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    WITH inputs AS (
        SELECT
            derived.f_piti_coefficient_muni(
                p_year, p_muni_code, p_down_pct, p_term_years,
                p_insurance_rate, p_rate_override) AS c,
            COALESCE(p_dti_front,
                     ref.f_assumption_value('dti_front_end_cap_conventional', p_year))
                AS dti_front,
            COALESCE(p_dti_back,
                     ref.f_assumption_value('dti_back_end_cap_conventional', p_year))
                AS dti_back,
            COALESCE(p_other_monthly_debt, 0::NUMERIC) AS other_debt
    )
    SELECT CASE
        WHEN p_gross_income            IS NULL OR p_gross_income <= 0 THEN NULL
        WHEN (SELECT c         FROM inputs) IS NULL OR (SELECT c FROM inputs) = 0 THEN NULL
        WHEN (SELECT dti_front FROM inputs) IS NULL THEN NULL
        WHEN (SELECT dti_back  FROM inputs) IS NULL THEN NULL
        ELSE GREATEST(
            0::NUMERIC,
            LEAST(
                round(
                    ((SELECT dti_front FROM inputs) * p_gross_income)
                    / (SELECT c FROM inputs), 2),
                round(
                    GREATEST(
                        0::NUMERIC,
                        ((SELECT dti_back FROM inputs) * p_gross_income)
                        - 12 * (SELECT other_debt FROM inputs))
                    / (SELECT c FROM inputs), 2)
            )
        )
    END;
$$;

COMMENT ON FUNCTION derived.f_user_max_affordable_home_price_dti_muni(NUMERIC, SMALLINT, CHAR, NUMERIC, NUMERIC, NUMERIC, NUMERIC, INTEGER, NUMERIC, NUMERIC) IS
    'Closed-form max home price under Fannie Mae conventional DTI '
    'for a household shopping in a specific NJ muni. Mirror of '
    'f_user_max_affordable_home_price_dti at muni granularity.';


-- ============================================================================
-- 6. derived.f_user_max_affordable_home_price_post_tax_muni
-- ============================================================================

CREATE OR REPLACE FUNCTION derived.f_user_max_affordable_home_price_post_tax_muni(
    p_gross_income       NUMERIC,
    p_year               SMALLINT,
    p_muni_code          CHAR(4),
    p_filing_status      TEXT,
    p_dependents         INT,
    p_qualifying_children INT,
    p_other_monthly_debt NUMERIC DEFAULT 0,
    p_dti_front          NUMERIC DEFAULT NULL,
    p_down_pct           NUMERIC DEFAULT NULL,
    p_term_years         INTEGER DEFAULT NULL,
    p_insurance_rate     NUMERIC DEFAULT NULL,
    p_rate_override      NUMERIC DEFAULT NULL
) RETURNS NUMERIC
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    WITH inputs AS (
        SELECT
            derived.f_piti_coefficient_muni(
                p_year, p_muni_code, p_down_pct, p_term_years,
                p_insurance_rate, p_rate_override) AS c,
            COALESCE(p_dti_front,
                     ref.f_assumption_value('dti_front_end_cap_conventional', p_year))
                AS dti_front,
            COALESCE(p_other_monthly_debt, 0::NUMERIC) AS other_debt,
            (SELECT total_tax
             FROM derived.f_household_taxes(
                 p_gross_income, p_gross_income, p_year, p_filing_status,
                 p_dependents, p_qualifying_children, 0::NUMERIC))
                AS total_tax
    )
    SELECT CASE
        WHEN p_gross_income            IS NULL OR p_gross_income <= 0 THEN NULL
        WHEN (SELECT c         FROM inputs) IS NULL OR (SELECT c FROM inputs) = 0 THEN NULL
        WHEN (SELECT dti_front FROM inputs) IS NULL THEN NULL
        WHEN (SELECT total_tax FROM inputs) IS NULL THEN NULL
        ELSE
            GREATEST(
                0::NUMERIC,
                round(
                    GREATEST(
                        0::NUMERIC,
                        ((SELECT dti_front FROM inputs)
                            * (p_gross_income - (SELECT total_tax FROM inputs)))
                        - 12 * (SELECT other_debt FROM inputs))
                    / (SELECT c FROM inputs), 2)
            )
    END;
$$;

COMMENT ON FUNCTION derived.f_user_max_affordable_home_price_post_tax_muni(NUMERIC, SMALLINT, CHAR, TEXT, INT, INT, NUMERIC, NUMERIC, NUMERIC, INTEGER, NUMERIC, NUMERIC) IS
    'Closed-form max home price under PITI <= dti_front * take-home '
    'for a household shopping in a specific NJ muni. Mirror of '
    'f_user_max_affordable_home_price_post_tax at muni granularity.';


-- ============================================================================
-- 7. derived.f_user_required_income_for_home_muni
-- ============================================================================

CREATE OR REPLACE FUNCTION derived.f_user_required_income_for_home_muni(
    p_home_price         NUMERIC,
    p_year               SMALLINT,
    p_muni_code          CHAR(4),
    p_other_monthly_debt NUMERIC DEFAULT 0,
    p_dti_front          NUMERIC DEFAULT NULL,
    p_dti_back           NUMERIC DEFAULT NULL,
    p_down_pct           NUMERIC DEFAULT NULL,
    p_term_years         INTEGER DEFAULT NULL,
    p_insurance_rate     NUMERIC DEFAULT NULL,
    p_rate_override      NUMERIC DEFAULT NULL
) RETURNS NUMERIC
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    WITH inputs AS (
        SELECT
            derived.f_piti_annual_muni(
                p_home_price, p_year, p_muni_code, p_down_pct,
                p_term_years, p_insurance_rate, p_rate_override) AS piti,
            COALESCE(p_dti_front,
                     ref.f_assumption_value('dti_front_end_cap_conventional', p_year))
                AS dti_front,
            COALESCE(p_dti_back,
                     ref.f_assumption_value('dti_back_end_cap_conventional', p_year))
                AS dti_back,
            COALESCE(p_other_monthly_debt, 0::NUMERIC) AS other_debt
    )
    SELECT CASE
        WHEN (SELECT piti      FROM inputs) IS NULL                     THEN NULL
        WHEN (SELECT dti_front FROM inputs) IS NULL OR (SELECT dti_front FROM inputs) = 0 THEN NULL
        WHEN (SELECT dti_back  FROM inputs) IS NULL OR (SELECT dti_back  FROM inputs) = 0 THEN NULL
        ELSE round(
            GREATEST(
                (SELECT piti FROM inputs) / (SELECT dti_front FROM inputs),
                ((SELECT piti FROM inputs) + 12 * (SELECT other_debt FROM inputs))
                    / (SELECT dti_back FROM inputs)
            ), 2)
    END;
$$;

COMMENT ON FUNCTION derived.f_user_required_income_for_home_muni(NUMERIC, SMALLINT, CHAR, NUMERIC, NUMERIC, NUMERIC, NUMERIC, INTEGER, NUMERIC, NUMERIC) IS
    'Closed-form gross income required for a given home in a specific '
    'muni to satisfy both DTI caps. Mirror of '
    'f_user_required_income_for_home at muni granularity.';


-- ============================================================================
-- 8. derived.f_user_town_verdict_muni -- headline per-muni verdict
-- ============================================================================

CREATE OR REPLACE FUNCTION derived.f_user_town_verdict_muni(
    p_year               SMALLINT,
    p_muni_code          CHAR(4),
    p_gross_income       NUMERIC,
    p_filing_status      TEXT,
    p_dependents         INT,
    p_qualifying_children INT,
    p_other_monthly_debt NUMERIC DEFAULT 0,
    p_dti_front          NUMERIC DEFAULT NULL,
    p_dti_back           NUMERIC DEFAULT NULL,
    p_down_pct           NUMERIC DEFAULT NULL,
    p_term_years         INTEGER DEFAULT NULL,
    p_insurance_rate     NUMERIC DEFAULT NULL,
    p_rate_override      NUMERIC DEFAULT NULL,
    p_stretch_multiplier NUMERIC DEFAULT NULL
) RETURNS TABLE (
    muni_code                    CHAR(4),
    median_home_price            NUMERIC,
    max_affordable_dti           NUMERIC,
    max_affordable_post_tax      NUMERIC,
    piti_on_median               NUMERIC,
    required_gross_for_median    NUMERIC,
    user_take_home               NUMERIC,
    personal_burden_ratio        NUMERIC,
    personal_burden_ratio_post_tax NUMERIC,
    verdict_dti                  TEXT,
    verdict_post_tax             TEXT,
    gross_income_gap             NUMERIC,
    formula_version              TEXT
)
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    WITH base AS (
        SELECT
            p_muni_code                                            AS muni_code,
            derived.f_muni_avg_home_price(p_muni_code, p_year)     AS median_home_price,
            derived.f_user_max_affordable_home_price_dti_muni(
                p_gross_income, p_year, p_muni_code,
                p_other_monthly_debt, p_dti_front, p_dti_back,
                p_down_pct, p_term_years, p_insurance_rate, p_rate_override
            )                                                      AS max_dti,
            derived.f_user_max_affordable_home_price_post_tax_muni(
                p_gross_income, p_year, p_muni_code,
                p_filing_status, p_dependents, p_qualifying_children,
                p_other_monthly_debt, p_dti_front,
                p_down_pct, p_term_years, p_insurance_rate, p_rate_override
            )                                                      AS max_post_tax,
            (SELECT total_tax
             FROM derived.f_household_taxes(
                 p_gross_income, p_gross_income, p_year, p_filing_status,
                 p_dependents, p_qualifying_children, 0::NUMERIC))  AS total_tax,
            COALESCE(p_stretch_multiplier,
                     ref.f_assumption_value('affordability_stretch_multiplier', p_year),
                     1.25)                                          AS stretch_mult
    ),
    enriched AS (
        SELECT
            b.*,
            derived.f_piti_annual_muni(
                b.median_home_price, p_year, p_muni_code,
                p_down_pct, p_term_years, p_insurance_rate, p_rate_override
            )                                                       AS piti_med,
            derived.f_user_required_income_for_home_muni(
                b.median_home_price, p_year, p_muni_code,
                p_other_monthly_debt, p_dti_front, p_dti_back,
                p_down_pct, p_term_years, p_insurance_rate, p_rate_override
            )                                                       AS req_gross
        FROM base b
    )
    SELECT
        e.muni_code,
        e.median_home_price,
        e.max_dti,
        e.max_post_tax,
        e.piti_med,
        e.req_gross,
        CASE WHEN e.total_tax IS NULL THEN NULL
             ELSE p_gross_income - e.total_tax
        END                                                          AS user_take_home,
        CASE WHEN e.piti_med IS NULL OR p_gross_income IS NULL OR p_gross_income = 0
             THEN NULL
             ELSE round(e.piti_med / p_gross_income, 4)
        END                                                          AS personal_burden_ratio,
        CASE WHEN e.piti_med IS NULL OR e.total_tax IS NULL OR (p_gross_income - e.total_tax) <= 0
             THEN NULL
             ELSE round(e.piti_med / (p_gross_income - e.total_tax), 4)
        END                                                          AS personal_burden_ratio_post_tax,
        CASE
            WHEN e.median_home_price IS NULL OR e.max_dti IS NULL THEN NULL
            WHEN e.median_home_price <= e.max_dti                 THEN 'affordable'
            WHEN e.median_home_price <= e.max_dti * e.stretch_mult THEN 'stretch'
            ELSE                                                       'out_of_reach'
        END                                                          AS verdict_dti,
        CASE
            WHEN e.median_home_price IS NULL OR e.max_post_tax IS NULL THEN NULL
            WHEN e.median_home_price <= e.max_post_tax                 THEN 'affordable'
            WHEN e.median_home_price <= e.max_post_tax * e.stretch_mult THEN 'stretch'
            ELSE                                                            'out_of_reach'
        END                                                          AS verdict_post_tax,
        CASE WHEN e.req_gross IS NULL OR p_gross_income IS NULL THEN NULL
             ELSE round(e.req_gross - p_gross_income, 2)
        END                                                          AS gross_income_gap,
        '1.5.0-municipality-drill-down-v1'::TEXT                     AS formula_version
    FROM enriched e;
$$;

COMMENT ON FUNCTION derived.f_user_town_verdict_muni(SMALLINT, CHAR, NUMERIC, TEXT, INT, INT, NUMERIC, NUMERIC, NUMERIC, NUMERIC, INTEGER, NUMERIC, NUMERIC, NUMERIC) IS
    'Per-(year, muni) personalization verdict for a user-supplied '
    'profile. Mirror of f_user_town_verdict at muni granularity. '
    'Returns max-affordable (gross-DTI + post-tax flavors), PITI on '
    'the muni median home, required income, personal burden ratio, '
    'verdict label, and dollar gap. Stamps formula version '
    '1.5.0-municipality-drill-down-v1.';


-- ============================================================================
-- 9. derived.f_user_nj_muni_verdicts -- set-returning per-county convenience
--
-- Emits one f_user_town_verdict_muni row for every muni in a given NJ
-- county. Drives the per-muni drill-down table on /personalize.
-- Scoped by county to keep result sets bounded (Bergen alone has 70
-- munis; emitting all 564 NJ munis would be a 564 x f_household_taxes
-- call which is gratuitous when the user has picked a county).
-- ============================================================================

CREATE OR REPLACE FUNCTION derived.f_user_nj_muni_verdicts(
    p_year               SMALLINT,
    p_county_fips        CHAR(5),
    p_gross_income       NUMERIC,
    p_filing_status      TEXT,
    p_dependents         INT,
    p_qualifying_children INT,
    p_other_monthly_debt NUMERIC DEFAULT 0,
    p_dti_front          NUMERIC DEFAULT NULL,
    p_dti_back           NUMERIC DEFAULT NULL,
    p_down_pct           NUMERIC DEFAULT NULL,
    p_term_years         INTEGER DEFAULT NULL,
    p_insurance_rate     NUMERIC DEFAULT NULL,
    p_rate_override      NUMERIC DEFAULT NULL,
    p_stretch_multiplier NUMERIC DEFAULT NULL
) RETURNS TABLE (
    muni_code                    CHAR(4),
    muni_name                    TEXT,
    county_fips                  CHAR(5),
    median_home_price            NUMERIC,
    max_affordable_dti           NUMERIC,
    max_affordable_post_tax      NUMERIC,
    piti_on_median               NUMERIC,
    required_gross_for_median    NUMERIC,
    user_take_home               NUMERIC,
    personal_burden_ratio        NUMERIC,
    personal_burden_ratio_post_tax NUMERIC,
    verdict_dti                  TEXT,
    verdict_post_tax             TEXT,
    gross_income_gap             NUMERIC,
    formula_version              TEXT
)
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    SELECT
        m.muni_code,
        m.muni_name,
        m.county_fips,
        v.median_home_price,
        v.max_affordable_dti,
        v.max_affordable_post_tax,
        v.piti_on_median,
        v.required_gross_for_median,
        v.user_take_home,
        v.personal_burden_ratio,
        v.personal_burden_ratio_post_tax,
        v.verdict_dti,
        v.verdict_post_tax,
        v.gross_income_gap,
        v.formula_version
    FROM ref.nj_municipality m
    CROSS JOIN LATERAL derived.f_user_town_verdict_muni(
        p_year, m.muni_code, p_gross_income, p_filing_status,
        p_dependents, p_qualifying_children, p_other_monthly_debt,
        p_dti_front, p_dti_back, p_down_pct, p_term_years,
        p_insurance_rate, p_rate_override, p_stretch_multiplier
    ) v
    WHERE m.county_fips = p_county_fips
    ORDER BY m.muni_name;
$$;

COMMENT ON FUNCTION derived.f_user_nj_muni_verdicts(SMALLINT, CHAR, NUMERIC, TEXT, INT, INT, NUMERIC, NUMERIC, NUMERIC, NUMERIC, INTEGER, NUMERIC, NUMERIC, NUMERIC) IS
    'Set-returning convenience: emits f_user_town_verdict_muni for '
    'every muni in a given NJ county. Scoped to one county to keep '
    'result-set sizes bounded (Bergen has 70 munis). Sorted by muni '
    'name. Munis with missing DCA substrate surface NULL verdicts.';


-- ============================================================================
-- 10. derived.v_muni_affordability_gap -- per-muni headline view
--
-- Mirror of derived.v_affordability_gap at muni granularity. Median
-- household income is reported at the COUNTY level (ACS does not
-- publish reliable county-subdivision income for all NJ munis), so
-- we join muni -> county_fips -> ACS5 median household income; the
-- resulting "headroom dollars" is muni_required_income - county_median
-- which is honest about its denominator. Future MOD-IV ingestion can
-- introduce muni-level home prices that diverge from the muni-DCA
-- avg_residential_value.
-- ============================================================================

CREATE OR REPLACE VIEW derived.v_muni_affordability_gap AS
WITH owner_profile AS (
    SELECT
        'mfj'::TEXT AS filing_status,
        1::INT      AS dependents,
        1::INT      AS qualifying_children
),
src AS (
    SELECT
        p.muni_code,
        m.county_fips,
        m.muni_name,
        p.year::SMALLINT                                      AS year,
        derived.f_muni_avg_home_price(p.muni_code, p.year::SMALLINT) AS home_price,
        i.estimate                                            AS county_median_income_nominal
    FROM raw.nj_property_tax_muni p
    JOIN ref.nj_municipality m ON m.muni_code = p.muni_code
    LEFT JOIN raw.acs_median_household_income i
           ON i.county_fips = m.county_fips
          AND i.year        = p.year
          AND i.product     = 'acs5'
          AND i.estimate IS NOT NULL
),
piti AS (
    SELECT s.*,
           derived.f_piti_annual_muni(s.home_price, s.year, s.muni_code) AS piti_annual
    FROM src s
)
SELECT
    p.muni_code,
    p.muni_name,
    p.county_fips,
    p.year,
    p.home_price,
    p.county_median_income_nominal,
    p.piti_annual,

    derived.f_required_income_hud_30pct(p.piti_annual)
                                                            AS required_income_hud_30pct,
    p.county_median_income_nominal -
        derived.f_required_income_hud_30pct(p.piti_annual)  AS hud_headroom_dollars,
    CASE WHEN derived.f_required_income_hud_30pct(p.piti_annual) > 0
         THEN round(
             derived.f_required_income_hud_30pct(p.piti_annual)
             / NULLIF(p.county_median_income_nominal, 0), 4)
    END                                                     AS hud_required_to_actual_ratio,

    derived.f_required_income_post_tax_30pct(
        p.piti_annual, p.year,
        op.filing_status, op.dependents, op.qualifying_children
    )                                                       AS required_income_post_tax_30pct,

    op.filing_status                                        AS profile_filing_status,
    op.dependents                                           AS profile_dependents,
    op.qualifying_children                                  AS profile_qualifying_children,
    '1.5.0-municipality-drill-down-v1'::TEXT                AS formula_version
FROM piti p
CROSS JOIN owner_profile op;

COMMENT ON VIEW derived.v_muni_affordability_gap IS
    'Per-(muni, year) Phase 8a headline numbers. Same shape as '
    'v_affordability_gap at county granularity. Median income '
    'denominator is COUNTY-level ACS (NJ muni-level income data is '
    'not consistently published). The strict full-burden metric is '
    'omitted at this level to keep the view fast for 564-muni '
    'cross-products; callers needing it can call '
    'f_required_income_full_burden_30pct directly with the muni '
    'home price and county FIPS.';


COMMIT;
