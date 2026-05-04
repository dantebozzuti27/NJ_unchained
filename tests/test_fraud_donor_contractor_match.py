"""Tests for the donor_employed_by_nj_contractor cross-source signal.

Test taxonomy
-------------
1. Pure SQL canonicalization (live_pg, DDL-only fixtures)
   - f_canonical_employer_name: case, suffix stripping, punctuation
     collapse, NFKD-equivalent inputs
   - Equivalence cases that MUST collapse (TETRA TECH INC ==
     "Tetra Tech, L.L.C." == "tetra-tech inc.")
   - NULL semantics (STRICT)

2. Refresher integration (live_pg)
   - Idempotency: re-running for the same cycle does not duplicate
   - Cycle isolation: a 2024 refresh leaves 2020 rows alone
   - Match cardinality: known synthetic data produces exactly the
     expected (cycle, canonical_employer) rows
   - memo_cd='X' donations are filtered (not double-counted in
     raw_value)
   - Negative donations (refunds) do not contribute to raw_value
   - Empty USAspending side -> 0 matches
   - Empty FEC side -> 0 matches
   - Stop-shaped employer strings (RETIRED, SELF, NONE) self-filter
     because no real contractor name canonicalizes to them
   - Inactive USAspending rows (last_seen_at older than the active
     window) drop out and unmatch their donor clusters
   - peer_percentile uses CUME_DIST: top cluster = 1.0, bottom =
     1/N within the matched set
"""

from __future__ import annotations

import datetime as dt
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


def _seed_usaspending_recipient(
    conn: psycopg.Connection,
    *,
    award_id: str,
    recipient_name: str,
    pop_state: str = "NJ",
    award_amount: float = 100000.0,
    last_seen_at: dt.datetime | None = None,
) -> None:
    """Seed one USAspending NJ-pop contract row.

    The active-view 35-day window is anchored at MAX(last_seen_at);
    we accept an explicit last_seen_at to test stale-row exclusion.
    """
    with conn.cursor() as cur:
        if last_seen_at is None:
            cur.execute(
                "INSERT INTO raw.usaspending_award ("
                "  generated_unique_award_id, award_type_code, "
                "  recipient_name, pop_state, award_amount, "
                "  fiscal_year_pulled, api_query_filter_sha256"
                ") VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    award_id, "D", recipient_name, pop_state,
                    award_amount, 2024, "0" * 64,
                ),
            )
        else:
            cur.execute(
                "INSERT INTO raw.usaspending_award ("
                "  generated_unique_award_id, award_type_code, "
                "  recipient_name, pop_state, award_amount, "
                "  fiscal_year_pulled, api_query_filter_sha256, "
                "  fetched_at, last_seen_at"
                ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    award_id, "D", recipient_name, pop_state,
                    award_amount, 2024, "0" * 64,
                    last_seen_at, last_seen_at,
                ),
            )


def _seed_fec_contribution(
    conn: psycopg.Connection,
    *,
    sub_id: str,
    cycle: str,
    name: str,
    employer: str | None,
    transaction_amt: float,
    memo_cd: str | None = None,
) -> None:
    """Seed one FEC contribution row."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.fec_contribution ("
            " cycle, sub_id, name, employer, transaction_amt, memo_cd, "
            " source_url, source_sha256, source_vintage"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                cycle, sub_id, name, employer, transaction_amt, memo_cd,
                "test", "0" * 64, "test",
            ),
        )


def _refresh(conn: psycopg.Connection, cycle: str) -> int:
    """Run the refresher; commit; return rows_inserted."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_signal_donor_employed_by_nj_contractor(%s)",
            (cycle,),
        )
        row = cur.fetchone()
        n = int(row[0]) if row else 0
    conn.commit()
    return n


def _scalar(conn: psycopg.Connection, q: str, *args: object) -> object:
    """Run a single-value query, return the scalar."""
    with conn.cursor() as cur:
        cur.execute(q, args)
        row = cur.fetchone()
        return row[0] if row else None


# ============================================================================
# 1. Pure SQL canonicalization
# ============================================================================


