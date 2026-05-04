"""Shared Dagster resources: Postgres connection + governance writer.

We expose two resources:

* :class:`PgResource` -- a thin context manager around ``psycopg.connect``
  that all assets use to talk to the data DB. Centralized so we have
  one place to add pooling, instrumentation, or read-replica routing.

* :class:`GovernanceWriter` -- writes rows to ``governance.dataset_health``
  for freshness violations, quality-check failures, and any other
  observability signal. Centralizing the schema discipline (severity,
  signal_name, details payload shape) here keeps it consistent across
  callers.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

import psycopg
from dagster import ConfigurableResource

if TYPE_CHECKING:
    from collections.abc import Iterator


# ============================================================================
# Postgres connection resource
# ============================================================================


class PgResource(ConfigurableResource):
    """Wraps a Postgres DSN. All assets receive this as a dependency."""

    dsn: str

    @classmethod
    def from_env(cls) -> PgResource:
        """Build from PG_DSN env var (the canonical platform setting)."""
        dsn = os.environ.get("PG_DSN")
        if not dsn:
            raise RuntimeError(
                "PG_DSN env var must be set for the orchestration layer."
            )
        return cls(dsn=dsn)

    @contextmanager
    def connect(self) -> Iterator[psycopg.Connection]:
        """Yield a psycopg connection; commit on success, rollback on error."""
        conn = psycopg.connect(self.dsn)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# ============================================================================
# Governance writer
# ============================================================================


Severity = Literal["info", "warn", "error"]


_VALID_SEVERITIES: Final[frozenset[str]] = frozenset({"info", "warn", "error"})


@dataclass(frozen=True)
class HealthSignal:
    """One emission to ``governance.dataset_health``."""

    dataset_id: str
    signal_name: str
    severity: Severity
    details: dict[str, Any]


class GovernanceWriter(ConfigurableResource):
    """Writes structured health signals to ``governance.dataset_health``.

    Use this from inside @asset functions / sensors instead of writing
    directly to the table. Centralizing the contract here prevents
    schema drift between callers (e.g. one caller emits severity='warn',
    another emits severity='WARNING'; one logs ``{"reason": "..."}``,
    another logs ``{"msg": "..."}``).
    """

    pg: PgResource

    def emit(self, signal: HealthSignal) -> None:
        """Insert a single health signal. Caller-managed transaction OK."""
        if signal.severity not in _VALID_SEVERITIES:
            raise ValueError(
                f"Invalid severity {signal.severity!r}; "
                f"expected one of {sorted(_VALID_SEVERITIES)}"
            )
        with self.pg.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO governance.dataset_health "
                "(dataset_id, signal_name, severity, details) "
                "VALUES (%s, %s, %s, %s::jsonb)",
                (
                    signal.dataset_id,
                    signal.signal_name,
                    signal.severity,
                    json.dumps(signal.details),
                ),
            )
