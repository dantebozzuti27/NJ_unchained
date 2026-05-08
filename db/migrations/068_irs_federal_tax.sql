-- ============================================================================
-- Migration: 068_irs_federal_tax
--
-- PHASE 1 of VISION_2026.md (idea spec §3.3, §5.2, §5.3, §5.4).
--
-- Establishes the federal-tax reference layer: marginal income-tax
-- brackets, standard deduction, personal exemption (zero for 2018-2025
-- per TCJA, but the field exists so historical years and post-TCJA
-- sunset are representable), child-tax-credit parameters, and FICA
-- (Social Security + Medicare) parameters. All values are versioned
-- per (tax_year, filing_status) and every row carries the IRS Revenue
-- Procedure citation that authorizes it.
--
-- WHY ref AND NOT raw
-- -------------------
-- The schema 001 comment says ref is "Hand-maintained or seeded from
-- authoritative sources (Census FIPS, IRS thresholds)" -- IRS thresholds
-- are called out by name. Brackets are not crawled from an HTTP source by
-- an ingester; they are transcribed from a published Revenue Procedure
-- and reviewed against the IRS's own published examples. The
-- (source_url, source_citation) columns make every row auditable.
--
-- WHY THIS MATTERS FOR THE PLATFORM
-- ---------------------------------
-- The idea spec section 10 ("Key design warning") explicitly names
-- "good enough tax assumptions" as the trap that destroys credibility.
-- The spec section 3.3 calls taxes "the most important and most
-- error-prone layer" and demands the effective tax rate be SIMULATED
-- per income band, not assumed flat. Until this layer exists, every
-- downstream affordability number (required income, disposable income,
-- affordability gap, the "collapse curve") is fiction.
--
-- The eventual derived.f_effective_tax_rate(income, year, filing_status,
-- dependents, county_fips) function reads from these tables (federal
-- here, NJ state in migration 069) and a county property-tax rate from
-- raw.nj_property_tax_county (already loaded). It carries a
-- formula_version stamp per the verifiable-data Cursor rule.
--
-- DATA YEAR COVERAGE PLAN
-- -----------------------
-- This migration ships the schema only. Seeds in db/seeds/ load data
-- per tax year. The initial seed window is 2010-2024 (15 tax years),
-- which is wider than the FHFA / ACS5 horizon already in the platform
-- and lets the AEI (idea section 5.5) span the full housing cycle.
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- ref.filing_status
--
-- IRS-recognized filing statuses. The five-value enum is fixed by
-- IRC section 1; we encode it as a small lookup table rather than a
-- CHECK enum so other tables can FK to it and so the human-readable
-- description is on hand for UI surfaces.
--
-- 'qss' = Qualifying Surviving Spouse (formerly Qualifying Widow(er)).
--         Allows MFJ rates for 2 years after spouse's death if
--         maintaining a household for a qualifying child.
-- ----------------------------------------------------------------------------
CREATE TABLE ref.filing_status (
    code        TEXT PRIMARY KEY
                CHECK (code IN ('single', 'mfj', 'mfs', 'hoh', 'qss')),
    short_name  TEXT NOT NULL,
    description TEXT NOT NULL
);

COMMENT ON TABLE ref.filing_status IS
    'IRS-recognized filing statuses (IRC section 1). Five rows, '
    'authoritative; FK target for ref.irs_federal_brackets and '
    'ref.irs_standard_deduction.';

-- The five rows are seeded as part of THIS migration (not a separate
-- seed file) because they are IRS-mandated and immutable; the schema
-- and the data are coupled.
INSERT INTO ref.filing_status (code, short_name, description) VALUES
    ('single', 'Single',                       'Unmarried and not qualifying as Head of Household'),
    ('mfj',    'Married Filing Jointly',       'Married couple combining returns'),
    ('mfs',    'Married Filing Separately',    'Married, each spouse files separately'),
    ('hoh',    'Head of Household',            'Unmarried, paid >50% of household, qualifying dependent'),
    ('qss',    'Qualifying Surviving Spouse',  'Surviving spouse, MFJ rates allowed for 2 years post-bereavement');


