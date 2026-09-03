"""Live-PG tests for FRAUD-V1 H-1B employer signals (mig 121)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import psycopg

from scripts.migrate import (
    MIGRATIONS_DIR,
    SEEDS_DIR,
    apply_migrations,
    discover,
)

pytestmark = pytest.mark.live_pg

_CYCLE = "2025"


@pytest.fixture
def h1b_db(live_pg: psycopg.Connection) -> psycopg.Connection:
    conn = live_pg
    with conn.cursor() as cur:
        for sch in ("governance", "derived", "raw", "ref"):
            cur.execute(f"DROP SCHEMA IF EXISTS {sch} CASCADE")
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
    conn.commit()
    return conn


def _insert_lca(
    conn: psycopg.Connection,
    *,
    case_number: str,
    employer: str,
    canonical: str,
    status: str,
    wage: float,
    pw: float,
    workers: int = 5,
    h1b_dependent: str | None = None,
    secondary_entity: str | None = None,
    pw_wage_level: str | None = None,
    willful_violator: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw.lca_disclosure (
                fiscal_year, fiscal_quarter, case_number, worksite_idx,
                case_status, visa_class, employer_name, employer_canonical_name,
                employer_state, worksite_state, total_workers,
                wage_rate_of_pay_from, wage_unit_of_pay,
                prevailing_wage, pw_unit_of_pay,
                h1b_dependent, secondary_entity, pw_wage_level, willful_violator,
                source_filename, source_sha256, source_schema_version
            ) VALUES (
                2025, 1, %s, 1,
                %s, 'H-1B', %s, %s,
                'NJ', 'NJ', %s,
                %s, 'Year',
                %s, 'Year',
                %s, %s, %s, %s,
                'test.csv', repeat('a', 64), 'v5_2023'
            )
            """,
            (
                case_number, status, employer, canonical, workers, wage, pw,
                h1b_dependent, secondary_entity, pw_wage_level, willful_violator,
            ),
        )
    conn.commit()


def _insert_whd(
    conn: psycopg.Connection,
    *,
    name: str,
    canonical: str,
    list_kind: str = "willful",
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw.dol_whd_h1b_list (
                list_kind, employer_name, employer_canonical_name,
                willful_violator, determination_date,
                source_url, source_filename, source_sha256
            ) VALUES (
                %s, %s, %s, TRUE, DATE '2025-04-11',
                'https://www.dol.gov/agencies/whd/immigration/h1b/willful-violator-list',
                'whd.html', repeat('c', 64)
            )
            """,
            (list_kind, name, canonical),
        )
    conn.commit()


def _insert_uscis(
    conn: psycopg.Connection,
    *,
    canonical: str,
    name: str,
    approvals: int,
    denials: int,
    state: str = "NJ",
    city: str = "Newark",
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw.uscis_h1b_employer (
                fiscal_year, employer_name, employer_canonical_name,
                tax_id_last4, petitioner_city, petitioner_state,
                initial_approval, initial_denial,
                continuing_approval, continuing_denial,
                source_filename, source_sha256, source_vintage
            ) VALUES (
                2025, %s, %s, '0001', %s, %s,
                %s, %s, 0, 0,
                'hub.csv', repeat('b', 64), 'FY2025'
            )
            """,
            (name, canonical, city, state, approvals, denials),
        )
    conn.commit()


def test_entity_kind_check_accepts_employer(h1b_db: psycopg.Connection) -> None:
    with h1b_db.cursor() as cur:
        cur.execute(
            """
            SELECT conname, pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conname = 'fraud_signal_observation_entity_kind_check'
            """
        )
        row = cur.fetchone()
    assert row is not None
    assert "employer" in row[1]


