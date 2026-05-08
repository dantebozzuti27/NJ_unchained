"""Zillow Home Value Index (ZHVI) county-level monthly ingester.

Downloads Zillow Research's headline county-level ZHVI CSV (single-family
+ condo, mid-tier 33-67%, smoothed, seasonally adjusted, monthly), filters
to NJ counties, and UPSERTs the long-format observations into
``raw.zillow_zhvi_county`` (Phase 6 substrate, migration 079).

Why this exists
---------------
Spec §3.2 explicitly names Zillow ZHVI / Redfin as preferred housing
indices for the platform. Until this ingester ships, the platform's only
county-level housing index is FHFA HPI -- a repeat-sales index built from
Fannie/Freddie conforming-loan data. ZHVI is independent (no overlap in
methodology, source data, or vintage cadence), which is exactly the
property spec §8.1 calls out as the substrate for cross-source
validation: when two methodologies agree, the signal is robust; when
they diverge, the divergence is itself a finding.

Source documentation
--------------------
* Public CSV downloads page: https://www.zillow.com/research/data/
* Source URL (county series, headline mid-tier sm sa monthly):
  https://files.zillowstatic.com/research/public_csvs/zhvi/
    County_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv

The CSV is wide-format: nine identifier columns + one column per
observation month (~313 months from 2000-01-31 through the most recent
publication). We melt to long so a row is (county, month, value) -- the
shape every downstream view assumes.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

import click
import httpx
import polars as pl

from ingestion._base import IngestError

if TYPE_CHECKING:
    import psycopg


log = logging.getLogger(__name__)


# ============================================================================
# Constants
# ============================================================================

ZHVI_COUNTY_URL: Final[str] = (
    "https://files.zillowstatic.com/research/public_csvs/zhvi/"
    "County_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv"
)

ZHVI_USER_AGENT: Final[str] = "Mozilla/5.0 (NJ-Affordability-Platform/0.0.1)"

# The nine non-month identifier columns Zillow publishes at the front of
# the wide CSV. We refuse to load if any of these is missing -- a schema
# change at Zillow should fail the ingest loudly rather than silently
# misalign columns.
_REQUIRED_ID_COLUMNS: Final[frozenset[str]] = frozenset({
    "RegionID",
    "SizeRank",
    "RegionName",
    "RegionType",
    "StateName",
    "State",
    "Metro",
    "StateCodeFIPS",
    "MunicipalCodeFIPS",
})

# Filter to the platform's home state. Phase 9 will widen this; for now
# every other row in the 3,073-row county CSV is dropped at parse time.
NJ_STATE_CODE: Final[str] = "NJ"
NJ_STATE_FIPS: Final[str] = "34"


# ============================================================================
# Fetch
# ============================================================================


@dataclass(frozen=True)
class FetchResult:
    """Output of :func:`fetch_zhvi_county_csv`."""

    path: Path
    sha256: str
    last_modified: dt.datetime | None


def _parse_last_modified(header_value: str | None) -> dt.datetime | None:
    """Parse an HTTP Last-Modified header into an aware UTC datetime.

    Returns None if the header is missing or unparseable. We use
    :func:`email.utils.parsedate_to_datetime` because it correctly
    handles the RFC 7231 IMF-fixdate format Zillow's CDN emits.
    """
    if not header_value:
        return None
    try:
        parsed = parsedate_to_datetime(header_value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def fetch_zhvi_county_csv(
    *, dest_dir: Path, overwrite: bool = False, timeout_s: float = 120.0,
) -> FetchResult:
    """Download Zillow's county-level ZHVI CSV to *dest_dir*.

    The CSV is small (~13 MB) and Zillow republishes monthly. Each
    download is a fresh full vintage; we do not attempt incremental
    fetches. If the file is already present and *overwrite* is False,
    returns the existing path with its on-disk SHA-256 and a None
    last_modified (we do not re-issue a HEAD request to refresh it).

    Provenance: SHA-256 of the bytes-on-disk + the CDN's Last-Modified
    header (RFC 7231 IMF-fixdate, parsed to a UTC datetime).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "County_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv"
    headers = {"User-Agent": ZHVI_USER_AGENT}

    if dest.exists() and not overwrite:
        log.info("Skipping fetch (already present): %s", dest)
        sha256 = hashlib.sha256(dest.read_bytes()).hexdigest()
        return FetchResult(path=dest, sha256=sha256, last_modified=None)

    log.info("Fetching Zillow ZHVI county CSV: %s", ZHVI_COUNTY_URL)
    with (
        httpx.Client(timeout=timeout_s, follow_redirects=True, headers=headers) as client,
        client.stream("GET", ZHVI_COUNTY_URL) as resp,
    ):
        resp.raise_for_status()
        last_modified = _parse_last_modified(resp.headers.get("last-modified"))
        h = hashlib.sha256()
        tmp = dest.with_suffix(dest.suffix + ".part")
        with tmp.open("wb") as fh:
            for chunk in resp.iter_bytes(chunk_size=1 << 20):
                fh.write(chunk)
                h.update(chunk)
        tmp.rename(dest)
        sha256 = h.hexdigest()

    log.info(
        "Downloaded %s (%.2f MiB, sha256=%s..., last_modified=%s)",
        dest, dest.stat().st_size / (1 << 20), sha256[:12], last_modified,
    )
    return FetchResult(path=dest, sha256=sha256, last_modified=last_modified)


