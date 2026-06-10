"""Live-PG regression tests for migration 110 + seed 048.

VISION_2026 Pillar 2 (civic integrity) FRAUD-F7 Phase-3 (NPPES identity spine).

name_resolved_excluded_provider_billing is the recall payoff of the NPPES
substrate: a name-only HHS-OIG LEIE individual exclusion (BLANK NPI) resolved
via NPPES -- by UNIQUE canonical LAST|FIRST + practice state -- to a concrete
NPI that is present in CMS Medicare billing (Part D or Part B) within the
exclusion window. Inferred identity => severity 3, basis inferred_identity.

What this module pins:
    * derived.v_leie_name_resolved_npi resolution view:
        - name-only LEIE + unique NPPES name+state -> resolved
        - AMBIGUITY GUARD: two NPPES providers sharing name+state -> NOT
          resolved (no guessing)
        - STATE GUARD: name match in a different state -> NOT resolved
        - LEIE rows that ALREADY carry a real NPI are excluded (those are
          the exact-match signals' job)
    * refresher: empty -> 0; resolved NPI billing Part D/Part B/both fires;
      date + reinstatement guards; idempotency + cycle isolation.
    * severity 3 / inferred_identity; HHS-OIG citation; OIG verify URL.
    * evidence view resolves display_name from CMS, renders with no residue.
    * master refresher invokes it; formula_version registered.
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


EXPECTED_FORMULA_VERSION = "2.9.0-fraud-name-resolved-excluded-provider-billing-v1"
_DATA_YEAR = 2023
_CYCLE = str(_DATA_YEAR)
_NPI = "1234567893"
_SIGNAL = "name_resolved_excluded_provider_billing"


@pytest.fixture
def nr_db(live_pg: psycopg.Connection) -> psycopg.Connection:
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
    npi: str | None = None,          # default: NAME-ONLY (the recall case)
    lastname: str = "DOE",
    firstname: str = "JANE",
    excldate: str = "20200115",
    reindate: str | None = None,
    state: str | None = "NJ",
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.hhs_oig_leie ("
            " record_hash, lastname, firstname, midname, busname, "
            " npi, excltype, excldate, reindate, state, "
            " vintage_month, source_url, source_sha256"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                record_hash, lastname, firstname, None, None,
                npi, "1128A1", excldate, reindate, state,
                "2026-03", "https://example.test/UPDATED.csv", "0" * 64,
            ),
        )


def _seed_nppes(
    conn: psycopg.Connection,
    *,
    npi: str,
    last: str = "DOE",
    first: str = "JANE",
    state: str = "NJ",
    entity_type_code: int = 1,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.nppes_provider ("
            " npi, entity_type_code, provider_last_name, provider_first_name, "
            " provider_org_name, practice_city, practice_state, practice_zip5, "
            " taxonomy_code_1, deactivation_date, "
            " source_url, source_sha256, source_vintage"
            ") VALUES (%s, %s, %s, %s, NULL, 'NEWARK', %s, '08608', "
            "          '207Q00000X', NULL, "
            "          'https://example.test/nppes.zip', %s, "
            "          '20260601-20260601')",
            (npi, entity_type_code, last, first, state, "0" * 64),
        )


def _seed_partd(
    conn: psycopg.Connection,
    *,
    npi: str,
    data_year: int = _DATA_YEAR,
    tot_drug_cst: float | None = 50000.0,
    state: str = "NJ",
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.cms_partd_prescriber ("
            " data_year, npi, prscrbr_last_org_name, prscrbr_first_name, "
            " prscrbr_city, prscrbr_state_abrvtn, prscrbr_type, "
            " tot_clms, tot_drug_cst, tot_benes, "
            " opioid_tot_clms, opioid_prscrbr_rate, "
            " source_url, source_sha256, source_vintage"
            ") VALUES (%s, %s, 'DOE', 'JANE', 'NEWARK', %s, "
            "'Internal Medicine', 100, %s, 50, NULL, NULL, "
            "'https://example.test/partd.csv', %s, 'CY2023')",
            (data_year, npi, state, tot_drug_cst, "0" * 64),
        )


def _seed_partb(
    conn: psycopg.Connection,
    *,
    npi: str,
    data_year: int = _DATA_YEAR,
    tot_mdcr_pymt_amt: float | None = 30000.0,
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
            ") VALUES (%s, %s, 'DOE', 'JANE', 'NEWARK', %s, "
            "'Internal Medicine', 50, 500, 120000, %s, 150000, 1.2, "
            "'https://example.test/partb.csv', %s, 'CY2023')",
            (data_year, npi, state, tot_mdcr_pymt_amt, "0" * 64),
        )


def _refresh(conn: psycopg.Connection, cycle: str = _CYCLE) -> object:
    return _scalar(
        conn,
        "SELECT derived.refresh_signal_name_resolved_excluded_provider_billing(%s)",
        cycle,
    )


def _resolved_npis(conn: psycopg.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT resolved_npi FROM derived.v_leie_name_resolved_npi"
        )
        return {r[0] for r in cur.fetchall()}


# ============================================================================
# 1. Resolution view
# ============================================================================


def test_resolution_unique_name_state_resolves(nr_db: psycopg.Connection) -> None:
    _seed_leie(nr_db, record_hash="a" * 64, npi=None, state="NJ")
    _seed_nppes(nr_db, npi=_NPI, last="DOE", first="JANE", state="NJ")
    nr_db.commit()
    assert _NPI in _resolved_npis(nr_db)


def test_resolution_ambiguous_name_state_is_dropped(
    nr_db: psycopg.Connection,
) -> None:
    """Two NPPES providers sharing canonical name+state -> NO resolution."""
    _seed_leie(nr_db, record_hash="b" * 64, npi=None, state="NJ")
    _seed_nppes(nr_db, npi="1111111111", last="DOE", first="JANE", state="NJ")
    _seed_nppes(nr_db, npi="2222222222", last="DOE", first="JANE", state="NJ")
    nr_db.commit()
    assert _resolved_npis(nr_db) == set()


def test_resolution_state_mismatch_is_dropped(nr_db: psycopg.Connection) -> None:
    _seed_leie(nr_db, record_hash="c" * 64, npi=None, state="NJ")
    _seed_nppes(nr_db, npi=_NPI, last="DOE", first="JANE", state="NY")
    nr_db.commit()
    assert _resolved_npis(nr_db) == set()


def test_resolution_skips_leie_rows_with_real_npi(
    nr_db: psycopg.Connection,
) -> None:
    """An LEIE row that already carries a usable NPI is the exact-match
    signals' job; the name-resolution layer ignores it."""
    _seed_leie(nr_db, record_hash="d" * 64, npi="9999999999", state="NJ")
    _seed_nppes(nr_db, npi=_NPI, last="DOE", first="JANE", state="NJ")
    nr_db.commit()
    assert _resolved_npis(nr_db) == set()


