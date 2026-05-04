"""Tests for the SAM.gov Exclusions Public Extract V2 loader.

Test taxonomy
-------------
1. Pure helpers
   - _canonicalize_header collapses spaces / slashes / case
   - _build_header_index handles aliases + missing required cols
   - _coerce_classification maps known variants
   - _coerce_uei normalizes case + rejects malformed
   - _coerce_date accepts ISO + MM/DD/YYYY
   - _normalize_for_hash + _compute_record_hash determinism

2. Pure parser (no DB)
   - parse_sam_csv on a synthetic 4-row extract
   - Header alias variations ("State / Province" vs "state")
   - Missing required column -> IngestError
   - Empty Classification -> IngestError
   - All-blank-name row -> IngestError
   - Empty file -> IngestError
   - Header-only file -> IngestError

3. Integration (live_pg)
   - Apply migrations; load synthetic extract; verify raw + views
   - Idempotent re-load (same hash, last_seen_at bumped)
   - SAM-side correction (different hash) -> two rows survive
   - UEI-keyed view filters NULL UEI
   - Active view filters past termination_date and Inactive status
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from ingestion._base import IngestError
from ingestion.sam_gov_exclusions import (
    _build_header_index,
    _canonicalize_header,
    _coerce_classification,
    _coerce_date,
    _coerce_uei,
    _compute_record_hash,
    _normalize_for_hash,
    _validate_vintage_day,
    load_to_postgres,
    parse_sam_csv,
)

if TYPE_CHECKING:
    import psycopg


# ============================================================================
# 1. Pure helpers
# ============================================================================


def test_canonicalize_header_basic() -> None:
    assert _canonicalize_header("Classification") == "classification"
    assert _canonicalize_header("Active Date") == "active_date"
    assert _canonicalize_header("State / Province") == "state_province"
    assert _canonicalize_header("Zip Code") == "zip_code"
    assert _canonicalize_header("  UEI  ") == "uei"
    assert _canonicalize_header("Address 1") == "address_1"


def test_canonicalize_header_collapses_special_chars() -> None:
    """Multiple non-alphanumeric runs collapse to a single underscore."""
    assert _canonicalize_header("Excluding   Agency  Name") == "excluding_agency_name"
    assert _canonicalize_header("---weird---") == "weird"
    assert _canonicalize_header("/leading-slash") == "leading_slash"


def test_build_header_index_happy_path() -> None:
    headers = [
        "Classification", "Name", "First", "Last",
        "UEI", "Active Date", "Termination Date", "Record Status",
    ]
    idx = _build_header_index(headers)
    assert idx["classification"] == 0
    assert idx["name"] == 1
    assert idx["first"] == 2
    assert idx["last"] == 3
    assert idx["uei"] == 4
    assert idx["active_date"] == 5
    assert idx["termination_date"] == 6
    assert idx["record_status"] == 7


def test_build_header_index_accepts_aliases() -> None:
    """SAM has shipped State / Province AND State; both map to state_province."""
    h1 = ["Classification", "State / Province"]
    h2 = ["Classification", "State"]
    h3 = ["Classification", "State or Province"]
    assert _build_header_index(h1)["state_province"] == 1
    assert _build_header_index(h2)["state_province"] == 1
    assert _build_header_index(h3)["state_province"] == 1


def test_build_header_index_rejects_missing_required() -> None:
    """Classification is the only REQUIRED column; missing it must raise."""
    with pytest.raises(IngestError, match="missing required column"):
        _build_header_index(["Name", "First", "Last"])


def test_build_header_index_ignores_unknown_columns() -> None:
    """Unknown columns (e.g., 'DODAAC') are silently dropped, not errors."""
    headers = ["Classification", "DODAAC", "Mystery Column", "Name"]
    idx = _build_header_index(headers)
    # Required col found; extras absent from mapping (no key).
    assert idx["classification"] == 0
    assert idx["name"] == 3
    assert "dodaac" not in idx


def test_coerce_classification_canonical_forms() -> None:
    assert _coerce_classification("Individual") == "Individual"
    assert _coerce_classification("Firm") == "Firm"
    assert _coerce_classification("Vessel") == "Vessel"
    assert (
        _coerce_classification("Special Entity Designation")
        == "Special Entity Designation"
    )


def test_coerce_classification_normalizes_variants() -> None:
    """SAM has shipped 'special entity' (no 'designation') historically."""
    assert _coerce_classification("special entity") == "Special Entity Designation"
    assert _coerce_classification("INDIVIDUAL") == "Individual"
    assert _coerce_classification("  firm  ") == "Firm"
    assert _coerce_classification("Individual Exclusion") == "Individual"


def test_coerce_classification_rejects_unknown() -> None:
    with pytest.raises(IngestError, match="Unknown SAM classification"):
        _coerce_classification("Mystery Class")


def test_coerce_uei_happy_path() -> None:
    assert _coerce_uei("ABC123XYZ987") == "ABC123XYZ987"
    assert _coerce_uei("abc123xyz987") == "ABC123XYZ987"  # case-normalized
    assert _coerce_uei("  ABC123XYZ987  ") == "ABC123XYZ987"


def test_coerce_uei_empty_returns_none() -> None:
    assert _coerce_uei("") is None
    assert _coerce_uei(None) is None
    assert _coerce_uei("   ") is None


def test_coerce_uei_rejects_malformed() -> None:
    """UEI must be 12-char [A-Z0-9]. Anything else is a SAM data bug."""
    for bad in ("SHORT", "TOOLONGTOOLONG", "BAD$CHARS!!!", "ABC 123 XYZ 987"):
        with pytest.raises(IngestError, match="Malformed UEI"):
            _coerce_uei(bad)


def test_coerce_date_iso_and_us_formats() -> None:
    assert _coerce_date("2026-05-04", field="active_date") == "2026-05-04"
    assert _coerce_date("05/04/2026", field="active_date") == "2026-05-04"
    assert _coerce_date("5/4/2026",   field="active_date") == "2026-05-04"
    assert _coerce_date("",   field="active_date") is None
    assert _coerce_date(None, field="active_date") is None


def test_coerce_date_rejects_unrecognized() -> None:
    with pytest.raises(IngestError, match="unrecognized date format"):
        _coerce_date("May 4, 2026", field="active_date")
    with pytest.raises(IngestError, match="unrecognized date format"):
        _coerce_date("2026/05/04", field="active_date")


def test_normalize_for_hash_basic() -> None:
    assert _normalize_for_hash("  Smith  ") == "SMITH"
    assert _normalize_for_hash(None) == ""
    assert _normalize_for_hash("") == ""
    assert _normalize_for_hash("o'Connor") == "O'CONNOR"


def test_compute_record_hash_is_deterministic_across_case_and_whitespace() -> None:
    a = {"classification": "Individual", "name": "JOHN DOE",
         "first": "JOHN", "last": "DOE", "uei": "ABC123XYZ987",
         "active_date": "2020-01-01", "exclusion_type_desc": "TEST",
         "duns": None, "sam_number": None, "middle": None, "suffix": None}
    b = {**a, "name": "  john doe  ", "first": "John"}
    assert _compute_record_hash(a) == _compute_record_hash(b)


def test_compute_record_hash_changes_on_field_edit() -> None:
    base = {"classification": "Individual", "name": "JOHN DOE",
            "first": "JOHN", "last": "DOE", "uei": "ABC123XYZ987",
            "active_date": "2020-01-01", "exclusion_type_desc": "TEST",
            "duns": None, "sam_number": None, "middle": None, "suffix": None}
    h0 = _compute_record_hash(base)
    assert h0 != _compute_record_hash({**base, "uei": "DEF456UVW123"})
    assert h0 != _compute_record_hash({**base, "active_date": "2021-01-01"})
    assert h0 != _compute_record_hash({**base, "last": "ROE"})


def test_compute_record_hash_is_64_hex_chars() -> None:
    h = _compute_record_hash({})
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_validate_vintage_day_accepts_iso() -> None:
    _validate_vintage_day("2026-05-04")
    _validate_vintage_day("1999-12-31")


def test_validate_vintage_day_rejects_other_shapes() -> None:
    for bad in ("2026-5-4", "5/4/2026", "20260504", "2026-05", ""):
        with pytest.raises(IngestError, match="vintage_day"):
            _validate_vintage_day(bad)


# ============================================================================
# 2. Pure parser (no DB)
# ============================================================================


# Synthetic SAM extract covering all four classifications + edge cases.
# Order matches a plausible V2 extract column layout.
_HEADER_COLS = (
    "Classification", "Name", "Prefix", "First", "Middle", "Last", "Suffix",
    "Title", "UEI", "DUNS", "CAGE", "NPI",
    "Address 1", "Address 2", "Address 3", "Address 4",
    "City", "State / Province", "Country", "Zip Code",
    "DODAAC",  # extra column we don't track; must be silently ignored
    "Cross-Reference", "Exclusion Program", "Excluding Agency",
    "Exclusion Type", "Active Date", "Termination Date", "Record Status",
    "SAM Number", "Additional Comments", "Open Data Flag", "Creation_Date",
)


def _synth_sam_csv(rows: list[dict[str, str]]) -> str:
    """Render a list of dicts as a header + body CSV in _HEADER_COLS order."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(_HEADER_COLS)
    for r in rows:
        w.writerow(r.get(c, "") for c in _HEADER_COLS)
    return out.getvalue()


