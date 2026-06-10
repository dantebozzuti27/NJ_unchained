"""CMS "Medicare Physician & Other Practitioners - by Provider" ingester.

Loads the broadest "active Medicare biller" file CMS publishes -- one row
per rendering NPI per calendar year, summarizing that provider's full
Part B utilization and payments -- into ``raw.cms_physician_provider``.

Why this ingester exists
------------------------
The platform needs a keyless, free, authoritative roster of every NPI
that actively billed Medicare Part B in a given year, with enough
utilization/payment detail to flag outliers downstream (e.g. cross-
referencing the HHS-OIG LEIE exclusion list, or surfacing risk-score /
allowed-amount anomalies). The "by Provider" file is exactly that: one
NPI-level summary row, as opposed to the much larger "by Provider and
Service" file (one row per NPI x HCPCS), which is a separate dataset.

How the source is shaped
------------------------
CMS publishes this as one full-replace CSV **per calendar year** through
the DKAN catalog at ``https://data.cms.gov/data.json``. Each year is a
distinct ``distribution`` of a single ``dataset`` whose title is exactly
``"Medicare Physician & Other Practitioners - by Provider"``. The bulk
CSV URL is not stable across releases (it embeds a release-dated path),
so we never hardcode it: :func:`resolve_download_url` reads the master
catalog and picks the ``text/csv`` distribution whose title carries the
requested calendar year. An explicit ``url=`` override is still accepted
(mirrors the LEIE ingester) for air-gapped / pinned-vintage loads.

CRITICAL disambiguation: the catalog also contains a dataset titled
``"Medicare Physician & Other Practitioners - by Provider and Service"``
-- a strict superset string. We therefore match the dataset title with
``==`` (exact), never a substring/``in`` test, or we would silently
ingest the wrong (10x larger, per-service) file.

Column contract
---------------
The raw CSV has ~81 columns and the column *order* drifts across
vintages (CMS appends new chronic-condition prevalence columns over
time). We need only 12 of them, so we select **by header name** into a
name->index map rather than by position. The 12 CSV headers were
verified against the CMS data dictionary for the CY2023 vintage:

    Rndrng_NPI                 -> npi                  (kept as 10-char string)
    Rndrng_Prvdr_Last_Org_Name -> prvdr_last_org_name
    Rndrng_Prvdr_First_Name    -> prvdr_first_name
    Rndrng_Prvdr_City          -> prvdr_city
    Rndrng_Prvdr_State_Abrvtn  -> prvdr_state_abrvtn
    Rndrng_Prvdr_Type          -> prvdr_type
    Tot_Benes                  -> tot_benes
    Tot_Srvcs                  -> tot_srvcs
    Tot_Mdcr_Alowd_Amt         -> tot_mdcr_alowd_amt
    Tot_Mdcr_Pymt_Amt          -> tot_mdcr_pymt_amt
    Tot_Sbmtd_Chrg             -> tot_sbmtd_chrg
    Bene_Avg_Risk_Scre         -> bene_avg_risk_scre

Verifiable-data discipline
--------------------------
* NPI stays a 10-char string (no int cast -- leading-zero NPIs exist and
  the value is an identifier, not a quantity). A non-10-digit NPI fails
  loud rather than being silently coerced.
* Blank / suppressed numeric cells become SQL NULL, never 0. CMS leaves
  cells blank when a beneficiary count would fall below its small-cell
  threshold; representing that as 0 would fabricate a measurement.
* Idempotent load = ``DELETE WHERE data_year = %s`` then bulk insert, so
  re-running a year is a no-op on row count and always reproduces the
  same table state from the same source bytes.
* Every row carries ``source_url`` / ``source_sha256`` / ``source_vintage``
  provenance, exactly like the LEIE loader.

What this ingester deliberately does NOT do
--------------------------------------------
* The drug/medical splits, demographic counts, and chronic-condition
  prevalence columns are dropped at parse time. They are out of scope
  for the current "active biller roster + headline utilization" need;
  adding them is a column-list change plus a migration, not a reshape.
* No entity resolution to LEIE / FEC names -- that is a separate derived
  layer with its own canonical-name function and migration.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import click
import httpx

from ingestion._base import IngestError, sha256_file

if TYPE_CHECKING:
    from collections.abc import Iterator

    import psycopg

log = logging.getLogger(__name__)


# ============================================================================
# Constants
# ============================================================================

CMS_DATA_JSON_URL: Final[str] = "https://data.cms.gov/data.json"

# Exact dataset title. Matched with == (never substring) to avoid the
# sibling "... - by Provider and Service" dataset, whose title is a
# superset string of this one.
DATASET_TITLE: Final[str] = "Medicare Physician & Other Practitioners - by Provider"

# Earliest calendar year CMS publishes this dataset (the DKAN temporal
# coverage starts at CY2013). We refuse out-of-range years so a typo'd
# --data-year fails loud instead of silently resolving to nothing.
CMS_EARLIEST_YEAR: Final[int] = 2013
CMS_LATEST_YEAR: Final[int] = 2099

# httpx timeouts. The catalog + HEAD probes are quick; the bulk GET can
# be a multi-hundred-MB file, so it gets a generous ceiling.
_HTTP_CATALOG_TIMEOUT_S: Final[float] = 60.0
_HTTP_HEAD_TIMEOUT_S:    Final[float] = 30.0
_HTTP_GET_TIMEOUT_S:     Final[float] = 600.0

# (CSV header, raw column) pairs, in raw-table column order (excluding
# the data_year / provenance columns, which are supplied by the loader).
# Selection is by header NAME, so the physical CSV column order is
# irrelevant and may drift across vintages.
_CSV_TO_RAW: Final[tuple[tuple[str, str], ...]] = (
    ("Rndrng_NPI",                 "npi"),
    ("Rndrng_Prvdr_Last_Org_Name", "prvdr_last_org_name"),
    ("Rndrng_Prvdr_First_Name",    "prvdr_first_name"),
    ("Rndrng_Prvdr_City",          "prvdr_city"),
    ("Rndrng_Prvdr_State_Abrvtn",  "prvdr_state_abrvtn"),
    ("Rndrng_Prvdr_Type",          "prvdr_type"),
    ("Tot_Benes",                  "tot_benes"),
    ("Tot_Srvcs",                  "tot_srvcs"),
    ("Tot_Mdcr_Alowd_Amt",         "tot_mdcr_alowd_amt"),
    ("Tot_Mdcr_Pymt_Amt",          "tot_mdcr_pymt_amt"),
    ("Tot_Sbmtd_Chrg",             "tot_sbmtd_chrg"),
    ("Bene_Avg_Risk_Scre",         "bene_avg_risk_scre"),
)

# Required CSV headers (the keys we look up by name in the file header).
CMS_CSV_HEADERS: Final[tuple[str, ...]] = tuple(h for h, _ in _CSV_TO_RAW)

# Raw column names, in the order the parser emits each row tuple.
_RAW_DATA_COLUMNS: Final[tuple[str, ...]] = tuple(r for _, r in _CSV_TO_RAW)

# Subset of raw columns typed NUMERIC. Blank cells in these map to NULL.
_NUMERIC_RAW_COLUMNS: Final[frozenset[str]] = frozenset({
    "tot_benes", "tot_srvcs", "tot_mdcr_alowd_amt",
    "tot_mdcr_pymt_amt", "tot_sbmtd_chrg", "bene_avg_risk_scre",
})

# Full COPY column list, in stream order: CY param, the 12 data columns,
# then provenance. ingested_at defaults to now() on insert.
_COPY_COLUMNS: Final[tuple[str, ...]] = (
    "data_year", *_RAW_DATA_COLUMNS, "source_url", "source_sha256", "source_vintage",
)

# Default state filter. The national "by Provider" file is ~1.1M rows /
# ~250 MB in Postgres, which overruns a 512 MB free-tier Neon project, so
# the platform loads NJ by default (consistent with the Part D / Open
# Payments / NPPES ingesters). --national opts into the full file.
DEFAULT_STATE_FILTER: Final[str] = "NJ"

# CSV header carrying the rendering-provider state (used by the NJ filter).
_STATE_HEADER: Final[str] = "Rndrng_Prvdr_State_Abrvtn"

# NPI is a 10-digit identifier (NPPES standard). Kept as a string so
# leading zeros survive; validated, not int-cast.
_NPI_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9]{10}$")

# A bare decimal (optionally signed). CMS bulk CSV numerics carry no
# thousands separators or currency symbols, so anything else is drift.
_NUMERIC_RE: Final[re.Pattern[str]] = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")

# Distribution titles end with the temporal-coverage date, e.g.
# "... - by Provider : 2023-12-31"; the leading 4 digits are the
# calendar year of the data.
_DIST_DATE_RE: Final[re.Pattern[str]] = re.compile(r"([0-9]{4})-[0-9]{2}-[0-9]{2}")

# COPY chunk size. "by Provider" rows are short; 4 MiB buffers many
# thousands of rows per flush for the ~1.1M-row annual file.
_COPY_CHUNK_SIZE: Final[int] = 4 * 1024 * 1024


# ============================================================================
# Catalog resolution
# ============================================================================


def _select_download_url(catalog: dict[str, Any], *, data_year: int) -> str:
    """Pick the bulk-CSV download URL for *data_year* from a parsed data.json.

    Pure (no I/O): takes the already-parsed DKAN catalog dict and returns
    the ``downloadURL`` of the ``text/csv`` distribution belonging to the
    dataset whose title is exactly :data:`DATASET_TITLE` and whose
    distribution title carries *data_year*.

    Raises:
        IngestError: if the exact dataset title is absent, or no CSV
            distribution matches the requested year.

    """
    datasets = catalog.get("dataset", [])
    matches = [d for d in datasets if str(d.get("title", "")).strip() == DATASET_TITLE]
    if not matches:
        raise IngestError(
            f"CMS catalog has no dataset titled exactly {DATASET_TITLE!r}; "
            "the title may have changed upstream.",
        )

    # Exactly one dataset is expected for this exact title; if CMS ever
    # publishes duplicates we scan them all rather than guessing.
    for ds in matches:
        for dist in ds.get("distribution", []):
            if dist.get("mediaType") != "text/csv":
                continue
            url = dist.get("downloadURL")
            if not url:
                continue
            m = _DIST_DATE_RE.search(str(dist.get("title", "")))
            if m and int(m.group(1)) == data_year:
                return str(url)

    raise IngestError(
        f"CMS dataset {DATASET_TITLE!r} has no text/csv distribution for "
        f"calendar year {data_year}.",
    )


def resolve_download_url(
    *,
    data_year: int,
    client: httpx.Client | None = None,
) -> str:
    """Resolve the bulk-CSV download URL for *data_year* via data.json.

    Fetches the master catalog and delegates selection to
    :func:`_select_download_url`. Pass *client* to reuse an existing
    httpx client (e.g. from a Dagster resource); otherwise a short-lived
    one is created and closed.
    """
    _validate_data_year(data_year)
    created = client is None
    c = client or httpx.Client(
        timeout=_HTTP_CATALOG_TIMEOUT_S, follow_redirects=True,
    )
    try:
        resp = c.get(CMS_DATA_JSON_URL)
        resp.raise_for_status()
        catalog: dict[str, Any] = resp.json()
    finally:
        if created:
            c.close()
    return _select_download_url(catalog, data_year=data_year)


# ============================================================================
# Fetch
# ============================================================================


@dataclass(frozen=True)
class FetchResult:
    """Output of :func:`fetch_cms_provider_csv`."""

    path:           Path
    source_url:     str
    source_sha256:  str
    source_vintage: str  # ETag / Last-Modified, whichever the server returns
    n_bytes:        int
    cache_hit:      bool
    data_year:      int


def fetch_cms_provider_csv(
    *,
    data_year: int,
    dest_dir: Path,
    overwrite: bool = False,
    url: str | None = None,
    client: httpx.Client | None = None,
) -> FetchResult:
    """Download the by-Provider CSV for *data_year* with conditional-GET.

    Resolves the bulk URL from the DKAN catalog unless an explicit *url*
    is supplied. Uses a HEAD probe + content-length comparison to skip
    re-downloading an unchanged file. Streams to a ``.part`` sidecar that
    is renamed atomically on success, so an interrupted download never
    leaves a corrupt cache file.

    Raises:
        httpx.HTTPStatusError: on a non-2xx upstream response.
        IngestError: on a download that produced zero bytes.

    """
    _validate_data_year(data_year)
    eff_url = url or resolve_download_url(data_year=data_year, client=client)

    dest_dir.mkdir(parents=True, exist_ok=True)
    local = dest_dir / f"cms_physician_provider_{data_year}.csv"

    with httpx.Client(
        timeout=_HTTP_HEAD_TIMEOUT_S, follow_redirects=True,
    ) as head_client:
        head_resp = head_client.head(eff_url)
        head_resp.raise_for_status()
        remote_etag    = head_resp.headers.get("etag", "").strip('"')
        remote_lastmod = head_resp.headers.get("last-modified", "")
        remote_size    = int(head_resp.headers.get("content-length", "0") or 0)

    vintage = remote_etag or remote_lastmod or dt.date.today().isoformat()

    if local.exists() and not overwrite:
        local_size = local.stat().st_size
        if remote_size and local_size == remote_size:
            log.info(
                "cms.fetch: cache hit for %s (size=%d bytes)", local.name, local_size,
            )
            return FetchResult(
                path=local,
                source_url=eff_url,
                source_sha256=sha256_file(local),
                source_vintage=vintage,
                n_bytes=local_size,
                cache_hit=True,
                data_year=data_year,
            )
        log.info(
            "cms.fetch: cache stale for %s (local=%d, remote=%d) -- re-downloading",
            local.name, local_size, remote_size,
        )

    log.info("cms.fetch: downloading %s -> %s", eff_url, local)
    tmp = local.with_suffix(local.suffix + ".part")
    with (
        httpx.Client(timeout=_HTTP_GET_TIMEOUT_S, follow_redirects=True) as get_client,
        get_client.stream("GET", eff_url) as resp,
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
                f"CMS upstream {eff_url} returned 0 bytes; refusing to write "
                "an empty cache file.",
            )
        shutil.move(tmp, local)
    log.info("cms.fetch: wrote %s (%.1f MiB)", local, n / (1 << 20))
    return FetchResult(
        path=local,
        source_url=eff_url,
        source_sha256=sha256_file(local),
        source_vintage=vintage,
        n_bytes=n,
        cache_hit=False,
        data_year=data_year,
    )


# ============================================================================
# Parse
# ============================================================================


@dataclass(frozen=True)
class ParseResult:
    """Output of :func:`parse_cms_provider_csv`."""

    rows:           list[tuple[str, ...]]   # one tuple per row, len == len(_RAW_DATA_COLUMNS)
    source_url:     str
    source_sha256:  str
    source_vintage: str
    data_year:      int
    n_rows:         int


def _validate_data_year(data_year: int) -> None:
    if not CMS_EARLIEST_YEAR <= data_year <= CMS_LATEST_YEAR:
        raise IngestError(
            f"data_year={data_year!r} out of supported range "
            f"{CMS_EARLIEST_YEAR}..{CMS_LATEST_YEAR}.",
        )


def _clean_npi(value: str) -> str:
    """Return a validated 10-digit NPI string, or raise IngestError.

    NPI is an identifier, not a quantity: kept as a string so leading
    zeros survive. Anything that is not exactly 10 digits is drift we
    surface immediately rather than silently coercing.
    """
    s = value.strip()
    if not _NPI_RE.match(s):
        raise IngestError(
            f"NPI {value!r} is not a 10-digit string (Rndrng_NPI).",
        )
    return s


def _coerce_numeric(value: str, *, field: str) -> str:
    """Validate a numeric CSV cell; return '' for blank (-> SQL NULL).

    Blank/suppressed cells map to '' so the COPY ``NULL ''`` clause turns
    them into SQL NULL (never 0). A non-blank value that is not a bare
    decimal is drift and raises rather than loading garbage.
    """
    s = value.strip()
    if not s:
        return ""
    if not _NUMERIC_RE.match(s):
        raise IngestError(
            f"CMS {field}={value!r} is not a bare decimal; "
            "expected a number or an empty (suppressed) cell.",
        )
    return s


def parse_cms_provider_csv(
    fetch: FetchResult,
    *,
    state_filter: str | None = DEFAULT_STATE_FILTER,
) -> ParseResult:
    """Parse a by-Provider CSV into a list of cleaned row tuples.

    Validates that every required CSV header in :data:`CMS_CSV_HEADERS`
    is present (matched by name CASE-INSENSITIVELY, since CMS ships pure
    case drift between vintages -- physical order is ignored), that every
    row has the same field count as the header, that NPI is a 10-digit
    string, and that the six numeric columns hold a bare decimal or a
    blank (suppressed) cell.

    ``state_filter`` (default ``'NJ'``) keeps only rows whose rendering-
    provider state equals the code, case-insensitively, bounding the
    national ~1.1M-row file to a free-tier-safe slice. ``state_filter=None``
    (CLI ``--national``) keeps every state.

    Returns one tuple per data row in :data:`_RAW_DATA_COLUMNS` order
    (numeric cells are kept as their original decimal string so NUMERIC
    precision is preserved; blanks are ''). Raises IngestError if the
    file is empty or parses zero data rows.
    """
    if not fetch.path.exists():
        raise IngestError(f"CMS file not found: {fetch.path}")

    code = state_filter.strip().upper() if state_filter is not None else None
    if state_filter is not None and not code:
        raise IngestError("state_filter must be a non-empty code or None.")

    with fetch.path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise IngestError(f"CMS file {fetch.path} is empty.") from exc

        header = [h.strip() for h in header]
        # Case-insensitive header index: lowercased name -> position.
        index_ci: dict[str, int] = {}
        for i, name in enumerate(header):
            index_ci.setdefault(name.lower(), i)

        missing = [h for h in CMS_CSV_HEADERS if h.lower() not in index_ci]
        if missing:
            raise IngestError(
                f"CMS file {fetch.path} missing required headers: {missing}; "
                f"available (first 20): {header[:20]}",
            )

        col_idx = [index_ci[h.lower()] for h in CMS_CSV_HEADERS]
        state_idx = index_ci[_STATE_HEADER.lower()]
        n_header = len(header)

        cleaned: list[tuple[str, ...]] = []
        for line_no, row in enumerate(reader, start=2):
            if not row or all(not c.strip() for c in row):
                continue  # tolerate stray blank lines
            if len(row) != n_header:
                raise IngestError(
                    f"CMS row {line_no}: got {len(row)} fields, "
                    f"expected {n_header}.",
                )

            if code is not None and row[state_idx].strip().upper() != code:
                continue  # NJ filter (or whatever state was requested)

            values: list[str] = []
            for (_, raw_col), idx in zip(_CSV_TO_RAW, col_idx, strict=True):
                cell = row[idx]
                if raw_col == "npi":
                    values.append(_clean_npi(cell))
                elif raw_col in _NUMERIC_RAW_COLUMNS:
                    values.append(_coerce_numeric(cell, field=raw_col))
                else:
                    values.append(cell.strip())
            cleaned.append(tuple(values))

    if not cleaned:
        raise IngestError(
            f"CMS file {fetch.path} parsed 0 data rows"
            + (f" for state {code!r}" if code else "")
            + "; refusing to load an empty pull.",
        )

    return ParseResult(
        rows=cleaned,
        source_url=fetch.source_url,
        source_sha256=fetch.source_sha256,
        source_vintage=fetch.source_vintage,
        data_year=fetch.data_year,
        n_rows=len(cleaned),
    )


# ============================================================================
# Load
# ============================================================================


def _iter_copy_lines(parse: ParseResult) -> Iterator[bytes]:
    """Yield CSV-formatted COPY rows in :data:`_COPY_COLUMNS` order."""
    year = str(parse.data_year)
    for row in parse.rows:
        record = (
            year, *row, parse.source_url, parse.source_sha256, parse.source_vintage,
        )
        line = io.StringIO()
        csv.writer(line, lineterminator="\n").writerow(record)
        yield line.getvalue().encode("utf-8")


def load_to_postgres(parse: ParseResult, conn: psycopg.Connection) -> int:
    """Replace-load the parsed rows into raw.cms_physician_provider.

    Idempotent per year: ``DELETE WHERE data_year = %s`` then a single
    COPY of the parsed rows. Re-running the same year reproduces the same
    table state and leaves the row count unchanged. Because the by-Provider
    file is one row per NPI per year, the ``(data_year, npi)`` primary key
    never collides within a load.

    Returns the number of rows inserted.
    """
    from psycopg import sql

    _validate_data_year(parse.data_year)
    if parse.n_rows == 0:
        log.info("cms.load: nothing to load (parse.n_rows=0)")
        return 0

    col_idents = sql.SQL(", ").join(sql.Identifier(c) for c in _COPY_COLUMNS)
    copy_query = sql.SQL(
        "COPY raw.cms_physician_provider ({cols}) FROM STDIN "
        "WITH (FORMAT csv, NULL '')",
    ).format(cols=col_idents)

    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM raw.cms_physician_provider WHERE data_year = %s",
            (parse.data_year,),
        )
        with cur.copy(copy_query) as cp:
            buf = bytearray()
            for line_bytes in _iter_copy_lines(parse):
                buf.extend(line_bytes)
                if len(buf) >= _COPY_CHUNK_SIZE:
                    cp.write(bytes(buf))
                    buf.clear()
            if buf:
                cp.write(bytes(buf))

    log.info(
        "cms.load: loaded %d rows into raw.cms_physician_provider "
        "(data_year=%d, sha256=%s)",
        parse.n_rows, parse.data_year, parse.source_sha256[:16] + "...",
    )
    return parse.n_rows


# ============================================================================
# Click CLI
# ============================================================================


@click.group()
def cli() -> None:
    """CMS Medicare Physician & Other Practitioners (by Provider) ingester."""


@cli.command("fetch")
@click.option("--data-year", type=int, required=True, help="Calendar year to fetch.")
@click.option(
    "--dest-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("data/cache/cms_physician_provider"),
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
    help="Override the source URL (skips catalog resolution).",
)
def cmd_fetch(data_year: int, dest_dir: Path, overwrite: bool, url: str | None) -> None:
    """Download the by-Provider CSV for one calendar year."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    fetch = fetch_cms_provider_csv(
        data_year=data_year, dest_dir=dest_dir, overwrite=overwrite, url=url,
    )
    click.echo(f"path:           {fetch.path}")
    click.echo(f"source_url:     {fetch.source_url}")
    click.echo(f"source_sha256:  {fetch.source_sha256}")
    click.echo(f"source_vintage: {fetch.source_vintage}")
    click.echo(f"n_bytes:        {fetch.n_bytes}")
    click.echo(f"cache_hit:      {fetch.cache_hit}")
    click.echo(f"data_year:      {fetch.data_year}")


