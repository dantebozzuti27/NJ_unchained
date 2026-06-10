"""Live-PG regression tests for migration 111 + seed 049.

VISION_2026 Pillar 2 (civic integrity) FRAUD-F7 (CMS Open Payments).

excluded_provider_received_open_payments is the FIRST signal mined from the
CMS Open Payments substrate (mig 103): an active HHS-OIG LEIE exclusion
(carrying a real NPI) that appears as a covered recipient in CMS Open
Payments General Payments for a program year in which the exclusion was
already in effect. Exact NPI equijoin; conflict-of-interest lead (an
industry transfer of value is NOT a federal payment, so this is NOT a 42 USC
1320a-7a payment-prohibition breach) -> severity 3; raw_value = total
payment_amount received.

This module ALSO pins the mig-111 widening of provider_meta in
v_entity_fraud_evidence: an Open-Payments-ONLY recipient (excluded, not
billing Medicare -- the natural case for this signal) must now resolve a real
display_name (from cms_open_payments_general) and is_nj from the recipient's
state, while Part D / Part B still win when present.
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


EXPECTED_FORMULA_VERSION = "2.9.1-fraud-excluded-provider-received-open-payments-v1"
_PROGRAM_YEAR = 2023
_CYCLE = str(_PROGRAM_YEAR)
_NPI = "1234567893"
_SIGNAL = "excluded_provider_received_open_payments"


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


def _seed_open_payment(
    conn: psycopg.Connection,
    *,
    record_id: str,
    npi: str,
    program_year: int = _PROGRAM_YEAR,
    first: str = "JANE",
    last: str = "DOE",
    state: str = "NJ",
    payment_amount: float | None = 5000.0,
) -> None:
    """Seed one CMS Open Payments General-Payments row (bypassing ingester)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.cms_open_payments_general ("
            " record_id, program_year, covered_recipient_npi, "
            " covered_recipient_profile_id, recipient_first_name, "
            " recipient_last_name, recipient_state, payer_name, "
            " payment_amount, payment_date, nature_of_payment, product_name, "
            " source_url, source_sha256, source_vintage"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "          %s, %s, %s)",
            (
                record_id, program_year, npi, "PROF-" + npi, first, last,
                state, "BigPharma Inc", payment_amount, "2023-06-15",
                "Consulting Fee", "DrugX",
                "https://example.test/op.csv", "0" * 64, "PY2023",
            ),
        )


def _refresh(conn: psycopg.Connection, cycle: str = _CYCLE) -> object:
    return _scalar(
        conn,
        "SELECT derived.refresh_signal_excluded_provider_received_open_payments(%s)",
        cycle,
    )


# ============================================================================
# 1. Empty-substrate behavior
# ============================================================================


def test_refresher_zero_on_empty_substrate(op_db: psycopg.Connection) -> None:
    assert _refresh(op_db) == 0


def test_refresher_zero_when_open_payments_empty(
    op_db: psycopg.Connection,
) -> None:
    """LEIE has the excluded NPI but no Open Payments record -> 0."""
    _seed_leie(op_db, record_hash="a" * 64)
    op_db.commit()
    assert _refresh(op_db) == 0


def test_refresher_zero_when_leie_empty(op_db: psycopg.Connection) -> None:
    """Open Payments has the recipient but no exclusion -> 0."""
    _seed_open_payment(op_db, record_id="r1", npi=_NPI)
    op_db.commit()
    assert _refresh(op_db) == 0


# ============================================================================
# 2. The match + its fields
# ============================================================================


def test_refresher_emits_observation_on_npi_match(
    op_db: psycopg.Connection,
) -> None:
    """Excluded NPI present in Open Payments within the exclusion window ->
    one row with the expected fields and severity 3."""
    _seed_leie(op_db, record_hash="b" * 64, npi=_NPI, excldate="20200115")
    _seed_open_payment(op_db, record_id="m0", npi=_NPI, payment_amount=5000.0)
    for i in range(1, 5):
        _seed_open_payment(
            op_db, record_id=f"m{i}", npi=f"900000000{i}", state="NJ",
            payment_amount=100.0,
        )
    op_db.commit()

    assert _refresh(op_db) == 1

    with op_db.cursor() as cur:
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
    assert sev == 3
    assert bucket == "kind=provider"
    assert float(raw_value) == pytest.approx(5000.0)
    # 1 match in a 5-recipient bucket -> 1 - 1/5 = 0.8
    assert float(pctile) == pytest.approx(0.8)