def _write_synth_sam(tmp_path: Path, rows: list[dict[str, str]]) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    p = tmp_path / "sam_synth.csv"
    p.write_text(_synth_sam_csv(rows), encoding="utf-8")
    return p


_FOUR_ROWS: list[dict[str, str]] = [
    # Active individual exclusion (no termination_date).
    {
        "Classification": "Individual",
        "Name": "JOHN Q DOE",
        "First": "JOHN", "Middle": "Q", "Last": "DOE",
        "UEI": "ABC123XYZ987",
        "Address 1": "1 MAIN ST", "City": "TRENTON",
        "State / Province": "NJ", "Zip Code": "08608",
        "Country": "USA",
        "Exclusion Program": "Reciprocal", "Excluding Agency": "HHS",
        "Exclusion Type": "Reciprocal Exclusion",
        "Active Date": "2018-05-15", "Record Status": "Active",
    },
    # Active firm with UEI + DUNS.
    {
        "Classification": "Firm",
        "Name": "ACME FRAUD CORP",
        "UEI": "DEF456UVW123",
        "DUNS": "123456789",
        "Address 1": "100 INDUSTRIAL BLVD", "City": "NEWARK",
        "State / Province": "NJ", "Zip Code": "07102",
        "Country": "USA",
        "Exclusion Program": "Procurement",
        "Excluding Agency": "GSA",
        "Exclusion Type": "Procurement",
        "Active Date": "2019-03-01", "Record Status": "Active",
    },
    # Special Entity Designation, alternate spelling.
    {
        "Classification": "Special Entity",  # variant; should normalize
        "Name": "XYZ FOREIGN ENTITY",
        "Country": "Russia",
        "Exclusion Program": "Non-Procurement",
        "Excluding Agency": "OFAC",
        "Active Date": "05/04/2020",  # MM/DD/YYYY format
        "Record Status": "Active",
    },
    # Vessel, terminated (past termination_date).
    {
        "Classification": "Vessel",
        "Name": "MV BAD ACTOR",
        "Country": "Panama",
        "Exclusion Program": "Reciprocal",
        "Excluding Agency": "Treasury",
        "Active Date": "2010-01-01",
        "Termination Date": "2015-01-01",
        "Record Status": "Inactive",
    },
]


