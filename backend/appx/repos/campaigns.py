"""Campaigns repository â€” backed by Appwrite `agentops.campaigns`."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from appx.client import get_appwrite
from appwrite.query import Query

COLLECTION = "campaigns"


def _db():
    return get_appwrite().databases


def list_campaigns(tenant_id: str) -> list[dict[str, Any]]:
    r = _db().list_documents(
        database_id="agentops", collection_id=COLLECTION,
        queries=[Query.equal("tenant_id", tenant_id), Query.order_desc("$createdAt")],
    )
    return r.get("documents", [])


def get(tenant_id: str, campaign_id: str) -> Optional[dict[str, Any]]:
    try:
        d = _db().get_document(database_id="agentops", collection_id=COLLECTION, document_id=campaign_id)
        if d.get("tenant_id") == tenant_id:
            return d
    except Exception:
        pass
    return None


def create(*, tenant_id: str, name: str, type_: str, from_number: str = "",
           message: str = "", contact_ids: list[str] | None = None,
           schedule_at: str = "", status: str = "draft",
           stats: dict | None = None) -> dict[str, Any]:
    return _db().create_document(
        database_id="agentops", collection_id=COLLECTION, document_id="unique()",
        data={
            "tenant_id": tenant_id,
            "name": name,
            "type": type_,
            "from_number": from_number or "",
            "message": message or "",
            "schedule_at": schedule_at or "",
            "status": status,
            "stats_json": json.dumps(stats or {})[:1024],
        },
    )


def update_status(tenant_id: str, campaign_id: str, status: str, **extra) -> Optional[dict[str, Any]]:
    existing = get(tenant_id, campaign_id)
    if not existing:
        return None
    patch = {"status": status, **{k: v for k, v in extra.items() if k in {"stats_json"}}}
    return _db().update_document(
        database_id="agentops", collection_id=COLLECTION,
        document_id=campaign_id, data=patch,
    )


def delete(tenant_id: str, campaign_id: str) -> bool:
    existing = get(tenant_id, campaign_id)
    if not existing:
        return False
    _db().delete_document(database_id="agentops", collection_id=COLLECTION, document_id=campaign_id)
    return True
