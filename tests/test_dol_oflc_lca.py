"""End-to-end and per-stage tests for the DOL OFLC LCA ingester.

The tests are organized by ingestion stage:

* **Schema-version detection** (``test_schema_version_detection_*``): the
  five-vintage signature registry resolves each fixture file to the
  correct version.
* **Projection + unstacking** (``test_unstack_*``, ``test_project_*``):
  output row counts match expectations and case-level fields propagate
  correctly across unstacked worksite rows.
* **Type coercion** (``test_coerce_*``): wage strings (with $ and
  commas), ZIPs (with lost leading zeros), case_status / visa_class /
  wage_unit normalization all behave as documented.
* **End-to-end parse** (``test_parse_*``): the full pipeline produces a
  DataFrame that satisfies the documented invariants of the canonical
  ``raw.lca_disclosure`` shape.

Database loads are exercised in a separate ``test_dol_oflc_lca_postgres.py``
module guarded by ``@pytest.mark.live_pg``; they are only run when
``PG_TEST_DSN`` is set.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest
from hypothesis import given
from hypothesis import strategies as st

from ingestion._base import IngestError
from ingestion.dol_oflc_lca import (
    SIGNATURES,
    ParseResult,
    build_dol_lca_url,
    parse_fiscal_period_from_filename,
    parse_lca_file,
    stage_dataframe,
)

# ============================================================================
# Filename -> (FY, Q) extraction
# ============================================================================


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("LCA_Disclosure_Data_FY2024_Q3.csv", (2024, 3)),
        ("H-1B_FY2010_Q4.xlsx", (2010, 4)),
        ("H1B_Disclosure_Data_FY15_Q1.xlsx", (2015, 1)),
        ("FY09_Q2_h-1b.csv", (2009, 2)),
    ],
)
def test_parse_fiscal_period(filename: str, expected: tuple[int, int]) -> None:
    assert parse_fiscal_period_from_filename(filename) == expected


def test_parse_fiscal_period_unparseable_raises() -> None:
    with pytest.raises(IngestError, match="Cannot extract fiscal period"):
        parse_fiscal_period_from_filename("random.csv")


# ============================================================================
# DOL static-URL builder
# ============================================================================


def test_build_dol_lca_url_modern() -> None:
    """The URL builder produces the static CDN path DOL uses for FY2018+."""
    url = build_dol_lca_url(2024, 3)
    assert url == (
        "https://www.dol.gov/sites/dolgov/files/ETA/oflc/pdfs/"
        "LCA_Disclosure_Data_FY2024_Q3.xlsx"
    )


@pytest.mark.parametrize("year", [2017, 2010, 2008, 1999])
def test_build_dol_lca_url_rejects_pre_2018(year: int) -> None:
    """Older vintages are operator-staged; the auto-fetcher refuses them."""
    with pytest.raises(IngestError, match="FY2018"):
        build_dol_lca_url(year, 1)


@pytest.mark.parametrize("q", [0, 5, -1, 99])
def test_build_dol_lca_url_rejects_invalid_quarter(q: int) -> None:
    with pytest.raises(IngestError, match="fiscal_quarter"):
        build_dol_lca_url(2024, q)


# ============================================================================
# Schema-version detection
# ============================================================================


def test_signatures_are_distinct() -> None:
    """No two signatures may match the same column set; would tie-break ambiguously."""
    seen: list[frozenset[str]] = []
    for sig in SIGNATURES:
        for other in seen:
            assert sig.required_columns != other, (
                "Two signatures share required_columns; need forbidden_columns "
                "to disambiguate."
            )
        seen.append(sig.required_columns)


@pytest.mark.parametrize(
    ("fixture_name", "expected_version"),
    [
        ("lca_v1_2008_csv", "v1_2008"),
        ("lca_v2_2014_csv", "v2_2014"),
        ("lca_v3_2018_csv", "v3_2018"),
        ("lca_v4_2020_csv", "v4_2020"),
        ("lca_v5_2023_csv", "v5_2023"),
    ],
)
def test_schema_version_detection_per_vintage(
    request: pytest.FixtureRequest,
    fixture_name: str,
    expected_version: str,
) -> None:
    """Every fixture file resolves to exactly the expected schema version."""
    path: Path = request.getfixturevalue(fixture_name)
    result = parse_lca_file(path)
    assert result.schema_version == expected_version


# ============================================================================
# Multi-worksite unstacking (v3_2018 only)
# ============================================================================


def test_unstack_v3_emits_one_row_per_populated_worksite(lca_v3_2018_csv: Path) -> None:
    """Row 1 has 2 worksites + Row 2 has 1 worksite => 3 output rows."""
    result = parse_lca_file(lca_v3_2018_csv)
    assert result.n_input_rows == 2
    assert result.n_output_rows == 3
    df = result.dataframe
    case_idx_pairs = (
        df.select(["case_number", "worksite_idx"])
        .sort(["case_number", "worksite_idx"])
        .rows()
    )
    assert case_idx_pairs == [
        ("I-203-18001-001", 1),
        ("I-203-18001-001", 2),
        ("I-203-18001-002", 1),
    ]


def test_unstack_v3_propagates_case_level_fields(lca_v3_2018_csv: Path) -> None:
    """Wage, status, employer fields must be identical across unstacked rows."""
    result = parse_lca_file(lca_v3_2018_csv)
    df = result.dataframe.filter(pl.col("case_number") == "I-203-18001-001")
    assert df.height == 2
    for col in ("case_status", "visa_class", "employer_canonical_name",
                "wage_rate_of_pay_from", "wage_unit_of_pay"):
        assert df[col].n_unique() == 1, f"Column {col} should be uniform across worksites"


# ============================================================================
# Single-worksite vintages emit worksite_idx=1 uniformly
# ============================================================================


@pytest.mark.parametrize(
    "fixture_name",
    ["lca_v1_2008_csv", "lca_v2_2014_csv", "lca_v4_2020_csv", "lca_v5_2023_csv"],
)
def test_single_worksite_vintages_emit_idx_one(
    request: pytest.FixtureRequest, fixture_name: str,
) -> None:
    path: Path = request.getfixturevalue(fixture_name)
    result = parse_lca_file(path)
    assert (result.dataframe["worksite_idx"] == 1).all()
    assert result.n_input_rows == result.n_output_rows


# ============================================================================
# Type coercion + canonicalization
# ============================================================================


def test_wage_dollars_strip_currency(lca_v5_2023_csv: Path) -> None:
    """v5_2023 fixture has '$95,000.00' formatted wages; must coerce to numeric."""
    result = parse_lca_file(lca_v5_2023_csv)
    row1 = result.dataframe.filter(pl.col("case_number") == "I-205-24001-001").row(0, named=True)
    assert row1["wage_rate_of_pay_from"] == pytest.approx(95000.0)
    assert row1["wage_rate_of_pay_to"] == pytest.approx(100000.0)


def test_zip_zero_padding(lca_v2_2014_csv: Path) -> None:
    """ZIPs that lost a leading zero in Excel must be zero-padded back to 5 digits."""
    result = parse_lca_file(lca_v2_2014_csv)
    iselin_row = result.dataframe.filter(
        pl.col("case_number") == "I-201-15001-001"
    ).row(0, named=True)
    assert iselin_row["worksite_postal_code"] == "08830"

    newark_row = result.dataframe.filter(
        pl.col("case_number") == "I-201-15001-002"
    ).row(0, named=True)
    assert newark_row["worksite_postal_code"] == "07102"  # already 5 digits


def test_case_status_normalization(lca_v1_2008_csv: Path) -> None:
    """Mixed-case statuses ('Certified', 'DENIED', 'Withdrawn') must normalize."""
    result = parse_lca_file(lca_v1_2008_csv)
    statuses = sorted(result.dataframe["case_status"].to_list())
    assert statuses == ["CERTIFIED", "DENIED", "WITHDRAWN"]


def test_visa_class_normalization(lca_v5_2023_csv: Path) -> None:
    result = parse_lca_file(lca_v5_2023_csv)
    classes = sorted(result.dataframe["visa_class"].to_list())
    assert classes == ["E-3 Australian", "H-1B", "H-1B"]


def test_employer_canonicalization_collapses_suffixes(lca_v5_2023_csv: Path) -> None:
    """Both 'Tata Consultancy Services LLC' rows should canonicalize identically."""
    result = parse_lca_file(lca_v5_2023_csv)
    tata_rows = result.dataframe.filter(pl.col("case_number") == "I-205-24001-001")
    assert tata_rows["employer_canonical_name"].to_list() == [
        "tata consultancy services"
    ]


def test_wage_unit_normalization_preserves_canonical_set(lca_v4_2020_csv: Path) -> None:
    """All wage_unit values must be in the CHECK-constrained set."""
    result = parse_lca_file(lca_v4_2020_csv)
    units = set(result.dataframe["wage_unit_of_pay"].to_list())
    allowed = {"Hour", "Week", "Bi-Weekly", "Month", "Year"}
    assert units.issubset(allowed)


# ============================================================================
# Wage-annualization arithmetic (the canonical SQL CASE; documented here
# for parity testing with raw.lca_disclosure GENERATED columns).
# ============================================================================

# This is the EXACT CASE expression encoded in migration 011. We mirror it
# in Python only for property-based testing of the input-validation rules
# the loader performs upstream of the database.
_PYTHON_WAGE_FACTORS = {"Hour": 2080, "Week": 52, "Bi-Weekly": 26, "Month": 12, "Year": 1}


def _python_annualize(rate: float | None, unit: str | None) -> float | None:
    if rate is None or unit is None:
        return None
    factor = _PYTHON_WAGE_FACTORS.get(unit)
    if factor is None:
        return None
    return rate * factor


@given(
    rate=st.floats(min_value=0.01, max_value=1_000_000, allow_nan=False, allow_infinity=False),
    unit=st.sampled_from(list(_PYTHON_WAGE_FACTORS.keys())),
)
def test_python_annualize_matches_sql_factors(rate: float, unit: str) -> None:
    """Annualization in Python must equal raw_rate * the SQL CASE factor."""
    annualized = _python_annualize(rate, unit)
    assert annualized is not None
    assert annualized == pytest.approx(rate * _PYTHON_WAGE_FACTORS[unit])


@given(rate=st.floats(min_value=0.01, max_value=1_000_000, allow_nan=False))
def test_python_annualize_unknown_unit_returns_none(rate: float) -> None:
    """Any unit not in the canonical set must annualize to NULL."""
    assert _python_annualize(rate, "Fortnight") is None


# ============================================================================
# End-to-end ParseResult invariants
# ============================================================================


@pytest.mark.parametrize(
    "fixture_name",
    ["lca_v1_2008_csv", "lca_v2_2014_csv", "lca_v3_2018_csv",
     "lca_v4_2020_csv", "lca_v5_2023_csv"],
)
def test_parse_result_invariants(
    request: pytest.FixtureRequest, fixture_name: str,
) -> None:
    """Per-vintage end-to-end smoke: every ParseResult must satisfy these."""
    path: Path = request.getfixturevalue(fixture_name)
    result = parse_lca_file(path)

    assert isinstance(result, ParseResult)
    assert result.n_output_rows >= result.n_input_rows  # unstacking only adds rows
    assert len(result.source_sha256) == 64
    assert all(c in "0123456789abcdef" for c in result.source_sha256)
    df = result.dataframe
    assert df["case_number"].null_count() == 0
    assert df["worksite_idx"].null_count() == 0
    assert df["worksite_idx"].min() >= 1
    assert df["worksite_idx"].max() <= 10


# ============================================================================
# stage_dataframe -- destination-shape projection
# ============================================================================


_EXPECTED_DEST_COLUMNS: tuple[str, ...] = (
    "fiscal_year", "fiscal_quarter", "case_number", "worksite_idx",
    "case_status", "visa_class",
    "received_date", "decision_date",
    "employment_start_date", "employment_end_date",
    "employer_name", "employer_canonical_name", "employer_naics",
    "employer_state", "employer_country",
    "worksite_city", "worksite_state", "worksite_postal_code",
    "total_workers",
    "wage_rate_of_pay_from", "wage_rate_of_pay_to", "wage_unit_of_pay",
    "prevailing_wage", "pw_unit_of_pay", "pw_source",
    "soc_code", "job_title",
    "source_filename", "source_sha256", "source_schema_version",
    "data_quality",
)


@pytest.mark.parametrize(
    "fixture_name",
    ["lca_v1_2008_csv", "lca_v2_2014_csv", "lca_v3_2018_csv",
     "lca_v4_2020_csv", "lca_v5_2023_csv"],
)
def test_stage_dataframe_emits_destination_schema(
    request: pytest.FixtureRequest, fixture_name: str,
) -> None:
    """staged DataFrame must have exactly the destination columns, in order."""
    path: Path = request.getfixturevalue(fixture_name)
    parsed = parse_lca_file(path)
    staged = stage_dataframe(parsed)
    assert tuple(staged.columns) == _EXPECTED_DEST_COLUMNS


def test_stage_dataframe_provenance_columns_populated(lca_v5_2023_csv: Path) -> None:
    parsed = parse_lca_file(lca_v5_2023_csv)
    staged = stage_dataframe(parsed)

    assert (staged["fiscal_year"] == 2024).all()
    assert (staged["fiscal_quarter"] == 3).all()
    assert (staged["source_filename"] == "LCA_Disclosure_Data_FY2024_Q3.csv").all()
    assert (staged["source_schema_version"] == "v5_2023").all()
    assert (staged["data_quality"] == "measured").all()
    assert (staged["source_sha256"].str.len_chars() == 64).all()
