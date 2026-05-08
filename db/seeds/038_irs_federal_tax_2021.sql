-- ============================================================================
-- Seed: 038_irs_federal_tax_2021
--
-- TY 2021: the ARPA YEAR. P.L. 117-2 (American Rescue Plan Act, signed
-- 2021-03-11) temporarily replaced the TCJA Child Tax Credit with the
-- ARPA-CTC for tax year 2021 only. ARPA expansion was NOT extended; CTC
-- reverted to TCJA structure for TY 2022 (s.21 of seed 016).
--
-- ARPA CTC PROVISIONS pinned by this seed (IRC s.24(i)):
--   amount_under_6     = $3,600   (vs $2,000 TCJA -> ARPA bump $1,600)
--   amount_6_to_17     = $3,000   (vs $2,000 TCJA -> ARPA bump $1,000)
--   refundable_max     = $3,600   (FULLY REFUNDABLE -- vs TCJA non-refundable
--                                 + $1,400 ACTC cap)
--
-- Two-stage phaseout (the schema-migration motivator):
--   Stage 1: phase the ARPA bump down to $2,000 per child at 5% above:
--     Single, MFS:    $75,000      (arpa_stage1_threshold_single)
--     HOH:            $112,500     (arpa_stage1_threshold_hoh)
--     MFJ, QSS:       $150,000     (arpa_stage1_threshold_mfj)
--   Stage 2: standard pre-ARPA single-stage phaseout of the $2,000 floor:
--     Single, HOH, MFS: $200,000   (phaseout_threshold_single)
--     MFJ, QSS:         $400,000   (phaseout_threshold_mfj)
--
-- BRACKETS, STANDARD DEDUCTION, PERSONAL EXEMPTION come from
-- Rev. Proc. 2020-45 (TY 2021 inflation adjustments). Personal exemption
-- remains $0 (TCJA s.11041, P.L. 115-97). Bracket floors are
-- inflation-indexed from TY 2020 floors via C-CPI-U per IRC s.1(j)(3).
--
-- AUTHORITATIVE SOURCES:
--   Rev. Proc. 2020-45 https://www.irs.gov/pub/irs-drop/rp-20-45.pdf
--   ARPA P.L. 117-2 s.9611 https://www.congress.gov/117/plaws/publ2/PLAW-117publ2.pdf
--   IRC s.24(i)(4) https://www.law.cornell.edu/uscode/text/26/24
--   SSA Fact Sheet 2020-10-13 (TY 2021 SS wage base $142,800)
-- ============================================================================