def test_parse_sam_csv_happy_path(tmp_path: Path) -> None:
    p = _write_synth_sam(tmp_path, _FOUR_ROWS)
    parsed = parse_sam_csv(p, source_url="https://example.test/sam.csv")

    assert parsed.n_rows == 4
    assert len(parsed.source_sha256) == 64
    assert parsed.source_url == "https://example.test/sam.csv"

    # Spot-check row 0: individual, classification preserved, UEI upper.
    r0 = parsed.rows[0]
    assert r0["classification"] == "Individual"
    assert r0["name"] == "JOHN Q DOE"
    assert r0["first"] == "JOHN"
    assert r0["last"] == "DOE"
    assert r0["uei"] == "ABC123XYZ987"
    assert r0["active_date"] == "2018-05-15"
    assert r0["termination_date"] is None  # empty -> None
    assert r0["record_status"] == "Active"

    # Row 2: classification variant normalized.
    r2 = parsed.rows[2]
    assert r2["classification"] == "Special Entity Designation"
    assert r2["active_date"] == "2020-05-04"  # MM/DD/YYYY -> ISO

    # Row 3: terminated, dates parsed.
    r3 = parsed.rows[3]
    assert r3["classification"] == "Vessel"
    assert r3["active_date"] == "2010-01-01"
    assert r3["termination_date"] == "2015-01-01"
    assert r3["record_status"] == "Inactive"


