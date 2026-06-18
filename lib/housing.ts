/**
 * Housing-affordability data layer.
 *
 * The platform stores nominal HPI and ACS median household income
 * separately. This module joins them into a single "burden divergence"
 * series per county: HPI growth divided by real-income growth, both
 * re-indexed to a common base year. Values >1.0 mean housing is
 * outpacing wages; <1.0 means wages are keeping up.
 *
 * Why this metric and not the PUMS-burden share
 * ---------------------------------------------
 * The PUMS-burden views (cost > 30% of income) are richer but also
 * tenure-segmented and bucket-segmented; they are appropriate for
 * a deeper "housing methodology" page. For the screener overview,
 * a single divergence ratio per county compresses the entire
 * "is NJ unaffordable?" question into one comparable number, which
 * is the right shape for a top-level table.
 *
 * Base year: read at runtime from ref.platform_constants.burden_base_year
 * (currently 2010). Both HPI and income are normalized to base_year=100
 * so the ratio at the base year is always 1.0 by construction; subsequent
 * years move freely. Per VISION_2026 §3 punch-list, the base year is no
 * longer a code-level magic number.
 */

import { getSql } from "./db";

/**
 * Per-process cache of the burden base year. Loaded from
 * ref.platform_constants on first read, then reused for the lifetime
 * of the process. Vercel cold-starts get a fresh cache; warm
 * invocations within a few minutes reuse it.
 */
let _baseYearCache: number | null = null;

/**
 * Returns the active burden_base_year from ref.platform_constants.
 * Throws if the row is missing -- the platform cannot operate without
 * this constant and the failure should be loud at the next page render.
 *
 * VISION_2026 §3 punch-list: replaces the `BURDEN_BASE_YEAR = 2010`
 * literal with a verifiable, version-stamped lookup. The value is
 * (still) 2010 today but every read is now traceable to migration 080
 * (ref.platform_constants) + seed 014 + formula version
 * 1.7.0-platform-constants-v1.
 */
export async function getBurdenBaseYear(): Promise<number> {
  if (_baseYearCache != null) return _baseYearCache;
  const sql = getSql();
  const rows = (await sql`
    SELECT derived.f_platform_constant('burden_base_year')::INT AS y
  `) as { y: number | null }[];
  if (rows.length === 0 || rows[0].y == null) {
    throw new Error(
      "ref.platform_constants is missing 'burden_base_year'. Apply migration 080 + seed 014.",
    );
  }
  _baseYearCache = Number(rows[0].y);
  return _baseYearCache;
}

/**
 * Tier band as stored in ref.tier_bands. The (lower_bound, upper_bound)
 * pair forms the half-open range [lower, upper); NULL bounds are
 * unbounded on that side.
 */
export type TierBand = {
  band_ord: number;
  label: string;
  description: string;
  severity_rank: number;
  lower_bound: number | null;
  upper_bound: number | null;
  ui_bg_classes: string;
  ui_fg_classes: string;
  citation_text: string;
  formula_version: string;
};

let _burdenTierBandsCache: TierBand[] | null = null;

/**
 * Returns the active burden_growth_ratio tier bands sorted by band_ord.
 * Cached per-process. Throws if the table is empty -- the platform
 * cannot classify counties without these.
 *
 * Pulls from the LATEST formula_version that has effective_date <=
 * CURRENT_DATE; older versions stay readable for historical audit
 * (see derived.f_tier_band's source).
 */
export async function getBurdenTierBands(): Promise<TierBand[]> {
  if (_burdenTierBandsCache != null) return _burdenTierBandsCache;
  const sql = getSql();
  const rows = (await sql`
    WITH active_version AS (
      SELECT formula_version
      FROM ref.tier_bands
      WHERE tier_kind = 'burden_growth_ratio'
        AND effective_date <= CURRENT_DATE
      ORDER BY effective_date DESC, formula_version DESC
      LIMIT 1
    )
    SELECT
      tb.band_ord::INT                          AS band_ord,
      tb.label                                  AS label,
      tb.description                            AS description,
      tb.severity_rank::INT                     AS severity_rank,
      tb.lower_bound::FLOAT8                    AS lower_bound,
      tb.upper_bound::FLOAT8                    AS upper_bound,
      tb.ui_bg_classes                          AS ui_bg_classes,
      tb.ui_fg_classes                          AS ui_fg_classes,
      tb.citation_text                          AS citation_text,
      tb.formula_version                        AS formula_version
    FROM ref.tier_bands tb
    JOIN active_version v ON v.formula_version = tb.formula_version
    WHERE tb.tier_kind = 'burden_growth_ratio'
    ORDER BY tb.band_ord
  `) as Record<string, unknown>[];
  if (rows.length === 0) {
    throw new Error(
      "ref.tier_bands is missing burden_growth_ratio rows. Apply migration 081 + seed 015.",
    );
  }
  _burdenTierBandsCache = rows.map((r) => ({
    band_ord: Number(r.band_ord),
    label: String(r.label),
    description: String(r.description),
    severity_rank: Number(r.severity_rank),
    lower_bound: r.lower_bound == null ? null : Number(r.lower_bound),
    upper_bound: r.upper_bound == null ? null : Number(r.upper_bound),
    ui_bg_classes: String(r.ui_bg_classes ?? ""),
    ui_fg_classes: String(r.ui_fg_classes ?? ""),
    citation_text: String(r.citation_text),
    formula_version: String(r.formula_version),
  }));
  return _burdenTierBandsCache;
}

export type CountyRow = {
  county_id: string;
  county_fips: string;
  county_name: string;
};

export type CountyBurdenRow = CountyRow & {
  /** Most recent year for which BOTH HPI and income are present. */
  year_latest: number | null;
  /** HPI(latest) / HPI(base), rounded to 4 dp. NULL when missing. */
  hpi_growth: number | null;
  /** real_income(latest) / real_income(base), rounded to 4 dp. */
  income_growth: number | null;
  /** hpi_growth / income_growth. NULL when either factor missing. */
  burden_ratio: number | null;
  /** real-dollar median household income for year_latest. */
  median_income_real_latest: number | null;
  /** Indexed HPI value for year_latest (base=100). */
  hpi_indexed_latest: number | null;
};

/**
 * One NJ municipality's affordability snapshot for the latest year in
 * derived.v_muni_affordability_gap. Unlike the county burden ratio (an
 * index divergence), this is dollar-denominated: the income HUD's 30%
 * rule says you need to afford the town's average home, vs the county
 * median income. The view bakes the representative profile (MFJ, 1
 * dependent, 1 qualifying child) — the same one the landing page cites.
 */
export type MuniBurdenRow = {
  muni_code: string;
  muni_name: string;
  county_fips: string;
  county_name: string;
  year: number;
  /** DCA average residential home price for the town. */
  home_price: number | null;
  /** County median household income (nominal) — the income denominator. */
  median_income: number | null;
  /** HUD-required income to afford the town's avg home at 30% of gross. */
  required_income: number | null;
  /** median_income − required_income; negative ⇒ unaffordable. */
  headroom: number | null;
  /** required_income / median_income; >1 ⇒ unaffordable. */
  ratio: number | null;
};

