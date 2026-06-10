-- ============================================================================
-- Migration: 102_raw_cms_physician_provider
--
-- FRAUD-F7 substrate slice: CMS Medicare Physician & Other Practitioners
-- -- by Provider.
--
-- The broadest "active Medicare Part B biller" roster: one row per
-- rendering-provider NPI per calendar year, summarizing Part B
-- utilization (services, beneficiaries) and payments (submitted charges,
-- Medicare allowed, Medicare paid) plus the beneficiary average HCC risk
-- score. Free/keyless annual CSV from data.cms.gov (DKAN catalog,
-- resolved via data.json -- exact dataset-title match disambiguates the
-- "...- by Provider" file from the 10x-larger "...- by Provider and
-- Service" file).
--
-- This is the Part-B analog of mig 100's Part-D substrate. Together they
-- give the exclusion-billing signal (mig 101) full biller coverage:
-- prescribers (Part D) AND practitioners (Part B). The ingester is
-- ingestion/cms_physician.py (load_to_postgres COPYs exactly the columns
-- below, NULL '' so suppressed cells become SQL NULL).
--
-- Grain (data_year, npi); npi is TEXT (no leading-zero loss); numeric
-- columns NULLABLE because CMS suppresses <11-beneficiary cells and the
-- no-silent-imputation invariant requires NULL-not-0.
-- ============================================================================

BEGIN;


INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '2.8.2-cms-physician-substrate-v1',
    'Pillar 2 (civic integrity) FRAUD-F7 substrate slice. Lands '
    'raw.cms_physician_provider (CMS Medicare Physician & Other '
    'Practitioners -- by Provider), one row per rendering NPI per calendar '
    'year, from data.cms.gov. Part-B analog of mig 100''s Part-D substrate; '
    'extends the provider_excluded_billing signal''s biller coverage to '
    'Part B practitioners. Suppressed (<11-beneficiary) cells load as SQL '
    'NULL.',
    '2026-06-09'::DATE,
    'Stacks on 2.8.1-fraud-provider-excluded-billing-v1.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


CREATE TABLE raw.cms_physician_provider (
    data_year             SMALLINT     NOT NULL
        CHECK (data_year BETWEEN 2012 AND 2050),
    npi                   TEXT         NOT NULL
        CHECK (npi <> ''),

    prvdr_last_org_name   TEXT,        -- "Rndrng_Prvdr_Last_Org_Name"
    prvdr_first_name      TEXT,        -- "Rndrng_Prvdr_First_Name"
    prvdr_city            TEXT,        -- "Rndrng_Prvdr_City"
    prvdr_state_abrvtn    TEXT,        -- "Rndrng_Prvdr_State_Abrvtn"
    prvdr_type            TEXT,        -- "Rndrng_Prvdr_Type" (specialty)

    tot_benes             NUMERIC,     -- "Tot_Benes"        (NULL if suppressed)
    tot_srvcs             NUMERIC,     -- "Tot_Srvcs"
    tot_mdcr_alowd_amt    NUMERIC,     -- "Tot_Mdcr_Alowd_Amt"
    tot_mdcr_pymt_amt     NUMERIC,     -- "Tot_Mdcr_Pymt_Amt"
    tot_sbmtd_chrg        NUMERIC,     -- "Tot_Sbmtd_Chrg"
    bene_avg_risk_scre    NUMERIC,     -- "Bene_Avg_Risk_Scre"

    source_url            TEXT         NOT NULL,
    source_sha256         CHAR(64)     NOT NULL
        CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    source_vintage        TEXT         NOT NULL,
    ingested_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),

    PRIMARY KEY (data_year, npi)
);

COMMENT ON TABLE raw.cms_physician_provider IS
    'CMS Medicare Physician & Other Practitioners -- by Provider. One row '
    'per rendering NPI per calendar year. Free/keyless annual CSV from '
    'data.cms.gov. Numeric columns NULL when CMS-suppressed (<11 benes). '
    'Part-B biller roster for the provider_excluded_billing signal and '
    'future Part-B utilization-outlier signals.';
COMMENT ON COLUMN raw.cms_physician_provider.npi IS
    'Rendering provider NPI (Rndrng_NPI). Exact join key to LEIE / CMS Part D.';

CREATE INDEX raw_cms_physician_provider_npi_idx
    ON raw.cms_physician_provider (npi)
    WHERE npi <> '' AND npi <> '0000000000';

CREATE INDEX raw_cms_physician_provider_state_idx
    ON raw.cms_physician_provider (prvdr_state_abrvtn);


CREATE OR REPLACE VIEW derived.v_cms_physician_provider_active AS
WITH latest AS (
    SELECT MAX(data_year) AS data_year FROM raw.cms_physician_provider
)
SELECT
    p.data_year,
    p.npi,
    p.prvdr_last_org_name,
    p.prvdr_first_name,
    p.prvdr_city,
    p.prvdr_state_abrvtn,
    p.prvdr_type,
    p.tot_benes,
    p.tot_srvcs,
    p.tot_mdcr_alowd_amt,
    p.tot_mdcr_pymt_amt,
    p.tot_sbmtd_chrg,
    p.bene_avg_risk_scre
FROM raw.cms_physician_provider p
JOIN latest ON latest.data_year = p.data_year
WHERE p.npi ~ '^[0-9]{10}$'
  AND p.npi <> '0000000000';

COMMENT ON VIEW derived.v_cms_physician_provider_active IS
    'Most-recent data_year of raw.cms_physician_provider, valid-NPI rows '
    'only. Current Part B practitioner population.';


COMMIT;
