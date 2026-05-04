-- ============================================================================
-- Migration: 026_census_acs_pums
--
-- TIER 3 (Population segmentation): ACS Public Use Microdata Sample (PUMS).
--
-- WHY THIS EXISTS
-- ---------------
-- Every other table in the platform is *aggregated* -- one row per (county,
-- year). Aggregations answer "is Bergen burdened?" but cannot answer "are
-- Hispanic renters in Bergen aged 25-34 burdened more than white renters?"
-- That second question is the platform's headline product question, and it
-- requires person-level microdata.
--
-- PUMS is a 1% (1-year) or 5% (5-year) sample of the ACS, released as
-- person-level and housing-unit-level records linked by SERIALNO. With
-- proper weighting (PWGTP / WGTP) it produces unbiased population estimates
-- for any subset of the US population that is large enough to satisfy
-- Census's disclosure-avoidance suppression rules.
--
-- IMPORTANT: PUMS IS A SAMPLE
-- ---------------------------
-- This is the first table in the platform that is NOT a population
-- aggregate. Two consequences:
--
--   1. Every analytical query against PUMS MUST weight by PWGTP (persons)
--      or WGTP (households). Naive `COUNT(*)` returns the SAMPLE count,
--      not the population count. The convention in derived.* views built
--      on PUMS is to ALWAYS apply weights and to NEVER expose un-weighted
--      counts to API consumers.
--
--   2. Standard errors require the 80 replicate weights (PWGTP1..80,
--      WGTP1..80). The Successive Differences Replication formula is
--      SE^2 = (4/80) * sum_i (theta_i - theta_hat)^2. Stored as a
--      Postgres array column for efficient bulk loading and to keep
--      the natural-key shape simple.
--
-- GEOGRAPHY: PUMA, NOT COUNTY
-- ---------------------------
-- PUMS records report the Public Use Microdata Area (PUMA), which is a
-- statistical geography of >=100K population. PUMA boundaries do NOT
-- align with county boundaries. NJ has ~50 PUMAs covering 21 counties.
-- Some counties span 2-3 PUMAs (e.g., Bergen) and some PUMAs span
-- multiple smaller counties.
--
-- The platform handles this in derived.* via a PUMA->county allocator
-- that uses PUMA->county population weights from Census's geographic
-- correspondence files. We do NOT pre-allocate at ingest time because
-- (a) the allocator is product/year specific (PUMA boundaries change
-- decennially after each Census), and (b) doing so loses the original
-- PUMA-grain precision that some queries need.
--
-- Tables in this migration store the canonical PUMA grain. PUMA->county
-- allocation lives in the derived layer (future migration).
--
-- VARIABLE SELECTION
-- ------------------
-- The full PUMS person record has ~280 variables and the housing record
-- has ~200. We project to a 30/22-column subset that covers:
--   * Geography (PUMA, ST)
--   * Demographics (AGEP, SEX, RAC1P, HISP, CIT, POBP, NATIVITY)
--   * Education / employment (SCHL, ESR, COW)
--   * Income (WAGP, PERNP, PINCP for person; HINCP, FINCP for housing)
--   * Housing (TEN, BDSP, RMSP, BLD, YBL, VEH, VALP, GRNTP, RNTP, SMOCP)
--   * Weights (PWGTP/WGTP + 80 replicate weights as INTEGER[])
--
-- Adding a column = (a) add it to ingestion/census_acs_pums.py PERSON_VARS
-- or HOUSING_VARS, (b) ALTER TABLE here in a new migration, (c) re-run.
-- The rest of the platform (asset, checks, API) is column-agnostic.
--
-- NATURAL KEY
-- -----------
-- Person:   (year, product, serialno, sporder)
-- Housing:  (year, product, serialno)
--
-- PUMS SERIALNO is unique per (year, product) but not globally; pre-2018
-- it was format YYYYxxxxxxxxxxxx, post-2018 it is YYYYGQxxxxxxxxxx.
-- We store it as TEXT and key composite to be safe.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- raw.acs_pums_person
--   One row per person-record in the PUMS sample.
--   Linked to raw.acs_pums_housing by (year, product, serialno).
-- ----------------------------------------------------------------------------
CREATE TABLE raw.acs_pums_person (
    -- Natural key
    year             SMALLINT  NOT NULL CHECK (year BETWEEN 2005 AND 2099),
    product          TEXT      NOT NULL CHECK (product IN ('acs1', 'acs5')),
    serialno         TEXT      NOT NULL,
    sporder          SMALLINT  NOT NULL CHECK (sporder >= 1),

    -- Geography
    state_fips       CHAR(2)   NOT NULL CHECK (state_fips ~ '^[0-9]{2}$'),
    puma             CHAR(5)   NOT NULL CHECK (puma ~ '^[0-9]{5}$'),

    -- Demographics
    agep             SMALLINT  CHECK (agep IS NULL OR agep BETWEEN 0 AND 99),
    sex              SMALLINT  CHECK (sex IS NULL OR sex IN (1, 2)),

    -- Race recode 1 (top-level): 1=White alone, 2=Black alone, 3=AIAN alone,
    -- 4=AIAN tribes specified, 5=Asian alone, 6=NHPI alone, 7=Some Other,
    -- 8=Two or more (incl. Some Other), 9=Two or more (excl. Some Other).
    rac1p            SMALLINT  CHECK (rac1p IS NULL OR rac1p BETWEEN 1 AND 9),

    -- Hispanic origin recode: 01=Not Hispanic, 02-24=specific origins.
    hisp             SMALLINT  CHECK (hisp IS NULL OR hisp BETWEEN 1 AND 24),

    -- Citizenship: 1=US-born, 2=PR/territories, 3=US parents abroad,
    -- 4=naturalized, 5=non-citizen.
    cit              SMALLINT  CHECK (cit IS NULL OR cit BETWEEN 1 AND 5),

    -- Place of birth (numeric code; 001-056 = US states/DC/territories,
    -- 100-554 = foreign countries). NULL when not collected.
    pobp             SMALLINT  CHECK (pobp IS NULL OR pobp BETWEEN 1 AND 999),

    -- Nativity: 1=native, 2=foreign-born. Convenience recode of CIT.
    nativity         SMALLINT  CHECK (nativity IS NULL OR nativity IN (1, 2)),

    -- Education / employment
    schl             SMALLINT  CHECK (schl IS NULL OR schl BETWEEN 1 AND 24),
    esr              SMALLINT  CHECK (esr IS NULL OR esr BETWEEN 1 AND 6),
    cow              SMALLINT  CHECK (cow IS NULL OR cow BETWEEN 1 AND 9),

    -- Income (top-coded by Census; values >= state_specific_topcode are
    -- replaced with the state median above the topcode).
    wagp             INTEGER   CHECK (wagp  IS NULL OR wagp  >= 0),
    pernp            INTEGER   CHECK (pernp IS NULL OR pernp BETWEEN -19998 AND 9999999),
    pincp            INTEGER   CHECK (pincp IS NULL OR pincp BETWEEN -19998 AND 9999999),

    -- Sampling weight (PWGTP); >= 0. Used as the population multiplier.
    pwgtp            INTEGER   NOT NULL CHECK (pwgtp >= 0),

    -- 80 replicate weights for SDR variance estimation. Length-80 array.
    -- We CHECK the array length at write time; mid-stream NULLs are not
    -- valid (Census always emits all 80).
    replicate_weights INTEGER[] NOT NULL
        CHECK (cardinality(replicate_weights) = 80),

    -- Provenance
    source_url       TEXT          NOT NULL,
    source_sha256    CHAR(64)      NOT NULL,
    source_vintage   TEXT          NOT NULL,
    ingested_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),

    PRIMARY KEY (year, product, serialno, sporder)
);