def test_resolution_skips_deactivated_nppes(nr_db: psycopg.Connection) -> None:
    """A deactivated NPPES record is excluded by v_nppes_provider_active."""
    _seed_leie(nr_db, record_hash="e" * 64, npi=None, state="NJ")
    with nr_db.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.nppes_provider ("
            " npi, entity_type_code, provider_last_name, provider_first_name, "
            " provider_org_name, practice_city, practice_state, practice_zip5, "
            " taxonomy_code_1, deactivation_date, "
            " source_url, source_sha256, source_vintage"
            ") VALUES (%s, 1, 'DOE', 'JANE', NULL, 'NEWARK', 'NJ', '08608', "
            "'207Q00000X', '2022-01-01', "
            "'https://example.test/nppes.zip', %s, '20260601-20260601')",
            (_NPI, "0" * 64),
        )
    nr_db.commit()
    assert _resolved_npis(nr_db) == set()


# ============================================================================
# 2. Refresher: matches + fields
# ============================================================================


def test_refresher_zero_on_empty_substrate(nr_db: psycopg.Connection) -> None:
    assert _refresh(nr_db) == 0


def test_refresher_fires_on_partd_billing(nr_db: psycopg.Connection) -> None:
    _seed_leie(nr_db, record_hash="1" * 64, npi=None, state="NJ")
    _seed_nppes(nr_db, npi=_NPI, state="NJ")
    _seed_partd(nr_db, npi=_NPI, tot_drug_cst=50000.0)
    nr_db.commit()
    assert _refresh(nr_db) == 1

    with nr_db.cursor() as cur:
        cur.execute(
            "SELECT entity_kind, entity_id, severity, peer_bucket, raw_value "
            "FROM derived.fraud_signal_observation WHERE signal_id = %s",
            (_SIGNAL,),
        )
        row = cur.fetchone()
    assert row is not None
    kind, eid, sev, bucket, raw_value = row
    assert (kind, eid, sev, bucket) == ("provider", _NPI, 3, "kind=provider")
    assert float(raw_value) == pytest.approx(50000.0)


