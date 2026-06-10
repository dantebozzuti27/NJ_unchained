"""Tests for the NPPES NPI Registry bulk-file ingester (identity spine).

Test taxonomy
-------------
1. Pure helpers (no DB, no network)
   - _left_zip5 truncation / blank behavior
   - _coerce_entity_type accepts blank/'1'/'2', rejects others
   - _find_npidata_member: zero / one / many members
   - _vintage_from_member parses the YYYYMMDD-YYYYMMDD window
   - _resolve_state_filter: --national overrides --state-filter

2. Pure parser (no DB, synthetic CSV via tmp_path)
   - Header-name (not position) projection with decoy columns interleaved
   - Default NJ size-bound filters out non-NJ practice states
   - state_filter=None (national) keeps every row
   - Header drift (missing required column) -> IngestError
   - Malformed NPI -> IngestError
   - Bad Entity Type Code -> IngestError
   - practice_zip5 = LEFT 5 of postal; blanks preserved as '' (-> NULL)
   - Zero rows after filter -> IngestError

3. Integration (live_pg)
   - Full-replace load into raw.nppes_provider; row count + provenance
   - TRUNCATE semantics: re-load with fewer rows shrinks the table
   - State filter is honored at the DB layer

NOTE: these tests NEVER touch the network or the real ~10 GB NPPES file.
Every fixture is a tiny synthetic CSV constructed in-process.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ingestion._base import IngestError
from ingestion.nppes import (
    NPPES_SOURCE_COLUMNS,
    FetchResult,
    _coerce_entity_type,
    _find_npidata_member,
    _left_zip5,
    _resolve_state_filter,
    _vintage_from_member,
    load_to_postgres,
    parse_nppes_csv,
)

if TYPE_CHECKING:
    from pathlib import Path

    import psycopg


# ============================================================================
# 1. Pure helpers
# ============================================================================


def test_left_zip5_truncates_and_handles_blank() -> None:
    assert _left_zip5("08608-1234") == "08608"
    assert _left_zip5("085406789") == "08540"
    assert _left_zip5("08540") == "08540"
    assert _left_zip5("  07102  ") == "07102"
    assert _left_zip5("") == ""
    # We do NOT zero-pad a short code; surface it as-is.
    assert _left_zip5("8540") == "8540"


def test_coerce_entity_type_accepts_blank_and_1_2() -> None:
    assert _coerce_entity_type("1", line_no=2) == "1"
    assert _coerce_entity_type("2", line_no=2) == "2"
    assert _coerce_entity_type("", line_no=2) == ""
    assert _coerce_entity_type("   ", line_no=2) == ""


def test_coerce_entity_type_rejects_other_values() -> None:
    for bad in ("3", "0", "I", "indiv"):
        with pytest.raises(IngestError, match="Entity Type Code"):
            _coerce_entity_type(bad, line_no=7)


def test_find_npidata_member_picks_the_main_file() -> None:
    names = [
        "npidata_pfile_20260601-20260601.csv",
        "npidata_pfile_20260601-20260601_FileHeader.csv",
        "othername_pfile_20260601-20260601.csv",
        "pl_pfile_20260601-20260601.csv",
    ]
    assert _find_npidata_member(names) == "npidata_pfile_20260601-20260601.csv"


def test_find_npidata_member_rejects_zero_matches() -> None:
    with pytest.raises(IngestError, match="no npidata"):
        _find_npidata_member(["readme.txt", "endpoint_pfile_20260601-20260601.csv"])


def test_find_npidata_member_rejects_multiple_matches() -> None:
    with pytest.raises(IngestError, match="multiple npidata"):
        _find_npidata_member([
            "npidata_pfile_20260601-20260601.csv",
            "npidata_pfile_20260501-20260501.csv",
        ])


def test_vintage_from_member_parses_window() -> None:
    assert (
        _vintage_from_member("npidata_pfile_20260601-20260601.csv")
        == "20260601-20260601"
    )
    # Tolerates a leading directory component.
    assert (
        _vintage_from_member("NPPES/npidata_pfile_20050523-20260601.csv")
        == "20050523-20260601"
    )


def test_resolve_state_filter_national_overrides() -> None:
    assert _resolve_state_filter("NJ", national=False) == "NJ"
    assert _resolve_state_filter("NJ", national=True) is None
    assert _resolve_state_filter("NY", national=False) == "NY"


# ============================================================================
# 2. Pure parser (synthetic CSV)
# ============================================================================
#
# The synthetic header interleaves DECOY columns between the ten real
# target columns so the test proves projection is BY HEADER NAME, not by
# fixed position.

_COLUMN_ORDER: list[str] = [
    "NPI",
    "decoy_provider_credential",
    "Entity Type Code",
    "Provider Last Name (Legal Name)",
    "Provider First Name",
    "decoy_provider_middle_name",
    "Provider Organization Name (Legal Business Name)",
    "Provider Business Practice Location Address City Name",
    "Provider Business Practice Location Address State Name",
    "Provider Business Practice Location Address Postal Code",
    "decoy_mailing_address",
    "Healthcare Provider Taxonomy Code_1",
    "NPI Deactivation Date",
    "decoy_is_sole_proprietor",
]


def _synth_csv(columns: list[str], rows: list[dict[str, str]]) -> str:
    """Render a header + body CSV. Missing keys render as empty fields."""
    out = [",".join(_csv_field(c) for c in columns)]
    for r in rows:
        out.append(",".join(_csv_field(r.get(c, "")) for c in columns))
    return "\n".join(out) + "\n"


def _csv_field(value: str) -> str:
    """Quote a field if it contains a comma/quote/newline (NPPES quotes all)."""
    if any(ch in value for ch in (",", '"', "\n")):
        return '"' + value.replace('"', '""') + '"'
    return value


def _write_synth(
    tmp_path: Path,
    rows: list[dict[str, str]],
    *,
    columns: list[str] | None = None,
) -> FetchResult:
    """Write a synthetic npidata CSV and wrap it in a FetchResult."""
    path = tmp_path / "npidata_synth.csv"
    path.write_text(
        _synth_csv(columns or _COLUMN_ORDER, rows), encoding="utf-8",
    )
    return FetchResult(
        path=path,
        source_url="https://example.test/NPPES_Data_Dissemination_June_2026_V2.zip",
        source_sha256="0" * 64,
        source_vintage="20260601-20260601",
        n_bytes=path.stat().st_size,
        cache_hit=False,
    )


_ROWS: list[dict[str, str]] = [
    {  # NJ individual physician
        "NPI": "1234567890",
        "Entity Type Code": "1",
        "Provider Last Name (Legal Name)": "DOE",
        "Provider First Name": "JANE",
        "Provider Business Practice Location Address City Name": "TRENTON",
        "Provider Business Practice Location Address State Name": "NJ",
        "Provider Business Practice Location Address Postal Code": "08608-1234",
        "Healthcare Provider Taxonomy Code_1": "207Q00000X",
    },
    {  # NJ organization
        "NPI": "2233445566",
        "Entity Type Code": "2",
        "Provider Organization Name (Legal Business Name)": "ACME HOME HEALTH, LLC",
        "Provider Business Practice Location Address City Name": "NEWARK",
        "Provider Business Practice Location Address State Name": "NJ",
        "Provider Business Practice Location Address Postal Code": "07102",
        "Healthcare Provider Taxonomy Code_1": "251E00000X",
    },
    {  # NY individual -- filtered out by the default NJ bound
        "NPI": "9876543210",
        "Entity Type Code": "1",
        "Provider Last Name (Legal Name)": "ROE",
        "Provider First Name": "JOHN",
        "Provider Business Practice Location Address City Name": "NEW YORK",
        "Provider Business Practice Location Address State Name": "NY",
        "Provider Business Practice Location Address Postal Code": "10001",
        "Healthcare Provider Taxonomy Code_1": "163W00000X",
    },
    {  # NJ deactivated NPI: blank entity type, blank zip, deactivation date
        "NPI": "5555555555",
        "Entity Type Code": "",
        "Provider Business Practice Location Address State Name": "NJ",
        "Provider Business Practice Location Address Postal Code": "",
        "NPI Deactivation Date": "2023-05-01",
    },
    {  # CA organization -- filtered out by the default NJ bound
        "NPI": "6677889900",
        "Entity Type Code": "2",
        "Provider Organization Name (Legal Business Name)": "WEST CLINIC INC",
        "Provider Business Practice Location Address State Name": "CA",
        "Provider Business Practice Location Address Postal Code": "90001",
        "Healthcare Provider Taxonomy Code_1": "261Q00000X",
    },
]


def test_parse_nj_filter_keeps_only_nj_and_projects_by_name(tmp_path: Path) -> None:
    fetch = _write_synth(tmp_path, _ROWS)
    parsed = parse_nppes_csv(fetch)  # default state_filter='NJ'

    assert parsed.state_filter == "NJ"
    assert parsed.n_rows == 3  # DOE, ACME, deactivated 5555555555

    by_npi = {row[0]: row for row in parsed.rows}
    assert set(by_npi) == {"1234567890", "2233445566", "5555555555"}

    # Tuple order is _RAW_PAYLOAD_COLUMNS:
    # (npi, entity_type, last, first, org, city, state, zip5, taxonomy, deact)
    doe = by_npi["1234567890"]
    assert doe == (
        "1234567890", "1", "DOE", "JANE", "",
        "TRENTON", "NJ", "08608", "207Q00000X", "",
    )

    org = by_npi["2233445566"]
    assert org[1] == "2"
    assert org[4] == "ACME HOME HEALTH, LLC"  # comma survives CSV round-trip
    assert org[2] == "" and org[3] == ""  # no person name on an org

    deact = by_npi["5555555555"]
    assert deact[1] == ""          # blank entity type -> '' (-> NULL at COPY)
    assert deact[7] == ""          # blank postal -> '' zip5
    assert deact[9] == "2023-05-01"  # raw deactivation date text


def test_parse_national_keeps_every_row(tmp_path: Path) -> None:
    fetch = _write_synth(tmp_path, _ROWS)
    parsed = parse_nppes_csv(fetch, state_filter=None)
    assert parsed.state_filter is None
    assert parsed.n_rows == 5
    assert {row[6] for row in parsed.rows} == {"NJ", "NY", "CA"}


def test_parse_carries_provenance_through(tmp_path: Path) -> None:
    fetch = _write_synth(tmp_path, _ROWS)
    parsed = parse_nppes_csv(fetch)
    assert parsed.source_url.endswith("_V2.zip")
    assert parsed.source_sha256 == "0" * 64
    assert parsed.source_vintage == "20260601-20260601"


def test_parse_rejects_header_drift_missing_required_column(tmp_path: Path) -> None:
    cols = [c for c in _COLUMN_ORDER if c != "Entity Type Code"]
    fetch = _write_synth(tmp_path, _ROWS, columns=cols)
    with pytest.raises(IngestError, match="schema drift"):
        parse_nppes_csv(fetch, state_filter=None)


def test_parse_rejects_malformed_npi(tmp_path: Path) -> None:
    rows = [{
        "NPI": "12345",  # not 10 digits
        "Entity Type Code": "1",
        "Provider Last Name (Legal Name)": "SHORT",
        "Provider Business Practice Location Address State Name": "NJ",
    }]
    fetch = _write_synth(tmp_path, rows)
    with pytest.raises(IngestError, match="not 10 digits"):
        parse_nppes_csv(fetch)


def test_parse_rejects_bad_entity_type(tmp_path: Path) -> None:
    rows = [{
        "NPI": "1112223334",
        "Entity Type Code": "3",  # not blank/1/2
        "Provider Last Name (Legal Name)": "ODD",
        "Provider Business Practice Location Address State Name": "NJ",
    }]
    fetch = _write_synth(tmp_path, rows)
    with pytest.raises(IngestError, match="Entity Type Code"):
        parse_nppes_csv(fetch)


def test_parse_rejects_zero_rows_after_filter(tmp_path: Path) -> None:
    """A filter that matches nothing is an empty snapshot -> refuse."""
    rows = [r for r in _ROWS if r.get(
        "Provider Business Practice Location Address State Name") != "NJ"]
    fetch = _write_synth(tmp_path, rows)
    with pytest.raises(IngestError, match="parsed 0 rows"):
        parse_nppes_csv(fetch, state_filter="NJ")


def test_parse_case_insensitive_state_filter(tmp_path: Path) -> None:
    rows = [{
        "NPI": "1234567890",
        "Entity Type Code": "1",
        "Provider Last Name (Legal Name)": "DOE",
        "Provider Business Practice Location Address State Name": "nj",  # lower
        "Provider Business Practice Location Address Postal Code": "08608",
    }]
    fetch = _write_synth(tmp_path, rows)
    parsed = parse_nppes_csv(fetch, state_filter="NJ")
    assert parsed.n_rows == 1
    assert parsed.rows[0][6] == "nj"  # raw value preserved verbatim


# ============================================================================
# 3. Integration (live_pg)
# ============================================================================
#
# These apply the full migration set and exercise TRUNCATE + COPY against
# real Postgres. Skipped when PG_TEST_DSN is not set (see conftest.py) and
# deselectable with -m "not live_pg".
# ============================================================================


@pytest.fixture
def nppes_db(live_pg: psycopg.Connection) -> psycopg.Connection:
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
def test_load_full_replace_round_trip(
    nppes_db: psycopg.Connection, tmp_path: Path,
) -> None:
    """NJ-filtered 3-row snapshot lands as 3 rows with provenance."""
    fetch = _write_synth(tmp_path, _ROWS)
    parsed = parse_nppes_csv(fetch)
    n = load_to_postgres(parsed, nppes_db)
    nppes_db.commit()
    assert n == 3

    with nppes_db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM raw.nppes_provider")
        assert (cur.fetchone() or (None,))[0] == 3

        # entity_type_code cast to SMALLINT; blank entity type -> NULL.
        cur.execute(
            "SELECT npi, entity_type_code, practice_zip5, deactivation_date "
            "FROM raw.nppes_provider ORDER BY npi",
        )
        rows = {r[0]: r for r in cur.fetchall()}
        assert rows["1234567890"][1] == 1
        assert rows["1234567890"][2] == "08608"
        assert rows["2233445566"][1] == 2
        assert rows["5555555555"][1] is None   # blank -> NULL
        assert rows["5555555555"][2] is None   # blank zip -> NULL
        assert rows["5555555555"][3] == "2023-05-01"

        # Provenance present and uniform.
        cur.execute(
            "SELECT DISTINCT source_url, source_sha256, source_vintage "
            "FROM raw.nppes_provider",
        )
        prov = cur.fetchall()
        assert len(prov) == 1
        assert prov[0][2] == "20260601-20260601"
        assert len(prov[0][1]) == 64


@pytest.mark.live_pg
def test_load_truncates_prior_snapshot(
    nppes_db: psycopg.Connection, tmp_path: Path,
) -> None:
    """A second load full-replaces: a smaller pull shrinks the table."""
    first = parse_nppes_csv(_write_synth(tmp_path, _ROWS))  # 3 NJ rows
    load_to_postgres(first, nppes_db)
    nppes_db.commit()

    smaller = parse_nppes_csv(_write_synth(tmp_path, _ROWS[:1]))  # 1 NJ row
    n = load_to_postgres(smaller, nppes_db)
    nppes_db.commit()
    assert n == 1

    with nppes_db.cursor() as cur:
        cur.execute("SELECT npi FROM raw.nppes_provider")
        npis = [r[0] for r in cur.fetchall()]
    assert npis == ["1234567890"]


@pytest.mark.live_pg
def test_load_national_keeps_all_states(
    nppes_db: psycopg.Connection, tmp_path: Path,
) -> None:
    fetch = _write_synth(tmp_path, _ROWS)
    parsed = parse_nppes_csv(fetch, state_filter=None)
    load_to_postgres(parsed, nppes_db)
    nppes_db.commit()

    with nppes_db.cursor() as cur:
        cur.execute("SELECT DISTINCT practice_state FROM raw.nppes_provider")
        states = {r[0] for r in cur.fetchall()}
    assert states == {"NJ", "NY", "CA"}


def test_source_columns_are_the_documented_ten() -> None:
    """Guard the projection contract: exactly ten source columns, in order."""
    assert NPPES_SOURCE_COLUMNS == (
        "NPI",
        "Entity Type Code",
        "Provider Last Name (Legal Name)",
        "Provider First Name",
        "Provider Organization Name (Legal Business Name)",
        "Provider Business Practice Location Address City Name",
        "Provider Business Practice Location Address State Name",
        "Provider Business Practice Location Address Postal Code",
        "Healthcare Provider Taxonomy Code_1",
        "NPI Deactivation Date",
    )
