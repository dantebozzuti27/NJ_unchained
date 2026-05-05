"""Tests for the candidate_funded_by_sam_excluded_donors signal.

Candidate-side projection of donor_on_sam (FRAUD-F2). Mirrors the
test taxonomy of test_fraud_candidate_excluded_donors.py because the
underlying SQL is parallel (LEIE -> SAM substitution + age-decay
weight source change).

Test taxonomy
-------------
1. Schema invariants
   - signal config row exists with family='sam_bearing' + threshold=$200

2. Refresher integration (live_pg)
   - End-to-end: SAM individual + FEC contribution + FEC committee +
     FEC candidate -> exactly one candidate signal row, severity=5.
   - Two-step refresh ordering: 065 (donor_on_sam) must run first;
     without its L1 rows this signal returns 0.
   - cmte.cand_id NULL filter: contributions to non-candidate
     committees (Super PACs, JFCs) drop out.
   - Multi-committee per candidate aggregation.
   - memo_cd='X' double-count exclusion.
   - Negative transaction_amt exclusion.
   - SAM Firm classification doesn't fire (filtered upstream by 065).
   - Terminated SAM exclusion doesn't fire (filtered by view).
   - Idempotency: re-running for the same cycle yields same row count.
   - Cycle isolation: 2024 refresh leaves 2020 rows alone.
   - Per-(office, state) bucketing.
   - Empty matched-donor set -> 0 rows.
   - Evidence URL well-formed.
   - Severity=5 (CRITICAL).
   - Per-contribution age decay applied (10y SAM exclusion -> ~37%
     of contribution value).

3. L2 / L3a integration
   - signal_family='sam_bearing' present in signal_families[]
   - Dual-fire candidate (LEIE + SAM donors) -> both signal_families
     present in L2 row -> diversity bonus
"""

from __future__ import annotations

import datetime as _dt
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import psycopg


pytestmark = pytest.mark.live_pg


# ============================================================================
# Fixtures and helpers
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
            "    EXECUTE 'DROP VIEW IF EXISTS public.' "
            "            || quote_ident(r.viewname) || ' CASCADE'; "
            "  END LOOP; "
            "END $$;",
        )
    conn.commit()
    apply_migrations(conn, discover(MIGRATIONS_DIR))
    apply_migrations(conn, discover(SEEDS_DIR))
    return conn


