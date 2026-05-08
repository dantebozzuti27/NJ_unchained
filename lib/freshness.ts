/**
 * Data-freshness data layer (VISION_2026 §7.1).
 *
 * Surfaces the platform's per-source publication freshness so the UI can
 * render an honest "data current as of <date>" badge on every page that
 * shows quantitative results.
 *
 * Reads from:
 *   * derived.v_data_freshness_summary       -- per-source rows
 *   * derived.v_platform_freshness_headline  -- single-row UI rollup
 *
 * Both are defined by migration 082_data_freshness_summary, formula version
 * 1.8.0-data-freshness-v1.
 *
 * Status taxonomy
 * ---------------
 *   fresh               -- within (cadence_period + publisher_lag_hours)
 *   stale               -- 1-1.5x of the budget (publisher slipped a release)
 *   critical            -- >1.5x (publisher revised cadence OR ingester broken)
 *   never_materialized  -- registered in ref.release_calendar but no
 *                          'materialized' signal yet (substrate-honest:
 *                          the platform never silently invents staleness)
 *
 * Per-page consumption pattern: call `getPlatformFreshnessHeadline()` once
 * at the top of the route's RSC and pass the result to `<FreshnessBadge />`.
 * For per-source detail, call `getSourceFreshness("raw.fhfa_hpi_county")`
 * inline next to the metric that source feeds.
 */

import { getSql } from "./db";

export type FreshnessStatus =
  | "fresh"
  | "stale"
  | "critical"
  | "never_materialized";

export type OverallFreshnessStatus = "FRESH" | "PARTIAL" | "STALE" | "CRITICAL";

/**
 * Per-source freshness row, mirroring derived.v_data_freshness_summary.
 *
 * `last_materialized_at` is the most recent 'materialized' signal in
 * governance.dataset_health. NULL when the source has never been
 * materialized in this DB (status = "never_materialized").
 */
export type SourceFreshness = {
  source_id: string;
  cadence: string;
  schedule_label: string;
  expected_lag_hours: number;
  cadence_period_hours: number | null;
  expected_max_age_hours: number;
  last_materialized_at: Date | null;
  hours_since_materialized: number | null;
  freshness_status: FreshnessStatus;
  publisher_notes: string | null;
};

/**
 * Single-row platform-wide rollup, mirroring derived.v_platform_freshness_headline.
 *
 * `worst_source_id` is the source most in need of attention (a critical or
 * stale source); NULL when all sources are fresh or never_materialized.
 * `overall_status` follows worst-source dominance: any critical -> CRITICAL;
 * any stale -> STALE; any never_materialized -> PARTIAL; else FRESH.
 */
export type PlatformFreshnessHeadline = {
  n_sources: number;
  n_fresh: number;
  n_stale: number;
  n_critical: number;
  n_never_materialized: number;
  most_recent_materialization: Date | null;
  oldest_materialization: Date | null;
  worst_source_id: string | null;
  worst_status: FreshnessStatus | null;
  overall_status: OverallFreshnessStatus;
};

/**
 * Fetch the single-row platform-wide headline.
 *
 * The view always returns exactly one row (even on an empty DB it returns
 * zeros); this function never returns NULL. If the view itself is missing
 * (migration 082 not applied), the call surfaces the underlying psql error
 * loudly so the next deploy fixes it.
 */
export async function getPlatformFreshnessHeadline(): Promise<PlatformFreshnessHeadline> {
  const sql = getSql();
  const rows = (await sql`
    SELECT
        n_sources::INT                  AS n_sources,
        n_fresh::INT                    AS n_fresh,
        n_stale::INT                    AS n_stale,
        n_critical::INT                 AS n_critical,
        n_never_materialized::INT       AS n_never_materialized,
        most_recent_materialization,
        oldest_materialization,
        worst_source_id,
        worst_status,
        overall_status
    FROM derived.v_platform_freshness_headline
  `) as Array<{
    n_sources: number;
    n_fresh: number;
    n_stale: number;
    n_critical: number;
    n_never_materialized: number;
    most_recent_materialization: string | null;
    oldest_materialization: string | null;
    worst_source_id: string | null;
    worst_status: string | null;
    overall_status: string;
  }>;
  if (rows.length === 0) {
    throw new Error(
      "derived.v_platform_freshness_headline returned 0 rows; migration 082 not applied?",
    );
  }
  const r = rows[0];
  return {
    n_sources: r.n_sources,
    n_fresh: r.n_fresh,
    n_stale: r.n_stale,
    n_critical: r.n_critical,
    n_never_materialized: r.n_never_materialized,
    most_recent_materialization: r.most_recent_materialization
      ? new Date(r.most_recent_materialization)
      : null,
    oldest_materialization: r.oldest_materialization
      ? new Date(r.oldest_materialization)
      : null,
    worst_source_id: r.worst_source_id,
    worst_status: r.worst_status as FreshnessStatus | null,
    overall_status: r.overall_status as OverallFreshnessStatus,
  };
}

