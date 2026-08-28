"""Scheduled jobs repository â€” backed by Appwrite `agentops.scheduled_jobs`."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from appx.client import get_appwrite
from appwrite.query import Query

COLLECTION = "scheduled_jobs"


def _db():
    return get_appwrite().databases


def list_jobs(tenant_id: str) -> list[dict[str, Any]]:
    r = _db().list_documents(
        database_id="agentops", collection_id=COLLECTION,
        queries=[Query.equal("tenant_id", tenant_id), Query.order_asc("run_at")],
    )
    return r.get("documents", [])


def due_jobs(now_iso: str, limit: int = 50) -> list[dict[str, Any]]:
    """Find jobs whose run_at <= now and are still pending. Used by the
    in-process SaaS scheduler thread."""
    r = _db().list_documents(
        database_id="agentops", collection_id=COLLECTION,
        queries=[
            Query.equal("status", "pending"),
            Query.less_than_equal("run_at", now_iso),
            Query.limit(limit),
        ],
    )
    return r.get("documents", [])


def create(*, tenant_id: str, kind: str, payload: dict, run_at: str,
           status: str = "pending") -> dict[str, Any]:
    return _db().create_document(
        database_id="agentops", collection_id=COLLECTION, document_id="unique()",
        data={
            "tenant_id": tenant_id,
            "kind": kind,
            "payload_json": json.dumps(payload, default=str)[:8192],
            "run_at": run_at,
            "status": status,
        },
    )


def update(job_id: str, **fields) -> dict[str, Any]:
    patch = {k: v for k, v in fields.items() if k in {"status", "last_error"}}
    return _db().update_document(
        database_id="agentops", collection_id=COLLECTION,
        document_id=job_id, data=patch,
    )


def delete(tenant_id: str, job_id: str) -> bool:
    try:
        d = _db().get_document(database_id="agentops", collection_id=COLLECTION, document_id=job_id)
        if d.get("tenant_id") != tenant_id:
            return False
        _db().delete_document(database_id="agentops", collection_id=COLLECTION, document_id=job_id)
        return True
    except Exception:
        return False
