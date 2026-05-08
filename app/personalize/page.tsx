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
 */

import Link from "next/link";

import { isDbReachable } from "@/lib/db";
import {
  CountyVerdictRow,
  DEFAULT_PROFILE,
  FILING_STATUS_LABEL,
  FilingStatus,
  fmtPct,
  fmtUsd,
  MuniVerdictRow,
  parseProfileFromSearch,
  profileToSearchParams,
  runMuniVerdicts,
  runPersonalizationEngine,
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

  // Phase 8c drilldown: when ?county=<5-digit FIPS> is present, fetch
  // the muni-level breakdown for that county in parallel with the
  // county-level engine. Sanitization happens inside runMuniVerdicts;
  // an unknown FIPS short-circuits to an empty result rather than
  // throwing.
  const drilldownFipsRaw = (() => {
    const v = sp["county"];
    if (Array.isArray(v)) return v[0] ?? null;
    return v ?? null;
  })();
  const drilldownFips =
    drilldownFipsRaw != null && /^\d{5}$/.test(drilldownFipsRaw)
      ? drilldownFipsRaw
      : null;

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
  const affordableCount = populatedCounties.filter(
    (c) => c.verdict_dti === "affordable",
  ).length;
  const stretchCount = populatedCounties.filter(
    (c) => c.verdict_dti === "stretch",
  ).length;
  const outOfReachCount = populatedCounties.filter(
    (c) => c.verdict_dti === "out_of_reach",
  ).length;

  return (
    <div className="space-y-6">
      <header>
        <div className="text-xs uppercase tracking-wider text-zinc-500">
          Personalized affordability engine
        </div>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight">
          Where in NJ can you actually afford to live?
        </h1>
        <p className="mt-2 text-sm text-zinc-600 dark:text-zinc-400 max-w-3xl">
          Enter your household profile below. We compute your federal +
          NJ + FICA tax burden, your max-affordable home price under
          conventional Fannie Mae DTI standards (28% front / 36% back),
          and a per-county verdict against each NJ county&apos;s median
          home. Every dollar number cites the source we derived it from
          — see the assumptions block at the bottom.
        </p>
      </header>

      <ProfileForm profile={profile} />

      {!submitted && (
        <div className="rounded-md border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-950 p-4 text-xs text-zinc-600 dark:text-zinc-400">
          Showing default profile. Edit the form above and submit to
          personalize.
        </div>
      )}

      {result.year_fallback_reason && (
        <div className="rounded-md bg-amber-50 dark:bg-amber-950 p-3 text-xs text-amber-800 dark:text-amber-200">
          {result.year_fallback_reason}
        </div>
      )}

      {result.tax != null && (
        <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-5">
          <h2 className="font-medium mb-3">
            Your tax burden ({result.resolved_year})
          </h2>
          <p className="text-xs text-zinc-500 mb-3">
            Computed by the Phase-1 federal + NJ + FICA simulator for
            your profile. Effective rate is total tax ÷ gross income.
          </p>
          <dl className="grid grid-cols-2 sm:grid-cols-5 gap-3 text-sm">
            <KV label="Federal" value={fmtUsd(result.tax.federal)} />
            <KV label="NJ state" value={fmtUsd(result.tax.nj_state)} />
            <KV label="FICA" value={fmtUsd(result.tax.fica)} />
            <KV
              label="Total tax"
              value={fmtUsd(result.tax.total)}
              tone="warn"
            />
            <KV
              label="Effective rate"
              value={fmtPct(result.tax.effective_rate)}
            />
          </dl>
          <div className="mt-3 grid grid-cols-2 sm:grid-cols-3 gap-3 text-sm border-t border-zinc-200 dark:border-zinc-800 pt-3">
            <KV
              label="Gross income"
              value={fmtUsd(profile.gross_income)}
              tone="info"
            />
            <KV
              label="Take-home"
              value={fmtUsd(result.tax.take_home)}
              tone="ok"
            />
          </div>
        </section>
      )}

      {populatedCounties.length > 0 && (
        <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-5">
          <div className="flex items-baseline justify-between mb-2">
            <h2 className="font-medium">
              Per-county verdict ({populatedCounties.length} of{" "}
              {result.counties.length} counties have substrate)
            </h2>
            <div className="text-xs text-zinc-500 font-mono">
              {result.formula_version}
            </div>
          </div>
          <div className="mb-3 flex flex-wrap gap-3 text-xs">
            <Pill
              label={`${affordableCount} affordable`}
              bg="bg-emerald-100 dark:bg-emerald-950"
              fg="text-emerald-800 dark:text-emerald-200"
            />
            <Pill
              label={`${stretchCount} stretch`}
              bg="bg-amber-100 dark:bg-amber-950"
              fg="text-amber-800 dark:text-amber-200"
            />
            <Pill
              label={`${outOfReachCount} out of reach`}
              bg="bg-red-100 dark:bg-red-950"
              fg="text-red-800 dark:text-red-200"
            />
          </div>
          <p className="text-xs text-zinc-500 mb-3">
            Verdict bands: <strong>Affordable</strong> = median home ≤
            your max-affordable. <strong>Stretch</strong> = up to 25%
            over (HUD outreach materials).{" "}
            <strong>Out of reach</strong> = beyond that. Sorted by income
            gap (most affordable first).
          </p>
          <CountyTable
            rows={sortedCounties}
            drilldownFips={drilldownFips}
            profileQuery={profileToSearchParams(profile)}
          />
        </section>
      )}

      {drilldown != null && (
        <MuniDrilldownSection drilldown={drilldown} />
      )}

      <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-5">
        <h2 className="font-medium mb-2">Assumptions used</h2>
        <p className="text-xs text-zinc-500 mb-3">
          Per the verifiable-data rule, every defaulted constant the
          engine touched is listed here with its source. If you override
          any of these in the form above, your override takes
          precedence and that row will reflect the override (the
          citation still shows what the default <em>would</em> have been).
        </p>
        <ul className="space-y-2 text-xs">
          {result.assumptions.map((a) => (
            <li key={a.constant_id} className="border-l-2 border-zinc-300 dark:border-zinc-700 pl-3">
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
      </section>

      <div className="text-xs text-zinc-500">
        <Link href="/about" className="underline underline-offset-4">
          ← Methodology
        </Link>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Subcomponents
// ---------------------------------------------------------------------------

function ProfileForm({
  profile,
}: {
  profile: ReturnType<typeof parseProfileFromSearch>;
}) {
  // GET method so the URL fully encodes the profile (shareable + no
  // server-side state). Default values render whatever the user
  // currently has in their URL (or the page default on first load).
  return (
    <form
      method="GET"
      className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-5 space-y-4"
    >
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
        <Field label="Annual gross income">
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

        <Field label="Filing status">
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

        <Field label="Tax / data year">
          <input
            name="year"
            type="number"
            min="2010"
            max="2099"
            defaultValue={profile.year}
            className="form-input"
          />
        </Field>

        <Field label="Dependents (total)">
          <input
            name="deps"
            type="number"
            min="0"
            max="20"
            defaultValue={profile.dependents}
            className="form-input"
          />
        </Field>

        <Field label="Of which qualifying children (CTC)">
          <input
            name="kids"
            type="number"
            min="0"
            max="20"
            defaultValue={profile.qualifying_children}
            className="form-input"
          />
        </Field>

        <Field label="Other monthly debt ($/mo)">
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

      <details className="text-sm">
        <summary className="cursor-pointer text-zinc-600 dark:text-zinc-400">
          Mortgage assumptions (optional overrides)
        </summary>
        <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          <Field label="Down payment %">
            <input
              name="down"
              type="number"
              min="0"
              max="1"
              step="0.01"
              defaultValue={profile.down_pct ?? ""}
              placeholder="0.20 (default)"
              className="form-input"
            />
          </Field>
          <Field label="Term (years)">
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
          <Field label="DTI front cap">
            <input
              name="dtif"
              type="number"
              min="0"
              max="1"
              step="0.01"
              defaultValue={profile.dti_front ?? ""}
              placeholder="0.28 (Fannie)"
              className="form-input"
            />
          </Field>
          <Field label="DTI back cap">
            <input
              name="dtib"
              type="number"
              min="0"
              max="1"
              step="0.01"
              defaultValue={profile.dti_back ?? ""}
              placeholder="0.36 (Fannie)"
              className="form-input"
            />
          </Field>
          <Field label="Mortgage rate override (decimal)">
            <input
              name="rate"
              type="number"
              min="0"
              max="0.30"
              step="0.001"
              defaultValue={profile.rate_override ?? ""}
              placeholder="auto (FRED 30-yr)"
              className="form-input"
            />
          </Field>
        </div>
      </details>

      <div className="flex flex-wrap gap-3 items-center">
        <button
          type="submit"
          className="px-4 py-2 rounded-md bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900 text-sm font-medium hover:opacity-90"
        >
          Compute affordability
        </button>
        <a
          href="/personalize"
          className="text-sm text-zinc-600 dark:text-zinc-400 underline underline-offset-4"
        >
          Reset to defaults
        </a>
        <span className="text-xs text-zinc-500 ml-auto">
          Profile lives in the URL — share or bookmark this page to save.
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
        }
        .dark .form-input {
          background-color: rgb(24 24 27);
          color: rgb(244 244 245);
          border-color: rgb(63 63 70);
        }
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
    <label className="block text-sm">
      <span className="block text-xs uppercase tracking-wider text-zinc-500 mb-1">
        {label}
      </span>
      {children}
    </label>
  );
}

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
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs uppercase tracking-wider text-zinc-500 border-b border-zinc-200 dark:border-zinc-800">
            <th className="py-2 pr-3">County</th>
            <th className="py-2 pr-3 text-right">Median home</th>
            <th className="py-2 pr-3 text-right">Your max (DTI)</th>
            <th className="py-2 pr-3 text-right">Income gap</th>
            <th className="py-2 pr-3 text-right">Burden</th>
            <th className="py-2 pr-3">Verdict (DTI)</th>
            <th className="py-2 pr-3">Verdict (post-tax)</th>
            <th className="py-2 pr-3">Drill down</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((c) => {
            const tone = verdictTone(c.verdict_dti);
            const ptt = verdictTone(c.verdict_post_tax);
            const gapClass =
              c.gross_income_gap == null
                ? "text-zinc-500"
                : c.gross_income_gap <= 0
                  ? "text-emerald-700 dark:text-emerald-300"
                  : "text-red-700 dark:text-red-300";
            const isActive = drilldownFips === c.county_fips;
            // Build a target URL that preserves the user's full
            // profile but flips the county filter on/off. Anchor to
            // #towns so the page jumps to the drilldown after navigation.
            const drilldownHref = isActive
              ? `/personalize?${profileQuery}`
              : `/personalize?${profileQuery}${profileQuery ? "&" : ""}county=${c.county_fips}#towns`;
            return (
              <tr
                key={c.county_fips}
                className={`border-b border-zinc-100 dark:border-zinc-900 ${
                  isActive
                    ? "bg-zinc-50 dark:bg-zinc-950 border-l-2 border-l-blue-500"
                    : ""
                }`}
              >
                <td className="py-2 pr-3 font-medium">{c.county_name}</td>
                <td className="py-2 pr-3 font-mono text-right">
                  {fmtUsd(c.median_home_price)}
                </td>
                <td className="py-2 pr-3 font-mono text-right">
                  {fmtUsd(c.max_affordable_dti)}
                </td>
                <td className={`py-2 pr-3 font-mono text-right ${gapClass}`}>
                  {c.gross_income_gap == null
                    ? "—"
                    : c.gross_income_gap <= 0
                      ? `−${fmtUsd(Math.abs(c.gross_income_gap))}`
                      : `+${fmtUsd(c.gross_income_gap)}`}
                </td>
                <td className="py-2 pr-3 font-mono text-right">
                  {fmtPct(c.personal_burden_ratio)}
                </td>
                <td className="py-2 pr-3">
                  <span
                    className={`inline-block px-2 py-0.5 rounded text-xs ${tone.bg} ${tone.fg}`}
                  >
                    {tone.label}
                  </span>
                </td>
                <td className="py-2 pr-3">
                  <span
                    className={`inline-block px-2 py-0.5 rounded text-xs ${ptt.bg} ${ptt.fg}`}
                  >
                    {ptt.label}
                  </span>
                </td>
                <td className="py-2 pr-3 text-xs">
                  {c.median_home_price == null ? (
                    <span className="text-zinc-400">—</span>
                  ) : (
                    <Link
                      href={drilldownHref}
                      className="text-blue-700 dark:text-blue-300 underline underline-offset-2"
                    >
                      {isActive ? "Hide towns" : "View towns →"}
                    </Link>
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

function MuniDrilldownSection({
  drilldown,
}: {
  drilldown: NonNullable<Awaited<ReturnType<typeof runMuniVerdicts>>>;
}) {
  const populated = drilldown.munis.filter(
    (m) => m.median_home_price != null,
  );
  const sorted = [...drilldown.munis].sort((a, b) => {
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
      <div className="flex items-baseline justify-between mb-2">
        <h2 className="font-medium">
          {drilldown.county_name == null
            ? `Town breakdown (county FIPS ${drilldown.county_fips})`
            : `Town breakdown — ${drilldown.county_name} County`}
          {populated.length > 0 && (
            <span className="text-xs text-zinc-500 ml-2 font-normal">
              ({populated.length} of {drilldown.munis.length} towns have
              substrate)
            </span>
          )}
        </h2>
        <div className="text-xs text-zinc-500 font-mono">
          {drilldown.formula_version}
        </div>
      </div>
      {drilldown.unknown_county ? (
        <p className="text-sm text-amber-700 dark:text-amber-300">
          Unknown county FIPS. Use the &ldquo;View towns&rdquo; link from the
          county table above.
        </p>
      ) : drilldown.munis.length === 0 ? (
        <p className="text-sm text-zinc-500">
          No municipalities found for this county. The Phase 8a
          ref.nj_municipality dimension may not be seeded.
        </p>
      ) : populated.length === 0 ? (
        <p className="text-sm text-amber-700 dark:text-amber-300">
          No NJ DCA municipal property-tax substrate loaded for this
          county/year. Try year=2024.
        </p>
      ) : (
        <>
          <div className="mb-3 flex flex-wrap gap-3 text-xs">
            <Pill
              label={`${affordable} affordable`}
              bg="bg-emerald-100 dark:bg-emerald-950"
              fg="text-emerald-800 dark:text-emerald-200"
            />
            <Pill
              label={`${stretch} stretch`}
              bg="bg-amber-100 dark:bg-amber-950"
              fg="text-amber-800 dark:text-amber-200"
            />
            <Pill
              label={`${oor} out of reach`}
              bg="bg-red-100 dark:bg-red-950"
              fg="text-red-800 dark:text-red-200"
            />
          </div>
          <p className="text-xs text-zinc-500 mb-3">
            Per-municipality verdicts using NJ DCA muni-level average home
            value &times; muni-level effective property tax rate. Same
            engine as the county table; only the geography is finer.
          </p>
          <MuniTable rows={sorted} />
        </>
      )}
    </section>
  );
}

function MuniTable({ rows }: { rows: MuniVerdictRow[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs uppercase tracking-wider text-zinc-500 border-b border-zinc-200 dark:border-zinc-800">
            <th className="py-2 pr-3">Town</th>
            <th className="py-2 pr-3 text-right">Avg home</th>
            <th className="py-2 pr-3 text-right">Your max (DTI)</th>
            <th className="py-2 pr-3 text-right">Income gap</th>
            <th className="py-2 pr-3 text-right">Burden</th>
            <th className="py-2 pr-3">Verdict (DTI)</th>
            <th className="py-2 pr-3">Verdict (post-tax)</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((m) => {
            const tone = verdictTone(m.verdict_dti);
            const ptt = verdictTone(m.verdict_post_tax);
            const gapClass =
              m.gross_income_gap == null
                ? "text-zinc-500"
                : m.gross_income_gap <= 0
                  ? "text-emerald-700 dark:text-emerald-300"
                  : "text-red-700 dark:text-red-300";
            return (
              <tr
                key={m.muni_code}
                className="border-b border-zinc-100 dark:border-zinc-900"
              >
                <td className="py-2 pr-3 font-medium">
                  {m.muni_name}
                  <span className="text-zinc-400 font-mono text-xs ml-1.5">
                    [{m.muni_code}]
                  </span>
                </td>
                <td className="py-2 pr-3 font-mono text-right">
                  {fmtUsd(m.median_home_price)}
                </td>
                <td className="py-2 pr-3 font-mono text-right">
                  {fmtUsd(m.max_affordable_dti)}
                </td>
                <td className={`py-2 pr-3 font-mono text-right ${gapClass}`}>
                  {m.gross_income_gap == null
                    ? "—"
                    : m.gross_income_gap <= 0
                      ? `−${fmtUsd(Math.abs(m.gross_income_gap))}`
                      : `+${fmtUsd(m.gross_income_gap)}`}
                </td>
                <td className="py-2 pr-3 font-mono text-right">
                  {fmtPct(m.personal_burden_ratio)}
                </td>
                <td className="py-2 pr-3">
                  <span
                    className={`inline-block px-2 py-0.5 rounded text-xs ${tone.bg} ${tone.fg}`}
                  >
                    {tone.label}
                  </span>
                </td>
                <td className="py-2 pr-3">
                  <span
                    className={`inline-block px-2 py-0.5 rounded text-xs ${ptt.bg} ${ptt.fg}`}
                  >
                    {ptt.label}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
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

function Pill({
  label,
  bg,
  fg,
}: {
  label: string;
  bg: string;
  fg: string;
}) {
  return (
    <span
      className={`inline-flex items-center px-2.5 py-1 rounded-full text-xs font-medium ${bg} ${fg}`}
    >
      {label}
    </span>
  );
}

// Note: DEFAULT_PROFILE is exported for test+stability; suppress unused-import warning.
void DEFAULT_PROFILE;
