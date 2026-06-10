"""Tests for the CMS "by Provider" Medicare physician ingester.

Test taxonomy
-------------
1. Pure helpers (no I/O)
   - _validate_data_year accepts in-range, rejects out-of-range
   - _clean_npi accepts 10 digits (leading zeros), rejects everything else
   - _coerce_numeric: blank -> '' (NULL), valid decimals pass, garbage raises
   - _select_download_url: exact-title disambiguation vs the
     "... and Service" sibling; correct year picked; missing year /
     missing title raise

2. Pure parser (no DB)
   - parse_cms_provider_csv on a synthetic file whose 12 required columns
     are interleaved with junk columns in a NON-raw order (proves
     name-based, not positional, selection)
   - blank/suppressed numeric cells -> '' (mapped to SQL NULL at load)
   - missing required header -> IngestError
   - bad NPI -> IngestError
   - non-numeric numeric cell -> IngestError
   - wrong field count row -> IngestError
   - empty file -> IngestError
   - header-only (zero data rows) -> IngestError

3. Integration (live_pg)
   - Apply migrations + seeds; load a synthetic file; verify
     raw.cms_physician_provider row count, provenance, data_year, and
     that blank numerics are SQL NULL (never 0) and NPI is stored as a
     10-char string.
   - Idempotent re-load of the same year is a no-op on row count.
   - DELETE-then-insert semantics: re-loading an edited file for the same
     year replaces the prior rows rather than accumulating them.

   NOTE: these are skipped by `-m "not live_pg"` and additionally require
   PG_TEST_DSN. They reference raw.cms_physician_provider, whose migration
   is authored separately; they will pass once that migration lands.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from ingestion._base import IngestError
from ingestion.cms_physician import (
    CMS_CSV_HEADERS,
    DATASET_TITLE,
    FetchResult,
    _clean_npi,
    _coerce_numeric,
    _select_download_url,
    _validate_data_year,
    load_to_postgres,
    parse_cms_provider_csv,
)

if TYPE_CHECKING:
    import psycopg


# ============================================================================
# 1. Pure helpers
# ============================================================================


def test_validate_data_year_accepts_in_range() -> None:
    _validate_data_year(2013)
    _validate_data_year(2023)
    _validate_data_year(2099)


def test_validate_data_year_rejects_out_of_range() -> None:
    for bad in (2012, 1999, 2100, 0, -1):
        with pytest.raises(IngestError, match="out of supported range"):
            _validate_data_year(bad)


def test_clean_npi_accepts_ten_digits_including_leading_zeros() -> None:
    assert _clean_npi("1234567890") == "1234567890"
    assert _clean_npi("0000000001") == "0000000001"  # leading zeros preserved
    assert _clean_npi("  1234567890  ") == "1234567890"


def test_clean_npi_rejects_non_ten_digit() -> None:
    for bad in ("", "123", "12345678901", "12345abcde", "123456789"):
        with pytest.raises(IngestError, match="not a 10-digit string"):
            _clean_npi(bad)


def test_coerce_numeric_blank_becomes_empty_for_null() -> None:
    assert _coerce_numeric("", field="tot_benes") == ""
    assert _coerce_numeric("   ", field="tot_benes") == ""


def test_coerce_numeric_accepts_bare_decimals() -> None:
    assert _coerce_numeric("0", field="tot_benes") == "0"
    assert _coerce_numeric("123", field="tot_srvcs") == "123"
    assert _coerce_numeric("123.45", field="tot_mdcr_pymt_amt") == "123.45"
    assert _coerce_numeric("-3.2", field="bene_avg_risk_scre") == "-3.2"


def test_coerce_numeric_rejects_garbage() -> None:
    for bad in ("abc", "1,234", "$50", "1.2.3", "12e3"):
        with pytest.raises(IngestError, match="not a bare decimal"):
            _coerce_numeric(bad, field="tot_sbmtd_chrg")


# ----------------------------------------------------------------------------
# Catalog selection
# ----------------------------------------------------------------------------


def _synth_catalog() -> dict[str, object]:
    """A minimal data.json with BOTH the target dataset and its sibling.

    The sibling title is a superset string of the target title, so a
    naive substring match would (wrongly) pick it. Exact == must win.
    """
    return {
        "dataset": [
            {
                "title": "Medicare Physician & Other Practitioners - by Provider and Service",
                "distribution": [
                    {
                        "mediaType": "text/csv",
                        "title": "... by Provider and Service : 2023-12-31",
                        "downloadURL": "https://data.cms.gov/WRONG_PROV_SVC_2023.csv",
                    },
                ],
            },
            {
                "title": DATASET_TITLE,
                "distribution": [
                    {"mediaType": None, "title": "x : 2023-12-31", "downloadURL": None},
                    {
                        "mediaType": "text/csv",
                        "title": f"{DATASET_TITLE} : 2024-12-01",
                        "downloadURL": "https://data.cms.gov/RIGHT_PROV_2024.csv",
                    },
                    {
                        "mediaType": "text/csv",
                        "title": f"{DATASET_TITLE} : 2023-12-31",
                        "downloadURL": "https://data.cms.gov/RIGHT_PROV_2023.csv",
                    },
                ],
            },
        ],
    }


def test_select_download_url_picks_exact_title_and_year() -> None:
    cat = _synth_catalog()
    assert _select_download_url(cat, data_year=2023) == "https://data.cms.gov/RIGHT_PROV_2023.csv"
    assert _select_download_url(cat, data_year=2024) == "https://data.cms.gov/RIGHT_PROV_2024.csv"


def test_select_download_url_does_not_match_sibling_substring() -> None:
    """The 'and Service' sibling must never be returned for the bare title."""
    cat = _synth_catalog()
    url = _select_download_url(cat, data_year=2023)
    assert "PROV_SVC" not in url
    assert "RIGHT_PROV_2023" in url


def test_select_download_url_missing_year_raises() -> None:
    cat = _synth_catalog()
    with pytest.raises(IngestError, match="no text/csv distribution for"):
        _select_download_url(cat, data_year=2019)


def test_select_download_url_missing_title_raises() -> None:
    with pytest.raises(IngestError, match="no dataset titled exactly"):
        _select_download_url({"dataset": []}, data_year=2023)


# ============================================================================
# 2. Pure parser (no DB)
# ============================================================================
#
# The synthetic file interleaves the 12 required columns with junk
# columns in an order that does NOT match the raw-table order, so the
# parser MUST select by header name, not position.
# ============================================================================

_SYNTH_COLUMNS: tuple[str, ...] = (
    "Rndrng_Prvdr_State_Abrvtn",
    "Rndrng_NPI",
    "Junk_Col_A",
    "Rndrng_Prvdr_Last_Org_Name",
    "Rndrng_Prvdr_First_Name",
    "Tot_Srvcs",
    "Rndrng_Prvdr_City",
    "Rndrng_Prvdr_Type",
    "Tot_Benes",
    "Tot_Mdcr_Alowd_Amt",
    "Junk_Col_B",
    "Tot_Mdcr_Pymt_Amt",
    "Tot_Sbmtd_Chrg",
    "Bene_Avg_Risk_Scre",
)


def _synth_cms_csv(rows: list[dict[str, str]]) -> str:
    """Render rows (keyed by CSV header) as a header + body CSV."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(_SYNTH_COLUMNS)
    for r in rows:
        writer.writerow([r.get(c, "x") for c in _SYNTH_COLUMNS])
    return buf.getvalue()


