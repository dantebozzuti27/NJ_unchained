-- ============================================================================
-- Seed: 016_irs_federal_tax_2020
--
-- Federal income-tax reference data for tax year 2020.
-- Every row is hand-transcribed from Rev. Proc. 2019-44 (the IRS document
-- that authorized the 2020 inflation adjustments under IRC s.1(j)(2)) and
-- cross-checked against the published IRS tax tables in Pub 17 (2020).
--
-- AUTHORITATIVE SOURCES
-- ---------------------
--   Rev. Proc. 2019-44 (released 2019-11-18), full text:
--     https://www.irs.gov/pub/irs-drop/rp-19-44.pdf
--   FICA SS wage base $137,700:
--     SSA Fact Sheet 2019-10-10 (https://www.ssa.gov/oact/cola/cbb.html).
--
-- ROADMAP NOTE
-- ------------
-- This seed advances Phase 5 (historical tax-table backfill) by one more
-- year. After this seed lands, the tax engine evaluates correctly for
-- TY 2020, TY 2022, TY 2023, TY 2024.
--
-- TY 2021 IS DELIBERATELY SKIPPED because ARPA (P.L. 117-2) replaced
-- the IRC s.24 CTC for that year ONLY with a two-stage phaseout
-- structure that the existing ref.irs_child_tax_credit schema cannot
-- express substrate-honestly:
--   (a) ARPA primary phaseout: $1,000 (under-6) or $1,000 (6-17) bonus
--       per child reduced at $50/$1,000 = 5% from $75K Single / $112,500
--       HOH / $150K MFJ thresholds, draining the bonus to the TCJA $2,000
--       base.
--   (b) ARPA secondary phaseout: the residual $2,000 base reduced at
--       5% from $200K Single/HOH/MFS / $400K MFJ thresholds, identical to
--       TCJA 2018+.
-- The current schema has ONE phaseout pair; encoding TY 2021 with EITHER
-- threshold pair would silently produce wrong-but-plausible CTC values
-- in the $75K-$400K AGI range, which the spec §10 explicitly names as
-- the credibility-killer ("'good enough tax assumptions' will destroy
-- credibility"). The right path is a future schema migration that adds
-- a second phaseout pair for ARPA's primary phaseout, plus a function
-- rewrite to apply both phaseouts in sequence; that work is filed in
-- work_left.txt under "TY 2021 ARPA blocker" and will land before any
-- TY 2021 seed file. Until then, derived.f_federal_child_tax_credit
-- correctly returns NULL for TY 2021, surfacing the data unavailability
-- to the user instead of computing a deceptive number.
--
-- IDEMPOTENCY
-- -----------
-- All inserts use ON CONFLICT DO UPDATE so re-running the seed file is
-- safe and amendments are loud (the row's source_citation will change).
-- ============================================================================

-- ----------------------------------------------------------------------------
-- ref.irs_federal_brackets   (TY 2020 = Rev. Proc. 2019-44, Section 3.01)
-- ----------------------------------------------------------------------------

INSERT INTO ref.irs_federal_brackets
    (tax_year, filing_status, bracket_ord, bracket_floor, marginal_rate,
     source_url, source_citation)
VALUES
    -- Single  (Rev. Proc. 2019-44, s.3.01, Table 3)
    (2020, 'single', 1,        0.00, 0.10000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 3 (Unmarried Individuals)'),
    (2020, 'single', 2,     9875.00, 0.12000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 3 (Unmarried Individuals)'),
    (2020, 'single', 3,    40125.00, 0.22000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 3 (Unmarried Individuals)'),
    (2020, 'single', 4,    85525.00, 0.24000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 3 (Unmarried Individuals)'),
    (2020, 'single', 5,   163300.00, 0.32000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 3 (Unmarried Individuals)'),
    (2020, 'single', 6,   207350.00, 0.35000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 3 (Unmarried Individuals)'),
    (2020, 'single', 7,   518400.00, 0.37000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 3 (Unmarried Individuals)'),

    -- Married Filing Jointly  (Rev. Proc. 2019-44, s.3.01, Table 1)
    (2020, 'mfj', 1,        0.00, 0.10000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 1 (MFJ + Surviving Spouses)'),
    (2020, 'mfj', 2,    19750.00, 0.12000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 1 (MFJ + Surviving Spouses)'),
    (2020, 'mfj', 3,    80250.00, 0.22000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 1 (MFJ + Surviving Spouses)'),
    (2020, 'mfj', 4,   171050.00, 0.24000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 1 (MFJ + Surviving Spouses)'),
    (2020, 'mfj', 5,   326600.00, 0.32000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 1 (MFJ + Surviving Spouses)'),
    (2020, 'mfj', 6,   414700.00, 0.35000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 1 (MFJ + Surviving Spouses)'),
    (2020, 'mfj', 7,   622050.00, 0.37000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 1 (MFJ + Surviving Spouses)'),

    -- Qualifying Surviving Spouse: same Table 1 as MFJ.
    (2020, 'qss', 1,        0.00, 0.10000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 1 (QSS = MFJ schedule)'),
    (2020, 'qss', 2,    19750.00, 0.12000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 1 (QSS = MFJ schedule)'),
    (2020, 'qss', 3,    80250.00, 0.22000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 1 (QSS = MFJ schedule)'),
    (2020, 'qss', 4,   171050.00, 0.24000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 1 (QSS = MFJ schedule)'),
    (2020, 'qss', 5,   326600.00, 0.32000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 1 (QSS = MFJ schedule)'),
    (2020, 'qss', 6,   414700.00, 0.35000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 1 (QSS = MFJ schedule)'),
    (2020, 'qss', 7,   622050.00, 0.37000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 1 (QSS = MFJ schedule)'),

    -- Married Filing Separately  (Rev. Proc. 2019-44, s.3.01, Table 4)
    -- Brackets 1-5 mirror Single (= MFJ / 2 by IRC s.1(j)(2)(D)); the
    -- top bracket diverges -- 37% starts at $311,025 = 622,050/2,
    -- DIFFERENT from Single's $518,400.
    (2020, 'mfs', 1,        0.00, 0.10000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 4 (MFS)'),
    (2020, 'mfs', 2,     9875.00, 0.12000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 4 (MFS)'),
    (2020, 'mfs', 3,    40125.00, 0.22000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 4 (MFS)'),
    (2020, 'mfs', 4,    85525.00, 0.24000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 4 (MFS)'),
    (2020, 'mfs', 5,   163300.00, 0.32000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 4 (MFS)'),
    (2020, 'mfs', 6,   207350.00, 0.35000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 4 (MFS)'),
    (2020, 'mfs', 7,   311025.00, 0.37000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 4 (MFS)'),

    -- Head of Household  (Rev. Proc. 2019-44, s.3.01, Table 2)
    (2020, 'hoh', 1,        0.00, 0.10000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 2 (HOH)'),
    (2020, 'hoh', 2,    14100.00, 0.12000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 2 (HOH)'),
    (2020, 'hoh', 3,    53700.00, 0.22000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 2 (HOH)'),
    (2020, 'hoh', 4,    85500.00, 0.24000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 2 (HOH)'),
    (2020, 'hoh', 5,   163300.00, 0.32000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 2 (HOH)'),
    (2020, 'hoh', 6,   207350.00, 0.35000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 2 (HOH)'),
    (2020, 'hoh', 7,   518400.00, 0.37000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.01 Table 2 (HOH)')
ON CONFLICT (tax_year, filing_status, bracket_ord) DO UPDATE SET
    bracket_floor   = EXCLUDED.bracket_floor,
    marginal_rate   = EXCLUDED.marginal_rate,
    source_url      = EXCLUDED.source_url,
    source_citation = EXCLUDED.source_citation;


-- ----------------------------------------------------------------------------
-- ref.irs_standard_deduction
--
-- 2020 (Rev. Proc. 2019-44 s.3.16(1) base + s.3.16(3) age/blind):
--   Base: Single $12,400  MFJ $24,800  MFS $12,400  HOH $18,650  QSS $24,800
--   Aged/blind addition under IRC s.63(f):
--     $1,300 (MFJ / MFS / QSS)
--     $1,650 (Single / HOH; "if also unmarried and not a surviving spouse")
-- ----------------------------------------------------------------------------

INSERT INTO ref.irs_standard_deduction
    (tax_year, filing_status, base_amount, additional_age_65, additional_blind,
     source_url, source_citation)
VALUES
    (2020, 'single', 12400.00, 1650.00, 1650.00,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.16(1) (base) + s.3.16(3) (age/blind add-on, $1,650 unmarried/non-surviving)'),
    (2020, 'mfj',    24800.00, 1300.00, 1300.00,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.16(1) (base) + s.3.16(3) (age/blind add-on, $1,300 MFJ)'),
    (2020, 'mfs',    12400.00, 1300.00, 1300.00,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.16(1) (base) + s.3.16(3) (age/blind add-on, $1,300 MFS)'),
    (2020, 'hoh',    18650.00, 1650.00, 1650.00,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.16(1) (base) + s.3.16(3) (age/blind add-on, $1,650 HOH unmarried)'),
    (2020, 'qss',    24800.00, 1300.00, 1300.00,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'Rev. Proc. 2019-44 s.3.16(1) (base) + s.3.16(3) (age/blind add-on, $1,300 QSS)')
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
-- 115-97 s.11041(a). Rev. Proc. 2019-44 reflects this in the
-- inflation-adjustment list.
-- ----------------------------------------------------------------------------

INSERT INTO ref.irs_personal_exemption
    (tax_year, amount, source_url, source_citation)
VALUES
    (2020, 0.00,
     'https://www.law.cornell.edu/uscode/text/26/151',
     'IRC s.151(d)(5)(A) (TCJA, P.L. 115-97 s.11041): exemption $0 for TY 2018-2025')
ON CONFLICT (tax_year) DO UPDATE SET
    amount          = EXCLUDED.amount,
    source_url      = EXCLUDED.source_url,
    source_citation = EXCLUDED.source_citation;


-- ----------------------------------------------------------------------------
-- ref.irs_child_tax_credit
--
-- TY 2020: pre-ARPA TCJA baseline.
--   $2,000/child under 17 per IRC s.24(h);
--   refundable max per Rev. Proc. 2019-44 s.3.05 = $1,400 per child;
--   phase-out at AGI $200K (single/HOH/MFS) or $400K (MFJ) at 5%.
--
-- The amount_under_6 and amount_6_to_17 columns hold the SAME value
-- for 2020 because TCJA did not split CTC by age. The split exists in
-- the schema specifically for ARPA TY 2021, which is the only year
-- (so far) where the two values differ.
-- ----------------------------------------------------------------------------

INSERT INTO ref.irs_child_tax_credit
    (tax_year,
     amount_under_6, amount_6_to_17, refundable_max_per_child,
     phaseout_threshold_single, phaseout_threshold_mfj, phaseout_rate,
     source_url, source_citation)
VALUES
    (2020,
     2000.00, 2000.00, 1400.00,
     200000.00, 400000.00, 0.05000,
     'https://www.irs.gov/pub/irs-drop/rp-19-44.pdf',
     'IRC s.24(h) (TCJA, $2,000 base); refundable max $1,400 per Rev. Proc. 2019-44 s.3.05')
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
-- TY 2020:
--   Social Security (OASDI) wage base = $137,700
--     (SSA Fact Sheet, "Contribution and Benefit Base", 2019-10-10).
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
    (2020,
     0.06200, 137700.00,
     0.01450,
     0.00900, 200000.00, 250000.00,
     'https://www.ssa.gov/oact/cola/cbb.html',
     'SSA Contribution and Benefit Base: SS wage base $137,700 for TY2020; Medicare add''l per IRC s.3101(b)(2)')
ON CONFLICT (tax_year) DO UPDATE SET
    ss_employee_rate                     = EXCLUDED.ss_employee_rate,
    ss_wage_base                         = EXCLUDED.ss_wage_base,
    medicare_employee_rate               = EXCLUDED.medicare_employee_rate,
    additional_medicare_rate             = EXCLUDED.additional_medicare_rate,
    additional_medicare_threshold_single = EXCLUDED.additional_medicare_threshold_single,
    additional_medicare_threshold_mfj    = EXCLUDED.additional_medicare_threshold_mfj,
    source_url                           = EXCLUDED.source_url,
    source_citation                      = EXCLUDED.source_citation;