INSERT INTO ref.irs_federal_brackets (tax_year, filing_status, bracket_ord, bracket_floor, marginal_rate, source_url, source_citation) VALUES
    (2021, 'single', 1,        0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'Rev. Proc. 2020-45 s.3.01 Table 3'),
    (2021, 'single', 2,     9950.00, 0.12000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'Rev. Proc. 2020-45 s.3.01 Table 3'),
    (2021, 'single', 3,    40525.00, 0.22000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'Rev. Proc. 2020-45 s.3.01 Table 3'),
    (2021, 'single', 4,    86375.00, 0.24000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'Rev. Proc. 2020-45 s.3.01 Table 3'),
    (2021, 'single', 5,   164925.00, 0.32000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'Rev. Proc. 2020-45 s.3.01 Table 3'),
    (2021, 'single', 6,   209425.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'Rev. Proc. 2020-45 s.3.01 Table 3'),
    (2021, 'single', 7,   523600.00, 0.37000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'Rev. Proc. 2020-45 s.3.01 Table 3'),
    (2021, 'mfj', 1,        0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'Rev. Proc. 2020-45 s.3.01 Table 1'),
    (2021, 'mfj', 2,    19900.00, 0.12000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'Rev. Proc. 2020-45 s.3.01 Table 1'),
    (2021, 'mfj', 3,    81050.00, 0.22000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'Rev. Proc. 2020-45 s.3.01 Table 1'),
    (2021, 'mfj', 4,   172750.00, 0.24000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'Rev. Proc. 2020-45 s.3.01 Table 1'),
    (2021, 'mfj', 5,   329850.00, 0.32000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'Rev. Proc. 2020-45 s.3.01 Table 1'),
    (2021, 'mfj', 6,   418850.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'Rev. Proc. 2020-45 s.3.01 Table 1'),
    (2021, 'mfj', 7,   628300.00, 0.37000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'Rev. Proc. 2020-45 s.3.01 Table 1'),
    (2021, 'qss', 1,        0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'QSS=MFJ'),
    (2021, 'qss', 2,    19900.00, 0.12000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'QSS=MFJ'),
    (2021, 'qss', 3,    81050.00, 0.22000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'QSS=MFJ'),
    (2021, 'qss', 4,   172750.00, 0.24000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'QSS=MFJ'),
    (2021, 'qss', 5,   329850.00, 0.32000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'QSS=MFJ'),
    (2021, 'qss', 6,   418850.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'QSS=MFJ'),
    (2021, 'qss', 7,   628300.00, 0.37000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'QSS=MFJ'),
    (2021, 'mfs', 1,        0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'Rev. Proc. 2020-45 s.3.01 Table 4'),
    (2021, 'mfs', 2,     9950.00, 0.12000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'Rev. Proc. 2020-45 s.3.01 Table 4'),
    (2021, 'mfs', 3,    40525.00, 0.22000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'Rev. Proc. 2020-45 s.3.01 Table 4'),
    (2021, 'mfs', 4,    86375.00, 0.24000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'Rev. Proc. 2020-45 s.3.01 Table 4'),
    (2021, 'mfs', 5,   164925.00, 0.32000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'Rev. Proc. 2020-45 s.3.01 Table 4'),
    (2021, 'mfs', 6,   209425.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'Rev. Proc. 2020-45 s.3.01 Table 4'),
    (2021, 'mfs', 7,   314150.00, 0.37000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'Rev. Proc. 2020-45 s.3.01 Table 4 (= MFJ/2 = 628,300/2)'),
    (2021, 'hoh', 1,        0.00, 0.10000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'Rev. Proc. 2020-45 s.3.01 Table 2'),
    (2021, 'hoh', 2,    14200.00, 0.12000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'Rev. Proc. 2020-45 s.3.01 Table 2'),
    (2021, 'hoh', 3,    54200.00, 0.22000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'Rev. Proc. 2020-45 s.3.01 Table 2'),
    (2021, 'hoh', 4,    86350.00, 0.24000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'Rev. Proc. 2020-45 s.3.01 Table 2'),
    (2021, 'hoh', 5,   164900.00, 0.32000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'Rev. Proc. 2020-45 s.3.01 Table 2'),
    (2021, 'hoh', 6,   209400.00, 0.35000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'Rev. Proc. 2020-45 s.3.01 Table 2'),
    (2021, 'hoh', 7,   523600.00, 0.37000, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'Rev. Proc. 2020-45 s.3.01 Table 2')
ON CONFLICT (tax_year, filing_status, bracket_ord) DO UPDATE SET bracket_floor = EXCLUDED.bracket_floor, marginal_rate = EXCLUDED.marginal_rate, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

INSERT INTO ref.irs_standard_deduction (tax_year, filing_status, base_amount, additional_age_65, additional_blind, source_url, source_citation) VALUES
    (2021, 'single', 12550.00, 1700.00, 1700.00, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'Rev. Proc. 2020-45 s.3.20'),
    (2021, 'mfj',    25100.00, 1350.00, 1350.00, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'Rev. Proc. 2020-45 s.3.20'),
    (2021, 'mfs',    12550.00, 1350.00, 1350.00, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'Rev. Proc. 2020-45 s.3.20'),
    (2021, 'hoh',    18800.00, 1700.00, 1700.00, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'Rev. Proc. 2020-45 s.3.20'),
    (2021, 'qss',    25100.00, 1350.00, 1350.00, 'https://www.irs.gov/pub/irs-drop/rp-20-45.pdf', 'Rev. Proc. 2020-45 s.3.20')
