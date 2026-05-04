r"""FEC bulk-data ingester (Tier 4 v1).

Three bulk files per cycle from the Federal Election Commission:

    cn{yy}.zip     -> Candidate Master      -> raw.fec_candidate
    cm{yy}.zip     -> Committee Master      -> raw.fec_committee
    indiv{yy}.zip  -> Individual Contribs   -> raw.fec_contribution

All three are pipe-delimited, no header, no CSV escapes, no quoting.
The schema for each file is defined by a separate ``*_header_file.csv``
under ``data_dictionaries/`` (a single header line with comma-separated
column names). We pin the column lists in this module so a schema
drift on FEC's side surfaces as a typed mismatch in our regression
tests, not a silent column re-mapping.

Architecture
------------
* ``fetch_*``  -> httpx GET with conditional headers (If-Modified-Since,
                 If-None-Match) so re-runs on an unchanged remote
                 file return the cached path without downloading.
* ``parse_*``  -> per-row sanity, dtype hints, vintage tagging. For
                 the small cn/cm files we eagerly parse with Polars.
                 For the large indiv file we DO NOT parse in Python at
                 all -- we stream the unzipped bytes directly into
                 Postgres COPY (the file IS the wire format).
* ``load_*``   -> psycopg COPY from STDIN, FORMAT csv, DELIMITER '|',
                 QUOTE = bell-character (so the parser never tries to
                 interpret double-quotes -- FEC's bulk files do not
                 quote fields, but they DO contain literal double-
                 quote characters inside text fields, which would
                 otherwise be mis-parsed).

Why FORMAT csv instead of FORMAT text
-------------------------------------
Postgres FORMAT text uses backslash escapes (``\\N`` for NULL,
``\\t`` for tab, etc.) which FEC's bulk files do not produce. FORMAT
csv with DELIMITER '|' and an unused QUOTE character matches FEC's
actual byte format byte-for-byte: empty field -> NULL, no escape
processing, embedded newlines impossible because the source uses
LF line endings.

Why we stream indiv directly to COPY
------------------------------------
indiv24.zip is 4.2 GB compressed; itcont.txt expanded is ~15 GB.
Loading either into a Polars DataFrame requires holding the full
table in RAM (or sinking to a temp CSV, doubling disk I/O). The
file's natural format -- pipe-delimited, no quoting, one row per
line -- is COPY-compatible. Streaming it through ``zipfile.open(...)
-> COPY ... FROM STDIN`` runs in constant memory and at the speed
of psycopg's COPY pipeline (~50K rows/sec on typical hardware).
"""

from __future__ import annotations

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
import polars as pl
from psycopg import sql

from ingestion._base import IngestError, sha256_file

if TYPE_CHECKING:
    from collections.abc import Iterable

    import psycopg

log = logging.getLogger(__name__)


# ============================================================================
# Constants
# ============================================================================

FEC_BULK_BASE_URL: Final[str] = "https://www.fec.gov/files/bulk-downloads"

# Bulk-file kinds we ingest. Each value is a tuple of:
#   (zip filename pattern, inner-file name, target raw table)
# The zip filename uses the two-digit year suffix; the inner txt file
# is named per FEC's longstanding convention (cn.txt / cm.txt /
# itcont.txt regardless of cycle).
FILE_KINDS: Final[dict[str, tuple[str, str, str]]] = {
    "cn":     ("cn{yy}.zip",     "cn.txt",     "raw.fec_candidate"),
    "cm":     ("cm{yy}.zip",     "cm.txt",     "raw.fec_committee"),
    "indiv":  ("indiv{yy}.zip",  "itcont.txt", "raw.fec_contribution"),
}

