-- ============================================================================
-- Seed: 014_irs_federal_tax_2022
--
-- Federal income-tax reference data for tax year 2022.
-- Every row is hand-transcribed from Rev. Proc. 2021-45 (the IRS document
-- that authorized the 2022 inflation adjustments under IRC s.1(j)(2)) and
-- cross-checked against the IRS newsroom announcement IR-2021-219.
--
-- AUTHORITATIVE SOURCES
-- ---------------------
--   Rev. Proc. 2021-45 (released 2021-11-10), full text:
--     https://www.irs.gov/pub/irs-drop/rp-21-45.pdf
--   Newsroom announcement (IR-2021-219):
--     https://www.irs.gov/newsroom/irs-provides-tax-inflation-adjustments-for-tax-year-2022
--   FICA SS wage base $147,000:
--     SSA Fact Sheet 2022 (https://www.ssa.gov/oact/cola/cbb.html)
--
-- ROADMAP NOTE
-- ------------
-- This seed advances Phase 5 (historical tax-table backfill) by one year.
-- The Phase-1 tax engine and the Phase-2/3/4 substrates that depend on it
-- already evaluate correctly for any year present in these tables; loading
-- 2022 means the Collapse Curve, DI trajectory, AEI, and `/personalize`
-- historical scenarios all light up for 2022 across all 21 NJ counties as
-- soon as the matching 015_nj_state_tax_2022.sql lands. No code changes
-- required; this is pure substrate work.
--
-- IDEMPOTENCY
-- -----------
-- All inserts use ON CONFLICT DO UPDATE so re-running the seed file is
-- safe and amendments are loud (the row's source_citation will change).
-- ============================================================================

-- ----------------------------------------------------------------------------
-- ref.irs_federal_brackets   (TY 2022 = Rev. Proc. 2021-45, Section 3.01)
-- The 7-bracket TCJA schedule (rates 10/12/22/24/32/35/37 percent),
-- inflation-adjusted from the 2017 statutory amounts via IRC s.1(j)(2).
-- Floors verified against the four bracket tables in s.3.01:
--   Table 1 (MFJ + Surviving Spouses)        -> 'mfj' and 'qss'
--   Table 2 (Heads of Households)            -> 'hoh'
--   Table 3 (Unmarried, non-HOH non-Surv)    -> 'single'
--   Table 4 (Married Filing Separately)      -> 'mfs'
-- ----------------------------------------------------------------------------

INSERT INTO ref.irs_federal_brackets
    (tax_year, filing_status, bracket_ord, bracket_floor, marginal_rate,
     source_url, source_citation)
VALUES
    -- Single  (Rev. Proc. 2021-45, s.3.01, Table 3)
    (2022, 'single', 1,        0.00, 0.10000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 3 (Unmarried Individuals)'),
    (2022, 'single', 2,    10275.00, 0.12000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 3 (Unmarried Individuals)'),
    (2022, 'single', 3,    41775.00, 0.22000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 3 (Unmarried Individuals)'),
    (2022, 'single', 4,    89075.00, 0.24000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 3 (Unmarried Individuals)'),
    (2022, 'single', 5,   170050.00, 0.32000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 3 (Unmarried Individuals)'),
    (2022, 'single', 6,   215950.00, 0.35000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 3 (Unmarried Individuals)'),
    (2022, 'single', 7,   539900.00, 0.37000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 3 (Unmarried Individuals)'),

    -- Married Filing Jointly  (Rev. Proc. 2021-45, s.3.01, Table 1)
    (2022, 'mfj', 1,        0.00, 0.10000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 1 (MFJ + Surviving Spouses)'),
    (2022, 'mfj', 2,    20550.00, 0.12000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 1 (MFJ + Surviving Spouses)'),
    (2022, 'mfj', 3,    83550.00, 0.22000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 1 (MFJ + Surviving Spouses)'),
    (2022, 'mfj', 4,   178150.00, 0.24000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 1 (MFJ + Surviving Spouses)'),
    (2022, 'mfj', 5,   340100.00, 0.32000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 1 (MFJ + Surviving Spouses)'),
    (2022, 'mfj', 6,   431900.00, 0.35000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 1 (MFJ + Surviving Spouses)'),
    (2022, 'mfj', 7,   647850.00, 0.37000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 1 (MFJ + Surviving Spouses)'),

    -- Qualifying Surviving Spouse: same Table 1 as MFJ.
    (2022, 'qss', 1,        0.00, 0.10000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 1 (QSS = MFJ schedule)'),
    (2022, 'qss', 2,    20550.00, 0.12000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 1 (QSS = MFJ schedule)'),
    (2022, 'qss', 3,    83550.00, 0.22000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 1 (QSS = MFJ schedule)'),
    (2022, 'qss', 4,   178150.00, 0.24000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 1 (QSS = MFJ schedule)'),
    (2022, 'qss', 5,   340100.00, 0.32000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 1 (QSS = MFJ schedule)'),
    (2022, 'qss', 6,   431900.00, 0.35000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 1 (QSS = MFJ schedule)'),
    (2022, 'qss', 7,   647850.00, 0.37000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 1 (QSS = MFJ schedule)'),

    -- Married Filing Separately  (Rev. Proc. 2021-45, s.3.01, Table 4)
    -- Brackets 1-5 mirror Single (= MFJ / 2 by IRC s.1(j)(2)(D)); the
    -- top brackets are MFJ / 2 floors (35% starts at $215,950 like Single,
    -- but 37% starts at $323,925 = 647,850 / 2 -- DIFFERENT from Single).
    (2022, 'mfs', 1,        0.00, 0.10000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 4 (MFS)'),
    (2022, 'mfs', 2,    10275.00, 0.12000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 4 (MFS)'),
    (2022, 'mfs', 3,    41775.00, 0.22000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 4 (MFS)'),
    (2022, 'mfs', 4,    89075.00, 0.24000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 4 (MFS)'),
    (2022, 'mfs', 5,   170050.00, 0.32000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 4 (MFS)'),
    (2022, 'mfs', 6,   215950.00, 0.35000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 4 (MFS)'),
    (2022, 'mfs', 7,   323925.00, 0.37000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 4 (MFS)'),

    -- Head of Household  (Rev. Proc. 2021-45, s.3.01, Table 2)
    (2022, 'hoh', 1,        0.00, 0.10000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 2 (HOH)'),
    (2022, 'hoh', 2,    14650.00, 0.12000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 2 (HOH)'),
    (2022, 'hoh', 3,    55900.00, 0.22000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 2 (HOH)'),
    (2022, 'hoh', 4,    89050.00, 0.24000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 2 (HOH)'),
    (2022, 'hoh', 5,   170050.00, 0.32000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 2 (HOH)'),
    (2022, 'hoh', 6,   215950.00, 0.35000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 2 (HOH)'),
    (2022, 'hoh', 7,   539900.00, 0.37000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.01 Table 2 (HOH)')
ON CONFLICT (tax_year, filing_status, bracket_ord) DO UPDATE SET
    bracket_floor   = EXCLUDED.bracket_floor,
    marginal_rate   = EXCLUDED.marginal_rate,
    source_url      = EXCLUDED.source_url,
    source_citation = EXCLUDED.source_citation;


-- ----------------------------------------------------------------------------
-- ref.irs_standard_deduction
--
-- 2022 (Rev. Proc. 2021-45 s.3.15(1) for base, s.3.15(3) for age/blind):
--   Base: Single $12,950  MFJ $25,900  MFS $12,950  HOH $19,400  QSS $25,900
--   Aged/blind addition under IRC s.63(f):
--     $1,400 for MFJ / MFS / QSS
--     $1,750 for Single and HOH (per the s.3.15(3) "if also unmarried and
--     not a surviving spouse" clause)
-- ----------------------------------------------------------------------------

INSERT INTO ref.irs_standard_deduction
    (tax_year, filing_status, base_amount, additional_age_65, additional_blind,
     source_url, source_citation)
VALUES
    (2022, 'single', 12950.00, 1750.00, 1750.00,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.15(1) (base) + s.3.15(3) (age/blind add-on, $1,750 unmarried/non-surviving)'),
    (2022, 'mfj',    25900.00, 1400.00, 1400.00,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.15(1) (base) + s.3.15(3) (age/blind add-on, $1,400 MFJ)'),
    (2022, 'mfs',    12950.00, 1400.00, 1400.00,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.15(1) (base) + s.3.15(3) (age/blind add-on, $1,400 MFS)'),
    (2022, 'hoh',    19400.00, 1750.00, 1750.00,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.15(1) (base) + s.3.15(3) (age/blind add-on, $1,750 HOH unmarried)'),
    (2022, 'qss',    25900.00, 1400.00, 1400.00,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'Rev. Proc. 2021-45 s.3.15(1) (base) + s.3.15(3) (age/blind add-on, $1,400 QSS)')
ON CONFLICT (tax_year, filing_status) DO UPDATE SET
    base_amount        = EXCLUDED.base_amount,
    additional_age_65  = EXCLUDED.additional_age_65,
    additional_blind   = EXCLUDED.additional_blind,
    source_url         = EXCLUDED.source_url,
    source_citation    = EXCLUDED.source_citation;


-- ----------------------------------------------------------------------------
-- ref.irs_personal_exemption
--
-- TCJA (2018-2025): set to $0 by IRC s.151(d)(5)(A) as added by Pub. L.
-- 115-97 s.11041(a). Rev. Proc. 2021-45 confirms "no personal exemption"
-- per the section .15(2) inflation-adjustment list reference.
-- ----------------------------------------------------------------------------

INSERT INTO ref.irs_personal_exemption
    (tax_year, amount, source_url, source_citation)
VALUES
    (2022, 0.00,
     'https://www.law.cornell.edu/uscode/text/26/151',
     'IRC s.151(d)(5)(A) (TCJA, P.L. 115-97 s.11041): exemption $0 for TY 2018-2025')
ON CONFLICT (tax_year) DO UPDATE SET
    amount          = EXCLUDED.amount,
    source_url      = EXCLUDED.source_url,
    source_citation = EXCLUDED.source_citation;


-- ----------------------------------------------------------------------------
-- ref.irs_child_tax_credit
--
-- TY 2022: ARPA's expanded CTC ($3,000 / $3,600 with full refundability)
-- expired at end of 2021. TY 2022 reverts to the TCJA baseline:
--   $2,000/child under 17, refundable max $1,500 per Rev. Proc. 2021-45 s.3.05
--   (the $1,400 statutory floor in IRC s.24(d)(1)(A) is inflation-adjusted
--   for tax years 2018+, and rounds down to the nearest $100 multiple; the
--   TY 2022 inflation adjustment lands at $1,500).
-- Phase-out at AGI $200K (single/HOH/MFS) or $400K (MFJ) at $50/$1,000 = 5%
-- per IRC s.24(b)(1) and (b)(2) (TCJA, not inflation-adjusted; held
-- constant 2018-2025).
-- ----------------------------------------------------------------------------

INSERT INTO ref.irs_child_tax_credit
    (tax_year,
     amount_under_6, amount_6_to_17, refundable_max_per_child,
     phaseout_threshold_single, phaseout_threshold_mfj, phaseout_rate,
     source_url, source_citation)
VALUES
    (2022,
     2000.00, 2000.00, 1500.00,
     200000.00, 400000.00, 0.05000,
     'https://www.irs.gov/pub/irs-drop/rp-21-45.pdf',
     'IRC s.24(h) (TCJA, $2,000 base); refundable max $1,500 per Rev. Proc. 2021-45 s.3.05')
ON CONFLICT (tax_year) DO UPDATE SET
    amount_under_6           = EXCLUDED.amount_under_6,
    amount_6_to_17           = EXCLUDED.amount_6_to_17,
    refundable_max_per_child = EXCLUDED.refundable_max_per_child,
    phaseout_threshold_single = EXCLUDED.phaseout_threshold_single,
    phaseout_threshold_mfj    = EXCLUDED.phaseout_threshold_mfj,
    phaseout_rate            = EXCLUDED.phaseout_rate,
    source_url               = EXCLUDED.source_url,
    source_citation          = EXCLUDED.source_citation;


-- ----------------------------------------------------------------------------
-- ref.fica_parameters
--
-- TY 2022:
--   Social Security (OASDI) wage base = $147,000
--     (SSA Fact Sheet, "Contribution and Benefit Base", 2021-10-13 release).
--   OASDI rate (employee) = 6.2% per IRC s.3101(a) -- statutory, never
--     inflation-adjusted.
--   Medicare (HI) rate = 1.45% per IRC s.3101(b)(1) -- statutory, no cap.
--   Additional Medicare 0.9% per IRC s.3101(b)(2) on wages over
--     $200K (single, HOH, MFS) or $250K (MFJ, QSS) -- statutory thresholds
--     fixed by ACA s.9015 since TY 2013 (NOT inflation-adjusted, deliberate
--     congressional design choice).
-- ----------------------------------------------------------------------------

INSERT INTO ref.fica_parameters
    (tax_year,
     ss_employee_rate, ss_wage_base,
     medicare_employee_rate,
     additional_medicare_rate,
     additional_medicare_threshold_single,
     additional_medicare_threshold_mfj,
     source_url, source_citation)
VALUES
    (2022,
     0.06200, 147000.00,
     0.01450,
     0.00900, 200000.00, 250000.00,
     'https://www.ssa.gov/oact/cola/cbb.html',
     'SSA Contribution and Benefit Base: SS wage base $147,000 for TY2022; Medicare add''l per IRC s.3101(b)(2)')
ON CONFLICT (tax_year) DO UPDATE SET
    ss_employee_rate                     = EXCLUDED.ss_employee_rate,
    ss_wage_base                         = EXCLUDED.ss_wage_base,
    medicare_employee_rate               = EXCLUDED.medicare_employee_rate,
    additional_medicare_rate             = EXCLUDED.additional_medicare_rate,
    additional_medicare_threshold_single = EXCLUDED.additional_medicare_threshold_single,
    additional_medicare_threshold_mfj    = EXCLUDED.additional_medicare_threshold_mfj,
    source_url                           = EXCLUDED.source_url,
    source_citation                      = EXCLUDED.source_citation;
