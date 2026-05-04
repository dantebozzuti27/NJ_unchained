"""GET /assets, /asset/{dataset_id}, /assets/{schema}/{table}: dataset pane."""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, status

from serving.db import borrow_connection
from serving.models import AssetDetail, AssetSummary
from serving.queries import (
    compute_freshness_state,
    get_asset_detail,
    list_assets,
)

router = APIRouter(tags=["assets"])

# Mirrors ``ref.release_calendar.source_id`` CHECK and Dagster AssetKey naming.
_DATASET_ID_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")


def _asset_detail(dataset_id: str) -> AssetDetail:
    """Shared handler for dotted dataset IDs (e.g. ``raw.fred_observation``)."""
    with borrow_connection() as conn:
        row = get_asset_detail(conn, dataset_id=dataset_id)

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown dataset {dataset_id!r}",
        )

    state, age_h = compute_freshness_state(
        last_materialized_at=row.get("last_materialized_at"),
        expected_lag_hours=row.get("expected_lag_hours"),
    )
    return AssetDetail(
        dataset_id=row["dataset_id"],
        cadence=row.get("cadence"),
        schedule_label=row.get("schedule_label"),
        expected_lag_hours=row.get("expected_lag_hours"),
        last_materialized_at=row.get("last_materialized_at"),
        last_rows_upserted=row.get("last_rows_upserted"),
        age_hours=age_h,
        freshness_state=state,
        n_warn_30d=row.get("n_warn_30d", 0),
        n_error_30d=row.get("n_error_30d", 0),
        last_materialization_details=row.get("last_materialization_details"),
    )


@router.get(
    "/assets",
    response_model=list[AssetSummary],
    summary="All datasets with freshness state and 30-day signal counts",
)
def get_assets() -> list[AssetSummary]:
    """Return one row per dataset, joining calendar + last materialization."""
    with borrow_connection() as conn:
        rows = list_assets(conn)

    out: list[AssetSummary] = []
    for r in rows:
        state, age_h = compute_freshness_state(
            last_materialized_at=r.get("last_materialized_at"),
            expected_lag_hours=r.get("expected_lag_hours"),
        )
        out.append(AssetSummary(
            dataset_id=r["dataset_id"],
            cadence=r.get("cadence"),
            schedule_label=r.get("schedule_label"),
            expected_lag_hours=r.get("expected_lag_hours"),
            last_materialized_at=r.get("last_materialized_at"),
            last_rows_upserted=r.get("last_rows_upserted"),
            age_hours=age_h,
            freshness_state=state,
            n_warn_30d=r.get("n_warn_30d", 0),
            n_error_30d=r.get("n_error_30d", 0),
        ))
    return out


@router.get(
    "/asset/{dataset_id}",
    response_model=AssetDetail,
    summary="One dataset by dotted id (BBG-style DES)",
    responses={
        400: {"description": "Malformed dataset_id"},
        404: {"description": "Unknown dataset"},
    },
)
def get_asset_by_dataset_id(dataset_id: str) -> AssetDetail:
    """Return the same payload as ``GET /assets/{schema}/{table}`` using one segment.

    Convenience for terminals and scripts that think in ``schema.table``
    dotted paths without splitting on ``.``.
    """
    if not _DATASET_ID_RE.fullmatch(dataset_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "dataset_id must look like schema.table "
                "(e.g. raw.fred_observation); lowercase identifiers only."
            ),
        )
    return _asset_detail(dataset_id)


@router.get(
    "/assets/{schema}/{table}",
    response_model=AssetDetail,
    summary="One dataset's detail: calendar + freshness + last-materialization payload",
    responses={404: {"description": "Unknown dataset"}},
)
def get_asset(schema: str, table: str) -> AssetDetail:
    """Return detail for ``{schema}.{table}``.

    Path is split into (schema, table) rather than accepting the dotted
    form because RFC 3986 reserves '.' but its semantics in path
    segments are middleware-dependent (some proxies strip trailing
    dots, others not). Splitting is unambiguous and matches the
    AssetKey convention used by Dagster.
    """
    dataset_id = f"{schema}.{table}"
    return _asset_detail(dataset_id)
