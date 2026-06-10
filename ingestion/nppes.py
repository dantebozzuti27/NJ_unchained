"""NPPES NPI Registry monthly bulk-file ingester (provider identity spine).

Downloads the monthly *V.2* full-replace ZIP published by CMS at
``https://download.cms.gov/nppes/NPI_Files.html`` and loads the provider
identity columns into ``raw.nppes_provider``.

Why this ingester is shaped the way it is
-----------------------------------------
NPPES is the National Plan and Provider Enumeration System -- the
authoritative registry mapping every ``NPI`` (10-char National Provider
Identifier) to a provider's legal name, business practice address, and
primary taxonomy. We ingest it for ONE structural reason: it is the
*identity spine*. HHS-OIG's LEIE exclusion list publishes name + (often
blank) NPI; NPPES is what later lets a name-only LEIE exclusion be
resolved to a concrete NPI and practice location.

The bulk file is brutal on resources -- the unzipped
``npidata_pfile_YYYYMMDD-YYYYMMDD.csv`` is ~10 GB with ~330 columns and
~8M rows. Every design choice here is about NOT materializing that in
memory and NOT loading 8M national rows into a single-VM Postgres when
the platform's mandate is New Jersey:

1. **Stream, never slurp.** :func:`fetch_nppes_bulk` streams the ZIP to a
   ``.part`` sidecar and atomically renames; the npidata member is then
   stream-extracted to the cache via ``shutil.copyfileobj`` (constant
   memory). :func:`parse_nppes_csv` iterates the CSV row-by-row with
   ``csv.reader`` -- it NEVER calls ``read_text()`` on a multi-GB file.
2. **Select by header name, not position.** The file has ~330 columns
   whose order CMS reserves the right to extend. We resolve a small set
   of long, exactly-quoted header strings to their column indices once,
   then project only those. A missing required header fails loud
   (schema-drift event), it does not silently mis-map.
3. **Size-bound to NJ by default.** The default parse filters to
   ``Provider Business Practice Location Address State Name == 'NJ'``.
   ``--national`` / ``state_filter=None`` opts back into the full file.
4. **Full-replace snapshot, not partitioned.** NPPES has no published
   delta/tombstone stream; the monthly file IS the truth. Load is
   ``TRUNCATE`` then ``COPY``. There is no per-month partition because
   we keep exactly one snapshot (the latest), stamped with provenance.

What this ingester deliberately does NOT do
-------------------------------------------
* It does not parse the ``othername``, ``pl`` (practice location), or
  ``endpoint`` companion CSVs in the ZIP -- only the main npidata member.
* It does not resolve LEIE <-> NPPES identities. That entity-resolution
  layer is a separate derived stage with its own canonical-name function
  and migration; this ingester only lands the raw spine.
* It does not interpret ``NPI Deactivation Date`` (kept as raw text). A
  deactivated NPI is still a true historical identity; the active/dead
  split is a derived-view concern, not a load-time filter.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import click
import httpx
from psycopg import sql

from ingestion._base import IngestError, sha256_file

if TYPE_CHECKING:
    from collections.abc import Iterator

    import psycopg

log = logging.getLogger(__name__)


# ============================================================================
# Constants
# ============================================================================

# Landing page that lists the monthly + weekly NPPES dissemination files.
# Surfaced to operators; not fetched programmatically.
NPPES_FILES_PAGE_URL: Final[str] = "https://download.cms.gov/nppes/NPI_Files.html"

# Default monthly *V.2* full-dissemination ZIP. CMS names this file by
# month, e.g. ``NPPES_Data_Dissemination_June_2026_V2.zip``. The concrete
# URL changes every month, so callers should pass ``url=`` (CLI: ``--url``)
# with the exact filename copied from NPPES_FILES_PAGE_URL. This constant
# is a documented, overridable default -- it is never silently trusted to
# be "current".
NPPES_BULK_ZIP_URL: Final[str] = (
    "https://download.cms.gov/nppes/NPPES_Data_Dissemination_June_2026_V2.zip"
)

# httpx timeouts. The ZIP is ~1 GB compressed; give the GET a long fuse.
# The HEAD probe stays short so a cache-check blip never stalls a tick.
_HTTP_HEAD_TIMEOUT_S: Final[float] = 30.0
_HTTP_GET_TIMEOUT_S:  Final[float] = 3600.0

# Source-CSV header names, quoted EXACTLY as CMS publishes them (the file
# has ~330 columns; these are the only ten we project). Order here is the
# canonical projection order used everywhere downstream.
NPI_COL:          Final[str] = "NPI"
ENTITY_TYPE_COL:  Final[str] = "Entity Type Code"
LAST_NAME_COL:    Final[str] = "Provider Last Name (Legal Name)"
FIRST_NAME_COL:   Final[str] = "Provider First Name"
ORG_NAME_COL:     Final[str] = "Provider Organization Name (Legal Business Name)"
CITY_COL:         Final[str] = "Provider Business Practice Location Address City Name"
STATE_COL:        Final[str] = "Provider Business Practice Location Address State Name"
POSTAL_COL:       Final[str] = "Provider Business Practice Location Address Postal Code"
TAXONOMY_COL:     Final[str] = "Healthcare Provider Taxonomy Code_1"
DEACTIVATION_COL: Final[str] = "NPI Deactivation Date"

# Source header names in projection order. parse_nppes_csv requires every
# one of these to be present in the header; a miss is a schema-drift error.
NPPES_SOURCE_COLUMNS: Final[tuple[str, ...]] = (
    NPI_COL,
    ENTITY_TYPE_COL,
    LAST_NAME_COL,
    FIRST_NAME_COL,
    ORG_NAME_COL,
    CITY_COL,
    STATE_COL,
    POSTAL_COL,
    TAXONOMY_COL,
    DEACTIVATION_COL,
)

# raw.nppes_provider column names for the projected payload, in the SAME
# order as NPPES_SOURCE_COLUMNS. Note POSTAL_COL maps to practice_zip5
# (the LEFT 5 chars), not a verbatim copy. COPY uses this exact order.
_RAW_PAYLOAD_COLUMNS: Final[tuple[str, ...]] = (
    "npi",
    "entity_type_code",
    "provider_last_name",
    "provider_first_name",
    "provider_org_name",
    "practice_city",
    "practice_state",
    "practice_zip5",
    "taxonomy_code_1",
    "deactivation_date",
)

# Full COPY column list = payload columns + the three provenance columns.
# ingested_at is omitted: it DEFAULTs to now() in the table.
_RAW_COPY_COLUMNS: Final[tuple[str, ...]] = (
    *_RAW_PAYLOAD_COLUMNS,
    "source_url",
    "source_sha256",
    "source_vintage",
)

# Identifies the main provider file member inside the ZIP and excludes the
# ``_FileHeader`` / ``_fileheader`` companion. The date range is the
# dissemination window CMS stamps into the filename -- we lift it verbatim
# as source_vintage.
_NPIDATA_MEMBER_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|/)npidata_pfile_(?P<vintage>\d{8}-\d{8})\.csv$",
    re.IGNORECASE,
)

# NPIs are exactly 10 digits (a Luhn-checked identifier; we validate shape,
# not the check digit, at the raw layer).
_NPI_RE: Final[re.Pattern[str]] = re.compile(r"^\d{10}$")

# COPY chunk size. npidata rows are wide; 8 MiB amortizes psycopg buffer
# churn without materializing the projected CSV in memory.
_COPY_CHUNK_SIZE: Final[int] = 8 * 1024 * 1024

# Default size-bound: the platform's mandate is New Jersey. Operators opt
# into the full national file with --national / state_filter=None.
DEFAULT_STATE_FILTER: Final[str] = "NJ"


# ============================================================================
# Fetch
# ============================================================================


@dataclass(frozen=True)
class FetchResult:
    """Output of :func:`fetch_nppes_bulk`.

    ``path`` points at the *extracted* npidata CSV (not the ZIP), so the
    parser can stream it directly. ``source_sha256`` fingerprints the
    downloaded ZIP -- the true artifact at ``source_url`` -- and
    ``source_vintage`` is the ``YYYYMMDD-YYYYMMDD`` window lifted from the
    npidata member's filename.
    """

    path:           Path
    source_url:     str
    source_sha256:  str
    source_vintage: str
    n_bytes:        int
    cache_hit:      bool


def _find_npidata_member(names: list[str]) -> str:
    """Return the single npidata member name from a ZIP namelist.

    Raises IngestError when zero or more than one member matches -- both
    are upstream-shape surprises we refuse to guess through.
    """
    matches = [n for n in names if _NPIDATA_MEMBER_RE.search(n)]
    if not matches:
        raise IngestError(
            "NPPES ZIP contains no npidata_pfile_YYYYMMDD-YYYYMMDD.csv "
            f"member; saw: {names[:10]}",
        )
    if len(matches) > 1:
        raise IngestError(
            f"NPPES ZIP contains multiple npidata members: {matches}",
        )
    return matches[0]


def _vintage_from_member(member: str) -> str:
    """Lift the ``YYYYMMDD-YYYYMMDD`` dissemination window from a member name."""
    m = _NPIDATA_MEMBER_RE.search(member)
    if m is None:  # pragma: no cover - guarded by _find_npidata_member
        raise IngestError(f"cannot parse vintage from member {member!r}")
    return m.group("vintage")


def _extract_npidata_to_csv(zip_path: Path, dest_csv: Path) -> tuple[str, str]:
    """Stream-extract the npidata member of *zip_path* to *dest_csv*.

    Returns ``(member_name, vintage)``. Uses ``shutil.copyfileobj`` so a
    ~10 GB member never lands in memory. Writes to a ``.part`` sidecar and
    renames atomically so an interrupted extraction never leaves a corrupt
    CSV in the cache.
    """
    with zipfile.ZipFile(zip_path) as zf:
        member = _find_npidata_member(zf.namelist())
        vintage = _vintage_from_member(member)
        tmp = dest_csv.with_suffix(dest_csv.suffix + ".part")
        with zf.open(member) as src, tmp.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=1 << 20)
        shutil.move(tmp, dest_csv)
    return member, vintage


def fetch_nppes_bulk(
    *,
    dest_dir: Path,
    overwrite: bool = False,
    url: str = NPPES_BULK_ZIP_URL,
) -> FetchResult:
    """Download the monthly NPPES ZIP and extract its npidata CSV.

    Conditional-GET by content-length: a HEAD probe compares the remote
    ZIP size against the cached ZIP. On a match (and an already-extracted
    CSV present) we return a ``cache_hit`` FetchResult without
    re-downloading or re-extracting.

    The ZIP streams to a ``.part`` sidecar renamed atomically on success;
    the npidata member is then stream-extracted (see
    :func:`_extract_npidata_to_csv`).

    Raises:
        httpx.HTTPStatusError: on a non-2xx upstream response.
        IngestError: on a zero-byte download, or a ZIP whose npidata
            member cannot be uniquely identified.

    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dest_dir / "nppes_bulk.zip"
    csv_path = dest_dir / "npidata.csv"

    with httpx.Client(timeout=_HTTP_HEAD_TIMEOUT_S, follow_redirects=True) as client:
        head_resp = client.head(url)
        head_resp.raise_for_status()
        remote_size = int(head_resp.headers.get("content-length", "0") or 0)

    if zip_path.exists() and csv_path.exists() and not overwrite:
        local_size = zip_path.stat().st_size
        if remote_size and local_size == remote_size:
            with zipfile.ZipFile(zip_path) as zf:
                vintage = _vintage_from_member(_find_npidata_member(zf.namelist()))
            log.info("nppes.fetch: cache hit for %s (size=%d)", zip_path.name, local_size)
            return FetchResult(
                path=csv_path,
                source_url=url,
                source_sha256=sha256_file(zip_path),
                source_vintage=vintage,
                n_bytes=csv_path.stat().st_size,
                cache_hit=True,
            )
        log.info(
            "nppes.fetch: cache stale (local=%d, remote=%d) -- re-downloading",
            local_size, remote_size,
        )

    log.info("nppes.fetch: downloading %s -> %s", url, zip_path)
    tmp = zip_path.with_suffix(zip_path.suffix + ".part")
    with (
        httpx.Client(timeout=_HTTP_GET_TIMEOUT_S, follow_redirects=True) as client,
        client.stream("GET", url) as resp,
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
                f"NPPES upstream {url} returned 0 bytes; refusing to write "
                "an empty cache file.",
            )
        shutil.move(tmp, zip_path)
    log.info("nppes.fetch: wrote %s (%.1f MiB)", zip_path, n / (1024 * 1024))

    member, vintage = _extract_npidata_to_csv(zip_path, csv_path)
    log.info("nppes.fetch: extracted %s (vintage=%s)", member, vintage)
    return FetchResult(
        path=csv_path,
        source_url=url,
        source_sha256=sha256_file(zip_path),
        source_vintage=vintage,
        n_bytes=csv_path.stat().st_size,
        cache_hit=False,
    )