def test_raw_value_sums_multiple_payments(
    op_db: psycopg.Connection,
) -> None:
    """raw_value aggregates ALL transfers of value to the recipient in the
    program year, not just the first record."""
    _seed_leie(op_db, record_hash="c" * 64, npi=_NPI)
    _seed_open_payment(op_db, record_id="s1", npi=_NPI, payment_amount=1000.0)
    _seed_open_payment(op_db, record_id="s2", npi=_NPI, payment_amount=2500.0)
    _seed_open_payment(op_db, record_id="s3", npi=_NPI, payment_amount=None)
    op_db.commit()
    assert _refresh(op_db) == 1
    raw_value = _scalar(
        op_db,
        "SELECT raw_value FROM derived.fraud_signal_observation "
        "WHERE signal_id = %s",
        _SIGNAL,
    )
    assert isinstance(raw_value, Decimal)
    assert float(raw_value) == pytest.approx(3500.0)  # NULL -> 0


def test_evidence_url_carries_npi_and_leie_hash(
    op_db: psycopg.Connection,
) -> None:
    _seed_leie(op_db, record_hash="d" * 64, npi=_NPI)
    _seed_open_payment(op_db, record_id="u1", npi=_NPI)
    op_db.commit()
    _refresh(op_db)
    url = _scalar(
        op_db,
        "SELECT evidence_url FROM derived.fraud_signal_observation "
        "WHERE signal_id = %s",
        _SIGNAL,
    )
    assert isinstance(url, str)
    assert _NPI in url
    assert "leie=" + ("d" * 64) in url


# ============================================================================
# 3. Precision guards
# ============================================================================


def test_exclusion_after_program_year_is_not_flagged(
    op_db: psycopg.Connection,
) -> None:
    """DATE GUARD: an exclusion effective AFTER the program year must NOT fire."""
    _seed_leie(op_db, record_hash="e" * 64, npi=_NPI, excldate="20250115")
    _seed_open_payment(op_db, record_id="g1", npi=_NPI, program_year=2023)
    op_db.commit()
    assert _refresh(op_db, "2023") == 0


def test_reinstated_before_year_end_is_not_flagged(
    op_db: psycopg.Connection,
) -> None:
    """REINSTATEMENT GUARD: excluded 2018, reinstated 2022, paid 2023 ->
    no longer excluded during 2023 -> not flagged."""
    _seed_leie(
        op_db, record_hash="f" * 64, npi=_NPI,
        excldate="20180115", reindate="20220101",
    )
    _seed_open_payment(op_db, record_id="g2", npi=_NPI, program_year=2023)
    op_db.commit()
    assert _refresh(op_db, "2023") == 0


def test_null_and_placeholder_npi_never_match(
    op_db: psycopg.Connection,
) -> None:
    """An LEIE exclusion with no NPI (or a placeholder) must not join."""
    _seed_leie(op_db, record_hash="00" + "a" * 62, npi=None)
    _seed_leie(op_db, record_hash="01" + "a" * 62, npi="0000000000")
    _seed_open_payment(op_db, record_id="p0", npi="0000000000")
    _seed_open_payment(op_db, record_id="p1", npi=_NPI)
    op_db.commit()
    assert _refresh(op_db) == 0


# ============================================================================
# 4. Idempotency + cycle isolation
# ============================================================================


