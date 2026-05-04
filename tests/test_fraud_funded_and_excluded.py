"""Tests for the entity_funded_and_excluded cross-source signal.

(FRAUD-F1 + F5 INTERSECTION) USAspending recipients x HHS-OIG LEIE
individuals on canonical "LAST|FIRST" key.

Test taxonomy
-------------
1. Schema invariants
   - Migration 058 extends the entity_kind whitelist to include
     'contractor'. A direct INSERT with entity_kind='contractor' must
     now succeed; before this migration it was rejected by the
     CHECK constraint.

2. Refresher integration (live_pg)
   - End-to-end: a recipient with a person-shaped name that matches
     an active LEIE individual -> exactly 1 signal row with
     entity_kind='contractor', severity=5.
   - Corporate recipient (no comma in name): canonicalizer returns
     NULL, signal does not fire.
   - LEIE entry with no recipient match: signal does not fire.
   - Same person on multiple awards: one signal row, raw_value =
     SUM(award_amount).
   - Same person with multiple active LEIE records: one signal row,
     leie_record_hash in evidence_url is the freshest (most recent
     excldate).
   - Idempotency: re-running the refresher for the same cycle yields
     the same row count and identical content.
   - Cycle isolation: refreshing for 2024 does not touch a 2020 row.
   - peer_bucket='kind=contractor', rate-based percentile is > 0 in
     a small bucket (1 of N matched).
   - Evidence URL is well-formed and includes the LEIE record hash.
   - Empty bucket (no parseable individual recipients): refresher
     returns 0 with no error.
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
        excldate = _dt.date.today().strftime("%Y%m%d")
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


def _seed_award(
    conn: psycopg.Connection,
    *,
    award_id: str,
    recipient_name: str,
    award_amount: float = 100_000.0,
    award_type_code: str = "D",
) -> None:
    """Seed one USAspending award row."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.usaspending_award ("
            "  generated_unique_award_id, award_type_code, "
            "  recipient_name, pop_state, award_amount, "
            "  fiscal_year_pulled, api_query_filter_sha256"
            ") VALUES (%s, %s, %s, 'NJ', %s, 2024, %s)",
            (award_id, award_type_code, recipient_name, award_amount, "0" * 64),
        )


