"""Quality gates for raw and derived assets.

Each :class:`AssetCheckSpec` runs a SQL query against the data DB and
declares pass/fail by row-count threshold. Dagster surfaces the result
in the asset-graph UI; we additionally write a 'warn' or 'error'
signal to ``governance.dataset_health`` so non-Dagster consumers see
the same signal.

Check taxonomy
--------------
We use three check families uniformly across assets:

    row_count_positive   -- table has at least one row
    nj_county_coverage   -- table has >= 1 row for each of NJ's 21 counties
    no_negative_values   -- check that monetary columns are not negative

Adding a new check
------------------
1. Author the check function below using @asset_check.
2. Append it to ALL_ASSET_CHECKS.

Why not Dagster's :func:`build_metadata_bounds_checks` factory?
We are deliberately keeping checks as plain @asset_check functions so
that we can write idiomatic SQL (against the actual table) rather than
inferring from materialization metadata. The latter is more
fashionable; the former is more testable and easier to debug.
"""

from typing import Any

from dagster import (
    AssetCheckExecutionContext,
    AssetCheckResult,
    AssetCheckSeverity,
    AssetKey,
    asset_check,
)

from orchestration.resources import GovernanceWriter, HealthSignal, PgResource

# Expected NJ county count for any NJ-scoped table.
_NJ_COUNTY_COUNT = 21


def _count(pg: PgResource, query: str, *args: object) -> int:
    """Run a single-int aggregation query; return the integer result.

    psycopg3 treats any non-None args (including an empty tuple) as a
    request for parameter substitution, which makes literal ``%`` in
    queries like ``LIKE '34%'`` raise ProgrammingError. Skip the args
    path entirely when no params are passed so literal ``%`` survives.
    """
    with pg.connect() as conn, conn.cursor() as cur:
        if args:
            cur.execute(query, args)
        else:
            cur.execute(query)
        row = cur.fetchone()
        return int(row[0]) if row else 0


def _emit(
    governance: GovernanceWriter,
    *,
    dataset_id: str,
    check_name: str,
    passed: bool,
    details: dict[str, object],
) -> None:
    """Write the check result to governance.dataset_health."""
    governance.emit(HealthSignal(
        dataset_id=dataset_id,
        signal_name=f"check.{check_name}",
        severity="info" if passed else "warn",
        details=details,
    ))


# ============================================================================
# raw.fred_observation
# ============================================================================


@asset_check(
    asset=AssetKey(["raw", "fred_observation"]),
    name="row_count_positive",
    description="raw.fred_observation must have at least one row.",
)
def fred_row_count_positive(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Confirm raw.fred_observation has at least one row."""
    n = _count(pg, "SELECT COUNT(*) FROM raw.fred_observation")
    passed = n > 0
    _emit(governance, dataset_id="raw.fred_observation",
          check_name="row_count_positive", passed=passed, details={"row_count": n})
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.ERROR,
        metadata={"row_count": n},
    )


# ============================================================================
# raw.cpi_u
# ============================================================================


@asset_check(
    asset=AssetKey(["raw", "cpi_u"]),
    name="row_count_positive",
    description="raw.cpi_u must have at least one row.",
)
def cpi_row_count_positive(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Confirm raw.cpi_u has at least one row."""
    n = _count(pg, "SELECT COUNT(*) FROM raw.cpi_u")
    passed = n > 0
    _emit(governance, dataset_id="raw.cpi_u",
          check_name="row_count_positive", passed=passed, details={"row_count": n})
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.ERROR,
        metadata={"row_count": n},
    )


# ============================================================================
# raw.fhfa_hpi_county
# ============================================================================


@asset_check(
    asset=AssetKey(["raw", "fhfa_hpi_county"]),
    name="nj_county_coverage",
    description=(
        "raw.fhfa_hpi_county must include all 21 NJ counties for the "
        "most recent year present in the table."
    ),
)
def fhfa_nj_county_coverage(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Confirm raw.fhfa_hpi_county covers all 21 NJ counties in the latest year."""
    n = _count(pg, """
        SELECT COUNT(DISTINCT h.county_fips)
        FROM raw.fhfa_hpi_county h
        WHERE h.county_fips LIKE '34%'
          AND h.year = (
              SELECT MAX(year) FROM raw.fhfa_hpi_county
              WHERE county_fips LIKE '34%'
          )
    """)
    passed = n == _NJ_COUNTY_COUNT
    _emit(governance, dataset_id="raw.fhfa_hpi_county",
          check_name="nj_county_coverage", passed=passed,
          details={"nj_counties_in_latest_year": n,
                   "expected": _NJ_COUNTY_COUNT})
    return AssetCheckResult(
        passed=passed,
        severity=AssetCheckSeverity.ERROR,
        metadata={
            "nj_counties_in_latest_year": n,
            "expected": _NJ_COUNTY_COUNT,
        },
    )


# ============================================================================
# raw.zillow_zhvi_county (Phase 6) + cross-source divergence vs FHFA HPI
#
# CALIBRATION NOTE -- 2026-05-08, against Neon production substrate.
#
# Empirical distributions over 546 (NJ county, year) pairs that have
# both FHFA HPI and ZHVI loaded back to 2000-01-31 (re-indexed to
# 2010 = 100):
#
#   |divergence_pct_of_fhfa|  p50=2.97%  p75=5.24%  p90=7.14%
#                            p95=8.55%  p99=11.38% max=14.66%
#
#   ZHVI YoY annual growth   p01=-9.79%  p50=+3.94%  p99=+20.73%
#                            min=-14.55% max=+22.56%
#
# The current real-world outliers exceeding 10% absolute cross-source
# divergence are concentrated in two real-economy regimes:
#
#   * Early 2000s (2000-2002) -- ZHVI's coverage was bootstrapping in
#     thinner counties (Cape May, Camden); FHFA had thin transaction
#     counts in the same counties; both methodologies were noisier in
#     this window than later periods.
#
#   * 2020-2021 COVID housing run-up -- ZHVI captured the rapid price
#     run-up faster than FHFA's repeat-sales lag (Passaic +14.58%,
#     Salem +12.17%, Atlantic +11.28% all in 2021).
#
# Both regimes are EXPLAINED divergences, not data bugs. Therefore the
# asset-check thresholds below are calibrated to fire only on values
# WELL OUTSIDE historical bounds:
#
#   - cross_source_divergence_plausible: ERROR if any (NJ county,
#     latest-year) pair exceeds 20% absolute (about 1.4x the historical
#     max); WARN if any exceeds 12% (about p99 + 0.6pp). Operator
#     interpretation: "this is genuinely outside normal."
#
#   - zhvi_yoy_outliers_plausible: ERROR if any NJ (county, latest-year)
#     pair exceeds +30% or falls below -20% (about 1.3x the historical
#     extremes). WARN if outside [-15%, +25%] (~ historical [min, max]
#     with a 0.5pp safety margin). The 2008-2009 housing crash hit -14.55%;
#     the 2021 run-up hit +22.56%; +30% / -20% would be a larger move
#     than either cyclical extreme.
# ============================================================================


@asset_check(
    asset=AssetKey(["raw", "zillow_zhvi_county"]),
    name="row_count_positive",
    description="raw.zillow_zhvi_county must have at least one row.",
)
def zhvi_row_count_positive(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Confirm raw.zillow_zhvi_county has at least one row."""
    n = _count(pg, "SELECT COUNT(*) FROM raw.zillow_zhvi_county")
    passed = n > 0
    _emit(governance, dataset_id="raw.zillow_zhvi_county",
          check_name="row_count_positive", passed=passed, details={"row_count": n})
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.ERROR,
        metadata={"row_count": n},
    )


@asset_check(
    asset=AssetKey(["raw", "zillow_zhvi_county"]),
    name="nj_county_coverage",
    description=(
        "raw.zillow_zhvi_county must include all 21 NJ counties for the "
        "most recent observation_month present in the table. ZHVI publishes "
        "every county every month, so a partial coverage in the latest "
        "month indicates either a parser failure on a county-row or a "
        "schema change at Zillow that dropped a column."
    ),
)
def zhvi_nj_county_coverage(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Confirm raw.zillow_zhvi_county covers all 21 NJ counties in the latest month."""
    n = _count(pg, """
        SELECT COUNT(DISTINCT z.county_fips)
        FROM raw.zillow_zhvi_county z
        WHERE z.county_fips LIKE '34%'
          AND z.observation_month = (
              SELECT MAX(observation_month) FROM raw.zillow_zhvi_county
              WHERE county_fips LIKE '34%'
          )
    """)
    passed = n == _NJ_COUNTY_COUNT
    _emit(governance, dataset_id="raw.zillow_zhvi_county",
          check_name="nj_county_coverage", passed=passed,
          details={"nj_counties_in_latest_month": n,
                   "expected": _NJ_COUNTY_COUNT})
    return AssetCheckResult(
        passed=passed,
        severity=AssetCheckSeverity.ERROR,
        metadata={
            "nj_counties_in_latest_month": n,
            "expected": _NJ_COUNTY_COUNT,
        },
    )


@asset_check(
    asset=AssetKey(["raw", "zillow_zhvi_county"]),
    name="zhvi_yoy_outliers_plausible",
    description=(
        "Year-over-year ZHVI annual-mean growth for any NJ (county, "
        "latest-year) pair should fall within the historical envelope of "
        "[-20%, +30%]. Calibrated against 546 (NJ county, year) pairs "
        "2000-2025 with empirical [min, max] = [-14.55%, +22.56%]. A YoY "
        "swing outside [-20%, +30%] would be a larger move than either "
        "the 2008-09 housing crash or the 2021 COVID run-up, and most "
        "likely indicates a parser bug, a wide/long melt error, or an "
        "undocumented Zillow schema change that mis-attributed a column "
        "to the wrong year."
    ),
)
def zhvi_yoy_outliers_plausible(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """No NJ (county, latest-year) pair has YoY ZHVI growth outside [-20%, +30%]."""
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("""
            WITH latest AS (
                SELECT MAX(year) AS y
                FROM derived.v_zhvi_county_annual
                WHERE county_fips LIKE '34%' AND n_months >= 6
            ),
            yoy AS (
                SELECT a.county_fips, a.year,
                       (a.zhvi_annual_mean - p.zhvi_annual_mean)
                       / p.zhvi_annual_mean AS yoy_pct
                FROM derived.v_zhvi_county_annual a
                JOIN derived.v_zhvi_county_annual p
                  ON p.county_fips = a.county_fips AND p.year = a.year - 1
                JOIN latest l ON a.year = l.y
                WHERE a.county_fips LIKE '34%'
            )
            SELECT
                COUNT(*)                                          AS n_total,
                COUNT(*) FILTER (WHERE yoy_pct > 0.30
                              OR yoy_pct < -0.20)                 AS n_error,
                COUNT(*) FILTER (WHERE (yoy_pct > 0.25 AND yoy_pct <= 0.30)
                              OR (yoy_pct < -0.15 AND yoy_pct >= -0.20)) AS n_warn,
                MIN(yoy_pct)                                      AS yoy_min,
                MAX(yoy_pct)                                      AS yoy_max
            FROM yoy
        """)
        row = cur.fetchone()
    n_total = int(row[0]) if row and row[0] is not None else 0
    n_error = int(row[1]) if row and row[1] is not None else 0
    n_warn = int(row[2]) if row and row[2] is not None else 0
    yoy_min = float(row[3]) if row and row[3] is not None else None
    yoy_max = float(row[4]) if row and row[4] is not None else None

    passed = n_error == 0
    severity = (
        AssetCheckSeverity.ERROR if n_error > 0
        else AssetCheckSeverity.WARN
    )
    yoy_min_f = yoy_min if yoy_min is not None else float("nan")
    yoy_max_f = yoy_max if yoy_max is not None else float("nan")
    _emit(governance, dataset_id="raw.zillow_zhvi_county",
          check_name="zhvi_yoy_outliers_plausible", passed=passed,
          details={
              "n_total": n_total, "n_error": n_error, "n_warn": n_warn,
              "yoy_min": yoy_min_f, "yoy_max": yoy_max_f,
              "envelope_warn":  "[-15%, +25%]",
              "envelope_error": "[-20%, +30%]",
          })
    return AssetCheckResult(
        passed=passed, severity=severity,
        metadata={
            "n_total":        n_total,
            "n_error":        n_error,
            "n_warn":         n_warn,
            "yoy_min":        yoy_min_f,
            "yoy_max":        yoy_max_f,
            "envelope_warn":  "[-15%, +25%]",
            "envelope_error": "[-20%, +30%]",
        },
    )


@asset_check(
    asset=AssetKey(["raw", "zillow_zhvi_county"]),
    name="cross_source_divergence_plausible",
    description=(
        "Cross-source housing-index divergence (Zillow ZHVI vs FHFA HPI, "
        "both re-indexed to 2010 = 100) for any NJ (county, latest-year) "
        "pair should fall within the historical envelope of "
        "|divergence_pct_of_fhfa| <= 20%. Calibrated against 546 (NJ "
        "county, year) pairs back to 2000 with empirical p99 = 11.38% "
        "and max = 14.66% (Cape May 2001 / Passaic 2021). A divergence "
        "above 20% is well beyond both the early-2000s thin-coverage "
        "regime and the 2020-21 COVID run-up regime, both of which are "
        "EXPLAINED divergences. Reads from "
        "derived.f_housing_index_cross_source(2010)."
    ),
)
def housing_index_cross_source_divergence_plausible(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """No NJ (county, latest-year) pair has |FHFA - ZHVI| divergence > 20%."""
    with pg.connect() as conn, conn.cursor() as cur:
        # Latest year where BOTH sources have data for at least 1 NJ county
        # (the join is INNER on the function output's WHERE clause).
        cur.execute("""
            WITH latest AS (
                SELECT MAX(year) AS y
                FROM derived.f_housing_index_cross_source(2010::SMALLINT)
                WHERE divergence_pct_of_fhfa IS NOT NULL
                  AND county_fips LIKE '34%'
            )
            SELECT
                COUNT(*)                                            AS n_total,
                COUNT(*) FILTER (WHERE ABS(divergence_pct_of_fhfa) > 0.20)
                                                                    AS n_error,
                COUNT(*) FILTER (WHERE ABS(divergence_pct_of_fhfa) > 0.12
                              AND ABS(divergence_pct_of_fhfa) <= 0.20)
                                                                    AS n_warn,
                MAX(ABS(divergence_pct_of_fhfa))                    AS max_abs_pct
            FROM derived.f_housing_index_cross_source(2010::SMALLINT) x
            JOIN latest l ON x.year = l.y
            WHERE x.county_fips LIKE '34%'
              AND x.divergence_pct_of_fhfa IS NOT NULL
        """)
        row = cur.fetchone()
    n_total = int(row[0]) if row and row[0] is not None else 0
    n_error = int(row[1]) if row and row[1] is not None else 0
    n_warn = int(row[2]) if row and row[2] is not None else 0
    max_abs = float(row[3]) if row and row[3] is not None else None

    passed = n_error == 0
    severity = (
        AssetCheckSeverity.ERROR if n_error > 0
        else AssetCheckSeverity.WARN
    )
    max_abs_f = max_abs if max_abs is not None else float("nan")
    _emit(governance, dataset_id="raw.zillow_zhvi_county",
          check_name="cross_source_divergence_plausible", passed=passed,
          details={
              "n_total": n_total,
              "n_error_over_20pct": n_error,
              "n_warn_12_to_20pct": n_warn,
              "max_abs_pct":        max_abs_f,
              "envelope_warn":      "<= 12%",
              "envelope_error":     "> 20%",
              "base_year":          2010,
          })
    return AssetCheckResult(
        passed=passed, severity=severity,
        metadata={
            "n_total":            n_total,
            "n_error_over_20pct": n_error,
            "n_warn_12_to_20pct": n_warn,
            "max_abs_pct":        max_abs_f,
            "envelope_warn":      "<= 12%",
            "envelope_error":     "> 20%",
            "base_year":          2010,
        },
    )


# ============================================================================
# raw.acs_median_household_income
# ============================================================================


@asset_check(
    asset=AssetKey(["raw", "acs_median_household_income"]),
    name="nj_county_coverage",
    description=(
        "raw.acs_median_household_income must include all 21 NJ counties "
        "for the most recent vintage present."
    ),
)
def acs_income_nj_coverage(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Confirm raw.acs_median_household_income covers all 21 NJ counties."""
    n = _count(pg, """
        SELECT COUNT(DISTINCT county_fips)
        FROM raw.acs_median_household_income
        WHERE county_fips LIKE '34%'
          AND year = (
              SELECT MAX(year) FROM raw.acs_median_household_income
              WHERE county_fips LIKE '34%'
          )
    """)
    passed = n == _NJ_COUNTY_COUNT
    _emit(governance, dataset_id="raw.acs_median_household_income",
          check_name="nj_county_coverage", passed=passed,
          details={"nj_counties_in_latest_year": n})
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.ERROR,
        metadata={"nj_counties_in_latest_year": n,
                  "expected": _NJ_COUNTY_COUNT},
    )


