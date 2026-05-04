"""Compute weighted segmented housing burden ratios from ACS PUMS.

This module is the analytical core of Tier 3. It takes raw PUMS person
+ housing DataFrames and emits the long-format
``derived.pums_burden_segmented`` rows.

Methodology
-----------
For each cell ``(puma, year, tenure_class, segment_dim, segment_value)``:

1. Filter person rows to the cell's demographic intersection.
2. JOIN to housing records via SERIALNO (one housing row per household;
   one person row per individual; many persons share a household).
3. Compute weighted aggregates:
   * ``weighted_n``           = SUM(pwgtp) over persons in the cell.
   * ``sample_n``             = COUNT(*) over persons (un-weighted).
   * ``household_income_p50`` = weighted_percentile(HINCP, WGTP, 0.5)
     -- joined per-household, NOT per-person, because HINCP is a
     household-level measurement.
   * ``monthly_cost_p50``     = weighted_percentile(GRNTP or SMOCP,
     WGTP, 0.5).
   * ``burden_ratio_p50``     = (monthly_cost_p50 * 12) / household_income_p50.

4. Suppression: if ``weighted_n < SUPPRESSION_FLOOR``, set burden ratios
   to NULL and mark ``suppressed=TRUE``. This mirrors ACS / Census
   disclosure-avoidance practice.

Why median of weighted medians, not weighted mean?
--------------------------------------------------
* Median-of-medians is what HUD and Census report for headline burden
  numbers. Matching their methodology means cross-checking against
  published tables is straightforward.
* Mean is dominated by top-coded high-income households (PUMS top-codes
  PINCP/HINCP at the state-specific 99.5th percentile and replaces with
  the state median above that cap). Median is robust to top-coding.
* Weighted median respects the sampling design; un-weighted median
  ignores PUMA-level sampling intensity differences.

Why compute burden as median_cost / median_income, not median of ratios?
------------------------------------------------------------------------
The ratio of medians is the standard published statistic. Median of
ratios is harder to interpret (it is not the burden of the median
household; it is the median burden across households, which is
sample-design-dependent). Also: ratio of medians has trivial
suppression semantics (suppress if either input is suppressed); median
of ratios requires per-row imputation when income is zero/negative.

PUMA grain
----------
We expose at PUMA grain because PUMS reports PUMA, not county. PUMA-
to-county allocation lives in a separate concern (future
``ref.puma_county_xwalk``). Computing at PUMA grain preserves
maximum precision; downstream consumers can roll up via the crosswalk
when it lands.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import polars as pl

from derived._stats import (
    ratio_of_percentiles_sdr,
    weighted_percentile_sdr,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    import psycopg

log = logging.getLogger(__name__)


# ============================================================================
# Methodology constants
# ============================================================================


# Cells with weighted population estimate < this floor are suppressed.
# 1000 mirrors the ACS Detailed Tables suppression threshold for small
# geographies. Increase to 5000 for higher-confidence headline numbers
# at the cost of more NULLs; decrease to 500 only if you accept higher
# variance.
SUPPRESSION_FLOOR: Final[int] = 1000

# Burden ratio cap. Anything above this is almost certainly an artifact
# of one of: top-coded income at $1, divide-by-zero, or coding error.
# Cells with computed ratio > this are coerced to suppressed.
BURDEN_RATIO_SANITY_CAP: Final[float] = 5.0

# Formula version. Bump when methodology changes; old rows remain in
# place via the (..., formula_version) primary key extension... actually,
# the schema's PK is (..., segment_dim, segment_value), not formula
# version. Old rows are TRUNCATEd on each materialization. If we ever
# want methodology-diff queries, change the PK to include formula_version.
FORMULA_VERSION: Final[str] = "v0.1"


# ============================================================================
# Demographic recodes
# ============================================================================
#
# PUMS uses numeric codes for race, citizenship, etc. The aggregator
# recodes to short string buckets so downstream queries are readable.
# Bucket boundaries are documented inline; changing them requires
# bumping FORMULA_VERSION above so cross-vintage diffs are tractable.
# ============================================================================


# RAC1P -> race_class. PUMS RAC1P codes:
#   1 = White alone
#   2 = Black or African American alone
#   3 = American Indian alone
#   4 = Alaska Native alone
#   5 = AIAN tribes specified or not specified, in combination
#   6 = Asian alone
#   7 = Native Hawaiian and Other Pacific Islander alone
#   8 = Some Other Race alone
#   9 = Two or More Races
RAC1P_TO_RACE_CLASS: Final[dict[int, str]] = {
    1: "white",
    2: "black",
    3: "aian",
    4: "aian",
    5: "aian",
    6: "asian",
    7: "nhpi",
    8: "other",
    9: "two_or_more",
}


# HISP -> hispanic_class. HISP=1 means "Not Spanish/Hispanic/Latino";
# 2-24 are specific Hispanic origin codes.
def _recode_hispanic(hisp: int | None) -> str | None:
    if hisp is None:
        return None
    return "not_hispanic" if hisp == 1 else "hispanic"


# CIT -> citizenship_class. PUMS CIT codes:
#   1 = Born in US
#   2 = Born in US territories
#   3 = Born abroad of US-citizen parents
#   4 = Naturalized US citizen
#   5 = Not a US citizen
CIT_TO_CITIZENSHIP_CLASS: Final[dict[int, str]] = {
    1: "us_born",
    2: "us_born",
    3: "us_born",
    4: "naturalized",
    5: "non_citizen",
}


# AGEP -> age_band.
def _recode_age_band(agep: int | None) -> str | None:
    if agep is None:
        return None
    if agep < 25:
        return "<25"
    if agep < 35:
        return "25-34"
    if agep < 45:
        return "35-44"
    if agep < 55:
        return "45-54"
    if agep < 65:
        return "55-64"
    return "65+"


# TEN -> tenure_class. PUMS TEN codes:
#   1 = Owned with mortgage
#   2 = Owned free and clear
#   3 = Rented
#   4 = Occupied without payment of rent (DROPPED -- no burden ratio)
TEN_TO_TENURE_CLASS: Final[dict[int, str]] = {
    1: "owner_w_mtg",
    2: "owner_no_mtg",
    3: "renter",
}


# ============================================================================
# Compute
# ============================================================================


@dataclass(frozen=True)
class CellSpec:
    """Identifies one (puma, year, tenure, segment_dim, segment_value) cell."""

    year:           int
    product:        str
    state_fips:     str
    puma:           str
    tenure_class:   str
    segment_dim:    str
    segment_value:  str


def _build_household_frame(
    person_df: pl.DataFrame, housing_df: pl.DataFrame,
) -> pl.DataFrame:
    """Join PERSON to HOUSING and produce the household-level analytical frame.

    Returns a frame with one row per (year, product, serialno) -- i.e.,
    one row per household -- carrying:
      * year, product, state_fips, puma                (geography/key)
      * tenure_class                                   (recoded from TEN)
      * monthly_cost                                   (GRNTP for renters,
                                                        SMOCP for owners)
      * household_income                               (HINCP)
      * wgtp                                           (housing weight)

    Plus one row per PERSON in that household, denormalized:
      * race_class, hispanic_class, citizenship_class, age_band, pwgtp

    The shape is "person rows joined to their household". Aggregation
    later groups by household for income/cost percentiles (using wgtp)
    and by person for demographic splits + weighted_n (using pwgtp).
    """
    # Recode person-level demographics
    person_recoded = person_df.with_columns(
        pl.col("rac1p").replace_strict(RAC1P_TO_RACE_CLASS, default=None)
            .alias("race_class"),
        pl.col("hisp").map_elements(_recode_hispanic, return_dtype=pl.Utf8)
            .alias("hispanic_class"),
        pl.col("cit").replace_strict(CIT_TO_CITIZENSHIP_CLASS, default=None)
            .alias("citizenship_class"),
        pl.col("agep").map_elements(_recode_age_band, return_dtype=pl.Utf8)
            .alias("age_band"),
    )

    # Recode housing tenure + select monthly cost column. Rename
    # the housing replicate-weights array to ``wgtp_replicates`` so it
    # does not collide with the person-level ``replicate_weights`` after
    # the join.
    housing_recoded = housing_df.with_columns(
        pl.col("ten").replace_strict(TEN_TO_TENURE_CLASS, default=None)
            .alias("tenure_class"),
        # Renters: GRNTP. Owners: SMOCP. We pick whichever is non-null;
        # if both are populated (rare), GRNTP wins because TEN=3 is
        # reliably "renter" in that case.
        pl.when(pl.col("ten") == 3)
          .then(pl.col("grntp"))
          .when(pl.col("ten").is_in([1, 2]))
          .then(pl.col("smocp"))
          .otherwise(None)
          .alias("monthly_cost"),
    ).rename({"replicate_weights": "wgtp_replicates"})

    # JOIN. The shape is intentionally "many persons per household";
    # group-by aggregates downstream choose the right weight column.
    # We propagate ``wgtp_replicates`` (housing 80-vector) into the
    # joined frame so cell aggregators can compute SDR variances on
    # household-level statistics (income, cost, burden ratio).
    return person_recoded.join(
        housing_recoded.select([
            "year", "product", "serialno",
            "tenure_class", "monthly_cost", "hincp", "wgtp",
            "wgtp_replicates",
        ]),
        on=["year", "product", "serialno"],
        how="inner",
    )


def _aggregate_one_cell(
    cell_df: pl.DataFrame,
    *,
    year: int, product: str, state_fips: str, puma: str,
    tenure_class: str, segment_dim: str, segment_value: str,
) -> dict[str, Any]:
    """Compute the row for one cell.

    Returns a dict with all columns required by derived.pums_burden_segmented.
    Suppression is applied here based on weighted_n < SUPPRESSION_FLOOR.

    Standard errors (``*_se`` columns) are computed via SDR using the
    80 replicate weights stored in ``wgtp_replicates``. When the cell
    is suppressed, all SE columns are NULL.
    """
    pwgtp = cell_df["pwgtp"]
    weighted_n = int(pwgtp.sum() or 0)
    sample_n = cell_df.height

    suppressed = weighted_n < SUPPRESSION_FLOOR

    # Dedupe to one row per household so income/cost percentiles weight
    # each household once (not once per person).
    hh_unique = cell_df.unique(subset=["serialno"], keep="first")
    incomes  = hh_unique["hincp"].to_list()
    costs    = hh_unique["monthly_cost"].to_list()
    wgtp     = hh_unique["wgtp"].to_list()
    wgtp_rep = hh_unique["wgtp_replicates"].to_list()

    income_p50:    float | None = None
    income_p50_se: float | None = None
    cost_p50:      float | None = None
    cost_p50_se:   float | None = None
    ratio_p50:     float | None = None
    ratio_p50_se:  float | None = None

    if not suppressed:
        income_p50, income_p50_se = weighted_percentile_sdr(
            incomes, wgtp, wgtp_rep, q=0.5,
        )
        cost_p50, cost_p50_se = weighted_percentile_sdr(
            costs, wgtp, wgtp_rep, q=0.5,
        )
        ratio_p50, ratio_p50_se = ratio_of_percentiles_sdr(
            costs, incomes, wgtp, wgtp_rep, numer_multiplier=12.0,
        )
        if ratio_p50 is not None:
            ratio_p50 = round(ratio_p50, 4)
            if ratio_p50 > BURDEN_RATIO_SANITY_CAP:
                # Suppress; almost certainly a numerical artifact
                # (top-coded $1 income, divide-by-zero, etc.).
                ratio_p50 = None
                ratio_p50_se = None
                income_p50 = None
                income_p50_se = None
                cost_p50 = None
                cost_p50_se = None
                suppressed = True
            elif ratio_p50_se is not None:
                ratio_p50_se = round(ratio_p50_se, 4)

    return {
        "year":                    year,
        "product":                 product,
        "state_fips":              state_fips,
        "puma":                    puma,
        "tenure_class":            tenure_class,
        "segment_dim":             segment_dim,
        "segment_value":           segment_value,
        "weighted_n":              weighted_n,
        "sample_n":                sample_n,
        "household_income_p50":    float(income_p50) if income_p50 is not None else None,
        "household_income_p50_se": float(income_p50_se) if income_p50_se is not None else None,
        "monthly_cost_p50":        float(cost_p50) if cost_p50 is not None else None,
        "monthly_cost_p50_se":     float(cost_p50_se) if cost_p50_se is not None else None,
        "burden_ratio_p50":        ratio_p50,
        "burden_ratio_p50_se":     ratio_p50_se,
        "suppressed":              suppressed,
    }


# Segment dimensions to compute. For each (segment_dim, segment_value
# expression), we compute one cell per (puma, year, tenure_class).
# 'overall' is special: it's the no-split baseline.
_SEGMENT_DIMS: Final[tuple[tuple[str, str], ...]] = (
    ("overall",     "overall"),  # special-cased below
    ("race",        "race_class"),
    ("hispanic",    "hispanic_class"),
    ("citizenship", "citizenship_class"),
    ("age_band",    "age_band"),
)


def compute_burden_segmented(
    person_df: pl.DataFrame,
    housing_df: pl.DataFrame,
    *,
    year: int,
    product: str = "acs1",
    state_fips: str = "34",
) -> pl.DataFrame:
    """Compute the full segmented burden table for one (year, product, state).

    Args:
        person_df:   raw.acs_pums_person rows for this state.
        housing_df:  raw.acs_pums_housing rows for the same state/year/product.
        year:        Survey year.
        product:     'acs1' or 'acs5'.
        state_fips:  2-char state FIPS. Filter is applied to both inputs.

    Returns:
        Long-format DataFrame matching derived.pums_burden_segmented schema.
        One row per (puma, year, tenure_class, segment_dim, segment_value).
        Includes formula_version and input_vintage_hash columns.

    """
    if person_df.height == 0 or housing_df.height == 0:
        return pl.DataFrame(schema=_RESULT_SCHEMA)

    # Filter to (state, year, product). Inputs may carry additional vintages;
    # the derived layer is per-(year, product).
    person_df = person_df.filter(
        (pl.col("state_fips") == state_fips)
        & (pl.col("year") == year)
        & (pl.col("product") == product)
    )
    housing_df = housing_df.filter(
        (pl.col("state_fips") == state_fips)
        & (pl.col("year") == year)
        & (pl.col("product") == product)
    )

    # Filter to puma_vintage='2020'. PUMA10 records (5-Year files spanning
    # the 2020 decennial revision) live in different geographies and
    # cannot be aggregated under the same puma key without a separate
    # 2010-vintage reference layer. We track the count of dropped rows
    # so the operator can audit the analytical loss.
    if "puma_vintage" in person_df.columns:
        n_p_pre  = person_df.height
        n_h_pre  = housing_df.height
        person_df  = person_df.filter(pl.col("puma_vintage") == "2020")
        housing_df = housing_df.filter(pl.col("puma_vintage") == "2020")
        dropped_p = n_p_pre - person_df.height
        dropped_h = n_h_pre - housing_df.height
        if dropped_p > 0 or dropped_h > 0:
            log.info(
                "compute_burden_segmented: dropped puma_vintage!='2020' rows: "
                "person=%d, housing=%d (year=%d product=%s). Add a 2010 "
                "crosswalk + dual-vintage compute to recover these.",
                dropped_p, dropped_h, year, product,
            )

    if person_df.height == 0 or housing_df.height == 0:
        return pl.DataFrame(schema=_RESULT_SCHEMA)

    hh_frame = _build_household_frame(person_df, housing_df)

    # Drop rows we cannot place into a tenure class (TEN=4 occupied
    # without rent, or NULL).
    hh_frame = hh_frame.filter(pl.col("tenure_class").is_not_null())

    # Compute input vintage hash from the source byte streams.
    # Prefer the source_sha256 columns set by the ingester; fall back
    # to a hash of (year, product, n_persons, n_housing) for tests
    # where source_sha256 is synthetic.
    input_hash = _compute_input_vintage_hash(person_df, housing_df)

    rows: list[dict[str, Any]] = []
    pumas = hh_frame["puma"].unique().sort().to_list()
    tenures = list(TEN_TO_TENURE_CLASS.values())

    for puma in pumas:
        puma_frame = hh_frame.filter(pl.col("puma") == puma)
        for tenure in tenures:
            tenure_frame = puma_frame.filter(pl.col("tenure_class") == tenure)
            if tenure_frame.height == 0:
                continue

            for segment_dim, segment_col in _SEGMENT_DIMS:
                if segment_dim == "overall":
                    rows.append(_aggregate_one_cell(
                        tenure_frame,
                        year=year, product=product, state_fips=state_fips,
                        puma=puma, tenure_class=tenure,
                        segment_dim="overall", segment_value="overall",
                    ))
                    continue

                # Group by the segment column; one cell per non-null value.
                values = (
                    tenure_frame[segment_col].drop_nulls().unique().sort().to_list()
                )
                for val in values:
                    cell_df = tenure_frame.filter(pl.col(segment_col) == val)
                    rows.append(_aggregate_one_cell(
                        cell_df,
                        year=year, product=product, state_fips=state_fips,
                        puma=puma, tenure_class=tenure,
                        segment_dim=segment_dim, segment_value=val,
                    ))

    if not rows:
        return pl.DataFrame(schema=_RESULT_SCHEMA)

    out = pl.DataFrame(rows, schema=_RESULT_SCHEMA_NO_PROVENANCE)
    return out.with_columns(
        pl.lit(FORMULA_VERSION).alias("formula_version"),
        pl.lit(input_hash).alias("input_vintage_hash"),
    )


def _compute_input_vintage_hash(
    person_df: pl.DataFrame, housing_df: pl.DataFrame,
) -> str:
    """SHA256 over the upstream source bytes that produced these rows.

    PUMS rows carry source_sha256 from the ingester (one hash per
    fetched ZIP). We hash the SET of distinct source_sha256 values
    across the two frames, so re-running the aggregator on identical
    inputs produces a byte-identical input_vintage_hash.

    For synthetic test data without source_sha256, falls back to a
    hash of (n_person, n_housing).
    """
    h = hashlib.sha256()
    if "source_sha256" in person_df.columns:
        for sha in sorted(person_df["source_sha256"].unique().to_list()):
            h.update(sha.encode("utf-8"))
    if "source_sha256" in housing_df.columns:
        for sha in sorted(housing_df["source_sha256"].unique().to_list()):
            h.update(sha.encode("utf-8"))
    h.update(f"|n_p={person_df.height}|n_h={housing_df.height}".encode())
    return h.hexdigest()


# ============================================================================
# Schema
# ============================================================================


# Polars schema for the output. Order matches derived.pums_burden_segmented
# columns (which is also the COPY order).
_RESULT_SCHEMA_NO_PROVENANCE: Final[dict[str, Any]] = {
    "year":                    pl.Int16,
    "product":                 pl.Utf8,
    "state_fips":              pl.Utf8,
    "puma":                    pl.Utf8,
    "tenure_class":            pl.Utf8,
    "segment_dim":             pl.Utf8,
    "segment_value":           pl.Utf8,
    "weighted_n":              pl.Int32,
    "sample_n":                pl.Int32,
    "household_income_p50":    pl.Float64,
    "household_income_p50_se": pl.Float64,
    "monthly_cost_p50":        pl.Float64,
    "monthly_cost_p50_se":     pl.Float64,
    "burden_ratio_p50":        pl.Float64,
    "burden_ratio_p50_se":     pl.Float64,
    "suppressed":              pl.Boolean,
}

_RESULT_SCHEMA: Final[dict[str, Any]] = {
    **_RESULT_SCHEMA_NO_PROVENANCE,
    "formula_version":    pl.Utf8,
    "input_vintage_hash": pl.Utf8,
}


# ============================================================================
# Load
# ============================================================================


def load_to_postgres(
    df: pl.DataFrame, connection: psycopg.Connection,
    *, year: int, product: str,
) -> int:
    """TRUNCATE the (year, product) slice + bulk-INSERT the new rows.

    We use DELETE + INSERT (not UPSERT) because:
      * The materialization is full -- a recompute always replaces
        every row for a given (year, product).
      * INSERTs are simpler than UPSERTs against a 6-column primary key.
      * Idempotent: re-running with identical inputs produces identical
        rows (same formula_version + input_vintage_hash) so a no-op
        DELETE+INSERT is fine.

    Wraps both statements in the caller's transaction.
    """
    if df.height == 0:
        log.warning("compute_burden_segmented produced 0 rows for year=%d product=%s",
                    year, product)
        return 0

    cols = list(df.columns)
    placeholders = ", ".join(["%s"] * len(cols))
    col_list = ", ".join(cols)
    insert_sql = (
        f"INSERT INTO derived.pums_burden_segmented ({col_list}) "
        f"VALUES ({placeholders})"
    )

    with connection.cursor() as cur:
        cur.execute(
            "DELETE FROM derived.pums_burden_segmented "
            "WHERE year = %s AND product = %s",
            (year, product),
        )
        # iter_rows preserves column order
        rows_iter: Iterable[tuple[Any, ...]] = df.iter_rows()
        cur.executemany(insert_sql, list(rows_iter))
    return df.height
