"""Contacts repository â€” backed by Appwrite `agentops.contacts`."""
from __future__ import annotations

from typing import Any, Optional

from appx.client import get_appwrite
from appwrite.query import Query

COLLECTION = "contacts"


def _db():
    return get_appwrite().databases


def list_contacts(tenant_id: str, limit: int = 100) -> list[dict[str, Any]]:
    r = _db().list_documents(
        database_id="agentops",
        collection_id=COLLECTION,
        queries=[
            Query.equal("tenant_id", tenant_id),
            Query.order_desc("$createdAt"),
            Query.limit(limit),
        ],
    )
    return r.get("documents", [])


def get_contact(tenant_id: str, contact_id: str) -> Optional[dict[str, Any]]:
    try:
        d = _db().get_document(database_id="agentops", collection_id=COLLECTION, document_id=contact_id)
        if d.get("tenant_id") == tenant_id:
            return d
        return None
    except Exception:
        return None


def find_by_phone(tenant_id: str, phone: str) -> Optional[dict[str, Any]]:
    r = _db().list_documents(
        database_id="agentops",
        collection_id=COLLECTION,
        queries=[
            Query.equal("tenant_id", tenant_id),
            Query.equal("phone", phone),
            Query.limit(1),
        ],
    )
    docs = r.get("documents", [])
    return docs[0] if docs else None


def create_contact(tenant_id: str, *, name: str = "", phone: str, email: str = "",
                  tags: list[str] | None = None, source: str = "manual") -> dict[str, Any]:
    return _db().create_document(
        database_id="agentops",
        collection_id=COLLECTION,
        document_id="unique()",
        data={
            "tenant_id": tenant_id,
            "name": name or "",
            "phone": phone,
            "email": email or "",
            "tags": tags or [],
            "source": source,
        },
    )


def update_contact(tenant_id: str, contact_id: str, **fields) -> Optional[dict[str, Any]]:
    existing = get_contact(tenant_id, contact_id)
    if not existing:
        return None
    patch = {k: v for k, v in fields.items() if k in {"name", "phone", "email", "tags", "source"}}
    if not patch:
        return existing
    return _db().update_document(
        database_id="agentops",
        collection_id=COLLECTION,
        document_id=contact_id,
        data=patch,
    )


def delete_contact(tenant_id: str, contact_id: str) -> bool:
    existing = get_contact(tenant_id, contact_id)
    if not existing:
        return False
    _db().delete_document(database_id="agentops", collection_id=COLLECTION, document_id=contact_id)
    return True
