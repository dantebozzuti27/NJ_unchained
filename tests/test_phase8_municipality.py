"""Live-PG tests for the Phase 8a municipality drill-down (migrations
077 + 078).

Every assertion is a HAND-COMPUTED muni-level PITI, max-affordable, or
verdict value; we pin them to specific dollar amounts so an auditor can
re-derive each one from the closed-form math in migration 078.

Functions under test:

* derived.f_muni_avg_home_price       (raw.nj_property_tax_muni lookup)
* derived.f_muni_property_tax_rate    (cy_total_rate / 100)
* derived.f_piti_annual_muni          (P&I + muni prop_tax + insurance)
* derived.f_piti_coefficient_muni     (PITI per $1 home, closed form)
* derived.f_user_max_affordable_home_price_dti_muni
* derived.f_user_max_affordable_home_price_post_tax_muni
* derived.f_user_required_income_for_home_muni
* derived.f_user_town_verdict_muni
* derived.f_user_nj_muni_verdicts
* derived.v_muni_affordability_gap

Cross-engine invariant: when a muni's avg_residential_value AND
cy_total_rate equal the surrounding county's values, the muni functions
must agree with the county functions to the cent (because the only
substrate that differs is the property-tax rate, which is identical
under that fixture). This is the strongest single test of the
composition strategy: muni-level is county-level with a different lookup.

Substrate-honesty: every "muni not seeded" / "year not seeded" /
"unknown muni" path is pinned to NULL. The platform never silently
substitutes a county-level value when a muni-level one is missing.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import psycopg

pytestmark = pytest.mark.live_pg


# ============================================================================
# Fixture: fully-migrated + fully-seeded DB plus synthetic muni-year rows
#
# We anchor on REAL seeded munis (Bergen 0201 Allendale Borough, Bergen
# 0204 Bogota Borough, Burlington 0301 Bass River Township) so the
# raw.nj_property_tax_muni FK to ref.nj_municipality is satisfied with
# zero hand-rolled muni-dim setup.
#
# Bergen 0201 ('Allendale') is loaded with the SAME (avg_value=500000,
# rate=2.85%) substrate as the surrounding Bergen county summary, so the
# muni-vs-county invariant test bites if the muni-level engine ever
# silently falls back to county.
#
# Bergen 0204 ('Bogota') is loaded with a DIFFERENT (avg_value=480000,
# rate=3.20%) substrate so the muni-specific lookup is verified.
# ============================================================================


@pytest.fixture
def phase8_db(live_pg: psycopg.Connection) -> psycopg.Connection:
    from scripts.migrate import (
        MIGRATIONS_DIR,
        SEEDS_DIR,
        apply_migrations,
        discover,
    )

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

    with conn.cursor() as cur:
        # Shared substrate the muni engine reads through.
        cur.execute(
            "INSERT INTO raw.fred_observation "
            "  (series_id, observation_date, value, source_url, source_sha256) "
            "VALUES ('MORTGAGE30US', '2024-06-06', 7.0000, "
            "        'https://fred.stlouisfed.org/', %s) "
            "ON CONFLICT DO NOTHING",
            ("a" * 64,),
        )

        # Bergen county summary at SAME ($500K, 2.85%) values as the
        # 0201 muni below -- the cross-engine invariant relies on this.
        cur.execute(
            "INSERT INTO raw.nj_property_tax_county "
            "  (county_fips, year, avg_residential_value, cy_total_rate, "
            "   source_url, source_sha256, source_vintage) "
            "VALUES ('34003', 2024, 500000, 2.8500, "
            "        'https://www.nj.gov/dca/divisions/dlgs/', %s, '2024-annual') "
            "ON CONFLICT DO NOTHING",
            ("b" * 64,),
        )

        # ACS5 median income for Bergen county.
        cur.execute(
            "INSERT INTO raw.acs_median_household_income "
            "  (county_fips, year, product, estimate, dollar_year, "
            "   source_url, source_sha256) "
            "VALUES ('34003', 2024, 'acs5', 120000, 2024, "
            "        'https://api.census.gov/data/2024/acs/acs5', %s) "
            "ON CONFLICT DO NOTHING",
            ("c" * 64,),
        )

        # 0201 Allendale Borough -- IDENTICAL substrate to the Bergen
        # county summary. PITI / coefficient functions MUST agree with
        # the county-level engine here.
        cur.execute(
            "INSERT INTO raw.nj_property_tax_muni "
            "  (muni_code, year, avg_residential_value, cy_total_rate, "
            "   source_url, source_sha256, source_vintage) "
            "VALUES ('0201', 2024, 500000, 2.8500, "
            "        'https://www.nj.gov/dca/divisions/dlgs/', %s, '2024-annual') "
            "ON CONFLICT DO NOTHING",
            ("d" * 64,),
        )

        # 0204 Bogota Borough -- DIFFERENT substrate (cheaper homes,
        # higher tax rate) so muni-specific behavior is exercised.
        cur.execute(
            "INSERT INTO raw.nj_property_tax_muni "
            "  (muni_code, year, avg_residential_value, cy_total_rate, "
            "   source_url, source_sha256, source_vintage) "
            "VALUES ('0204', 2024, 480000, 3.2000, "
            "        'https://www.nj.gov/dca/divisions/dlgs/', %s, '2024-annual') "
            "ON CONFLICT DO NOTHING",
            ("e" * 64,),
        )

    conn.commit()
    return conn


def _scalar(conn: psycopg.Connection, sql: str, *params: object) -> object:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _approx_dec(
    actual: object, expected: str | Decimal, *, abs_tol: str = "0.50"
) -> None:
    a = Decimal(str(actual))
    e = Decimal(str(expected))
    assert abs(a - e) <= Decimal(abs_tol), f"expected {e} +/- {abs_tol}, got {a}"


# ============================================================================
# Hand-computed anchors (same shape as Phase 4 §138-171 but at muni level)
#
# Allendale (0201) -- IDENTICAL substrate to Bergen county synthetic ($500K,
# 2.85%); FRED 7%, 20% down, 30y, ins 0.35%:
#   monthly_PI on $400K loan @ 7%/30y = $2,661.21 (Phase 4 anchor)
#   annual P&I = $31,934.52
#   prop tax  = $14,250
#   insurance = $1,750
#   PITI      = $47,934.52  -- MUST match county-level f_piti_annual exactly
#   c         = 0.095869   -- MUST match county-level f_piti_coefficient
#
# Bogota (0204) -- DIFFERENT substrate ($480K, 3.20%):
#   loan = 480000 * 0.80 = 384000
#   monthly_PI(7%/30y) on $384K = 2661.21 * 0.96 = $2,554.76 (component basis)
#   annual P&I + prop tax + insurance summed at full Postgres NUMERIC
#   precision yields PITI = $47,697.14 (NOT the abbreviated $47,697.16
#   that hand-rounding the components would produce -- the engine uses
#   the closed-form annuity factor extracted via f_mortgage_pi_monthly($1)).
#   c = 47697.13897... / 480000 = 0.09936903953... (full Postgres precision)
#
# For $200K MFJ-1-1 user, no other debt, default 28/36 DTI, in 0204:
#   front_cap = 0.28 * 200000 / c = 56000 / 0.09936903953 = $563,555.81
#   back_cap  = 0.36 * 200000 / c = 72000 / 0.09936903953 = $724,571.76
#   max_dti   = $563,555.81 (front-end binds)
#
# For $80K MFJ-1-1 user in 0204:
#   front_cap = 0.28 * 80000 / c = 22400 / 0.09936903953 = $225,422.33
#   median home $480K vs $225K max -> ratio 2.13 -> 'out_of_reach'
#
# Required income for $480K home in 0204:
#   PITI = $47,697.14; front: $47,697.14 / 0.28 = $170,346.92
#                       back:  $47,697.14 / 0.36 = $132,492.05
#   required = max = $170,346.92
#
# HUD-style required income for $480K Bogota:
#   $47,697.14 / 0.30 = $158,990.46
# ============================================================================


# ============================================================================
# 1. f_muni_avg_home_price + f_muni_property_tax_rate -- raw lookups
# ============================================================================


class TestMuniLookups:

    def test_avg_home_price_seeded_muni_year(
        self, phase8_db: psycopg.Connection
    ) -> None:
        out = _scalar(
            phase8_db,
            "SELECT derived.f_muni_avg_home_price(%s, 2024::SMALLINT)",
            "0204",
        )
        _approx_dec(out, "480000")

    def test_avg_home_price_other_muni_unaffected(
        self, phase8_db: psycopg.Connection
    ) -> None:
        """Sanity: 0201 and 0204 have different seeded values; the
        function must return each one independently (no cross-row
        bleed)."""
        out = _scalar(
            phase8_db,
            "SELECT derived.f_muni_avg_home_price(%s, 2024::SMALLINT)",
            "0201",
        )
        _approx_dec(out, "500000")

    def test_avg_home_price_unseeded_year_is_null(
        self, phase8_db: psycopg.Connection
    ) -> None:
        out = _scalar(
            phase8_db,
            "SELECT derived.f_muni_avg_home_price(%s, 2023::SMALLINT)",
            "0201",
        )
        assert out is None

    def test_avg_home_price_unknown_muni_is_null(
        self, phase8_db: psycopg.Connection
    ) -> None:
        """Unseeded muni_code (a CHECK-valid code that is not in any
        Bergen muni_code list) returns NULL -- substrate honesty.
        Note: the FK on raw to ref means we cannot SELECT a row that
        doesn't exist, so the lookup just yields no rows -> NULL."""
        out = _scalar(
            phase8_db,
            "SELECT derived.f_muni_avg_home_price(%s, 2024::SMALLINT)",
            "0271",  # past Bergen's max (0270); not in seed
        )
        assert out is None

    def test_property_tax_rate_seeded_muni(
        self, phase8_db: psycopg.Connection
    ) -> None:
        """3.20% on disk -> 0.032 returned as decimal (matches the rest
        of the platform's rate-as-decimal convention)."""
        out = _scalar(
            phase8_db,
            "SELECT derived.f_muni_property_tax_rate(%s, 2024::SMALLINT)",
            "0204",
        )
        _approx_dec(out, "0.032000", abs_tol="0.000001")

    def test_property_tax_rate_unseeded_year_is_null(
        self, phase8_db: psycopg.Connection
    ) -> None:
        out = _scalar(
            phase8_db,
            "SELECT derived.f_muni_property_tax_rate(%s, 2023::SMALLINT)",
            "0204",
        )
        assert out is None


