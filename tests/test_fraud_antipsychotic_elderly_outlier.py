"""Live-PG regression tests for migration 115 + seed 051.

VISION_2026 Pillar 2 (civic integrity) FRAUD-F8 prospective detector.

The signal under test -- antipsychotic_elderly_outlier -- flags a CMS Part D
prescriber in the extreme upper tail (top 1%) of its OWN specialty peer group
on the share of elderly (>=65) beneficiaries receiving antipsychotics
(antpsyct_ge65_tot_benes / ge65_tot_benes), gated by an elderly-population floor
and a minimum specialty-peer count (all constants from ref.platform_constants).
It is a "chemical restraint" / medically-unnecessary-prescribing lead that fires
on providers with NO enforcement action on record.

What this module pins:
    * Tuning constants seeded into ref.platform_constants.
    * Refresher derived.refresh_signal_antipsychotic_elderly_outlier:
        - empty substrate -> 0, no error
        - within a >=50-peer specialty, the extreme-rate prescriber is flagged;
          a median-rate prescriber is not
        - emitted fields: entity_kind=provider, severity 4, bucket
          'specialty=<type>', raw_value = the elderly-antipsychotic rate (%),
          percentile = within-specialty CUME_DIST
        - BUCKET-SIZE GUARD and ELDERLY-DENOMINATOR-FLOOR GUARD
        - suppressed (NULL numerator/denominator) + placeholder NPI never flag
        - missing-constant guard RAISES
        - idempotency + cycle isolation
    * Master refresher invokes the new refresher.
    * Reference data: severity 4 / empirical_pctile; platform authority;
      CMS.gov verify URL; cms_utilization family. formula_version registered.
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

EXPECTED_FORMULA_VERSION = "3.3.0-fraud-antipsychotic-elderly-outlier-v1"
_SIGNAL = "antipsychotic_elderly_outlier"
_DATA_YEAR = 2024
_CYCLE = str(_DATA_YEAR)


@pytest.fixture
def ap_db(live_pg: psycopg.Connection) -> psycopg.Connection:
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


def _seed_specialty_bucket(
    conn: psycopg.Connection,
    *,
    specialty: str,
    n: int,
    data_year: int = _DATA_YEAR,
    ge65_benes: int = 100,
) -> None:
    """Seed *n* prescribers in *specialty* with distinct elderly-antipsychotic
    rates: peer #k has antpsyct_ge65_tot_benes=k, ge65_tot_benes=ge65_benes, so
    rate = k/ge65_benes. Distinct k -> clean within-specialty CUME_DIST. NPI of
    peer #k is _npi_for(k). ge65_benes is above the eligibility floor."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.cms_partd_prescriber ("
            " data_year, npi, prscrbr_last_org_name, prscrbr_first_name, "
            " prscrbr_city, prscrbr_state_abrvtn, prscrbr_type, "
            " tot_clms, tot_drug_cst, tot_benes, "
            " antpsyct_ge65_tot_benes, ge65_tot_benes, "
            " source_url, source_sha256, source_vintage"
            ") SELECT %s, (1000000000 + gs)::text, 'DOC' || gs, 'A', "
            "          'NEWARK', 'NJ', %s, 1000, 50000, 200, "
            "          gs::numeric, %s, "
            "          'https://example.test/partd.csv', %s, 'CY2024' "
            "   FROM generate_series(1, %s) AS gs",
            (data_year, specialty, ge65_benes, "0" * 64, n),
        )


