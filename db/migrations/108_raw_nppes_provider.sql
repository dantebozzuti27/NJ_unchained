-- ============================================================================
-- Migration: 108_raw_nppes_provider
--
-- FRAUD-F7 substrate slice (Phase-3 identity spine): NPPES NPI Registry.
--
-- NPPES (National Plan and Provider Enumeration System) is the
-- authoritative registry mapping every 10-digit NPI to a provider's legal
-- name, business practice address, and primary taxonomy. We land it for
-- ONE structural reason: it is the IDENTITY SPINE. HHS-OIG's LEIE and the
-- NJ Medicaid exclusion list publish a name and an OFTEN-BLANK NPI; NPPES
-- is what later lets a name-only exclusion be resolved to a concrete NPI
-- and a New Jersey practice location. It is the join target for the
-- Phase-3 entity-resolution layer, not itself a signal source.
--
-- LOAD MODEL: FULL-REPLACE SNAPSHOT (TRUNCATE + COPY)
-- --------------------------------------------------
-- NPPES publishes no delta/tombstone stream -- the monthly bulk file IS
-- the complete truth. The ingester (ingestion/nppes.py) TRUNCATEs and
-- COPYs the projected ten columns (NULL '' so blank cells become SQL
-- NULL). Hence the grain is one row per NPI (PRIMARY KEY (npi)); we keep
-- exactly one snapshot (the latest), stamped with provenance. There is no
-- (data_year, npi) compound grain as in the CMS substrates because there
-- is no historical accumulation here -- only the current registry.
--
-- SIZE-BOUND TO NJ BY DEFAULT
-- ---------------------------
-- The national npidata file is ~10 GB / ~8M rows. The ingester filters to
-- Provider Business Practice Location State = 'NJ' by default (operators
-- opt into --national). This table therefore normally holds only NJ
-- practice-location providers; practice_state is retained verbatim so a
-- national load remains queryable.
--
-- COLUMN NOTES (verifiable-data invariants)
-- -----------------------------------------
-- * npi TEXT (not BIGINT): NPIs are identifiers, not quantities; TEXT
--   preserves shape and forbids arithmetic. CHECK enforces 10 digits.
-- * entity_type_code SMALLINT: 1 = individual, 2 = organization. A
--   deactivated NPI legitimately carries a BLANK entity type, which the
--   COPY's NULL '' turns into SQL NULL -- so the column is NULLABLE and
--   the CHECK admits NULL.
-- * deactivation_date TEXT (raw): a deactivated NPI is still a true
--   historical identity. The active/dead split is a derived-view concern
--   (see derived.v_nppes_provider_active), NOT a load-time filter, and the
--   date is kept as upstream text (no silent reformat).
-- * practice_zip5 TEXT: LEFT-5 of the postal code, NOT zero-padded -- a
--   value that lost a leading zero upstream is surfaced as-is, never
--   silently "repaired" into a different ZIP.
--
-- IDEMPOTENT VIA ON CONFLICT + CREATE OR REPLACE + the schema_migrations
-- sha256 ledger. Safe to re-run.
-- ============================================================================

BEGIN;


-- ----------------------------------------------------------------------------
-- 0. Formula version registration
-- ----------------------------------------------------------------------------
INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '2.8.8-nppes-substrate-v1',
    'Pillar 2 (civic integrity) FRAUD-F7 Phase-3 substrate slice. Lands '
    'raw.nppes_provider (NPPES NPI Registry monthly bulk file), one row per '
    'NPI, full-replace snapshot from download.cms.gov/nppes. Identity spine '
    'for resolving name-only LEIE / NJ-Medicaid exclusions to a concrete '
    'NPI + NJ practice location. NJ-filtered by default; blank cells load '
    'as SQL NULL.',
    '2026-06-09'::DATE,
    'Stacks on 2.8.7-fraud-services-per-beneficiary-outlier-v1. Substrate '
    'for the Phase-3 identity-resolution layer.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ----------------------------------------------------------------------------
