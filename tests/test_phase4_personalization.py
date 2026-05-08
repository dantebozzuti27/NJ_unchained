"""Live-PG tests for the Phase-4 personalization engine (migration 074).

Every assertion is a HAND-COMPUTED max-affordable / required-income /
verdict value. We pin these to specific dollar amounts so an auditor
can re-derive each one from the closed-form math documented in the
migration header.

Functions under test:

* derived.f_piti_coefficient
    The "c" constant: PITI per dollar of home price for given
    (year, county, term, down, insurance). Closed form via $1-loan
    annuity-factor extraction.
* derived.f_user_max_affordable_home_price_dti
    Closed-form max H under Fannie Mae conventional DTI on gross.
* derived.f_user_max_affordable_home_price_post_tax
    Closed-form max H under DTI on take-home (gross - tax).
* derived.f_user_required_income_for_home
    Closed-form gross required to make a given home satisfy both DTIs.
* derived.f_user_town_verdict
    Per-(year, county, profile) verdict tuple.
* derived.f_user_nj_county_verdicts
    Set-returning convenience: emits f_user_town_verdict per NJ county.

Substrate-honesty: every "data not seeded" path is pinned to a NULL
assertion. Every "user input invalid" path (zero / negative gross) is
pinned to a NULL assertion. The platform never silently substitutes.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import psycopg

pytestmark = pytest.mark.live_pg


# ============================================================================
# Fixture: fully-migrated + fully-seeded DB plus a synthetic county-year
#
# County XX-99001 with 2024 substrate (FRED 7%, DCA $500K @ 2.85%,
# ACS5 $120K). Same shape as Phase 2/3 fixtures so the hand-computed
# anchors compose cleanly across phases.
# ============================================================================


@pytest.fixture
def phase4_db(live_pg: psycopg.Connection) -> psycopg.Connection:
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
        cur.execute(
            "INSERT INTO ref.state (state_code, state_fips, name) "
            "VALUES ('XX','99','Test State') ON CONFLICT DO NOTHING"
        )
        cur.execute(
            "INSERT INTO ref.county (county_id, state_code, county_fips, name) "
            "VALUES ('XX-TEST','XX','99001','Test County') "
            "ON CONFLICT DO NOTHING"
        )
        cur.execute(
            "INSERT INTO raw.fred_observation "
            "  (series_id, observation_date, value, source_url, source_sha256) "
            "VALUES ('MORTGAGE30US', '2024-06-06', 7.0000, "
            "        'https://fred.stlouisfed.org/', %s) "
            "ON CONFLICT DO NOTHING",
            ("a" * 64,),
        )
        cur.execute(
            "INSERT INTO raw.nj_property_tax_county "
            "  (county_fips, year, avg_residential_value, cy_total_rate, "
            "   source_url, source_sha256, source_vintage) "
            "VALUES ('99001', 2024, 500000, 2.8500, "
            "        'https://www.nj.gov/dca/divisions/dlgs/', %s, '2024-annual') "
            "ON CONFLICT DO NOTHING",
            ("b" * 64,),
        )
        cur.execute(
            "INSERT INTO raw.acs_median_household_income "
            "  (county_fips, year, product, estimate, dollar_year, "
            "   source_url, source_sha256) "
            "VALUES ('99001', 2024, 'acs5', 120000, 2024, "
            "        'https://api.census.gov/data/2024/acs/acs5', %s) "
            "ON CONFLICT DO NOTHING",
            ("c" * 64,),
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
# Hand-computed anchors (locked in once, referenced across many tests):
#
# 2024, FRED 7%, $500K home, county tax 2.85%, ins 0.35%, 20% down, 30y term.
#   monthly_PI on $400K @ 7%/30y = $2,661.21 (Phase-2 hand-anchor)
#   annual P&I = $31,934.52
#   prop tax  = $14,250
#   insurance = $1,750
#   PITI annual = $47,934.52
#
#   Therefore PITI coefficient c = 47934.52 / 500000 = 0.0958690
#   (the closed-form refactor MUST agree with PITI-via-component-sum)
#
# For $150K MFJ-1-1 user, no other debt, default 28/36 DTI:
#   front_cap = 0.28 * 150000 / c = 42000 / 0.0958690 = $438,097.64
#   back_cap  = 0.36 * 150000 / c = 54000 / 0.0958690 = $563,268.40
#   max_dti = min = $438,097.64 (front-end binds)
#
# tax($150K MFJ-1-1, 2024) = $31,426.13 (Phase-1 engine output)
# take_home = $118,573.87
#   max_post_tax = 0.28 * 118573.87 / c = 33200.68 / 0.0958690 = $346,312.90
#
# Required income for $500K home, default DTI:
#   front: G >= PITI / 0.28 = 47934.52 / 0.28 = $171,194.71
#   back:  G >= PITI / 0.36 = 47934.52 / 0.36 = $133,151.44
#   required = max = $171,194.71
#
# Verdict for $150K user, $500K median:
#   $500K vs max_dti=$438,097 ratio 1.141 -> within 1.25 -> "stretch"
#   $500K vs max_post_tax=$346,313 ratio 1.444 -> over 1.25 -> "out_of_reach"
#   personal_burden_ratio = 47934.52 / 150000 = 0.3196
#   personal_burden_ratio_post_tax = 47934.52 / 118573.87 = 0.4043
#   gross_income_gap = 171194.71 - 150000 = $21,194.71
# ============================================================================


# ============================================================================
# 1. f_piti_coefficient -- the closed-form constant
# ============================================================================


class TestPITICoefficient:

    def test_c_matches_piti_via_component_sum(
        self, phase4_db: psycopg.Connection
    ) -> None:
        """The fundamental identity: PITI(H) = H * c. If the c-coefficient
        function disagrees with the component-sum f_piti_annual at ANY
        precision, the personalization engine is silently broken."""
        with phase4_db.cursor() as cur:
            cur.execute(
                "SELECT derived.f_piti_coefficient(2024::SMALLINT, %s)",
                ("99001",),
            )
            row = cur.fetchone()
            assert row is not None
            c = Decimal(str(row[0]))
            cur.execute(
                "SELECT derived.f_piti_annual(%s, 2024::SMALLINT, %s)",
                (Decimal("500000"), "99001"),
            )
            row = cur.fetchone()
            assert row is not None
            piti = Decimal(str(row[0]))
        # 500000 * c MUST equal PITI to the cent.
        product = Decimal("500000") * c
        diff = abs(product - piti)
        assert diff < Decimal("0.01"), \
            f"500000*c={product} vs f_piti_annual={piti}, diff={diff}"

    def test_c_is_about_0_096_for_the_test_substrate(
        self, phase4_db: psycopg.Connection
    ) -> None:
        """Sanity: c = 47934.52 / 500000 = 0.095869. Catches an off-by-
        order-of-magnitude error in the annuity-factor extraction."""
        c = _scalar(
            phase4_db,
            "SELECT round(derived.f_piti_coefficient(2024::SMALLINT, %s), 6)",
            "99001",
        )
        _approx_dec(c, "0.095869", abs_tol="0.000001")

    def test_c_responds_to_rate_override(
        self, phase4_db: psycopg.Connection
    ) -> None:
        """Lower mortgage rate => lower P&I component => lower c."""
        c_at_7 = _scalar(
            phase4_db,
            "SELECT derived.f_piti_coefficient("
            "  2024::SMALLINT, %s, NULL, NULL, NULL, %s)",
            "99001", Decimal("0.07"),
        )
        c_at_3 = _scalar(
            phase4_db,
            "SELECT derived.f_piti_coefficient("
            "  2024::SMALLINT, %s, NULL, NULL, NULL, %s)",
            "99001", Decimal("0.03"),
        )
        assert Decimal(str(c_at_3)) < Decimal(str(c_at_7))

    def test_c_responds_to_higher_down_payment(
        self, phase4_db: psycopg.Connection
    ) -> None:
        """Higher down => smaller loan => lower P&I component => lower c."""
        c_at_20 = _scalar(
            phase4_db,
            "SELECT derived.f_piti_coefficient(2024::SMALLINT, %s, %s)",
            "99001", Decimal("0.20"),
        )
        c_at_50 = _scalar(
            phase4_db,
            "SELECT derived.f_piti_coefficient(2024::SMALLINT, %s, %s)",
            "99001", Decimal("0.50"),
        )
        assert Decimal(str(c_at_50)) < Decimal(str(c_at_20))

    def test_c_null_for_unknown_county(
        self, phase4_db: psycopg.Connection
    ) -> None:
        out = _scalar(
            phase4_db,
            "SELECT derived.f_piti_coefficient(2024::SMALLINT, %s)",
            "34999",
        )
        assert out is None


# ============================================================================
# 2. f_user_max_affordable_home_price_dti -- closed-form gross-DTI
# ============================================================================


class TestMaxAffordableDTI:

    def test_150k_mfj_2024_test_county(
        self, phase4_db: psycopg.Connection
    ) -> None:
        """The headline hand-computed scenario: $150K user, default DTI,
        no other debt => $438,097.64. Front-end binds."""
        out = _scalar(
            phase4_db,
            "SELECT derived.f_user_max_affordable_home_price_dti("
            "  %s, 2024::SMALLINT, %s)",
            Decimal("150000"), "99001",
        )
        _approx_dec(out, "438097.64", abs_tol="1.00")

    def test_other_debt_at_boundary_makes_them_tie(
        self, phase4_db: psycopg.Connection
    ) -> None:
        """At $1000/mo other debt, back_cap = (0.36*150K - 12K)/c =
        $42K/c = front_cap. The two binds tie almost exactly. This
        test pins the algebra and catches a signed-arithmetic bug
        on the back-cap."""
        out = _scalar(
            phase4_db,
            "SELECT derived.f_user_max_affordable_home_price_dti("
            "  %s, 2024::SMALLINT, %s, %s)",
            Decimal("150000"), "99001", Decimal("1000"),
        )
        _approx_dec(out, "438097.64", abs_tol="1.00")

    def test_excessive_other_debt_back_cap_clamped_to_zero(
        self, phase4_db: psycopg.Connection
    ) -> None:
        """If user has $5000/mo other debt at $150K gross:
        back_cap raw = (0.36*150K - 60K)/c = -$6000/c < 0 => clamped to 0
        => max = min(front, 0) = 0. The user can't afford ANY home."""
        out = _scalar(
            phase4_db,
            "SELECT derived.f_user_max_affordable_home_price_dti("
            "  %s, 2024::SMALLINT, %s, %s)",
            Decimal("150000"), "99001", Decimal("5000"),
        )
        _approx_dec(out, "0.00")

    def test_higher_dti_caps_increase_max(
        self, phase4_db: psycopg.Connection
    ) -> None:
        """The CFPB QM rule allows back DTI up to 0.43 vs Fannie's 0.36.
        At $150K gross with no other debt: back_cap_43 = $54K/c is no
        longer the bind (front 0.28 still binds at $42K/c) so max
        unchanged. Check by raising the FRONT to make front the slack one."""
        max_default = _scalar(
            phase4_db,
            "SELECT derived.f_user_max_affordable_home_price_dti("
            "  %s, 2024::SMALLINT, %s, 0, 0.40, 0.50)",
            Decimal("150000"), "99001",
        )
        max_baseline = _scalar(
            phase4_db,
            "SELECT derived.f_user_max_affordable_home_price_dti("
            "  %s, 2024::SMALLINT, %s)",
            Decimal("150000"), "99001",
        )
        # Front raised to 0.40, back to 0.50 -- both more permissive
        # than defaults => max should grow.
        assert Decimal(str(max_default)) > Decimal(str(max_baseline))

    def test_rate_override_2_pct_increases_max(
        self, phase4_db: psycopg.Connection
    ) -> None:
        """Counterfactual slider: drop rate to 2% => lower c => higher max."""
        max_at_7 = _scalar(
            phase4_db,
            "SELECT derived.f_user_max_affordable_home_price_dti("
            "  %s, 2024::SMALLINT, %s)",
            Decimal("150000"), "99001",
        )
        max_at_2 = _scalar(
            phase4_db,
            "SELECT derived.f_user_max_affordable_home_price_dti("
            "  %s, 2024::SMALLINT, %s, 0, NULL, NULL, NULL, NULL, NULL, %s)",
            Decimal("150000"), "99001", Decimal("0.02"),
        )
        assert Decimal(str(max_at_2)) > Decimal(str(max_at_7))

    def test_null_when_gross_null(self, phase4_db: psycopg.Connection) -> None:
        out = _scalar(
            phase4_db,
            "SELECT derived.f_user_max_affordable_home_price_dti("
            "  NULL, 2024::SMALLINT, %s)",
            "99001",
        )
        assert out is None

    def test_null_when_gross_zero(self, phase4_db: psycopg.Connection) -> None:
        """Zero gross => DTI ratio undefined => NULL (not silent zero)."""
        out = _scalar(
            phase4_db,
            "SELECT derived.f_user_max_affordable_home_price_dti("
            "  %s, 2024::SMALLINT, %s)",
            Decimal("0"), "99001",
        )
        assert out is None

    def test_null_when_gross_negative(
        self, phase4_db: psycopg.Connection
    ) -> None:
        out = _scalar(
            phase4_db,
            "SELECT derived.f_user_max_affordable_home_price_dti("
            "  %s, 2024::SMALLINT, %s)",
            Decimal("-1000"), "99001",
        )
        assert out is None

    def test_null_when_county_unknown(
        self, phase4_db: psycopg.Connection
    ) -> None:
        out = _scalar(
            phase4_db,
            "SELECT derived.f_user_max_affordable_home_price_dti("
            "  %s, 2024::SMALLINT, %s)",
            Decimal("150000"), "34999",
        )
        assert out is None


