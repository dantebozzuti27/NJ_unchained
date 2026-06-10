"""Live-PG regression tests for migration 109 + seed 046.

VISION_2026 Pillar 2 (civic integrity) FRAUD-F7 (CMS Medicare, Part B).

provider_excluded_billing_partb is the Part-B companion to
provider_excluded_billing (mig 101): an active HHS-OIG LEIE exclusion
(carrying a real NPI) that appears in CMS Medicare Physician & Other
Practitioners (Part B) data for a year in which the exclusion was already
in effect. Exact NPI equijoin; severity 5 (payment-prohibition overlap,
42 USC 1320a-7a); raw_value = Tot_Mdcr_Pymt_Amt (Medicare paid amount).

This module ALSO pins the mig-109 widening of provider_meta in
v_entity_fraud_evidence: a Part-B-only excluded provider must now resolve
a real display_name (from cms_physician_provider) and is_nj from the
practitioner's practice state -- previously provider_meta read Part D only.
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


EXPECTED_FORMULA_VERSION = "2.8.9-fraud-provider-excluded-billing-partb-v1"
_DATA_YEAR = 2023
_CYCLE = str(_DATA_YEAR)
_NPI = "1234567893"
_SIGNAL = "provider_excluded_billing_partb"


@pytest.fixture
def prov_db(live_pg: psycopg.Connection) -> psycopg.Connection:
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


def _seed_leie(
    conn: psycopg.Connection,
    *,
    record_hash: str,
    npi: str | None = _NPI,
    lastname: str = "DOE",
    firstname: str = "JANE",
    excldate: str = "20200115",
    reindate: str | None = None,
    excltype: str = "1128A1",
    state: str | None = "NJ",
) -> None:
    """Seed one LEIE individual row WITH an NPI (bypassing the ingester)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.hhs_oig_leie ("
            " record_hash, lastname, firstname, midname, busname, "
            " npi, excltype, excldate, reindate, state, "
            " vintage_month, source_url, source_sha256"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                record_hash, lastname, firstname, None, None,
                npi, excltype, excldate, reindate, state,
                "2026-03",
                "https://example.test/UPDATED.csv",
                "0" * 64,
            ),
        )


