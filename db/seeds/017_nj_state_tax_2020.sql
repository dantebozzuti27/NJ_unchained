-- ============================================================================
-- Seed: 017_nj_state_tax_2020
--
-- NJ Gross Income Tax reference data for tax year 2020.
-- Every row is hand-transcribed from the NJ Division of Taxation
-- "2020 Tax Rate Schedules" published in the NJ-1040 / NJ-1040X
-- instruction packets and cross-checked against the NJ Tax Rate
-- Schedules table on the NJ Treasury website.
--
-- AUTHORITATIVE SOURCES
-- ---------------------
--   NJ-1040 Instructions (2020) -- Tax Rate Schedules:
--     https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf
--   NJ-1040X Amended Return (2020) -- same Tax Rate Schedules:
--     https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2020/1040x.pdf
--   NJ Income Tax - Important Changes for 2020 (announces P.L. 2020 c.95):
--     https://www.nj.gov/treasury/taxation/new2020.shtml
--   NJ Income Tax Rate and Withholding Instruction for income $1M-$5M:
--     https://www.nj.gov/treasury/taxation/git2020taxrate.shtml
--   General NJ tax law:
--     NJSA 54A:2-1 (rates), NJSA 54A:3-1.1 (exemptions),
--     NJSA 54A:3A-17 (property-tax deduction).
--     P.L. 2020 c.95 (lowered 10.75% bracket threshold from $5M to $1M,
--                     RETROACTIVE to January 1, 2020).
--     P.L. 2020 c.21 (NJ EITC raised to 40%, effective TY2020).
--
-- BRACKET SHAPE NOTE
-- ------------------
-- NJ has TWO bracket schedules (this has been stable since 2004):
--   (A) Schedule I:  Single + Married Filing Separately   (7 brackets)
--   (B) Schedule II: MFJ + HOH + QSS                       (8 brackets,
--                                                           extra 2.45%)
-- The 10.75% top bracket existed BEFORE 2020 but only above $5,000,000
-- (P.L. 2018 c.45). P.L. 2020 c.95 (signed Sept 29, 2020, retroactive
-- to Jan 1, 2020) LOWERED the 10.75% threshold to $1,000,000, expanding
-- the bracket's coverage. The 2020 NJ-1040 Tax Rate Schedules reflect
-- this retroactive change at the $1M floor (verified against
-- NJ-1040X 2020 page 19, lines 446-447 and 458-459).
--
-- TY 2020 NJ tax rates and bracket FLOORS are therefore IDENTICAL to
-- TY 2022, TY 2023, and TY 2024. The cross-year invariance is itself
-- a useful audit pin: a typo in this seed against any other seeded year
-- trips a specific bracket-walk assertion in TestPhase5Ty2020.
--
-- IDEMPOTENCY: re-run safe via ON CONFLICT DO UPDATE.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- ref.nj_state_brackets   (TY 2020)
-- ----------------------------------------------------------------------------

INSERT INTO ref.nj_state_brackets
    (tax_year, filing_status, bracket_ord, bracket_floor, marginal_rate,
     source_url, source_citation)
