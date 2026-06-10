"""Tests for the CMS Medicare Part D Prescribers - by Provider ingester.

Test taxonomy
-------------
1. Pure helpers (no network, no DB)
   - _normalize_title whitespace/case behavior
   - _validate_year range guard

2. Pure parser (no DB)
   - parse_partd_csv on a synthetic CSV with the REAL CMS header names
     plus extra columns (subset-by-name selection); asserts column
     mapping, blank numeric -> None, NPI preserved as a 10-char string
     (leading zero intact), and a suppressed opioid cell -> None.
   - Missing/renamed column -> IngestError.
   - Blank-NPI rows dropped; all-blank-NPI file -> IngestError.
   - Non-numeric value in a numeric column -> IngestError.

3. Catalog resolution (no network -- injected fake client)
   - resolve_download_url selects the right CSV downloadURL for a year,
     excludes the sibling "... by Provider and Drug" dataset, and skips
     non-CSV distributions.

4. Integration (live_pg)
   - Apply all migrations + seeds; load a synthetic parsed frame into
     raw.cms_partd_prescriber; assert row count, that a blank opioid cell
     is SQL NULL, and that tot_drug_cst parsed to the exact NUMERIC.
   NOTE: migration 100_raw_cms_partd_prescriber.sql is not in the tree
   yet; the live test errors until it lands (acceptable for now).
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from ingestion._base import IngestError
from ingestion.cms_medicare import (
    CMS_COLUMN_MAP,
    FetchResult,
    _normalize_title,
    _validate_year,
    load_to_postgres,
    parse_partd_csv,
    resolve_download_url,
)

if TYPE_CHECKING:
    from pathlib import Path

    import psycopg


# ============================================================================
# 1. Pure helpers
# ============================================================================


def test_normalize_title_collapses_whitespace_and_lowercases() -> None:
    assert _normalize_title("  Medicare   Part D  ") == "medicare part d"
    assert _normalize_title("Foo\tBar") == "foo bar"


def test_validate_year_accepts_supported_range() -> None:
    _validate_year(2013)
    _validate_year(2023)


def test_validate_year_rejects_out_of_range() -> None:
    for bad in (2012, 2100, 1999):
        with pytest.raises(IngestError, match="out of supported range"):
            _validate_year(bad)


# ============================================================================
# 2. Pure parser (no DB)
# ============================================================================

# Real CMS header names for the slice we ingest, plus two extra columns
# the published file actually carries (to prove subset-by-name selection
# ignores them). Order intentionally NOT the same as CMS_COLUMN_MAP.
_SYNTH_HEADER: tuple[str, ...] = (
    "Prscrbr_NPI",
    "Prscrbr_Last_Org_Name",
    "Prscrbr_First_Name",
    "Prscrbr_City",
    "Prscrbr_State_Abrvtn",
    "Prscrbr_Type",
    "Prscrbr_RUCA",            # extra column -> must be ignored
    "Tot_Clms",
    "Tot_Drug_Cst",
    "Tot_Benes",
    "Opioid_Tot_Clms",
    "Opioid_Prscrbr_Rate",
    "Antbtc_Tot_Clms",         # extra column -> must be ignored
)


def _synth_csv(rows: list[dict[str, str]]) -> str:
    """Render dict rows as a CSV with the synthetic header above."""
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(_SYNTH_HEADER)
    for r in rows:
        writer.writerow([r.get(c, "") for c in _SYNTH_HEADER])
    return buf.getvalue()


def _write_synth(tmp_path: Path, rows: list[dict[str, str]]) -> FetchResult:
    """Write a synthetic CMS CSV and wrap it in a FetchResult."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "partd_synth.csv"
    path.write_text(_synth_csv(rows), encoding="utf-8")
    return FetchResult(
        path=path,
        source_url="https://data.cms.gov/synthetic/partd_cy2023.csv",
        source_sha256="0" * 64,
        source_vintage="CY2023",
        n_bytes=path.stat().st_size,
        cache_hit=False,
    )