@cli.command("load")
@click.argument(
    "csv_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--data-year", type=int, required=True, help="Calendar year of the file.")
@click.option(
    "--dsn",
    envvar="PG_DSN",
    required=True,
    help="Postgres DSN (defaults to PG_DSN env var).",
)
@click.option(
    "--source-url",
    default=None,
    help=(
        "Source URL recorded on every row. If omitted, it is resolved from "
        "the CMS catalog (the ONLY network call this command can make; pass "
        "--source-url to keep the load fully offline)."
    ),
)
@click.option(
    "--source-vintage",
    default=None,
    help="Vintage stamp recorded on every row. Defaults to 'CY{data_year}'.",
)
@click.option(
    "--state-filter",
    default=DEFAULT_STATE_FILTER,
    show_default=True,
    help="Two-letter state code to keep (overrun guard for the 512 MB free tier).",
)
@click.option(
    "--national",
    is_flag=True,
    default=False,
    help="Load all states (overrides --state-filter; needs a paid/self-hosted DB).",
)
def cmd_load(
    csv_path: Path,
    data_year: int,
    dsn: str,
    source_url: str | None,
    source_vintage: str | None,
    state_filter: str,
    national: bool,
) -> None:
    """Parse and replace-load a previously-fetched by-Provider CSV.

    Operates on a local file: parse + load make no network calls. The
    only optional network call is resolving --source-url for provenance
    when it is not supplied.
    """
    import psycopg as _psycopg  # late import keeps the help screen fast

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    eff_url = source_url or resolve_download_url(data_year=data_year)
    eff_vintage = source_vintage or f"CY{data_year}"
    eff_filter = None if national else state_filter

    fetch = FetchResult(
        path=csv_path,
        source_url=eff_url,
        source_sha256=sha256_file(csv_path),
        source_vintage=eff_vintage,
        n_bytes=csv_path.stat().st_size,
        cache_hit=True,
        data_year=data_year,
    )
    parse = parse_cms_provider_csv(fetch, state_filter=eff_filter)
    click.echo(f"parsed {parse.n_rows} rows (state={eff_filter or 'ALL'})")

    with _psycopg.connect(dsn) as conn:
        n = load_to_postgres(parse, conn)
        conn.commit()
    click.echo(f"loaded {n} rows (data_year={data_year})")


