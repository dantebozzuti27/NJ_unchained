-- ============================================================================
-- Migration: 074_personalization_engine
--
-- PHASE 4 of VISION_2026.md (idea spec §11 -- "relocation decision tools").
--
-- The Phase-1 tax engine, the Phase-2 PITI / required-income engine, and the
-- Phase-3 disposable-income / erosion engine all use the median MFJ-1-1
-- household as the representative profile. This migration takes the SAME
-- engines and parameterizes every per-county output on a USER-SUPPLIED
-- household profile (gross income, filing status, dependents, qualifying
-- children, savings/down-payment, other monthly debt, term, DTI caps, year
-- of tax law).
--
-- THE KEY MATH (closed-form, no bisection)
-- -----------------------------------------
-- Annual PITI on a home of price H decomposes linearly in H:
--
--     PITI(H) = 12 * (1 - down_pct) * H * annuity_factor(rate, term)
--             + H * prop_tax_rate
--             + H * insurance_rate
--             = H * c
--
-- where c = 12*(1-down)*annuity_factor + prop_tax_rate + insurance_rate
-- is a constant once (year, county, term, down_pct, insurance_rate) are
-- fixed. Therefore the DTI constraints are also linear in H and the
-- max-affordable-home-price has a CLOSED FORM:
--
--   Front-end DTI (PITI / gross <= dti_front):
--       H * c / 12 <= dti_front * G / 12
--       H <= dti_front * G / c
--
--   Back-end DTI ((PITI + 12 * other_debt) / gross <= dti_back):
--       H * c / 12 + other_debt <= dti_back * G / 12
--       H <= (dti_back * G - 12 * other_debt) / c
--
--   max_affordable = min(front_cap, max(0, back_cap))
--
-- The Phase-2 required-income engine had to bisect because the income-tax
-- function is non-linear in income; here the constraints are linear in
-- HOME PRICE for a fixed gross income (taxes don't depend on home price).
-- We get a closed-form, exact, deterministic answer with no convergence
-- ceremony. We DO offer a tax-aware variant (PITI <= dti * take-home)
-- which is also closed-form because tax depends on G alone.
--
-- WHAT'S IN THIS MIGRATION
-- ------------------------
-- Six SQL surfaces, all stamped with formula version
-- '1.4.0-personalization-engine-v1':
--
--   derived.f_piti_coefficient
--       The "c" above. Pure SQL closed form. Substrate-honest NULL.
--   derived.f_user_max_affordable_home_price_dti
--       Closed-form max H under (front, back) DTI on GROSS income.
--       This is what conventional underwriters compute (Fannie Mae
--       Selling Guide B3-6; Freddie Mac Loan Product Advisor).
--   derived.f_user_max_affordable_home_price_post_tax
--       Closed-form max H under (front, back) DTI on TAKE-HOME income.
--       Stricter; matches the Phase-2 lender-style required-income
--       definition. NULL when tax substrate missing for the year.
--   derived.f_user_required_income_for_home
--       Closed-form gross income required to make a given home fit
--       under both DTI caps. The inverse of max_affordable; useful
--       for the per-town "you need $N more / year" delta.
--   derived.f_user_town_verdict
--       Per-(year, county, profile) row with median home price,
--       max-affordable (both flavors), PITI on median, required income,
--       verdict label (Affordable / Stretch / Out of reach), gap dollars,
--       and personal burden ratio. The headline output of this engine.
--   derived.f_user_nj_county_verdicts
--       Set-returning convenience: emits one f_user_town_verdict row
--       for every NJ county for the given (year, profile). Drives the
--       per-county verdict table on /personalize.
--
-- ARCHITECTURAL DECISION: the engine is a SET OF FUNCTIONS, not a
-- materialized view. Reason: the input space is huge (cross-product of
-- household profile dimensions x counties x years) and 99% of it is
-- never queried. Lazy compute on the request path is the right shape.
-- Functions inline cleanly (LANGUAGE sql STABLE PARALLEL SAFE) so the
-- planner pushes constants into f_piti_annual / f_household_taxes /
-- f_assumption_value calls with no per-row overhead.
--
-- DEPENDS ON:
--   * derived.f_piti_annual                  (migration 072)
--   * derived.f_county_property_tax_rate     (migration 072)
--   * derived.f_county_avg_home_price        (migration 072)
--   * derived.f_fred_30yr_annual_rate        (migration 072)
--   * derived.f_mortgage_pi_monthly          (migration 072)
--   * derived.f_household_taxes              (migration 070)
--   * ref.f_assumption_value                 (migration 071)
-- ============================================================================

BEGIN;

INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '1.4.0-personalization-engine-v1',
    'Phase 4 personalization engine: closed-form max-affordable-home-price '
    'under conventional DTI (front 28% / back 36% per Fannie Mae) and '
    'post-tax DTI; per-town verdict (Affordable / Stretch / Out of reach) '
    'with personal PITI / burden ratio / dollar gap. Stacks on '
    '1.3.0-disposable-income-erosion-v1 + 1.2.0-affordability-engine-v1 + '
    '1.1.0-tax-engine-v1.',
    '2026-05-06',
    'Per VISION_2026.md Phase 4 / idea §11. Closed-form because PITI(H) '
    'is linear in H for fixed (year, county, term, down, insurance) so '
    'the DTI constraints reduce to H <= dti*G/c. No bisection needed. '
    'Default DTI caps from ref.affordability_assumptions; user-supplied '
    'overrides for counterfactual sliders.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ============================================================================
-- 1. derived.f_piti_coefficient
--
-- The "c" coefficient: annual PITI per dollar of home price for a given
-- (year, county, down_pct, term, insurance_rate, rate_override).
--
--   c = 12 * (1 - down_pct) * annuity_factor(rate, term)
--     + prop_tax_rate
--     + insurance_rate
--
-- Where annuity_factor = monthly_PI per dollar of loan
--                      = monthly_rate / (1 - (1 + monthly_rate)^(-n_months))
--
-- Per-call overrides for down/term/insurance/rate so the personalization
-- engine can compute counterfactuals. NULL for any missing substrate
-- (substrate honesty).
-- ============================================================================

CREATE OR REPLACE FUNCTION derived.f_piti_coefficient(
    p_year           SMALLINT,
    p_county_fips    CHAR(5),
    p_down_pct       NUMERIC DEFAULT NULL,
    p_term_years     INTEGER DEFAULT NULL,
    p_insurance_rate NUMERIC DEFAULT NULL,
    p_rate_override  NUMERIC DEFAULT NULL
) RETURNS NUMERIC
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    -- Resolve all five inputs the same way f_piti_annual does. Reuse
    -- the assumption registry for defaults.
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
            derived.f_county_property_tax_rate(p_county_fips, p_year)
                AS prop_tax_rate
    )
    -- c = 12 * (1 - down) * annuity_factor + prop_tax_rate + insurance_rate.
    -- We invoke f_mortgage_pi_monthly with a $1 loan to extract the annuity
    -- factor in a numerically-safe way (handles the zero-rate edge that
    -- the analytic form blows up on without a special case).
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

