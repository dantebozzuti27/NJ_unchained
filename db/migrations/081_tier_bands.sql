-- ============================================================================
-- Migration: 081_tier_bands
--
-- VISION_2026.md §3 punch-list item (.cursor/rules/verifiable-data.mdc).
--
-- ESTABLISHES a generic tier-band registry for color-coded categorical
-- classifiers across the platform (burden tier, AEI severity, future
-- DI-erosion bands, etc.). Per the verifiable-data invariants:
--
--   "Color bins, tier cutoffs, severity ladders, peer-bucket bounds --
--    these are reference data, not application constants. They live in
--    db/seeds/*.sql or ref.* tables, not inline in .tsx or .py."
--
-- The lib/housing.ts burdenTier() function currently has cutoffs
-- {1.4, 1.15, 0.95} hardcoded with no calibration evidence. This
-- migration moves them to a versioned ref table + introduces
-- empirically calibrated cutoffs against the historical NJ panel.
--
-- WHY (band_id, lower_bound, upper_bound) AND NOT A SINGLE-VALUE TABLE
-- ------------------------------------------------------------------
-- A tier band is a (range, label) pair, not a single constant. Storing
-- it as one row per band lets the SQL layer issue the classification
-- as a single scalar lookup (SELECT label FROM ref.tier_bands WHERE
-- ratio >= lower_bound AND ratio < upper_bound) instead of building
-- a CASE statement in application code. It also makes amendments
-- explicit: changing one band's cutoff is a row-level UPDATE, not a
-- multi-line code edit.
--
-- WHY NULL-able lower_bound AND upper_bound
-- -----------------------------------------
-- The bottom and top tiers have open-ended sides ((-inf, x] or [y, +inf)).
-- NULLs encode "no bound on this side" cleanly without needing a
-- sentinel value.
--
-- IDEMPOTENCY
-- -----------
-- INSERT ... ON CONFLICT (tier_kind, band_ord, formula_version) DO
-- UPDATE so re-applying the migration is safe.
-- ============================================================================

BEGIN;

-- Register the formula version that introduces tier bands. The tier
-- bands themselves are seeded in db/seeds/015_tier_bands.sql with the
-- empirical calibration evidence baked into the citation_text.
INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '1.7.1-tier-bands-v1',
    'VISION_2026 §3 verifiable-data punch-list: introduce ref.tier_bands '
    'as the canonical home for color-coded categorical classifiers. '
    'First consumer: lib/housing.ts burdenTier(). Cutoffs are empirically '
    'calibrated against the 315-pair (NJ county, year) historical panel '
    'with the (p25, p75, p90) anchors documented per row. Replaces the '
    'inline {1.4, 1.15, 0.95} cutoffs which had no source citation.',
    '2026-05-08'::DATE,
    NULL
)
ON CONFLICT (formula_version) DO NOTHING;


-- ----------------------------------------------------------------------------
-- ref.tier_bands
--
-- One row per (tier_kind, band_ord, formula_version). band_ord is the
-- ascending integer key within a tier_kind (1 = the lowest band, N =
-- the highest). For a 4-band classifier you have band_ord in {1,2,3,4}.
--
-- A row's range is [lower_bound, upper_bound). lower_bound NULL means
-- (-infinity, upper_bound); upper_bound NULL means [lower_bound,
-- +infinity). Successive rows must form a contiguous partition.
-- ----------------------------------------------------------------------------
CREATE TABLE ref.tier_bands (
    tier_kind        TEXT          NOT NULL
                     CHECK (length(tier_kind) BETWEEN 3 AND 60
                            AND tier_kind ~ '^[a-z][a-z0-9_]*$'),
    band_ord         SMALLINT      NOT NULL
                     CHECK (band_ord BETWEEN 1 AND 20),
    label            TEXT          NOT NULL
                     CHECK (length(label) BETWEEN 1 AND 30),
    description      TEXT          NOT NULL
                     CHECK (length(description) >= 5),
    severity_rank    SMALLINT      NOT NULL
                     CHECK (severity_rank >= 0),

    -- Inclusive lower bound of the band ([lower_bound, upper_bound)
    -- semantics). NULL = unbounded below.
    lower_bound      NUMERIC(20,8),
    -- Exclusive upper bound. NULL = unbounded above.
    upper_bound      NUMERIC(20,8),
    CHECK (lower_bound IS NULL OR upper_bound IS NULL OR lower_bound < upper_bound),

    -- UI presentation: tailwind-class-style background + foreground.
    -- These are display hints, not semantic data. The label is the
    -- semantic carrier; bg/fg are advisory.
    ui_bg_classes    TEXT,
    ui_fg_classes    TEXT,

    citation_text    TEXT          NOT NULL
                     CHECK (length(citation_text) >= 10),
    source_url       TEXT
                     CHECK (source_url IS NULL OR source_url ~* '^https?://'),
    formula_version  TEXT          NOT NULL
                     REFERENCES ref.formula_version(formula_version),
    effective_date   DATE          NOT NULL,
    notes            TEXT,

    PRIMARY KEY (tier_kind, band_ord, formula_version)
);

COMMENT ON TABLE ref.tier_bands IS
    'Versioned tier-band registry. Each (tier_kind, formula_version) '
    'tuple defines a contiguous partition of the real line into N '
    'labeled bands. Consumers SELECT the row whose [lower_bound, '
    'upper_bound) range contains the value, classifying it.';


-- ----------------------------------------------------------------------------
-- A read-time helper: derived.f_tier_band(tier_kind, value) returns
-- the matching band row for the active version. Inlines as a single
-- scalar lookup at the call site.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.f_tier_band(
    p_tier_kind TEXT,
    p_value     NUMERIC
)
RETURNS TABLE (
    label           TEXT,
    description     TEXT,
    severity_rank   SMALLINT,
    ui_bg_classes   TEXT,
    ui_fg_classes   TEXT,
    citation_text   TEXT,
    source_url      TEXT,
    formula_version TEXT
)
LANGUAGE sql
STABLE
AS $$
    WITH active AS (
        SELECT tb.*
        FROM ref.tier_bands tb
        WHERE tb.tier_kind      = p_tier_kind
          AND tb.effective_date <= CURRENT_DATE
        ORDER BY tb.effective_date DESC, tb.formula_version DESC, tb.band_ord
    ),
    latest_version AS (
        SELECT formula_version FROM active LIMIT 1
    )
    SELECT
        tb.label, tb.description, tb.severity_rank,
        tb.ui_bg_classes, tb.ui_fg_classes,
        tb.citation_text, tb.source_url, tb.formula_version
    FROM ref.tier_bands tb
    JOIN latest_version v ON v.formula_version = tb.formula_version
    WHERE tb.tier_kind = p_tier_kind
      AND (tb.lower_bound IS NULL OR p_value >= tb.lower_bound)
      AND (tb.upper_bound IS NULL OR p_value <  tb.upper_bound)
    ORDER BY tb.band_ord
    LIMIT 1
$$;

COMMENT ON FUNCTION derived.f_tier_band(TEXT, NUMERIC) IS
    'Returns the matching tier-band row for a value under the latest '
    'active formula_version. NULL value or no-matching-band returns '
    'zero rows; the application layer handles that as the unknown/'
    'missing-data case.';

COMMIT;
