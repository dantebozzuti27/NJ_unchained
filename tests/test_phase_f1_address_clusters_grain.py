"""Live-PG regression tests for migration 087 (committee_address_clusters grain fix).

VISION_2026 Pillar 2 -- Phase F-1 substrate fix.

This test module pins three behaviors of the
``derived.fec_committee_address_clusters`` view + the
``derived.refresh_committee_address_clusters_observations`` refresher
after migration 087:

  1. ZIP+4 collapses: a single physical address registered with both
     ``33606`` and ``336062647`` produces ONE view row whose
     ``n_committees`` is the union of distinct cmte_ids across both
     filing styles, not two rows.

  2. Same-street-name across municipalities stays distinct: street
     ``421 OFFICE PARK DR`` filed by some committees as Birmingham AL
     and others as Mountain Brook AL must remain TWO observations
     (legitimately different physical addresses on a road that
     straddles a municipal boundary).

  3. Refresher idempotency under zip-extension noise: the
     ``derived.refresh_committee_address_clusters_observations`` call
     completes without a PK violation when the synthetic raw data
     contains zip+4 noise. Pre-087 this raised
     ``UniqueViolation`` on the (cycle, entity_kind, entity_id, signal_id)
     PK; post-087 the entity_id encodes the full address|city|state|zip5
     grain, which matches the source view.

These tests use synthetic ``raw.fec_committee`` rows. They do NOT depend
on the real FEC bulk data being present in the test DB (the live FEC
loader is its own concern).
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


# ---------------------------------------------------------------------------
# Fixture: clean DB with all migrations applied + fraud_signal_observation
# table empty. raw.fec_committee is empty too -- each test seeds it.
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_substrate(live_pg: psycopg.Connection) -> psycopg.Connection:
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


def _insert_committee(
    conn: psycopg.Connection,
    *,
    cycle: str,
    cmte_id: str,
    cmte_nm: str,
    cmte_st1: str,
    cmte_city: str,
    cmte_st: str,
    cmte_zip: str,
) -> None:
    """Insert one synthetic FEC committee row.

    Provides only the fields the address-clusters view reads; everything
    else is NULL. Other refreshers consume different columns, so leaving
    them NULL keeps each test's blast radius scoped.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw.fec_committee
                (cycle, cmte_id, cmte_nm, cmte_st1, cmte_city, cmte_st, cmte_zip,
                 source_url, source_sha256, source_vintage)
            VALUES (%s, %s, %s, %s, %s, %s, %s,
                    'synth://test/cm', 'deadbeef', 'TEST')
            """,
            (cycle, cmte_id, cmte_nm, cmte_st1, cmte_city, cmte_st, cmte_zip),
        )


# ===========================================================================
# Class A: source-view grain semantics
# ===========================================================================


class TestSourceViewGrain:
    def test_zip_plus_four_collapses_to_single_view_row(
        self, clean_substrate: psycopg.Connection
    ) -> None:
        """Same physical address with mixed zip5/zip+4 filings must produce
        ONE view row whose n_committees = union of distinct cmte_ids."""
        conn = clean_substrate
        # 4 committees at the same address. Two filed with zip5, two
        # with zip+4. Pre-087 the view emits two rows with n=2 each;
        # post-087 it emits one row with n=4.
        for i, zip_code in enumerate(
            ["33606", "33606", "336062647", "336062647"], start=1
        ):
            _insert_committee(
                conn,
                cycle="2024",
                cmte_id=f"C90000{i:03d}",
                cmte_nm=f"Test Committee {i}",
                cmte_st1="610 S BOULEVARD",
                cmte_city="TAMPA",
                cmte_st="FL",
                cmte_zip=zip_code,
            )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT zip_canonical, n_committees
                FROM derived.fec_committee_address_clusters
                WHERE cycle = '2024'
                  AND address_canonical = '610 S BOULEVARD'
                  AND state = 'FL'
                ORDER BY zip_canonical
                """
            )
            rows = cur.fetchall()
        assert len(rows) == 1, (
            f"expected zip+4 collapsing to 1 view row; got {len(rows)}: {rows}"
        )
        zip_canonical, n_committees = rows[0]
        assert zip_canonical == "33606", (
            f"zip_canonical should be 5-digit; got {zip_canonical!r}"
        )
        assert n_committees == 4, (
            f"n_committees should union both filing styles; got {n_committees}"
        )

    def test_same_street_different_city_stays_distinct(
        self, clean_substrate: psycopg.Connection
    ) -> None:
        """A street name shared across two municipalities (Birmingham vs
        Mountain Brook on 421 OFFICE PARK DR) MUST remain two observations:
        they are legitimately different physical addresses despite sharing
        a road name."""
        conn = clean_substrate
        # 3 committees in Birmingham, 4 in Mountain Brook.
        for i, city in enumerate(
            ["BIRMINGHAM"] * 3 + ["MOUNTAIN BROOK"] * 4, start=1
        ):
            _insert_committee(
                conn,
                cycle="2024",
                cmte_id=f"C91000{i:03d}",
                cmte_nm=f"Test Committee {i}",
                cmte_st1="421 OFFICE PARK DR",
                cmte_city=city,
                cmte_st="AL",
                cmte_zip="35223",
            )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT city_canonical, n_committees
                FROM derived.fec_committee_address_clusters
                WHERE cycle = '2024'
                  AND address_canonical = '421 OFFICE PARK DR'
                  AND state = 'AL'
                ORDER BY city_canonical
                """
            )
            rows = cur.fetchall()
        assert len(rows) == 2, (
            f"municipality boundary should yield 2 view rows; got {rows}"
        )
        cities = {row[0] for row in rows}
        assert cities == {"BIRMINGHAM", "MOUNTAIN BROOK"}, (
            f"unexpected cities: {cities}"
        )
        # Counts split correctly per city.
        per_city = dict(rows)
        assert per_city["BIRMINGHAM"] == 3
        assert per_city["MOUNTAIN BROOK"] == 4

    def test_zip_canonical_is_five_digits_max(
        self, clean_substrate: psycopg.Connection
    ) -> None:
        """zip_canonical MUST always be 0..5 digits (no zip+4 leakage)."""
        conn = clean_substrate
        # Mix of malformed zips: pure 5, zip+4, junk, dashes.
        for i, zip_code in enumerate(
            ["07102", "07302-1234", "08542", "8830", "1234567890"], start=1
        ):
            _insert_committee(
                conn,
                cycle="2024",
                cmte_id=f"C92000{i:03d}",
                cmte_nm=f"NJ Committee {i}",
                cmte_st1="100 MAIN ST",
                cmte_city="NEWARK",
                cmte_st="NJ",
                cmte_zip=zip_code,
            )
        # Need >=3 distinct cmte_ids per group to clear the HAVING filter.
        for i in range(6, 9):
            _insert_committee(
                conn,
                cycle="2024",
                cmte_id=f"C92000{i:03d}",
                cmte_nm=f"NJ Committee {i}",
                cmte_st1="100 MAIN ST",
                cmte_city="NEWARK",
                cmte_st="NJ",
                cmte_zip="07102",
            )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT zip_canonical
                FROM derived.fec_committee_address_clusters
                WHERE cycle = '2024'
                """
            )
            zips = [row[0] for row in cur.fetchall()]
        for z in zips:
            assert z is not None
            assert len(z) <= 5, f"zip_canonical {z!r} exceeds 5 digits"
            assert z.isdigit() or z == "", (
                f"zip_canonical {z!r} contains non-digits"
            )