export type SeriesPoint = {
  year: number;
  /** Indexed value (HPI or income), base=100 at BURDEN_BASE_YEAR. */
  indexed: number;
};

export type CountyDetail = CountyRow & {
  base_year: number;
  hpi_series: SeriesPoint[];
  /**
   * Zillow ZHVI series re-indexed to base_year=100. Phase 6 substrate
   * (raw.zillow_zhvi_county) joined annually via derived.f_zhvi_county_indexed.
   * Independent of FHFA HPI; the two series together implement the
   * spec §8.1 cross-source validation surface end-to-end.
   */
  zhvi_series: SeriesPoint[];
  income_series_real: SeriesPoint[];
  burden_series: SeriesPoint[];
  current: {
    year: number | null;
    hpi_growth: number | null;
    income_growth: number | null;
    burden_ratio: number | null;
    median_income_real: number | null;
  };
  /**
   * Cross-source housing-index posture for the most recent year where
   * BOTH FHFA HPI and Zillow ZHVI have data for this county. NULL when
   * either source is missing for every joinable year.
   */
  cross_source: {
    year: number;
    fhfa_indexed: number;
    zhvi_indexed: number;
    /** zhvi_indexed - fhfa_indexed in index points (base = 100). */
    divergence_indexed_points: number;
    /** (zhvi_indexed - fhfa_indexed) / fhfa_indexed; signed. */
    divergence_pct_of_fhfa: number;
  } | null;
  /**
   * Real-dollar (CPI-deflated) affordability headline for the most
   * recent year where the full housing-burden substrate is present
   * (DCA property tax + ACS5 income + FRED MORTGAGE30US + tax brackets).
   * NULL when any substrate is missing.
   *
   * VISION_2026 §3.4 — the spec-mandated real-dollar headline that
   * replaces the unitless burden ratio as the primary signal.
   * Sourced from derived.v_affordability_gap_real (mig 085).
   */
  real_dollar: {
    year: number;
    /** Real-dollar base year (latest year in derived.cpi_u_headline_annual). */
    base_year: number;
    /** Median home price for the county-year, in real dollars. */
    home_price_real: number;
    /** ACS5 median household income, in real dollars. */
    median_income_real: number;
    /** Annual PITI on the median home, in real dollars. */
    piti_annual_real: number;
    /** HUD's required income at PITI <= 30% of gross, in real dollars. */
    required_income_hud_30pct_real: number;
    /** median_income - required_income_hud_30pct, in real dollars.
     *  Negative = household earns less than HUD threshold. THE headline. */
    hud_headroom_dollars_real: number;
  } | null;
  /**
   * Nominal-dollar (un-deflated) affordability headline for the same
   * (county, year) pair as `real_dollar`. Sibling lens to support an
   * explicit nominal/real toggle on /housing/[id] without paying for
   * a second DB round-trip. Substrate-honest: when CPI substrate is
   * loaded both lenses are present; when it isn't, both are NULL.
   * Sourced from the same v_affordability_gap_real view as the real
   * lens (it carries both via the _nominal suffix columns).
   */
  nominal_dollar: {
    year: number;
    home_price_nominal: number;
    median_income_nominal: number;
    piti_annual_nominal: number;
    required_income_hud_30pct_nominal: number;
    hud_headroom_dollars_nominal: number;
  } | null;
};

export async function listNjCounties(): Promise<CountyRow[]> {
  const sql = getSql();
  const rows = (await sql`
    SELECT county_id, county_fips, name AS county_name
    FROM ref.county
    WHERE state_code = 'NJ'
    ORDER BY name
  `) as Record<string, unknown>[];
  return rows.map((r) => ({
    county_id: String(r.county_id),
    county_fips: String(r.county_fips),
    county_name: String(r.county_name),
  }));
}

/**
 * Listing view: one row per NJ county with the latest divergence
 * ratio. Quietly degrades to NULL components when a county lacks
 * either HPI or income at the base year (some counties have ACS
 * gaps in the early 2010s); the UI sorts NULLs last.
 */
export async function listCountyBurden(): Promise<CountyBurdenRow[]> {
  const sql = getSql();
  const base = await getBurdenBaseYear();

  // Use the platform's existing indexed-functions for both series.
  // We compute per-county "latest year both present" inside SQL to
  // avoid a per-county roundtrip and to keep the page render fast
  // even on cold-started serverless functions.
  const rows = (await sql`
    WITH counties AS (
      SELECT county_id, county_fips, name AS county_name
      FROM ref.county
      WHERE state_code = 'NJ'
    ),
    hpi AS (
      SELECT county_fips, year, hpi_indexed
      FROM derived.f_fhfa_hpi_indexed(${base}::SMALLINT)
    ),
    income AS (
      SELECT county_fips, year, estimate_real
      FROM derived.f_acs_mhi_real(${base}::SMALLINT)
      WHERE product = 'acs5'
    ),
    paired AS (
      SELECT
        c.county_id,
        c.county_fips,
        c.county_name,
        h.year,
        h.hpi_indexed,
        i.estimate_real
      FROM counties c
      JOIN hpi    h ON h.county_fips = c.county_fips
      JOIN income i ON i.county_fips = c.county_fips AND i.year = h.year
    ),
    latest AS (
      SELECT DISTINCT ON (county_fips)
        county_id, county_fips, county_name,
        year                      AS year_latest,
        hpi_indexed               AS hpi_indexed_latest,
        estimate_real             AS estimate_real_latest
      FROM paired
      ORDER BY county_fips, year DESC
    ),
    income_base AS (
      SELECT county_fips, estimate_real AS base_income
      FROM derived.f_acs_mhi_real(${base}::SMALLINT)
      WHERE product = 'acs5' AND year = ${base}
    )
    SELECT
      c.county_id,
      c.county_fips,
      c.county_name,
      l.year_latest::INT AS year_latest,
      CASE
        WHEN l.hpi_indexed_latest IS NULL THEN NULL
        ELSE round((l.hpi_indexed_latest / 100.0)::NUMERIC, 4)::FLOAT8
      END AS hpi_growth,
      CASE
        WHEN ib.base_income IS NULL OR ib.base_income = 0 THEN NULL
        WHEN l.estimate_real_latest IS NULL THEN NULL
        ELSE round((l.estimate_real_latest / ib.base_income)::NUMERIC, 4)::FLOAT8
      END AS income_growth,
      CASE
        WHEN ib.base_income IS NULL
          OR ib.base_income = 0
          OR l.estimate_real_latest IS NULL
          OR l.estimate_real_latest = 0
          OR l.hpi_indexed_latest IS NULL
        THEN NULL
        ELSE round((
          (l.hpi_indexed_latest / 100.0)
          / (l.estimate_real_latest / ib.base_income)
        )::NUMERIC, 4)::FLOAT8
      END AS burden_ratio,
      l.estimate_real_latest::FLOAT8 AS median_income_real_latest,
      l.hpi_indexed_latest::FLOAT8   AS hpi_indexed_latest
    FROM counties c
    LEFT JOIN latest      l  ON l.county_fips = c.county_fips
    LEFT JOIN income_base ib ON ib.county_fips = c.county_fips
    ORDER BY
      burden_ratio DESC NULLS LAST,
      c.county_name ASC
  `) as Record<string, unknown>[];

  return rows.map((r) => ({
    county_id: String(r.county_id),
    county_fips: String(r.county_fips),
    county_name: String(r.county_name),
    year_latest: r.year_latest == null ? null : Number(r.year_latest),
    hpi_growth: r.hpi_growth == null ? null : Number(r.hpi_growth),
    income_growth: r.income_growth == null ? null : Number(r.income_growth),
    burden_ratio: r.burden_ratio == null ? null : Number(r.burden_ratio),
    median_income_real_latest:
      r.median_income_real_latest == null
        ? null
        : Number(r.median_income_real_latest),
    hpi_indexed_latest:
      r.hpi_indexed_latest == null ? null : Number(r.hpi_indexed_latest),
  }));
}

