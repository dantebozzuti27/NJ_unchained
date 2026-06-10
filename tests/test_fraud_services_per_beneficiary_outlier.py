"""Live-PG regression tests for migration 107 + seed 045.

VISION_2026 Pillar 2 (civic integrity) FRAUD-F7 Phase-2 (CMS utilization).

services_per_beneficiary_outlier is the Part-B overutilization companion to
opioid_prescribing_outlier (mig 106): a CMS Part B practitioner in the
extreme upper tail (top 1%) of its OWN specialty peer group on
Tot_Srvcs/Tot_Benes, gated by a beneficiary-count floor and a minimum
specialty-peer count (all three constants from ref.platform_constants).

Note on the evidence view: mig 109 widened the provider_meta CTE to
resolve display_name / is_nj from EITHER raw.cms_partd_prescriber (Part D,
preferred) OR raw.cms_physician_provider (Part B). A Part-B-only provider
therefore now resolves a real name + NJ flag -- asserted below.
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


EXPECTED_FORMULA_VERSION = "2.8.7-fraud-services-per-beneficiary-outlier-v1"
_DATA_YEAR = 2023
_CYCLE = str(_DATA_YEAR)


@pytest.fixture
def spb_db(live_pg: psycopg.Connection) -> psycopg.Connection:
    """Cleanly-migrated DB with all migrations + seeds applied."""
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


def _refresh(conn: psycopg.Connection, cycle: str = _CYCLE) -> int:
    v = _scalar(
        conn,
        "SELECT derived.refresh_signal_services_per_beneficiary_outlier(%s)",
        cycle,
    )
    assert isinstance(v, int)
    return v


def _count(conn: psycopg.Connection, q: str, *args: object) -> int:
    v = _scalar(conn, q, *args)
    assert isinstance(v, int)
    return v


def _npi_for(rank: int) -> str:
    return str(1000000000 + rank)


def _seed_bucket(
    conn: psycopg.Connection,
    *,
    specialty: str,
    n: int,
    data_year: int = _DATA_YEAR,
    tot_benes: int = 100,
) -> None:
    """Seed *n* Part B practitioners in *specialty* with distinct ratios 1..n
    (tot_srvcs = gs*tot_benes -> ratio = gs) via generate_series. All carry
    tot_benes above the beneficiary floor."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.cms_physician_provider ("
            " data_year, npi, prvdr_last_org_name, prvdr_first_name, "
            " prvdr_city, prvdr_state_abrvtn, prvdr_type, "
            " tot_benes, tot_srvcs, tot_mdcr_alowd_amt, tot_mdcr_pymt_amt, "
            " tot_sbmtd_chrg, bene_avg_risk_scre, "
            " source_url, source_sha256, source_vintage"
            ") SELECT %s, (1000000000 + gs)::text, 'DOC' || gs, 'A', "
            "          'NEWARK', 'NJ', %s, %s, gs * %s, 1000, 1000, "
            "          1000, 1.0, 'https://example.test/partb.csv', %s, "
            "          'CY2023' "
            "   FROM generate_series(1, %s) AS gs",
            (data_year, specialty, tot_benes, tot_benes, "0" * 64, n),
        )