# ============================================================================
# Parse
# ============================================================================


@dataclass(frozen=True)
class ParseResult:
    """Output of :func:`parse_zhvi_county_csv`."""

    dataframe: pl.DataFrame
    source_url: str
    source_sha256: str
    source_modified_at: dt.datetime | None
    source_vintage: str
    n_counties: int
    n_observations: int


def parse_zhvi_county_csv(
    csv_path: Path,
    *,
    sha256: str,
    last_modified: dt.datetime | None,
    state_code: str = NJ_STATE_CODE,
) -> ParseResult:
    """Parse Zillow's wide county CSV -> long-format Polars DataFrame.

    Steps:
      1. Read with polars (header row = row 0; everything else is data).
      2. Verify every required identifier column is present (schema-version
         guard against a future Zillow rename).
      3. Filter to the requested state code (default 'NJ').
      4. Construct a 5-digit FIPS by concatenating StateCodeFIPS +
         MunicipalCodeFIPS (Zillow stores them split as '34' + '003' for
         Bergen County).
      5. Melt the month-named value columns to long format, dropping any
         month with a NULL value (some early-2000s rows have NULLs while
         Zillow was bootstrapping coverage).
      6. Cast observation_month to DATE and zhvi to FLOAT64.

    Returns a ParseResult with one row per (county_fips, observation_month).
    """
    if not csv_path.exists():
        raise IngestError(f"ZHVI CSV not found: {csv_path}")

    raw = pl.read_csv(csv_path, infer_schema_length=0)
    if raw.height == 0:
        raise IngestError(f"ZHVI CSV is empty: {csv_path}")

    columns = list(raw.columns)
    missing = _REQUIRED_ID_COLUMNS - set(columns)
    if missing:
        raise IngestError(
            f"ZHVI CSV missing required identifier columns: {sorted(missing)}; "
            f"observed first 12={columns[:12]!r}"
        )

    # Month columns are everything that isn't an identifier.
    month_cols = [c for c in columns if c not in _REQUIRED_ID_COLUMNS]
    if not month_cols:
        raise IngestError(
            f"ZHVI CSV has identifier columns but no month columns; "
            f"file appears truncated. Columns: {columns!r}"
        )

    # Validate that every month_col parses as YYYY-MM-DD.
    for m in month_cols:
        try:
            dt.date.fromisoformat(m)
        except ValueError as exc:
            raise IngestError(
                f"ZHVI CSV column {m!r} is not a YYYY-MM-DD date; "
                f"Zillow may have changed the wide-CSV layout."
            ) from exc

    # Filter to the requested state code BEFORE melting, so we melt
    # 21 NJ rows x ~313 months instead of 3,073 x ~313.
    nj_only = raw.filter(pl.col("State") == state_code)
    if nj_only.height == 0:
        raise IngestError(
            f"ZHVI CSV produced 0 rows for state {state_code!r}. "
            f"Available State values (first 20): "
            f"{sorted(set(raw.get_column('State').to_list()))[:20]}"
        )

    # Construct the 5-digit FIPS.
    nj_with_fips = nj_only.with_columns(
        (pl.col("StateCodeFIPS").cast(pl.Utf8)
         + pl.col("MunicipalCodeFIPS").cast(pl.Utf8)).alias("county_fips"),
    )

    # Melt to long.
    long = nj_with_fips.unpivot(
        index=[
            "RegionID", "RegionName", "State", "StateCodeFIPS",
            "MunicipalCodeFIPS", "Metro", "county_fips",
        ],
        on=month_cols,
        variable_name="observation_month",
        value_name="zhvi_str",
    )

    # Drop rows with no published value for that month, then cast.
    long_typed = (
        long.filter(pl.col("zhvi_str").is_not_null())
            .filter(pl.col("zhvi_str") != "")
            .with_columns([
                pl.col("observation_month").str.to_date(format="%Y-%m-%d"),
                pl.col("zhvi_str").cast(pl.Float64).alias("zhvi"),
            ])
            .drop("zhvi_str")
    )

    if long_typed.height == 0:
        raise IngestError(
            f"ZHVI CSV produced 0 typed observations for state {state_code!r}. "
            "Either every row had NULL values or the cast to Float64 failed."
        )

    # Sanity check: every value must be strictly positive (ZHVI is a
    # dollar-denominated home value; 0 or negative would indicate a
    # parsing bug).
    nonpos = long_typed.filter(pl.col("zhvi") <= 0).height
    if nonpos > 0:
        raise IngestError(
            f"ZHVI CSV produced {nonpos} non-positive observations after parse; "
            "expected every value to be strictly positive."
        )

    n_counties = long_typed.select(pl.col("county_fips").n_unique()).item()
    vintage = (
        last_modified.date().isoformat() if last_modified is not None
        else dt.date.today().isoformat()
    )

    final = long_typed.select([
        pl.col("RegionID").cast(pl.Int64).alias("region_id"),
        pl.col("county_fips"),
        pl.col("RegionName").alias("region_name"),
        pl.col("State").alias("state_code"),
        pl.col("Metro").alias("metro"),
        pl.col("observation_month"),
        pl.col("zhvi"),
    ])

    return ParseResult(
        dataframe=final,
        source_url=ZHVI_COUNTY_URL,
        source_sha256=sha256,
        source_modified_at=last_modified,
        source_vintage=f"zhvi-county-{vintage}",
        n_counties=int(n_counties),
        n_observations=final.height,
    )