_THREE_ROWS: list[dict[str, str]] = [
    {  # individual prescriber, fully populated incl. opioid fields
        "Prscrbr_NPI": "0123456789",  # leading zero must survive
        "Prscrbr_Last_Org_Name": "DOE",
        "Prscrbr_First_Name": "JANE",
        "Prscrbr_City": "TRENTON",
        "Prscrbr_State_Abrvtn": "NJ",
        "Prscrbr_Type": "Internal Medicine",
        "Tot_Clms": "1234",
        "Tot_Drug_Cst": "98765.43",
        "Tot_Benes": "210",
        "Opioid_Tot_Clms": "42",
        "Opioid_Prscrbr_Rate": "3.40",
    },
    {  # opioid fields BLANK (non-prescriber / suppressed) -> must be NULL
        "Prscrbr_NPI": "9876543210",
        "Prscrbr_Last_Org_Name": "ROE",
        "Prscrbr_First_Name": "JOHN",
        "Prscrbr_City": "NEWARK",
        "Prscrbr_State_Abrvtn": "NJ",
        "Prscrbr_Type": "Nurse Practitioner",
        "Tot_Clms": "55",
        "Tot_Drug_Cst": "1200.00",
        "Tot_Benes": "",            # suppressed (<11 benes) -> NULL
        "Opioid_Tot_Clms": "",       # blank -> NULL
        "Opioid_Prscrbr_Rate": "",   # blank -> NULL
    },
    {  # organizational prescriber (no first name)
        "Prscrbr_NPI": "5555555555",
        "Prscrbr_Last_Org_Name": "ACME HOME HEALTH LLC",
        "Prscrbr_First_Name": "",
        "Prscrbr_City": "JERSEY CITY",
        "Prscrbr_State_Abrvtn": "NJ",
        "Prscrbr_Type": "Pharmacy",
        "Tot_Clms": "9000",
        "Tot_Drug_Cst": "500000.5",
        "Tot_Benes": "1500",
        "Opioid_Tot_Clms": "100",
        "Opioid_Prscrbr_Rate": "1.11",
    },
]


def test_parse_maps_columns_and_preserves_npi_string(tmp_path: Path) -> None:
    fetch = _write_synth(tmp_path, _THREE_ROWS)
    parsed = parse_partd_csv(fetch, data_year=2023)

    assert parsed.n_rows == 3
    assert parsed.data_year == 2023
    # Provenance flows from the FetchResult unchanged.
    assert parsed.source_vintage == "CY2023"
    assert parsed.source_sha256 == "0" * 64

    df = parsed.dataframe
    # Columns are exactly the mapped raw names, in map order.
    assert df.columns == list(CMS_COLUMN_MAP.values())

    by_npi = {row["npi"]: row for row in df.iter_rows(named=True)}

    # Leading-zero NPI survives as a 10-char string (never cast to int).
    assert "0123456789" in by_npi
    assert by_npi["0123456789"]["npi"] == "0123456789"
    assert len(by_npi["0123456789"]["npi"]) == 10

    r0 = by_npi["0123456789"]
    assert r0["prscrbr_last_org_name"] == "DOE"
    assert r0["prscrbr_first_name"] == "JANE"
    assert r0["prscrbr_state_abrvtn"] == "NJ"
    assert r0["tot_drug_cst"] == "98765.43"
    assert r0["opioid_prscrbr_rate"] == "3.40"


def test_parse_blank_numeric_cells_become_null(tmp_path: Path) -> None:
    fetch = _write_synth(tmp_path, _THREE_ROWS)
    parsed = parse_partd_csv(fetch, data_year=2023)
    by_npi = {row["npi"]: row for row in parsed.dataframe.iter_rows(named=True)}

    roe = by_npi["9876543210"]
    # A blank opioid cell is "no data", not zero.
    assert roe["opioid_tot_clms"] is None
    assert roe["opioid_prscrbr_rate"] is None
    assert roe["tot_benes"] is None  # suppressed <11 benes
    # Non-blank numerics still present (as exact strings).
    assert roe["tot_drug_cst"] == "1200.00"

    # The org row has an empty first name -> NULL.
    org = by_npi["5555555555"]
    assert org["prscrbr_first_name"] is None
    assert org["prscrbr_last_org_name"] == "ACME HOME HEALTH LLC"


