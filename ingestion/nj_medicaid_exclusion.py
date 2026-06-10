"""New Jersey Medicaid "Ineligible Provider" / debarment exclusion ingester.

Downloads the OpenSanctions structured mirror of the New Jersey Office
of the State Comptroller (OSC) Medicaid disqualification list and loads
it into ``raw.nj_medicaid_exclusion``.

Why this ingester is shaped the way it is
-----------------------------------------
This is NJ's state-level analog to the federal HHS-OIG LEIE
(:mod:`ingestion.hhs_oig_leie`), and it is deliberately built on the
same skeleton:

1. Conditionally re-download a small CSV (HEAD probe + content-length
   comparison; the file is ~900 KB so the cost-benefit is dominated by
   "skip-on-unchanged" simplicity, not bandwidth).
2. Parse the CSV.
3. Compute a stable surrogate ``record_hash`` per row from the canonical
   *content* columns (mirrors LEIE's :func:`compute_record_hash`).
4. UPSERT into ``raw.nj_medicaid_exclusion`` by ``record_hash``, bumping
   ``last_seen_at = now()`` on conflict.

The UPSERT semantics are the key, exactly as in LEIE: NJ does NOT
publish a "this entry was reinstated" delta. A reinstated/removed
provider simply drops out of the next pull. The platform detects that
by watching ``last_seen_at`` fall behind ``MAX(last_seen_at)``.

Data source (keyless)
----------------------
PRIMARY: the OpenSanctions dataset ``us_nj_med_exclusions`` publishes a
simplified tabular CSV daily at a stable "latest" URL::

    https://data.opensanctions.org/datasets/latest/us_nj_med_exclusions/targets.simple.csv

OpenSanctions re-derives this CSV from the authoritative NJ OSC PDF
(https://nj.gov/comptroller/doc/nj_debarment_list.pdf) on a ~daily
cadence and serves it from a CDN that honors ``ETag`` / ``Last-Modified``
/ ``Content-Length`` -- everything the conditional-GET path needs.

FALLBACK (documented, not networked): an operator can extract the OSC
PDF to a CSV with the same simplified columns and feed it to ``load``
with ``--source-url`` pointed at the PDF. ``load`` works entirely from a
local file with NO network access.

Column mapping (OpenSanctions targets.simple.csv -> raw contract)
-----------------------------------------------------------------
The simplified export carries 16 columns; we map a subset onto the raw
contract and intentionally leave two columns NULL because the simplified
export does not separate them:

* ``name``        -> ``full_name``      (verbatim)
* ``identifiers`` -> ``npi``            (first ``;``-segment that is 10
                                         digits; NJ warns NPI coverage is
                                         non-exhaustive, so blank is common)
* ``addresses``   -> ``address``        (verbatim, ``;``-joined multiples
                                         preserved -- lossless)
* ``addresses``   -> ``city``/``state``/``zip`` (best-effort parse of the
                                         FIRST address segment; left NULL
                                         when the trailing ``CITY, ST ZIP``
                                         pattern does not match -- no
                                         silent imputation)
* ``sanctions``   -> ``effective_date`` (verbatim sanction caption -- a
                                         date, a date range, or a
                                         ``;``-joined list)
* (none)          -> ``action``         (NULL: the simplified export has
                                         no per-row action-type column)
* (none)          -> ``expiration_date``(NULL: not separable from the
                                         ``sanctions`` caption in the
                                         simplified export)

What this ingester deliberately does NOT do
--------------------------------------------
* Parse action type / expiration out of the ``sanctions`` caption -- the
  simplified export collapses them into one free-text field, and
  splitting blindly would violate the "no silent interpolation" rule.
  Those two columns stay NULL from this source; the PDF fallback is the
  place to populate them if an operator extracts them deliberately.
* Entity resolution to the LEIE / CMS Medicare names -- that is a
  separate derived layer with its own canonical-name function.
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

NJ_MED_EXCLUSION_URL: Final[str] = (
    "https://data.opensanctions.org/datasets/latest/"
    "us_nj_med_exclusions/targets.simple.csv"
)

# httpx timeouts. The file is small (~900 KB) but routed through a CDN;
# keep the HEAD probe short so a cache-check blip does not stall a
# Dagster tick, and give the GET a generous window.
_HTTP_HEAD_TIMEOUT_S: Final[float] = 30.0
_HTTP_GET_TIMEOUT_S:  Final[float] = 300.0

# OpenSanctions' documented targets.simple.csv column order. The CSV's
# first row is the header (these names verbatim, lower-case). We compare
# the header EXACTLY and fail loud on drift (mirroring LEIE): OpenSanctions
# can evolve the simplified export, and a column add/rename must be a
# deliberate operator decision, not a silent re-map.
OPENSANCTIONS_COLUMNS: Final[tuple[str, ...]] = (
    "id",
    "schema",
    "name",
    "aliases",
    "birth_date",
    "countries",
    "addresses",
    "identifiers",
    "sanctions",
    "phones",
    "emails",
    "program_ids",
    "dataset",
    "first_seen",
    "last_seen",
    "last_change",
)

# Lower-case data columns of raw.nj_medicaid_exclusion, in COPY order.
# record_hash + provenance columns are appended separately in the COPY
# record builder, so they are not part of this tuple.
_RAW_COLUMNS: Final[tuple[str, ...]] = (
    "full_name",
    "npi",
    "address",
    "city",
    "state",
    "zip",
    "action",
    "effective_date",
    "expiration_date",
)

# Columns that feed the canonical record-hash input, in concatenation
# order. This is the full mapped *content* of a row (provenance excluded),
# so the hash is reproducible from the stored row and an OpenSanctions
# profile correction yields a new hash -- the platform's "track edits as
# new entities" contract, identical to LEIE.
_HASH_COLUMNS: Final[tuple[str, ...]] = _RAW_COLUMNS

# An NPI is exactly 10 digits (CMS NPPES). We extract the first ``;``-
# segment of the ``identifiers`` field that matches; anything else is
# discarded (the simplified export only ever carries NPIs here, but we
# guard rather than assume).
_NPI_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9]{10}$")

# Trailing "<CITY>, <ST> <ZIP>" matcher for a single US address segment.
# CITY is the last comma-delimited token before the state (so apartment
# lines like "..., APT 6, ELMWOOD PARK, NJ 07407" still resolve CITY to
# "ELMWOOD PARK"). ZIP is 5 digits or ZIP+4. A non-match leaves
# city/state/zip NULL rather than guessing.
_ADDR_TAIL_RE: Final[re.Pattern[str]] = re.compile(
    r",\s*(?P<city>[^,]+?)\s*,\s*(?P<state>[A-Za-z]{2})\s+"
    r"(?P<zip>[0-9]{5}(?:-[0-9]{4})?)\s*$",
)

# COPY chunk size. Rows are ~250 bytes; a 4 MiB buffer holds the whole
# ~3.3K-row file in one or two flushes.
_COPY_CHUNK_SIZE: Final[int] = 4 * 1024 * 1024


# ============================================================================
# Fetch
# ============================================================================


@dataclass(frozen=True)
class FetchResult:
    """Output of :func:`fetch_nj_med_exclusions`."""

    path:           Path
    source_url:     str
    source_sha256:  str
    source_vintage: str  # ETag or Last-Modified, whichever the CDN returns
    n_bytes:        int
    cache_hit:      bool


def fetch_nj_med_exclusions(
    *,
    dest_dir: Path,
    overwrite: bool = False,
    url: str = NJ_MED_EXCLUSION_URL,
) -> FetchResult:
    """Download targets.simple.csv with conditional-GET semantics.

    The "latest" URL is reused every pull (OpenSanctions replaces the
    file in place behind the CDN), so a HEAD probe + content-length
    comparison is the cheapest way to detect "no new pull". When the
    local size matches the remote size we return a ``cache_hit``
    FetchResult without re-downloading.

    Streams to a ``.part`` sidecar renamed atomically on success, so an
    interrupted download never leaves a corrupt file in the cache.

    Raises:
        httpx.HTTPStatusError: on a non-2xx upstream response.
        IngestError: on a successful download that produced zero bytes
            (the NJ list is never empty; an empty file is upstream
            malfunction we surface immediately, not load-as-zero-rows).

    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    local = dest_dir / "nj_med_exclusions.csv"

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
                "nj_med.fetch: cache hit for %s (size=%d bytes)",
                local.name, local_size,
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
            "nj_med.fetch: cache stale for %s (local=%d, remote=%d) -- re-downloading",
            local.name, local_size, remote_size,
        )

    log.info("nj_med.fetch: downloading %s -> %s", url, local)
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
                f"NJ Medicaid exclusion upstream {url} returned 0 bytes; "
                "refusing to write an empty cache file.",
            )
        shutil.move(tmp, local)
    log.info("nj_med.fetch: wrote %s (%.1f KiB)", local, n / 1024)
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
    """Output of :func:`parse_nj_med_csv`."""

    rows:           list[tuple[str, ...]]   # one tuple per row, len == 9
    source_url:     str
    source_sha256:  str
    source_vintage: str
    n_rows:         int


