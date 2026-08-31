"""Audit log REST API (issue #20).

Endpoints
---------
GET  /v1/audit?from=...&to=...&user_id=...&action=...&limit=50
GET  /v1/audit/{id}             full row including request + response bodies
GET  /v1/audit/export?format=csv|json&from=...&to=...

All endpoints are tenant-scoped — the middleware's ``tenant_ctx`` decides
which rows the caller can see.
"""
from __future__ import annotations

import csv
import io
import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from webhooks.storage import get_store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/audit", tags=["Audit"])


def _tenant_id(request: Request) -> str:
    tid = getattr(request.state, "tenant_id", None)
    if not tid:
        raise HTTPException(401, "authentication required")
    return tid


@router.get("")
def list_audit(
    request: Request,
    from_: Optional[str] = None,
    to: Optional[str] = None,
    user_id: Optional[str] = None,
    action: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Paginated audit list. Newest first."""
    tid = _tenant_id(request)
    if int(limit) < 1 or int(limit) > 500:
        raise HTTPException(400, "limit must be 1-500")
    store = get_store()
    rows = store.list_audit(
        tenant_id=tid,
        user_id=user_id,
        action=action,
        day_from=from_,
        day_to=to,
        limit=int(limit),
        offset=int(offset),
    )
    return {
        "tenant_id": tid,
        "items": rows,
        "count": len(rows),
        "limit": int(limit),
        "offset": int(offset),
    }


# NOTE: /export is mounted BEFORE /{audit_id} so FastAPI's path matcher
# doesn't confuse "export" for an audit id.
@router.get("/export")
def export_audit(
    request: Request,
    format: str = "csv",
    from_: Optional[str] = None,
    to: Optional[str] = None,
    action: Optional[str] = None,
) -> Response:
    """Byte-stable export. Same query → same bytes, every time.

    CSV columns (in this order) are the column names from the audit_log
    table. JSON is a single object with a ``items`` array.
    """
    tid = _tenant_id(request)
    fmt = (format or "csv").lower()
    if fmt not in ("csv", "json"):
        raise HTTPException(400, "format must be csv or json")

    store = get_store()
    # Pull all rows (capped to a safe max) for the window. We do this
    # server-side so the export is reproducible regardless of pagination.
    rows = store.list_audit(
        tenant_id=tid,
        action=action,
        day_from=from_,
        day_to=to,
        limit=10_000,
        offset=0,
    )

    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow([
            "id", "tenant_id", "user_id", "action", "target",
            "ip", "user_agent", "request_id", "method", "path",
            "response_status", "response_time_ms", "timestamp",
        ])
        for r in rows:
            writer.writerow([
                r.get("id"), r.get("tenant_id"), r.get("user_id"),
                r.get("action"), r.get("target"),
                r.get("ip"), r.get("user_agent"), r.get("request_id"),
                r.get("method"), r.get("path"),
                r.get("response_status"), r.get("response_time_ms"),
                r.get("timestamp"),
            ])
        body = buf.getvalue().encode("utf-8")
        return Response(
            content=body,
            media_type="text/csv",
            headers={
                "Content-Disposition": 'attachment; filename="agentops-audit.csv"',
                "Cache-Control": "no-store",
            },
        )

    # JSON
    return Response(
        content=json.dumps(
            {"tenant_id": tid, "items": rows, "count": len(rows)},
            indent=2, sort_keys=True,
        ).encode("utf-8"),
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="agentops-audit.json"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/{audit_id}")
def get_audit(audit_id: str, request: Request) -> dict:
    """Full row — includes the truncated request/response bodies."""
    tid = _tenant_id(request)
    store = get_store()
    row = store.get_audit(tid, audit_id)
    if not row:
        raise HTTPException(404, f"audit row {audit_id} not found")
    return {"tenant_id": tid, "item": row}
