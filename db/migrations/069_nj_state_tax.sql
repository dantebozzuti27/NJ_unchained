-- ============================================================================
-- Migration: 069_nj_state_tax
--
-- PHASE 1 of VISION_2026.md (idea spec §3.3, §5.2, §5.3, §5.4).
--
-- Establishes the NJ-state-tax reference layer: marginal income-tax
-- brackets per filing status, personal exemptions per dependent class,
-- property-tax deduction cap (or the $50 alternative refundable credit),
-- and the NJ piggyback rate on the federal Earned Income Tax Credit.
--
-- WHY NJ NEEDS ITS OWN MIGRATION (NOT JUST A "STATE" GENERIC TABLE)
-- ----------------------------------------------------------------
-- NJ tax law is genuinely irregular relative to other states:
--
--   1. NJ has TWO bracket schedules: one for {single, mfs} and one
--      for {mfj, hoh, qss}. They are NOT a 2x scale of each other --
--      the {mfj, hoh, qss} schedule has an additional bracket
--      (2.45%) inside the lower end and the bracket boundaries are
--      not in 1:2 ratio with the single schedule. Many states ARE
--      a uniform doubling; NJ is not.
--
--   2. NJ does not allow itemized deductions on the federal model.
--      A NJ taxpayer either (a) takes a property-tax deduction up
--      to the cap (currently $15,000; was $10,000 pre-2018), or
--      (b) takes a $50 refundable property-tax credit. Whichever
--      is larger after computing the resulting NJ tax. The
--      modeling consequence is that the NJ tax function takes
--      property_tax_paid as an input and returns the better of
--      two computed liabilities.
--
--   3. NJ has its own per-person personal exemption ($1,000 for
--      taxpayer + spouse, $1,500 for each dependent under 22 in
--      college, $1,000 otherwise). The federal personal exemption
--      was zeroed by TCJA 2018; the NJ one was not.
--
--   4. NJ piggybacks the federal Earned Income Tax Credit at a
--      legislated percentage (40% for tax year 2024); the rate
--      changes by year.
--
--   5. NJ has a pension exclusion (IRC s.402(a)) that materially
--      changes effective rates for retirees over 62 with income
--      below ~$150K. We DEFER this to a future migration; the
--      Phase 1 scope is W-2 households.
--
-- A generic "state.tax_brackets" table would obscure these. The NJ-
-- specific table makes the nuances obvious to the next reader and
-- to the eventual derived.f_nj_state_tax(...) function.
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- ref.nj_state_brackets
--
-- Per (tax_year, filing_status, bracket_ord). Same shape as the
-- federal table (068) for consistency, but logically separate -- a
-- federal bracket and a NJ bracket happen to use the same data
-- structure and that is all.
-- ----------------------------------------------------------------------------
CREATE TABLE ref.nj_state_brackets (
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

    source_url      TEXT NOT NULL
                    CHECK (source_url ~* '^https?://'),
    source_citation TEXT NOT NULL
                    CHECK (length(source_citation) > 5),

    PRIMARY KEY (tax_year, filing_status, bracket_ord)
);

COMMENT ON TABLE ref.nj_state_brackets IS
    'NJ Gross Income Tax marginal brackets per (tax_year, '
    'filing_status). Two effective schedules are shipped per year: '
    'one for {single, mfs} and one for {mfj, hoh, qss} -- but the '
    'table stores them as 5 separate (filing_status) keys for '
    'simpler joinability with ref.filing_status.';

ALTER TABLE ref.nj_state_brackets
    ADD CONSTRAINT nj_state_brackets_first_bracket_starts_at_zero
    CHECK (
        bracket_ord <> 1 OR bracket_floor = 0
    );

CREATE UNIQUE INDEX nj_state_brackets_no_duplicate_floor
    ON ref.nj_state_brackets (tax_year, filing_status, bracket_floor);

CREATE INDEX nj_state_brackets_year_idx
    ON ref.nj_state_brackets (tax_year);


-- ----------------------------------------------------------------------------
-- ref.nj_state_personal_exemption
--
-- NJ retains a per-person personal exemption (NJSA 54A:3-1.1). The
-- amounts vary by category (taxpayer/spouse vs dependent vs college
-- student dependent under 22). One row per (tax_year, exemption_kind).
-- ----------------------------------------------------------------------------
CREATE TABLE ref.nj_state_personal_exemption (
    tax_year        SMALLINT NOT NULL
                    CHECK (tax_year BETWEEN 2000 AND 2099),
    exemption_kind  TEXT NOT NULL
                    CHECK (exemption_kind IN
                           ('taxpayer', 'spouse', 'dependent',
                            'dependent_college_under_22',
                            'taxpayer_age_65_plus',
                            'spouse_age_65_plus',
                            'taxpayer_blind_disabled',
                            'spouse_blind_disabled',
                            'veteran')),
    amount          NUMERIC(8,2) NOT NULL
                    CHECK (amount >= 0),

    source_url      TEXT NOT NULL
                    CHECK (source_url ~* '^https?://'),
    source_citation TEXT NOT NULL
                    CHECK (length(source_citation) > 5),

    PRIMARY KEY (tax_year, exemption_kind)
);

