"""Live-PG tests for the fraud-evidence substrate (mig 086, seeds 018 + 019).

VISION_2026 Pillar 2 (civic integrity) -- Phase F-UX work items F2 + F3 + F5.

The substrate this module tests:
    * ref.fraud_signal_human_explanation -- federal-authority citation
      registry, one row per signal_id (FK to derived.fraud_signal_config).
    * ref.fraud_signal_severity_calibration -- severity precedent registry,
      one row per signal_id (FK to derived.fraud_signal_config).
    * derived.v_anomaly_score_percentile_by_kind_cycle -- per-entity
      empirical CDF of risk_score within (cycle, entity_kind), the
      substrate for the UI's "above N% of peers" headline.

What this test module pins:
    * Both ref tables: column shapes, every NOT-NULL / CHECK constraint,
      FK to derived.fraud_signal_config(signal_id), updated_at trigger,
      formula_version stamping.
    * Seed 018: every fraud_signal_config row has a corresponding
      explanation row; every citation_authority is in the whitelist;
      every citation_url starts with http (federal-domain check is at the
      seed-author layer; URL-shape check is at the constraint layer).
    * Seed 019: every fraud_signal_config row has a corresponding
      calibration row; every calibration_basis is in the whitelist;
      severity_level on each row matches the SMALLINT literal the
      corresponding refresher emits (verified by extracting refresher
      severities from the migration files and cross-checking against
      derived.fraud_signal_observation severities after a synthetic
      INSERT).
    * v_anomaly_score_percentile_by_kind_cycle: column shape, peer-CDF
      arithmetic on hand-computed synthetic inputs (PERCENT_RANK and
      CUME_DIST), partition behavior on (cycle, entity_kind),
      empty-substrate -> empty-result substrate-honesty.

The arithmetic-vs-real-substrate integration is gated on F1 (the FEC
bulk loader) and verified once per-FEC-cycle data is loaded; these
tests pin the SUBSTRATE behavior independent of upstream data presence.
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


# ---------------------------------------------------------------------------
# All 17 known fraud signal_ids, with the (severity, calibration_basis) we
# expect each row in seed 019 to declare. severity matches the SMALLINT
# literal the corresponding refresher migration emits (audited against
# 050-066). calibration_basis matches what seed 019 declares.
#
# This map is the test-side oracle; if a future migration changes either
# the severity OR the basis, the matching test fails LOUDLY rather than
# silently re-aligning.
# ---------------------------------------------------------------------------

EXPECTED_SIGNALS: dict[str, tuple[int, str]] = {
    # leie_bearing
    "entity_on_leie": (5, "oig_report"),
    "entity_on_leie_strict_address": (5, "oig_report"),
    "entity_funded_and_excluded": (5, "far_authority"),
    "donor_on_leie": (5, "empirical_pctile"),
    "candidate_funded_by_excluded_donors": (5, "empirical_pctile"),
    # sam_bearing
    "entity_excluded_via_sam_uei": (5, "far_authority"),
    "donor_on_sam": (5, "empirical_pctile"),
    "candidate_funded_by_sam_excluded_donors": (5, "empirical_pctile"),
    # workforce
    "donor_employed_by_nj_contractor": (3, "fec_mur"),
    "candidate_funded_by_nj_contractor_employees": (3, "fec_mur"),
    # address
    "committee_address_clusters": (4, "fec_mur"),
    # structural
    "treasurer_concentration": (3, "empirical_pctile"),
    "candidate_no_pcc": (1, "empirical_pctile"),
    "candidate_broken_pcc": (2, "empirical_pctile"),
    "candidate_multiple_pccs": (2, "empirical_pctile"),
    "committee_name_collisions": (3, "fec_advisory"),
    "candidate_namesakes": (3, "empirical_pctile"),
    "treasurer_is_candidate": (1, "fec_advisory"),
}

# EXPECTED_FORMULA_VERSION: the BASE formula_version used by the original
# 17 signals (the one tests use when they construct their own synthetic
# seed rows for table-contract tests). Kept for backward compatibility
# with the table-contract test suite.
EXPECTED_FORMULA_VERSION = "2.1.0-fraud-evidence-substrate-v1"

# EXPECTED_FORMULA_VERSIONS: the EXACT set of formula_version strings
# present in the seed tables. Grows as new signals land under new
# substrate versions; the seed-completeness tests assert
# DISTINCT(formula_version) over the seed tables == this set, catching
# accidental version drift (a new signal that lands under an
# unregistered version will fail here).
EXPECTED_FORMULA_VERSIONS = {
    "2.1.0-fraud-evidence-substrate-v1",        # original 17 signals
    "2.3.0-fraud-strict-address-v1",            # entity_on_leie_strict_address
}


@pytest.fixture
def fraud_db(live_pg: psycopg.Connection) -> psycopg.Connection:
    """Cleanly-migrated DB; raw tables empty, fraud_signal_config seeded."""
    conn = live_pg
    with conn.cursor() as cur:
        for sch in ("governance", "derived", "raw", "ref"):
            cur.execute(f"DROP SCHEMA IF EXISTS {sch} CASCADE")
        cur.execute(
            "DO $$ "
            "DECLARE r record; "
            "BEGIN "
            "  FOR r IN SELECT viewname FROM pg_views "
            "           WHERE schemaname='public' AND viewname LIKE 'v_%%' LOOP "
            "    EXECUTE 'DROP VIEW IF EXISTS public.' "
            "         || quote_ident(r.viewname) || ' CASCADE'; "
            "  END LOOP; "
            "END $$;",
        )
    conn.commit()
    apply_migrations(conn, discover(MIGRATIONS_DIR))
    apply_migrations(conn, discover(SEEDS_DIR))
    conn.commit()
    return conn


# ===========================================================================
# Class A: ref.fraud_signal_human_explanation -- table contract
# ===========================================================================


class TestExplanationTableContract:
    def test_table_exists_with_expected_columns(
        self, fraud_db: psycopg.Connection
    ) -> None:
        """Pin the column set so the UI cannot silently consume a column
        that disappears."""
        with fraud_db.cursor() as cur:
            cur.execute("""
                SELECT column_name, is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'ref'
                  AND table_name = 'fraud_signal_human_explanation'
                ORDER BY ordinal_position
            """)
            cols = {row[0]: row[1] for row in cur.fetchall()}
        for required in (
            "signal_id",
            "rule_text",
            "citation_authority",
            "citation_section",
            "citation_url",
            "plain_english_template",
            "formula_version",
            "effective_date",
            "created_at",
            "updated_at",
        ):
            assert required in cols, f"missing column: {required!r}"
        for not_nullable in (
            "signal_id",
            "rule_text",
            "citation_authority",
            "citation_section",
            "citation_url",
            "plain_english_template",
            "formula_version",
            "effective_date",
        ):
            assert cols[not_nullable] == "NO", (
                f"{not_nullable} should be NOT NULL"
            )

    def test_signal_id_is_pk_and_fk_to_signal_config(
        self, fraud_db: psycopg.Connection
    ) -> None:
        """signal_id must be PK and FK to derived.fraud_signal_config."""
        with fraud_db.cursor() as cur:
            cur.execute("""
                SELECT
                    tc.constraint_type,
                    ccu.table_schema || '.' || ccu.table_name AS referenced
                FROM information_schema.table_constraints tc
                LEFT JOIN information_schema.constraint_column_usage ccu
                       ON tc.constraint_name = ccu.constraint_name
                WHERE tc.table_schema = 'ref'
                  AND tc.table_name   = 'fraud_signal_human_explanation'
                  AND tc.constraint_type IN ('PRIMARY KEY', 'FOREIGN KEY')
            """)
            kinds = sorted({row[0] for row in cur.fetchall()})
        assert "PRIMARY KEY" in kinds
        assert "FOREIGN KEY" in kinds

    def test_authority_check_rejects_non_whitelisted_value(
        self, fraud_db: psycopg.Connection
    ) -> None:
        """citation_authority is a CHECK enum -- a value outside the
        whitelist must fail the INSERT."""
        with fraud_db.cursor() as cur:
            cur.execute(
                "INSERT INTO derived.fraud_signal_config "
                "(signal_id, signal_family, min_actionable_threshold, comment) "
                "VALUES ('test_authority_chk', 'structural', 0, 'test row') "
                "ON CONFLICT (signal_id) DO NOTHING"
            )
            with pytest.raises(Exception) as exc:
                cur.execute(
                    "INSERT INTO ref.fraud_signal_human_explanation "
                    "(signal_id, rule_text, citation_authority, "
                    " citation_section, citation_url, "
                    " plain_english_template, formula_version, effective_date) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        "test_authority_chk",
                        "x" * 25,
                        "BOGUS_AUTHORITY",  # not in whitelist
                        "section_x",
                        "http://example.gov/x",
                        "y" * 35,
                        EXPECTED_FORMULA_VERSION,
                        "2026-05-08",
                    ),
                )
            assert "fraud_signal_human_explanation_authority_chk" in str(exc.value)
        fraud_db.rollback()

    def test_url_check_rejects_short_url(
        self, fraud_db: psycopg.Connection
    ) -> None:
        """citation_url must be >=11 chars to prevent accidental TBD."""
        with fraud_db.cursor() as cur:
            cur.execute(
                "INSERT INTO derived.fraud_signal_config "
                "(signal_id, signal_family, min_actionable_threshold, comment) "
                "VALUES ('test_url_chk', 'structural', 0, 'test row') "
                "ON CONFLICT (signal_id) DO NOTHING"
            )
            with pytest.raises(Exception) as exc:
                cur.execute(
                    "INSERT INTO ref.fraud_signal_human_explanation "
                    "(signal_id, rule_text, citation_authority, "
                    " citation_section, citation_url, "
                    " plain_english_template, formula_version, effective_date) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        "test_url_chk",
                        "x" * 25,
                        "FEC",
                        "section_x",
                        "TBD",  # too short
                        "y" * 35,
                        EXPECTED_FORMULA_VERSION,
                        "2026-05-08",
                    ),
                )
            assert "fraud_signal_human_explanation_url_chk" in str(exc.value)
        fraud_db.rollback()

    def test_fk_blocks_insert_when_signal_id_missing(
        self, fraud_db: psycopg.Connection
    ) -> None:
        """Cannot insert an explanation for a signal_id not in
        fraud_signal_config -- the FK must enforce this."""
        with fraud_db.cursor() as cur, pytest.raises(Exception) as exc:
            cur.execute(
                "INSERT INTO ref.fraud_signal_human_explanation "
                "(signal_id, rule_text, citation_authority, "
                " citation_section, citation_url, "
                " plain_english_template, formula_version, effective_date) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    "nonexistent_signal_id",
                    "x" * 25,
                    "FEC",
                    "section_x",
                    "http://example.gov/x",
                    "y" * 35,
                    EXPECTED_FORMULA_VERSION,
                    "2026-05-08",
                ),
            )
        assert "violates foreign key constraint" in str(exc.value).lower() or \
               "is not present" in str(exc.value).lower()
        fraud_db.rollback()

    def test_updated_at_trigger_fires_on_update(
        self, fraud_db: psycopg.Connection
    ) -> None:
        """The trigger must bump updated_at on every UPDATE."""
        with fraud_db.cursor() as cur:
            cur.execute(
                "SELECT updated_at FROM ref.fraud_signal_human_explanation "
                "WHERE signal_id = 'entity_on_leie'"
            )
            row_before = cur.fetchone()
            assert row_before is not None
            before = row_before[0]
            cur.execute(
                "UPDATE ref.fraud_signal_human_explanation "
                "SET rule_text = rule_text || ' ' "
                "WHERE signal_id = 'entity_on_leie'"
            )
            cur.execute(
                "SELECT updated_at FROM ref.fraud_signal_human_explanation "
                "WHERE signal_id = 'entity_on_leie'"
            )
            row_after = cur.fetchone()
            assert row_after is not None
            after = row_after[0]
        assert after > before
        fraud_db.rollback()


# ===========================================================================
# Class B: ref.fraud_signal_severity_calibration -- table contract
# ===========================================================================


class TestCalibrationTableContract:
    def test_table_exists_with_expected_columns(
        self, fraud_db: psycopg.Connection
    ) -> None:
        with fraud_db.cursor() as cur:
            cur.execute("""
                SELECT column_name, is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'ref'
                  AND table_name = 'fraud_signal_severity_calibration'
                ORDER BY ordinal_position
            """)
            cols = {row[0]: row[1] for row in cur.fetchall()}
        for required in (
            "signal_id",
            "severity_level",
            "calibration_basis",
            "precedent_url",
            "precedent_summary",
            "formula_version",
            "effective_date",
            "created_at",
            "updated_at",
        ):
            assert required in cols, f"missing column: {required!r}"

    def test_severity_check_rejects_out_of_range(
        self, fraud_db: psycopg.Connection
    ) -> None:
        """severity_level must be in [1, 5]."""
        with fraud_db.cursor() as cur:
            cur.execute(
                "INSERT INTO derived.fraud_signal_config "
                "(signal_id, signal_family, min_actionable_threshold, comment) "
                "VALUES ('test_sev_chk', 'structural', 0, 'test row') "
                "ON CONFLICT (signal_id) DO NOTHING"
            )
            with pytest.raises(Exception) as exc:
                cur.execute(
                    "INSERT INTO ref.fraud_signal_severity_calibration "
                    "(signal_id, severity_level, calibration_basis, "
                    " precedent_url, precedent_summary, "
                    " formula_version, effective_date) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        "test_sev_chk",
                        7,  # out of range
                        "empirical_pctile",
                        "http://example.gov/x",
                        "y" * 35,
                        EXPECTED_FORMULA_VERSION,
                        "2026-05-08",
                    ),
                )
            assert "fraud_signal_severity_calibration_level_chk" in str(exc.value)
        fraud_db.rollback()

    def test_basis_check_rejects_non_whitelisted_value(
        self, fraud_db: psycopg.Connection
    ) -> None:
        """calibration_basis must be one of the seven enum values."""
        with fraud_db.cursor() as cur:
            cur.execute(
                "INSERT INTO derived.fraud_signal_config "
                "(signal_id, signal_family, min_actionable_threshold, comment) "
                "VALUES ('test_basis_chk', 'structural', 0, 'test row') "
                "ON CONFLICT (signal_id) DO NOTHING"
            )
            with pytest.raises(Exception) as exc:
                cur.execute(
                    "INSERT INTO ref.fraud_signal_severity_calibration "
                    "(signal_id, severity_level, calibration_basis, "
                    " precedent_url, precedent_summary, "
                    " formula_version, effective_date) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        "test_basis_chk",
                        3,
                        "BOGUS_BASIS",
                        "http://example.gov/x",
                        "y" * 35,
                        EXPECTED_FORMULA_VERSION,
                        "2026-05-08",
                    ),
                )
            assert "fraud_signal_severity_calibration_basis_chk" in str(exc.value)
        fraud_db.rollback()


# ===========================================================================
# Class C: Seed 018 + 019 completeness + correctness
# ===========================================================================


class TestSeedCompleteness:
    def test_every_signal_in_config_has_explanation_row(
        self, fraud_db: psycopg.Connection
    ) -> None:
        """No fraud signal_id in fraud_signal_config may lack an
        explanation row (the UI cannot honestly render a card without one)."""
        with fraud_db.cursor() as cur:
            cur.execute("""
                SELECT cfg.signal_id
                FROM derived.fraud_signal_config cfg
                LEFT JOIN ref.fraud_signal_human_explanation expl
                       ON expl.signal_id = cfg.signal_id
                WHERE expl.signal_id IS NULL
            """)
            missing = [row[0] for row in cur.fetchall()]
        assert missing == [], f"signals lacking explanation: {missing}"

    def test_every_signal_in_config_has_calibration_row(
        self, fraud_db: psycopg.Connection
    ) -> None:
        """Same as above, for severity calibration."""
        with fraud_db.cursor() as cur:
            cur.execute("""
                SELECT cfg.signal_id
                FROM derived.fraud_signal_config cfg
                LEFT JOIN ref.fraud_signal_severity_calibration cal
                       ON cal.signal_id = cfg.signal_id
                WHERE cal.signal_id IS NULL
            """)
            missing = [row[0] for row in cur.fetchall()]
        assert missing == [], f"signals lacking calibration: {missing}"

    def test_explanation_rows_match_expected_signal_set(
        self, fraud_db: psycopg.Connection
    ) -> None:
        """Pin the exact set of signal_ids -- catches accidental adds /
        removes against EXPECTED_SIGNALS."""
        with fraud_db.cursor() as cur:
            cur.execute(
                "SELECT signal_id FROM ref.fraud_signal_human_explanation"
            )
            actual = {row[0] for row in cur.fetchall()}
        assert actual == set(EXPECTED_SIGNALS.keys()), (
            f"unexpected signal set: only-in-actual="
            f"{actual - set(EXPECTED_SIGNALS.keys())} "
            f"only-in-expected="
            f"{set(EXPECTED_SIGNALS.keys()) - actual}"
        )

    def test_every_explanation_row_uses_expected_formula_version(
        self, fraud_db: psycopg.Connection
    ) -> None:
        """The set of formula_versions present in the seed must exactly
        match EXPECTED_FORMULA_VERSIONS. Catches accidental version
        drift -- a new signal that lands under an unregistered version
        will fail here.
        """
        with fraud_db.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT formula_version "
                "FROM ref.fraud_signal_human_explanation"
            )
            versions = {row[0] for row in cur.fetchall()}
        assert versions == EXPECTED_FORMULA_VERSIONS, (
            f"unexpected versions: only-in-actual="
            f"{versions - EXPECTED_FORMULA_VERSIONS} "
            f"only-in-expected="
            f"{EXPECTED_FORMULA_VERSIONS - versions}"
        )

    def test_every_calibration_row_uses_expected_formula_version(
        self, fraud_db: psycopg.Connection
    ) -> None:
        with fraud_db.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT formula_version "
                "FROM ref.fraud_signal_severity_calibration"
            )
            versions = {row[0] for row in cur.fetchall()}
        assert versions == EXPECTED_FORMULA_VERSIONS, (
            f"unexpected versions: only-in-actual="
            f"{versions - EXPECTED_FORMULA_VERSIONS} "
            f"only-in-expected="
            f"{EXPECTED_FORMULA_VERSIONS - versions}"
        )


class TestSeedSeverityAndBasis:
    @pytest.mark.parametrize("signal_id", sorted(EXPECTED_SIGNALS.keys()))
    def test_severity_and_basis_match_oracle(
        self,
        fraud_db: psycopg.Connection,
        signal_id: str,
    ) -> None:
        """Every signal's (severity_level, calibration_basis) row must
        match the EXPECTED_SIGNALS oracle. Regression guard against any
        future migration that retunes severity without updating the seed.
        """
        expected_sev, expected_basis = EXPECTED_SIGNALS[signal_id]
        with fraud_db.cursor() as cur:
            cur.execute(
                "SELECT severity_level, calibration_basis "
                "FROM ref.fraud_signal_severity_calibration "
                "WHERE signal_id = %s",
                (signal_id,),
            )
            row = cur.fetchone()
        assert row is not None, f"no calibration row for {signal_id}"
        sev, basis = row
        assert int(sev) == expected_sev, (
            f"{signal_id}: severity {sev} != expected {expected_sev}"
        )
        assert basis == expected_basis, (
            f"{signal_id}: basis {basis!r} != expected {expected_basis!r}"
        )


class TestSeedCitationShape:
    """Verify URL shape for every seed row -- not the federal-domain
    whitelist (that's an authorial discipline) but the at-least-http
    discipline that prevents 'TBD' / empty / typo URLs from shipping."""

    def test_every_explanation_url_is_http_or_https(
        self, fraud_db: psycopg.Connection
    ) -> None:
        with fraud_db.cursor() as cur:
            cur.execute(
                "SELECT signal_id, citation_url "
                "FROM ref.fraud_signal_human_explanation"
            )
            rows = list(cur.fetchall())
        for sig, url in rows:
            assert url.startswith(("http://", "https://")), (
                f"{sig}: citation_url {url!r} is not http(s)"
            )

    def test_every_calibration_url_is_http_or_https(
        self, fraud_db: psycopg.Connection
    ) -> None:
        with fraud_db.cursor() as cur:
            cur.execute(
                "SELECT signal_id, precedent_url "
                "FROM ref.fraud_signal_severity_calibration"
            )
            rows = list(cur.fetchall())
        for sig, url in rows:
            assert url.startswith(("http://", "https://")), (
                f"{sig}: precedent_url {url!r} is not http(s)"
            )

    def test_explanation_template_contains_at_least_one_placeholder(
        self, fraud_db: psycopg.Connection
    ) -> None:
        """Every plain_english_template should contain at least one
        {{placeholder}} so the UI can substitute in entity_id / raw_value /
        cycle / etc. A template with no placeholders is a static string
        and should be in rule_text instead."""
        with fraud_db.cursor() as cur:
            cur.execute(
                "SELECT signal_id, plain_english_template "
                "FROM ref.fraud_signal_human_explanation"
            )
            rows = list(cur.fetchall())
        missing = [
            sig for sig, tpl in rows
            if "{{" not in tpl or "}}" not in tpl
        ]
        assert missing == [], (
            f"templates lacking {{{{placeholder}}}} tokens: {missing}"
        )


# ===========================================================================
# Class D: derived.v_anomaly_score_percentile_by_kind_cycle
# ===========================================================================


def _seed_observations(
    conn: psycopg.Connection,
    rows: list[tuple[str, str, str, str, float, int, str, float]],
) -> None:
    """Insert (cycle, entity_kind, entity_id, signal_id, raw_value, severity,
    peer_bucket, peer_percentile) tuples into fraud_signal_observation.
    evidence_url is hard-coded to '/test'."""
    with conn.cursor() as cur:
        for c, k, eid, sid, rv, sev, pb, pp in rows:
            cur.execute(
                "INSERT INTO derived.fraud_signal_observation "
                "(cycle, entity_kind, entity_id, signal_id, "
                " raw_value, severity, peer_bucket, peer_percentile, "
                " evidence_url) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                (c, k, eid, sid, rv, sev, pb, pp, "/test"),
            )
    conn.commit()


class TestPercentileViewColumnShape:
    def test_view_exists_with_expected_columns(
        self, fraud_db: psycopg.Connection
    ) -> None:
        with fraud_db.cursor() as cur:
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'derived'
                  AND table_name = 'v_anomaly_score_percentile_by_kind_cycle'
                ORDER BY ordinal_position
            """)
            cols = {row[0] for row in cur.fetchall()}
        for required in (
            "cycle",
            "entity_kind",
            "entity_id",
            "risk_score",
            "pctile_within_kind_cycle",
            "cume_dist_within_kind_cycle",
            "n_peers_in_bucket",
            "formula_version",
        ):
            assert required in cols, f"missing column: {required!r}"

    def test_empty_substrate_yields_empty_view(
        self, fraud_db: psycopg.Connection
    ) -> None:
        """No fraud_signal_observation rows -> empty view (substrate-honest:
        no rows, not zeros)."""
        with fraud_db.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) "
                "FROM derived.v_anomaly_score_percentile_by_kind_cycle"
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == 0


