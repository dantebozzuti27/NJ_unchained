"""Fraud-detection metric API.

Three endpoints:

  GET  /fec/metrics                  -> catalog (all signals + metadata)
  GET  /fec/metrics/_summary         -> total flagged-row count per signal
  GET  /fec/metrics/{metric_id}      -> paginated flagged rows for one signal
  GET  /fec/metrics/{metric_id}/csv  -> CSV export for the same

Why a generic ``{metric_id}`` URL instead of one path per signal:
the signal set is a registry (``serving.queries_fec_metrics._METRIC_CATALOG``)
and adding a new signal should require a single registry entry, not a
new route. The metric_id is whitelisted against the registry on every
request, so opening the URL space does not expose unexpected views.

Error semantics
---------------
* Unknown metric_id     -> 404 with the catalog listed
* Bad sort_by column    -> 400 with the allowed columns listed
* Connection unavailable -> 503 with retry-after (handled by serving.db)

These match the existing /fec endpoints' contract.
"""

from __future__ import annotations

import csv
import io
import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Path, Query
from fastapi.responses import StreamingResponse

from serving.db import borrow_connection
from serving.models import (
    FraudMetricCatalogEntry,
    FraudMetricResult,
    FraudMetricSummary,
)
from serving.queries_fec_metrics import (
    MAX_EXPORT,
    MetricSpec,
    get_catalog,
    get_metric,
    list_metric,
    metric_counts,
    stream_metric,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

log = logging.getLogger(__name__)

router = APIRouter(prefix="/fec/metrics", tags=["fec_metrics"])


# ============================================================================
# Catalog -> public API translation
# ============================================================================

def _to_catalog_entry(spec: MetricSpec) -> FraudMetricCatalogEntry:
    """Convert an internal MetricSpec to its public Pydantic projection.

    The internal spec carries SQL-side state (view name, sortable
    column whitelist) that the API deliberately does not expose. The
    public entry exposes only what the UI needs to render.
    """
    return FraudMetricCatalogEntry(
        id=spec.id,
        name=spec.name,
        tier=spec.tier,
        description=spec.description,
        threshold_note=spec.threshold_note,
        sort_default=spec.sort_default,
        primary_key_cols=list(spec.primary_key_cols),
    )


# ============================================================================
# Routes
# ============================================================================

@router.get(
    "",
    response_model=list[FraudMetricCatalogEntry],
    summary="List all available fraud-detection metrics",
)
def get_metrics_catalog() -> list[FraudMetricCatalogEntry]:
    """Return the static signal catalog (no DB roundtrip).

    Ordered (tier, id) so the UI can render groups -> items directly.
    """
    return [_to_catalog_entry(s) for s in get_catalog()]


@router.get(
    "/_summary",
    response_model=FraudMetricSummary,
    summary="Total flagged-row count per metric (one cycle)",
)
def get_metrics_summary(
    cycle: str | None = Query(default=None, examples=["2024"]),
) -> FraudMetricSummary:
    """Cardinality dashboard for the Metrics tab header.

    Roughly N small COUNT queries (currently 8). The serving layer
    holds the connection until the loop completes; for catalog growth
    past ~30 metrics we'll fold these into a single UNION ALL.
    """
    with borrow_connection() as conn:
        counts = metric_counts(conn, cycle=cycle)
    return FraudMetricSummary(cycle=cycle or "", counts=counts)


@router.get(
    "/{metric_id}",
    response_model=FraudMetricResult,
    summary="Paginated flagged rows for a single metric",
)
def get_metric_rows(  # noqa: D103 (public-facing route docstring lives in summary=)
    metric_id: str       = Path(..., examples=["treasurer_concentration"]),
    cycle:     str | None = Query(default=None, examples=["2024"]),
    sort_by:   str | None = Query(
        default=None,
        description=(
            "Column to sort by. Must be one of the metric's whitelisted "
            "sort columns; see the catalog for the allowed set."
        ),
    ),
    sort_dir:  str | None = Query(default="DESC", pattern="^(?i)(asc|desc)$"),
    limit:     int        = Query(default=100, ge=1, le=1000),
    offset:    int        = Query(default=0, ge=0),
) -> FraudMetricResult:
    try:
        spec = get_metric(metric_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": str(exc),
                "available_metrics": [m.id for m in get_catalog()],
            },
        ) from exc

    try:
        with borrow_connection() as conn:
            rows, total = list_metric(
                conn,
                metric_id=metric_id,
                cycle=cycle,
                sort_by=sort_by,
                sort_dir=sort_dir,
                limit=limit,
                offset=offset,
            )
    except KeyError as exc:
        # Bad sort_by -- queries_fec_metrics raises KeyError with the
        # allowed-column list embedded.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return FraudMetricResult(
        metric=_to_catalog_entry(spec),
        rows=rows,
        total_count=total,
        limit=limit,
        offset=offset,
    )


# ============================================================================
# CSV export
# ============================================================================

def _csv_stream(columns: list[str], cursor: object) -> Iterator[bytes]:
    """Yield CSV bytes (header + rows) from a Postgres server-side cursor.

    Mirrors the helper in routes/fec_export.py byte-for-byte. We
    intentionally duplicate rather than import to keep the metrics
    module self-contained: it has no dependency on the row-list export
    layer's import graph.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    writer.writerow(columns)
    yield buf.getvalue().encode("utf-8")
    buf.seek(0)
    buf.truncate()

    for row in cursor:  # type: ignore[attr-defined]
        # Server-side cursor row is a tuple. Coerce arrays/None for CSV.
        writer.writerow([
            "" if v is None
            else (
                "{" + ",".join(str(x) for x in v) + "}"
                if isinstance(v, list)
                else v
            )
            for v in row
        ])
        yield buf.getvalue().encode("utf-8")
        buf.seek(0)
        buf.truncate()


@router.get(
    "/{metric_id}/csv",
    response_class=StreamingResponse,
    summary="CSV export of a metric's flagged rows (constant-memory stream)",
)
def export_metric_csv(  # noqa: D103 (public-facing route docstring lives in summary=)
    metric_id: str        = Path(..., examples=["treasurer_concentration"]),
    cycle:     str | None = Query(default=None, examples=["2024"]),
    sort_by:   str | None = Query(default=None),
    sort_dir:  str | None = Query(default="DESC", pattern="^(?i)(asc|desc)$"),
) -> StreamingResponse:
    # Validate metric_id and sort_by BEFORE entering the streaming
    # generator. We want clean 4xx responses, not partial CSV bodies
    # with HTTP 200 followed by a server-side traceback.
    try:
        spec = get_metric(metric_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": str(exc),
                "available_metrics": [m.id for m in get_catalog()],
            },
        ) from exc
    requested_sort = sort_by or spec.sort_default
    if requested_sort not in spec.sort_cols:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Sort column {requested_sort!r} not allowed for metric "
                f"{metric_id!r}. Allowed: {sorted(spec.sort_cols)}"
            ),
        )

    def gen() -> Iterator[bytes]:
        with borrow_connection() as conn:
            cols, cur = stream_metric(
                conn,
                metric_id=metric_id,
                cycle=cycle,
                sort_by=sort_by,
                sort_dir=sort_dir,
                cap=MAX_EXPORT,
            )
            try:
                yield from _csv_stream(cols, cur)
            finally:
                cur.close()

    suffix = f"_{cycle}" if cycle else ""
    return StreamingResponse(
        gen(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="metric_{metric_id}{suffix}.csv"',
            "X-Export-Cap-Rows":   str(MAX_EXPORT),
        },
    )