# ============================================================================
# 3. f_user_max_affordable_home_price_post_tax -- closed-form take-home DTI
# ============================================================================


class TestMaxAffordablePostTax:

    def test_150k_mfj_2024_test_county(
        self, phase4_db: psycopg.Connection
    ) -> None:
        """tax(150K MFJ-1-1 2024) = $31,426.13, take_home $118,573.87.
        max_post_tax = 0.28 * 118,573.87 / 0.095869 = $346,312.90."""
        out = _scalar(
            phase4_db,
            "SELECT derived.f_user_max_affordable_home_price_post_tax("
            "  %s, 2024::SMALLINT, %s, %s, %s, %s)",
            Decimal("150000"), "99001", "mfj", 1, 1,
        )
        _approx_dec(out, "346312.90", abs_tol="1.00")

    def test_post_tax_strictly_less_than_dti(
        self, phase4_db: psycopg.Connection
    ) -> None:
        """For any positive tax burden, max_post_tax < max_dti because
        take_home < gross. Catches a sign-flip bug on the take-home calc."""
        max_dti = _scalar(
            phase4_db,
            "SELECT derived.f_user_max_affordable_home_price_dti("
            "  %s, 2024::SMALLINT, %s)",
            Decimal("150000"), "99001",
        )
        max_post_tax = _scalar(
            phase4_db,
            "SELECT derived.f_user_max_affordable_home_price_post_tax("
            "  %s, 2024::SMALLINT, %s, %s, %s, %s)",
            Decimal("150000"), "99001", "mfj", 1, 1,
        )
        assert Decimal(str(max_post_tax)) < Decimal(str(max_dti))

    def test_null_when_tax_year_unseeded(
        self, phase4_db: psycopg.Connection
    ) -> None:
        """No tax tables for 2010 in this fixture -> total_tax NULL ->
        max_post_tax NULL. NEVER silently substitute another year's tax."""
        out = _scalar(
            phase4_db,
            "SELECT derived.f_user_max_affordable_home_price_post_tax("
            "  %s, 2010::SMALLINT, %s, %s, %s, %s)",
            Decimal("150000"), "99001", "mfj", 1, 1,
        )
        assert out is None

    def test_null_when_filing_status_unknown(
        self, phase4_db: psycopg.Connection
    ) -> None:
        """An unknown filing-status string falls through the tax engine to
        NULL total_tax => NULL max_post_tax."""
        out = _scalar(
            phase4_db,
            "SELECT derived.f_user_max_affordable_home_price_post_tax("
            "  %s, 2024::SMALLINT, %s, %s, %s, %s)",
            Decimal("150000"), "99001", "bogus", 1, 1,
        )
        assert out is None


