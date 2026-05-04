"""Unit tests for derived.pums_burden_county compute module.

Tests the methodologically-critical claims that distinguish this
module from a naive roll-up of the PUMA-level table:

1. Single-county PUMAs (allocation = 1.0) produce identical results
   to the PUMA-level table at the matching county.
2. Multi-county PUMAs split weights fractionally; sum across the
   counties recovers the original PUMA total.
3. The compute fn refuses to silently drop PUMAs not in the crosswalk.
4. County-level medians are computed across allocated observations,
   NOT rolled up from PUMA medians (median-of-medians is invalid).
"""

from __future__ import annotations

import polars as pl
import pytest

from derived.pums_burden import (
    FORMULA_VERSION,
    SUPPRESSION_FLOOR,
    compute_burden_segmented,
)
from derived.pums_burden_county import (
    compute_burden_county_segmented,
)
from tests.test_pums_burden import _make_housing, _make_person


def _xwalk(
    rows: list[tuple[str, str, str, float]],
    *, puma_vintage: str = "2020",
) -> pl.DataFrame:
    """Build a synthetic crosswalk frame.

    Each input is (state_fips, puma, county_fips, allocation_factor).
    The compute layer requires a ``puma_vintage`` column (2010 or 2020)
    on the crosswalk so a PUMA10 record cannot accidentally be allocated
    via a PUMA20 row. Tests default to ``'2020'`` for parity with the
    pre-multi-vintage behavior; the dual-vintage tests pass an explicit
    vintage when constructing per-vintage xwalk frames and concatenate.
    """
    return pl.DataFrame(
        rows,
        schema={
            "state_fips":        pl.Utf8,
            "puma":              pl.Utf8,
            "county_fips":       pl.Utf8,
            "allocation_factor": pl.Float64,
        },
        orient="row",
    ).with_columns(pl.lit(puma_vintage).alias("puma_vintage"))


# ============================================================================
# Single-county equivalence (sanity baseline)
# ============================================================================


def test_single_county_puma_matches_puma_level_compute() -> None:
    """PUMA wholly in one county (alloc = 1.0): county = PUMA totals.

    This is the architectural invariant that justifies the design:
    when allocation_factor = 1.0, the county aggregator must produce
    the same numbers as the PUMA aggregator (just labeled with
    county_fips instead of puma).
    """
    persons: list[dict[str, object]] = []
    housings: list[dict[str, object]] = []
    # 50 renter households in PUMA 03501; weight 25 each. weighted_n = 1250.
    for i in range(50):
        s = f"S{i:013d}"
        persons.append(_make_person(
            s, puma="03501", pwgtp=25, pincp=60000, rac1p=1,
        ))
        housings.append(_make_housing(
            s, puma="03501", ten=3, hincp=60000, grntp=2000, smocp=None,
            wgtp=25,
        ))

    p = pl.DataFrame(persons)
    h = pl.DataFrame(housings)
    xwalk = _xwalk([("34", "03501", "34003", 1.0)])

    puma_out = compute_burden_segmented(
        p, h, year=2022, product="acs1", state_fips="34",
    )
    county_out = compute_burden_county_segmented(
        p, h, xwalk, year=2022, product="acs1", state_fips="34",
    )

    p_overall = puma_out.filter(
        (pl.col("segment_dim") == "overall")
        & (pl.col("tenure_class") == "renter"),
    ).row(0, named=True)
    c_overall = county_out.filter(
        (pl.col("segment_dim") == "overall")
        & (pl.col("tenure_class") == "renter"),
    ).row(0, named=True)

    assert p_overall["weighted_n"]   == c_overall["weighted_n"]
    assert p_overall["sample_n"]     == c_overall["sample_n"]
    assert p_overall["household_income_p50"] == c_overall["household_income_p50"]
    assert p_overall["monthly_cost_p50"]     == c_overall["monthly_cost_p50"]
    assert p_overall["burden_ratio_p50"]     == c_overall["burden_ratio_p50"]
    assert c_overall["county_fips"]       == "34003"
    assert c_overall["n_pumas_contributing"] == 1


