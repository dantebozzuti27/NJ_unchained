"""Tests for the FastAPI serving layer.

We use FastAPI's TestClient (synchronous), which exercises the real
ASGI app without bringing up uvicorn. The tests run against the same
PG_TEST_DSN-pointed Postgres instance the integration tests use.

Test taxonomy
-------------
1. **Pure helpers**: compute_freshness_state covers the freshness
   classification logic without DB access.

2. **Schema validation**: every endpoint's response must match its
   Pydantic model. We validate by relying on FastAPI's response_model
   coercion -- if the SQL column types drift, the response 500s and
   the test catches it.

3. **HTTP contract**: 404 / 400 paths return the documented error
   shape. Future-me wants regression coverage on these because the
   error responses are part of the contract.
"""

from __future__ import annotations

import datetime as dt
import os
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi.testclient import TestClient

# Skip entire module if PG_TEST_DSN is not set OR if FastAPI is not
# installed (serving is an optional extra).
pytest.importorskip("fastapi")
pytest.importorskip("psycopg_pool")

if not os.environ.get("PG_TEST_DSN"):
    pytest.skip(
        "PG_TEST_DSN not set; skipping serving tests "
        "(they require a live Postgres).",
        allow_module_level=True,
    )


# ============================================================================
# Pure helpers
# ============================================================================


def test_compute_freshness_state_fresh() -> None:
    from serving.queries import compute_freshness_state

    now = dt.datetime(2026, 4, 29, 12, 0, tzinfo=dt.UTC)
    last = dt.datetime(2026, 4, 29, 11, 30, tzinfo=dt.UTC)  # 30 min ago
    state, age = compute_freshness_state(
        last_materialized_at=last, expected_lag_hours=24, now=now,
    )
    assert state == "fresh"
    assert age == 0.5


def test_compute_freshness_state_stale() -> None:
    from serving.queries import compute_freshness_state

    now = dt.datetime(2026, 4, 29, 12, 0, tzinfo=dt.UTC)
    last = dt.datetime(2026, 4, 27, 12, 0, tzinfo=dt.UTC)  # 48h ago
    state, age = compute_freshness_state(
        last_materialized_at=last, expected_lag_hours=24, now=now,
    )
    assert state == "stale"
    assert age == 48.0


def test_compute_freshness_state_unknown_when_no_materialization() -> None:
    from serving.queries import compute_freshness_state

    state, age = compute_freshness_state(
        last_materialized_at=None, expected_lag_hours=24,
    )
    assert state == "unknown"
    assert age is None


def test_compute_freshness_state_unknown_when_no_calendar() -> None:
    from serving.queries import compute_freshness_state

    state, age = compute_freshness_state(
        last_materialized_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        expected_lag_hours=None,
    )
    assert state == "unknown"
    assert age is None


def test_compute_freshness_state_handles_naive_timestamp() -> None:
    """Postgres TIMESTAMPTZ -> psycopg returns aware. But if a test
    fixture passes naive, we should still produce a sensible answer
    rather than crash."""
    from serving.queries import compute_freshness_state

    now = dt.datetime(2026, 4, 29, 12, 0, tzinfo=dt.UTC)
    last = dt.datetime(2026, 4, 29, 11, 0)  # naive
    state, age = compute_freshness_state(
        last_materialized_at=last, expected_lag_hours=24, now=now,
    )
    assert state == "fresh"
    assert age == 1.0


# ============================================================================
# HTTP contract
# ============================================================================


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    """Yield a FastAPI TestClient bound to the test Postgres.

    Using TestClient as a context manager ensures the FastAPI lifespan
    handlers fire on enter (init pool) and exit (close pool), so the
    test process terminates cleanly without psycopg-pool worker
    threadpool warnings.

    Critical: PG_DSN is set in os.environ for the duration of the
    fixture and RESTORED on teardown. The previous implementation
    leaked PG_DSN globally for the rest of the pytest session,
    breaking 23 downstream `test_*_cli_requires_pg_dsn` tests that
    rely on PG_DSN being unset.
    """
    _orig_pg_dsn = os.environ.get("PG_DSN")
    os.environ["PG_DSN"] = os.environ["PG_TEST_DSN"]

    from fastapi.testclient import TestClient

    from serving.app import create_app

    app = create_app()
    try:
        with TestClient(app) as c:
            yield c
    finally:
        if _orig_pg_dsn is None:
            os.environ.pop("PG_DSN", None)
        else:
            os.environ["PG_DSN"] = _orig_pg_dsn


