"""FHFA House Price Index (county-level annual all-transactions) ingester.

Downloads the single canonical workbook from FHFA's static URL, parses
the county sheet, and UPSERTs into ``raw.fhfa_hpi_county``.

The file is small (~5 MB) and updated quarterly. Each download is a
fresh full vintage; we do not attempt incremental loads.

Data documentation: https://www.fhfa.gov/data/house-price-index
File:               https://www.fhfa.gov/hpi/download/annual/hpi_at_county.xlsx
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
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

FHFA_HPI_COUNTY_URL: Final[str] = (
    "https://www.fhfa.gov/hpi/download/annual/hpi_at_county.xlsx"
)


# ============================================================================
# Fetch
# ============================================================================


def fetch_fhfa_county_workbook(
    *, dest_dir: Path, overwrite: bool = False, timeout_s: float = 120.0,
) -> Path:
    """Download the FHFA county HPI workbook to *dest_dir*.

    Returns the local path. If the workbook is already present and
    *overwrite* is False, returns the existing path without re-fetching.
    Filename is fixed (``hpi_at_county.xlsx``).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "hpi_at_county.xlsx"
    if dest.exists() and not overwrite:
        log.info("Skipping fetch (already present): %s", dest)
        return dest

    log.info("Fetching FHFA county HPI workbook: %s", FHFA_HPI_COUNTY_URL)
    with (
        httpx.Client(timeout=timeout_s, follow_redirects=True) as client,
        client.stream("GET", FHFA_HPI_COUNTY_URL) as resp,
    ):
        resp.raise_for_status()
        tmp = dest.with_suffix(dest.suffix + ".part")
        with tmp.open("wb") as fh:
            for chunk in resp.iter_bytes(chunk_size=1 << 20):
                fh.write(chunk)
        tmp.rename(dest)

    log.info("Downloaded %s (%.1f MiB)", dest, dest.stat().st_size / (1 << 20))
    return dest


# ============================================================================
# Parse
# ============================================================================


@dataclass(frozen=True)
class ParseResult:
    """Output of :func:`parse_fhfa_county_workbook`."""

    dataframe: pl.DataFrame
    source_url: str
    source_sha256: str
    source_vintage: str
    n_rows: int


# FHFA's county workbook columns (verified against the FY2024Q4 release).
# The published header row often starts a few rows down because the
# top of the sheet has notes/metadata. We canonicalize column names
# defensively rather than trust positional indexing.
_EXPECTED_COLUMNS: Final[frozenset[str]] = frozenset({
    "fips_code",
    "year",
    "annual_change_pct",
    "hpi",
})

# Column-name fallbacks observed across vintages. Map each to its
# canonical name in our staging shape.
_COLUMN_RENAMES: Final[dict[str, str]] = {
    # 2024Q4 release names
    "fips code":          "fips_code",
    "fips_code":          "fips_code",
    "year":               "year",
    "annual change (%)":  "annual_change_pct",
    "annual_change_(%)":  "annual_change_pct",
    "hpi":                "hpi",
    "hpi_with_2000_base": "hpi",
    "hpi (with 2000 base)": "hpi",
}


def _detect_header_row(raw: pl.DataFrame) -> int:
    """Return the 0-indexed row that contains the column headers.

    FHFA's workbooks lead with a metadata block (typically 1-3 rows of
    notes that span the entire row width). The header row is the first
    one whose CELLS individually equal 'year' and ones containing 'fips'
    (case-insensitive). We require an exact 'year' cell to discriminate
    from prose paragraphs that mention 'year' inside a sentence.
    """
    for i in range(min(20, raw.height)):
        cells = [str(c).strip().lower() if c is not None else "" for c in raw.row(i)]
        # Require an exact-match 'year' cell (not 'year' embedded in a
        # paragraph) AND a cell containing 'fips'.
        has_year_exact = any(c == "year" for c in cells)
        has_fips_cell  = any("fips" in c and len(c) <= 30 for c in cells)
        if has_year_exact and has_fips_cell:
            return i
    raise IngestError(
        "Could not locate header row in FHFA workbook; expected a row "
        "with an exact 'Year' cell and a 'FIPS'-bearing cell within the "
        "first 20 rows."
    )


def _canonicalize_columns(cols: list[object]) -> list[str]:
    """Map FHFA-published column names to our canonical names.

    Empty/None header cells get unique positional names (`_unused_N`)
    so polars does not reject duplicate column names. Downstream code
    only references the named canonical columns.
    """
    out: list[str] = []
    for i, c in enumerate(cols):
        if c is None or (isinstance(c, str) and not c.strip()):
            out.append(f"_unused_{i}")
            continue
        key = str(c).strip().lower()
        canonical = _COLUMN_RENAMES.get(
            key,
            key.replace(" ", "_").replace("(", "").replace(")", "").replace("%", "pct"),
        )
        out.append(canonical)
    return out