def _write_synth_cms(
    tmp_path: Path, rows: list[dict[str, str]], *, name: str = "cms_synth.csv",
) -> FetchResult:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / name
    path.write_text(_synth_cms_csv(rows), encoding="utf-8")
    return FetchResult(
        path=path,
        source_url="https://data.cms.gov/RIGHT_PROV_2023.csv",
        source_sha256="0" * 64,
        source_vintage="test-vintage",
        n_bytes=path.stat().st_size,
        cache_hit=False,
        data_year=2023,
    )


_FOUR_ROWS: list[dict[str, str]] = [
    {  # individual physician, all fields populated
        "Rndrng_NPI": "1234567890",
        "Rndrng_Prvdr_Last_Org_Name": "DOE",
        "Rndrng_Prvdr_First_Name": "JANE",
        "Rndrng_Prvdr_City": "TRENTON",
        "Rndrng_Prvdr_State_Abrvtn": "NJ",
        "Rndrng_Prvdr_Type": "Internal Medicine",
        "Tot_Benes": "250",
        "Tot_Srvcs": "1200",
        "Tot_Mdcr_Alowd_Amt": "150000.50",
        "Tot_Mdcr_Pymt_Amt": "120000.25",
        "Tot_Sbmtd_Chrg": "300000.00",
        "Bene_Avg_Risk_Scre": "1.7531",
    },
    {  # suppressed small-cell provider: blank benes + blank risk score
        "Rndrng_NPI": "9876543210",
        "Rndrng_Prvdr_Last_Org_Name": "ROE",
        "Rndrng_Prvdr_First_Name": "JOHN",
        "Rndrng_Prvdr_City": "NEWARK",
        "Rndrng_Prvdr_State_Abrvtn": "NJ",
        "Rndrng_Prvdr_Type": "Nurse Practitioner",
        "Tot_Benes": "",        # suppressed -> NULL (never 0)
        "Tot_Srvcs": "45",
        "Tot_Mdcr_Alowd_Amt": "5000",
        "Tot_Mdcr_Pymt_Amt": "4000",
        "Tot_Sbmtd_Chrg": "9000",
        "Bene_Avg_Risk_Scre": "",  # suppressed -> NULL
    },
    {  # organization with a comma in the name (CSV quoting), blank first name
        "Rndrng_NPI": "0000000001",  # leading-zero NPI must survive
        "Rndrng_Prvdr_Last_Org_Name": "ACME CARDIOLOGY GROUP, LLC",
        "Rndrng_Prvdr_First_Name": "",
        "Rndrng_Prvdr_City": "JERSEY CITY",
        "Rndrng_Prvdr_State_Abrvtn": "NJ",
        "Rndrng_Prvdr_Type": "Clinic/Group Practice",
        "Tot_Benes": "1100",
        "Tot_Srvcs": "8800",
        "Tot_Mdcr_Alowd_Amt": "990000",
        "Tot_Mdcr_Pymt_Amt": "880000",
        "Tot_Sbmtd_Chrg": "2200000",
        "Bene_Avg_Risk_Scre": "2.10",
    },
    {  # another individual
        "Rndrng_NPI": "5555555555",
        "Rndrng_Prvdr_Last_Org_Name": "OCONNOR",
        "Rndrng_Prvdr_First_Name": "TERRY",
        "Rndrng_Prvdr_City": "PRINCETON",
        "Rndrng_Prvdr_State_Abrvtn": "NJ",
        "Rndrng_Prvdr_Type": "Dentist",
        "Tot_Benes": "75",
        "Tot_Srvcs": "300",
        "Tot_Mdcr_Alowd_Amt": "60000",
        "Tot_Mdcr_Pymt_Amt": "48000",
        "Tot_Sbmtd_Chrg": "100000",
        "Bene_Avg_Risk_Scre": "0.9",
    },
]


