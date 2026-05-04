"""Unit tests for ``derived.lca_aggregator``.

The DB-side query (``_QUERY_OBSERVATIONS``) is exercised by the live_pg
integration test in ``test_pg_integration.py``. Here we test the pure-
Python aggregation step: given a list of observation rows, produce the
correct AggregateRow output.
"""

from __future__ import annotations

import pytest

from derived.lca_aggregator import (
    SUPPRESSION_MIN_N,
    AggregateRow,
    _ObservationRow,
    aggregate_groups,
)


def _obs(
    county: str = "NJ-MIDDLESEX",
    fy: int = 2024,
    visa: str = "H-1B",
    bus_ratio: float = 1.0,
    workers: float | None = 1.0,
    wage: float | None = 100_000.0,
    pw: float | None = 90_000.0,
) -> _ObservationRow:
    """Compact factory for tests."""
    return _ObservationRow(
        county_id=county,
        fiscal_year=fy,
        visa_class=visa,
        bus_ratio=bus_ratio,
        total_workers=workers,
        annualized_wage_from=wage,
        annualized_pw=pw,
    )


# ---------------------------------------------------------------------------
# Group keying
# ---------------------------------------------------------------------------


def test_aggregate_groups_keys_by_county_fy_visa() -> None:
    obs = [
        _obs(county="NJ-MIDDLESEX", fy=2024, visa="H-1B"),
        _obs(county="NJ-MIDDLESEX", fy=2024, visa="H-1B"),
        _obs(county="NJ-MIDDLESEX", fy=2024, visa="H-2A"),
        _obs(county="NJ-ESSEX",     fy=2024, visa="H-1B"),
        _obs(county="NJ-MIDDLESEX", fy=2023, visa="H-1B"),
    ]
    rows = aggregate_groups(obs, formula_version="1.0.0-baseline", input_vintage_hash="x" * 64)
    keys = sorted((r.county_id, r.fiscal_year, r.visa_class) for r in rows)
    assert keys == sorted([
        ("NJ-MIDDLESEX", 2023, "H-1B"),
        ("NJ-MIDDLESEX", 2024, "H-1B"),
        ("NJ-MIDDLESEX", 2024, "H-2A"),
        ("NJ-ESSEX",     2024, "H-1B"),
    ])


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------


def test_unweighted_count_is_row_count() -> None:
    obs = [_obs(bus_ratio=0.5) for _ in range(12)]
    [row] = aggregate_groups(obs, formula_version="1.0.0-baseline",
                             input_vintage_hash="x" * 64)
    assert row.n_unweighted_certs == 12


def test_weighted_count_is_sum_of_bus_ratio() -> None:
    obs = [_obs(bus_ratio=0.5) for _ in range(12)]
    [row] = aggregate_groups(obs, formula_version="1.0.0-baseline",
                             input_vintage_hash="x" * 64)
    assert row.n_certs_weighted == pytest.approx(6.0)


def test_workers_weighted_handles_none() -> None:
    """Total_workers=None contributes 0; non-null contributes workers * bus_ratio."""
    obs = [
        _obs(bus_ratio=1.0, workers=2.0),
        _obs(bus_ratio=0.5, workers=10.0),
        _obs(bus_ratio=1.0, workers=None),
    ]
    rows = aggregate_groups(obs, formula_version="1.0.0-baseline",
                            input_vintage_hash="x" * 64)
    [row] = [r for r in rows if r.county_id == "NJ-MIDDLESEX"]
    # 2.0*1.0 + 10.0*0.5 + 0*1.0 = 7.0
    assert row.n_workers_weighted == pytest.approx(7.0)


# ---------------------------------------------------------------------------
# Suppression invariant
# ---------------------------------------------------------------------------


