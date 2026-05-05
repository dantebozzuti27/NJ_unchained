"""Tests for the donor_on_leie cross-source signal (FRAUD-F5c).

raw.fec_contribution donors x derived.v_leie_individual_canonical
on canonical "LAST|FIRST" key. Mirrors the test structure of
test_fraud_funded_and_excluded.py / test_fraud_leie_match.py.

Test taxonomy
-------------
1. Schema invariants
   - Migration 059 extends the entity_kind whitelist to include
     'donor'. A direct INSERT with entity_kind='donor' must succeed.
   - The CHECK constraint still rejects unknown kinds.

2. Refresher integration (live_pg)
   - End-to-end: a contribution from "DOE, JANE" who is also on
     LEIE -> exactly 1 signal row, entity_kind='donor', severity=5,
     raw_value = the contribution amount.
   - Corporate-shape donor name (no comma) -> NULL canonical key ->
     does not fire.
   - No LEIE match -> 0 rows.
   - Multi-contribution aggregation: same donor, N contributions ->
     1 row, raw_value = SUM of positives.
   - Multi-LEIE record collapse picks the freshest exclusion's hash.
   - memo_cd='X' contributions excluded from the SUM.
   - Negative transaction amounts excluded (positive-only SUM).
   - Donor with all-refund (net-zero positive) giving -> 0 rows.
   - Idempotency: re-running same cycle yields same row count.
   - Cycle isolation: refresh 2024 doesn't touch 2020.
   - Percentile shape: 1-of-10 bucket -> percentile = 0.9.
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


def _seed_leie_individual(
    conn: psycopg.Connection,
    *,
    record_hash: str,
    lastname: str,
    firstname: str,
    excldate: str | None = None,
    excltype: str = "1128A1",
    midname: str | None = None,
    state: str | None = "NJ",
) -> None:
    """Seed one LEIE individual row directly (bypassing the ingester).

    Default excldate is "today" so derived.f_leie_age_decay returns
    1.0 and the existing literal-sum assertions still hold. Tests
    that want to exercise the LEIE-age decay path pass an older
    excldate explicitly.
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
            ") VALUES (%s, %s, %s, %s, NULL, %s, %s, %s, %s, %s, %s)",
            (
                record_hash, lastname, firstname, midname,
                excltype, excldate, state,
                "2026-03",
                "https://example.test/UPDATED.csv",
                "0" * 64,
            ),
        )


