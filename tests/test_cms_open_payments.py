"""Tests for the CMS Open Payments (General Payments) ingester.

Test taxonomy
-------------
1. Pure helpers (no IO)
   - general_payments_zip_url: pinned year, un-pinned year, out-of-range
   - _vintage_from_filename: parses _P{MMDDYYYY}_, falls back to filename

2. Pure parser (no DB)
   - parse_general_payments name-based column mapping with scrambled +
     extra columns
   - NJ default filter keeps only NJ rows; state_filter=None keeps all
   - blank Total_Amount preserved as '' (-> NULL at COPY), never 0
   - reads the GNRL member out of a real ZIP, ignoring the RSRCH member
   - header missing a required column -> IngestError
   - zero / multiple GNRL members -> IngestError
   - empty Record_ID -> IngestError
   - non-numeric / mixed Program_Year -> IngestError
   - zero rows after filter -> IngestError
   - expected_program_year mismatch -> IngestError

3. Integration (live_pg)
   - Create the FIXED raw.cms_open_payments_general contract inline (the
     SQL migration ships separately), then exercise the loader:
       * NJ round-trip row count + provenance populated
       * blank amount lands as SQL NULL (never 0)
       * DELETE-then-insert idempotency (re-load replaces the year)
       * a different program_year load leaves other years intact
       * intra-file duplicate Record_ID collapsed via DISTINCT ON
"""

from __future__ import annotations

import csv
import io
import zipfile
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from ingestion._base import IngestError
from ingestion.cms_open_payments import (
    DEFAULT_STATE_FILTER,
    FetchResult,
    _find_general_payments_member,
    _vintage_from_filename,
    general_payments_zip_url,
    load_to_postgres,
    parse_general_payments,
)

if TYPE_CHECKING:
    import psycopg


# ============================================================================
# Synthetic-file helpers
# ============================================================================

# Header deliberately scrambled and padded with extra columns so the tests
# prove the loader maps by NAME, not position. Program_Year sits near the
# end; three non-target columns sit before/between/after the targets.
_SYNTH_HEADER: list[str] = [
    "Change_Type",
    "Covered_Recipient_NPI",
    "Record_ID",
    "Recipient_State",
    "Covered_Recipient_First_Name",
    "Covered_Recipient_Last_Name",
    "Covered_Recipient_Profile_ID",
    "Teaching_Hospital_Name",
    "Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name",
    "Total_Amount_of_Payment_USDollars",
    "Date_of_Payment",
    "Nature_of_Payment_or_Transfer_of_Value",
    "Name_of_Drug_or_Biological_or_Device_or_Medical_Supply_1",
    "Program_Year",
    "Some_Trailing_Col",
]


def _render_csv(rows: list[dict[str, str]], *, header: list[str] | None = None) -> str:
    """Render row dicts (keyed by source column name) as a header+body CSV."""
    hdr = header if header is not None else _SYNTH_HEADER
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(hdr)
    for r in rows:
        w.writerow([r.get(c, "") for c in hdr])
    return buf.getvalue()


def _write_csv_fetch(tmp_path: Path, rows: list[dict[str, str]]) -> FetchResult:
    """Write a synthetic General Payments CSV and wrap it in a FetchResult."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "OP_DTL_GNRL_PGYR2023_P01302025_01212025.csv"
    path.write_text(_render_csv(rows), encoding="utf-8")
    return FetchResult(
        path=path,
        source_url="https://example.test/PGYR2023_P01302025_01212025.zip",
        source_sha256="0" * 64,
        source_vintage="2025-01-30",
        n_bytes=path.stat().st_size,
        cache_hit=False,
    )


def _write_zip_fetch(
    tmp_path: Path,
    rows: list[dict[str, str]],
    *,
    extra_members: dict[str, str] | None = None,
) -> FetchResult:
    """Write a ZIP containing the GNRL CSV member (+ optional extra members)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "PGYR2023_P01302025_01212025.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "OP_DTL_GNRL_PGYR2023_P01302025_01212025.csv", _render_csv(rows),
        )
        for name, content in (extra_members or {}).items():
            zf.writestr(name, content)
    return FetchResult(
        path=path,
        source_url="https://example.test/PGYR2023_P01302025_01212025.zip",
        source_sha256="0" * 64,
        source_vintage="2025-01-30",
        n_bytes=path.stat().st_size,
        cache_hit=False,
    )


