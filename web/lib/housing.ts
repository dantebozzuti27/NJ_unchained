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
 * Base year: BURDEN_BASE_YEAR (constant, 2010). Both HPI and income
 * are normalized to base_year=100 so the ratio at the base year is
 * always 1.0 by construction; subsequent years move freely.
 */

import { getSql } from "./db";

export const BURDEN_BASE_YEAR = 2010;

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

export type SeriesPoint = {
  year: number;
  /** Indexed value (HPI or income), base=100 at BURDEN_BASE_YEAR. */
  indexed: number;
};

export type CountyDetail = CountyRow & {
  base_year: number;
  hpi_series: SeriesPoint[];
  income_series_real: SeriesPoint[];
  burden_series: SeriesPoint[];
  current: {
    year: number | null;
    hpi_growth: number | null;
    income_growth: number | null;
    burden_ratio: number | null;
    median_income_real: number | null;
  };
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
  const base = BURDEN_BASE_YEAR;

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
  const base = BURDEN_BASE_YEAR;

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
  const income: SeriesPoint[] = incomeRows.map((r) => ({
    year: Number(r.year),
    indexed: Number(r.indexed),
  }));

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

  return {
    county_id: String(c.county_id),
    county_fips: fips,
    county_name: String(c.county_name),
    base_year: base,
    hpi_series: hpi,
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
  };
}

/** Tier-style classifier for the county-level burden ratio. */
export function burdenTier(ratio: number | null): {
  label: "STRESS" | "ELEVATED" | "TRACKING" | "LAGGING" | "—";
  bg: string;
  fg: string;
  description: string;
} {
  if (ratio == null)
    return {
      label: "—",
      bg: "bg-zinc-100 dark:bg-zinc-800",
      fg: "text-zinc-500",
      description: "missing data",
    };
  if (ratio >= 1.4)
    return {
      label: "STRESS",
      bg: "bg-red-100 dark:bg-red-950",
      fg: "text-red-800 dark:text-red-200",
      description: "housing growth >40% above wage growth",
    };
  if (ratio >= 1.15)
    return {
      label: "ELEVATED",
      bg: "bg-orange-100 dark:bg-orange-950",
      fg: "text-orange-800 dark:text-orange-200",
      description: "housing growth >15% above wage growth",
    };
  if (ratio >= 0.95)
    return {
      label: "TRACKING",
      bg: "bg-emerald-100 dark:bg-emerald-950",
      fg: "text-emerald-800 dark:text-emerald-200",
      description: "housing growth roughly matches wage growth",
    };
  return {
    label: "LAGGING",
    bg: "bg-blue-50 dark:bg-blue-950",
    fg: "text-blue-700 dark:text-blue-300",
    description: "wages outpacing housing growth",
  };
}
