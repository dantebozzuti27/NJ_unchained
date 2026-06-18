import Link from "next/link";

import { FreshnessBadge } from "@/components/FreshnessBadge";
import { Sparkline } from "@/components/Sparkline";
import { TableControls } from "@/components/TableControls";
import { isDbReachable } from "@/lib/db";
import { fmtUsd } from "@/lib/format";
import {
  getPlatformFreshnessHeadline,
  type PlatformFreshnessHeadline,
} from "@/lib/freshness";
import {
  burdenTier,
  getBurdenBaseYear,
  getBurdenTierBands,
  getCountyDetail,
  getNjAffordabilityHeadline,
  listCountyBurden,
  listMuniAffordability,
  type CountyBurdenRow,
  type CountyHeadlineRow,
  type MuniBurdenRow,
  type NjAffordabilityHeadline,
  type TierBand,
} from "@/lib/housing";

export const dynamic = "force-dynamic";
export const revalidate = 0;

export const metadata = {
  title: "Housing burden — NJ Unchained",
  description:
    "County-level housing-affordability divergence: home-price growth vs. " +
    "real wage growth across all 21 New Jersey counties.",
};

interface HousingSearchParams {
  view?: string;
  q?: string;
  sort?: string;
  dir?: string;
  tier?: string;
  county?: string;
}

const SORT_OPTIONS = [
  { value: "burden", label: "Burden ratio" },
  { value: "name", label: "County" },
  { value: "gap", label: "Income gap" },
  { value: "hpi", label: "HPI growth" },
];

const TOWN_SORT_OPTIONS = [
  { value: "headroom", label: "Income gap" },
  { value: "home", label: "Home price" },
  { value: "required", label: "Required income" },
  { value: "name", label: "Town" },
];

