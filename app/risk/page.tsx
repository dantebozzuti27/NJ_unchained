import Link from "next/link";

import { isDbReachable } from "@/lib/db";
import {
  getCycleSummary,
  getNjFederalOfficials,
  listAvailableCycles,
  listTopNjAnomalies,
  listTopRiskEntities,
  resolveDefaultCycle,
} from "@/lib/queries";
import { fmtScore, riskTier } from "@/lib/format";
import type {
  CycleSummary,
  EntityKind,
  NjAnomalyCard,
  NjFederalOfficial,
  RiskRow,
} from "@/lib/types";

export const dynamic = "force-dynamic";
export const revalidate = 0;

const KIND_LABELS: Record<EntityKind, string> = {
  candidate: "Candidate",
  committee: "Committee",
  treasurer: "Treasurer",
  donor: "Donor",
  donor_cluster: "Donor cluster",
  contractor: "Contractor",
  address: "Address cluster",
};

const PARTY_LABELS: Record<string, { fg: string; bg: string }> = {
  DEM: {
    fg: "text-blue-900 dark:text-blue-100",
    bg: "bg-blue-100 dark:bg-blue-900",
  },
  REP: {
    fg: "text-red-900 dark:text-red-100",
    bg: "bg-red-100 dark:bg-red-900",
  },
  IND: {
    fg: "text-purple-900 dark:text-purple-100",
    bg: "bg-purple-100 dark:bg-purple-900",
  },
};

interface SearchParams {
  cycle?: string;
  scope?: string;
  limit?: string;
}

export default async function RiskPage({
  searchParams,
}: {
  searchParams: Promise<SearchParams>;
}) {
  const params = await searchParams;
  const reachable = await isDbReachable();

  if (!reachable.reachable) {
    return (
      <div className="rounded-md bg-amber-50 dark:bg-amber-950 p-4">
        <div className="font-medium text-amber-800 dark:text-amber-200">
          Database not reachable.
        </div>
        <p className="mt-1 text-sm text-amber-700 dark:text-amber-300">
          Configure <code className="font-mono">NEON_DATABASE_URL</code>{" "}
          in your Vercel project to populate this view.
        </p>
      </div>
    );
  }

  const cycle = params.cycle ?? (await resolveDefaultCycle());
  const scope = params.scope === "national" ? "national" : "nj";
  const limit = Math.min(
    Math.max(parseInt(params.limit ?? "20", 10) || 20, 1),
    100,
  );

  let officials: NjFederalOfficial[] = [];
  let anomalies: NjAnomalyCard[] = [];
  let nationalRows: RiskRow[] = [];
  let summary: CycleSummary = {
    cycle,
    n_candidates: 0,
    n_committees: 0,
    ingested_at_iso: null,
    hours_since_ingest: null,
  };
  let availableCycles: string[] = [];
  let dbError: string | null = null;

  try {
    [summary, availableCycles] = await Promise.all([
      getCycleSummary(cycle),
      listAvailableCycles(),
    ]);
    if (scope === "nj") {
      const [o, a] = await Promise.all([
        getNjFederalOfficials(cycle),
        listTopNjAnomalies({ cycle, limit }),
      ]);
      officials = o;
      anomalies = a;
    } else {
      nationalRows = await listTopRiskEntities({ cycle, limit });
    }
  } catch (e) {
    dbError = e instanceof Error ? e.message : String(e);
  }

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">
            Civic-integrity risk
          </h1>
          <p className="mt-1 max-w-3xl text-sm text-zinc-600 dark:text-zinc-400">
            Structural-anomaly score (0–100) for FEC-registered entities in
            cycle{" "}
            <span className="font-mono font-semibold">{cycle}</span>.
            Built from federal source data only — FEC bulk filings,{" "}
            cross-referenced against published OIG / SAM / USAspending
            registries.{" "}
            <em>Not</em> a probability of fraud — see{" "}
            <Link href="/about" className="underline">
              about
            </Link>{" "}
            for the methodology.
          </p>
        </div>
        <ScopeToggle cycle={cycle} scope={scope} limit={limit} />
      </header>

      <CycleFreshnessBar
        summary={summary}
        availableCycles={availableCycles}
        scope={scope}
        limit={limit}
      />

      {dbError ? (
        <div className="rounded-md bg-red-50 dark:bg-red-950 p-4 text-sm">
          <div className="font-medium text-red-800 dark:text-red-200">
            Database query failed.
          </div>
          <pre className="mt-2 overflow-x-auto rounded bg-red-100/60 dark:bg-red-900/40 p-2 text-xs text-red-900 dark:text-red-200">
            {dbError}
          </pre>
        </div>
      ) : scope === "nj" ? (
        <>
          <NjFederalOfficialsSection
            officials={officials}
            cycle={cycle}
          />
          <NjAnomaliesSection anomalies={anomalies} />
          <ScopeBoundaryNote />
        </>
      ) : (
        <NationalSection rows={nationalRows} cycle={cycle} />
      )}
    </div>
  );
}