def _seed_one(
    conn: psycopg.Connection,
    *,
    npi: str,
    specialty: str,
    tot_benes: int,
    tot_srvcs: int,
    data_year: int = _DATA_YEAR,
    state: str = "NJ",
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.cms_physician_provider ("
            " data_year, npi, prvdr_last_org_name, prvdr_first_name, "
            " prvdr_city, prvdr_state_abrvtn, prvdr_type, "
            " tot_benes, tot_srvcs, tot_mdcr_alowd_amt, tot_mdcr_pymt_amt, "
            " tot_sbmtd_chrg, bene_avg_risk_scre, "
            " source_url, source_sha256, source_vintage"
            ") VALUES (%s, %s, 'DOE', 'JANE', 'NEWARK', %s, %s, %s, %s, "
            "          1000, 1000, 1000, 1.0, "
            "          'https://example.test/partb.csv', %s, 'CY2023')",
            (
                data_year, npi, state, specialty, tot_benes, tot_srvcs,
                "0" * 64,
            ),
        )


def _flagged_npis(conn: psycopg.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT entity_id FROM derived.fraud_signal_observation "
            "WHERE signal_id = 'services_per_beneficiary_outlier'"
        )
        return {r[0] for r in cur.fetchall()}


# ============================================================================
# Constants + config
# ============================================================================


def test_platform_constants_seeded(spb_db: psycopg.Connection) -> None:
    for cid, expected in (
        ("spb_outlier_tail_pctile", 0.99),
        ("spb_outlier_min_benes", 50.0),
        ("spb_outlier_min_bucket", 100.0),
    ):
        val = _scalar(spb_db, "SELECT derived.f_platform_constant(%s)", cid)
        assert isinstance(val, Decimal), f"constant {cid} not seeded"
        assert float(val) == pytest.approx(expected)


def test_signal_config_uses_cms_utilization_family(
    spb_db: psycopg.Connection,
) -> None:
    with spb_db.cursor() as cur:
        cur.execute(
            "SELECT signal_family, min_actionable_threshold "
            "FROM derived.fraud_signal_config "
            "WHERE signal_id = 'services_per_beneficiary_outlier'"
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "cms_utilization"
    assert float(row[1]) == 0.0


# ============================================================================
# Refresher behavior
# ============================================================================


def test_refresher_zero_on_empty_substrate(spb_db: psycopg.Connection) -> None:
    assert _refresh(spb_db) == 0


def test_extreme_flagged_median_not(spb_db: psycopg.Connection) -> None:
    _seed_bucket(spb_db, specialty="Ophthalmology", n=100)
    spb_db.commit()
    assert _refresh(spb_db) >= 1

    flagged = _flagged_npis(spb_db)
    assert _npi_for(100) in flagged
    assert _npi_for(50) not in flagged

    with spb_db.cursor() as cur:
        cur.execute(
            "SELECT entity_kind, severity, peer_bucket, raw_value, "
            "       peer_percentile "
            "FROM derived.fraud_signal_observation "
            "WHERE signal_id = 'services_per_beneficiary_outlier' "
            "  AND entity_id = %s",
            (_npi_for(100),),
        )
        row = cur.fetchone()
    assert row is not None
    kind, sev, bucket, raw_value, pctile = row
    assert kind == "provider"
    assert sev == 4
    assert bucket == "specialty=Ophthalmology"
    assert float(raw_value) == pytest.approx(100.0)  # ratio = gs = 100
    assert float(pctile) == pytest.approx(1.0)


def test_bucket_below_min_peers_yields_nothing(
    spb_db: psycopg.Connection,
) -> None:
    _seed_bucket(spb_db, specialty="Rare Specialty", n=50)
    spb_db.commit()
    assert _refresh(spb_db) == 0


def test_beneficiary_floor_excludes_low_panel(
    spb_db: psycopg.Connection,
) -> None:
    _seed_bucket(spb_db, specialty="Ophthalmology", n=100)
    # Extreme ratio but only 10 beneficiaries -> below the 50-bene floor.
    _seed_one(
        spb_db, npi="1999999999", specialty="Ophthalmology",
        tot_benes=10, tot_srvcs=99999,
    )
    spb_db.commit()
    _refresh(spb_db)
    flagged = _flagged_npis(spb_db)
    assert "1999999999" not in flagged
    assert _npi_for(100) in flagged


def test_placeholder_npi_never_flagged(spb_db: psycopg.Connection) -> None:
    _seed_bucket(spb_db, specialty="Ophthalmology", n=100)
    _seed_one(
        spb_db, npi="0000000000", specialty="Ophthalmology",
        tot_benes=100, tot_srvcs=999999,
    )
    spb_db.commit()
    _refresh(spb_db)
    assert "0000000000" not in _flagged_npis(spb_db)


def test_missing_constant_raises(spb_db: psycopg.Connection) -> None:
    import psycopg.errors
    with spb_db.cursor() as cur:
        cur.execute(
            "DELETE FROM ref.platform_constants "
            "WHERE constant_id = 'spb_outlier_min_bucket'"
        )
    spb_db.commit()
    with pytest.raises(psycopg.errors.NoDataFound):
        _refresh(spb_db)
    spb_db.rollback()


def test_refresher_is_idempotent(spb_db: psycopg.Connection) -> None:
    _seed_bucket(spb_db, specialty="Ophthalmology", n=100)
    spb_db.commit()
    n1 = _refresh(spb_db)
    n2 = _refresh(spb_db)
    assert n1 == n2
    total = _count(
        spb_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE signal_id = 'services_per_beneficiary_outlier'",
    )
    assert total == n1


def test_master_refresher_includes_signal(spb_db: psycopg.Connection) -> None:
    _seed_bucket(spb_db, specialty="Ophthalmology", n=100, data_year=2023)
    spb_db.commit()
    _scalar(
        spb_db,
        "SELECT derived.refresh_all_fraud_signal_observations(%s)",
        "2023",
    )
    n = _count(
        spb_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle = '2023' AND signal_id = "
        "'services_per_beneficiary_outlier'",
    )
    assert n >= 1


# ============================================================================
# Reference data + evidence view
# ============================================================================


def test_severity_calibration_row(spb_db: psycopg.Connection) -> None:
    with spb_db.cursor() as cur:
        cur.execute(
            "SELECT severity_level, calibration_basis "
            "FROM ref.fraud_signal_severity_calibration "
            "WHERE signal_id = 'services_per_beneficiary_outlier'"
        )
        row = cur.fetchone()
    assert row == (4, "empirical_pctile")


def test_evidence_url_template_row(spb_db: psycopg.Connection) -> None:
    with spb_db.cursor() as cur:
        cur.execute(
            "SELECT url_template, upstream_source "
            "FROM ref.fraud_signal_evidence_url_template "
            "WHERE signal_id = 'services_per_beneficiary_outlier'"
        )
        row = cur.fetchone()
    assert row is not None
    url, source = row
    assert url.startswith("https://data.cms.gov")
    assert source == "CMS.gov"


def test_evidence_view_resolves_partb_name_and_is_nj(
    spb_db: psycopg.Connection,
) -> None:
    """Since mig 109 widened provider_meta to read Part B, a Part-B-only
    provider resolves a real display_name (from cms_physician_provider) and
    is_nj from its practice state, and the explanation renders with no
    token residue."""
    _seed_bucket(spb_db, specialty="Ophthalmology", n=99)
    _seed_one(
        spb_db, npi="1234567893", specialty="Ophthalmology",
        tot_benes=100, tot_srvcs=50000,
    )
    spb_db.commit()
    _refresh(spb_db)

    with spb_db.cursor() as cur:
        cur.execute(
            "SELECT display_name, is_nj, severity, rendered_explanation, "
            "       upstream_verify_url, citation_authority "
            "FROM derived.v_entity_fraud_evidence "
            "WHERE signal_id = 'services_per_beneficiary_outlier' "
            "  AND entity_id = '1234567893'"
        )
        row = cur.fetchone()
    assert row is not None, "no evidence row for the flagged outlier"
    display_name, is_nj, severity, explanation, upstream, citation = row
    assert display_name == "JANE DOE"  # resolved from Part B (mig 109)
    assert is_nj is True               # _seed_one default state = NJ
    assert severity == 4
    assert upstream.startswith("https://data.cms.gov")
    assert citation == "platform"
    import re
    assert re.findall(r"\{\{[^}]+\}\}", explanation) == [], (
        f"unsubstituted tokens in rendered_explanation: {explanation!r}"
    )


def test_formula_version_registered(spb_db: psycopg.Connection) -> None:
    with spb_db.cursor() as cur:
        cur.execute(
            "SELECT effective_date, description "
            "FROM ref.formula_version WHERE formula_version = %s",
            (EXPECTED_FORMULA_VERSION,),
        )
        row = cur.fetchone()
    assert row is not None
    eff_date, desc = row
    assert eff_date.isoformat() == "2026-06-09"
    assert "services_per_beneficiary_outlier" in desc