export default async function HousingPage({
  searchParams,
}: {
  searchParams: Promise<HousingSearchParams>;
}) {
  const sp = await searchParams;
  const view = sp.view === "towns" ? "towns" : "counties";
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

  let rows: CountyBurdenRow[] = [];
  let headline: NjAffordabilityHeadline | null = null;
  let baseYear = 2010;
  let bands: TierBand[] = [];
  let freshness: PlatformFreshnessHeadline | null = null;
  let err: string | null = null;
  try {
    [rows, headline, baseYear, bands, freshness] = await Promise.all([
      listCountyBurden(),
      getNjAffordabilityHeadline(),
      getBurdenBaseYear(),
      getBurdenTierBands(),
      // Freshness is best-effort: a missing migration must not break the page.
      getPlatformFreshnessHeadline().catch(() => null),
    ]);
  } catch (e) {
    err = e instanceof Error ? e.message : String(e);
  }

  const countiesWithRatio = rows.filter((r) => r.burden_ratio != null);
  const detailMap = new Map<string, Awaited<ReturnType<typeof getCountyDetail>>>();
  // Sparklines are only rendered in the county view; skip the per-county
  // detail fetch entirely when the user is on the towns view.
  if (view === "counties") {
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
  }

  // Town-level substrate (565 NJ municipalities) — only fetched for the
  // towns view. Best-effort: a missing view must not break the page.
  let munis: MuniBurdenRow[] = [];
  if (view === "towns" && !err) {
    try {
      munis = await listMuniAffordability();
    } catch (e) {
      err = e instanceof Error ? e.message : String(e);
    }
  }

  const headroomByFips = new Map<string, CountyHeadlineRow>();
  if (headline != null) {
    for (const h of headline.rows) headroomByFips.set(h.county_fips, h);
  }

  // ---- URL-driven filter + sort (server-side over the full 21-row set) -----
  const q = (sp.q ?? "").trim().toLowerCase();
  const tierFilter = sp.tier ?? "";
  const sortKey = sp.sort ?? "burden";
  const asc = sp.dir === "asc";

  const tierOptions = bands.map((b) => ({ value: b.label, label: b.label }));

  // Nulls always sort last regardless of direction (no fabricated ordering).
  const numCmp = (a: number | null, b: number | null) => {
    if (a == null && b == null) return 0;
    if (a == null) return 1;
    if (b == null) return -1;
    return asc ? a - b : b - a;
  };
  const sortVal = (r: CountyBurdenRow): number | null => {
    switch (sortKey) {
      case "gap":
        return headroomByFips.get(r.county_fips)?.headroom ?? null;
      case "hpi":
        return r.hpi_growth;
      case "burden":
        return r.burden_ratio;
      default:
        return null;
    }
  };

  const visibleRows = rows
    .filter((r) => (q ? r.county_name.toLowerCase().includes(q) : true))
    .filter((r) =>
      tierFilter
        ? r.burden_ratio != null &&
          burdenTier(r.burden_ratio, bands).label === tierFilter
        : true,
    )
    .sort((a, b) => {
      if (sortKey === "name") {
        const c = a.county_name.localeCompare(b.county_name);
        return asc ? c : -c;
      }
      return numCmp(sortVal(a), sortVal(b));
    });

  // ---- Town view: filter + sort over the ~565 municipalities --------------
  const countyFilter = sp.county ?? "";
  const townSortKey = sp.sort ?? "headroom";
  // Default direction ascending so the worst (most negative headroom) lead.
  const townAsc = sp.dir ? sp.dir === "asc" : true;
  const countyOptions = Array.from(new Set(munis.map((m) => m.county_name)))
    .sort()
    .map((c) => ({ value: c, label: c }));
  const townNumCmp = (a: number | null, b: number | null) => {
    if (a == null && b == null) return 0;
    if (a == null) return 1;
    if (b == null) return -1;
    return townAsc ? a - b : b - a;
  };
  const townSortVal = (m: MuniBurdenRow): number | null => {
    switch (townSortKey) {
      case "home":
        return m.home_price;
      case "required":
        return m.required_income;
      case "headroom":
        return m.headroom;
      default:
        return null;
    }
  };
  const visibleMunis = munis
    .filter((m) => (q ? m.muni_name.toLowerCase().includes(q) : true))
    .filter((m) => (countyFilter ? m.county_name === countyFilter : true))
    .sort((a, b) => {
      if (townSortKey === "name") {
        const c = a.muni_name.localeCompare(b.muni_name);
        return townAsc ? c : -c;
      }
      return townNumCmp(townSortVal(a), townSortVal(b));
    });

  return (
    <div className="space-y-8">
      <header className="space-y-3">
        <div className="flex flex-wrap items-baseline gap-3">
          <h1 className="text-3xl font-bold tracking-tight">Housing burden</h1>
          <span className="text-sm text-zinc-500">
            21 NJ counties &middot; FHFA HPI &times; ACS5 income, base{" "}
            <span className="font-mono">{baseYear}=100</span>
          </span>
          {freshness && (
            <FreshnessBadge headline={freshness} variant="compact" />
          )}
        </div>
        <p className="max-w-3xl text-sm text-zinc-600 dark:text-zinc-400">
          Each row compares home-price growth (FHFA HPI) to real wage growth
          (ACS5 median household income, deflated via CPI-U) since{" "}
          <span className="font-mono">{baseYear}</span>. Click any
          county to open its <strong>Collapse Curve</strong> &mdash; actual
          median income vs the income required to afford the median home in
          dollars, not abstract ratios.
        </p>
      </header>

      {err ? (
        <ErrorPanel error={err} />
      ) : rows.length === 0 ? (
        <EmptyState />
      ) : (
        <>
          {headline && headline.latest_year != null && (
            <HeadlineAnswerCard headline={headline} rows={rows} bands={bands} />
          )}

          <div className="space-y-3">
            <ViewToggle view={view} />

            {view === "towns" ? (
              <>
                <TableControls
                  search={{ param: "q", placeholder: "Town name…" }}
                  filters={[
                    { param: "county", label: "County", options: countyOptions },
                  ]}
                  sort={{
                    param: "sort",
                    options: TOWN_SORT_OPTIONS,
                    defaultValue: "headroom",
                  }}
                  direction={{ param: "dir", defaultValue: "asc" }}
                  shown={visibleMunis.length}
                  total={munis.length}
                />
                {visibleMunis.length === 0 ? (
                  <div className="rounded-xl border border-dashed border-zinc-300 dark:border-zinc-700 p-8 text-center text-sm text-zinc-500">
                    No towns match the current filters.
                  </div>
                ) : (
                  <TownTable rows={visibleMunis} />
                )}
              </>
            ) : (
              <>
                <TableControls
                  search={{ param: "q", placeholder: "County name…" }}
                  filters={[
                    { param: "tier", label: "Tier", options: tierOptions },
                  ]}
                  sort={{
                    param: "sort",
                    options: SORT_OPTIONS,
                    defaultValue: "burden",
                  }}
                  direction={{ param: "dir", defaultValue: "desc" }}
                  shown={visibleRows.length}
                  total={rows.length}
                />
                {visibleRows.length === 0 ? (
                  <div className="rounded-xl border border-dashed border-zinc-300 dark:border-zinc-700 p-8 text-center text-sm text-zinc-500">
                    No counties match the current filters.
                  </div>
                ) : (
                  <BurdenTable
                    rows={visibleRows}
                    detailMap={detailMap}
                    headroomByFips={headroomByFips}
                    bands={bands}
                  />
                )}
              </>
            )}
          </div>

          <PersonalizeCTA />

          <Methodology baseYear={baseYear} bands={bands} />
        </>
      )}
    </div>
  );
}

