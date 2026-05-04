"""Unit tests for derived.pums_burden compute module.

These tests cover the methodology -- demographic recodes, weighted
percentile per cell, suppression, and the end-to-end shape of the
output DataFrame.

We construct deterministic synthetic PUMS frames so the expected
outputs are computable by hand and the tests catch regressions in
the recode lookup tables, the suppression floor, or the weighted-
percentile semantics.
"""

from __future__ import annotations

import polars as pl
import pytest

from derived.pums_burden import (
    BURDEN_RATIO_SANITY_CAP,
    CIT_TO_CITIZENSHIP_CLASS,
    FORMULA_VERSION,
    RAC1P_TO_RACE_CLASS,
    SUPPRESSION_FLOOR,
    TEN_TO_TENURE_CLASS,
    _recode_age_band,
    _recode_hispanic,
    compute_burden_segmented,
)

# ============================================================================
# Helpers: build synthetic PUMS frames
# ============================================================================


def _make_person(
    serialno: str, *, puma: str = "03501", puma_vintage: str = "2020",
    rac1p: int = 1, hisp: int = 1, cit: int = 1, agep: int = 35,
    pwgtp: int = 50, year: int = 2022, product: str = "acs1",
    pincp: int = 80000,
    replicate_weights: list[int] | None = None,
) -> dict[str, object]:
    """Build one synthetic PUMS person row, full schema.

    ``replicate_weights`` defaults to ``[pwgtp] * 80`` (zero-variance
    replicates), which produces SE = 0 in SDR -- the right baseline
    for tests that assert "compute does not crash"; tests that exercise
    SE > 0 should pass an explicit non-uniform 80-vector.

    ``puma_vintage`` defaults to ``'2020'`` for parity with the post-
    decennial PUMA boundaries; pass ``'2010'`` to construct
    PUMA10-tagged synthetic data for the dual-vintage compute tests.
    """
    return {
        "year": year, "product": product, "serialno": serialno, "sporder": 1,
        "state_fips": "34", "puma": puma, "puma_vintage": puma_vintage,
        "agep": agep, "sex": 1, "rac1p": rac1p, "hisp": hisp, "cit": cit,
        "pobp": 1, "nativity": 1 if cit <= 3 else 2,
        "schl": 21, "esr": 1, "cow": 1,
        "wagp": pincp, "pernp": pincp, "pincp": pincp,
        "pwgtp": pwgtp,
        "replicate_weights": replicate_weights or [pwgtp] * 80,
    }


def _make_housing(
    serialno: str, *, puma: str = "03501", puma_vintage: str = "2020",
    ten: int = 1, hincp: int = 80000,
    grntp: int | None = None, smocp: int | None = 2500,
    wgtp: int = 50, year: int = 2022, product: str = "acs1",
    replicate_weights: list[int] | None = None,
) -> dict[str, object]:
    """Build one synthetic PUMS housing row, full schema.

    Replicate weights default to ``[wgtp] * 80`` for zero-variance
    SDR baseline. ``puma_vintage`` defaults to ``'2020'``.
    """
    return {
        "year": year, "product": product, "serialno": serialno,
        "state_fips": "34", "puma": puma, "puma_vintage": puma_vintage,
        "ten": ten, "bdsp": 3, "rmsp": 6, "bld": 2, "yrblt": 1980, "veh": 2,
        "valp": 500000, "grntp": grntp, "rntp": grntp,
        "smocp": smocp, "smp": smocp,
        "hincp": hincp, "fincp": hincp, "wgtp": wgtp,
        "replicate_weights": replicate_weights or [wgtp] * 80,
    }


# ============================================================================
# Recode tables
# ============================================================================


def test_rac1p_recode_collapses_aian_codes() -> None:
    """RAC1P codes 3, 4, 5 all map to 'aian'."""
    assert RAC1P_TO_RACE_CLASS[3] == "aian"
    assert RAC1P_TO_RACE_CLASS[4] == "aian"
    assert RAC1P_TO_RACE_CLASS[5] == "aian"


