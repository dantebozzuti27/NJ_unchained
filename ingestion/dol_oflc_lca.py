"""DOL OFLC Labor Condition Application (LCA) disclosure ingester.

POP-2 of the population-segmentation tier (TIER 3.5). See
``db/migrations/011_dol_oflc_lca.sql`` for the schema this loader writes
into.

Architecture
------------
The loader is split into three layers, exposed as separate functions so
each is independently testable:

1. **Parse** -- :func:`parse_lca_file` reads a single quarterly LCA file
   (CSV or Excel), detects its schema version, projects to the canonical
   ``raw.lca_disclosure`` shape, and unstacks multi-worksite (v3_2018)
   rows. Pure function: file in, ``polars.DataFrame`` out. No database
   connection.
2. **Stage** -- :func:`stage_dataframe` adds provenance columns
   (source_filename, source_sha256, source_schema_version, fiscal_year,
   fiscal_quarter, data_quality) and validates DataFrame shape against
   the destination schema.
3. **Load** -- :func:`load_to_postgres` bulk-COPYs the staged DataFrame
   into ``raw.lca_disclosure`` via psycopg3's binary COPY protocol.

The CLI (:func:`cli`) composes all three. Tests exercise each layer
independently against fixtures.

Schema-version registry
-----------------------
:data:`SIGNATURES` lists the five known LCA disclosure schema vintages.
Adding a sixth requires (a) adding a new ``SchemaSignature`` here, (b)
adding the corresponding entry to :data:`COLUMN_MAPPINGS`, and (c)
extending the ``CHECK (source_schema_version IN ...)`` constraint in
migration 011.

Wage annualization
------------------
The loader does NOT annualize wages. ``raw.lca_disclosure`` annualizes
in SQL via GENERATED columns (see migration 011). Keeping the conversion
rule in exactly one place is a deliberate methodological choice; see
the migration header.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import click
import polars as pl

from ingestion._base import (
    IngestError,
    SchemaSignature,
    canonical_employer_name,
    canonicalize_column,
    detect_schema_version,
    sha256_file,
)

if TYPE_CHECKING:
    import psycopg

log = logging.getLogger(__name__)

_COPY_CHUNK = 1 << 20  # 1 MiB


# ============================================================================
# Schema registry
# ============================================================================

# Each signature lists the canonicalized columns that uniquely identify the
# vintage. ``forbidden_columns`` carries names that exist only in *later*
# vintages; their absence pins the file to an earlier schema.
SIGNATURES: Final[tuple[SchemaSignature, ...]] = (
    SchemaSignature(
        name="v1_2008",
        required_columns=frozenset({
            "lca_case_number",
            "lca_case_employer_name",
            "status",
            "wage_rate_1",
            "wage_unit_1",
        }),
        # v1_2008 used per-program files. case_number / case_status appeared
        # in v2_2014; their absence is the strongest pin.
        forbidden_columns=frozenset({"case_number", "case_status"}),
    ),
    SchemaSignature(
        name="v2_2014",
        required_columns=frozenset({
            "case_number",
            "case_status",
            "employer_name",
            "wage_rate_of_pay_from",
            "wage_unit_of_pay",
        }),
        # Two later vintages must NOT match v2 even though they share
        # case_*/wage_* columns:
        #   * v3_2018 introduced multi-worksite wide columns
        #     (worksite_postal_code_1).
        #   * v4_2020+ introduced full_time_position as a top-level column.
        #   * v5_2023 added naics_code.
        # The forbidden_columns set discriminates against ALL of them.
        forbidden_columns=frozenset({
            "worksite_postal_code_1",
            "worksite_city_1",
            "full_time_position",
            "naics_code",
        }),
    ),
    SchemaSignature(
        name="v3_2018",
        required_columns=frozenset({
            "case_number",
            "case_status",
            "employer_name",
            "worksite_postal_code_1",   # the multi-worksite wide format
            "wage_rate_of_pay_from",
        }),
        # v3 had no single-worksite worksite_postal_code, no NAICS, and no
        # full_time_position (those came in v4+).
        forbidden_columns=frozenset({
            "worksite_postal_code",
            "full_time_position",
            "employer_naics_code",
        }),
    ),
    SchemaSignature(
        name="v4_2020",
        required_columns=frozenset({
            "case_number",
            "case_status",
            "employer_name",
            "worksite_postal_code",     # back to one-row-per-worksite at source
            "wage_rate_of_pay_from",
            "full_time_position",       # v4+ marker; also forbidden in v2/v3
        }),
        # v5_2023 added NAICS_CODE as a top-level column. Its absence
        # pins us to v4.
        forbidden_columns=frozenset({
            "naics_code",
            "worksite_postal_code_1",
        }),
    ),
    SchemaSignature(
        name="v5_2023",
        required_columns=frozenset({
            "case_number",
            "case_status",
            "employer_name",
            "worksite_postal_code",
            "wage_rate_of_pay_from",
            "full_time_position",
            # Real DOL files (FY2023+) ship the column as NAICS_CODE; the
            # earlier "employer_naics_code" was an incorrect guess made before
            # we had ground-truth data from a real download.
            "naics_code",
        }),
        forbidden_columns=frozenset({
            "worksite_postal_code_1",
        }),
    ),
)


# ============================================================================
# Per-vintage column-name -> canonical-raw-column mapping
# ============================================================================

# All keys are CANONICAL column names in the destination schema
# (``raw.lca_disclosure`` columns minus the provenance columns and minus the
# generated annualized_* columns). All values are the canonicalized source
# column name in that vintage.
#
# Multi-worksite columns (v3_2018) are NOT in this dict; they are handled
# separately by the unstacker, which knows about WORKSITE_*_{1..N}.

# Common columns shared by v2_2014 / v3_2018 / v4_2020 / v5_2023.
# IMPORTANT: dates are NOT in this base because DOL renamed the LCA
# employment-period columns when ETA Form 9035 was reissued in 2018:
#   * v1_2008/v2_2014 used  EMPLOYMENT_START_DATE / EMPLOYMENT_END_DATE
#   * v3_2018+         use  BEGIN_DATE             / END_DATE
# Each vintage's mapping below specifies which source columns to read.
_BASE_V2PLUS: Final[dict[str, str]] = {
    "case_number": "case_number",
    "case_status": "case_status",
    "visa_class": "visa_class",
    "received_date": "received_date",
    "decision_date": "decision_date",
    "employer_name": "employer_name",
    "employer_state": "employer_state",
    "employer_country": "employer_country",
    "total_workers": "total_worker_positions",
    "wage_rate_of_pay_from": "wage_rate_of_pay_from",
    "wage_rate_of_pay_to": "wage_rate_of_pay_to",
    "wage_unit_of_pay": "wage_unit_of_pay",
    "prevailing_wage": "prevailing_wage",
    "pw_unit_of_pay": "pw_unit_of_pay",
    "pw_source": "pw_source_year",
    "soc_code": "soc_code",
    "job_title": "job_title",
    # ETA-9035 attestation bits (v4/v5 always; earlier vintages often
    # absent and then staged as NULL). Source header H-1B_DEPENDENT
    # canonicalizes to h_1b_dependent.
    "employer_fein": "employer_fein",
    "h1b_dependent": "h_1b_dependent",
    "willful_violator": "willful_violator",
    "secondary_entity": "secondary_entity",
    "secondary_entity_business_name": "secondary_entity_business_name",
    "pw_wage_level": "pw_wage_level",
}

# Dest column -> extra canonicalized source names if the primary mapping
# misses a vintage that used a shorter header (FEIN, H1B_DEPENDENT, ...).
_SRC_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "employer_fein": ("fein", "employer_tax_id"),
    "h1b_dependent": ("h1b_dependent", "h1_b_dependent"),
    "willful_violator": ("willful_violator_yn",),
    "secondary_entity": ("secondary_entity_yn",),
    "pw_wage_level": ("pw_level", "wage_level"),
}

COLUMN_MAPPINGS: Final[dict[str, dict[str, str]]] = {
    "v1_2008": {
        "case_number": "lca_case_number",
        "case_status": "status",
        "visa_class": "visa_class",
        "received_date": "submission_date",
        "decision_date": "decision_date",
        "employment_start_date": "lca_case_employment_start_date",
        "employment_end_date": "lca_case_employment_end_date",
        "employer_name": "lca_case_employer_name",
        "employer_state": "lca_case_employer_state",
        "total_workers": "total_workers",
        "wage_rate_of_pay_from": "wage_rate_1",
        "wage_unit_of_pay": "wage_unit_1",
        "prevailing_wage": "pw_1",
        "pw_unit_of_pay": "pw_unit_1",
        "soc_code": "lca_case_soc_code",
        "job_title": "lca_case_job_title",
        "worksite_city": "lca_case_workloc1_city",
        "worksite_state": "lca_case_workloc1_state",
        "worksite_postal_code": "lca_case_workloc1_postal_code",
    },
    "v2_2014": {
        **_BASE_V2PLUS,
        # Pre-2018 LCA form (ETA 9035 vintage 2009): EMPLOYMENT_START_DATE.
        "employment_start_date": "employment_start_date",
        "employment_end_date": "employment_end_date",
        "worksite_city": "worksite_city",
        "worksite_state": "worksite_state",
        "worksite_postal_code": "worksite_postal_code",
    },
    # v3_2018 worksite columns are NOT mapped here -- they are built by the
    # unstacker. The keys present here are the case-level columns that are
    # the same across all unstacked rows.
    "v3_2018": {
        **_BASE_V2PLUS,
        # 2018 LCA form rename: BEGIN_DATE / END_DATE.
        "employment_start_date": "begin_date",
        "employment_end_date": "end_date",
    },
    "v4_2020": {
        **_BASE_V2PLUS,
        "employment_start_date": "begin_date",
        "employment_end_date": "end_date",
        "worksite_city": "worksite_city",
        "worksite_state": "worksite_state",
        "worksite_postal_code": "worksite_postal_code",
    },
    "v5_2023": {
        **_BASE_V2PLUS,
        "employment_start_date": "begin_date",
        "employment_end_date": "end_date",
        # FY2023+: column header is NAICS_CODE (not EMPLOYER_NAICS_CODE).
        "employer_naics": "naics_code",
        "worksite_city": "worksite_city",
        "worksite_state": "worksite_state",
        "worksite_postal_code": "worksite_postal_code",
    },
}


# Maximum worksite_idx supported by the unstacker (and the CHECK constraint
# in raw.lca_disclosure). DOL has historically gone up to 10.
MAX_WORKSITES: Final[int] = 10


# ============================================================================
# Visa-class normalization
# ============================================================================

# DOL's visa_class values vary in capitalization and whitespace. Map all
# observed variants to the canonical set listed in raw.lca_disclosure's
# CHECK constraint.
_VISA_CLASS_NORMALIZER: Final[dict[str, str]] = {
    "h-1b": "H-1B",
    "h1b": "H-1B",
    "h1-b": "H-1B",
    "h-1b1 chile": "H-1B1 Chile",
    "h-1b1 singapore": "H-1B1 Singapore",
    "e-3 australian": "E-3 Australian",
    "e3 australian": "E-3 Australian",
    "h-2a": "H-2A",
    "h-2b": "H-2B",
    "perm": "PERM",
    "pw determination - perm": "PERM",
    "cw-1": "CW-1",
}


def _normalize_visa_class(raw: str | None) -> str:
    """Map DOL visa_class variants to the canonical CHECK-constrained set."""
    if raw is None:
        return "OTHER"
    key = raw.strip().lower()
    return _VISA_CLASS_NORMALIZER.get(key, "OTHER")


# ============================================================================
# Case-status normalization
# ============================================================================

_CASE_STATUS_NORMALIZER: Final[dict[str, str]] = {
    "certified": "CERTIFIED",
    "certified - withdrawn": "CERTIFIED-WITHDRAWN",
    "certified-withdrawn": "CERTIFIED-WITHDRAWN",
    "withdrawn": "WITHDRAWN",
    "denied": "DENIED",
}


def _normalize_case_status(raw: str | None) -> str | None:
    """Map DOL case_status variants to the CHECK-constrained set; None passes through."""
    if raw is None:
        return None
    return _CASE_STATUS_NORMALIZER.get(raw.strip().lower())


# ============================================================================
# Wage-unit normalization
# ============================================================================

_WAGE_UNIT_NORMALIZER: Final[dict[str, str]] = {
    "hour":  "Hour", "hr": "Hour", "hourly": "Hour",
    "week":  "Week", "wk": "Week", "weekly": "Week",
    "bi-weekly": "Bi-Weekly", "biweekly": "Bi-Weekly", "bw": "Bi-Weekly",
    "month": "Month", "mo": "Month", "monthly": "Month",
    "year":  "Year",  "yr": "Year",  "annual": "Year", "annually": "Year",
}


def _normalize_wage_unit(raw: str | None) -> str | None:
    """Map DOL wage_unit_of_pay variants to the CHECK-constrained set."""
    if raw is None:
        return None
    return _WAGE_UNIT_NORMALIZER.get(raw.strip().lower())


def _normalize_yn(raw: str | None) -> str | None:
    """Map Yes/No / Y/N attestation flags to the CHECK-constrained {Y, N}."""
    if raw is None:
        return None
    key = raw.strip().lower()
    if key in {"y", "yes", "true", "1"}:
        return "Y"
    if key in {"n", "no", "false", "0"}:
        return "N"
    return None


def _normalize_pw_level(raw: str | None) -> str | None:
    """Map OFLC PW_WAGE_LEVEL variants to {I, II, III, IV}."""
    if raw is None:
        return None
    key = raw.strip().upper().replace("LEVEL", "").strip()
    return {
        "I": "I", "1": "I",
        "II": "II", "2": "II",
        "III": "III", "3": "III",
        "IV": "IV", "4": "IV",
    }.get(key)


def _normalize_fein(raw: str | None) -> str | None:
    """Keep EIN digits only; drop dashes/spaces. None if no digits."""
    if raw is None:
        return None
    digits = "".join(c for c in raw if c.isdigit())
    return digits or None


def _source_column(df: pl.DataFrame, dest: str, primary: str) -> str | None:
    """Return the first matching source column for *dest*, honoring aliases."""
    if primary in df.columns:
        return primary
    for alias in _SRC_ALIASES.get(dest, ()):
        if alias in df.columns:
            return alias
    return None


# ============================================================================
# Filename-based fiscal-year/quarter extraction
# ============================================================================

# DOL filename conventions (across vintages):
#   H-1B_FY2010_Q4.xlsx
#   H1B_Disclosure_Data_FY15_Q1.xlsx
#   LCA_Disclosure_Data_FY2024_Q3.csv
_FILENAME_FY_RE: Final[re.Pattern[str]] = re.compile(
    r"FY(\d{2,4}).*?Q([1-4])", re.IGNORECASE
)


def parse_fiscal_period_from_filename(filename: str) -> tuple[int, int]:
    """Extract (fiscal_year, fiscal_quarter) from an LCA filename.

    >>> parse_fiscal_period_from_filename("LCA_Disclosure_Data_FY2024_Q3.csv")
    (2024, 3)
    >>> parse_fiscal_period_from_filename("H-1B_FY2010_Q4.xlsx")
    (2010, 4)
    >>> parse_fiscal_period_from_filename("H1B_Disclosure_Data_FY15_Q1.xlsx")
    (2015, 1)

    Two-digit years are rebased to 20YY (DOL OFLC publishing began in 2008).
    """
    m = _FILENAME_FY_RE.search(filename)
    if not m:
        raise IngestError(
            f"Cannot extract fiscal period from filename {filename!r}; "
            "expected pattern like 'FY2024_Q3' or 'FY15_Q1'."
        )
    year_raw = int(m.group(1))
    fiscal_year = 2000 + year_raw if year_raw < 100 else year_raw
    fiscal_quarter = int(m.group(2))
    return fiscal_year, fiscal_quarter


# ============================================================================
# Reading
# ============================================================================


def _read_raw(path: Path) -> pl.DataFrame:
    """Read an LCA file (CSV or Excel) into a polars DataFrame as strings.

    Reading as strings sidesteps polars' type inference doing the wrong thing
    on mixed-type columns (e.g. wage values that arrive as both ``"45.50"``
    and ``"$45.50"`` in older vintages). Type coercion happens later in
    :func:`_coerce_types`.
    """
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pl.read_csv(
            path,
            infer_schema_length=0,    # all columns as Utf8
            ignore_errors=False,
            null_values=["", "NA", "N/A", "NULL"],
        )
    if suffix in {".xlsx", ".xls"}:
        # fastexcel's calamine engine treats row 0 as the header by default.
        # We force every column to Utf8 to defer typing to the explicit
        # coercion passes below; this is the only safe way to handle the
        # mixed-type wage strings in older vintages without losing precision.
        return pl.read_excel(
            path,
            engine="calamine",
            infer_schema_length=0,
        )
    raise IngestError(f"Unsupported LCA file extension: {suffix!r} ({path})")


def _canonicalize_columns(df: pl.DataFrame) -> pl.DataFrame:
    """Return a copy of *df* with all column headers canonicalized in place."""
    return df.rename({c: canonicalize_column(c) for c in df.columns})


# ============================================================================
# Projection + unstacking
# ============================================================================


def _project_v3_2018(df: pl.DataFrame) -> pl.DataFrame:
    """Unstack the WORKSITE_*_{1..N} wide format used by FY2018-FY2019 files.

    For each input row, emit up to N output rows (one per non-null
    WORKSITE_POSTAL_CODE_i), each carrying the same case-level fields and
    a worksite_idx in 1..N. Cases with zero non-null worksites are dropped
    (a sanity check; the source has never legally produced one).
    """
    case_mapping = COLUMN_MAPPINGS["v3_2018"]
    case_cols = {}
    for dst, src in case_mapping.items():
        resolved = _source_column(df, dst, src)
        if resolved is not None:
            case_cols[dst] = df[resolved]

    frames: list[pl.DataFrame] = []
    for i in range(1, MAX_WORKSITES + 1):
        zip_col = f"worksite_postal_code_{i}"
        if zip_col not in df.columns:
            continue
        worksite_block = pl.DataFrame({
            **case_cols,
            "worksite_idx": pl.Series([i] * df.height, dtype=pl.Int16),
            "worksite_city": df[f"worksite_city_{i}"]
                if f"worksite_city_{i}" in df.columns
                else pl.Series([None] * df.height, dtype=pl.Utf8),
            "worksite_state": df[f"worksite_state_{i}"]
                if f"worksite_state_{i}" in df.columns
                else pl.Series([None] * df.height, dtype=pl.Utf8),
            "worksite_postal_code": df[zip_col],
        })
        # Drop rows where this worksite slot is empty (the typical case for
        # i > 2: most LCAs have 1-2 worksites, leaving slots 3..10 null).
        worksite_block = worksite_block.filter(pl.col("worksite_postal_code").is_not_null())
        if worksite_block.height > 0:
            frames.append(worksite_block)

    if not frames:
        # Defensive: every v3_2018 file should have at least worksite_1.
        raise IngestError(
            "v3_2018 file has no populated worksite_postal_code_{1..10} columns."
        )
    return pl.concat(frames, how="vertical_relaxed")


def _project_simple(df: pl.DataFrame, version: str) -> pl.DataFrame:
    """Project a single-worksite vintage (v1, v2, v4, v5) to the canonical schema."""
    mapping = COLUMN_MAPPINGS[version]
    out_cols = {}
    for dst, src in mapping.items():
        resolved = _source_column(df, dst, src)
        if resolved is not None:
            out_cols[dst] = df[resolved]
        else:
            # Optional column for this vintage; emit nulls so the schema is
            # uniform across vintages.
            out_cols[dst] = pl.Series([None] * df.height, dtype=pl.Utf8)
    out_cols["worksite_idx"] = pl.Series([1] * df.height, dtype=pl.Int16)
    return pl.DataFrame(out_cols)


# ============================================================================
# Type coercion + canonicalization
# ============================================================================

# All raw.lca_disclosure columns expected as strings on input (pre-coercion).
# Wage and total_workers columns get coerced to numerics; date columns to
# date.

# Float-typed numerics: wages are NUMERIC(12,2) in Postgres; we hand them
# off as Float64 and let psycopg/Postgres do the decimal coercion.
_FLOAT_COLS: Final[tuple[str, ...]] = (
    "wage_rate_of_pay_from",
    "wage_rate_of_pay_to",
    "prevailing_wage",
)

# Integer-typed numerics: total_workers is INTEGER. Excel-via-fastexcel emits
# integer-valued cells as "1.0" strings; if we passed those through unchanged,
# Postgres' COPY would reject them with `invalid input syntax for type
# integer: "1.0"`. We strip currency, cast to Float64 (which tolerates "1.0"),
# round to nearest, and finally cast to Int64. We accept that this drops
# fractional values silently -- DOL never emits a fractional worker count, and
# rejecting on fractional would be too strict for legacy CSVs that have
# whitespace-padded ints.
_INT_COLS: Final[tuple[str, ...]] = (
    "total_workers",
)

_NUMERIC_COLS: Final[tuple[str, ...]] = _FLOAT_COLS + _INT_COLS

_DATE_COLS: Final[tuple[str, ...]] = (
    "received_date",
    "decision_date",
    "employment_start_date",
    "employment_end_date",
)

# DOL has shipped LCA dates in at least four wire formats over the years:
#   * "YYYY-MM-DD HH:MM:SS"  (modern xlsx via fastexcel/calamine)
#   * "YYYY-MM-DDTHH:MM:SS"  (some ISO variants)
#   * "YYYY-MM-DD"            (some CSV vintages, JSON exports)
#   * "M/D/YYYY"              (legacy CSV vintages, sometimes with a time tail)
# `format=None` strict=False cannot resolve all of them in one polars call,
# so we explicitly enumerate. Order matters only for performance: the first
# matching format wins via pl.coalesce.
_DATE_FORMATS: Final[tuple[str, ...]] = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y",
)


def _strip_currency(s: str | None) -> str | None:
    """Remove $ and thousands separators; preserve None and pass-through floats."""
    if s is None:
        return None
    return s.replace("$", "").replace(",", "").strip() or None


def _zero_pad_zip(s: str | None) -> str | None:
    """Return a 5-digit ZIP if *s* parses to one; None otherwise.

    Handles ZIP+4 (truncated to 5), ZIPs that lost their leading zero in
    Excel ("8901" -> "08901"), and unparseable values (returns None rather
    than raising; downstream allocator drops nulls explicitly).
    """
    if s is None:
        return None
    digits = "".join(c for c in s if c.isdigit())
    if not digits:
        return None
    truncated = digits[:5]
    return truncated.zfill(5) if len(truncated) <= 5 else None


def _coerce_types(df: pl.DataFrame) -> pl.DataFrame:
    """Coerce string columns to typed columns and apply normalizers.

    Column-by-column transforms:

    * numeric columns: strip $/commas, cast to Float64 (NULL on failure).
    * date columns: parse permissively (multiple formats, NULL on failure).
    * wage_unit_of_pay / pw_unit_of_pay: normalize via :data:`_WAGE_UNIT_NORMALIZER`.
    * case_status: normalize via :data:`_CASE_STATUS_NORMALIZER`.
    * visa_class: normalize via :data:`_VISA_CLASS_NORMALIZER` (defaults to 'OTHER').
    * worksite_postal_code: zero-pad / truncate to CHAR(5).
    * employer_canonical_name: NFKD + suffix-strip via :func:`canonical_employer_name`.
    """
    transforms: list[pl.Expr] = []

    for col in _FLOAT_COLS:
        if col in df.columns:
            transforms.append(
                pl.col(col)
                .map_elements(_strip_currency, return_dtype=pl.Utf8)
                .cast(pl.Float64, strict=False)
                .alias(col)
            )

    for col in _INT_COLS:
        if col in df.columns:
            transforms.append(
                pl.col(col)
                .map_elements(_strip_currency, return_dtype=pl.Utf8)
                .cast(pl.Float64, strict=False)
                .round(0)
                .cast(pl.Int64, strict=False)
                .alias(col)
            )

    for col in _DATE_COLS:
        if col in df.columns:
            candidates = [
                pl.col(col).str.strptime(pl.Date, format=fmt, strict=False)
                for fmt in _DATE_FORMATS
            ]
            transforms.append(pl.coalesce(candidates).alias(col))

    if "wage_unit_of_pay" in df.columns:
        transforms.append(
            pl.col("wage_unit_of_pay")
            .map_elements(_normalize_wage_unit, return_dtype=pl.Utf8)
            .alias("wage_unit_of_pay")
        )
    if "pw_unit_of_pay" in df.columns:
        transforms.append(
            pl.col("pw_unit_of_pay")
            .map_elements(_normalize_wage_unit, return_dtype=pl.Utf8)
            .alias("pw_unit_of_pay")
        )
    if "case_status" in df.columns:
        transforms.append(
            pl.col("case_status")
            .map_elements(_normalize_case_status, return_dtype=pl.Utf8)
            .alias("case_status")
        )
    if "visa_class" in df.columns:
        transforms.append(
            pl.col("visa_class")
            .map_elements(_normalize_visa_class, return_dtype=pl.Utf8)
            .alias("visa_class")
        )
    if "worksite_postal_code" in df.columns:
        transforms.append(
            pl.col("worksite_postal_code")
            .map_elements(_zero_pad_zip, return_dtype=pl.Utf8)
            .alias("worksite_postal_code")
        )
    if "employer_name" in df.columns:
        transforms.append(
            pl.col("employer_name")
            .map_elements(canonical_employer_name, return_dtype=pl.Utf8)
            .alias("employer_canonical_name")
        )
    for yn_col in ("h1b_dependent", "willful_violator", "secondary_entity"):
        if yn_col in df.columns:
            transforms.append(
                pl.col(yn_col)
                .map_elements(_normalize_yn, return_dtype=pl.Utf8)
                .alias(yn_col)
            )
    if "pw_wage_level" in df.columns:
        transforms.append(
            pl.col("pw_wage_level")
            .map_elements(_normalize_pw_level, return_dtype=pl.Utf8)
            .alias("pw_wage_level")
        )
    if "employer_fein" in df.columns:
        transforms.append(
            pl.col("employer_fein")
            .map_elements(_normalize_fein, return_dtype=pl.Utf8)
            .alias("employer_fein")
        )

    return df.with_columns(transforms)


# ============================================================================
# Public API
# ============================================================================


@dataclass(frozen=True)
class ParseResult:
    """Output of :func:`parse_lca_file` -- the canonical-shape DataFrame plus metadata."""

    dataframe: pl.DataFrame
    schema_version: str
    fiscal_year: int
    fiscal_quarter: int
    source_filename: str
    source_sha256: str
    n_input_rows: int
    n_output_rows: int


def parse_lca_file(path: Path) -> ParseResult:
    """Read, detect, project, unstack, and coerce a single LCA file.

    Pure function: takes a filesystem path, returns a :class:`ParseResult`
    whose ``dataframe`` matches the canonical ``raw.lca_disclosure`` shape
    (minus the GENERATED columns and minus the provenance columns added by
    :func:`stage_dataframe`).

    Raises:
        IngestError: on schema-version detection failure, fiscal-period
            extraction failure, or empty result.

    """
    fiscal_year, fiscal_quarter = parse_fiscal_period_from_filename(path.name)
    source_sha = sha256_file(path)
    raw_df = _read_raw(path)
    n_input = raw_df.height
    canonical_df = _canonicalize_columns(raw_df)

    version = detect_schema_version(canonical_df.columns, SIGNATURES)

    projected = (
        _project_v3_2018(canonical_df)
        if version == "v3_2018"
        else _project_simple(canonical_df, version)
    )
    coerced = _coerce_types(projected)
    n_output = coerced.height

    if n_output == 0:
        raise IngestError(f"Parsed zero rows from {path}; refusing to load.")

    return ParseResult(
        dataframe=coerced,
        schema_version=version,
        fiscal_year=fiscal_year,
        fiscal_quarter=fiscal_quarter,
        source_filename=path.name,
        source_sha256=source_sha,
        n_input_rows=n_input,
        n_output_rows=n_output,
    )


def stage_dataframe(parse: ParseResult) -> pl.DataFrame:
    """Add provenance columns and align to the destination ``raw.lca_disclosure`` schema.

    The returned DataFrame contains exactly the columns that the database
    accepts on INSERT (i.e. all CHECK-constrained columns minus the
    GENERATED annualized_* columns minus ``ingested_at`` which defaults
    server-side).
    """
    df = parse.dataframe.with_columns([
        pl.lit(parse.fiscal_year, dtype=pl.Int16).alias("fiscal_year"),
        pl.lit(parse.fiscal_quarter, dtype=pl.Int16).alias("fiscal_quarter"),
        pl.lit(parse.source_filename, dtype=pl.Utf8).alias("source_filename"),
        pl.lit(parse.source_sha256, dtype=pl.Utf8).alias("source_sha256"),
        pl.lit(parse.schema_version, dtype=pl.Utf8).alias("source_schema_version"),
        pl.lit("measured", dtype=pl.Utf8).alias("data_quality"),
    ])

    # Project to the exact destination column list, in order. Missing
    # columns (e.g. employer_naics from pre-v5 vintages) are emitted as null.
    destination_cols: tuple[str, ...] = (
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
        "employer_fein", "h1b_dependent", "willful_violator",
        "secondary_entity", "secondary_entity_business_name", "pw_wage_level",
        "source_filename", "source_sha256", "source_schema_version",
        "data_quality",
    )
    for col in destination_cols:
        if col not in df.columns:
            df = df.with_columns(pl.lit(None).cast(pl.Utf8).alias(col))
    return df.select(destination_cols)


def load_to_postgres(
    staged: pl.DataFrame,
    connection: psycopg.Connection,
    *,
    table: str = "raw.lca_disclosure",
) -> int:
    """Bulk-COPY *staged* into *table*. Returns the number of rows loaded.

    Uses psycopg3's CSV COPY protocol for high throughput. Wraps the COPY
    in the caller's transaction (we do NOT manage commit here -- the
    caller is responsible for transaction boundaries so multi-file loads
    can be atomic).
    """
    from psycopg import sql  # local: psycopg is an optional runtime dep for the parser

    buf = io.BytesIO()
    staged.write_csv(buf, include_header=False)
    buf.seek(0)

    if "." in table:
        schema_part, table_part = table.split(".", 1)
        ident = sql.Identifier(schema_part, table_part)
    else:
        ident = sql.Identifier(table)
    col_idents = sql.SQL(", ").join(sql.Identifier(c) for c in staged.columns)
    copy_query = sql.SQL("COPY {tbl} ({cols}) FROM STDIN WITH (FORMAT csv, NULL '')").format(
        tbl=ident, cols=col_idents,
    )

    n_rows_before = _count_rows(connection, ident)
    with connection.cursor().copy(copy_query) as cp:
        while chunk := buf.read(_COPY_CHUNK):
            cp.write(chunk)
    n_rows_after = _count_rows(connection, ident)
    return n_rows_after - n_rows_before


def _count_rows(connection: psycopg.Connection, ident: object) -> int:
    from psycopg import sql

    if not isinstance(ident, sql.Identifier):
        raise TypeError("ident must be a psycopg.sql.Identifier")
    cur = connection.execute(sql.SQL("SELECT count(*) FROM {}").format(ident))
    row = cur.fetchone()
    if row is None:
        return 0
    return int(row[0])


# ============================================================================
# CLI
# ============================================================================


@click.group()
def cli() -> None:
    """DOL OFLC LCA disclosure ingester (POP-2)."""


@cli.command("parse")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--head", default=5, show_default=True, help="Print this many sample rows.")
def parse_cmd(path: Path, head: int) -> None:
    """Parse an LCA file and print a summary. Does not touch the database."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = parse_lca_file(path)
    click.echo(
        f"file={result.source_filename}\n"
        f"fiscal_period=FY{result.fiscal_year}Q{result.fiscal_quarter}\n"
        f"schema_version={result.schema_version}\n"
        f"sha256={result.source_sha256}\n"
        f"input_rows={result.n_input_rows}\n"
        f"output_rows={result.n_output_rows}\n"
    )
    click.echo(result.dataframe.head(head))


