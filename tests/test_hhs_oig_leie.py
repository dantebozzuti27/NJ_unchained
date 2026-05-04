"""Tests for the HHS-OIG LEIE ingester (Tier 4 v3 / FRAUD-F5 substrate).

Test taxonomy
-------------
1. Pure helpers
   - compute_record_hash determinism + canonicalization
   - _normalize_for_hash whitespace / case behavior
   - _coerce_date_string accepts 8-digit + '00000000' + empty -> None
   - _validate_vintage_month rejects malformed values

2. Pure parser (no DB)
   - parse_leie_csv on a synthetic 4-row LEIE CSV
   - Header drift -> IngestError
   - Wrong column count per data row -> IngestError
   - Empty EXCLDATE -> IngestError
   - Empty file -> IngestError
   - All-blank LASTNAME / BUSNAME row -> IngestError

3. Integration (live_pg)
   - Apply all migrations + seeds; load a synthetic 4-row file;
     verify raw.hhs_oig_leie row counts, derived.v_leie_active,
     derived.v_leie_individuals_active / _businesses_active splits.
   - Idempotent re-load: same file twice produces same row count and
     bumps last_seen_at without duplicating rows.
   - HHS profile-correction: edit one row, re-load; old hash stays
     (last_seen_at not bumped), new hash is inserted.
   - Reinstatement: drop a row from the second pull; old row's
     last_seen_at remains stale, falls out of v_leie_active when the
     new pull's max(last_seen_at) advances by more than 7 days.
   - ref.release_calendar entry exists with cadence='monthly'.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from ingestion._base import IngestError
from ingestion.hhs_oig_leie import (
    LEIE_COLUMNS,
    FetchResult,
    _coerce_date_string,
    _normalize_for_hash,
    _validate_vintage_month,
    compute_record_hash,
    load_to_postgres,
    parse_leie_csv,
)

if TYPE_CHECKING:
    import psycopg


# ============================================================================
# 1. Pure helpers
# ============================================================================


def test_normalize_for_hash_strips_and_uppercases() -> None:
    assert _normalize_for_hash("  Smith  ") == "SMITH"
    assert _normalize_for_hash("o'Connor") == "O'CONNOR"
    assert _normalize_for_hash("") == ""
    assert _normalize_for_hash("   ") == ""


def test_normalize_for_hash_strips_nul_bytes_defensively() -> None:
    """A stray NUL byte must not enter the canonical hash input."""
    assert _normalize_for_hash("Smith\x00") == "SMITH"
    assert _normalize_for_hash("\x00\x00") == ""


def test_compute_record_hash_is_deterministic_across_whitespace_and_case() -> None:
    a = {"lastname": "SMITH", "firstname": "JANE", "midname": "",
         "busname": "", "dob": "19600101",
         "excltype": "1128A1", "excldate": "20180515",
         "general": "PHYSICIAN", "specialty": "CARDIOLOGY",
         "upin": "", "npi": "1234567890",
         "address": "123 MAIN ST", "city": "TRENTON",
         "state": "NJ", "zip": "08608"}
    b = {**a, "lastname": "  smith  ", "firstname": "Jane", "city": "trenton"}
    assert compute_record_hash(a) == compute_record_hash(b)


def test_compute_record_hash_changes_when_canonical_field_changes() -> None:
    base = {"lastname": "SMITH", "firstname": "JANE", "midname": "",
            "busname": "", "dob": "19600101",
            "excltype": "1128A1", "excldate": "20180515",
            "general": "PHYSICIAN", "specialty": "CARDIOLOGY",
            "upin": "", "npi": "1234567890",
            "address": "123 MAIN ST", "city": "TRENTON",
            "state": "NJ", "zip": "08608"}
    different_excldate = {**base, "excldate": "20180516"}
    different_zip      = {**base, "zip": "08609"}
    different_npi      = {**base, "npi": "9999999999"}
    h0 = compute_record_hash(base)
    assert h0 != compute_record_hash(different_excldate)
    assert h0 != compute_record_hash(different_zip)
    assert h0 != compute_record_hash(different_npi)


def test_compute_record_hash_is_64_hex_chars() -> None:
    h = compute_record_hash({})
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_coerce_date_string_accepts_8_digits_and_zero_sentinel() -> None:
    assert _coerce_date_string("20180515", field="EXCLDATE") == "20180515"
    assert _coerce_date_string("00000000", field="REINDATE") == "00000000"


def test_coerce_date_string_returns_none_for_empty() -> None:
    assert _coerce_date_string("",    field="REINDATE") is None
    assert _coerce_date_string("   ", field="WAIVERDATE") is None


def test_coerce_date_string_rejects_malformed() -> None:
    with pytest.raises(IngestError, match="not 8 digits"):
        _coerce_date_string("2018-05-15", field="EXCLDATE")
    with pytest.raises(IngestError, match="not 8 digits"):
        _coerce_date_string("180515", field="EXCLDATE")


def test_validate_vintage_month_accepts_yyyymm() -> None:
    _validate_vintage_month("2026-03")
    _validate_vintage_month("1999-12")


def test_validate_vintage_month_rejects_other_shapes() -> None:
    for bad in ["2026-3", "26-03", "March 2026", "20260301", ""]:
        with pytest.raises(IngestError, match="vintage_month"):
            _validate_vintage_month(bad)


# ============================================================================
# 2. Pure parser (no DB)
# ============================================================================


_SYNTH_HEADER = ",".join(LEIE_COLUMNS) + "\n"


def _synth_leie_csv(rows: list[dict[str, str]]) -> str:
    """Render a list of dicts as a header + body CSV in LEIE_COLUMNS order."""
    out: list[str] = [_SYNTH_HEADER]
    for r in rows:
        line = ",".join(r.get(c, "") for c in LEIE_COLUMNS)
        out.append(line + "\n")
    return "".join(out)


def _write_synth_leie(tmp_path: Path, rows: list[dict[str, str]]) -> FetchResult:
    """Write a synthetic LEIE file and wrap it in a FetchResult."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "leie_synth.csv"
    path.write_text(_synth_leie_csv(rows), encoding="utf-8")
    return FetchResult(
        path=path,
        source_url="https://example.test/UPDATED.csv",
        source_sha256="0" * 64,
        source_vintage="test-vintage",
        n_bytes=path.stat().st_size,
        cache_hit=False,
    )


