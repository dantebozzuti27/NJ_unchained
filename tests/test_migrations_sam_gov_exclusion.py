"""Tests for migration 063: raw.sam_gov_exclusion + derived views.

Schema-only migration. The loader and cross-source signal are in
follow-up migrations and have their own test files.

Test taxonomy
-------------
1. Schema invariants
   - All four classifications accepted; a fifth value rejected.
   - record_hash CHECK enforces 64-char lowercase hex.
   - UEI CHECK accepts a 12-char alphanumeric and rejects malformed.
   - source_sha256 CHECK enforces 64-char lowercase hex.
   - Sanity row-content CHECK rejects all-blank rows.
2. Active view
   - termination_date in the past -> excluded.
   - termination_date NULL -> included.
   - termination_date in the future -> included.
   - record_status='Inactive' -> excluded.
   - record_status='Active' -> included.
3. UEI view
   - Drops rows without UEI.
   - Carries through the UEI value verbatim.
4. Individual canonical view
   - Includes Individual classification.
   - Excludes Firm/Vessel/Special Entity Designation.
   - Skips rows where canonicalization returns NULL.
   - Computes the LAST|FIRST canonical key consistently with LEIE.
"""

from __future__ import annotations

import datetime as dt

import psycopg
import pytest

pytestmark = pytest.mark.live_pg


# ============================================================================
# Fixture: fresh-schema conn with all migrations applied
# ============================================================================


@pytest.fixture
def fraud_db(live_pg: psycopg.Connection) -> psycopg.Connection:
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
            "         || quote_ident(r.viewname) || ' CASCADE'; "
            "  END LOOP; "
            "END $$;",
        )
    conn.commit()
    apply_migrations(conn, discover(MIGRATIONS_DIR))
    apply_migrations(conn, discover(SEEDS_DIR))
    return conn


# ============================================================================
# Helpers
# ============================================================================


def _insert_minimal(
    conn: psycopg.Connection,
    *,
    record_hash: str,
    classification: str,
    name: str | None = None,
    last: str | None = None,
    first: str | None = None,
    uei: str | None = None,
    duns: str | None = None,
    sam_number: str | None = None,
    termination_date: dt.date | None = None,
    record_status: str | None = None,
    state_province: str | None = "NJ",
    active_date: dt.date | None = None,
    excluding_agency_name: str | None = "GSA",
    exclusion_type_desc: str | None = "Procurement",
    vintage_day: dt.date | None = None,
) -> None:
    """Insert one minimal row covering only fields tests assert on."""
    if vintage_day is None:
        vintage_day = dt.datetime.now(dt.UTC).date()
    if active_date is None:
        active_date = dt.date(2024, 1, 1)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.sam_gov_exclusion ("
            "  record_hash, classification, "
            "  name, first, last, uei, duns, sam_number, "
            "  state_province, "
            "  excluding_agency_name, exclusion_type_desc, "
            "  active_date, termination_date, record_status, "
            "  vintage_day, source_url, source_sha256"
            ") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                record_hash, classification,
                name, first, last, uei, duns, sam_number,
                state_province,
                excluding_agency_name, exclusion_type_desc,
                active_date, termination_date, record_status,
                vintage_day,
                "https://sam.gov/extract/test.csv", "0" * 64,
            ),
        )


# ============================================================================
# 1. Schema invariants
# ============================================================================


def test_all_four_classifications_accepted(
    fraud_db: psycopg.Connection,
) -> None:
    for i, kind in enumerate([
        "Individual",
        "Special Entity Designation",
        "Firm",
        "Vessel",
    ]):
        _insert_minimal(
            fraud_db,
            record_hash=f"{i}" * 64,
            classification=kind,
            name="ANYONE",
            sam_number=f"SAM-{i}",
        )
    fraud_db.commit()
    with fraud_db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM raw.sam_gov_exclusion")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 4


def test_unknown_classification_rejected(
    fraud_db: psycopg.Connection,
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):  # noqa: SIM117
        with fraud_db.cursor() as cur:
            cur.execute(
                "INSERT INTO raw.sam_gov_exclusion ("
                "  record_hash, classification, name, "
                "  vintage_day, source_url, source_sha256"
                ") VALUES (%s, 'Bogus', 'Anyone', %s, %s, %s)",
                ("a" * 64, dt.datetime.now(dt.UTC).date(),
                 "https://sam.gov/test.csv", "0" * 64),
            )
    fraud_db.rollback()


def test_uei_check_accepts_valid_format(
    fraud_db: psycopg.Connection,
) -> None:
    """12-char uppercase alphanumeric is accepted."""
    _insert_minimal(
        fraud_db,
        record_hash="b" * 64,
        classification="Firm",
        name="ACME PROCUREMENT INC",
        uei="ABCDEF123456",
    )
    fraud_db.commit()


def test_uei_check_rejects_lowercase(fraud_db: psycopg.Connection) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):  # noqa: SIM117
        with fraud_db.cursor() as cur:
            cur.execute(
                "INSERT INTO raw.sam_gov_exclusion ("
                "  record_hash, classification, name, uei, "
                "  vintage_day, source_url, source_sha256"
                ") VALUES (%s, 'Firm', 'ACME', 'abcdef123456', %s, %s, %s)",
                ("c" * 64, dt.datetime.now(dt.UTC).date(),
                 "https://sam.gov/test.csv", "0" * 64),
            )
    fraud_db.rollback()


