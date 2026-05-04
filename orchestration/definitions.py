"""Top-level Dagster Definitions object.

This is the entrypoint the Dagster webserver and daemon load via
``-m orchestration.definitions``. It registers:

* All software-defined assets (orchestration/assets.py).
* All schedules (orchestration/schedules.py).
* All sensors (orchestration/sensors.py).
* Shared resources (Postgres connection, governance writer).

To add a new asset/schedule/sensor: define it in the appropriate
module, append to that module's ALL_* list, and the registration
flows through automatically.
"""

from __future__ import annotations

from dagster import Definitions

from orchestration.asset_checks import ALL_ASSET_CHECKS
from orchestration.assets import ALL_ASSETS
from orchestration.resources import GovernanceWriter, PgResource
from orchestration.schedules import ALL_SCHEDULES
from orchestration.sensors import ALL_SENSORS

# Resources are constructed lazily from environment variables so this
# module imports cleanly even when PG_DSN is not set (e.g. during
# `make typecheck`).
_pg = PgResource(dsn="${PG_DSN}")
_governance = GovernanceWriter(pg=_pg)


defs = Definitions(
    assets=ALL_ASSETS,
    asset_checks=ALL_ASSET_CHECKS,
    schedules=ALL_SCHEDULES,
    sensors=ALL_SENSORS,
    resources={
        "pg":         PgResource(dsn="${PG_DSN}"),
        "governance": GovernanceWriter(pg=PgResource(dsn="${PG_DSN}")),
    },
)
