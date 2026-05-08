"""NJ DCA property tax (county- and municipality-level annual) ingester.

Downloads `{YY}taxes.xls` from nj.gov/dca/dlgs/resources/Property_Tax/
and extracts both:

  * The 'County Tax Summary' sheet's county-level rows (MuniCode ends
    in '00') into ``raw.nj_property_tax_county`` (Phase 2 substrate).

  * The 'Municipal Tax Summary' sheet's per-muni rows (MuniCode 4
    digits, NOT ending in '00') into ``raw.nj_property_tax_muni``
    (Phase 8a substrate).

Why this exists:
    NJ has the highest effective property tax rate in the U.S. (~2.2%).
    Without property tax, any NJ housing-burden ratio is significantly
    understated. The county-level rows produce the headline numbers
    for NJ's 21 counties; the muni-level rows produce the same
    headline numbers for each of NJ's 564 incorporated municipalities,
    enabling town-level personalization (Phase 8 of VISION_2026.md).

Source documentation:
    https://www.nj.gov/dca/dlgs/resources/Property_Tax_info.shtml
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

DCA_URL_TEMPLATE: Final[str] = (
    "https://www.nj.gov/dca/dlgs/resources/Property_Tax/{yy}_data/{yy}taxes.xls"
)

DCA_USER_AGENT: Final[str] = "Mozilla/5.0 (NJ-Affordability-Platform/0.0.1)"

# DCA codes its counties 01..21 in alphabetical order; FIPS county codes
# in NJ are odd numbers 001..041 in the same order. We resolve to FIPS
# at load time via a hardcoded mapping (also seeded as ref.nj_dca_county
# in the SQL migration; kept in Python for early validation).
_DCA_TO_FIPS: Final[dict[str, str]] = {
    "01": "34001",  # Atlantic
    "02": "34003",  # Bergen
    "03": "34005",  # Burlington
    "04": "34007",  # Camden
    "05": "34009",  # Cape May
    "06": "34011",  # Cumberland
    "07": "34013",  # Essex
    "08": "34015",  # Gloucester
    "09": "34017",  # Hudson
    "10": "34019",  # Hunterdon
    "11": "34021",  # Mercer
    "12": "34023",  # Middlesex
    "13": "34025",  # Monmouth
    "14": "34027",  # Morris
    "15": "34029",  # Ocean
    "16": "34031",  # Passaic
    "17": "34033",  # Salem
    "18": "34035",  # Somerset
    "19": "34037",  # Sussex
    "20": "34039",  # Union
    "21": "34041",  # Warren
}

# Earliest year DCA publishes the modern multi-sheet workbook with a
# named "County Tax Summary" sheet. Older years (2012-2015) use a single
# flat "{YYYY} Taxes" sheet with municipality-level rows only and no
# pre-aggregated county summary; supporting them requires a separate
# municipal-aggregation code path which is deferred. Years before 2012
# are 404 at the canonical URL.
DCA_EARLIEST_YEAR: Final[int] = 2016


# ============================================================================
# Fetch
# ============================================================================


def build_dca_url(year: int) -> str:
    """Return the canonical DCA workbook URL for *year*.

    NJ DCA encodes year as a 2-digit suffix (e.g. ``24taxes.xls`` for
    2024). We refuse to construct URLs for years outside the documented
    range to avoid silent 404s.
    """
    if year < DCA_EARLIEST_YEAR or year > 2099:
        raise IngestError(
            f"NJ DCA workbook year out of supported range: {year}; "
            f"expected {DCA_EARLIEST_YEAR}..2099"
        )
    yy = f"{year % 100:02d}"
    return DCA_URL_TEMPLATE.format(yy=yy)


def fetch_dca_workbook(
    year: int, *, dest_dir: Path, overwrite: bool = False, timeout_s: float = 60.0,
) -> Path:
    """Download the DCA county-tax workbook for *year* into *dest_dir*."""
    url = build_dca_url(year)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{year % 100:02d}taxes.xls"
    if dest.exists() and not overwrite:
        log.info("Skipping fetch (already present): %s", dest)
        return dest

    log.info("Fetching NJ DCA tax workbook: %s", url)
    headers = {"User-Agent": DCA_USER_AGENT}
    with (
        httpx.Client(timeout=timeout_s, follow_redirects=True, headers=headers) as client,
        client.stream("GET", url) as resp,
    ):
        resp.raise_for_status()
        tmp = dest.with_suffix(dest.suffix + ".part")
        with tmp.open("wb") as fh:
            for chunk in resp.iter_bytes(chunk_size=1 << 20):
                fh.write(chunk)
        tmp.rename(dest)

    log.info("Downloaded %s (%.2f MiB)", dest, dest.stat().st_size / (1 << 20))
    return dest


# ============================================================================
# Parse
# ============================================================================


@dataclass(frozen=True)
class ParseResult:
    """Output of :func:`parse_dca_workbook`."""

    dataframe: pl.DataFrame
    source_url: str
    source_sha256: str
    source_vintage: str
    year: int
    n_rows: int


# Map DCA's published header names to our canonical column names. This is
# more brittle than the FHFA case (DCA varies a header name across years),
# so we keep this mapping in sync as new vintages reveal renamings.
_HEADER_RENAMES: Final[dict[str, str]] = {
    "municode":                                                "muni_code",
    "county":                                                  "county_name_full",
    "net valuation taxable":                                   "net_valuation_taxable",
    "total county levy":                                       "total_county_levy",
    "total school levy":                                       "total_school_levy",
    "total local municipal tax levy":                          "total_municipal_levy",
    "total levy on which tax rate is computed":                "total_levy",
    "cy total rate":                                           "cy_total_rate",
    "cy county rate":                                          "cy_county_rate",
    "cy school rate":                                          "cy_school_rate",
    "cy total municipal rate":                                 "cy_municipal_rate",
    "average residential property value":                      "avg_residential_value",
    "average total property taxes "
        "(not including credits and deductions)":              "avg_total_property_taxes",
    "average county taxes":                                    "avg_county_taxes",
    "average school taxes":                                    "avg_school_taxes",
    "average municipal taxes":                                 "avg_municipal_taxes",
    "cy equalized property value (pre-appeal)":                "cy_equalized_property_value",
    "cy total eq rate (reap not included)":                    "cy_total_eq_rate",
}


_REQUIRED_CANONICAL: Final[frozenset[str]] = frozenset({
    "muni_code", "avg_total_property_taxes", "cy_total_rate",
    "avg_residential_value",
})


def _canonicalize_dca_columns(cols: list[object]) -> list[str]:
    """Map DCA-published column names to our canonical names; dedupe duplicates."""
    out: list[str] = []
    seen: dict[str, int] = {}
    for i, c in enumerate(cols):
        if c is None or (isinstance(c, str) and not c.strip()):
            out.append(f"_unused_{i}")
            continue
        key = str(c).strip().lower()
        canonical = _HEADER_RENAMES.get(
            key,
            key.replace(" ", "_").replace("(", "").replace(")", "").replace("%", "pct"),
        )
        # The "County" header appears twice in DCA workbooks (once for
        # full name, once for short name). Suffix duplicates so polars
        # doesn't reject the rename.
        if canonical in seen:
            seen[canonical] += 1
            canonical = f"{canonical}_{seen[canonical]}"
        else:
            seen[canonical] = 0
        out.append(canonical)
    return out


def parse_dca_workbook(path: Path, *, year: int) -> ParseResult:
    """Parse a DCA tax workbook -> typed county-level DataFrame.

    Reads the "County Tax Summary" sheet, keeps only county-summary rows
    (MuniCode ends in "00"), maps DCA codes to FIPS, types the numeric
    columns. Returns a :class:`ParseResult` with one row per NJ county.
    """
    if not path.exists():
        raise IngestError(f"DCA workbook not found: {path}")

    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()

    raw = pl.read_excel(
        path, engine="calamine",
        sheet_name="County Tax Summary",
        infer_schema_length=0,
    )
    if raw.height == 0:
        raise IngestError(f"DCA County Tax Summary sheet is empty: {path}")

    # Row 0 of the sheet is the header row.
    headers = _canonicalize_dca_columns(list(raw.row(0)))
    body = raw.slice(1).rename({
        old: new for old, new in zip(raw.columns, headers, strict=True)
    })

    missing = _REQUIRED_CANONICAL - set(body.columns)
    if missing:
        raise IngestError(
            f"DCA workbook for year {year} missing canonical columns: "
            f"{sorted(missing)}; available={body.columns!r}"
        )

    # Numeric columns we expect; cast each non-strict (turns blanks to null).
    numeric_cols = [
        "net_valuation_taxable", "total_county_levy", "total_school_levy",
        "total_municipal_levy", "total_levy",
        "cy_total_rate", "cy_county_rate", "cy_school_rate", "cy_municipal_rate",
        "avg_residential_value", "avg_total_property_taxes",
        "avg_county_taxes", "avg_school_taxes", "avg_municipal_taxes",
        "cy_equalized_property_value", "cy_total_eq_rate",
    ]
    cast_exprs = [
        pl.col(c).cast(pl.Float64, strict=False).alias(c)
        for c in numeric_cols if c in body.columns
    ]
    typed = body.with_columns(cast_exprs)

    # Filter to county-level summary rows: MuniCode is 4 chars, ending in
    # "00". DCA emits one such row per county at the start of each
    # county's section.
    county_only = typed.filter(
        pl.col("muni_code").is_not_null()
        & (pl.col("muni_code").str.len_chars() == 4)
        & pl.col("muni_code").str.ends_with("00")
    )

    if county_only.height == 0:
        raise IngestError(
            f"DCA workbook for year {year} produced 0 county-summary rows; "
            "MuniCode column may have changed format."
        )

    # Map MuniCode (e.g. '0100') -> DCA county code ('01') -> FIPS ('34001').
    dca_codes = county_only.with_columns(
        pl.col("muni_code").str.slice(0, 2).alias("dca_code"),
    )

    # Apply the FIPS mapping; rows whose dca_code is not in our table get
    # dropped (with a warning). All 21 NJ counties should map.
    rows: list[dict[str, object]] = []
    for row in dca_codes.iter_rows(named=True):
        dca_code = row["dca_code"]
        fips = _DCA_TO_FIPS.get(dca_code)
        if fips is None:
            log.warning("Skipping unknown DCA code %r in %s", dca_code, path)
            continue
        rows.append({
            "county_fips":                 fips,
            "year":                        year,
            "net_valuation_taxable":       row.get("net_valuation_taxable"),
            "total_county_levy":           row.get("total_county_levy"),
            "total_school_levy":           row.get("total_school_levy"),
            "total_municipal_levy":        row.get("total_municipal_levy"),
            "total_levy":                  row.get("total_levy"),
            "cy_total_rate":               row.get("cy_total_rate"),
            "cy_county_rate":              row.get("cy_county_rate"),
            "cy_school_rate":              row.get("cy_school_rate"),
            "cy_municipal_rate":           row.get("cy_municipal_rate"),
            "avg_residential_value":       row.get("avg_residential_value"),
            "avg_total_property_taxes":    row.get("avg_total_property_taxes"),
            "avg_county_taxes":            row.get("avg_county_taxes"),
            "avg_school_taxes":            row.get("avg_school_taxes"),
            "avg_municipal_taxes":         row.get("avg_municipal_taxes"),
            "cy_equalized_property_value": row.get("cy_equalized_property_value"),
            "cy_total_eq_rate":            row.get("cy_total_eq_rate"),
        })

    if not rows:
        raise IngestError(
            f"DCA workbook for year {year} produced 0 mappable county rows."
        )

    df = pl.DataFrame(rows)

    return ParseResult(
        dataframe=df,
        source_url=build_dca_url(year),
        source_sha256=sha256,
        source_vintage=f"{year}-annual",
        year=year,
        n_rows=df.height,
    )


# ============================================================================
# Stage + load
# ============================================================================


def stage_dataframe(result: ParseResult) -> pl.DataFrame:
    """Add provenance columns; return DataFrame ready to UPSERT."""
    return result.dataframe.with_columns(
        pl.lit(result.source_url).alias("source_url"),
        pl.lit(result.source_sha256).alias("source_sha256"),
        pl.lit(result.source_vintage).alias("source_vintage"),
    )


_UPSERT_COLS: Final[tuple[str, ...]] = (
    "county_fips", "year",
    "net_valuation_taxable", "total_county_levy", "total_school_levy",
    "total_municipal_levy", "total_levy",
    "cy_total_rate", "cy_county_rate", "cy_school_rate", "cy_municipal_rate",
    "avg_residential_value", "avg_total_property_taxes",
    "avg_county_taxes", "avg_school_taxes", "avg_municipal_taxes",
    "cy_equalized_property_value", "cy_total_eq_rate",
    "source_url", "source_sha256", "source_vintage",
)


_UPSERT_SQL: Final[str] = f"""
INSERT INTO raw.nj_property_tax_county
    ({", ".join(_UPSERT_COLS)})