/**
 * Fetch per-source freshness rows for a specific subset, ordered by
 * source_id. Pass an empty array to fetch every registered source.
 *
 * Used by the methodology box / detail tooltip to show "raw.fhfa_hpi_county
 * was last refreshed on <date>" inline.
 */
export async function getSourceFreshness(
  sourceIds: readonly string[] = [],
): Promise<SourceFreshness[]> {
  const sql = getSql();
  // Neon's tagged-template binding does not support TEXT[] in IN-list
  // position, but it does support `= ANY(<expr>)`. We pass the source_ids
  // as a Postgres array literal (or NULL for "all sources") via a single
  // template parameter.
  const filter = sourceIds.length === 0 ? null : sourceIds;
  const rows = (await sql`
    SELECT
        source_id,
        cadence,
        schedule_label,
        expected_lag_hours::INT          AS expected_lag_hours,
        cadence_period_hours::INT        AS cadence_period_hours,
        expected_max_age_hours::INT      AS expected_max_age_hours,
        last_materialized_at,
        hours_since_materialized::FLOAT  AS hours_since_materialized,
        freshness_status,
        publisher_notes
    FROM derived.v_data_freshness_summary
    WHERE ${filter}::TEXT[] IS NULL
       OR source_id = ANY(${filter}::TEXT[])
    ORDER BY source_id
  `) as Array<{
    source_id: string;
    cadence: string;
    schedule_label: string;
    expected_lag_hours: number;
    cadence_period_hours: number | null;
    expected_max_age_hours: number;
    last_materialized_at: string | null;
    hours_since_materialized: number | null;
    freshness_status: string;
    publisher_notes: string | null;
  }>;
  return rows.map((r) => ({
    source_id: r.source_id,
    cadence: r.cadence,
    schedule_label: r.schedule_label,
    expected_lag_hours: r.expected_lag_hours,
    cadence_period_hours: r.cadence_period_hours,
    expected_max_age_hours: r.expected_max_age_hours,
    last_materialized_at: r.last_materialized_at
      ? new Date(r.last_materialized_at)
      : null,
    hours_since_materialized: r.hours_since_materialized,
    freshness_status: r.freshness_status as FreshnessStatus,
    publisher_notes: r.publisher_notes,
  }));
}

// ---------------------------------------------------------------------------
// Display helpers
// ---------------------------------------------------------------------------

/** Map an overall_status to a Tailwind palette compatible with the
 *  housing/personalize design system (emerald / amber / red / slate). */
export function freshnessStatusClasses(
  status: OverallFreshnessStatus | FreshnessStatus,
): { bg: string; fg: string; ring: string } {
  switch (status) {
    case "FRESH":
    case "fresh":
      return {
        bg: "bg-emerald-50 dark:bg-emerald-950",
        fg: "text-emerald-700 dark:text-emerald-300",
        ring: "ring-emerald-200 dark:ring-emerald-800",
      };
    case "PARTIAL":
    case "never_materialized":
      return {
        bg: "bg-slate-100 dark:bg-slate-800",
        fg: "text-slate-700 dark:text-slate-300",
        ring: "ring-slate-300 dark:ring-slate-700",
      };
    case "STALE":
    case "stale":
      return {
        bg: "bg-amber-50 dark:bg-amber-950",
        fg: "text-amber-800 dark:text-amber-300",
        ring: "ring-amber-200 dark:ring-amber-800",
      };
    case "CRITICAL":
    case "critical":
      return {
        bg: "bg-red-50 dark:bg-red-950",
        fg: "text-red-800 dark:text-red-300",
        ring: "ring-red-200 dark:ring-red-800",
      };
  }
}

/** Human-readable summary of the headline rollup. */
export function summarizeFreshness(h: PlatformFreshnessHeadline): string {
  switch (h.overall_status) {
    case "FRESH":
      return `All ${h.n_sources} sources fresh.`;
    case "STALE":
      return `${h.n_stale} of ${h.n_sources} sources running late (publisher delay).`;
    case "CRITICAL":
      return `${h.n_critical} of ${h.n_sources} sources past 1.5x of expected refresh window.`;
    case "PARTIAL":
      return `${h.n_never_materialized} of ${h.n_sources} sources have never been materialized in this environment.`;
  }
}

/** Format a Date as "May 8, 2026" for the badge. */
export function formatFreshnessDate(d: Date | null): string {
  if (!d) return "—";
  return d.toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

/** Format hours-since-materialized as a coarse human string ("3 days ago"). */
export function formatAge(hours: number | null): string {
  if (hours == null || !Number.isFinite(hours)) return "—";
  if (hours < 1) return "just now";
  if (hours < 48) return `${Math.round(hours)}h ago`;
  const days = hours / 24;
  if (days < 60) return `${Math.round(days)}d ago`;
  const months = days / 30;
  if (months < 24) return `${Math.round(months)}mo ago`;
  return `${Math.round(months / 12)}y ago`;
}