# Two NJ rows (one fully populated, one with blank NPI + blank amount) and
# two out-of-state rows that the NJ filter must drop.
_ROWS: list[dict[str, str]] = [
    {
        "Record_ID": "100000001",
        "Program_Year": "2023",
        "Covered_Recipient_NPI": "1234567890",
        "Covered_Recipient_Profile_ID": "555001",
        "Covered_Recipient_First_Name": "JANE",
        "Covered_Recipient_Last_Name": "DOE",
        "Recipient_State": "NJ",
        "Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name":
            "ACME PHARMA, INC.",
        "Total_Amount_of_Payment_USDollars": "1250.00",
        "Date_of_Payment": "06/15/2023",
        "Nature_of_Payment_or_Transfer_of_Value": "Consulting Fee",
        "Name_of_Drug_or_Biological_or_Device_or_Medical_Supply_1": "WIDGETOL",
    },
    {
        "Record_ID": "100000002",
        "Program_Year": "2023",
        "Covered_Recipient_NPI": "",  # blank NPI -> NULL
        "Covered_Recipient_Profile_ID": "555002",
        "Covered_Recipient_First_Name": "JOHN",
        "Covered_Recipient_Last_Name": "ROE",
        "Recipient_State": "nj",  # lower-case: filter is case-insensitive
        "Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name":
            "BETA DEVICES LLC",
        "Total_Amount_of_Payment_USDollars": "",  # blank amount -> NULL, never 0
        "Date_of_Payment": "11/02/2023",
        "Nature_of_Payment_or_Transfer_of_Value": "Food and Beverage",
        "Name_of_Drug_or_Biological_or_Device_or_Medical_Supply_1": "",
    },
    {
        "Record_ID": "100000003",
        "Program_Year": "2023",
        "Covered_Recipient_NPI": "9999999999",
        "Recipient_State": "CA",
        "Covered_Recipient_Last_Name": "WEST",
        "Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name": "GAMMA CO",
        "Total_Amount_of_Payment_USDollars": "9999.99",
        "Date_of_Payment": "01/01/2023",
        "Nature_of_Payment_or_Transfer_of_Value": "Travel",
    },
    {
        "Record_ID": "100000004",
        "Program_Year": "2023",
        "Recipient_State": "NY",
        "Covered_Recipient_Last_Name": "EAST",
        "Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name": "DELTA INC",
        "Total_Amount_of_Payment_USDollars": "42.00",
        "Date_of_Payment": "02/02/2023",
        "Nature_of_Payment_or_Transfer_of_Value": "Gift",
    },
]


# ============================================================================
# 1. Pure helpers
# ============================================================================


def test_general_payments_zip_url_returns_pinned_year() -> None:
    url = general_payments_zip_url(2023)
    assert url == (
        "https://download.cms.gov/openpayments/"
        "PGYR2023_P01302025_01212025.zip"
    )


def test_general_payments_zip_url_templates_unpinned_year() -> None:
    url = general_payments_zip_url(2099)
    assert url.startswith("https://download.cms.gov/openpayments/PGYR2099_P")
    assert url.endswith(".zip")


def test_general_payments_zip_url_rejects_out_of_range() -> None:
    with pytest.raises(IngestError, match="out of range"):
        general_payments_zip_url(1999)


def test_vintage_from_filename_parses_publication_date() -> None:
    assert (
        _vintage_from_filename("PGYR2023_P01302025_01212025.zip") == "2025-01-30"
    )
    assert (
        _vintage_from_filename("OP_DTL_GNRL_PGYR2023_P06302024_05012024.csv")
        == "2024-06-30"
    )


