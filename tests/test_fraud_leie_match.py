"""Tests for the entity_on_leie cross-source signal (FRAUD-F5b).

Test taxonomy
-------------
1. Pure SQL canonicalization (live_pg, but DDL-only fixtures)
   - f_normalize_name_token: case, punctuation, suffixes, NULL semantics
   - f_canonical_lastfirst_split: hyphenation, middle-name handling,
     NULL on degenerate inputs
   - f_canonical_lastfirst_from_fec: comma split, no-comma -> NULL

2. Refresher integration (live_pg)
   - Idempotency: re-running for the same cycle does not duplicate rows
   - Cycle isolation: a 2024 refresh does not touch 2020 entity_on_leie
   - Match cardinality: known-construction synthetic data produces
     exactly the expected (cand_id, leie_record_hash) cross-product
   - DISTINCT ON: an FEC entity matching multiple LEIE entries
     produces one row per FEC entity, not one per LEIE entry
   - Empty LEIE -> 0 matches (no error)
   - Empty FEC for a cycle -> 0 matches (no error)
   - Both empty -> 0 matches (no error)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import psycopg


pytestmark = pytest.mark.live_pg


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def fraud_db(live_pg: psycopg.Connection) -> psycopg.Connection:
    """Apply all migrations + seeds; yield a fresh-schema conn."""
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


def _seed_leie_individual(
    conn: psycopg.Connection,
    *,
    record_hash: str,
    lastname: str,
    firstname: str,
    state: str | None = "NJ",
    excldate: str = "20180515",
    excltype: str = "1128A1",
    midname: str | None = None,
) -> None:
    """Seed one LEIE individual row directly (bypassing the ingester)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.hhs_oig_leie ("
            " record_hash, lastname, firstname, midname, busname, "
            " excltype, excldate, "
            " state, "
            " vintage_month, source_url, source_sha256"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                record_hash,
                lastname, firstname, midname, None,
                excltype, excldate,
                state,
                "2026-03",
                "https://example.test/UPDATED.csv",
                "0" * 64,
            ),
        )


def _seed_fec_candidate(
    conn: psycopg.Connection,
    *,
    cycle: str,
    cand_id: str,
    cand_name: str,
    cand_office: str = "S",
    cand_office_st: str = "NJ",
) -> None:
    """Seed one FEC candidate row."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.fec_candidate ("
            " cycle, cand_id, cand_name, cand_office, cand_office_st, "
            " source_url, source_sha256, source_vintage"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                cycle, cand_id, cand_name, cand_office, cand_office_st,
                "test", "0" * 64, "test",
            ),
        )


def _seed_fec_committee(
    conn: psycopg.Connection,
    *,
    cycle: str,
    cmte_id: str,
    cmte_nm: str,
    tres_nm: str,
    cmte_st: str = "NJ",
) -> None:
    """Seed one FEC committee row."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.fec_committee ("
            " cycle, cmte_id, cmte_nm, tres_nm, cmte_st, "
            " source_url, source_sha256, source_vintage"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                cycle, cmte_id, cmte_nm, tres_nm, cmte_st,
                "test", "0" * 64, "test",
            ),
        )


def _scalar(conn: psycopg.Connection, q: str, *args: object) -> object:
    """Run a single-value query, return the value (or None)."""
    with conn.cursor() as cur:
        cur.execute(q, args)
        row = cur.fetchone()
        return row[0] if row else None


# ============================================================================
# 1. Pure SQL canonicalization
# ============================================================================


def test_normalize_name_token_uppercases_and_strips_punctuation(
    fraud_db: psycopg.Connection,
) -> None:
    cases = [
        ("doe", "DOE"),
        ("  Doe  ", "DOE"),
        ("D.O.E.", "D O E"),
        ("Smith-Jones", "SMITH-JONES"),
        ("O'Connor", "O'CONNOR"),
        ("MacDonald", "MACDONALD"),
        ("a.b. C", "A B C"),
    ]
    for input_text, want in cases:
        got = _scalar(
            fraud_db,
            "SELECT derived.f_normalize_name_token(%s)",
            input_text,
        )
        assert got == want, f"{input_text!r}: got {got!r}, want {want!r}"