# Column lists pinned to the FEC header files as of 2026-04-29.
# Verified live: GET https://www.fec.gov/files/bulk-downloads/data_dictionaries/{kind}_header_file.csv
# A schema drift here will trip the parse-time row-width assertion below.
CN_COLUMNS: Final[tuple[str, ...]] = (
    "CAND_ID", "CAND_NAME", "CAND_PTY_AFFILIATION", "CAND_ELECTION_YR",
    "CAND_OFFICE_ST", "CAND_OFFICE", "CAND_OFFICE_DISTRICT",
    "CAND_ICI", "CAND_STATUS", "CAND_PCC",
    "CAND_ST1", "CAND_ST2", "CAND_CITY", "CAND_ST", "CAND_ZIP",
)
CM_COLUMNS: Final[tuple[str, ...]] = (
    "CMTE_ID", "CMTE_NM", "TRES_NM",
    "CMTE_ST1", "CMTE_ST2", "CMTE_CITY", "CMTE_ST", "CMTE_ZIP",
    "CMTE_DSGN", "CMTE_TP", "CMTE_PTY_AFFILIATION", "CMTE_FILING_FREQ",
    "ORG_TP", "CONNECTED_ORG_NM", "CAND_ID",
)
INDIV_COLUMNS: Final[tuple[str, ...]] = (
    "CMTE_ID", "AMNDT_IND", "RPT_TP", "TRANSACTION_PGI", "IMAGE_NUM",
    "TRANSACTION_TP", "ENTITY_TP", "NAME", "CITY", "STATE", "ZIP_CODE",
    "EMPLOYER", "OCCUPATION", "TRANSACTION_DT", "TRANSACTION_AMT",
    "OTHER_ID", "TRAN_ID", "FILE_NUM", "MEMO_CD", "MEMO_TEXT", "SUB_ID",
)

# Map our raw-table columns back to the FEC header order so COPY can
# insert in the source's column order without per-row reshaping.
# raw schema columns are lowercase variants of the source columns
# above, prepended with `cycle` (which we add at COPY time, not from
# the source bytes).
_CN_RAW_COLUMNS: Final[tuple[str, ...]] = tuple(c.lower() for c in CN_COLUMNS)
_CM_RAW_COLUMNS: Final[tuple[str, ...]] = tuple(c.lower() for c in CM_COLUMNS)
_INDIV_RAW_COLUMNS: Final[tuple[str, ...]] = tuple(c.lower() for c in INDIV_COLUMNS)

# QUOTE character for COPY: ASCII bell (0x07) is never present in FEC
# bulk text. Setting QUOTE to a guaranteed-absent byte effectively
# disables CSV quote processing while keeping FORMAT csv's empty-
# field-as-NULL semantics.
_COPY_QUOTE_CHAR: Final[str] = "\x07"

# Streaming chunk size for COPY ... FROM STDIN. 8 MiB balances
# psycopg buffer churn against zlib decompression latency.
_COPY_CHUNK_SIZE: Final[int] = 8 * 1024 * 1024

# httpx timeout for the metadata HEAD probe (small) and the streamed
# GET (large -- give the indiv24.zip download up to 30 minutes).
_HTTP_HEAD_TIMEOUT_S: Final[float] = 30.0
_HTTP_GET_TIMEOUT_S:  Final[float] = 1800.0


# ============================================================================
# URL builders
# ============================================================================


def bulk_url(cycle: str, file_kind: str) -> str:
    """Return the FEC bulk URL for *file_kind* of *cycle*.

    Args:
        cycle: 4-digit cycle year, e.g. "2024".
        file_kind: One of FILE_KINDS keys.

    Raises:
        ValueError: on a bad cycle / file_kind.

    """
    if not re.match(r"^[0-9]{4}$", cycle):
        raise ValueError(f"Bad cycle {cycle!r}; expected 4-digit year.")
    if file_kind not in FILE_KINDS:
        raise ValueError(
            f"Bad file_kind {file_kind!r}; expected one of {sorted(FILE_KINDS)}."
        )
    fname_template, _, _ = FILE_KINDS[file_kind]
    yy = cycle[-2:]
    return f"{FEC_BULK_BASE_URL}/{cycle}/{fname_template.format(yy=yy)}"


def header_file_url(file_kind: str) -> str:
    """Return the URL of the comma-separated header-file for *file_kind*.

    The header file is a single line of comma-separated column names
    that we use only at test time to pin the column ordering against
    FEC's published canonical schema.
    """
    if file_kind not in FILE_KINDS:
        raise ValueError(
            f"Bad file_kind {file_kind!r}; expected one of {sorted(FILE_KINDS)}."
        )
    return f"{FEC_BULK_BASE_URL}/data_dictionaries/{file_kind}_header_file.csv"


# ============================================================================
# Fetch
# ============================================================================


@dataclass(frozen=True)
class FetchResult:
    """Output of :func:`fetch_fec_bulk`."""

    path:           Path
    source_url:     str
    source_sha256:  str
    source_vintage: str  # ETag or Last-Modified, whichever is present
    n_bytes:        int
    cache_hit:      bool


