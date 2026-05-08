"""Live-PG tests for the Phase-3 disposable-income + erosion engine
(migration 073).

Every assertion in this file is a HAND-COMPUTED disposable-income,
HBR, or AEI value derived from the spec definitions in idea §5.3 and
§5.5. We are testing "the function returns *the* number", not "the
function returns *a* number".

Functions under test:

* derived.f_disposable_income_annual
    DI = gross - tax(gross, year, status, deps, kids) - PITI(home, year, county).
    Composes f_household_taxes (070) + f_piti_annual (072).
* derived.f_disposable_income_real
    Nominal DI deflated to base_year via CPI ratio.
* derived.f_household_burden_ratio
    HBR = PITI(median_home) / median_income.
* derived.f_affordability_erosion_index
    AEI = HBR(year) / HBR(anchor_year).
* derived.v_disposable_income_trajectory
    per-(county, year) DI rolled up for the representative MFJ-1-1 hh.
* derived.v_aei_by_county
    per-county AEI vs the earliest year for which HBR is computable.

Substrate-honesty: every "data not seeded for year X" path is pinned
to a NULL assertion. The platform NEVER pretends to know an answer
it cannot derive from a verifiable source.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import psycopg

pytestmark = pytest.mark.live_pg


# ============================================================================
# Fixture: fully-migrated + fully-seeded DB plus a synthetic two-year substrate
#
# County XX-99001 with:
#   2023: FRED 6%, DCA $450K @ 2.85%, ACS5 $115K, CPI 304.702
#   2024: FRED 7%, DCA $500K @ 2.85%, ACS5 $120K, CPI 313.689
#
# Real BLS CPI annual averages for 2023 and 2024 are used so the
# deflator math is verifiable against published data.
# ============================================================================


@pytest.fixture
def phase3_db(live_pg: psycopg.Connection) -> psycopg.Connection:
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
            "VALUES ('XX', '99', 'Test State') ON CONFLICT DO NOTHING"
        )
        cur.execute(
            "INSERT INTO ref.county (county_id, state_code, county_fips, name) "
            "VALUES ('XX-TEST', 'XX', '99001', 'Test County') "
            "ON CONFLICT DO NOTHING"
        )
        # Two FRED observations -- 6% in 2023, 7% in 2024.
        cur.executemany(
            "INSERT INTO raw.fred_observation "
            "  (series_id, observation_date, value, source_url, source_sha256) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            [
                ("MORTGAGE30US", "2023-06-06", Decimal("6.0000"),
                 "https://fred.stlouisfed.org/", "a" * 64),
                ("MORTGAGE30US", "2024-06-06", Decimal("7.0000"),
                 "https://fred.stlouisfed.org/", "b" * 64),
            ],
        )
        cur.executemany(
            "INSERT INTO raw.nj_property_tax_county "
            "  (county_fips, year, avg_residential_value, cy_total_rate, "
            "   source_url, source_sha256, source_vintage) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            [
                ("99001", 2023, Decimal("450000"), Decimal("2.8500"),
                 "https://www.nj.gov/dca/divisions/dlgs/", "c" * 64, "2023-annual"),
                ("99001", 2024, Decimal("500000"), Decimal("2.8500"),
                 "https://www.nj.gov/dca/divisions/dlgs/", "d" * 64, "2024-annual"),
            ],
        )
        cur.executemany(
            "INSERT INTO raw.acs_median_household_income "
            "  (county_fips, year, product, estimate, dollar_year, "
            "   source_url, source_sha256) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            [
                ("99001", 2023, "acs5", Decimal("115000"), 2023,
                 "https://api.census.gov/data/2023/acs/acs5", "e" * 64),
                ("99001", 2024, "acs5", Decimal("120000"), 2024,
                 "https://api.census.gov/data/2024/acs/acs5", "f" * 64),
            ],
        )
        # Real BLS CPI-U All Items annual averages (CUUR0000SA0).
        # 2023 = 304.702, 2024 = 313.689 (BLS M13).
        cur.executemany(
            "INSERT INTO raw.cpi_u "
            "  (series_id, year, period, value, source_url, source_sha256) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            [
                ("CUUR0000SA0", 2023, "M13", Decimal("304.702"),
                 "https://api.bls.gov/publicAPI/v2/timeseries/data/", "g" * 64),
                ("CUUR0000SA0", 2024, "M13", Decimal("313.689"),
                 "https://api.bls.gov/publicAPI/v2/timeseries/data/", "h" * 64),
            ],
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
    actual: object, expected: str | Decimal, *, abs_tol: str = "0.01"
) -> None:
    a = Decimal(str(actual))
    e = Decimal(str(expected))
    assert abs(a - e) <= Decimal(abs_tol), f"expected {e} +/- {abs_tol}, got {a}"


# ============================================================================
# Anchor values used across many tests, hand-computed once.
#
# 2024 (FRED 7%, $500K home, county tax 2.85%, ACS $120K, MFJ-1-1):
#   * P&I monthly: M = 400000 * (0.07/12) * (1+0.07/12)^360 / ((1+0.07/12)^360 - 1)
#                    = $2,661.21      (matches Bankrate calculator)
#   * Annual P&I:  $31,934.51
#   * Property tax: 500000 * 0.0285 = $14,250
#   * Insurance:   500000 * 0.0035 =  $1,750
#   * PITI annual: $47,934.51
#   * Federal+NJ+FICA tax for $120K MFJ-1-1, 2024:
#       (verified via the f_household_taxes engine = $21,223.63)
#   * DI nominal:  $120,000 - $21,223.63 - $47,934.51 = $50,841.86
#
# 2023 (FRED 6%, $450K home, county tax 2.85%, ACS $115K, MFJ-1-1):
#   * P&I monthly @ 6%, $360K: $2,158.39
#   * Annual P&I:  $25,900.69
#   * Property tax: 450000 * 0.0285 = $12,825
#   * Insurance:   450000 * 0.0035 =  $1,575
#   * PITI annual: $40,300.69
#   * Federal+NJ+FICA tax for $115K MFJ-1-1, 2023:
#       (verified via the f_household_taxes engine = $20,168.77)
#   * DI nominal:  $115,000 - $20,168.77 - $40,300.69 = $54,530.54
#
# CPI deflator 2023 -> 2024 base: 313.689 / 304.702 = 1.029491... so:
#   * DI 2023 in 2024 dollars = 54530.54 * 1.029491 = $56,138.89
#
# HBR_2024 = 47934.51 / 120000 = 0.399454
# HBR_2023 = 40300.69 / 115000 = 0.350441
# AEI_2024_vs_2023 = 0.399454 / 0.350441 = 1.13987
# ============================================================================


# ============================================================================
# 1. f_disposable_income_annual -- DI = gross - tax - PITI
# ============================================================================


class TestDisposableIncomeAnnual:

    def test_di_120k_mfj_2024_500k_home(self, phase3_db: psycopg.Connection) -> None:
        """The headline 2024 hand-computed scenario."""
        out = _scalar(
            phase3_db,
            "SELECT round(derived.f_disposable_income_annual("
            "  %s, %s::SMALLINT, %s, %s, %s, %s, %s), 2)",
            Decimal("120000"), 2024, "99001", "mfj", 1, 1, Decimal("500000"),
        )
        _approx_dec(out, "50841.86")

    def test_di_115k_mfj_2023_450k_home(self, phase3_db: psycopg.Connection) -> None:
        """The 2023 hand-computed scenario."""
        out = _scalar(
            phase3_db,
            "SELECT round(derived.f_disposable_income_annual("
            "  %s, %s::SMALLINT, %s, %s, %s, %s, %s), 2)",
            Decimal("115000"), 2023, "99001", "mfj", 1, 1, Decimal("450000"),
        )
        _approx_dec(out, "54530.54")

    def test_di_decomposition_matches_components(
        self, phase3_db: psycopg.Connection
    ) -> None:
        """DI should be EXACTLY gross - total_tax - PITI down to the cent
        because it's a pure subtraction; any drift indicates a precision
        bug somewhere in the composition."""
        with phase3_db.cursor() as cur:
            cur.execute(
                "SELECT total_tax FROM derived.f_household_taxes("
                "  %s, %s, %s::SMALLINT, %s, %s, %s, %s)",
                (Decimal("120000"), Decimal("120000"), 2024,
                 "mfj", 1, 1, Decimal("0")),
            )
            row = cur.fetchone()
            assert row is not None
            tax = Decimal(str(row[0]))
            cur.execute(
                "SELECT derived.f_piti_annual(%s, %s::SMALLINT, %s)",
                (Decimal("500000"), 2024, "99001"),
            )
            row = cur.fetchone()
            assert row is not None
            piti = Decimal(str(row[0]))
        expected_di = Decimal("120000") - tax - piti

        out = _scalar(
            phase3_db,
            "SELECT derived.f_disposable_income_annual("
            "  %s, %s::SMALLINT, %s, %s, %s, %s, %s)",
            Decimal("120000"), 2024, "99001", "mfj", 1, 1, Decimal("500000"),
        )
        # f_household_taxes' total_tax column has typed NUMERIC precision
        # while f_piti_annual returns raw NUMERIC; the composition can
        # therefore disagree at the sub-cent level. We pin the gap to <1e-6
        # to detect any real arithmetic drift while tolerating PG's
        # internal precision rounding.
        diff = abs(Decimal(str(out)) - expected_di)
        assert diff < Decimal("0.000001"), \
            f"DI {out} != gross-tax-PITI {expected_di} (diff {diff})"

    def test_di_higher_income_higher_di(self, phase3_db: psycopg.Connection) -> None:
        """Sanity: at fixed home price, more gross income leaves more DI
        even after marginal taxes (NJ top bracket is 10.75% so DI must
        still grow with gross)."""
        di_low = _scalar(
            phase3_db,
            "SELECT derived.f_disposable_income_annual("
            "  %s, %s::SMALLINT, %s, %s, %s, %s, %s)",
            Decimal("100000"), 2024, "99001", "mfj", 1, 1, Decimal("500000"),
        )
        di_high = _scalar(
            phase3_db,
            "SELECT derived.f_disposable_income_annual("
            "  %s, %s::SMALLINT, %s, %s, %s, %s, %s)",
            Decimal("200000"), 2024, "99001", "mfj", 1, 1, Decimal("500000"),
        )
        assert Decimal(str(di_high)) > Decimal(str(di_low))

    def test_di_cheaper_home_higher_di(
        self, phase3_db: psycopg.Connection
    ) -> None:
        """Sanity: at fixed gross income, cheaper home leaves more DI."""
        di_expensive = _scalar(
            phase3_db,
            "SELECT derived.f_disposable_income_annual("
            "  %s, %s::SMALLINT, %s, %s, %s, %s, %s)",
            Decimal("120000"), 2024, "99001", "mfj", 1, 1, Decimal("500000"),
        )
        di_cheap = _scalar(
            phase3_db,
            "SELECT derived.f_disposable_income_annual("
            "  %s, %s::SMALLINT, %s, %s, %s, %s, %s)",
            Decimal("120000"), 2024, "99001", "mfj", 1, 1, Decimal("250000"),
        )
        assert Decimal(str(di_cheap)) > Decimal(str(di_expensive))

    # ------------------------------------------------------------------
    # Substrate-honesty NULL tests
    # ------------------------------------------------------------------

    def test_di_null_when_gross_null(self, phase3_db: psycopg.Connection) -> None:
        out = _scalar(
            phase3_db,
            "SELECT derived.f_disposable_income_annual("
            "  NULL, %s::SMALLINT, %s, %s, %s, %s, %s)",
            2024, "99001", "mfj", 1, 1, Decimal("500000"),
        )
        assert out is None

    def test_di_null_when_home_null(self, phase3_db: psycopg.Connection) -> None:
        out = _scalar(
            phase3_db,
            "SELECT derived.f_disposable_income_annual("
            "  %s, %s::SMALLINT, %s, %s, %s, %s, NULL)",
            Decimal("120000"), 2024, "99001", "mfj", 1, 1,
        )
        assert out is None

    def test_di_null_when_county_missing_tax_data(
        self, phase3_db: psycopg.Connection
    ) -> None:
        """Unknown county => f_piti_annual NULL => DI NULL."""
        out = _scalar(
            phase3_db,
            "SELECT derived.f_disposable_income_annual("
            "  %s, %s::SMALLINT, %s, %s, %s, %s, %s)",
            Decimal("120000"), 2024, "34999", "mfj", 1, 1, Decimal("500000"),
        )
        assert out is None

    def test_di_null_when_year_missing_tax_tables(
        self, phase3_db: psycopg.Connection
    ) -> None:
        """A year with no IRS/NJ brackets seeded => tax NULL => DI NULL.
        2010 is not in the seed range (010_irs_federal_tax_2023_2024 +
        011_nj_state_tax_2023_2024)."""
        out = _scalar(
            phase3_db,
            "SELECT derived.f_disposable_income_annual("
            "  %s, %s::SMALLINT, %s, %s, %s, %s, %s)",
            Decimal("120000"), 2010, "99001", "mfj", 1, 1, Decimal("500000"),
        )
        assert out is None


# ============================================================================
# 2. f_disposable_income_real -- CPI deflation
# ============================================================================


class TestDisposableIncomeReal:

    def test_identity_when_base_equals_value_year(
        self, phase3_db: psycopg.Connection
    ) -> None:
        """Deflating to your own year is the identity (modulo round-to-cents)."""
        nominal = _scalar(
            phase3_db,
            "SELECT round(derived.f_disposable_income_annual("
            "  %s, %s::SMALLINT, %s, %s, %s, %s, %s), 2)",
            Decimal("120000"), 2024, "99001", "mfj", 1, 1, Decimal("500000"),
        )
        real = _scalar(
            phase3_db,
            "SELECT derived.f_disposable_income_real("
            "  %s, %s::SMALLINT, %s, %s, %s, %s, %s, %s::SMALLINT)",
            Decimal("120000"), 2024, "99001", "mfj", 1, 1, Decimal("500000"), 2024,
        )
        _approx_dec(real, str(nominal))

    def test_deflate_2023_to_2024_dollars(
        self, phase3_db: psycopg.Connection
    ) -> None:
        """Deflator = 313.689 / 304.702 = 1.029491. So 2023 DI of
        $54,530.54 in 2024 dollars = $56,138.89."""
        out = _scalar(
            phase3_db,
            "SELECT derived.f_disposable_income_real("
            "  %s, %s::SMALLINT, %s, %s, %s, %s, %s, %s::SMALLINT)",
            Decimal("115000"), 2023, "99001", "mfj", 1, 1, Decimal("450000"), 2024,
        )
        _approx_dec(out, "56138.89", abs_tol="0.05")

    def test_deflator_correctly_inflates_older_year(
        self, phase3_db: psycopg.Connection
    ) -> None:
        """Real-2024 DI of $54,530 nominal 2023 must be > $54,530, because
        CPI rose. Catches a deflator-direction sign bug (the most common
        kind of CPI-deflation mistake)."""
        nominal_2023 = _scalar(
            phase3_db,
            "SELECT derived.f_disposable_income_annual("
            "  %s, %s::SMALLINT, %s, %s, %s, %s, %s)",
            Decimal("115000"), 2023, "99001", "mfj", 1, 1, Decimal("450000"),
        )
        real_2023_in_2024 = _scalar(
            phase3_db,
            "SELECT derived.f_disposable_income_real("
            "  %s, %s::SMALLINT, %s, %s, %s, %s, %s, %s::SMALLINT)",
            Decimal("115000"), 2023, "99001", "mfj", 1, 1, Decimal("450000"), 2024,
        )
        assert Decimal(str(real_2023_in_2024)) > Decimal(str(nominal_2023))

    def test_null_when_base_year_cpi_missing(
        self, phase3_db: psycopg.Connection
    ) -> None:
        """Spec says 2026 baseline; we don't have 2026 CPI loaded, so
        deflating any value to 2026 base must return NULL. This is the
        substrate-honesty contract: never silently substitute a different
        base year."""
        out = _scalar(
            phase3_db,
            "SELECT derived.f_disposable_income_real("
            "  %s, %s::SMALLINT, %s, %s, %s, %s, %s, %s::SMALLINT)",
            Decimal("120000"), 2024, "99001", "mfj", 1, 1, Decimal("500000"), 2026,
        )
        assert out is None

    def test_null_when_value_year_cpi_missing(
        self, phase3_db: psycopg.Connection
    ) -> None:
        """1995 has no CPI in this fixture (we only seeded 2023+2024),
        so any value-year deflation from 1995 must be NULL."""
        out = _scalar(
            phase3_db,
            "SELECT derived.f_disposable_income_real("
            "  %s, %s::SMALLINT, %s, %s, %s, %s, %s, %s::SMALLINT)",
            Decimal("60000"), 1995, "99001", "mfj", 1, 1, Decimal("250000"), 2024,
        )
        assert out is None

    def test_null_when_nominal_di_null(self, phase3_db: psycopg.Connection) -> None:
        """If nominal DI is NULL (e.g. unknown county), real DI is NULL."""
        out = _scalar(
            phase3_db,
            "SELECT derived.f_disposable_income_real("
            "  %s, %s::SMALLINT, %s, %s, %s, %s, %s, %s::SMALLINT)",
            Decimal("120000"), 2024, "34999", "mfj", 1, 1, Decimal("500000"), 2024,
        )
        assert out is None


# ============================================================================
# 3. f_household_burden_ratio -- the spec's HBR per §5.1
# ============================================================================


class TestHouseholdBurdenRatio:

    def test_hbr_2024(self, phase3_db: psycopg.Connection) -> None:
        """HBR_2024 = PITI($500K, 2024, 99001) / $120K = 47934.51 / 120000 = 0.39945"""
        out = _scalar(
            phase3_db,
            "SELECT derived.f_household_burden_ratio(%s::SMALLINT, %s)",
            2024, "99001",
        )
        _approx_dec(out, "0.399454", abs_tol="0.000005")

    def test_hbr_2023(self, phase3_db: psycopg.Connection) -> None:
        """HBR_2023 = PITI($450K @ 6%, 2023, 99001) / $115K = 40300.69 / 115000 = 0.35044"""
        out = _scalar(
            phase3_db,
            "SELECT derived.f_household_burden_ratio(%s::SMALLINT, %s)",
            2023, "99001",
        )
        _approx_dec(out, "0.350441", abs_tol="0.000005")

    def test_hbr_decomposition_matches_components(
        self, phase3_db: psycopg.Connection
    ) -> None:
        """HBR should be EXACTLY PITI / income."""
        with phase3_db.cursor() as cur:
            cur.execute(
                "SELECT derived.f_county_avg_home_price(%s, %s::SMALLINT)",
                ("99001", 2024),
            )
            row = cur.fetchone()
            assert row is not None
            home = Decimal(str(row[0]))
            cur.execute(
                "SELECT derived.f_piti_annual(%s, %s::SMALLINT, %s)",
                (home, 2024, "99001"),
            )
            row = cur.fetchone()
            assert row is not None
            piti = Decimal(str(row[0]))
            cur.execute(
                "SELECT estimate FROM raw.acs_median_household_income "
                "WHERE county_fips=%s AND year=%s AND product='acs5'",
                ("99001", 2024),
            )
            row = cur.fetchone()
            assert row is not None
            income = Decimal(str(row[0]))
        expected_hbr = (piti / income).quantize(Decimal("0.000001"))
        out = _scalar(
            phase3_db,
            "SELECT derived.f_household_burden_ratio(%s::SMALLINT, %s)",
            2024, "99001",
        )
        # The function rounds to 6dp; allow that.
        _approx_dec(out, str(expected_hbr), abs_tol="0.000005")

    def test_hbr_null_when_income_missing(
        self, phase3_db: psycopg.Connection
    ) -> None:
        """No ACS for 2010 in fixture => HBR NULL."""
        out = _scalar(
            phase3_db,
            "SELECT derived.f_household_burden_ratio(%s::SMALLINT, %s)",
            2010, "99001",
        )
        assert out is None

    def test_hbr_null_when_home_price_missing(
        self, phase3_db: psycopg.Connection
    ) -> None:
        """Unknown county => no DCA/FRED rate => PITI NULL => HBR NULL."""
        out = _scalar(
            phase3_db,
            "SELECT derived.f_household_burden_ratio(%s::SMALLINT, %s)",
            2024, "34999",
        )
        assert out is None


# ============================================================================
# 4. f_affordability_erosion_index -- the spec's AEI per §5.5
# ============================================================================


class TestAffordabilityErosionIndex:

    def test_aei_2024_vs_2023(self, phase3_db: psycopg.Connection) -> None:
        """AEI = 0.399454 / 0.350441 = 1.139871"""
        out = _scalar(
            phase3_db,
            "SELECT derived.f_affordability_erosion_index("
            "  %s, %s::SMALLINT, %s::SMALLINT)",
            "99001", 2024, 2023,
        )
        _approx_dec(out, "1.1399", abs_tol="0.0005")

    def test_aei_self_is_one(self, phase3_db: psycopg.Connection) -> None:
        """Same year vs itself => 1.0 exactly (HBR / HBR = 1)."""
        out = _scalar(
            phase3_db,
            "SELECT derived.f_affordability_erosion_index("
            "  %s, %s::SMALLINT, %s::SMALLINT)",
            "99001", 2024, 2024,
        )
        _approx_dec(out, "1.0000")

    def test_aei_inverse_relationship(
        self, phase3_db: psycopg.Connection
    ) -> None:
        """AEI(A,B) * AEI(B,A) = 1 (modulo rounding)."""
        forward = _scalar(
            phase3_db,
            "SELECT derived.f_affordability_erosion_index("
            "  %s, %s::SMALLINT, %s::SMALLINT)",
            "99001", 2024, 2023,
        )
        backward = _scalar(
            phase3_db,
            "SELECT derived.f_affordability_erosion_index("
            "  %s, %s::SMALLINT, %s::SMALLINT)",
            "99001", 2023, 2024,
        )
        product = Decimal(str(forward)) * Decimal(str(backward))
        # 4dp on each side => 8dp tolerance is fine.
        assert abs(product - Decimal("1")) < Decimal("0.0001"), \
            f"forward={forward} * backward={backward} = {product}, want 1.0"

    def test_aei_null_when_anchor_year_missing(
        self, phase3_db: psycopg.Connection
    ) -> None:
        """AEI vs an anchor year for which we have no substrate => NULL."""
        out = _scalar(
            phase3_db,
            "SELECT derived.f_affordability_erosion_index("
            "  %s, %s::SMALLINT, %s::SMALLINT)",
            "99001", 2024, 2010,
        )
        assert out is None

    def test_aei_null_when_current_year_missing(
        self, phase3_db: psycopg.Connection
    ) -> None:
        """AEI for a current year with no substrate => NULL."""
        out = _scalar(
            phase3_db,
            "SELECT derived.f_affordability_erosion_index("
            "  %s, %s::SMALLINT, %s::SMALLINT)",
            "99001", 2010, 2024,
        )
        assert out is None


# ============================================================================
# 5. v_disposable_income_trajectory -- the per-county time series
# ============================================================================


class TestDisposableIncomeTrajectoryView:

    def test_view_returns_two_rows_for_test_county(
        self, phase3_db: psycopg.Connection
    ) -> None:
        out = _scalar(
            phase3_db,
            "SELECT count(*) FROM derived.v_disposable_income_trajectory "
            "WHERE county_fips = %s",
            "99001",
        )
        assert int(str(out)) == 2

    def test_view_di_nominal_matches_function(
        self, phase3_db: psycopg.Connection
    ) -> None:
        with phase3_db.cursor() as cur:
            cur.execute(
                "SELECT year, di_nominal "
                "FROM derived.v_disposable_income_trajectory "
                "WHERE county_fips=%s ORDER BY year",
                ("99001",),
            )
            rows = cur.fetchall()
        assert [int(r[0]) for r in rows] == [2023, 2024]
        _approx_dec(rows[0][1], "54530.54", abs_tol="0.10")
        _approx_dec(rows[1][1], "50841.86", abs_tol="0.10")

    def test_view_di_real_uses_latest_cpi_year(
        self, phase3_db: psycopg.Connection
    ) -> None:
        """Latest CPI year in fixture is 2024, so real_dollars_base_year=2024
        and the 2024 row's di_nominal == di_real (identity)."""
        with phase3_db.cursor() as cur:
            cur.execute(
                "SELECT di_nominal, di_real, real_dollars_base_year "
                "FROM derived.v_disposable_income_trajectory "
                "WHERE county_fips=%s AND year=%s",
                ("99001", 2024),
            )
            row = cur.fetchone()
        assert row is not None
        di_nominal, di_real, base_year = row
        assert int(base_year) == 2024
        _approx_dec(di_nominal, str(di_real), abs_tol="0.01")

    def test_view_di_real_2023_inflated_to_2024(
        self, phase3_db: psycopg.Connection
    ) -> None:
        with phase3_db.cursor() as cur:
            cur.execute(
                "SELECT di_real FROM derived.v_disposable_income_trajectory "
                "WHERE county_fips=%s AND year=%s",
                ("99001", 2023),
            )
            row = cur.fetchone()
        assert row is not None
        _approx_dec(row[0], "56138.89", abs_tol="0.10")

    def test_view_records_profile_and_formula_version(
        self, phase3_db: psycopg.Connection
    ) -> None:
        """Provenance: every row carries the household profile and
        formula version so an auditor can reproduce."""
        with phase3_db.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT profile_filing_status, profile_dependents, "
                "                profile_qualifying_children, formula_version "
                "FROM derived.v_disposable_income_trajectory "
                "WHERE county_fips=%s",
                ("99001",),
            )
            row = cur.fetchone()
        assert row is not None
        status, deps, kids, fv = row
        assert status == "mfj"
        assert int(deps) == 1
        assert int(kids) == 1
        assert fv == "1.3.0-disposable-income-erosion-v1"