# ===========================================================================
# Class B: refresher idempotency
# ===========================================================================


class TestRefresherIdempotency:
    def test_refresh_completes_when_zip_plus_four_noise_present(
        self, clean_substrate: psycopg.Connection
    ) -> None:
        """Pre-087 this raised UniqueViolation on the fraud_signal_observation
        PK because two view rows per physical address mapped to the same
        entity_id. Post-087 the refresher completes cleanly."""
        conn = clean_substrate
        # Same physical address, mixed zip5 / zip+4 (>=3 distinct cmte_ids
        # required for HAVING filter).
        for i, zip_code in enumerate(
            ["27624", "27624", "27624", "276247275", "276247275"], start=1
        ):
            _insert_committee(
                conn,
                cycle="2024",
                cmte_id=f"C93000{i:03d}",
                cmte_nm=f"NC Committee {i}",
                cmte_st1="PO BOX 97275",
                cmte_city="RALEIGH",
                cmte_st="NC",
                cmte_zip=zip_code,
            )
        conn.commit()

        # MUST NOT RAISE.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT derived.refresh_committee_address_clusters_observations('2024')"
            )
            n_inserted = cur.fetchone()[0]
        conn.commit()

        assert n_inserted == 1, (
            f"expected exactly 1 observation (zip+4 collapses); got {n_inserted}"
        )

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT entity_id, raw_value, severity, peer_bucket
                FROM derived.fraud_signal_observation
                WHERE cycle     = '2024'
                  AND signal_id = 'committee_address_clusters'
                """
            )
            rows = cur.fetchall()
        assert len(rows) == 1
        entity_id, raw_value, severity, peer_bucket = rows[0]
        # entity_id MUST contain all 4 grain attributes (address|city|state|zip5).
        assert entity_id.count("|") == 3, (
            f"entity_id should be address|city|state|zip5 4-tuple; "
            f"got {entity_id!r}"
        )
        assert "PO BOX 97275" in entity_id
        assert "RALEIGH" in entity_id
        assert "NC" in entity_id
        assert entity_id.endswith("|27624"), (
            f"entity_id should end with zip5; got {entity_id!r}"
        )
        # raw_value = union of distinct cmte_ids across both filing styles = 5.
        assert int(raw_value) == 5
        # severity stays at 4 per ref.fraud_signal_severity_calibration.
        assert severity == 4
        assert peer_bucket == "state=NC"

    def test_refresh_emits_two_observations_for_municipal_boundary(
        self, clean_substrate: psycopg.Connection
    ) -> None:
        """The Birmingham/Mountain Brook case must produce TWO distinct
        observations -- the entity_id grain preserves the city distinction."""
        conn = clean_substrate
        for i, city in enumerate(
            ["BIRMINGHAM"] * 3 + ["MOUNTAIN BROOK"] * 4, start=1
        ):
            _insert_committee(
                conn,
                cycle="2024",
                cmte_id=f"C94000{i:03d}",
                cmte_nm=f"AL Committee {i}",
                cmte_st1="421 OFFICE PARK DR",
                cmte_city=city,
                cmte_st="AL",
                cmte_zip="35223",
            )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT derived.refresh_committee_address_clusters_observations('2024')"
            )
            n_inserted = cur.fetchone()[0]
        conn.commit()

        assert n_inserted == 2

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT entity_id, raw_value
                FROM derived.fraud_signal_observation
                WHERE cycle     = '2024'
                  AND signal_id = 'committee_address_clusters'
                ORDER BY entity_id
                """
            )
            rows = cur.fetchall()
        assert len(rows) == 2
        # Different cities -> different entity_ids -> different observations.
        entity_ids = {row[0] for row in rows}
        assert any("BIRMINGHAM" in eid for eid in entity_ids)
        assert any("MOUNTAIN BROOK" in eid for eid in entity_ids)
        per_city = {
            ("BIRMINGHAM" if "BIRMINGHAM" in row[0] else "MOUNTAIN BROOK"): row[1]
            for row in rows
        }
        assert int(per_city["BIRMINGHAM"]) == 3
        assert int(per_city["MOUNTAIN BROOK"]) == 4

    def test_refresh_is_re_runnable(
        self, clean_substrate: psycopg.Connection
    ) -> None:
        """Calling the refresher twice must produce identical results --
        the DELETE WHERE signal_id + INSERT pattern is idempotent."""
        conn = clean_substrate
        for i in range(1, 5):
            _insert_committee(
                conn,
                cycle="2024",
                cmte_id=f"C95000{i:03d}",
                cmte_nm=f"VA Committee {i}",
                cmte_st1="PO BOX 2485",
                cmte_city="SPRINGFIELD",
                cmte_st="VA",
                cmte_zip="22152" if i % 2 == 0 else "221520485",
            )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT derived.refresh_committee_address_clusters_observations('2024')"
            )
            first = cur.fetchone()[0]
            cur.execute(
                "SELECT derived.refresh_committee_address_clusters_observations('2024')"
            )
            second = cur.fetchone()[0]
        conn.commit()

        assert first == second == 1
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM derived.fraud_signal_observation
                WHERE cycle     = '2024'
                  AND signal_id = 'committee_address_clusters'
                """
            )
            count_after = cur.fetchone()[0]
        assert count_after == 1


# ===========================================================================
# Class C: refresh_all_fraud_signal_observations integration
# ===========================================================================


class TestMasterRefresherCompletes:
    def test_refresh_all_completes_with_zip_plus_four_noise(
        self, clean_substrate: psycopg.Connection
    ) -> None:
        """The master refresher derived.refresh_all_fraud_signal_observations
        must run cleanly when raw.fec_committee contains zip+4 noise --
        the historical bug aborted the entire batch on this signal."""
        conn = clean_substrate
        # Two distinct physical addresses, both with zip+4 noise.
        for i, (addr, zip_code) in enumerate(
            [
                ("PO BOX 97275", "27624"),
                ("PO BOX 97275", "27624"),
                ("PO BOX 97275", "276247275"),
                ("PO BOX 97275", "276247275"),
                ("9856 ARCHER LN", "43017"),
                ("9856 ARCHER LN", "43017"),
                ("9856 ARCHER LN", "43017"),
                ("9856 ARCHER LN", "430178914"),
            ],
            start=1,
        ):
            _insert_committee(
                conn,
                cycle="2024",
                cmte_id=f"C96000{i:03d}",
                cmte_nm=f"Committee {i}",
                cmte_st1=addr,
                cmte_city="RALEIGH" if "BOX" in addr else "DUBLIN",
                cmte_st="NC" if "BOX" in addr else "OH",
                cmte_zip=zip_code,
            )
        conn.commit()

        # MUST NOT RAISE on the address-clusters refresher.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT derived.refresh_all_fraud_signal_observations('2024')"
            )
            n_total = cur.fetchone()[0]
        conn.commit()

        assert n_total >= 2, (
            f"master refresher should fire address_clusters at minimum; "
            f"got n_total={n_total}"
        )

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT entity_id
                FROM derived.fraud_signal_observation
                WHERE cycle     = '2024'
                  AND signal_id = 'committee_address_clusters'
                ORDER BY entity_id
                """
            )
            entity_ids = [row[0] for row in cur.fetchall()]
        # Two physical addresses -> two observations (zip+4 collapses each).
        assert len(entity_ids) == 2
        assert all(eid.count("|") == 3 for eid in entity_ids), (
            f"all entity_ids must be 4-tuples; got {entity_ids}"
        )