def _seed_sam_individual(
    conn: psycopg.Connection,
    *,
    record_hash: str,
    last: str,
    first: str,
    middle: str | None = None,
    active_date: str | None = None,
    classification: str = "Individual",
    termination_date: str | None = None,
    record_status: str = "Active",
) -> None:
    if active_date is None:
        active_date = _dt.datetime.now(_dt.UTC).date().isoformat()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.sam_gov_exclusion ("
            "  record_hash, classification, "
            "  name, last, first, middle, "
            "  active_date, termination_date, record_status, "
            "  excluding_agency_name, "
            "  vintage_day, source_url, source_sha256"
            ") VALUES ("
            "  %s, %s, "
            "  %s, %s, %s, %s, "
            "  %s, %s, %s, "
            "  %s, "
            "  CURRENT_DATE, %s, %s)",
            (
                record_hash, classification,
                f"{first} {last}", last, first, middle,
                active_date, termination_date, record_status,
                "GSA",
                "https://example.test/sam.csv", "0" * 64,
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
    """Run the upstream signal-065 refresher; return rows."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_signal_donor_on_sam(%s)",
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
            "SELECT derived.refresh_signal_candidate_funded_by_sam_excluded_donors(%s)",
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


def _seed_minimal_one_match(
    conn: psycopg.Connection, cycle: str = "2024",
) -> None:
    """Smallest end-to-end fixture: 1 SAM donor, 1 candidate, 1 contribution."""
    _seed_sam_individual(
        conn, record_hash="a" * 64,
        last="DOE", first="JANE",
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
# 1. Schema invariants
# ============================================================================


def test_signal_config_seeded(fraud_db: psycopg.Connection) -> None:
    """Migration 066 seeds the new signal config row."""
    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT signal_family, min_actionable_threshold "
            "FROM derived.fraud_signal_config "
            "WHERE signal_id='candidate_funded_by_sam_excluded_donors'",
        )
        row = cur.fetchone()
    assert row is not None
    family, threshold = row
    assert family == "sam_bearing"
    assert float(threshold) == 200.0


# ============================================================================
# 2. Refresher integration
# ============================================================================


def test_end_to_end_one_candidate_one_sam_donor(
    fraud_db: psycopg.Connection,
) -> None:
    """Single contribution from a SAM individual -> one candidate row."""
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
            "  'candidate_funded_by_sam_excluded_donors'",
        )
        row = cur.fetchone()
    assert row is not None
    cand_id, raw_value, peer_bucket, severity, evidence_url = row
    assert cand_id == "S0NJ00001"
    # active_date=today => decay=1.0 => raw_value=2500
    assert float(raw_value) == pytest.approx(2500.0, rel=1e-9)
    assert peer_bucket == "office=S|state=NJ"
    assert severity == 5
    assert "candidate_funded_by_sam_excluded_donors" in str(evidence_url)
    assert "S0NJ00001" in str(evidence_url)
    assert "cycle=2024" in str(evidence_url)


def test_returns_zero_when_donor_signal_not_refreshed(
    fraud_db: psycopg.Connection,
) -> None:
    """Without 065's L1 rows present, this signal must return 0."""
    _seed_minimal_one_match(fraud_db)
    # Skip _refresh_donor() to simulate "065 hasn't run yet".
    n = _refresh(fraud_db, "2024")
    assert n == 0


def test_drops_contributions_to_non_candidate_committees(
    fraud_db: psycopg.Connection,
) -> None:
    """Super PAC / JFC contributions (cand_id IS NULL) don't credit a candidate."""
    _seed_sam_individual(
        fraud_db, record_hash="a" * 64,
        last="DOE", first="JANE",
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
        "signal_id='candidate_funded_by_sam_excluded_donors'",
    )
    # Only the $500 to the principal committee credits the candidate.
    assert float(raw_value) == pytest.approx(500.0, rel=1e-9)  # type: ignore[arg-type]


def test_aggregates_across_multiple_committees(
    fraud_db: psycopg.Connection,
) -> None:
    """A candidate with multiple committees aggregates contributions."""
    _seed_sam_individual(
        fraud_db, record_hash="a" * 64,
        last="DOE", first="JANE",
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
        "signal_id='candidate_funded_by_sam_excluded_donors'",
    )
    assert float(rv) == pytest.approx(1500.0, rel=1e-9)  # type: ignore[arg-type]


def test_excludes_memo_records(fraud_db: psycopg.Connection) -> None:
    """memo_cd='X' contributions don't double-count."""
    _seed_sam_individual(
        fraud_db, record_hash="a" * 64,
        last="DOE", first="JANE",
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
        "WHERE cycle='2024' AND entity_id='S0NJ00001' AND "
        "signal_id='candidate_funded_by_sam_excluded_donors'",
    )
    assert float(rv) == pytest.approx(1000.0, rel=1e-9)  # type: ignore[arg-type]


def test_excludes_negative_amounts(fraud_db: psycopg.Connection) -> None:
    """Refunds (-amt) are excluded by transaction_amt > 0 filter."""
    _seed_sam_individual(
        fraud_db, record_hash="a" * 64,
        last="DOE", first="JANE",
    )
    _seed_candidate(fraud_db, cycle="2024", cand_id="S0NJ00001",
                    cand_name="SMITH, ROBERT")
    _seed_committee(fraud_db, cycle="2024", cmte_id="C00000001",
                    cmte_nm="ROBERT SMITH FOR SENATE",
                    cand_id="S0NJ00001")

    _seed_contribution(fraud_db, sub_id="N1", cycle="2024",
                       cmte_id="C00000001",
                       name="DOE, JANE", transaction_amt=1000.0)
    _seed_contribution(fraud_db, sub_id="N2", cycle="2024",
                       cmte_id="C00000001",
                       name="DOE, JANE", transaction_amt=-300.0)
    fraud_db.commit()

    _refresh_donor(fraud_db, "2024")
    _refresh(fraud_db, "2024")

    rv = _scalar(
        fraud_db,
        "SELECT raw_value FROM derived.fraud_signal_observation "
        "WHERE cycle='2024' AND entity_id='S0NJ00001' AND "
        "signal_id='candidate_funded_by_sam_excluded_donors'",
    )
    assert float(rv) == pytest.approx(1000.0, rel=1e-9)  # type: ignore[arg-type]


def test_sam_firm_does_not_propagate(fraud_db: psycopg.Connection) -> None:
    """A SAM Firm-classification doesn't fire donor_on_sam, so no candidate row."""
    with fraud_db.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.sam_gov_exclusion ("
            "  record_hash, classification, name, "
            "  active_date, record_status, excluding_agency_name, "
            "  vintage_day, source_url, source_sha256"
            ") VALUES (%s, 'Firm', %s, CURRENT_DATE, 'Active', 'GSA', "
            "          CURRENT_DATE, %s, %s)",
            ("a" * 64, "DOE, JANE",
             "https://example.test/sam.csv", "0" * 64),
        )
    _seed_candidate(fraud_db, cycle="2024", cand_id="S0NJ00001",
                    cand_name="SMITH, ROBERT")
    _seed_committee(fraud_db, cycle="2024", cmte_id="C00000001",
                    cmte_nm="ROBERT SMITH FOR SENATE",
                    cand_id="S0NJ00001")
    _seed_contribution(fraud_db, sub_id="F1", cycle="2024",
                       cmte_id="C00000001",
                       name="DOE, JANE", transaction_amt=2500.0)
    fraud_db.commit()

    n_donor = _refresh_donor(fraud_db, "2024")
    n_cand = _refresh(fraud_db, "2024")
    assert n_donor == 0
    assert n_cand == 0


def test_terminated_sam_does_not_fire(fraud_db: psycopg.Connection) -> None:
    """Past termination_date is filtered upstream by v_sam_exclusion_active."""
    yesterday = (_dt.datetime.now(_dt.UTC).date() - _dt.timedelta(days=1)).isoformat()
    _seed_sam_individual(
        fraud_db, record_hash="a" * 64,
        last="DOE", first="JANE",
        active_date="2010-01-01",
        termination_date=yesterday,
    )
    _seed_candidate(fraud_db, cycle="2024", cand_id="S0NJ00001",
                    cand_name="SMITH, ROBERT")
    _seed_committee(fraud_db, cycle="2024", cmte_id="C00000001",
                    cmte_nm="ROBERT SMITH FOR SENATE",
                    cand_id="S0NJ00001")
    _seed_contribution(fraud_db, sub_id="T1", cycle="2024",
                       cmte_id="C00000001",
                       name="DOE, JANE", transaction_amt=2500.0)
    fraud_db.commit()

    _refresh_donor(fraud_db, "2024")
    n = _refresh(fraud_db, "2024")
    assert n == 0


def test_idempotent_refresh(fraud_db: psycopg.Connection) -> None:
    """Re-running yields same row count + content."""
    _seed_minimal_one_match(fraud_db)
    _refresh_donor(fraud_db, "2024")

    n1 = _refresh(fraud_db, "2024")
    n2 = _refresh(fraud_db, "2024")
    assert n1 == 1
    assert n2 == 1


def test_cycle_isolation(fraud_db: psycopg.Connection) -> None:
    """Refreshing 2024 doesn't touch 2020 rows."""
    with fraud_db.cursor() as cur:
        cur.execute(
            "INSERT INTO derived.fraud_signal_observation ("
            "  cycle, entity_kind, entity_id, signal_id, "
            "  raw_value, severity, peer_bucket, peer_percentile, "
            "  evidence_url"
            ") VALUES ("
            "  '2020', 'candidate', 'S0NJ_OLD', "
            "  'candidate_funded_by_sam_excluded_donors', "
            "  500, 5, 'office=S|state=NJ', 0.99, '/x'"
            ")",
        )
    fraud_db.commit()

    _seed_minimal_one_match(fraud_db)
    _refresh_donor(fraud_db, "2024")
    _refresh(fraud_db, "2024")

    n_2020 = _int_scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle='2020' AND signal_id="
        "  'candidate_funded_by_sam_excluded_donors'",
    )
    assert n_2020 == 1


def test_per_office_state_bucketing(fraud_db: psycopg.Connection) -> None:
    """Bucket key is 'office=X|state=Y'."""
    _seed_sam_individual(
        fraud_db, record_hash="a" * 64,
        last="DOE", first="JANE",
    )
    _seed_candidate(fraud_db, cycle="2024", cand_id="H0NJ00010",
                    cand_name="JONES, MARK",
                    cand_office="H", cand_office_st="NJ")
    _seed_committee(fraud_db, cycle="2024", cmte_id="C00000010",
                    cmte_nm="MARK JONES FOR HOUSE",
                    cand_id="H0NJ00010")
    _seed_contribution(fraud_db, sub_id="H1", cycle="2024",
                       cmte_id="C00000010",
                       name="DOE, JANE", transaction_amt=1500.0)
    fraud_db.commit()

    _refresh_donor(fraud_db, "2024")
    _refresh(fraud_db, "2024")

    bucket = _scalar(
        fraud_db,
        "SELECT peer_bucket FROM derived.fraud_signal_observation "
        "WHERE cycle='2024' AND signal_id="
        "  'candidate_funded_by_sam_excluded_donors' "
        "  AND entity_id='H0NJ00010'",
    )
    assert bucket == "office=H|state=NJ"


def test_age_decay_applied_per_contribution(
    fraud_db: psycopg.Connection,
) -> None:
    """A 10-year-old SAM exclusion -> per-contribution decay = exp(-1)."""
    ten_years_ago = (
        _dt.datetime.now(_dt.UTC).date() - _dt.timedelta(days=int(365.25 * 10))
    ).isoformat()
    _seed_sam_individual(
        fraud_db, record_hash="a" * 64,
        last="DOE", first="JANE",
        active_date=ten_years_ago,
    )
    _seed_candidate(fraud_db, cycle="2024", cand_id="S0NJ00001",
                    cand_name="SMITH, ROBERT")
    _seed_committee(fraud_db, cycle="2024", cmte_id="C00000001",
                    cmte_nm="ROBERT SMITH FOR SENATE",
                    cand_id="S0NJ00001")
    _seed_contribution(fraud_db, sub_id="D1", cycle="2024",
                       cmte_id="C00000001",
                       name="DOE, JANE", transaction_amt=1000.0)
    fraud_db.commit()

    _refresh_donor(fraud_db, "2024")
    _refresh(fraud_db, "2024")

    rv = _scalar(
        fraud_db,
        "SELECT raw_value FROM derived.fraud_signal_observation "
        "WHERE cycle='2024' AND signal_id="
        "  'candidate_funded_by_sam_excluded_donors'",
    )
    assert float(rv) == pytest.approx(  # type: ignore[arg-type]
        1000.0 * 0.3678794, rel=1e-3,
    )


# ============================================================================
# 3. L2 / L3a integration
# ============================================================================


def test_l2_view_carries_sam_bearing_family(
    fraud_db: psycopg.Connection,
) -> None:
    """L2 view exposes 'sam_bearing' in signal_families[]."""
    _seed_minimal_one_match(fraud_db)
    _refresh_donor(fraud_db, "2024")
    _refresh(fraud_db, "2024")

    families = _scalar(
        fraud_db,
        "SELECT signal_families FROM derived.v_entity_fraud_features "
        "WHERE cycle='2024' AND entity_kind='candidate' "
        "  AND entity_id='S0NJ00001'",
    )
    assert families is not None
    assert "sam_bearing" in families  # type: ignore[operator]


def test_dual_fire_leie_sam_donors_for_one_candidate(
    fraud_db: psycopg.Connection,
) -> None:
    """A candidate funded by both LEIE and SAM individuals fires both
    candidate-side projections; L2 has both families.

    This is the candidate-side mirror of the donor_on_sam dual-fire
    test: the multi-family diversity bonus surfaces at the
    candidate level when distinct signal families corroborate.
    """
    today = _dt.datetime.now(_dt.UTC).date().strftime("%Y%m%d")
    # LEIE donor
    with fraud_db.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.hhs_oig_leie ("
            " record_hash, lastname, firstname, midname, busname, "
            " excltype, excldate, state, "
            " vintage_month, source_url, source_sha256"
            ") VALUES (%s, 'DOE', 'JANE', NULL, NULL, '1128A1', %s, "
            "          'NJ', '2026-03', "
            "          'https://example.test/UPDATED.csv', %s)",
            ("a" * 64, today, "0" * 64),
        )
    # SAM donor (different individual)
    _seed_sam_individual(
        fraud_db, record_hash="b" * 64,
        last="ROE", first="RICHARD",
    )

    _seed_candidate(fraud_db, cycle="2024", cand_id="S0NJ00001",
                    cand_name="SMITH, ROBERT")
    _seed_committee(fraud_db, cycle="2024", cmte_id="C00000001",
                    cmte_nm="ROBERT SMITH FOR SENATE",
                    cand_id="S0NJ00001")
    _seed_contribution(fraud_db, sub_id="L1", cycle="2024",
                       cmte_id="C00000001",
                       name="DOE, JANE", transaction_amt=2000.0)
    _seed_contribution(fraud_db, sub_id="S1", cycle="2024",
                       cmte_id="C00000001",
                       name="ROE, RICHARD", transaction_amt=2000.0)
    fraud_db.commit()

    # Refresh BOTH donor signals AND BOTH candidate-side projections.
    with fraud_db.cursor() as cur:
        cur.execute("SELECT derived.refresh_signal_donor_on_leie('2024')")
        cur.execute("SELECT derived.refresh_signal_donor_on_sam('2024')")
        cur.execute(
            "SELECT "
            "derived.refresh_signal_candidate_funded_by_excluded_donors('2024')",
        )
        cur.execute(
            "SELECT "
            "derived.refresh_signal_candidate_funded_by_sam_excluded_donors('2024')",
        )
    fraud_db.commit()

    # L1: two rows for the same candidate (one per signal_id).
    n_l1 = _int_scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE entity_kind='candidate' AND entity_id='S0NJ00001'",
    )
    assert n_l1 == 2

    families = _scalar(
        fraud_db,
        "SELECT signal_families FROM derived.v_entity_fraud_features "
        "WHERE cycle='2024' AND entity_kind='candidate' "
        "  AND entity_id='S0NJ00001'",
    )
    assert families is not None
    assert "leie_bearing" in families  # type: ignore[operator]
    assert "sam_bearing" in families  # type: ignore[operator]
