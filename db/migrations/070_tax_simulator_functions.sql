-- ============================================================================
-- Migration: 070_tax_simulator_functions
--
-- PHASE 1 of VISION_2026.md (idea spec §3.3, §5.2, §5.3, §5.4).
--
-- The deterministic tax engine. Six SQL functions that, given a
-- household profile and a tax year, compute the federal income tax,
-- the NJ state income tax (honoring the property-tax deduction-vs-
-- credit choice), the FICA payroll tax, and the resulting effective
-- tax rate. Together these are the foundation of every downstream
-- affordability metric: required income (idea §5.4), disposable
-- income (idea §5.3), and the affordability gap (idea §5.4).
--
-- NULL CONTRACT
-- -------------
-- Any function returns NULL if (and only if) the underlying ref.*
-- tables do not have data for the requested (tax_year, filing_status).
-- That is the substrate-honesty signal: "we cannot compute this
-- without the IRS Rev. Proc. you asked us to honor". Calling code
-- must check for NULL and surface "tax data unavailable for this
-- year" -- never fall back to a different year silently.
--
-- WHY SQL FUNCTIONS, NOT PL/pgSQL
-- -------------------------------
-- Every function below is pure-SQL (LANGUAGE sql) and STABLE PARALLEL
-- SAFE. That means:
--   1. They can be inlined by the planner into larger queries (no
--      per-row function-call overhead).
--   2. They can run inside parallel scans.
--   3. They are deterministic given (input args, ref data), which
--      satisfies the verifiable-data Cursor rule.
--
-- CITATION DISCIPLINE
-- -------------------
-- These functions are *deterministic transforms over hand-cited ref
-- data*. The citations live on the ref rows; the functions reference
-- the formulas (bracket walk, std deduction subtraction, etc.) which
-- come from IRC sections cited in the COMMENT blocks below.
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- ref.formula_version: register the tax-engine version.
-- ----------------------------------------------------------------------------
INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '1.1.0-tax-engine-v1',
    'Phase 1 tax engine: federal + NJ state + FICA per (income, year, '
    'filing_status, dependents). Models the standard deduction, '
    'TCJA-era $0 personal exemption, CTC with phaseout, NJ property-'
    'tax deduction-vs-credit choice, and Additional Medicare. '
    'Does NOT model: itemized deductions, AMT, EITC, NJ pension '
    'exclusion, ACTC refundability, QBI deduction.',
    '2026-05-05',
    'Per VISION_2026.md Phase 1. NULL when ref data missing for the '
    'requested (year, filing_status); never silently substitutes a '
    'different year. Tested against IRS Pub 17 + NJ-1040 examples '
    'for tax years 2023 and 2024.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ----------------------------------------------------------------------------
