r"""Compute upcoming release timestamps from ``ref.release_calendar`` rows.

The SQL table stores cadence + structured fields (day-of-week, day-of-month,
month-of-year, local time, IANA timezone). This module turns those fields
into UTC :class:`~datetime.datetime` values for the ``/release-calendar``
endpoint (Bloomberg ECO <GO> style: "what is coming up, what is overdue").

Assumptions (documented here because the seed rows are not enough for full
cron expressiveness):

* **weekly** - ``day_of_week`` matches ISO-8601 (1=Monday .. 7=Sunday),
  consistent with :meth:`datetime.date.isoweekday`.
* **monthly** - ``day_of_month`` anchors a single day unless the source is
  ``raw.cpi_u``, where BLS publishes on variable days; we emit the 10th-15th
  of each relevant month to mirror the Dagster polling window.
* **quarterly** - ``raw.fhfa_hpi_county`` uses Feb/May/Aug/Nov on the 28th at
  the seeded local time (matches ``orchestration/schedules.py`` approximating
  FHFA's quarter-end-month publication). Other quarterly rows without enough
  structure return no computed times.
* **annual** - ``month_of_year`` + ``day_of_month`` + ``time_of_day_local``.
* **daily** - if ``time_of_day_local`` is set, every calendar day at that time;
  otherwise unscheduled.
* **on_event** - no computed schedule.

When computation is not possible, ``schedule_computed`` is False and both
``next_expected_at`` and ``upcoming_releases`` are empty / null.
"""

from __future__ import annotations

import calendar
import datetime as dt
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from collections.abc import Iterator


UTC = ZoneInfo("UTC")

# FHFA publication months (Feb/May/Aug/Nov), aligned with cron in schedules.py.
_FHFA_MONTHS = (2, 5, 8, 11)
_FHFA_DAY = 28

# BLS CPI polling window (dagster uses 10-15).
_CPI_POLL_DOM_RANGE = range(10, 16)