def test_canonical_employer_name_basic_lowercase(
    fraud_db: psycopg.Connection,
) -> None:
    """Mixed-case names canonicalize to lowercase."""
    got = _scalar(
        fraud_db,
        "SELECT derived.f_canonical_employer_name(%s)",
        "Tetra Tech",
    )
    assert got == "tetra tech"


def test_canonical_employer_strips_business_suffixes(
    fraud_db: psycopg.Connection,
) -> None:
    """LLC, INC, CORP, LTD, etc. all strip to the bare name."""
    cases = [
        ("TETRA TECH LLC",          "tetra tech"),
        ("Tetra Tech Inc",          "tetra tech"),
        ("Tetra Tech Inc.",         "tetra tech"),
        ("Tetra Tech, Inc.",        "tetra tech"),
        ("Tetra Tech Incorporated", "tetra tech"),
        ("Tetra Tech Corp",         "tetra tech"),
        ("Tetra Tech Corp.",        "tetra tech"),
        ("Tetra Tech Corporation",  "tetra tech"),
        ("Tetra Tech Co.",          "tetra tech"),
        ("Tetra Tech Company",      "tetra tech"),
        ("Tetra Tech LTD.",         "tetra tech"),
        ("Tetra Tech Limited",      "tetra tech"),
        ("Tetra Tech LP",           "tetra tech"),
        ("Tetra Tech L.P.",         "tetra tech"),
        ("Tetra Tech LLP",          "tetra tech"),
        ("Tetra Tech PLLC",         "tetra tech"),
        ("Tetra Tech P.C.",         "tetra tech"),
        ("Tetra Tech Holdings",     "tetra tech"),
        ("The Tetra Tech Group",    "tetra tech"),
    ]
    for input_text, want in cases:
        got = _scalar(
            fraud_db,
            "SELECT derived.f_canonical_employer_name(%s)",
            input_text,
        )
        assert got == want, (
            f"{input_text!r}: got {got!r}, want {want!r}"
        )


def test_canonical_employer_dotted_suffix_variants_collapse(
    fraud_db: psycopg.Connection,
) -> None:
    """L.L.C., LLC, L.L.C must all collapse identically.

    This is the CRITICAL invariant for cross-source matching:
    USAspending publishes "TETRA TECH INC" while FEC self-reports may
    say "Tetra Tech L.L.C." or "tetra-tech, inc." -- the platform
    matches them all to the same canonical key or the cross-source
    join silently misses real corp identities.
    """
    forms = [
        "Tetra Tech LLC",
        "Tetra Tech L.L.C.",
        "Tetra Tech L.L.C",
        "tetra-tech llc",
        "TETRA TECH, LLC",
        "TETRA TECH L.L.C.",
        "TETRA TECH INCORPORATED",
        "Tetra Tech, Inc.",
    ]
    canonicals = []
    for f in forms:
        got = _scalar(
            fraud_db,
            "SELECT derived.f_canonical_employer_name(%s)",
            f,
        )
        canonicals.append(got)
    # All must collapse to the same key.
    assert len(set(canonicals)) == 1, (
        f"non-collapsing variants: {dict(zip(forms, canonicals, strict=True))}"
    )
    assert canonicals[0] == "tetra tech"


def test_canonical_employer_punctuation_collapses(
    fraud_db: psycopg.Connection,
) -> None:
    """Commas, hyphens, periods, multiple spaces all collapse to single space."""
    cases = [
        ("Tetra,Tech",       "tetra tech"),
        ("Tetra-Tech",       "tetra tech"),
        ("Tetra  Tech",      "tetra tech"),
        ("Tetra.Tech",       "tetra tech"),
        ("  Tetra   Tech  ", "tetra tech"),
    ]
    for input_text, want in cases:
        got = _scalar(
            fraud_db,
            "SELECT derived.f_canonical_employer_name(%s)",
            input_text,
        )
        assert got == want, f"{input_text!r}: got {got!r}, want {want!r}"


def test_canonical_employer_handles_null_strict(
    fraud_db: psycopg.Connection,
) -> None:
    """STRICT marker: NULL in -> NULL out without invoking the body."""
    got = _scalar(
        fraud_db,
        "SELECT derived.f_canonical_employer_name(%s::TEXT)",
        None,
    )
    assert got is None


