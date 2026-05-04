"""Tests for migration 061: fraud_signal_config + family-aware scoring.

Covers four concerns:

1. Schema invariants on derived.fraud_signal_config
   - All 14 known signal_ids seeded with correct (family, threshold).
   - CHECK constraints reject negative thresholds, unknown families,
     empty signal_id, and empty comment.
   - updated_at trigger fires on UPDATE.

2. L2 view threshold filter
   - Below-threshold L1 observations drop out of L2 / L3a.
   - Above-threshold L1 observations are preserved in L2.
   - L1 itself stays untouched (substrate honesty).

3. L2 view signal_families column
   - Aggregates the family from config in signal_id-sorted order
     (parallel-array invariant).

4. New 3-arg derived.fraud_risk_score
   - NULL / empty / mismatched-length handling.
   - Single-family scoring matches the prior 2-arg shape (no
     diversity bonus for one family).
   - 2-family case beats 1-family case at equal per-signal phi.
   - 3-family case beats 2-family case.
   - Below-0.95-percentile signals do NOT count as a family for
     the diversity bonus.

The four checks live in the orchestration layer
(orchestration/asset_checks.py) and are not re-tested here as
PG-side; the asset-check wiring is covered by tests/test_orchestration.py.
"""

from __future__ import annotations

from decimal import Decimal

import psycopg
import pytest

pytestmark = pytest.mark.live_pg


# ============================================================================
# Fixture: fresh schema with all migrations applied
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


def _scalar(conn: psycopg.Connection, q: str, *args: object) -> object:
    """Return the first column of the first row, or None."""
    with conn.cursor() as cur:
        cur.execute(q, args)
        row = cur.fetchone()
    return None if row is None else row[0]


def _insert_l1(
    conn: psycopg.Connection,
    *,
    cycle: str,
    entity_kind: str,
    entity_id: str,
    signal_id: str,
    raw_value: float,
    severity: int = 5,
    peer_bucket: str = "office=H|state=NJ",
    peer_percentile: float = 0.99,
    evidence_url: str = "/test/evidence",
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO derived.fraud_signal_observation "
            "(cycle, entity_kind, entity_id, signal_id, raw_value, "
            " severity, peer_bucket, peer_percentile, evidence_url) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (cycle, entity_kind, entity_id, signal_id, raw_value,
             severity, peer_bucket, peer_percentile, evidence_url),
        )


# ============================================================================
# 1. Schema invariants
# ============================================================================


def test_all_17_known_signals_seeded(fraud_db: psycopg.Connection) -> None:
    """The 17 signal_ids known at migration time must be seeded.

    14 from migration 061 + 3 from migrations 064/065/066 (sam_bearing family:
    entity_excluded_via_sam_uei, donor_on_sam, candidate_funded_by_sam_excluded_donors).
    """
    expected = {
        "entity_on_leie": ("leie_bearing", Decimal("0.00")),
        "entity_funded_and_excluded": ("leie_bearing", Decimal("10000.00")),
        "donor_on_leie": ("leie_bearing", Decimal("200.00")),
        "candidate_funded_by_excluded_donors":
            ("leie_bearing", Decimal("200.00")),
        "donor_employed_by_nj_contractor":
            ("workforce", Decimal("1000.00")),
        "candidate_funded_by_nj_contractor_employees":
            ("workforce", Decimal("1000.00")),
        "committee_address_clusters": ("address", Decimal("0.00")),
        "treasurer_concentration": ("structural", Decimal("0.00")),
        "candidate_no_pcc": ("structural", Decimal("0.00")),
        "candidate_broken_pcc": ("structural", Decimal("0.00")),
        "candidate_multiple_pccs": ("structural", Decimal("0.00")),
        "committee_name_collisions": ("structural", Decimal("0.00")),
        "candidate_namesakes": ("structural", Decimal("0.00")),
        "treasurer_is_candidate": ("structural", Decimal("0.00")),
        # Migration 064: UEI-deterministic SAM x USAspending match.
        "entity_excluded_via_sam_uei":
            ("sam_bearing", Decimal("0.00")),
        # Migration 065: SAM-individual donor cross-source signal.
        "donor_on_sam": ("sam_bearing", Decimal("200.00")),
        # Migration 066: candidate-side projection of donor_on_sam.
        "candidate_funded_by_sam_excluded_donors":
            ("sam_bearing", Decimal("200.00")),
    }

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT signal_id, signal_family, min_actionable_threshold "
            "FROM derived.fraud_signal_config",
        )
        seeded = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

    assert seeded == expected, (
        f"Seeded config diverges from expected. "
        f"Got: {seeded}"
    )