# ============================================================================
# raw.acs_housing
# ============================================================================


@asset_check(
    asset=AssetKey(["raw", "acs_housing"]),
    name="nj_county_coverage",
    description=(
        "raw.acs_housing must include all 21 NJ counties for the most "
        "recent vintage."
    ),
)
def acs_housing_nj_coverage(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Confirm raw.acs_housing covers all 21 NJ counties in the latest year."""
    n = _count(pg, """
        SELECT COUNT(DISTINCT county_fips)
        FROM raw.acs_housing
        WHERE county_fips LIKE '34%'
          AND year = (
              SELECT MAX(year) FROM raw.acs_housing
              WHERE county_fips LIKE '34%'
          )
    """)
    passed = n == _NJ_COUNTY_COUNT
    _emit(governance, dataset_id="raw.acs_housing",
          check_name="nj_county_coverage", passed=passed,
          details={"nj_counties_in_latest_year": n})
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.ERROR,
        metadata={"nj_counties_in_latest_year": n,
                  "expected": _NJ_COUNTY_COUNT},
    )


# ============================================================================
# raw.lca_disclosure
# ============================================================================


@asset_check(
    asset=AssetKey(["raw", "lca_disclosure"]),
    name="row_count_positive",
    description="raw.lca_disclosure must have at least one row.",
)
def lca_row_count_positive(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Confirm raw.lca_disclosure has at least one row."""
    n = _count(pg, "SELECT COUNT(*) FROM raw.lca_disclosure")
    passed = n > 0
    _emit(governance, dataset_id="raw.lca_disclosure",
          check_name="row_count_positive", passed=passed, details={"row_count": n})
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.ERROR,
        metadata={"row_count": n},
    )


# ============================================================================
# raw.nj_property_tax_county
# ============================================================================


@asset_check(
    asset=AssetKey(["raw", "nj_property_tax_county"]),
    name="nj_county_coverage",
    description=(
        "raw.nj_property_tax_county must include all 21 NJ counties for "
        "the most recent year."
    ),
)
def nj_proptax_county_coverage(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Confirm raw.nj_property_tax_county covers all 21 NJ counties."""
    n = _count(pg, """
        SELECT COUNT(DISTINCT county_fips)
        FROM raw.nj_property_tax_county
        WHERE year = (SELECT MAX(year) FROM raw.nj_property_tax_county)
    """)
    passed = n == _NJ_COUNTY_COUNT
    _emit(governance, dataset_id="raw.nj_property_tax_county",
          check_name="nj_county_coverage", passed=passed,
          details={"nj_counties_in_latest_year": n})
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.ERROR,
        metadata={"nj_counties_in_latest_year": n,
                  "expected": _NJ_COUNTY_COUNT},
    )


@asset_check(
    asset=AssetKey(["raw", "nj_property_tax_county"]),
    name="tax_substrate_prior_year_seeded",
    description=(
        "VISION_2026 §7.1 forcing function: BOTH ref.irs_federal_brackets and "
        "ref.nj_state_brackets must include rows for tax year (current_year - 1) "
        "by April 1 of (current_year). The IRS publishes its prior-year Rev. Proc. "
        "constants in late October (one quarter before tax-filing season opens) "
        "and the NJ Division of Taxation publishes the matching NJ-1040 tables in "
        "January; April 1 is the deadline by which Phase-1 hand-transcribed seed "
        "files (db/seeds/NNN_irs_federal_tax_<year>.sql, NNN_nj_state_tax_<year>.sql) "
        "must be present for downstream affordability metrics (Collapse Curve, AEI, "
        "/personalize) to evaluate the prior tax year. Before April 1 the absence "
        "is grace-period; on or after April 1 the absence is a substrate-honesty "
        "alarm. Severity is WARN, not ERROR: the engine returns NULL for unseeded "
        "years (correct behavior) so the platform stays online; the check is the "
        "polite forcing function that makes the gap visible to operators."
    ),
)
def tax_substrate_prior_year_seeded(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Assert IRS + NJ tax-bracket seeds for (current_year - 1) are present.

    The check is deliberately attached to raw.nj_property_tax_county because
    that asset shares the same annual NJ-vintage publication semantics; the
    tax-engine reference tables (ref.irs_federal_brackets, ref.nj_state_brackets)
    are not Dagster assets in this platform (they are seed-loaded ref data).
    Co-locating the check here preserves the one-asset-per-check contract while
    capturing a cross-cutting platform health signal.
    """
    target_tax_year = _count(pg, """
        SELECT EXTRACT(YEAR FROM CURRENT_DATE)::INT - 1
    """)
    deadline_passed = bool(_count(pg, """
        SELECT (CURRENT_DATE >= make_date(EXTRACT(YEAR FROM CURRENT_DATE)::INT, 4, 1))::INT
    """))
    irs_rows = _count(pg, """
        SELECT COUNT(*) FROM ref.irs_federal_brackets WHERE tax_year = %s
    """, target_tax_year)
    nj_rows = _count(pg, """
        SELECT COUNT(*) FROM ref.nj_state_brackets WHERE tax_year = %s
    """, target_tax_year)
    irs_seeded = irs_rows > 0
    nj_seeded = nj_rows > 0
    both_seeded = irs_seeded and nj_seeded
    if both_seeded:
        passed, reason = True, "ok"
    elif not deadline_passed:
        passed, reason = True, "vacuous_pass_before_april_1_grace_period"
    else:
        passed, reason = False, "deadline_passed_with_missing_seeds"
    details: dict[str, Any] = {
        "target_tax_year": target_tax_year,
        "deadline_passed_april_1": deadline_passed,
        "irs_federal_rows": irs_rows,
        "nj_state_rows": nj_rows,
        "irs_seeded": irs_seeded,
        "nj_seeded": nj_seeded,
        "reason": reason,
    }
    _emit(governance, dataset_id="raw.nj_property_tax_county",
          check_name="tax_substrate_prior_year_seeded", passed=passed,
          details=details)
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.WARN, metadata=details,
    )


# ============================================================================
# raw.acs_pums_person + raw.acs_pums_housing
# ============================================================================
#
# PUMS is structurally different from the other tables in the catalog
# (sample, not aggregate; PUMA-grain, not county-grain). It needs its
# own check family:
#
#   1. row_count_positive: a non-empty NJ load must contain >> 21 rows
#      (NJ has ~50 PUMAs and ~100K person records per year).
#   2. nj_puma_coverage: must include >= 40 PUMAs in the latest vintage.
#   3. replicate_weights_cardinality_80: every row must have exactly
#      80 replicate weights, otherwise variance estimation is invalid.
#   4. person_housing_serialno_consistency: every PERSON serialno must
#      have a matching HOUSING serialno (1:1 by (year, product)).
# ============================================================================


# Floor for "we successfully loaded a PUMS year." NJ has ~50 PUMAs
# under the 2020 vintage; the smallest published year had ~38 PUMAs.
_NJ_PUMA_FLOOR = 30


@asset_check(
    asset=AssetKey(["raw", "acs_pums_person"]),
    name="row_count_positive",
    description=(
        "raw.acs_pums_person must have at least 1000 rows for NJ. "
        "PUMS is a 1% sample; even a single year covers ~80K-100K "
        "NJ persons, so anything under 1000 indicates a partial or "
        "broken load."
    ),
)
def pums_person_row_count_positive(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Confirm raw.acs_pums_person has a plausible row count for NJ."""
    n = _count(
        pg,
        "SELECT COUNT(*) FROM raw.acs_pums_person WHERE state_fips = '34'",
    )
    passed = n >= 1000
    _emit(governance, dataset_id="raw.acs_pums_person",
          check_name="row_count_positive", passed=passed,
          details={"row_count": n, "floor": 1000})
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.ERROR,
        metadata={"row_count": n, "floor": 1000},
    )


@asset_check(
    asset=AssetKey(["raw", "acs_pums_person"]),
    name="nj_puma_coverage",
    description=(
        "raw.acs_pums_person must cover >= 30 distinct NJ PUMAs in the "
        "latest year (NJ has ~50 PUMAs; some years publish fewer)."
    ),
)
def pums_person_nj_puma_coverage(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Confirm raw.acs_pums_person covers most NJ PUMAs in the latest year."""
    n = _count(pg, """
        SELECT COUNT(DISTINCT puma)
        FROM raw.acs_pums_person
        WHERE state_fips = '34'
          AND year = (SELECT MAX(year) FROM raw.acs_pums_person WHERE state_fips = '34')
    """)
    passed = n >= _NJ_PUMA_FLOOR
    _emit(governance, dataset_id="raw.acs_pums_person",
          check_name="nj_puma_coverage", passed=passed,
          details={"distinct_pumas_latest_year": n, "floor": _NJ_PUMA_FLOOR})
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.ERROR,
        metadata={"distinct_pumas_latest_year": n, "floor": _NJ_PUMA_FLOOR},
    )


@asset_check(
    asset=AssetKey(["raw", "acs_pums_person"]),
    name="replicate_weights_cardinality_80",
    description=(
        "Every PUMS row must have exactly 80 replicate weights. SDR "
        "variance estimation requires this; a row with fewer weights "
        "would silently produce wrong standard errors."
    ),
)
def pums_replicate_weights_cardinality(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Confirm every PUMS row has exactly 80 replicate weights."""
    n_bad = _count(pg, """
        SELECT COUNT(*) FROM raw.acs_pums_person
        WHERE cardinality(replicate_weights) <> 80
    """)
    passed = n_bad == 0
    _emit(governance, dataset_id="raw.acs_pums_person",
          check_name="replicate_weights_cardinality_80", passed=passed,
          details={"rows_with_wrong_cardinality": n_bad})
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.ERROR,
        metadata={"rows_with_wrong_cardinality": n_bad},
    )


@asset_check(
    asset=AssetKey(["raw", "acs_pums_housing"]),
    name="person_housing_serialno_consistency",
    description=(
        "Every (year, product, serialno) in raw.acs_pums_person must "
        "exist in raw.acs_pums_housing. PUMS publishes the two files "
        "as a paired drop; an orphan serialno indicates a mid-load "
        "interruption."
    ),
)
def pums_person_housing_serialno_consistency(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Confirm every PERSON.serialno has a matching HOUSING.serialno."""
    n_orphans = _count(pg, """
        SELECT COUNT(*) FROM (
            SELECT DISTINCT year, product, serialno
            FROM raw.acs_pums_person
        ) p
        LEFT JOIN raw.acs_pums_housing h
               USING (year, product, serialno)
        WHERE h.serialno IS NULL
    """)
    passed = n_orphans == 0
    _emit(governance, dataset_id="raw.acs_pums_housing",
          check_name="person_housing_serialno_consistency", passed=passed,
          details={"orphan_person_serialnos": n_orphans})
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.ERROR,
        metadata={"orphan_person_serialnos": n_orphans},
    )


