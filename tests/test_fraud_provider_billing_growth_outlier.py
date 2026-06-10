"""Live-PG regression tests for migration 118 + seed 052.

VISION_2026 Pillar 2 (civic integrity) FRAUD-F8 prospective TEMPORAL detector.

The signal under test -- provider_billing_growth_outlier -- flags a CMS Part B
practitioner in the extreme upper tail (top 1%) of its OWN specialty peer group
on year-over-year Medicare-paid growth (Tot_Mdcr_Pymt_Amt for the cycle year
divided by the prior year), gated by a material current-year payment floor, a
prior-year denominator floor, and a minimum specialty-peer count (all constants
from ref.platform_constants). It is the "bust-out" / NPI-takeover signature and
fires on providers with NO enforcement action on record.

What this module pins:
    * Tuning constants seeded into ref.platform_constants.
    * Signal config uses the NEW cms_temporal family.
    * Refresher derived.refresh_signal_provider_billing_growth_outlier:
        - empty substrate -> 0, no error
        - prior year absent -> 0 (the ratio needs both years)
        - within a >=50-peer specialty, the extreme grower is flagged; a
          median grower is not
        - emitted fields: entity_kind=provider, severity 4, bucket
          'specialty=<type>', raw_value = the growth ratio, percentile =
          within-specialty CUME_DIST
        - CURRENT-PAYMENT FLOOR and PRIOR-YEAR DENOMINATOR FLOOR guards
        - placeholder NPI never flags
        - missing-constant guard RAISES
        - idempotency + cycle isolation
    * Master refresher invokes the new refresher.
    * Reference data: severity 4 / empirical_pctile; platform authority;
      CMS.gov verify URL; tier-3 no-bounty channel; formula_version registered.
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

EXPECTED_FORMULA_VERSION = "3.6.0-fraud-provider-billing-growth-outlier-v1"
_SIGNAL = "provider_billing_growth_outlier"
_DATA_YEAR = 2024
_PREV_YEAR = 2023
_CYCLE = str(_DATA_YEAR)


@pytest.fixture
def gr_db(live_pg: psycopg.Connection) -> psycopg.Connection:
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


def _npi_for(rank: int) -> str:
    return str(1000000000 + rank)


def _seed_pb(
    conn: psycopg.Connection,
    *,
    npi: str,
    data_year: int,
    pymt: float,
    specialty: str = "Cardiology",
    state: str = "NJ",
) -> None:
    """Insert one Part B row with a given Medicare-paid amount."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.cms_physician_provider ("
            " data_year, npi, prvdr_last_org_name, prvdr_first_name, "
            " prvdr_city, prvdr_state_abrvtn, prvdr_type, "
            " tot_benes, tot_srvcs, tot_mdcr_alowd_amt, tot_mdcr_pymt_amt, "
            " tot_sbmtd_chrg, bene_avg_risk_scre, "
            " source_url, source_sha256, source_vintage"
            ") VALUES (%s, %s, 'DOE', 'JANE', 'NEWARK', %s, %s, "
            "          100, 100, %s, %s, %s, 1.0, "
            "          'https://example.test/partb.csv', %s, 'CY') "
            "ON CONFLICT (data_year, npi) DO UPDATE SET "
            "  tot_mdcr_pymt_amt = EXCLUDED.tot_mdcr_pymt_amt",
            (data_year, npi, state, specialty, pymt, pymt, pymt, "0" * 64),
        )


