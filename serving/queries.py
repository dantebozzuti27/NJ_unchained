"""SQL queries that back the serving API.

One function per endpoint. Functions accept a psycopg connection and
return typed results (or raise a documented exception). Routes do NOT
build SQL strings; they call these functions.

Why this lives in its own module
--------------------------------
Separating queries from routes makes both easier to test:

* Query functions can be tested directly against a Postgres test
  fixture without going through FastAPI.
* Route handlers can be tested with mocked query functions if the
  query touches an unreliable source.

Why we do NOT use an ORM (SQLAlchemy / Tortoise / etc.)
-------------------------------------------------------
The schema is owned by SQL migrations, not Python. ORMs that try to
model the schema in Python become a second source of truth that
drifts. Plain psycopg + parameterized queries keeps the SQL the only
source of truth, which is the right call for this codebase.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any, Final

from psycopg.rows import dict_row

if TYPE_CHECKING:
    import psycopg


# ============================================================================
# /health
# ============================================================================


def select_one(conn: psycopg.Connection) -> int:
    """Liveness probe: SELECT 1.

    Used by /health to confirm the pool can talk to Postgres. Wrapped
    in a function (rather than inline in the route) so we can mock
    failure modes in tests.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("SELECT 1 returned no row (impossible)")
        return int(row[0])


def count_recent_errors(conn: psycopg.Connection, *, hours: int) -> int:
    """Count error/fatal signals raised in the last *hours* hours."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM governance.dataset_health "
            "WHERE severity IN ('error', 'fatal') "
            "  AND observed_at >= now() - make_interval(hours => %s)",
            (hours,),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0


# ============================================================================
# /releases
# ============================================================================


def list_release_calendar(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """All ref.release_calendar rows ordered by source_id."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT source_id, cadence, schedule_label, timezone, "
            "       expected_lag_hours, notes "
            "FROM ref.release_calendar "
            "ORDER BY source_id"
        )
        return list(cur.fetchall())


