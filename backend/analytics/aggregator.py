"""SQL aggregations for the analytics dashboard (issue #16).

Three jobs:

1. ``aggregate_for_tenant``     — total calls / sms / spend for a window
2. ``aggregate_for_assistants`` — per-assistant volume + transfer/fallback rate
3. ``backfill_rollup_for_day``  — refresh one day on the rollup table

The data lives in ``deliveries`` and ``assistant_call_log``. There is no
``calls`` table in v1 (it lives in the Appwrite cloud store) so we drive
everything from the deliveries append-only log: every Telnyx event the
hook handler saw ended up as a row there. ``deliveries.kind`` is freeform
but in practice we have ``outbound_call`` / ``inbound_call`` /
``outbound_sms`` / ``inbound_sms`` / ``sms`` / ``call``.

Sentiment is not available in v1 — we return ``None`` for the per-assistant
sentiment bucket so the UI can show "—" without crashing.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from webhooks.storage import Store

log = logging.getLogger(__name__)


# ─────────────────────────── helpers ────────────────────────────────────────

def _day(s: str) -> str:
    """Return the YYYY-MM-DD prefix of an ISO-8601 timestamp. Best-effort."""
    if not s:
        return ""
    if "T" in s:
        return s.split("T", 1)[0]
    return s[:10]


# ─────────────────────────── tenant-level rollup ────────────────────────────

def aggregate_for_tenant(
    store: Store,
    tenant_id: str,
    day_from: str,
    day_to: str,
) -> dict:
    """Return the tenant-level KPIs the overview card uses.

    Prefers the rollup table; falls back to a live COUNT on the deliveries
    table when the rollup is empty (e.g. just-upgraded DB, cron hasn't run).
    """
    rollup = store.get_rollup_window(tenant_id, day_from, day_to)
    if rollup:
        calls_in = sum(int(r.get("calls_in") or 0) for r in rollup)
        calls_out = sum(int(r.get("calls_out") or 0) for r in rollup)
        sms_in = sum(int(r.get("sms_in") or 0) for r in rollup)
        sms_out = sum(int(r.get("sms_out") or 0) for r in rollup)
        spend_cents = sum(int(r.get("spend_cents") or 0) for r in rollup)
        total_calls = calls_in + calls_out
        total_sms = sms_in + sms_out
    else:
        counts = store.count_deliveries_in_window(tenant_id, day_from, day_to)
        calls_in = counts["calls_in"]
        calls_out = counts["calls_out"]
        sms_in = counts["sms_in"]
        sms_out = counts["sms_out"]
        total_calls = counts["calls_total"]
        total_sms = counts["sms_total"]
        spend_cents = 0  # No rollup yet — no spend data either.

    # Busiest hours-of-day from the deliveries table (no rollup, cheap).
    by_hour = [0] * 24
    rows = store._rows(  # type: ignore[attr-defined]
        "SELECT created_at FROM deliveries "
        "WHERE tenant_id = ? AND substr(created_at, 1, 10) BETWEEN ? AND ? "
        "AND (kind LIKE '%call%' OR kind = 'call')",
        (tenant_id, day_from, day_to),
    )
    for r in rows:
        ts = r.get("created_at") or ""
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            by_hour[dt.hour] += 1
        except Exception:
            pass

    # Top assistant by call volume. Joins assistant_call_log against itself
    # (counting distinct call_id per assistant) to find a winner.
    top_rows = store._rows(  # type: ignore[attr-defined]
        "SELECT a.id AS assistant_id, a.name, "
        "       COUNT(DISTINCT l.call_id) AS calls "
        "FROM assistants a "
        "LEFT JOIN assistant_call_log l "
        "  ON l.tenant_id = a.tenant_id AND l.assistant_id = a.id "
        " AND substr(l.created_at, 1, 10) BETWEEN ? AND ? "
        "WHERE a.tenant_id = ? "
        "GROUP BY a.id, a.name "
        "ORDER BY calls DESC LIMIT 5",
        (day_from, day_to, tenant_id),
    )
    top_agents = [
        {
            "assistant_id": r.get("assistant_id"),
            "name": r.get("name"),
            "calls": int(r.get("calls") or 0),
        }
        for r in top_rows
    ]

    return {
        "from": day_from,
        "to": day_to,
        "total_calls": int(total_calls),
        "calls_in": int(calls_in),
        "calls_out": int(calls_out),
        "total_sms": int(total_sms),
        "sms_in": int(sms_in),
        "sms_out": int(sms_out),
        "spend_cents": int(spend_cents),
        "busiest_hours": [{"hour": h, "calls": c} for h, c in enumerate(by_hour)],
        "top_agents": top_agents,
    }


# ─────────────────────────── per-assistant rollup ───────────────────────────

def aggregate_for_assistants(
    store: Store,
    tenant_id: str,
    day_from: str,
    day_to: str,
) -> list[dict]:
    """Return one row per assistant with the dashboard's KPIs.

    Numbers are derived from the assistant_call_log + deliveries table.
    A "call" in this context is a unique call_id we saw a transcript row
    for in the window. Outcome distribution is approximated from the
    role column on assistant_call_log: any 'tool' row counts as a tool
    invocation; we also flag the last role per call to bucket outcomes.
    """
    rows = store._rows(  # type: ignore[attr-defined]
        "SELECT a.id AS assistant_id, a.name, "
        "       COUNT(DISTINCT l.call_id) AS call_count, "
        "       SUM(CASE WHEN l.role = 'tool' THEN 1 ELSE 0 END) AS tool_count, "
        "       SUM(CASE WHEN l.role = 'user'  THEN 1 ELSE 0 END) AS user_count, "
        "       SUM(CASE WHEN l.role = 'assistant' THEN 1 ELSE 0 END) AS assistant_count "
        "FROM assistants a "
        "LEFT JOIN assistant_call_log l "
        "  ON l.tenant_id = a.tenant_id AND l.assistant_id = a.id "
        " AND substr(l.created_at, 1, 10) BETWEEN ? AND ? "
        "WHERE a.tenant_id = ? "
        "GROUP BY a.id, a.name "
        "ORDER BY call_count DESC, a.name ASC",
        (day_from, day_to, tenant_id),
    )

    out: list[dict] = []
    for r in rows:
        call_count = int(r.get("call_count") or 0)
        tool_count = int(r.get("tool_count") or 0)
        # "Transfer rate" is approximated by counting tool calls where
        # tool_name == 'transfer' or 'connect_agent' in the window.
        # Falls back to a 0 when no tool log has been kept.
        transfer_count = store._row(  # type: ignore[attr-defined]
            "SELECT COUNT(*) AS c FROM assistant_call_log "
            "WHERE tenant_id = ? AND assistant_id = ? "
            "AND substr(created_at, 1, 10) BETWEEN ? AND ? "
            "AND role = 'tool' AND (tool_name IN ('transfer','connect_agent','handoff') "
            "                        OR tool_name LIKE 'transfer_%')",
            (tenant_id, r.get("assistant_id"), day_from, day_to),
        )
        transfers = int((transfer_count or {}).get("c") or 0)
        transfer_rate = (transfers / call_count) if call_count else 0.0
        fallback_rate = (tool_count / (call_count * 5)) if call_count else 0.0
        # Outcome distribution: count of distinct call_ids grouped by
        # whether their last assistant role line contains 'complete' or
        # 'transfer' in the content. Best-effort.
        outcome_rows = store._rows(  # type: ignore[attr-defined]
            "SELECT call_id, "
            "  (SELECT content FROM assistant_call_log l2 "
            "     WHERE l2.tenant_id = l.tenant_id "
            "       AND l2.assistant_id = l.assistant_id "
            "       AND l2.call_id = l.call_id "
            "       AND l2.role = 'assistant' "
            "     ORDER BY l2.created_at DESC LIMIT 1) AS last_assistant "
            "FROM assistant_call_log l "
            "WHERE l.tenant_id = ? AND l.assistant_id = ? "
            "AND substr(l.created_at, 1, 10) BETWEEN ? AND ? "
            "AND l.call_id IS NOT NULL "
            "GROUP BY l.call_id",
            (tenant_id, r.get("assistant_id"), day_from, day_to),
        )
        outcomes: dict[str, int] = {"completed": 0, "transferred": 0, "voicemail": 0, "abandoned": 0}
        for o in outcome_rows:
            content = (o.get("last_assistant") or "").lower()
            if not content:
                outcomes["abandoned"] += 1
            elif "transfer" in content or "connect" in content:
                outcomes["transferred"] += 1
            elif "voicemail" in content or "leave a message" in content:
                outcomes["voicemail"] += 1
            else:
                outcomes["completed"] += 1

        # Avg handle time: avg seconds of recording duration where available
        # (joined on call_id) — falls back to NULL when we have no data.
        dur_row = store._row(  # type: ignore[attr-defined]
            "SELECT AVG(r.duration) AS avg_dur "
            "FROM recordings r "
            "WHERE r.tenant_id = ? AND r.call_id IN ("
            "  SELECT DISTINCT call_id FROM assistant_call_log "
            "   WHERE tenant_id = ? AND assistant_id = ? "
            "     AND substr(created_at, 1, 10) BETWEEN ? AND ?"
            ")",
            (tenant_id, tenant_id, r.get("assistant_id"), day_from, day_to),
        )
        avg_handle_time = (dur_row or {}).get("avg_dur")
        if avg_handle_time is not None:
            try:
                avg_handle_time = float(avg_handle_time)
            except (TypeError, ValueError):
                avg_handle_time = None

        out.append({
            "assistant_id": r.get("assistant_id"),
            "name": r.get("name"),
            "call_count": call_count,
            "tool_count": tool_count,
            "transfer_count": transfers,
            "transfer_rate": round(transfer_rate, 4),
            "fallback_rate": round(min(1.0, fallback_rate), 4),
            "outcomes": outcomes,
            "avg_handle_time": avg_handle_time,
            "sentiment_avg": None,  # not available in v1
        })
    return out


# ─────────────────────────── rollup maintenance ────────────────────────────

def backfill_rollup_for_day(store: Store, tenant_id: str, day: str) -> None:
    """Recompute one tenant's rollup row for a single day.

    Idempotent — re-running for the same day just overwrites the row.
    Used by the cron job and by ``refresh_rollup`` on a per-tenant basis.
    """
    counts = store.count_deliveries_in_window(tenant_id, day, day)
    # Spend: 1 cent per outbound call, 1 cent per outbound SMS — placeholder
    # rate. Real billing is computed from the per-tenant rate in
    # ``billing.plans`` once usage metering is live.
    spend = (counts["calls_out"] + counts["sms_out"]) * 1
    store.upsert_rollup_day(
        tenant_id=tenant_id,
        day=day,
        calls_in=counts["calls_in"],
        calls_out=counts["calls_out"],
        sms_in=counts["sms_in"],
        sms_out=counts["sms_out"],
        spend_cents=spend,
    )


def refresh_rollup(store: Store, days: int = 1) -> int:
    """Recompute the last ``days`` days for every tenant.

    Returns the number of (tenant, day) rows written. Safe to call from
    a cron — it's idempotent and O(tenants * days).
    """
    today = datetime.now(timezone.utc).date()
    tenants = store.list_tenants()
    written = 0
    for t in tenants:
        for d in range(days):
            day = (today - timedelta(days=d)).isoformat()
            backfill_rollup_for_day(store, t["id"], day)
            written += 1
    return written
