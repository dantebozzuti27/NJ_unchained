import Link from "next/link";

import { isDbReachable } from "@/lib/db";
import { fmtUsd } from "@/lib/format";
import {
  type CountyHeadlineRow,
  type NjAffordabilityHeadline,
  getNjAffordabilityHeadline,
} from "@/lib/housing";

export const dynamic = "force-dynamic";
export const revalidate = 0;

const FORMULA_VERSION = "1.2.0-affordability-engine-v1";

export default async function HomePage() {
  const reachable = await isDbReachable();

  let headline: NjAffordabilityHeadline | null = null;
  let queryError: string | null = null;
  if (reachable.reachable) {
    try {
      headline = await getNjAffordabilityHeadline();
    } catch (e) {
      queryError = e instanceof Error ? e.message : String(e);
    }
  }

  return (
    <div className="space-y-10">
      <Hero headline={headline} />

      {!reachable.reachable ? (
        <DbDownNotice error={reachable.error} />
      ) : queryError ? (
        <QueryErrorNotice error={queryError} />
      ) : headline && headline.latest_year != null ? (
        <>
          <CountyExtremesCard headline={headline} />
          <SecondaryCallouts />
        </>
      ) : (
        <SubstrateEmptyNotice />
      )}
    </div>
  );
}

/* ---------------------------------------------------------------- */
/*  Hero                                                            */
/* ---------------------------------------------------------------- */

function Hero({ headline }: { headline: NjAffordabilityHeadline | null }) {
  const hasData = headline != null && headline.latest_year != null;
  const year = headline?.latest_year ?? null;
  const unaffordable = headline?.counties_unaffordable ?? null;
  const total = headline?.counties_with_data ?? headline?.total_counties ?? 21;

  const unaffordableRows =
    headline?.rows.filter(
      (r) => r.headroom != null && r.headroom < 0,
    ) ?? [];
  const avgGapAmongUnaffordable =
    unaffordableRows.length === 0
      ? null
      : unaffordableRows.reduce((acc, r) => acc + (r.headroom ?? 0), 0) /
        unaffordableRows.length;
  const worstRow = unaffordableRows[0] ?? null;

  return (
    <section className="relative overflow-hidden rounded-2xl border border-zinc-200 dark:border-zinc-800 bg-gradient-to-br from-zinc-50 via-white to-zinc-100 dark:from-zinc-950 dark:via-zinc-900 dark:to-black p-8 sm:p-12">
      <div className="max-w-3xl space-y-6">
        <span className="inline-flex items-center gap-2 rounded-full border border-rose-300 dark:border-rose-800 bg-rose-50 dark:bg-rose-950/40 px-3 py-1 text-xs font-semibold uppercase tracking-wider text-rose-800 dark:text-rose-200">
          <span className="h-1.5 w-1.5 rounded-full bg-rose-500" />
          New Jersey housing &amp; civic-integrity screener
        </span>

        <h1 className="text-4xl sm:text-5xl font-bold tracking-tight leading-tight">
          {hasData && unaffordable != null ? (
            <>
              In <span className="text-rose-600 dark:text-rose-400">{year}</span>,
              the median household in{" "}
              <span className="text-rose-600 dark:text-rose-400">
                {unaffordable} of {total}
              </span>{" "}
              NJ counties can&rsquo;t afford the county&rsquo;s median home.
            </>
          ) : (
            <>How far has middle-class NJ been priced out of itself?</>
          )}
        </h1>

        <p className="max-w-2xl text-lg text-zinc-600 dark:text-zinc-400">
          {hasData && avgGapAmongUnaffordable != null ? (
            <>
              Across those counties, households fall short by an average of{" "}
              <strong className="text-zinc-900 dark:text-zinc-100">
                {fmtUsd(Math.abs(avgGapAmongUnaffordable))}
              </strong>{" "}
              of annual gross income
              {worstRow && worstRow.headroom != null ? (
                <>
                  {" "}&mdash; up to{" "}
                  <strong className="text-zinc-900 dark:text-zinc-100">
                    {fmtUsd(Math.abs(worstRow.headroom))}
                  </strong>{" "}
                  in {worstRow.county_name}
                </>
              ) : null}
              . We compute every figure straight from the published substrate
              &mdash; FHFA, ACS, FRED, NJ DCA, IRS &amp; NJ Treasury &mdash;
              per the spec&rsquo;s headline metric (&sect;5.4). Every number
              links to its source.
            </>
          ) : (
            <>
              Two open-data screeners on one platform: a housing-affordability
              tracker that compares home prices and tax-adjusted disposable
              income county by county, and a civic-integrity engine that ranks
              political and federal-procurement entities by cross-source risk.
              Every figure links to its source.
            </>
          )}
        </p>

        <div className="flex flex-wrap gap-3 pt-2">
          <Link
            href="/personalize"
            className="inline-flex items-center gap-2 rounded-lg bg-rose-600 hover:bg-rose-500 px-5 py-2.5 text-sm font-semibold text-white shadow-sm shadow-rose-900/20"
          >
            Run my numbers
            <span aria-hidden>&rarr;</span>
          </Link>
          <Link
            href="/housing"
            className="inline-flex items-center gap-2 rounded-lg border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 px-5 py-2.5 text-sm font-semibold hover:bg-zinc-100 dark:hover:bg-zinc-800"
          >
            County burden table
          </Link>
          <Link
            href="/risk"
            className="inline-flex items-center gap-2 rounded-lg border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 px-5 py-2.5 text-sm font-semibold hover:bg-zinc-100 dark:hover:bg-zinc-800"
          >
            Risk queue
          </Link>
        </div>

        {hasData && (
          <p className="pt-3 text-xs text-zinc-500 dark:text-zinc-500">
            HUD 30% threshold &middot; representative profile MFJ, 1 dependent,
            1 qualifying child &middot; formula version{" "}
            <code className="font-mono">{FORMULA_VERSION}</code>
          </p>
        )}
      </div>
    </section>
  );
}