def test_normalize_name_token_returns_null_on_empty_input(
    fraud_db: psycopg.Connection,
) -> None:
    for empty in [None, "", "    ", "...", "!@#"]:
        got = _scalar(
            fraud_db,
            "SELECT derived.f_normalize_name_token(%s)",
            empty,
        )
        assert got is None, f"{empty!r}: expected NULL, got {got!r}"


def test_normalize_name_token_drops_jr_sr_roman_suffixes(
    fraud_db: psycopg.Connection,
) -> None:
    cases = [
        ("DOE JR",    "DOE"),
        ("DOE SR",    "DOE"),
        ("DOE II",    "DOE"),
        ("DOE III",   "DOE"),
        ("DOE IV",    "DOE"),
        ("DOE V",     "DOE"),
        # Suffix is only stripped at end-of-string; embedded "JR" stays.
        ("JR DOE",    "JR DOE"),
        # Multi-token last-names with suffix.
        ("MacDonald JR", "MACDONALD"),
    ]
    for input_text, want in cases:
        got = _scalar(
            fraud_db,
            "SELECT derived.f_normalize_name_token(%s)",
            input_text,
        )
        assert got == want, f"{input_text!r}: got {got!r}, want {want!r}"


def test_canonical_lastfirst_split_basic(fraud_db: psycopg.Connection) -> None:
    got = _scalar(
        fraud_db,
        "SELECT derived.f_canonical_lastfirst_split(%s, %s)",
        "Doe", "Jane",
    )
    assert got == "DOE|JANE"


def test_canonical_lastfirst_split_drops_middle_name(
    fraud_db: psycopg.Connection,
) -> None:
    """LEIE has 'JANE A', FEC has 'JANE'; both must collapse to 'JANE'."""
    a = _scalar(
        fraud_db,
        "SELECT derived.f_canonical_lastfirst_split(%s, %s)",
        "Doe", "Jane A",
    )
    b = _scalar(
        fraud_db,
        "SELECT derived.f_canonical_lastfirst_split(%s, %s)",
        "Doe", "Jane",
    )
    assert a == b == "DOE|JANE"


def test_canonical_lastfirst_split_returns_null_on_missing_parts(
    fraud_db: psycopg.Connection,
) -> None:
    for last, first in [(None, "Jane"), ("Doe", None), ("", "Jane"),
                        ("Doe", ""), ("...", "Jane"), ("Doe", "...")]:
        got = _scalar(
            fraud_db,
            "SELECT derived.f_canonical_lastfirst_split(%s, %s)",
            last, first,
        )
        assert got is None, f"({last!r}, {first!r}) -> {got!r}, want NULL"


def test_canonical_lastfirst_from_fec_basic(
    fraud_db: psycopg.Connection,
) -> None:
    cases = [
        ("DOE, JANE",       "DOE|JANE"),
        ("DOE, JANE A",     "DOE|JANE"),
        ("doe, jane a",     "DOE|JANE"),
        ("MacDonald, John", "MACDONALD|JOHN"),
        ("Smith-Jones, Mary", "SMITH-JONES|MARY"),
        ("O'Connor, Terry", "O'CONNOR|TERRY"),
        ("DOE JR, JANE",    "DOE|JANE"),
    ]
    for fec_name, want in cases:
        got = _scalar(
            fraud_db,
            "SELECT derived.f_canonical_lastfirst_from_fec(%s)",
            fec_name,
        )
        assert got == want, f"{fec_name!r}: got {got!r}, want {want!r}"


def test_canonical_lastfirst_from_fec_returns_null_when_no_comma(
    fraud_db: psycopg.Connection,
) -> None:
    """No comma = org-shaped name; should NOT match LEIE individuals."""
    for fec_name in [
        None,
        "FRIENDS OF JANE DOE",   # committee-style, no comma
        "JANE DOE",              # rare malformed-individual row
        "ACME CORP",
    ]:
        got = _scalar(
            fraud_db,
            "SELECT derived.f_canonical_lastfirst_from_fec(%s)",
            fec_name,
        )
        assert got is None, f"{fec_name!r}: expected NULL, got {got!r}"


