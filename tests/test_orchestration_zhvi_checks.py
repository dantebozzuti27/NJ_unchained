"""Live-Postgres tests for the Phase 6 + Phase 7 ZHVI asset checks.

The four checks under test
--------------------------

* ``zhvi_row_count_positive``                       -- raw.zillow_zhvi_county must have >= 1 row
* ``zhvi_nj_county_coverage``                       -- all 21 NJ counties present in latest month
* ``zhvi_yoy_outliers_plausible``                   -- NJ YoY growth in [-20%, +30%] in latest year
* ``housing_index_cross_source_divergence_plausible`` -- NJ |FHFA - ZHVI| <= 20% in latest year

Each test seeds a minimal synthetic substrate and pins both the
pass/fail outcome and the exact metadata the check returns. The
threshold-calibration headers in ``orchestration/asset_checks.py``
explain the empirical basis for the bounds; these tests confirm the
SQL behind those bounds matches the documented contract.
"""

from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

import pytest

if TYPE_CHECKING:
    import psycopg


pytestmark = pytest.mark.live_pg


# ---------------------------------------------------------------------------
# Fixture: clean DB with all migrations + seeds applied.
# ---------------------------------------------------------------------------


@pytest.fixture
def zhvi_db(live_pg: psycopg.Connection) -> psycopg.Connection:
    """Apply migrations + seeds against a clean DB; yield the conn."""
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
            "    EXECUTE 'DROP VIEW IF EXISTS public.' || quote_ident(r.viewname) "
            "            || ' CASCADE'; "
            "  END LOOP; "
            "END $$;",
        )
    conn.commit()
    apply_migrations(conn, discover(MIGRATIONS_DIR))
    apply_migrations(conn, discover(SEEDS_DIR))
    return conn


# ---------------------------------------------------------------------------
# Helpers (shared shape with test_orchestration_fraud_pg.py)
# ---------------------------------------------------------------------------


def _pg_resource_for(conn: psycopg.Connection) -> Any:
    """Wrap a psycopg connection in a PgResource-like shim for asset-check use."""

    class _Shim:
        @contextmanager
        def connect(self) -> Any:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    return _Shim()


class _CapturingGovernance:
    """Capture HealthSignal emissions without round-tripping to Postgres."""

    def __init__(self) -> None:
        from orchestration.resources import HealthSignal

        self._HealthSignal = HealthSignal
        self.emitted: list[Any] = []

    def emit(self, signal: Any) -> None:
        from orchestration.resources import _VALID_SEVERITIES

        if signal.severity not in _VALID_SEVERITIES:
            raise ValueError(
                f"Invalid severity {signal.severity!r}; expected one of "
                f"{sorted(_VALID_SEVERITIES)}",
            )
        self.emitted.append(signal)


def _run_check(check_fn: Any, conn: psycopg.Connection) -> Any:
    """Invoke an @asset_check function with shimmed dependencies."""
    underlying = cast("Any", check_fn).op.compute_fn.decorated_fn
    return underlying(
        context=None,
        pg=_pg_resource_for(conn),
        governance=_CapturingGovernance(),
    )


def _unwrap_metadata(result: Any) -> dict[str, Any]:
    """Unwrap AssetCheckResult.metadata's typed MetadataValues to plain values."""
    out: dict[str, Any] = {}
    for k, v in result.metadata.items():
        if hasattr(v, "value"):
            out[k] = v.value
        elif hasattr(v, "text"):
            out[k] = v.text
        elif hasattr(v, "data"):
            out[k] = v.data
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Substrate seeders (minimal synthetic ZHVI + FHFA panels)
# ---------------------------------------------------------------------------


def _seed_zhvi_panel(
    conn: psycopg.Connection,
    rows: list[tuple[str, str, dt.date, float]],
) -> None:
    """Insert (region_name, county_fips, observation_month, zhvi) rows.

    Provenance fields are filled with deterministic stand-ins.
    """
    with conn.cursor() as cur:
        for region_name, fips, month, zhvi in rows:
            cur.execute(
                "INSERT INTO raw.zillow_zhvi_county "
                "  (region_id, county_fips, region_name, state_code, "
                "   metro, observation_month, zhvi, "
                "   source_url, source_sha256, source_vintage) "
                "VALUES (%s, %s, %s, 'NJ', 'NYC', %s, %s, "
                "        'http://test/zhvi', %s, 'test-vintage') "
                "ON CONFLICT DO NOTHING",
                (
                    {"34003": 874, "34021": 1201, "34009": 2174,
                     "34011": 2913, "34029": 2002, "34037": 1413,
                     "34005": 1001, "34007": 2007, "34013": 2013,
                     "34015": 2015, "34017": 2017, "34019": 2019,
                     "34023": 2023, "34025": 2025, "34027": 2027,
                     "34031": 2031, "34033": 2033, "34035": 2035,
                     "34039": 2039, "34041": 2041, "34001": 2001}.get(fips, 9999),
                    fips, region_name, month, zhvi, "0" * 64,
                ),
            )
    conn.commit()