def list_release_calendar_detailed(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Full calendar rows + last materialization for schedule computation.

    Includes structured columns (day-of-week, day-of-month, etc.) used by
    :mod:`serving.release_schedule` to derive upcoming release instants.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT
                rc.source_id,
                rc.cadence,
                rc.schedule_label,
                rc.day_of_week,
                rc.day_of_month,
                rc.month_of_year,
                rc.time_of_day_local,
                rc.timezone,
                rc.expected_lag_hours,
                rc.notes,
                mat.last_materialized_at
            FROM ref.release_calendar rc
            LEFT JOIN governance.v_latest_materialization mat
                ON mat.dataset_id = rc.source_id
            ORDER BY rc.source_id
            """
        )
        return list(cur.fetchall())


# ============================================================================
# /assets
# ============================================================================


def list_assets(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """All known datasets joined with calendar + last materialization + 30d health.

    A dataset appears in the result if EITHER:
      * It has a ref.release_calendar entry, OR
      * It has at least one governance.dataset_health row.

    We FULL OUTER JOIN to surface both sides; this is intentional --
    a dataset with a calendar but no materialization yet is just as
    interesting (it shows up as freshness_state='unknown') as a
    dataset with materializations but no calendar (which indicates
    a calendar gap that operators should fix).
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT
                COALESCE(rc.source_id, mat.dataset_id) AS dataset_id,
                rc.cadence,
                rc.schedule_label,
                rc.expected_lag_hours,
                mat.last_materialized_at,
                mat.rows_upserted                       AS last_rows_upserted,
                COALESCE(hs.n_warn_30d,  0)             AS n_warn_30d,
                COALESCE(hs.n_error_30d, 0)             AS n_error_30d
            FROM ref.release_calendar                       rc
            FULL JOIN governance.v_latest_materialization   mat
                ON mat.dataset_id = rc.source_id
            LEFT JOIN governance.v_dataset_health_summary   hs
                ON hs.dataset_id  = COALESCE(rc.source_id, mat.dataset_id)
            ORDER BY 1
            """,
        )
        return list(cur.fetchall())


def get_asset_detail(
    conn: psycopg.Connection, *, dataset_id: str,
) -> dict[str, Any] | None:
    """One asset row + last materialization details payload.

    Returns None when the dataset has no calendar entry AND no
    materialization signal. The route maps None to a 404.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT
                COALESCE(rc.source_id, mat.dataset_id) AS dataset_id,
                rc.cadence,
                rc.schedule_label,
                rc.expected_lag_hours,
                mat.last_materialized_at,
                mat.rows_upserted                       AS last_rows_upserted,
                mat.details                             AS last_materialization_details,
                COALESCE(hs.n_warn_30d,  0)             AS n_warn_30d,
                COALESCE(hs.n_error_30d, 0)             AS n_error_30d
            FROM ref.release_calendar                       rc
            FULL JOIN governance.v_latest_materialization   mat
                ON mat.dataset_id = rc.source_id
            LEFT JOIN governance.v_dataset_health_summary   hs
                ON hs.dataset_id  = COALESCE(rc.source_id, mat.dataset_id)
            WHERE COALESCE(rc.source_id, mat.dataset_id) = %s
            """,
            (dataset_id,),
        )
        return cur.fetchone()


# ============================================================================
# /burden
# ============================================================================


# Single source of truth for the column projection used by /burden
# endpoints. Centralized so adding a new column to derived.housing_burden_ratio
# is a one-line change here, not a hunt across two query functions.
_BURDEN_SELECT = """
    b.county_fips,
    c.name                                     AS county_name,
    b.year,
    b.household_income,
    b.median_gross_rent,
    b.median_owner_cost_w_mortgage             AS median_owner_cost_w_mtg,
    b.renter_burden_ratio,
    b.owner_burden_w_mtg_ratio,
    b.owner_burden_no_mtg_ratio,
    b.blended_burden_ratio,
    b.property_tax_amount_avg,
    b.property_tax_effective_rate_pct,
    b.property_tax_share_of_income,
    b.property_tax_share_of_owner_cost_w_mtg
"""


def list_burden_for_county(
    conn: psycopg.Connection, *, county_fips: str,
) -> list[dict[str, Any]]:
    """List housing burden time series for one NJ county (ACS 5-yr)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT {_BURDEN_SELECT}
            FROM derived.housing_burden_ratio b
            JOIN ref.county                   c ON c.county_fips = b.county_fips
            WHERE b.county_fips = %s
              AND b.product     = 'acs5'
            ORDER BY b.year
            """,
            (county_fips,),
        )
        return list(cur.fetchall())


def list_burden_latest_year_nj(
    conn: psycopg.Connection,
) -> list[dict[str, Any]]:
    """Latest year, all 21 NJ counties (the dashboard query).

    Returns up to 21 rows. If a county is missing for the latest year
    (the ACS sometimes suppresses small populations), it simply does
    not appear; clients should treat 'absent' as 'not yet published',
    not 'zero'.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            WITH latest AS (
                SELECT MAX(year) AS year
                FROM derived.housing_burden_ratio b
                JOIN ref.county c ON c.county_fips = b.county_fips
                WHERE c.state_code = 'NJ' AND b.product = 'acs5'
            )
            SELECT {_BURDEN_SELECT}
            FROM derived.housing_burden_ratio   b
            JOIN ref.county                     c ON c.county_fips = b.county_fips
            JOIN latest                          ON b.year = latest.year
            WHERE c.state_code = 'NJ' AND b.product = 'acs5'
            ORDER BY c.name
            """,
        )
        return list(cur.fetchall())


# ============================================================================
# /pums-burden
# ============================================================================
#
# Two query shapes:
#   * list_pums_burden_latest(): all NJ PUMAs, latest published vintage.
#   * list_pums_burden_for_puma(): one PUMA, latest published vintage.
#
# Both accept optional dim/tenure filters and an include_suppressed
# toggle. The "latest vintage" subquery is shared so both endpoints
# return data from the same year.
# ============================================================================


_PUMS_BURDEN_SELECT = """
    year, product, state_fips, puma,
    tenure_class, segment_dim, segment_value,
    weighted_n, sample_n,
    household_income_p50, household_income_p50_se,
    monthly_cost_p50,     monthly_cost_p50_se,
    burden_ratio_p50,     burden_ratio_p50_se,
    suppressed
"""


# ----------------------------------------------------------------------------
# Latest-vintage CTE for a *given* product. The product is supplied at
# bind time (not interpolated) to keep this both safe and reusable. The
# CTE returns exactly one (year, product) row: the most recent year for
# that product. This lets the API expose a `?product=` filter while the
# read path stays a simple JOIN.
# ----------------------------------------------------------------------------
_PUMS_BURDEN_LATEST_FOR_PRODUCT_CTE = """
WITH latest AS (
    SELECT MAX(year) AS year, %s::text AS product
    FROM derived.pums_burden_segmented
    WHERE state_fips = '34' AND product = %s
)
"""

# Default product when the API caller omits ?product=. acs5 is preferred
# because the larger sample yields fewer suppressed cells -- the platform's
# headline answer to "are Hispanic renters in Bergen burdened?" is best
# served by the lower-variance, less-suppressed 5-year vintage. Callers
# wanting fresher data can pass ?product=acs1 explicitly.
DEFAULT_PUMS_PRODUCT: Final[str] = "acs5"


def list_pums_burden_latest(
    conn: psycopg.Connection,
    *,
    dim: str | None = None,
    tenure: str | None = None,
    include_suppressed: bool = False,
    product: str = DEFAULT_PUMS_PRODUCT,
) -> list[dict[str, Any]]:
    """List PUMS-derived burden cells across all NJ PUMAs (latest vintage of *product*)."""
    where_clauses = ["b.state_fips = '34'"]
    params: list[Any] = [product, product]  # for CTE
    if dim is not None:
        where_clauses.append("b.segment_dim = %s")
        params.append(dim)
    if tenure is not None:
        where_clauses.append("b.tenure_class = %s")
        params.append(tenure)
    if not include_suppressed:
        where_clauses.append("NOT b.suppressed")
    where_sql = " AND ".join(where_clauses)

    query = f"""
        {_PUMS_BURDEN_LATEST_FOR_PRODUCT_CTE}
        SELECT {_PUMS_BURDEN_SELECT}
        FROM derived.pums_burden_segmented b
        JOIN latest USING (year, product)
        WHERE {where_sql}
        ORDER BY b.puma, b.tenure_class, b.segment_dim, b.segment_value
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        return list(cur.fetchall())


def list_pums_burden_for_puma(
    conn: psycopg.Connection,
    *,
    puma: str,
    dim: str | None = None,
    tenure: str | None = None,
    include_suppressed: bool = False,
    product: str = DEFAULT_PUMS_PRODUCT,
) -> list[dict[str, Any]]:
    """List PUMS-derived burden cells for one PUMA (latest vintage of *product*)."""
    where_clauses = ["b.state_fips = '34'", "b.puma = %s"]
    params: list[Any] = [product, product, puma]
    if dim is not None:
        where_clauses.append("b.segment_dim = %s")
        params.append(dim)
    if tenure is not None:
        where_clauses.append("b.tenure_class = %s")
        params.append(tenure)
    if not include_suppressed:
        where_clauses.append("NOT b.suppressed")
    where_sql = " AND ".join(where_clauses)

    query = f"""
        {_PUMS_BURDEN_LATEST_FOR_PRODUCT_CTE}
        SELECT {_PUMS_BURDEN_SELECT}
        FROM derived.pums_burden_segmented b
        JOIN latest USING (year, product)
        WHERE {where_sql}
        ORDER BY b.tenure_class, b.segment_dim, b.segment_value
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        return list(cur.fetchall())


# ============================================================================
# /pums-burden-county
# ============================================================================
#
# Mirror of /pums-burden (PUMA-grain) but at COUNTY grain. Backed by the
# materialized derived.pums_burden_county_segmented (re-aggregated from
# raw PUMS via the population-weighted PUMA-county crosswalk; NOT a
# roll-up of the PUMA-grain table).
# ============================================================================


_PUMS_BURDEN_COUNTY_SELECT = """
    b.year, b.product, b.state_fips, b.county_fips,
    c.name                                         AS county_name,
    b.tenure_class, b.segment_dim, b.segment_value,
    b.weighted_n, b.sample_n,
    b.household_income_p50, b.household_income_p50_se,
    b.monthly_cost_p50,     b.monthly_cost_p50_se,
    b.burden_ratio_p50,     b.burden_ratio_p50_se,
    b.suppressed,
    b.n_pumas_contributing
"""


_PUMS_BURDEN_COUNTY_LATEST_FOR_PRODUCT_CTE = """
WITH latest AS (
    SELECT MAX(year) AS year, %s::text AS product
    FROM derived.pums_burden_county_segmented
    WHERE state_fips = '34' AND product = %s
)
"""


def list_pums_burden_county_latest(
    conn: psycopg.Connection,
    *,
    dim: str | None = None,
    tenure: str | None = None,
    include_suppressed: bool = False,
    product: str = DEFAULT_PUMS_PRODUCT,
) -> list[dict[str, Any]]:
    """List county-grain PUMS burden cells across all NJ counties (latest vintage of *product*)."""
    where_clauses = ["b.state_fips = '34'"]
    params: list[Any] = [product, product]
    if dim is not None:
        where_clauses.append("b.segment_dim = %s")
        params.append(dim)
    if tenure is not None:
        where_clauses.append("b.tenure_class = %s")
        params.append(tenure)
    if not include_suppressed:
        where_clauses.append("NOT b.suppressed")
    where_sql = " AND ".join(where_clauses)

    query = f"""
        {_PUMS_BURDEN_COUNTY_LATEST_FOR_PRODUCT_CTE}
        SELECT {_PUMS_BURDEN_COUNTY_SELECT}
        FROM derived.pums_burden_county_segmented b
        JOIN latest                           USING (year, product)
        JOIN ref.county                       c ON c.county_fips = b.county_fips
        WHERE {where_sql}
        ORDER BY c.name, b.tenure_class, b.segment_dim, b.segment_value
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        return list(cur.fetchall())


def list_pums_burden_county_for_county(
    conn: psycopg.Connection,
    *,
    county_fips: str,
    dim: str | None = None,
    tenure: str | None = None,
    include_suppressed: bool = False,
    product: str = DEFAULT_PUMS_PRODUCT,
) -> list[dict[str, Any]]:
    """List county-grain PUMS burden cells for one NJ county (latest vintage of *product*)."""
    where_clauses = ["b.state_fips = '34'", "b.county_fips = %s"]
    params: list[Any] = [product, product, county_fips]
    if dim is not None:
        where_clauses.append("b.segment_dim = %s")
        params.append(dim)
    if tenure is not None:
        where_clauses.append("b.tenure_class = %s")
        params.append(tenure)
    if not include_suppressed:
        where_clauses.append("NOT b.suppressed")
    where_sql = " AND ".join(where_clauses)

    query = f"""
        {_PUMS_BURDEN_COUNTY_LATEST_FOR_PRODUCT_CTE}
        SELECT {_PUMS_BURDEN_COUNTY_SELECT}
        FROM derived.pums_burden_county_segmented b
        JOIN latest                           USING (year, product)
        JOIN ref.county                       c ON c.county_fips = b.county_fips
        WHERE {where_sql}
        ORDER BY b.tenure_class, b.segment_dim, b.segment_value
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        return list(cur.fetchall())


# ============================================================================
# /pums-burden-county-series
# ============================================================================
#
# Multi-year time-series read surface for the UI's "Trend" view. Returns
# one row per (year, product, county_fips, tenure_class) for the
# segment_dim='overall' rollup. Distinct from /pums-burden-county
# (which pins to the latest year only) -- this endpoint exposes ALL
# available years so the UI can plot a series.
#
# We deliberately do NOT consume the v_pums_burden_county_yoy_overall
# view here: that view drops the earliest year (no prior_year). For the
# UI we want every year, including the earliest. The YoY math can be
# done client-side (delta = y[t] - y[t-1]) or via the YoY view if a
# downstream consumer needs the naive-independence SE.
# ============================================================================


_PUMS_BURDEN_COUNTY_SERIES_SELECT = """
    b.year, b.product, b.state_fips, b.county_fips,
    c.name                                         AS county_name,
    b.tenure_class,
    b.weighted_n, b.sample_n,
    b.burden_ratio_p50, b.burden_ratio_p50_se,
    b.suppressed
"""


def list_pums_burden_county_series(
    conn: psycopg.Connection,
    *,
    tenure: str | None = None,
    county_fips: str | None = None,
    product: str = DEFAULT_PUMS_PRODUCT,
    include_suppressed: bool = False,
) -> list[dict[str, Any]]:
    """List county-grain overall burden ratios across ALL years for *product*.

    Filters
    -------
    * ``tenure``      -- restrict to one tenure class (renter / owner_w_mtg /
                         owner_no_mtg). When ``None``, returns all three.
    * ``county_fips`` -- restrict to one county. When ``None``, returns all
                         21 NJ counties.
    * ``product``     -- 'acs1' or 'acs5'. The two products are NOT mixed
                         in the same series because their sampling windows
                         are different (1Y is a single year; 5Y is a
                         rolling 5-year window).

    Ordering matches what a charting client wants: county_name ASC,
    tenure_class ASC, year ASC. The client can group-by
    (county_fips, tenure_class) and plot each group as a separate trace.
    """
    where_clauses = [
        "b.state_fips = '34'",
        "b.product = %s",
        "b.segment_dim = 'overall'",
        "b.segment_value = 'overall'",
    ]
    params: list[Any] = [product]
    if tenure is not None:
        where_clauses.append("b.tenure_class = %s")
        params.append(tenure)
    if county_fips is not None:
        where_clauses.append("b.county_fips = %s")
        params.append(county_fips)
    if not include_suppressed:
        where_clauses.append("NOT b.suppressed")
    where_sql = " AND ".join(where_clauses)

    query = f"""
        SELECT {_PUMS_BURDEN_COUNTY_SERIES_SELECT}
        FROM derived.pums_burden_county_segmented b
        JOIN ref.county c ON c.county_fips = b.county_fips
        WHERE {where_sql}
        ORDER BY c.name, b.tenure_class, b.year
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        return list(cur.fetchall())


# ============================================================================
# /hpi/{county_fips}/series
# ============================================================================
#
# Wraps derived.f_fhfa_hpi_indexed(base_year): re-bases the FHFA HPI so that
# `base_year` = 100.000 for every county. The default base year is the
# project canonical (2000): it lines up with the README, with the way
# downstream charts label the y-axis ("HPI = 100 in 2000"), and with how
# the burden-vs-prices comparison in the dashboard is framed.
#
# WHY the function and not a materialised view: FHFA re-publishes the
# index with a re-base each year, but the *ratios between years* are
# stable across vintages -- so we never have to re-write history, we
# just compute the indexed series on the fly. Cost is ~21 rows/county
# x decades = a few hundred rows. Materialising would be premature.
#
# Caveat for the API contract: when `base_year` is missing for a given
# county (FHFA's series may start later than 2000 for sparse counties),
# the SQL function emits zero rows for that county. The route maps that
# to a 404 and the CLI to a graceful empty list, mirroring /burden.
# ============================================================================


HPI_DEFAULT_BASE_YEAR: Final[int] = 2000


_HPI_SELECT = """
    h.county_fips,
    c.name           AS county_name,
    h.year,
    h.hpi_indexed,
    h.hpi_raw,
    h.base_year_used,
    raw.annual_change,
    raw.n_transactions
"""


def list_hpi_county_series(
    conn: psycopg.Connection,
    *,
    county_fips: str,
    base_year: int = HPI_DEFAULT_BASE_YEAR,
) -> list[dict[str, Any]]:
    """List FHFA HPI annual series for one NJ county, re-indexed to ``base_year``.

    Returns rows in chronological year order. ``hpi_indexed`` is the
    base-year-anchored level (base_year = 100.000); ``hpi_raw`` is FHFA's
    published value in their current vintage's base; ``annual_change``
    and ``n_transactions`` come straight from raw.fhfa_hpi_county.

    NJ-only because ``ref.county`` is the curated NJ subset; passing a
    non-NJ FIPS returns an empty list (no JOIN match).
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT {_HPI_SELECT}
            FROM derived.f_fhfa_hpi_indexed(%s::SMALLINT)  h
            JOIN ref.county                                c
                ON c.county_fips = h.county_fips
            JOIN raw.fhfa_hpi_county                       raw
                ON raw.county_fips = h.county_fips
                AND raw.year       = h.year
            WHERE c.state_code   = 'NJ'
              AND h.county_fips  = %s
            ORDER BY h.year
            """,
            (base_year, county_fips),
        )
        return list(cur.fetchall())


