"""Tests for migration 051_fraud_signal_observation_adapter (Tier 4 v3 step 2).

Two layers, same pattern as test_migrations_fraud_signal.py:

1. STATIC (no DB): the migration file declares every per-signal function
   name + the dispatcher. Catches silent renames that would break the
   Dagster asset wiring downstream.

2. LIVE (live_pg): apply migrations against an ephemeral Postgres,
   INSERT engineered raw.fec_candidate + raw.fec_committee fixtures
   that trigger every v2.A signal at least once, run the dispatcher,
   then assert that derived.fraud_signal_observation contains the
   expected (entity_kind, entity_id, signal_id) tuples and that the
   percentile/severity contracts hold.

The fixture is a Python-side INSERT (not a synthetic ZIP through the
ingester) because the adapter under test reads from raw.fec_* and the
v2.A views in migration 040 -- the loader pipeline is exercised by
other tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import psycopg


REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATION_PATH = (
    REPO_ROOT / "db" / "migrations" / "051_fraud_signal_observation_adapter.sql"
)


# ============================================================================
# 1. STATIC checks (no DB)
# ============================================================================


_EXPECTED_REFRESHERS = (
    "derived.refresh_treasurer_concentration_observations",
    "derived.refresh_candidate_no_pcc_observations",
    "derived.refresh_candidate_broken_pcc_observations",
    "derived.refresh_candidate_multiple_pccs_observations",
    "derived.refresh_committee_address_clusters_observations",
    "derived.refresh_committee_name_collisions_observations",
    "derived.refresh_candidate_namesakes_observations",
    "derived.refresh_treasurer_is_candidate_observations",
)


def test_migration_file_exists() -> None:
    """The migration ships at the canonical path."""
    assert MIGRATION_PATH.is_file(), f"missing: {MIGRATION_PATH}"


def test_migration_declares_all_eight_per_signal_refreshers() -> None:
    """Every v2.A signal must have its own per-signal refresher function."""
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    for fn in _EXPECTED_REFRESHERS:
        assert (
            f"CREATE OR REPLACE FUNCTION {fn}" in sql
        ), f"missing per-signal refresher: {fn}"


def test_migration_declares_dispatcher() -> None:
    """The top-level dispatcher must exist (Dagster asset entry point)."""
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    assert (
        "CREATE OR REPLACE FUNCTION derived.refresh_all_fraud_signal_observations"
        in sql
    )


def test_migration_uses_cume_dist_for_continuous_signals() -> None:
    """Continuous-signal percentiles must use CUME_DIST (at-or-below), not
    PERCENT_RANK (rank-based). The two disagree on the lowest-value rows
    (PERCENT_RANK gives 0; CUME_DIST gives n_min/n) and only one matches
    the documented "fraction of peers at-or-below this value" semantics."""
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "CUME_DIST() OVER" in sql
    assert "PERCENT_RANK() OVER" not in sql


# ============================================================================
# 2. LIVE checks (live_pg)
# ============================================================================


pytestmark = pytest.mark.live_pg


@pytest.fixture
def fraud_db(live_pg: psycopg.Connection) -> psycopg.Connection:
    """Apply all migrations + seeds against a clean DB; yield the conn."""
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
            "    EXECUTE 'DROP VIEW IF EXISTS public.' || quote_ident(r.viewname) "
            "            || ' CASCADE'; "
            "  END LOOP; "
            "END $$;",
        )
    conn.commit()

    apply_migrations(conn, discover(MIGRATIONS_DIR))
    apply_migrations(conn, discover(SEEDS_DIR))
    return conn


def _seed_engineered_anomalies(conn: psycopg.Connection) -> None:
    """INSERT raw.fec_candidate + raw.fec_committee rows that trigger every
    v2.A signal in migration 040 at least once, in cycle 2024.

    Engineered anomalies (the same pattern as
    tests/test_serving_fec_metrics.py's synthetic fixture, ported to
    direct INSERTs so this test does not depend on the loader pipeline):

      - candidate_no_pcc          KAPLAN (NJ Senate, no PCC)
      - candidate_broken_pcc      JONES  (NJ House, PCC = C99999999, not in cm)
      - candidate_multiple_pccs   SMITH  (H0NJ00004, two 'P' cmtes)
      - candidate_namesakes       SMITH JOHN x 2 (H0NJ00004 + H0NJ00005)
      - treasurer_concentration   "DOE, JOHN" listed on 3 cmtes
      - committee_address_clusters PO BOX 99 / NJ / 12345 hosts 3 cmtes
      - committee_name_collisions "FRIENDS OF JOHN" appears on 2 cmtes
      - treasurer_is_candidate    ADAMS PCC tres_nm = "ADAMS, JANE"
    """
    src = ("file://test", "0" * 64, "test-2024")

    candidates = [
        # cand_id, cand_name, party, year, off_st, off, dist, ici, status, pcc
        ("S0NJ00001", "BOOKER, CORY ANTHONY", "DEM", 2024, "NJ", "S",
         "00", "I", "C", "C00500001"),
        # No PCC
        ("S0NJ00002", "KAPLAN, KENNETH",      "REP", 2024, "NJ", "S",
         "00", "C", "C", ""),
        # Broken PCC: C99999999 does not exist in cm
        ("H0NJ00003", "JONES, MARY",          "DEM", 2024, "NJ", "H",
         "01", "C", "C", "C99999999"),
        # Namesake group: same canonical name, same office, same state
        ("H0NJ00004", "SMITH, JOHN",          "DEM", 2024, "NJ", "H",
         "02", "C", "C", "C00500004"),
        ("H0NJ00005", "SMITH, JOHN",          "REP", 2024, "NJ", "H",
         "02", "C", "C", "C00500005"),
        # Treasurer-is-candidate
        ("S0NJ00006", "ADAMS, JANE",          "IND", 2024, "NJ", "S",
         "00", "O", "C", "C00500006"),
    ]
    committees = [
        # cmte_id, cmte_nm, tres_nm, st1, st2, city, st, zip, dsgn, tp, pty,
        # filing_freq, org_tp, conn_org, cand_id
        ("C00500001", "BOOKER FOR SENATE",      "WHITE, ELIZABETH",
         "PO BOX 32157", None, "NEWARK", "NJ", "07102",
         "P", "S", "DEM", "Q", None, None, "S0NJ00001"),
        # 3-cmte treasurer concentration + 3-cmte address cluster
        ("C00500010", "FRIENDS OF JOHN",        "DOE, JOHN",
         "PO BOX 99", None, "TRENTON", "NJ", "12345",
         "U", "N", "DEM", "Q", None, None, None),
        ("C00500011", "JOHN VICTORY FUND",      "DOE, JOHN",
         "PO BOX 99", None, "TRENTON", "NJ", "12345",
         "U", "N", "DEM", "Q", None, None, None),
        ("C00500012", "JOHN LEADERSHIP PAC",    "DOE, JOHN",
         "PO BOX 99", None, "TRENTON", "NJ", "12345",
         "D", "N", "DEM", "Q", None, None, None),
        # Name collision with C00500010 ("FRIENDS OF JOHN")
        ("C00500013", "FRIENDS OF JOHN",        "ROE, RICHARD",
         "5 MAIN ST", None, "PRINCETON", "NJ", "08540",
         "U", "N", "DEM", "Q", None, None, None),
        # SMITH multi-PCC: both designated 'P', both linked to H0NJ00004
        ("C00500004", "SMITH FOR HOUSE",        "SMITH, JANE",
         "10 OAK ST", None, "MARLTON", "NJ", "08053",
         "P", "H", "DEM", "Q", None, None, "H0NJ00004"),
        ("C00500014", "JOHN SMITH HOUSE 2024",  "SMITH, JANE",
         "10 OAK ST", None, "MARLTON", "NJ", "08053",
         "P", "H", "DEM", "Q", None, None, "H0NJ00004"),
        # Treasurer-is-candidate
        ("C00500006", "ADAMS FOR SENATE",       "ADAMS, JANE",
         "1 ELM ST", None, "MONTCLAIR", "NJ", "07042",
         "P", "S", "IND", "Q", None, None, "S0NJ00006"),
        # SMITH JOHN's other PCC for completeness
        ("C00500005", "SMITH FOR CONGRESS",     "MILLER, BOB",
         "12 OAK ST", None, "MARLTON", "NJ", "08053",
         "P", "H", "REP", "Q", None, None, "H0NJ00005"),
    ]

    with conn.cursor() as cur:
        for c in candidates:
            cur.execute(
                "INSERT INTO raw.fec_candidate ("
                "cycle, cand_id, cand_name, cand_pty_affiliation, "
                "cand_election_yr, cand_office_st, cand_office, "
                "cand_office_district, cand_ici, cand_status, cand_pcc, "
                "source_url, source_sha256, source_vintage) "
                "VALUES ('2024', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, %s, %s)",
                (*c, *src),
            )
        for cm in committees:
            cur.execute(
                "INSERT INTO raw.fec_committee ("
                "cycle, cmte_id, cmte_nm, tres_nm, cmte_st1, cmte_st2, "
                "cmte_city, cmte_st, cmte_zip, cmte_dsgn, cmte_tp, "
                "cmte_pty_affiliation, cmte_filing_freq, org_tp, "
                "connected_org_nm, cand_id, source_url, source_sha256, "
                "source_vintage) "
                "VALUES ('2024', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, %s, %s, %s, %s, %s, %s, %s)",
                (*cm, *src),
            )
    conn.commit()


def _run_dispatcher(conn: psycopg.Connection, cycle: str = "2024") -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_all_fraud_signal_observations(%s)",
            (cycle,),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def test_dispatcher_populates_l1_for_every_signal(
    fraud_db: psycopg.Connection,
) -> None:
    """Every one of the 8 v2.A signals must produce at least one L1 row
    against the engineered anomaly fixture."""
    _seed_engineered_anomalies(fraud_db)
    n_total = _run_dispatcher(fraud_db)
    assert n_total >= 8, f"dispatcher returned only {n_total} rows"

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT signal_id, COUNT(*) "
            "FROM derived.fraud_signal_observation "
            "WHERE cycle = '2024' "
            "GROUP BY signal_id"
        )
        counts = dict(cur.fetchall())

    expected_signals = {
        "treasurer_concentration",
        "candidate_no_pcc",
        "candidate_broken_pcc",
        "candidate_multiple_pccs",
        "committee_address_clusters",
        "committee_name_collisions",
        "candidate_namesakes",
        "treasurer_is_candidate",
    }
    assert expected_signals.issubset(counts.keys()), (
        f"signals missing from L1: {expected_signals - counts.keys()}"
    )
    for sig in expected_signals:
        assert counts[sig] >= 1, f"signal {sig} produced 0 L1 rows"


def test_treasurer_concentration_emits_to_treasurer_entity(
    fraud_db: psycopg.Connection,
) -> None:
    """treasurer_concentration's entity_kind is 'treasurer', entity_id
    is the canonical name. The fixture has DOE, JOHN on 3 cmtes and
    SMITH, JANE on 2 cmtes (both SMITH PCCs); the v2.A view threshold
    is n_committees >= 2 so both fire. CUME_DIST within the single
    'kind=treasurer' bucket gives DOE percentile 1.0 (max) and SMITH
    percentile 0.5 (1 of 2 at-or-below)."""
    _seed_engineered_anomalies(fraud_db)
    _run_dispatcher(fraud_db)

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT entity_id, raw_value, severity, peer_bucket, "
            "       peer_percentile, evidence_url "
            "FROM derived.fraud_signal_observation "
            "WHERE signal_id = 'treasurer_concentration' "
            "  AND cycle = '2024' "
            "ORDER BY raw_value DESC"
        )
        rows = cur.fetchall()

    assert len(rows) == 2, f"expected DOE + SMITH; got {[r[0] for r in rows]}"

    # Top of the bucket: DOE, JOHN, n=3
    eid, raw, sev, bucket, pct, url = rows[0]
    assert eid == "DOE, JOHN"
    assert int(raw) == 3
    assert sev == 3
    assert bucket == "kind=treasurer"
    assert float(pct) == pytest.approx(1.0)
    assert url == "/fec/metrics/treasurer_concentration?cycle=2024"

    # Below: SMITH, JANE, n=2
    eid2, raw2, _sev2, _bucket2, pct2, _url2 = rows[1]
    assert eid2 == "SMITH, JANE"
    assert int(raw2) == 2
    assert float(pct2) == pytest.approx(0.5)


def test_committee_address_clusters_emits_to_address_entity(
    fraud_db: psycopg.Connection,
) -> None:
    """address-clusters entity_id concatenates address_canonical + state."""
    _seed_engineered_anomalies(fraud_db)
    _run_dispatcher(fraud_db)

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT entity_kind, entity_id, raw_value, severity, peer_bucket "
            "FROM derived.fraud_signal_observation "
            "WHERE signal_id = 'committee_address_clusters' "
            "  AND cycle = '2024'"
        )
        rows = cur.fetchall()

    assert len(rows) == 1
    kind, eid, raw, sev, bucket = rows[0]
    assert kind == "address"
    # The fixture has one address (PO BOX 99) hosting three committees
    assert eid.startswith("PO BOX 99|")
    assert eid.endswith("|NJ")
    assert int(raw) == 3
    assert sev == 4
    assert bucket == "state=NJ"


def test_candidate_signals_use_office_state_bucket(
    fraud_db: psycopg.Connection,
) -> None:
    """All four candidate-keyed signals encode (office, state) in
    peer_bucket. ICI is intentionally absent in v3 step 2 (insufficient
    single-cycle bucket size)."""
    _seed_engineered_anomalies(fraud_db)
    _run_dispatcher(fraud_db)

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT peer_bucket "
            "FROM derived.fraud_signal_observation "
            "WHERE entity_kind = 'candidate' "
            "  AND cycle = '2024'"
        )
        buckets = {r[0] for r in cur.fetchall()}

    for b in buckets:
        assert b.startswith("office="), f"unexpected candidate bucket: {b}"
        assert "|state=" in b, f"unexpected candidate bucket: {b}"
        assert "ici=" not in b, f"ICI must not be in v3-step-2 buckets: {b}"


def test_namesake_signal_fans_out_to_each_cand_id(
    fraud_db: psycopg.Connection,
) -> None:
    """candidate_namesakes UNNESTs candidate_ids to one obs per cand_id.
    The fixture has SMITH, JOHN under H0NJ00004 + H0NJ00005 -> 2 rows."""
    _seed_engineered_anomalies(fraud_db)
    _run_dispatcher(fraud_db)

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT entity_id, raw_value, peer_percentile "
            "FROM derived.fraud_signal_observation "
            "WHERE signal_id = 'candidate_namesakes' "
            "  AND cycle = '2024' "
            "ORDER BY entity_id"
        )
        rows = cur.fetchall()

    assert len(rows) == 2
    ids = [r[0] for r in rows]
    assert ids == ["H0NJ00004", "H0NJ00005"]
    # Both share the same group raw_value (n_cand_ids = 2) and percentile
    assert int(rows[0][1]) == 2
    assert int(rows[1][1]) == 2
    assert float(rows[0][2]) == pytest.approx(float(rows[1][2]))


def test_dispatcher_is_idempotent(fraud_db: psycopg.Connection) -> None:
    """Re-running the dispatcher for the same cycle replaces, not appends."""
    _seed_engineered_anomalies(fraud_db)
    n_first = _run_dispatcher(fraud_db)
    n_second = _run_dispatcher(fraud_db)

    assert n_first == n_second, (
        "dispatcher must be idempotent: per-signal DELETE-then-INSERT"
    )

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM derived.fraud_signal_observation "
            "WHERE cycle = '2024'"
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == n_first


def test_dispatcher_only_touches_requested_cycle(
    fraud_db: psycopg.Connection,
) -> None:
    """Calling refresh for cycle X must not delete cycle Y's rows."""
    _seed_engineered_anomalies(fraud_db)
    _run_dispatcher(fraud_db, cycle="2024")

    # Hand-insert a sentinel observation in a different cycle.
    with fraud_db.cursor() as cur:
        cur.execute(
            "INSERT INTO derived.fraud_signal_observation ("
            "cycle, entity_kind, entity_id, signal_id, raw_value, "
            "severity, peer_bucket, peer_percentile, evidence_url) "
            "VALUES ('2022', 'committee', 'C0SENT0001', "
            "'treasurer_concentration', 5, 3, 'kind=treasurer', 0.99, "
            "'/fec/metrics/treasurer_concentration?cycle=2022')"
        )
    fraud_db.commit()

    _run_dispatcher(fraud_db, cycle="2024")

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM derived.fraud_signal_observation "
            "WHERE cycle = '2022' AND entity_id = 'C0SENT0001'"
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 1, "2024 refresh must not touch 2022 rows"