def test_canonical_lastfirst_fec_and_split_agree(
    fraud_db: psycopg.Connection,
) -> None:
    """The two canonicalizers must produce the same key on equivalent inputs.

    This is the cornerstone of the LEIE-FEC join: if these disagree on
    a real-world equivalent, the join silently drops the match.
    """
    pairs = [
        ("DOE, JANE",         ("DOE", "JANE")),
        ("DOE JR, JANE A",    ("DOE", "JANE")),
        ("Smith-Jones, Mary", ("Smith-Jones", "Mary")),
        ("O'Connor, Terry",   ("O'Connor", "Terry")),
    ]
    for fec_name, (last, first) in pairs:
        a = _scalar(
            fraud_db,
            "SELECT derived.f_canonical_lastfirst_from_fec(%s)",
            fec_name,
        )
        b = _scalar(
            fraud_db,
            "SELECT derived.f_canonical_lastfirst_split(%s, %s)",
            last, first,
        )
        assert a == b, f"{fec_name!r} vs ({last!r}, {first!r}): {a!r} != {b!r}"


# ============================================================================
# 2. Refresher integration
# ============================================================================


def test_refresh_signal_entity_on_leie_inserts_no_rows_when_both_empty(
    fraud_db: psycopg.Connection,
) -> None:
    """Empty FEC and empty LEIE -> 0 matches; no error."""
    n = _scalar(
        fraud_db,
        "SELECT derived.refresh_signal_entity_on_leie(%s)",
        "2024",
    )
    assert n == 0


def test_refresh_signal_entity_on_leie_inserts_no_rows_when_leie_empty(
    fraud_db: psycopg.Connection,
) -> None:
    """FEC populated, LEIE empty -> 0 matches; no error."""
    _seed_fec_candidate(
        fraud_db, cycle="2024", cand_id="H4NJ01000",
        cand_name="DOE, JANE", cand_office="H", cand_office_st="NJ",
    )
    fraud_db.commit()

    n = _scalar(
        fraud_db,
        "SELECT derived.refresh_signal_entity_on_leie(%s)",
        "2024",
    )
    assert n == 0


def test_refresh_signal_entity_on_leie_matches_candidate(
    fraud_db: psycopg.Connection,
) -> None:
    """Single FEC candidate matches single LEIE individual -> 1 row."""
    _seed_leie_individual(
        fraud_db,
        record_hash="a" * 64,
        lastname="DOE", firstname="JANE",
    )
    _seed_fec_candidate(
        fraud_db, cycle="2024", cand_id="H4NJ01000",
        cand_name="DOE, JANE A",  # middle initial drops in canon
        cand_office="H", cand_office_st="NJ",
    )
    fraud_db.commit()

    n = _scalar(
        fraud_db,
        "SELECT derived.refresh_signal_entity_on_leie(%s)",
        "2024",
    )
    assert n == 1

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT entity_kind, entity_id, signal_id, severity, peer_bucket "
            "FROM derived.fraud_signal_observation "
            "WHERE signal_id = 'entity_on_leie'",
        )
        rows = cur.fetchall()
    assert rows == [
        ("candidate", "H4NJ01000", "entity_on_leie", 5, "kind=candidate"),
    ]


def test_refresh_signal_entity_on_leie_matches_treasurer(
    fraud_db: psycopg.Connection,
) -> None:
    """A treasurer's name in tres_nm matches a LEIE entry."""
    _seed_leie_individual(
        fraud_db,
        record_hash="b" * 64,
        lastname="ROE", firstname="JOHN",
    )
    _seed_fec_committee(
        fraud_db, cycle="2024", cmte_id="C00500001",
        cmte_nm="FRIENDS OF JANE DOE",
        tres_nm="ROE, JOHN P",
    )
    fraud_db.commit()

    n = _scalar(
        fraud_db,
        "SELECT derived.refresh_signal_entity_on_leie(%s)",
        "2024",
    )
    assert n == 1

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT entity_kind, entity_id, signal_id, severity, peer_bucket "
            "FROM derived.fraud_signal_observation "
            "WHERE signal_id = 'entity_on_leie'",
        )
        rows = cur.fetchall()
    # entity_id for treasurer = canonical tres_nm (matches the existing
    # structural-signal canonicalization so the L2 pivot lines up).
    assert rows == [
        ("treasurer", "ROE, JOHN P", "entity_on_leie", 5, "kind=treasurer"),
    ]