def fetch_fec_bulk(
    cycle: str,
    file_kind: str,
    *,
    dest_dir: Path,
    overwrite: bool = False,
) -> FetchResult:
    """Download a FEC bulk file with conditional-GET semantics.

    The destination filename is the same as the source (e.g. cn24.zip);
    if the file already exists locally we issue a HEAD against FEC and
    only re-download when the remote ETag/Last-Modified differs.

    Returns a FetchResult with provenance fields. Streams to disk in
    1 MiB chunks via a ``.part`` sidecar that is renamed atomically
    on success -- so an interrupted download never leaves a corrupt
    file in the cache.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    url = bulk_url(cycle, file_kind)
    fname_template, _, _ = FILE_KINDS[file_kind]
    yy = cycle[-2:]
    local = dest_dir / fname_template.format(yy=yy)

    # Probe upstream metadata.
    with httpx.Client(timeout=_HTTP_HEAD_TIMEOUT_S, follow_redirects=True) as client:
        head_resp = client.head(url)
        head_resp.raise_for_status()
        remote_etag    = head_resp.headers.get("etag", "").strip('"')
        remote_lastmod = head_resp.headers.get("last-modified", "")
        remote_size    = int(head_resp.headers.get("content-length", "0") or 0)

    vintage = remote_etag or remote_lastmod or dt.date.today().isoformat()

    # Decide whether to skip.
    if local.exists() and not overwrite:
        local_size = local.stat().st_size
        if remote_size and local_size == remote_size:
            log.info(
                "fec.fetch: cache hit for %s (size=%d bytes)", local.name, local_size,
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
            "fec.fetch: cache stale for %s (local=%d, remote=%d) -- re-downloading",
            local.name, local_size, remote_size,
        )

    log.info("fec.fetch: downloading %s -> %s", url, local)
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
        shutil.move(tmp, local)
    log.info("fec.fetch: wrote %s (%.1f MiB)", local, n / (1 << 20))
    return FetchResult(
        path=local,
        source_url=url,
        source_sha256=sha256_file(local),
        source_vintage=vintage,
        n_bytes=n,
        cache_hit=False,
    )


# ============================================================================
# Parse (small files: cn, cm)
# ============================================================================


@dataclass(frozen=True)
class ParseResult:
    """Output of :func:`parse_fec_small_table` for cn / cm."""

    dataframe:      pl.DataFrame
    file_kind:      str
    cycle:          str
    source_url:     str
    source_sha256:  str
    source_vintage: str
    n_rows:         int


def _read_zip_inner_bytes(zip_path: Path, inner_name: str) -> bytes:
    """Return the bytes of *inner_name* from *zip_path*."""
    with zipfile.ZipFile(zip_path) as zf:
        try:
            with zf.open(inner_name) as f:
                return f.read()
        except KeyError as exc:
            raise IngestError(
                f"FEC zip {zip_path.name} missing inner file {inner_name!r}; "
                f"contents: {zf.namelist()}"
            ) from exc


def parse_fec_small_table(
    fetch: FetchResult,
    *,
    cycle: str,
    file_kind: str,
) -> ParseResult:
    """Parse a small FEC bulk file (cn or cm) into a typed DataFrame.

    Not used for indiv -- that file is too large to materialize in
    memory and is streamed directly into COPY by load_fec_indiv.

    All columns are read as Utf8 to faithfully preserve the source
    bytes; any narrowing happens in the derived layer.
    """
    if file_kind not in {"cn", "cm"}:
        raise ValueError(
            "parse_fec_small_table is for cn/cm only; use load_fec_indiv for indiv."
        )
    expected_cols = CN_COLUMNS if file_kind == "cn" else CM_COLUMNS
    inner_name = FILE_KINDS[file_kind][1]
    raw_bytes = _read_zip_inner_bytes(fetch.path, inner_name)

    # FEC bulk files do NOT quote fields. Real data contains literal
    # double-quote characters in plain text (e.g. nicknames like
    # 'JOHN "JACK" DOE'); leaving Polars's default quote_char='"'
    # interprets those as broken CSV escapes and aborts parsing.
    # Setting quote_char=None disables quote interpretation entirely,
    # matching FEC's actual byte format.
    df = pl.read_csv(
        io.BytesIO(raw_bytes),
        separator="|",
        has_header=False,
        new_columns=list(expected_cols),
        schema_overrides=dict.fromkeys(expected_cols, pl.Utf8),
        quote_char=None,
        truncate_ragged_lines=False,
        ignore_errors=False,
    )
    if df.width != len(expected_cols):
        raise IngestError(
            f"FEC {file_kind} {cycle}: expected {len(expected_cols)} columns "
            f"({list(expected_cols)}); got {df.width} ({df.columns})."
        )
    if df.height == 0:
        raise IngestError(f"FEC {file_kind} {cycle}: parsed 0 rows from {fetch.path}")

    return ParseResult(
        dataframe=df,
        file_kind=file_kind,
        cycle=cycle,
        source_url=fetch.source_url,
        source_sha256=fetch.source_sha256,
        source_vintage=fetch.source_vintage,
        n_rows=df.height,
    )


def stage_small_dataframe(parse: ParseResult) -> pl.DataFrame:
    """Add provenance + cycle columns; lower-case the column names.

    Returns a DataFrame whose column names match the raw.fec_candidate
    or raw.fec_committee schema, in the same order, ready for COPY.
    """
    expected = CN_COLUMNS if parse.file_kind == "cn" else CM_COLUMNS
    raw_cols = (
        _CN_RAW_COLUMNS if parse.file_kind == "cn" else _CM_RAW_COLUMNS
    )
    rename_map = dict(zip(expected, raw_cols, strict=True))

    return (
        parse.dataframe.rename(rename_map)
        .with_columns(
            pl.lit(parse.cycle).alias("cycle"),
            pl.lit(parse.source_url).alias("source_url"),
            pl.lit(parse.source_sha256).alias("source_sha256"),
            pl.lit(parse.source_vintage).alias("source_vintage"),
        )
        # Final column order = (cycle, raw_cols..., provenance...).
        .select(
            ["cycle", *raw_cols, "source_url", "source_sha256", "source_vintage"],
        )
    )


# ============================================================================
# Load
# ============================================================================


def _qualified_table(table: str) -> sql.Identifier:
    """Build a sql.Identifier from a 'schema.table' or bare 'table' string."""
    if "." in table:
        schema, name = table.split(".", 1)
        return sql.Identifier(schema, name)
    return sql.Identifier(table)


def _delete_cycle(
    conn: psycopg.Connection, *, table: str, cycle: str,
) -> int:
    """DELETE all rows in *table* for the given cycle. Returns rows deleted.

    For raw.fec_candidate / raw.fec_committee the PK includes cycle,
    so DELETE+COPY is the correct idempotent re-load semantics.

    For raw.fec_contribution sub_id is globally unique per FEC docs;
    if the loader is ever re-run for the same cycle it would raise
    on PK conflict during COPY. DELETE WHERE cycle=% guarantees clean
    re-load semantics matching the cn/cm path.
    """
    qual = _qualified_table(table)
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("DELETE FROM {tbl} WHERE cycle = %s").format(tbl=qual),
            (cycle,),
        )
        return cur.rowcount


def _copy_dataframe_csv(
    df: pl.DataFrame,
    conn: psycopg.Connection,
    *,
    table: str,
) -> int:
    """COPY *df* into *table* via FORMAT csv with default '"' quoting.

    Polars' ``write_csv`` emits standard CSV (comma-delimited, double-
    quoted fields containing commas/quotes), so the COPY statement
    must use the default QUOTE='"' to round-trip correctly. The
    bell-character QUOTE escape used by load_fec_indiv is only valid
    for the raw FEC byte stream where no quoting exists.
    """
    qual = _qualified_table(table)
    col_idents = sql.SQL(", ").join(sql.Identifier(c) for c in df.columns)
    copy_query = sql.SQL(
        "COPY {tbl} ({cols}) FROM STDIN WITH (FORMAT csv, NULL '')",
    ).format(tbl=qual, cols=col_idents)

    buf = io.BytesIO()
    df.write_csv(buf, include_header=False)
    buf.seek(0)
    with conn.cursor().copy(copy_query) as cp:
        while chunk := buf.read(_COPY_CHUNK_SIZE):
            cp.write(chunk)
    return df.height


def load_fec_small_table(
    parse: ParseResult,
    conn: psycopg.Connection,
) -> int:
    """DELETE + COPY a parsed cn/cm into raw.fec_candidate / raw.fec_committee.

    Wrapped in the caller's transaction so a partial-cycle reload is
    atomic. Returns the number of rows COPYed.
    """
    target_table = FILE_KINDS[parse.file_kind][2]
    staged = stage_small_dataframe(parse)
    _delete_cycle(conn, table=target_table, cycle=parse.cycle)
    n = _copy_dataframe_csv(staged, conn, table=target_table)
    log.info(
        "fec.load: COPYed %d rows into %s (cycle=%s)",
        n, target_table, parse.cycle,
    )
    return n


def load_fec_indiv(
    fetch: FetchResult,
    conn: psycopg.Connection,
    *,
    cycle: str,
) -> int:
    """Stream itcont.txt from the zip directly into raw.fec_contribution.

    The file is too large (15-20 GB uncompressed) to load via Polars.
    Instead we open the zip, read the inner txt as a stream, prepend
    each line with the cycle column, and pipe the result through
    psycopg's COPY ... FROM STDIN.

    Idempotency: DELETE WHERE cycle=%s before COPY so a re-run for
    the same cycle is a clean replace.

    Returns the number of rows successfully COPYed (= number of lines
    in itcont.txt; FEC files have no header so every line is a row).
    """
    target_table = "raw.fec_contribution"
    inner_name = FILE_KINDS["indiv"][1]

    _delete_cycle(conn, table=target_table, cycle=cycle)

    # The COPY column list:
    #   cycle, sub_id, cmte_id, amndt_ind, rpt_tp, transaction_pgi,
    #   image_num, transaction_tp, entity_tp, name, city, state,
    #   zip_code, employer, occupation, transaction_dt, transaction_amt,
    #   other_id, tran_id, file_num, memo_cd, memo_text,
    #   source_url, source_sha256, source_vintage
    #
    # FEC's per-row column order is:
    #   CMTE_ID|AMNDT_IND|RPT_TP|TRANSACTION_PGI|IMAGE_NUM|
    #   TRANSACTION_TP|ENTITY_TP|NAME|CITY|STATE|ZIP_CODE|EMPLOYER|
    #   OCCUPATION|TRANSACTION_DT|TRANSACTION_AMT|OTHER_ID|TRAN_ID|
    #   FILE_NUM|MEMO_CD|MEMO_TEXT|SUB_ID
    #
    # We prepend the cycle column and append three provenance columns
    # via byte-level prefix/suffix injection per line, so we never
    # have to materialize the file in Polars.
    col_idents = sql.SQL(", ").join([
        sql.Identifier("cycle"),
        sql.Identifier("cmte_id"), sql.Identifier("amndt_ind"),
        sql.Identifier("rpt_tp"),  sql.Identifier("transaction_pgi"),
        sql.Identifier("image_num"), sql.Identifier("transaction_tp"),
        sql.Identifier("entity_tp"), sql.Identifier("name"),
        sql.Identifier("city"), sql.Identifier("state"),
        sql.Identifier("zip_code"), sql.Identifier("employer"),
        sql.Identifier("occupation"), sql.Identifier("transaction_dt"),
        sql.Identifier("transaction_amt"), sql.Identifier("other_id"),
        sql.Identifier("tran_id"), sql.Identifier("file_num"),
        sql.Identifier("memo_cd"), sql.Identifier("memo_text"),
        sql.Identifier("sub_id"),
        sql.Identifier("source_url"), sql.Identifier("source_sha256"),
        sql.Identifier("source_vintage"),
    ])
    # FORMAT csv, DELIMITER '|', QUOTE = ASCII bell. The bell-character
    # QUOTE effectively disables CSV quote interpretation (FEC bulk
    # files do not quote fields), while DELIMITER '|' matches FEC's
    # source format. NULL '' so empty fields land as NULL.
    copy_query = sql.SQL(
        "COPY {tbl} ({cols}) FROM STDIN "
        "WITH (FORMAT csv, DELIMITER '|', NULL '', QUOTE E'\\b')",
    ).format(tbl=_qualified_table(target_table), cols=col_idents)

    cycle_prefix = f"{cycle}|".encode()
    suffix = (
        f"|{fetch.source_url}|{fetch.source_sha256}|{fetch.source_vintage}\n"
    ).encode()

    # zipfile.ZipExtFile is a BufferedIOBase that supports line iteration
    # natively (returns bytes per line). We avoid wrapping in
    # io.BufferedReader because mypy's typeshed types ZipFile.open as
    # IO[bytes], which BufferedReader's TypeVar refuses; iterating
    # directly is just as fast and skips the wrapper allocation.
    n_rows = 0
    with (
        zipfile.ZipFile(fetch.path) as zf,
        zf.open(inner_name) as src,
        conn.cursor().copy(copy_query) as cp,
    ):
        out_buf = bytearray()
        for raw_line in src:
            stripped = raw_line.rstrip(b"\r\n")
            if not stripped:
                continue
            out_buf.extend(cycle_prefix)
            out_buf.extend(stripped)
            out_buf.extend(suffix)
            n_rows += 1
            if len(out_buf) >= _COPY_CHUNK_SIZE:
                cp.write(bytes(out_buf))
                out_buf.clear()
        if out_buf:
            cp.write(bytes(out_buf))

    log.info(
        "fec.load: streamed %d rows into %s (cycle=%s)",
        n_rows, target_table, cycle,
    )
    return n_rows


# ============================================================================
# CLI
# ============================================================================


@click.group()
def cli() -> None:
    """FEC bulk-data ingester (Tier 4 v1)."""


def _validate_cycle(_ctx: object, _param: object, value: str) -> str:
    if not re.match(r"^[0-9]{4}$", value):
        raise click.BadParameter(f"Bad cycle {value!r}; expected 4-digit year.")
    return value


def _resolve_files(value: str) -> Iterable[str]:
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if not parts:
        raise click.BadParameter("--files must be non-empty.")
    bad = [p for p in parts if p not in FILE_KINDS]
    if bad:
        raise click.BadParameter(
            f"Unknown file kinds {bad}; expected from {sorted(FILE_KINDS)}."
        )
    return parts


@cli.command("fetch")
@click.option("--cycle", required=True, callback=_validate_cycle,
              help="4-digit FEC cycle year, e.g. 2024.")
@click.option("--files", default="cn,cm,indiv", show_default=True,
              help="Comma-separated subset of {cn,cm,indiv}.")
@click.option("--dest-dir", type=click.Path(file_okay=False, path_type=Path),
              default=Path("data/manual/fec"), show_default=True)
@click.option("--overwrite", is_flag=True, default=False,
              help="Force re-download even if cached file exists.")
def fetch_cmd(cycle: str, files: str, dest_dir: Path, overwrite: bool) -> None:
    """Download FEC bulk files for one cycle into the local cache."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    for kind in _resolve_files(files):
        result = fetch_fec_bulk(
            cycle, kind, dest_dir=dest_dir / cycle, overwrite=overwrite,
        )
        click.echo(
            f"{kind}: {result.path}  "
            f"({result.n_bytes:,} bytes, "
            f"sha256={result.source_sha256[:12]}..., "
            f"cache_hit={result.cache_hit})",
        )