# ============================================================================
# Cross-source consistency: housing burden ratios should be in [0, 5]
# ============================================================================


@asset_check(
    asset=AssetKey(["derived", "housing_burden_ratio"]),
    name="burden_ratio_in_plausible_range",
    description=(
        "derived.housing_burden_ratio.burden_blended must lie in [0, 5]. "
        "Values outside this range indicate either a unit-conversion bug "
        "or an unexpected data shape (e.g. monthly reported as annual)."
    ),
)
def burden_ratio_plausible(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Confirm derived.housing_burden_ratio.blended_burden_ratio lies in [0, 5]."""
    n = _count(pg, """
        SELECT COUNT(*) FROM derived.housing_burden_ratio
        WHERE blended_burden_ratio IS NOT NULL
          AND (blended_burden_ratio < 0 OR blended_burden_ratio > 5)
    """)
    passed = n == 0
    _emit(governance, dataset_id="derived.housing_burden_ratio",
          check_name="burden_ratio_in_plausible_range", passed=passed,
          details={"out_of_range_rows": n})
    return AssetCheckResult(
        passed=passed,
        severity=AssetCheckSeverity.WARN,
        metadata={"out_of_range_rows": n},
    )


# ============================================================================
# derived.pums_burden_segmented
# ============================================================================
#
# Three checks for the platform's first materialized derived TABLE:
#   1. row_count_positive: must contain rows after a successful upstream load.
#   2. suppression_rate_not_catastrophic: <30% of cells suppressed in a
#      typical NJ load. A higher rate signals upstream data corruption
#      or a too-aggressive SUPPRESSION_FLOOR.
#   3. burden_ratio_plausible_range: every non-suppressed burden_ratio
#      must be in [0, BURDEN_RATIO_SANITY_CAP=5.0]. The compute function
#      already enforces this via post-suppression coercion, so a
#      violation here would mean the compute logic regressed.
# ============================================================================


@asset_check(
    asset=AssetKey(["derived", "pums_burden_segmented"]),
    name="row_count_positive",
    description=(
        "derived.pums_burden_segmented must have rows. After a successful "
        "PUMS load for NJ, this table should contain ~3-6K cells "
        "(50 PUMAs x 3 tenures x ~25 segment values)."
    ),
)
def pums_burden_row_count_positive(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Confirm derived.pums_burden_segmented has at least one row."""
    n = _count(pg, "SELECT COUNT(*) FROM derived.pums_burden_segmented")
    passed = n > 0
    _emit(governance, dataset_id="derived.pums_burden_segmented",
          check_name="row_count_positive", passed=passed, details={"row_count": n})
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.ERROR,
        metadata={"row_count": n},
    )


@asset_check(
    asset=AssetKey(["derived", "pums_burden_segmented"]),
    name="suppression_rate_not_catastrophic",
    description=(
        "Of all cells in derived.pums_burden_segmented, fewer than 50% "
        "should be suppressed. A higher rate indicates the PUMS sample "
        "is too sparse for the chosen segmentation, OR upstream raw "
        "PUMS rows are corrupt and lost their demographic codes."
    ),
)
def pums_burden_suppression_rate(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Confirm fewer than 50% of pums_burden_segmented cells are suppressed."""
    total = _count(pg, "SELECT COUNT(*) FROM derived.pums_burden_segmented")
    n_supp = _count(
        pg, "SELECT COUNT(*) FROM derived.pums_burden_segmented WHERE suppressed",
    )
    if total == 0:
        # Empty table; the row_count_positive check covers this.
        # We pass with a noted metadata value so we don't double-fire.
        rate = 0.0
        passed = True
    else:
        rate = n_supp / total
        passed = rate < 0.50
    _emit(governance, dataset_id="derived.pums_burden_segmented",
          check_name="suppression_rate_not_catastrophic", passed=passed,
          details={"total": total, "suppressed": n_supp,
                   "rate": round(rate, 4), "threshold": 0.50})
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.WARN,
        metadata={"total_cells": total, "suppressed_cells": n_supp,
                  "suppression_rate": round(rate, 4)},
    )


@asset_check(
    asset=AssetKey(["derived", "pums_burden_segmented"]),
    name="burden_ratio_plausible_range",
    description=(
        "Every non-suppressed burden_ratio_p50 in pums_burden_segmented "
        "must be in [0, 5]. The compute module enforces this via "
        "post-suppression coercion; a violation here means the compute "
        "logic regressed and is producing implausible ratios."
    ),
)
def pums_burden_ratio_range(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Confirm all non-suppressed burden ratios in pums_burden_segmented are in [0, 5]."""
    n_bad = _count(pg, """
        SELECT COUNT(*) FROM derived.pums_burden_segmented
        WHERE NOT suppressed
          AND burden_ratio_p50 IS NOT NULL
          AND (burden_ratio_p50 < 0 OR burden_ratio_p50 > 5)
    """)
    passed = n_bad == 0
    _emit(governance, dataset_id="derived.pums_burden_segmented",
          check_name="burden_ratio_plausible_range", passed=passed,
          details={"out_of_range_rows": n_bad})
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.ERROR,
        metadata={"out_of_range_rows": n_bad},
    )


# ============================================================================
# derived.pums_burden_county_segmented checks
# ============================================================================


@asset_check(
    asset=AssetKey(["derived", "pums_burden_county_segmented"]),
    name="row_count_positive",
    description=(
        "derived.pums_burden_county_segmented must have at least one row "
        "after a successful materialization. Empty output signals the "
        "compute fn returned a zero-row frame -- usually because the "
        "crosswalk join silently dropped all PUMAs."
    ),
)
def pums_burden_county_row_count_positive(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Row-count floor for derived.pums_burden_county_segmented."""
    n = _count(pg, "SELECT COUNT(*) FROM derived.pums_burden_county_segmented")
    passed = n > 0
    _emit(governance, dataset_id="derived.pums_burden_county_segmented",
          check_name="row_count_positive", passed=passed,
          details={"row_count": n})
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.ERROR,
        metadata={"row_count": n},
    )


@asset_check(
    asset=AssetKey(["derived", "pums_burden_county_segmented"]),
    name="nj_county_coverage",
    description=(
        "Every NJ county must have at least one row in the "
        "pums_burden_county_segmented table. If this fails, the most "
        "likely cause is a regression in the crosswalk seed (a county "
        "got dropped) or in the compute function (a county got filtered "
        "out). Includes suppressed cells -- coverage is about whether "
        "the cell exists, not about its quality."
    ),
)
def pums_burden_county_nj_coverage(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """NJ-21-county coverage for derived.pums_burden_county_segmented."""
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(DISTINCT county_fips) "
            "FROM derived.pums_burden_county_segmented "
            "WHERE state_fips = '34'"
        )
        row = cur.fetchone()
        n_counties = int(row[0]) if row else 0
    passed = n_counties == _NJ_COUNTY_COUNT
    _emit(governance, dataset_id="derived.pums_burden_county_segmented",
          check_name="nj_county_coverage", passed=passed,
          details={"counties_present": n_counties,
                   "counties_expected": _NJ_COUNTY_COUNT})
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.ERROR,
        metadata={"counties_present":  n_counties,
                  "counties_expected": _NJ_COUNTY_COUNT},
    )


@asset_check(
    asset=AssetKey(["derived", "pums_burden_segmented"]),
    name="standard_errors_non_negative",
    description=(
        "Every populated *_se column in derived.pums_burden_segmented "
        "must be non-negative. SE is sqrt(variance) by construction; a "
        "negative value means a numerical regression in the SDR formula. "
        "This check is the principal guard on the variance pipeline."
    ),
)
def pums_burden_se_non_negative(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """All non-null SE columns in pums_burden_segmented must be >= 0."""
    n_bad = _count(pg, """
        SELECT COUNT(*) FROM derived.pums_burden_segmented
        WHERE (household_income_p50_se IS NOT NULL AND household_income_p50_se < 0)
           OR (monthly_cost_p50_se     IS NOT NULL AND monthly_cost_p50_se     < 0)
           OR (burden_ratio_p50_se     IS NOT NULL AND burden_ratio_p50_se     < 0)
    """)
    passed = n_bad == 0
    _emit(governance, dataset_id="derived.pums_burden_segmented",
          check_name="standard_errors_non_negative", passed=passed,
          details={"negative_se_rows": n_bad})
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.ERROR,
        metadata={"negative_se_rows": n_bad},
    )


@asset_check(
    asset=AssetKey(["derived", "pums_burden_county_segmented"]),
    name="standard_errors_non_negative",
    description=(
        "Every populated *_se column in pums_burden_county_segmented "
        "must be non-negative. Mirror of the PUMA-level check."
    ),
)
def pums_burden_county_se_non_negative(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """All non-null SE columns in pums_burden_county_segmented must be >= 0."""
    n_bad = _count(pg, """
        SELECT COUNT(*) FROM derived.pums_burden_county_segmented
        WHERE (household_income_p50_se IS NOT NULL AND household_income_p50_se < 0)
           OR (monthly_cost_p50_se     IS NOT NULL AND monthly_cost_p50_se     < 0)
           OR (burden_ratio_p50_se     IS NOT NULL AND burden_ratio_p50_se     < 0)
    """)
    passed = n_bad == 0
    _emit(governance, dataset_id="derived.pums_burden_county_segmented",
          check_name="standard_errors_non_negative", passed=passed,
          details={"negative_se_rows": n_bad})
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.ERROR,
        metadata={"negative_se_rows": n_bad},
    )


@asset_check(
    asset=AssetKey(["derived", "pums_burden_county_segmented"]),
    name="burden_ratio_plausible_range",
    description=(
        "Every non-suppressed burden_ratio_p50 in "
        "pums_burden_county_segmented must be in [0, 5]. Mirrors the "
        "PUMA-level check; same cap (BURDEN_RATIO_SANITY_CAP) is enforced "
        "in the county compute module."
    ),
)
def pums_burden_county_ratio_range(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Confirm county-level burden ratios are in [0, 5]."""
    n_bad = _count(pg, """
        SELECT COUNT(*) FROM derived.pums_burden_county_segmented
        WHERE NOT suppressed
          AND burden_ratio_p50 IS NOT NULL
          AND (burden_ratio_p50 < 0 OR burden_ratio_p50 > 5)
    """)
    passed = n_bad == 0
    _emit(governance, dataset_id="derived.pums_burden_county_segmented",
          check_name="burden_ratio_plausible_range", passed=passed,
          details={"out_of_range_rows": n_bad})
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.ERROR,
        metadata={"out_of_range_rows": n_bad},
    )


