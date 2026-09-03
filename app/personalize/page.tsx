/**
 * /personalize — the Personalized Affordability Engine page.
 *
 * Implements idea spec §11 ("relocation decision tools"). Same engine
 * as Phases 1-3, with the median household swapped for a user-supplied
 * profile. Server-rendered (zero client JS), profile in URL query
 * params (shareable, no PII server-side), per-county verdict table.
 *
 * Architectural posture
 * ---------------------
 * 1. The form uses METHOD=GET so submitting it navigates to a URL that
 *    fully encodes the profile. The URL IS the state. Bookmarks,
 *    sharing, and back/forward all "just work."
 * 2. The page is a Server Component. No client JS, no hydration cost,
 *    no `'use client'`. Filters/recalcs happen server-side with sub-
 *    second latency thanks to the Phase-2/3/4 closed-form math.
 * 3. Every dollar number on the page traces back to one of the SQL
 *    functions in migrations 070/072/074 with a cited assumption.
 *
 * UI posture (post-Phase-9 polish)
 * --------------------------------
 * - Hero answers a question, not an essay.
 * - Sample-profile chips give first-time visitors a one-click demo.
 * - The county table is the page's spine: one verdict column, the
 *   county name itself is the drill-down toggle. Income gap is
 *   `nowrap` so signed currency never wraps.
 * - Muni drill-down has a server-side `?q=` filter and `?sort=` order
 *   so a 70-row Bergen list isn't a wall.
 */

import Link from "next/link";

import { AffordablePriceRangeCard } from "@/components/AffordablePriceRange";
import { isDbReachable } from "@/lib/db";
import {
  CountyVerdictRow,
  DEFAULT_PROFILE,
  FILING_STATUS_LABEL,
  FilingStatus,
  affordableHomePriceRange,
  fmtPct,
  fmtUsd,
  HouseholdProfile,
  MuniVerdictRow,
  parseProfileFromSearch,
  profileToSearchParams,
  runMuniVerdicts,
  runPersonalizationEngine,
  stretchMultiplierFromAssumptions,
  verdictTone,
} from "@/lib/personalize";

export const dynamic = "force-dynamic";
export const revalidate = 0;

export const metadata = {
  title: "Personalized affordability — NJ Unchained",
  description:
    "Enter your household profile and see, county by county, where in " +
    "New Jersey you can actually afford to live. Every number cited.",
};