def _by_col(parsed_row: tuple[str, ...]) -> dict[str, str]:
    from ingestion.cms_physician import _RAW_DATA_COLUMNS

    return dict(zip(_RAW_DATA_COLUMNS, parsed_row, strict=True))


def test_parse_happy_path_selects_columns_by_name(tmp_path: Path) -> None:
    fetch = _write_synth_cms(tmp_path, _FOUR_ROWS)
    parsed = parse_cms_provider_csv(fetch)

    assert parsed.n_rows == 4
    assert parsed.data_year == 2023

    row0 = _by_col(parsed.rows[0])
    assert row0["npi"] == "1234567890"
    assert row0["prvdr_last_org_name"] == "DOE"
    assert row0["prvdr_first_name"] == "JANE"
    assert row0["prvdr_city"] == "TRENTON"
    assert row0["prvdr_state_abrvtn"] == "NJ"
    assert row0["prvdr_type"] == "Internal Medicine"
    assert row0["tot_benes"] == "250"
    assert row0["tot_srvcs"] == "1200"
    assert row0["tot_mdcr_alowd_amt"] == "150000.50"
    assert row0["tot_mdcr_pymt_amt"] == "120000.25"
    assert row0["tot_sbmtd_chrg"] == "300000.00"
    assert row0["bene_avg_risk_scre"] == "1.7531"

    # Org row: comma-in-name preserved, leading-zero NPI preserved.
    row2 = _by_col(parsed.rows[2])
    assert row2["npi"] == "0000000001"
    assert row2["prvdr_last_org_name"] == "ACME CARDIOLOGY GROUP, LLC"
    assert row2["prvdr_first_name"] == ""


