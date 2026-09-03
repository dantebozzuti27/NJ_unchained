import Link from "next/link";
import type { ReactNode } from "react";

import { TableControls } from "@/components/TableControls";
import { isDbReachable } from "@/lib/db";
import {
  getCycleSummary,
  getNjFederalOfficials,
  getNjStateCandidates,
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
  NjStateCandidate,
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
  nj_state_candidate: "NJ state candidate",
  provider: "Healthcare provider",
  employer: "H-1B employer",
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
  q?: string;
  kind?: string;
  sort?: string;
  dir?: string;
}

const ANOMALY_SORT_OPTIONS = [
  { value: "score", label: "Risk score" },
  { value: "signals", label: "# signals" },
  { value: "severity", label: "Top severity" },
];
const NATIONAL_SORT_OPTIONS = [
  { value: "score", label: "Risk score" },
  { value: "signals", label: "# signals" },
  { value: "families", label: "# signal families" },
];

function numericCompare(
  a: number | null,
  b: number | null,
  asc: boolean,
): number {
  if (a == null && b == null) return 0;
  if (a == null) return 1;
  if (b == null) return -1;
  return asc ? a - b : b - a;
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
  let stateCandidates: NjStateCandidate[] = [];
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
      const [o, s, a] = await Promise.all([
        getNjFederalOfficials(cycle),
        getNjStateCandidates(),
        listTopNjAnomalies({ cycle, limit }),
      ]);
      officials = o;
      stateCandidates = s;
      anomalies = a;
    } else {
      nationalRows = await listTopRiskEntities({ cycle, limit });
    }
  } catch (e) {
    dbError = e instanceof Error ? e.message : String(e);
  }

  // ---- URL-driven filter + sort over the two rankable lists ----------------
  const fq = (params.q ?? "").trim().toLowerCase();
  const kindFilter = params.kind ?? "";
  const sortKey = params.sort ?? "score";
  const asc = params.dir === "asc";

  const kindOptions = (kinds: EntityKind[]) =>
    Array.from(new Set(kinds))
      .sort()
      .map((k) => ({ value: k, label: KIND_LABELS[k] ?? k }));

  const filteredAnomalies = anomalies
    .filter((a) =>
      fq
        ? (a.display_name ?? a.entity_id).toLowerCase().includes(fq) ||
          a.entity_id.toLowerCase().includes(fq)
        : true,
    )
    .filter((a) => (kindFilter ? a.entity_kind === kindFilter : true))
    .sort((a, b) => {
      if (sortKey === "signals")
        return numericCompare(a.n_signals, b.n_signals, asc);
      if (sortKey === "severity")
        return numericCompare(a.preview_severity, b.preview_severity, asc);
      return numericCompare(a.risk_score, b.risk_score, asc);
    });

  const filteredNational = nationalRows
    .filter((r) =>
      fq
        ? (r.display_name ?? r.entity_id).toLowerCase().includes(fq) ||
          r.entity_id.toLowerCase().includes(fq)
        : true,
    )
    .filter((r) => (kindFilter ? r.entity_kind === kindFilter : true))
    .sort((a, b) => {
      if (sortKey === "signals")
        return numericCompare(a.n_signals, b.n_signals, asc);
      if (sortKey === "families")
        return numericCompare(
          a.n_contributing_families,
          b.n_contributing_families,
          asc,
        );
      return numericCompare(a.risk_score, b.risk_score, asc);
    });

  const anomalyControls = anomalies.length > 0 && (
    <TableControls
      search={{ param: "q", placeholder: "Entity name / ID…" }}
      filters={
        kindOptions(anomalies.map((a) => a.entity_kind)).length > 1
          ? [
              {
                param: "kind",
                label: "Kind",
                options: kindOptions(anomalies.map((a) => a.entity_kind)),
              },
            ]
          : []
      }
      sort={{
        param: "sort",
        options: ANOMALY_SORT_OPTIONS,
        defaultValue: "score",
      }}
      direction={{ param: "dir", defaultValue: "desc" }}
      shown={filteredAnomalies.length}
      total={anomalies.length}
    />
  );

  const nationalControls = nationalRows.length > 0 && (
    <TableControls
      search={{ param: "q", placeholder: "Entity name / ID…" }}
      filters={
        kindOptions(nationalRows.map((r) => r.entity_kind)).length > 1
          ? [
              {
                param: "kind",
                label: "Kind",
                options: kindOptions(nationalRows.map((r) => r.entity_kind)),
              },
            ]
          : []
      }
      sort={{
        param: "sort",
        options: NATIONAL_SORT_OPTIONS,
        defaultValue: "score",
      }}
      direction={{ param: "dir", defaultValue: "desc" }}
      shown={filteredNational.length}
      total={nationalRows.length}
    />
  );

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
          <NjStateCandidatesSection candidates={stateCandidates} />
          <NjAnomaliesSection
            anomalies={filteredAnomalies}
            totalCount={anomalies.length}
            controls={anomalyControls || undefined}
          />
          <ScopeBoundaryNote />
        </>
      ) : (
        <NationalSection
          rows={filteredNational}
          cycle={cycle}
          totalCount={nationalRows.length}
          controls={nationalControls || undefined}
        />
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

function NjStateCandidatesSection({
  candidates,
}: {
  candidates: NjStateCandidate[];
}) {
  if (candidates.length === 0) return null;

  // Group by (election_year, office) so the UI surfaces "2025 Governor"
  // as one block; future-proof for state legislature + AG races.
  type Bucket = {
    election_year: number;
    office: string;
    office_label: string;
    primary_date: string | null;
    general_date: string | null;
    rows: NjStateCandidate[];
  };
  const buckets = new Map<string, Bucket>();
  for (const c of candidates) {
    const k = `${c.election_year}|${c.office}`;
    const b = buckets.get(k);
    if (b) {
      b.rows.push(c);
    } else {
      buckets.set(k, {
        election_year: c.election_year,
        office: c.office,
        office_label: c.office_label,
        primary_date: c.primary_date,
        general_date: c.general_date,
        rows: [c],
      });
    }
  }
  const ordered = [...buckets.values()].sort((a, b) => {
    if (a.election_year !== b.election_year) {
      return b.election_year - a.election_year;
    }
    return a.office.localeCompare(b.office);
  });

  return (
    <section>
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-3">
        <h2 className="text-lg font-semibold tracking-tight">
          NJ statewide candidates
          <span className="ml-2 text-sm font-normal text-zinc-500">
            ({candidates.length} publicly-announced)
          </span>
        </h2>
        <span className="text-xs text-zinc-500">
          State-level offices (NJ ELEC jurisdiction).{" "}
          <span className="font-mono">
            campaign-finance ingest pending
          </span>{" "}
          on every card.
        </span>
      </div>

      <div className="space-y-5">
        {ordered.map((b) => (
          <NjStateCandidateBucket key={`${b.election_year}|${b.office}`} bucket={b} />
        ))}
      </div>
    </section>
  );
}

function NjStateCandidateBucket({
  bucket,
}: {
  bucket: {
    election_year: number;
    office: string;
    office_label: string;
    primary_date: string | null;
    general_date: string | null;
    rows: NjStateCandidate[];
  };
}) {
  const dems = bucket.rows.filter((c) => c.party === "DEM");
  const reps = bucket.rows.filter((c) => c.party === "REP");
  const other = bucket.rows.filter(
    (c) => c.party !== "DEM" && c.party !== "REP",
  );
  const primaryDate = bucket.primary_date
    ? new Date(bucket.primary_date).toLocaleDateString(undefined, {
        month: "short",
        day: "numeric",
        year: "numeric",
      })
    : null;
  const generalDate = bucket.general_date
    ? new Date(bucket.general_date).toLocaleDateString(undefined, {
        month: "short",
        day: "numeric",
        year: "numeric",
      })
    : null;

  return (
    <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-3">
        <div className="text-sm font-semibold">
          {bucket.election_year} {bucket.office_label}
          <span className="ml-2 font-normal text-zinc-500">
            ({bucket.rows.length} candidates)
          </span>
        </div>
        <div className="font-mono text-[11px] text-zinc-500">
          {primaryDate ? <>primary {primaryDate}</> : null}
          {primaryDate && generalDate ? <span aria-hidden> · </span> : null}
          {generalDate ? <>general {generalDate}</> : null}
        </div>
      </div>

      {dems.length > 0 && (
        <PartyColumn party="DEM" label="Democratic primary" rows={dems} />
      )}
      {reps.length > 0 && (
        <PartyColumn party="REP" label="Republican primary" rows={reps} />
      )}
      {other.length > 0 && (
        <PartyColumn party="OTHER" label="Other / unaffiliated" rows={other} />
      )}
    </div>
  );
}

function PartyColumn({
  party,
  label,
  rows,
}: {
  party: string;
  label: string;
  rows: NjStateCandidate[];
}) {
  return (
    <div className="mt-3 first:mt-0">
      <div className="mb-1 flex items-center gap-2 text-xs uppercase tracking-wider text-zinc-500">
        <span>{label}</span>
        <span className="font-mono normal-case text-[10px]">
          ({rows.length})
        </span>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
        {rows.map((c) => (
          <StateCandidateCard key={c.entity_id} candidate={c} />
        ))}
      </div>
      {/* Suppress unused-var warning while keeping the named prop. */}
      <span className="hidden" data-party={party} />
    </div>
  );
}

function StateCandidateCard({
  candidate,
}: {
  candidate: NjStateCandidate;
}) {
  const partyStyle = PARTY_LABELS[candidate.party] ?? {
    fg: "text-zinc-700 dark:text-zinc-300",
    bg: "bg-zinc-100 dark:bg-zinc-800",
  };
  const announced = candidate.announcement_date
    ? new Date(candidate.announcement_date).toLocaleDateString(undefined, {
        month: "short",
        year: "numeric",
      })
    : null;
  const verified = candidate.source_doc_date
    ? new Date(candidate.source_doc_date).toLocaleDateString(undefined, {
        month: "short",
        day: "numeric",
        year: "numeric",
      })
    : null;

  return (
    <div className="rounded-md border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-950 p-3">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span
              className={`inline-flex rounded px-1.5 py-0.5 text-[10px] font-bold ${partyStyle.bg} ${partyStyle.fg}`}
            >
              {candidate.party}
            </span>
            {candidate.campaign_finance_ingest_pending && (
              <span
                className="inline-flex rounded bg-amber-100 dark:bg-amber-950 px-1.5 py-0.5 text-[10px] font-bold text-amber-800 dark:text-amber-200"
                title="NJ ELEC contribution/expenditure data is not yet ingested. The platform makes no claims about this candidate's campaign finance."
              >
                ELEC ingest pending
              </span>
            )}
          </div>
          <div className="mt-1 truncate text-sm font-semibold">
            {candidate.full_name}
          </div>
          {candidate.prior_office && (
            <div className="mt-0.5 line-clamp-2 text-xs text-zinc-600 dark:text-zinc-400">
              {candidate.prior_office}
            </div>
          )}
        </div>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px] text-zinc-500">
        {announced && (
          <span title="Date the candidacy was publicly announced">
            announced {announced}
          </span>
        )}
        {verified && (
          <>
            {announced ? <span aria-hidden>·</span> : null}
            <span
              title="Date the platform maintainer last verified the citation URL"
              className="font-mono"
            >
              verified {verified}
            </span>
          </>
        )}
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-2 border-t border-zinc-200 dark:border-zinc-800 pt-2 text-[10px]">
        <a
          href={candidate.source_url}
          target="_blank"
          rel="noreferrer"
          className="font-mono text-blue-700 underline hover:text-blue-900 dark:text-blue-400 dark:hover:text-blue-200"
          title={`Citation: ${candidate.source_authority}`}
        >
          Verify on Wikipedia ↗
        </a>
        {candidate.announcement_url &&
          candidate.announcement_url !== candidate.source_url && (
            <>
              <span aria-hidden className="text-zinc-300 dark:text-zinc-700">
                ·
              </span>
              <a
                href={candidate.announcement_url}
                target="_blank"
                rel="noreferrer"
                className="font-mono text-zinc-700 underline hover:text-zinc-900 dark:text-zinc-300 dark:hover:text-zinc-100"
              >
                Profile ↗
              </a>
            </>
          )}
      </div>
    </div>
  );
}