def test_cit_recode_collapses_us_born_codes() -> None:
    """CIT codes 1, 2, 3 (US-born variants) all collapse to 'us_born'."""
    assert CIT_TO_CITIZENSHIP_CLASS[1] == "us_born"
    assert CIT_TO_CITIZENSHIP_CLASS[2] == "us_born"
    assert CIT_TO_CITIZENSHIP_CLASS[3] == "us_born"
    assert CIT_TO_CITIZENSHIP_CLASS[4] == "naturalized"
    assert CIT_TO_CITIZENSHIP_CLASS[5] == "non_citizen"


def test_ten_recode_drops_no_rent() -> None:
    """TEN=4 (occupied without rent) is intentionally dropped."""
    assert 4 not in TEN_TO_TENURE_CLASS
    assert TEN_TO_TENURE_CLASS[1] == "owner_w_mtg"
    assert TEN_TO_TENURE_CLASS[2] == "owner_no_mtg"
    assert TEN_TO_TENURE_CLASS[3] == "renter"


@pytest.mark.parametrize(("agep", "expected"), [
    (24,  "<25"),
    (25,  "25-34"),
    (34,  "25-34"),
    (35,  "35-44"),
    (64,  "55-64"),
    (65,  "65+"),
    (99,  "65+"),
    (None, None),
])
def test_age_band_recode(agep: int | None, expected: str | None) -> None:
    """Age bands at boundaries: 25-34 inclusive on lower, exclusive on upper."""
    assert _recode_age_band(agep) == expected


def test_hispanic_recode() -> None:
    """HISP=1 is 'not_hispanic'; everything else is 'hispanic'."""
    assert _recode_hispanic(1)    == "not_hispanic"
    assert _recode_hispanic(2)    == "hispanic"
    assert _recode_hispanic(20)   == "hispanic"
    assert _recode_hispanic(None) is None


# ============================================================================
# Compute: small-cell suppression
# ============================================================================


def test_compute_suppresses_below_threshold() -> None:
    """A cell with weighted_n < SUPPRESSION_FLOOR has NULL ratios."""
    # 5 persons, weight 100 each = weighted_n 500, well below floor 1000
    persons = [
        _make_person(f"S{i:013d}", pwgtp=100, pincp=50000)
        for i in range(5)
    ]
    housings = [_make_housing(f"S{i:013d}", ten=3, hincp=50000,
                              grntp=1500, smocp=None, wgtp=100)
                for i in range(5)]

    result = compute_burden_segmented(
        pl.DataFrame(persons), pl.DataFrame(housings),
        year=2022, product="acs1", state_fips="34",
    )

    overall = result.filter(
        (pl.col("segment_dim") == "overall") & (pl.col("tenure_class") == "renter")
    )
    assert overall.height == 1
    row = overall.row(0, named=True)
    assert row["weighted_n"] == 500
    assert row["weighted_n"] < SUPPRESSION_FLOOR
    assert row["suppressed"] is True
    assert row["burden_ratio_p50"] is None
    assert row["household_income_p50"] is None


def test_compute_does_not_suppress_above_threshold() -> None:
    """A cell with weighted_n >= SUPPRESSION_FLOOR has populated ratios."""
    # 50 persons, weight 25 each = weighted_n 1250, above floor 1000
    persons = [
        _make_person(f"S{i:013d}", pwgtp=25, pincp=60000)
        for i in range(50)
    ]
    housings = [_make_housing(f"S{i:013d}", ten=3, hincp=60000,
                              grntp=2000, smocp=None, wgtp=25)
                for i in range(50)]

    result = compute_burden_segmented(
        pl.DataFrame(persons), pl.DataFrame(housings),
        year=2022, product="acs1", state_fips="34",
    )

    overall = result.filter(
        (pl.col("segment_dim") == "overall") & (pl.col("tenure_class") == "renter")
    )
    assert overall.height == 1
    row = overall.row(0, named=True)
    assert row["weighted_n"] == 1250
    assert row["suppressed"] is False
    # 2000 * 12 / 60000 = 0.4
    assert row["burden_ratio_p50"] == pytest.approx(0.4, abs=0.01)


