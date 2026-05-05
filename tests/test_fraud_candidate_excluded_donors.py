"""Tests for the candidate_funded_by_excluded_donors signal.

Candidate-side projection of donor_on_leie (FRAUD-F5d). Mirrors the
test taxonomy of test_fraud_candidate_contractor_money.py because
the underlying SQL is parallel.

Test taxonomy
-------------
Refresher integration (live_pg)
    - End-to-end: LEIE donor + FEC contribution + FEC committee +
      FEC candidate -> exactly one candidate signal row, severity=5.
    - Two-step refresh ordering: 059 must run first; without its
      L1 rows this signal returns 0 (regression-resistant: this
      test catches the case where a developer adds a parallel
      refresher path that bypasses L1).
    - cmte.cand_id NULL filter: contributions to non-candidate
      committees (Super PACs, JFCs) drop out.
    - Multi-committee per candidate aggregation.
    - memo_cd='X' double-count exclusion (must mirror 059's filter).
    - Negative transaction_amt exclusion.
    - Idempotency: re-running for the same cycle yields the same
      row count.
    - Cycle isolation: 2024 refresh leaves 2020 rows alone.
    - Per-(office, state) bucketing.
    - Empty matched-donor set -> 0 rows.
    - Evidence URL well-formed.
    - Severity=5 (escalated vs 057's severity=3).
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
    excldate: str | None = None,
    excltype: str = "1128A1",
    state: str | None = "NJ",
) -> None:
    """Seed one LEIE individual row directly (bypassing the ingester).

    Default excldate is "today" so derived.f_leie_age_decay returns
    1.0 and existing literal-sum assertions still hold. Tests that
    want to exercise the LEIE-age decay path pass an older excldate
    explicitly.
    """
    import datetime as _dt
    if excldate is None:
        excldate = _dt.datetime.now(_dt.UTC).date().strftime("%Y%m%d")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.hhs_oig_leie ("
            " record_hash, lastname, firstname, midname, busname, "
            " excltype, excldate, state, "
            " vintage_month, source_url, source_sha256"
            ") VALUES (%s, %s, %s, NULL, NULL, %s, %s, %s, %s, %s, %s)",
            (
                record_hash, lastname, firstname,
                excltype, excldate, state,
                "2026-03",
                "https://example.test/UPDATED.csv",
                "0" * 64,
            ),
        )


def _seed_candidate(
    conn: psycopg.Connection,
    *,
    cycle: str,
    cand_id: str,
    cand_name: str,
    cand_office: str = "S",
    cand_office_st: str = "NJ",
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.fec_candidate ("
            "  cycle, cand_id, cand_name, cand_office, cand_office_st, "
            "  source_url, source_sha256, source_vintage"
            ") VALUES (%s, %s, %s, %s, %s, 'test', %s, 'test')",
            (cycle, cand_id, cand_name, cand_office, cand_office_st, "0" * 64),
        )


def _seed_committee(
    conn: psycopg.Connection,
    *,
    cycle: str,
    cmte_id: str,
    cmte_nm: str,
    cand_id: str | None,
    cmte_st: str = "NJ",
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.fec_committee ("
            "  cycle, cmte_id, cmte_nm, cand_id, cmte_st, "
            "  source_url, source_sha256, source_vintage"
            ") VALUES (%s, %s, %s, %s, %s, 'test', %s, 'test')",
            (cycle, cmte_id, cmte_nm, cand_id, cmte_st, "0" * 64),
        )


def _seed_contribution(
    conn: psycopg.Connection,
    *,
    sub_id: str,
    cycle: str,
    cmte_id: str,
    name: str,
    transaction_amt: float,
    memo_cd: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.fec_contribution ("
            "  cycle, sub_id, cmte_id, name, "
            "  transaction_amt, memo_cd, "
            "  source_url, source_sha256, source_vintage"
            ") VALUES (%s, %s, %s, %s, %s, %s, 'test', %s, 'test')",
            (cycle, sub_id, cmte_id, name,
             transaction_amt, memo_cd, "0" * 64),
        )


def _refresh_donor(conn: psycopg.Connection, cycle: str) -> int:
    """Run the upstream signal-059 refresher; return rows."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_signal_donor_on_leie(%s)",
            (cycle,),
        )
        row = cur.fetchone()
        n = int(row[0]) if row else 0
    conn.commit()
    return n