def test_refresher_combines_partd_and_partb_exposure(
    nr_db: psycopg.Connection,
) -> None:
    _seed_leie(nr_db, record_hash="2" * 64, npi=None, state="NJ")
    _seed_nppes(nr_db, npi=_NPI, state="NJ")
    _seed_partd(nr_db, npi=_NPI, tot_drug_cst=50000.0)
    _seed_partb(nr_db, npi=_NPI, tot_mdcr_pymt_amt=30000.0)
    nr_db.commit()
    _refresh(nr_db)
    raw_value = _scalar(
        nr_db,
        "SELECT raw_value FROM derived.fraud_signal_observation "
        "WHERE signal_id = %s",
        _SIGNAL,
    )
    assert isinstance(raw_value, Decimal)
    assert float(raw_value) == pytest.approx(80000.0)  # 50k Part D + 30k Part B


def test_refresher_fires_on_partb_only(nr_db: psycopg.Connection) -> None:
    _seed_leie(nr_db, record_hash="3" * 64, npi=None, state="NJ")
    _seed_nppes(nr_db, npi=_NPI, state="NJ")
    _seed_partb(nr_db, npi=_NPI, tot_mdcr_pymt_amt=30000.0)
    nr_db.commit()
    assert _refresh(nr_db) == 1


def test_no_fire_when_resolved_npi_not_billing(
    nr_db: psycopg.Connection,
) -> None:
    """Resolved identity but no CMS billing for the cycle -> 0."""
    _seed_leie(nr_db, record_hash="4" * 64, npi=None, state="NJ")
    _seed_nppes(nr_db, npi=_NPI, state="NJ")
    nr_db.commit()
    assert _refresh(nr_db) == 0


def test_evidence_url_carries_npi_and_leie_hash(
    nr_db: psycopg.Connection,
) -> None:
    _seed_leie(nr_db, record_hash="5" * 64, npi=None, state="NJ")
    _seed_nppes(nr_db, npi=_NPI, state="NJ")
    _seed_partd(nr_db, npi=_NPI)
    nr_db.commit()
    _refresh(nr_db)
    url = _scalar(
        nr_db,
        "SELECT evidence_url FROM derived.fraud_signal_observation "
        "WHERE signal_id = %s",
        _SIGNAL,
    )
    assert isinstance(url, str)
    assert _NPI in url
    assert "leie=" + ("5" * 64) in url


# ============================================================================
# 3. Precision guards (date window)
# ============================================================================


def test_exclusion_after_billing_year_is_not_flagged(
    nr_db: psycopg.Connection,
) -> None:
    _seed_leie(nr_db, record_hash="6" * 64, npi=None, excldate="20250115",
               state="NJ")
    _seed_nppes(nr_db, npi=_NPI, state="NJ")
    _seed_partd(nr_db, npi=_NPI, data_year=2023)
    nr_db.commit()
    assert _refresh(nr_db, "2023") == 0


def test_reinstated_before_year_end_is_not_flagged(
    nr_db: psycopg.Connection,
) -> None:
    _seed_leie(nr_db, record_hash="7" * 64, npi=None,
               excldate="20180115", reindate="20220101", state="NJ")
    _seed_nppes(nr_db, npi=_NPI, state="NJ")
    _seed_partd(nr_db, npi=_NPI, data_year=2023)
    nr_db.commit()
    assert _refresh(nr_db, "2023") == 0