# ============================================================================
# 2. f_piti_annual_muni / f_piti_coefficient_muni -- composition + invariant
# ============================================================================


class TestMuniPITI:

    def test_piti_on_500k_in_allendale_matches_county_engine(
        self, phase8_db: psycopg.Connection
    ) -> None:
        """The cross-engine invariant: when a muni's substrate matches
        its surrounding county's substrate, f_piti_annual_muni must
        return the SAME value as f_piti_annual to the cent. If this
        ever drifts, the muni engine is silently doing something the
        county engine is not."""
        muni_piti = _scalar(
            phase8_db,
            "SELECT derived.f_piti_annual_muni(%s, 2024::SMALLINT, %s)",
            Decimal("500000"), "0201",
        )
        county_piti = _scalar(
            phase8_db,
            "SELECT derived.f_piti_annual(%s, 2024::SMALLINT, %s)",
            Decimal("500000"), "34003",
        )
        a = Decimal(str(muni_piti))
        e = Decimal(str(county_piti))
        assert abs(a - e) < Decimal("0.01"), \
            f"muni PITI={a} vs county PITI={e}, drift={abs(a-e)}"

    def test_piti_on_500k_in_allendale_matches_phase4_anchor(
        self, phase8_db: psycopg.Connection
    ) -> None:
        """The Phase 4 anchor: $500K @ 2.85% / 7% / 20% / 30y = $47,934.52."""
        out = _scalar(
            phase8_db,
            "SELECT derived.f_piti_annual_muni(%s, 2024::SMALLINT, %s)",
            Decimal("500000"), "0201",
        )
        _approx_dec(out, "47934.52")

    def test_piti_on_480k_in_bogota(
        self, phase8_db: psycopg.Connection
    ) -> None:
        """Bogota has 3.20% rate vs Allendale's 2.85%; PITI on 480K
        decomposes to $30,657.13 + $15,360 + $1,680 = $47,697.14
        at full Postgres NUMERIC precision."""
        out = _scalar(
            phase8_db,
            "SELECT derived.f_piti_annual_muni(%s, 2024::SMALLINT, %s)",
            Decimal("480000"), "0204",
        )
        _approx_dec(out, "47697.14")

    def test_coefficient_matches_piti_via_component_sum_in_bogota(
        self, phase8_db: psycopg.Connection
    ) -> None:
        """The fundamental closed-form identity: PITI(H) = H * c.
        f_piti_coefficient_muni and f_piti_annual_muni must agree at
        ANY home price. If they drift the personalization engine is
        silently broken."""
        with phase8_db.cursor() as cur:
            cur.execute(
                "SELECT derived.f_piti_coefficient_muni(2024::SMALLINT, %s)",
                ("0204",),
            )
            row = cur.fetchone()
            assert row is not None
            c = Decimal(str(row[0]))
            cur.execute(
                "SELECT derived.f_piti_annual_muni(%s, 2024::SMALLINT, %s)",
                (Decimal("480000"), "0204"),
            )
            row = cur.fetchone()
            assert row is not None
            piti = Decimal(str(row[0]))
        diff = abs(Decimal("480000") * c - piti)
        assert diff < Decimal("0.01"), \
            f"480000*c={Decimal('480000')*c} vs PITI_muni={piti}, diff={diff}"

    def test_coefficient_in_bogota_is_about_0_0994(
        self, phase8_db: psycopg.Connection
    ) -> None:
        """c = 47697.14 / 480000 = 0.099369. Sanity-check the magnitude
        so an off-by-decimal drift in the annuity-factor extraction
        fails loudly. Tighter tolerance for the closed-form math: 1e-6."""
        c = _scalar(
            phase8_db,
            "SELECT round(derived.f_piti_coefficient_muni(2024::SMALLINT, %s), 6)",
            "0204",
        )
        _approx_dec(c, "0.099369", abs_tol="0.000002")

    def test_piti_on_unseeded_year_is_null(
        self, phase8_db: psycopg.Connection
    ) -> None:
        """No 2023 row -> prop_tax_rate is NULL -> PITI bubbles NULL."""
        out = _scalar(
            phase8_db,
            "SELECT derived.f_piti_annual_muni(%s, 2023::SMALLINT, %s)",
            Decimal("480000"), "0204",
        )
        assert out is None