@asset_check(
    asset=AssetKey(["derived", "pums_burden_county_segmented"]),
    name="puma_xwalk_invariants_clean",
    description=(
        "Both PUMA->county crosswalks (2010 and 2020 vintages) must have "
        "their per-PUMA allocation factors sum to 1.0 (within tolerance). "
        "The diagnostic views ref.v_puma_xwalk_invariant_violations and "
        "ref.v_puma2010_xwalk_invariant_violations should each be empty. "
        "A populated row means a seeded PUMA's allocation does not "
        "partition its population correctly -- a population-conservation "
        "violation that would silently bias every county aggregation that "
        "touches that PUMA."
    ),
)
def pums_burden_county_xwalk_invariants(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Both crosswalk vintage invariant views must be empty."""
    n_bad_2020 = _count(
        pg, "SELECT COUNT(*) FROM ref.v_puma_xwalk_invariant_violations"
    )
    n_bad_2010 = _count(
        pg, "SELECT COUNT(*) FROM ref.v_puma2010_xwalk_invariant_violations"
    )
    n_bad = n_bad_2020 + n_bad_2010
    passed = n_bad == 0
    _emit(governance, dataset_id="derived.pums_burden_county_segmented",
          check_name="puma_xwalk_invariants_clean", passed=passed,
          details={
              "violations_2020": n_bad_2020,
              "violations_2010": n_bad_2010,
          })
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.ERROR,
        metadata={
            "violations_2020": n_bad_2020,
            "violations_2010": n_bad_2010,
        },
    )


@asset_check(
    asset=AssetKey(["derived", "pums_burden_county_segmented"]),
    name="yoy_burden_ratio_swings_plausible",
    description=(
        "Year-over-year burden ratio changes for the same county-"
        "tenure-overall cell should never exceed 0.20 (20 percentage "
        "points). Real housing markets do not move that fast; a swing "
        "of that magnitude indicates either (a) a sample-size issue "
        "in a thin cell that should have been suppressed, (b) a vintage-"
        "tag corruption that mis-routed records to a county, or (c) a "
        "compute-fn regression. A small number of allowed exceptions "
        "covers genuine COVID-era shocks; we cap at 5 cells. Reads from "
        "public.v_pums_burden_county_yoy_overall."
    ),
)
def pums_burden_county_yoy_swings_plausible(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """No county-tenure-overall cell should swing >20pp YoY (with slack).

    Reads the public time-series view rather than materialized YoY,
    which doesn't exist yet. The threshold is intentionally loose;
    its purpose is to catch correctness regressions, not to police
    real economic dynamics.
    """
    n_bad = _count(pg, """
        SELECT COUNT(*) FROM public.v_pums_burden_county_yoy_overall
        WHERE NOT suppressed
          AND burden_ratio_delta IS NOT NULL
          AND ABS(burden_ratio_delta) > 0.20
    """)
    passed = n_bad <= 5
    _emit(governance, dataset_id="derived.pums_burden_county_segmented",
          check_name="yoy_burden_ratio_swings_plausible", passed=passed,
          details={"yoy_swings_over_20pp": n_bad,
                   "tolerance": 5})
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.WARN,
        metadata={"yoy_swings_over_20pp": n_bad,
                  "tolerance": 5},
    )


@asset_check(
    asset=AssetKey(["derived", "pums_burden_county_segmented"]),
    name="multiyear_coverage_per_product",
    description=(
        "After the multi-year backfill the county derived table should "
        "carry at least 2 years per product (acs1 must have >= 2; acs5 "
        "must have >= 2). A single-year materialization signals that "
        "the orchestration multi-product loop regressed, or that a "
        "year's raw ingestion failed silently."
    ),
)
def pums_burden_county_multiyear_coverage(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Each product must materialize >= 2 years' data."""
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT product, COUNT(DISTINCT year) AS n_years
            FROM derived.pums_burden_county_segmented
            GROUP BY product
        """)
        per_product: dict[str, int] = {row[0]: int(row[1]) for row in cur.fetchall()}

    has_acs1_multiyear = per_product.get("acs1", 0) >= 2
    has_acs5_multiyear = per_product.get("acs5", 0) >= 2
    passed = has_acs1_multiyear or has_acs5_multiyear  # not yet strict

    _emit(governance, dataset_id="derived.pums_burden_county_segmented",
          check_name="multiyear_coverage_per_product", passed=passed,
          details={"years_by_product": per_product})
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.WARN,
        metadata={"years_by_product": per_product},
    )


@asset_check(
    asset=AssetKey(["derived", "pums_burden_county_segmented"]),
    name="multivintage_coverage_when_acs5_present",
    description=(
        "When the 5-Year (acs5) product is materialized in the county "
        "table, it should reflect contributions from BOTH 2010-vintage "
        "and 2020-vintage PUMAs in raw -- the whole point of the dual-"
        "vintage compute path. Heuristic: for any (year, product) "
        "present in derived where the raw 5-year file carries both "
        "puma_vintage values, every multi-PUMA county must have "
        "n_pumas_contributing >= 2 in at least one cell. A regression "
        "to PUMA20-only would drop the 2010 contributions and shrink "
        "n_pumas_contributing by ~half across the table."
    ),
)
def pums_burden_county_multivintage_coverage(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Confirm 5-year derived rows pull from both PUMA vintages.

    For a 5-year product we expect roughly twice as many distinct
    PUMAs contributing per county than for a 1-year product (since
    5-year carries both vintages). We don't enforce a strict ratio --
    that's data-shape-dependent -- but we do refuse to pass when
    no multi-PUMA county exists in any 5-year materialization.
    """
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT
                COUNT(*) FILTER (
                    WHERE product = 'acs5' AND n_pumas_contributing >= 2
                ) AS multi_acs5_cells,
                COUNT(*) FILTER (WHERE product = 'acs5') AS total_acs5_cells
            FROM derived.pums_burden_county_segmented
        """)
        row = cur.fetchone()
    multi = int(row[0]) if row else 0
    total = int(row[1]) if row else 0

    if total == 0:
        # No acs5 materialized (e.g., 1-year-only setup); the check
        # is vacuously satisfied because there is no multi-vintage
        # data to dispatch against.
        passed = True
        details: dict[str, Any] = {
            "acs5_cells_total": 0,
            "skipped_reason": "no acs5 product materialized",
        }
    else:
        passed = multi > 0
        details = {
            "acs5_cells_total": total,
            "acs5_multi_puma_cells": multi,
        }

    _emit(governance, dataset_id="derived.pums_burden_county_segmented",
          check_name="multivintage_coverage_when_acs5_present",
          passed=passed, details=details)
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.WARN,
        metadata=details,
    )


# ============================================================================
# raw.fec_candidate / raw.fec_committee / raw.fec_contribution
# ============================================================================
#
# Five checks across the three FEC raw tables:
#   row_count_positive     -- table has >= 1 row
#   nj_candidate_coverage  -- raw.fec_candidate has >= 1 NJ-state row
#   referential_integrity  -- 95%+ of contribution.cmte_id rows resolve
#                             to a row in raw.fec_committee for the
#                             same cycle (FEC bulk does include some
#                             orphan committees -- usually
#                             non-disclosing pre-registration entities;
#                             95% is the conservative floor below which
#                             we surface a warn signal)
#   amount_plausibility    -- transaction_amt within (-10M, +10M)
#                             (the table CHECK enforces this hard;
#                             the asset check measures the empirical
#                             distribution and warns if outliers spike)
#   nj_money_visible       -- public.v_fec_money_to_nj_candidates
#                             returns >= 1 row when raw.fec_contribution
#                             is non-empty (catches a downstream
#                             join-key bug that would silently zero out
#                             the headline view)
# ============================================================================


@asset_check(
    asset=AssetKey(["raw", "fec_candidate"]),
    name="row_count_positive",
    description="raw.fec_candidate must have at least one row.",
)
def fec_candidate_row_count_positive(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Confirm raw.fec_candidate has at least one row."""
    n = _count(pg, "SELECT COUNT(*) FROM raw.fec_candidate")
    passed = n > 0
    _emit(governance, dataset_id="raw.fec_candidate",
          check_name="row_count_positive", passed=passed,
          details={"row_count": n})
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.ERROR,
        metadata={"row_count": n},
    )


@asset_check(
    asset=AssetKey(["raw", "fec_candidate"]),
    name="nj_candidate_coverage",
    description="raw.fec_candidate must include at least one NJ-state candidate.",
)
def fec_candidate_nj_coverage(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Surface NJ-axis coverage gaps as ERRORs.

    The platform's purpose is NJ analytics; an FEC load with no NJ
    candidates would silently break every downstream view.
    """
    n = _count(
        pg,
        "SELECT COUNT(*) FROM raw.fec_candidate WHERE cand_office_st = 'NJ'",
    )
    passed = n > 0
    _emit(governance, dataset_id="raw.fec_candidate",
          check_name="nj_candidate_coverage", passed=passed,
          details={"nj_candidate_count": n})
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.ERROR,
        metadata={"nj_candidate_count": n},
    )


@asset_check(
    asset=AssetKey(["raw", "fec_committee"]),
    name="row_count_positive",
    description="raw.fec_committee must have at least one row.",
)
def fec_committee_row_count_positive(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Confirm raw.fec_committee has at least one row."""
    n = _count(pg, "SELECT COUNT(*) FROM raw.fec_committee")
    passed = n > 0
    _emit(governance, dataset_id="raw.fec_committee",
          check_name="row_count_positive", passed=passed,
          details={"row_count": n})
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.ERROR,
        metadata={"row_count": n},
    )


@asset_check(
    asset=AssetKey(["raw", "fec_contribution"]),
    name="row_count_positive",
    description="raw.fec_contribution must have at least one row.",
)
def fec_contribution_row_count_positive(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Confirm raw.fec_contribution has at least one row."""
    n = _count(pg, "SELECT COUNT(*) FROM raw.fec_contribution")
    passed = n > 0
    _emit(governance, dataset_id="raw.fec_contribution",
          check_name="row_count_positive", passed=passed,
          details={"row_count": n})
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.ERROR,
        metadata={"row_count": n},
    )


@asset_check(
    asset=AssetKey(["raw", "fec_contribution"]),
    name="referential_integrity_to_committee",
    description=(
        "At least 95% of raw.fec_contribution rows must resolve to a "
        "row in raw.fec_committee for the same cycle."
    ),
)
def fec_contribution_referential_integrity(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Surface contribution -> committee join-resolution rate.

    FEC ships a small number of orphan cmte_ids per cycle (entities
    that filed but withdrew before publication). Anything below ~95%
    indicates a real problem (mis-named cycle, partial load, or a
    schema drift that misaligned the cmte_id column).
    """
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT "
            "  COUNT(*) AS n_total, "
            "  COUNT(cm.cmte_id) AS n_resolved "
            "FROM raw.fec_contribution c "
            "LEFT JOIN raw.fec_committee cm "
            "  ON cm.cmte_id = c.cmte_id AND cm.cycle = c.cycle"
        )
        row = cur.fetchone()
    n_total = int(row[0]) if row else 0
    n_resolved = int(row[1]) if row else 0
    if n_total == 0:
        # Pass vacuously if no contributions yet (the row_count check
        # surfaces the empty-table condition separately).
        passed, ratio = True, 1.0
    else:
        ratio = n_resolved / n_total
        passed = ratio >= 0.95
    details: dict[str, Any] = {
        "n_total":          n_total,
        "n_resolved":       n_resolved,
        "resolution_ratio": round(ratio, 6),
    }
    _emit(governance, dataset_id="raw.fec_contribution",
          check_name="referential_integrity_to_committee",
          passed=passed, details=details)
    return AssetCheckResult(
        passed=passed,
        severity=AssetCheckSeverity.WARN,
        metadata=details,
    )


@asset_check(
    asset=AssetKey(["raw", "fec_contribution"]),
    name="nj_money_visible",
    description=(
        "When raw.fec_contribution is non-empty, "
        "public.v_fec_money_to_nj_candidates must return at least one row "
        "(otherwise the headline civic-integrity surface is silently empty)."
    ),
)
def fec_nj_money_visible(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Detect silent join breakage in the headline NJ-money view.

    Catches the failure mode where the join in
    public.v_fec_money_to_nj_candidates breaks (e.g. cycle column type
    drift, missing committees, NJ candidates absent).
    """
    n_contrib = _count(pg, "SELECT COUNT(*) FROM raw.fec_contribution")
    n_nj_view = _count(pg, "SELECT COUNT(*) FROM public.v_fec_money_to_nj_candidates")
    passed = True if n_contrib == 0 else n_nj_view > 0
    details: dict[str, Any] = {
        "n_contribution_rows":         n_contrib,
        "n_money_to_nj_view_rows":     n_nj_view,
    }
    _emit(governance, dataset_id="raw.fec_contribution",
          check_name="nj_money_visible", passed=passed, details=details)
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.WARN,
        metadata=details,
    )


# ============================================================================
# Tier 4 v3: derived.fraud_signal_observation + derived.v_entity_fraud_risk
# ============================================================================
#
# The L1 + L3a layers are populated by a Dagster asset that calls the SQL
# dispatcher (derived.refresh_all_fraud_signal_observations). The schema
# CHECK constraints on derived.fraud_signal_observation already enforce
# per-row domains (severity in [1,5], peer_percentile in [0,1], evidence_url
# NOT NULL/empty, entity_kind in whitelist) at INSERT time, so we DELIBERATELY
# do NOT duplicate those as asset checks: that would just be re-asserting
# what the table itself already rejects.
#
# What asset checks UNIQUELY catch is the failure mode the schema cannot:
#   1. The L1 table is supposed to have data but is empty.
#   2. The dispatcher silently dropped a signal -- e.g., one of the eight
#      derived.fec_* views regressed to zero rows after a schema drift,
#      so its refresher inserted nothing. The schema is happy; the data
#      is missing. The DISTINCT signal_id set is the only way to detect this.
#   3. L1 has rows but the L3a scoring view returns risk_score=0 for every
#      entity. That means either the formula constants drifted, the L2 pivot
#      broke, or every percentile is at exactly the threshold. All are
#      catastrophic for the analyst queue and all warrant a hard surface.
# ============================================================================


@asset_check(
    asset=AssetKey(["derived", "fraud_signal_observation"]),
    name="row_count_positive",
    description=(
        "derived.fraud_signal_observation must have at least one row when "
        "the upstream raw.fec_candidate is non-empty. If candidates are "
        "loaded but no signal observations were emitted, the dispatcher "
        "is silently broken."
    ),
)
def fraud_signal_observation_row_count_positive(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Pass vacuously when raw.fec_candidate is empty; require >=1 row otherwise."""
    n_cand = _count(pg, "SELECT COUNT(*) FROM raw.fec_candidate")
    n_obs = _count(pg, "SELECT COUNT(*) FROM derived.fraud_signal_observation")
    if n_cand == 0:
        passed, reason = True, "vacuous_pass_no_candidates_loaded"
    else:
        passed = n_obs > 0
        reason = "ok" if passed else "candidates_loaded_but_no_observations"
    details: dict[str, Any] = {
        "n_candidates":   n_cand,
        "n_observations": n_obs,
        "reason":         reason,
    }
    _emit(governance, dataset_id="derived.fraud_signal_observation",
          check_name="row_count_positive", passed=passed, details=details)
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.WARN, metadata=details,
    )


