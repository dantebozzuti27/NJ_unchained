"""USCIS H-1B Employer Data Hub ingester (POP-3 / FRAUD-V1).

Downloads the public annual CSV exports and loads them into
``raw.uscis_h1b_employer``. Keyless, free, full-replace per fiscal year.

Source
------
https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub

USCIS publishes one CSV per fiscal year at a stable path::

    https://www.uscis.gov/sites/default/files/document/data/h1b_datahubexport-{fy}.csv

FY2026 is a partial year (through Q3 as of 2026-08). Counts are first
decisions on initial and continuing petitions, not unique workers.

Architecture
------------
1. **Parse** -- :func:`parse_uscis_h1b_file` (pure; file in, DataFrame out).
2. **Stage** -- provenance columns + canonical employer name.
3. **Load** -- DELETE the fiscal_year slice, then COPY.

The same ``canonical_employer_name`` used by the LCA ingester is the
join key for ``employer_lca_uscis_volume_gap``.
"""

from __future__ import annotations

import io
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import click
import polars as pl

from ingestion._base import IngestError, canonical_employer_name, sha256_file

if TYPE_CHECKING:
    import psycopg

log = logging.getLogger(__name__)

_COPY_CHUNK = 1 << 20

USCIS_H1B_URL_TEMPLATE: Final[str] = (
    "https://www.uscis.gov/sites/default/files/document/data/"
    "h1b_datahubexport-{fy}.csv"
)

# Header aliases observed across FY2009-FY2026 exports. Canonicalized
# (lowercase, non-alnum stripped) before lookup.
_HEADER_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "fiscal_year": ("fiscalyear", "fy", "year"),
    "employer_name": (
        "employerpetitionername",
        "employername",
        "petitionername",
        "employer",
    ),
    "tax_id_last4": (
        "taxid",
        "fein",
        "federalein",
        "employertaxid",
        "petitionerfein",
    ),
    "naics_code": ("naics", "naicscode", "industrycode"),
    "petitioner_city": ("city", "petitionercity", "employercity"),
    "petitioner_state": ("state", "petitionerstate", "employerstate"),
    "petitioner_zip": ("zip", "zipcode", "petitionerzip", "employerzip"),
    "initial_approval": ("initialapproval", "newapproval", "initialapprovals"),
    "initial_denial": ("initialdenial", "newdenial", "initialdenials"),
    "continuing_approval": (
        "continuingapproval",
        "continuationapproval",
        "continuingapprovals",
    ),
    "continuing_denial": (
        "continuingdenial",
        "continuationdenial",
        "continuingdenials",
    ),
}

_DEST_COLS: Final[tuple[str, ...]] = (
    "fiscal_year",
    "employer_name",
    "employer_canonical_name",
    "tax_id_last4",
    "naics_code",
    "petitioner_city",
    "petitioner_state",
    "petitioner_zip",
    "initial_approval",
    "initial_denial",
    "continuing_approval",
    "continuing_denial",
    "source_filename",
    "source_sha256",
    "source_vintage",
    "data_quality",
)


