"""Tests for the candidate_funded_by_nj_contractor_employees signal.

Test taxonomy
-------------
Refresher integration (live_pg)
    - End-to-end: USAspending recipient + FEC contributions + FEC
      committee + FEC candidate -> one candidate signal row per
      candidate that received money from contractor-employed donors.
    - Two-step refresh ordering: 056 must run first; without its rows
      this signal returns 0.
    - cmte.cand_id NULL filter: contributions to non-candidate
      committees (Super PACs, etc.) drop out.
    - Multi-committee per candidate: a candidate with two principal
      committees aggregates correctly.
    - memo_cd='X' double-count exclusion (mirrors 056).
    - Negative transaction_amt exclusion.
    - Idempotency: re-running for the same cycle yields the same row
      count.
    - Cycle isolation: 2024 refresh leaves 2020 rows alone.
    - Per-(office, state) bucketing: H-NJ candidates rank against
      each other, not against P-US candidates.
    - Empty matched-employer set -> 0 candidate rows.
    - Evidence URL well-formed.
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


def _seed_usaspending(
    conn: psycopg.Connection,
    *,
    award_id: str,
    recipient_name: str,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.usaspending_award ("
            "  generated_unique_award_id, award_type_code, "
            "  recipient_name, pop_state, award_amount, "
            "  fiscal_year_pulled, api_query_filter_sha256"
            ") VALUES (%s, %s, %s, 'NJ', 100000, 2024, %s)",
            (award_id, "D", recipient_name, "0" * 64),
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
    employer: str | None,
    transaction_amt: float,
    memo_cd: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.fec_contribution ("
            "  cycle, sub_id, cmte_id, name, employer, "
            "  transaction_amt, memo_cd, "
            "  source_url, source_sha256, source_vintage"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, 'test', %s, 'test')",
            (cycle, sub_id, cmte_id, name, employer,
             transaction_amt, memo_cd, "0" * 64),
        )


def _refresh_donor(conn: psycopg.Connection, cycle: str) -> int:
    """Run the upstream signal-056 refresher; return rows."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_signal_donor_employed_by_nj_contractor(%s)",
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
            "SELECT derived.refresh_signal_candidate_funded_by_nj_contractor_employees(%s)",
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


def _seed_minimal_one_match(conn: psycopg.Connection, cycle: str = "2024") -> None:
    """Smallest end-to-end fixture: 1 contractor, 1 candidate, 1 contribution."""
    _seed_usaspending(
        conn, award_id="CONT_AWD_001", recipient_name="Tetra Tech Inc",
    )
    _seed_candidate(conn, cycle=cycle, cand_id="S0NJ00001",
                    cand_name="DOE, JANE")
    _seed_committee(conn, cycle=cycle, cmte_id="C00000001",
                    cmte_nm="JANE DOE FOR SENATE",
                    cand_id="S0NJ00001")
    _seed_contribution(conn, sub_id="X1", cycle=cycle, cmte_id="C00000001",
                       name="SMITH, JOHN", employer="TETRA TECH",
                       transaction_amt=1000.0)
    conn.commit()


# ============================================================================
# Refresher integration
# ============================================================================


def test_end_to_end_one_candidate_one_contractor(
    fraud_db: psycopg.Connection,
) -> None:
    """Single contributor at a NJ contractor -> one candidate signal row."""
    _seed_minimal_one_match(fraud_db)
    _refresh_donor(fraud_db, "2024")
    n = _refresh(fraud_db, "2024")
    assert n == 1

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT entity_id, raw_value, peer_bucket, severity "
            "FROM derived.fraud_signal_observation "
            "WHERE cycle='2024' AND signal_id="
            "  'candidate_funded_by_nj_contractor_employees'",
        )
        row = cur.fetchone()
    assert row is not None
    cand_id, raw_value, peer_bucket, severity = row
    assert cand_id == "S0NJ00001"
    assert float(raw_value) == 1000.0
    assert peer_bucket == "office=S|state=NJ"
    assert severity == 3