-- derived.f_apply_brackets
--
-- The atomic piecewise-linear bracket walker shared by federal and NJ
-- state. Given a tax-table identifier (which table to read from) and
-- a (year, status, taxable_income), returns the cumulative tax owed.
--
-- We need TWO of these (federal vs NJ) because they read from
-- different ref tables. We keep the implementation parallel so a
-- reviewer can verify by inspection that the algorithm is identical.
-- ----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION derived.f_apply_federal_brackets(
    p_taxable_income NUMERIC,
    p_tax_year       SMALLINT,
    p_filing_status  TEXT
) RETURNS NUMERIC
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    -- Piecewise-linear sum: for each bracket, multiply the portion of
    -- income that lies inside that bracket by the bracket's marginal
    -- rate. The portion is GREATEST(0, MIN(income, next_floor) - floor).
    --
    -- The OUTER COALESCE covers two NULL paths:
    --   (a) p_taxable_income is NULL  -> return NULL (caller's bug)
    --   (b) no rows for (year, status) -> return NULL (substrate honesty)
    -- The INNER GREATEST(0,...) clamps below-bracket income to 0.
    SELECT
        CASE WHEN p_taxable_income IS NULL THEN NULL ELSE
            (SELECT SUM(
                GREATEST(
                    0::NUMERIC,
                    LEAST(p_taxable_income,
                          COALESCE(next_floor, 'Infinity'::NUMERIC))
                    - bracket_floor
                ) * marginal_rate
            )
            FROM (
                SELECT
                    bracket_floor,
                    marginal_rate,
                    LEAD(bracket_floor) OVER (ORDER BY bracket_ord) AS next_floor
                FROM ref.irs_federal_brackets
                WHERE tax_year      = p_tax_year
                  AND filing_status = p_filing_status
            ) walk)
        END;
$$;

COMMENT ON FUNCTION derived.f_apply_federal_brackets(NUMERIC, SMALLINT, TEXT) IS
    'Pure piecewise-linear bracket walk over ref.irs_federal_brackets. '
    'Returns NULL if no bracket data for (year, status). Inputs are '
    'TAXABLE income (post-std-deduction), not gross income.';


CREATE OR REPLACE FUNCTION derived.f_apply_nj_state_brackets(
    p_taxable_income NUMERIC,
    p_tax_year       SMALLINT,
    p_filing_status  TEXT
) RETURNS NUMERIC
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    -- Identical algorithm to federal; only the source table differs.
    SELECT
        CASE WHEN p_taxable_income IS NULL THEN NULL ELSE
            (SELECT SUM(
                GREATEST(
                    0::NUMERIC,
                    LEAST(p_taxable_income,
                          COALESCE(next_floor, 'Infinity'::NUMERIC))
                    - bracket_floor
                ) * marginal_rate
            )
            FROM (
                SELECT
                    bracket_floor,
                    marginal_rate,
                    LEAD(bracket_floor) OVER (ORDER BY bracket_ord) AS next_floor
                FROM ref.nj_state_brackets
                WHERE tax_year      = p_tax_year
                  AND filing_status = p_filing_status
            ) walk)
        END;
$$;

COMMENT ON FUNCTION derived.f_apply_nj_state_brackets(NUMERIC, SMALLINT, TEXT) IS
    'Pure piecewise-linear bracket walk over ref.nj_state_brackets. '
    'Returns NULL if no bracket data for (year, status). Inputs are '
    'NJ Gross Income net of NJ-allowed deductions (e.g. property-'
    'tax deduction up to cap), not raw gross.';


-- ----------------------------------------------------------------------------
-- derived.f_federal_taxable_income
--
-- Gross -> Taxable.
--   taxable = max(0, gross - standard_deduction - personal_exemption_total)
--
-- For TCJA years (2018-2025) the personal exemption is $0, so the
-- per-dependent contribution is also $0. The function still subtracts
-- it correctly so historical years (pre-2018) and post-2025 years
-- compute right when their seed data is added.
--
-- The standard deduction for taxpayer + spouse age 65+ / blind
-- add-ons is OUT OF SCOPE for v1 -- they are surfaced in the ref
-- table but the function uses base_amount only. A v1.1 will add an
-- (age_65_plus_taxpayer, blind_taxpayer, ...) parameter pack.
-- ----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION derived.f_federal_taxable_income(
    p_gross_income   NUMERIC,
    p_tax_year       SMALLINT,
    p_filing_status  TEXT,
    p_dependents     INT
) RETURNS NUMERIC
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    -- Filer + spouse counted in the personal-exemption multiplier when
    -- filing status implies a spouse (mfj or qss).
    --
    -- SUBSTRATE HONESTY: if the standard-deduction row is MISSING for
    -- the requested (year, status), we return NULL. We never silently
    -- substitute 0 (which would inflate taxable income to gross and
    -- compute a wrong-but-plausible tax). The personal-exemption
    -- table is allowed to be missing and treated as 0 because TCJA
    -- explicitly set the exemption to $0 for 2018-2025; absence is
    -- the documented case for those years.
    WITH parts AS (
        SELECT
            sd.base_amount AS std_deduction,
            COALESCE(pe.amount, 0) AS exemption_per_person,
            CASE WHEN p_filing_status IN ('mfj', 'qss') THEN 2 ELSE 1 END
                + GREATEST(0, COALESCE(p_dependents, 0)) AS exemption_count
        FROM ref.irs_standard_deduction sd
        LEFT JOIN ref.irs_personal_exemption pe
               ON pe.tax_year = sd.tax_year
        WHERE sd.tax_year      = p_tax_year
          AND sd.filing_status = p_filing_status
    )
    SELECT
        CASE
            WHEN p_gross_income IS NULL THEN NULL
            -- No std-deduction row for (year, status) -> the substrate
            -- has not been seeded for this year. Surface NULL.
            WHEN (SELECT std_deduction FROM parts) IS NULL THEN NULL
            ELSE GREATEST(
                0::NUMERIC,
                p_gross_income
                - (SELECT std_deduction FROM parts)
                - (SELECT exemption_per_person * exemption_count FROM parts)
            )
        END;
$$;

COMMENT ON FUNCTION derived.f_federal_taxable_income(NUMERIC, SMALLINT, TEXT, INT) IS
    'Federal taxable income: gross minus standard deduction minus '
    'personal exemptions for taxpayer + spouse (if MFJ/QSS) + '
    'dependents. Returns NULL if std deduction not seeded for year. '
    'V1 limitation: ignores age-65/blind add-ons and itemized '
    'deductions; the personalization UI must label this assumption.';


-- ----------------------------------------------------------------------------
-- derived.f_federal_child_tax_credit
--
-- Computes the non-refundable CTC for the household. Uses the user-
-- provided gross_income as the modified-AGI proxy (V1 simplification;
-- documented in COMMENT). The credit phases out at $50 per $1,000
-- (= 5%) of MAGI above $200K (single/HOH) or $400K (MFJ/QSS).
--
-- Returns the NON-refundable portion only -- i.e. the amount that
-- can offset tax liability but cannot generate a refund. The
-- refundable Additional CTC (ACTC) is a future enhancement.
-- ----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION derived.f_federal_child_tax_credit(
    p_modified_agi        NUMERIC,
    p_tax_year            SMALLINT,
    p_filing_status       TEXT,
    p_qualifying_children INT
) RETURNS NUMERIC
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    WITH params AS (
        SELECT
            -- ARPA 2021 had under-6 vs 6-to-17 split; for all other
            -- years the two columns are equal. V1 assumes all
            -- qualifying children get amount_6_to_17 (treating the
            -- split as a future v1.1 refinement).
            amount_6_to_17 AS per_child,
            CASE WHEN p_filing_status IN ('mfj', 'qss')
                 THEN phaseout_threshold_mfj
                 ELSE phaseout_threshold_single
            END AS threshold,
            phaseout_rate
        FROM ref.irs_child_tax_credit
        WHERE tax_year = p_tax_year
    )
    SELECT
        CASE
            WHEN p_qualifying_children IS NULL OR p_qualifying_children <= 0
                THEN 0
            WHEN (SELECT per_child FROM params) IS NULL
                THEN NULL  -- substrate-honesty: missing seed
            ELSE GREATEST(
                0::NUMERIC,
                (SELECT per_child * p_qualifying_children FROM params)
                - GREATEST(
                    0::NUMERIC,
                    COALESCE(p_modified_agi, 0)
                    - (SELECT threshold FROM params)
                  ) * (SELECT phaseout_rate FROM params)
            )
        END;
$$;

COMMENT ON FUNCTION derived.f_federal_child_tax_credit(NUMERIC, SMALLINT, TEXT, INT) IS
    'Non-refundable CTC after phaseout. Uses gross income as MAGI '
    'proxy (V1; close-enough for W-2 households without HSA, large '
    'tax-exempt interest, foreign-earned income, etc). Returns NULL '
    'if CTC params not seeded for year.';


-- ----------------------------------------------------------------------------
-- derived.f_federal_income_tax
--
-- COMPOSITE: gross income -> federal tax owed.
--   taxable          = f_federal_taxable_income(gross, year, status, deps)
--   tentative_tax    = f_apply_federal_brackets(taxable, year, status)
--   ctc              = f_federal_child_tax_credit(gross, year, status, kids)
--   federal_tax_owed = max(0, tentative_tax - ctc)
--
-- The max(0, ...) reflects that CTC under TCJA is non-refundable; the
-- refundable ACTC component would offset payroll taxes, not produce a
-- refund here. V1 treats CTC as purely non-refundable; the ACTC
-- refundability lives in a future composite that returns total
-- federal tax INCLUDING refundable credits (which can be negative).
-- ----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION derived.f_federal_income_tax(
    p_gross_income        NUMERIC,
    p_tax_year            SMALLINT,
    p_filing_status       TEXT,
    p_dependents          INT,
    p_qualifying_children INT
) RETURNS NUMERIC
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    -- Composite. Bubbles NULL through if any upstream component is
    -- NULL (substrate not seeded). Clamps to >= 0 only when all
    -- inputs are present (CTC fully wiping out tentative tax should
    -- not produce a negative; that is a SEMANTIC clamp, not a NULL
    -- swallow).
    WITH parts AS (
        SELECT
            derived.f_apply_federal_brackets(
                derived.f_federal_taxable_income(
                    p_gross_income, p_tax_year, p_filing_status, p_dependents),
                p_tax_year, p_filing_status) AS tentative,
            derived.f_federal_child_tax_credit(
                p_gross_income, p_tax_year, p_filing_status, p_qualifying_children
            ) AS ctc
    )
    SELECT
        CASE
            WHEN (SELECT tentative FROM parts) IS NULL THEN NULL
            -- CTC NULL (params missing) bubbles NULL too -- you cannot
            -- compute the federal liability without knowing whether
            -- you would have applied a credit.
            WHEN (SELECT ctc FROM parts) IS NULL THEN NULL
            ELSE GREATEST(
                0::NUMERIC,
                (SELECT tentative FROM parts) - (SELECT ctc FROM parts)
            )
        END;
$$;

COMMENT ON FUNCTION derived.f_federal_income_tax(NUMERIC, SMALLINT, TEXT, INT, INT) IS
    'Federal income-tax owed (post-CTC, non-refundable view). '
    'Gross -> Taxable -> Tentative tax -> Net tax after CTC. '
    'V1 limitations: ignores AMT, itemized deductions, EITC, ACTC, '
    'QBI, capital-gains preferential rates, and age/blind add-ons.';


-- ----------------------------------------------------------------------------
-- derived.f_fica_tax
--
-- Employee-side FICA. Three components per IRC s.3101:
--   ss        = ss_employee_rate * MIN(wage_income, ss_wage_base)
--   medicare  = medicare_employee_rate * wage_income
--   add_med   = additional_medicare_rate * MAX(0, wage_income - threshold)
--               (where threshold depends on filing_status; absent before 2013)
--
-- Note: this returns the EMPLOYEE share only. Self-employed would owe
-- both employee + employer halves (SECA); a v2 SECA function would
-- multiply ss and medicare by 2 and add the deduction-of-half-SE-tax
-- adjustment. V1 targets W-2 households per VISION Phase 4.
-- ----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION derived.f_fica_tax(
    p_wage_income    NUMERIC,
    p_tax_year       SMALLINT,
    p_filing_status  TEXT
) RETURNS NUMERIC
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    WITH params AS (
        SELECT
            ss_employee_rate,
            ss_wage_base,
            medicare_employee_rate,
            additional_medicare_rate,
            CASE WHEN p_filing_status IN ('mfj', 'qss')
                 THEN additional_medicare_threshold_mfj
                 ELSE additional_medicare_threshold_single
            END AS add_med_threshold
        FROM ref.fica_parameters
        WHERE tax_year = p_tax_year
    )
    SELECT
        CASE WHEN p_wage_income IS NULL THEN NULL ELSE
            COALESCE(
                (SELECT ss_employee_rate
                        * LEAST(GREATEST(0, p_wage_income), ss_wage_base)
                 FROM params),
                NULL
            )
            + COALESCE(
                (SELECT medicare_employee_rate * GREATEST(0, p_wage_income)
                 FROM params),
                NULL
            )
            + COALESCE(
                (SELECT additional_medicare_rate
                        * GREATEST(0, p_wage_income
                                      - COALESCE(add_med_threshold, 'Infinity'::NUMERIC))
                 FROM params),
                0
            )
        END;
$$;

COMMENT ON FUNCTION derived.f_fica_tax(NUMERIC, SMALLINT, TEXT) IS
    'Employee-side FICA payroll tax (IRC s.3101). Sum of OASDI '
    '(capped at SS wage base) + Medicare (uncapped) + Additional '
    'Medicare (over filing-status threshold, since TY 2013). Returns '
    'NULL if FICA params not seeded for year. V1: W-2 only; SECA '
    '(self-employed) is a v2 enhancement.';


-- ----------------------------------------------------------------------------
-- derived.f_nj_state_income_tax
--
-- NJ tax is computed two ways and the lower of the two is returned
-- (NJSA 54A:3A-17 + 54A:3A-20):
--
--   Method A (deduction): subtract MIN(property_tax_paid, cap) from
--   NJ gross income, also subtract personal exemptions, then walk
--   brackets. Final NJ tax = bracket result.
--
--   Method B (credit): walk brackets on NJ gross income minus
--   personal exemptions only (NO property-tax deduction), then
--   subtract the alternative_credit ($50). Final NJ tax = max(0,
--   bracket result - alternative_credit).
--
-- Return min(A, B). The credit method is better for taxpayers whose
-- property tax produces less marginal-rate savings than $50; mostly
-- relevant for very low income or very low property tax payments.
--
-- Personal exemptions: V1 supports taxpayer + spouse (if MFJ/QSS) +
-- dependents @ $1,500 each, per NJ-1040 Schedule A. Future v1.1 adds
-- the age-65, blind, veteran, college-student kinds.
-- ----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION derived.f_nj_state_income_tax(
    p_gross_income       NUMERIC,
    p_tax_year           SMALLINT,
    p_filing_status      TEXT,
    p_dependents         INT,
    p_property_tax_paid  NUMERIC
) RETURNS NUMERIC
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    -- SUBSTRATE HONESTY: if ANY required ref table is empty for this
    -- tax_year (brackets, personal exemption, property-tax dedn),
    -- return NULL. The function never silently substitutes 0 for
    -- missing reference data.
    WITH exemption_rows AS (
        -- We compute a per-kind aggregate so a missing kind shows up
        -- as NULL, not 0; then sum with explicit NULL handling below.
        SELECT
            MAX(amount) FILTER (WHERE exemption_kind = 'taxpayer')   AS taxpayer,
            MAX(amount) FILTER (WHERE exemption_kind = 'spouse')     AS spouse,
            MAX(amount) FILTER (WHERE exemption_kind = 'dependent')  AS dependent
        FROM ref.nj_state_personal_exemption
        WHERE tax_year = p_tax_year
    ),
    pt AS (
        SELECT deduction_cap, alternative_credit
        FROM ref.nj_state_property_tax_deduction
        WHERE tax_year = p_tax_year
    ),
    seeded AS (
        SELECT
            -- Are the required ref rows present at all for this year?
            (SELECT taxpayer  FROM exemption_rows) IS NOT NULL
            AND (SELECT dependent FROM exemption_rows) IS NOT NULL
            -- spouse only required if filing status uses one
            AND (CASE WHEN p_filing_status IN ('mfj', 'qss')
                      THEN (SELECT spouse FROM exemption_rows) IS NOT NULL
                      ELSE TRUE END)
            AND (SELECT deduction_cap FROM pt) IS NOT NULL
            AS ok
    ),
    exemption_total AS (
        SELECT
            (SELECT taxpayer FROM exemption_rows)
            + CASE WHEN p_filing_status IN ('mfj', 'qss')
                   THEN (SELECT spouse FROM exemption_rows)
                   ELSE 0 END
            + (SELECT dependent FROM exemption_rows)
              * GREATEST(0, COALESCE(p_dependents, 0))
            AS total
    ),
    method_a AS (
        -- Property-tax DEDUCTION: subtract MIN(prop_tax, cap) before brackets.
        SELECT derived.f_apply_nj_state_brackets(
            GREATEST(
                0::NUMERIC,
                p_gross_income
                - (SELECT total FROM exemption_total)
                - LEAST(
                    GREATEST(0::NUMERIC, COALESCE(p_property_tax_paid, 0)),
                    (SELECT deduction_cap FROM pt)
                )
            ),
            p_tax_year, p_filing_status
        ) AS tax
    ),
    method_b AS (
        -- Property-tax CREDIT: walk brackets without the deduction,
        -- then subtract the alternative_credit (clamp to >= 0).
        SELECT GREATEST(
            0::NUMERIC,
            derived.f_apply_nj_state_brackets(
                GREATEST(
                    0::NUMERIC,
                    p_gross_income - (SELECT total FROM exemption_total)
                ),
                p_tax_year, p_filing_status
            ) - (SELECT alternative_credit FROM pt)
        ) AS tax
    )
    SELECT
        CASE
            WHEN p_gross_income IS NULL THEN NULL
            -- Substrate-honesty NULLs propagate.
            WHEN NOT (SELECT ok FROM seeded) THEN NULL
            WHEN (SELECT tax FROM method_a) IS NULL THEN NULL  -- brackets missing
            ELSE LEAST(
                (SELECT tax FROM method_a),
                (SELECT tax FROM method_b)
            )
        END;
$$;

COMMENT ON FUNCTION derived.f_nj_state_income_tax(NUMERIC, SMALLINT, TEXT, INT, NUMERIC) IS
    'NJ Gross Income Tax. Computes BOTH the property-tax deduction '
    'method and the alternative-credit method per NJSA 54A:3A-17/20 '
    'and returns the lower of the two. V1 limitations: ignores age/'
    'blind/veteran exemptions, NJ EITC, pension exclusion, and the '
    'rent-as-property-tax-equivalent path for renters (renters '
    'should pass p_property_tax_paid = 0.18 * annual_rent at the '
    'call site; future v1.1 will surface this as a flag).';


-- ----------------------------------------------------------------------------
-- derived.f_household_taxes
--
-- The headline composite. Returns one ROW (typed) with the four
-- components and the effective rate. Calling code references columns
-- by name so adding components in V1.1 (e.g. EITC, ACTC) does not
-- break the contract.
-- ----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION derived.f_household_taxes(
    p_gross_income        NUMERIC,
    p_wage_income         NUMERIC,        -- usually = gross for W-2 households
    p_tax_year            SMALLINT,
    p_filing_status       TEXT,
    p_dependents          INT,
    p_qualifying_children INT,
    p_property_tax_paid   NUMERIC
) RETURNS TABLE (
    federal_income_tax NUMERIC,
    nj_state_tax       NUMERIC,
    fica_tax           NUMERIC,
    total_tax          NUMERIC,
    effective_rate     NUMERIC,
    formula_version    TEXT
)
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    SELECT
        derived.f_federal_income_tax(
            p_gross_income, p_tax_year, p_filing_status,
            p_dependents, p_qualifying_children
        ) AS federal_income_tax,
        derived.f_nj_state_income_tax(
            p_gross_income, p_tax_year, p_filing_status,
            p_dependents, p_property_tax_paid
        ) AS nj_state_tax,
        derived.f_fica_tax(
            p_wage_income, p_tax_year, p_filing_status
        ) AS fica_tax,
        ( derived.f_federal_income_tax(
            p_gross_income, p_tax_year, p_filing_status,
            p_dependents, p_qualifying_children
          )
          + derived.f_nj_state_income_tax(
              p_gross_income, p_tax_year, p_filing_status,
              p_dependents, p_property_tax_paid
            )
          + derived.f_fica_tax(
              p_wage_income, p_tax_year, p_filing_status
            )
        ) AS total_tax,
        CASE
            WHEN p_gross_income IS NULL OR p_gross_income = 0 THEN NULL
            ELSE round(
                ((derived.f_federal_income_tax(
                      p_gross_income, p_tax_year, p_filing_status,
                      p_dependents, p_qualifying_children)
                  + derived.f_nj_state_income_tax(
                      p_gross_income, p_tax_year, p_filing_status,
                      p_dependents, p_property_tax_paid)
                  + derived.f_fica_tax(
                      p_wage_income, p_tax_year, p_filing_status))
                 / p_gross_income)::NUMERIC,
                5
            )
        END AS effective_rate,
        '1.1.0-tax-engine-v1'::TEXT AS formula_version;
$$;

COMMENT ON FUNCTION derived.f_household_taxes(NUMERIC, NUMERIC, SMALLINT, TEXT, INT, INT, NUMERIC) IS
    'Phase 1 composite. Returns (federal, nj_state, fica, total, '
    'effective_rate, formula_version) for a household profile. The '
    'formula_version stamp lets downstream materializations carry '
    'reproducibility lineage per migration 001 contract. Any NULL '
    'component bubbles through to total_tax (and effective_rate); '
    'callers should not silently coalesce.';


COMMIT;