/* ---------------------------------------------------------------- */
/*  Headline tile + best/worst counties                             */
/* ---------------------------------------------------------------- */

function CountyExtremesCard({ headline }: { headline: NjAffordabilityHeadline }) {
  const { rows, latest_year } = headline;
  const populated = rows.filter((r) => r.headroom != null);

  // rows are sorted ASC by headroom (worst first); reverse for "easiest"
  const worst = populated.slice(0, 5);
  const best = populated.slice(-5).reverse();

  return (
    <section className="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <SummaryTile headline={headline} />

      <CountyList
        title={`Worst affordability gap (${latest_year ?? "—"})`}
        sublabel="Median household needs MORE annual gross income to afford the county median home"
        rows={worst}
        tone="bad"
      />

      <CountyList
        title={`Most headroom (${latest_year ?? "—"})`}
        sublabel="Median household has surplus income vs the HUD-required threshold"
        rows={best}
        tone="good"
      />
    </section>
  );
}

function SummaryTile({ headline }: { headline: NjAffordabilityHeadline }) {
  const {
    latest_year,
    counties_with_data,
    counties_unaffordable,
    counties_severely_unaffordable,
    avg_required_income,
    avg_median_income,
    avg_headroom,
  } = headline;

  return (
    <div className="rounded-xl border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-6 space-y-5">
      <div>
        <div className="text-xs uppercase tracking-wider text-zinc-500">
          NJ statewide snapshot
        </div>
        <div className="mt-0.5 text-sm text-zinc-600 dark:text-zinc-400">
          {latest_year ?? "—"} &middot; HUD 30% definition
        </div>
      </div>

      <div className="grid grid-cols-2 gap-y-4 gap-x-3">
        <Stat
          label="Counties unaffordable"
          value={`${counties_unaffordable} / ${counties_with_data}`}
          accent="text-rose-600 dark:text-rose-400"
        />
        <Stat
          label={`Severely unaffordable (gap > $25K)`}
          value={`${counties_severely_unaffordable} / ${counties_with_data}`}
          accent="text-rose-600 dark:text-rose-400"
        />
        <Stat
          label="Avg county median income"
          value={avg_median_income == null ? "—" : fmtUsd(avg_median_income)}
        />
        <Stat
          label="Avg required income (HUD)"
          value={avg_required_income == null ? "—" : fmtUsd(avg_required_income)}
        />
        <Stat
          label="Avg shortfall vs HUD threshold"
          value={avg_headroom == null ? "—" : signedUsd(avg_headroom)}
          accent={
            avg_headroom != null && avg_headroom < 0
              ? "text-rose-600 dark:text-rose-400"
              : "text-emerald-600 dark:text-emerald-400"
          }
          full
        />
      </div>
    </div>
  );
}