# ============================================================================
# 3. f_user_max_affordable_home_price_dti_muni -- closed-form gross DTI
# ============================================================================


class TestMuniMaxAffordableDTI:

    def test_200k_user_in_bogota(
        self, phase8_db: psycopg.Connection
    ) -> None:
        """$200K user, 28% / 36% DTI default; in Bogota (c = 0.0993690)
        the front-cap binds at $563,555.81."""
        out = _scalar(
            phase8_db,
            "SELECT derived.f_user_max_affordable_home_price_dti_muni("
            "  %s, 2024::SMALLINT, %s)",
            Decimal("200000"), "0204",
        )
        _approx_dec(out, "563555.81", abs_tol="0.10")

    def test_80k_user_in_bogota_floors_lower(
        self, phase8_db: psycopg.Connection
    ) -> None:
        """$80K user in Bogota (c = 0.0993690): max = 0.28*80000/c = $225,422.33."""
        out = _scalar(
            phase8_db,
            "SELECT derived.f_user_max_affordable_home_price_dti_muni("
            "  %s, 2024::SMALLINT, %s)",
            Decimal("80000"), "0204",
        )
        _approx_dec(out, "225422.33", abs_tol="0.10")

    def test_zero_gross_returns_null(
        self, phase8_db: psycopg.Connection
    ) -> None:
        out = _scalar(
            phase8_db,
            "SELECT derived.f_user_max_affordable_home_price_dti_muni("
            "  0::NUMERIC, 2024::SMALLINT, %s)",
            "0204",
        )
        assert out is None

    def test_unknown_muni_returns_null(
        self, phase8_db: psycopg.Connection
    ) -> None:
        out = _scalar(
            phase8_db,
            "SELECT derived.f_user_max_affordable_home_price_dti_muni("
            "  %s, 2024::SMALLINT, %s)",
            Decimal("100000"), "0271",
        )
        assert out is None