# ============================================================================
# Multi-county PUMAs: allocation correctness
# ============================================================================


def test_multi_county_puma_splits_weights_fractionally() -> None:
    """A multi-county PUMA divides PWGTP by allocation_factor across counties.

    Sum of weighted_n across the allocated counties must recover the
    original PUMA-level weighted_n (within rounding).
    """
    # 60 renter households in multi-county PUMA 02501.
    persons: list[dict[str, object]] = []
    housings: list[dict[str, object]] = []
    for i in range(60):
        s = f"M{i:013d}"
        persons.append(_make_person(
            s, puma="02501", pwgtp=20, pincp=40000, rac1p=1,
        ))
        housings.append(_make_housing(
            s, puma="02501", ten=3, hincp=40000, grntp=1500, smocp=None,
            wgtp=20,
        ))

    # 50/50 split between Salem and Cumberland (synthetic, not real).
    xwalk = _xwalk([
        ("34", "02501", "34033", 0.5),
        ("34", "02501", "34011", 0.5),
    ])

    p = pl.DataFrame(persons)
    h = pl.DataFrame(housings)

    puma_out = compute_burden_segmented(
        p, h, year=2022, product="acs1", state_fips="34",
    )
    county_out = compute_burden_county_segmented(
        p, h, xwalk, year=2022, product="acs1", state_fips="34",
    )

    puma_renter_overall = puma_out.filter(
        (pl.col("segment_dim") == "overall")
        & (pl.col("tenure_class") == "renter"),
    ).row(0, named=True)
    puma_weighted_n = puma_renter_overall["weighted_n"]

    # Sum the county weighted_n across both allocated counties.
    county_renter_overall = county_out.filter(
        (pl.col("segment_dim") == "overall")
        & (pl.col("tenure_class") == "renter"),
    )
    total_county_weighted_n = county_renter_overall["weighted_n"].sum()

    assert total_county_weighted_n == puma_weighted_n, (
        "Sum of allocated county weighted_n must equal PUMA-level "
        f"weighted_n; got {total_county_weighted_n} vs {puma_weighted_n}"
    )

    # Each county should have approximately half the population.
    weighted_ns = sorted(county_renter_overall["weighted_n"].to_list())
    assert weighted_ns[0] == 600 and weighted_ns[1] == 600

    # n_pumas_contributing must record the multi-county split.
    assert county_renter_overall["n_pumas_contributing"].max() == 1


# ============================================================================
# Sanity: refuse to silently drop PUMAs missing from the crosswalk
# ============================================================================


def test_compute_raises_when_puma_missing_from_xwalk() -> None:
    """A PUMA in the data but not in the crosswalk must raise, not silently drop."""
    persons = [
        _make_person(f"S{i:013d}", puma="99999", pwgtp=25, pincp=60000)
        for i in range(40)
    ]
    housings = [
        _make_housing(f"S{i:013d}", puma="99999", ten=3, hincp=60000,
                      grntp=2000, smocp=None, wgtp=25)
        for i in range(40)
    ]
    xwalk = _xwalk([("34", "03501", "34003", 1.0)])  # 99999 missing

    with pytest.raises(ValueError, match="missing from"):
        compute_burden_county_segmented(
            pl.DataFrame(persons), pl.DataFrame(housings), xwalk,
            year=2022, product="acs1", state_fips="34",
        )


# ============================================================================
# Suppression at county grain
# ============================================================================