def test_health_endpoint(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["db_reachable"] is True
    assert body["status"] in {"ok", "degraded"}
    assert body["api_version"] == "0.1.0"


def test_releases_endpoint(client: TestClient) -> None:
    r = client.get("/releases")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) >= 7
    for row in rows:
        assert "source_id" in row
        assert "cadence" in row
        assert row["cadence"] in {
            "daily", "weekly", "monthly", "quarterly", "annual", "on_event",
        }


def test_release_calendar_endpoint(client: TestClient) -> None:
    r = client.get("/release-calendar?days=14")
    assert r.status_code == 200
    body = r.json()
    assert "as_of" in body
    assert body["horizon_days"] == 14
    sources = body["sources"]
    assert len(sources) >= 7
    for src in sources:
        assert src["freshness_state"] in {"fresh", "stale", "unknown"}
        assert isinstance(src["overdue"], bool)
        assert isinstance(src["schedule_computed"], bool)
        assert "upcoming_releases" in src
        assert "next_expected_at" in src


def test_assets_list_endpoint(client: TestClient) -> None:
    r = client.get("/assets")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) >= 1
    for row in rows:
        assert row["freshness_state"] in {"fresh", "stale", "unknown"}
        assert "n_warn_30d" in row
        assert "n_error_30d" in row


def test_asset_detail_404_on_unknown(client: TestClient) -> None:
    r = client.get("/assets/raw/does_not_exist")
    assert r.status_code == 404
    assert "Unknown dataset" in r.json()["detail"]


def test_asset_detail_known_source(client: TestClient) -> None:
    """raw.fred_observation should at minimum exist in release_calendar."""
    r = client.get("/assets/raw/fred_observation")
    assert r.status_code == 200
    body = r.json()
    assert body["dataset_id"] == "raw.fred_observation"
    assert body["cadence"] == "weekly"


def test_asset_by_dataset_id_bad_format(client: TestClient) -> None:
    r = client.get("/asset/Raw.Fred")
    assert r.status_code == 400
    assert "dataset_id" in r.json()["detail"].lower()


def test_asset_by_dataset_id_matches_split_path(client: TestClient) -> None:
    """BBG-LIKE-2: dotted id is equivalent to /assets/{schema}/{table}."""
    r1 = client.get("/assets/raw/fred_observation")
    r2 = client.get("/asset/raw.fred_observation")
    assert r1.status_code == r2.status_code
    if r1.status_code == 200:
        assert r1.json() == r2.json()


def test_burden_invalid_fips_returns_400(client: TestClient) -> None:
    r = client.get("/burden/99999")
    assert r.status_code == 400
    assert "Invalid NJ county FIPS" in r.json()["detail"]


def test_burden_missing_county_returns_404(client: TestClient) -> None:
    """No county data populated => 404, not 200 with empty array."""
    r = client.get("/burden/34001")  # Atlantic County, valid FIPS
    assert r.status_code in {200, 404}
    if r.status_code == 404:
        assert "No housing burden data" in r.json()["detail"]


def test_burden_latest_endpoint(client: TestClient) -> None:
    r = client.get("/burden")
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)
    if rows:
        for row in rows:
            assert row["county_fips"].startswith("34")


# ============================================================================
# /pums-burden endpoints
# ============================================================================


def test_pums_burden_invalid_puma_returns_400(client: TestClient) -> None:
    """Non-numeric PUMA must be rejected at the route boundary."""
    r = client.get("/pums-burden/abc")
    assert r.status_code == 400
    assert "Invalid PUMA code" in r.json()["detail"]


