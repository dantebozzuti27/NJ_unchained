"""Live-PG tests for the Phase-1 tax simulator (migration 070).

Every assertion in this file is a HAND-COMPUTED tax liability that
matches a published IRS or NJ Division of Taxation example. We are
not testing "the function returns a number"; we are testing "the
function returns *the* number that an auditor would compute by
hand from the same Rev. Proc. or NJ-1040 schedule".

The four function families under test:

* derived.f_apply_federal_brackets / derived.f_apply_nj_state_brackets
  -- piecewise-linear bracket walkers
* derived.f_federal_taxable_income / derived.f_federal_income_tax
  -- gross -> taxable -> tax (with CTC)
* derived.f_nj_state_income_tax
  -- the deduction-vs-credit selector NJSA 54A:3A-17/20
* derived.f_fica_tax / derived.f_household_taxes
  -- payroll tax + composite

Substrate-honesty contract: when the requested (year, filing_status)
is not seeded, every function returns NULL. The platform NEVER
silently substitutes an adjacent year. Several tests below pin
exactly this behavior.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import psycopg

pytestmark = pytest.mark.live_pg


# ============================================================================
# Fixture: fully-migrated + fully-seeded tax DB
# ============================================================================


@pytest.fixture
def tax_db(live_pg: psycopg.Connection) -> psycopg.Connection:
    """Drop platform schemas, re-apply all migrations + seeds.

    Mirrors tests/test_fraud_leie_age_decay.py's fraud_db idiom.
    """
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
    return conn


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _scalar(conn: psycopg.Connection, sql: str, *params: object) -> object:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _approx_dec(actual: object, expected: str | Decimal, *, abs_tol: str = "0.01") -> None:
    """Assert two decimals are equal within abs_tol dollars (default $0.01)."""
    a = Decimal(str(actual))
    e = Decimal(str(expected))
    assert abs(a - e) <= Decimal(abs_tol), f"expected {e} +/- {abs_tol}, got {a}"


# ============================================================================
# 1. Bracket walks (the atomic primitives)
# ============================================================================


class TestFederalBracketWalk:
    """Each test corresponds to a hand-computed walk over Rev. Proc. 2023-34.

    Method: for income X falling in bracket K, tentative tax is the
    sum over brackets 1..K of (top_of_bracket - bottom_of_bracket) *
    rate, plus (X - bottom_of_K) * rate_K. We hand-walk and pin the
    expected dollar amount.
    """

    def test_2024_single_in_first_bracket(self, tax_db: psycopg.Connection) -> None:
        # $5,000 single 2024: entirely 10% bracket -> $500.
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2024::SMALLINT, 'single')",
            Decimal("5000"),
        )
        _approx_dec(out, "500.00")

    def test_2024_single_at_first_bracket_boundary(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Exactly $11,600: 10% * 11,600 = $1,160.
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2024::SMALLINT, 'single')",
            Decimal("11600"),
        )
        _approx_dec(out, "1160.00")

    def test_2024_single_45400_pub17_example(self, tax_db: psycopg.Connection) -> None:
        # Pub 17 example: taxable $45,400 single 2024.
        # 10% * 11,600 + 12% * (45,400 - 11,600) = 1,160 + 4,056 = $5,216.
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2024::SMALLINT, 'single')",
            Decimal("45400"),
        )
        _approx_dec(out, "5216.00")

    def test_2024_single_high_income_into_35_bracket(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Taxable $235,400 (= $250K - $14,600 std). Walk:
        #   11,600*0.10 + 35,550*0.12 + 53,375*0.22 + 91,425*0.24 + 43,450*0.32
        #   = 1,160 + 4,266 + 11,742.50 + 21,942.00 + 13,904.00 = $53,014.50.
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2024::SMALLINT, 'single')",
            Decimal("235400"),
        )
        _approx_dec(out, "53014.50")

    def test_2024_single_top_bracket(self, tax_db: psycopg.Connection) -> None:
        # Taxable $1,000,000 single 2024 (well past $609,350 top floor).
        # Per-bracket walk:
        #   11,600   * 0.10 = 1,160.00
        #   35,550   * 0.12 = 4,266.00
        #   53,375   * 0.22 = 11,742.50
        #   91,425   * 0.24 = 21,942.00
        #   51,775   * 0.32 = 16,568.00
        #   365,625  * 0.35 = 127,968.75   (243,725 -> 609,350)
        #   390,650  * 0.37 = 144,540.50   (609,350 -> 1,000,000)
        #   Total          = $328,187.75.
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2024::SMALLINT, 'single')",
            Decimal("1000000"),
        )
        _approx_dec(out, "328187.75")

    def test_2024_mfj_50000(self, tax_db: psycopg.Connection) -> None:
        # MFJ 2024 taxable $50,000:
        # 23,200 * 0.10 + (50,000 - 23,200) * 0.12 = 2,320 + 3,216 = $5,536.
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2024::SMALLINT, 'mfj')",
            Decimal("50000"),
        )
        _approx_dec(out, "5536.00")

    def test_2024_mfs_uses_separate_top(self, tax_db: psycopg.Connection) -> None:
        # MFS 2024 has 37% starting at $365,600 (not $609,350 like Single).
        # Taxable $400,000 MFS:
        #   At $365,600: 1,160 + 4,266 + 11,742.50 + 21,942.00 + 16,568.00
        #                + 42,656.25 = $98,334.75
        #   Plus (400,000 - 365,600) * 0.37 = 34,400 * 0.37 = $12,728.00
        #   Total = $111,062.75
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2024::SMALLINT, 'mfs')",
            Decimal("400000"),
        )
        _approx_dec(out, "111062.75")


class TestNjStateBracketWalk:
    """Hand-walks over NJ-1040 Tax Rate Schedules I (Single/MFS) and II (MFJ/HOH/QSS)."""

    def test_2024_single_in_first_bracket(self, tax_db: psycopg.Connection) -> None:
        # $10,000 single (Schedule I) -> 10,000 * 0.014 = $140.
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_nj_state_brackets(%s, 2024::SMALLINT, 'single')",
            Decimal("10000"),
        )
        _approx_dec(out, "140.00")

    def test_2024_single_75000(self, tax_db: psycopg.Connection) -> None:
        # $75,000 single. Walk:
        #   20,000 * 0.014   = 280.00
        #   15,000 * 0.0175  = 262.50
        #    5,000 * 0.035   = 175.00
        #   35,000 * 0.05525 = 1,933.75
        #   Total            = 2,651.25
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_nj_state_brackets(%s, 2024::SMALLINT, 'single')",
            Decimal("75000"),
        )
        _approx_dec(out, "2651.25")

    def test_2024_mfj_uses_245_bracket(self, tax_db: psycopg.Connection) -> None:
        # MFJ Schedule II has the extra 2.45% bracket from $50K-$70K.
        # Taxable $60,000 MFJ:
        #   20,000 * 0.014  = 280.00
        #   30,000 * 0.0175 = 525.00
        #   10,000 * 0.0245 = 245.00
        #   Total           = 1,050.00
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_nj_state_brackets(%s, 2024::SMALLINT, 'mfj')",
            Decimal("60000"),
        )
        _approx_dec(out, "1050.00")

    def test_2024_millionaires_tax_kicks_in(self, tax_db: psycopg.Connection) -> None:
        # MFJ taxable $1,500,000: walks all brackets up through 10.75%.
        #   (full lower brackets cumulative through $1M)
        #   20,000 * 0.014 + 30,000 * 0.0175 + 20,000 * 0.0245 + 10,000 * 0.035
        #     + 70,000 * 0.05525 + 350,000 * 0.0637 + 500,000 * 0.0897
        #   = 280 + 525 + 490 + 350 + 3,867.50 + 22,295 + 44,850 = 72,657.50
        #   Plus (1,500,000 - 1,000,000) * 0.1075 = 500,000 * 0.1075 = 53,750.
        #   Total = $126,407.50.
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_nj_state_brackets(%s, 2024::SMALLINT, 'mfj')",
            Decimal("1500000"),
        )
        _approx_dec(out, "126407.50")


# ============================================================================
# 2. Standard deduction + taxable-income transform
# ============================================================================


class TestFederalTaxableIncome:
    def test_2024_single_60k(self, tax_db: psycopg.Connection) -> None:
        # 60,000 - 14,600 (std) - 0 (TCJA exemption) = 45,400.
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_taxable_income(%s, 2024::SMALLINT, 'single', 0)",
            Decimal("60000"),
        )
        _approx_dec(out, "45400.00")

    def test_2024_mfj_120k_2_dependents(self, tax_db: psycopg.Connection) -> None:
        # 120,000 - 29,200 (std) - 0 (TCJA: per-person exemption is $0) = 90,800.
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_taxable_income(%s, 2024::SMALLINT, 'mfj', %s)",
            Decimal("120000"),
            2,
        )
        _approx_dec(out, "90800.00")

    def test_2024_hoh_uses_hoh_std_deduction(self, tax_db: psycopg.Connection) -> None:
        # HOH 2024 std = $21,900. Gross 50,000 -> taxable 28,100.
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_taxable_income(%s, 2024::SMALLINT, 'hoh', 0)",
            Decimal("50000"),
        )
        _approx_dec(out, "28100.00")

    def test_taxable_clamps_at_zero(self, tax_db: psycopg.Connection) -> None:
        # Gross $5,000 single 2024: 5,000 - 14,600 = -9,600 -> clamp to 0.
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_taxable_income(%s, 2024::SMALLINT, 'single', 0)",
            Decimal("5000"),
        )
        _approx_dec(out, "0.00")


# ============================================================================
# 3. Child Tax Credit (with and without phaseout)
# ============================================================================


class TestChildTaxCredit:
    def test_2024_two_kids_no_phaseout(self, tax_db: psycopg.Connection) -> None:
        # 2 qualifying kids, MFJ MAGI $120K (well below $400K threshold).
        # 2 * $2,000 = $4,000.
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_child_tax_credit(%s, 2024::SMALLINT, 'mfj', %s)",
            Decimal("120000"),
            2,
        )
        _approx_dec(out, "4000.00")

    def test_2024_zero_kids(self, tax_db: psycopg.Connection) -> None:
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_child_tax_credit(%s, 2024::SMALLINT, 'single', 0)",
            Decimal("60000"),
        )
        _approx_dec(out, "0.00")

    def test_2024_single_partial_phaseout(self, tax_db: psycopg.Connection) -> None:
        # 1 qualifying child, single MAGI $230,000.
        # Excess = 230,000 - 200,000 = 30,000.
        # Reduction = 30,000 * 0.05 = $1,500.
        # Credit = max(0, 2,000 - 1,500) = $500.
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_child_tax_credit(%s, 2024::SMALLINT, 'single', 1)",
            Decimal("230000"),
        )
        _approx_dec(out, "500.00")

    def test_2024_mfj_full_phaseout(self, tax_db: psycopg.Connection) -> None:
        # 1 child, MFJ MAGI $500K. Excess = 100K. Reduction = 5,000.
        # Credit floor = max(0, 2,000 - 5,000) = $0.
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_child_tax_credit(%s, 2024::SMALLINT, 'mfj', 1)",
            Decimal("500000"),
        )
        _approx_dec(out, "0.00")


# ============================================================================
# 4. Composite federal income tax
# ============================================================================


class TestFederalIncomeTax:
    def test_2024_single_60k_no_kids(self, tax_db: psycopg.Connection) -> None:
        # Pub 17 walk: $5,216 (matches TestFederalBracketWalk too).
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_income_tax(%s, 2024::SMALLINT, 'single', 0, 0)",
            Decimal("60000"),
        )
        _approx_dec(out, "5216.00")

    def test_2024_mfj_120k_2_kids(self, tax_db: psycopg.Connection) -> None:
        # Tentative tax: 23,200*0.10 + (90,800-23,200)*0.12
        #              = 2,320 + 8,112 = 10,432
        # CTC: $4,000.
        # Net: $6,432.
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_income_tax(%s, 2024::SMALLINT, 'mfj', 2, 2)",
            Decimal("120000"),
        )
        _approx_dec(out, "6432.00")

    def test_2024_mfj_500k_1_kid_ctc_phased_out(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Hand: tentative 106,029; CTC fully phased out. Net 106,029.
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_income_tax(%s, 2024::SMALLINT, 'mfj', 1, 1)",
            Decimal("500000"),
        )
        _approx_dec(out, "106029.00")

    def test_2024_low_income_tax_clamps_at_zero(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # MFJ $25K with 1 kid: taxable = max(0, 25K - 29.2K) = 0.
        # Tentative = 0. CTC = $2K. max(0, 0 - 2K) = $0 (CTC non-refundable).
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_income_tax(%s, 2024::SMALLINT, 'mfj', 1, 1)",
            Decimal("25000"),
        )
        _approx_dec(out, "0.00")

    def test_2023_year_uses_2023_brackets(self, tax_db: psycopg.Connection) -> None:
        # 2023 single $60K. 2023 std = $13,850 -> taxable $46,150.
        # Walk: 11,000*0.10 + (44,725-11,000)*0.12 + (46,150-44,725)*0.22
        #     = 1,100 + 4,047 + 313.50 = $5,460.50
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_income_tax(%s, 2023::SMALLINT, 'single', 0, 0)",
            Decimal("60000"),
        )
        _approx_dec(out, "5460.50")


# ============================================================================
# 5. NJ state income tax (deduction-vs-credit selector)
# ============================================================================


class TestNjStateIncomeTax:
    def test_2024_single_60k_zero_property_tax_credit_wins(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # NJ exempt = $1,000. Taxable = $59,000.
        # Walk: 280 + 262.50 + 175 + 1,049.75 = 1,767.25 (Method A).
        # Method B: same walk - $50 credit = 1,717.25. CREDIT WINS.
        out = _scalar(
            tax_db,
            "SELECT derived.f_nj_state_income_tax(%s, 2024::SMALLINT, 'single', 0, 0)",
            Decimal("60000"),
        )
        _approx_dec(out, "1717.25")

    def test_2024_mfj_100k_with_property_tax_deduction_wins(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # MFJ 100K with 2 deps and $9K prop tax.
        # exempt = 1,000+1,000+2*1,500 = 5,000.
        # Method A: 100K-5K-9K = 86K. Schedule II walk:
        #   280 + 525 + 490 + 350 + 6,000*0.05525 = 280 + 525 + 490 + 350 + 331.50
        #   = $1,976.50.
        # Method B: 100K-5K = 95K. Walk:
        #   280 + 525 + 490 + 350 + 15,000*0.05525 = 280+525+490+350+828.75 = 2,473.75
        #   minus $50 credit = $2,423.75.
        # min(A, B) = $1,976.50.
        out = _scalar(
            tax_db,
            "SELECT derived.f_nj_state_income_tax(%s, 2024::SMALLINT, 'mfj', 2, 9000)",
            Decimal("100000"),
        )
        _approx_dec(out, "1976.50")

    def test_2024_property_tax_above_cap_clamped_to_15000(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Same MFJ $100K, 2 deps, but $20K prop tax (above $15K cap).
        # Method A uses min(20K, 15K) = 15K. Walk over 100K-5K-15K = 80K.
        #   280 + 525 + 490 + 350 + 0 = $1,645.00.
        out = _scalar(
            tax_db,
            "SELECT derived.f_nj_state_income_tax(%s, 2024::SMALLINT, 'mfj', 2, 20000)",
            Decimal("100000"),
        )
        _approx_dec(out, "1645.00")

    def test_2024_low_income_tax_clamps_at_zero(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # MFJ $5K, 0 deps, 0 prop tax. Exempt = $2K. Taxable = $3K.
        # Walk: 3,000 * 0.014 = $42. Method B: $42 - $50 = max(0, -8) = $0.
        # min(A=42, B=0) = $0.
        out = _scalar(
            tax_db,
            "SELECT derived.f_nj_state_income_tax(%s, 2024::SMALLINT, 'mfj', 0, 0)",
            Decimal("5000"),
        )
        _approx_dec(out, "0.00")


# ============================================================================
# 6. FICA payroll tax
# ============================================================================


class TestFica:
    def test_2024_single_60k(self, tax_db: psycopg.Connection) -> None:
        # SS: 6.2% * 60K = 3,720.  Medicare: 1.45% * 60K = 870.  Total $4,590.
        out = _scalar(
            tax_db,
            "SELECT derived.f_fica_tax(%s, 2024::SMALLINT, 'single')",
            Decimal("60000"),
        )
        _approx_dec(out, "4590.00")

    def test_2024_at_ss_wage_base_boundary(self, tax_db: psycopg.Connection) -> None:
        # Wage = $168,600 single 2024 (exactly the SS cap).
        # SS: 6.2% * 168,600 = 10,453.20.  Medicare: 1.45% * 168,600 = 2,444.70.
        # Total: $12,897.90.
        out = _scalar(
            tax_db,
            "SELECT derived.f_fica_tax(%s, 2024::SMALLINT, 'single')",
            Decimal("168600"),
        )
        _approx_dec(out, "12897.90")

    def test_2024_above_ss_wage_base_caps_ss(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Wage = $200,000 single 2024.
        # SS capped at 6.2% * 168,600 = 10,453.20.
        # Medicare: 1.45% * 200,000 = 2,900.00.
        # Additional Medicare: 0.9% * max(0, 200K - 200K) = $0.
        # Total: $13,353.20.
        out = _scalar(
            tax_db,
            "SELECT derived.f_fica_tax(%s, 2024::SMALLINT, 'single')",
            Decimal("200000"),
        )
        _approx_dec(out, "13353.20")

    def test_2024_high_income_triggers_additional_medicare(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Wage = $300,000 single. Excess over $200K = $100K.
        # SS: 10,453.20.  Medicare: 4,350.00.  AddMed: 0.9% * 100K = 900.
        # Total: $15,703.20.
        out = _scalar(
            tax_db,
            "SELECT derived.f_fica_tax(%s, 2024::SMALLINT, 'single')",
            Decimal("300000"),
        )
        _approx_dec(out, "15703.20")

    def test_2024_mfj_uses_higher_addmed_threshold(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Wage = $300K MFJ. Threshold $250K (not $200K).
        # SS 10,453.20 + Medicare 4,350.00 + AddMed 0.9%*50K=450.
        # Total: $15,253.20.
        out = _scalar(
            tax_db,
            "SELECT derived.f_fica_tax(%s, 2024::SMALLINT, 'mfj')",
            Decimal("300000"),
        )
        _approx_dec(out, "15253.20")

    def test_2023_uses_2023_wage_base(self, tax_db: psycopg.Connection) -> None:
        # 2023 SS wage base = $160,200 (not $168,600).
        # Wage $200K single: SS = 6.2% * 160,200 = 9,932.40.
        # Medicare = 2,900.00.  AddMed at 200K threshold = $0.
        # Total: $12,832.40.
        out = _scalar(
            tax_db,
            "SELECT derived.f_fica_tax(%s, 2023::SMALLINT, 'single')",
            Decimal("200000"),
        )
        _approx_dec(out, "12832.40")


# ============================================================================
# 7. Composite household-taxes function
# ============================================================================


class TestHouseholdTaxesComposite:
    def test_2024_single_60k_full_breakdown(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Combine the three known scenarios:
        #   federal: 5,216.00
        #   nj:      1,717.25
        #   fica:    4,590.00
        #   total:  11,523.25
        #   eff:     0.19205 (= 11,523.25 / 60,000)
        with tax_db.cursor() as cur:
            cur.execute(
                "SELECT federal_income_tax, nj_state_tax, fica_tax, "
                "       total_tax, effective_rate, formula_version "
                "FROM derived.f_household_taxes("
                "  p_gross_income        => %s::NUMERIC,"
                "  p_wage_income         => %s::NUMERIC,"
                "  p_tax_year            => 2024::SMALLINT,"
                "  p_filing_status       => 'single',"
                "  p_dependents          => 0,"
                "  p_qualifying_children => 0,"
                "  p_property_tax_paid   => 0::NUMERIC)",
                (Decimal("60000"), Decimal("60000")),
            )
            row = cur.fetchone()
        assert row is not None
        federal, nj, fica, total, eff_rate, fv = row
        _approx_dec(federal, "5216.00")
        _approx_dec(nj,      "1717.25")
        _approx_dec(fica,    "4590.00")
        _approx_dec(total,  "11523.25")
        _approx_dec(eff_rate, "0.19205", abs_tol="0.00001")
        assert fv == "1.1.0-tax-engine-v1"

    def test_2024_mfj_120k_2_kids_full_breakdown(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # federal: 6,432.00 (verified in TestFederalIncomeTax above).
        # nj at 120K MFJ, 2 deps, 0 prop tax:
        #   exempt = 1,000+1,000+2*1,500 = 5,000
        #   Method A walks brackets on (120K - 5K - 0K) = 115K:
        #     20K * 0.014   =   280.00
        #     30K * 0.0175  =   525.00
        #     20K * 0.0245  =   490.00
        #     10K * 0.035   =   350.00
        #     35K * 0.05525 = 1,933.75   (115K - 80K = 35K in the 5.525% band)
        #     -----------------------
        #     Total A       = 3,578.75
        #   Method B walks the same 115K (no prop-tax deduction available) and
        #   then subtracts the $50 alternative credit -> 3,528.75.
        #   min(A, B) = $3,528.75.
        # fica on $120K MFJ wage:
        #   SS  6.2% * 120K = 7,440.00
        #   Med 1.45% * 120K = 1,740.00
        #   AddMed: $0 (gross < $250K MFJ threshold)
        #   Total fica = $9,180.00.
        # total: 6,432.00 + 3,528.75 + 9,180.00 = $19,140.75.
        # eff:  19,140.75 / 120,000 = 0.15951.
        with tax_db.cursor() as cur:
            cur.execute(
                "SELECT federal_income_tax, nj_state_tax, fica_tax, "
                "       total_tax, effective_rate "
                "FROM derived.f_household_taxes("
                "  120000::NUMERIC, 120000::NUMERIC, 2024::SMALLINT,"
                "  'mfj', 2, 2, 0::NUMERIC)",
            )
            row = cur.fetchone()
        assert row is not None
        federal, nj, fica, total, eff_rate = row
        _approx_dec(federal, "6432.00")
        _approx_dec(nj,      "3528.75")
        _approx_dec(fica,    "9180.00")
        _approx_dec(total,  "19140.75")
        _approx_dec(eff_rate, "0.15951", abs_tol="0.00001")


# ============================================================================
# 7.5 Phase-5 historical backfill: TY 2022 hand-computed anchors
# ============================================================================
#
# Each test below pins a hand-walked TY 2022 tax computation derived
# directly from Rev. Proc. 2021-45 (federal) or the 2022 NJ-1040 Tax Rate
# Schedules (NJ). Pairing every Phase-5 seed with anchor tests is the
# substrate-honesty contract made operational: a typo in the seed file
# trips a specific dollar mismatch here, not a vague "tax engine looks
# off" complaint a year later. As additional Phase-5 years land
# (2010-2021), each gets its own anchor class in this section.
# ============================================================================


class TestPhase5Ty2022:
    """Hand-computed TY 2022 anchors. Source: Rev. Proc. 2021-45 + NJ-1040 2022."""

    # ----- Federal bracket walks (verifiable against Rev. Proc. 2021-45 s.3.01) -----

    def test_2022_single_bracket_boundary(self, tax_db: psycopg.Connection) -> None:
        # Single 2022, taxable $10,275 (exactly the 10%->12% boundary).
        # 10,275 * 0.10 = $1,027.50 (also the printed "$1,027.50" base in the
        # Rev. Proc. 2021-45 Table 3 12%-bracket formula row).
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2022::SMALLINT, 'single')",
            Decimal("10275"),
        )
        _approx_dec(out, "1027.50")

    def test_2022_single_47050_taxable(self, tax_db: psycopg.Connection) -> None:
        # Single 2022, taxable $47,050 (= $60K gross - $12,950 std).
        #   10,275 * 0.10  = 1,027.50
        #   31,500 * 0.12  = 3,780.00   (10,275 -> 41,775)
        #    5,275 * 0.22  = 1,160.50   (41,775 -> 47,050)
        #   Total          = 5,968.00
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2022::SMALLINT, 'single')",
            Decimal("47050"),
        )
        _approx_dec(out, "5968.00")

    def test_2022_mfj_94100_taxable(self, tax_db: psycopg.Connection) -> None:
        # MFJ 2022, taxable $94,100 (= $120K gross - $25,900 std).
        #   20,550 * 0.10  =  2,055.00
        #   63,000 * 0.12  =  7,560.00   (20,550 -> 83,550)
        #   10,550 * 0.22  =  2,321.00   (83,550 -> 94,100)
        #   Total          = 11,936.00
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2022::SMALLINT, 'mfj')",
            Decimal("94100"),
        )
        _approx_dec(out, "11936.00")

    def test_2022_mfs_uses_separate_top_bracket(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # MFS 2022 has 37% starting at $323,925 (NOT $539,900 like Single,
        # NOT $647,850 like MFJ). Floor verified against Rev. Proc. 2021-45
        # Table 4. Walk MFS at exactly $323,925 (the boundary):
        #   10,275 * 0.10   = 1,027.50
        #   31,500 * 0.12   = 3,780.00
        #   47,300 * 0.22   = 10,406.00   (41,775 -> 89,075)
        #   80,975 * 0.24   = 19,434.00   (89,075 -> 170,050)
        #   45,900 * 0.32   = 14,688.00   (170,050 -> 215,950)
        #  107,975 * 0.35   = 37,791.25   (215,950 -> 323,925)
        #   Total at floor  = 87,126.75   (matches the printed
        #                                  "$87,126.75 plus 37% of excess
        #                                  over $323,925" formula row).
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2022::SMALLINT, 'mfs')",
            Decimal("323925"),
        )
        _approx_dec(out, "87126.75")

    # ----- NJ bracket walks (rates + floors unchanged from TY 2023+2024;
    #       these tests confirm the seed loaded the correct schedule) -----

    def test_2022_nj_single_75000_matches_2024(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # NJ Schedule I rates have not changed since the Millionaires' Tax
        # was added (P.L. 2020 c.95). $75K Single 2022 must equal $75K
        # Single 2024 to the cent.
        out_2022 = _scalar(
            tax_db,
            "SELECT derived.f_apply_nj_state_brackets(%s, 2022::SMALLINT, 'single')",
            Decimal("75000"),
        )
        out_2024 = _scalar(
            tax_db,
            "SELECT derived.f_apply_nj_state_brackets(%s, 2024::SMALLINT, 'single')",
            Decimal("75000"),
        )
        # Both walk to $2,651.25 (see TestNjStateBracketWalk for the
        # full per-bracket derivation).
        _approx_dec(out_2022, "2651.25")
        _approx_dec(out_2022, str(out_2024))

    def test_2022_nj_mfj_uses_245_bracket(self, tax_db: psycopg.Connection) -> None:
        # NJ Schedule II MFJ 2022 walks the 2.45% band. $60K NJ taxable:
        #   20,000 * 0.014  = 280.00
        #   30,000 * 0.0175 = 525.00
        #   10,000 * 0.0245 = 245.00
        #   Total           = 1,050.00
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_nj_state_brackets(%s, 2022::SMALLINT, 'mfj')",
            Decimal("60000"),
        )
        _approx_dec(out, "1050.00")

    # ----- Standard deduction (Rev. Proc. 2021-45 s.3.15) -----

    def test_2022_taxable_income_single_60k(self, tax_db: psycopg.Connection) -> None:
        # 60,000 - 12,950 (TY 2022 single std) - 0 (TCJA exemption) = 47,050.
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_taxable_income(%s, 2022::SMALLINT, 'single', 0)",
            Decimal("60000"),
        )
        _approx_dec(out, "47050.00")

    def test_2022_taxable_income_mfj_120k_2_dependents(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # 120,000 - 25,900 (TY 2022 MFJ std) - 0 = 94,100.
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_taxable_income(%s, 2022::SMALLINT, 'mfj', %s)",
            Decimal("120000"),
            2,
        )
        _approx_dec(out, "94100.00")

    # ----- CTC (TY 2022 reverts to TCJA baseline post-ARPA) -----

    def test_2022_ctc_post_arpa_two_kids_no_phaseout(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # 2 kids, MAGI $120K MFJ, well under $400K phaseout threshold.
        # CTC = 2 * $2,000 = $4,000 (TY 2022 NOT under ARPA's $3K/$3,600
        # expansion -- that lapsed at end of 2021).
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_child_tax_credit("
            "  %s::NUMERIC, 2022::SMALLINT, 'mfj', %s)",
            Decimal("120000"),
            2,
        )
        _approx_dec(out, "4000.00")

    # ----- FICA (TY 2022 SS wage base = $147,000) -----

    def test_2022_fica_at_wage_base_boundary(self, tax_db: psycopg.Connection) -> None:
        # Wage = $147,000 single 2022 (exactly the SS cap).
        # SS:  6.2%  * 147,000 = 9,114.00
        # Med: 1.45% * 147,000 = 2,131.50
        # AddMed: max(0, 147K - 200K) * 0.009 = 0
        # Total: $11,245.50.
        out = _scalar(
            tax_db,
            "SELECT derived.f_fica_tax(%s, 2022::SMALLINT, 'single')",
            Decimal("147000"),
        )
        _approx_dec(out, "11245.50")

    def test_2022_fica_above_cap_caps_ss_at_2022_base(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Wage = $200K single 2022.
        # SS capped at 6.2% * 147,000 = 9,114.00 (NOT 168,600's $10,453.20).
        # This is the cross-year-correctness pin -- a copy/paste from the
        # 2023 or 2024 FICA seed would put SS at the wrong wage base and
        # this assertion catches it.
        # Med: 1.45% * 200,000 = 2,900.00
        # AddMed: 0.9% * (200K - 200K) = 0
        # Total: $12,014.00.
        out = _scalar(
            tax_db,
            "SELECT derived.f_fica_tax(%s, 2022::SMALLINT, 'single')",
            Decimal("200000"),
        )
        _approx_dec(out, "12014.00")

    # ----- Composite (federal + NJ + FICA) -----

    def test_2022_household_taxes_single_60k_full_breakdown(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Anchor scenario for TY 2022. Matches the structure of the
        # existing 2024 single $60K test, with the values shifted to
        # reflect the smaller 2022 std deduction and the lower 2022
        # bracket floors:
        #   federal: 5,968.00 (vs 5,216.00 in 2024)
        #   nj:      1,717.25 (unchanged; NJ brackets identical 2020-2024)
        #   fica:    4,590.00 (unchanged; wage < both 2022 and 2024 caps)
        #   total:  12,275.25
        #   eff:     0.20459 (= 12,275.25 / 60,000)
        with tax_db.cursor() as cur:
            cur.execute(
                "SELECT federal_income_tax, nj_state_tax, fica_tax, "
                "       total_tax, effective_rate, formula_version "
                "FROM derived.f_household_taxes("
                "  60000::NUMERIC, 60000::NUMERIC, 2022::SMALLINT,"
                "  'single', 0, 0, 0::NUMERIC)",
            )
            row = cur.fetchone()
        assert row is not None
        federal, nj, fica, total, eff_rate, fv = row
        _approx_dec(federal, "5968.00")
        _approx_dec(nj,      "1717.25")
        _approx_dec(fica,    "4590.00")
        _approx_dec(total,  "12275.25")
        _approx_dec(eff_rate, "0.20459", abs_tol="0.00001")
        assert fv == "1.1.0-tax-engine-v1"

    def test_2022_household_taxes_mfj_120k_2_kids_full_breakdown(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Anchor scenario for TY 2022, MFJ 2 deps + 2 qualifying children:
        #   federal: 7,936.00 (= tentative 11,936 - CTC 4,000)
        #   nj:      3,528.75 (Method B beats Method A by the $50 credit;
        #                     bracket walk identical to 2024 since NJ
        #                     hasn't changed brackets)
        #   fica:    9,180.00 (SS 7,440 + Med 1,740, no AddMed under
        #                     $250K MFJ threshold; wage < 2022 cap of
        #                     $147K so SS caps; actually $120K < $147K so
        #                     SS does NOT cap and = 6.2% * 120K = 7,440)
        #   total:  20,644.75
        #   eff:     0.17204 (= 20,644.75 / 120,000)
        with tax_db.cursor() as cur:
            cur.execute(
                "SELECT federal_income_tax, nj_state_tax, fica_tax, "
                "       total_tax, effective_rate "
                "FROM derived.f_household_taxes("
                "  120000::NUMERIC, 120000::NUMERIC, 2022::SMALLINT,"
                "  'mfj', 2, 2, 0::NUMERIC)",
            )
            row = cur.fetchone()
        assert row is not None
        federal, nj, fica, total, eff_rate = row
        _approx_dec(federal, "7936.00")
        _approx_dec(nj,      "3528.75")
        _approx_dec(fica,    "9180.00")
        _approx_dec(total,  "20644.75")
        _approx_dec(eff_rate, "0.17204", abs_tol="0.00001")


# ============================================================================
# 7d. Phase-5d -- TY 2018 hand-computed anchors
# ============================================================================


class TestPhase5Ty2018:
    """Hand-computed TY 2018 anchors. Source: Rev. Proc. 2018-18 + NJ-1040 2018.

    TY 2018 is the FIRST POST-TCJA YEAR for federal AND the FIRST YEAR
    for three NJ policy changes:
      (a) NJ Schedule I/II 10.75% Millionaires-Tax bracket above $5M
          (P.L. 2018 c.45). Pre-TY 2018 the top bracket was 8.97% above
          $500K. TY 2018 introduced the new 10.75% band, which P.L. 2020
          c.95 then expanded to start at $1M retroactive to TY 2020.
      (b) NJ property-tax deduction cap raised from $10K to $15K
          (same P.L. 2018 c.45) -- partial NJ response to the federal
          SALT cap imposed by TCJA s.11042.
      (c) NJ EITC match jumped from 35% (TY 2008-2017) to 37% (TY 2018),
          first step in the phased schedule that landed at 40% in TY 2020.

    Federal-side TY 2018 quirks pinned by tests:
      (1) HOH 24%/32% bracket floors EQUAL Single floors ($82,500 /
          $157,500) -- no $25 cross-status divergence like TY 2019.
      (2) Aged/blind unmarried add-on is $1,600 (not $1,650 like TY 2019).
          Not directly exercised by f_household_taxes (which doesn't
          take an aged/blind flag) but encoded in the seed for future
          use.
      (3) MFS 37% floor is $300,000 (= 600,000/2), separate from
          Single's $500,000 -- cross-status pin.
      (4) SS wage base $128,400, unique to TY 2018 in the seeded
          substrate -- single-row provenance check.
    """

    # ----- Federal bracket walks (verifiable against Rev. Proc. 2018-18 s.3.01) -----

    def test_2018_single_bracket_boundary(self, tax_db: psycopg.Connection) -> None:
        # Single 2018, taxable $9,525 (exactly the 10%->12% boundary).
        # 9,525 * 0.10 = $952.50 (also the printed "$952.50" base in
        # Rev. Proc. 2018-18 Table 3 12%-bracket formula row).
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2018::SMALLINT, 'single')",
            Decimal("9525"),
        )
        _approx_dec(out, "952.50")

    def test_2018_single_48000_taxable(self, tax_db: psycopg.Connection) -> None:
        # Single 2018, taxable $48,000 (= $60K gross - $12,000 std).
        #    9,525 * 0.10  =   952.50
        #   29,175 * 0.12  = 3,501.00   (9,525 -> 38,700)
        #    9,300 * 0.22  = 2,046.00   (38,700 -> 48,000)
        #   Total          = 6,499.50
        # Cross-check via printed formula:
        #   $4,453.50 + 0.22*(48,000 - 38,700) = 4,453.50 + 2,046 = 6,499.50.
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2018::SMALLINT, 'single')",
            Decimal("48000"),
        )
        _approx_dec(out, "6499.50")

    def test_2018_mfj_96000_taxable(self, tax_db: psycopg.Connection) -> None:
        # MFJ 2018, taxable $96,000 (= $120K gross - $24,000 std).
        #   19,050 * 0.10  =  1,905.00
        #   58,350 * 0.12  =  7,002.00   (19,050 -> 77,400)
        #   18,600 * 0.22  =  4,092.00   (77,400 -> 96,000)
        #   Total          = 12,999.00
        # Cross-check via printed formula:
        #   $8,907 + 0.22*(96,000 - 77,400) = 8,907 + 4,092 = 12,999.
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2018::SMALLINT, 'mfj')",
            Decimal("96000"),
        )
        _approx_dec(out, "12999.00")

    def test_2018_mfs_uses_separate_top_bracket(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # MFS 2018 has 37% starting at $300,000 (= 600,000 / 2), NOT
        # $500,000 like Single, NOT $600,000 like MFJ.
        # Walk MFS at exactly $300,000:
        #     952.50 (0 -> 9,525 @ 10%)
        #   3,501.00 (9,525 -> 38,700 @ 12%, 29,175 * 0.12)
        #   9,636.00 (38,700 -> 82,500 @ 22%, 43,800 * 0.22)
        #  18,000.00 (82,500 -> 157,500 @ 24%, 75,000 * 0.24)
        #  13,600.00 (157,500 -> 200,000 @ 32%, 42,500 * 0.32)
        #  35,000.00 (200,000 -> 300,000 @ 35%, 100,000 * 0.35)
        # = 80,689.50 (matches Rev. Proc. 2018-18 Table 4 row
        #              "$80,689.50 plus 37% of excess over $300,000").
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2018::SMALLINT, 'mfs')",
            Decimal("300000"),
        )
        _approx_dec(out, "80689.50")

    def test_2018_hoh_32pct_floor_equals_single(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # TY 2018 has HOH 32% floor = Single 32% floor = $157,500
        # (unlike TY 2019 where they diverge by $25). This is the
        # initial post-TCJA statutory equality; subsequent C-CPI-U
        # inflation indexing introduced small divergences in later
        # years. Walk HOH at exactly $157,500:
        #   1,360.00 (0 -> 13,600 @ 10%)
        # + 4,584.00 (13,600 -> 51,800 @ 12%, 38,200 * 0.12)
        # + 6,754.00 (51,800 -> 82,500 @ 22%, 30,700 * 0.22)
        # +18,000.00 (82,500 -> 157,500 @ 24%, 75,000 * 0.24)
        # =30,698.00 (matches Rev. Proc. 2018-18 Table 2 row
        #             "$30,698 plus 32% of excess over $157,500").
        out_hoh = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2018::SMALLINT, 'hoh')",
            Decimal("157500"),
        )
        _approx_dec(out_hoh, "30698.00")

        # Walk Single at the same $157,500. Should be different
        # because Single has different bottom brackets (12% from $9,525
        # vs HOH's 12% from $13,600 etc.):
        #     952.50 + 3,501.00 + 9,636.00 + 18,000.00 = 32,089.50
        # (matches Rev. Proc. 2018-18 Table 3 row "$32,089.50 plus
        #  32% of excess over $157,500").
        # Same FLOOR, different walk-to-floor (different-shape brackets
        # below the 32% boundary).
        out_single = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2018::SMALLINT, 'single')",
            Decimal("157500"),
        )
        _approx_dec(out_single, "32089.50")

    # ----- NJ bracket walks (cross-year invariance + cross-year divergence) -----

    def test_2018_nj_single_75000_matches_2024(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Below the Millionaires' Tax threshold, NJ Schedule I floors and
        # rates are identical TY 2018-2024. $75K Single TY 2018 must equal
        # $75K Single TY 2024 to the cent.
        out_2018 = _scalar(
            tax_db,
            "SELECT derived.f_apply_nj_state_brackets(%s, 2018::SMALLINT, 'single')",
            Decimal("75000"),
        )
        out_2024 = _scalar(
            tax_db,
            "SELECT derived.f_apply_nj_state_brackets(%s, 2024::SMALLINT, 'single')",
            Decimal("75000"),
        )
        _approx_dec(out_2018, "2651.25")
        _approx_dec(out_2018, str(out_2024))

    def test_2018_nj_single_2m_equals_2019_diverges_from_2020(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Cross-year-divergence pin extending the TY 2019 narrative:
        # TY 2018 and TY 2019 have IDENTICAL NJ schedules (both use the
        # P.L. 2018 c.45 structure with 10.75% above $5M); both differ
        # from TY 2020 by exactly $17,800 at $2M income.
        #
        # At $2M Single NJ taxable income:
        #   TY 2018: $164,273.75 (10.75% floor at $5M, 8.97% in $1M-$5M)
        #   TY 2019: $164,273.75 (same schedule as TY 2018)
        #   TY 2020: $182,073.75 (10.75% floor at $1M, 10.75% in $1M-$2M)
        #
        # This three-year assertion checks BOTH the TY 2018=TY 2019
        # equality (which a typo in seed 021's top-floor encoding would
        # break) AND the TY 2018 vs TY 2020 $17,800 divergence (which
        # confirms TY 2018 has the pre-P.L.2020 c.95 floor).
        out_2018 = _scalar(
            tax_db,
            "SELECT derived.f_apply_nj_state_brackets(%s, 2018::SMALLINT, 'single')",
            Decimal("2000000"),
        )
        out_2019 = _scalar(
            tax_db,
            "SELECT derived.f_apply_nj_state_brackets(%s, 2019::SMALLINT, 'single')",
            Decimal("2000000"),
        )
        out_2020 = _scalar(
            tax_db,
            "SELECT derived.f_apply_nj_state_brackets(%s, 2020::SMALLINT, 'single')",
            Decimal("2000000"),
        )
        _approx_dec(out_2018, "164273.75")
        _approx_dec(out_2018, str(out_2019))  # 2018 = 2019
        _approx_dec(out_2020, "182073.75")
        diff = Decimal(str(out_2020)) - Decimal(str(out_2018))
        assert diff == Decimal("17800.00"), (
            f"TY2018 -> TY2020 divergence at $2M should be exactly "
            f"$17,800.00 (= P.L.2020 c.95 effect), got {diff}"
        )

    def test_2018_nj_mfj_uses_245_bracket(self, tax_db: psycopg.Connection) -> None:
        # NJ Schedule II MFJ 2018 walks the 2.45% band. $60K NJ taxable:
        #   20,000 * 0.014  = 280.00
        #   30,000 * 0.0175 = 525.00
        #   10,000 * 0.0245 = 245.00
        #   Total           = 1,050.00
        # Pinned identical to TY 2019/2020/2024.
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_nj_state_brackets(%s, 2018::SMALLINT, 'mfj')",
            Decimal("60000"),
        )
        _approx_dec(out, "1050.00")

    # ----- Standard deduction (Rev. Proc. 2018-18 s.3.14) -----

    def test_2018_taxable_income_single_60k(self, tax_db: psycopg.Connection) -> None:
        # 60,000 - 12,000 (TY 2018 single std, FIRST YEAR of TCJA-doubled
        # std deduction) - 0 (TCJA exemption) = 48,000.
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_taxable_income(%s, 2018::SMALLINT, 'single', 0)",
            Decimal("60000"),
        )
        _approx_dec(out, "48000.00")

    def test_2018_taxable_income_mfj_120k_2_dependents(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # 120,000 - 24,000 (TY 2018 MFJ std) - 0 = 96,000.
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_taxable_income(%s, 2018::SMALLINT, 'mfj', %s)",
            Decimal("120000"),
            2,
        )
        _approx_dec(out, "96000.00")

    # ----- CTC (TY 2018 is FIRST YEAR of TCJA $2,000 / $1,400) -----

    def test_2018_ctc_first_tcja_year_two_kids_no_phaseout(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # 2 kids, MAGI $120K MFJ, well under $400K phaseout threshold.
        # CTC = 2 * $2,000 = $4,000. TY 2018 IS THE FIRST YEAR with
        # the TCJA-doubled $2,000 base (was $1,000 pre-TCJA). The
        # non-refundable CTC value is identical TY 2018-2020 and TY
        # 2022-2024 ($2,000 base, only TY 2021 differs structurally).
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_child_tax_credit("
            "  %s::NUMERIC, 2018::SMALLINT, 'mfj', %s)",
            Decimal("120000"),
            2,
        )
        _approx_dec(out, "4000.00")

    # ----- FICA (TY 2018 SS wage base = $128,400, UNIQUE in seeded substrate) -----

    def test_2018_fica_at_wage_base_boundary(self, tax_db: psycopg.Connection) -> None:
        # Wage = $128,400 single 2018 (exactly the SS cap).
        # SS:  6.2%  * 128,400 = 7,960.80
        # Med: 1.45% * 128,400 = 1,861.80
        # AddMed: max(0, 128,400 - 200K) * 0.009 = 0
        # Total: $9,822.60.
        out = _scalar(
            tax_db,
            "SELECT derived.f_fica_tax(%s, 2018::SMALLINT, 'single')",
            Decimal("128400"),
        )
        _approx_dec(out, "9822.60")

    def test_2018_fica_above_cap_caps_ss_at_2018_base(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Wage = $200K single 2018.
        # SS capped at 6.2% * 128,400 = 7,960.80
        # which is UNIQUE in the seeded substrate:
        #   2018 -> $7,960.80
        #   2019 -> $8,239.80 (cap $132,900)
        #   2020 -> $8,537.40 (cap $137,700)
        #   2022 -> $9,114.00 (cap $147,000)
        #   2024 -> $10,453.20 (cap $168,600)
        # All five values distinct -- a copy/paste from any other year
        # produces a different number, so this single assertion catches
        # all four cross-year-error directions.
        # Med:    1.45%  * 200,000 = 2,900.00
        # AddMed: 0.9%   * (200K - 200K) = 0
        # Total: 10,860.80.
        out = _scalar(
            tax_db,
            "SELECT derived.f_fica_tax(%s, 2018::SMALLINT, 'single')",
            Decimal("200000"),
        )
        _approx_dec(out, "10860.80")

    # ----- Composite (federal + NJ + FICA) -----

    def test_2018_household_taxes_single_60k_full_breakdown(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Anchor scenario for TY 2018, the EARLIEST seeded year in the
        # TCJA era. Multi-year YoY ladder for $60K Single:
        #
        #   federal: 6,499.50 (HIGHEST -- TY 2018 std deduction $12,000
        #                     is smallest in the seeded substrate;
        #                     bracket floors lowest, so most income
        #                     hits the 22% band)
        #   federal TY 2019: 6,374.50 (-125.00 vs 2018, -1.92pp)
        #   federal TY 2020: 6,262.00 (-112.50 vs 2019)
        #   federal TY 2022: 5,968.00 (-294.00 vs 2020)
        #   federal TY 2024: 5,216.00 (-752.00 vs 2022)
        #
        #   nj:      1,717.25 (UNCHANGED -- NJ brackets identical
        #                     TY 2018-2024 below Millionaires' Tax band)
        #   fica:    4,590.00 (UNCHANGED -- wage < all caps TY 2018-2024,
        #                     SS does not bind)
        #   total:  12,806.75
        #   eff:     0.21345 (= 12,806.75 / 60,000 = 0.21344583)
        with tax_db.cursor() as cur:
            cur.execute(
                "SELECT federal_income_tax, nj_state_tax, fica_tax, "
                "       total_tax, effective_rate, formula_version "
                "FROM derived.f_household_taxes("
                "  60000::NUMERIC, 60000::NUMERIC, 2018::SMALLINT,"
                "  'single', 0, 0, 0::NUMERIC)",
            )
            row = cur.fetchone()
        assert row is not None
        federal, nj, fica, total, eff_rate, fv = row
        _approx_dec(federal, "6499.50")
        _approx_dec(nj,      "1717.25")
        _approx_dec(fica,    "4590.00")
        _approx_dec(total,  "12806.75")
        _approx_dec(eff_rate, "0.21345", abs_tol="0.00001")
        assert fv == "1.1.0-tax-engine-v1"

    def test_2018_household_taxes_mfj_120k_2_kids_full_breakdown(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Anchor scenario for TY 2018, MFJ 2 deps + 2 qualifying children.
        # YoY ladder ($120K MFJ-2-2): 2018 -> 2019 -> 2020 -> 2022 -> 2024:
        #   federal: 8,999.00 (= tentative 12,999 - CTC 4,000)
        #     vs 2019 $8,749 / 2020 $8,524 / 2022 $7,936 / 2024 $7,176
        #   nj:      3,528.75 (UNCHANGED 2018-2024 at $115K NJ taxable)
        #   fica:    9,180.00 (UNCHANGED -- SS uncapped at $120K)
        #   total:  21,707.75
        #   eff:     0.18090 (= 21,707.75 / 120,000 = 0.180897917)
        # The 0.21pp YoY effective-rate drop from 2018 to 2019 reflects
        # ONLY federal std-deduction inflation indexing (NJ + FICA are
        # constants in this scenario).
        with tax_db.cursor() as cur:
            cur.execute(
                "SELECT federal_income_tax, nj_state_tax, fica_tax, "
                "       total_tax, effective_rate "
                "FROM derived.f_household_taxes("
                "  120000::NUMERIC, 120000::NUMERIC, 2018::SMALLINT,"
                "  'mfj', 2, 2, 0::NUMERIC)",
            )
            row = cur.fetchone()
        assert row is not None
        federal, nj, fica, total, eff_rate = row
        _approx_dec(federal, "8999.00")
        _approx_dec(nj,      "3528.75")
        _approx_dec(fica,    "9180.00")
        _approx_dec(total,  "21707.75")
        _approx_dec(eff_rate, "0.18090", abs_tol="0.00001")


# ============================================================================
# 7c. Phase-5c -- TY 2019 hand-computed anchors
# ============================================================================


class TestPhase5Ty2019:
    """Hand-computed TY 2019 anchors. Source: Rev. Proc. 2018-57 + NJ-1040 2019.

    TY 2019 is the LAST PRE-LOWERED-MILLIONAIRES-TAX-THRESHOLD year:
    the 10.75% NJ top bracket starts at $5M in TY 2019 (P.L. 2018 c.45)
    and at $1M in TY 2020+ (P.L. 2020 c.95 retroactive). The
    test_2019_nj_single_2m_diverges_from_2020_by_17800 case is the
    cross-year divergence pin -- it asserts $17,800 of additional
    NJ tax burden between the two years at $2M income, which is
    EXACTLY the dollar effect of the threshold-lowering policy.

    Three other TY-2019-specific quirks are pinned:
      (1) HOH 32%-bracket floor is $160,700, NOT $160,725 like Single
          (TCJA rounding divergence under IRC s.1(j)(2)(B)).
      (2) MFS top floor is $306,175 (= 612,350/2), separate cross-status
          pin (same shape as TY 2020/2022 but with TY 2019 dollar values).
      (3) NJ EITC match rate is 39%, unique to TY 2019 in the seeded
          substrate (was 37% TY 2018, 39% TY 2019, 40% TY 2020+).
    """

    # ----- Federal bracket walks (verifiable against Rev. Proc. 2018-57 s.3.01) -----

    def test_2019_single_bracket_boundary(self, tax_db: psycopg.Connection) -> None:
        # Single 2019, taxable $9,700 (exactly the 10%->12% boundary).
        # 9,700 * 0.10 = $970.00 (also the printed "$970" base in the
        # Rev. Proc. 2018-57 Table 3 12%-bracket formula row).
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2019::SMALLINT, 'single')",
            Decimal("9700"),
        )
        _approx_dec(out, "970.00")

    def test_2019_single_47800_taxable(self, tax_db: psycopg.Connection) -> None:
        # Single 2019, taxable $47,800 (= $60K gross - $12,200 std).
        #    9,700 * 0.10  =   970.00
        #   29,775 * 0.12  = 3,573.00   (9,700 -> 39,475)
        #    8,325 * 0.22  = 1,831.50   (39,475 -> 47,800)
        #   Total          = 6,374.50
        # Cross-check via printed formula:
        #   $4,543 + 0.22*(47,800 - 39,475) = 4,543 + 1,831.50 = 6,374.50.
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2019::SMALLINT, 'single')",
            Decimal("47800"),
        )
        _approx_dec(out, "6374.50")

    def test_2019_mfj_95600_taxable(self, tax_db: psycopg.Connection) -> None:
        # MFJ 2019, taxable $95,600 (= $120K gross - $24,400 std).
        #   19,400 * 0.10  =  1,940.00
        #   59,550 * 0.12  =  7,146.00   (19,400 -> 78,950)
        #   16,650 * 0.22  =  3,663.00   (78,950 -> 95,600)
        #   Total          = 12,749.00
        # Cross-check via printed formula:
        #   $9,086 + 0.22*(95,600 - 78,950) = 9,086 + 3,663 = 12,749.
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2019::SMALLINT, 'mfj')",
            Decimal("95600"),
        )
        _approx_dec(out, "12749.00")

    def test_2019_mfs_uses_separate_top_bracket(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # MFS 2019 has 37% starting at $306,175 (NOT $510,300 like Single,
        # NOT $612,350 like MFJ). The MFS top floor is exactly half the
        # MFJ floor by IRC s.1(j)(2)(D). Walk MFS at exactly $306,175:
        #    9,700 * 0.10   =    970.00
        #   29,775 * 0.12   =  3,573.00
        #   44,725 * 0.22   =  9,839.50   (39,475 -> 84,200)
        #   76,525 * 0.24   = 18,366.00   (84,200 -> 160,725)
        #   43,375 * 0.32   = 13,880.00   (160,725 -> 204,100)
        #  102,075 * 0.35   = 35,726.25   (204,100 -> 306,175)
        #   Total at floor  = 82,354.75   (matches Rev. Proc. 2018-57
        #                                  Table 4 row "$82,354.75 plus
        #                                  37% of excess over $306,175").
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2019::SMALLINT, 'mfs')",
            Decimal("306175"),
        )
        _approx_dec(out, "82354.75")

    def test_2019_hoh_32pct_floor_diverges_from_single(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # TY 2019 has a $25 cross-status divergence: Single 32% floor is
        # $160,725, HOH 32% floor is $160,700. This is a TCJA rounding
        # quirk under IRC s.1(j)(2)(B) where HOH inflation factors are
        # computed independently from Single. A copy/paste from Table 3
        # into Table 2 during seeding would collapse this divergence.
        #
        # Walk HOH at exactly the HOH 32% floor ($160,700):
        #     1,385.00 (0 -> 13,850 @ 10%)
        #   + 4,680.00 (13,850 -> 52,850 @ 12%, 39,000 * 0.12)
        #   + 6,897.00 (52,850 -> 84,200 @ 22%, 31,350 * 0.22)
        #   +18,360.00 (84,200 -> 160,700 @ 24%, 76,500 * 0.24)
        #   = 31,322.00 (matches Rev. Proc. 2018-57 Table 2 row
        #                "$31,322 plus 32% of excess over $160,700").
        out_hoh = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2019::SMALLINT, 'hoh')",
            Decimal("160700"),
        )
        _approx_dec(out_hoh, "31322.00")

        # Walk Single at exactly $160,725 (the Single 32% floor):
        #     970.00 + 3,573.00 + 9,839.50 + 18,366.00 = 32,748.50
        # (matches Rev. Proc. 2018-57 Table 3 row "$32,748.50 plus
        #  32% of excess over $160,725").
        # If HOH were mistakenly seeded with Single's $160,725 floor,
        # the HOH walk to $160,700 would produce 24% instead of 0%
        # of the marginal-bracket entry, giving a different number.
        out_single = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2019::SMALLINT, 'single')",
            Decimal("160725"),
        )
        _approx_dec(out_single, "32748.50")

    # ----- NJ bracket walks (the cross-year-divergence headline test) -----

    def test_2019_nj_single_75000_matches_2024(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Below the Millionaires' Tax threshold ($1M / $5M), NJ rates +
        # floors are STABLE across TY 2018-2024 (no schedule change in
        # the bottom 6 brackets since 2004). $75K Single TY 2019 must
        # equal $75K Single TY 2024 to the cent. A typo in seed 019
        # that mis-set a low-income floor trips here.
        out_2019 = _scalar(
            tax_db,
            "SELECT derived.f_apply_nj_state_brackets(%s, 2019::SMALLINT, 'single')",
            Decimal("75000"),
        )
        out_2024 = _scalar(
            tax_db,
            "SELECT derived.f_apply_nj_state_brackets(%s, 2024::SMALLINT, 'single')",
            Decimal("75000"),
        )
        _approx_dec(out_2019, "2651.25")
        _approx_dec(out_2019, str(out_2024))

    def test_2019_nj_single_2m_diverges_from_2020_by_17800(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # THE HEADLINE TEST FOR TY 2019: P.L. 2018 c.45 placed the
        # 10.75% Millionaires' Tax floor at $5M for TY 2018-2019.
        # P.L. 2020 c.95 lowered it to $1M, retroactive to TY 2020.
        # At $2M Single NJ taxable income:
        #
        #   TY 2019 walk (10.75% floor at $5M, so $2M is in the
        #   $500K-$5M @ 8.97% band):
        #          280.00 (0 -> 20K @ 1.4%)
        #        + 262.50 (20K -> 35K @ 1.75%)
        #        + 175.00 (35K -> 40K @ 3.5%)
        #      + 1,933.75 (40K -> 75K @ 5.525%, 35K*0.05525)
        #     + 27,072.50 (75K -> 500K @ 6.37%, 425K*0.0637)
        #    + 134,550.00 (500K -> 2M @ 8.97%, 1.5M*0.0897)
        #     = 164,273.75
        #
        #   TY 2020 walk (10.75% floor at $1M):
        #          ... same first 5 brackets = 29,723.75
        #     + 44,850.00 (500K -> 1M @ 8.97%, 500K*0.0897)
        #    + 107,500.00 (1M -> 2M @ 10.75%, 1M*0.1075)
        #     = 182,073.75
        #
        # DIFFERENCE: $182,073.75 - $164,273.75 = $17,800.00
        # which equals exactly $1M (the income in the $1M-$2M band)
        # times (10.75% - 8.97%) = 1.78%. This is the EXACT dollar
        # effect of P.L. 2020 c.95 at $2M income, the most rigorous
        # possible verification of the seed's correctness.
        #
        # If seed 019 mistakenly encoded TY 2020's $1M floor into
        # TY 2019, the 2019 walk would also produce $182,073.75 and
        # the divergence would COLLAPSE TO ZERO -- this assertion
        # would fail loudly.
        out_2019 = _scalar(
            tax_db,
            "SELECT derived.f_apply_nj_state_brackets(%s, 2019::SMALLINT, 'single')",
            Decimal("2000000"),
        )
        out_2020 = _scalar(
            tax_db,
            "SELECT derived.f_apply_nj_state_brackets(%s, 2020::SMALLINT, 'single')",
            Decimal("2000000"),
        )
        _approx_dec(out_2019, "164273.75")
        _approx_dec(out_2020, "182073.75")
        # And the divergence itself, hand-computed:
        diff = Decimal(str(out_2020)) - Decimal(str(out_2019))
        assert diff == Decimal("17800.00"), (
            f"P.L.2020 c.95 cross-year divergence at $2M should be exactly "
            f"$17,800.00 (= $1M * 1.78pp), got {diff}"
        )

    def test_2019_nj_mfj_uses_245_bracket(self, tax_db: psycopg.Connection) -> None:
        # NJ Schedule II MFJ 2019 walks the 2.45% band. $60K NJ taxable:
        #   20,000 * 0.014  = 280.00
        #   30,000 * 0.0175 = 525.00
        #   10,000 * 0.0245 = 245.00
        #   Total           = 1,050.00
        # Pinned identical to TY 2020/2022/2024 (cross-year invariance
        # below Millionaires' Tax threshold).
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_nj_state_brackets(%s, 2019::SMALLINT, 'mfj')",
            Decimal("60000"),
        )
        _approx_dec(out, "1050.00")

    # ----- Standard deduction (Rev. Proc. 2018-57 s.3.16) -----

    def test_2019_taxable_income_single_60k(self, tax_db: psycopg.Connection) -> None:
        # 60,000 - 12,200 (TY 2019 single std) - 0 (TCJA exemption) = 47,800.
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_taxable_income(%s, 2019::SMALLINT, 'single', 0)",
            Decimal("60000"),
        )
        _approx_dec(out, "47800.00")

    def test_2019_taxable_income_mfj_120k_2_dependents(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # 120,000 - 24,400 (TY 2019 MFJ std) - 0 = 95,600.
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_taxable_income(%s, 2019::SMALLINT, 'mfj', %s)",
            Decimal("120000"),
            2,
        )
        _approx_dec(out, "95600.00")

    # ----- CTC (TY 2019 is pre-ARPA TCJA: $2,000 / $1,400 / $200K-$400K) -----

    def test_2019_ctc_pre_arpa_two_kids_no_phaseout(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # 2 kids, MAGI $120K MFJ, well under $400K phaseout threshold.
        # CTC = 2 * $2,000 = $4,000. TY 2019 is pre-ARPA TCJA. The
        # non-refundable CTC value is identical TY 2018-2020 and TY
        # 2022-2024; only TY 2021 differs structurally (and remains
        # blocked at NULL by the substrate-honesty pin).
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_child_tax_credit("
            "  %s::NUMERIC, 2019::SMALLINT, 'mfj', %s)",
            Decimal("120000"),
            2,
        )
        _approx_dec(out, "4000.00")

    # ----- FICA (TY 2019 SS wage base = $132,900) -----

    def test_2019_fica_at_wage_base_boundary(self, tax_db: psycopg.Connection) -> None:
        # Wage = $132,900 single 2019 (exactly the SS cap).
        # SS:  6.2%  * 132,900 = 8,239.80
        # Med: 1.45% * 132,900 = 1,927.05
        # AddMed: max(0, 132,900 - 200K) * 0.009 = 0
        # Total: $10,166.85.
        out = _scalar(
            tax_db,
            "SELECT derived.f_fica_tax(%s, 2019::SMALLINT, 'single')",
            Decimal("132900"),
        )
        _approx_dec(out, "10166.85")

    def test_2019_fica_above_cap_caps_ss_at_2019_base(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Wage = $200K single 2019.
        # SS capped at 6.2% * 132,900 = 8,239.80
        # (NOT 137,700's $8,537.40 from TY 2020,
        #  NOT 147,000's $9,114 from TY 2022,
        #  NOT 168,600's $10,453.20 from TY 2024).
        # Cross-year-correctness pin: a copy/paste from any other
        # FICA seed binds SS to the wrong wage base; this assertion
        # catches all four common error directions in one row.
        # Med:    1.45%  * 200,000 = 2,900.00
        # AddMed: 0.9%   * (200K - 200K) = 0
        # Total: 11,139.80.
        out = _scalar(
            tax_db,
            "SELECT derived.f_fica_tax(%s, 2019::SMALLINT, 'single')",
            Decimal("200000"),
        )
        _approx_dec(out, "11139.80")

    # ----- Composite (federal + NJ + FICA) -----

    def test_2019_household_taxes_single_60k_full_breakdown(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Anchor scenario for TY 2019, the EARLIEST seeded year.
        # Multi-year YoY ladder for $60K Single at the same household
        # profile (this is the granular "tax burden eroded" signal
        # the spec demanded, isolated to federal-only because NJ +
        # FICA are unchanged at this income across all four years):
        #
        #   federal: 6,374.50 (HIGHEST -- 2019 std deduction $12,200
        #                     was smallest in the seeded substrate;
        #                     bracket floors lowest, so most income
        #                     hits the 22% band)
        #   federal TY 2020:  6,262.00  (-112.50 vs 2019)
        #   federal TY 2022:  5,968.00  (-294.00 vs 2020)
        #   federal TY 2024:  5,216.00  (-752.00 vs 2022)
        #
        #   nj:      1,717.25 (UNCHANGED -- NJ brackets identical
        #                     2019-2024 below Millionaires' Tax band)
        #   fica:    4,590.00 (UNCHANGED -- wage < all caps 2019-2024,
        #                     so SS does not bind; rates 6.2%+1.45%
        #                     are statutory and unchanged)
        #   total:  12,681.75
        #   eff:     0.21136 (= 12,681.75 / 60,000 = 0.2113625)
        with tax_db.cursor() as cur:
            cur.execute(
                "SELECT federal_income_tax, nj_state_tax, fica_tax, "
                "       total_tax, effective_rate, formula_version "
                "FROM derived.f_household_taxes("
                "  60000::NUMERIC, 60000::NUMERIC, 2019::SMALLINT,"
                "  'single', 0, 0, 0::NUMERIC)",
            )
            row = cur.fetchone()
        assert row is not None
        federal, nj, fica, total, eff_rate, fv = row
        _approx_dec(federal, "6374.50")
        _approx_dec(nj,      "1717.25")
        _approx_dec(fica,    "4590.00")
        _approx_dec(total,  "12681.75")
        _approx_dec(eff_rate, "0.21136", abs_tol="0.00001")
        assert fv == "1.1.0-tax-engine-v1"

    def test_2019_household_taxes_mfj_120k_2_kids_full_breakdown(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Anchor scenario for TY 2019, MFJ 2 deps + 2 qualifying children.
        # YoY ladder ($120K MFJ-2-2): 2019 -> 2020 -> 2022 -> 2024:
        #   federal: 8,749.00 (= tentative 12,749 - CTC 4,000)
        #     vs 2020 $8,524 / 2022 $7,936 / 2024 $7,176
        #   nj:      3,528.75 (UNCHANGED 2019-2024 at $115K NJ taxable)
        #   fica:    9,180.00 (UNCHANGED -- SS uncapped at $120K)
        #   total:  21,457.75
        #   eff:     0.17881 (= 21,457.75 / 120,000 = 0.17881458)
        # The 0.49pp YoY effective-rate drop from 2019 to 2020 reflects
        # ONLY the federal std-deduction inflation adjustment + bracket
        # creep relief; NJ + FICA are constants in this scenario, which
        # makes the YoY signal entirely attributable to TCJA inflation
        # mechanics. This is the kind of granular "did Congress indexing
        # actually keep up with inflation?" signal the spec demanded.
        with tax_db.cursor() as cur:
            cur.execute(
                "SELECT federal_income_tax, nj_state_tax, fica_tax, "
                "       total_tax, effective_rate "
                "FROM derived.f_household_taxes("
                "  120000::NUMERIC, 120000::NUMERIC, 2019::SMALLINT,"
                "  'mfj', 2, 2, 0::NUMERIC)",
            )
            row = cur.fetchone()
        assert row is not None
        federal, nj, fica, total, eff_rate = row
        _approx_dec(federal, "8749.00")
        _approx_dec(nj,      "3528.75")
        _approx_dec(fica,    "9180.00")
        _approx_dec(total,  "21457.75")
        _approx_dec(eff_rate, "0.17881", abs_tol="0.00001")


# ============================================================================
# 7b. Phase-5b -- TY 2020 hand-computed anchors
# ============================================================================


class TestPhase5Ty2020:
    """Hand-computed TY 2020 anchors. Source: Rev. Proc. 2019-44 + NJ-1040 2020.

    Cross-year invariance reminder: TY 2020 NJ tax rates and bracket floors
    are IDENTICAL to TY 2022, TY 2023, TY 2024 because P.L. 2020 c.95
    (signed 2020-09-29) was retroactive to 2020-01-01. We pin one of the
    NJ asserts at the same dollar value as the corresponding 2024 assert
    so a future seed typo is loudly caught.
    """

    # ----- Federal bracket walks (verifiable against Rev. Proc. 2019-44 s.3.01) -----

    def test_2020_single_bracket_boundary(self, tax_db: psycopg.Connection) -> None:
        # Single 2020, taxable $9,875 (exactly the 10%->12% boundary).
        # 9,875 * 0.10 = $987.50 (also the printed "$987.50" base in
        # Rev. Proc. 2019-44 Table 3 12%-bracket formula row).
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2020::SMALLINT, 'single')",
            Decimal("9875"),
        )
        _approx_dec(out, "987.50")

    def test_2020_single_47600_taxable(self, tax_db: psycopg.Connection) -> None:
        # Single 2020, taxable $47,600 (= $60K gross - $12,400 std).
        #    9,875 * 0.10  =   987.50
        #   30,250 * 0.12  = 3,630.00   (9,875 -> 40,125)
        #    7,475 * 0.22  = 1,644.50   (40,125 -> 47,600)
        #   Total          = 6,262.00
        # Cross-check via printed formula:
        #   $4,617.50 + 0.22*(47,600 - 40,125) = 4,617.50 + 1,644.50 = 6,262.00.
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2020::SMALLINT, 'single')",
            Decimal("47600"),
        )
        _approx_dec(out, "6262.00")

    def test_2020_mfj_95200_taxable(self, tax_db: psycopg.Connection) -> None:
        # MFJ 2020, taxable $95,200 (= $120K gross - $24,800 std).
        #   19,750 * 0.10  =  1,975.00
        #   60,500 * 0.12  =  7,260.00   (19,750 -> 80,250)
        #   14,950 * 0.22  =  3,289.00   (80,250 -> 95,200)
        #   Total          = 12,524.00
        # Cross-check via printed formula:
        #   $9,235 + 0.22*(95,200 - 80,250) = 9,235 + 3,289 = 12,524.
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2020::SMALLINT, 'mfj')",
            Decimal("95200"),
        )
        _approx_dec(out, "12524.00")

    def test_2020_mfs_uses_separate_top_bracket(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # MFS 2020 has 37% starting at $311,025 (NOT $518,400 like Single,
        # NOT $622,050 like MFJ). The MFS top floor is exactly half the MFJ
        # floor by IRC s.1(j)(2)(D); this is the cross-status divergence
        # that the seed must NOT collapse onto the Single floor. Walk MFS
        # at exactly $311,025 (the boundary):
        #    9,875 * 0.10   =    987.50
        #   30,250 * 0.12   =  3,630.00
        #   45,400 * 0.22   =  9,988.00   (40,125 -> 85,525)
        #   77,775 * 0.24   = 18,666.00   (85,525 -> 163,300)
        #   44,050 * 0.32   = 14,096.00   (163,300 -> 207,350)
        #  103,675 * 0.35   = 36,286.25   (207,350 -> 311,025)
        #   Total at floor  = 83,653.75   (matches Rev. Proc. 2019-44
        #                                  Table 4 row "$83,653.75 plus
        #                                  37% of excess over $311,025").
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2020::SMALLINT, 'mfs')",
            Decimal("311025"),
        )
        _approx_dec(out, "83653.75")

    # ----- NJ bracket walks (rates + floors unchanged 2020-2024 by P.L.2020 c.95) -----

    def test_2020_nj_single_75000_matches_2024(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # NJ Schedule I rates + floors are identical 2020-2024 because
        # P.L. 2020 c.95 was retroactive to 2020-01-01. $75K Single 2020
        # MUST equal $75K Single 2024 to the cent. A typo in seed 017 that
        # accidentally encodes the pre-Millionaires' Tax brackets, or that
        # mis-sets a floor, is caught here.
        out_2020 = _scalar(
            tax_db,
            "SELECT derived.f_apply_nj_state_brackets(%s, 2020::SMALLINT, 'single')",
            Decimal("75000"),
        )
        out_2024 = _scalar(
            tax_db,
            "SELECT derived.f_apply_nj_state_brackets(%s, 2024::SMALLINT, 'single')",
            Decimal("75000"),
        )
        _approx_dec(out_2020, "2651.25")
        _approx_dec(out_2020, str(out_2024))

    def test_2020_nj_mfj_uses_245_bracket(self, tax_db: psycopg.Connection) -> None:
        # NJ Schedule II MFJ 2020 walks the 2.45% band. $60K NJ taxable:
        #   20,000 * 0.014  = 280.00
        #   30,000 * 0.0175 = 525.00
        #   10,000 * 0.0245 = 245.00
        #   Total           = 1,050.00
        # Pinned identical to TY 2022/2024 (cross-year invariance).
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_nj_state_brackets(%s, 2020::SMALLINT, 'mfj')",
            Decimal("60000"),
        )
        _approx_dec(out, "1050.00")

    # ----- Standard deduction (Rev. Proc. 2019-44 s.3.16) -----

    def test_2020_taxable_income_single_60k(self, tax_db: psycopg.Connection) -> None:
        # 60,000 - 12,400 (TY 2020 single std) - 0 (TCJA exemption) = 47,600.
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_taxable_income(%s, 2020::SMALLINT, 'single', 0)",
            Decimal("60000"),
        )
        _approx_dec(out, "47600.00")

    def test_2020_taxable_income_mfj_120k_2_dependents(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # 120,000 - 24,800 (TY 2020 MFJ std) - 0 = 95,200.
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_taxable_income(%s, 2020::SMALLINT, 'mfj', %s)",
            Decimal("120000"),
            2,
        )
        _approx_dec(out, "95200.00")

    # ----- CTC (TY 2020 is pure pre-ARPA TCJA: $2,000 / $1,400 / $200K-$400K) -----

    def test_2020_ctc_pre_arpa_two_kids_no_phaseout(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # 2 kids, MAGI $120K MFJ, well under $400K phaseout threshold.
        # CTC = 2 * $2,000 = $4,000. TY 2020 is pre-ARPA -- the $3K/$3,600
        # expansion only applied for TY 2021. The non-refundable CTC value
        # for $120K MFJ with 2 kids is identical 2018-2020 and 2022-2024;
        # only TY 2021 differs structurally. This test plus the 2021
        # substrate-honesty assertion together pin BOTH directions of the
        # ARPA gap (correct base for non-ARPA years, NULL for ARPA year).
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_child_tax_credit("
            "  %s::NUMERIC, 2020::SMALLINT, 'mfj', %s)",
            Decimal("120000"),
            2,
        )
        _approx_dec(out, "4000.00")

    # ----- FICA (TY 2020 SS wage base = $137,700) -----

    def test_2020_fica_at_wage_base_boundary(self, tax_db: psycopg.Connection) -> None:
        # Wage = $137,700 single 2020 (exactly the SS cap).
        # SS:  6.2%  * 137,700 = 8,537.40
        # Med: 1.45% * 137,700 = 1,996.65
        # AddMed: max(0, 137,700 - 200K) * 0.009 = 0
        # Total: $10,534.05.
        out = _scalar(
            tax_db,
            "SELECT derived.f_fica_tax(%s, 2020::SMALLINT, 'single')",
            Decimal("137700"),
        )
        _approx_dec(out, "10534.05")

    def test_2020_fica_above_cap_caps_ss_at_2020_base(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Wage = $200K single 2020.
        # SS capped at 6.2% * 137,700 = 8,537.40 (NOT 147,000's $9,114
        # from TY 2022, NOT 168,600's $10,453.20 from TY 2024).
        # Cross-year-correctness pin: a copy/paste from the 2022 or 2024
        # FICA seed would bind SS to the wrong wage base; this assertion
        # catches it.
        # Med:    1.45%  * 200,000 = 2,900.00
        # AddMed: 0.9%   * (200K - 200K) = 0
        # Total: 11,437.40.
        out = _scalar(
            tax_db,
            "SELECT derived.f_fica_tax(%s, 2020::SMALLINT, 'single')",
            Decimal("200000"),
        )
        _approx_dec(out, "11437.40")

    # ----- Composite (federal + NJ + FICA) -----

    def test_2020_household_taxes_single_60k_full_breakdown(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Anchor scenario for TY 2020.
        # Compared to TY 2022 same scenario (5,968 / 1,717.25 / 4,590):
        #   federal 6,262.00 (LARGER -- 2020 std deduction $12,400 < 2022's
        #                    $12,950, so taxable income is higher; 2020
        #                    bracket floors are also lower so more
        #                    income hits the 22% band).
        #   nj      1,717.25 (UNCHANGED -- NJ brackets identical 2020-2024).
        #   fica    4,590.00 (UNCHANGED -- wage < both 2020 and 2022 caps,
        #                    so SS doesn't bind).
        #   total   12,569.25
        #   eff      0.20949 (= 12,569.25 / 60,000 = 0.2094875)
        with tax_db.cursor() as cur:
            cur.execute(
                "SELECT federal_income_tax, nj_state_tax, fica_tax, "
                "       total_tax, effective_rate, formula_version "
                "FROM derived.f_household_taxes("
                "  60000::NUMERIC, 60000::NUMERIC, 2020::SMALLINT,"
                "  'single', 0, 0, 0::NUMERIC)",
            )
            row = cur.fetchone()
        assert row is not None
        federal, nj, fica, total, eff_rate, fv = row
        _approx_dec(federal, "6262.00")
        _approx_dec(nj,      "1717.25")
        _approx_dec(fica,    "4590.00")
        _approx_dec(total,  "12569.25")
        _approx_dec(eff_rate, "0.20949", abs_tol="0.00001")
        assert fv == "1.1.0-tax-engine-v1"

    def test_2020_household_taxes_mfj_120k_2_kids_full_breakdown(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Anchor scenario for TY 2020, MFJ 2 deps + 2 qualifying children.
        #   federal 8,524.00 (= tentative 12,524 - CTC 4,000)
        #   nj      3,528.75 (Method B beats Method A by the $50 credit;
        #                    bracket walk identical to 2022/2024)
        #   fica    9,180.00 (SS 7,440 + Med 1,740, no AddMed under
        #                    $250K MFJ threshold; wage < 2020 cap of
        #                    $137,700 so SS does NOT cap and = 6.2%*120K)
        #   total  21,232.75
        #   eff     0.17694 (= 21,232.75 / 120,000 = 0.1769395833)
        with tax_db.cursor() as cur:
            cur.execute(
                "SELECT federal_income_tax, nj_state_tax, fica_tax, "
                "       total_tax, effective_rate "
                "FROM derived.f_household_taxes("
                "  120000::NUMERIC, 120000::NUMERIC, 2020::SMALLINT,"
                "  'mfj', 2, 2, 0::NUMERIC)",
            )
            row = cur.fetchone()
        assert row is not None
        federal, nj, fica, total, eff_rate = row
        _approx_dec(federal, "8524.00")
        _approx_dec(nj,      "3528.75")
        _approx_dec(fica,    "9180.00")
        _approx_dec(total,  "21232.75")
        _approx_dec(eff_rate, "0.17694", abs_tol="0.00001")


# ============================================================================
# 7e. Phase-5e -- TY 2017 hand-computed anchors (THE LAST PRE-TCJA YEAR)
# ============================================================================


class TestPhase5Ty2017:
    """TY 2017 hand-computed anchors. Source: Rev. Proc. 2016-55 + NJ-1040 2017.

    TY 2017 is THE LAST PRE-TCJA YEAR. The single-largest cross-year tax
    divergence in the seeded substrate sits at the TY 2017 -> TY 2018
    boundary, driven by FIVE simultaneous TCJA changes:
      (1) Bracket ladder swap: 10/15/25/28/33/35/39.6 -> 10/12/22/24/32/35/37
      (2) Standard deduction roughly doubled
      (3) Personal exemption killed: $4,050 -> $0
      (4) CTC doubled and phaseout pushed up: $1,000 / $75K-$110K
          -> $2,000 / $200K-$400K
      (5) NJ side: P.L. 2017 c.36 introduced $3,000 veteran exemption
          (FIRST YEAR with the veteran exemption); P.L. 2018 c.45 raised
          PTD cap from $10K to $15K and EITC match from 30% to 37% in
          TY 2018; the cross-era pin uses the union of these.

    These tests pin each TCJA change in isolation AND the headline
    aggregate divergence (federal tax cut at constant household profile
    crossing the 2017->2018 boundary).
    """

    def test_2017_single_bracket_walk_48000(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Single 2017 taxable $48,000 (the same income point as TY 2018's
        # anchor for cross-era comparison).
        #    9,325 * 0.10 = 932.50
        #   28,625 * 0.15 = 4,293.75   (9,325 -> 37,950)
        #   10,050 * 0.25 = 2,512.50   (37,950 -> 48,000)
        #   Total         = 7,738.75
        # Cross-check Rev. Proc. 2016-55 Table 3 formula row:
        #   $5,226.25 + 0.25 * (48,000 - 37,950) = 5,226.25 + 2,512.50 = 7,738.75.
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2017::SMALLINT, 'single')",
            Decimal("48000"),
        )
        _approx_dec(out, "7738.75")

    def test_2017_top_bracket_uses_396pct(self, tax_db: psycopg.Connection) -> None:
        # Single 2017 at exactly $418,400 (the 39.6% floor):
        # walk 0 -> 418,400 must equal printed Table 3 base $121,505.25.
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2017::SMALLINT, 'single')",
            Decimal("418400"),
        )
        _approx_dec(out, "121505.25")

    def test_2017_personal_exemption_4050(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # PE $4,050 (vs $0 TY 2018+) is the single most concrete TCJA
        # change for low-income filers. Verify by composite:
        # TY 2017 single $52,400 gross, no deps:
        #   taxable = 52,400 - 6,350 std - 4,050 PE = 42,000
        #   tentative = 932.50 + 0.15*(37,950-9,325) + 0.25*(42,000-37,950)
        #             = 932.50 + 4,293.75 + 1,012.50 = 6,238.75
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_taxable_income("
            "  %s, 2017::SMALLINT, 'single', 0)",
            Decimal("52400"),
        )
        _approx_dec(out, "42000.00")

    def test_2017_ctc_phaseout_at_120k_mfj(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Pre-TCJA CTC phaseout: $50/$1000 above $110K MFJ.
        # 2 kids, MFJ AGI $120K:
        #   excess = 120,000 - 110,000 = 10,000
        #   reduction = 10,000 * 0.05 = 500
        #   credit = 2 * 1,000 - 500 = 1,500
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_child_tax_credit("
            "  %s, 2017::SMALLINT, 'mfj', 2)",
            Decimal("120000"),
        )
        _approx_dec(out, "1500.00")

    def test_2017_nj_single_75000_matches_2024(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # NJ Schedule I unchanged TY 2010-2017 below 8.97% top.
        # $75K Single -> NJ 2017 == NJ 2024 (cross-year invariance pin).
        out_2017 = _scalar(
            tax_db,
            "SELECT derived.f_apply_nj_state_brackets(%s, 2017::SMALLINT, 'single')",
            Decimal("75000"),
        )
        _approx_dec(out_2017, "2651.25")

    def test_2017_nj_no_millionaires_tax_at_2m(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # TY 2017 NJ has NO 10.75% bracket. At $2M Single:
        #   below $500K walks to NJ 2018-equivalent partial sum,
        #   then $500K -> $2M at 8.97% across the WHOLE band:
        #     8.97% * 1,500,000 = 134,550
        #   Plus the up-to-500K ladder. The total is therefore
        #   strictly LESS than TY 2018 (which adds 10.75% above $5M
        #   but TY 2018 also has 10.75% in the band... wait, TY 2018
        #   has 10.75% only above $5M, so $2M is identical to TY 2017).
        # Pin: NJ($2M, 'single', 2017) == NJ($2M, 'single', 2018)
        out_2017 = _scalar(
            tax_db,
            "SELECT derived.f_apply_nj_state_brackets(%s, 2017::SMALLINT, 'single')",
            Decimal("2000000"),
        )
        out_2018 = _scalar(
            tax_db,
            "SELECT derived.f_apply_nj_state_brackets(%s, 2018::SMALLINT, 'single')",
            Decimal("2000000"),
        )
        _approx_dec(out_2017, str(out_2018))
        _approx_dec(out_2017, "164273.75")

    def test_2017_fica_at_127200_wage_base(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # SS wage base $127,200, the TY 2017 cap (jumped from $118,500 in
        # TY 2016 -- one of the largest single-year cap jumps because TY
        # 2016 had been frozen at TY 2015's level due to zero COLA).
        # At exactly $127,200: SS = 127,200 * 0.062 = 7,886.40;
        # Medicare = 127,200 * 0.0145 = 1,844.40; total = 9,730.80.
        out = _scalar(
            tax_db,
            "SELECT derived.f_fica_tax(%s, 2017::SMALLINT, 'single')",
            Decimal("127200"),
        )
        _approx_dec(out, "9730.80")

    def test_2017_to_2018_tcja_federal_cut_single_60k(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # THE HEADLINE CROSS-ERA DIVERGENCE PIN.
        # Single, no deps, gross $60K:
        #   TY 2017: taxable = 60,000 - 6,350 std - 4,050 PE = 49,600
        #            tentative = 932.50 + 4,293.75 + 0.25*(49,600-37,950)
        #                      = 932.50 + 4,293.75 + 2,912.50 = 8,138.75
        #            no kids -> CTC=0, federal = 8,138.75
        #   TY 2018: taxable = 60,000 - 12,000 std - 0 PE = 48,000
        #            tentative = 6,499.50 (per TestPhase5Ty2018 anchor)
        #            no kids -> CTC=0, federal = 6,499.50
        #   Divergence (cut) = 8,138.75 - 6,499.50 = 1,639.25.
        out_2017 = _scalar(
            tax_db,
            "SELECT derived.f_federal_income_tax("
            "  %s, 2017::SMALLINT, 'single', 0, 0)",
            Decimal("60000"),
        )
        out_2018 = _scalar(
            tax_db,
            "SELECT derived.f_federal_income_tax("
            "  %s, 2018::SMALLINT, 'single', 0, 0)",
            Decimal("60000"),
        )
        _approx_dec(out_2017, "8138.75")
        _approx_dec(out_2018, "6499.50")
        diff = Decimal(str(out_2017)) - Decimal(str(out_2018))
        assert diff == Decimal("1639.25"), (
            f"TCJA cut at single $60K should be exactly $1,639.25, got {diff}"
        )

    def test_2017_to_2018_tcja_federal_cut_mfj_120k_2_kids(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # MFJ $120K with 2 qualifying children, no other deps:
        #   TY 2017: dependents = 2 (kids count as deps for PE)
        #            taxable = 120,000 - 12,700 std - 4,050*4 PE
        #                    = 120,000 - 12,700 - 16,200 = 91,100
        #            tentative = 1,865 + 8,587.50 + 0.25*(91,100-75,900)
        #                      = 1,865 + 8,587.50 + 3,800 = 14,252.50
        #            CTC: 2 * 1,000 = 2,000 base
        #              phaseout = max(0, 120,000-110,000) * 0.05 = 500
        #              CTC = 2,000 - 500 = 1,500
        #            federal = 14,252.50 - 1,500 = 12,752.50
        #   TY 2018: taxable = 120,000 - 24,000 std - 0 PE = 96,000
        #            tentative = 12,999 (per TestPhase5Ty2018)
        #            CTC = 2 * 2,000 = 4,000 (no phaseout: AGI < 400K MFJ)
        #            federal = 12,999 - 4,000 = 8,999
        #   Divergence (cut) = 12,752.50 - 8,999 = 3,753.50.
        out_2017 = _scalar(
            tax_db,
            "SELECT derived.f_federal_income_tax("
            "  %s, 2017::SMALLINT, 'mfj', 2, 2)",
            Decimal("120000"),
        )
        out_2018 = _scalar(
            tax_db,
            "SELECT derived.f_federal_income_tax("
            "  %s, 2018::SMALLINT, 'mfj', 2, 2)",
            Decimal("120000"),
        )
        _approx_dec(out_2017, "12752.50")
        _approx_dec(out_2018, "8999.00")
        diff = Decimal(str(out_2017)) - Decimal(str(out_2018))
        assert diff == Decimal("3753.50"), (
            f"TCJA cut at MFJ $120K w/2 kids should be exactly $3,753.50, got {diff}"
        )


# ============================================================================
# 7f. Phase-5f -- TY 2013-2016 ATRA-era anchors
# ============================================================================


class TestPhase5Ty2016:
    """TY 2016 ATRA-era. SS wage base $118,500 (FROZEN from TY 2015 due to
    zero COLA -- 2 of the only 3 years post-1975 with a zero COLA, the
    other being TY 2010 / TY 2011 also at $106,800)."""

    def test_2016_single_taxable_48000(self, tax_db: psycopg.Connection) -> None:
        # Single 2016 taxable $48,000:
        #    9,275 * 0.10 = 927.50
        #   28,375 * 0.15 = 4,256.25 (9,275 -> 37,650)
        #   10,350 * 0.25 = 2,587.50 (37,650 -> 48,000)
        #   Total         = 7,771.25
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2016::SMALLINT, 'single')",
            Decimal("48000"),
        )
        _approx_dec(out, "7771.25")

    def test_2016_fica_frozen_wage_base_with_2015(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # SS wage base $118,500, FROZEN from TY 2015 (zero COLA).
        # FICA at $200K wage TY 2016:
        #   SS = 118,500 * 0.062 = 7,347
        #   Medicare = 200,000 * 0.0145 = 2,900
        #   Add'l Medicare = (200,000 - 200,000) * 0.009 = 0
        #   Total = 10,247
        out_2016 = _scalar(
            tax_db,
            "SELECT derived.f_fica_tax(%s, 2016::SMALLINT, 'single')",
            Decimal("200000"),
        )
        out_2015 = _scalar(
            tax_db,
            "SELECT derived.f_fica_tax(%s, 2015::SMALLINT, 'single')",
            Decimal("200000"),
        )
        _approx_dec(out_2016, str(out_2015))  # FROZEN
        _approx_dec(out_2016, "10247.00")

    def test_2016_nj_eitc_30pct(self, tax_db: psycopg.Connection) -> None:
        # First year at 30% NJ EITC match (P.L. 2015 c.180).
        match_rate = _scalar(
            tax_db,
            "SELECT match_rate FROM ref.nj_state_eitc_match WHERE tax_year = 2016",
        )
        assert Decimal(str(match_rate)) == Decimal("0.30000")

    def test_2016_nj_no_veteran_exemption(self, tax_db: psycopg.Connection) -> None:
        # Veteran exemption introduced TY 2017 only (P.L. 2017 c.36).
        # TY 2016 row should NOT exist.
        out = _scalar(
            tax_db,
            "SELECT COUNT(*) FROM ref.nj_state_personal_exemption "
            "WHERE tax_year = 2016 AND exemption_kind = 'veteran'",
        )
        assert int(str(out)) == 0


class TestPhase5Ty2015:
    def test_2015_single_taxable_48000(self, tax_db: psycopg.Connection) -> None:
        # Single 2015 taxable $48,000:
        #    9,225 * 0.10 = 922.50
        #   28,225 * 0.15 = 4,233.75
        #   10,550 * 0.25 = 2,637.50
        #   Total         = 7,793.75
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2015::SMALLINT, 'single')",
            Decimal("48000"),
        )
        _approx_dec(out, "7793.75")

    def test_2015_personal_exemption_4000(self, tax_db: psycopg.Connection) -> None:
        out = _scalar(
            tax_db,
            "SELECT amount FROM ref.irs_personal_exemption WHERE tax_year = 2015",
        )
        _approx_dec(out, "4000.00")

    def test_2015_nj_eitc_25pct(self, tax_db: psycopg.Connection) -> None:
        match_rate = _scalar(
            tax_db,
            "SELECT match_rate FROM ref.nj_state_eitc_match WHERE tax_year = 2015",
        )
        assert Decimal(str(match_rate)) == Decimal("0.25000")


class TestPhase5Ty2014:
    def test_2014_single_taxable_60000(self, tax_db: psycopg.Connection) -> None:
        # Single 2014 taxable $60,000:
        #    9,075 * 0.10 = 907.50
        #   27,825 * 0.15 = 4,173.75 (9,075 -> 36,900)
        #   23,100 * 0.25 = 5,775.00 (36,900 -> 60,000)
        #   Total         = 10,856.25
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2014::SMALLINT, 'single')",
            Decimal("60000"),
        )
        _approx_dec(out, "10856.25")

    def test_2014_fica_at_117k_wage_base(self, tax_db: psycopg.Connection) -> None:
        # SS wage base $117,000.
        # At $117,000: SS = 117,000 * 0.062 = 7,254;
        #              Medicare = 117,000 * 0.0145 = 1,696.50;
        #              Total = 8,950.50.
        out = _scalar(
            tax_db,
            "SELECT derived.f_fica_tax(%s, 2014::SMALLINT, 'single')",
            Decimal("117000"),
        )
        _approx_dec(out, "8950.50")


class TestPhase5Ty2013:
    """TY 2013 = THE FIRST ATRA YEAR. ATRA (P.L. 112-240) added the 39.6%
    top bracket back. TY 2013 is also THE FIRST YEAR with the ACA s.9015
    Additional Medicare 0.9% surtax (over $200K Single / $250K MFJ)."""

    def test_2013_first_year_with_396pct_top_bracket(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Verify the substrate: TY 2013 has a 7-bracket ladder INCLUDING
        # 39.6%, while TY 2012 has only 6 brackets (top 35%).
        rate_2013_top = _scalar(
            tax_db,
            "SELECT marginal_rate FROM ref.irs_federal_brackets "
            "WHERE tax_year = 2013 AND filing_status = 'single' "
            "ORDER BY bracket_ord DESC LIMIT 1",
        )
        rate_2012_top = _scalar(
            tax_db,
            "SELECT marginal_rate FROM ref.irs_federal_brackets "
            "WHERE tax_year = 2012 AND filing_status = 'single' "
            "ORDER BY bracket_ord DESC LIMIT 1",
        )
        assert Decimal(str(rate_2013_top)) == Decimal("0.39600")
        assert Decimal(str(rate_2012_top)) == Decimal("0.35000")

    def test_2013_single_at_400k_top_bracket_floor(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Single 2013 at $400,000 (the 39.6% floor):
        # Walk via Rev. Proc. 2013-15 Table 3 printed base = $116,163.75.
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2013::SMALLINT, 'single')",
            Decimal("400000"),
        )
        _approx_dec(out, "116163.75")

    def test_2013_first_year_with_addl_medicare(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # ACA s.9015 effective TY 2013+. At $250K Single:
        #   SS = 113,700 * 0.062 = 7,049.40
        #   Medicare = 250,000 * 0.0145 = 3,625
        #   Add'l Medicare = (250,000 - 200,000) * 0.009 = 450
        #   Total = 11,124.40
        out = _scalar(
            tax_db,
            "SELECT derived.f_fica_tax(%s, 2013::SMALLINT, 'single')",
            Decimal("250000"),
        )
        _approx_dec(out, "11124.40")


# ============================================================================
# 7g. Phase-5g -- TY 2010-2012 EGTRRA/JGTRRA-era anchors
# ============================================================================


class TestPhase5Ty2012:
    """TY 2012 = THE LAST YEAR with 6-bracket pre-ATRA ladder AND the LAST
    YEAR of the payroll-tax holiday (4.2% employee SS rate)."""

    def test_2012_only_six_brackets(self, tax_db: psycopg.Connection) -> None:
        out = _scalar(
            tax_db,
            "SELECT COUNT(*) FROM ref.irs_federal_brackets "
            "WHERE tax_year = 2012 AND filing_status = 'single'",
        )
        assert int(str(out)) == 6  # 10/15/25/28/33/35; no 39.6%

    def test_2012_payroll_tax_holiday_42_pct_ss(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # P.L. 111-312 + P.L. 112-78 + P.L. 112-96: employee SS rate
        # reduced from 6.2% to 4.2% for TY 2011 and TY 2012 only.
        # At $60K wage TY 2012:
        #   SS = 60,000 * 0.042 = 2,520
        #   Medicare = 60,000 * 0.0145 = 870
        #   Add'l Medicare = 0 (not yet ACA-effective; TY 2013+)
        #   Total = 3,390
        out = _scalar(
            tax_db,
            "SELECT derived.f_fica_tax(%s, 2012::SMALLINT, 'single')",
            Decimal("60000"),
        )
        _approx_dec(out, "3390.00")

    def test_2012_no_addl_medicare_at_250k(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # TY 2012 has additional_medicare_rate = 0; even at $250K wage no
        # Add'l Medicare. SS at the $110,100 wage base cap, Medicare on
        # full wage. SS = 110,100 * 0.042 = 4,624.20; Medicare = 250,000
        # * 0.0145 = 3,625; Total = 8,249.20.
        out = _scalar(
            tax_db,
            "SELECT derived.f_fica_tax(%s, 2012::SMALLINT, 'single')",
            Decimal("250000"),
        )
        _approx_dec(out, "8249.20")


class TestPhase5Ty2011:
    """TY 2011 = FIRST YEAR of payroll-tax holiday (4.2% SS rate)."""

    def test_2011_payroll_tax_holiday_distinguishes_from_2010(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # At $60K wage TY 2011 vs TY 2010:
        #   TY 2010: SS=3,720 + Med=870 = 4,590 (full 6.2% rate)
        #   TY 2011: SS=2,520 + Med=870 = 3,390 (holiday 4.2% rate)
        # Holiday savings = 1,200 at $60K; $1,200 = 60,000 * 0.020 (the
        # 2pp SS-rate cut from P.L. 111-312).
        out_2010 = _scalar(
            tax_db,
            "SELECT derived.f_fica_tax(%s, 2010::SMALLINT, 'single')",
            Decimal("60000"),
        )
        out_2011 = _scalar(
            tax_db,
            "SELECT derived.f_fica_tax(%s, 2011::SMALLINT, 'single')",
            Decimal("60000"),
        )
        _approx_dec(out_2010, "4590.00")
        _approx_dec(out_2011, "3390.00")
        diff = Decimal(str(out_2010)) - Decimal(str(out_2011))
        assert diff == Decimal("1200.00"), (
            f"P.L. 111-312 holiday cut at $60K should be $1,200, got {diff}"
        )

    def test_2011_personal_exemption_3700(self, tax_db: psycopg.Connection) -> None:
        out = _scalar(
            tax_db,
            "SELECT amount FROM ref.irs_personal_exemption WHERE tax_year = 2011",
        )
        _approx_dec(out, "3700.00")


class TestPhase5Ty2010:
    """TY 2010 = THE EARLIEST seeded year. NJ-side has the unique
    P.L. 2010 c.27 PROPERTY-TAX-DEDUCTION SUSPENSION (cap=$0)."""

    def test_2010_single_taxable_48000(self, tax_db: psycopg.Connection) -> None:
        # Single 2010 taxable $48,000:
        #    8,375 * 0.10 = 837.50
        #   25,625 * 0.15 = 3,843.75 (8,375 -> 34,000)
        #   14,000 * 0.25 = 3,500.00 (34,000 -> 48,000)
        #   Total         = 8,181.25
        # Cross-check Rev. Proc. 2009-50 Table 3 formula row:
        #   $4,681.25 + 0.25 * (48,000 - 34,000) = 4,681.25 + 3,500 = 8,181.25.
        out = _scalar(
            tax_db,
            "SELECT derived.f_apply_federal_brackets(%s, 2010::SMALLINT, 'single')",
            Decimal("48000"),
        )
        _approx_dec(out, "8181.25")

    def test_2010_nj_property_tax_deduction_suspended(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # P.L. 2010 c.27 (Christie austerity): PTD cap $0 for TY 2010.
        # Restored to $10K TY 2011+. Substrate-level pin.
        cap = _scalar(
            tax_db,
            "SELECT deduction_cap FROM ref.nj_state_property_tax_deduction "
            "WHERE tax_year = 2010",
        )
        cap_2011 = _scalar(
            tax_db,
            "SELECT deduction_cap FROM ref.nj_state_property_tax_deduction "
            "WHERE tax_year = 2011",
        )
        _approx_dec(cap, "0.00")
        _approx_dec(cap_2011, "10000.00")

    def test_2010_nj_eitc_20pct_holiday(self, tax_db: psycopg.Connection) -> None:
        # P.L. 2010 c.27 cut NJ EITC from 25% to 20% for TY 2010-2011.
        match_rate = _scalar(
            tax_db,
            "SELECT match_rate FROM ref.nj_state_eitc_match WHERE tax_year = 2010",
        )
        assert Decimal(str(match_rate)) == Decimal("0.20000")

    def test_2010_fica_uses_106800_wage_base(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # SS wage base $106,800 (UNCHANGED FROM 2009 due to zero COLA).
        # At exactly $106,800: SS = 106,800 * 0.062 = 6,621.60;
        # Medicare = 106,800 * 0.0145 = 1,548.60; total = 8,170.20.
        out = _scalar(
            tax_db,
            "SELECT derived.f_fica_tax(%s, 2010::SMALLINT, 'single')",
            Decimal("106800"),
        )
        _approx_dec(out, "8170.20")


# ============================================================================
# 7h. Phase-5h -- TY 2021 ARPA two-stage CTC anchors
# ============================================================================


class TestPhase5Ty2021:
    """TY 2021 = THE ARPA YEAR. P.L. 117-2 s.9611 temporarily replaced the
    TCJA CTC with $3,000/$3,600-per-child fully-refundable credits with
    a TWO-STAGE phaseout (Stage 1 at $75K/$112.5K/$150K reduces to the
    pre-ARPA $2,000 floor; Stage 2 at $200K/$400K reduces $2,000 -> $0).
    These tests pin both stages of the phaseout and the headline ARPA
    expansion in absolute dollars.

    Schema requirement: migration 075 (add arpa_stage1_threshold_*
    columns) + migration 076 (rewrite f_federal_child_tax_credit to
    handle the two-stage phaseout).

    V1 LIMITATION pinned: f_federal_child_tax_credit treats every
    qualifying child at amount_6_to_17 ($3,000 in TY 2021), so the
    composite under-credits households with under-6 kids by $600 per
    young child. Tests below assume all kids age 6-17.
    """

    def test_2021_full_arpa_credit_below_stage1(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # MFJ AGI $50K (well below all thresholds), 2 kids age 6-17:
        # full $3,000 each, no phaseout -> $6,000.
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_child_tax_credit("
            "  %s, 2021::SMALLINT, 'mfj', 2)",
            Decimal("50000"),
        )
        _approx_dec(out, "6000.00")

    def test_2021_stage1_phaseout_single_80k_2_kids(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Single AGI $80K, 2 kids age 6-17:
        #   ARPA full = 2 * 3,000 = 6,000
        #   Stage1 floor (per child) = 2,000 -> total floor = 4,000
        #   Stage1 max reduction = 6,000 - 4,000 = 2,000 (the ARPA bump)
        #   Stage1 actual = (80,000 - 75,000) * 0.05 = 250
        #   Stage1 capped reduction = min(250, 2000) = 250
        #   Stage2: AGI $80K < $200K Single threshold -> 0
        #   Final = 6,000 - 250 - 0 = 5,750
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_child_tax_credit("
            "  %s, 2021::SMALLINT, 'single', 2)",
            Decimal("80000"),
        )
        _approx_dec(out, "5750.00")

    def test_2021_stage1_phaseout_hoh_use_dedicated_threshold(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # HOH AGI $130K, 2 kids age 6-17:
        # HOH Stage1 threshold = $112,500 (NOT $75K Single, NOT $150K MFJ).
        #   Stage1 actual = (130,000 - 112,500) * 0.05 = 875
        #   Stage1 capped at 2,000 = 875
        #   Stage2: AGI $130K < $200K HOH threshold -> 0
        #   Final = 6,000 - 875 = 5,125
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_child_tax_credit("
            "  %s, 2021::SMALLINT, 'hoh', 2)",
            Decimal("130000"),
        )
        _approx_dec(out, "5125.00")

    def test_2021_stage1_complete_at_2k_floor(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # MFJ AGI $200K, 2 kids age 6-17:
        # Stage1 actual = (200,000 - 150,000) * 0.05 = 2,500
        # Stage1 capped at 2,000 (the ARPA-bump cap) -> Stage1 = 2,000
        # Stage2: AGI $200K < $400K MFJ threshold -> 0
        # Final = 6,000 - 2,000 - 0 = 4,000 (= pre-ARPA $2,000 floor x 2 kids)
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_child_tax_credit("
            "  %s, 2021::SMALLINT, 'mfj', 2)",
            Decimal("200000"),
        )
        _approx_dec(out, "4000.00")

    def test_2021_two_stage_phaseout_single_250k(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Single AGI $250K, 2 kids age 6-17:
        # Stage1: (250,000 - 75,000) * 0.05 = 8,750 capped at 2,000 = 2,000
        # Stage2: (250,000 - 200,000) * 0.05 = 2,500
        # Final = 6,000 - 2,000 - 2,500 = 1,500
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_child_tax_credit("
            "  %s, 2021::SMALLINT, 'single', 2)",
            Decimal("250000"),
        )
        _approx_dec(out, "1500.00")

    def test_2021_two_stage_phaseout_completely_zero(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Single AGI $500K, 2 kids:
        # Stage1: 2,000 (capped); Stage2: (500K-200K) * 0.05 = 15,000
        # Final = max(0, 6,000 - 2,000 - 15,000) = 0
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_child_tax_credit("
            "  %s, 2021::SMALLINT, 'single', 2)",
            Decimal("500000"),
        )
        _approx_dec(out, "0.00")

    def test_2021_arpa_full_amount_under_6_in_seed(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # The $3,600 (under-6) and $3,000 (6-17) age-tier values must be
        # encoded in the seed even though the V1 function ignores the
        # under-6 split. This is the SUBSTRATE pin: future v2 functions
        # must be able to read $3,600 / $3,000 / $2,000-floor.
        with tax_db.cursor() as cur:
            cur.execute(
                "SELECT amount_under_6, amount_6_to_17, refundable_max_per_child "
                "FROM ref.irs_child_tax_credit WHERE tax_year = 2021",
            )
            row = cur.fetchone()
        assert row is not None
        _approx_dec(row[0], "3600.00")
        _approx_dec(row[1], "3000.00")
        _approx_dec(row[2], "3600.00")

    def test_2021_arpa_thresholds_in_seed(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Stage1 thresholds: Single $75K, HOH $112.5K, MFJ $150K.
        # Stage2 thresholds (existing columns): Single $200K, MFJ $400K.
        with tax_db.cursor() as cur:
            cur.execute(
                "SELECT arpa_stage1_threshold_single, arpa_stage1_threshold_hoh, "
                "       arpa_stage1_threshold_mfj, "
                "       phaseout_threshold_single, phaseout_threshold_mfj "
                "FROM ref.irs_child_tax_credit WHERE tax_year = 2021",
            )
            row = cur.fetchone()
        assert row is not None
        _approx_dec(row[0], "75000.00")
        _approx_dec(row[1], "112500.00")
        _approx_dec(row[2], "150000.00")
        _approx_dec(row[3], "200000.00")
        _approx_dec(row[4], "400000.00")

    def test_2021_non_arpa_years_have_null_stage1(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # ARPA columns must be NULL for all OTHER seeded years -- this is
        # what makes the f_federal_child_tax_credit branch on
        # arpa_stage1_threshold_single IS NULL pivot to the standard
        # single-stage phaseout for non-ARPA years.
        with tax_db.cursor() as cur:
            cur.execute(
                "SELECT tax_year, arpa_stage1_threshold_single "
                "FROM ref.irs_child_tax_credit "
                "WHERE tax_year != 2021 ORDER BY tax_year",
            )
            rows = cur.fetchall()
        # All non-2021 rows must have NULL Stage1 single threshold.
        for row in rows:
            assert row[1] is None, (
                f"TY {row[0]} should have NULL arpa_stage1_threshold_single "
                f"(only TY 2021 is ARPA), got {row[1]}"
            )


# ============================================================================
# 8. Substrate-honesty contract
# ============================================================================


class TestSubstrateHonesty:
    """Every function returns NULL when the requested year is not seeded.

    This is the verifiable-data invariant: we never silently substitute
    an adjacent year. Calling code MUST surface "data unavailable" to
    the user and never compute a wrong-but-plausible number.
    """

    # PHASE 5 COMPLETE: all years TY 2010 through TY 2024 are now seeded.
    # The ONLY unseeded-year sentinels remaining are:
    #   - 2009: pre-Phase-5 baseline; predates Rev. Proc. 2009-50 substrate
    #   - 2025: future year; not yet seeded (Rev. Proc. 2024-40 published
    #     but TY 2025 substrate work is post-Phase-5 scope per VISION 2026
    @pytest.mark.parametrize("year", [2009, 2025])
    def test_federal_unseeded_year_returns_null(
        self, tax_db: psycopg.Connection, year: int,
    ) -> None:
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_income_tax(%s, %s::SMALLINT, 'single', 0, 0)",
            Decimal("60000"),
            year,
        )
        assert out is None, f"expected NULL for unseeded TY {year}, got {out}"

    @pytest.mark.parametrize("year", [2009, 2025])
    def test_nj_unseeded_year_returns_null(
        self, tax_db: psycopg.Connection, year: int,
    ) -> None:
        out = _scalar(
            tax_db,
            "SELECT derived.f_nj_state_income_tax(%s, %s::SMALLINT, 'single', 0, 0)",
            Decimal("60000"),
            year,
        )
        assert out is None

    @pytest.mark.parametrize("year", [2009, 2025])
    def test_fica_unseeded_year_returns_null(
        self, tax_db: psycopg.Connection, year: int,
    ) -> None:
        out = _scalar(
            tax_db,
            "SELECT derived.f_fica_tax(%s, %s::SMALLINT, 'single')",
            Decimal("60000"),
            year,
        )
        assert out is None

    def test_null_gross_income_returns_null(
        self, tax_db: psycopg.Connection,
    ) -> None:
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_income_tax("
            "  NULL::NUMERIC, 2024::SMALLINT, 'single', 0, 0)",
        )
        assert out is None

    def test_dropping_std_deduction_makes_taxable_null(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Surgically delete the seeded std-deduction row for (2024, single).
        # Confirms the substrate-honesty path: missing ref data -> NULL,
        # NOT silent fallback to "no std deduction" (which would give a
        # wrong-but-plausible $7,216 instead of $5,216).
        with tax_db.cursor() as cur:
            cur.execute(
                "DELETE FROM ref.irs_standard_deduction "
                "WHERE tax_year = 2024 AND filing_status = 'single'",
            )
        out = _scalar(
            tax_db,
            "SELECT derived.f_federal_taxable_income("
            "  60000::NUMERIC, 2024::SMALLINT, 'single', 0)",
        )
        assert out is None

    def test_composite_total_null_when_any_component_null(
        self, tax_db: psycopg.Connection,
    ) -> None:
        with tax_db.cursor() as cur:
            cur.execute(
                "SELECT total_tax FROM derived.f_household_taxes("
                "  60000::NUMERIC, 60000::NUMERIC, 2025::SMALLINT,"
                "  'single', 0, 0, 0::NUMERIC)",
            )
            row = cur.fetchone()
        assert row is not None and row[0] is None


# ============================================================================
# 9. Coverage diagnostic views (asset-check substrate)
# ============================================================================


class TestCoverageViews:
    def test_federal_brackets_coverage_has_zero_floor_for_seeded_years(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Every (year, status) tuple that has any rows must include a
        # bracket starting at $0 (otherwise income < lowest_floor is
        # ambiguous). The view is the asset-check substrate.
        with tax_db.cursor() as cur:
            cur.execute(
                "SELECT tax_year, filing_status, has_zero_floor, bracket_count "
                "FROM ref.v_irs_federal_brackets_coverage "
                "WHERE NOT has_zero_floor OR bracket_count = 0",
            )
            broken = cur.fetchall()
        assert broken == [], f"federal coverage drift: {broken}"

    def test_nj_brackets_coverage_has_zero_floor_for_seeded_years(
        self, tax_db: psycopg.Connection,
    ) -> None:
        with tax_db.cursor() as cur:
            cur.execute(
                "SELECT tax_year, filing_status, has_zero_floor, bracket_count "
                "FROM ref.v_nj_state_brackets_coverage "
                "WHERE NOT has_zero_floor OR bracket_count = 0",
            )
            broken = cur.fetchall()
        assert broken == [], f"NJ coverage drift: {broken}"

    def test_all_5_filing_statuses_present_for_seeded_years(
        self, tax_db: psycopg.Connection,
    ) -> None:
        # Each seeded year must have ALL 5 filing statuses for both
        # federal and NJ. A partial seed (e.g. only Single + MFJ)
        # would silently break HOH/QSS computations.
        with tax_db.cursor() as cur:
            cur.execute(
                "SELECT tax_year, count(DISTINCT filing_status) "
                "FROM ref.irs_federal_brackets "
                "GROUP BY tax_year "
                "HAVING count(DISTINCT filing_status) <> 5",
            )
            assert cur.fetchall() == []
            cur.execute(
                "SELECT tax_year, count(DISTINCT filing_status) "
                "FROM ref.nj_state_brackets "
                "GROUP BY tax_year "
                "HAVING count(DISTINCT filing_status) <> 5",
            )
            assert cur.fetchall() == []