def _seed_growth_bucket(
    conn: psycopg.Connection,
    *,
    specialty: str,
    n: int,
    prev_pymt: float = 10000.0,
) -> None:
    """Seed *n* practitioners present in BOTH years. Peer #k has a constant
    prior-year payment (prev_pymt) and a current-year payment of 60000*k, so
    growth = 60000*k / prev_pymt is distinct and strictly increasing in k. All
    clear the current-year floor (60000 >= 50000) and the prior-year floor
    (prev_pymt >= 1000). NPI of peer #k is _npi_for(k)."""
    with conn.cursor() as cur:
        # Prior year (constant base).
        cur.execute(
            "INSERT INTO raw.cms_physician_provider ("
            " data_year, npi, prvdr_state_abrvtn, prvdr_type, "
            " tot_benes, tot_srvcs, tot_mdcr_alowd_amt, tot_mdcr_pymt_amt, "
            " tot_sbmtd_chrg, source_url, source_sha256, source_vintage"
            ") SELECT %s, (1000000000 + gs)::text, 'NJ', %s, "
            "          100, 100, %s, %s, %s, "
            "          'https://example.test/partb.csv', %s, 'CY2023' "
            "   FROM generate_series(1, %s) AS gs",
            (_PREV_YEAR, specialty, prev_pymt, prev_pymt, prev_pymt,
             "0" * 64, n),
        )
        # Current year (ramped: 60000 * k).
        cur.execute(
            "INSERT INTO raw.cms_physician_provider ("
            " data_year, npi, prvdr_state_abrvtn, prvdr_type, "
            " tot_benes, tot_srvcs, tot_mdcr_alowd_amt, tot_mdcr_pymt_amt, "
            " tot_sbmtd_chrg, source_url, source_sha256, source_vintage"
            ") SELECT %s, (1000000000 + gs)::text, 'NJ', %s, "
            "          100, 100, (60000 * gs), (60000 * gs), (60000 * gs), "
            "          'https://example.test/partb.csv', %s, 'CY2024' "
            "   FROM generate_series(1, %s) AS gs",
            (_DATA_YEAR, specialty, "0" * 64, n),
        )


def _refresh(conn: psycopg.Connection, cycle: str = _CYCLE) -> int:
    v = _scalar(
        conn,
        "SELECT derived.refresh_signal_provider_billing_growth_outlier(%s)",
        cycle,
    )
    assert isinstance(v, int)
    return v


def _flagged_npis(conn: psycopg.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT entity_id FROM derived.fraud_signal_observation "
            "WHERE signal_id = %s",
            (_SIGNAL,),
        )
        return {r[0] for r in cur.fetchall()}


# ----------------------------------------------------------------------------
# Constants + config
# ----------------------------------------------------------------------------


def test_platform_constants_seeded(gr_db: psycopg.Connection) -> None:
    for cid, expected in (
        ("billing_growth_tail_pctile", 0.99),
        ("billing_growth_min_curr_pymt", 50000.0),
        ("billing_growth_min_prev_pymt", 1000.0),
        ("billing_growth_min_bucket", 50.0),
    ):
        val = _scalar(gr_db, "SELECT derived.f_platform_constant(%s)", cid)
        assert isinstance(val, Decimal), f"constant {cid} not seeded"
        assert float(val) == pytest.approx(expected)