def test_parse_blank_numeric_becomes_empty_for_null(tmp_path: Path) -> None:
    fetch = _write_synth_cms(tmp_path, _FOUR_ROWS)
    parsed = parse_cms_provider_csv(fetch)
    row1 = _by_col(parsed.rows[1])
    # Suppressed cells are '' (NOT '0') so the loader writes SQL NULL.
    assert row1["tot_benes"] == ""
    assert row1["bene_avg_risk_scre"] == ""
    # A genuine zero stays a zero, never confused with a suppressed blank.
    assert _by_col(parsed.rows[0])["tot_benes"] == "250"


def test_parse_rejects_missing_required_header(tmp_path: Path) -> None:
    body = _synth_cms_csv(_FOUR_ROWS).replace("Tot_Benes", "Tot_Benez", 1)
    p = tmp_path / "missing_header.csv"
    p.write_text(body, encoding="utf-8")
    fetch = FetchResult(
        path=p, source_url="x", source_sha256="0" * 64, source_vintage="v",
        n_bytes=p.stat().st_size, cache_hit=False, data_year=2023,
    )
    with pytest.raises(IngestError, match="missing required headers"):
        parse_cms_provider_csv(fetch)


def test_parse_rejects_bad_npi(tmp_path: Path) -> None:
    rows = [dict(_FOUR_ROWS[0])]
    rows[0]["Rndrng_NPI"] = "12345"  # too short
    fetch = _write_synth_cms(tmp_path, rows)
    with pytest.raises(IngestError, match="not a 10-digit string"):
        parse_cms_provider_csv(fetch)


def test_parse_rejects_non_numeric_amount(tmp_path: Path) -> None:
    rows = [dict(_FOUR_ROWS[0])]
    rows[0]["Tot_Mdcr_Pymt_Amt"] = "N/A"
    fetch = _write_synth_cms(tmp_path, rows)
    with pytest.raises(IngestError, match="not a bare decimal"):
        parse_cms_provider_csv(fetch)


def test_parse_rejects_wrong_field_count(tmp_path: Path) -> None:
    body = _synth_cms_csv([_FOUR_ROWS[0]])
    body += "NJ,1234567890,x\n"  # short row (3 fields)
    p = tmp_path / "shortrow.csv"
    p.write_text(body, encoding="utf-8")
    fetch = FetchResult(
        path=p, source_url="x", source_sha256="0" * 64, source_vintage="v",
        n_bytes=p.stat().st_size, cache_hit=False, data_year=2023,
    )
    with pytest.raises(IngestError, match=r"got \d+ fields, expected"):
        parse_cms_provider_csv(fetch)


def test_parse_rejects_empty_file(tmp_path: Path) -> None:
    p = tmp_path / "empty.csv"
    p.write_text("", encoding="utf-8")
    fetch = FetchResult(
        path=p, source_url="x", source_sha256="0" * 64, source_vintage="v",
        n_bytes=0, cache_hit=False, data_year=2023,
    )
    with pytest.raises(IngestError, match="empty"):
        parse_cms_provider_csv(fetch)


def test_parse_rejects_zero_data_rows(tmp_path: Path) -> None:
    p = tmp_path / "headeronly.csv"
    p.write_text(",".join(CMS_CSV_HEADERS) + "\n", encoding="utf-8")
    fetch = FetchResult(
        path=p, source_url="x", source_sha256="0" * 64, source_vintage="v",
        n_bytes=p.stat().st_size, cache_hit=False, data_year=2023,
    )
    with pytest.raises(IngestError, match="parsed 0 data rows"):
        parse_cms_provider_csv(fetch)


def test_parse_skips_blank_lines(tmp_path: Path) -> None:
    body = _synth_cms_csv([_FOUR_ROWS[0]])
    body += "\n\n"
    body += _synth_cms_csv([_FOUR_ROWS[2]]).split("\n", 1)[1]  # body only (drop header)
    p = tmp_path / "blanks.csv"
    p.write_text(body, encoding="utf-8")
    fetch = FetchResult(
        path=p, source_url="x", source_sha256="0" * 64, source_vintage="v",
        n_bytes=p.stat().st_size, cache_hit=False, data_year=2023,
    )
    parsed = parse_cms_provider_csv(fetch)
    assert parsed.n_rows == 2


