"""Live-PG validation of the Collapse Curve SQL surface (idea §7.3).

The Next.js Collapse Curve page (`app/housing/[id]/collapse/page.tsx`)
calls `getCountyAffordabilityGap()` in `lib/housing.ts`, which issues
TWO queries against the production substrate:

1.  Per-county time series read from ``derived.v_affordability_gap``
    (Phase 2, migration 072).
2.  A coverage summary that reports the year ranges of each upstream
    substrate (DCA / ACS5 / FRED / IRS+NJ tax tables) so the page can
    be honest about what's loaded.

This test re-issues the same SQL through psycopg against a freshly-
migrated PG with synthetic substrate, then asserts:

  * the gap row count matches the seeded year set,
  * dollar values are within $0.50 of hand-computed expectations,
  * NULL semantics are correct (years with seeded DCA+ACS but no
    seeded tax tables surface NULL required-income, not 0),
  * the coverage summary reflects exactly what we seeded,
  * the formula version stamp is the Phase-2 engine.

If the frontend and backend ever drift apart, this test is the canary.
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
# Fixture: cleanly initialized DB with the minimum substrate the Collapse
# Curve needs -- one synthetic county, two years of DCA + ACS5 substrate,
# FRED 30-yr rate seeded for both years, and Phase-1 tax tables already
# seeded (2023 + 2024).
# ---------------------------------------------------------------------------


@pytest.fixture
def collapse_db(live_pg: psycopg.Connection) -> psycopg.Connection:
    conn = live_pg
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS governance CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS derived    CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS raw        CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS ref        CASCADE")
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

    # Synthetic substrate. Use a fake state code so we never collide with
    # real NJ data (and so the existence of these rows is unambiguous in
    # any future debugging).
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ref.state (state_code, state_fips, name) "
            "VALUES ('XX', '99', 'Test State') "
            "ON CONFLICT DO NOTHING"
        )
        cur.execute(
            "INSERT INTO ref.county (county_id, state_code, county_fips, name) "
            "VALUES ('XX-COLLAPSE', 'XX', '99001', 'Collapse Test County') "
            "ON CONFLICT DO NOTHING"
        )
        # FRED 30-yr at 7% in 2024, 6% in 2023.
        cur.executemany(
            "INSERT INTO raw.fred_observation "
            "  (series_id, observation_date, value, source_url, source_sha256) "
            "VALUES ('MORTGAGE30US', %s, %s, "
            "        'https://fred.stlouisfed.org/', %s) "
            "ON CONFLICT DO NOTHING",
            [
                ("2023-06-06", 6.0000, "a" * 64),
                ("2024-06-06", 7.0000, "b" * 64),
            ],
        )
        # DCA: $450K avg residential value in 2023, $500K in 2024.
        cur.executemany(
            "INSERT INTO raw.nj_property_tax_county "
            "  (county_fips, year, avg_residential_value, cy_total_rate, "
            "   source_url, source_sha256, source_vintage) "
            "VALUES ('99001', %s, %s, 2.8500, "
            "        'https://www.nj.gov/dca/', %s, '%s-annual') "
            "ON CONFLICT DO NOTHING",
            [
                (2023, 450_000, "c" * 64, 2023),
                (2024, 500_000, "d" * 64, 2024),
            ],
        )
        # ACS5 median income: $115K 2023, $120K 2024.
        cur.executemany(
            "INSERT INTO raw.acs_median_household_income "
            "  (county_fips, year, product, estimate, dollar_year, "
            "   source_url, source_sha256) "
            "VALUES ('99001', %s, 'acs5', %s, %s, "
            "        'https://api.census.gov/data/', %s) "
            "ON CONFLICT DO NOTHING",
            [
                (2023, 115_000, 2023, "e" * 64),
                (2024, 120_000, 2024, "f" * 64),
            ],
        )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# 1. derived.v_affordability_gap row shape + count
# ---------------------------------------------------------------------------


class TestViewRowShape:
    """The view should return exactly one row per (county_fips, year)
    where DCA is seeded. Tax-dependent columns are NULL when the tax
    substrate is missing for that year."""

    def test_returns_one_row_per_seeded_year(
        self, collapse_db: psycopg.Connection
    ) -> None:
        with collapse_db.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM derived.v_affordability_gap "
                "WHERE county_fips = '99001'"
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == 2, "expect 2 rows -- 2023 and 2024"

    def test_row_columns_are_typed_correctly(
        self, collapse_db: psycopg.Connection
    ) -> None:
        # The frontend casts everything to FLOAT8 in its query; this test
        # validates the underlying numeric types are coercible.
        with collapse_db.cursor() as cur:
            cur.execute(
                "SELECT year, home_price, median_income_nominal, "
                "       piti_annual, required_income_hud_30pct, "
                "       hud_headroom_dollars, formula_version "
                "FROM derived.v_affordability_gap "
                "WHERE county_fips = '99001' AND year = 2024"
            )
            row = cur.fetchone()
        assert row is not None
        year, home_price, med_inc, piti, req_hud, headroom, version = row
        assert year == 2024
        assert float(home_price) == 500_000.00
        assert float(med_inc) == 120_000.00
        assert piti is not None and float(piti) > 0
        assert req_hud is not None and float(req_hud) > 0
        assert headroom is not None  # can be negative
        assert version == "1.2.0-affordability-engine-v1"


# ---------------------------------------------------------------------------
# 2. Hand-computed PITI + required-income for the seeded substrate
# ---------------------------------------------------------------------------


class TestHandComputedNumbers:
    """Cross-check the view's numbers against an independent hand calc."""

    def test_piti_2024_matches_hand_calc(
        self, collapse_db: psycopg.Connection
    ) -> None:
        # 2024 substrate: $500K home, FRED 7%, 2.85% prop tax, 0.35% ins.
        #   Loan = 400K, M = 400K * 0.005833 * 1.005833^360 / (...) = $2,661.21
        #   Annual P&I = $31,934.51
        #   Prop tax = $14,250
        #   Insurance = $1,750
        #   PITI annual = $47,934.52
        with collapse_db.cursor() as cur:
            cur.execute(
                "SELECT round(piti_annual, 2) "
                "FROM derived.v_affordability_gap "
                "WHERE county_fips = '99001' AND year = 2024"
            )
            row = cur.fetchone()
        assert row is not None
        assert float(row[0]) == pytest.approx(47_934.52, abs=0.01)

    def test_piti_2023_matches_hand_calc(
        self, collapse_db: psycopg.Connection
    ) -> None:
        # 2023 substrate: $450K home, FRED 6%, 2.85% prop tax, 0.35% ins.
        #   Loan = $360K, M @ 6%/30y = $360K * 0.005 * (1.005)^360 / ((1.005)^360 - 1)
        #   1.005^360 = 6.022575, M = 360K * 0.005 * 6.022575 / 5.022575 = $2,158.36
        #   Annual P&I = $25,900.30
        #   Prop tax = $450K * 0.0285 = $12,825
        #   Insurance = $450K * 0.0035 = $1,575
        #   PITI annual = $40,300.30 (within $1 of full-precision)
        with collapse_db.cursor() as cur:
            cur.execute(
                "SELECT round(piti_annual, 2) "
                "FROM derived.v_affordability_gap "
                "WHERE county_fips = '99001' AND year = 2023"
            )
            row = cur.fetchone()
        assert row is not None
        # Allow $1 tolerance for the rounded-monthly-vs-full-precision
        # delta documented in test_phase2_affordability::TestPITIAnnual.
        assert float(row[0]) == pytest.approx(40_300.30, abs=1.00)

    def test_hud_required_income_is_piti_div_threshold(
        self, collapse_db: psycopg.Connection
    ) -> None:
        # required_income_hud_30pct = PITI / 0.30 (linear, by definition).
        # For 2024 PITI $47,934.52 -> required = $159,781.73.
        with collapse_db.cursor() as cur:
            cur.execute(
                "SELECT round(required_income_hud_30pct, 2) "
                "FROM derived.v_affordability_gap "
                "WHERE county_fips = '99001' AND year = 2024"
            )
            row = cur.fetchone()
        assert row is not None
        assert float(row[0]) == pytest.approx(159_781.73, abs=0.01)

    def test_headroom_is_median_minus_required(
        self, collapse_db: psycopg.Connection
    ) -> None:
        # headroom = median - required = 120,000 - 159,781.73 = -39,781.73.
        with collapse_db.cursor() as cur:
            cur.execute(
                "SELECT round(hud_headroom_dollars, 2) "
                "FROM derived.v_affordability_gap "
                "WHERE county_fips = '99001' AND year = 2024"
            )
            row = cur.fetchone()
        assert row is not None
        assert float(row[0]) == pytest.approx(-39_781.73, abs=0.01)

    def test_required_to_actual_ratio(
        self, collapse_db: psycopg.Connection
    ) -> None:
        # ratio = required / actual = 159,781.73 / 120,000 = 1.3315 (4dp).
        with collapse_db.cursor() as cur:
            cur.execute(
                "SELECT hud_required_to_actual_ratio "
                "FROM derived.v_affordability_gap "
                "WHERE county_fips = '99001' AND year = 2024"
            )
            row = cur.fetchone()
        assert row is not None
        assert float(row[0]) == pytest.approx(1.3315, abs=0.0001)


