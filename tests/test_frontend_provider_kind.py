"""Frontend-wiring contract tests for the new ``provider`` entity_kind.

Pillar 2 (civic integrity) FRAUD-F7-frontend. These tests pin the SQL
contract the Next.js ``/risk/[kind]/[id]`` URL space depends on for the
entity_kind added by mig 100/101 + seed 041.

The contract codified here (no Python -> TypeScript import; pinned at the
SQL boundary the TS layer reads):

    * The L1 ``fraud_signal_observation.entity_kind`` CHECK accepts
      ``provider`` -- this is what makes ``/risk/provider/<npi>`` a valid
      surface.

    * lib/queries.ts ``getEntityHeader({kind:'provider', id, cycle})``
      reads from ``raw.cms_partd_prescriber`` filtered by
      ``npi = :id AND data_year::text = :cycle`` and projects
      ``display_name`` (first+last), ``is_nj`` (practice state == NJ), and
      ``prscrbr_type`` (-> office_code). We pin the column existence + the
      exact query so a future schema change breaks this test BEFORE it
      breaks the prod /risk page.
"""

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

_NPI = "1234567893"
_CYCLE = "2023"


@pytest.fixture
def fe_db(live_pg: psycopg.Connection) -> psycopg.Connection:
    conn = live_pg
    with conn.cursor() as cur:
        for sch in ("governance", "derived", "raw", "ref"):
            cur.execute(f"DROP SCHEMA IF EXISTS {sch} CASCADE")
        cur.execute(
            "DO $$ DECLARE r record; "
            "BEGIN FOR r IN SELECT viewname FROM pg_views "
            "WHERE schemaname='public' AND viewname LIKE 'v_%%' LOOP "
            "EXECUTE 'DROP VIEW IF EXISTS public.' || quote_ident(r.viewname) "
            "|| ' CASCADE'; END LOOP; END $$;",
        )
    conn.commit()
    apply_migrations(conn, discover(MIGRATIONS_DIR))
    apply_migrations(conn, discover(SEEDS_DIR))
    conn.commit()
    return conn


def _seed_prescriber(conn: psycopg.Connection, *, state: str = "NJ") -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.cms_partd_prescriber ("
            " data_year, npi, prscrbr_last_org_name, prscrbr_first_name, "
            " prscrbr_city, prscrbr_state_abrvtn, prscrbr_type, "
            " tot_clms, tot_drug_cst, tot_benes, "
            " source_url, source_sha256, source_vintage"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                2023, _NPI, "DOE", "JANE", "NEWARK", state,
                "Internal Medicine", 100, 50000.0, 50,
                "https://example.test/partd.csv", "0" * 64, "CY2023",
            ),
        )
    conn.commit()


def test_provider_in_l1_entity_kind_check(fe_db: psycopg.Connection) -> None:
    with fe_db.cursor() as cur:
        cur.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'fraud_signal_observation_entity_kind_check'"
        )
        row = cur.fetchone()
    assert row is not None
    assert "'provider'" in row[0], (
        f"L1 entity_kind CHECK does not accept 'provider': {row[0]}"
    )


def test_cms_partd_exposes_required_header_columns(
    fe_db: psycopg.Connection,
) -> None:
    required = {
        "npi",
        "data_year",
        "prscrbr_first_name",
        "prscrbr_last_org_name",
        "prscrbr_state_abrvtn",
        "prscrbr_type",
    }
    with fe_db.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='raw' AND table_name='cms_partd_prescriber'"
        )
        cols = {r[0] for r in cur.fetchall()}
    missing = required - cols
    assert not missing, f"raw.cms_partd_prescriber missing columns: {missing}"


def test_get_entity_header_query_resolves_nj_provider(
    fe_db: psycopg.Connection,
) -> None:
    """Mirror of lib/queries.ts getEntityHeader('provider', ...)."""
    _seed_prescriber(fe_db, state="NJ")
    with fe_db.cursor() as cur:
        cur.execute(
            """
            SELECT
                npi AS entity_id,
                NULLIF(TRIM(
                  COALESCE(prscrbr_first_name, '') || ' ' ||
                  COALESCE(prscrbr_last_org_name, '')
                ), '')                      AS display_name,
                (prscrbr_state_abrvtn = 'NJ') AS is_nj,
                prscrbr_type
            FROM raw.cms_partd_prescriber
            WHERE npi = %s AND data_year::text = %s
            LIMIT 1
            """,
            (_NPI, _CYCLE),
        )
        row = cur.fetchone()
    assert row is not None, "getEntityHeader query found no provider row"
    entity_id, display_name, is_nj, ptype = row
    assert entity_id == _NPI
    assert display_name == "JANE DOE"
    assert is_nj is True
    assert ptype == "Internal Medicine"


def test_get_entity_header_query_is_nj_false_out_of_state(
    fe_db: psycopg.Connection,
) -> None:
    _seed_prescriber(fe_db, state="TX")
    is_nj = None
    with fe_db.cursor() as cur:
        cur.execute(
            "SELECT (prscrbr_state_abrvtn = 'NJ') "
            "FROM raw.cms_partd_prescriber "
            "WHERE npi = %s AND data_year::text = %s",
            (_NPI, _CYCLE),
        )
        (is_nj,) = cur.fetchone()
    assert is_nj is False


def test_get_entity_header_query_isolates_by_data_year(
    fe_db: psycopg.Connection,
) -> None:
    """A prescriber row for data_year 2023 must not match cycle='2099'."""
    _seed_prescriber(fe_db)
    with fe_db.cursor() as cur:
        cur.execute(
            "SELECT npi FROM raw.cms_partd_prescriber "
            "WHERE npi = %s AND data_year::text = %s",
            (_NPI, "2099"),
        )
        rows = cur.fetchall()
    assert rows == []
