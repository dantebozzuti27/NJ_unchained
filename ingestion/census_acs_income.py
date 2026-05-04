"""ACS B19013 (median household income) ingester.

Pulls county-level median household income from the Census Bureau's ACS
API and UPSERTs into ``raw.acs_median_household_income``. Both ACS 1-year
and ACS 5-year products are supported.

Without an API key, Census limits unauthenticated callers to 500 requests
per day; for the NJ-only use case (one request per year per product) we
never approach that. With ``CENSUS_API_KEY`` set in the environment the
key is appended to each request.

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

# Census API is occasionally slow/unresponsive. Retry transients with
# exponential backoff. 404 is authoritative ("not published").
_RETRY_MAX_ATTEMPTS: Final[int] = 4
_RETRY_BASE_BACKOFF_S: Final[float] = 2.0


class VintageNotPublishedError(IngestError):
    """Raised when Census's API confirms (via 404) that no file exists.

    The single most prominent case is the regular ACS 1-year for 2020,
    which Census did NOT release due to COVID-19 disruption (only the
    experimental file was published, with a smaller table set that
    excludes B19013). Callers should treat this as 'expected gap' and
    record a governance.dataset_health row, NOT a failure.
    """

if TYPE_CHECKING:
    import psycopg


log = logging.getLogger(__name__)


# ============================================================================
# Constants
# ============================================================================

# Census variable IDs for B19013 (median household income).
#   _001E = estimate, _001M = margin of error.
B19013_ESTIMATE_VAR: Final[str] = "B19013_001E"
B19013_MOE_VAR:      Final[str] = "B19013_001M"

# Census API base URL. {year}/acs/{product} resolves to e.g.
# https://api.census.gov/data/2022/acs/acs5
CENSUS_API_BASE: Final[str] = "https://api.census.gov/data"

# Default state FIPS for the NJ-first contract. Pass --state to override.
NJ_STATE_FIPS: Final[str] = "34"

# Allowed ACS products. acs1 requires county population >= 65,000;
# all NJ counties qualify in most years (Salem is borderline).
ALLOWED_PRODUCTS: Final[frozenset[str]] = frozenset({"acs1", "acs5"})

# Earliest year each product is available.
PRODUCT_START_YEAR: Final[dict[str, int]] = {
    "acs1": 2005,
    "acs5": 2009,
}

# Census ACS suppression sentinels. Documented in the ACS API spec; values
# are int-typed in the JSON response. We map each to a `suppression_code`
# enum value matching raw.acs_median_household_income.suppression_code.
SUPPRESSION_SENTINELS: Final[dict[int, str]] = {
    -666666666: "confidentiality",
    -222222222: "too_small",
    -333333333: "other",
    -555555555: "other",
    -888888888: "other",
    -999999999: "other",
}


# ============================================================================
# Fetch
# ============================================================================


@dataclass(frozen=True)
class FetchResult:
    """Output of :func:`fetch_acs_b19013_year`. One per (year, product) call."""

    dataframe: pl.DataFrame
    source_url: str
    source_sha256: str
    year: int
    product: str
    state_fips: str
    n_rows: int


def _build_url(
    year: int, product: str, state_fips: str, *, api_key: str | None,
) -> str:
    """Return the canonical Census API URL for B19013 county-level pull."""
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

    qs = {
        "get": f"NAME,{B19013_ESTIMATE_VAR},{B19013_MOE_VAR}",
        "for": "county:*",
        "in":  f"state:{state_fips}",
    }
    if api_key:
        qs["key"] = api_key
    qs_str = "&".join(f"{k}={v}" for k, v in qs.items())
    return f"{CENSUS_API_BASE}/{year}/acs/{product}?{qs_str}"


def _coerce_value(raw: Any) -> tuple[float | None, str | None]:
    """Map a raw ACS value to (estimate, suppression_code).

    Census returns:
      * a numeric string for valid estimates
      * one of the SUPPRESSION_SENTINELS as an int (or sometimes a string)
      * JSON null when the variable is not collected for that geography

    We accept all three and return (estimate, suppression_code) where
    estimate is non-null IFF suppression_code is None.
    """
    if raw is None:
        return None, "other"
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None, "other"
    if int(v) in SUPPRESSION_SENTINELS:
        return None, SUPPRESSION_SENTINELS[int(v)]
    if v <= 0:
        # Census occasionally emits 0 for "not applicable" without using
        # the sentinel; treat as suppressed-other to satisfy the table's
        # "estimate > 0 OR suppressed" CHECK constraint.
        return None, "other"
    return v, None


def _get_with_retry(
    url: str, *, hash_url: str, timeout_s: float,
) -> bytes:
    """GET *url* with exponential-backoff retry on transient errors.

    Transient = network read timeout, connect failure, 5xx HTTP status,
    or 408/429. Authoritative 4xx (other than 408/429) raise immediately;
    404 raises :class:`VintageNotPublishedError`.
    """
    last_exc: Exception | None = None
    for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
        try:
            with httpx.Client(timeout=timeout_s) as client:
                resp = client.get(url)
            if resp.status_code == 404:
                raise VintageNotPublishedError(
                    f"Census returned 404 for {hash_url}. "
                    "The most common cause is the missing 2020 ACS 1-year "
                    "release; verify against the Census release calendar."
                )
            if 400 <= resp.status_code < 500 and resp.status_code not in (408, 429):
                resp.raise_for_status()
            if resp.is_success:
                return resp.content
            last_exc = httpx.HTTPStatusError(
                f"transient HTTP {resp.status_code}",
                request=resp.request, response=resp,
            )
        except httpx.TransportError as exc:
            # TransportError covers ReadTimeout, ConnectTimeout, ConnectError,
            # ReadError, WriteError, NetworkError, RemoteProtocolError, etc.
            last_exc = exc
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


def fetch_acs_b19013_year(
    *,
    year: int,
    product: str,
    state_fips: str = NJ_STATE_FIPS,
    api_key: str | None = None,
    timeout_s: float = 60.0,
) -> FetchResult:
    """Fetch B19013 (median household income) for *year* / *product* / *state_fips*.

    Returns a :class:`FetchResult` whose DataFrame has columns
    ``county_fips``, ``year``, ``product``, ``estimate``, ``margin_of_error``,
    ``dollar_year``, ``suppression_code``. The latter two depend on the
    product:

      * ACS 1-year: dollar_year == year
      * ACS 5-year: dollar_year == year (Census deflates 5y to the END year)
    """
    url = _build_url(year, product, state_fips, api_key=api_key)
    # Provenance hash: hash the URL WITHOUT the api_key (different keys
    # producing the same data should compare equal).
    hash_url = _build_url(year, product, state_fips, api_key=None)
    log.info("Fetching ACS B19013: %s", hash_url)

    body = _get_with_retry(url, hash_url=hash_url, timeout_s=timeout_s)

    import json as _json
    # Census API returns JSON-encoded array-of-arrays, where the first row
    # is the column headers.
    payload = _json.loads(body)
    if not payload or len(payload) < 2:
        raise IngestError(
            f"Census API returned empty result for {hash_url}; "
            "check year/product/state combination."
        )

    headers: list[str] = payload[0]
    rows = payload[1:]

    est_idx      = headers.index(B19013_ESTIMATE_VAR)
    moe_idx      = headers.index(B19013_MOE_VAR)
    state_idx    = headers.index("state")
    county_idx   = headers.index("county")

    out_rows: list[dict[str, Any]] = []
    for r in rows:
        county_fips = f"{r[state_idx]}{r[county_idx]}"
        if len(county_fips) != 5 or not county_fips.isdigit():
            log.warning("Skipping malformed county_fips %r in %s", county_fips, hash_url)
            continue
        estimate, suppression = _coerce_value(r[est_idx])
        moe, _ = _coerce_value(r[moe_idx])
        # MOE-only suppression is not meaningful for the value's CHECK
        # constraint; we keep MOE NULL when the estimate is suppressed
        # (the table allows MOE=NULL freely).
        out_rows.append({
            "county_fips":      county_fips,
            "year":             year,
            "product":          product,
            "estimate":         estimate,
            "margin_of_error":  moe,
            "dollar_year":      year,   # both products: end-year dollars
            "suppression_code": suppression,
        })

    if not out_rows:
        raise IngestError(
            f"Census returned 0 county rows for state {state_fips} "
            f"{product} {year}; this should not happen for valid inputs."
        )

    df = pl.DataFrame(out_rows, schema={
        "county_fips":      pl.Utf8,
        "year":             pl.Int64,
        "product":          pl.Utf8,
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
    """Add provenance columns; return DataFrame in raw table shape."""
    return result.dataframe.with_columns(
        pl.lit(result.source_url).alias("source_url"),
        pl.lit(result.source_sha256).alias("source_sha256"),
    ).select([
        "county_fips", "year", "product",
        "estimate", "margin_of_error",
        "dollar_year", "suppression_code",
        "source_url", "source_sha256",
    ])


# UPSERT, not COPY, because Census occasionally reissues vintages with
# revised values (esp. when boundary changes are processed retroactively).
# Volume is small (21 NJ counties x ~17 years x 2 products ~= 700 rows total).
_UPSERT_SQL: Final[str] = """
INSERT INTO raw.acs_median_household_income
    (county_fips, year, product, estimate, margin_of_error,
     dollar_year, suppression_code, source_url, source_sha256)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (county_fips, year, product) DO UPDATE SET
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
    """ACS B19013 (median household income) ingester (Tier 2)."""