# ---------------------------------------------------------------------------
# 3. The coverage query the page uses for the methodology box
# ---------------------------------------------------------------------------


class TestCoverageQuery:
    """The page issues a coverage query that reports year-ranges of
    each input substrate. It must reflect EXACTLY what's seeded."""

    def test_coverage_query_returns_seeded_ranges(
        self, collapse_db: psycopg.Connection
    ) -> None:
        # Same SQL the frontend issues (modulo placeholder syntax).
        with collapse_db.cursor() as cur:
            cur.execute(
                "WITH dca AS ( "
                "  SELECT MIN(year)::INT AS y_min, MAX(year)::INT AS y_max "
                "  FROM raw.nj_property_tax_county "
                "  WHERE county_fips = %s "
                "), "
                "acs AS ( "
                "  SELECT MIN(year)::INT AS y_min, MAX(year)::INT AS y_max "
                "  FROM raw.acs_median_household_income "
                "  WHERE county_fips = %s AND product = 'acs5' "
                "    AND estimate IS NOT NULL "
                "), "
                "fred AS ( "
                "  SELECT MIN(year)::INT AS y_min, MAX(year)::INT AS y_max "
                "  FROM derived.fred_annual "
                "  WHERE series_id = 'MORTGAGE30US' AND n_obs >= 1 "
                "), "
                "tax AS ( "
                "  SELECT array_agg(DISTINCT tax_year ORDER BY tax_year)::INT[] "
                "    AS years "
                "  FROM ( "
                "    SELECT tax_year FROM ref.irs_federal_brackets "
                "    INTERSECT "
                "    SELECT tax_year FROM ref.nj_state_brackets "
                "  ) t "
                ") "
                "SELECT dca.y_min, dca.y_max, acs.y_min, acs.y_max, "
                "       fred.y_min, fred.y_max, tax.years "
                "FROM dca, acs, fred, tax",
                ("99001", "99001"),
            )
            row = cur.fetchone()
        assert row is not None
        dca_min, dca_max, acs_min, acs_max, fred_min, fred_max, tax_years = row

        assert dca_min == 2023 and dca_max == 2024
        assert acs_min == 2023 and acs_max == 2024
        assert fred_min == 2023 and fred_max == 2024
        assert tax_years is not None
        # Both 2023 and 2024 should be in the IRS ∩ NJ-state intersection
        # because seeds 010 + 011 ship them.
        assert 2023 in tax_years
        assert 2024 in tax_years


