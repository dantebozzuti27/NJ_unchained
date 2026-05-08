-- ============================================================================
-- Seed: 025_nj_state_tax_2016
-- NJ Schedule I/II identical to TY 2010-2017 (8.97% top above $500K, no
-- Millionaires-Tax). FIRST YEAR with 30% NJ EITC match (P.L. 2015 c.180,
-- raised from 25% TY2012-2015). NO veteran exemption (P.L. 2017 c.36 not
-- yet effective). Property-tax cap $10,000 per P.L. 1996 c.60.
-- Source: 2016 NJ-1040 Instructions (Tax Rate Schedules page 60).
-- ============================================================================
INSERT INTO ref.nj_state_brackets (tax_year, filing_status, bracket_ord, bracket_floor, marginal_rate, source_url, source_citation) VALUES
    (2016, 'single', 1,        0.00, 0.01400, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 Schedule I'),
    (2016, 'single', 2,    20000.00, 0.01750, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 Schedule I'),
    (2016, 'single', 3,    35000.00, 0.03500, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 Schedule I'),
    (2016, 'single', 4,    40000.00, 0.05525, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 Schedule I'),
    (2016, 'single', 5,    75000.00, 0.06370, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 Schedule I'),
    (2016, 'single', 6,   500000.00, 0.08970, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 Schedule I'),
    (2016, 'mfs', 1,        0.00, 0.01400, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 Schedule I'),
    (2016, 'mfs', 2,    20000.00, 0.01750, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 Schedule I'),
    (2016, 'mfs', 3,    35000.00, 0.03500, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 Schedule I'),
    (2016, 'mfs', 4,    40000.00, 0.05525, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 Schedule I'),
    (2016, 'mfs', 5,    75000.00, 0.06370, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 Schedule I'),
    (2016, 'mfs', 6,   500000.00, 0.08970, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 Schedule I'),
    (2016, 'mfj', 1,        0.00, 0.01400, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 Schedule II'),
    (2016, 'mfj', 2,    20000.00, 0.01750, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 Schedule II'),
    (2016, 'mfj', 3,    50000.00, 0.02450, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 Schedule II'),
    (2016, 'mfj', 4,    70000.00, 0.03500, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 Schedule II'),
    (2016, 'mfj', 5,    80000.00, 0.05525, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 Schedule II'),
    (2016, 'mfj', 6,   150000.00, 0.06370, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 Schedule II'),
    (2016, 'mfj', 7,   500000.00, 0.08970, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 Schedule II'),
    (2016, 'hoh', 1,        0.00, 0.01400, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 Schedule II'),
    (2016, 'hoh', 2,    20000.00, 0.01750, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 Schedule II'),
    (2016, 'hoh', 3,    50000.00, 0.02450, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 Schedule II'),
    (2016, 'hoh', 4,    70000.00, 0.03500, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 Schedule II'),
    (2016, 'hoh', 5,    80000.00, 0.05525, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 Schedule II'),
    (2016, 'hoh', 6,   150000.00, 0.06370, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 Schedule II'),
    (2016, 'hoh', 7,   500000.00, 0.08970, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 Schedule II'),
    (2016, 'qss', 1,        0.00, 0.01400, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 Schedule II'),
    (2016, 'qss', 2,    20000.00, 0.01750, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 Schedule II'),
    (2016, 'qss', 3,    50000.00, 0.02450, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 Schedule II'),
    (2016, 'qss', 4,    70000.00, 0.03500, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 Schedule II'),
    (2016, 'qss', 5,    80000.00, 0.05525, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 Schedule II'),
    (2016, 'qss', 6,   150000.00, 0.06370, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 Schedule II'),
    (2016, 'qss', 7,   500000.00, 0.08970, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 Schedule II')
ON CONFLICT (tax_year, filing_status, bracket_ord) DO UPDATE SET bracket_floor = EXCLUDED.bracket_floor, marginal_rate = EXCLUDED.marginal_rate, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

INSERT INTO ref.nj_state_personal_exemption (tax_year, exemption_kind, amount, source_url, source_citation) VALUES
    (2016, 'taxpayer',                       1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 NJSA 54A:3-1.1'),
    (2016, 'spouse',                         1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016'),
    (2016, 'dependent',                      1500.00, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016'),
    (2016, 'dependent_college_under_22',     1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016'),
    (2016, 'taxpayer_age_65_plus',           1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016'),
    (2016, 'spouse_age_65_plus',             1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016'),
    (2016, 'taxpayer_blind_disabled',        1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016'),
    (2016, 'spouse_blind_disabled',          1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016')
    -- NO veteran exemption (P.L. 2017 c.36 not yet effective).
ON CONFLICT (tax_year, exemption_kind) DO UPDATE SET amount = EXCLUDED.amount, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

INSERT INTO ref.nj_state_property_tax_deduction (tax_year, deduction_cap, alternative_credit, rent_property_tax_share, source_url, source_citation)
VALUES (2016, 10000.00, 50.00, 0.18, 'https://www.state.nj.us/treasury/taxation/pdf/2016/1040i.pdf', 'NJ-1040 TY2016 PTD; $10K cap (P.L. 1996 c.60); NJSA 54A:3A-17')
ON CONFLICT (tax_year) DO UPDATE SET deduction_cap = EXCLUDED.deduction_cap, alternative_credit = EXCLUDED.alternative_credit, rent_property_tax_share = EXCLUDED.rent_property_tax_share, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

INSERT INTO ref.nj_state_eitc_match (tax_year, match_rate, eligibility_note, source_url, source_citation)
VALUES (2016, 0.30000, 'NJEITC for workers 25-64 with qualifying children. Pre-P.L.2020 c.21 / P.L.2021 c.128 expansions.',
        'https://nj.gov/treasury/taxation/eitc/prioryear.shtml',
        'NJSA 54A:4-7; FIRST YEAR at 30% per P.L. 2015 c.180 (raised from 25% TY2012-2015)')
ON CONFLICT (tax_year) DO UPDATE SET match_rate = EXCLUDED.match_rate, eligibility_note = EXCLUDED.eligibility_note, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;