def test_canonical_employer_empty_string_yields_empty_string(
    fraud_db: psycopg.Connection,
) -> None:
    """Empty input is non-NULL -> empty string output (not NULL)."""
    got = _scalar(
        fraud_db,
        "SELECT derived.f_canonical_employer_name('')",
    )
    assert got == ""


def test_canonical_employer_handles_distinct_names_distinctly(
    fraud_db: psycopg.Connection,
) -> None:
    """The canonicalizer must NOT collapse genuinely different names.

    A regression that strips too aggressively (e.g. dropping the word
    "tech" along with suffixes) would produce false-positive matches.
    """
    distinct = [
        "Tetra Tech",
        "Alpha Tech",
        "Bravo Tech",
        "Tetra Computing",
        "Tetra Corp",   # stripped suffix yields just "tetra"
    ]
    canonicals = []
    for n in distinct:
        got = _scalar(
            fraud_db,
            "SELECT derived.f_canonical_employer_name(%s)",
            n,
        )
        canonicals.append(got)
    # Tetra Corp -> "tetra"; the four others must be distinct.
    assert canonicals[0] == "tetra tech"
    assert canonicals[1] == "alpha tech"
    assert canonicals[2] == "bravo tech"
    assert canonicals[3] == "tetra computing"
    assert canonicals[4] == "tetra"
    assert len(set(canonicals)) == 5, (
        f"collapse: {dict(zip(distinct, canonicals, strict=True))}"
    )


# ============================================================================
# 2. Refresher integration -- happy paths
# ============================================================================


def test_refresh_produces_one_row_per_matched_employer(
    fraud_db: psycopg.Connection,
) -> None:
    """Three USAspending recipients, three matching donor clusters."""
    _seed_usaspending_recipient(
        fraud_db, award_id="CONT_AWD_001",
        recipient_name="Tetra Tech Inc",
    )
    _seed_usaspending_recipient(
        fraud_db, award_id="CONT_AWD_002",
        recipient_name="Acme Defense Systems LLC",
    )
    _seed_usaspending_recipient(
        fraud_db, award_id="CONT_AWD_003",
        recipient_name="Bravo Industries Corp",
    )

    # Tetra Tech: 3 donors, $3000 total
    _seed_fec_contribution(
        fraud_db, sub_id="A1", cycle="2024",
        name="DOE, JOHN", employer="Tetra Tech, L.L.C.",
        transaction_amt=1000.0,
    )
    _seed_fec_contribution(
        fraud_db, sub_id="A2", cycle="2024",
        name="ROE, JANE", employer="TETRA TECH INC",
        transaction_amt=1000.0,
    )
    _seed_fec_contribution(
        fraud_db, sub_id="A3", cycle="2024",
        name="DOE, JANE", employer="tetra-tech",
        transaction_amt=1000.0,
    )
    # Acme: 1 donor, $500
    _seed_fec_contribution(
        fraud_db, sub_id="B1", cycle="2024",
        name="SMITH, BOB", employer="Acme Defense Systems",
        transaction_amt=500.0,
    )
    # Bravo: 1 donor, $250
    _seed_fec_contribution(
        fraud_db, sub_id="C1", cycle="2024",
        name="JONES, ALICE", employer="Bravo Industries",
        transaction_amt=250.0,
    )
    # Non-matching donor: should NOT produce a signal row.
    _seed_fec_contribution(
        fraud_db, sub_id="D1", cycle="2024",
        name="WHITE, PAT", employer="Some Random Co",
        transaction_amt=750.0,
    )
    fraud_db.commit()

    n = _refresh(fraud_db, "2024")
    assert n == 3, f"expected 3 matched clusters, got {n}"

    rows = _scalar(
        fraud_db,
        "SELECT array_agg(entity_id ORDER BY entity_id) FROM "
        "derived.fraud_signal_observation WHERE cycle='2024' AND "
        "signal_id='donor_employed_by_nj_contractor'",
    )
    assert rows == ["acme defense systems", "bravo industries", "tetra tech"]

    # raw_value sums correctly per cluster
    tetra_amt = _scalar(
        fraud_db,
        "SELECT raw_value FROM derived.fraud_signal_observation "
        "WHERE cycle='2024' AND entity_id='tetra tech' "
        "AND signal_id='donor_employed_by_nj_contractor'",
    )
    assert float(tetra_amt) == 3000.0  # type: ignore[arg-type]

    # Severity is fixed at 3
    sev = _scalar(
        fraud_db,
        "SELECT DISTINCT severity FROM derived.fraud_signal_observation "
        "WHERE cycle='2024' AND signal_id='donor_employed_by_nj_contractor'",
    )
    assert sev == 3


