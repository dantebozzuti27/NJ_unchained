import Link from "next/link";

import { Sparkline } from "@/components/Sparkline";
import { isDbReachable } from "@/lib/db";
import {
  BURDEN_BASE_YEAR,
  burdenTier,
  getCountyDetail,
  listCountyBurden,
  type CountyBurdenRow,
} from "@/lib/housing";

export const dynamic = "force-dynamic";
export const revalidate = 0;

export const metadata = {
  title: "Housing burden — NJ Unchained",
  description:
    "County-level housing-affordability divergence: home-price growth vs. " +
    "real wage growth across all 21 New Jersey counties.",
};

export default async function HousingPage() {
  const reachable = await isDbReachable();
  if (!reachable.reachable) {
    return (
      <div className="rounded-md bg-amber-50 dark:bg-amber-950 p-4">
        <div className="font-medium text-amber-800 dark:text-amber-200">
          Database not reachable.
        </div>
        <p className="mt-1 text-sm text-amber-700 dark:text-amber-300">
          Configure <code className="font-mono">NEON_DATABASE_URL</code> to
          populate this view.
        </p>
      </div>
    );
  }

  let rows: CountyBurdenRow[];
  let err: string | null = null;
  try {
    rows = await listCountyBurden();
  } catch (e) {
    rows = [];
    err = e instanceof Error ? e.message : String(e);
  }

  // Pre-fetch sparkline series for the rendered rows. We do this in
  // parallel and only for counties that actually have a burden ratio,
  // because rows without one will render the "—" placeholder anyway.
  const countiesWithRatio = rows.filter((r) => r.burden_ratio != null);
  const detailMap = new Map<string, Awaited<ReturnType<typeof getCountyDetail>>>();
  await Promise.all(
    countiesWithRatio.map(async (r) => {
      try {
        const d = await getCountyDetail(r.county_id);
        if (d) detailMap.set(r.county_id, d);
      } catch {
        /* leave missing -- row falls back to no sparkline */
      }
    }),
  );

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Housing burden</h1>
          <p className="mt-1 max-w-2xl text-sm text-zinc-600 dark:text-zinc-400">
            County-by-county divergence between home-price growth (FHFA HPI)
            and real wage growth (ACS 5-year median household income, deflated
            via CPI-U). Both series indexed so{" "}
            <span className="font-mono">
              {BURDEN_BASE_YEAR}
            </span>{" "}
            = 100; the burden ratio is{" "}
            <span className="font-mono">HPI / income</span>. Values above 1.0
            mean housing is outpacing wages; values below mean wages are
            keeping up.
          </p>
        </div>
      </header>

      {err ? (
        <div className="rounded-md bg-red-50 dark:bg-red-950 p-4 text-sm">
          <div className="font-medium text-red-800 dark:text-red-200">
            Database query failed.
          </div>
          <pre className="mt-2 overflow-x-auto rounded bg-red-100/60 dark:bg-red-900/40 p-2 text-xs">
            {err}
          </pre>
          <p className="mt-2 text-xs text-red-800 dark:text-red-200">
            This usually means the FHFA / ACS / CPI substrates are not loaded.
            From the repo root, run{" "}
            <code className="font-mono">nj-ingest-fhfa</code>,{" "}
            <code className="font-mono">nj-ingest-acs-income</code>, and{" "}
            <code className="font-mono">nj-ingest-cpi</code>.
          </p>
        </div>
      ) : rows.length === 0 ? (
        <EmptyState />
      ) : (
        <BurdenTable rows={rows} detailMap={detailMap} />
      )}

      <Methodology />
    </div>
  );
}