# ============================================================================
# Stage + load
# ============================================================================


def stage_dataframe(result: ParseResult) -> pl.DataFrame:
    """Add provenance columns; return DataFrame ready to UPSERT."""
    return result.dataframe.with_columns([
        pl.lit(result.source_url).alias("source_url"),
        pl.lit(result.source_sha256).alias("source_sha256"),
        pl.lit(result.source_modified_at).alias("source_modified_at"),
        pl.lit(result.source_vintage).alias("source_vintage"),
    ])


_UPSERT_COLS: Final[tuple[str, ...]] = (
    "region_id", "county_fips", "region_name", "state_code", "metro",
    "observation_month", "zhvi",
    "source_url", "source_sha256", "source_modified_at", "source_vintage",
)


_UPSERT_SQL: Final[str] = f"""
INSERT INTO raw.zillow_zhvi_county
    ({", ".join(_UPSERT_COLS)})
VALUES ({", ".join(["%s"] * len(_UPSERT_COLS))})
ON CONFLICT (county_fips, observation_month) DO UPDATE SET
    region_id           = EXCLUDED.region_id,
    region_name         = EXCLUDED.region_name,
    state_code          = EXCLUDED.state_code,
    metro               = EXCLUDED.metro,
    zhvi                = EXCLUDED.zhvi,
    source_url          = EXCLUDED.source_url,
    source_sha256       = EXCLUDED.source_sha256,
    source_modified_at  = EXCLUDED.source_modified_at,
    source_vintage      = EXCLUDED.source_vintage,
    ingested_at         = now()
"""


