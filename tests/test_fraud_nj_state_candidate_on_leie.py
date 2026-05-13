"""Live-PG regression tests for migration 098 + seed 023.

VISION_2026 Pillar 2 (civic integrity) Phase F8.5-cross-source.

What this module pins:
    * derived.f_canonical_lastfirst_from_first_last canonicalizer:
      "First [Middle] Last" parsing, suffix-strip via the underlying
      f_normalize_name_token, single-token-input -> NULL, NULL-input
      -> NULL, output format BIT-IDENTICAL to the FEC-side and LEIE-
      side canonicalizers (the cornerstone of the cross-source join).
    * Schema: entity_kind CHECK constraint widened to include
      'nj_state_candidate'; old-list values still accepted (no
      regression against the existing 7 entity kinds).
    * Refresher derived.refresh_signal_nj_state_candidate_on_leie:
      idempotent DELETE+INSERT on (cycle, signal_id) slice; cycle
      isolation; FEC-cycle invocation returns 0 (election_year filter);
      bucket-relative percentile arithmetic; multi-LEIE-per-candidate
      collapse via DISTINCT ON; substrate-honest 0 returns on empty
      LEIE / empty NJ-state roster / non-matching cycle.
    * Master refresher derived.refresh_all_fraud_signal_observations
      now invokes the new refresher (signal coverage parity).
    * Reference data: severity_calibration row exists with severity 5
      and oig_report basis; human_explanation row exists with the
      expected federal-authority citation; evidence_url_template row
      exists and points at the OIG LEIE search landing page.
    * Evidence-card view derived.v_entity_fraud_evidence resolves
      display_name to ref.nj_state_candidate.full_name and is_nj=TRUE
      for an nj_state_candidate observation row.
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


EXPECTED_FORMULA_VERSION = "2.7.1-fraud-nj-state-candidate-on-leie-v1"


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def nj_db(live_pg: psycopg.Connection) -> psycopg.Connection:
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


def _scalar(
    conn: psycopg.Connection,
    q: str,
    *args: object,
) -> object:
    """Run a single-value query, return the value (or None)."""
    with conn.cursor() as cur:
        cur.execute(q, args)
        row = cur.fetchone()
        return row[0] if row else None


def _seed_leie_individual(
    conn: psycopg.Connection,
    *,
    record_hash: str,
    lastname: str,
    firstname: str,
    state: str | None = "NJ",
    excldate: str = "20200115",
    excltype: str = "1128A1",
    midname: str | None = None,
) -> None:
    """Seed one LEIE individual row directly (bypassing the ingester)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.hhs_oig_leie ("
            " record_hash, lastname, firstname, midname, busname, "
            " excltype, excldate, "
            " state, "
            " vintage_month, source_url, source_sha256"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                record_hash,
                lastname, firstname, midname, None,
                excltype, excldate,
                state,
                "2026-03",
                "https://example.test/UPDATED.csv",
                "0" * 64,
            ),
        )


TEST_ELECTION_YEAR = 2027  # Use a year ALL test fixtures share to isolate
                           # from the production seed (022) that loads
                           # the 10 announced 2025 gubernatorial candidates.
                           # Mixing test-fixture rows into the same
                           # election_year would inflate the bucket
                           # population and break the percentile arithmetic
                           # tests.


