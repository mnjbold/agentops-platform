"""Admin API — tenant and secret management (Phase A issues #2, #3).

Endpoints
---------
GET    /api/admin/tenants                          list tenants
POST   /api/admin/tenants                          create a tenant
GET    /api/admin/tenants/{tenant_id}              fetch one tenant
POST   /api/admin/tenants/{tenant_id}/rotate-key   rotate API key
PATCH  /api/admin/tenants/{tenant_id}              update tier
GET    /api/admin/tenants/{tenant_id}/secrets      list secret *keys* (no values)
PUT    /api/admin/tenants/{tenant_id}/secrets/{key}  upsert a secret
DELETE /api/admin/tenants/{tenant_id}/secrets/{key}  delete a secret
GET    /api/admin/me                               current tenant (from auth context)

All endpoints require either:
* a valid ``X-Api-Key`` matching the tenant in the URL (per-tenant scope), or
* a JWT in ``Authorization: Bearer <token>`` whose ``tid`` matches the URL.

Cross-tenant access returns 403. Missing auth returns 401.
"""
from __future__ import annotations

import logging
import secrets as _secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from webhooks.storage import get_store
from webhooks.tenancy import (
    TenantContext,
    decrypt_secret,
    encrypt_secret,
    hash_api_key,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ──────────────────────────── auth dependency ───────────────────────────────
# We need a "current_tenant" dependency here that returns the *TenantContext*
# (set by the middleware) so admin endpoints can verify the URL tenant
# matches. The full JWT/X-Api-Key middleware lives in server.py; here we
# just read what it set on request.state.


def _ctx(request: Request) -> TenantContext:
    """Pull the TenantContext the middleware populated, or raise 401."""
    ctx: Optional[TenantContext] = getattr(request.state, "tenant_ctx", None)
    if ctx is None:
        raise HTTPException(401, "authentication required")
    return ctx


def _require_tenant_match(ctx: TenantContext, url_tenant_id: str) -> None:
    """Per-tenant admin scope: the URL tenant must match the auth tenant."""
    if ctx.tenant_id != url_tenant_id:
        raise HTTPException(403, f"tenant mismatch: auth={ctx.tenant_id} url={url_tenant_id}")


# ──────────────────────────── tenants ──────────────────────────────────────


@router.get("/tenants")
def list_tenants(request: Request) -> dict:
    """List all tenants. Requires a JWT in the same tenant; only operators
    with the ``admin`` role can see the full list (v1: the default tenant
    can see itself; full multi-tenant directory comes later)."""
    ctx = _ctx(request)
    store = get_store()
    if ctx.tenant_id == "default":
        tenants = store.list_tenants()
    else:
        # Non-default tenants only see their own row.
        self_row = store.get_tenant(ctx.tenant_id)
        tenants = [self_row] if self_row else []
    # Never leak the API key hash to callers; it's bcrypt anyway but still.
    for t in tenants:
        t.pop("api_key_hash", None)
    return {"tenants": tenants, "count": len(tenants)}


@router.post("/tenants")
async def create_tenant(request: Request) -> dict:
    """Create a tenant and return the freshly-generated API key **once**.

    Body: ``{name, tier?}``. Returns: ``{tenant_id, api_key, created_at}``.

    Authorization: the caller must be the ``default`` tenant (operator tier).
    In Phase B we'll add a true super-admin role; for now the default
    tenant is the only one that can mint new tenants.
    """
    ctx = _ctx(request)
    if ctx.tenant_id != "default":
        raise HTTPException(403, "only the default tenant can create new tenants")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    name = (body.get("name") or "").strip()
    tier = (body.get("tier") or "free").strip()
    if not name:
        raise HTTPException(400, "name is required")
    if tier not in ("free", "pro", "enterprise"):
        raise HTTPException(400, f"tier must be one of: free, pro, enterprise (got {tier!r})")

    api_key = "w3j_" + _secrets.token_urlsafe(32)
    api_key_hash = hash_api_key(api_key)
    tenant_id = "t_" + _secrets.token_urlsafe(10)
    store = get_store()
    tenant = store.create_tenant(
        tenant_id=tenant_id,
        name=name,
        tier=tier,
        api_key_hash=api_key_hash,
    )
    log.info("Created tenant %s (%s, tier=%s)", tenant_id, name, tier)
    return {
        "tenant_id": tenant_id,
        "api_key": api_key,  # shown ONCE; never returned again
        "name": name,
        "tier": tier,
        "created_at": tenant["created_at"],
    }


@router.get("/tenants/{tenant_id}")
def get_tenant(tenant_id: str, request: Request) -> dict:
    ctx = _ctx(request)
    _require_tenant_match(ctx, tenant_id)
    store = get_store()
    tenant = store.get_tenant(tenant_id)
    if not tenant:
        raise HTTPException(404, f"tenant {tenant_id} not found")
    tenant.pop("api_key_hash", None)
    return {"tenant": tenant}


@router.post("/tenants/{tenant_id}/rotate-key")
def rotate_api_key(tenant_id: str, request: Request) -> dict:
    """Generate a new API key for the tenant. The old one is invalidated.

    Returns ``{tenant_id, api_key}`` — the new key is shown once.
    """
    ctx = _ctx(request)
    _require_tenant_match(ctx, tenant_id)
    store = get_store()
    if not store.get_tenant(tenant_id):
        raise HTTPException(404, f"tenant {tenant_id} not found")
    new_key = "w3j_" + _secrets.token_urlsafe(32)
    new_hash = hash_api_key(new_key)
    store.rotate_tenant_api_key(tenant_id, new_hash)
    log.info("Rotated API key for tenant %s", tenant_id)
    return {"tenant_id": tenant_id, "api_key": new_key}


@router.patch("/tenants/{tenant_id}")
async def update_tenant(tenant_id: str, request: Request) -> dict:
    """Update tenant tier (only field exposed in v1)."""
    ctx = _ctx(request)
    _require_tenant_match(ctx, tenant_id)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    tier = body.get("tier")
    if tier is None:
        raise HTTPException(400, "no updatable fields provided")
    if tier not in ("free", "pro", "enterprise"):
        raise HTTPException(400, f"tier must be one of: free, pro, enterprise (got {tier!r})")
    store = get_store()
    store.update_tenant_tier(tenant_id, tier)
    return {"ok": True, "tenant_id": tenant_id, "tier": tier}


# ──────────────────────── tenant_secrets ────────────────────────────────────


@router.get("/tenants/{tenant_id}/secrets")
def list_secrets(tenant_id: str, request: Request) -> dict:
    """List *keys* (no values) for the tenant. Safe to expose to the
    tenant's admin UI so operators can see what they have stored."""
    ctx = _ctx(request)
    _require_tenant_match(ctx, tenant_id)
    store = get_store()
    if not store.get_tenant(tenant_id):
        raise HTTPException(404, f"tenant {tenant_id} not found")
    secrets_list = store.list_secrets(tenant_id)
    return {"secrets": secrets_list, "count": len(secrets_list)}


@router.put("/tenants/{tenant_id}/secrets/{key}")
async def upsert_secret(tenant_id: str, key: str, request: Request) -> dict:
    """Encrypt and store a secret. Body: ``{value}``.

    Returns 204 No Content on success. The value is Fernet-encrypted with
    the master key before it hits disk; the plaintext is never persisted.
    """
    ctx = _ctx(request)
    _require_tenant_match(ctx, tenant_id)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    value = body.get("value")
    if value is None:
        raise HTTPException(400, "value is required")
    if not isinstance(value, str):
        raise HTTPException(400, "value must be a string")
    if not key or len(key) > 100:
        raise HTTPException(400, "key must be 1-100 characters")
    if not key.replace("_", "").replace("-", "").replace(".", "").isalnum():
        raise HTTPException(400, "key must be alphanumeric (._- allowed)")
    store = get_store()
    if not store.get_tenant(tenant_id):
        raise HTTPException(404, f"tenant {tenant_id} not found")
    encrypted = encrypt_secret(value)
    store.upsert_secret(tenant_id, key, encrypted)
    log.info("Stored secret %s for tenant %s", key, tenant_id)
    return {"ok": True, "tenant_id": tenant_id, "key": key}


@router.delete("/tenants/{tenant_id}/secrets/{key}")
def delete_secret(tenant_id: str, key: str, request: Request) -> dict:
    ctx = _ctx(request)
    _require_tenant_match(ctx, tenant_id)
    store = get_store()
    if not store.delete_secret(tenant_id, key):
        raise HTTPException(404, f"secret {key!r} not found for tenant {tenant_id}")
    return {"ok": True, "tenant_id": tenant_id, "key": key}


# ──────────────────────── helper used elsewhere ────────────────────────────


def get_tenant_secret(tenant_id: str, key: str) -> Optional[str]:
    """Read a tenant secret by key. Returns the **plaintext** (decrypted)
    or ``None`` if the key is unset. Centralised here so call sites
    don't need to import Fernet + store.

    Used by the WebRTC credential lookup, the dialer, etc. — anywhere
    that used to read ``os.environ["TELNYX_WEBRTC_*"]``.
    """
    store = get_store()
    ciphertext = store.get_secret(tenant_id, key)
    if ciphertext is None:
        return None
    return decrypt_secret(ciphertext)
