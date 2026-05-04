"""Tests for the entity_excluded_via_sam_uei cross-source signal.

(FRAUD-F2 signal layer, migration 064) USAspending recipients x SAM.gov
exclusion list on a UEI-deterministic 12-character key.

Test taxonomy
-------------
1. Schema invariants
   - signal_family CHECK extended: 'sam_bearing' is a valid family
   - 'entity_excluded_via_sam_uei' has a config row with family
     ='sam_bearing' and threshold=0
   - The config CHECK still rejects an unknown family

2. Refresher integration (live_pg)
   - End-to-end: a UEI-bearing recipient that matches an active SAM
     exclusion -> exactly 1 signal row (entity_kind='contractor',
     entity_id=UEI, severity=5, family='sam_bearing')
   - Recipient with NULL UEI: refresher does not fire
   - SAM exclusion that's terminated (past termination_date): does
     not match (filtered by v_sam_exclusion_active)
   - SAM exclusion with record_status='Inactive': does not match
   - Same UEI on N awards: collapses to 1 row, raw_value = SUM
     (decayed)
   - Same UEI with multiple SAM rows (re-exclusion): freshest pick
   - Idempotent re-refresh: same row count, same content
   - Cycle isolation: 2024 refresh does not touch 2020 row
   - LEIE-age decay applied to award_amount: old active_date -> reduced
     raw_value
   - peer_bucket='kind=contractor_uei', percentile correct
   - Evidence URL well-formed and includes sam_record_hash
   - Empty bucket (no UEI-bearing recipients): refresher returns 0

3. L2 / L3a integration
   - Below-threshold (raw_value=0 with a degenerate SUM): NOT
     filtered (threshold=$0). All matches flow through to L2.
   - signal_family='sam_bearing' appears in the L2
     signal_families[] column for matched entities.
   - Multi-family entity (LEIE-bearing + SAM-bearing for the same
     person at the LAST|FIRST and UEI levels respectively): not
     possible at L3a today because they have different entity_ids
     (LAST|FIRST vs UEI). Documented as a future entity-resolution
     concern; the test confirms the *single-family* case works
     end-to-end.
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


def _seed_sam_exclusion(
    conn: psycopg.Connection,
    *,
    record_hash: str,
    classification: str = "Firm",
    name: str = "ACME EXCLUDED CORP",
    uei: str | None = None,
    active_date: str | None = None,
    termination_date: str | None = None,
    record_status: str = "Active",
    excluding_agency: str = "GSA",
    last: str | None = None,
    first: str | None = None,
) -> None:
    """Seed one SAM exclusion row directly (bypassing the loader).

    Defaults to a Firm with no termination_date (i.e., active under
    the v_sam_exclusion_active view). active_date defaults to today
    so derived.f_leie_age_decay returns 1.0 by default.
    """
    if active_date is None:
        active_date = _dt.date.today().isoformat()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.sam_gov_exclusion ("
            "  record_hash, classification, name, last, first, "
            "  uei, active_date, termination_date, record_status, "
            "  excluding_agency_name, "
            "  vintage_day, source_url, source_sha256"
            ") VALUES ("
            "  %s, %s, %s, %s, %s, "
            "  %s, %s, %s, %s, "
            "  %s, "
            "  CURRENT_DATE, %s, %s)",
            (
                record_hash, classification, name, last, first,
                uei, active_date, termination_date, record_status,
                excluding_agency,
                "https://example.test/sam.csv", "0" * 64,
            ),
        )


def _seed_award(
    conn: psycopg.Connection,
    *,
    award_id: str,
    recipient_uei: str | None,
    recipient_name: str = "ACME EXCLUDED CORP",
    award_amount: float = 100_000.0,
    award_type_code: str = "D",
) -> None:
    """Seed one USAspending award row."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.usaspending_award ("
            "  generated_unique_award_id, award_type_code, "
            "  recipient_name, recipient_uei, pop_state, "
            "  award_amount, fiscal_year_pulled, api_query_filter_sha256"
            ") VALUES (%s, %s, %s, %s, 'NJ', %s, 2024, %s)",
            (
                award_id, award_type_code, recipient_name, recipient_uei,
                award_amount, "0" * 64,
            ),
        )