/**
 * Town-level listing: every NJ municipality with a dollar-denominated
 * affordability gap for the latest year in derived.v_muni_affordability_gap.
 * county_name is resolved from ref.county. Ordered worst-first (most
 * negative headroom), NULLs last — the UI re-sorts client-side via URL
 * params. Returns all ~565 rows (small) so the page can search/filter
 * without a roundtrip per keystroke.
 */
export async function listMuniAffordability(): Promise<MuniBurdenRow[]> {
  const sql = getSql();
  const rows = (await sql`
    WITH latest AS (
      SELECT MAX(year) AS y FROM derived.v_muni_affordability_gap
    )
    SELECT
      m.muni_code,
      m.muni_name,
      m.county_fips,
      COALESCE(c.name, m.county_fips)            AS county_name,
      m.year::INT                                AS year,
      m.home_price::FLOAT8                       AS home_price,
      m.county_median_income_nominal::FLOAT8     AS median_income,
      m.required_income_hud_30pct::FLOAT8        AS required_income,
      m.hud_headroom_dollars::FLOAT8             AS headroom,
      m.hud_required_to_actual_ratio::FLOAT8     AS ratio
    FROM derived.v_muni_affordability_gap m
    CROSS JOIN latest
    LEFT JOIN ref.county c ON c.county_fips = m.county_fips
    WHERE m.year = latest.y
    ORDER BY m.hud_headroom_dollars ASC NULLS LAST, m.muni_name ASC
  `) as Record<string, unknown>[];

  return rows.map((r) => ({
    muni_code: String(r.muni_code),
    muni_name: String(r.muni_name),
    county_fips: String(r.county_fips),
    county_name: String(r.county_name),
    year: Number(r.year),
    home_price: r.home_price == null ? null : Number(r.home_price),
    median_income: r.median_income == null ? null : Number(r.median_income),
    required_income:
      r.required_income == null ? null : Number(r.required_income),
    headroom: r.headroom == null ? null : Number(r.headroom),
    ratio: r.ratio == null ? null : Number(r.ratio),
  }));
}

/**
 * Per-county detail with three time series:
 *   - HPI indexed
 *   - Real income indexed
 *   - Burden divergence (HPI / income, both indexed)
 *
 * Series are sparse where the underlying data is sparse; the UI
 * sparkline renderer handles gaps by drawing line segments only
 * between consecutive years.
 */