def test_l1_evidence_panel_lookup_returns_signals_for_one_entity(
    fraud_db: psycopg.Connection,
) -> None:
    """The L4 evidence panel will query L2 (v_entity_fraud_features) keyed
    on (entity_kind, entity_id). Smoke-test that lookup against the
    fixture: H0NJ00004 (SMITH, JOHN) fires both candidate_namesakes and
    candidate_multiple_pccs, so its L2 row should list both signals."""
    _seed_engineered_anomalies(fraud_db)
    _run_dispatcher(fraud_db)

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT n_signals_fired, signals_fired, severities, "
            "       peer_percentiles "
            "FROM derived.v_entity_fraud_features "
            "WHERE cycle = '2024' "
            "  AND entity_kind = 'candidate' "
            "  AND entity_id = 'H0NJ00004'"
        )
        rows = cur.fetchall()

    assert len(rows) == 1
    n, signals, severities, _percentiles = rows[0]
    assert n == 2
    assert sorted(signals) == [
        "candidate_multiple_pccs", "candidate_namesakes",
    ]
    # parallel arrays in alphabetical signal_id order
    sig_to_sev = dict(zip(signals, severities, strict=True))
    assert sig_to_sev["candidate_multiple_pccs"] == 2
    assert sig_to_sev["candidate_namesakes"]     == 3
