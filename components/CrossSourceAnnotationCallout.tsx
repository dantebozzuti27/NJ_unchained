/**
 * Cross-source divergence annotation callout
 * (VISION_2026 §8.1 Phase 7b — UI surfacing).
 *
 * Renders the annotation metadata for a single (county, year) pair as
 * an inline callout next to the FHFA-vs-ZHVI divergence number. Three
 * visual states:
 *
 *   - annotated_within_envelope    -> emerald "Documented" pill + cause +
 *                                      envelope + citation
 *   - annotated_envelope_exceeded  -> red alarm pill + cause + envelope
 *                                      breach detail + citation (the
 *                                      substrate-honesty branch: a
 *                                      documented cause that BROKE its
 *                                      envelope must re-fire)
 *   - unannotated                  -> slate neutral pill, no detail
 *                                      (callers may opt to omit entirely)
 *
 * Server component; fetches no data itself (consumes a
 * `CrossSourceAnnotation` from `lib/cross-source-annotations.ts`).
 */

import type { CrossSourceAnnotation } from "@/lib/cross-source-annotations";
import {
  annotationCauseLabel,
  annotationStatusClasses,
  annotationStatusLabel,
  formatEnvelope,
} from "@/lib/cross-source-annotations";

export function CrossSourceAnnotationCallout({
  annotation,
  year,
  hideWhenUnannotated = true,
}: {
  annotation: CrossSourceAnnotation;
  year: number;
  hideWhenUnannotated?: boolean;
}) {
  if (annotation.annotation_status === "unannotated" && hideWhenUnannotated) {
    return null;
  }

  const palette = annotationStatusClasses(annotation.annotation_status);
  const label = annotationStatusLabel(annotation.annotation_status);
  const observed =
    annotation.divergence_pct_of_fhfa == null
      ? null
      : (annotation.divergence_pct_of_fhfa * 100).toFixed(2);

  return (
    <div
      className={[
        "mt-3 rounded-md p-3 text-xs ring-1",
        palette.bg,
        palette.fg,
        palette.ring,
      ].join(" ")}
      role={
        annotation.annotation_status === "annotated_envelope_exceeded"
          ? "alert"
          : "note"
      }
    >
      <div className="flex items-center gap-2 font-semibold">
        <span
          aria-hidden
          className={[
            "h-1.5 w-1.5 rounded-full",
            palette.dot,
          ].join(" ")}
        />
        <span>{label}</span>
        {annotation.annotation_cause_category && (
          <>
            <span className="opacity-50">·</span>
            <span className="font-normal opacity-90">
              {annotationCauseLabel(annotation.annotation_cause_category)}
            </span>
          </>
        )}
      </div>

      {annotation.annotation_description && (
        <p className="mt-2 leading-relaxed opacity-90">
          {annotation.annotation_description}
        </p>
      )}

      <dl className="mt-3 grid grid-cols-1 sm:grid-cols-3 gap-2">
        <div>
          <dt className="uppercase tracking-wider opacity-70">
            Observed ({year})
          </dt>
          <dd className="mt-0.5 font-mono">
            {observed == null
              ? "—"
              : `${
                  annotation.divergence_pct_of_fhfa! >= 0 ? "+" : ""
                }${observed}%`}
          </dd>
        </div>
        <div>
          <dt className="uppercase tracking-wider opacity-70">
            Documented envelope
          </dt>
          <dd className="mt-0.5 font-mono">
            ±{formatEnvelope(annotation.annotation_expected_max_abs_pct)}
          </dd>
        </div>
        <div>
          <dt className="uppercase tracking-wider opacity-70">Effect</dt>
          <dd className="mt-0.5">
            {annotation.annotation_status === "annotated_within_envelope"
              ? "Suppresses asset-check alarm"
              : annotation.annotation_status ===
                  "annotated_envelope_exceeded"
                ? "Annotation envelope BROKEN — alarm re-fires"
                : "—"}
          </dd>
        </div>
      </dl>

      {annotation.annotation_source_citation && (
        <p className="mt-3 border-t border-current/20 pt-2 opacity-80">
          <span className="uppercase tracking-wider opacity-70">
            Source ·{" "}
          </span>
          <span className="break-words">
            {annotation.annotation_source_citation}
          </span>
        </p>
      )}
    </div>
  );
}