def _seed_full_year_zhvi(
    conn: psycopg.Connection,
    fips: str,
    region_name: str,
    year: int,
    annual_value: float,
) -> None:
    """Seed 12 months of constant ZHVI so AVG = annual_value exactly."""
    rows = []
    for month in range(1, 13):
        last_day = (
            dt.date(year, month + 1, 1) if month < 12
            else dt.date(year + 1, 1, 1)
        ) - dt.timedelta(days=1)
        rows.append((region_name, fips, last_day, annual_value))
    _seed_zhvi_panel(conn, rows)


def _seed_fhfa_year(
    conn: psycopg.Connection,
    fips: str,
    year: int,
    hpi_at: float,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.fhfa_hpi_county "
            "  (county_fips, year, hpi_at, source_url, source_sha256, source_vintage) "
            "VALUES (%s, %s, %s, 'http://test/fhfa', %s, 'test-vintage') "
            "ON CONFLICT DO NOTHING",
            (fips, year, hpi_at, "1" * 64),
        )
    conn.commit()


# All 21 NJ counties so nj_county_coverage can pass.
_NJ_FIPS_NAMES: list[tuple[str, str]] = [
    ("34001", "Atlantic County"),
    ("34003", "Bergen County"),
    ("34005", "Burlington County"),
    ("34007", "Camden County"),
    ("34009", "Cape May County"),
    ("34011", "Cumberland County"),
    ("34013", "Essex County"),
    ("34015", "Gloucester County"),
    ("34017", "Hudson County"),
    ("34019", "Hunterdon County"),
    ("34021", "Mercer County"),
    ("34023", "Middlesex County"),
    ("34025", "Monmouth County"),
    ("34027", "Morris County"),
    ("34029", "Ocean County"),
    ("34031", "Passaic County"),
    ("34033", "Salem County"),
    ("34035", "Somerset County"),
    ("34037", "Sussex County"),
    ("34039", "Union County"),
    ("34041", "Warren County"),
]


# ===========================================================================
# Scenario A: empty raw -- row_count_positive must FAIL; coverage WARN-empty.
# ===========================================================================


def test_zhvi_row_count_positive_fails_on_empty_table(
    zhvi_db: psycopg.Connection,
) -> None:
    """No rows -> row_count_positive must NOT pass."""
    from orchestration.asset_checks import zhvi_row_count_positive

    result = _run_check(zhvi_row_count_positive, zhvi_db)
    assert result.passed is False
    md = _unwrap_metadata(result)
    assert md["row_count"] == 0


def test_zhvi_nj_county_coverage_fails_on_empty_table(
    zhvi_db: psycopg.Connection,
) -> None:
    """No rows -> coverage check fails (0 of 21)."""
    from orchestration.asset_checks import zhvi_nj_county_coverage

    result = _run_check(zhvi_nj_county_coverage, zhvi_db)
    assert result.passed is False
    md = _unwrap_metadata(result)
    assert md["nj_counties_in_latest_month"] == 0
    assert md["expected"] == 21


# ===========================================================================
# Scenario B: 21 NJ counties present in latest month -> coverage passes.
# ===========================================================================


def test_zhvi_nj_county_coverage_passes_when_all_21_counties_present(
    zhvi_db: psycopg.Connection,
) -> None:
    """One row per NJ county at month 2024-12-31 -> coverage = 21 / 21."""
    rows = [
        (name, fips, dt.date(2024, 12, 31), 500_000.0 + i * 10_000)
        for i, (fips, name) in enumerate(_NJ_FIPS_NAMES)
    ]
    _seed_zhvi_panel(zhvi_db, rows)

    from orchestration.asset_checks import zhvi_nj_county_coverage

    result = _run_check(zhvi_nj_county_coverage, zhvi_db)
    assert result.passed is True
    md = _unwrap_metadata(result)
    assert md["nj_counties_in_latest_month"] == 21
    assert md["expected"] == 21