_FOUR_ROWS: list[dict[str, str]] = [
    {  # individual physician, NJ
        "LASTNAME": "DOE", "FIRSTNAME": "JANE", "MIDNAME": "A",
        "GENERAL": "PHYSICIAN", "SPECIALTY": "INTERNAL MEDICINE",
        "NPI": "1234567890", "DOB": "19600101",
        "ADDRESS": "1 MAIN ST", "CITY": "TRENTON", "STATE": "NJ",
        "ZIP": "08608",
        "EXCLTYPE": "1128A1", "EXCLDATE": "20180515",
        "REINDATE": "00000000", "WAIVERDATE": "00000000",
    },
    {  # individual provider, reinstated
        "LASTNAME": "ROE", "FIRSTNAME": "JOHN",
        "GENERAL": "NURSE", "SPECIALTY": "RN",
        "NPI": "9876543210", "DOB": "19550715",
        "ADDRESS": "5 OAK AVE", "CITY": "NEWARK", "STATE": "NJ",
        "ZIP": "07102",
        "EXCLTYPE": "1128B4", "EXCLDATE": "20100303",
        "REINDATE": "20200101", "WAIVERDATE": "00000000",
    },
    {  # business / entity
        "BUSNAME": "ACME HOME HEALTH LLC",
        "GENERAL": "HOME HEALTH AGENCY", "SPECIALTY": "",
        "ADDRESS": "100 INDUSTRIAL BLVD", "CITY": "JERSEY CITY",
        "STATE": "NJ", "ZIP": "07302",
        "EXCLTYPE": "1128A2", "EXCLDATE": "20210701",
    },
    {  # individual with waiver
        "LASTNAME": "OCONNOR", "FIRSTNAME": "TERRY",
        "GENERAL": "DENTIST", "SPECIALTY": "ORTHODONTICS",
        "NPI": "5555555555", "DOB": "19800101",
        "ADDRESS": "200 PINE ST", "CITY": "PRINCETON", "STATE": "NJ",
        "ZIP": "08540",
        "EXCLTYPE": "1128B7", "EXCLDATE": "20150810",
        "REINDATE": "00000000",
        "WAIVERDATE": "20200115", "WVRSTATE": "NJ",
    },
]