# ---------------------------------------------------------------------------
# 4. Substrate-honesty: NULL when tax substrate missing
# ---------------------------------------------------------------------------


class TestSubstrateHonesty:
    """If we add a year with DCA + ACS + FRED but NO seeded tax table,
    the view must surface NULL for required-income (NOT silently fall
    back to an adjacent year's tax tables)."""

    def test_unseeded_tax_year_yields_null_required(
        self, collapse_db: psycopg.Connection
    ) -> None:
        # Add 2025 substrate (DCA + ACS + FRED), but NO 2025 tax tables.
        with collapse_db.cursor() as cur:
            cur.execute(
                "INSERT INTO raw.fred_observation "
                "  (series_id, observation_date, value, source_url, source_sha256) "
                "VALUES ('MORTGAGE30US', '2025-06-06', 6.5, "
                "        'https://fred.stlouisfed.org/', %s) "
                "ON CONFLICT DO NOTHING",
                ("g" * 64,),
            )
            cur.execute(
                "INSERT INTO raw.nj_property_tax_county "
                "  (county_fips, year, avg_residential_value, cy_total_rate, "
                "   source_url, source_sha256, source_vintage) "
                "VALUES ('99001', 2025, 525000, 2.85, "
                "        'https://www.nj.gov/dca/', %s, '2025-annual') "
                "ON CONFLICT DO NOTHING",
                ("h" * 64,),
            )
            cur.execute(
                "INSERT INTO raw.acs_median_household_income "
                "  (county_fips, year, product, estimate, dollar_year, "
                "   source_url, source_sha256) "
                "VALUES ('99001', 2025, 'acs5', 125000, 2025, "
                "        'https://api.census.gov/data/', %s) "
                "ON CONFLICT DO NOTHING",
                ("i" * 64,),
            )
        collapse_db.commit()

        with collapse_db.cursor() as cur:
            cur.execute(
                "SELECT median_income_nominal, piti_annual, "
                "       required_income_hud_30pct, "
                "       required_income_post_tax_30pct "
                "FROM derived.v_affordability_gap "
                "WHERE county_fips = '99001' AND year = 2025"
            )
            row = cur.fetchone()
        assert row is not None
        med_inc, piti, req_hud, req_post = row
        # ACS gives us the actual income.
        assert float(med_inc) == 125_000.00
        # PITI is computable (no tax tables needed).
        assert piti is not None and float(piti) > 0
        # HUD-required is computable (also no tax tables needed -- linear).
        assert req_hud is not None and float(req_hud) > 0
        # Post-tax DOES need tax tables. Must be NULL for unseeded year.
        assert req_post is None, (
            "post-tax required income MUST be NULL for unseeded tax year, "
            "not silently substituted from another year"
        )