# ============================================================================
# Parse
# ============================================================================


@dataclass(frozen=True)
class ParseResult:
    """Output of :func:`parse_nppes_csv`.

    Each ``rows`` tuple has exactly ``len(_RAW_PAYLOAD_COLUMNS)`` elements
    in ``_RAW_PAYLOAD_COLUMNS`` order, with empty string used for SQL NULL
    (the COPY uses ``NULL ''``). ``state_filter`` records the size-bound
    that produced this result (``None`` == national).
    """

    rows:           list[tuple[str, ...]]
    source_url:     str
    source_sha256:  str
    source_vintage: str
    n_rows:         int
    state_filter:   str | None


def _left_zip5(postal: str) -> str:
    """Return the left 5 chars of a stripped postal code (blank -> '').

    NPPES postal codes are either ZIP5 or ZIP9; the raw layer keeps only
    the 5-digit prefix. We do NOT zero-pad a short code -- a value that
    lost a leading zero upstream is surfaced as-is, never silently
    "repaired" into a different ZIP.
    """
    s = postal.strip()
    return s[:5]


def _coerce_entity_type(value: str, *, line_no: int) -> str:
    """Validate the Entity Type Code (blank -> '', else '1' or '2').

    ``1`` = individual, ``2`` = organization. Deactivated NPIs legitimately
    carry a blank entity type, so blank is allowed (-> NULL). Any other
    non-blank value is upstream drift we refuse to load.
    """
    s = value.strip()
    if not s:
        return ""
    if s not in ("1", "2"):
        raise IngestError(
            f"NPPES row {line_no}: Entity Type Code={value!r} is not "
            "blank, '1', or '2'.",
        )
    return s