/* ---------------------------------------------------------------- */
/*  Headline answer (mirrors landing-page summary, scoped to table) */
/* ---------------------------------------------------------------- */

function HeadlineAnswerCard({
  headline,
  rows,
  bands,
}: {
  headline: NjAffordabilityHeadline;
  rows: CountyBurdenRow[];
  bands: TierBand[];
}) {
  const ratios = rows
    .map((r) => r.burden_ratio)
    .filter((x): x is number => x != null);
  const total = ratios.length;
  // Tier labels are version-stamped: count by severity_rank rather than
  // hardcoding cutoffs. Highest severity_rank = STRESS in v1.7.1.
  const labelByRatio = ratios.map((r) => burdenTier(r, bands).label);
  const stress = labelByRatio.filter((l) => l === "STRESS").length;
  const elevated = labelByRatio.filter((l) => l === "ELEVATED").length;
  const lagging = labelByRatio.filter((l) => l === "LAGGING").length;
  // Pull the live cutoffs from the bands themselves so the copy
  // tracks the calibration. STRESS is the highest band (largest band_ord).
  const stressBand = bands.find((b) => b.label === "STRESS");
  const elevatedBand = bands.find((b) => b.label === "ELEVATED");
  const stressPct =
    stressBand?.lower_bound != null
      ? Math.round((stressBand.lower_bound - 1) * 100)
      : null;
  const elevatedPct =
    elevatedBand?.lower_bound != null
      ? Math.round((elevatedBand.lower_bound - 1) * 100)
      : null;

  return (
    <section className="rounded-xl border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-6 grid grid-cols-1 md:grid-cols-3 gap-6">
      <div>
        <div className="text-xs uppercase tracking-wider text-zinc-500">
          Burden ratio
        </div>
        <p className="mt-2 text-sm text-zinc-700 dark:text-zinc-300">
          Housing has outpaced real wages by &ge;{stressPct ?? "—"}% in{" "}
          <span className="font-semibold text-rose-600 dark:text-rose-400">
            {stress}
          </span>{" "}
          of {total} NJ counties (the &quot;STRESS&quot; band, calibrated to
          historical p90), by &ge;{elevatedPct ?? "—"}% in{" "}
          <span className="font-semibold text-orange-600 dark:text-orange-400">
            {stress + elevated}
          </span>
          , and lagged wages in only{" "}
          <span className="font-semibold text-blue-600 dark:text-blue-400">
            {lagging}
          </span>
          .
        </p>
      </div>
      <div>
        <div className="text-xs uppercase tracking-wider text-zinc-500">
          Affordability gap ({headline.latest_year})
        </div>
        <p className="mt-2 text-sm text-zinc-700 dark:text-zinc-300">
          The median household can&rsquo;t afford the county median home in{" "}
          <span className="font-semibold text-rose-600 dark:text-rose-400">
            {headline.counties_unaffordable} of {headline.counties_with_data}
          </span>{" "}
          counties.
          {(() => {
            const u = headline.rows.filter(
              (r) => r.headroom != null && r.headroom < 0,
            );
            if (u.length === 0) return null;
            const avg =
              u.reduce((a, r) => a + (r.headroom ?? 0), 0) / u.length;
            return (
              <>
                {" "}
                Average shortfall there:{" "}
                <span className="font-semibold">
                  {fmtUsd(Math.abs(avg))}
                </span>{" "}
                of annual gross.
              </>
            );
          })()}
        </p>
      </div>
      <div>
        <div className="text-xs uppercase tracking-wider text-zinc-500">
          Avg required income (HUD)
        </div>
        <div className="mt-2">
          <div className="font-mono text-2xl font-bold">
            {headline.avg_required_income == null
              ? "—"
              : fmtUsd(headline.avg_required_income)}
          </div>
          <p className="mt-1 text-xs text-zinc-500">
            vs avg county median income{" "}
            <span className="font-mono">
              {headline.avg_median_income == null
                ? "—"
                : fmtUsd(headline.avg_median_income)}
            </span>{" "}
            &middot; PITI &divide; 0.30
          </p>
        </div>
      </div>
    </section>
  );
}