def test_returns_zero_when_donor_signal_not_refreshed(
    fraud_db: psycopg.Connection,
) -> None:
    """Without 056's L1 rows present, this signal must return 0.

    Substrate-honesty: the candidate-side projection reads from L1's
    matched-employer set. If 056 has not run, the set is empty and
    we produce 0 rows -- not an error.
    """
    _seed_minimal_one_match(fraud_db)
    # Skip _refresh_donor() to simulate "056 hasn't run yet".
    n = _refresh(fraud_db, "2024")
    assert n == 0


def test_drops_contributions_to_non_candidate_committees(
    fraud_db: psycopg.Connection,
) -> None:
    """Super PAC / JFC contributions (cand_id IS NULL) do NOT credit a candidate.

    A candidate's principal/authorized committees have non-NULL
    cmte.cand_id. Contributions to non-candidate committees do not
    map to a candidate via this signal (a separate signal would
    handle independent expenditures / leadership PACs).
    """
    _seed_usaspending(
        fraud_db, award_id="CONT_AWD_001", recipient_name="Tetra Tech",
    )
    _seed_candidate(fraud_db, cycle="2024", cand_id="S0NJ00001",
                    cand_name="DOE, JANE")
    # Principal committee for the candidate
    _seed_committee(fraud_db, cycle="2024", cmte_id="C00000001",
                    cmte_nm="JANE DOE FOR SENATE", cand_id="S0NJ00001")
    # Super PAC: cand_id NULL
    _seed_committee(fraud_db, cycle="2024", cmte_id="C99999999",
                    cmte_nm="SUPER PAC", cand_id=None)

    # Donor at Tetra Tech gives $500 to principal committee
    _seed_contribution(
        fraud_db, sub_id="P1", cycle="2024", cmte_id="C00000001",
        name="SMITH, JOHN", employer="TETRA TECH",
        transaction_amt=500.0,
    )
    # Same donor gives $5000 to Super PAC
    _seed_contribution(
        fraud_db, sub_id="S1", cycle="2024", cmte_id="C99999999",
        name="SMITH, JOHN", employer="TETRA TECH",
        transaction_amt=5000.0,
    )
    fraud_db.commit()

    _refresh_donor(fraud_db, "2024")
    _refresh(fraud_db, "2024")

    raw_value = _scalar(
        fraud_db,
        "SELECT raw_value FROM derived.fraud_signal_observation "
        "WHERE cycle='2024' AND entity_id='S0NJ00001' AND "
        "signal_id='candidate_funded_by_nj_contractor_employees'",
    )
    # Only the $500 to the principal committee credits the candidate.
    assert float(raw_value) == 500.0  # type: ignore[arg-type]


def test_aggregates_across_multiple_committees_for_same_candidate(
    fraud_db: psycopg.Connection,
) -> None:
    """A candidate with both a principal and an authorized committee
    aggregates contributions to both.
    """
    _seed_usaspending(
        fraud_db, award_id="CONT_AWD_001", recipient_name="Tetra Tech",
    )
    _seed_candidate(fraud_db, cycle="2024", cand_id="S0NJ00001",
                    cand_name="DOE, JANE")
    # Two committees both linked to the candidate (principal +
    # authorized, e.g., a recount fund).
    _seed_committee(fraud_db, cycle="2024", cmte_id="C00000001",
                    cmte_nm="JANE DOE FOR SENATE", cand_id="S0NJ00001")
    _seed_committee(fraud_db, cycle="2024", cmte_id="C00000002",
                    cmte_nm="JANE DOE RECOUNT FUND", cand_id="S0NJ00001")

    _seed_contribution(
        fraud_db, sub_id="A1", cycle="2024", cmte_id="C00000001",
        name="SMITH, JOHN", employer="TETRA TECH",
        transaction_amt=1000.0,
    )
    _seed_contribution(
        fraud_db, sub_id="A2", cycle="2024", cmte_id="C00000002",
        name="ROE, JANE", employer="TETRA TECH",
        transaction_amt=2000.0,
    )
    fraud_db.commit()

    _refresh_donor(fraud_db, "2024")
    _refresh(fraud_db, "2024")

    n_rows = _scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle='2024' AND entity_id='S0NJ00001' AND "
        "signal_id='candidate_funded_by_nj_contractor_employees'",
    )
    assert n_rows == 1, "must aggregate to one row per candidate"

    raw_value = _scalar(
        fraud_db,
        "SELECT raw_value FROM derived.fraud_signal_observation "
        "WHERE cycle='2024' AND entity_id='S0NJ00001' AND "
        "signal_id='candidate_funded_by_nj_contractor_employees'",
    )
    assert float(raw_value) == 3000.0  # type: ignore[arg-type]


