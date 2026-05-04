"""FRED rate series ingester (mortgage rates, Treasury yields, fed funds).

FRED publishes individual time series as plain CSV at a stable URL. No
auth is required for unauthenticated callers. The ingester pulls the
canonical platform set (MORTGAGE30US, DGS10, FEDFUNDS) and UPSERTs into
``raw.fred_observation``.

URL pattern:
    https://fred.stlouisfed.org/graph/fredgraph.csv?id={SERIES}&cosd=YYYY-MM-DD&coed=YYYY-MM-DD

CSV format: ``observation_date,SERIES_ID``. Missing values are encoded
as ``"."``; we map those to NULL.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Final

import click
import httpx
import polars as pl

from ingestion._base import IngestError

if TYPE_CHECKING:
    from collections.abc import Iterable

    import psycopg


log = logging.getLogger(__name__)


# ============================================================================
# Canonical series catalog
# ============================================================================

# The platform's canonical FRED series. Adding requires a deliberate
# review (interpretation differs by series).
CANONICAL_SERIES: Final[tuple[str, ...]] = (
    "MORTGAGE30US",  # Freddie Mac 30-yr fixed mortgage avg, weekly
    "DGS10",         # 10-yr Treasury constant maturity, daily
    "FEDFUNDS",      # Effective Fed Funds Rate, monthly
)


FRED_CSV_URL_TEMPLATE: Final[str] = (
    "https://fred.stlouisfed.org/graph/fredgraph.csv"
    "?id={series}&cosd={start}&coed={end}"
)


# Reuse the same retry policy as the Census ingester. FRED is generally
# more available than Census, but not infinitely so.
_RETRY_MAX_ATTEMPTS: Final[int] = 4
_RETRY_BASE_BACKOFF_S: Final[float] = 2.0


# ============================================================================
# Fetch
# ============================================================================


@dataclass(frozen=True)
class FetchResult:
    """Output of :func:`fetch_fred_series`. One per series."""

    dataframe: pl.DataFrame
    source_url: str
    source_sha256: str
    series_id: str
    start_date: date
    end_date: date
    n_observations: int


def _build_url(series_id: str, start: date, end: date) -> str:
    """Return the canonical FRED CSV download URL for *series_id*/*start*/*end*."""
    if not series_id or not series_id.replace("_", "").isalnum():
        raise IngestError(f"Invalid FRED series_id: {series_id!r}")
    if start > end:
        raise IngestError(f"start {start} must be <= end {end}")
    return FRED_CSV_URL_TEMPLATE.format(
        series=series_id, start=start.isoformat(), end=end.isoformat(),
    )


def _get_csv_with_retry(url: str, *, timeout_s: float) -> bytes:
    """GET FRED CSV with exponential-backoff retry on transient failure."""
    last_exc: Exception | None = None
    for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
        try:
            with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
                resp = client.get(url)
            if 400 <= resp.status_code < 500 and resp.status_code not in (408, 429):
                resp.raise_for_status()
            if resp.is_success:
                return resp.content
            last_exc = httpx.HTTPStatusError(
                f"transient HTTP {resp.status_code}",
                request=resp.request, response=resp,
            )
        except httpx.TransportError as exc:
            last_exc = exc
        if attempt < _RETRY_MAX_ATTEMPTS:
            backoff = _RETRY_BASE_BACKOFF_S * (2 ** (attempt - 1))
            log.warning(
                "FRED fetch attempt %d/%d failed (%s); retrying in %.1fs",
                attempt, _RETRY_MAX_ATTEMPTS, type(last_exc).__name__, backoff,
            )
            time.sleep(backoff)
    assert last_exc is not None
    raise IngestError(
        f"FRED fetch failed after {_RETRY_MAX_ATTEMPTS} attempts: "
        f"{type(last_exc).__name__}: {last_exc}"
    ) from last_exc


def fetch_fred_series(
    series_id: str,
    *,
    start: date,
    end: date,
    timeout_s: float = 60.0,
) -> FetchResult:
    """Fetch one FRED series across [*start*, *end*].

    Returns a :class:`FetchResult` whose DataFrame has columns
    ``series_id`` (Utf8), ``observation_date`` (Date),
    ``value`` (Float64, NULL for missing observations).
    """
    url = _build_url(series_id, start, end)
    log.info("Fetching FRED series: %s", url)
    body = _get_csv_with_retry(url, timeout_s=timeout_s)

    # FRED CSV columns: ``observation_date`` and the SERIES_ID. Missing
    # values are ``"."``; polars treats those as null when we set
    # null_values appropriately.
    raw = pl.read_csv(
        body,
        try_parse_dates=True,
        null_values=["."],
        infer_schema_length=10_000,
    )

    if "observation_date" not in raw.columns:
        raise IngestError(
            f"Unexpected FRED CSV shape; missing observation_date column. "
            f"Got: {raw.columns!r}"
        )
    if series_id not in raw.columns:
        raise IngestError(
            f"FRED CSV missing expected value column {series_id!r}; "
            f"got {raw.columns!r}"
        )

    df = raw.select([
        pl.lit(series_id).alias("series_id"),
        pl.col("observation_date").cast(pl.Date),
        pl.col(series_id).cast(pl.Float64, strict=False).alias("value"),
    ])

    sha256 = hashlib.sha256(body).hexdigest()
    return FetchResult(
        dataframe=df,
        source_url=url,
        source_sha256=sha256,
        series_id=series_id,
        start_date=start,
        end_date=end,
        n_observations=df.height,
    )


# ============================================================================
# Stage + load
# ============================================================================


def stage_dataframe(result: FetchResult) -> pl.DataFrame:
    """Add provenance; return DataFrame in raw.fred_observation shape."""
    return result.dataframe.with_columns(
        pl.lit(result.source_url).alias("source_url"),
        pl.lit(result.source_sha256).alias("source_sha256"),
    ).select([
        "series_id", "observation_date", "value",
        "source_url", "source_sha256",
    ])


_UPSERT_SQL: Final[str] = """
INSERT INTO raw.fred_observation
    (series_id, observation_date, value, source_url, source_sha256)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (series_id, observation_date) DO UPDATE SET
    value         = EXCLUDED.value,
    source_url    = EXCLUDED.source_url,
    source_sha256 = EXCLUDED.source_sha256,
    ingested_at   = now()
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
    """FRED rate-series ingester (Tier 2)."""


