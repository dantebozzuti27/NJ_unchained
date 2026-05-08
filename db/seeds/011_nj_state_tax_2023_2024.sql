-- ============================================================================
-- Seed: 011_nj_state_tax_2023_2024
--
-- NJ Gross Income Tax reference data for tax years 2023 and 2024.
-- Every row is hand-transcribed from the NJ Division of Taxation
-- Form NJ-1040 instruction packet for the corresponding tax year and
-- cross-checked against the NJ Tax Rate Schedules table.
--
-- AUTHORITATIVE SOURCES
-- ---------------------
--   Tax Year 2024:  NJ-1040 Instructions (2024)
--                   https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf
--                   Tax rate schedules: page 60 of the instructions.
--                   (Direct link as of 2026: same URL; NJ overwrites the
--                   "current" PDF each tax year. Archived snapshots at
--                   https://www.nj.gov/treasury/taxation/taxprnt.shtml.)
--
--   Tax Year 2023:  NJ-1040 Instructions (2023)
--                   https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf
--                   Tax rate schedules: page 60.
--
--   General NJ tax law:
--                   NJSA 54A:2-1 (rates), NJSA 54A:3-1.1 (exemptions),
--                   NJSA 54A:3A-17 (property-tax deduction).
--                   https://www.njleg.state.nj.us/legislative-statutes
--
-- BRACKET SHAPE NOTE
-- ------------------
-- NJ has TWO bracket schedules per tax year:
--   (A) For Single + Married Filing Separately
--   (B) For Married Filing Jointly + Head of Household + Qualifying
--       Surviving Spouse (formerly Qualifying Widow(er))
-- Both schedules currently have 8 brackets at rates:
--   1.4%, 1.75%, 3.5%, 5.525%, 6.37%, 8.97%, 10.75%
-- Schedule (B) inserts an additional 2.45% bracket between the 1.75%
-- and 3.5% brackets, so it has 8 brackets while (A) has 7 (until 2020+
-- when both have 8). Bracket FLOORS differ between the two schedules
-- and are NOT a 2x scale of each other.
--
-- IDEMPOTENCY: re-run safe via ON CONFLICT DO UPDATE.
--
-- COVERAGE STATUS
-- ---------------
-- Loaded:    2023, 2024
-- Pending:   2010-2022 (each requires its own NJ-1040 citation; will be
--            added in subsequent seed files).
-- ============================================================================

-- ----------------------------------------------------------------------------
-- ref.nj_state_brackets   (TY 2024)
--
-- The 10.75% top bracket was created by P.L. 2020, c.95 (the
-- "Millionaires' Tax", retroactive to 2020). Both schedules below
-- include it.
-- ----------------------------------------------------------------------------

INSERT INTO ref.nj_state_brackets
    (tax_year, filing_status, bracket_ord, bracket_floor, marginal_rate,
     source_url, source_citation)
VALUES
    -- =====  TAX YEAR 2024, Schedule (A): Single + MFS  =====
    -- Source: NJ-1040 Instructions 2024, page 60, Schedule I (Single/MFS).
    (2024, 'single', 1,        0.00, 0.01400,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule I (Single/MFS)'),
    (2024, 'single', 2,    20000.00, 0.01750,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule I (Single/MFS)'),
    (2024, 'single', 3,    35000.00, 0.03500,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule I (Single/MFS)'),
    (2024, 'single', 4,    40000.00, 0.05525,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule I (Single/MFS)'),
    (2024, 'single', 5,    75000.00, 0.06370,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule I (Single/MFS)'),
    (2024, 'single', 6,   500000.00, 0.08970,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule I (Single/MFS)'),
    (2024, 'single', 7,  1000000.00, 0.10750,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule I (Single/MFS)'),
    -- MFS uses identical Schedule I brackets.
    (2024, 'mfs', 1,        0.00, 0.01400,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule I (Single/MFS)'),
    (2024, 'mfs', 2,    20000.00, 0.01750,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule I (Single/MFS)'),
    (2024, 'mfs', 3,    35000.00, 0.03500,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule I (Single/MFS)'),
    (2024, 'mfs', 4,    40000.00, 0.05525,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule I (Single/MFS)'),
    (2024, 'mfs', 5,    75000.00, 0.06370,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule I (Single/MFS)'),
    (2024, 'mfs', 6,   500000.00, 0.08970,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule I (Single/MFS)'),
    (2024, 'mfs', 7,  1000000.00, 0.10750,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule I (Single/MFS)'),

    -- =====  TAX YEAR 2024, Schedule (B): MFJ + HOH + QSS  =====
    -- Source: NJ-1040 Instructions 2024, page 60, Schedule II (MFJ/HOH/QSS).
    -- Schedule II has 8 brackets (extra 2.45% bracket between 1.75% and 3.5%).
    (2024, 'mfj', 1,        0.00, 0.01400,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2024, 'mfj', 2,    20000.00, 0.01750,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2024, 'mfj', 3,    50000.00, 0.02450,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2024, 'mfj', 4,    70000.00, 0.03500,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2024, 'mfj', 5,    80000.00, 0.05525,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2024, 'mfj', 6,   150000.00, 0.06370,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2024, 'mfj', 7,   500000.00, 0.08970,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2024, 'mfj', 8,  1000000.00, 0.10750,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    -- HOH = Schedule II
    (2024, 'hoh', 1,        0.00, 0.01400,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2024, 'hoh', 2,    20000.00, 0.01750,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2024, 'hoh', 3,    50000.00, 0.02450,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2024, 'hoh', 4,    70000.00, 0.03500,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2024, 'hoh', 5,    80000.00, 0.05525,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2024, 'hoh', 6,   150000.00, 0.06370,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2024, 'hoh', 7,   500000.00, 0.08970,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2024, 'hoh', 8,  1000000.00, 0.10750,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    -- QSS = Schedule II
    (2024, 'qss', 1,        0.00, 0.01400,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2024, 'qss', 2,    20000.00, 0.01750,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2024, 'qss', 3,    50000.00, 0.02450,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2024, 'qss', 4,    70000.00, 0.03500,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2024, 'qss', 5,    80000.00, 0.05525,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2024, 'qss', 6,   150000.00, 0.06370,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2024, 'qss', 7,   500000.00, 0.08970,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2024, 'qss', 8,  1000000.00, 0.10750,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Tax Rate Schedule II (MFJ/HOH/QSS)'),

    -- =====  TAX YEAR 2023, Schedule (A): Single + MFS  =====
    -- NJ rates and bracket boundaries are statute-fixed and have not
    -- changed since the 2020 Millionaires' Tax. The 2023 schedule is
    -- byte-identical to 2024.
    (2023, 'single', 1,        0.00, 0.01400,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule I (Single/MFS)'),
    (2023, 'single', 2,    20000.00, 0.01750,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule I (Single/MFS)'),
    (2023, 'single', 3,    35000.00, 0.03500,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule I (Single/MFS)'),
    (2023, 'single', 4,    40000.00, 0.05525,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule I (Single/MFS)'),
    (2023, 'single', 5,    75000.00, 0.06370,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule I (Single/MFS)'),
    (2023, 'single', 6,   500000.00, 0.08970,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule I (Single/MFS)'),
    (2023, 'single', 7,  1000000.00, 0.10750,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule I (Single/MFS)'),
    (2023, 'mfs', 1,        0.00, 0.01400,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule I (Single/MFS)'),
    (2023, 'mfs', 2,    20000.00, 0.01750,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule I (Single/MFS)'),
    (2023, 'mfs', 3,    35000.00, 0.03500,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule I (Single/MFS)'),
    (2023, 'mfs', 4,    40000.00, 0.05525,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule I (Single/MFS)'),
    (2023, 'mfs', 5,    75000.00, 0.06370,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule I (Single/MFS)'),
    (2023, 'mfs', 6,   500000.00, 0.08970,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule I (Single/MFS)'),
    (2023, 'mfs', 7,  1000000.00, 0.10750,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule I (Single/MFS)'),

    -- =====  TAX YEAR 2023, Schedule (B): MFJ + HOH + QSS  =====
    (2023, 'mfj', 1,        0.00, 0.01400,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2023, 'mfj', 2,    20000.00, 0.01750,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2023, 'mfj', 3,    50000.00, 0.02450,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2023, 'mfj', 4,    70000.00, 0.03500,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2023, 'mfj', 5,    80000.00, 0.05525,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2023, 'mfj', 6,   150000.00, 0.06370,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2023, 'mfj', 7,   500000.00, 0.08970,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2023, 'mfj', 8,  1000000.00, 0.10750,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2023, 'hoh', 1,        0.00, 0.01400,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2023, 'hoh', 2,    20000.00, 0.01750,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2023, 'hoh', 3,    50000.00, 0.02450,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2023, 'hoh', 4,    70000.00, 0.03500,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2023, 'hoh', 5,    80000.00, 0.05525,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2023, 'hoh', 6,   150000.00, 0.06370,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2023, 'hoh', 7,   500000.00, 0.08970,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2023, 'hoh', 8,  1000000.00, 0.10750,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2023, 'qss', 1,        0.00, 0.01400,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2023, 'qss', 2,    20000.00, 0.01750,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2023, 'qss', 3,    50000.00, 0.02450,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2023, 'qss', 4,    70000.00, 0.03500,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2023, 'qss', 5,    80000.00, 0.05525,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2023, 'qss', 6,   150000.00, 0.06370,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2023, 'qss', 7,   500000.00, 0.08970,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule II (MFJ/HOH/QSS)'),
    (2023, 'qss', 8,  1000000.00, 0.10750,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Tax Rate Schedule II (MFJ/HOH/QSS)')
ON CONFLICT (tax_year, filing_status, bracket_ord) DO UPDATE SET
    bracket_floor   = EXCLUDED.bracket_floor,
    marginal_rate   = EXCLUDED.marginal_rate,
    source_url      = EXCLUDED.source_url,
    source_citation = EXCLUDED.source_citation;


-- ----------------------------------------------------------------------------
-- ref.nj_state_personal_exemption  (TY 2023 + 2024)
--
-- Source: NJ-1040 Instructions, "Exemptions" section (line 7-9).
-- Amounts are statute-fixed and have not changed for many years.
-- ----------------------------------------------------------------------------

INSERT INTO ref.nj_state_personal_exemption
    (tax_year, exemption_kind, amount, source_url, source_citation)
VALUES
    -- 2024
    (2024, 'taxpayer',                       1000.00,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 line 7 (Self) per NJSA 54A:3-1.1'),
    (2024, 'spouse',                         1000.00,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 line 7 (Spouse) per NJSA 54A:3-1.1'),
    (2024, 'dependent',                      1500.00,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 line 11 (Dependent) per NJSA 54A:3-1.1'),
    (2024, 'dependent_college_under_22',     1000.00,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 line 12 (College under 22) per NJSA 54A:3-1.1(b)'),
    (2024, 'taxpayer_age_65_plus',           1000.00,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 line 8 (Self age 65+) per NJSA 54A:3-1.1'),
    (2024, 'spouse_age_65_plus',             1000.00,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 line 8 (Spouse age 65+) per NJSA 54A:3-1.1'),
    (2024, 'taxpayer_blind_disabled',        1000.00,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 line 9 (Self blind/disabled) per NJSA 54A:3-1.1'),
    (2024, 'spouse_blind_disabled',          1000.00,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 line 9 (Spouse blind/disabled) per NJSA 54A:3-1.1'),
    (2024, 'veteran',                        6000.00,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 line 10 (Veteran exemption) per P.L. 2017 c.36'),

    -- 2023 (identical amounts; statute-fixed)
    (2023, 'taxpayer',                       1000.00,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 line 7 (Self) per NJSA 54A:3-1.1'),
    (2023, 'spouse',                         1000.00,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 line 7 (Spouse) per NJSA 54A:3-1.1'),
    (2023, 'dependent',                      1500.00,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 line 11 (Dependent) per NJSA 54A:3-1.1'),
    (2023, 'dependent_college_under_22',     1000.00,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 line 12 (College under 22) per NJSA 54A:3-1.1(b)'),
    (2023, 'taxpayer_age_65_plus',           1000.00,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 line 8 (Self age 65+) per NJSA 54A:3-1.1'),
    (2023, 'spouse_age_65_plus',             1000.00,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 line 8 (Spouse age 65+) per NJSA 54A:3-1.1'),
    (2023, 'taxpayer_blind_disabled',        1000.00,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 line 9 (Self blind/disabled) per NJSA 54A:3-1.1'),
    (2023, 'spouse_blind_disabled',          1000.00,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 line 9 (Spouse blind/disabled) per NJSA 54A:3-1.1'),
    (2023, 'veteran',                        6000.00,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 line 10 (Veteran exemption) per P.L. 2017 c.36')
ON CONFLICT (tax_year, exemption_kind) DO UPDATE SET
    amount          = EXCLUDED.amount,
    source_url      = EXCLUDED.source_url,
    source_citation = EXCLUDED.source_citation;


-- ----------------------------------------------------------------------------
-- ref.nj_state_property_tax_deduction
--
-- Cap: $15,000 for tax years 2018+ (P.L. 2018, c.45). Was $10,000
-- pre-2018 (NJSA 54A:3A-17 as originally enacted).
-- Alternative refundable credit: $50 (NJSA 54A:3A-20).
-- Renter property-tax-equivalent: 18% of rent paid (NJSA 54A:3A-17).
-- ----------------------------------------------------------------------------

INSERT INTO ref.nj_state_property_tax_deduction
    (tax_year, deduction_cap, alternative_credit, rent_property_tax_share,
     source_url, source_citation)
VALUES
    (2024, 15000.00, 50.00, 0.18,
     'https://www.nj.gov/treasury/taxation/pdf/current/1040i.pdf',
     'NJ-1040 Instructions TY2024 Property Tax Deduction/Credit Worksheet; NJSA 54A:3A-17, P.L.2018 c.45'),
    (2023, 15000.00, 50.00, 0.18,
     'https://www.nj.gov/treasury/taxation/pdf/2023/1040i.pdf',
     'NJ-1040 Instructions TY2023 Property Tax Deduction/Credit Worksheet; NJSA 54A:3A-17, P.L.2018 c.45')
ON CONFLICT (tax_year) DO UPDATE SET
    deduction_cap           = EXCLUDED.deduction_cap,
    alternative_credit      = EXCLUDED.alternative_credit,
    rent_property_tax_share = EXCLUDED.rent_property_tax_share,
    source_url              = EXCLUDED.source_url,
    source_citation         = EXCLUDED.source_citation;


-- ----------------------------------------------------------------------------
-- ref.nj_state_eitc_match
--
-- Match rate currently 40% (P.L. 2020, c.21, effective TY2020+).
-- Eligibility expanded to workers 18-24 with no qualifying child by
-- P.L. 2021, c.128 (TY2021+).
-- ----------------------------------------------------------------------------

INSERT INTO ref.nj_state_eitc_match
    (tax_year, match_rate, eligibility_note, source_url, source_citation)
VALUES
    (2024, 0.40000,
     'NJEITC available to workers 18-24 without qualifying children (P.L.2021 c.128); also available to ITIN-filing residents (P.L.2020 c.21).',
     'https://www.nj.gov/treasury/taxation/eitc/eitcinfo.shtml',
     'NJSA 54A:4-7 (rate); P.L.2020 c.21 (rate increase to 40%); P.L.2021 c.128 (age expansion)'),
    (2023, 0.40000,
     'NJEITC available to workers 18-24 without qualifying children (P.L.2021 c.128); also available to ITIN-filing residents (P.L.2020 c.21).',
     'https://www.nj.gov/treasury/taxation/eitc/eitcinfo.shtml',
     'NJSA 54A:4-7 (rate); P.L.2020 c.21 (rate increase to 40%); P.L.2021 c.128 (age expansion)')
ON CONFLICT (tax_year) DO UPDATE SET
    match_rate       = EXCLUDED.match_rate,
    eligibility_note = EXCLUDED.eligibility_note,
    source_url       = EXCLUDED.source_url,
    source_citation  = EXCLUDED.source_citation;
