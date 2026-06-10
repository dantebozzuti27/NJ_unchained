"""Live-PG regression tests for migration 117: derived.v_signal_validation.

VISION_2026 Pillar 2 (civic integrity) FRAUD-F8 precision layer.

The view under test answers, per (cycle, behavioral signal):

    precision = P(provider on an exclusion list | flagged by the detector)
    base_rate = sanctioned-provider rate in the whole CMS billing universe
    lift      = precision / base_rate

Ground truth (positives) = NPIs flagged by any prior-sanction signal
(ref.fraud_reportability_channel.is_prior_sanction = TRUE). The universe = every
billing NPI in raw.cms_partd_prescriber UNION raw.cms_physician_provider for the
data_year.

What this module pins:
    * formula_version registered.
    * Empty substrate -> no rows (no crash, no fabricated stats).
    * A behavioral detector that perfectly concentrates the positive set scores
      precision = 1.0 and lift = 1/base_rate; the counts are exact.
    * A detector that flags only NON-sanctioned providers scores precision 0.
    * lift is NULL for a cycle with zero ground truth (explicit gap).
    * Prior-sanction signals are NOT themselves validated (excluded from output).
    * The Wilson lower bound is <= the point precision and within [0,1].
    * base_rate = n_positives / n_universe exactly.
"""

from __future__ import annotations

from decimal import Decimal
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

EXPECTED_FORMULA_VERSION = "3.5.0-fraud-signal-validation-harness-v1"
_CYCLE = "2024"
_DATA_YEAR = 2024

# A real behavioral (non-prior-sanction) provider signal and a real
# prior-sanction provider signal that both ship with the platform.
_BEHAVIORAL = "opioid_prescribing_outlier"
_PRIOR_SANCTION = "provider_excluded_billing"


@pytest.fixture
def vdb(live_pg: psycopg.Connection) -> psycopg.Connection:
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


def _scalar(conn: psycopg.Connection, q: str, *args: object) -> object:
    with conn.cursor() as cur:
        cur.execute(q, args)
        row = cur.fetchone()
        return row[0] if row else None


def _seed_universe_npis(
    conn: psycopg.Connection, npis: list[str], *, data_year: int = _DATA_YEAR
) -> None:
    """Insert minimal Part D rows so each NPI counts in the billing universe."""
    with conn.cursor() as cur:
        for npi in npis:
            cur.execute(
                "INSERT INTO raw.cms_partd_prescriber ("
                " data_year, npi, prscrbr_type, prscrbr_state_abrvtn, "
                " tot_clms, tot_drug_cst, tot_benes, "
                " source_url, source_sha256, source_vintage"
                ") VALUES (%s, %s, 'Internal Medicine', 'NJ', 100, 5000, 50, "
                "          'https://example.test/partd.csv', %s, 'CY2024') "
                "ON CONFLICT (data_year, npi) DO NOTHING",
                (data_year, npi, "0" * 64),
            )


def _emit_obs(
    conn: psycopg.Connection,
    *,
    npi: str,
    signal_id: str,
    cycle: str = _CYCLE,
    severity: int = 3,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO derived.fraud_signal_observation "
            "(cycle, entity_kind, entity_id, signal_id, raw_value, severity, "
            " peer_bucket, peer_percentile, evidence_url) "
            "VALUES (%s, 'provider', %s, %s, 1, %s, 'b', 0.99, '/x') "
            "ON CONFLICT (cycle, entity_kind, entity_id, signal_id) "
            "DO NOTHING",
            (cycle, npi, signal_id, severity),
        )


def _row(conn: psycopg.Connection, signal_id: str, cycle: str = _CYCLE):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT n_universe, n_positives, n_flagged, n_true_positive, "
            "       base_rate, precision, lift, precision_wilson_lo95 "
            "FROM derived.v_signal_validation "
            "WHERE cycle = %s AND signal_id = %s",
            (cycle, signal_id),
        )
        return cur.fetchone()


# ----------------------------------------------------------------------------
# Provenance
# ----------------------------------------------------------------------------


def test_formula_version_registered(vdb: psycopg.Connection) -> None:
    desc = _scalar(
        vdb,
        "SELECT description FROM ref.formula_version WHERE formula_version = %s",
        EXPECTED_FORMULA_VERSION,
    )
    assert isinstance(desc, str)
    assert "v_signal_validation" in desc


# ----------------------------------------------------------------------------
# Behavior
# ----------------------------------------------------------------------------


def test_empty_substrate_yields_no_rows(vdb: psycopg.Connection) -> None:
    n = _scalar(vdb, "SELECT COUNT(*) FROM derived.v_signal_validation")
    assert n == 0


