"""Unit tests for ``ingestion.hud_zip_county``.

These exercise the parser/validator path. The Postgres COPY path is
covered by ``test_pg_integration.py`` (live_pg).
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from ingestion._base import IngestError
from ingestion.hud_zip_county import (
    parse_hud_file,
    parse_vintage_from_filename,
    stage_dataframe,
)

# ---------------------------------------------------------------------------
# Vintage extraction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("ZIP_COUNTY_032025.xlsx", (2025, 1)),
        ("ZIP_COUNTY_062021.csv",  (2021, 2)),
        ("ZIP_COUNTY_092019.xlsx", (2019, 3)),
        ("ZIP_COUNTY_122024.xlsx", (2024, 4)),
        ("ZIP_COUNTY_022019.xlsx", (2019, 1)),
        ("ZIP_COUNTY_052020.xlsx", (2020, 2)),
    ],
)
def test_parse_vintage_from_filename(filename: str, expected: tuple[int, int]) -> None:
    assert parse_vintage_from_filename(filename) == expected


def test_parse_vintage_unparseable_raises() -> None:
    with pytest.raises(IngestError, match="Cannot extract HUD vintage"):
        parse_vintage_from_filename("crosswalk.xlsx")


def test_parse_vintage_invalid_month_raises() -> None:
    with pytest.raises(IngestError, match="Invalid month"):
        parse_vintage_from_filename("ZIP_COUNTY_132024.xlsx")


# ---------------------------------------------------------------------------
# Fixtures: synthetic HUD CSVs with the correct ratio-sum invariant
# ---------------------------------------------------------------------------


@pytest.fixture
def hud_complete_csv(tmp_path: Path) -> Path:
    """A 5-row HUD CSV where two ZIPs cleanly nest in one county each.

    All three ZIPs satisfy the ratio-sum invariant exactly.
    """
    path = tmp_path / "ZIP_COUNTY_032024.csv"
    path.write_text(
        "ZIP,COUNTY,RES_RATIO,BUS_RATIO,OTH_RATIO,TOT_RATIO\n"
        # 08830 (Iselin, NJ) -- entirely in NJ-MIDDLESEX (34023)
        "08830,34023,1.000000,1.000000,1.000000,1.000000\n"
        # 07102 (Newark, NJ) -- entirely in NJ-ESSEX (34013)
        "07102,34013,1.000000,1.000000,1.000000,1.000000\n"
        # 08901 (New Brunswick, NJ) -- splits between NJ-MIDDLESEX (0.7)
        # and NJ-SOMERSET (0.3) for business addresses; 0.6/0.4 residential.
        "08901,34023,0.600000,0.700000,0.700000,0.650000\n"
        "08901,34035,0.400000,0.300000,0.300000,0.350000\n"
        # 99999 (synthetic non-existent) -- valid format but ZIP doesn't exist
        # in real life. Used to test that the parser does not assume a
        # given ZIP appears in NJ.
        "99999,99999,1.000000,1.000000,1.000000,1.000000\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def hud_lost_leading_zero_csv(tmp_path: Path) -> Path:
    """A HUD CSV where Excel ate the leading zero on ZIPs.

    The parser must zero-pad both the ZIP and the COUNTY columns back to
    5 digits. This is the most common real-world data-quality issue.
    """
    path = tmp_path / "ZIP_COUNTY_032018.csv"
    path.write_text(
        "ZIP,COUNTY,RES_RATIO,BUS_RATIO,OTH_RATIO,TOT_RATIO\n"
        "8830,34023,1.0,1.0,1.0,1.0\n"
        "7102,34013,1.0,1.0,1.0,1.0\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def hud_invariant_violation_csv(tmp_path: Path) -> Path:
    """A HUD CSV where 08901's bus_ratio sums to 0.5 -- invariant violation."""
    path = tmp_path / "ZIP_COUNTY_032023.csv"
    path.write_text(
        "ZIP,COUNTY,RES_RATIO,BUS_RATIO,OTH_RATIO,TOT_RATIO\n"
        "08901,34023,0.4,0.3,0.3,0.35\n"
        "08901,34035,0.4,0.2,0.3,0.35\n"
        # Note: bus_ratio sums to 0.5, NOT 1.0.
        "08830,34023,1.0,1.0,1.0,1.0\n",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parse_hud_complete(hud_complete_csv: Path) -> None:
    result = parse_hud_file(hud_complete_csv)
    assert result.vintage_year == 2024
    assert result.vintage_quarter == 1
    assert result.n_input_rows == 5
    assert result.n_output_rows == 5
    df = result.dataframe
    assert set(df.columns) == {
        "zip5", "county_fips", "res_ratio", "bus_ratio", "oth_ratio", "tot_ratio",
    }
    assert (df["zip5"].str.len_chars() == 5).all()
    assert (df["county_fips"].str.len_chars() == 5).all()


def test_parse_hud_zero_pads_zip_and_county(hud_lost_leading_zero_csv: Path) -> None:
    """Excel-mangled ZIPs (lost leading zero) must come back as CHAR(5)."""
    result = parse_hud_file(hud_lost_leading_zero_csv)
    zips = sorted(result.dataframe["zip5"].to_list())
    assert zips == ["07102", "08830"]


def test_parse_hud_invariant_violation_raises(hud_invariant_violation_csv: Path) -> None:
    """ZIP 08901 has bus_ratio summing to 0.5; the parser must reject."""
    with pytest.raises(IngestError, match="ratio-sum invariant violated"):
        parse_hud_file(hud_invariant_violation_csv)


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------


def test_stage_adds_provenance_columns(hud_complete_csv: Path) -> None:
    parsed = parse_hud_file(hud_complete_csv)
    staged = stage_dataframe(parsed, source_url="https://example.test/")
    expected_cols = (
        "zip5", "county_fips", "vintage_year", "vintage_quarter",
        "res_ratio", "bus_ratio", "oth_ratio", "tot_ratio",
        "source_url", "source_sha256",
    )
    assert tuple(staged.columns) == expected_cols
    assert (staged["vintage_year"] == 2024).all()
    assert (staged["vintage_quarter"] == 1).all()
    assert (staged["source_url"] == "https://example.test/").all()
    assert (staged["source_sha256"].str.len_chars() == 64).all()


def test_stage_preserves_row_count(hud_complete_csv: Path) -> None:
    parsed = parse_hud_file(hud_complete_csv)
    staged = stage_dataframe(parsed, source_url="https://example.test/")
    assert staged.height == parsed.n_output_rows


# ---------------------------------------------------------------------------
# Polars schema sanity
# ---------------------------------------------------------------------------


def test_dataframe_has_expected_dtypes(hud_complete_csv: Path) -> None:
    df = parse_hud_file(hud_complete_csv).dataframe
    assert df.schema["zip5"] == pl.Utf8
    assert df.schema["county_fips"] == pl.Utf8
    for col in ("res_ratio", "bus_ratio", "oth_ratio", "tot_ratio"):
        assert df.schema[col] == pl.Float64
