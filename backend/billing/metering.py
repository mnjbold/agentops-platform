"""Usage metering — increment voice minutes / SMS segments on webhook events.

The hook into the call/SMS lifecycle lives in
``webhooks.handlers.default.DefaultEventHandler``; this module is the
*function* the handler calls. Keeping it isolated makes it easy to unit
test and easy to swap for a different backend later.

The current period is the calendar month (UTC) — we round down to the
first of the month. Free plan doesn't track minutes (it's $0/min for
unlimited inbound), but we still record so the dashboard can show "you
used N minutes" even if the bill is $0.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from webhooks.storage import get_store

log = logging.getLogger(__name__)


def current_period(now: Optional[datetime] = None) -> tuple[str, str]:
    """Return (period_start_iso, period_end_iso) for the current calendar
    month in UTC. ``period_end`` is exclusive (start of next month)."""
    now = now or datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start.isoformat(), end.isoformat()


def record_voice_minutes(
    tenant_id: str,
    call_id: str,
    duration_seconds: int,
) -> dict:
    """Append one usage record for the call.

    ``duration_seconds`` is rounded up to the nearest minute to match
    how Telnyx bills (partial minutes count as full).
    """
    if duration_seconds <= 0:
        return {"skipped": "zero duration"}
    minutes = max(1, -(-int(duration_seconds) // 60))  # ceil div
    period_start, period_end = current_period()
    store = get_store()
    rec = store.record_usage(
        tenant_id=tenant_id,
        kind="voice_minutes",
        quantity=minutes,
        period_start=period_start,
        period_end=period_end,
    )
    log.info(
        "usage: tenant=%s call=%s minutes=%s period=%s..%s",
        tenant_id, call_id, minutes, period_start, period_end,
    )
    return rec


def record_sms_segment(
    tenant_id: str,
    message_id: str,
    segments: int = 1,
) -> dict:
    """Append one usage record for the SMS message.

    ``segments`` is the number of billable segments (Telnyx splits long
    messages at 160 chars for GSM or 70 for UCS-2).
    """
    segments = max(1, int(segments))
    period_start, period_end = current_period()
    store = get_store()
    rec = store.record_usage(
        tenant_id=tenant_id,
        kind="sms_segments",
        quantity=segments,
        period_start=period_start,
        period_end=period_end,
    )
    log.info(
        "usage: tenant=%s msg=%s segments=%s period=%s..%s",
        tenant_id, message_id, segments, period_start, period_end,
    )
    return rec


def usage_for_period(
    tenant_id: str,
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
) -> dict:
    """Return the totals for the given period (default: current month)."""
    if not period_start or not period_end:
        period_start, period_end = current_period()
    store = get_store()
    return {
        "period_start": period_start,
        "period_end": period_end,
        "voice_minutes": store.sum_usage_in_period(
            tenant_id, "voice_minutes", period_start, period_end
        ),
        "sms_segments": store.sum_usage_in_period(
            tenant_id, "sms_segments", period_start, period_end
        ),
    }
