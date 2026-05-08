-- ============================================================================
-- Seed: 020_irs_federal_tax_2018
--
-- Federal income-tax reference data for tax year 2018.
-- Every row is hand-transcribed from Rev. Proc. 2018-18 (the IRS document
-- that MODIFIED AND SUPERSEDED parts of Rev. Proc. 2017-58 to reflect the
-- statutory changes in the Tax Cuts and Jobs Act, Pub. L. 115-97, signed
-- 2017-12-22).
--
-- AUTHORITATIVE SOURCES
-- ---------------------
--   Rev. Proc. 2018-18, 2018-10 I.R.B. 392 (released 2018-03-05),
--   re-published as part of IRB 2018-10:
--     https://www.irs.gov/irb/2018-10_IRB
--   Original Rev. Proc. 2017-58 (October 2017, pre-TCJA -- now
--   modified/superseded for the TCJA-affected items below):
--     https://www.irs.gov/pub/irs-drop/rp-17-58.pdf
--   FICA SS wage base $128,400:
--     SSA Fact Sheet 2017-10-13 (https://www.ssa.gov/oact/cola/cbb.html).
--
-- TY 2018 IS THE FIRST POST-TCJA YEAR
-- -----------------------------------
-- TCJA (P.L. 115-97 s.11001(a)) replaced the entire IRC s.1 bracket
-- structure for taxable years beginning after 2017-12-31 and before
-- 2026-01-01. New seven-rate ladder: 10/12/22/24/32/35/37 (was
-- 10/15/25/28/33/35/39.6 pre-TCJA). The bracket floors below were
-- set BY STATUTE for TY 2018 -- IRC s.1(j)(2)(A)-(D) -- not derived
-- from inflation indexing; this is the only year in the seeded
-- substrate where the bracket floors are statutory rather than
-- C-CPI-U-indexed (TY 2019+ used C-CPI-U inflation under the new
-- s.1(f)(3) added by TCJA s.11002).
--
-- ROADMAP NOTE
-- ------------
-- This seed advances Phase 5 (historical tax-table backfill) by one more
-- year. After this seed lands, the tax engine evaluates correctly for
-- TY 2018, TY 2019, TY 2020, TY 2022, TY 2023, TY 2024 (6 of 7 seeded
-- years for the TCJA era; TY 2021 remains blocked on ARPA schema work).
--
-- TY 2018 STRUCTURAL NOTES
-- ------------------------
-- (1) Aged/blind unmarried add-on is $1,600 (NOT $1,650 like TY 2019).
--     Rev. Proc. 2018-18 s.3.14(3) sets it at $1,600 for TY 2018; the
--     TY 2019 inflation adjustment in Rev. Proc. 2018-57 brings it to
--     $1,650. This is the smallest cross-year divergence in the
--     standard-deduction substrate ($50 per filer) but is correctly
--     pinned by the seed value.
-- (2) HOH 24% AND 32% bracket floors EQUAL the Single floors in TY 2018
--     ($82,500 and $157,500) -- there is NO $25 cross-status divergence
--     like TY 2019 ($160,725 vs $160,700) or TY 2020 ($85,525 vs
--     $85,500). TCJA initially set HOH and Single floors at the same
--     statutory values; subsequent C-CPI-U inflation indexing introduced
--     the small divergences in later years.
-- (3) MFS top floor is $300,000 (= 600,000 / 2) -- cross-status pin
--     same shape as later years.
-- (4) TY 2018 CTC: $2,000 base / $1,400 refundable max / $200K Single /
--     $400K MFJ at 5% phaseout -- IDENTICAL to TY 2019, TY 2020. The
--     refundable max stays at $1,400 from TY 2018-2020 because IRC
--     s.24(h)(5) only allows $100-increment inflation adjustments and
--     C-CPI-U did not exceed the $100 threshold until TY 2022.
--
-- IDEMPOTENCY: re-run safe via ON CONFLICT DO UPDATE.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- ref.irs_federal_brackets   (TY 2018 = Rev. Proc. 2018-18, Section 3.01)
-- ----------------------------------------------------------------------------

INSERT INTO ref.irs_federal_brackets
    (tax_year, filing_status, bracket_ord, bracket_floor, marginal_rate,
     source_url, source_citation)
VALUES
    -- Single  (Rev. Proc. 2018-18, s.3.01, Table 3 / IRC s.1(c))
    (2018, 'single', 1,        0.00, 0.10000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 3 / TCJA s.11001(a) IRC s.1(j)(2)(C)'),
    (2018, 'single', 2,     9525.00, 0.12000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 3'),
    (2018, 'single', 3,    38700.00, 0.22000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 3'),
    (2018, 'single', 4,    82500.00, 0.24000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 3'),
    (2018, 'single', 5,   157500.00, 0.32000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 3'),
    (2018, 'single', 6,   200000.00, 0.35000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 3'),
    (2018, 'single', 7,   500000.00, 0.37000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 3'),

    -- Married Filing Jointly  (Rev. Proc. 2018-18, s.3.01, Table 1)
    (2018, 'mfj', 1,        0.00, 0.10000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 1 / TCJA s.11001(a) IRC s.1(j)(2)(A)'),
    (2018, 'mfj', 2,    19050.00, 0.12000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 1 (MFJ + Surviving Spouses)'),
    (2018, 'mfj', 3,    77400.00, 0.22000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 1 (MFJ + Surviving Spouses)'),
    (2018, 'mfj', 4,   165000.00, 0.24000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 1 (MFJ + Surviving Spouses)'),
    (2018, 'mfj', 5,   315000.00, 0.32000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 1 (MFJ + Surviving Spouses)'),
    (2018, 'mfj', 6,   400000.00, 0.35000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 1 (MFJ + Surviving Spouses)'),
    (2018, 'mfj', 7,   600000.00, 0.37000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 1 (MFJ + Surviving Spouses)'),

    -- Qualifying Surviving Spouse: same Table 1 as MFJ (s.3.01 header).
    (2018, 'qss', 1,        0.00, 0.10000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 1 (QSS = MFJ schedule)'),
    (2018, 'qss', 2,    19050.00, 0.12000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 1 (QSS = MFJ schedule)'),
    (2018, 'qss', 3,    77400.00, 0.22000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 1 (QSS = MFJ schedule)'),
    (2018, 'qss', 4,   165000.00, 0.24000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 1 (QSS = MFJ schedule)'),
    (2018, 'qss', 5,   315000.00, 0.32000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 1 (QSS = MFJ schedule)'),
    (2018, 'qss', 6,   400000.00, 0.35000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 1 (QSS = MFJ schedule)'),
    (2018, 'qss', 7,   600000.00, 0.37000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 1 (QSS = MFJ schedule)'),

    -- Married Filing Separately  (Rev. Proc. 2018-18, s.3.01, Table 4)
    -- Brackets 1-5 mirror Single (= MFJ / 2 by IRC s.1(j)(2)(D));
    -- the top bracket diverges -- 37% starts at $300,000 = 600,000/2.
    (2018, 'mfs', 1,        0.00, 0.10000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 4 (MFS)'),
    (2018, 'mfs', 2,     9525.00, 0.12000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 4 (MFS)'),
    (2018, 'mfs', 3,    38700.00, 0.22000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 4 (MFS)'),
    (2018, 'mfs', 4,    82500.00, 0.24000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 4 (MFS)'),
    (2018, 'mfs', 5,   157500.00, 0.32000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 4 (MFS)'),
    (2018, 'mfs', 6,   200000.00, 0.35000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 4 (MFS)'),
    (2018, 'mfs', 7,   300000.00, 0.37000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 4 (MFS)'),

    -- Head of Household  (Rev. Proc. 2018-18, s.3.01, Table 2)
    -- TY 2018 has HOH 24% AND 32% bracket floors EQUAL to Single floors
    -- (no $25 divergence like TY 2019); TCJA initially set them
    -- at the same statutory values.
    (2018, 'hoh', 1,        0.00, 0.10000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 2 (HOH)'),
    (2018, 'hoh', 2,    13600.00, 0.12000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 2 (HOH)'),
    (2018, 'hoh', 3,    51800.00, 0.22000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 2 (HOH)'),
    (2018, 'hoh', 4,    82500.00, 0.24000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 2 (HOH); EQUAL to Single 24% floor in TY 2018'),
    (2018, 'hoh', 5,   157500.00, 0.32000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 2 (HOH); EQUAL to Single 32% floor in TY 2018'),
    (2018, 'hoh', 6,   200000.00, 0.35000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 2 (HOH)'),
    (2018, 'hoh', 7,   500000.00, 0.37000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.01 Table 2 (HOH)')
ON CONFLICT (tax_year, filing_status, bracket_ord) DO UPDATE SET
    bracket_floor   = EXCLUDED.bracket_floor,
    marginal_rate   = EXCLUDED.marginal_rate,
    source_url      = EXCLUDED.source_url,
    source_citation = EXCLUDED.source_citation;


-- ----------------------------------------------------------------------------
-- ref.irs_standard_deduction
--
-- 2018 (Rev. Proc. 2018-18 s.3.14):
--   Base: Single $12,000  MFJ $24,000  MFS $12,000  HOH $18,000  QSS $24,000
--     (TCJA s.11021(a) raised these above the pre-TCJA inflation-indexed
--      amounts; new amounts will be C-CPI-U-adjusted starting TY 2019.)
--   Aged/blind addition under IRC s.63(f) per Rev. Proc. 2018-18 s.3.14(3):
--     $1,300 (MFJ / MFS / QSS)
--     $1,600 (Single / HOH; "if also unmarried and not a surviving spouse")
--     -- $1,600 NOT $1,650 like TY 2019; this $50 single-year shift
--     -- reflects the C-CPI-U inflation factor between TY 2018 and TY 2019.
-- ----------------------------------------------------------------------------

INSERT INTO ref.irs_standard_deduction
    (tax_year, filing_status, base_amount, additional_age_65, additional_blind,
     source_url, source_citation)
VALUES
    (2018, 'single', 12000.00, 1600.00, 1600.00,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.14(1) (base) + s.3.14(3) (age/blind add-on, $1,600 unmarried/non-surviving)'),
    (2018, 'mfj',    24000.00, 1300.00, 1300.00,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.14(1) (base) + s.3.14(3) (age/blind add-on, $1,300 MFJ)'),
    (2018, 'mfs',    12000.00, 1300.00, 1300.00,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.14(1) (base) + s.3.14(3) (age/blind add-on, $1,300 MFS)'),
    (2018, 'hoh',    18000.00, 1600.00, 1600.00,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.14(1) (base) + s.3.14(3) (age/blind add-on, $1,600 HOH unmarried)'),
    (2018, 'qss',    24000.00, 1300.00, 1300.00,
     'https://www.irs.gov/irb/2018-10_IRB',
     'Rev. Proc. 2018-18 s.3.14(1) (base) + s.3.14(3) (age/blind add-on, $1,300 QSS)')
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
-- 115-97 s.11041(a). Rev. Proc. 2018-18 s.3.24 confirms the $0 amount.
-- TY 2018 IS THE FIRST YEAR with a $0 personal exemption (TY 2017 was
-- $4,050 per Rev. Proc. 2017-58 s.3.24 -- the most concrete TCJA tax
-- shift for low-income filers and the natural cross-year-divergence
-- pin against any future TY 2017 seed).
-- ----------------------------------------------------------------------------

INSERT INTO ref.irs_personal_exemption
    (tax_year, amount, source_url, source_citation)
VALUES
    (2018, 0.00,
     'https://www.law.cornell.edu/uscode/text/26/151',
     'IRC s.151(d)(5)(A) (TCJA, P.L. 115-97 s.11041): exemption $0 for TY 2018-2025; FIRST YEAR at $0 (was $4,050 in TY 2017)')
ON CONFLICT (tax_year) DO UPDATE SET
    amount          = EXCLUDED.amount,
    source_url      = EXCLUDED.source_url,
    source_citation = EXCLUDED.source_citation;


-- ----------------------------------------------------------------------------
-- ref.irs_child_tax_credit
--
-- TY 2018: pure post-TCJA pre-ARPA baseline. THE FIRST YEAR with TCJA
-- expansions:
--   $2,000/child under 17 per IRC s.24(h)(2) (was $1,000 pre-TCJA);
--   refundable max $1,400 per IRC s.24(h)(5) (was $1,000 fully refundable
--     pre-TCJA; the cap is statutory, inflation-indexed in $100
--     increments per s.24(h)(5)(A));
--   phase-out at AGI $200K (single/HOH/MFS) or $400K (MFJ) at 5% per
--     IRC s.24(b)(1) -- statutory thresholds, NOT inflation-indexed,
--     held constant 2018-2025.
--
-- Note that the new $500 nonrefundable credit for qualifying non-child
-- dependents (IRC s.24(h)(4)) is NOT modeled here -- the engine treats
-- only "qualifying children under 17" via this row. A future schema
-- expansion may add a separate row for the s.24(h)(4) "Credit for
-- Other Dependents" (ODC).
--
-- The amount_under_6 and amount_6_to_17 columns hold the SAME value
-- for 2018 because TCJA did not split CTC by age.
-- ----------------------------------------------------------------------------

INSERT INTO ref.irs_child_tax_credit
    (tax_year,
     amount_under_6, amount_6_to_17, refundable_max_per_child,
     phaseout_threshold_single, phaseout_threshold_mfj, phaseout_rate,
     source_url, source_citation)
VALUES
    (2018,
     2000.00, 2000.00, 1400.00,
     200000.00, 400000.00, 0.05000,
     'https://www.irs.gov/irb/2018-10_IRB',
     'IRC s.24(h)(2) (TCJA, $2,000 base, FIRST YEAR at this amount); refundable max $1,400 per IRC s.24(h)(5)')
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
-- TY 2018:
--   Social Security (OASDI) wage base = $128,400
--     (SSA Fact Sheet, "Contribution and Benefit Base", 2017-10-13).
--     The 2017->2018 jump from $127,200; uniquely $128,400 among all
--     seeded years -- the cross-year-correctness pin.
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
    (2018,
     0.06200, 128400.00,
     0.01450,
     0.00900, 200000.00, 250000.00,
     'https://www.ssa.gov/oact/cola/cbb.html',
     'SSA Contribution and Benefit Base: SS wage base $128,400 for TY2018; Medicare add''l per IRC s.3101(b)(2)')
ON CONFLICT (tax_year) DO UPDATE SET
    ss_employee_rate                     = EXCLUDED.ss_employee_rate,
    ss_wage_base                         = EXCLUDED.ss_wage_base,
    medicare_employee_rate               = EXCLUDED.medicare_employee_rate,
    additional_medicare_rate             = EXCLUDED.additional_medicare_rate,
    additional_medicare_threshold_single = EXCLUDED.additional_medicare_threshold_single,
    additional_medicare_threshold_mfj    = EXCLUDED.additional_medicare_threshold_mfj,
    source_url                           = EXCLUDED.source_url,
    source_citation                      = EXCLUDED.source_citation;
