"""``nj-serve`` CLI: run the FastAPI app under uvicorn.

Why a thin CLI rather than just calling uvicorn directly:
  * Single canonical entry point that pyproject.toml exposes.
  * Centralized place to add startup checks (e.g. ensure migrations
    are applied, refuse to start if PG_DSN is unset) without baking
    them into the app's lifespan.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import uvicorn


def main() -> int:
    """Parse args, validate env, run uvicorn. Returns the process exit code."""
    parser = argparse.ArgumentParser(prog="nj-serve",
                                     description="Run the NJ platform read API.")
    parser.add_argument("--host", default="0.0.0.0",  # noqa: S104
                        help=(
                            "Bind host (default: 0.0.0.0). Binding to all "
                            "interfaces is intended; production deployment "
                            "puts this behind a reverse proxy."
                        ))
    parser.add_argument("--port", type=int, default=8000,
                        help="Bind port (default: 8000).")
    parser.add_argument("--workers", type=int, default=1,
                        help="Uvicorn worker count (default: 1; raise for prod).")
    parser.add_argument("--log-level", default="info",
                        help="Uvicorn/python log level.")
    parser.add_argument("--reload", action="store_true",
                        help="Auto-reload on source changes (dev only).")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not os.environ.get("PG_DSN"):
        sys.stderr.write(
            "PG_DSN environment variable must be set before running nj-serve.\n"
        )
        return 2

    uvicorn.run(
        "serving.app:app",
        host=args.host,
        port=args.port,
        workers=args.workers,
        log_level=args.log_level,
        reload=args.reload,
    )
    return 0
