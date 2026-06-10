"""CMS Open Payments -- General Payments detail ingester.

Downloads the per-program-year "General Payments Details" file from
``download.cms.gov`` and loads the columns the platform cares about into
``raw.cms_open_payments_general``.

What this powers
----------------
Open Payments records every payment / transfer-of-value an applicable
manufacturer or GPO (pharma, device makers) makes to a covered recipient
(a physician or non-physician practitioner, keyed by NPI). Joined to
Medicare Part D prescribing volume on the same NPI, it is the substrate
for a future kickback-correlation signal: "doctors who took the most
money from a drug maker also prescribed the most of that maker's drug."
This ingester only lands the raw payments; the NPI x prescribing join is
a separate derived layer.

Why this ingester is shaped the way it is
-----------------------------------------
* **The file is enormous.** The national General Payments detail file is
  ~900 MB compressed and ~7.6 GB / ~14.7M rows raw (PY2023, per the CMS
  Jan 2026 methodology doc). We therefore NEVER materialize the national
  file in Postgres by default: :func:`parse_general_payments` filters to
  ``Recipient_State == 'NJ'`` (the platform is NJ-focused) unless the
  caller opts into ``state_filter=None`` (CLI ``--national``). This bounds
  both storage and memory to the NJ slice (low tens of thousands of rows).
* **The download filename changes every refresh.** CMS republishes each
  program year under a dated name, e.g.
  ``PGYR2023_P01302025_01212025.zip`` (publication date + extract date).
  There is no stable "latest" alias. :func:`general_payments_zip_url`
  returns a verified pin for years we have confirmed and a best-effort
  template otherwise; an explicit ``url=`` always overrides, and the load
  path works from a local cached ZIP/CSV with no network at all.
* **Columns are addressed by name, not position.** The bulk CSV carries
  ~91 columns and CMS adds/reorders them across vintages. We resolve our
  12 target columns by header name and fail loud if any is missing, so a
  reorder upstream can never silently mis-map a column.

Column provenance
-----------------
All 12 source column names below are verbatim from Appendix B
("General Payments Detail (PY 2016 and Onwards)") of the CMS Open Payments
Methodology & Data Dictionary, January 2026:
https://www.cms.gov/files/document/open-payments-methodology-data-dictionary-document-january-2026.pdf

What this ingester deliberately does NOT do
--------------------------------------------
* Research payments / ownership-interest files -- separate file shapes,
  separate migrations, deferred.
* Drug names 2..5, payment context, teaching-hospital recipients --
  we keep only the headline columns the kickback signal needs.
* NPI x Part D entity resolution -- a derived layer, not raw ingest.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import shutil
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import click
import httpx

from ingestion._base import IngestError, sha256_file

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import IO

    import psycopg

log = logging.getLogger(__name__)


# ============================================================================
# Constants
# ============================================================================

# CMS publishes the per-year ZIPs at this host. Public, keyless.
# Source: CMS Open Payments dataset downloads page.
# https://www.cms.gov/priorities/key-initiatives/open-payments/data/dataset-downloads
OPEN_PAYMENTS_DOWNLOAD_BASE: Final[str] = "https://download.cms.gov/openpayments"

# Verified per-year ZIP URLs. The publication+extract date suffix changes
# on every CMS refresh, so each entry is a point-in-time pin verified
# against the dataset-downloads page (Jan 2026 refresh). For any year not
# pinned here, pass an explicit url= -- the dated suffix is not derivable.
_KNOWN_ZIP_URLS: Final[dict[int, str]] = {
    # PY2023, 874 MB, verified on the CMS downloads page (Jan 2026 refresh).
    2023: f"{OPEN_PAYMENTS_DOWNLOAD_BASE}/PGYR2023_P01302025_01212025.zip",
}

# Best-effort template for years we have not pinned. The two date tokens
# are placeholders that WILL be wrong for an un-pinned year (each refresh
# stamps its own dates); we surface that loudly and recommend url=.
_ZIP_URL_TEMPLATE: Final[str] = (
    OPEN_PAYMENTS_DOWNLOAD_BASE + "/PGYR{year}_P{pub}_{extract}.zip"
)
# The most recent publication/extract date strings we have observed, reused
# as the template default. Treated as a hint, never as ground truth.
_TEMPLATE_PUB_DATE: Final[str] = "01302025"
_TEMPLATE_EXTRACT_DATE: Final[str] = "01212025"

# The General Payments detail CSV member inside the ZIP. CMS names it
# ``OP_DTL_GNRL_PGYR{YYYY}_P{dates}.csv``. We match by pattern (not exact
# name) because the date suffix varies; the GNRL token disambiguates it
# from the research (RSRCH) and ownership (OWNRSHP) members.
_GENERAL_PAYMENTS_MEMBER_RE: Final[re.Pattern[str]] = re.compile(
    r"OP_DTL_GNRL_PGYR\d{4}.*\.csv$", re.IGNORECASE,
)

# Default recipient-state filter. The platform is NJ-focused and the
# national file is ~900 MB; filtering to NJ bounds storage and memory.
DEFAULT_STATE_FILTER: Final[str] = "NJ"

# Ordered mapping of (source CSV header name -> raw column name). The
# source names are verbatim from Appendix B of the CMS Jan-2026 data
# dictionary. Order matches the COPY column list in load_to_postgres and
# the raw.cms_open_payments_general column order.
GENERAL_PAYMENTS_COLUMN_MAP: Final[tuple[tuple[str, str], ...]] = (
    ("Record_ID", "record_id"),
    ("Program_Year", "program_year"),
    ("Covered_Recipient_NPI", "covered_recipient_npi"),
    ("Covered_Recipient_Profile_ID", "covered_recipient_profile_id"),
    ("Covered_Recipient_First_Name", "recipient_first_name"),
    ("Covered_Recipient_Last_Name", "recipient_last_name"),
    ("Recipient_State", "recipient_state"),
    (
        "Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name",
        "payer_name",
    ),
    ("Total_Amount_of_Payment_USDollars", "payment_amount"),
    ("Date_of_Payment", "payment_date"),
    ("Nature_of_Payment_or_Transfer_of_Value", "nature_of_payment"),
    (
        "Name_of_Drug_or_Biological_or_Device_or_Medical_Supply_1",
        "product_name",
    ),
)

# Source header names, in target order. Used for header validation.
_SOURCE_COLUMNS: Final[tuple[str, ...]] = tuple(
    src for src, _ in GENERAL_PAYMENTS_COLUMN_MAP
)
# Raw (destination) column names, in target order. Used by COPY.
_RAW_COLUMNS: Final[tuple[str, ...]] = tuple(
    dst for _, dst in GENERAL_PAYMENTS_COLUMN_MAP
)
# Index of the state column within a parsed record tuple (for filtering).
_STATE_IDX: Final[int] = _RAW_COLUMNS.index("recipient_state")
_RECORD_ID_IDX: Final[int] = _RAW_COLUMNS.index("record_id")
_PROGRAM_YEAR_IDX: Final[int] = _RAW_COLUMNS.index("program_year")

# httpx timeouts. The GET streams a ~900 MB file, so give it a generous
# 30-minute read budget; a HEAD probe stays short.
_HTTP_HEAD_TIMEOUT_S: Final[float] = 30.0
_HTTP_GET_TIMEOUT_S: Final[float] = 1800.0

# COPY buffer flush size. Open Payments NJ rows are ~150 bytes each; a
# 4 MiB buffer batches ~25K rows per flush.
_COPY_CHUNK_SIZE: Final[int] = 4 * 1024 * 1024

# A program year is a 4-digit calendar year.
_PROGRAM_YEAR_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9]{4}$")
# Publication-date token inside a CMS filename, e.g. ``_P01302025_``.
_PUB_DATE_RE: Final[re.Pattern[str]] = re.compile(r"_P([0-9]{2})([0-9]{2})([0-9]{4})")


# ============================================================================
# URL helper
# ============================================================================


def general_payments_zip_url(program_year: int) -> str:
    """Return the best-known General Payments ZIP URL for *program_year*.

    For years pinned in :data:`_KNOWN_ZIP_URLS` (verified against the CMS
    dataset-downloads page), returns the exact URL. For un-pinned years,
    returns a best-effort templated URL and logs a warning, because the
    publication/extract date tokens CMS stamps into the filename are not
    derivable and the URL will likely 404 -- callers should pass an
    explicit ``url=`` for un-pinned years.
    """
    if program_year < 2013 or program_year > 2099:
        raise IngestError(
            f"Open Payments program_year out of range: {program_year}; "
            "Open Payments data starts at PY2013.",
        )
    known = _KNOWN_ZIP_URLS.get(program_year)
    if known is not None:
        return known
    log.warning(
        "cms_open_payments: no pinned URL for PY%d; returning a templated "
        "guess that may 404. Pass url= with the exact filename from "
        "https://www.cms.gov/priorities/key-initiatives/open-payments/data/dataset-downloads",
        program_year,
    )
    return _ZIP_URL_TEMPLATE.format(
        year=program_year,
        pub=_TEMPLATE_PUB_DATE,
        extract=_TEMPLATE_EXTRACT_DATE,
    )


def _vintage_from_filename(name: str) -> str:
    """Derive a source_vintage stamp from a CMS ZIP/CSV filename.

    CMS embeds the publication date as ``_P{MMDDYYYY}_``; we render it as
    ISO ``YYYY-MM-DD``. Falls back to the bare filename when no token is
    present so source_vintage is always populated (NOT NULL in raw).
    """
    m = _PUB_DATE_RE.search(name)
    if m is None:
        return name
    mm, dd, yyyy = m.groups()
    return f"{yyyy}-{mm}-{dd}"


# ============================================================================
# Fetch
# ============================================================================


@dataclass(frozen=True)
class FetchResult:
    """Output of :func:`fetch_general_payments_zip`."""

    path:           Path
    source_url:     str
    source_sha256:  str
    source_vintage: str
    n_bytes:        int
    cache_hit:      bool


def fetch_general_payments_zip(
    program_year: int,
    *,
    dest_dir: Path,
    overwrite: bool = False,
    url: str | None = None,
) -> FetchResult:
    """Download the General Payments ZIP for *program_year* into *dest_dir*.

    The cache key is the URL basename, so distinct refreshes never clobber
    one another. Existence-based caching (not conditional-GET) is the right
    default here: the file is ~900 MB and the dated filename already encodes
    the vintage, so a present file of that exact name is authoritative.

    Streams to a ``.part`` sidecar renamed atomically on success, so an
    interrupted multi-hundred-MB download never leaves a corrupt cache file.

    Args:
        program_year: Open Payments program year (e.g. 2023).
        dest_dir: Cache directory; created if absent.
        overwrite: Force re-download even when the cache file exists.
        url: Explicit download URL. When ``None``, resolved via
            :func:`general_payments_zip_url`.

    Raises:
        httpx.HTTPStatusError: on a non-2xx upstream response.
        IngestError: on a successful download that produced zero bytes.

    """
    eff_url = url if url is not None else general_payments_zip_url(program_year)
    dest_dir.mkdir(parents=True, exist_ok=True)
    local = dest_dir / Path(eff_url).name
    vintage = _vintage_from_filename(local.name)

    if local.exists() and not overwrite:
        n_bytes = local.stat().st_size
        log.info(
            "cms_open_payments.fetch: cache hit for %s (%.1f MiB)",
            local.name, n_bytes / (1 << 20),
        )
        return FetchResult(
            path=local,
            source_url=eff_url,
            source_sha256=sha256_file(local),
            source_vintage=vintage,
            n_bytes=n_bytes,
            cache_hit=True,
        )

    log.info("cms_open_payments.fetch: downloading %s -> %s", eff_url, local)
    tmp = local.with_suffix(local.suffix + ".part")
    with (
        httpx.Client(timeout=_HTTP_GET_TIMEOUT_S, follow_redirects=True) as client,
        client.stream("GET", eff_url) as resp,
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
                f"Open Payments upstream {eff_url} returned 0 bytes; "
                "refusing to write an empty cache file.",
            )
        shutil.move(tmp, local)
    log.info("cms_open_payments.fetch: wrote %s (%.1f MiB)", local, n / (1 << 20))
    return FetchResult(
        path=local,
        source_url=eff_url,
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
    """Output of :func:`parse_general_payments`."""

    rows:           list[tuple[str, ...]]   # one tuple per kept row, len == 12
    source_url:     str
    source_sha256:  str
    source_vintage: str
    program_year:   int
    state_filter:   str | None
    n_rows:         int


def _find_general_payments_member(names: list[str]) -> str:
    """Return the single General Payments CSV member name in a ZIP listing.

    Raises IngestError if zero or multiple members match the GNRL pattern,
    so an upstream packaging change fails loud instead of loading the wrong
    file (e.g. the research-payments member).
    """
    matches = [n for n in names if _GENERAL_PAYMENTS_MEMBER_RE.search(n)]
    if not matches:
        raise IngestError(
            "No General Payments CSV member matched "
            f"{_GENERAL_PAYMENTS_MEMBER_RE.pattern!r} in ZIP; found: {names!r}",
        )
    if len(matches) > 1:
        raise IngestError(
            f"Multiple General Payments members matched in ZIP: {matches!r}",
        )
    return matches[0]


@contextmanager
def _open_general_payments_csv(path: Path) -> Iterator[tuple[str, IO[str]]]:
    """Yield ``(member_name, text_stream)`` for the General Payments CSV.

    Transparently handles both a downloaded ``.zip`` (reads the GNRL member
    by streaming it, never extracting the multi-GB file to disk) and a bare
    ``.csv`` (operator-supplied or already-extracted). ``utf-8-sig`` strips
    a BOM if present.
    """
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            member = _find_general_payments_member(zf.namelist())
            with (
                zf.open(member) as raw,
                io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text,
            ):
                yield member, text
    else:
        with path.open("r", encoding="utf-8-sig", newline="") as text:
            yield path.name, text


def _build_header_index(header: list[str]) -> dict[str, int]:
    """Map each required source column to its position in *header*.

    Raises IngestError listing every missing required column, so a schema
    drift surfaces all gaps at once rather than failing one at a time.
    """
    pos = {name.strip(): i for i, name in enumerate(header)}
    missing = [c for c in _SOURCE_COLUMNS if c not in pos]
    if missing:
        raise IngestError(
            "Open Payments header missing required columns: "
            f"{missing!r}. Verify against the CMS data dictionary "
            "(Appendix B) and update GENERAL_PAYMENTS_COLUMN_MAP.",
        )
    return {c: pos[c] for c in _SOURCE_COLUMNS}


def parse_general_payments(
    fetch: FetchResult,
    *,
    state_filter: str | None = DEFAULT_STATE_FILTER,
    expected_program_year: int | None = None,
) -> ParseResult:
    """Stream the General Payments CSV into cleaned row tuples.

    Columns are addressed by header name (the bulk file carries ~91
    columns); only the 12 in :data:`GENERAL_PAYMENTS_COLUMN_MAP` are kept.

    Filtering:
        * ``state_filter='NJ'`` (default) keeps only rows whose
          ``Recipient_State`` equals the filter (case-insensitive). This is
          the size bound that makes the national ~900 MB file tractable.
        * ``state_filter=None`` keeps every row (CLI ``--national``). This
          materializes the whole file's rows in memory; only use it when you
          actually want the national slice.

    Validation:
        * ``Record_ID`` must be non-empty (PRIMARY KEY / NOT NULL).
        * ``Program_Year`` must be a 4-digit year, and every kept row must
          share the same value (a per-year file has exactly one). If
          ``expected_program_year`` is given, the file's value must match it.
        * Blank values are left as empty strings here and converted to NULL
          at COPY time (``NULL ''``); ``payment_amount`` therefore becomes
          NULL when blank, never 0.

    Returns a :class:`ParseResult`. NPI and record_id are preserved as
    strings (never coerced to int); payment_date is preserved as raw text.
    """
    if state_filter is not None and not state_filter.strip():
        raise IngestError("state_filter must be a non-empty code or None.")
    norm_state = state_filter.strip().upper() if state_filter is not None else None

    rows: list[tuple[str, ...]] = []
    program_years: set[int] = set()

    with _open_general_payments_csv(fetch.path) as (member, text):
        reader = csv.reader(text)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise IngestError(
                f"Open Payments file {member} has no header row.",
            ) from exc

        idx = _build_header_index(header)
        col_positions = [idx[c] for c in _SOURCE_COLUMNS]
        max_pos = max(col_positions)

        for line_no, raw_row in enumerate(reader, start=2):
            if not raw_row or all(not c.strip() for c in raw_row):
                continue
            if len(raw_row) <= max_pos:
                raise IngestError(
                    f"Open Payments {member} row {line_no}: only "
                    f"{len(raw_row)} fields, need index {max_pos} "
                    "(Record_ID..product_name).",
                )

            record = tuple(raw_row[p].strip() for p in col_positions)

            if norm_state is not None and record[_STATE_IDX].upper() != norm_state:
                continue

            record_id = record[_RECORD_ID_IDX]
            if not record_id:
                raise IngestError(
                    f"Open Payments {member} row {line_no}: empty Record_ID "
                    "(PRIMARY KEY / NOT NULL).",
                )
            py_raw = record[_PROGRAM_YEAR_IDX]
            if not _PROGRAM_YEAR_RE.match(py_raw):
                raise IngestError(
                    f"Open Payments {member} row {line_no}: Program_Year="
                    f"{py_raw!r} is not a 4-digit year.",
                )
            program_years.add(int(py_raw))

            rows.append(record)

    if not rows:
        raise IngestError(
            f"Open Payments file {fetch.path} parsed 0 rows"
            + (f" for state {norm_state}" if norm_state else "")
            + ". Refusing to load an empty pull.",
        )

    if len(program_years) != 1:
        raise IngestError(
            "Open Payments file mixes multiple Program_Year values "
            f"{sorted(program_years)!r}; expected exactly one per-year file.",
        )
    program_year = program_years.pop()
    if expected_program_year is not None and program_year != expected_program_year:
        raise IngestError(
            f"Open Payments file Program_Year={program_year} does not match "
            f"expected {expected_program_year}.",
        )

    log.info(
        "cms_open_payments.parse: kept %d rows (state=%s, PY%d) from %s",
        len(rows), norm_state or "ALL", program_year, member,
    )
    return ParseResult(
        rows=rows,
        source_url=fetch.source_url,
        source_sha256=fetch.source_sha256,
        source_vintage=fetch.source_vintage,
        program_year=program_year,
        state_filter=norm_state,
        n_rows=len(rows),
    )


# ============================================================================
# Load
# ============================================================================


def _iter_csv_lines(parse: ParseResult) -> Iterator[bytes]:
    """Yield CSV-formatted rows (12 source cols + 3 provenance) for COPY."""
    for record in parse.rows:
        out = (
            *record,
            parse.source_url,
            parse.source_sha256,
            parse.source_vintage,
        )
        line = io.StringIO()
        csv.writer(line, lineterminator="\n").writerow(out)
        yield line.getvalue().encode("utf-8")


def load_to_postgres(
    parse: ParseResult,
    conn: psycopg.Connection,
) -> int:
    """Load parsed rows into raw.cms_open_payments_general (idempotent).

    Strategy:

    1. COPY the cleaned rows into a TEMP staging table (typed so Postgres
       parses program_year -> SMALLINT and payment_amount -> NUMERIC, with
       ``NULL ''`` converting blanks to NULL -- so a blank amount is NULL,
       never 0).
    2. ``DELETE FROM raw.cms_open_payments_general WHERE program_year = %s``
       to make the per-year load idempotent (re-running a year replaces it).
    3. ``INSERT ... SELECT DISTINCT ON (record_id) ... FROM staging`` --
       DISTINCT ON defends against any intra-file duplicate Record_ID
       (record_id is the PK; a dup would otherwise raise on INSERT).

    DELETE-then-insert (rather than ON CONFLICT upsert) matches the source
    contract: CMS republishes a whole program year at once, so the correct
    idempotent unit is "replace the year", not "merge individual rows".

    Returns the number of rows inserted.
    """
    if parse.n_rows == 0:
        log.info("cms_open_payments.load: nothing to load (n_rows=0)")
        return 0

    staging_cols = (*_RAW_COLUMNS, "source_url", "source_sha256", "source_vintage")
    insert_col_list = ", ".join(staging_cols)

    with conn.cursor() as cur:
        cur.execute(
            "CREATE TEMP TABLE cms_open_payments_staging ("
            "    record_id TEXT, "
            "    program_year SMALLINT, "
            "    covered_recipient_npi TEXT, "
            "    covered_recipient_profile_id TEXT, "
            "    recipient_first_name TEXT, "
            "    recipient_last_name TEXT, "
            "    recipient_state TEXT, "
            "    payer_name TEXT, "
            "    payment_amount NUMERIC, "
            "    payment_date TEXT, "
            "    nature_of_payment TEXT, "
            "    product_name TEXT, "
            "    source_url TEXT, "
            "    source_sha256 TEXT, "
            "    source_vintage TEXT"
            ") ON COMMIT DROP",
        )

        copy_query = (
            f"COPY cms_open_payments_staging ({insert_col_list}) "
            "FROM STDIN WITH (FORMAT csv, NULL '')"
        )
        with cur.copy(copy_query) as cp:
            buf = bytearray()
            for line_bytes in _iter_csv_lines(parse):
                buf.extend(line_bytes)
                if len(buf) >= _COPY_CHUNK_SIZE:
                    cp.write(bytes(buf))
                    buf.clear()
            if buf:
                cp.write(bytes(buf))

        cur.execute(
            "DELETE FROM raw.cms_open_payments_general WHERE program_year = %s",
            (parse.program_year,),
        )

        cur.execute(
            f"""
            INSERT INTO raw.cms_open_payments_general ({insert_col_list})
            SELECT DISTINCT ON (record_id) {insert_col_list}
            FROM cms_open_payments_staging
            ORDER BY record_id, ctid
            """,
        )
        n_inserted = cur.rowcount

    log.info(
        "cms_open_payments.load: inserted %d rows into "
        "raw.cms_open_payments_general (PY%d, sha256=%s)",
        n_inserted, parse.program_year, parse.source_sha256[:16] + "...",
    )
    return n_inserted


# ============================================================================
# Click CLI
# ============================================================================


@click.group()
def cli() -> None:
    """CMS Open Payments (General Payments) ingester."""


@cli.command("fetch")
@click.option("--program-year", type=int, required=True)
@click.option(
    "--dest-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("data/cache/cms_open_payments"),
    show_default=True,
    help="Where to cache the downloaded ZIP.",
)
@click.option(
    "--overwrite/--no-overwrite",
    default=False,
    help="Force re-download even when the cache file exists.",
)
@click.option(
    "--url",
    default=None,
    help="Explicit download URL (required for un-pinned program years).",
)
def cmd_fetch(
    program_year: int, dest_dir: Path, overwrite: bool, url: str | None,
) -> None:
    """Download a General Payments ZIP for one program year."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    fetch = fetch_general_payments_zip(
        program_year, dest_dir=dest_dir, overwrite=overwrite, url=url,
    )
    click.echo(f"path:           {fetch.path}")
    click.echo(f"source_url:     {fetch.source_url}")
    click.echo(f"source_sha256:  {fetch.source_sha256}")
    click.echo(f"source_vintage: {fetch.source_vintage}")
    click.echo(f"n_bytes:        {fetch.n_bytes}")
    click.echo(f"cache_hit:      {fetch.cache_hit}")