def _seed_nj_state_candidate(
    conn: psycopg.Connection,
    *,
    candidate_id: str,
    full_name: str,
    election_year: int = TEST_ELECTION_YEAR,
    party: str = "DEM",
    office: str = "governor",
) -> None:
    """Seed one NJ-state candidate row directly (bypassing the seed file).

    Goes through the public-facing INSERT path so all CHECK constraints
    on ref.nj_state_candidate are enforced (party enum, office enum,
    HTTPS source URL, candidate_id regex).

    Defaults to TEST_ELECTION_YEAR (2027) so the test bucket is disjoint
    from the production seed (022) that uses 2025.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ref.nj_state_candidate ("
            " candidate_id, full_name, party, office, election_year, "
            " primary_date, general_date, announced_candidate, "
            " source_url, source_authority, source_doc_date, "
            " formula_version, effective_date"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                candidate_id, full_name, party, office, election_year,
                f"{election_year}-06-10",
                f"{election_year}-11-04",
                True,
                "https://en.wikipedia.org/wiki/Test",
                "Test fixture",
                "2026-05-12",
                "2.7.1-fraud-nj-state-candidate-on-leie-v1",
                "2026-05-12",
            ),
        )


# ============================================================================
# 1. Canonicalizer (pure-SQL, no fixture seeding required)
# ============================================================================


def test_canonicalizer_basic_first_last(nj_db: psycopg.Connection) -> None:
    """The expected NJ-state full-name shapes canonicalize correctly."""
    cases = [
        ("Mikie Sherrill",   "SHERRILL|MIKIE"),
        ("Steven Fulop",     "FULOP|STEVEN"),
        ("Josh Gottheimer",  "GOTTHEIMER|JOSH"),
        ("Andy Kim",         "KIM|ANDY"),
        ("Sean Spiller",     "SPILLER|SEAN"),
    ]
    for inp, want in cases:
        got = _scalar(
            nj_db,
            "SELECT derived.f_canonical_lastfirst_from_first_last(%s)",
            inp,
        )
        assert got == want, f"{inp!r}: got {got!r}, want {want!r}"


def test_canonicalizer_drops_middle_name(nj_db: psycopg.Connection) -> None:
    """Middle names / initials must be dropped (matches LEIE format)."""
    cases = [
        ("Cory A. Booker",      "BOOKER|CORY"),
        ("Robert W. Smith",     "SMITH|ROBERT"),
        ("John Q Public",       "PUBLIC|JOHN"),
        ("Jane Marie Doe",      "DOE|JANE"),
    ]
    for inp, want in cases:
        got = _scalar(
            nj_db,
            "SELECT derived.f_canonical_lastfirst_from_first_last(%s)",
            inp,
        )
        assert got == want, f"{inp!r}: got {got!r}, want {want!r}"


def test_canonicalizer_strips_jr_sr_suffixes(
    nj_db: psycopg.Connection,
) -> None:
    """JR/SR/II/III/IV/V suffixes must NOT survive canonicalization.

    Critical: 'Bill Pascrell Jr' must canonicalize to ('Pascrell', 'Bill'),
    not ('Jr', 'Bill'). The underlying f_normalize_name_token handles
    the suffix-strip BEFORE the new canonicalizer's whitespace split.
    """
    cases = [
        ("Bill Pascrell Jr",     "PASCRELL|BILL"),
        ("Robert Doe Sr",        "DOE|ROBERT"),
        ("John Smith II",        "SMITH|JOHN"),
        ("Hannah Brown III",     "BROWN|HANNAH"),
    ]
    for inp, want in cases:
        got = _scalar(
            nj_db,
            "SELECT derived.f_canonical_lastfirst_from_first_last(%s)",
            inp,
        )
        assert got == want, f"{inp!r}: got {got!r}, want {want!r}"


def test_canonicalizer_preserves_hyphenated_lastnames(
    nj_db: psycopg.Connection,
) -> None:
    """Hyphenated last names must be preserved (LEIE search guidance)."""
    got = _scalar(
        nj_db,
        "SELECT derived.f_canonical_lastfirst_from_first_last(%s)",
        "Mary Smith-Jones",
    )
    assert got == "SMITH-JONES|MARY"


def test_canonicalizer_returns_null_on_single_token_name(
    nj_db: psycopg.Connection,
) -> None:
    """Single-token names cannot produce a (last, first) pair."""
    for inp in ["Beyonce", "Sting", "Madonna"]:
        got = _scalar(
            nj_db,
            "SELECT derived.f_canonical_lastfirst_from_first_last(%s)",
            inp,
        )
        assert got is None, f"{inp!r}: expected NULL, got {got!r}"


def test_canonicalizer_returns_null_on_null_or_empty(
    nj_db: psycopg.Connection,
) -> None:
    """NULL / empty / pure-punctuation inputs must return NULL."""
    for inp in [None, "", "   ", "...", "!@#$"]:
        got = _scalar(
            nj_db,
            "SELECT derived.f_canonical_lastfirst_from_first_last(%s)",
            inp,
        )
        assert got is None, f"{inp!r}: expected NULL, got {got!r}"


def test_canonicalizer_output_matches_fec_side(
    nj_db: psycopg.Connection,
) -> None:
    """Critical: NJ-state and FEC canonicalizers MUST agree on the same
    underlying identity. If they disagree, the cross-source join silently
    drops the match.
    """
    pairs = [
        # NJ-state shape          FEC shape
        ("Jane Doe",               "DOE, JANE"),
        ("Jane A. Doe",            "DOE, JANE A"),
        ("Mary Smith-Jones",       "SMITH-JONES, MARY"),
        ("Bill Pascrell Jr",       "PASCRELL JR, BILL"),
        ("Cory Booker",            "BOOKER, CORY"),
    ]
    for nj_form, fec_form in pairs:
        a = _scalar(
            nj_db,
            "SELECT derived.f_canonical_lastfirst_from_first_last(%s)",
            nj_form,
        )
        b = _scalar(
            nj_db,
            "SELECT derived.f_canonical_lastfirst_from_fec(%s)",
            fec_form,
        )
        assert a == b, (
            f"NJ {nj_form!r} -> {a!r} vs FEC {fec_form!r} -> {b!r} "
            "(cross-source canonicalizer disagreement -- the join would "
            "silently drop this identity match)"
        )


# ============================================================================
# 2. Schema: entity_kind whitelist widening
# ============================================================================


def test_entity_kind_check_constraint_includes_nj_state_candidate(
    nj_db: psycopg.Connection,
) -> None:
    """The widened CHECK constraint must accept the new value AND retain
    the prior 7 values (no regression).
    """
    # Probe each accepted value via INSERT-then-DELETE (CHECK is enforced
    # at row time). We only need to verify the CHECK passes -- subsequent
    # constraints (PK, FK) are not exercised because we use a unique
    # (cycle, entity_id, signal_id) tuple per row.
    values = [
        "candidate", "committee", "treasurer", "address",
        "donor_cluster", "contractor", "donor", "nj_state_candidate",
    ]
    for kind in values:
        with nj_db.cursor() as cur:
            cur.execute(
                "INSERT INTO derived.fraud_signal_observation "
                "(cycle, entity_kind, entity_id, signal_id, "
                " raw_value, severity, peer_bucket, peer_percentile, evidence_url) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    "2025", kind, f"PROBE-{kind}",
                    "nj_state_candidate_on_leie",
                    1, 5, f"kind={kind}", 0.9, "/probe",
                ),
            )
            cur.execute(
                "DELETE FROM derived.fraud_signal_observation "
                "WHERE entity_id = %s",
                (f"PROBE-{kind}",),
            )
    nj_db.commit()


def test_entity_kind_check_constraint_rejects_unknown(
    nj_db: psycopg.Connection,
) -> None:
    """Constraint must still reject an unknown entity_kind value."""
    import psycopg.errors
    with nj_db.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            "INSERT INTO derived.fraud_signal_observation "
            "(cycle, entity_kind, entity_id, signal_id, "
            " raw_value, severity, peer_bucket, peer_percentile, evidence_url) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                "2025", "made_up_kind", "PROBE-INVALID",
                "nj_state_candidate_on_leie",
                1, 5, "kind=made_up_kind", 0.9, "/probe",
            ),
        )


# ============================================================================
# 3. Refresher: cycle isolation, idempotency, empty-substrate behavior
# ============================================================================


_TEST_CYCLE = str(TEST_ELECTION_YEAR)  # CHAR(4) cycle that matches our test
                                       # election_year. Using '2027' instead
                                       # of '2025' so the test bucket is
                                       # disjoint from the production seed
                                       # 022 (10 announced 2025 candidates).


def test_refresher_returns_zero_on_empty_substrate(
    nj_db: psycopg.Connection,
) -> None:
    """No NJ candidates and no LEIE -> 0 rows; no error."""
    n = _scalar(
        nj_db,
        "SELECT derived.refresh_signal_nj_state_candidate_on_leie(%s)",
        _TEST_CYCLE,
    )
    assert n == 0


def test_refresher_returns_zero_when_leie_empty(
    nj_db: psycopg.Connection,
) -> None:
    """NJ candidates loaded, LEIE empty -> 0 rows; no error."""
    _seed_nj_state_candidate(
        nj_db,
        candidate_id="NJ-STATE-DOE-JANE-2027-GOVERNOR",
        full_name="Jane Doe",
    )
    nj_db.commit()
    n = _scalar(
        nj_db,
        "SELECT derived.refresh_signal_nj_state_candidate_on_leie(%s)",
        _TEST_CYCLE,
    )
    assert n == 0


def test_refresher_returns_zero_when_no_nj_candidates(
    nj_db: psycopg.Connection,
) -> None:
    """LEIE loaded, no NJ candidates -> 0 rows."""
    _seed_leie_individual(
        nj_db, record_hash="a" * 64,
        lastname="DOE", firstname="JANE",
    )
    nj_db.commit()
    n = _scalar(
        nj_db,
        "SELECT derived.refresh_signal_nj_state_candidate_on_leie(%s)",
        _TEST_CYCLE,
    )
    assert n == 0


def test_refresher_emits_observation_on_match(
    nj_db: psycopg.Connection,
) -> None:
    """A canonical name match between an NJ candidate and an active LEIE
    individual must produce exactly one observation row."""
    _seed_leie_individual(
        nj_db, record_hash="b" * 64,
        lastname="DOE", firstname="JANE",
    )
    _seed_nj_state_candidate(
        nj_db,
        candidate_id="NJ-STATE-DOE-JANE-2027-GOVERNOR",
        full_name="Jane M. Doe",  # middle initial dropped
    )
    nj_db.commit()

    n = _scalar(
        nj_db,
        "SELECT derived.refresh_signal_nj_state_candidate_on_leie(%s)",
        _TEST_CYCLE,
    )
    assert n == 1

    with nj_db.cursor() as cur:
        cur.execute(
            "SELECT cycle, entity_kind, entity_id, signal_id, "
            "       severity, peer_bucket "
            "FROM derived.fraud_signal_observation "
            "WHERE signal_id = 'nj_state_candidate_on_leie'"
        )
        rows = cur.fetchall()
    assert rows == [(
        _TEST_CYCLE, "nj_state_candidate", "NJ-STATE-DOE-JANE-2027-GOVERNOR",
        "nj_state_candidate_on_leie", 5, "kind=nj_state_candidate",
    )]


def test_refresher_collapses_multiple_leie_per_candidate(
    nj_db: psycopg.Connection,
) -> None:
    """An NJ candidate matching multiple LEIE rows must produce exactly
    one observation, picking the freshest excldate (DISTINCT ON)."""
    # Two LEIE rows with same canonical key, different excldates.
    _seed_leie_individual(
        nj_db, record_hash="1" * 64,
        lastname="DOE", firstname="JANE",
        excldate="20100115",  # older
    )
    _seed_leie_individual(
        nj_db, record_hash="2" * 64,
        lastname="DOE", firstname="JANE A",  # different middle, same canon
        excldate="20240601",  # newer
    )
    _seed_nj_state_candidate(
        nj_db,
        candidate_id="NJ-STATE-DOE-JANE-2027-GOVERNOR",
        full_name="Jane Doe",
    )
    nj_db.commit()

    n = _scalar(
        nj_db,
        "SELECT derived.refresh_signal_nj_state_candidate_on_leie(%s)",
        _TEST_CYCLE,
    )
    assert n == 1

    # The evidence_url should reference the FRESHEST LEIE record (2 = 2024).
    with nj_db.cursor() as cur:
        cur.execute(
            "SELECT evidence_url FROM derived.fraud_signal_observation "
            "WHERE signal_id = 'nj_state_candidate_on_leie'"
        )
        (url,) = cur.fetchone()
    assert "leie=" + ("2" * 64) in url, (
        f"expected freshest LEIE record_hash in evidence_url; got {url!r}"
    )


def test_refresher_is_idempotent(nj_db: psycopg.Connection) -> None:
    """Re-running the refresher for the same cycle must NOT duplicate rows."""
    _seed_leie_individual(
        nj_db, record_hash="c" * 64,
        lastname="DOE", firstname="JANE",
    )
    _seed_nj_state_candidate(
        nj_db,
        candidate_id="NJ-STATE-DOE-JANE-2027-GOVERNOR",
        full_name="Jane Doe",
    )
    nj_db.commit()

    n1 = _scalar(
        nj_db,
        "SELECT derived.refresh_signal_nj_state_candidate_on_leie(%s)",
        _TEST_CYCLE,
    )
    n2 = _scalar(
        nj_db,
        "SELECT derived.refresh_signal_nj_state_candidate_on_leie(%s)",
        _TEST_CYCLE,
    )
    assert n1 == n2 == 1

    n_total = _scalar(
        nj_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE signal_id = 'nj_state_candidate_on_leie'",
    )
    assert n_total == 1


def test_refresher_isolates_cycles(nj_db: psycopg.Connection) -> None:
    """A test-cycle refresh must not touch any other cycle's slice."""
    _seed_leie_individual(
        nj_db, record_hash="d" * 64,
        lastname="DOE", firstname="JANE",
    )
    _seed_nj_state_candidate(
        nj_db,
        candidate_id="NJ-STATE-DOE-JANE-2027-GOVERNOR",
        full_name="Jane Doe",
    )
    # Pre-seed an unrelated cycle row in the observation table that
    # the refresher must NOT touch. Cycle '2029' is disjoint from any
    # NJ-state candidate row in the test fixtures.
    with nj_db.cursor() as cur:
        cur.execute(
            "INSERT INTO derived.fraud_signal_observation "
            "(cycle, entity_kind, entity_id, signal_id, "
            " raw_value, severity, peer_bucket, peer_percentile, evidence_url) "
            "VALUES ('2029', 'nj_state_candidate', 'PROBE', "
            "'nj_state_candidate_on_leie', 1, 5, 'kind=nj_state_candidate', "
            "0.9, '/probe-2029')"
        )
    nj_db.commit()

    _ = _scalar(
        nj_db,
        "SELECT derived.refresh_signal_nj_state_candidate_on_leie(%s)",
        _TEST_CYCLE,
    )
    # Pre-seeded 2029 row must still exist.
    n_2029 = _scalar(
        nj_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        "WHERE cycle = '2029' AND signal_id = 'nj_state_candidate_on_leie'",
    )
    assert n_2029 == 1


