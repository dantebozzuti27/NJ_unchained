    -- ============================================================================
    -- Seed: 032_irs_federal_tax_2012
    -- Source: Rev. Proc. 2011-52 (https://www.irs.gov/pub/irs-drop/rp-11-52.pdf)
    -- 6-bracket pre-ATRA ladder (10/15/25/28/33/35); ATRA 39.6% top added TY 2013.
    -- SS rate 0.04200 (PAYROLL TAX HOLIDAY: 4.2% per P.L. 111-312 (TY 2011) extended via P.L. 112-78 + P.L. 112-96 (TY 2012)); wage base $110,100.
    -- ============================================================================
    INSERT INTO ref.irs_federal_brackets (tax_year, filing_status, bracket_ord, bracket_floor, marginal_rate, source_url, source_citation) VALUES
        (2012, 'single', 1,          0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 s.3.01 (single)'),
(2012, 'single', 2,       8700.00, 0.15000, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 s.3.01 (single)'),
(2012, 'single', 3,      35350.00, 0.25000, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 s.3.01 (single)'),
(2012, 'single', 4,      85650.00, 0.28000, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 s.3.01 (single)'),
(2012, 'single', 5,     178650.00, 0.33000, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 s.3.01 (single)'),
(2012, 'single', 6,     388350.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 s.3.01 (single)'),
(2012, 'mfj', 1,          0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 s.3.01 (mfj)'),
(2012, 'mfj', 2,      17400.00, 0.15000, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 s.3.01 (mfj)'),
(2012, 'mfj', 3,      70700.00, 0.25000, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 s.3.01 (mfj)'),
(2012, 'mfj', 4,     142700.00, 0.28000, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 s.3.01 (mfj)'),
(2012, 'mfj', 5,     217450.00, 0.33000, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 s.3.01 (mfj)'),
(2012, 'mfj', 6,     388350.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 s.3.01 (mfj)'),
(2012, 'qss', 1,          0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 s.3.01 (qss)'),
(2012, 'qss', 2,      17400.00, 0.15000, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 s.3.01 (qss)'),
(2012, 'qss', 3,      70700.00, 0.25000, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 s.3.01 (qss)'),
(2012, 'qss', 4,     142700.00, 0.28000, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 s.3.01 (qss)'),
(2012, 'qss', 5,     217450.00, 0.33000, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 s.3.01 (qss)'),
(2012, 'qss', 6,     388350.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 s.3.01 (qss)'),
(2012, 'mfs', 1,          0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 s.3.01 (mfs)'),
(2012, 'mfs', 2,       8700.00, 0.15000, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 s.3.01 (mfs)'),
(2012, 'mfs', 3,      35350.00, 0.25000, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 s.3.01 (mfs)'),
(2012, 'mfs', 4,      71350.00, 0.28000, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 s.3.01 (mfs)'),
(2012, 'mfs', 5,     108725.00, 0.33000, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 s.3.01 (mfs)'),
(2012, 'mfs', 6,     194175.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 s.3.01 (mfs)'),
(2012, 'hoh', 1,          0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 s.3.01 (hoh)'),
(2012, 'hoh', 2,      12400.00, 0.15000, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 s.3.01 (hoh)'),
(2012, 'hoh', 3,      47350.00, 0.25000, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 s.3.01 (hoh)'),
(2012, 'hoh', 4,     122300.00, 0.28000, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 s.3.01 (hoh)'),
(2012, 'hoh', 5,     198050.00, 0.33000, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 s.3.01 (hoh)'),
(2012, 'hoh', 6,     388350.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 s.3.01 (hoh)')
    ON CONFLICT (tax_year, filing_status, bracket_ord) DO UPDATE SET bracket_floor = EXCLUDED.bracket_floor, marginal_rate = EXCLUDED.marginal_rate, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

    INSERT INTO ref.irs_standard_deduction (tax_year, filing_status, base_amount, additional_age_65, additional_blind, source_url, source_citation) VALUES
        (2012, 'single',   5950.00, 1450.00, 1450.00, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 std deduction'),
(2012, 'mfj',    11900.00, 1150.00, 1150.00, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 std deduction'),
(2012, 'mfs',     5950.00, 1150.00, 1150.00, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 std deduction'),
(2012, 'hoh',     8700.00, 1450.00, 1450.00, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 std deduction'),
(2012, 'qss',    11900.00, 1150.00, 1150.00, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 std deduction')
    ON CONFLICT (tax_year, filing_status) DO UPDATE SET base_amount = EXCLUDED.base_amount, additional_age_65 = EXCLUDED.additional_age_65, additional_blind = EXCLUDED.additional_blind, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

    INSERT INTO ref.irs_personal_exemption (tax_year, amount, source_url, source_citation)
    VALUES (2012, 3800.00, 'https://www.irs.gov/pub/irs-drop/rp-11-52.pdf', 'Rev. Proc. 2011-52 personal exemption')
    ON CONFLICT (tax_year) DO UPDATE SET amount = EXCLUDED.amount, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

    INSERT INTO ref.irs_child_tax_credit (tax_year, amount_under_6, amount_6_to_17, refundable_max_per_child, phaseout_threshold_single, phaseout_threshold_mfj, phaseout_rate, source_url, source_citation)
    VALUES (2012, 1000.00, 1000.00, 1000.00, 75000.00, 110000.00, 0.05000, 'https://www.law.cornell.edu/uscode/text/26/24', 'IRC s.24 pre-TCJA: $1,000 base; phaseout $75K Single/$110K MFJ at 5%')
    ON CONFLICT (tax_year) DO UPDATE SET amount_under_6 = EXCLUDED.amount_under_6, amount_6_to_17 = EXCLUDED.amount_6_to_17, refundable_max_per_child = EXCLUDED.refundable_max_per_child, phaseout_threshold_single = EXCLUDED.phaseout_threshold_single, phaseout_threshold_mfj = EXCLUDED.phaseout_threshold_mfj, phaseout_rate = EXCLUDED.phaseout_rate, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

    INSERT INTO ref.fica_parameters (tax_year, ss_employee_rate, ss_wage_base, medicare_employee_rate, additional_medicare_rate, additional_medicare_threshold_single, additional_medicare_threshold_mfj, source_url, source_citation)
    VALUES (2012, 0.04200, 110100.00, 0.01450, 0.00000, NULL, NULL, 'https://www.ssa.gov/oact/cola/cbb.html', 'SSA Contribution and Benefit Base TY2012 $110,100; ACA s.9015 Add''l Medicare 0.9% NOT yet effective (TY < 2013)')
    ON CONFLICT (tax_year) DO UPDATE SET ss_employee_rate = EXCLUDED.ss_employee_rate, ss_wage_base = EXCLUDED.ss_wage_base, medicare_employee_rate = EXCLUDED.medicare_employee_rate, additional_medicare_rate = EXCLUDED.additional_medicare_rate, additional_medicare_threshold_single = EXCLUDED.additional_medicare_threshold_single, additional_medicare_threshold_mfj = EXCLUDED.additional_medicare_threshold_mfj, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;
