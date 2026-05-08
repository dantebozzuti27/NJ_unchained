    -- ============================================================================
    -- Seed: 034_irs_federal_tax_2011
    -- Source: Rev. Proc. 2011-12 (https://www.irs.gov/pub/irs-drop/rp-11-12.pdf)
    -- 6-bracket pre-ATRA ladder (10/15/25/28/33/35); ATRA 39.6% top added TY 2013.
    -- SS rate 0.04200 (PAYROLL TAX HOLIDAY: 4.2% per P.L. 111-312 (TY 2011) extended via P.L. 112-78 + P.L. 112-96 (TY 2012)); wage base $106,800.
    -- ============================================================================
    INSERT INTO ref.irs_federal_brackets (tax_year, filing_status, bracket_ord, bracket_floor, marginal_rate, source_url, source_citation) VALUES
        (2011, 'single', 1,          0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 s.3.01 (single)'),
(2011, 'single', 2,       8500.00, 0.15000, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 s.3.01 (single)'),
(2011, 'single', 3,      34500.00, 0.25000, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 s.3.01 (single)'),
(2011, 'single', 4,      83600.00, 0.28000, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 s.3.01 (single)'),
(2011, 'single', 5,     174400.00, 0.33000, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 s.3.01 (single)'),
(2011, 'single', 6,     379150.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 s.3.01 (single)'),
(2011, 'mfj', 1,          0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 s.3.01 (mfj)'),
(2011, 'mfj', 2,      17000.00, 0.15000, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 s.3.01 (mfj)'),
(2011, 'mfj', 3,      69000.00, 0.25000, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 s.3.01 (mfj)'),
(2011, 'mfj', 4,     139350.00, 0.28000, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 s.3.01 (mfj)'),
(2011, 'mfj', 5,     212300.00, 0.33000, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 s.3.01 (mfj)'),
(2011, 'mfj', 6,     379150.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 s.3.01 (mfj)'),
(2011, 'qss', 1,          0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 s.3.01 (qss)'),
(2011, 'qss', 2,      17000.00, 0.15000, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 s.3.01 (qss)'),
(2011, 'qss', 3,      69000.00, 0.25000, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 s.3.01 (qss)'),
(2011, 'qss', 4,     139350.00, 0.28000, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 s.3.01 (qss)'),
(2011, 'qss', 5,     212300.00, 0.33000, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 s.3.01 (qss)'),
(2011, 'qss', 6,     379150.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 s.3.01 (qss)'),
(2011, 'mfs', 1,          0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 s.3.01 (mfs)'),
(2011, 'mfs', 2,       8500.00, 0.15000, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 s.3.01 (mfs)'),
(2011, 'mfs', 3,      34500.00, 0.25000, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 s.3.01 (mfs)'),
(2011, 'mfs', 4,      69675.00, 0.28000, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 s.3.01 (mfs)'),
(2011, 'mfs', 5,     106150.00, 0.33000, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 s.3.01 (mfs)'),
(2011, 'mfs', 6,     189575.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 s.3.01 (mfs)'),
(2011, 'hoh', 1,          0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 s.3.01 (hoh)'),
(2011, 'hoh', 2,      12150.00, 0.15000, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 s.3.01 (hoh)'),
(2011, 'hoh', 3,      46250.00, 0.25000, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 s.3.01 (hoh)'),
(2011, 'hoh', 4,     119400.00, 0.28000, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 s.3.01 (hoh)'),
(2011, 'hoh', 5,     193350.00, 0.33000, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 s.3.01 (hoh)'),
(2011, 'hoh', 6,     379150.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 s.3.01 (hoh)')
    ON CONFLICT (tax_year, filing_status, bracket_ord) DO UPDATE SET bracket_floor = EXCLUDED.bracket_floor, marginal_rate = EXCLUDED.marginal_rate, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

    INSERT INTO ref.irs_standard_deduction (tax_year, filing_status, base_amount, additional_age_65, additional_blind, source_url, source_citation) VALUES
        (2011, 'single',   5800.00, 1450.00, 1450.00, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 std deduction'),
(2011, 'mfj',    11600.00, 1150.00, 1150.00, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 std deduction'),
(2011, 'mfs',     5800.00, 1150.00, 1150.00, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 std deduction'),
(2011, 'hoh',     8500.00, 1450.00, 1450.00, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 std deduction'),
(2011, 'qss',    11600.00, 1150.00, 1150.00, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 std deduction')
    ON CONFLICT (tax_year, filing_status) DO UPDATE SET base_amount = EXCLUDED.base_amount, additional_age_65 = EXCLUDED.additional_age_65, additional_blind = EXCLUDED.additional_blind, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

    INSERT INTO ref.irs_personal_exemption (tax_year, amount, source_url, source_citation)
    VALUES (2011, 3700.00, 'https://www.irs.gov/pub/irs-drop/rp-11-12.pdf', 'Rev. Proc. 2011-12 personal exemption')
    ON CONFLICT (tax_year) DO UPDATE SET amount = EXCLUDED.amount, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

    INSERT INTO ref.irs_child_tax_credit (tax_year, amount_under_6, amount_6_to_17, refundable_max_per_child, phaseout_threshold_single, phaseout_threshold_mfj, phaseout_rate, source_url, source_citation)
    VALUES (2011, 1000.00, 1000.00, 1000.00, 75000.00, 110000.00, 0.05000, 'https://www.law.cornell.edu/uscode/text/26/24', 'IRC s.24 pre-TCJA: $1,000 base; phaseout $75K Single/$110K MFJ at 5%')
    ON CONFLICT (tax_year) DO UPDATE SET amount_under_6 = EXCLUDED.amount_under_6, amount_6_to_17 = EXCLUDED.amount_6_to_17, refundable_max_per_child = EXCLUDED.refundable_max_per_child, phaseout_threshold_single = EXCLUDED.phaseout_threshold_single, phaseout_threshold_mfj = EXCLUDED.phaseout_threshold_mfj, phaseout_rate = EXCLUDED.phaseout_rate, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

    INSERT INTO ref.fica_parameters (tax_year, ss_employee_rate, ss_wage_base, medicare_employee_rate, additional_medicare_rate, additional_medicare_threshold_single, additional_medicare_threshold_mfj, source_url, source_citation)
    VALUES (2011, 0.04200, 106800.00, 0.01450, 0.00000, NULL, NULL, 'https://www.ssa.gov/oact/cola/cbb.html', 'SSA Contribution and Benefit Base TY2011 $106,800; ACA s.9015 Add''l Medicare 0.9% NOT yet effective (TY < 2013)')
    ON CONFLICT (tax_year) DO UPDATE SET ss_employee_rate = EXCLUDED.ss_employee_rate, ss_wage_base = EXCLUDED.ss_wage_base, medicare_employee_rate = EXCLUDED.medicare_employee_rate, additional_medicare_rate = EXCLUDED.additional_medicare_rate, additional_medicare_threshold_single = EXCLUDED.additional_medicare_threshold_single, additional_medicare_threshold_mfj = EXCLUDED.additional_medicare_threshold_mfj, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;