interface PageProps {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

// ---------------------------------------------------------------------------
// Sample profiles -- one-click demos for first-time visitors.
// ---------------------------------------------------------------------------

const SAMPLE_PROFILES: ReadonlyArray<{
  label: string;
  hint: string;
  query: string;
}> = [
  {
    label: "Single, $80k",
    hint: "1099 grad-school graduate",
    query: "gross=80000&filing=single",
  },
  {
    label: "Single, $150k",
    hint: "tech IC, no kids",
    query: "gross=150000&filing=single",
  },
  {
    label: "MFJ, $200k +1",
    hint: "two earners, one kid",
    query: "gross=200000&filing=mfj&deps=1&kids=1",
  },
  {
    label: "MFJ, $300k +2",
    hint: "dual-income, two kids",
    query: "gross=300000&filing=mfj&deps=2&kids=2",
  },
  {
    label: "HoH, $90k +2",
    hint: "single parent, two kids",
    query: "gross=90000&filing=hoh&deps=2&kids=2",
  },
];

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default async function PersonalizePage({ searchParams }: PageProps) {
  const reachable = await isDbReachable();
  if (!reachable.reachable) {
    return (
      <div className="rounded-md bg-amber-50 dark:bg-amber-950 p-4 text-sm text-amber-800 dark:text-amber-200">
        Database not reachable. Configure{" "}
        <code className="font-mono">NEON_DATABASE_URL</code>.
      </div>
    );
  }

  const sp = await searchParams;
  const profile = parseProfileFromSearch(sp);
  // The page renders BOTH on initial load (with default profile) AND
  // after form submit (with user profile). Same code path either way.
  const submitted = "gross" in sp;

  const drilldownFipsRaw = (() => {
    const v = sp["county"];
    if (Array.isArray(v)) return v[0] ?? null;
    return v ?? null;
  })();
  const drilldownFips =
    drilldownFipsRaw != null && /^\d{5}$/.test(drilldownFipsRaw)
      ? drilldownFipsRaw
      : null;

  // Muni-table search/sort lift state into the URL too.
  const muniQ = singleStr(sp["q"]) ?? "";
  const muniSort = (() => {
    const s = singleStr(sp["sort"]);
    return s === "name" || s === "home" || s === "gap" ? s : "gap";
  })();

  const [result, drilldown] = await Promise.all([
    runPersonalizationEngine(profile),
    drilldownFips != null
      ? runMuniVerdicts(profile, drilldownFips)
      : Promise.resolve(null),
  ]);

  // Sort counties by gross_income_gap ascending (most-affordable first).
  // Counties with NULL median (no substrate) sink to the bottom.
  const sortedCounties = [...result.counties].sort((a, b) => {
    if (a.median_home_price == null && b.median_home_price == null) return 0;
    if (a.median_home_price == null) return 1;
    if (b.median_home_price == null) return -1;
    const ga = a.gross_income_gap ?? Number.POSITIVE_INFINITY;
    const gb = b.gross_income_gap ?? Number.POSITIVE_INFINITY;
    return ga - gb;
  });

  const populatedCounties = sortedCounties.filter(
    (c) => c.median_home_price != null,
  );
  const affordableCounties = populatedCounties.filter(
    (c) => c.verdict_dti === "affordable",
  );
  const stretchCounties = populatedCounties.filter(
    (c) => c.verdict_dti === "stretch",
  );
  const outOfReachCounties = populatedCounties.filter(
    (c) => c.verdict_dti === "out_of_reach",
  );
  const cheapestAffordable = affordableCounties[0] ?? null;
  const priceRange = affordableHomePriceRange(
    populatedCounties,
    stretchMultiplierFromAssumptions(result.assumptions),
  );
  const tightestOutOfReach =
    [...outOfReachCounties].sort(
      (a, b) =>
        (a.gross_income_gap ?? Number.POSITIVE_INFINITY) -
        (b.gross_income_gap ?? Number.POSITIVE_INFINITY),
    )[0] ?? null;

  return (
    <div className="space-y-6">
      <Hero />
      <SampleChips currentQuery={profileToSearchParams(profile)} />
      <ProfileForm profile={profile} />

      {!submitted && (
        <div className="rounded-md border border-zinc-200 dark:border-zinc-800 bg-zinc-100/60 dark:bg-zinc-900/60 px-4 py-3 text-xs text-zinc-600 dark:text-zinc-400">
          Showing the default profile. Pick a sample above, edit the
          form, and submit to personalize. Your profile lives in the
          URL — bookmark or share.
        </div>
      )}

      {result.year_fallback_reason && (
        <div className="rounded-md bg-amber-50 dark:bg-amber-950 p-3 text-xs text-amber-800 dark:text-amber-200">
          {result.year_fallback_reason}
        </div>
      )}

      {submitted && (
        <AffordablePriceRangeCard
          range={priceRange}
          year={result.resolved_year}
        />
      )}

      {populatedCounties.length > 0 && (
        <HeadlineAnswer
          year={result.resolved_year}
          totalPopulated={populatedCounties.length}
          totalCounties={result.counties.length}
          affordable={affordableCounties.length}
          stretch={stretchCounties.length}
          outOfReach={outOfReachCounties.length}
          cheapest={cheapestAffordable}
          tightest={tightestOutOfReach}
        />
      )}

      {result.tax != null && (
        <TaxBurdenCard
          year={result.resolved_year}
          gross={profile.gross_income}
          federal={result.tax.federal}
          nj_state={result.tax.nj_state}
          fica={result.tax.fica}
          total={result.tax.total}
          take_home={result.tax.take_home}
          effective_rate={result.tax.effective_rate}
        />
      )}

      {populatedCounties.length > 0 && (
        <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-5">
          <div className="flex items-baseline justify-between mb-3">
            <h2 className="font-medium text-base">
              Per-county verdict
              <span className="ml-2 text-xs font-normal text-zinc-500">
                {populatedCounties.length} of {result.counties.length}{" "}
                counties have substrate · sorted by income gap
              </span>
            </h2>
            <div className="text-xs text-zinc-500 font-mono hidden sm:block">
              {result.formula_version}
            </div>
          </div>
          <CountyTable
            rows={sortedCounties}
            drilldownFips={drilldownFips}
            profileQuery={profileToSearchParams(profile)}
          />
          <p className="mt-3 text-xs text-zinc-500">
            <strong className="text-emerald-700 dark:text-emerald-300">
              Affordable
            </strong>{" "}
            = median home ≤ your max-affordable.{" "}
            <strong className="text-amber-700 dark:text-amber-300">
              Stretch
            </strong>{" "}
            = up to 25% over (HUD outreach materials).{" "}
            <strong className="text-red-700 dark:text-red-300">
              Out of reach
            </strong>{" "}
            = beyond that. <em>Click a county to drill into towns.</em>
          </p>
        </section>
      )}

      {drilldown != null && (
        <MuniDrilldownSection
          drilldown={drilldown}
          q={muniQ}
          sort={muniSort}
          profileQuery={profileToSearchParams(profile)}
        />
      )}

      <AssumptionsCard assumptions={result.assumptions} />

      <div className="text-xs text-zinc-500">
        <Link href="/about" className="underline underline-offset-4">
          ← Methodology
        </Link>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Hero / chips
// ---------------------------------------------------------------------------

function Hero() {
  return (
    <header className="space-y-2">
      <div className="text-xs uppercase tracking-wider text-zinc-500">
        Personalized affordability engine
      </div>
      <h1 className="text-3xl sm:text-4xl font-semibold tracking-tight leading-tight">
        Where in NJ can you{" "}
        <span className="text-red-600 dark:text-red-400">
          actually afford
        </span>{" "}
        to live?
      </h1>
      <p className="text-sm text-zinc-600 dark:text-zinc-400 max-w-3xl">
        Tax + Fannie Mae DTI + median-home math, computed for{" "}
        <strong>your</strong> household against every NJ county and
        every town. Every dollar cites the source we derived it from.
      </p>
    </header>
  );
}

function SampleChips({ currentQuery: _ }: { currentQuery: string }) {
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs">
      <span className="text-zinc-500">Try a sample:</span>
      {SAMPLE_PROFILES.map((s) => (
        <Link
          key={s.query}
          href={`/personalize?${s.query}`}
          className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 hover:border-red-500 dark:hover:border-red-400 hover:text-red-600 dark:hover:text-red-400 transition-colors"
          title={s.hint}
        >
          <span className="font-medium">{s.label}</span>
          <span className="text-zinc-400">·</span>
          <span className="text-zinc-500">{s.hint}</span>
        </Link>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Headline answer (the one panel everyone reads first)
// ---------------------------------------------------------------------------

function HeadlineAnswer({
  year,
  totalPopulated,
  totalCounties,
  affordable,
  stretch,
  outOfReach,
  cheapest,
  tightest,
}: {
  year: number;
  totalPopulated: number;
  totalCounties: number;
  affordable: number;
  stretch: number;
  outOfReach: number;
  cheapest: CountyVerdictRow | null;
  tightest: CountyVerdictRow | null;
}) {
  const total = totalPopulated;
  const affordablePct = total > 0 ? affordable / total : 0;

  return (
    <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-gradient-to-br from-zinc-50 to-white dark:from-zinc-900 dark:to-zinc-950 p-5 sm:p-6">
      <div className="flex items-baseline justify-between mb-1">
        <div className="text-xs uppercase tracking-wider text-zinc-500">
          The answer ({year} substrate)
        </div>
      </div>
      <h2 className="text-2xl sm:text-3xl font-semibold tracking-tight">
        You can comfortably afford{" "}
        <span className="text-emerald-700 dark:text-emerald-300">
          {affordable} of {totalCounties}
        </span>{" "}
        NJ counties.
      </h2>

      <div className="mt-4 grid grid-cols-3 gap-3">
        <StackedBarTile
          label="Affordable"
          count={affordable}
          total={total}
          tone="ok"
        />
        <StackedBarTile
          label="Stretch"
          count={stretch}
          total={total}
          tone="warn"
        />
        <StackedBarTile
          label="Out of reach"
          count={outOfReach}
          total={total}
          tone="bad"
        />
      </div>

      <div className="mt-5 grid grid-cols-1 sm:grid-cols-2 gap-3 text-sm">
        {cheapest != null && (
          <div className="rounded-md border border-emerald-200 dark:border-emerald-900 bg-emerald-50/50 dark:bg-emerald-950/30 p-3">
            <div className="text-xs uppercase tracking-wider text-emerald-700 dark:text-emerald-300">
              Most headroom
            </div>
            <div className="mt-1 font-medium">{cheapest.county_name}</div>
            <div className="text-xs text-zinc-600 dark:text-zinc-400 mt-0.5">
              Median home {fmtUsd(cheapest.median_home_price)} · you have{" "}
              <span className="font-mono whitespace-nowrap text-emerald-700 dark:text-emerald-300">
                {cheapest.gross_income_gap != null
                  ? fmtUsd(Math.abs(cheapest.gross_income_gap))
                  : "—"}
              </span>{" "}
              of income headroom over what&apos;s required.
            </div>
          </div>
        )}
        {tightest != null && (
          <div className="rounded-md border border-red-200 dark:border-red-900 bg-red-50/50 dark:bg-red-950/30 p-3">
            <div className="text-xs uppercase tracking-wider text-red-700 dark:text-red-300">
              Closest miss
            </div>
            <div className="mt-1 font-medium">{tightest.county_name}</div>
            <div className="text-xs text-zinc-600 dark:text-zinc-400 mt-0.5">
              Median home {fmtUsd(tightest.median_home_price)} · you&apos;d
              need{" "}
              <span className="font-mono whitespace-nowrap text-red-700 dark:text-red-300">
                +{fmtUsd(tightest.gross_income_gap)}
              </span>{" "}
              more gross income to qualify under conventional DTI.
            </div>
          </div>
        )}
      </div>

      {affordablePct === 0 && total > 0 && (
        <p className="mt-4 text-xs text-zinc-500">
          No NJ county is comfortably affordable on this profile. Use
          the &ldquo;Stretch&rdquo; band as a guide to which counties
          come closest.
        </p>
      )}
    </section>
  );
}

function StackedBarTile({
  label,
  count,
  total,
  tone,
}: {
  label: string;
  count: number;
  total: number;
  tone: "ok" | "warn" | "bad";
}) {
  const pct = total > 0 ? Math.round((count / total) * 100) : 0;
  const palette =
    tone === "ok"
      ? {
          fg: "text-emerald-700 dark:text-emerald-300",
          bar: "bg-emerald-500",
          track: "bg-emerald-100 dark:bg-emerald-950",
        }
      : tone === "warn"
        ? {
            fg: "text-amber-700 dark:text-amber-300",
            bar: "bg-amber-500",
            track: "bg-amber-100 dark:bg-amber-950",
          }
        : {
            fg: "text-red-700 dark:text-red-300",
            bar: "bg-red-500",
            track: "bg-red-100 dark:bg-red-950",
          };
  return (
    <div className="space-y-1.5">
      <div className="flex items-baseline justify-between text-xs">
        <span className={`uppercase tracking-wider font-medium ${palette.fg}`}>
          {label}
        </span>
        <span className="text-zinc-500 font-mono">{pct}%</span>
      </div>
      <div className="text-2xl font-semibold tabular-nums">{count}</div>
      <div className={`h-1.5 rounded-full overflow-hidden ${palette.track}`}>
        <div
          className={`h-full ${palette.bar}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tax burden card
// ---------------------------------------------------------------------------

function TaxBurdenCard({
  year,
  gross,
  federal,
  nj_state,
  fica,
  total,
  take_home,
  effective_rate,
}: {
  year: number;
  gross: number;
  federal: number;
  nj_state: number;
  fica: number;
  total: number;
  take_home: number;
  effective_rate: number;
}) {
  return (
    <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-5">
      <div className="flex items-baseline justify-between mb-3">
        <h2 className="font-medium text-base">
          Your tax burden{" "}
          <span className="text-zinc-500 font-normal text-sm">({year})</span>
        </h2>
        <div className="hidden sm:block text-xs text-zinc-500">
          Federal + NJ + FICA · effective = total ÷ gross
        </div>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 text-sm">
        <KV label="Gross income" value={fmtUsd(gross)} tone="info" />
        <KV
          label="Total tax"
          value={fmtUsd(total)}
          tone="warn"
          sub={fmtPct(effective_rate)}
        />
        <KV label="Take-home" value={fmtUsd(take_home)} tone="ok" />
      </div>

      <div className="mt-4 grid grid-cols-3 gap-3 text-xs border-t border-zinc-200 dark:border-zinc-800 pt-3">
        <KV label="Federal" value={fmtUsd(federal)} dim />
        <KV label="NJ state" value={fmtUsd(nj_state)} dim />
        <KV label="FICA" value={fmtUsd(fica)} dim />
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Profile form
// ---------------------------------------------------------------------------

function ProfileForm({ profile }: { profile: HouseholdProfile }) {
  return (
    <form
      method="GET"
      className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-5 space-y-4"
    >
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
        <Field label="Gross income ($)">
          <input
            name="gross"
            type="number"
            min="0"
            step="1000"
            defaultValue={profile.gross_income}
            className="form-input"
            required
          />
        </Field>

        <Field label="Filing">
          <select
            name="filing"
            defaultValue={profile.filing_status}
            className="form-input"
          >
            {(["single", "mfj", "mfs", "hoh", "qss"] as FilingStatus[]).map(
              (s) => (
                <option key={s} value={s}>
                  {FILING_STATUS_LABEL[s]}
                </option>
              ),
            )}
          </select>
        </Field>

        <Field label="Year">
          <input
            name="year"
            type="number"
            min="2010"
            max="2099"
            defaultValue={profile.year}
            className="form-input"
          />
        </Field>

        <Field label="Dependents">
          <input
            name="deps"
            type="number"
            min="0"
            max="20"
            defaultValue={profile.dependents}
            className="form-input"
          />
        </Field>

        <Field label="CTC kids">
          <input
            name="kids"
            type="number"
            min="0"
            max="20"
            defaultValue={profile.qualifying_children}
            className="form-input"
          />
        </Field>

        <Field label="Other debt $/mo">
          <input
            name="debt"
            type="number"
            min="0"
            step="50"
            defaultValue={profile.other_monthly_debt}
            className="form-input"
          />
        </Field>
      </div>

      <details className="text-sm group/mortgage">
        <summary className="no-marker cursor-pointer text-xs uppercase tracking-wider text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300 select-none inline-flex items-center gap-1.5">
          <span className="inline-block transition-transform group-open/mortgage:rotate-90 text-zinc-400">
            ▸
          </span>
          Mortgage assumptions (optional overrides)
        </summary>
        <div className="mt-3 grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
          <Field label="Down %">
            <input
              name="down"
              type="number"
              min="0"
              max="1"
              step="0.01"
              defaultValue={profile.down_pct ?? ""}
              placeholder="0.20"
              className="form-input"
            />
          </Field>
          <Field label="Term (yrs)">
            <select
              name="term"
              defaultValue={
                profile.term_years == null ? "" : String(profile.term_years)
              }
              className="form-input"
            >
              <option value="">30 (default)</option>
              <option value="15">15</option>
              <option value="30">30</option>
            </select>
          </Field>
          <Field label="DTI front">
            <input
              name="dtif"
              type="number"
              min="0"
              max="1"
              step="0.01"
              defaultValue={profile.dti_front ?? ""}
              placeholder="0.28"
              className="form-input"
            />
          </Field>
          <Field label="DTI back">
            <input
              name="dtib"
              type="number"
              min="0"
              max="1"
              step="0.01"
              defaultValue={profile.dti_back ?? ""}
              placeholder="0.36"
              className="form-input"
            />
          </Field>
          <Field label="Rate override">
            <input
              name="rate"
              type="number"
              min="0"
              max="0.30"
              step="0.001"
              defaultValue={profile.rate_override ?? ""}
              placeholder="auto (FRED)"
              className="form-input"
            />
          </Field>
        </div>
      </details>

      <div className="flex flex-wrap gap-3 items-center pt-1">
        <button
          type="submit"
          className="px-5 py-2.5 rounded-md bg-red-600 text-white text-sm font-semibold hover:bg-red-700 dark:hover:bg-red-500 shadow-sm"
        >
          Compute affordability
        </button>
        <a
          href="/personalize"
          className="px-3 py-2 rounded-md border border-zinc-300 dark:border-zinc-700 text-sm text-zinc-700 dark:text-zinc-300 hover:bg-zinc-100 dark:hover:bg-zinc-800"
        >
          Reset
        </a>
        <span className="text-xs text-zinc-500 ml-auto">
          Profile lives in the URL — share or bookmark.
        </span>
      </div>

      <style>{`
        .form-input {
          display: block;
          width: 100%;
          padding: 0.45rem 0.6rem;
          border: 1px solid rgb(212 212 216);
          border-radius: 0.375rem;
          background-color: white;
          color: rgb(24 24 27);
          font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
          font-size: 0.875rem;
          line-height: 1.25rem;
        }
        .form-input:focus {
          outline: 2px solid rgb(220 38 38);
          outline-offset: 1px;
          border-color: rgb(220 38 38);
        }
        .dark .form-input {
          background-color: rgb(24 24 27);
          color: rgb(244 244 245);
          border-color: rgb(63 63 70);
        }
        summary.no-marker { list-style: none; }
        summary.no-marker::-webkit-details-marker { display: none; }
        summary.no-marker::marker { content: ""; }
      `}</style>
    </form>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block text-sm relative">
      <span className="block text-[0.65rem] uppercase tracking-wider text-zinc-500 mb-1 font-medium">
        {label}
      </span>
      <span className="block relative">{children}</span>
    </label>
  );
}

// ---------------------------------------------------------------------------
// County table
// ---------------------------------------------------------------------------

function CountyTable({
  rows,
  drilldownFips,
  profileQuery,
}: {
  rows: CountyVerdictRow[];
  drilldownFips: string | null;
  profileQuery: string;
}) {
  if (rows.length === 0) {
    return (
      <p className="text-sm text-zinc-500">
        No NJ counties available for the requested year.
      </p>
    );
  }
  return (
    <div className="overflow-x-auto -mx-5 sm:mx-0">
      <table className="w-full text-sm border-collapse">
        <thead>
          <tr className="text-left text-[0.65rem] uppercase tracking-wider text-zinc-500 border-b border-zinc-200 dark:border-zinc-800">
            <th className="py-2 px-5 sm:px-3 font-medium">County</th>
            <th className="py-2 px-3 text-right font-medium whitespace-nowrap">
              Median home
            </th>
            <th className="py-2 px-3 text-right font-medium whitespace-nowrap">
              Your max
            </th>
            <th className="py-2 px-3 text-right font-medium whitespace-nowrap">
              Income gap
            </th>
            <th className="py-2 px-3 text-right font-medium whitespace-nowrap hidden sm:table-cell">
              Burden
            </th>
            <th className="py-2 px-3 font-medium">Verdict</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((c) => {
            const tone = verdictTone(c.verdict_dti);
            const ptt = verdictTone(c.verdict_post_tax);
            const showPostTaxBadge =
              c.verdict_post_tax != null &&
              c.verdict_dti !== c.verdict_post_tax;

            const gapClass =
              c.gross_income_gap == null
                ? "text-zinc-500"
                : c.gross_income_gap <= 0
                  ? "text-emerald-700 dark:text-emerald-300"
                  : "text-red-700 dark:text-red-300";

            const isActive = drilldownFips === c.county_fips;
            const drilldownHref = isActive
              ? `/personalize?${profileQuery}`
              : `/personalize?${profileQuery}${profileQuery ? "&" : ""}county=${c.county_fips}#towns`;

            const hasSubstrate = c.median_home_price != null;

            return (
              <tr
                key={c.county_fips}
                className={`border-b border-zinc-100 dark:border-zinc-900 ${
                  isActive
                    ? "bg-blue-50/50 dark:bg-blue-950/30"
                    : "hover:bg-zinc-50 dark:hover:bg-zinc-950/50"
                }`}
              >
                <td className="py-2.5 px-5 sm:px-3 font-medium">
                  {hasSubstrate ? (
                    <Link
                      href={drilldownHref}
                      className="inline-flex items-center gap-1.5 text-zinc-900 dark:text-zinc-100 hover:text-red-600 dark:hover:text-red-400"
                    >
                      <span
                        className={`text-zinc-400 transition-transform ${isActive ? "rotate-90" : ""}`}
                        aria-hidden
                      >
                        ▸
                      </span>
                      <span className="underline-offset-4 hover:underline">
                        {c.county_name}
                      </span>
                    </Link>
                  ) : (
                    <span className="text-zinc-500">{c.county_name}</span>
                  )}
                </td>
                <td className="py-2.5 px-3 font-mono text-right whitespace-nowrap">
                  {fmtUsd(c.median_home_price)}
                </td>
                <td className="py-2.5 px-3 font-mono text-right whitespace-nowrap">
                  {fmtUsd(c.max_affordable_dti)}
                </td>
                <td
                  className={`py-2.5 px-3 font-mono text-right whitespace-nowrap ${gapClass}`}
                >
                  {fmtSignedGap(c.gross_income_gap)}
                </td>
                <td className="py-2.5 px-3 font-mono text-right whitespace-nowrap hidden sm:table-cell">
                  {fmtPct(c.personal_burden_ratio)}
                </td>
                <td className="py-2.5 px-3">
                  <span
                    className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${tone.bg} ${tone.fg} whitespace-nowrap`}
                  >
                    {tone.label}
                  </span>
                  {showPostTaxBadge && (
                    <span
                      className={`ml-1.5 inline-block px-1.5 py-0.5 rounded text-[0.65rem] ${ptt.bg} ${ptt.fg} whitespace-nowrap`}
                      title="Different verdict using post-tax take-home (not just gross-income DTI)"
                    >
                      post-tax: {ptt.label.toLowerCase()}
                    </span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Muni drilldown
// ---------------------------------------------------------------------------

function MuniDrilldownSection({
  drilldown,
  q,
  sort,
  profileQuery,
}: {
  drilldown: NonNullable<Awaited<ReturnType<typeof runMuniVerdicts>>>;
  q: string;
  sort: "gap" | "name" | "home";
  profileQuery: string;
}) {
  const populated = drilldown.munis.filter(
    (m) => m.median_home_price != null,
  );

  const filtered = q
    ? drilldown.munis.filter((m) =>
        m.muni_name.toLowerCase().includes(q.toLowerCase()),
      )
    : drilldown.munis;

  const sorted = [...filtered].sort((a, b) => {
    if (sort === "name") return a.muni_name.localeCompare(b.muni_name);
    if (sort === "home") {
      if (a.median_home_price == null && b.median_home_price == null) return 0;
      if (a.median_home_price == null) return 1;
      if (b.median_home_price == null) return -1;
      return a.median_home_price - b.median_home_price;
    }
    if (a.median_home_price == null && b.median_home_price == null)
      return a.muni_name.localeCompare(b.muni_name);
    if (a.median_home_price == null) return 1;
    if (b.median_home_price == null) return -1;
    const ga = a.gross_income_gap ?? Number.POSITIVE_INFINITY;
    const gb = b.gross_income_gap ?? Number.POSITIVE_INFINITY;
    return ga - gb;
  });

  const affordable = populated.filter(
    (m) => m.verdict_dti === "affordable",
  ).length;
  const stretch = populated.filter((m) => m.verdict_dti === "stretch").length;
  const oor = populated.filter((m) => m.verdict_dti === "out_of_reach").length;

  return (
    <section
      id="towns"
      className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-5"
    >
      <div className="flex items-baseline justify-between mb-3 gap-2 flex-wrap">
        <h2 className="font-medium text-base">
          {drilldown.county_name == null
            ? `Town breakdown (FIPS ${drilldown.county_fips})`
            : `Town breakdown — ${drilldown.county_name} County`}
          {populated.length > 0 && (
            <span className="ml-2 text-xs font-normal text-zinc-500">
              {populated.length} of {drilldown.munis.length} towns ·
              {" "}{affordable} affordable · {stretch} stretch · {oor} out
              of reach
            </span>
          )}
        </h2>
        <Link
          href={`/personalize?${profileQuery}`}
          className="text-xs text-zinc-600 dark:text-zinc-400 hover:text-red-600 dark:hover:text-red-400 underline underline-offset-4"
        >
          ✕ Close towns
        </Link>
      </div>

      {drilldown.unknown_county ? (
        <p className="text-sm text-amber-700 dark:text-amber-300">
          Unknown county FIPS. Use the county name in the table above
          to drill in.
        </p>
      ) : drilldown.munis.length === 0 ? (
        <p className="text-sm text-zinc-500">
          No municipalities found for this county. The Phase 8a{" "}
          <code className="font-mono">ref.nj_municipality</code> dimension
          may not be seeded.
        </p>
      ) : populated.length === 0 ? (
        <p className="text-sm text-amber-700 dark:text-amber-300">
          No NJ DCA municipal property-tax substrate loaded for this
          county/year. Try year=2024.
        </p>
      ) : (
        <>
          <MuniFilterBar
            q={q}
            sort={sort}
            countyFips={drilldown.county_fips}
            profileQuery={profileQuery}
            visibleCount={sorted.length}
            totalCount={drilldown.munis.length}
          />
          <MuniTable rows={sorted} />
          <p className="mt-3 text-xs text-zinc-500">
            Per-municipality verdicts using NJ DCA muni-level average home
            value × muni-level effective property tax rate. Same engine
            as the county table; only the geography is finer.
          </p>
        </>
      )}
    </section>
  );
}

function MuniFilterBar({
  q,
  sort,
  countyFips,
  profileQuery,
  visibleCount,
  totalCount,
}: {
  q: string;
  sort: "gap" | "name" | "home";
  countyFips: string;
  profileQuery: string;
  visibleCount: number;
  totalCount: number;
}) {
  // Render hidden inputs so the GET form preserves the user profile
  // query params + county selection alongside the q/sort state.
  const profileFields = profileQuery
    .split("&")
    .filter(Boolean)
    .map((kv) => {
      const [k, v] = kv.split("=");
      return [decodeURIComponent(k), decodeURIComponent(v ?? "")] as const;
    });
  return (
    <form
      method="GET"
      action="/personalize"
      className="mb-3 flex flex-wrap items-end gap-2"
    >
      {profileFields.map(([k, v], i) => (
        <input key={`p-${i}`} type="hidden" name={k} value={v} />
      ))}
      <input type="hidden" name="county" value={countyFips} />
      <Field label="Search town">
        <input
          name="q"
          type="text"
          defaultValue={q}
          placeholder="e.g. Englewood"
          className="form-input"
        />
      </Field>
      <Field label="Sort by">
        <select name="sort" defaultValue={sort} className="form-input">
          <option value="gap">Income gap (best first)</option>
          <option value="name">Name (A→Z)</option>
          <option value="home">Median home (low→high)</option>
        </select>
      </Field>
      <button
        type="submit"
        className="px-3 py-2 rounded-md bg-zinc-900 dark:bg-zinc-100 text-white dark:text-zinc-900 text-sm font-medium hover:opacity-90"
      >
        Apply
      </button>
      <span className="text-xs text-zinc-500 ml-auto">
        {q ? (
          <>
            Showing <strong>{visibleCount}</strong> of {totalCount} towns
            matching <span className="font-mono">{q}</span>.
          </>
        ) : (
          <>
            All {totalCount} towns. <a href={`#towns`}>↑ back to top</a>
          </>
        )}
      </span>
      <style>{`
        form input[type=text].form-input { width: 14rem; }
        form select.form-input { width: 16rem; }
      `}</style>
    </form>
  );
}

function MuniTable({ rows }: { rows: MuniVerdictRow[] }) {
  if (rows.length === 0) {
    return (
      <p className="text-sm text-zinc-500 italic">
        No towns match the current filter.
      </p>
    );
  }
  return (
    <div className="overflow-x-auto -mx-5 sm:mx-0">
      <table className="w-full text-sm border-collapse">
        <thead>
          <tr className="text-left text-[0.65rem] uppercase tracking-wider text-zinc-500 border-b border-zinc-200 dark:border-zinc-800">
            <th className="py-2 px-5 sm:px-3 font-medium">Town</th>
            <th className="py-2 px-3 text-right font-medium whitespace-nowrap">
              Avg home
            </th>
            <th className="py-2 px-3 text-right font-medium whitespace-nowrap">
              Your max
            </th>
            <th className="py-2 px-3 text-right font-medium whitespace-nowrap">
              Income gap
            </th>
            <th className="py-2 px-3 text-right font-medium whitespace-nowrap hidden sm:table-cell">
              Burden
            </th>
            <th className="py-2 px-3 font-medium">Verdict</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((m) => {
            const tone = verdictTone(m.verdict_dti);
            const ptt = verdictTone(m.verdict_post_tax);
            const showPostTaxBadge =
              m.verdict_post_tax != null &&
              m.verdict_dti !== m.verdict_post_tax;
            const gapClass =
              m.gross_income_gap == null
                ? "text-zinc-500"
                : m.gross_income_gap <= 0
                  ? "text-emerald-700 dark:text-emerald-300"
                  : "text-red-700 dark:text-red-300";
            return (
              <tr
                key={m.muni_code}
                className="border-b border-zinc-100 dark:border-zinc-900 hover:bg-zinc-50 dark:hover:bg-zinc-950/50"
              >
                <td className="py-2.5 px-5 sm:px-3 font-medium">
                  {m.muni_name}
                  <span className="text-zinc-400 font-mono text-[0.7rem] ml-1.5">
                    [{m.muni_code}]
                  </span>
                </td>
                <td className="py-2.5 px-3 font-mono text-right whitespace-nowrap">
                  {fmtUsd(m.median_home_price)}
                </td>
                <td className="py-2.5 px-3 font-mono text-right whitespace-nowrap">
                  {fmtUsd(m.max_affordable_dti)}
                </td>
                <td
                  className={`py-2.5 px-3 font-mono text-right whitespace-nowrap ${gapClass}`}
                >
                  {fmtSignedGap(m.gross_income_gap)}
                </td>
                <td className="py-2.5 px-3 font-mono text-right whitespace-nowrap hidden sm:table-cell">
                  {fmtPct(m.personal_burden_ratio)}
                </td>
                <td className="py-2.5 px-3">
                  <span
                    className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${tone.bg} ${tone.fg} whitespace-nowrap`}
                  >
                    {tone.label}
                  </span>
                  {showPostTaxBadge && (
                    <span
                      className={`ml-1.5 inline-block px-1.5 py-0.5 rounded text-[0.65rem] ${ptt.bg} ${ptt.fg} whitespace-nowrap`}
                      title="Different verdict using post-tax take-home (not just gross-income DTI)"
                    >
                      post-tax: {ptt.label.toLowerCase()}
                    </span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Assumptions
// ---------------------------------------------------------------------------

type AssumptionRow = {
  constant_id: string;
  value_numeric: number;
  unit: string;
  source_url: string;
  source_citation: string;
  effective_year: number;
};

function AssumptionsCard({
  assumptions,
}: {
  assumptions: ReadonlyArray<AssumptionRow>;
}) {
  return (
    <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-5">
      <details className="group/asum">
        <summary className="no-marker cursor-pointer select-none flex items-baseline justify-between">
          <h2 className="font-medium text-base">
            <span className="inline-block transition-transform group-open/asum:rotate-90 text-zinc-400 mr-1.5">
              ▸
            </span>
            Assumptions used
            <span className="ml-2 text-xs font-normal text-zinc-500">
              ({assumptions.length} cited constants)
            </span>
          </h2>
          <span className="text-xs text-zinc-500 group-open/asum:hidden">
            click to expand
          </span>
        </summary>
        <style>{`
          summary.no-marker { list-style: none; }
          summary.no-marker::-webkit-details-marker { display: none; }
          summary.no-marker::marker { content: ""; }
        `}</style>
        <p className="mt-3 text-xs text-zinc-500">
          Per the verifiable-data rule, every defaulted constant the
          engine touched is listed here with its source. If you override
          any of these in the form above, your override takes precedence
          and that row will reflect the override (the citation still
          shows what the default <em>would</em> have been).
        </p>
        <ul className="mt-3 space-y-2 text-xs">
          {assumptions.map((a) => (
            <li
              key={a.constant_id}
              className="border-l-2 border-zinc-300 dark:border-zinc-700 pl-3"
            >
              <div className="font-mono">
                <strong>{a.constant_id}</strong>: {a.value_numeric}{" "}
                <span className="text-zinc-500">{a.unit}</span>
                {a.effective_year !== 0 && (
                  <span className="text-zinc-500">
                    {" "}
                    (effective from {a.effective_year})
                  </span>
                )}
              </div>
              <div className="text-zinc-600 dark:text-zinc-400 mt-0.5">
                {a.source_citation}
              </div>
              <a
                href={a.source_url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-blue-700 dark:text-blue-300 underline underline-offset-2 break-all"
              >
                {a.source_url}
              </a>
            </li>
          ))}
        </ul>
      </details>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Small reusable bits
// ---------------------------------------------------------------------------

function KV({
  label,
  value,
  tone,
  sub,
  dim,
}: {
  label: string;
  value: string;
  tone?: "warn" | "ok" | "info";
  sub?: string;
  dim?: boolean;
}) {
  let valueClass = "font-mono text-base";
  if (tone === "warn")
    valueClass =
      "font-mono text-base text-amber-700 dark:text-amber-300 font-semibold";
  if (tone === "ok")
    valueClass =
      "font-mono text-base text-emerald-700 dark:text-emerald-300 font-semibold";
  if (tone === "info")
    valueClass = "font-mono text-base text-blue-700 dark:text-blue-300";
  if (dim) valueClass = "font-mono text-sm text-zinc-700 dark:text-zinc-300";

  return (
    <div>
      <div className="text-[0.65rem] uppercase tracking-wider text-zinc-500 font-medium">
        {label}
      </div>
      <div className={`mt-0.5 ${valueClass} whitespace-nowrap`}>{value}</div>
      {sub != null && (
        <div className="text-[0.65rem] text-zinc-500 font-mono">{sub}</div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmtSignedGap(n: number | null): string {
  if (n == null || !Number.isFinite(n)) return "—";
  // U+2212 minus sign instead of hyphen-minus for proper typographic
  // alignment with `+`. Keep `whitespace-nowrap` on the cell.
  return n <= 0
    ? `−${fmtUsd(Math.abs(n))}`
    : `+${fmtUsd(n)}`;
}

function singleStr(
  v: string | string[] | undefined | null,
): string | null {
  if (v == null) return null;
  if (Array.isArray(v)) return v[0] ?? null;
  return v;
}

// Note: DEFAULT_PROFILE is exported for test+stability; suppress unused-import warning.
void DEFAULT_PROFILE;