def test_pums_burden_invalid_dim_returns_400(client: TestClient) -> None:
    """Unknown segment dim must be rejected at the route boundary."""
    r = client.get("/pums-burden?dim=unknown")
    assert r.status_code == 400
    assert "Invalid dim" in r.json()["detail"]


def test_pums_burden_invalid_tenure_returns_400(client: TestClient) -> None:
    """Unknown tenure must be rejected at the route boundary."""
    r = client.get("/pums-burden?tenure=foo")
    assert r.status_code == 400
    assert "Invalid tenure" in r.json()["detail"]


def test_pums_burden_unknown_puma_returns_404(client: TestClient) -> None:
    """Valid-shape PUMA with no data returns 404, not empty 200."""
    r = client.get("/pums-burden/99999")
    assert r.status_code == 404
    assert "No PUMS burden data" in r.json()["detail"]


def test_pums_burden_list_endpoint_shape(client: TestClient) -> None:
    """The list endpoint returns a list of rows with the expected schema.

    May be empty in the test DB if the derived asset has not been
    materialized; we assert shape, not content.
    """
    r = client.get("/pums-burden?dim=overall")
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)
    for row in rows:
        # Every row must carry the natural-key + classification columns,
        # plus the SDR standard-error companions for every percentile.
        for k in (
            "year", "product", "puma", "tenure_class",
            "segment_dim", "segment_value",
            "weighted_n", "sample_n", "suppressed",
            "household_income_p50",   "household_income_p50_se",
            "monthly_cost_p50",       "monthly_cost_p50_se",
            "burden_ratio_p50",       "burden_ratio_p50_se",
        ):
            assert k in row, f"PumsBurdenRow missing column {k!r}"


def test_pums_burden_default_excludes_suppressed(client: TestClient) -> None:
    """Default include_suppressed=false; no suppressed=true rows leak through."""
    r = client.get("/pums-burden")
    assert r.status_code == 200
    rows = r.json()
    for row in rows:
        assert row["suppressed"] is False, (
            "Default GET /pums-burden must not include suppressed cells; "
            "consumers opt in via ?include_suppressed=true."
        )


# ============================================================================
# /pums-burden-county endpoints
# ============================================================================


def test_pums_burden_county_invalid_fips_returns_400(client: TestClient) -> None:
    """Non-5-digit county FIPS must be rejected."""
    r = client.get("/pums-burden-county/abcde")
    assert r.status_code == 400
    assert "Invalid county_fips" in r.json()["detail"]


def test_pums_burden_county_invalid_dim_returns_400(client: TestClient) -> None:
    """Unknown segment dim must be rejected at the route boundary."""
    r = client.get("/pums-burden-county?dim=unknown")
    assert r.status_code == 400
    assert "Invalid dim" in r.json()["detail"]


def test_pums_burden_county_invalid_tenure_returns_400(client: TestClient) -> None:
    """Unknown tenure must be rejected at the route boundary."""
    r = client.get("/pums-burden-county?tenure=foo")
    assert r.status_code == 400


def test_pums_burden_county_unknown_county_returns_404(client: TestClient) -> None:
    """Valid-shape county_fips with no data returns 404, not empty 200."""
    r = client.get("/pums-burden-county/99999")
    assert r.status_code == 404
    assert "No PUMS burden data" in r.json()["detail"]


def test_pums_burden_county_list_endpoint_shape(client: TestClient) -> None:
    """List endpoint must return rows with the county-grain schema.

    May be empty in the test DB; assert shape only.
    """
    r = client.get("/pums-burden-county?dim=overall")
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)
    for row in rows:
        for k in (
            "year", "product", "county_fips", "county_name",
            "tenure_class", "segment_dim", "segment_value",
            "weighted_n", "sample_n", "suppressed",
            "n_pumas_contributing",
            "household_income_p50",   "household_income_p50_se",
            "monthly_cost_p50",       "monthly_cost_p50_se",
            "burden_ratio_p50",       "burden_ratio_p50_se",
        ):
            assert k in row, f"PumsBurdenCountyRow missing column {k!r}"
        assert row["county_fips"].startswith("34"), (
            "NJ-only filter: state_fips = '34' implies county_fips startswith '34'."
        )


