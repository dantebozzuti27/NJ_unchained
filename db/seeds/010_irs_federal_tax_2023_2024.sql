-- ============================================================================
-- Seed: 010_irs_federal_tax_2023_2024
--
-- Federal income-tax reference data for tax years 2023 and 2024.
-- Every row is hand-transcribed from the IRS Revenue Procedure that
-- authorized the inflation adjustments for that year and cross-checked
-- against IRS Publication 17 examples.
--
-- AUTHORITATIVE SOURCES
-- ---------------------
--   Tax Year 2024:  Rev. Proc. 2023-34 (released 2023-11-09)
--                   https://www.irs.gov/pub/irs-drop/rp-23-34.pdf
--                   IRS Tax Inflation Adjustments page:
--                   https://www.irs.gov/newsroom/irs-provides-tax-inflation-adjustments-for-tax-year-2024
--
--   Tax Year 2023:  Rev. Proc. 2022-38 (released 2022-10-18)
--                   https://www.irs.gov/pub/irs-drop/rp-22-38.pdf
--                   https://www.irs.gov/newsroom/irs-provides-tax-inflation-adjustments-for-tax-year-2023
--
-- COVERAGE STATUS
-- ---------------
-- Loaded:    2023, 2024
-- Pending:   2010-2022 (each requires its own Rev. Proc. citation; will
--            be added in subsequent seed files NNN_irs_federal_tax_<year>.sql
--            with the same per-row provenance discipline).
-- Pending:   2025+ (Rev. Proc. 2024-40 published 2024-10-22; will be added
--            when we have a 2025 use case).
--
-- IDEMPOTENCY
-- -----------
-- All inserts use ON CONFLICT DO UPDATE so re-running the seed file is
-- safe and amendments are loud (the row's source_citation will change).
-- ============================================================================

-- ----------------------------------------------------------------------------
-- ref.irs_federal_brackets   (TY 2024 = Rev. Proc. 2023-34, Section 3.01)
--
-- The 7-bracket TCJA schedule (rates 10/12/22/24/32/35/37 percent),
-- inflation-adjusted from the 2017 statutory amounts.
-- ----------------------------------------------------------------------------

INSERT INTO ref.irs_federal_brackets
    (tax_year, filing_status, bracket_ord, bracket_floor, marginal_rate,
     source_url, source_citation)
VALUES
    -- Single  (Rev. Proc. 2023-34, Section 3.01, Table 1)
    (2024, 'single', 1,        0.00, 0.10000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 1 (Single)'),
    (2024, 'single', 2,    11600.00, 0.12000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 1 (Single)'),
    (2024, 'single', 3,    47150.00, 0.22000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 1 (Single)'),
    (2024, 'single', 4,   100525.00, 0.24000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 1 (Single)'),
    (2024, 'single', 5,   191950.00, 0.32000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 1 (Single)'),
    (2024, 'single', 6,   243725.00, 0.35000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 1 (Single)'),
    (2024, 'single', 7,   609350.00, 0.37000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 1 (Single)'),

    -- Married Filing Jointly   (Rev. Proc. 2023-34, Section 3.01, Table 2)
    (2024, 'mfj', 1,        0.00, 0.10000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 2 (MFJ)'),
    (2024, 'mfj', 2,    23200.00, 0.12000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 2 (MFJ)'),
    (2024, 'mfj', 3,    94300.00, 0.22000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 2 (MFJ)'),
    (2024, 'mfj', 4,   201050.00, 0.24000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 2 (MFJ)'),
    (2024, 'mfj', 5,   383900.00, 0.32000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 2 (MFJ)'),
    (2024, 'mfj', 6,   487450.00, 0.35000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 2 (MFJ)'),
    (2024, 'mfj', 7,   731200.00, 0.37000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 2 (MFJ)'),

    -- Qualifying Surviving Spouse: rates and brackets identical to MFJ.
    -- (Rev. Proc. 2023-34, Section 3.01, Table 2 (last paragraph))
    (2024, 'qss', 1,        0.00, 0.10000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 2 (QSS = MFJ schedule)'),
    (2024, 'qss', 2,    23200.00, 0.12000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 2 (QSS = MFJ schedule)'),
    (2024, 'qss', 3,    94300.00, 0.22000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 2 (QSS = MFJ schedule)'),
    (2024, 'qss', 4,   201050.00, 0.24000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 2 (QSS = MFJ schedule)'),
    (2024, 'qss', 5,   383900.00, 0.32000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 2 (QSS = MFJ schedule)'),
    (2024, 'qss', 6,   487450.00, 0.35000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 2 (QSS = MFJ schedule)'),
    (2024, 'qss', 7,   731200.00, 0.37000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 2 (QSS = MFJ schedule)'),

    -- Married Filing Separately  (Rev. Proc. 2023-34, s.3.01, Table 3)
    -- Brackets 1-5 mirror Single (= MFJ / 2 by IRC s.1(j)(2)(D)); the
    -- top two brackets are MFJ / 2 floors (i.e. 35% starts at $243,725
    -- like Single, but 37% starts at $365,600 = 731,200 / 2 -- DIFFERENT
    -- from Single's $609,350).
    (2024, 'mfs', 1,        0.00, 0.10000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 3 (MFS)'),
    (2024, 'mfs', 2,    11600.00, 0.12000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 3 (MFS)'),
    (2024, 'mfs', 3,    47150.00, 0.22000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 3 (MFS)'),
    (2024, 'mfs', 4,   100525.00, 0.24000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 3 (MFS)'),
    (2024, 'mfs', 5,   191950.00, 0.32000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 3 (MFS)'),
    (2024, 'mfs', 6,   243725.00, 0.35000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 3 (MFS)'),
    (2024, 'mfs', 7,   365600.00, 0.37000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 3 (MFS)'),

    -- Head of Household  (Rev. Proc. 2023-34, s.3.01, Table 4)
    (2024, 'hoh', 1,        0.00, 0.10000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 4 (HOH)'),
    (2024, 'hoh', 2,    16550.00, 0.12000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 4 (HOH)'),
    (2024, 'hoh', 3,    63100.00, 0.22000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 4 (HOH)'),
    (2024, 'hoh', 4,   100500.00, 0.24000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 4 (HOH)'),
    (2024, 'hoh', 5,   191950.00, 0.32000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 4 (HOH)'),
    (2024, 'hoh', 6,   243700.00, 0.35000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 4 (HOH)'),
    (2024, 'hoh', 7,   609350.00, 0.37000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.01 Table 4 (HOH)'),

    -- =====================  TAX YEAR 2023  ============================
    -- Source: Rev. Proc. 2022-38, Section 3.01

    -- Single  (Table 1)
    (2023, 'single', 1,        0.00, 0.10000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 1 (Single)'),
    (2023, 'single', 2,    11000.00, 0.12000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 1 (Single)'),
    (2023, 'single', 3,    44725.00, 0.22000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 1 (Single)'),
    (2023, 'single', 4,    95375.00, 0.24000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 1 (Single)'),
    (2023, 'single', 5,   182100.00, 0.32000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 1 (Single)'),
    (2023, 'single', 6,   231250.00, 0.35000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 1 (Single)'),
    (2023, 'single', 7,   578125.00, 0.37000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 1 (Single)'),

    -- MFJ (Table 2)
    (2023, 'mfj', 1,        0.00, 0.10000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 2 (MFJ)'),
    (2023, 'mfj', 2,    22000.00, 0.12000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 2 (MFJ)'),
    (2023, 'mfj', 3,    89450.00, 0.22000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 2 (MFJ)'),
    (2023, 'mfj', 4,   190750.00, 0.24000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 2 (MFJ)'),
    (2023, 'mfj', 5,   364200.00, 0.32000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 2 (MFJ)'),
    (2023, 'mfj', 6,   462500.00, 0.35000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 2 (MFJ)'),
    (2023, 'mfj', 7,   693750.00, 0.37000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 2 (MFJ)'),

    -- QSS = MFJ schedule
    (2023, 'qss', 1,        0.00, 0.10000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 2 (QSS = MFJ)'),
    (2023, 'qss', 2,    22000.00, 0.12000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 2 (QSS = MFJ)'),
    (2023, 'qss', 3,    89450.00, 0.22000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 2 (QSS = MFJ)'),
    (2023, 'qss', 4,   190750.00, 0.24000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 2 (QSS = MFJ)'),
    (2023, 'qss', 5,   364200.00, 0.32000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 2 (QSS = MFJ)'),
    (2023, 'qss', 6,   462500.00, 0.35000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 2 (QSS = MFJ)'),
    (2023, 'qss', 7,   693750.00, 0.37000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 2 (QSS = MFJ)'),

    -- MFS (Table 3)
    (2023, 'mfs', 1,        0.00, 0.10000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 3 (MFS)'),
    (2023, 'mfs', 2,    11000.00, 0.12000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 3 (MFS)'),
    (2023, 'mfs', 3,    44725.00, 0.22000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 3 (MFS)'),
    (2023, 'mfs', 4,    95375.00, 0.24000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 3 (MFS)'),
    (2023, 'mfs', 5,   182100.00, 0.32000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 3 (MFS)'),
    (2023, 'mfs', 6,   231250.00, 0.35000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 3 (MFS)'),
    (2023, 'mfs', 7,   346875.00, 0.37000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 3 (MFS)'),

    -- HOH (Table 4)
    (2023, 'hoh', 1,        0.00, 0.10000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 4 (HOH)'),
    (2023, 'hoh', 2,    15700.00, 0.12000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 4 (HOH)'),
    (2023, 'hoh', 3,    59850.00, 0.22000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 4 (HOH)'),
    (2023, 'hoh', 4,    95350.00, 0.24000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 4 (HOH)'),
    (2023, 'hoh', 5,   182100.00, 0.32000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 4 (HOH)'),
    (2023, 'hoh', 6,   231250.00, 0.35000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 4 (HOH)'),
    (2023, 'hoh', 7,   578100.00, 0.37000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.01 Table 4 (HOH)')
ON CONFLICT (tax_year, filing_status, bracket_ord) DO UPDATE SET
    bracket_floor   = EXCLUDED.bracket_floor,
    marginal_rate   = EXCLUDED.marginal_rate,
    source_url      = EXCLUDED.source_url,
    source_citation = EXCLUDED.source_citation;


-- ----------------------------------------------------------------------------
-- ref.irs_standard_deduction
--
-- 2024 (Rev. Proc. 2023-34 s.3.16, additional age/blind s.3.17):
--   Single $14,600  MFJ $29,200  MFS $14,600  HOH $21,900  QSS $29,200
--   Additional age 65/blind: $1,950 (Single, HOH); $1,550 (MFJ, MFS, QSS)
--
-- 2023 (Rev. Proc. 2022-38 s.3.16, s.3.17):
--   Single $13,850  MFJ $27,700  MFS $13,850  HOH $20,800  QSS $27,700
--   Additional age 65/blind: $1,850 (Single, HOH); $1,500 (MFJ, MFS, QSS)
-- ----------------------------------------------------------------------------

INSERT INTO ref.irs_standard_deduction
    (tax_year, filing_status, base_amount, additional_age_65, additional_blind,
     source_url, source_citation)
VALUES
    -- 2024
    (2024, 'single', 14600.00, 1950.00, 1950.00,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.16 (base) + s.3.17 (age/blind add-on)'),
    (2024, 'mfj',    29200.00, 1550.00, 1550.00,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.16 (base) + s.3.17 (age/blind add-on)'),
    (2024, 'mfs',    14600.00, 1550.00, 1550.00,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.16 (base) + s.3.17 (age/blind add-on)'),
    (2024, 'hoh',    21900.00, 1950.00, 1950.00,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.16 (base) + s.3.17 (age/blind add-on)'),
    (2024, 'qss',    29200.00, 1550.00, 1550.00,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'Rev. Proc. 2023-34 s.3.16 (base) + s.3.17 (age/blind add-on)'),

    -- 2023
    (2023, 'single', 13850.00, 1850.00, 1850.00,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.16 (base) + s.3.17 (age/blind add-on)'),
    (2023, 'mfj',    27700.00, 1500.00, 1500.00,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.16 (base) + s.3.17 (age/blind add-on)'),
    (2023, 'mfs',    13850.00, 1500.00, 1500.00,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.16 (base) + s.3.17 (age/blind add-on)'),
    (2023, 'hoh',    20800.00, 1850.00, 1850.00,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.16 (base) + s.3.17 (age/blind add-on)'),
    (2023, 'qss',    27700.00, 1500.00, 1500.00,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'Rev. Proc. 2022-38 s.3.16 (base) + s.3.17 (age/blind add-on)')
ON CONFLICT (tax_year, filing_status) DO UPDATE SET
    base_amount        = EXCLUDED.base_amount,
    additional_age_65  = EXCLUDED.additional_age_65,
    additional_blind   = EXCLUDED.additional_blind,
    source_url         = EXCLUDED.source_url,
    source_citation    = EXCLUDED.source_citation;


-- ----------------------------------------------------------------------------
-- ref.irs_personal_exemption
--
-- TCJA (2018-2025): set to $0 by IRC s.151(d)(5)(A) as added by Pub. L.
-- 115-97 s.11041(a). Rev. Proc.s for these years confirm "no personal
-- exemption" in the inflation-adjustment list.
-- ----------------------------------------------------------------------------

INSERT INTO ref.irs_personal_exemption
    (tax_year, amount, source_url, source_citation)
VALUES
    (2024, 0.00,
     'https://www.law.cornell.edu/uscode/text/26/151',
     'IRC s.151(d)(5)(A) (TCJA, P.L. 115-97 s.11041): exemption $0 for TY 2018-2025'),
    (2023, 0.00,
     'https://www.law.cornell.edu/uscode/text/26/151',
     'IRC s.151(d)(5)(A) (TCJA, P.L. 115-97 s.11041): exemption $0 for TY 2018-2025')
ON CONFLICT (tax_year) DO UPDATE SET
    amount          = EXCLUDED.amount,
    source_url      = EXCLUDED.source_url,
    source_citation = EXCLUDED.source_citation;


-- ----------------------------------------------------------------------------
-- ref.irs_child_tax_credit
--
-- TCJA: $2,000/child under 17, refundable portion $1,400 (2018) ->
-- $1,500 (2022) -> $1,600 (2023) -> $1,700 (2024). Phase-out at AGI
-- $200K (single/HOH) or $400K (MFJ) at $50/$1,000 = 5%.
-- ----------------------------------------------------------------------------

INSERT INTO ref.irs_child_tax_credit
    (tax_year,
     amount_under_6, amount_6_to_17, refundable_max_per_child,
     phaseout_threshold_single, phaseout_threshold_mfj, phaseout_rate,
     source_url, source_citation)
VALUES
    (2024,
     2000.00, 2000.00, 1700.00,
     200000.00, 400000.00, 0.05000,
     'https://www.irs.gov/pub/irs-drop/rp-23-34.pdf',
     'IRC s.24(h) (TCJA); refundable max per Rev. Proc. 2023-34 s.3.07 (TY 2024 = $1,700)'),
    (2023,
     2000.00, 2000.00, 1600.00,
     200000.00, 400000.00, 0.05000,
     'https://www.irs.gov/pub/irs-drop/rp-22-38.pdf',
     'IRC s.24(h) (TCJA); refundable max per Rev. Proc. 2022-38 s.3.07 (TY 2023 = $1,600)')
ON CONFLICT (tax_year) DO UPDATE SET
    amount_under_6           = EXCLUDED.amount_under_6,
    amount_6_to_17           = EXCLUDED.amount_6_to_17,
    refundable_max_per_child = EXCLUDED.refundable_max_per_child,
    phaseout_threshold_single = EXCLUDED.phaseout_threshold_single,
    phaseout_threshold_mfj    = EXCLUDED.phaseout_threshold_mfj,
    phaseout_rate            = EXCLUDED.phaseout_rate,
    source_url               = EXCLUDED.source_url,
    source_citation          = EXCLUDED.source_citation;


-- ----------------------------------------------------------------------------
-- ref.fica_parameters
--
-- 2024: SS wage base = $168,600 (SSA Fact Sheet 2024-10-12).
-- 2023: SS wage base = $160,200 (SSA Fact Sheet 2022-10-13).
-- Rates fixed by IRC s.3101 (employee 6.2% OASDI + 1.45% HI) and
-- s.3101(b)(2) (additional 0.9% Medicare on wages > $200K single
-- / $250K MFJ, since tax year 2013).
-- ----------------------------------------------------------------------------

INSERT INTO ref.fica_parameters
    (tax_year,
     ss_employee_rate, ss_wage_base,
     medicare_employee_rate,
     additional_medicare_rate,
     additional_medicare_threshold_single,
     additional_medicare_threshold_mfj,
     source_url, source_citation)
VALUES
    (2024,
     0.06200, 168600.00,
     0.01450,
     0.00900, 200000.00, 250000.00,
     'https://www.ssa.gov/oact/cola/cbb.html',
     'SSA: SS wage base $168,600 for 2024; Medicare add''l per IRC s.3101(b)(2)'),
    (2023,
     0.06200, 160200.00,
     0.01450,
     0.00900, 200000.00, 250000.00,
     'https://www.ssa.gov/oact/cola/cbb.html',
     'SSA: SS wage base $160,200 for 2023; Medicare add''l per IRC s.3101(b)(2)')
ON CONFLICT (tax_year) DO UPDATE SET
    ss_employee_rate                     = EXCLUDED.ss_employee_rate,
    ss_wage_base                         = EXCLUDED.ss_wage_base,
    medicare_employee_rate               = EXCLUDED.medicare_employee_rate,
    additional_medicare_rate             = EXCLUDED.additional_medicare_rate,
    additional_medicare_threshold_single = EXCLUDED.additional_medicare_threshold_single,
    additional_medicare_threshold_mfj    = EXCLUDED.additional_medicare_threshold_mfj,
    source_url                           = EXCLUDED.source_url,
    source_citation                      = EXCLUDED.source_citation;