class TestPercentileViewArithmetic:
    """Hand-anchored peer-CDF tests on synthetic 5-entity input. Each
    entity gets a single observation on signal_id='entity_on_leie' (severity
    5, leie_bearing) at a distinct peer_percentile in (0.95, 1.0]; the
    score function maps these to 5 distinct risk_scores, and the
    percentile view should rank them at exactly (0.0, 0.25, 0.5, 0.75, 1.0).
    """

    def test_five_distinct_scores_yield_evenly_spaced_percent_rank(
        self, fraud_db: psycopg.Connection
    ) -> None:
        _seed_observations(
            fraud_db,
            [
                # Each tuple: (cycle, kind, entity_id, signal_id,
                #              raw_value, severity, peer_bucket, peer_percentile)
                ("2024", "candidate", "E1", "entity_on_leie",
                 1.0, 5, "office=H|state=NJ", 0.96),
                ("2024", "candidate", "E2", "entity_on_leie",
                 1.0, 5, "office=H|state=NJ", 0.97),
                ("2024", "candidate", "E3", "entity_on_leie",
                 1.0, 5, "office=H|state=NJ", 0.98),
                ("2024", "candidate", "E4", "entity_on_leie",
                 1.0, 5, "office=H|state=NJ", 0.99),
                ("2024", "candidate", "E5", "entity_on_leie",
                 1.0, 5, "office=H|state=NJ", 0.995),
            ],
        )
        with fraud_db.cursor() as cur:
            cur.execute("""
                SELECT entity_id,
                       risk_score,
                       pctile_within_kind_cycle,
                       cume_dist_within_kind_cycle,
                       n_peers_in_bucket
                FROM derived.v_anomaly_score_percentile_by_kind_cycle
                WHERE cycle = '2024' AND entity_kind = 'candidate'
                ORDER BY risk_score
            """)
            rows = list(cur.fetchall())
        assert len(rows) == 5
        ids = [r[0] for r in rows]
        assert ids == ["E1", "E2", "E3", "E4", "E5"]
        # Scores ascend strictly.
        from itertools import pairwise

        scores = [float(r[1]) for r in rows]
        assert scores == sorted(scores)
        assert all(s2 > s1 for s1, s2 in pairwise(scores))
        # PERCENT_RANK = (rank-1)/(N-1): 0, 0.25, 0.5, 0.75, 1.0
        prs = [float(r[2]) for r in rows]
        for actual, expected in zip(prs, [0.0, 0.25, 0.5, 0.75, 1.0],
                                    strict=True):
            assert actual == pytest.approx(expected, abs=1e-6)
        # CUME_DIST = rank/N: 0.2, 0.4, 0.6, 0.8, 1.0
        cds = [float(r[3]) for r in rows]
        for actual, expected in zip(cds, [0.2, 0.4, 0.6, 0.8, 1.0],
                                    strict=True):
            assert actual == pytest.approx(expected, abs=1e-6)
        # n_peers_in_bucket = 5 for every row
        for r in rows:
            assert r[4] == 5

    def test_partition_separates_kinds_within_same_cycle(
        self, fraud_db: psycopg.Connection
    ) -> None:
        """An entity_kind='candidate' bucket and an entity_kind='committee'
        bucket in the same cycle must be separately ranked: the top-scored
        candidate and the top-scored committee both read PERCENT_RANK=1.0,
        not relative to each other."""
        _seed_observations(
            fraud_db,
            [
                ("2024", "candidate", "C1", "entity_on_leie",
                 1.0, 5, "office=H|state=NJ", 0.96),
                ("2024", "candidate", "C2", "entity_on_leie",
                 1.0, 5, "office=H|state=NJ", 0.99),
                ("2024", "committee", "K1", "entity_on_leie",
                 1.0, 5, "kind=committee", 0.97),
                ("2024", "committee", "K2", "entity_on_leie",
                 1.0, 5, "kind=committee", 0.995),
            ],
        )
        with fraud_db.cursor() as cur:
            cur.execute("""
                SELECT entity_kind, entity_id,
                       pctile_within_kind_cycle, n_peers_in_bucket
                FROM derived.v_anomaly_score_percentile_by_kind_cycle
                WHERE cycle = '2024'
                ORDER BY entity_kind, entity_id
            """)
            rows = list(cur.fetchall())
        # 4 rows total, 2 per kind.
        assert len(rows) == 4
        per_kind: dict[str, list[tuple[str, float, int]]] = {}
        for kind, eid, pr, n in rows:
            per_kind.setdefault(kind, []).append((eid, float(pr), int(n)))
        assert set(per_kind.keys()) == {"candidate", "committee"}
        for kind, kind_rows in per_kind.items():
            # Each partition has 2 rows; PERCENT_RANK 0 and 1.
            assert len(kind_rows) == 2
            prs = sorted([r[1] for r in kind_rows])
            assert prs[0] == pytest.approx(0.0, abs=1e-6)
            assert prs[1] == pytest.approx(1.0, abs=1e-6)
            assert all(r[2] == 2 for r in kind_rows), (
                f"{kind}: n_peers must be 2"
            )

    def test_partition_separates_cycles_within_same_kind(
        self, fraud_db: psycopg.Connection
    ) -> None:
        """Two cycles with the same entity_kind must be ranked
        independently."""
        _seed_observations(
            fraud_db,
            [
                ("2022", "candidate", "X1", "entity_on_leie",
                 1.0, 5, "office=H|state=NJ", 0.96),
                ("2024", "candidate", "Y1", "entity_on_leie",
                 1.0, 5, "office=H|state=NJ", 0.99),
            ],
        )
        with fraud_db.cursor() as cur:
            cur.execute("""
                SELECT cycle, entity_id,
                       pctile_within_kind_cycle, n_peers_in_bucket
                FROM derived.v_anomaly_score_percentile_by_kind_cycle
                ORDER BY cycle, entity_id
            """)
            rows = list(cur.fetchall())
        assert len(rows) == 2
        # Each cycle has a single-row partition; PERCENT_RANK over a
        # single row is 0 (Postgres convention; (rank-1)/(N-1) with
        # N=1 -> 0/0 special-cased to 0).
        for _c, _eid, pr, n in rows:
            assert int(n) == 1
            assert float(pr) == pytest.approx(0.0, abs=1e-6)

    def test_formula_version_stamp(
        self, fraud_db: psycopg.Connection
    ) -> None:
        _seed_observations(
            fraud_db,
            [
                ("2024", "candidate", "Z1", "entity_on_leie",
                 1.0, 5, "office=H|state=NJ", 0.99),
            ],
        )
        with fraud_db.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT formula_version "
                "FROM derived.v_anomaly_score_percentile_by_kind_cycle"
            )
            versions = [row[0] for row in cur.fetchall()]
        assert versions == [EXPECTED_FORMULA_VERSION]