VALUES ({", ".join(["%s"] * len(_UPSERT_COLS))})
ON CONFLICT (county_fips, year) DO UPDATE SET
    net_valuation_taxable       = EXCLUDED.net_valuation_taxable,
    total_county_levy           = EXCLUDED.total_county_levy,
    total_school_levy           = EXCLUDED.total_school_levy,
    total_municipal_levy        = EXCLUDED.total_municipal_levy,
    total_levy                  = EXCLUDED.total_levy,
    cy_total_rate               = EXCLUDED.cy_total_rate,
    cy_county_rate              = EXCLUDED.cy_county_rate,
    cy_school_rate              = EXCLUDED.cy_school_rate,
    cy_municipal_rate           = EXCLUDED.cy_municipal_rate,
    avg_residential_value       = EXCLUDED.avg_residential_value,
    avg_total_property_taxes    = EXCLUDED.avg_total_property_taxes,
    avg_county_taxes            = EXCLUDED.avg_county_taxes,
    avg_school_taxes            = EXCLUDED.avg_school_taxes,
    avg_municipal_taxes         = EXCLUDED.avg_municipal_taxes,
    cy_equalized_property_value = EXCLUDED.cy_equalized_property_value,
    cy_total_eq_rate            = EXCLUDED.cy_total_eq_rate,
    source_url                  = EXCLUDED.source_url,
    source_sha256               = EXCLUDED.source_sha256,
    source_vintage              = EXCLUDED.source_vintage,
    ingested_at                 = now()
