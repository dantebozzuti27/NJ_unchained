-- ============================================================================
-- Migration: 103_raw_cms_open_payments_general
--
-- FRAUD-F7 substrate slice: CMS Open Payments -- General Payments detail.
--
-- Open Payments records every payment / transfer-of-value an applicable
-- manufacturer or GPO (pharma, device makers) makes to a covered
-- recipient (physician / non-physician practitioner, keyed by NPI).
-- Joined to Medicare Part D prescribing on the same NPI, it is the
-- substrate for a future kickback-correlation signal ("doctors who took
-- the most money from a drug maker also prescribed the most of that
-- maker's drug").
--
-- SIZE DISCIPLINE: the national General Payments detail file is ~7.6 GB /
-- ~14.7M rows raw (PY2023). The ingester (ingestion/cms_open_payments.py)
-- DEFAULTS to filtering rows to Recipient_State='NJ' (a `--national`
-- opt-in loads all), so this table holds the NJ slice unless an operator
-- explicitly widens it. That keeps the $0 / 200 GB-box constraint intact.
--
-- Grain: one row per Open Payments Record_ID (the source PK). The loader
-- COPYs into a typed TEMP staging table then INSERT ... SELECT DISTINCT ON
-- (record_id) with DELETE-WHERE-program_year for per-year idempotency,
-- NULL '' so blank numerics become SQL NULL (never 0).
-- ============================================================================

BEGIN;


INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '2.8.3-cms-open-payments-substrate-v1',
    'Pillar 2 (civic integrity) FRAUD-F7 substrate slice. Lands '
    'raw.cms_open_payments_general (CMS Open Payments General Payments '
    'detail), one row per Record_ID, from download.cms.gov. NJ-recipient '
    'default filter bounds the ~7.6 GB national file to the NJ slice '
    '(operator --national opt-in loads all). Substrate for a future '
    'industry-payment x Part D prescribing kickback-correlation signal.',
    '2026-06-09'::DATE,
    'Stacks on 2.8.2-cms-physician-substrate-v1.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


CREATE TABLE raw.cms_open_payments_general (
    record_id                    TEXT         NOT NULL
        CHECK (record_id <> ''),                 -- "Record_ID" (source PK)
    program_year                 SMALLINT     NOT NULL
        CHECK (program_year BETWEEN 2013 AND 2050),
    covered_recipient_npi        TEXT,           -- "Covered_Recipient_NPI"
    covered_recipient_profile_id TEXT,           -- "Covered_Recipient_Profile_ID"
    recipient_first_name         TEXT,           -- "Covered_Recipient_First_Name"
    recipient_last_name          TEXT,           -- "Covered_Recipient_Last_Name"
    recipient_state              TEXT,           -- "Recipient_State"
    payer_name                   TEXT,           -- manufacturer / GPO name
    payment_amount               NUMERIC,        -- "Total_Amount_of_Payment_USDollars"
    payment_date                 TEXT,           -- "Date_of_Payment" (raw text)
    nature_of_payment            TEXT,           -- "Nature_of_Payment_or_Transfer_of_Value"
    product_name                 TEXT,           -- drug/device name (slot 1)

    source_url                   TEXT         NOT NULL,
    source_sha256                CHAR(64)     NOT NULL
        CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    source_vintage               TEXT         NOT NULL,
    ingested_at                  TIMESTAMPTZ  NOT NULL DEFAULT now(),

    PRIMARY KEY (record_id)
);

COMMENT ON TABLE raw.cms_open_payments_general IS
    'CMS Open Payments -- General Payments detail. One row per Record_ID. '
    'Industry (pharma/device) -> physician payments, keyed by recipient '
    'NPI. Default-loaded as the NJ-recipient slice (ingester --national '
    'opt-in loads all states). Blank payment_amount loads as NULL, never 0. '
    'Substrate for a future kickback-correlation signal.';
COMMENT ON COLUMN raw.cms_open_payments_general.covered_recipient_npi IS
    'Recipient NPI; join key to CMS Part D / Part B prescribing. May be '
    'blank (NULL) for older records or non-NPI recipients.';

-- Hot join path: payments aggregated per recipient NPI (kickback signal).
CREATE INDEX raw_cms_open_payments_general_npi_idx
    ON raw.cms_open_payments_general (covered_recipient_npi)
    WHERE covered_recipient_npi IS NOT NULL
      AND covered_recipient_npi <> '';

CREATE INDEX raw_cms_open_payments_general_year_idx
    ON raw.cms_open_payments_general (program_year);


COMMIT;
