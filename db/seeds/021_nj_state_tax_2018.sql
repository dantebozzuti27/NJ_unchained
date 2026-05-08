-- ============================================================================
-- Seed: 021_nj_state_tax_2018
--
-- NJ Gross Income Tax reference data for tax year 2018.
-- Every row is hand-transcribed from the NJ Division of Taxation
-- "2018 Tax Rate Schedules" published in the NJ-1040 / NJ-1040X
-- instruction packets. Verified directly against the NJ-1040X 2018
-- amended-return PDF.
--
-- AUTHORITATIVE SOURCES
-- ---------------------
--   2018 NJ-1040X Tax Rate Schedules:
--     https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf
--     (page 19 -- printed Schedule I + Schedule II structure verified
--      directly during Phase 5c session against this PDF).
--   New Gross Income Tax Legislation Makes Changes for Tax Year 2018:
--     https://www.nj.gov/treasury/taxation/grossincometax.shtml
--   NJ Veteran Exemption history (TY 2017-2018 = $3,000;
--    TY 2019+ = $6,000 per P.L. 2019 c.413):
--     https://nj.gov/treasury/taxation/military/vetexemption.shtml
--   NJ EITC phased schedule (35%/37%/39%/40% for 2017/2018/2019/2020+):
--     https://nj.gov/treasury/taxation/eitc/prioryear.shtml
--   General NJ tax law:
--     NJSA 54A:2-1 (rates), NJSA 54A:3-1.1 (exemptions),
--     NJSA 54A:3A-17 (property-tax deduction).
--     P.L. 2018 c.45 (BIG package: added 10.75% bracket above $5M effective
--                     TY 2018; raised property-tax deduction cap from
--                     $10K to $15K effective TY 2018; phased EITC match
--                     to 37% TY 2018, 39% TY 2019, 40% TY 2020).
--     P.L. 2017 c.36 (introduced $3,000 veteran exemption, effective TY 2017).
--     P.L. 2020 c.95 (lowered 10.75% threshold to $1M, retroactive to
--                     TY 2020 -- DOES NOT APPLY to TY 2018).
--
-- TY 2018 IS THE FIRST YEAR FOR THREE NJ POLICY CHANGES
-- -----------------------------------------------------
-- (1) FIRST YEAR with the 10.75% Millionaires' Tax bracket above $5M
--     (P.L. 2018 c.45). Pre-TY 2018, the top bracket was 8.97% above
--     $500,000 (Schedule I) / $500,000 (Schedule II); TY 2018 added
--     a new 10.75% band starting at $5M. The same threshold remained
--     for TY 2018 and TY 2019; P.L. 2020 c.95 lowered it to $1M
--     retroactive to TY 2020.
--
-- (2) FIRST YEAR with the $15,000 property-tax deduction cap (was
--     $10,000 in TY 2017 and prior, per P.L. 1996 c.60). The
--     $5,000 increase was a partial NJ legislative response to the
--     federal SALT cap imposed by TCJA s.11042 (which limited federal
--     SALT itemization to $10,000 per return).
--
-- (3) FIRST YEAR with the 37% NJ EITC match (was 30% in TY 2016-2017
--     per P.L. 2015 c.180); part of the same P.L. 2018 c.45 that
--     phased to 39% in TY 2019 and 40% in TY 2020+.
--
-- BRACKET INVARIANCE
-- ------------------
-- Below the Millionaires' Tax threshold ($5M), TY 2018 NJ Schedule I/II
-- floors and rates are IDENTICAL to TY 2019, TY 2020, TY 2022, TY 2023,
-- TY 2024 because NJ does not inflation-adjust brackets and no
-- legislative change has occurred to the bottom 7 brackets since
-- the late 1990s. This means: NJ($75K, 'single', 2018) ==
-- NJ($75K, 'single', 2019) == NJ($75K, 'single', 2024) to the cent.
-- The cross-year invariance test in TestPhase5Ty2018 catches a typo
-- in any bottom-bracket floor.
--
-- TY 2018 vs TY 2019 vs TY 2020 at $2M
-- ------------------------------------
-- At $2M Single NJ taxable income:
--   TY 2018: $164,273.75 (10.75% floor at $5M, $2M is in 8.97% band)
--   TY 2019: $164,273.75 (IDENTICAL to TY 2018 -- same schedule)
--   TY 2020: $182,073.75 (10.75% floor at $1M, $1M-$2M at 10.75%)
-- TY 2018 and TY 2019 are equal at $2M; both differ from TY 2020 by
-- exactly $17,800 (the policy effect of P.L. 2020 c.95 at $2M).
--
-- IDEMPOTENCY: re-run safe via ON CONFLICT DO UPDATE.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- ref.nj_state_brackets   (TY 2018)
-- ----------------------------------------------------------------------------

