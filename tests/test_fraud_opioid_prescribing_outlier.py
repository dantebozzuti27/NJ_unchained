"""Live-PG regression tests for migration 106 + seed 044.

VISION_2026 Pillar 2 (civic integrity) FRAUD-F7 Phase-2 (CMS utilization).

The signal under test -- opioid_prescribing_outlier -- is the platform's
first DISTRIBUTIONAL healthcare signal: a CMS Part D prescriber in the
extreme upper tail (top 1%) of its OWN specialty peer group on the
CMS-published opioid-prescribing rate, gated by a claim-volume floor and a
minimum specialty-peer count (all three constants from
ref.platform_constants -- no inline magic numbers).

What this module pins:
    * Tuning constants seeded into ref.platform_constants.
    * Refresher derived.refresh_signal_opioid_prescribing_outlier:
        - empty substrate -> 0, no error
        - within a >=100-peer specialty, the extreme-rate prescriber is
          flagged; a median-rate prescriber is not
        - emitted fields: entity_kind=provider, severity 4, bucket
          'specialty=<type>', raw_value = the opioid rate, percentile =
          within-specialty CUME_DIST
        - BUCKET-SIZE GUARD: a specialty with < min_bucket peers yields
          nothing even if a prescriber tops it
        - VOLUME-FLOOR GUARD: a prescriber below the claim floor is
          ineligible (cannot be flagged)
        - missing-constant guard RAISES (loud, never silent default)
        - NPI hygiene, idempotency, cycle isolation
    * Master refresher invokes the new refresher.
    * Reference data: severity 4 / empirical_pctile; platform authority;
      CMS.gov verify URL; cms_utilization signal_family.
    * Evidence-card view renders display_name + explanation with no residue.
    * formula_version registered.
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


EXPECTED_FORMULA_VERSION = "2.8.6-fraud-opioid-prescribing-outlier-v1"
_DATA_YEAR = 2023
_CYCLE = str(_DATA_YEAR)


# ============================================================================
# Fixtures + helpers
# ============================================================================


@pytest.fixture
def op_db(live_pg: psycopg.Connection) -> psycopg.Connection:
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


def _npi_for(rank: int) -> str:
    """Deterministic 10-digit NPI for synthetic peer #rank."""
    return str(1000000000 + rank)


def _seed_specialty_bucket(
    conn: psycopg.Connection,
    *,
    specialty: str,
    n: int,
    data_year: int = _DATA_YEAR,
    tot_clms: int = 100,
) -> None:
    """Seed *n* prescribers in *specialty* with distinct opioid rates 1..n
    (so within-specialty CUME_DIST is clean) via generate_series.

    NPI of peer #k is _npi_for(k). All carry tot_clms above the volume floor.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.cms_partd_prescriber ("
            " data_year, npi, prscrbr_last_org_name, prscrbr_first_name, "
            " prscrbr_city, prscrbr_state_abrvtn, prscrbr_type, "
            " tot_clms, tot_drug_cst, tot_benes, "
            " opioid_tot_clms, opioid_prscrbr_rate, "
            " source_url, source_sha256, source_vintage"
            ") SELECT %s, (1000000000 + gs)::text, 'DOC' || gs, 'A', "
            "          'NEWARK', 'NJ', %s, %s, 1000, 50, "
            "          NULL, gs::numeric, "
            "          'https://example.test/partd.csv', %s, 'CY2023' "
            "   FROM generate_series(1, %s) AS gs",
            (data_year, specialty, tot_clms, "0" * 64, n),
        )


def _seed_one(
    conn: psycopg.Connection,
    *,
    npi: str,
    specialty: str,
    rate: float | None,
    data_year: int = _DATA_YEAR,
    tot_clms: int = 100,
    state: str = "NJ",
    last_org: str = "DOE",
    first: str = "JANE",
) -> None:
    """Seed one specific prescriber row."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.cms_partd_prescriber ("
            " data_year, npi, prscrbr_last_org_name, prscrbr_first_name, "
            " prscrbr_city, prscrbr_state_abrvtn, prscrbr_type, "
            " tot_clms, tot_drug_cst, tot_benes, "
            " opioid_tot_clms, opioid_prscrbr_rate, "
            " source_url, source_sha256, source_vintage"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "          %s, %s, %s)",
            (
                data_year, npi, last_org, first,
                "NEWARK", state, specialty,
                tot_clms, 1000, 50,
                None, rate,
                "https://example.test/partd.csv", "0" * 64, "CY2023",
            ),
        )


def _refresh(conn: psycopg.Connection, cycle: str = _CYCLE) -> int:
    v = _scalar(
        conn,
        "SELECT derived.refresh_signal_opioid_prescribing_outlier(%s)",
        cycle,
    )
    assert isinstance(v, int)
    return v


def _count(conn: psycopg.Connection, q: str, *args: object) -> int:
    v = _scalar(conn, q, *args)
    assert isinstance(v, int)
    return v