function ScopeToggle({
  cycle,
  scope,
  limit,
}: {
  cycle: string;
  scope: "nj" | "national";
  limit: number;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs">
      <div className="flex rounded-md border border-zinc-300 dark:border-zinc-700 overflow-hidden">
        <Link
          href={`/risk?cycle=${encodeURIComponent(cycle)}&scope=nj&limit=${limit}`}
          className={`px-3 py-1.5 ${
            scope === "nj"
              ? "bg-zinc-900 dark:bg-zinc-100 text-white dark:text-zinc-900 font-semibold"
              : "bg-white dark:bg-zinc-900 text-zinc-700 dark:text-zinc-300 hover:bg-zinc-50 dark:hover:bg-zinc-800"
          }`}
        >
          New Jersey
        </Link>
        <Link
          href={`/risk?cycle=${encodeURIComponent(cycle)}&scope=national&limit=${limit}`}
          className={`px-3 py-1.5 border-l border-zinc-300 dark:border-zinc-700 ${
            scope === "national"
              ? "bg-zinc-900 dark:bg-zinc-100 text-white dark:text-zinc-900 font-semibold"
              : "bg-white dark:bg-zinc-900 text-zinc-700 dark:text-zinc-300 hover:bg-zinc-50 dark:hover:bg-zinc-800"
          }`}
        >
          National
        </Link>
      </div>
      <span className="font-mono text-zinc-500">cycle {cycle}</span>
    </div>
  );
}

/**
 * Shows the user how current the underlying FEC bulk is for the
 * selected cycle (so they can immediately tell whether the displayed
 * incumbents reflect today's reality or last year's), and lets them
 * pick a different loaded cycle to view historical / future filings.
 */