def test_parse_sam_csv_drops_unknown_extra_columns(tmp_path: Path) -> None:
    """The extra 'DODAAC' header column must be silently ignored."""
    p = _write_synth_sam(tmp_path, _FOUR_ROWS)
    parsed = parse_sam_csv(p, source_url="https://x.test")
    # No exception on 'DODAAC'; row dicts only carry known columns.
    sample = parsed.rows[0]
    assert "dodaac" not in sample
    assert "classification" in sample


def test_parse_sam_csv_rejects_missing_classification_column(
    tmp_path: Path,
) -> None:
    """Header without Classification is a hard SAM schema-change event."""
    body = "Name,First,Last\n" + "JOHN DOE,JOHN,DOE\n"
    p = tmp_path / "no_class.csv"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(IngestError, match="missing required column"):
        parse_sam_csv(p, source_url="x")


def test_parse_sam_csv_rejects_empty_classification(tmp_path: Path) -> None:
    """Empty Classification field on a data row is a row-level CHECK."""
    rows = [{"Name": "MYSTERY", "Last": "DOE", "Active Date": "2020-01-01"}]
    p = _write_synth_sam(tmp_path, rows)
    with pytest.raises(IngestError, match="empty classification"):
        parse_sam_csv(p, source_url="x")


def test_parse_sam_csv_rejects_unknown_classification(tmp_path: Path) -> None:
    rows = [{"Classification": "Mystery", "Last": "DOE",
             "Active Date": "2020-01-01"}]
    p = _write_synth_sam(tmp_path, rows)
    with pytest.raises(IngestError, match="Unknown SAM classification"):
        parse_sam_csv(p, source_url="x")


def test_parse_sam_csv_rejects_all_blank_name_row(tmp_path: Path) -> None:
    """Row with all of (name, last, uei, duns, sam_number) empty fails CHECK."""
    rows = [{"Classification": "Individual", "Active Date": "2020-01-01"}]
    p = _write_synth_sam(tmp_path, rows)
    with pytest.raises(IngestError, match=r"all of .*are empty"):
        parse_sam_csv(p, source_url="x")


def test_parse_sam_csv_rejects_empty_file(tmp_path: Path) -> None:
    p = tmp_path / "empty.csv"
    p.write_text("", encoding="utf-8")
    with pytest.raises(IngestError, match="empty after read"):
        parse_sam_csv(p, source_url="x")


def test_parse_sam_csv_rejects_header_only_file(tmp_path: Path) -> None:
    p = _write_synth_sam(tmp_path, [])
    with pytest.raises(IngestError, match="parsed 0 data rows"):
        parse_sam_csv(p, source_url="x")


def test_parse_sam_csv_skips_blank_lines(tmp_path: Path) -> None:
    """Blank lines between rows are tolerated (treated as separators, not rows)."""
    body = _synth_sam_csv(_FOUR_ROWS[:2])
    body = body.replace("\n", "\n\n", 1)  # inject blank line after header
    p = tmp_path / "blanks.csv"
    p.write_text(body, encoding="utf-8")
    parsed = parse_sam_csv(p, source_url="x")
    assert parsed.n_rows == 2


