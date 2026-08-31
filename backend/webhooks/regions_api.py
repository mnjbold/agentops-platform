"""Multi-region REST API (issue #29).

Endpoints
---------
GET  /api/health?region=auto
    Local region status + cross-region probe (DNS → /health on each
    other region's host). When ``region=local`` we skip the probe.

GET  /api/admin/regions
    List tenants by region. Operator-only (used by the dashboard's
    region map). Reads ``tenants.region``.

PATCH /api/admin/tenants/{id}/region
    Body ``{"region": "us"|"eu", "region_lock": 0|1}``. Updates
    ``tenants.region`` and optionally ``tenants.region_lock``. The
    request runs against the LOCAL region's DB; use
    ``scripts/migrate_tenant.py`` to move data between regions.

POST /api/admin/regions/verify
    Body ``{"tenant_id": "..."}``. Operator smoke-test that the
    tenant's ``region`` is consistent with the local DB. Returns
    ``{"ok": bool, "reason": "..."}``.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from webhooks._phase_b_ctx import _tenant_id
from webhooks.regions import (
    VALID_REGIONS,
    RegionLockError,
    assert_write_allowed,
    current_region,
    get_health_summary,
)
from webhooks.storage import get_store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["regions"])


# ──────────────────────────── /api/health ─────────────────────────────────────


@router.get("/health")
def health(request: Request, region: str = "local") -> dict:
    """Health endpoint (issue #29, replaces the simple /health on the API).

    ``region=auto`` adds the cross-region probe matrix; ``region=local``
    returns just the local DB status. The plain ``/health`` alias is
    still served by the server module for backward compatibility.
    """
    region = (region or "local").strip().lower()
    if region == "auto":
        return get_health_summary()
    # Local-only health (fast — no DNS / no remote call).
    local = current_region()
    return {
        "ok": True,
        "service": "w3j-telephony-webhooks",
        "region": local,
        "checked_at": _now_iso(),
    }


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ──────────────────────────── admin: region list ─────────────────────────────


@router.get("/admin/regions")
def list_by_region(request: Request) -> dict:
    """Group tenants by their configured region. Operator-only."""
    # The admin endpoints aren't auth-gated by JWT in the current
    # middleware; rely on the request having a valid tenant ctx.
    _tenant_id(request)
    store = get_store()
    out = {}
    for r in VALID_REGIONS:
        rows = store.list_tenants_by_region(r)
        # Don't leak secrets to the dashboard.
        for row in rows:
            row.pop("api_key_hash", None)
        out[r] = rows
    return {"regions": out, "local_region": current_region()}


# ──────────────────────────── admin: update region ───────────────────────────


@router.patch("/admin/tenants/{tenant_id}/region")
async def update_region(tenant_id: str, request: Request) -> dict:
    """Set a tenant's region (and optionally its lock).

    The endpoint enforces the cross-region lock: a tenant locked to
    EU cannot be reassigned to US from this process. Operators should
    run ``scripts/migrate_tenant.py`` to move the data first, then
    unlock + reassign.
    """
    # Confirm the requester is allowed to act on this tenant.
    requesting_tenant = _tenant_id(request)
    if requesting_tenant != "default" and requesting_tenant != tenant_id:
        # Non-default tenant can't move *other* tenants between regions.
        raise HTTPException(403, "only the default tenant admin can reassign regions")

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON body must be an object")
    region = (body.get("region") or "").strip().lower()
    if region not in VALID_REGIONS:
        raise HTTPException(400, f"region must be one of {list(VALID_REGIONS)}")
    region_lock_raw = body.get("region_lock")
    region_lock: Optional[int] = None
    if region_lock_raw is not None:
        region_lock = 1 if bool(region_lock_raw) else 0

    store = get_store()
    t = store.get_tenant(tenant_id)
    if not t:
        raise HTTPException(404, f"tenant {tenant_id} not found")

    # Cross-region lock: refuse to silently demote a locked tenant.
    if int(t.get("region_lock") or 0) == 1:
        old_region = (t.get("region") or "us").lower()
        if old_region != region and region_lock != 0:
            raise RegionLockError(
                f"tenant is locked to region '{old_region}'; clear the lock first"
            )

    store.update_tenant_region(tenant_id, region, region_lock=region_lock)
    return {
        "ok": True,
        "tenant_id": tenant_id,
        "region": region,
        "region_lock": region_lock if region_lock is not None else int(t.get("region_lock") or 0),
    }


# ──────────────────────────── admin: verify ──────────────────────────────────


@router.post("/admin/regions/verify")
async def verify_region(request: Request) -> dict:
    """Confirm the request's tenant + region are consistent with the
    local DB. Used by the dashboard as a "ping" before region moves."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    tenant_id = (body.get("tenant_id") or "").strip()
    if not tenant_id:
        raise HTTPException(400, "tenant_id is required")
    store = get_store()
    t = store.get_tenant(tenant_id)
    if not t:
        raise HTTPException(404, f"tenant {tenant_id} not found")
    row_region = (t.get("region") or "us").lower()
    try:
        assert_write_allowed(tenant_id, row_region)
        ok = True
        reason = None
    except RegionLockError as e:
        ok = False
        reason = str(e)
    return {
        "ok": ok,
        "reason": reason,
        "tenant_id": tenant_id,
        "tenant_region": row_region,
        "local_region": current_region(),
        "region_lock": int(t.get("region_lock") or 0),
    }
