"""Live-PG regression tests for migration 093 + seed 022.

VISION_2026 Pillar 2 (civic integrity) -- Phase F8.5 stub. The substrate
this module pins:

    * ref.nj_state_candidate -- manually-curated NJ state-level candidate
      reference table with citation + provenance columns.
    * derived.v_nj_state_candidates -- UI-shape view with
      campaign_finance_ingest_pending flag.
    * seed 022 -- 10 publicly-announced 2025 NJ Gubernatorial primary
      candidates (6 D + 4 R) with HTTPS citation URLs.

What this module pins:
    * Schema: all 11 CHECK constraints attached.
    * Seed completeness: 6 DEM + 4 REP candidates for governor 2025.
    * Substrate-honesty: every row has HTTPS source_url, every row has
      campaign_finance_ingest_pending=TRUE, no row claims primary_winner
      / general_winner.
    * CHECK semantics: party / office / id-format / URL / winner-requires-
      url / announced-consistency all reject violations as expected.
    * View: row count matches base table; ordering puts governor first
      within an election year.
    * Forward-compat: setting elec_filing_id flips
      campaign_finance_ingest_pending FALSE.
    * Idempotency: re-running the seed is a no-op (ON CONFLICT DO UPDATE
      pattern preserves row count + values).
    * Trigger: updated_at advances on UPDATE.
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


EXPECTED_DEM_CANDIDATES = frozenset({
    "NJ-STATE-SHERRILL-MIKIE-2025-GOVERNOR",
    "NJ-STATE-FULOP-STEVEN-2025-GOVERNOR",
    "NJ-STATE-GOTTHEIMER-JOSH-2025-GOVERNOR",
    "NJ-STATE-SWEENEY-STEVE-2025-GOVERNOR",
    "NJ-STATE-BARAKA-RAS-2025-GOVERNOR",
    "NJ-STATE-SPILLER-SEAN-2025-GOVERNOR",
})

EXPECTED_REP_CANDIDATES = frozenset({
    "NJ-STATE-CIATTARELLI-JACK-2025-GOVERNOR",
    "NJ-STATE-SPADEA-BILL-2025-GOVERNOR",
    "NJ-STATE-BRAMNICK-JON-2025-GOVERNOR",
    "NJ-STATE-KRANJAC-MARIO-2025-GOVERNOR",
})

EXPECTED_FORMULA_VERSION = "2.4.0-nj-state-candidate-substrate-v1"

EXPECTED_CHECK_CONSTRAINTS = frozenset({
    "nj_state_candidate_party_chk",
    "nj_state_candidate_office_chk",
    "nj_state_candidate_election_year_chk",
    "nj_state_candidate_id_format_chk",
    "nj_state_candidate_source_url_chk",
    "nj_state_candidate_announcement_url_chk",
    "nj_state_candidate_primary_result_url_chk",
    "nj_state_candidate_general_result_url_chk",
    "nj_state_candidate_primary_winner_requires_url_chk",
    "nj_state_candidate_general_winner_requires_url_chk",
    "nj_state_candidate_announced_consistency_chk",
})


@pytest.fixture
def state_db(live_pg: psycopg.Connection) -> psycopg.Connection:
    """Cleanly-migrated DB with ref.nj_state_candidate + seed applied."""
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


# =============================================================================
# SCHEMA contract
# =============================================================================


def test_table_exists_with_expected_columns(state_db: psycopg.Connection):
    """ref.nj_state_candidate is created with all the columns the view + UI need."""
    with state_db.cursor() as cur:
        cur.execute("""
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema='ref' AND table_name='nj_state_candidate'
            ORDER BY ordinal_position
        """)
        cols = {row[0]: (row[1], row[2]) for row in cur.fetchall()}

    expected_cols = {
        "candidate_id", "full_name", "party", "office",
        "election_year", "primary_date", "general_date",
        "announced_candidate", "announcement_date", "announcement_url",
        "prior_office", "campaign_committee_name",
        "primary_winner", "primary_result_url",
        "general_winner", "general_result_url",
        "elec_filing_id",
        "source_url", "source_authority", "source_doc_date",
        "notes",
        "formula_version", "effective_date", "ingested_at", "updated_at",
    }
    missing = expected_cols - set(cols)
    assert not missing, f"missing columns: {missing}"

    # Substrate-honesty: source_url, source_authority, source_doc_date MUST be NOT NULL
    assert cols["source_url"][1] == "NO", "source_url must be NOT NULL"
    assert cols["source_authority"][1] == "NO", "source_authority must be NOT NULL"
    assert cols["source_doc_date"][1] == "NO", "source_doc_date must be NOT NULL"


def test_all_check_constraints_attached(state_db: psycopg.Connection):
    with state_db.cursor() as cur:
        cur.execute("""
            SELECT conname
            FROM pg_constraint
            WHERE conrelid = 'ref.nj_state_candidate'::regclass
              AND contype = 'c'
        """)
        names = {r[0] for r in cur.fetchall()}
    missing = EXPECTED_CHECK_CONSTRAINTS - names
    assert not missing, f"missing CHECK constraints: {missing}"


def test_party_check_rejects_unknown_party(state_db: psycopg.Connection):
    """Party enum is enforced: 'XYZ' is rejected."""
    import psycopg as _pg
    with state_db.cursor() as cur, pytest.raises(_pg.errors.CheckViolation):
        cur.execute("""
                INSERT INTO ref.nj_state_candidate (
                    candidate_id, full_name, party, office, election_year,
                    source_url, source_authority, source_doc_date,
                    formula_version
                ) VALUES (
                    'NJ-STATE-TEST-2030-GOVERNOR', 'Test Name', 'XYZ',
                    'governor', 2030,
                    'https://example.com/test-page',
                    'unit test', '2026-05-10',
                    %s
                )
            """, (EXPECTED_FORMULA_VERSION,))
    state_db.rollback()


def test_office_check_rejects_unknown_office(state_db: psycopg.Connection):
    """Office enum is enforced: 'mayor' is rejected (municipal not in v1 scope)."""
    import psycopg as _pg
    with state_db.cursor() as cur, pytest.raises(_pg.errors.CheckViolation):
        cur.execute("""
                INSERT INTO ref.nj_state_candidate (
                    candidate_id, full_name, party, office, election_year,
                    source_url, source_authority, source_doc_date,
                    formula_version
                ) VALUES (
                    'NJ-STATE-MAYOR-2030-MAYOR', 'Test Mayor', 'DEM',
                    'mayor', 2030,
                    'https://example.com/test-page',
                    'unit test', '2026-05-10',
                    %s
                )
            """, (EXPECTED_FORMULA_VERSION,))
    state_db.rollback()


def test_id_format_check_rejects_bad_id(state_db: psycopg.Connection):
    """candidate_id must match NJ-STATE-<...>-<YEAR>-<OFFICE>."""
    import psycopg as _pg
    with state_db.cursor() as cur, pytest.raises(_pg.errors.CheckViolation):
        cur.execute("""
                INSERT INTO ref.nj_state_candidate (
                    candidate_id, full_name, party, office, election_year,
                    source_url, source_authority, source_doc_date,
                    formula_version
                ) VALUES (
                    'lowercase-no-prefix', 'Test', 'DEM', 'governor', 2030,
                    'https://example.com/test-page',
                    'unit test', '2026-05-10',
                    %s
                )
            """, (EXPECTED_FORMULA_VERSION,))
    state_db.rollback()


def test_source_url_must_be_https(state_db: psycopg.Connection):
    """source_url HTTP is rejected (substrate-honesty: cite-via-TLS only)."""
    import psycopg as _pg
    with state_db.cursor() as cur, pytest.raises(_pg.errors.CheckViolation):
        cur.execute("""
                INSERT INTO ref.nj_state_candidate (
                    candidate_id, full_name, party, office, election_year,
                    source_url, source_authority, source_doc_date,
                    formula_version
                ) VALUES (
                    'NJ-STATE-TEST-2030-GOVERNOR', 'Test', 'DEM',
                    'governor', 2030,
                    'http://insecure.example.com/page',
                    'unit test', '2026-05-10',
                    %s
                )
            """, (EXPECTED_FORMULA_VERSION,))
    state_db.rollback()


def test_winner_requires_result_url(state_db: psycopg.Connection):
    """Claiming primary_winner=TRUE without primary_result_url is rejected."""
    import psycopg as _pg
    with state_db.cursor() as cur, pytest.raises(_pg.errors.CheckViolation):
        cur.execute("""
                INSERT INTO ref.nj_state_candidate (
                    candidate_id, full_name, party, office, election_year,
                    primary_winner,
                    source_url, source_authority, source_doc_date,
                    formula_version
                ) VALUES (
                    'NJ-STATE-WINNER-2030-GOVERNOR', 'Winner Test', 'DEM',
                    'governor', 2030,
                    TRUE,
                    'https://example.com/page',
                    'unit test', '2026-05-10',
                    %s
                )
            """, (EXPECTED_FORMULA_VERSION,))
    state_db.rollback()


def test_winner_with_result_url_is_accepted(state_db: psycopg.Connection):
    """Claiming primary_winner=TRUE WITH a primary_result_url IS accepted."""
    with state_db.cursor() as cur:
        cur.execute("""
            INSERT INTO ref.nj_state_candidate (
                candidate_id, full_name, party, office, election_year,
                primary_winner, primary_result_url,
                source_url, source_authority, source_doc_date,
                formula_version
            ) VALUES (
                'NJ-STATE-WINNER-2030-GOVERNOR', 'Winner Test', 'DEM',
                'governor', 2030,
                TRUE, 'https://example.com/results-page',
                'https://example.com/page',
                'unit test', '2026-05-10',
                %s
            )
        """, (EXPECTED_FORMULA_VERSION,))
        cur.execute("""
            SELECT primary_winner, primary_result_url
            FROM ref.nj_state_candidate
            WHERE candidate_id = 'NJ-STATE-WINNER-2030-GOVERNOR'
        """)
        row = cur.fetchone()
    assert row[0] is True
    assert row[1] == "https://example.com/results-page"
    state_db.rollback()


def test_announced_consistency_check(state_db: psycopg.Connection):
    """announced_candidate=FALSE with non-null announcement_date is rejected."""
    import psycopg as _pg
    with state_db.cursor() as cur, pytest.raises(_pg.errors.CheckViolation):
        cur.execute("""
                INSERT INTO ref.nj_state_candidate (
                    candidate_id, full_name, party, office, election_year,
                    announced_candidate, announcement_date,
                    source_url, source_authority, source_doc_date,
                    formula_version
                ) VALUES (
                    'NJ-STATE-NOTANN-2030-GOVERNOR', 'Not Announced', 'DEM',
                    'governor', 2030,
                    FALSE, '2030-01-01',
                    'https://example.com/page',
                    'unit test', '2026-05-10',
                    %s
                )
            """, (EXPECTED_FORMULA_VERSION,))
    state_db.rollback()


def test_election_year_check_rejects_out_of_range(state_db: psycopg.Connection):
    """election_year < 2000 or > 2050 is rejected."""
    import psycopg as _pg
    with state_db.cursor() as cur, pytest.raises(_pg.errors.CheckViolation):
        cur.execute("""
                INSERT INTO ref.nj_state_candidate (
                    candidate_id, full_name, party, office, election_year,
                    source_url, source_authority, source_doc_date,
                    formula_version
                ) VALUES (
                    'NJ-STATE-OLDTIMER-1999-GOVERNOR', 'Old Timer', 'DEM',
                    'governor', 1999,
                    'https://example.com/page',
                    'unit test', '2026-05-10',
                    %s
                )
            """, (EXPECTED_FORMULA_VERSION,))
    state_db.rollback()


# =============================================================================
# SEED completeness
# =============================================================================


def test_seed_count_6_dem_4_rep(state_db: psycopg.Connection):
    """Seed 022 yields exactly 6 Dem + 4 Rep candidates for governor 2025."""
    with state_db.cursor() as cur:
        cur.execute("""
            SELECT party, COUNT(*)
            FROM ref.nj_state_candidate
            WHERE office='governor' AND election_year=2025
            GROUP BY party
            ORDER BY party
        """)
        counts = dict(cur.fetchall())
    assert counts.get("DEM") == 6, f"DEM count: {counts.get('DEM')}"
    assert counts.get("REP") == 4, f"REP count: {counts.get('REP')}"


def test_all_expected_dem_candidates_present(state_db: psycopg.Connection):
    with state_db.cursor() as cur:
        cur.execute("""
            SELECT candidate_id
            FROM ref.nj_state_candidate
            WHERE party='DEM' AND office='governor' AND election_year=2025
        """)
        ids = {r[0] for r in cur.fetchall()}
    missing = EXPECTED_DEM_CANDIDATES - ids
    assert not missing, f"missing DEM candidates: {missing}"


def test_all_expected_rep_candidates_present(state_db: psycopg.Connection):
    with state_db.cursor() as cur:
        cur.execute("""
            SELECT candidate_id
            FROM ref.nj_state_candidate
            WHERE party='REP' AND office='governor' AND election_year=2025
        """)
        ids = {r[0] for r in cur.fetchall()}
    missing = EXPECTED_REP_CANDIDATES - ids
    assert not missing, f"missing REP candidates: {missing}"


def test_every_seeded_row_has_https_source_url(state_db: psycopg.Connection):
    with state_db.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*)
            FROM ref.nj_state_candidate
            WHERE source_url NOT LIKE 'https://%'
               OR length(source_url) < 15
        """)
        (n_bad,) = cur.fetchone()
    assert n_bad == 0, f"{n_bad} rows lack HTTPS source_url"