def test_vintage_from_filename_falls_back_to_name() -> None:
    assert _vintage_from_filename("manual_extract.csv") == "manual_extract.csv"


def test_find_general_payments_member_rejects_zero_matches() -> None:
    with pytest.raises(IngestError, match="No General Payments CSV member"):
        _find_general_payments_member(["OP_DTL_RSRCH_PGYR2023_P0.csv", "README.txt"])


def test_find_general_payments_member_rejects_multiple_matches() -> None:
    with pytest.raises(IngestError, match="Multiple General Payments members"):
        _find_general_payments_member([
            "OP_DTL_GNRL_PGYR2023_P0.csv",
            "OP_DTL_GNRL_PGYR2022_P0.csv",
        ])


# ============================================================================
# 2. Pure parser (no DB)
# ============================================================================


def test_parse_nj_filter_keeps_only_nj_rows(tmp_path: Path) -> None:
    fetch = _write_csv_fetch(tmp_path, _ROWS)
    parsed = parse_general_payments(fetch)  # default NJ

    assert parsed.state_filter == "NJ"
    assert parsed.program_year == 2023
    assert parsed.n_rows == 2  # the CA and NY rows are dropped

    by_id = {r[0]: r for r in parsed.rows}
    assert set(by_id) == {"100000001", "100000002"}

    # Field mapping by name (header was scrambled): row 1 spot-check.
    r1 = by_id["100000001"]
    # Tuple order == raw column order.
    (
        record_id, program_year, npi, profile_id, first, last, state,
        payer, amount, pay_date, nature, product,
    ) = r1
    assert record_id == "100000001"
    assert program_year == "2023"
    assert npi == "1234567890"
    assert profile_id == "555001"
    assert first == "JANE"
    assert last == "DOE"
    assert state == "NJ"
    assert payer == "ACME PHARMA, INC."
    assert amount == "1250.00"
    assert pay_date == "06/15/2023"
    assert nature == "Consulting Fee"
    assert product == "WIDGETOL"


def test_parse_blank_amount_and_npi_preserved_as_empty(tmp_path: Path) -> None:
    """Blank numeric/text stays '' in the tuple (COPY converts to NULL)."""
    fetch = _write_csv_fetch(tmp_path, _ROWS)
    parsed = parse_general_payments(fetch)
    row2 = next(r for r in parsed.rows if r[0] == "100000002")
    npi = row2[2]
    amount = row2[8]
    assert npi == ""  # never coerced
    assert amount == ""  # never coerced to 0


def test_parse_national_keeps_all_states(tmp_path: Path) -> None:
    fetch = _write_csv_fetch(tmp_path, _ROWS)
    parsed = parse_general_payments(fetch, state_filter=None)
    assert parsed.state_filter is None
    assert parsed.n_rows == 4
    assert {r[6] for r in parsed.rows} == {"NJ", "nj", "CA", "NY"}


def test_parse_reads_gnrl_member_from_zip_ignoring_rsrch(tmp_path: Path) -> None:
    fetch = _write_zip_fetch(
        tmp_path,
        _ROWS,
        extra_members={
            "OP_DTL_RSRCH_PGYR2023_P01302025.csv": "garbage,header\n1,2\n",
            "README.txt": "notes",
        },
    )
    parsed = parse_general_payments(fetch)
    assert parsed.n_rows == 2  # NJ only, from the GNRL member


def test_parse_rejects_zip_with_multiple_gnrl_members(tmp_path: Path) -> None:
    fetch = _write_zip_fetch(
        tmp_path,
        _ROWS,
        extra_members={
            "OP_DTL_GNRL_PGYR2022_P01302025.csv": _render_csv(_ROWS),
        },
    )
    with pytest.raises(IngestError, match="Multiple General Payments members"):
        parse_general_payments(fetch)