def test_zhvi_nj_county_coverage_fails_when_one_county_missing(
    zhvi_db: psycopg.Connection,
) -> None:
    """20 of 21 -> coverage fails."""
    rows = [
        (name, fips, dt.date(2024, 12, 31), 500_000.0 + i * 10_000)
        for i, (fips, name) in enumerate(_NJ_FIPS_NAMES[:-1])  # drop Warren
    ]
    _seed_zhvi_panel(zhvi_db, rows)

    from orchestration.asset_checks import zhvi_nj_county_coverage

    result = _run_check(zhvi_nj_county_coverage, zhvi_db)
    assert result.passed is False
    md = _unwrap_metadata(result)
    assert md["nj_counties_in_latest_month"] == 20


# ===========================================================================
# Scenario C: ZHVI YoY outlier check on a hand-pinned panel.
# ===========================================================================


def test_zhvi_yoy_outliers_passes_when_all_growth_inside_envelope(
    zhvi_db: psycopg.Connection,
) -> None:
    """+5% YoY for Bergen, -3% YoY for Mercer -> well inside [-15%, +25%]."""
    _seed_full_year_zhvi(zhvi_db, "34003", "Bergen County", 2023, 600_000.0)
    _seed_full_year_zhvi(zhvi_db, "34003", "Bergen County", 2024, 630_000.0)  # +5%
    _seed_full_year_zhvi(zhvi_db, "34021", "Mercer County", 2023, 400_000.0)
    _seed_full_year_zhvi(zhvi_db, "34021", "Mercer County", 2024, 388_000.0)  # -3%

    from orchestration.asset_checks import zhvi_yoy_outliers_plausible

    result = _run_check(zhvi_yoy_outliers_plausible, zhvi_db)
    assert result.passed is True
    md = _unwrap_metadata(result)
    assert md["n_total"] == 2
    assert md["n_error"] == 0
    assert md["n_warn"] == 0


def test_zhvi_yoy_outliers_warns_at_edge_of_warn_band(
    zhvi_db: psycopg.Connection,
) -> None:
    """+27% YoY -> WARN band [25%, 30%], n_warn=1, passed=True."""
    _seed_full_year_zhvi(zhvi_db, "34003", "Bergen County", 2023, 600_000.0)
    _seed_full_year_zhvi(zhvi_db, "34003", "Bergen County", 2024, 762_000.0)  # +27%

    from orchestration.asset_checks import zhvi_yoy_outliers_plausible

    result = _run_check(zhvi_yoy_outliers_plausible, zhvi_db)
    assert result.passed is True   # WARN doesn't fail the check
    md = _unwrap_metadata(result)
    assert md["n_total"] == 1
    assert md["n_error"] == 0
    assert md["n_warn"] == 1
    assert 0.265 <= md["yoy_max"] <= 0.275  # 27% +/- rounding


def test_zhvi_yoy_outliers_fails_above_30pct_growth(
    zhvi_db: psycopg.Connection,
) -> None:
    """+35% YoY -> ERROR; check fails."""
    _seed_full_year_zhvi(zhvi_db, "34003", "Bergen County", 2023, 600_000.0)
    _seed_full_year_zhvi(zhvi_db, "34003", "Bergen County", 2024, 810_000.0)  # +35%

    from orchestration.asset_checks import zhvi_yoy_outliers_plausible

    result = _run_check(zhvi_yoy_outliers_plausible, zhvi_db)
    assert result.passed is False
    md = _unwrap_metadata(result)
    assert md["n_error"] == 1
    assert md["yoy_max"] > 0.30


def test_zhvi_yoy_outliers_fails_below_minus_20pct_growth(
    zhvi_db: psycopg.Connection,
) -> None:
    """-25% YoY -> ERROR; check fails."""
    _seed_full_year_zhvi(zhvi_db, "34003", "Bergen County", 2023, 600_000.0)
    _seed_full_year_zhvi(zhvi_db, "34003", "Bergen County", 2024, 450_000.0)  # -25%

    from orchestration.asset_checks import zhvi_yoy_outliers_plausible

    result = _run_check(zhvi_yoy_outliers_plausible, zhvi_db)
    assert result.passed is False
    md = _unwrap_metadata(result)
    assert md["n_error"] == 1
    assert md["yoy_min"] < -0.20


# ===========================================================================
# Scenario D: cross-source divergence check on hand-pinned ZHVI + FHFA panels.
# ===========================================================================


