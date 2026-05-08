/**
 * Personalized Affordability Engine -- frontend data layer.
 *
 * Wraps the Phase-4 SQL functions (migration 074) into typed
 * TypeScript surfaces consumed by /personalize.
 *
 * Architectural posture: ALL SQL is in migration 074. This file
 * issues one read-only set-returning function call per request and
 * casts the rows to typed objects. Zero business logic on the JS
 * side; the SQL is the source of truth for every dollar amount.
 *
 * Privacy posture: the household profile is a CLIENT-SIDE construct
 * passed to the page as URL query params. The server reads the
 * profile once per request, calls the engine, returns rows. No
 * profile is persisted server-side. No accounts, no session, no PII.
 */

import { getSql } from "./db";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type FilingStatus = "single" | "mfj" | "mfs" | "hoh" | "qss";

export const FILING_STATUS_LABEL: Record<FilingStatus, string> = {
  single: "Single",
  mfj: "Married filing jointly",
  mfs: "Married filing separately",
  hoh: "Head of household",
  qss: "Qualifying surviving spouse",
};

/**
 * The user's household profile. Every field has a sensible default
 * sourced from the spec or ref.affordability_assumptions; the page
 * exposes them as form inputs so the user can override.
 */
export type HouseholdProfile = {
  /** Annual gross household income in dollars. Required. */
  gross_income: number;
  filing_status: FilingStatus;
  dependents: number;
  qualifying_children: number;
  /** Monthly debt service for car loans, student loans, credit cards. */
  other_monthly_debt: number;
  /** Down-payment percent as decimal (0.20 = 20%). null = use default. */
  down_pct: number | null;
  term_years: 15 | 30 | null;
  dti_front: number | null;
  dti_back: number | null;
  /** Optional rate override for counterfactual sliders (0.07 = 7%). */
  rate_override: number | null;
  /** The tax-year and substrate-year to evaluate against. */
  year: number;
};

export const DEFAULT_PROFILE: HouseholdProfile = {
  gross_income: 100_000,
  filing_status: "single",
  dependents: 0,
  qualifying_children: 0,
  other_monthly_debt: 0,
  down_pct: null,
  term_years: null,
  dti_front: null,
  dti_back: null,
  rate_override: null,
  year: 2024,
};

export type CountyVerdictRow = {
  county_id: string;
  county_fips: string;
  county_name: string;
  median_home_price: number | null;
  max_affordable_dti: number | null;
  max_affordable_post_tax: number | null;
  piti_on_median: number | null;
  required_gross_for_median: number | null;
  user_take_home: number | null;
  personal_burden_ratio: number | null;
  personal_burden_ratio_post_tax: number | null;
  verdict_dti: "affordable" | "stretch" | "out_of_reach" | null;
  verdict_post_tax: "affordable" | "stretch" | "out_of_reach" | null;
  gross_income_gap: number | null;
};

/**
 * Per-muni verdict row, returned by f_user_nj_muni_verdicts.
 * Same shape as CountyVerdictRow but keyed by 4-digit DCA muni_code +
 * muni_name + county_fips. The Phase 8a substrate; rendered on
 * /personalize when ?county=<fips> is set.
 */
export type MuniVerdictRow = {
  muni_code: string;
  muni_name: string;
  county_fips: string;
  median_home_price: number | null;
  max_affordable_dti: number | null;
  max_affordable_post_tax: number | null;
  piti_on_median: number | null;
  required_gross_for_median: number | null;
  user_take_home: number | null;
  personal_burden_ratio: number | null;
  personal_burden_ratio_post_tax: number | null;
  verdict_dti: "affordable" | "stretch" | "out_of_reach" | null;
  verdict_post_tax: "affordable" | "stretch" | "out_of_reach" | null;
  gross_income_gap: number | null;
};

export type PersonalizationResult = {
  profile: HouseholdProfile;
  /** Federal + NJ + FICA tax breakdown for the user's profile. */
  tax: {
    federal: number;
    nj_state: number;
    fica: number;
    total: number;
    effective_rate: number;
    take_home: number;
  } | null;
  /** Per-county verdict rows. May be empty when substrate missing. */
  counties: CountyVerdictRow[];
  /** Citation rows for every default constant the engine used. */
  assumptions: AssumptionCitation[];
  formula_version: string;
  /** Most recent year for which the engine has full substrate. */
  resolved_year: number;
  /** Year that fell back to default because user-requested was unavailable. */
  year_fallback_reason: string | null;
};