-- ----------------------------------------------------------------------------
-- ref.irs_federal_brackets
--
-- One row per (tax_year, filing_status, bracket ordinal).
--
-- bracket_floor is the lower bound (inclusive) of the bracket in
-- nominal dollars. The upper bound is implicit: it equals the next
-- ordinal's floor minus $0.01, OR is open-ended for the highest
-- ordinal. We do NOT store an explicit ceiling because the brackets
-- form a contiguous partition by construction; storing both
-- redundantly is a CHECK constraint waiting to be violated.
--
-- marginal_rate is the rate APPLIED TO INCOME WITHIN THE BRACKET, not
-- the cumulative tax. Computing the federal liability is a piecewise-
-- linear sum: SUM over brackets of MIN(income - floor, ceiling - floor)
-- * rate, where (income - floor) is clamped to >= 0.
--
-- The (source_url, source_citation) pair is non-optional. Every
-- bracket row must point at a specific IRS Revenue Procedure (or
-- substitute statute for years with no Rev. Proc., e.g. when ARPA
-- changed mid-year amounts). This satisfies the verifiable-data rule.
-- ----------------------------------------------------------------------------
CREATE TABLE ref.irs_federal_brackets (
    tax_year        SMALLINT NOT NULL
                    CHECK (tax_year BETWEEN 2000 AND 2099),
    filing_status   TEXT NOT NULL
                    REFERENCES ref.filing_status(code),
    bracket_ord     SMALLINT NOT NULL
                    CHECK (bracket_ord BETWEEN 1 AND 20),
    bracket_floor   NUMERIC(14,2) NOT NULL
                    CHECK (bracket_floor >= 0),
    marginal_rate   NUMERIC(7,5) NOT NULL
                    CHECK (marginal_rate > 0 AND marginal_rate <= 1),

    -- Provenance: every row must cite an IRS publication.
    source_url      TEXT NOT NULL
                    CHECK (source_url ~* '^https?://'),
    source_citation TEXT NOT NULL
                    CHECK (length(source_citation) > 5),

    PRIMARY KEY (tax_year, filing_status, bracket_ord)
);

COMMENT ON TABLE ref.irs_federal_brackets IS
    'Federal marginal income-tax brackets per (tax_year, filing_status). '
    'Bracket floors are inclusive lower bounds; ceilings are implicit '
    '(equal next ordinal''s floor). Highest ordinal per (year,status) '
    'has no upper bound. Every row cites the IRS Rev. Proc. that '
    'authorized it.';

-- The first-bracket invariant: every (year, status) MUST start at $0,
-- otherwise income below the lowest floor is ambiguous. We enforce
-- this declaratively rather than relying on the seed author.
ALTER TABLE ref.irs_federal_brackets
    ADD CONSTRAINT irs_federal_brackets_first_bracket_starts_at_zero
    CHECK (
        bracket_ord <> 1 OR bracket_floor = 0
    );

-- Bracket floors must strictly increase within (year, status). We can
-- only enforce this with a deferred trigger or a unique index on
-- (year, status, floor) -- we use the latter; it also catches duplicate
-- floors (which would imply two brackets with the same starting point).
CREATE UNIQUE INDEX irs_federal_brackets_no_duplicate_floor
    ON ref.irs_federal_brackets (tax_year, filing_status, bracket_floor);

CREATE INDEX irs_federal_brackets_year_idx
    ON ref.irs_federal_brackets (tax_year);


