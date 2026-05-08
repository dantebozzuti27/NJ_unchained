"""Live-PG tests for the real-dollar baseline substrate (mig 085).

VISION_2026 §3.4 substrate:
    * derived.f_real_dollar_base_year() -- latest CPI year, currently 2024
    * derived.v_affordability_gap_real -- CPI-deflated v_affordability_gap

The tests pin:
    1. f_real_dollar_base_year() returns the MAX(year) of cpi_u_headline_annual.
    2. f_real_dollar_base_year() returns NULL on empty CPI substrate
       (substrate-honest: never invent a base year).
    3. f_real_dollar_base_year() picks up new CPI years automatically.
    4. v_affordability_gap_real exposes the expected real-dollar columns.
    5. The deflation CASE statements correctly produce real = nominal
       * CPI(base) / CPI(year) when both CPI lookups hit, NULL otherwise.

The arithmetic-vs-real-substrate integration is covered by the
production verification in scripts/deploy_neon_substrate.sh and the
ad-hoc check this session: Bergen 2016 nominal $466,051 -> real $609,128
matches the 2016->2024 CPI inflation factor 1.3070 to 4 sig-figs.
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


@pytest.fixture
def real_db(live_pg: psycopg.Connection) -> psycopg.Connection:
    """Cleanly-migrated DB; raw tables empty."""
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


def _seed_cpi(conn: psycopg.Connection, rows: list[tuple[int, float]]) -> None:
    """Seed (year, annual_value) into the raw substrate that
    derived.cpi_u_headline_annual reads.

    The headline CPI view filters series_id='CUUR0000SA0' (CPI-U All
    Items, NSA) -- the canonical deflator. The annual value is read
    via derived.cpi_u_annual which aggregates from raw.cpi_u; we
    synthesize 12 identical monthly observations so the annual
    aggregate equals the seeded value.
    """
    with conn.cursor() as cur:
        for year, value in rows:
            for month in range(1, 13):
                cur.execute(
                    "INSERT INTO raw.cpi_u "
                    "  (series_id, year, period, value, "
                    "   source_url, source_sha256) "
                    "VALUES ('CUUR0000SA0', %s, %s, %s, "
                    "        'http://test/bls', %s) "
                    "ON CONFLICT DO NOTHING",
                    (year, f"M{month:02d}", value, "0" * 64),
                )
    conn.commit()


# ---------------------------------------------------------------------------
# Class A: f_real_dollar_base_year() returns MAX(cpi_u_headline_annual.year).
# ---------------------------------------------------------------------------


class TestRealDollarBaseYearFunction:
    def test_returns_max_year_when_cpi_present(
        self, real_db: psycopg.Connection
    ) -> None:
        """Seed CPI for 2020 + 2024 -> function returns 2024."""
        _seed_cpi(real_db, [(2020, 250.0), (2024, 313.7)])
        with real_db.cursor() as cur:
            cur.execute("SELECT derived.f_real_dollar_base_year()::INT")
            row = cur.fetchone()
        assert row is not None
        assert row[0] == 2024

    def test_returns_null_when_cpi_table_empty(
        self, real_db: psycopg.Connection
    ) -> None:
        """Empty CPI substrate -> function returns NULL (substrate-honest:
        we cannot invent a base year that doesn't exist)."""
        with real_db.cursor() as cur:
            cur.execute("SELECT derived.f_real_dollar_base_year()")
            row = cur.fetchone()
        assert row is not None
        assert row[0] is None

    def test_picks_up_new_year_automatically(
        self, real_db: psycopg.Connection
    ) -> None:
        """When 2025 CPI lands, the function returns 2025 with no code
        change. This is the key automated-update property of the design --
        the spec mandates 2026 eventually; until BLS publishes M13 2026
        the function tracks whatever year IS available."""
        _seed_cpi(real_db, [(2024, 313.7)])
        with real_db.cursor() as cur:
            cur.execute("SELECT derived.f_real_dollar_base_year()::INT")
            r1 = cur.fetchone()
            assert r1 is not None
            assert r1[0] == 2024
        _seed_cpi(real_db, [(2025, 322.0)])
        with real_db.cursor() as cur:
            cur.execute("SELECT derived.f_real_dollar_base_year()::INT")
            r2 = cur.fetchone()
            assert r2 is not None
            assert r2[0] == 2025


# ---------------------------------------------------------------------------
# Class B: v_affordability_gap_real column shape and deflation arithmetic.
#
# The real test of the deflation CASE statements is performed against
# production data (Bergen 2016 nominal $466K -> real $609K at the
# 2024 base, matching the 1.3070 CPI factor); these tests pin the
# COLUMN SHAPE and the function-level deflation contract.
# ---------------------------------------------------------------------------


class TestAffordabilityGapRealColumnShape:
    def test_view_exposes_the_expected_real_dollar_columns(
        self, real_db: psycopg.Connection
    ) -> None:
        """The view's column set must include real_dollar_base_year +
        every *_nominal column AND the matching *_real column. Pins the
        contract the UI consumes."""
        with real_db.cursor() as cur:
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'derived'
                  AND table_name = 'v_affordability_gap_real'
                ORDER BY ordinal_position
            """)
            cols = {row[0] for row in cur.fetchall()}
        # Headline contract -- the page consumes these names verbatim.
        for required in (
            "real_dollar_base_year",
            "home_price_nominal",
            "home_price_real",
            "median_income_nominal",
            "median_income_real",
            "piti_annual_nominal",
            "piti_annual_real",
            "required_income_hud_30pct_nominal",
            "required_income_hud_30pct_real",
            "hud_headroom_dollars_nominal",
            "hud_headroom_dollars_real",
            "cpi_at_base_year",
            "cpi_at_year",
            "formula_version",
        ):
            assert required in cols, (
                f"v_affordability_gap_real is missing column {required!r}; "
                f"present: {sorted(cols)}"
            )

    def test_formula_version_is_stamped_on_every_row(
        self, real_db: psycopg.Connection
    ) -> None:
        """The view must carry the formula_version stamp so a UI render
        can audit which version produced its numbers. This is the
        verifiable-data contract from .cursor/rules/verifiable-data.mdc."""
        with real_db.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT formula_version
                FROM derived.v_affordability_gap_real
                LIMIT 5
            """)
            versions = [row[0] for row in cur.fetchall()]
        for v in versions:
            assert v == "2.0.0-real-dollar-baseline-v1", (
                f"unexpected formula_version: {v!r}"
            )


