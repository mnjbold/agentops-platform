"""Date-window helpers for the analytics dashboard.

The frontend sends a ``preset`` (today, 7d, 30d, this-month, last-month,
custom) and optional ``from``/``to`` ISO date strings. The API normalises
that to a ``(day_from, day_to)`` pair of YYYY-MM-DD strings for SQL.

We always work in UTC. The dashboard will eventually show local time but
the analytics rollup table is bucketed by UTC days.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional, Tuple


def resolve_window(
    preset: Optional[str] = None,
    from_: Optional[str] = None,
    to: Optional[str] = None,
) -> Tuple[str, str]:
    """Return ``(day_from, day_to)`` as YYYY-MM-DD strings.

    ``preset`` is one of: ``today``, ``7d``, ``30d``, ``this-month``,
    ``last-month``. If ``preset == 'custom'`` (or is None + from/to given)
    we honour the explicit dates.
    """
    today = datetime.now(timezone.utc).date()

    if preset == "today":
        return today.isoformat(), today.isoformat()
    if preset == "7d":
        return (today - timedelta(days=6)).isoformat(), today.isoformat()
    if preset == "30d":
        return (today - timedelta(days=29)).isoformat(), today.isoformat()
    if preset == "this-month":
        first = today.replace(day=1)
        return first.isoformat(), today.isoformat()
    if preset == "last-month":
        first_this_month = today.replace(day=1)
        last_month_end = first_this_month - timedelta(days=1)
        first_last_month = last_month_end.replace(day=1)
        return first_last_month.isoformat(), last_month_end.isoformat()

    # Custom (or no preset): use from/to as-is, fall back to 7d default.
    if from_ and to:
        return _validate_day(from_), _validate_day(to)
    if from_:
        return _validate_day(from_), today.isoformat()
    if to:
        return (today - timedelta(days=6)).isoformat(), _validate_day(to)
    # Default to last 7 days
    return (today - timedelta(days=6)).isoformat(), today.isoformat()


def parse_range(
    from_: Optional[str] = None,
    to: Optional[str] = None,
    preset: Optional[str] = None,
) -> Tuple[str, str]:
    """Alias used by some screens; identical to :func:`resolve_window`."""
    return resolve_window(preset=preset, from_=from_, to=to)


def previous_window(day_from: str, day_to: str) -> Tuple[str, str]:
    """Return the window of the same length immediately preceding this one.

    Used by the "this period vs previous period" comparison toggle.
    """
    start = _validate_day(day_from)
    end = _validate_day(day_to)
    days = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)
    return prev_start.isoformat(), prev_end.isoformat()


def _validate_day(s: str) -> date:
    """Parse a YYYY-MM-DD string; raise ValueError otherwise."""
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except Exception as e:
        raise ValueError(f"invalid date: {s!r} (expected YYYY-MM-DD)") from e
