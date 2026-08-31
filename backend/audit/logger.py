"""Audit logger — pure helpers (issue #20).

The FastAPI middleware uses these to derive a row dict and append it.
Splitting the helpers from the middleware makes it easy to write a
direct unit test that doesn't have to spin up a TestClient.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Optional

from webhooks.storage import get_store

log = logging.getLogger(__name__)

# Path patterns we audit. Anything else (Swagger assets, health) is
# dropped so the table doesn't fill with noise.
_AUDIT_PATH_RE = re.compile(r"^/v?api/")


def is_audit_path(path: str) -> bool:
    """Return True if requests to this path should land in the audit log."""
    if not path:
        return False
    # Skip the audit endpoints themselves (they would create an infinite
    # loop of "I just read the audit log" entries).
    if path.startswith("/api/audit") or path.startswith("/v1/audit"):
        return False
    if path.startswith("/v1/docs") or path.startswith("/api/docs"):
        return False
    if path == "/health" or path.startswith("/health/"):
        return False
    if path.startswith("/ws") or path.startswith("/webhooks/telnyx"):
        # Webhooks are signed by Telnyx; we still want a record but
        # they're not a "user action" the same way. Treated as a separate
        # stream (the deliveries table).
        return False
    return bool(_AUDIT_PATH_RE.match(path))


def derive_action(method: str, path: str) -> str:
    """Normalise ``GET /api/contacts`` -> ``contacts.list``,
    ``POST /api/auth/login`` -> ``auth.login``,
    ``POST /api/admin/tenants/{id}/rotate-key`` -> ``admin.tenants.rotate_key``.

    The first non-prefix segment is the resource; the verb is derived
    from the HTTP method.
    """
    method = (method or "").upper()
    # Strip /v1 or /api prefix.
    p = re.sub(r"^/(v1|api)", "", path or "").strip("/")
    parts = p.split("/") if p else []
    # Skip the first segment if it's a version-like prefix.
    if parts and parts[0] in ("v1",):
        parts = parts[1:]

    verb = {
        "GET": "list",
        "POST": "create",
        "PUT": "update",
        "PATCH": "update",
        "DELETE": "delete",
    }.get(method, method.lower() or "unknown")

    if not parts:
        return f"root.{verb}"

    # Treat "{collection}/{id}/{verb}" patterns: GET /contacts/abc -> contacts.read
    if len(parts) >= 2 and method in ("GET", "PUT", "PATCH", "DELETE") and not parts[1].startswith("{"):
        # First segment is the resource.
        resource = parts[0]
        if method == "GET":
            return f"{resource}.read"
        if method == "DELETE":
            return f"{resource}.delete"
        return f"{resource}.{verb}"

    # /auth/login, /auth/me, /auth/reset -> auth.login etc.
    # /api/admin/tenants/{id}/rotate-key -> admin.tenants.rotate_key
    if len(parts) >= 3 and parts[1] not in ("v1",):
        # Skip the {id} segment if it looks like one (starts with t_, u_,
        # ast_, cmp_, etc. — see _derive_target in middleware.py).
        looks_like_id = (
            parts[2].startswith("t_") or parts[2].startswith("u_")
            or parts[2].startswith("ast_") or parts[2].startswith("cmp_")
            or parts[2].startswith("vm_") or parts[2].startswith("rec_")
            or parts[2].startswith("dlv_") or parts[2].startswith("aud_")
            or parts[2].startswith("sub_") or parts[2].startswith("id_")
            or parts[2].startswith("cus_")
        )
        if looks_like_id and len(parts) >= 4:
            return f"{parts[0]}.{parts[1]}.{parts[3].replace('-', '_')}"
        return f"{parts[0]}.{parts[1]}.{parts[2].replace('-', '_')}"

    if len(parts) >= 2 and parts[1] in ("login", "logout", "me", "reset", "rotate-key",
                                          "rotate_key", "billing", "webhook", "checkout",
                                          "portal", "start", "stop", "pause", "resume",
                                          "launch", "answer", "hangup", "reject", "read"):
        return ".".join(parts[:2]).replace("-", "_")

    return f"{parts[0]}.{verb}"


def append_audit(
    tenant_id: str,
    action: str,
    method: str,
    path: str,
    response_status: int,
    response_time_ms: int,
    *,
    user_id: Optional[str] = None,
    target: Optional[str] = None,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
    request_body: Optional[str] = None,
    response_body: Optional[str] = None,
) -> str:
    """Append one audit row. Returns the new row id."""
    entry = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "action": action,
        "target": target,
        "ip": ip,
        "user_agent": user_agent,
        "request_id": uuid.uuid4().hex,
        "method": method,
        "path": path,
        "response_status": int(response_status),
        "response_time_ms": int(response_time_ms),
        "request_body": _truncate_body(request_body),
        "response_body": _truncate_body(response_body),
        "timestamp": _utcnow(),
    }
    return get_store().append_audit(entry)


def _truncate_body(body: Optional[str], max_bytes: int = 4096) -> Optional[str]:
    """Don't let a 10MB POST body blow out the audit table. Truncate with
    a marker so the operator can see "this was clipped"."""
    if body is None:
        return None
    if not isinstance(body, str):
        try:
            body = json.dumps(body)
        except Exception:
            body = str(body)[:max_bytes]
    if len(body) <= max_bytes:
        return body
    return body[:max_bytes] + f"... [truncated {len(body) - max_bytes} bytes]"


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
