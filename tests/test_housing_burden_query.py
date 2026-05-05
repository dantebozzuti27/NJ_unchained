"""Live-PG validation of the housing-burden SQL surface used by the screener.

The Next.js screener (`lib/housing.ts`) runs two queries against the
production Postgres / Neon substrate to build the public housing pages:

1.  A per-county "latest joined year" rollup that joins
    ``derived.f_fhfa_hpi_indexed(BASE)`` to
    ``derived.f_acs_mhi_real(BASE)`` and returns the burden ratio.
2.  Per-county time-series for the detail page.

The frontend uses Neon's HTTP client and tagged-template binding; the
backend test here reissues the **identical SQL** through psycopg against
a freshly-migrated Postgres. That gives us:

  * proof the joins / column types / function signatures still work
    after schema changes,
  * proof the burden ratio is mathematically what we promised in the
    methodology page (HPI growth ÷ real-income growth),
  * a regression net for any future migration that renames a column or
    changes a function signature.

If the frontend and backend ever drift apart, this test is the canary.
We deliberately keep the SQL strings here byte-equivalent (modulo
parameter syntax) to ``lib/housing.ts`` and call out the contract
in comments.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import polars as pl
import pytest

if TYPE_CHECKING:
    import psycopg

from ingestion.bls_cpi import (
    FetchResult as CpiResult,
)
from ingestion.bls_cpi import (
    load_to_postgres as load_cpi_to_pg,
)
from ingestion.bls_cpi import (
    stage_dataframe as stage_cpi,
)
from ingestion.census_acs_income import (
    FetchResult as AcsResult,
)
from ingestion.census_acs_income import (
    load_to_postgres as load_acs_to_pg,
)
from ingestion.census_acs_income import (
    stage_dataframe as stage_acs,
)
from scripts.migrate import (
    MIGRATIONS_DIR,
    SEEDS_DIR,
    apply_migrations,
    discover,
)

# Must match `BURDEN_BASE_YEAR` in lib/housing.ts.
BURDEN_BASE_YEAR = 2010

pytestmark = pytest.mark.live_pg


# ---------------------------------------------------------------------------
# Fixture: cleanly initialized DB with FHFA + ACS + CPI substrates seeded
# with synthetic but realistic data for two counties.
# ---------------------------------------------------------------------------


@pytest.fixture
def burden_db(live_pg: psycopg.Connection) -> psycopg.Connection:
    """Init schema, load synthetic CPI + ACS + FHFA HPI for 2010 and 2022.

    We seed data for two counties so the listing query has more than one
    row to sort and the divergence calculation is verifiable for both.

      * 34003 Bergen   -- HPI grows 50% (100 -> 150), real income flat
                       => burden ratio = 1.50 / 1.00 = 1.50  (STRESS)
      * 34023 Middlesex -- HPI grows 20% (100 -> 120), real income +20%
                       => burden ratio = 1.20 / 1.20 = 1.00  (TRACKING)

    CPI: 2010 = 218.056, 2022 = 292.655 (real BLS values).
    """
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
            "    EXECUTE 'DROP VIEW IF EXISTS public.' || quote_ident(r.viewname) || ' CASCADE'; "
            "  END LOOP; "
            "END $$;"
        )
    conn.commit()

    apply_migrations(conn, discover(MIGRATIONS_DIR))
    apply_migrations(conn, discover(SEEDS_DIR))

    # CPI-U All Items: real published BLS annual averages for 2010, 2022.
    cpi_df = pl.DataFrame({
        "series_id": ["CUUR0000SA0", "CUUR0000SA0"],
        "year":      [2010, 2022],
        "period":    ["M13", "M13"],
        "value":     [218.056, 292.655],
    })
    load_cpi_to_pg(
        stage_cpi(CpiResult(
            dataframe=cpi_df,
            source_url="https://api.bls.gov/test",
            source_sha256="0" * 64,
            series_ids=("CUUR0000SA0",),
            start_year=2010, end_year=2022, n_observations=2,
        )),
        conn,
    )

    # ACS median household income: nominal $.
    # Bergen real income should hold roughly flat across 2010 -> 2022.
    # Real_income = nominal * (CPI_base / CPI_year) where base=2010.
    # For Bergen 2022 to be REAL-flat vs 2010 in 2010 dollars, we need:
    #   nominal_2022 * (218.056 / 292.655) ~= nominal_2010
    # so nominal_2022 ~= 80000 * (292.655/218.056) ~= 107362.
    # Middlesex: real income +20% means nominal_2022 = 80000 * 1.2 *
    # (292.655/218.056) ~= 128835.
    acs_df = pl.DataFrame({
        "county_fips":      ["34003", "34003", "34023", "34023"],
        "year":             [2010, 2022, 2010, 2022],
        "product":          ["acs5", "acs5", "acs5", "acs5"],
        "estimate":         [80_000.0, 107_362.0, 80_000.0, 128_835.0],
        "margin_of_error":  [1500.0, 1600.0, 1500.0, 1600.0],
        "dollar_year":      [2010, 2022, 2010, 2022],
        "suppression_code": [None, None, None, None],
    })
    load_acs_to_pg(
        stage_acs(AcsResult(
            dataframe=acs_df,
            source_url="https://api.census.gov/test",
            source_sha256="1" * 64,
            year=2022, product="acs5", state_fips="34", n_rows=4,
        )),
        conn,
    )

    # FHFA HPI: hand-insert directly (no bulk-loader needed). Schema is
    # county_fips, year, hpi_at, annual_change, n_transactions, plus
    # source_url/source_sha256/source_vintage provenance fields.
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw.fhfa_hpi_county (
                county_fips, year, hpi_at, annual_change, n_transactions,
                source_url, source_sha256, source_vintage
            ) VALUES
              ('34003', 2010, 100.00, 0.00, 1000,
               'https://test/fhfa', %(h1)s, '2024Q4'),
              ('34003', 2022, 150.00, 5.00, 1500,
               'https://test/fhfa', %(h1)s, '2024Q4'),
              ('34023', 2010, 100.00, 0.00, 1000,
               'https://test/fhfa', %(h1)s, '2024Q4'),
              ('34023', 2022, 120.00, 3.00, 1500,
               'https://test/fhfa', %(h1)s, '2024Q4')
            """,
            {"h1": "2" * 64},
        )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_fhfa_indexed_function_returns_expected_shape(burden_db: psycopg.Connection) -> None:
    """The HPI-indexed function returns 100.0 at the base year by definition."""
    cur = burden_db.execute(
        "SELECT county_fips, year, hpi_indexed::FLOAT8 "
        "FROM derived.f_fhfa_hpi_indexed(%s::SMALLINT) "
        "WHERE county_fips IN ('34003', '34023') "
        "ORDER BY county_fips, year",
        (BURDEN_BASE_YEAR,),
    )
    rows = cur.fetchall()
    assert len(rows) == 4
    by_key = {(r[0], int(r[1])): float(r[2]) for r in rows}
    assert by_key[("34003", 2010)] == pytest.approx(100.0)
    assert by_key[("34003", 2022)] == pytest.approx(150.0)
    assert by_key[("34023", 2010)] == pytest.approx(100.0)
    assert by_key[("34023", 2022)] == pytest.approx(120.0)


