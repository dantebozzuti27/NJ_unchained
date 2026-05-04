"""Serving layer: BBG-terminal-style read API.

This package is the consumer surface for the platform. It does NOT
write to the data DB; every endpoint is a read against existing
tables and views (raw.*, derived.*, governance.*, ref.*).

Public surface
--------------
* ``serving.app``      -- FastAPI :class:`fastapi.FastAPI` instance.
* ``serving.cli``      -- ``nj-serve`` entry point that runs uvicorn.
* ``serving.db``       -- shared psycopg connection pool.
* ``serving.models``   -- pydantic v2 response models.
* ``serving.queries``  -- SQL query helpers (one function per endpoint).
* ``serving.routes``   -- route definitions, one module per concern.

Design discipline
-----------------
1. **No business logic in routes.** Routes parse path/query params,
   call a query function, return a model. Computation lives in SQL or
   in derived.* views.

2. **Strict response models.** Every endpoint returns a typed
   ``BaseModel``. No raw dicts. The OpenAPI schema is the contract.

3. **Read-only.** This package never writes to the data DB. The DB
   connection is pooled and reused; transactions are short.

4. **Observability via headers.** Every response carries ``X-Request-Id``
   and ``X-Query-Time-Ms``. The latter is the single SQL call's
   wall-clock time, useful for spotting slow queries from the client.

5. **No auth in v0.** The API is intended to run inside the trusted
   docker-compose network (or behind a reverse proxy that handles
   auth). Adding auth is a v1 follow-up.
"""

from __future__ import annotations