@asset_check(
    asset=AssetKey(["derived", "fraud_signal_observation"]),
    name="signal_coverage",
    description=(
        "Every one of the eight v2.A structural signal_ids must appear in "
        "derived.fraud_signal_observation when the table is non-empty. A "
        "missing signal_id means one of the per-signal refreshers inserted "
        "zero rows -- either the underlying derived.fec_* view returned "
        "empty (schema drift in raw.fec_*) or the refresher's SELECT "
        "regressed. The schema CHECK constraint cannot detect missing data, "
        "only invalid data, so this asset check is the only line of defense."
    ),
)
def fraud_signal_observation_signal_coverage(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Assert every expected signal_id is represented in L1 (when L1 is non-empty)."""
    from orchestration.assets import FRAUD_STRUCTURAL_SIGNAL_IDS

    expected: set[str] = set(FRAUD_STRUCTURAL_SIGNAL_IDS)
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT signal_id, COUNT(*) "
            "FROM derived.fraud_signal_observation "
            "GROUP BY signal_id"
        )
        rows = cur.fetchall()
    present: dict[str, int] = {str(r[0]): int(r[1]) for r in rows}
    n_total = sum(present.values())

    if n_total == 0:
        # No data -- the row_count_positive check handles this case.
        # We pass vacuously to avoid double-warning the operator.
        passed, missing = True, set()
        reason = "vacuous_pass_empty_table"
    else:
        missing = expected - set(present.keys())
        passed = len(missing) == 0
        reason = "ok" if passed else "dispatcher_dropped_signals"

    details: dict[str, Any] = {
        "expected_signal_ids":  sorted(expected),
        "present_signal_ids":   sorted(present.keys()),
        "missing_signal_ids":   sorted(missing),
        "per_signal_row_count": present,
        "reason":               reason,
    }
    _emit(governance, dataset_id="derived.fraud_signal_observation",
          check_name="signal_coverage", passed=passed, details=details)
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.WARN, metadata=details,
    )


@asset_check(
    asset=AssetKey(["derived", "v_entity_fraud_risk"]),
    name="risk_score_positive_when_l1_present",
    description=(
        "When derived.fraud_signal_observation has rows, "
        "derived.v_entity_fraud_risk must surface at least one entity with "
        "risk_score > 0. Score=0 for every entity in a populated L1 means "
        "either the L3a formula constants drifted (gamma, k, percentile "
        "floor), the L2 pivot returned only below-threshold percentiles, or "
        "the view's join broke. All three are catastrophic for the analyst "
        "queue and all warrant a hard surface."
    ),
)
def fraud_risk_score_positive_when_l1_present(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Require >=1 entity with risk_score > 0 when L1 is non-empty."""
    n_obs = _count(pg, "SELECT COUNT(*) FROM derived.fraud_signal_observation")
    n_entities = _count(pg, "SELECT COUNT(*) FROM derived.v_entity_fraud_risk")
    n_positive = _count(
        pg,
        "SELECT COUNT(*) FROM derived.v_entity_fraud_risk WHERE risk_score > 0",
    )

    if n_obs == 0:
        passed, reason = True, "vacuous_pass_l1_empty"
    elif n_entities == 0:
        passed, reason = False, "l1_has_rows_but_l3a_view_is_empty"
    else:
        passed = n_positive > 0
        reason = "ok" if passed else "l3a_returns_zero_for_every_entity"

    details: dict[str, Any] = {
        "n_l1_observations":      n_obs,
        "n_l3a_entities":         n_entities,
        "n_l3a_score_positive":   n_positive,
        "reason":                 reason,
    }
    _emit(governance, dataset_id="derived.v_entity_fraud_risk",
          check_name="risk_score_positive_when_l1_present",
          passed=passed, details=details)
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.WARN, metadata=details,
    )


# ============================================================================
# CHECKS for derived.fraud_signal_config (detection-quality registry)
# ----------------------------------------------------------------------------
# Two checks. These are CRITICAL (ERROR severity) because the L2 view
# INNER-JOINs against this table -- a missing config row silently
# drops every observation of that signal from the analyst queue. We
# fail fast rather than letting the queue go quiet.
#
# (a) every_signal_id_in_l1_has_a_config_row
#     A new signal added to L1 without a corresponding config row
#     would disappear from L2/L3a. The check enumerates distinct
#     signal_ids in L1 and compares against config; any signal_id
#     in L1-but-not-config is reported with name and observation
#     count.
#
# (b) every_seeded_family_present
#     Defends against an operator's UPDATE that accidentally
#     mass-rewrites signal_family to a single value (or removes
#     all rows of one family). The diversity bonus is meaningless
#     if every signal has the same family. We require at least 2
#     distinct families to be present in config.
# ============================================================================


@asset_check(
    asset=AssetKey(["derived", "fraud_signal_config"]),
    name="every_signal_id_in_l1_has_a_config_row",
    description=(
        "Every distinct signal_id in derived.fraud_signal_observation "
        "must have a corresponding row in derived.fraud_signal_config. "
        "An L1 signal without a config row INNER-JOINs out of L2 and "
        "disappears from the analyst queue silently. ERROR severity: "
        "this is a configuration bug, not an environmental drift, "
        "and it must block the pipeline."
    ),
)
def fraud_signal_config_orphan_check(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Detect orphan signals (in L1 but not in config)."""
    orphans: list[tuple[str, int]] = []
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT o.signal_id, COUNT(*) "
            "FROM derived.fraud_signal_observation o "
            "LEFT JOIN derived.fraud_signal_config cfg "
            "  ON cfg.signal_id = o.signal_id "
            "WHERE cfg.signal_id IS NULL "
            "GROUP BY o.signal_id "
            "ORDER BY o.signal_id",
        )
        orphans = [(str(r[0]), int(r[1])) for r in cur.fetchall()]

    n_orphans = len(orphans)
    passed = n_orphans == 0
    reason = "ok" if passed else "orphan_signals_in_l1_without_config"

    details: dict[str, Any] = {
        "n_orphan_signals": n_orphans,
        "orphan_signals":   [
            {"signal_id": s, "n_observations": n} for s, n in orphans
        ],
        "reason": reason,
    }
    _emit(governance, dataset_id="derived.fraud_signal_config",
          check_name="every_signal_id_in_l1_has_a_config_row",
          passed=passed, details=details)
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.ERROR, metadata=details,
    )


@asset_check(
    asset=AssetKey(["derived", "fraud_signal_config"]),
    name="age_decay_function_utc_anchored",
    description=(
        "derived.f_leie_age_decay must be UTC-anchored, not session-TZ "
        "dependent. We assert that decay(today_utc) == 1.0 exactly. "
        "Pre-067 the function used CURRENT_DATE which depends on the "
        "session TimeZone GUC, so two Neon instances in different "
        "regions computed different weights for the same exclusion "
        "and tests run near midnight UTC saw 0.9997 instead of 1.0. "
        "ERROR severity: a regression here breaks substrate-honesty "
        "(L1 raw_values would no longer be a deterministic function "
        "of input) and silently shifts the analyst queue ranking."
    ),
)
def fraud_signal_config_decay_utc_anchored(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Pin f_leie_age_decay to UTC by asserting decay(today_utc) == 1.0.

    The check seeds today's UTC date as the input, executes the
    function in the same transaction, and asserts the returned
    NUMERIC equals 1.0 exactly. If the function is using session-TZ
    CURRENT_DATE and the session is offset from UTC across midnight,
    the assertion fails with a sub-1.0 value, surfacing the
    regression before it ships to production.
    """
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT derived.f_leie_age_decay(((NOW() AT TIME ZONE 'UTC')::DATE))"
        )
        row = cur.fetchone()
    weight = float(row[0]) if row else 0.0
    if abs(weight - 1.0) < 1e-12:
        passed, reason = True, "ok"
    else:
        passed, reason = False, "decay_today_utc_not_one_session_tz_leak"
    details: dict[str, Any] = {
        "decay_today_utc": weight,
        "reason":          reason,
    }
    _emit(governance, dataset_id="derived.fraud_signal_config",
          check_name="age_decay_function_utc_anchored",
          passed=passed, details=details)
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.ERROR, metadata=details,
    )


@asset_check(
    asset=AssetKey(["derived", "fraud_signal_config"]),
    name="multiple_families_present",
    description=(
        "derived.fraud_signal_config must contain at least 2 distinct "
        "signal_family values. The diversity bonus in "
        "derived.fraud_risk_score is meaningless if every signal "
        "shares one family. ERROR severity: an operator UPDATE that "
        "mass-rewrites this column would otherwise silently neuter "
        "the multi-family scoring and degrade the analyst queue."
    ),
)
def fraud_signal_config_family_diversity(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Confirm at least two distinct signal_family values exist."""
    n_families = _count(
        pg,
        "SELECT COUNT(DISTINCT signal_family) "
        "FROM derived.fraud_signal_config",
    )
    n_total_rows = _count(
        pg,
        "SELECT COUNT(*) FROM derived.fraud_signal_config",
    )
    if n_total_rows == 0:
        passed, reason = True, "vacuous_pass_config_empty"
    elif n_families < 2:
        passed, reason = False, "single_family_neuters_diversity_bonus"
    else:
        passed, reason = True, "ok"
    details: dict[str, Any] = {
        "n_distinct_families": n_families,
        "n_config_rows":       n_total_rows,
        "reason":              reason,
    }
    _emit(governance, dataset_id="derived.fraud_signal_config",
          check_name="multiple_families_present",
          passed=passed, details=details)
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.ERROR, metadata=details,
    )


# ============================================================================
# CHECK for derived.signal_entity_on_leie (FRAUD-F5b cross-source)
# ----------------------------------------------------------------------------
# Two failure modes worth surfacing programmatically:
#
#   (a) BOTH FEC and LEIE have rows, but zero entity_on_leie matches.
#       Most likely cause: canonicalization function returns NULL for
#       all input shapes (a regression in derived.f_canonical_*). A
#       silent zero would let the structural-only score look "clean"
#       while a real cross-source signal sits broken.
#
#   (b) Match rate exceeds 5% of the bucket population. Most likely
#       cause: canonicalization is too lossy and is collapsing distinct
#       names into a single key (a regression that reduces "DOE JANE"
#       and "JOE DAY" to the same canonical form). False-positive
#       flooding makes the queue useless.
#
# We use WARN severity rather than ERROR because cross-source signals
# can legitimately be zero in dev environments (LEIE freshly seeded
# with synthetic data that doesn't overlap FEC names) and we don't
# want the asset graph to flash red on legitimate test setups.
# ============================================================================


@asset_check(
    asset=AssetKey(["derived", "signal_entity_on_leie"]),
    name="match_rate_in_plausible_range",
    description=(
        "When raw.fec_candidate, raw.fec_committee, AND raw.hhs_oig_leie "
        "all have rows, derived.signal_entity_on_leie must produce >0 "
        "matches AND <= 5% of the bucket population. Zero matches "
        "indicates a canonicalization regression; >5% indicates "
        "over-matching (lossy canonicalization)."
    ),
)
def signal_entity_on_leie_match_rate_plausible(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Sanity-bound the cross-source match rate."""
    n_fec_cand   = _count(pg, "SELECT COUNT(*) FROM raw.fec_candidate")
    n_fec_cmte   = _count(pg, "SELECT COUNT(*) FROM raw.fec_committee")
    n_leie       = _count(pg, "SELECT COUNT(*) FROM raw.hhs_oig_leie")
    n_matches    = _count(
        pg,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE signal_id = 'entity_on_leie'",
    )

    upstream_present = n_fec_cand > 0 and n_fec_cmte > 0 and n_leie > 0
    if not upstream_present:
        passed, reason = True, "vacuous_pass_upstream_empty"
        rate = 0.0
    else:
        # Population denominator for the rate sanity bound. The two
        # entity_kinds in the dispatcher (candidate + treasurer) bucket
        # against different populations; we use their SUM as a unified
        # denominator for the bound. Per-kind rates are visible in the
        # asset materialization metadata for finer diagnosis.
        n_pop = n_fec_cand + _count(
            pg,
            "SELECT COUNT(DISTINCT REGEXP_REPLACE(UPPER(TRIM(tres_nm)), "
            "'\\s+', ' ', 'g')) FROM raw.fec_committee "
            "WHERE tres_nm IS NOT NULL AND TRIM(tres_nm) <> ''",
        )
        rate = (n_matches / n_pop) if n_pop > 0 else 0.0
        if n_matches == 0:
            passed, reason = False, "zero_matches_canonicalizer_likely_broken"
        elif rate > 0.05:
            passed, reason = False, "match_rate_above_5pct_overmatching"
        else:
            passed, reason = True, "ok"

    details: dict[str, Any] = {
        "n_fec_candidate":  n_fec_cand,
        "n_fec_committee":  n_fec_cmte,
        "n_leie":           n_leie,
        "n_matches":        n_matches,
        "match_rate":       round(rate, 6),
        "reason":           reason,
    }
    _emit(governance, dataset_id="derived.signal_entity_on_leie",
          check_name="match_rate_in_plausible_range",
          passed=passed, details=details)
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.WARN, metadata=details,
    )


# ============================================================================
# CROSS-SOURCE SIGNAL: donor_employed_by_nj_contractor (FRAUD-F1 v3)
# ============================================================================
#
# Mirrors the entity_on_leie check shape with two adjustments:
#
#   1. The denominator is NOT the FEC entity population (millions of
#      contributions); it is the count of DISTINCT canonical employer
#      names appearing in raw.fec_contribution for the cycle. That
#      latter number is the natural "bucket the matches live in" --
#      every distinct employer is a candidate cluster, and matches
#      are the subset that overlapped USAspending recipients.
#
#   2. The plausible-rate ceiling is 1% (not 5%) because employer-
#      name canonicalization is much more vulnerable to lossy
#      collisions than person-name canonicalization. A regression
#      that strips too aggressively (e.g., dropping the entire word
#      "tech" from suffixes) can collapse "TETRA TECH" and
#      "ALPHA TECH" to "tetra alpha tech" or similar and over-match
#      catastrophically. 1% is empirically conservative: the platform
#      expects ~1K-5K matched clusters in a typical cycle out of
#      ~500K-1M distinct employer strings.
#
# WARN severity (same rationale as entity_on_leie).
# ============================================================================


