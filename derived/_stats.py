r"""Weighted statistical primitives.

Used by every aggregator that allocates observations across geographies
via crosswalk ratios (LCA via HUD ``bus_ratio``; PUMS PUMA + county
aggregators via the population-weighted PUMA-county xwalk). Implementing
these here once -- with property tests -- avoids per-aggregator
reinvention.

Definitions
-----------
The **weighted q-quantile** (or "type 1 weighted percentile") of values
:math:`v_1, \ldots, v_n` with non-negative weights :math:`w_1, \ldots,
w_n` is the smallest :math:`v_{(k)}` (after sorting by value) such that
the cumulative weight up to and including :math:`v_{(k)}` is at least
:math:`q \cdot \sum_i w_i`.

Equivalent definitions exist (Hyndman-Fan types 4-9 with weighted
extensions); type 1 is the standard for survey-data weighted
percentiles. We use it because:

1. It is monotone in the weights -- doubling all weights does not change
   the percentile.
2. It produces an actual observed value (no interpolation), which is
   what suppression-checked publication of LCA wages requires.
3. It is computable in O(n log n) without numerical stability concerns.

Successive Differences Replication (SDR)
----------------------------------------
ACS PUMS publishes 80 replicate weights per record so that the Bureau's
Successive Differences Replication variance estimator can be applied
to any user-defined statistic. The formula:

.. math::

    \widehat{V}(\hat{\theta}) = \frac{4}{R}
        \sum_{r=1}^{R} (\hat{\theta}_{(r)} - \hat{\theta})^2,
    \quad R = 80

where :math:`\hat{\theta}` is the point estimate using the main weights
(``PWGTP``/``WGTP``) and :math:`\hat{\theta}_{(r)}` is the same statistic
recomputed using replicate weight set :math:`r`. The scaling factor
``4/R`` is specific to the ACS sample design (it would be ``2/R`` for
balanced-half-samples, or ``(R-1)/R`` for jackknife). Reference: Census
*ACS PUMS Accuracy of the Data* technical doc.

The point estimator can be any function of values + weights -- a
weighted percentile, a mean, a ratio of two percentiles, etc. The
helpers below cover the two we need today (single percentile, ratio
of two percentiles).

Why store SE rather than CIs?
* SE is a single number; any alpha-level CI is derivable as
  ``theta +/- z_alpha * se``. Storing CIs forces the choice of alpha
  at materialization time.
* It mirrors how Census publishes ACS estimates: tables ship with
  margins-of-error at 90% but the underlying SE is what consumers
  need for hypothesis testing.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence


# Number of replicate weights per ACS PUMS record. Hardcoded because
# the SDR scaling factor is design-specific; a future Census
# methodology change would require both an ingester schema migration
# AND a re-derivation of the scale.
R_PUMS_REPLICATES: Final[int] = 80

# The SDR scaling factor for ACS. Variance = SDR_SCALE * sum-of-squared-deviations.
SDR_SCALE: Final[float] = 4.0 / R_PUMS_REPLICATES

# Minimum fraction of replicates that must produce a finite point
# estimate for the SE to be considered reliable. Below this, we
# return None for SE rather than report a noisy estimate. The 0.5
# threshold is conservative; Census-published SEs typically use
# all 80 replicates.
_MIN_VALID_REPLICATE_FRACTION: Final[float] = 0.5


def weighted_percentile(
    values: Iterable[float | None],
    weights: Iterable[float | None],
    q: float,
) -> float | None:
    r"""Return the type-1 weighted q-quantile of *values* with *weights*.

    Args:
        values: Observation values. ``None`` entries are dropped.
        weights: Non-negative weights. ``None`` and zero/negative weights
            are dropped (paired drop with ``values``).
        q: Quantile in ``[0.0, 1.0]``.

    Returns:
        The smallest value :math:`v_{(k)}` whose cumulative weight reaches
        :math:`q \cdot \sum w`, or ``None`` if no positive-weight
        observations remain after filtering.

    Raises:
        ValueError: if ``q`` is not in ``[0, 1]``.

    Examples:
        >>> weighted_percentile([1.0, 2.0, 3.0], [1.0, 1.0, 1.0], 0.5)
        2.0
        >>> weighted_percentile([1.0, 2.0, 3.0], [1.0, 1.0, 1.0], 0.0)
        1.0
        >>> weighted_percentile([1.0, 2.0, 3.0], [1.0, 1.0, 1.0], 1.0)
        3.0
        >>> weighted_percentile([10.0, 20.0], [9.0, 1.0], 0.5)
        10.0
        >>> weighted_percentile([], [], 0.5) is None
        True

    """
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"quantile q must be in [0, 1], got {q}")

    paired: list[tuple[float, float]] = [
        (float(v), float(w))
        for v, w in zip(values, weights, strict=True)
        if v is not None and w is not None and float(w) > 0.0
    ]
    if not paired:
        return None
    paired.sort(key=lambda x: x[0])

    total_w = sum(w for _, w in paired)
    target = q * total_w

    cum = 0.0
    for v, w in paired:
        cum += w
        if cum >= target:
            return v
    # Numerical fall-through: return the largest value (q == 1.0 typically
    # hits this since cum may equal target exactly, but for safety).
    return paired[-1][0]


# ============================================================================
# Successive Differences Replication (SDR)
# ============================================================================


def weighted_percentile_sdr(
    values: Sequence[float | None],
    main_weights: Sequence[float | None],
    replicate_weights: Sequence[Sequence[float | None] | None],
    q: float,
    *,
    sdr_scale: float = SDR_SCALE,
) -> tuple[float | None, float | None]:
    r"""Return ``(point_estimate, standard_error)`` for a weighted q-quantile.

    The point estimate is computed with ``main_weights``. The standard
    error is computed via Successive Differences Replication:

    .. math::

        \widehat{SE}(\hat{\theta}) = \sqrt{
            \frac{4}{R}\sum_{r=1}^{R}(\hat{\theta}_{(r)} - \hat{\theta})^2
        }

    where each :math:`\hat{\theta}_{(r)}` is the q-quantile recomputed
    using replicate weight set :math:`r` (same observations, different
    weights).

    Args:
        values:            Length-N observation values; aligned with both
                           ``main_weights`` and the rows of
                           ``replicate_weights``.
        main_weights:      Length-N main weights (``WGTP`` or ``PWGTP``).
        replicate_weights: Length-N sequence of length-R replicate-weight
                           sequences. ``replicate_weights[i][r]`` is row
                           :math:`i`'s weight under replicate :math:`r`.
                           Use ``None`` for missing rows; the function
                           drops them paired with the main weights.
        q:                 Quantile in ``[0, 1]``.
        sdr_scale:         Variance scaling factor. Defaults to
                           :data:`SDR_SCALE` (4/80) for ACS PUMS.

    Returns:
        ``(p, se)`` where ``p`` is the main-weight quantile and ``se``
        is the SDR-based standard error. ``p`` is ``None`` if the main
        estimate is undefined (no data); ``se`` is ``None`` if too few
        replicates produced a finite estimate (less than half).

    Raises:
        ValueError: if the input lengths do not align.

    """
    if not 0.0 <= q <= 1.0:
        msg = f"quantile q must be in [0, 1], got {q}"
        raise ValueError(msg)

    n = len(values)
    if len(main_weights) != n or len(replicate_weights) != n:
        msg = (
            f"input length mismatch: values={n}, "
            f"main_weights={len(main_weights)}, "
            f"replicate_weights={len(replicate_weights)}"
        )
        raise ValueError(msg)

    point = weighted_percentile(values, main_weights, q)
    if point is None:
        return None, None

    if n == 0:
        return point, None
    # All rows must have replicate-weight columns of equal length;
    # take R from the first non-None row.
    r_len = 0
    for rw in replicate_weights:
        if rw is not None:
            r_len = len(rw)
            break
    if r_len == 0:
        return point, None

    replicate_estimates: list[float] = []
    for r in range(r_len):
        rep_w = [
            (rw[r] if (rw is not None and r < len(rw)) else None)
            for rw in replicate_weights
        ]
        est = weighted_percentile(values, rep_w, q)
        if est is not None:
            replicate_estimates.append(est)

    if len(replicate_estimates) < r_len * _MIN_VALID_REPLICATE_FRACTION:
        return point, None

    # SDR: variance = scale * sum of squared deviations from the point
    # estimate. Use the actual count r_len (not len(replicate_estimates))
    # to keep the scaling consistent with Census's published methodology;
    # missing replicates contribute zero deviation rather than rescaling
    # the formula. (This is conservative -- if many replicates fail the
    # variance is biased downward, which is why we gate on the
    # MIN_VALID_REPLICATE_FRACTION above.)
    ssd = sum((est - point) ** 2 for est in replicate_estimates)
    variance = sdr_scale * ssd
    return point, math.sqrt(variance)


def ratio_of_percentiles_sdr(
    numer_values:      Sequence[float | None],
    denom_values:      Sequence[float | None],
    main_weights:      Sequence[float | None],
    replicate_weights: Sequence[Sequence[float | None] | None],
    *,
    numer_multiplier:  float = 1.0,
    q:                 float = 0.5,
    sdr_scale:         float = SDR_SCALE,
) -> tuple[float | None, float | None]:
    r"""SE-aware ratio of two weighted percentiles, computed jointly per replicate.

    Used for the per-cell housing-burden ratio:
    :math:`\hat{R} = (12 \cdot \mathrm{cost}_{p50}) / \mathrm{income}_{p50}`.

    The SE is NOT computed via the delta method (which assumes
    asymptotic normality of both numerator and denominator and requires
    a covariance term). Instead we compute the ratio directly under
    each replicate -- :math:`\hat{R}_{(r)} = (12 \cdot \mathrm{cost}_{p50,(r)})
    / \mathrm{income}_{p50,(r)}` -- and apply the same SDR formula to
    the ratio. This preserves the joint distribution of numerator and
    denominator across replicates, which is the right thing to do when
    the same weight column drives both percentiles.

    Args:
        numer_values:      Aligned numerator values.
        denom_values:      Aligned denominator values.
        main_weights:      Length-N main weights.
        replicate_weights: As in :func:`weighted_percentile_sdr`.
        numer_multiplier:  Scalar applied to the numerator percentile.
                           For housing burden, pass 12.0 to convert
                           monthly cost to annualized.
        q:                 Quantile (typically 0.5 for medians).
        sdr_scale:         SDR scaling factor.

    Returns:
        ``(ratio, se)``. ``ratio`` is None when either percentile is
        None or denom is non-positive; ``se`` is None when too few
        replicates produced finite ratios.

    """
    if not 0.0 <= q <= 1.0:
        msg = f"quantile q must be in [0, 1], got {q}"
        raise ValueError(msg)

    n = len(numer_values)
    if (
        len(denom_values) != n
        or len(main_weights) != n
        or len(replicate_weights) != n
    ):
        msg = (
            f"input length mismatch: numer={n}, denom={len(denom_values)}, "
            f"main_w={len(main_weights)}, rep_w={len(replicate_weights)}"
        )
        raise ValueError(msg)

    numer_main = weighted_percentile(numer_values, main_weights, q)
    denom_main = weighted_percentile(denom_values, main_weights, q)
    if numer_main is None or denom_main is None or denom_main <= 0:
        return None, None
    ratio_main = (numer_multiplier * numer_main) / denom_main

    r_len = 0
    for rw in replicate_weights:
        if rw is not None:
            r_len = len(rw)
            break
    if r_len == 0:
        return ratio_main, None

    replicate_ratios: list[float] = []
    for r in range(r_len):
        rep_w = [
            (rw[r] if (rw is not None and r < len(rw)) else None)
            for rw in replicate_weights
        ]
        nr = weighted_percentile(numer_values, rep_w, q)
        dr = weighted_percentile(denom_values, rep_w, q)
        if nr is not None and dr is not None and dr > 0:
            replicate_ratios.append((numer_multiplier * nr) / dr)

    if len(replicate_ratios) < r_len * _MIN_VALID_REPLICATE_FRACTION:
        return ratio_main, None

    ssd = sum((rr - ratio_main) ** 2 for rr in replicate_ratios)
    variance = sdr_scale * ssd
    return ratio_main, math.sqrt(variance)