# ============================================================================
# /income/{county_fips}/series
# ============================================================================
#
# Wraps derived.f_acs_mhi_real(base_year): CPI-deflates ACS B19013 median
# household income to constant base-year dollars. Default base year is
# computed at query time as min(max(dollar_year), max(cpi_year)) so the
# series shows up in "today's dollars" without the analyst having to
# pick a year manually.
#
# Two vintages exist in the underlying table (acs1 and acs5). Default
# is acs5 to mirror the burden endpoints: lower MOE, fewer suppressed
# small-population cells. Callers can pass `product='acs1'` for the
# fresher single-year series.
# ============================================================================


def resolve_default_income_base_year(conn: psycopg.Connection) -> int | None:
    """Pick a sensible default base year for ``f_acs_mhi_real``.

    Returns the most recent year that is BOTH (a) a `dollar_year` in
    `raw.acs_median_household_income` and (b) a CPI-published year in
    `derived.cpi_u_headline_annual`. None when either table is empty
    (in which case the route should 404 and the CLI should error out
    explicitly instead of silently picking a wrong year).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT LEAST(
                (SELECT MAX(dollar_year) FROM raw.acs_median_household_income),
                (SELECT MAX(year)        FROM derived.cpi_u_headline_annual)
            )
            """,
        )
        row = cur.fetchone()
        if row is None or row[0] is None:
            return None
        return int(row[0])