export type AssumptionCitation = {
  constant_id: string;
  value_numeric: number;
  unit: string;
  source_url: string;
  source_citation: string;
  effective_year: number;
};

// ---------------------------------------------------------------------------
// URL <-> Profile encoding (server-side, deterministic)
//
// The page is rendered server-side and the form posts via GET so the
// URL fully encodes the profile. This makes the result page shareable
// without persisting anything server-side.
// ---------------------------------------------------------------------------

const VALID_FILING: ReadonlySet<string> = new Set([
  "single", "mfj", "mfs", "hoh", "qss",
]);

function num(v: string | null, fallback: number | null): number | null {
  if (v == null || v === "") return fallback;
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

function clampInt(v: number | null, lo: number, hi: number): number {
  if (v == null || !Number.isFinite(v)) return lo;
  return Math.max(lo, Math.min(hi, Math.floor(v)));
}

/**
 * Parse a URLSearchParams-shaped object (or a Next.js searchParams
 * proxy) into a HouseholdProfile. Sanitizes every input -- a hostile
 * URL can't crash the server or trigger an SQL error.
 */
export function parseProfileFromSearch(
  raw: Record<string, string | string[] | undefined>,
): HouseholdProfile {
  const get = (k: string): string | null => {
    const v = raw[k];
    if (Array.isArray(v)) return v[0] ?? null;
    return v ?? null;
  };

  const gross = num(get("gross"), DEFAULT_PROFILE.gross_income) ?? 0;
  const filing = get("filing") ?? DEFAULT_PROFILE.filing_status;
  const filing_status = (
    VALID_FILING.has(filing) ? filing : DEFAULT_PROFILE.filing_status
  ) as FilingStatus;

  const term_raw = num(get("term"), null);
  const term_years: 15 | 30 | null =
    term_raw === 15 ? 15 : term_raw === 30 ? 30 : null;

  return {
    gross_income: Math.max(0, gross),
    filing_status,
    dependents: clampInt(num(get("deps"), 0), 0, 20),
    qualifying_children: clampInt(num(get("kids"), 0), 0, 20),
    other_monthly_debt: Math.max(0, num(get("debt"), 0) ?? 0),
    down_pct: num(get("down"), null),
    term_years,
    dti_front: num(get("dtif"), null),
    dti_back: num(get("dtib"), null),
    rate_override: num(get("rate"), null),
    year: clampInt(num(get("year"), DEFAULT_PROFILE.year), 2010, 2099),
  };
}

/**
 * Serialize a HouseholdProfile back to a URLSearchParams string for
 * shareable URLs. Only emits non-default fields so the URL stays short.
 */
export function profileToSearchParams(p: HouseholdProfile): string {
  const u = new URLSearchParams();
  u.set("gross", String(p.gross_income));
  u.set("filing", p.filing_status);
  if (p.dependents !== 0) u.set("deps", String(p.dependents));
  if (p.qualifying_children !== 0) u.set("kids", String(p.qualifying_children));
  if (p.other_monthly_debt !== 0) u.set("debt", String(p.other_monthly_debt));
  if (p.down_pct != null) u.set("down", String(p.down_pct));
  if (p.term_years != null) u.set("term", String(p.term_years));
  if (p.dti_front != null) u.set("dtif", String(p.dti_front));
  if (p.dti_back != null) u.set("dtib", String(p.dti_back));
  if (p.rate_override != null) u.set("rate", String(p.rate_override));
  if (p.year !== DEFAULT_PROFILE.year) u.set("year", String(p.year));
  return u.toString();
}

// ---------------------------------------------------------------------------
// SQL queries
// ---------------------------------------------------------------------------

/**
 * Run the personalization engine for a profile. Returns the per-county
 * verdict table + the user's tax breakdown + the citation rows for
 * every assumption used. Single round trip per top-level fetch
 * (verdicts + tax + assumptions executed in parallel via Promise.all).
 */
export async function runPersonalizationEngine(
  profile: HouseholdProfile,
): Promise<PersonalizationResult> {
  const sql = getSql();

  // The engine functions take parameters that map 1:1 to the profile;
  // SQL types are SMALLINT/CHAR(5)/NUMERIC/TEXT/INT and the Neon HTTP
  // client coerces. We emit FLOAT8 for all numeric outputs so JS
  // receives numbers not Decimal strings.
  const [verdictRows, taxRows] = await Promise.all([
    sql`
      SELECT
        county_id,
        county_fips,
        county_name,
        median_home_price::FLOAT8                AS median_home_price,
        max_affordable_dti::FLOAT8               AS max_affordable_dti,
        max_affordable_post_tax::FLOAT8          AS max_affordable_post_tax,
        piti_on_median::FLOAT8                   AS piti_on_median,
        required_gross_for_median::FLOAT8        AS required_gross_for_median,
        user_take_home::FLOAT8                   AS user_take_home,
        personal_burden_ratio::FLOAT8            AS personal_burden_ratio,
        personal_burden_ratio_post_tax::FLOAT8   AS personal_burden_ratio_post_tax,
        verdict_dti,
        verdict_post_tax,
        gross_income_gap::FLOAT8                 AS gross_income_gap,
        formula_version
      FROM derived.f_user_nj_county_verdicts(
        ${profile.year}::SMALLINT,
        ${profile.gross_income}::NUMERIC,
        ${profile.filing_status}::TEXT,
        ${profile.dependents}::INT,
        ${profile.qualifying_children}::INT,
        ${profile.other_monthly_debt}::NUMERIC,
        ${profile.dti_front}::NUMERIC,
        ${profile.dti_back}::NUMERIC,
        ${profile.down_pct}::NUMERIC,
        ${profile.term_years}::INT,
        NULL::NUMERIC,                  -- insurance rate (default from assumption)
        ${profile.rate_override}::NUMERIC
      )
    `,
    sql`
      SELECT
        federal_income_tax::FLOAT8  AS federal,
        nj_state_tax::FLOAT8        AS nj_state,
        fica_tax::FLOAT8            AS fica,
        total_tax::FLOAT8           AS total_tax,
        effective_rate::FLOAT8      AS effective_rate
      FROM derived.f_household_taxes(
        ${profile.gross_income}::NUMERIC,
        ${profile.gross_income}::NUMERIC,
        ${profile.year}::SMALLINT,
        ${profile.filing_status}::TEXT,
        ${profile.dependents}::INT,
        ${profile.qualifying_children}::INT,
        0::NUMERIC
      )
    `,
  ]);

  const counties: CountyVerdictRow[] = (verdictRows as Record<string, unknown>[]).map(
    (r) => ({
      county_id: String(r.county_id),
      county_fips: String(r.county_fips),
      county_name: String(r.county_name),
      median_home_price: r.median_home_price == null ? null : Number(r.median_home_price),
      max_affordable_dti:
        r.max_affordable_dti == null ? null : Number(r.max_affordable_dti),
      max_affordable_post_tax:
        r.max_affordable_post_tax == null ? null : Number(r.max_affordable_post_tax),
      piti_on_median: r.piti_on_median == null ? null : Number(r.piti_on_median),
      required_gross_for_median:
        r.required_gross_for_median == null ? null : Number(r.required_gross_for_median),
      user_take_home: r.user_take_home == null ? null : Number(r.user_take_home),
      personal_burden_ratio:
        r.personal_burden_ratio == null ? null : Number(r.personal_burden_ratio),
      personal_burden_ratio_post_tax:
        r.personal_burden_ratio_post_tax == null
          ? null
          : Number(r.personal_burden_ratio_post_tax),
      verdict_dti:
        r.verdict_dti == null
          ? null
          : (String(r.verdict_dti) as CountyVerdictRow["verdict_dti"]),
      verdict_post_tax:
        r.verdict_post_tax == null
          ? null
          : (String(r.verdict_post_tax) as CountyVerdictRow["verdict_post_tax"]),
      gross_income_gap: r.gross_income_gap == null ? null : Number(r.gross_income_gap),
    }),
  );

  let tax: PersonalizationResult["tax"] = null;
  if (taxRows.length > 0) {
    const t = (taxRows as Record<string, unknown>[])[0];
    if (t.total_tax != null) {
      const total = Number(t.total_tax);
      tax = {
        federal: t.federal == null ? 0 : Number(t.federal),
        nj_state: t.nj_state == null ? 0 : Number(t.nj_state),
        fica: t.fica == null ? 0 : Number(t.fica),
        total,
        effective_rate: t.effective_rate == null ? 0 : Number(t.effective_rate),
        take_home: profile.gross_income - total,
      };
    }
  }

  // Pull citations for every assumption the engine relies on. Read
  // separately (and unparametrized over the profile) so it can be
  // cached aggressively if we ever want to.
  const assumptionRows = (await sql`
    SELECT
      constant_id,
      value_numeric::FLOAT8 AS value_numeric,
      unit,
      source_url,
      source_citation,
      effective_year::INT   AS effective_year
    FROM ref.affordability_assumptions
    WHERE constant_id IN (
      'mortgage_default_down_pct',
      'mortgage_default_term_years',
      'homeowners_insurance_annual_rate_default',
      'dti_front_end_cap_conventional',
      'dti_back_end_cap_conventional',
      'affordability_stretch_multiplier',
      'affordability_threshold_pct'
    )
      AND effective_year <= ${profile.year}
    ORDER BY constant_id, effective_year DESC
  `) as Record<string, unknown>[];

  // Take the freshest effective_year per constant_id (the f_assumption
  // resolver does this in SQL; we mirror it for the citation list).
  const seen = new Set<string>();
  const assumptions: AssumptionCitation[] = [];
  for (const r of assumptionRows) {
    const id = String(r.constant_id);
    if (seen.has(id)) continue;
    seen.add(id);
    assumptions.push({
      constant_id: id,
      value_numeric: Number(r.value_numeric),
      unit: String(r.unit),
      source_url: String(r.source_url),
      source_citation: String(r.source_citation),
      effective_year: Number(r.effective_year),
    });
  }

  // Detect substrate gap for the requested year. If every county has
  // NULL median_home_price, the page should suggest an earlier year.
  const populatedCount = counties.filter((c) => c.median_home_price != null).length;
  const fallbackReason =
    populatedCount === 0
      ? `No NJ DCA property-tax substrate loaded for ${profile.year}. ` +
        "Try year=2024."
      : null;

  const formulaVersion =
    counties.length > 0 ? "1.4.0-personalization-engine-v1" : "1.4.0-personalization-engine-v1";

  return {
    profile,
    tax,
    counties,
    assumptions,
    formula_version: formulaVersion,
    resolved_year: profile.year,
    year_fallback_reason: fallbackReason,
  };
}

// ---------------------------------------------------------------------------
// Phase 8c: muni-level drilldown
// ---------------------------------------------------------------------------

export type MuniDrilldownResult = {
  county_fips: string;
  county_name: string | null;
  munis: MuniVerdictRow[];
  formula_version: string;
  /** True when the requested county_fips is not a known NJ county. */
  unknown_county: boolean;
};

/**
 * Run the muni-level engine for one NJ county. Returns one verdict row
 * per muni in that county (a Bergen call returns 70 rows; Salem 15;
 * etc.). Substrate-honest: munis whose 2024 DCA workbook row is missing
 * surface NULL across home/max/verdict columns.
 *
 * Why scoped to one county at a time: emitting all 564 NJ munis on a
 * single request would call f_household_taxes 564 times via the
 * CROSS JOIN LATERAL in f_user_nj_muni_verdicts (the tax engine alone
 * is a few ms but x564 dominates). Scoping by county keeps p99 page
 * latency acceptable (Bergen at 70 munis is the worst case).
 */
export async function runMuniVerdicts(
  profile: HouseholdProfile,
  countyFips: string,
): Promise<MuniDrilldownResult> {
  // Sanitize: a hostile URL can't trigger an SQL error or leak rows.
  // The CHECK constraint on ref.county.county_fips already requires
  // 5 digits; we mirror that here so a malformed param short-circuits
  // before the SQL call.
  const cleanFips = /^\d{5}$/.test(countyFips) ? countyFips : "";
  if (!cleanFips) {
    return {
      county_fips: countyFips,
      county_name: null,
      munis: [],
      formula_version: "1.5.0-municipality-drill-down-v1",
      unknown_county: true,
    };
  }

  const sql = getSql();

  const [muniRows, countyRows] = await Promise.all([
    sql`
      SELECT
        muni_code,
        muni_name,
        county_fips,
        median_home_price::FLOAT8                AS median_home_price,
        max_affordable_dti::FLOAT8               AS max_affordable_dti,
        max_affordable_post_tax::FLOAT8          AS max_affordable_post_tax,
        piti_on_median::FLOAT8                   AS piti_on_median,
        required_gross_for_median::FLOAT8        AS required_gross_for_median,
        user_take_home::FLOAT8                   AS user_take_home,
        personal_burden_ratio::FLOAT8            AS personal_burden_ratio,
        personal_burden_ratio_post_tax::FLOAT8   AS personal_burden_ratio_post_tax,
        verdict_dti,
        verdict_post_tax,
        gross_income_gap::FLOAT8                 AS gross_income_gap,
        formula_version
      FROM derived.f_user_nj_muni_verdicts(
        ${profile.year}::SMALLINT,
        ${cleanFips}::CHAR(5),
        ${profile.gross_income}::NUMERIC,
        ${profile.filing_status}::TEXT,
        ${profile.dependents}::INT,
        ${profile.qualifying_children}::INT,
        ${profile.other_monthly_debt}::NUMERIC,
        ${profile.dti_front}::NUMERIC,
        ${profile.dti_back}::NUMERIC,
        ${profile.down_pct}::NUMERIC,
        ${profile.term_years}::INT,
        NULL::NUMERIC,
        ${profile.rate_override}::NUMERIC
      )
    `,
    sql`
      SELECT name FROM ref.county
      WHERE county_fips = ${cleanFips} AND state_code = 'NJ'
      LIMIT 1
    `,
  ]);

  const munis: MuniVerdictRow[] = (muniRows as Record<string, unknown>[]).map(
    (r) => ({
      muni_code: String(r.muni_code),
      muni_name: String(r.muni_name),
      county_fips: String(r.county_fips),
      median_home_price:
        r.median_home_price == null ? null : Number(r.median_home_price),
      max_affordable_dti:
        r.max_affordable_dti == null ? null : Number(r.max_affordable_dti),
      max_affordable_post_tax:
        r.max_affordable_post_tax == null ? null : Number(r.max_affordable_post_tax),
      piti_on_median: r.piti_on_median == null ? null : Number(r.piti_on_median),
      required_gross_for_median:
        r.required_gross_for_median == null ? null : Number(r.required_gross_for_median),
      user_take_home: r.user_take_home == null ? null : Number(r.user_take_home),
      personal_burden_ratio:
        r.personal_burden_ratio == null ? null : Number(r.personal_burden_ratio),
      personal_burden_ratio_post_tax:
        r.personal_burden_ratio_post_tax == null
          ? null
          : Number(r.personal_burden_ratio_post_tax),
      verdict_dti:
        r.verdict_dti == null
          ? null
          : (String(r.verdict_dti) as MuniVerdictRow["verdict_dti"]),
      verdict_post_tax:
        r.verdict_post_tax == null
          ? null
          : (String(r.verdict_post_tax) as MuniVerdictRow["verdict_post_tax"]),
      gross_income_gap:
        r.gross_income_gap == null ? null : Number(r.gross_income_gap),
    }),
  );

  const countyName =
    countyRows.length > 0
      ? String((countyRows as Record<string, unknown>[])[0].name)
      : null;

  return {
    county_fips: cleanFips,
    county_name: countyName,
    munis,
    formula_version: "1.5.0-municipality-drill-down-v1",
    unknown_county: countyName == null,
  };
}

// ---------------------------------------------------------------------------
// Display helpers
// ---------------------------------------------------------------------------

export function fmtUsd(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return `$${Math.round(n).toLocaleString("en-US")}`;
}

export function fmtPct(n: number | null | undefined, dp = 1): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return `${(n * 100).toFixed(dp)}%`;
}

export function verdictTone(
  v: CountyVerdictRow["verdict_dti"] | MuniVerdictRow["verdict_dti"],
): { label: string; bg: string; fg: string } {
  if (v === "affordable")
    return {
      label: "Affordable",
      bg: "bg-emerald-100 dark:bg-emerald-950",
      fg: "text-emerald-800 dark:text-emerald-200",
    };
  if (v === "stretch")
    return {
      label: "Stretch",
      bg: "bg-amber-100 dark:bg-amber-950",
      fg: "text-amber-800 dark:text-amber-200",
    };
  if (v === "out_of_reach")
    return {
      label: "Out of reach",
      bg: "bg-red-100 dark:bg-red-950",
      fg: "text-red-800 dark:text-red-200",
    };
  return {
    label: "—",
    bg: "bg-zinc-100 dark:bg-zinc-900",
    fg: "text-zinc-600 dark:text-zinc-400",
  };
}