ON CONFLICT (tax_year, filing_status) DO UPDATE SET base_amount = EXCLUDED.base_amount, additional_age_65 = EXCLUDED.additional_age_65, additional_blind = EXCLUDED.additional_blind, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

INSERT INTO ref.irs_personal_exemption (tax_year, amount, source_url, source_citation)
VALUES (2021, 0.00, 'https://www.law.cornell.edu/uscode/text/26/151', 'IRC s.151(d)(5)(A) (TCJA, P.L. 115-97 s.11041): exemption $0 for TY 2018-2025; ARPA did NOT touch s.151')
ON CONFLICT (tax_year) DO UPDATE SET amount = EXCLUDED.amount, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

-- ARPA CTC: this is the row that the entire schema migration 075/076 was for.
INSERT INTO ref.irs_child_tax_credit (
    tax_year, amount_under_6, amount_6_to_17, refundable_max_per_child,
    phaseout_threshold_single, phaseout_threshold_mfj, phaseout_rate,
    arpa_stage1_threshold_single, arpa_stage1_threshold_mfj, arpa_stage1_threshold_hoh,
    source_url, source_citation
) VALUES (
    2021, 3600.00, 3000.00, 3600.00,
    200000.00, 400000.00, 0.05000,
    75000.00, 150000.00, 112500.00,
    'https://www.law.cornell.edu/uscode/text/26/24',
    'IRC s.24(i) ARPA TY2021 only (P.L. 117-2 s.9611): $3,600/$3,000 fully refundable; two-stage phaseout (Stage1 $75K/$150K/$112.5K, Stage2 $200K/$400K). Reverts to TCJA TY2022.'
)
ON CONFLICT (tax_year) DO UPDATE SET
    amount_under_6 = EXCLUDED.amount_under_6, amount_6_to_17 = EXCLUDED.amount_6_to_17,
    refundable_max_per_child = EXCLUDED.refundable_max_per_child,
    phaseout_threshold_single = EXCLUDED.phaseout_threshold_single,
    phaseout_threshold_mfj = EXCLUDED.phaseout_threshold_mfj, phaseout_rate = EXCLUDED.phaseout_rate,
    arpa_stage1_threshold_single = EXCLUDED.arpa_stage1_threshold_single,
    arpa_stage1_threshold_mfj = EXCLUDED.arpa_stage1_threshold_mfj,
    arpa_stage1_threshold_hoh = EXCLUDED.arpa_stage1_threshold_hoh,
    source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;

INSERT INTO ref.fica_parameters (tax_year, ss_employee_rate, ss_wage_base, medicare_employee_rate, additional_medicare_rate, additional_medicare_threshold_single, additional_medicare_threshold_mfj, source_url, source_citation)
VALUES (2021, 0.06200, 142800.00, 0.01450, 0.00900, 200000.00, 250000.00, 'https://www.ssa.gov/oact/cola/cbb.html', 'SSA Contribution and Benefit Base TY2021 $142,800')
ON CONFLICT (tax_year) DO UPDATE SET ss_employee_rate = EXCLUDED.ss_employee_rate, ss_wage_base = EXCLUDED.ss_wage_base, medicare_employee_rate = EXCLUDED.medicare_employee_rate, additional_medicare_rate = EXCLUDED.additional_medicare_rate, additional_medicare_threshold_single = EXCLUDED.additional_medicare_threshold_single, additional_medicare_threshold_mfj = EXCLUDED.additional_medicare_threshold_mfj, source_url = EXCLUDED.source_url, source_citation = EXCLUDED.source_citation;