_INCOME_SELECT = """
    m.county_fips,
    c.name             AS county_name,
    m.year,
    m.product,
    m.estimate_real,
    m.estimate_nominal,
    m.deflator,
    m.base_year_used,
    raw.dollar_year,
    raw.margin_of_error
"""


def list_income_county_series(
    conn: psycopg.Connection,
    *,
    county_fips: str,
    base_year: int,
    product: str = "acs5",
) -> list[dict[str, Any]]:
    """List CPI-deflated ACS B19013 income series for one NJ county.

    Filters to a single ACS product (acs1 / acs5) because the two have
    different sampling windows and should not be plotted together as a
    single line. ``estimate_real`` is in ``base_year`` dollars;
    ``estimate_nominal`` is the as-published value; ``deflator`` is the
    multiplicative factor applied (>1 for past-dollar -> today-dollar,
    <1 for the rare future-base case).

    Suppressed estimates are excluded by ``f_acs_mhi_real`` itself
    (the function's WHERE m.estimate IS NOT NULL), which is the right
    default for chartable series.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT {_INCOME_SELECT}
            FROM derived.f_acs_mhi_real(%s::SMALLINT)         m
            JOIN ref.county                                   c
                ON c.county_fips = m.county_fips
            JOIN raw.acs_median_household_income              raw
                ON raw.county_fips = m.county_fips
                AND raw.year       = m.year
                AND raw.product    = m.product
            WHERE c.state_code   = 'NJ'
              AND m.county_fips  = %s
              AND m.product      = %s
            ORDER BY m.year
            """,
            (base_year, county_fips, product),
        )
        return list(cur.fetchall())


