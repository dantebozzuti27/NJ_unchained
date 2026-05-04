"""Unit tests for the ACS PUMS ingester.

These tests cover the fetch -> parse -> stage -> load pipeline up to
(but not including) the live Census FTP and the live Postgres
connection. Live tests live in ``tests/test_pg_integration.py``.

We synthesize PUMS-shaped CSV fixtures inline so the tests don't
depend on the upstream FTP or any local fixture files. The synthetic
fixtures match the exact column names and ordering Census uses, so
column-rename refactors in the production parser will fail loudly here.
"""

from __future__ import annotations

import io
import zipfile
from typing import Final

import polars as pl
import pytest

from ingestion._base import IngestError
from ingestion.census_acs_pums import (
    EARLIEST_SUPPORTED_YEAR,
    HOUSING_REPL_WEIGHT_VARS,
    HOUSING_VARS,
    PERSON_REPL_WEIGHT_VARS,
    PERSON_VARS,
    PUMSFetchResult,
    VintageNotPublishedError,
    _build_url,
    _extract_csv_from_zip,
    _format_int_array,
    _parse_pums_csv,
    stage_housing_dataframe,
    stage_person_dataframe,
)

# ============================================================================
# Helpers: build a synthetic PUMS CSV
# ============================================================================


def _synth_person_csv(rows: int, *, puma_col: str = "PUMA") -> bytes:
    """Build a CSV in the exact PUMS person column shape with *rows* rows.

    *puma_col* picks which PUMA-vintage column to emit. The ingester
    detects PUMA / PUMA10 / PUMA20 dynamically; tests can pass any of
    those values to exercise the detection logic.
    """
    cols = [*PERSON_VARS, puma_col, *PERSON_REPL_WEIGHT_VARS]
    header = ",".join(cols)
    lines = [header]
    for i in range(rows):
        row = [
            f"2022GQ{i:08d}",          # SERIALNO
            "1",                        # SPORDER
            "34",                       # ST
            str(25 + i % 50),           # AGEP
            "1" if i % 2 == 0 else "2", # SEX
            "1",                        # RAC1P
            "1",                        # HISP
            "1",                        # CIT
            "1",                        # POBP
            "1",                        # NATIVITY
            "21",                       # SCHL (BA)
            "1",                        # ESR (employed)
            "1",                        # COW (private)
            "85000",                    # WAGP
            "85000",                    # PERNP
            "92000",                    # PINCP
            "42",                       # PWGTP
            f"{3500 + i % 50:05d}",     # PUMA / PUMA10 / PUMA20
        ]
        row += [str(40 + (j % 5)) for j in range(80)]
        lines.append(",".join(row))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _synth_housing_csv(
    rows: int, *,
    puma_col: str = "PUMA",
    year_built_col: str | None = "YRBLT",
) -> bytes:
    """Build a synthetic PUMS-housing CSV.

    ``year_built_col`` controls the year-built variable name:
      * ``"YRBLT"`` (default): 4-digit-year encoding (ACS 2019+ 1-Year,
        ACS 2021+ 5-Year)
      * ``"YBL"``: pre-2019 1-22 binned encoding
      * ``None``: omit the column entirely (regression for files
        where the variable is absent)
    """
    cols = [*HOUSING_VARS, puma_col]
    if year_built_col is not None:
        cols.append(year_built_col)
    cols += [*HOUSING_REPL_WEIGHT_VARS]
    header = ",".join(cols)
    lines = [header]
    for i in range(rows):
        row = [
            f"2022GQ{i:08d}",          # SERIALNO
            "34",                       # ST
            "1",                        # TEN (owned-with-mortgage)
            "3",                        # BDSP
            "6",                        # RMSP
            "2",                        # BLD
            "2",                        # VEH
            "550000",                   # VALP
            "",                         # GRNTP
            "",                         # RNTP
            "3500",                     # SMOCP
            "2400",                     # SMP
            "120000",                   # HINCP
            "120000",                   # FINCP
            "85",                       # WGTP
            f"{3500 + i % 50:05d}",     # PUMA / PUMA10 / PUMA20
        ]
        if year_built_col == "YRBLT":
            row.append("1980")
        elif year_built_col == "YBL":
            row.append("10")  # 1980-1989 bin in pre-2019 encoding
        row += [str(80 + (j % 5)) for j in range(80)]
        lines.append(",".join(row))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _zip_bytes(name: str, payload: bytes) -> bytes:
    """Wrap *payload* in a single-file ZIP archive (mimics Census FTP layout)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, payload)
    return buf.getvalue()


# ============================================================================
# URL construction
# ============================================================================


def test_build_url_acs1_person() -> None:
    url = _build_url(year=2022, product="acs1", state_lower="nj", kind="p")
    assert url == (
        "https://www2.census.gov/programs-surveys/acs/data/pums/"
        "2022/1-Year/csv_pnj.zip"
    )


def test_build_url_acs5_housing() -> None:
    url = _build_url(year=2022, product="acs5", state_lower="nj", kind="h")
    assert url == (
        "https://www2.census.gov/programs-surveys/acs/data/pums/"
        "2022/5-Year/csv_hnj.zip"
    )


def test_build_url_rejects_invalid_kind() -> None:
    with pytest.raises(IngestError, match="kind must be 'p' or 'h'"):
        _build_url(year=2022, product="acs1", state_lower="nj", kind="x")


def test_build_url_rejects_invalid_product() -> None:
    with pytest.raises(IngestError, match="product must be"):
        _build_url(year=2022, product="acs10", state_lower="nj", kind="p")


# ============================================================================
# ZIP -> CSV extraction
# ============================================================================


def test_extract_csv_from_zip_preferred_name() -> None:
    payload = b"a,b\n1,2\n"
    zip_bytes = _zip_bytes("psam_pNJ.csv", payload)
    out = _extract_csv_from_zip(zip_bytes, kind="p", state_upper="NJ")
    assert out == payload


def test_extract_csv_from_zip_fips_alt_name() -> None:
    """If the preferred name isn't present, fall back to first .csv."""
    payload = b"a,b\n1,2\n"
    zip_bytes = _zip_bytes("psam_p34.csv", payload)
    out = _extract_csv_from_zip(zip_bytes, kind="p", state_upper="NJ")
    assert out == payload


