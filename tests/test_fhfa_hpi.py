"""Unit tests for the FHFA county HPI ingester."""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from ingestion._base import IngestError
from ingestion.fhfa_hpi import (
    _canonicalize_columns,
    _detect_header_row,
    parse_fhfa_county_workbook,
    stage_dataframe,
)

# ============================================================================
# Header-row detection
# ============================================================================


def test_detect_header_row_finds_year_fips_row() -> None:
    """Detector finds the row with EXACT 'Year' and a FIPS-bearing cell."""
    raw = pl.DataFrame({
        "c0": ["a long paragraph mentioning year and FIPS within prose",
               "Last updated: 2025",
               "Not Seasonally Adjusted",
               "State"],
        "c1": [None, None, None, "County"],
        "c2": [None, None, None, "FIPS code"],
        "c3": [None, None, None, "Year"],
        "c4": [None, None, None, "HPI"],
    })
    assert _detect_header_row(raw) == 3


def test_detect_header_row_rejects_paragraph_with_year_substring() -> None:
    """A row that has 'year' as a substring of prose must not match."""
    raw = pl.DataFrame({
        "c0": ["The annual change in the index is computed each year",
               "Some other note",
               "fips_code"],
        "c1": [None, None, "year"],
    })
    # Only row 2 has both 'year' (exact) and 'fips' (cell-level, len<=30).
    assert _detect_header_row(raw) == 2


def test_detect_header_row_raises_when_no_match() -> None:
    raw = pl.DataFrame({
        "c0": ["intro", "more intro", "still intro"],
    })
    with pytest.raises(IngestError, match="Could not locate header row"):
        _detect_header_row(raw)


# ============================================================================
# Column canonicalization
# ============================================================================


def test_canonicalize_columns_maps_known_headers() -> None:
    out = _canonicalize_columns([
        "State", "County", "FIPS code", "Year",
        "Annual Change (%)", "HPI",
    ])
    assert out == ["state", "county", "fips_code", "year",
                   "annual_change_pct", "hpi"]


def test_canonicalize_columns_handles_none_with_positional_names() -> None:
    """None header cells become _unused_N rather than colliding."""
    out = _canonicalize_columns(["A", None, "B", None])
    assert out == ["a", "_unused_1", "b", "_unused_3"]


def test_canonicalize_columns_handles_empty_strings() -> None:
    out = _canonicalize_columns(["State", "", "  ", "Year"])
    assert out[0] == "state"
    assert out[1].startswith("_unused_")
    assert out[2].startswith("_unused_")
    assert out[3] == "year"


# ============================================================================
# Parse a synthetic workbook
# ============================================================================


def test_parse_fhfa_workbook_smoke(tmp_path: Path) -> None:
    """Parser produces a typed DataFrame from a 2-county minimal Excel file."""
    pytest.importorskip("xlsxwriter")
    import xlsxwriter

    path = tmp_path / "hpi_at_county.xlsx"
    wb = xlsxwriter.Workbook(str(path))
    ws = wb.add_worksheet()
    # Two metadata rows then a header row.
    ws.write(0, 0, "Top of file metadata blob mentioning year and fips inside a paragraph.")
    ws.write(1, 0, "Last updated: 2025-01-01")
    headers = ["State", "County", "FIPS code", "Year", "Annual Change (%)", "HPI"]
    for j, h in enumerate(headers):
        ws.write(2, j, h)
    # Two counties, two years each.
    rows = [
        ["NJ", "Bergen",   "34003", 2010, "",     "100.0"],
        ["NJ", "Bergen",   "34003", 2011, "1.50", "101.5"],
        ["NJ", "Cape May", "34009", 2010, "",     "100.0"],
        ["NJ", "Cape May", "34009", 2011, "5.00", "105.0"],
    ]
    for i, r in enumerate(rows):
        for j, v in enumerate(r):
            ws.write(i + 3, j, v)
    wb.close()

    result = parse_fhfa_county_workbook(path)
    assert result.n_rows == 4
    assert set(result.dataframe["county_fips"].to_list()) == {"34003", "34009"}
    bergen_2011 = result.dataframe.filter(
        (pl.col("county_fips") == "34003") & (pl.col("year") == 2011)
    )
    assert bergen_2011["hpi_at"].item() == pytest.approx(101.5)
    assert bergen_2011["annual_change"].item() == pytest.approx(1.5)


def test_parse_fhfa_workbook_drops_invalid_rows(tmp_path: Path) -> None:
    """Rows with missing/invalid year/fips/hpi are silently dropped.

    Mirrors the real FHFA structure: a metadata prose row, then a header
    row with cell-level 'Year' and 'FIPS'-bearing labels, then data.
    """
    pytest.importorskip("xlsxwriter")
    import xlsxwriter

    path = tmp_path / "hpi_at_county.xlsx"
    wb = xlsxwriter.Workbook(str(path))
    ws = wb.add_worksheet()
    # Metadata blob (single cell, spanning row 0). Real FHFA workbooks
    # have several of these; one is enough to exercise the detector.
    ws.write(0, 0, "Top of file metadata describing year and FIPS in prose.")
    headers = ["State", "County", "FIPS code", "Year", "Annual Change (%)", "HPI"]
    for j, h in enumerate(headers):
        ws.write(1, j, h)
    rows = [
        ["NJ", "Bergen", "34003", 2010, "", "100.0"],   # OK
        ["NJ", "Bad",   "",       2010, "", "100.0"],   # missing fips
        ["NJ", "Bad",   "34003",  None, "", "100.0"],   # missing year
        ["NJ", "Bad",   "34003",  2010, "", ""],        # missing hpi
    ]
    for i, r in enumerate(rows):
        for j, v in enumerate(r):
            if v is not None:
                ws.write(i + 2, j, v)
    wb.close()

    result = parse_fhfa_county_workbook(path)
    assert result.n_rows == 1
    assert result.dataframe["county_fips"][0] == "34003"


def test_stage_dataframe_includes_provenance() -> None:
    """stage_dataframe attaches source_url / source_sha256 / source_vintage."""
    from ingestion.fhfa_hpi import ParseResult

    df = pl.DataFrame({
        "county_fips":    ["34003"],
        "year":           [2024],
        "hpi_at":         [180.0],
        "annual_change":  [3.0],
        "n_transactions": [None],
    }, schema={
        "county_fips":    pl.Utf8,
        "year":           pl.Int64,
        "hpi_at":         pl.Float64,
        "annual_change":  pl.Float64,
        "n_transactions": pl.Int64,
    })
    result = ParseResult(
        dataframe=df,
        source_url="http://example/fhfa",
        source_sha256="0" * 64,
        source_vintage="2024-annual",
        n_rows=1,
    )
    staged = stage_dataframe(result)
    assert staged["source_url"][0] == "http://example/fhfa"
    assert staged["source_sha256"][0] == "0" * 64
    assert staged["source_vintage"][0] == "2024-annual"
