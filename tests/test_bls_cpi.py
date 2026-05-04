"""Unit tests for the BLS CPI-U ingester.

These tests exercise the parsing/staging logic with synthetic API
responses; live API tests are guarded by ``pytest.mark.live_pg`` in
``test_pg_integration.py``.
"""

from __future__ import annotations

from typing import Any

import polars as pl
import pytest

from ingestion._base import IngestError
from ingestion.bls_cpi import (
    BLS_API_URL,
    CANONICAL_SERIES,
    FetchResult,
    fetch_cpi_series,
    stage_dataframe,
)

# A realistic mock BLS response with two months for two series.
_MOCK_RESPONSE: dict[str, Any] = {
    "status": "REQUEST_SUCCEEDED",
    "responseTime": 0,
    "message": [],
    "Results": {
        "series": [
            {
                "seriesID": "CUUR0000SA0",
                "data": [
                    {"year": "2024", "period": "M02", "periodName": "February",
                     "value": "310.326", "footnotes": [{}]},
                    {"year": "2024", "period": "M01", "periodName": "January",
                     "value": "308.417", "footnotes": [{}]},
                    {"year": "2023", "period": "M13", "periodName": "Annual",
                     "value": "304.702", "footnotes": [{}]},
                ],
            },
            {
                "seriesID": "CUUR0000SAH",
                "data": [
                    {"year": "2024", "period": "M02", "periodName": "February",
                     "value": "377.123", "footnotes": [{}]},
                    {"year": "2024", "period": "M01", "periodName": "January",
                     "value": "376.500", "footnotes": [{}]},
                ],
            },
        ],
    },
}


def test_canonical_series_is_fixed_set() -> None:
    """Adding/removing a canonical series is a deliberate design change."""
    assert "CUUR0000SA0" in CANONICAL_SERIES
    assert "CUUR0000SAH" in CANONICAL_SERIES
    assert len(CANONICAL_SERIES) == 6


def test_fetch_with_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_cpi_series parses BLS responses into a typed DataFrame."""
    import json

    import httpx

    captured: dict[str, Any] = {}

    class MockResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, Any]:
            return _MOCK_RESPONSE

    class MockClient:
        def __init__(self, *_a: object, **_k: object) -> None:
            pass

        def __enter__(self) -> MockClient:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def post(
            self, url: str, *, content: bytes, headers: dict[str, str],
        ) -> MockResponse:
            captured["url"] = url
            captured["payload"] = json.loads(content)
            return MockResponse()

    monkeypatch.setattr(httpx, "Client", MockClient)

    result = fetch_cpi_series(
        ["CUUR0000SA0", "CUUR0000SAH"],
        start_year=2023, end_year=2024,
    )

    assert captured["url"] == BLS_API_URL
    assert captured["payload"]["seriesid"] == ["CUUR0000SA0", "CUUR0000SAH"]
    assert captured["payload"]["startyear"] == "2023"
    assert captured["payload"]["endyear"] == "2024"
    assert "registrationkey" not in captured["payload"]

    df = result.dataframe
    assert df.height == 5
    assert set(df.columns) == {"series_id", "year", "period", "value"}
    assert df.schema["value"] == pl.Float64

    sa0_2023_m13 = df.filter(
        (pl.col("series_id") == "CUUR0000SA0")
        & (pl.col("year") == 2023)
        & (pl.col("period") == "M13"),
    )["value"].item()
    assert sa0_2023_m13 == pytest.approx(304.702)


def test_fetch_with_api_key_includes_registrationkey(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing api_key adds it to the POST body, not the URL."""
    import json

    import httpx

    captured: dict[str, Any] = {}

    class MockResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, Any]:
            return _MOCK_RESPONSE

    class MockClient:
        def __init__(self, *_a: object, **_k: object) -> None:
            pass

        def __enter__(self) -> MockClient:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def post(
            self, url: str, *, content: bytes, headers: dict[str, str],
        ) -> MockResponse:
            captured["payload"] = json.loads(content)
            return MockResponse()

    monkeypatch.setattr(httpx, "Client", MockClient)
    fetch_cpi_series(
        ["CUUR0000SA0"],
        start_year=2024, end_year=2024,
        api_key="fake-key",
    )
    assert captured["payload"]["registrationkey"] == "fake-key"


def test_fetch_chunks_year_span_when_unauth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unauthenticated callers get split into 10-year chunks automatically."""
    import json

    import httpx

    calls: list[dict[str, Any]] = []

    class MockResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, Any]:
            return _MOCK_RESPONSE

    class MockClient:
        def __init__(self, *_a: object, **_k: object) -> None:
            pass

        def __enter__(self) -> MockClient:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def post(
            self, url: str, *, content: bytes, headers: dict[str, str],
        ) -> MockResponse:
            calls.append(json.loads(content))
            return MockResponse()

    monkeypatch.setattr(httpx, "Client", MockClient)
    fetch_cpi_series(
        ["CUUR0000SA0"],
        start_year=2000, end_year=2024,  # 25 years -> 3 chunks of 10/10/5
    )
    spans = [(int(c["startyear"]), int(c["endyear"])) for c in calls]
    assert spans == [(2000, 2009), (2010, 2019), (2020, 2024)]


def test_fetch_rejects_inverted_year_range() -> None:
    with pytest.raises(IngestError, match="must be <="):
        fetch_cpi_series(["CUUR0000SA0"], start_year=2024, end_year=2020)


def test_fetch_rejects_empty_series_list() -> None:
    with pytest.raises(IngestError, match="series_ids must be non-empty"):
        fetch_cpi_series([], start_year=2020, end_year=2024)


def test_stage_dataframe_adds_provenance() -> None:
    """stage_dataframe surfaces source_url / source_sha256 as columns."""
    df = pl.DataFrame({
        "series_id": ["CUUR0000SA0"],
        "year":      [2024],
        "period":    ["M01"],
        "value":     [308.417],
    })
    result = FetchResult(
        dataframe=df, source_url="http://example/api", source_sha256="abc" * 21 + "abcd",
        series_ids=("CUUR0000SA0",), start_year=2024, end_year=2024,
        n_observations=1,
    )
    staged = stage_dataframe(result)
    assert "source_url" in staged.columns
    assert "source_sha256" in staged.columns
    assert staged["source_url"][0] == "http://example/api"
    assert staged["source_sha256"][0] == "abc" * 21 + "abcd"