def test_check_rejects_unknown_family(fraud_db: psycopg.Connection) -> None:
    with pytest.raises(psycopg.errors.CheckViolation), fraud_db.cursor() as cur:
        cur.execute(
            "INSERT INTO derived.fraud_signal_config "
            "(signal_id, signal_family, comment) "
            "VALUES ('test_x', 'made_up_family', 'rationale')",
        )
    fraud_db.rollback()


def test_check_rejects_negative_threshold(fraud_db: psycopg.Connection) -> None:
    with pytest.raises(psycopg.errors.CheckViolation), fraud_db.cursor() as cur:
        cur.execute(
            "INSERT INTO derived.fraud_signal_config "
            "(signal_id, signal_family, min_actionable_threshold, "
            " comment) "
            "VALUES ('test_x', 'structural', -1, 'rationale')",
        )
    fraud_db.rollback()


def test_check_rejects_empty_comment(fraud_db: psycopg.Connection) -> None:
    with pytest.raises(psycopg.errors.CheckViolation), fraud_db.cursor() as cur:
        cur.execute(
            "INSERT INTO derived.fraud_signal_config "
            "(signal_id, signal_family, comment) "
            "VALUES ('test_x', 'structural', '')",
        )
    fraud_db.rollback()


def test_check_rejects_invalid_signal_id(fraud_db: psycopg.Connection) -> None:
    """Regex constraint requires lowercase letters/digits/underscores."""
    with pytest.raises(psycopg.errors.CheckViolation), fraud_db.cursor() as cur:
        cur.execute(
            "INSERT INTO derived.fraud_signal_config "
            "(signal_id, signal_family, comment) "
            "VALUES ('Bad-ID', 'structural', 'rationale')",
        )
    fraud_db.rollback()


def test_updated_at_trigger_fires(fraud_db: psycopg.Connection) -> None:
    """UPDATE bumps updated_at via trigger; INSERT keeps both = now()."""
    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT created_at, updated_at "
            "FROM derived.fraud_signal_config "
            "WHERE signal_id = 'donor_on_leie'",
        )
        row = cur.fetchone()
        assert row is not None
        created, updated_initial = row
        # Sleep proxy: trigger uses now() which is per-statement, so
        # we just need to ensure a different transaction time.
        cur.execute("SELECT pg_sleep(0.05)")
        cur.execute(
            "UPDATE derived.fraud_signal_config "
            "SET min_actionable_threshold = 250 "
            "WHERE signal_id = 'donor_on_leie'",
        )
        cur.execute(
            "SELECT created_at, updated_at "
            "FROM derived.fraud_signal_config "
            "WHERE signal_id = 'donor_on_leie'",
        )
        row = cur.fetchone()
        assert row is not None
        created_after, updated_after = row

    assert created == created_after, (
        "created_at must not change on UPDATE"
    )
    assert updated_after > updated_initial, (
        f"updated_at trigger did not fire. "
        f"initial={updated_initial} after={updated_after}"
    )
    # Restore the original threshold so other tests don't observe drift.
    with fraud_db.cursor() as cur:
        cur.execute(
            "UPDATE derived.fraud_signal_config "
            "SET min_actionable_threshold = 200 "
            "WHERE signal_id = 'donor_on_leie'",
        )
    fraud_db.commit()


# ============================================================================
# 2. L2 view threshold filter
# ============================================================================


