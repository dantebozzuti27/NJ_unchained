"""BLS Consumer Price Index (CPI-U) ingester.

Pulls monthly index values for the canonical national CPI-U series from
the BLS Public Data API v2 and UPSERTs into ``raw.cpi_u``.

Two access modes:

* **Unauthenticated (default).** GET per series, up to 10 years per call.
  Suitable for development and small backfills. No API key required.
* **Registered key (``BLS_API_KEY`` env var).** POST batched (up to
  25 series, 20 years per call). Use for full backfills.

The ingester is idempotent: BLS occasionally revises seasonally-adjusted
series retroactively, so the loader UPSERTs on
``(series_id, year, period)`` and updates value if it changed.

API docs: https://www.bls.gov/developers/api_signature_v2.htm
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import click
import httpx
import polars as pl

from ingestion._base import IngestError

if TYPE_CHECKING:
    from collections.abc import Sequence

    import psycopg


log = logging.getLogger(__name__)


# ============================================================================
# Canonical series catalog
# ============================================================================

# The platform's canonical CPI-U series. Adding to this list is a deliberate
# design decision (each series implies new derived-layer math), so we keep it
# narrow. Unauthenticated callers can fetch this set in one round-trip per
# series.
CANONICAL_SERIES: Final[tuple[str, ...]] = (
    "CUUR0000SA0",      # All items, NSA  -- headline deflator
    "CUSR0000SA0",      # All items, SA
    "CUUR0000SA0L1E",   # All items less food and energy ("core")
    "CUUR0000SAH",      # Shelter
    "CUUR0000SAH1",     # Rent of primary residence
    "CUUR0000SAH2",     # Owners' equivalent rent of residences
)


# BLS API v2 endpoint. v1 lacks the JSON request body and is rate-limited
# more aggressively; we use v2 unconditionally.
BLS_API_URL: Final[str] = "https://api.bls.gov/publicAPI/v2/timeseries/data/"


# Maximum year span allowed by BLS in a single API request. The unauth limit
# is 10 years; the registered-key limit is 20.
MAX_YEAR_SPAN_UNAUTH: Final[int] = 10
MAX_YEAR_SPAN_KEYED:  Final[int] = 20


# ============================================================================
# Fetch
# ============================================================================


@dataclass(frozen=True)
class FetchResult:
    """Output of :func:`fetch_cpi_series`.

    Holds the raw observations as a polars DataFrame plus provenance.
    The DataFrame schema matches ``raw.cpi_u``'s natural-key columns plus
    ``value``; provenance columns (``source_url``, ``source_sha256``) live
    on the result and are added by :func:`stage_dataframe`.
    """

    dataframe: pl.DataFrame
    source_url: str
    source_sha256: str
    series_ids: tuple[str, ...]
    start_year: int
    end_year: int
    n_observations: int


def _bls_request(
    series_ids: Sequence[str],
    start_year: int,
    end_year: int,
    *,
    api_key: str | None,
    timeout_s: float,
) -> tuple[dict[str, Any], str, str]:
    """Issue a single BLS POST request and return (parsed_json, url, sha256).

    BLS POST accepts up to 50 series with a registered key, 25 without; we
    do not split here -- the caller is responsible for chunking. We use POST
    even for single-series calls because the response shape is identical
    and the request is more uniform across modes.
    """
    payload: dict[str, Any] = {
        "seriesid":  list(series_ids),
        "startyear": str(start_year),
        "endyear":   str(end_year),
    }
    if api_key:
        payload["registrationkey"] = api_key

    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    sha256 = hashlib.sha256(body).hexdigest()

    with httpx.Client(timeout=timeout_s) as client:
        resp = client.post(
            BLS_API_URL,
            content=body,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()

    if data.get("status") != "REQUEST_SUCCEEDED":
        raise IngestError(
            f"BLS API request failed: status={data.get('status')!r}, "
            f"message={data.get('message')!r}"
        )

    return data, BLS_API_URL, sha256


def fetch_cpi_series(
    series_ids: Sequence[str],
    *,
    start_year: int,
    end_year: int,
    api_key: str | None = None,
    timeout_s: float = 60.0,
) -> FetchResult:
    """Fetch CPI-U observations for *series_ids* across [*start_year*, *end_year*].

    The BLS API enforces a year-span limit per request (10 unauth, 20
    keyed); if the requested span exceeds that, we issue multiple requests
    and concatenate. Series counts are NOT chunked here -- BLS's
    25/50-series-per-request limit is enforced by the caller.

    Returns a :class:`FetchResult` whose DataFrame has columns
    ``series_id`` (Utf8), ``year`` (Int64), ``period`` (Utf8),
    ``value`` (Float64). Empty results raise :class:`IngestError`
    rather than returning an empty DataFrame -- a silent empty pull is
    almost always a misconfigured series ID.
    """
    if start_year > end_year:
        raise IngestError(f"start_year {start_year} must be <= end_year {end_year}")
    if not series_ids:
        raise IngestError("series_ids must be non-empty")

    max_span = MAX_YEAR_SPAN_KEYED if api_key else MAX_YEAR_SPAN_UNAUTH

    # Chunk year-spans to respect BLS limits.
    spans: list[tuple[int, int]] = []
    s = start_year
    while s <= end_year:
        e = min(s + max_span - 1, end_year)
        spans.append((s, e))
        s = e + 1

    log.info(
        "Fetching %d series across %d-%d in %d chunk(s) (auth=%s)",
        len(series_ids), start_year, end_year, len(spans),
        "yes" if api_key else "no",
    )

    rows: list[dict[str, Any]] = []
    sha_inputs: list[str] = []
    for s_year, e_year in spans:
        data, _, sha = _bls_request(
            series_ids, s_year, e_year,
            api_key=api_key, timeout_s=timeout_s,
        )
        sha_inputs.append(sha)
        for series in data["Results"]["series"]:
            sid = series["seriesID"]
            for obs in series["data"]:
                # BLS returns annotated rows for some series; the period
                # codes outside our CHECK constraint (M13 is fine; Q01..Q04
                # appear for some non-CPI series). We accept M01..M12, M13,
                # and S01/S02 here; anything else is dropped with a warning.
                period = obs["period"]
                if not (
                    (period.startswith("M") and len(period) == 3
                     and period[1:] in {f"{i:02d}" for i in range(1, 14)})
                    or period in {"S01", "S02"}
                ):
                    log.debug("Skipping unsupported period %s for series %s",
                              period, sid)
                    continue
                rows.append({
                    "series_id": sid,
                    "year":      int(obs["year"]),
                    "period":    period,
                    "value":     float(obs["value"]),
                })

    if not rows:
        raise IngestError(
            f"BLS returned zero observations for {series_ids!r} "
            f"in [{start_year}, {end_year}]; check series IDs."
        )

    df = pl.DataFrame(rows, schema={
        "series_id": pl.Utf8,
        "year":      pl.Int64,
        "period":    pl.Utf8,
        "value":     pl.Float64,
    })

    # Aggregate provenance: a single sha over the sorted concatenation of
    # per-chunk shas. This makes (source_url, source_sha256) a stable
    # fingerprint of a multi-chunk fetch.
    combined_sha = hashlib.sha256(
        "\n".join(sorted(sha_inputs)).encode("utf-8")
    ).hexdigest()

    return FetchResult(
        dataframe=df,
        source_url=BLS_API_URL,
        source_sha256=combined_sha,
        series_ids=tuple(series_ids),
        start_year=start_year,
        end_year=end_year,
        n_observations=df.height,
    )


# ============================================================================
# Stage + load
# ============================================================================


def stage_dataframe(result: FetchResult) -> pl.DataFrame:
    """Add provenance columns and return the DataFrame in raw.cpi_u shape."""
    return result.dataframe.with_columns(
        pl.lit(result.source_url).alias("source_url"),
        pl.lit(result.source_sha256).alias("source_sha256"),
    ).select([
        "series_id", "year", "period", "value",
        "source_url", "source_sha256",
    ])


# UPSERT, not COPY, because BLS revises SA series retroactively and we want
# the latest value to win without manual cleanup. Volume is small (a few
# thousand rows total for the canonical series) so per-row INSERT is fine.
_UPSERT_SQL: Final[str] = """
INSERT INTO raw.cpi_u
    (series_id, year, period, value, source_url, source_sha256)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (series_id, year, period) DO UPDATE SET
    value         = EXCLUDED.value,
    source_url    = EXCLUDED.source_url,
    source_sha256 = EXCLUDED.source_sha256,
    ingested_at   = now()