export async function getCountyDetail(
  countyId: string,
): Promise<CountyDetail | null> {
  const sql = getSql();
  const base = await getBurdenBaseYear();

  const countyRows = (await sql`
    SELECT county_id, county_fips, name AS county_name
    FROM ref.county
    WHERE state_code = 'NJ' AND county_id = ${countyId}
    LIMIT 1
  `) as Record<string, unknown>[];
  if (countyRows.length === 0) return null;
  const c = countyRows[0];
  const fips = String(c.county_fips);

  const hpiRows = (await sql`
    SELECT year::INT AS year, hpi_indexed::FLOAT8 AS indexed
    FROM derived.f_fhfa_hpi_indexed(${base}::SMALLINT)
    WHERE county_fips = ${fips}
    ORDER BY year
  `) as Record<string, unknown>[];

  // Zillow ZHVI re-indexed to base_year=100 (Phase 6 substrate). The
  // function emits one row per year where the county has ZHVI data;
  // the underlying raw.zillow_zhvi_county is monthly so a year is
  // emitted as soon as ANY month is loaded -- early years (2000-...)
  // typically have <12 months because Zillow's coverage was bootstrapping.
  const zhviRows = (await sql`
    SELECT year::INT AS year, zhvi_indexed::FLOAT8 AS indexed
    FROM derived.f_zhvi_county_indexed(${base}::SMALLINT)
    WHERE county_fips = ${fips}
    ORDER BY year
  `) as Record<string, unknown>[];

  // Cross-source posture: the latest year where BOTH FHFA HPI and ZHVI
  // are populated for this county. We query this from the existing
  // derived.f_housing_index_cross_source function so the UI shows
  // exactly what the asset check sees.
  const crossSourceRows = (await sql`
    WITH paired AS (
      SELECT
        year::INT                                  AS year,
        fhfa_hpi_indexed::FLOAT8                   AS fhfa_indexed,
        zillow_zhvi_indexed::FLOAT8                AS zhvi_indexed,
        divergence_indexed_points::FLOAT8          AS divergence_indexed_points,
        divergence_pct_of_fhfa::FLOAT8             AS divergence_pct_of_fhfa
      FROM derived.f_housing_index_cross_source(${base}::SMALLINT)
      WHERE county_fips = ${fips}
        AND fhfa_hpi_indexed IS NOT NULL
        AND zillow_zhvi_indexed IS NOT NULL
    )
    SELECT year, fhfa_indexed, zhvi_indexed,
           divergence_indexed_points, divergence_pct_of_fhfa
    FROM paired
    ORDER BY year DESC
    LIMIT 1
  `) as Record<string, unknown>[];

  const incomeRows = (await sql`
    WITH base_row AS (
      SELECT estimate_real AS base_income
      FROM derived.f_acs_mhi_real(${base}::SMALLINT)
      WHERE product = 'acs5' AND year = ${base} AND county_fips = ${fips}
    )
    SELECT
      m.year::INT                                              AS year,
      round((m.estimate_real / b.base_income * 100.0)::NUMERIC, 3)::FLOAT8
                                                                AS indexed
    FROM derived.f_acs_mhi_real(${base}::SMALLINT) m
    CROSS JOIN base_row b
    WHERE m.product = 'acs5'
      AND m.county_fips = ${fips}
      AND b.base_income IS NOT NULL
      AND b.base_income <> 0
    ORDER BY year
  `) as Record<string, unknown>[];

  const hpi: SeriesPoint[] = hpiRows.map((r) => ({
    year: Number(r.year),
    indexed: Number(r.indexed),
  }));
  const zhvi: SeriesPoint[] = zhviRows.map((r) => ({
    year: Number(r.year),
    indexed: Number(r.indexed),
  }));
  const income: SeriesPoint[] = incomeRows.map((r) => ({
    year: Number(r.year),
    indexed: Number(r.indexed),
  }));
  const crossSource =
    crossSourceRows.length > 0
      ? {
          year: Number(crossSourceRows[0].year),
          fhfa_indexed: Number(crossSourceRows[0].fhfa_indexed),
          zhvi_indexed: Number(crossSourceRows[0].zhvi_indexed),
          divergence_indexed_points: Number(
            crossSourceRows[0].divergence_indexed_points,
          ),
          divergence_pct_of_fhfa: Number(
            crossSourceRows[0].divergence_pct_of_fhfa,
          ),
        }
      : null;

  // Burden series = HPI / income at years where both are present.
  const incomeByYear = new Map<number, number>();
  for (const p of income) incomeByYear.set(p.year, p.indexed);
  const burden: SeriesPoint[] = hpi
    .filter((h) => incomeByYear.has(h.year))
    .map((h) => ({
      year: h.year,
      indexed: Number(((h.indexed / incomeByYear.get(h.year)!) * 100).toFixed(3)),
    }));

  // Latest joined year (both series non-null).
  const latest = burden.length > 0 ? burden[burden.length - 1] : null;
  const hpiAt = latest ? hpi.find((p) => p.year === latest.year) : null;
  const incomeAt = latest ? income.find((p) => p.year === latest.year) : null;
  const incomeNominalRows = latest
    ? ((await sql`
        SELECT estimate_real::FLOAT8 AS v
        FROM derived.f_acs_mhi_real(${base}::SMALLINT)
        WHERE product = 'acs5'
          AND county_fips = ${fips}
          AND year = ${latest.year}
        LIMIT 1
      `) as Record<string, unknown>[])
    : [];
  const medianIncomeReal =
    incomeNominalRows.length > 0 && incomeNominalRows[0].v != null
      ? Number(incomeNominalRows[0].v)
      : null;

  // Real-dollar (CPI-deflated) affordability headline. Sourced from
  // derived.v_affordability_gap_real (mig 085 / VISION_2026 §3.4).
  // Picks the most recent (county, year) row where the FULL housing-
  // burden substrate is present (DCA + ACS5 + FRED + tax brackets).
  // NULL when any substrate is missing.
  const realDollarRows = (await sql`
    SELECT year::INT AS year,
           real_dollar_base_year::INT AS base_year,
           home_price_real::FLOAT8 AS home_price_real,
           median_income_real::FLOAT8 AS median_income_real,
           piti_annual_real::FLOAT8 AS piti_annual_real,
           required_income_hud_30pct_real::FLOAT8
                       AS required_income_hud_30pct_real,
           hud_headroom_dollars_real::FLOAT8
                       AS hud_headroom_dollars_real,
           home_price_nominal::FLOAT8 AS home_price_nominal,
           median_income_nominal::FLOAT8 AS median_income_nominal,
           piti_annual_nominal::FLOAT8 AS piti_annual_nominal,
           required_income_hud_30pct_nominal::FLOAT8
                       AS required_income_hud_30pct_nominal,
           hud_headroom_dollars_nominal::FLOAT8
                       AS hud_headroom_dollars_nominal
    FROM derived.v_affordability_gap_real
    WHERE county_fips = ${fips}
      AND home_price_real IS NOT NULL
      AND median_income_real IS NOT NULL
      AND required_income_hud_30pct_real IS NOT NULL
    ORDER BY year DESC
    LIMIT 1
  `) as Record<string, unknown>[];

  const realDollar =
    realDollarRows.length > 0
      ? {
          year: Number(realDollarRows[0].year),
          base_year: Number(realDollarRows[0].base_year),
          home_price_real: Number(realDollarRows[0].home_price_real),
          median_income_real: Number(realDollarRows[0].median_income_real),
          piti_annual_real: Number(realDollarRows[0].piti_annual_real),
          required_income_hud_30pct_real: Number(
            realDollarRows[0].required_income_hud_30pct_real,
          ),
          hud_headroom_dollars_real: Number(
            realDollarRows[0].hud_headroom_dollars_real,
          ),
        }
      : null;

  // Nominal-dollar sibling lens. Comes from the SAME row as the real
  // lens (v_affordability_gap_real carries both via _nominal columns),
  // so when real is present nominal is also present and they refer to
  // the same county-year. When CPI substrate is absent (real null) the
  // nominal lens may still be populated -- but our SQL query gates on
  // real-dollar NOT NULL above, so both lenses are tied together for
  // substrate-honest "either both or neither" presentation. Future
  // refactor: relax the WHERE clause + carry nominal independently
  // when CPI is unavailable so the nominal lens degrades gracefully.
  const nominalDollar =
    realDollarRows.length > 0 &&
    realDollarRows[0].home_price_nominal != null &&
    realDollarRows[0].median_income_nominal != null &&
    realDollarRows[0].required_income_hud_30pct_nominal != null
      ? {
          year: Number(realDollarRows[0].year),
          home_price_nominal: Number(realDollarRows[0].home_price_nominal),
          median_income_nominal: Number(
            realDollarRows[0].median_income_nominal,
          ),
          piti_annual_nominal: Number(realDollarRows[0].piti_annual_nominal),
          required_income_hud_30pct_nominal: Number(
            realDollarRows[0].required_income_hud_30pct_nominal,
          ),
          hud_headroom_dollars_nominal: Number(
            realDollarRows[0].hud_headroom_dollars_nominal,
          ),
        }
      : null;

  return {
    county_id: String(c.county_id),
    county_fips: fips,
    county_name: String(c.county_name),
    base_year: base,
    hpi_series: hpi,
    zhvi_series: zhvi,
    income_series_real: income,
    burden_series: burden,
    current: {
      year: latest?.year ?? null,
      hpi_growth:
        hpiAt != null ? Number((hpiAt.indexed / 100).toFixed(4)) : null,
      income_growth:
        incomeAt != null
          ? Number((incomeAt.indexed / 100).toFixed(4))
          : null,
      burden_ratio:
        latest != null ? Number((latest.indexed / 100).toFixed(4)) : null,
      median_income_real: medianIncomeReal,
    },
    cross_source: crossSource,
    real_dollar: realDollar,
    nominal_dollar: nominalDollar,
  };
}

export type DataVintage = {
  /** Latest FHFA HPI year present in raw.fhfa_hpi_county for any NJ county. */
  fhfa_year: number | null;
  /** Latest ACS5 income year present for any NJ county. */
  acs5_year: number | null;
  /** Latest ACS1 income year present for any NJ county (may be null). */
  acs1_year: number | null;
  /** Latest CPI-U year. */
  cpi_year: number | null;
  /** Latest year for which BOTH HPI and ACS5 are present, joined per county. */
  joined_year: number | null;
};

/**
 * Reports the freshest available year for each input substrate. We call
 * this once per page render so the UI can be honest about how stale the
 * underlying numbers are. The "joined_year" tells the user what the
 * burden-ratio table is actually pinned to.
 *
 * Why we expose ACS1 even when the table only uses ACS5
 * -----------------------------------------------------
 * ACS1 is published faster than ACS5 (single-year vs 5-year average)
 * but only for counties with population >65k. Surfacing both lets a
 * future revision swap to ACS1 for the larger NJ counties without a
 * UI redesign, and lets the current UI explain why "latest year" lags.
 */
