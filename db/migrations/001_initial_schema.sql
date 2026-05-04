-- ============================================================================
-- Migration: 001_initial_schema
--
-- Establishes the four-schema layout that every subsequent migration depends
-- on, plus the minimum reference + governance tables needed to load any other
-- data source. NOTHING in this migration is NJ-specific in its schema -- the
-- spec calls for the platform to scale beyond NJ later (`idea` section 11).
--
-- SCHEMA SEPARATION RATIONALE
-- ---------------------------
--   ref        Curated reference data. Hand-maintained or seeded from
--              authoritative sources (Census FIPS, IRS thresholds). Stable
--              identifiers; everything else FKs into here.
--   raw        Loaded as-shipped from the source. We retain the source
--              row exactly as published to keep the audit trail straight;
--              normalization is the *next* layer's job. The contract:
--              `raw.*` rows are byte-equivalent (modulo encoding) to the
--              source line they came from. No quiet rewrites.
--   derived   Computed metrics. Every row carries:
--                - formula_version (FK to ref.formula_version)
--                - input_vintage_hash (sha256 of the raw inputs)
--              Together those two columns make every `derived.*` row
--              reproducible without having to rerun the upstream pull.
--   governance Operational metadata: migration tracking, dataset health,
--              run logs, lineage. These tables describe the platform
--              about itself, not the world.
--
-- The four schemas exist as separate namespaces (not just naming prefixes)
-- so that GRANT can be scoped per-layer. Read-only analytics consumers see
-- `derived` + `ref` only; they never touch `raw` or `governance`.
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- Schemas
-- ----------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS ref;
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS derived;
CREATE SCHEMA IF NOT EXISTS governance;

COMMENT ON SCHEMA ref IS
    'Curated reference data: county FIPS, formula versions, suppression '
    'thresholds. Stable identifiers; FK target for everything else.';
COMMENT ON SCHEMA raw IS
    'Source rows loaded byte-equivalent (modulo encoding). No normalization '
    'happens at this layer. Audit-trail substrate.';
COMMENT ON SCHEMA derived IS
    'Computed metrics. Every row stamps (formula_version, input_vintage_hash) '
    'so reruns are reproducible.';
COMMENT ON SCHEMA governance IS
    'Platform self-description: migration ledger, dataset health, run logs.';

-- ----------------------------------------------------------------------------
-- governance.schema_migrations
--
-- The migration ledger. Each row is the canonical record that a migration
-- file was applied to this database, with a sha256 of the file as it was at
-- the time of application. The runner (scripts/migrate.py) refuses to apply
-- a migration whose sha256 differs from a previously-applied one with the
-- same id, which catches accidental edits to already-shipped migrations.
-- ----------------------------------------------------------------------------
CREATE TABLE governance.schema_migrations (
    migration_id   TEXT          PRIMARY KEY,    -- e.g. '001_initial_schema'
    applied_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    sha256         CHAR(64)      NOT NULL,        -- sha256 of the .sql file
    applied_by     TEXT          NOT NULL DEFAULT current_user,
    duration_ms    INTEGER       NOT NULL CHECK (duration_ms >= 0)
);

COMMENT ON TABLE governance.schema_migrations IS
    'Migration ledger. The runner enforces that re-applying a migration with '
    'the same id but a different sha256 fails loudly, preventing silent '
    'schema drift from edits to already-shipped migrations.';

-- ----------------------------------------------------------------------------
-- ref.state, ref.county
--
-- The geographic spine. county_id is the canonical identifier used as a
-- foreign key throughout the platform; the format is `<state_code>-<NAME>`
-- (e.g. 'NJ-MIDDLESEX') for human-readability, but `county_fips` (5-digit
-- string, leading zero preserved) is the authoritative join key for any
-- external dataset that uses FIPS.
--
-- WHY county_id IS A STRING NOT AN INTEGER
-- ----------------------------------------
-- Surrogate INTs lose information when you debug a query in psql. With
-- 'NJ-MIDDLESEX' you can immediately read what's being computed; with
-- `county_id = 47` you cannot. The cost is six extra bytes per row. The
-- platform's working set is small (~3K US counties), so total cost is
-- negligible. We pay the bytes for the auditability.
-- ----------------------------------------------------------------------------
CREATE TABLE ref.state (
    state_code  CHAR(2)   PRIMARY KEY,
    state_fips  CHAR(2)   NOT NULL UNIQUE,
    name        TEXT      NOT NULL UNIQUE
);

CREATE TABLE ref.county (
    county_id           TEXT       PRIMARY KEY
        CHECK (county_id ~ '^[A-Z]{2}-[A-Z][A-Z _-]{1,40}$'),
    state_code          CHAR(2)    NOT NULL REFERENCES ref.state(state_code),
    county_fips         CHAR(5)    NOT NULL UNIQUE
        CHECK (county_fips ~ '^[0-9]{5}$'),
    name                TEXT       NOT NULL,
    -- Census-published common name and ALAND/AWATER are useful enough to keep
    -- here so downstream code never has to re-join against a Census table for
    -- presentation. Sourced from Census 2020 TIGER/Line.
    name_legal          TEXT,                          -- e.g. 'Middlesex County'
    aland_sqmeters      BIGINT,                        -- land area
    awater_sqmeters     BIGINT,                        -- water area
    centroid_lat        NUMERIC(8,5),
    centroid_lon        NUMERIC(8,5),
    UNIQUE (state_code, name)
);

