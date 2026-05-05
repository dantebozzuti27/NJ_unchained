import Link from "next/link";

import { isDbReachable } from "@/lib/db";
import {
  isValidKind,
  listTopRiskEntities,
  resolveDefaultCycle,
} from "@/lib/queries";
import { fmtScore, riskTier } from "@/lib/format";
import type { EntityKind, RiskRow } from "@/lib/types";

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

const KIND_FILTERS: { value: ""; label: "All" }[] | (
  { value: EntityKind | ""; label: string }[]
) = [
  { value: "", label: "All" },
  { value: "candidate", label: "Candidates" },
  { value: "committee", label: "Committees" },
  { value: "treasurer", label: "Treasurers" },
  { value: "donor", label: "Donors" },
  { value: "donor_cluster", label: "Donor clusters" },
  { value: "contractor", label: "Contractors" },
  { value: "address", label: "Address clusters" },
];

interface SearchParams {
  kind?: string;
  cycle?: string;
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
  const kindParam = params.kind ?? "";
  const kind: EntityKind | undefined =
    kindParam !== "" && isValidKind(kindParam) ? kindParam : undefined;
  const limit = Math.min(
    Math.max(parseInt(params.limit ?? "100", 10) || 100, 1),
    500,
  );

  let rows: RiskRow[];
  let dbError: string | null = null;
  try {
    rows = await listTopRiskEntities({ cycle, kind, limit });
  } catch (e) {
    rows = [];
    dbError = e instanceof Error ? e.message : String(e);
  }

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Risk queue</h1>
          <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">
            Top {rows.length.toLocaleString()} entities by composite risk
            score for cycle{" "}
            <span className="font-mono font-semibold">{cycle}</span>.
            Sorted by{" "}
            <span className="font-mono">risk_score DESC</span>.
          </p>
        </div>
        <FiltersForm cycle={cycle} kind={kindParam} limit={limit} />
      </header>

      {dbError ? (
        <div className="rounded-md bg-red-50 dark:bg-red-950 p-4 text-sm">
          <div className="font-medium text-red-800 dark:text-red-200">
            Database query failed.
          </div>
          <pre className="mt-2 overflow-x-auto rounded bg-red-100/60 dark:bg-red-900/40 p-2 text-xs text-red-900 dark:text-red-200">
            {dbError}
          </pre>
        </div>
      ) : rows.length === 0 ? (
        <EmptyState />
      ) : (
        <RiskTable rows={rows} />
      )}
    </div>
  );
}

function FiltersForm({
  cycle,
  kind,
  limit,
}: {
  cycle: string;
  kind: string;
  limit: number;
}) {
  return (
    <form
      method="get"
      className="flex flex-wrap gap-2 text-sm items-center"
    >
      <label className="flex items-center gap-1">
        <span className="text-zinc-500">Cycle</span>
        <input
          name="cycle"
          defaultValue={cycle}
          className="w-20 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 px-2 py-1 font-mono"
        />
      </label>
      <label className="flex items-center gap-1">
        <span className="text-zinc-500">Kind</span>
        <select
          name="kind"
          defaultValue={kind}
          className="rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 px-2 py-1"
        >
          {KIND_FILTERS.map((f) => (
            <option key={f.value} value={f.value}>
              {f.label}
            </option>
          ))}
        </select>
      </label>
      <label className="flex items-center gap-1">
        <span className="text-zinc-500">Limit</span>
        <input
          name="limit"
          type="number"
          min={1}
          max={500}
          defaultValue={limit}
          className="w-20 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 px-2 py-1 font-mono"
        />
      </label>
      <button
        type="submit"
        className="rounded-md bg-zinc-900 dark:bg-zinc-100 px-3 py-1 text-white dark:text-zinc-900 text-sm font-medium hover:opacity-90"
      >
        Filter
      </button>
    </form>
  );
}

function RiskTable({ rows }: { rows: RiskRow[] }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-zinc-200 dark:border-zinc-800">
      <table className="min-w-full text-sm">
        <thead className="bg-zinc-100 dark:bg-zinc-900 text-left text-xs uppercase tracking-wider text-zinc-500">
          <tr>
            <th className="px-3 py-2">Tier</th>
            <th className="px-3 py-2">Score</th>
            <th className="px-3 py-2">Kind</th>
            <th className="px-3 py-2">Entity</th>
            <th className="px-3 py-2">Families</th>
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
                    )}/${encodeURIComponent(r.entity_id)}?cycle=${encodeURIComponent(
                      r.cycle,
                    )}`}
                    className="font-mono hover:underline"
                  >
                    {r.display_name ?? r.entity_id}
                  </Link>
                  {r.display_name && (
                    <span className="ml-2 text-xs font-mono text-zinc-500">
                      {r.entity_id}
                    </span>
                  )}
                </td>
                <td className="px-3 py-2">
                  <div className="flex flex-wrap gap-1">
                    {r.signal_families.length === 0 ? (
                      <span className="text-zinc-500 italic">none</span>
                    ) : (
                      r.signal_families.map((f) => (
                        <span
                          key={f}
                          className="rounded bg-zinc-100 dark:bg-zinc-800 px-1.5 py-0.5 text-xs font-mono"
                        >
                          {f}
                        </span>
                      ))
                    )}
                  </div>
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
  );
}

function EmptyState() {
  return (
    <div className="rounded-md border border-dashed border-zinc-300 dark:border-zinc-700 p-8 text-center text-sm text-zinc-600 dark:text-zinc-400">
      <p className="font-medium">No entities scored yet for this cycle.</p>
      <p className="mt-1">
        The database schema is present, but{" "}
        <code className="font-mono">derived.v_entity_fraud_risk</code> has
        no rows. Run the substrate ingesters and Dagster materializations
        to populate the L3a view.
      </p>
    </div>
  );
}