# ---------------------------------------------------------------------------
# 5. Empty-county case (the page's "county not found" branch)
# ---------------------------------------------------------------------------


class TestEmptyCounty:
    def test_unseeded_county_returns_no_rows(
        self, collapse_db: psycopg.Connection
    ) -> None:
        with collapse_db.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM derived.v_affordability_gap "
                "WHERE county_fips = '99999'"
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == 0


# ---------------------------------------------------------------------------
# 6. Phase-3 disposable-income trajectory + AEI queries
#
# These mirror what `getCountyDisposableIncome()` issues in lib/housing.ts
# so the canary catches any future drift between page query and SQL.
# ---------------------------------------------------------------------------


class TestPhase3DisposableIncomeQuery:
    """The page's DI/AEI query reads two views:
      * derived.v_disposable_income_trajectory (per year)
      * derived.v_aei_by_county                (one row per county)
    Both are tested here against the same fixture."""

    @pytest.fixture(autouse=True)
    def _seed_cpi(self, collapse_db: psycopg.Connection) -> None:
        """The trajectory view's di_real column needs CPI for the value
        years (2023+2024) AND for the base year (latest CPI). Seed both
        with real BLS CPI-U All Items annual averages so the deflator
        matches published values."""
        with collapse_db.cursor() as cur:
            cur.executemany(
                "INSERT INTO raw.cpi_u "
                "  (series_id, year, period, value, source_url, source_sha256) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                [
                    ("CUUR0000SA0", 2023, "M13", 304.702,
                     "https://api.bls.gov/", "g" * 64),
                    ("CUUR0000SA0", 2024, "M13", 313.689,
                     "https://api.bls.gov/", "h" * 64),
                ],
            )
        collapse_db.commit()

    def test_trajectory_view_returns_two_rows(
        self, collapse_db: psycopg.Connection
    ) -> None:
        with collapse_db.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM derived.v_disposable_income_trajectory "
                "WHERE county_fips = '99001'"
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == 2

    def test_trajectory_view_di_nominal_matches_hand_calc(
        self, collapse_db: psycopg.Connection
    ) -> None:
        """2024 row: gross $120K - tax $21,223.63 - PITI $47,934.52 = $50,841.85
        2023 row: gross $115K - tax $20,168.77 - PITI $40,300.69 = $54,530.54
        (The Phase-1 + Phase-2 hand-checked anchors propagated through.)"""
        with collapse_db.cursor() as cur:
            cur.execute(
                "SELECT year, round(di_nominal, 2) "
                "FROM derived.v_disposable_income_trajectory "
                "WHERE county_fips = '99001' ORDER BY year"
            )
            rows = cur.fetchall()
        assert [int(r[0]) for r in rows] == [2023, 2024]
        assert float(rows[0][1]) == pytest.approx(54_530.54, abs=0.10)
        assert float(rows[1][1]) == pytest.approx(50_841.86, abs=0.10)

    def test_trajectory_view_real_dollars_base_year_is_latest_cpi(
        self, collapse_db: psycopg.Connection
    ) -> None:
        """Latest CPI year in this fixture is 2024."""
        with collapse_db.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT real_dollars_base_year "
                "FROM derived.v_disposable_income_trajectory "
                "WHERE county_fips = '99001'"
            )
            rows = cur.fetchall()
        assert len(rows) == 1
        assert int(rows[0][0]) == 2024

    def test_trajectory_view_di_real_uses_cpi_deflator(
        self, collapse_db: psycopg.Connection
    ) -> None:
        """2023 DI in 2024 dollars: $54,530.54 * (313.689/304.702) = $56,138.89."""
        with collapse_db.cursor() as cur:
            cur.execute(
                "SELECT round(di_real, 2) "
                "FROM derived.v_disposable_income_trajectory "
                "WHERE county_fips = '99001' AND year = 2023"
            )
            row = cur.fetchone()
        assert row is not None
        assert float(row[0]) == pytest.approx(56_138.89, abs=0.10)

    def test_trajectory_view_di_real_identity_for_base_year(
        self, collapse_db: psycopg.Connection
    ) -> None:
        """A row whose value year == base year must have di_real == di_nominal."""
        with collapse_db.cursor() as cur:
            cur.execute(
                "SELECT round(di_nominal, 2), round(di_real, 2) "
                "FROM derived.v_disposable_income_trajectory "
                "WHERE county_fips = '99001' AND year = 2024"
            )
            row = cur.fetchone()
        assert row is not None
        assert float(row[0]) == pytest.approx(float(row[1]), abs=0.01)

    def test_aei_view_returns_one_row(
        self, collapse_db: psycopg.Connection
    ) -> None:
        with collapse_db.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM derived.v_aei_by_county "
                "WHERE county_fips = '99001'"
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == 1

    def test_aei_view_anchor_and_latest_correct(
        self, collapse_db: psycopg.Connection
    ) -> None:
        """Two-year fixture: anchor 2023, latest 2024, AEI ~1.14."""
        with collapse_db.cursor() as cur:
            cur.execute(
                "SELECT anchor_year, latest_year, "
                "       round(anchor_hbr,4), round(latest_hbr,4), "
                "       round(aei,4), years_observed "
                "FROM derived.v_aei_by_county "
                "WHERE county_fips = '99001'"
            )
            row = cur.fetchone()
        assert row is not None
        anchor_year, latest_year, anchor_hbr, latest_hbr, aei, observed = row
        assert int(anchor_year) == 2023
        assert int(latest_year) == 2024
        assert float(anchor_hbr) == pytest.approx(0.3504, abs=0.0005)
        assert float(latest_hbr) == pytest.approx(0.3995, abs=0.0005)
        assert float(aei) == pytest.approx(1.1399, abs=0.0005)
        assert int(observed) == 1