def _resolve_header_indices(header: list[str]) -> dict[str, int]:
    """Map each required source column to its index in *header*.

    Raises IngestError naming any missing column -- a CMS header rename or
    drop must fail loud, not silently project NULLs.
    """
    # First occurrence of each header name wins; our target columns are
    # unique in the real NPPES header.
    positions: dict[str, int] = {}
    for i, name in enumerate(header):
        positions.setdefault(name.strip(), i)
    resolved: dict[str, int] = {}
    missing: list[str] = []
    for col in NPPES_SOURCE_COLUMNS:
        if col in positions:
            resolved[col] = positions[col]
        else:
            missing.append(col)
    if missing:
        raise IngestError(
            f"NPPES schema drift: header is missing required columns "
            f"{missing}. Update NPPES_SOURCE_COLUMNS deliberately.",
        )
    return resolved


def parse_nppes_csv(
    fetch: FetchResult,
    *,
    state_filter: str | None = DEFAULT_STATE_FILTER,
) -> ParseResult:
    """Stream-parse the npidata CSV into projected, filtered row tuples.

    Iterates the file row-by-row (constant memory regardless of the ~10 GB
    source). For every row:

    * ``NPI`` must be exactly 10 digits (validated, kept as a string).
    * ``Entity Type Code`` is validated to blank/'1'/'2'.
    * ``practice_zip5`` is the LEFT 5 chars of the postal code.
    * Empty strings are preserved as ``''`` and become SQL NULL at COPY.

    When ``state_filter`` is set (default ``'NJ'``), only rows whose
    ``Provider Business Practice Location Address State Name`` equals it
    (case-insensitive) are kept. ``state_filter=None`` keeps everything.

    Raises IngestError on header drift, a malformed NPI, a bad entity-type
    code, or a parse that yields zero rows (an empty pull is upstream
    malfunction, not a valid snapshot).
    """
    norm_filter = state_filter.strip().upper() if state_filter else None

    with fetch.path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise IngestError(f"NPPES file {fetch.path} has no header row.") from exc

        idx = _resolve_header_indices(header)
        i_npi   = idx[NPI_COL]
        i_etc   = idx[ENTITY_TYPE_COL]
        i_last  = idx[LAST_NAME_COL]
        i_first = idx[FIRST_NAME_COL]
        i_org   = idx[ORG_NAME_COL]
        i_city  = idx[CITY_COL]
        i_state = idx[STATE_COL]
        i_post  = idx[POSTAL_COL]
        i_tax   = idx[TAXONOMY_COL]
        i_deact = idx[DEACTIVATION_COL]
        max_idx = max(idx.values())

        rows: list[tuple[str, ...]] = []
        for line_no, row in enumerate(reader, start=2):
            if not row or all(not c.strip() for c in row):
                continue  # tolerate stray blank lines
            if len(row) <= max_idx:
                raise IngestError(
                    f"NPPES row {line_no}: has {len(row)} fields, need at "
                    f"least {max_idx + 1} to project required columns.",
                )

            state = row[i_state].strip()
            if norm_filter is not None and state.upper() != norm_filter:
                continue

            npi = row[i_npi].strip()
            if not _NPI_RE.match(npi):
                raise IngestError(
                    f"NPPES row {line_no}: NPI={npi!r} is not 10 digits.",
                )

            entity_type = _coerce_entity_type(row[i_etc], line_no=line_no)

            rows.append((
                npi,
                entity_type,
                row[i_last].strip(),
                row[i_first].strip(),
                row[i_org].strip(),
                row[i_city].strip(),
                state,
                _left_zip5(row[i_post]),
                row[i_tax].strip(),
                row[i_deact].strip(),
            ))

    if not rows:
        raise IngestError(
            f"NPPES file {fetch.path} parsed 0 rows "
            f"(state_filter={state_filter!r}); refusing to load an empty "
            "snapshot.",
        )

    return ParseResult(
        rows=rows,
        source_url=fetch.source_url,
        source_sha256=fetch.source_sha256,
        source_vintage=fetch.source_vintage,
        n_rows=len(rows),
        state_filter=state_filter,
    )