def test_below_threshold_l1_row_drops_from_l2(
    fraud_db: psycopg.Connection,
) -> None:
    """A donor_on_leie observation at $50 (< $200 floor) must NOT
    appear in v_entity_fraud_features, but the row stays in L1."""
    _insert_l1(
        fraud_db,
        cycle="2024",
        entity_kind="donor",
        entity_id="DOE|JOHN",
        signal_id="donor_on_leie",
        raw_value=50,
    )
    fraud_db.commit()

    n_l1 = _scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE entity_id = 'DOE|JOHN'",
    )
    n_l2 = _scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.v_entity_fraud_features "
        "WHERE entity_id = 'DOE|JOHN'",
    )
    n_l3 = _scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.v_entity_fraud_risk "
        "WHERE entity_id = 'DOE|JOHN'",
    )
    assert n_l1 == 1, "L1 must preserve substrate-honest record"
    assert n_l2 == 0, "below-threshold row must drop from L2"
    assert n_l3 == 0, "below-threshold row must drop from L3a"


def test_above_threshold_l1_row_preserved_in_l2(
    fraud_db: psycopg.Connection,
) -> None:
    """A donor_on_leie observation at $250 (>= $200 floor) must
    appear in v_entity_fraud_features and v_entity_fraud_risk."""
    _insert_l1(
        fraud_db,
        cycle="2024",
        entity_kind="donor",
        entity_id="DOE|JANE",
        signal_id="donor_on_leie",
        raw_value=250,
    )
    fraud_db.commit()

    n_l2 = _scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.v_entity_fraud_features "
        "WHERE entity_id = 'DOE|JANE'",
    )
    n_l3 = _scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.v_entity_fraud_risk "
        "WHERE entity_id = 'DOE|JANE'",
    )
    score = _scalar(
        fraud_db,
        "SELECT risk_score FROM derived.v_entity_fraud_risk "
        "WHERE entity_id = 'DOE|JANE'",
    )
    assert n_l2 == 1
    assert n_l3 == 1
    assert score is not None
    assert isinstance(score, Decimal)
    assert score > 0, "above-threshold row must produce nonzero score"


def test_threshold_filter_per_signal_not_per_entity(
    fraud_db: psycopg.Connection,
) -> None:
    """For an entity firing TWO signals where ONE is below threshold,
    only the above-threshold signal aggregates into L2 (the other
    signal silently drops)."""
    # entity ABC fires donor_on_leie at $300 (above $200 floor)
    # and donor_employed_by_nj_contractor at $500 (below $1000 floor)
    _insert_l1(
        fraud_db,
        cycle="2024",
        entity_kind="donor",
        entity_id="ABC",
        signal_id="donor_on_leie",
        raw_value=300,
    )
    _insert_l1(
        fraud_db,
        cycle="2024",
        entity_kind="donor",
        entity_id="ABC",
        signal_id="donor_employed_by_nj_contractor",
        raw_value=500,
    )
    fraud_db.commit()

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT signals_fired, n_signals_fired "
            "FROM derived.v_entity_fraud_features "
            "WHERE entity_id = 'ABC'",
        )
        row = cur.fetchone()
    assert row is not None, "entity ABC must appear in L2 (one signal cleared)"
    signals_fired, n_signals = row
    assert n_signals == 1
    assert signals_fired == ["donor_on_leie"]


# ============================================================================
# 3. L2 view signal_families column
# ============================================================================


