"""Compute county-grain segmented housing burden from ACS PUMS.

This is the county counterpart to ``derived.pums_burden`` (which computes
at PUMA grain). The crucial methodological point:

    We do **not** roll up PUMA-level medians to county. Median-of-medians
    is statistically invalid -- two PUMAs with median income $50K and
    $80K do NOT have a combined median of $65K. We **re-aggregate from
    raw PUMS**, allocating each person's PWGTP across counties via the
    population-weighted crosswalk(s).

DUAL-VINTAGE DISPATCH
---------------------
ACS 5-Year files span samples drawn before AND after the 2020 decennial
PUMA revision, so a single 5-Year vintage can carry rows tagged with
either ``puma_vintage='2010'`` or ``puma_vintage='2020'``. We load BOTH
crosswalks (``ref.puma2010_county_xwalk`` and ``ref.puma2020_county_xwalk``)
and dispatch each row to the right one by joining on
``(state_fips, puma, puma_vintage)``. County FIPS codes are decennial-
stable, so the aggregation key downstream is unchanged -- 2010-vintage
PUMA10=02500 records and 2020-vintage PUMA20=02501 records both feed
into county_fips=34033 (Salem) with their respective allocation
factors.

For a single-county PUMA (allocation_factor = 1.0), the computation is
identical to what ``derived.pums_burden`` would produce, just grouped
by county. For a multi-county PUMA, each PUMS household contributes a
fractional weight to each county the PUMA spans.

Compute cost: roughly 2x of ``derived.pums_burden`` because we do the
percentile work again at a different grain. For NJ this is well under
a second; not worth caching.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

import polars as pl

from derived._stats import (
    ratio_of_percentiles_sdr,
    weighted_percentile_sdr,
)
from derived.pums_burden import (
    BURDEN_RATIO_SANITY_CAP,
    FORMULA_VERSION,
    SUPPRESSION_FLOOR,
    TEN_TO_TENURE_CLASS,
    _build_household_frame,
    _compute_input_vintage_hash,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    import psycopg

log = logging.getLogger(__name__)


# Segment dimensions to compute. Mirrors derived.pums_burden._SEGMENT_DIMS.
# Kept private here (not imported) because tying both modules to a single
# tuple would create a hidden coupling -- if we ever want county-only or
# PUMA-only segments, we should be able to evolve them independently.
_SEGMENT_DIMS: Final[tuple[tuple[str, str], ...]] = (
    ("overall",     "overall"),
    ("race",        "race_class"),
    ("hispanic",    "hispanic_class"),
    ("citizenship", "citizenship_class"),
    ("age_band",    "age_band"),
)


# ============================================================================
# Compute
# ============================================================================


def _allocate_to_counties(
    hh_frame: pl.DataFrame,
    xwalk_df: pl.DataFrame,
) -> pl.DataFrame:
    """Join the household frame to the PUMA-county crosswalk.

    The join is keyed on ``(state_fips, puma, puma_vintage)``. The
    crosswalk DataFrame must carry a ``puma_vintage`` column tagging
    each row as ``'2010'`` or ``'2020'`` so PUMA codes that happen to
    collide across decennial revisions (e.g., ``00400`` exists in both
    vintages but maps to different counties) cannot be cross-allocated.

    Input ``hh_frame`` has one row per (person, household) pair carrying
    pwgtp, wgtp, PUMA, and ``puma_vintage``. Output has one row per
    (person, household, county) triple carrying:

      * county_fips                 -- new geography column
      * allocation_factor           -- crosswalk weight (in [0, 1])
      * pwgtp_alloc = pwgtp * f     -- allocated person weight
      * wgtp_alloc  = wgtp  * f     -- allocated housing weight

    For a single-county PUMA, allocation_factor = 1.0 so the allocated
    weights equal the original weights and the row count is unchanged.
    For a multi-county PUMA, each input row produces N output rows, one
    per county the PUMA spans, with fractional allocated weights summing
    to the original weight.

    Note on dtype: pwgtp/wgtp are INT32 in the input; multiplied by a
    float allocation factor they become FLOAT64. ``weighted_percentile``
    handles floats natively, and the ``int(sum(...))`` cast at the end
    of ``_aggregate_one_county_cell`` produces a clean integer for
    storage.
    """
    return hh_frame.join(
        xwalk_df.select([
            pl.col("state_fips"),
            pl.col("puma"),
            pl.col("puma_vintage"),
            pl.col("county_fips"),
            pl.col("allocation_factor").cast(pl.Float64),
        ]),
        on=["state_fips", "puma", "puma_vintage"],
        how="inner",
    ).with_columns(
        (pl.col("pwgtp") * pl.col("allocation_factor")).alias("pwgtp_alloc"),
        (pl.col("wgtp")  * pl.col("allocation_factor")).alias("wgtp_alloc"),
    )


def _allocate_replicate_row(
    rep: list[float] | None, alloc: float,
) -> list[float] | None:
    """Multiply each replicate weight by the row's allocation factor.

    Returns None if the input is None (defensive; PUMS rows always
    carry an 80-vector). For single-county PUMAs (alloc = 1.0) the
    output is identical to the input apart from int->float coercion.
    """
    if rep is None:
        return None
    return [w * alloc for w in rep]


def _aggregate_one_county_cell(
    cell_df: pl.DataFrame,
    *,
    year: int, product: str, state_fips: str, county_fips: str,
    tenure_class: str, segment_dim: str, segment_value: str,
) -> dict[str, Any]:
    """Compute the row for one county-cell.

    Mirror of ``_aggregate_one_cell`` in ``derived.pums_burden`` but
    operates on allocated weights and allocated replicate weights.
    Adds ``n_pumas_contributing`` for transparency about the
    allocation.

    For multi-county PUMAs the SE picks up the additional uncertainty
    from the allocation step naturally: each replicate's allocated
    weight is ``replicate_w * allocation_factor``, so the SDR variance
    formula sees the same fractional weighting Census would apply if
    PUMS published at county grain directly.
    """
    pwgtp_alloc = cell_df["pwgtp_alloc"]
    weighted_n = round(pwgtp_alloc.sum() or 0.0)
    sample_n = cell_df.height
    # Count distinct (puma, puma_vintage) pairs, not bare PUMA codes:
    # 63 of 73 NJ PUMA10 codes collide with PUMA20 codes despite covering
    # different geography. Counting bare codes would silently undercount
    # contributions in those cells.
    n_pumas = (
        cell_df.select(["puma", "puma_vintage"]).unique().height
        if "puma_vintage" in cell_df.columns
        else cell_df["puma"].n_unique()
    )

    suppressed = weighted_n < SUPPRESSION_FLOOR

    hh_unique = cell_df.unique(subset=["serialno"], keep="first")
    incomes  = hh_unique["hincp"].to_list()
    costs    = hh_unique["monthly_cost"].to_list()
    wgtp_a   = hh_unique["wgtp_alloc"].to_list()
    wgtp_rep = hh_unique["wgtp_replicates"].to_list()
    alloc    = hh_unique["allocation_factor"].to_list()

    # Apply allocation factor to each replicate weight vector. For
    # single-county PUMAs (alloc = 1.0) this is a no-op.
    wgtp_rep_alloc: list[list[float] | None] = [
        _allocate_replicate_row(rw, a) for rw, a in zip(wgtp_rep, alloc, strict=True)
    ]

    income_p50:    float | None = None
    income_p50_se: float | None = None
    cost_p50:      float | None = None
    cost_p50_se:   float | None = None
    ratio_p50:     float | None = None
    ratio_p50_se:  float | None = None

    if not suppressed:
        income_p50, income_p50_se = weighted_percentile_sdr(
            incomes, wgtp_a, wgtp_rep_alloc, q=0.5,
        )
        cost_p50, cost_p50_se = weighted_percentile_sdr(
            costs, wgtp_a, wgtp_rep_alloc, q=0.5,
        )
        ratio_p50, ratio_p50_se = ratio_of_percentiles_sdr(
            costs, incomes, wgtp_a, wgtp_rep_alloc, numer_multiplier=12.0,
        )
        if ratio_p50 is not None:
            ratio_p50 = round(ratio_p50, 4)
            if ratio_p50 > BURDEN_RATIO_SANITY_CAP:
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
        "county_fips":             county_fips,
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
        "n_pumas_contributing":    int(n_pumas),
    }


def compute_burden_county_segmented(
    person_df: pl.DataFrame,
    housing_df: pl.DataFrame,
    xwalk_df: pl.DataFrame,
    *,
    year: int,
    product: str = "acs1",
    state_fips: str = "34",
) -> pl.DataFrame:
    """Compute the full county-grain segmented burden table.

    Args:
        person_df:   raw.acs_pums_person rows.
        housing_df:  raw.acs_pums_housing rows (same vintage).
        xwalk_df:    Combined PUMA->county crosswalk for this state. Must
            carry a ``puma_vintage`` column ('2010' or '2020'); use
            ``load_xwalk_from_postgres`` which assembles this from the
            two physical crosswalk tables.
        year:        Survey year.
        product:     'acs1' or 'acs5'.
        state_fips:  2-char state FIPS.

    Returns:
        Long-format DataFrame matching derived.pums_burden_county_segmented
        schema. One row per (county_fips, year, tenure_class, segment_dim,
        segment_value).

    """
    if person_df.height == 0 or housing_df.height == 0 or xwalk_df.height == 0:
        return pl.DataFrame(schema=_RESULT_SCHEMA)

    if "puma_vintage" not in xwalk_df.columns:
        msg = (
            "xwalk_df is missing 'puma_vintage' column; refusing to "
            "allocate. Use load_xwalk_from_postgres() which assembles the "
            "combined 2010+2020 crosswalk with the vintage tag."
        )
        raise ValueError(msg)

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
    xwalk_df = xwalk_df.filter(pl.col("state_fips") == state_fips)

    if person_df.height == 0 or housing_df.height == 0 or xwalk_df.height == 0:
        return pl.DataFrame(schema=_RESULT_SCHEMA)

    hh_frame = _build_household_frame(person_df, housing_df)
    hh_frame = hh_frame.filter(pl.col("tenure_class").is_not_null())

    if "puma_vintage" not in hh_frame.columns:
        msg = (
            "household frame is missing 'puma_vintage'. The raw PUMS "
            "tables must be re-ingested with migration 035 applied so "
            "every row carries a decennial-vintage tag."
        )
        raise ValueError(msg)

    allocated = _allocate_to_counties(hh_frame, xwalk_df)

    # Sanity check: every (puma, vintage) pair in the data must be in
    # the combined crosswalk. If not, those persons silently disappear
    # from the output and we under-count.
    input_keys = set(
        hh_frame.select(["puma", "puma_vintage"])
                .unique()
                .iter_rows()
    )
    xwalk_keys = set(
        xwalk_df.select(["puma", "puma_vintage"])
                .unique()
                .iter_rows()
    )
    missing = input_keys - xwalk_keys
    if missing:
        # Surface a per-vintage breakdown so the operator knows which
        # crosswalk needs extending.
        by_vintage: dict[str, list[str]] = {}
        for puma, vintage in sorted(missing):
            by_vintage.setdefault(vintage, []).append(puma)
        msg = (
            "PUMAs present in PUMS but missing from the combined "
            "crosswalk (ref.puma2010_county_xwalk + "
            "ref.puma2020_county_xwalk):\n"
            + "\n".join(
                f"  vintage={v}: {pumas}" for v, pumas in by_vintage.items()
            )
            + "\nRefusing to materialize -- these persons would be "
              "silently dropped from the county aggregation."
        )
        raise ValueError(msg)

    # Diagnostic: how much of the input arrived through each vintage.
    # Useful when reading the orchestrator log to understand the 5-Year
    # / 1-Year split or to spot a regression in the ingester.
    vintage_breakdown = (
        hh_frame.group_by("puma_vintage")
                .agg(pl.len().alias("n_rows"))
                .sort("puma_vintage")
    )
    log.info(
        "compute_burden_county_segmented: input vintage breakdown for "
        "year=%d product=%s state=%s -> %s",
        year, product, state_fips,
        dict(zip(vintage_breakdown["puma_vintage"].to_list(),
                 vintage_breakdown["n_rows"].to_list(), strict=True)),
    )

    input_hash = _compute_input_vintage_hash(person_df, housing_df)

    rows: list[dict[str, Any]] = []
    counties = allocated["county_fips"].unique().sort().to_list()
    tenures = list(TEN_TO_TENURE_CLASS.values())

    for county in counties:
        county_frame = allocated.filter(pl.col("county_fips") == county)
        for tenure in tenures:
            tenure_frame = county_frame.filter(pl.col("tenure_class") == tenure)
            if tenure_frame.height == 0:
                continue

            for segment_dim, segment_col in _SEGMENT_DIMS:
                if segment_dim == "overall":
                    rows.append(_aggregate_one_county_cell(
                        tenure_frame,
                        year=year, product=product, state_fips=state_fips,
                        county_fips=county, tenure_class=tenure,
                        segment_dim="overall", segment_value="overall",
                    ))
                    continue

                values = (
                    tenure_frame[segment_col].drop_nulls().unique().sort().to_list()
                )
                for val in values:
                    cell_df = tenure_frame.filter(pl.col(segment_col) == val)
                    rows.append(_aggregate_one_county_cell(
                        cell_df,
                        year=year, product=product, state_fips=state_fips,
                        county_fips=county, tenure_class=tenure,
                        segment_dim=segment_dim, segment_value=val,
                    ))

    if not rows:
        return pl.DataFrame(schema=_RESULT_SCHEMA)

    out = pl.DataFrame(rows, schema=_RESULT_SCHEMA_NO_PROVENANCE)
    return out.with_columns(
        pl.lit(FORMULA_VERSION).alias("formula_version"),
        pl.lit(input_hash).alias("input_vintage_hash"),
    )


# ============================================================================
# Schema
# ============================================================================


_RESULT_SCHEMA_NO_PROVENANCE: Final[dict[str, Any]] = {
    "year":                    pl.Int16,
    "product":                 pl.Utf8,
    "state_fips":              pl.Utf8,
    "county_fips":             pl.Utf8,
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
    "n_pumas_contributing":    pl.Int16,
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
    """DELETE the (year, product) slice + bulk-INSERT the new rows.

    Same semantics as ``derived.pums_burden.load_to_postgres``. Wraps
    statements in the caller's transaction.
    """
    if df.height == 0:
        log.warning(
            "compute_burden_county_segmented produced 0 rows for "
            "year=%d product=%s", year, product,
        )
        return 0

    cols = list(df.columns)
    placeholders = ", ".join(["%s"] * len(cols))
    col_list = ", ".join(cols)
    insert_sql = (
        f"INSERT INTO derived.pums_burden_county_segmented ({col_list}) "
        f"VALUES ({placeholders})"
    )

    with connection.cursor() as cur:
        cur.execute(
            "DELETE FROM derived.pums_burden_county_segmented "
            "WHERE year = %s AND product = %s",
            (year, product),
        )
        rows_iter: Iterable[tuple[Any, ...]] = df.iter_rows()
        cur.executemany(insert_sql, list(rows_iter))
    return df.height


def load_xwalk_from_postgres(
    connection: psycopg.Connection, *, state_fips: str = "34",
) -> pl.DataFrame:
    """Read the combined 2010+2020 PUMA->county crosswalk.

    Returns a Polars DataFrame with one row per (state_fips, puma,
    puma_vintage, county_fips). The ``puma_vintage`` column tags rows
    from ``ref.puma2010_county_xwalk`` as ``'2010'`` and rows from
    ``ref.puma2020_county_xwalk`` as ``'2020'``. The compute layer
    keys its inner-join on ``(state_fips, puma, puma_vintage)`` so a
    PUMA10 record cannot be allocated against a PUMA20 crosswalk row
    (and vice versa) even when codes happen to collide across
    decennial vintages.

    Operator note: if either physical table is missing the assembled
    frame is partial, but the join is still safe -- the missing-PUMA
    sanity check inside ``compute_burden_county_segmented`` will
    fail fast and tell you which vintage is missing entries.
    """
    sql = (
        "  SELECT state_fips, puma, '2010'::text AS puma_vintage, "
        "         county_fips, allocation_factor::float8 "
        "    FROM ref.puma2010_county_xwalk WHERE state_fips = %s "
        "UNION ALL "
        "  SELECT state_fips, puma, '2020'::text AS puma_vintage, "
        "         county_fips, allocation_factor::float8 "
        "    FROM ref.puma2020_county_xwalk WHERE state_fips = %s"
    )
    with connection.cursor() as cur:
        cur.execute(sql, (state_fips, state_fips))
        rows = cur.fetchall()
    return pl.DataFrame(
        rows,
        schema={
            "state_fips":        pl.Utf8,
            "puma":              pl.Utf8,
            "puma_vintage":      pl.Utf8,
            "county_fips":       pl.Utf8,
            "allocation_factor": pl.Float64,
        },
        orient="row",
    )