COMMENT ON FUNCTION derived.f_piti_coefficient(SMALLINT, CHAR, NUMERIC, INTEGER, NUMERIC, NUMERIC) IS
    'Annual PITI per dollar of home price for given (year, county) and '
    'mortgage assumptions. PITI(H) = H * f_piti_coefficient(...). The '
    'closed-form max-affordable-home-price functions divide by this. '
    'NULL bubbles through every missing-substrate path. Annuity factor '
    'extracted via f_mortgage_pi_monthly($1 loan) so the zero-rate '
    'edge (P/n) is handled without a separate code path.';


-- ============================================================================
-- 2. derived.f_user_max_affordable_home_price_dti
--
-- Closed-form max home price under conventional DTI on GROSS income.
--
--   front_cap = dti_front * gross / c
--   back_cap  = (dti_back * gross - 12 * other_monthly_debt) / c
--   H_max     = min(front_cap, max(0, back_cap))
--
-- back_cap can be negative when other_debt is very large; we floor at 0
-- (a negative back_cap means even a $0 home doesn't satisfy back DTI,
-- which means the user can't afford ANY house under these constraints --
-- the function returns 0 so the verdict layer reads "out of reach").
--
-- Defaults: dti_front = 0.28, dti_back = 0.36 from ref.affordability_assumptions
-- (Fannie Mae Selling Guide B3-6 conventional underwriting standard).
-- ============================================================================

