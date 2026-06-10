-- ============================================================================
-- Migration: 104_raw_nj_medicaid_exclusion
--
-- FRAUD-F7 substrate slice: New Jersey Medicaid exclusion / debarment
-- list -- NJ's state-level analog to the federal HHS-OIG LEIE.
--
-- Source (keyless): the OpenSanctions dataset us_nj_med_exclusions, a
-- simplified daily CSV re-derived from the authoritative NJ Office of the
-- State Comptroller (OSC) debarment PDF, served from a CDN that honors
-- conditional GET. The ingester is ingestion/nj_medicaid_exclusion.py.
--
-- This table is built on the SAME skeleton as raw.hhs_oig_leie (mig 053):
-- a stable surrogate record_hash PK (SHA-256 over the mapped content
-- columns), and a last_seen_at "tripwire". NJ, like HHS-OIG, does NOT
-- publish a reinstatement delta -- a removed provider simply drops out of
-- the next pull. The platform detects that by watching last_seen_at fall
-- behind MAX(last_seen_at) (see derived.v_nj_medicaid_exclusion_active).
--
-- The loader UPSERTs ON CONFLICT (record_hash) DO UPDATE last_seen_at =
-- now() and refreshes provenance, with COPY NULL '' so blank cells become
-- SQL NULL. action + expiration_date are always NULL in the simplified
-- export (no per-row action-type/expiration column exists upstream); the
-- PDF fallback is where an operator would populate them -- no silent
-- imputation here.
--
-- This substrate powers a future Phase-3 signal: a NJ-Medicaid-excluded
-- provider (with NPI) still billing Medicare -- the state-exclusion
-- mirror of provider_excluded_billing (mig 101).
-- ============================================================================

BEGIN;


INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '2.8.4-nj-medicaid-exclusion-substrate-v1',
    'Pillar 2 (civic integrity) FRAUD-F7 substrate slice. Lands '
    'raw.nj_medicaid_exclusion (NJ Medicaid / OSC debarment list) via the '
    'keyless OpenSanctions us_nj_med_exclusions daily CSV. Mirrors the LEIE '
    'skeleton (record_hash PK + last_seen_at reinstatement tripwire). '
    'action + expiration_date are NULL in the simplified export (no silent '
    'imputation). Substrate for the future state-excluded-provider-billing '
    'signal.',
    '2026-06-09'::DATE,
    'Stacks on 2.8.3-cms-open-payments-substrate-v1.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


CREATE TABLE raw.nj_medicaid_exclusion (
    -- SHA-256 over the 9 mapped content columns (mirrors LEIE). An
    -- OpenSanctions profile correction yields a new hash, so edits are
    -- tracked as new rows rather than silent in-place mutation.
    record_hash      CHAR(64)     NOT NULL
        CHECK (record_hash ~ '^[0-9a-f]{64}$'),

    full_name        TEXT,        -- OpenSanctions "name" (mandatory upstream)
    npi              TEXT,        -- first 10-digit id in "identifiers"; else NULL
    address          TEXT,        -- ";"-joined "addresses" (lossless)
    city             TEXT,        -- best-effort parse; NULL if unparseable
    state            TEXT,        -- best-effort parse; NULL if unparseable
    zip              TEXT,        -- best-effort parse; NULL if unparseable
    action           TEXT,        -- always NULL in simplified export
    effective_date   TEXT,        -- "sanctions" caption (date/range/list); raw text
    expiration_date  TEXT,        -- always NULL in simplified export

    source_url       TEXT         NOT NULL,
    source_sha256    CHAR(64)     NOT NULL
        CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    source_vintage   TEXT         NOT NULL,     -- ETag / Last-Modified / 'local'
    ingested_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_seen_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),

    PRIMARY KEY (record_hash)
);

COMMENT ON TABLE raw.nj_medicaid_exclusion IS
    'NJ Medicaid / OSC debarment exclusion list (state analog to HHS-OIG '
    'LEIE). One row per stable record_hash. UPSERT-by-record_hash with a '
    'last_seen_at tripwire for silent reinstatements/removals. NPI is '
    'non-exhaustive upstream (NJ warns); action/expiration_date are NULL in '
    'the simplified export.';
COMMENT ON COLUMN raw.nj_medicaid_exclusion.npi IS
    'NPI when present in the OpenSanctions identifiers field; NULL otherwise '
    '(NJ does not publish NPI for every excluded provider). Join key to CMS.';
COMMENT ON COLUMN raw.nj_medicaid_exclusion.last_seen_at IS
    'Timestamp of the most recent pull that still contained this row. A row '
    'whose last_seen_at falls behind MAX(last_seen_at) was removed/reinstated '
    'upstream (see derived.v_nj_medicaid_exclusion_active).';

-- Hot join path: state-excluded provider with a real NPI vs CMS billing.
CREATE INDEX raw_nj_medicaid_exclusion_npi_idx
    ON raw.nj_medicaid_exclusion (npi)
    WHERE npi IS NOT NULL AND npi <> '' AND npi <> '0000000000';


-- Currently-active exclusions: last_seen_at within 7 days of the freshest
-- pull (mirrors derived.v_leie_active). Rows that dropped out of recent
-- pulls (reinstated/removed) fall out of this view automatically.
CREATE OR REPLACE VIEW derived.v_nj_medicaid_exclusion_active AS
WITH latest AS (
    SELECT MAX(last_seen_at) AS most_recent FROM raw.nj_medicaid_exclusion
)
SELECT
    e.record_hash,
    e.full_name,
    e.npi,
    e.address,
    e.city,
    e.state,
    e.zip,
    e.action,
    e.effective_date,
    e.expiration_date,
    e.source_vintage,
    e.last_seen_at
FROM raw.nj_medicaid_exclusion e, latest
WHERE e.last_seen_at >= latest.most_recent - INTERVAL '7 days';

COMMENT ON VIEW derived.v_nj_medicaid_exclusion_active IS
    'Currently-active NJ Medicaid exclusions (last_seen_at within 7 days of '
    'the freshest pull). Mirrors derived.v_leie_active. For historical '
    'lookups query raw.nj_medicaid_exclusion directly.';


COMMIT;
