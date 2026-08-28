"""Calls repository - backed by Appwrite `agentops.calls`.

All calls (inbound + outbound) get a row here. The dashboard reads from
here for /api/calls/recent. The Telnyx API is still called for live
data (active calls list) - this is for the historical record.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from typing import Any, Optional

from appx.client import get_appwrite
from appwrite.query import Query

log = logging.getLogger(__name__)

COLLECTION = "calls"


def _db():
    return get_appwrite().databases


def _iso(ts) -> Optional[str]:
    if not ts:
        return None
    if isinstance(ts, str):
        return ts
    if isinstance(ts, (int, float)):
        return datetime.utcfromtimestamp(ts).isoformat() + "Z"
    return str(ts)


def _to_doc_id(call_control_id: str) -> str:
    """Map a Telnyx call_control_id to a valid Appwrite document_id.

    Appwrite document IDs must be <= 36 chars, [a-zA-Z0-9._-], and not
    start with a special char. Telnyx IDs look like
    `v3:7tccDtBM4KDTcvtgXQZ6ssIM...` (too long, contains a colon).
    We hash the original ID with SHA-256 and take the first 32 hex
    chars, prefixed with a letter. Collisions on SHA-256-128 are
    negligible for telephony event volume.
    """
    if not call_control_id:
        return "unknown"
    if re.match(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,35}$', call_control_id):
        return call_control_id
    h = hashlib.sha256(call_control_id.encode("utf-8")).hexdigest()[:32]
    return f"c{h}"  # prefix with letter to satisfy "can't start with special"


def upsert_call(*, tenant_id: str, call_control_id: str, direction: str,
                from_number: str, to_number: str, from_name: str = "", to_name: str = "",
                status: str, started_at, answered_at=None, ended_at=None,
                duration_seconds: int = 0, has_recording: bool = False,
                recording_url: str = "", assistant_id: str = "") -> dict[str, Any]:
    """Create or update a call row (idempotent on call_control_id)."""
    data = {
        "tenant_id": tenant_id,
        "call_control_id": call_control_id,
        "direction": direction,
        "from_number": from_number,
        "to_number": to_number,
        "from_name": from_name or "",
        "to_name": to_name or "",
        "status": status,
        "started_at": _iso(started_at),
        "answered_at": _iso(answered_at),
        "ended_at": _iso(ended_at),
        "duration_seconds": int(duration_seconds or 0),
        "has_recording": bool(has_recording),
        "recording_url": recording_url or "",
        "assistant_id": assistant_id or "",
    }
    # Strip empty optionals (Appwrite will reject them otherwise)
    data = {k: v for k, v in data.items() if v not in (None, "")}

    doc_id = _to_doc_id(call_control_id)
    db = _db()
    # Try update first (most webhook events are updates)
    try:
        existing = db.get_document(database_id="agentops", collection_id=COLLECTION, document_id=doc_id)
        return db.update_document(
            database_id="agentops", collection_id=COLLECTION, document_id=doc_id,
            data={k: v for k, v in data.items() if k not in {"call_control_id", "started_at", "tenant_id"}},
        )
    except Exception:
        # Doesn't exist yet - create
        return db.create_document(
            database_id="agentops", collection_id=COLLECTION,
            document_id=doc_id, data=data,
        )


def list_recent(tenant_id: str, limit: int = 50) -> list[dict[str, Any]]:
    r = _db().list_documents(
        database_id="agentops", collection_id=COLLECTION,
        queries=[
            Query.equal("tenant_id", tenant_id),
            Query.order_desc("started_at"),
            Query.limit(limit),
        ],
    )
    return r.get("documents", [])


def get(tenant_id: str, call_control_id: str) -> Optional[dict[str, Any]]:
    try:
        d = _db().get_document(
            database_id="agentops", collection_id=COLLECTION,
            document_id=_to_doc_id(call_control_id),
        )
        if d.get("tenant_id") == tenant_id:
            return d
    except Exception:
        pass
    return None