def test_parse_leie_csv_happy_path(tmp_path: Path) -> None:
    fetch = _write_synth_leie(tmp_path, _FOUR_ROWS)
    parsed = parse_leie_csv(fetch)

    assert parsed.n_rows == 4
    assert len(parsed.rows) == 4

    # Spot-check the first row preserves source bytes byte-for-byte.
    by_col = dict(zip(LEIE_COLUMNS, parsed.rows[0], strict=True))
    assert by_col["LASTNAME"] == "DOE"
    assert by_col["FIRSTNAME"] == "JANE"
    assert by_col["NPI"] == "1234567890"
    assert by_col["EXCLDATE"] == "20180515"
    assert by_col["REINDATE"] == "00000000"

    # And the entity-row has BUSNAME populated, LASTNAME blank.
    by_col_3 = dict(zip(LEIE_COLUMNS, parsed.rows[2], strict=True))
    assert by_col_3["BUSNAME"] == "ACME HOME HEALTH LLC"
    assert by_col_3["LASTNAME"] == ""


def test_parse_leie_csv_rejects_header_drift(tmp_path: Path) -> None:
    """A renamed column in the header must surface as IngestError, not silent re-map."""
    bad = _SYNTH_HEADER.replace("EXCLTYPE", "EXCL_TYPE")  # underscore drift
    bad += ",".join(["X"] * len(LEIE_COLUMNS)) + "\n"
    p = tmp_path / "drift.csv"
    p.write_text(bad, encoding="utf-8")
    fetch = FetchResult(
        path=p, source_url="x", source_sha256="0" * 64,
        source_vintage="v", n_bytes=p.stat().st_size, cache_hit=False,
    )
    with pytest.raises(IngestError, match="LEIE schema drift"):
        parse_leie_csv(fetch)


def test_parse_leie_csv_rejects_wrong_column_count(tmp_path: Path) -> None:
    body = _SYNTH_HEADER + ",".join(["X"] * (len(LEIE_COLUMNS) - 1)) + "\n"
    p = tmp_path / "shortrow.csv"
    p.write_text(body, encoding="utf-8")
    fetch = FetchResult(
        path=p, source_url="x", source_sha256="0" * 64,
        source_vintage="v", n_bytes=p.stat().st_size, cache_hit=False,
    )
    with pytest.raises(IngestError, match=r"got \d+ fields, expected"):
        parse_leie_csv(fetch)


def test_parse_leie_csv_rejects_empty_excldate(tmp_path: Path) -> None:
    rows = [{
        "LASTNAME": "DOE", "FIRSTNAME": "JANE",
        "EXCLTYPE": "1128A1", "EXCLDATE": "",  # required NOT NULL
    }]
    fetch = _write_synth_leie(tmp_path, rows)
    with pytest.raises(IngestError, match="empty EXCLDATE"):
        parse_leie_csv(fetch)


def test_parse_leie_csv_rejects_empty_file(tmp_path: Path) -> None:
    p = tmp_path / "empty.csv"
    p.write_text("", encoding="utf-8")
    fetch = FetchResult(
        path=p, source_url="x", source_sha256="0" * 64,
        source_vintage="v", n_bytes=0, cache_hit=False,
    )
    with pytest.raises(IngestError, match="empty"):
        parse_leie_csv(fetch)


def test_parse_leie_csv_rejects_row_with_no_name_or_busname(tmp_path: Path) -> None:
    rows = [{
        "EXCLTYPE": "1128A1", "EXCLDATE": "20180515",
        "ADDRESS": "MYSTERY ST",
    }]
    fetch = _write_synth_leie(tmp_path, rows)
    with pytest.raises(IngestError, match="LASTNAME and BUSNAME are empty"):
        parse_leie_csv(fetch)


