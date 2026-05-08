-- ============================================================================
-- Migration: 072_piti_required_income_gap
--
-- PHASE 2 of VISION_2026.md.
--
-- The composite affordability layer the spec actually asked for:
--
--   idea §5.1  HBR (Housing Burden Ratio) using mortgage-equivalent PITI
--              instead of HPI, so the rate environment is honored.
--   idea §5.4  Required income at 30% threshold  -- "your headline
--              collapse metric".
--   idea §7.3  The Collapse Curve -- chart of (county, year) showing
--              actual median income vs required income vs gap.
--
-- Five SQL surfaces, each provenance-tracked:
--
--   derived.f_mortgage_pi_monthly        pure amortization formula
--   derived.f_fred_30yr_annual_rate      reads derived.fred_annual
--   derived.f_county_property_tax_rate   reads raw.nj_property_tax_county
--   derived.f_county_avg_home_price      reads raw.nj_property_tax_county
--   derived.f_piti_annual                composite (P&I + property + insurance)
--   derived.f_required_income_at_threshold  bisection solver (PL/pgSQL)
--   derived.v_affordability_gap          per-(county, year) view
--
-- DEPENDS ON:
--   * raw.fred_observation             (migration 024)
--   * raw.nj_property_tax_county       (migration 025)
--   * raw.acs_median_household_income  (migration 021)
--   * derived.f_household_taxes(...)   (migration 070)
--   * ref.affordability_assumptions    (migration 071)
--
-- FORMULA VERSION
-- ---------------
-- The composite functions stamp '1.2.0-affordability-engine-v1'. Bumps
-- when (a) the PITI formula changes, (b) the required-income solver
-- algorithm changes, or (c) the assumed inputs change in a way that
-- moves the headline numbers.
-- ============================================================================

BEGIN;


-- ----------------------------------------------------------------------------
-- ref.formula_version: register Phase 2 engine.
-- ----------------------------------------------------------------------------
INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '1.2.0-affordability-engine-v1',
    'Phase 2 affordability engine: PITI = mortgage P&I + property tax '
    '+ homeowners insurance; required-income solved by bisection at '
    'the HUD 30% threshold; affordability gap = required - actual. '
    'Stacks on 1.1.0-tax-engine-v1 (federal + NJ + FICA). Does NOT '
    'model: PMI for low-down-payment loans, HOA/condo fees, mortgage '
    'interest deduction in itemized scenarios, refundable EITC/ACTC.',
    '2026-05-05',
    'Per VISION_2026.md Phase 2. Required-income converges to $0.01 '
    'in <=30 bisection iterations for any (PITI, year, status) where '
    'a finite solution exists. Returns NULL when affordability is '
    'unreachable at any income (max marginal tax > threshold).'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ============================================================================
-- 1. derived.f_mortgage_pi_monthly
--
-- The standard amortization formula for a fixed-rate fully-amortizing
-- mortgage:
--     M = P * r * (1+r)^n / ((1+r)^n - 1)
-- where:
--   M = monthly payment (principal + interest)
--   P = principal (loan amount, = home_price * (1 - down_pct))
--   r = monthly interest rate = annual_rate / 12
--   n = total payments = term_years * 12
--
-- Edge case: zero interest. The formula above is 0/0 at r=0. The
-- limit is M = P / n (interest-free amortization). Handle explicitly.
--
-- Pure SQL, STABLE PARALLEL SAFE. Inlinable.
-- ============================================================================

CREATE OR REPLACE FUNCTION derived.f_mortgage_pi_monthly(
    p_loan_amount        NUMERIC,
    p_annual_rate_decimal NUMERIC,  -- 0.0700 = 7%
    p_term_years         INTEGER
) RETURNS NUMERIC
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT CASE
        WHEN p_loan_amount IS NULL OR p_annual_rate_decimal IS NULL
             OR p_term_years IS NULL                THEN NULL
        WHEN p_loan_amount = 0                      THEN 0
        WHEN p_term_years <= 0                      THEN NULL
        WHEN p_annual_rate_decimal < 0              THEN NULL
        WHEN p_annual_rate_decimal = 0
             THEN p_loan_amount / (p_term_years * 12)
        ELSE
            p_loan_amount
            * (p_annual_rate_decimal / 12)
            * power(1 + p_annual_rate_decimal / 12, p_term_years * 12)
            / (power(1 + p_annual_rate_decimal / 12, p_term_years * 12) - 1)
    END;