def parse_fhfa_county_workbook(path: Path) -> ParseResult:
    """Parse the FHFA county HPI workbook into a typed DataFrame.

    Returns a :class:`ParseResult` whose DataFrame has columns
    ``county_fips`` (str, 5-padded), ``year`` (Int64),
    ``hpi_at`` (Float64), ``annual_change`` (Float64, percent points),
    ``n_transactions`` (Int64 or NULL). Rows where any of the required
    fields are unparseable are dropped with a warning.
    """
    if not path.exists():
        raise IngestError(f"FHFA workbook not found: {path}")

    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()

    raw = pl.read_excel(path, engine="calamine", infer_schema_length=0)

    header_row = _detect_header_row(raw)
    headers = _canonicalize_columns(list(raw.row(header_row)))

    # Re-read with the detected header row.
    body = raw.slice(header_row + 1).rename({
        old: new for old, new in zip(raw.columns, headers, strict=True)
    })

    missing = _EXPECTED_COLUMNS - set(body.columns)
    if missing:
        raise IngestError(
            f"FHFA workbook missing expected columns: {sorted(missing)}; "
            f"got {body.columns!r}"
        )

    # Some FHFA releases include a "n_transactions" column under various
    # names; if absent, we fill with NULL.
    has_n_tx = any(c in body.columns for c in (
        "n_transactions", "transactions", "n_trans", "annual_n",
    ))
    n_tx_src = next(
        (c for c in ("n_transactions", "transactions", "n_trans", "annual_n")
         if c in body.columns),
        None,
    )

    n_tx_expr = (
        pl.col(n_tx_src).cast(pl.Int64, strict=False)
        if (has_n_tx and n_tx_src is not None)
        else pl.lit(None, dtype=pl.Int64)
    ).alias("n_transactions")
    selected = body.select([
        pl.col("fips_code").cast(pl.Utf8).str.zfill(5).alias("county_fips"),
        pl.col("year").cast(pl.Int64, strict=False).alias("year"),
        pl.col("hpi").cast(pl.Float64, strict=False).alias("hpi_at"),
        pl.col("annual_change_pct").cast(pl.Float64, strict=False).alias("annual_change"),
        n_tx_expr,
    ])

    # Drop rows where year or fips or hpi is null (these are typically
    # blank rows or metadata footers that survived the slice).
    pre_drop = selected.height
    cleaned = selected.filter(
        pl.col("year").is_not_null()
        & pl.col("county_fips").is_not_null()
        & pl.col("hpi_at").is_not_null()
        & (pl.col("hpi_at") > 0)
        & pl.col("year").is_between(1975, 2099)
        & (pl.col("county_fips").str.len_chars() == 5)
    )
    n_dropped = pre_drop - cleaned.height
    if n_dropped:
        log.warning("Dropped %d FHFA rows with NULL year/fips/hpi", n_dropped)

    if cleaned.height == 0:
        raise IngestError("FHFA workbook produced 0 valid rows after cleaning")

    # Vintage label: the most recent year present is the canonical
    # vintage marker. FHFA does not put a release date in the workbook
    # itself, but max(year) is a sufficient proxy for our purposes.
    max_year = int(cleaned["year"].max())  # type: ignore[arg-type]
    vintage = f"{max_year}-annual"

    return ParseResult(
        dataframe=cleaned,
        source_url=FHFA_HPI_COUNTY_URL,
        source_sha256=sha256,
        source_vintage=vintage,
        n_rows=cleaned.height,
    )


# ============================================================================
# Stage + load
# ============================================================================


def stage_dataframe(result: ParseResult) -> pl.DataFrame:
    """Add provenance columns; return DataFrame in raw.fhfa_hpi_county shape."""
    return result.dataframe.with_columns(
        pl.lit(result.source_url).alias("source_url"),
        pl.lit(result.source_sha256).alias("source_sha256"),
        pl.lit(result.source_vintage).alias("source_vintage"),
    ).select([
        "county_fips", "year", "hpi_at", "annual_change", "n_transactions",
        "source_url", "source_sha256", "source_vintage",
    ])


_UPSERT_SQL: Final[str] = """
INSERT INTO raw.fhfa_hpi_county
    (county_fips, year, hpi_at, annual_change, n_transactions,
     source_url, source_sha256, source_vintage)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (county_fips, year) DO UPDATE SET
    hpi_at         = EXCLUDED.hpi_at,
    annual_change  = EXCLUDED.annual_change,
    n_transactions = EXCLUDED.n_transactions,
    source_url     = EXCLUDED.source_url,
    source_sha256  = EXCLUDED.source_sha256,
    source_vintage = EXCLUDED.source_vintage,
    ingested_at    = now()
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
    """FHFA county HPI ingester (Tier 2)."""


@cli.command("fetch")
@click.option("--dest-dir", type=click.Path(file_okay=False, path_type=Path),
              default=Path("data/manual/fhfa_hpi"), show_default=True)
@click.option("--overwrite", is_flag=True, default=False)
def fetch_cmd(dest_dir: Path, overwrite: bool) -> None:
    """Download the FHFA county HPI workbook."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    dest = fetch_fhfa_county_workbook(dest_dir=dest_dir, overwrite=overwrite)
    click.echo(str(dest))


@cli.command("parse")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def parse_cmd(path: Path) -> None:
    """Parse an FHFA workbook and print a summary."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = parse_fhfa_county_workbook(path)
    yr_min = int(result.dataframe["year"].min())  # type: ignore[arg-type]
    yr_max = int(result.dataframe["year"].max())  # type: ignore[arg-type]
    click.echo(
        f"sha256={result.source_sha256}\n"
        f"vintage={result.source_vintage}\n"
        f"n_rows={result.n_rows}\n"
        f"year_range={yr_min}-{yr_max}\n"
        f"counties={result.dataframe['county_fips'].n_unique()}\n"
    )
    click.echo(result.dataframe.head(10))


@cli.command("load")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--dsn", envvar="PG_DSN", required=True)
def load_cmd(path: Path, dsn: str) -> None:
    """Parse + UPSERT FHFA county HPI into raw.fhfa_hpi_county."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = parse_fhfa_county_workbook(path)
    staged = stage_dataframe(result)

    import psycopg

    with psycopg.connect(dsn) as conn:
        n = load_to_postgres(staged, conn)
        conn.commit()
        click.echo(f"UPSERTed {n} rows into raw.fhfa_hpi_county.")