# ============================================================================
# Compute: end-to-end shape
# ============================================================================


def _synthetic_balanced_frame(
    n_per_cell: int = 50, weight: int = 30,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Build a balanced synthetic frame with 4 race classes x renter/owner.

    Each (race, tenure) cell has ``n_per_cell`` persons, weight ``weight``,
    so cells are over-suppression-floor when n_per_cell * weight >= 1000.
    With defaults 50 * 30 = 1500 > 1000.
    """
    persons: list[dict[str, object]] = []
    housings: list[dict[str, object]] = []
    sno = 0
    for rac1p in (1, 2, 6, 8):  # white, black, asian, other
        for ten in (1, 3):       # owner_w_mtg, renter
            for _ in range(n_per_cell):
                sno += 1
                serialno = f"S{sno:013d}"
                income = 80000 if ten == 1 else 50000
                cost = 2500 if ten == 1 else 1800
                persons.append(_make_person(
                    serialno, rac1p=rac1p, pwgtp=weight, pincp=income,
                ))
                housings.append(_make_housing(
                    serialno, ten=ten, hincp=income,
                    grntp=cost if ten == 3 else None,
                    smocp=cost if ten == 1 else None,
                    wgtp=weight,
                ))
    return pl.DataFrame(persons), pl.DataFrame(housings)


def test_compute_emits_expected_segment_dimensions() -> None:
    """Every (puma, year, tenure) cell must yield rows for all 5 segment dims."""
    p, h = _synthetic_balanced_frame()
    result = compute_burden_segmented(
        p, h, year=2022, product="acs1", state_fips="34",
    )
    dims = set(result["segment_dim"].unique().to_list())
    assert dims == {"overall", "race", "hispanic", "citizenship", "age_band"}


def test_compute_carries_formula_version_and_input_hash() -> None:
    """Provenance columns must be populated on every row."""
    p, h = _synthetic_balanced_frame()
    result = compute_burden_segmented(
        p, h, year=2022, product="acs1", state_fips="34",
    )
    assert (result["formula_version"] == FORMULA_VERSION).all()
    assert result["input_vintage_hash"].null_count() == 0


def test_compute_drops_no_rent_tenure() -> None:
    """TEN=4 (occupied without rent) does not appear in any cell."""
    persons = [_make_person(f"S{i:013d}", pwgtp=50) for i in range(10)]
    housings = [_make_housing(f"S{i:013d}", ten=4, hincp=50000, smocp=None,
                              grntp=None, wgtp=50)
                for i in range(10)]
    result = compute_burden_segmented(
        pl.DataFrame(persons), pl.DataFrame(housings),
        year=2022, product="acs1", state_fips="34",
    )
    assert result.height == 0, (
        "TEN=4 should be dropped entirely; result must be empty when all "
        "synthetic rows are no-rent occupants."
    )


def test_compute_filters_to_requested_state() -> None:
    """A person in a different state is filtered out before aggregation."""
    persons = [
        _make_person("S0000000000001", pwgtp=50),  # NJ
        # Person in state 06 (CA) -- should be filtered.
        {**_make_person("S0000000000002", pwgtp=50), "state_fips": "06"},
    ]
    housings = [
        _make_housing("S0000000000001"),
        {**_make_housing("S0000000000002"), "state_fips": "06"},
    ]
    result = compute_burden_segmented(
        pl.DataFrame(persons), pl.DataFrame(housings),
        year=2022, product="acs1", state_fips="34",
    )
    # Either suppressed (weighted_n=50 < 1000) or empty -- but importantly,
    # no row should leak the CA serialno's geography
    assert (result["state_fips"] == "34").all() if result.height > 0 else True


# ============================================================================
# Compute: methodology -- median of medians, NOT median of ratios
# ============================================================================


def test_burden_ratio_uses_ratio_of_medians_not_median_of_ratios() -> None:
    """Methodology check: cell ratio = median_cost*12 / median_income.

    Construct a pathological case where median-of-ratios and ratio-of-
    medians diverge, then assert which one we computed.
    """
    # 50 households over the suppression floor, evenly split between:
    #   Group A: income 100K, rent 1000  -> ratio = 0.12
    #   Group B: income 20K,  rent 2000  -> ratio = 1.20
    # Median of ratios = ~0.66 (interpolated between 0.12 and 1.20).
    # Median income = 60K (midpoint), median rent = 1500 (midpoint).
    # Ratio of medians = 1500*12 / 60000 = 0.30. Different number.
    persons: list[dict[str, object]] = []
    housings: list[dict[str, object]] = []
    for i in range(25):
        s = f"A{i:013d}"
        persons.append(_make_person(s, pwgtp=40, pincp=100000))
        housings.append(_make_housing(
            s, ten=3, hincp=100000, grntp=1000, smocp=None, wgtp=40,
        ))
    for i in range(25):
        s = f"B{i:013d}"
        persons.append(_make_person(s, pwgtp=40, pincp=20000))
        housings.append(_make_housing(
            s, ten=3, hincp=20000, grntp=2000, smocp=None, wgtp=40,
        ))
    result = compute_burden_segmented(
        pl.DataFrame(persons), pl.DataFrame(housings),
        year=2022, product="acs1", state_fips="34",
    )
    overall = result.filter(
        (pl.col("segment_dim") == "overall") & (pl.col("tenure_class") == "renter")
    ).row(0, named=True)
    # Weighted percentile is type-1 (smallest value with cumulative weight
    # >= q*total). With 25-25 split, median income lands on the upper of
    # the two groups (lower or upper, but consistently).
    # Either income_p50 = 100000 (cost_p50 = 1000, ratio = 0.12)
    # or     income_p50 = 20000  (cost_p50 = 2000, ratio = 1.20).
    # In either case the ratio is NOT 0.66 (the median of per-row ratios).
    ratio = overall["burden_ratio_p50"]
    assert ratio is not None
    assert ratio not in (None,) and abs(float(ratio) - 0.66) > 0.05, (
        f"burden_ratio_p50 = {ratio!r}; methodology must be ratio-of-medians, "
        "not median-of-ratios."
    )


# ============================================================================
# Sanity cap
# ============================================================================


def test_burden_ratio_sanity_cap_documented() -> None:
    """The sanity cap is an explicit constant, not a magic number."""
    assert BURDEN_RATIO_SANITY_CAP == 5.0


# ============================================================================
# Standard errors via SDR
# ============================================================================


def test_se_columns_present_on_every_unsuppressed_row() -> None:
    """After SDR plumbing, every non-suppressed row must have *_se columns.

    The columns may be NULL (low-replicate failure) but they must exist
    in the schema, so consumers can assume presence.
    """
    p, h = _synthetic_balanced_frame()
    result = compute_burden_segmented(
        p, h, year=2022, product="acs1", state_fips="34",
    )
    for col in (
        "household_income_p50_se",
        "monthly_cost_p50_se",
        "burden_ratio_p50_se",
    ):
        assert col in result.columns, f"missing SE column {col!r}"


def test_se_is_zero_when_replicate_weights_match_main_weights() -> None:
    """Synthetic-data baseline: replicates equal main weight -> SE = 0.

    This is the contract our test fixtures rely on: when ``_make_*``
    helpers leave replicate_weights at its default ``[w] * 80``, every
    SDR replicate p50 equals the main p50 and the variance is exactly
    zero. A non-zero SE in this regime would indicate the compute is
    introducing spurious noise.
    """
    persons = [
        _make_person(f"S{i:013d}", pwgtp=25, pincp=60000)
        for i in range(50)
    ]
    housings = [
        _make_housing(
            f"S{i:013d}", ten=3, hincp=60000, grntp=2000, smocp=None,
            wgtp=25,
        )
        for i in range(50)
    ]
    result = compute_burden_segmented(
        pl.DataFrame(persons), pl.DataFrame(housings),
        year=2022, product="acs1", state_fips="34",
    )
    overall = result.filter(
        (pl.col("segment_dim") == "overall")
        & (pl.col("tenure_class") == "renter"),
    ).row(0, named=True)
    assert overall["suppressed"]              is False
    assert overall["household_income_p50_se"] == 0.0
    assert overall["monthly_cost_p50_se"]     == 0.0
    assert overall["burden_ratio_p50_se"]     == 0.0


def test_se_is_positive_when_replicates_disagree() -> None:
    """When replicate weights produce different medians, SE > 0.

    Construct two groups (income $50K, $100K) with weights such that
    the MAIN weight gives a median in one group, but ~half the replicate
    weights would flip the median to the other group. The resulting
    SDR variance is non-zero.
    """
    persons: list[dict[str, object]] = []
    housings: list[dict[str, object]] = []

    # 30 households with HINCP $50K, main wgtp 25
    # Replicate weights: alternate between 50 (heavy) and 0 (light)
    # so half the replicates will pull the median toward the other group.
    for i in range(30):
        s = f"A{i:013d}"
        rep = [50 if r % 2 == 0 else 1 for r in range(80)]
        persons.append(
            _make_person(s, pwgtp=25, pincp=50000, replicate_weights=rep),
        )
        housings.append(
            _make_housing(
                s, ten=3, hincp=50000, grntp=2000, smocp=None,
                wgtp=25, replicate_weights=rep,
            ),
        )

    # 30 households with HINCP $100K, main wgtp 25
    # Replicate weights: opposite alternation (light when other is heavy)
    for i in range(30):
        s = f"B{i:013d}"
        rep = [1 if r % 2 == 0 else 50 for r in range(80)]
        persons.append(
            _make_person(s, pwgtp=25, pincp=100000, replicate_weights=rep),
        )
        housings.append(
            _make_housing(
                s, ten=3, hincp=100000, grntp=2000, smocp=None,
                wgtp=25, replicate_weights=rep,
            ),
        )

    result = compute_burden_segmented(
        pl.DataFrame(persons), pl.DataFrame(housings),
        year=2022, product="acs1", state_fips="34",
    )
    overall = result.filter(
        (pl.col("segment_dim") == "overall")
        & (pl.col("tenure_class") == "renter"),
    ).row(0, named=True)
    assert overall["suppressed"] is False
    # The income SE must be strictly positive because the replicate
    # medians disagree across the 50K/100K boundary.
    assert overall["household_income_p50_se"] is not None
    assert overall["household_income_p50_se"] > 0.0


def test_se_is_null_when_cell_is_suppressed() -> None:
    """Suppressed cells have NULL p50 AND NULL SE."""
    persons = [
        _make_person(f"S{i:013d}", pwgtp=10, pincp=50000) for i in range(5)
    ]
    housings = [
        _make_housing(
            f"S{i:013d}", ten=3, hincp=50000, grntp=1500, smocp=None, wgtp=10,
        )
        for i in range(5)
    ]
    result = compute_burden_segmented(
        pl.DataFrame(persons), pl.DataFrame(housings),
        year=2022, product="acs1", state_fips="34",
    )
    overall = result.filter(
        (pl.col("segment_dim") == "overall")
        & (pl.col("tenure_class") == "renter"),
    ).row(0, named=True)
    assert overall["suppressed"]              is True
    assert overall["household_income_p50"]    is None
    assert overall["household_income_p50_se"] is None
    assert overall["burden_ratio_p50_se"]     is None