@cli.command("fetch")
@click.option("--series", required=True, help="FRED series ID (e.g. MORTGAGE30US)")
@click.option("--start-date", "start_date_str", type=str, required=True,
              help="ISO date (YYYY-MM-DD)")
@click.option("--end-date", "end_date_str", type=str, required=True,
              help="ISO date (YYYY-MM-DD)")
def fetch_cmd(series: str, start_date_str: str, end_date_str: str) -> None:
    """Fetch one FRED series and print a summary."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = fetch_fred_series(
        series,
        start=date.fromisoformat(start_date_str),
        end=date.fromisoformat(end_date_str),
    )
    click.echo(
        f"series={result.series_id}\n"
        f"sha256={result.source_sha256}\n"
        f"date_range={result.start_date}..{result.end_date}\n"
        f"n_observations={result.n_observations}\n"
    )
    click.echo(result.dataframe.head(10))


@cli.command("load")
@click.option("--start-date", "start_date_str", type=str, required=True)
@click.option("--end-date", "end_date_str", type=str, required=True)
@click.option("--series", multiple=True,
              help="FRED series IDs (repeat). Defaults to canonical catalog.")
@click.option("--dsn", envvar="PG_DSN", required=True)
def load_cmd(
    start_date_str: str, end_date_str: str, series: tuple[str, ...], dsn: str,
) -> None:
    """Fetch + UPSERT one or more FRED series into raw.fred_observation."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sids: Iterable[str] = series or CANONICAL_SERIES
    start = date.fromisoformat(start_date_str)
    end   = date.fromisoformat(end_date_str)

    import psycopg

    total = 0
    with psycopg.connect(dsn) as conn:
        for sid in sids:
            result = fetch_fred_series(sid, start=start, end=end)
            staged = stage_dataframe(result)
            n = load_to_postgres(staged, conn)
            total += n
            log.info("Loaded %d rows for %s", n, sid)
        conn.commit()
    click.echo(f"UPSERTed {total} rows into raw.fred_observation.")
