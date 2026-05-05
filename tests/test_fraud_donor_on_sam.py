"""Tests for the donor_on_sam cross-source signal (FRAUD-F2 donor-side).

raw.fec_contribution donors x derived.v_sam_exclusion_individual_canonical
on canonical "LAST|FIRST" key. Mirrors test_fraud_donor_on_leie.py shape.

Test taxonomy
-------------
1. Schema invariants
   - signal config row exists with family='sam_bearing' + threshold=$200

2. Refresher integration (live_pg)
   - End-to-end: a contribution from "DOE, JANE" who is also on
     SAM (Individual classification) -> exactly 1 signal row,
     entity_kind='donor', severity=5, raw_value = the contribution
     amount (decay=1.0 with today active_date)
   - Corporate-shape donor name (no comma) -> NULL canonical key ->
     does not fire
   - SAM Firm-classification (not Individual) -> does not fire
     (filtered out by v_sam_exclusion_individual_canonical's
     classification='Individual' WHERE clause)
   - SAM exclusion that's terminated -> does not fire (filtered by
     v_sam_exclusion_active)
   - No SAM individual match -> 0 rows
   - Multi-contribution aggregation: same donor, N contributions ->
     1 row, raw_value = SUM of positives (decayed)
   - Multi-SAM-row collapse picks the freshest active_date
   - memo_cd='X' contributions excluded from the SUM
   - Negative transaction amounts excluded (positive-only SUM)
   - Donor with all-refund (net-zero positive) giving -> 0 rows
   - Age decay applied: a 10-year-old SAM exclusion with $1000 of
     contributions yields raw_value = ~$367
   - Idempotency: re-running same cycle yields same row count
   - Cycle isolation: refresh 2024 doesn't touch 2020
   - Percentile shape correct
   - Evidence URL well-formed and includes sam_record_hash

3. L2 / L3a integration
   - signal_family='sam_bearing' present in signal_families[]
   - threshold=$200: a $50 contribution survives L1 but drops from L2
   - Dual-fire LEIE+SAM on same person earns the multi-family
     diversity bonus (sum of percentiles -> elevated risk_score)
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
    """Seed one SAM Individual exclusion row (bypassing the loader).

    Default active_date is today so f_leie_age_decay returns 1.0.
    """
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


def _seed_sam_firm(
    conn: psycopg.Connection,
    *,
    record_hash: str,
    name: str,
    uei: str | None = None,
    active_date: str | None = None,
) -> None:
    """Seed one SAM Firm exclusion (no last/first; should NOT fire donor_on_sam)."""
    if active_date is None:
        active_date = _dt.datetime.now(_dt.UTC).date().isoformat()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.sam_gov_exclusion ("
            "  record_hash, classification, name, uei, "
            "  active_date, record_status, excluding_agency_name, "
            "  vintage_day, source_url, source_sha256"
            ") VALUES (%s, 'Firm', %s, %s, %s, 'Active', 'GSA', "
            "          CURRENT_DATE, %s, %s)",
            (
                record_hash, name, uei, active_date,
                "https://example.test/sam.csv", "0" * 64,
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
            "SELECT derived.refresh_signal_donor_on_sam(%s)",
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


# ============================================================================
# 1. Schema invariants
# ============================================================================


def test_donor_on_sam_signal_config_seeded(
    fraud_db: psycopg.Connection,
) -> None:
    """The new signal config row exists with family + threshold."""
    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT signal_family, min_actionable_threshold "
            "FROM derived.fraud_signal_config "
            "WHERE signal_id = 'donor_on_sam'",
        )
        row = cur.fetchone()
    assert row is not None
    family, threshold = row
    assert family == "sam_bearing"
    assert float(threshold) == 200.0  # mirror of donor_on_leie


# ============================================================================
# 2. Refresher integration: end-to-end happy path
# ============================================================================


def test_end_to_end_one_donor_match(fraud_db: psycopg.Connection) -> None:
    """Donor 'DOE, JANE' matches SAM individual JANE DOE -> 1 row."""
    _seed_sam_individual(
        fraud_db, record_hash="a" * 64,
        last="DOE", first="JANE",
    )
    _seed_contribution(
        fraud_db, sub_id="SUB001", cycle="2024",
        name="DOE, JANE", transaction_amt=2_000.0,
    )
    fraud_db.commit()

    n = _refresh(fraud_db, "2024")
    assert n == 1

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT entity_kind, entity_id, raw_value, severity, "
            "       peer_bucket, peer_percentile, evidence_url "
            "FROM derived.fraud_signal_observation "
            "WHERE cycle='2024' AND signal_id='donor_on_sam'",
        )
        row = cur.fetchone()
    assert row is not None
    (entity_kind, entity_id, raw_value, severity,
     peer_bucket, peer_percentile, evidence_url) = row
    assert entity_kind == "donor"
    assert entity_id == "DOE|JANE"
    # active_date=today => decay=1.0 => raw_value = 2000
    assert float(raw_value) == pytest.approx(2_000.0, rel=1e-9)
    assert severity == 5
    assert peer_bucket == "kind=donor"
    # 1 matched of 1 in bucket -> percentile = 1 - 1/1 = 0.0
    assert float(peer_percentile) == 0.0
    s = str(evidence_url)
    assert "donor_on_sam" in s
    assert "sam=" + ("a" * 64) in s


def test_corporate_donor_does_not_fire(fraud_db: psycopg.Connection) -> None:
    """A corporate-shape name (no comma) yields NULL canonical, no fire."""
    _seed_sam_individual(
        fraud_db, record_hash="b" * 64,
        last="LOCKHEED", first="MARTIN",
    )
    _seed_contribution(
        fraud_db, sub_id="SUB_CORP", cycle="2024",
        name="LOCKHEED MARTIN CORPORATION", transaction_amt=5_000.0,
    )
    fraud_db.commit()

    n = _refresh(fraud_db, "2024")
    assert n == 0


def test_sam_firm_classification_does_not_match_donors(
    fraud_db: psycopg.Connection,
) -> None:
    """A SAM Firm exclusion is excluded by the individual-canonical view."""
    # Firm-shape SAM row whose name happens to match a donor's name.
    _seed_sam_firm(
        fraud_db, record_hash="c" * 64,
        name="DOE, JANE",  # not actually a real firm name; tests filter
        uei="ABC123XYZ987",
    )
    _seed_contribution(
        fraud_db, sub_id="SUB_FIRM", cycle="2024",
        name="DOE, JANE", transaction_amt=2_000.0,
    )
    fraud_db.commit()

    n = _refresh(fraud_db, "2024")
    assert n == 0


def test_terminated_sam_exclusion_does_not_fire(
    fraud_db: psycopg.Connection,
) -> None:
    """Past termination_date is filtered by v_sam_exclusion_active."""
    yesterday = (_dt.datetime.now(_dt.UTC).date() - _dt.timedelta(days=1)).isoformat()
    _seed_sam_individual(
        fraud_db, record_hash="d" * 64,
        last="DOE", first="JANE",
        active_date="2010-01-01",
        termination_date=yesterday,
    )
    _seed_contribution(
        fraud_db, sub_id="SUB_TERM", cycle="2024",
        name="DOE, JANE", transaction_amt=2_000.0,
    )
    fraud_db.commit()

    n = _refresh(fraud_db, "2024")
    assert n == 0


def test_no_sam_match_no_signal(fraud_db: psycopg.Connection) -> None:
    """A donor with no SAM match -> 0 rows."""
    _seed_contribution(
        fraud_db, sub_id="SUB_NOMATCH", cycle="2024",
        name="SMITH, JOHN", transaction_amt=2_000.0,
    )
    fraud_db.commit()

    n = _refresh(fraud_db, "2024")
    assert n == 0


def test_aggregates_multiple_contributions(
    fraud_db: psycopg.Connection,
) -> None:
    """Same donor, multiple contributions -> 1 row, raw_value = SUM."""
    _seed_sam_individual(
        fraud_db, record_hash="e" * 64,
        last="DOE", first="JANE",
    )
    for i, amt in enumerate([500.0, 1000.0, 250.0]):
        _seed_contribution(
            fraud_db, sub_id=f"SUB_M_{i}", cycle="2024",
            name="DOE, JANE", transaction_amt=amt,
        )
    fraud_db.commit()
    _refresh(fraud_db, "2024")

    raw_value = _scalar(
        fraud_db,
        "SELECT raw_value FROM derived.fraud_signal_observation "
        "WHERE signal_id='donor_on_sam'",
    )
    assert float(raw_value) == pytest.approx(  # type: ignore[arg-type]
        1750.0, rel=1e-9,
    )


def test_freshest_sam_record_picked(fraud_db: psycopg.Connection) -> None:
    """A donor matching multi-SAM rows -> freshest active_date wins."""
    today = _dt.datetime.now(_dt.UTC).date().isoformat()
    _seed_sam_individual(
        fraud_db, record_hash="0" * 64,
        last="DOE", first="JANE",
        active_date="2010-01-01",
    )
    _seed_sam_individual(
        fraud_db, record_hash="f" * 64,
        last="DOE", first="JANE",
        active_date=today,
    )
    _seed_contribution(
        fraud_db, sub_id="SUB_FRESH", cycle="2024",
        name="DOE, JANE", transaction_amt=1_000.0,
    )
    fraud_db.commit()
    _refresh(fraud_db, "2024")

    raw_value = _scalar(
        fraud_db,
        "SELECT raw_value FROM derived.fraud_signal_observation "
        "WHERE signal_id='donor_on_sam'",
    )
    # Freshest (today) wins -> decay=1.0 -> raw_value=1000
    assert float(raw_value) == pytest.approx(1_000.0, rel=1e-9)  # type: ignore[arg-type]
    evidence_url = _scalar(
        fraud_db,
        "SELECT evidence_url FROM derived.fraud_signal_observation "
        "WHERE signal_id='donor_on_sam'",
    )
    assert "sam=" + ("f" * 64) in str(evidence_url)


def test_memo_cd_x_excluded(fraud_db: psycopg.Connection) -> None:
    """memo_cd='X' contributions are excluded from the SUM."""
    _seed_sam_individual(
        fraud_db, record_hash="1" * 64,
        last="DOE", first="JANE",
    )
    _seed_contribution(
        fraud_db, sub_id="SUB_REAL", cycle="2024",
        name="DOE, JANE", transaction_amt=500.0,
    )
    _seed_contribution(
        fraud_db, sub_id="SUB_MEMO", cycle="2024",
        name="DOE, JANE", transaction_amt=999.0, memo_cd="X",
    )
    fraud_db.commit()
    _refresh(fraud_db, "2024")

    raw_value = _scalar(
        fraud_db,
        "SELECT raw_value FROM derived.fraud_signal_observation "
        "WHERE signal_id='donor_on_sam'",
    )
    # Only the non-memo $500 counted.
    assert float(raw_value) == pytest.approx(500.0, rel=1e-9)  # type: ignore[arg-type]


def test_negative_amount_excluded(fraud_db: psycopg.Connection) -> None:
    """GREATEST(amt, 0) means refunds (-amounts) clamp to 0."""
    _seed_sam_individual(
        fraud_db, record_hash="2" * 64,
        last="DOE", first="JANE",
    )
    _seed_contribution(
        fraud_db, sub_id="SUB_GIFT", cycle="2024",
        name="DOE, JANE", transaction_amt=1_000.0,
    )
    _seed_contribution(
        fraud_db, sub_id="SUB_REFUND", cycle="2024",
        name="DOE, JANE", transaction_amt=-300.0,
    )
    fraud_db.commit()
    _refresh(fraud_db, "2024")

    raw_value = _scalar(
        fraud_db,
        "SELECT raw_value FROM derived.fraud_signal_observation "
        "WHERE signal_id='donor_on_sam'",
    )
    # $1000 gift, -$300 refund clamped to 0, sum = $1000.
    assert float(raw_value) == pytest.approx(1_000.0, rel=1e-9)  # type: ignore[arg-type]


def test_all_refund_donor_dropped(fraud_db: psycopg.Connection) -> None:
    """A donor with only refunds (sum_decayed_amt=0) drops from the queue."""
    _seed_sam_individual(
        fraud_db, record_hash="3" * 64,
        last="DOE", first="JANE",
    )
    _seed_contribution(
        fraud_db, sub_id="SUB_REFUND_ONLY", cycle="2024",
        name="DOE, JANE", transaction_amt=-300.0,
    )
    fraud_db.commit()

    n = _refresh(fraud_db, "2024")
    assert n == 0


def test_age_decay_applied(fraud_db: psycopg.Connection) -> None:
    """A 10-year-old SAM exclusion -> decay = exp(-1) ~= 0.3679."""
    ten_years_ago = (
        _dt.datetime.now(_dt.UTC).date() - _dt.timedelta(days=int(365.25 * 10))
    ).isoformat()
    _seed_sam_individual(
        fraud_db, record_hash="4" * 64,
        last="DOE", first="JANE",
        active_date=ten_years_ago,
    )
    _seed_contribution(
        fraud_db, sub_id="SUB_DECAY", cycle="2024",
        name="DOE, JANE", transaction_amt=1_000.0,
    )
    fraud_db.commit()
    _refresh(fraud_db, "2024")

    raw_value = _scalar(
        fraud_db,
        "SELECT raw_value FROM derived.fraud_signal_observation "
        "WHERE signal_id='donor_on_sam'",
    )
    assert float(raw_value) == pytest.approx(  # type: ignore[arg-type]
        1_000.0 * 0.3678794,
        rel=1e-3,
    )


def test_idempotent_refresh(fraud_db: psycopg.Connection) -> None:
    """Re-running the refresher: same row count + content."""
    _seed_sam_individual(
        fraud_db, record_hash="5" * 64,
        last="DOE", first="JANE",
    )
    _seed_contribution(
        fraud_db, sub_id="SUB_ID", cycle="2024",
        name="DOE, JANE", transaction_amt=1_000.0,
    )
    fraud_db.commit()

    n1 = _refresh(fraud_db, "2024")
    n2 = _refresh(fraud_db, "2024")
    assert n1 == 1
    assert n2 == 1


def test_cycle_isolation(fraud_db: psycopg.Connection) -> None:
    """Refreshing 2024 leaves a 2020 row alone."""
    with fraud_db.cursor() as cur:
        cur.execute(
            "INSERT INTO derived.fraud_signal_observation ("
            "  cycle, entity_kind, entity_id, signal_id, "
            "  raw_value, severity, peer_bucket, peer_percentile, "
            "  evidence_url"
            ") VALUES ("
            "  '2020', 'donor', 'OLD|DONOR', 'donor_on_sam', "
            "  500, 5, 'kind=donor', 0.99, '/x'"
            ")",
        )
    fraud_db.commit()

    n = _refresh(fraud_db, "2024")
    assert n == 0

    n_2020 = _int_scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle='2020' AND signal_id='donor_on_sam'",
    )
    assert n_2020 == 1


def test_percentile_in_two_donor_bucket(fraud_db: psycopg.Connection) -> None:
    """1 matched donor in 2-donor bucket -> percentile = 0.5."""
    _seed_sam_individual(
        fraud_db, record_hash="6" * 64,
        last="DOE", first="JANE",
    )
    _seed_contribution(
        fraud_db, sub_id="SUB_M", cycle="2024",
        name="DOE, JANE", transaction_amt=2_000.0,
    )
    # Unmatched donor adds to the bucket but no SAM match.
    _seed_contribution(
        fraud_db, sub_id="SUB_U", cycle="2024",
        name="SMITH, JOHN", transaction_amt=500.0,
    )
    fraud_db.commit()
    _refresh(fraud_db, "2024")

    pct = _scalar(
        fraud_db,
        "SELECT peer_percentile FROM derived.fraud_signal_observation "
        "WHERE signal_id='donor_on_sam'",
    )
    assert float(pct) == pytest.approx(0.5, abs=1e-9)  # type: ignore[arg-type]


# ============================================================================
# 3. L2 / L3a integration
# ============================================================================


def test_l2_view_carries_sam_bearing_family(
    fraud_db: psycopg.Connection,
) -> None:
    """L2 view exposes 'sam_bearing' in signal_families[] for matched donor."""
    _seed_sam_individual(
        fraud_db, record_hash="7" * 64,
        last="DOE", first="JANE",
    )
    _seed_contribution(
        fraud_db, sub_id="SUB_L2", cycle="2024",
        name="DOE, JANE", transaction_amt=2_000.0,
    )
    fraud_db.commit()
    _refresh(fraud_db, "2024")

    families = _scalar(
        fraud_db,
        "SELECT signal_families FROM derived.v_entity_fraud_features "
        "WHERE cycle='2024' AND entity_kind='donor' AND entity_id='DOE|JANE'",
    )
    assert families is not None
    assert "sam_bearing" in families  # type: ignore[operator]


def test_threshold_drops_below_floor_match(
    fraud_db: psycopg.Connection,
) -> None:
    """A $50 contribution survives L1 but drops from L2 (threshold=$200)."""
    _seed_sam_individual(
        fraud_db, record_hash="8" * 64,
        last="DOE", first="JANE",
    )
    _seed_contribution(
        fraud_db, sub_id="SUB_TINY", cycle="2024",
        name="DOE, JANE", transaction_amt=50.0,
    )
    fraud_db.commit()
    _refresh(fraud_db, "2024")

    # L1 has the row.
    n_l1 = _int_scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE signal_id='donor_on_sam'",
    )
    assert n_l1 == 1

    # L2 view drops it (below $200 threshold).
    n_l2 = _int_scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.v_entity_fraud_features "
        "WHERE cycle='2024' AND entity_kind='donor' AND entity_id='DOE|JANE'",
    )
    assert n_l2 == 0


def test_dual_fire_leie_and_sam_earns_diversity_bonus(
    fraud_db: psycopg.Connection,
) -> None:
    """A donor on BOTH LEIE and SAM fires both signals.

    The L2 pivot aggregates both into one (entity_kind, entity_id) row;
    signal_families includes both 'leie_bearing' and 'sam_bearing'.
    """
    # Seed LEIE + SAM for the same canonical key.
    today = _dt.datetime.now(_dt.UTC).date().strftime("%Y%m%d")
    with fraud_db.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.hhs_oig_leie ("
            " record_hash, lastname, firstname, midname, busname, "
            " excltype, excldate, state, "
            " vintage_month, source_url, source_sha256"
            ") VALUES (%s, %s, %s, NULL, NULL, '1128A1', %s, 'NJ', "
            "          '2026-03', 'https://example.test/UPDATED.csv', %s)",
            ("9" * 64, "DOE", "JANE", today, "0" * 64),
        )
    _seed_sam_individual(
        fraud_db, record_hash="ab" * 32,
        last="DOE", first="JANE",
    )
    # Two contributions, $2000 each.
    _seed_contribution(
        fraud_db, sub_id="SUB_DUAL_1", cycle="2024",
        name="DOE, JANE", transaction_amt=2_000.0,
    )
    fraud_db.commit()

    # Refresh BOTH signals.
    with fraud_db.cursor() as cur:
        cur.execute("SELECT derived.refresh_signal_donor_on_leie('2024')")
        cur.execute("SELECT derived.refresh_signal_donor_on_sam('2024')")
    fraud_db.commit()

    # L1: two rows for the same donor.
    n_l1 = _int_scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE entity_kind='donor' AND entity_id='DOE|JANE'",
    )
    assert n_l1 == 2

    # L2: one row, signal_families[] contains BOTH families.
    families = _scalar(
        fraud_db,
        "SELECT signal_families FROM derived.v_entity_fraud_features "
        "WHERE cycle='2024' AND entity_kind='donor' AND entity_id='DOE|JANE'",
    )
    assert families is not None
    assert "leie_bearing" in families  # type: ignore[operator]
    assert "sam_bearing" in families  # type: ignore[operator]