def _seed_partb(
    conn: psycopg.Connection,
    *,
    npi: str,
    data_year: int = _DATA_YEAR,
    last_org: str = "DOE",
    first: str = "JANE",
    state: str = "NJ",
    tot_mdcr_pymt_amt: float | None = 98765.43,
) -> None:
    """Seed one CMS Part B practitioner row (bypassing the ingester)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.cms_physician_provider ("
            " data_year, npi, prvdr_last_org_name, prvdr_first_name, "
            " prvdr_city, prvdr_state_abrvtn, prvdr_type, "
            " tot_benes, tot_srvcs, tot_mdcr_alowd_amt, tot_mdcr_pymt_amt, "
            " tot_sbmtd_chrg, bene_avg_risk_scre, "
            " source_url, source_sha256, source_vintage"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "          %s, %s, %s)",
            (
                data_year, npi, last_org, first,
                "NEWARK", state, "Internal Medicine",
                50, 500, 120000, tot_mdcr_pymt_amt,
                150000, 1.2,
                "https://example.test/partb.csv", "0" * 64, "CY2023",
            ),
        )


def _refresh(conn: psycopg.Connection, cycle: str = _CYCLE) -> object:
    return _scalar(
        conn,
        "SELECT derived.refresh_signal_provider_excluded_billing_partb(%s)",
        cycle,
    )


# ============================================================================
# 1. Empty-substrate behavior
# ============================================================================


def test_refresher_zero_on_empty_substrate(prov_db: psycopg.Connection) -> None:
    assert _refresh(prov_db) == 0


def test_refresher_zero_when_cms_empty(prov_db: psycopg.Connection) -> None:
    """LEIE has the excluded NPI but no Part B billing -> 0."""
    _seed_leie(prov_db, record_hash="a" * 64)
    prov_db.commit()
    assert _refresh(prov_db) == 0


def test_refresher_zero_when_leie_empty(prov_db: psycopg.Connection) -> None:
    """Part B has the practitioner but no exclusion -> 0."""
    _seed_partb(prov_db, npi=_NPI)
    prov_db.commit()
    assert _refresh(prov_db) == 0


# ============================================================================
# 2. The match + its fields
# ============================================================================


def test_refresher_emits_observation_on_npi_match(
    prov_db: psycopg.Connection,
) -> None:
    """Excluded NPI present in Part B within the exclusion window -> one row
    with the expected fields and raw_value = Medicare paid amount."""
    _seed_leie(prov_db, record_hash="b" * 64, npi=_NPI, excldate="20200115")
    _seed_partb(prov_db, npi=_NPI, tot_mdcr_pymt_amt=98765.43)
    for i in range(1, 5):
        _seed_partb(prov_db, npi=f"900000000{i}", state="NJ",
                    tot_mdcr_pymt_amt=1000.0)
    prov_db.commit()

    assert _refresh(prov_db) == 1

    with prov_db.cursor() as cur:
        cur.execute(
            "SELECT cycle, entity_kind, entity_id, signal_id, severity, "
            "       peer_bucket, raw_value, peer_percentile "
            "FROM derived.fraud_signal_observation "
            "WHERE signal_id = %s",
            (_SIGNAL,),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    cycle, kind, eid, sig, sev, bucket, raw_value, pctile = rows[0]
    assert (cycle, kind, eid, sig) == (_CYCLE, "provider", _NPI, _SIGNAL)
    assert sev == 5
    assert bucket == "kind=provider"
    assert float(raw_value) == pytest.approx(98765.43)
    # 1 match in a 5-practitioner bucket -> 1 - 1/5 = 0.8
    assert float(pctile) == pytest.approx(0.8)


def test_evidence_url_carries_npi_and_leie_hash(
    prov_db: psycopg.Connection,
) -> None:
    _seed_leie(prov_db, record_hash="c" * 64, npi=_NPI)
    _seed_partb(prov_db, npi=_NPI)
    prov_db.commit()
    _refresh(prov_db)
    url = _scalar(
        prov_db,
        "SELECT evidence_url FROM derived.fraud_signal_observation "
        "WHERE signal_id = %s",
        _SIGNAL,
    )
    assert isinstance(url, str)
    assert _NPI in url
    assert "leie=" + ("c" * 64) in url


# ============================================================================
# 3. Precision guards
# ============================================================================


def test_exclusion_after_billing_year_is_not_flagged(
    prov_db: psycopg.Connection,
) -> None:
    """DATE GUARD: an exclusion effective AFTER the data year must NOT fire."""
    _seed_leie(prov_db, record_hash="d" * 64, npi=_NPI, excldate="20250115")
    _seed_partb(prov_db, npi=_NPI, data_year=2023)
    prov_db.commit()
    assert _refresh(prov_db, "2023") == 0


def test_reinstated_before_year_end_is_not_flagged(
    prov_db: psycopg.Connection,
) -> None:
    """REINSTATEMENT GUARD: excluded 2018, reinstated 2022, billing 2023 ->
    no longer excluded during 2023 -> not flagged."""
    _seed_leie(
        prov_db, record_hash="e" * 64, npi=_NPI,
        excldate="20180115", reindate="20220101",
    )
    _seed_partb(prov_db, npi=_NPI, data_year=2023)
    prov_db.commit()
    assert _refresh(prov_db, "2023") == 0


def test_null_and_placeholder_npi_never_match(
    prov_db: psycopg.Connection,
) -> None:
    """An LEIE exclusion with no NPI (or a placeholder) must not join."""
    _seed_leie(prov_db, record_hash="f0" + "0" * 62, npi=None)
    _seed_leie(prov_db, record_hash="f1" + "0" * 62, npi="0000000000")
    _seed_partb(prov_db, npi="0000000000")
    _seed_partb(prov_db, npi=_NPI)
    prov_db.commit()
    assert _refresh(prov_db) == 0


# ============================================================================
# 4. Idempotency + cycle isolation
# ============================================================================


def test_refresher_is_idempotent(prov_db: psycopg.Connection) -> None:
    _seed_leie(prov_db, record_hash="1" * 64, npi=_NPI)
    _seed_partb(prov_db, npi=_NPI)
    prov_db.commit()
    n1 = _refresh(prov_db)
    n2 = _refresh(prov_db)
    assert n1 == n2 == 1
    total = _scalar(
        prov_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE signal_id = %s",
        _SIGNAL,
    )
    assert total == 1


def test_refresher_isolates_other_cycles(prov_db: psycopg.Connection) -> None:
    _seed_leie(prov_db, record_hash="2" * 64, npi=_NPI)
    _seed_partb(prov_db, npi=_NPI, data_year=2023)
    with prov_db.cursor() as cur:
        cur.execute(
            "INSERT INTO derived.fraud_signal_observation "
            "(cycle, entity_kind, entity_id, signal_id, raw_value, severity, "
            " peer_bucket, peer_percentile, evidence_url) "
            "VALUES ('2099', 'provider', 'PROBE', %s, 1, 5, 'kind=provider', "
            "0.9, '/probe-2099')",
            (_SIGNAL,),
        )
    prov_db.commit()
    _refresh(prov_db, "2023")
    n = _scalar(
        prov_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle = '2099' AND signal_id = %s",
        _SIGNAL,
    )
    assert n == 1


# ============================================================================
# 5. Master refresher integration
# ============================================================================


def test_master_refresher_includes_partb_signal(
    prov_db: psycopg.Connection,
) -> None:
    _seed_leie(prov_db, record_hash="3" * 64, npi=_NPI)
    _seed_partb(prov_db, npi=_NPI, data_year=2023)
    prov_db.commit()
    _scalar(
        prov_db,
        "SELECT derived.refresh_all_fraud_signal_observations(%s)",
        "2023",
    )
    n = _scalar(
        prov_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle = '2023' AND signal_id = %s",
        _SIGNAL,
    )
    assert n == 1, (
        "master refresher must invoke provider_excluded_billing_partb"
    )


def test_both_parts_fire_for_dual_biller(prov_db: psycopg.Connection) -> None:
    """An excluded provider billing BOTH Part D and Part B fires BOTH the
    Part-D and Part-B exclusion signals (the L3 engine stacks them)."""
    _seed_leie(prov_db, record_hash="7" * 64, npi=_NPI)
    _seed_partb(prov_db, npi=_NPI, data_year=2023)
    with prov_db.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.cms_partd_prescriber ("
            " data_year, npi, prscrbr_last_org_name, prscrbr_first_name, "
            " prscrbr_city, prscrbr_state_abrvtn, prscrbr_type, "
            " tot_clms, tot_drug_cst, tot_benes, "
            " opioid_tot_clms, opioid_prscrbr_rate, "
            " source_url, source_sha256, source_vintage"
            ") VALUES (2023, %s, 'DOE', 'JANE', 'NEWARK', 'NJ', "
            "'Internal Medicine', 100, 5000, 50, NULL, NULL, "
            "'https://example.test/partd.csv', %s, 'CY2023')",
            (_NPI, "0" * 64),
        )
    prov_db.commit()
    _scalar(
        prov_db,
        "SELECT derived.refresh_all_fraud_signal_observations(%s)",
        "2023",
    )
    with prov_db.cursor() as cur:
        cur.execute(
            "SELECT signal_id FROM derived.fraud_signal_observation "
            "WHERE cycle = '2023' AND entity_id = %s "
            "  AND signal_id IN ('provider_excluded_billing', %s) "
            "ORDER BY signal_id",
            (_NPI, _SIGNAL),
        )
        fired = [r[0] for r in cur.fetchall()]
    assert fired == ["provider_excluded_billing", _SIGNAL]


# ============================================================================
# 6. Reference data + evidence-card view (incl. mig-109 provider_meta widening)
# ============================================================================


def test_severity_calibration_row(prov_db: psycopg.Connection) -> None:
    with prov_db.cursor() as cur:
        cur.execute(
            "SELECT severity_level, calibration_basis "
            "FROM ref.fraud_signal_severity_calibration "
            "WHERE signal_id = %s",
            (_SIGNAL,),
        )
        row = cur.fetchone()
    assert row == (5, "oig_report")


def test_human_explanation_row(prov_db: psycopg.Connection) -> None:
    with prov_db.cursor() as cur:
        cur.execute(
            "SELECT citation_authority, citation_section "
            "FROM ref.fraud_signal_human_explanation "
            "WHERE signal_id = %s",
            (_SIGNAL,),
        )
        row = cur.fetchone()
    assert row is not None
    auth, section = row
    assert auth == "HHS-OIG"
    assert "1320a" in section


def test_evidence_url_template_row(prov_db: psycopg.Connection) -> None:
    with prov_db.cursor() as cur:
        cur.execute(
            "SELECT url_template, button_label, upstream_source "
            "FROM ref.fraud_signal_evidence_url_template "
            "WHERE signal_id = %s",
            (_SIGNAL,),
        )
        row = cur.fetchone()
    assert row is not None
    url, label, source = row
    assert url.startswith("https://exclusions.oig.hhs.gov")
    assert label == "Search OIG LEIE"
    assert source == "OIG.gov"


def test_evidence_view_resolves_partb_name_and_is_nj(
    prov_db: psycopg.Connection,
) -> None:
    """mig 109 widened provider_meta: a Part-B-only excluded provider must
    resolve display_name from cms_physician_provider and is_nj from its
    practice state, with no token residue in the explanation."""
    _seed_leie(prov_db, record_hash="8" * 64, npi=_NPI)
    _seed_partb(prov_db, npi=_NPI, last_org="DOE", first="JANE", state="NJ")
    prov_db.commit()
    _refresh(prov_db)

    with prov_db.cursor() as cur:
        cur.execute(
            "SELECT display_name, is_nj, severity, rendered_explanation, "
            "       upstream_verify_url, citation_authority "
            "FROM derived.v_entity_fraud_evidence "
            "WHERE signal_id = %s",
            (_SIGNAL,),
        )
        row = cur.fetchone()
    assert row is not None, "v_entity_fraud_evidence yielded no row"
    display_name, is_nj, severity, explanation, upstream, citation = row
    assert display_name == "JANE DOE"   # resolved from Part B (mig 109)
    assert is_nj is True
    assert severity == 5
    assert upstream.startswith("https://exclusions.oig.hhs.gov")
    assert citation == "HHS-OIG"
    import re
    assert re.findall(r"\{\{[^}]+\}\}", explanation) == [], (
        f"unsubstituted tokens in rendered_explanation: {explanation!r}"
    )


def test_evidence_view_one_row_when_provider_in_both_rosters(
    prov_db: psycopg.Connection,
) -> None:
    """provider_meta DISTINCT ON must keep exactly ONE identity row per
    (npi, data_year) even when the NPI is in BOTH Part D and Part B, so the
    Part-B signal yields exactly one evidence row (Part D name preferred)."""
    _seed_leie(prov_db, record_hash="aa" + "0" * 62, npi=_NPI)
    _seed_partb(prov_db, npi=_NPI, last_org="PARTB", first="NAME", state="NJ")
    with prov_db.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.cms_partd_prescriber ("
            " data_year, npi, prscrbr_last_org_name, prscrbr_first_name, "
            " prscrbr_city, prscrbr_state_abrvtn, prscrbr_type, "
            " tot_clms, tot_drug_cst, tot_benes, "
            " opioid_tot_clms, opioid_prscrbr_rate, "
            " source_url, source_sha256, source_vintage"
            ") VALUES (2023, %s, 'PARTD', 'NAME', 'NEWARK', 'NJ', "
            "'Internal Medicine', 100, 5000, 50, NULL, NULL, "
            "'https://example.test/partd.csv', %s, 'CY2023')",
            (_NPI, "0" * 64),
        )
    prov_db.commit()
    _refresh(prov_db)

    with prov_db.cursor() as cur:
        cur.execute(
            "SELECT display_name FROM derived.v_entity_fraud_evidence "
            "WHERE signal_id = %s AND entity_id = %s",
            (_SIGNAL, _NPI),
        )
        rows = cur.fetchall()
    assert len(rows) == 1, f"expected exactly one evidence row, got {rows}"
    assert rows[0][0] == "NAME PARTD"   # Part D preferred (pref=1)


def test_evidence_view_is_nj_false_for_out_of_state_provider(
    prov_db: psycopg.Connection,
) -> None:
    _seed_leie(prov_db, record_hash="9" * 64, npi=_NPI)
    _seed_partb(prov_db, npi=_NPI, state="TX")
    prov_db.commit()
    _refresh(prov_db)
    is_nj = _scalar(
        prov_db,
        "SELECT is_nj FROM derived.v_entity_fraud_evidence "
        "WHERE signal_id = %s",
        _SIGNAL,
    )
    assert is_nj is False


# ============================================================================
# 7. Provenance
# ============================================================================


def test_formula_version_registered(prov_db: psycopg.Connection) -> None:
    with prov_db.cursor() as cur:
        cur.execute(
            "SELECT effective_date, description "
            "FROM ref.formula_version WHERE formula_version = %s",
            (EXPECTED_FORMULA_VERSION,),
        )
        row = cur.fetchone()
    assert row is not None
    eff_date, desc = row
    assert eff_date.isoformat() == "2026-06-09"
    assert "provider_excluded_billing_partb" in desc