def test_acs_real_income_matches_cpi_deflator(burden_db: psycopg.Connection) -> None:
    """Real income at the base year equals nominal income (deflator=1)."""
    cur = burden_db.execute(
        "SELECT county_fips, year, estimate_real::FLOAT8 "
        "FROM derived.f_acs_mhi_real(%s::SMALLINT) "
        "WHERE county_fips IN ('34003', '34023') AND product = 'acs5' "
        "ORDER BY county_fips, year",
        (BURDEN_BASE_YEAR,),
    )
    rows = cur.fetchall()
    assert len(rows) == 4
    by_key = {(r[0], int(r[1])): float(r[2]) for r in rows}
    # base year: real == nominal == 80000
    assert by_key[("34003", 2010)] == pytest.approx(80_000.0)
    assert by_key[("34023", 2010)] == pytest.approx(80_000.0)
    # Bergen 2022: real ~= 80000 (we picked nominal so this is true)
    assert by_key[("34003", 2022)] == pytest.approx(80_000.0, rel=5e-3)
    # Middlesex 2022: real ~= 80000 * 1.20 = 96000
    assert by_key[("34023", 2022)] == pytest.approx(96_000.0, rel=5e-3)


def test_screener_listing_query_burden_ratios(burden_db: psycopg.Connection) -> None:
    """The exact SQL served by the screener listing page returns the
    expected burden ratios.

    This SQL is a 1:1 mirror of `listCountyBurden()` in
    lib/housing.ts, modulo psycopg parameter syntax. If a future
    migration breaks the join, this test fails before the screener
    page does.

    Expected (BASE=2010):
      Bergen     34003: HPI 1.50 / income 1.00 = 1.50  (STRESS)
      Middlesex  34023: HPI 1.20 / income 1.20 = 1.00  (TRACKING)
    """
    sql = """
        WITH counties AS (
          SELECT county_id, county_fips, name AS county_name
          FROM ref.county
          WHERE state_code = 'NJ'
            AND county_fips IN ('34003', '34023')
        ),
        hpi AS (
          SELECT county_fips, year, hpi_indexed
          FROM derived.f_fhfa_hpi_indexed(%(base)s::SMALLINT)
        ),
        income AS (
          SELECT county_fips, year, estimate_real
          FROM derived.f_acs_mhi_real(%(base)s::SMALLINT)
          WHERE product = 'acs5'
        ),
        paired AS (
          SELECT
            c.county_id, c.county_fips, c.county_name,
            h.year, h.hpi_indexed, i.estimate_real
          FROM counties c
          JOIN hpi    h ON h.county_fips = c.county_fips
          JOIN income i ON i.county_fips = c.county_fips AND i.year = h.year
        ),
        latest AS (
          SELECT DISTINCT ON (county_fips)
            county_id, county_fips, county_name,
            year         AS year_latest,
            hpi_indexed  AS hpi_indexed_latest,
            estimate_real AS estimate_real_latest
          FROM paired
          ORDER BY county_fips, year DESC
        ),
        income_base AS (
          SELECT county_fips, estimate_real AS base_income
          FROM derived.f_acs_mhi_real(%(base)s::SMALLINT)
          WHERE product = 'acs5' AND year = %(base)s
        )
        SELECT
          c.county_fips,
          l.year_latest::INT AS year_latest,
          (l.hpi_indexed_latest / 100.0)::FLOAT8 AS hpi_growth,
          (l.estimate_real_latest / ib.base_income)::FLOAT8 AS income_growth,
          (
            (l.hpi_indexed_latest / 100.0)
            / (l.estimate_real_latest / ib.base_income)
          )::FLOAT8 AS burden_ratio
        FROM counties c
        LEFT JOIN latest      l  ON l.county_fips = c.county_fips
        LEFT JOIN income_base ib ON ib.county_fips = c.county_fips
        ORDER BY burden_ratio DESC NULLS LAST, c.county_name ASC
    """
    cur = burden_db.execute(sql, {"base": BURDEN_BASE_YEAR})
    rows = cur.fetchall()
    assert len(rows) == 2

    # First row is the higher burden (sorted DESC).
    bergen = rows[0]
    middlesex = rows[1]
    assert bergen[0] == "34003"
    assert int(bergen[1]) == 2022
    assert float(bergen[2]) == pytest.approx(1.50, rel=1e-3)
    assert float(bergen[3]) == pytest.approx(1.00, rel=5e-3)
    assert float(bergen[4]) == pytest.approx(1.50, rel=5e-3)

    assert middlesex[0] == "34023"
    assert int(middlesex[1]) == 2022
    assert float(middlesex[2]) == pytest.approx(1.20, rel=1e-3)
    assert float(middlesex[3]) == pytest.approx(1.20, rel=5e-3)
    assert float(middlesex[4]) == pytest.approx(1.00, rel=5e-3)


