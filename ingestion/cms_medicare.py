"""CMS Medicare Part D Prescribers - by Provider ingester.

Downloads the free, keyless, single-CSV-per-calendar-year file published
on data.cms.gov and loads it into ``raw.cms_partd_prescriber``.

Why this ingester is shaped the way it is
-----------------------------------------
CMS publishes the "Medicare Part D Prescribers - by Provider" dataset as
one full CSV per calendar year (CY). Unlike LEIE (a single stable URL that
HHS replaces in place), CMS hosts each year as a distinct distribution in
a DKAN/DCAT catalog. The download URL is therefore NOT stable across years
and must be resolved from the master catalog at
``https://data.cms.gov/data.json`` -- each catalog ``dataset`` carries one
or more ``distribution[]`` entries, and the CSV is the distribution whose
``mediaType == "text/csv"``.

That resolution is the fragile part: CMS reorganizes titles, mints new
dataset GUIDs, and occasionally publishes a "latest" alias alongside the
per-year files. The ingester therefore:

1. Resolves the per-year CSV URL from the catalog (:func:`resolve_download_url`),
   matching the canonical dataset title AND the requested year.
2. ALWAYS lets the caller bypass resolution with an explicit ``url=`` --
   exactly like the LEIE ingester's ``url`` override -- so an operator can
   hand-pull the CSV from the CMS UI and load it air-gapped.
3. Conditionally re-downloads (HEAD size probe vs. local cache) and streams
   to a ``.part`` sidecar renamed atomically on success, because the
   provider file is large (hundreds of MB) and an interrupted download must
   never leave a corrupt cache file.

What this ingester deliberately does NOT do
-------------------------------------------
* Drug-level detail -- that is the separate "by Provider and Drug" dataset
  (a different, much larger substrate) and is future work.
* Cross-source NPI resolution to LEIE / NPPES -- that linkage lives in a
  derived layer with its own match rules and ``data_quality`` stamp.

Verifiable-data discipline
--------------------------
CMS suppresses any cell derived from fewer than 11 beneficiaries, and the
opioid columns are frequently blank for non-prescribers. A suppressed or
blank numeric cell is "no data", NOT zero: every blank numeric maps to SQL
NULL, never 0. NPI is preserved as the raw 10-character string (never cast
to int) for leading-zero safety. The load is idempotent per CY:
``DELETE WHERE data_year = %s`` then COPY, mirroring the LEIE/DCA contract.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import click
import httpx
import polars as pl
from psycopg import sql

from ingestion._base import IngestError, sha256_file

if TYPE_CHECKING:
    from collections.abc import Iterator

    import psycopg

log = logging.getLogger(__name__)


# ============================================================================
# Constants
# ============================================================================

# Master DCAT catalog. Every data.cms.gov dataset is listed here with its
# distribution[].downloadURL entries. Public + keyless.
CMS_DATA_JSON_URL: Final[str] = "https://data.cms.gov/data.json"

# The canonical dataset title we resolve against. Matching is done on a
# whitespace-normalized, year-stripped form so that a year-suffixed title
# ("... by Provider 2023") still matches while the sibling dataset
# "Medicare Part D Prescribers - by Provider and Drug" does NOT (it has
# trailing tokens after "by Provider" and so fails the exact compare).
CMS_PARTD_PROVIDER_TITLE: Final[str] = (
    "Medicare Part D Prescribers - by Provider"
)

# CSV header -> raw.cms_partd_prescriber column. The published file has
# ~80 columns; we ingest only this headline slice. Selecting by NAME (not
# position) means CMS appending columns is a no-op, while RENAMING one of
# these surfaces as a loud IngestError (missing-column check in parse).
CMS_COLUMN_MAP: Final[dict[str, str]] = {
    "Prscrbr_NPI":            "npi",
    "Prscrbr_Last_Org_Name":  "prscrbr_last_org_name",
    "Prscrbr_First_Name":     "prscrbr_first_name",
    "Prscrbr_City":           "prscrbr_city",
    "Prscrbr_State_Abrvtn":   "prscrbr_state_abrvtn",
    "Prscrbr_Type":           "prscrbr_type",
    "Tot_Clms":               "tot_clms",
    "Tot_Drug_Cst":           "tot_drug_cst",
    "Tot_Benes":              "tot_benes",
    "Opioid_Tot_Clms":        "opioid_tot_clms",
    "Opioid_Prscrbr_Rate":    "opioid_prscrbr_rate",
}

# Raw columns in CSV-map order. COPY's column list uses this exact order.
_MAPPED_RAW_COLUMNS: Final[tuple[str, ...]] = tuple(CMS_COLUMN_MAP.values())

# Columns COPY'd into NUMERIC. Kept as exact strings through parse so no
# float-rounding precision loss creeps in; Postgres parses NUMERIC during
# COPY (and fails loud on a non-numeric value, which is the desired
# schema-drift signal). Blank cells are SQL NULL, never 0.
_NUMERIC_RAW_COLUMNS: Final[tuple[str, ...]] = (
    "tot_clms",
    "tot_drug_cst",
    "tot_benes",
    "opioid_tot_clms",
    "opioid_prscrbr_rate",
)

# Full COPY column list: the CY (a load-time parameter, not a CSV column),
# the mapped CSV columns, then provenance. ingested_at defaults to now().
_COPY_COLUMNS: Final[tuple[str, ...]] = (
    "data_year",
    *_MAPPED_RAW_COLUMNS,
    "source_url",
    "source_sha256",
    "source_vintage",
)

# A permissive decimal/integer shape. CMS publishes plain numbers (no $,
# no thousands separators) in this file; anything else is schema drift.
_NUMERIC_RE: Final[str] = r"^-?[0-9]+(\.[0-9]+)?$"

# Earliest CY CMS publishes the modern Part D Prescriber file. Earlier
# years used a different layout; we refuse to construct a request for them.
CMS_EARLIEST_YEAR: Final[int] = 2013

# httpx timeouts. The catalog + HEAD probes stay short so a Dagster tick
# does not stall on a network blip; the streaming GET gets a generous
# window because the provider CSV is hundreds of MB.
_HTTP_CATALOG_TIMEOUT_S: Final[float] = 60.0
_HTTP_HEAD_TIMEOUT_S:    Final[float] = 30.0
_HTTP_GET_TIMEOUT_S:     Final[float] = 600.0

# COPY chunk size. Provider rows are ~200-400 bytes each; a 4 MiB buffer
# flushes every ~12-20K rows, bounding psycopg buffer churn without
# materializing the full CSV stream in memory.
_COPY_CHUNK_SIZE: Final[int] = 4 * 1024 * 1024


def _validate_year(data_year: int) -> None:
    """Reject CY values outside the supported range (fail loud, not 404)."""
    if data_year < CMS_EARLIEST_YEAR or data_year > 2099:
        raise IngestError(
            f"CMS Part D Prescriber year out of supported range: {data_year}; "
            f"expected {CMS_EARLIEST_YEAR}..2099",
        )


# ============================================================================
# Resolve download URL (catalog lookup)
# ============================================================================


def _normalize_title(title: str) -> str:
    """Collapse whitespace and lowercase a catalog title for comparison."""
    return re.sub(r"\s+", " ", title).strip().lower()


def resolve_download_url(
    *,
    data_year: int,
    client: httpx.Client | None = None,
) -> str:
    """Resolve the CSV downloadURL for the given CY from the CMS catalog.

    Fetches ``data.json`` and returns the ``distribution[].downloadURL``
    whose ``mediaType == "text/csv"`` for the dataset titled
    :data:`CMS_PARTD_PROVIDER_TITLE` and matching ``data_year``.

    Matching strategy (deliberately strict, because the catalog is fragile):

    * The dataset title, whitespace-normalized and with the year token
      stripped, must EQUAL the canonical title. This excludes the sibling
      "... by Provider and Drug" dataset, whose normalized title differs.
    * The year must appear in the title, a keyword, or the temporal /
      issued field -- catalogs vary on where they stamp the CY.

    If several CSV distributions survive both filters (e.g. a mirror), the
    first is returned; they are by construction the same CY's file.

    Args:
        data_year: Calendar year of the desired file (e.g. 2023).
        client: Optional httpx client (for dependency injection in tests).
            When None, a short-timeout client is created and closed here.

    Raises:
        IngestError: if no matching CSV distribution is found.
        httpx.HTTPStatusError: on a non-2xx catalog response.

    """
    _validate_year(data_year)
    owns_client = client is None
    if client is None:
        client = httpx.Client(
            timeout=_HTTP_CATALOG_TIMEOUT_S, follow_redirects=True,
        )
    try:
        resp = client.get(CMS_DATA_JSON_URL)
        resp.raise_for_status()
        catalog = resp.json()
    finally:
        if owns_client:
            client.close()

    base_title = _normalize_title(CMS_PARTD_PROVIDER_TITLE)
    year_str = str(data_year)
    datasets = catalog.get("dataset", []) if isinstance(catalog, dict) else []

    candidates: list[str] = []
    for ds in datasets:
        if not isinstance(ds, dict):
            continue
        raw_title = str(ds.get("title", ""))
        title_noyear = _normalize_title(raw_title.replace(year_str, " "))
        if title_noyear != base_title:
            continue

        # Confirm the year is stamped somewhere on the dataset.
        keywords = ds.get("keyword", [])
        keyword_str = " ".join(str(k) for k in keywords) if isinstance(keywords, list) else ""
        haystack = " ".join((
            raw_title,
            keyword_str,
            str(ds.get("temporal", "")),
            str(ds.get("issued", "")),
            str(ds.get("modified", "")),
        ))
        if year_str not in haystack:
            continue

        for dist in ds.get("distribution", []):
            if not isinstance(dist, dict):
                continue
            url = dist.get("downloadURL")
            if dist.get("mediaType") == "text/csv" and isinstance(url, str) and url:
                candidates.append(url)

    if not candidates:
        raise IngestError(
            f"CMS catalog has no text/csv distribution for "
            f"{CMS_PARTD_PROVIDER_TITLE!r} matching CY {data_year}. "
            f"Pass --url to bypass catalog resolution.",
        )
    return candidates[0]


# ============================================================================
# Fetch
# ============================================================================


@dataclass(frozen=True)
class FetchResult:
    """Output of :func:`fetch_partd_csv`."""

    path:           Path
    source_url:     str
    source_sha256:  str
    source_vintage: str  # ETag or Last-Modified when fetched, else "CY{year}"
    n_bytes:        int
    cache_hit:      bool


def fetch_partd_csv(
    *,
    data_year: int,
    dest_dir: Path,
    overwrite: bool = False,
    url: str | None = None,
    client: httpx.Client | None = None,
) -> FetchResult:
    """Download the Part D Prescriber CSV for *data_year* into *dest_dir*.

    When *url* is None the per-year CSV URL is resolved from the CMS
    catalog (:func:`resolve_download_url`); pass an explicit *url* to
    bypass resolution (operator hand-pull / air-gapped load).

    Uses conditional-GET semantics: a HEAD probe compares the remote
    ``content-length`` to the local cache size and returns a cache_hit
    FetchResult without re-downloading when they match. Streams to a
    ``.part`` sidecar renamed atomically on success.

    Args:
        data_year: Calendar year of the desired file.
        dest_dir: Cache directory (created if absent).
        overwrite: Force re-download even when the cache size matches.
        url: Explicit CSV URL; resolved from the catalog when None.
        client: Optional httpx client used ONLY for catalog resolution
            (the streaming download always uses its own long-timeout
            client). Useful for dependency injection in tests.

    Raises:
        httpx.HTTPStatusError: on a non-2xx upstream response.
        IngestError: on a download that produced zero bytes.

    """
    _validate_year(data_year)
    resolved_url = url if url is not None else resolve_download_url(
        data_year=data_year, client=client,
    )

    dest_dir.mkdir(parents=True, exist_ok=True)
    local = dest_dir / f"partd_prescriber_cy{data_year}.csv"

    with httpx.Client(timeout=_HTTP_HEAD_TIMEOUT_S, follow_redirects=True) as head_client:
        head_resp = head_client.head(resolved_url)
        head_resp.raise_for_status()
        remote_etag    = head_resp.headers.get("etag", "").strip('"')
        remote_lastmod = head_resp.headers.get("last-modified", "")
        remote_size    = int(head_resp.headers.get("content-length", "0") or 0)

    vintage = remote_etag or remote_lastmod or f"CY{data_year}"

    if local.exists() and not overwrite:
        local_size = local.stat().st_size
        if remote_size and local_size == remote_size:
            log.info(
                "cms.fetch: cache hit for %s (size=%d bytes)", local.name, local_size,
            )
            return FetchResult(
                path=local,
                source_url=resolved_url,
                source_sha256=sha256_file(local),
                source_vintage=vintage,
                n_bytes=local_size,
                cache_hit=True,
            )
        log.info(
            "cms.fetch: cache stale for %s (local=%d, remote=%d) -- re-downloading",
            local.name, local_size, remote_size,
        )

    log.info("cms.fetch: downloading %s -> %s", resolved_url, local)
    tmp = local.with_suffix(local.suffix + ".part")
    with (
        httpx.Client(timeout=_HTTP_GET_TIMEOUT_S, follow_redirects=True) as get_client,
        get_client.stream("GET", resolved_url) as resp,
    ):
        resp.raise_for_status()
        n = 0
        with tmp.open("wb") as fh:
            for chunk in resp.iter_bytes(chunk_size=1 << 20):
                fh.write(chunk)
                n += len(chunk)
        if n == 0:
            tmp.unlink(missing_ok=True)
            raise IngestError(
                f"CMS upstream {resolved_url} returned 0 bytes; refusing to "
                "write an empty cache file.",
            )
        shutil.move(tmp, local)
    log.info("cms.fetch: wrote %s (%.1f MiB)", local, n / (1 << 20))
    return FetchResult(
        path=local,
        source_url=resolved_url,
        source_sha256=sha256_file(local),
        source_vintage=vintage,
        n_bytes=n,
        cache_hit=False,
    )


# ============================================================================
# Parse
# ============================================================================


@dataclass(frozen=True)
class ParseResult:
    """Output of :func:`parse_partd_csv`."""

    dataframe:      pl.DataFrame  # mapped raw columns, all Utf8, blanks -> null
    source_url:     str
    source_sha256:  str
    source_vintage: str
    data_year:      int
    n_rows:         int


def parse_partd_csv(fetch: FetchResult, *, data_year: int) -> ParseResult:
    """Parse the Part D Prescriber CSV into a typed-by-string DataFrame.

    Reads ONLY the columns named in :data:`CMS_COLUMN_MAP` (selected by
    name, so CMS appending columns is a no-op). Every column is read as a
    string -- numeric exactness is preserved for the NUMERIC COPY downstream
    and Polars treats an empty CSV field as null, so suppressed/blank cells
    become SQL NULL rather than 0.

    Validation:

    * Every mapped CSV header must be present (a rename -> IngestError).
    * Rows with a blank NPI are dropped (logged) -- an NPI is the PK half
      we cannot synthesize.
    * Every non-null numeric cell must match :data:`_NUMERIC_RE`; a stray
      ``$`` or thousands separator is treated as schema drift (IngestError)
      rather than silently coerced.

    Returns a :class:`ParseResult` whose DataFrame columns are the raw
    column names in :data:`_MAPPED_RAW_COLUMNS` order.
    """
    if not fetch.path.exists():
        raise IngestError(f"CMS Part D file not found: {fetch.path}")

    # Cheap header read first so a renamed/missing column fails before we
    # pull the (large) body into memory.
    header_df = pl.read_csv(fetch.path, n_rows=0)
    present = set(header_df.columns)
    missing = [c for c in CMS_COLUMN_MAP if c not in present]
    if missing:
        raise IngestError(
            f"CMS Part D CSV missing expected columns {missing!r}; "
            f"header has {sorted(present)[:12]}... "
            "Update CMS_COLUMN_MAP deliberately.",
        )

    raw = pl.read_csv(
        fetch.path,
        columns=list(CMS_COLUMN_MAP.keys()),
        infer_schema_length=0,  # force every column to Utf8 (no int/float cast)
    )
    df = raw.rename(dict(CMS_COLUMN_MAP))

    # Normalize NPI: trim, treat blank as null, then drop null-NPI rows.
    df = df.with_columns(
        pl.col("npi").str.strip_chars().replace("", None).alias("npi"),
    )
    n_before = df.height
    df = df.filter(pl.col("npi").is_not_null())
    dropped = n_before - df.height
    if dropped:
        log.warning("cms.parse: dropped %d row(s) with a blank NPI", dropped)

    if df.height == 0:
        raise IngestError(
            f"CMS Part D file {fetch.path} parsed 0 rows with an NPI; "
            "refusing to load an empty pull.",
        )

    # Fail loud on a non-numeric value in a numeric column. Blank/suppressed
    # cells are already null (Polars empty-field default) and are exempt.
    for col in _NUMERIC_RAW_COLUMNS:
        bad = df.filter(
            pl.col(col).is_not_null() & ~pl.col(col).str.contains(_NUMERIC_RE),
        )
        if bad.height:
            sample = bad.get_column(col).head(3).to_list()
            raise IngestError(
                f"CMS Part D column {col!r} has {bad.height} non-numeric "
                f"value(s); sample={sample!r}. Expected blank or a number.",
            )

    # Keep deterministic raw-column order for the COPY stream.
    df = df.select(_MAPPED_RAW_COLUMNS)

    return ParseResult(
        dataframe=df,
        source_url=fetch.source_url,
        source_sha256=fetch.source_sha256,
        source_vintage=fetch.source_vintage,
        data_year=data_year,
        n_rows=df.height,
    )


# ============================================================================
# Load
# ============================================================================


def _iter_csv_lines(parse: ParseResult) -> Iterator[bytes]:
    """Yield CSV rows ready for ``COPY ... FORMAT csv`` in _COPY_COLUMNS order.

    Each row is ``(data_year, <11 mapped columns>, source_url,
    source_sha256, source_vintage)``. None (a suppressed/blank cell) is
    emitted as an empty field, which COPY ``NULL ''`` materializes as SQL
    NULL -- never 0.
    """
    year_str = str(parse.data_year)
    for row in parse.dataframe.iter_rows():
        record = (
            year_str,
            *row,
            parse.source_url,
            parse.source_sha256,
            parse.source_vintage,
        )
        line = io.StringIO()
        csv.writer(line, lineterminator="\n").writerow(record)
        yield line.getvalue().encode("utf-8")


def load_to_postgres(parse: ParseResult, conn: psycopg.Connection) -> int:
    """Idempotently load the parsed CY into ``raw.cms_partd_prescriber``.

    Strategy (mirrors LEIE/DCA idempotency): ``DELETE WHERE data_year = %s``
    for the CY being loaded, then stream the rows in via COPY. Because the
    file holds one row per NPI per CY, a direct COPY into the raw table is
    safe -- a duplicate NPI would violate the ``(data_year, npi)`` PK and
    fail loud, which is the correct schema-drift behavior.

    Returns the number of rows COPY'd.
    """
    if parse.n_rows == 0:
        log.info("cms.load: nothing to load (parse.n_rows=0)")
        return 0

    col_idents = sql.SQL(", ").join(sql.Identifier(c) for c in _COPY_COLUMNS)

    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM raw.cms_partd_prescriber WHERE data_year = %s",
            (parse.data_year,),
        )
        copy_query = sql.SQL(
            "COPY raw.cms_partd_prescriber ({cols}) FROM STDIN "
            "WITH (FORMAT csv, NULL '')",
        ).format(cols=col_idents)

        with cur.copy(copy_query) as cp:
            buf = bytearray()
            for line_bytes in _iter_csv_lines(parse):
                buf.extend(line_bytes)
                if len(buf) >= _COPY_CHUNK_SIZE:
                    cp.write(bytes(buf))
                    buf.clear()
            if buf:
                cp.write(bytes(buf))

    log.info(
        "cms.load: loaded %d rows into raw.cms_partd_prescriber "
        "(data_year=%d, vintage=%s, sha256=%s)",
        parse.n_rows, parse.data_year, parse.source_vintage,
        parse.source_sha256[:16] + "...",
    )
    return parse.n_rows


# ============================================================================
# Click CLI
# ============================================================================


@click.group()
def cli() -> None:
    """CMS Medicare Part D Prescribers - by Provider ingester."""


@cli.command("fetch")
@click.option("--year", type=int, required=True, help="Calendar year (e.g. 2023).")
@click.option(
    "--dest-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("data/cache/cms_partd_prescriber"),
    show_default=True,
    help="Where to cache the downloaded CSV.",
)
@click.option(
    "--overwrite/--no-overwrite",
    default=False,
    help="Force re-download even when local cache size matches remote.",
)
@click.option(
    "--url",
    default=None,
    help="Explicit CSV URL; bypasses catalog resolution (operator hand-pull).",
)
def cmd_fetch(year: int, dest_dir: Path, overwrite: bool, url: str | None) -> None:
    """Resolve + download the Part D Prescriber CSV for one year."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    fetch = fetch_partd_csv(
        data_year=year, dest_dir=dest_dir, overwrite=overwrite, url=url,
    )
    click.echo(f"path:           {fetch.path}")
    click.echo(f"source_url:     {fetch.source_url}")
    click.echo(f"source_sha256:  {fetch.source_sha256}")
    click.echo(f"source_vintage: {fetch.source_vintage}")
    click.echo(f"n_bytes:        {fetch.n_bytes}")
    click.echo(f"cache_hit:      {fetch.cache_hit}")