# ============================================================================
# 4. f_user_required_income_for_home -- the inverse of max-affordable
# ============================================================================


class TestRequiredIncomeForHome:

    def test_500k_home_2024_test_county(
        self, phase4_db: psycopg.Connection
    ) -> None:
        """PITI($500K) = $47,934.52. Front: G >= 47934.52/0.28 = $171,194.71.
        Back: G >= 47934.52/0.36 = $133,151.44. Required = max = $171,194.71."""
        out = _scalar(
            phase4_db,
            "SELECT derived.f_user_required_income_for_home("
            "  %s, 2024::SMALLINT, %s)",
            Decimal("500000"), "99001",
        )
        _approx_dec(out, "171194.71")

    def test_inverse_of_max_affordable(
        self, phase4_db: psycopg.Connection
    ) -> None:
        """The required-income for the home equal to a user's max-affordable
        should equal that user's gross income (within the rounding the
        max-affordable function applies)."""
        with phase4_db.cursor() as cur:
            cur.execute(
                "SELECT derived.f_user_max_affordable_home_price_dti("
                "  %s, 2024::SMALLINT, %s)",
                (Decimal("150000"), "99001"),
            )
            row = cur.fetchone()
            assert row is not None
            max_h = Decimal(str(row[0]))
            cur.execute(
                "SELECT derived.f_user_required_income_for_home("
                "  %s, 2024::SMALLINT, %s)",
                (max_h, "99001"),
            )
            row = cur.fetchone()
            assert row is not None
            req = Decimal(str(row[0]))
        # max_affordable rounds H to 2 decimals; required-income then
        # back-computes G from rounded PITI. Allow $1 tolerance.
        assert abs(req - Decimal("150000")) < Decimal("1.00"), \
            f"inverse-test: required for max_h={max_h} is {req}, want 150000"

    def test_other_debt_increases_required_via_back_dti(
        self, phase4_db: psycopg.Connection
    ) -> None:
        """At $500K home, with $2000/mo other debt:
        back: G >= (PITI + 24K) / 0.36 = ($47934 + $24K)/0.36 = $199,818
        front: unchanged at $171,195. required = max = $199,818
        (back DTI now binds, where it didn't with no other debt)."""
        out = _scalar(
            phase4_db,
            "SELECT derived.f_user_required_income_for_home("
            "  %s, 2024::SMALLINT, %s, %s)",
            Decimal("500000"), "99001", Decimal("2000"),
        )
        _approx_dec(out, "199818.11", abs_tol="1.00")

    def test_null_when_county_unknown(
        self, phase4_db: psycopg.Connection
    ) -> None:
        out = _scalar(
            phase4_db,
            "SELECT derived.f_user_required_income_for_home("
            "  %s, 2024::SMALLINT, %s)",
            Decimal("500000"), "34999",
        )
        assert out is None