def test_extract_csv_from_zip_no_csv_raises() -> None:
    zip_bytes = _zip_bytes("README.txt", b"hello")
    with pytest.raises(IngestError, match="No CSV found"):
        _extract_csv_from_zip(zip_bytes, kind="p", state_upper="NJ")


# ============================================================================
# Parser: column projection, replicate-weight folding, type coercion
# ============================================================================


def test_parse_person_csv_projects_to_canonical_columns() -> None:
    csv = _synth_person_csv(rows=10)
    df = _parse_pums_csv(
        csv, primary_vars=PERSON_VARS, weight_vars=PERSON_REPL_WEIGHT_VARS,
        is_person=True, year=2022, product="acs1",
    )
    expected = {*PERSON_VARS, "PUMA", "puma_vintage", "replicate_weights"}
    assert set(df.columns) == expected, (
        f"PUMS parser must emit only the projected columns + replicate_weights; "
        f"got {set(df.columns)} - {expected}"
    )


def test_parse_person_csv_replicate_weights_length_80() -> None:
    csv = _synth_person_csv(rows=5)
    df = _parse_pums_csv(
        csv, primary_vars=PERSON_VARS, weight_vars=PERSON_REPL_WEIGHT_VARS,
        is_person=True, year=2022, product="acs1",
    )
    weights_col = df["replicate_weights"]
    for w in weights_col:
        assert len(w) == 80, "Every PUMS row must have exactly 80 replicate weights"


