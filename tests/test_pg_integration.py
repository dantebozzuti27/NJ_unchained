"""End-to-end live-Postgres integration test.

Skipped unless ``PG_TEST_DSN`` is set to an ephemeral Postgres. The test
applies all migrations and seeds, loads a tiny synthetic HUD crosswalk
plus a tiny LCA file, runs the aggregator, and asserts the contents of
``derived.lca_wage_by_county_yr_visa``.

Bring up an ephemeral Postgres locally with:

    docker run --rm -e POSTGRES_PASSWORD=ci -p 5432:5432 -d postgres:16
    export PG_TEST_DSN=postgresql://postgres:ci@localhost:5432/postgres

then ``make test``.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from derived.lca_aggregator import (
    DEFAULT_FORMULA_VERSION,
    aggregate_groups,
    fetch_observations,
    run_aggregation,
    write_aggregate_rows,
)
from ingestion.dol_oflc_lca import (
    load_to_postgres as load_lca_to_pg,
)
from ingestion.dol_oflc_lca import (
    parse_lca_file,
)
from ingestion.dol_oflc_lca import (
    stage_dataframe as stage_lca,
)
from ingestion.hud_zip_county import (
    load_to_postgres as load_hud_to_pg,
)
from ingestion.hud_zip_county import (
    parse_hud_file,
)
from ingestion.hud_zip_county import (
    stage_dataframe as stage_hud,
)
from scripts.migrate import (
    MIGRATIONS_DIR,
    SEEDS_DIR,
    apply_migrations,
    discover,
)

# CPI + ACS imports are local to the deflator test to keep import latency
# low for the LCA-focused tests above.

pytestmark = pytest.mark.live_pg


# ---------------------------------------------------------------------------
# Fixture: cleanly initialized DB (migrations + seeds applied)
# ---------------------------------------------------------------------------


@pytest.fixture
def initialized_db(live_pg):
    """Drop any prior schemas, re-apply all migrations + seeds, return the conn."""
    conn = live_pg
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS governance CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS derived    CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS raw        CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS ref        CASCADE")
        # Drop any of our public.v_* views the migrations create.
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
    return conn


# ---------------------------------------------------------------------------
# Fixture: synthetic HUD + LCA files
# ---------------------------------------------------------------------------


@pytest.fixture
def hud_csv(tmp_path: Path) -> Path:
    """A HUD crosswalk that covers our test ZIPs cleanly (sums to 1.0 each)."""
    path = tmp_path / "ZIP_COUNTY_032024.csv"
    path.write_text(
        "ZIP,COUNTY,RES_RATIO,BUS_RATIO,OTH_RATIO,TOT_RATIO\n"
        # 08830 Iselin, NJ -- entirely in NJ-MIDDLESEX (FIPS 34023).
        "08830,34023,1.0,1.0,1.0,1.0\n"
        # 07102 Newark, NJ -- entirely in NJ-ESSEX (FIPS 34013).
        "07102,34013,1.0,1.0,1.0,1.0\n"
        # 08901 New Brunswick, NJ -- splits 0.7/0.3 between MIDDLESEX/SOMERSET.
        "08901,34023,0.6,0.7,0.7,0.65\n"
        "08901,34035,0.4,0.3,0.3,0.35\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def lca_csv(tmp_path: Path) -> Path:
    """Synthetic LCA file with 12 CERTIFIED + 1 DENIED row in MIDDLESEX FY2024.

    12 is just above the SUPPRESSION_MIN_N=10 threshold, so percentile
    columns must be populated (not NULL).

    Uses the modern (FY2023+) v5_2023 column convention: NAICS_CODE,
    BEGIN_DATE/END_DATE, TOTAL_WORKER_POSITIONS.
    """
    path = tmp_path / "LCA_Disclosure_Data_FY2024_Q3.csv"
    rows = []
    rows.append(
        "CASE_NUMBER,CASE_STATUS,VISA_CLASS,EMPLOYER_NAME,NAICS_CODE,"
        "FULL_TIME_POSITION,BEGIN_DATE,END_DATE,"
        "WORKSITE_CITY,WORKSITE_STATE,WORKSITE_POSTAL_CODE,"
        "TOTAL_WORKER_POSITIONS,WAGE_RATE_OF_PAY_FROM,WAGE_RATE_OF_PAY_TO,"
        "WAGE_UNIT_OF_PAY,PREVAILING_WAGE,PW_UNIT_OF_PAY"
    )
    # 12 CERTIFIED rows in 08830 (MIDDLESEX), wage = 60K..115K in 5K steps.
    for i in range(12):
        wage = 60_000 + i * 5_000
        pw = 55_000 + i * 5_000
        rows.append(
            f"I-205-24001-{i:03d},CERTIFIED,H-1B,Test Employer LLC,541512,Y,"
            f"2024-07-15 00:00:00,2027-07-14 00:00:00,"
            f"Iselin,NJ,8830,1,{wage},{wage + 10_000},Year,{pw},Year"
        )
    # 1 DENIED row -- must be excluded by the aggregator.
    rows.append(
        "I-205-24001-999,DENIED,H-1B,Other Employer LLC,541512,Y,"
        "2024-07-15 00:00:00,2027-07-14 00:00:00,"
        "Iselin,NJ,8830,1,99999,99999,Year,99999,Year"
    )
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_migrations_apply_idempotently(initialized_db) -> None:
    """Re-applying the same migrations twice should be a no-op (skipped)."""
    conn = initialized_db
    second_run = apply_migrations(conn, discover(MIGRATIONS_DIR))
    assert all(action == "skipped" for _, action in second_run), second_run


def test_nj_counties_are_seeded(initialized_db) -> None:
    cur = initialized_db.execute(
        "SELECT count(*) FROM ref.county WHERE state_code = 'NJ'"
    )
    row = cur.fetchone()
    assert row is not None
    assert row[0] == 21


def test_hud_load_validates_ratio_sums(initialized_db, hud_csv: Path) -> None:
    """The DEFERRABLE CONSTRAINT TRIGGER fires at COMMIT for incomplete loads."""
    conn = initialized_db

    # Successful load: all our test ZIPs sum to 1.0.
    parsed = parse_hud_file(hud_csv)
    staged = stage_hud(parsed, source_url="https://example.test/hud")
    n = load_hud_to_pg(staged, conn)
    conn.commit()
    assert n == 4


def test_end_to_end_lca_aggregation(
    initialized_db, hud_csv: Path, lca_csv: Path,
) -> None:
    conn = initialized_db

    # 1) Load HUD crosswalk.
    hud_parsed = parse_hud_file(hud_csv)
    hud_staged = stage_hud(hud_parsed, source_url="https://example.test/hud")
    n_hud = load_hud_to_pg(hud_staged, conn)
    conn.commit()
    assert n_hud == 4

    # 2) Load LCA file.
    lca_parsed = parse_lca_file(lca_csv)
    lca_staged = stage_lca(lca_parsed)
    n_lca = load_lca_to_pg(lca_staged, conn)
    conn.commit()
    # 12 CERTIFIED + 1 DENIED = 13 rows in raw.
    assert n_lca == 13

    # 3) Run aggregator.
    n_written, vintage_hash = run_aggregation(conn)
    conn.commit()
    assert n_written == 1, "expect exactly one (county, FY, visa) cell"
    assert len(vintage_hash) == 64

    # 4) Assert the derived row.
    cur = conn.execute(
        "SELECT county_id, fiscal_year, visa_class, "
        "       n_unweighted_certs, n_certs_weighted, "
        "       median_annualized_wage_from, "
        "       p25_annualized_wage_from, p75_annualized_wage_from "
        "FROM derived.lca_wage_by_county_yr_visa"
    )
    rows = cur.fetchall()
    assert len(rows) == 1
    (county_id, fy, visa, n_unw, n_w, median_w, p25_w, p75_w) = rows[0]
    assert county_id == "NJ-MIDDLESEX"
    assert fy == 2024
    assert visa == "H-1B"
    assert n_unw == 12
    # bus_ratio for 08830->MIDDLESEX is 1.0; sum of 12 = 12.
    assert float(n_w) == pytest.approx(12.0)
    # The 12 wages are 60K, 65K, ..., 115K. Equal weights -> type-1 median
    # is the value at cumulative weight >= 6 / 12 = 0.5, i.e. the 6th sorted
    # value: 60K, 65K, 70K, 75K, 80K, 85K -> 85K.
    assert float(median_w) == 85_000.0
    # 25th: cum >= 3, sorted index 3 -> 70K (because cum after 3 obs = 3).
    # Type-1: smallest v whose cum_w >= q*total_w = 3.
    # Sort: 60(1),65(2),70(3),75(4),80(5),85(6),90(7),95(8),100(9),105(10),110(11),115(12)
    # cum=3 hits at 70K -> p25 = 70K.
    assert float(p25_w) == 70_000.0
    # 75th: cum >= 9 -> 100K.
    assert float(p75_w) == 100_000.0


def test_aggregator_is_idempotent_under_replay(
    initialized_db, hud_csv: Path, lca_csv: Path,
) -> None:
    """Two consecutive runs of run_aggregation must INSERT zero new rows on the second pass."""
    conn = initialized_db

    hud_staged = stage_hud(parse_hud_file(hud_csv), source_url="https://example.test/hud")
    load_hud_to_pg(hud_staged, conn)
    conn.commit()

    lca_staged = stage_lca(parse_lca_file(lca_csv))
    load_lca_to_pg(lca_staged, conn)
    conn.commit()

    n1, hash1 = run_aggregation(conn)
    conn.commit()
    n2, hash2 = run_aggregation(conn)
    conn.commit()

    assert n1 >= 1
    assert n2 == 0, "second run must be a no-op via ON CONFLICT DO NOTHING"
    assert hash1 == hash2, "vintage_hash must be deterministic across runs"


def test_observations_query_excludes_denied_rows(
    initialized_db, hud_csv: Path, lca_csv: Path,
) -> None:
    conn = initialized_db

    hud_staged = stage_hud(parse_hud_file(hud_csv), source_url="https://example.test/hud")
    load_hud_to_pg(hud_staged, conn)
    conn.commit()

    lca_staged = stage_lca(parse_lca_file(lca_csv))
    load_lca_to_pg(lca_staged, conn)
    conn.commit()

    obs = fetch_observations(conn)
    # 12 CERTIFIED LCAs, each goes to exactly one county (MIDDLESEX, bus_ratio=1.0).
    assert len(obs) == 12
    assert all(o.county_id == "NJ-MIDDLESEX" for o in obs)
    assert all(o.visa_class == "H-1B" for o in obs)


def test_polars_round_trip(
    initialized_db, hud_csv: Path,
) -> None:
    """Polars SELECT round-trip: HUD rows we loaded come back exactly as staged."""
    conn = initialized_db
    hud_parsed = parse_hud_file(hud_csv)
    hud_staged = stage_hud(hud_parsed, source_url="https://example.test/hud")
    load_hud_to_pg(hud_staged, conn)
    conn.commit()

    cur = conn.execute(
        "SELECT zip5, county_fips, res_ratio, bus_ratio "
        "FROM ref.zip_county ORDER BY zip5, county_fips"
    )
    db_rows = cur.fetchall()
    expected = (
        hud_staged
        .select(["zip5", "county_fips", "res_ratio", "bus_ratio"])
        .sort(["zip5", "county_fips"])
        .rows()
    )
    assert len(db_rows) == len(expected)
    for actual, exp in zip(db_rows, expected, strict=True):
        assert actual[0] == exp[0]                         # zip5
        assert actual[1] == exp[1]                         # county_fips
        assert float(actual[2]) == pytest.approx(exp[2])   # res_ratio
        assert float(actual[3]) == pytest.approx(exp[3])   # bus_ratio


def test_thin_cell_suppression_in_db(
    initialized_db, hud_csv: Path, tmp_path: Path,
) -> None:
    """Aggregator-side suppression must produce a row with NULL percentiles for thin cells."""
    conn = initialized_db

    hud_staged = stage_hud(parse_hud_file(hud_csv), source_url="https://example.test/hud")
    load_hud_to_pg(hud_staged, conn)
    conn.commit()

    # Build a 5-row LCA file (below SUPPRESSION_MIN_N=10).
    p = tmp_path / "LCA_Disclosure_Data_FY2024_Q1.csv"
    rows = [
        "CASE_NUMBER,CASE_STATUS,VISA_CLASS,EMPLOYER_NAME,NAICS_CODE,"
        "FULL_TIME_POSITION,BEGIN_DATE,END_DATE,"
        "WORKSITE_CITY,WORKSITE_STATE,WORKSITE_POSTAL_CODE,"
        "TOTAL_WORKER_POSITIONS,WAGE_RATE_OF_PAY_FROM,WAGE_RATE_OF_PAY_TO,"
        "WAGE_UNIT_OF_PAY,PREVAILING_WAGE,PW_UNIT_OF_PAY",
    ]
    for i in range(5):
        rows.append(
            f"I-205-24091-{i:03d},CERTIFIED,H-1B,Tiny Employer LLC,541512,Y,"
            f"2024-01-01 00:00:00,2026-12-31 00:00:00,"
            f"Newark,NJ,07102,1,{60_000 + i * 1000},{70_000 + i * 1000},Year,"
            f"{55_000 + i * 1000},Year"
        )
    p.write_text("\n".join(rows) + "\n", encoding="utf-8")

    lca_staged = stage_lca(parse_lca_file(p))
    load_lca_to_pg(lca_staged, conn)
    conn.commit()

    # Pure-Python aggregate -- the DB CHECK constraint on
    # n_unweighted_certs >= 10 OR all percentiles NULL would refuse a
    # rule violation; aggregator must NULL them itself.
    n_written, _ = run_aggregation(conn)
    conn.commit()

    cur = conn.execute(
        "SELECT n_unweighted_certs, median_annualized_wage_from "
        "FROM derived.lca_wage_by_county_yr_visa "
        "WHERE county_id = 'NJ-ESSEX' AND fiscal_year = 2024"
    )
    rows_db = cur.fetchall()
    assert len(rows_db) == 1
    n_unw, median_w = rows_db[0]
    assert n_unw == 5
    assert median_w is None   # suppressed because n < 10
    assert n_written >= 1


# ---------------------------------------------------------------------------
# CPI + ACS deflator end-to-end (Tier 2 substrate)
# ---------------------------------------------------------------------------


def test_cpi_acs_deflator_end_to_end(initialized_db) -> None:
    """The deflator function joins ACS to CPI cleanly and produces real-dollar income.

    Loads two synthetic CPI rows and two synthetic ACS rows, then asserts
    that derived.f_acs_mhi_real(2022) deflates the 2010 ACS estimate
    correctly to 2022 dollars.
    """
    from ingestion.bls_cpi import FetchResult as CpiResult
    from ingestion.bls_cpi import (
        load_to_postgres as load_cpi_to_pg,
    )
    from ingestion.bls_cpi import (
        stage_dataframe as stage_cpi,
    )
    from ingestion.census_acs_income import FetchResult as AcsResult
    from ingestion.census_acs_income import (
        load_to_postgres as load_acs_to_pg,
    )
    from ingestion.census_acs_income import (
        stage_dataframe as stage_acs,
    )

    conn = initialized_db

    # Two synthetic CPI-U All Items observations: 2010 = 218.056, 2022 = 292.655.
    # These are the actual published BLS values for CUUR0000SA0 M13.
    cpi_df = pl.DataFrame({
        "series_id": ["CUUR0000SA0", "CUUR0000SA0"],
        "year":      [2010, 2022],
        "period":    ["M13", "M13"],
        "value":     [218.056, 292.655],
    })
    cpi_result = CpiResult(
        dataframe=cpi_df,
        source_url="https://api.bls.gov/test",
        source_sha256="0" * 64,
        series_ids=("CUUR0000SA0",),
        start_year=2010, end_year=2022, n_observations=2,
    )
    load_cpi_to_pg(stage_cpi(cpi_result), conn)

    # Two synthetic ACS rows for Bergen County (34003): 2010 and 2022.
    # The 2022 estimate matches Census's published value.
    acs_df = pl.DataFrame({
        "county_fips":      ["34003", "34003"],
        "year":             [2010, 2022],
        "product":          ["acs5", "acs5"],
        "estimate":         [82_002.0, 118_714.0],   # nominal, current-year $
        "margin_of_error":  [1500.0, 1607.0],
        "dollar_year":      [2010, 2022],
        "suppression_code": [None, None],
    })
    acs_result = AcsResult(
        dataframe=acs_df,
        source_url="https://api.census.gov/test",
        source_sha256="1" * 64,
        year=2022, product="acs5", state_fips="34", n_rows=2,
    )
    load_acs_to_pg(stage_acs(acs_result), conn)
    conn.commit()

    # Exercise the deflator function. Bergen 2010 nominal = $82,002 in 2010 $.
    # Deflator to 2022 = 292.655 / 218.056 ~= 1.34209.
    # Real (in 2022 $) = 82,002 * 1.34209 ~= $110,054.
    cur = conn.execute(
        "SELECT estimate_real, estimate_nominal, deflator "
        "FROM derived.f_acs_mhi_real(2022::SMALLINT) "
        "WHERE county_fips = '34003' AND year = 2010 AND product = 'acs5'"
    )
    row = cur.fetchone()
    assert row is not None
    real, nominal, deflator = row
    assert float(nominal) == pytest.approx(82002.0)
    assert float(deflator) == pytest.approx(292.655 / 218.056, rel=1e-5)
    assert float(real) == pytest.approx(82002.0 * (292.655 / 218.056), rel=1e-4)

    # The 2022 row's real == nominal (deflator is 1.0 at the base year).
    cur = conn.execute(
        "SELECT estimate_real, estimate_nominal, deflator "
        "FROM derived.f_acs_mhi_real(2022::SMALLINT) "
        "WHERE county_fips = '34003' AND year = 2022"
    )
    row = cur.fetchone()
    assert row is not None
    real_2022, nominal_2022, deflator_2022 = row
    assert float(deflator_2022) == pytest.approx(1.0, abs=1e-6)
    assert float(real_2022) == pytest.approx(float(nominal_2022))

    # The annual-average view should expose CUUR0000SA0 cleanly.
    cur = conn.execute(
        "SELECT year, cpi_u_all_items FROM derived.cpi_u_headline_annual "
        "ORDER BY year"
    )
    rows = cur.fetchall()
    assert (2010, 218.056) in [(int(y), float(v)) for y, v in rows]
    assert (2022, 292.655) in [(int(y), float(v)) for y, v in rows]


# Quietly suppress noise from polars import in test discovery context.
_ = pl, DEFAULT_FORMULA_VERSION, aggregate_groups, write_aggregate_rows