export async function getDataVintage(): Promise<DataVintage> {
  const sql = getSql();
  const rows = (await sql`
    WITH nj_counties AS (
      SELECT county_fips FROM ref.county WHERE state_code = 'NJ'
    ),
    fhfa AS (
      SELECT MAX(year)::INT AS y
      FROM raw.fhfa_hpi_county
      WHERE county_fips IN (SELECT county_fips FROM nj_counties)
    ),
    acs5 AS (
      SELECT MAX(year)::INT AS y
      FROM raw.acs_county_median_household_income
      WHERE product = 'acs5'
        AND county_fips IN (SELECT county_fips FROM nj_counties)
    ),
    acs1 AS (
      SELECT MAX(year)::INT AS y
      FROM raw.acs_county_median_household_income
      WHERE product = 'acs1'
        AND county_fips IN (SELECT county_fips FROM nj_counties)
    ),
    cpi AS (
      SELECT MAX(year)::INT AS y FROM raw.bls_cpi_observation
    ),
    joined AS (
      SELECT MAX(LEAST(h.year, i.year))::INT AS y
      FROM raw.fhfa_hpi_county h
      JOIN raw.acs_county_median_household_income i
        ON i.county_fips = h.county_fips
       AND i.year = h.year
      WHERE i.product = 'acs5'
        AND h.county_fips IN (SELECT county_fips FROM nj_counties)
    )
    SELECT fhfa.y AS fhfa_year, acs5.y AS acs5_year, acs1.y AS acs1_year,
           cpi.y AS cpi_year, joined.y AS joined_year
    FROM fhfa, acs5, acs1, cpi, joined
  `) as Record<string, unknown>[];
  const r = rows[0] ?? {};
  return {
    fhfa_year: r.fhfa_year == null ? null : Number(r.fhfa_year),
    acs5_year: r.acs5_year == null ? null : Number(r.acs5_year),
    acs1_year: r.acs1_year == null ? null : Number(r.acs1_year),
    cpi_year: r.cpi_year == null ? null : Number(r.cpi_year),
    joined_year: r.joined_year == null ? null : Number(r.joined_year),
  };
}

/** ============================================================================
 * Phase 2: Affordability Gap (the "Collapse Curve" data layer)
 *
 * Reads derived.v_affordability_gap (migration 072) -- the headline
 * per-(county, year) view that combines:
 *   * DCA county property-tax rate + avg residential home price
 *   * FRED 30-yr fixed mortgage rate (annual mean)
 *   * IRS federal + NJ state + FICA tax engines (Phase 1)
 *   * HUD 30%-of-gross cost-burden definition
 *
 * Returns the time series the Collapse Curve frontend (idea spec §7.3
 * "your viral insight chart") plots: actual median household income
 * vs the income required to afford the median home in each year.
 *
 * NULL semantics: if either median_income or required_income is NULL
 * for a year (typically because tax tables for that year are not yet
 * seeded), that year is OMITTED from the series rather than rendered
 * as a gap. The page surfaces the seeded year range explicitly in
 * its methodology section so the user knows what's missing and why.
 * ============================================================================ */

export type AffordabilityPoint = {
  year: number;
  /** Annual PITI on the county's avg residential home value. */
  piti_annual: number | null;
  /** ACS5 median household income for the county-year. */
  median_income_nominal: number | null;
  /** HUD-aligned: PITI / 0.30. The headline required-income series. */
  required_income_hud_30pct: number | null;
  /** Lender-style: PITI <= 30% of take-home. Stricter than HUD. */
  required_income_post_tax_30pct: number | null;
  /** Strict full-burden: NULL when unreachable (the crisis signal). */
  required_income_full_burden_30pct: number | null;
  /** median - hud_required (negative = unaffordable for median household). */
  hud_headroom_dollars: number | null;
  /** hud_required / actual_median. >1 means median is short. */
  hud_required_to_actual_ratio: number | null;
  // ----------------------------------------------------------------
  // CPI-deflated counterparts (real-dollar lens).
  //
  // Every nominal-dollar column above has a `_real` counterpart that
  // expresses the same observation in base-year dollars per
  // derived.v_affordability_gap_real (mig 085, formula version
  // 2.0.0-real-dollar-baseline-v1). NULL when CPI substrate is missing
  // for either p_cycle's source year OR the base year -- substrate-
  // honest, no silent fallback.
  // ----------------------------------------------------------------
  /** PITI annual in real-dollar base year. */
  piti_annual_real: number | null;
  /** Median household income in real-dollar base year. */
  median_income_real: number | null;
  /** HUD required income in real-dollar base year. */
  required_income_hud_30pct_real: number | null;
  /** Post-tax required income in real-dollar base year. */
  required_income_post_tax_30pct_real: number | null;
  /** Full-burden required income in real-dollar base year. */
  required_income_full_burden_30pct_real: number | null;
  /** Income headroom (median - required) in real-dollar base year. */
  hud_headroom_dollars_real: number | null;
};

export type CountyAffordabilityGap = CountyRow & {
  /** All available years, sorted ascending. */
  series: AffordabilityPoint[];
  /** The most recent fully-populated year (HUD-required is non-null). */
  latest_year: number | null;
  /** Headline numbers for the latest year. */
  latest:
    | (AffordabilityPoint & {
        home_price: number | null;
        home_price_real: number | null;
      })
    | null;
  /** Profile used by the per-county view. */
  profile: {
    filing_status: string;
    dependents: number;
    qualifying_children: number;
  };
  /**
   * Real-dollar base year per derived.f_real_dollar_base_year() at the
   * time the query ran. The substrate-honest answer to "what base year
   * are these real-dollar values in?" -- spec §3.4 mandates 2026 but
   * BLS publishes CPI-U M13 with ~12-month lag, so until 2026 CPI lands
   * this is the latest year present in derived.cpi_u_headline_annual.
   * NULL when CPI substrate is empty.
   */
  real_dollar_base_year: number | null;
  /** Substrate-honest data-availability summary for the methodology box. */
  coverage: {
    dca_year_min: number | null;
    dca_year_max: number | null;
    acs_year_min: number | null;
    acs_year_max: number | null;
    fred_year_min: number | null;
    fred_year_max: number | null;
    /** Years where Phase 1 tax tables (federal + NJ) are seeded. */
    tax_seeded_years: number[];
    /** Intersection of all four substrates -- years the curve plots. */
    affordability_years: number[];
  };
  /** ref.formula_version row that produced these numbers. */
  formula_version: string;
};

/**
 * Fetches the per-county affordability time series + coverage metadata
 * for the Collapse Curve page. One trip to Postgres for the gap rows,
 * one for the coverage summary.
 */