# DOL serves quarterly LCA disclosure files at static, unauthenticated URLs.
# We do NOT attempt content discovery (DOL has changed naming conventions
# over the years and a misnamed 404 is a worse failure mode than asking the
# operator to specify FY/Q explicitly).
DOL_LCA_URL_TEMPLATE: Final[str] = (
    "https://www.dol.gov/sites/dolgov/files/ETA/oflc/pdfs/"
    "LCA_Disclosure_Data_FY{fy}_Q{q}.xlsx"
)

# DOL has published at least one quarterly file under a misspelled
# filename (FY2026 Q3: LCA_Dislclosure_Data_...). Try the typo after
# the canonical name 404s so fetch stays mechanical.
DOL_LCA_URL_TEMPLATE_TYPO: Final[str] = (
    "https://www.dol.gov/sites/dolgov/files/ETA/oflc/pdfs/"
    "LCA_Dislclosure_Data_FY{fy}_Q{q}.xlsx"
)


def build_dol_lca_url(fiscal_year: int, fiscal_quarter: int) -> str:
    """Return the canonical DOL OFLC LCA download URL for a fiscal period.

    DOL's URL convention is stable for FY2018+; older vintages used different
    file names (and live elsewhere). We restrict to the era we can mechanically
    reach without a manual catalog.
    """
    if fiscal_year < 2018:
        raise IngestError(
            f"Auto-fetch supports FY2018+ only (requested FY{fiscal_year}); "
            "older vintages must be operator-staged from the DOL archive."
        )
    if fiscal_quarter not in (1, 2, 3, 4):
        raise IngestError(f"Invalid fiscal_quarter {fiscal_quarter!r}; expected 1..4.")
    return DOL_LCA_URL_TEMPLATE.format(fy=fiscal_year, q=fiscal_quarter)


