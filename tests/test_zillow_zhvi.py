"""Tests for the Zillow ZHVI ingester (Phase 6, migration 079).

Two layers:

1. UNIT TESTS (no DB) -- exercise the parser against a synthetic CSV
   that mirrors Zillow's wide layout (9 identifier columns followed by
   month columns named YYYY-MM-DD). Pin column-melt behavior, NULL
   handling, state filtering, FIPS construction, and the schema-version
   guard.

2. LIVE-PG TESTS -- spin up a fully-migrated DB, insert a tiny synthetic
   ZHVI panel for two NJ counties across two years, and pin every output
   of derived.v_zhvi_county_annual, derived.f_zhvi_county_indexed, and
   derived.f_housing_index_cross_source to a hand-computed value.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import polars as pl
import pytest

from ingestion._base import IngestError
from ingestion.zillow_zhvi import (
    NJ_STATE_CODE,
    ZHVI_COUNTY_URL,
    _parse_last_modified,
    parse_zhvi_county_csv,
    stage_dataframe,
)

if TYPE_CHECKING:
    import psycopg


# ============================================================================
# Last-Modified parsing
# ============================================================================


def test_parse_last_modified_handles_imf_fixdate() -> None:
    """RFC 7231 IMF-fixdate parses to an aware UTC datetime."""
    parsed = _parse_last_modified("Thu, 16 Apr 2026 17:36:30 GMT")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.year == 2026
    assert parsed.month == 4
    assert parsed.day == 16


def test_parse_last_modified_handles_missing_or_invalid() -> None:
    assert _parse_last_modified(None) is None
    assert _parse_last_modified("") is None
    assert _parse_last_modified("not a date") is None


# ============================================================================
# Parser unit tests against synthetic wide CSVs
# ============================================================================


def _write_synthetic_zhvi_csv(
    path: Path,
    *,
    rows: list[dict[str, object]],
    months: list[str],
) -> None:
    """Write a Zillow-shaped wide CSV with the 9 identifier columns followed
    by one column per element of *months*. Uses :mod:`csv` so commas inside
    Metro values get quoted exactly the way Zillow's real CSV does.
    """
    import csv as _csv

    id_cols = [
        "RegionID", "SizeRank", "RegionName", "RegionType",
        "StateName", "State", "Metro", "StateCodeFIPS", "MunicipalCodeFIPS",
    ]
    cols = id_cols + months
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = _csv.writer(fh, quoting=_csv.QUOTE_MINIMAL)
        w.writerow(cols)
        for r in rows:
            w.writerow([str(r.get(c, "")) for c in cols])


def test_parse_zhvi_synthetic_two_counties_two_months(tmp_path: Path) -> None:
    """Parser produces 4 long-format observations for 2 counties x 2 months."""
    months = ["2024-01-31", "2024-02-29"]
    rows = [
        {
            "RegionID": 874, "SizeRank": 5, "RegionName": "Bergen County",
            "RegionType": "county", "StateName": "New Jersey", "State": "NJ",
            "Metro": "New York-Newark-Jersey City, NY-NJ-PA",
            "StateCodeFIPS": "34", "MunicipalCodeFIPS": "003",
            "2024-01-31": "700000.00", "2024-02-29": "705000.50",
        },
        {
            "RegionID": 1201, "SizeRank": 50, "RegionName": "Mercer County",
            "RegionType": "county", "StateName": "New Jersey", "State": "NJ",
            "Metro": "Trenton-Princeton, NJ",
            "StateCodeFIPS": "34", "MunicipalCodeFIPS": "021",
            "2024-01-31": "420000.00", "2024-02-29": "421500.25",
        },
        # Out-of-state row -- must be filtered.
        {
            "RegionID": 9999, "SizeRank": 1, "RegionName": "New York County",
            "RegionType": "county", "StateName": "New York", "State": "NY",
            "Metro": "New York-Newark-Jersey City, NY-NJ-PA",
            "StateCodeFIPS": "36", "MunicipalCodeFIPS": "061",
            "2024-01-31": "1500000", "2024-02-29": "1505000",
        },
    ]

    csv_path = tmp_path / "zhvi.csv"
    _write_synthetic_zhvi_csv(csv_path, rows=rows, months=months)

    result = parse_zhvi_county_csv(
        csv_path, sha256="a" * 64, last_modified=None,
    )
    assert result.n_counties == 2
    assert result.n_observations == 4

    df = result.dataframe.sort(["county_fips", "observation_month"])
    fips_list = df["county_fips"].to_list()
    assert fips_list == ["34003", "34003", "34021", "34021"]

    bergen_jan = df.filter(
        (pl.col("county_fips") == "34003")
        & (pl.col("observation_month") == dt.date(2024, 1, 31))
    )
    assert bergen_jan["zhvi"].item() == pytest.approx(700_000.0)
    assert bergen_jan["region_id"].item() == 874
    assert bergen_jan["region_name"].item() == "Bergen County"
    assert bergen_jan["state_code"].item() == "NJ"


def test_parse_zhvi_drops_null_value_months(tmp_path: Path) -> None:
    """Months with empty-string ZHVI are dropped (Zillow had NULLs in early years)."""
    months = ["2000-01-31", "2024-01-31"]
    rows = [{
        "RegionID": 874, "SizeRank": 5, "RegionName": "Bergen County",
        "RegionType": "county", "StateName": "New Jersey", "State": "NJ",
        "Metro": "NYC", "StateCodeFIPS": "34", "MunicipalCodeFIPS": "003",
        "2000-01-31": "",  # null -- early-coverage gap
        "2024-01-31": "700000",
    }]
    csv_path = tmp_path / "zhvi.csv"
    _write_synthetic_zhvi_csv(csv_path, rows=rows, months=months)

    result = parse_zhvi_county_csv(
        csv_path, sha256="b" * 64, last_modified=None,
    )
    assert result.n_observations == 1
    assert result.dataframe["observation_month"].item() == dt.date(2024, 1, 31)


def test_parse_zhvi_rejects_missing_identifier_columns(tmp_path: Path) -> None:
    """Schema-version guard fires when an identifier column is absent."""
    csv_path = tmp_path / "zhvi.csv"
    csv_path.write_text(
        "RegionID,RegionName,State,2024-01-31\n"  # missing StateCodeFIPS, MunicipalCodeFIPS, ...
        "874,Bergen County,NJ,700000\n",
        encoding="utf-8",
    )
    with pytest.raises(IngestError, match="missing required identifier columns"):
        parse_zhvi_county_csv(csv_path, sha256="c" * 64, last_modified=None)


def test_parse_zhvi_rejects_non_date_month_columns(tmp_path: Path) -> None:
    """A non-YYYY-MM-DD column header trips the date-parse guard."""
    months = ["NotADate"]
    rows = [{
        "RegionID": 1, "SizeRank": 1, "RegionName": "X County",
        "RegionType": "county", "StateName": "New Jersey", "State": "NJ",
        "Metro": "", "StateCodeFIPS": "34", "MunicipalCodeFIPS": "003",
        "NotADate": "100",
    }]
    csv_path = tmp_path / "zhvi.csv"
    _write_synthetic_zhvi_csv(csv_path, rows=rows, months=months)
    with pytest.raises(IngestError, match="not a YYYY-MM-DD date"):
        parse_zhvi_county_csv(csv_path, sha256="d" * 64, last_modified=None)


def test_parse_zhvi_rejects_zero_values(tmp_path: Path) -> None:
    """Non-positive ZHVI is treated as a parse bug, not a substrate value."""
    months = ["2024-01-31"]
    rows = [{
        "RegionID": 1, "SizeRank": 1, "RegionName": "Bergen County",
        "RegionType": "county", "StateName": "New Jersey", "State": "NJ",
        "Metro": "", "StateCodeFIPS": "34", "MunicipalCodeFIPS": "003",
        "2024-01-31": "0",
    }]
    csv_path = tmp_path / "zhvi.csv"
    _write_synthetic_zhvi_csv(csv_path, rows=rows, months=months)
    with pytest.raises(IngestError, match="non-positive observations"):
        parse_zhvi_county_csv(csv_path, sha256="e" * 64, last_modified=None)


def test_parse_zhvi_uses_state_filter(tmp_path: Path) -> None:
    """Passing state_code='NY' includes NY rows and excludes NJ rows."""
    months = ["2024-01-31"]
    rows = [
        {
            "RegionID": 874, "SizeRank": 5, "RegionName": "Bergen County",
            "RegionType": "county", "StateName": "New Jersey", "State": "NJ",
            "Metro": "", "StateCodeFIPS": "34", "MunicipalCodeFIPS": "003",
            "2024-01-31": "700000",
        },
        {
            "RegionID": 9999, "SizeRank": 1, "RegionName": "New York County",
            "RegionType": "county", "StateName": "New York", "State": "NY",
            "Metro": "", "StateCodeFIPS": "36", "MunicipalCodeFIPS": "061",
            "2024-01-31": "1500000",
        },
    ]
    csv_path = tmp_path / "zhvi.csv"
    _write_synthetic_zhvi_csv(csv_path, rows=rows, months=months)

    nj = parse_zhvi_county_csv(csv_path, sha256="f" * 64, last_modified=None,
                               state_code="NJ")
    assert nj.n_counties == 1
    assert nj.dataframe["county_fips"].item() == "34003"

    ny = parse_zhvi_county_csv(csv_path, sha256="f" * 64, last_modified=None,
                               state_code="NY")
    assert ny.n_counties == 1
    assert ny.dataframe["county_fips"].item() == "36061"


def test_stage_dataframe_attaches_provenance(tmp_path: Path) -> None:
    """stage_dataframe adds source_url / source_sha256 / source_modified_at."""
    months = ["2024-01-31"]
    rows = [{
        "RegionID": 874, "SizeRank": 5, "RegionName": "Bergen County",
        "RegionType": "county", "StateName": "New Jersey", "State": "NJ",
        "Metro": "NYC", "StateCodeFIPS": "34", "MunicipalCodeFIPS": "003",
        "2024-01-31": "700000",
    }]
    csv_path = tmp_path / "zhvi.csv"
    _write_synthetic_zhvi_csv(csv_path, rows=rows, months=months)
    last_mod = dt.datetime(2026, 4, 16, 17, 36, 30, tzinfo=dt.UTC)
    parsed = parse_zhvi_county_csv(
        csv_path, sha256="0" * 64, last_modified=last_mod,
    )
    staged = stage_dataframe(parsed)
    assert staged["source_url"][0] == ZHVI_COUNTY_URL
    assert staged["source_sha256"][0] == "0" * 64
    assert staged["source_modified_at"][0] == last_mod
    assert staged["source_vintage"][0].startswith("zhvi-county-2026-04-16")


# ============================================================================
# Live-PG: derived views/functions on a synthetic two-county panel
# ============================================================================




@pytest.fixture
def zhvi_db(live_pg: psycopg.Connection) -> psycopg.Connection:
    """Drop platform schemas, reapply migrations + seeds, then load a tiny
    two-county synthetic ZHVI panel for 2010-2024 plus FHFA HPI for the
    same panel so the cross-source divergence function has both sides.
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

    # Two NJ counties, three years (2010, 2020, 2024), 12 months each
    # so v_zhvi_county_annual.n_months = 12 for every (county, year).
    # Bergen 2010 mean = $400,000; 2020 mean = $600,000; 2024 mean = $720,000.
    # Mercer 2010 mean = $300,000; 2020 mean = $410,000; 2024 mean = $440,000.
    # Each value identical across all 12 months so AVG = MIN = MAX = year value.
    panel = [
        ("34003", 2010, 400_000.0),
        ("34003", 2020, 600_000.0),
        ("34003", 2024, 720_000.0),
        ("34021", 2010, 300_000.0),
        ("34021", 2020, 410_000.0),
        ("34021", 2024, 440_000.0),
    ]
    with conn.cursor() as cur:
        for fips, year, value in panel:
            for month in range(1, 13):
                last_day = (
                    dt.date(year, month + 1, 1) if month < 12
                    else dt.date(year + 1, 1, 1)
                ) - dt.timedelta(days=1)
                cur.execute(
                    "INSERT INTO raw.zillow_zhvi_county "
                    "  (region_id, county_fips, region_name, state_code, "
                    "   metro, observation_month, zhvi, "
                    "   source_url, source_sha256, source_vintage) "
                    "VALUES (%s, %s, %s, 'NJ', 'NYC', %s, %s, "
                    "        'http://test/zhvi', %s, 'test-vintage') "
                    "ON CONFLICT DO NOTHING",
                    (
                        874 if fips == "34003" else 1201,
                        fips,
                        "Bergen County" if fips == "34003" else "Mercer County",
                        last_day,
                        value,
                        "0" * 64,
                    ),
                )

        # FHFA HPI for the same (county, year) pairs so the cross-source
        # function has both sides. Hand-pick values so the divergence is
        # nontrivial: FHFA grows 50%, ZHVI grows 80% from 2010 to 2024.
        fhfa_panel = [
            ("34003", 2010, 100.0),
            ("34003", 2020, 130.0),
            ("34003", 2024, 150.0),
            ("34021", 2010, 100.0),
            ("34021", 2020, 120.0),
            ("34021", 2024, 130.0),
        ]
        for fips, year, hpi in fhfa_panel:
            cur.execute(
                "INSERT INTO raw.fhfa_hpi_county "
                "  (county_fips, year, hpi_at, source_url, source_sha256, source_vintage) "
                "VALUES (%s, %s, %s, 'http://test/fhfa', %s, '2024-annual') "
                "ON CONFLICT DO NOTHING",
                (fips, year, hpi, "1" * 64),
            )
    conn.commit()
    return conn