def test_refresher_is_idempotent(op_db: psycopg.Connection) -> None:
    _seed_leie(op_db, record_hash="1" * 64, npi=_NPI)
    _seed_open_payment(op_db, record_id="i1", npi=_NPI)
    op_db.commit()
    n1 = _refresh(op_db)
    n2 = _refresh(op_db)
    assert n1 == n2 == 1
    total = _scalar(
        op_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE signal_id = %s",
        _SIGNAL,
    )
    assert total == 1


def test_refresher_isolates_other_cycles(op_db: psycopg.Connection) -> None:
    _seed_leie(op_db, record_hash="2" * 64, npi=_NPI)
    _seed_open_payment(op_db, record_id="c1", npi=_NPI, program_year=2023)
    with op_db.cursor() as cur:
        cur.execute(
            "INSERT INTO derived.fraud_signal_observation "
            "(cycle, entity_kind, entity_id, signal_id, raw_value, severity, "
            " peer_bucket, peer_percentile, evidence_url) "
            "VALUES ('2099', 'provider', 'PROBE', %s, 1, 3, 'kind=provider', "
            "0.9, '/probe-2099')",
            (_SIGNAL,),
        )
    op_db.commit()
    _refresh(op_db, "2023")
    n = _scalar(
        op_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle = '2099' AND signal_id = %s",
        _SIGNAL,
    )
    assert n == 1


# ============================================================================
# 5. Master refresher integration
# ============================================================================


def test_master_refresher_includes_open_payments_signal(
    op_db: psycopg.Connection,
) -> None:
    _seed_leie(op_db, record_hash="3" * 64, npi=_NPI)
    _seed_open_payment(op_db, record_id="ma", npi=_NPI, program_year=2023)
    op_db.commit()
    _scalar(
        op_db,
        "SELECT derived.refresh_all_fraud_signal_observations(%s)",
        "2023",
    )
    n = _scalar(
        op_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle = '2023' AND signal_id = %s",
        _SIGNAL,
    )
    assert n == 1, (
        "master refresher must invoke excluded_provider_received_open_payments"
    )


def test_corroborates_billing_signal_for_dual_exposure(
    op_db: psycopg.Connection,
) -> None:
    """An excluded provider both BILLING Part B and RECEIVING Open Payments
    fires BOTH the severity-5 Part-B billing signal and this severity-3
    conflict-of-interest signal (the L3 engine stacks them)."""
    _seed_leie(op_db, record_hash="7" * 64, npi=_NPI)
    _seed_open_payment(op_db, record_id="d1", npi=_NPI, program_year=2023)
    with op_db.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.cms_physician_provider ("
            " data_year, npi, prvdr_last_org_name, prvdr_first_name, "
            " prvdr_city, prvdr_state_abrvtn, prvdr_type, "
            " tot_benes, tot_srvcs, tot_mdcr_alowd_amt, tot_mdcr_pymt_amt, "
            " tot_sbmtd_chrg, bene_avg_risk_scre, "
            " source_url, source_sha256, source_vintage"
            ") VALUES (2023, %s, 'DOE', 'JANE', 'NEWARK', 'NJ', "
            "'Internal Medicine', 50, 500, 120000, 98765.43, 150000, 1.2, "
            "'https://example.test/partb.csv', %s, 'CY2023')",
            (_NPI, "0" * 64),
        )
    op_db.commit()
    _scalar(
        op_db,
        "SELECT derived.refresh_all_fraud_signal_observations(%s)",
        "2023",
    )
    with op_db.cursor() as cur:
        cur.execute(
            "SELECT signal_id FROM derived.fraud_signal_observation "
            "WHERE cycle = '2023' AND entity_id = %s "
            "  AND signal_id IN ('provider_excluded_billing_partb', %s) "
            "ORDER BY signal_id",
            (_NPI, _SIGNAL),
        )
        fired = [r[0] for r in cur.fetchall()]
    assert fired == [_SIGNAL, "provider_excluded_billing_partb"]


# ============================================================================
# 6. Reference data + evidence-card view (incl. mig-111 provider_meta widening)
# ============================================================================


