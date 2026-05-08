-- ============================================================================
-- Seed: 027_nj_state_tax_2015
-- NJ Schedule I/II UNCHANGED 2010-2017. EITC 25% (P.L. 2012 restored from
-- P.L. 2010 c.27 austerity 20%). $10K PTD cap. No veteran exemption.
-- ============================================================================
INSERT INTO ref.nj_state_brackets (tax_year, filing_status, bracket_ord, bracket_floor, marginal_rate, source_url, source_citation) VALUES
    (2015, 'single', 1,        0.00, 0.01400, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 Schedule I'),
    (2015, 'single', 2,    20000.00, 0.01750, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 Schedule I'),
    (2015, 'single', 3,    35000.00, 0.03500, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 Schedule I'),
    (2015, 'single', 4,    40000.00, 0.05525, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 Schedule I'),
    (2015, 'single', 5,    75000.00, 0.06370, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 Schedule I'),
    (2015, 'single', 6,   500000.00, 0.08970, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 Schedule I'),
    (2015, 'mfs', 1,        0.00, 0.01400, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 Schedule I'),
    (2015, 'mfs', 2,    20000.00, 0.01750, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 Schedule I'),
    (2015, 'mfs', 3,    35000.00, 0.03500, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 Schedule I'),
    (2015, 'mfs', 4,    40000.00, 0.05525, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 Schedule I'),
    (2015, 'mfs', 5,    75000.00, 0.06370, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 Schedule I'),
    (2015, 'mfs', 6,   500000.00, 0.08970, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 Schedule I'),
    (2015, 'mfj', 1,        0.00, 0.01400, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 Schedule II'),
    (2015, 'mfj', 2,    20000.00, 0.01750, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 Schedule II'),
    (2015, 'mfj', 3,    50000.00, 0.02450, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 Schedule II'),
    (2015, 'mfj', 4,    70000.00, 0.03500, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 Schedule II'),
    (2015, 'mfj', 5,    80000.00, 0.05525, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 Schedule II'),
    (2015, 'mfj', 6,   150000.00, 0.06370, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 Schedule II'),
    (2015, 'mfj', 7,   500000.00, 0.08970, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 Schedule II'),
    (2015, 'hoh', 1,        0.00, 0.01400, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 Schedule II'),
    (2015, 'hoh', 2,    20000.00, 0.01750, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 Schedule II'),
    (2015, 'hoh', 3,    50000.00, 0.02450, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 Schedule II'),
    (2015, 'hoh', 4,    70000.00, 0.03500, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 Schedule II'),
    (2015, 'hoh', 5,    80000.00, 0.05525, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 Schedule II'),
    (2015, 'hoh', 6,   150000.00, 0.06370, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 Schedule II'),
    (2015, 'hoh', 7,   500000.00, 0.08970, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 Schedule II'),
    (2015, 'qss', 1,        0.00, 0.01400, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 Schedule II'),
    (2015, 'qss', 2,    20000.00, 0.01750, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 Schedule II'),
    (2015, 'qss', 3,    50000.00, 0.02450, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 Schedule II'),
    (2015, 'qss', 4,    70000.00, 0.03500, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 Schedule II'),
    (2015, 'qss', 5,    80000.00, 0.05525, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 Schedule II'),
    (2015, 'qss', 6,   150000.00, 0.06370, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 Schedule II'),
    (2015, 'qss', 7,   500000.00, 0.08970, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 Schedule II')
ON CONFLICT (tax_year, filing_status, bracket_ord) DO UPDATE SET bracket_floor = EXCLUDED.bracket_floor, marginal_rate = EXCLUDED.marginal_rate, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

INSERT INTO ref.nj_state_personal_exemption (tax_year, exemption_kind, amount, source_url, source_citation) VALUES
    (2015, 'taxpayer', 1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 NJSA 54A:3-1.1'),
    (2015, 'spouse', 1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015'),
    (2015, 'dependent', 1500.00, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015'),
    (2015, 'dependent_college_under_22', 1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015'),
    (2015, 'taxpayer_age_65_plus', 1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015'),
    (2015, 'spouse_age_65_plus', 1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015'),
    (2015, 'taxpayer_blind_disabled', 1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015'),
    (2015, 'spouse_blind_disabled', 1000.00, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015')
ON CONFLICT (tax_year, exemption_kind) DO UPDATE SET amount = EXCLUDED.amount, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

INSERT INTO ref.nj_state_property_tax_deduction (tax_year, deduction_cap, alternative_credit, rent_property_tax_share, source_url, source_citation)
VALUES (2015, 10000.00, 50.00, 0.18, 'https://www.state.nj.us/treasury/taxation/pdf/2015/1040i.pdf', 'NJ-1040 TY2015 PTD; $10K cap; NJSA 54A:3A-17')
ON CONFLICT (tax_year) DO UPDATE SET deduction_cap = EXCLUDED.deduction_cap, alternative_credit = EXCLUDED.alternative_credit, rent_property_tax_share = EXCLUDED.rent_property_tax_share, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

INSERT INTO ref.nj_state_eitc_match (tax_year, match_rate, eligibility_note, source_url, source_citation)
VALUES (2015, 0.25000, 'NJEITC for workers 25-64 with qualifying children.', 'https://nj.gov/treasury/taxation/eitc/prioryear.shtml', 'NJSA 54A:4-7; 25% match (P.L. 2012 restored from P.L. 2010 c.27 austerity 20%)')
ON CONFLICT (tax_year) DO UPDATE SET match_rate = EXCLUDED.match_rate, eligibility_note = EXCLUDED.eligibility_note, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;
