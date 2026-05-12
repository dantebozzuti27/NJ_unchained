"""ScheduleDefinitions derived from ref.release_calendar.

Each schedule wraps a Dagster :class:`AssetSelection` and a cron
expression. Cron expressions are CONSTRUCTED at module-import time
from the structured columns of ``ref.release_calendar`` (the
single source of truth for cadence).

Why this is split from `assets.py`
----------------------------------
Schedules carry side-effects (cron firing) and are easier to reason
about when separated from pure asset definitions. Adding a new
schedule is a one-row INSERT to ref.release_calendar plus a one-line
addition to ALL_SCHEDULES below.

Cadence -> cron mapping
-----------------------
We support a deliberately small set of cadence shapes (see migration
030 for the rationale):

    weekly    -> "{minute} {hour} * * {dow}"                   (e.g. Thu 12:00)
    monthly   -> "{minute} {hour} {dom} * *"                   (e.g. day 10, 08:30)
    quarterly -> custom; runs once per quarter via month-mod-3 logic
    annual    -> "{minute} {hour} {dom} {moy} *"               (e.g. Jan 15)
    daily     -> "{minute} {hour} * * *"                       (e.g. 06:00)
    on_event  -> NO schedule emitted; AssetSensor handles firing.

Quarterly is the only cadence that does not map cleanly to a single
cron line, because cron has no "every 3 months starting in February"
expression. We emit four schedules (one per quarter month) all
pointing at the same asset; idempotent re-materialization makes this
safe.
"""

from __future__ import annotations

import logging

from dagster import AssetKey, AssetSelection, ScheduleDefinition

log = logging.getLogger(__name__)


# ============================================================================
# Schedule registry
# ============================================================================
#
# We start with explicit cron strings. A future enhancement is to
# generate these from a query against ref.release_calendar at module-
# import time (so adding a row to the calendar table immediately spawns
# a schedule). For now the explicit registry is more debuggable.
# ============================================================================


# FRED weekly: Thursdays at 12:30 ET (Freddie Mac PMMS publishes 12:00;
# wait 30 minutes for the file to land).
_FRED_SCHEDULE = ScheduleDefinition(
    name="fred_weekly",
    cron_schedule="30 12 * * 4",
    execution_timezone="America/New_York",
    target=AssetSelection.assets(AssetKey(["raw", "fred_observation"])),
    description="FRED rate panel: weekly Thursday refresh after the PMMS release.",
)


# BLS CPI monthly: try 09:00 ET on the 10th, 11th, 12th, 13th, 14th, 15th
# of each month. BLS varies the release date within this window. Since
# materialization is idempotent and the loader's UPSERT is cheap,
# polling daily within the window is the simplest correct behavior.
_BLS_CPI_SCHEDULE = ScheduleDefinition(
    name="bls_cpi_monthly_window",
    cron_schedule="0 9 10-15 * *",
    execution_timezone="America/New_York",
    target=AssetSelection.assets(AssetKey(["raw", "cpi_u"])),
    description=(
        "BLS CPI: poll daily 10:00-15:00 ET to catch the variable "
        "monthly release window."
    ),
)


# FHFA HPI quarterly: end-of-quarter-month at 10:00 ET. Cron has no
# "last Tuesday" expression; using last day of the publication month
# (Feb/May/Aug/Nov) is a defensible approximation that catches the
# release within 24-48h.
_FHFA_SCHEDULE = ScheduleDefinition(
    name="fhfa_hpi_quarterly",
    cron_schedule="0 10 28 2,5,8,11 *",
    execution_timezone="America/New_York",
    target=AssetSelection.assets(AssetKey(["raw", "fhfa_hpi_county"])),
    description=(
        "FHFA HPI: re-materialize on the 28th of the FHFA publication "
        "months (Feb/May/Aug/Nov)."
    ),
)


# ACS annual: ACS 5-year vintage Y is published mid-December of Y+1.
# Refresh daily during a 2-week window starting Dec 5; UPSERT means
# repeated polling is free, and we catch the publication date even
# if Census shifts it within that window.
_ACS_INCOME_SCHEDULE = ScheduleDefinition(
    name="acs_income_annual_window",
    cron_schedule="0 6 5-19 12 *",
    execution_timezone="America/New_York",
    target=AssetSelection.assets(
        AssetKey(["raw", "acs_median_household_income"]),
    ),
    description="ACS B19013: poll daily Dec 5-19 to catch the vintage drop.",
)


_ACS_HOUSING_SCHEDULE = ScheduleDefinition(
    name="acs_housing_annual_window",
    cron_schedule="0 6 5-19 12 *",
    execution_timezone="America/New_York",
    target=AssetSelection.assets(AssetKey(["raw", "acs_housing"])),
    description="ACS B25xxx: poll daily Dec 5-19 to catch the vintage drop.",
)


