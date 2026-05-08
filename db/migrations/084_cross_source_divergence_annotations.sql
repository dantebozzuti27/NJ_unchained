-- ============================================================================
-- Migration: 084_cross_source_divergence_annotations
--
-- VISION_2026 §8.1 (Phase 7b): annotation substrate for cross-source
-- housing-index divergence. The Phase 7 asset check
-- `housing_index_cross_source_divergence_plausible` correctly fires on
-- |FHFA HPI - Zillow ZHVI| > 20% (or warns on 12-20%), but treats every
-- divergence the same -- a Hudson urban-condo composition gap is alarmed
-- identically to a Sussex parser bug, even though one is a documented
-- methodology artifact and the other is a real data quality failure.
--
-- This migration adds an annotation layer so DOCUMENTED divergences
-- ("known causes") can be subtracted from the alarm count, leaving the
-- check to fire only on UNDOCUMENTED divergences. The annotations are
-- substrate-honest: each row carries a citation, a documented_at date,
-- and an expected_max_abs_pct envelope. If a divergence STAYS within
-- the documented envelope, no alarm; if it BREAKS the documented
-- envelope, the alarm still fires (with a "known cause exceeded" reason).
--
-- WHAT THIS MIGRATION SHIPS
-- -------------------------
--   1. ref.cross_source_divergence_known_causes -- one row per
--      (county_fips, year_range, cause_category). The annotations live
--      in `ref` (auditable reference data); each row carries a source
--      citation, a documented_at date, and an expected_max_abs_pct
--      envelope.
--
--   2. derived.v_cross_source_divergence_annotated -- joins
--      derived.f_housing_index_cross_source(2010) against the annotation
--      table and labels each (county, year) pair as
--      'unannotated' / 'annotated_within_envelope' /
--      'annotated_envelope_exceeded'. This is the surface the asset check
--      reads from.
--
--   3. derived.f_cross_source_divergence_annotation(county_fips, year)
--      -- scalar lookup for one-row callers (e.g., a future
--      /housing/methodology page).
--
-- DESIGN DECISIONS
-- ----------------
-- * Annotations are RANGE-BASED (year_start, year_end) because the same
--   methodology cause applies to multiple consecutive years (e.g., the
--   2000-2002 ZHVI bootstrap window). year_end IS NULL means
--   "open-ended" (e.g., a structural urban-density artifact that does
--   not have a known end date).
-- * cause_category is a TEXT-with-CHECK enum: 'thin_coverage',
--   'methodology_lag', 'composition_change', 'parser_bootstrap',
--   'other'. Adding a category requires a migration (intentional --
--   each category has a documented operational meaning).
-- * The PK is (county_fips, year_start, cause_category). A county/year
--   range can have multiple causes (e.g., Cape May 2001 has BOTH
--   thin_coverage AND parser_bootstrap), and we want all of them
--   visible -- so we don't collapse on cause.
-- * expected_max_abs_pct is a NUMERIC(5,4) (4 decimal places, max
--   9.9999 = 999.99%). Stored as a fraction (0.15 = 15%) to match the
--   downstream divergence_pct_of_fhfa convention.
-- * The view's classifier is THREE-WAY ('unannotated' /
--   'annotated_within_envelope' / 'annotated_envelope_exceeded') rather
--   than binary so the asset check can fire on annotation breaches with
--   an explicit reason ("documented cause exceeded its envelope") that
--   is operationally distinct from "unknown divergence."
-- ============================================================================

BEGIN;


-- ----------------------------------------------------------------------------
-- Formula version registration. Stacks on 1.8.1-freshness-backfill-v1.
-- ----------------------------------------------------------------------------
INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '1.9.0-cross-source-annotations-v1',
    'Cross-source housing-index divergence annotation substrate '
    '(VISION_2026 §8.1 Phase 7b). Adds ref.cross_source_divergence_known_causes '
    'as the annotation registry, derived.v_cross_source_divergence_annotated '
    'as the annotated view (per-(county,year) classifier: unannotated / '
    'annotated_within_envelope / annotated_envelope_exceeded), and a scalar '
    'function for one-row lookup. The annotation envelope is BOUND-TO-CAUSE: '
    'a known cause has a documented expected_max_abs_pct; staying within '
    'the envelope suppresses the alarm, breaking the envelope re-fires it '
    'with a distinguishable reason. This preserves the substrate-honesty '
    'tenet: we do not silence alarms, we DOCUMENT what is normal and let '
    'unknowns fire.',
    '2026-05-09'::DATE,
    'Stacks on 1.8.1-freshness-backfill-v1.'
)
ON CONFLICT (formula_version) DO NOTHING;


