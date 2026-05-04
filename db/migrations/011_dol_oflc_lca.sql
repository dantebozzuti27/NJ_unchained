-- ============================================================================
-- Migration: 011_dol_oflc_lca
--
-- TIER 3.5 / POP-2: Department of Labor Office of Foreign Labor Certification
-- (OFLC) Labor Condition Application (LCA) disclosure data. Worksite-level
-- statutory wage records for H-1B / E-3 / H-1B1 / H-2A / H-2B / PERM
-- petitions.
--
-- WHY THIS EXISTS
-- ---------------
-- LCA records are the only public, worksite-keyed, employer-keyed source of
-- statutory wage data for non-citizen workers. They are filed BEFORE USCIS
-- adjudication, so LCA volume overstates the eventually-approved population
-- (POP-3 USCIS H-1B Employer Data Hub will provide the approval-side cross-
-- check). We use LCA for:
--   (a) per-(county, fiscal_year, visa_class) median statutory wage,
--       the methodologically defensible H-1B household income substrate
--       (POP-5 derived.household_income_h1b_modeled).
--   (b) cross-check of PUMS-derived non-citizen wage estimates: if the
--       median PUMS WAGP for OCCP-matched non-citizens in a county is BELOW
--       the median LCA wage_rate_of_pay_from for the same SOC + county +
--       year, the PUMS estimate is suspect (PUMS top-codes and rounds; LCA
--       does not). Inversions raise governance.dataset_health(severity='warn').
--
-- SOURCE / SCHEMA EVOLUTION
-- -------------------------
-- DOL OFLC publishes quarterly disclosure files at
--   https://www.dol.gov/agencies/eta/foreign-labor/performance
-- The schema has changed approximately five times since FY2009:
--
--   v1_2008 (FY2008-2014): per-program files (e.g. H-1B_FY2010_Q4.xlsx).
--                          Columns LCA_CASE_NUMBER, LCA_CASE_EMPLOYER_NAME,
--                          STATUS, WAGE_RATE_1, WAGE_UNIT_1, WORK_LOCATION_*.
--   v2_2014 (FY2015-2017): consolidated H-1B file. CASE_NUMBER, CASE_STATUS,
--                          EMPLOYER_NAME, WAGE_RATE_OF_PAY_FROM,
--                          WAGE_RATE_OF_PAY_TO, WAGE_UNIT_OF_PAY.
--   v3_2018 (FY2018-2019): cross-program LCA file. Up to 10 worksites per
--                          LCA carried in WORKSITE_*_1..WORKSITE_*_10 wide
--                          columns; the ingester unstacks these to long.
--   v4_2020 (FY2020-2022): single WORKSITE_* columns (DOL switched to one
--                          row per (case, worksite) at the source).
--   v5_2023 (FY2023+):     stable, but added EMPLOYER_NAICS_CODE,
--                          EMPLOYER_PHONE, AGREE_TO_LC_STATEMENT.
--
-- The ingester (ingestion/dol_oflc_lca.py) detects schema version by
-- canonicalized column-set fingerprint (see _base.detect_schema_version).
--
-- WAGE ANNUALIZATION HAPPENS HERE, NOT IN PYTHON
-- ----------------------------------------------
-- annualized_wage_from / _to / _pw are GENERATED ALWAYS AS STORED columns
-- computed by Postgres. This is deliberate:
--   1. The annualization rule (Hour x 2080, Bi-Weekly x 26, etc.) is
--      auditable in `\d+ raw.lca_disclosure` rather than buried in Python.
--   2. There is exactly one place to fix if the rule changes (DOL has hinted
--      at adopting per-state annualization factors in future rulemaking).
--   3. Any tool that reads `raw.lca_disclosure` -- BI tool, ad-hoc psql --
--      gets the same annualized values without re-implementing the rule.
--
-- LICENSE
-- -------
-- DOL OFLC disclosure files are public domain. We attribute via
-- sources_manifest.toml [dol_oflc_lca]. CASE_NUMBER and EMPLOYER_NAME are
-- public. We do NOT publish individual worker names; the DOL files do not
-- contain them.
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- raw.lca_disclosure
--
-- One row per (fiscal_year, fiscal_quarter, case_number, worksite_idx).
-- Multi-worksite LCAs from v3_2018 are unstacked at ingest into
-- worksite_idx = 1..N rows.
--
-- The PRIMARY KEY includes (fiscal_year, fiscal_quarter) because the same
-- case_number can theoretically appear in re-issued quarterly files; we
-- prefer to ingest both and let downstream dedupe rather than silently
-- overwrite.
-- ----------------------------------------------------------------------------
CREATE TABLE raw.lca_disclosure (
    -- Period
    fiscal_year             SMALLINT      NOT NULL CHECK (fiscal_year BETWEEN 2008 AND 2099),
    fiscal_quarter          SMALLINT      NOT NULL CHECK (fiscal_quarter BETWEEN 1 AND 4),

    -- Case identification (raw from source)
    case_number             TEXT          NOT NULL,
    worksite_idx            SMALLINT      NOT NULL CHECK (worksite_idx BETWEEN 1 AND 10),
    case_status             TEXT          NOT NULL CHECK (case_status IN (
        'CERTIFIED',
        'CERTIFIED-WITHDRAWN',
        'WITHDRAWN',
        'DENIED'
    )),
    visa_class              TEXT          NOT NULL CHECK (visa_class IN (
        'H-1B', 'H-1B1 Chile', 'H-1B1 Singapore', 'E-3 Australian',
        'H-2A', 'H-2B', 'PERM', 'CW-1', 'OTHER'
    )),

    -- Dates
    received_date           DATE,
    decision_date           DATE,
    employment_start_date   DATE,
    employment_end_date     DATE,

    -- Employer (raw form)
    employer_name           TEXT          NOT NULL,
    employer_canonical_name TEXT          NOT NULL,    -- NFKD + lower + suffix-stripped
    employer_naics          CHAR(6),                   -- v5_2023+; NULL pre-FY2023
    employer_state          TEXT,
    employer_country        TEXT,

    -- Worksite
    worksite_city           TEXT,
    worksite_state          TEXT,
    worksite_postal_code    CHAR(5)       CHECK (
        worksite_postal_code IS NULL
        OR worksite_postal_code ~ '^[0-9]{5}$'
    ),

    -- Workers
    total_workers           INTEGER       CHECK (total_workers IS NULL OR total_workers >= 0),

    -- Filed wage range
    wage_rate_of_pay_from   NUMERIC(12,2) CHECK (wage_rate_of_pay_from IS NULL
                                                 OR wage_rate_of_pay_from >= 0),
    wage_rate_of_pay_to     NUMERIC(12,2) CHECK (wage_rate_of_pay_to IS NULL
                                                 OR wage_rate_of_pay_to >= 0),
    wage_unit_of_pay        TEXT          CHECK (wage_unit_of_pay IS NULL OR wage_unit_of_pay IN (
        'Hour', 'Week', 'Bi-Weekly', 'Month', 'Year'
    )),

    -- Annualized wages (GENERATED -- single source of truth in SQL).
    -- Conversion factors:
    --   Hour       x 2080  (40 hr/wk x 52 wks)
    --   Week       x 52
    --   Bi-Weekly  x 26
    --   Month      x 12
    --   Year       x 1
    -- Part-time positions are NOT re-scaled; the DOL filer is responsible
    -- for declaring the annualized equivalent on file.
    annualized_wage_from    NUMERIC(14,2) GENERATED ALWAYS AS (
        CASE wage_unit_of_pay
            WHEN 'Hour'      THEN wage_rate_of_pay_from * 2080
            WHEN 'Week'      THEN wage_rate_of_pay_from *   52
            WHEN 'Bi-Weekly' THEN wage_rate_of_pay_from *   26
            WHEN 'Month'     THEN wage_rate_of_pay_from *   12
            WHEN 'Year'      THEN wage_rate_of_pay_from
            ELSE NULL
        END
    ) STORED,
    annualized_wage_to      NUMERIC(14,2) GENERATED ALWAYS AS (
        CASE wage_unit_of_pay
            WHEN 'Hour'      THEN wage_rate_of_pay_to * 2080
            WHEN 'Week'      THEN wage_rate_of_pay_to *   52
            WHEN 'Bi-Weekly' THEN wage_rate_of_pay_to *   26
            WHEN 'Month'     THEN wage_rate_of_pay_to *   12
            WHEN 'Year'      THEN wage_rate_of_pay_to
            ELSE NULL
        END
    ) STORED,

    -- Prevailing wage (DOL-published statutory floor for the SOC)
    prevailing_wage         NUMERIC(12,2),
    pw_unit_of_pay          TEXT          CHECK (pw_unit_of_pay IS NULL OR pw_unit_of_pay IN (
        'Hour', 'Week', 'Bi-Weekly', 'Month', 'Year'
    )),
    annualized_pw           NUMERIC(14,2) GENERATED ALWAYS AS (
        CASE pw_unit_of_pay
            WHEN 'Hour'      THEN prevailing_wage * 2080
            WHEN 'Week'      THEN prevailing_wage *   52
            WHEN 'Bi-Weekly' THEN prevailing_wage *   26
            WHEN 'Month'     THEN prevailing_wage *   12
            WHEN 'Year'      THEN prevailing_wage
            ELSE NULL
        END
    ) STORED,
    pw_source               TEXT,                      -- 'OES', 'CBA', 'DBA', 'SCA', 'OTHER'
    soc_code                TEXT,
    job_title               TEXT,

    -- Provenance
    source_filename         TEXT          NOT NULL,
    source_sha256           CHAR(64)      NOT NULL,
    source_schema_version   TEXT          NOT NULL CHECK (source_schema_version IN (
        'v1_2008', 'v2_2014', 'v3_2018', 'v4_2020', 'v5_2023'
    )),
    data_quality            TEXT          NOT NULL DEFAULT 'measured'
        CHECK (data_quality IN ('measured', 'computed', 'modeled')),
    ingested_at             TIMESTAMPTZ   NOT NULL DEFAULT now(),

    PRIMARY KEY (fiscal_year, fiscal_quarter, case_number, worksite_idx),

    -- annualized_from <= annualized_to when both are present.
    -- (Cannot reference GENERATED columns in CHECK; this is the equivalent
    -- check on the underlying raw columns.)
    CHECK (
        wage_rate_of_pay_from IS NULL
        OR wage_rate_of_pay_to IS NULL
        OR wage_rate_of_pay_from <= wage_rate_of_pay_to
    )
);