"""


def load_to_postgres(
    staged: pl.DataFrame,
    connection: psycopg.Connection,
) -> int:
    """UPSERT staged rows into raw.nj_property_tax_county. Returns rows touched."""
    rows = list(staged.select(_UPSERT_COLS).iter_rows())
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
    """NJ DCA county property tax ingester (Tier 2)."""


@cli.command("fetch")
@click.option("--year", type=int, required=True)
@click.option("--dest-dir", type=click.Path(file_okay=False, path_type=Path),
              default=Path("data/manual/nj_dca_property_tax"), show_default=True)
@click.option("--overwrite", is_flag=True, default=False)
def fetch_cmd(year: int, dest_dir: Path, overwrite: bool) -> None:
    """Download a DCA tax workbook for one year."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    dest = fetch_dca_workbook(year, dest_dir=dest_dir, overwrite=overwrite)
    click.echo(str(dest))


@cli.command("parse")
@click.option("--year", type=int, required=True)
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def parse_cmd(year: int, path: Path) -> None:
    """Parse a DCA workbook and print a summary."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = parse_dca_workbook(path, year=year)
    click.echo(
        f"sha256={result.source_sha256}\n"
        f"vintage={result.source_vintage}\n"
        f"year={result.year}\n"
        f"n_counties={result.n_rows}\n"
    )
    click.echo(result.dataframe.select([
        "county_fips", "avg_residential_value",
        "avg_total_property_taxes", "cy_total_rate",
    ]).head(25))


@cli.command("load")
@click.option("--start-year", type=int, required=True)
@click.option("--end-year",   type=int, required=True)
@click.option("--dest-dir", type=click.Path(file_okay=False, path_type=Path),
              default=Path("data/manual/nj_dca_property_tax"), show_default=True)
@click.option("--overwrite", is_flag=True, default=False)
@click.option("--dsn", envvar="PG_DSN", required=True)
def load_cmd(
    start_year: int, end_year: int, dest_dir: Path, overwrite: bool, dsn: str,
) -> None:
    """Fetch + parse + UPSERT one year per call across [start_year, end_year]."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if start_year > end_year:
        raise click.UsageError(f"start_year {start_year} > end_year {end_year}")
    if start_year < DCA_EARLIEST_YEAR:
        raise click.UsageError(
            f"DCA standardized format starts at {DCA_EARLIEST_YEAR}; "
            f"got start_year={start_year}"
        )

    import psycopg

    total = 0
    failed: list[tuple[int, str]] = []
    with psycopg.connect(dsn) as conn:
        for yr in range(start_year, end_year + 1):
            try:
                path = fetch_dca_workbook(yr, dest_dir=dest_dir, overwrite=overwrite)
                result = parse_dca_workbook(path, year=yr)
            except (IngestError, httpx.HTTPError) as exc:
                log.warning("Skipping DCA %d: %s", yr, exc)
                failed.append((yr, str(exc)))
                continue
            staged = stage_dataframe(result)
            n = load_to_postgres(staged, conn)
            total += n
            log.info("Loaded %d county rows for DCA %d", n, yr)
        conn.commit()

    msg = f"UPSERTed {total} rows into raw.nj_property_tax_county."
    if failed:
        msg += f" Failed years: {failed}"
    click.echo(msg)