-- ----------------------------------------------------------------------------
-- ref.cross_source_divergence_known_causes
--
-- One row per (county_fips, year_range, cause_category). A county-year
-- pair can have multiple causes; the view below picks the FIRST
-- annotation that contains the year (ordered by year_start DESC so
-- narrower / more-recent annotations win).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ref.cross_source_divergence_known_causes (
    county_fips           CHAR(5)        NOT NULL,
    year_start            SMALLINT       NOT NULL,
    year_end              SMALLINT,                      -- NULL = open-ended
    cause_category        TEXT           NOT NULL,
    description           TEXT           NOT NULL,
    expected_max_abs_pct  NUMERIC(5, 4)  NOT NULL,       -- 0.15 = 15%
    source_citation       TEXT           NOT NULL,
    documented_by         TEXT           NOT NULL DEFAULT 'platform',
    documented_at         DATE           NOT NULL DEFAULT CURRENT_DATE,
    created_at            TIMESTAMPTZ    NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ    NOT NULL DEFAULT now(),
    PRIMARY KEY (county_fips, year_start, cause_category),
    -- year_end >= year_start when both present
    CONSTRAINT cross_source_known_causes_year_range_chk
        CHECK (year_end IS NULL OR year_end >= year_start),
    -- envelope must be a sane non-negative fraction; 100% would cover
    -- the entire universe so we ceiling at 1.0 to catch typos.
    CONSTRAINT cross_source_known_causes_envelope_chk
        CHECK (expected_max_abs_pct >= 0.0
           AND expected_max_abs_pct <= 1.0),
    CONSTRAINT cross_source_known_causes_category_chk
        CHECK (cause_category IN (
            'thin_coverage',         -- one or both sources have thin transaction counts
            'methodology_lag',       -- repeat-sales lags hedonic+listing during shocks
            'composition_change',    -- housing stock composition changes (urban condo, etc.)
            'parser_bootstrap',      -- early-data parser/coverage instability
            'other'
        )),
    -- Source citation must NOT be empty -- annotations require provenance.
    CONSTRAINT cross_source_known_causes_citation_chk
        CHECK (length(source_citation) > 10)
);

COMMENT ON TABLE ref.cross_source_divergence_known_causes IS
    'Annotation registry for documented housing-index cross-source '
    'divergence causes. One row per (county_fips, year_range, '
    'cause_category). Each row carries a citation, documented_at date, '
    'and an expected_max_abs_pct envelope. Consumed by '
    'derived.v_cross_source_divergence_annotated. '
    'Formula 1.9.0-cross-source-annotations-v1; spec VISION_2026 §8.1.';

COMMENT ON COLUMN ref.cross_source_divergence_known_causes.year_end IS
    'NULL = open-ended annotation (the cause has no documented end '
    'date; e.g., a structural urban-density artifact).';

COMMENT ON COLUMN ref.cross_source_divergence_known_causes.expected_max_abs_pct IS
    'Documented envelope: while the annotation is in effect for this '
    'county-year, |divergence_pct_of_fhfa| <= this value is expected. '
    'Stored as a fraction (0.15 = 15%) to match divergence_pct_of_fhfa.';


-- updated_at trigger: keep updated_at in sync with row mutations.
CREATE OR REPLACE FUNCTION ref._cross_source_known_causes_set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS cross_source_known_causes_updated_at
    ON ref.cross_source_divergence_known_causes;

CREATE TRIGGER cross_source_known_causes_updated_at
BEFORE UPDATE ON ref.cross_source_divergence_known_causes
FOR EACH ROW
EXECUTE FUNCTION ref._cross_source_known_causes_set_updated_at();