def test_parse_rejects_missing_required_column(tmp_path: Path) -> None:
    header = [c for c in _SYNTH_HEADER if c != "Record_ID"]
    path = tmp_path / "OP_DTL_GNRL_PGYR2023_P0.csv"
    path.write_text(_render_csv(_ROWS, header=header), encoding="utf-8")
    fetch = FetchResult(
        path=path, source_url="x", source_sha256="0" * 64,
        source_vintage="v", n_bytes=path.stat().st_size, cache_hit=False,
    )
    with pytest.raises(IngestError, match="missing required columns"):
        parse_general_payments(fetch, state_filter=None)


def test_parse_rejects_empty_record_id(tmp_path: Path) -> None:
    rows = [{**_ROWS[0], "Record_ID": ""}]
    fetch = _write_csv_fetch(tmp_path, rows)
    with pytest.raises(IngestError, match="empty Record_ID"):
        parse_general_payments(fetch)


def test_parse_rejects_non_numeric_program_year(tmp_path: Path) -> None:
    rows = [{**_ROWS[0], "Program_Year": "FY23"}]
    fetch = _write_csv_fetch(tmp_path, rows)
    with pytest.raises(IngestError, match="not a 4-digit year"):
        parse_general_payments(fetch)


def test_parse_rejects_mixed_program_years(tmp_path: Path) -> None:
    rows = [
        {**_ROWS[0], "Record_ID": "A", "Program_Year": "2022"},
        {**_ROWS[0], "Record_ID": "B", "Program_Year": "2023"},
    ]
    fetch = _write_csv_fetch(tmp_path, rows)
    with pytest.raises(IngestError, match="mixes multiple Program_Year"):
        parse_general_payments(fetch)


def test_parse_rejects_zero_rows_after_filter(tmp_path: Path) -> None:
    only_out_of_state = [_ROWS[2], _ROWS[3]]  # CA, NY
    fetch = _write_csv_fetch(tmp_path, only_out_of_state)
    with pytest.raises(IngestError, match="parsed 0 rows"):
        parse_general_payments(fetch)  # NJ filter -> nothing


def test_parse_rejects_expected_program_year_mismatch(tmp_path: Path) -> None:
    fetch = _write_csv_fetch(tmp_path, _ROWS)
    with pytest.raises(IngestError, match="does not match expected"):
        parse_general_payments(fetch, expected_program_year=2022)


def test_default_state_filter_is_nj() -> None:
    assert DEFAULT_STATE_FILTER == "NJ"


# ============================================================================
# 3. Integration (live_pg)
# ============================================================================
#
# The SQL migration for raw.cms_open_payments_general ships separately, so
# these tests build the FIXED contract inline and exercise the loader's SQL
# directly. Skipped when PG_TEST_DSN is not set (see tests/conftest.py).
# ============================================================================


_RAW_TABLE_DDL = """
CREATE SCHEMA IF NOT EXISTS raw;
DROP TABLE IF EXISTS raw.cms_open_payments_general;
CREATE TABLE raw.cms_open_payments_general (
    record_id                    TEXT        NOT NULL,
    program_year                 SMALLINT    NOT NULL,
    covered_recipient_npi        TEXT,
    covered_recipient_profile_id TEXT,
    recipient_first_name         TEXT,
    recipient_last_name          TEXT,
    recipient_state              TEXT,
    payer_name                   TEXT,
    payment_amount               NUMERIC,
    payment_date                 TEXT,
    nature_of_payment            TEXT,
    product_name                 TEXT,
    source_url                   TEXT        NOT NULL,
    source_sha256                CHAR(64)    NOT NULL,
    source_vintage               TEXT        NOT NULL,
    ingested_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (record_id)
);
"""


@pytest.fixture
def cms_db(live_pg: psycopg.Connection) -> psycopg.Connection:
    """Create the fixed raw contract inline; yield the connection."""
    conn = live_pg
    with conn.cursor() as cur:
        cur.execute(_RAW_TABLE_DDL)
    conn.commit()
    return conn