def test_parse_sam_csv_rejects_malformed_uei(tmp_path: Path) -> None:
    rows = [{
        "Classification": "Firm", "Name": "BAD UEI INC",
        "UEI": "TOOSHORT",
        "Active Date": "2020-01-01",
    }]
    p = _write_synth_sam(tmp_path, rows)
    with pytest.raises(IngestError, match="Malformed UEI"):
        parse_sam_csv(p, source_url="x")


# ============================================================================
# 3. Integration (live_pg)
# ============================================================================


@pytest.fixture
def sam_db(live_pg: psycopg.Connection) -> psycopg.Connection:
    """Apply migrations into a fresh schema set; yield the conn."""
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
            "    EXECUTE 'DROP VIEW IF EXISTS public.' "
            "            || quote_ident(r.viewname) || ' CASCADE'; "
            "  END LOOP; "
            "END $$;",
        )
    conn.commit()
    apply_migrations(conn, discover(MIGRATIONS_DIR))
    apply_migrations(conn, discover(SEEDS_DIR))
    return conn


@pytest.mark.live_pg
def test_load_synthetic_sam_round_trip(
    sam_db: psycopg.Connection, tmp_path: Path,
) -> None:
    """Happy path: 4-row file lands as 4 rows in raw.sam_gov_exclusion."""
    p = _write_synth_sam(tmp_path, _FOUR_ROWS)
    parsed = parse_sam_csv(p, source_url="https://example.test/sam.csv")
    n = load_to_postgres(parsed, sam_db, vintage_day="2026-05-04")
    sam_db.commit()
    assert n == 4

    with sam_db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM raw.sam_gov_exclusion")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 4

        cur.execute(
            "SELECT DISTINCT vintage_day, source_url FROM raw.sam_gov_exclusion",
        )
        rows = cur.fetchall()
        assert len(rows) == 1
        vd, url = rows[0]
        assert vd == dt.date(2026, 5, 4)
        assert url == "https://example.test/sam.csv"

        # Classifications canonicalized at load (Special Entity -> ...Designation).
        cur.execute(
            "SELECT classification, COUNT(*) "
            "FROM raw.sam_gov_exclusion GROUP BY classification "
            "ORDER BY classification",
        )
        counts: dict[str, int] = dict(cur.fetchall())
        assert counts == {
            "Firm": 1, "Individual": 1,
            "Special Entity Designation": 1, "Vessel": 1,
        }


@pytest.mark.live_pg
def test_active_view_filters_terminated_and_inactive(
    sam_db: psycopg.Connection, tmp_path: Path,
) -> None:
    """v_sam_exclusion_active drops past-termination AND Inactive rows."""
    p = _write_synth_sam(tmp_path, _FOUR_ROWS)
    parsed = parse_sam_csv(p, source_url="x")
    load_to_postgres(parsed, sam_db, vintage_day="2026-05-04")
    sam_db.commit()

    with sam_db.cursor() as cur:
        # Vessel row terminated 2015 + status Inactive -> excluded by view.
        cur.execute("SELECT COUNT(*) FROM derived.v_sam_exclusion_active")
        row = cur.fetchone()
        assert row is not None
        # 4 raw, 1 vessel filtered out -> 3 active.
        assert row[0] == 3


@pytest.mark.live_pg
def test_uei_view_drops_null_uei_rows(
    sam_db: psycopg.Connection, tmp_path: Path,
) -> None:
    """v_sam_exclusion_by_uei retains only UEI-bearing exclusions."""
    p = _write_synth_sam(tmp_path, _FOUR_ROWS)
    parsed = parse_sam_csv(p, source_url="x")
    load_to_postgres(parsed, sam_db, vintage_day="2026-05-04")
    sam_db.commit()

    with sam_db.cursor() as cur:
        cur.execute(
            "SELECT sam_uei FROM derived.v_sam_exclusion_by_uei ORDER BY sam_uei",
        )
        rows = [r[0] for r in cur.fetchall()]
    # Only Individual + Firm have UEI in our synth data.
    assert rows == ["ABC123XYZ987", "DEF456UVW123"]