def test_parse_person_csv_state_and_puma_zfilled() -> None:
    csv = _synth_person_csv(rows=3)
    df = _parse_pums_csv(
        csv, primary_vars=PERSON_VARS, weight_vars=PERSON_REPL_WEIGHT_VARS,
        is_person=True, year=2022, product="acs1",
    )
    assert all(len(s) == 2 for s in df["ST"]), "ST must be 2-char zero-padded"
    assert all(len(s) == 5 for s in df["PUMA"]), "PUMA must be 5-char zero-padded"


def test_parse_housing_csv_handles_null_rent() -> None:
    """GRNTP is empty for owner-occupied units; parser must tolerate."""
    csv = _synth_housing_csv(rows=3)
    df = _parse_pums_csv(
        csv, primary_vars=HOUSING_VARS, weight_vars=HOUSING_REPL_WEIGHT_VARS,
        is_person=False, year=2022, product="acs1",
    )
    assert df["GRNTP"].null_count() == 3


def test_parse_housing_csv_modern_yrblt_carries_4_digit_year() -> None:
    """ACS 2019+ uses YRBLT (4-digit year). Parser preserves the value."""
    csv = _synth_housing_csv(rows=3, year_built_col="YRBLT")
    df = _parse_pums_csv(
        csv, primary_vars=HOUSING_VARS, weight_vars=HOUSING_REPL_WEIGHT_VARS,
        is_person=False, year=2022, product="acs1",
    )
    assert "YRBLT" in df.columns
    assert (df["YRBLT"] == 1980).all()


def test_parse_housing_csv_legacy_ybl_writes_null_yrblt() -> None:
    """Pre-2019 files carry YBL (1-22 bin code); parser nulls YRBLT.

    Regression pin: the bin->midpoint mapping is intentionally NOT
    applied here. The downstream constraint on the yrblt column is
    1939 <= y <= 2099 OR NULL, so writing the raw bin value (e.g.,
    10) would corrupt the column. NULL is correct loss-of-resolution
    behavior; we can recover with a real mapping later if needed.
    """
    csv = _synth_housing_csv(rows=4, year_built_col="YBL")
    df = _parse_pums_csv(
        csv, primary_vars=HOUSING_VARS, weight_vars=HOUSING_REPL_WEIGHT_VARS,
        is_person=False, year=2018, product="acs1",
    )
    assert "YRBLT" in df.columns
    assert "YBL" not in df.columns
    assert df["YRBLT"].null_count() == 4


def test_parse_housing_csv_no_year_built_column_writes_null() -> None:
    """If the file has neither YRBLT nor YBL, parser writes NULL."""
    csv = _synth_housing_csv(rows=2, year_built_col=None)
    df = _parse_pums_csv(
        csv, primary_vars=HOUSING_VARS, weight_vars=HOUSING_REPL_WEIGHT_VARS,
        is_person=False, year=2022, product="acs1",
    )
    assert df["YRBLT"].null_count() == 2


def test_parse_pums_csv_dispatches_default_puma_vintage_on_year() -> None:
    """Bare-PUMA files: vintage default tracks the year (decennial transition).

    1-Year files for 2017-2021 carry only "PUMA" but encode 2010-vintage
    boundaries; 2022+ encode 2020-vintage. The ingester must dispatch on
    year, not blanket-tag '2020'. The threshold is 2022, not 2021 -- 2021
    1-Year was published before Census finalized 2020-vintage PUMAs, so
    Census kept 2010 boundaries in that file. Regression for a silent-
    correctness bug surfaced by a real materialization failure.
    """
    csv_2018 = _synth_person_csv(rows=3, puma_col="PUMA")
    csv_2021 = _synth_person_csv(rows=3, puma_col="PUMA")
    csv_2022 = _synth_person_csv(rows=3, puma_col="PUMA")
    df_2018 = _parse_pums_csv(
        csv_2018, primary_vars=PERSON_VARS,
        weight_vars=PERSON_REPL_WEIGHT_VARS,
        is_person=True, year=2018, product="acs1",
    )
    df_2021 = _parse_pums_csv(
        csv_2021, primary_vars=PERSON_VARS,
        weight_vars=PERSON_REPL_WEIGHT_VARS,
        is_person=True, year=2021, product="acs1",
    )
    df_2022 = _parse_pums_csv(
        csv_2022, primary_vars=PERSON_VARS,
        weight_vars=PERSON_REPL_WEIGHT_VARS,
        is_person=True, year=2022, product="acs1",
    )
    assert (df_2018["puma_vintage"] == "2010").all()
    assert (df_2021["puma_vintage"] == "2010").all(), (
        "2021 1-Year still uses 2010-vintage PUMAs -- decennial threshold "
        "is 2022, not 2021."
    )
    assert (df_2022["puma_vintage"] == "2020").all()