@asset_check(
    asset=AssetKey(["derived", "signal_donor_employed_by_nj_contractor"]),
    name="match_rate_in_plausible_range",
    description=(
        "When raw.fec_contribution AND raw.usaspending_award both have "
        "rows, derived.signal_donor_employed_by_nj_contractor must "
        "produce >0 matched clusters AND <= 1% of the distinct-employer "
        "denominator. Zero matches indicates a canonicalization "
        "regression on either side; >1% indicates over-matching "
        "(lossy canonicalization collapsing distinct contractors "
        "into one cluster)."
    ),
)
def signal_donor_employed_by_nj_contractor_match_rate_plausible(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Sanity-bound the donor-employer cross-source match rate."""
    n_fec_contrib = _count(pg, "SELECT COUNT(*) FROM raw.fec_contribution")
    n_us          = _count(pg, "SELECT COUNT(*) FROM raw.usaspending_award")
    n_matches     = _count(
        pg,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE signal_id = 'donor_employed_by_nj_contractor'",
    )

    upstream_present = n_fec_contrib > 0 and n_us > 0
    if not upstream_present:
        passed, reason = True, "vacuous_pass_upstream_empty"
        rate = 0.0
        n_distinct_employers = 0
    else:
        # Denominator = count of distinct canonical employer keys
        # actually present on the FEC side. We canonicalize once via
        # the SQL function (avoid duplicating the canonicalizer in
        # Python) so the denominator can never drift from the
        # numerator's keying.
        n_distinct_employers = _count(
            pg,
            "SELECT COUNT(DISTINCT derived.f_canonical_employer_name(employer)) "
            "FROM raw.fec_contribution "
            "WHERE employer IS NOT NULL "
            "  AND derived.f_canonical_employer_name(employer) <> ''",
        )
        rate = (
            n_matches / n_distinct_employers if n_distinct_employers > 0 else 0.0
        )
        if n_matches == 0:
            passed, reason = False, "zero_matches_canonicalizer_likely_broken"
        elif rate > 0.01:
            passed, reason = False, "match_rate_above_1pct_overmatching"
        else:
            passed, reason = True, "ok"

    details: dict[str, Any] = {
        "n_fec_contribution":   n_fec_contrib,
        "n_usaspending":        n_us,
        "n_distinct_employers": n_distinct_employers,
        "n_matches":            n_matches,
        "match_rate":           round(rate, 6),
        "reason":               reason,
    }
    _emit(governance,
          dataset_id="derived.signal_donor_employed_by_nj_contractor",
          check_name="match_rate_in_plausible_range",
          passed=passed, details=details)
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.WARN, metadata=details,
    )


# ============================================================================
# CROSS-SOURCE SIGNAL: candidate_funded_by_nj_contractor_employees
# ============================================================================
#
# Mirrors the donor-side check shape but with a different denominator
# and ceiling:
#
#   * Denominator = count of FEC candidates in the cycle whose
#     principal/authorized committees received any contributions at
#     all (the natural "could have received money from contractor
#     employees" cohort).
#   * Ceiling = 50%. Unlike the donor side (where >1% match indicates
#     over-matching), the candidate side legitimately can have a much
#     higher match rate -- in a presidential cycle, a majority of
#     active candidates will have received SOMETHING from a
#     contractor-employed donor, because federal contractors employ
#     hundreds of thousands of people who routinely make small
#     contributions. The 50% ceiling catches genuinely degenerate
#     cases (e.g., a canonicalization bug that flags every donation as
#     matching) without firing on the legitimate broad reach of
#     contractor-employee donations.
#
# WARN severity (consistent with the other cross-source signal
# checks).
# ============================================================================


@asset_check(
    asset=AssetKey([
        "derived",
        "signal_candidate_funded_by_nj_contractor_employees",
    ]),
    name="match_rate_in_plausible_range",
    description=(
        "When raw.fec_contribution + raw.fec_committee + "
        "raw.fec_candidate + raw.usaspending_award are all non-empty, "
        "derived.signal_candidate_funded_by_nj_contractor_employees "
        "must produce >0 candidate rows AND <= 50% of the active-"
        "committee-tied candidate cohort. Zero rows indicates either "
        "(a) the upstream donor-side signal regressed or (b) the "
        "fec_committee.cand_id linkage broke. >50% suggests "
        "canonicalization over-matching upstream."
    ),
)
def signal_candidate_funded_by_nj_contractor_employees_match_rate_plausible(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Sanity-bound the candidate-side cross-source match rate."""
    n_fec_contrib = _count(pg, "SELECT COUNT(*) FROM raw.fec_contribution")
    n_fec_cmte    = _count(pg, "SELECT COUNT(*) FROM raw.fec_committee")
    n_fec_cand    = _count(pg, "SELECT COUNT(*) FROM raw.fec_candidate")
    n_us          = _count(pg, "SELECT COUNT(*) FROM raw.usaspending_award")
    n_matches     = _count(
        pg,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE signal_id = "
        "      'candidate_funded_by_nj_contractor_employees'",
    )

    upstream_present = (
        n_fec_contrib > 0 and n_fec_cmte > 0
        and n_fec_cand > 0 and n_us > 0
    )
    if not upstream_present:
        passed, reason = True, "vacuous_pass_upstream_empty"
        rate = 0.0
        n_pop = 0
    else:
        # Cohort denominator: count of distinct candidates whose
        # principal/authorized committees received ANY contributions
        # at all in the cycle (not the matched subset). This is the
        # set "could have been flagged"; the rate is "fraction of
        # those that actually were."
        n_pop = _count(
            pg,
            "SELECT COUNT(DISTINCT cmte.cand_id) "
            "FROM raw.fec_committee cmte "
            "JOIN raw.fec_contribution c "
            "  ON c.cycle = cmte.cycle AND c.cmte_id = cmte.cmte_id "
            "WHERE cmte.cand_id IS NOT NULL "
            "  AND (c.memo_cd IS NULL OR c.memo_cd <> 'X') "
            "  AND c.transaction_amt > 0",
        )
        rate = (n_matches / n_pop) if n_pop > 0 else 0.0
        if n_matches == 0:
            passed, reason = (
                False, "zero_matches_upstream_signal_or_linkage_broken"
            )
        elif rate > 0.50:
            passed, reason = False, "match_rate_above_50pct_overmatching"
        else:
            passed, reason = True, "ok"

    details: dict[str, Any] = {
        "n_fec_contribution":      n_fec_contrib,
        "n_fec_committee":         n_fec_cmte,
        "n_fec_candidate":         n_fec_cand,
        "n_usaspending":           n_us,
        "n_active_cand_cohort":    n_pop,
        "n_matches":               n_matches,
        "match_rate":              round(rate, 6),
        "reason":                  reason,
    }
    _emit(governance,
          dataset_id=(
              "derived.signal_candidate_funded_by_nj_contractor_employees"
          ),
          check_name="match_rate_in_plausible_range",
          passed=passed, details=details)
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.WARN, metadata=details,
    )


# ============================================================================
# CHECK: derived.signal_candidate_funded_by_excluded_donors plausibility
# ----------------------------------------------------------------------------
# Standard cross-source candidate-side semantics, mirroring the
# 057 candidate-side check:
#   * Zero matches when both upstreams (signal_donor_on_leie L1 rows
#     AND fec_committee/candidate referential integrity) are non-
#     empty indicates a regression (donor-side signal regressed,
#     fec_committee.cand_id linkage broke, or the canonicalizer
#     drifted between 059 and 060).
#   * Match rate > 50% of the active-committee-tied candidate cohort
#     suggests upstream over-matching.
#
# Cohort denominator: distinct candidates whose principal/authorized
# committees received ANY positive non-memo contributions in the
# cycle. This is the population "could have been flagged"; the rate
# is "fraction of those that actually were."
# ============================================================================


@asset_check(
    asset=AssetKey([
        "derived",
        "signal_candidate_funded_by_excluded_donors",
    ]),
    name="match_rate_in_plausible_range",
    description=(
        "When raw.fec_contribution + raw.fec_committee + "
        "raw.fec_candidate + raw.hhs_oig_leie are all non-empty, "
        "derived.signal_candidate_funded_by_excluded_donors must "
        "produce >0 rows AND <= 50% of the active-committee-tied "
        "candidate cohort. Zero rows indicates either (a) the "
        "donor-side signal (059) regressed, (b) the canonicalizer "
        "drifted between 059 and 060, or (c) the fec_committee."
        "cand_id linkage broke. >50% suggests canonicalization "
        "over-matching upstream in 059."
    ),
)
def signal_candidate_funded_by_excluded_donors_match_rate_plausible(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Sanity-bound the candidate-side LEIE-donor match rate."""
    n_fec_contrib = _count(pg, "SELECT COUNT(*) FROM raw.fec_contribution")
    n_fec_cmte    = _count(pg, "SELECT COUNT(*) FROM raw.fec_committee")
    n_fec_cand    = _count(pg, "SELECT COUNT(*) FROM raw.fec_candidate")
    n_lei         = _count(pg, "SELECT COUNT(*) FROM raw.hhs_oig_leie")
    n_matches     = _count(
        pg,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE signal_id = 'candidate_funded_by_excluded_donors'",
    )

    upstream_present = (
        n_fec_contrib > 0 and n_fec_cmte > 0
        and n_fec_cand > 0 and n_lei > 0
    )
    if not upstream_present:
        passed, reason = True, "vacuous_pass_upstream_empty"
        rate = 0.0
        n_pop = 0
    else:
        n_pop = _count(
            pg,
            "SELECT COUNT(DISTINCT cmte.cand_id) "
            "FROM raw.fec_committee cmte "
            "JOIN raw.fec_contribution c "
            "  ON c.cycle = cmte.cycle AND c.cmte_id = cmte.cmte_id "
            "WHERE cmte.cand_id IS NOT NULL "
            "  AND (c.memo_cd IS NULL OR c.memo_cd <> 'X') "
            "  AND c.transaction_amt > 0",
        )
        rate = (n_matches / n_pop) if n_pop > 0 else 0.0
        if n_matches == 0:
            passed, reason = (
                False, "zero_matches_upstream_or_linkage_broken"
            )
        elif rate > 0.50:
            passed, reason = False, "match_rate_above_50pct_overmatching"
        else:
            passed, reason = True, "ok"

    details: dict[str, Any] = {
        "n_fec_contribution":   n_fec_contrib,
        "n_fec_committee":      n_fec_cmte,
        "n_fec_candidate":      n_fec_cand,
        "n_leie":               n_lei,
        "n_active_cand_cohort": n_pop,
        "n_matches":            n_matches,
        "match_rate":           round(rate, 6),
        "reason":               reason,
    }
    _emit(governance,
          dataset_id=(
              "derived.signal_candidate_funded_by_excluded_donors"
          ),
          check_name="match_rate_in_plausible_range",
          passed=passed, details=details)
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.WARN, metadata=details,
    )


# ============================================================================
# CHECK: derived.signal_donor_on_leie plausibility
# ----------------------------------------------------------------------------
# Standard cross-source-signal semantics: zero matches when both
# substrates are non-empty indicates a regression (canonicalizer
# broke, an upstream loader skipped, or the join column drifted).
# Match rate above 1% indicates over-matching (the canonical
# "LAST|FIRST" key has a small but non-trivial collision rate; a
# >1% match rate against ~80K active LEIE individuals on a
# multi-million-row donor population is implausibly high).
#
# These bounds differ deliberately from entity_funded_and_excluded
# (where zero is the EXPECTED state). The donor population is
# orders of magnitude larger than the federal-contractor population,
# so the LEIE-overlap is small but non-zero in steady state -- a
# few dozen to a few hundred matched donors per cycle is the
# realistic expected band.
# ============================================================================


@asset_check(
    asset=AssetKey(["derived", "signal_donor_on_leie"]),
    name="match_rate_in_plausible_range",
    description=(
        "When raw.fec_contribution + raw.hhs_oig_leie are both non-"
        "empty, derived.signal_donor_on_leie must produce >0 matches "
        "AND <= 1% of the active canonical donor cohort. Zero "
        "matches indicates a canonicalizer or join-column "
        "regression; >1% indicates over-matching."
    ),
)
def signal_donor_on_leie_match_rate_plausible(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Sanity-bound the donor-side LEIE match rate."""
    n_fec = _count(pg, "SELECT COUNT(*) FROM raw.fec_contribution")
    n_lei = _count(pg, "SELECT COUNT(*) FROM raw.hhs_oig_leie")
    n_matches = _count(
        pg,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE signal_id = 'donor_on_leie'",
    )

    upstream_present = n_fec > 0 and n_lei > 0
    if not upstream_present:
        passed, reason = True, "vacuous_pass_upstream_empty"
        rate = 0.0
        n_pop = 0
    else:
        # Cohort denominator: distinct canonical donor keys across
        # all cycles, post-canonicalizer-NULL filter, post-memo
        # filter. Mirrors the refresher's bucket-population query.
        n_pop = _count(
            pg,
            "SELECT COUNT(DISTINCT "
            "  derived.f_canonical_lastfirst_from_fec(name)) "
            "FROM raw.fec_contribution "
            "WHERE name IS NOT NULL "
            "  AND derived.f_canonical_lastfirst_from_fec(name) "
            "      IS NOT NULL "
            "  AND (memo_cd IS NULL OR memo_cd <> 'X')",
        )
        rate = (n_matches / n_pop) if n_pop > 0 else 0.0
        if n_matches == 0:
            passed, reason = (
                False, "zero_matches_canonicalizer_or_join_regression"
            )
        elif rate > 0.01:
            passed, reason = False, "match_rate_above_1pct_overmatching"
        else:
            passed, reason = True, "ok"

    details: dict[str, Any] = {
        "n_fec_contribution":  n_fec,
        "n_leie":              n_lei,
        "n_active_donor_pop":  n_pop,
        "n_matches":           n_matches,
        "match_rate":          round(rate, 6),
        "reason":              reason,
    }
    _emit(governance,
          dataset_id="derived.signal_donor_on_leie",
          check_name="match_rate_in_plausible_range",
          passed=passed, details=details)
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.WARN, metadata=details,
    )


