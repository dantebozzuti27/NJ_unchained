"""GET /releases and GET /release-calendar: publication calendar surfaces."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Query

from serving.db import borrow_connection
from serving.models import (
    ReleaseCalendarHorizonRow,
    ReleaseCalendarPanel,
    ReleaseCalendarRow,
)
from serving.queries import (
    compute_freshness_state,
    list_release_calendar,
    list_release_calendar_detailed,
)
from serving.release_schedule import compute_release_calendar_row

router = APIRouter(tags=["releases"])


@router.get(
    "/releases",
    response_model=list[ReleaseCalendarRow],
    summary="Per-source publication calendar",
)
def get_releases() -> list[ReleaseCalendarRow]:
    """Return all ref.release_calendar rows.

    The 'expected_lag_hours' field is the slack we tolerate before
    declaring a source stale; it should match the FreshnessPolicy
    fail_window in the orchestration code (single source of truth
    in SQL, mirrored in Python decorators).
    """
    with borrow_connection() as conn:
        rows = list_release_calendar(conn)
    return [ReleaseCalendarRow.model_validate(r) for r in rows]


@router.get(
    "/release-calendar",
    response_model=ReleaseCalendarPanel,
    summary="Upcoming releases, next expected time per source, overdue flags",
)
def get_release_calendar(
    days: int = Query(
        14,
        ge=1,
        le=366,
        description="Number of days forward from now (UTC) for upcoming_releases.",
    ),
) -> ReleaseCalendarPanel:
    """ECO-style panel: what may publish soon, when each source is due next, what is late.

    ``upcoming_releases`` lists UTC instants within the horizon when the
    schedule can be resolved from structured columns. ``next_expected_at``
    is always the first scheduled instant after *as_of* when computable,
    even if that falls after the horizon. Rows without enough structure
    (``on_event``, sparse quarterly, FEC monthly without a day anchor)
    set ``schedule_computed`` false.
    """
    as_of = dt.datetime.now(dt.UTC)
    out_rows: list[ReleaseCalendarHorizonRow] = []
    with borrow_connection() as conn:
        detailed = list_release_calendar_detailed(conn)
    for r in detailed:
        state, age_h = compute_freshness_state(
            last_materialized_at=r.get("last_materialized_at"),
            expected_lag_hours=r.get("expected_lag_hours"),
            now=as_of,
        )
        upcoming, next_at, ok = compute_release_calendar_row(
            r, now_utc=as_of, horizon_days=days,
        )
        overdue = state == "stale"
        out_rows.append(
            ReleaseCalendarHorizonRow(
                source_id=r["source_id"],
                cadence=r["cadence"],
                schedule_label=r["schedule_label"],
                timezone=r["timezone"],
                expected_lag_hours=r["expected_lag_hours"],
                notes=r.get("notes"),
                last_materialized_at=r.get("last_materialized_at"),
                age_hours=age_h,
                freshness_state=state,
                overdue=overdue,
                upcoming_releases=upcoming,
                next_expected_at=next_at,
                schedule_computed=ok,
            ),
        )
    return ReleaseCalendarPanel(as_of=as_of, horizon_days=days, sources=out_rows)