@cli.command("load")
@click.argument(
    "csv_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--year", type=int, required=True, help="Calendar year of the file.")
@click.option(
    "--dsn",
    envvar="PG_DSN",
    required=True,
    help="Postgres DSN (defaults to PG_DSN env var).",
)
@click.option(
    "--source-url",
    default=CMS_DATA_JSON_URL,
    show_default=True,
    help="Source URL recorded on every row (the CMS catalog by default).",
)
def cmd_load(csv_path: Path, year: int, dsn: str, source_url: str) -> None:
    """Parse and load a previously-fetched Part D Prescriber CSV (no network).

    Use this when the CSV is already on disk (from a prior fetch, or an
    operator hand-pull for an air-gapped load). For end-to-end resolve +
    fetch + load, use ``fetch-and-load``.
    """
    import psycopg as _psycopg  # late import keeps the help screen fast

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    _validate_year(year)

    fetch = FetchResult(
        path=csv_path,
        source_url=source_url,
        source_sha256=sha256_file(csv_path),
        source_vintage=f"CY{year}",
        n_bytes=csv_path.stat().st_size,
        cache_hit=True,
    )
    parse = parse_partd_csv(fetch, data_year=year)
    click.echo(f"parsed {parse.n_rows} rows")

    with _psycopg.connect(dsn) as conn:
        n = load_to_postgres(parse, conn)
        conn.commit()
    click.echo(f"loaded {n} rows (data_year={year})")