COMMENT ON TABLE raw.acs_pums_person IS
    'ACS Public Use Microdata Sample, person-level. One row per sampled '
    'person. Population estimates require weighting by pwgtp; standard '
    'errors require the replicate_weights array (SDR formula). Geography '
    'is at PUMA level, not county; allocate to county via correspondence '
    'in the derived layer.';

CREATE INDEX raw_acs_pums_person_puma_year_idx
    ON raw.acs_pums_person (state_fips, puma, year);

CREATE INDEX raw_acs_pums_person_serialno_idx
    ON raw.acs_pums_person (year, product, serialno);


-- ----------------------------------------------------------------------------
-- raw.acs_pums_housing
--   One row per housing-unit record in the PUMS sample.
--   Joined to raw.acs_pums_person by (year, product, serialno).
-- ----------------------------------------------------------------------------
CREATE TABLE raw.acs_pums_housing (
    -- Natural key
    year             SMALLINT  NOT NULL CHECK (year BETWEEN 2005 AND 2099),
    product          TEXT      NOT NULL CHECK (product IN ('acs1', 'acs5')),
    serialno         TEXT      NOT NULL,

    -- Geography
    state_fips       CHAR(2)   NOT NULL CHECK (state_fips ~ '^[0-9]{2}$'),
    puma             CHAR(5)   NOT NULL CHECK (puma ~ '^[0-9]{5}$'),

    -- Tenure: 1=owned-with-mortgage, 2=owned-free-and-clear,
    -- 3=rented, 4=occupied-without-payment-of-rent.
    ten              SMALLINT  CHECK (ten IS NULL OR ten BETWEEN 1 AND 4),

    -- Building characteristics
    bdsp             SMALLINT  CHECK (bdsp IS NULL OR bdsp BETWEEN 0 AND 20),
    rmsp             SMALLINT  CHECK (rmsp IS NULL OR rmsp BETWEEN 1 AND 20),
    bld              SMALLINT  CHECK (bld  IS NULL OR bld  BETWEEN 1 AND 10),
    ybl              SMALLINT  CHECK (ybl  IS NULL OR ybl  BETWEEN 1 AND 22),
    veh              SMALLINT  CHECK (veh  IS NULL OR veh  BETWEEN 0 AND 6),

    -- Costs (current-year dollars; PUMS does NOT pre-deflate)
    valp             INTEGER   CHECK (valp  IS NULL OR valp  BETWEEN 0 AND 999999999),
    grntp            INTEGER   CHECK (grntp IS NULL OR grntp BETWEEN 0 AND 99999),
    rntp             INTEGER   CHECK (rntp  IS NULL OR rntp  BETWEEN 0 AND 99999),
    smocp            INTEGER   CHECK (smocp IS NULL OR smocp BETWEEN 0 AND 99999),
    smp              INTEGER   CHECK (smp   IS NULL OR smp   BETWEEN 0 AND 99999),

    -- Income
    hincp            INTEGER   CHECK (hincp IS NULL OR hincp BETWEEN -29997 AND 999999999),
    fincp            INTEGER   CHECK (fincp IS NULL OR fincp BETWEEN -29997 AND 999999999),

    -- Weights
    wgtp             INTEGER   NOT NULL CHECK (wgtp >= 0),
    replicate_weights INTEGER[] NOT NULL
        CHECK (cardinality(replicate_weights) = 80),

    -- Provenance
    source_url       TEXT          NOT NULL,
    source_sha256    CHAR(64)      NOT NULL,
    source_vintage   TEXT          NOT NULL,
    ingested_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),

    PRIMARY KEY (year, product, serialno)
);

