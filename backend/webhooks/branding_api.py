"""White-label / per-tenant branding REST API (issue #30).

Endpoints
---------
GET  /api/tenant/brand?subdomain=acme
    Public, unauthenticated. Resolves the tenant by subdomain (or by
    the ``Host`` header set by the platform's edge) and returns the
    safe-for-public brand JSON. Used by the login page to apply
    theming BEFORE first paint (the inline script in
    ``frontend/index.html`` calls this).

GET  /api/tenant/brand?domain=acme.com
    Same as above but the operator can also resolve by their custom
    domain (e.g. from a CNAME that already points at us).

PUT  /api/admin/tenants/{id}/brand
    Auth: tenant admin. Body: ``{...}`` — any subset of the brand
    fields. Returns the normalised brand.

GET  /api/admin/tenants/{id}/brand/verify-domain
    Returns ``{expected, actual, verified, reason}`` after DNS-probing
    the tenant's ``custom_domain``. The dashboard calls this when the
    operator clicks "Verify domain" in the white-label panel.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from webhooks._phase_b_ctx import _tenant_id
from webhooks.branding import (
    public_brand,
    normalise_brand,
    tenant_from_host,
    verify_custom_domain,
    is_apex_host,
)
from webhooks.storage import get_store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["branding"])


# ──────────────────────────── public lookup ──────────────────────────────────


def _resolve_tenant_for_public(request: Request, subdomain: Optional[str],
                                domain: Optional[str]) -> Optional[dict]:
    """Find a tenant for the public brand endpoint.

    Resolution order:
      1. ``?subdomain=acme`` (the operator types it in the URL bar)
      2. ``?domain=acme.com`` (the operator is using a custom domain
         and wants to test the lookup)
      3. The request's ``Host`` header — covers the actual
         ``acme.agentops.com`` and ``acme.com`` flows
    """
    store = get_store()
    if subdomain:
        return store.get_tenant(subdomain.strip().lower())
    if domain:
        d = domain.strip().lower()
        t = store.find_tenant_by_domain(d)
        if t:
            return t
        # Fall back: maybe the operator passed a subdomain-shaped
        # string in the ``domain`` query param.
        if "." not in d:
            return store.get_tenant(d)
        return None
    host = request.headers.get("host") or request.headers.get("Host") or ""
    tenant_id = tenant_from_host(host)
    if not tenant_id:
        return None
    return store.get_tenant(tenant_id)


@router.get("/tenant/brand")
def public_tenant_brand(
    request: Request,
    subdomain: Optional[str] = None,
    domain: Optional[str] = None,
) -> dict:
    """Public brand lookup. Returns the apex-defaults shape when the
    request isn't tenant-scoped (the operator typed the bare apex URL
    or the CDN swallowed the Host header)."""
    tenant = _resolve_tenant_for_public(request, subdomain, domain)
    if not tenant:
        # Apex host — return the platform's default brand so the
        # frontend can still render the chrome. ``tenant_id`` is None
        # so the frontend knows to use the operator's stored theme.
        return {
            "tenant_id": None,
            "is_apex": True,
            "brand": public_brand({}),
        }
    raw = _safe_brand(tenant)
    return {
        "tenant_id": tenant.get("id"),
        "is_apex": False,
        "brand": public_brand(raw),
    }


def _safe_brand(tenant: dict) -> dict:
    """Pull the brand_json off a tenant row, defensively."""
    import json
    raw = tenant.get("brand_json")
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


# ──────────────────────────── admin: update brand ────────────────────────────


@router.put("/admin/tenants/{tenant_id}/brand")
async def admin_update_brand(tenant_id: str, request: Request) -> dict:
    """Replace or merge the tenant's brand JSON. Auth: admin only."""
    requesting_tenant = _tenant_id(request)
    if requesting_tenant != "default" and requesting_tenant != tenant_id:
        raise HTTPException(403, "only the default tenant admin can edit other tenants")
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON body must be an object")

    store = get_store()
    t = store.get_tenant(tenant_id)
    if not t:
        raise HTTPException(404, f"tenant {tenant_id} not found")

    # Merge with the existing brand so PUT works as a partial update.
    existing = _safe_brand(t)
    merged = {**existing, **body}
    # Drop keys that aren't part of the public schema.
    cleaned = normalise_brand(merged)
    store.update_tenant_brand(tenant_id, cleaned)
    return {
        "ok": True,
        "tenant_id": tenant_id,
        "brand": public_brand(cleaned),
    }


# ──────────────────────────── admin: verify custom domain ─────────────────────


@router.get("/admin/tenants/{tenant_id}/brand/verify-domain")
def admin_verify_domain(tenant_id: str, request: Request) -> dict:
    """Best-effort CNAME check for the tenant's ``custom_domain``."""
    requesting_tenant = _tenant_id(request)
    if requesting_tenant != "default" and requesting_tenant != tenant_id:
        raise HTTPException(403, "only the default tenant admin can verify other tenants")
    store = get_store()
    t = store.get_tenant(tenant_id)
    if not t:
        raise HTTPException(404, f"tenant {tenant_id} not found")
    brand = _safe_brand(t)
    domain = (brand.get("custom_domain") or "").strip()
    if not domain:
        return {
            "tenant_id": tenant_id,
            "verified": False,
            "expected": "",
            "actual": None,
            "reason": "no custom_domain configured",
        }
    result = verify_custom_domain(domain)
    # If the verification just succeeded, flip the flag in storage.
    if result.get("verified"):
        brand["custom_domain_verified"] = True
        store.update_tenant_brand(tenant_id, normalise_brand(brand))
    return {"tenant_id": tenant_id, "domain": domain, **result}