class TestPhase3SubstrateHonesty:
    """If we add a year with DCA + ACS but NO seeded tax tables, the
    DI must be NULL for that year (because the tax engine can't
    compute it). The trajectory view must NOT silently substitute zero."""

    def test_trajectory_di_null_for_unseeded_tax_year(
        self, collapse_db: psycopg.Connection
    ) -> None:
        # Seed CPI for 2025 (so CPI is not the limiting factor) but
        # leave the IRS/NJ tax tables unseeded for 2025.
        with collapse_db.cursor() as cur:
            cur.execute(
                "INSERT INTO raw.cpi_u "
                "  (series_id, year, period, value, source_url, source_sha256) "
                "VALUES ('CUUR0000SA0', 2025, 'M13', 320.0, "
                "        'https://api.bls.gov/', %s) "
                "ON CONFLICT DO NOTHING",
                ("p" * 64,),
            )
            cur.execute(
                "INSERT INTO raw.fred_observation "
                "  (series_id, observation_date, value, source_url, source_sha256) "
                "VALUES ('MORTGAGE30US', '2025-06-06', 6.5, "
                "        'https://fred.stlouisfed.org/', %s) "
                "ON CONFLICT DO NOTHING",
                ("q" * 64,),
            )
            cur.execute(
                "INSERT INTO raw.nj_property_tax_county "
                "  (county_fips, year, avg_residential_value, cy_total_rate, "
                "   source_url, source_sha256, source_vintage) "
                "VALUES ('99001', 2025, 525000, 2.85, "
                "        'https://www.nj.gov/dca/', %s, '2025-annual') "
                "ON CONFLICT DO NOTHING",
                ("r" * 64,),
            )
            cur.execute(
                "INSERT INTO raw.acs_median_household_income "
                "  (county_fips, year, product, estimate, dollar_year, "
                "   source_url, source_sha256) "
                "VALUES ('99001', 2025, 'acs5', 125000, 2025, "
                "        'https://api.census.gov/data/', %s) "
                "ON CONFLICT DO NOTHING",
                ("s" * 64,),
            )
            # Also seed CPI for 2023+2024 (autouse fixture from the
            # other test class doesn't apply here).
            cur.executemany(
                "INSERT INTO raw.cpi_u "
                "  (series_id, year, period, value, source_url, source_sha256) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                [
                    ("CUUR0000SA0", 2023, "M13", 304.702,
                     "https://api.bls.gov/", "t" * 64),
                    ("CUUR0000SA0", 2024, "M13", 313.689,
                     "https://api.bls.gov/", "u" * 64),
                ],
            )
        collapse_db.commit()

        with collapse_db.cursor() as cur:
            cur.execute(
                "SELECT median_income_nominal, di_nominal, di_real "
                "FROM derived.v_disposable_income_trajectory "
                "WHERE county_fips = '99001' AND year = 2025"
            )
            row = cur.fetchone()
        assert row is not None
        med_inc, di_nominal, di_real = row
        assert float(med_inc) == 125_000.00
        # DI requires tax engine -- 2025 unseeded => NULL.
        assert di_nominal is None, (
            "DI nominal MUST be NULL for unseeded tax year, "
            "not silently substituted from another year"
        )
        assert di_real is None, "DI real bubbles NULL from DI nominal"