# ============================================================================
# Load
# ============================================================================


def _iter_csv_lines(parse: ParseResult) -> Iterator[bytes]:
    """Yield CSV-formatted rows (payload + provenance) for COPY ... FORMAT csv."""
    for payload in parse.rows:
        record = (
            *payload,
            parse.source_url,
            parse.source_sha256,
            parse.source_vintage,
        )
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\n").writerow(record)
        yield buf.getvalue().encode("utf-8")


def load_to_postgres(parse: ParseResult, conn: psycopg.Connection) -> int:
    """Full-replace ``raw.nppes_provider`` with the parsed snapshot.

    NPPES publishes no delta/tombstone stream -- the monthly file is the
    complete truth. The load is therefore TRUNCATE-then-COPY: we wipe the
    table and stream the projected rows straight in. There is no
    ON CONFLICT / staging dance because there is nothing to merge against.

    Empty-string fields become SQL NULL via ``NULL ''``; Postgres applies
    each column's input function during COPY, so ``entity_type_code``
    text ('1'/'2') casts to SMALLINT and blanks become NULL.

    Returns the number of rows inserted.
    """
    if parse.n_rows == 0:  # pragma: no cover - parse refuses empty results
        log.info("nppes.load: nothing to load (parse.n_rows=0)")
        return 0

    col_idents = sql.SQL(", ").join(sql.Identifier(c) for c in _RAW_COPY_COLUMNS)
    copy_query = sql.SQL(
        "COPY raw.nppes_provider ({cols}) FROM STDIN WITH (FORMAT csv, NULL '')",
    ).format(cols=col_idents)

    with conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE raw.nppes_provider")
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
        "nppes.load: replaced raw.nppes_provider with %d rows "
        "(vintage=%s, state_filter=%s, sha256=%s)",
        parse.n_rows, parse.source_vintage, parse.state_filter,
        parse.source_sha256[:16] + "...",
    )
    return parse.n_rows