function CountyList({
  title,
  sublabel,
  rows,
  tone,
}: {
  title: string;
  sublabel: string;
  rows: CountyHeadlineRow[];
  tone: "good" | "bad";
}) {
  return (
    <div className="rounded-xl border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-6 space-y-4">
      <div>
        <h2 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
          {title}
        </h2>
        <p className="mt-0.5 text-xs text-zinc-500">{sublabel}</p>
      </div>
      <ul className="divide-y divide-zinc-100 dark:divide-zinc-800">
        {rows.map((r) => (
          <li key={r.county_id} className="py-2.5 flex items-baseline gap-3">
            <Link
              href={`/housing/${encodeURIComponent(r.county_id)}/collapse`}
              className="text-sm font-medium hover:underline truncate"
            >
              {r.county_name}
            </Link>
            <span className="text-xs font-mono text-zinc-500 hidden sm:inline">
              {r.county_fips}
            </span>
            <span className="ml-auto text-sm font-mono whitespace-nowrap">
              <span
                className={
                  tone === "bad"
                    ? "text-rose-600 dark:text-rose-400 font-semibold"
                    : "text-emerald-600 dark:text-emerald-400 font-semibold"
                }
              >
                {r.headroom == null ? "—" : signedUsd(r.headroom)}
              </span>
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function SecondaryCallouts() {
  return (
    <section className="grid grid-cols-1 md:grid-cols-3 gap-4">
      <Callout
        title="Personalize"
        body="Your income, filing status, dependents, debt, town &mdash; we run the same engine for you and tell you which NJ towns actually fit."
        href="/personalize"
        cta="Run my numbers"
        primary
      />
      <Callout
        title="Collapse Curve"
        body="Per-county chart of actual median household income vs the income required to afford the median home. The viral insight chart from the spec (&sect;7.3)."
        href="/housing"
        cta="Browse counties"
      />
      <Callout
        title="Methodology"
        body="Every formula is versioned. Every constant carries a citation. Substrate honesty: missing data renders as a hole, never as a guess."
        href="/about"
        cta="Read the methodology"
      />
    </section>
  );
}

/* ---------------------------------------------------------------- */
/*  Helpers                                                         */
/* ---------------------------------------------------------------- */

function Stat({
  label,
  value,
  accent,
  full,
}: {
  label: string;
  value: string;
  accent?: string;
  full?: boolean;
}) {
  return (
    <div className={full ? "col-span-2" : undefined}>
      <div className="text-[11px] uppercase tracking-wider text-zinc-500">
        {label}
      </div>
      <div className={`mt-0.5 font-mono text-lg font-semibold ${accent ?? ""}`}>
        {value}
      </div>
    </div>
  );
}

function Callout({
  title,
  body,
  href,
  cta,
  primary,
}: {
  title: string;
  body: string;
  href: string;
  cta: string;
  primary?: boolean;
}) {
  return (
    <Link
      href={href}
      className={`group rounded-xl border p-5 transition-colors ${
        primary
          ? "border-rose-300 dark:border-rose-900 bg-rose-50 dark:bg-rose-950/30 hover:bg-rose-100 dark:hover:bg-rose-950/60"
          : "border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 hover:bg-zinc-50 dark:hover:bg-zinc-800/60"
      }`}
    >
      <div className="text-xs uppercase tracking-wider text-zinc-500">
        {title}
      </div>
      <p
        className="mt-2 text-sm text-zinc-700 dark:text-zinc-300"
        dangerouslySetInnerHTML={{ __html: body }}
      />
      <div className="mt-3 inline-flex items-center gap-1 text-sm font-semibold group-hover:gap-2 transition-all">
        <span
          className={
            primary
              ? "text-rose-700 dark:text-rose-300"
              : "text-zinc-700 dark:text-zinc-300"
          }
        >
          {cta}
        </span>
        <span aria-hidden>&rarr;</span>
      </div>
    </Link>
  );
}

function signedUsd(n: number): string {
  if (!Number.isFinite(n)) return "—";
  const sign = n < 0 ? "\u2212" : n > 0 ? "+" : "";
  return `${sign}${fmtUsd(Math.abs(n))}`;
}

/* ---------------------------------------------------------------- */
/*  Empty / error states                                            */
/* ---------------------------------------------------------------- */

function DbDownNotice({ error }: { error?: string }) {
  return (
    <div className="rounded-md bg-amber-50 dark:bg-amber-950 p-4 text-sm">
      <div className="font-medium text-amber-800 dark:text-amber-200">
        Database not reachable yet.
      </div>
      <p className="mt-1 text-amber-700 dark:text-amber-300">
        This deployment has not been wired to a Postgres instance, or the{" "}
        <code className="font-mono">NEON_DATABASE_URL</code> env var has not
        been set. The screener works once a Neon (or compatible) Postgres URL
        is configured in the Vercel project. See the repository{" "}
        <code className="font-mono">README.md</code> for setup steps.
      </p>
      {error && (
        <pre className="mt-2 overflow-x-auto rounded bg-amber-100/60 dark:bg-amber-900/40 p-2 text-xs text-amber-900 dark:text-amber-200">
          {error}
        </pre>
      )}
    </div>
  );
}

function QueryErrorNotice({ error }: { error: string }) {
  return (
    <div className="rounded-md bg-red-50 dark:bg-red-950 p-4 text-sm">
      <div className="font-medium text-red-800 dark:text-red-200">
        Database reachable, but the affordability substrate query failed.
      </div>
      <p className="mt-1 text-red-700 dark:text-red-300">
        Run the migrations and seeds (
        <code className="font-mono">scripts/deploy_neon_substrate.sh</code>)
        against the configured Neon instance.
      </p>
      <pre className="mt-2 overflow-x-auto rounded bg-red-100/60 dark:bg-red-900/40 p-2 text-xs text-red-900 dark:text-red-200">
        {error}
      </pre>
    </div>
  );
}

function SubstrateEmptyNotice() {
  return (
    <div className="rounded-md border border-dashed border-zinc-300 dark:border-zinc-700 p-6 text-sm text-zinc-600 dark:text-zinc-400">
      <p className="font-medium">No affordability rows yet.</p>
      <p className="mt-1">
        The schema is present but{" "}
        <code className="font-mono">derived.v_affordability_gap</code> has no
        joinable years. Bulk-load FRED, ACS, NJ DCA, and the IRS / NJ tax
        seeds via{" "}
        <code className="font-mono">scripts/deploy_neon_substrate.sh</code>.
      </p>
    </div>
  );
}