def test_ambiguous_resolution_never_fires(nr_db: psycopg.Connection) -> None:
    """End-to-end: an ambiguous name+state never produces a signal even when
    one of the same-named providers is billing."""
    _seed_leie(nr_db, record_hash="8" * 64, npi=None, state="NJ")
    _seed_nppes(nr_db, npi="1111111111", state="NJ")
    _seed_nppes(nr_db, npi="2222222222", state="NJ")
    _seed_partd(nr_db, npi="1111111111")
    nr_db.commit()
    assert _refresh(nr_db) == 0


# ============================================================================
# 4. Idempotency + cycle isolation + master refresher
# ============================================================================


def test_refresher_is_idempotent(nr_db: psycopg.Connection) -> None:
    _seed_leie(nr_db, record_hash="9" * 64, npi=None, state="NJ")
    _seed_nppes(nr_db, npi=_NPI, state="NJ")
    _seed_partd(nr_db, npi=_NPI)
    nr_db.commit()
    n1 = _refresh(nr_db)
    n2 = _refresh(nr_db)
    assert n1 == n2 == 1
    total = _scalar(
        nr_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE signal_id = %s",
        _SIGNAL,
    )
    assert total == 1


def test_master_refresher_includes_signal(nr_db: psycopg.Connection) -> None:
    _seed_leie(nr_db, record_hash="aa" + "0" * 62, npi=None, state="NJ")
    _seed_nppes(nr_db, npi=_NPI, state="NJ")
    _seed_partd(nr_db, npi=_NPI, data_year=2023)
    nr_db.commit()
    _scalar(
        nr_db,
        "SELECT derived.refresh_all_fraud_signal_observations(%s)",
        "2023",
    )
    n = _scalar(
        nr_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle = '2023' AND signal_id = %s",
        _SIGNAL,
    )
    assert n == 1


# ============================================================================
# 5. Reference data + evidence-card view
# ============================================================================


def test_severity_calibration_row(nr_db: psycopg.Connection) -> None:
    with nr_db.cursor() as cur:
        cur.execute(
            "SELECT severity_level, calibration_basis "
            "FROM ref.fraud_signal_severity_calibration WHERE signal_id = %s",
            (_SIGNAL,),
        )
        row = cur.fetchone()
    assert row == (3, "inferred_identity")


def test_evidence_url_template_row(nr_db: psycopg.Connection) -> None:
    with nr_db.cursor() as cur:
        cur.execute(
            "SELECT url_template, upstream_source "
            "FROM ref.fraud_signal_evidence_url_template WHERE signal_id = %s",
            (_SIGNAL,),
        )
        row = cur.fetchone()
    assert row is not None
    url, source = row
    assert url.startswith("https://exclusions.oig.hhs.gov")
    assert source == "OIG.gov"


def test_evidence_view_renders_clean(nr_db: psycopg.Connection) -> None:
    _seed_leie(nr_db, record_hash="bb" + "0" * 62, npi=None, state="NJ")
    _seed_nppes(nr_db, npi=_NPI, state="NJ")
    _seed_partd(nr_db, npi=_NPI, state="NJ")
    nr_db.commit()
    _refresh(nr_db)

    with nr_db.cursor() as cur:
        cur.execute(
            "SELECT display_name, is_nj, severity, rendered_explanation, "
            "       citation_authority "
            "FROM derived.v_entity_fraud_evidence WHERE signal_id = %s",
            (_SIGNAL,),
        )
        row = cur.fetchone()
    assert row is not None
    display_name, is_nj, severity, explanation, citation = row
    assert display_name == "JANE DOE"   # resolved from CMS (provider_meta)
    assert is_nj is True
    assert severity == 3
    assert citation == "HHS-OIG"
    import re
    assert re.findall(r"\{\{[^}]+\}\}", explanation) == [], (
        f"unsubstituted tokens in rendered_explanation: {explanation!r}"
    )


def test_formula_version_registered(nr_db: psycopg.Connection) -> None:
    with nr_db.cursor() as cur:
        cur.execute(
            "SELECT effective_date, description "
            "FROM ref.formula_version WHERE formula_version = %s",
            (EXPECTED_FORMULA_VERSION,),
        )
        row = cur.fetchone()
    assert row is not None
    eff_date, desc = row
    assert eff_date.isoformat() == "2026-06-09"
    assert "name_resolved_excluded_provider_billing" in desc