def _normalize_for_hash(value: str) -> str:
    """Return *value* upper-cased and stripped, with NULs stripped.

    OpenSanctions publishes mixed-case names/addresses with occasional
    leading/trailing whitespace. A record_hash should be stable across
    that noise -- two rows that differ only in trailing whitespace are
    the same row.
    """
    if not value:
        return ""
    # NUL bytes are theoretically possible if a parser ever leaks them
    # through; strip defensively so the hash never mixes them in.
    cleaned = value.replace("\x00", "")
    return cleaned.strip().upper()


def compute_record_hash(row_dict: dict[str, str]) -> str:
    """Compute the stable SHA-256 surrogate key for a mapped NJ row.

    Concatenates the canonicalized values of :data:`_HASH_COLUMNS` with
    pipe delimiters and hashes the result. The pipe is safe because the
    mapped content fields never legitimately contain one (OpenSanctions
    joins multi-values with ``;`` and ``,``, not ``|``).
    """
    parts = [_normalize_for_hash(row_dict.get(col, "")) for col in _HASH_COLUMNS]
    payload = "|".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _extract_npi(identifiers: str) -> str:
    """Return the first 10-digit NPI in a ``;``-joined identifiers field.

    Returns "" when no segment is a clean 10-digit NPI. NJ's list warns
    that NPI coverage is non-exhaustive, so a blank result is normal --
    we never fabricate one. When an entity carries multiple NPIs we keep
    the first; the full set is recoverable from the structured
    OpenSanctions JSON if a future ingester needs it.
    """
    if not identifiers:
        return ""
    for token in identifiers.split(";"):
        candidate = token.strip()
        if _NPI_RE.match(candidate):
            return candidate
    return ""