def _refresh(conn: psycopg.Connection, cycle: str) -> int:
    """Run the refresher; return rows inserted."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_signal_entity_excluded_via_sam_uei(%s)",
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


def test_migration_064_extends_signal_family_check(
    fraud_db: psycopg.Connection,
) -> None:
    """A direct INSERT with signal_family='sam_bearing' must succeed."""
    with fraud_db.cursor() as cur:
        cur.execute(
            "INSERT INTO derived.fraud_signal_config ("
            "  signal_id, signal_family, "
            "  min_actionable_threshold, comment) "
            "VALUES ('test_sam_signal', 'sam_bearing', 0, 'test')",
        )
    fraud_db.commit()
    n = _int_scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_config "
        "WHERE signal_family='sam_bearing'",
    )
    # The seeded entity_excluded_via_sam_uei + the test row.
    assert n >= 2


def test_migration_064_signal_config_seeded(
    fraud_db: psycopg.Connection,
) -> None:
    """The new signal config row exists with correct family + threshold."""
    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT signal_family, min_actionable_threshold "
            "FROM derived.fraud_signal_config "
            "WHERE signal_id = 'entity_excluded_via_sam_uei'",
        )
        row = cur.fetchone()
    assert row is not None
    family, threshold = row
    assert family == "sam_bearing"
    # Threshold = $0 (every match goes to the queue).
    assert float(threshold) == 0.0


def test_migration_064_rejects_unknown_family(
    fraud_db: psycopg.Connection,
) -> None:
    """The CHECK still rejects unknown family values."""
    import psycopg as _psycopg
    with fraud_db.cursor() as cur, pytest.raises(_psycopg.errors.CheckViolation):
        cur.execute(
            "INSERT INTO derived.fraud_signal_config ("
            "  signal_id, signal_family, "
            "  min_actionable_threshold, comment) "
            "VALUES ('test_bad', 'mystery_family', 0, 't')",
        )
    fraud_db.rollback()


# ============================================================================
# 2. Refresher integration: end-to-end happy path
# ============================================================================


def test_end_to_end_one_uei_match(fraud_db: psycopg.Connection) -> None:
    """Recipient UEI matches SAM-excluded UEI -> one signal row."""
    uei = "ABC123XYZ987"
    _seed_sam_exclusion(
        fraud_db, record_hash="a" * 64, uei=uei,
        name="ACME EXCLUDED CORP",
    )
    _seed_award(
        fraud_db, award_id="AWD_001",
        recipient_uei=uei, award_amount=200_000.0,
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
            "  AND signal_id='entity_excluded_via_sam_uei'",
        )
        row = cur.fetchone()
    assert row is not None
    (
        entity_kind, entity_id, raw_value, severity,
        peer_bucket, peer_percentile, evidence_url,
    ) = row
    assert entity_kind     == "contractor"
    assert entity_id       == uei
    # active_date=today => decay weight = 1.0 => raw_value = 200_000
    assert float(raw_value) == pytest.approx(200_000.0, rel=1e-9)
    assert severity        == 5
    assert peer_bucket     == "kind=contractor_uei"
    # 1 matched UEI of 1 in bucket -> percentile = 1 - 1/1 = 0.0
    assert float(peer_percentile) == 0.0
    s = str(evidence_url)
    assert "entity_excluded_via_sam_uei" in s
    assert "sam=" + ("a" * 64) in s
    assert uei in s


def test_recipient_without_uei_does_not_fire(
    fraud_db: psycopg.Connection,
) -> None:
    """A recipient with NULL UEI is excluded by v_usaspending_award_active."""
    _seed_sam_exclusion(
        fraud_db, record_hash="b" * 64, uei="ABC123XYZ987",
    )
    _seed_award(
        fraud_db, award_id="AWD_NULL_UEI",
        recipient_uei=None, recipient_name="ACME, JANE",
    )
    fraud_db.commit()

    n = _refresh(fraud_db, "2024")
    assert n == 0


def test_terminated_sam_exclusion_does_not_fire(
    fraud_db: psycopg.Connection,
) -> None:
    """A SAM exclusion with past termination_date is filtered by v_sam_exclusion_active."""
    yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
    uei = "DEF456UVW123"
    _seed_sam_exclusion(
        fraud_db, record_hash="c" * 64, uei=uei,
        active_date="2010-01-01",
        termination_date=yesterday,
    )
    _seed_award(
        fraud_db, award_id="AWD_TERM",
        recipient_uei=uei, award_amount=100_000.0,
    )
    fraud_db.commit()

    n = _refresh(fraud_db, "2024")
    assert n == 0


def test_inactive_sam_exclusion_does_not_fire(
    fraud_db: psycopg.Connection,
) -> None:
    """A SAM exclusion with record_status='Inactive' is filtered."""
    uei = "GHI789RST456"
    _seed_sam_exclusion(
        fraud_db, record_hash="d" * 64, uei=uei,
        record_status="Inactive",
    )
    _seed_award(
        fraud_db, award_id="AWD_INACTIVE",
        recipient_uei=uei,
    )
    fraud_db.commit()

    n = _refresh(fraud_db, "2024")
    assert n == 0


def test_aggregates_multiple_awards_same_uei(
    fraud_db: psycopg.Connection,
) -> None:
    """Same UEI on N awards -> one row, raw_value = SUM (decayed)."""
    uei = "JKL111MNO222"
    _seed_sam_exclusion(
        fraud_db, record_hash="e" * 64, uei=uei,
    )
    _seed_award(
        fraud_db, award_id="AWD_M_1", recipient_uei=uei,
        award_amount=50_000.0,
    )
    _seed_award(
        fraud_db, award_id="AWD_M_2", recipient_uei=uei,
        award_amount=150_000.0,
    )
    _seed_award(
        fraud_db, award_id="AWD_M_3", recipient_uei=uei,
        award_amount=100_000.0,
    )
    fraud_db.commit()

    n = _refresh(fraud_db, "2024")
    assert n == 1

    raw_value = _scalar(
        fraud_db,
        "SELECT raw_value FROM derived.fraud_signal_observation "
        "WHERE signal_id='entity_excluded_via_sam_uei'",
    )
    assert float(raw_value) == pytest.approx(300_000.0, rel=1e-9)  # type: ignore[arg-type]


def test_freshest_sam_record_picked_under_multi_exclusion(
    fraud_db: psycopg.Connection,
) -> None:
    """A UEI re-excluded twice -> freshest active_date picked.

    Concretely: if two SAM rows exist for the same UEI -- one from 2010,
    one from today -- the refresher uses the today row (decay=1.0) and
    so the raw_value equals the sum of award amounts (not decayed).
    The evidence_url's sam=<hash> matches the freshest record's hash.
    """
    uei = "PQR333STU444"
    old_date = "2010-01-01"
    today    = _dt.date.today().isoformat()

    _seed_sam_exclusion(
        fraud_db, record_hash="0" * 64, uei=uei,
        active_date=old_date, name="ACME (old)",
    )
    _seed_sam_exclusion(
        fraud_db, record_hash="f" * 64, uei=uei,
        active_date=today,    name="ACME (fresh)",
    )
    _seed_award(
        fraud_db, award_id="AWD_FRESH",
        recipient_uei=uei, award_amount=100_000.0,
    )
    fraud_db.commit()

    _refresh(fraud_db, "2024")

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT raw_value, evidence_url "
            "FROM derived.fraud_signal_observation "
            "WHERE signal_id='entity_excluded_via_sam_uei'",
        )
        row = cur.fetchone()
    assert row is not None
    raw_value, evidence_url = row
    # Decay=1.0 because we picked the today row.
    assert float(raw_value) == pytest.approx(100_000.0, rel=1e-9)
    # evidence_url cites the freshest hash.
    assert "sam=" + ("f" * 64) in str(evidence_url)


def test_age_decay_applied_to_old_exclusion(
    fraud_db: psycopg.Connection,
) -> None:
    """An old SAM active_date scales raw_value by f_leie_age_decay.

    A 10-year-old exclusion: decay = exp(-1) ~= 0.3679.
    """
    uei = "VWX555YZA666"
    ten_years_ago = (
        _dt.date.today() - _dt.timedelta(days=int(365.25 * 10))
    ).isoformat()
    _seed_sam_exclusion(
        fraud_db, record_hash="9" * 64, uei=uei,
        active_date=ten_years_ago,
    )
    _seed_award(
        fraud_db, award_id="AWD_DECAY",
        recipient_uei=uei, award_amount=100_000.0,
    )
    fraud_db.commit()

    _refresh(fraud_db, "2024")

    raw_value = _scalar(
        fraud_db,
        "SELECT raw_value FROM derived.fraud_signal_observation "
        "WHERE signal_id='entity_excluded_via_sam_uei'",
    )
    # exp(-1) * 100_000 = 36_787.94 give or take a calendar day.
    assert float(raw_value) == pytest.approx(  # type: ignore[arg-type]
        100_000.0 * 0.3678794,
        rel=1e-3,
    )


def test_idempotent_refresh(fraud_db: psycopg.Connection) -> None:
    """Re-running the refresher for the same cycle: same row count + content."""
    uei = "AAA111BBB222"
    _seed_sam_exclusion(
        fraud_db, record_hash="1" * 64, uei=uei,
    )
    _seed_award(
        fraud_db, award_id="AWD_ID",
        recipient_uei=uei, award_amount=100_000.0,
    )
    fraud_db.commit()

    n1 = _refresh(fraud_db, "2024")
    assert n1 == 1
    rows1 = _int_scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE signal_id='entity_excluded_via_sam_uei'",
    )
    assert rows1 == 1

    n2 = _refresh(fraud_db, "2024")
    assert n2 == 1
    rows2 = _int_scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE signal_id='entity_excluded_via_sam_uei'",
    )
    assert rows2 == 1


def test_cycle_isolation(fraud_db: psycopg.Connection) -> None:
    """Refreshing 2024 leaves a 2020 row alone."""
    with fraud_db.cursor() as cur:
        cur.execute(
            "INSERT INTO derived.fraud_signal_observation ("
            "  cycle, entity_kind, entity_id, signal_id, "
            "  raw_value, severity, peer_bucket, peer_percentile, "
            "  evidence_url"
            ") VALUES ("
            "  '2020', 'contractor', 'OLDUEI012345', "
            "  'entity_excluded_via_sam_uei', "
            "  50000, 5, 'kind=contractor_uei', 0.99, '/x'"
            ")",
        )
    fraud_db.commit()

    n = _refresh(fraud_db, "2024")
    assert n == 0  # nothing to insert for 2024

    n_2020 = _int_scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle='2020' "
        "  AND signal_id='entity_excluded_via_sam_uei'",
    )
    assert n_2020 == 1  # 2020 row preserved


def test_empty_bucket_returns_zero(fraud_db: psycopg.Connection) -> None:
    """No UEI-bearing recipients -> refresher returns 0 cleanly."""
    n = _refresh(fraud_db, "2024")
    assert n == 0


def test_percentile_correct_in_two_uei_bucket(
    fraud_db: psycopg.Connection,
) -> None:
    """Percentile = 1 - n_matched / n_in_bucket, computed within the cycle."""
    matched_uei   = "MAT111CHE222"
    unmatched_uei = "UNM333MAT444"
    _seed_sam_exclusion(
        fraud_db, record_hash="2" * 64, uei=matched_uei,
    )
    _seed_award(
        fraud_db, award_id="AWD_P_M", recipient_uei=matched_uei,
    )
    _seed_award(
        fraud_db, award_id="AWD_P_U", recipient_uei=unmatched_uei,
    )
    fraud_db.commit()

    _refresh(fraud_db, "2024")

    pct = _scalar(
        fraud_db,
        "SELECT peer_percentile FROM derived.fraud_signal_observation "
        "WHERE signal_id='entity_excluded_via_sam_uei'",
    )
    # 1 matched of 2 -> 1 - 1/2 = 0.5
    assert float(pct) == pytest.approx(0.5, abs=1e-9)  # type: ignore[arg-type]


# ============================================================================
# 3. L2 / L3a integration
# ============================================================================


def test_l2_view_carries_sam_bearing_family(
    fraud_db: psycopg.Connection,
) -> None:
    """L2 view exposes 'sam_bearing' in signal_families[] for a matched entity."""
    uei = "L2T111EST222"
    _seed_sam_exclusion(
        fraud_db, record_hash="3" * 64, uei=uei,
    )
    _seed_award(
        fraud_db, award_id="AWD_L2",
        recipient_uei=uei, award_amount=100_000.0,
    )
    fraud_db.commit()
    _refresh(fraud_db, "2024")

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT signal_families "
            "FROM derived.v_entity_fraud_features "
            "WHERE cycle='2024' AND entity_kind='contractor' "
            "  AND entity_id=%s",
            (uei,),
        )
        row = cur.fetchone()
    assert row is not None
    families = row[0]
    assert "sam_bearing" in families


def test_l3a_view_exposes_risk_score_for_sam_match(
    fraud_db: psycopg.Connection,
) -> None:
    """A SAM-only match shows up in v_entity_fraud_risk with score > 0.

    Score formula (migration 061, 3-arg): per-signal phi = sev * max(0,
    p - 0.95)^2 then composite = 100 * (1 - exp(-50 * (sum_phi +
    diversity_bonus))). With sev=5 and only one signal contributing,
    the percentile must be safely above 0.95 OR the score rounds to
    0.00 in NUMERIC(5,2). Below we seed 50 OTHER distinct UEI
    recipients in the active window, putting the matched entity at
    percentile = 1 - 1/51 ~= 0.98 (well above the 0.95 floor).
    """
    matched_uei = "L3T111EST222"
    _seed_sam_exclusion(
        fraud_db, record_hash="4" * 64, uei=matched_uei,
    )
    _seed_award(
        fraud_db, award_id="AWD_L3",
        recipient_uei=matched_uei, award_amount=200_000.0,
    )
    # 50 distinct unmatched UEIs in the active window. 1-of-51 ratio
    # puts the matched entity at percentile ~0.98, comfortably in
    # the score-bearing tail.
    for i in range(50):
        # 12-char alphanumeric UEI per the raw-schema CHECK.
        other_uei = f"OTH{i:09d}"
        _seed_award(
            fraud_db, award_id=f"AWD_OTHER_{i:02d}",
            recipient_uei=other_uei,
            award_amount=10_000.0,
        )
    fraud_db.commit()
    _refresh(fraud_db, "2024")

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT risk_score "
            "FROM derived.v_entity_fraud_risk "
            "WHERE cycle='2024' AND entity_kind='contractor' "
            "  AND entity_id=%s",
            (matched_uei,),
        )
        row = cur.fetchone()
    assert row is not None
    score = float(row[0])
    # phi = 5 * (0.98 - 0.95)^2 = 5 * 0.0009 = 0.0045
    # score = 100 * (1 - exp(-50 * 0.0045)) ~= 100 * (1 - 0.7985) ~= 20
    # Assert >> 0 so a NUMERIC(5,2) rounding bug surfaces clearly.
    assert score > 1.0


def test_threshold_zero_does_not_drop_match(
    fraud_db: psycopg.Connection,
) -> None:
    """min_actionable_threshold=$0 means even tiny matches survive to L2."""
    uei = "TIN111YEW222"
    _seed_sam_exclusion(
        fraud_db, record_hash="5" * 64, uei=uei,
    )
    _seed_award(
        fraud_db, award_id="AWD_TINY",
        recipient_uei=uei, award_amount=1.00,  # $1 award
    )
    fraud_db.commit()
    _refresh(fraud_db, "2024")

    n = _int_scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.v_entity_fraud_features "
        "WHERE cycle='2024' AND entity_kind='contractor' "
        "  AND entity_id=%s",
        uei,
    )
    assert n == 1  # threshold=$0 keeps a $1 match in L2