def test_below_pw_fires_on_shortfall(h1b_db: psycopg.Connection) -> None:
    _insert_lca(
        h1b_db,
        case_number="I-200-1",
        employer="LOW PAY LLC",
        canonical="low pay",
        status="CERTIFIED",
        wage=80_000,
        pw=100_000,
    )
    with h1b_db.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_signal_employer_below_prevailing_wage(%s)",
            (_CYCLE,),
        )
        n = cur.fetchone()[0]
        cur.execute(
            """
            SELECT entity_kind, raw_value, severity
            FROM derived.fraud_signal_observation
            WHERE signal_id = 'employer_below_prevailing_wage'
            """
        )
        obs = cur.fetchone()
    assert n == 1
    assert obs[0] == "employer"
    assert float(obs[1]) == 20_000
    assert obs[2] == 5


def test_below_pw_ignores_tiny_rounding_gap(h1b_db: psycopg.Connection) -> None:
    _insert_lca(
        h1b_db,
        case_number="I-200-2",
        employer="ROUNDING INC",
        canonical="rounding",
        status="CERTIFIED",
        wage=99_900,
        pw=100_000,
    )
    with h1b_db.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_signal_employer_below_prevailing_wage(%s)",
            (_CYCLE,),
        )
        n = cur.fetchone()[0]
    assert n == 0


def test_denial_rate_tail(h1b_db: psycopg.Connection) -> None:
    # 11 clean employers (low denial) + 1 extreme denier. Tail cutoff 0.99
    # with 12 peers flags only the top row.
    for i in range(11):
        _insert_uscis(
            h1b_db,
            canonical=f"clean {i}",
            name=f"CLEAN {i} INC",
            approvals=20,
            denials=0,
            city=f"City{i}",
        )
    _insert_uscis(
        h1b_db,
        canonical="deny heavy",
        name="DENY HEAVY INC",
        approvals=1,
        denials=20,
        city="Trenton",
    )
    with h1b_db.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_signal_employer_h1b_denial_rate_outlier(%s)",
            (_CYCLE,),
        )
        n = cur.fetchone()[0]
        cur.execute(
            """
            SELECT entity_id FROM derived.fraud_signal_observation
            WHERE signal_id = 'employer_h1b_denial_rate_outlier'
            """
        )
        ids = {r[0] for r in cur.fetchall()}
    assert n == 1
    assert ids == {"deny heavy"}


def test_master_refresher_invokes_h1b_lane(h1b_db: psycopg.Connection) -> None:
    _insert_lca(
        h1b_db,
        case_number="I-200-9",
        employer="LOW PAY LLC",
        canonical="low pay",
        status="CERTIFIED",
        wage=70_000,
        pw=120_000,
    )
    with h1b_db.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_all_fraud_signal_observations(%s)",
            (_CYCLE,),
        )
        cur.execute(
            """
            SELECT COUNT(*) FROM derived.fraud_signal_observation
            WHERE signal_id = 'employer_below_prevailing_wage'
            """
        )
        n = cur.fetchone()[0]
    assert n >= 1


def test_whd_list_match_fires(h1b_db: psycopg.Connection) -> None:
    _insert_lca(
        h1b_db,
        case_number="I-200-whd",
        employer="GOWRATECH LLC",
        canonical="gowratech",
        status="CERTIFIED",
        wage=120_000,
        pw=120_000,
    )
    _insert_whd(h1b_db, name="GowraTech, LLC", canonical="gowratech")
    with h1b_db.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_signal_employer_on_whd_willful_or_debarred(%s)",
            (_CYCLE,),
        )
        n = cur.fetchone()[0]
        cur.execute(
            """
            SELECT entity_id, raw_value, severity
            FROM derived.fraud_signal_observation
            WHERE signal_id = 'employer_on_whd_willful_or_debarred'
            """
        )
        obs = cur.fetchone()
    assert n == 1
    assert obs[0] == "gowratech"
    assert float(obs[1]) == 1
    assert obs[2] == 5


