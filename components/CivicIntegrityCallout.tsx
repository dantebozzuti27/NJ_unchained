import Link from "next/link";

import type { NjCivicIntegritySummary } from "@/lib/types";

/**
 * Cross-pillar callout rendered on /housing/[id] county pages. Surfaces
 * the state-wide NJ civic-integrity context for the most recent FEC
 * cycle so a Pillar-1 housing-affordability page does not exist in
 * isolation from the Pillar-2 fraud-evidence surface.
 *
 * Substrate-honesty boundaries explicitly noted in the body:
 *   1. The numbers are NJ-WIDE, not for this specific county. Per-
 *      county granularity needs the HUD USPS-County crosswalk
 *      (currently gated on a HUD API key registration; deferred work
 *      item documented in work_left.txt).
 *   2. The callout does NOT claim any causal link between housing-
 *      affordability strain and federal-campaign-finance anomalies. It
 *      is a side-by-side context surface; the user clicks through to
 *      /risk for the actual evidence cards.
 *   3. The "structural anomaly" framing is preserved verbatim from the
 *      /risk page so the platform speaks one language about Pillar 2
 *      across all surfaces.
 */
export function CivicIntegrityCallout({
  summary,
  cycle,
  countyName,
}: {
  summary: NjCivicIntegritySummary;
  cycle: string;
  countyName: string;
}) {
  const hasAny = summary.total_nj_entities_with_signals > 0;

  return (
    <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4 text-sm">
      <div className="flex items-baseline justify-between gap-3 flex-wrap">
        <h2 className="font-medium">
          Civic-integrity context (NJ-wide, cycle {cycle})
        </h2>
        <Link
          href={`/risk?cycle=${encodeURIComponent(cycle)}&scope=nj`}
          className="text-xs underline underline-offset-4 text-zinc-600 dark:text-zinc-400 hover:text-zinc-900 dark:hover:text-zinc-100"
        >
          View on /risk →
        </Link>
      </div>

      <p className="mt-2 text-zinc-700 dark:text-zinc-300">
        While {countyName} County faces measurable housing-affordability
        strain, the platform also tracks structural-anomaly signals on
        federal campaign-finance entities operating in NJ. The numbers
        below are state-wide for cycle {cycle} (per-county breakdown is
        gated on the HUD USPS-County crosswalk; see methodology note
        below). This is parallel context — the platform makes no causal
        claim linking housing strain to campaign-finance anomalies.
      </p>

      {hasAny ? (
        <dl className="mt-3 grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
          <KV
            label="Candidates with signals"
            value={`${summary.n_candidates_with_signals.toLocaleString()} of ${summary.n_candidates_total.toLocaleString()}`}
            sub={`max score ${summary.max_candidate_risk_score.toFixed(1)}`}
          />
          <KV
            label="Committees with signals"
            value={`${summary.n_committees_with_signals.toLocaleString()} of ${summary.n_committees_total.toLocaleString()}`}
            sub={`max score ${summary.max_committee_risk_score.toFixed(1)}`}
          />
          <KV
            label="Address clusters with signals"
            value={summary.n_addresses_with_signals.toLocaleString()}
            sub={`max score ${summary.max_address_risk_score.toFixed(1)}`}
          />
          <KV
            label="Headline max score"
            value={summary.max_nj_risk_score.toFixed(1)}
            sub="0–100 structural-anomaly scale"
          />
        </dl>
      ) : (
        <div className="mt-3 rounded-md border border-dashed border-zinc-300 dark:border-zinc-700 p-3 text-xs text-zinc-600 dark:text-zinc-400">
          No structural-anomaly signals firing on any NJ-keyed federal
          entity for cycle {cycle}.
        </div>
      )}

      <p className="mt-3 text-xs text-zinc-500">
        Source: FEC Candidate Master + Committee Master (cn{cycle.slice(2)}.zip,
        cm{cycle.slice(2)}.zip), filtered to{" "}
        <span className="font-mono">cand_office_st = &lsquo;NJ&rsquo;</span>{" "}
        / <span className="font-mono">cmte_st = &lsquo;NJ&rsquo;</span> /
        physical state token = NJ for address clusters. Treasurers are
        excluded from this state-level roll-up because a single treasurer
        can serve both NJ and non-NJ committees and the state-level dedup
        is ambiguous; see <code className="font-mono">/risk</code> for
        the per-treasurer evidence with explicit{" "}
        <span className="font-mono">is_nj</span> attribution. Per-county
        granularity requires the HUD USPS-County ZIP crosswalk in{" "}
        <code className="font-mono">ref.zip_county</code>; that table is
        currently empty and the loader is gated on a HUD API key
        registration (deferred-work item).
      </p>
    </section>
  );
}

function KV({
  label,
  value,
  sub,
}: {
  label: string;
  value: string;
  sub?: string;
}) {
  return (
    <div className="space-y-0.5">
      <dt className="text-zinc-500">{label}</dt>
      <dd className="font-mono text-base font-semibold text-zinc-900 dark:text-zinc-100">
        {value}
      </dd>
      {sub && <dd className="font-mono text-[10px] text-zinc-500">{sub}</dd>}
    </div>
  );
}
