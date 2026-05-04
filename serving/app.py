"""Top-level FastAPI application.

Loaded by uvicorn via ``serving.app:app``. The lifespan handler manages
the shared psycopg pool: it eagerly initializes the pool on startup
(so the first request is not penalized by pool warm-up) and closes
it on shutdown (so connections are returned to Postgres cleanly).

Middleware
----------
* X-Request-Id and X-Query-Time-Ms response headers via :func:`add_observability`.
  These are minimal -- enough for log correlation and slow-query
  visibility without bringing in a heavyweight tracing stack.

Versioning
----------
v0 has no URL versioning prefix because there is no v1 to differentiate
from yet. When we ship a breaking change, we will mount a ``/v1`` and
``/v2`` simultaneously and deprecate v0 with a sunset header. Adding
versioning prematurely would create an empty namespace nobody uses.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, cast

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from serving import db
from serving.routes import (
    assets,
    burden,
    counties,
    fec,
    fec_export,
    fec_metrics,
    fec_risk,
    health,
    hpi_income,
    pums_burden,
    pums_burden_county,
    pums_burden_county_series,
    releases,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.responses import Response

log = logging.getLogger(__name__)


# Front-end is shipped as static files alongside the package: /static
# serves JS/CSS, and ``GET /`` returns index.html. Resolved at import
# time so FastAPI fails fast (rather than at first request) if the
# static directory is somehow missing from the install.
_STATIC_DIR: Path = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Eagerly init the pool on startup; tear it down on shutdown."""
    log.info("nj-serve: warming Postgres connection pool")
    db.get_pool()
    yield
    log.info("nj-serve: closing connection pool")
    db.close_pool()


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """Attach X-Request-Id and X-Query-Time-Ms response headers."""

    async def dispatch(self, request: Request, call_next: object) -> Response:
        """Tag every response with X-Request-Id and X-Query-Time-Ms."""
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        t0 = time.perf_counter()
        # Starlette's BaseHTTPMiddleware passes the next-callable as a
        # generic Callable. Cast the result rather than weakening the
        # annotation; the runtime contract is "always Response".
        response = cast("Response", await call_next(request))  # type: ignore[operator]
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        response.headers["X-Request-Id"] = request_id
        response.headers["X-Query-Time-Ms"] = f"{elapsed_ms:.1f}"
        log.info(
            "request_id=%s method=%s path=%s status=%s elapsed_ms=%.1f",
            request_id, request.method, request.url.path,
            response.status_code, elapsed_ms,
        )
        return response


def create_app() -> FastAPI:
    """Construct the FastAPI app + register routes/middleware.

    Exposed as a factory (rather than a module-level constant) so tests
    can build an app per-test if they need to swap the lifespan.
    """
    app = FastAPI(
        title="NJ Housing Burden Platform API",
        description=(
            "Read-only API over the platform's curated views. The "
            "BBG-terminal-style consumer surface for Tier 0/1/2 data."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(ObservabilityMiddleware)
    app.include_router(health.router)
    app.include_router(releases.router)
    app.include_router(assets.router)
    app.include_router(burden.router)
    app.include_router(hpi_income.router)
    app.include_router(pums_burden.router)
    app.include_router(pums_burden_county.router)
    app.include_router(pums_burden_county_series.router)
    app.include_router(counties.router)
    app.include_router(fec.router)
    app.include_router(fec_export.router)
    app.include_router(fec_metrics.router)
    app.include_router(fec_risk.router)

    # Static UI: assets at /static/*, housing index at /. We deliberately
    # do NOT mount StaticFiles at "/" with html=True because that would
    # shadow API routes; mounting at "/static" keeps the root path
    # reserved for the index file (and future server-rendered pages).
    #
    # The fraud UI lives under serving/static/fraud/ and is reached via
    # GET /fraud (a deliberate top-level route, not a redirect to a
    # nested path, so it can be deep-linked from anywhere).
    if _STATIC_DIR.is_dir():
        app.mount(
            "/static",
            StaticFiles(directory=str(_STATIC_DIR)),
            name="static",
        )

        @app.get("/", include_in_schema=False)
        def _index() -> FileResponse:
            return FileResponse(_STATIC_DIR / "index.html")

        @app.get("/fraud", include_in_schema=False)
        def _fraud_index() -> FileResponse:
            # Separate front-end for the FEC / civic-integrity surface.
            # Same FastAPI app + DB pool; different HTML/JS bundle.
            return FileResponse(_STATIC_DIR / "fraud" / "index.html")
    else:
        log.warning(
            "serving.app: static UI directory %s not found; / will 404. "
            "This is expected when running the API without the bundled "
            "front-end (e.g. in some test contexts).",
            _STATIC_DIR,
        )

    return app


# Loaded by uvicorn as `serving.app:app`.
app = create_app()