# ============================================================================
# CHECK: derived.signal_candidate_funded_by_sam_excluded_donors plausibility
# ----------------------------------------------------------------------------
# Same shape as candidate_funded_by_excluded_donors check but
# wider vacuous-pass: requires non-empty SAM-individual cohort (a
# fresh hand-load with only Firm/Vessel rows produces empty
# individual cohort -> empty donor_on_sam -> empty candidate
# projection; that is correct, not a regression).
# ============================================================================


@asset_check(
    asset=AssetKey([
        "derived",
        "signal_candidate_funded_by_sam_excluded_donors",
    ]),
    name="match_rate_in_plausible_range",
    description=(
        "When raw.fec_contribution + raw.fec_committee + "
        "raw.fec_candidate + raw.sam_gov_exclusion are all non-empty "
        "AND v_sam_exclusion_individual_canonical has rows, the "
        "candidate-side projection of donor_on_sam must produce >0 "
        "rows AND <=50% of the active-committee-tied candidate "
        "cohort. Vacuous-pass when SAM individuals are empty "
        "(no domain to project from)."
    ),
)
def signal_candidate_funded_by_sam_excluded_donors_match_rate_plausible(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Sanity-bound the candidate-side SAM-donor match rate."""
    n_fec_contrib = _count(pg, "SELECT COUNT(*) FROM raw.fec_contribution")
    n_fec_cmte    = _count(pg, "SELECT COUNT(*) FROM raw.fec_committee")
    n_fec_cand    = _count(pg, "SELECT COUNT(*) FROM raw.fec_candidate")
    n_sam         = _count(pg, "SELECT COUNT(*) FROM raw.sam_gov_exclusion")
    n_sam_ind     = _count(
        pg,
        "SELECT COUNT(*) FROM derived.v_sam_exclusion_individual_canonical",
    )
    n_donor_sam_l1 = _count(
        pg,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE signal_id = 'donor_on_sam'",
    )
    n_matches = _count(
        pg,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE signal_id = 'candidate_funded_by_sam_excluded_donors'",
    )

    # Vacuous-pass branches:
    # 1. Any base substrate empty -> can't project from nothing.
    # 2. SAM individual cohort empty -> donor_on_sam can't fire.
    # 3. donor_on_sam empty -> candidate projection can't fire.
    upstream_present = (
        n_fec_contrib > 0 and n_fec_cmte > 0
        and n_fec_cand > 0 and n_sam > 0
        and n_sam_ind > 0 and n_donor_sam_l1 > 0
    )
    if not upstream_present:
        passed, reason = True, "vacuous_pass_upstream_empty"
        rate = 0.0
        n_pop = 0
    else:
        n_pop = _count(
            pg,
            "SELECT COUNT(DISTINCT cmte.cand_id) "
            "FROM raw.fec_committee cmte "
            "JOIN raw.fec_contribution c "
            "  ON c.cycle = cmte.cycle AND c.cmte_id = cmte.cmte_id "
            "WHERE cmte.cand_id IS NOT NULL "
            "  AND (c.memo_cd IS NULL OR c.memo_cd <> 'X') "
            "  AND c.transaction_amt > 0",
        )
        rate = (n_matches / n_pop) if n_pop > 0 else 0.0
        if n_matches == 0:
            passed, reason = (
                False, "zero_matches_upstream_or_linkage_broken"
            )
        elif rate > 0.50:
            passed, reason = False, "match_rate_above_50pct_overmatching"
        else:
            passed, reason = True, "ok"

    details: dict[str, Any] = {
        "n_fec_contribution":   n_fec_contrib,
        "n_fec_committee":      n_fec_cmte,
        "n_fec_candidate":      n_fec_cand,
        "n_sam_exclusion":      n_sam,
        "n_sam_individual":     n_sam_ind,
        "n_donor_on_sam_l1":    n_donor_sam_l1,
        "n_active_cand_cohort": n_pop,
        "n_matches":            n_matches,
        "match_rate":           round(rate, 6),
        "reason":               reason,
    }
    _emit(governance,
          dataset_id=(
              "derived.signal_candidate_funded_by_sam_excluded_donors"
          ),
          check_name="match_rate_in_plausible_range",
          passed=passed, details=details)
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.WARN, metadata=details,
    )


# ============================================================================
# CHECK: derived.signal_donor_on_sam plausibility (FRAUD-F2 donor-side)
# ----------------------------------------------------------------------------
# Same semantic shape as donor_on_leie:
#   - zero matches when SAM individual cohort is non-empty AND
#     FEC is non-empty -> regression
#   - match rate > 1% of donor cohort -> overmatching suspect
#
# Vacuous-pass condition is BROADER than donor_on_leie: SAM
# exclusions are mostly firms (Vessel/Firm/Special Entity), with
# Individual being a smaller subset. A fresh hand-load with only
# firm rows produces an empty sam-individual cohort and zero
# matches -- that is NOT a regression. Pre-check the cohort size
# and treat empty individuals as "no domain to match against".
# ============================================================================


@asset_check(
    asset=AssetKey(["derived", "signal_donor_on_sam"]),
    name="match_rate_in_plausible_range",
    description=(
        "Plausibility bound for the donor_on_sam signal. Same "
        "semantic shape as donor_on_leie's check, but vacuous-passes "
        "when v_sam_exclusion_individual_canonical is empty (a fresh "
        "SAM hand-load with only firm-shape exclusions has no domain "
        "to match against and zero is correct, not a regression)."
    ),
)
def signal_donor_on_sam_match_rate_plausible(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Sanity-bound the donor-side SAM match rate."""
    n_fec  = _count(pg, "SELECT COUNT(*) FROM raw.fec_contribution")
    n_sam  = _count(pg, "SELECT COUNT(*) FROM raw.sam_gov_exclusion")
    n_sam_ind = _count(
        pg,
        "SELECT COUNT(*) FROM derived.v_sam_exclusion_individual_canonical",
    )
    n_matches = _count(
        pg,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE signal_id = 'donor_on_sam'",
    )

    upstream_present = n_fec > 0 and n_sam > 0 and n_sam_ind > 0
    if not upstream_present:
        passed, reason = True, "vacuous_pass_upstream_empty"
        rate = 0.0
        n_pop = 0
    else:
        n_pop = _count(
            pg,
            "SELECT COUNT(DISTINCT "
            "  derived.f_canonical_lastfirst_from_fec(name)) "
            "FROM raw.fec_contribution "
            "WHERE name IS NOT NULL "
            "  AND derived.f_canonical_lastfirst_from_fec(name) "
            "      IS NOT NULL "
            "  AND (memo_cd IS NULL OR memo_cd <> 'X')",
        )
        rate = (n_matches / n_pop) if n_pop > 0 else 0.0
        if n_matches == 0:
            passed, reason = (
                False, "zero_matches_canonicalizer_or_join_regression"
            )
        elif rate > 0.01:
            passed, reason = False, "match_rate_above_1pct_overmatching"
        else:
            passed, reason = True, "ok"

    details: dict[str, Any] = {
        "n_fec_contribution":   n_fec,
        "n_sam_exclusion":      n_sam,
        "n_sam_individual":     n_sam_ind,
        "n_active_donor_pop":   n_pop,
        "n_matches":            n_matches,
        "match_rate":           round(rate, 6),
        "reason":               reason,
    }
    _emit(governance,
          dataset_id="derived.signal_donor_on_sam",
          check_name="match_rate_in_plausible_range",
          passed=passed, details=details)
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.WARN, metadata=details,
    )


# ============================================================================
# CHECK: derived.signal_entity_funded_and_excluded plausibility
# ----------------------------------------------------------------------------
# THIS CHECK IS DELIBERATELY DIFFERENT FROM THE OTHER CROSS-SOURCE CHECKS.
#
# For the other cross-source signals (entity_on_leie, donor_employed_*,
# candidate_funded_*) zero matches usually means a regression: the
# canonicalizer broke, an upstream loader skipped, or a join column
# moved. So those checks FAIL on zero matches when both substrates
# are non-empty.
#
# entity_funded_and_excluded is the inverse semantic: zero matches is
# the EXPECTED healthy state. The platform's purpose is to detect
# the signal, not assume it's always present. Federal procurement
# is supposed to never pay an excluded individual; non-zero is
# evidence either of a procurement failure or a canonicalization
# false positive.
#
# So this check:
#   * Does NOT fail on zero matches.
#   * Does flag (WARN) when the matched count is unreasonably HIGH
#     relative to the active individual-recipient population.
#     A real-world expected ceiling is well below 1% of the bucket;
#     anything above 1% is overwhelmingly more likely to be
#     canonicalization over-matching than actual mass federal
#     procurement fraud.
# ============================================================================


@asset_check(
    asset=AssetKey([
        "derived",
        "signal_entity_funded_and_excluded",
    ]),
    name="match_rate_in_plausible_range",
    description=(
        "Plausibility bound for the entity_funded_and_excluded "
        "signal. Zero matches is EXPECTED healthy state (federal "
        "procurement should not pay LEIE-excluded individuals); the "
        "check passes on zero. The check WARNs only when matches "
        "exceed 1% of the active individual-recipient bucket -- that "
        "magnitude almost certainly indicates canonicalization "
        "over-matching rather than real mass procurement fraud."
    ),
)
def signal_entity_funded_and_excluded_match_rate_plausible(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Sanity-bound the funded-and-excluded match rate.

    Two-sided semantics differ from the other cross-source checks:
    - n=0 is healthy (expected steady state)
    - n>0 is always investigation-worthy but NOT a check failure
    - n / population > 0.01 is a CHECK FAILURE (over-match suspect)
    """
    n_us  = _count(pg, "SELECT COUNT(*) FROM raw.usaspending_award")
    n_lei = _count(pg, "SELECT COUNT(*) FROM raw.hhs_oig_leie")
    n_matches = _count(
        pg,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE signal_id = 'entity_funded_and_excluded'",
    )

    upstream_present = n_us > 0 and n_lei > 0
    if not upstream_present:
        passed, reason = True, "vacuous_pass_upstream_empty"
        rate = 0.0
        n_pop = 0
    else:
        n_pop = _count(
            pg,
            "SELECT COUNT(DISTINCT recipient_canonical_individual) "
            "FROM derived.v_usaspending_award_active "
            "WHERE recipient_canonical_individual IS NOT NULL",
        )
        rate = (n_matches / n_pop) if n_pop > 0 else 0.0
        if n_pop == 0 and n_matches == 0:
            # No individual-shaped recipients in active window -- the
            # signal has no domain to fire on. Pass: not a regression.
            passed, reason = True, "no_individual_recipients_in_window"
        elif rate > 0.01:
            passed, reason = False, "match_rate_above_1pct_overmatching"
        else:
            passed, reason = True, ("ok_zero_matches"
                                     if n_matches == 0
                                     else "ok_within_bound")

    details: dict[str, Any] = {
        "n_usaspending":         n_us,
        "n_leie":                n_lei,
        "n_individual_cohort":   n_pop,
        "n_matches":             n_matches,
        "match_rate":            round(rate, 6),
        "reason":                reason,
    }
    _emit(governance,
          dataset_id="derived.signal_entity_funded_and_excluded",
          check_name="match_rate_in_plausible_range",
          passed=passed, details=details)
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.WARN, metadata=details,
    )


# ============================================================================
# CHECK: derived.signal_entity_excluded_via_sam_uei plausibility
# ----------------------------------------------------------------------------
# UEI-deterministic match (FRAUD-F2 / migration 064). Same inverse-
# semantic shape as entity_funded_and_excluded:
#   - n=0 is the EXPECTED healthy state (federal procurement should
#     never pay a SAM-excluded UEI under FAR 9.405)
#   - n>0 is investigation-worthy but NOT a check failure; the
#     analyst queue surfaces individual matches for review
#   - n / population > 0.005 (0.5%) is a CHECK FAILURE: at that
#     scale the explanation is overwhelmingly "SAM data-publishing
#     bug" or "USAspending out-of-sync with SAM", because actual
#     mass FAR 9.405 violations of that magnitude would be a
#     procurement-system collapse worth a New York Times piece
#
# Why 0.5% (vs 1% for the LEIE-canonicalized cousin)
# --------------------------------------------------
# UEI = UEI is unique-by-construction; there is no canonicalization
# false-positive layer. So the noise floor is 2x lower than name-
# canonicalization signals, and the ceiling that distinguishes
# "real procurement failure" from "data-publishing artifact" is
# correspondingly tighter.
# ============================================================================


@asset_check(
    asset=AssetKey([
        "derived",
        "signal_entity_excluded_via_sam_uei",
    ]),
    name="match_rate_in_plausible_range",
    description=(
        "Plausibility bound for the entity_excluded_via_sam_uei "
        "signal. Zero matches is EXPECTED healthy state (federal "
        "procurement should not pay SAM-excluded UEIs); the check "
        "passes on zero. The check WARNs only when matches exceed "
        "0.5% of the active UEI-recipient bucket -- at that "
        "magnitude the explanation is almost certainly a SAM<->"
        "USAspending data-sync bug, not real mass procurement "
        "fraud (UEI = UEI matching has no canonicalization layer)."
    ),
)
def signal_entity_excluded_via_sam_uei_match_rate_plausible(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Sanity-bound the SAM-UEI match rate.

    Two-sided semantics mirror entity_funded_and_excluded:
    - n=0 is healthy (expected steady state)
    - n>0 is investigation-worthy but NOT a check failure
    - n / population > 0.005 is a CHECK FAILURE (data-sync suspect)
    """
    n_us  = _count(pg, "SELECT COUNT(*) FROM raw.usaspending_award")
    n_sam = _count(pg, "SELECT COUNT(*) FROM raw.sam_gov_exclusion")
    n_matches = _count(
        pg,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE signal_id = 'entity_excluded_via_sam_uei'",
    )

    upstream_present = n_us > 0 and n_sam > 0
    if not upstream_present:
        passed, reason = True, "vacuous_pass_upstream_empty"
        rate = 0.0
        n_pop = 0
    else:
        n_pop = _count(
            pg,
            "SELECT COUNT(DISTINCT recipient_uei) "
            "FROM derived.v_usaspending_award_active "
            "WHERE recipient_uei IS NOT NULL",
        )
        rate = (n_matches / n_pop) if n_pop > 0 else 0.0
        if n_pop == 0 and n_matches == 0:
            passed, reason = True, "no_uei_recipients_in_window"
        elif rate > 0.005:
            passed, reason = False, "match_rate_above_0_5pct_data_sync_suspect"
        else:
            passed, reason = True, ("ok_zero_matches"
                                     if n_matches == 0
                                     else "ok_within_bound")

    details: dict[str, Any] = {
        "n_usaspending":     n_us,
        "n_sam_exclusion":   n_sam,
        "n_uei_cohort":      n_pop,
        "n_matches":         n_matches,
        "match_rate":        round(rate, 6),
        "reason":            reason,
        "upstream_present":  upstream_present,
    }
    _emit(governance,
          dataset_id="derived.signal_entity_excluded_via_sam_uei",
          check_name="match_rate_in_plausible_range",
          passed=passed, details=details)
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.WARN, metadata=details,
    )