def test_refresher_returns_zero_for_fec_cycle(
    nj_db: psycopg.Connection,
) -> None:
    """Calling the refresher with an FEC even-year cycle (2024, 2026)
    must return 0 because no NJ-state candidate has election_year in
    those cycles. Substrate-honest no-op for the master orchestrator.
    """
    _seed_leie_individual(
        nj_db, record_hash="e" * 64,
        lastname="DOE", firstname="JANE",
    )
    _seed_nj_state_candidate(
        nj_db,
        candidate_id="NJ-STATE-DOE-JANE-2027-GOVERNOR",
        full_name="Jane Doe",
        election_year=TEST_ELECTION_YEAR,
    )
    nj_db.commit()

    for fec_cycle in ("2024", "2026"):
        n = _scalar(
            nj_db,
            "SELECT derived.refresh_signal_nj_state_candidate_on_leie(%s)",
            fec_cycle,
        )
        assert n == 0, (
            f"cycle {fec_cycle}: expected 0 NJ-state matches "
            f"(no NJ-state candidate in that election_year), got {n}"
        )


def test_refresher_percentile_arithmetic_small_bucket(
    nj_db: psycopg.Connection,
) -> None:
    """Bucket-relative percentile: 1 match against a 10-candidate bucket
    must yield percentile 1 - 1/10 = 0.9 exactly.
    """
    # Seed 10 NJ candidates; only the first matches LEIE.
    _seed_leie_individual(
        nj_db, record_hash="f" * 64,
        lastname="DOE", firstname="JANE",
    )
    _seed_nj_state_candidate(
        nj_db,
        candidate_id="NJ-STATE-DOE-JANE-2027-GOVERNOR",
        full_name="Jane Doe",
    )
    for i in range(2, 11):
        _seed_nj_state_candidate(
            nj_db,
            candidate_id=f"NJ-STATE-OTHER-CAND{i:02d}-2027-GOVERNOR",
            full_name=f"Other Candidate{i:02d}",
        )
    nj_db.commit()

    _ = _scalar(
        nj_db,
        "SELECT derived.refresh_signal_nj_state_candidate_on_leie(%s)",
        _TEST_CYCLE,
    )
    p = _scalar(
        nj_db,
        "SELECT peer_percentile FROM derived.fraud_signal_observation "
        "WHERE signal_id = 'nj_state_candidate_on_leie'",
    )
    assert float(p) == pytest.approx(0.9), (
        f"expected percentile 0.9 (1 match in 10-candidate bucket); got {p}"
    )