COMMENT ON TABLE raw.acs_pums_housing IS
    'ACS Public Use Microdata Sample, housing-unit-level. Linked to '
    'raw.acs_pums_person by (year, product, serialno). Costs are in '
    'current-year (= survey year) dollars; deflate via raw.cpi_u when '
    'comparing across years.';

CREATE INDEX raw_acs_pums_housing_puma_year_idx
    ON raw.acs_pums_housing (state_fips, puma, year);


-- ----------------------------------------------------------------------------
-- ref.puma2020
--   Reference table mapping NJ PUMA codes -> human-readable names.
--   Seeded separately (db/seeds/004_nj_puma_2020.sql, future).
--
--   We define the table here so the FK in any derived layer is stable
--   from day one. The seed file populates it.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ref.puma2020 (
    state_fips     CHAR(2) NOT NULL CHECK (state_fips ~ '^[0-9]{2}$'),
    puma           CHAR(5) NOT NULL CHECK (puma ~ '^[0-9]{5}$'),
    puma_name      TEXT    NOT NULL,
    population_est INTEGER CHECK (population_est IS NULL OR population_est > 0),
    PRIMARY KEY (state_fips, puma)
);

COMMENT ON TABLE ref.puma2020 IS
    '2020-vintage PUMA reference: PUMA code -> human name. PUMA boundaries '
    'change with each decennial Census; vintage-tag the table when the '
    '2030 boundaries are released. NJ-only seed data lives in '
    'db/seeds/004_nj_puma_2020.sql.';


-- ----------------------------------------------------------------------------
-- public.v_pums_nj_recent
--   Convenience view: NJ persons in the most recent loaded vintage,
--   joined to housing for the headline burden-relevant columns.
--
--   This is the BBG-terminal "PUMS<GO>" entry point. Not a derived
--   metric -- just a pre-joined sample for ad-hoc exploration.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW public.v_pums_nj_recent AS
WITH latest AS (
    SELECT MAX(year) AS year, product
    FROM raw.acs_pums_person
    WHERE state_fips = '34'
    GROUP BY product
)
SELECT
    p.year, p.product, p.puma,
    p.serialno, p.sporder,
    p.agep, p.sex, p.rac1p, p.hisp, p.cit, p.nativity,
    p.schl, p.esr, p.wagp, p.pincp, p.pwgtp,
    h.ten, h.bdsp, h.rmsp,
    h.valp, h.grntp, h.smocp,
    h.hincp,  h.wgtp
FROM raw.acs_pums_person p
JOIN latest USING (year, product)
LEFT JOIN raw.acs_pums_housing h
       ON h.year     = p.year
      AND h.product  = p.product
      AND h.serialno = p.serialno
WHERE p.state_fips = '34';

COMMENT ON VIEW public.v_pums_nj_recent IS
    'NJ-only most-recent-year ACS PUMS persons joined to housing. The '
    'BBG-terminal entry point for ad-hoc microdata exploration. Always '
    'apply pwgtp weighting in analytical queries.';
