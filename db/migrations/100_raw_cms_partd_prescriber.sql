-- ============================================================================
-- Migration: 100_raw_cms_partd_prescriber
--
-- TIER 4 v3 / FRAUD-F7 (CMS Medicare substrate slice): CMS Medicare Part D
-- Prescribers -- by Provider.
--
-- WHAT IT IS
-- ----------
-- CMS (Office of Enterprise Data & Analytics) publishes, annually, one row
-- per prescriber NPI summarizing that prescriber's Medicare Part D activity:
-- total claims, 30-day fills, drug cost, beneficiary counts, and a set of
-- pre-computed sub-totals (opioids, long-acting opioids, antibiotics,
-- antipsychotics-in-elderly, brand/generic). The file is free, keyless, and
-- downloadable as a single CSV per calendar year from data.cms.gov.
--
-- WHY THIS IS THE FIRST CMS SUBSTRATE WE LAND
-- -------------------------------------------
-- The platform already ingests the HHS-OIG LEIE exclusion list (mig 053) and
-- stores -- but has never used -- the LEIE NPI column (mig 053 lines 165-170
-- created a partial NPI index "for any future entity match against ...
-- Medicare claim files (both carry NPI)"). This table IS that future. An
-- exact NPI equijoin between an active LEIE exclusion and a Part D prescriber
-- row is the canonical, highest-precision healthcare-fraud signal: a provider
-- whom HHS-OIG has excluded from all federal health-care programs who is
-- nonetheless prescribing under Medicare Part D. (Methodological precedent:
-- the OpenPrescriber LEIE x Part D NPI cross-match, CY2023.)
--
-- This is FRAUD-F7 (CMS Medicare), which was a name-only stub in work_left.txt
-- with no schema. The healthcare-fraud pillar is not in the original `idea`
-- spec; per VISION_2026 Sec.5 rule 3 the rationale is documented in work_left.txt
-- before building.
--
-- WHY THE TABLE LOOKS LIKE THIS
-- -----------------------------
-- 1. Grain is (data_year, npi). CMS publishes one file per calendar year and
--    one row per prescriber NPI within it. data_year is supplied at load time
--    (it is not a column in the CSV) and doubles as the platform "cycle" so
--    the row plugs into the CHAR(4) cycle-keyed fraud-signal substrate.
--
-- 2. npi is TEXT, not an integer. NPIs are opaque 10-character identifiers;
--    integer coercion risks leading-zero corruption. The raw layer mirrors
--    the source string verbatim.
--
-- 3. Numeric columns are NULLABLE. CMS suppresses any aggregate cell derived
--    from fewer than 11 beneficiaries/claims (the opioid sub-totals are blank
--    for most low-volume prescribers). Per the verifiable-data invariant
--    (no silent imputation), a suppressed cell is loaded as SQL NULL -- it is
--    "no data", never 0. Downstream signals must treat NULL as absence.
--
-- 4. Only the subset of CMS columns the platform's current + near-term
--    signals consume is mirrored. The exclusion-billing signal needs NPI +
--    name + state + a billing magnitude (tot_drug_cst). The opioid fields
--    are carried so the Phase-2 opioid-prescribing-rate outlier signal can be
--    built without a schema change. The full ~80-column CMS file is not
--    mirrored; that is a deliberate scope choice, re-derivable from source.
--
-- 5. Provenance columns follow the platform contract: every raw row knows the
--    file URL it came from (source_url), the SHA-256 of that file
--    (source_sha256), and the publisher vintage (source_vintage = HTTP
--    ETag/Last-Modified when fetched, else a logical "CY<year>" label).
--
-- WHAT'S NOT IN THIS MIGRATION (deliberately)
-- -------------------------------------------
-- * The cross-source signal itself -- that is migration 101, which adds the
--   'provider' entity_kind + refresh_signal_provider_excluded_billing +
--   master-refresher wiring + evidence-view widening.
-- * The by-Provider-and-Drug file (per NPI x drug, ~25M rows) -- a separate,
--   much larger substrate, future work for drug-level outlier detection.
-- * NPPES / Physician&Other-Practitioners / Open Payments -- sibling CMS
--   substrates landing in their own migrations.
-- ============================================================================

BEGIN;


-- ----------------------------------------------------------------------------
-- Formula version registration
-- ----------------------------------------------------------------------------
INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '2.8.0-cms-medicare-substrate-v1',
    'Pillar 2 (civic integrity) FRAUD-F7 substrate slice. Lands the first '
    'CMS Medicare substrate: raw.cms_partd_prescriber (Medicare Part D '
    'Prescribers -- by Provider), one row per prescriber NPI per calendar '
    'year, from data.cms.gov (free/keyless CSV). Mirrors the NPI, name, '
    'state, specialty, billing magnitude (tot_clms/tot_drug_cst/tot_benes) '
    'and pre-computed opioid sub-totals. Suppressed (<11-beneficiary) cells '
    'load as SQL NULL per the no-silent-imputation invariant. This is the '
    'substrate the dormant LEIE NPI index (mig 053) was created for; the '
    'exclusion-billing cross-source signal ships in mig 101.',
    '2026-06-08'::DATE,
    'Stacks on 2.7.1-fraud-nj-state-candidate-on-leie-v1.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ----------------------------------------------------------------------------
-- raw.cms_partd_prescriber (one row per (data_year, npi))
-- ----------------------------------------------------------------------------
CREATE TABLE raw.cms_partd_prescriber (
    -- Calendar year of the published file. Supplied at load time (NOT a CSV
    -- column). Doubles as the platform "cycle" (CHAR(4)-compatible: 2013-2050).
    data_year             SMALLINT     NOT NULL
        CHECK (data_year BETWEEN 2013 AND 2050),

    -- Prescriber NPI ("Prscrbr_NPI"). 10-char opaque identifier; raw string.
    npi                   TEXT         NOT NULL
        CHECK (npi <> ''),

    -- Identity (NPPES-sourced in the CMS file, so aligns with LEIE name cols).
    prscrbr_last_org_name TEXT,        -- "Prscrbr_Last_Org_Name"
    prscrbr_first_name    TEXT,        -- "Prscrbr_First_Name"
    prscrbr_city          TEXT,        -- "Prscrbr_City"
    prscrbr_state_abrvtn  TEXT,        -- "Prscrbr_State_Abrvtn" (2-letter)
    prscrbr_type          TEXT,        -- "Prscrbr_Type" (specialty)

    -- Billing magnitude. NULL when CMS-suppressed (<11 benes/claims).
    tot_clms              NUMERIC,     -- "Tot_Clms"
    tot_drug_cst          NUMERIC,     -- "Tot_Drug_Cst" (gross Part D drug cost)
    tot_benes             NUMERIC,     -- "Tot_Benes"

    -- Pre-computed opioid sub-totals (carried for the Phase-2 opioid-rate
    -- outlier signal). NULL when suppressed -- a blank rate is NOT zero.
    opioid_tot_clms       NUMERIC,     -- "Opioid_Tot_Clms"
    opioid_prscrbr_rate   NUMERIC,     -- "Opioid_Prscrbr_Rate" (percent 0-100)

    -- Provenance / vintage.
    source_url            TEXT         NOT NULL,
    source_sha256         CHAR(64)     NOT NULL
        CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    source_vintage        TEXT         NOT NULL,
    ingested_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),

    PRIMARY KEY (data_year, npi)
);