# ============================================================================
# Municipality-level (Phase 8a): same workbook, different sheet
# ============================================================================


# Mapping from DCA-published header names on the 'Municipal Tax Summary'
# sheet to the canonical column names used by raw.nj_property_tax_muni.
# Distinct from the County Tax Summary mapping above because the muni
# sheet has additional sub-rates (library, open space, REAP) that we
# are NOT yet ingesting -- we keep only the headline columns.
_MUNI_HEADER_RENAMES: Final[dict[str, str]] = {
    "municode":                                                "muni_code",
    "municipality":                                            "muni_name",
    "county":                                                  "county_name_full",
    "net valuation taxable":                                   "net_valuation_taxable",
    "total county levy":                                       "total_county_levy",
    "total school levy":                                       "total_school_levy",
    "total local municipal tax levy":                          "total_municipal_levy",
    "total levy on which tax rate is computed":                "total_levy",
    "cy total rate":                                           "cy_total_rate",
    "cy county rate":                                          "cy_county_rate",
    "cy school rate":                                          "cy_school_rate",
    "cy total municipal rate":                                 "cy_municipal_rate",
    "average residential property value":                      "avg_residential_value",
    "average total property taxes "
        "(not including credits and deductions)":              "avg_total_property_taxes",
    "average county taxes":                                    "avg_county_taxes",
    "average school taxes":                                    "avg_school_taxes",
    "average municipal taxes":                                 "avg_municipal_taxes",
    "cy equalized property value (pre-appeal)":                "cy_equalized_property_value",
    "cy total eq rate (reap not included)":                    "cy_total_eq_rate",
}