function CycleFreshnessBar({
  summary,
  availableCycles,
  scope,
  limit,
}: {
  summary: CycleSummary;
  availableCycles: string[];
  scope: "nj" | "national";
  limit: number;
}) {
  const cycle = summary.cycle;
  const ingestedISO = summary.ingested_at_iso;
  const ingestedDate = ingestedISO ? new Date(ingestedISO) : null;
  const hours = summary.hours_since_ingest;

  let freshnessLabel: string;
  let freshnessTone: string;
  if (hours == null) {
    freshnessLabel = "no ingest timestamp";
    freshnessTone =
      "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300";
  } else if (hours < 48) {
    freshnessLabel = `refreshed ${formatHours(hours)}`;
    freshnessTone =
      "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200";
  } else if (hours < 24 * 14) {
    freshnessLabel = `refreshed ${formatHours(hours)}`;
    freshnessTone =
      "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-200";
  } else {
    freshnessLabel = `refreshed ${formatHours(hours)}`;
    freshnessTone =
      "bg-orange-100 text-orange-800 dark:bg-orange-950 dark:text-orange-200";
  }

  const otherCycles = availableCycles.filter((c) => c !== cycle);

  return (
    <div className="rounded-md border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 px-4 py-3 text-xs flex flex-wrap items-center gap-x-4 gap-y-2">
      <div className="flex items-center gap-2">
        <span
          className={`inline-flex items-center rounded-full px-2 py-0.5 font-mono ${freshnessTone}`}
          title={
            ingestedISO
              ? `Most recent ingested_at on raw.fec_candidate / raw.fec_committee for cycle ${cycle}: ${ingestedISO}`
              : ""
          }
        >
          cycle {cycle} · {freshnessLabel}
        </span>
        {ingestedDate ? (
          <span className="font-mono text-zinc-500">
            ingested {ingestedDate.toISOString().slice(0, 16).replace("T", " ")}{" "}
            UTC
          </span>
        ) : null}
      </div>
      <div className="font-mono text-zinc-500">
        scope: {summary.n_candidates.toLocaleString()} candidates ·{" "}
        {summary.n_committees.toLocaleString()} committees in{" "}
        <span className="font-semibold">raw.fec_*</span>
      </div>
      {otherCycles.length > 0 && (
        <div className="ml-auto flex items-center gap-1">
          <span className="text-zinc-500">view cycle:</span>
          {availableCycles.map((c) => (
            <Link
              key={c}
              href={`/risk?cycle=${encodeURIComponent(
                c,
              )}&scope=${scope}&limit=${limit}`}
              className={`rounded px-2 py-0.5 font-mono ${
                c === cycle
                  ? "bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900"
                  : "border border-zinc-300 dark:border-zinc-700 text-zinc-700 dark:text-zinc-300 hover:bg-zinc-50 dark:hover:bg-zinc-800"
              }`}
            >
              {c}
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}

function formatHours(h: number): string {
  if (h < 1) {
    const mins = Math.max(1, Math.round(h * 60));
    return `${mins} min ago`;
  }
  if (h < 48) {
    return `${Math.round(h)}h ago`;
  }
  const days = h / 24;
  if (days < 30) return `${Math.round(days)}d ago`;
  const months = days / 30;
  if (months < 24) return `${Math.round(months)}mo ago`;
  return `${Math.round(months / 12)}y ago`;
}

function NjFederalOfficialsSection({
  officials,
  cycle,
}: {
  officials: NjFederalOfficial[];
  cycle: string;
}) {
  const senators = officials.filter((o) => o.office_code === "S");
  const reps = officials.filter((o) => o.office_code === "H");

  return (
    <section>
      <div className="mb-3 flex items-baseline justify-between gap-3">
        <h2 className="text-lg font-semibold tracking-tight">
          NJ federal incumbents
          <span className="ml-2 text-sm font-normal text-zinc-500">
            ({officials.length} sitting officials, cycle {cycle})
          </span>
        </h2>
        <span className="text-xs text-zinc-500">
          Source: FEC Candidate Master (cn{cycle.slice(2)}.zip),
          deduplicated by tenure (prior incumbent cycles)
        </span>
      </div>

      {officials.length === 0 ? (
        <p className="rounded-md border border-dashed border-zinc-300 dark:border-zinc-700 p-4 text-sm text-zinc-500">
          No NJ federal incumbents found in cycle {cycle}.
        </p>
      ) : (
        <>
          {senators.length > 0 && (
            <div className="mb-3">
              <div className="mb-1 text-xs uppercase tracking-wider text-zinc-500">
                U.S. Senate ({senators.length})
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                {senators.map((o) => (
                  <OfficialCard key={o.entity_id} official={o} />
                ))}
              </div>
            </div>
          )}
          {reps.length > 0 && (
            <div>
              <div className="mb-1 text-xs uppercase tracking-wider text-zinc-500">
                U.S. House of Representatives ({reps.length})
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
                {reps.map((o) => (
                  <OfficialCard key={o.entity_id} official={o} />
                ))}
              </div>
            </div>
          )}
        </>
      )}
    </section>
  );
}

function OfficialCard({ official }: { official: NjFederalOfficial }) {
  const partyStyle = official.office_party
    ? PARTY_LABELS[official.office_party] ?? {
        fg: "text-zinc-700 dark:text-zinc-300",
        bg: "bg-zinc-100 dark:bg-zinc-800",
      }
    : null;
  const isClean = official.n_signals_fired === 0;
  const districtLabel =
    official.office_code === "S"
      ? "Senate"
      : `NJ-${(official.office_district ?? "").replace(/^0/, "")}`;

  return (
    <Link
      href={`/risk/candidate/${encodeURIComponent(
        official.entity_id,
      )}?cycle=${encodeURIComponent(official.cycle)}`}
      className={`group block rounded-lg border p-3 transition-colors ${
        isClean
          ? "border-emerald-200 dark:border-emerald-900 bg-emerald-50/40 dark:bg-emerald-950/30 hover:bg-emerald-50 dark:hover:bg-emerald-950/50"
          : "border-amber-300 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/40 hover:bg-amber-100 dark:hover:bg-amber-950/60"
      }`}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
              {districtLabel}
            </span>
            {partyStyle && (
              <span
                className={`inline-flex rounded px-1.5 py-0.5 text-[10px] font-bold ${partyStyle.bg} ${partyStyle.fg}`}
              >
                {official.office_party}
              </span>
            )}
            {official.prior_incumbent_cycles === 0 && (
              <span
                className="inline-flex rounded bg-blue-100 dark:bg-blue-950 px-1.5 py-0.5 text-[10px] font-bold text-blue-800 dark:text-blue-200"
                title="No prior FEC cycle where this candidate ran as a true incumbent. Likely a new appointee, special-election winner, or first-time office-holder this cycle. FEC self-declaration only."
              >
                NEW THIS CYCLE
              </span>
            )}
          </div>
          <div className="mt-1 truncate text-sm font-semibold">
            {formatName(official.official_name)}
          </div>
          <div className="mt-0.5 truncate font-mono text-[10px] text-zinc-500">
            {official.entity_id}
          </div>
        </div>
        <div className="flex-shrink-0">
          {isClean ? (
            <span
              className="inline-flex items-center gap-0.5 rounded-full bg-emerald-100 dark:bg-emerald-900 px-2 py-0.5 text-xs font-bold text-emerald-900 dark:text-emerald-100"
              title="No structural-anomaly signals firing on this entity"
            >
              ✓ clean
            </span>
          ) : (
            <span className="inline-flex items-center gap-0.5 rounded-full bg-amber-200 dark:bg-amber-800 px-2 py-0.5 text-xs font-bold text-amber-900 dark:text-amber-100">
              {official.n_signals_fired} signal
              {official.n_signals_fired === 1 ? "" : "s"}
            </span>
          )}
        </div>
      </div>
    </Link>
  );
}

function NjAnomaliesSection({ anomalies }: { anomalies: NjAnomalyCard[] }) {
  return (
    <section>
      <div className="mb-3 flex items-baseline justify-between gap-3">
        <h2 className="text-lg font-semibold tracking-tight">
          Most anomalous NJ-relevant entities
          <span className="ml-2 text-sm font-normal text-zinc-500">
            ({anomalies.length})
          </span>
        </h2>
        <span className="text-xs text-zinc-500">
          One row per entity, ordered by composite anomaly score
        </span>
      </div>

      {anomalies.length === 0 ? (
        <div className="rounded-md border border-dashed border-zinc-300 dark:border-zinc-700 p-6 text-center text-sm text-zinc-500">
          No NJ-relevant anomalies found. The substrate may not yet be
          materialized; run{" "}
          <code className="font-mono">
            scripts/deploy_neon_pillar2_substrate.sh
          </code>
          .
        </div>
      ) : (
        <div className="space-y-2">
          {anomalies.map((a) => (
            <AnomalyCard key={`${a.entity_kind}|${a.entity_id}`} anomaly={a} />
          ))}
        </div>
      )}
    </section>
  );
}

function AnomalyCard({ anomaly }: { anomaly: NjAnomalyCard }) {
  const tier = riskTier(anomaly.risk_score);
  const officeContext =
    anomaly.entity_kind === "candidate" && anomaly.office_code
      ? formatOfficeContext(anomaly)
      : null;

  return (
    <Link
      href={`/risk/${encodeURIComponent(
        anomaly.entity_kind,
      )}/${encodeURIComponent(anomaly.entity_id)}?cycle=${encodeURIComponent(
        anomaly.cycle,
      )}`}
      className="block rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4 hover:bg-zinc-50 dark:hover:bg-zinc-800/50 transition-colors"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
              {KIND_LABELS[anomaly.entity_kind] ?? anomaly.entity_kind}
            </span>
            {officeContext && (
              <span className="inline-flex rounded bg-zinc-100 dark:bg-zinc-800 px-1.5 py-0.5 text-[10px] font-medium text-zinc-700 dark:text-zinc-300">
                {officeContext}
              </span>
            )}
            <span className="inline-flex rounded-full px-2 py-0.5 text-[10px] font-bold bg-emerald-100 text-emerald-900 dark:bg-emerald-900 dark:text-emerald-100">
              NJ
            </span>
            <span
              className={`inline-flex rounded-full px-2 py-0.5 text-[10px] font-bold ${tier.bg} ${tier.fg}`}
            >
              {tier.label}
            </span>
          </div>
          <h3 className="mt-1 truncate text-base font-semibold">
            {anomaly.display_name ?? anomaly.entity_id}
          </h3>
          {anomaly.display_name && (
            <div className="font-mono text-xs text-zinc-500">
              {anomaly.entity_id}
            </div>
          )}
        </div>
        <div className="flex flex-col items-end">
          <div className="font-mono text-2xl font-bold leading-none">
            {fmtScore(anomaly.risk_score)}
          </div>
          <div className="mt-0.5 text-[10px] uppercase tracking-wider text-zinc-500">
            {anomaly.n_signals} signal{anomaly.n_signals === 1 ? "" : "s"}
          </div>
        </div>
      </div>

      <div className="mt-3 rounded-md border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-950 p-3">
        <div className="flex flex-wrap items-center gap-2 text-[11px] text-zinc-500">
          <span className="font-mono font-semibold text-zinc-700 dark:text-zinc-300">
            {anomaly.preview_signal_id}
          </span>
          <span aria-hidden>·</span>
          <span title={`Severity ${anomaly.preview_severity}/5`}>
            {"●".repeat(anomaly.preview_severity)}
            <span className="text-zinc-300 dark:text-zinc-700">
              {"○".repeat(5 - anomaly.preview_severity)}
            </span>
          </span>
          {anomaly.preview_peer_percentile != null && (
            <>
              <span aria-hidden>·</span>
              <span>
                exceeds{" "}
                <span className="font-mono">
                  {(anomaly.preview_peer_percentile * 100).toFixed(1)}%
                </span>{" "}
                of peers
              </span>
            </>
          )}
          {anomaly.preview_citation_authority && (
            <>
              <span aria-hidden>·</span>
              <span className="font-mono">
                {anomaly.preview_citation_authority}{" "}
                {anomaly.preview_citation_section ?? ""}
              </span>
            </>
          )}
        </div>
        <p className="mt-1.5 text-sm text-zinc-700 dark:text-zinc-300">
          {anomaly.preview_explanation}
        </p>
      </div>
    </Link>
  );
}

function NationalSection({
  rows,
  cycle,
}: {
  rows: RiskRow[];
  cycle: string;
}) {
  return (
    <section>
      <div className="mb-3 flex items-baseline justify-between gap-3">
        <h2 className="text-lg font-semibold tracking-tight">
          Top {rows.length} national entities
          <span className="ml-2 text-sm font-normal text-zinc-500">
            (cycle {cycle}, no NJ filter)
          </span>
        </h2>
        <span className="text-xs text-amber-700 dark:text-amber-400">
          NJ-only is the canonical view; national surfaces stale incumbent
          records and out-of-state PACs.
        </span>
      </div>

      {rows.length === 0 ? (
        <p className="text-sm text-zinc-500">No entities scored.</p>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-zinc-200 dark:border-zinc-800">
          <table className="min-w-full text-sm">
            <thead className="bg-zinc-100 dark:bg-zinc-900 text-left text-xs uppercase tracking-wider text-zinc-500">
              <tr>
                <th className="px-3 py-2">Tier</th>
                <th className="px-3 py-2">Score</th>
                <th className="px-3 py-2">Kind</th>
                <th className="px-3 py-2">Entity</th>
                <th className="px-3 py-2 text-right">Signals</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const tier = riskTier(r.risk_score);
                return (
                  <tr
                    key={`${r.cycle}|${r.entity_kind}|${r.entity_id}`}
                    className="border-t border-zinc-200 dark:border-zinc-800 hover:bg-zinc-50 dark:hover:bg-zinc-800/50"
                  >
                    <td className="px-3 py-2">
                      <span
                        className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-bold ${tier.bg} ${tier.fg}`}
                      >
                        {tier.label}
                      </span>
                    </td>
                    <td className="px-3 py-2 font-mono font-semibold">
                      {fmtScore(r.risk_score)}
                    </td>
                    <td className="px-3 py-2 text-zinc-600 dark:text-zinc-400">
                      {KIND_LABELS[r.entity_kind] ?? r.entity_kind}
                    </td>
                    <td className="px-3 py-2">
                      <Link
                        href={`/risk/${encodeURIComponent(
                          r.entity_kind,
                        )}/${encodeURIComponent(
                          r.entity_id,
                        )}?cycle=${encodeURIComponent(r.cycle)}`}
                        className="font-mono hover:underline"
                      >
                        {r.display_name ?? r.entity_id}
                      </Link>
                    </td>
                    <td className="px-3 py-2 text-right font-mono text-zinc-600 dark:text-zinc-400">
                      {r.n_signals}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function ScopeBoundaryNote() {
  return (
    <aside className="rounded-md border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-900 p-4 text-xs text-zinc-600 dark:text-zinc-400">
      <strong className="text-zinc-700 dark:text-zinc-300">
        Scope boundary:
      </strong>{" "}
      this view covers <em>federal</em> seats only (US Senate + House) and
      FEC-registered committees / treasurers / address clusters with NJ
      filings. NJ Governor, state legislature, and county/municipal offices
      live at{" "}
      <a
        href="https://www.elec.nj.gov/publicinformation/data_download.htm"
        className="underline"
        target="_blank"
        rel="noreferrer"
      >
        NJ ELEC
      </a>{" "}
      and are scoped to a separate ingester (deferred). The national
      toggle surfaces all 5,851 entities scored for the cycle without the
      NJ filter — useful for benchmarking but introduces stale-incumbent
      noise.
    </aside>
  );
}

function formatName(rawName: string): string {
  // FEC bulk stores names as "LASTNAME, FIRSTNAME (SUFFIX)". Render as
  // "FIRSTNAME LASTNAME" for human readability.
  const m = rawName.match(/^([^,]+),\s*(.+)$/);
  if (!m) return rawName;
  const last = m[1].trim();
  const first = m[2].trim();
  return `${first} ${last}`;
}

function formatOfficeContext(a: NjAnomalyCard): string {
  if (a.office_code === "S") return "U.S. Senate (NJ)";
  if (a.office_code === "H") {
    const dist = a.office_district?.replace(/^0/, "") ?? "?";
    return `U.S. House NJ-${dist}`;
  }
  return a.office_code ?? "";
}