$$;

COMMENT ON FUNCTION derived.f_mortgage_pi_monthly(NUMERIC, NUMERIC, INTEGER) IS
    'Standard fully-amortizing fixed-rate mortgage monthly P&I '
    'payment. Returns NULL on NULL inputs, negative rate, or '
    'non-positive term. Zero rate handled as principal/n. IMMUTABLE '
    '(no I/O).';


-- ============================================================================
-- 2. derived.f_fred_30yr_annual_rate
--
-- Annual mean of the Freddie Mac 30-yr fixed mortgage rate (FRED
-- series MORTGAGE30US). Reads derived.fred_annual.
--
-- Returns the rate as a DECIMAL fraction (e.g. 0.06875 for 6.875%),
-- not as a percent (FRED reports as percent; we divide by 100).
-- This way every rate-using function in the platform talks decimals
-- and the unit confusion that has destroyed mortgage calculators
-- since the 1980s is impossible here.
--
-- NULL if no observations for the year. Substrate honesty.
-- ============================================================================

CREATE OR REPLACE FUNCTION derived.f_fred_30yr_annual_rate(
    p_year SMALLINT
) RETURNS NUMERIC
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    -- FRED MORTGAGE30US is reported as percent (e.g. 7.04 = 7.04%);
    -- divide by 100 to return the decimal form every other rate
    -- function expects.
    SELECT round(annual_avg / 100, 6)
    FROM derived.fred_annual
    WHERE series_id = 'MORTGAGE30US'
      AND year = p_year
      AND n_obs >= 1;
$$;

COMMENT ON FUNCTION derived.f_fred_30yr_annual_rate(SMALLINT) IS
    'Annual mean Freddie Mac 30-yr fixed mortgage rate as a decimal '
    'fraction (0.07 = 7%). NULL if no MORTGAGE30US observations for '
    'the year. Reads derived.fred_annual; the underlying CSV is '
    'loaded by nj-ingest-fred.';


-- ============================================================================
-- 3. derived.f_county_property_tax_rate
--
-- Effective property-tax rate for (county, year) as a decimal
-- fraction. Reads raw.nj_property_tax_county.cy_total_rate and
-- divides by 100 (DCA reports as percent, e.g. 2.85 = 2.85%).
--
-- Returns NULL if not loaded.
-- ============================================================================

CREATE OR REPLACE FUNCTION derived.f_county_property_tax_rate(
    p_county_fips CHAR(5),
    p_year        SMALLINT
) RETURNS NUMERIC
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    SELECT round(cy_total_rate / 100, 6)
    FROM raw.nj_property_tax_county
    WHERE county_fips = p_county_fips
      AND year        = p_year;
$$;

COMMENT ON FUNCTION derived.f_county_property_tax_rate(CHAR, SMALLINT) IS
    'Effective property-tax rate for (county, year) as decimal '
    'fraction. NULL when DCA has not published yet for that year. '
    'Source: raw.nj_property_tax_county.cy_total_rate (NJ DCA).';


-- ============================================================================
-- 4. derived.f_county_avg_home_price
--
-- The DCA-published average residential property value for (county,
-- year). For Phase 2 V1 this is the home-price proxy used as the
-- denominator of the burden ratio and the input to PITI. Future
-- enhancement: ACS B25077 median home value (different statistic --
-- median vs mean -- to triangulate).
-- ============================================================================

CREATE OR REPLACE FUNCTION derived.f_county_avg_home_price(
    p_county_fips CHAR(5),
    p_year        SMALLINT
) RETURNS NUMERIC
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    SELECT avg_residential_value
    FROM raw.nj_property_tax_county
    WHERE county_fips = p_county_fips
      AND year        = p_year;