def test_refresh_signal_entity_on_leie_collapses_multiple_leie_per_fec_entity(
    fraud_db: psycopg.Connection,
) -> None:
    """An FEC entity matching multiple LEIE rows -> one observation row."""
    # Two LEIE individuals share canonical "DOE|JANE" (different middle).
    _seed_leie_individual(
        fraud_db, record_hash="1" * 64,
        lastname="DOE", firstname="JANE A",
        excldate="20180515",
    )
    _seed_leie_individual(
        fraud_db, record_hash="2" * 64,
        lastname="DOE", firstname="JANE M",
        excldate="20210101",  # newer
    )
    _seed_fec_candidate(
        fraud_db, cycle="2024", cand_id="H4NJ01000",
        cand_name="DOE, JANE",
    )
    fraud_db.commit()

    n = _scalar(
        fraud_db,
        "SELECT derived.refresh_signal_entity_on_leie(%s)",
        "2024",
    )
    assert n == 1, "DISTINCT ON should collapse to one row per FEC entity"

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT evidence_url FROM derived.fraud_signal_observation "
            "WHERE signal_id = 'entity_on_leie'",
        )
        url = cur.fetchone()[0]  # type: ignore[index]
    # The kept LEIE record should be the one with the most recent excldate.
    assert "leie=" + ("2" * 64) in url


def test_refresh_signal_entity_on_leie_does_not_match_org_shaped_names(
    fraud_db: psycopg.Connection,
) -> None:
    """A no-comma cand_name (org-shaped) does NOT match a LEIE individual."""
    _seed_leie_individual(
        fraud_db, record_hash="3" * 64,
        lastname="ACME", firstname="CORP",
    )
    _seed_fec_candidate(
        fraud_db, cycle="2024", cand_id="H4NJ01000",
        cand_name="ACME CORP",  # no comma -> NULL canonical -> no match
    )
    fraud_db.commit()

    n = _scalar(
        fraud_db,
        "SELECT derived.refresh_signal_entity_on_leie(%s)",
        "2024",
    )
    assert n == 0


def test_refresh_signal_entity_on_leie_is_idempotent(
    fraud_db: psycopg.Connection,
) -> None:
    """Re-running the refresher does not duplicate rows."""
    _seed_leie_individual(
        fraud_db, record_hash="4" * 64,
        lastname="DOE", firstname="JANE",
    )
    _seed_fec_candidate(
        fraud_db, cycle="2024", cand_id="H4NJ01000",
        cand_name="DOE, JANE",
    )
    fraud_db.commit()

    n1 = _scalar(
        fraud_db,
        "SELECT derived.refresh_signal_entity_on_leie(%s)",
        "2024",
    )
    n2 = _scalar(
        fraud_db,
        "SELECT derived.refresh_signal_entity_on_leie(%s)",
        "2024",
    )
    assert n1 == n2 == 1

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM derived.fraud_signal_observation "
            "WHERE signal_id = 'entity_on_leie'",
        )
        row = cur.fetchone()
    assert row is not None and row[0] == 1