@cli.command("fetch-and-load")
@click.option("--data-year", type=int, required=True)
@click.option(
    "--dest-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("data/cache/cms_physician_provider"),
    show_default=True,
)
@click.option("--overwrite/--no-overwrite", default=False, help="Force re-download.")
@click.option("--dsn", envvar="PG_DSN", required=True)
@click.option("--url", default=None, help="Override the source URL.")
@click.option(
    "--state-filter",
    default=DEFAULT_STATE_FILTER,
    show_default=True,
    help="Two-letter state code to keep (overrun guard for the 512 MB free tier).",
)
@click.option(
    "--national",
    is_flag=True,
    default=False,
    help="Load all states (overrides --state-filter; needs a paid/self-hosted DB).",
)
def cmd_fetch_and_load(
    data_year: int,
    dest_dir: Path,
    overwrite: bool,
    dsn: str,
    url: str | None,
    state_filter: str,
    national: bool,
) -> None:
    """Fetch the by-Provider CSV for a year and replace-load it in one step."""
    import psycopg as _psycopg

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    eff_filter = None if national else state_filter
    fetch = fetch_cms_provider_csv(
        data_year=data_year, dest_dir=dest_dir, overwrite=overwrite, url=url,
    )
    parse = parse_cms_provider_csv(fetch, state_filter=eff_filter)
    click.echo(
        f"parsed {parse.n_rows} rows "
        f"(cache_hit={fetch.cache_hit}, state={eff_filter or 'ALL'})",
    )

    with _psycopg.connect(dsn) as conn:
        n = load_to_postgres(parse, conn)
        conn.commit()
    click.echo(
        f"loaded {n} rows (data_year={data_year}, sha256={fetch.source_sha256})",
    )


if __name__ == "__main__":
    cli()