@cli.command("load")
@click.argument(
    "path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--dsn", envvar="PG_DSN", required=True, help="Postgres DSN.")
@click.option(
    "--national",
    is_flag=True,
    default=False,
    help="Load ALL states (default: NJ only, to bound storage).",
)
@click.option(
    "--source-url",
    default=None,
    help="Source URL recorded on every row (defaults to the file name).",
)
@click.option(
    "--program-year",
    type=int,
    default=None,
    help="Expected program year; validated against the file if given.",
)
def cmd_load(
    path: Path,
    dsn: str,
    national: bool,
    source_url: str | None,
    program_year: int | None,
) -> None:
    """Parse and load a previously-fetched ZIP or extracted CSV (no network)."""
    import psycopg as _psycopg

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    fetch = FetchResult(
        path=path,
        source_url=source_url if source_url is not None else path.name,
        source_sha256=sha256_file(path),
        source_vintage=_vintage_from_filename(path.name),
        n_bytes=path.stat().st_size,
        cache_hit=True,
    )
    parse = parse_general_payments(
        fetch,
        state_filter=None if national else DEFAULT_STATE_FILTER,
        expected_program_year=program_year,
    )
    click.echo(f"parsed {parse.n_rows} rows (state={parse.state_filter or 'ALL'})")

    with _psycopg.connect(dsn) as conn:
        n = load_to_postgres(parse, conn)
        conn.commit()
    click.echo(f"inserted {n} rows (program_year={parse.program_year})")