COMMENT ON TABLE ref.nj_state_personal_exemption IS
    'NJ personal exemptions (NJSA 54A:3-1.1). The eight stackable '
    'kinds correspond to the lines on Form NJ-1040 Schedule A. A '
    'taxpayer claims the sum of all applicable kinds; e.g. a '
    'married couple filing jointly with two dependent kids and one '
    'spouse over 65 claims taxpayer + spouse + 2*dependent + '
    'spouse_age_65_plus.';


-- ----------------------------------------------------------------------------
-- ref.nj_state_property_tax_deduction
--
-- NJ taxpayers who pay property tax (or rent, treated as 18%
-- equivalent) may EITHER deduct property taxes paid up to a cap
-- (NJSA 54A:3A-17), OR take a flat refundable credit (currently
-- $50). The taxpayer chooses whichever produces lower total NJ tax.
--
-- The cap was $10,000 for tax years pre-2018, raised to $15,000
-- effective tax year 2018 (P.L. 2018, c.45).
-- ----------------------------------------------------------------------------
CREATE TABLE ref.nj_state_property_tax_deduction (
    tax_year                 SMALLINT PRIMARY KEY
                             CHECK (tax_year BETWEEN 2000 AND 2099),
    deduction_cap            NUMERIC(10,2) NOT NULL
                             CHECK (deduction_cap >= 0),
    alternative_credit       NUMERIC(8,2) NOT NULL
                             CHECK (alternative_credit >= 0),
    rent_property_tax_share  NUMERIC(5,4) NOT NULL DEFAULT 0.18
                             CHECK (rent_property_tax_share >= 0
                                    AND rent_property_tax_share <= 1),

    source_url       TEXT NOT NULL CHECK (source_url ~* '^https?://'),
    source_citation  TEXT NOT NULL CHECK (length(source_citation) > 5)
);

COMMENT ON TABLE ref.nj_state_property_tax_deduction IS
    'NJ property-tax deduction parameters. Renters compute their '
    '"property tax equivalent" as rent_property_tax_share * annual '
    'rent (currently 18%, NJSA 54A:3A-17). The eventual NJ-tax '
    'function compares (a) deducting min(prop_tax, cap) vs (b) '
    'taking the alternative_credit and returns whichever yields '
    'lower NJ tax.';


-- ----------------------------------------------------------------------------
-- ref.nj_state_eitc_match
--
-- NJ EITC piggybacks the federal EITC at a legislated rate. The rate
-- has changed over time: 25% (2010), 35% (2017), 40% (2020+).
-- One row per tax year keeps the function pure data.
-- ----------------------------------------------------------------------------
CREATE TABLE ref.nj_state_eitc_match (
    tax_year         SMALLINT PRIMARY KEY
                     CHECK (tax_year BETWEEN 2000 AND 2099),
    match_rate       NUMERIC(6,5) NOT NULL
                     CHECK (match_rate >= 0 AND match_rate <= 1),

    -- NJ also extends EITC to certain federal-EITC-ineligible groups
    -- (e.g. workers 18-24 with no qualifying child, since 2021).
    -- We capture the policy via a free-text note rather than another
    -- table, because the eligibility extensions are not numeric.
    eligibility_note TEXT,

    source_url       TEXT NOT NULL CHECK (source_url ~* '^https?://'),
    source_citation  TEXT NOT NULL CHECK (length(source_citation) > 5)
);

COMMENT ON TABLE ref.nj_state_eitc_match IS
    'NJ EITC matches federal EITC at a legislated percentage. '
    'Per-year row; eligibility_note captures non-numeric expansions '
    'such as the 2021 expansion to workers 18-24 without children.';


-- ----------------------------------------------------------------------------
-- Plausibility view: every (year, status) NJ bracket tuple must
-- have at least one row with floor 0. Same invariant as federal.
-- ----------------------------------------------------------------------------
CREATE VIEW ref.v_nj_state_brackets_coverage AS
SELECT
    tax_year,
    filing_status,
    count(*)                       AS bracket_count,
    bool_or(bracket_floor = 0)     AS has_zero_floor,
    max(marginal_rate)             AS max_marginal_rate
FROM ref.nj_state_brackets
GROUP BY tax_year, filing_status;

COMMENT ON VIEW ref.v_nj_state_brackets_coverage IS
    'Coverage diagnostic for NJ state brackets. Asset checks query '
    'this to fail fast when a (year, status) tuple is partially '
    'loaded.';


COMMIT;