def build_dol_lca_url_candidates(fiscal_year: int, fiscal_quarter: int) -> tuple[str, ...]:
    """Canonical URL first, then DOL typo / FY-subdir / Drupal-media variants.

    FY2026 Q3 published under ``.../pdfs/FY26Q3/`` and a ``/media/`` alias;
    the page label still uses the historic ``LCA_Dislclosure_`` typo.
    """
    fy = fiscal_year
    q = fiscal_quarter
    yy = fy % 100
    return (
        build_dol_lca_url(fy, q),
        DOL_LCA_URL_TEMPLATE_TYPO.format(fy=fy, q=q),
        (
            "https://www.dol.gov/sites/dolgov/files/ETA/oflc/pdfs/"
            f"FY{yy}Q{q}/LCA_Disclosure_Data_FY{fy}_Q{q}.xlsx"
        ),
        (
            "https://www.dol.gov/sites/dolgov/files/ETA/oflc/pdfs/"
            f"FY{yy}Q{q}/LCA_Dislclosure_Data_FY{fy}_Q{q}.xlsx"
        ),
        f"https://www.dol.gov/media/LCA_Disclosure_Data_FY{fy}_Q{q}.xlsx",
    )


def fetch_dol_lca_file(
    fiscal_year: int,
    fiscal_quarter: int,
    *,
    out_dir: Path,
    overwrite: bool = False,
    timeout_s: float = 600.0,
) -> Path:
    """Download a DOL OFLC LCA quarterly disclosure file by fiscal year/quarter.

    Returns the local path. If the file already exists at the destination
    and *overwrite* is False, returns the existing path unchanged (skipping
    the download). The destination filename is the canonical
    ``LCA_Disclosure_Data_FY{fy}_Q{q}.xlsx``.

    Raises :class:`IngestError` on HTTP non-2xx (treated as authoritative
    "no such file") so that the caller can distinguish unavailable
    vintages from transient network errors.

    Notes
    -----
    DOL ships these files via a CloudFront CDN (no auth, no CSRF, no
    rate limiting we have observed at our volumes). The auto-fetch path
    is therefore safe to call interactively. For batch backfills,
    operator-staged downloads remain a valid alternative.

    """
    import httpx

    urls = build_dol_lca_url_candidates(fiscal_year, fiscal_quarter)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"LCA_Disclosure_Data_FY{fiscal_year}_Q{fiscal_quarter}.xlsx"
    if dest.exists() and not overwrite:
        log.info("Skipping fetch (already present): %s", dest)
        return dest

    last_404: str | None = None
    headers = {
        "User-Agent": (
            "NJ-Unchained/1.0 (civic-integrity research; "
            "+https://github.com/dantebozzuti27/NJ_unchained)"
        )
    }
    with httpx.Client(timeout=timeout_s, follow_redirects=True, headers=headers) as client:
        for url in urls:
            log.info("Fetching DOL LCA file: %s", url)
            with client.stream("GET", url) as resp:
                if resp.status_code == 404:
                    last_404 = url
                    continue
                resp.raise_for_status()
                tmp = dest.with_suffix(dest.suffix + ".part")
                with tmp.open("wb") as fh:
                    for chunk in resp.iter_bytes(chunk_size=1 << 20):  # 1 MiB
                        fh.write(chunk)
                tmp.rename(dest)
                log.info(
                    "Downloaded %s (%.1f MiB)", dest, dest.stat().st_size / (1 << 20)
                )
                return dest
    raise IngestError(
        f"DOL has no file at {urls[0]} (HTTP 404"
        + (f"; typo variant also 404: {last_404}" if last_404 else "")
        + "). Either the fiscal period has not yet published, or the "
        "URL convention has changed. Check the DOL OFLC performance-data page."
    )