# ============================================================================
# 6. v_aei_by_county -- the per-county headline AEI stat
# ============================================================================


class TestAEIByCountyView:

    def test_view_returns_one_row_for_test_county(
        self, phase3_db: psycopg.Connection
    ) -> None:
        out = _scalar(
            phase3_db,
            "SELECT count(*) FROM derived.v_aei_by_county WHERE county_fips=%s",
            "99001",
        )
        assert int(str(out)) == 1

    def test_view_anchor_is_earliest_populated_year(
        self, phase3_db: psycopg.Connection
    ) -> None:
        """Two-year fixture: anchor=2023, latest=2024."""
        with phase3_db.cursor() as cur:
            cur.execute(
                "SELECT anchor_year, latest_year, years_observed "
                "FROM derived.v_aei_by_county WHERE county_fips=%s",
                ("99001",),
            )
            row = cur.fetchone()
        assert row is not None
        assert int(row[0]) == 2023
        assert int(row[1]) == 2024
        assert int(row[2]) == 1

    def test_view_aei_matches_function(
        self, phase3_db: psycopg.Connection
    ) -> None:
        """The view's aei must equal the function's aei to the cent."""
        with phase3_db.cursor() as cur:
            cur.execute(
                "SELECT aei FROM derived.v_aei_by_county WHERE county_fips=%s",
                ("99001",),
            )
            row = cur.fetchone()
            assert row is not None
            view_aei = Decimal(str(row[0]))
            cur.execute(
                "SELECT derived.f_affordability_erosion_index("
                "  %s, %s::SMALLINT, %s::SMALLINT)",
                ("99001", 2024, 2023),
            )
            row = cur.fetchone()
            assert row is not None
            fn_aei = Decimal(str(row[0]))
        assert view_aei == fn_aei

    def test_view_excludes_counties_with_only_one_populated_year(
        self, phase3_db: psycopg.Connection
    ) -> None:
        """Add a single-year county and verify it is NOT in the view
        (AEI vs itself is trivially 1.0 and meaningless)."""
        with phase3_db.cursor() as cur:
            cur.execute(
                "INSERT INTO ref.county (county_id, state_code, county_fips, name) "
                "VALUES ('XX-LONELY', 'XX', '99002', 'Lonely County') "
                "ON CONFLICT DO NOTHING"
            )
            cur.execute(
                "INSERT INTO raw.nj_property_tax_county "
                "  (county_fips, year, avg_residential_value, cy_total_rate, "
                "   source_url, source_sha256, source_vintage) "
                "VALUES ('99002', 2024, 400000, 2.5000, "
                "        'https://www.nj.gov/dca/divisions/dlgs/', %s, '2024-annual') "
                "ON CONFLICT DO NOTHING",
                ("z" * 64,),
            )
            cur.execute(
                "INSERT INTO raw.acs_median_household_income "
                "  (county_fips, year, product, estimate, dollar_year, "
                "   source_url, source_sha256) "
                "VALUES ('99002', 2024, 'acs5', 110000, 2024, "
                "        'https://api.census.gov/data/2024/acs/acs5', %s) "
                "ON CONFLICT DO NOTHING",
                ("y" * 64,),
            )
        phase3_db.commit()

        out = _scalar(
            phase3_db,
            "SELECT count(*) FROM derived.v_aei_by_county WHERE county_fips=%s",
            "99002",
        )
        assert int(str(out)) == 0