def test_thin_cell_suppresses_percentiles() -> None:
    """A cell with <SUPPRESSION_MIN_N rows must NULL all percentile columns."""
    obs = [_obs(wage=float(100_000 + i * 1000)) for i in range(SUPPRESSION_MIN_N - 1)]
    [row] = aggregate_groups(obs, formula_version="1.0.0-baseline",
                             input_vintage_hash="x" * 64)
    assert row.n_unweighted_certs == SUPPRESSION_MIN_N - 1
    assert row.median_annualized_wage_from is None
    assert row.p25_annualized_wage_from is None
    assert row.p75_annualized_wage_from is None
    assert row.median_prevailing_wage is None


def test_just_above_threshold_keeps_percentiles() -> None:
    """A cell with exactly SUPPRESSION_MIN_N rows must keep percentiles."""
    obs = [_obs(wage=float(100_000 + i * 1000)) for i in range(SUPPRESSION_MIN_N)]
    [row] = aggregate_groups(obs, formula_version="1.0.0-baseline",
                             input_vintage_hash="x" * 64)
    assert row.n_unweighted_certs == SUPPRESSION_MIN_N
    assert row.median_annualized_wage_from is not None


# ---------------------------------------------------------------------------
# Median correctness
# ---------------------------------------------------------------------------


def test_median_with_equal_weights_matches_unweighted() -> None:
    """Equal bus_ratio across observations: weighted median == sample median.

    11 wages 50K..100K in 5K steps. With type-1 weighted percentile and
    equal weights of 1.0 (total weight 11), the result at quantile q is
    the smallest sorted value whose cumulative weight reaches q*11:

      q=0.50 -> target=5.5 -> cum=6  -> 6th value = 75K
      q=0.25 -> target=2.75 -> cum=3 -> 3rd value = 60K
      q=0.75 -> target=8.25 -> cum=9 -> 9th value = 90K
    """
    wages = [50_000.0 + i * 5_000 for i in range(11)]
    obs = [_obs(bus_ratio=1.0, wage=w) for w in wages]
    [row] = aggregate_groups(obs, formula_version="1.0.0-baseline",
                             input_vintage_hash="x" * 64)
    assert row.median_annualized_wage_from == 75_000.0
    assert row.p25_annualized_wage_from   == 60_000.0
    assert row.p75_annualized_wage_from   == 90_000.0


def test_median_skewed_by_unequal_weights() -> None:
    """A heavy weight on one value drags the weighted median toward it."""
    obs = [
        _obs(bus_ratio=0.01, wage=200_000.0),
        # 13 low-wage rows with full weight (above suppression threshold).
        *[_obs(bus_ratio=1.0, wage=50_000.0 + i * 100) for i in range(13)],
    ]
    [row] = aggregate_groups(obs, formula_version="1.0.0-baseline",
                             input_vintage_hash="x" * 64)
    assert row.median_annualized_wage_from is not None
    assert 50_000 <= row.median_annualized_wage_from < 100_000


# ---------------------------------------------------------------------------
# Output ordering + provenance fields
# ---------------------------------------------------------------------------


def test_output_is_sorted() -> None:
    obs = [
        _obs(county="NJ-UNION",  fy=2024, visa="H-1B"),
        _obs(county="NJ-ESSEX",  fy=2023, visa="H-2A"),
        _obs(county="NJ-ESSEX",  fy=2024, visa="H-1B"),
    ]
    rows = aggregate_groups(obs, formula_version="1.0.0-baseline",
                            input_vintage_hash="x" * 64)
    keys = [(r.county_id, r.fiscal_year, r.visa_class) for r in rows]
    assert keys == sorted(keys)


def test_provenance_fields_propagate() -> None:
    obs = [_obs() for _ in range(SUPPRESSION_MIN_N)]
    [row] = aggregate_groups(
        obs,
        formula_version="9.9.9-test",
        input_vintage_hash="abc" * 21 + "x",  # 64-char fake hash
    )
    assert isinstance(row, AggregateRow)
    assert row.formula_version == "9.9.9-test"
    assert row.input_vintage_hash == "abc" * 21 + "x"