def test_level1_share_tail(h1b_db: psycopg.Connection) -> None:
    for i in range(11):
        for j in range(10):
            _insert_lca(
                h1b_db,
                case_number=f"I-200-L1-{i}-{j}",
                employer=f"LEVEL PEER {i} INC",
                canonical=f"level peer {i}",
                status="CERTIFIED",
                wage=120_000,
                pw=120_000,
                pw_wage_level="II",
            )
    for j in range(10):
        _insert_lca(
            h1b_db,
            case_number=f"I-200-L1-heavy-{j}",
            employer="LEVEL ONE HEAVY INC",
            canonical="level one heavy",
            status="CERTIFIED",
            wage=80_000,
            pw=80_000,
            pw_wage_level="I",
        )
    with h1b_db.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_signal_employer_level1_wage_share_outlier(%s)",
            (_CYCLE,),
        )
        n = cur.fetchone()[0]
        cur.execute(
            """
            SELECT entity_id FROM derived.fraud_signal_observation
            WHERE signal_id = 'employer_level1_wage_share_outlier'
            """
        )
        ids = {r[0] for r in cur.fetchall()}
    assert n == 1
    assert ids == {"level one heavy"}


def test_dependent_plus_anomaly_requires_corroboration(
    h1b_db: psycopg.Connection,
) -> None:
    _insert_lca(
        h1b_db,
        case_number="I-200-dep-only",
        employer="DEPENDENT ONLY LLC",
        canonical="dependent only",
        status="CERTIFIED",
        wage=120_000,
        pw=120_000,
        h1b_dependent="Y",
    )
    _insert_lca(
        h1b_db,
        case_number="I-200-dep-gap",
        employer="DEPENDENT GAP LLC",
        canonical="dependent gap",
        status="CERTIFIED",
        wage=70_000,
        pw=120_000,
        h1b_dependent="Y",
    )
    with h1b_db.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_signal_employer_below_prevailing_wage(%s)",
            (_CYCLE,),
        )
        cur.execute(
            "SELECT derived.refresh_signal_employer_h1b_dependent_plus_anomaly(%s)",
            (_CYCLE,),
        )
        n = cur.fetchone()[0]
        cur.execute(
            """
            SELECT entity_id, raw_value
            FROM derived.fraud_signal_observation
            WHERE signal_id = 'employer_h1b_dependent_plus_anomaly'
            """
        )
        rows = cur.fetchall()
    assert n == 1
    assert rows[0][0] == "dependent gap"
    assert int(rows[0][1]) == 1


def test_wage_at_pw_floor_tail(h1b_db: psycopg.Connection) -> None:
    for i in range(11):
        for j in range(10):
            _insert_lca(
                h1b_db,
                case_number=f"I-200-fl-{i}-{j}",
                employer=f"ABOVE FLOOR {i} INC",
                canonical=f"above floor {i}",
                status="CERTIFIED",
                wage=130_000,
                pw=120_000,
            )
    for j in range(10):
        _insert_lca(
            h1b_db,
            case_number=f"I-200-fl-heavy-{j}",
            employer="FLOOR HEAVY INC",
            canonical="floor heavy",
            status="CERTIFIED",
            wage=80_000,
            pw=80_000,
        )
    with h1b_db.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_signal_employer_wage_at_pw_floor_share_outlier(%s)",
            (_CYCLE,),
        )
        n = cur.fetchone()[0]
        cur.execute(
            """
            SELECT entity_id FROM derived.fraud_signal_observation
            WHERE signal_id = 'employer_wage_at_pw_floor_share_outlier'
            """
        )
        ids = {r[0] for r in cur.fetchall()}
    assert n == 1
    assert ids == {"floor heavy"}


def test_lca_willful_attestation_fires(h1b_db: psycopg.Connection) -> None:
    _insert_lca(
        h1b_db,
        case_number="I-200-willful",
        employer="WILLFUL ATTEST LLC",
        canonical="willful attest",
        status="CERTIFIED",
        wage=120_000,
        pw=120_000,
        willful_violator="Y",
    )
    with h1b_db.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_signal_employer_lca_willful_attestation(%s)",
            (_CYCLE,),
        )
        n = cur.fetchone()[0]
        cur.execute(
            """
            SELECT entity_id, raw_value, severity
            FROM derived.fraud_signal_observation
            WHERE signal_id = 'employer_lca_willful_attestation'
            """
        )
        obs = cur.fetchone()
    assert n == 1
    assert obs[0] == "willful attest"
    assert int(obs[1]) == 1
    assert obs[2] == 5