def _seed_contribution(
    conn: psycopg.Connection,
    *,
    sub_id: str,
    cycle: str,
    name: str,
    transaction_amt: float,
    memo_cd: str | None = None,
    cmte_id: str = "C00000001",
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.fec_contribution ("
            "  cycle, sub_id, cmte_id, name, transaction_amt, memo_cd, "
            "  source_url, source_sha256, source_vintage"
            ") VALUES (%s, %s, %s, %s, %s, %s, 'test', %s, 'test')",
            (cycle, sub_id, cmte_id, name, transaction_amt, memo_cd, "0" * 64),
        )


def _refresh(conn: psycopg.Connection, cycle: str) -> int:
    """Run the refresher; return rows inserted."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_signal_donor_on_leie(%s)",
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
    """Typed wrapper around _scalar for integer scalars."""
    v = _scalar(conn, q, *args)
    assert v is not None, f"query returned NULL: {q}"
    n: int = int(v)  # type: ignore[call-overload]
    return n


# ============================================================================
# Schema invariants
# ============================================================================


def test_migration_059_extends_entity_kind_whitelist(
    fraud_db: psycopg.Connection,
) -> None:
    """A direct INSERT with entity_kind='donor' must succeed.

    Before migration 059 this would fail the CHECK constraint with
    a 23514 (check_violation) error.
    """
    with fraud_db.cursor() as cur:
        cur.execute(
            "INSERT INTO derived.fraud_signal_observation ("
            "  cycle, entity_kind, entity_id, signal_id, "
            "  raw_value, severity, peer_bucket, peer_percentile, "
            "  evidence_url"
            ") VALUES ("
            "  '2024', 'donor', 'TEST|TEST', 'donor_on_leie', "
            "  100, 5, 'kind=donor', 0.99, '/test'"
            ")",
        )
    fraud_db.commit()
    n = _int_scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE entity_kind='donor'",
    )
    assert n == 1


def test_migration_059_rejects_unknown_entity_kind(
    fraud_db: psycopg.Connection,
) -> None:
    """The CHECK constraint still rejects unknown kinds.

    Defends against accidentally relaxing the CHECK to a no-op.
    """
    import psycopg as _psycopg
    with fraud_db.cursor() as cur, pytest.raises(
        _psycopg.errors.CheckViolation,
    ):
        cur.execute(
            "INSERT INTO derived.fraud_signal_observation ("
            "  cycle, entity_kind, entity_id, signal_id, "
            "  raw_value, severity, peer_bucket, peer_percentile, "
            "  evidence_url"
            ") VALUES ("
            "  '2024', 'NEW_KIND_XYZ', 'X', 'sig', "
            "  100, 5, 'b', 0.99, '/t'"
            ")",
        )
    fraud_db.rollback()


# ============================================================================
# Refresher integration: end-to-end happy path
# ============================================================================


def test_end_to_end_one_match(fraud_db: psycopg.Connection) -> None:
    """Donor 'DOE, JANE' matches LEIE individual JANE DOE.

    Expected: one signal row, entity_kind='donor',
    entity_id='DOE|JANE', raw_value = contribution amount,
    severity=5, peer_bucket='kind=donor'.
    """
    _seed_leie_individual(
        fraud_db, record_hash="a" * 64,
        lastname="DOE", firstname="JANE",
    )
    _seed_contribution(
        fraud_db, sub_id="X1", cycle="2024",
        name="DOE, JANE", transaction_amt=2500.0,
    )
    fraud_db.commit()

    n = _refresh(fraud_db, "2024")
    assert n == 1

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT entity_kind, entity_id, raw_value, severity, "
            "       peer_bucket, peer_percentile, evidence_url "
            "FROM derived.fraud_signal_observation "
            "WHERE cycle='2024' "
            "  AND signal_id='donor_on_leie'",
        )
        row = cur.fetchone()
    assert row is not None
    (entity_kind, entity_id, raw_value, severity,
     peer_bucket, peer_percentile, evidence_url) = row
    assert entity_kind     == "donor"
    assert entity_id       == "DOE|JANE"
    assert float(raw_value) == 2500.0
    assert severity        == 5
    assert peer_bucket     == "kind=donor"
    # 1 match in 1-donor bucket -> 1 - 1/1 = 0
    assert float(peer_percentile) == 0.0
    assert "donor_on_leie" in str(evidence_url)
    assert "leie=" + ("a" * 64) in str(evidence_url)


def test_corporate_donor_does_not_fire(
    fraud_db: psycopg.Connection,
) -> None:
    """A donor with no comma in name (corporate / org / aggregate)
    yields NULL canonical key and does not fire.
    """
    _seed_leie_individual(
        fraud_db, record_hash="b" * 64,
        lastname="ACME", firstname="CORP",
    )
    _seed_contribution(
        fraud_db, sub_id="C1", cycle="2024",
        name="ACME CORP",  # no comma
        transaction_amt=10000.0,
    )
    fraud_db.commit()

    n = _refresh(fraud_db, "2024")
    assert n == 0


def test_no_leie_match_no_signal(fraud_db: psycopg.Connection) -> None:
    """A person-shaped donor with no LEIE entry -> 0 rows."""
    _seed_contribution(
        fraud_db, sub_id="N1", cycle="2024",
        name="SMITH, ALICE", transaction_amt=500.0,
    )
    fraud_db.commit()
    n = _refresh(fraud_db, "2024")
    assert n == 0


def test_aggregates_multiple_contributions_same_donor(
    fraud_db: psycopg.Connection,
) -> None:
    """Same matched donor on N contributions -> 1 row, raw_value = SUM."""
    _seed_leie_individual(
        fraud_db, record_hash="c" * 64,
        lastname="LEE", firstname="ROBERT",
    )
    _seed_contribution(
        fraud_db, sub_id="L1", cycle="2024",
        name="LEE, ROBERT", transaction_amt=1000.0,
    )
    _seed_contribution(
        fraud_db, sub_id="L2", cycle="2024",
        name="LEE, ROBERT", transaction_amt=500.0,
    )
    _seed_contribution(
        fraud_db, sub_id="L3", cycle="2024",
        name="LEE, ROBERT M.",  # middle initial -> same canonical key
        transaction_amt=250.0,
    )
    fraud_db.commit()

    n = _refresh(fraud_db, "2024")
    assert n == 1

    rv = _scalar(
        fraud_db,
        "SELECT raw_value FROM derived.fraud_signal_observation "
        "WHERE cycle='2024' AND entity_id='LEE|ROBERT' "
        "  AND signal_id='donor_on_leie'",
    )
    assert float(rv) == 1750.0  # type: ignore[arg-type]


def test_collapses_multiple_leie_records_picks_freshest(
    fraud_db: psycopg.Connection,
) -> None:
    """Same canonical donor key on N LEIE records -> 1 row,
    freshest exclusion's record_hash in evidence URL.
    """
    _seed_leie_individual(
        fraud_db, record_hash="1" * 64,
        lastname="JONES", firstname="MARY",
        excldate="20100101", excltype="1128A1",
    )
    _seed_leie_individual(
        fraud_db, record_hash="2" * 64,
        lastname="JONES", firstname="MARY",
        excldate="20240601", excltype="1128B7",
    )
    _seed_contribution(
        fraud_db, sub_id="J1", cycle="2024",
        name="JONES, MARY", transaction_amt=300.0,
    )
    fraud_db.commit()

    n = _refresh(fraud_db, "2024")
    assert n == 1

    url = _scalar(
        fraud_db,
        "SELECT evidence_url FROM derived.fraud_signal_observation "
        "WHERE entity_id='JONES|MARY' "
        "  AND signal_id='donor_on_leie'",
    )
    assert "leie=" + ("2" * 64) in str(url)
    assert ("1" * 64) not in str(url)


def test_excludes_memo_records(fraud_db: psycopg.Connection) -> None:
    """memo_cd='X' rows are FEC double-counts; must not contribute to SUM."""
    _seed_leie_individual(
        fraud_db, record_hash="d" * 64,
        lastname="DOE", firstname="JANE",
    )
    _seed_contribution(
        fraud_db, sub_id="M1", cycle="2024",
        name="DOE, JANE", transaction_amt=1000.0,
        memo_cd=None,  # real contribution
    )
    _seed_contribution(
        fraud_db, sub_id="M2", cycle="2024",
        name="DOE, JANE", transaction_amt=1000.0,
        memo_cd="X",  # memo double-count, must be filtered
    )
    fraud_db.commit()

    _refresh(fraud_db, "2024")
    rv = _scalar(
        fraud_db,
        "SELECT raw_value FROM derived.fraud_signal_observation "
        "WHERE entity_id='DOE|JANE' "
        "  AND signal_id='donor_on_leie'",
    )
    assert float(rv) == 1000.0  # type: ignore[arg-type]


def test_excludes_negative_amounts_from_sum(
    fraud_db: psycopg.Connection,
) -> None:
    """Refunds (negative transaction_amt) do not subtract from raw_value."""
    _seed_leie_individual(
        fraud_db, record_hash="e" * 64,
        lastname="DOE", firstname="JANE",
    )
    _seed_contribution(
        fraud_db, sub_id="P1", cycle="2024",
        name="DOE, JANE", transaction_amt=2000.0,
    )
    _seed_contribution(
        fraud_db, sub_id="P2", cycle="2024",
        name="DOE, JANE", transaction_amt=-500.0,  # refund
    )
    fraud_db.commit()

    _refresh(fraud_db, "2024")
    rv = _scalar(
        fraud_db,
        "SELECT raw_value FROM derived.fraud_signal_observation "
        "WHERE entity_id='DOE|JANE' "
        "  AND signal_id='donor_on_leie'",
    )
    # GREATEST(amt, 0) clips negatives to 0; sum stays at 2000.
    assert float(rv) == 2000.0  # type: ignore[arg-type]


def test_drops_donor_with_all_refund_giving(
    fraud_db: psycopg.Connection,
) -> None:
    """Donor whose only contributions are refunds -> 0 rows.

    Active-filter in aggregated_active CTE drops sum=0 donors so the
    analyst queue doesn't see a "donor" who actually did not give.
    """
    _seed_leie_individual(
        fraud_db, record_hash="f" * 64,
        lastname="DOE", firstname="JANE",
    )
    _seed_contribution(
        fraud_db, sub_id="R1", cycle="2024",
        name="DOE, JANE", transaction_amt=-1000.0,
    )
    fraud_db.commit()

    n = _refresh(fraud_db, "2024")
    assert n == 0


def test_idempotency_same_cycle(fraud_db: psycopg.Connection) -> None:
    """Re-running the refresher for the same cycle yields the same rows."""
    _seed_leie_individual(
        fraud_db, record_hash="a" * 64,
        lastname="DOE", firstname="JANE",
    )
    _seed_contribution(
        fraud_db, sub_id="X1", cycle="2024",
        name="DOE, JANE", transaction_amt=100.0,
    )
    fraud_db.commit()

    n1 = _refresh(fraud_db, "2024")
    n2 = _refresh(fraud_db, "2024")
    assert n1 == 1
    assert n2 == 1

    total = _int_scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle='2024' "
        "  AND signal_id='donor_on_leie'",
    )
    assert total == 1


def test_cycle_isolation(fraud_db: psycopg.Connection) -> None:
    """Refreshing 2024 does not touch the 2020 slice."""
    _seed_leie_individual(
        fraud_db, record_hash="a" * 64,
        lastname="DOE", firstname="JANE",
    )
    _seed_contribution(
        fraud_db, sub_id="X1", cycle="2020",
        name="DOE, JANE", transaction_amt=100.0,
    )
    _seed_contribution(
        fraud_db, sub_id="X2", cycle="2024",
        name="DOE, JANE", transaction_amt=200.0,
    )
    fraud_db.commit()

    _refresh(fraud_db, "2020")
    _refresh(fraud_db, "2024")

    rv_2020 = _scalar(
        fraud_db,
        "SELECT raw_value FROM derived.fraud_signal_observation "
        "WHERE cycle='2020' "
        "  AND signal_id='donor_on_leie'",
    )
    rv_2024 = _scalar(
        fraud_db,
        "SELECT raw_value FROM derived.fraud_signal_observation "
        "WHERE cycle='2024' "
        "  AND signal_id='donor_on_leie'",
    )
    assert float(rv_2020) == 100.0  # type: ignore[arg-type]
    assert float(rv_2024) == 200.0  # type: ignore[arg-type]


def test_percentile_grows_with_bucket_size(
    fraud_db: psycopg.Connection,
) -> None:
    """1 match in N=10 bucket -> percentile = 0.9."""
    fillers = [
        ("ADAMS",   "ALICE"),
        ("BROWN",   "BOB"),
        ("CLARK",   "CAROL"),
        ("DAVIS",   "DAVID"),
        ("EVANS",   "EMILY"),
        ("FRANKS",  "FRANK"),
        ("GREEN",   "GRACE"),
        ("HARRIS",  "HENRY"),
        ("INGRAM",  "IRIS"),
    ]
    for i, (last, first) in enumerate(fillers):
        _seed_contribution(
            fraud_db, sub_id=f"OTHER_{i}", cycle="2024",
            name=f"{last}, {first}", transaction_amt=100.0,
        )
    _seed_leie_individual(
        fraud_db, record_hash="a" * 64,
        lastname="DOE", firstname="JANE",
    )
    _seed_contribution(
        fraud_db, sub_id="MATCHED", cycle="2024",
        name="DOE, JANE", transaction_amt=100.0,
    )
    fraud_db.commit()

    _refresh(fraud_db, "2024")
    pct = _scalar(
        fraud_db,
        "SELECT peer_percentile FROM derived.fraud_signal_observation "
        "WHERE entity_id='DOE|JANE' "
        "  AND signal_id='donor_on_leie'",
    )
    # 1 - 1/10 = 0.9
    assert abs(float(pct) - 0.9) < 1e-9  # type: ignore[arg-type]


def test_evidence_url_well_formed(fraud_db: psycopg.Connection) -> None:
    """Evidence URL has the expected query parameters and shape."""
    _seed_leie_individual(
        fraud_db, record_hash="a" * 64,
        lastname="DOE", firstname="JANE",
    )
    _seed_contribution(
        fraud_db, sub_id="X1", cycle="2024",
        name="DOE, JANE", transaction_amt=100.0,
    )
    fraud_db.commit()

    _refresh(fraud_db, "2024")
    url = _scalar(
        fraud_db,
        "SELECT evidence_url FROM derived.fraud_signal_observation "
        "WHERE signal_id='donor_on_leie'",
    )
    s = str(url)
    assert s.startswith("/fec/risk/entities/donor/")
    assert "DOE_JANE" in s  # path-safe escape of the canonical key
    assert "signal=donor_on_leie" in s
    assert "cycle=2024" in s
    assert "leie=" in s


def test_empty_inputs_return_zero(
    fraud_db: psycopg.Connection,
) -> None:
    """Refresher is well-behaved on empty raw tables (no error)."""
    n = _refresh(fraud_db, "2024")
    assert n == 0