# ============================================================================
# 5. f_user_town_verdict -- the headline tuple
# ============================================================================


class TestUserTownVerdict:

    def test_full_row_for_150k_user_2024_test_county(
        self, phase4_db: psycopg.Connection
    ) -> None:
        """End-to-end: $150K MFJ-1-1 user, $500K median home in test county.
        Pin every field to its hand-computed expectation."""
        with phase4_db.cursor() as cur:
            cur.execute(
                "SELECT * FROM derived.f_user_town_verdict("
                "  2024::SMALLINT, %s, %s, %s, %s, %s)",
                ("99001", Decimal("150000"), "mfj", 1, 1),
            )
            row = cur.fetchone()
        assert row is not None
        (
            county_fips, median_home, max_dti, max_post_tax, piti_med,
            req_gross, take_home, burden_ratio, burden_ratio_post_tax,
            verdict_dti, verdict_post_tax, gross_gap, formula_version,
        ) = row

        assert county_fips == "99001"
        _approx_dec(median_home, "500000")
        _approx_dec(max_dti, "438097.64", abs_tol="1.00")
        _approx_dec(max_post_tax, "346312.90", abs_tol="1.00")
        _approx_dec(piti_med, "47934.52", abs_tol="0.10")
        _approx_dec(req_gross, "171194.71")
        _approx_dec(take_home, "118573.87", abs_tol="0.10")
        _approx_dec(burden_ratio, "0.3196", abs_tol="0.0005")
        _approx_dec(burden_ratio_post_tax, "0.4043", abs_tol="0.0005")
        assert verdict_dti == "stretch"      # 500K vs 438K = 1.14x
        assert verdict_post_tax == "out_of_reach"  # 500K vs 346K = 1.44x
        _approx_dec(gross_gap, "21194.71")
        assert formula_version == "1.4.0-personalization-engine-v1"

    def test_verdict_affordable_when_user_can_easily_afford(
        self, phase4_db: psycopg.Connection
    ) -> None:
        """High income ($300K MFJ) easily covers the $500K median.
        max_dti = 0.28 * 300K / c = $876,195 >> $500K => 'affordable'."""
        verdict = _scalar(
            phase4_db,
            "SELECT verdict_dti FROM derived.f_user_town_verdict("
            "  2024::SMALLINT, %s, %s, %s, %s, %s)",
            "99001", Decimal("300000"), "mfj", 1, 1,
        )
        assert verdict == "affordable"

    def test_verdict_out_of_reach_when_user_far_short(
        self, phase4_db: psycopg.Connection
    ) -> None:
        """Low income ($60K) gives max_dti = 0.28 * 60K / c = $175,239.
        $500K is 2.85x => over 1.25 => 'out_of_reach'."""
        verdict = _scalar(
            phase4_db,
            "SELECT verdict_dti FROM derived.f_user_town_verdict("
            "  2024::SMALLINT, %s, %s, %s, %s, %s)",
            "99001", Decimal("60000"), "single", 0, 0,
        )
        assert verdict == "out_of_reach"

    def test_verdict_stretch_at_boundary(
        self, phase4_db: psycopg.Connection
    ) -> None:
        """Engineered: pick gross so median_home = 1.10 * max_dti exactly.
        median = 500K, want max_dti = 500K/1.10 = $454,545.
        max_dti = 0.28 * G / c = 454,545 => G = 454545 * 0.095869 / 0.28
        ~= $155,640. At this G, verdict should be 'stretch'."""
        verdict = _scalar(
            phase4_db,
            "SELECT verdict_dti FROM derived.f_user_town_verdict("
            "  2024::SMALLINT, %s, %s, %s, %s, %s)",
            "99001", Decimal("155640"), "mfj", 1, 1,
        )
        assert verdict == "stretch"

    def test_personal_burden_ratio_matches_piti_div_gross(
        self, phase4_db: psycopg.Connection
    ) -> None:
        """personal_burden_ratio is PITI / gross. Pin that arithmetic."""
        with phase4_db.cursor() as cur:
            cur.execute(
                "SELECT piti_on_median, personal_burden_ratio "
                "FROM derived.f_user_town_verdict("
                "  2024::SMALLINT, %s, %s, %s, %s, %s)",
                ("99001", Decimal("150000"), "mfj", 1, 1),
            )
            row = cur.fetchone()
        assert row is not None
        piti, burden = row
        expected = (Decimal(str(piti)) / Decimal("150000")).quantize(Decimal("0.0001"))
        _approx_dec(burden, str(expected), abs_tol="0.0001")

    def test_gross_gap_negative_when_user_above_required(
        self, phase4_db: psycopg.Connection
    ) -> None:
        """$300K user vs $171K required => gap = $171K - $300K = -$129K.
        Negative gap = user has MORE income than required (headroom)."""
        gap = _scalar(
            phase4_db,
            "SELECT gross_income_gap FROM derived.f_user_town_verdict("
            "  2024::SMALLINT, %s, %s, %s, %s, %s)",
            "99001", Decimal("300000"), "mfj", 1, 1,
        )
        _approx_dec(gap, "-128805.29", abs_tol="1.00")

    def test_returns_one_row_always(
        self, phase4_db: psycopg.Connection
    ) -> None:
        """Even for an unknown county, the verdict function returns a
        single row (with NULL fields). The page can render the row;
        the user sees "data not loaded" honestly instead of a blank."""
        with phase4_db.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM derived.f_user_town_verdict("
                "  2024::SMALLINT, %s, %s, %s, %s, %s)",
                ("34999", Decimal("150000"), "mfj", 1, 1),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == 1

    def test_unknown_county_yields_null_verdicts_not_crash(
        self, phase4_db: psycopg.Connection
    ) -> None:
        with phase4_db.cursor() as cur:
            cur.execute(
                "SELECT median_home_price, verdict_dti, verdict_post_tax "
                "FROM derived.f_user_town_verdict("
                "  2024::SMALLINT, %s, %s, %s, %s, %s)",
                ("34999", Decimal("150000"), "mfj", 1, 1),
            )
            row = cur.fetchone()
        assert row is not None
        median, vdti, vpt = row
        assert median is None
        assert vdti is None
        assert vpt is None


# ============================================================================
# 6. f_user_nj_county_verdicts -- per-NJ-county convenience
# ============================================================================


class TestUserNJCountyVerdicts:

    def test_returns_21_rows_one_per_nj_county(
        self, phase4_db: psycopg.Connection
    ) -> None:
        """The seeded ref.county includes 21 NJ counties + 1 test 'XX'
        county. The NJ-only function should return exactly 21 rows."""
        out = _scalar(
            phase4_db,
            "SELECT count(*) FROM derived.f_user_nj_county_verdicts("
            "  2024::SMALLINT, %s, %s, %s, %s)",
            Decimal("150000"), "mfj", 1, 1,
        )
        assert int(str(out)) == 21

    def test_excludes_test_county_xx(
        self, phase4_db: psycopg.Connection
    ) -> None:
        """The XX-99001 fixture county is in ref.county with state_code='XX',
        so the NJ-only filter must exclude it."""
        with phase4_db.cursor() as cur:
            cur.execute(
                "SELECT county_id FROM derived.f_user_nj_county_verdicts("
                "  2024::SMALLINT, %s, %s, %s, %s)",
                (Decimal("150000"), "mfj", 1, 1),
            )
            ids = [r[0] for r in cur.fetchall()]
        assert "XX-TEST" not in ids

    def test_rows_carry_county_name(
        self, phase4_db: psycopg.Connection
    ) -> None:
        """Each row should carry the county name for the per-county table."""
        with phase4_db.cursor() as cur:
            cur.execute(
                "SELECT county_name FROM derived.f_user_nj_county_verdicts("
                "  2024::SMALLINT, %s, %s, %s, %s) "
                "WHERE county_name = 'Bergen'",
                (Decimal("150000"), "mfj", 1, 1),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == "Bergen"

    def test_rows_sorted_by_county_name(
        self, phase4_db: psycopg.Connection
    ) -> None:
        with phase4_db.cursor() as cur:
            cur.execute(
                "SELECT county_name FROM derived.f_user_nj_county_verdicts("
                "  2024::SMALLINT, %s, %s, %s, %s)",
                (Decimal("150000"), "mfj", 1, 1),
            )
            names = [r[0] for r in cur.fetchall()]
        assert names == sorted(names)