def test_parse_csv_rejects_unknown_columns() -> None:
    """Parser must fail if upstream renames a required column."""
    bad_csv = b"FOO,BAR\n1,2\n"
    with pytest.raises(Exception):  # noqa: B017 -- polars-specific exception type
        _parse_pums_csv(
            bad_csv,
            primary_vars=PERSON_VARS, weight_vars=PERSON_REPL_WEIGHT_VARS,
            is_person=True, year=2022, product="acs1",
        )


# ============================================================================
# Stage: rename + provenance columns
# ============================================================================


def _synthetic_fetch(rows: int = 5) -> PUMSFetchResult:
    """Build a PUMSFetchResult from synthetic CSVs end-to-end."""
    csv_p = _synth_person_csv(rows=rows)
    csv_h = _synth_housing_csv(rows=rows)
    df_p = _parse_pums_csv(
        csv_p, primary_vars=PERSON_VARS, weight_vars=PERSON_REPL_WEIGHT_VARS,
        is_person=True, year=2022, product="acs1",
    )
    df_h = _parse_pums_csv(
        csv_h, primary_vars=HOUSING_VARS, weight_vars=HOUSING_REPL_WEIGHT_VARS,
        is_person=False, year=2022, product="acs1",
    )
    return PUMSFetchResult(
        person=df_p, housing=df_h,
        source_url_person="https://test/p.zip",
        source_url_housing="https://test/h.zip",
        source_sha256_person="0" * 64,
        source_sha256_housing="1" * 64,
        year=2022, product="acs1", state_fips="34",
        n_person_rows=df_p.height, n_housing_rows=df_h.height,
    )


def test_stage_person_renames_columns_to_lowercase() -> None:
    fetch = _synthetic_fetch(rows=3)
    staged = stage_person_dataframe(fetch)
    # Must contain lowercase + provenance columns
    expected: Final[set[str]] = {
        "year", "product", "serialno", "sporder",
        "state_fips", "puma", "puma_vintage",
        "agep", "sex", "rac1p", "hisp", "cit", "pobp", "nativity",
        "schl", "esr", "cow",
        "wagp", "pernp", "pincp",
        "pwgtp", "replicate_weights",
        "source_url", "source_sha256", "source_vintage",
    }
    assert set(staged.columns) == expected


def test_stage_person_year_and_product_constant() -> None:
    fetch = _synthetic_fetch(rows=3)
    staged = stage_person_dataframe(fetch)
    assert (staged["year"] == 2022).all()
    assert (staged["product"] == "acs1").all()


def test_stage_housing_renames_correctly() -> None:
    fetch = _synthetic_fetch(rows=3)
    staged = stage_housing_dataframe(fetch)
    # No 'sporder' on housing
    assert "sporder" not in staged.columns
    assert "ten" in staged.columns
    assert "wgtp" in staged.columns


def test_stage_provenance_uses_correct_url_per_table() -> None:
    """Person staged rows get url_person; housing get url_housing.

    A regression where both used the same URL would silently lose
    auditability for one of the two tables.
    """
    fetch = _synthetic_fetch(rows=2)
    p = stage_person_dataframe(fetch)
    h = stage_housing_dataframe(fetch)
    assert p["source_url"][0] == "https://test/p.zip"
    assert h["source_url"][0] == "https://test/h.zip"
    assert p["source_url"][0] != h["source_url"][0]


def test_stage_vintage_default_format() -> None:
    fetch = _synthetic_fetch(rows=1)
    p = stage_person_dataframe(fetch)
    assert p["source_vintage"][0] == "2022-acs1"