# ============================================================================
# 4. Master refresher integration
# ============================================================================


def test_master_refresher_includes_new_signal(
    nj_db: psycopg.Connection,
) -> None:
    """A call into the master refresher must invoke the new NJ-state
    refresher (the master is the canonical single source of truth for
    'which signals exist' -- this regression test enforces that)."""
    _seed_leie_individual(
        nj_db, record_hash="7" * 64,
        lastname="DOE", firstname="JANE",
    )
    _seed_nj_state_candidate(
        nj_db,
        candidate_id="NJ-STATE-DOE-JANE-2027-GOVERNOR",
        full_name="Jane Doe",
    )
    nj_db.commit()

    # Call the master with the test cycle.
    _ = _scalar(
        nj_db,
        "SELECT derived.refresh_all_fraud_signal_observations(%s)",
        _TEST_CYCLE,
    )

    n = _scalar(
        nj_db,
        "SELECT COUNT(*) FROM derived.fraud_signal_observation "
        f"WHERE cycle = '{_TEST_CYCLE}' "
        "  AND signal_id = 'nj_state_candidate_on_leie'",
    )
    assert n == 1, (
        "master refresher must include nj_state_candidate_on_leie; "
        "found 0 observations after master call -- the master is no "
        "longer the single source of truth"
    )