_MUNI_REQUIRED_CANONICAL: Final[frozenset[str]] = frozenset({
    "muni_code", "muni_name", "avg_total_property_taxes",
    "cy_total_rate", "avg_residential_value",
})


def _canonicalize_muni_columns(cols: list[object]) -> list[str]:
    out: list[str] = []
    seen: dict[str, int] = {}
    for i, c in enumerate(cols):
        if c is None or (isinstance(c, str) and not c.strip()):
            out.append(f"_unused_{i}")
            continue
        key = str(c).strip().lower()
        canonical = _MUNI_HEADER_RENAMES.get(
            key,
            key.replace(" ", "_").replace("(", "").replace(")", "").replace("%", "pct"),
        )
        if canonical in seen:
            seen[canonical] += 1
            canonical = f"{canonical}_{seen[canonical]}"
        else:
            seen[canonical] = 0
        out.append(canonical)
    return out


def parse_dca_workbook_munis(path: Path, *, year: int) -> ParseResult:
    """Parse the 'Municipal Tax Summary' sheet -> typed muni-level frame.

    Filters to real munis (4-char MuniCode, last 2 digits NOT '00').
    Returns one row per NJ municipality (~564 in the 2024 workbook).
    """
    if not path.exists():
        raise IngestError(f"DCA workbook not found: {path}")

    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()

    raw = pl.read_excel(
        path, engine="calamine",
        sheet_name="Municipal Tax Summary",
        infer_schema_length=0,
    )
    if raw.height == 0:
        raise IngestError(f"DCA Municipal Tax Summary sheet is empty: {path}")

    headers = _canonicalize_muni_columns(list(raw.row(0)))
    body = raw.slice(1).rename({
        old: new for old, new in zip(raw.columns, headers, strict=True)
    })

    missing = _MUNI_REQUIRED_CANONICAL - set(body.columns)
    if missing:
        raise IngestError(
            f"DCA muni workbook for year {year} missing canonical columns: "
            f"{sorted(missing)}; available={body.columns!r}"
        )

    numeric_cols = [
        "net_valuation_taxable", "total_county_levy", "total_school_levy",
        "total_municipal_levy", "total_levy",
        "cy_total_rate", "cy_county_rate", "cy_school_rate", "cy_municipal_rate",
        "avg_residential_value", "avg_total_property_taxes",
        "avg_county_taxes", "avg_school_taxes", "avg_municipal_taxes",
        "cy_equalized_property_value", "cy_total_eq_rate",
    ]
    cast_exprs = [
        pl.col(c).cast(pl.Float64, strict=False).alias(c)
        for c in numeric_cols if c in body.columns
    ]
    typed = body.with_columns(cast_exprs)

    # Real munis only: 4-digit MuniCode whose last 2 chars are NOT '00'.
    # The '00'-suffix rows are county summaries; they belong in
    # raw.nj_property_tax_county (loaded by the county code path) and
    # are never inserted into raw.nj_property_tax_muni.
    munis_only = typed.filter(
        pl.col("muni_code").is_not_null()
        & (pl.col("muni_code").str.len_chars() == 4)
        & ~pl.col("muni_code").str.ends_with("00")
    )

    if munis_only.height == 0:
        raise IngestError(
            f"DCA muni workbook for year {year} produced 0 muni rows; "
            "MuniCode column may have changed format."
        )

    rows: list[dict[str, object]] = []
    for row in munis_only.iter_rows(named=True):
        rows.append({
            "muni_code":                   row.get("muni_code"),
            "year":                        year,
            "net_valuation_taxable":       row.get("net_valuation_taxable"),
            "total_county_levy":           row.get("total_county_levy"),
            "total_school_levy":           row.get("total_school_levy"),
            "total_municipal_levy":        row.get("total_municipal_levy"),
            "total_levy":                  row.get("total_levy"),
            "cy_total_rate":               row.get("cy_total_rate"),
            "cy_county_rate":              row.get("cy_county_rate"),
            "cy_school_rate":              row.get("cy_school_rate"),
            "cy_municipal_rate":           row.get("cy_municipal_rate"),
            "avg_residential_value":       row.get("avg_residential_value"),
            "avg_total_property_taxes":    row.get("avg_total_property_taxes"),
            "avg_county_taxes":            row.get("avg_county_taxes"),
            "avg_school_taxes":            row.get("avg_school_taxes"),
            "avg_municipal_taxes":         row.get("avg_municipal_taxes"),
            "cy_equalized_property_value": row.get("cy_equalized_property_value"),
            "cy_total_eq_rate":            row.get("cy_total_eq_rate"),
        })

    df = pl.DataFrame(rows)

    return ParseResult(
        dataframe=df,
        source_url=build_dca_url(year),
        source_sha256=sha256,
        source_vintage=f"{year}-annual",
        year=year,
        n_rows=df.height,
    )