def test_screener_listing_includes_counties_without_data_as_nulls(
    burden_db: psycopg.Connection,
) -> None:
    """All 21 NJ counties appear in the listing; ones without HPI/ACS data
    show NULL ratios. This guarantees the housing page renders a complete
    21-row table even before all counties are loaded.
    """
    sql = """
        WITH counties AS (
          SELECT county_id, county_fips, name AS county_name
          FROM ref.county
          WHERE state_code = 'NJ'
        ),
        hpi AS (
          SELECT county_fips, year, hpi_indexed
          FROM derived.f_fhfa_hpi_indexed(%(base)s::SMALLINT)
        ),
        income AS (
          SELECT county_fips, year, estimate_real
          FROM derived.f_acs_mhi_real(%(base)s::SMALLINT)
          WHERE product = 'acs5'
        ),
        paired AS (
          SELECT c.county_id, c.county_fips, h.year, h.hpi_indexed, i.estimate_real
          FROM counties c
          JOIN hpi    h ON h.county_fips = c.county_fips
          JOIN income i ON i.county_fips = c.county_fips AND i.year = h.year
        ),
        latest AS (
          SELECT DISTINCT ON (county_fips)
            county_id, county_fips, year AS year_latest, hpi_indexed, estimate_real
          FROM paired ORDER BY county_fips, year DESC
        )
        SELECT
          c.county_fips,
          l.year_latest::INT,
          l.hpi_indexed::FLOAT8
        FROM counties c
        LEFT JOIN latest l ON l.county_fips = c.county_fips
        ORDER BY c.county_name
    """
    cur = burden_db.execute(sql, {"base": BURDEN_BASE_YEAR})
    rows = cur.fetchall()
    # NJ has 21 counties; ref.county is seeded by migration 001.
    assert len(rows) == 21

    by_fips = {r[0]: (r[1], r[2]) for r in rows}
    # Two counties with data
    assert by_fips["34003"][0] is not None
    assert by_fips["34023"][0] is not None
    # Other 19 counties: NULL year/HPI
    populated = {"34003", "34023"}
    for fips, (year_latest, hpi) in by_fips.items():
        if fips in populated:
            assert year_latest is not None and hpi is not None
        else:
            assert year_latest is None and hpi is None


