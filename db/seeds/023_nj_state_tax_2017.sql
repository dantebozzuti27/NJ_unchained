-- ============================================================================
-- Seed: 023_nj_state_tax_2017
--
-- NJ Gross Income Tax reference data for tax year 2017.
--   - LAST YEAR with $10,000 property-tax cap (raised to $15K by P.L. 2018 c.45)
--   - LAST YEAR with no 10.75% Millionaires-Tax bracket (added TY 2018)
--   - LAST YEAR with 30% NJ EITC match (raised to 37% by P.L. 2018 c.45)
--   - FIRST YEAR with $3,000 veteran exemption (P.L. 2017 c.36)
--
-- AUTHORITATIVE SOURCES:
--   2017 NJ-1040 Tax Rate Schedules (NJ-1040 Instructions, page 60):
--     https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf
--   NJ EITC history: https://nj.gov/treasury/taxation/eitc/prioryear.shtml
--   NJ Veteran Exemption (P.L. 2017 c.36): introduced FIRST in TY 2017
--   NJSA 54A:2-1 (rates), 54A:3-1.1 (exemptions), 54A:3A-17 (PTD).
--
-- BRACKET INVARIANCE: Below the (non-existent) Millionaires-Tax band, NJ
-- Schedule I/II floors and rates have been UNCHANGED since 2004 (P.L.
-- 2004 c.40 created the 8.97% top bracket above $500K). So TY 2017 NJ
-- floors and rates EQUAL TY 2010-2018 below the top -- the cross-year
-- invariance pin protects the substrate against any partial-seed drift.
-- IDEMPOTENCY: re-run safe via ON CONFLICT DO UPDATE.
-- ============================================================================

