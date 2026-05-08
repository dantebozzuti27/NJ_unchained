-- ============================================================================
-- Seed: 019_nj_state_tax_2019
--
-- NJ Gross Income Tax reference data for tax year 2019.
-- Every row is hand-transcribed from the NJ Division of Taxation
-- "2019 Tax Rate Schedules" published in the NJ-1040 / NJ-1040X
-- instruction packets. Verified against the structurally-identical
-- 2018 NJ-1040X Tax Rate Schedules (which carry the same statutory
-- shape because P.L. 2018 c.45 was the only change between TY 2018
-- and TY 2019 -- and that law was already in force from TY 2018).
--
-- AUTHORITATIVE SOURCES
-- ---------------------
--   2018 NJ-1040X Tax Rate Schedules (printed structure that applies
--   identically to TY 2019; pre-P.L.2020 c.95 retroactive Millionaires'
--   Tax expansion):
--     https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf
--     (page 19, lines 352-358 Schedule I, lines 364-371 Schedule II)
--   NJ Division of Taxation 2019 Income Tax Changes:
--     https://nj.gov/treasury/taxation/new2019.shtml
--   NJ Veteran Exemption increase to $6,000 (TY 2019):
--     https://nj.gov/treasury/taxation/military/vetexemption.shtml
--     (P.L. 2019 c.413 raised it from $3,000 in P.L. 2017 c.36)
--   NJ EITC 39% match (TY 2019):
--     https://nj.gov/treasury/taxation/eitc/prioryear.shtml
--     (P.L. 2018 c.45 phased the EITC match: 35% pre-2018, 37% TY2018,
--      39% TY2019, 40% TY2020+ per P.L. 2020 c.21)
--   General NJ tax law:
--     NJSA 54A:2-1 (rates), NJSA 54A:3-1.1 (exemptions),
--     NJSA 54A:3A-17 (property-tax deduction).
--     P.L. 2018 c.45 (added 10.75% bracket above $5,000,000 effective
--                     TY 2018; lowered the EITC match step to 39%
--                     for TY 2019).
--     P.L. 2020 c.95 (lowered 10.75% threshold to $1,000,000
--                     RETROACTIVE to 2020-01-01 -- DOES NOT APPLY to
--                     TY 2019; this is the cross-year divergence).
--
-- THE CRITICAL CROSS-YEAR DIVERGENCE
-- ----------------------------------
-- TY 2019 is the LAST PRE-LOWERED-MILLIONAIRES-TAX-THRESHOLD year in
-- the seeded substrate. The 10.75% top bracket in TY 2019 starts at
-- $5,000,000; in TY 2020+ it starts at $1,000,000. This means that
-- for taxpayers with income between $1M and $5M, TY 2019 NJ tax is
-- materially lower than TY 2020 NJ tax.
--
-- Worked example pinned in TestPhase5Ty2019::
--   test_2019_nj_single_2m_diverges_from_2020_by_17800:
--   At $2M Single NJ taxable income:
--     TY 2019: $164,273.75 (bracket walk under 8.97% from $500K-$5M)
--     TY 2020: $182,073.75 (bracket walk with 10.75% from $1M-$2M)
--     Diff:    $17,800.00  (= $1M [income in the $1M-$2M band]
--                            x 0.0178 [extra rate 10.75% - 8.97%])
-- This is the most rigorous test of the seed's correctness: a typo
-- in seed 019 that accidentally encoded TY 2020's $1M floor into
-- TY 2019 would COLLAPSE the 2019/2020 difference to $0 and trip
-- the test loudly.
--
-- For taxpayers below $1M income (the vast majority of NJ households),
-- TY 2019 NJ tax is IDENTICAL to TY 2020 / TY 2022 / TY 2024 because
-- Schedule I/II floors and rates have been stable since 2004 below
-- the Millionaires' Tax threshold.
--
-- BRACKET SHAPE NOTE
-- ------------------
-- NJ has TWO bracket schedules per tax year (stable since 2004):
--   (A) Schedule I:  Single + Married Filing Separately   (7 brackets in TY 2019)
--   (B) Schedule II: MFJ + HOH + QSS                       (8 brackets in TY 2019,
--                                                           extra 2.45%)
--
-- IDEMPOTENCY: re-run safe via ON CONFLICT DO UPDATE.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- ref.nj_state_brackets   (TY 2019)
-- ----------------------------------------------------------------------------