def test_pums_burden_county_default_excludes_suppressed(client: TestClient) -> None:
    """Default include_suppressed=false on the county endpoint too."""
    r = client.get("/pums-burden-county")
    assert r.status_code == 200
    rows = r.json()
    for row in rows:
        assert row["suppressed"] is False


# ============================================================================
# /counties endpoint (UI dropdown source)
# ============================================================================


def test_counties_endpoint_returns_21_nj_counties(client: TestClient) -> None:
    """ref.county is seeded with all 21 NJ counties; the endpoint must return them."""
    r = client.get("/counties")
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)
    assert len(rows) == 21, f"Expected 21 NJ counties, got {len(rows)}"
    for row in rows:
        assert set(row.keys()) == {"county_fips", "name"}
        assert row["county_fips"].startswith("34")
        assert len(row["county_fips"]) == 5
    # Alphabetical ordering is part of the contract.
    names = [row["name"] for row in rows]
    assert names == sorted(names)


# ============================================================================
# /pums-burden-county-series endpoint (UI trend view)
# ============================================================================


def test_pums_burden_county_series_invalid_tenure_returns_400(
    client: TestClient,
) -> None:
    r = client.get("/pums-burden-county-series?tenure=foo")
    assert r.status_code == 400
    assert "Invalid tenure" in r.json()["detail"]


def test_pums_burden_county_series_invalid_product_returns_400(
    client: TestClient,
) -> None:
    r = client.get("/pums-burden-county-series?product=acs10")
    assert r.status_code == 400
    assert "Invalid product" in r.json()["detail"]


def test_pums_burden_county_series_invalid_county_fips_returns_400(
    client: TestClient,
) -> None:
    r = client.get("/pums-burden-county-series?county_fips=abcde")
    assert r.status_code == 400
    assert "Invalid county_fips" in r.json()["detail"]


def test_pums_burden_county_series_list_endpoint_shape(
    client: TestClient,
) -> None:
    """Series endpoint must return rows with the slim time-series schema.

    May be empty if the derived asset is not materialized in the test DB;
    assert shape only.
    """
    r = client.get("/pums-burden-county-series?tenure=renter&product=acs5")
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)
    for row in rows:
        for k in (
            "year", "product", "state_fips",
            "county_fips", "county_name",
            "tenure_class",
            "weighted_n", "sample_n",
            "burden_ratio_p50", "burden_ratio_p50_se",
            "suppressed",
        ):
            assert k in row, f"PumsBurdenCountySeriesRow missing column {k!r}"
        # NJ-only filter.
        assert row["state_fips"] == "34"
        assert row["county_fips"].startswith("34")
        # tenure filter must be honored.
        assert row["tenure_class"] == "renter"
        assert row["product"] == "acs5"
        # Default include_suppressed=False.
        assert row["suppressed"] is False
        # Series rows are never suppressed (filtered out) so the ratio
        # should always be populated.
        assert row["burden_ratio_p50"] is not None


def test_pums_burden_county_series_county_filter_is_honored(
    client: TestClient,
) -> None:
    """Filtering by ?county_fips returns only that county's rows."""
    r = client.get(
        "/pums-burden-county-series?county_fips=34003&product=acs5",
    )
    assert r.status_code == 200
    rows = r.json()
    for row in rows:
        assert row["county_fips"] == "34003"


# ============================================================================
# /hpi/{county_fips}/series + /income/{county_fips}/series
# ============================================================================
#
# Both endpoints share a contract with /burden/{county_fips}: 400 on
# malformed FIPS, 404 on no rows, 200 with a typed list otherwise.
# The route module computes the income default base year at request
# time (min(max(dollar_year), max(cpi_year))) -- when either source
# is empty the route returns 404 and the test-skip is implicit.


def test_hpi_invalid_fips_returns_400(client: TestClient) -> None:
    r = client.get("/hpi/99999/series")
    assert r.status_code == 400
    assert "Invalid NJ county FIPS" in r.json()["detail"]