function NjAnomaliesSection({
  anomalies,
  totalCount,
  controls,
}: {
  anomalies: NjAnomalyCard[];
  totalCount?: number;
  controls?: ReactNode;
}) {
  const total = totalCount ?? anomalies.length;
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

      {controls && (
        <div className="mb-3 rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-3">
          {controls}
        </div>
      )}

      {anomalies.length === 0 ? (
        <div className="rounded-md border border-dashed border-zinc-300 dark:border-zinc-700 p-6 text-center text-sm text-zinc-500">
          {total === 0 ? (
            <>
              No NJ-relevant anomalies found. The substrate may not yet be
              materialized; run{" "}
              <code className="font-mono">
                scripts/deploy_neon_pillar2_substrate.sh
              </code>
              .
            </>
          ) : (
            <>No entities match the current filters.</>
          )}
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
  totalCount,
  controls,
}: {
  rows: RiskRow[];
  cycle: string;
  totalCount?: number;
  controls?: ReactNode;
}) {
  const total = totalCount ?? rows.length;
  return (
    <section>
      <div className="mb-3 flex items-baseline justify-between gap-3">
        <h2 className="text-lg font-semibold tracking-tight">
          Top national entities
          <span className="ml-2 text-sm font-normal text-zinc-500">
            ({rows.length}, cycle {cycle}, no NJ filter)
          </span>
        </h2>
        <span className="text-xs text-amber-700 dark:text-amber-400">
          NJ-only is the canonical view; national surfaces stale incumbent
          records and out-of-state PACs.
        </span>
      </div>

      {controls && (
        <div className="mb-3 rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-3">
          {controls}
        </div>
      )}

      {rows.length === 0 ? (
        <p className="text-sm text-zinc-500">
          {total === 0
            ? "No entities scored."
            : "No entities match the current filters."}
        </p>
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
      fraud-signal evaluation in this view covers <em>federal</em>{" "}
      seats only (US Senate + House) and FEC-registered committees /
      treasurers / address clusters with NJ filings. NJ Governor + state
      legislature candidates appear in the curated{" "}
      <em>"NJ statewide candidates"</em> section above, but the platform
      runs <em>no</em> contribution / expenditure / anomaly signals
      against them until the{" "}
      <a
        href="https://www.elec.nj.gov/publicinformation/data_download.htm"
        className="underline"
        target="_blank"
        rel="noreferrer"
      >
        NJ ELEC
      </a>{" "}
      ingester ships — every state-candidate card carries an{" "}
      <span className="font-mono">ELEC ingest pending</span> badge to
      make that gap explicit. The national toggle surfaces all entities
      scored for the cycle without the NJ filter — useful for
      benchmarking but introduces stale-incumbent noise.
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