CREATE INDEX idx_lca_employer
    ON raw.lca_disclosure (employer_canonical_name, fiscal_year);

CREATE INDEX idx_lca_worksite_zip
    ON raw.lca_disclosure (worksite_postal_code, fiscal_year)
    WHERE worksite_postal_code IS NOT NULL;

CREATE INDEX idx_lca_worksite_state
    ON raw.lca_disclosure (worksite_state, fiscal_year);

CREATE INDEX idx_lca_status_visa
    ON raw.lca_disclosure (case_status, visa_class, fiscal_year);

COMMENT ON TABLE raw.lca_disclosure IS
    'DOL OFLC LCA disclosure records, one row per (FY, FQ, case_number, '
    'worksite_idx). Multi-worksite LCAs (v3_2018) are unstacked at ingest. '
    'annualized_wage_* are GENERATED columns -- the conversion rule lives '
    'in SQL exactly once.';

COMMENT ON COLUMN raw.lca_disclosure.case_status IS
    'Headline aggregates filter to CERTIFIED only. The other three are '
    'retained because per-status mix (DENIED rate, CERTIFIED-WITHDRAWN '
    'rate per employer) is itself a governance.dataset_health signal.';

COMMENT ON COLUMN raw.lca_disclosure.employer_canonical_name IS
    'employer_name normalized via NFKD + lowercase + business-suffix strip '
    '(LLC / L.L.C. / INC / CORP / etc.). Index target. The raw '
    'employer_name is retained alongside for citation.';

