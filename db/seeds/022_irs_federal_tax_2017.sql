-- ============================================================================
-- Seed: 022_irs_federal_tax_2017
--
-- Federal income-tax reference data for tax year 2017 -- THE LAST PRE-TCJA
-- YEAR. All values transcribed from Rev. Proc. 2016-55 (IRS document
-- authorizing 2017 inflation adjustments under IRC s.1(f)).
--
-- AUTHORITATIVE SOURCES:
--   Rev. Proc. 2016-55: https://www.irs.gov/pub/irs-drop/rp-16-55.pdf
--   FICA SS wage base $127,200: SSA Fact Sheet 2016-10-18
--     https://www.ssa.gov/oact/cola/cbb.html
--
-- TY 2017 STRUCTURAL FACTS (the "old world" vs TY 2018 TCJA):
--   - 7 brackets at rates 10/15/25/28/33/35/39.6 (the ATRA-era ladder
--     created by P.L. 112-240 effective TY 2013)
--   - Personal exemption $4,050 (vs $0 TY 2018+)
--   - Std deduction Single $6,350 / MFJ $12,700 (vs $12,000/$24,000 TY 2018)
--   - CTC $1,000 base / $1,000 refundable (vs $2,000/$1,400 TY 2018)
--   - CTC phaseout $75K Single / $110K MFJ (vs $200K/$400K TY 2018) -
--     a far steeper claw-back at much lower income levels
--
-- These five differences create the largest single cross-year tax
-- divergence in the seeded substrate: the TCJA tax cut at TY 2017->2018.
-- IDEMPOTENCY: re-run safe via ON CONFLICT DO UPDATE.
-- ============================================================================

INSERT INTO ref.irs_federal_brackets
    (tax_year, filing_status, bracket_ord, bracket_floor, marginal_rate,
     source_url, source_citation)