VALUES
    -- =====  TAX YEAR 2020, Schedule I: Single + MFS  =====
    (2020, 'single', 1,        0.00, 0.01400,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule I (Single/MFS); P.L.2020 c.95 lowered 10.75% to $1M retroactive to 2020-01-01'),
    (2020, 'single', 2,    20000.00, 0.01750,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule I (Single/MFS)'),
    (2020, 'single', 3,    35000.00, 0.03500,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule I (Single/MFS)'),
    (2020, 'single', 4,    40000.00, 0.05525,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule I (Single/MFS)'),
    (2020, 'single', 5,    75000.00, 0.06370,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule I (Single/MFS)'),
    (2020, 'single', 6,   500000.00, 0.08970,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule I (Single/MFS)'),
    (2020, 'single', 7,  1000000.00, 0.10750,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule I (Single/MFS); P.L.2020 c.95 (retroactive Millionaires'' Tax)'),
    -- MFS uses identical Schedule I brackets.
    (2020, 'mfs', 1,        0.00, 0.01400,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule I (Single/MFS)'),
    (2020, 'mfs', 2,    20000.00, 0.01750,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule I (Single/MFS)'),
    (2020, 'mfs', 3,    35000.00, 0.03500,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule I (Single/MFS)'),
    (2020, 'mfs', 4,    40000.00, 0.05525,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule I (Single/MFS)'),
    (2020, 'mfs', 5,    75000.00, 0.06370,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule I (Single/MFS)'),
    (2020, 'mfs', 6,   500000.00, 0.08970,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule I (Single/MFS)'),
    (2020, 'mfs', 7,  1000000.00, 0.10750,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule I (Single/MFS); P.L.2020 c.95'),

    -- =====  TAX YEAR 2020, Schedule II: MFJ + HOH + QSS  =====
    (2020, 'mfj', 1,        0.00, 0.01400,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2020, 'mfj', 2,    20000.00, 0.01750,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2020, 'mfj', 3,    50000.00, 0.02450,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2020, 'mfj', 4,    70000.00, 0.03500,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2020, 'mfj', 5,    80000.00, 0.05525,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2020, 'mfj', 6,   150000.00, 0.06370,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2020, 'mfj', 7,   500000.00, 0.08970,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2020, 'mfj', 8,  1000000.00, 0.10750,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule II (MFJ/HOH/QSS); P.L.2020 c.95'),
    -- HOH uses Schedule II
    (2020, 'hoh', 1,        0.00, 0.01400,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2020, 'hoh', 2,    20000.00, 0.01750,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2020, 'hoh', 3,    50000.00, 0.02450,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2020, 'hoh', 4,    70000.00, 0.03500,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2020, 'hoh', 5,    80000.00, 0.05525,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2020, 'hoh', 6,   150000.00, 0.06370,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2020, 'hoh', 7,   500000.00, 0.08970,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2020, 'hoh', 8,  1000000.00, 0.10750,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule II (MFJ/HOH/QSS); P.L.2020 c.95'),
    -- QSS uses Schedule II
    (2020, 'qss', 1,        0.00, 0.01400,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2020, 'qss', 2,    20000.00, 0.01750,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2020, 'qss', 3,    50000.00, 0.02450,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2020, 'qss', 4,    70000.00, 0.03500,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2020, 'qss', 5,    80000.00, 0.05525,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2020, 'qss', 6,   150000.00, 0.06370,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2020, 'qss', 7,   500000.00, 0.08970,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2020, 'qss', 8,  1000000.00, 0.10750,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Tax Rate Schedule II (MFJ/HOH/QSS); P.L.2020 c.95')
ON CONFLICT (tax_year, filing_status, bracket_ord) DO UPDATE SET
    bracket_floor   = EXCLUDED.bracket_floor,
    marginal_rate   = EXCLUDED.marginal_rate,
    source_url      = EXCLUDED.source_url,
    source_citation = EXCLUDED.source_citation;


-- ----------------------------------------------------------------------------
-- ref.nj_state_personal_exemption  (TY 2020)
--
-- Source: NJ-1040 Instructions, "Exemptions" section (lines 7-12).
-- Amounts are statute-fixed by NJSA 54A:3-1.1; the veteran exemption was
-- raised to $6,000 by P.L. 2019 c.413 effective TY2019+ (was $3,000
-- for TY2017-2018 under P.L. 2017 c.36 -- this is the only Phase-5-era
-- year-on-year change in the NJ exemption table). For TY 2020 the
-- veteran exemption is $6,000, identical to TY 2022+.
-- ----------------------------------------------------------------------------

INSERT INTO ref.nj_state_personal_exemption
    (tax_year, exemption_kind, amount, source_url, source_citation)
VALUES
    (2020, 'taxpayer',                       1000.00,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 line 7 (Self) per NJSA 54A:3-1.1'),
    (2020, 'spouse',                         1000.00,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 line 7 (Spouse) per NJSA 54A:3-1.1'),
    (2020, 'dependent',                      1500.00,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 line 11 (Dependent) per NJSA 54A:3-1.1'),
    (2020, 'dependent_college_under_22',     1000.00,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 line 12 (College under 22) per NJSA 54A:3-1.1(b)'),
    (2020, 'taxpayer_age_65_plus',           1000.00,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 line 8 (Self age 65+) per NJSA 54A:3-1.1'),
    (2020, 'spouse_age_65_plus',             1000.00,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 line 8 (Spouse age 65+) per NJSA 54A:3-1.1'),
    (2020, 'taxpayer_blind_disabled',        1000.00,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 line 9 (Self blind/disabled) per NJSA 54A:3-1.1'),
    (2020, 'spouse_blind_disabled',          1000.00,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 line 9 (Spouse blind/disabled) per NJSA 54A:3-1.1'),
    (2020, 'veteran',                        6000.00,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 line 10 (Veteran exemption) per P.L. 2019 c.413 (raised from $3,000 in P.L. 2017 c.36)')
ON CONFLICT (tax_year, exemption_kind) DO UPDATE SET
    amount          = EXCLUDED.amount,
    source_url      = EXCLUDED.source_url,
    source_citation = EXCLUDED.source_citation;


-- ----------------------------------------------------------------------------
-- ref.nj_state_property_tax_deduction (TY 2020)
--
-- Cap: $15,000 for tax years 2018+ per P.L. 2018 c.45.
-- Alternative refundable credit: $50 per NJSA 54A:3A-20.
-- Renter property-tax-equivalent: 18% of rent paid per NJSA 54A:3A-17.
-- All three values were unchanged from TY 2018 through TY 2024.
-- ----------------------------------------------------------------------------

INSERT INTO ref.nj_state_property_tax_deduction
    (tax_year, deduction_cap, alternative_credit, rent_property_tax_share,
     source_url, source_citation)
VALUES
    (2020, 15000.00, 50.00, 0.18,
     'https://www.nj.gov/treasury/taxation/pdf/2020/1040i.pdf',
     'NJ-1040 Instructions TY2020 Property Tax Deduction/Credit Worksheet; NJSA 54A:3A-17, P.L.2018 c.45')
ON CONFLICT (tax_year) DO UPDATE SET
    deduction_cap           = EXCLUDED.deduction_cap,
    alternative_credit      = EXCLUDED.alternative_credit,
    rent_property_tax_share = EXCLUDED.rent_property_tax_share,
    source_url              = EXCLUDED.source_url,
    source_citation         = EXCLUDED.source_citation;


-- ----------------------------------------------------------------------------
-- ref.nj_state_eitc_match (TY 2020)
--
-- Match rate 40% per P.L. 2020 c.21, EFFECTIVE TY 2020 (the rate jumped
-- from 39% in TY 2019 to 40% in TY 2020 -- this is the only Phase-5-era
-- NJ-EITC year-on-year change).
-- The age-expansion to workers 18-24 with no qualifying child was added
-- by P.L. 2021 c.128 effective TY 2021+, so for TY 2020 the eligibility
-- note reflects the pre-expansion rule (workers 25-64 with qualifying
-- children OR ITIN-filer per P.L. 2020 c.21).
-- ----------------------------------------------------------------------------

INSERT INTO ref.nj_state_eitc_match
    (tax_year, match_rate, eligibility_note, source_url, source_citation)
VALUES
    (2020, 0.40000,
     'NJEITC available to ITIN-filing residents (P.L.2020 c.21). The 18-24-without-qualifying-children expansion (P.L.2021 c.128) was NOT yet effective in TY 2020.',
     'https://www.nj.gov/treasury/taxation/eitc/eitcinfo.shtml',
     'NJSA 54A:4-7 (rate); P.L.2020 c.21 (rate increase to 40%, ITIN expansion, effective TY2020)')
ON CONFLICT (tax_year) DO UPDATE SET
    match_rate       = EXCLUDED.match_rate,
    eligibility_note = EXCLUDED.eligibility_note,
    source_url       = EXCLUDED.source_url,
    source_citation  = EXCLUDED.source_citation;