# ============================================================================
# 5. Reference data + evidence-card view widening
# ============================================================================


def test_severity_calibration_row_exists(
    nj_db: psycopg.Connection,
) -> None:
    """The seed must register severity 5 with oig_report basis."""
    with nj_db.cursor() as cur:
        cur.execute(
            "SELECT severity_level, calibration_basis "
            "FROM ref.fraud_signal_severity_calibration "
            "WHERE signal_id = 'nj_state_candidate_on_leie'"
        )
        row = cur.fetchone()
    assert row is not None, "seed 023 did not insert severity calibration row"
    sev, basis = row
    assert sev == 5
    assert basis == "oig_report"


def test_human_explanation_row_exists(
    nj_db: psycopg.Connection,
) -> None:
    """The seed must register a federal-authority citation."""
    with nj_db.cursor() as cur:
        cur.execute(
            "SELECT citation_authority, citation_section "
            "FROM ref.fraud_signal_human_explanation "
            "WHERE signal_id = 'nj_state_candidate_on_leie'"
        )
        row = cur.fetchone()
    assert row is not None
    auth, section = row
    assert auth == "HHS-OIG"
    assert "1320a" in section


def test_evidence_url_template_row_exists(
    nj_db: psycopg.Connection,
) -> None:
    """The seed must register an upstream-verify URL template."""
    with nj_db.cursor() as cur:
        cur.execute(
            "SELECT url_template, button_label, upstream_source "
            "FROM ref.fraud_signal_evidence_url_template "
            "WHERE signal_id = 'nj_state_candidate_on_leie'"
        )
        row = cur.fetchone()
    assert row is not None
    url, label, source = row
    assert url.startswith("https://oig.hhs.gov/")
    assert label == "Search OIG LEIE"
    assert source == "OIG.gov"