@cli.command("fetch-and-load")
@click.option("--year", type=int, required=True, help="Calendar year (e.g. 2023).")
@click.option(
    "--dest-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("data/cache/cms_partd_prescriber"),
    show_default=True,
)
@click.option("--overwrite/--no-overwrite", default=False, help="Force re-download.")
@click.option("--dsn", envvar="PG_DSN", required=True)
@click.option(
    "--url",
    default=None,
    help="Explicit CSV URL; bypasses catalog resolution.",
)
def cmd_fetch_and_load(
    year: int,
    dest_dir: Path,
    overwrite: bool,
    dsn: str,
    url: str | None,
) -> None:
    """Resolve + fetch + load one CY in a single step (the Dagster-tick command)."""
    import psycopg as _psycopg

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    fetch = fetch_partd_csv(
        data_year=year, dest_dir=dest_dir, overwrite=overwrite, url=url,
    )
    parse = parse_partd_csv(fetch, data_year=year)
    click.echo(f"parsed {parse.n_rows} rows (cache_hit={fetch.cache_hit})")

    with _psycopg.connect(dsn) as conn:
        n = load_to_postgres(parse, conn)
        conn.commit()
    click.echo(
        f"loaded {n} rows (data_year={year}, sha256={fetch.source_sha256})",
    )


if __name__ == "__main__":
    cli()
