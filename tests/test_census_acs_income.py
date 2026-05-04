"""Unit tests for the Census ACS B19013 ingester."""

from __future__ import annotations

from typing import Any

import polars as pl
import pytest

from ingestion._base import IngestError
from ingestion.census_acs_income import (
    NJ_STATE_FIPS,
    PRODUCT_START_YEAR,
    SUPPRESSION_SENTINELS,
    VintageNotPublishedError,
    _build_url,
    _coerce_value,
    fetch_acs_b19013_year,
    stage_dataframe,
)

# ============================================================================
# URL construction
# ============================================================================


def test_build_url_modern() -> None:
    url = _build_url(2022, "acs5", NJ_STATE_FIPS, api_key=None)
    assert url == (
        "https://api.census.gov/data/2022/acs/acs5"
        "?get=NAME,B19013_001E,B19013_001M&for=county:*&in=state:34"
    )


def test_build_url_includes_api_key_when_given() -> None:
    url = _build_url(2022, "acs5", NJ_STATE_FIPS, api_key="abc123")
    assert url.endswith("&key=abc123")


def test_build_url_rejects_unknown_product() -> None:
    with pytest.raises(IngestError, match="Unknown product"):
        _build_url(2022, "acs10", NJ_STATE_FIPS, api_key=None)


@pytest.mark.parametrize("product, year, ok", [
    ("acs5", 2009, True),
    ("acs5", 2008, False),     # acs5 didn't exist before 2009
    ("acs1", 2005, True),
    ("acs1", 2004, False),
])
def test_build_url_rejects_pre_release_years(
    product: str, year: int, ok: bool,
) -> None:
    if ok:
        _build_url(year, product, NJ_STATE_FIPS, api_key=None)
    else:
        with pytest.raises(IngestError, match="did not exist before"):
            _build_url(year, product, NJ_STATE_FIPS, api_key=None)


def test_product_start_years_are_canonical() -> None:
    assert PRODUCT_START_YEAR == {"acs1": 2005, "acs5": 2009}


# ============================================================================
# Value coercion + suppression sentinels
# ============================================================================


def test_coerce_value_normal() -> None:
    est, supp = _coerce_value("89703")
    assert est == 89703.0
    assert supp is None


def test_coerce_value_float_string() -> None:
    est, supp = _coerce_value("89703.5")
    assert est == pytest.approx(89703.5)
    assert supp is None


@pytest.mark.parametrize("sentinel, expected", [
    (-666666666, "confidentiality"),
    (-222222222, "too_small"),
    (-333333333, "other"),
])
def test_coerce_value_suppression_sentinels(sentinel: int, expected: str) -> None:
    """Suppression sentinels yield (None, code) so the CHECK constraint passes."""
    assert sentinel in SUPPRESSION_SENTINELS
    est, supp = _coerce_value(sentinel)
    assert est is None
    assert supp == expected


def test_coerce_value_null_is_other_suppression() -> None:
    est, supp = _coerce_value(None)
    assert est is None
    assert supp == "other"


def test_coerce_value_unparseable_is_other() -> None:
    est, supp = _coerce_value("not-a-number")
    assert est is None
    assert supp == "other"


def test_coerce_value_zero_treated_as_suppressed() -> None:
    """Census occasionally emits 0 instead of using a sentinel; we suppress it."""
    est, supp = _coerce_value(0)
    assert est is None
    assert supp == "other"


# ============================================================================
# Fetch with mock HTTP (no live API call)
# ============================================================================


_MOCK_ACS_PAYLOAD: list[list[Any]] = [
    ["NAME", "B19013_001E", "B19013_001M", "state", "county"],
    ["Atlantic County, New Jersey",   "73113",  "1917", "34", "001"],
    ["Bergen County, New Jersey",     "118714", "1607", "34", "003"],
    ["Suppressed County, New Jersey", "-666666666", "-666666666", "34", "999"],
]


def test_fetch_parses_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_acs_b19013_year reshapes the array-of-arrays into our schema."""
    import httpx

    class MockResponse:
        status_code = 200
        is_success = True
        content = b'[["NAME","B19013_001E","B19013_001M","state","county"],' \
                  b'["Atlantic County, New Jersey","73113","1917","34","001"],' \
                  b'["Bergen County, New Jersey","118714","1607","34","003"],' \
                  b'["Suppressed County, New Jersey","-666666666","-666666666","34","999"]]'
        request = None

        def raise_for_status(self) -> None:
            pass

        def json(self) -> list[list[Any]]:
            return _MOCK_ACS_PAYLOAD

    class MockClient:
        def __init__(self, *_a: object, **_k: object) -> None:
            pass

        def __enter__(self) -> MockClient:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def get(self, url: str) -> MockResponse:
            return MockResponse()

    monkeypatch.setattr(httpx, "Client", MockClient)
    result = fetch_acs_b19013_year(year=2022, product="acs5")

    assert result.year == 2022
    assert result.product == "acs5"
    assert result.state_fips == "34"
    assert result.n_rows == 3

    df = result.dataframe
    bergen = df.filter(pl.col("county_fips") == "34003")
    assert bergen.height == 1
    assert bergen["estimate"].item() == pytest.approx(118714.0)
    assert bergen["margin_of_error"].item() == pytest.approx(1607.0)
    assert bergen["dollar_year"].item() == 2022
    assert bergen["suppression_code"].item() is None

    suppressed = df.filter(pl.col("county_fips") == "34999")
    assert suppressed["estimate"].item() is None
    assert suppressed["suppression_code"].item() == "confidentiality"


def test_fetch_raises_vintage_not_published_on_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Census 404 -> VintageNotPublishedError, not a generic HTTPStatusError."""
    import httpx

    class MockResponse:
        status_code = 404
        is_success = False
        content = b""
        request = None

        def raise_for_status(self) -> None:
            raise httpx.HTTPStatusError("404", request=None, response=None)  # type: ignore[arg-type]

    class MockClient:
        def __init__(self, *_a: object, **_k: object) -> None:
            pass

        def __enter__(self) -> MockClient:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def get(self, url: str) -> MockResponse:
            return MockResponse()

    monkeypatch.setattr(httpx, "Client", MockClient)
    with pytest.raises(VintageNotPublishedError, match="404"):
        fetch_acs_b19013_year(year=2020, product="acs1")


def test_stage_dataframe_adds_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    """stage_dataframe surfaces source_url + source_sha256."""
    import httpx

    class MockResponse:
        status_code = 200
        is_success = True
        content = b'[["NAME","B19013_001E","B19013_001M","state","county"],' \
                  b'["Atlantic County, New Jersey","73113","1917","34","001"],' \
                  b'["Bergen County, New Jersey","118714","1607","34","003"]]'
        request = None

        def raise_for_status(self) -> None:
            pass

        def json(self) -> list[list[Any]]:
            return _MOCK_ACS_PAYLOAD

    class MockClient:
        def __init__(self, *_a: object, **_k: object) -> None:
            pass

        def __enter__(self) -> MockClient:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def get(self, url: str) -> MockResponse:
            return MockResponse()

    monkeypatch.setattr(httpx, "Client", MockClient)
    result = fetch_acs_b19013_year(year=2022, product="acs5")
    staged = stage_dataframe(result)
    assert "source_url" in staged.columns
    assert "source_sha256" in staged.columns
    # Provenance hash uses the URL WITHOUT the API key, so it's stable
    # across keyed and unkeyed callers.
    assert "key=" not in staged["source_url"][0]
