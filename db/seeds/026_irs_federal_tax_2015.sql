-- ============================================================================
-- Seed: 026_irs_federal_tax_2015
-- Source: Rev. Proc. 2014-61 (https://www.irs.gov/pub/irs-drop/rp-14-61.pdf)
-- ATRA-era 7-bracket ladder; SS wage base $118,500.
-- ============================================================================
INSERT INTO ref.irs_federal_brackets (tax_year, filing_status, bracket_ord, bracket_floor, marginal_rate, source_url, source_citation) VALUES
    (2015, 'single', 1,        0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.01 Table 3'),
    (2015, 'single', 2,     9225.00, 0.15000, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.01 Table 3'),
    (2015, 'single', 3,    37450.00, 0.25000, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.01 Table 3'),
    (2015, 'single', 4,    90750.00, 0.28000, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.01 Table 3'),
    (2015, 'single', 5,   189300.00, 0.33000, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.01 Table 3'),
    (2015, 'single', 6,   411500.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.01 Table 3'),
    (2015, 'single', 7,   413200.00, 0.39600, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.01 Table 3'),
    (2015, 'mfj', 1,        0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.01 Table 1'),
    (2015, 'mfj', 2,    18450.00, 0.15000, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.01 Table 1'),
    (2015, 'mfj', 3,    74900.00, 0.25000, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.01 Table 1'),
    (2015, 'mfj', 4,   151200.00, 0.28000, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.01 Table 1'),
    (2015, 'mfj', 5,   230450.00, 0.33000, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.01 Table 1'),
    (2015, 'mfj', 6,   411500.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.01 Table 1'),
    (2015, 'mfj', 7,   464850.00, 0.39600, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.01 Table 1'),
    (2015, 'qss', 1,        0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'QSS=MFJ'),
    (2015, 'qss', 2,    18450.00, 0.15000, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'QSS=MFJ'),
    (2015, 'qss', 3,    74900.00, 0.25000, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'QSS=MFJ'),
    (2015, 'qss', 4,   151200.00, 0.28000, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'QSS=MFJ'),
    (2015, 'qss', 5,   230450.00, 0.33000, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'QSS=MFJ'),
    (2015, 'qss', 6,   411500.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'QSS=MFJ'),
    (2015, 'qss', 7,   464850.00, 0.39600, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'QSS=MFJ'),
    (2015, 'mfs', 1,        0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.01 Table 4'),
    (2015, 'mfs', 2,     9225.00, 0.15000, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.01 Table 4'),
    (2015, 'mfs', 3,    37450.00, 0.25000, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.01 Table 4'),
    (2015, 'mfs', 4,    75600.00, 0.28000, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.01 Table 4'),
    (2015, 'mfs', 5,   115225.00, 0.33000, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.01 Table 4'),
    (2015, 'mfs', 6,   205750.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.01 Table 4'),
    (2015, 'mfs', 7,   232425.00, 0.39600, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.01 Table 4'),
    (2015, 'hoh', 1,        0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.01 Table 2'),
    (2015, 'hoh', 2,    13150.00, 0.15000, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.01 Table 2'),
    (2015, 'hoh', 3,    50200.00, 0.25000, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.01 Table 2'),
    (2015, 'hoh', 4,   129600.00, 0.28000, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.01 Table 2'),
    (2015, 'hoh', 5,   209850.00, 0.33000, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.01 Table 2'),
    (2015, 'hoh', 6,   411500.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.01 Table 2'),
    (2015, 'hoh', 7,   439000.00, 0.39600, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.01 Table 2')
ON CONFLICT (tax_year, filing_status, bracket_ord) DO UPDATE SET bracket_floor = EXCLUDED.bracket_floor, marginal_rate = EXCLUDED.marginal_rate, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

INSERT INTO ref.irs_standard_deduction (tax_year, filing_status, base_amount, additional_age_65, additional_blind, source_url, source_citation) VALUES
    (2015, 'single',  6300.00, 1550.00, 1550.00, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.14'),
    (2015, 'mfj',    12600.00, 1250.00, 1250.00, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.14'),
    (2015, 'mfs',     6300.00, 1250.00, 1250.00, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.14'),
    (2015, 'hoh',     9250.00, 1550.00, 1550.00, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.14'),
    (2015, 'qss',    12600.00, 1250.00, 1250.00, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.14')
ON CONFLICT (tax_year, filing_status) DO UPDATE SET base_amount = EXCLUDED.base_amount, additional_age_65 = EXCLUDED.additional_age_65, additional_blind = EXCLUDED.additional_blind, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

INSERT INTO ref.irs_personal_exemption (tax_year, amount, source_url, source_citation)
VALUES (2015, 4000.00, 'https://www.irs.gov/pub/irs-drop/rp-14-61.pdf', 'Rev. Proc. 2014-61 s.3.24')
ON CONFLICT (tax_year) DO UPDATE SET amount = EXCLUDED.amount, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

INSERT INTO ref.irs_child_tax_credit (tax_year, amount_under_6, amount_6_to_17, refundable_max_per_child, phaseout_threshold_single, phaseout_threshold_mfj, phaseout_rate, source_url, source_citation)
VALUES (2015, 1000.00, 1000.00, 1000.00, 75000.00, 110000.00, 0.05000, 'https://www.law.cornell.edu/uscode/text/26/24', 'IRC s.24 pre-TCJA: $1,000 base; phaseout $75K Single/$110K MFJ at 5%')
ON CONFLICT (tax_year) DO UPDATE SET amount_under_6 = EXCLUDED.amount_under_6, amount_6_to_17 = EXCLUDED.amount_6_to_17, refundable_max_per_child = EXCLUDED.refundable_max_per_child, phaseout_threshold_single = EXCLUDED.phaseout_threshold_single, phaseout_threshold_mfj = EXCLUDED.phaseout_threshold_mfj, phaseout_rate = EXCLUDED.phaseout_rate, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

INSERT INTO ref.fica_parameters (tax_year, ss_employee_rate, ss_wage_base, medicare_employee_rate, additional_medicare_rate, additional_medicare_threshold_single, additional_medicare_threshold_mfj, source_url, source_citation)
VALUES (2015, 0.06200, 118500.00, 0.01450, 0.00900, 200000.00, 250000.00, 'https://www.ssa.gov/oact/cola/cbb.html', 'SSA Contribution and Benefit Base TY2015 $118,500')
ON CONFLICT (tax_year) DO UPDATE SET ss_employee_rate = EXCLUDED.ss_employee_rate, ss_wage_base = EXCLUDED.ss_wage_base, medicare_employee_rate = EXCLUDED.medicare_employee_rate, additional_medicare_rate = EXCLUDED.additional_medicare_rate, additional_medicare_threshold_single = EXCLUDED.additional_medicare_threshold_single, additional_medicare_threshold_mfj = EXCLUDED.additional_medicare_threshold_mfj, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;