export async function getCountyAffordabilityGap(
  countyId: string,
): Promise<CountyAffordabilityGap | null> {
  const sql = getSql();

  const countyRows = (await sql`
    SELECT county_id, county_fips, name AS county_name
    FROM ref.county
    WHERE state_code = 'NJ' AND county_id = ${countyId}
    LIMIT 1
  `) as Record<string, unknown>[];
  if (countyRows.length === 0) return null;
  const c = countyRows[0];
  const fips = String(c.county_fips);

  // Read the real-dollar headline view -- it carries BOTH the nominal
  // and CPI-deflated counterparts in a single row, plus the base year
  // used for deflation. The page renders both lenses and we want them
  // to come from a SINGLE round-trip so a stale base-year-vs-rows mismatch
  // is structurally impossible (mig 085 derived.v_affordability_gap_real).
  //
  // v_affordability_gap_real does NOT carry profile metadata or the
  // formula_version of the underlying v_affordability_gap (its own
  // formula_version is "2.0.0-real-dollar-baseline-v1"); we LEFT JOIN
  // back to the source view for those two fields so the UI banner
  // continues to display the source-tier formula_version.
  const gapRows = (await sql`
    SELECT
      r.year::INT                                    AS year,
      r.real_dollar_base_year::INT                   AS real_dollar_base_year,
      r.home_price_nominal::FLOAT8                   AS home_price,
      r.home_price_real::FLOAT8                      AS home_price_real,
      r.median_income_nominal::FLOAT8                AS median_income_nominal,
      r.median_income_real::FLOAT8                   AS median_income_real,
      r.piti_annual_nominal::FLOAT8                  AS piti_annual,
      r.piti_annual_real::FLOAT8                     AS piti_annual_real,
      r.required_income_hud_30pct_nominal::FLOAT8    AS required_income_hud_30pct,
      r.required_income_hud_30pct_real::FLOAT8       AS required_income_hud_30pct_real,
      r.required_income_post_tax_30pct_nominal::FLOAT8
                                                     AS required_income_post_tax_30pct,
      r.required_income_post_tax_30pct_real::FLOAT8  AS required_income_post_tax_30pct_real,
      r.required_income_full_burden_30pct_nominal::FLOAT8
                                                     AS required_income_full_burden_30pct,
      r.required_income_full_burden_30pct_real::FLOAT8
                                                     AS required_income_full_burden_30pct_real,
      r.hud_headroom_dollars_nominal::FLOAT8         AS hud_headroom_dollars,
      r.hud_headroom_dollars_real::FLOAT8            AS hud_headroom_dollars_real,
      r.hud_required_to_actual_ratio::FLOAT8         AS hud_required_to_actual_ratio,
      v.profile_filing_status                        AS profile_filing_status,
      v.profile_dependents                           AS profile_dependents,
      v.profile_qualifying_children                  AS profile_qualifying_children,
      v.formula_version                              AS formula_version
    FROM derived.v_affordability_gap_real r
    LEFT JOIN derived.v_affordability_gap v
      ON v.county_fips = r.county_fips AND v.year = r.year
    WHERE r.county_fips = ${fips}
    ORDER BY r.year
  `) as Record<string, unknown>[];

  // Coverage block -- the methodology box explains exactly which
  // years have which substrate. Substrate honesty: instead of papering
  // over missing tax years, we show them.
  const coverageRows = (await sql`
    WITH dca AS (
      SELECT MIN(year)::INT AS y_min, MAX(year)::INT AS y_max
      FROM raw.nj_property_tax_county
      WHERE county_fips = ${fips}
    ),
    acs AS (
      SELECT MIN(year)::INT AS y_min, MAX(year)::INT AS y_max
      FROM raw.acs_median_household_income
      WHERE county_fips = ${fips} AND product = 'acs5' AND estimate IS NOT NULL
    ),
    fred AS (
      SELECT MIN(year)::INT AS y_min, MAX(year)::INT AS y_max
      FROM derived.fred_annual
      WHERE series_id = 'MORTGAGE30US' AND n_obs >= 1
    ),
    tax AS (
      SELECT array_agg(DISTINCT tax_year ORDER BY tax_year)::INT[]
        AS years
      FROM (
        SELECT tax_year FROM ref.irs_federal_brackets
        INTERSECT
        SELECT tax_year FROM ref.nj_state_brackets
      ) t
    )
    SELECT
      dca.y_min  AS dca_min,
      dca.y_max  AS dca_max,
      acs.y_min  AS acs_min,
      acs.y_max  AS acs_max,
      fred.y_min AS fred_min,
      fred.y_max AS fred_max,
      tax.years  AS tax_years
    FROM dca, acs, fred, tax
  `) as Record<string, unknown>[];
  const cov = coverageRows[0] ?? {};
  const taxYears = Array.isArray(cov.tax_years)
    ? (cov.tax_years as unknown[]).map((y) => Number(y))
    : [];

  const series: AffordabilityPoint[] = gapRows.map((r) => ({
    year: Number(r.year),
    piti_annual: r.piti_annual == null ? null : Number(r.piti_annual),
    median_income_nominal:
      r.median_income_nominal == null ? null : Number(r.median_income_nominal),
    required_income_hud_30pct:
      r.required_income_hud_30pct == null
        ? null
        : Number(r.required_income_hud_30pct),
    required_income_post_tax_30pct:
      r.required_income_post_tax_30pct == null
        ? null
        : Number(r.required_income_post_tax_30pct),
    required_income_full_burden_30pct:
      r.required_income_full_burden_30pct == null
        ? null
        : Number(r.required_income_full_burden_30pct),
    hud_headroom_dollars:
      r.hud_headroom_dollars == null ? null : Number(r.hud_headroom_dollars),
    hud_required_to_actual_ratio:
      r.hud_required_to_actual_ratio == null
        ? null
        : Number(r.hud_required_to_actual_ratio),
    piti_annual_real:
      r.piti_annual_real == null ? null : Number(r.piti_annual_real),
    median_income_real:
      r.median_income_real == null ? null : Number(r.median_income_real),
    required_income_hud_30pct_real:
      r.required_income_hud_30pct_real == null
        ? null
        : Number(r.required_income_hud_30pct_real),
    required_income_post_tax_30pct_real:
      r.required_income_post_tax_30pct_real == null
        ? null
        : Number(r.required_income_post_tax_30pct_real),
    required_income_full_burden_30pct_real:
      r.required_income_full_burden_30pct_real == null
        ? null
        : Number(r.required_income_full_burden_30pct_real),
    hud_headroom_dollars_real:
      r.hud_headroom_dollars_real == null
        ? null
        : Number(r.hud_headroom_dollars_real),
  }));

  // The "plotted" years are those with both actual median income AND
  // the HUD required-income (i.e. tax tables present for that year).
  const affordabilityYears = series
    .filter(
      (p) =>
        p.median_income_nominal != null && p.required_income_hud_30pct != null,
    )
    .map((p) => p.year);

  // Latest fully-populated point for the page header.
  const populated = series.filter(
    (p) =>
      p.median_income_nominal != null && p.required_income_hud_30pct != null,
  );
  const latest = populated.length > 0 ? populated[populated.length - 1] : null;
  const latestRow =
    latest != null
      ? gapRows.find((r) => Number(r.year) === latest.year)
      : null;

  const formulaVersion =
    gapRows.length > 0 && gapRows[0].formula_version != null
      ? String(gapRows[0].formula_version)
      : "1.2.0-affordability-engine-v1";

  const profile =
    gapRows.length > 0
      ? {
          filing_status: String(gapRows[0].profile_filing_status ?? "mfj"),
          dependents: Number(gapRows[0].profile_dependents ?? 1),
          qualifying_children: Number(
            gapRows[0].profile_qualifying_children ?? 1,
          ),
        }
      : { filing_status: "mfj", dependents: 1, qualifying_children: 1 };

  // The real-dollar base year is constant across every row (it's the
  // output of a STABLE scalar function evaluated once per query in the
  // view). We read it from row 0 and tolerate an empty result set (the
  // base year is meaningless without rows).
  const realDollarBaseYear =
    gapRows.length > 0 && gapRows[0].real_dollar_base_year != null
      ? Number(gapRows[0].real_dollar_base_year)
      : null;

  return {
    county_id: String(c.county_id),
    county_fips: fips,
    county_name: String(c.county_name),
    series,
    latest_year: latest?.year ?? null,
    latest:
      latest != null
        ? {
            ...latest,
            home_price:
              latestRow?.home_price == null ? null : Number(latestRow.home_price),
            home_price_real:
              latestRow?.home_price_real == null
                ? null
                : Number(latestRow.home_price_real),
          }
        : null,
    profile,
    real_dollar_base_year: realDollarBaseYear,
    coverage: {
      dca_year_min: cov.dca_min == null ? null : Number(cov.dca_min),
      dca_year_max: cov.dca_max == null ? null : Number(cov.dca_max),
      acs_year_min: cov.acs_min == null ? null : Number(cov.acs_min),
      acs_year_max: cov.acs_max == null ? null : Number(cov.acs_max),
      fred_year_min: cov.fred_min == null ? null : Number(cov.fred_min),
      fred_year_max: cov.fred_max == null ? null : Number(cov.fred_max),
      tax_seeded_years: taxYears,
      affordability_years: affordabilityYears,
    },
    formula_version: formulaVersion,
  };
}