# ============================================================================
# Postgres array literal formatter
# ============================================================================


def test_format_int_array_normal() -> None:
    out = _format_int_array([1, 2, 3])
    assert out == "{1,2,3}"


def test_format_int_array_empty_list_emits_braces() -> None:
    out = _format_int_array([])
    assert out == "{}"


def test_format_int_array_none_returns_empty_string() -> None:
    """NULL array -> empty string -> Postgres NULL via NULL ''."""
    assert _format_int_array(None) == ""


def test_format_int_array_rejects_inner_null() -> None:
    """A NULL inside a 80-weight array is upstream corruption; fail loudly."""
    with pytest.raises(IngestError, match="NULL inside replicate_weights"):
        _format_int_array([1, 2, None, 4])


# ============================================================================
# Vintage / version guards
# ============================================================================


def test_earliest_supported_year_is_2017() -> None:
    """Pre-2017 PUMS uses ss{YY}p{state}.csv -- explicitly out of scope."""
    assert EARLIEST_SUPPORTED_YEAR == 2017


def test_vintage_not_published_is_ingest_error_subclass() -> None:
    """Subclass relationship matters: orchestration catches IngestError."""
    assert issubclass(VintageNotPublishedError, IngestError)


# ============================================================================
# DataFrame shape end-to-end
# ============================================================================


def test_end_to_end_synthetic_shape() -> None:
    """Pipe synthetic CSVs through fetch -> stage and check final shape."""
    fetch = _synthetic_fetch(rows=10)
    p = stage_person_dataframe(fetch)
    h = stage_housing_dataframe(fetch)
    assert p.height == 10
    assert h.height == 10
    # Replicate weights survive the rename + provenance addition
    weights_p = p["replicate_weights"]
    weights_h = h["replicate_weights"]
    assert all(len(w) == 80 for w in weights_p)
    assert all(len(w) == 80 for w in weights_h)
    # Population-weight columns are integers
    assert p["pwgtp"].dtype in {pl.Int64, pl.Int32}
    assert h["wgtp"].dtype  in {pl.Int64, pl.Int32}


# ============================================================================
# PUMA vintage detection -- the cross-decennial-boundary hazard
#
# Real-data finding: Census's 2022 5-Year ACS PUMS file carries BOTH
# PUMA10 and PUMA20 columns (PUMA boundaries were revised after the 2020
# Census). Records sampled 2018-2019 populate PUMA10; 2020-2022 populate
# PUMA20. Census uses the literal "-9" as the not-applicable sentinel
# in the column that does not apply to that record.
#
# These tests pin three critical behaviors:
#   (a) Detection picks up whichever PUMA-vintage columns are in the header.
#   (b) Coalesce prefers PUMA20 (post-decennial), tags vintage correctly.
#   (c) The "-9" sentinel is treated as null, not as a literal PUMA value.
# ============================================================================


def test_parse_csv_only_puma_column_tags_2020_vintage() -> None:
    """1-Year files post-2022 carry only `PUMA`; tag puma_vintage='2020'."""
    csv = _synth_person_csv(rows=5, puma_col="PUMA")
    df = _parse_pums_csv(
        csv, primary_vars=PERSON_VARS, weight_vars=PERSON_REPL_WEIGHT_VARS,
        is_person=True, year=2022, product="acs1",
    )
    assert "puma_vintage" in df.columns
    assert (df["puma_vintage"] == "2020").all()
    assert all(len(s) == 5 for s in df["PUMA"])


def test_parse_csv_only_puma20_column_tags_2020_vintage() -> None:
    """If only PUMA20 is present, tag puma_vintage='2020'."""
    csv = _synth_person_csv(rows=5, puma_col="PUMA20")
    df = _parse_pums_csv(
        csv, primary_vars=PERSON_VARS, weight_vars=PERSON_REPL_WEIGHT_VARS,
        is_person=True, year=2022, product="acs1",
    )
    assert (df["puma_vintage"] == "2020").all()


