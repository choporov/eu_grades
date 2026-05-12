from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


def today_in(timezone: ZoneInfo) -> date:
    return datetime.now(timezone).date()


def current_week_bounds(anchor: date) -> tuple[date, date]:
    start = anchor - timedelta(days=anchor.weekday())
    return start, start + timedelta(days=6)


def previous_week_bounds(anchor: date) -> tuple[date, date]:
    current_start, _ = current_week_bounds(anchor)
    previous_start = current_start - timedelta(days=7)
    return previous_start, previous_start + timedelta(days=6)


def date_range_filter(
    entries: list,
    start: date | None = None,
    end: date | None = None,
):
    if start is None and end is None:
        return entries
    return [
        entry
        for entry in entries
        if (start is None or entry.date >= start) and (end is None or entry.date <= end)
    ]