def test_signal_families_array_aligned_with_signal_ids(
    fraud_db: psycopg.Connection,
) -> None:
    """signal_families is array-aggregated in signal_id sort order,
    parallel to severities/percentiles/etc."""
    # entity XYZ fires three signals from three families:
    #   donor_on_leie (leie_bearing, $300, above $200 floor)
    #   candidate_no_pcc (structural, raw_value=1, threshold=0)
    #   committee_address_clusters (address, raw_value=10, threshold=0)
    # We use entity_kind='donor' for all three; the L1 schema does
    # not constrain family-to-entity_kind alignment, and the test
    # only cares about the ARRAY_AGG ordering.
    _insert_l1(
        fraud_db,
        cycle="2024", entity_kind="donor", entity_id="XYZ",
        signal_id="donor_on_leie", raw_value=300,
    )
    _insert_l1(
        fraud_db,
        cycle="2024", entity_kind="donor", entity_id="XYZ",
        signal_id="candidate_no_pcc", raw_value=1,
    )
    _insert_l1(
        fraud_db,
        cycle="2024", entity_kind="donor", entity_id="XYZ",
        signal_id="committee_address_clusters", raw_value=10,
    )
    fraud_db.commit()

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT signals_fired, signal_families "
            "FROM derived.v_entity_fraud_features "
            "WHERE entity_id = 'XYZ'",
        )
        row = cur.fetchone()
    assert row is not None
    signals_fired, signal_families = row

    expected_signals = sorted([
        "donor_on_leie",
        "candidate_no_pcc",
        "committee_address_clusters",
    ])
    expected_families = [
        # Same order as expected_signals (alphabetical):
        # candidate_no_pcc -> structural
        # committee_address_clusters -> address
        # donor_on_leie -> leie_bearing
        "structural",
        "address",
        "leie_bearing",
    ]
    assert list(signals_fired) == expected_signals
    assert list(signal_families) == expected_families


# ============================================================================
# 4. New 3-arg derived.fraud_risk_score
# ============================================================================