def _parse_address(addresses: str) -> tuple[str, str, str, str]:
    """Map the OpenSanctions ``addresses`` field onto (address, city, state, zip).

    ``address`` is the raw field verbatim (``;``-joined multiples
    preserved, so nothing is lost). ``city``/``state``/``zip`` are a
    best-effort parse of the FIRST address segment's trailing
    ``CITY, ST ZIP`` shape. When that pattern does not match -- a foreign
    address, a free-text "in care of" line, etc. -- the three structured
    fields stay "" (rendered NULL on load) rather than being guessed.
    """
    raw = addresses.strip()
    if not raw:
        return "", "", "", ""
    first_segment = raw.split(";", 1)[0].strip()
    match = _ADDR_TAIL_RE.search(first_segment)
    if match is None:
        return raw, "", "", ""
    return (
        raw,
        match.group("city").strip(),
        match.group("state").strip().upper(),
        match.group("zip").strip(),
    )


def _map_row(record: dict[str, str]) -> dict[str, str]:
    """Map one OpenSanctions row dict onto the raw-contract content dict.

    Keys of the returned dict are exactly :data:`_RAW_COLUMNS`. Empty
    strings are preserved here (and converted to SQL NULL at load time),
    so this function never invents data.
    """
    address, city, state, zip_code = _parse_address(record.get("addresses", ""))
    return {
        "full_name":       record.get("name", "").strip(),
        "npi":             _extract_npi(record.get("identifiers", "")),
        "address":         address,
        "city":            city,
        "state":           state,
        "zip":             zip_code,
        # The simplified export has no action-type / expiration columns;
        # both stay NULL from this source (see module docstring).
        "action":          "",
        "effective_date":  record.get("sanctions", "").strip(),
        "expiration_date": "",
    }


