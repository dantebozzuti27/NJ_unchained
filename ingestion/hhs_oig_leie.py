"""HHS-OIG LEIE (List of Excluded Individuals/Entities) ingester.

Downloads the monthly full-database CSV from
``https://oig.hhs.gov/exclusions/downloadables/UPDATED.csv`` and loads
it into ``raw.hhs_oig_leie``.

Why this ingester is shaped the way it is
-----------------------------------------
HHS publishes the LEIE as a single full-replace CSV every month. The
URL is stable across pulls (the file content is what changes), and the
schema has been frozen at 18 columns since the 2016 EXE -> CSV
migration. The ingester therefore only needs to:

1. Conditionally re-download the file (LEIE files are small -- ~12 MB
   currently -- so the conditional-GET cost-benefit is dominated by
   "skip-on-unchanged" simplicity, not by bandwidth concerns).
2. Parse the CSV.
3. Compute a stable surrogate ``record_hash`` per row.
4. UPSERT into ``raw.hhs_oig_leie`` by ``record_hash``, bumping
   ``last_seen_at = now()`` on conflict.

The UPSERT semantics are the key: HHS does NOT publish a "this entry
disappeared" delta. Reinstatements simply drop out of the next pull.
The platform detects them by watching ``last_seen_at`` fall behind
``MAX(last_seen_at)``; the canonical view ``derived.v_leie_active``
encapsulates that logic.

What this ingester deliberately does NOT do
-------------------------------------------
* SSN/EIN crosswalk -- HHS does not publish those (Privacy Act).
* Monthly-supplement parsing -- the full-file pull plus our
  ``last_seen_at`` logic captures the same information without two
  more parser shapes. Supplements stay future work.
* Entity resolution to FEC / USAspending names -- that is a separate
  layer with its own canonical name function and a separate migration.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import logging
import re
import shutil
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

LEIE_FULL_DB_URL: Final[str] = (
    "https://oig.hhs.gov/exclusions/downloadables/UPDATED.csv"
)

# httpx timeouts. The LEIE file is small but oig.hhs.gov has been known
# to serve slowly under load -- give the GET a generous 5 minutes; the
# HEAD probe stays short so a network blip on cache-check doesn't stall
# a Dagster materialization tick.
_HTTP_HEAD_TIMEOUT_S: Final[float] = 30.0
_HTTP_GET_TIMEOUT_S:  Final[float] = 300.0

# OIG's documented column order. The CSV's first row is the header
# (these names verbatim, ALL CAPS). We do not detect schema vintages
# the way the LCA loader does because LEIE has had ONE schema since
# the CSV format was introduced in 2016; a column drift would be a
# major HHS event and should fail loud, not silently re-map.
LEIE_COLUMNS: Final[tuple[str, ...]] = (
    "LASTNAME",
    "FIRSTNAME",
    "MIDNAME",
    "BUSNAME",
    "GENERAL",
    "SPECIALTY",
    "UPIN",
    "NPI",
    "DOB",
    "ADDRESS",
    "CITY",
    "STATE",
    "ZIP",
    "EXCLTYPE",
    "EXCLDATE",
    "REINDATE",
    "WAIVERDATE",
    "WVRSTATE",
)

# Lower-case column names matching raw.hhs_oig_leie. Order matters --
# COPY uses this exact order in its column list.
_LEIE_RAW_COLUMNS: Final[tuple[str, ...]] = tuple(c.lower() for c in LEIE_COLUMNS)

# Columns that are part of the canonical record-hash input, in the
# order their values are concatenated. Stable across pulls IFF the
# underlying LEIE row content is byte-identical (modulo the trim and
# uppercase pass below); a profile correction by HHS will produce a
# different hash, which is the desired behavior (the platform notices
# corrections and stops matching the old hash).
_HASH_COLUMNS: Final[tuple[str, ...]] = (
    "lastname", "firstname", "midname", "busname",
    "dob", "excltype", "excldate",
    "general", "specialty", "upin", "npi",
    "address", "city", "state", "zip",
)

# Vintage-month regex / format. YYYY-MM is the platform's canonical
# month encoding (matches CHAR(7) constraint on the table).
_VINTAGE_MONTH_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9]{4}-[0-9]{2}$")

_DATE_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9]{8}$")

# COPY chunk size. LEIE rows are ~200 bytes each; 4 MiB buffers ~20K
# rows -- one or two flushes for the full ~80K-row file.
_COPY_CHUNK_SIZE: Final[int] = 4 * 1024 * 1024


# ============================================================================
# Fetch
# ============================================================================


@dataclass(frozen=True)
class FetchResult:
    """Output of :func:`fetch_leie_csv`."""

    path:           Path
    source_url:     str
    source_sha256:  str
    source_vintage: str  # ETag or Last-Modified, whichever the server returns
    n_bytes:        int
    cache_hit:      bool


def fetch_leie_csv(
    *,
    dest_dir: Path,
    overwrite: bool = False,
    url: str = LEIE_FULL_DB_URL,
) -> FetchResult:
    """Download UPDATED.csv with conditional-GET semantics.

    The same URL is reused every month (HHS replaces the file in place),
    so a HEAD probe + content-length comparison is the cheapest way to
    detect "no new pull". When the local size matches the remote size
    we return a cache_hit FetchResult without re-downloading.

    Returns a FetchResult with provenance fields. Streams to a ``.part``
    sidecar that is renamed atomically on success, so an interrupted
    download never leaves a corrupt file in the cache.

    Raises:
        httpx.HTTPStatusError: on a non-2xx upstream response.
        IngestError: on a successful download that produced zero bytes
            (LEIE is never empty; an empty file is upstream malfunction
            we surface immediately, not load-as-zero-rows).

    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    local = dest_dir / "leie_updated.csv"

    with httpx.Client(timeout=_HTTP_HEAD_TIMEOUT_S, follow_redirects=True) as client:
        head_resp = client.head(url)
        head_resp.raise_for_status()
        remote_etag    = head_resp.headers.get("etag", "").strip('"')
        remote_lastmod = head_resp.headers.get("last-modified", "")
        remote_size    = int(head_resp.headers.get("content-length", "0") or 0)

    vintage = remote_etag or remote_lastmod or dt.date.today().isoformat()

    if local.exists() and not overwrite:
        local_size = local.stat().st_size
        if remote_size and local_size == remote_size:
            log.info(
                "leie.fetch: cache hit for %s (size=%d bytes)", local.name, local_size,
            )
            return FetchResult(
                path=local,
                source_url=url,
                source_sha256=sha256_file(local),
                source_vintage=vintage,
                n_bytes=local_size,
                cache_hit=True,
            )
        log.info(
            "leie.fetch: cache stale for %s (local=%d, remote=%d) -- re-downloading",
            local.name, local_size, remote_size,
        )

    log.info("leie.fetch: downloading %s -> %s", url, local)
    tmp = local.with_suffix(local.suffix + ".part")
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
                f"LEIE upstream {url} returned 0 bytes; refusing to write "
                "an empty cache file.",
            )
        shutil.move(tmp, local)
    log.info("leie.fetch: wrote %s (%.1f KiB)", local, n / 1024)
    return FetchResult(
        path=local,
        source_url=url,
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
    """Output of :func:`parse_leie_csv`."""

    rows:           list[tuple[str, ...]]   # one tuple per data row, len == 18
    source_url:     str
    source_sha256:  str
    source_vintage: str
    n_rows:         int


def _normalize_for_hash(value: str) -> str:
    """Return *value* upper-cased and stripped, with NULs/CRLF stripped.

    HHS publishes mixed-case, sometimes leading/trailing whitespace. A
    record_hash should be stable across that noise -- two LEIE rows
    that differ only in trailing whitespace are the same row.
    """
    if not value:
        return ""
    # NUL bytes are theoretically possible if a parser ever leaks them
    # through; strip defensively so the hash never mixes them in.
    cleaned = value.replace("\x00", "")
    return cleaned.strip().upper()


def compute_record_hash(row_dict: dict[str, str]) -> str:
    """Compute the stable SHA-256 surrogate key for a LEIE row.

    Concatenates the canonicalized values of :data:`_HASH_COLUMNS` with
    pipe delimiters and hashes the result. The pipe is safe because no
    LEIE field legitimately contains a pipe -- LEIE is comma-separated
    and pipes never appear in published rows.
    """
    parts = [_normalize_for_hash(row_dict.get(col, "")) for col in _HASH_COLUMNS]
    payload = "|".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _coerce_date_string(value: str, *, field: str) -> str | None:
    """Validate / pass through an LEIE date field.

    LEIE date fields are 8-digit YYYYMMDD strings, with "00000000"
    used as the "absent" sentinel. Anything else (e.g., empty string,
    7 digits, or a real-looking date that fails the regex) raises an
    IngestError so the operator notices the schema drift instead of
    silently writing NULL.

    Returns the cleaned value, or None when the field is empty
    (REINDATE / WAIVERDATE are both legitimately empty for never-
    reinstated, never-waived rows).
    """
    if value is None:
        return None
    s = value.strip()
    if not s:
        return None
    if not _DATE_RE.match(s):
        raise IngestError(
            f"LEIE {field}={value!r} is not 8 digits; "
            "expected YYYYMMDD or '00000000' or empty.",
        )
    return s


def parse_leie_csv(fetch: FetchResult) -> ParseResult:
    """Parse the LEIE UPDATED.csv into a list of cleaned row tuples.

    Validates that:

    * The header row matches :data:`LEIE_COLUMNS` exactly. A drift here
      should fail loud (not silently re-map), so we can surface it as
      an HHS schema-change event and update the loader deliberately.
    * Every data row has exactly 18 fields.
    * EXCLDATE is a non-empty 8-digit string (mandatory); REINDATE,
      WAIVERDATE, DOB are 8-digit strings or empty.
    * Each row has at least one of LASTNAME or BUSNAME populated.

    Returns a list of cleaned tuples -- not a Polars DataFrame -- because
    we need to compute the per-row record_hash before COPY anyway, and
    a tuple-of-strings is the natural intermediate.
    """
    text = fetch.path.read_text(encoding="utf-8-sig", newline="")
    if not text.strip():
        raise IngestError(f"LEIE file {fetch.path} is empty after read.")

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise IngestError(f"LEIE file {fetch.path} has no header row.") from exc

    # Strip BOM / whitespace from header tokens, normalize-case for compare.
    canonical_header = tuple(h.strip().upper() for h in header)
    if canonical_header != LEIE_COLUMNS:
        raise IngestError(
            f"LEIE schema drift: header={canonical_header!r}; "
            f"expected={LEIE_COLUMNS!r}. Update LEIE_COLUMNS deliberately.",
        )

    cleaned: list[tuple[str, ...]] = []
    for line_no, row in enumerate(reader, start=2):
        if not row or all(not c.strip() for c in row):
            continue  # skip blank lines (rare but cheap to tolerate)
        if len(row) != len(LEIE_COLUMNS):
            raise IngestError(
                f"LEIE row {line_no}: got {len(row)} fields, "
                f"expected {len(LEIE_COLUMNS)}: {row!r}",
            )

        d = dict(zip(LEIE_COLUMNS, row, strict=True))

        # Treat empty strings as NULL for the storage path.
        for key in LEIE_COLUMNS:
            d[key] = d[key].strip()

        if not d["EXCLTYPE"]:
            raise IngestError(
                f"LEIE row {line_no}: empty EXCLTYPE (NOT NULL in raw schema).",
            )
        excldate = _coerce_date_string(d["EXCLDATE"], field="EXCLDATE")
        if excldate is None:
            raise IngestError(
                f"LEIE row {line_no}: empty EXCLDATE (NOT NULL in raw schema).",
            )
        d["EXCLDATE"] = excldate
        for date_field in ("REINDATE", "WAIVERDATE"):
            d[date_field] = _coerce_date_string(d[date_field], field=date_field) or ""

        # Sanity: at least one of LASTNAME or BUSNAME populated. A row
        # with neither is a parser bug or upstream corruption; either
        # way we refuse to load it.
        if not d["LASTNAME"] and not d["BUSNAME"]:
            raise IngestError(
                f"LEIE row {line_no}: both LASTNAME and BUSNAME are empty.",
            )

        # Build an output tuple aligned to _LEIE_RAW_COLUMNS order.
        ordered = tuple(d[c] for c in LEIE_COLUMNS)
        cleaned.append(ordered)

    if not cleaned:
        raise IngestError(
            f"LEIE file {fetch.path} parsed 0 data rows; "
            "expected ~80K. Refusing to load an empty pull.",
        )

    return ParseResult(
        rows=cleaned,
        source_url=fetch.source_url,
        source_sha256=fetch.source_sha256,
        source_vintage=fetch.source_vintage,
        n_rows=len(cleaned),
    )


# ============================================================================
# Load
# ============================================================================


def _validate_vintage_month(vintage_month: str) -> None:
    if not _VINTAGE_MONTH_RE.match(vintage_month):
        raise IngestError(
            f"vintage_month={vintage_month!r} must match YYYY-MM "
            "(e.g. '2026-03').",
        )


def _row_to_csv_record(
    raw_values: tuple[str, ...],
    *,
    vintage_month: str,
    source_url: str,
    source_sha256: str,
) -> tuple[str, ...]:
    """Build the COPY column tuple for one row.

    Order matches the COPY column list in :func:`load_to_postgres`:
        record_hash, <18 LEIE columns>, vintage_month, source_url, source_sha256.

    last_seen_at and ingested_at are NOT supplied -- they default to
    now() on INSERT and we set last_seen_at explicitly on UPDATE in the
    upsert SQL, so they never need to ride on the COPY stream.
    """
    row_dict = dict(zip(_LEIE_RAW_COLUMNS, raw_values, strict=True))
    record_hash = compute_record_hash(row_dict)
    return (record_hash, *raw_values, vintage_month, source_url, source_sha256)


def _iter_csv_lines(
    parse: ParseResult,
    *,
    vintage_month: str,
) -> Iterator[bytes]:
    """Yield CSV-formatted rows ready for COPY ... FORMAT csv."""
    for raw_values in parse.rows:
        record = _row_to_csv_record(
            raw_values,
            vintage_month=vintage_month,
            source_url=parse.source_url,
            source_sha256=parse.source_sha256,
        )
        line = io.StringIO()
        csv.writer(line, lineterminator="\n").writerow(record)
        yield line.getvalue().encode("utf-8")


def load_to_postgres(
    parse: ParseResult,
    conn: psycopg.Connection,
    *,
    vintage_month: str,
) -> int:
    """UPSERT the parsed rows into raw.hhs_oig_leie.

    Strategy:

    1. Stream the cleaned rows into a TEMP staging table (same schema
       as raw.hhs_oig_leie, no constraints, on commit drop) via COPY.
    2. ``INSERT INTO raw.hhs_oig_leie ... SELECT ... FROM staging
        ON CONFLICT (record_hash) DO UPDATE SET last_seen_at = now()``.

    Why two-phase rather than COPY directly into raw.hhs_oig_leie:
    Postgres' COPY does not support ON CONFLICT. COPY-then-UPSERT is
    the canonical idiom, costs ~one extra full-table scan, and is well
    under one second for an 80K-row file.

    Returns the total number of rows touched (inserted + updated).
    """
    _validate_vintage_month(vintage_month)
    if parse.n_rows == 0:
        log.info("leie.load: nothing to upsert (parse.n_rows=0)")
        return 0

    # Staging table mirrors the columns we COPY into. Skipping the
    # CHECK constraints + foreign-keyish nuances on the staging side
    # keeps the COPY fast; the real raw table catches anything bad
    # at the UPSERT stage.
    staging_cols = (
        "record_hash",
        *_LEIE_RAW_COLUMNS,
        "vintage_month", "source_url", "source_sha256",
    )
    staging_col_idents = sql.SQL(", ").join(
        sql.Identifier(c) for c in staging_cols
    )

    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "CREATE TEMP TABLE leie_staging ("
                "    record_hash CHAR(64), "
                "    lastname TEXT, firstname TEXT, midname TEXT, "
                "    busname TEXT, "
                "    general TEXT, specialty TEXT, "
                "    upin TEXT, npi TEXT, dob TEXT, "
                "    address TEXT, city TEXT, state TEXT, zip TEXT, "
                "    excltype TEXT, excldate TEXT, "
                "    reindate TEXT, waiverdate TEXT, wvrstate TEXT, "
                "    vintage_month CHAR(7), "
                "    source_url TEXT, source_sha256 CHAR(64)"
                ") ON COMMIT DROP",
            ),
        )

        copy_query = sql.SQL(
            "COPY leie_staging ({cols}) FROM STDIN "
            "WITH (FORMAT csv, NULL '')",
        ).format(cols=staging_col_idents)

        # Stream the row generator in chunked writes. csv.writer outputs
        # ~200 byte lines for typical LEIE rows; chunking keeps the
        # psycopg buffer churn modest without materializing the full
        # CSV in memory.
        with cur.copy(copy_query) as cp:
            buf = bytearray()
            for line_bytes in _iter_csv_lines(parse, vintage_month=vintage_month):
                buf.extend(line_bytes)
                if len(buf) >= _COPY_CHUNK_SIZE:
                    cp.write(bytes(buf))
                    buf.clear()
            if buf:
                cp.write(bytes(buf))

        # NULL-out empty-string date columns so the CHAR(8) CHECK
        # regex in raw.hhs_oig_leie never sees them. Doing this in
        # SQL (not Python) keeps the staged COPY a verbatim mirror of
        # the LEIE bytes.
        cur.execute(
            "UPDATE leie_staging SET "
            "  reindate   = NULLIF(reindate,   ''), "
            "  waiverdate = NULLIF(waiverdate, '')",
        )
        # Empty string -> NULL for everything that's NULLable in the raw
        # schema. This lets the CHECK constraints on raw.hhs_oig_leie
        # (e.g., npi must be NULL or 10 digits) operate on real NULLs
        # instead of empty strings.
        for col in (
            "lastname", "firstname", "midname", "busname",
            "general", "specialty", "upin", "npi", "dob",
            "address", "city", "state", "zip", "wvrstate",
        ):
            cur.execute(
                sql.SQL("UPDATE leie_staging SET {c} = NULLIF({c}, '')")
                .format(c=sql.Identifier(col)),
            )

        # UPSERT into raw. Insert-cols + select-cols match the staging
        # column order; on conflict bump last_seen_at + refresh
        # vintage_month and provenance so the latest pull's metadata
        # wins for subsequent active-vs-historical filtering.
        #
        # WHY DISTINCT ON (record_hash):
        #   LEIE UPDATED.csv contains a small number of intra-batch
        #   duplicate rows (~25 hash collisions / ~51 rows out of ~83K
        #   in the May 2026 vintage). The duplicates have IDENTICAL
        #   content for every column (same EXCLDATE, EXCLTYPE, REINDATE,
        #   WAIVERDATE, NPI, name, address) -- they are a genuine HHS
        #   publishing quirk where the same exclusion row appears
        #   twice in the bulk download. Without dedup the UPSERT
        #   raises CardinalityViolation ("ON CONFLICT DO UPDATE
        #   command cannot affect row a second time") because two
        #   staging rows hit the same target record_hash within one
        #   statement.
        #
        #   We dedupe staging via DISTINCT ON (record_hash) ORDER BY
        #   record_hash, ctid -- ctid is the deterministic physical
        #   row identifier within a transaction, so re-runs of the
        #   same vintage pick the same surviving row. Since the
        #   duplicates are pixel-identical, the choice is immaterial
        #   to downstream consumers; this is purely a Postgres ON
        #   CONFLICT mechanics workaround, not an information loss.
        cur.execute(
            """
            WITH deduped AS (
                SELECT DISTINCT ON (record_hash)
                    record_hash,
                    lastname, firstname, midname, busname,
                    general, specialty, upin, npi, dob,
                    address, city, state, zip,
                    excltype, excldate, reindate, waiverdate, wvrstate,
                    vintage_month, source_url, source_sha256
                FROM leie_staging
                ORDER BY record_hash, ctid
            )
            INSERT INTO raw.hhs_oig_leie (
                record_hash,
                lastname, firstname, midname, busname,
                general, specialty, upin, npi, dob,
                address, city, state, zip,
                excltype, excldate, reindate, waiverdate, wvrstate,
                vintage_month, source_url, source_sha256
            )
            SELECT
                record_hash,
                lastname, firstname, midname, busname,
                general, specialty, upin, npi, dob,
                address, city, state, zip,
                excltype, excldate, reindate, waiverdate, wvrstate,
                vintage_month, source_url, source_sha256
            FROM deduped
            ON CONFLICT (record_hash) DO UPDATE SET
                last_seen_at  = now(),
                vintage_month = EXCLUDED.vintage_month,
                source_url    = EXCLUDED.source_url,
                source_sha256 = EXCLUDED.source_sha256
            """,
        )
        n_touched = cur.rowcount

    log.info(
        "leie.load: UPSERTed %d rows into raw.hhs_oig_leie (vintage=%s, sha256=%s)",
        n_touched, vintage_month, parse.source_sha256[:16] + "...",
    )
    return n_touched