# ============================================================================
# 4. f_user_required_income_for_home_muni
# ============================================================================


class TestMuniRequiredIncome:

    def test_required_income_for_480k_in_bogota(
        self, phase8_db: psycopg.Connection
    ) -> None:
        """For a $480K home in Bogota, PITI = $47,697.14. Required
        gross under default DTI = max(PITI/0.28, PITI/0.36)
        = max(170346.92, 132492.05) = $170,346.92."""
        out = _scalar(
            phase8_db,
            "SELECT derived.f_user_required_income_for_home_muni("
            "  %s, 2024::SMALLINT, %s)",
            Decimal("480000"), "0204",
        )
        _approx_dec(out, "170346.92", abs_tol="0.10")

    def test_required_income_unknown_muni_is_null(
        self, phase8_db: psycopg.Connection
    ) -> None:
        out = _scalar(
            phase8_db,
            "SELECT derived.f_user_required_income_for_home_muni("
            "  %s, 2024::SMALLINT, %s)",
            Decimal("400000"), "0271",
        )
        assert out is None


# ============================================================================
# 5. f_user_town_verdict_muni -- composite per-muni verdict
# ============================================================================


class TestMuniTownVerdict:

    def test_200k_mfj_verdict_in_bogota(
        self, phase8_db: psycopg.Connection
    ) -> None:
        """Bogota $480K vs $200K MFJ-1-1 user max-DTI=$563,555.81:
            ratio 480/563.6 = 0.852 -> 'affordable' (under cap)."""
        with phase8_db.cursor() as cur:
            cur.execute(
                "SELECT median_home_price, max_affordable_dti, "
                "       verdict_dti, gross_income_gap "
                "FROM derived.f_user_town_verdict_muni("
                "  2024::SMALLINT, %s, %s, %s, %s, %s)",
                ("0204", Decimal("200000"), "mfj", 1, 1),
            )
            row = cur.fetchone()
        assert row is not None
        median, max_dti, verdict, gap = row
        _approx_dec(median, "480000")
        _approx_dec(max_dti, "563555.81", abs_tol="0.10")
        assert verdict == "affordable"
        # Required income for 480K Bogota = 170346.92 -> gap = -29653.08
        _approx_dec(gap, "-29653.08", abs_tol="0.10")

    def test_80k_user_in_bogota_is_out_of_reach(
        self, phase8_db: psycopg.Connection
    ) -> None:
        """$80K user in Bogota: max_dti=$225K, median=$480K, ratio
        2.13 -> 'out_of_reach' (above 1.25 stretch band)."""
        with phase8_db.cursor() as cur:
            cur.execute(
                "SELECT verdict_dti, gross_income_gap "
                "FROM derived.f_user_town_verdict_muni("
                "  2024::SMALLINT, %s, %s, %s, %s, %s)",
                ("0204", Decimal("80000"), "mfj", 1, 1),
            )
            row = cur.fetchone()
        assert row is not None
        verdict, gap = row
        assert verdict == "out_of_reach"
        # Gap = required - 80000 = $170,346.92 - $80,000 = $90,346.92 short.
        _approx_dec(gap, "90346.92", abs_tol="0.10")

    def test_verdict_for_unseeded_muni_year_is_null(
        self, phase8_db: psycopg.Connection
    ) -> None:
        """Unseeded year (2023) -> median is NULL -> all verdicts NULL.
        The function must not silently substitute a 2024 verdict."""
        with phase8_db.cursor() as cur:
            cur.execute(
                "SELECT median_home_price, max_affordable_dti, verdict_dti "
                "FROM derived.f_user_town_verdict_muni("
                "  2023::SMALLINT, %s, %s, %s, %s, %s)",
                ("0204", Decimal("200000"), "mfj", 1, 1),
            )
            row = cur.fetchone()
        assert row is not None
        median, max_dti, verdict = row
        assert median is None
        assert max_dti is None
        assert verdict is None


