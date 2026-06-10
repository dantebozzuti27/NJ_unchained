"""Live-PG regression tests for migrations 100 + 101 + seed 041.

VISION_2026 Pillar 2 (civic integrity) FRAUD-F7 (CMS Medicare).

The signal under test -- provider_excluded_billing -- is the first
healthcare-fraud signal on the platform and the first to fire against
entity_kind='provider'. It is also the first signal to join on an EXACT
NPI rather than a canonicalized name: an active HHS-OIG LEIE exclusion
(carrying a real NPI) that appears in CMS Medicare Part D prescriber
data for a year in which the exclusion was already in effect.

What this module pins:
    * Schema: entity_kind CHECK widened to include 'provider' (prior
      eight kinds still accepted; unknown kinds still rejected).
    * Refresher derived.refresh_signal_provider_excluded_billing:
        - empty substrate / empty LEIE / empty CMS -> 0, no error
        - exact-NPI match in the exclusion window -> exactly one row
          with the right (cycle, entity_kind, entity_id, signal_id,
          severity, peer_bucket) and raw_value = gross Part D drug cost
        - DATE GUARD: an exclusion effective AFTER the billing year is
          NOT flagged (no false positive on later exclusions)
        - REINSTATEMENT GUARD: an exclusion reinstated before year-end
          is NOT flagged
        - NPI hygiene: NULL / placeholder NPIs never match
        - bucket-relative percentile arithmetic
        - idempotency + cycle isolation
    * Master refresher invokes the new refresher (single source of truth).
    * Reference data: severity 5 / oig_report; HHS-OIG citation; OIG LEIE
      verify URL.
    * Evidence-card view resolves display_name to the CMS prescriber name
      and is_nj from the prescriber's practice state.
    * formula_version registered.
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


EXPECTED_FORMULA_VERSION = "2.8.1-fraud-provider-excluded-billing-v1"
_DATA_YEAR = 2023
_CYCLE = str(_DATA_YEAR)  # CHAR(4) cycle == CMS data_year
_NPI = "1234567893"


# ============================================================================
# Fixtures + helpers
# ============================================================================


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


def _seed_prescriber(
    conn: psycopg.Connection,
    *,
    npi: str,
    data_year: int = _DATA_YEAR,
    last_org: str = "DOE",
    first: str = "JANE",
    state: str = "NJ",
    tot_drug_cst: float | None = 123456.78,
) -> None:
    """Seed one CMS Part D prescriber row (bypassing the ingester)."""
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
                "NEWARK", state, "Internal Medicine",
                100, tot_drug_cst, 50,
                None, None,
                "https://example.test/partd.csv", "0" * 64, "CY2023",
            ),
        )


def _refresh(conn: psycopg.Connection, cycle: str = _CYCLE) -> object:
    return _scalar(
        conn,
        "SELECT derived.refresh_signal_provider_excluded_billing(%s)",
        cycle,
    )


# ============================================================================
# 1. Schema: entity_kind whitelist widening
# ============================================================================


def test_entity_kind_check_accepts_provider_and_prior_kinds(
    prov_db: psycopg.Connection,
) -> None:
    """The widened CHECK accepts 'provider' AND retains the prior eight."""
    values = [
        "candidate", "committee", "treasurer", "address",
        "donor_cluster", "contractor", "donor", "nj_state_candidate",
        "provider",
    ]
    for kind in values:
        with prov_db.cursor() as cur:
            cur.execute(
                "INSERT INTO derived.fraud_signal_observation "
                "(cycle, entity_kind, entity_id, signal_id, "
                " raw_value, severity, peer_bucket, peer_percentile, "
                " evidence_url) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    _CYCLE, kind, f"PROBE-{kind}",
                    "provider_excluded_billing",
                    1, 5, f"kind={kind}", 0.9, "/probe",
                ),
            )
            cur.execute(
                "DELETE FROM derived.fraud_signal_observation "
                "WHERE entity_id = %s",
                (f"PROBE-{kind}",),
            )
    prov_db.commit()


def test_entity_kind_check_still_rejects_unknown(
    prov_db: psycopg.Connection,
) -> None:
    import psycopg.errors
    with prov_db.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            "INSERT INTO derived.fraud_signal_observation "
            "(cycle, entity_kind, entity_id, signal_id, "
            " raw_value, severity, peer_bucket, peer_percentile, evidence_url) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                _CYCLE, "made_up_kind", "PROBE-INVALID",
                "provider_excluded_billing",
                1, 5, "kind=made_up_kind", 0.9, "/probe",
            ),
        )


# ============================================================================
# 2. Refresher: empty-substrate behavior
# ============================================================================


def test_refresher_zero_on_empty_substrate(prov_db: psycopg.Connection) -> None:
    assert _refresh(prov_db) == 0


def test_refresher_zero_when_cms_empty(prov_db: psycopg.Connection) -> None:
    """LEIE has the excluded NPI but no CMS billing -> 0."""
    _seed_leie(prov_db, record_hash="a" * 64)
    prov_db.commit()
    assert _refresh(prov_db) == 0


def test_refresher_zero_when_leie_empty(prov_db: psycopg.Connection) -> None:
    """CMS has the prescriber but no exclusion -> 0."""
    _seed_prescriber(prov_db, npi=_NPI)
    prov_db.commit()
    assert _refresh(prov_db) == 0


# ============================================================================
# 3. Refresher: the match + its fields
# ============================================================================


def test_refresher_emits_observation_on_npi_match(
    prov_db: psycopg.Connection,
) -> None:
    """Excluded NPI present in Part D within the exclusion window -> one row
    with the expected fields and raw_value = gross drug cost."""
    _seed_leie(prov_db, record_hash="b" * 64, npi=_NPI, excldate="20200115")
    _seed_prescriber(prov_db, npi=_NPI, tot_drug_cst=123456.78)
    # Four clean prescribers so the bucket is 5 and percentile is non-zero.
    for i in range(1, 5):
        _seed_prescriber(prov_db, npi=f"900000000{i}", state="NJ",
                         tot_drug_cst=1000.0)
    prov_db.commit()

    assert _refresh(prov_db) == 1

    with prov_db.cursor() as cur:
        cur.execute(
            "SELECT cycle, entity_kind, entity_id, signal_id, severity, "
            "       peer_bucket, raw_value, peer_percentile "
            "FROM derived.fraud_signal_observation "
            "WHERE signal_id = 'provider_excluded_billing'"
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    cycle, kind, eid, sig, sev, bucket, raw_value, pctile = rows[0]
    assert (cycle, kind, eid, sig) == (
        _CYCLE, "provider", _NPI, "provider_excluded_billing",
    )
    assert sev == 5
    assert bucket == "kind=provider"
    assert float(raw_value) == pytest.approx(123456.78)
    # 1 match in a 5-prescriber bucket -> 1 - 1/5 = 0.8
    assert float(pctile) == pytest.approx(0.8)


def test_evidence_url_carries_npi_and_leie_hash(
    prov_db: psycopg.Connection,
) -> None:
    _seed_leie(prov_db, record_hash="c" * 64, npi=_NPI)
    _seed_prescriber(prov_db, npi=_NPI)
    prov_db.commit()
    _refresh(prov_db)
    url = _scalar(
        prov_db,
        "SELECT evidence_url FROM derived.fraud_signal_observation "
        "WHERE signal_id = 'provider_excluded_billing'",
    )
    assert _NPI in url
    assert "leie=" + ("c" * 64) in url


# ============================================================================
# 4. Precision guards (the whole point of the signal)
# ============================================================================


def test_exclusion_after_billing_year_is_not_flagged(
    prov_db: psycopg.Connection,
) -> None:
    """DATE GUARD: an exclusion effective AFTER the data year must NOT fire.
    Provider excluded 2025-01; Part D year 2023 -> not yet excluded then."""
    _seed_leie(prov_db, record_hash="d" * 64, npi=_NPI, excldate="20250115")
    _seed_prescriber(prov_db, npi=_NPI, data_year=2023)
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
    _seed_prescriber(prov_db, npi=_NPI, data_year=2023)
    prov_db.commit()
    assert _refresh(prov_db, "2023") == 0


def test_null_and_placeholder_npi_never_match(
    prov_db: psycopg.Connection,
) -> None:
    """An LEIE exclusion with no NPI (or a placeholder) must not join even
    if a same-named prescriber exists."""
    _seed_leie(prov_db, record_hash="f0" + "0" * 62, npi=None)
    _seed_leie(prov_db, record_hash="f1" + "0" * 62, npi="0000000000")
    _seed_prescriber(prov_db, npi="0000000000")  # placeholder both sides
    _seed_prescriber(prov_db, npi=_NPI)
    prov_db.commit()
    assert _refresh(prov_db) == 0


# ============================================================================
# 5. Idempotency + cycle isolation
# ============================================================================


def test_refresher_is_idempotent(prov_db: psycopg.Connection) -> None:
    _seed_leie(prov_db, record_hash="1" * 64, npi=_NPI)
    _seed_prescriber(prov_db, npi=_NPI)
    prov_db.commit()
    n1 = _refresh(prov_db)
    n2 = _refresh(prov_db)
    assert n1 == n2 == 1
    total = _scalar(
        prov_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE signal_id = 'provider_excluded_billing'",
    )
    assert total == 1


def test_refresher_isolates_other_cycles(prov_db: psycopg.Connection) -> None:
    _seed_leie(prov_db, record_hash="2" * 64, npi=_NPI)
    _seed_prescriber(prov_db, npi=_NPI, data_year=2023)
    with prov_db.cursor() as cur:
        cur.execute(
            "INSERT INTO derived.fraud_signal_observation "
            "(cycle, entity_kind, entity_id, signal_id, raw_value, severity, "
            " peer_bucket, peer_percentile, evidence_url) "
            "VALUES ('2099', 'provider', 'PROBE', "
            "'provider_excluded_billing', 1, 5, 'kind=provider', 0.9, "
            "'/probe-2099')"
        )
    prov_db.commit()
    _refresh(prov_db, "2023")
    n = _scalar(
        prov_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle = '2099' AND signal_id = 'provider_excluded_billing'",
    )
    assert n == 1


# ============================================================================
# 6. Master refresher integration
# ============================================================================


def test_master_refresher_includes_provider_signal(
    prov_db: psycopg.Connection,
) -> None:
    _seed_leie(prov_db, record_hash="3" * 64, npi=_NPI)
    _seed_prescriber(prov_db, npi=_NPI, data_year=2023)
    prov_db.commit()
    _scalar(
        prov_db,
        "SELECT derived.refresh_all_fraud_signal_observations(%s)",
        "2023",
    )
    n = _scalar(
        prov_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle = '2023' AND signal_id = 'provider_excluded_billing'",
    )
    assert n == 1, (
        "master refresher must invoke provider_excluded_billing; the master "
        "is the single source of truth for which signals exist"
    )


# ============================================================================
# 7. Reference data + evidence-card view
# ============================================================================


def test_severity_calibration_row(prov_db: psycopg.Connection) -> None:
    with prov_db.cursor() as cur:
        cur.execute(
            "SELECT severity_level, calibration_basis "
            "FROM ref.fraud_signal_severity_calibration "
            "WHERE signal_id = 'provider_excluded_billing'"
        )
        row = cur.fetchone()
    assert row is not None, "seed 041 did not insert severity calibration row"
    assert row == (5, "oig_report")


def test_human_explanation_row(prov_db: psycopg.Connection) -> None:
    with prov_db.cursor() as cur:
        cur.execute(
            "SELECT citation_authority, citation_section "
            "FROM ref.fraud_signal_human_explanation "
            "WHERE signal_id = 'provider_excluded_billing'"
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
            "WHERE signal_id = 'provider_excluded_billing'"
        )
        row = cur.fetchone()
    assert row is not None
    url, label, source = row
    assert url.startswith("https://exclusions.oig.hhs.gov")
    assert label == "Search OIG LEIE"
    assert source == "OIG.gov"


def test_evidence_view_resolves_provider_name_and_is_nj(
    prov_db: psycopg.Connection,
) -> None:
    """v_entity_fraud_evidence must resolve display_name to the CMS
    prescriber name and is_nj from the prescriber's practice state, and
    render the explanation with no token residue."""
    _seed_leie(prov_db, record_hash="8" * 64, npi=_NPI)
    _seed_prescriber(
        prov_db, npi=_NPI, last_org="DOE", first="JANE", state="NJ",
    )
    prov_db.commit()
    _refresh(prov_db)

    with prov_db.cursor() as cur:
        cur.execute(
            "SELECT display_name, is_nj, severity, rendered_explanation, "
            "       upstream_verify_url, citation_authority "
            "FROM derived.v_entity_fraud_evidence "
            "WHERE signal_id = 'provider_excluded_billing'"
        )
        row = cur.fetchone()
    assert row is not None, "v_entity_fraud_evidence yielded no row"
    display_name, is_nj, severity, explanation, upstream, citation = row
    assert display_name == "JANE DOE"
    assert is_nj is True
    assert severity == 5
    assert upstream.startswith("https://exclusions.oig.hhs.gov")
    assert citation == "HHS-OIG"
    import re
    assert re.findall(r"\{\{[^}]+\}\}", explanation) == [], (
        f"unsubstituted tokens in rendered_explanation: {explanation!r}"
    )


def test_evidence_view_is_nj_false_for_out_of_state_provider(
    prov_db: psycopg.Connection,
) -> None:
    _seed_leie(prov_db, record_hash="9" * 64, npi=_NPI)
    _seed_prescriber(prov_db, npi=_NPI, state="TX")
    prov_db.commit()
    _refresh(prov_db)
    is_nj = _scalar(
        prov_db,
        "SELECT is_nj FROM derived.v_entity_fraud_evidence "
        "WHERE signal_id = 'provider_excluded_billing'",
    )
    assert is_nj is False


# ============================================================================
# 8. Provenance
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
    assert eff_date.isoformat() == "2026-06-08"
    assert "provider_excluded_billing" in desc