def test_refresh_signal_entity_on_leie_isolates_cycles(
    fraud_db: psycopg.Connection,
) -> None:
    """Refreshing 2024 must not touch 2020 entity_on_leie rows."""
    _seed_leie_individual(
        fraud_db, record_hash="5" * 64,
        lastname="DOE", firstname="JANE",
    )
    _seed_fec_candidate(
        fraud_db, cycle="2024", cand_id="H4NJ01000",
        cand_name="DOE, JANE",
    )
    _seed_fec_candidate(
        fraud_db, cycle="2020", cand_id="H0NJ01000",
        cand_name="DOE, JANE",
    )
    fraud_db.commit()

    # Refresh 2024 only.
    _scalar(
        fraud_db,
        "SELECT derived.refresh_signal_entity_on_leie(%s)",
        "2024",
    )

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT cycle, COUNT(*) FROM derived.fraud_signal_observation "
            "WHERE signal_id = 'entity_on_leie' GROUP BY cycle",
        )
        rows: dict[str, int] = {str(r[0]): int(r[1]) for r in cur.fetchall()}
    assert rows == {"2024": 1}, "2020 must not have been touched"

    # Now refresh 2020. 2024 must remain unchanged.
    _scalar(
        fraud_db,
        "SELECT derived.refresh_signal_entity_on_leie(%s)",
        "2020",
    )
    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT cycle, COUNT(*) FROM derived.fraud_signal_observation "
            "WHERE signal_id = 'entity_on_leie' GROUP BY cycle "
            "ORDER BY cycle",
        )
        rows = {str(r[0]): int(r[1]) for r in cur.fetchall()}
    assert rows == {"2020": 1, "2024": 1}


def test_refresh_signal_entity_on_leie_peer_percentile_is_rate_based(
    fraud_db: psycopg.Connection,
) -> None:
    """peer_percentile reflects how rare the match is in its bucket.

    With 1 matched candidate out of 4 total candidates in the cycle,
    the rate-based percentile is 1 - (1/4) = 0.75. Common matches are
    less informative; this is the same pattern the structural binary
    signals use.
    """
    _seed_leie_individual(
        fraud_db, record_hash="6" * 64,
        lastname="DOE", firstname="JANE",
    )
    # One matching + three non-matching candidates in cycle 2024.
    _seed_fec_candidate(fraud_db, cycle="2024", cand_id="H4NJ01001",
                        cand_name="DOE, JANE")
    _seed_fec_candidate(fraud_db, cycle="2024", cand_id="H4NJ01002",
                        cand_name="SMITH, ALICE")
    _seed_fec_candidate(fraud_db, cycle="2024", cand_id="H4NJ01003",
                        cand_name="JONES, BOB")
    _seed_fec_candidate(fraud_db, cycle="2024", cand_id="H4NJ01004",
                        cand_name="WILSON, CARL")
    fraud_db.commit()

    _scalar(
        fraud_db,
        "SELECT derived.refresh_signal_entity_on_leie(%s)",
        "2024",
    )

    pct = _scalar(
        fraud_db,
        "SELECT peer_percentile FROM derived.fraud_signal_observation "
        "WHERE signal_id = 'entity_on_leie' AND entity_kind = 'candidate'",
    )
    assert pct is not None
    # _scalar returns object; psycopg surfaces NUMERIC as Decimal, both
    # of which float() accepts. The cast is informational, not coercive.
    assert abs(float(pct) - 0.75) < 1e-9, f"got {pct}"  # type: ignore[arg-type]


def test_refresh_signal_entity_on_leie_does_not_double_count_treasurer_across_committees(
    fraud_db: psycopg.Connection,
) -> None:
    """A treasurer running multiple committees yields ONE observation row.

    The structural treasurer_concentration signal already enforces this
    invariant (one row per treasurer); the LEIE refresher must follow
    the same contract so the L2 pivot lines up correctly.
    """
    _seed_leie_individual(
        fraud_db, record_hash="7" * 64,
        lastname="ROE", firstname="JOHN",
    )
    _seed_fec_committee(fraud_db, cycle="2024", cmte_id="C00000001",
                        cmte_nm="CMTE A", tres_nm="ROE, JOHN P")
    _seed_fec_committee(fraud_db, cycle="2024", cmte_id="C00000002",
                        cmte_nm="CMTE B", tres_nm="ROE, JOHN P")
    _seed_fec_committee(fraud_db, cycle="2024", cmte_id="C00000003",
                        cmte_nm="CMTE C", tres_nm="ROE, JOHN P")
    fraud_db.commit()

    n = _scalar(
        fraud_db,
        "SELECT derived.refresh_signal_entity_on_leie(%s)",
        "2024",
    )
    assert n == 1, "DISTINCT ON tres_canonical should collapse to 1"