# ============================================================================
# 6. f_user_nj_muni_verdicts -- set-returning per-county convenience
# ============================================================================


class TestNjMuniVerdicts:

    def test_county_scoping_emits_only_county_munis(
        self, phase8_db: psycopg.Connection
    ) -> None:
        """The function MUST emit exactly the munis whose county_fips
        matches the requested one (Bergen has 70 munis in the seed).
        Counter-test that Burlington (40 munis) returns 40 rows."""
        with phase8_db.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM derived.f_user_nj_muni_verdicts("
                "  2024::SMALLINT, '34003', %s, %s, %s, %s)",
                (Decimal("200000"), "mfj", 1, 1),
            )
            row = cur.fetchone()
            assert row is not None
            (n_bergen,) = row
            assert n_bergen == 70, (
                f"Expected 70 Bergen munis, got {n_bergen}; ref.nj_municipality "
                "may have drifted from the 2024 DCA seed."
            )

            cur.execute(
                "SELECT count(*) FROM derived.f_user_nj_muni_verdicts("
                "  2024::SMALLINT, '34005', %s, %s, %s, %s)",
                (Decimal("200000"), "mfj", 1, 1),
            )
            row = cur.fetchone()
            assert row is not None
            (n_burlington,) = row
            assert n_burlington == 40

    def test_seeded_munis_have_concrete_verdicts(
        self, phase8_db: psycopg.Connection
    ) -> None:
        """Bergen has 70 munis but only 0201 + 0204 have raw substrate
        in the test fixture; the other 68 surface NULL median home
        (substrate honesty). This is the right shape for the
        /personalize muni-table UI: rows render in a coherent table
        with a "data not loaded" cell where applicable."""
        with phase8_db.cursor() as cur:
            cur.execute(
                "SELECT muni_code, muni_name, median_home_price, verdict_dti "
                "FROM derived.f_user_nj_muni_verdicts("
                "  2024::SMALLINT, '34003', %s, %s, %s, %s) "
                "WHERE median_home_price IS NOT NULL "
                "ORDER BY muni_code",
                (Decimal("200000"), "mfj", 1, 1),
            )
            rows = cur.fetchall()
        codes = {r[0] for r in rows}
        assert codes == {"0201", "0204"}, \
            f"Only seeded munis should have non-NULL median; got codes={codes}"
        for _code, name, median, verdict in rows:
            assert verdict in {"affordable", "stretch", "out_of_reach"}
            assert Decimal(str(median)) > 0
            assert isinstance(name, str) and len(name) > 0