def load_to_postgres(
    staged: pl.DataFrame,
    connection: psycopg.Connection,
    *,
    batch_size: int = 5000,
) -> int:
    """UPSERT staged rows into raw.zillow_zhvi_county. Returns rows touched.

    Uses ``executemany`` in batches of *batch_size* so a fresh load of
    ~6,500 NJ rows fits in a single round-trip per batch (psycopg's
    pipeline mode is not yet supported by Neon's HTTP shim, so we keep
    the implementation portable).
    """
    rows = list(staged.select(_UPSERT_COLS).iter_rows())
    if not rows:
        return 0

    n = 0
    with connection.cursor() as cur:
        for start in range(0, len(rows), batch_size):
            chunk = rows[start:start + batch_size]
            cur.executemany(_UPSERT_SQL, chunk)
            n += len(chunk)
    return n


# ============================================================================
# CLI
# ============================================================================


@click.group()
def cli() -> None:
    """Zillow Home Value Index (ZHVI) ingester (Phase 6)."""


@cli.command("fetch")
@click.option("--dest-dir", type=click.Path(file_okay=False, path_type=Path),
              default=Path("data/manual/zillow_zhvi"), show_default=True)
@click.option("--overwrite", is_flag=True, default=False)
def fetch_cmd(dest_dir: Path, overwrite: bool) -> None:
    """Download the latest ZHVI county CSV."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = fetch_zhvi_county_csv(dest_dir=dest_dir, overwrite=overwrite)
    click.echo(
        f"path={result.path}\n"
        f"sha256={result.sha256}\n"
        f"last_modified={result.last_modified}\n"
    )


@cli.command("parse")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--state-code", default=NJ_STATE_CODE, show_default=True)
def parse_cmd(path: Path, state_code: str) -> None:
    """Parse a ZHVI CSV and print a summary."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    result = parse_zhvi_county_csv(
        path, sha256=sha256, last_modified=None, state_code=state_code,
    )
    click.echo(
        f"sha256={result.source_sha256}\n"
        f"vintage={result.source_vintage}\n"
        f"n_counties={result.n_counties}\n"
        f"n_observations={result.n_observations}\n"
    )
    click.echo(result.dataframe.head(10))
    click.echo(result.dataframe.tail(5))


@cli.command("load")
@click.option("--dest-dir", type=click.Path(file_okay=False, path_type=Path),
              default=Path("data/manual/zillow_zhvi"), show_default=True)
@click.option("--overwrite", is_flag=True, default=False)
@click.option("--state-code", default=NJ_STATE_CODE, show_default=True)
@click.option("--dsn", envvar="PG_DSN", required=True)
def load_cmd(
    dest_dir: Path, overwrite: bool, state_code: str, dsn: str,
) -> None:
    """Fetch + parse + UPSERT the latest ZHVI county vintage."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    fetched = fetch_zhvi_county_csv(dest_dir=dest_dir, overwrite=overwrite)
    parsed = parse_zhvi_county_csv(
        fetched.path,
        sha256=fetched.sha256,
        last_modified=fetched.last_modified,
        state_code=state_code,
    )
    staged = stage_dataframe(parsed)

    import psycopg

    with psycopg.connect(dsn) as conn:
        n = load_to_postgres(staged, conn)
        conn.commit()

    click.echo(
        f"UPSERTed {n} rows into raw.zillow_zhvi_county "
        f"({parsed.n_counties} counties x ~{n // max(parsed.n_counties, 1)} months/county) "
        f"from vintage {parsed.source_vintage}."
    )


if __name__ == "__main__":
    cli()