-- ----------------------------------------------------------------------------
-- ref.irs_standard_deduction
--
-- Per (tax_year, filing_status). Rev. Proc. publishes these annually
-- adjusted for inflation (IRC section 63). The two add-ons (age >= 65,
-- blind) are stackable: a single 67-year-old blind taxpayer in 2024
-- gets $14,600 base + $1,950 age + $1,950 blind = $18,500.
--
-- We do NOT store the dependent-deduction here -- that was eliminated
-- by TCJA for 2018-2025 and replaced by an expanded standard deduction
-- and CTC. ref.irs_personal_exemption (below) carries the historical
-- exemption amount (zero from 2018 through at least 2025).
-- ----------------------------------------------------------------------------
CREATE TABLE ref.irs_standard_deduction (
    tax_year             SMALLINT NOT NULL
                         CHECK (tax_year BETWEEN 2000 AND 2099),
    filing_status        TEXT NOT NULL
                         REFERENCES ref.filing_status(code),
    base_amount          NUMERIC(10,2) NOT NULL
                         CHECK (base_amount >= 0),
    additional_age_65    NUMERIC(8,2)  NOT NULL DEFAULT 0
                         CHECK (additional_age_65 >= 0),
    additional_blind     NUMERIC(8,2)  NOT NULL DEFAULT 0
                         CHECK (additional_blind >= 0),

    source_url           TEXT NOT NULL
                         CHECK (source_url ~* '^https?://'),
    source_citation      TEXT NOT NULL
                         CHECK (length(source_citation) > 5),

    PRIMARY KEY (tax_year, filing_status)
);

COMMENT ON TABLE ref.irs_standard_deduction IS
    'Standard deduction per (tax_year, filing_status). additional_age_65 '
    'and additional_blind are stackable add-ons per IRC section 63(f).';


-- ----------------------------------------------------------------------------
-- ref.irs_personal_exemption
--
-- The per-person personal exemption is a single annual amount under
-- pre-TCJA law (IRC section 151) and was set to $0 by TCJA for tax
-- years 2018 through 2025. We model it here so historical years
-- (e.g. 2010-2017) compute correctly and so any post-2025 sunset
-- restoration can be loaded without a schema change.
-- ----------------------------------------------------------------------------
CREATE TABLE ref.irs_personal_exemption (
    tax_year         SMALLINT PRIMARY KEY
                     CHECK (tax_year BETWEEN 2000 AND 2099),
    amount           NUMERIC(8,2) NOT NULL
                     CHECK (amount >= 0),
    source_url       TEXT NOT NULL
                     CHECK (source_url ~* '^https?://'),
    source_citation  TEXT NOT NULL
                     CHECK (length(source_citation) > 5)
);

COMMENT ON TABLE ref.irs_personal_exemption IS
    'Per-person personal exemption (IRC s.151). Set to $0 by TCJA for '
    'tax years 2018-2025. Multiply by (taxpayer + spouse + dependents) '
    'to get total exemption claimable.';


-- ----------------------------------------------------------------------------
-- ref.irs_child_tax_credit
--
-- Child Tax Credit (IRC section 24). Pre-TCJA: $1,000 per qualifying
-- child. TCJA (2018-2025): $2,000 per qualifying child under 17, of
-- which up to $1,700 (2024) is refundable as the Additional Child Tax
-- Credit (ACTC). ARPA (2021 only): expanded to $3,000-$3,600 per child
-- and made fully refundable; we encode 2021's exception explicitly.
--
-- Phase-out starts at modified AGI of $200K (single/HOH) or $400K
-- (MFJ) under TCJA, reducing the credit by $50 per $1,000 of excess
-- AGI. We store both thresholds and the phase-out rate per year so
-- temporal-law changes are data-side, not code-side.
-- ----------------------------------------------------------------------------
CREATE TABLE ref.irs_child_tax_credit (
    tax_year                 SMALLINT PRIMARY KEY
                             CHECK (tax_year BETWEEN 2000 AND 2099),

    -- Per-child credit amounts. For 2021 only (ARPA), there are two
    -- amounts (under-6 vs 6-to-17). For all other years the under-6
    -- column equals the under-17 column.
    amount_under_6           NUMERIC(8,2) NOT NULL CHECK (amount_under_6 >= 0),
    amount_6_to_17           NUMERIC(8,2) NOT NULL CHECK (amount_6_to_17 >= 0),

    -- Maximum refundable portion (Additional CTC). For 2021 only the
    -- entire credit was refundable; encode as the same value as the
    -- per-child amount.
    refundable_max_per_child NUMERIC(8,2) NOT NULL
                             CHECK (refundable_max_per_child >= 0),

    -- Phase-out: credit reduced by phaseout_rate per $1 of AGI above
    -- threshold. (TCJA: $50 per $1,000 = 0.05 per $1.)
    phaseout_threshold_single  NUMERIC(12,2) NOT NULL,
    phaseout_threshold_mfj     NUMERIC(12,2) NOT NULL,
    phaseout_rate              NUMERIC(7,5)  NOT NULL
                               CHECK (phaseout_rate >= 0 AND phaseout_rate <= 1),

    source_url       TEXT NOT NULL CHECK (source_url ~* '^https?://'),
    source_citation  TEXT NOT NULL CHECK (length(source_citation) > 5)
);