def _scalar(conn: psycopg.Connection, sql: str, *params: object) -> object:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    assert row is not None
    return row[0]


@pytest.mark.live_pg
class TestVZhviCountyAnnual:
    """derived.v_zhvi_county_annual: mean across calendar months + n_months."""

    def test_bergen_2010_annual_mean(self, zhvi_db: psycopg.Connection) -> None:
        v = _scalar(
            zhvi_db,
            "SELECT zhvi_annual_mean FROM derived.v_zhvi_county_annual "
            "WHERE county_fips='34003' AND year=2010",
        )
        # 12 months @ $400,000 -> mean $400,000.
        assert Decimal(str(v)) == Decimal("400000.0000")

    def test_bergen_2024_year_end_value(self, zhvi_db: psycopg.Connection) -> None:
        v = _scalar(
            zhvi_db,
            "SELECT zhvi_year_end FROM derived.v_zhvi_county_annual "
            "WHERE county_fips='34003' AND year=2024",
        )
        assert Decimal(str(v)) == Decimal("720000.0000")

    def test_n_months_is_12(self, zhvi_db: psycopg.Connection) -> None:
        v = _scalar(
            zhvi_db,
            "SELECT n_months FROM derived.v_zhvi_county_annual "
            "WHERE county_fips='34021' AND year=2020",
        )
        assert v == 12