# ============================================================================
# 7. v_muni_affordability_gap -- per-muni headline view
# ============================================================================


class TestMuniAffordabilityGap:

    def test_view_emits_one_row_per_seeded_muni_year(
        self, phase8_db: psycopg.Connection
    ) -> None:
        """Two muni rows seeded in 2024 -> view has exactly two rows."""
        with phase8_db.cursor() as cur:
            cur.execute("SELECT count(*) FROM derived.v_muni_affordability_gap")
            row = cur.fetchone()
            assert row is not None
            assert row[0] == 2

    def test_view_columns_for_bogota_are_internally_consistent(
        self, phase8_db: psycopg.Connection
    ) -> None:
        """home_price * 0.30 should equal hud-required income (linear
        HUD definition). View must not silently break this arithmetic."""
        with phase8_db.cursor() as cur:
            cur.execute(
                "SELECT muni_name, home_price, piti_annual, "
                "       required_income_hud_30pct, "
                "       county_median_income_nominal, "
                "       hud_headroom_dollars, formula_version "
                "FROM derived.v_muni_affordability_gap "
                "WHERE muni_code = %s AND year = 2024",
                ("0204",),
            )
            row = cur.fetchone()
        assert row is not None
        name, home, piti, req_hud, med_inc, headroom, version = row
        assert name == "Bogota Borough"
        _approx_dec(home, "480000")
        _approx_dec(piti, "47697.14", abs_tol="0.10")
        # HUD headline: required = PITI / 0.30 = $47,697.14 / 0.30 = $158,990.46.
        _approx_dec(req_hud, "158990.46", abs_tol="0.10")
        # county_median_income = 120000 (Bergen ACS5 from fixture).
        _approx_dec(med_inc, "120000")
        # headroom = 120000 - 158990.46 = -$38,990.46 (median falls short).
        _approx_dec(headroom, "-38990.46", abs_tol="0.10")
        assert version == "1.5.0-municipality-drill-down-v1"

    def test_view_handles_missing_acs_substrate(
        self, phase8_db: psycopg.Connection
    ) -> None:
        """The fixture seeds ACS for Bergen only; Burlington munis with
        no ACS join produce NULL income -> NULL headroom. We don't seed
        a Burlington muni in this fixture, so this is a structural pin
        only -- demonstrating the LEFT JOIN doesn't drop rows."""
        with phase8_db.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM derived.v_muni_affordability_gap "
                "WHERE county_fips = '34003'"
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == 2  # both seeded Bergen munis are present