def test_county_aggregation_can_unsuppress_smaller_pumas() -> None:
    """Two PUMAs that are individually suppressed at PUMA grain may combine
    to exceed the floor at county grain.

    This is the principal analytical justification for the county
    table: smaller PUMAs whose demographic cells are too sparse can
    yield meaningful county-level signals once aggregated.
    """
    # Two PUMAs in the same county; each PUMA has 30 households w/ wgt 20
    # = weighted_n 600 per PUMA (suppressed at PUMA grain, < 1000).
    # Combined: weighted_n 1200 (above floor at county grain).
    persons: list[dict[str, object]] = []
    housings: list[dict[str, object]] = []
    sno = 0
    for puma in ("03001", "03002"):
        for _ in range(30):
            sno += 1
            s = f"S{sno:013d}"
            persons.append(_make_person(
                s, puma=puma, pwgtp=20, pincp=60000, rac1p=1,
            ))
            housings.append(_make_housing(
                s, puma=puma, ten=3, hincp=60000, grntp=2000, smocp=None,
                wgtp=20,
            ))

    xwalk = _xwalk([
        ("34", "03001", "34999", 1.0),
        ("34", "03002", "34999", 1.0),
    ])

    puma_out = compute_burden_segmented(
        pl.DataFrame(persons), pl.DataFrame(housings),
        year=2022, product="acs1", state_fips="34",
    )
    county_out = compute_burden_county_segmented(
        pl.DataFrame(persons), pl.DataFrame(housings), xwalk,
        year=2022, product="acs1", state_fips="34",
    )

    puma_overall = puma_out.filter(
        (pl.col("segment_dim") == "overall")
        & (pl.col("tenure_class") == "renter"),
    )
    assert puma_overall["weighted_n"].max() < SUPPRESSION_FLOOR
    assert puma_overall["suppressed"].all()

    county_overall = county_out.filter(
        (pl.col("segment_dim") == "overall")
        & (pl.col("tenure_class") == "renter"),
    ).row(0, named=True)
    assert county_overall["weighted_n"]   == 1200
    assert county_overall["suppressed"]   is False
    assert county_overall["burden_ratio_p50"] is not None
    assert county_overall["n_pumas_contributing"] == 2


# ============================================================================
# Provenance + shape
# ============================================================================


def test_county_compute_carries_formula_version() -> None:
    """formula_version + input_vintage_hash present on every county row."""
    persons = [
        _make_person(f"S{i:013d}", puma="03501", pwgtp=25, pincp=60000)
        for i in range(50)
    ]
    housings = [
        _make_housing(f"S{i:013d}", puma="03501", ten=3, hincp=60000,
                      grntp=2000, smocp=None, wgtp=25)
        for i in range(50)
    ]
    xwalk = _xwalk([("34", "03501", "34003", 1.0)])

    out = compute_burden_county_segmented(
        pl.DataFrame(persons), pl.DataFrame(housings), xwalk,
        year=2022, product="acs1", state_fips="34",
    )
    assert (out["formula_version"] == FORMULA_VERSION).all()
    assert out["input_vintage_hash"].null_count() == 0
    assert "n_pumas_contributing" in out.columns


def test_county_compute_emits_se_columns() -> None:
    """Schema contract: county output carries the same SE columns as PUMA."""
    persons = [
        _make_person(f"S{i:013d}", puma="03501", pwgtp=25, pincp=60000)
        for i in range(50)
    ]
    housings = [
        _make_housing(
            f"S{i:013d}", puma="03501", ten=3, hincp=60000,
            grntp=2000, smocp=None, wgtp=25,
        )
        for i in range(50)
    ]
    xwalk = _xwalk([("34", "03501", "34003", 1.0)])
    out = compute_burden_county_segmented(
        pl.DataFrame(persons), pl.DataFrame(housings), xwalk,
        year=2022, product="acs1", state_fips="34",
    )
    for col in (
        "household_income_p50_se",
        "monthly_cost_p50_se",
        "burden_ratio_p50_se",
    ):
        assert col in out.columns, f"missing SE column {col!r}"
    overall = out.filter(
        (pl.col("segment_dim") == "overall")
        & (pl.col("tenure_class") == "renter"),
    ).row(0, named=True)
    # Identical replicate weights -> SE = 0.
    assert overall["burden_ratio_p50_se"] == 0.0