def _seed_one(
    conn: psycopg.Connection,
    *,
    npi: str,
    specialty: str,
    ap_benes: float | None,
    ge65_benes: float | None,
    data_year: int = _DATA_YEAR,
    state: str = "NJ",
    last_org: str = "DOE",
    first: str = "JANE",
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.cms_partd_prescriber ("
            " data_year, npi, prscrbr_last_org_name, prscrbr_first_name, "
            " prscrbr_city, prscrbr_state_abrvtn, prscrbr_type, "
            " tot_clms, tot_drug_cst, tot_benes, "
            " antpsyct_ge65_tot_benes, ge65_tot_benes, "
            " source_url, source_sha256, source_vintage"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, 1000, 50000, 200, "
            "          %s, %s, "
            "          'https://example.test/partd.csv', %s, 'CY2024')",
            (
                data_year, npi, last_org, first, "NEWARK", state, specialty,
                ap_benes, ge65_benes, "0" * 64,
            ),
        )


def _refresh(conn: psycopg.Connection, cycle: str = _CYCLE) -> int:
    v = _scalar(
        conn,
        "SELECT derived.refresh_signal_antipsychotic_elderly_outlier(%s)",
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


def test_platform_constants_seeded(ap_db: psycopg.Connection) -> None:
    for cid, expected in (
        ("antipsychotic_elderly_tail_pctile", 0.99),
        ("antipsychotic_elderly_min_ge65_benes", 50.0),
        ("antipsychotic_elderly_min_bucket", 50.0),
    ):
        val = _scalar(ap_db, "SELECT derived.f_platform_constant(%s)", cid)
        assert isinstance(val, Decimal), f"constant {cid} not seeded"
        assert float(val) == pytest.approx(expected)


def test_signal_config_uses_cms_utilization_family(
    ap_db: psycopg.Connection,
) -> None:
    with ap_db.cursor() as cur:
        cur.execute(
            "SELECT signal_family, min_actionable_threshold "
            "FROM derived.fraud_signal_config WHERE signal_id = %s",
            (_SIGNAL,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "cms_utilization"
    assert float(row[1]) == 0.0


# ----------------------------------------------------------------------------
# Refresher behavior
# ----------------------------------------------------------------------------


def test_refresher_zero_on_empty_substrate(ap_db: psycopg.Connection) -> None:
    assert _refresh(ap_db) == 0


def test_extreme_flagged_median_not(ap_db: psycopg.Connection) -> None:
    """In a 100-peer specialty (rates 1..100 per 100 elderly benes), the
    top-rate prescriber is flagged; a median-rate one is not."""
    _seed_specialty_bucket(ap_db, specialty="Psychiatry", n=100)
    ap_db.commit()
    assert _refresh(ap_db) >= 1

    flagged = _flagged_npis(ap_db)
    assert _npi_for(100) in flagged, "the rate=100% outlier must be flagged"
    assert _npi_for(50) not in flagged, "a median prescriber must NOT be flagged"

    with ap_db.cursor() as cur:
        cur.execute(
            "SELECT entity_kind, severity, peer_bucket, raw_value, "
            "       peer_percentile "
            "FROM derived.fraud_signal_observation "
            "WHERE signal_id = %s AND entity_id = %s",
            (_SIGNAL, _npi_for(100)),
        )
        row = cur.fetchone()
    assert row is not None
    kind, sev, bucket, raw_value, pctile = row
    assert kind == "provider"
    assert sev == 4
    assert bucket == "specialty=Psychiatry"
    # rate = 100 benes / 100 ge65 benes * 100 = 100.0%
    assert float(raw_value) == pytest.approx(100.0)
    assert float(pctile) == pytest.approx(1.0)


def test_zero_prescribers_never_flagged(ap_db: psycopg.Connection) -> None:
    """The metric is zero-inflated: most providers prescribe ZERO antipsychotics
    to the elderly (the good outcome). Those must NEVER be flagged, even when
    they dominate the specialty -- otherwise CUME_DIST over the zero-mode would
    flag non-prescribers. Only actual prescribers form the peer group."""
    # 60 actual prescribers (rate 1/200 .. 60/200), NPIs 1000000001..1000000060.
    _seed_specialty_bucket(ap_db, specialty="Psychiatry", n=60, ge65_benes=200)
    # 100 zero-rate providers in the SAME specialty (NPIs 1000002001..2100).
    with ap_db.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.cms_partd_prescriber ("
            " data_year, npi, prscrbr_type, prscrbr_state_abrvtn, "
            " antpsyct_ge65_tot_benes, ge65_tot_benes, "
            " source_url, source_sha256, source_vintage"
            ") SELECT %s, (1000002000 + gs)::text, 'Psychiatry', 'NJ', "
            "          0, 200, 'https://example.test/partd.csv', %s, 'CY2024' "
            "   FROM generate_series(1, 100) AS gs",
            (_DATA_YEAR, "0" * 64),
        )
    ap_db.commit()
    _refresh(ap_db)
    flagged = _flagged_npis(ap_db)
    # No zero-rate provider may be flagged.
    assert not any(f.startswith("1000002") for f in flagged), (
        "zero-antipsychotic providers must never be flagged"
    )
    # The top actual prescriber is flagged; raw_value is a real positive rate.
    assert _npi_for(60) in flagged
    with ap_db.cursor() as cur:
        cur.execute(
            "SELECT MIN(raw_value) FROM derived.fraud_signal_observation "
            "WHERE signal_id = %s",
            (_SIGNAL,),
        )
        (min_rate,) = cur.fetchone()
    assert float(min_rate) > 0.0, "no zero-rate row should ever be emitted"


def test_bucket_below_min_peers_yields_nothing(
    ap_db: psycopg.Connection,
) -> None:
    """A specialty with only 49 peers (< min_bucket=50) yields nothing."""
    _seed_specialty_bucket(ap_db, specialty="Rare Specialty", n=49)
    ap_db.commit()
    assert _refresh(ap_db) == 0


def test_elderly_floor_excludes_small_denominator(
    ap_db: psycopg.Connection,
) -> None:
    """A prescriber below the elderly-beneficiary floor (ge65<50) is
    ineligible even with an extreme rate; the >=50 bucket still ranks."""
    _seed_specialty_bucket(ap_db, specialty="Psychiatry", n=100)
    _seed_one(
        ap_db, npi="1999999999", specialty="Psychiatry",
        ap_benes=10, ge65_benes=10,  # rate 100% but only 10 elderly benes
    )
    ap_db.commit()
    _refresh(ap_db)
    flagged = _flagged_npis(ap_db)
    assert "1999999999" not in flagged, (
        "a sub-floor elderly-denominator prescriber must be ineligible"
    )
    assert _npi_for(100) in flagged


def test_suppressed_and_placeholder_never_flagged(
    ap_db: psycopg.Connection,
) -> None:
    _seed_specialty_bucket(ap_db, specialty="Psychiatry", n=100)
    # CMS-suppressed numerator (NULL) -> ineligible.
    _seed_one(ap_db, npi="2000000001", specialty="Psychiatry",
              ap_benes=None, ge65_benes=200)
    # Suppressed denominator (NULL) -> ineligible.
    _seed_one(ap_db, npi="2000000002", specialty="Psychiatry",
              ap_benes=100, ge65_benes=None)
    # Placeholder NPI -> ineligible.
    _seed_one(ap_db, npi="0000000000", specialty="Psychiatry",
              ap_benes=200, ge65_benes=200)
    ap_db.commit()
    _refresh(ap_db)
    flagged = _flagged_npis(ap_db)
    assert "2000000001" not in flagged
    assert "2000000002" not in flagged
    assert "0000000000" not in flagged


def test_missing_constant_raises(ap_db: psycopg.Connection) -> None:
    import psycopg.errors
    with ap_db.cursor() as cur:
        cur.execute(
            "DELETE FROM ref.platform_constants "
            "WHERE constant_id = 'antipsychotic_elderly_tail_pctile'"
        )
    ap_db.commit()
    with pytest.raises(psycopg.errors.NoDataFound):
        _refresh(ap_db)
    ap_db.rollback()


def test_refresher_is_idempotent(ap_db: psycopg.Connection) -> None:
    _seed_specialty_bucket(ap_db, specialty="Psychiatry", n=100)
    ap_db.commit()
    n1 = _refresh(ap_db)
    n2 = _refresh(ap_db)
    assert n1 == n2
    total = _scalar(
        ap_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE signal_id = %s",
        _SIGNAL,
    )
    assert total == n1


def test_refresher_isolates_other_cycles(ap_db: psycopg.Connection) -> None:
    _seed_specialty_bucket(ap_db, specialty="Psychiatry", n=100,
                           data_year=2024)
    with ap_db.cursor() as cur:
        cur.execute(
            "INSERT INTO derived.fraud_signal_observation "
            "(cycle, entity_kind, entity_id, signal_id, raw_value, severity, "
            " peer_bucket, peer_percentile, evidence_url) "
            "VALUES ('2099', 'provider', 'PROBE', %s, 1, 4, 'specialty=X', "
            "0.99, '/probe-2099')",
            (_SIGNAL,),
        )
    ap_db.commit()
    _refresh(ap_db, "2024")
    n = _scalar(
        ap_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle = '2099' AND signal_id = %s",
        _SIGNAL,
    )
    assert n == 1


def test_master_refresher_includes_signal(ap_db: psycopg.Connection) -> None:
    _seed_specialty_bucket(ap_db, specialty="Psychiatry", n=100,
                           data_year=2024)
    ap_db.commit()
    _scalar(
        ap_db,
        "SELECT derived.refresh_all_fraud_signal_observations(%s)",
        "2024",
    )
    n = _scalar(
        ap_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle = '2024' AND signal_id = %s",
        _SIGNAL,
    )
    assert isinstance(n, int) and n >= 1


# ----------------------------------------------------------------------------
# Reference data + provenance
# ----------------------------------------------------------------------------


def test_severity_calibration_row(ap_db: psycopg.Connection) -> None:
    with ap_db.cursor() as cur:
        cur.execute(
            "SELECT severity_level, calibration_basis "
            "FROM ref.fraud_signal_severity_calibration WHERE signal_id = %s",
            (_SIGNAL,),
        )
        row = cur.fetchone()
    assert row == (4, "empirical_pctile")


def test_reportability_channel_no_bounty_tier3(ap_db: psycopg.Connection) -> None:
    with ap_db.cursor() as cur:
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


def test_evidence_url_template_row(ap_db: psycopg.Connection) -> None:
    with ap_db.cursor() as cur:
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


def test_formula_version_registered(ap_db: psycopg.Connection) -> None:
    with ap_db.cursor() as cur:
        cur.execute(
            "SELECT effective_date, description "
            "FROM ref.formula_version WHERE formula_version = %s",
            (EXPECTED_FORMULA_VERSION,),
        )
        row = cur.fetchone()
    assert row is not None
    eff_date, desc = row
    assert eff_date.isoformat() == "2026-06-09"
    assert "antipsychotic_elderly_outlier" in desc