def test_filters_memo_cd_x_double_counts(
    fraud_db: psycopg.Connection,
) -> None:
    """memo_cd='X' rows are FEC sub-line itemizations and excluded."""
    _seed_minimal_one_match(fraud_db)

    # Add a memo sub-line that would double-count the parent.
    _seed_contribution(
        fraud_db, sub_id="M1", cycle="2024", cmte_id="C00000001",
        name="SMITH, JOHN", employer="TETRA TECH",
        transaction_amt=500.0, memo_cd="X",
    )
    fraud_db.commit()

    _refresh_donor(fraud_db, "2024")
    _refresh(fraud_db, "2024")

    raw_value = _scalar(
        fraud_db,
        "SELECT raw_value FROM derived.fraud_signal_observation "
        "WHERE cycle='2024' AND entity_id='S0NJ00001' AND "
        "signal_id='candidate_funded_by_nj_contractor_employees'",
    )
    # Just the parent: $1000, NOT $1500.
    assert float(raw_value) == 1000.0  # type: ignore[arg-type]


def test_excludes_negative_contributions(
    fraud_db: psycopg.Connection,
) -> None:
    """Refunds (negative transaction_amt) do not contribute."""
    _seed_minimal_one_match(fraud_db)

    # Refund: -$200 to the same candidate from the same cluster.
    _seed_contribution(
        fraud_db, sub_id="R1", cycle="2024", cmte_id="C00000001",
        name="SMITH, JOHN", employer="TETRA TECH",
        transaction_amt=-200.0,
    )
    fraud_db.commit()

    _refresh_donor(fraud_db, "2024")
    _refresh(fraud_db, "2024")
    raw_value = _scalar(
        fraud_db,
        "SELECT raw_value FROM derived.fraud_signal_observation "
        "WHERE cycle='2024' AND entity_id='S0NJ00001' AND "
        "signal_id='candidate_funded_by_nj_contractor_employees'",
    )
    # Only the +$1000 counts.
    assert float(raw_value) == 1000.0  # type: ignore[arg-type]


def test_refresh_is_idempotent(fraud_db: psycopg.Connection) -> None:
    """Re-running for the same cycle yields the same row count."""
    _seed_minimal_one_match(fraud_db)
    _refresh_donor(fraud_db, "2024")

    n1 = _refresh(fraud_db, "2024")
    n2 = _refresh(fraud_db, "2024")
    assert n1 == n2 == 1

    total = _scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle='2024' AND signal_id="
        "  'candidate_funded_by_nj_contractor_employees'",
    )
    assert total == 1


