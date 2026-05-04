-- ============================================================================
-- Migration: 053_hhs_oig_leie
--
-- TIER 4 v3 / FRAUD-F5 (substrate slice): HHS-OIG List of Excluded
-- Individuals/Entities (LEIE).
--
-- WHAT IT IS
-- ----------
-- The LEIE is HHS-OIG's authoritative database of individuals and
-- organizations excluded by federal action from billing or
-- participating in any federal health-care program (Medicare,
-- Medicaid, etc.). Hiring or doing business with someone on the LEIE
-- exposes the hiring entity to civil monetary penalties; that is why
-- HHS publishes the database monthly and why every state Medicaid
-- agency cross-checks against it.
--
-- For our fraud platform the LEIE is the FIRST GROUND-TRUTH LABEL
-- SOURCE we can land. Inclusion in LEIE is not "fraud" by itself --
-- the exclusion authorities span everything from a felony fraud
-- conviction (1128a1) to default on a federal student loan (1128b14)
-- -- but every LEIE entry is the result of a formal federal action
-- against a named entity. As a *negative class signal*, that is far
-- stronger than anything we can derive from FEC bulk data alone.
--
-- WHY THE TABLE LOOKS LIKE THIS
-- -----------------------------
-- 1. The LEIE downloadable file does NOT contain SSN or EIN (Privacy
--    Act prohibits redistribution). The natural key is therefore some
--    composite of (LASTNAME, FIRSTNAME, MIDNAME, DOB, EXCLDATE) for
--    individuals or (BUSNAME, ADDRESS, EXCLDATE) for entities, which
--    is fragile (NULLs disqualify rows from PK enforcement, names
--    drift across pulls). We instead compute a surrogate
--    ``record_hash`` -- SHA-256 of the canonical row content -- in
--    Python at load time and use it as the primary key. UPSERT
--    semantics on ``(record_hash)`` are then trivial.
--
-- 2. ``vintage_month`` + ``last_seen_at`` are the historical-
--    archive trick. HHS replaces the entire downloadable file each
--    month; reinstated entities simply drop out. We never DELETE on
--    re-load -- we UPSERT and bump ``last_seen_at = now()``. A row
--    whose ``last_seen_at`` is older than the latest pull is therefore
--    a *reinstatement signal* (or a row that was edited and rehashed,
--    which is a profile-correction event we surface separately).
--
-- 3. Date columns (DOB, EXCLDATE, REINDATE, WAIVERDATE) are stored as
--    TEXT/CHAR(8) verbatim. The LEIE writes "00000000" for absent
--    dates, has historical mixed-format DOBs, and we follow the
--    raw-mirrors-source contract. Cooked date columns live in the
--    canonical view.
--
-- 4. ``excltype`` is NOT NULL because every active LEIE row has an
--    exclusion authority code; an empty string would be a bug in the
--    parser, not a real LEIE row.
--
-- 5. Liberal CHECK constraints. Real LEIE rows include weird states
--    ("PR", "GU"), foreign addresses, and edge punctuation. The raw
--    layer accepts everything the source publishes; cleansing happens
--    downstream.
--
-- WHAT'S NOT IN THIS MIGRATION (deliberately)
-- -------------------------------------------
-- * Entity resolution to FEC donors / committees / treasurers -- needs
--   a name-canonicalisation pass (separate migration once the data
--   has been loaded once and we can see real text shapes).
-- * Monthly-supplement loaders (NNNNEXCL / NNNNREIN). The full-file
--   re-pull plus our last_seen_at logic captures the same information
--   without two more parser shapes; supplements stay future work.
-- * SSN/EIN crosswalk -- HHS does not publish those; verifying matches
--   against an unrelated SSN is the operator's responsibility per the
--   LEIE Quick Tips guidance.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- raw.hhs_oig_leie (one row per LEIE entry per stable record_hash)
-- ----------------------------------------------------------------------------
CREATE TABLE raw.hhs_oig_leie (
    -- Surrogate primary key: SHA-256 of a canonical pipe-joined string of
    -- (lastname, firstname, midname, busname, dob, excltype, excldate,
    --  general, specialty, upin, npi, address, city, state, zip).
    -- Computed in Python at load time so we don't need pgcrypto. Stable
    -- across re-pulls of the same record; changes IFF HHS edits the row.
    record_hash      CHAR(64)     NOT NULL CHECK (record_hash ~ '^[0-9a-f]{64}$'),

    -- LEIE record-layout columns (per HHS-OIG "Current LEIE Database
    -- Record Layout" PDF). Field-order matches the published CSV.
    lastname         TEXT,
    firstname        TEXT,
    midname          TEXT,
    busname          TEXT,                  -- mutually exclusive with lastname (in practice)
    general          TEXT,                  -- general provider type (e.g., "PHYSICIAN")
    specialty        TEXT,                  -- sub-specialty (e.g., "INTERNAL MEDICINE")
    upin             TEXT,                  -- legacy Unique Physician ID
    npi              TEXT,                  -- 10-digit National Provider Identifier
                                            -- (raw -- some rows are blank, some are
                                            --  "0000000000" placeholders)
    dob              TEXT,                  -- raw birthdate as published (mixed formats)
    address          TEXT,
    city             TEXT,
    state            TEXT,
    zip              TEXT,

    -- Exclusion authority + dates. excltype is the SSA section that
    -- authorised the exclusion (e.g., "1128a1" = mandatory exclusion
    -- for program-related conviction; "1128b4" = permissive exclusion
    -- for license revocation). The full code list is published by
    -- HHS-OIG separately and is exposed through the canonical view.
    excltype         TEXT         NOT NULL CHECK (excltype <> ''),
    excldate         CHAR(8)      NOT NULL CHECK (excldate ~ '^[0-9]{8}$'),
    reindate         CHAR(8)      CHECK (reindate IS NULL OR reindate ~ '^[0-9]{8}$'),
    waiverdate       CHAR(8)      CHECK (waiverdate IS NULL OR waiverdate ~ '^[0-9]{8}$'),
    wvrstate         TEXT,                  -- state(s) waiving the exclusion (free text)

    -- Provenance / vintage. The platform contract: every raw row knows
    -- which file it came from and when. ``vintage_month`` is the YYYY-MM
    -- of the published file (operator-supplied, not derived from URL,
    -- because the same UPDATED.csv URL is reused each month).
    vintage_month    CHAR(7)      NOT NULL CHECK (vintage_month ~ '^[0-9]{4}-[0-9]{2}$'),
    source_url       TEXT         NOT NULL,
    source_sha256    CHAR(64)     NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    ingested_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),

    -- Bumped on every UPSERT. A row whose last_seen_at falls behind the
    -- max across the table is either (a) a reinstatement (not present
    -- in the latest full-file pull), or (b) a profile correction
    -- (present in the latest pull but with edited content -> different
    -- record_hash). Either way, last_seen_at is the analyst's tripwire.
    last_seen_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),

    PRIMARY KEY (record_hash),

    -- Sanity: a real LEIE row has either an individual name OR a
    -- business name (or, very rarely, both for sole-proprietorship
    -- entries). All three blank is always a parser bug.
    CHECK (
        COALESCE(NULLIF(lastname, ''), NULLIF(busname, '')) IS NOT NULL
    )
);