def test_refresh_canonicalizes_both_sides_for_match(
    fraud_db: psycopg.Connection,
) -> None:
    """USAspending 'Tetra Tech Inc' must match FEC employer 'tetra-tech, l.l.c.'.

    This is the cross-source contract: aggressive corporate-suffix
    variation between the two sources must canonicalize to the same
    key. A regression that fails to strip "INC" on USAspending side
    or "L.L.C." on FEC side would silently fail to match.
    """
    _seed_usaspending_recipient(
        fraud_db, award_id="CONT_AWD_001",
        recipient_name="Tetra Tech Inc",
    )
    _seed_fec_contribution(
        fraud_db, sub_id="A1", cycle="2024",
        name="DOE, JOHN", employer="tetra-tech, l.l.c.",
        transaction_amt=1000.0,
    )
    fraud_db.commit()

    n = _refresh(fraud_db, "2024")
    assert n == 1


def test_refresh_filters_memo_cd_x_double_counts(
    fraud_db: psycopg.Connection,
) -> None:
    """memo_cd='X' rows are sub-line itemizations and must NOT be summed.

    FEC bulk-data convention: memo_cd='X' rows are itemized splits of
    a parent transaction. Summing both produces 2x raw_value. The
    refresher's WHERE clause must filter them out.
    """
    _seed_usaspending_recipient(
        fraud_db, award_id="CONT_AWD_001",
        recipient_name="Tetra Tech",
    )
    # Parent transaction: $5000
    _seed_fec_contribution(
        fraud_db, sub_id="P1", cycle="2024",
        name="DOE, JOHN", employer="Tetra Tech",
        transaction_amt=5000.0, memo_cd=None,
    )
    # Memo sub-line: $2500 (would double-count if not filtered)
    _seed_fec_contribution(
        fraud_db, sub_id="M1", cycle="2024",
        name="DOE, JOHN", employer="Tetra Tech",
        transaction_amt=2500.0, memo_cd="X",
    )
    fraud_db.commit()

    _refresh(fraud_db, "2024")
    raw_value = _scalar(
        fraud_db,
        "SELECT raw_value FROM derived.fraud_signal_observation "
        "WHERE cycle='2024' AND entity_id='tetra tech' AND "
        "signal_id='donor_employed_by_nj_contractor'",
    )
    # Only the parent counts: $5000, NOT $7500.
    assert float(raw_value) == 5000.0  # type: ignore[arg-type]


def test_refresh_excludes_negative_donations_from_raw_value(
    fraud_db: psycopg.Connection,
) -> None:
    """Refunds (negative transaction_amt) do not contribute to raw_value.

    Negative-only clusters drop out entirely (raw_value floor > 0 in
    the refresher's WHERE clause).
    """
    _seed_usaspending_recipient(
        fraud_db, award_id="CONT_AWD_001",
        recipient_name="Tetra Tech",
    )
    # Refund only: $-500
    _seed_fec_contribution(
        fraud_db, sub_id="R1", cycle="2024",
        name="DOE, JOHN", employer="Tetra Tech",
        transaction_amt=-500.0,
    )
    fraud_db.commit()
    n = _refresh(fraud_db, "2024")
    assert n == 0, "negative-only cluster should not generate a signal row"

    # Now add a positive donation: $1000. Cluster total = $1000-$500 = $500
    # but our SUM(GREATEST(amt, 0)) keeps only the positive part = $1000.
    _seed_fec_contribution(
        fraud_db, sub_id="P1", cycle="2024",
        name="DOE, JOHN", employer="Tetra Tech",
        transaction_amt=1000.0,
    )
    fraud_db.commit()
    _refresh(fraud_db, "2024")
    raw_value = _scalar(
        fraud_db,
        "SELECT raw_value FROM derived.fraud_signal_observation "
        "WHERE cycle='2024' AND entity_id='tetra tech' AND "
        "signal_id='donor_employed_by_nj_contractor'",
    )
    assert float(raw_value) == 1000.0  # type: ignore[arg-type]