def test_refresh_isolates_cycles(fraud_db: psycopg.Connection) -> None:
    """A 2024 refresh leaves 2020 rows untouched."""
    _seed_usaspending(
        fraud_db, award_id="CONT_AWD_001", recipient_name="Tetra Tech",
    )

    # Two candidates, one per cycle (avoid PK collision on
    # raw.fec_candidate which is keyed on (cycle, cand_id)).
    _seed_candidate(fraud_db, cycle="2020", cand_id="S0NJ00001",
                    cand_name="DOE, JANE")
    _seed_committee(fraud_db, cycle="2020", cmte_id="C00000001",
                    cmte_nm="JANE 2020", cand_id="S0NJ00001")
    _seed_candidate(fraud_db, cycle="2024", cand_id="S0NJ00002",
                    cand_name="DOE, JANE")
    _seed_committee(fraud_db, cycle="2024", cmte_id="C00000002",
                    cmte_nm="JANE 2024", cand_id="S0NJ00002")

    _seed_contribution(
        fraud_db, sub_id="X1", cycle="2020", cmte_id="C00000001",
        name="SMITH, J", employer="TETRA TECH", transaction_amt=500.0,
    )
    _seed_contribution(
        fraud_db, sub_id="Y1", cycle="2024", cmte_id="C00000002",
        name="SMITH, J", employer="TETRA TECH", transaction_amt=2000.0,
    )
    fraud_db.commit()

    _refresh_donor(fraud_db, "2020")
    _refresh_donor(fraud_db, "2024")
    _refresh(fraud_db, "2020")
    _refresh(fraud_db, "2024")

    n_2020 = _scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle='2020' AND signal_id="
        "  'candidate_funded_by_nj_contractor_employees'",
    )
    n_2024 = _scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle='2024' AND signal_id="
        "  'candidate_funded_by_nj_contractor_employees'",
    )
    assert n_2020 == 1
    assert n_2024 == 1

    # Re-refresh 2024 only: 2020 must remain.
    _refresh(fraud_db, "2024")
    n_2020_again = _scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle='2020' AND signal_id="
        "  'candidate_funded_by_nj_contractor_employees'",
    )
    assert n_2020_again == 1


def test_per_office_state_bucket(fraud_db: psycopg.Connection) -> None:
    """Candidates are bucketed by (cand_office, cand_office_st).

    Three House-NJ candidates and one Senate-NJ candidate: the
    percentile rank for the Senate candidate (alone in its bucket)
    is 1.0, regardless of how it would rank against the House-NJ
    candidates.
    """
    _seed_usaspending(
        fraud_db, award_id="CONT_AWD_001", recipient_name="Tetra Tech",
    )

    # Three House candidates with different amounts.
    for i, amt in enumerate([100.0, 200.0, 5000.0], start=1):
        cand_id = f"H0NJ0000{i}"
        cmte_id = f"C0000010{i}"
        _seed_candidate(
            fraud_db, cycle="2024", cand_id=cand_id,
            cand_name=f"DOE, J{i}", cand_office="H",
            cand_office_st="NJ",
        )
        _seed_committee(
            fraud_db, cycle="2024", cmte_id=cmte_id,
            cmte_nm=f"J{i} FOR HOUSE", cand_id=cand_id,
        )
        _seed_contribution(
            fraud_db, sub_id=f"H{i}", cycle="2024", cmte_id=cmte_id,
            name=f"DONOR{i}", employer="TETRA TECH",
            transaction_amt=amt,
        )

    # One Senate-NJ candidate, $50.
    _seed_candidate(
        fraud_db, cycle="2024", cand_id="S0NJ99999",
        cand_name="ROE, J", cand_office="S", cand_office_st="NJ",
    )
    _seed_committee(
        fraud_db, cycle="2024", cmte_id="C00099999",
        cmte_nm="ROE FOR SENATE", cand_id="S0NJ99999",
    )
    _seed_contribution(
        fraud_db, sub_id="S99", cycle="2024", cmte_id="C00099999",
        name="DONOR99", employer="TETRA TECH",
        transaction_amt=50.0,
    )
    fraud_db.commit()

    _refresh_donor(fraud_db, "2024")
    _refresh(fraud_db, "2024")

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT entity_id, peer_bucket, peer_percentile, raw_value "
            "FROM derived.fraud_signal_observation "
            "WHERE cycle='2024' AND signal_id="
            "  'candidate_funded_by_nj_contractor_employees' "
            "ORDER BY peer_bucket, raw_value",
        )
        rows = cur.fetchall()

    # 4 rows total
    assert len(rows) == 4

    # Senate-NJ candidate alone in its bucket: percentile = 1.0
    s_rows = [r for r in rows if r[1] == "office=S|state=NJ"]
    assert len(s_rows) == 1
    assert abs(float(s_rows[0][2]) - 1.0) < 1e-9

    # House-NJ candidates: 3-way bucket
    h_rows = [r for r in rows if r[1] == "office=H|state=NJ"]
    assert len(h_rows) == 3
    # Sorted by raw_value: 100, 200, 5000
    # CUME_DIST: 1/3, 2/3, 3/3
    expected = [1.0 / 3.0, 2.0 / 3.0, 1.0]
    for got_row, want in zip(h_rows, expected, strict=True):
        assert abs(float(got_row[2]) - want) < 1e-9, h_rows