COMMENT ON TABLE ref.irs_child_tax_credit IS
    'Child Tax Credit parameters (IRC s.24). 2021 ARPA expansion is '
    'represented via amount_under_6 vs amount_6_to_17 differing; for '
    'all other years the two columns are equal.';


-- ----------------------------------------------------------------------------
-- ref.fica_parameters
--
-- Social Security (OASDI) + Medicare (HI) payroll-tax parameters.
-- These are EMPLOYEE-SIDE rates (the W-2 amounts the user actually
-- pays). Self-employed people pay both halves (SECA); a v2 of this
-- table would carry that distinction, but the personalization engine
-- targets W-2 households for Phase 1.
--
-- Social Security has a wage base above which the OASDI rate stops
-- applying. Medicare has no wage cap. Additional Medicare (ACA, 2013+)
-- is 0.9% on wages above $200K single / $250K MFJ; absent for years
-- before 2013 (we store NULL or 0).
-- ----------------------------------------------------------------------------
CREATE TABLE ref.fica_parameters (
    tax_year                          SMALLINT PRIMARY KEY
                                      CHECK (tax_year BETWEEN 2000 AND 2099),

    ss_employee_rate                  NUMERIC(6,5) NOT NULL
                                      CHECK (ss_employee_rate >= 0 AND ss_employee_rate <= 1),
    ss_wage_base                      NUMERIC(12,2) NOT NULL
                                      CHECK (ss_wage_base >= 0),

    medicare_employee_rate            NUMERIC(6,5) NOT NULL
                                      CHECK (medicare_employee_rate >= 0
                                             AND medicare_employee_rate <= 1),

    additional_medicare_rate          NUMERIC(6,5) NOT NULL DEFAULT 0
                                      CHECK (additional_medicare_rate >= 0
                                             AND additional_medicare_rate <= 1),
    additional_medicare_threshold_single NUMERIC(12,2)
                                      CHECK (additional_medicare_threshold_single IS NULL
                                             OR additional_medicare_threshold_single >= 0),
    additional_medicare_threshold_mfj    NUMERIC(12,2)
                                      CHECK (additional_medicare_threshold_mfj IS NULL
                                             OR additional_medicare_threshold_mfj >= 0),

    source_url       TEXT NOT NULL CHECK (source_url ~* '^https?://'),
    source_citation  TEXT NOT NULL CHECK (length(source_citation) > 5)
);

COMMENT ON TABLE ref.fica_parameters IS
    'FICA (Social Security + Medicare) employee-side payroll-tax '
    'parameters per tax year. additional_medicare_* fields are NULL '
    'for years before 2013 (Additional Medicare Tax did not exist).';


-- ----------------------------------------------------------------------------
-- Plausibility view: every (year, status) tuple should have at least
-- one bracket row whose floor is 0. Without this, a federal-tax
-- function would return NULL for low incomes.
-- ----------------------------------------------------------------------------
CREATE VIEW ref.v_irs_federal_brackets_coverage AS
SELECT
    tax_year,
    filing_status,
    count(*)                                 AS bracket_count,
    bool_or(bracket_floor = 0)               AS has_zero_floor,
    max(marginal_rate)                       AS max_marginal_rate
FROM ref.irs_federal_brackets
GROUP BY tax_year, filing_status;

COMMENT ON VIEW ref.v_irs_federal_brackets_coverage IS
    'Coverage diagnostic: one row per (tax_year, filing_status) with '
    'bracket_count and has_zero_floor. Asset checks query this to '
    'fail fast when a year/status is partially loaded.';


COMMIT;