INSERT INTO ref.nj_state_brackets
    (tax_year, filing_status, bracket_ord, bracket_floor, marginal_rate,
     source_url, source_citation)
VALUES
    -- =====  TAX YEAR 2018, Schedule I: Single + MFS  =====
    -- 10.75% top floor is $5,000,000 (P.L. 2018 c.45) -- FIRST YEAR
    -- this bracket existed.
    (2018, 'single', 1,        0.00, 0.01400,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule I (Single/MFS); FIRST YEAR with 10.75% above $5M (P.L.2018 c.45)'),
    (2018, 'single', 2,    20000.00, 0.01750,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule I (Single/MFS)'),
    (2018, 'single', 3,    35000.00, 0.03500,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule I (Single/MFS)'),
    (2018, 'single', 4,    40000.00, 0.05525,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule I (Single/MFS)'),
    (2018, 'single', 5,    75000.00, 0.06370,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule I (Single/MFS)'),
    (2018, 'single', 6,   500000.00, 0.08970,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule I (Single/MFS)'),
    (2018, 'single', 7,  5000000.00, 0.10750,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule I (Single/MFS); 10.75% top floor at $5M (P.L.2018 c.45). P.L.2020 c.95 lowering to $1M is retroactive only to TY2020+ -- DOES NOT apply here'),
    -- MFS uses identical Schedule I brackets.
    (2018, 'mfs', 1,        0.00, 0.01400,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule I (Single/MFS)'),
    (2018, 'mfs', 2,    20000.00, 0.01750,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule I (Single/MFS)'),
    (2018, 'mfs', 3,    35000.00, 0.03500,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule I (Single/MFS)'),
    (2018, 'mfs', 4,    40000.00, 0.05525,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule I (Single/MFS)'),
    (2018, 'mfs', 5,    75000.00, 0.06370,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule I (Single/MFS)'),
    (2018, 'mfs', 6,   500000.00, 0.08970,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule I (Single/MFS)'),
    (2018, 'mfs', 7,  5000000.00, 0.10750,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule I (Single/MFS); $5M floor (FIRST YEAR per P.L.2018 c.45)'),

    -- =====  TAX YEAR 2018, Schedule II: MFJ + HOH + QSS  =====
    -- Same divergence: 10.75% top floor is $5,000,000 in TY 2018.
    (2018, 'mfj', 1,        0.00, 0.01400,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2018, 'mfj', 2,    20000.00, 0.01750,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2018, 'mfj', 3,    50000.00, 0.02450,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2018, 'mfj', 4,    70000.00, 0.03500,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2018, 'mfj', 5,    80000.00, 0.05525,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2018, 'mfj', 6,   150000.00, 0.06370,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2018, 'mfj', 7,   500000.00, 0.08970,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2018, 'mfj', 8,  5000000.00, 0.10750,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule II (MFJ/HOH/QSS); $5M floor (FIRST YEAR per P.L.2018 c.45)'),
    -- HOH uses Schedule II
    (2018, 'hoh', 1,        0.00, 0.01400,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2018, 'hoh', 2,    20000.00, 0.01750,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2018, 'hoh', 3,    50000.00, 0.02450,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2018, 'hoh', 4,    70000.00, 0.03500,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2018, 'hoh', 5,    80000.00, 0.05525,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2018, 'hoh', 6,   150000.00, 0.06370,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2018, 'hoh', 7,   500000.00, 0.08970,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2018, 'hoh', 8,  5000000.00, 0.10750,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule II (MFJ/HOH/QSS); $5M floor (FIRST YEAR per P.L.2018 c.45)'),
    -- QSS uses Schedule II
    (2018, 'qss', 1,        0.00, 0.01400,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2018, 'qss', 2,    20000.00, 0.01750,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2018, 'qss', 3,    50000.00, 0.02450,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2018, 'qss', 4,    70000.00, 0.03500,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2018, 'qss', 5,    80000.00, 0.05525,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2018, 'qss', 6,   150000.00, 0.06370,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2018, 'qss', 7,   500000.00, 0.08970,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2018, 'qss', 8,  5000000.00, 0.10750,
     'https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2018/1040x.pdf',
     'NJ-1040 TY2018 Tax Rate Schedule II (MFJ/HOH/QSS); $5M floor (FIRST YEAR per P.L.2018 c.45)')
ON CONFLICT (tax_year, filing_status, bracket_ord) DO UPDATE SET
    bracket_floor   = EXCLUDED.bracket_floor,
    marginal_rate   = EXCLUDED.marginal_rate,
    source_url      = EXCLUDED.source_url,
    source_citation = EXCLUDED.source_citation;


-- ----------------------------------------------------------------------------
-- ref.nj_state_personal_exemption  (TY 2018)
--
-- Source: NJ-1040 Instructions, "Exemptions" section.
-- Amounts are statute-fixed by NJSA 54A:3-1.1.
-- TY 2018 IS THE LAST YEAR with the $3,000 veteran exemption (introduced
-- by P.L. 2017 c.36 effective TY 2017). P.L. 2019 c.413 raised it to
-- $6,000 effective TY 2019. The $3,000-vs-$6,000 cross-year divergence
-- is a useful audit pin: a typo that copied the TY 2019+ $6,000 amount
-- into the TY 2018 row would silently double the veteran exemption.
-- ----------------------------------------------------------------------------

INSERT INTO ref.nj_state_personal_exemption
    (tax_year, exemption_kind, amount, source_url, source_citation)
VALUES
    (2018, 'taxpayer',                       1000.00,
     'https://www.nj.gov/treasury/taxation/grossincometax.shtml',
     'NJ-1040 Instructions TY2018 (Self) per NJSA 54A:3-1.1'),
    (2018, 'spouse',                         1000.00,
     'https://www.nj.gov/treasury/taxation/grossincometax.shtml',
     'NJ-1040 Instructions TY2018 (Spouse) per NJSA 54A:3-1.1'),
    (2018, 'dependent',                      1500.00,
     'https://www.nj.gov/treasury/taxation/grossincometax.shtml',
     'NJ-1040 Instructions TY2018 (Dependent) per NJSA 54A:3-1.1'),
    (2018, 'dependent_college_under_22',     1000.00,
     'https://www.nj.gov/treasury/taxation/grossincometax.shtml',
     'NJ-1040 Instructions TY2018 (College under 22) per NJSA 54A:3-1.1(b)'),
    (2018, 'taxpayer_age_65_plus',           1000.00,
     'https://www.nj.gov/treasury/taxation/grossincometax.shtml',
     'NJ-1040 Instructions TY2018 (Self age 65+) per NJSA 54A:3-1.1'),
    (2018, 'spouse_age_65_plus',             1000.00,
     'https://www.nj.gov/treasury/taxation/grossincometax.shtml',
     'NJ-1040 Instructions TY2018 (Spouse age 65+) per NJSA 54A:3-1.1'),
    (2018, 'taxpayer_blind_disabled',        1000.00,
     'https://www.nj.gov/treasury/taxation/grossincometax.shtml',
     'NJ-1040 Instructions TY2018 (Self blind/disabled) per NJSA 54A:3-1.1'),
    (2018, 'spouse_blind_disabled',          1000.00,
     'https://www.nj.gov/treasury/taxation/grossincometax.shtml',
     'NJ-1040 Instructions TY2018 (Spouse blind/disabled) per NJSA 54A:3-1.1'),
    (2018, 'veteran',                        3000.00,
     'https://nj.gov/treasury/taxation/military/vetexemption.shtml',
     'NJ-1040 Instructions TY2018 (Veteran exemption) per P.L. 2017 c.36 (LAST year at $3,000; raised to $6,000 by P.L. 2019 c.413 effective TY 2019)')
ON CONFLICT (tax_year, exemption_kind) DO UPDATE SET
    amount          = EXCLUDED.amount,
    source_url      = EXCLUDED.source_url,
    source_citation = EXCLUDED.source_citation;


-- ----------------------------------------------------------------------------
-- ref.nj_state_property_tax_deduction (TY 2018)
--
-- Cap: $15,000 for tax years 2018+ per P.L. 2018 c.45 (raised from
--   $10,000 in TY 2017 and prior, P.L. 1996 c.60).
-- Alternative refundable credit: $50 per NJSA 54A:3A-20.
-- Renter property-tax-equivalent: 18% of rent paid per NJSA 54A:3A-17.
--
-- TY 2018 IS THE FIRST YEAR with the $15,000 cap; TY 2017 and earlier
-- had a $10,000 cap. The $5,000 increase was a partial NJ legislative
-- response to the federal SALT cap imposed by TCJA s.11042 (which
-- limited federal SALT itemization to $10,000 per return). Even with
-- the increase, NJ filers facing $20K+ property taxes still hit
-- the $15K NJ cap; the gap is the substrate-honest signal of
-- "what is left over after the legislature's partial response".
-- ----------------------------------------------------------------------------

INSERT INTO ref.nj_state_property_tax_deduction
    (tax_year, deduction_cap, alternative_credit, rent_property_tax_share,
     source_url, source_citation)
VALUES
    (2018, 15000.00, 50.00, 0.18,
     'https://www.nj.gov/treasury/taxation/grossincometax.shtml',
     'NJ-1040 Instructions TY2018 Property Tax Deduction/Credit Worksheet; FIRST YEAR with $15K cap per P.L.2018 c.45 (was $10K in TY2017 and prior); NJSA 54A:3A-17')
ON CONFLICT (tax_year) DO UPDATE SET
    deduction_cap           = EXCLUDED.deduction_cap,
    alternative_credit      = EXCLUDED.alternative_credit,
    rent_property_tax_share = EXCLUDED.rent_property_tax_share,
    source_url              = EXCLUDED.source_url,
    source_citation         = EXCLUDED.source_citation;


-- ----------------------------------------------------------------------------
-- ref.nj_state_eitc_match (TY 2018)
--
-- Match rate 37% per P.L. 2018 c.45 phased schedule. Full NJ EITC history:
--     TY 2008-2009: 25% (P.L. 2008 c.110 raised from 17.5% to 25%)
--     TY 2010-2011: 20% (P.L. 2010 c.27 austerity cut from 25% to 20%)
--     TY 2012-2015: 25% (restored)
--     TY 2016-2017: 30% (raised by P.L. 2015 c.180)
--     TY 2018:      37% <-- THIS YEAR (FIRST YEAR of the bump from 30%)
--     TY 2019:      39%
--     TY 2020+:     40% (P.L. 2020 c.21)
-- The 37% match is unique to TY 2018 in the seeded substrate; a typo
-- that copied 39% from TY 2019 or 40% from TY 2020+ would be a
-- materially-wrong NJ EITC for every taxpayer at the bottom of the
-- income distribution.
--
-- ITIN-filer expansion (P.L.2020 c.21) and 18-24-no-qualifying-child
-- expansion (P.L.2021 c.128) NOT effective in TY 2018.
-- ----------------------------------------------------------------------------

INSERT INTO ref.nj_state_eitc_match
    (tax_year, match_rate, eligibility_note, source_url, source_citation)
VALUES
    (2018, 0.37000,
     'NJEITC available to workers 25-64 with qualifying children (federal-EITC-eligible). NEITHER the ITIN-filer expansion (P.L.2020 c.21) NOR the 18-24-without-qualifying-children expansion (P.L.2021 c.128) is yet effective in TY 2018.',
     'https://nj.gov/treasury/taxation/eitc/prioryear.shtml',
     'NJSA 54A:4-7 (rate); P.L.2018 c.45 (phased schedule: 37% TY2018 (FIRST YEAR of bump from 30%), 39% TY2019, 40% TY2020+)')
ON CONFLICT (tax_year) DO UPDATE SET
    match_rate       = EXCLUDED.match_rate,
    eligibility_note = EXCLUDED.eligibility_note,
    source_url       = EXCLUDED.source_url,
    source_citation  = EXCLUDED.source_citation;
