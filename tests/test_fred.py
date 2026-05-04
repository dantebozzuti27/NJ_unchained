"""Unit tests for the FRED rate-series ingester."""

from __future__ import annotations

from datetime import date
from typing import Any

import polars as pl
import pytest

from ingestion._base import IngestError
from ingestion.fred_mortgage_rates import (
    CANONICAL_SERIES,
    FetchResult,
    _build_url,
    fetch_fred_series,
    stage_dataframe,
)

# ============================================================================
# URL builder
# ============================================================================


def test_build_url_canonical_form() -> None:
    url = _build_url("MORTGAGE30US", date(2020, 1, 1), date(2024, 12, 31))
    assert url == (
        "https://fred.stlouisfed.org/graph/fredgraph.csv"
        "?id=MORTGAGE30US&cosd=2020-01-01&coed=2024-12-31"
    )


@pytest.mark.parametrize("bad", ["", "with space", "lower-case", "@@bad"])
def test_build_url_rejects_invalid_series_id(bad: str) -> None:
    with pytest.raises(IngestError, match="Invalid FRED series_id"):
        _build_url(bad, date(2020, 1, 1), date(2024, 12, 31))


def test_build_url_rejects_inverted_dates() -> None:
    with pytest.raises(IngestError, match="must be <="):
        _build_url("MORTGAGE30US", date(2024, 12, 31), date(2020, 1, 1))


def test_canonical_series_is_fixed_set() -> None:
    """Adding/removing canonical series is a deliberate design change."""
    assert "MORTGAGE30US" in CANONICAL_SERIES
    assert "DGS10" in CANONICAL_SERIES
    assert "FEDFUNDS" in CANONICAL_SERIES
    assert len(CANONICAL_SERIES) == 3


# ============================================================================
# CSV parsing with mocked HTTP
# ============================================================================


_MOCK_FRED_CSV = (
    b"observation_date,MORTGAGE30US\n"
    b"2024-01-04,6.62\n"
    b"2024-01-11,6.66\n"
    b"2024-01-18,.\n"            # FRED's missing-value sentinel
    b"2024-01-25,6.69\n"
)


def test_fetch_parses_csv_with_missing_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_fred_series treats '.' as NULL and types value as Float64."""
    import httpx

    class MockResponse:
        status_code = 200
        content = _MOCK_FRED_CSV
        is_success = True

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
    result = fetch_fred_series(
        "MORTGAGE30US",
        start=date(2024, 1, 1), end=date(2024, 1, 31),
    )
    df = result.dataframe
    assert df.height == 4
    assert df["series_id"].unique().to_list() == ["MORTGAGE30US"]

    # The "." row should be NULL.
    null_row = df.filter(pl.col("observation_date") == date(2024, 1, 18))
    assert null_row["value"].item() is None

    valid_row = df.filter(pl.col("observation_date") == date(2024, 1, 11))
    assert valid_row["value"].item() == pytest.approx(6.66)


def test_fetch_raises_on_unexpected_csv_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """If FRED returns a CSV without our value column, raise IngestError."""
    import httpx

    class MockResponse:
        status_code = 200
        content = b"foo,bar\n1,2\n"
        is_success = True

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
    with pytest.raises(IngestError, match="missing observation_date"):
        fetch_fred_series(
            "MORTGAGE30US",
            start=date(2024, 1, 1), end=date(2024, 1, 31),
        )


def test_fetch_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 503 then a 200 succeeds; tests the retry-with-backoff path."""
    import httpx

    from ingestion import fred_mortgage_rates as mod

    monkeypatch.setattr(mod, "_RETRY_BASE_BACKOFF_S", 0.0)  # speed up

    calls: list[Any] = []

    class FlakyResponse:
        is_success: bool

        def __init__(self, status_code: int) -> None:
            self.status_code = status_code
            self.is_success = 200 <= status_code < 300
            self.content = _MOCK_FRED_CSV if self.is_success else b""
            self.request = None

        def raise_for_status(self) -> None:
            if not self.is_success:
                raise httpx.HTTPStatusError(
                    f"{self.status_code}", request=None, response=self,  # type: ignore[arg-type]
                )

    class FlakyClient:
        def __init__(self, *_a: object, **_k: object) -> None:
            pass

        def __enter__(self) -> FlakyClient:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def get(self, url: str) -> FlakyResponse:
            calls.append(url)
            # First two calls fail with 503, third succeeds.
            if len(calls) <= 2:
                return FlakyResponse(503)
            return FlakyResponse(200)

    monkeypatch.setattr(httpx, "Client", FlakyClient)
    result = fetch_fred_series(
        "MORTGAGE30US",
        start=date(2024, 1, 1), end=date(2024, 1, 31),
    )
    assert len(calls) == 3
    assert result.dataframe.height == 4


def test_stage_dataframe_includes_provenance() -> None:
    df = pl.DataFrame({
        "series_id":        ["MORTGAGE30US"],
        "observation_date": [date(2024, 1, 4)],
        "value":            [6.62],
    }, schema={
        "series_id":        pl.Utf8,
        "observation_date": pl.Date,
        "value":            pl.Float64,
    })
    result = FetchResult(
        dataframe=df, source_url="http://example/fred", source_sha256="0" * 64,
        series_id="MORTGAGE30US",
        start_date=date(2024, 1, 1), end_date=date(2024, 12, 31),
        n_observations=1,
    )
    staged = stage_dataframe(result)
    assert "source_url" in staged.columns
    assert "source_sha256" in staged.columns
    assert staged["source_url"][0] == "http://example/fred"
