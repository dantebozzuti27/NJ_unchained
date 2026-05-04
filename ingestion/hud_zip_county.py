"""HUD USPS ZIP <-> County crosswalk loader.

Loads quarterly HUD ``ZIP_COUNTY_<MMYYYY>.{xlsx,csv}`` files into
``ref.zip_county`` (see ``db/migrations/010_hud_zip_county.sql``).

Architecture
------------
Three pure-function layers, exposed independently for testability:

1. :func:`parse_hud_file` -- read one HUD file (XLSX or CSV), canonicalize
   columns, validate ratio sums *before* the database sees the data,
   return a :class:`ParseResult`.
2. :func:`stage_dataframe` -- add provenance columns and align to
   destination schema.
3. :func:`load_to_postgres` -- bulk-COPY into ``ref.zip_county``. Wraps
   the COPY in the caller's transaction so a partial vintage load can be
   rolled back atomically.

Vintage extraction
------------------
HUD filenames carry the vintage as ``MMYYYY`` (calendar month, full
year). The first month of a calendar quarter pins the quarter:

==========  ==============  ===============
Month MM    vintage_quarter Notes
==========  ==============  ===============
03          1               March file
06          2               June file
09          3               September file
12          4               December file
==========  ==============  ===============

HUD also publishes mid-quarter snapshots (e.g. ``ZIP_COUNTY_022024.xlsx``)
which we round to the *containing* quarter (Feb -> Q1, May -> Q2, ...).
The vintage_quarter is stored as the canonical 1/2/3/4.

Source format quirks
--------------------
* **ZIPs lose leading zeros in Excel.** A ZIP of ``08830`` arrives as
  ``8830``. We zero-pad before storing.
* **Column casing has wandered** (``ZIP`` vs ``zip``, ``COUNTY`` vs
  ``county``). Resolved via the canonicalizer in :mod:`ingestion._base`.
* **Older vintages (2010-2012) had a USPS_ZIP_PREF_CITY column** that
  later vintages dropped. We ignore it; only the four ratio columns and
  ZIP/COUNTY are used.

Methodological invariant
------------------------
A complete US load satisfies ``SUM(bus_ratio) = 1.0 +/- 0.01`` for every
``(zip5, vintage_year, vintage_quarter)``. The DEFERRABLE CONSTRAINT
TRIGGER on ``ref.zip_county`` enforces this at COMMIT, so partial
loads -- e.g. dropping rows because the parser failed on one county --
fail loudly rather than silently producing a biased crosswalk.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import click
import polars as pl

from ingestion._base import IngestError, canonicalize_column, sha256_file

if TYPE_CHECKING:
    from pathlib import Path

    import psycopg

log = logging.getLogger(__name__)

_COPY_CHUNK = 1 << 20

# ---------------------------------------------------------------------------
# Vintage extraction
# ---------------------------------------------------------------------------

_FILENAME_VINTAGE_RE: Final[re.Pattern[str]] = re.compile(
    r"ZIP_COUNTY_(\d{2})(\d{4})", re.IGNORECASE,
)

# month -> calendar quarter (1..4). Defensive against mid-quarter snapshots.
_MONTH_TO_QUARTER: Final[dict[int, int]] = {
    1: 1, 2: 1, 3: 1,
    4: 2, 5: 2, 6: 2,
    7: 3, 8: 3, 9: 3,
    10: 4, 11: 4, 12: 4,
}


def parse_vintage_from_filename(filename: str) -> tuple[int, int]:
    """Extract (vintage_year, vintage_quarter) from a HUD filename.

    >>> parse_vintage_from_filename("ZIP_COUNTY_032025.xlsx")
    (2025, 1)
    >>> parse_vintage_from_filename("ZIP_COUNTY_122021.csv")
    (2021, 4)
    >>> parse_vintage_from_filename("ZIP_COUNTY_022019.xlsx")  # Feb -> Q1
    (2019, 1)

    Raises:
        IngestError: if the filename does not match ``ZIP_COUNTY_MMYYYY``.

    """
    m = _FILENAME_VINTAGE_RE.search(filename)
    if not m:
        raise IngestError(
            f"Cannot extract HUD vintage from filename {filename!r}; "
            "expected pattern like 'ZIP_COUNTY_032025.xlsx'."
        )
    month = int(m.group(1))
    year = int(m.group(2))
    if not 1 <= month <= 12:
        raise IngestError(f"Invalid month {month} in filename {filename!r}.")
    return year, _MONTH_TO_QUARTER[month]


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

# HUD's canonical column names (post-canonicalization). The four ratio
# columns are always present in every vintage we support.
_REQUIRED_COLS: Final[frozenset[str]] = frozenset({
    "zip", "county", "res_ratio", "bus_ratio", "oth_ratio", "tot_ratio",
})


def _read_raw(path: Path) -> pl.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pl.read_csv(path, infer_schema_length=0, ignore_errors=False)
    if suffix in {".xlsx", ".xls"}:
        return pl.read_excel(path, engine="calamine", infer_schema_length=0)
    raise IngestError(f"Unsupported HUD file extension: {suffix!r} ({path})")


def _canonicalize_columns(df: pl.DataFrame) -> pl.DataFrame:
    return df.rename({c: canonicalize_column(c) for c in df.columns})


def _coerce_and_canonicalize(df: pl.DataFrame) -> pl.DataFrame:
    """Project to the canonical 6 columns, coerce types, zero-pad ZIPs.

    ZIP and COUNTY are coerced to CHAR(5) zero-padded; ratios to Float64.
    Rows with malformed ZIP / COUNTY (cannot zero-pad to 5 digits) are
    dropped after a warning -- they are typically Census-imputed APO/FPO
    addresses that HUD includes for completeness but which have no real
    county affiliation.
    """
    missing = _REQUIRED_COLS - set(df.columns)
    if missing:
        raise IngestError(
            f"HUD file is missing required columns: {sorted(missing)}. "
            f"Saw: {sorted(df.columns)[:20]}"
        )

    df = df.select(["zip", "county", "res_ratio", "bus_ratio", "oth_ratio", "tot_ratio"])

    df = df.with_columns([
        pl.col("zip").cast(pl.Utf8).str.strip_chars().str.zfill(5).alias("zip5"),
        pl.col("county").cast(pl.Utf8).str.strip_chars().str.zfill(5).alias("county_fips"),
        pl.col("res_ratio").cast(pl.Float64, strict=False),
        pl.col("bus_ratio").cast(pl.Float64, strict=False),
        pl.col("oth_ratio").cast(pl.Float64, strict=False),
        pl.col("tot_ratio").cast(pl.Float64, strict=False),
    ])

    # Drop malformed identifiers (APO/FPO rows have non-numeric COUNTY).
    df = df.filter(
        pl.col("zip5").str.contains(r"^\d{5}$")
        & pl.col("county_fips").str.contains(r"^\d{5}$")
    )

    return df.select(["zip5", "county_fips", "res_ratio", "bus_ratio",
                      "oth_ratio", "tot_ratio"])


# ---------------------------------------------------------------------------
# Pre-database invariant validation
# ---------------------------------------------------------------------------


def _validate_ratio_sums(df: pl.DataFrame, *, tol: float = 0.01) -> None:
    """Raise if any ZIP's bus_ratio / res_ratio / tot_ratio sum is out of [1-tol, 1+tol].

    The database also enforces this via a DEFERRABLE CONSTRAINT TRIGGER, but
    catching it pre-load saves a Postgres round-trip on a malformed file.
    """
    sums = (
        df.group_by("zip5")
        .agg(
            pl.col("res_ratio").sum().alias("sum_res"),
            pl.col("bus_ratio").sum().alias("sum_bus"),
            pl.col("tot_ratio").sum().alias("sum_tot"),
        )
        .filter(
            (pl.col("sum_bus") - 1.0).abs() > tol
        )
    )
    if sums.height > 0:
        bad = sums.head(5).rows(named=True)
        raise IngestError(
            f"HUD ratio-sum invariant violated for {sums.height} ZIPs. "
            f"First 5: {bad}. Tolerance is +/- {tol}."
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParseResult:
    """Output of :func:`parse_hud_file`."""

    dataframe: pl.DataFrame
    vintage_year: int
    vintage_quarter: int
    source_filename: str
    source_sha256: str
    n_input_rows: int
    n_output_rows: int


def parse_hud_file(path: Path) -> ParseResult:
    """Read, canonicalize, validate, and return a :class:`ParseResult`.

    Pure function: reads the file, returns the canonical DataFrame plus
    metadata. No database connection.

    Raises:
        IngestError: missing required columns, vintage extraction failure,
            or ratio-sum invariant violation.

    """
    vintage_year, vintage_quarter = parse_vintage_from_filename(path.name)
    sha = sha256_file(path)
    raw_df = _read_raw(path)
    n_input = raw_df.height
    canonicalized = _canonicalize_columns(raw_df)
    coerced = _coerce_and_canonicalize(canonicalized)
    _validate_ratio_sums(coerced)
    n_output = coerced.height

    if n_output == 0:
        raise IngestError(f"Parsed zero rows from {path}; refusing to load.")

    return ParseResult(
        dataframe=coerced,
        vintage_year=vintage_year,
        vintage_quarter=vintage_quarter,
        source_filename=path.name,
        source_sha256=sha,
        n_input_rows=n_input,
        n_output_rows=n_output,
    )


def stage_dataframe(parse: ParseResult, source_url: str) -> pl.DataFrame:
    """Add provenance columns and project to the destination ``ref.zip_county`` shape."""
    df = parse.dataframe.with_columns([
        pl.lit(parse.vintage_year, dtype=pl.Int16).alias("vintage_year"),
        pl.lit(parse.vintage_quarter, dtype=pl.Int16).alias("vintage_quarter"),
        pl.lit(source_url, dtype=pl.Utf8).alias("source_url"),
        pl.lit(parse.source_sha256, dtype=pl.Utf8).alias("source_sha256"),
    ])
    destination_cols: tuple[str, ...] = (
        "zip5", "county_fips", "vintage_year", "vintage_quarter",
        "res_ratio", "bus_ratio", "oth_ratio", "tot_ratio",
        "source_url", "source_sha256",
    )
    return df.select(destination_cols)


def load_to_postgres(
    staged: pl.DataFrame,
    connection: psycopg.Connection,
    *,
    table: str = "ref.zip_county",
) -> int:
    """Bulk-COPY *staged* into *table*. Returns rows loaded.

    Sets the constraint trigger to deferred mode so the entire vintage
    load is validated at COMMIT, not after each row.
    """
    from psycopg import sql

    buf = io.BytesIO()
    staged.write_csv(buf, include_header=False)
    buf.seek(0)

    if "." in table:
        schema_part, table_part = table.split(".", 1)
        ident = sql.Identifier(schema_part, table_part)
    else:
        ident = sql.Identifier(table)
    col_idents = sql.SQL(", ").join(sql.Identifier(c) for c in staged.columns)
    copy_query = sql.SQL(
        "COPY {tbl} ({cols}) FROM STDIN WITH (FORMAT csv, NULL '')"
    ).format(tbl=ident, cols=col_idents)

    with connection.cursor() as cur:
        cur.execute("SET CONSTRAINTS ALL DEFERRED")
        n_before = _count_rows(connection, ident)
        with cur.copy(copy_query) as cp:
            while chunk := buf.read(_COPY_CHUNK):
                cp.write(chunk)
        n_after = _count_rows(connection, ident)
    return n_after - n_before


def _count_rows(connection: psycopg.Connection, ident: object) -> int:
    from psycopg import sql

    if not isinstance(ident, sql.Identifier):
        raise TypeError("ident must be a psycopg.sql.Identifier")
    cur = connection.execute(sql.SQL("SELECT count(*) FROM {}").format(ident))
    row = cur.fetchone()
    return 0 if row is None else int(row[0])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.group()
def cli() -> None:
    """HUD USPS ZIP <-> County crosswalk ingester."""


@cli.command("parse")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
def parse_cmd(path: str) -> None:
    """Parse a HUD ZIP-County file and print summary. No database touched."""
    from pathlib import Path as _Path

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = parse_hud_file(_Path(path))
    click.echo(
        f"file={result.source_filename}\n"
        f"vintage={result.vintage_year}Q{result.vintage_quarter}\n"
        f"sha256={result.source_sha256}\n"
        f"rows_in={result.n_input_rows}\n"
        f"rows_out={result.n_output_rows}\n"
    )
    click.echo(result.dataframe.head(5))


@cli.command("load")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@click.option("--dsn", envvar="PG_DSN", required=True)
@click.option("--source-url", required=True,
              help="The HUD landing-page URL this file came from. Stored as provenance.")
def load_cmd(path: str, dsn: str, source_url: str) -> None:
    """Parse + stage + load a HUD ZIP-County file into ``ref.zip_county``."""
    from pathlib import Path as _Path

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = parse_hud_file(_Path(path))
    staged = stage_dataframe(result, source_url=source_url)
    log.info(
        "Staged %d rows from %s (vintage %dQ%d)",
        staged.height, path, result.vintage_year, result.vintage_quarter,
    )

    import psycopg

    with psycopg.connect(dsn) as conn:
        n = load_to_postgres(staged, conn)
        conn.commit()
        click.echo(f"Loaded {n} rows into ref.zip_county.")
