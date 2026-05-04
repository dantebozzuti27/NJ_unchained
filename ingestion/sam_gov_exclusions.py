"""SAM.gov Exclusions Public Extract V2 loader.

LOAD-ONLY ingester for migration 063's substrate. The fetch path
(daily extract URL or API key route) is intentionally NOT shipped
because the SAM.gov auth situation is inconsistent across clients
and may change without notice. Operators hand-pull the daily ZIP
from sam.gov in a browser and run::

    nj-ingest-sam-exclusions load /path/to/extract.csv \
        --dsn=$PG_DSN \
        --vintage-day=2026-05-04

The same loader works for any future fetcher (extract URL or API)
that drops a CSV on disk; adding a fetcher is purely additive.

Design choices
--------------
1. **Header-name-based parsing, not column-index.** SAM has changed
   the column ORDER between V1 and V2 extracts and may again. We
   build a column-name -> index map from the header row and look
   up each field by canonicalized name. Required columns missing
   raises IngestError; optional columns missing maps to NULL.

2. **Defensive enum coercion.** The Classification CHECK in the raw
   schema only allows the four documented values
   ('Individual', 'Special Entity Designation', 'Firm', 'Vessel');
   the loader maps common variants ('Special Entity', 'special
   entity designation') to the canonical form before INSERT.

3. **UEI normalization to upper-case.** SAM publishes UEI in
   upper-case but a future export pipeline could lower-case. The
   raw schema's UEI CHECK requires upper-case so we coerce here.

4. **UPSERT semantics mirror LEIE.** Daily full-replace: stage in a
   TEMP table, INSERT ... ON CONFLICT (record_hash) DO UPDATE bumps
   last_seen_at. A row that disappears from the latest extract
   does NOT get DELETE'd; its last_seen_at falls behind
   MAX(last_seen_at) and the operator can detect reinstatements by
   that lag.

5. **record_hash inputs.** SHA-256 of pipe-joined canonicalized
   values of (classification, name, first, middle, last, suffix,
   uei, duns, sam_number, exclusion_type_desc, active_date). This
   set was chosen so two extracts of the SAME row produce the SAME
   hash, but a SAM-side data correction (different active_date,
   different sam_number, etc.) produces a NEW hash and the old row
   ages out via last_seen_at. Address fields are excluded from
   the hash because SAM normalizes them aggressively across
   releases (e.g., "STE 200" vs "Suite 200") and we don't want
   that churn to spam the hash space.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import click
from psycopg import sql

from ingestion._base import IngestError, sha256_file

if TYPE_CHECKING:
    from collections.abc import Iterator

    import psycopg

log = logging.getLogger(__name__)


# ============================================================================
# Constants
# ============================================================================

# Default source URL: the public-facing landing page for the V2
# extract. Operators hand-pull from here. Stamping the URL on every
# row preserves provenance even when the loader runs from disk.
DEFAULT_SOURCE_URL: Final[str] = (
    "https://sam.gov/data-services/Exclusions/Public%20V2"
)

# COPY chunk size: SAM exclusion rows average ~600 bytes; 4 MiB
# buffers ~6.5K rows.
_COPY_CHUNK_SIZE: Final[int] = 4 * 1024 * 1024

# Vintage-day regex / format. ISO YYYY-MM-DD is what the table
# stores (DATE column); the CLI accepts the same shape.
_VINTAGE_DAY_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$",
)

# UEI format. 12-char upper-case alphanumeric. SAM's own data
# dictionary states this; the raw-schema CHECK enforces it. We
# normalize input to upper-case and validate before INSERT.
_UEI_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Z0-9]{12}$")


# ----------------------------------------------------------------------------
# Header field map
# ----------------------------------------------------------------------------
# SAM publishes one header row in mixed case with spaces in column
# names. We canonicalize each header to a snake_case key (mirroring
# canonicalize_column from ingestion._base, but without an explicit
# unicode-normalize since SAM headers are pure ASCII) and look up
# values by canonical key.
#
# REQUIRED columns: the loader fails loud if any of these are
# missing from the extract header (probably a SAM schema change
# requiring an update here). OPTIONAL columns default to NULL.
# ----------------------------------------------------------------------------

_REQUIRED_FIELDS: Final[tuple[str, ...]] = ("classification",)

# (DB column, set of acceptable header canonicalizations) -- order is
# the INSERT column order downstream. We tolerate small wording
# variations ('State' vs 'State / Province'; 'Zip' vs 'Zip Code')
# because SAM has shipped both at different times.
_FIELD_MAP: Final[tuple[tuple[str, frozenset[str]], ...]] = (
    ("classification",        frozenset({"classification"})),
    ("name",                  frozenset({"name", "full_name"})),
    ("prefix",                frozenset({"prefix"})),
    ("first",                 frozenset({"first", "first_name"})),
    ("middle",                frozenset({"middle", "middle_name"})),
    ("last",                  frozenset({"last", "last_name"})),
    ("suffix",                frozenset({"suffix"})),
    ("title",                 frozenset({"title"})),
    ("uei",                   frozenset({"uei", "unique_entity_id",
                                          "unique_entity_identifier"})),
    ("duns",                  frozenset({"duns", "duns_number"})),
    ("cage",                  frozenset({"cage", "cage_code"})),
    ("npi",                   frozenset({"npi"})),
    ("address1",              frozenset({"address_1", "address1"})),
    ("address2",              frozenset({"address_2", "address2"})),
    ("address3",              frozenset({"address_3", "address3"})),
    ("address4",              frozenset({"address_4", "address4"})),
    ("city",                  frozenset({"city"})),
    ("state_province",        frozenset({"state_province",
                                          "state",
                                          "state_or_province"})),
    ("country",               frozenset({"country"})),
    ("zip",                   frozenset({"zip", "zip_code", "postal_code"})),
    ("exclusion_program",     frozenset({"exclusion_program",
                                          "exclusion_program_type"})),
    ("excluding_agency_name", frozenset({"excluding_agency",
                                          "excluding_agency_name",
                                          "agency"})),
    ("exclusion_type_desc",   frozenset({"exclusion_type",
                                          "exclusion_type_description",
                                          "exclusion_type_desc"})),
    ("active_date",           frozenset({"active_date",
                                          "exclusion_active_date"})),
    ("termination_date",      frozenset({"termination_date",
                                          "exclusion_termination_date"})),
    ("record_status",         frozenset({"record_status",
                                          "exclusion_status"})),
    ("cross_reference",       frozenset({"cross_reference"})),
    ("sam_number",            frozenset({"sam_number"})),
    ("additional_comments",   frozenset({"additional_comments",
                                          "comments"})),
    ("open_data_flag",        frozenset({"open_data_flag", "open_data"})),
    ("creation_date",         frozenset({"creation_date"})),
)

_DB_COLUMNS: Final[tuple[str, ...]] = tuple(c for c, _ in _FIELD_MAP)

# Classification value canonicalization. SAM has shipped variants
# like 'special entity' and 'individual exclusion'; we map them to
# the four-value enum the raw schema enforces.
_CLASSIFICATION_MAP: Final[dict[str, str]] = {
    "individual":                  "Individual",
    "individual exclusion":        "Individual",
    "special entity":              "Special Entity Designation",
    "special entity designation":  "Special Entity Designation",
    "firm":                        "Firm",
    "firm exclusion":              "Firm",
    "vessel":                      "Vessel",
}

# Columns whose values are part of the record_hash input, in hash
# order. Stable across same-row re-pulls; changes when SAM edits
# any of these fields, which is the desired behavior.
_HASH_COLUMNS: Final[tuple[str, ...]] = (
    "classification", "name",
    "first", "middle", "last", "suffix",
    "uei", "duns", "sam_number",
    "exclusion_type_desc", "active_date",
)


# ============================================================================
# Header parsing
# ============================================================================


def _canonicalize_header(s: str) -> str:
    """Return a SAM header normalized to snake_case ASCII.

    SAM headers are pure ASCII with spaces, slashes, and case
    variations. The canonicalization collapses all non-alphanumeric
    runs to single underscores and lowercases.
    """
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


def _build_header_index(
    header_row: list[str],
) -> dict[str, int]:
    """Map (db_column_name) -> (header row index) for one extract.

    Walks _FIELD_MAP and finds the first header column whose canonical
    name matches one of the known aliases. Returns a dict of only
    those db columns whose source column was found. Unmapped headers
    are silently ignored (SAM ships extra columns we don't care
    about, e.g. 'DODAAC').

    Raises IngestError if any of _REQUIRED_FIELDS lacks a mapping.
    """
    canonical = {_canonicalize_header(h): idx for idx, h in enumerate(header_row)}
    mapping: dict[str, int] = {}
    for db_col, aliases in _FIELD_MAP:
        for alias in aliases:
            if alias in canonical:
                mapping[db_col] = canonical[alias]
                break
    missing_required = [
        c for c in _REQUIRED_FIELDS if c not in mapping
    ]
    if missing_required:
        raise IngestError(
            f"SAM Exclusions extract header missing required column(s) "
            f"{missing_required!r}. Saw headers: {header_row!r}",
        )
    return mapping


# ============================================================================
# Parse
# ============================================================================


@dataclass(frozen=True)
class ParseResult:
    """One parsed SAM Exclusions CSV ready for COPY."""

    rows:           list[dict[str, str | None]]
    source_url:     str
    source_sha256:  str
    n_rows:         int


def _normalize_for_hash(value: str | None) -> str:
    """Upper-case, strip whitespace, strip NULs. NULL/empty -> ''.

    Same shape as the LEIE normalizer; the hash is stable across
    extracts that differ only in cosmetic formatting.
    """
    if not value:
        return ""
    cleaned = value.replace("\x00", "").strip().upper()
    return cleaned


def _compute_record_hash(row: dict[str, str | None]) -> str:
    """SHA-256 of pipe-joined canonicalized hash columns."""
    parts = [_normalize_for_hash(row.get(col)) for col in _HASH_COLUMNS]
    payload = "|".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _coerce_classification(raw: str) -> str:
    """Map raw value to the four-value enum. Raises on unknown."""
    key = raw.strip().lower()
    if key in _CLASSIFICATION_MAP:
        return _CLASSIFICATION_MAP[key]
    raise IngestError(
        f"Unknown SAM classification value {raw!r}. "
        f"Known: {sorted(_CLASSIFICATION_MAP.keys())!r}",
    )


def _coerce_uei(raw: str | None) -> str | None:
    """Upper-case UEI; return None for empty; raise on malformed."""
    if not raw:
        return None
    candidate = raw.strip().upper()
    if not candidate:
        return None
    if not _UEI_RE.match(candidate):
        raise IngestError(
            f"Malformed UEI {raw!r}: expected 12-char [A-Z0-9].",
        )
    return candidate


def _coerce_date(raw: str | None, *, field: str) -> str | None:
    """Parse a SAM date string, return ISO YYYY-MM-DD or None.

    SAM commonly publishes dates as 'MM/DD/YYYY' or 'YYYY-MM-DD'.
    Empty/whitespace returns None. Anything else raises IngestError
    so a schema drift is loud, not silent.
    """
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None
    # ISO first (preferred).
    try:
        return dt.date.fromisoformat(s).isoformat()
    except ValueError:
        pass
    # MM/DD/YYYY.
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    raise IngestError(
        f"SAM {field}={raw!r}: unrecognized date format. "
        "Expected YYYY-MM-DD or MM/DD/YYYY.",
    )


def _validate_row_content(row: dict[str, str | None], *, line_no: int) -> None:
    """Mirror the row-content CHECK in raw.sam_gov_exclusion.

    At least one of (name, last, uei, duns, sam_number) must be
    non-empty. Catching this in Python before COPY produces a
    clearer error message than the bare CHECK violation.
    """
    if any(
        row.get(col) and row[col] != ""
        for col in ("name", "last", "uei", "duns", "sam_number")
    ):
        return
    raise IngestError(
        f"SAM row {line_no}: all of (name, last, uei, duns, sam_number) "
        "are empty. Refusing to load (raw-schema CHECK would reject).",
    )


def parse_sam_csv(
    csv_path: Path,
    *,
    source_url: str,
) -> ParseResult:
    """Parse a SAM Exclusions Public Extract V2 CSV.

    Returns a ParseResult with one dict per data row, keyed by DB
    column name. Empty strings are converted to None. Dates are
    coerced to ISO format. UEI is upper-cased. Classification is
    normalized to the four-value enum.

    Raises IngestError on:
        * Missing required header column.
        * Unknown classification.
        * Malformed UEI.
        * Unrecognized date format.
        * All-blank row content.
    """
    text = csv_path.read_text(encoding="utf-8-sig", newline="")
    if not text.strip():
        raise IngestError(f"SAM file {csv_path} is empty after read.")

    reader = csv.reader(io.StringIO(text))
    try:
        header_row = next(reader)
    except StopIteration as exc:
        raise IngestError(
            f"SAM file {csv_path} has no header row.",
        ) from exc

    header_index = _build_header_index(header_row)

    cleaned: list[dict[str, str | None]] = []
    for line_no, fields in enumerate(reader, start=2):
        if not fields or all(not c.strip() for c in fields):
            continue

        # Build the row dict by header index.
        row: dict[str, str | None] = {}
        for db_col in _DB_COLUMNS:
            idx = header_index.get(db_col)
            if idx is None or idx >= len(fields):
                row[db_col] = None
                continue
            value = fields[idx].strip()
            row[db_col] = value if value else None

        # Required field coercions.
        cls_raw = row.get("classification")
        if not cls_raw:
            raise IngestError(
                f"SAM row {line_no}: empty classification "
                "(required by raw-schema CHECK).",
            )
        row["classification"] = _coerce_classification(cls_raw)

        # UEI / dates.
        row["uei"] = _coerce_uei(row.get("uei"))
        row["active_date"] = _coerce_date(
            row.get("active_date"), field="active_date",
        )
        row["termination_date"] = _coerce_date(
            row.get("termination_date"), field="termination_date",
        )
        row["creation_date"] = _coerce_date(
            row.get("creation_date"), field="creation_date",
        )

        _validate_row_content(row, line_no=line_no)

        cleaned.append(row)

    if not cleaned:
        raise IngestError(
            f"SAM file {csv_path} parsed 0 data rows; "
            "expected ~80K active exclusions. Refusing to load.",
        )

    source_sha256 = sha256_file(csv_path)
    return ParseResult(
        rows=cleaned,
        source_url=source_url,
        source_sha256=source_sha256,
        n_rows=len(cleaned),
    )


# ============================================================================
# Load
# ============================================================================


def _validate_vintage_day(vintage_day: str) -> None:
    if not _VINTAGE_DAY_RE.match(vintage_day):
        raise IngestError(
            f"vintage_day={vintage_day!r} must match YYYY-MM-DD "
            "(e.g. '2026-05-04').",
        )


def _row_to_csv_record(
    row: dict[str, str | None],
    *,
    vintage_day: str,
    source_url: str,
    source_sha256: str,
) -> tuple[str | None, ...]:
    """Build the COPY tuple for one row.

    Order: record_hash, <_DB_COLUMNS>, vintage_day, source_url, source_sha256.
    """
    record_hash = _compute_record_hash(row)
    return (
        record_hash,
        *(row.get(c) for c in _DB_COLUMNS),
        vintage_day,
        source_url,
        source_sha256,
    )


def _iter_csv_lines(
    parse: ParseResult,
    *,
    vintage_day: str,
) -> Iterator[bytes]:
    """Yield CSV-formatted rows ready for COPY ... FORMAT csv."""
    for row in parse.rows:
        record = _row_to_csv_record(
            row,
            vintage_day=vintage_day,
            source_url=parse.source_url,
            source_sha256=parse.source_sha256,
        )
        line = io.StringIO()
        csv.writer(line, lineterminator="\n").writerow(
            ["" if v is None else v for v in record]
        )
        yield line.getvalue().encode("utf-8")


def load_to_postgres(
    parse: ParseResult,
    conn: psycopg.Connection,
    *,
    vintage_day: str,
) -> int:
    """UPSERT parsed rows into raw.sam_gov_exclusion.

    Two-phase: COPY into a TEMP staging table, then INSERT ... ON
    CONFLICT (record_hash) DO UPDATE bumping last_seen_at and
    refreshing provenance fields so the latest pull's metadata wins.

    Returns total rows touched (inserted + updated).
    """
    _validate_vintage_day(vintage_day)
    if parse.n_rows == 0:
        log.info("sam.load: nothing to upsert (parse.n_rows=0)")
        return 0

    staging_cols = (
        "record_hash",
        *_DB_COLUMNS,
        "vintage_day", "source_url", "source_sha256",
    )
    staging_col_idents = sql.SQL(", ").join(
        sql.Identifier(c) for c in staging_cols
    )

    with conn.cursor() as cur:
        cur.execute(
            "CREATE TEMP TABLE sam_staging ("
            "    record_hash CHAR(64), "
            "    classification TEXT, "
            "    name TEXT, prefix TEXT, first TEXT, middle TEXT, "
            "    last TEXT, suffix TEXT, title TEXT, "
            "    uei TEXT, duns TEXT, cage TEXT, npi TEXT, "
            "    address1 TEXT, address2 TEXT, address3 TEXT, address4 TEXT, "
            "    city TEXT, state_province TEXT, country TEXT, zip TEXT, "
            "    exclusion_program TEXT, excluding_agency_name TEXT, "
            "    exclusion_type_desc TEXT, "
            "    active_date DATE, termination_date DATE, "
            "    record_status TEXT, cross_reference TEXT, "
            "    sam_number TEXT, additional_comments TEXT, "
            "    open_data_flag TEXT, creation_date DATE, "
            "    vintage_day DATE, "
            "    source_url TEXT, source_sha256 CHAR(64)"
            ") ON COMMIT DROP",
        )

        copy_query = sql.SQL(
            "COPY sam_staging ({cols}) FROM STDIN "
            "WITH (FORMAT csv, NULL '')",
        ).format(cols=staging_col_idents)

        with cur.copy(copy_query) as cp:
            buf = bytearray()
            for line_bytes in _iter_csv_lines(parse, vintage_day=vintage_day):
                buf.extend(line_bytes)
                if len(buf) >= _COPY_CHUNK_SIZE:
                    cp.write(bytes(buf))
                    buf.clear()
            if buf:
                cp.write(bytes(buf))

        cur.execute(
            """
            INSERT INTO raw.sam_gov_exclusion (
                record_hash, classification,
                name, prefix, first, middle, last, suffix, title,
                uei, duns, cage, npi,
                address1, address2, address3, address4,
                city, state_province, country, zip,
                exclusion_program, excluding_agency_name,
                exclusion_type_desc,
                active_date, termination_date, record_status,
                cross_reference, sam_number, additional_comments,
                open_data_flag, creation_date,
                vintage_day, source_url, source_sha256
            )
            SELECT
                record_hash, classification,
                name, prefix, first, middle, last, suffix, title,
                uei, duns, cage, npi,
                address1, address2, address3, address4,
                city, state_province, country, zip,
                exclusion_program, excluding_agency_name,
                exclusion_type_desc,
                active_date, termination_date, record_status,
                cross_reference, sam_number, additional_comments,
                open_data_flag, creation_date,
                vintage_day, source_url, source_sha256
            FROM sam_staging
            ON CONFLICT (record_hash) DO UPDATE SET
                last_seen_at  = now(),
                vintage_day   = EXCLUDED.vintage_day,
                source_url    = EXCLUDED.source_url,
                source_sha256 = EXCLUDED.source_sha256
            """,
        )
        n_touched = cur.rowcount

    log.info(
        "sam.load: UPSERTed %d rows into raw.sam_gov_exclusion "
        "(vintage_day=%s, sha256=%s)",
        n_touched, vintage_day, parse.source_sha256[:16] + "...",
    )
    return n_touched


# ============================================================================
# Click CLI
# ============================================================================


def _default_vintage_day() -> str:
    """Today's YYYY-MM-DD in UTC."""
    return dt.datetime.now(dt.UTC).date().isoformat()


