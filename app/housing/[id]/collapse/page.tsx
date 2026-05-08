import Link from "next/link";

import { CollapseCurve } from "@/components/CollapseCurve";
import { DisposableIncomeChart } from "@/components/DisposableIncomeChart";
import { isDbReachable } from "@/lib/db";
import {
  getCountyAffordabilityGap,
  getCountyDisposableIncome,
} from "@/lib/housing";

export const dynamic = "force-dynamic";
export const revalidate = 0;

interface RouteParams {
  id: string;
}

export const metadata = {
  title: "Collapse Curve — NJ Unchained",
  description:
    "Per-county time series of actual median household income vs the " +
    "income required to afford the median home at the HUD 30% threshold.",
};

export default async function CollapseCurvePage({
  params,
}: {
  params: Promise<RouteParams>;
}) {
  const { id: rawId } = await params;
  const reachable = await isDbReachable();
  if (!reachable.reachable) {
    return (
      <div className="rounded-md bg-amber-50 dark:bg-amber-950 p-4 text-sm text-amber-800 dark:text-amber-200">
        Database not reachable. Configure{" "}
        <code className="font-mono">NEON_DATABASE_URL</code>.
      </div>
    );
  }

  const id = decodeURIComponent(rawId);
  // Two parallel reads -- the affordability gap (Phase 2) and the
  // disposable-income trajectory + AEI (Phase 3). Both are single
  // round-trips and join on county_fips downstream, so we await
  // them concurrently.
  const [data, di] = await Promise.all([
    getCountyAffordabilityGap(id),
    getCountyDisposableIncome(id),
  ]);
  if (!data) {
    return (
      <div className="rounded-md border border-zinc-300 dark:border-zinc-700 p-6">
        <p className="font-semibold">County not found.</p>
        <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">
          <span className="font-mono">{id}</span> is not in{" "}
          <code className="font-mono">ref.county</code> with{" "}
          <code className="font-mono">state_code = &lsquo;NJ&rsquo;</code>.
        </p>
        <p className="mt-3 text-sm">
          <Link href="/housing" className="underline">
            ← Back to housing overview
          </Link>
        </p>
      </div>
    );
  }

  const points = data.series.map((p) => ({
    year: p.year,
    median_income: p.median_income_nominal,
    required_income: p.required_income_hud_30pct,
  }));
  const plottedYears = points
    .filter((p) => p.median_income != null && p.required_income != null)
    .map((p) => p.year);

  const headlineHeadroom = data.latest?.hud_headroom_dollars ?? null;
  const headlineRatio = data.latest?.hud_required_to_actual_ratio ?? null;
  const isUnaffordable = headlineHeadroom != null && headlineHeadroom < 0;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <Link
          href={`/housing/${encodeURIComponent(data.county_id)}`}
          className="text-sm underline underline-offset-4 text-zinc-600 dark:text-zinc-400"
        >
          ← Back to {data.county_name}
        </Link>
        <Link
          href="/housing"
          className="text-sm underline underline-offset-4 text-zinc-600 dark:text-zinc-400"
        >
          All counties
        </Link>
      </div>

      <header className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-5">
        <div className="text-xs uppercase tracking-wider text-zinc-500">
          Collapse Curve / county / fips {data.county_fips}
        </div>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight">
          {data.county_name}
        </h1>
        <p className="mt-2 text-sm text-zinc-600 dark:text-zinc-400">
          Actual median household income vs the income required to afford
          the median home in this county at the{" "}
          <span className="font-medium">HUD 30%-of-gross threshold</span>.
          Mortgage assumptions: 20% down, 30-year fixed at the year&apos;s
          Freddie Mac PMMS mean rate; 0.35% homeowners insurance; county
          property-tax rate from NJ DCA. Tax burden computed by the
          Phase-1 federal + NJ + FICA simulator for a representative{" "}
          {data.profile.filing_status.toUpperCase()} household with{" "}
          {data.profile.dependents} dependent(s) /{" "}
          {data.profile.qualifying_children} qualifying child(ren).
        </p>

        {data.latest != null ? (
          <dl className="mt-4 grid grid-cols-2 sm:grid-cols-4 gap-3 text-sm">
            <KV
              label="Latest year"
              value={String(data.latest_year)}
            />
            <KV
              label="Median income (actual)"
              value={fmtUsd(data.latest.median_income_nominal)}
              tone="info"
            />
            <KV
              label="Required income (HUD 30%)"
              value={fmtUsd(data.latest.required_income_hud_30pct)}
              tone="warn"
            />
            <KV
              label={isUnaffordable ? "Income shortfall" : "Income headroom"}
              value={
                headlineHeadroom == null
                  ? "—"
                  : `${headlineHeadroom < 0 ? "−" : "+"}${fmtUsd(Math.abs(headlineHeadroom))}`
              }
              tone={isUnaffordable ? "warn" : "ok"}
            />
          </dl>
        ) : (
          <div className="mt-4 rounded-md bg-amber-50 dark:bg-amber-950 p-3 text-xs text-amber-800 dark:text-amber-200">
            No fully-populated year yet for this county. The Collapse
            Curve renders only years for which DCA property tax + ACS5
            income + FRED rate + IRS/NJ tax tables all exist.
          </div>
        )}

        {/* Phase-3 affordability erosion stat. Renders as an auxiliary
            strip below the headline KV grid; honest about its anchor
            year (we auto-discover earliest non-NULL HBR year, NOT 1990,
            until pre-2009 income substrate is loaded). */}
        {di?.aei != null && (
          <div className="mt-4 rounded-md border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-950 p-3 text-sm">
            <div className="flex flex-col sm:flex-row sm:items-baseline sm:justify-between gap-2">
              <div>
                <span className="text-xs uppercase tracking-wider text-zinc-500">
                  Affordability Erosion Index
                </span>
                <span
                  className={`ml-2 font-mono text-lg ${
                    di.aei.aei >= 1.25
                      ? "text-amber-700 dark:text-amber-300 font-semibold"
                      : di.aei.aei >= 1.0
                        ? "text-zinc-900 dark:text-zinc-100 font-semibold"
                        : "text-emerald-700 dark:text-emerald-300 font-semibold"
                  }`}
                >
                  {di.aei.aei.toFixed(2)}×
                </span>
              </div>
              <div className="text-xs text-zinc-500 font-mono">
                HBR<sub>{di.aei.latest_year}</sub> /
                HBR<sub>{di.aei.anchor_year}</sub> = {di.aei.latest_hbr.toFixed(3)}
                {" / "}
                {di.aei.anchor_hbr.toFixed(3)}
              </div>
            </div>
            <p className="mt-1 text-xs text-zinc-500 leading-snug">
              Housing today is{" "}
              <span className="font-semibold">
                {di.aei.aei.toFixed(2)}×
              </span>{" "}
              as burdensome as in {di.aei.anchor_year} (the earliest
              year with all four substrates for this county). Spec
              §5.5 calls for a 1990 anchor; we use the earliest
              available year and label it explicitly rather than
              hardcoding 1990 (which requires Decennial 2000/1990
              income substrate not yet loaded).
            </p>
          </div>
        )}
      </header>

      <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-5">
        <div className="mb-1 flex items-baseline justify-between">
          <h2 className="font-medium">The Collapse Curve</h2>
          <div className="text-xs text-zinc-500">
            <span className="font-mono">
              formula version: {data.formula_version}
            </span>
          </div>
        </div>
        <p className="text-xs text-zinc-500 dark:text-zinc-500 mb-3">
          When the red line (required income) climbs above the blue line
          (actual median), housing has become structurally unaffordable
          for the median household. The shaded area is the dollar gap.
        </p>
        <div className="overflow-x-auto">
          <CollapseCurve
            points={points}
            width={720}
            height={380}
            title={`${data.county_name} Collapse Curve`}
          />
        </div>
        {plottedYears.length > 0 && (
          <p className="mt-2 text-xs text-zinc-500 font-mono">
            plotted years: {plottedYears.join(", ")}
          </p>
        )}
      </section>

      {data.latest != null && (
        <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-5">
          <h2 className="font-medium mb-3">All three required-income metrics</h2>
          <p className="text-xs text-zinc-500 mb-3">
            The curve plots the HUD definition (the cited, comparable
            standard). Two stricter definitions are also computed and
            shown here for the same household profile.
          </p>
          <dl className="grid grid-cols-1 sm:grid-cols-3 gap-3 text-sm">
            <Metric
              label="HUD 30% of gross"
              value={fmtUsd(data.latest.required_income_hud_30pct)}
              note="PITI ÷ 0.30. The published HUD CHAS cost-burden definition."
            />
            <Metric
              label="Lender style (30% of take-home)"
              value={fmtUsd(data.latest.required_income_post_tax_30pct)}
              note="PITI ≤ 30% of (gross − federal − NJ − FICA). What underwriters actually model."
            />
            <Metric
              label="Strict (housing + tax ≤ 30% of gross)"
              value={
                data.latest.required_income_full_burden_30pct == null
                  ? "Unreachable"
                  : fmtUsd(data.latest.required_income_full_burden_30pct)
              }
              note={
                data.latest.required_income_full_burden_30pct == null
                  ? "No income makes housing AND taxes both fit in 30% of gross. Combined federal + NJ + FICA marginal rate exceeds 30% before PITI/G falls below 30%. The crisis as math."
                  : "Solves PITI + tax(G) ≤ 0.30 × G. A bisection over the tax engine."
              }
              tone={
                data.latest.required_income_full_burden_30pct == null
                  ? "warn"
                  : undefined
              }
            />
          </dl>
        </section>
      )}

      {/* Phase-3 Disposable Income Trajectory.
          The CollapseCurve answers "can the median household afford
          the median home?" The DI trajectory answers the followup:
          "after they pay for housing AND taxes, what's left over?"
          Plotted in real (CPI-deflated) dollars so the line isolates
          true purchasing-power erosion from headline inflation. */}
      {di != null && (
        <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-5">
          <div className="mb-1 flex items-baseline justify-between">
            <h2 className="font-medium">
              Disposable income trajectory
            </h2>
            <div className="text-xs text-zinc-500">
              <span className="font-mono">
                formula version: {di.formula_version}
              </span>
            </div>
          </div>
          <p className="text-xs text-zinc-500 dark:text-zinc-500 mb-3">
            What the median household has left over after paying
            federal + NJ + FICA tax and PITI on the county&apos;s
            average residential home. Real series is deflated to{" "}
            <span className="font-mono">{di.real_dollars_base_year ?? "—"}</span>{" "}
            dollars via the BLS CPI-U deflator
            (idea §3.4 calls for 2026 baseline; we use the latest
            available CPI year and label it explicitly until BLS
            publishes 2026&apos;s annual M13).
          </p>
          <div className="overflow-x-auto">
            <DisposableIncomeChart
              points={di.series.map((p) => ({
                year: p.year,
                di_nominal: p.di_nominal,
                di_real: p.di_real,
              }))}
              realDollarsBaseYear={di.real_dollars_base_year}
              width={720}
              height={320}
              title={`${data.county_name} Disposable Income Trajectory`}
            />
          </div>
          {di.series.filter((p) => p.di_real != null).length > 0 && (
            <p className="mt-2 text-xs text-zinc-500 font-mono">
              plotted years:{" "}
              {di.series
                .filter((p) => p.di_real != null)
                .map((p) => p.year)
                .join(", ")}
            </p>
          )}
        </section>
      )}

      <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4 text-sm">
        <h2 className="font-medium">Data substrate (what's loaded)</h2>
        <p className="mt-2 text-xs text-zinc-500">
          Per the verifiable-data rule, every component below has a
          year range and we don&apos;t fake it for years we don&apos;t
          have.
        </p>
        <dl className="mt-3 grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
          <Coverage
            label="DCA property tax"
            range={range(data.coverage.dca_year_min, data.coverage.dca_year_max)}
          />
          <Coverage
            label="ACS5 median income"
            range={range(data.coverage.acs_year_min, data.coverage.acs_year_max)}
          />
          <Coverage
            label="FRED 30-yr mortgage"
            range={range(
              data.coverage.fred_year_min,
              data.coverage.fred_year_max,
            )}
          />
          <Coverage
            label="Tax tables (IRS + NJ)"
            range={
              data.coverage.tax_seeded_years.length === 0
                ? "—"
                : `${data.coverage.tax_seeded_years[0]}–${data.coverage.tax_seeded_years.at(-1)} (${data.coverage.tax_seeded_years.length} yrs)`
            }
          />
        </dl>
        <p className="mt-3 text-xs text-zinc-500">
          The chart can only plot a year when ALL FOUR substrates have
          data for it. Currently:{" "}
          <span className="font-mono">
            {data.coverage.affordability_years.length === 0
              ? "no joined years yet"
              : `${data.coverage.affordability_years.length} year(s) plottable`}
          </span>
          . Earlier years light up as the historical IRS / NJ tax tables
          for 2010–2022 are seeded (Phase 1 follow-up data work).
        </p>
      </section>
    </div>
  );
}