def parse_nj_med_csv(fetch: FetchResult) -> ParseResult:
    """Parse targets.simple.csv into a list of mapped content tuples.

    Validates that:

    * The header matches :data:`OPENSANCTIONS_COLUMNS` exactly. A drift
      here fails loud (not a silent re-map) so an OpenSanctions schema
      change surfaces as a deliberate operator event.
    * Every data row has exactly the expected field count.
    * Each row has a non-empty ``name`` (the only field we treat as
      mandatory; a nameless row is a parser bug or upstream corruption).

    Returns a list of mapped tuples aligned to :data:`_RAW_COLUMNS` --
    not a DataFrame -- because we compute the per-row record_hash before
    COPY anyway, and a tuple-of-strings is the natural intermediate.
    """
    text = fetch.path.read_text(encoding="utf-8-sig", newline="")
    if not text.strip():
        raise IngestError(f"NJ Medicaid exclusion file {fetch.path} is empty after read.")

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise IngestError(
            f"NJ Medicaid exclusion file {fetch.path} has no header row.",
        ) from exc

    canonical_header = tuple(h.strip() for h in header)
    if canonical_header != OPENSANCTIONS_COLUMNS:
        raise IngestError(
            f"NJ Medicaid exclusion schema drift: header={canonical_header!r}; "
            f"expected={OPENSANCTIONS_COLUMNS!r}. "
            "Update OPENSANCTIONS_COLUMNS deliberately.",
        )

    cleaned: list[tuple[str, ...]] = []
    for line_no, row in enumerate(reader, start=2):
        if not row or all(not c.strip() for c in row):
            continue  # skip blank lines (rare but cheap to tolerate)
        if len(row) != len(OPENSANCTIONS_COLUMNS):
            raise IngestError(
                f"NJ Medicaid exclusion row {line_no}: got {len(row)} fields, "
                f"expected {len(OPENSANCTIONS_COLUMNS)}: {row!r}",
            )

        record = dict(zip(OPENSANCTIONS_COLUMNS, row, strict=True))
        mapped = _map_row(record)

        if not mapped["full_name"]:
            raise IngestError(
                f"NJ Medicaid exclusion row {line_no}: empty name "
                "(every exclusion must name a provider/entity).",
            )

        cleaned.append(tuple(mapped[c] for c in _RAW_COLUMNS))

    if not cleaned:
        raise IngestError(
            f"NJ Medicaid exclusion file {fetch.path} parsed 0 data rows; "
            "refusing to load an empty pull.",
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


def _validate_source_vintage(source_vintage: str) -> None:
    """Reject an empty source_vintage (the raw column is NOT NULL)."""
    if not source_vintage or not source_vintage.strip():
        raise IngestError(
            "source_vintage is empty; raw.nj_medicaid_exclusion.source_vintage "
            "is NOT NULL. Pass a non-empty value (ETag / Last-Modified / 'local').",
        )


def _row_to_csv_record(
    raw_values: tuple[str, ...],
    *,
    source_url: str,
    source_sha256: str,
    source_vintage: str,
) -> tuple[str, ...]:
    """Build the COPY column tuple for one row.

    Order matches the COPY column list in :func:`load_to_postgres`:
    ``record_hash, <9 content columns>, source_url, source_sha256,
    source_vintage``.

    ``ingested_at`` and ``last_seen_at`` are NOT supplied -- they default
    to ``now()`` on INSERT and we set ``last_seen_at`` explicitly on
    UPDATE in the upsert SQL, so they never ride the COPY stream.
    """
    row_dict = dict(zip(_RAW_COLUMNS, raw_values, strict=True))
    record_hash = compute_record_hash(row_dict)
    return (record_hash, *raw_values, source_url, source_sha256, source_vintage)


def _iter_csv_lines(
    parse: ParseResult,
    *,
    source_vintage: str,
) -> Iterator[bytes]:
    """Yield CSV-formatted rows ready for COPY ... FORMAT csv."""
    for raw_values in parse.rows:
        record = _row_to_csv_record(
            raw_values,
            source_url=parse.source_url,
            source_sha256=parse.source_sha256,
            source_vintage=source_vintage,
        )
        line = io.StringIO()
        csv.writer(line, lineterminator="\n").writerow(record)
        yield line.getvalue().encode("utf-8")


def load_to_postgres(
    parse: ParseResult,
    conn: psycopg.Connection,
) -> int:
    """UPSERT the parsed rows into raw.nj_medicaid_exclusion.

    Strategy (identical idiom to the LEIE loader):

    1. Stream the mapped rows into a TEMP staging table (same columns as
       raw.nj_medicaid_exclusion's content + provenance, no constraints,
       on commit drop) via COPY.
    2. ``INSERT INTO raw.nj_medicaid_exclusion ... SELECT ... FROM staging
        ON CONFLICT (record_hash) DO UPDATE SET last_seen_at = now()`` and
       refresh the provenance columns so the latest pull's metadata wins.

    Why two-phase rather than COPY directly into raw: Postgres' COPY does
    not support ON CONFLICT. COPY-then-UPSERT is the canonical idiom and
    is well under a second for a ~3.3K-row file.

    The source vintage stamped on every row comes from
    ``parse.source_vintage`` (the fetch's ETag / Last-Modified, or
    ``'local'`` for an operator-supplied file).

    Returns the total number of rows touched (inserted + updated).
    """
    _validate_source_vintage(parse.source_vintage)
    if parse.n_rows == 0:
        log.info("nj_med.load: nothing to upsert (parse.n_rows=0)")
        return 0

    staging_cols = (
        "record_hash",
        *_RAW_COLUMNS,
        "source_url", "source_sha256", "source_vintage",
    )
    staging_col_idents = sql.SQL(", ").join(
        sql.Identifier(c) for c in staging_cols
    )

    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "CREATE TEMP TABLE nj_med_staging ("
                "    record_hash CHAR(64), "
                "    full_name TEXT, npi TEXT, "
                "    address TEXT, city TEXT, state TEXT, zip TEXT, "
                "    action TEXT, effective_date TEXT, expiration_date TEXT, "
                "    source_url TEXT, source_sha256 CHAR(64), source_vintage TEXT"
                ") ON COMMIT DROP",
            ),
        )

        copy_query = sql.SQL(
            "COPY nj_med_staging ({cols}) FROM STDIN "
            "WITH (FORMAT csv, NULL '')",
        ).format(cols=staging_col_idents)

        with cur.copy(copy_query) as cp:
            buf = bytearray()
            for line_bytes in _iter_csv_lines(parse, source_vintage=parse.source_vintage):
                buf.extend(line_bytes)
                if len(buf) >= _COPY_CHUNK_SIZE:
                    cp.write(bytes(buf))
                    buf.clear()
            if buf:
                cp.write(bytes(buf))

        # Empty string -> NULL for every NULLable content column, so the
        # raw table stores real NULLs (per the "blank -> SQL NULL" rule)
        # rather than empty strings.
        for col in _RAW_COLUMNS:
            cur.execute(
                sql.SQL("UPDATE nj_med_staging SET {c} = NULLIF({c}, '')")
                .format(c=sql.Identifier(col)),
            )

        # UPSERT into raw. DISTINCT ON (record_hash) ORDER BY
        # record_hash, ctid collapses any intra-batch duplicate rows
        # (the same OSC entry can surface twice in a pull) to a single
        # surviving row; without it the UPSERT would raise
        # CardinalityViolation ("ON CONFLICT DO UPDATE command cannot
        # affect row a second time"). Because the duplicates share an
        # identical record_hash they are byte-identical content, so the
        # ctid tiebreak is information-preserving -- this mirrors the
        # LEIE loader exactly.
        cur.execute(
            """
            WITH deduped AS (
                SELECT DISTINCT ON (record_hash)
                    record_hash,
                    full_name, npi, address, city, state, zip,
                    action, effective_date, expiration_date,
                    source_url, source_sha256, source_vintage
                FROM nj_med_staging
                ORDER BY record_hash, ctid
            )
            INSERT INTO raw.nj_medicaid_exclusion (
                record_hash,
                full_name, npi, address, city, state, zip,
                action, effective_date, expiration_date,
                source_url, source_sha256, source_vintage
            )
            SELECT
                record_hash,
                full_name, npi, address, city, state, zip,
                action, effective_date, expiration_date,
                source_url, source_sha256, source_vintage
            FROM deduped
            ON CONFLICT (record_hash) DO UPDATE SET
                last_seen_at   = now(),
                source_url     = EXCLUDED.source_url,
                source_sha256  = EXCLUDED.source_sha256,
                source_vintage = EXCLUDED.source_vintage
            """,
        )
        n_touched = cur.rowcount

    log.info(
        "nj_med.load: UPSERTed %d rows into raw.nj_medicaid_exclusion "
        "(vintage=%s, sha256=%s)",
        n_touched, parse.source_vintage, parse.source_sha256[:16] + "...",
    )
    return n_touched