@cli.command("load")
@click.option("--cycle", required=True, callback=_validate_cycle)
@click.option("--files", default="cn,cm", show_default=True,
              help="Comma-separated subset of {cn,cm,indiv}. Default "
                   "excludes 'indiv' because it is multi-GB; pass "
                   "--files cn,cm,indiv to include it.")
@click.option("--dest-dir", type=click.Path(file_okay=False, path_type=Path),
              default=Path("data/manual/fec"), show_default=True)
@click.option("--dsn", envvar="PG_DSN", required=True)
def load_cmd(cycle: str, files: str, dest_dir: Path, dsn: str) -> None:
    """Fetch (cached) + COPY into Postgres for one cycle."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    import psycopg

    chosen = list(_resolve_files(files))
    with psycopg.connect(dsn) as conn:
        for kind in chosen:
            fetch = fetch_fec_bulk(
                cycle, kind, dest_dir=dest_dir / cycle, overwrite=False,
            )
            if kind == "indiv":
                n = load_fec_indiv(fetch, conn, cycle=cycle)
            else:
                parse = parse_fec_small_table(
                    fetch, cycle=cycle, file_kind=kind,
                )
                n = load_fec_small_table(parse, conn)
            conn.commit()
            click.echo(f"{kind}: {n:,} rows loaded into {FILE_KINDS[kind][2]}")
