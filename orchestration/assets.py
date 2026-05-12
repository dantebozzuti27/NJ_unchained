"""Software-defined assets for the platform.

Each ``raw.*`` table the platform tracks is a first-class Dagster
:class:`AssetsDefinition`. Each ``derived.*`` view is also an asset
(its compute is a SELECT-and-record-shape query) so that the asset
graph mirrors data lineage end-to-end.

Asset key convention
--------------------
We use the schema-qualified table/view name as the asset key:

    AssetKey(["raw", "fred_observation"])
    AssetKey(["derived", "f_acs_mhi_real"])

This makes the Dagster asset graph isomorphic to the database schema,
so a developer who knows the data model immediately knows the asset
graph.

Asset coverage (current set)
----------------------------
RAW (7):
  raw.fred_observation             -- weekly  (FRED rate series)
  raw.cpi_u                        -- monthly (BLS CPI-U catalog)
  raw.fhfa_hpi_county              -- quarterly (FHFA HPI)
  raw.acs_median_household_income  -- annual  (ACS B19013)
  raw.acs_housing                  -- annual  (ACS B25xxx)
  raw.lca_disclosure               -- quarterly (DOL OFLC LCA)
  raw.nj_property_tax_county       -- annual  (NJ DCA)

DERIVED (4):
  derived.fred_annual              -- annual rate averages
  derived.f_acs_mhi_real           -- CPI-deflated median income
  derived.fhfa_hpi_indexed_2000    -- HPI re-based to 2000=100
  derived.housing_burden_ratio     -- per-tenure + blended burden ratio

DEFERRED:
  ref.zip_county (HUD)             -- operator-staged; sensor-driven,
                                     not schedule-driven.

Adding a new asset
------------------
1. Define the @asset function below.
2. Append it to ALL_ASSETS.
3. Add a release_calendar entry (db/seeds/003_release_calendar.sql).
4. Add a schedule in schedules.py (raw assets) or ride a parent's
   AutoMaterializePolicy (derived assets).
5. Add a row-count AssetCheck in asset_checks.py.
"""

import datetime as dt
import logging
from pathlib import Path
from typing import Any, Final

from dagster import (
    AssetDep,
    AssetExecutionContext,
    AssetKey,
    AutomationCondition,
    FreshnessPolicy,
    MaterializeResult,
    MetadataValue,
    asset,
)

from derived import pums_burden, pums_burden_county
from ingestion import (
    bls_cpi,
    census_acs_housing,
    census_acs_income,
    census_acs_pums,
    dol_oflc_lca,
    fec,
    fhfa_hpi,
    fred_mortgage_rates,
    hhs_oig_leie,
    nj_dca_property_tax,
    usaspending,
    zillow_zhvi,
)
from ingestion._base import IngestError
from ingestion.census_acs_income import VintageNotPublishedError
from ingestion.census_acs_pums import (
    VintageNotPublishedError as PUMSVintageNotPublishedError,
)
from orchestration.resources import GovernanceWriter, HealthSignal, PgResource

log = logging.getLogger(__name__)


# ============================================================================
# Freshness policies
# ============================================================================
# A FreshnessPolicy declares: "this asset's most recent materialization
# must be no older than X." Lag budgets are publication cadence + a buffer.
# Modern Dagster API: FreshnessPolicy.time_window(fail_window, warn_window).
# ============================================================================

FRED_FRESHNESS = FreshnessPolicy.time_window(
    fail_window=dt.timedelta(days=10),  warn_window=dt.timedelta(days=7),
)
BLS_FRESHNESS = FreshnessPolicy.time_window(
    fail_window=dt.timedelta(days=35),  warn_window=dt.timedelta(days=32),
)
FHFA_FRESHNESS = FreshnessPolicy.time_window(
    fail_window=dt.timedelta(days=100), warn_window=dt.timedelta(days=92),
)
# Zillow ZHVI: monthly cadence; the public CSV's HTTP Last-Modified
# header has historically slipped by 2-7 days past the calendar
# month-end (e.g. 2026-04-16 release for the 2026-03-31 observation
# month). 45 / 35 day envelope absorbs that publication slip without
# triggering spurious warns; a 60-day fail window lights up if Zillow
# misses two consecutive monthly releases.
ZHVI_FRESHNESS = FreshnessPolicy.time_window(
    fail_window=dt.timedelta(days=60),  warn_window=dt.timedelta(days=45),
)
ACS_FRESHNESS = FreshnessPolicy.time_window(
    fail_window=dt.timedelta(days=420), warn_window=dt.timedelta(days=380),
)
LCA_FRESHNESS = FreshnessPolicy.time_window(
    fail_window=dt.timedelta(days=120), warn_window=dt.timedelta(days=100),
)
DCA_FRESHNESS = FreshnessPolicy.time_window(
    fail_window=dt.timedelta(days=420), warn_window=dt.timedelta(days=380),
)
# HHS-OIG LEIE: full database CSV refreshed monthly by the 10th. We use
# a 25-day fail / 20-day warn budget so a normal month boundary plus
# HHS's typical few-day publication slip lands inside the warn window
# without triggering a fail. A second consecutive missed month flips
# fail and the operator should investigate.
LEIE_FRESHNESS = FreshnessPolicy.time_window(
    fail_window=dt.timedelta(days=25), warn_window=dt.timedelta(days=20),
)
# USAspending: continuous upstream feed; platform pulls monthly. We
# allow 35 days for fail with 32 days for warn. The wider envelope vs
# LEIE absorbs the API rate-limit duty cycle of a full FY pull (~17
# minutes per FY at 1 req/sec) being interleaved across multiple
# Dagster ticks if the operator backfills several FYs at once.
USASPENDING_FRESHNESS = FreshnessPolicy.time_window(
    fail_window=dt.timedelta(days=35), warn_window=dt.timedelta(days=32),
)
# PUMS 1-year is published ~Oct of year+1 (e.g., 2022 PUMS released
# Oct 2023). We allow 13 months without a refresh before failing
# freshness, with a warn at 12 months. This is wider than the ACS
# tabular freshness because PUMS has historically lagged a few weeks
# behind the tabular release.
PUMS_FRESHNESS = FreshnessPolicy.time_window(
    fail_window=dt.timedelta(days=400), warn_window=dt.timedelta(days=370),
)
# FEC bulk: bi-weekly during an active cycle, monthly off-cycle. Use a
# 21-day fail / 16-day warn budget which absorbs both cadences without
# spurious off-cycle warnings.
FEC_FRESHNESS = FreshnessPolicy.time_window(
    fail_window=dt.timedelta(days=21),  warn_window=dt.timedelta(days=16),
)


# ============================================================================
# Default backfill windows
# ============================================================================
# How far back to fetch on each materialization. UPSERT semantics make
# overlap with prior runs free, so favor wider windows over narrower
# ones to absorb publication delays / revisions.
# ============================================================================

ACS_BACKFILL_YEARS: Final[int] = 3       # ACS publishes 1-year vintages annually
LCA_BACKFILL_QUARTERS: Final[int] = 6    # last 1.5 fiscal years
DCA_BACKFILL_YEARS: Final[int] = 3       # NJ DCA publishes annually in January

# PUMS files are large (~50-100 MB compressed per state-year). We
# refresh only the most-recently-published vintage on each tick and
# rely on idempotent DELETE+COPY so re-runs are safe. Older vintages
# do not change once published.
PUMS_BACKFILL_YEARS: Final[int] = 1


# ============================================================================
# Automation condition for derived assets
# ============================================================================
#
# Derived assets refresh whenever any upstream parent materializes.
# The Dagster daemon evaluates this condition on each tick (~30s default)
# and queues a run when the condition is satisfied.
#
# Why AutomationCondition.eager() over a polled cron:
#   * Latency: ~30s after parent materialization, instead of up to 6h.
#   * Cost: one materialization per parent refresh, not 144 per day.
#   * Operationally truthful: the Dagster UI shows real lineage flow,
#     not a cron that hides "is the child stale because of an upstream
#     issue, or because the cron hasn't fired yet?"
#
# AutomationCondition.eager() is the modern replacement for the now-
# deprecated AutoMaterializePolicy.eager(); it composes from the same
# primitives but is more explicit and forward-compatible.
# ============================================================================

DERIVED_AUTOMATION = AutomationCondition.eager()


# ============================================================================
# Tier 4 v3: canonical structural-signal IDs
# ============================================================================
#
# Eight per-signal refresher functions in migration 051 each emit rows into
# derived.fraud_signal_observation tagged with one of these signal_ids. The
# dispatcher (derived.refresh_all_fraud_signal_observations) calls all eight.
#
# Listing them here (rather than re-deriving them from the table) is the
# truthful answer to the question "what does the dispatcher SAY it produces?"
# An asset check then compares this expected set against the DISTINCT signal_id
# set actually present in derived.fraud_signal_observation. If a refresher
# silently drops -- e.g., because its underlying derived.fec_* view returns
# zero rows after a schema drift -- the missing-signal check is the only thing
# that surfaces it. Schema CHECKs constrain what's there; only an asset check
# can constrain what's missing.
# ============================================================================

FRAUD_STRUCTURAL_SIGNAL_IDS: Final[tuple[str, ...]] = (
    "candidate_broken_pcc",
    "candidate_multiple_pccs",
    "candidate_namesakes",
    "candidate_no_pcc",
    "committee_address_clusters",
    "committee_name_collisions",
    "treasurer_concentration",
    "treasurer_is_candidate",
)


# ============================================================================
# Helpers
# ============================================================================


def _emit_materialized(
    governance: GovernanceWriter,
    *,
    dataset_id: str,
    rows_upserted: int,
    extra: dict[str, object] | None = None,
) -> None:
    """Emit a standard 'materialized' signal payload to governance."""
    details: dict[str, object] = {"rows_upserted": rows_upserted}
    if extra:
        details.update(extra)
    governance.emit(HealthSignal(
        dataset_id=dataset_id,
        signal_name="materialized",
        severity="info",
        details=details,
    ))


def _rows_to_polars(
    rows: list[tuple[Any, ...]] | list[Any],
    columns: list[str],
) -> Any:
    """Build a Polars DataFrame from psycopg row tuples robustly.

    Polars' default schema inference only reads ``infer_schema_length``
    rows and infers ``Null`` from any column whose first row is None.
    Real PUMS data has many nullable INTEGER columns that begin with
    NULL (e.g., wagp for non-earners), and Polars then errors on later
    non-null values.

    The fix: build column-major (dict-of-lists) and pass that to
    ``pl.DataFrame``, which scans the whole column at construction
    time. For ~500K rows this is still fast (<200ms) because we are
    not coercing types -- Polars infers them once per column.
    """
    import polars as pl
    if not rows:
        return pl.DataFrame(schema=dict.fromkeys(columns, pl.Null))
    columnar = {col: [r[i] for r in rows] for i, col in enumerate(columns)}
    return pl.DataFrame(columnar)


def _current_dol_fiscal_quarter(today: dt.date | None = None) -> tuple[int, int]:
    """Return the (fiscal_year, fiscal_quarter) the date falls in.

    DOL fiscal year runs October -> September. FY2024 = Oct 2023 - Sep 2024.
    Q1=Oct-Dec, Q2=Jan-Mar, Q3=Apr-Jun, Q4=Jul-Sep.
    """
    today = today or dt.date.today()
    fy = today.year + 1 if today.month >= 10 else today.year
    if today.month in (10, 11, 12):
        fq = 1
    elif today.month in (1, 2, 3):
        fq = 2
    elif today.month in (4, 5, 6):
        fq = 3
    else:
        fq = 4
    return fy, fq


def _previous_fiscal_quarter(fy: int, fq: int) -> tuple[int, int]:
    """One fiscal quarter before (fy, fq). Wraps Q1 -> previous-year Q4."""
    if fq == 1:
        return fy - 1, 4
    return fy, fq - 1


# ============================================================================
# RAW ASSET 1: raw.fred_observation
# ============================================================================