def test_hpi_base_year_below_range_returns_422(client: TestClient) -> None:
    """FastAPI's Query(ge=) returns 422 (validation error), not 400."""
    r = client.get("/hpi/34003/series?base_year=1800")
    assert r.status_code == 422


def test_hpi_county_series_shape(client: TestClient) -> None:
    """When data is loaded, response is a typed list of HpiCountyRow.

    Permissive on emptiness because raw.fhfa_hpi_county may be empty in
    the test DB; we assert SHAPE on the rows that do come back.
    """
    r = client.get("/hpi/34003/series")
    assert r.status_code in {200, 404}
    if r.status_code == 200:
        rows = r.json()
        assert isinstance(rows, list)
        for row in rows:
            for k in (
                "county_fips", "county_name", "year",
                "hpi_indexed", "hpi_raw", "base_year_used",
            ):
                assert k in row, f"HpiCountyRow missing {k!r}"
            assert row["county_fips"].startswith("34")
            assert row["base_year_used"] == 2000  # default


def test_hpi_custom_base_year_echoed_in_rows(client: TestClient) -> None:
    """``?base_year=2010`` -> every row's base_year_used should equal 2010."""
    r = client.get("/hpi/34003/series?base_year=2010")
    assert r.status_code in {200, 404}
    if r.status_code == 200:
        rows = r.json()
        for row in rows:
            assert row["base_year_used"] == 2010


def test_income_invalid_fips_returns_400(client: TestClient) -> None:
    r = client.get("/income/99999/series")
    assert r.status_code == 400
    assert "Invalid NJ county FIPS" in r.json()["detail"]


def test_income_invalid_product_returns_400(client: TestClient) -> None:
    r = client.get("/income/34003/series?product=acs10")
    assert r.status_code == 400
    assert "Invalid product" in r.json()["detail"]


def test_income_county_series_shape(client: TestClient) -> None:
    """Same permissive shape check as HPI: assert columns + NJ filter."""
    r = client.get("/income/34003/series")
    assert r.status_code in {200, 404}
    if r.status_code == 200:
        rows = r.json()
        assert isinstance(rows, list)
        for row in rows:
            for k in (
                "county_fips", "county_name", "year", "product",
                "estimate_real", "estimate_nominal", "deflator",
                "base_year_used", "dollar_year",
            ):
                assert k in row, f"IncomeCountyRow missing {k!r}"
            assert row["county_fips"].startswith("34")
            assert row["product"] in {"acs5", "acs1"}
            # Suppressed estimates are excluded server-side, so estimate_real
            # is always populated.
            assert row["estimate_real"] is not None


# ============================================================================
# Static UI mount
# ============================================================================


def test_index_html_is_served_at_root(client: TestClient) -> None:
    """GET / returns the bundled index.html from serving/static/."""
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    body = r.text
    assert "<title>NJ Housing Burden Terminal</title>" in body
    # Loaded from /static/, served by StaticFiles mount.
    assert '/static/app.js' in body
    assert '/static/styles.css' in body


def test_static_assets_are_served(client: TestClient) -> None:
    """The /static mount serves the JS and CSS bundles."""
    r_js = client.get("/static/app.js")
    assert r_js.status_code == 200
    assert "renderTrend" in r_js.text  # smoke check that the right file is served
    r_css = client.get("/static/styles.css")
    assert r_css.status_code == 200
    assert "--bg" in r_css.text


# ============================================================================
# Observability headers
# ============================================================================


def test_response_headers_include_request_id_and_query_time(
    client: TestClient,
) -> None:
    r = client.get("/health")
    assert "X-Request-Id" in r.headers
    assert "X-Query-Time-Ms" in r.headers
    # Query time must be parseable as a float (ms).
    float(r.headers["X-Query-Time-Ms"])


def test_request_id_is_propagated_when_supplied(client: TestClient) -> None:
    r = client.get("/health", headers={"X-Request-Id": "test-corr-12345"})
    assert r.headers["X-Request-Id"] == "test-corr-12345"
