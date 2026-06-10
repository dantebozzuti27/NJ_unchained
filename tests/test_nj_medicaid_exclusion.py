"""Tests for the NJ Medicaid (OSC) exclusion-list ingester.

NJ's state-level analog to the federal HHS-OIG LEIE; the test taxonomy
mirrors tests/test_hhs_oig_leie.py.

Test taxonomy
-------------
1. Pure helpers
   - compute_record_hash determinism + canonicalization
   - _normalize_for_hash whitespace / case / NUL behavior
   - _extract_npi: first 10-digit segment, multi-value, none
   - _parse_address: trailing "CITY, ST ZIP" parse + lossless raw + NULL fallback

2. Pure parser (no DB)
   - parse_nj_med_csv on a synthetic OpenSanctions targets.simple.csv
   - Header drift -> IngestError
   - Wrong column count per data row -> IngestError
   - Empty name -> IngestError
   - Empty file -> IngestError
   - Header-only (zero data rows) -> IngestError
   - Blank lines tolerated

3. Integration (live_pg)
   - Apply all migrations + seeds; load a synthetic file; verify
     raw.nj_medicaid_exclusion row count + provenance.
   - Idempotent re-load: same file twice -> same row count, bumps
     last_seen_at without duplicating rows.
   - Intra-batch duplicate rows deduped via DISTINCT ON.
   - Profile correction: edit one row, re-load; old hash stays, new
     hash inserted.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from ingestion._base import IngestError
from ingestion.nj_medicaid_exclusion import (
    OPENSANCTIONS_COLUMNS,
    FetchResult,
    _extract_npi,
    _normalize_for_hash,
    _parse_address,
    compute_record_hash,
    load_to_postgres,
    parse_nj_med_csv,
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
    assert _normalize_for_hash("Smith\x00") == "SMITH"
    assert _normalize_for_hash("\x00\x00") == ""


def test_compute_record_hash_is_deterministic_across_whitespace_and_case() -> None:
    a = {"full_name": "FASONU, AYODEJI", "npi": "1740518992",
         "address": "70 RUSLING PLACE, BRIDGEPORT, CT 06604",
         "city": "BRIDGEPORT", "state": "CT", "zip": "06604",
         "action": "", "effective_date": "2023-02-07", "expiration_date": ""}
    b = {**a, "full_name": "  fasonu, ayodeji  ", "city": "bridgeport"}
    assert compute_record_hash(a) == compute_record_hash(b)


def test_compute_record_hash_changes_when_canonical_field_changes() -> None:
    base = {"full_name": "FASONU, AYODEJI", "npi": "1740518992",
            "address": "70 RUSLING PLACE, BRIDGEPORT, CT 06604",
            "city": "BRIDGEPORT", "state": "CT", "zip": "06604",
            "action": "", "effective_date": "2023-02-07", "expiration_date": ""}
    h0 = compute_record_hash(base)
    assert h0 != compute_record_hash({**base, "effective_date": "2023-02-08"})
    assert h0 != compute_record_hash({**base, "zip": "06605"})
    assert h0 != compute_record_hash({**base, "npi": "9999999999"})
    assert h0 != compute_record_hash({**base, "address": "1 OTHER ST, TRENTON, NJ 08608"})


def test_compute_record_hash_is_64_hex_chars() -> None:
    h = compute_record_hash({})
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_extract_npi_single_value() -> None:
    assert _extract_npi("1740518992") == "1740518992"


def test_extract_npi_first_of_multiple() -> None:
    assert _extract_npi("1407087802;1578557344") == "1407087802"


def test_extract_npi_skips_non_npi_tokens() -> None:
    # A leading non-NPI token must not block a valid NPI later in the list.
    assert _extract_npi("NJ-1234;1578557344") == "1578557344"


def test_extract_npi_returns_blank_when_absent() -> None:
    assert _extract_npi("") == ""
    assert _extract_npi("not-an-npi") == ""
    assert _extract_npi("123456789") == ""   # 9 digits, not an NPI


def test_parse_address_standard_shape() -> None:
    addr, city, state, zip_code = _parse_address(
        "70 RUSLING PLACE, BRIDGEPORT, CT 06604",
    )
    assert addr == "70 RUSLING PLACE, BRIDGEPORT, CT 06604"  # lossless raw
    assert city == "BRIDGEPORT"
    assert state == "CT"
    assert zip_code == "06604"


def test_parse_address_with_apartment_extra_comma() -> None:
    _, city, state, zip_code = _parse_address(
        "64 CARUTH AVENUE, APT. 66, ELMWOOD PARK, NJ 07407",
    )
    assert city == "ELMWOOD PARK"
    assert state == "NJ"
    assert zip_code == "07407"


def test_parse_address_keeps_raw_multi_address_but_parses_first() -> None:
    raw = "115 NELLIS DRIVE, WAYNE, NJ 07470;1618 MAIN AVENUE, CLIFTON, NJ 07011"
    addr, city, state, zip_code = _parse_address(raw)
    assert addr == raw                # nothing lost
    assert city == "WAYNE"            # parsed from the FIRST segment only
    assert state == "NJ"
    assert zip_code == "07470"


def test_parse_address_zip_plus_four() -> None:
    _, _, _, zip_code = _parse_address("1 MAIN ST, TRENTON, NJ 08608-1234")
    assert zip_code == "08608-1234"


def test_parse_address_unparseable_keeps_raw_and_nulls_components() -> None:
    addr, city, state, zip_code = _parse_address("SOMEWHERE IN MEXICO")
    assert addr == "SOMEWHERE IN MEXICO"
    assert (city, state, zip_code) == ("", "", "")


def test_parse_address_empty() -> None:
    assert _parse_address("") == ("", "", "", "")
    assert _parse_address("   ") == ("", "", "", "")


# ============================================================================
# 2. Pure parser (no DB)
# ============================================================================


def _render_opensanctions_csv(rows: list[dict[str, str]]) -> str:
    """Render dicts as a header + body CSV in OPENSANCTIONS_COLUMNS order.

    Uses csv.writer so address fields containing commas are quoted the
    same way the real OpenSanctions export quotes them.
    """
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(OPENSANCTIONS_COLUMNS)
    for r in rows:
        writer.writerow([r.get(c, "") for c in OPENSANCTIONS_COLUMNS])
    return out.getvalue()


def _write_synth_csv(tmp_path: Path, rows: list[dict[str, str]]) -> FetchResult:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "nj_med_synth.csv"
    path.write_text(_render_opensanctions_csv(rows), encoding="utf-8")
    return FetchResult(
        path=path,
        source_url="https://example.test/targets.simple.csv",
        source_sha256="0" * 64,
        source_vintage="test-vintage",
        n_bytes=path.stat().st_size,
        cache_hit=False,
    )


_FOUR_ROWS: list[dict[str, str]] = [
    {  # individual provider, NJ, with NPI
        "id": "NK-0001", "schema": "LegalEntity",
        "name": "FASONU, AYODEJI",
        "countries": "us",
        "addresses": "70 RUSLING PLACE, BRIDGEPORT, CT 06604",
        "identifiers": "1740518992",
        "sanctions": "2023-02-07",
        "dataset": "US New Jersey Ineligible Medicaid Providers",
        "first_seen": "2024-11-08T09:17:44",
        "last_seen": "2026-06-01T04:50:01",
        "last_change": "2024-11-08T09:17:44",
    },
    {  # NJ entity, no NPI
        "id": "NK-0002", "schema": "LegalEntity",
        "name": "TREGO-CAMPION, ANITA",
        "countries": "us",
        "addresses": "136 S. FELLOWSHIP ROAD, MAPLE SHADE, NJ 08052",
        "identifiers": "",
        "sanctions": "2005-10-20",
        "dataset": "US New Jersey Ineligible Medicaid Providers",
    },
    {  # multiple NPIs + multiple addresses
        "id": "NK-0003", "schema": "LegalEntity",
        "name": "POSNER, ROBERT S.",
        "countries": "us",
        "addresses": (
            "115 NELLIS DRIVE, WAYNE, NJ 07470;"
            "1618 MAIN AVENUE, CLIFTON, NJ 07011"
        ),
        "identifiers": "1407087802;1578557344",
        "sanctions": "1999-06-23",
        "dataset": "US New Jersey Ineligible Medicaid Providers",
    },
    {  # date-range / multi sanction caption, unparseable address
        "id": "NK-0004", "schema": "LegalEntity",
        "name": "WILSON, JEAN",
        "countries": "us",
        "addresses": "OVERSEAS ADDRESS UNKNOWN",
        "identifiers": "1740568450",
        "sanctions": "2023-05-02 - 2024-06-18;2024-06-19",
        "dataset": "US New Jersey Ineligible Medicaid Providers",
    },
]


def test_parse_happy_path_maps_columns(tmp_path: Path) -> None:
    fetch = _write_synth_csv(tmp_path, _FOUR_ROWS)
    parsed = parse_nj_med_csv(fetch)

    assert parsed.n_rows == 4
    assert len(parsed.rows) == 4

    # Row 0: full mapping incl. parsed address components + NPI.
    cols = (
        "full_name", "npi", "address", "city", "state", "zip",
        "action", "effective_date", "expiration_date",
    )
    r0 = dict(zip(cols, parsed.rows[0], strict=True))
    assert r0["full_name"] == "FASONU, AYODEJI"
    assert r0["npi"] == "1740518992"
    assert r0["address"] == "70 RUSLING PLACE, BRIDGEPORT, CT 06604"
    assert r0["city"] == "BRIDGEPORT"
    assert r0["state"] == "CT"
    assert r0["zip"] == "06604"
    assert r0["action"] == ""                  # no action-type in simplified export
    assert r0["effective_date"] == "2023-02-07"
    assert r0["expiration_date"] == ""         # not separable in simplified export

    # Row 1: no NPI -> blank.
    r1 = dict(zip(cols, parsed.rows[1], strict=True))
    assert r1["npi"] == ""
    assert r1["city"] == "MAPLE SHADE"

    # Row 2: first of multiple NPIs, first of multiple addresses parsed,
    # full multi-address string preserved verbatim in `address`.
    r2 = dict(zip(cols, parsed.rows[2], strict=True))
    assert r2["npi"] == "1407087802"
    assert ";" in r2["address"]
    assert r2["city"] == "WAYNE"
    assert r2["zip"] == "07470"

    # Row 3: unparseable address keeps raw, NULLs the components; the
    # raw sanction caption (range + list) is preserved as effective_date.
    r3 = dict(zip(cols, parsed.rows[3], strict=True))
    assert r3["address"] == "OVERSEAS ADDRESS UNKNOWN"
    assert (r3["city"], r3["state"], r3["zip"]) == ("", "", "")
    assert r3["effective_date"] == "2023-05-02 - 2024-06-18;2024-06-19"


def test_parse_rejects_header_drift(tmp_path: Path) -> None:
    body = _render_opensanctions_csv(_FOUR_ROWS[:1])
    bad = body.replace("sanctions", "sanction_dates", 1)  # rename one header
    p = tmp_path / "drift.csv"
    p.write_text(bad, encoding="utf-8")
    fetch = FetchResult(
        path=p, source_url="x", source_sha256="0" * 64,
        source_vintage="v", n_bytes=p.stat().st_size, cache_hit=False,
    )
    with pytest.raises(IngestError, match="schema drift"):
        parse_nj_med_csv(fetch)


def test_parse_rejects_wrong_column_count(tmp_path: Path) -> None:
    header = ",".join(OPENSANCTIONS_COLUMNS) + "\n"
    short = header + ",".join(["x"] * (len(OPENSANCTIONS_COLUMNS) - 1)) + "\n"
    p = tmp_path / "shortrow.csv"
    p.write_text(short, encoding="utf-8")
    fetch = FetchResult(
        path=p, source_url="x", source_sha256="0" * 64,
        source_vintage="v", n_bytes=p.stat().st_size, cache_hit=False,
    )
    with pytest.raises(IngestError, match=r"got \d+ fields, expected"):
        parse_nj_med_csv(fetch)


def test_parse_rejects_empty_name(tmp_path: Path) -> None:
    rows = [{
        "id": "NK-x", "schema": "LegalEntity", "name": "",
        "addresses": "1 MAIN ST, TRENTON, NJ 08608",
        "sanctions": "2020-01-01",
    }]
    fetch = _write_synth_csv(tmp_path, rows)
    with pytest.raises(IngestError, match="empty name"):
        parse_nj_med_csv(fetch)


def test_parse_rejects_empty_file(tmp_path: Path) -> None:
    p = tmp_path / "empty.csv"
    p.write_text("", encoding="utf-8")
    fetch = FetchResult(
        path=p, source_url="x", source_sha256="0" * 64,
        source_vintage="v", n_bytes=0, cache_hit=False,
    )
    with pytest.raises(IngestError, match="empty"):
        parse_nj_med_csv(fetch)


def test_parse_rejects_header_only_file(tmp_path: Path) -> None:
    p = tmp_path / "headeronly.csv"
    p.write_text(",".join(OPENSANCTIONS_COLUMNS) + "\n", encoding="utf-8")
    fetch = FetchResult(
        path=p, source_url="x", source_sha256="0" * 64,
        source_vintage="v", n_bytes=p.stat().st_size, cache_hit=False,
    )
    with pytest.raises(IngestError, match="parsed 0 data rows"):
        parse_nj_med_csv(fetch)


def test_parse_skips_blank_lines(tmp_path: Path) -> None:
    body = _render_opensanctions_csv([_FOUR_ROWS[0], _FOUR_ROWS[1]])
    # Inject two blank lines between header and body's tail.
    lines = body.splitlines(keepends=True)
    injected = [*lines[:2], "\n", "\n", *lines[2:]]
    p = tmp_path / "blanks.csv"
    p.write_text("".join(injected), encoding="utf-8")
    fetch = FetchResult(
        path=p, source_url="x", source_sha256="0" * 64,
        source_vintage="v", n_bytes=p.stat().st_size, cache_hit=False,
    )
    parsed = parse_nj_med_csv(fetch)
    assert parsed.n_rows == 2


# ============================================================================
# 3. Integration (live_pg)
# ============================================================================
#
# These exercise the COPY + UPSERT path against real Postgres semantics.
# They depend on the raw.nj_medicaid_exclusion migration (written
# separately) being present in db/migrations. Skipped entirely when
# PG_TEST_DSN is not set (see tests/conftest.py).
# ============================================================================


@pytest.fixture
def njmed_db(live_pg: psycopg.Connection) -> psycopg.Connection:
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
def test_load_synthetic_round_trip(
    njmed_db: psycopg.Connection, tmp_path: Path,
) -> None:
    """Happy path: 4-row file lands as 4 rows with provenance populated."""
    fetch = _write_synth_csv(tmp_path, _FOUR_ROWS)
    parsed = parse_nj_med_csv(fetch)
    n = load_to_postgres(parsed, njmed_db)
    njmed_db.commit()
    assert n == 4

    with njmed_db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM raw.nj_medicaid_exclusion")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 4

        cur.execute(
            "SELECT DISTINCT source_vintage, source_sha256, source_url "
            "FROM raw.nj_medicaid_exclusion",
        )
        rows = cur.fetchall()
        assert len(rows) == 1
        vintage, sha, url = rows[0]
        assert vintage == "test-vintage"
        assert len(sha) == 64
        assert url.endswith("targets.simple.csv")

        # Blank -> SQL NULL: the no-NPI row stores NULL, not ''.
        cur.execute(
            "SELECT npi FROM raw.nj_medicaid_exclusion "
            "WHERE full_name = 'TREGO-CAMPION, ANITA'",
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] is None

        # action / expiration_date are NULL for every row from this source.
        cur.execute(
            "SELECT COUNT(*) FROM raw.nj_medicaid_exclusion "
            "WHERE action IS NOT NULL OR expiration_date IS NOT NULL",
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 0


@pytest.mark.live_pg
def test_idempotent_reload_is_a_no_op_on_row_count(
    njmed_db: psycopg.Connection, tmp_path: Path,
) -> None:
    """Loading the same file twice leaves the row count unchanged."""
    fetch = _write_synth_csv(tmp_path, _FOUR_ROWS)
    parsed = parse_nj_med_csv(fetch)
    load_to_postgres(parsed, njmed_db)
    njmed_db.commit()

    with njmed_db.cursor() as cur:
        cur.execute("SELECT MIN(last_seen_at) FROM raw.nj_medicaid_exclusion")
        row = cur.fetchone()
        assert row is not None
        first_pull_floor = row[0]

    load_to_postgres(parsed, njmed_db)
    njmed_db.commit()

    with njmed_db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM raw.nj_medicaid_exclusion")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 4  # no duplicates

        cur.execute("SELECT MIN(last_seen_at) FROM raw.nj_medicaid_exclusion")
        row = cur.fetchone()
        assert row is not None
        assert row[0] >= first_pull_floor


@pytest.mark.live_pg
def test_intra_batch_duplicate_rows_are_deduped_via_distinct_on(
    njmed_db: psycopg.Connection, tmp_path: Path,
) -> None:
    """A pixel-identical duplicate row must not raise CardinalityViolation."""
    rows = [dict(r) for r in _FOUR_ROWS]
    rows.append(dict(_FOUR_ROWS[0]))  # exact duplicate of row 0
    assert len(rows) == 5

    fetch = _write_synth_csv(tmp_path, rows)
    parsed = parse_nj_med_csv(fetch)
    assert parsed.n_rows == 5  # parser does not dedupe

    n = load_to_postgres(parsed, njmed_db)  # DISTINCT ON collapses the dup
    njmed_db.commit()
    assert n == 4

    with njmed_db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM raw.nj_medicaid_exclusion")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 4


@pytest.mark.live_pg
def test_profile_correction_inserts_new_row_keeps_old(
    njmed_db: psycopg.Connection, tmp_path: Path,
) -> None:
    """A content edit yields a new record_hash; the old hash stays."""
    first = _write_synth_csv(tmp_path, _FOUR_ROWS)
    load_to_postgres(parse_nj_med_csv(first), njmed_db)
    njmed_db.commit()

    edited = [dict(r) for r in _FOUR_ROWS]
    edited[0]["addresses"] = "999 NEW ADDRESS ST, TRENTON, NJ 08611"
    second = _write_synth_csv(tmp_path / "second", edited)
    load_to_postgres(parse_nj_med_csv(second), njmed_db)
    njmed_db.commit()

    with njmed_db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM raw.nj_medicaid_exclusion")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 5  # four originals + one re-hashed FASONU

        cur.execute(
            "SELECT COUNT(*) FROM raw.nj_medicaid_exclusion "
            "WHERE full_name = 'FASONU, AYODEJI'",
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 2