def test_uei_check_rejects_wrong_length(
    fraud_db: psycopg.Connection,
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):  # noqa: SIM117
        with fraud_db.cursor() as cur:
            cur.execute(
                "INSERT INTO raw.sam_gov_exclusion ("
                "  record_hash, classification, name, uei, "
                "  vintage_day, source_url, source_sha256"
                ") VALUES (%s, 'Firm', 'ACME', 'ABCDEF12345', %s, %s, %s)",
                ("d" * 64, dt.datetime.now(dt.UTC).date(),
                 "https://sam.gov/test.csv", "0" * 64),
            )
    fraud_db.rollback()


def test_record_hash_check_rejects_uppercase(
    fraud_db: psycopg.Connection,
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):  # noqa: SIM117
        with fraud_db.cursor() as cur:
            cur.execute(
                "INSERT INTO raw.sam_gov_exclusion ("
                "  record_hash, classification, name, "
                "  vintage_day, source_url, source_sha256"
                ") VALUES (%s, 'Firm', 'ACME', %s, %s, %s)",
                ("A" * 64, dt.datetime.now(dt.UTC).date(),
                 "https://sam.gov/test.csv", "0" * 64),
            )
    fraud_db.rollback()


def test_all_blank_row_rejected(fraud_db: psycopg.Connection) -> None:
    """The CHECK requires at least one of (name, last, uei, duns,
    sam_number) to be non-empty."""
    with pytest.raises(psycopg.errors.CheckViolation):  # noqa: SIM117
        with fraud_db.cursor() as cur:
            cur.execute(
                "INSERT INTO raw.sam_gov_exclusion ("
                "  record_hash, classification, "
                "  vintage_day, source_url, source_sha256"
                ") VALUES (%s, 'Firm', %s, %s, %s)",
                ("e" * 64, dt.datetime.now(dt.UTC).date(),
                 "https://sam.gov/test.csv", "0" * 64),
            )
    fraud_db.rollback()


def test_vessel_with_sam_number_only_accepted(
    fraud_db: psycopg.Connection,
) -> None:
    """A Vessel exclusion can lack human/firm names and key only by
    SAM number; the row-content CHECK explicitly allows this."""
    _insert_minimal(
        fraud_db,
        record_hash="f" * 64,
        classification="Vessel",
        sam_number="V-2024-ABC",
    )
    fraud_db.commit()


# ============================================================================
# 2. Active view
# ============================================================================