-- ----------------------------------------------------------------------------
-- derived.v_cross_source_divergence_annotated
--
-- Joins the cross-source divergence function output against the
-- annotation registry. For each (county, year) pair with both indices
-- loaded:
--
--   * If no annotation contains the year, status = 'unannotated'
--     (this is what the asset check should fire on).
--
--   * If an annotation contains the year AND the divergence is within
--     the annotation's envelope, status = 'annotated_within_envelope'
--     (alarm SUPPRESSED; this is documented expected behavior).
--
--   * If an annotation contains the year BUT the divergence EXCEEDS
--     the annotation's envelope, status = 'annotated_envelope_exceeded'
--     (alarm STILL FIRES with a distinguishable reason: "the
--     documented cause is no longer adequate; either the cause has
--     intensified or a new cause has appeared").
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.v_cross_source_divergence_annotated AS
WITH x AS (
    SELECT * FROM derived.f_housing_index_cross_source(2010::SMALLINT)
),
matched AS (
    -- For each (county, year), pick the annotation with the NARROWEST
    -- year range that contains the year. Narrowest range = most-specific
    -- annotation. Ties broken by MAX(year_start) so the more-recent
    -- annotation wins (a 2025 Hudson annotation should beat an
    -- open-ended 2020+ annotation that also contains 2025).
    SELECT DISTINCT ON (x.county_fips, x.year)
        x.county_fips,
        x.year,
        x.fhfa_hpi_indexed,
        x.zillow_zhvi_indexed,
        x.divergence_indexed_points,
        x.divergence_pct_of_fhfa,
        kc.cause_category                      AS annotation_cause_category,
        kc.description                         AS annotation_description,
        kc.expected_max_abs_pct                AS annotation_expected_max_abs_pct,
        kc.source_citation                     AS annotation_source_citation,
        kc.year_start                          AS annotation_year_start,
        kc.year_end                            AS annotation_year_end
    FROM x
    LEFT JOIN ref.cross_source_divergence_known_causes kc
        ON kc.county_fips = x.county_fips
       AND x.year >= kc.year_start
       AND (kc.year_end IS NULL OR x.year <= kc.year_end)
    ORDER BY x.county_fips, x.year,
             -- Narrowness: smaller (year_end - year_start) is more specific.
             COALESCE(kc.year_end, 9999) - kc.year_start ASC NULLS LAST,
             kc.year_start DESC NULLS LAST
)
SELECT
    county_fips,
    year,
    fhfa_hpi_indexed,
    zillow_zhvi_indexed,
    divergence_indexed_points,
    divergence_pct_of_fhfa,
    annotation_cause_category,
    annotation_description,
    annotation_expected_max_abs_pct,
    annotation_source_citation,
    annotation_year_start,
    annotation_year_end,
    CASE
        WHEN divergence_pct_of_fhfa IS NULL              THEN NULL
        WHEN annotation_cause_category IS NULL           THEN 'unannotated'
        WHEN ABS(divergence_pct_of_fhfa)
              <= annotation_expected_max_abs_pct          THEN 'annotated_within_envelope'
        ELSE                                                   'annotated_envelope_exceeded'
    END                                                  AS annotation_status
FROM matched;

COMMENT ON VIEW derived.v_cross_source_divergence_annotated IS
    'Per-(county, year) cross-source divergence joined with the '
    'annotation registry. annotation_status: unannotated (no documented '
    'cause; alarms fire on threshold breach), annotated_within_envelope '
    '(documented cause; alarm suppressed if within envelope), '
    'annotated_envelope_exceeded (documented cause but envelope broken; '
    'alarm fires with a distinct reason). '
    'Formula 1.9.0-cross-source-annotations-v1.';


-- ----------------------------------------------------------------------------
-- derived.f_cross_source_divergence_annotation(county_fips, year)
--
-- Scalar wrapper for one-row callers. Returns the annotation_status
-- and annotation metadata for a single (county, year) pair, or
-- 'unannotated'/NULLs if no annotation matches.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.f_cross_source_divergence_annotation(
    p_county_fips CHAR(5), p_year SMALLINT
)
RETURNS TABLE(
    annotation_status               TEXT,
    annotation_cause_category       TEXT,
    annotation_description          TEXT,
    annotation_expected_max_abs_pct NUMERIC,
    annotation_source_citation      TEXT,
    divergence_pct_of_fhfa          NUMERIC
)
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    SELECT
        v.annotation_status,
        v.annotation_cause_category,
        v.annotation_description,
        v.annotation_expected_max_abs_pct,
        v.annotation_source_citation,
        v.divergence_pct_of_fhfa
    FROM derived.v_cross_source_divergence_annotated v
    WHERE v.county_fips = p_county_fips
      AND v.year        = p_year
$$;

COMMENT ON FUNCTION derived.f_cross_source_divergence_annotation(CHAR, SMALLINT) IS
    'Scalar wrapper: annotation_status + metadata for a single '
    '(county_fips, year) pair. Returns no rows if the pair has no '
    'cross-source data; returns one row with annotation_status = '
    '''unannotated'' if it has data but no documented cause.';

COMMIT;