@click.group()
def cli() -> None:
    """SAM.gov Exclusions ingester (load-only).

    The fetch path is deferred until the SAM auth landscape is
    verified; meanwhile, hand-pull the daily ZIP from
    https://sam.gov/data-services/Exclusions/Public%20V2/ and run
    `nj-ingest-sam-exclusions load <csv_path>`.
    """


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
    "--vintage-day",
    default=None,
    help=(
        "ISO YYYY-MM-DD stamp recorded on every row. "
        "Defaults to today's UTC date."
    ),
)
@click.option(
    "--source-url",
    default=DEFAULT_SOURCE_URL,
    show_default=True,
    help="Source URL recorded on every row.",
)
def cmd_load(
    csv_path: Path,
    dsn: str,
    vintage_day: str | None,
    source_url: str,
) -> None:
    """Parse and UPSERT a hand-pulled SAM Exclusions Public Extract V2 CSV."""
    import psycopg as _psycopg

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    eff_vintage = vintage_day or _default_vintage_day()
    _validate_vintage_day(eff_vintage)

    parse = parse_sam_csv(csv_path, source_url=source_url)
    click.echo(f"parsed {parse.n_rows} rows")

    with _psycopg.connect(dsn) as conn:
        n = load_to_postgres(parse, conn, vintage_day=eff_vintage)
        conn.commit()
    click.echo(f"upserted {n} rows (vintage_day={eff_vintage})")


if __name__ == "__main__":
    cli()