# ============================================================================
# Click CLI
# ============================================================================


@click.group()
def cli() -> None:
    """NJ Medicaid (OSC) exclusion-list ingester."""


@cli.command("fetch")
@click.option(
    "--dest-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("data/cache/nj_medicaid_exclusion"),
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
    default=NJ_MED_EXCLUSION_URL,
    show_default=True,
    help="Override the source URL (mostly useful for tests / PDF fallback).",
)
def cmd_fetch(dest_dir: Path, overwrite: bool, url: str) -> None:
    """Download targets.simple.csv with conditional-GET semantics."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    fetch = fetch_nj_med_exclusions(dest_dir=dest_dir, overwrite=overwrite, url=url)
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
    "--source-url",
    default=NJ_MED_EXCLUSION_URL,
    show_default=True,
    help="Source URL recorded on every row (point at the PDF for a fallback load).",
)
@click.option(
    "--source-vintage",
    default="local",
    show_default=True,
    help="Free-text vintage stamp recorded on every row (ETag / date / 'local').",
)
def cmd_load(
    csv_path: Path,
    dsn: str,
    source_url: str,
    source_vintage: str,
) -> None:
    """Parse and UPSERT a previously-fetched (or operator-supplied) CSV.

    Use this when the CSV is already on disk (from a prior fetch, or
    extracted from the OSC PDF for an air-gapped load). For end-to-end
    fetch + load in one step, use ``fetch-and-load``.
    """
    import psycopg as _psycopg  # late import keeps the help screen fast

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    fetch = FetchResult(
        path=csv_path,
        source_url=source_url,
        source_sha256=sha256_file(csv_path),
        source_vintage=source_vintage,
        n_bytes=csv_path.stat().st_size,
        cache_hit=True,
    )
    parse = parse_nj_med_csv(fetch)
    click.echo(f"parsed {parse.n_rows} rows")

    with _psycopg.connect(dsn) as conn:
        n = load_to_postgres(parse, conn)
        conn.commit()
    click.echo(f"upserted {n} rows (source_vintage={source_vintage})")


@cli.command("fetch-and-load")
@click.option(
    "--dest-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("data/cache/nj_medicaid_exclusion"),
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
    "--url",
    default=NJ_MED_EXCLUSION_URL,
    show_default=True,
)
def cmd_fetch_and_load(
    dest_dir: Path,
    overwrite: bool,
    dsn: str,
    url: str,
) -> None:
    """Fetch targets.simple.csv and UPSERT it in one step.

    The recommended Dagster-tick command. Conditional-GET means a
    no-change tick is cheap (HEAD probe + last_seen_at no-op).
    """
    import psycopg as _psycopg

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    fetch = fetch_nj_med_exclusions(dest_dir=dest_dir, overwrite=overwrite, url=url)
    parse = parse_nj_med_csv(fetch)
    click.echo(f"parsed {parse.n_rows} rows (cache_hit={fetch.cache_hit})")

    with _psycopg.connect(dsn) as conn:
        n = load_to_postgres(parse, conn)
        conn.commit()
    click.echo(
        f"upserted {n} rows (source_vintage={fetch.source_vintage}, "
        f"sha256={fetch.source_sha256})",
    )


if __name__ == "__main__":
    cli()