VALUES
    (2017, 'single', 1,        0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 3'),
    (2017, 'single', 2,     9325.00, 0.15000, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 3'),
    (2017, 'single', 3,    37950.00, 0.25000, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 3'),
    (2017, 'single', 4,    91900.00, 0.28000, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 3'),
    (2017, 'single', 5,   191650.00, 0.33000, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 3'),
    (2017, 'single', 6,   416700.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 3'),
    (2017, 'single', 7,   418400.00, 0.39600, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 3 (ATRA 39.6% top, P.L. 112-240)'),

    (2017, 'mfj', 1,        0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 1 (MFJ+Surviving Spouses)'),
    (2017, 'mfj', 2,    18650.00, 0.15000, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 1'),
    (2017, 'mfj', 3,    75900.00, 0.25000, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 1'),
    (2017, 'mfj', 4,   153100.00, 0.28000, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 1'),
    (2017, 'mfj', 5,   233350.00, 0.33000, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 1'),
    (2017, 'mfj', 6,   416700.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 1'),
    (2017, 'mfj', 7,   470700.00, 0.39600, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 1 (ATRA 39.6%)'),

    (2017, 'qss', 1,        0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 1 (QSS = MFJ schedule)'),
    (2017, 'qss', 2,    18650.00, 0.15000, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 1'),
    (2017, 'qss', 3,    75900.00, 0.25000, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 1'),
    (2017, 'qss', 4,   153100.00, 0.28000, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 1'),
    (2017, 'qss', 5,   233350.00, 0.33000, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 1'),
    (2017, 'qss', 6,   416700.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 1'),
    (2017, 'qss', 7,   470700.00, 0.39600, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 1'),

    (2017, 'mfs', 1,        0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 4'),
    (2017, 'mfs', 2,     9325.00, 0.15000, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 4'),
    (2017, 'mfs', 3,    37950.00, 0.25000, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 4'),
    (2017, 'mfs', 4,    76550.00, 0.28000, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 4'),
    (2017, 'mfs', 5,   116675.00, 0.33000, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 4'),
    (2017, 'mfs', 6,   208350.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 4'),
    (2017, 'mfs', 7,   235350.00, 0.39600, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 4 (= MFJ/2 = 470,700/2)'),

    (2017, 'hoh', 1,        0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 2 (HOH)'),
    (2017, 'hoh', 2,    13350.00, 0.15000, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 2'),
    (2017, 'hoh', 3,    50800.00, 0.25000, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 2'),
    (2017, 'hoh', 4,   131200.00, 0.28000, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 2'),
    (2017, 'hoh', 5,   212500.00, 0.33000, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 2'),
    (2017, 'hoh', 6,   416700.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 2'),
    (2017, 'hoh', 7,   444550.00, 0.39600, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.01 Table 2')
ON CONFLICT (tax_year, filing_status, bracket_ord) DO UPDATE SET
    bracket_floor = EXCLUDED.bracket_floor, marginal_rate = EXCLUDED.marginal_rate,
    source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

INSERT INTO ref.irs_standard_deduction
    (tax_year, filing_status, base_amount, additional_age_65, additional_blind,
     source_url, source_citation)
VALUES
    (2017, 'single',  6350.00, 1550.00, 1550.00, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.14 (base+age/blind unmarried)'),
    (2017, 'mfj',    12700.00, 1250.00, 1250.00, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.14 (MFJ+age/blind)'),
    (2017, 'mfs',     6350.00, 1250.00, 1250.00, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.14 (MFS+age/blind)'),
    (2017, 'hoh',     9350.00, 1550.00, 1550.00, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.14 (HOH+age/blind unmarried)'),
    (2017, 'qss',    12700.00, 1250.00, 1250.00, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.14 (QSS = MFJ)')
ON CONFLICT (tax_year, filing_status) DO UPDATE SET
    base_amount = EXCLUDED.base_amount, additional_age_65 = EXCLUDED.additional_age_65,
    additional_blind = EXCLUDED.additional_blind,
    source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

INSERT INTO ref.irs_personal_exemption (tax_year, amount, source_url, source_citation)
VALUES (2017, 4050.00, 'https://www.irs.gov/pub/irs-drop/rp-16-55.pdf', 'Rev. Proc. 2016-55 s.3.24; LAST YEAR with non-zero exemption ($0 in TY 2018+ per TCJA)')
ON CONFLICT (tax_year) DO UPDATE SET amount = EXCLUDED.amount,
    source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

INSERT INTO ref.irs_child_tax_credit
    (tax_year, amount_under_6, amount_6_to_17, refundable_max_per_child,
     phaseout_threshold_single, phaseout_threshold_mfj, phaseout_rate,
     source_url, source_citation)
VALUES
    (2017, 1000.00, 1000.00, 1000.00, 75000.00, 110000.00, 0.05000,
     'https://www.law.cornell.edu/uscode/text/26/24',
     'IRC s.24 pre-TCJA: $1,000 base, fully refundable subject to ACTC; phaseout $75K Single/HOH/MFS, $110K MFJ at 5%')
ON CONFLICT (tax_year) DO UPDATE SET
    amount_under_6 = EXCLUDED.amount_under_6, amount_6_to_17 = EXCLUDED.amount_6_to_17,
    refundable_max_per_child = EXCLUDED.refundable_max_per_child,
    phaseout_threshold_single = EXCLUDED.phaseout_threshold_single,
    phaseout_threshold_mfj = EXCLUDED.phaseout_threshold_mfj, phaseout_rate = EXCLUDED.phaseout_rate,
    source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

INSERT INTO ref.fica_parameters
    (tax_year, ss_employee_rate, ss_wage_base, medicare_employee_rate,
     additional_medicare_rate, additional_medicare_threshold_single,
     additional_medicare_threshold_mfj, source_url, source_citation)
VALUES
    (2017, 0.06200, 127200.00, 0.01450, 0.00900, 200000.00, 250000.00,
     'https://www.ssa.gov/oact/cola/cbb.html',
     'SSA Contribution and Benefit Base TY2017 $127,200; Add''l Medicare per IRC s.3101(b)(2) (ACA s.9015 effective TY 2013+)')
ON CONFLICT (tax_year) DO UPDATE SET
    ss_employee_rate = EXCLUDED.ss_employee_rate, ss_wage_base = EXCLUDED.ss_wage_base,
    medicare_employee_rate = EXCLUDED.medicare_employee_rate,
    additional_medicare_rate = EXCLUDED.additional_medicare_rate,
    additional_medicare_threshold_single = EXCLUDED.additional_medicare_threshold_single,
    additional_medicare_threshold_mfj = EXCLUDED.additional_medicare_threshold_mfj,
    source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;