_MUNI_UPSERT_COLS: Final[tuple[str, ...]] = (
    "muni_code", "year",
    "net_valuation_taxable", "total_county_levy", "total_school_levy",
    "total_municipal_levy", "total_levy",
    "cy_total_rate", "cy_county_rate", "cy_school_rate", "cy_municipal_rate",
    "avg_residential_value", "avg_total_property_taxes",
    "avg_county_taxes", "avg_school_taxes", "avg_municipal_taxes",
    "cy_equalized_property_value", "cy_total_eq_rate",
    "source_url", "source_sha256", "source_vintage",
)


_MUNI_UPSERT_SQL: Final[str] = f"""
INSERT INTO raw.nj_property_tax_muni
    ({", ".join(_MUNI_UPSERT_COLS)})
VALUES ({", ".join(["%s"] * len(_MUNI_UPSERT_COLS))})
ON CONFLICT (muni_code, year) DO UPDATE SET
    net_valuation_taxable       = EXCLUDED.net_valuation_taxable,
    total_county_levy           = EXCLUDED.total_county_levy,
    total_school_levy           = EXCLUDED.total_school_levy,
    total_municipal_levy        = EXCLUDED.total_municipal_levy,
    total_levy                  = EXCLUDED.total_levy,
    cy_total_rate               = EXCLUDED.cy_total_rate,
    cy_county_rate              = EXCLUDED.cy_county_rate,
    cy_school_rate              = EXCLUDED.cy_school_rate,
    cy_municipal_rate           = EXCLUDED.cy_municipal_rate,
    avg_residential_value       = EXCLUDED.avg_residential_value,
    avg_total_property_taxes    = EXCLUDED.avg_total_property_taxes,
    avg_county_taxes            = EXCLUDED.avg_county_taxes,
    avg_school_taxes            = EXCLUDED.avg_school_taxes,
    avg_municipal_taxes         = EXCLUDED.avg_municipal_taxes,
    cy_equalized_property_value = EXCLUDED.cy_equalized_property_value,
    cy_total_eq_rate            = EXCLUDED.cy_total_eq_rate,
    source_url                  = EXCLUDED.source_url,
    source_sha256               = EXCLUDED.source_sha256,
    source_vintage              = EXCLUDED.source_vintage,
    ingested_at                 = now()
"""


