"""Unit tests for the ACS housing ingester."""

from __future__ import annotations

from typing import Any

import polars as pl
import pytest

from ingestion._base import IngestError
from ingestion.census_acs_housing import (
    CANONICAL_HOUSING_VARS,
    _build_url,
    fetch_acs_housing_year,
    stage_dataframe,
)
from ingestion.census_acs_income import VintageNotPublishedError

# ============================================================================
# Variable catalog + URL construction
# ============================================================================


def test_canonical_housing_vars_includes_core_burden_inputs() -> None:
    """Burden ratio depends on rent, owner cost, tenure mix; all must be in catalog."""
    assert "B25064_001" in CANONICAL_HOUSING_VARS  # median gross rent
    assert "B25077_001" in CANONICAL_HOUSING_VARS  # median home value
    assert "B25088_002" in CANONICAL_HOUSING_VARS  # owner cost w/ mortgage
    assert "B25003_001" in CANONICAL_HOUSING_VARS  # total occupied units


def test_build_url_includes_E_and_M_for_each_variable() -> None:
    """Census API needs both estimate (_E) and margin (_M) per variable."""
    url = _build_url(2022, "acs5", "34", ["B25064_001", "B25077_001"], api_key=None)
    assert "B25064_001E" in url
    assert "B25064_001M" in url
    assert "B25077_001E" in url
    assert "B25077_001M" in url


def test_build_url_rejects_empty_variable_list() -> None:
    with pytest.raises(IngestError, match="variable_ids must be non-empty"):
        _build_url(2022, "acs5", "34", [], api_key=None)


# ============================================================================
# Fetch with mocked HTTP
# ============================================================================


_MOCK_PAYLOAD: list[list[Any]] = [
    ["NAME",
     "B25064_001E", "B25064_001M",
     "B25077_001E", "B25077_001M",
     "state", "county"],
    ["Bergen County, NJ",   "1850", "25", "550000", "8000", "34", "003"],
    ["Atlantic County, NJ", "1280", "30", "240000", "6000", "34", "001"],
    # Suppression sentinel for the value, valid MOE.
    ["Suppressed County",   "-666666666", "100", "350000", "5000", "34", "999"],
]


def test_fetch_parses_long_skinny_one_row_per_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json as _json

    import httpx

    class MockResponse:
        status_code = 200
        is_success = True
        content = _json.dumps(_MOCK_PAYLOAD).encode("utf-8")
        request = None

        def raise_for_status(self) -> None:
            pass

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
    result = fetch_acs_housing_year(
        year=2022, product="acs5",
        variable_ids=["B25064_001", "B25077_001"],
    )

    df = result.dataframe
    # 3 counties x 2 variables = 6 rows.
    assert df.height == 6
    assert set(df.columns) >= {
        "county_fips", "year", "product", "variable_id",
        "estimate", "margin_of_error", "dollar_year", "suppression_code",
    }

    # Bergen has both variables present, no suppression.
    bergen_rent = df.filter(
        (pl.col("county_fips") == "34003")
        & (pl.col("variable_id") == "B25064_001")
    )
    assert bergen_rent["estimate"].item() == pytest.approx(1850.0)
    assert bergen_rent["margin_of_error"].item() == pytest.approx(25.0)
    assert bergen_rent["suppression_code"].item() is None

    # Suppressed value yields (None, "confidentiality") even though MOE is set.
    suppressed_value = df.filter(
        (pl.col("county_fips") == "34999")
        & (pl.col("variable_id") == "B25064_001")
    )
    assert suppressed_value["estimate"].item() is None
    assert suppressed_value["suppression_code"].item() == "confidentiality"


def test_fetch_raises_when_variable_missing_from_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the response is missing an expected _E or _M column, raise IngestError."""
    import json as _json

    import httpx
    payload_missing_b25077 = [
        ["NAME", "B25064_001E", "B25064_001M", "state", "county"],
        ["Bergen County, NJ", "1850", "25", "34", "003"],
    ]

    class MockResponse:
        status_code = 200
        is_success = True
        content = _json.dumps(payload_missing_b25077).encode("utf-8")
        request = None

        def raise_for_status(self) -> None:
            pass

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
    with pytest.raises(IngestError, match="missing expected variable B25077"):
        fetch_acs_housing_year(
            year=2022, product="acs5",
            variable_ids=["B25064_001", "B25077_001"],
        )


def test_fetch_raises_vintage_not_published_on_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Census 404 -> VintageNotPublishedError with helpful message."""
    import httpx

    class MockResponse:
        status_code = 404
        is_success = False
        content = b""
        request = None

        def raise_for_status(self) -> None:
            raise httpx.HTTPStatusError("404", request=None, response=self)  # type: ignore[arg-type]

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
        fetch_acs_housing_year(year=2020, product="acs1")


def test_stage_dataframe_includes_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    import json as _json

    import httpx

    class MockResponse:
        status_code = 200
        is_success = True
        content = _json.dumps(_MOCK_PAYLOAD).encode("utf-8")
        request = None

        def raise_for_status(self) -> None:
            pass

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
    result = fetch_acs_housing_year(
        year=2022, product="acs5",
        variable_ids=["B25064_001", "B25077_001"],
    )
    staged = stage_dataframe(result)
    assert "source_url" in staged.columns
    assert "source_sha256" in staged.columns
    # API key never makes it into the provenance URL.
    assert "key=" not in staged["source_url"][0]