COMMENT ON TABLE raw.hhs_oig_leie IS
    'HHS-OIG List of Excluded Individuals/Entities (LEIE). One row per '
    'unique excluded entity record. Re-pulled monthly from '
    'oig.hhs.gov/exclusions/downloadables/UPDATED.csv (full database, '
    'not supplements). last_seen_at indicates when the entry was last '
    'observed in a pull -- entries that fall behind the most recent '
    'pull are reinstatements (or profile corrections that re-hashed).';

COMMENT ON COLUMN raw.hhs_oig_leie.record_hash IS
    'SHA-256 of canonical row content; stable PK across re-pulls.';
COMMENT ON COLUMN raw.hhs_oig_leie.excltype IS
    'SSA exclusion authority code (e.g., 1128a1 = mandatory for '
    'program-related conviction; 1128b4 = license revocation).';
COMMENT ON COLUMN raw.hhs_oig_leie.last_seen_at IS
    'Bumped on every UPSERT. Stale = reinstated or profile-edited.';


CREATE INDEX raw_hhs_oig_leie_lastname_idx
    ON raw.hhs_oig_leie (lastname)
    WHERE lastname IS NOT NULL;

CREATE INDEX raw_hhs_oig_leie_busname_idx
    ON raw.hhs_oig_leie (busname)
    WHERE busname IS NOT NULL;