def _refresh(conn: psycopg.Connection, cycle: str) -> int:
    """Run the candidate-side refresher; return rows."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_signal_candidate_funded_by_excluded_donors(%s)",
            (cycle,),
        )
        row = cur.fetchone()
        n = int(row[0]) if row else 0
    conn.commit()
    return n


def _scalar(conn: psycopg.Connection, q: str, *args: object) -> object:
    with conn.cursor() as cur:
        cur.execute(q, args)
        row = cur.fetchone()
        return row[0] if row else None


def _int_scalar(conn: psycopg.Connection, q: str, *args: object) -> int:
    v = _scalar(conn, q, *args)
    assert v is not None, f"query returned NULL: {q}"
    n: int = int(v)  # type: ignore[call-overload]
    return n


def _seed_minimal_one_match(conn: psycopg.Connection, cycle: str = "2024") -> None:
    """Smallest end-to-end fixture: 1 LEIE donor, 1 candidate, 1 contribution."""
    _seed_leie_individual(
        conn, record_hash="a" * 64,
        lastname="DOE", firstname="JANE",
    )
    _seed_candidate(conn, cycle=cycle, cand_id="S0NJ00001",
                    cand_name="SMITH, ROBERT")
    _seed_committee(conn, cycle=cycle, cmte_id="C00000001",
                    cmte_nm="ROBERT SMITH FOR SENATE",
                    cand_id="S0NJ00001")
    _seed_contribution(conn, sub_id="X1", cycle=cycle, cmte_id="C00000001",
                       name="DOE, JANE", transaction_amt=2500.0)
    conn.commit()


# ============================================================================
# Refresher integration
# ============================================================================


def test_end_to_end_one_candidate_one_leie_donor(
    fraud_db: psycopg.Connection,
) -> None:
    """Single contribution from a LEIE donor -> one candidate signal row."""
    _seed_minimal_one_match(fraud_db)
    _refresh_donor(fraud_db, "2024")
    n = _refresh(fraud_db, "2024")
    assert n == 1

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT entity_id, raw_value, peer_bucket, severity, "
            "       evidence_url "
            "FROM derived.fraud_signal_observation "
            "WHERE cycle='2024' AND signal_id="
            "  'candidate_funded_by_excluded_donors'",
        )
        row = cur.fetchone()
    assert row is not None
    cand_id, raw_value, peer_bucket, severity, evidence_url = row
    assert cand_id == "S0NJ00001"
    assert float(raw_value) == 2500.0
    assert peer_bucket == "office=S|state=NJ"
    # Severity=5 (CRITICAL), escalated vs 057's severity=3.
    assert severity == 5
    assert "candidate_funded_by_excluded_donors" in str(evidence_url)
    assert "S0NJ00001" in str(evidence_url)
    assert "cycle=2024" in str(evidence_url)


def test_returns_zero_when_donor_signal_not_refreshed(
    fraud_db: psycopg.Connection,
) -> None:
    """Without 059's L1 rows present, this signal must return 0.

    Substrate-honesty: the candidate-side projection reads from
    L1's matched-donor set. If 059 has not run, the set is empty
    and we produce 0 rows -- not an error.

    This test is a regression tripwire for the case where a future
    refactor introduces a parallel path that recomputes the match
    inline (bypassing L1). Such a refactor would defeat the
    "one canonicalizer, one source of truth" invariant.
    """
    _seed_minimal_one_match(fraud_db)
    # Skip _refresh_donor() to simulate "059 hasn't run yet".
    n = _refresh(fraud_db, "2024")
    assert n == 0


def test_drops_contributions_to_non_candidate_committees(
    fraud_db: psycopg.Connection,
) -> None:
    """Super PAC / JFC contributions (cand_id IS NULL) do NOT credit a candidate.

    Documented limitation: joint-fundraising-committee and Leadership-
    PAC unrolling requires raw.fec_ccl ingestion, which is deferred
    to a future slice. Until then, only direct principal/authorized-
    committee contributions credit the candidate.
    """
    _seed_leie_individual(
        fraud_db, record_hash="a" * 64,
        lastname="DOE", firstname="JANE",
    )
    _seed_candidate(fraud_db, cycle="2024", cand_id="S0NJ00001",
                    cand_name="SMITH, ROBERT")
    _seed_committee(fraud_db, cycle="2024", cmte_id="C00000001",
                    cmte_nm="ROBERT SMITH FOR SENATE",
                    cand_id="S0NJ00001")
    _seed_committee(fraud_db, cycle="2024", cmte_id="C99999999",
                    cmte_nm="SUPER PAC", cand_id=None)

    _seed_contribution(fraud_db, sub_id="P1", cycle="2024",
                       cmte_id="C00000001",
                       name="DOE, JANE", transaction_amt=500.0)
    _seed_contribution(fraud_db, sub_id="S1", cycle="2024",
                       cmte_id="C99999999",
                       name="DOE, JANE", transaction_amt=5000.0)
    fraud_db.commit()

    _refresh_donor(fraud_db, "2024")
    _refresh(fraud_db, "2024")

    raw_value = _scalar(
        fraud_db,
        "SELECT raw_value FROM derived.fraud_signal_observation "
        "WHERE cycle='2024' AND entity_id='S0NJ00001' AND "
        "signal_id='candidate_funded_by_excluded_donors'",
    )
    # Only the $500 to the principal committee credits the candidate.
    assert float(raw_value) == 500.0  # type: ignore[arg-type]


def test_aggregates_across_multiple_committees_for_same_candidate(
    fraud_db: psycopg.Connection,
) -> None:
    """A candidate with both a principal and an authorized committee
    aggregates contributions to both.
    """
    _seed_leie_individual(
        fraud_db, record_hash="a" * 64,
        lastname="DOE", firstname="JANE",
    )
    _seed_candidate(fraud_db, cycle="2024", cand_id="S0NJ00001",
                    cand_name="SMITH, ROBERT")
    _seed_committee(fraud_db, cycle="2024", cmte_id="C00000001",
                    cmte_nm="ROBERT SMITH FOR SENATE",
                    cand_id="S0NJ00001")
    _seed_committee(fraud_db, cycle="2024", cmte_id="C00000002",
                    cmte_nm="ROBERT SMITH RECOUNT FUND",
                    cand_id="S0NJ00001")

    _seed_contribution(fraud_db, sub_id="A1", cycle="2024",
                       cmte_id="C00000001",
                       name="DOE, JANE", transaction_amt=1000.0)
    _seed_contribution(fraud_db, sub_id="A2", cycle="2024",
                       cmte_id="C00000002",
                       name="DOE, JANE", transaction_amt=500.0)
    fraud_db.commit()

    _refresh_donor(fraud_db, "2024")
    _refresh(fraud_db, "2024")

    rv = _scalar(
        fraud_db,
        "SELECT raw_value FROM derived.fraud_signal_observation "
        "WHERE cycle='2024' AND entity_id='S0NJ00001' AND "
        "signal_id='candidate_funded_by_excluded_donors'",
    )
    assert float(rv) == 1500.0  # type: ignore[arg-type]


def test_excludes_memo_records(fraud_db: psycopg.Connection) -> None:
    """memo_cd='X' contributions don't double-count.

    Mirrors 059's filter: if 060 used a different filter, the
    candidate-side total would diverge from the donor-side total
    and analyst confusion would result.
    """
    _seed_leie_individual(
        fraud_db, record_hash="a" * 64,
        lastname="DOE", firstname="JANE",
    )
    _seed_candidate(fraud_db, cycle="2024", cand_id="S0NJ00001",
                    cand_name="SMITH, ROBERT")
    _seed_committee(fraud_db, cycle="2024", cmte_id="C00000001",
                    cmte_nm="ROBERT SMITH FOR SENATE",
                    cand_id="S0NJ00001")

    _seed_contribution(fraud_db, sub_id="M1", cycle="2024",
                       cmte_id="C00000001",
                       name="DOE, JANE", transaction_amt=1000.0,
                       memo_cd=None)
    _seed_contribution(fraud_db, sub_id="M2", cycle="2024",
                       cmte_id="C00000001",
                       name="DOE, JANE", transaction_amt=1000.0,
                       memo_cd="X")
    fraud_db.commit()

    _refresh_donor(fraud_db, "2024")
    _refresh(fraud_db, "2024")

    rv = _scalar(
        fraud_db,
        "SELECT raw_value FROM derived.fraud_signal_observation "
        "WHERE entity_id='S0NJ00001' AND "
        "signal_id='candidate_funded_by_excluded_donors'",
    )
    assert float(rv) == 1000.0  # type: ignore[arg-type]


def test_excludes_negative_amounts(
    fraud_db: psycopg.Connection,
) -> None:
    """Refunds (negative transaction_amt) drop out of per_candidate sum.

    The donor-side signal (059) uses GREATEST(amt, 0) clipping; the
    candidate-side filter is "transaction_amt > 0" at the row level,
    which is more restrictive (an all-refund donor still produces a
    non-zero donor row in 059 only via positive contributions, but
    here we drop the negatives at the row level).
    """
    _seed_leie_individual(
        fraud_db, record_hash="a" * 64,
        lastname="DOE", firstname="JANE",
    )
    _seed_candidate(fraud_db, cycle="2024", cand_id="S0NJ00001",
                    cand_name="SMITH, ROBERT")
    _seed_committee(fraud_db, cycle="2024", cmte_id="C00000001",
                    cmte_nm="ROBERT SMITH FOR SENATE",
                    cand_id="S0NJ00001")

    _seed_contribution(fraud_db, sub_id="P1", cycle="2024",
                       cmte_id="C00000001",
                       name="DOE, JANE", transaction_amt=2000.0)
    _seed_contribution(fraud_db, sub_id="P2", cycle="2024",
                       cmte_id="C00000001",
                       name="DOE, JANE", transaction_amt=-500.0)
    fraud_db.commit()

    _refresh_donor(fraud_db, "2024")
    _refresh(fraud_db, "2024")

    rv = _scalar(
        fraud_db,
        "SELECT raw_value FROM derived.fraud_signal_observation "
        "WHERE entity_id='S0NJ00001' AND "
        "signal_id='candidate_funded_by_excluded_donors'",
    )
    assert float(rv) == 2000.0  # type: ignore[arg-type]


def test_idempotency_same_cycle(fraud_db: psycopg.Connection) -> None:
    """Re-running the refresher for the same cycle yields the same rows."""
    _seed_minimal_one_match(fraud_db)
    _refresh_donor(fraud_db, "2024")
    n1 = _refresh(fraud_db, "2024")
    n2 = _refresh(fraud_db, "2024")
    assert n1 == 1
    assert n2 == 1

    total = _int_scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle='2024' AND signal_id="
        "  'candidate_funded_by_excluded_donors'",
    )
    assert total == 1


def test_cycle_isolation(fraud_db: psycopg.Connection) -> None:
    """Refreshing 2024 does not delete or touch the 2020 slice."""
    _seed_leie_individual(
        fraud_db, record_hash="a" * 64,
        lastname="DOE", firstname="JANE",
    )
    for cycle in ("2020", "2024"):
        _seed_candidate(fraud_db, cycle=cycle,
                        cand_id=f"S{cycle}NJ001",
                        cand_name="SMITH, ROBERT")
        _seed_committee(fraud_db, cycle=cycle,
                        cmte_id=f"C{cycle}001",
                        cmte_nm="ROBERT SMITH FOR SENATE",
                        cand_id=f"S{cycle}NJ001")
        _seed_contribution(fraud_db, sub_id=f"X_{cycle}",
                           cycle=cycle, cmte_id=f"C{cycle}001",
                           name="DOE, JANE",
                           transaction_amt=100.0)
    fraud_db.commit()

    _refresh_donor(fraud_db, "2020")
    _refresh_donor(fraud_db, "2024")
    _refresh(fraud_db, "2020")
    _refresh(fraud_db, "2024")

    n_2020 = _int_scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle='2020' AND signal_id="
        "  'candidate_funded_by_excluded_donors'",
    )
    n_2024 = _int_scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle='2024' AND signal_id="
        "  'candidate_funded_by_excluded_donors'",
    )
    assert n_2020 == 1
    assert n_2024 == 1


def test_per_office_state_bucketing(
    fraud_db: psycopg.Connection,
) -> None:
    """House-NJ candidates rank against each other, not Senate-US.

    A H-NJ candidate at the top of their cohort gets percentile 1.0
    even if they received less money than a Senate candidate in a
    different bucket. Tests the PARTITION BY semantics survive
    multi-bucket data.
    """
    _seed_leie_individual(
        fraud_db, record_hash="a" * 64,
        lastname="DOE", firstname="JANE",
    )
    # H-NJ candidate (smaller bucket, smaller dollars)
    _seed_candidate(fraud_db, cycle="2024", cand_id="H4NJ12345",
                    cand_name="ALICE, BOB",
                    cand_office="H", cand_office_st="NJ")
    _seed_committee(fraud_db, cycle="2024", cmte_id="C00000010",
                    cmte_nm="BOB ALICE FOR HOUSE",
                    cand_id="H4NJ12345")
    _seed_contribution(fraud_db, sub_id="H1", cycle="2024",
                       cmte_id="C00000010",
                       name="DOE, JANE", transaction_amt=500.0)
    # Senate candidate (different bucket)
    _seed_candidate(fraud_db, cycle="2024", cand_id="S0NJ00001",
                    cand_name="SMITH, ROBERT",
                    cand_office="S", cand_office_st="NJ")
    _seed_committee(fraud_db, cycle="2024", cmte_id="C00000001",
                    cmte_nm="ROBERT SMITH FOR SENATE",
                    cand_id="S0NJ00001")
    _seed_contribution(fraud_db, sub_id="S1", cycle="2024",
                       cmte_id="C00000001",
                       name="DOE, JANE", transaction_amt=10000.0)
    fraud_db.commit()

    _refresh_donor(fraud_db, "2024")
    _refresh(fraud_db, "2024")

    # Each is the sole occupant of their (office, state) bucket
    # -> each gets percentile 1.0.
    for cand_id in ("H4NJ12345", "S0NJ00001"):
        pct = _scalar(
            fraud_db,
            "SELECT peer_percentile FROM derived.fraud_signal_observation "
            "WHERE entity_id=%s AND signal_id="
            "  'candidate_funded_by_excluded_donors'",
            cand_id,
        )
        assert abs(float(pct) - 1.0) < 1e-9, (  # type: ignore[arg-type]
            f"cand_id={cand_id} percentile={pct}"
        )


def test_empty_matched_donor_set(fraud_db: psycopg.Connection) -> None:
    """No LEIE-donor matches in 059 -> 0 candidate rows here."""
    _seed_candidate(fraud_db, cycle="2024", cand_id="S0NJ00001",
                    cand_name="SMITH, ROBERT")
    _seed_committee(fraud_db, cycle="2024", cmte_id="C00000001",
                    cmte_nm="ROBERT SMITH FOR SENATE",
                    cand_id="S0NJ00001")
    _seed_contribution(fraud_db, sub_id="X1", cycle="2024",
                       cmte_id="C00000001",
                       name="UNRELATED, DONOR", transaction_amt=1000.0)
    fraud_db.commit()

    _refresh_donor(fraud_db, "2024")
    n = _refresh(fraud_db, "2024")
    assert n == 0


def test_evidence_url_well_formed(
    fraud_db: psycopg.Connection,
) -> None:
    """Evidence URL has the expected query parameters and shape."""
    _seed_minimal_one_match(fraud_db)
    _refresh_donor(fraud_db, "2024")
    _refresh(fraud_db, "2024")

    url = _scalar(
        fraud_db,
        "SELECT evidence_url FROM derived.fraud_signal_observation "
        "WHERE signal_id='candidate_funded_by_excluded_donors'",
    )
    s = str(url)
    assert s.startswith("/fec/risk/entities/candidate/")
    assert "S0NJ00001" in s
    assert "signal=candidate_funded_by_excluded_donors" in s
    assert "cycle=2024" in s
