/**
 * Server-rendered "data current as of <date>" badge.
 *
 * Why this exists
 * ---------------
 * VISION_2026 §7.1: every page that shows quantitative results carries a
 * verifiable-data contract — and that contract extends to staleness. A
 * dollar amount that is "true as of three years ago" is not the same
 * answer as "true as of last week," and the user is owed the difference.
 *
 * The badge is rendered server-side from a single
 * `derived.v_platform_freshness_headline` row and the most-recent
 * materialization timestamp. Zero client JS — the page is faster and the
 * markup is cacheable on the edge.
 *
 * Variants
 * --------
 *   <FreshnessBadge headline={...} variant="compact" />  -- inline pill
 *   <FreshnessBadge headline={...} variant="detail"  />  -- full panel
 *                                                          with per-bucket
 *                                                          counts
 *
 * Both variants link to the methodology page (#freshness anchor) so a user
 * can drill into per-source detail.
 */

import {
  type PlatformFreshnessHeadline,
  formatFreshnessDate,
  freshnessStatusClasses,
  summarizeFreshness,
} from "@/lib/freshness";

export interface FreshnessBadgeProps {
  headline: PlatformFreshnessHeadline;
  variant?: "compact" | "detail";
  /**
   * Optional href; when set, the badge is wrapped in an anchor pointing at
   * the per-source detail (typically /housing/methodology#freshness).
   */
  href?: string;
}

export function FreshnessBadge({
  headline,
  variant = "compact",
  href,
}: FreshnessBadgeProps) {
  const palette = freshnessStatusClasses(headline.overall_status);
  const labelMap: Record<typeof headline.overall_status, string> = {
    FRESH: "Data fresh",
    PARTIAL: "Partial substrate",
    STALE: "Publisher delay",
    CRITICAL: "Refresh overdue",
  };

  const dotColorMap: Record<typeof headline.overall_status, string> = {
    FRESH: "bg-emerald-500",
    PARTIAL: "bg-slate-400",
    STALE: "bg-amber-500",
    CRITICAL: "bg-red-500",
  };

  const lastFetched = formatFreshnessDate(headline.most_recent_materialization);

  if (variant === "compact") {
    const inner = (
      <span
        className={[
          "inline-flex items-center gap-2 rounded-full px-3 py-1",
          "text-xs font-medium ring-1",
          palette.bg,
          palette.fg,
          palette.ring,
        ].join(" ")}
        title={summarizeFreshness(headline)}
      >
        <span
          aria-hidden
          className={[
            "h-1.5 w-1.5 rounded-full",
            dotColorMap[headline.overall_status],
          ].join(" ")}
        />
        <span>{labelMap[headline.overall_status]}</span>
        <span className="opacity-70">·</span>
        <span className="font-normal opacity-80">last fetch {lastFetched}</span>
      </span>
    );
    return href ? (
      <a href={href} className="no-underline hover:opacity-90">
        {inner}
      </a>
    ) : (
      inner
    );
  }

  // Detail variant.
  return (
    <section
      aria-label="Data freshness"
      className={[
        "rounded-lg border px-4 py-3",
        palette.bg,
        palette.ring,
        "border-current/10",
      ].join(" ")}
    >
      <div className="flex items-start justify-between gap-4">
        <div>
          <h3
            className={["text-sm font-semibold", palette.fg].join(" ")}
            data-testid="freshness-overall"
          >
            <span
              aria-hidden
              className={[
                "mr-2 inline-block h-2 w-2 rounded-full align-middle",
                dotColorMap[headline.overall_status],
              ].join(" ")}
            />
            {labelMap[headline.overall_status]}
          </h3>
          <p className={["mt-1 text-xs", palette.fg, "opacity-80"].join(" ")}>
            {summarizeFreshness(headline)}
          </p>
        </div>
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-4">
          <div>
            <dt className="opacity-70">Fresh</dt>
            <dd className="font-mono">{headline.n_fresh}</dd>
          </div>
          <div>
            <dt className="opacity-70">Stale</dt>
            <dd className="font-mono">{headline.n_stale}</dd>
          </div>
          <div>
            <dt className="opacity-70">Critical</dt>
            <dd className="font-mono">{headline.n_critical}</dd>
          </div>
          <div>
            <dt className="opacity-70">Partial</dt>
            <dd className="font-mono">{headline.n_never_materialized}</dd>
          </div>
        </dl>
      </div>
      <p
        className={[
          "mt-3 text-xs",
          palette.fg,
          "opacity-80",
          "border-t border-current/10 pt-2",
        ].join(" ")}
      >
        Most recent ingester run: <span className="font-mono">{lastFetched}</span>.
        {headline.worst_source_id ? (
          <>
            {" "}
            Source needing attention:{" "}
            <code className="font-mono">{headline.worst_source_id}</code>
            {headline.worst_status ? (
              <>
                {" "}
                (<span className="capitalize">{headline.worst_status.replace("_", " ")}</span>)
              </>
            ) : null}
            .
          </>
        ) : null}
        {href ? (
          <>
            {" "}
            <a href={href} className="underline">
              Per-source detail.
            </a>
          </>
        ) : null}
      </p>
    </section>
  );
}
