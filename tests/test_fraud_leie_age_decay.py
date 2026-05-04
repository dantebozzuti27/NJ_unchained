"""Tests for migration 062: derived.f_leie_age_decay + decayed refreshers.

Covers four concerns:

1. Decay function shape (NULL, today, future, 5y, 10y, 20y).
2. 058 entity_funded_and_excluded raw_value scales by decay weight
   when excldate is old.
3. 059 donor_on_leie raw_value scales by decay weight when
   excldate is old.
4. 060 candidate_funded_by_excluded_donors applies PER-CONTRIBUTION
   decay correctly (each donor in the candidate roll-up has its
   own freshest excldate).
5. The decayed raw_value interacts with migration 061's
   min_actionable_threshold: a sufficiently old + sufficiently
   small match drops below the L2 floor.

The 054 entity_on_leie raw_value also gets decayed to (0, 1] but
its threshold is 0 so no L2 drop happens; covered as a bonus
shape check.
"""

from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal
from itertools import pairwise
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
            "    EXECUTE 'DROP VIEW IF EXISTS public.' "
            "         || quote_ident(r.viewname) || ' CASCADE'; "
            "  END LOOP; "
            "END $$;",
        )
    conn.commit()
    apply_migrations(conn, discover(MIGRATIONS_DIR))
    apply_migrations(conn, discover(SEEDS_DIR))
    return conn


def _decay_value(conn: psycopg.Connection, excldate: dt.date | None) -> float:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT derived.f_leie_age_decay(%s)",
            (excldate,),
        )
        row = cur.fetchone()
    assert row is not None
    return float(row[0])


# ============================================================================
# 1. Decay function shape
# ============================================================================


def test_decay_today_is_one(fraud_db: psycopg.Connection) -> None:
    today = dt.date.today()
    w = _decay_value(fraud_db, today)
    assert w == 1.0, w


def test_decay_null_is_one(fraud_db: psycopg.Connection) -> None:
    """NULL excldate -> conservative no-decay."""
    w = _decay_value(fraud_db, None)
    assert w == 1.0


def test_decay_future_clamped_to_one(
    fraud_db: psycopg.Connection,
) -> None:
    """A future excldate (data error) is clamped to age=0 -> weight=1.0."""
    one_year_ahead = dt.date.today() + dt.timedelta(days=365)
    w = _decay_value(fraud_db, one_year_ahead)
    assert w == 1.0


def test_decay_5y_matches_exp_minus_half(
    fraud_db: psycopg.Connection,
) -> None:
    """5 years old -> exp(-0.5) ~ 0.6065. Exact dates float around
    leap-year math, so allow a small absolute tolerance."""
    five_years_ago = dt.date.today() - dt.timedelta(days=5 * 365)
    w = _decay_value(fraud_db, five_years_ago)
    expected = math.exp(-0.5)
    assert abs(w - expected) < 0.005, (w, expected)


def test_decay_10y_matches_exp_minus_one(
    fraud_db: psycopg.Connection,
) -> None:
    ten_years_ago = dt.date.today() - dt.timedelta(days=10 * 365)
    w = _decay_value(fraud_db, ten_years_ago)
    expected = math.exp(-1.0)
    assert abs(w - expected) < 0.005, (w, expected)


def test_decay_20y_matches_exp_minus_two(
    fraud_db: psycopg.Connection,
) -> None:
    twenty_years_ago = dt.date.today() - dt.timedelta(days=20 * 365)
    w = _decay_value(fraud_db, twenty_years_ago)
    expected = math.exp(-2.0)
    assert abs(w - expected) < 0.01, (w, expected)


def test_decay_monotone_decreasing(fraud_db: psycopg.Connection) -> None:
    """Older excldate -> strictly smaller weight."""
    today = dt.date.today()
    weights = [
        _decay_value(fraud_db, today - dt.timedelta(days=365 * y))
        for y in (1, 5, 10, 20, 30)
    ]
    for a, b in pairwise(weights):
        assert a > b, f"decay not monotone decreasing: {weights}"