def _refresh(conn: psycopg.Connection, cycle: str) -> int:
    """Run the refresher; return rows inserted."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_signal_entity_funded_and_excluded(%s)",
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


def test_migration_058_extends_entity_kind_whitelist(
    fraud_db: psycopg.Connection,
) -> None:
    """A direct INSERT with entity_kind='contractor' must succeed.

    Before migration 058 this would fail the CHECK constraint with a
    23514 (check_violation) error. Confirming it succeeds verifies
    the constraint was successfully replaced.
    """
    with fraud_db.cursor() as cur:
        cur.execute(
            "INSERT INTO derived.fraud_signal_observation ("
            "  cycle, entity_kind, entity_id, signal_id, "
            "  raw_value, severity, peer_bucket, peer_percentile, "
            "  evidence_url"
            ") VALUES ("
            "  '2024', 'contractor', 'TEST|TEST', "
            "  'entity_funded_and_excluded', "
            "  100, 5, 'kind=contractor', 0.99, "
            "  '/test'"
            ")",
        )
    fraud_db.commit()
    n = _int_scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE entity_kind='contractor'",
    )
    assert n == 1


def test_migration_058_rejects_unknown_entity_kind(
    fraud_db: psycopg.Connection,
) -> None:
    """The CHECK constraint still rejects unknown kinds.

    Defense against accidentally relaxing the CHECK to a no-op.
    """
    import psycopg as _psycopg
    with fraud_db.cursor() as cur, pytest.raises(_psycopg.errors.CheckViolation):
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
    """Recipient 'DOE, JANE' matches LEIE individual JANE DOE.

    Expected: one signal row, entity_kind='contractor',
    entity_id='DOE|JANE', raw_value = sum of award_amounts,
    severity=5, peer_bucket='kind=contractor', percentile > 0.
    """
    _seed_leie_individual(
        fraud_db, record_hash="a" * 64,
        lastname="DOE", firstname="JANE",
    )
    _seed_award(
        fraud_db, award_id="AWD_001",
        recipient_name="DOE, JANE", award_amount=200_000.0,
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
            "  AND signal_id='entity_funded_and_excluded'",
        )
        row = cur.fetchone()
    assert row is not None
    (entity_kind, entity_id, raw_value, severity,
     peer_bucket, peer_percentile, evidence_url) = row
    assert entity_kind     == "contractor"
    assert entity_id       == "DOE|JANE"
    assert float(raw_value) == 200_000.0
    assert severity        == 5
    assert peer_bucket     == "kind=contractor"
    # 1 match out of 1 individual recipient -> 1 - 1/1 = 0.0
    # 1 match out of 2 individual recipients -> 1 - 1/2 = 0.5
    # Here we have only one individual recipient.
    assert float(peer_percentile) == 0.0
    assert "entity_funded_and_excluded" in str(evidence_url)
    assert "leie=" + ("a" * 64) in str(evidence_url)


def test_corporate_recipient_does_not_fire(
    fraud_db: psycopg.Connection,
) -> None:
    """A corporate recipient name (no comma) yields NULL canonical key.

    The canonicalizer derived.f_canonical_lastfirst_from_fec returns
    NULL when no comma is present. Even if a LEIE entry exists, the
    join produces zero rows.
    """
    _seed_leie_individual(
        fraud_db, record_hash="c" * 64,
        lastname="LOCKHEED", firstname="CORP",
    )
    _seed_award(
        fraud_db, award_id="AWD_CORP",
        recipient_name="LOCKHEED MARTIN CORPORATION",
    )
    fraud_db.commit()

    n = _refresh(fraud_db, "2024")
    assert n == 0


def test_no_leie_match_no_signal(fraud_db: psycopg.Connection) -> None:
    """A person-shaped recipient with no LEIE entry -> 0 rows."""
    _seed_award(
        fraud_db, award_id="AWD_NOMATCH",
        recipient_name="SMITH, JOHN",
    )
    fraud_db.commit()

    n = _refresh(fraud_db, "2024")
    assert n == 0


def test_aggregates_multiple_awards_same_person(
    fraud_db: psycopg.Connection,
) -> None:
    """Same matched individual on N awards -> one row, raw_value = SUM."""
    _seed_leie_individual(
        fraud_db, record_hash="d" * 64,
        lastname="SMITH", firstname="JANE",
    )
    _seed_award(
        fraud_db, award_id="AWD_X1",
        recipient_name="SMITH, JANE", award_amount=50_000.0,
    )
    _seed_award(
        fraud_db, award_id="AWD_X2",
        recipient_name="SMITH, JANE", award_amount=75_000.0,
    )
    _seed_award(
        fraud_db, award_id="AWD_X3",
        recipient_name="SMITH, JANE", award_amount=25_000.0,
    )
    fraud_db.commit()

    n = _refresh(fraud_db, "2024")
    assert n == 1

    rv = _scalar(
        fraud_db,
        "SELECT raw_value FROM derived.fraud_signal_observation "
        "WHERE cycle='2024' AND signal_id='entity_funded_and_excluded' "
        "  AND entity_id='SMITH|JANE'",
    )
    assert float(rv) == 150_000.0  # type: ignore[arg-type]


def test_collapses_multiple_leie_records_picks_freshest(
    fraud_db: psycopg.Connection,
) -> None:
    """Same canonical key on N LEIE records -> one row, freshest hash.

    Person re-excluded under different authority codes appears as
    multiple LEIE rows. The signal must collapse to one row per
    person and embed the FRESHEST exclusion's record_hash in the
    evidence URL (so the analyst lands on the most recent action).
    """
    _seed_leie_individual(
        fraud_db, record_hash="1" * 64,
        lastname="JONES", firstname="ROBERT",
        excldate="20100101", excltype="1128A1",
    )
    _seed_leie_individual(
        fraud_db, record_hash="2" * 64,
        lastname="JONES", firstname="ROBERT",
        excldate="20240601", excltype="1128B7",
    )
    _seed_award(
        fraud_db, award_id="AWD_J1",
        recipient_name="JONES, ROBERT", award_amount=10_000.0,
    )
    fraud_db.commit()

    n = _refresh(fraud_db, "2024")
    assert n == 1

    evidence_url = _scalar(
        fraud_db,
        "SELECT evidence_url FROM derived.fraud_signal_observation "
        "WHERE entity_id='JONES|ROBERT' "
        "  AND signal_id='entity_funded_and_excluded'",
    )
    assert "leie=" + ("2" * 64) in str(evidence_url)
    assert ("1" * 64) not in str(evidence_url)


def test_award_double_count_safe_under_multi_leie(
    fraud_db: psycopg.Connection,
) -> None:
    """N awards x M LEIE records for same person -> raw_value summed once.

    Without the leie_canonical_freshest pre-collapse, an N-award person
    with M LEIE records would have its awards counted M times. This
    test would catch that regression.

    Setup: two LEIE records for the same person with different
    excldates. The OLDER one is dated 2010 (decay <0.4); the NEWER
    one is "today" (decay = 1.0). This is intentional belt-and-
    suspenders: if the multi-LEIE collapse picks the FRESHEST
    (correct behavior, migration 062), raw_value = 300 * 1.0 = 300.
    If a regression picks the older record (wrong), raw_value
    drops below 200 and the assertion fails. The test thus
    validates BOTH the multi-LEIE collapse AND the LEIE-age decay
    integration.
    """
    import datetime as _dt
    today_yyyymmdd = _dt.date.today().strftime("%Y%m%d")
    _seed_leie_individual(
        fraud_db, record_hash="3" * 64,
        lastname="LEE", firstname="ANNA",
        excldate="20100101", excltype="1128A1",
    )
    _seed_leie_individual(
        fraud_db, record_hash="4" * 64,
        lastname="LEE", firstname="ANNA",
        excldate=today_yyyymmdd, excltype="1128B7",
    )
    _seed_award(
        fraud_db, award_id="AWD_L1",
        recipient_name="LEE, ANNA", award_amount=100.0,
    )
    _seed_award(
        fraud_db, award_id="AWD_L2",
        recipient_name="LEE, ANNA", award_amount=200.0,
    )
    fraud_db.commit()

    _refresh(fraud_db, "2024")
    rv = _scalar(
        fraud_db,
        "SELECT raw_value FROM derived.fraud_signal_observation "
        "WHERE entity_id='LEE|ANNA' "
        "  AND signal_id='entity_funded_and_excluded'",
    )
    assert float(rv) == 300.0  # type: ignore[arg-type]


def test_idempotency_same_cycle(fraud_db: psycopg.Connection) -> None:
    """Re-running the refresher for the same cycle yields the same rows."""
    _seed_leie_individual(
        fraud_db, record_hash="a" * 64,
        lastname="DOE", firstname="JANE",
    )
    _seed_award(
        fraud_db, award_id="AWD_1",
        recipient_name="DOE, JANE", award_amount=100.0,
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
        "  AND signal_id='entity_funded_and_excluded'",
    )
    assert total == 1


def test_cycle_isolation(fraud_db: psycopg.Connection) -> None:
    """Refreshing 2024 does not delete or touch the 2020 slice."""
    _seed_leie_individual(
        fraud_db, record_hash="a" * 64,
        lastname="DOE", firstname="JANE",
    )
    _seed_award(
        fraud_db, award_id="AWD_1",
        recipient_name="DOE, JANE",
    )
    fraud_db.commit()

    _refresh(fraud_db, "2020")
    _refresh(fraud_db, "2024")

    n_2020 = _int_scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle='2020' "
        "  AND signal_id='entity_funded_and_excluded'",
    )
    n_2024 = _int_scalar(
        fraud_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle='2024' "
        "  AND signal_id='entity_funded_and_excluded'",
    )
    assert n_2020 == 1
    assert n_2024 == 1


def test_percentile_grows_with_bucket_size(
    fraud_db: psycopg.Connection,
) -> None:
    """1 match in N=10 bucket has higher percentile than 1 match in N=2.

    Rate-based percentile = 1 - (n_flagged / n_in_bucket). Larger
    bucket with same flagged count -> rarer event -> higher
    percentile. Confirms the percentile semantics survive bucket-
    size variation.
    """
    # Bucket size 10: 9 unrelated person-recipients + 1 matched.
    # NOTE: f_normalize_name_token strips digits, so we use
    # distinct alphabetic last names to keep canonical keys unique.
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
        _seed_award(
            fraud_db, award_id=f"AWD_OTHER_{i}",
            recipient_name=f"{last}, {first}",
        )
    _seed_leie_individual(
        fraud_db, record_hash="a" * 64,
        lastname="DOE", firstname="JANE",
    )
    _seed_award(
        fraud_db, award_id="AWD_M",
        recipient_name="DOE, JANE",
    )
    fraud_db.commit()

    _refresh(fraud_db, "2024")
    pct = _scalar(
        fraud_db,
        "SELECT peer_percentile FROM derived.fraud_signal_observation "
        "WHERE entity_id='DOE|JANE' "
        "  AND signal_id='entity_funded_and_excluded'",
    )
    # 1 - 1/10 = 0.9
    assert abs(float(pct) - 0.9) < 1e-9  # type: ignore[arg-type]


def test_empty_bucket_returns_zero(fraud_db: psycopg.Connection) -> None:
    """Refresher returns 0 (no error) when no individual recipients exist."""
    _seed_leie_individual(
        fraud_db, record_hash="a" * 64,
        lastname="DOE", firstname="JANE",
    )
    # All recipients are corporate-shaped -> NULL canonical individual.
    _seed_award(
        fraud_db, award_id="AWD_C1",
        recipient_name="ACME CORP",
    )
    _seed_award(
        fraud_db, award_id="AWD_C2",
        recipient_name="BETA INDUSTRIES INC",
    )
    fraud_db.commit()

    n = _refresh(fraud_db, "2024")
    assert n == 0


def test_evidence_url_well_formed(fraud_db: psycopg.Connection) -> None:
    """Evidence URL has the expected query parameters and shape."""
    _seed_leie_individual(
        fraud_db, record_hash="a" * 64,
        lastname="DOE", firstname="JANE",
    )
    _seed_award(
        fraud_db, award_id="AWD_1",
        recipient_name="DOE, JANE",
    )
    fraud_db.commit()

    _refresh(fraud_db, "2024")
    url = _scalar(
        fraud_db,
        "SELECT evidence_url FROM derived.fraud_signal_observation "
        "WHERE signal_id='entity_funded_and_excluded'",
    )
    s = str(url)
    assert s.startswith("/fec/risk/entities/contractor/")
    assert "DOE_JANE" in s  # path-safe escape of canonical_individual
    assert "signal=entity_funded_and_excluded" in s
    assert "cycle=2024" in s
    assert "leie=" in s