def test_evidence_view_resolves_display_name_and_is_nj(
    nj_db: psycopg.Connection,
) -> None:
    """v_entity_fraud_evidence must resolve display_name to the NJ
    candidate's full_name and is_nj to TRUE."""
    _seed_leie_individual(
        nj_db, record_hash="8" * 64,
        lastname="DOE", firstname="JANE",
    )
    _seed_nj_state_candidate(
        nj_db,
        candidate_id="NJ-STATE-DOE-JANE-2027-GOVERNOR",
        full_name="Jane Doe",
    )
    nj_db.commit()
    _ = _scalar(
        nj_db,
        "SELECT derived.refresh_signal_nj_state_candidate_on_leie(%s)",
        _TEST_CYCLE,
    )

    with nj_db.cursor() as cur:
        cur.execute(
            "SELECT display_name, is_nj, severity, "
            "       upstream_verify_url, citation_authority "
            "FROM derived.v_entity_fraud_evidence "
            "WHERE signal_id = 'nj_state_candidate_on_leie'"
        )
        row = cur.fetchone()
    assert row is not None, "v_entity_fraud_evidence yielded no row"
    display_name, is_nj, severity, upstream, citation = row
    assert display_name == "Jane Doe"
    assert is_nj is True
    assert severity == 5
    assert upstream.startswith("https://oig.hhs.gov/")
    assert citation == "HHS-OIG"


# ============================================================================
# 6. Provenance
# ============================================================================


def test_formula_version_registered(nj_db: psycopg.Connection) -> None:
    """The migration's formula_version must be registered."""
    with nj_db.cursor() as cur:
        cur.execute(
            "SELECT effective_date, description "
            "FROM ref.formula_version "
            "WHERE formula_version = %s",
            (EXPECTED_FORMULA_VERSION,),
        )
        row = cur.fetchone()
    assert row is not None
    eff_date, desc = row
    assert eff_date.isoformat() == "2026-05-12"
    assert "nj_state_candidate_on_leie" in desc