-- ----------------------------------------------------------------------------
-- ref.suppression_threshold seed for the LCA aggregate
-- ----------------------------------------------------------------------------
INSERT INTO ref.suppression_threshold (table_name, rule_name, min_n, rationale)
VALUES (
    'derived.lca_wage_by_county_yr_visa',
    'percentile_min_unweighted_certs',
    10,
    'LCA is a CENSUS of certifications, not a survey, so we do not need a '
    'replicate-weight variance threshold; the suppression here is the '
    'empirical Bayesian risk that one extreme cert dominates the median. '
    '10 is conservative; revisit after backtest against ACS PUMS WAGP.'
)
ON CONFLICT (table_name, rule_name) DO NOTHING;

-- ----------------------------------------------------------------------------
-- derived.lca_wage_by_county_yr_visa
--
-- Headline aggregate. Filters to case_status='CERTIFIED' and allocates each
-- worksite's contribution by the HUD bus_ratio for the (zip, vintage that
-- matches the fiscal_year). A worksite ZIP that splits across counties
-- contributes a *fraction* of its total_workers and median wage to each
-- county weighted by bus_ratio.
--
-- We retain BOTH the unweighted observation count (n_unweighted_certs --
-- the suppression denominator) AND the bus_ratio-weighted count
-- (n_certs_weighted) so consumers can pick the right denominator.
-- ----------------------------------------------------------------------------
CREATE TABLE derived.lca_wage_by_county_yr_visa (
    county_id                     TEXT          NOT NULL REFERENCES ref.county(county_id),
    fiscal_year                   SMALLINT      NOT NULL CHECK (fiscal_year BETWEEN 2008 AND 2099),
    visa_class                    TEXT          NOT NULL,

    n_unweighted_certs            INTEGER       NOT NULL CHECK (n_unweighted_certs >= 0),
    n_certs_weighted              NUMERIC(14,4) NOT NULL CHECK (n_certs_weighted >= 0),
    n_workers_weighted            NUMERIC(14,4) NOT NULL CHECK (n_workers_weighted >= 0),

    median_annualized_wage_from   NUMERIC(14,2),
    p25_annualized_wage_from      NUMERIC(14,2),
    p75_annualized_wage_from      NUMERIC(14,2),
    median_prevailing_wage        NUMERIC(14,2),

    formula_version               TEXT          NOT NULL REFERENCES ref.formula_version(formula_version),
    input_vintage_hash            CHAR(64)      NOT NULL,
    method                        TEXT          NOT NULL DEFAULT 'lca_bus_ratio_allocation',
    data_quality                  TEXT          NOT NULL DEFAULT 'computed'
        CHECK (data_quality IN ('measured', 'computed', 'modeled')),
    computed_at                   TIMESTAMPTZ   NOT NULL DEFAULT now(),

    PRIMARY KEY (county_id, fiscal_year, visa_class,
                 formula_version, input_vintage_hash),

    -- Suppression invariant (mirrors ref.suppression_threshold seed above).
    -- This is enforced by the database, not by the aggregator. The
    -- aggregator computes the percentiles unconditionally; the database
    -- refuses to store them when the cell is too thin.
    CHECK (
        n_unweighted_certs >= 10
        OR (median_annualized_wage_from IS NULL
            AND p25_annualized_wage_from IS NULL
            AND p75_annualized_wage_from IS NULL
            AND median_prevailing_wage IS NULL)
    ),
    CHECK (
        median_annualized_wage_from IS NULL
        OR (p25_annualized_wage_from IS NULL
            OR p75_annualized_wage_from IS NULL
            OR (p25_annualized_wage_from <= median_annualized_wage_from
                AND median_annualized_wage_from <= p75_annualized_wage_from))
    )
);

CREATE INDEX idx_lca_wage_county_lookup
    ON derived.lca_wage_by_county_yr_visa (county_id, fiscal_year, visa_class);

COMMENT ON TABLE derived.lca_wage_by_county_yr_visa IS
    'Per-(county, FY, visa_class) median statutory wage, CERTIFIED-only, '
    'with worksite ZIP -> county allocated by HUD bus_ratio of the matching '
    'vintage. n_unweighted_certs is the suppression denominator.';

-- ----------------------------------------------------------------------------
-- Latest-vintage view (analytics-friendly)
-- ----------------------------------------------------------------------------
CREATE VIEW public.v_lca_wage_by_county_yr_visa_latest AS
SELECT DISTINCT ON (county_id, fiscal_year, visa_class) *
FROM derived.lca_wage_by_county_yr_visa
ORDER BY county_id, fiscal_year, visa_class, computed_at DESC;

COMMENT ON VIEW public.v_lca_wage_by_county_yr_visa_latest IS
    'Most-recent computation per (county, FY, visa_class). Use for ad-hoc '
    'analytics; query derived.lca_wage_by_county_yr_visa directly to '
    'reproduce a specific (formula_version, input_vintage_hash).';

COMMIT;
