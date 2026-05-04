-- ============================================================================
-- Migration: 063_sam_gov_exclusion
--
-- FRAUD-F2 SAM.gov substrate (schema only). Establishes the raw and
-- derived surfaces required by a future cross-source signal that
-- joins USAspending recipient_uei against SAM.gov-excluded UEIs --
-- the deterministic version of the entity_funded_and_excluded
-- signal (058) which currently relies on name canonicalization
-- and is therefore false-positive prone.
--
-- WHAT IS SAM.gov Exclusions
-- -------------------------
-- The General Services Administration's System for Award Management
-- (SAM) maintains the federal "do not contract" list. Anyone (firm
-- or individual) excluded under Federal Acquisition Regulation
-- 9.405 -- whether for procurement violations, statutory grounds,
-- or DOJ-debarment reciprocity -- appears here. Coverage is BROADER
-- than HHS-OIG LEIE (which is healthcare-only): SAM aggregates
-- exclusions from every excluding agency, including DOJ, GSA,
-- NIH/NSF (research-misconduct), Department of Education, and
-- Treasury OFAC reciprocity. A single excluded individual frequently
-- appears in BOTH SAM and LEIE; a firm typically appears only in
-- SAM (LEIE has no firm coverage).
--
-- DATA SOURCE
-- -----------
-- SAM.gov publishes a daily Exclusions Public Extract V2 ZIP at
--   https://sam.gov/api/prod/fileextractservices/v1/api/download/Exclusions+Public+V2/
-- containing a comma-delimited CSV with header row. The file
-- updates daily at ~00:00 ET. The auth path on this endpoint is
-- inconsistent across clients; the loader is intentionally NOT
-- shipped in this migration so the substrate can land without
-- depending on a working fetch path. Operator hand-loads the
-- daily CSV via the upcoming nj-ingest-sam-exclusions tool once
-- the auth path is verified.
--
-- WHY UEI MATTERS
-- ---------------
-- DUNS numbers were retired by the federal procurement community
-- on April 4, 2022. Since then UEI (Unique Entity Identifier, a
-- 12-character alphanumeric SAM-issued ID) is the canonical
-- federal entity ID. USAspending.gov has used UEI on every award
-- since FY2022, and SAM.gov Exclusions has UEI on every active
-- exclusion since the same date. A direct UEI <-> UEI join is
-- therefore deterministic: no name canonicalization needed, and
-- false-positive risk drops to ~zero (the only false positive is
-- a SAM-side data-entry bug, which is rare and would surface as
-- an asset check).
--
-- WHAT'S IN THIS MIGRATION
-- ------------------------
-- 1. raw.sam_gov_exclusion -- one row per stable record_hash, with
--    full SAM Exclusions V2 column coverage. Daily full-replace
--    semantics planned (mirrors LEIE migration 053): UPSERT on
--    record_hash, bump last_seen_at, drop nothing -- so a
--    reinstatement (record disappears from the daily extract)
--    is detected by last_seen_at falling behind MAX(last_seen_at)
--    rather than by a DELETE.
-- 2. derived.v_sam_exclusion_active -- filters to rows where the
--    exclusion is currently in effect (no termination_date set,
--    or termination_date is in the future, AND record_status is
--    not 'Inactive'). The cross-source signal will join here.
-- 3. derived.v_sam_exclusion_by_uei -- convenient projection for
--    UEI-keyed cross-source matching (USAspending.recipient_uei
--    against this view's uei).
-- 4. derived.v_sam_exclusion_individual_canonical -- reuses the
--    LEIE-style LAST|FIRST canonical key (derived.f_canonical_lastfirst_split)
--    so individual-side matching can also work via name when UEI
--    is absent (e.g., for individuals who have no UEI because they
--    were excluded as natural persons rather than firm
--    representatives). This view is the parallel of
--    derived.v_leie_individual_canonical.
--
-- WHAT'S NOT IN THIS MIGRATION (deliberately)
-- -------------------------------------------
-- * Loader / ingester. Shipping next once the SAM.gov fetch path's
--   auth requirements are verified against a real CSV.
-- * Cross-source signals (derived.refresh_signal_entity_excluded_via_sam_uei,
--   etc.). Gated on the loader because they have no input data yet.
-- * Dagster asset for raw.sam_gov_exclusion. No compute exists,
--   and registering an empty asset would mislead the lineage view.
--   The asset registers in the next migration alongside the loader.
-- * fraud_signal_config row. No new signal_id yet; the orphan-block
--   asset check (migration 061) would fail if we registered a
--   signal_id with no L1 writer.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- raw.sam_gov_exclusion (one row per stable record_hash)
-- ----------------------------------------------------------------------------
CREATE TABLE raw.sam_gov_exclusion (
    -- Surrogate primary key: SHA-256 of a canonical pipe-joined string
    -- of the loader-chosen identifying fields (classification, name,
    -- first, middle, last, suffix, uei, duns, sam_number, exclusion
    -- type, active_date). Computed in Python at load time.
    record_hash            CHAR(64)    NOT NULL PRIMARY KEY
        CHECK (record_hash ~ '^[0-9a-f]{64}$'),

    -- Top-level classification. The four-value enumeration is the
    -- SAM Exclusions data dictionary's official set; defensive
    -- whitelist with explicit CHECK so a parser regression that
    -- coerces a fifth value blocks at the boundary.
    classification         TEXT        NOT NULL
        CHECK (classification IN (
            'Individual',
            'Special Entity Designation',
            'Firm',
            'Vessel'
        )),

    -- Name fields. SAM publishes both the full Name string AND the
    -- parsed (Prefix, First, Middle, Last, Suffix) tuple for
    -- Individual classification. Both are preserved verbatim --
    -- matching downstream sometimes needs the full Name (e.g., when
    -- SAM didn't parse it cleanly), sometimes the components.
    name                   TEXT,
    prefix                 TEXT,
    first                  TEXT,
    middle                 TEXT,
    last                   TEXT,
    suffix                 TEXT,
    title                  TEXT,

    -- Federal entity identifiers. UEI is the canonical post-2022 ID;
    -- DUNS is legacy (still present on records active before the
    -- April 2022 cutover). CAGE is the DoD's 5-character supplier
    -- code -- many SAM exclusions for DoD-related procurement
    -- carry CAGE. NPI is the National Provider Identifier
    -- (rare on SAM, common on LEIE).
    uei                    TEXT
        CHECK (uei IS NULL OR uei ~ '^[A-Z0-9]{12}$'),
    duns                   TEXT,
    cage                   TEXT,
    npi                    TEXT,

    -- Address fields. SAM publishes up to four address lines plus
    -- city/state/country/zip. Free text; we do not enforce shape.
    address1               TEXT,
    address2               TEXT,
    address3               TEXT,
    address4               TEXT,
    city                   TEXT,
    state_province         TEXT,
    country                TEXT,
    zip                    TEXT,

    -- Exclusion metadata.
    --   exclusion_program     {'Reciprocal','Procurement','Nonprocurement'}
    --                         (free text; some records use longer phrases)
    --   excluding_agency_name SAM-published agency string
    --   exclusion_type_desc   detailed type ('Procurement','Nonprocurement',
    --                         'Statutory','Reciprocal', etc.)
    --   active_date           when the exclusion took effect
    --   termination_date      NULL = indefinite or 'permanent'; a date in
    --                         the future = scheduled end; a date in the
    --                         past = exclusion has expired (caller must
    --                         filter via derived.v_sam_exclusion_active)
    exclusion_program      TEXT,
    excluding_agency_name  TEXT,
    exclusion_type_desc    TEXT,
    active_date            DATE,
    termination_date       DATE,

    -- Record-level status / cross-references.
    record_status          TEXT,
    cross_reference        TEXT,
    sam_number             TEXT,
    additional_comments    TEXT,
    open_data_flag         TEXT,
    creation_date          DATE,

    -- Provenance / vintage. vintage_day is the calendar date of the
    -- daily extract the row was pulled from (operator-supplied since
    -- the SAM file URL embeds an ordinal day, not a friendly date).
    vintage_day            DATE        NOT NULL,
    source_url             TEXT        NOT NULL CHECK (source_url <> ''),
    source_sha256          CHAR(64)    NOT NULL
        CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    ingested_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Bumped on every UPSERT. A row whose last_seen_at falls behind
    -- MAX(last_seen_at) was not in the latest daily pull -- meaning
    -- either a reinstatement (the record was removed) or an edit
    -- (re-hashed under a new record_hash). Same semantic as LEIE.
    last_seen_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Sanity: a real exclusion has at least an entity-level identifier.
    -- We require ANY of (name, last, uei, duns, sam_number) to be
    -- non-empty; all-blank rows are parser bugs. This is intentionally
    -- LAX (compared to LEIE's name-or-busname) because SAM Vessel
    -- entries can lack human/firm names and key only by SAM number.
    CHECK (
        COALESCE(
            NULLIF(name, ''),
            NULLIF(last, ''),
            NULLIF(uei, ''),
            NULLIF(duns, ''),
            NULLIF(sam_number, '')
        ) IS NOT NULL
    )
);

COMMENT ON TABLE raw.sam_gov_exclusion IS
    'SAM.gov Exclusions Public Extract V2. One row per stable '
    'record_hash. Daily full-replace semantics: UPSERT on record_hash, '
    'last_seen_at bumped to now(), no DELETE -- so reinstatements '
    'show as last_seen_at falling behind MAX(last_seen_at). Loader '
    'and Dagster asset are deferred to the next migration once the '
    'SAM fetch path is verified.';

COMMENT ON COLUMN raw.sam_gov_exclusion.uei IS
    'Unique Entity Identifier: 12-character alphanumeric, SAM-issued, '
    'replaced DUNS on 2022-04-04. Canonical federal entity ID; the '
    'cross-source UEI<->UEI match against USAspending.recipient_uei '
    'is deterministic when both sides set this column.';

COMMENT ON COLUMN raw.sam_gov_exclusion.classification IS
    'SAM Exclusions data-dictionary enum: Individual / Special Entity '
    'Designation / Firm / Vessel. CHECK-constrained so a parser '
    'regression that emits a fifth value blocks at the boundary.';

COMMENT ON COLUMN raw.sam_gov_exclusion.last_seen_at IS
    'Bumped on every UPSERT. Stale = the row was not in the latest '
    'daily pull (reinstatement or edit -> new record_hash).';


-- ----------------------------------------------------------------------------
-- Indexes
-- ----------------------------------------------------------------------------
-- The two indexes serve the two intended join paths:
--   * idx_sam_uei: cross-source matching against USAspending.recipient_uei
--     (the deterministic-match cross-source signal).
--   * idx_sam_individual_last: name-based matching for Individual-
--     classification rows (parallel to LEIE individual matching).
-- Indexing classification alone is unnecessary: there are <5 distinct
-- values; the planner prefers a sequential scan + filter on a column
-- that low-cardinality.
-- ----------------------------------------------------------------------------
CREATE INDEX idx_sam_exclusion_uei
    ON raw.sam_gov_exclusion (uei)
    WHERE uei IS NOT NULL;

CREATE INDEX idx_sam_exclusion_individual_last
    ON raw.sam_gov_exclusion (last)
    WHERE classification = 'Individual' AND last IS NOT NULL;

CREATE INDEX idx_sam_exclusion_termination_date
    ON raw.sam_gov_exclusion (termination_date);


-- ============================================================================
-- DERIVED VIEWS
-- ============================================================================


-- ----------------------------------------------------------------------------
-- derived.v_sam_exclusion_active
-- ----------------------------------------------------------------------------
-- An exclusion is "active" iff (a) termination_date is unset or in
-- the future, AND (b) record_status is not 'Inactive'.
--
-- The two conditions are independent: SAM sometimes marks a record
-- 'Inactive' even when termination_date is in the future (e.g., when
-- an excluded firm wins reinstatement before the scheduled term);
-- conversely, SAM sometimes leaves record_status NULL while
-- back-dating termination_date to a recent past date when an
-- agency settles a debarment early. The intersection of "no
-- termination yet" AND "not Inactive" is the only correct filter.
-- ----------------------------------------------------------------------------
CREATE VIEW derived.v_sam_exclusion_active AS
SELECT
    record_hash,
    classification,
    name, prefix, first, middle, last, suffix, title,
    uei, duns, cage, npi,
    address1, address2, address3, address4,
    city, state_province, country, zip,
    exclusion_program, excluding_agency_name, exclusion_type_desc,
    active_date, termination_date,
    record_status, cross_reference, sam_number,
    additional_comments, open_data_flag, creation_date,
    vintage_day, source_url, source_sha256,
    ingested_at, last_seen_at
FROM raw.sam_gov_exclusion
WHERE (termination_date IS NULL OR termination_date > CURRENT_DATE)
  AND (record_status IS NULL OR LOWER(record_status) <> 'inactive');

COMMENT ON VIEW derived.v_sam_exclusion_active IS
    'Currently-active SAM.gov exclusions. Filters out terminated '
    '(termination_date <= today) and explicitly Inactive rows. '
    'Cross-source signals join against this view, not the raw table.';


-- ----------------------------------------------------------------------------
-- derived.v_sam_exclusion_by_uei
-- ----------------------------------------------------------------------------
-- Projection of the active set keyed for UEI-based cross-source
-- matching. Drops rows without UEI -- those are matched by name
-- via the individual-canonical view below.
-- ----------------------------------------------------------------------------
CREATE VIEW derived.v_sam_exclusion_by_uei AS
SELECT
    record_hash       AS sam_record_hash,
    classification,
    name              AS sam_name,
    uei               AS sam_uei,
    cage              AS sam_cage,
    duns              AS sam_duns,
    excluding_agency_name,
    exclusion_program,
    exclusion_type_desc,
    active_date       AS sam_active_date,
    termination_date  AS sam_termination_date
FROM derived.v_sam_exclusion_active
WHERE uei IS NOT NULL;

COMMENT ON VIEW derived.v_sam_exclusion_by_uei IS
    'UEI-keyed projection of active SAM exclusions. The intended '
    'cross-source join is USAspending.recipient_uei = sam_uei: '
    'deterministic, no canonicalization required.';


-- ----------------------------------------------------------------------------
-- derived.v_sam_exclusion_individual_canonical
-- ----------------------------------------------------------------------------
-- Mirrors derived.v_leie_individual_canonical so a future cross-
-- source individual-name signal can use the IDENTICAL canonical key
-- on both sides. The LAST|FIRST split function (defined in
-- migration 054) is shared between LEIE and SAM individual matching.
-- ----------------------------------------------------------------------------
CREATE VIEW derived.v_sam_exclusion_individual_canonical AS
SELECT
    record_hash                                                     AS sam_record_hash,
    last                                                            AS sam_last,
    first                                                           AS sam_first,
    middle                                                          AS sam_middle,
    state_province                                                  AS sam_state,
    active_date                                                     AS sam_active_date,
    termination_date                                                AS sam_termination_date,
    excluding_agency_name,
    derived.f_canonical_lastfirst_split(last, first)                AS canonical_key
FROM derived.v_sam_exclusion_active
WHERE classification = 'Individual'
  AND derived.f_canonical_lastfirst_split(last, first) IS NOT NULL;

COMMENT ON VIEW derived.v_sam_exclusion_individual_canonical IS
    'Active SAM individual exclusions with the LAST|FIRST canonical '
    'key (same f_canonical_lastfirst_split as LEIE individual '
    'canonical). Drops Firm/Vessel/Special Entity rows and rows '
    'whose canonicalization yields NULL (empty after normalization).';