INSERT INTO ref.nj_state_brackets
    (tax_year, filing_status, bracket_ord, bracket_floor, marginal_rate,
     source_url, source_citation)
VALUES
    -- =====  TAX YEAR 2019, Schedule I: Single + MFS  =====
    -- Note: 10.75% top floor is $5,000,000 (P.L. 2018 c.45);
    --       P.L. 2020 c.95 lowered it to $1M but only retroactive
    --       to 2020-01-01, so TY 2019 keeps the $5M floor.
    (2019, 'single', 1,        0.00, 0.01400,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule I (Single/MFS); pre-P.L.2020 c.95 retroactive'),
    (2019, 'single', 2,    20000.00, 0.01750,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule I (Single/MFS)'),
    (2019, 'single', 3,    35000.00, 0.03500,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule I (Single/MFS)'),
    (2019, 'single', 4,    40000.00, 0.05525,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule I (Single/MFS)'),
    (2019, 'single', 5,    75000.00, 0.06370,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule I (Single/MFS)'),
    (2019, 'single', 6,   500000.00, 0.08970,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule I (Single/MFS)'),
    (2019, 'single', 7,  5000000.00, 0.10750,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule I (Single/MFS); 10.75% top floor at $5M (P.L.2018 c.45). P.L.2020 c.95 lowering to $1M is RETROACTIVE only to TY2020+ -- DOES NOT apply here'),
    -- MFS uses identical Schedule I brackets.
    (2019, 'mfs', 1,        0.00, 0.01400,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule I (Single/MFS)'),
    (2019, 'mfs', 2,    20000.00, 0.01750,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule I (Single/MFS)'),
    (2019, 'mfs', 3,    35000.00, 0.03500,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule I (Single/MFS)'),
    (2019, 'mfs', 4,    40000.00, 0.05525,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule I (Single/MFS)'),
    (2019, 'mfs', 5,    75000.00, 0.06370,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule I (Single/MFS)'),
    (2019, 'mfs', 6,   500000.00, 0.08970,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule I (Single/MFS)'),
    (2019, 'mfs', 7,  5000000.00, 0.10750,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule I (Single/MFS); $5M floor (pre-P.L.2020 c.95)'),

    -- =====  TAX YEAR 2019, Schedule II: MFJ + HOH + QSS  =====
    -- Same divergence: 10.75% top floor is $5,000,000 in TY 2019.
    (2019, 'mfj', 1,        0.00, 0.01400,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2019, 'mfj', 2,    20000.00, 0.01750,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2019, 'mfj', 3,    50000.00, 0.02450,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2019, 'mfj', 4,    70000.00, 0.03500,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2019, 'mfj', 5,    80000.00, 0.05525,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2019, 'mfj', 6,   150000.00, 0.06370,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2019, 'mfj', 7,   500000.00, 0.08970,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2019, 'mfj', 8,  5000000.00, 0.10750,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule II (MFJ/HOH/QSS); $5M floor (pre-P.L.2020 c.95)'),
    -- HOH uses Schedule II
    (2019, 'hoh', 1,        0.00, 0.01400,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2019, 'hoh', 2,    20000.00, 0.01750,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2019, 'hoh', 3,    50000.00, 0.02450,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2019, 'hoh', 4,    70000.00, 0.03500,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2019, 'hoh', 5,    80000.00, 0.05525,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2019, 'hoh', 6,   150000.00, 0.06370,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2019, 'hoh', 7,   500000.00, 0.08970,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2019, 'hoh', 8,  5000000.00, 0.10750,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule II (MFJ/HOH/QSS); $5M floor (pre-P.L.2020 c.95)'),
    -- QSS uses Schedule II
    (2019, 'qss', 1,        0.00, 0.01400,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2019, 'qss', 2,    20000.00, 0.01750,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2019, 'qss', 3,    50000.00, 0.02450,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2019, 'qss', 4,    70000.00, 0.03500,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2019, 'qss', 5,    80000.00, 0.05525,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2019, 'qss', 6,   150000.00, 0.06370,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2019, 'qss', 7,   500000.00, 0.08970,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2019, 'qss', 8,  5000000.00, 0.10750,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2019 Tax Rate Schedule II (MFJ/HOH/QSS); $5M floor (pre-P.L.2020 c.95)')
ON CONFLICT (tax_year, filing_status, bracket_ord) DO UPDATE SET
    bracket_floor   = EXCLUDED.bracket_floor,
    marginal_rate   = EXCLUDED.marginal_rate,
    source_url      = EXCLUDED.source_url,
    source_citation = EXCLUDED.source_citation;


-- ----------------------------------------------------------------------------
-- ref.nj_state_personal_exemption  (TY 2019)
--
-- Source: NJ-1040 Instructions, "Exemptions" section.
-- Amounts are statute-fixed by NJSA 54A:3-1.1.
-- TY 2019 IS THE FIRST YEAR with the $6,000 veteran exemption (per
-- P.L. 2019 c.413, signed 2020-01-21, effective TY 2019). For TY 2017
-- and TY 2018 the veteran exemption was $3,000 (introduced by P.L.
-- 2017 c.36); for TY 2019+ it is $6,000.
-- ----------------------------------------------------------------------------

INSERT INTO ref.nj_state_personal_exemption
    (tax_year, exemption_kind, amount, source_url, source_citation)
VALUES
    (2019, 'taxpayer',                       1000.00,
     'https://nj.gov/treasury/taxation/new2019.shtml',
     'NJ-1040 Instructions TY2019 (Self) per NJSA 54A:3-1.1'),
    (2019, 'spouse',                         1000.00,
     'https://nj.gov/treasury/taxation/new2019.shtml',
     'NJ-1040 Instructions TY2019 (Spouse) per NJSA 54A:3-1.1'),
    (2019, 'dependent',                      1500.00,
     'https://nj.gov/treasury/taxation/new2019.shtml',
     'NJ-1040 Instructions TY2019 (Dependent) per NJSA 54A:3-1.1'),
    (2019, 'dependent_college_under_22',     1000.00,
     'https://nj.gov/treasury/taxation/new2019.shtml',
     'NJ-1040 Instructions TY2019 (College under 22) per NJSA 54A:3-1.1(b)'),
    (2019, 'taxpayer_age_65_plus',           1000.00,
     'https://nj.gov/treasury/taxation/new2019.shtml',
     'NJ-1040 Instructions TY2019 (Self age 65+) per NJSA 54A:3-1.1'),
    (2019, 'spouse_age_65_plus',             1000.00,
     'https://nj.gov/treasury/taxation/new2019.shtml',
     'NJ-1040 Instructions TY2019 (Spouse age 65+) per NJSA 54A:3-1.1'),
    (2019, 'taxpayer_blind_disabled',        1000.00,
     'https://nj.gov/treasury/taxation/new2019.shtml',
     'NJ-1040 Instructions TY2019 (Self blind/disabled) per NJSA 54A:3-1.1'),
    (2019, 'spouse_blind_disabled',          1000.00,
     'https://nj.gov/treasury/taxation/new2019.shtml',
     'NJ-1040 Instructions TY2019 (Spouse blind/disabled) per NJSA 54A:3-1.1'),
    (2019, 'veteran',                        6000.00,
     'https://nj.gov/treasury/taxation/military/vetexemption.shtml',
     'NJ-1040 Instructions TY2019 (Veteran exemption) per P.L. 2019 c.413 (FIRST year at $6,000; was $3,000 in P.L. 2017 c.36 for TY 2017-2018)')
ON CONFLICT (tax_year, exemption_kind) DO UPDATE SET
    amount          = EXCLUDED.amount,
    source_url      = EXCLUDED.source_url,
    source_citation = EXCLUDED.source_citation;


-- ----------------------------------------------------------------------------
-- ref.nj_state_property_tax_deduction (TY 2019)
--
-- Cap: $15,000 for tax years 2018+ per P.L. 2018 c.45 (raised from
-- $10,000 in TY 2017 and prior).
-- Alternative refundable credit: $50 per NJSA 54A:3A-20.
-- Renter property-tax-equivalent: 18% of rent paid per NJSA 54A:3A-17.
-- All three values were unchanged from TY 2018 through TY 2024.
-- ----------------------------------------------------------------------------

INSERT INTO ref.nj_state_property_tax_deduction
    (tax_year, deduction_cap, alternative_credit, rent_property_tax_share,
     source_url, source_citation)
VALUES
    (2019, 15000.00, 50.00, 0.18,
     'https://nj.gov/treasury/taxation/new2019.shtml',
     'NJ-1040 Instructions TY2019 Property Tax Deduction/Credit Worksheet; NJSA 54A:3A-17, P.L.2018 c.45')
ON CONFLICT (tax_year) DO UPDATE SET
    deduction_cap           = EXCLUDED.deduction_cap,
    alternative_credit      = EXCLUDED.alternative_credit,
    rent_property_tax_share = EXCLUDED.rent_property_tax_share,
    source_url              = EXCLUDED.source_url,
    source_citation         = EXCLUDED.source_citation;


-- ----------------------------------------------------------------------------
-- ref.nj_state_eitc_match (TY 2019)
--
-- Match rate 39% per P.L. 2018 c.45 phased schedule:
--     pre-2018: 35% (P.L. 2008 c.110)
--     TY 2018:  37%
--     TY 2019:  39%   <-- THIS YEAR
--     TY 2020+: 40%   (P.L. 2020 c.21)
-- The 39% match is unique to TY 2019 in the seeded substrate; a
-- typo that copied the 40% rate from a TY 2020+ seed would be a
-- materially-wrong NJ EITC for every taxpayer at the bottom of the
-- income distribution, so the cross-year 39% pin in
-- TestPhase5Ty2019 catches it.
--
-- The age-expansion to workers 18-24 with no qualifying child
-- (P.L. 2021 c.128) and the ITIN-filer expansion (P.L. 2020 c.21)
-- are NOT yet effective in TY 2019; the eligibility note records
-- the pre-expansion rule.
-- ----------------------------------------------------------------------------

INSERT INTO ref.nj_state_eitc_match
    (tax_year, match_rate, eligibility_note, source_url, source_citation)
VALUES
    (2019, 0.39000,
     'NJEITC available to workers 25-64 with qualifying children (federal-EITC-eligible). NEITHER the ITIN-filer expansion (P.L.2020 c.21) NOR the 18-24-without-qualifying-children expansion (P.L.2021 c.128) is yet effective in TY 2019.',
     'https://nj.gov/treasury/taxation/eitc/prioryear.shtml',
     'NJSA 54A:4-7 (rate); P.L.2018 c.45 (phased schedule: 37% TY2018, 39% TY2019, 40% TY2020+)')
ON CONFLICT (tax_year) DO UPDATE SET
    match_rate       = EXCLUDED.match_rate,
    eligibility_note = EXCLUDED.eligibility_note,
    source_url       = EXCLUDED.source_url,
    source_citation  = EXCLUDED.source_citation;
