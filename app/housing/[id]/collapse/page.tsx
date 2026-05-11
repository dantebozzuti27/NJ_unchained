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

type Lens = "nominal" | "real";

/**
 * Parse the `?lens=` query string into our discriminated lens type.
 * Defaults to "real" because the substrate-honest narrative for a
 * "collapse curve" is the CPI-deflated lens: nominal-dollar plots
 * understate the structural-affordability erosion by letting headline
 * inflation push both lines up in parallel. We surface both lenses
 * via a toggle so the reader can audit the deflation arithmetic.
 */
function parseLens(raw: string | string[] | undefined): Lens {
  const v = Array.isArray(raw) ? raw[0] : raw;
  return v === "nominal" ? "nominal" : "real";
}

export const metadata = {
  title: "Collapse Curve — NJ Unchained",
  description:
    "Per-county time series of actual median household income vs the " +
    "income required to afford the median home at the HUD 30% threshold.",
};

export default async function CollapseCurvePage({
  params,
  searchParams,
}: {
  params: Promise<RouteParams>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const [{ id: rawId }, sp] = await Promise.all([params, searchParams]);
  const lens: Lens = parseLens(sp.lens);
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

  // The real-dollar lens is only viable when the CPI substrate is loaded
  // (mig 085's f_real_dollar_base_year returns NULL on empty CPI).
  // If the caller asked for ?lens=real but we cannot deliver, we fall
  // back to nominal silently rather than mislabel real-dollar values.
  const realDollarsBaseYear = data.real_dollar_base_year;
  const effectiveLens: Lens =
    lens === "real" && realDollarsBaseYear != null ? "real" : "nominal";

  const points = data.series.map((p) =>
    effectiveLens === "real"
      ? {
          year: p.year,
          median_income: p.median_income_real,
          required_income: p.required_income_hud_30pct_real,
        }
      : {
          year: p.year,
          median_income: p.median_income_nominal,
          required_income: p.required_income_hud_30pct,
        },
  );
  const plottedYears = points
    .filter((p) => p.median_income != null && p.required_income != null)
    .map((p) => p.year);

  // Headline numbers also pivot on the active lens. The headroom flag
  // (color / "shortfall" copy) is computed from the active lens to keep
  // the narrative coherent: if the chart shows the real-dollar gap,
  // the headline metric must too.
  const headlineHeadroom =
    effectiveLens === "real"
      ? data.latest?.hud_headroom_dollars_real ?? null
      : data.latest?.hud_headroom_dollars ?? null;
  const isUnaffordable = headlineHeadroom != null && headlineHeadroom < 0;
  const headlineMedianIncome =
    effectiveLens === "real"
      ? data.latest?.median_income_real ?? null
      : data.latest?.median_income_nominal ?? null;
  const headlineRequiredIncome =
    effectiveLens === "real"
      ? data.latest?.required_income_hud_30pct_real ?? null
      : data.latest?.required_income_hud_30pct ?? null;
  const lensLabel =
    effectiveLens === "real" && realDollarsBaseYear != null
      ? `${realDollarsBaseYear} dollars`
      : "nominal dollars";

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

        {/* Lens switcher: nominal vs real-dollar (CPI-deflated). Server-
            rendered links so there is zero client-side JS; the
            ?lens=... param round-trips the choice. The active button is
            visually distinct; the inactive button is a link. */}
        <LensSwitcher
          countyId={data.county_id}
          activeLens={effectiveLens}
          realDollarBaseYear={realDollarsBaseYear}
        />

        {data.latest != null ? (
          <dl className="mt-4 grid grid-cols-2 sm:grid-cols-4 gap-3 text-sm">
            <KV
              label="Latest year"
              value={String(data.latest_year)}
            />
            <KV
              label={`Median income (actual, ${lensLabel})`}
              value={fmtUsd(headlineMedianIncome)}
              tone="info"
            />
            <KV
              label={`Required income (HUD 30%, ${lensLabel})`}
              value={fmtUsd(headlineRequiredIncome)}
              tone="warn"
            />
            <KV
              label={`${isUnaffordable ? "Income shortfall" : "Income headroom"} (${lensLabel})`}
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
          <h2 className="font-medium">
            The Collapse Curve{" "}
            <span className="text-xs font-normal text-zinc-500">
              ({lensLabel})
            </span>
          </h2>
          <div className="text-xs text-zinc-500">
            <span className="font-mono">
              formula version: {data.formula_version}
            </span>
          </div>
        </div>
        <p className="text-xs text-zinc-500 dark:text-zinc-500 mb-3">
          When the red line (required income) climbs above the blue line
          (actual median), housing has become structurally unaffordable
          for the median household. The shaded area is the dollar gap.{" "}
          {effectiveLens === "real" ? (
            <span>
              All values are CPI-deflated to{" "}
              <span className="font-mono">{realDollarsBaseYear}</span>{" "}
              dollars via the BLS CPI-U headline annual series so
              cross-year comparisons isolate structural-affordability
              erosion from headline inflation.
            </span>
          ) : (
            <span>
              Values are in nominal year-of-observation dollars; each
              year&apos;s pair is internally comparable, but cross-year
              trends conflate housing-cost growth with general
              inflation. Flip to the real-dollar lens above to isolate
              the structural component.
            </span>
          )}
        </p>
        <div className="overflow-x-auto">
          <CollapseCurve
            points={points}
            width={720}
            height={380}
            title={`${data.county_name} Collapse Curve (${lensLabel})`}
            lens={effectiveLens}
            realDollarsBaseYear={realDollarsBaseYear}
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
          <h2 className="font-medium mb-3">
            All three required-income metrics{" "}
            <span className="text-xs font-normal text-zinc-500">
              ({lensLabel})
            </span>
          </h2>
          <p className="text-xs text-zinc-500 mb-3">
            The curve plots the HUD definition (the cited, comparable
            standard). Two stricter definitions are also computed and
            shown here for the same household profile. All three pivot
            on the active dollar lens.
          </p>
          <dl className="grid grid-cols-1 sm:grid-cols-3 gap-3 text-sm">
            <Metric
              label="HUD 30% of gross"
              value={fmtUsd(
                effectiveLens === "real"
                  ? data.latest.required_income_hud_30pct_real
                  : data.latest.required_income_hud_30pct,
              )}
              note="PITI ÷ 0.30. The published HUD CHAS cost-burden definition."
            />
            <Metric
              label="Lender style (30% of take-home)"
              value={fmtUsd(
                effectiveLens === "real"
                  ? data.latest.required_income_post_tax_30pct_real
                  : data.latest.required_income_post_tax_30pct,
              )}
              note="PITI ≤ 30% of (gross − federal − NJ − FICA). What underwriters actually model."
            />
            <Metric
              label="Strict (housing + tax ≤ 30% of gross)"
              value={
                (effectiveLens === "real"
                  ? data.latest.required_income_full_burden_30pct_real
                  : data.latest.required_income_full_burden_30pct) == null
                  ? "Unreachable"
                  : fmtUsd(
                      effectiveLens === "real"
                        ? data.latest.required_income_full_burden_30pct_real
                        : data.latest.required_income_full_burden_30pct,
                    )
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

function fmtUsd(n: number | null | undefined): string {
  if (n == null) return "—";
  return `$${Math.round(n).toLocaleString("en-US")}`;
}

/**
 * Two-state lens switcher.
 *
 * Server-rendered <Link>s so the toggle round-trips through the URL
 * (?lens=nominal | ?lens=real), zero client JS. The active state is a
 * styled div; the inactive is a link. This matches the pattern used
 * by the `/risk?scope=...` switcher and keeps the collapse page
 * RSC-friendly.
 *
 * When `realDollarBaseYear` is NULL (CPI substrate not loaded), the
 * real-dollar option is rendered as a disabled-looking ghost button
 * that explains the substrate gap inline -- substrate-honest, the UI
 * tells the reader exactly why the lens is unavailable.
 */
function LensSwitcher({
  countyId,
  activeLens,
  realDollarBaseYear,
}: {
  countyId: string;
  activeLens: Lens;
  realDollarBaseYear: number | null;
}) {
  const base = `/housing/${encodeURIComponent(countyId)}/collapse`;
  const realAvailable = realDollarBaseYear != null;
  return (
    <div
      className="mt-4 flex flex-wrap items-center gap-2 text-xs"
      role="group"
      aria-label="Dollar lens"
    >
      <span className="uppercase tracking-wider text-zinc-500">
        Dollar lens:
      </span>
      <LensButton
        active={activeLens === "real"}
        disabled={!realAvailable}
        href={`${base}?lens=real`}
        label={
          realAvailable
            ? `Real (${realDollarBaseYear} dollars)`
            : "Real (CPI substrate unloaded)"
        }
      />
      <LensButton
        active={activeLens === "nominal"}
        href={`${base}?lens=nominal`}
        label="Nominal (year-of-observation dollars)"
      />
    </div>
  );
}

function LensButton({
  active,
  disabled = false,
  href,
  label,
}: {
  active: boolean;
  disabled?: boolean;
  href: string;
  label: string;
}) {
  if (active) {
    return (
      <span className="rounded-md border border-zinc-900 bg-zinc-900 px-2 py-1 font-mono text-white dark:border-zinc-100 dark:bg-zinc-100 dark:text-zinc-900">
        {label}
      </span>
    );
  }
  if (disabled) {
    return (
      <span className="rounded-md border border-dashed border-zinc-300 px-2 py-1 font-mono text-zinc-400 dark:border-zinc-700 dark:text-zinc-600">
        {label}
      </span>
    );
  }
  return (
    <Link
      href={href}
      className="rounded-md border border-zinc-300 px-2 py-1 font-mono text-zinc-700 hover:border-zinc-500 dark:border-zinc-700 dark:text-zinc-300 dark:hover:border-zinc-500"
    >
      {label}
    </Link>
  );
}
