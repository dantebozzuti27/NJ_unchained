"""Derived-layer aggregators.

Each module in this package reads from ``raw.*`` (and ``ref.*``), computes
metrics, and writes to ``derived.*``. Every output row carries:

* ``formula_version`` -- FK to ``ref.formula_version``. Bump this when
  the methodology changes; old rows remain in place so consumers can
  diff "before vs after methodology change".
* ``input_vintage_hash`` -- sha256 of the raw inputs that produced the
  row. Two reruns with the same inputs and the same formula_version
  produce byte-identical output.

Aggregators are pure with respect to their inputs: given the same
``(formula_version, raw rows, ref rows)``, they produce the same
``derived`` rows. The Postgres write is the only side effect.
"""

from derived._stats import weighted_percentile

__all__ = ["weighted_percentile"]

# Note: ``derived.pums_burden`` and ``derived.lca_aggregator`` are not
# re-exported here because they import polars/psycopg eagerly. Import
# them directly: ``from derived import pums_burden``.