def _tz(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def _as_time(val: Any) -> dt.time | None:
    """Normalize Postgres TIME / timedelta / None to datetime.time."""
    if val is None:
        return None
    if isinstance(val, dt.time):
        return val
    if isinstance(val, dt.timedelta):
        secs = int(val.total_seconds()) % 86400
        return dt.time(hour=secs // 3600, minute=(secs % 3600) // 60, second=secs % 60)
    raise TypeError(f"unexpected time type: {type(val)!r}")


def _local_to_utc(local: dt.datetime) -> dt.datetime:
    if local.tzinfo is None:
        raise ValueError("local datetime must be timezone-aware")
    return local.astimezone(UTC)


def _combine_local(
    d: dt.date,
    tod: dt.time | None,
    tz: ZoneInfo,
) -> dt.datetime:
    t = tod or dt.time(0, 0, 0)
    return dt.datetime.combine(d, t, tzinfo=tz)


def _month_days(year: int, month: int, dom: int) -> int:
    """Clamp day-of-month to last valid day in month."""
    _, last = calendar.monthrange(year, month)
    return min(dom, last)


def _iter_weekly(
    *,
    now_utc: dt.datetime,
    horizon_end_utc: dt.datetime,
    iso_dow: int,
    tod: dt.time | None,
    tz_name: str,
) -> Iterator[dt.datetime]:
    tz = _tz(tz_name)
    now_local = now_utc.astimezone(tz)
    end_local = horizon_end_utc.astimezone(tz)
    d = now_local.date()
    # Scan day-by-day across the horizon (cheap for 14-400 days).
    while True:
        if d.isoweekday() == iso_dow:
            cand_local = _combine_local(d, tod, tz)
            if cand_local > now_local and cand_local <= end_local:
                yield _local_to_utc(cand_local)
        if d > end_local.date():
            break
        d += dt.timedelta(days=1)


def _first_weekly_after(
    *,
    now_utc: dt.datetime,
    iso_dow: int,
    tod: dt.time | None,
    tz_name: str,
) -> dt.datetime | None:
    tz = _tz(tz_name)
    now_local = now_utc.astimezone(tz)
    for i in range(400):
        d = now_local.date() + dt.timedelta(days=i)
        if d.isoweekday() != iso_dow:
            continue
        cand_local = _combine_local(d, tod, tz)
        if cand_local > now_local:
            return _local_to_utc(cand_local)
    return None


def _iter_monthly_single_dom(
    *,
    now_utc: dt.datetime,
    horizon_end_utc: dt.datetime,
    dom: int,
    tod: dt.time | None,
    tz_name: str,
) -> Iterator[dt.datetime]:
    tz = _tz(tz_name)
    now_local = now_utc.astimezone(tz)
    end_local = horizon_end_utc.astimezone(tz)
    y, m = now_local.year, now_local.month
    for _ in range(48):  # up to 4 years forward
        day = _month_days(y, m, dom)
        cand_local = _combine_local(dt.date(y, m, day), tod, tz)
        if cand_local > now_local and cand_local <= end_local:
            yield _local_to_utc(cand_local)
        if m == 12:
            m = 1
            y += 1
        else:
            m += 1


def _first_monthly_single_dom_after(
    *,
    now_utc: dt.datetime,
    dom: int,
    tod: dt.time | None,
    tz_name: str,
) -> dt.datetime | None:
    tz = _tz(tz_name)
    now_local = now_utc.astimezone(tz)
    y, m = now_local.year, now_local.month
    for _ in range(48):
        day = _month_days(y, m, dom)
        cand_local = _combine_local(dt.date(y, m, day), tod, tz)
        if cand_local > now_local:
            return _local_to_utc(cand_local)
        if m == 12:
            m = 1
            y += 1
        else:
            m += 1
    return None


def _iter_cpi_monthly_window(
    *,
    now_utc: dt.datetime,
    horizon_end_utc: dt.datetime,
    tod: dt.time | None,
    tz_name: str,
) -> Iterator[dt.datetime]:
    """Emit 10th-15th of each month inside the horizon (BLS CPI window)."""
    tz = _tz(tz_name)
    now_local = now_utc.astimezone(tz)
    end_local = horizon_end_utc.astimezone(tz)
    y, m = now_local.year, now_local.month
    for _ in range(36):
        for dom in _CPI_POLL_DOM_RANGE:
            day = _month_days(y, m, dom)
            cand_local = _combine_local(dt.date(y, m, day), tod, tz)
            if cand_local > now_local and cand_local <= end_local:
                yield _local_to_utc(cand_local)
        if m == 12:
            m = 1
            y += 1
        else:
            m += 1


def _first_cpi_after(
    *,
    now_utc: dt.datetime,
    tod: dt.time | None,
    tz_name: str,
) -> dt.datetime | None:
    tz = _tz(tz_name)
    now_local = now_utc.astimezone(tz)
    y, m = now_local.year, now_local.month
    for _ in range(48):
        for dom in _CPI_POLL_DOM_RANGE:
            day = _month_days(y, m, dom)
            cand_local = _combine_local(dt.date(y, m, day), tod, tz)
            if cand_local > now_local:
                return _local_to_utc(cand_local)
        if m == 12:
            m = 1
            y += 1
        else:
            m += 1
    return None


def _iter_fhfa_quarterly(
    *,
    now_utc: dt.datetime,
    horizon_end_utc: dt.datetime,
    tod: dt.time | None,
    tz_name: str,
) -> Iterator[dt.datetime]:
    tz = _tz(tz_name)
    now_local = now_utc.astimezone(tz)
    end_local = horizon_end_utc.astimezone(tz)
    y = now_local.year
    for _ in range(6):  # years
        for mo in _FHFA_MONTHS:
            day = _month_days(y, mo, _FHFA_DAY)
            cand_local = _combine_local(dt.date(y, mo, day), tod, tz)
            if cand_local > now_local and cand_local <= end_local:
                yield _local_to_utc(cand_local)
        y += 1


def _first_fhfa_after(
    *,
    now_utc: dt.datetime,
    tod: dt.time | None,
    tz_name: str,
) -> dt.datetime | None:
    tz = _tz(tz_name)
    now_local = now_utc.astimezone(tz)
    y = now_local.year
    for _ in range(6):
        for mo in _FHFA_MONTHS:
            day = _month_days(y, mo, _FHFA_DAY)
            cand_local = _combine_local(dt.date(y, mo, day), tod, tz)
            if cand_local > now_local:
                return _local_to_utc(cand_local)
        y += 1
    return None


def _iter_annual(
    *,
    now_utc: dt.datetime,
    horizon_end_utc: dt.datetime,
    month: int,
    dom: int,
    tod: dt.time | None,
    tz_name: str,
) -> Iterator[dt.datetime]:
    tz = _tz(tz_name)
    now_local = now_utc.astimezone(tz)
    end_local = horizon_end_utc.astimezone(tz)
    y = now_local.year
    for _ in range(20):
        day = _month_days(y, month, dom)
        cand_local = _combine_local(dt.date(y, month, day), tod, tz)
        if cand_local > now_local and cand_local <= end_local:
            yield _local_to_utc(cand_local)
        y += 1


def _first_annual_after(
    *,
    now_utc: dt.datetime,
    month: int,
    dom: int,
    tod: dt.time | None,
    tz_name: str,
) -> dt.datetime | None:
    tz = _tz(tz_name)
    now_local = now_utc.astimezone(tz)
    y = now_local.year
    for _ in range(20):
        day = _month_days(y, month, dom)
        cand_local = _combine_local(dt.date(y, month, day), tod, tz)
        if cand_local > now_local:
            return _local_to_utc(cand_local)
        y += 1
    return None


def _iter_daily(
    *,
    now_utc: dt.datetime,
    horizon_end_utc: dt.datetime,
    tod: dt.time | None,
    tz_name: str,
) -> Iterator[dt.datetime]:
    if tod is None:
        return
    tz = _tz(tz_name)
    now_local = now_utc.astimezone(tz)
    end_local = horizon_end_utc.astimezone(tz)
    d = now_local.date()
    while True:
        cand_local = _combine_local(d, tod, tz)
        if cand_local > now_local and cand_local <= end_local:
            yield _local_to_utc(cand_local)
        if d > end_local.date():
            break
        d += dt.timedelta(days=1)


def _first_daily_after(
    *,
    now_utc: dt.datetime,
    tod: dt.time | None,
    tz_name: str,
) -> dt.datetime | None:
    if tod is None:
        return None
    tz = _tz(tz_name)
    now_local = now_utc.astimezone(tz)
    for i in range(400):
        d = now_local.date() + dt.timedelta(days=i)
        cand_local = _combine_local(d, tod, tz)
        if cand_local > now_local:
            return _local_to_utc(cand_local)
    return None


def compute_release_calendar_row(
    row: dict[str, Any],
    *,
    now_utc: dt.datetime | None = None,
    horizon_days: int = 14,
) -> tuple[list[dt.datetime], dt.datetime | None, bool]:
    """Return (upcoming_in_horizon_utc, next_expected_utc, schedule_computed).

    ``next_expected_utc`` is the first scheduled instant strictly after *now*
    (even if it falls outside the horizon).
    """
    if now_utc is None:
        now_utc = dt.datetime.now(UTC)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=UTC)

    horizon_end = now_utc + dt.timedelta(days=max(0, horizon_days))
    cadence = row["cadence"]
    tz_name = row.get("timezone") or "America/New_York"
    tod = _as_time(row.get("time_of_day_local"))
    source_id = row["source_id"]

    upcoming: list[dt.datetime] = []
    next_single: dt.datetime | None = None

    if cadence == "on_event":
        return [], None, False

    if cadence == "weekly":
        dow = row.get("day_of_week")
        if dow is None or not isinstance(dow, int):
            return [], None, False
        upcoming = sorted(
            _iter_weekly(
                now_utc=now_utc,
                horizon_end_utc=horizon_end,
                iso_dow=dow,
                tod=tod,
                tz_name=tz_name,
            ),
        )
        next_single = _first_weekly_after(
            now_utc=now_utc, iso_dow=dow, tod=tod, tz_name=tz_name,
        )
        return upcoming, next_single, True

    if cadence == "monthly":
        dom = row.get("day_of_month")
        if source_id == "raw.cpi_u":
            upcoming = sorted(
                _iter_cpi_monthly_window(
                    now_utc=now_utc,
                    horizon_end_utc=horizon_end,
                    tod=tod,
                    tz_name=tz_name,
                ),
            )
            next_single = _first_cpi_after(
                now_utc=now_utc, tod=tod, tz_name=tz_name,
            )
            return upcoming, next_single, True
        if dom is None or not isinstance(dom, int):
            return [], None, False
        upcoming = sorted(
            _iter_monthly_single_dom(
                now_utc=now_utc,
                horizon_end_utc=horizon_end,
                dom=dom,
                tod=tod,
                tz_name=tz_name,
            ),
        )
        next_single = _first_monthly_single_dom_after(
            now_utc=now_utc, dom=dom, tod=tod, tz_name=tz_name,
        )
        return upcoming, next_single, True

    if cadence == "quarterly":
        if source_id == "raw.fhfa_hpi_county":
            upcoming = sorted(
                _iter_fhfa_quarterly(
                    now_utc=now_utc,
                    horizon_end_utc=horizon_end,
                    tod=tod,
                    tz_name=tz_name,
                ),
            )
            next_single = _first_fhfa_after(
                now_utc=now_utc, tod=tod, tz_name=tz_name,
            )
            return upcoming, next_single, True
        return [], None, False

    if cadence == "annual":
        moy = row.get("month_of_year")
        dom = row.get("day_of_month")
        if moy is None or dom is None:
            return [], None, False
        if not isinstance(moy, int) or not isinstance(dom, int):
            return [], None, False
        upcoming = sorted(
            _iter_annual(
                now_utc=now_utc,
                horizon_end_utc=horizon_end,
                month=moy,
                dom=dom,
                tod=tod,
                tz_name=tz_name,
            ),
        )
        next_single = _first_annual_after(
            now_utc=now_utc, month=moy, dom=dom, tod=tod, tz_name=tz_name,
        )
        return upcoming, next_single, True

    if cadence == "daily":
        it = list(
            _iter_daily(
                now_utc=now_utc,
                horizon_end_utc=horizon_end,
                tod=tod,
                tz_name=tz_name,
            ),
        )
        upcoming = sorted(it)
        next_single = _first_daily_after(
            now_utc=now_utc, tod=tod, tz_name=tz_name,
        )
        return upcoming, next_single, tod is not None

    return [], None, False
