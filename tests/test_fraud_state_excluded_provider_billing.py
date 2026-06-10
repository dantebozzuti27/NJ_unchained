"""Live-PG regression tests for migrations 104 + 105 + seed 043.

VISION_2026 Pillar 2 (civic integrity) FRAUD-F7 Phase-3 (NJ state x CMS).

The signal under test -- state_excluded_provider_billing -- is the
state-exclusion mirror of provider_excluded_billing (mig 101). It joins a
currently-active NJ Medicaid/OSC exclusion (carrying a real NPI) against
the combined CMS Medicare biller population (Part D prescriber OR Part B
practitioner) for the cycle data year, on an EXACT NPI.

What this module pins:
    * Refresher derived.refresh_signal_state_excluded_provider_billing:
        - empty substrate / exclusion-only / billing-only -> 0, no error
        - exact-NPI match against Part D billing -> one row, raw_value =
          Part D drug cost
        - exact-NPI match against Part B billing -> one row, raw_value =
          Part B Medicare paid
        - match against BOTH -> raw_value = combined exposure (summed)
        - peer_percentile rate-based binary over the COMBINED biller
          population (distinct NPIs across Part D + Part B)
        - ACTIVE-VIEW TRIPWIRE: an exclusion that has dropped out of recent
          pulls (stale last_seen_at) is NOT flagged
        - NPI hygiene: NULL / placeholder NPIs never match
        - idempotency + cycle isolation
    * Master refresher invokes the new refresher (single source of truth).
    * Reference data: severity 4 / state_exclusion; NJ-OSC citation; NJ.gov
      verify URL; new state_exclusion signal_family.
    * Evidence-card view resolves display_name / is_nj for a Part-D-billing
      provider and renders the explanation with no token residue.
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


EXPECTED_FORMULA_VERSION = "2.8.5-fraud-state-excluded-provider-billing-v1"
_DATA_YEAR = 2023
_CYCLE = str(_DATA_YEAR)  # CHAR(4) cycle == CMS data_year
_NPI = "1234567893"


# ============================================================================
# Fixtures + helpers
# ============================================================================


@pytest.fixture
def sx_db(live_pg: psycopg.Connection) -> psycopg.Connection:
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


def _seed_nj_exclusion(
    conn: psycopg.Connection,
    *,
    record_hash: str,
    npi: str | None = _NPI,
    full_name: str = "DOE, JANE",
    stale: bool = False,
) -> None:
    """Seed one NJ Medicaid exclusion row (bypassing the ingester).

    stale=True backdates last_seen_at 30 days so it falls out of
    derived.v_nj_medicaid_exclusion_active (which keeps rows within 7 days
    of the freshest pull).
    """
    last_seen = "now() - INTERVAL '30 days'" if stale else "now()"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.nj_medicaid_exclusion ("
            " record_hash, full_name, npi, address, city, state, zip, "
            " action, effective_date, expiration_date, "
            " source_url, source_sha256, source_vintage, last_seen_at"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            f"          {last_seen})",
            (
                record_hash, full_name, npi, "1 PARK AVE", "NEWARK", "NJ",
                "07102", None, "2021", None,
                "https://example.test/nj.csv", "0" * 64, "local",
            ),
        )


def _seed_partd(
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


def _seed_partb(
    conn: psycopg.Connection,
    *,
    npi: str,
    data_year: int = _DATA_YEAR,
    state: str = "NJ",
    tot_mdcr_pymt_amt: float | None = 50000.0,
) -> None:
    """Seed one CMS Part B (Physician & Other Practitioners) row."""
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
                data_year, npi, "DOE", "JANE",
                "NEWARK", state, "Internal Medicine",
                40, 200, 80000.0, tot_mdcr_pymt_amt, 90000.0, 1.2,
                "https://example.test/partb.csv", "0" * 64, "CY2023",
            ),
        )


def _refresh(conn: psycopg.Connection, cycle: str = _CYCLE) -> object:
    return _scalar(
        conn,
        "SELECT derived.refresh_signal_state_excluded_provider_billing(%s)",
        cycle,
    )


# ============================================================================
# 1. Config: new family + threshold
# ============================================================================


def test_signal_config_row_uses_state_exclusion_family(
    sx_db: psycopg.Connection,
) -> None:
    with sx_db.cursor() as cur:
        cur.execute(
            "SELECT signal_family, min_actionable_threshold "
            "FROM derived.fraud_signal_config "
            "WHERE signal_id = 'state_excluded_provider_billing'"
        )
        row = cur.fetchone()
    assert row is not None, "mig 105 did not seed the fraud_signal_config row"
    assert row[0] == "state_exclusion"
    assert float(row[1]) == 0.0


# ============================================================================
# 2. Refresher: empty / one-sided substrate
# ============================================================================


def test_refresher_zero_on_empty_substrate(sx_db: psycopg.Connection) -> None:
    assert _refresh(sx_db) == 0


def test_refresher_zero_when_billing_empty(sx_db: psycopg.Connection) -> None:
    """NJ exclusion present but no CMS billing -> 0."""
    _seed_nj_exclusion(sx_db, record_hash="a" * 64)
    sx_db.commit()
    assert _refresh(sx_db) == 0


def test_refresher_zero_when_exclusion_empty(
    sx_db: psycopg.Connection,
) -> None:
    """CMS billing present but no NJ exclusion -> 0."""
    _seed_partd(sx_db, npi=_NPI)
    sx_db.commit()
    assert _refresh(sx_db) == 0


# ============================================================================
# 3. Refresher: the match + its fields (Part D, Part B, both)
# ============================================================================


def test_match_on_part_d_billing(sx_db: psycopg.Connection) -> None:
    """Excluded NPI billing Part D -> one row, raw_value = drug cost,
    severity 4, bucket kind=provider, percentile over combined population."""
    _seed_nj_exclusion(sx_db, record_hash="b" * 64, npi=_NPI)
    _seed_partd(sx_db, npi=_NPI, tot_drug_cst=123456.78)
    # Four clean Part D prescribers -> combined population 5 -> percentile 0.8.
    for i in range(1, 5):
        _seed_partd(sx_db, npi=f"900000000{i}", tot_drug_cst=1000.0)
    sx_db.commit()

    assert _refresh(sx_db) == 1

    with sx_db.cursor() as cur:
        cur.execute(
            "SELECT cycle, entity_kind, entity_id, signal_id, severity, "
            "       peer_bucket, raw_value, peer_percentile "
            "FROM derived.fraud_signal_observation "
            "WHERE signal_id = 'state_excluded_provider_billing'"
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    cycle, kind, eid, sig, sev, bucket, raw_value, pctile = rows[0]
    assert (cycle, kind, eid, sig) == (
        _CYCLE, "provider", _NPI, "state_excluded_provider_billing",
    )
    assert sev == 4
    assert bucket == "kind=provider"
    assert float(raw_value) == pytest.approx(123456.78)
    assert float(pctile) == pytest.approx(0.8)


def test_match_on_part_b_billing(sx_db: psycopg.Connection) -> None:
    """Excluded NPI billing ONLY Part B -> one row, raw_value = Part B paid."""
    _seed_nj_exclusion(sx_db, record_hash="c" * 64, npi=_NPI)
    _seed_partb(sx_db, npi=_NPI, tot_mdcr_pymt_amt=50000.0)
    sx_db.commit()

    assert _refresh(sx_db) == 1
    raw_value = _scalar(
        sx_db,
        "SELECT raw_value FROM derived.fraud_signal_observation "
        "WHERE signal_id = 'state_excluded_provider_billing'",
    )
    assert float(raw_value) == pytest.approx(50000.0)


def test_match_on_both_sums_exposure(sx_db: psycopg.Connection) -> None:
    """A provider billing BOTH Part D and Part B -> one row, raw_value =
    combined exposure; the NPI is counted ONCE in the population."""
    _seed_nj_exclusion(sx_db, record_hash="d" * 64, npi=_NPI)
    _seed_partd(sx_db, npi=_NPI, tot_drug_cst=100000.0)
    _seed_partb(sx_db, npi=_NPI, tot_mdcr_pymt_amt=50000.0)
    sx_db.commit()

    assert _refresh(sx_db) == 1
    with sx_db.cursor() as cur:
        cur.execute(
            "SELECT raw_value, peer_percentile "
            "FROM derived.fraud_signal_observation "
            "WHERE signal_id = 'state_excluded_provider_billing'"
        )
        raw_value, pctile = cur.fetchone()
    assert float(raw_value) == pytest.approx(150000.0)
    # Combined population is a single distinct NPI -> 1 - 1/1 = 0.
    assert float(pctile) == pytest.approx(0.0)


def test_evidence_url_carries_npi_and_nj_hash(
    sx_db: psycopg.Connection,
) -> None:
    _seed_nj_exclusion(sx_db, record_hash="e" * 64, npi=_NPI)
    _seed_partd(sx_db, npi=_NPI)
    sx_db.commit()
    _refresh(sx_db)
    url = _scalar(
        sx_db,
        "SELECT evidence_url FROM derived.fraud_signal_observation "
        "WHERE signal_id = 'state_excluded_provider_billing'",
    )
    assert _NPI in url
    assert "njx=" + ("e" * 64) in url


# ============================================================================
# 4. Precision guards
# ============================================================================


def test_stale_exclusion_not_flagged(sx_db: psycopg.Connection) -> None:
    """ACTIVE-VIEW TRIPWIRE: an exclusion that dropped out of recent pulls
    (stale last_seen_at, with a fresher row setting the high-water mark) is
    NOT in v_nj_medicaid_exclusion_active and must not fire."""
    _seed_nj_exclusion(sx_db, record_hash="f0" + "0" * 62, npi=_NPI,
                       stale=True)
    # A fresh, unrelated exclusion sets MAX(last_seen_at)=now() so the stale
    # target falls outside the 7-day active window.
    _seed_nj_exclusion(sx_db, record_hash="f1" + "0" * 62, npi="9999999999")
    _seed_partd(sx_db, npi=_NPI)
    sx_db.commit()
    assert _refresh(sx_db) == 0


def test_null_and_placeholder_npi_never_match(
    sx_db: psycopg.Connection,
) -> None:
    """An NJ exclusion with no NPI (or a placeholder) must not join even if a
    same-named biller exists."""
    _seed_nj_exclusion(sx_db, record_hash="a0" + "0" * 62, npi=None)
    _seed_nj_exclusion(sx_db, record_hash="a1" + "0" * 62, npi="0000000000")
    _seed_partd(sx_db, npi="0000000000")  # placeholder both sides
    _seed_partd(sx_db, npi=_NPI)
    sx_db.commit()
    assert _refresh(sx_db) == 0


# ============================================================================
# 5. Idempotency + cycle isolation
# ============================================================================


def test_refresher_is_idempotent(sx_db: psycopg.Connection) -> None:
    _seed_nj_exclusion(sx_db, record_hash="1" * 64, npi=_NPI)
    _seed_partd(sx_db, npi=_NPI)
    sx_db.commit()
    n1 = _refresh(sx_db)
    n2 = _refresh(sx_db)
    assert n1 == n2 == 1
    total = _scalar(
        sx_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE signal_id = 'state_excluded_provider_billing'",
    )
    assert total == 1


def test_refresher_isolates_other_cycles(sx_db: psycopg.Connection) -> None:
    _seed_nj_exclusion(sx_db, record_hash="2" * 64, npi=_NPI)
    _seed_partd(sx_db, npi=_NPI, data_year=2023)
    with sx_db.cursor() as cur:
        cur.execute(
            "INSERT INTO derived.fraud_signal_observation "
            "(cycle, entity_kind, entity_id, signal_id, raw_value, severity, "
            " peer_bucket, peer_percentile, evidence_url) "
            "VALUES ('2099', 'provider', 'PROBE', "
            "'state_excluded_provider_billing', 1, 4, 'kind=provider', 0.9, "
            "'/probe-2099')"
        )
    sx_db.commit()
    _refresh(sx_db, "2023")
    n = _scalar(
        sx_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle = '2099' AND signal_id = "
        "'state_excluded_provider_billing'",
    )
    assert n == 1


# ============================================================================
# 6. Master refresher integration
# ============================================================================


def test_master_refresher_includes_signal(sx_db: psycopg.Connection) -> None:
    _seed_nj_exclusion(sx_db, record_hash="3" * 64, npi=_NPI)
    _seed_partd(sx_db, npi=_NPI, data_year=2023)
    sx_db.commit()
    _scalar(
        sx_db,
        "SELECT derived.refresh_all_fraud_signal_observations(%s)",
        "2023",
    )
    n = _scalar(
        sx_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle = '2023' AND signal_id = "
        "'state_excluded_provider_billing'",
    )
    assert n == 1, (
        "master refresher must invoke state_excluded_provider_billing; the "
        "master is the single source of truth for which signals exist"
    )


# ============================================================================
# 7. Reference data + evidence-card view
# ============================================================================


def test_severity_calibration_row(sx_db: psycopg.Connection) -> None:
    with sx_db.cursor() as cur:
        cur.execute(
            "SELECT severity_level, calibration_basis "
            "FROM ref.fraud_signal_severity_calibration "
            "WHERE signal_id = 'state_excluded_provider_billing'"
        )
        row = cur.fetchone()
    assert row is not None, "seed 043 did not insert severity calibration row"
    assert row == (4, "state_exclusion")


def test_human_explanation_row(sx_db: psycopg.Connection) -> None:
    with sx_db.cursor() as cur:
        cur.execute(
            "SELECT citation_authority, citation_section "
            "FROM ref.fraud_signal_human_explanation "
            "WHERE signal_id = 'state_excluded_provider_billing'"
        )
        row = cur.fetchone()
    assert row is not None
    auth, section = row
    assert auth == "NJ-OSC"
    assert "30:4D" in section


def test_evidence_url_template_row(sx_db: psycopg.Connection) -> None:
    with sx_db.cursor() as cur:
        cur.execute(
            "SELECT url_template, button_label, upstream_source "
            "FROM ref.fraud_signal_evidence_url_template "
            "WHERE signal_id = 'state_excluded_provider_billing'"
        )
        row = cur.fetchone()
    assert row is not None
    url, label, source = row
    assert url.startswith("https://nj.gov/comptroller")
    assert source == "NJ.gov"
    assert 4 <= len(label) <= 60


def test_evidence_view_resolves_provider_name_and_is_nj(
    sx_db: psycopg.Connection,
) -> None:
    """For a Part-D-billing NJ provider, v_entity_fraud_evidence resolves
    display_name + is_nj and renders the explanation with no token residue."""
    _seed_nj_exclusion(sx_db, record_hash="8" * 64, npi=_NPI)
    _seed_partd(sx_db, npi=_NPI, last_org="DOE", first="JANE", state="NJ")
    sx_db.commit()
    _refresh(sx_db)

    with sx_db.cursor() as cur:
        cur.execute(
            "SELECT display_name, is_nj, severity, rendered_explanation, "
            "       upstream_verify_url, citation_authority "
            "FROM derived.v_entity_fraud_evidence "
            "WHERE signal_id = 'state_excluded_provider_billing'"
        )
        row = cur.fetchone()
    assert row is not None, "v_entity_fraud_evidence yielded no row"
    display_name, is_nj, severity, explanation, upstream, citation = row
    assert display_name == "JANE DOE"
    assert is_nj is True
    assert severity == 4
    assert upstream.startswith("https://nj.gov/comptroller")
    assert citation == "NJ-OSC"
    import re
    assert re.findall(r"\{\{[^}]+\}\}", explanation) == [], (
        f"unsubstituted tokens in rendered_explanation: {explanation!r}"
    )


# ============================================================================
# 8. Provenance
# ============================================================================


def test_formula_version_registered(sx_db: psycopg.Connection) -> None:
    with sx_db.cursor() as cur:
        cur.execute(
            "SELECT effective_date, description "
            "FROM ref.formula_version WHERE formula_version = %s",
            (EXPECTED_FORMULA_VERSION,),
        )
        row = cur.fetchone()
    assert row is not None
    eff_date, desc = row
    assert eff_date.isoformat() == "2026-06-09"
    assert "state_excluded_provider_billing" in desc