def test_signal_config_uses_cms_temporal_family(
    gr_db: psycopg.Connection,
) -> None:
    with gr_db.cursor() as cur:
        cur.execute(
            "SELECT signal_family, min_actionable_threshold "
            "FROM derived.fraud_signal_config WHERE signal_id = %s",
            (_SIGNAL,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "cms_temporal"
    assert float(row[1]) == 0.0


# ----------------------------------------------------------------------------
# Refresher behavior
# ----------------------------------------------------------------------------


def test_refresher_zero_on_empty_substrate(gr_db: psycopg.Connection) -> None:
    assert _refresh(gr_db) == 0


def test_zero_when_prior_year_missing(gr_db: psycopg.Connection) -> None:
    """With only the current year loaded (no prior year), the ratio is
    undefined for everyone -> nothing fires."""
    with gr_db.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.cms_physician_provider ("
            " data_year, npi, prvdr_state_abrvtn, prvdr_type, "
            " tot_benes, tot_srvcs, tot_mdcr_alowd_amt, tot_mdcr_pymt_amt, "
            " tot_sbmtd_chrg, source_url, source_sha256, source_vintage"
            ") SELECT %s, (1000000000 + gs)::text, 'NJ', 'Cardiology', "
            "          100, 100, 100000, 100000, 100000, "
            "          'https://example.test/partb.csv', %s, 'CY2024' "
            "   FROM generate_series(1, 100) AS gs",
            (_DATA_YEAR, "0" * 64),
        )
    gr_db.commit()
    assert _refresh(gr_db) == 0


def test_extreme_grower_flagged_median_not(gr_db: psycopg.Connection) -> None:
    """In a 60-peer specialty (growth 6x..360x), the top grower is flagged; a
    median grower is not. Emitted fields are checked."""
    _seed_growth_bucket(gr_db, specialty="Cardiology", n=60)
    gr_db.commit()
    assert _refresh(gr_db) >= 1

    flagged = _flagged_npis(gr_db)
    assert _npi_for(60) in flagged, "the fastest grower must be flagged"
    assert _npi_for(30) not in flagged, "a median grower must NOT be flagged"

    with gr_db.cursor() as cur:
        cur.execute(
            "SELECT entity_kind, severity, peer_bucket, raw_value, "
            "       peer_percentile "
            "FROM derived.fraud_signal_observation "
            "WHERE signal_id = %s AND entity_id = %s",
            (_SIGNAL, _npi_for(60)),
        )
        row = cur.fetchone()
    assert row is not None
    kind, sev, bucket, raw_value, pctile = row
    assert kind == "provider"
    assert sev == 4
    assert bucket == "specialty=Cardiology"
    # growth = 60000*60 / 10000 = 360.0
    assert float(raw_value) == pytest.approx(360.0)
    assert float(pctile) == pytest.approx(1.0)


def test_current_payment_floor_excludes(gr_db: psycopg.Connection) -> None:
    """A provider with a huge growth multiple but a sub-floor current-year
    payment ($40k < $50k) is ineligible despite the extreme ratio."""
    _seed_growth_bucket(gr_db, specialty="Cardiology", n=60)
    # growth 40x but curr only $40,000 (below the $50k material floor).
    _seed_pb(gr_db, npi="1999999999", data_year=_PREV_YEAR, pymt=1000)
    _seed_pb(gr_db, npi="1999999999", data_year=_DATA_YEAR, pymt=40000)
    gr_db.commit()
    _refresh(gr_db)
    assert "1999999999" not in _flagged_npis(gr_db)


def test_prev_payment_floor_excludes(gr_db: psycopg.Connection) -> None:
    """A provider with an explosive ratio off a sub-floor prior-year base
    ($500 < $1,000) is ineligible (divide-by-tiny guard)."""
    _seed_growth_bucket(gr_db, specialty="Cardiology", n=60)
    # curr $200k (material) but prev only $500 -> growth 400x, excluded by floor.
    _seed_pb(gr_db, npi="1999999998", data_year=_PREV_YEAR, pymt=500)
    _seed_pb(gr_db, npi="1999999998", data_year=_DATA_YEAR, pymt=200000)
    gr_db.commit()
    _refresh(gr_db)
    assert "1999999998" not in _flagged_npis(gr_db)


def test_bucket_below_min_peers_yields_nothing(
    gr_db: psycopg.Connection,
) -> None:
    """A specialty with only 49 two-year peers (< min_bucket=50) yields
    nothing."""
    _seed_growth_bucket(gr_db, specialty="Rare Specialty", n=49)
    gr_db.commit()
    assert _refresh(gr_db) == 0


def test_placeholder_npi_never_flagged(gr_db: psycopg.Connection) -> None:
    _seed_growth_bucket(gr_db, specialty="Cardiology", n=60)
    _seed_pb(gr_db, npi="0000000000", data_year=_PREV_YEAR, pymt=10000)
    _seed_pb(gr_db, npi="0000000000", data_year=_DATA_YEAR, pymt=9000000)
    gr_db.commit()
    _refresh(gr_db)
    assert "0000000000" not in _flagged_npis(gr_db)


def test_missing_constant_raises(gr_db: psycopg.Connection) -> None:
    import psycopg.errors
    with gr_db.cursor() as cur:
        cur.execute(
            "DELETE FROM ref.platform_constants "
            "WHERE constant_id = 'billing_growth_tail_pctile'"
        )
    gr_db.commit()
    with pytest.raises(psycopg.errors.NoDataFound):
        _refresh(gr_db)
    gr_db.rollback()


def test_refresher_is_idempotent(gr_db: psycopg.Connection) -> None:
    _seed_growth_bucket(gr_db, specialty="Cardiology", n=60)
    gr_db.commit()
    n1 = _refresh(gr_db)
    n2 = _refresh(gr_db)
    assert n1 == n2
    total = _scalar(
        gr_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE signal_id = %s",
        _SIGNAL,
    )
    assert total == n1


def test_refresher_isolates_other_cycles(gr_db: psycopg.Connection) -> None:
    _seed_growth_bucket(gr_db, specialty="Cardiology", n=60)
    with gr_db.cursor() as cur:
        cur.execute(
            "INSERT INTO derived.fraud_signal_observation "
            "(cycle, entity_kind, entity_id, signal_id, raw_value, severity, "
            " peer_bucket, peer_percentile, evidence_url) "
            "VALUES ('2099', 'provider', 'PROBE', %s, 1, 4, 'specialty=X', "
            "0.99, '/probe-2099')",
            (_SIGNAL,),
        )
    gr_db.commit()
    _refresh(gr_db, "2024")
    n = _scalar(
        gr_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle = '2099' AND signal_id = %s",
        _SIGNAL,
    )
    assert n == 1


def test_master_refresher_includes_signal(gr_db: psycopg.Connection) -> None:
    _seed_growth_bucket(gr_db, specialty="Cardiology", n=60)
    gr_db.commit()
    _scalar(
        gr_db,
        "SELECT derived.refresh_all_fraud_signal_observations(%s)",
        "2024",
    )
    n = _scalar(
        gr_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle = '2024' AND signal_id = %s",
        _SIGNAL,
    )
    assert isinstance(n, int) and n >= 1


# ----------------------------------------------------------------------------
# Reference data + provenance
# ----------------------------------------------------------------------------


def test_severity_calibration_row(gr_db: psycopg.Connection) -> None:
    with gr_db.cursor() as cur:
        cur.execute(
            "SELECT severity_level, calibration_basis "
            "FROM ref.fraud_signal_severity_calibration WHERE signal_id = %s",
            (_SIGNAL,),
        )
        row = cur.fetchone()
    assert row == (4, "empirical_pctile")


def test_reportability_channel_no_bounty_tier3(
    gr_db: psycopg.Connection,
) -> None:
    with gr_db.cursor() as cur:
        cur.execute(
            "SELECT reward_eligible, reward_tier, raw_value_is_usd, "
            "       is_prior_sanction "
            "FROM ref.fraud_reportability_channel WHERE signal_id = %s",
            (_SIGNAL,),
        )
        row = cur.fetchone()
    assert row is not None
    eligible, tier, is_usd, prior = row
    assert eligible is False
    assert tier == 3
    assert is_usd is False
    assert prior is False


def test_evidence_url_template_row(gr_db: psycopg.Connection) -> None:
    with gr_db.cursor() as cur:
        cur.execute(
            "SELECT url_template, upstream_source "
            "FROM ref.fraud_signal_evidence_url_template WHERE signal_id = %s",
            (_SIGNAL,),
        )
        row = cur.fetchone()
    assert row is not None
    url, source = row
    assert url.startswith("https://data.cms.gov")
    assert source == "CMS.gov"


def test_formula_version_registered(gr_db: psycopg.Connection) -> None:
    with gr_db.cursor() as cur:
        cur.execute(
            "SELECT effective_date, description "
            "FROM ref.formula_version WHERE formula_version = %s",
            (EXPECTED_FORMULA_VERSION,),
        )
        row = cur.fetchone()
    assert row is not None
    eff_date, desc = row
    assert eff_date.isoformat() == "2026-06-10"
    assert "provider_billing_growth_outlier" in desc