def _flagged_npis(conn: psycopg.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT entity_id FROM derived.fraud_signal_observation "
            "WHERE signal_id = 'opioid_prescribing_outlier'"
        )
        return {r[0] for r in cur.fetchall()}


# ============================================================================
# 1. Constants + config
# ============================================================================


def test_platform_constants_seeded(op_db: psycopg.Connection) -> None:
    for cid, expected in (
        ("opioid_outlier_tail_pctile", 0.99),
        ("opioid_outlier_min_claims", 50.0),
        ("opioid_outlier_min_bucket", 100.0),
    ):
        val = _scalar(
            op_db,
            "SELECT derived.f_platform_constant(%s)",
            cid,
        )
        assert isinstance(val, Decimal), f"constant {cid} not seeded"
        assert float(val) == pytest.approx(expected)


def test_signal_config_uses_cms_utilization_family(
    op_db: psycopg.Connection,
) -> None:
    with op_db.cursor() as cur:
        cur.execute(
            "SELECT signal_family, min_actionable_threshold "
            "FROM derived.fraud_signal_config "
            "WHERE signal_id = 'opioid_prescribing_outlier'"
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "cms_utilization"
    assert float(row[1]) == 0.0


# ============================================================================
# 2. Refresher: empty + the core tail behavior
# ============================================================================


def test_refresher_zero_on_empty_substrate(op_db: psycopg.Connection) -> None:
    assert _refresh(op_db) == 0


def test_extreme_flagged_median_not(op_db: psycopg.Connection) -> None:
    """In a 100-peer specialty (rates 1..100), the top-rate prescriber is
    flagged; a median-rate one is not."""
    _seed_specialty_bucket(op_db, specialty="Pain Management", n=100)
    op_db.commit()
    assert _refresh(op_db) >= 1

    flagged = _flagged_npis(op_db)
    assert _npi_for(100) in flagged, "the rate=100 outlier must be flagged"
    assert _npi_for(50) not in flagged, "a median prescriber must NOT be flagged"

    with op_db.cursor() as cur:
        cur.execute(
            "SELECT entity_kind, severity, peer_bucket, raw_value, "
            "       peer_percentile "
            "FROM derived.fraud_signal_observation "
            "WHERE signal_id = 'opioid_prescribing_outlier' "
            "  AND entity_id = %s",
            (_npi_for(100),),
        )
        row = cur.fetchone()
    assert row is not None
    kind, sev, bucket, raw_value, pctile = row
    assert kind == "provider"
    assert sev == 4
    assert bucket == "specialty=Pain Management"
    assert float(raw_value) == pytest.approx(100.0)
    assert float(pctile) == pytest.approx(1.0)


def test_bucket_below_min_peers_yields_nothing(
    op_db: psycopg.Connection,
) -> None:
    """A specialty with only 50 peers (< min_bucket=100) yields nothing,
    even though one prescriber tops it."""
    _seed_specialty_bucket(op_db, specialty="Rare Specialty", n=50)
    op_db.commit()
    assert _refresh(op_db) == 0


def test_volume_floor_excludes_low_claim_prescriber(
    op_db: psycopg.Connection,
) -> None:
    """A prescriber below the claim-volume floor is ineligible and cannot be
    flagged, even with an extreme rate; the rest of the >=100 bucket still
    ranks normally."""
    _seed_specialty_bucket(op_db, specialty="Pain Management", n=100)
    # Extreme rate but only 10 claims -> below the 50-claim floor.
    _seed_one(
        op_db, npi="1999999999", specialty="Pain Management",
        rate=999.0, tot_clms=10,
    )
    op_db.commit()
    _refresh(op_db)
    flagged = _flagged_npis(op_db)
    assert "1999999999" not in flagged, (
        "a sub-floor-volume prescriber must be ineligible"
    )
    # The legitimate top-of-bucket prescriber is still flagged.
    assert _npi_for(100) in flagged


def test_null_rate_and_placeholder_npi_never_flagged(
    op_db: psycopg.Connection,
) -> None:
    _seed_specialty_bucket(op_db, specialty="Pain Management", n=100)
    _seed_one(op_db, npi="2000000001", specialty="Pain Management", rate=None)
    _seed_one(op_db, npi="0000000000", specialty="Pain Management", rate=999.0)
    op_db.commit()
    _refresh(op_db)
    flagged = _flagged_npis(op_db)
    assert "2000000001" not in flagged  # NULL rate
    assert "0000000000" not in flagged  # placeholder NPI


# ============================================================================
# 3. Missing-constant guard
# ============================================================================


def test_missing_constant_raises(op_db: psycopg.Connection) -> None:
    """Deleting a tuning constant makes the refresher RAISE, never silently
    fall back to a default."""
    import psycopg.errors
    with op_db.cursor() as cur:
        cur.execute(
            "DELETE FROM ref.platform_constants "
            "WHERE constant_id = 'opioid_outlier_tail_pctile'"
        )
    op_db.commit()
    with pytest.raises(psycopg.errors.NoDataFound):
        _refresh(op_db)
    op_db.rollback()


# ============================================================================
# 4. Idempotency + cycle isolation
# ============================================================================


def test_refresher_is_idempotent(op_db: psycopg.Connection) -> None:
    _seed_specialty_bucket(op_db, specialty="Pain Management", n=100)
    op_db.commit()
    n1 = _refresh(op_db)
    n2 = _refresh(op_db)
    assert n1 == n2
    total = _count(
        op_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE signal_id = 'opioid_prescribing_outlier'",
    )
    assert total == n1


def test_refresher_isolates_other_cycles(op_db: psycopg.Connection) -> None:
    _seed_specialty_bucket(op_db, specialty="Pain Management", n=100,
                           data_year=2023)
    with op_db.cursor() as cur:
        cur.execute(
            "INSERT INTO derived.fraud_signal_observation "
            "(cycle, entity_kind, entity_id, signal_id, raw_value, severity, "
            " peer_bucket, peer_percentile, evidence_url) "
            "VALUES ('2099', 'provider', 'PROBE', "
            "'opioid_prescribing_outlier', 1, 4, 'specialty=X', 0.99, "
            "'/probe-2099')"
        )
    op_db.commit()
    _refresh(op_db, "2023")
    n = _scalar(
        op_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle = '2099' AND signal_id = 'opioid_prescribing_outlier'",
    )
    assert n == 1


# ============================================================================
# 5. Master refresher integration
# ============================================================================


def test_master_refresher_includes_signal(op_db: psycopg.Connection) -> None:
    _seed_specialty_bucket(op_db, specialty="Pain Management", n=100,
                           data_year=2023)
    op_db.commit()
    _scalar(
        op_db,
        "SELECT derived.refresh_all_fraud_signal_observations(%s)",
        "2023",
    )
    n = _count(
        op_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle = '2023' AND signal_id = 'opioid_prescribing_outlier'",
    )
    assert n >= 1, "master refresher must invoke opioid_prescribing_outlier"


# ============================================================================
# 6. Reference data + evidence-card view
# ============================================================================


def test_severity_calibration_row(op_db: psycopg.Connection) -> None:
    with op_db.cursor() as cur:
        cur.execute(
            "SELECT severity_level, calibration_basis "
            "FROM ref.fraud_signal_severity_calibration "
            "WHERE signal_id = 'opioid_prescribing_outlier'"
        )
        row = cur.fetchone()
    assert row == (4, "empirical_pctile")


def test_human_explanation_row(op_db: psycopg.Connection) -> None:
    with op_db.cursor() as cur:
        cur.execute(
            "SELECT citation_authority FROM ref.fraud_signal_human_explanation "
            "WHERE signal_id = 'opioid_prescribing_outlier'"
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "platform"


def test_evidence_url_template_row(op_db: psycopg.Connection) -> None:
    with op_db.cursor() as cur:
        cur.execute(
            "SELECT url_template, upstream_source "
            "FROM ref.fraud_signal_evidence_url_template "
            "WHERE signal_id = 'opioid_prescribing_outlier'"
        )
        row = cur.fetchone()
    assert row is not None
    url, source = row
    assert url.startswith("https://data.cms.gov")
    assert source == "CMS.gov"


def test_evidence_view_renders_clean(op_db: psycopg.Connection) -> None:
    """v_entity_fraud_evidence resolves the provider name and renders the
    explanation with no token residue for a flagged outlier."""
    _seed_specialty_bucket(op_db, specialty="Pain Management", n=99)
    _seed_one(
        op_db, npi="1234567893", specialty="Pain Management", rate=500.0,
        last_org="DOE", first="JANE", state="NJ",
    )
    op_db.commit()
    _refresh(op_db)

    with op_db.cursor() as cur:
        cur.execute(
            "SELECT display_name, is_nj, severity, rendered_explanation, "
            "       upstream_verify_url, citation_authority "
            "FROM derived.v_entity_fraud_evidence "
            "WHERE signal_id = 'opioid_prescribing_outlier' "
            "  AND entity_id = '1234567893'"
        )
        row = cur.fetchone()
    assert row is not None, "no evidence row for the flagged outlier"
    display_name, is_nj, severity, explanation, upstream, citation = row
    assert display_name == "JANE DOE"
    assert is_nj is True
    assert severity == 4
    assert upstream.startswith("https://data.cms.gov")
    assert citation == "platform"
    import re
    assert re.findall(r"\{\{[^}]+\}\}", explanation) == [], (
        f"unsubstituted tokens in rendered_explanation: {explanation!r}"
    )


# ============================================================================
# 7. Provenance
# ============================================================================


def test_formula_version_registered(op_db: psycopg.Connection) -> None:
    with op_db.cursor() as cur:
        cur.execute(
            "SELECT effective_date, description "
            "FROM ref.formula_version WHERE formula_version = %s",
            (EXPECTED_FORMULA_VERSION,),
        )
        row = cur.fetchone()
    assert row is not None
    eff_date, desc = row
    assert eff_date.isoformat() == "2026-06-09"
    assert "opioid_prescribing_outlier" in desc