function BurdenTable({
  rows,
  detailMap,
}: {
  rows: CountyBurdenRow[];
  detailMap: Map<string, Awaited<ReturnType<typeof getCountyDetail>>>;
}) {
  return (
    <div className="overflow-x-auto rounded-lg border border-zinc-200 dark:border-zinc-800">
      <table className="min-w-full text-sm">
        <thead className="bg-zinc-100 dark:bg-zinc-900 text-left text-xs uppercase tracking-wider text-zinc-500">
          <tr>
            <th className="px-3 py-2">Tier</th>
            <th className="px-3 py-2">County</th>
            <th className="px-3 py-2 text-right">Burden ratio</th>
            <th className="px-3 py-2 text-right">HPI growth</th>
            <th className="px-3 py-2 text-right">Real income growth</th>
            <th className="px-3 py-2">Burden trend</th>
            <th className="px-3 py-2 text-right">Latest year</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const tier = burdenTier(r.burden_ratio);
            const detail = detailMap.get(r.county_id);
            return (
              <tr
                key={r.county_id}
                className="border-t border-zinc-200 dark:border-zinc-800 hover:bg-zinc-50 dark:hover:bg-zinc-800/50"
              >
                <td className="px-3 py-2">
                  <span
                    className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-bold ${tier.bg} ${tier.fg}`}
                    title={tier.description}
                  >
                    {tier.label}
                  </span>
                </td>
                <td className="px-3 py-2">
                  <Link
                    href={`/housing/${encodeURIComponent(r.county_id)}`}
                    className="font-medium hover:underline"
                  >
                    {r.county_name}
                  </Link>
                  <span className="ml-2 text-xs font-mono text-zinc-500">
                    {r.county_fips}
                  </span>
                </td>
                <td className="px-3 py-2 text-right font-mono font-semibold">
                  {r.burden_ratio == null ? "—" : r.burden_ratio.toFixed(2)}
                </td>
                <td className="px-3 py-2 text-right font-mono">
                  {fmtFactor(r.hpi_growth)}
                </td>
                <td className="px-3 py-2 text-right font-mono">
                  {fmtFactor(r.income_growth)}
                </td>
                <td className="px-3 py-2">
                  {detail ? (
                    <Sparkline
                      points={detail.burden_series.map((p) => ({
                        year: p.year,
                        indexed: p.indexed,
                      }))}
                      baseline={100}
                      title={`${r.county_name} burden ratio (${detail.burden_series[0]?.year ?? ""}–${detail.burden_series.at(-1)?.year ?? ""})`}
                      className={tier.fg}
                    />
                  ) : (
                    <span className="text-zinc-500 italic text-xs">
                      no series
                    </span>
                  )}
                </td>
                <td className="px-3 py-2 text-right font-mono text-zinc-600 dark:text-zinc-400">
                  {r.year_latest ?? "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function fmtFactor(x: number | null): string {
  if (x == null) return "—";
  const pct = (x - 1) * 100;
  const sign = pct > 0 ? "+" : "";
  return `${x.toFixed(2)}× (${sign}${pct.toFixed(0)}%)`;
}

function EmptyState() {
  return (
    <div className="rounded-md border border-dashed border-zinc-300 dark:border-zinc-700 p-8 text-center text-sm text-zinc-600 dark:text-zinc-400">
      <p className="font-medium">No housing data loaded yet.</p>
      <p className="mt-1">
        The schema is present, but the FHFA HPI and ACS income tables are
        empty. Run{" "}
        <code className="font-mono">nj-ingest-fhfa</code> +{" "}
        <code className="font-mono">nj-ingest-acs-income</code> +{" "}
        <code className="font-mono">nj-ingest-cpi</code> to populate this
        view.
      </p>
    </div>
  );
}

function Methodology() {
  return (
    <details className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4 text-sm">
      <summary className="font-medium cursor-pointer">
        How is the burden ratio computed?
      </summary>
      <div className="mt-3 space-y-2 text-zinc-700 dark:text-zinc-300">
        <p>
          For each county, we re-index two series so that{" "}
          <code className="font-mono">{BURDEN_BASE_YEAR}</code> = 100:
        </p>
        <ul className="list-disc pl-5 space-y-1">
          <li>
            <strong>HPI growth</strong>: FHFA House Price Index for the
            county (purchase-only, all-transactions) divided by the same
            county&apos;s {BURDEN_BASE_YEAR} value.
          </li>
          <li>
            <strong>Real income growth</strong>: ACS 5-year median household
            income, deflated to {BURDEN_BASE_YEAR} dollars via CPI-U All
            Items, divided by the same county&apos;s {BURDEN_BASE_YEAR}{" "}
            real-dollar value.
          </li>
        </ul>
        <p>
          The <strong>burden ratio</strong> is{" "}
          <code className="font-mono">HPI growth ÷ real income growth</code>.
          A ratio of 1.0 means home prices and real wages grew at exactly
          the same pace. A ratio of 1.40 means home prices have grown 40%
          faster than wages — a single household needs ~40% more income to
          buy the same house, in inflation-adjusted terms, as in the base
          year. Values below 1.0 mean wages have outpaced housing growth.
        </p>
        <p className="text-xs text-zinc-500">
          This metric is intentionally simple. It does not capture
          interest-rate effects on mortgage affordability, property-tax
          burden, or tenure-segmented (renter vs owner) cost-burden share.
          Those richer views are exposed via the per-county detail page
          and the backend PUMS views (<code className="font-mono">derived.housing_burden_ratio</code>).
        </p>
      </div>
    </details>
  );
}
