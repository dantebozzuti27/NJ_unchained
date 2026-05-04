"""Sensors: react to events that schedules cannot express.

This module currently defines two sensors:

* :func:`freshness_violation_sensor` -- evaluates each asset's
  FreshnessPolicy on a 1-hour cadence; emits a 'warn' health signal
  for every asset that has fallen behind its lag budget. The Dagster
  UI also surfaces these visually; we duplicate to
  governance.dataset_health so non-Dagster consumers (e.g. a Slack
  digest job) can see them too.

* :func:`hud_staging_sensor` -- placeholder for the operator-staged
  HUD ZIP-County loader: watches data/manual/hud_zip_county/ for new
  files and triggers the HUD ingester on detection. Not yet wired
  up (deferred to a follow-up commit; the asset itself is not
  defined yet).

Why sensors live here, not in assets.py
---------------------------------------
Sensors are time-driven (Dagster daemon polls them on an interval),
asset @decorators are content-defined. Keeping them apart makes the
trigger surface easy to audit ("what makes things run?") without
wading through asset compute logic.
"""

import logging

from dagster import (
    DefaultSensorStatus,
    SensorEvaluationContext,
    SensorResult,
    SkipReason,
    sensor,
)

from orchestration.assets import ALL_ASSETS
from orchestration.resources import GovernanceWriter, HealthSignal, PgResource

log = logging.getLogger(__name__)


# ============================================================================
# Freshness violation sensor
# ============================================================================


@sensor(
    name="freshness_violation_sensor",
    description=(
        "Hourly: every asset whose FreshnessPolicy lag budget has been "
        "exceeded gets a 'warn' row in governance.dataset_health."
    ),
    minimum_interval_seconds=60 * 60,  # 1 hour
    default_status=DefaultSensorStatus.RUNNING,
)
def freshness_violation_sensor(
    context: SensorEvaluationContext,
    pg: PgResource,
    governance: GovernanceWriter,
) -> SensorResult | SkipReason:
    """Emit a warn signal for each asset that breached its freshness policy.

    We do not trigger re-materialization here (the schedule handles that).
    This sensor is purely observability: it ensures freshness violations
    land in our governance table even if the operator never opens the
    Dagster UI.
    """
    instance = context.instance
    n_violations = 0

    for asset_def in ALL_ASSETS:
        for asset_key in asset_def.keys:
            spec = asset_def.specs_by_key.get(asset_key)
            if spec is None or spec.freshness_policy is None:
                continue
            policy = spec.freshness_policy
            # Dagster's freshness compute is internal API; do a simple
            # observational check: is the most recent materialization
            # older than the policy's lag budget?
            record = instance.get_latest_materialization_event(asset_key)
            if record is None or record.timestamp is None:
                # Never materialized: also a freshness concern.
                governance.emit(HealthSignal(
                    dataset_id="/".join(asset_key.path),
                    signal_name="freshness_violation",
                    severity="warn",
                    details={"reason": "never_materialized"},
                ))
                n_violations += 1
                continue

            import datetime as dt
            now = dt.datetime.now(dt.UTC)
            last_mat = dt.datetime.fromtimestamp(record.timestamp, tz=dt.UTC)
            age_minutes = (now - last_mat).total_seconds() / 60.0

            # TimeWindowFreshnessPolicy has fail_window (timedelta);
            # legacy policies have maximum_lag_minutes. Cover both shapes
            # without binding to internal attributes.
            fail_minutes: float | None
            if hasattr(policy, "fail_window"):
                fail_minutes = policy.fail_window.total_seconds() / 60.0
            elif hasattr(policy, "maximum_lag_minutes"):
                fail_minutes = float(policy.maximum_lag_minutes or 0)
            else:
                fail_minutes = None

            if fail_minutes is not None and age_minutes > fail_minutes:
                governance.emit(HealthSignal(
                    dataset_id="/".join(asset_key.path),
                    signal_name="freshness_violation",
                    severity="warn",
                    details={
                        "age_minutes":          int(age_minutes),
                        "lag_budget_minutes":   int(fail_minutes),
                        "last_materialized_at": last_mat.isoformat(),
                    },
                ))
                n_violations += 1

    if n_violations == 0:
        return SkipReason("All assets within freshness budget.")
    return SensorResult(skip_reason=SkipReason(
        f"{n_violations} freshness violations recorded."
    ))


ALL_SENSORS = [
    freshness_violation_sensor,
]
