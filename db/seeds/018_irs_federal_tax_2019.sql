-- ============================================================================
-- Seed: 018_irs_federal_tax_2019
--
-- Federal income-tax reference data for tax year 2019.
-- Every row is hand-transcribed from Rev. Proc. 2018-57 (the IRS document
-- that authorized the 2019 inflation adjustments under IRC s.1(j)(2)) and
-- cross-checked against the printed IRS Pub 17 (2019) tax tables.
--
-- AUTHORITATIVE SOURCES
-- ---------------------
--   Rev. Proc. 2018-57 (released 2018-11-15), full text:
--     https://www.irs.gov/pub/irs-drop/rp-18-57.pdf
--   FICA SS wage base $132,900:
--     SSA Fact Sheet 2018-10-11 (https://www.ssa.gov/oact/cola/cbb.html).
--
-- ROADMAP NOTE
-- ------------
-- This seed advances Phase 5 (historical tax-table backfill) by one more
-- year. After this seed lands, the tax engine evaluates correctly for
-- TY 2019, TY 2020, TY 2022, TY 2023, TY 2024.
--
-- TY 2021 REMAINS BLOCKED on the ARPA two-stage CTC phaseout schema
-- migration (see work_left.txt "TY 2021 ARPA blocker"). The
-- substrate-honesty test guard for year=2021 remains in place after
-- this seed lands.
--
-- TY 2019 STRUCTURAL NOTES
-- ------------------------
-- (1) HOH 32%-bracket floor is $160,700, NOT $160,725 (the Single/MFS
--     32% floor). This $25 cross-status divergence is a TCJA-era
--     rounding quirk under IRC s.1(j)(2)(B) where the HOH and Single
--     bracket floors are computed from independent inflation factors.
--     The TestPhase5Ty2019 anchor class pins this difference explicitly
--     so a copy/paste from Table 3 (Single) into Table 2 (HOH) trips
--     a hand-walked assertion.
-- (2) MFS 37%-bracket floor is $306,175 (= 612,350 / 2 by IRC
--     s.1(j)(2)(D)), NOT $510,300 like Single/HOH and NOT $612,350
--     like MFJ. Same cross-status pin discipline as TY 2020 / TY 2022.
-- (3) TY 2019 is pure pre-ARPA TCJA: $2,000 base CTC / $1,400
--     refundable / $200K-$400K phaseout. Identical CTC structure to
--     TY 2018, 2020, 2022, 2023, 2024 (TY 2021 is the only divergent
--     year).
--
-- IDEMPOTENCY
-- -----------
-- All inserts use ON CONFLICT DO UPDATE so re-running the seed file is
-- safe and amendments are loud (the row's source_citation will change).
-- ============================================================================

-- ----------------------------------------------------------------------------
-- ref.irs_federal_brackets   (TY 2019 = Rev. Proc. 2018-57, Section 3.01)
-- ----------------------------------------------------------------------------

INSERT INTO ref.irs_federal_brackets
    (tax_year, filing_status, bracket_ord, bracket_floor, marginal_rate,
     source_url, source_citation)
VALUES
    -- Single  (Rev. Proc. 2018-57, s.3.01, Table 3)
    (2019, 'single', 1,        0.00, 0.10000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 3 (Unmarried Individuals)'),
    (2019, 'single', 2,     9700.00, 0.12000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 3 (Unmarried Individuals)'),
    (2019, 'single', 3,    39475.00, 0.22000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 3 (Unmarried Individuals)'),
    (2019, 'single', 4,    84200.00, 0.24000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 3 (Unmarried Individuals)'),
    (2019, 'single', 5,   160725.00, 0.32000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 3 (Unmarried Individuals)'),
    (2019, 'single', 6,   204100.00, 0.35000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 3 (Unmarried Individuals)'),
    (2019, 'single', 7,   510300.00, 0.37000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 3 (Unmarried Individuals)'),

    -- Married Filing Jointly  (Rev. Proc. 2018-57, s.3.01, Table 1)
    (2019, 'mfj', 1,        0.00, 0.10000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 1 (MFJ + Surviving Spouses)'),
    (2019, 'mfj', 2,    19400.00, 0.12000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 1 (MFJ + Surviving Spouses)'),
    (2019, 'mfj', 3,    78950.00, 0.22000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 1 (MFJ + Surviving Spouses)'),
    (2019, 'mfj', 4,   168400.00, 0.24000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 1 (MFJ + Surviving Spouses)'),
    (2019, 'mfj', 5,   321450.00, 0.32000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 1 (MFJ + Surviving Spouses)'),
    (2019, 'mfj', 6,   408200.00, 0.35000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 1 (MFJ + Surviving Spouses)'),
    (2019, 'mfj', 7,   612350.00, 0.37000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 1 (MFJ + Surviving Spouses)'),

    -- Qualifying Surviving Spouse: same Table 1 as MFJ (s.3.01 header).
    (2019, 'qss', 1,        0.00, 0.10000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 1 (QSS = MFJ schedule)'),
    (2019, 'qss', 2,    19400.00, 0.12000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 1 (QSS = MFJ schedule)'),
    (2019, 'qss', 3,    78950.00, 0.22000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 1 (QSS = MFJ schedule)'),
    (2019, 'qss', 4,   168400.00, 0.24000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 1 (QSS = MFJ schedule)'),
    (2019, 'qss', 5,   321450.00, 0.32000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 1 (QSS = MFJ schedule)'),
    (2019, 'qss', 6,   408200.00, 0.35000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 1 (QSS = MFJ schedule)'),
    (2019, 'qss', 7,   612350.00, 0.37000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 1 (QSS = MFJ schedule)'),

    -- Married Filing Separately  (Rev. Proc. 2018-57, s.3.01, Table 4)
    -- Brackets 1-5 mirror Single (= MFJ / 2 by IRC s.1(j)(2)(D)); the
    -- top bracket diverges -- 37% starts at $306,175 = 612,350/2,
    -- DIFFERENT from Single's $510,300.
    (2019, 'mfs', 1,        0.00, 0.10000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 4 (MFS)'),
    (2019, 'mfs', 2,     9700.00, 0.12000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 4 (MFS)'),
    (2019, 'mfs', 3,    39475.00, 0.22000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 4 (MFS)'),
    (2019, 'mfs', 4,    84200.00, 0.24000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 4 (MFS)'),
    (2019, 'mfs', 5,   160725.00, 0.32000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 4 (MFS)'),
    (2019, 'mfs', 6,   204100.00, 0.35000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 4 (MFS)'),
    (2019, 'mfs', 7,   306175.00, 0.37000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 4 (MFS)'),

    -- Head of Household  (Rev. Proc. 2018-57, s.3.01, Table 2)
    -- HOH 32%-bracket floor is $160,700, $25 LESS than Single's $160,725.
    -- This rounding divergence is the TY 2019 cross-status quirk pinned
    -- by TestPhase5Ty2019::test_2019_hoh_32pct_floor_diverges_from_single.
    (2019, 'hoh', 1,        0.00, 0.10000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 2 (HOH)'),
    (2019, 'hoh', 2,    13850.00, 0.12000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 2 (HOH)'),
    (2019, 'hoh', 3,    52850.00, 0.22000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 2 (HOH)'),
    (2019, 'hoh', 4,    84200.00, 0.24000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 2 (HOH)'),
    (2019, 'hoh', 5,   160700.00, 0.32000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 2 (HOH); $160,700 NOT $160,725 -- TCJA rounding divergence vs Single'),
    (2019, 'hoh', 6,   204100.00, 0.35000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 2 (HOH)'),
    (2019, 'hoh', 7,   510300.00, 0.37000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.01 Table 2 (HOH)')
ON CONFLICT (tax_year, filing_status, bracket_ord) DO UPDATE SET
    bracket_floor   = EXCLUDED.bracket_floor,
    marginal_rate   = EXCLUDED.marginal_rate,
    source_url      = EXCLUDED.source_url,
    source_citation = EXCLUDED.source_citation;


-- ----------------------------------------------------------------------------
-- ref.irs_standard_deduction
--
-- 2019 (Rev. Proc. 2018-57 s.3.16(1) base + s.3.16(3) age/blind):
--   Base: Single $12,200  MFJ $24,400  MFS $12,200  HOH $18,350  QSS $24,400
--   Aged/blind addition under IRC s.63(f):
--     $1,300 (MFJ / MFS / QSS)
--     $1,650 (Single / HOH; "if also unmarried and not a surviving spouse")
-- ----------------------------------------------------------------------------

INSERT INTO ref.irs_standard_deduction
    (tax_year, filing_status, base_amount, additional_age_65, additional_blind,
     source_url, source_citation)
VALUES
    (2019, 'single', 12200.00, 1650.00, 1650.00,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.16(1) (base) + s.3.16(3) (age/blind add-on, $1,650 unmarried/non-surviving)'),
    (2019, 'mfj',    24400.00, 1300.00, 1300.00,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.16(1) (base) + s.3.16(3) (age/blind add-on, $1,300 MFJ)'),
    (2019, 'mfs',    12200.00, 1300.00, 1300.00,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.16(1) (base) + s.3.16(3) (age/blind add-on, $1,300 MFS)'),
    (2019, 'hoh',    18350.00, 1650.00, 1650.00,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.16(1) (base) + s.3.16(3) (age/blind add-on, $1,650 HOH unmarried)'),
    (2019, 'qss',    24400.00, 1300.00, 1300.00,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'Rev. Proc. 2018-57 s.3.16(1) (base) + s.3.16(3) (age/blind add-on, $1,300 QSS)')
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
-- 115-97 s.11041(a). Rev. Proc. 2018-57 reflects this in the
-- inflation-adjustment list.
-- ----------------------------------------------------------------------------

INSERT INTO ref.irs_personal_exemption
    (tax_year, amount, source_url, source_citation)
VALUES
    (2019, 0.00,
     'https://www.law.cornell.edu/uscode/text/26/151',
     'IRC s.151(d)(5)(A) (TCJA, P.L. 115-97 s.11041): exemption $0 for TY 2018-2025')
ON CONFLICT (tax_year) DO UPDATE SET
    amount          = EXCLUDED.amount,
    source_url      = EXCLUDED.source_url,
    source_citation = EXCLUDED.source_citation;


-- ----------------------------------------------------------------------------
-- ref.irs_child_tax_credit
--
-- TY 2019: pure pre-ARPA TCJA baseline.
--   $2,000/child under 17 per IRC s.24(h);
--   refundable max per Rev. Proc. 2018-57 s.3.05 = $1,400 per child;
--   phase-out at AGI $200K (single/HOH/MFS) or $400K (MFJ) at 5%.
--
-- The amount_under_6 and amount_6_to_17 columns hold the SAME value
-- for 2019 because TCJA did not split CTC by age. The split exists in
-- the schema specifically for ARPA TY 2021, which is the only year
-- (so far) where the two values differ -- and which remains blocked
-- on the schema migration described in work_left.txt.
-- ----------------------------------------------------------------------------

INSERT INTO ref.irs_child_tax_credit
    (tax_year,
     amount_under_6, amount_6_to_17, refundable_max_per_child,
     phaseout_threshold_single, phaseout_threshold_mfj, phaseout_rate,
     source_url, source_citation)
VALUES
    (2019,
     2000.00, 2000.00, 1400.00,
     200000.00, 400000.00, 0.05000,
     'https://www.irs.gov/pub/irs-drop/rp-18-57.pdf',
     'IRC s.24(h) (TCJA, $2,000 base); refundable max $1,400 per Rev. Proc. 2018-57 s.3.05')
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
-- TY 2019:
--   Social Security (OASDI) wage base = $132,900
--     (SSA Fact Sheet, "Contribution and Benefit Base", 2018-10-11).
--     The 2018->2019 jump from $128,400 -- the cross-year-correctness
--     pin against a copy/paste of the 2018 wage base.
--   OASDI rate (employee) = 6.2% per IRC s.3101(a) -- statutory.
--   Medicare (HI) rate = 1.45% per IRC s.3101(b)(1) -- statutory, no cap.
--   Additional Medicare 0.9% per IRC s.3101(b)(2) on wages over
--     $200K (single, HOH, MFS) or $250K (MFJ, QSS) -- statutory thresholds
--     fixed by ACA s.9015 since TY 2013, NOT inflation-adjusted.
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
    (2019,
     0.06200, 132900.00,
     0.01450,
     0.00900, 200000.00, 250000.00,
     'https://www.ssa.gov/oact/cola/cbb.html',
     'SSA Contribution and Benefit Base: SS wage base $132,900 for TY2019; Medicare add''l per IRC s.3101(b)(2)')
ON CONFLICT (tax_year) DO UPDATE SET
    ss_employee_rate                     = EXCLUDED.ss_employee_rate,
    ss_wage_base                         = EXCLUDED.ss_wage_base,
    medicare_employee_rate               = EXCLUDED.medicare_employee_rate,
    additional_medicare_rate             = EXCLUDED.additional_medicare_rate,
    additional_medicare_threshold_single = EXCLUDED.additional_medicare_threshold_single,
    additional_medicare_threshold_mfj    = EXCLUDED.additional_medicare_threshold_mfj,
    source_url                           = EXCLUDED.source_url,
    source_citation                      = EXCLUDED.source_citation;