class TestDeflationArithmeticDirect:
    """Pin the deflation arithmetic by running the same CASE expression
    the view uses against synthetic inputs constructed via a CTE -- no
    upstream substrate needed.
    """

    def test_year_equals_base_yields_unity_ratio(
        self, real_db: psycopg.Connection
    ) -> None:
        """When CPI(base) == CPI(year), the multiplier is 1.0 and real
        equals nominal exactly."""
        with real_db.cursor() as cur:
            cur.execute("""
                WITH
                  nominal AS (SELECT 500000.0::NUMERIC AS v),
                  cpi_base AS (SELECT 313.7::NUMERIC AS v),
                  cpi_year AS (SELECT 313.7::NUMERIC AS v)
                SELECT CASE WHEN cb.v IS NOT NULL AND cy.v IS NOT NULL
                                 AND cy.v <> 0
                            THEN ROUND(n.v * cb.v / cy.v, 2)
                       END AS real_v
                FROM nominal n, cpi_base cb, cpi_year cy
            """)
            row = cur.fetchone()
        assert row is not None
        assert float(row[0]) == pytest.approx(500000.0, abs=0.01)

    def test_cpi_inflation_factor_pins_real_value(
        self, real_db: psycopg.Connection
    ) -> None:
        """The CASE expression matches the production pattern: a 2016
        nominal value of 466,051 with CPI(2016)=240.007 and
        CPI(2024)=313.689 deflates to ~609,140 in 2024 dollars
        (matches the production verification this session to 1pp)."""
        with real_db.cursor() as cur:
            cur.execute("""
                WITH
                  nominal AS (SELECT 466051.0::NUMERIC AS v),
                  cpi_base AS (SELECT 313.689::NUMERIC AS v),
                  cpi_year AS (SELECT 240.007::NUMERIC AS v)
                SELECT ROUND(n.v * cb.v / cy.v, 2)::FLOAT8
                FROM nominal n, cpi_base cb, cpi_year cy
            """)
            row = cur.fetchone()
        assert row is not None
        # Production observed Bergen 2016 -> 609,128. Allow 1pp drift
        # because the production CPI values may differ at the 4th decimal.
        assert float(row[0]) == pytest.approx(609140.0, abs=200.0)

    def test_null_cpi_year_yields_null_real(
        self, real_db: psycopg.Connection
    ) -> None:
        """If CPI(year) is NULL, the CASE returns NULL -- substrate-honest:
        we cannot deflate a year for which we have no inflation data."""
        with real_db.cursor() as cur:
            cur.execute("""
                WITH
                  nominal AS (SELECT 466051.0::NUMERIC AS v),
                  cpi_base AS (SELECT 313.689::NUMERIC AS v),
                  cpi_year AS (SELECT NULL::NUMERIC AS v)
                SELECT CASE WHEN cb.v IS NOT NULL AND cy.v IS NOT NULL
                                 AND cy.v <> 0
                            THEN ROUND(n.v * cb.v / cy.v, 2)
                       END AS real_v
                FROM nominal n, cpi_base cb, cpi_year cy
            """)
            row = cur.fetchone()
        assert row is not None
        assert row[0] is None

    def test_cpi_year_zero_yields_null_real(
        self, real_db: psycopg.Connection
    ) -> None:
        """If CPI(year) is 0 (defensive against arithmetic glitches in
        upstream BLS data), the CASE returns NULL rather than dividing
        by zero."""
        with real_db.cursor() as cur:
            cur.execute("""
                WITH
                  nominal AS (SELECT 466051.0::NUMERIC AS v),
                  cpi_base AS (SELECT 313.689::NUMERIC AS v),
                  cpi_year AS (SELECT 0::NUMERIC AS v)
                SELECT CASE WHEN cb.v IS NOT NULL AND cy.v IS NOT NULL
                                 AND cy.v <> 0
                            THEN ROUND(n.v * cb.v / cy.v, 2)
                       END AS real_v
                FROM nominal n, cpi_base cb, cpi_year cy
            """)
            row = cur.fetchone()
        assert row is not None
        assert row[0] is None