# ============================================================================
# /counties
# ============================================================================
#
# Reference list of NJ counties for UI dropdowns. Pulled live from
# ref.county (rather than baked into the JS) so that if we ever expand
# beyond NJ, the UI auto-discovers new counties.
# ============================================================================


def list_counties(
    conn: psycopg.Connection,
    *,
    state_code: str = "NJ",
) -> list[dict[str, Any]]:
    """List counties from ref.county for *state_code* (default NJ).

    Ordered alphabetically by name -- the natural display order for a
    dropdown. Returns ``county_fips`` and ``name`` only; consumers that
    need centroids or area can hit the underlying table directly.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT county_fips, name "
            "FROM ref.county "
            "WHERE state_code = %s "
            "ORDER BY name",
            (state_code,),
        )
        return list(cur.fetchall())


# ============================================================================
# Helpers used by routes
# ============================================================================


def compute_freshness_state(
    *,
    last_materialized_at: dt.datetime | None,
    expected_lag_hours: int | None,
    now: dt.datetime | None = None,
) -> tuple[str, float | None]:
    """Compute (freshness_state, age_hours) from materialization metadata.

    The lag budget lives in `ref.release_calendar.expected_lag_hours`
    (and is also encoded in the orchestration FreshnessPolicy.fail_window
    -- the two should be kept in sync). When either input is missing,
    we report 'unknown'; we never silently default to 'fresh', because
    that would hide gaps.
    """
    if last_materialized_at is None or expected_lag_hours is None:
        return "unknown", None
    now = now or dt.datetime.now(dt.UTC)
    if last_materialized_at.tzinfo is None:
        last_materialized_at = last_materialized_at.replace(tzinfo=dt.UTC)
    age_hours = (now - last_materialized_at).total_seconds() / 3600.0
    state = "fresh" if age_hours < expected_lag_hours else "stale"
    return state, round(age_hours, 2)