# DOL OFLC LCA quarterly: publication is ~3 months after fiscal quarter
# ends, but the exact date drifts. Run on the 15th of each month;
# UPSERT means we are idempotent against the publication-date surprise.
_LCA_SCHEDULE = ScheduleDefinition(
    name="lca_monthly_poll",
    cron_schedule="0 7 15 * *",
    execution_timezone="America/New_York",
    target=AssetSelection.assets(AssetKey(["raw", "lca_disclosure"])),
    description=(
        "DOL OFLC LCA: monthly poll on the 15th. The asset walks back "
        "LCA_BACKFILL_QUARTERS so it always catches whatever is newly "
        "published since the prior poll."
    ),
)


# NJ DCA property tax annual: publication is mid-January. Poll daily
# Jan 8-22 to catch the drop window.
_DCA_SCHEDULE = ScheduleDefinition(
    name="nj_dca_january_window",
    cron_schedule="0 6 8-22 1 *",
    execution_timezone="America/New_York",
    target=AssetSelection.assets(AssetKey(["raw", "nj_property_tax_county"])),
    description=(
        "NJ DCA County Tax Summary: poll daily Jan 8-22 to catch the "
        "annual publication."
    ),
)


# ACS PUMS annual: 1-year PUMS for survey year Y is published in mid-
# October of Y+1, ~6-8 weeks AFTER the tabular ACS releases. Poll
# daily Oct 1-31 (a long window because Census's PUMS publication date
# drifts by 2-3 weeks from year to year). The asset walks back
# PUMS_BACKFILL_YEARS so it always catches whatever vintage just
# dropped since the last successful run.
_PUMS_SCHEDULE = ScheduleDefinition(
    name="acs_pums_annual_window",
    cron_schedule="0 6 1-31 10 *",
    execution_timezone="America/New_York",
    target=AssetSelection.assets(
        AssetKey(["raw", "acs_pums_person"]),
        AssetKey(["raw", "acs_pums_housing"]),
    ),
    description=(
        "ACS PUMS: poll daily during October to catch the 1-year "
        "vintage drop. Materializes person + housing as a paired drop "
        "(housing reads from raw.acs_pums_housing after person's COPY)."
    ),
)


# Derived assets are NO LONGER on a cron. They use
# AutomationCondition.eager() (see orchestration/assets.py
# DERIVED_AUTOMATION) which fires whenever an upstream parent
# materializes -- ~30s latency vs the prior 6h polled cron. This
# requires the dagster-daemon container to be running; without the
# daemon, the conditions are inert.


# Pillar 2 fraud-signal master refresher: bi-weekly cadence.
#
# Cron: every other Sunday at 04:00 ET. We use "1,15" (the 1st and
# 15th of every month) as a practical 14-day approximation -- cron
# has no native "every 14 days" expression, and using "1,15" is the
# canonical pattern across the team's other bi-weekly jobs. Sunday at
# 04:00 ET is chosen because FEC's bulk-file infrastructure is
# typically quiet then (the FEC's own publish cycle is mid-week) so a
# raw.fec_* re-pull triggered upstream of this asset is least likely
# to collide with the source's own ingest.
#
# WHY THE SCHEDULE EXISTS (vs relying on DERIVED_AUTOMATION):
# DERIVED_AUTOMATION fires when raw.fec_* re-materializes, which
# happens only on the (manual) FEC bulk-loader cadence -- there is no
# upstream automation_condition that periodically re-pulls FEC bulk
# files. Without this cron, derived.fraud_signal_observation would
# stay frozen at the last manual FEC load. The schedule forces a
# materialization every 14 days; the asset's compute will re-run the
# master refresher against whatever FEC substrate is current. Doing
# this on a regular cadence is also what lets the
# governance.fraud_signal_baseline table accumulate samples (mig 097);
# without the schedule, the 2sigma drift detector would never have enough
# history to trigger.
_FRAUD_SIGNAL_OBSERVATION_SCHEDULE = ScheduleDefinition(
    name="fraud_signal_observation_biweekly",
    cron_schedule="0 4 1,15 * *",
    execution_timezone="America/New_York",
    target=AssetSelection.assets(
        AssetKey(["derived", "fraud_signal_observation"]),
    ),
    description=(
        "Pillar 2 fraud-signal master refresher: every other Sunday "
        "(1st and 15th of each month) at 04:00 ET. Re-runs "
        "derived.refresh_all_fraud_signal_observations against the "
        "current FEC + cross-source substrate; the asset compute also "
        "calls governance.capture_fraud_signal_baseline so the bi-"
        "weekly cadence builds up samples for the per_signal_"
        "distribution_drift_within_2sigma asset check (mig 097)."
    ),
)


ALL_SCHEDULES = [
    _FRED_SCHEDULE,
    _BLS_CPI_SCHEDULE,
    _FHFA_SCHEDULE,
    _ACS_INCOME_SCHEDULE,
    _ACS_HOUSING_SCHEDULE,
    _LCA_SCHEDULE,
    _DCA_SCHEDULE,
    _PUMS_SCHEDULE,
    _FRAUD_SIGNAL_OBSERVATION_SCHEDULE,
]