CREATE INDEX idx_county_state ON ref.county (state_code);

COMMENT ON COLUMN ref.county.county_id IS
    'Human-readable canonical identifier of the form STATE-NAME (e.g. '
    'NJ-MIDDLESEX). Used as FK everywhere; preferred over surrogate ints '
    'for debuggability in psql.';
COMMENT ON COLUMN ref.county.county_fips IS
    'Authoritative 5-digit FIPS (state2 + county3) for joining external '
    'datasets. Leading zeros preserved (CHAR(5)).';

-- ----------------------------------------------------------------------------
-- ref.formula_version
--
-- Every derived metric stamps the formula version that produced it. When
-- a methodology change ships, we bump the formula_version and rerun the
-- pipeline; the OLD rows remain in derived.* tables, and consumers can
-- compare versions side-by-side to see how the methodology change moved
-- the metric. This is the platform's anti-drift discipline.
--
-- Format: <semver>-<short-tag>, e.g. '1.0.0-baseline', '1.1.0-pums-cleanup'.
-- ----------------------------------------------------------------------------
CREATE TABLE ref.formula_version (
    formula_version   TEXT          PRIMARY KEY
        CHECK (formula_version ~ '^[0-9]+\.[0-9]+\.[0-9]+(-[a-z0-9._-]+)?$'),
    description       TEXT          NOT NULL,
    effective_date    DATE          NOT NULL,
    deprecated_at     TIMESTAMPTZ,
    notes             TEXT
);

COMMENT ON TABLE ref.formula_version IS
    'Methodology version registry. Bumping this preserves history of older '
    'computations rather than overwriting them, so we can quantify how a '
    'methodology change moved the headline numbers.';

INSERT INTO ref.formula_version (formula_version, description, effective_date, notes)
VALUES (
    '1.0.0-baseline',
    'Initial baseline: county-level annual aggregation, 30%-of-income '
    'qualifying-income rule, HUD bus_ratio worksite allocation, no PUMS.',
    '2026-04-28',
    'See `idea` section 5 for the metric definitions this anchors.'
);

-- ----------------------------------------------------------------------------
-- governance.dataset_health
--
-- Per-(dataset, observed_at) signals that get raised by ingesters and
-- detectors. Examples a downstream consumer needs to be loud about:
--   - source vintage shifted unexpectedly
--   - row count fell more than X% vs prior vintage
--   - a wage cross-check (LCA median >= PUMS WAGP) inverted
--   - a measured value crossed a `data_quality` threshold
--
-- This is intentionally append-only. Severity drives whether the platform
-- refuses to publish a derived metric on top of the affected raw rows.
-- ----------------------------------------------------------------------------
CREATE TABLE governance.dataset_health (
    health_id        BIGSERIAL    PRIMARY KEY,
    dataset_id       TEXT         NOT NULL,           -- e.g. 'raw.lca_disclosure'
    observed_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    signal_name      TEXT         NOT NULL,           -- e.g. 'row_count_drop'
    severity         TEXT         NOT NULL CHECK (severity IN
                                  ('info', 'warn', 'error', 'fatal')),
    metric_value     NUMERIC,
    metric_unit      TEXT,
    -- Free-form structured payload. Keep small (< 4KB); large blobs go to
    -- object storage with a URI in `details->>'artifact_uri'`.
    details          JSONB        NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX idx_dataset_health_dataset_observed
    ON governance.dataset_health (dataset_id, observed_at DESC);
CREATE INDEX idx_dataset_health_severity
    ON governance.dataset_health (severity, observed_at DESC)
    WHERE severity IN ('error', 'fatal');

COMMENT ON TABLE governance.dataset_health IS
    'Append-only signals raised by ingesters and detectors. error/fatal '
    'rows block downstream derived computations until acknowledged.';

-- ----------------------------------------------------------------------------
-- ref.suppression_threshold
--
-- Per-(table, percentile) minimum cell sizes. The CHECK constraints on
-- derived tables hard-code these values; the table here exists so analytics
-- consumers can introspect "why did this cell go NULL?" without grepping SQL.
-- ----------------------------------------------------------------------------
CREATE TABLE ref.suppression_threshold (
    table_name       TEXT          NOT NULL,
    rule_name        TEXT          NOT NULL,
    min_n            INTEGER       NOT NULL CHECK (min_n >= 1),
    rationale        TEXT          NOT NULL,
    PRIMARY KEY (table_name, rule_name)
);

COMMENT ON TABLE ref.suppression_threshold IS
    'Per-(table, rule) suppression minimums. The CHECK constraints on the '
    'actual tables enforce these at write time; this table is for '
    'introspection ("why is this cell NULL?").';

-- ----------------------------------------------------------------------------
-- Seed: NJ
-- ----------------------------------------------------------------------------
INSERT INTO ref.state (state_code, state_fips, name)
VALUES ('NJ', '34', 'New Jersey')
ON CONFLICT (state_code) DO NOTHING;

COMMIT;