function range(min: number | null, max: number | null): string {
  if (min == null || max == null) return "—";
  if (min === max) return String(min);
  return `${min}–${max}`;
}

function Coverage({ label, range }: { label: string; range: string }) {
  return (
    <div>
      <div className="uppercase tracking-wider text-zinc-500">{label}</div>
      <div className="mt-0.5 font-mono">{range}</div>
    </div>
  );
}

function Metric({
  label,
  value,
  note,
  tone,
}: {
  label: string;
  value: string;
  note: string;
  tone?: "warn";
}) {
  const valueClass =
    tone === "warn"
      ? "text-amber-700 dark:text-amber-300 font-semibold"
      : "font-semibold";
  return (
    <div className="rounded-md border border-zinc-200 dark:border-zinc-800 p-3">
      <div className="text-xs uppercase tracking-wider text-zinc-500">
        {label}
      </div>
      <div className={`mt-1 font-mono text-lg ${valueClass}`}>{value}</div>
      <p className="mt-1 text-[11px] text-zinc-500 leading-snug">{note}</p>
    </div>
  );
}

function KV({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "warn" | "ok" | "info";
}) {
  let valueClass = "font-mono";
  if (tone === "warn")
    valueClass =
      "font-mono text-amber-700 dark:text-amber-300 font-semibold";
  if (tone === "ok")
    valueClass =
      "font-mono text-emerald-700 dark:text-emerald-300 font-semibold";
  if (tone === "info") valueClass = "font-mono text-blue-700 dark:text-blue-300";
  return (
    <div>
      <div className="text-xs uppercase tracking-wider text-zinc-500">
        {label}
      </div>
      <div className={`mt-0.5 ${valueClass}`}>{value}</div>
    </div>
  );
}

function fmtUsd(n: number | null): string {
  if (n == null) return "—";
  return `$${Math.round(n).toLocaleString("en-US")}`;
}