@cli.command("fetch")
@click.option("--fiscal-year", type=int, required=True,
              help="Fiscal year (e.g. 2024). FY2018+ only.")
@click.option("--fiscal-quarter", type=int, required=True,
              help="Fiscal quarter (1..4).")
@click.option("--out-dir", type=click.Path(file_okay=False, path_type=Path),
              default=Path("data/manual/dol_oflc_lca"), show_default=True,
              help="Where to write the downloaded file.")
@click.option("--overwrite", is_flag=True, default=False,
              help="Re-download even if the file already exists locally.")
def fetch_cmd(
    fiscal_year: int, fiscal_quarter: int, out_dir: Path, overwrite: bool,
) -> None:
    """Download a single quarterly LCA file from DOL's static URL."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    dest = fetch_dol_lca_file(
        fiscal_year, fiscal_quarter, out_dir=out_dir, overwrite=overwrite,
    )
    click.echo(str(dest))


@cli.command("load")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--dsn", envvar="PG_DSN", required=True,
              help="Postgres DSN (or set PG_DSN env var).")
@click.option("--dry-run", is_flag=True, default=False,
              help="Parse and stage but do not COPY into Postgres.")
@click.option(
    "--nj-only",
    is_flag=True,
    default=False,
    help="Keep only rows whose worksite_state or employer_state is NJ "
    "(required for Neon free-tier storage).",
)
@click.option(
    "--replace",
    is_flag=True,
    default=False,
    help="DELETE the (fiscal_year, fiscal_quarter) slice before COPY "
    "so a re-ingest with new attestation columns replaces the old rows.",
)
def load_cmd(path: Path, dsn: str, dry_run: bool, nj_only: bool, replace: bool) -> None:
    """Parse + stage + load an LCA file into ``raw.lca_disclosure``."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = parse_lca_file(path)
    staged = stage_dataframe(result)
    if nj_only:
        before = staged.height
        staged = staged.filter(
            (pl.col("worksite_state").str.to_uppercase() == "NJ")
            | (pl.col("employer_state").str.to_uppercase() == "NJ")
        )
        log.info("NJ filter: %d -> %d rows", before, staged.height)
    log.info(
        "Staged %d rows from %s (schema %s, FY%dQ%d)",
        staged.height, path.name, result.schema_version,
        result.fiscal_year, result.fiscal_quarter,
    )
    if dry_run:
        click.echo("--dry-run: skipping COPY.")
        return

    import psycopg

    with psycopg.connect(dsn) as conn:
        if replace:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM raw.lca_disclosure "
                    "WHERE fiscal_year = %s AND fiscal_quarter = %s",
                    (result.fiscal_year, result.fiscal_quarter),
                )
                log.info(
                    "Replaced FY%dQ%d: deleted %d existing rows",
                    result.fiscal_year,
                    result.fiscal_quarter,
                    cur.rowcount,
                )
        n = load_to_postgres(staged, conn)
        conn.commit()
        click.echo(f"Loaded {n} rows into raw.lca_disclosure.")
        # Best-effort dataset_health signal so the platform notices.
        import json
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO governance.dataset_health "
                "(dataset_id, signal_name, severity, metric_value, metric_unit, details) "
                "VALUES (%s, %s, %s, %s, %s, %s::jsonb)",
                (
                    "raw.lca_disclosure",
                    "rows_loaded",
                    "info",
                    n,
                    "rows",
                    json.dumps({
                        "file": path.name,
                        "schema_version": result.schema_version,
                        "fiscal_year": result.fiscal_year,
                        "fiscal_quarter": result.fiscal_quarter,
                    }),
                ),
            )
        conn.commit()