/* ---------------------------------------------------------------- */
/*  Table                                                           */
/* ---------------------------------------------------------------- */

function BurdenTable({
  rows,
  detailMap,
  headroomByFips,
  bands,
}: {
  rows: CountyBurdenRow[];
  detailMap: Map<string, Awaited<ReturnType<typeof getCountyDetail>>>;
  headroomByFips: Map<string, CountyHeadlineRow>;
  bands: TierBand[];
}) {
  return (
    <div className="overflow-x-auto rounded-xl border border-zinc-200 dark:border-zinc-800">
      <table className="min-w-full text-sm">
        <thead className="bg-zinc-50 dark:bg-zinc-900/60 text-left text-xs uppercase tracking-wider text-zinc-500">
          <tr>
            <th className="px-4 py-3">County</th>
            <th className="px-4 py-3 text-right">Burden ratio</th>
            <th className="px-4 py-3 text-right hidden md:table-cell">
              HPI &middot; Income
            </th>
            <th className="px-4 py-3 hidden lg:table-cell">Burden trend</th>
            <th className="px-4 py-3 text-right">
              Income gap (HUD)
            </th>
            <th className="px-4 py-3 text-right">
              <span className="sr-only">Drill in</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const tier = burdenTier(r.burden_ratio, bands);
            const detail = detailMap.get(r.county_id);
            const headroom = headroomByFips.get(r.county_fips);
            return (
              <tr
                key={r.county_id}
                className="border-t border-zinc-200 dark:border-zinc-800 hover:bg-zinc-50 dark:hover:bg-zinc-800/50"
              >
                <td className="px-4 py-3">
                  <Link
                    href={`/housing/${encodeURIComponent(r.county_id)}/collapse`}
                    className="group flex items-baseline gap-2"
                  >
                    <span className="font-semibold text-zinc-900 dark:text-zinc-100 group-hover:underline">
                      {r.county_name}
                    </span>
                    <span className="text-xs font-mono text-zinc-500">
                      {r.county_fips}
                    </span>
                  </Link>
                  <div className="mt-0.5 text-[11px] uppercase tracking-wider">
                    <span
                      className={`inline-flex items-center rounded-full px-1.5 py-0.5 font-bold ${tier.bg} ${tier.fg}`}
                      title={tier.description}
                    >
                      {tier.label}
                    </span>
                    <span className="ml-2 text-zinc-500">
                      {r.year_latest ?? "—"}
                    </span>
                  </div>
                </td>
                <td className="px-4 py-3 text-right">
                  <span className="font-mono text-base font-semibold">
                    {r.burden_ratio == null ? "—" : r.burden_ratio.toFixed(2)}
                  </span>
                </td>
                <td className="px-4 py-3 text-right hidden md:table-cell">
                  <span className="font-mono text-xs">
                    {fmtFactor(r.hpi_growth)}
                    <span className="mx-1 text-zinc-400">/</span>
                    {fmtFactor(r.income_growth)}
                  </span>
                </td>
                <td className="px-4 py-3 hidden lg:table-cell">
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
                <td className="px-4 py-3 text-right whitespace-nowrap">
                  {headroom?.headroom == null ? (
                    <span className="text-zinc-500 italic text-xs">
                      no substrate
                    </span>
                  ) : (
                    <span
                      className={`font-mono font-semibold ${
                        headroom.headroom < 0
                          ? "text-rose-600 dark:text-rose-400"
                          : "text-emerald-600 dark:text-emerald-400"
                      }`}
                      title={`${headroom.county_name}: median ${
                        headroom.median_income == null
                          ? "—"
                          : fmtUsd(headroom.median_income)
                      } vs HUD-required ${
                        headroom.required_income == null
                          ? "—"
                          : fmtUsd(headroom.required_income)
                      }`}
                    >
                      {signedUsd(headroom.headroom)}
                    </span>
                  )}
                </td>
                <td className="px-4 py-3 text-right">
                  <Link
                    href={`/housing/${encodeURIComponent(r.county_id)}/collapse`}
                    className="inline-flex items-center gap-1 text-xs font-semibold text-zinc-700 dark:text-zinc-300 hover:text-zinc-900 dark:hover:text-zinc-100"
                  >
                    <span className="hidden sm:inline">Collapse curve</span>
                    <span aria-hidden>&rarr;</span>
                  </Link>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/* ---------------------------------------------------------------- */
/*  Counties / Towns view toggle                                    */
/* ---------------------------------------------------------------- */

function ViewToggle({ view }: { view: "counties" | "towns" }) {
  const base =
    "px-3 py-1.5 text-sm font-medium transition-colors";
  const active =
    "bg-zinc-900 dark:bg-zinc-100 text-white dark:text-zinc-900";
  const idle =
    "bg-white dark:bg-zinc-900 text-zinc-700 dark:text-zinc-300 hover:bg-zinc-50 dark:hover:bg-zinc-800";
  return (
    <div className="inline-flex rounded-md border border-zinc-300 dark:border-zinc-700 overflow-hidden">
      <Link
        href="/housing"
        className={`${base} ${view === "counties" ? active : idle}`}
      >
        21 counties
      </Link>
      <Link
        href="/housing?view=towns"
        className={`${base} border-l border-zinc-300 dark:border-zinc-700 ${
          view === "towns" ? active : idle
        }`}
      >
        565 towns
      </Link>
    </div>
  );
}

/* ---------------------------------------------------------------- */
/*  Town-level affordability table                                  */
/* ---------------------------------------------------------------- */

function TownTable({ rows }: { rows: MuniBurdenRow[] }) {
  return (
    <div className="overflow-x-auto rounded-xl border border-zinc-200 dark:border-zinc-800">
      <table className="min-w-full text-sm">
        <thead className="bg-zinc-50 dark:bg-zinc-900/60 text-left text-xs uppercase tracking-wider text-zinc-500">
          <tr>
            <th className="px-4 py-3">Town</th>
            <th className="px-4 py-3 text-right">Avg home price</th>
            <th className="px-4 py-3 text-right hidden sm:table-cell">
              Required income (HUD)
            </th>
            <th className="px-4 py-3 text-right">Income gap (HUD)</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((m) => (
            <tr
              key={`${m.county_fips}-${m.muni_code}`}
              className="border-t border-zinc-200 dark:border-zinc-800 hover:bg-zinc-50 dark:hover:bg-zinc-800/50"
            >
              <td className="px-4 py-3">
                <div className="font-semibold text-zinc-900 dark:text-zinc-100">
                  {m.muni_name}
                </div>
                <div className="mt-0.5 text-[11px] text-zinc-500">
                  {m.county_name} County
                </div>
              </td>
              <td className="px-4 py-3 text-right font-mono">
                {m.home_price == null ? "—" : fmtUsd(m.home_price)}
              </td>
              <td className="px-4 py-3 text-right font-mono hidden sm:table-cell">
                {m.required_income == null ? "—" : fmtUsd(m.required_income)}
              </td>
              <td className="px-4 py-3 text-right whitespace-nowrap">
                {m.headroom == null ? (
                  <span className="text-zinc-500 italic text-xs">no data</span>
                ) : (
                  <span
                    className={`font-mono font-semibold ${
                      m.headroom < 0
                        ? "text-rose-600 dark:text-rose-400"
                        : "text-emerald-600 dark:text-emerald-400"
                    }`}
                    title={`${m.muni_name}: county median ${
                      m.median_income == null ? "—" : fmtUsd(m.median_income)
                    } vs HUD-required ${
                      m.required_income == null
                        ? "—"
                        : fmtUsd(m.required_income)
                    }`}
                  >
                    {signedUsd(m.headroom)}
                  </span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function fmtFactor(x: number | null): string {
  if (x == null) return "—";
  const pct = (x - 1) * 100;
  const sign = pct > 0 ? "+" : "";
  return `${x.toFixed(2)}\u00d7 ${sign}${pct.toFixed(0)}%`;
}

function signedUsd(n: number): string {
  if (!Number.isFinite(n)) return "—";
  const sign = n < 0 ? "\u2212" : n > 0 ? "+" : "";
  return `${sign}${fmtUsd(Math.abs(n))}`;
}

/* ---------------------------------------------------------------- */
/*  Lower-fold: personalize CTA + methodology                       */
/* ---------------------------------------------------------------- */

function PersonalizeCTA() {
  return (
    <section className="rounded-xl border border-rose-300 dark:border-rose-900 bg-rose-50 dark:bg-rose-950/30 p-6 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
      <div>
        <div className="text-xs uppercase tracking-wider text-rose-700 dark:text-rose-300">
          Personalize
        </div>
        <h2 className="mt-1 text-lg font-semibold">
          Run these numbers for your household.
        </h2>
        <p className="mt-1 text-sm text-zinc-700 dark:text-zinc-300 max-w-2xl">
          Same engine. Plug in your gross income, filing status, dependents,
          and other monthly debt &mdash; we compute every NJ county verdict
          (and every NJ town drill-down) for you.
        </p>
      </div>
      <Link
        href="/personalize"
        className="inline-flex items-center justify-center gap-2 rounded-lg bg-rose-600 hover:bg-rose-500 px-5 py-2.5 text-sm font-semibold text-white shadow-sm shadow-rose-900/20 whitespace-nowrap"
      >
        Open personalizer
        <span aria-hidden>&rarr;</span>
      </Link>
    </section>
  );
}

function ErrorPanel({ error }: { error: string }) {
  return (
    <div className="rounded-md bg-red-50 dark:bg-red-950 p-4 text-sm">
      <div className="font-medium text-red-800 dark:text-red-200">
        Database query failed.
      </div>
      <pre className="mt-2 overflow-x-auto rounded bg-red-100/60 dark:bg-red-900/40 p-2 text-xs">
        {error}
      </pre>
      <p className="mt-2 text-xs text-red-800 dark:text-red-200">
        This usually means the FHFA / ACS / CPI substrates are not loaded.
        From the repo root, run{" "}
        <code className="font-mono">nj-ingest-fhfa</code>,{" "}
        <code className="font-mono">nj-ingest-acs-income</code>, and{" "}
        <code className="font-mono">nj-ingest-cpi</code>.
      </p>
    </div>
  );
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

function Methodology({
  baseYear,
  bands,
}: {
  baseYear: number;
  bands: TierBand[];
}) {
  const formulaVersion = bands[0]?.formula_version ?? "—";
  return (
    <details className="rounded-xl border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-5 text-sm group no-marker">
      <summary className="font-semibold cursor-pointer flex items-center justify-between gap-2">
        How is the burden ratio computed?
        <span
          className="text-zinc-400 group-open:rotate-90 transition-transform"
          aria-hidden
        >
          &rsaquo;
        </span>
      </summary>
      <div className="mt-3 space-y-3 text-zinc-700 dark:text-zinc-300">
        <p>
          For each county we re-index two series so that{" "}
          <code className="font-mono">{baseYear}</code> = 100. The base year
          itself is loaded from{" "}
          <code className="font-mono">ref.platform_constants.burden_base_year</code>{" "}
          (no magic numbers in code).
        </p>
        <ul className="list-disc pl-5 space-y-1">
          <li>
            <strong>HPI growth</strong>: FHFA House Price Index for the
            county (purchase-only, all-transactions) divided by the same
            county&apos;s {baseYear} value.
          </li>
          <li>
            <strong>Real income growth</strong>: ACS 5-year median household
            income, deflated to {baseYear} dollars via CPI-U All
            Items, divided by the same county&apos;s {baseYear}{" "}
            real-dollar value.
          </li>
        </ul>
        <p>
          The <strong>burden ratio</strong> is{" "}
          <code className="font-mono">HPI growth &divide; real income growth</code>.
          The colored tier badge on each row classifies the ratio against
          empirically calibrated cutoffs from{" "}
          <code className="font-mono">ref.tier_bands</code> (formula version{" "}
          <span className="font-mono">{formulaVersion}</span>):
        </p>
        <ul className="list-disc pl-5 space-y-1 font-mono text-xs">
          {bands.map((b) => {
            const lo =
              b.lower_bound == null ? "(-inf" : `[${b.lower_bound.toFixed(2)}`;
            const hi =
              b.upper_bound == null ? "+inf)" : `${b.upper_bound.toFixed(2)})`;
            return (
              <li key={b.band_ord}>
                <span className="font-semibold">{b.label}</span> {lo}, {hi}
                <span className="ml-2 text-zinc-500 not-italic">
                  &mdash; {b.description}
                </span>
              </li>
            );
          })}
        </ul>
        <p className="text-xs text-zinc-500">
          Cutoffs are calibrated to historical NJ panel percentiles
          (315 (county, year) pairs 2010-2024, p75 / p90 anchors).
          See <code className="font-mono">db/seeds/015_tier_bands.sql</code>
          for the full citation per band.
        </p>
        <p>
          The <strong>income gap (HUD)</strong> column is the dollar version
          of the same question, computed for the latest year where every
          required substrate (DCA property tax + ACS5 income + FRED
          MORTGAGE30US + IRS / NJ tax brackets) is present:{" "}
          <code className="font-mono">
            median income &minus; (PITI on county avg home &divide; 0.30)
          </code>
          . Negative values mean the median household earns less than HUD&rsquo;s
          30%-of-gross threshold for that home. See{" "}
          <Link href="/about" className="underline">
            methodology
          </Link>{" "}
          for full citations.
        </p>
      </div>
    </details>
  );
}