-- NPI lookups are a hot path for any future entity match against
-- USAspending or Medicare claim files (both carry NPI). Partial index
-- skips the bulk of rows that have no NPI (or "0000000000" placeholder).
CREATE INDEX raw_hhs_oig_leie_npi_idx
    ON raw.hhs_oig_leie (npi)
    WHERE npi IS NOT NULL AND npi <> '' AND npi <> '0000000000';

-- last_seen_at index supports the active-vs-historical filter in the
-- canonical view below; without it every active-only query scans the
-- full table.
CREATE INDEX raw_hhs_oig_leie_last_seen_idx
    ON raw.hhs_oig_leie (last_seen_at);


-- ----------------------------------------------------------------------------
-- Canonical views: active rows only, plus individuals / businesses splits
-- ----------------------------------------------------------------------------
--
-- "Active" = last_seen_at within 7 days of the freshest pull. Picking a
-- 7-day window (rather than == max) absorbs the case where the operator
-- runs the loader twice in the same week against successive monthly
-- files: the older pull's rows briefly look stale, then the newer pull's
-- rows replace them. Operationally, "this entity is on LEIE today" is a
-- weekly-resolution question; we don't need second-level precision.
--
-- These views are not vintaged -- they always reflect the latest pull's
-- contents. Historical reconstruction (e.g., "was X on LEIE in Q3 2024?")
-- requires querying raw with a vintage_month filter, which is fine
-- because that's exactly the question the raw table is designed for.
-- ----------------------------------------------------------------------------

CREATE OR REPLACE VIEW derived.v_leie_active AS
WITH latest AS (
    SELECT MAX(last_seen_at) AS most_recent FROM raw.hhs_oig_leie
)
SELECT
    record_hash,
    lastname, firstname, midname, busname,
    general, specialty,
    upin, npi,
    dob,
    address, city, state, zip,
    excltype,
    -- Cooked date variants: NULL when source was '00000000'.
    -- to_date is NOT used here because it would raise on '00000000' --
    -- which is the published value for "no date", not invalid input.
    -- The CASE expression is the right separation of "absent" vs
    -- "present-but-bad".
    CASE WHEN excldate   = '00000000' THEN NULL
         ELSE to_date(excldate,   'YYYYMMDD') END AS excldate_d,
    CASE WHEN reindate   = '00000000' THEN NULL
         WHEN reindate IS NULL        THEN NULL
         ELSE to_date(reindate,   'YYYYMMDD') END AS reindate_d,
    CASE WHEN waiverdate = '00000000' THEN NULL
         WHEN waiverdate IS NULL      THEN NULL
         ELSE to_date(waiverdate, 'YYYYMMDD') END AS waiverdate_d,
    wvrstate,
    vintage_month,
    last_seen_at
FROM raw.hhs_oig_leie, latest
WHERE last_seen_at >= latest.most_recent - INTERVAL '7 days';

COMMENT ON VIEW derived.v_leie_active IS
    'Currently-active LEIE entries (last_seen_at within 7 days of '
    'freshest pull). Joins the cooked date variants. For reinstated '
    'or historical lookups, query raw.hhs_oig_leie directly with a '
    'vintage_month or last_seen_at filter.';


-- Individual exclusions: rows with a person name. Convenience for
-- analysts running employee-screening queries.
CREATE OR REPLACE VIEW derived.v_leie_individuals_active AS
SELECT
    record_hash,
    lastname, firstname, midname,
    general, specialty,
    upin, npi,
    dob,
    address, city, state, zip,
    excltype,
    excldate_d,
    reindate_d,
    waiverdate_d,
    wvrstate,
    vintage_month
FROM derived.v_leie_active
WHERE lastname IS NOT NULL AND lastname <> '';

COMMENT ON VIEW derived.v_leie_individuals_active IS
    'Active LEIE individual-person exclusions (lastname populated).';


-- Business / entity exclusions: rows with a business name. The fraud
-- engine uses these for entity-name match against FEC committee names,
-- USAspending recipients, etc.
CREATE OR REPLACE VIEW derived.v_leie_businesses_active AS
SELECT
    record_hash,
    busname,
    general, specialty,
    upin, npi,
    address, city, state, zip,
    excltype,
    excldate_d,
    reindate_d,
    waiverdate_d,
    wvrstate,
    vintage_month
FROM derived.v_leie_active
WHERE busname IS NOT NULL AND busname <> '';

COMMENT ON VIEW derived.v_leie_businesses_active IS
    'Active LEIE business / entity exclusions (busname populated). '
    'Used by the fraud-engine entity-match layer (separate migration).';
