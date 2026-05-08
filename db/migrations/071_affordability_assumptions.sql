-- ============================================================================
-- Migration: 071_affordability_assumptions
--
-- PHASE 2 of VISION_2026.md (idea spec §5.1, §5.4) -- prerequisite of
-- the PITI + required-income functions in migration 072.
--
-- Establishes the platform's tier-A constants registry: every numeric
-- assumption that influences a metric, threshold, or default carries
-- (constant_id, value, source_url, source_citation, effective_year)
-- so that downstream calculations are traceable to a published rule
-- of thumb or a regulator's stated standard.
--
-- WHY THIS TABLE EXISTS (the verifiable-data rule made operational)
-- -----------------------------------------------------------------
-- The .cursor/rules/verifiable-data.mdc rule says: any numeric
-- constant influencing a metric must be EITHER (a) loaded from a
-- versioned reference table, OR (b) defined exactly once with an
-- in-line citation, OR (c) derived from raw data inside the same
-- query. PITI computation needs SEVERAL such constants:
--
--   * Default down-payment fraction: 20% per idea spec §5.1
--   * Default mortgage term: 30 years per idea spec §5.1
--   * Default homeowners insurance rate: ~0.35% of home value/year
--     (NAIC homeowners insurance summary national average)
--   * Affordability threshold: 30% of gross income (HUD definition
--     of "housing-cost burdened" per HUD-PD&R Worst-Case Housing
--     Needs report methodology)
--   * Conventional underwriting front-DTI cap: 28% (Fannie Mae)
--   * Conventional underwriting back-DTI cap: 36% (Fannie Mae)
--   * CFPB Qualified Mortgage back-DTI cap: 43% (CFPB Reg Z 1026.43)
--   * PMI threshold LTV: 80% (HOPA 1998 + Fannie Mae)
--
-- Hard-coding any of these in SQL or app code makes the platform
-- non-auditable. Centralizing them in ref.* with citations makes the
-- "where does this number come from?" question one query away.
--
-- WHY NOT ref.formula_version
-- ---------------------------
-- formula_version stamps the *algorithm* version. This table stamps
-- *parameter* values plugged into the algorithm. They are orthogonal:
-- you can have formula_version 1.0 with two different sets of seeded
-- assumptions (e.g. insurance rate 0.35% vs 0.40%) and reproducibly
-- compare them. Composite derived rows should carry BOTH stamps.
--
-- VERSIONING SHAPE
-- ----------------
-- (constant_id, effective_year) is the PK with effective_year=0
-- being a SENTINEL meaning "applies to all years until a year-
-- specific row supersedes it". We use 0 (not NULL) because PRIMARY
-- KEY columns must be NOT NULL in Postgres; the sentinel keeps the
-- ON CONFLICT and resolver logic simple. Year-specific rows
-- (e.g. effective_year=2014 for the CFPB QM rule that went live
-- 2014-01-10) take precedence over the perpetual (year=0) row.
-- ============================================================================

BEGIN;

CREATE TABLE ref.affordability_assumptions (
    constant_id      TEXT NOT NULL
                     CHECK (constant_id ~ '^[a-z][a-z0-9_]{2,80}$'),

    -- 0 = SENTINEL for "perpetual default" (applies to all years
    -- until a year-specific row supersedes it). Otherwise a real
    -- calendar year. We could not use NULL here because PRIMARY
    -- KEY columns are NOT NULL in Postgres; the sentinel keeps the
    -- ON CONFLICT inference straightforward.
    effective_year   SMALLINT NOT NULL DEFAULT 0
                     CHECK (effective_year = 0
                            OR effective_year BETWEEN 1900 AND 2099),

    value_numeric    NUMERIC(20,8) NOT NULL,

    -- Optional human-readable unit label (e.g. 'fraction', 'percent',
    -- 'years', 'dollars'). Self-documents the meaning of value_numeric.
    unit             TEXT NOT NULL
                     CHECK (unit IN ('fraction', 'percent', 'years',
                                     'dollars', 'months', 'count')),

    -- Provenance (every row required to have these).
    source_url       TEXT NOT NULL CHECK (source_url ~* '^https?://'),
    source_citation  TEXT NOT NULL CHECK (length(source_citation) > 5),

    -- Optional human-readable note about why this value was chosen
    -- and what the reasonable alternative range looks like.
    note             TEXT,

    PRIMARY KEY (constant_id, effective_year)
);

COMMENT ON TABLE ref.affordability_assumptions IS
    'Tier-A constants registry. Every numeric assumption used in '
    'PITI / required-income / affordability-gap calculations cites '
    'a published source. Year-specific rows override the perpetual '
    '(effective_year=0 sentinel) default, so a 2014+ CFPB QM '
    'threshold can ship without invalidating pre-2014 historical '
    'computations.';


CREATE INDEX affordability_assumptions_constant_idx
    ON ref.affordability_assumptions (constant_id);


-- ----------------------------------------------------------------------------
-- Resolver function
--
-- Given (constant_id, year), returns the most-specific applicable
-- value: prefer the year-specific row; fall back to the perpetual
-- (year=NULL) row; NULL if neither exists.
--
-- Returns a TABLE so callers can read both value AND its provenance
-- in one shot (every UI surface that displays a derived number can
-- show the citation underneath without a second lookup).
-- ----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION ref.f_assumption(
    p_constant_id  TEXT,
    p_year         SMALLINT
) RETURNS TABLE (
    value_numeric    NUMERIC,
    unit             TEXT,
    source_url       TEXT,
    source_citation  TEXT,
    effective_year   SMALLINT
)
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    -- "As of" lookup: pick the most recent row whose effective_year
    -- is <= p_year. The perpetual default (effective_year = 0) is
    -- the universal lower bound; year-specific rows take over from
    -- their effective_year onward.
    --
    -- A row with effective_year=2014 (e.g. CFPB QM rule going live
    -- 2014-01-10) returns NULL for queries at year 2010 (no
    -- applicable row), value=0.43 for queries at year 2014+.
    -- A row with effective_year=0 (e.g. the 0.20 down-payment
    -- default) returns value=0.20 for queries at any year.
    SELECT value_numeric, unit, source_url, source_citation, effective_year
    FROM ref.affordability_assumptions
    WHERE constant_id = p_constant_id
      AND effective_year <= p_year
    ORDER BY effective_year DESC
    LIMIT 1;
$$;

COMMENT ON FUNCTION ref.f_assumption(TEXT, SMALLINT) IS
    'Resolve the most-specific (constant_id, year) value with "as of" '
    'semantics: returns the row with the largest effective_year that '
    'is <= p_year. effective_year=0 acts as the perpetual default. '
    'A constant introduced mid-history (e.g. CFPB QM 2014) returns '
    'NULL for queries before its effective year. Returns empty (no '
    'rows) when no constant is seeded; caller must distinguish "no '
    'assumption seeded" (NULL) from "assumption is literally zero".';


-- ----------------------------------------------------------------------------
-- Convenience scalar wrapper -- returns just the value.
--
-- For callers (mostly other SQL functions in migration 072) that
-- only need the numeric value and not the provenance.
-- ----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION ref.f_assumption_value(
    p_constant_id  TEXT,
    p_year         SMALLINT
) RETURNS NUMERIC
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    SELECT value_numeric FROM ref.f_assumption(p_constant_id, p_year);
$$;

COMMENT ON FUNCTION ref.f_assumption_value(TEXT, SMALLINT) IS
    'Scalar convenience wrapper: returns just value_numeric. NULL '
    'if no matching constant. Use ref.f_assumption() when the '
    'caller needs to surface the citation alongside the number.';


COMMIT;
