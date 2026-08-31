"""Analytics package — aggregation primitives for the dashboard.

The package is split into:
- ``aggregator``  — pure SQL rollups over ``deliveries`` + ``assistant_call_log``
- ``windows``     — date-range parsing helpers (today, 7d, this-month, ...)

The FastAPI router lives in ``webhooks.analytics`` because the rest of the
HTTP surface mounts there. The aggregation is a plain module so it can
also be called by the nightly cron / a future background worker.
"""
from __future__ import annotations

from analytics.aggregator import (  # noqa: F401
    aggregate_for_assistants,
    aggregate_for_tenant,
    backfill_rollup_for_day,
    refresh_rollup,
)
from analytics.windows import (  # noqa: F401
    parse_range,
    resolve_window,
)