def test_active_view_excludes_past_termination(
    fraud_db: psycopg.Connection,
) -> None:
    """termination_date in the past -> exclusion is over -> drop."""
    _insert_minimal(
        fraud_db,
        record_hash="0" * 64,
        classification="Firm",
        name="OLD FIRM",
        termination_date=dt.datetime.now(dt.UTC).date() - dt.timedelta(days=30),
    )
    fraud_db.commit()
    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM derived.v_sam_exclusion_active "
            "WHERE record_hash = %s",
            ("0" * 64,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 0


def test_active_view_includes_null_termination(
    fraud_db: psycopg.Connection,
) -> None:
    _insert_minimal(
        fraud_db,
        record_hash="1" * 64,
        classification="Firm",
        name="INDEFINITELY EXCLUDED",
        termination_date=None,
    )
    fraud_db.commit()
    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM derived.v_sam_exclusion_active "
            "WHERE record_hash = %s",
            ("1" * 64,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 1


def test_active_view_includes_future_termination(
    fraud_db: psycopg.Connection,
) -> None:
    _insert_minimal(
        fraud_db,
        record_hash="2" * 64,
        classification="Firm",
        name="SCHEDULED TO REINSTATE",
        termination_date=dt.datetime.now(dt.UTC).date() + dt.timedelta(days=365),
    )
    fraud_db.commit()
    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM derived.v_sam_exclusion_active "
            "WHERE record_hash = %s",
            ("2" * 64,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 1


def test_active_view_excludes_inactive_record_status(
    fraud_db: psycopg.Connection,
) -> None:
    """SAM sometimes flips record_status='Inactive' before
    termination_date hits. The active view must respect that."""
    _insert_minimal(
        fraud_db,
        record_hash="3" * 64,
        classification="Firm",
        name="EARLY REINSTATEMENT",
        termination_date=dt.datetime.now(dt.UTC).date() + dt.timedelta(days=365),
        record_status="Inactive",
    )
    fraud_db.commit()
    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM derived.v_sam_exclusion_active "
            "WHERE record_hash = %s",
            ("3" * 64,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 0


def test_active_view_record_status_active_string_match(
    fraud_db: psycopg.Connection,
) -> None:
    """record_status='Active' explicitly is accepted (only 'inactive'
    drops; case-insensitive match)."""
    for i, status in enumerate(["Active", "active", "ACTIVE", None]):
        _insert_minimal(
            fraud_db,
            record_hash=f"{i+4}" * 64,
            classification="Firm",
            name=f"FIRM_{i}",
            record_status=status,
        )
    fraud_db.commit()
    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM derived.v_sam_exclusion_active "
            "WHERE name LIKE 'FIRM_%'",
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 4


# ============================================================================
# 3. UEI view
# ============================================================================


def test_by_uei_view_drops_rows_without_uei(
    fraud_db: psycopg.Connection,
) -> None:
    _insert_minimal(
        fraud_db,
        record_hash="8" * 64,
        classification="Firm",
        name="NO UEI FIRM",
        uei=None,
    )
    _insert_minimal(
        fraud_db,
        record_hash="9" * 64,
        classification="Firm",
        name="HAS UEI FIRM",
        uei="ZYXWVU098765",
    )
    fraud_db.commit()
    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT sam_uei "
            "FROM derived.v_sam_exclusion_by_uei "
            "ORDER BY sam_record_hash",
        )
        rows = [r[0] for r in cur.fetchall()]
    assert rows == ["ZYXWVU098765"], rows


def test_by_uei_view_carries_uei_verbatim(
    fraud_db: psycopg.Connection,
) -> None:
    _insert_minimal(
        fraud_db,
        record_hash="a" * 64,
        classification="Firm",
        name="LITERAL UEI FIRM",
        uei="ABC123DEF456",
    )
    fraud_db.commit()
    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT sam_uei FROM derived.v_sam_exclusion_by_uei "
            "WHERE sam_record_hash = %s",
            ("a" * 64,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "ABC123DEF456"


# ============================================================================
# 4. Individual canonical view
# ============================================================================


def test_individual_canonical_includes_individuals(
    fraud_db: psycopg.Connection,
) -> None:
    _insert_minimal(
        fraud_db,
        record_hash="b" * 64,
        classification="Individual",
        last="DOE",
        first="JANE",
    )
    fraud_db.commit()
    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT canonical_key "
            "FROM derived.v_sam_exclusion_individual_canonical "
            "WHERE sam_record_hash = %s",
            ("b" * 64,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "DOE|JANE"


def test_individual_canonical_excludes_firm(
    fraud_db: psycopg.Connection,
) -> None:
    _insert_minimal(
        fraud_db,
        record_hash="c" * 64,
        classification="Firm",
        name="ACME LLC",
        last="ACME",  # firm with last set should still be excluded
        first="LLC",
    )
    fraud_db.commit()
    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT 1 "
            "FROM derived.v_sam_exclusion_individual_canonical "
            "WHERE sam_record_hash = %s",
            ("c" * 64,),
        )
        assert cur.fetchone() is None


def test_individual_canonical_excludes_vessel(
    fraud_db: psycopg.Connection,
) -> None:
    _insert_minimal(
        fraud_db,
        record_hash="d" * 64,
        classification="Vessel",
        sam_number="V-2024-XYZ",
    )
    fraud_db.commit()
    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT 1 "
            "FROM derived.v_sam_exclusion_individual_canonical "
            "WHERE sam_record_hash = %s",
            ("d" * 64,),
        )
        assert cur.fetchone() is None


def test_individual_canonical_excludes_special_entity(
    fraud_db: psycopg.Connection,
) -> None:
    _insert_minimal(
        fraud_db,
        record_hash="e" * 64,
        classification="Special Entity Designation",
        name="OFAC RECIPROCAL DESIGNATION",
    )
    fraud_db.commit()
    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT 1 "
            "FROM derived.v_sam_exclusion_individual_canonical "
            "WHERE sam_record_hash = %s",
            ("e" * 64,),
        )
        assert cur.fetchone() is None


def test_individual_canonical_skips_null_canonicalization(
    fraud_db: psycopg.Connection,
) -> None:
    """A row with empty/NULL last+first canonicalizes to NULL and
    is dropped."""
    _insert_minimal(
        fraud_db,
        record_hash="0" * 63 + "1",
        classification="Individual",
        name="UNPARSED",
        last=None,
        first=None,
    )
    fraud_db.commit()
    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT 1 "
            "FROM derived.v_sam_exclusion_individual_canonical "
            "WHERE sam_record_hash = %s",
            ("0" * 63 + "1",),
        )
        assert cur.fetchone() is None


def test_individual_canonical_matches_leie_canonical_function(
    fraud_db: psycopg.Connection,
) -> None:
    """The canonical key is the same f_canonical_lastfirst_split that
    LEIE uses, so cross-source matching by canonical_key is
    consistent across both lists."""
    _insert_minimal(
        fraud_db,
        record_hash="0" * 62 + "10",
        classification="Individual",
        last="O'BRIEN",
        first="MARY-JANE",
    )
    fraud_db.commit()
    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT canonical_key, "
            "       derived.f_canonical_lastfirst_split('O''BRIEN', "
            "                                           'MARY-JANE') "
            "FROM derived.v_sam_exclusion_individual_canonical "
            "WHERE sam_record_hash = %s",
            ("0" * 62 + "10",),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == row[1], (
            f"SAM canonical_key {row[0]} diverged from "
            f"f_canonical_lastfirst_split direct call {row[1]}"
        )