def test_refresh_is_idempotent(fraud_db: psycopg.Connection) -> None:
    """Calling refresh twice for the same cycle yields the same row count."""
    _seed_usaspending_recipient(
        fraud_db, award_id="CONT_AWD_001",
        recipient_name="Tetra Tech",
    )
    _seed_fec_contribution(
        fraud_db, sub_id="A1", cycle="2024",
        name="DOE, JOHN", employer="Tetra Tech",
        transaction_amt=1000.0,
    )
    fraud_db.commit()

    n1 = _refresh(fraud_db, "2024")
    n2 = _refresh(fraud_db, "2024")
    assert n1 == n2 == 1

    total_rows = _scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle='2024' AND signal_id='donor_employed_by_nj_contractor'",
    )
    assert total_rows == 1


def test_refresh_isolates_cycles(fraud_db: psycopg.Connection) -> None:
    """A 2024 refresh leaves 2020 rows untouched."""
    _seed_usaspending_recipient(
        fraud_db, award_id="CONT_AWD_001",
        recipient_name="Tetra Tech",
    )
    _seed_fec_contribution(
        fraud_db, sub_id="A1", cycle="2020",
        name="DOE, JOHN", employer="Tetra Tech",
        transaction_amt=1000.0,
    )
    _seed_fec_contribution(
        fraud_db, sub_id="A2", cycle="2024",
        name="ROE, JANE", employer="Tetra Tech",
        transaction_amt=2000.0,
    )
    fraud_db.commit()

    _refresh(fraud_db, "2020")
    _refresh(fraud_db, "2024")

    n_2020 = _scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle='2020' AND signal_id='donor_employed_by_nj_contractor'",
    )
    n_2024 = _scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle='2024' AND signal_id='donor_employed_by_nj_contractor'",
    )
    assert n_2020 == 1
    assert n_2024 == 1

    # Re-refresh 2024 only: 2020 must remain.
    _refresh(fraud_db, "2024")
    n_2020_again = _scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle='2020' AND signal_id='donor_employed_by_nj_contractor'",
    )
    assert n_2020_again == 1


def test_refresh_percentile_uses_cume_dist(
    fraud_db: psycopg.Connection,
) -> None:
    """CUME_DIST: bottom = 1/N, top = 1.0.

    With three matched clusters of distinct sizes:
        small (raw_value=100)  -> CUME_DIST = 1/3 = 0.333...
        medium (raw_value=500) -> CUME_DIST = 2/3 = 0.666...
        big (raw_value=2500)   -> CUME_DIST = 3/3 = 1.0
    """
    for award_id, name in [
        ("CONT_AWD_001", "Small Inc"),
        ("CONT_AWD_002", "Medium Inc"),
        ("CONT_AWD_003", "Big Inc"),
    ]:
        _seed_usaspending_recipient(
            fraud_db, award_id=award_id, recipient_name=name,
        )

    _seed_fec_contribution(
        fraud_db, sub_id="A1", cycle="2024",
        name="DOE, JOHN", employer="Small Inc",
        transaction_amt=100.0,
    )
    _seed_fec_contribution(
        fraud_db, sub_id="B1", cycle="2024",
        name="ROE, JANE", employer="Medium Inc",
        transaction_amt=500.0,
    )
    _seed_fec_contribution(
        fraud_db, sub_id="C1", cycle="2024",
        name="WHITE, PAT", employer="Big Inc",
        transaction_amt=2500.0,
    )
    fraud_db.commit()
    _refresh(fraud_db, "2024")

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT entity_id, peer_percentile FROM "
            "derived.fraud_signal_observation "
            "WHERE cycle='2024' AND signal_id='donor_employed_by_nj_contractor' "
            "ORDER BY raw_value",
        )
        rows = cur.fetchall()
    assert len(rows) == 3
    # Bottom: 1/3 -> CUME_DIST returns 0.333...
    assert abs(float(rows[0][1]) - 1.0 / 3.0) < 1e-9, rows
    # Middle: 2/3
    assert abs(float(rows[1][1]) - 2.0 / 3.0) < 1e-9, rows
    # Top: 3/3 = 1.0
    assert abs(float(rows[2][1]) - 1.0) < 1e-9, rows