def test_refresh_with_empty_matched_set_returns_zero(
    fraud_db: psycopg.Connection,
) -> None:
    """If no donor_clusters matched, candidate-side has no rows either."""
    # FEC seeded but NO USAspending recipient -> donor signal will be empty
    _seed_candidate(fraud_db, cycle="2024", cand_id="S0NJ00001",
                    cand_name="DOE, JANE")
    _seed_committee(fraud_db, cycle="2024", cmte_id="C00000001",
                    cmte_nm="JANE", cand_id="S0NJ00001")
    _seed_contribution(
        fraud_db, sub_id="X1", cycle="2024", cmte_id="C00000001",
        name="SMITH, J", employer="TETRA TECH", transaction_amt=1000.0,
    )
    fraud_db.commit()

    _refresh_donor(fraud_db, "2024")
    n = _refresh(fraud_db, "2024")
    assert n == 0


def test_evidence_url_is_well_formed(
    fraud_db: psycopg.Connection,
) -> None:
    """evidence_url has the expected /fec/risk/entities/candidate/... shape."""
    _seed_minimal_one_match(fraud_db)
    _refresh_donor(fraud_db, "2024")
    _refresh(fraud_db, "2024")

    url = _scalar(
        fraud_db,
        "SELECT evidence_url FROM derived.fraud_signal_observation "
        "WHERE cycle='2024' AND entity_id='S0NJ00001' AND "
        "signal_id='candidate_funded_by_nj_contractor_employees'",
    )
    assert isinstance(url, str)
    assert url.startswith("/fec/risk/entities/candidate/S0NJ00001")
    assert "signal=candidate_funded_by_nj_contractor_employees" in url
    assert "cycle=2024" in url


def test_unmatched_donor_employer_does_not_credit_candidate(
    fraud_db: psycopg.Connection,
) -> None:
    """A donor whose employer is NOT a NJ contractor produces no signal row."""
    # USAspending has Tetra Tech, but the donor works at Some Random Co.
    _seed_usaspending(
        fraud_db, award_id="CONT_AWD_001", recipient_name="Tetra Tech",
    )
    _seed_candidate(fraud_db, cycle="2024", cand_id="S0NJ00001",
                    cand_name="DOE, JANE")
    _seed_committee(fraud_db, cycle="2024", cmte_id="C00000001",
                    cmte_nm="JANE", cand_id="S0NJ00001")
    _seed_contribution(
        fraud_db, sub_id="X1", cycle="2024", cmte_id="C00000001",
        name="SMITH, J", employer="Some Random Co",
        transaction_amt=1000.0,
    )
    fraud_db.commit()

    _refresh_donor(fraud_db, "2024")
    n = _refresh(fraud_db, "2024")
    assert n == 0
