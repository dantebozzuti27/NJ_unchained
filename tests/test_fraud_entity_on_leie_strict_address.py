"""Tests for entity_on_leie_strict_address (FRAUD-F5b-strict, mig 092).

The strict variant requires canonical name match AND zip5 + city match.
This file pins:

1. Canonical helpers (zip5 + city)
2. Refresher integration:
   - Happy path: name + address match -> 1 observation at sev=5
   - Name-only (no address match): NO strict observation
   - Address-only (no name match): NO strict observation
   - Multiple LEIE rows for one FEC entity collapse via DISTINCT ON
   - Idempotency: re-run produces same set
   - Cycle isolation
   - Empty LEIE -> 0 strict obs
   - Empty FEC -> 0 strict obs
   - Percentile arithmetic: pop=N, flagged=1 -> p = 1 - 1/N

The substrate goal: strict matches lift peer_percentile from ~0.945
(name-only) to >=0.99 (name+address), driving non-zero risk_score
under the tail-only fraud_risk_score formula. The percentile tests
pin the rate-based calculation against known cardinalities.
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
    address: str = "1 MAIN ST",
    city: str = "TRENTON",
    state: str = "NJ",
    zip_: str = "08608",
    excldate: str = "20180515",
    excltype: str = "1128A1",
) -> None:
    """Seed one LEIE individual row directly (bypassing the ingester)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.hhs_oig_leie ("
            " record_hash, lastname, firstname, midname, busname, "
            " excltype, excldate, "
            " address, city, state, zip, "
            " vintage_month, source_url, source_sha256"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                record_hash,
                lastname, firstname, None, None,
                excltype, excldate,
                address, city, state, zip_,
                "2026-03",
                "https://example.test/UPDATED.csv",
                "0" * 64,
            ),
        )


def _seed_fec_candidate(
    conn: psycopg.Connection,
    *,
    cycle: str,
    cand_id: str,
    cand_name: str,
    cand_office: str = "S",
    cand_office_st: str = "NJ",
    cand_city: str | None = "TRENTON",
    cand_zip: str | None = "08608",
) -> None:
    """Seed one FEC candidate row with address."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.fec_candidate ("
            " cycle, cand_id, cand_name, cand_office, cand_office_st, "
            " cand_city, cand_zip, "
            " source_url, source_sha256, source_vintage"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                cycle, cand_id, cand_name, cand_office, cand_office_st,
                cand_city, cand_zip,
                "test", "0" * 64, "test",
            ),
        )


def _seed_fec_committee(
    conn: psycopg.Connection,
    *,
    cycle: str,
    cmte_id: str,
    cmte_nm: str,
    tres_nm: str,
    cmte_st: str = "NJ",
    cmte_city: str | None = "TRENTON",
    cmte_zip: str | None = "08608",
) -> None:
    """Seed one FEC committee row with address."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.fec_committee ("
            " cycle, cmte_id, cmte_nm, tres_nm, cmte_st, "
            " cmte_city, cmte_zip, "
            " source_url, source_sha256, source_vintage"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                cycle, cmte_id, cmte_nm, tres_nm, cmte_st,
                cmte_city, cmte_zip,
                "test", "0" * 64, "test",
            ),
        )


def _scalar(conn: psycopg.Connection, q: str, *args: object) -> object:
    """Run a single-value query, return the value (or None)."""
    with conn.cursor() as cur:
        cur.execute(q, args)
        row = cur.fetchone()
        return row[0] if row else None