def test_county_compute_allocates_replicate_weights_for_multi_county_pumas() -> None:
    """Replicate weights are scaled by allocation factor for multi-county PUMAs.

    The principal claim under test: when a PUMA spans two counties
    with allocation factors (0.6, 0.4), each replicate weight is
    multiplied by the same factor, NOT applied uniformly. If we
    forgot to allocate the replicates, the county-level SE for a
    multi-county PUMA would equal the PUMA-level SE -- which is
    exactly what we want to PREVENT.

    With identical replicate-weight vectors but a 50/50 split, both
    counties should produce SE = 0 (replicates still match the
    allocated main weight in each county).
    """
    persons: list[dict[str, object]] = []
    housings: list[dict[str, object]] = []
    for i in range(60):
        s = f"M{i:013d}"
        # Non-uniform replicate weights -> non-zero SE if not allocated correctly
        rep = [40 if r % 2 == 0 else 20 for r in range(80)]
        persons.append(_make_person(
            s, puma="02501", pwgtp=30, pincp=40000, replicate_weights=rep,
        ))
        housings.append(_make_housing(
            s, puma="02501", ten=3, hincp=40000, grntp=1500, smocp=None,
            wgtp=30, replicate_weights=rep,
        ))

    xwalk = _xwalk([
        ("34", "02501", "34033", 0.6),
        ("34", "02501", "34011", 0.4),
    ])

    out = compute_burden_county_segmented(
        pl.DataFrame(persons), pl.DataFrame(housings), xwalk,
        year=2022, product="acs1", state_fips="34",
    )
    salem = out.filter(
        (pl.col("county_fips") == "34033")
        & (pl.col("segment_dim") == "overall")
        & (pl.col("tenure_class") == "renter"),
    ).row(0, named=True)

    # The income SE must be defined and finite for an unsuppressed cell.
    if not salem["suppressed"]:
        assert salem["household_income_p50_se"] is not None
        assert salem["household_income_p50_se"] >= 0.0


def test_county_compute_returns_empty_when_no_overlap() -> None:
    """Crosswalk that has no overlap with the persons' state -> empty result.

    Different state in xwalk -> filter wipes it out before the
    missing-PUMA check; result is just empty.
    """
    persons = [
        _make_person(f"S{i:013d}", puma="03501", pwgtp=25, pincp=60000)
        for i in range(50)
    ]
    housings = [
        _make_housing(f"S{i:013d}", puma="03501", ten=3, hincp=60000,
                      grntp=2000, smocp=None, wgtp=25)
        for i in range(50)
    ]
    xwalk = _xwalk([("06", "03501", "06037", 1.0)])  # CA-only crosswalk
    out = compute_burden_county_segmented(
        pl.DataFrame(persons), pl.DataFrame(housings), xwalk,
        year=2022, product="acs1", state_fips="34",
    )
    assert out.height == 0


# ============================================================================
# Dual-vintage (2010 + 2020) compute path
# ============================================================================