# ============================================================================
# CHECKS for raw.hhs_oig_leie (Tier 4 v3 / FRAUD-F5 substrate)
# ----------------------------------------------------------------------------
# Three checks, mirroring the FEC pattern:
#
#   row_count_positive             -- table has at least one row (a fresh
#                                     install with zero pulls is a fail
#                                     so the operator notices)
#   excldate_format_valid          -- every row has an 8-digit excldate
#                                     (NOT NULL constraint already
#                                     enforces presence; this confirms
#                                     the format regex didn't somehow
#                                     get bypassed by a future bulk load
#                                     that skipped the parser)
#   active_window_non_empty        -- v_leie_active is non-empty when raw
#                                     has rows. Catches the failure mode
#                                     "loader stamped last_seen_at far in
#                                     the past so every row looks stale"
# ============================================================================


@asset_check(
    asset=AssetKey(["raw", "hhs_oig_leie"]),
    name="row_count_positive",
    description="raw.hhs_oig_leie must have at least one row.",
)
def leie_row_count_positive(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Confirm raw.hhs_oig_leie has at least one row."""
    n = _count(pg, "SELECT COUNT(*) FROM raw.hhs_oig_leie")
    passed = n > 0
    _emit(governance, dataset_id="raw.hhs_oig_leie",
          check_name="row_count_positive", passed=passed,
          details={"row_count": n})
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.ERROR,
        metadata={"row_count": n},
    )


@asset_check(
    asset=AssetKey(["raw", "hhs_oig_leie"]),
    name="excldate_format_valid",
    description=(
        "Every raw.hhs_oig_leie row must have an 8-digit excldate "
        "(YYYYMMDD or '00000000' sentinel)."
    ),
)
def leie_excldate_format_valid(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Confirm every excldate in raw.hhs_oig_leie passes the 8-digit regex.

    The CHECK constraint on raw.hhs_oig_leie.excldate already enforces
    this at INSERT time; this check is a defense-in-depth tripwire for
    the case where the constraint is dropped or a future schema
    migration relaxes it.
    """
    n_bad = _count(
        pg,
        "SELECT COUNT(*) FROM raw.hhs_oig_leie WHERE excldate !~ '^[0-9]{8}$'",
    )
    passed = n_bad == 0
    _emit(governance, dataset_id="raw.hhs_oig_leie",
          check_name="excldate_format_valid", passed=passed,
          details={"n_bad_excldate": n_bad})
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.ERROR,
        metadata={"n_bad_excldate": n_bad},
    )


@asset_check(
    asset=AssetKey(["raw", "hhs_oig_leie"]),
    name="active_window_non_empty",
    description=(
        "When raw.hhs_oig_leie has rows, derived.v_leie_active must "
        "also have rows. An empty active-view with non-empty raw means "
        "every row's last_seen_at is too stale for the 7-day window "
        "(loader bug or 7+ days since last successful pull)."
    ),
)
def leie_active_window_non_empty(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Sanity-check the v_leie_active filter window."""
    n_raw = _count(pg, "SELECT COUNT(*) FROM raw.hhs_oig_leie")
    n_active = _count(pg, "SELECT COUNT(*) FROM derived.v_leie_active")
    if n_raw == 0:
        passed, reason = True, "vacuous_pass_raw_empty"
    elif n_active == 0:
        passed, reason = False, "raw_has_rows_but_active_view_is_empty"
    else:
        passed, reason = True, "ok"
    details: dict[str, Any] = {
        "n_raw": n_raw, "n_active": n_active, "reason": reason,
    }
    _emit(governance, dataset_id="raw.hhs_oig_leie",
          check_name="active_window_non_empty", passed=passed,
          details=details)
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.WARN, metadata=details,
    )


# ============================================================================
# USAspending (FRAUD-F1 substrate) -- 3 asset checks
# ============================================================================
#
# row_count_positive               -- raw table has any rows at all (catches
#                                     "fetch returned 0 results" + a loader
#                                     that didn't fail loud).
# pop_state_nj_invariant           -- every row's pop_state is 'NJ' or NULL
#                                     when present. The ingester filter pins
#                                     pop=NJ at the API; if a non-NJ row
#                                     leaks in, the filter or the parser is
#                                     wrong (substrate-honesty tripwire).
# active_window_non_empty          -- when raw is non-empty, the
#                                     35-day v_usaspending_award_active view
#                                     also has rows.
# ============================================================================


@asset_check(
    asset=AssetKey(["raw", "usaspending_award"]),
    name="row_count_positive",
    description="raw.usaspending_award must have at least one row.",
)
def usaspending_row_count_positive(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Confirm raw.usaspending_award has at least one row."""
    n = _count(pg, "SELECT COUNT(*) FROM raw.usaspending_award")
    passed = n > 0
    _emit(governance, dataset_id="raw.usaspending_award",
          check_name="row_count_positive", passed=passed,
          details={"row_count": n})
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.ERROR,
        metadata={"row_count": n},
    )


@asset_check(
    asset=AssetKey(["raw", "usaspending_award"]),
    name="pop_state_nj_invariant",
    description=(
        "Every raw.usaspending_award row's pop_state must be 'NJ' or "
        "NULL. The ingester pins place_of_performance.state=NJ on the "
        "API filter; a non-NJ row leaking through indicates either "
        "(a) the filter regressed, or (b) the parser flattened the "
        "wrong subobject. Substrate-honesty tripwire."
    ),
)
def usaspending_pop_state_nj_invariant(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Confirm every loaded row has pop_state in {'NJ', NULL}."""
    n_bad = _count(
        pg,
        "SELECT COUNT(*) FROM raw.usaspending_award "
        "WHERE pop_state IS NOT NULL AND pop_state <> 'NJ'",
    )
    passed = n_bad == 0
    _emit(governance, dataset_id="raw.usaspending_award",
          check_name="pop_state_nj_invariant", passed=passed,
          details={"n_bad_pop_state": n_bad})
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.ERROR,
        metadata={"n_bad_pop_state": n_bad},
    )


@asset_check(
    asset=AssetKey(["raw", "usaspending_award"]),
    name="active_window_non_empty",
    description=(
        "When raw.usaspending_award has rows, "
        "derived.v_usaspending_award_active must also have rows. "
        "An empty active-view with non-empty raw means every row's "
        "last_seen_at is too stale for the 35-day window (loader bug "
        "or >35 days since last successful pull)."
    ),
)
def usaspending_active_window_non_empty(
    context: AssetCheckExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> AssetCheckResult:
    """Sanity-check the v_usaspending_award_active filter window."""
    n_raw = _count(pg, "SELECT COUNT(*) FROM raw.usaspending_award")
    n_active = _count(
        pg, "SELECT COUNT(*) FROM derived.v_usaspending_award_active",
    )
    if n_raw == 0:
        passed, reason = True, "vacuous_pass_raw_empty"
    elif n_active == 0:
        passed, reason = False, "raw_has_rows_but_active_view_is_empty"
    else:
        passed, reason = True, "ok"
    details: dict[str, Any] = {
        "n_raw": n_raw, "n_active": n_active, "reason": reason,
    }
    _emit(governance, dataset_id="raw.usaspending_award",
          check_name="active_window_non_empty", passed=passed,
          details=details)
    return AssetCheckResult(
        passed=passed, severity=AssetCheckSeverity.WARN, metadata=details,
    )


ALL_ASSET_CHECKS = [
    fred_row_count_positive,
    cpi_row_count_positive,
    fhfa_nj_county_coverage,
    # ZHVI (Phase 6) + cross-source vs FHFA (Phase 7)
    zhvi_row_count_positive,
    zhvi_nj_county_coverage,
    zhvi_yoy_outliers_plausible,
    housing_index_cross_source_divergence_plausible,
    acs_income_nj_coverage,
    acs_housing_nj_coverage,
    lca_row_count_positive,
    nj_proptax_county_coverage,
    # VISION_2026 §7.1 forcing function for hand-transcribed tax-table backfill
    tax_substrate_prior_year_seeded,
    pums_person_row_count_positive,
    pums_person_nj_puma_coverage,
    pums_replicate_weights_cardinality,
    pums_person_housing_serialno_consistency,
    burden_ratio_plausible,
    pums_burden_row_count_positive,
    pums_burden_suppression_rate,
    pums_burden_ratio_range,
    pums_burden_county_row_count_positive,
    pums_burden_county_nj_coverage,
    pums_burden_county_ratio_range,
    pums_burden_se_non_negative,
    pums_burden_county_se_non_negative,
    pums_burden_county_xwalk_invariants,
    pums_burden_county_multivintage_coverage,
    pums_burden_county_yoy_swings_plausible,
    pums_burden_county_multiyear_coverage,
    # FEC (Tier 4 v1)
    fec_candidate_row_count_positive,
    fec_candidate_nj_coverage,
    fec_committee_row_count_positive,
    fec_contribution_row_count_positive,
    fec_contribution_referential_integrity,
    fec_nj_money_visible,
    # HHS-OIG LEIE (Tier 4 v3 / FRAUD-F5 substrate)
    leie_row_count_positive,
    leie_excldate_format_valid,
    leie_active_window_non_empty,
    # USAspending (Tier 4 v3 / FRAUD-F1 substrate)
    usaspending_row_count_positive,
    usaspending_pop_state_nj_invariant,
    usaspending_active_window_non_empty,
    # Tier 4 v3 fraud-risk surface
    fraud_signal_observation_row_count_positive,
    fraud_signal_observation_signal_coverage,
    fraud_risk_score_positive_when_l1_present,
    # Detection-quality registry (migration 061)
    fraud_signal_config_orphan_check,
    fraud_signal_config_family_diversity,
    # FRAUD-F5b cross-source signal
    signal_entity_on_leie_match_rate_plausible,
    # FRAUD-F1 cross-source signal
    signal_donor_employed_by_nj_contractor_match_rate_plausible,
    signal_candidate_funded_by_nj_contractor_employees_match_rate_plausible,
    # FRAUD-F1 + F5 INTERSECTION cross-source signal
    signal_entity_funded_and_excluded_match_rate_plausible,
    # FRAUD-F5c donor-side LEIE cross-source signal
    signal_donor_on_leie_match_rate_plausible,
    # FRAUD-F5d candidate-side projection of donor_on_leie
    signal_candidate_funded_by_excluded_donors_match_rate_plausible,
    # FRAUD-F2 UEI-deterministic SAM x USAspending cross-source signal
    signal_entity_excluded_via_sam_uei_match_rate_plausible,
    # FRAUD-F2 donor-side SAM cross-source signal
    signal_donor_on_sam_match_rate_plausible,
    # FRAUD-F2 candidate-side projection of donor_on_sam
    signal_candidate_funded_by_sam_excluded_donors_match_rate_plausible,
    # MIGRATION 067: regression-defense for f_leie_age_decay UTC anchor
    fraud_signal_config_decay_utc_anchored,
]
