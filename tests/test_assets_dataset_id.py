"""Contract tests that do not require Postgres (regex only)."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from serving.routes.assets import _DATASET_ID_RE


def test_dataset_id_accepts_schema_table() -> None:
    assert _DATASET_ID_RE.fullmatch("raw.fred_observation") is not None
    assert _DATASET_ID_RE.fullmatch("derived.housing_burden_ratio") is not None


def test_dataset_id_rejects_invalid() -> None:
    assert _DATASET_ID_RE.fullmatch("Raw.Bad") is None
    assert _DATASET_ID_RE.fullmatch("no_dot") is None
    assert _DATASET_ID_RE.fullmatch("") is None