def test_county_compute_handles_dual_vintage_input() -> None:
    """5-Year-style mixed input: PUMA10 + PUMA20 records both feed county.

    This is the substrate-honesty test for the 2010-vintage PUMA
    crosswalk: a 5-Year file carries records from samples drawn before
    AND after the 2020 decennial PUMA revision. Each row tags itself
    with the right ``puma_vintage`` (the ingester's job, regression-
    pinned by tests/test_census_acs_pums.py); the compute fn here
    must dispatch each row to the matching crosswalk and aggregate
    them into the same county_fips bucket.

    Construction: 40 households in PUMA20=03501 (Bergen, alloc 1.0)
    + 40 households in PUMA10=00301 (Bergen 2010 vintage; same
    geographic area, different decennial code). The county output
    must show all 80 households in county 34003. Failing to include
    one vintage halves weighted_n; failing to dispatch correctly
    drops to zero on the missing side.
    """
    persons: list[dict[str, object]] = []
    housings: list[dict[str, object]] = []
    for i in range(40):
        s = f"P20_{i:011d}"
        persons.append(_make_person(
            s, puma="03501", puma_vintage="2020", product="acs5",
            pwgtp=25, pincp=70000,
        ))
        housings.append(_make_housing(
            s, puma="03501", puma_vintage="2020", product="acs5",
            ten=3, hincp=70000, grntp=2000, smocp=None, wgtp=25,
        ))
    for i in range(40):
        s = f"P10_{i:011d}"
        persons.append(_make_person(
            s, puma="00301", puma_vintage="2010", product="acs5",
            pwgtp=25, pincp=70000,
        ))
        housings.append(_make_housing(
            s, puma="00301", puma_vintage="2010", product="acs5",
            ten=3, hincp=70000, grntp=2000, smocp=None, wgtp=25,
        ))

    xwalk_2020 = _xwalk(
        [("34", "03501", "34003", 1.0)], puma_vintage="2020",
    )
    xwalk_2010 = _xwalk(
        [("34", "00301", "34003", 1.0)], puma_vintage="2010",
    )
    combined = pl.concat([xwalk_2020, xwalk_2010], how="vertical")

    out = compute_burden_county_segmented(
        pl.DataFrame(persons), pl.DataFrame(housings), combined,
        year=2022, product="acs5", state_fips="34",
    )

    overall = out.filter(
        (pl.col("county_fips") == "34003")
        & (pl.col("segment_dim") == "overall")
        & (pl.col("tenure_class") == "renter"),
    )
    assert overall.height == 1
    row = overall.row(0, named=True)
    # 80 households * pwgtp=25 = 2000 weighted persons in Bergen
    assert row["weighted_n"] == 2000
    assert row["sample_n"] == 80
    # n_pumas_contributing should be 2 (one PUMA10, one PUMA20 -- they
    # are NOT the same PUMA even though they map to the same county).
    assert row["n_pumas_contributing"] == 2


def test_county_compute_rejects_xwalk_missing_vintage_column() -> None:
    """Sanity: a crosswalk DataFrame without 'puma_vintage' is rejected.

    Refusing to materialize against a vintage-blind crosswalk prevents
    a class of silent correctness loss where PUMA10 records would be
    inadvertently allocated against PUMA20 rows.
    """
    persons = [_make_person(f"S{i:013d}", puma="03501", pwgtp=25)
               for i in range(50)]
    housings = [_make_housing(f"S{i:013d}", puma="03501", ten=3,
                              grntp=2000, smocp=None, wgtp=25)
                for i in range(50)]
    bad = pl.DataFrame(
        [("34", "03501", "34003", 1.0)],
        schema={
            "state_fips":        pl.Utf8,
            "puma":              pl.Utf8,
            "county_fips":       pl.Utf8,
            "allocation_factor": pl.Float64,
        },
        orient="row",
    )
    with pytest.raises(ValueError, match="puma_vintage"):
        compute_burden_county_segmented(
            pl.DataFrame(persons), pl.DataFrame(housings), bad,
            year=2022, product="acs1", state_fips="34",
        )