def test_refresh_percentile_single_match_is_one(
    fraud_db: psycopg.Connection,
) -> None:
    """A single matched cluster has CUME_DIST = 1.0 (it IS the bucket)."""
    _seed_usaspending_recipient(
        fraud_db, award_id="CONT_AWD_001",
        recipient_name="Tetra Tech",
    )
    _seed_fec_contribution(
        fraud_db, sub_id="A1", cycle="2024",
        name="DOE, JOHN", employer="Tetra Tech",
        transaction_amt=1000.0,
    )
    fraud_db.commit()
    _refresh(fraud_db, "2024")

    pctile = _scalar(
        fraud_db,
        "SELECT peer_percentile FROM derived.fraud_signal_observation "
        "WHERE cycle='2024' AND signal_id='donor_employed_by_nj_contractor'",
    )
    assert abs(float(pctile) - 1.0) < 1e-9  # type: ignore[arg-type]


# ============================================================================
# 3. Refresher integration -- edge cases
# ============================================================================


def test_refresh_with_empty_usaspending_returns_zero_matches(
    fraud_db: psycopg.Connection,
) -> None:
    """No USAspending rows -> no matches even if FEC has employers."""
    _seed_fec_contribution(
        fraud_db, sub_id="A1", cycle="2024",
        name="DOE, JOHN", employer="Tetra Tech",
        transaction_amt=1000.0,
    )
    fraud_db.commit()
    n = _refresh(fraud_db, "2024")
    assert n == 0


def test_refresh_with_empty_fec_returns_zero_matches(
    fraud_db: psycopg.Connection,
) -> None:
    """No FEC contributions -> no matches even if USAspending has rows."""
    _seed_usaspending_recipient(
        fraud_db, award_id="CONT_AWD_001",
        recipient_name="Tetra Tech",
    )
    fraud_db.commit()
    n = _refresh(fraud_db, "2024")
    assert n == 0


def test_refresh_does_not_match_self_employed_or_retired(
    fraud_db: psycopg.Connection,
) -> None:
    """FEC's placeholder employer values self-filter.

    'RETIRED' / 'SELF EMPLOYED' / 'NONE' / 'HOMEMAKER' do not match
    any real federal contractor name. A canonicalization regression
    that ALSO appears on the USAspending side (e.g., a contractor
    literally named 'Self Employed Consulting LLC' canonicalizing to
    'self employed consulting') would create real matches and is
    expected: that's a real overlap, not a false positive.
    """
    for placeholder in [
        "RETIRED", "SELF EMPLOYED", "SELF-EMPLOYED", "NONE",
        "HOMEMAKER", "STUDENT", "UNEMPLOYED", "N/A",
        "INFORMATION REQUESTED",
    ]:
        _seed_fec_contribution(
            fraud_db, sub_id=f"PLH_{placeholder[:6]}", cycle="2024",
            name="DOE, JOHN", employer=placeholder,
            transaction_amt=100.0,
        )
    # Real contractor on USAspending side that does NOT match any
    # placeholder.
    _seed_usaspending_recipient(
        fraud_db, award_id="CONT_AWD_001",
        recipient_name="Tetra Tech",
    )
    fraud_db.commit()
    n = _refresh(fraud_db, "2024")
    assert n == 0, (
        "placeholder employers must not match real contractors"
    )