# ============================================================================
# Click CLI
# ============================================================================


def _default_vintage() -> str:
    """Today's date (UTC) as YYYYMMDD-YYYYMMDD, for operator-supplied loads."""
    today = dt.datetime.now(dt.UTC).date().strftime("%Y%m%d")
    return f"{today}-{today}"


@click.group()
def cli() -> None:
    """NPPES NPI Registry monthly bulk-file ingester."""


@cli.command("fetch")
@click.option(
    "--dest-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("data/cache/nppes"),
    show_default=True,
    help="Where to cache the downloaded ZIP + extracted CSV.",
)
@click.option(
    "--overwrite/--no-overwrite",
    default=False,
    help="Force re-download even when the cached ZIP size matches remote.",
)
@click.option(
    "--url",
    default=NPPES_BULK_ZIP_URL,
    show_default=True,
    help="Override the monthly ZIP URL (copy the current filename from "
         f"{NPPES_FILES_PAGE_URL}).",
)
def cmd_fetch(dest_dir: Path, overwrite: bool, url: str) -> None:
    """Download the monthly ZIP and extract its npidata CSV."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    fetch = fetch_nppes_bulk(dest_dir=dest_dir, overwrite=overwrite, url=url)
    click.echo(f"path:           {fetch.path}")
    click.echo(f"source_url:     {fetch.source_url}")
    click.echo(f"source_sha256:  {fetch.source_sha256}")
    click.echo(f"source_vintage: {fetch.source_vintage}")
    click.echo(f"n_bytes:        {fetch.n_bytes}")
    click.echo(f"cache_hit:      {fetch.cache_hit}")


def _resolve_state_filter(state_filter: str, national: bool) -> str | None:
    """--national wins over --state-filter; otherwise use the given state."""
    return None if national else state_filter


@cli.command("load")
@click.argument(
    "csv_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--dsn", envvar="PG_DSN", required=True, help="Postgres DSN.")
@click.option(
    "--state-filter",
    default=DEFAULT_STATE_FILTER,
    show_default=True,
    help="Keep only this practice-location state.",
)
@click.option(
    "--national",
    is_flag=True,
    default=False,
    help="Load all states (overrides --state-filter).",
)
@click.option(
    "--source-url",
    default=NPPES_BULK_ZIP_URL,
    show_default=True,
    help="Source URL recorded on every row.",
)
@click.option(
    "--source-vintage",
    default=None,
    help="YYYYMMDD-YYYYMMDD window recorded on every row (default: today).",
)
def cmd_load(
    csv_path: Path,
    dsn: str,
    state_filter: str,
    national: bool,
    source_url: str,
    source_vintage: str | None,
) -> None:
    """Parse and full-replace-load a previously-extracted npidata CSV."""
    import psycopg as _psycopg

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    eff_filter = _resolve_state_filter(state_filter, national)
    eff_vintage = source_vintage or _default_vintage()

    fetch = FetchResult(
        path=csv_path,
        source_url=source_url,
        source_sha256=sha256_file(csv_path),
        source_vintage=eff_vintage,
        n_bytes=csv_path.stat().st_size,
        cache_hit=True,
    )
    parse = parse_nppes_csv(fetch, state_filter=eff_filter)
    click.echo(f"parsed {parse.n_rows} rows (state_filter={eff_filter})")

    with _psycopg.connect(dsn) as conn:
        n = load_to_postgres(parse, conn)
        conn.commit()
    click.echo(f"loaded {n} rows (vintage={eff_vintage})")


@cli.command("fetch-and-load")
@click.option(
    "--dest-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("data/cache/nppes"),
    show_default=True,
)
@click.option("--overwrite/--no-overwrite", default=False, help="Force re-download.")
@click.option("--dsn", envvar="PG_DSN", required=True)
@click.option(
    "--state-filter",
    default=DEFAULT_STATE_FILTER,
    show_default=True,
)
@click.option("--national", is_flag=True, default=False, help="Load all states.")
@click.option("--url", default=NPPES_BULK_ZIP_URL, show_default=True)
def cmd_fetch_and_load(
    dest_dir: Path,
    overwrite: bool,
    dsn: str,
    state_filter: str,
    national: bool,
    url: str,
) -> None:
    """Fetch the monthly ZIP and full-replace-load it in one step."""
    import psycopg as _psycopg

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    eff_filter = _resolve_state_filter(state_filter, national)

    fetch = fetch_nppes_bulk(dest_dir=dest_dir, overwrite=overwrite, url=url)
    parse = parse_nppes_csv(fetch, state_filter=eff_filter)
    click.echo(
        f"parsed {parse.n_rows} rows "
        f"(cache_hit={fetch.cache_hit}, state_filter={eff_filter})",
    )

    with _psycopg.connect(dsn) as conn:
        n = load_to_postgres(parse, conn)
        conn.commit()
    click.echo(
        f"loaded {n} rows (vintage={fetch.source_vintage}, "
        f"sha256={fetch.source_sha256})",
    )


if __name__ == "__main__":
    cli()