def test_perfect_detector_precision_and_lift(vdb: psycopg.Connection) -> None:
    """100-provider universe; 10 are sanctioned (base rate 0.10). The behavioral
    detector flags exactly those 10 sanctioned providers -> precision 1.0,
    lift = 1/0.10 = 10."""
    universe = [str(2000000000 + i) for i in range(100)]
    _seed_universe_npis(vdb, universe)
    sanctioned = universe[:10]
    for npi in sanctioned:
        _emit_obs(vdb, npi=npi, signal_id=_PRIOR_SANCTION, severity=5)
        _emit_obs(vdb, npi=npi, signal_id=_BEHAVIORAL)
    vdb.commit()

    row = _row(vdb, _BEHAVIORAL)
    assert row is not None
    n_uni, n_pos, n_flag, n_tp, base, prec, lift, wlo = row
    assert n_uni == 100
    assert n_pos == 10
    assert n_flag == 10
    assert n_tp == 10
    assert float(base) == pytest.approx(0.10)
    assert float(prec) == pytest.approx(1.0)
    assert float(lift) == pytest.approx(10.0)
    # Wilson lower bound is conservative: <= point precision, within [0,1].
    assert 0.0 <= float(wlo) <= 1.0
    assert float(wlo) <= float(prec)


def test_useless_detector_scores_zero_precision(vdb: psycopg.Connection) -> None:
    """A detector that flags only NON-sanctioned providers has precision 0 and
    lift 0 -- the harness correctly refuses to credit it."""
    universe = [str(2100000000 + i) for i in range(100)]
    _seed_universe_npis(vdb, universe)
    # 10 sanctioned providers, but the behavioral detector flags 10 OTHERS.
    for npi in universe[:10]:
        _emit_obs(vdb, npi=npi, signal_id=_PRIOR_SANCTION, severity=5)
    for npi in universe[50:60]:
        _emit_obs(vdb, npi=npi, signal_id=_BEHAVIORAL)
    vdb.commit()

    row = _row(vdb, _BEHAVIORAL)
    assert row is not None
    _, n_pos, n_flag, n_tp, base, prec, lift, _ = row
    assert n_pos == 10
    assert n_flag == 10
    assert n_tp == 0
    assert float(prec) == pytest.approx(0.0)
    assert float(lift) == pytest.approx(0.0)


def test_no_ground_truth_yields_null_lift(vdb: psycopg.Connection) -> None:
    """With zero sanctioned providers in the cycle, base_rate is 0 and lift is
    NULL -- an explicit 'cannot validate', never a fabricated number."""
    universe = [str(2200000000 + i) for i in range(50)]
    _seed_universe_npis(vdb, universe)
    for npi in universe[:5]:
        _emit_obs(vdb, npi=npi, signal_id=_BEHAVIORAL)
    vdb.commit()

    row = _row(vdb, _BEHAVIORAL)
    assert row is not None
    _, n_pos, n_flag, n_tp, base, prec, lift, _ = row
    assert n_pos == 0
    assert n_flag == 5
    assert n_tp == 0
    assert float(base) == pytest.approx(0.0)
    assert lift is None, "lift must be NULL when there is no ground truth"


def test_prior_sanction_signal_not_self_validated(
    vdb: psycopg.Connection,
) -> None:
    """A prior-sanction signal is the ground truth; it must NOT appear as a
    validated behavioral detector (that would be a circular precision of 1)."""
    universe = [str(2300000000 + i) for i in range(20)]
    _seed_universe_npis(vdb, universe)
    for npi in universe[:5]:
        _emit_obs(vdb, npi=npi, signal_id=_PRIOR_SANCTION, severity=5)
    vdb.commit()

    row = _row(vdb, _PRIOR_SANCTION)
    assert row is None, "prior-sanction signals are excluded from the harness"


def test_base_rate_is_exact_ratio(vdb: psycopg.Connection) -> None:
    """base_rate = n_positives / n_universe, computed over the union of Part B
    and Part D NPIs (deduped)."""
    universe = [str(2400000000 + i) for i in range(40)]
    _seed_universe_npis(vdb, universe)
    for npi in universe[:8]:
        _emit_obs(vdb, npi=npi, signal_id=_PRIOR_SANCTION, severity=5)
    for npi in universe[:4]:
        _emit_obs(vdb, npi=npi, signal_id=_BEHAVIORAL)
    vdb.commit()

    row = _row(vdb, _BEHAVIORAL)
    assert row is not None
    n_uni, n_pos, _, _, base, _, _, _ = row
    assert n_uni == 40
    assert n_pos == 8
    assert float(base) == pytest.approx(8 / 40)