@cli.command("fetch")
@click.option("--year",  type=int, required=True)
@click.option("--product", type=click.Choice(sorted(ALLOWED_PRODUCTS)),
              default="acs5", show_default=True)
@click.option("--state", default=NJ_STATE_FIPS, show_default=True,
              help="State FIPS (e.g. 34 for NJ).")
def fetch_cmd(year: int, product: str, state: str) -> None:
    """Fetch one (year, product, state) batch and print a summary."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    api_key = os.environ.get("CENSUS_API_KEY")
    result = fetch_acs_b19013_year(
        year=year, product=product, state_fips=state, api_key=api_key,
    )
    click.echo(
        f"source_url={result.source_url}\n"
        f"sha256={result.source_sha256}\n"
        f"year={result.year} product={result.product} state={result.state_fips}\n"
        f"n_rows={result.n_rows}\n"
    )
    click.echo(result.dataframe.head(25))


@cli.command("load")
@click.option("--start-year", type=int, required=True)
@click.option("--end-year",   type=int, required=True)
@click.option("--product",
              type=click.Choice([*sorted(ALLOWED_PRODUCTS), "both"]),
              default="acs5", show_default=True,
              help="ACS product to fetch. 'both' fetches acs1 and acs5.")
@click.option("--state", default=NJ_STATE_FIPS, show_default=True)
@click.option("--dsn", envvar="PG_DSN", required=True,
              help="Postgres DSN (or set PG_DSN env var).")
def load_cmd(
    start_year: int, end_year: int, product: str, state: str, dsn: str,
) -> None:
    """Fetch + UPSERT B19013 across [start_year, end_year]."""
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
                    result = fetch_acs_b19013_year(
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
                                "raw.acs_median_household_income",
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

    msg = f"UPSERTed {total} rows into raw.acs_median_household_income."
    if skipped:
        msg += f" Skipped (recorded in governance.dataset_health): {skipped}"
    click.echo(msg)