def _canon_header(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _map_headers(columns: list[str]) -> dict[str, str]:
    """Return dest_col -> source_col for columns present in *columns*."""
    observed = {_canon_header(c): c for c in columns}
    mapping: dict[str, str] = {}
    for dest, aliases in _HEADER_ALIASES.items():
        for alias in aliases:
            if alias in observed:
                mapping[dest] = observed[alias]
                break
    missing = [k for k in ("fiscal_year", "employer_name") if k not in mapping]
    if missing:
        raise IngestError(
            "USCIS H-1B file is missing required columns "
            f"{missing}. Observed headers: {columns[:20]}"
        )
    return mapping


def build_uscis_h1b_url(fiscal_year: int) -> str:
    """Return the canonical USCIS Data Hub CSV URL for *fiscal_year*."""
    if fiscal_year < 2009:
        raise IngestError(
            f"USCIS H-1B Data Hub starts at FY2009 (requested FY{fiscal_year})."
        )
    return USCIS_H1B_URL_TEMPLATE.format(fy=fiscal_year)


def fetch_uscis_h1b_file(
    fiscal_year: int,
    *,
    out_dir: Path,
    overwrite: bool = False,
    timeout_s: float = 300.0,
) -> Path:
    """Download one FY CSV. Returns the local path."""
    import httpx

    url = build_uscis_h1b_url(fiscal_year)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"h1b_datahubexport-{fiscal_year}.csv"
    if dest.exists() and not overwrite:
        log.info("Skipping fetch (already present): %s", dest)
        return dest

    log.info("Fetching USCIS H-1B Data Hub: %s", url)
    headers = {
        "User-Agent": (
            "NJ-Unchained/1.0 (civic-integrity research; "
            "+https://github.com/dantebozzuti27/NJ_unchained)"
        )
    }
    with (
        httpx.Client(timeout=timeout_s, follow_redirects=True, headers=headers) as client,
        client.stream("GET", url) as resp,
    ):
        if resp.status_code == 404:
            raise IngestError(
                f"USCIS has no file at {url} (HTTP 404). The FY may be "
                "unpublished, or the URL convention changed."
            )
        resp.raise_for_status()
        tmp = dest.with_suffix(dest.suffix + ".part")
        with tmp.open("wb") as fh:
            for chunk in resp.iter_bytes(chunk_size=_COPY_CHUNK):
                fh.write(chunk)
        tmp.rename(dest)
    log.info("Downloaded %s (%.1f MiB)", dest, dest.stat().st_size / (1 << 20))
    return dest


@dataclass(frozen=True)
class ParseResult:
    dataframe: pl.DataFrame
    fiscal_year: int
    source_filename: str
    source_sha256: str
    n_input_rows: int
    n_output_rows: int


def parse_uscis_h1b_file(path: Path) -> ParseResult:
    """Parse a USCIS H-1B Employer Data Hub CSV into the canonical shape."""
    raw = pl.read_csv(
        path,
        infer_schema_length=0,
        ignore_errors=True,
        truncate_ragged_lines=True,
    )
    mapping = _map_headers(list(raw.columns))
    exprs: list[pl.Expr] = []
    for dest, src in mapping.items():
        exprs.append(pl.col(src).alias(dest))
    df = raw.select(exprs)

    def _intish(col: str) -> pl.Expr:
        if col not in df.columns:
            return pl.lit(0).cast(pl.Int64).alias(col)
        return (
            pl.col(col)
            .cast(pl.Utf8)
            .str.replace_all(r"[^0-9-]", "")
            .cast(pl.Int64, strict=False)
            .fill_null(0)
            .alias(col)
        )

    fy_expr = (
        pl.col("fiscal_year")
        .cast(pl.Utf8)
        .str.replace_all(r"[^0-9]", "")
        .cast(pl.Int16, strict=False)
    )
    df = df.with_columns([
        fy_expr.alias("fiscal_year"),
        pl.col("employer_name").cast(pl.Utf8).str.strip_chars(),
        _intish("initial_approval") if "initial_approval" in df.columns
        else pl.lit(0).cast(pl.Int64).alias("initial_approval"),
        _intish("initial_denial") if "initial_denial" in df.columns
        else pl.lit(0).cast(pl.Int64).alias("initial_denial"),
        _intish("continuing_approval") if "continuing_approval" in df.columns
        else pl.lit(0).cast(pl.Int64).alias("continuing_approval"),
        _intish("continuing_denial") if "continuing_denial" in df.columns
        else pl.lit(0).cast(pl.Int64).alias("continuing_denial"),
    ])
    for optional in (
        "tax_id_last4", "naics_code", "petitioner_city",
        "petitioner_state", "petitioner_zip",
    ):
        if optional not in df.columns:
            df = df.with_columns(pl.lit("").cast(pl.Utf8).alias(optional))
        else:
            df = df.with_columns(
                pl.col(optional).cast(pl.Utf8).fill_null("").str.strip_chars()
            )

    df = df.filter(
        pl.col("employer_name").is_not_null()
        & (pl.col("employer_name") != "")
        & pl.col("fiscal_year").is_not_null()
    )
    names = df["employer_name"].to_list()
    df = df.with_columns(
        pl.Series(
            "employer_canonical_name",
            [canonical_employer_name(n) for n in names],
        )
    )
    # Last-4 EIN: keep digits only, pad/trim to <= 4.
    df = df.with_columns(
        pl.col("tax_id_last4")
        .str.replace_all(r"[^0-9]", "")
        .str.slice(-4)
        .alias("tax_id_last4")
    )
    df = df.with_columns(
        pl.col("petitioner_state").str.to_uppercase().str.slice(0, 2)
    )
    df = df.with_columns(
        pl.when(pl.col("petitioner_zip").str.contains(r"^\d{5}"))
        .then(pl.col("petitioner_zip").str.slice(0, 5))
        .otherwise(pl.lit(None))
        .alias("petitioner_zip")
    )

    years = df["fiscal_year"].drop_nulls().unique().to_list()
    if len(years) != 1:
        raise IngestError(
            f"USCIS file {path.name} spans fiscal years {years}; "
            "expected exactly one FY per file."
        )
    fiscal_year = int(years[0])
    return ParseResult(
        dataframe=df,
        fiscal_year=fiscal_year,
        source_filename=path.name,
        source_sha256=sha256_file(path),
        n_input_rows=raw.height,
        n_output_rows=df.height,
    )


def stage_dataframe(parse: ParseResult) -> pl.DataFrame:
    """Add provenance and project to ``raw.uscis_h1b_employer`` column order."""
    df = parse.dataframe.with_columns([
        pl.lit(parse.source_filename).alias("source_filename"),
        pl.lit(parse.source_sha256).alias("source_sha256"),
        pl.lit(f"FY{parse.fiscal_year}").alias("source_vintage"),
        pl.lit("measured").alias("data_quality"),
    ])
    for col in _DEST_COLS:
        if col not in df.columns:
            df = df.with_columns(pl.lit(None).cast(pl.Utf8).alias(col))
    # Collapse accidental PK collisions (same canonical + city/state/EIN)
    # by summing decision counts — USCIS sometimes repeats a petitioner
    # under punctuation variants that canonicalize together.
    staged = (
        df.select(_DEST_COLS)
        .group_by(
            "fiscal_year",
            "employer_canonical_name",
            "petitioner_state",
            "petitioner_city",
            "tax_id_last4",
        )
        .agg([
            pl.col("employer_name").first(),
            pl.col("naics_code").first(),
            pl.col("petitioner_zip").first(),
            pl.col("initial_approval").sum(),
            pl.col("initial_denial").sum(),
            pl.col("continuing_approval").sum(),
            pl.col("continuing_denial").sum(),
            pl.col("source_filename").first(),
            pl.col("source_sha256").first(),
            pl.col("source_vintage").first(),
            pl.col("data_quality").first(),
        ])
        .select(_DEST_COLS)
    )
    return staged


def load_to_postgres(
    staged: pl.DataFrame,
    connection: "psycopg.Connection",
    *,
    table: str = "raw.uscis_h1b_employer",
) -> int:
    """DELETE the FY slice then COPY. Returns rows inserted."""
    from psycopg import sql

    if staged.height == 0:
        return 0
    years = staged["fiscal_year"].unique().to_list()
    if len(years) != 1:
        raise IngestError(f"staged frame spans multiple FYs: {years}")
    fy = int(years[0])

    schema_part, table_part = table.split(".", 1)
    ident = sql.Identifier(schema_part, table_part)
    with connection.cursor() as cur:
        cur.execute(
            sql.SQL("DELETE FROM {} WHERE fiscal_year = %s").format(ident),
            (fy,),
        )
    buf = io.BytesIO()
    staged.write_csv(buf, include_header=False)
    buf.seek(0)
    col_idents = sql.SQL(", ").join(sql.Identifier(c) for c in staged.columns)
    copy_query = sql.SQL("COPY {tbl} ({cols}) FROM STDIN WITH (FORMAT csv, NULL '')").format(
        tbl=ident, cols=col_idents,
    )
    with connection.cursor().copy(copy_query) as cp:
        while chunk := buf.read(_COPY_CHUNK):
            cp.write(chunk)
    return staged.height


@click.group()
def cli() -> None:
    """USCIS H-1B Employer Data Hub ingester (POP-3)."""


@cli.command("fetch")
@click.option("--fiscal-year", type=int, required=True)
@click.option(
    "--out-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("data/manual/uscis_h1b"),
    show_default=True,
)
@click.option("--overwrite", is_flag=True, default=False)
def fetch_cmd(fiscal_year: int, out_dir: Path, overwrite: bool) -> None:
    """Download one FY CSV from USCIS."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    dest = fetch_uscis_h1b_file(fiscal_year, out_dir=out_dir, overwrite=overwrite)
    click.echo(str(dest))


@cli.command("load")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--dsn", envvar="PG_DSN", required=True)
@click.option("--dry-run", is_flag=True, default=False)
def load_cmd(path: Path, dsn: str, dry_run: bool) -> None:
    """Parse + stage + load a USCIS H-1B Data Hub CSV."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parsed = parse_uscis_h1b_file(path)
    staged = stage_dataframe(parsed)
    log.info(
        "Staged %d rows from %s (FY%d)",
        staged.height, path.name, parsed.fiscal_year,
    )
    if dry_run:
        click.echo("--dry-run: skipping COPY.")
        return
    import psycopg

    with psycopg.connect(dsn) as conn:
        n = load_to_postgres(staged, conn)
        conn.commit()
        click.echo(f"Loaded {n} rows into raw.uscis_h1b_employer (FY{parsed.fiscal_year}).")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO governance.dataset_health "
                "(dataset_id, signal_name, severity, metric_value, metric_unit, details) "
                "VALUES (%s, %s, %s, %s, %s, %s::jsonb)",
                (
                    "raw.uscis_h1b_employer",
                    "rows_loaded",
                    "info",
                    n,
                    "rows",
                    json.dumps({
                        "file": path.name,
                        "fiscal_year": parsed.fiscal_year,
                    }),
                ),
            )
        conn.commit()