@cli.command("fetch-and-load")
@click.option("--program-year", type=int, required=True)
@click.option(
    "--dest-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("data/cache/cms_open_payments"),
    show_default=True,
)
@click.option("--overwrite/--no-overwrite", default=False, help="Force re-download.")
@click.option("--dsn", envvar="PG_DSN", required=True)
@click.option(
    "--national",
    is_flag=True,
    default=False,
    help="Load ALL states (default: NJ only, to bound storage).",
)
@click.option(
    "--url",
    default=None,
    help="Explicit download URL (required for un-pinned program years).",
)
def cmd_fetch_and_load(
    program_year: int,
    dest_dir: Path,
    overwrite: bool,
    dsn: str,
    national: bool,
    url: str | None,
) -> None:
    """Fetch a program year's ZIP and load it in one step (NJ-only by default)."""
    import psycopg as _psycopg

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    fetch = fetch_general_payments_zip(
        program_year, dest_dir=dest_dir, overwrite=overwrite, url=url,
    )
    parse = parse_general_payments(
        fetch,
        state_filter=None if national else DEFAULT_STATE_FILTER,
        expected_program_year=program_year,
    )
    click.echo(
        f"parsed {parse.n_rows} rows "
        f"(state={parse.state_filter or 'ALL'}, cache_hit={fetch.cache_hit})",
    )

    with _psycopg.connect(dsn) as conn:
        n = load_to_postgres(parse, conn)
        conn.commit()
    click.echo(
        f"inserted {n} rows (program_year={parse.program_year}, "
        f"sha256={fetch.source_sha256})",
    )


if __name__ == "__main__":
    cli()
