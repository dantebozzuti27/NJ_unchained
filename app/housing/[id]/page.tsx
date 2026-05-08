import Link from "next/link";

import { CrossSourceAnnotationCallout } from "@/components/CrossSourceAnnotationCallout";
import { FreshnessBadge } from "@/components/FreshnessBadge";
import { Sparkline } from "@/components/Sparkline";
import { getCrossSourceAnnotation } from "@/lib/cross-source-annotations";
import { isDbReachable } from "@/lib/db";
import { getPlatformFreshnessHeadline } from "@/lib/freshness";
import {
  burdenTier,
  getBurdenTierBands,
  getCountyDetail,
} from "@/lib/housing";

export const dynamic = "force-dynamic";
export const revalidate = 0;

interface RouteParams {
  id: string;
}

export default async function CountyDetailPage({
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
  const [detail, bands, freshness] = await Promise.all([
    getCountyDetail(id),
    getBurdenTierBands(),
    // Freshness is best-effort; missing migration must not break the page.
    getPlatformFreshnessHeadline().catch(() => null),
  ]);
  // Annotation fetch is dependent on the cross_source slice of detail;
  // best-effort so a missing mig 084 / seed 017 cannot break the page.
  const annotation =
    detail?.cross_source != null
      ? await getCrossSourceAnnotation(
          detail.county_fips,
          detail.cross_source.year,
        ).catch(() => null)
      : null;
  if (!detail) {
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

  const tier = burdenTier(detail.current.burden_ratio, bands);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <Link
          href="/housing"
          className="text-sm underline underline-offset-4 text-zinc-600 dark:text-zinc-400"
        >
          ← Back to housing overview
        </Link>
        <Link
          href={`/housing/${encodeURIComponent(detail.county_id)}/collapse`}
          className="rounded-md border border-red-300 dark:border-red-800 bg-red-50 dark:bg-red-950 px-3 py-1.5 text-xs font-medium text-red-800 dark:text-red-200 hover:bg-red-100 dark:hover:bg-red-900 transition-colors"
          title="The Collapse Curve: actual median income vs income required to afford the median home"
        >
          View Collapse Curve →
        </Link>
      </div>

      <header className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-5">
        <div className="flex flex-wrap gap-3 items-baseline justify-between">
          <div>
            <div className="text-xs uppercase tracking-wider text-zinc-500">
              county / fips {detail.county_fips} / base year {detail.base_year}
            </div>
            <h1 className="mt-1 text-2xl font-semibold tracking-tight">
              {detail.county_name}
            </h1>
            <div className="mt-0.5 font-mono text-xs text-zinc-500">
              {detail.county_id}
            </div>
          </div>
          <div className="flex items-center gap-3">
            <span
              className={`rounded-full px-3 py-1 text-xs font-bold ${tier.bg} ${tier.fg}`}
              title={tier.description}
            >
              {tier.label}
            </span>
            <div className="text-right">
              <div className="font-mono text-3xl font-bold leading-none">
                {detail.current.burden_ratio == null
                  ? "—"
                  : detail.current.burden_ratio.toFixed(2)}
              </div>
              <div className="mt-0.5 text-xs uppercase tracking-wider text-zinc-500">
                Burden ratio ({detail.current.year ?? "—"})
              </div>
            </div>
          </div>
        </div>

        <dl className="mt-4 grid grid-cols-2 sm:grid-cols-4 gap-3 text-sm">
          <KV
            label="HPI growth (vs base)"
            value={fmtFactor(detail.current.hpi_growth)}
          />
          <KV
            label="Real income growth"
            value={fmtFactor(detail.current.income_growth)}
          />
          <KV
            label="Real median income (latest)"
            value={fmtUsd(detail.current.median_income_real)}
          />
          <KV
            label="Years observed"
            value={
              detail.burden_series.length === 0
                ? "—"
                : `${detail.burden_series.length} (${detail.burden_series[0].year}–${detail.burden_series.at(-1)!.year})`
            }
          />
        </dl>
      </header>

      <section className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <ChartCard
          title="Home-price index (FHFA)"
          subtitle={`FHFA HPI repeat-sales, ${detail.base_year}=100`}
          color="text-orange-700 dark:text-orange-300"
          series={detail.hpi_series}
        />
        <ChartCard
          title="Home-price index (Zillow)"
          subtitle={`Zillow ZHVI mid-tier SFR+condo, ${detail.base_year}=100`}
          color="text-purple-700 dark:text-purple-300"
          series={detail.zhvi_series}
        />
        <ChartCard
          title="Real wage index"
          subtitle={`ACS5 median income (CPI-deflated), ${detail.base_year}=100`}
          color="text-blue-700 dark:text-blue-300"
          series={detail.income_series_real}
        />
        <ChartCard
          title="Burden ratio"
          subtitle={`HPI \u00f7 real income, ${detail.base_year}=1.0`}
          color={tier.fg}
          series={detail.burden_series}
        />
      </section>

      {detail.cross_source != null && (
        <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4 text-sm">
          <h2 className="font-medium">Methodology cross-check (FHFA vs Zillow)</h2>
          <p className="mt-2 text-zinc-700 dark:text-zinc-300">
            Two independent housing indices, both re-indexed to{" "}
            <span className="font-mono">{detail.base_year}=100</span>. FHFA
            HPI is a repeat-sales index controlling for compositional change
            in the housing stock; Zillow ZHVI is a smoothed, seasonally-
            adjusted typical-home-value estimate built from the full
            transaction record plus listings. They should agree within a
            few index points except in regimes where one methodology
            captures price moves the other lags (Zillow leads in fast-
            moving markets; FHFA lags but is more transaction-anchored).
          </p>
          <dl className="mt-3 grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
            <KV
              label={`FHFA index (${detail.cross_source.year})`}
              value={detail.cross_source.fhfa_indexed.toFixed(1)}
            />
            <KV
              label={`Zillow index (${detail.cross_source.year})`}
              value={detail.cross_source.zhvi_indexed.toFixed(1)}
            />
            <KV
              label="Divergence (index pts)"
              value={`${detail.cross_source.divergence_indexed_points >= 0 ? "+" : ""}${detail.cross_source.divergence_indexed_points.toFixed(1)}`}
            />
            <KV
              label="Divergence (% of FHFA)"
              value={`${detail.cross_source.divergence_pct_of_fhfa >= 0 ? "+" : ""}${(detail.cross_source.divergence_pct_of_fhfa * 100).toFixed(2)}%`}
            />
          </dl>
          <p className="mt-3 text-xs text-zinc-500">
            <span className="font-mono">
              divergence_pct_of_fhfa = (zhvi_indexed &minus; fhfa_indexed) / fhfa_indexed
            </span>
            . Asset-check thresholds (calibrated on 546 historical NJ
            (county, year) pairs back to 2000): WARN at &gt;12% absolute,
            ERROR at &gt;20%, EXCEPT for documented methodology causes
            recorded in{" "}
            <code className="font-mono">
              ref.cross_source_divergence_known_causes
            </code>{" "}
            (Phase 7b annotations). See spec &sect;8.1 cross-source
            validation.
          </p>
          {annotation && (
            <CrossSourceAnnotationCallout
              annotation={annotation}
              year={detail.cross_source.year}
            />
          )}
        </section>
      )}

      {freshness && <FreshnessBadge headline={freshness} variant="detail" />}

      <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4 text-sm">
        <h2 className="font-medium">Reading the chart</h2>
        <p className="mt-2 text-zinc-700 dark:text-zinc-300">
          The burden chart starts at exactly 100 by construction
          (HPI/income at the base year is 1.0). When it climbs above 100,
          home-price growth has outpaced real wage growth in this county;
          when it dips below, wages are catching up. The dashed reference
          line is the &ldquo;break-even&rdquo; level (100 = parity with the
          base year). The two home-price index cards above (FHFA, Zillow)
          are independent methodologies; the cross-check section shows
          how much they agree for this county.
        </p>
      </section>
    </div>
  );
}

function ChartCard({
  title,
  subtitle,
  color,
  series,
}: {
  title: string;
  subtitle: string;
  color: string;
  series: { year: number; indexed: number }[];
}) {
  const latest = series.at(-1);
  return (
    <div className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
      <div className="flex items-baseline justify-between">
        <div>
          <div className="text-xs uppercase tracking-wider text-zinc-500">
            {title}
          </div>
          <div className="text-xs text-zinc-500">{subtitle}</div>
        </div>
        <div className="text-right">
          <div className="font-mono text-lg font-semibold">
            {latest ? latest.indexed.toFixed(1) : "—"}
          </div>
          <div className="text-[10px] text-zinc-500">{latest?.year ?? "—"}</div>
        </div>
      </div>
      <div className={`mt-3 ${color}`}>
        <Sparkline
          points={series}
          width={300}
          height={80}
          baseline={100}
          showAxisYears={true}
          title={`${title} ${series[0]?.year ?? ""}–${series.at(-1)?.year ?? ""}`}
        />
      </div>
    </div>
  );
}

function KV({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-xs uppercase tracking-wider text-zinc-500">
        {label}
      </div>
      <div className="mt-0.5 font-mono">{value}</div>
    </div>
  );
}

function fmtFactor(x: number | null): string {
  if (x == null) return "—";
  const pct = (x - 1) * 100;
  const sign = pct > 0 ? "+" : "";
  return `${x.toFixed(2)}× (${sign}${pct.toFixed(0)}%)`;
}

function fmtUsd(n: number | null): string {
  if (n == null) return "—";
  return `$${Math.round(n).toLocaleString("en-US")}`;
}