def test_parse_csv_only_puma10_column_tags_2010_vintage() -> None:
    """If only PUMA10 is present, tag puma_vintage='2010'."""
    csv = _synth_person_csv(rows=5, puma_col="PUMA10")
    df = _parse_pums_csv(
        csv, primary_vars=PERSON_VARS, weight_vars=PERSON_REPL_WEIGHT_VARS,
        is_person=True, year=2022, product="acs1",
    )
    assert (df["puma_vintage"] == "2010").all()


def _synth_person_csv_dual_vintage() -> bytes:
    """Build a 5-Year-style CSV with both PUMA10 and PUMA20 columns.

    Half the rows have populated PUMA10 and "-9" PUMA20 (2018-2019);
    half have populated PUMA20 and "-9" PUMA10 (2020-2022). Mirrors
    the actual Census 5-Year encoding.
    """
    cols = [
        *PERSON_VARS,
        "PUMA10", "PUMA20",
        *PERSON_REPL_WEIGHT_VARS,
    ]
    header = ",".join(cols)
    lines = [header]
    for i in range(20):
        is_old = i < 10
        puma10 = f"{1700 + i:05d}" if is_old else "-9"
        puma20 = "-9"              if is_old else f"{3500 + i:05d}"
        row = [
            f"2022GQ{i:08d}",          # SERIALNO
            "1",                        # SPORDER
            "34",                       # ST
            str(30),                    # AGEP
            "1",                        # SEX
            "1",                        # RAC1P
            "1",                        # HISP
            "1",                        # CIT
            "1",                        # POBP
            "1",                        # NATIVITY
            "21",                       # SCHL
            "1",                        # ESR
            "1",                        # COW
            "85000",                    # WAGP
            "85000",                    # PERNP
            "92000",                    # PINCP
            "42",                       # PWGTP
            puma10,                     # PUMA10
            puma20,                     # PUMA20
        ] + [str(40 + (j % 5)) for j in range(80)]
        lines.append(",".join(row))
    return ("\n".join(lines) + "\n").encode("utf-8")


def test_parse_csv_dual_vintage_coalesces_prefer_puma20() -> None:
    """Both PUMA10 and PUMA20 present: pick the populated one, tag vintage."""
    csv = _synth_person_csv_dual_vintage()
    df = _parse_pums_csv(
        csv, primary_vars=PERSON_VARS, weight_vars=PERSON_REPL_WEIGHT_VARS,
        is_person=True, year=2022, product="acs1",
    )
    assert df.height == 20
    n_2010 = (df["puma_vintage"] == "2010").sum()
    n_2020 = (df["puma_vintage"] == "2020").sum()
    assert n_2010 == 10, "Half the rows should be tagged as 2010-vintage"
    assert n_2020 == 10, "Other half should be tagged as 2020-vintage"

    # The "-9" sentinel must be filtered out; PUMA values are 5-digit
    # zero-padded strings and never "-0009".
    pumas = df["PUMA"].to_list()
    assert all(p is not None for p in pumas)
    assert "-0009" not in pumas
    assert all(len(p) == 5 for p in pumas)


def test_parse_csv_negative_sentinel_does_not_become_literal_puma() -> None:
    """Pin the failure mode that bit us in the 5-Year live ingest.

    Before the fix, "-9" -> int(-9) -> "-9" -> zfill(5) = "-0009"
    survived as a "PUMA". The CHECK constraint in raw.acs_pums_person
    rejects that pattern -- but only at COPY time, after a long fetch.
    Catch it here at parse time instead.
    """
    csv = _synth_person_csv_dual_vintage()
    df = _parse_pums_csv(
        csv, primary_vars=PERSON_VARS, weight_vars=PERSON_REPL_WEIGHT_VARS,
        is_person=True, year=2022, product="acs1",
    )
    for p in df["PUMA"].to_list():
        assert p is None or (p.isdigit() and 1 <= int(p) <= 99999), (
            f"Bad PUMA value in parser output: {p!r}"
        )