@pytest.mark.live_pg
class TestFZhviCountyIndexed:
    """derived.f_zhvi_county_indexed(base_year): re-indexed series."""

    def test_base_year_is_100(self, zhvi_db: psycopg.Connection) -> None:
        v = _scalar(
            zhvi_db,
            "SELECT zhvi_indexed FROM derived.f_zhvi_county_indexed(2010::SMALLINT) "
            "WHERE county_fips='34003' AND year=2010",
        )
        assert Decimal(str(v)) == Decimal("100.000")

    def test_bergen_2024_indexed_to_2010(self, zhvi_db: psycopg.Connection) -> None:
        # 720000 / 400000 * 100 = 180.000.
        v = _scalar(
            zhvi_db,
            "SELECT zhvi_indexed FROM derived.f_zhvi_county_indexed(2010::SMALLINT) "
            "WHERE county_fips='34003' AND year=2024",
        )
        assert Decimal(str(v)) == Decimal("180.000")

    def test_mercer_2024_indexed_to_2010(self, zhvi_db: psycopg.Connection) -> None:
        # 440000 / 300000 * 100 = 146.667.
        v = _scalar(
            zhvi_db,
            "SELECT zhvi_indexed FROM derived.f_zhvi_county_indexed(2010::SMALLINT) "
            "WHERE county_fips='34021' AND year=2024",
        )
        assert Decimal(str(v)) == Decimal("146.667")

    def test_function_returns_zero_rows_for_unseeded_base(
        self, zhvi_db: psycopg.Connection,
    ) -> None:
        """A base year with no rows returns an empty result set, not NULLs."""
        n = _scalar(
            zhvi_db,
            "SELECT count(*) FROM derived.f_zhvi_county_indexed(1990::SMALLINT)",
        )
        assert n == 0


