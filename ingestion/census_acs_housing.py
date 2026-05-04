"""ACS housing-cost ingester.

Pulls the canonical set of ACS housing variables (B25064 / B25077 /
B25088 / B25003) at county level and UPSERTs into ``raw.acs_housing``.

This module shares its suppression-sentinel handling and 404-vintage
behavior with :mod:`ingestion.census_acs_income`; the surface differs
because we batch many variables in a single Census API call.

The Census API allows up to 50 variables per request. Our canonical set
is well below that, so we do single-shot per (year, product) calls.

API docs: https://www.census.gov/data/developers/data-sets/acs-5year.html
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import click
import httpx
import polars as pl

from ingestion._base import IngestError
from ingestion.census_acs_income import (
    ALLOWED_PRODUCTS,
    NJ_STATE_FIPS,
    PRODUCT_START_YEAR,
    SUPPRESSION_SENTINELS,
    VintageNotPublishedError,
    _coerce_value,
)

# Census's API is occasionally unresponsive (we have observed multi-minute
# stalls during the daily refresh windows). We retry transient failures
# with exponential backoff before declaring a fetch failed. 404 is NOT
# retried -- it is authoritative ("vintage not published").
_RETRY_MAX_ATTEMPTS: Final[int] = 4
_RETRY_BASE_BACKOFF_S: Final[float] = 2.0

if TYPE_CHECKING:
    from collections.abc import Iterable

    import psycopg


log = logging.getLogger(__name__)


# ============================================================================
# Canonical variable catalog
# ============================================================================

# Census variables we track. Adding to this list requires:
#   1. INSERT to ref.acs_housing_variable (a follow-up migration).
#   2. Adding the variable here.
#   3. (Optional) Updating derived.acs_housing_wide to PIVOT it out.
#
# We track BOTH the ESTIMATE (`_E`) and MARGIN OF ERROR (`_M`) variants
# for each. _E gets stored as `estimate`, _M as `margin_of_error`.
#
# Each tuple entry is the BASE id (e.g. "B25064_001"); the API parameters
# request both _E and _M.
CANONICAL_HOUSING_VARS: Final[tuple[str, ...]] = (
    "B25064_001",  # median gross rent
    "B25077_001",  # median home value
    "B25088_002",  # median monthly owner costs (with mortgage)
    "B25088_003",  # median monthly owner costs (without mortgage)
    "B25003_001",  # total occupied units
    "B25003_002",  # owner-occupied units
    "B25003_003",  # renter-occupied units
)


# Census API base URL.
CENSUS_API_BASE: Final[str] = "https://api.census.gov/data"


# ============================================================================
# Fetch
# ============================================================================


def _get_with_retry(
    url: str, *, hash_url: str, timeout_s: float,
) -> bytes:
    """GET *url* with exponential-backoff retry on transient errors.

    Transient = network read timeout, connect failure, 5xx HTTP status.
    Authoritative non-200 responses (4xx other than 408/429) raise
    immediately; 404 raises :class:`VintageNotPublishedError`.
    """
    last_exc: Exception | None = None
    for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
        try:
            with httpx.Client(timeout=timeout_s) as client:
                resp = client.get(url)
            if resp.status_code == 404:
                raise VintageNotPublishedError(
                    f"Census returned 404 for {hash_url}. "
                    "Verify against the Census release calendar (e.g. 2020 ACS1 "
                    "was not published due to COVID disruption)."
                )
            if 400 <= resp.status_code < 500 and resp.status_code not in (408, 429):
                resp.raise_for_status()
            if resp.is_success:
                return resp.content
            # 5xx, 408, 429 -> retryable
            last_exc = httpx.HTTPStatusError(
                f"transient HTTP {resp.status_code}",
                request=resp.request, response=resp,
            )
        except httpx.TransportError as exc:
            # TransportError covers ReadTimeout, ConnectTimeout, ConnectError,
            # ReadError, WriteError, NetworkError, RemoteProtocolError, etc.
            # All transient; retry.
            last_exc = exc
        # Exponential backoff: 2, 4, 8, 16 seconds.
        if attempt < _RETRY_MAX_ATTEMPTS:
            backoff = _RETRY_BASE_BACKOFF_S * (2 ** (attempt - 1))
            log.warning(
                "Census fetch attempt %d/%d failed (%s); retrying in %.1fs",
                attempt, _RETRY_MAX_ATTEMPTS, type(last_exc).__name__, backoff,
            )
            time.sleep(backoff)
    assert last_exc is not None
    raise IngestError(
        f"Census fetch failed after {_RETRY_MAX_ATTEMPTS} attempts: "
        f"{type(last_exc).__name__}: {last_exc}"
    ) from last_exc


@dataclass(frozen=True)
class FetchResult:
    """Output of :func:`fetch_acs_housing_year`. One per (year, product) call."""

    dataframe: pl.DataFrame
    source_url: str
    source_sha256: str
    year: int
    product: str
    state_fips: str
    n_rows: int


def _build_url(
    year: int,
    product: str,
    state_fips: str,
    variable_ids: Iterable[str],
    *,
    api_key: str | None,
) -> str:
    """Return the canonical Census API URL for a multi-variable batch."""
    if product not in ALLOWED_PRODUCTS:
        raise IngestError(
            f"Unknown product {product!r}; expected one of {sorted(ALLOWED_PRODUCTS)}"
        )
    earliest = PRODUCT_START_YEAR[product]
    if year < earliest:
        raise IngestError(
            f"Census product {product!r} did not exist before {earliest}; "
            f"requested {year}."
        )

    var_list = list(variable_ids)
    if not var_list:
        raise IngestError("variable_ids must be non-empty")

    # Census ACS API requests both the estimate (_E) and margin (_M) per
    # variable. Up to 50 vars per request including these doubles
    # (so 25 base IDs is the safe ceiling without an API key).
    get_fields = []
    for v in var_list:
        get_fields.append(f"{v}E")
        get_fields.append(f"{v}M")
    get_str = ",".join(["NAME", *get_fields])

    qs = {
        "get": get_str,
        "for": "county:*",
        "in":  f"state:{state_fips}",
    }
    if api_key:
        qs["key"] = api_key
    qs_str = "&".join(f"{k}={v}" for k, v in qs.items())
    return f"{CENSUS_API_BASE}/{year}/acs/{product}?{qs_str}"


def fetch_acs_housing_year(
    *,
    year: int,
    product: str,
    state_fips: str = NJ_STATE_FIPS,
    variable_ids: Iterable[str] | None = None,
    api_key: str | None = None,
    timeout_s: float = 60.0,
) -> FetchResult:
    """Fetch all canonical ACS housing variables for *year*/*product*/*state_fips*.

    Returns a :class:`FetchResult` whose DataFrame is in the long-skinny
    shape that ``raw.acs_housing`` expects: one row per (county, variable)
    with columns ``county_fips``, ``year``, ``product``, ``variable_id``,
    ``estimate``, ``margin_of_error``, ``dollar_year``, ``suppression_code``.
    """
    var_list = list(variable_ids) if variable_ids is not None else list(CANONICAL_HOUSING_VARS)

    url = _build_url(year, product, state_fips, var_list, api_key=api_key)
    hash_url = _build_url(year, product, state_fips, var_list, api_key=None)
    log.info("Fetching ACS housing (%d vars): %s", len(var_list), hash_url)

    body = _get_with_retry(url, hash_url=hash_url, timeout_s=timeout_s)

    import json as _json
    payload = _json.loads(body)
    if not payload or len(payload) < 2:
        raise IngestError(
            f"Census API returned empty result for {hash_url}; "
            "check year/product/state combination."
        )

    headers: list[str] = payload[0]
    rows = payload[1:]

    state_idx  = headers.index("state")
    county_idx = headers.index("county")

    # Map each base variable id -> (estimate-column-index, moe-column-index).
    var_idx: dict[str, tuple[int, int]] = {}
    for v in var_list:
        try:
            var_idx[v] = (headers.index(f"{v}E"), headers.index(f"{v}M"))
        except ValueError as exc:
            raise IngestError(
                f"Census response missing expected variable {v}E/{v}M; "
                f"got headers={headers!r}"
            ) from exc

    out_rows: list[dict[str, Any]] = []
    for r in rows:
        county_fips = f"{r[state_idx]}{r[county_idx]}"
        if len(county_fips) != 5 or not county_fips.isdigit():
            log.warning("Skipping malformed county_fips %r in %s", county_fips, hash_url)
            continue
        for variable_id, (est_idx, moe_idx) in var_idx.items():
            estimate, suppression = _coerce_value(r[est_idx])
            moe, _ = _coerce_value(r[moe_idx])
            out_rows.append({
                "county_fips":      county_fips,
                "year":             year,
                "product":          product,
                "variable_id":      variable_id,
                "estimate":         estimate,
                "margin_of_error":  moe,
                "dollar_year":      year,
                "suppression_code": suppression,
            })

    if not out_rows:
        raise IngestError(
            f"Census returned 0 rows for state {state_fips} {product} {year}; "
            "this should not happen for valid inputs."
        )

    df = pl.DataFrame(out_rows, schema={
        "county_fips":      pl.Utf8,
        "year":             pl.Int64,
        "product":          pl.Utf8,
        "variable_id":      pl.Utf8,
        "estimate":         pl.Float64,
        "margin_of_error":  pl.Float64,
        "dollar_year":      pl.Int64,
        "suppression_code": pl.Utf8,
    })

    sha256 = hashlib.sha256(body).hexdigest()
    return FetchResult(
        dataframe=df,
        source_url=hash_url,
        source_sha256=sha256,
        year=year,
        product=product,
        state_fips=state_fips,
        n_rows=df.height,
    )


# ============================================================================
# Stage + load
# ============================================================================


def stage_dataframe(result: FetchResult) -> pl.DataFrame:
    """Add provenance columns; return DataFrame in raw.acs_housing shape."""
    return result.dataframe.with_columns(
        pl.lit(result.source_url).alias("source_url"),
        pl.lit(result.source_sha256).alias("source_sha256"),
    ).select([
        "county_fips", "year", "product", "variable_id",
        "estimate", "margin_of_error",
        "dollar_year", "suppression_code",
        "source_url", "source_sha256",
    ])


_UPSERT_SQL: Final[str] = """
INSERT INTO raw.acs_housing
    (county_fips, year, product, variable_id, estimate, margin_of_error,
     dollar_year, suppression_code, source_url, source_sha256)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (county_fips, year, product, variable_id) DO UPDATE SET
    estimate         = EXCLUDED.estimate,
    margin_of_error  = EXCLUDED.margin_of_error,
    dollar_year      = EXCLUDED.dollar_year,
    suppression_code = EXCLUDED.suppression_code,
    source_url       = EXCLUDED.source_url,
    source_sha256    = EXCLUDED.source_sha256,
    ingested_at      = now()