/* ============================================================================
 * Phase 3 -- Disposable income trajectory + Affordability Erosion Index.
 *
 * Data layer for the per-county DI time series (idea §5.3) and the
 * single-stat AEI (idea §5.5). Both surface honestly: a county-year
 * with NULL DI in the DB renders as a hole in the chart and is
 * documented in the methodology box, never silently zeroed.
 * ========================================================================= */

export type DisposableIncomePoint = {
  year: number;
  /** ACS5 median household income for the county-year (nominal $). */
  median_income_nominal: number | null;
  /** Average residential home value for the county-year (nominal $). */
  home_price: number | null;
  /** Disposable income = gross - federal/NJ/FICA tax - PITI (nominal $). */
  di_nominal: number | null;
  /** DI deflated to real_dollars_base_year via CPI ratio. */
  di_real: number | null;
};

export type CountyDisposableIncome = CountyRow & {
  series: DisposableIncomePoint[];
  /** Year the di_real column is denominated in (latest available CPI). */
  real_dollars_base_year: number | null;
  /** Headline AEI vs the earliest year with a non-NULL HBR. */
  aei: {
    anchor_year: number;
    anchor_hbr: number;
    latest_year: number;
    latest_hbr: number;
    /** HBR(latest) / HBR(anchor). >1 means housing got harder. */
    aei: number;
    years_observed: number;
  } | null;
  profile: {
    filing_status: string;
    dependents: number;
    qualifying_children: number;
  };
  formula_version: string;
};

/**
 * Fetches the per-county DI trajectory + AEI in a single round-trip.
 * Both the trajectory view and the AEI view are filtered to the same
 * county FIPS so the page can render them together without a join.
 *
 * Returns null only when the county_id is unknown. A known county
 * with no populated years returns an object with empty series and
 * aei=null (the page handles this gracefully).
 */
export async function getCountyDisposableIncome(
  countyId: string,
): Promise<CountyDisposableIncome | null> {
  const sql = getSql();

  const countyRows = (await sql`
    SELECT county_id, county_fips, name AS county_name
    FROM ref.county
    WHERE state_code = 'NJ' AND county_id = ${countyId}
    LIMIT 1
  `) as Record<string, unknown>[];
  if (countyRows.length === 0) return null;
  const c = countyRows[0];
  const fips = String(c.county_fips);

  const trajRows = (await sql`
    SELECT
      year::INT                              AS year,
      median_income_nominal::FLOAT8          AS median_income_nominal,
      home_price::FLOAT8                     AS home_price,
      di_nominal::FLOAT8                     AS di_nominal,
      di_real::FLOAT8                        AS di_real,
      real_dollars_base_year::INT            AS real_dollars_base_year,
      profile_filing_status                  AS profile_filing_status,
      profile_dependents                     AS profile_dependents,
      profile_qualifying_children            AS profile_qualifying_children,
      formula_version                        AS formula_version
    FROM derived.v_disposable_income_trajectory
    WHERE county_fips = ${fips}
    ORDER BY year
  `) as Record<string, unknown>[];

  const aeiRows = (await sql`
    SELECT
      anchor_year::INT     AS anchor_year,
      anchor_hbr::FLOAT8   AS anchor_hbr,
      latest_year::INT     AS latest_year,
      latest_hbr::FLOAT8   AS latest_hbr,
      aei::FLOAT8          AS aei,
      years_observed::INT  AS years_observed
    FROM derived.v_aei_by_county
    WHERE county_fips = ${fips}
    LIMIT 1
  `) as Record<string, unknown>[];

  const series: DisposableIncomePoint[] = trajRows.map((r) => ({
    year: Number(r.year),
    median_income_nominal:
      r.median_income_nominal == null ? null : Number(r.median_income_nominal),
    home_price: r.home_price == null ? null : Number(r.home_price),
    di_nominal: r.di_nominal == null ? null : Number(r.di_nominal),
    di_real: r.di_real == null ? null : Number(r.di_real),
  }));

  const baseYear =
    trajRows.length > 0 && trajRows[0].real_dollars_base_year != null
      ? Number(trajRows[0].real_dollars_base_year)
      : null;

  const profile =
    trajRows.length > 0
      ? {
          filing_status: String(trajRows[0].profile_filing_status ?? "mfj"),
          dependents: Number(trajRows[0].profile_dependents ?? 1),
          qualifying_children: Number(
            trajRows[0].profile_qualifying_children ?? 1,
          ),
        }
      : { filing_status: "mfj", dependents: 1, qualifying_children: 1 };

  const formulaVersion =
    trajRows.length > 0 && trajRows[0].formula_version != null
      ? String(trajRows[0].formula_version)
      : "1.3.0-disposable-income-erosion-v1";

  const aei =
    aeiRows.length > 0
      ? {
          anchor_year: Number(aeiRows[0].anchor_year),
          anchor_hbr: Number(aeiRows[0].anchor_hbr),
          latest_year: Number(aeiRows[0].latest_year),
          latest_hbr: Number(aeiRows[0].latest_hbr),
          aei: Number(aeiRows[0].aei),
          years_observed: Number(aeiRows[0].years_observed),
        }
      : null;

  return {
    county_id: String(c.county_id),
    county_fips: fips,
    county_name: String(c.county_name),
    series,
    real_dollars_base_year: baseYear,
    aei,
    profile,
    formula_version: formulaVersion,
  };
}