@pytest.mark.live_pg
def test_load_nj_round_trip(cms_db: psycopg.Connection, tmp_path: Path) -> None:
    fetch = _write_csv_fetch(tmp_path, _ROWS)
    parsed = parse_general_payments(fetch)
    n = load_to_postgres(parsed, cms_db)
    cms_db.commit()
    assert n == 2

    with cms_db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM raw.cms_open_payments_general")
        assert (cur.fetchone() or (None,))[0] == 2

        # Provenance populated on every row.
        cur.execute(
            "SELECT DISTINCT source_url, source_sha256, source_vintage, program_year "
            "FROM raw.cms_open_payments_general",
        )
        rows = cur.fetchall()
        assert len(rows) == 1
        url, sha, vintage, py = rows[0]
        assert url.endswith(".zip")
        assert len(sha) == 64
        assert vintage == "2025-01-30"
        assert py == 2023


@pytest.mark.live_pg
def test_load_blank_amount_is_sql_null_not_zero(
    cms_db: psycopg.Connection, tmp_path: Path,
) -> None:
    fetch = _write_csv_fetch(tmp_path, _ROWS)
    parsed = parse_general_payments(fetch)
    load_to_postgres(parsed, cms_db)
    cms_db.commit()

    with cms_db.cursor() as cur:
        cur.execute(
            "SELECT payment_amount, covered_recipient_npi "
            "FROM raw.cms_open_payments_general WHERE record_id = '100000002'",
        )
        row = cur.fetchone()
        assert row is not None
        amount, npi = row
        assert amount is None  # NULL, never 0
        assert npi is None  # blank NPI -> NULL

        # The populated row keeps its numeric value.
        cur.execute(
            "SELECT payment_amount FROM raw.cms_open_payments_general "
            "WHERE record_id = '100000001'",
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == Decimal("1250.00")


@pytest.mark.live_pg
def test_load_is_idempotent_replace_by_program_year(
    cms_db: psycopg.Connection, tmp_path: Path,
) -> None:
    fetch = _write_csv_fetch(tmp_path, _ROWS)
    parsed = parse_general_payments(fetch)
    load_to_postgres(parsed, cms_db)
    cms_db.commit()

    # Re-load the same year -- DELETE-then-insert keeps the count stable.
    load_to_postgres(parsed, cms_db)
    cms_db.commit()

    with cms_db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM raw.cms_open_payments_general")
        assert (cur.fetchone() or (None,))[0] == 2


@pytest.mark.live_pg
def test_load_other_year_leaves_existing_intact(
    cms_db: psycopg.Connection, tmp_path: Path,
) -> None:
    fetch_2023 = _write_csv_fetch(tmp_path / "y23", _ROWS)
    load_to_postgres(parse_general_payments(fetch_2023), cms_db)
    cms_db.commit()

    rows_2022 = [
        {**_ROWS[0], "Record_ID": "200000001", "Program_Year": "2022"},
    ]
    fetch_2022 = _write_csv_fetch(tmp_path / "y22", rows_2022)
    load_to_postgres(parse_general_payments(fetch_2022), cms_db)
    cms_db.commit()

    with cms_db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM raw.cms_open_payments_general")
        assert (cur.fetchone() or (None,))[0] == 3  # 2 from 2023 + 1 from 2022

        cur.execute(
            "SELECT COUNT(*) FROM raw.cms_open_payments_general "
            "WHERE program_year = 2023",
        )
        assert (cur.fetchone() or (None,))[0] == 2


@pytest.mark.live_pg
def test_load_dedups_intra_file_duplicate_record_id(
    cms_db: psycopg.Connection, tmp_path: Path,
) -> None:
    """A duplicate Record_ID within one file must not raise on the PK."""
    rows = [*_ROWS, dict(_ROWS[0])]  # exact dup of record 100000001
    fetch = _write_csv_fetch(tmp_path, rows)
    parsed = parse_general_payments(fetch)
    assert parsed.n_rows == 3  # parser does not dedup (2 NJ + 1 dup)

    n = load_to_postgres(parsed, cms_db)  # DISTINCT ON collapses the dup
    cms_db.commit()
    assert n == 2

    with cms_db.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM raw.cms_open_payments_general "
            "WHERE record_id = '100000001'",
        )
        assert (cur.fetchone() or (None,))[0] == 1