def test_every_seeded_row_is_announced(state_db: psycopg.Connection):
    """Every seeded primary candidate must have announced_candidate=TRUE."""
    with state_db.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*)
            FROM ref.nj_state_candidate
            WHERE announced_candidate = FALSE
              AND election_year = 2025
              AND office = 'governor'
        """)
        (n_not_announced,) = cur.fetchone()
    assert n_not_announced == 0, "seeded primary candidates must be announced"


def test_no_certified_results_in_seed(state_db: psycopg.Connection):
    """Substrate-honesty: seed must not claim primary/general results."""
    with state_db.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*)
            FROM ref.nj_state_candidate
            WHERE primary_winner IS NOT NULL
               OR general_winner IS NOT NULL
        """)
        (n,) = cur.fetchone()
    assert n == 0, f"{n} rows claim certified results without verified ingest"


def test_every_seeded_row_has_ingest_pending(state_db: psycopg.Connection):
    """Every seeded candidate has elec_filing_id IS NULL (ingest pending)."""
    with state_db.cursor() as cur:
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE campaign_finance_ingest_pending=TRUE),
                COUNT(*)
            FROM derived.v_nj_state_candidates
        """)
        pending, total = cur.fetchone()
    assert pending == total == 10, f"{pending}/{total} ingest-pending"


# =============================================================================
# VIEW shape + ordering
# =============================================================================


def test_view_row_count_matches_base_table(state_db: psycopg.Connection):
    """The view does NOT multiply rows via its ORDER BY."""
    with state_db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM ref.nj_state_candidate")
        (base,) = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM derived.v_nj_state_candidates")
        (view,) = cur.fetchone()
    assert base == view == 10


def test_view_exposes_ingest_pending_flag(state_db: psycopg.Connection):
    """The view computes campaign_finance_ingest_pending as elec_filing_id IS NULL."""
    with state_db.cursor() as cur:
        cur.execute("""
            SELECT candidate_id
            FROM ref.nj_state_candidate
            WHERE party = 'DEM' AND office = 'governor'
            ORDER BY candidate_id
            LIMIT 1
        """)
        (cid,) = cur.fetchone()

        # Set elec_filing_id; the view should report ingest_pending = FALSE.
        cur.execute("""
            UPDATE ref.nj_state_candidate
            SET elec_filing_id = 'ELEC-FAKE-9999'
            WHERE candidate_id = %s
        """, (cid,))

        cur.execute("""
            SELECT campaign_finance_ingest_pending
            FROM derived.v_nj_state_candidates
            WHERE entity_id = %s
        """, (cid,))
        (pending,) = cur.fetchone()
    assert pending is False, (
        "campaign_finance_ingest_pending must flip FALSE once elec_filing_id is set"
    )
    state_db.rollback()


def test_view_ordering_governor_first(state_db: psycopg.Connection):
    """View orders election_year DESC, then office by precedence (governor first)."""
    with state_db.cursor() as cur:
        cur.execute("""
            INSERT INTO ref.nj_state_candidate (
                candidate_id, full_name, party, office, election_year,
                announced_candidate, announcement_date,
                source_url, source_authority, source_doc_date,
                formula_version
            ) VALUES
                ('NJ-STATE-TEST-AG-2025-ATTORNEY_GENERAL',
                 'AG Test', 'DEM', 'attorney_general', 2025,
                 TRUE, '2024-01-01',
                 'https://example.com/page',
                 'unit test', '2026-05-10',
                 %s),
                ('NJ-STATE-TEST-LEG-2025-STATE_ASSEMBLY',
                 'Assembly Test', 'DEM', 'state_assembly', 2025,
                 TRUE, '2024-01-01',
                 'https://example.com/page',
                 'unit test', '2026-05-10',
                 %s)
        """, (EXPECTED_FORMULA_VERSION, EXPECTED_FORMULA_VERSION))

        cur.execute("""
            SELECT office
            FROM derived.v_nj_state_candidates
            WHERE election_year = 2025
        """)
        offices_in_order = [r[0] for r in cur.fetchall()]

    governor_idx = offices_in_order.index("governor")
    ag_idx = offices_in_order.index("attorney_general")
    assembly_idx = offices_in_order.index("state_assembly")

    assert governor_idx < ag_idx < assembly_idx, (
        f"office ordering wrong: {offices_in_order!r}"
    )
    state_db.rollback()


# =============================================================================
# Idempotency: re-running the seed is a no-op
# =============================================================================


def test_seed_is_idempotent(state_db: psycopg.Connection):
    """Re-applying seed 022 does not duplicate rows or alter content."""
    seed_path = SEEDS_DIR / "022_nj_state_candidate_2025_gubernatorial.sql"
    seed_sql = seed_path.read_text(encoding="utf-8")

    with state_db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM ref.nj_state_candidate")
        (before,) = cur.fetchone()
        cur.execute("""
            SELECT candidate_id, full_name, party, source_url
            FROM ref.nj_state_candidate
            ORDER BY candidate_id
        """)
        before_rows = cur.fetchall()

        cur.execute(seed_sql)

        cur.execute("SELECT COUNT(*) FROM ref.nj_state_candidate")
        (after,) = cur.fetchone()
        cur.execute("""
            SELECT candidate_id, full_name, party, source_url
            FROM ref.nj_state_candidate
            ORDER BY candidate_id
        """)
        after_rows = cur.fetchall()

    assert before == after == 10, f"row count drift: {before} -> {after}"
    assert before_rows == after_rows, "row content changed on re-apply"


# =============================================================================
# Trigger: updated_at advances on UPDATE
# =============================================================================


def test_updated_at_trigger_fires(state_db: psycopg.Connection):
    """updated_at must advance on UPDATE; ingested_at must NOT."""
    with state_db.cursor() as cur:
        cur.execute("""
            SELECT candidate_id, ingested_at, updated_at
            FROM ref.nj_state_candidate
            LIMIT 1
        """)
        cid, ingested0, updated0 = cur.fetchone()

        cur.execute("""
            UPDATE ref.nj_state_candidate
            SET notes = COALESCE(notes, '') || ' [touched]'
            WHERE candidate_id = %s
        """, (cid,))

        cur.execute("""
            SELECT ingested_at, updated_at
            FROM ref.nj_state_candidate
            WHERE candidate_id = %s
        """, (cid,))
        ingested1, updated1 = cur.fetchone()

    assert ingested0 == ingested1, "ingested_at must not change"
    assert updated1 > updated0, "updated_at must advance"
    state_db.rollback()


# =============================================================================
# Provenance: every row carries the expected formula_version
# =============================================================================


def test_every_seeded_row_carries_formula_version(state_db: psycopg.Connection):
    with state_db.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT formula_version
            FROM ref.nj_state_candidate
        """)
        versions = {r[0] for r in cur.fetchall()}
    assert versions == {EXPECTED_FORMULA_VERSION}, (
        f"unexpected formula_version values: {versions}"
    )


def test_formula_version_registered_in_ref(state_db: psycopg.Connection):
    """The seed's formula_version must exist in ref.formula_version (FK)."""
    with state_db.cursor() as cur:
        cur.execute("""
            SELECT effective_date, description
            FROM ref.formula_version
            WHERE formula_version = %s
        """, (EXPECTED_FORMULA_VERSION,))
        row = cur.fetchone()
    assert row is not None, (
        f"formula_version {EXPECTED_FORMULA_VERSION} not registered"
    )
    eff_date, desc = row
    assert eff_date.isoformat() == "2026-05-10"
    assert "Phase F8.5" in desc
    assert "campaign_finance_ingest_pending" in desc