def _refresh(conn: psycopg.Connection, cycle: str) -> int:
    """Run the strict refresher; return rowcount."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT derived.refresh_signal_entity_on_leie_strict_address(%s)",
            (cycle,),
        )
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


def _strict_obs(conn: psycopg.Connection, cycle: str) -> list[tuple[object, ...]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT entity_kind, entity_id, raw_value, severity, "
            "       peer_bucket, peer_percentile, evidence_url "
            "FROM derived.fraud_signal_observation "
            "WHERE signal_id='entity_on_leie_strict_address' "
            "  AND cycle = %s "
            "ORDER BY entity_kind, entity_id",
            (cycle,),
        )
        return list(cur.fetchall())


# ============================================================================
# 1. Pure-SQL canonicalization helpers
# ============================================================================


def test_f_canonical_zip5_strips_non_digits_and_returns_left_5(
    fraud_db: psycopg.Connection,
) -> None:
    cases = [
        ("08608",       "08608"),
        ("086084321",   "08608"),
        ("08608-4321",  "08608"),
        ("08608 4321",  "08608"),
        ("  08608  ",   "08608"),
        # Foreign zip / not 5+ digits -> NULL
        ("123",         None),
        ("",            None),
        ("ABCDE",       None),
        # Mixed: digits + letters -> strip to 5 digits if available
        ("ABC12345",    "12345"),
    ]
    for input_zip, want in cases:
        got = _scalar(
            fraud_db,
            "SELECT derived.f_canonical_zip5(%s)",
            input_zip,
        )
        assert got == want, f"{input_zip!r}: got {got!r}, want {want!r}"


def test_f_canonical_zip5_returns_null_on_null_input(
    fraud_db: psycopg.Connection,
) -> None:
    got = _scalar(fraud_db, "SELECT derived.f_canonical_zip5(NULL::TEXT)")
    assert got is None


def test_f_canonical_city_uppercases_trims_collapses_whitespace(
    fraud_db: psycopg.Connection,
) -> None:
    cases = [
        ("Trenton",         "TRENTON"),
        ("  trenton  ",     "TRENTON"),
        ("New  York",       "NEW YORK"),
        ("NEW\tYORK",       "NEW YORK"),
        ("Jersey City",     "JERSEY CITY"),
        # Empty / whitespace-only -> NULL
        ("",                None),
        ("   ",             None),
    ]
    for input_city, want in cases:
        got = _scalar(
            fraud_db,
            "SELECT derived.f_canonical_city(%s)",
            input_city,
        )
        assert got == want, f"{input_city!r}: got {got!r}, want {want!r}"


def test_f_canonical_city_returns_null_on_null_input(
    fraud_db: psycopg.Connection,
) -> None:
    got = _scalar(fraud_db, "SELECT derived.f_canonical_city(NULL::TEXT)")
    assert got is None


# ============================================================================
# 2. View shape
# ============================================================================


def test_v_leie_individual_canonical_with_addr_columns(
    fraud_db: psycopg.Connection,
) -> None:
    """The strict-matcher view exposes the full canonical+address shape."""
    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='derived' "
            "  AND table_name='v_leie_individual_canonical_with_addr' "
            "ORDER BY ordinal_position",
        )
        cols = [r[0] for r in cur.fetchall()]
    expected = {
        "leie_record_hash", "leie_lastname", "leie_firstname",
        "leie_state", "leie_address", "leie_city_canonical",
        "leie_zip5", "leie_excldate", "leie_excltype", "canonical_key",
    }
    assert expected.issubset(set(cols)), f"missing: {expected - set(cols)}"


# ============================================================================
# 3. Refresher integration -- match cardinality
# ============================================================================


def test_strict_match_fires_on_name_plus_full_address(
    fraud_db: psycopg.Connection,
) -> None:
    """Name + city + zip5 all match -> 1 strict observation."""
    _seed_leie_individual(
        fraud_db,
        record_hash="a" * 64,
        lastname="DOE", firstname="JANE",
        city="TRENTON", state="NJ", zip_="08608",
    )
    _seed_fec_candidate(
        fraud_db,
        cycle="2024",
        cand_id="S0NJ00001",
        cand_name="DOE, JANE",
        cand_city="TRENTON",
        cand_zip="08608",
    )
    fraud_db.commit()

    n = _refresh(fraud_db, "2024")
    fraud_db.commit()
    assert n == 1, f"expected 1 strict obs; got {n}"

    obs = _strict_obs(fraud_db, "2024")
    assert len(obs) == 1
    kind, eid, raw_val, sev, bucket, pct, url = obs[0]
    assert kind == "candidate"
    assert eid == "S0NJ00001"
    assert int(raw_val) == 1
    assert sev == 5
    assert bucket == "kind=candidate"
    # pop=1 (only one candidate seeded), flagged=1 -> percentile = 1 - 1/1 = 0
    # The percentile FORMULA is correct but degenerate at n=1; sanity-check
    # it stays in [0, 1].
    assert 0 <= float(pct) <= 1
    assert "/fec/risk/entities/candidate/S0NJ00001" in url
    assert "signal=entity_on_leie_strict_address" in url
    assert "leie=" + ("a" * 64) in url


def test_strict_match_does_not_fire_on_name_only(
    fraud_db: psycopg.Connection,
) -> None:
    """Name matches but address does not -> NO strict observation.

    This is the substrate-honest tightening: name-only collisions
    (different people, same name) are filtered out by the address
    requirement. The LOOSE entity_on_leie signal still fires (we
    do not check that here -- that's pinned by test_fraud_leie_match.py).
    """
    _seed_leie_individual(
        fraud_db,
        record_hash="b" * 64,
        lastname="DOE", firstname="JANE",
        city="TRENTON", state="NJ", zip_="08608",  # LEIE: NJ 08608
    )
    _seed_fec_candidate(
        fraud_db,
        cycle="2024",
        cand_id="S0CA00001",
        cand_name="DOE, JANE",
        cand_city="LOS ANGELES", cand_zip="90001",  # FEC: CA 90001
    )
    fraud_db.commit()

    n = _refresh(fraud_db, "2024")
    fraud_db.commit()
    assert n == 0, f"expected 0 strict obs (name-only collision); got {n}"

    assert _strict_obs(fraud_db, "2024") == []


def test_strict_match_does_not_fire_on_address_only(
    fraud_db: psycopg.Connection,
) -> None:
    """Address matches but name does not -> NO strict observation."""
    _seed_leie_individual(
        fraud_db,
        record_hash="c" * 64,
        lastname="DOE", firstname="JANE",
        city="TRENTON", state="NJ", zip_="08608",
    )
    _seed_fec_candidate(
        fraud_db,
        cycle="2024",
        cand_id="S0NJ00002",
        cand_name="SMITH, ROBERT",  # different name
        cand_city="TRENTON",
        cand_zip="08608",
    )
    fraud_db.commit()

    n = _refresh(fraud_db, "2024")
    fraud_db.commit()
    assert n == 0, f"expected 0 strict obs (address-only); got {n}"


def test_strict_match_zip_plus_four_canonicalizes_to_zip5(
    fraud_db: psycopg.Connection,
) -> None:
    """LEIE has zip5 '08608'; FEC has zip+4 '08608-4321' -> still matches.

    Substrate-honest call: zip+4 noise should not be the difference
    between "matches" and "doesn't match" -- the platform's loose
    grain is 5-digit ZIP (consistent with mig 087's address-cluster
    fix). The f_canonical_zip5 helper enforces this on both sides
    of the join.
    """
    _seed_leie_individual(
        fraud_db,
        record_hash="d" * 64,
        lastname="ROE", firstname="JOHN",
        city="NEWARK", state="NJ", zip_="07102",
    )
    _seed_fec_candidate(
        fraud_db,
        cycle="2024",
        cand_id="S0NJ00003",
        cand_name="ROE, JOHN",
        cand_city="NEWARK",
        cand_zip="07102-1234",  # zip+4 form
    )
    fraud_db.commit()

    assert _refresh(fraud_db, "2024") == 1
    fraud_db.commit()


def test_strict_match_one_obs_per_fec_entity_when_multiple_leie_match(
    fraud_db: psycopg.Connection,
) -> None:
    """DISTINCT ON: one observation per (entity_kind, entity_id).

    If two LEIE individuals at the same address share canonical name
    (e.g. a re-listed entry + a new entry), the FEC entity matches
    BOTH but should emit exactly ONE strict observation. The DISTINCT
    ON in the refresher picks the LEIE row with the most recent
    excldate (freshest evidence).
    """
    _seed_leie_individual(
        fraud_db,
        record_hash="e" * 64,
        lastname="DOE", firstname="JANE",
        city="TRENTON", state="NJ", zip_="08608",
        excldate="20100101",  # older
    )
    _seed_leie_individual(
        fraud_db,
        record_hash="f" * 64,
        lastname="DOE", firstname="JANE",
        city="TRENTON", state="NJ", zip_="08608",
        excldate="20200101",  # newer -- should win DISTINCT ON
    )
    _seed_fec_candidate(
        fraud_db,
        cycle="2024",
        cand_id="S0NJ00004",
        cand_name="DOE, JANE",
        cand_city="TRENTON",
        cand_zip="08608",
    )
    fraud_db.commit()

    n = _refresh(fraud_db, "2024")
    fraud_db.commit()
    assert n == 1

    obs = _strict_obs(fraud_db, "2024")
    assert len(obs) == 1
    # Evidence URL should cite the FRESHEST LEIE hash (F-hash, 2020 excldate)
    _, _, _, _, _, _, url = obs[0]
    assert "leie=" + ("f" * 64) in url, (
        f"DISTINCT ON should pick freshest excldate; got url={url!r}"
    )


def test_strict_match_treasurer_via_committee_address(
    fraud_db: psycopg.Connection,
) -> None:
    """A treasurer strict-matches when their committee address overlaps LEIE."""
    _seed_leie_individual(
        fraud_db,
        record_hash="1" * 64,
        lastname="TAYLOR", firstname="JAMES",
        address="3 ROBERTS TRACE",
        city="HAMPTON", state="VA", zip_="23666",
    )
    _seed_fec_committee(
        fraud_db,
        cycle="2024",
        cmte_id="C99999991",
        cmte_nm="JAMES TAYLOR FOR CONGRESS",
        tres_nm="TAYLOR, JAMES L",
        cmte_st="VA",
        cmte_city="HAMPTON",
        cmte_zip="23666",
    )
    fraud_db.commit()

    n = _refresh(fraud_db, "2024")
    fraud_db.commit()
    assert n == 1

    obs = _strict_obs(fraud_db, "2024")
    assert len(obs) == 1
    kind, eid, _, sev, bucket, _, _ = obs[0]
    assert kind == "treasurer"
    # entity_id is the canonical treasurer string (UPPER + space-collapsed)
    assert eid == "TAYLOR, JAMES L"
    assert sev == 5
    assert bucket == "kind=treasurer"


# ============================================================================
# 4. Refresher idempotency + cycle isolation
# ============================================================================


def test_strict_refresher_is_idempotent_on_same_cycle(
    fraud_db: psycopg.Connection,
) -> None:
    """Running twice for the same cycle yields the same set of rows."""
    _seed_leie_individual(
        fraud_db, record_hash="a" * 64,
        lastname="DOE", firstname="JANE",
        city="TRENTON", state="NJ", zip_="08608",
    )
    _seed_fec_candidate(
        fraud_db, cycle="2024", cand_id="S0NJ00001",
        cand_name="DOE, JANE",
        cand_city="TRENTON", cand_zip="08608",
    )
    fraud_db.commit()

    n1 = _refresh(fraud_db, "2024")
    fraud_db.commit()
    n2 = _refresh(fraud_db, "2024")
    fraud_db.commit()
    assert n1 == n2 == 1

    obs = _strict_obs(fraud_db, "2024")
    assert len(obs) == 1


def test_strict_refresher_cycle_isolation(
    fraud_db: psycopg.Connection,
) -> None:
    """Refreshing 2024 does NOT touch 2022 strict observations."""
    _seed_leie_individual(
        fraud_db, record_hash="a" * 64,
        lastname="DOE", firstname="JANE",
        city="TRENTON", state="NJ", zip_="08608",
    )
    _seed_fec_candidate(
        fraud_db, cycle="2022", cand_id="S0NJ22001",
        cand_name="DOE, JANE",
        cand_city="TRENTON", cand_zip="08608",
    )
    _seed_fec_candidate(
        fraud_db, cycle="2024", cand_id="S0NJ24001",
        cand_name="DOE, JANE",
        cand_city="TRENTON", cand_zip="08608",
    )
    fraud_db.commit()

    _refresh(fraud_db, "2022")
    fraud_db.commit()
    _refresh(fraud_db, "2024")
    fraud_db.commit()
    # Re-running 2024 must not delete the 2022 observation
    _refresh(fraud_db, "2024")
    fraud_db.commit()

    obs_2022 = _strict_obs(fraud_db, "2022")
    obs_2024 = _strict_obs(fraud_db, "2024")
    assert len(obs_2022) == 1
    assert len(obs_2024) == 1
    assert obs_2022[0][1] == "S0NJ22001"
    assert obs_2024[0][1] == "S0NJ24001"


# ============================================================================
# 5. Empty cases
# ============================================================================


def test_strict_refresher_empty_leie_returns_zero(
    fraud_db: psycopg.Connection,
) -> None:
    _seed_fec_candidate(
        fraud_db, cycle="2024", cand_id="S0NJ00001",
        cand_name="DOE, JANE",
        cand_city="TRENTON", cand_zip="08608",
    )
    fraud_db.commit()
    assert _refresh(fraud_db, "2024") == 0
    fraud_db.commit()


def test_strict_refresher_empty_fec_returns_zero(
    fraud_db: psycopg.Connection,
) -> None:
    _seed_leie_individual(
        fraud_db, record_hash="a" * 64,
        lastname="DOE", firstname="JANE",
        city="TRENTON", state="NJ", zip_="08608",
    )
    fraud_db.commit()
    assert _refresh(fraud_db, "2024") == 0
    fraud_db.commit()


def test_strict_refresher_both_empty_returns_zero(
    fraud_db: psycopg.Connection,
) -> None:
    assert _refresh(fraud_db, "2024") == 0
    fraud_db.commit()


# ============================================================================
# 6. Percentile arithmetic (rate-based binary)
# ============================================================================


def test_strict_percentile_arithmetic_one_in_n_population(
    fraud_db: psycopg.Connection,
) -> None:
    """With pop=N candidates and 1 strict match, percentile = 1 - 1/N.

    Seed 3 candidates with full address; only 1 matches LEIE strictly.
    The other 2 are addressed but their names do not match LEIE.
    Expected: pop=3, flagged=1, percentile = 1 - 1/3 = 0.6667.
    """
    _seed_leie_individual(
        fraud_db, record_hash="a" * 64,
        lastname="DOE", firstname="JANE",
        city="TRENTON", state="NJ", zip_="08608",
    )
    _seed_fec_candidate(
        fraud_db, cycle="2024", cand_id="S0NJ00001",
        cand_name="DOE, JANE",
        cand_city="TRENTON", cand_zip="08608",  # MATCHES
    )
    _seed_fec_candidate(
        fraud_db, cycle="2024", cand_id="S0NJ00002",
        cand_name="SMITH, ROBERT",
        cand_city="TRENTON", cand_zip="08608",  # name no match
    )
    _seed_fec_candidate(
        fraud_db, cycle="2024", cand_id="S0NJ00003",
        cand_name="JONES, MARY",
        cand_city="NEWARK", cand_zip="07102",  # name + addr no match
    )
    fraud_db.commit()

    _refresh(fraud_db, "2024")
    fraud_db.commit()

    obs = _strict_obs(fraud_db, "2024")
    assert len(obs) == 1
    _, _, _, _, _, pct, _ = obs[0]
    # pop=3 candidates with name + city + zip; 1 strict match.
    # percentile = 1 - 1/3 = 0.6666...
    assert abs(float(pct) - (1.0 - 1.0 / 3.0)) < 1e-6, (
        f"expected ~0.6667, got {pct}"
    )


# ============================================================================
# 7. Layering with loose entity_on_leie
# ============================================================================


def test_strict_and_loose_can_fire_on_same_entity(
    fraud_db: psycopg.Connection,
) -> None:
    """Both signals fire on the same entity when full evidence is present.

    Substrate-honest layering: the analyst sees TWO evidence cards
    (loose at p ~ 1-1/N, strict at p ~ 1-1/N from the strict pool),
    not one or the other.
    """
    _seed_leie_individual(
        fraud_db, record_hash="a" * 64,
        lastname="DOE", firstname="JANE",
        city="TRENTON", state="NJ", zip_="08608",
    )
    _seed_fec_candidate(
        fraud_db, cycle="2024", cand_id="S0NJ00001",
        cand_name="DOE, JANE",
        cand_city="TRENTON", cand_zip="08608",
    )
    fraud_db.commit()

    # Fire BOTH refreshers; the loose one is mig 054, the strict is mig 092.
    with fraud_db.cursor() as cur:
        cur.execute("SELECT derived.refresh_signal_entity_on_leie('2024')")
        cur.execute(
            "SELECT derived.refresh_signal_entity_on_leie_strict_address('2024')",
        )
    fraud_db.commit()

    with fraud_db.cursor() as cur:
        cur.execute(
            "SELECT signal_id, COUNT(*) "
            "FROM derived.fraud_signal_observation "
            "WHERE cycle='2024' AND entity_kind='candidate' "
            "  AND entity_id='S0NJ00001' "
            "GROUP BY signal_id ORDER BY signal_id",
        )
        rows = dict(cur.fetchall())
    assert rows.get("entity_on_leie") == 1, (
        "loose signal should fire on this entity"
    )
    assert rows.get("entity_on_leie_strict_address") == 1, (
        "strict signal should fire on this entity"
    )