def test_cross_source_divergence_passes_when_indices_agree(
    zhvi_db: psycopg.Connection,
) -> None:
    """Both indices grow 30% from 2010 to 2024 -> 0% divergence everywhere."""
    _seed_full_year_zhvi(zhvi_db, "34003", "Bergen County", 2010, 400_000.0)
    _seed_full_year_zhvi(zhvi_db, "34003", "Bergen County", 2024, 520_000.0)
    _seed_fhfa_year(zhvi_db, "34003", 2010, 100.0)
    _seed_fhfa_year(zhvi_db, "34003", 2024, 130.0)

    from orchestration.asset_checks import (
        housing_index_cross_source_divergence_plausible,
    )

    result = _run_check(housing_index_cross_source_divergence_plausible, zhvi_db)
    assert result.passed is True
    md = _unwrap_metadata(result)
    assert md["n_total"] == 1
    assert md["n_error_over_20pct"] == 0
    assert md["n_warn_12_to_20pct"] == 0
    assert md["max_abs_pct"] == pytest.approx(0.0, abs=0.001)


def test_cross_source_divergence_warns_at_15pct_disagreement(
    zhvi_db: psycopg.Connection,
) -> None:
    """ZHVI 100 -> 165, FHFA 100 -> 150 in 2024.
    Divergence pct of FHFA = (165 - 150) / 150 = 10%, which is exactly at the
    warn-band lower bound (12%). With this configuration we land OUTSIDE the
    warn band (<= 12%), so n_warn = 0. The case below pushes it into the warn
    band proper.
    """
    _seed_full_year_zhvi(zhvi_db, "34003", "Bergen County", 2010, 400_000.0)
    _seed_full_year_zhvi(zhvi_db, "34003", "Bergen County", 2024, 660_000.0)  # +65%
    _seed_fhfa_year(zhvi_db, "34003", 2010, 100.0)
    _seed_fhfa_year(zhvi_db, "34003", 2024, 150.0)  # +50%

    from orchestration.asset_checks import (
        housing_index_cross_source_divergence_plausible,
    )

    result = _run_check(housing_index_cross_source_divergence_plausible, zhvi_db)
    assert result.passed is True   # 10% is below the 12% warn threshold
    md = _unwrap_metadata(result)
    assert md["n_total"] == 1
    assert md["n_error_over_20pct"] == 0
    assert md["n_warn_12_to_20pct"] == 0
    # Divergence pct of FHFA = (165-150)/150 = 0.10
    assert md["max_abs_pct"] == pytest.approx(0.10, abs=0.001)


def test_cross_source_divergence_fails_when_30pct_disagreement(
    zhvi_db: psycopg.Connection,
) -> None:
    """ZHVI grows 100->200, FHFA grows 100->150 -> divergence = 33% -> ERROR."""
    _seed_full_year_zhvi(zhvi_db, "34003", "Bergen County", 2010, 400_000.0)
    _seed_full_year_zhvi(zhvi_db, "34003", "Bergen County", 2024, 800_000.0)
    _seed_fhfa_year(zhvi_db, "34003", 2010, 100.0)
    _seed_fhfa_year(zhvi_db, "34003", 2024, 150.0)

    from orchestration.asset_checks import (
        housing_index_cross_source_divergence_plausible,
    )

    result = _run_check(housing_index_cross_source_divergence_plausible, zhvi_db)
    assert result.passed is False
    md = _unwrap_metadata(result)
    assert md["n_error_over_20pct"] >= 1
    assert md["max_abs_pct"] > 0.20


def test_cross_source_divergence_handles_partial_substrate(
    zhvi_db: psycopg.Connection,
) -> None:
    """ZHVI present in 2024, FHFA absent in 2024 but both present in 2010.

    The check's CTE picks the latest year where BOTH sources have data,
    which is 2010 here (not 2024). At 2010 both indices = base = 100, so
    divergence is exactly 0 and the check passes. This is the
    substrate-honest behavior -- the cross-source signal can only fire
    when BOTH sources are loaded for the same (county, year).
    """
    _seed_full_year_zhvi(zhvi_db, "34003", "Bergen County", 2010, 400_000.0)
    _seed_full_year_zhvi(zhvi_db, "34003", "Bergen County", 2024, 600_000.0)
    _seed_fhfa_year(zhvi_db, "34003", 2010, 100.0)
    # No FHFA row for 2024 -- substrate gap.

    from orchestration.asset_checks import (
        housing_index_cross_source_divergence_plausible,
    )

    result = _run_check(housing_index_cross_source_divergence_plausible, zhvi_db)
    assert result.passed is True
    md = _unwrap_metadata(result)
    # Latest year with both = 2010 -> exactly 1 (county, year) pair, 0 divergence.
    assert md["n_total"] == 1
    assert md["n_error_over_20pct"] == 0
    assert md["max_abs_pct"] == pytest.approx(0.0, abs=0.001)