COMMENT ON TABLE raw.cms_partd_prescriber IS
    'CMS Medicare Part D Prescribers -- by Provider. One row per prescriber '
    'NPI per calendar year (data_year). Free/keyless annual CSV from '
    'data.cms.gov. Numeric columns are NULL when CMS-suppressed (<11 '
    'beneficiaries/claims) -- absence, never zero. Substrate for the '
    'provider_excluded_billing cross-source signal (mig 101) and future '
    'opioid-rate outlier signals.';
COMMENT ON COLUMN raw.cms_partd_prescriber.data_year IS
    'Calendar year of the file; load-time parameter; doubles as cycle.';
COMMENT ON COLUMN raw.cms_partd_prescriber.npi IS
    'Prescriber NPI (Prscrbr_NPI). Exact join key to raw.hhs_oig_leie.npi.';
COMMENT ON COLUMN raw.cms_partd_prescriber.opioid_prscrbr_rate IS
    'CMS-precomputed Opioid_Prscrbr_Rate (percent). NULL = suppressed, not 0.';


-- NPI is the hot join path (LEIE x Part D exclusion match). Partial index
-- skips the placeholder NPI so the exclusion join scans only real NPIs.
CREATE INDEX raw_cms_partd_prescriber_npi_idx
    ON raw.cms_partd_prescriber (npi)
    WHERE npi <> '' AND npi <> '0000000000';

CREATE INDEX raw_cms_partd_prescriber_state_idx
    ON raw.cms_partd_prescriber (prscrbr_state_abrvtn);


-- ----------------------------------------------------------------------------
-- derived.v_cms_partd_prescriber_active
--
-- The most-recent loaded data_year only, restricted to rows with a real,
-- well-formed NPI (10 digits, not the all-zeros placeholder). This is the
-- "current Medicare Part D prescriber population" view -- the denominator
-- bucket for the exclusion-billing signal and the UI "latest year" surface.
-- Historical-year queries hit raw.cms_partd_prescriber with a data_year
-- filter directly.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.v_cms_partd_prescriber_active AS
WITH latest AS (
    SELECT MAX(data_year) AS data_year FROM raw.cms_partd_prescriber
)
SELECT
    p.data_year,
    p.npi,
    p.prscrbr_last_org_name,
    p.prscrbr_first_name,
    p.prscrbr_city,
    p.prscrbr_state_abrvtn,
    p.prscrbr_type,
    p.tot_clms,
    p.tot_drug_cst,
    p.tot_benes,
    p.opioid_tot_clms,
    p.opioid_prscrbr_rate
FROM raw.cms_partd_prescriber p
JOIN latest ON latest.data_year = p.data_year
WHERE p.npi ~ '^[0-9]{10}$'
  AND p.npi <> '0000000000';

COMMENT ON VIEW derived.v_cms_partd_prescriber_active IS
    'Most-recent data_year of raw.cms_partd_prescriber, valid-NPI rows only. '
    'Current Part D prescriber population. For historical years query the '
    'raw table with a data_year filter.';


COMMIT;