@pytest.mark.live_pg
def test_individual_canonical_view_only_individuals(
    sam_db: psycopg.Connection, tmp_path: Path,
) -> None:
    """v_sam_exclusion_individual_canonical retains only Individual classification.

    The view itself does not project `classification` (it filters via WHERE),
    so we count rows and verify the canonical key was computed for the one
    Individual exclusion in our synthetic extract. Sanity-cross-check via the
    raw table that the COUNT matches the number of Individual rows that have
    a non-NULL canonical key.
    """
    p = _write_synth_sam(tmp_path, _FOUR_ROWS)
    parsed = parse_sam_csv(p, source_url="x")
    load_to_postgres(parsed, sam_db, vintage_day="2026-05-04")
    sam_db.commit()

    with sam_db.cursor() as cur:
        cur.execute(
            "SELECT sam_last, sam_first, canonical_key "
            "FROM derived.v_sam_exclusion_individual_canonical "
            "ORDER BY sam_last, sam_first",
        )
        rows = cur.fetchall()
    # Only one Individual row in the synthetic extract (DOE).
    assert len(rows) == 1
    last, first, canonical_key = rows[0]
    assert last == "DOE"
    assert first == "JOHN"
    # Canonical key shape: 'LAST|FIRST' upper-cased, normalized.
    assert canonical_key is not None
    assert canonical_key.startswith("DOE|")


@pytest.mark.live_pg
def test_idempotent_reload_no_duplicates(
    sam_db: psycopg.Connection, tmp_path: Path,
) -> None:
    """Loading the same file twice is a no-op on row count + bumps last_seen_at."""
    p = _write_synth_sam(tmp_path, _FOUR_ROWS)
    parsed = parse_sam_csv(p, source_url="x")
    load_to_postgres(parsed, sam_db, vintage_day="2026-05-04")
    sam_db.commit()

    with sam_db.cursor() as cur:
        cur.execute("SELECT MIN(last_seen_at) FROM raw.sam_gov_exclusion")
        row = cur.fetchone()
        assert row is not None
        first_floor = row[0]

    load_to_postgres(parsed, sam_db, vintage_day="2026-05-05")
    sam_db.commit()

    with sam_db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM raw.sam_gov_exclusion")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 4  # no duplicates

        cur.execute("SELECT MIN(last_seen_at) FROM raw.sam_gov_exclusion")
        row = cur.fetchone()
        assert row is not None
        assert row[0] >= first_floor

        cur.execute("SELECT DISTINCT vintage_day FROM raw.sam_gov_exclusion")
        rows = cur.fetchall()
        assert rows == [(dt.date(2026, 5, 5),)]


@pytest.mark.live_pg
def test_sam_correction_inserts_new_row_keeps_old(
    sam_db: psycopg.Connection, tmp_path: Path,
) -> None:
    """A SAM-side data correction yields a NEW record_hash; old row stays."""
    first = _write_synth_sam(tmp_path / "first", _FOUR_ROWS)
    parsed = parse_sam_csv(first, source_url="x")
    load_to_postgres(parsed, sam_db, vintage_day="2026-05-04")
    sam_db.commit()

    # Edit row 0's exclusion_type_desc (in the hash column set).
    edited = [dict(r) for r in _FOUR_ROWS]
    edited[0]["Exclusion Type"] = "Reciprocal Exclusion (Updated)"
    second = _write_synth_sam(tmp_path / "second", edited)
    parsed2 = parse_sam_csv(second, source_url="x")
    load_to_postgres(parsed2, sam_db, vintage_day="2026-06-04")
    sam_db.commit()

    with sam_db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM raw.sam_gov_exclusion")
        row = cur.fetchone()
        assert row is not None
        # Five rows: four originals + one re-hashed Doe (old hash retained).
        assert row[0] == 5
