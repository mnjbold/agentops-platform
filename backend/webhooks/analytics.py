"""Analytics REST API (issue #16).

Endpoints
---------
GET  /v1/analytics/overview?from=...&to=...&preset=...
GET  /v1/analytics/assistants?from=...&to=...&preset=...
GET  /v1/analytics/assistants/{id}/calls?from=...&to=...&limit=50
GET  /v1/analytics/export.csv?from=...&to=...&preset=...

Auth: same TenantContext as the rest of /api/* — the middleware populates
``request.state.tenant_id`` from X-Api-Key or JWT.
"""
from __future__ import annotations

import csv
import io
import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from analytics.aggregator import aggregate_for_assistants, aggregate_for_tenant
from analytics.windows import previous_window, resolve_window
from webhooks.storage import get_store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/analytics", tags=["Analytics"])


def _tenant_id(request: Request) -> str:
    tid = getattr(request.state, "tenant_id", None)
    if not tid:
        raise HTTPException(401, "authentication required")
    return tid


@router.get("/overview")
def overview(
    request: Request,
    from_: Optional[str] = None,
    to: Optional[str] = None,
    preset: Optional[str] = None,
    compare: int = 0,
) -> dict:
    """Tenant-level KPIs for the chosen window.

    ``compare=1`` includes the previous-period totals + a delta so the
    dashboard can show "this period vs previous period" with arrows.
    """
    tid = _tenant_id(request)
    day_from, day_to = resolve_window(preset=preset, from_=from_, to=to)
    store = get_store()
    current = aggregate_for_tenant(store, tid, day_from, day_to)

    out = {
        "tenant_id": tid,
        "window": {"from": day_from, "to": day_to, "preset": preset or "custom"},
        "current": current,
    }

    if compare:
        prev_from, prev_to = previous_window(day_from, day_to)
        prev = aggregate_for_tenant(store, tid, prev_from, prev_to)
        out["previous"] = prev
        out["delta"] = {
            "total_calls": current["total_calls"] - prev["total_calls"],
            "total_sms": current["total_sms"] - prev["total_sms"],
            "spend_cents": current["spend_cents"] - prev["spend_cents"],
        }

    return out


@router.get("/assistants")
def assistants(
    request: Request,
    from_: Optional[str] = None,
    to: Optional[str] = None,
    preset: Optional[str] = None,
) -> dict:
    """Per-assistant aggregates."""
    tid = _tenant_id(request)
    day_from, day_to = resolve_window(preset=preset, from_=from_, to=to)
    store = get_store()
    rows = aggregate_for_assistants(store, tid, day_from, day_to)
    return {
        "tenant_id": tid,
        "window": {"from": day_from, "to": day_to, "preset": preset or "custom"},
        "assistants": rows,
        "count": len(rows),
    }


@router.get("/assistants/{assistant_id}/calls")
def assistant_calls(
    assistant_id: str,
    request: Request,
    from_: Optional[str] = None,
    to: Optional[str] = None,
    preset: Optional[str] = None,
    limit: int = 50,
) -> dict:
    """List recent call rows for an assistant with the live transcript
    + tool-call log attached.

    The transcript rows come from ``assistant_call_log`` (one per turn).
    For each unique call_id we also pull the recording row from
    ``recordings`` so the UI can play audio and show duration.
    """
    tid = _tenant_id(request)
    if int(limit) < 1 or int(limit) > 500:
        raise HTTPException(400, "limit must be 1-500")
    day_from, day_to = resolve_window(preset=preset, from_=from_, to=to)
    store = get_store()

    ast = store.get_assistant(tid, assistant_id)
    if not ast:
        raise HTTPException(404, f"assistant {assistant_id} not found")

    # Distinct call_ids in the window, newest first.
    call_rows = store._rows(  # type: ignore[attr-defined]
        "SELECT DISTINCT call_id FROM assistant_call_log "
        "WHERE tenant_id = ? AND assistant_id = ? "
        "AND substr(created_at, 1, 10) BETWEEN ? AND ? "
        "AND call_id IS NOT NULL "
        "ORDER BY call_id DESC LIMIT ?",
        (tid, assistant_id, day_from, day_to, int(limit)),
    )
    call_ids = [r["call_id"] for r in call_rows if r.get("call_id")]

    calls: list[dict] = []
    for cid in call_ids:
        log_rows = store._rows(  # type: ignore[attr-defined]
            "SELECT id, role, content, tool_name, tool_args, created_at "
            "FROM assistant_call_log "
            "WHERE tenant_id = ? AND call_id = ? "
            "ORDER BY created_at ASC",
            (tid, cid),
        )
        for r in log_rows:
            if r.get("tool_args"):
                try:
                    r["tool_args"] = json.loads(r["tool_args"])
                except Exception:
                    pass
        rec = store.get_recording_by_telnyx_id(tid, cid)
        calls.append({
            "call_id": cid,
            "transcript": log_rows,
            "recording": rec,
        })

    return {
        "tenant_id": tid,
        "assistant_id": assistant_id,
        "window": {"from": day_from, "to": day_to},
        "calls": calls,
        "count": len(calls),
    }


@router.get("/export.csv")
def export_csv(
    request: Request,
    from_: Optional[str] = None,
    to: Optional[str] = None,
    preset: Optional[str] = None,
) -> Response:
    """Byte-stable CSV export of the per-day rollup.

    Same query → same response. No volatile timestamps in the row
    content. Filename includes the day range for the operator's
    downloads folder.
    """
    tid = _tenant_id(request)
    day_from, day_to = resolve_window(preset=preset, from_=from_, to=to)
    store = get_store()
    rollup = store.get_rollup_window(tid, day_from, day_to)

    # If the rollup is empty (just-upgraded DB) we still emit headers
    # so the file is well-formed. One zero-row.
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow([
        "day", "calls_in", "calls_out", "sms_in", "sms_out", "spend_cents",
    ])
    for r in rollup:
        writer.writerow([
            r["day"],
            r["calls_in"],
            r["calls_out"],
            r["sms_in"],
            r["sms_out"],
            r["spend_cents"],
        ])
    csv_bytes = buf.getvalue().encode("utf-8")
    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="agentops-analytics-{day_from}-{day_to}.csv"',
            "Cache-Control": "no-store",
        },
    )