"""


def load_to_postgres(
    staged: pl.DataFrame,
    connection: psycopg.Connection,
) -> int:
    """UPSERT staged rows; return rows touched."""
    rows = list(staged.iter_rows())
    if not rows:
        return 0
    with connection.cursor() as cur:
        cur.executemany(_UPSERT_SQL, rows)
    return len(rows)


# ============================================================================
# CLI
# ============================================================================


@click.group()
def cli() -> None:
    """ACS housing variables ingester (Tier 2)."""


@cli.command("fetch")
@click.option("--year", type=int, required=True)
@click.option("--product", type=click.Choice(sorted(ALLOWED_PRODUCTS)),
              default="acs5", show_default=True)
@click.option("--state", default=NJ_STATE_FIPS, show_default=True,
              help="State FIPS (e.g. 34 for NJ).")
def fetch_cmd(year: int, product: str, state: str) -> None:
    """Fetch one (year, product, state) batch and print a summary."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    api_key = os.environ.get("CENSUS_API_KEY")
    result = fetch_acs_housing_year(
        year=year, product=product, state_fips=state, api_key=api_key,
    )
    click.echo(
        f"source_url={result.source_url}\n"
        f"sha256={result.source_sha256}\n"
        f"year={result.year} product={result.product} state={result.state_fips}\n"
        f"n_rows={result.n_rows}\n"
        f"variables={sorted(set(result.dataframe['variable_id'].to_list()))}\n"
    )
    click.echo(result.dataframe.head(15))