def test_severity_calibration_row(op_db: psycopg.Connection) -> None:
    with op_db.cursor() as cur:
        cur.execute(
            "SELECT severity_level, calibration_basis "
            "FROM ref.fraud_signal_severity_calibration "
            "WHERE signal_id = %s",
            (_SIGNAL,),
        )
        row = cur.fetchone()
    assert row == (3, "oig_report")


def test_human_explanation_row(op_db: psycopg.Connection) -> None:
    with op_db.cursor() as cur:
        cur.execute(
            "SELECT citation_authority, rule_text "
            "FROM ref.fraud_signal_human_explanation "
            "WHERE signal_id = %s",
            (_SIGNAL,),
        )
        row = cur.fetchone()
    assert row is not None
    auth, rule_text = row
    assert auth == "HHS-OIG"
    assert "transfer" in rule_text.lower()


def test_evidence_url_template_row(op_db: psycopg.Connection) -> None:
    with op_db.cursor() as cur:
        cur.execute(
            "SELECT url_template, button_label, upstream_source "
            "FROM ref.fraud_signal_evidence_url_template "
            "WHERE signal_id = %s",
            (_SIGNAL,),
        )
        row = cur.fetchone()
    assert row is not None
    url, label, source = row
    assert url.startswith("https://openpaymentsdata.cms.gov")
    assert label == "Search CMS Open Payments"
    assert source == "CMS.gov"


def test_evidence_view_resolves_open_payments_name_and_is_nj(
    op_db: psycopg.Connection,
) -> None:
    """mig 111 widened provider_meta: an Open-Payments-ONLY excluded provider
    (not billing Medicare) must resolve display_name from
    cms_open_payments_general and is_nj from the recipient state, with no
    token residue in the explanation."""
    _seed_leie(op_db, record_hash="8" * 64, npi=_NPI)
    _seed_open_payment(
        op_db, record_id="e1", npi=_NPI, first="JANE", last="DOE", state="NJ",
    )
    op_db.commit()
    _refresh(op_db)

    with op_db.cursor() as cur:
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
    assert display_name == "JANE DOE"   # resolved from Open Payments (mig 111)
    assert is_nj is True
    assert severity == 3
    assert upstream.startswith("https://openpaymentsdata.cms.gov")
    assert citation == "HHS-OIG"
    import re
    assert re.findall(r"\{\{[^}]+\}\}", explanation) == [], (
        f"unsubstituted tokens in rendered_explanation: {explanation!r}"
    )


def test_evidence_view_prefers_partd_name_over_open_payments(
    op_db: psycopg.Connection,
) -> None:
    """provider_meta preference order: an NPI present in BOTH Part D and Open
    Payments resolves its display_name from Part D (pref=1), not Open Payments
    (pref=3), and still yields exactly one evidence row."""
    _seed_leie(op_db, record_hash="aa" + "0" * 62, npi=_NPI)
    _seed_open_payment(
        op_db, record_id="f1", npi=_NPI, first="OPNAME", last="OPLAST",
    )
    with op_db.cursor() as cur:
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
    op_db.commit()
    _refresh(op_db)

    with op_db.cursor() as cur:
        cur.execute(
            "SELECT display_name FROM derived.v_entity_fraud_evidence "
            "WHERE signal_id = %s AND entity_id = %s",
            (_SIGNAL, _NPI),
        )
        rows = cur.fetchall()
    assert len(rows) == 1, f"expected exactly one evidence row, got {rows}"
    assert rows[0][0] == "NAME PARTD"   # Part D preferred (pref=1)


def test_evidence_view_is_nj_false_for_out_of_state_recipient(
    op_db: psycopg.Connection,
) -> None:
    _seed_leie(op_db, record_hash="9" * 64, npi=_NPI)
    _seed_open_payment(op_db, record_id="x1", npi=_NPI, state="TX")
    op_db.commit()
    _refresh(op_db)
    is_nj = _scalar(
        op_db,
        "SELECT is_nj FROM derived.v_entity_fraud_evidence "
        "WHERE signal_id = %s",
        _SIGNAL,
    )
    assert is_nj is False


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
    assert "excluded_provider_received_open_payments" in desc
