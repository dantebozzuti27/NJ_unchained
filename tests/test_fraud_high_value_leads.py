"""Live-PG regression tests for migrations 112 + 113 and seed 050.

VISION_2026 Pillar 2 (civic integrity) FRAUD-F8: highest-value lead ranking,
reframed (mig 113) to surface UNDETECTED fraud first.

What this module pins:
    * ref.fraud_reportability_channel:
        - EVERY configured signal has a channel row (completeness).
        - reward-share consistency (the CHECK): reward_eligible <=> non-null,
          well-ordered relator band; not eligible <=> both shares NULL.
        - the five dollar-denominated exclusion-billing signals are the
          reward-eligible, raw_value_is_usd tier 1/2 lane; FEC structural
          signals are tier 5 / no bounty.
    * derived.v_high_value_leads (undetected-first, mig 113):
        - enforcement status is the PRIMARY ordering axis: entities with a
          prior-sanction signal (already on an exclusion list = "already
          caught") are demoted BELOW all undetected entities.
        - within the undetected lane, ranking is lexicographic on financial
          scale (COALESCE(peak exposure, provider Medicare volume)) → multi-
          source breadth → severity.
        - provider_scale_usd = peak single-year Medicare volume (Part B
          payment + Part D drug cost) and breaks ties among undetected leads.
        - repeat_violator = a prior-sanction signal recurred across >=2 cycles.
        - multi_source = >=2 distinct signal families.
        - reward band = statutory relator share (15-30%) on PEAK exposure.
        - peak_exposure_usd is NULL for a non-dollar signal.
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

EXPECTED_FORMULA_VERSION = "3.0.0-fraud-high-value-leads-v1"
EXPECTED_FORMULA_VERSION_113 = "3.1.0-fraud-leads-undetected-first-v1"

# Minimal Part B row insert (only NOT NULL columns + the payment we rank on).
_PARTB_INSERT = (
    "INSERT INTO raw.cms_physician_provider "
    "(data_year, npi, prvdr_state_abrvtn, tot_mdcr_pymt_amt, "
    " source_url, source_sha256, source_vintage) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s)"
)
_SHA = "0" * 64

# The five dollar-denominated, reward-eligible exclusion-billing signals.
_USD_REWARD_SIGNALS = {
    "provider_excluded_billing",
    "provider_excluded_billing_partb",
    "state_excluded_provider_billing",
    "name_resolved_excluded_provider_billing",
    "excluded_provider_received_open_payments",
}

_OBS_INSERT = (
    "INSERT INTO derived.fraud_signal_observation "
    "(cycle, entity_kind, entity_id, signal_id, raw_value, severity, "
    " peer_bucket, peer_percentile, evidence_url) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
)


@pytest.fixture
def hv_db(live_pg: psycopg.Connection) -> psycopg.Connection:
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


# ----------------------------------------------------------------------------
# Reference table: completeness + integrity
# ----------------------------------------------------------------------------


def test_formula_version_registered(hv_db: psycopg.Connection) -> None:
    with hv_db.cursor() as cur:
        for fv in (EXPECTED_FORMULA_VERSION, EXPECTED_FORMULA_VERSION_113):
            cur.execute(
                "SELECT 1 FROM ref.formula_version WHERE formula_version = %s",
                (fv,),
            )
            assert cur.fetchone() is not None, fv


def test_every_configured_signal_has_a_channel(hv_db: psycopg.Connection) -> None:
    """No configured signal may be unmapped -- the ranking would silently
    default it to tier 5, hiding a real channel."""
    with hv_db.cursor() as cur:
        cur.execute(
            "SELECT signal_id FROM derived.fraud_signal_config "
            "EXCEPT SELECT signal_id FROM ref.fraud_reportability_channel"
        )
        missing = [r[0] for r in cur.fetchall()]
    assert missing == [], f"signals with no reportability channel: {missing}"


def test_reward_share_consistency(hv_db: psycopg.Connection) -> None:
    """reward_eligible <=> a well-ordered, non-null relator band."""
    with hv_db.cursor() as cur:
        cur.execute(
            "SELECT signal_id, reward_eligible, relator_share_low, "
            "relator_share_high FROM ref.fraud_reportability_channel"
        )
        for sid, eligible, lo, hi in cur.fetchall():
            if eligible:
                assert lo is not None and hi is not None, sid
                assert 0 <= float(lo) <= float(hi) <= 1, sid
            else:
                assert lo is None and hi is None, sid


def test_usd_reward_signals_are_tier_1_or_2(hv_db: psycopg.Connection) -> None:
    with hv_db.cursor() as cur:
        cur.execute(
            "SELECT signal_id, reward_tier, reward_eligible, raw_value_is_usd "
            "FROM ref.fraud_reportability_channel WHERE signal_id = ANY(%s)",
            (list(_USD_REWARD_SIGNALS),),
        )
        rows = cur.fetchall()
    assert len(rows) == len(_USD_REWARD_SIGNALS)
    for sid, tier, eligible, is_usd in rows:
        assert eligible is True, sid
        assert is_usd is True, sid
        assert tier in (1, 2), sid


def test_fec_structural_signals_have_no_bounty(hv_db: psycopg.Connection) -> None:
    with hv_db.cursor() as cur:
        cur.execute(
            "SELECT ch.reward_eligible, ch.reward_tier "
            "FROM ref.fraud_reportability_channel ch "
            "JOIN derived.fraud_signal_config cfg USING (signal_id) "
            "WHERE cfg.signal_family = 'structural'"
        )
        rows = cur.fetchall()
    assert rows, "expected structural signals to exist"
    for eligible, tier in rows:
        assert eligible is False
        assert tier == 5


# ----------------------------------------------------------------------------
# Ranking view: ordering + derived flags
# ----------------------------------------------------------------------------


def _seed_ranking_fixture(conn: psycopg.Connection) -> None:
    """Three entities spanning enforcement status and the tier ladder.

    A (provider AAA): state_excluded_provider_billing in 2022 ($1M) AND 2023
       ($2M) -> ALREADY CAUGHT (prior sanction), repeat (sanction in 2 cycles);
       plus an opioid outlier in 2023 -> a 2nd family -> multi-source.
    B (provider BBB): opioid outlier only -> UNDETECTED, severity 4.
    C (committee CCC): treasurer_concentration -> UNDETECTED, severity 3.

    Undetected-first ranking (mig 113): BBB and CCC (no prior sanction) rank
    above AAA (caught). Between the two undetected entities, scale ties at 0
    (no raw CMS rows), so severity breaks it: BBB (4) before CCC (3).
    """
    rows = [
        ("2022", "provider", "AAA", "state_excluded_provider_billing",
         1_000_000, 4, "kind=provider", 0.99, "/t"),
        ("2023", "provider", "AAA", "state_excluded_provider_billing",
         2_000_000, 4, "kind=provider", 0.99, "/t"),
        ("2023", "provider", "AAA", "opioid_prescribing_outlier",
         80, 4, "specialty=X", 0.99, "/t"),
        ("2023", "provider", "BBB", "opioid_prescribing_outlier",
         50, 4, "specialty=X", 0.95, "/t"),
        ("2024", "committee", "CCC", "treasurer_concentration",
         9, 3, "kind=treasurer", 0.99, "/t"),
    ]
    with conn.cursor() as cur:
        for r in rows:
            cur.execute(_OBS_INSERT, r)
    conn.commit()


def test_ranking_order_and_flags(hv_db: psycopg.Connection) -> None:
    _seed_ranking_fixture(hv_db)
    with hv_db.cursor() as cur:
        cur.execute(
            "SELECT entity_id, best_reward_tier, peak_exposure_usd, "
            "reward_low_usd, reward_high_usd, repeat_violator, multi_source, "
            "has_prior_sanction, lead_rank "
            "FROM derived.v_high_value_leads ORDER BY lead_rank"
        )
        rows = cur.fetchall()

    by_id = {r[0]: r for r in rows}
    assert set(by_id) == {"AAA", "BBB", "CCC"}

    # Undetected-first: the two never-sanctioned entities rank above the caught
    # one; severity breaks the undetected tie (BBB sev 4 > CCC sev 3).
    assert [r[0] for r in rows] == ["BBB", "CCC", "AAA"]

    a = by_id["AAA"]
    assert a[1] == 1                       # best_reward_tier (channel still tier 1)
    assert float(a[2]) == 2_000_000.0      # peak = max single-cycle exposure
    assert float(a[3]) == 2_000_000.0 * 0.15  # reward floor low
    assert float(a[4]) == 2_000_000.0 * 0.30  # reward floor high
    assert a[5] is True                    # repeat_violator (sanction in 2 cycles)
    assert a[6] is True                    # multi_source (2 families)
    assert a[7] is True                    # has_prior_sanction -> demoted

    b = by_id["BBB"]
    assert b[1] == 3                       # OIG-referral tier
    assert b[2] is None                    # opioid raw_value is not USD
    assert b[3] is None and b[4] is None   # no bounty
    assert b[5] is False
    assert b[6] is False
    assert b[7] is False                   # undetected

    c = by_id["CCC"]
    assert c[1] == 5                       # FEC structural
    assert c[7] is False                   # undetected


def test_provider_scale_breaks_undetected_ties(hv_db: psycopg.Connection) -> None:
    """Among undetected providers, real Medicare volume ranks first.

    Two never-sanctioned providers each fire one opioid outlier (same family,
    same severity). The one billing more Medicare (Part B payment) must outrank
    the other purely on provider_scale_usd.
    """
    with hv_db.cursor() as cur:
        for npi, sev in (("HISCALE001", 4), ("LOSCALE002", 4)):
            cur.execute(
                _OBS_INSERT,
                ("2023", "provider", npi, "opioid_prescribing_outlier",
                 50, sev, "specialty=X", 0.95, "/t"),
            )
        cur.execute(_PARTB_INSERT,
                    (2023, "HISCALE001", "NJ", 5_000_000, "/u", _SHA, "v"))
        cur.execute(_PARTB_INSERT,
                    (2023, "LOSCALE002", "NJ", 1_000_000, "/u", _SHA, "v"))
        hv_db.commit()
        cur.execute(
            "SELECT entity_id, provider_scale_usd, has_prior_sanction, lead_rank "
            "FROM derived.v_high_value_leads ORDER BY lead_rank"
        )
        rows = cur.fetchall()

    assert [r[0] for r in rows] == ["HISCALE001", "LOSCALE002"]
    assert float(rows[0][1]) == 5_000_000.0
    assert float(rows[1][1]) == 1_000_000.0
    assert all(r[2] is False for r in rows)   # both undetected


def test_multi_pattern_corroboration_outranks_single(
    hv_db: psycopg.Connection,
) -> None:
    """Among undetected providers with equal financial scale and the same single
    signal family (cms_utilization), the one tripping MORE distinct detectors
    (n_signals) ranks first (mig 116)."""
    with hv_db.cursor() as cur:
        # MANY: two distinct cms_utilization detectors (n_signals=2).
        for sig in ("opioid_prescribing_outlier", "antipsychotic_elderly_outlier"):
            cur.execute(
                _OBS_INSERT,
                ("2024", "provider", "MANY", sig,
                 50, 4, "specialty=X", 0.99, "/t"),
            )
        # FEW: one detector, same family, same severity (n_signals=1).
        cur.execute(
            _OBS_INSERT,
            ("2024", "provider", "FEW", "opioid_prescribing_outlier",
             50, 4, "specialty=X", 0.99, "/t"),
        )
        hv_db.commit()
        cur.execute(
            "SELECT entity_id, n_signals, n_families, lead_rank "
            "FROM derived.v_high_value_leads ORDER BY lead_rank"
        )
        rows = cur.fetchall()
    assert [r[0] for r in rows] == ["MANY", "FEW"]
    assert rows[0][1] == 2 and rows[1][1] == 1   # n_signals
    assert rows[0][2] == 1 and rows[1][2] == 1   # same single family


def test_single_cycle_sanction_is_not_repeat(hv_db: psycopg.Connection) -> None:
    """One sanction cycle must NOT count as a repeat violator."""
    with hv_db.cursor() as cur:
        cur.execute(
            _OBS_INSERT,
            ("2023", "provider", "ZZZ", "provider_excluded_billing",
             500_000, 5, "kind=provider", 0.99, "/t"),
        )
        hv_db.commit()
        cur.execute(
            "SELECT repeat_violator, best_reward_tier "
            "FROM derived.v_high_value_leads WHERE entity_id = 'ZZZ'"
        )
        rep, tier = cur.fetchone()
    assert rep is False
    assert tier == 1


def test_empty_substrate_yields_no_leads(hv_db: psycopg.Connection) -> None:
    """A migrated-but-empty engine returns an empty queue, never a fabricated one."""
    with hv_db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM derived.v_high_value_leads")
        assert cur.fetchone()[0] == 0