def _score3(
    conn: psycopg.Connection,
    severities: list[int],
    percentiles: list[float],
    families: list[str],
) -> Decimal:
    """Helper: call derived.fraud_risk_score(SMALLINT[], NUMERIC[], TEXT[])."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT derived.fraud_risk_score("
            "  %s::SMALLINT[], %s::NUMERIC[], %s::TEXT[])",
            (severities, percentiles, families),
        )
        row = cur.fetchone()
    assert row is not None
    val = row[0]
    assert isinstance(val, Decimal)
    return val


def test_score3_null_inputs_return_zero(
    fraud_db: psycopg.Connection,
) -> None:
    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT derived.fraud_risk_score("
            "  NULL::SMALLINT[], NULL::NUMERIC[], NULL::TEXT[])",
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == Decimal("0.00")


def test_score3_empty_arrays_return_zero(
    fraud_db: psycopg.Connection,
) -> None:
    s = _score3(fraud_db, [], [], [])
    assert s == Decimal("0.00")


def test_score3_mismatched_lengths_raises(
    fraud_db: psycopg.Connection,
) -> None:
    with pytest.raises(psycopg.errors.RaiseException):
        _score3(fraud_db, [5, 5], [0.99], ["leie_bearing"])
    fraud_db.rollback()


def test_score3_single_signal_one_family_no_diversity_bonus(
    fraud_db: psycopg.Connection,
) -> None:
    """1 signal, sev=5, p=0.99: phi = 5 * 0.04^2 = 0.008.
    score = 100 * (1 - exp(-0.4)) ~ 32.97. No diversity bonus."""
    s = _score3(fraud_db, [5], [0.99], ["leie_bearing"])
    assert Decimal("32.0") < s < Decimal("34.0"), s


def test_score3_two_signals_same_family_no_diversity_bonus(
    fraud_db: psycopg.Connection,
) -> None:
    """2 signals same family at p=0.99 each: raw_sum = 0.016, no
    diversity bonus. score = 100 * (1 - exp(-0.8)) ~ 55.07."""
    s = _score3(
        fraud_db,
        [5, 5], [0.99, 0.99],
        ["leie_bearing", "leie_bearing"],
    )
    assert Decimal("54.0") < s < Decimal("56.5"), s


def test_score3_two_families_beats_one_family(
    fraud_db: psycopg.Connection,
) -> None:
    """At identical per-signal phi, 2-family entity must outscore
    1-family entity due to diversity bonus."""
    one_family = _score3(
        fraud_db,
        [5, 5], [0.99, 0.99],
        ["leie_bearing", "leie_bearing"],
    )
    two_families = _score3(
        fraud_db,
        [5, 5], [0.99, 0.99],
        ["leie_bearing", "workforce"],
    )
    assert two_families > one_family, (
        f"diversity bonus did not fire: 1fam={one_family} "
        f"vs 2fam={two_families}"
    )
    # Quantitative check: bonus = 0.01 * 1^2 = 0.01;
    # raw_sum_with_bonus = 0.016 + 0.01 = 0.026;
    # score = 100 * (1 - exp(-1.3)) ~ 72.75
    assert Decimal("71.0") < two_families < Decimal("74.0"), two_families


def test_score3_three_families_beats_two_families(
    fraud_db: psycopg.Connection,
) -> None:
    """3-family entity must outscore 2-family entity at equal phi."""
    two_families = _score3(
        fraud_db,
        [5, 5], [0.99, 0.99],
        ["leie_bearing", "workforce"],
    )
    three_families = _score3(
        fraud_db,
        [5, 5, 5], [0.99, 0.99, 0.99],
        ["leie_bearing", "workforce", "structural"],
    )
    assert three_families > two_families, (
        f"3-fam ({three_families}) must outscore 2-fam ({two_families})"
    )


def test_score3_below_threshold_signal_does_not_count_for_diversity(
    fraud_db: psycopg.Connection,
) -> None:
    """A signal at p=0.93 (below the 0.95 contributing threshold)
    must NOT count as a contributing family in the diversity bonus."""
    # Both entities have one above-threshold signal and one
    # below-threshold signal, but the 2nd signal's family differs.
    # The diversity bonus should be the same (1 contributing family
    # in both cases).
    case_a = _score3(
        fraud_db,
        [5, 5], [0.99, 0.93],
        ["leie_bearing", "leie_bearing"],
    )
    case_b = _score3(
        fraud_db,
        [5, 5], [0.99, 0.93],
        ["leie_bearing", "workforce"],
    )
    assert case_a == case_b, (
        f"below-0.95 signal must not count as contributing family. "
        f"case_a={case_a} case_b={case_b}"
    )


def test_score3_l3a_view_uses_new_function(
    fraud_db: psycopg.Connection,
) -> None:
    """End-to-end: insert two L1 entities (one single-family,
    one multi-family), confirm L3a risk_score reflects the
    diversity bonus."""
    # Single-family entity: 2 signals from leie_bearing family
    _insert_l1(
        fraud_db,
        cycle="2024", entity_kind="donor", entity_id="ENT_SINGLE",
        signal_id="donor_on_leie",
        raw_value=300, severity=5, peer_percentile=0.99,
    )
    _insert_l1(
        fraud_db,
        cycle="2024", entity_kind="donor", entity_id="ENT_SINGLE",
        signal_id="entity_on_leie",
        raw_value=1, severity=5, peer_percentile=0.99,
    )
    # Multi-family entity: leie_bearing + workforce
    _insert_l1(
        fraud_db,
        cycle="2024", entity_kind="donor", entity_id="ENT_MULTI",
        signal_id="donor_on_leie",
        raw_value=300, severity=5, peer_percentile=0.99,
    )
    _insert_l1(
        fraud_db,
        cycle="2024", entity_kind="donor", entity_id="ENT_MULTI",
        signal_id="donor_employed_by_nj_contractor",
        raw_value=2000, severity=5, peer_percentile=0.99,
    )
    fraud_db.commit()

    score_single = _scalar(
        fraud_db,
        "SELECT risk_score FROM derived.v_entity_fraud_risk "
        "WHERE entity_id = 'ENT_SINGLE'",
    )
    score_multi = _scalar(
        fraud_db,
        "SELECT risk_score FROM derived.v_entity_fraud_risk "
        "WHERE entity_id = 'ENT_MULTI'",
    )
    assert score_single is not None
    assert score_multi is not None
    assert isinstance(score_single, Decimal)
    assert isinstance(score_multi, Decimal)
    assert score_multi > score_single, (
        f"multi-family score ({score_multi}) must beat "
        f"single-family score ({score_single}) at equal phi"
    )