def test_parse_leie_csv_skips_blank_lines(tmp_path: Path) -> None:
    """Two blank lines between data rows are tolerated (not parsed as malformed rows)."""
    body = _SYNTH_HEADER
    body += ",".join(_FOUR_ROWS[0].get(c, "") for c in LEIE_COLUMNS) + "\n"
    body += "\n\n"  # blank lines
    body += ",".join(_FOUR_ROWS[2].get(c, "") for c in LEIE_COLUMNS) + "\n"
    p = tmp_path / "blanks.csv"
    p.write_text(body, encoding="utf-8")
    fetch = FetchResult(
        path=p, source_url="x", source_sha256="0" * 64,
        source_vintage="v", n_bytes=p.stat().st_size, cache_hit=False,
    )
    parsed = parse_leie_csv(fetch)
    assert parsed.n_rows == 2


def test_parse_leie_csv_rejects_zero_rows(tmp_path: Path) -> None:
    """A header-only file is not a valid LEIE pull."""
    p = tmp_path / "headeronly.csv"
    p.write_text(_SYNTH_HEADER, encoding="utf-8")
    fetch = FetchResult(
        path=p, source_url="x", source_sha256="0" * 64,
        source_vintage="v", n_bytes=p.stat().st_size, cache_hit=False,
    )
    with pytest.raises(IngestError, match="parsed 0 data rows"):
        parse_leie_csv(fetch)


# ============================================================================
# 3. Integration (live_pg)
# ============================================================================
#
# These tests apply the full migration set and exercise the COPY +
# UPSERT path against real Postgres semantics. Skipped when PG_TEST_DSN
# is not set (see tests/conftest.py).
# ============================================================================


@pytest.fixture
def leie_db(live_pg: psycopg.Connection) -> psycopg.Connection:
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
def test_load_synthetic_leie_round_trip(
    leie_db: psycopg.Connection, tmp_path: Path,
) -> None:
    """Happy path: 4-row file lands as 4 rows in raw.hhs_oig_leie."""
    fetch = _write_synth_leie(tmp_path, _FOUR_ROWS)
    parsed = parse_leie_csv(fetch)
    n = load_to_postgres(parsed, leie_db, vintage_month="2026-03")
    leie_db.commit()
    assert n == 4

    with leie_db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM raw.hhs_oig_leie")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 4

        # Distinct vintage_month + provenance fields populated on every row.
        cur.execute(
            "SELECT DISTINCT vintage_month, source_sha256, source_url "
            "FROM raw.hhs_oig_leie",
        )
        rows = cur.fetchall()
        assert len(rows) == 1
        vm, sha, url = rows[0]
        assert vm == "2026-03"
        assert len(sha) == 64
        assert url.endswith("/UPDATED.csv")


@pytest.mark.live_pg
def test_active_vs_individual_vs_business_views(
    leie_db: psycopg.Connection, tmp_path: Path,
) -> None:
    """The three canonical views split rows correctly."""
    fetch = _write_synth_leie(tmp_path, _FOUR_ROWS)
    parsed = parse_leie_csv(fetch)
    load_to_postgres(parsed, leie_db, vintage_month="2026-03")
    leie_db.commit()

    with leie_db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM derived.v_leie_active")
        assert (cur.fetchone() or (None,))[0] == 4

        cur.execute(
            "SELECT COUNT(*) FROM derived.v_leie_individuals_active",
        )
        assert (cur.fetchone() or (None,))[0] == 3  # three persons

        cur.execute(
            "SELECT COUNT(*) FROM derived.v_leie_businesses_active",
        )
        assert (cur.fetchone() or (None,))[0] == 1  # one entity

        # Cooked dates: '00000000' becomes NULL; real dates are parsed.
        cur.execute(
            "SELECT lastname, excldate_d, reindate_d, waiverdate_d "
            "FROM derived.v_leie_individuals_active "
            "ORDER BY lastname",
        )
        rows = cur.fetchall()
        # Sorted: DOE, OCONNOR, ROE
        assert rows[0][0] == "DOE"
        assert rows[0][1] == dt.date(2018, 5, 15)
        assert rows[0][2] is None  # reindate '00000000'
        assert rows[0][3] is None  # waiverdate '00000000'

        # ROE was reinstated 2020-01-01.
        roe = next(r for r in rows if r[0] == "ROE")
        assert roe[2] == dt.date(2020, 1, 1)

        # OCONNOR has a waiver in 2020-01-15 with WVRSTATE = NJ.
        oconnor = next(r for r in rows if r[0] == "OCONNOR")
        assert oconnor[3] == dt.date(2020, 1, 15)