@cli.command("load")
@click.option("--start-year", type=int, required=True)
@click.option("--end-year",   type=int, required=True)
@click.option("--product",
              type=click.Choice([*sorted(ALLOWED_PRODUCTS), "both"]),
              default="acs5", show_default=True)
@click.option("--state", default=NJ_STATE_FIPS, show_default=True)
@click.option("--dsn", envvar="PG_DSN", required=True,
              help="Postgres DSN (or set PG_DSN env var).")
def load_cmd(
    start_year: int, end_year: int, product: str, state: str, dsn: str,
) -> None:
    """Fetch + UPSERT ACS housing variables across [start_year, end_year]."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if start_year > end_year:
        raise click.UsageError(f"start_year {start_year} > end_year {end_year}")
    api_key = os.environ.get("CENSUS_API_KEY")
    products = ("acs1", "acs5") if product == "both" else (product,)

    import json as _json

    import psycopg

    total = 0
    skipped: list[tuple[str, int]] = []
    with psycopg.connect(dsn) as conn:
        for prod in products:
            earliest = PRODUCT_START_YEAR[prod]
            for yr in range(max(start_year, earliest), end_year + 1):
                try:
                    result = fetch_acs_housing_year(
                        year=yr, product=prod, state_fips=state, api_key=api_key,
                    )
                except VintageNotPublishedError as exc:
                    log.warning("Skipping %s %d: %s", prod, yr, exc)
                    skipped.append((prod, yr))
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO governance.dataset_health "
                            "(dataset_id, signal_name, severity, details) "
                            "VALUES (%s, %s, %s, %s::jsonb)",
                            (
                                "raw.acs_housing",
                                "vintage_not_published",
                                "warn",
                                _json.dumps({
                                    "product": prod,
                                    "year": yr,
                                    "state_fips": state,
                                    "reason": "Census API returned 404",
                                }),
                            ),
                        )
                    continue
                staged = stage_dataframe(result)
                n = load_to_postgres(staged, conn)
                total += n
                log.info("Loaded %d rows for %s %d", n, prod, yr)
        conn.commit()

    msg = f"UPSERTed {total} rows into raw.acs_housing."
    if skipped:
        msg += f" Skipped (recorded in governance.dataset_health): {skipped}"
    click.echo(msg)


# Suppress unused-import warning -- SUPPRESSION_SENTINELS is re-exported by
# proxy of the test suite checking it works the same as in income.py.
_ = SUPPRESSION_SENTINELS