# ============================================================================
# 3. Integration (live_pg)
# ============================================================================
#
# Skipped by `-m "not live_pg"`; also require PG_TEST_DSN. They reference
# raw.cms_physician_provider, whose migration is authored separately.
# ============================================================================


@pytest.fixture
def cms_db(live_pg: psycopg.Connection) -> psycopg.Connection:
    """Apply all migrations + seeds into a fresh schema set; yield the conn."""
    from scripts.migrate import (
        MIGRATIONS_DIR,
        SEEDS_DIR,
        apply_migrations,
        discover,
    )

    conn = live_pg
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS governance CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS derived    CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS raw        CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS ref        CASCADE")
    conn.commit()
    apply_migrations(conn, discover(MIGRATIONS_DIR))
    apply_migrations(conn, discover(SEEDS_DIR))
    return conn


@pytest.mark.live_pg
def test_load_synthetic_round_trip(
    cms_db: psycopg.Connection, tmp_path: Path,
) -> None:
    """Happy path: 4-row file lands as 4 rows with correct provenance."""
    fetch = _write_synth_cms(tmp_path, _FOUR_ROWS)
    parsed = parse_cms_provider_csv(fetch)
    n = load_to_postgres(parsed, cms_db)
    cms_db.commit()
    assert n == 4

    with cms_db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM raw.cms_physician_provider")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 4

        cur.execute(
            "SELECT DISTINCT data_year, source_url, source_sha256, source_vintage "
            "FROM raw.cms_physician_provider",
        )
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 2023


@pytest.mark.live_pg
def test_blank_numeric_is_sql_null_not_zero(
    cms_db: psycopg.Connection, tmp_path: Path,
) -> None:
    """Suppressed numeric cells must be SQL NULL, never 0; NPI stays a string."""
    fetch = _write_synth_cms(tmp_path, _FOUR_ROWS)
    parsed = parse_cms_provider_csv(fetch)
    load_to_postgres(parsed, cms_db)
    cms_db.commit()

    with cms_db.cursor() as cur:
        cur.execute(
            "SELECT tot_benes, bene_avg_risk_scre "
            "FROM raw.cms_physician_provider WHERE npi = '9876543210'",
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] is None  # suppressed -> NULL (not 0)
        assert row[1] is None

        # Leading-zero NPI preserved as a 10-char string.
        cur.execute(
            "SELECT npi FROM raw.cms_physician_provider WHERE npi = '0000000001'",
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "0000000001"


@pytest.mark.live_pg
def test_idempotent_reload_same_year_is_no_op_on_count(
    cms_db: psycopg.Connection, tmp_path: Path,
) -> None:
    """Loading the same year twice leaves the row count unchanged."""
    fetch = _write_synth_cms(tmp_path, _FOUR_ROWS)
    parsed = parse_cms_provider_csv(fetch)
    load_to_postgres(parsed, cms_db)
    cms_db.commit()
    load_to_postgres(parsed, cms_db)
    cms_db.commit()

    with cms_db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM raw.cms_physician_provider")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 4


@pytest.mark.live_pg
def test_reload_edited_year_replaces_rows(
    cms_db: psycopg.Connection, tmp_path: Path,
) -> None:
    """DELETE-then-insert: re-loading an edited file replaces, not accumulates."""
    fetch = _write_synth_cms(tmp_path, _FOUR_ROWS)
    load_to_postgres(parse_cms_provider_csv(fetch), cms_db)
    cms_db.commit()

    # Edit one provider's payment for the same year, drop one row.
    edited = [dict(r) for r in _FOUR_ROWS[:3]]
    edited[0]["Tot_Mdcr_Pymt_Amt"] = "999999.99"
    second = _write_synth_cms(tmp_path / "second", edited, name="edited.csv")
    load_to_postgres(parse_cms_provider_csv(second), cms_db)
    cms_db.commit()

    with cms_db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM raw.cms_physician_provider")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 3  # replaced, not 4 + 3

        cur.execute(
            "SELECT tot_mdcr_pymt_amt FROM raw.cms_physician_provider "
            "WHERE npi = '1234567890'",
        )
        row = cur.fetchone()
        assert row is not None
        assert float(row[0]) == pytest.approx(999999.99)
