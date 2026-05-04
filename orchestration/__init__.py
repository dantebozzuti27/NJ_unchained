"""Dagster orchestration layer for the platform.

This package wraps the existing :mod:`ingestion` CLIs in Dagster's
software-defined-asset model. The asset graph is the BBG-terminal
freshness pane: each `raw.*` table is a first-class asset with
declared upstream sources, freshness policies, and quality checks.

Public surface
--------------
* ``orchestration.definitions`` -- the top-level Dagster ``Definitions``
  object that the webserver/daemon load via ``-m orchestration.definitions``.
* ``orchestration.assets``      -- the @asset functions, one per table.
* ``orchestration.schedules``   -- ScheduleDefinitions wrapping the
  release_calendar table.
* ``orchestration.sensors``     -- AssetSensors + freshness-policy
  violation sensors.
* ``orchestration.resources``   -- shared Postgres connection resource.

Why this is separate from `ingestion/`
--------------------------------------
The ingester functions are framework-agnostic CLIs. Dagster wraps them
without owning them, so:
  * Tests can run loaders without bringing up Dagster.
  * If we ever swap orchestrators (Prefect, Airflow, plain cron), only
    this package changes.
  * The orchestration package can be uninstalled without breaking the
    data layer.
"""

from __future__ import annotations