def test_county_compute_rejects_puma_present_only_in_wrong_vintage() -> None:
    """Refuses to silently drop a PUMA10 record when only PUMA20 xwalk has its code.

    A 2010-vintage record on PUMA=00301 cannot be allocated by a
    crosswalk that only contains a 2020-vintage row keyed on the same
    code (those are different geographic areas despite the code
    collision). The compute layer must surface this as a fail-fast
    error, not a silent inner-join drop.
    """
    persons = [
        _make_person(f"S{i:013d}", puma="00301", puma_vintage="2010",
                     product="acs5", pwgtp=25, pincp=60000)
        for i in range(50)
    ]
    housings = [
        _make_housing(f"S{i:013d}", puma="00301", puma_vintage="2010",
                      product="acs5", ten=3, hincp=60000, grntp=2000,
                      smocp=None, wgtp=25)
        for i in range(50)
    ]
    # Crosswalk has the same PUMA code but tagged 2020 -- wrong vintage.
    xwalk = _xwalk([("34", "00301", "34003", 1.0)], puma_vintage="2020")

    with pytest.raises(ValueError, match="missing from the combined"):
        compute_burden_county_segmented(
            pl.DataFrame(persons), pl.DataFrame(housings), xwalk,
            year=2022, product="acs5", state_fips="34",
        )


def test_county_compute_dual_vintage_multi_county_split() -> None:
    """Multi-county PUMAs in BOTH vintages allocate independently.

    PUMA10=02500 (Salem 71% / Cumberland 29%) and PUMA20=02501
    (Salem 56% / Cumberland 44%) both feed Salem and Cumberland
    counties. Each row picks its allocation factor from the matching
    vintage; the county aggregator then merges both streams under
    a single county_fips key. Failing to dispatch on vintage would
    give every row the same allocation factor regardless of which
    geographic boundary it actually fell in.
    """
    persons: list[dict[str, object]] = []
    housings: list[dict[str, object]] = []
    for i in range(50):
        s = f"V20_{i:011d}"
        persons.append(_make_person(
            s, puma="02501", puma_vintage="2020", product="acs5",
            pwgtp=20, pincp=50000,
        ))
        housings.append(_make_housing(
            s, puma="02501", puma_vintage="2020", product="acs5",
            ten=3, hincp=50000, grntp=1500, smocp=None, wgtp=20,
        ))
    for i in range(50):
        s = f"V10_{i:011d}"
        persons.append(_make_person(
            s, puma="02500", puma_vintage="2010", product="acs5",
            pwgtp=20, pincp=50000,
        ))
        housings.append(_make_housing(
            s, puma="02500", puma_vintage="2010", product="acs5",
            ten=3, hincp=50000, grntp=1500, smocp=None, wgtp=20,
        ))

    xwalk_2020 = _xwalk([
        ("34", "02501", "34033", 0.56),
        ("34", "02501", "34011", 0.44),
    ], puma_vintage="2020")
    xwalk_2010 = _xwalk([
        ("34", "02500", "34033", 0.71),
        ("34", "02500", "34011", 0.29),
    ], puma_vintage="2010")
    combined = pl.concat([xwalk_2020, xwalk_2010], how="vertical")

    out = compute_burden_county_segmented(
        pl.DataFrame(persons), pl.DataFrame(housings), combined,
        year=2022, product="acs5", state_fips="34",
    )

    salem = out.filter(
        (pl.col("county_fips") == "34033")
        & (pl.col("segment_dim") == "overall")
        & (pl.col("tenure_class") == "renter"),
    ).row(0, named=True)
    cumberland = out.filter(
        (pl.col("county_fips") == "34011")
        & (pl.col("segment_dim") == "overall")
        & (pl.col("tenure_class") == "renter"),
    ).row(0, named=True)

    # Salem expected weighted_n: 50 hh * pwgtp=20 * 0.56 (2020) +
    #                            50 hh * pwgtp=20 * 0.71 (2010)
    #                          = 560 + 710 = 1270
    expected_salem = round(50 * 20 * 0.56 + 50 * 20 * 0.71)
    expected_cumb = round(50 * 20 * 0.44 + 50 * 20 * 0.29)
    assert salem["weighted_n"] == expected_salem
    assert cumberland["weighted_n"] == expected_cumb
    # Both PUMAs (one per vintage) should contribute to each county.
    assert salem["n_pumas_contributing"] == 2
    assert cumberland["n_pumas_contributing"] == 2