# ============================================================================
# 2. Refresher integration: entity_funded_and_excluded (058)
# ============================================================================


def _seed_award_058(
    conn: psycopg.Connection,
    *,
    award_id: str,
    recipient_name: str,
    award_amount: float,
) -> None:
    """Minimal raw.usaspending_award row for the 058 refresher path.

    Table shape mirrors the canonical fixture in
    test_fraud_funded_and_excluded.py: only the fields the
    derived.v_usaspending_award_active view reads are populated.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.usaspending_award ("
            "  generated_unique_award_id, award_type_code, "
            "  recipient_name, pop_state, award_amount, "
            "  fiscal_year_pulled, api_query_filter_sha256"
            ") VALUES (%s, 'D', %s, 'NJ', %s, 2024, %s)",
            (award_id, recipient_name, award_amount, "0" * 64),
        )


def _seed_leie(
    conn: psycopg.Connection,
    *,
    record_hash: str,
    lastname: str,
    firstname: str,
    excldate: dt.date,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.hhs_oig_leie ("
            " record_hash, lastname, firstname, midname, busname, "
            " excltype, excldate, state, "
            " vintage_month, source_url, source_sha256"
            ") VALUES (%s, %s, %s, NULL, NULL, '1128A1', %s, 'NJ', "
            " '2026-03', 'https://example.test/UPDATED.csv', %s)",
            (record_hash, lastname, firstname,
             excldate.strftime("%Y%m%d"), "0" * 64),
        )


def test_058_decays_old_excldate(fraud_db: psycopg.Connection) -> None:
    """A 10-year-old LEIE exclusion + $10K award should produce
    raw_value ~ 10000 * exp(-1.0) ~ 3679. Without decay this would
    be 10000."""
    ten_years_ago = dt.date.today() - dt.timedelta(days=10 * 365)
    _seed_leie(fraud_db,
               record_hash="a" * 64,
               lastname="OLD", firstname="EXCLUDED",
               excldate=ten_years_ago)
    _seed_award_058(fraud_db,
                    award_id="AWD_OLD",
                    recipient_name="OLD, EXCLUDED",
                    award_amount=10000.0)
    fraud_db.commit()

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_signal_entity_funded_and_excluded(%s)",
            ("2024",),
        )
        cur.execute(
            "SELECT raw_value FROM derived.fraud_signal_observation "
            "WHERE entity_id = 'OLD|EXCLUDED' "
            "  AND signal_id = 'entity_funded_and_excluded'",
        )
        row = cur.fetchone()
    assert row is not None
    assert isinstance(row[0], Decimal)
    rv = float(row[0])
    expected = 10000.0 * math.exp(-1.0)
    assert abs(rv - expected) < 50.0, (rv, expected)


def test_058_decay_pushes_below_threshold(
    fraud_db: psycopg.Connection,
) -> None:
    """A 12-year-old $20K award decays to ~$6024, BELOW the
    $10K min_actionable_threshold. The L1 row should still be
    written (substrate honesty) but the L2 entity should drop."""
    twelve_years_ago = dt.date.today() - dt.timedelta(days=12 * 365)
    _seed_leie(fraud_db,
               record_hash="b" * 64,
               lastname="STALE", firstname="EXCLUSION",
               excldate=twelve_years_ago)
    _seed_award_058(fraud_db,
                    award_id="AWD_STALE",
                    recipient_name="STALE, EXCLUSION",
                    award_amount=20000.0)
    fraud_db.commit()

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_signal_entity_funded_and_excluded(%s)",
            ("2024",),
        )
        # L1 row exists (substrate honesty)
        cur.execute(
            "SELECT raw_value FROM derived.fraud_signal_observation "
            "WHERE entity_id = 'STALE|EXCLUSION'",
        )
        l1_row = cur.fetchone()
        # L2 entity does NOT appear (filtered out)
        cur.execute(
            "SELECT 1 FROM derived.v_entity_fraud_features "
            "WHERE entity_id = 'STALE|EXCLUSION'",
        )
        l2_row = cur.fetchone()

    assert l1_row is not None, "L1 must preserve the match"
    rv = float(l1_row[0])
    assert rv < 10000.0, (
        f"decayed raw_value ({rv}) must be below the $10K floor"
    )
    assert l2_row is None, (
        "entity must drop out of L2 because raw_value < threshold"
    )


# ============================================================================
# 3. Refresher integration: donor_on_leie (059)
# ============================================================================


def _seed_contribution_059(
    conn: psycopg.Connection,
    *,
    sub_id: str,
    cycle: str,
    cmte_id: str,
    name: str,
    transaction_amt: float,
) -> None:
    """Minimal raw.fec_contribution row covering only fields 059 reads."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.fec_committee ("
            " cycle, cmte_id, cmte_nm, "
            " source_url, source_sha256, source_vintage) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (cycle, cmte_id, "TEST CMTE",
             "file://test", "0" * 64, "test-2024"),
        )
        cur.execute(
            "INSERT INTO raw.fec_contribution ("
            " cycle, sub_id, cmte_id, name, transaction_amt, "
            " source_url, source_sha256, source_vintage) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (cycle, sub_id, cmte_id, name, transaction_amt,
             "file://test", "0" * 64, "test-2024"),
        )