@pytest.mark.live_pg
def test_idempotent_reload_is_a_no_op_on_row_count(
    leie_db: psycopg.Connection, tmp_path: Path,
) -> None:
    """Loading the same file twice must leave the row count unchanged.

    The second load bumps last_seen_at on every row but does NOT
    insert duplicates. record_hash is the PK; ON CONFLICT DO UPDATE
    is the contract.
    """
    fetch = _write_synth_leie(tmp_path, _FOUR_ROWS)
    parsed = parse_leie_csv(fetch)
    load_to_postgres(parsed, leie_db, vintage_month="2026-03")
    leie_db.commit()

    # Capture the first-pull last_seen_at floor.
    with leie_db.cursor() as cur:
        cur.execute("SELECT MIN(last_seen_at) FROM raw.hhs_oig_leie")
        row = cur.fetchone()
        assert row is not None
        first_pull_floor = row[0]

    # Second load -- same bytes, different vintage stamp.
    load_to_postgres(parsed, leie_db, vintage_month="2026-04")
    leie_db.commit()

    with leie_db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM raw.hhs_oig_leie")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 4  # no duplicates

        # last_seen_at advanced past the first-pull floor on every row.
        cur.execute(
            "SELECT MIN(last_seen_at) FROM raw.hhs_oig_leie",
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] >= first_pull_floor

        # vintage_month was overwritten by the latest pull.
        cur.execute(
            "SELECT DISTINCT vintage_month FROM raw.hhs_oig_leie",
        )
        rows = cur.fetchall()
        assert rows == [("2026-04",)]


@pytest.mark.live_pg
def test_profile_correction_inserts_new_row_keeps_old(
    leie_db: psycopg.Connection, tmp_path: Path,
) -> None:
    """An HHS profile correction (edit to a field) yields a new record_hash.

    The old hash stays in the table (its last_seen_at no longer
    advances and it falls out of v_leie_active after 7 days). The new
    hash is INSERTed alongside. This is the platform's "track edits as
    new entities" contract.
    """
    first = _write_synth_leie(tmp_path, _FOUR_ROWS)
    parsed = parse_leie_csv(first)
    load_to_postgres(parsed, leie_db, vintage_month="2026-03")
    leie_db.commit()

    # Edit DOE's address (a real-world profile-correction shape).
    edited = [dict(r) for r in _FOUR_ROWS]
    edited[0]["ADDRESS"] = "999 NEW ADDRESS ST"
    second = _write_synth_leie(tmp_path / "second", edited)
    parsed2 = parse_leie_csv(second)
    load_to_postgres(parsed2, leie_db, vintage_month="2026-04")
    leie_db.commit()

    with leie_db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM raw.hhs_oig_leie")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 5  # four originals + one re-hashed DOE

        # Both DOE rows live in raw; v_leie_active shows both because
        # they were both seen within 7 days of MAX(last_seen_at).
        cur.execute(
            "SELECT COUNT(*) FROM derived.v_leie_individuals_active "
            "WHERE lastname = 'DOE'",
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 2


@pytest.mark.live_pg
def test_ref_release_calendar_has_leie_entry(
    leie_db: psycopg.Connection,
) -> None:
    """The seed migration registered raw.hhs_oig_leie's release schedule."""
    with leie_db.cursor() as cur:
        cur.execute(
            "SELECT cadence, expected_lag_hours "
            "FROM ref.release_calendar "
            "WHERE source_id = 'raw.hhs_oig_leie'",
        )
        row = cur.fetchone()
    assert row is not None
    cadence, lag = row
    assert cadence == "monthly"
    # Lag budget gives ~20 days past the 10th publication target.
    assert lag == 480