"""


def load_to_postgres(
    staged: pl.DataFrame,
    connection: psycopg.Connection,
) -> int:
    """UPSERT staged rows into raw.cpi_u. Returns number of rows touched."""
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
    """BLS CPI-U ingester (Tier 2)."""


@cli.command("fetch")
@click.option("--start-year", type=int, required=True)
@click.option("--end-year",   type=int, required=True)
@click.option("--series", multiple=True,
              help="BLS series IDs (repeat for multiple). Defaults to the "
                   "canonical 6-series catalog.")
def fetch_cmd(start_year: int, end_year: int, series: tuple[str, ...]) -> None:
    """Fetch CPI-U observations and print a summary. Does not touch the database."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    api_key = os.environ.get("BLS_API_KEY")
    sids = series or CANONICAL_SERIES
    result = fetch_cpi_series(
        sids, start_year=start_year, end_year=end_year, api_key=api_key,
    )
    click.echo(
        f"series={result.series_ids}\n"
        f"years=[{result.start_year}, {result.end_year}]\n"
        f"observations={result.n_observations}\n"
        f"sha256={result.source_sha256}\n"
    )
    click.echo(result.dataframe.head(10))


@cli.command("load")
@click.option("--start-year", type=int, required=True)
@click.option("--end-year",   type=int, required=True)
@click.option("--series", multiple=True,
              help="BLS series IDs (repeat). Defaults to the canonical catalog.")
@click.option("--dsn", envvar="PG_DSN", required=True,
              help="Postgres DSN (or set PG_DSN env var).")
def load_cmd(
    start_year: int, end_year: int, series: tuple[str, ...], dsn: str,
) -> None:
    """Fetch + UPSERT CPI-U observations into raw.cpi_u."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    api_key = os.environ.get("BLS_API_KEY")
    sids = series or CANONICAL_SERIES
    result = fetch_cpi_series(
        sids, start_year=start_year, end_year=end_year, api_key=api_key,
    )
    staged = stage_dataframe(result)
    log.info("Staged %d observations across %d series",
             staged.height, len(result.series_ids))

    import psycopg

    with psycopg.connect(dsn) as conn:
        n = load_to_postgres(staged, conn)
        conn.commit()
        click.echo(f"UPSERTed {n} rows into raw.cpi_u.")