def test_parse_rejects_missing_column(tmp_path: Path) -> None:
    """A renamed/absent mapped header must surface as IngestError."""
    bad_header = list(_SYNTH_HEADER)
    bad_header[bad_header.index("Tot_Drug_Cst")] = "Total_Drug_Cost"  # rename drift
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(bad_header)
    w.writerow(["1234567890"] + [""] * (len(bad_header) - 1))
    path = tmp_path / "drift.csv"
    path.write_text(buf.getvalue(), encoding="utf-8")
    fetch = FetchResult(
        path=path, source_url="x", source_sha256="0" * 64,
        source_vintage="CY2023", n_bytes=path.stat().st_size, cache_hit=False,
    )
    with pytest.raises(IngestError, match="missing expected columns"):
        parse_partd_csv(fetch, data_year=2023)


def test_parse_drops_blank_npi_rows(tmp_path: Path) -> None:
    rows = [dict(_THREE_ROWS[0]), {**_THREE_ROWS[1], "Prscrbr_NPI": "  "}]
    fetch = _write_synth(tmp_path, rows)
    parsed = parse_partd_csv(fetch, data_year=2023)
    assert parsed.n_rows == 1  # the blank-NPI row was dropped
    assert parsed.dataframe["npi"].to_list() == ["0123456789"]


def test_parse_rejects_all_blank_npi(tmp_path: Path) -> None:
    rows = [{**_THREE_ROWS[0], "Prscrbr_NPI": ""}]
    fetch = _write_synth(tmp_path, rows)
    with pytest.raises(IngestError, match="0 rows with an NPI"):
        parse_partd_csv(fetch, data_year=2023)


def test_parse_rejects_non_numeric_in_numeric_column(tmp_path: Path) -> None:
    rows = [{**_THREE_ROWS[0], "Tot_Drug_Cst": "$98,765.43"}]
    fetch = _write_synth(tmp_path, rows)
    with pytest.raises(IngestError, match="non-numeric"):
        parse_partd_csv(fetch, data_year=2023)


# ============================================================================
# 3. Catalog resolution (no network -- injected fake client)
# ============================================================================


class _FakeResponse:
    """Minimal stand-in for httpx.Response carrying a JSON catalog body."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


class _FakeClient:
    """Injected client whose .get returns a fixed synthetic data.json."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.requested_urls: list[str] = []

    def get(self, url: str) -> _FakeResponse:
        self.requested_urls.append(url)
        return _FakeResponse(self._payload)


def _catalog() -> dict[str, Any]:
    """A synthetic DCAT catalog with two years + a decoy sibling dataset."""
    return {
        "dataset": [
            {
                "title": "Medicare Part D Prescribers - by Provider",
                "keyword": ["medicare", "part d"],
                "temporal": "2022-01-01T00:00:00/2022-12-31T00:00:00",
                "distribution": [
                    {"mediaType": "application/zip",
                     "downloadURL": "https://data.cms.gov/zip/2022.zip"},
                    {"mediaType": "text/csv",
                     "downloadURL": "https://data.cms.gov/csv/provider_2022.csv"},
                ],
            },
            {
                "title": "Medicare Part D Prescribers - by Provider",
                "keyword": ["medicare", "part d"],
                "temporal": "2023-01-01T00:00:00/2023-12-31T00:00:00",
                "distribution": [
                    {"mediaType": "text/csv",
                     "downloadURL": "https://data.cms.gov/csv/provider_2023.csv"},
                ],
            },
            {  # decoy: sibling dataset must NOT match
                "title": "Medicare Part D Prescribers - by Provider and Drug",
                "keyword": ["medicare", "part d"],
                "temporal": "2023-01-01T00:00:00/2023-12-31T00:00:00",
                "distribution": [
                    {"mediaType": "text/csv",
                     "downloadURL": "https://data.cms.gov/csv/provider_and_drug_2023.csv"},
                ],
            },
        ],
    }


