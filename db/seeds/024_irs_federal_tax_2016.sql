-- ============================================================================
-- Seed: 024_irs_federal_tax_2016
-- Source: Rev. Proc. 2015-53 (https://www.irs.gov/pub/irs-drop/rp-15-53.pdf)
-- ATRA-era 7-bracket ladder (10/15/25/28/33/35/39.6); pre-TCJA personal
-- exemption $4,050; pre-TCJA CTC $1,000.
-- FICA SS wage base $118,500 (UNCHANGED from TY 2015 -- SSA Fact Sheet
-- 2015-10-15: zero COLA in 2015 -> 2016 stays at TY 2015 base).
-- ============================================================================
INSERT INTO ref.irs_federal_brackets (tax_year, filing_status, bracket_ord, bracket_floor, marginal_rate, source_url, source_citation) VALUES
    (2016, 'single', 1,        0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 3'),
    (2016, 'single', 2,     9275.00, 0.15000, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 3'),
    (2016, 'single', 3,    37650.00, 0.25000, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 3'),
    (2016, 'single', 4,    91150.00, 0.28000, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 3'),
    (2016, 'single', 5,   190150.00, 0.33000, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 3'),
    (2016, 'single', 6,   413350.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 3'),
    (2016, 'single', 7,   415050.00, 0.39600, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 3'),
    (2016, 'mfj', 1,        0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 1'),
    (2016, 'mfj', 2,    18550.00, 0.15000, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 1'),
    (2016, 'mfj', 3,    75300.00, 0.25000, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 1'),
    (2016, 'mfj', 4,   151900.00, 0.28000, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 1'),
    (2016, 'mfj', 5,   231450.00, 0.33000, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 1'),
    (2016, 'mfj', 6,   413350.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 1'),
    (2016, 'mfj', 7,   466950.00, 0.39600, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 1'),
    (2016, 'qss', 1,        0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 1 (QSS=MFJ)'),
    (2016, 'qss', 2,    18550.00, 0.15000, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 1'),
    (2016, 'qss', 3,    75300.00, 0.25000, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 1'),
    (2016, 'qss', 4,   151900.00, 0.28000, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 1'),
    (2016, 'qss', 5,   231450.00, 0.33000, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 1'),
    (2016, 'qss', 6,   413350.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 1'),
    (2016, 'qss', 7,   466950.00, 0.39600, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 1'),
    (2016, 'mfs', 1,        0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 4'),
    (2016, 'mfs', 2,     9275.00, 0.15000, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 4'),
    (2016, 'mfs', 3,    37650.00, 0.25000, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 4'),
    (2016, 'mfs', 4,    75950.00, 0.28000, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 4'),
    (2016, 'mfs', 5,   115725.00, 0.33000, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 4'),
    (2016, 'mfs', 6,   206675.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 4'),
    (2016, 'mfs', 7,   233475.00, 0.39600, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 4'),
    (2016, 'hoh', 1,        0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 2'),
    (2016, 'hoh', 2,    13250.00, 0.15000, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 2'),
    (2016, 'hoh', 3,    50400.00, 0.25000, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 2'),
    (2016, 'hoh', 4,   130150.00, 0.28000, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 2'),
    (2016, 'hoh', 5,   210800.00, 0.33000, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 2'),
    (2016, 'hoh', 6,   413350.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 2'),
    (2016, 'hoh', 7,   441000.00, 0.39600, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.01 Table 2')
ON CONFLICT (tax_year, filing_status, bracket_ord) DO UPDATE SET bracket_floor = EXCLUDED.bracket_floor, marginal_rate = EXCLUDED.marginal_rate, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

INSERT INTO ref.irs_standard_deduction (tax_year, filing_status, base_amount, additional_age_65, additional_blind, source_url, source_citation) VALUES
    (2016, 'single',  6300.00, 1550.00, 1550.00, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.14'),
    (2016, 'mfj',    12600.00, 1250.00, 1250.00, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.14'),
    (2016, 'mfs',     6300.00, 1250.00, 1250.00, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.14'),
    (2016, 'hoh',     9300.00, 1550.00, 1550.00, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.14'),
    (2016, 'qss',    12600.00, 1250.00, 1250.00, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.14')
ON CONFLICT (tax_year, filing_status) DO UPDATE SET base_amount = EXCLUDED.base_amount, additional_age_65 = EXCLUDED.additional_age_65, additional_blind = EXCLUDED.additional_blind, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

INSERT INTO ref.irs_personal_exemption (tax_year, amount, source_url, source_citation)
VALUES (2016, 4050.00, 'https://www.irs.gov/pub/irs-drop/rp-15-53.pdf', 'Rev. Proc. 2015-53 s.3.24')
ON CONFLICT (tax_year) DO UPDATE SET amount = EXCLUDED.amount, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

INSERT INTO ref.irs_child_tax_credit (tax_year, amount_under_6, amount_6_to_17, refundable_max_per_child, phaseout_threshold_single, phaseout_threshold_mfj, phaseout_rate, source_url, source_citation)
VALUES (2016, 1000.00, 1000.00, 1000.00, 75000.00, 110000.00, 0.05000, 'https://www.law.cornell.edu/uscode/text/26/24', 'IRC s.24 pre-TCJA: $1,000 base; phaseout $75K Single / $110K MFJ at 5%')
ON CONFLICT (tax_year) DO UPDATE SET amount_under_6 = EXCLUDED.amount_under_6, amount_6_to_17 = EXCLUDED.amount_6_to_17, refundable_max_per_child = EXCLUDED.refundable_max_per_child, phaseout_threshold_single = EXCLUDED.phaseout_threshold_single, phaseout_threshold_mfj = EXCLUDED.phaseout_threshold_mfj, phaseout_rate = EXCLUDED.phaseout_rate, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

INSERT INTO ref.fica_parameters (tax_year, ss_employee_rate, ss_wage_base, medicare_employee_rate, additional_medicare_rate, additional_medicare_threshold_single, additional_medicare_threshold_mfj, source_url, source_citation)
VALUES (2016, 0.06200, 118500.00, 0.01450, 0.00900, 200000.00, 250000.00, 'https://www.ssa.gov/oact/cola/cbb.html', 'SSA Contribution and Benefit Base TY2016 $118,500 (UNCHANGED from TY2015 due to zero COLA); Add''l Medicare per IRC s.3101(b)(2)')
ON CONFLICT (tax_year) DO UPDATE SET ss_employee_rate = EXCLUDED.ss_employee_rate, ss_wage_base = EXCLUDED.ss_wage_base, medicare_employee_rate = EXCLUDED.medicare_employee_rate, additional_medicare_rate = EXCLUDED.additional_medicare_rate, additional_medicare_threshold_single = EXCLUDED.additional_medicare_threshold_single, additional_medicare_threshold_mfj = EXCLUDED.additional_medicare_threshold_mfj, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;