def test_059_decays_old_excldate(fraud_db: psycopg.Connection) -> None:
    """5-year-old LEIE exclusion + $1000 donation -> raw_value ~ $607."""
    five_years_ago = dt.date.today() - dt.timedelta(days=5 * 365)
    _seed_leie(fraud_db,
               record_hash="c" * 64,
               lastname="DECAYED", firstname="DONOR",
               excldate=five_years_ago)
    _seed_contribution_059(fraud_db,
                           sub_id="C1", cycle="2024",
                           cmte_id="C00000001",
                           name="DECAYED, DONOR",
                           transaction_amt=1000.0)
    fraud_db.commit()

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_signal_donor_on_leie(%s)",
            ("2024",),
        )
        cur.execute(
            "SELECT raw_value FROM derived.fraud_signal_observation "
            "WHERE entity_id = 'DECAYED|DONOR' "
            "  AND signal_id = 'donor_on_leie'",
        )
        row = cur.fetchone()
    assert row is not None
    rv = float(row[0])
    expected = 1000.0 * math.exp(-0.5)
    assert abs(rv - expected) < 5.0, (rv, expected)


# ============================================================================
# 4. Refresher integration: candidate_funded_by_excluded_donors (060)
# ============================================================================


def _seed_candidate(
    conn: psycopg.Connection,
    *,
    cycle: str,
    cand_id: str,
    cand_name: str,
    cand_pcc: str,
    cand_office: str = "S",
    cand_office_st: str = "NJ",
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.fec_candidate ("
            " cycle, cand_id, cand_name, cand_pty_affiliation, "
            " cand_election_yr, cand_office_st, cand_office, "
            " cand_office_district, cand_ici, cand_status, cand_pcc, "
            " source_url, source_sha256, source_vintage) "
            "VALUES (%s, %s, %s, 'DEM', 2024, %s, %s, NULL, "
            "        'C', 'C', %s, "
            "        'file://test', %s, 'test-2024')",
            (cycle, cand_id, cand_name,
             cand_office_st, cand_office, cand_pcc, "0" * 64),
        )


def _seed_committee(
    conn: psycopg.Connection,
    *,
    cycle: str,
    cmte_id: str,
    cand_id: str,
) -> None:
    """ALTER existing committee with cand_id linkage (overrides 059's seed)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.fec_committee ("
            " cycle, cmte_id, cmte_nm, cand_id, "
            " source_url, source_sha256, source_vintage) "
            "VALUES (%s, %s, %s, %s, "
            "        'file://test', %s, 'test-2024') "
            "ON CONFLICT (cycle, cmte_id) DO UPDATE "
            "  SET cand_id = EXCLUDED.cand_id",
            (cycle, cmte_id, "TEST CMTE", cand_id, "0" * 64),
        )


def test_060_per_donor_decay(fraud_db: psycopg.Connection) -> None:
    """060 must apply the decay PER-DONOR before the candidate roll-up.

    Setup:
      Candidate S0NJ00099 has principal committee C00000099.
      Donor #1 OLD,DONOR (exclusion 10y ago) gives $1000 -> decays to $368.
      Donor #2 NEW,DONOR (exclusion today)  gives $1000 -> decays to $1000.
      Candidate-level sum_amt = ~$1368, NOT $2000.
    """
    today = dt.date.today()
    ten_years_ago = today - dt.timedelta(days=10 * 365)

    _seed_leie(fraud_db, record_hash="d" * 64,
               lastname="OLD", firstname="DONOR",
               excldate=ten_years_ago)
    _seed_leie(fraud_db, record_hash="e" * 64,
               lastname="NEW", firstname="DONOR",
               excldate=today)

    _seed_candidate(fraud_db,
                    cycle="2024", cand_id="S0NJ00099",
                    cand_name="OK, INCUMBENT",
                    cand_pcc="C00000099")

    _seed_contribution_059(fraud_db,
                           sub_id="P_OLD", cycle="2024",
                           cmte_id="C00000099",
                           name="OLD, DONOR",
                           transaction_amt=1000.0)
    _seed_contribution_059(fraud_db,
                           sub_id="P_NEW", cycle="2024",
                           cmte_id="C00000099",
                           name="NEW, DONOR",
                           transaction_amt=1000.0)
    _seed_committee(fraud_db, cycle="2024",
                    cmte_id="C00000099", cand_id="S0NJ00099")
    fraud_db.commit()

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_signal_donor_on_leie(%s)",
            ("2024",),
        )
        cur.execute(
            "SELECT derived.refresh_signal_candidate_funded_by_excluded_donors(%s)",
            ("2024",),
        )
        cur.execute(
            "SELECT raw_value FROM derived.fraud_signal_observation "
            "WHERE entity_id = 'S0NJ00099' "
            "  AND signal_id = 'candidate_funded_by_excluded_donors'",
        )
        row = cur.fetchone()
    assert row is not None
    rv = float(row[0])
    expected = 1000.0 * math.exp(-1.0) + 1000.0 * 1.0  # ~$1368
    assert abs(rv - expected) < 10.0, (rv, expected)


# ============================================================================
# 5. Refresher integration: entity_on_leie (054) — binary -> decayed binary
# ============================================================================


def test_054_binary_decays_to_fraction(fraud_db: psycopg.Connection) -> None:
    """entity_on_leie raw_value pre-062 was 1.0 (binary). Post-062
    it's the decay weight of the freshest match's excldate, in (0, 1].

    A 5-year-old exclusion -> raw_value ~ 0.61."""
    five_years_ago = dt.date.today() - dt.timedelta(days=5 * 365)
    _seed_leie(fraud_db,
               record_hash="f" * 64,
               lastname="HALF", firstname="DECAY",
               excldate=five_years_ago)
    _seed_candidate(fraud_db,
                    cycle="2024", cand_id="H0NJ00777",
                    cand_name="HALF, DECAY",
                    cand_pcc="C00007770",
                    cand_office="H")
    fraud_db.commit()

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_signal_entity_on_leie(%s)",
            ("2024",),
        )
        cur.execute(
            "SELECT raw_value FROM derived.fraud_signal_observation "
            "WHERE entity_id = 'H0NJ00777' "
            "  AND signal_id = 'entity_on_leie' "
            "  AND entity_kind = 'candidate'",
        )
        row = cur.fetchone()
    assert row is not None
    rv = float(row[0])
    expected = math.exp(-0.5)
    assert abs(rv - expected) < 0.01, (rv, expected)
    assert 0 < rv < 1, rv