def test_resolve_download_url_selects_correct_year() -> None:
    client = _FakeClient(_catalog())
    url = resolve_download_url(data_year=2023, client=client)  # type: ignore[arg-type]
    assert url == "https://data.cms.gov/csv/provider_2023.csv"
    assert client.requested_urls == ["https://data.cms.gov/data.json"]

    url_2022 = resolve_download_url(data_year=2022, client=client)  # type: ignore[arg-type]
    assert url_2022 == "https://data.cms.gov/csv/provider_2022.csv"


def test_resolve_download_url_raises_when_no_match() -> None:
    client = _FakeClient(_catalog())
    with pytest.raises(IngestError, match="no text/csv distribution"):
        resolve_download_url(data_year=2099, client=client)  # type: ignore[arg-type]


def test_resolve_download_url_parses_real_shaped_payload() -> None:
    """Round-trip through json.dumps/loads to mimic a real HTTP body."""
    payload = json.loads(json.dumps(_catalog()))
    client = _FakeClient(payload)
    url = resolve_download_url(data_year=2023, client=client)  # type: ignore[arg-type]
    assert url.endswith("provider_2023.csv")


# ============================================================================
# 4. Integration (live_pg)
# ============================================================================
#
# Applies the full migration set and exercises the DELETE + COPY path
# against real Postgres semantics. Skipped when PG_TEST_DSN is unset
# (see tests/conftest.py). NOTE: requires migration
# 100_raw_cms_partd_prescriber.sql, which is added separately.
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
        cur.execute(
            "DO $$ "
            "DECLARE r record; "
            "BEGIN "
            "  FOR r IN SELECT viewname FROM pg_views "
            "           WHERE schemaname='public' AND viewname LIKE 'v_%%' LOOP "
            "    EXECUTE 'DROP VIEW IF EXISTS public.' || quote_ident(r.viewname) || ' CASCADE'; "
            "  END LOOP; "
            "END $$;",
        )
    conn.commit()
    apply_migrations(conn, discover(MIGRATIONS_DIR))
    apply_migrations(conn, discover(SEEDS_DIR))
    return conn


@pytest.mark.live_pg
def test_load_synthetic_partd_round_trip(
    cms_db: psycopg.Connection, tmp_path: Path,
) -> None:
    """Happy path: 3-row file lands as 3 rows; blanks are SQL NULL."""
    fetch = _write_synth(tmp_path, _THREE_ROWS)
    parsed = parse_partd_csv(fetch, data_year=2023)
    n = load_to_postgres(parsed, cms_db)
    cms_db.commit()
    assert n == 3

    with cms_db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM raw.cms_partd_prescriber")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 3

        # The suppressed/blank opioid + benes cells are SQL NULL, not 0.
        cur.execute(
            "SELECT opioid_tot_clms, opioid_prscrbr_rate, tot_benes, tot_drug_cst "
            "FROM raw.cms_partd_prescriber WHERE npi = %s",
            ("9876543210",),
        )
        roe = cur.fetchone()
        assert roe is not None
        assert roe[0] is None  # opioid_tot_clms blank -> NULL
        assert roe[1] is None  # opioid_prscrbr_rate blank -> NULL
        assert roe[2] is None  # tot_benes suppressed -> NULL
        assert roe[3] == Decimal("1200.00")  # exact NUMERIC

        # Leading-zero NPI preserved through COPY.
        cur.execute(
            "SELECT data_year, tot_drug_cst FROM raw.cms_partd_prescriber "
            "WHERE npi = %s",
            ("0123456789",),
        )
        doe = cur.fetchone()
        assert doe is not None
        assert doe[0] == 2023
        assert doe[1] == Decimal("98765.43")


@pytest.mark.live_pg
def test_load_is_idempotent_per_year(
    cms_db: psycopg.Connection, tmp_path: Path,
) -> None:
    """Re-loading the same CY DELETEs then re-COPYs: row count is stable."""
    fetch = _write_synth(tmp_path, _THREE_ROWS)
    parsed = parse_partd_csv(fetch, data_year=2023)
    load_to_postgres(parsed, cms_db)
    cms_db.commit()
    load_to_postgres(parsed, cms_db)
    cms_db.commit()

    with cms_db.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM raw.cms_partd_prescriber WHERE data_year = 2023",
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 3  # no duplicates after re-load
