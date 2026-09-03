"""DOL WHD H-1B debarment + willful-violator list ingester (FRAUD-V1b).

Parses the two public HTML tables and loads them into
``raw.dol_whd_h1b_list``. Keyless, free, full-replace.

Sources
-------
https://www.dol.gov/agencies/whd/immigration/h1b/debarment
https://www.dol.gov/agencies/whd/immigration/h1b/willful-violator-list

WHD does not publish a stable machine-readable dump. The lists are tiny
(typically <20 rows). We parse the HTML ``<table>`` with the stdlib
``html.parser`` so there is no BeautifulSoup dependency.

Architecture
------------
1. **Parse** -- :func:`parse_whd_h1b_html` (pure; HTML in, DataFrame out).
2. **Stage** -- provenance + canonical employer name.
3. **Load** -- DELETE the ``list_kind`` slice, then COPY.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Final

import click
import polars as pl

from ingestion._base import IngestError, canonical_employer_name, sha256_file

if TYPE_CHECKING:
    import psycopg

log = logging.getLogger(__name__)

_COPY_CHUNK = 1 << 20

WHD_DEBARMENT_URL: Final[str] = (
    "https://www.dol.gov/agencies/whd/immigration/h1b/debarment"
)
WHD_WILLFUL_URL: Final[str] = (
    "https://www.dol.gov/agencies/whd/immigration/h1b/willful-violator-list"
)

_DEST_COLS: Final[tuple[str, ...]] = (
    "list_kind",
    "employer_name",
    "employer_canonical_name",
    "employer_address",
    "city",
    "state",
    "willful_violator",
    "debarment_start",
    "debarment_end",
    "determination_date",
    "determining_agency",
    "list_effective_date",
    "source_page_updated",
    "source_url",
    "source_filename",
    "source_sha256",
    "data_quality",
)

_HTTP_HEADERS: Final[dict[str, str]] = {
    "User-Agent": (
        "NJ-Unchained/1.0 (civic-integrity research; "
        "+https://github.com/dantebozzuti27/NJ_unchained)"
    )
}

_UPDATED_RE: Final[re.Pattern[str]] = re.compile(
    r"Last Updated on\s+([A-Za-z]+ \d{1,2}, \d{4})",
    re.IGNORECASE,
)
_EFFECTIVE_RE: Final[re.Pattern[str]] = re.compile(
    r"effective as of\s+([A-Za-z]+ \d{1,2}, \d{4})",
    re.IGNORECASE,
)
_PERIOD_RE: Final[re.Pattern[str]] = re.compile(
    r"(\d{1,2}/\d{1,2}/\d{4})\s+to\s+(\d{1,2}/\d{1,2}/\d{4})",
    re.IGNORECASE,
)


class _TableParser(HTMLParser):
    """Collect every HTML table as a list of row-of-strings."""

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            if any(self._row):
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.tables.append(self._table)
            self._table = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _parse_mdy(raw: str) -> date | None:
    raw = raw.strip()
    if not raw:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _parse_month_day_year(raw: str) -> date | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%B %d, %Y").date()
    except ValueError:
        return None


def _canon_header(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def extract_html_tables(html: str) -> list[list[list[str]]]:
    """Return every HTML table as rows of cell strings."""
    parser = _TableParser()
    parser.feed(html)
    parser.close()
    return parser.tables


def _page_dates(html: str) -> tuple[date | None, date | None]:
    updated = None
    effective = None
    m = _UPDATED_RE.search(html)
    if m:
        updated = _parse_month_day_year(m.group(1))
    m = _EFFECTIVE_RE.search(html)
    if m:
        effective = _parse_month_day_year(m.group(1))
    return updated, effective


def _pick_table(
    tables: list[list[list[str]]], required: frozenset[str]
) -> list[list[str]]:
    for table in tables:
        if not table:
            continue
        headers = {_canon_header(c) for c in table[0]}
        if required <= headers:
            return table
    raise IngestError(
        f"WHD HTML has no table with headers {sorted(required)}. "
        f"Saw {[t[0] for t in tables if t][:4]}"
    )


def _header_index(header: list[str], *aliases: str) -> int | None:
    canon = {_canon_header(c): i for i, c in enumerate(header)}
    for alias in aliases:
        if alias in canon:
            return canon[alias]
    return None


def _cell(row: list[str], idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return row[idx].strip()


def parse_whd_h1b_html(html: str, *, list_kind: str, source_url: str) -> pl.DataFrame:
    """Parse one WHD list page into a canonical DataFrame (no provenance)."""
    if list_kind not in {"debarment", "willful"}:
        raise IngestError(f"Unknown WHD list_kind {list_kind!r}")

    tables = extract_html_tables(html)
    updated, effective = _page_dates(html)

    if list_kind == "debarment":
        table = _pick_table(tables, frozenset({"employername", "debarmentperiod"}))
        header, *rows = table
        i_name = _header_index(header, "employername")
        i_addr = _header_index(header, "employeraddress", "address")
        i_willful = _header_index(header, "willfulviolator")
        i_period = _header_index(header, "debarmentperiod")
        if i_name is None or i_period is None:
            raise IngestError("Debarment table missing Employer Name or Debarment Period")
        records: list[dict[str, object]] = []
        for row in rows:
            name = _cell(row, i_name)
            if not name:
                continue
            period = _cell(row, i_period)
            start, end = None, None
            m = _PERIOD_RE.search(period)
            if m:
                start = _parse_mdy(m.group(1))
                end = _parse_mdy(m.group(2))
            willful_raw = _cell(row, i_willful).lower()
            records.append({
                "list_kind": "debarment",
                "employer_name": name,
                "employer_canonical_name": canonical_employer_name(name),
                "employer_address": _cell(row, i_addr) or None,
                "city": None,
                "state": None,
                "willful_violator": willful_raw in {"yes", "y", "true"} if willful_raw else None,
                "debarment_start": start,
                "debarment_end": end,
                "determination_date": None,
                "determining_agency": None,
                "list_effective_date": effective,
                "source_page_updated": updated,
                "source_url": source_url,
            })
        return pl.DataFrame(records)

    table = _pick_table(
        tables, frozenset({"employername", "dateofwillfulviolationdetermination"})
    )
    header, *rows = table
    i_name = _header_index(header, "employername")
    i_city = _header_index(header, "city")
    i_state = _header_index(header, "state")
    i_date = _header_index(header, "dateofwillfulviolationdetermination")
    i_agency = _header_index(header, "agencymakingdetermination", "agency")
    if i_name is None or i_date is None:
        raise IngestError("Willful table missing Employer Name or determination date")
    records = []
    for row in rows:
        name = _cell(row, i_name)
        if not name:
            continue
        records.append({
            "list_kind": "willful",
            "employer_name": name,
            "employer_canonical_name": canonical_employer_name(name),
            "employer_address": None,
            "city": _cell(row, i_city) or None,
            "state": _cell(row, i_state) or None,
            "willful_violator": True,
            "debarment_start": None,
            "debarment_end": None,
            "determination_date": _parse_mdy(_cell(row, i_date)),
            "determining_agency": _cell(row, i_agency) or None,
            "list_effective_date": effective,
            "source_page_updated": updated,
            "source_url": source_url,
        })
    return pl.DataFrame(records)


@dataclass(frozen=True)
class ParseResult:
    dataframe: pl.DataFrame
    list_kind: str
    source_filename: str
    source_sha256: str
    source_url: str
    n_output_rows: int


def parse_whd_h1b_file(
    path: Path, *, list_kind: str, source_url: str
) -> ParseResult:
    """Read a saved WHD HTML page and parse it."""
    html = path.read_text(encoding="utf-8", errors="replace")
    df = parse_whd_h1b_html(html, list_kind=list_kind, source_url=source_url)
    if df.height == 0:
        raise IngestError(f"Parsed zero WHD {list_kind} rows from {path}")
    # PK is (list_kind, employer_canonical_name); collapse rare dups.
    df = df.unique(subset=["list_kind", "employer_canonical_name"], keep="first")
    return ParseResult(
        dataframe=df,
        list_kind=list_kind,
        source_filename=path.name,
        source_sha256=sha256_file(path),
        source_url=source_url,
        n_output_rows=df.height,
    )


def stage_dataframe(parse: ParseResult) -> pl.DataFrame:
    """Add provenance columns and project to the destination column list."""
    df = parse.dataframe.with_columns([
        pl.lit(parse.source_filename, dtype=pl.Utf8).alias("source_filename"),
        pl.lit(parse.source_sha256, dtype=pl.Utf8).alias("source_sha256"),
        pl.lit("measured", dtype=pl.Utf8).alias("data_quality"),
    ])
    for col in _DEST_COLS:
        if col not in df.columns:
            df = df.with_columns(pl.lit(None).cast(pl.Utf8).alias(col))
    return df.select(_DEST_COLS)


def load_to_postgres(
    staged: pl.DataFrame,
    connection: psycopg.Connection,
    *,
    list_kind: str,
    table: str = "raw.dol_whd_h1b_list",
) -> int:
    """DELETE the list_kind slice, then COPY. Returns rows loaded."""
    from psycopg import sql

    with connection.cursor() as cur:
        cur.execute(
            "DELETE FROM raw.dol_whd_h1b_list WHERE list_kind = %s",
            (list_kind,),
        )

    buf = io.BytesIO()
    staged.write_csv(buf, include_header=False)
    buf.seek(0)

    schema_part, table_part = table.split(".", 1)
    ident = sql.Identifier(schema_part, table_part)
    col_idents = sql.SQL(", ").join(sql.Identifier(c) for c in staged.columns)
    copy_query = sql.SQL(
        "COPY {tbl} ({cols}) FROM STDIN WITH (FORMAT csv, NULL '')"
    ).format(tbl=ident, cols=col_idents)

    with connection.cursor().copy(copy_query) as cp:
        while chunk := buf.read(_COPY_CHUNK):
            cp.write(chunk)
    return staged.height


def fetch_whd_h1b_page(
    list_kind: str,
    *,
    out_dir: Path,
    overwrite: bool = False,
    timeout_s: float = 60.0,
) -> Path:
    """Download one WHD HTML page. Returns the local path."""
    import httpx

    url = WHD_DEBARMENT_URL if list_kind == "debarment" else WHD_WILLFUL_URL
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"whd_h1b_{list_kind}.html"
    if dest.exists() and not overwrite:
        log.info("Skipping fetch (already present): %s", dest)
        return dest

    with httpx.Client(
        timeout=timeout_s, follow_redirects=True, headers=_HTTP_HEADERS
    ) as client:
        log.info("Fetching WHD %s list: %s", list_kind, url)
        resp = client.get(url)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
    log.info("Wrote %s (%d bytes)", dest, dest.stat().st_size)
    return dest


@click.group()
def cli() -> None:
    """DOL WHD H-1B debarment + willful-violator list ingester."""


@cli.command("fetch")
@click.option(
    "--list",
    "list_kind",
    type=click.Choice(["debarment", "willful", "both"]),
    default="both",
)
@click.option(
    "--out-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("data/manual/dol_whd_h1b"),
    show_default=True,
)
@click.option("--overwrite", is_flag=True, default=False)
def fetch_cmd(list_kind: str, out_dir: Path, overwrite: bool) -> None:
    """Download WHD HTML list page(s)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    kinds = ("debarment", "willful") if list_kind == "both" else (list_kind,)
    for kind in kinds:
        dest = fetch_whd_h1b_page(kind, out_dir=out_dir, overwrite=overwrite)
        click.echo(str(dest))


@cli.command("load")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--list",
    "list_kind",
    type=click.Choice(["debarment", "willful"]),
    required=True,
)
@click.option("--dsn", envvar="PG_DSN", required=True)
@click.option("--dry-run", is_flag=True, default=False)
def load_cmd(path: Path, list_kind: str, dsn: str, dry_run: bool) -> None:
    """Parse + stage + load one WHD HTML list into ``raw.dol_whd_h1b_list``."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    url = WHD_DEBARMENT_URL if list_kind == "debarment" else WHD_WILLFUL_URL
    result = parse_whd_h1b_file(path, list_kind=list_kind, source_url=url)
    staged = stage_dataframe(result)
    log.info("Staged %d %s rows from %s", staged.height, list_kind, path.name)
    if dry_run:
        click.echo("--dry-run: skipping COPY.")
        return

    import psycopg

    with psycopg.connect(dsn) as conn:
        n = load_to_postgres(staged, conn, list_kind=list_kind)
        conn.commit()
        click.echo(f"Loaded {n} rows into raw.dol_whd_h1b_list ({list_kind}).")


if __name__ == "__main__":
    cli()
