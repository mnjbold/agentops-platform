"""telnyx_events repository â€” append-only audit log of every webhook event."""
from __future__ import annotations

import json
from typing import Any, Optional

from appx.client import get_appwrite
from appwrite.query import Query

COLLECTION = "telnyx_events"


def _db():
    return get_appwrite().databases


def append(*, event_type: str, call_control_id: str = "", from_number: str = "",
          to_number: str = "", direction: str = "", payload: dict | None = None,
          received_at) -> dict[str, Any]:
    raw = json.dumps(payload or {}, default=str)[:16000]
    return _db().create_document(
        database_id="agentops", collection_id=COLLECTION,
        document_id="unique()",
        data={
            "event_type": event_type,
            "call_control_id": call_control_id or "",
            "from_number": from_number or "",
            "to_number": to_number or "",
            "direction": direction or "",
            "raw_json": raw,
            "received_at": received_at,
        },
    )


def list_recent(tenant_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """Recent webhook events for the tenant (tenant filter by event_type prefix
    doesn't really work since events don't carry tenant â€” caller filters)."""
    r = _db().list_documents(
        database_id="agentops", collection_id=COLLECTION,
        queries=[Query.order_desc("received_at"), Query.limit(limit)],
    )
    return r.get("documents", [])