def test_screener_detail_query_returns_indexed_series(burden_db: psycopg.Connection) -> None:
    """The per-county detail endpoint's SQL returns a series with
    BASE_YEAR=100 by construction for the first observation.
    """
    sql = """
        WITH base_row AS (
          SELECT estimate_real AS base_income
          FROM derived.f_acs_mhi_real(%(base)s::SMALLINT)
          WHERE product = 'acs5' AND year = %(base)s AND county_fips = %(fips)s
        )
        SELECT
          m.year::INT AS year,
          round((m.estimate_real / b.base_income * 100.0)::NUMERIC, 3)::FLOAT8
                                                          AS indexed
        FROM derived.f_acs_mhi_real(%(base)s::SMALLINT) m
        CROSS JOIN base_row b
        WHERE m.product = 'acs5'
          AND m.county_fips = %(fips)s
          AND b.base_income IS NOT NULL
          AND b.base_income <> 0
        ORDER BY year
    """
    cur = burden_db.execute(
        sql, {"base": BURDEN_BASE_YEAR, "fips": "34003"},
    )
    rows = cur.fetchall()
    assert len(rows) == 2
    by_year = {int(r[0]): float(r[1]) for r in rows}
    # Base year is 100.0 by construction.
    assert by_year[2010] == pytest.approx(100.0)
    # Bergen 2022 real income is engineered to be flat in real terms.
    assert by_year[2022] == pytest.approx(100.0, rel=5e-3)
