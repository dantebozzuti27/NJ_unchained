"""GET /health: liveness + dependency state."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, status

from serving.db import borrow_connection
from serving.models import Health
from serving.queries import count_recent_errors, select_one

router = APIRouter(tags=["meta"])


@router.get(
    "/health",
    response_model=Health,
    status_code=status.HTTP_200_OK,
    summary="Service liveness + recent error count",
)
def get_health() -> Health:
    """Return service status.

    'ok'        -- DB reachable AND no critical errors in the last 1h.
    'degraded'  -- DB reachable but >=1 error/fatal signal in the
                   last 1h (the operator should investigate).

    We deliberately do NOT return 5xx when 'degraded'; clients should
    keep working off cached data while operators fix the backend.
    """
    with borrow_connection() as conn:
        db_ok = select_one(conn) == 1
        n_errors = count_recent_errors(conn, hours=1) if db_ok else 0
    return Health(
        status="ok" if (db_ok and n_errors == 0) else "degraded",
        db_reachable=db_ok,
        n_errors_last_1h=n_errors,
        timestamp=dt.datetime.now(dt.UTC),
    )
