-- ============================================================================
-- Seed: 015_nj_state_tax_2022
--
-- NJ Gross Income Tax reference data for tax year 2022.
-- Every row is hand-transcribed from the NJ Division of Taxation
-- "2022 Tax Rate Schedules" published in the NJ-1040 / NJ-1040X
-- instruction packets and cross-checked against the NJ Tax Rate
-- Schedules table on the NJ Treasury website.
--
-- AUTHORITATIVE SOURCES
-- ---------------------
--   NJ-1040 Instructions (2022) -- Tax Rate Schedules:
--     https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf
--     (NJ archives prior years at /pdf/<year>/1040i.pdf)
--   NJ-1040X Amended Return (2022) -- same Tax Rate Schedules:
--     https://www.nj.gov/treasury/taxation/pdf/other_forms/tgi-ee/2022/1040x.pdf
--   NJ Tax Rate Schedules portal:
--     https://www.nj.gov/treasury/taxation/taxtables.shtml
--   General NJ tax law:
--     NJSA 54A:2-1 (rates), NJSA 54A:3-1.1 (exemptions),
--     NJSA 54A:3A-17 (property-tax deduction).
--     P.L. 2020 c.95 (10.75% Millionaires' Tax bracket, retroactive to 2020).
--     P.L. 2020 c.21 (NJ EITC raised to 40%).
--     P.L. 2021 c.128 (NJEITC eligibility expanded to 18-24 with no kids).
--
-- BRACKET SHAPE NOTE
-- ------------------
-- NJ has TWO bracket schedules (this has been stable since 2004):
--   (A) Schedule I:  Single + Married Filing Separately   (7 brackets)
--   (B) Schedule II: MFJ + HOH + QSS                       (8 brackets)
-- Schedule II inserts an additional 2.45% bracket between the 1.75% and
-- 3.5% brackets of Schedule I; bracket FLOORS differ between the two
-- schedules (NJ does NOT use a 2x scaling rule like the federal MFS=Single
-- floors). The 10.75% top bracket was created by P.L. 2020 c.95
-- ("Millionaires' Tax", retroactive to 2020); both schedules carry it.
--
-- TY 2022 NJ tax rates and bracket FLOORS are IDENTICAL to TY 2023 and
-- TY 2024 (NJ does not inflation-adjust brackets; the schedule has only
-- changed when the legislature passes a new public law). Verified against
-- both the 2022 NJ-1040X (page 20) and the 2022 NJ-1040 instructions.
--
-- IDEMPOTENCY: re-run safe via ON CONFLICT DO UPDATE.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- ref.nj_state_brackets   (TY 2022)
-- ----------------------------------------------------------------------------

INSERT INTO ref.nj_state_brackets
    (tax_year, filing_status, bracket_ord, bracket_floor, marginal_rate,
     source_url, source_citation)
VALUES
    -- =====  TAX YEAR 2022, Schedule I: Single + MFS  =====
    (2022, 'single', 1,        0.00, 0.01400,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule I (Single/MFS)'),
    (2022, 'single', 2,    20000.00, 0.01750,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule I (Single/MFS)'),
    (2022, 'single', 3,    35000.00, 0.03500,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule I (Single/MFS)'),
    (2022, 'single', 4,    40000.00, 0.05525,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule I (Single/MFS)'),
    (2022, 'single', 5,    75000.00, 0.06370,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule I (Single/MFS)'),
    (2022, 'single', 6,   500000.00, 0.08970,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule I (Single/MFS)'),
    (2022, 'single', 7,  1000000.00, 0.10750,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule I (Single/MFS)'),
    -- MFS uses identical Schedule I brackets.
    (2022, 'mfs', 1,        0.00, 0.01400,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule I (Single/MFS)'),
    (2022, 'mfs', 2,    20000.00, 0.01750,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule I (Single/MFS)'),
    (2022, 'mfs', 3,    35000.00, 0.03500,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule I (Single/MFS)'),
    (2022, 'mfs', 4,    40000.00, 0.05525,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule I (Single/MFS)'),
    (2022, 'mfs', 5,    75000.00, 0.06370,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule I (Single/MFS)'),
    (2022, 'mfs', 6,   500000.00, 0.08970,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule I (Single/MFS)'),
    (2022, 'mfs', 7,  1000000.00, 0.10750,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule I (Single/MFS)'),

    -- =====  TAX YEAR 2022, Schedule II: MFJ + HOH + QSS  =====
    (2022, 'mfj', 1,        0.00, 0.01400,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2022, 'mfj', 2,    20000.00, 0.01750,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2022, 'mfj', 3,    50000.00, 0.02450,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2022, 'mfj', 4,    70000.00, 0.03500,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2022, 'mfj', 5,    80000.00, 0.05525,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2022, 'mfj', 6,   150000.00, 0.06370,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2022, 'mfj', 7,   500000.00, 0.08970,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2022, 'mfj', 8,  1000000.00, 0.10750,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    -- HOH uses Schedule II
    (2022, 'hoh', 1,        0.00, 0.01400,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2022, 'hoh', 2,    20000.00, 0.01750,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2022, 'hoh', 3,    50000.00, 0.02450,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2022, 'hoh', 4,    70000.00, 0.03500,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2022, 'hoh', 5,    80000.00, 0.05525,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2022, 'hoh', 6,   150000.00, 0.06370,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2022, 'hoh', 7,   500000.00, 0.08970,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2022, 'hoh', 8,  1000000.00, 0.10750,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    -- QSS uses Schedule II
    (2022, 'qss', 1,        0.00, 0.01400,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2022, 'qss', 2,    20000.00, 0.01750,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2022, 'qss', 3,    50000.00, 0.02450,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2022, 'qss', 4,    70000.00, 0.03500,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2022, 'qss', 5,    80000.00, 0.05525,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2022, 'qss', 6,   150000.00, 0.06370,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2022, 'qss', 7,   500000.00, 0.08970,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2022, 'qss', 8,  1000000.00, 0.10750,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Tax Rate Schedule II (MFJ/HOH/QSS)')
ON CONFLICT (tax_year, filing_status, bracket_ord) DO UPDATE SET
    bracket_floor   = EXCLUDED.bracket_floor,
    marginal_rate   = EXCLUDED.marginal_rate,
    source_url      = EXCLUDED.source_url,
    source_citation = EXCLUDED.source_citation;


-- ----------------------------------------------------------------------------
-- ref.nj_state_personal_exemption  (TY 2022)
--
-- Source: NJ-1040 Instructions, "Exemptions" section (lines 7-12).
-- Amounts are statute-fixed by NJSA 54A:3-1.1; the veteran exemption was
-- established at $6,000 by P.L. 2017 c.36 (effective TY2017+). None of
-- these have changed for TY 2022 vs TY 2023+2024.
-- ----------------------------------------------------------------------------

INSERT INTO ref.nj_state_personal_exemption
    (tax_year, exemption_kind, amount, source_url, source_citation)
VALUES
    (2022, 'taxpayer',                       1000.00,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 line 7 (Self) per NJSA 54A:3-1.1'),
    (2022, 'spouse',                         1000.00,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 line 7 (Spouse) per NJSA 54A:3-1.1'),
    (2022, 'dependent',                      1500.00,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 line 11 (Dependent) per NJSA 54A:3-1.1'),
    (2022, 'dependent_college_under_22',     1000.00,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 line 12 (College under 22) per NJSA 54A:3-1.1(b)'),
    (2022, 'taxpayer_age_65_plus',           1000.00,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 line 8 (Self age 65+) per NJSA 54A:3-1.1'),
    (2022, 'spouse_age_65_plus',             1000.00,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 line 8 (Spouse age 65+) per NJSA 54A:3-1.1'),
    (2022, 'taxpayer_blind_disabled',        1000.00,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 line 9 (Self blind/disabled) per NJSA 54A:3-1.1'),
    (2022, 'spouse_blind_disabled',          1000.00,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 line 9 (Spouse blind/disabled) per NJSA 54A:3-1.1'),
    (2022, 'veteran',                        6000.00,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 line 10 (Veteran exemption) per P.L. 2017 c.36')
ON CONFLICT (tax_year, exemption_kind) DO UPDATE SET
    amount          = EXCLUDED.amount,
    source_url      = EXCLUDED.source_url,
    source_citation = EXCLUDED.source_citation;


-- ----------------------------------------------------------------------------
-- ref.nj_state_property_tax_deduction (TY 2022)
--
-- Cap: $15,000 for tax years 2018+ per P.L. 2018 c.45 (was $10,000
-- pre-2018 under NJSA 54A:3A-17 as originally enacted).
-- Alternative refundable credit: $50 per NJSA 54A:3A-20.
-- Renter property-tax-equivalent: 18% of rent paid per NJSA 54A:3A-17.
-- All three values were unchanged from TY 2018 through TY 2024.
-- ----------------------------------------------------------------------------

INSERT INTO ref.nj_state_property_tax_deduction
    (tax_year, deduction_cap, alternative_credit, rent_property_tax_share,
     source_url, source_citation)
VALUES
    (2022, 15000.00, 50.00, 0.18,
     'https://www.nj.gov/treasury/taxation/pdf/2022/1040i.pdf',
     'NJ-1040 Instructions TY2022 Property Tax Deduction/Credit Worksheet; NJSA 54A:3A-17, P.L.2018 c.45')
ON CONFLICT (tax_year) DO UPDATE SET
    deduction_cap           = EXCLUDED.deduction_cap,
    alternative_credit      = EXCLUDED.alternative_credit,
    rent_property_tax_share = EXCLUDED.rent_property_tax_share,
    source_url              = EXCLUDED.source_url,
    source_citation         = EXCLUDED.source_citation;


-- ----------------------------------------------------------------------------
-- ref.nj_state_eitc_match (TY 2022)
--
-- Match rate 40% per P.L. 2020 c.21 (effective TY2020+).
-- Eligibility expanded to workers 18-24 with no qualifying child by
-- P.L. 2021 c.128 (effective TY2021+; TY 2022 inherits this expansion).
-- ----------------------------------------------------------------------------

INSERT INTO ref.nj_state_eitc_match
    (tax_year, match_rate, eligibility_note, source_url, source_citation)
VALUES
    (2022, 0.40000,
     'NJEITC available to workers 18-24 without qualifying children (P.L.2021 c.128); also available to ITIN-filing residents (P.L.2020 c.21).',
     'https://www.nj.gov/treasury/taxation/eitc/eitcinfo.shtml',
     'NJSA 54A:4-7 (rate); P.L.2020 c.21 (rate increase to 40%); P.L.2021 c.128 (age expansion)')
ON CONFLICT (tax_year) DO UPDATE SET
    match_rate       = EXCLUDED.match_rate,
    eligibility_note = EXCLUDED.eligibility_note,
    source_url       = EXCLUDED.source_url,
    source_citation  = EXCLUDED.source_citation;
