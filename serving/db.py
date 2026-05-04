"""Shared psycopg connection pool for the serving layer.

We use psycopg-pool's :class:`ConnectionPool` rather than constructing
a fresh connection per request because:

* Connection setup is ~5-30ms over local sockets, ~50-200ms over TLS.
  Per-request overhead is unacceptable for a read API.
* Postgres has a hard ceiling on concurrent connections (default 100).
  A pool with bounded `max_size` ensures we degrade gracefully under
  load instead of refusing connections.
* The pool's `check` callback transparently revalidates connections
  that have been idle, so we recover from transient network blips
  without surfacing them to clients.

Pool sizing rationale
---------------------
`min_size=2, max_size=10` is appropriate for a single-operator dev
environment. Production sizing should match `(workers * pool_size)
< postgres.max_connections / 2` (leave half the budget for ad-hoc
analysts + Dagster). At our workload (read-mostly, sub-100ms
queries), max_size=10 is generous.

DSN resolution
--------------
We read ``PG_DSN`` from the environment. There is intentionally NO
fallback to a default dev DSN; production config must be explicit.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import TYPE_CHECKING

from psycopg_pool import ConnectionPool

if TYPE_CHECKING:
    from collections.abc import Iterator

    import psycopg


log = logging.getLogger(__name__)


_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    """Return the process-wide connection pool, lazily constructing it."""
    global _pool
    if _pool is None:
        dsn = os.environ.get("PG_DSN")
        if not dsn:
            raise RuntimeError(
                "PG_DSN environment variable must be set for the serving layer."
            )
        log.info("Initializing Postgres connection pool")
        _pool = ConnectionPool(
            conninfo=dsn,
            min_size=2,
            max_size=10,
            timeout=10.0,
            max_idle=300.0,
            check=ConnectionPool.check_connection,
            kwargs={"application_name": "nj_serving_api"},
            open=True,  # Eagerly open the pool. Default flips to False
                        # in a future psycopg_pool release; pinning the
                        # behavior protects us from that change.
        )
        _pool.wait()
    return _pool


def close_pool() -> None:
    """Tear down the pool. Called from the FastAPI shutdown hook."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def borrow_connection() -> Iterator[psycopg.Connection]:
    """Yield a pooled connection. Auto-returned to the pool on exit."""
    pool = get_pool()
    with pool.connection() as conn:
        yield conn
