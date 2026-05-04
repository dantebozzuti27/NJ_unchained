"""Unit tests for :mod:`serving.release_schedule` (no Postgres required)."""

from __future__ import annotations

import datetime as dt

from serving.release_schedule import UTC, compute_release_calendar_row


def test_weekly_fred_next_thursday() -> None:
    # 2026-05-01 is a Friday; next Thursday is 2026-05-07.
    now = dt.datetime(2026, 5, 1, 18, 0, tzinfo=UTC)
    row = {
        "source_id": "raw.fred_observation",
        "cadence": "weekly",
        "day_of_week": 4,
        "timezone": "America/New_York",
        "time_of_day_local": dt.time(12, 0, 0),
    }
    upcoming, next_at, ok = compute_release_calendar_row(
        row, now_utc=now, horizon_days=14,
    )
    assert ok is True
    assert next_at is not None
    assert next_at.weekday() == 3  # Thursday UTC-local translation varies; check date in NY
    # May 7 12:00 ET == May 7 16:00 UTC during EDT
    assert upcoming and upcoming[0].date() == dt.date(2026, 5, 7)
    assert all(u > now for u in upcoming)


def test_fhfa_quarterly_may_window() -> None:
    now = dt.datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    row = {
        "source_id": "raw.fhfa_hpi_county",
        "cadence": "quarterly",
        "timezone": "America/New_York",
        "time_of_day_local": dt.time(9, 0, 0),
    }
    upcoming, next_at, ok = compute_release_calendar_row(
        row, now_utc=now, horizon_days=30,
    )
    assert ok is True
    assert next_at is not None
    # Next quarterly date after May 1 2026 is May 28, 2026
    assert next_at.date() == dt.date(2026, 5, 28)
    assert any(u.date() == dt.date(2026, 5, 28) for u in upcoming)


def test_cpi_monthly_emits_bls_window() -> None:
    now = dt.datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    row = {
        "source_id": "raw.cpi_u",
        "cadence": "monthly",
        "day_of_month": 10,
        "timezone": "America/New_York",
        "time_of_day_local": dt.time(8, 30, 0),
    }
    upcoming, next_at, ok = compute_release_calendar_row(
        row, now_utc=now, horizon_days=20,
    )
    assert ok is True
    may_days = {u.day for u in upcoming if u.month == 5 and u.year == 2026}
    assert may_days == {10, 11, 12, 13, 14, 15}
    assert next_at is not None
    assert next_at.day == 10


def test_on_event_unscheduled() -> None:
    row = {
        "source_id": "ref.zip_county",
        "cadence": "on_event",
        "timezone": "America/New_York",
    }
    upcoming, next_at, ok = compute_release_calendar_row(
        row, now_utc=dt.datetime(2026, 5, 1, tzinfo=UTC), horizon_days=14,
    )
    assert ok is False
    assert upcoming == []
    assert next_at is None


def test_fec_monthly_no_day_not_computed() -> None:
    row = {
        "source_id": "raw.fec",
        "cadence": "monthly",
        "day_of_month": None,
        "timezone": "America/New_York",
        "time_of_day_local": dt.time(0, 0, 0),
    }
    upcoming, next_at, ok = compute_release_calendar_row(
        row, now_utc=dt.datetime(2026, 5, 1, tzinfo=UTC), horizon_days=30,
    )
    assert ok is False
    assert upcoming == [] and next_at is None