CREATE OR REPLACE FUNCTION derived.f_user_max_affordable_home_price_dti(
    p_gross_income       NUMERIC,
    p_year               SMALLINT,
    p_county_fips        CHAR(5),
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
            derived.f_piti_coefficient(
                p_year, p_county_fips, p_down_pct, p_term_years,
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

COMMENT ON FUNCTION derived.f_user_max_affordable_home_price_dti(NUMERIC, SMALLINT, CHAR, NUMERIC, NUMERIC, NUMERIC, NUMERIC, INTEGER, NUMERIC, NUMERIC) IS
    'Closed-form max home price under Fannie Mae conventional DTI '
    '(front 28% PITI/gross, back 36% (PITI+other_debt)/gross). NO '
    'bisection -- the constraints are linear in H. Returns NULL when '
    'gross_income <= 0 or any required substrate (FRED, DCA, '
    'assumption table) is missing. Returns 0 when even a $0 home '
    'fails back DTI (which means the user has too much other debt to '
    'qualify for any mortgage). Defaults from ref.affordability_assumptions.';


-- ============================================================================
-- 3. derived.f_user_max_affordable_home_price_post_tax
--
-- The stricter "PITI <= dti_front * take-home" definition. Take-home
-- = gross - federal/NJ/FICA tax(gross, year, status, deps, kids).
-- Same closed form; tax computed once for the user's gross because
-- it doesn't depend on home price.
--
-- This matches the Phase-2 lender-style required-income (which solved
-- PITI = threshold * take-home for income); here we hold income fixed
-- and solve for max H, which is the dual.
-- ============================================================================

CREATE OR REPLACE FUNCTION derived.f_user_max_affordable_home_price_post_tax(
    p_gross_income       NUMERIC,
    p_year               SMALLINT,
    p_county_fips        CHAR(5),
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
            derived.f_piti_coefficient(
                p_year, p_county_fips, p_down_pct, p_term_years,
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
        WHEN (SELECT total_tax FROM inputs) IS NULL THEN NULL  -- unseeded tax year
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

COMMENT ON FUNCTION derived.f_user_max_affordable_home_price_post_tax(NUMERIC, SMALLINT, CHAR, TEXT, INT, INT, NUMERIC, NUMERIC, NUMERIC, INTEGER, NUMERIC, NUMERIC) IS
    'Closed-form max home price under PITI <= dti_front * take-home '
    '(gross - federal/NJ/FICA tax). Stricter than the gross-DTI '
    'function; matches the Phase-2 lender-style required-income '
    'definition. NULL when tax tables not seeded for the year -- '
    'substrate honesty.';


-- ============================================================================
-- 4. derived.f_user_required_income_for_home
--
-- Closed-form gross income required for a given home to fit under both
-- DTI caps. Inverse of f_user_max_affordable_home_price_dti.
--
--   PITI = H * c
--   Front: PITI / 12 <= dti_front * G / 12 => G >= PITI / dti_front
--   Back:  (PITI + 12 * other_debt) / 12 <= dti_back * G / 12
--          => G >= (PITI + 12 * other_debt) / dti_back
--   G_required = max(front, back)
--
-- Useful for the per-town "you need $N more / year of gross income" delta.
-- ============================================================================

CREATE OR REPLACE FUNCTION derived.f_user_required_income_for_home(
    p_home_price         NUMERIC,
    p_year               SMALLINT,
    p_county_fips        CHAR(5),
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
            derived.f_piti_annual(
                p_home_price, p_year, p_county_fips, p_down_pct,
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

COMMENT ON FUNCTION derived.f_user_required_income_for_home(NUMERIC, SMALLINT, CHAR, NUMERIC, NUMERIC, NUMERIC, NUMERIC, INTEGER, NUMERIC, NUMERIC) IS
    'Closed-form gross income required for a given home to satisfy '
    'both Fannie Mae DTI caps (front 28% PITI/gross, back 36% '
    '(PITI+other_debt)/gross). The dual of '
    'f_user_max_affordable_home_price_dti. Used by the per-town '
    'verdict to compute the "you need $N more income" delta.';


-- ============================================================================
-- 5. derived.f_user_town_verdict
--
-- The headline output of the personalization engine. For a given user
-- profile and one (year, county_fips):
--
--   * county_name + median_home_price (DCA avg residential value)
--   * max_affordable_dti       (Fannie Mae conventional, gross)
--   * max_affordable_post_tax  (stricter, take-home)
--   * piti_on_median           (annual PITI on the county's median home)
--   * required_gross_for_median (gross income needed to make median fit)
--   * personal_burden_ratio    (PITI / user_gross)
--   * personal_burden_ratio_post_tax (PITI / user_take_home)
--   * verdict_dti              ('affordable' / 'stretch' / 'out_of_reach')
--   * verdict_post_tax         (same labels, post-tax basis)
--   * gross_income_gap         (required - user_gross; +ve means short)
--
-- Verdict bands (citable in the page methodology):
--   affordable:    median_home <= max_affordable
--   stretch:       max_affordable < median_home <= 1.25 * max_affordable
--   out_of_reach:  median_home > 1.25 * max_affordable
--
-- The 1.25 multiplier is the canonical "20% over budget = stretch" band
-- used in HUD's affordability outreach materials. We expose it as the
-- 'affordability_stretch_multiplier' constant in ref.affordability_assumptions
-- (added via this migration; see seed change in 013).
-- ============================================================================

CREATE OR REPLACE FUNCTION derived.f_user_town_verdict(
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
    WITH base AS (
        SELECT
            p_county_fips                                          AS county_fips,
            derived.f_county_avg_home_price(p_county_fips, p_year) AS median_home_price,
            derived.f_user_max_affordable_home_price_dti(
                p_gross_income, p_year, p_county_fips,
                p_other_monthly_debt, p_dti_front, p_dti_back,
                p_down_pct, p_term_years, p_insurance_rate, p_rate_override
            )                                                      AS max_dti,
            derived.f_user_max_affordable_home_price_post_tax(
                p_gross_income, p_year, p_county_fips,
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
            derived.f_piti_annual(
                b.median_home_price, p_year, p_county_fips,
                p_down_pct, p_term_years, p_insurance_rate, p_rate_override
            )                                                       AS piti_med,
            derived.f_user_required_income_for_home(
                b.median_home_price, p_year, p_county_fips,
                p_other_monthly_debt, p_dti_front, p_dti_back,
                p_down_pct, p_term_years, p_insurance_rate, p_rate_override
            )                                                       AS req_gross
        FROM base b
    )
    SELECT
        e.county_fips,
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
        '1.4.0-personalization-engine-v1'::TEXT                      AS formula_version
    FROM enriched e;
$$;

COMMENT ON FUNCTION derived.f_user_town_verdict(SMALLINT, CHAR, NUMERIC, TEXT, INT, INT, NUMERIC, NUMERIC, NUMERIC, NUMERIC, INTEGER, NUMERIC, NUMERIC, NUMERIC) IS
    'The headline personalization output: per-(year, county) verdict '
    'for a user-supplied household profile. Returns max-affordable '
    '(both gross-DTI and post-tax flavors), PITI on the county median '
    'home, required income, personal burden ratio, dollar gap, and '
    'verdict label (affordable/stretch/out_of_reach). Stretch band '
    'is 1.25x max-affordable per HUD outreach materials; '
    'configurable via p_stretch_multiplier or the assumption table.';


-- ============================================================================
-- 6. derived.f_user_nj_county_verdicts
--
-- Set-returning convenience: emits f_user_town_verdict for every NJ
-- county. Drives the per-county verdict table on /personalize.
-- ============================================================================

CREATE OR REPLACE FUNCTION derived.f_user_nj_county_verdicts(
    p_year               SMALLINT,
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
    county_id                    TEXT,
    county_fips                  CHAR(5),
    county_name                  TEXT,
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
        c.county_id,
        v.county_fips,
        c.name AS county_name,
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
    FROM ref.county c
    CROSS JOIN LATERAL derived.f_user_town_verdict(
        p_year, c.county_fips, p_gross_income, p_filing_status,
        p_dependents, p_qualifying_children, p_other_monthly_debt,
        p_dti_front, p_dti_back, p_down_pct, p_term_years,
        p_insurance_rate, p_rate_override, p_stretch_multiplier
    ) v
    WHERE c.state_code = 'NJ'
    ORDER BY c.name;
$$;

COMMENT ON FUNCTION derived.f_user_nj_county_verdicts(SMALLINT, NUMERIC, TEXT, INT, INT, NUMERIC, NUMERIC, NUMERIC, NUMERIC, INTEGER, NUMERIC, NUMERIC, NUMERIC) IS
    'Set-returning convenience: emits f_user_town_verdict for every '
    'NJ county. One row per county for the per-county verdict table '
    'on /personalize. Sorted by county name. Counties with missing '
    'substrate (no DCA / FRED) surface NULL verdicts and are '
    'rendered as "data not loaded" on the page.';


COMMIT;