$$;

COMMENT ON FUNCTION derived.f_county_avg_home_price(CHAR, SMALLINT) IS
    'DCA-published average residential property value for (county, '
    'year). NULL when DCA has not published yet for that year. '
    'Future enhancement: cross-reference ACS B25077 median home '
    'value to detect mean-vs-median divergence (an inequality '
    'signal in itself).';


-- ============================================================================
-- 5. derived.f_piti_annual
--
-- The headline PITI calculation. Annual cost of owning a home of
-- price p_home_price in county p_county_fips in year p_year:
--
--   annual_p_and_i  = 12 * f_mortgage_pi_monthly(loan, rate, term)
--                     where loan = home_price * (1 - down_pct)
--                           rate = f_fred_30yr_annual_rate(year)
--   annual_taxes    = home_price * county_property_tax_rate
--   annual_insurance= home_price * insurance_rate
--   PITI_annual     = sum of the three
--
-- Down-payment, term, and insurance all default to the seeded
-- ref.affordability_assumptions values when NULL is passed; this
-- gives V1 callers a one-line invocation while still accepting
-- per-call overrides for the Phase 4 personalization engine.
--
-- NULL contract: any missing component (no FRED rate for year,
-- no DCA tax rate for county-year, no seeded insurance default)
-- bubbles NULL through. Caller surfaces "data unavailable".
-- ============================================================================