# ============================================================================
# Click CLI
# ============================================================================


def _default_vintage_month() -> str:
    """Today's YYYY-MM in America/New_York (the platform's canonical zone).

    Used as the default for ``--vintage-month`` so analysts running the
    loader without an explicit value still get a sane stamp. Operators
    backfilling old pulls supply ``--vintage-month`` themselves.
    """
    today = dt.datetime.now(dt.UTC).date()
    return today.strftime("%Y-%m")


@click.group()
def cli() -> None:
    """HHS-OIG LEIE ingester."""


@cli.command("fetch")
@click.option(
    "--dest-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("data/cache/hhs_oig_leie"),
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
    default=LEIE_FULL_DB_URL,
    show_default=True,
    help="Override the source URL (mostly useful for tests).",
)
def cmd_fetch(dest_dir: Path, overwrite: bool, url: str) -> None:
    """Download UPDATED.csv with conditional-GET semantics."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    fetch = fetch_leie_csv(dest_dir=dest_dir, overwrite=overwrite, url=url)
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
@click.option(
    "--dsn",
    envvar="PG_DSN",
    required=True,
    help="Postgres DSN (defaults to PG_DSN env var).",
)
@click.option(
    "--vintage-month",
    default=None,
    help=(
        "YYYY-MM stamp recorded on every row. "
        "Defaults to the current month (UTC)."
    ),
)
@click.option(
    "--source-url",
    default=LEIE_FULL_DB_URL,
    show_default=True,
    help="Source URL recorded on every row.",
)
def cmd_load(
    csv_path: Path,
    dsn: str,
    vintage_month: str | None,
    source_url: str,
) -> None:
    """Parse and UPSERT a previously-fetched LEIE CSV.

    Use this when the CSV is already on disk (from a prior fetch, or
    operator-supplied for an air-gapped load). For end-to-end fetch +
    load in one step, use ``fetch-and-load``.
    """
    import psycopg as _psycopg  # late import keeps the help screen fast

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    eff_vintage = vintage_month or _default_vintage_month()

    fetch = FetchResult(
        path=csv_path,
        source_url=source_url,
        source_sha256=sha256_file(csv_path),
        source_vintage="local",
        n_bytes=csv_path.stat().st_size,
        cache_hit=True,
    )
    parse = parse_leie_csv(fetch)
    click.echo(f"parsed {parse.n_rows} rows")

    with _psycopg.connect(dsn) as conn:
        n = load_to_postgres(parse, conn, vintage_month=eff_vintage)
        conn.commit()
    click.echo(f"upserted {n} rows (vintage_month={eff_vintage})")


@cli.command("fetch-and-load")
@click.option(
    "--dest-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("data/cache/hhs_oig_leie"),
    show_default=True,
)
@click.option(
    "--overwrite/--no-overwrite",
    default=False,
    help="Force re-download.",
)
@click.option(
    "--dsn",
    envvar="PG_DSN",
    required=True,
)
@click.option(
    "--vintage-month",
    default=None,
)
@click.option(
    "--url",
    default=LEIE_FULL_DB_URL,
    show_default=True,
)
def cmd_fetch_and_load(
    dest_dir: Path,
    overwrite: bool,
    dsn: str,
    vintage_month: str | None,
    url: str,
) -> None:
    """Fetch UPDATED.csv and UPSERT it in one step.

    The recommended Dagster-tick command. Conditional-GET means a
    no-change tick is cheap (HEAD probe + last_seen_at no-op).
    """
    import psycopg as _psycopg

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    eff_vintage = vintage_month or _default_vintage_month()

    fetch = fetch_leie_csv(dest_dir=dest_dir, overwrite=overwrite, url=url)
    parse = parse_leie_csv(fetch)
    click.echo(f"parsed {parse.n_rows} rows (cache_hit={fetch.cache_hit})")

    with _psycopg.connect(dsn) as conn:
        n = load_to_postgres(parse, conn, vintage_month=eff_vintage)
        conn.commit()
    click.echo(
        f"upserted {n} rows (vintage_month={eff_vintage}, "
        f"sha256={fetch.source_sha256})",
    )


if __name__ == "__main__":
    cli()
