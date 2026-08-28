"""Messages repository - backed by Appwrite `agentops.messages`."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any, Optional

from appx.client import get_appwrite
from appwrite.query import Query

COLLECTION = "messages"


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


def _to_doc_id(message_id: str) -> str:
    """Map a Telnyx message_id to a valid Appwrite document_id.

    Same rules as calls: Appwrite wants <=36 chars, [a-zA-Z0-9._-],
    no leading special. Most Telnyx message_ids are UUIDs (36 chars,
    all valid), but a defensive hash for any future format keeps us
    safe.
    """
    if not message_id:
        return "unknown"
    if re.match(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,35}$', message_id):
        return message_id
    h = hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:32]
    return f"m{h}"


def upsert_message(*, tenant_id: str, message_id: str, direction: str,
                   from_number: str, to_number: str, body: str = "",
                   media_urls: list[str] | None = None, status: str,
                   sent_at=None, received_at=None) -> dict[str, Any]:
    data = {
        "tenant_id": tenant_id,
        "message_id": message_id,
        "direction": direction,
        "from_number": from_number,
        "to_number": to_number,
        "body": body or "",
        "media_urls": media_urls or [],
        "status": status,
        "sent_at": _iso(sent_at),
        "received_at": _iso(received_at),
    }
    data = {k: v for k, v in data.items() if v not in (None, "")}

    doc_id = _to_doc_id(message_id)
    db = _db()
    try:
        return db.update_document(
            database_id="agentops", collection_id=COLLECTION,
            document_id=doc_id, data=data,
        )
    except Exception:
        return db.create_document(
            database_id="agentops", collection_id=COLLECTION,
            document_id=doc_id, data=data,
        )


def list_recent(tenant_id: str, limit: int = 50) -> list[dict[str, Any]]:
    r = _db().list_documents(
        database_id="agentops", collection_id=COLLECTION,
        queries=[
            Query.equal("tenant_id", tenant_id),
            Query.order_desc("$createdAt"),
            Query.limit(limit),
        ],
    )
    return r.get("documents", [])


def list_threads(tenant_id: str, limit: int = 30) -> list[dict[str, Any]]:
    """Group messages by (from, to) pair, return latest from each thread.

    Appwrite doesn't have native group-by, so we fetch recent messages
    and dedupe in Python. For tenants with thousands of threads this
    needs a real aggregation; for the MVP (single user) it's fine.
    """
    r = _db().list_documents(
        database_id="agentops", collection_id=COLLECTION,
        queries=[
            Query.equal("tenant_id", tenant_id),
            Query.order_desc("$createdAt"),
            Query.limit(500),
        ],
    )
    threads: dict[tuple[str, str], dict[str, Any]] = {}
    for m in r.get("documents", []):
        key = (m.get("from_number", ""), m.get("to_number", ""))
        if key not in threads:
            threads[key] = {
                "from_number": key[0],
                "to_number": key[1],
                "last_message": m,
                "count": 0,
            }
        threads[key]["count"] += 1
        if len(threads) >= limit:
            break
    return list(threads.values())