@asset(
    key=AssetKey(["raw", "fred_observation"]),
    description=(
        "FRED rate-series observations: MORTGAGE30US (weekly), DGS10 "
        "(daily), FEDFUNDS (monthly). One row per (series, date)."
    ),
    group_name="rates",
    freshness_policy=FRED_FRESHNESS,
    compute_kind="python",
)
def raw_fred_observation(
    context: AssetExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> MaterializeResult:
    """Re-fetch the rolling-90-day window for each canonical FRED series.

    UPSERT against ``raw.fred_observation``. Per-series row counts and
    fetch window land in governance.dataset_health.
    """
    end = dt.date.today()
    start = end - dt.timedelta(days=90)

    total = 0
    per_series: dict[str, int] = {}
    with pg.connect() as conn:
        for sid in fred_mortgage_rates.CANONICAL_SERIES:
            result = fred_mortgage_rates.fetch_fred_series(sid, start=start, end=end)
            staged = fred_mortgage_rates.stage_dataframe(result)
            n = fred_mortgage_rates.load_to_postgres(staged, conn)
            per_series[sid] = n
            total += n
            context.log.info("FRED %s: UPSERTed %d rows", sid, n)

    _emit_materialized(governance, dataset_id="raw.fred_observation",
                       rows_upserted=total,
                       extra={"per_series": per_series,
                              "fetch_start": start.isoformat(),
                              "fetch_end":   end.isoformat()})
    return MaterializeResult(metadata={
        "rows_upserted": MetadataValue.int(total),
        "fetch_window":  MetadataValue.text(f"{start.isoformat()}..{end.isoformat()}"),
        "per_series":    MetadataValue.json(per_series),
    })


# ============================================================================
# RAW ASSET 2: raw.cpi_u
# ============================================================================


@asset(
    key=AssetKey(["raw", "cpi_u"]),
    description=(
        "BLS CPI-U monthly observations across the canonical series "
        "catalog (headline, core, shelter, rent, etc.)."
    ),
    group_name="deflators",
    freshness_policy=BLS_FRESHNESS,
    compute_kind="python",
)
def raw_cpi_u(
    context: AssetExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> MaterializeResult:
    """Refetch the trailing 3 calendar years for each canonical series.

    BLS publishes M13 (annual averages) at year end; the 3-year window
    catches both new observations and revisions to prior years.
    """
    today = dt.date.today()
    start_year = today.year - 3
    end_year = today.year

    total = 0
    with pg.connect() as conn:
        for sid in bls_cpi.CANONICAL_SERIES:
            result = bls_cpi.fetch_cpi_series(sid, start_year=start_year, end_year=end_year)
            staged = bls_cpi.stage_dataframe(result)
            n = bls_cpi.load_to_postgres(staged, conn)
            total += n
            context.log.info("BLS %s: UPSERTed %d rows", sid, n)

    _emit_materialized(governance, dataset_id="raw.cpi_u",
                       rows_upserted=total,
                       extra={"year_range": [start_year, end_year],
                              "n_series":   len(bls_cpi.CANONICAL_SERIES)})
    return MaterializeResult(metadata={
        "rows_upserted":  MetadataValue.int(total),
        "year_range":     MetadataValue.text(f"{start_year}..{end_year}"),
        "n_series":       MetadataValue.int(len(bls_cpi.CANONICAL_SERIES)),
    })


# ============================================================================
# RAW ASSET 3: raw.fhfa_hpi_county
# ============================================================================


@asset(
    key=AssetKey(["raw", "fhfa_hpi_county"]),
    description=(
        "FHFA House Price Index (county-level annual all-transactions). "
        "Repeat-sales index controlling for compositional change in "
        "the housing stock."
    ),
    group_name="house_prices",
    freshness_policy=FHFA_FRESHNESS,
    compute_kind="python",
)
def raw_fhfa_hpi_county(
    context: AssetExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> MaterializeResult:
    """Re-download the FHFA workbook and refresh raw.fhfa_hpi_county.

    The workbook is small (~5 MiB) and updated quarterly with the full
    history; cheaper to re-download than to track incremental changes.
    """
    workbook = fhfa_hpi.fetch_fhfa_county_workbook(
        dest_dir=Path("data/manual/fhfa_hpi"), overwrite=True,
    )
    parsed = fhfa_hpi.parse_fhfa_county_workbook(workbook)
    staged = fhfa_hpi.stage_dataframe(parsed)
    with pg.connect() as conn:
        n = fhfa_hpi.load_to_postgres(staged, conn)

    _emit_materialized(governance, dataset_id="raw.fhfa_hpi_county",
                       rows_upserted=n,
                       extra={"source_vintage": parsed.source_vintage,
                              "source_sha256":  parsed.source_sha256})
    return MaterializeResult(metadata={
        "rows_upserted":  MetadataValue.int(n),
        "source_vintage": MetadataValue.text(parsed.source_vintage),
        "source_sha256":  MetadataValue.text(parsed.source_sha256),
    })


# ============================================================================
# RAW ASSET 3b: raw.zillow_zhvi_county (Phase 6)
# ============================================================================


@asset(
    key=AssetKey(["raw", "zillow_zhvi_county"]),
    description=(
        "Zillow ZHVI county-level monthly typical home value (mid-tier "
        "single-family + condo, smoothed, seasonally adjusted). Second "
        "independent housing index alongside FHFA HPI; substrate for "
        "spec §8.1 cross-source validation via "
        "derived.f_housing_index_cross_source(base_year)."
    ),
    group_name="house_prices",
    freshness_policy=ZHVI_FRESHNESS,
    compute_kind="python",
)
def raw_zillow_zhvi_county(
    context: AssetExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> MaterializeResult:
    """Re-download Zillow's county ZHVI CSV and refresh raw.zillow_zhvi_county.

    The CSV is ~13 MiB and updated monthly. Zillow republishes the full
    history every release; cheaper to re-download than to track
    incremental changes.
    """
    fetched = zillow_zhvi.fetch_zhvi_county_csv(
        dest_dir=Path("data/manual/zillow_zhvi"), overwrite=True,
    )
    parsed = zillow_zhvi.parse_zhvi_county_csv(
        fetched.path,
        sha256=fetched.sha256,
        last_modified=fetched.last_modified,
    )
    staged = zillow_zhvi.stage_dataframe(parsed)
    with pg.connect() as conn:
        n = zillow_zhvi.load_to_postgres(staged, conn)
        conn.commit()

    _emit_materialized(governance, dataset_id="raw.zillow_zhvi_county",
                       rows_upserted=n,
                       extra={"source_vintage":      parsed.source_vintage,
                              "source_sha256":       parsed.source_sha256,
                              "source_modified_at":  (
                                  parsed.source_modified_at.isoformat()
                                  if parsed.source_modified_at else None
                              ),
                              "n_counties":          parsed.n_counties,
                              "n_observations":      parsed.n_observations})
    return MaterializeResult(metadata={
        "rows_upserted":     MetadataValue.int(n),
        "source_vintage":    MetadataValue.text(parsed.source_vintage),
        "source_sha256":     MetadataValue.text(parsed.source_sha256),
        "n_counties":        MetadataValue.int(parsed.n_counties),
        "n_observations":    MetadataValue.int(parsed.n_observations),
    })


# ============================================================================
# RAW ASSET 4: raw.acs_median_household_income
# ============================================================================


@asset(
    key=AssetKey(["raw", "acs_median_household_income"]),
    description=(
        "Census ACS B19013 median household income by NJ county, "
        "ACS 5-year estimates. Trailing window covers prior vintages "
        "(UPSERT idempotent against revisions)."
    ),
    group_name="acs",
    freshness_policy=ACS_FRESHNESS,
    compute_kind="python",
)
def raw_acs_median_household_income(
    context: AssetExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> MaterializeResult:
    """Refetch the trailing N ACS 5-year vintages for NJ counties.

    The Census API endpoint for vintage Y is published the December
    following the close of the data window (e.g. 2022 5-yr was
    published Dec 2023). We pull through the most-recently-available
    vintage; vintages still pending publication 404 cleanly.
    """
    today = dt.date.today()
    candidate_years = list(range(today.year - ACS_BACKFILL_YEARS, today.year + 1))
    per_year: dict[str, int] = {}
    failures: list[dict[str, object]] = []
    total = 0

    with pg.connect() as conn:
        for year in candidate_years:
            try:
                result = census_acs_income.fetch_acs_b19013_year(
                    year=year, product="acs5", state_fips="34",
                )
            except VintageNotPublishedError:
                context.log.info("ACS B19013 acs5 %d: not yet published", year)
                continue
            except Exception as exc:
                context.log.warning("ACS B19013 acs5 %d: fetch failed: %s", year, exc)
                failures.append({"year": year, "error": str(exc)})
                continue
            staged = census_acs_income.stage_dataframe(result)
            n = census_acs_income.load_to_postgres(staged, conn)
            per_year[str(year)] = n
            total += n
            context.log.info("ACS B19013 acs5 %d: UPSERTed %d rows", year, n)

    if total == 0 and not per_year:
        raise RuntimeError(
            f"ACS B19013 produced 0 rows across {candidate_years}; "
            f"failures={failures}"
        )

    if failures:
        governance.emit(HealthSignal(
            dataset_id="raw.acs_median_household_income",
            signal_name="partial_fetch_failure",
            severity="warn",
            details={"failures": failures, "succeeded_years": list(per_year.keys())},
        ))

    _emit_materialized(governance, dataset_id="raw.acs_median_household_income",
                       rows_upserted=total,
                       extra={"per_year": per_year, "n_failures": len(failures)})
    return MaterializeResult(metadata={
        "rows_upserted": MetadataValue.int(total),
        "per_year":      MetadataValue.json(per_year),
        "n_failures":    MetadataValue.int(len(failures)),
    })


# ============================================================================
# RAW ASSET 5: raw.acs_housing
# ============================================================================


@asset(
    key=AssetKey(["raw", "acs_housing"]),
    description=(
        "Census ACS B25xxx housing variables (median gross rent, "
        "median owner cost, median home value, occupancy by tenure) "
        "by NJ county, ACS 5-year estimates."
    ),
    group_name="acs",
    freshness_policy=ACS_FRESHNESS,
    compute_kind="python",
)
def raw_acs_housing(
    context: AssetExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> MaterializeResult:
    """Refetch the trailing N ACS 5-year housing vintages for NJ counties."""
    today = dt.date.today()
    candidate_years = list(range(today.year - ACS_BACKFILL_YEARS, today.year + 1))
    per_year: dict[str, int] = {}
    failures: list[dict[str, object]] = []
    total = 0

    with pg.connect() as conn:
        for year in candidate_years:
            try:
                result = census_acs_housing.fetch_acs_housing_year(
                    year=year, product="acs5", state_fips="34",
                    variable_ids=census_acs_housing.CANONICAL_HOUSING_VARS,
                )
            except VintageNotPublishedError:
                context.log.info("ACS housing acs5 %d: not yet published", year)
                continue
            except Exception as exc:
                context.log.warning("ACS housing acs5 %d: fetch failed: %s", year, exc)
                failures.append({"year": year, "error": str(exc)})
                continue
            staged = census_acs_housing.stage_dataframe(result)
            n = census_acs_housing.load_to_postgres(staged, conn)
            per_year[str(year)] = n
            total += n
            context.log.info("ACS housing acs5 %d: UPSERTed %d rows", year, n)

    if total == 0 and not per_year:
        raise RuntimeError(
            f"ACS housing produced 0 rows across {candidate_years}; "
            f"failures={failures}"
        )

    if failures:
        governance.emit(HealthSignal(
            dataset_id="raw.acs_housing",
            signal_name="partial_fetch_failure",
            severity="warn",
            details={"failures": failures, "succeeded_years": list(per_year.keys())},
        ))

    _emit_materialized(governance, dataset_id="raw.acs_housing",
                       rows_upserted=total,
                       extra={"per_year": per_year, "n_failures": len(failures)})
    return MaterializeResult(metadata={
        "rows_upserted": MetadataValue.int(total),
        "per_year":      MetadataValue.json(per_year),
        "n_failures":    MetadataValue.int(len(failures)),
    })


# ============================================================================
# RAW ASSET 6: raw.lca_disclosure
# ============================================================================


@asset(
    key=AssetKey(["raw", "lca_disclosure"]),
    description=(
        "DOL OFLC Labor Condition Application disclosures, FY2018+. "
        "One row per LCA case, fields canonicalized across vintage "
        "schema variants. Trailing window covers the most recent "
        "fiscal quarters (publication ~3 months after quarter end)."
    ),
    group_name="labor",
    freshness_policy=LCA_FRESHNESS,
    compute_kind="python",
)
def raw_lca_disclosure(
    context: AssetExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> MaterializeResult:
    """Walk back from the current fiscal quarter; UPSERT each available file.

    DOL publishes ~3 months after a fiscal quarter ends. The current
    fiscal quarter and the immediately-previous one will frequently
    404; that is expected and silenced. We continue back for
    LCA_BACKFILL_QUARTERS to absorb the publication delay window.
    """
    out_dir = Path("data/manual/dol_oflc_lca")
    fy, fq = _current_dol_fiscal_quarter()

    per_period: dict[str, int] = {}
    failures: list[dict[str, object]] = []
    total = 0

    with pg.connect() as conn:
        for _ in range(LCA_BACKFILL_QUARTERS):
            label = f"FY{fy}Q{fq}"
            try:
                path = dol_oflc_lca.fetch_dol_lca_file(
                    fiscal_year=fy, fiscal_quarter=fq,
                    out_dir=out_dir, overwrite=False,
                )
            except IngestError as exc:
                context.log.info("LCA %s: not available (%s)", label, exc)
                fy, fq = _previous_fiscal_quarter(fy, fq)
                continue
            except Exception as exc:
                context.log.warning("LCA %s: fetch failed: %s", label, exc)
                failures.append({"period": label, "error": str(exc)})
                fy, fq = _previous_fiscal_quarter(fy, fq)
                continue
            try:
                parse = dol_oflc_lca.parse_lca_file(path)
                staged = dol_oflc_lca.stage_dataframe(parse)
                n = dol_oflc_lca.load_to_postgres(staged, conn)
            except Exception as exc:
                context.log.warning("LCA %s: parse/load failed: %s", label, exc)
                failures.append({"period": label, "error": str(exc)})
                fy, fq = _previous_fiscal_quarter(fy, fq)
                continue
            per_period[label] = n
            total += n
            context.log.info("LCA %s: UPSERTed %d rows", label, n)
            fy, fq = _previous_fiscal_quarter(fy, fq)

    if total == 0:
        raise RuntimeError(
            f"LCA produced 0 rows across {LCA_BACKFILL_QUARTERS} quarters; "
            f"failures={failures}"
        )

    if failures:
        governance.emit(HealthSignal(
            dataset_id="raw.lca_disclosure",
            signal_name="partial_fetch_failure",
            severity="warn",
            details={"failures": failures, "succeeded_periods": list(per_period.keys())},
        ))

    _emit_materialized(governance, dataset_id="raw.lca_disclosure",
                       rows_upserted=total,
                       extra={"per_period": per_period,
                              "n_failures": len(failures)})
    return MaterializeResult(metadata={
        "rows_upserted": MetadataValue.int(total),
        "per_period":    MetadataValue.json(per_period),
        "n_failures":    MetadataValue.int(len(failures)),
    })


# ============================================================================
# RAW ASSET 7: raw.nj_property_tax_county
# ============================================================================


@asset(
    key=AssetKey(["raw", "nj_property_tax_county"]),
    description=(
        "NJ Department of Community Affairs County Tax Summary. "
        "21 counties, 2016+ (older vintages use a different sheet "
        "structure and are intentionally out of scope)."
    ),
    group_name="taxes",
    freshness_policy=DCA_FRESHNESS,
    compute_kind="python",
)
def raw_nj_property_tax_county(
    context: AssetExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> MaterializeResult:
    """Refetch the trailing 3 NJ DCA workbooks (annual cadence)."""
    today = dt.date.today()
    candidate_years = list(range(
        max(today.year - DCA_BACKFILL_YEARS, nj_dca_property_tax.DCA_EARLIEST_YEAR),
        today.year,
    ))
    out_dir = Path("data/manual/nj_dca")
    per_year: dict[str, int] = {}
    failures: list[dict[str, object]] = []
    total = 0

    with pg.connect() as conn:
        for year in candidate_years:
            try:
                path = nj_dca_property_tax.fetch_dca_workbook(
                    year=year, dest_dir=out_dir, overwrite=False,
                )
                parsed = nj_dca_property_tax.parse_dca_workbook(path, year=year)
                staged = nj_dca_property_tax.stage_dataframe(parsed)
                n = nj_dca_property_tax.load_to_postgres(staged, conn)
            except Exception as exc:
                context.log.warning("DCA %d: failed: %s", year, exc)
                failures.append({"year": year, "error": str(exc)})
                continue
            per_year[str(year)] = n
            total += n
            context.log.info("DCA %d: UPSERTed %d rows", year, n)

    if total == 0:
        raise RuntimeError(
            f"NJ DCA produced 0 rows across {candidate_years}; failures={failures}"
        )

    if failures:
        governance.emit(HealthSignal(
            dataset_id="raw.nj_property_tax_county",
            signal_name="partial_fetch_failure",
            severity="warn",
            details={"failures": failures, "succeeded_years": list(per_year.keys())},
        ))

    _emit_materialized(governance, dataset_id="raw.nj_property_tax_county",
                       rows_upserted=total,
                       extra={"per_year": per_year, "n_failures": len(failures)})
    return MaterializeResult(metadata={
        "rows_upserted": MetadataValue.int(total),
        "per_year":      MetadataValue.json(per_year),
        "n_failures":    MetadataValue.int(len(failures)),
    })


# ============================================================================
# RAW ASSET 8: raw.acs_pums_person + raw.acs_pums_housing
# ============================================================================
#
# PUMS is published as a paired (PERSON, HOUSING) drop and the two
# tables are always loaded together (analytical queries always join
# them). We keep them as TWO separate assets so the asset graph is
# truthful (each table has its own row count, fingerprint, freshness),
# and they share a single fetch via a small helper.
#
# Key tradeoff: each materialization downloads 30-100 MB and parses
# ~100K+ rows. This is the heaviest asset in the catalog; we run it
# at most a few times a year (annual ACS PUMS release cadence).
# ============================================================================


def _materialize_pums_pair(
    *, year: int, product: str, state: str,
    pg: PgResource, governance: GovernanceWriter,
    log_fn: object,  # context.log
) -> tuple[int, int]:
    """Fetch + DELETE + COPY one PUMS (year, product, state) drop.

    Returns ``(n_person_rows, n_housing_rows)``. Wraps both COPYs in a
    single transaction. Re-raises :class:`PUMSVintageNotPublishedError`
    so the caller can record a governance signal and skip.

    This helper exists because the person and housing assets BOTH
    depend on a single fetch, but they are TWO assets in the graph.
    The helper centralizes the fetch+stage+load so neither asset
    duplicates the work and both observe the same source bytes.
    """
    fetch = census_acs_pums.fetch_pums_year(
        year=year, product=product, state=state,
    )
    person_staged  = census_acs_pums.stage_person_dataframe(fetch)
    housing_staged = census_acs_pums.stage_housing_dataframe(fetch)
    with pg.connect() as conn:
        n_p, n_h = census_acs_pums.load_to_postgres(
            person_staged, housing_staged, conn,
            year=year, product=product,
        )
    if hasattr(log_fn, "info"):
        log_fn.info(
            "PUMS %s %d %s: %d person rows, %d housing rows",
            state, year, product, n_p, n_h,
        )
    return n_p, n_h


@asset(
    key=AssetKey(["raw", "acs_pums_person"]),
    description=(
        "ACS Public Use Microdata Sample, person-level. NJ-only, "
        "1-year product, 25 demographic + income columns + 80 "
        "replicate weights. ~100K rows per year. The substrate for "
        "all person-level segmented analyses (race, citizenship, "
        "age, education, employment)."
    ),
    group_name="pums",
    freshness_policy=PUMS_FRESHNESS,
    compute_kind="python",
)
def raw_acs_pums_person(
    context: AssetExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> MaterializeResult:
    """Fetch + DELETE + COPY the most-recently-published PUMS vintage.

    PUMS releases ~12 months after the survey year. We attempt the
    trailing PUMS_BACKFILL_YEARS vintages; not-yet-published years
    are caught and recorded as governance warns, not failures.
    """
    today = dt.date.today()
    candidate_years = list(range(
        today.year - PUMS_BACKFILL_YEARS, today.year,
    ))
    per_year: dict[str, int] = {}
    failures: list[dict[str, object]] = []
    total_person = 0
    total_housing = 0

    for year in candidate_years:
        try:
            n_p, n_h = _materialize_pums_pair(
                year=year, product="acs1", state="NJ",
                pg=pg, governance=governance, log_fn=context.log,
            )
        except PUMSVintageNotPublishedError:
            context.log.info("PUMS NJ %d acs1: not yet published", year)
            continue
        except (IngestError, Exception) as exc:
            context.log.warning("PUMS NJ %d acs1: fetch/load failed: %s", year, exc)
            failures.append({"year": year, "error": str(exc)})
            continue
        per_year[str(year)] = n_p
        total_person  += n_p
        total_housing += n_h

    if total_person == 0 and not failures:
        # All candidate years 404'd -- this is a legitimate state
        # early in the calendar year before the prior vintage drops.
        context.log.warning("PUMS NJ: no candidate years yielded data")

    if failures:
        governance.emit(HealthSignal(
            dataset_id="raw.acs_pums_person",
            signal_name="partial_fetch_failure",
            severity="warn",
            details={"failures": failures, "succeeded_years": list(per_year.keys())},
        ))

    _emit_materialized(
        governance, dataset_id="raw.acs_pums_person",
        rows_upserted=total_person,
        extra={
            "per_year_person":   per_year,
            "rows_housing":      total_housing,
            "n_failures":        len(failures),
        },
    )
    return MaterializeResult(metadata={
        "rows_upserted":      MetadataValue.int(total_person),
        "rows_upserted_housing": MetadataValue.int(total_housing),
        "per_year":           MetadataValue.json(per_year),
        "n_failures":         MetadataValue.int(len(failures)),
    })


@asset(
    key=AssetKey(["raw", "acs_pums_housing"]),
    description=(
        "ACS Public Use Microdata Sample, housing-unit-level. NJ-only, "
        "1-year product. Loaded as a side-effect of materializing "
        "raw.acs_pums_person; this asset records the housing row count "
        "and freshness independently."
    ),
    group_name="pums",
    freshness_policy=PUMS_FRESHNESS,
    deps=[AssetDep(AssetKey(["raw", "acs_pums_person"]))],
    compute_kind="sql",
)
def raw_acs_pums_housing(
    context: AssetExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> MaterializeResult:
    """Read the housing row count from the most recent PUMS load.

    Why a separate asset and not rolled into raw.acs_pums_person?
    Because the asset graph should reflect the database schema, and
    raw.acs_pums_housing is its own table. The actual COPY happens
    inside raw.acs_pums_person's compute (single fetch -> two COPYs);
    this asset just observes the result and records freshness.
    """
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), max(year) FROM raw.acs_pums_housing "
            "WHERE state_fips = '34' AND product = 'acs1'"
        )
        row = cur.fetchone()
        rc = int(row[0]) if row and row[0] is not None else 0
        latest_year = row[1] if row else None

    _emit_materialized(
        governance, dataset_id="raw.acs_pums_housing",
        rows_upserted=rc,
        extra={"latest_year": latest_year},
    )
    return MaterializeResult(metadata={
        "row_count":   MetadataValue.int(rc),
        "latest_year": MetadataValue.text(str(latest_year) if latest_year else ""),
    })


# ============================================================================
# DERIVED ASSETS
# ============================================================================
#
# `derived.*` views are SQL VIEWs (not materialized), so the compute
# function does not literally rebuild them. Instead, each derived asset:
#
#   1. Declares its upstream raw assets via `ins=` -- this is the
#      lineage edge the asset graph displays.
#   2. Runs a SELECT against the view to:
#      - Confirm the view evaluates without error post-raw refresh.
#      - Capture row count, distinct counties, year range as metadata.
#      - Compute a content fingerprint (SHA256 over (county, year,
#        rounded value) tuples) to detect non-trivial changes.
#   3. Emits a "materialized" signal so governance.dataset_health
#      tracks derived freshness alongside raw.
#
# When a SQL view is too expensive to compute on every read, promoting
# it to a TABLE-driven materialization is a one-line change in this
# function (REPLACE the SELECT with a DROP TABLE + CREATE TABLE AS).
# Today, every view we expose evaluates in <500 ms on full NJ data,
# so we keep them as views.
# ============================================================================


def _derived_view_fingerprint(
    conn: object,
    *,
    view_name: str,
    fingerprint_query: str,
) -> tuple[int, str]:
    """Run the view, return (row_count, sha256_fingerprint).

    *fingerprint_query* should yield a stable ordered representation
    of the view's contents (ORDER BY is REQUIRED -- without it the
    fingerprint is non-deterministic across runs).
    """
    import hashlib
    cur = conn.cursor()  # type: ignore[attr-defined]
    cur.execute(f"SELECT COUNT(*) FROM {view_name}")
    row = cur.fetchone()
    row_count = int(row[0]) if row else 0
    cur.execute(fingerprint_query)
    h = hashlib.sha256()
    for row in cur:
        h.update(repr(row).encode("utf-8"))
    return row_count, h.hexdigest()


@asset(
    key=AssetKey(["derived", "fred_annual"]),
    description=(
        "Annual averages of canonical FRED rate series (MORTGAGE30US, "
        "DGS10, FEDFUNDS). Derived view; refreshes implicitly on read."
    ),
    group_name="rates_derived",
    deps=[AssetDep(AssetKey(["raw", "fred_observation"]))],
    automation_condition=DERIVED_AUTOMATION,
    compute_kind="sql",
)
def derived_fred_annual(
    context: AssetExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> MaterializeResult:
    """Confirm derived.fred_annual evaluates; record shape + fingerprint."""
    with pg.connect() as conn:
        rc, fp = _derived_view_fingerprint(
            conn, view_name="derived.fred_annual",
            fingerprint_query=(
                "SELECT series_id, year, ROUND(annual_avg::numeric, 4) "
                "FROM derived.fred_annual ORDER BY series_id, year"
            ),
        )
    _emit_materialized(governance, dataset_id="derived.fred_annual",
                       rows_upserted=rc, extra={"content_sha256": fp})
    return MaterializeResult(metadata={
        "row_count":      MetadataValue.int(rc),
        "content_sha256": MetadataValue.text(fp),
    })


@asset(
    key=AssetKey(["derived", "f_acs_mhi_real"]),
    description=(
        "CPI-deflated ACS B19013 median household income, NJ counties. "
        "Joins raw.acs_median_household_income to raw.cpi_u (M13 annual "
        "averages); deflates nominal income to chosen base year."
    ),
    group_name="acs_derived",
    deps=[
        AssetDep(AssetKey(["raw", "acs_median_household_income"])),
        AssetDep(AssetKey(["raw", "cpi_u"])),
    ],
    automation_condition=DERIVED_AUTOMATION,
    compute_kind="sql",
)
def derived_f_acs_mhi_real(
    context: AssetExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> MaterializeResult:
    """Confirm derived.f_acs_mhi_real(2022) evaluates; record fingerprint."""
    base_year = 2022
    with pg.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM derived.f_acs_mhi_real(%s::SMALLINT)",
            (base_year,),
        )
        row = cur.fetchone()
        rc = int(row[0]) if row else 0
        cur.execute(
            "SELECT county_fips, year, ROUND(real_mhi::numeric, 0) "
            "FROM derived.f_acs_mhi_real(%s::SMALLINT) "
            "ORDER BY county_fips, year",
            (base_year,),
        )
        import hashlib
        h = hashlib.sha256()
        for row in cur:
            h.update(repr(row).encode("utf-8"))
        fp = h.hexdigest()

    _emit_materialized(governance, dataset_id="derived.f_acs_mhi_real",
                       rows_upserted=rc,
                       extra={"base_year": base_year, "content_sha256": fp})
    return MaterializeResult(metadata={
        "row_count":      MetadataValue.int(rc),
        "base_year":      MetadataValue.int(base_year),
        "content_sha256": MetadataValue.text(fp),
    })


@asset(
    key=AssetKey(["derived", "fhfa_hpi_indexed_2000"]),
    description=(
        "FHFA HPI re-indexed so that base_year=2000 maps to 100.0 for "
        "every county. Derived view (parameterized SQL function)."
    ),
    group_name="house_prices_derived",
    deps=[AssetDep(AssetKey(["raw", "fhfa_hpi_county"]))],
    automation_condition=DERIVED_AUTOMATION,
    compute_kind="sql",
)
def derived_fhfa_hpi_indexed_2000(
    context: AssetExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> MaterializeResult:
    """Materialize the 2000-base-year reindex of FHFA HPI."""
    base_year = 2000
    with pg.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM derived.f_fhfa_hpi_indexed(%s::SMALLINT)",
            (base_year,),
        )
        row = cur.fetchone()
        rc = int(row[0]) if row else 0
        cur.execute(
            "SELECT county_fips, year, ROUND(hpi_indexed::numeric, 3) "
            "FROM derived.f_fhfa_hpi_indexed(%s::SMALLINT) "
            "ORDER BY county_fips, year",
            (base_year,),
        )
        import hashlib
        h = hashlib.sha256()
        for row in cur:
            h.update(repr(row).encode("utf-8"))
        fp = h.hexdigest()

    _emit_materialized(governance, dataset_id="derived.fhfa_hpi_indexed_2000",
                       rows_upserted=rc,
                       extra={"base_year": base_year, "content_sha256": fp})
    return MaterializeResult(metadata={
        "row_count":      MetadataValue.int(rc),
        "base_year":      MetadataValue.int(base_year),
        "content_sha256": MetadataValue.text(fp),
    })


@asset(
    key=AssetKey(["derived", "housing_burden_ratio"]),
    description=(
        "Per-tenure (renter / owner) and blended housing burden ratio "
        "= annualized housing cost / median household income, by NJ "
        "county and year. The platform's headline metric. The view "
        "also surfaces NJ DCA property-tax context columns "
        "(property_tax_amount_avg, property_tax_share_of_income, etc.) "
        "for NJ counties, year >= 2016. ACS B25088/B25089 already "
        "include property tax in owner cost, so the burden RATIOS do "
        "NOT add it again -- see migration 032 header for the analytic "
        "rationale."
    ),
    group_name="housing_burden",
    deps=[
        AssetDep(AssetKey(["raw", "acs_housing"])),
        AssetDep(AssetKey(["raw", "acs_median_household_income"])),
        AssetDep(AssetKey(["raw", "nj_property_tax_county"])),
    ],
    automation_condition=DERIVED_AUTOMATION,
    compute_kind="sql",
)
def derived_housing_burden_ratio(
    context: AssetExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> MaterializeResult:
    """Confirm derived.housing_burden_ratio evaluates; record fingerprint.

    Fingerprint includes both the burden ratio and the property-tax
    share columns so a refresh of EITHER upstream (ACS or NJ DCA)
    produces a different fingerprint. This makes the dep edges
    truthful -- nothing in the asset graph is decorative.
    """
    with pg.connect() as conn:
        rc, fp = _derived_view_fingerprint(
            conn, view_name="derived.housing_burden_ratio",
            fingerprint_query=(
                "SELECT county_fips, year, "
                "       ROUND(blended_burden_ratio::numeric, 4), "
                "       ROUND(property_tax_share_of_income::numeric, 4) "
                "FROM derived.housing_burden_ratio "
                "ORDER BY county_fips, year"
            ),
        )
    _emit_materialized(governance, dataset_id="derived.housing_burden_ratio",
                       rows_upserted=rc, extra={"content_sha256": fp})
    return MaterializeResult(metadata={
        "row_count":      MetadataValue.int(rc),
        "content_sha256": MetadataValue.text(fp),
    })


# ============================================================================
# DERIVED ASSET 5: derived.pums_burden_segmented
# ============================================================================
#
# The platform's first MATERIALIZED derived TABLE (every prior derived
# asset is a SQL view). The asset's compute reads raw.acs_pums_person +
# raw.acs_pums_housing into Polars, calls derived.pums_burden.compute_*,
# and writes the result via DELETE+INSERT.
#
# Why a table, not a view: weighted percentiles across ~100K rows x 4
# segment dimensions is too expensive to recompute on every read.
# Materializing once per refresh + cheap SELECTs at read time is the
# standard tradeoff.
# ============================================================================


@asset(
    key=AssetKey(["derived", "pums_burden_segmented"]),
    description=(
        "PUMS-derived person-level housing burden ratios, segmented by "
        "tenure x demographic dimension at PUMA grain. The substrate "
        "for 'who is being priced out' analyses. Materialized table; "
        "weighted_n < 1000 cells are suppressed."
    ),
    group_name="pums_derived",
    deps=[
        AssetDep(AssetKey(["raw", "acs_pums_person"])),
        AssetDep(AssetKey(["raw", "acs_pums_housing"])),
    ],
    automation_condition=DERIVED_AUTOMATION,
    compute_kind="python",
)
def derived_pums_burden_segmented(
    context: AssetExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> MaterializeResult:
    """Recompute derived.pums_burden_segmented for ALL (year, product) pairs.

    Strategy: discover every (year, product) pair currently in raw and
    materialize each one separately. The derived table is keyed on
    (year, product, puma, ...) so different vintages coexist; consumers
    pick by ?product= filter at the API layer.

    Materializing all pairs means: when a new acs5 vintage lands
    alongside an existing acs1, both stay queryable. The previous
    "MAX(year), product LIMIT 1" picked one and silently shadowed the
    other -- a substrate-honesty hazard.

    Each (year, product) is one transaction; failure of one does not
    leave others stale. We aggregate metadata across all pairs for
    the asset materialization summary.
    """
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT year, product FROM raw.acs_pums_person "
            "WHERE state_fips = '34' ORDER BY year DESC, product"
        )
        pairs = [(int(y), str(p)) for (y, p) in cur.fetchall()]
        if not pairs:
            context.log.warning(
                "raw.acs_pums_person has no NJ rows; skipping derived materialization"
            )
            _emit_materialized(
                governance, dataset_id="derived.pums_burden_segmented",
                rows_upserted=0, extra={"reason": "empty_upstream"},
            )
            return MaterializeResult(metadata={"row_count": MetadataValue.int(0)})

        total_inserted = 0
        per_pair: list[dict[str, int | str]] = []

        for (year, product) in pairs:
            cur.execute(
                "SELECT * FROM raw.acs_pums_person "
                "WHERE state_fips = '34' AND year = %s AND product = %s",
                (year, product),
            )
            person_cols = [d.name for d in cur.description] if cur.description else []
            person_df = _rows_to_polars(cur.fetchall(), person_cols)

            cur.execute(
                "SELECT * FROM raw.acs_pums_housing "
                "WHERE state_fips = '34' AND year = %s AND product = %s",
                (year, product),
            )
            housing_cols = [d.name for d in cur.description] if cur.description else []
            housing_df = _rows_to_polars(cur.fetchall(), housing_cols)

            context.log.info(
                "Computing pums_burden_segmented for year=%d product=%s "
                "from %d person rows + %d housing rows",
                year, product, person_df.height, housing_df.height,
            )
            result = pums_burden.compute_burden_segmented(
                person_df, housing_df,
                year=year, product=product, state_fips="34",
            )
            n_inserted = pums_burden.load_to_postgres(
                result, conn, year=year, product=product,
            )
            total_inserted += n_inserted

            cur.execute(
                "SELECT count(*), count(*) FILTER (WHERE suppressed) "
                "FROM derived.pums_burden_segmented "
                "WHERE year = %s AND product = %s",
                (year, product),
            )
            agg_row = cur.fetchone()
            per_pair.append({
                "year": year,
                "product": product,
                "rows": n_inserted,
                "total_cells": int(agg_row[0]) if agg_row else 0,
                "suppressed_cells": int(agg_row[1]) if agg_row else 0,
            })

    _emit_materialized(
        governance, dataset_id="derived.pums_burden_segmented",
        rows_upserted=total_inserted,
        extra={
            "pairs":           per_pair,
            "formula_version": pums_burden.FORMULA_VERSION,
        },
    )
    return MaterializeResult(metadata={
        "row_count":         MetadataValue.int(total_inserted),
        "n_pairs":           MetadataValue.int(len(pairs)),
        "formula_version":   MetadataValue.text(pums_burden.FORMULA_VERSION),
    })


# ----------------------------------------------------------------------------
# derived.pums_burden_county_segmented
#   County-grain PUMS burden, segmented by tenure x demographic dim.
#   Re-aggregates raw PUMS via population-weighted PUMA-county allocation
#   (ref.puma2020_county_xwalk). NOT computed by rolling up the PUMA
#   table -- median-of-medians is statistically invalid.
# ----------------------------------------------------------------------------
@asset(
    key=AssetKey(["derived", "pums_burden_county_segmented"]),
    description=(
        "PUMS-derived person-level housing burden ratios at COUNTY grain, "
        "segmented by tenure x demographic dim. Re-aggregated from raw "
        "PUMS via ref.puma2020_county_xwalk -- not rolled up from the "
        "PUMA-grain table (which would be median-of-medians)."
    ),
    group_name="pums_derived",
    deps=[
        AssetDep(AssetKey(["raw", "acs_pums_person"])),
        AssetDep(AssetKey(["raw", "acs_pums_housing"])),
    ],
    automation_condition=DERIVED_AUTOMATION,
    compute_kind="python",
)
def derived_pums_burden_county_segmented(
    context: AssetExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> MaterializeResult:
    """Recompute derived.pums_burden_county_segmented for ALL (year, product) pairs.

    See `derived_pums_burden_segmented` for the multi-pair rationale.
    Reads the crosswalk from ref.puma2020_county_xwalk once and reuses
    it across all pairs. The crosswalk is ref data (slow-changing,
    seeded), so it is not a Dagster asset dependency.

    Re-aggregates from raw PUMS, NOT from the PUMA-grain derived
    table -- median-of-medians is statistically invalid.
    """
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT year, product FROM raw.acs_pums_person "
            "WHERE state_fips = '34' ORDER BY year DESC, product"
        )
        pairs = [(int(y), str(p)) for (y, p) in cur.fetchall()]
        if not pairs:
            context.log.warning(
                "raw.acs_pums_person has no NJ rows; "
                "skipping county-grain materialization"
            )
            _emit_materialized(
                governance, dataset_id="derived.pums_burden_county_segmented",
                rows_upserted=0, extra={"reason": "empty_upstream"},
            )
            return MaterializeResult(metadata={"row_count": MetadataValue.int(0)})

        xwalk_df = pums_burden_county.load_xwalk_from_postgres(conn, state_fips="34")

        total_inserted = 0
        per_pair: list[dict[str, int | str]] = []

        for (year, product) in pairs:
            cur.execute(
                "SELECT * FROM raw.acs_pums_person "
                "WHERE state_fips = '34' AND year = %s AND product = %s",
                (year, product),
            )
            person_cols = [d.name for d in cur.description] if cur.description else []
            person_df = _rows_to_polars(cur.fetchall(), person_cols)

            cur.execute(
                "SELECT * FROM raw.acs_pums_housing "
                "WHERE state_fips = '34' AND year = %s AND product = %s",
                (year, product),
            )
            housing_cols = [d.name for d in cur.description] if cur.description else []
            housing_df = _rows_to_polars(cur.fetchall(), housing_cols)

            context.log.info(
                "Computing pums_burden_county_segmented for year=%d product=%s "
                "from %d person rows, %d housing rows, %d xwalk rows",
                year, product,
                person_df.height, housing_df.height, xwalk_df.height,
            )
            result = pums_burden_county.compute_burden_county_segmented(
                person_df, housing_df, xwalk_df,
                year=year, product=product, state_fips="34",
            )
            n_inserted = pums_burden_county.load_to_postgres(
                result, conn, year=year, product=product,
            )
            total_inserted += n_inserted

            cur.execute(
                "SELECT count(*), count(*) FILTER (WHERE suppressed), "
                "       count(DISTINCT county_fips) "
                "FROM derived.pums_burden_county_segmented "
                "WHERE year = %s AND product = %s",
                (year, product),
            )
            agg_row = cur.fetchone()
            per_pair.append({
                "year": year,
                "product": product,
                "rows": n_inserted,
                "total_cells":      int(agg_row[0]) if agg_row else 0,
                "suppressed_cells": int(agg_row[1]) if agg_row else 0,
                "n_counties":       int(agg_row[2]) if agg_row else 0,
            })

    _emit_materialized(
        governance, dataset_id="derived.pums_burden_county_segmented",
        rows_upserted=total_inserted,
        extra={
            "pairs":           per_pair,
            "formula_version": pums_burden.FORMULA_VERSION,
        },
    )
    return MaterializeResult(metadata={
        "row_count":         MetadataValue.int(total_inserted),
        "n_pairs":           MetadataValue.int(len(pairs)),
        "formula_version":   MetadataValue.text(pums_burden.FORMULA_VERSION),
    })


# ============================================================================
# Asset registry
# ============================================================================

# ============================================================================
# RAW ASSETS: FEC bulk (Tier 4 v1)
# ============================================================================
#
# Three SDAs, one per FEC bulk file kind. They share a single helper
# that resolves "the current cycle" so the whole graph stays in sync
# (loading 2024 cn alongside 2022 cm would give wrong join results
# in the canonical view; we always load the same cycle for all three).
#
# raw.fec_contribution depends on raw.fec_committee, which depends on
# raw.fec_candidate. The dependency chain mirrors the foreign-key
# direction so a downstream "load contributions for cycle X" implies
# "candidates and committees for cycle X are already present".
# ============================================================================


def _fec_default_cycle(today: dt.date | None = None) -> str:
    """Return the most recent COMPLETED FEC cycle (last even year <= today.year).

    The 2024 cycle's bulk files keep getting appended through Q1 of the
    following odd year (post-election year-end reports), so we treat
    even years as cycle anchors. During an active cycle the operator
    can override with a Dagster config; the default tracks "the cycle
    everyone refers to right now".
    """
    today = today or dt.date.today()
    even_year = today.year if today.year % 2 == 0 else today.year - 1
    return str(even_year)


def _materialize_fec_kind(
    *,
    cycle: str,
    file_kind: str,
    pg: PgResource,
    log_fn: object,  # context.log
) -> tuple[int, str, str]:
    """Fetch + load one FEC bulk file kind. Returns (n_rows, sha256, vintage)."""
    fetch = fec.fetch_fec_bulk(
        cycle=cycle,
        file_kind=file_kind,
        dest_dir=Path("data/manual/fec") / cycle,
        overwrite=False,
    )
    with pg.connect() as conn:
        if file_kind == "indiv":
            n = fec.load_fec_indiv(fetch, conn, cycle=cycle)
        else:
            parse = fec.parse_fec_small_table(
                fetch, cycle=cycle, file_kind=file_kind,
            )
            n = fec.load_fec_small_table(parse, conn)
        conn.commit()
    if hasattr(log_fn, "info"):
        log_fn.info(
            "FEC %s %s: loaded %d rows (sha256=%s..., vintage=%s)",
            file_kind, cycle, n, fetch.source_sha256[:12], fetch.source_vintage,
        )
    return n, fetch.source_sha256, fetch.source_vintage


@asset(
    key=AssetKey(["raw", "fec_candidate"]),
    description=(
        "FEC Candidate Master (cn{yy}.zip). Federal candidates registered "
        "with the FEC for one election cycle. Small (<1MB compressed); "
        "loaded fully on each materialization."
    ),
    group_name="fec",
    freshness_policy=FEC_FRESHNESS,
    compute_kind="python",
)
def raw_fec_candidate(
    context: AssetExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> MaterializeResult:
    """Refresh raw.fec_candidate for the current FEC cycle."""
    cycle = _fec_default_cycle()
    n, sha, vintage = _materialize_fec_kind(
        cycle=cycle, file_kind="cn", pg=pg, log_fn=context.log,
    )
    _emit_materialized(
        governance, dataset_id="raw.fec_candidate",
        rows_upserted=n,
        extra={"cycle": cycle, "source_vintage": vintage,
               "source_sha256": sha},
    )
    return MaterializeResult(metadata={
        "rows_upserted":  MetadataValue.int(n),
        "cycle":          MetadataValue.text(cycle),
        "source_vintage": MetadataValue.text(vintage),
        "source_sha256":  MetadataValue.text(sha),
    })


@asset(
    key=AssetKey(["raw", "fec_committee"]),
    description=(
        "FEC Committee Master (cm{yy}.zip). Federal political "
        "committees -- principal campaign committees, PACs, party "
        "committees. Small (~1MB); loaded fully on each materialization. "
        "Joins to raw.fec_candidate via cand_id."
    ),
    group_name="fec",
    freshness_policy=FEC_FRESHNESS,
    deps=[AssetDep(AssetKey(["raw", "fec_candidate"]))],
    compute_kind="python",
)
def raw_fec_committee(
    context: AssetExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> MaterializeResult:
    """Refresh raw.fec_committee for the current FEC cycle."""
    cycle = _fec_default_cycle()
    n, sha, vintage = _materialize_fec_kind(
        cycle=cycle, file_kind="cm", pg=pg, log_fn=context.log,
    )
    _emit_materialized(
        governance, dataset_id="raw.fec_committee",
        rows_upserted=n,
        extra={"cycle": cycle, "source_vintage": vintage,
               "source_sha256": sha},
    )
    return MaterializeResult(metadata={
        "rows_upserted":  MetadataValue.int(n),
        "cycle":          MetadataValue.text(cycle),
        "source_vintage": MetadataValue.text(vintage),
        "source_sha256":  MetadataValue.text(sha),
    })


@asset(
    key=AssetKey(["raw", "fec_contribution"]),
    description=(
        "FEC Individual Contributions (indiv{yy}.zip / itcont.txt). "
        "Per-transaction itemized contributions to federal committees. "
        "Large (~4 GB compressed, ~25M rows for a presidential cycle); "
        "streamed directly into Postgres via COPY without "
        "materializing in Python memory."
    ),
    group_name="fec",
    freshness_policy=FEC_FRESHNESS,
    deps=[AssetDep(AssetKey(["raw", "fec_committee"]))],
    compute_kind="python",
)
def raw_fec_contribution(
    context: AssetExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> MaterializeResult:
    """Refresh raw.fec_contribution for the current FEC cycle.

    The asset compute is heavy (multi-GB download + ~25M-row COPY).
    Production schedules should run this no more often than the FEC
    bulk refresh cadence (bi-weekly during cycle, monthly off-cycle).
    """
    cycle = _fec_default_cycle()
    n, sha, vintage = _materialize_fec_kind(
        cycle=cycle, file_kind="indiv", pg=pg, log_fn=context.log,
    )
    _emit_materialized(
        governance, dataset_id="raw.fec_contribution",
        rows_upserted=n,
        extra={"cycle": cycle, "source_vintage": vintage,
               "source_sha256": sha},
    )
    return MaterializeResult(metadata={
        "rows_upserted":  MetadataValue.int(n),
        "cycle":          MetadataValue.text(cycle),
        "source_vintage": MetadataValue.text(vintage),
        "source_sha256":  MetadataValue.text(sha),
    })


# ============================================================================
# RAW ASSET: HHS-OIG LEIE (FRAUD-F5 substrate)
# ============================================================================
#
# Monthly full-replace bulk pull from HHS-OIG. Idempotent: a no-change
# pull is a HEAD probe + a no-op last_seen_at bump; a real refresh
# streams ~80K rows through a COPY-then-UPSERT in well under a second.
# No FEC dependency -- LEIE stands alone until a future "leie x
# fec_donor" entity-match layer joins them.
# ============================================================================


@asset(
    key=AssetKey(["raw", "hhs_oig_leie"]),
    description=(
        "HHS-OIG LEIE: federally-excluded individuals and entities. "
        "Refreshed monthly from oig.hhs.gov/exclusions/downloadables/"
        "UPDATED.csv (full database, not supplements). UPSERT semantics "
        "by record_hash; reinstatements detected via last_seen_at "
        "falling behind the most recent pull (see "
        "derived.v_leie_active). Substrate-only -- entity match against "
        "FEC / USAspending names is a separate downstream layer."
    ),
    group_name="fec",
    freshness_policy=LEIE_FRESHNESS,
    compute_kind="python",
)
def raw_hhs_oig_leie(
    context: AssetExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> MaterializeResult:
    """Fetch UPDATED.csv (conditional GET) and UPSERT into raw.hhs_oig_leie.

    Vintage stamp = current UTC YYYY-MM. Operators backfilling old
    pulls should run ``nj-ingest-leie load --vintage-month`` directly,
    not through this asset.
    """
    out_dir = Path("data/cache/hhs_oig_leie")
    today_utc = dt.datetime.now(dt.UTC).date()
    vintage_month = today_utc.strftime("%Y-%m")

    fetch = hhs_oig_leie.fetch_leie_csv(dest_dir=out_dir, overwrite=False)
    parse = hhs_oig_leie.parse_leie_csv(fetch)
    context.log.info(
        "LEIE: parsed %d rows (cache_hit=%s, sha256=%s...)",
        parse.n_rows, fetch.cache_hit, fetch.source_sha256[:12],
    )

    with pg.connect() as conn:
        n = hhs_oig_leie.load_to_postgres(
            parse, conn, vintage_month=vintage_month,
        )
        conn.commit()

    _emit_materialized(
        governance, dataset_id="raw.hhs_oig_leie",
        rows_upserted=n,
        extra={
            "vintage_month":  vintage_month,
            "source_vintage": fetch.source_vintage,
            "source_sha256":  fetch.source_sha256,
            "cache_hit":      fetch.cache_hit,
            "n_bytes":        fetch.n_bytes,
        },
    )
    return MaterializeResult(metadata={
        "rows_upserted":  MetadataValue.int(n),
        "vintage_month":  MetadataValue.text(vintage_month),
        "source_vintage": MetadataValue.text(fetch.source_vintage),
        "source_sha256":  MetadataValue.text(fetch.source_sha256),
        "cache_hit":      MetadataValue.bool(fetch.cache_hit),
        "n_bytes":        MetadataValue.int(fetch.n_bytes),
    })


# ============================================================================
# RAW ASSET: USAspending federal-award (FRAUD-F1 substrate)
# ============================================================================
#
# Monthly fiscal-year-to-date paginated REST pull from
# USAspending.gov. The asset materializes the CURRENT FY only; older
# FYs are backfilled by operators running
# `nj-ingest-usaspending fetch-and-load --fiscal-year YYYY` directly,
# which bypasses Dagster's freshness machinery.
#
# Why current-FY-only on the schedule: USAspending continuously
# back-amends prior-FY awards (transaction modifications can land 6-12
# months after the original obligation). A platform that schedules a
# pull of the CURRENT FY catches ~95% of useful changes for free; the
# tail of prior-FY amendments is a one-off backfill concern, not a
# steady-state freshness concern.
# ============================================================================


def _usaspending_current_fiscal_year() -> int:
    """Return the federal FY active at "now" (US Eastern wall clock).

    Federal FY N runs Oct 1 of CY(N-1) through Sep 30 of CY(N). On
    Oct 1, the FY rolls forward; the platform's monthly Dagster tick
    naturally re-pulls the new FY on its next run.
    """
    today = dt.datetime.now(dt.UTC).date()
    return today.year + 1 if today.month >= 10 else today.year


@asset(
    key=AssetKey(["raw", "usaspending_award"]),
    description=(
        "USAspending.gov federal CONTRACT awards (award_type_codes "
        "A/B/C/D) with place of performance in New Jersey. Pulled from "
        "POST /api/v2/search/spending_by_award/ as a paginated REST "
        "fetch, current-FY-to-date on the schedule. UPSERT-by-"
        "generated_unique_award_id; the loader bumps last_seen_at on "
        "every present row so reinstatements / removals are recoverable "
        "via the derived.v_usaspending_award_active view (35-day "
        "active-window). Substrate-only: cross-source signals "
        "(USAspending recipient x FEC donor / LEIE individual) are a "
        "separate downstream layer."
    ),
    group_name="fec",
    freshness_policy=USASPENDING_FRESHNESS,
    compute_kind="python",
)
def raw_usaspending_award(
    context: AssetExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> MaterializeResult:
    """Fetch the current FY's NJ-pop contract awards and UPSERT them.

    Operators backfilling older FYs should run
    ``nj-ingest-usaspending fetch-and-load --fiscal-year YYYY``
    directly, not through this asset.
    """
    out_dir = Path("data/cache/usaspending")
    fiscal_year = _usaspending_current_fiscal_year()

    fetch = usaspending.fetch_awards(
        fiscal_year=fiscal_year, dest_dir=out_dir, overwrite=False,
    )
    parse = usaspending.parse_awards(fetch)
    context.log.info(
        "usaspending: parsed %d rows (cache_hit=%s, file_sha=%s..., "
        "filter_sha=%s...)",
        parse.n_rows, fetch.cache_hit,
        fetch.file_sha256[:12], fetch.filter_sha256[:12],
    )

    with pg.connect() as conn:
        n = usaspending.load_to_postgres(parse, conn)
        conn.commit()

    _emit_materialized(
        governance, dataset_id="raw.usaspending_award",
        rows_upserted=n,
        extra={
            "fiscal_year":     fiscal_year,
            "state":           fetch.state,
            "filter_sha256":   fetch.filter_sha256,
            "file_sha256":     fetch.file_sha256,
            "n_pages":         fetch.n_pages,
            "n_awards":        fetch.n_awards,
            "cache_hit":       fetch.cache_hit,
        },
    )
    return MaterializeResult(metadata={
        "rows_upserted":  MetadataValue.int(n),
        "fiscal_year":    MetadataValue.int(fiscal_year),
        "state":          MetadataValue.text(fetch.state),
        "filter_sha256":  MetadataValue.text(fetch.filter_sha256),
        "file_sha256":    MetadataValue.text(fetch.file_sha256),
        "n_pages":        MetadataValue.int(fetch.n_pages),
        "n_awards":       MetadataValue.int(fetch.n_awards),
        "cache_hit":      MetadataValue.bool(fetch.cache_hit),
    })


# ============================================================================
# DERIVED FRAUD-RISK ASSETS (Tier 4 v3: L1 + L3a)
# ============================================================================
#
# derived.fraud_signal_observation (L1) is a TABLE populated by the L1
# adapter dispatcher (migration 051). Every refresh of raw.fec_candidate /
# raw.fec_committee should fan out a re-materialization here so the
# analyst queue is never stale relative to the underlying entity rosters.
#
# derived.v_entity_fraud_risk (L3a) is a VIEW on top of L1 -> L2 ->
# fraud_risk_score. The asset is fingerprint-only (same pattern as
# derived.housing_burden_ratio): we evaluate the view, hash the result,
# and emit a governance row. Asset checks attached to the view catch the
# failure mode "L1 has rows but L3a returns score=0 for everyone".
# ============================================================================


@asset(
    key=AssetKey(["derived", "fraud_signal_observation"]),
    description=(
        "TIER 4 v3 L1: long-format fraud signal observation table. One row "
        "per (cycle, entity_kind, entity_id, signal_id) for every entity "
        "that fired any of the eight v2.A structural signals. Compute calls "
        "derived.refresh_all_fraud_signal_observations(cycle), which fans "
        "out to eight idempotent per-signal refreshers (each does "
        "DELETE + INSERT within its own (cycle, signal_id) slice). Re-runs "
        "for the same cycle are safe. Downstream surfaces (L2 view, L3a "
        "scoring view, /fec/risk/* API, /fraud UI risk queue) read from "
        "this table; only this asset's compute writes to it."
    ),
    group_name="fec",
    freshness_policy=FEC_FRESHNESS,
    deps=[
        AssetDep(AssetKey(["raw", "fec_candidate"])),
        AssetDep(AssetKey(["raw", "fec_committee"])),
    ],
    automation_condition=DERIVED_AUTOMATION,
    compute_kind="sql",
)
def derived_fraud_signal_observation(
    context: AssetExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> MaterializeResult:
    """Refresh derived.fraud_signal_observation for the current FEC cycle.

    The dispatcher returns total rows inserted across all eight signals.
    We additionally count rows per signal_id so the materialization
    metadata makes it obvious if a refresher silently dropped to zero
    (the matching asset check then fails on the same condition; this
    metadata is the breadcrumb for the operator who is debugging it).
    """
    cycle = _fec_default_cycle()
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_all_fraud_signal_observations(%s)",
            (cycle,),
        )
        row = cur.fetchone()
        n_total = int(row[0]) if row else 0
        cur.execute(
            "SELECT signal_id, COUNT(*) "
            "FROM derived.fraud_signal_observation "
            "WHERE cycle = %s "
            "GROUP BY signal_id "
            "ORDER BY signal_id",
            (cycle,),
        )
        per_signal: dict[str, int] = {r[0]: int(r[1]) for r in cur.fetchall()}

        # Capture the per-signal counts into the baseline history
        # (mig 097). The companion asset check
        # `per_signal_distribution_drift_within_2sigma` consumes this
        # via governance.v_fraud_signal_baseline_stats. The capture
        # function is idempotent within a given microsecond (PK
        # includes captured_at) so concurrent calls are safe in
        # practice -- the bi-weekly schedule guarantees minutes-of-
        # spacing between runs. We tolerate failure here so a
        # governance-schema regression cannot break the refresher.
        try:
            cur.execute(
                "SELECT governance.capture_fraud_signal_baseline(%s)",
                (cycle,),
            )
            row = cur.fetchone()
            n_baseline_rows = int(row[0]) if row else 0
        except Exception as exc:
            context.log.warning(
                "fraud_signal_baseline capture failed (non-fatal): %r",
                exc,
            )
            n_baseline_rows = -1
        conn.commit()

    _emit_materialized(
        governance, dataset_id="derived.fraud_signal_observation",
        rows_upserted=n_total,
        extra={
            "cycle": cycle,
            "per_signal_counts": per_signal,
            "n_baseline_rows_captured": n_baseline_rows,
        },
    )
    return MaterializeResult(metadata={
        "rows_upserted":              MetadataValue.int(n_total),
        "cycle":                      MetadataValue.text(cycle),
        "per_signal_counts":          MetadataValue.json(per_signal),
        "n_signals_present":          MetadataValue.int(len(per_signal)),
        "n_baseline_rows_captured":   MetadataValue.int(n_baseline_rows),
    })


# ============================================================================
# CROSS-SOURCE FRAUD SIGNAL: entity_on_leie (FRAUD-F5b)
# ============================================================================
#
# This asset is structurally separate from derived_fraud_signal_observation
# (the structural-FEC signals) because:
#
#   1. It depends on raw.hhs_oig_leie in addition to FEC. Its lineage
#      should reflect that; the structural dispatcher's lineage should
#      NOT (it's pure-FEC).
#   2. The signal is data-conditional: in environments without LEIE
#      loaded, this asset materializes 0 rows successfully. The
#      structural dispatcher's signal-coverage check expects all 8
#      structural signals to fire; entity_on_leie cannot be folded
#      into that contract without making the check noisy.
#
# Both refreshers write to the same target table
# (derived.fraud_signal_observation) on disjoint signal_id slices, so
# the L2/L3a downstream surfaces pick up entity_on_leie for free.
# ============================================================================


@asset(
    key=AssetKey(["derived", "signal_entity_on_leie"]),
    description=(
        "TIER 4 v3 / FRAUD-F5b: entity_on_leie cross-source signal. "
        "Joins raw.fec_candidate / raw.fec_committee against "
        "derived.v_leie_individual_canonical on canonical \"LAST|FIRST\" "
        "key. Idempotent on its (cycle, signal_id='entity_on_leie') "
        "slice; writes into derived.fraud_signal_observation. Severity "
        "is fixed at 5 (CRITICAL) on every match; peer_percentile is "
        "rate-based within entity_kind ('kind=candidate' or "
        "'kind=treasurer'). The L3a v_entity_fraud_risk view picks up "
        "the matches automatically because it reads from the shared "
        "fraud_signal_observation table."
    ),
    group_name="fec",
    deps=[
        AssetDep(AssetKey(["raw", "fec_candidate"])),
        AssetDep(AssetKey(["raw", "fec_committee"])),
        AssetDep(AssetKey(["raw", "hhs_oig_leie"])),
    ],
    automation_condition=DERIVED_AUTOMATION,
    compute_kind="sql",
)
def derived_signal_entity_on_leie(
    context: AssetExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> MaterializeResult:
    """Refresh entity_on_leie observations for the current FEC cycle.

    Reports per-entity-kind match counts as metadata so the operator
    can spot anomalies (zero matches when both raw tables are non-empty
    is a canonicalization bug; matches > 5% of the bucket suggests
    over-matching). The asset check on this asset enforces those
    bounds programmatically.
    """
    cycle = _fec_default_cycle()
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_signal_entity_on_leie(%s)",
            (cycle,),
        )
        row = cur.fetchone()
        n_total = int(row[0]) if row else 0
        cur.execute(
            "SELECT entity_kind, COUNT(*) "
            "FROM derived.fraud_signal_observation "
            "WHERE cycle = %s AND signal_id = 'entity_on_leie' "
            "GROUP BY entity_kind "
            "ORDER BY entity_kind",
            (cycle,),
        )
        per_kind: dict[str, int] = {r[0]: int(r[1]) for r in cur.fetchall()}
        # Auxiliary diagnostics: how many distinct LEIE rows backed the
        # matches? If matches are concentrated on a single LEIE record
        # (a JOHN SMITH false-positive flood), this number is ~1; on a
        # healthy distribution it tracks the match count.
        cur.execute(
            "SELECT COUNT(DISTINCT (regexp_match(evidence_url, "
            "'leie=([0-9a-f]{64})'))[1]) "
            "FROM derived.fraud_signal_observation "
            "WHERE cycle = %s AND signal_id = 'entity_on_leie'",
            (cycle,),
        )
        row = cur.fetchone()
        n_distinct_leie = int(row[0]) if row else 0
        conn.commit()

    _emit_materialized(
        governance, dataset_id="derived.signal_entity_on_leie",
        rows_upserted=n_total,
        extra={
            "cycle":             cycle,
            "per_entity_kind":   per_kind,
            "n_distinct_leie":   n_distinct_leie,
        },
    )
    return MaterializeResult(metadata={
        "rows_upserted":    MetadataValue.int(n_total),
        "cycle":            MetadataValue.text(cycle),
        "per_entity_kind":  MetadataValue.json(per_kind),
        "n_distinct_leie":  MetadataValue.int(n_distinct_leie),
    })


# ============================================================================
# DERIVED ASSET: donor_employed_by_nj_contractor cross-source signal
# ============================================================================
#
# Joins raw.fec_contribution.employer text against
# derived.v_usaspending_award_active.recipient_name on a canonical
# employer-name key. Surfaces FEC donor clusters whose self-reported
# employer matches a NJ-pop federal contractor. Pure SQL refresh
# (function in migration 056); the asset just orchestrates the call,
# captures cardinality, and emits governance metadata.
#
# Dep edges trace BOTH source roots: raw.fec_contribution (donor side)
# and raw.usaspending_award (contractor side). When either upstream
# materializes, this signal is eligible to re-materialize via
# DERIVED_AUTOMATION.
# ============================================================================


@asset(
    key=AssetKey(["derived", "signal_donor_employed_by_nj_contractor"]),
    description=(
        "TIER 4 v3 / FRAUD-F1 (signal layer): "
        "donor_employed_by_nj_contractor cross-source signal. Joins "
        "raw.fec_contribution.employer against "
        "derived.v_usaspending_award_active.recipient_name on a "
        "canonical employer-name key (derived.f_canonical_employer_name). "
        "Idempotent on its (cycle, signal_id="
        "'donor_employed_by_nj_contractor') slice; writes into "
        "derived.fraud_signal_observation with entity_kind="
        "'donor_cluster', entity_id=canonical employer, severity=3 "
        "(HIGH), peer_percentile=CUME_DIST of SUM(positive amt) within "
        "the matched-cluster bucket. The L3a v_entity_fraud_risk view "
        "picks up the matches automatically because it reads from the "
        "shared fraud_signal_observation table."
    ),
    group_name="fec",
    deps=[
        AssetDep(AssetKey(["raw", "fec_contribution"])),
        AssetDep(AssetKey(["raw", "usaspending_award"])),
    ],
    automation_condition=DERIVED_AUTOMATION,
    compute_kind="sql",
)
def derived_signal_donor_employed_by_nj_contractor(
    context: AssetExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> MaterializeResult:
    """Refresh donor_employed_by_nj_contractor for the current FEC cycle.

    Reports cluster-count and aggregate-money-flow as metadata so the
    operator can spot anomalies (zero clusters when both raw tables
    are non-empty is a canonicalization regression; cluster count
    >>1% of distinct FEC employers suggests over-matching). The
    asset check on this asset enforces those bounds programmatically.
    """
    cycle = _fec_default_cycle()
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_signal_donor_employed_by_nj_contractor(%s)",
            (cycle,),
        )
        row = cur.fetchone()
        n_clusters = int(row[0]) if row else 0

        # Aggregate the matched money flow for the operator. SUM of
        # raw_value across all clusters = total positive donations
        # whose donor's employer matched a NJ contractor in this cycle.
        cur.execute(
            "SELECT COALESCE(SUM(raw_value), 0)::TEXT, "
            "       COALESCE(MAX(raw_value), 0)::TEXT, "
            "       COUNT(*) "
            "FROM derived.fraud_signal_observation "
            "WHERE cycle = %s "
            "  AND signal_id = 'donor_employed_by_nj_contractor'",
            (cycle,),
        )
        agg = cur.fetchone()
        total_money    = str(agg[0]) if agg else "0"
        max_cluster    = str(agg[1]) if agg else "0"
        n_rows_actual  = int(agg[2]) if agg else 0
        conn.commit()

    _emit_materialized(
        governance,
        dataset_id="derived.signal_donor_employed_by_nj_contractor",
        rows_upserted=n_clusters,
        extra={
            "cycle":           cycle,
            "n_clusters":      n_rows_actual,
            "total_money_usd": total_money,
            "max_cluster_usd": max_cluster,
        },
    )
    return MaterializeResult(metadata={
        "rows_upserted":   MetadataValue.int(n_clusters),
        "cycle":           MetadataValue.text(cycle),
        "n_clusters":      MetadataValue.int(n_rows_actual),
        "total_money_usd": MetadataValue.text(total_money),
        "max_cluster_usd": MetadataValue.text(max_cluster),
    })


# ============================================================================
# DERIVED ASSET: candidate_funded_by_nj_contractor_employees
# ============================================================================
#
# Candidate-side projection of donor_employed_by_nj_contractor. Reads
# the matched-employer set from L1 (signal 056's output), joins
# through fec_contribution -> fec_committee -> fec_candidate, sums
# positive contributions per candidate, percentile-ranks per
# (office, state) bucket. The signal answers the canonical analyst
# question: which candidates received money from contractor-employed
# donors?
#
# This asset MUST run after derived.signal_donor_employed_by_nj_contractor
# in the same cycle. The dep edge enforces ordering; the SQL function
# header documents the contract.
# ============================================================================


@asset(
    key=AssetKey([
        "derived", "signal_candidate_funded_by_nj_contractor_employees",
    ]),
    description=(
        "TIER 4 v3 / FRAUD-F1 (signal layer): "
        "candidate_funded_by_nj_contractor_employees cross-source "
        "signal. Candidate-side projection of "
        "donor_employed_by_nj_contractor: reads the matched-employer "
        "set from L1 (cycle, signal_id='donor_employed_by_nj_contractor'), "
        "joins through raw.fec_contribution -> raw.fec_committee -> "
        "raw.fec_candidate, sums positive transaction_amt per cand_id, "
        "percentile-ranks per (cand_office, cand_office_st) bucket. "
        "Severity=3 (HIGH); peer_percentile is CUME_DIST. The L3a "
        "v_entity_fraud_risk view picks up the rows automatically."
    ),
    group_name="fec",
    deps=[
        AssetDep(AssetKey(["derived", "signal_donor_employed_by_nj_contractor"])),
        AssetDep(AssetKey(["raw", "fec_committee"])),
        AssetDep(AssetKey(["raw", "fec_candidate"])),
        AssetDep(AssetKey(["raw", "fec_contribution"])),
    ],
    automation_condition=DERIVED_AUTOMATION,
    compute_kind="sql",
)
def derived_signal_candidate_funded_by_nj_contractor_employees(
    context: AssetExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> MaterializeResult:
    """Refresh candidate_funded_by_nj_contractor_employees for the cycle.

    Reports per-bucket cardinality and aggregate money-flow as
    metadata. The asset check enforces match-rate plausibility
    bounds programmatically.
    """
    cycle = _fec_default_cycle()
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_signal_candidate_funded_by_nj_contractor_employees(%s)",
            (cycle,),
        )
        row = cur.fetchone()
        n_candidates = int(row[0]) if row else 0

        cur.execute(
            "SELECT peer_bucket, COUNT(*), "
            "       COALESCE(SUM(raw_value), 0)::TEXT, "
            "       COALESCE(MAX(raw_value), 0)::TEXT "
            "FROM derived.fraud_signal_observation "
            "WHERE cycle = %s "
            "  AND signal_id = "
            "      'candidate_funded_by_nj_contractor_employees' "
            "GROUP BY peer_bucket "
            "ORDER BY peer_bucket",
            (cycle,),
        )
        per_bucket: dict[str, dict[str, str]] = {
            r[0]: {
                "n_candidates": str(r[1]),
                "total_usd":    str(r[2]),
                "max_usd":      str(r[3]),
            }
            for r in cur.fetchall()
        }

        cur.execute(
            "SELECT COALESCE(SUM(raw_value), 0)::TEXT, "
            "       COALESCE(MAX(raw_value), 0)::TEXT "
            "FROM derived.fraud_signal_observation "
            "WHERE cycle = %s "
            "  AND signal_id = "
            "      'candidate_funded_by_nj_contractor_employees'",
            (cycle,),
        )
        agg = cur.fetchone()
        total_money = str(agg[0]) if agg else "0"
        max_cand    = str(agg[1]) if agg else "0"
        conn.commit()

    _emit_materialized(
        governance,
        dataset_id=(
            "derived.signal_candidate_funded_by_nj_contractor_employees"
        ),
        rows_upserted=n_candidates,
        extra={
            "cycle":            cycle,
            "n_candidates":     n_candidates,
            "total_money_usd":  total_money,
            "max_candidate_usd": max_cand,
            "per_bucket":       per_bucket,
        },
    )
    return MaterializeResult(metadata={
        "rows_upserted":     MetadataValue.int(n_candidates),
        "cycle":             MetadataValue.text(cycle),
        "n_candidates":      MetadataValue.int(n_candidates),
        "total_money_usd":   MetadataValue.text(total_money),
        "max_candidate_usd": MetadataValue.text(max_cand),
        "per_bucket":        MetadataValue.json(per_bucket),
    })


# ============================================================================
# DERIVED ASSET: fraud_signal_config (detection-quality registry)
# ============================================================================
#
# Per-signal min_actionable_threshold and signal_family classification
# table introduced in migration 061. This asset is a fingerprint
# asset: the table's contents are a static configuration registry,
# not a refreshing dataset. Materialization confirms the table
# evaluates and records a content fingerprint of its rows so an
# operator's UPDATE (or a future migration's INSERT) propagates to
# downstream views' lineage.
#
# v_entity_fraud_features (L2) INNER-JOINs against this table to
# apply per-signal thresholds and to ARRAY_AGG signal_families for
# the L3a diversity bonus. Hence the L3a v_entity_fraud_risk asset
# now depends on this one.
# ============================================================================


@asset(
    key=AssetKey(["derived", "fraud_signal_config"]),
    description=(
        "TIER 4 v3 detection-quality registry: per-signal "
        "min_actionable_threshold and signal_family classification. "
        "Static configuration table seeded by migration 061 and "
        "tunable by operator UPDATE. v_entity_fraud_features (L2) "
        "INNER-JOINs against this table to filter sub-threshold "
        "matches out of the analyst queue and to expose family tags "
        "for the L3a multi-family diversity bonus. Asset compute is "
        "fingerprint-only -- the table is a registry, not a dataset."
    ),
    group_name="fec",
    deps=[],
    automation_condition=DERIVED_AUTOMATION,
    compute_kind="sql",
)
def derived_fraud_signal_config(
    context: AssetExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> MaterializeResult:
    """Confirm derived.fraud_signal_config evaluates; record fingerprint."""
    with pg.connect() as conn:
        rc, fp = _derived_view_fingerprint(
            conn, view_name="derived.fraud_signal_config",
            fingerprint_query=(
                "SELECT signal_id, signal_family, "
                "       min_actionable_threshold "
                "FROM derived.fraud_signal_config "
                "ORDER BY signal_id"
            ),
        )
    _emit_materialized(governance,
                       dataset_id="derived.fraud_signal_config",
                       rows_upserted=rc,
                       extra={"content_sha256": fp})
    return MaterializeResult(metadata={
        "row_count":      MetadataValue.int(rc),
        "content_sha256": MetadataValue.text(fp),
    })


# ============================================================================
# DERIVED ASSET: signal_candidate_funded_by_excluded_donors
# ============================================================================
#
# TIER 4 v3 / FRAUD-F5d. Candidate-side projection of donor_on_leie:
# rolls flagged-donor contributions through fec_committee.cand_id to
# fec_candidate, surfacing candidates whose campaigns are funded by
# people on the federal exclusion list. Severity=5 CRITICAL.
#
# This asset MUST run after derived.signal_donor_on_leie in the
# same cycle. The dep edge enforces ordering; the SQL function
# header documents the contract.
# ============================================================================


@asset(
    key=AssetKey([
        "derived", "signal_candidate_funded_by_excluded_donors",
    ]),
    description=(
        "TIER 4 v3 / FRAUD-F5d candidate-side projection of "
        "donor_on_leie. Reads the matched-donor set from L1 "
        "(cycle, signal_id='donor_on_leie'), joins through "
        "raw.fec_contribution -> raw.fec_committee -> "
        "raw.fec_candidate, sums positive transaction_amt per "
        "cand_id, percentile-ranks per (cand_office, cand_office_st) "
        "bucket. Severity=5 (CRITICAL). The L3a v_entity_fraud_risk "
        "view picks up the rows automatically. Mirrors 057's shape "
        "byte-for-byte; the only differences are the upstream "
        "matched-set source signal_id and the severity escalation "
        "(LEIE is a federal-exclusion source, not a workforce-"
        "correlation source)."
    ),
    group_name="fec",
    deps=[
        AssetDep(AssetKey(["derived", "signal_donor_on_leie"])),
        AssetDep(AssetKey(["raw", "fec_committee"])),
        AssetDep(AssetKey(["raw", "fec_candidate"])),
        AssetDep(AssetKey(["raw", "fec_contribution"])),
    ],
    automation_condition=DERIVED_AUTOMATION,
    compute_kind="sql",
)
def derived_signal_candidate_funded_by_excluded_donors(
    context: AssetExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> MaterializeResult:
    """Refresh candidate_funded_by_excluded_donors for the cycle.

    Reports per-bucket cardinality and aggregate money-flow as
    metadata. The asset check enforces match-rate plausibility
    bounds programmatically.
    """
    cycle = _fec_default_cycle()
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_signal_candidate_funded_by_excluded_donors(%s)",
            (cycle,),
        )
        row = cur.fetchone()
        n_candidates = int(row[0]) if row else 0

        cur.execute(
            "SELECT peer_bucket, COUNT(*), "
            "       COALESCE(SUM(raw_value), 0)::TEXT, "
            "       COALESCE(MAX(raw_value), 0)::TEXT "
            "FROM derived.fraud_signal_observation "
            "WHERE cycle = %s "
            "  AND signal_id = 'candidate_funded_by_excluded_donors' "
            "GROUP BY peer_bucket "
            "ORDER BY peer_bucket",
            (cycle,),
        )
        per_bucket: dict[str, dict[str, str]] = {
            r[0]: {
                "n_candidates": str(r[1]),
                "total_usd":    str(r[2]),
                "max_usd":      str(r[3]),
            }
            for r in cur.fetchall()
        }

        cur.execute(
            "SELECT COALESCE(SUM(raw_value), 0)::TEXT, "
            "       COALESCE(MAX(raw_value), 0)::TEXT "
            "FROM derived.fraud_signal_observation "
            "WHERE cycle = %s "
            "  AND signal_id = 'candidate_funded_by_excluded_donors'",
            (cycle,),
        )
        agg = cur.fetchone()
        total_money = str(agg[0]) if agg else "0"
        max_cand    = str(agg[1]) if agg else "0"
        conn.commit()

    _emit_materialized(
        governance,
        dataset_id="derived.signal_candidate_funded_by_excluded_donors",
        rows_upserted=n_candidates,
        extra={
            "cycle":             cycle,
            "n_candidates":      n_candidates,
            "total_money_usd":   total_money,
            "max_candidate_usd": max_cand,
            "per_bucket":        per_bucket,
        },
    )
    return MaterializeResult(metadata={
        "rows_upserted":     MetadataValue.int(n_candidates),
        "cycle":             MetadataValue.text(cycle),
        "n_candidates":      MetadataValue.int(n_candidates),
        "total_money_usd":   MetadataValue.text(total_money),
        "max_candidate_usd": MetadataValue.text(max_cand),
        "per_bucket":        MetadataValue.json(per_bucket),
    })


# ============================================================================
# DERIVED ASSET: signal_donor_on_leie
# ============================================================================
#
# TIER 4 v3 / FRAUD-F5c. Joins active raw.fec_contribution donors
# against active LEIE individual exclusions on canonical "LAST|FIRST"
# key. Distinct from signal_entity_on_leie (which matches FEC
# entities -- candidates / treasurers); donor_on_leie matches the
# vastly larger third-party-donor population.
#
# Severity is fixed at 5 (CRITICAL) on every match: a federally-
# excluded individual donating to NJ political campaigns is a
# procurement-influence red flag.
#
# ORDERING CONTRACT
# -----------------
# Deps on raw.fec_contribution and raw.hhs_oig_leie. No dependency
# on other L1 signal assets; the shared L1 table is sliced by
# (cycle, signal_id), so concurrent refreshers writing disjoint
# slices are safe.
# ============================================================================


@asset(
    key=AssetKey(["derived", "signal_donor_on_leie"]),
    description=(
        "TIER 4 v3 / FRAUD-F5c: donor_on_leie cross-source signal. "
        "Joins active raw.fec_contribution donors against active "
        "LEIE individual exclusions on canonical 'LAST|FIRST' key. "
        "entity_kind='donor' (kind added to whitelist in migration "
        "059). Distinct from signal_entity_on_leie which matches "
        "FEC ENTITIES (candidates / treasurers) -- this matches "
        "the donor population. Severity=5 (CRITICAL). "
        "peer_bucket='kind=donor', rate-based percentile."
    ),
    group_name="fec",
    deps=[
        AssetDep(AssetKey(["raw", "fec_contribution"])),
        AssetDep(AssetKey(["raw", "hhs_oig_leie"])),
    ],
    automation_condition=DERIVED_AUTOMATION,
    compute_kind="sql",
)
def derived_signal_donor_on_leie(
    context: AssetExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> MaterializeResult:
    """Refresh donor_on_leie for the cycle.

    Reports population, flagged count, and aggregate dollars from
    excluded donors as metadata. The asset check enforces match-rate
    plausibility bounds programmatically.
    """
    cycle = _fec_default_cycle()
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_signal_donor_on_leie(%s)",
            (cycle,),
        )
        row = cur.fetchone()
        n_matched = int(row[0]) if row else 0

        cur.execute(
            "SELECT COUNT(DISTINCT "
            "  derived.f_canonical_lastfirst_from_fec(name)) "
            "FROM raw.fec_contribution "
            "WHERE cycle = %s "
            "  AND name IS NOT NULL "
            "  AND derived.f_canonical_lastfirst_from_fec(name) "
            "      IS NOT NULL "
            "  AND (memo_cd IS NULL OR memo_cd <> 'X')",
            (cycle,),
        )
        pop_row = cur.fetchone()
        n_in_bucket = int(pop_row[0]) if pop_row else 0

        cur.execute(
            "SELECT COALESCE(SUM(raw_value), 0)::TEXT, "
            "       COALESCE(MAX(raw_value), 0)::TEXT "
            "FROM derived.fraud_signal_observation "
            "WHERE cycle = %s "
            "  AND signal_id = 'donor_on_leie'",
            (cycle,),
        )
        agg = cur.fetchone()
        total_dollars = str(agg[0]) if agg else "0"
        max_donor     = str(agg[1]) if agg else "0"
        conn.commit()

    match_rate = (
        round(n_matched / n_in_bucket, 6)
        if n_in_bucket > 0
        else 0.0
    )

    _emit_materialized(
        governance,
        dataset_id="derived.signal_donor_on_leie",
        rows_upserted=n_matched,
        extra={
            "cycle":             cycle,
            "n_matched":         n_matched,
            "n_in_bucket":       n_in_bucket,
            "match_rate":        match_rate,
            "total_dollars_usd": total_dollars,
            "max_donor_usd":     max_donor,
        },
    )
    return MaterializeResult(metadata={
        "rows_upserted":     MetadataValue.int(n_matched),
        "cycle":             MetadataValue.text(cycle),
        "n_matched":         MetadataValue.int(n_matched),
        "n_in_bucket":       MetadataValue.int(n_in_bucket),
        "match_rate":        MetadataValue.float(match_rate),
        "total_dollars_usd": MetadataValue.text(total_dollars),
        "max_donor_usd":     MetadataValue.text(max_donor),
    })


# ============================================================================
# DERIVED ASSET: signal_donor_on_sam (Tier 4 v3 / FRAUD-F2 donor-side)
# ============================================================================
#
# Migration 065. Joins active raw.fec_contribution donors against
# active SAM.gov individual exclusions on canonical "LAST|FIRST" key.
# Parallel to donor_on_leie (059) but using SAM's broader exclusion
# coverage (DOJ, OFAC, GSA, NIH, NSF, DOE, etc., not just HHS-OIG).
#
# A donor on BOTH SAM and LEIE will fire BOTH signals. That is the
# correct semantic: dual-list inclusion is stronger evidence than
# single-list, and the diversity bonus in fraud_risk_score amplifies
# multi-family entities (sam_bearing + leie_bearing).
#
# Severity=5 (CRITICAL). family=sam_bearing. threshold=$200 (mirrors
# donor_on_leie; below that FEC aggregates the contribution).
#
# Deps on raw.fec_contribution and (load-only) raw.sam_gov_exclusion.
# The SAM substrate has no automated fetcher yet; the operator hand-
# loads via `nj-ingest-sam-exclusions load`.
# ============================================================================


@asset(
    key=AssetKey(["derived", "signal_donor_on_sam"]),
    description=(
        "TIER 4 v3 / FRAUD-F2 donor-side: donor_on_sam cross-source "
        "signal. Joins active raw.fec_contribution donors against "
        "active SAM.gov individual exclusions on canonical "
        "'LAST|FIRST' key. Parallel to donor_on_leie (059) but using "
        "SAM's broader exclusion coverage (every federal excluding "
        "agency, not just HHS-OIG). entity_kind='donor' (kind in "
        "whitelist as of 059). Severity=5 (CRITICAL). "
        "signal_family='sam_bearing'; same-person dual fire with "
        "donor_on_leie earns the multi-family diversity bonus. "
        "peer_bucket='kind=donor', rate-based percentile."
    ),
    group_name="fec",
    deps=[
        AssetDep(AssetKey(["raw", "fec_contribution"])),
        # raw.sam_gov_exclusion has no automated fetcher; hand-loaded
        # via `nj-ingest-sam-exclusions load`. No asset dep until
        # automation lands.
    ],
    automation_condition=DERIVED_AUTOMATION,
    compute_kind="sql",
)
def derived_signal_donor_on_sam(
    context: AssetExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> MaterializeResult:
    """Refresh donor_on_sam for the cycle.

    Reports population, flagged count, and aggregate decayed dollars
    from SAM-excluded donors as metadata. The asset check enforces
    match-rate plausibility bounds programmatically.
    """
    cycle = _fec_default_cycle()
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_signal_donor_on_sam(%s)",
            (cycle,),
        )
        row = cur.fetchone()
        n_matched = int(row[0]) if row else 0

        cur.execute(
            "SELECT COUNT(DISTINCT "
            "  derived.f_canonical_lastfirst_from_fec(name)) "
            "FROM raw.fec_contribution "
            "WHERE cycle = %s "
            "  AND name IS NOT NULL "
            "  AND derived.f_canonical_lastfirst_from_fec(name) "
            "      IS NOT NULL "
            "  AND (memo_cd IS NULL OR memo_cd <> 'X')",
            (cycle,),
        )
        pop_row = cur.fetchone()
        n_in_bucket = int(pop_row[0]) if pop_row else 0

        cur.execute(
            "SELECT COALESCE(SUM(raw_value), 0)::TEXT, "
            "       COALESCE(MAX(raw_value), 0)::TEXT "
            "FROM derived.fraud_signal_observation "
            "WHERE cycle = %s "
            "  AND signal_id = 'donor_on_sam'",
            (cycle,),
        )
        agg = cur.fetchone()
        total_dollars = str(agg[0]) if agg else "0"
        max_donor     = str(agg[1]) if agg else "0"
        conn.commit()

    match_rate = (
        round(n_matched / n_in_bucket, 6)
        if n_in_bucket > 0
        else 0.0
    )

    _emit_materialized(
        governance,
        dataset_id="derived.signal_donor_on_sam",
        rows_upserted=n_matched,
        extra={
            "cycle":             cycle,
            "n_matched":         n_matched,
            "n_in_bucket":       n_in_bucket,
            "match_rate":        match_rate,
            "total_dollars_usd": total_dollars,
            "max_donor_usd":     max_donor,
        },
    )
    return MaterializeResult(metadata={
        "rows_upserted":     MetadataValue.int(n_matched),
        "cycle":             MetadataValue.text(cycle),
        "n_matched":         MetadataValue.int(n_matched),
        "n_in_bucket":       MetadataValue.int(n_in_bucket),
        "match_rate":        MetadataValue.float(match_rate),
        "total_dollars_usd": MetadataValue.text(total_dollars),
        "max_donor_usd":     MetadataValue.text(max_donor),
    })


# ============================================================================
# DERIVED ASSET: signal_candidate_funded_by_sam_excluded_donors
# ============================================================================
#
# TIER 4 v3 / FRAUD-F2 candidate-side projection of donor_on_sam
# (migration 066). Reads the matched-donor set from L1 (cycle,
# signal_id='donor_on_sam'), joins through raw.fec_contribution ->
# raw.fec_committee -> raw.fec_candidate, sums per-contribution
# (transaction_amt * f_leie_age_decay(sam_active_date)) per cand_id,
# percentile-ranks per (cand_office, cand_office_st) bucket.
#
# ORDERING CONTRACT
# -----------------
# MUST run after derived/signal_donor_on_sam materializes (it reads
# that signal's L1 rows). The dep edge enforces the order in the
# Dagster DAG; the SQL refresher header documents the contract for
# direct-SQL readers.
# ============================================================================


@asset(
    key=AssetKey([
        "derived",
        "signal_candidate_funded_by_sam_excluded_donors",
    ]),
    description=(
        "TIER 4 v3 / FRAUD-F2 candidate-side projection of "
        "donor_on_sam. Reads the matched-donor set from L1 "
        "(cycle, signal_id='donor_on_sam'), joins through "
        "raw.fec_contribution -> raw.fec_committee -> "
        "raw.fec_candidate, sums per-contribution "
        "(transaction_amt * f_leie_age_decay(sam_active_date)) per "
        "cand_id, percentile-ranks per (cand_office, cand_office_st) "
        "bucket. Severity=5 (CRITICAL). signal_family='sam_bearing'. "
        "The L3a v_entity_fraud_risk view picks up the rows "
        "automatically. Mirrors candidate_funded_by_excluded_donors "
        "(060/062) byte-for-byte except for upstream signal_id and "
        "decay weight source (sam_active_date vs leie_excldate)."
    ),
    group_name="fec",
    deps=[
        AssetDep(AssetKey(["derived", "signal_donor_on_sam"])),
        AssetDep(AssetKey(["raw", "fec_committee"])),
        AssetDep(AssetKey(["raw", "fec_candidate"])),
        AssetDep(AssetKey(["raw", "fec_contribution"])),
    ],
    automation_condition=DERIVED_AUTOMATION,
    compute_kind="sql",
)
def derived_signal_candidate_funded_by_sam_excluded_donors(
    context: AssetExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> MaterializeResult:
    """Refresh candidate_funded_by_sam_excluded_donors for the cycle.

    Reports per-bucket cardinality and aggregate (decayed) money-flow
    as metadata. The asset check enforces match-rate plausibility
    bounds programmatically.
    """
    cycle = _fec_default_cycle()
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_signal_candidate_funded_by_sam_excluded_donors(%s)",
            (cycle,),
        )
        row = cur.fetchone()
        n_candidates = int(row[0]) if row else 0

        cur.execute(
            "SELECT peer_bucket, COUNT(*), "
            "       COALESCE(SUM(raw_value), 0)::TEXT, "
            "       COALESCE(MAX(raw_value), 0)::TEXT "
            "FROM derived.fraud_signal_observation "
            "WHERE cycle = %s "
            "  AND signal_id = 'candidate_funded_by_sam_excluded_donors' "
            "GROUP BY peer_bucket "
            "ORDER BY peer_bucket",
            (cycle,),
        )
        per_bucket: dict[str, dict[str, str]] = {
            r[0]: {
                "n_candidates": str(r[1]),
                "total_usd":    str(r[2]),
                "max_usd":      str(r[3]),
            }
            for r in cur.fetchall()
        }

        cur.execute(
            "SELECT COALESCE(SUM(raw_value), 0)::TEXT, "
            "       COALESCE(MAX(raw_value), 0)::TEXT "
            "FROM derived.fraud_signal_observation "
            "WHERE cycle = %s "
            "  AND signal_id = 'candidate_funded_by_sam_excluded_donors'",
            (cycle,),
        )
        agg = cur.fetchone()
        total_money = str(agg[0]) if agg else "0"
        max_cand    = str(agg[1]) if agg else "0"
        conn.commit()

    _emit_materialized(
        governance,
        dataset_id=(
            "derived.signal_candidate_funded_by_sam_excluded_donors"
        ),
        rows_upserted=n_candidates,
        extra={
            "cycle":             cycle,
            "n_candidates":      n_candidates,
            "total_money_usd":   total_money,
            "max_candidate_usd": max_cand,
            "per_bucket":        per_bucket,
        },
    )
    return MaterializeResult(metadata={
        "rows_upserted":     MetadataValue.int(n_candidates),
        "cycle":             MetadataValue.text(cycle),
        "n_candidates":      MetadataValue.int(n_candidates),
        "total_money_usd":   MetadataValue.text(total_money),
        "max_candidate_usd": MetadataValue.text(max_cand),
        "per_bucket":        MetadataValue.json(per_bucket),
    })


# ============================================================================
# DERIVED ASSET: signal_entity_funded_and_excluded
# ============================================================================
#
# TIER 4 v3 / FRAUD-F1 + F5 INTERSECTION cross-source signal.
# Joins active USAspending NJ-pop recipients (people, parsed via the
# canonical "LAST, FIRST" recognizer) against active LEIE individual
# exclusions on canonical "LAST|FIRST" key.
#
# WHY THIS IS A FIRST-CLASS DAGSTER ASSET (not just a SQL function call)
# --------------------------------------------------------------------
# 1. It depends on TWO substrates from disjoint federal authorities
#    (USAspending = Treasury / federal procurement; LEIE = HHS-OIG /
#    federal exclusions). Dagster's DAG is the only place the cross-
#    source dependency can be enforced -- the SQL side has no
#    visibility into refresh ordering of raw tables.
# 2. Steady-state expected count is ZERO (a well-functioning federal
#    procurement system never pays an excluded individual). The
#    asset check intentionally does NOT fire on zero matches; the
#    PURPOSE of the platform is to surface deviations.
# 3. Severity is fixed at 5 (CRITICAL) on every match. There is no
#    soft-scoring layer between this signal and the analyst queue.
#
# ORDERING CONTRACT
# -----------------
# This asset declares deps on raw.usaspending_award and raw.hhs_oig_leie
# only (NOT on any other L1 signal asset). The shared
# derived.fraud_signal_observation table is sliced by (cycle,
# signal_id), so concurrent refreshers writing to different slices
# are safe.
# ============================================================================


@asset(
    key=AssetKey(["derived", "signal_entity_funded_and_excluded"]),
    description=(
        "TIER 4 v3 / FRAUD-F1 + F5 intersection: "
        "entity_funded_and_excluded cross-source signal. Joins active "
        "USAspending NJ-pop recipients (people only, via "
        "derived.f_canonical_lastfirst_from_fec) against active "
        "LEIE individual exclusions on canonical 'LAST|FIRST' key. "
        "Severity=5 (CRITICAL). entity_kind='contractor' (a kind "
        "added to the whitelist in migration 058). "
        "Steady-state expected count is ZERO; non-empty rows are "
        "always investigation-worthy. peer_bucket='kind=contractor', "
        "rate-based percentile."
    ),
    group_name="fec",
    deps=[
        AssetDep(AssetKey(["raw", "usaspending_award"])),
        AssetDep(AssetKey(["raw", "hhs_oig_leie"])),
    ],
    automation_condition=DERIVED_AUTOMATION,
    compute_kind="sql",
)
def derived_signal_entity_funded_and_excluded(
    context: AssetExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> MaterializeResult:
    """Refresh entity_funded_and_excluded for the cycle.

    Reports population, flagged count, and aggregate dollars-at-risk
    as metadata. The asset check enforces match-rate plausibility
    bounds programmatically.
    """
    cycle = _fec_default_cycle()
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_signal_entity_funded_and_excluded(%s)",
            (cycle,),
        )
        row = cur.fetchone()
        n_matched = int(row[0]) if row else 0

        cur.execute(
            "SELECT COUNT(DISTINCT recipient_canonical_individual) "
            "FROM derived.v_usaspending_award_active "
            "WHERE recipient_canonical_individual IS NOT NULL"
        )
        pop_row = cur.fetchone()
        n_in_bucket = int(pop_row[0]) if pop_row else 0

        cur.execute(
            "SELECT COALESCE(SUM(raw_value), 0)::TEXT, "
            "       COALESCE(MAX(raw_value), 0)::TEXT "
            "FROM derived.fraud_signal_observation "
            "WHERE cycle = %s "
            "  AND signal_id = 'entity_funded_and_excluded'",
            (cycle,),
        )
        agg = cur.fetchone()
        total_dollars = str(agg[0]) if agg else "0"
        max_person    = str(agg[1]) if agg else "0"
        conn.commit()

    match_rate = (
        round(n_matched / n_in_bucket, 6)
        if n_in_bucket > 0
        else 0.0
    )

    _emit_materialized(
        governance,
        dataset_id="derived.signal_entity_funded_and_excluded",
        rows_upserted=n_matched,
        extra={
            "cycle":             cycle,
            "n_matched":         n_matched,
            "n_in_bucket":       n_in_bucket,
            "match_rate":        match_rate,
            "total_dollars_usd": total_dollars,
            "max_person_usd":    max_person,
        },
    )
    return MaterializeResult(metadata={
        "rows_upserted":     MetadataValue.int(n_matched),
        "cycle":             MetadataValue.text(cycle),
        "n_matched":         MetadataValue.int(n_matched),
        "n_in_bucket":       MetadataValue.int(n_in_bucket),
        "match_rate":        MetadataValue.float(match_rate),
        "total_dollars_usd": MetadataValue.text(total_dollars),
        "max_person_usd":    MetadataValue.text(max_person),
    })


# ============================================================================
# FRAUD-F2 (signal layer): entity_excluded_via_sam_uei (Tier 4 v3)
# ============================================================================
# UEI-deterministic match between USAspending NJ-pop active recipients
# and SAM.gov-excluded UEIs (migration 064). Parallel to / broader than
# entity_funded_and_excluded (058) which uses LEIE individual-name
# canonicalization. UEI = UEI is unique by SAM design -- false-positive
# risk is ~zero. severity=5, threshold=$0, family=sam_bearing.
#
# Steady-state expected count is ZERO; non-empty rows are
# investigation-worthy (FAR 9.405 violation).
#
# Dep declared on raw.usaspending_award AND raw.sam_gov_exclusion. The
# SAM substrate is currently load-only (no Dagster asset on
# raw.sam_gov_exclusion -- the operator hand-loads via
# `nj-ingest-sam-exclusions load`). When that loader becomes
# automated and gets its own asset, this dep will pick it up.
# ============================================================================


@asset(
    key=AssetKey(["derived", "signal_entity_excluded_via_sam_uei"]),
    description=(
        "TIER 4 v3 / FRAUD-F2 (signal layer): "
        "entity_excluded_via_sam_uei UEI-deterministic cross-source "
        "signal. Joins active USAspending NJ-pop recipients "
        "(recipient_uei) against active SAM.gov exclusions (sam_uei). "
        "Severity=5 (CRITICAL). entity_kind='contractor', "
        "entity_id=12-char UEI. signal_family='sam_bearing'. "
        "Steady-state expected count is ZERO; non-empty rows are "
        "always investigation-worthy. peer_bucket='kind=contractor_uei', "
        "rate-based percentile. Per-award amount weighted by "
        "f_leie_age_decay(active_date) so a stale exclusion paired "
        "with a recent award is correctly de-emphasized."
    ),
    group_name="fec",
    deps=[
        AssetDep(AssetKey(["raw", "usaspending_award"])),
        # SAM exclusion substrate: hand-loaded for now (migration 063 +
        # ingestion.sam_gov_exclusions). No asset on the raw table yet
        # because no automated fetch path is shipped.
    ],
    automation_condition=DERIVED_AUTOMATION,
    compute_kind="sql",
)
def derived_signal_entity_excluded_via_sam_uei(
    context: AssetExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> MaterializeResult:
    """Refresh entity_excluded_via_sam_uei for the cycle.

    Reports population (distinct UEIs in active USAspending), flagged
    count (UEIs that match a SAM exclusion), and aggregate dollars-at-
    risk (decayed sum of award amounts).
    """
    cycle = _fec_default_cycle()
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_signal_entity_excluded_via_sam_uei(%s)",
            (cycle,),
        )
        row = cur.fetchone()
        n_matched = int(row[0]) if row else 0

        cur.execute(
            "SELECT COUNT(DISTINCT recipient_uei) "
            "FROM derived.v_usaspending_award_active "
            "WHERE recipient_uei IS NOT NULL"
        )
        pop_row = cur.fetchone()
        n_in_bucket = int(pop_row[0]) if pop_row else 0

        cur.execute(
            "SELECT COALESCE(SUM(raw_value), 0)::TEXT, "
            "       COALESCE(MAX(raw_value), 0)::TEXT "
            "FROM derived.fraud_signal_observation "
            "WHERE cycle = %s "
            "  AND signal_id = 'entity_excluded_via_sam_uei'",
            (cycle,),
        )
        agg = cur.fetchone()
        total_dollars = str(agg[0]) if agg else "0"
        max_uei       = str(agg[1]) if agg else "0"
        conn.commit()

    match_rate = (
        round(n_matched / n_in_bucket, 6)
        if n_in_bucket > 0
        else 0.0
    )

    _emit_materialized(
        governance,
        dataset_id="derived.signal_entity_excluded_via_sam_uei",
        rows_upserted=n_matched,
        extra={
            "cycle":             cycle,
            "n_matched":         n_matched,
            "n_in_bucket":       n_in_bucket,
            "match_rate":        match_rate,
            "total_dollars_usd": total_dollars,
            "max_uei_usd":       max_uei,
        },
    )
    return MaterializeResult(metadata={
        "rows_upserted":     MetadataValue.int(n_matched),
        "cycle":             MetadataValue.text(cycle),
        "n_matched":         MetadataValue.int(n_matched),
        "n_in_bucket":       MetadataValue.int(n_in_bucket),
        "match_rate":        MetadataValue.float(match_rate),
        "total_dollars_usd": MetadataValue.text(total_dollars),
        "max_uei_usd":       MetadataValue.text(max_uei),
    })


@asset(
    key=AssetKey(["derived", "v_entity_fraud_risk"]),
    description=(
        "TIER 4 v3 L3a read surface: per-entity feature vector + composite "
        "risk_score in [0, 100]. View on top of derived.v_entity_fraud_features "
        "(L2) with derived.fraud_risk_score(severities, peer_percentiles) "
        "appended. Sort DESC for the analyst queue. Asset compute is "
        "fingerprint-only -- the view is virtual; this asset confirms the "
        "view evaluates and records a content fingerprint of "
        "(entity_kind, entity_id, risk_score) so an upstream regression "
        "(e.g. a refresher dropped a signal, the score formula drifted) "
        "produces a different fingerprint and surfaces in governance."
    ),
    group_name="fec",
    deps=[
        AssetDep(AssetKey(["derived", "fraud_signal_observation"])),
        AssetDep(AssetKey(["derived", "signal_entity_on_leie"])),
        AssetDep(AssetKey(["derived", "signal_donor_employed_by_nj_contractor"])),
        AssetDep(AssetKey([
            "derived",
            "signal_candidate_funded_by_nj_contractor_employees",
        ])),
        AssetDep(AssetKey(["derived", "signal_entity_funded_and_excluded"])),
        AssetDep(AssetKey(["derived", "signal_donor_on_leie"])),
        AssetDep(AssetKey([
            "derived",
            "signal_candidate_funded_by_excluded_donors",
        ])),
        AssetDep(AssetKey(["derived", "signal_entity_excluded_via_sam_uei"])),
        AssetDep(AssetKey(["derived", "signal_donor_on_sam"])),
        AssetDep(AssetKey([
            "derived",
            "signal_candidate_funded_by_sam_excluded_donors",
        ])),
        # Detection-quality registry (migration 061): the L2 view
        # joins this for per-signal thresholds and signal_family
        # tags; a config edit must propagate to L3a fingerprint.
        AssetDep(AssetKey(["derived", "fraud_signal_config"])),
    ],
    automation_condition=DERIVED_AUTOMATION,
    compute_kind="sql",
)
def derived_v_entity_fraud_risk(
    context: AssetExecutionContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> MaterializeResult:
    """Confirm derived.v_entity_fraud_risk evaluates; record fingerprint."""
    with pg.connect() as conn:
        rc, fp = _derived_view_fingerprint(
            conn, view_name="derived.v_entity_fraud_risk",
            fingerprint_query=(
                "SELECT cycle, entity_kind, entity_id, "
                "       ROUND(risk_score::numeric, 2) "
                "FROM derived.v_entity_fraud_risk "
                "ORDER BY cycle, entity_kind, entity_id"
            ),
        )
    _emit_materialized(governance, dataset_id="derived.v_entity_fraud_risk",
                       rows_upserted=rc, extra={"content_sha256": fp})
    return MaterializeResult(metadata={
        "row_count":      MetadataValue.int(rc),
        "content_sha256": MetadataValue.text(fp),
    })


ALL_ASSETS = [
    # raw
    raw_fred_observation,
    raw_cpi_u,
    raw_fhfa_hpi_county,
    raw_zillow_zhvi_county,
    raw_acs_median_household_income,
    raw_acs_housing,
    raw_lca_disclosure,
    raw_nj_property_tax_county,
    raw_acs_pums_person,
    raw_acs_pums_housing,
    # raw -- FEC (Tier 4 v1)
    raw_fec_candidate,
    raw_fec_committee,
    raw_fec_contribution,
    # raw -- HHS-OIG LEIE (Tier 4 v3 / FRAUD-F5 substrate)
    raw_hhs_oig_leie,
    # raw -- USAspending federal awards (Tier 4 v3 / FRAUD-F1 substrate)
    raw_usaspending_award,
    # derived
    derived_fred_annual,
    derived_f_acs_mhi_real,
    derived_fhfa_hpi_indexed_2000,
    derived_housing_burden_ratio,
    derived_pums_burden_segmented,
    derived_pums_burden_county_segmented,
    # derived -- FEC fraud-risk surface (Tier 4 v3)
    derived_fraud_signal_observation,
    derived_signal_entity_on_leie,
    derived_signal_donor_employed_by_nj_contractor,
    derived_signal_candidate_funded_by_nj_contractor_employees,
    derived_signal_entity_funded_and_excluded,
    derived_signal_donor_on_leie,
    derived_signal_candidate_funded_by_excluded_donors,
    # FRAUD-F2 (migration 064): UEI-deterministic SAM x USAspending match
    derived_signal_entity_excluded_via_sam_uei,
    # FRAUD-F2 donor-side (migration 065): SAM-excluded individuals donating
    derived_signal_donor_on_sam,
    # FRAUD-F2 candidate-side projection (migration 066): the candidate whose
    # campaign received money from SAM-excluded donors
    derived_signal_candidate_funded_by_sam_excluded_donors,
    # Detection-quality registry (migration 061)
    derived_fraud_signal_config,
    derived_v_entity_fraud_risk,
]