@pytest.mark.live_pg
class TestFHousingIndexCrossSource:
    """derived.f_housing_index_cross_source(base_year): FHFA vs ZHVI."""

    def test_bergen_2024_divergence_signed(self, zhvi_db: psycopg.Connection) -> None:
        """ZHVI(2024 / 2010) = 180; FHFA(2024 / 2010) = 150 -> divergence = +30 pts."""
        with zhvi_db.cursor() as cur:
            cur.execute(
                "SELECT fhfa_hpi_indexed, zillow_zhvi_indexed, "
                "       divergence_indexed_points, divergence_pct_of_fhfa "
                "FROM derived.f_housing_index_cross_source(2010::SMALLINT) "
                "WHERE county_fips='34003' AND year=2024",
            )
            row = cur.fetchone()
        assert row is not None
        fhfa, zhvi, diff_pts, diff_pct = row
        assert Decimal(str(fhfa)) == Decimal("150.000")
        assert Decimal(str(zhvi)) == Decimal("180.000")
        assert Decimal(str(diff_pts)) == Decimal("30.0000")
        # 30 / 150 = 0.20000.
        assert Decimal(str(diff_pct)) == Decimal("0.20000")

    def test_base_year_divergence_is_zero(self, zhvi_db: psycopg.Connection) -> None:
        """Base year = 100 in both indices -> divergence = 0 by construction."""
        v = _scalar(
            zhvi_db,
            "SELECT divergence_indexed_points "
            "FROM derived.f_housing_index_cross_source(2010::SMALLINT) "
            "WHERE county_fips='34003' AND year=2010",
        )
        assert Decimal(str(v)) == Decimal("0.0000")


@pytest.mark.live_pg
class TestPublicViewVZhviNjRecent:
    """public.v_zhvi_nj_recent: most-recent 12 months per county."""

    def test_recent_view_returns_24_rows(self, zhvi_db: psycopg.Connection) -> None:
        """2 counties x 12 months in 2024 = 24 rows."""
        n = _scalar(zhvi_db, "SELECT count(*) FROM public.v_zhvi_nj_recent")
        assert n == 24

    def test_recent_view_has_county_id_join(self, zhvi_db: psycopg.Connection) -> None:
        county_id = _scalar(
            zhvi_db,
            "SELECT DISTINCT county_id FROM public.v_zhvi_nj_recent "
            "WHERE county_fips='34003'",
        )
        assert county_id == "NJ-BERGEN"


_ = NJ_STATE_CODE  # silence unused-import lint when tests pass