def test_refresh_drops_inactive_usaspending_rows(
    fraud_db: psycopg.Connection,
) -> None:
    """A USAspending row whose last_seen_at is >35 days stale falls out.

    The refresher reads from derived.v_usaspending_award_active, which
    filters last_seen_at >= MAX(last_seen_at) - 35 days. A stale row
    that no longer appears in the freshest pull stops contributing to
    the contractor-employer set, and its donor cluster unmatches.
    """
    now = dt.datetime.now(dt.UTC)
    fresh = now
    stale = now - dt.timedelta(days=60)

    # Active row: a fresh contractor.
    _seed_usaspending_recipient(
        fraud_db, award_id="CONT_AWD_FRESH",
        recipient_name="Fresh Contractor",
        last_seen_at=fresh,
    )
    # Inactive row: a stale contractor (last_seen_at > 35 days ago
    # relative to MAX(last_seen_at) = `fresh`).
    _seed_usaspending_recipient(
        fraud_db, award_id="CONT_AWD_STALE",
        recipient_name="Stale Contractor",
        last_seen_at=stale,
    )
    # FEC donors at both.
    _seed_fec_contribution(
        fraud_db, sub_id="A1", cycle="2024",
        name="DOE, JOHN", employer="Fresh Contractor",
        transaction_amt=1000.0,
    )
    _seed_fec_contribution(
        fraud_db, sub_id="A2", cycle="2024",
        name="ROE, JANE", employer="Stale Contractor",
        transaction_amt=2000.0,
    )
    fraud_db.commit()

    _refresh(fraud_db, "2024")
    rows = _scalar(
        fraud_db,
        "SELECT array_agg(entity_id ORDER BY entity_id) "
        "FROM derived.fraud_signal_observation "
        "WHERE cycle='2024' AND signal_id='donor_employed_by_nj_contractor'",
    )
    # Only the fresh contractor's cluster matches.
    assert rows == ["fresh contractor"]


def test_refresh_evidence_url_is_well_formed(
    fraud_db: psycopg.Connection,
) -> None:
    """evidence_url has the expected /fec/risk/entities/donor_cluster/... shape."""
    _seed_usaspending_recipient(
        fraud_db, award_id="CONT_AWD_001",
        recipient_name="Tetra Tech",
    )
    _seed_fec_contribution(
        fraud_db, sub_id="A1", cycle="2024",
        name="DOE, JOHN", employer="Tetra Tech",
        transaction_amt=1000.0,
    )
    fraud_db.commit()
    _refresh(fraud_db, "2024")
    url = _scalar(
        fraud_db,
        "SELECT evidence_url FROM derived.fraud_signal_observation "
        "WHERE cycle='2024' AND entity_id='tetra tech' AND "
        "signal_id='donor_employed_by_nj_contractor'",
    )
    assert isinstance(url, str)
    assert url.startswith("/fec/risk/entities/donor_cluster/")
    assert "signal=donor_employed_by_nj_contractor" in url
    assert "cycle=2024" in url


def test_refresh_aggregates_multiple_canonical_variants_of_same_employer(
    fraud_db: psycopg.Connection,
) -> None:
    """5 donors at 5 different surface-form variants of the same employer
    must aggregate to a single cluster of $500 (not 5 clusters of $100).
    """
    _seed_usaspending_recipient(
        fraud_db, award_id="CONT_AWD_001",
        recipient_name="Tetra Tech",
    )
    variants = [
        "Tetra Tech",
        "Tetra Tech Inc",
        "Tetra Tech, LLC",
        "TETRA TECH L.L.C.",
        "tetra-tech corp",
    ]
    for i, variant in enumerate(variants):
        _seed_fec_contribution(
            fraud_db, sub_id=f"V{i}", cycle="2024",
            name=f"DONOR{i}, X", employer=variant,
            transaction_amt=100.0,
        )
    fraud_db.commit()
    _refresh(fraud_db, "2024")

    n_clusters = _scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle='2024' AND entity_id='tetra tech' AND "
        "signal_id='donor_employed_by_nj_contractor'",
    )
    assert n_clusters == 1
    raw_value = _scalar(
        fraud_db,
        "SELECT raw_value FROM derived.fraud_signal_observation "
        "WHERE cycle='2024' AND entity_id='tetra tech' AND "
        "signal_id='donor_employed_by_nj_contractor'",
    )
    assert float(raw_value) == 500.0  # type: ignore[arg-type]