/** ============================================================================
 * NJ-statewide affordability headline (powers the landing page).
 *
 * Single round-trip that, for the most recent year where ALL FOUR substrates
 * (DCA property tax + ACS5 income + FRED mortgage rate + IRS/NJ tax brackets)
 * exist, returns one row per NJ county with the income gap a representative
 * MFJ-1-1 household faces against the HUD 30%-of-gross threshold (idea §5.4).
 *
 * The page composes a headline like "In 2024, X of 21 NJ counties' median
 * households need an extra $Y per year to afford their county's median home"
 * directly from these rows, so every number on the front door is verifiable
 * against `derived.v_affordability_gap` and the cited substrate.
 * ========================================================================= */

export type CountyHeadlineRow = CountyRow & {
  latest_year: number;
  home_price: number | null;
  median_income: number | null;
  required_income: number | null;
  /** median_income - required_income; <0 means median household can't afford. */
  headroom: number | null;
  /** required_income / median_income; >1 means median is short. */
  required_ratio: number | null;
};

export type NjAffordabilityHeadline = {
  latest_year: number | null;
  total_counties: number;
  counties_with_data: number;
  /** headroom < 0 (median household earns less than HUD-required income). */
  counties_unaffordable: number;
  /** headroom < -$25K of annual gross. Substrate-honest "severe" cutoff. */
  counties_severely_unaffordable: number;
  /** AVG(headroom) across counties with data, in $. */
  avg_headroom: number | null;
  /** AVG(median income) across counties with data, in $. */
  avg_median_income: number | null;
  /** AVG(required income) across counties with data, in $. */
  avg_required_income: number | null;
  /** Counties sorted ascending by headroom (worst affordability first). */
  rows: CountyHeadlineRow[];
};

/**
 * Fetches the per-county headroom for the latest year where every substrate
 * is present, plus aggregate counts the landing-page hero renders directly.
 *
 * Returns `latest_year = null` and zero counts when the substrate is empty
 * (the landing page falls back to a "not yet hydrated" message in that case).
 */
export async function getNjAffordabilityHeadline(): Promise<NjAffordabilityHeadline> {
  const sql = getSql();

  const rows = (await sql`
    WITH global_latest AS (
      SELECT MAX(g.year)::INT AS y
      FROM derived.v_affordability_gap g
      JOIN ref.county c
        ON c.county_fips = g.county_fips
       AND c.state_code = 'NJ'
      WHERE g.median_income_nominal IS NOT NULL
        AND g.required_income_hud_30pct IS NOT NULL
    ),
    nj AS (
      SELECT county_id, county_fips, name AS county_name
      FROM ref.county
      WHERE state_code = 'NJ'
    )
    SELECT
      n.county_id,
      n.county_fips,
      n.county_name,
      gl.y::INT                                    AS latest_year,
      g.home_price::FLOAT8                         AS home_price,
      g.median_income_nominal::FLOAT8              AS median_income,
      g.required_income_hud_30pct::FLOAT8          AS required_income,
      g.hud_headroom_dollars::FLOAT8               AS headroom,
      g.hud_required_to_actual_ratio::FLOAT8       AS required_ratio
    FROM global_latest gl
    CROSS JOIN nj n
    LEFT JOIN derived.v_affordability_gap g
      ON g.county_fips = n.county_fips
     AND g.year = gl.y
    ORDER BY g.hud_headroom_dollars ASC NULLS LAST, n.county_name ASC
  `) as Record<string, unknown>[];

  const totalRow = (await sql`
    SELECT COUNT(*)::INT AS n
    FROM ref.county
    WHERE state_code = 'NJ'
  `) as { n: number }[];
  const totalCounties = totalRow.length > 0 ? Number(totalRow[0].n) : 0;

  const headlineRows: CountyHeadlineRow[] = rows.map((r) => ({
    county_id: String(r.county_id),
    county_fips: String(r.county_fips),
    county_name: String(r.county_name),
    latest_year:
      r.latest_year == null ? 0 : Number(r.latest_year),
    home_price: r.home_price == null ? null : Number(r.home_price),
    median_income: r.median_income == null ? null : Number(r.median_income),
    required_income:
      r.required_income == null ? null : Number(r.required_income),
    headroom: r.headroom == null ? null : Number(r.headroom),
    required_ratio:
      r.required_ratio == null ? null : Number(r.required_ratio),
  }));

  const withData = headlineRows.filter((r) => r.headroom != null);
  const unaffordable = withData.filter((r) => (r.headroom ?? 0) < 0);
  const severely = withData.filter((r) => (r.headroom ?? 0) < -25000);

  const mean = (xs: number[]): number | null =>
    xs.length === 0 ? null : xs.reduce((a, b) => a + b, 0) / xs.length;

  const latestYear = headlineRows.find((r) => r.latest_year > 0)?.latest_year ?? null;

  return {
    latest_year: latestYear,
    total_counties: totalCounties,
    counties_with_data: withData.length,
    counties_unaffordable: unaffordable.length,
    counties_severely_unaffordable: severely.length,
    avg_headroom: mean(withData.map((r) => r.headroom as number)),
    avg_median_income: mean(
      withData
        .map((r) => r.median_income)
        .filter((x): x is number => x != null),
    ),
    avg_required_income: mean(
      withData
        .map((r) => r.required_income)
        .filter((x): x is number => x != null),
    ),
    rows: headlineRows,
  };
}

/**
 * Classifies a burden ratio against a versioned tier-band registry.
 *
 * The legacy 4-arg shape (ratio only) used hardcoded cutoffs
 * {1.4, 1.15, 0.95}; this version takes the bands from
 * `ref.tier_bands` (loaded by `getBurdenTierBands`) so every cutoff
 * carries a citation_text and formula_version. Cutoffs are now
 * empirically calibrated against the historical NJ panel
 * (see seed 015 calibration notes).
 *
 * NULL ratio renders the "missing data" fallback without consulting
 * the bands -- this is a UI affordance, not a tier in itself.
 */
export type BurdenTierResult = {
  label: string;
  bg: string;
  fg: string;
  description: string;
  formula_version: string | null;
  citation_text: string | null;
};

export function burdenTier(
  ratio: number | null,
  bands: TierBand[],
): BurdenTierResult {
  if (ratio == null) {
    return {
      label: "—",
      bg: "bg-zinc-100 dark:bg-zinc-800",
      fg: "text-zinc-500",
      description: "missing data",
      formula_version: null,
      citation_text: null,
    };
  }
  // Half-open intervals [lower, upper). Bands are sorted by band_ord
  // from getBurdenTierBands; the SQL guarantees they form a contiguous
  // partition.
  for (const band of bands) {
    const aboveLower =
      band.lower_bound == null || ratio >= band.lower_bound;
    const belowUpper =
      band.upper_bound == null || ratio < band.upper_bound;
    if (aboveLower && belowUpper) {
      return {
        label: band.label,
        bg: band.ui_bg_classes,
        fg: band.ui_fg_classes,
        description: band.description,
        formula_version: band.formula_version,
        citation_text: band.citation_text,
      };
    }
  }
  // No matching band -- shouldn't happen with a contiguous partition,
  // but emit the unknown fallback rather than throw.
  return {
    label: "—",
    bg: "bg-zinc-100 dark:bg-zinc-800",
    fg: "text-zinc-500",
    description: "ratio outside all configured tier bands",
    formula_version: null,
    citation_text: null,
  };
}