# ============================================================================
# 8. Coverage views -- substrate-honesty surface
# ============================================================================


class TestCoverageViews:

    def test_municipality_coverage_per_county(
        self, phase8_db: psycopg.Connection
    ) -> None:
        """ref.v_nj_municipality_coverage emits one row per NJ county
        (21 rows). The seeded muni count by county matches the 2024
        DCA workbook; if the seed is ever truncated this fails."""
        with phase8_db.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM ref.v_nj_municipality_coverage "
                "WHERE n_munis > 0"
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == 21

            cur.execute(
                "SELECT n_munis FROM ref.v_nj_municipality_coverage "
                "WHERE county_fips = '34003'"
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == 70  # Bergen

    def test_property_tax_muni_coverage_pct(
        self, phase8_db: psycopg.Connection
    ) -> None:
        """The fixture seeds 2 of Bergen's 70 munis -> pct_loaded should
        be 2/70 = 2.86% for that (county, year)."""
        with phase8_db.cursor() as cur:
            cur.execute(
                "SELECT n_munis_loaded, n_munis_total, pct_loaded "
                "FROM raw.v_nj_property_tax_muni_coverage "
                "WHERE county_fips = '34003' AND year = 2024"
            )
            row = cur.fetchone()
        assert row is not None
        loaded, total, pct = row
        assert loaded == 2
        assert total == 70
        _approx_dec(pct, "2.86", abs_tol="0.01")
