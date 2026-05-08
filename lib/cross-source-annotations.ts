/**
 * Cross-source housing-index divergence annotation data layer
 * (VISION_2026 §8.1 Phase 7b).
 *
 * Surfaces the platform's documented methodology causes for FHFA-vs-Zillow
 * divergence so the UI can render an honest "this is documented because of
 * X" callout next to a (county, year) divergence value, instead of leaving
 * the user to wonder whether a 14% gap is a bug or a known artifact.
 *
 * Reads from:
 *   * derived.v_cross_source_divergence_annotated   -- per-(county, year) rows
 *   * derived.f_cross_source_divergence_annotation  -- scalar one-row lookup
 *
 * Both are defined by migration 084_cross_source_divergence_annotations,
 * formula version 1.9.0-cross-source-annotations-v1. The annotation
 * registry itself lives in ref.cross_source_divergence_known_causes
 * (seed 017).
 *
 * Status taxonomy
 * ---------------
 *   unannotated                   -- no documented cause for this (county, year);
 *                                    if the divergence is large, the asset
 *                                    check fires
 *   annotated_within_envelope     -- documented cause; divergence is within
 *                                    the documented envelope; alarm
 *                                    suppressed
 *   annotated_envelope_exceeded   -- documented cause exists but the
 *                                    divergence has BROKEN the documented
 *                                    envelope; the alarm re-fires with a
 *                                    distinguishable reason
 *
 * The "envelope_exceeded" branch is the substrate-honesty contract:
 * annotations DOCUMENT what is normal, they do not silence outliers.
 *
 * Per-page consumption pattern: call `getCrossSourceAnnotation(fips, year)`
 * inline alongside the divergence number, and render
 * `<CrossSourceAnnotationCallout />` (or inline the metadata) next to it.
 */

import { getSql } from "./db";

export type CrossSourceAnnotationStatus =
  | "unannotated"
  | "annotated_within_envelope"
  | "annotated_envelope_exceeded";

/** Cause categories; mirrors the CHECK constraint on
 *  ref.cross_source_divergence_known_causes.cause_category. */
export type CrossSourceAnnotationCauseCategory =
  | "thin_coverage"
  | "methodology_lag"
  | "composition_change"
  | "parser_bootstrap"
  | "other";

export type CrossSourceAnnotation = {
  /** The classification. NULL only when the (county, year) pair has no
   *  cross-source data at all (no row in
   *  derived.f_housing_index_cross_source for that pair). */
  annotation_status: CrossSourceAnnotationStatus;
  /** NULL when annotation_status === "unannotated". */
  annotation_cause_category: CrossSourceAnnotationCauseCategory | null;
  /** Human-readable description of the cause. NULL when unannotated. */
  annotation_description: string | null;
  /** Documented envelope: while the annotation is in effect for this
   *  (county, year), |divergence_pct_of_fhfa| <= this value is expected.
   *  Stored as a fraction (0.15 = 15%). NULL when unannotated. */
  annotation_expected_max_abs_pct: number | null;
  /** Source citation (URL + reference). Required by table CHECK
   *  (length >= 11). NULL when unannotated. */
  annotation_source_citation: string | null;
  /** Signed: positive = ZHVI grew faster than FHFA. NULL only when one
   *  source is missing for the pair. */
  divergence_pct_of_fhfa: number | null;
};

/**
 * Fetch the annotation for one (county_fips, year) pair, or NULL if
 * cross-source data is absent for the pair (e.g., FHFA loaded but ZHVI
 * not for that year). Best-effort caller pattern: the page should treat
 * a NULL return identically to "no annotation" and degrade gracefully.
 *
 * @param countyFips 5-character state+county FIPS (NJ counties: 34???)
 * @param year SMALLINT year
 */
export async function getCrossSourceAnnotation(
  countyFips: string,
  year: number,
): Promise<CrossSourceAnnotation | null> {
  const sql = getSql();
  const rows = (await sql`
    SELECT
      annotation_status,
      annotation_cause_category,
      annotation_description,
      annotation_expected_max_abs_pct::FLOAT8 AS annotation_expected_max_abs_pct,
      annotation_source_citation,
      divergence_pct_of_fhfa::FLOAT8          AS divergence_pct_of_fhfa
    FROM derived.f_cross_source_divergence_annotation(
      ${countyFips}::CHAR(5), ${year}::SMALLINT
    )
  `) as Record<string, unknown>[];

  if (rows.length === 0) return null;
  const r = rows[0];

  const status = r.annotation_status as
    | CrossSourceAnnotationStatus
    | null;
  if (status === null) return null;

  return {
    annotation_status: status,
    annotation_cause_category:
      (r.annotation_cause_category as
        | CrossSourceAnnotationCauseCategory
        | null) ?? null,
    annotation_description:
      typeof r.annotation_description === "string"
        ? r.annotation_description
        : null,
    annotation_expected_max_abs_pct:
      r.annotation_expected_max_abs_pct == null
        ? null
        : Number(r.annotation_expected_max_abs_pct),
    annotation_source_citation:
      typeof r.annotation_source_citation === "string"
        ? r.annotation_source_citation
        : null,
    divergence_pct_of_fhfa:
      r.divergence_pct_of_fhfa == null
        ? null
        : Number(r.divergence_pct_of_fhfa),
  };
}

// ---------------------------------------------------------------------------
// Display helpers
// ---------------------------------------------------------------------------

/** Tailwind color classes per annotation_status. */
export function annotationStatusClasses(
  status: CrossSourceAnnotationStatus,
): { bg: string; fg: string; ring: string; dot: string } {
  switch (status) {
    case "annotated_within_envelope":
      return {
        bg: "bg-emerald-50 dark:bg-emerald-950/40",
        fg: "text-emerald-800 dark:text-emerald-200",
        ring: "ring-emerald-200 dark:ring-emerald-800",
        dot: "bg-emerald-500",
      };
    case "annotated_envelope_exceeded":
      return {
        bg: "bg-red-50 dark:bg-red-950/40",
        fg: "text-red-800 dark:text-red-200",
        ring: "ring-red-200 dark:ring-red-800",
        dot: "bg-red-500",
      };
    case "unannotated":
      return {
        bg: "bg-slate-50 dark:bg-slate-900/60",
        fg: "text-slate-700 dark:text-slate-300",
        ring: "ring-slate-200 dark:ring-slate-700",
        dot: "bg-slate-400",
      };
  }
}

/** Short label for the badge pill. */
export function annotationStatusLabel(
  status: CrossSourceAnnotationStatus,
): string {
  switch (status) {
    case "annotated_within_envelope":
      return "Documented divergence";
    case "annotated_envelope_exceeded":
      return "Documented cause exceeded envelope";
    case "unannotated":
      return "No documented cause";
  }
}

/** Friendly label for the cause category. */
export function annotationCauseLabel(
  cause: CrossSourceAnnotationCauseCategory,
): string {
  switch (cause) {
    case "parser_bootstrap":
      return "Early-data coverage bootstrap";
    case "methodology_lag":
      return "Repeat-sales lag during price shock";
    case "composition_change":
      return "Housing-stock composition change";
    case "thin_coverage":
      return "Thin transaction coverage";
    case "other":
      return "Other documented cause";
  }
}

/** Format a fractional envelope as a percentage string. */
export function formatEnvelope(fraction: number | null): string {
  if (fraction == null) return "—";
  return `${(fraction * 100).toFixed(1)}%`;
}
