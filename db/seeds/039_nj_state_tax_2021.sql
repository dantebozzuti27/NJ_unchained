-- ============================================================================
-- Seed: 039_nj_state_tax_2021
--
-- NJ Gross Income Tax for TY 2021. The NJ schedule for TY 2021 is
-- IDENTICAL to TY 2020 because:
--   - P.L. 2020 c.95 (signed 2020-09-29, retroactive to 2020-01-01) added
--     the 10.75% bracket above $1M (down from $5M for TY 2018-2019);
--     unchanged for TY 2021.
--   - P.L. 2020 c.21 phased the NJ EITC to 40% for TY 2020+; unchanged.
--   - P.L. 2019 c.413 raised veteran exemption to $6,000 effective TY
--     2019; unchanged.
--   - P.L. 2018 c.45 set property-tax cap at $15,000; unchanged.
--
-- Source: NJ-1040 Instructions TY 2021
--   https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf
-- ============================================================================
INSERT INTO ref.nj_state_brackets (tax_year, filing_status, bracket_ord, bracket_floor, marginal_rate, source_url, source_citation) VALUES
    (2021, 'single', 1,        0.00, 0.01400, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule I'),
    (2021, 'single', 2,    20000.00, 0.01750, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule I'),
    (2021, 'single', 3,    35000.00, 0.03500, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule I'),
    (2021, 'single', 4,    40000.00, 0.05525, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule I'),
    (2021, 'single', 5,    75000.00, 0.06370, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule I'),
    (2021, 'single', 6,   500000.00, 0.08970, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule I'),
    (2021, 'single', 7,  1000000.00, 0.10750, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule I; 10.75% above $1M (P.L. 2020 c.95)'),
    (2021, 'mfs', 1,        0.00, 0.01400, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule I'),
    (2021, 'mfs', 2,    20000.00, 0.01750, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule I'),
    (2021, 'mfs', 3,    35000.00, 0.03500, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule I'),
    (2021, 'mfs', 4,    40000.00, 0.05525, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule I'),
    (2021, 'mfs', 5,    75000.00, 0.06370, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule I'),
    (2021, 'mfs', 6,   500000.00, 0.08970, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule I'),
    (2021, 'mfs', 7,  1000000.00, 0.10750, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule I'),
    (2021, 'mfj', 1,        0.00, 0.01400, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule II'),
    (2021, 'mfj', 2,    20000.00, 0.01750, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule II'),
    (2021, 'mfj', 3,    50000.00, 0.02450, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule II'),
    (2021, 'mfj', 4,    70000.00, 0.03500, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule II'),
    (2021, 'mfj', 5,    80000.00, 0.05525, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule II'),
    (2021, 'mfj', 6,   150000.00, 0.06370, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule II'),
    (2021, 'mfj', 7,   500000.00, 0.08970, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule II'),
    (2021, 'mfj', 8,  1000000.00, 0.10750, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule II'),
    (2021, 'hoh', 1,        0.00, 0.01400, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule II'),
    (2021, 'hoh', 2,    20000.00, 0.01750, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule II'),
    (2021, 'hoh', 3,    50000.00, 0.02450, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule II'),
    (2021, 'hoh', 4,    70000.00, 0.03500, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule II'),
    (2021, 'hoh', 5,    80000.00, 0.05525, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule II'),
    (2021, 'hoh', 6,   150000.00, 0.06370, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule II'),
    (2021, 'hoh', 7,   500000.00, 0.08970, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule II'),
    (2021, 'hoh', 8,  1000000.00, 0.10750, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule II'),
    (2021, 'qss', 1,        0.00, 0.01400, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule II'),
    (2021, 'qss', 2,    20000.00, 0.01750, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule II'),
    (2021, 'qss', 3,    50000.00, 0.02450, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule II'),
    (2021, 'qss', 4,    70000.00, 0.03500, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule II'),
    (2021, 'qss', 5,    80000.00, 0.05525, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule II'),
    (2021, 'qss', 6,   150000.00, 0.06370, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule II'),
    (2021, 'qss', 7,   500000.00, 0.08970, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule II'),
    (2021, 'qss', 8,  1000000.00, 0.10750, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 Schedule II')
ON CONFLICT (tax_year, filing_status, bracket_ord) DO UPDATE SET bracket_floor = EXCLUDED.bracket_floor, marginal_rate = EXCLUDED.marginal_rate, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

INSERT INTO ref.nj_state_personal_exemption (tax_year, exemption_kind, amount, source_url, source_citation) VALUES
    (2021, 'taxpayer', 1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 NJSA 54A:3-1.1'),
    (2021, 'spouse', 1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021'),
    (2021, 'dependent', 1500.00, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021'),
    (2021, 'dependent_college_under_22', 1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021'),
    (2021, 'taxpayer_age_65_plus', 1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021'),
    (2021, 'spouse_age_65_plus', 1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021'),
    (2021, 'taxpayer_blind_disabled', 1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021'),
    (2021, 'spouse_blind_disabled', 1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021'),
    (2021, 'veteran', 6000.00, 'https://nj.gov/treasury/taxation/military/vetexemption.shtml', 'P.L. 2019 c.413 raised to $6,000 effective TY 2019')
ON CONFLICT (tax_year, exemption_kind) DO UPDATE SET amount = EXCLUDED.amount, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

INSERT INTO ref.nj_state_property_tax_deduction (tax_year, deduction_cap, alternative_credit, rent_property_tax_share, source_url, source_citation)
VALUES (2021, 15000.00, 50.00, 0.18, 'https://www.state.nj.us/treasury/taxation/pdf/2021/1040i.pdf', 'NJ-1040 TY2021 PTD; $15K cap (P.L. 2018 c.45); NJSA 54A:3A-17')
ON CONFLICT (tax_year) DO UPDATE SET deduction_cap = EXCLUDED.deduction_cap, alternative_credit = EXCLUDED.alternative_credit, rent_property_tax_share = EXCLUDED.rent_property_tax_share, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

INSERT INTO ref.nj_state_eitc_match (tax_year, match_rate, eligibility_note, source_url, source_citation)
VALUES (2021, 0.40000, 'NJEITC 40% (P.L. 2020 c.21 phased to 40% for TY 2020+).', 'https://nj.gov/treasury/taxation/eitc/prioryear.shtml', 'NJSA 54A:4-7; 40% match per P.L. 2020 c.21')
ON CONFLICT (tax_year) DO UPDATE SET match_rate = EXCLUDED.match_rate, eligibility_note = EXCLUDED.eligibility_note, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;
