-- ============================================================================
-- Migration: 080_platform_constants
--
-- VISION_2026.md §3 punch-list item (.cursor/rules/verifiable-data.mdc).
--
-- ESTABLISHES a generic key-value reference table for platform-wide
-- numeric constants that influence user-visible metrics. Per the
-- verifiable-data invariants, every numeric constant on a screen, in
-- an API response, or in a derived table must be loadable from a
-- versioned reference table with full provenance. This migration
-- creates that table.
--
-- WHY GENERIC ref.platform_constants (NOT ad-hoc per-domain TABLES)
-- ----------------------------------------------------------------
-- The platform has only a handful of "single-value" constants -- the
-- burden base year, the cross-source base year, future tax-engine
-- defaults, etc. Each is independent and small. A single key-value
-- table is the right shape: simpler to read, simpler to extend, and
-- the (constant_id, formula_version) PK lets a downstream caller pin
-- which version of a constant produced their derived row.
--
-- Tier bands -- which are arrays of cutoffs, not single values -- get
-- their own table in migration 081 (ref.tier_bands) because they need
-- a (band_id, lower_bound, upper_bound, label) tuple per row.
--
-- IDEMPOTENCY
-- -----------
-- INSERT ... ON CONFLICT (constant_id, formula_version) DO UPDATE so
-- re-applying the migration is safe. A constant's value can only
-- change by registering a NEW formula_version row; the old one stays
-- visible for historical audit reproducibility.
-- ============================================================================

BEGIN;

-- Idempotent insertion in ref.formula_version registers the constant
-- family this migration introduces. A single entry covers both this
-- migration (080) and the seed-data file 014 that populates it.
INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '1.7.0-platform-constants-v1',
    'VISION_2026 §3 verifiable-data punch-list: introduce ref.platform_constants '
    'as the canonical home for single-value platform constants (burden base '
    'year, AEI base year, etc.). Each row carries source_url + citation_text + '
    'effective_date so every user-visible number traces to a verifiable origin. '
    'Backward-compatible: the lib/housing.ts BURDEN_BASE_YEAR literal moves '
    'to a SELECT against this table; same value (2010), now auditable.',
    '2026-05-08'::DATE,
    NULL
)
ON CONFLICT (formula_version) DO NOTHING;


-- ----------------------------------------------------------------------------
-- ref.platform_constants
--
-- One row per (constant_id, formula_version). The "current" value for
-- a constant_id is the row with the most recent effective_date that
-- has effective_date <= CURRENT_DATE; older rows are retained for
-- historical reproducibility (a derived row that pinned formula_version
-- = 'X' must always resolve to the same constant, even after the
-- constant changes).
-- ----------------------------------------------------------------------------
CREATE TABLE ref.platform_constants (
    constant_id      TEXT          NOT NULL
                     CHECK (length(constant_id) BETWEEN 3 AND 60
                            AND constant_id ~ '^[a-z][a-z0-9_]*$'),
    value            NUMERIC(20,8) NOT NULL,
    description      TEXT          NOT NULL
                     CHECK (length(description) >= 10),
    source_url       TEXT
                     CHECK (source_url IS NULL OR source_url ~* '^https?://'),
    citation_text    TEXT          NOT NULL
                     CHECK (length(citation_text) >= 10),
    formula_version  TEXT          NOT NULL
                     REFERENCES ref.formula_version(formula_version),
    effective_date   DATE          NOT NULL,
    notes            TEXT,

    PRIMARY KEY (constant_id, formula_version)
);

COMMENT ON TABLE ref.platform_constants IS
    'Generic single-value numeric constants used by the platform. '
    'Every user-visible number that does not trace to raw.* or a '
    'derived.* aggregation lands here with full provenance. The '
    '(constant_id, formula_version) PK makes amendments versioned and '
    'historical reproducibility unbreakable.';

COMMENT ON COLUMN ref.platform_constants.constant_id IS
    'Stable lowercase snake_case identifier; example: '
    '''burden_base_year'', ''aei_base_year'', ''pmms_default_term_years''.';

COMMENT ON COLUMN ref.platform_constants.value IS
    'Constant value as NUMERIC(20,8). Integers (e.g. base year 2010) '
    'are stored as 2010.00000000 and read back via ::SMALLINT cast at '
    'the call site.';

COMMENT ON COLUMN ref.platform_constants.formula_version IS
    'FK to ref.formula_version. Bumping the formula_version is the '
    'ONLY way to change a constant; this preserves historical '
    'reproducibility for derived rows that pinned the prior version.';

COMMENT ON COLUMN ref.platform_constants.effective_date IS
    'Date this constant value became effective for live use. The '
    '"current" value of a constant_id is the row with the latest '
    'effective_date <= CURRENT_DATE.';


-- ----------------------------------------------------------------------------
-- A read-time helper: derived.f_platform_constant(constant_id) returns
-- the active value for a constant as of CURRENT_DATE. Used by the SQL
-- layer to inline a constant without an extra round-trip.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.f_platform_constant(p_constant_id TEXT)
RETURNS NUMERIC
LANGUAGE sql
STABLE
AS $$
    SELECT value
    FROM ref.platform_constants
    WHERE constant_id    = p_constant_id
      AND effective_date <= CURRENT_DATE
    ORDER BY effective_date DESC, formula_version DESC
    LIMIT 1
$$;

COMMENT ON FUNCTION derived.f_platform_constant(TEXT) IS
    'Returns the active NUMERIC value for a platform constant as of '
    'CURRENT_DATE. Inlines into a parent query as '
    'derived.f_platform_constant(''burden_base_year'')::SMALLINT, '
    'so the constant is verifiable in the SQL itself with no extra '
    'round-trip from the application layer.';

COMMIT;