CREATE OR REPLACE FUNCTION derived.f_piti_annual(
    p_home_price     NUMERIC,
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
    -- Resolve the four assumptions: caller-provided value takes
    -- precedence; otherwise look up ref.affordability_assumptions.
    -- Mortgage rate also has an override so the personalization
    -- engine can compute counterfactuals ("what if rates dropped to
    -- 2021 lows?").
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

COMMENT ON FUNCTION derived.f_piti_annual(NUMERIC, SMALLINT, CHAR, NUMERIC, INTEGER, NUMERIC, NUMERIC) IS
    'Annual PITI (Principal + Interest + Taxes + Insurance) for a '
    'home of given price in given (county, year). NULLs bubble '
    'through if any required input is missing. Defaults pulled from '
    'ref.affordability_assumptions; per-call overrides supported '
    'for the Phase 4 personalization engine. p_rate_override lets '
    'callers run counterfactual rate scenarios.';


-- ============================================================================
-- 6a. derived.f_required_income_hud_30pct -- THE HEADLINE
--
-- The HUD cost-burden definition: a household is "cost-burdened"
-- when housing costs exceed 30% of gross income. So the income
-- required to pull housing burden down to exactly 30% is just:
--
--     required_income = PITI / 0.30
--
-- Linear, always defined (any non-negative PITI -> a finite
-- positive answer). This is the published, citable, comparable
-- metric. EVERY federal housing program (HUD CHAS, LIHTC, Section
-- 8) uses this definition. Use this as the headline; the
-- bisection-based "f_required_income_full_burden_30pct" below is
-- a stricter informational metric.
--
-- The threshold is configurable to support 50% (severe burden) and
-- the personalization engine, but defaults to the HUD 30%.
-- ============================================================================

CREATE OR REPLACE FUNCTION derived.f_required_income_hud_30pct(
    p_piti_annual NUMERIC,
    p_threshold   NUMERIC DEFAULT NULL
) RETURNS NUMERIC
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    SELECT CASE
        WHEN p_piti_annual IS NULL THEN NULL
        WHEN p_piti_annual < 0     THEN NULL
        ELSE p_piti_annual / COALESCE(
            p_threshold,
            ref.f_assumption_value('affordability_threshold_pct',
                                   extract(year FROM CURRENT_DATE)::SMALLINT))
    END;
$$;

COMMENT ON FUNCTION derived.f_required_income_hud_30pct(NUMERIC, NUMERIC) IS
    'HUD cost-burden definition: required income such that PITI <= '
    'threshold * gross. Linear, always defined. The headline for '
    'comparability across counties / years / households. Threshold '
    'defaults to ref.affordability_assumptions/affordability_'
    'threshold_pct (HUD 30%). Use 0.50 for severe-burden analysis. '
    'Source: HUD-PD&R Worst Case Housing Needs report methodology.';


-- ============================================================================
-- 6b. derived.f_required_income_post_tax_30pct
--
-- Lender-style definition: housing <= threshold of TAKE-HOME pay.
-- This is what mortgage underwriters effectively model when they
-- compute DTI. Solves:
--     PITI = threshold * (G - tax(G))
-- <=> G - tax(G) = PITI / threshold
--
-- Take-home (G - tax(G)) is monotone non-decreasing in G (you keep
-- at least zero of every additional dollar; in practice well above
-- zero), so this equation always has a solution. Bisects to find G.
--
-- Returns NULL if tax substrate is missing for (year, status).
-- ============================================================================

CREATE OR REPLACE FUNCTION derived.f_required_income_post_tax_30pct(
    p_piti_annual         NUMERIC,
    p_year                SMALLINT,
    p_filing_status       TEXT,
    p_dependents          INT,
    p_qualifying_children INT,
    p_threshold           NUMERIC DEFAULT NULL
) RETURNS NUMERIC
LANGUAGE plpgsql
STABLE
PARALLEL SAFE
AS $$
DECLARE
    v_threshold NUMERIC;
    v_target    NUMERIC;   -- required take-home = PITI / threshold
    v_lo        NUMERIC;
    v_hi        NUMERIC;
    v_mid       NUMERIC;
    v_tax       NUMERIC;
    v_take_home NUMERIC;
    v_iter      INT := 0;
    v_max_iter  CONSTANT INT := 30;
    v_tolerance CONSTANT NUMERIC := 0.01;
BEGIN
    IF p_piti_annual IS NULL OR p_piti_annual < 0 THEN
        RETURN NULL;
    END IF;

    v_threshold := COALESCE(p_threshold,
                   ref.f_assumption_value('affordability_threshold_pct', p_year));
    IF v_threshold IS NULL THEN
        RETURN NULL;
    END IF;

    v_target := p_piti_annual / v_threshold;

    -- Lower bound: zero-tax case, G = v_target. Real G is higher.
    v_lo := v_target;
    -- Upper bound: 5x overshoot. take_home grows roughly like
    -- 0.5*G even at top brackets, so 5*v_target is plenty.
    v_hi := v_target * 5;

    -- Make sure G_hi takes home enough.
    SELECT total_tax INTO v_tax
    FROM derived.f_household_taxes(
        v_hi, v_hi, p_year, p_filing_status,
        p_dependents, p_qualifying_children, 0::NUMERIC);
    IF v_tax IS NULL THEN
        RETURN NULL;
    END IF;
    v_take_home := v_hi - v_tax;

    -- If even G_hi doesn't yield enough take-home, expand once.
    IF v_take_home < v_target THEN
        v_hi := v_target * 50;
        SELECT total_tax INTO v_tax
        FROM derived.f_household_taxes(
            v_hi, v_hi, p_year, p_filing_status,
            p_dependents, p_qualifying_children, 0::NUMERIC);
        IF v_tax IS NULL OR v_hi - v_tax < v_target THEN
            -- Genuinely unreachable; substrate-honest NULL.
            RETURN NULL;
        END IF;
    END IF;

    -- Bisect on F(G) = (G - tax(G)) - v_target. F monotone non-decreasing.
    WHILE v_iter < v_max_iter AND (v_hi - v_lo) > v_tolerance LOOP
        v_mid := (v_lo + v_hi) / 2;

        SELECT total_tax INTO v_tax
        FROM derived.f_household_taxes(
            v_mid, v_mid, p_year, p_filing_status,
            p_dependents, p_qualifying_children, 0::NUMERIC);
        IF v_tax IS NULL THEN
            RETURN NULL;
        END IF;
        v_take_home := v_mid - v_tax;

        IF abs(v_take_home - v_target) <= v_tolerance THEN
            RETURN round(v_mid, 2);
        ELSIF v_take_home < v_target THEN
            v_lo := v_mid;
        ELSE
            v_hi := v_mid;
        END IF;
        v_iter := v_iter + 1;
    END LOOP;

    RETURN round((v_lo + v_hi) / 2, 2);
END;
$$;

COMMENT ON FUNCTION derived.f_required_income_post_tax_30pct(NUMERIC, SMALLINT, TEXT, INT, INT, NUMERIC) IS
    'Lender-style definition: gross income such that PITI <= '
    'threshold * (gross - taxes). Bisection on the monotone '
    'function take_home(G) - PITI/threshold. Always converges if '
    'tax substrate exists; returns NULL otherwise. Stricter than '
    'the HUD headline for high-tax households (you need MORE gross '
    'to leave 30% of net for housing).';


-- ============================================================================
-- 6c. derived.f_required_income_full_burden_30pct
--
-- The strict "tax+housing both fit in 30% of gross" definition the
-- VISION_2026 spec sketched but which is mathematically often
-- unreachable in NJ:
--     find G such that (PITI + tax(G)) / G = threshold
--
-- Tax is monotone non-decreasing in G but NOT linear (brackets).
-- The combined federal+NJ+FICA marginal rate can EXCEED 30% in
-- middle brackets, which means F(G) = G*threshold - PITI - tax(G)
-- can remain negative for ALL G when PITI is non-trivial.
--
-- That NULL is a meaningful signal: it says "no income makes this
-- home affordable under the strict (housing + tax both fit in 30%)
-- standard". For most NJ counties at current rates and home
-- prices, this metric returns NULL -- which IS the housing-cost
-- crisis rendered numerically.
--
-- ALGORITHM: bisection over [PITI/threshold, PITI/threshold * 10].
-- Returns NULL if F(G_hi) < 0 (unreachable), the bisected G otherwise.
-- ============================================================================

CREATE OR REPLACE FUNCTION derived.f_required_income_full_burden_30pct(
    p_home_price          NUMERIC,
    p_year                SMALLINT,
    p_county_fips         CHAR(5),
    p_filing_status       TEXT,
    p_dependents          INT,
    p_qualifying_children INT,
    p_threshold           NUMERIC DEFAULT NULL
) RETURNS NUMERIC
LANGUAGE plpgsql
STABLE
PARALLEL SAFE
AS $$
DECLARE
    v_piti        NUMERIC;
    v_threshold   NUMERIC;
    v_lo          NUMERIC;
    v_hi          NUMERIC;
    v_mid         NUMERIC;
    v_tax_lo      NUMERIC;
    v_tax_hi      NUMERIC;
    v_tax_mid     NUMERIC;
    v_f_lo        NUMERIC;
    v_f_hi        NUMERIC;
    v_f_mid       NUMERIC;
    v_iter        INT := 0;
    v_max_iter    CONSTANT INT := 30;
    v_tolerance   CONSTANT NUMERIC := 0.01;  -- one cent
BEGIN
    -- Resolve threshold default (HUD 30%)
    v_threshold := COALESCE(p_threshold,
                   ref.f_assumption_value('affordability_threshold_pct', p_year));
    IF v_threshold IS NULL THEN
        RETURN NULL;  -- substrate honesty: missing assumption
    END IF;

    -- Compute PITI once; if any input missing, bubble NULL.
    v_piti := derived.f_piti_annual(p_home_price, p_year, p_county_fips);
    IF v_piti IS NULL OR v_piti <= 0 THEN
        RETURN NULL;
    END IF;

    -- Lower bound: zero-tax answer is PITI / threshold (a real solution
    -- requires more income than this because taxes > 0 for any G > 0).
    v_lo := v_piti / v_threshold;

    -- Upper bound: 10x overshoot. For NJ PITI in the $30K-$50K range
    -- and threshold 0.30, lo is $100K-$170K and hi is $1M-$1.7M; the
    -- true required income is typically inside (lo, 2*lo).
    v_hi := v_lo * 10;

    -- F(G) = G * threshold - PITI - tax(G).
    -- F(G_lo) is the value at the zero-tax estimate; tax > 0 there
    -- so F(G_lo) < 0 (the candidate income is too low).
    SELECT total_tax INTO v_tax_lo
    FROM derived.f_household_taxes(
        v_lo, v_lo, p_year, p_filing_status,
        p_dependents, p_qualifying_children, 0::NUMERIC);

    -- If we cannot compute taxes at the lower bound, the substrate is
    -- not seeded for this (year, status). Bubble NULL.
    IF v_tax_lo IS NULL THEN
        RETURN NULL;
    END IF;

    v_f_lo := v_lo * v_threshold - v_piti - v_tax_lo;
    IF abs(v_f_lo) <= v_tolerance THEN
        RETURN round(v_lo, 2);
    END IF;

    -- F(G_hi)
    SELECT total_tax INTO v_tax_hi
    FROM derived.f_household_taxes(
        v_hi, v_hi, p_year, p_filing_status,
        p_dependents, p_qualifying_children, 0::NUMERIC);
    IF v_tax_hi IS NULL THEN
        RETURN NULL;
    END IF;

    v_f_hi := v_hi * v_threshold - v_piti - v_tax_hi;

    -- If the upper bound is still negative, no income makes housing
    -- 30% of gross. Substrate-honest signal: return NULL ("unreachable
    -- under current tax + housing cost regime"), do NOT silently
    -- expand the search and pretend we found an answer.
    IF v_f_hi < 0 THEN
        RETURN NULL;
    END IF;

    -- Bisect.
    WHILE v_iter < v_max_iter AND (v_hi - v_lo) > v_tolerance LOOP
        v_mid := (v_lo + v_hi) / 2;

        SELECT total_tax INTO v_tax_mid
        FROM derived.f_household_taxes(
            v_mid, v_mid, p_year, p_filing_status,
            p_dependents, p_qualifying_children, 0::NUMERIC);
        IF v_tax_mid IS NULL THEN
            RETURN NULL;
        END IF;

        v_f_mid := v_mid * v_threshold - v_piti - v_tax_mid;

        IF abs(v_f_mid) <= v_tolerance THEN
            RETURN round(v_mid, 2);
        ELSIF v_f_mid > 0 THEN
            -- mid-point gives surplus; the answer is below mid.
            v_hi := v_mid;
        ELSE
            v_lo := v_mid;
        END IF;
        v_iter := v_iter + 1;
    END LOOP;

    RETURN round((v_lo + v_hi) / 2, 2);
END;
$$;

COMMENT ON FUNCTION derived.f_required_income_full_burden_30pct(NUMERIC, SMALLINT, CHAR, TEXT, INT, INT, NUMERIC) IS
    'Strict full-burden definition: gross income such that PITI + '
    'tax(G) <= threshold * G. Bisects 30 iterations to <$0.01. '
    'Returns NULL when (a) tax data not seeded for (year, status), '
    '(b) PITI not computable (missing FRED / DCA / insurance), or '
    '(c) the threshold is unreachable at any income (the combined '
    'marginal tax exceeds the threshold). The (c) NULL is a '
    'meaningful signal: at current NJ tax rates + home prices, '
    'this metric returns NULL for most county/year cells -- a '
    'numeric rendering of the housing-cost crisis. Use the HUD-'
    'aligned f_required_income_hud_30pct(piti) as the comparable '
    'headline, this as the supplementary "stricter standard" view.';


-- ============================================================================
-- 7. derived.v_affordability_gap
--
-- Per-(county, year) view of the headline numbers, suitable for the
-- /housing pages and the Collapse Curve chart (idea spec §7.3).
--
-- One row per (county_fips, year) for which we have ALL of:
--   * DCA avg residential value (home_price proxy)
--   * DCA county property-tax rate
--   * FRED 30-yr rate annual mean (i.e. n_obs >= 1)
--   * ACS5 median household income
--   * ref.affordability_assumptions seed for the year
--   * IRS + NJ + FICA tax tables for the year
--
-- The required-income solver runs once per row. For 21 NJ counties
-- x ~15 years that is ~315 rows -- well below the threshold where
-- materializing the view as a TABLE buys anything; keep as a view
-- for V1 so it auto-updates with each refresh of underlying data.
--
-- Representative household: MFJ, 1 dependent, 1 qualifying child
-- (a rough approximation of the median NJ owner-occupied household
-- per ACS B11005). The personalization engine in Phase 4 lets the
-- user override these.
-- ============================================================================

CREATE OR REPLACE VIEW derived.v_affordability_gap AS
WITH owner_profile AS (
    SELECT
        'mfj'::TEXT AS filing_status,
        1::INT      AS dependents,
        1::INT      AS qualifying_children
),
src AS (
    SELECT
        p.county_fips,
        p.year::SMALLINT                                    AS year,
        derived.f_county_avg_home_price(p.county_fips, p.year::SMALLINT) AS home_price,
        i.estimate                                          AS median_income_nominal
    FROM raw.nj_property_tax_county p
    LEFT JOIN raw.acs_median_household_income i
           ON i.county_fips = p.county_fips
          AND i.year        = p.year
          AND i.product     = 'acs5'
          AND i.estimate IS NOT NULL
),
piti AS (
    SELECT s.*,
           derived.f_piti_annual(s.home_price, s.year, s.county_fips) AS piti_annual
    FROM src s
)
SELECT
    p.county_fips,
    p.year,
    p.home_price,
    p.median_income_nominal,
    p.piti_annual,

    -- Headline: HUD cost-burden definition. Linear, always defined
    -- when PITI is. This is the column the Collapse Curve plots.
    derived.f_required_income_hud_30pct(p.piti_annual)
                                                            AS required_income_hud_30pct,
    p.median_income_nominal -
        derived.f_required_income_hud_30pct(p.piti_annual)  AS hud_headroom_dollars,
    CASE WHEN derived.f_required_income_hud_30pct(p.piti_annual) > 0
         THEN round(
             derived.f_required_income_hud_30pct(p.piti_annual)
             / NULLIF(p.median_income_nominal, 0), 4)
    END                                                     AS hud_required_to_actual_ratio,

    -- Lender-style: housing <= 30% of take-home. Bisection,
    -- always converges (when tax substrate exists).
    derived.f_required_income_post_tax_30pct(
        p.piti_annual, p.year,
        op.filing_status, op.dependents, op.qualifying_children
    )                                                       AS required_income_post_tax_30pct,

    -- Strict: housing + tax <= 30% of gross. Often NULL for NJ.
    -- Surfaced as a "stricter standard reachability" signal.
    derived.f_required_income_full_burden_30pct(
        p.home_price, p.year, p.county_fips,
        op.filing_status, op.dependents, op.qualifying_children
    )                                                       AS required_income_full_burden_30pct,

    op.filing_status                                        AS profile_filing_status,
    op.dependents                                           AS profile_dependents,
    op.qualifying_children                                  AS profile_qualifying_children,
    '1.2.0-affordability-engine-v1'::TEXT                   AS formula_version
FROM piti p
CROSS JOIN owner_profile op;

COMMENT ON VIEW derived.v_affordability_gap IS
    E'Per-(county, year) Phase 2 headline numbers. Three required-'
    'income metrics in increasing strictness:'
    '\n  required_income_hud_30pct          -- HUD: PITI <= 30% of gross'
    '\n  required_income_post_tax_30pct     -- lender: PITI <= 30% of take-home'
    '\n  required_income_full_burden_30pct  -- strict: PITI + tax <= 30% of gross'
    '\nThe HUD metric is the comparable, citable headline; the '
    'others surface increasingly strict notions of "affordable". '
    'Representative household (MFJ + 1 dep + 1 child); '
    'personalization engine (Phase 4) computes per-user.';


COMMIT;