-- 1. raw.nppes_provider
-- ----------------------------------------------------------------------------
CREATE TABLE raw.nppes_provider (
    npi                  TEXT         NOT NULL
        CHECK (npi ~ '^[0-9]{10}$'),

    entity_type_code     SMALLINT
        CHECK (entity_type_code IN (1, 2)),   -- 1 = individual, 2 = org; NULL ok

    provider_last_name   TEXT,        -- "Provider Last Name (Legal Name)"
    provider_first_name  TEXT,        -- "Provider First Name"
    provider_org_name    TEXT,        -- "Provider Organization Name (Legal Business Name)"

    practice_city        TEXT,        -- "...Practice Location Address City Name"
    practice_state       TEXT,        -- "...Practice Location Address State Name"
    practice_zip5        TEXT,        -- LEFT-5 of "...Postal Code" (not zero-padded)

    taxonomy_code_1      TEXT,        -- "Healthcare Provider Taxonomy Code_1"
    deactivation_date    TEXT,        -- "NPI Deactivation Date" (raw text)

    source_url           TEXT         NOT NULL,
    source_sha256        CHAR(64)     NOT NULL
        CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    source_vintage       TEXT         NOT NULL,
    ingested_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),

    PRIMARY KEY (npi)
);

COMMENT ON TABLE raw.nppes_provider IS
    'NPPES NPI Registry -- one row per NPI, full-replace monthly snapshot '
    'from download.cms.gov/nppes. Identity spine: resolves name-only LEIE / '
    'NJ-Medicaid exclusions to a concrete NPI + NJ practice location. '
    'NJ-filtered by default. Blank source cells are SQL NULL.';
COMMENT ON COLUMN raw.nppes_provider.npi IS
    'National Provider Identifier (10 digits). PRIMARY KEY; exact join key '
    'to LEIE / NJ-Medicaid exclusion / CMS Part B & Part D rosters.';
COMMENT ON COLUMN raw.nppes_provider.entity_type_code IS
    '1 = individual, 2 = organization. NULL for deactivated NPIs that '
    'publish a blank entity type.';
COMMENT ON COLUMN raw.nppes_provider.deactivation_date IS
    'Raw "NPI Deactivation Date" text. Kept verbatim; active/dead split is '
    'a derived-view concern (derived.v_nppes_provider_active), not a '
    'load-time filter.';
COMMENT ON COLUMN raw.nppes_provider.practice_zip5 IS
    'LEFT-5 of the practice-location postal code. NOT zero-padded -- a '
    'leading zero lost upstream is surfaced as-is, never silently repaired.';

-- Practice-state index: the dominant access pattern is NJ-scoped lookups
-- and (under --national) per-state slicing of the identity spine.
CREATE INDEX raw_nppes_provider_state_idx
    ON raw.nppes_provider (practice_state);

-- Last-name index for name-based LEIE / NJ-Medicaid resolution joins,
-- restricted to valid, non-placeholder NPIs.
CREATE INDEX raw_nppes_provider_last_name_idx
    ON raw.nppes_provider (provider_last_name)
    WHERE npi <> '0000000000';


-- ----------------------------------------------------------------------------
-- 2. Active view: valid-NPI, non-deactivated providers
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.v_nppes_provider_active AS
SELECT
    n.npi,
    n.entity_type_code,
    n.provider_last_name,
    n.provider_first_name,
    n.provider_org_name,
    n.practice_city,
    n.practice_state,
    n.practice_zip5,
    n.taxonomy_code_1
FROM raw.nppes_provider n
WHERE n.npi ~ '^[0-9]{10}$'
  AND n.npi <> '0000000000'
  AND (n.deactivation_date IS NULL OR n.deactivation_date = '');

COMMENT ON VIEW derived.v_nppes_provider_active IS
    'Currently-active NPPES providers: valid-NPI rows with no deactivation '
    'date. The identity-resolution layer joins against this view so '
    'deactivated historical identities never resolve a live exclusion.';


COMMIT;