def load_munis_to_postgres(
    staged: pl.DataFrame,
    connection: psycopg.Connection,
) -> int:
    """UPSERT staged muni rows into raw.nj_property_tax_muni. Returns rows touched.

    Pre-condition: ref.nj_municipality must already contain every
    muni_code that appears in *staged* (the FK from raw to ref enforces
    this; load db/seeds/040_nj_municipality.sql first).
    """
    rows = list(staged.select(_MUNI_UPSERT_COLS).iter_rows())
    if not rows:
        return 0
    with connection.cursor() as cur:
        cur.executemany(_MUNI_UPSERT_SQL, rows)
    return len(rows)


@cli.command("load-muni")
@click.option("--start-year", type=int, required=True)
@click.option("--end-year",   type=int, required=True)
@click.option("--dest-dir", type=click.Path(file_okay=False, path_type=Path),
              default=Path("data/manual/nj_dca_property_tax"), show_default=True)
@click.option("--overwrite", is_flag=True, default=False)
@click.option("--dsn", envvar="PG_DSN", required=True)
def load_muni_cmd(
    start_year: int, end_year: int, dest_dir: Path, overwrite: bool, dsn: str,
) -> None:
    """Fetch + parse + UPSERT muni rows for [start_year, end_year]."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if start_year > end_year:
        raise click.UsageError(f"start_year {start_year} > end_year {end_year}")
    if start_year < DCA_EARLIEST_YEAR:
        raise click.UsageError(
            f"DCA standardized format starts at {DCA_EARLIEST_YEAR}; "
            f"got start_year={start_year}"
        )

    import psycopg

    total = 0
    failed: list[tuple[int, str]] = []
    with psycopg.connect(dsn) as conn:
        for yr in range(start_year, end_year + 1):
            try:
                path = fetch_dca_workbook(yr, dest_dir=dest_dir, overwrite=overwrite)
                result = parse_dca_workbook_munis(path, year=yr)
            except (IngestError, httpx.HTTPError) as exc:
                log.warning("Skipping DCA muni %d: %s", yr, exc)
                failed.append((yr, str(exc)))
                continue
            staged = stage_dataframe(result)
            n = load_munis_to_postgres(staged, conn)
            total += n
            log.info("Loaded %d muni rows for DCA %d", n, yr)
        conn.commit()

    msg = f"UPSERTed {total} rows into raw.nj_property_tax_muni."
    if failed:
        msg += f" Failed years: {failed}"
    click.echo(msg)
