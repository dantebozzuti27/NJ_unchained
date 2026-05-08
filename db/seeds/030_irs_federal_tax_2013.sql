    -- ============================================================================
    -- Seed: 030_irs_federal_tax_2013
    -- Source: Rev. Proc. 2013-15 (https://www.irs.gov/pub/irs-drop/rp-13-15.pdf)
    -- 7-bracket ATRA-era ladder (10/15/25/28/33/35/39.6) per P.L. 112-240.
    -- SS rate 0.06200; wage base $113,700.
    -- ============================================================================
    INSERT INTO ref.irs_federal_brackets (tax_year, filing_status, bracket_ord, bracket_floor, marginal_rate, source_url, source_citation) VALUES
        (2013, 'single', 1,          0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (single)'),
(2013, 'single', 2,       8925.00, 0.15000, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (single)'),
(2013, 'single', 3,      36250.00, 0.25000, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (single)'),
(2013, 'single', 4,      87850.00, 0.28000, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (single)'),
(2013, 'single', 5,     183250.00, 0.33000, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (single)'),
(2013, 'single', 6,     398350.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (single)'),
(2013, 'single', 7,     400000.00, 0.39600, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (single)'),
(2013, 'mfj', 1,          0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (mfj)'),
(2013, 'mfj', 2,      17850.00, 0.15000, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (mfj)'),
(2013, 'mfj', 3,      72500.00, 0.25000, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (mfj)'),
(2013, 'mfj', 4,     146400.00, 0.28000, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (mfj)'),
(2013, 'mfj', 5,     223050.00, 0.33000, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (mfj)'),
(2013, 'mfj', 6,     398350.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (mfj)'),
(2013, 'mfj', 7,     450000.00, 0.39600, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (mfj)'),
(2013, 'qss', 1,          0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (qss)'),
(2013, 'qss', 2,      17850.00, 0.15000, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (qss)'),
(2013, 'qss', 3,      72500.00, 0.25000, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (qss)'),
(2013, 'qss', 4,     146400.00, 0.28000, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (qss)'),
(2013, 'qss', 5,     223050.00, 0.33000, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (qss)'),
(2013, 'qss', 6,     398350.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (qss)'),
(2013, 'qss', 7,     450000.00, 0.39600, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (qss)'),
(2013, 'mfs', 1,          0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (mfs)'),
(2013, 'mfs', 2,       8925.00, 0.15000, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (mfs)'),
(2013, 'mfs', 3,      36250.00, 0.25000, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (mfs)'),
(2013, 'mfs', 4,      73200.00, 0.28000, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (mfs)'),
(2013, 'mfs', 5,     111525.00, 0.33000, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (mfs)'),
(2013, 'mfs', 6,     199175.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (mfs)'),
(2013, 'mfs', 7,     225000.00, 0.39600, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (mfs)'),
(2013, 'hoh', 1,          0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (hoh)'),
(2013, 'hoh', 2,      12750.00, 0.15000, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (hoh)'),
(2013, 'hoh', 3,      48600.00, 0.25000, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (hoh)'),
(2013, 'hoh', 4,     125450.00, 0.28000, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (hoh)'),
(2013, 'hoh', 5,     203150.00, 0.33000, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (hoh)'),
(2013, 'hoh', 6,     398350.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (hoh)'),
(2013, 'hoh', 7,     425000.00, 0.39600, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 s.3.01 (hoh)')
    ON CONFLICT (tax_year, filing_status, bracket_ord) DO UPDATE SET bracket_floor = EXCLUDED.bracket_floor, marginal_rate = EXCLUDED.marginal_rate, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

    INSERT INTO ref.irs_standard_deduction (tax_year, filing_status, base_amount, additional_age_65, additional_blind, source_url, source_citation) VALUES
        (2013, 'single',   6100.00, 1500.00, 1500.00, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 std deduction'),
(2013, 'mfj',    12200.00, 1200.00, 1200.00, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 std deduction'),
(2013, 'mfs',     6100.00, 1200.00, 1200.00, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 std deduction'),
(2013, 'hoh',     8950.00, 1500.00, 1500.00, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 std deduction'),
(2013, 'qss',    12200.00, 1200.00, 1200.00, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 std deduction')
    ON CONFLICT (tax_year, filing_status) DO UPDATE SET base_amount = EXCLUDED.base_amount, additional_age_65 = EXCLUDED.additional_age_65, additional_blind = EXCLUDED.additional_blind, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

    INSERT INTO ref.irs_personal_exemption (tax_year, amount, source_url, source_citation)
    VALUES (2013, 3900.00, 'https://www.irs.gov/pub/irs-drop/rp-13-15.pdf', 'Rev. Proc. 2013-15 personal exemption')
    ON CONFLICT (tax_year) DO UPDATE SET amount = EXCLUDED.amount, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

    INSERT INTO ref.irs_child_tax_credit (tax_year, amount_under_6, amount_6_to_17, refundable_max_per_child, phaseout_threshold_single, phaseout_threshold_mfj, phaseout_rate, source_url, source_citation)
    VALUES (2013, 1000.00, 1000.00, 1000.00, 75000.00, 110000.00, 0.05000, 'https://www.law.cornell.edu/uscode/text/26/24', 'IRC s.24 pre-TCJA: $1,000 base; phaseout $75K Single/$110K MFJ at 5%')
    ON CONFLICT (tax_year) DO UPDATE SET amount_under_6 = EXCLUDED.amount_under_6, amount_6_to_17 = EXCLUDED.amount_6_to_17, refundable_max_per_child = EXCLUDED.refundable_max_per_child, phaseout_threshold_single = EXCLUDED.phaseout_threshold_single, phaseout_threshold_mfj = EXCLUDED.phaseout_threshold_mfj, phaseout_rate = EXCLUDED.phaseout_rate, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

    INSERT INTO ref.fica_parameters (tax_year, ss_employee_rate, ss_wage_base, medicare_employee_rate, additional_medicare_rate, additional_medicare_threshold_single, additional_medicare_threshold_mfj, source_url, source_citation)
    VALUES (2013, 0.06200, 113700.00, 0.01450, 0.00900, 200000.00, 250000.00, 'https://www.ssa.gov/oact/cola/cbb.html', 'SSA Contribution and Benefit Base TY2013 $113,700; Add''l Medicare per IRC s.3101(b)(2) (ACA s.9015 effective TY 2013+)')
    ON CONFLICT (tax_year) DO UPDATE SET ss_employee_rate = EXCLUDED.ss_employee_rate, ss_wage_base = EXCLUDED.ss_wage_base, medicare_employee_rate = EXCLUDED.medicare_employee_rate, additional_medicare_rate = EXCLUDED.additional_medicare_rate, additional_medicare_threshold_single = EXCLUDED.additional_medicare_threshold_single, additional_medicare_threshold_mfj = EXCLUDED.additional_medicare_threshold_mfj, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;