INSERT INTO ref.nj_state_brackets (tax_year, filing_status, bracket_ord, bracket_floor, marginal_rate, source_url, source_citation) VALUES
    (2017, 'single', 1,        0.00, 0.01400, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 Tax Rate Schedule I (Single/MFS)'),
    (2017, 'single', 2,    20000.00, 0.01750, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 Schedule I'),
    (2017, 'single', 3,    35000.00, 0.03500, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 Schedule I'),
    (2017, 'single', 4,    40000.00, 0.05525, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 Schedule I'),
    (2017, 'single', 5,    75000.00, 0.06370, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 Schedule I'),
    (2017, 'single', 6,   500000.00, 0.08970, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 Schedule I; LAST YEAR with 8.97% as top rate (10.75% above $5M added TY 2018 by P.L.2018 c.45)'),
    (2017, 'mfs', 1,        0.00, 0.01400, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 Schedule I'),
    (2017, 'mfs', 2,    20000.00, 0.01750, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 Schedule I'),
    (2017, 'mfs', 3,    35000.00, 0.03500, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 Schedule I'),
    (2017, 'mfs', 4,    40000.00, 0.05525, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 Schedule I'),
    (2017, 'mfs', 5,    75000.00, 0.06370, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 Schedule I'),
    (2017, 'mfs', 6,   500000.00, 0.08970, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 Schedule I'),
    (2017, 'mfj', 1,        0.00, 0.01400, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 Schedule II (MFJ/HOH/QSS)'),
    (2017, 'mfj', 2,    20000.00, 0.01750, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 Schedule II'),
    (2017, 'mfj', 3,    50000.00, 0.02450, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 Schedule II'),
    (2017, 'mfj', 4,    70000.00, 0.03500, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 Schedule II'),
    (2017, 'mfj', 5,    80000.00, 0.05525, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 Schedule II'),
    (2017, 'mfj', 6,   150000.00, 0.06370, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 Schedule II'),
    (2017, 'mfj', 7,   500000.00, 0.08970, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 Schedule II'),
    (2017, 'hoh', 1,        0.00, 0.01400, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 Schedule II'),
    (2017, 'hoh', 2,    20000.00, 0.01750, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 Schedule II'),
    (2017, 'hoh', 3,    50000.00, 0.02450, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 Schedule II'),
    (2017, 'hoh', 4,    70000.00, 0.03500, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 Schedule II'),
    (2017, 'hoh', 5,    80000.00, 0.05525, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 Schedule II'),
    (2017, 'hoh', 6,   150000.00, 0.06370, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 Schedule II'),
    (2017, 'hoh', 7,   500000.00, 0.08970, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 Schedule II'),
    (2017, 'qss', 1,        0.00, 0.01400, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 Schedule II'),
    (2017, 'qss', 2,    20000.00, 0.01750, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 Schedule II'),
    (2017, 'qss', 3,    50000.00, 0.02450, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 Schedule II'),
    (2017, 'qss', 4,    70000.00, 0.03500, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 Schedule II'),
    (2017, 'qss', 5,    80000.00, 0.05525, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 Schedule II'),
    (2017, 'qss', 6,   150000.00, 0.06370, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 Schedule II'),
    (2017, 'qss', 7,   500000.00, 0.08970, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 Schedule II')
ON CONFLICT (tax_year, filing_status, bracket_ord) DO UPDATE SET
    bracket_floor = EXCLUDED.bracket_floor, marginal_rate = EXCLUDED.marginal_rate,
    source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

INSERT INTO ref.nj_state_personal_exemption (tax_year, exemption_kind, amount, source_url, source_citation) VALUES
    (2017, 'taxpayer',                       1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 (Self) NJSA 54A:3-1.1'),
    (2017, 'spouse',                         1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 (Spouse)'),
    (2017, 'dependent',                      1500.00, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 (Dependent)'),
    (2017, 'dependent_college_under_22',     1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 (College <22)'),
    (2017, 'taxpayer_age_65_plus',           1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 (Age 65+)'),
    (2017, 'spouse_age_65_plus',             1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 (Spouse 65+)'),
    (2017, 'taxpayer_blind_disabled',        1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 (Blind/disabled)'),
    (2017, 'spouse_blind_disabled',          1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf', 'NJ-1040 Instructions TY2017 (Spouse blind/disabled)'),
    (2017, 'veteran',                        3000.00, 'https://nj.gov/treasury/taxation/military/vetexemption.shtml', 'NJ-1040 TY2017 -- FIRST YEAR with veteran exemption (P.L. 2017 c.36)')
ON CONFLICT (tax_year, exemption_kind) DO UPDATE SET amount = EXCLUDED.amount,
    source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

INSERT INTO ref.nj_state_property_tax_deduction (tax_year, deduction_cap, alternative_credit, rent_property_tax_share, source_url, source_citation)
VALUES (2017, 10000.00, 50.00, 0.18, 'https://www.state.nj.us/treasury/taxation/pdf/2017/1040i.pdf',
        'NJ-1040 TY2017 PTD Worksheet -- LAST YEAR at $10K cap (P.L. 1996 c.60); raised to $15K by P.L. 2018 c.45 effective TY 2018; NJSA 54A:3A-17')
ON CONFLICT (tax_year) DO UPDATE SET deduction_cap = EXCLUDED.deduction_cap,
    alternative_credit = EXCLUDED.alternative_credit, rent_property_tax_share = EXCLUDED.rent_property_tax_share,
    source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

INSERT INTO ref.nj_state_eitc_match (tax_year, match_rate, eligibility_note, source_url, source_citation)
VALUES (2017, 0.30000, 'NJEITC for workers 25-64 with qualifying children. Pre-P.L.2020 c.21 / P.L.2021 c.128 expansions.',
        'https://nj.gov/treasury/taxation/eitc/prioryear.shtml',
        'NJSA 54A:4-7; phased: 25% TY2012-2015, 30% TY2016-2017 (LAST YEAR at 30% before P.L.2018 c.45 raised to 37% in TY 2018)')
ON CONFLICT (tax_year) DO UPDATE SET match_rate = EXCLUDED.match_rate,
    eligibility_note = EXCLUDED.eligibility_note,
    source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;
