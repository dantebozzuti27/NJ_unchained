"""Unit tests for ``derived._stats``.

We use Hypothesis heavily here because the weighted-percentile contract
has several invariants that are easy to state and hard to hand-test:

* Doubling all weights does not change the result (scale invariance).
* The result is always one of the input values (type-1 quantile property).
* Result at q=0 is the min; at q=1 is the max.
* Equal weights reduce to the unweighted "select the q-th element after
  sorting" (matches the sample percentile up to the
  type-1 vs type-7 difference, which only matters at non-tertile q's).
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from derived._stats import (
    R_PUMS_REPLICATES,
    SDR_SCALE,
    ratio_of_percentiles_sdr,
    weighted_percentile,
    weighted_percentile_sdr,
)

# ---------------------------------------------------------------------------
# Fixed-input regression tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "weights", "q", "expected"),
    [
        # Equal weights, simple median.
        ([1.0, 2.0, 3.0], [1.0, 1.0, 1.0], 0.5, 2.0),
        # q=0 returns min; q=1 returns max.
        ([1.0, 2.0, 3.0], [1.0, 1.0, 1.0], 0.0, 1.0),
        ([1.0, 2.0, 3.0], [1.0, 1.0, 1.0], 1.0, 3.0),
        # Heavy weight on first value: median is the first value.
        ([10.0, 20.0], [9.0, 1.0], 0.5, 10.0),
        # Heavy weight on second value: median is the second value.
        ([10.0, 20.0], [1.0, 9.0], 0.5, 20.0),
        # Single observation.
        ([42.0], [1.0], 0.5, 42.0),
        # Out-of-order input must still yield the correct sorted percentile.
        ([5.0, 3.0, 4.0, 1.0, 2.0], [1.0] * 5, 0.5, 3.0),
    ],
)
def test_weighted_percentile_fixed(
    values: list[float], weights: list[float], q: float, expected: float,
) -> None:
    assert weighted_percentile(values, weights, q) == expected


def test_weighted_percentile_drops_nones() -> None:
    """None observations and None/zero weights are dropped pairwise.

    After dropping the middle entry, the remaining pairs are
    (1.0, w=1) and (3.0, w=1). Total weight = 2; target = 0.5*2 = 1.0;
    cumulative weight at value 1.0 is 1.0 (>= target) -> return 1.0.
    """
    assert weighted_percentile([1.0, None, 3.0], [1.0, 1.0, 1.0], 0.5) == 1.0
    assert weighted_percentile([1.0, 2.0, 3.0], [1.0, None, 1.0], 0.5) == 1.0
    assert weighted_percentile([1.0, 2.0, 3.0], [1.0, 0.0, 1.0], 0.5) == 1.0


def test_weighted_percentile_empty_returns_none() -> None:
    assert weighted_percentile([], [], 0.5) is None
    assert weighted_percentile([1.0], [0.0], 0.5) is None
    assert weighted_percentile([None], [1.0], 0.5) is None


def test_weighted_percentile_q_out_of_range_raises() -> None:
    with pytest.raises(ValueError, match="quantile q must be in"):
        weighted_percentile([1.0], [1.0], 1.5)
    with pytest.raises(ValueError, match="quantile q must be in"):
        weighted_percentile([1.0], [1.0], -0.1)


# ---------------------------------------------------------------------------
# Hypothesis property tests
# ---------------------------------------------------------------------------


_pos_floats = st.floats(min_value=0.001, max_value=1_000_000,
                        allow_nan=False, allow_infinity=False)
_pos_weights = st.floats(min_value=0.001, max_value=1_000,
                         allow_nan=False, allow_infinity=False)


@given(
    values=st.lists(_pos_floats, min_size=1, max_size=50),
    weights=st.lists(_pos_weights, min_size=1, max_size=50),
    q=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    scale=st.floats(min_value=0.01, max_value=100.0, allow_nan=False, allow_infinity=False),
)
@settings(suppress_health_check=[HealthCheck.differing_executors])
def test_scale_invariance_in_weights(
    values: list[float], weights: list[float], q: float, scale: float,
) -> None:
    """Multiplying all weights by a positive constant does not change the result."""
    n = min(len(values), len(weights))
    v, w = values[:n], weights[:n]
    base = weighted_percentile(v, w, q)
    scaled = weighted_percentile(v, [wi * scale for wi in w], q)
    assert base == scaled


@given(
    values=st.lists(_pos_floats, min_size=1, max_size=50),
    weights=st.lists(_pos_weights, min_size=1, max_size=50),
)
def test_q0_is_min_q1_is_max(
    values: list[float], weights: list[float],
) -> None:
    """q=0 returns the minimum; q=1 returns the maximum."""
    n = min(len(values), len(weights))
    v, w = values[:n], weights[:n]
    assert weighted_percentile(v, w, 0.0) == min(v)
    assert weighted_percentile(v, w, 1.0) == max(v)


@given(
    values=st.lists(_pos_floats, min_size=1, max_size=50),
    weights=st.lists(_pos_weights, min_size=1, max_size=50),
    q=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
)
def test_result_is_an_input_value(
    values: list[float], weights: list[float], q: float,
) -> None:
    """Type-1 weighted percentile always returns an observed value (no interpolation)."""
    n = min(len(values), len(weights))
    v, w = values[:n], weights[:n]
    result = weighted_percentile(v, w, q)
    assert result in v


@given(
    values=st.lists(_pos_floats, min_size=1, max_size=50),
    weights=st.lists(_pos_weights, min_size=1, max_size=50),
)
def test_monotone_in_q(
    values: list[float], weights: list[float],
) -> None:
    """Result must be non-decreasing in q."""
    n = min(len(values), len(weights))
    v, w = values[:n], weights[:n]
    qs = [0.0, 0.25, 0.5, 0.75, 1.0]
    results = [weighted_percentile(v, w, q) for q in qs]
    assert results == sorted(results)


# ============================================================================
# SDR (Successive Differences Replication)
# ============================================================================


def test_sdr_constants() -> None:
    """ACS PUMS uses 80 replicate weights with scale 4/80 = 0.05."""
    assert R_PUMS_REPLICATES == 80
    assert pytest.approx(0.05) == SDR_SCALE


def test_sdr_identical_replicates_yield_zero_se() -> None:
    """When all replicate weights equal the main weight, SE = 0 by construction.

    This is the synthetic-data baseline: our test fixtures store
    ``[W]*80`` as replicate weights, so the materialized SE on
    synthetic data must be exactly zero. If a future refactor
    introduces noise, this test will catch it.
    """
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    main_w = [1.0, 1.0, 1.0, 1.0, 1.0]
    rep_w  = [[1.0] * 80 for _ in range(5)]

    p, se = weighted_percentile_sdr(values, main_w, rep_w, q=0.5)
    assert p  == 30.0
    assert se == 0.0


def test_sdr_known_variance_simple_two_outcome() -> None:
    """Hand-computable SDR variance for a 2-outcome flip.

    Setup: 2 observations, values [10, 20], main weight [1, 1].
    Main median = 10 (type-1 takes the lower value at the boundary).

    Replicate-weight scheme: 4 replicates that flip which value
    "wins" the median:
        rep 1: weights [2, 1] -> p50 = 10
        rep 2: weights [2, 1] -> p50 = 10
        rep 3: weights [1, 2] -> p50 = 20
        rep 4: weights [1, 2] -> p50 = 20

    Deviations from main (10): [0, 0, 10, 10]
    Sum of squared deviations: 0 + 0 + 100 + 100 = 200
    With sdr_scale = 4/4 = 1.0 (custom for this test, not PUMS's 4/80):
        variance = 1.0 * 200 = 200
        se = sqrt(200) ~= 14.14
    """
    import math

    values = [10.0, 20.0]
    main_w = [1.0,  1.0]
    rep_w  = [
        [2.0, 2.0, 1.0, 1.0],  # row 0's weight under each of 4 replicates
        [1.0, 1.0, 2.0, 2.0],  # row 1's weight under each of 4 replicates
    ]
    p, se = weighted_percentile_sdr(
        values, main_w, rep_w, q=0.5, sdr_scale=1.0,
    )
    assert p  == 10.0
    assert se == pytest.approx(math.sqrt(200), rel=1e-9)


def test_sdr_returns_se_none_when_too_few_replicates_succeed() -> None:
    """If fewer than half the replicates produce a finite estimate, return None.

    Construct a case where all 80 replicate weights are zero, so each
    replicate produces no estimate. SE must be None (not zero), to
    avoid silently reporting "no uncertainty" when in fact we have
    no information.
    """
    values = [10.0, 20.0, 30.0]
    main_w = [1.0,  1.0,  1.0]
    rep_w  = [[0.0] * 80, [0.0] * 80, [0.0] * 80]
    p, se = weighted_percentile_sdr(values, main_w, rep_w, q=0.5)
    assert p  == 20.0  # main estimate still works
    assert se is None  # SE is None because all replicates failed


def test_sdr_returns_none_se_when_no_replicates() -> None:
    """Empty replicate-weights yields None SE."""
    p, se = weighted_percentile_sdr(
        [10.0, 20.0], [1.0, 1.0], [None, None], q=0.5,
    )
    assert p  == 10.0
    assert se is None


def test_sdr_input_length_mismatch_raises() -> None:
    """All three input vectors must have the same length."""
    with pytest.raises(ValueError, match="length mismatch"):
        weighted_percentile_sdr([1.0, 2.0], [1.0], [None], q=0.5)


def test_ratio_of_percentiles_identical_replicates_yield_zero_se() -> None:
    """Same baseline check for the ratio helper."""
    numer = [1500.0] * 5
    denom = [60_000.0] * 5
    main_w = [1.0] * 5
    rep_w  = [[1.0] * 80 for _ in range(5)]
    r, se = ratio_of_percentiles_sdr(
        numer, denom, main_w, rep_w, numer_multiplier=12.0,
    )
    assert r  == pytest.approx(1500.0 * 12.0 / 60_000.0)  # 0.30
    assert se == 0.0


def test_ratio_of_percentiles_with_zero_denominator_returns_none() -> None:
    """Ratio with denom <= 0 must return (None, None), not divide by zero."""
    numer  = [1500.0]
    denom  = [0.0]
    main_w = [1.0]
    rep_w  = [[1.0] * 80]
    r, se = ratio_of_percentiles_sdr(numer, denom, main_w, rep_w)
    assert r  is None
    assert se is None


def test_ratio_of_percentiles_propagates_replicate_failures() -> None:
    """A replicate where the denom percentile collapses to 0 is dropped.

    If too many drop, SE is None. Construct a case where 80 replicates
    are split: 60 valid (positive denom), 20 with zero weights so
    denom percentile is None. 60/80 = 75% > 50% threshold -> SE is
    finite.
    """
    numer  = [1000.0, 2000.0]
    denom  = [50_000.0, 80_000.0]
    main_w = [1.0,    1.0]
    rep_w  = [
        # 60 valid replicates, 20 with zero weights
        [1.0] * 60 + [0.0] * 20,
        [1.0] * 60 + [0.0] * 20,
    ]
    r, se = ratio_of_percentiles_sdr(
        numer, denom, main_w, rep_w, numer_multiplier=12.0,
    )
    assert r is not None
    assert se is not None
    assert se == 0.0  # all valid replicates produce the same ratio
