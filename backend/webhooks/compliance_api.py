"""Compliance pre-flight API (issue #25).

Endpoints
---------
GET  /api/compliance/preview?campaign_id=X
    Returns ``{total, will_dial, skipped_dnc, skipped_time, ...}`` for
    the campaign's contact list. Computed by running the contact list
    through the DNC cache + the time-of-day check.

GET  /api/compliance/dnc?phone=+1...
    Single-phone DNC lookup. Caches for 30 days.

POST /api/compliance/dnc/refresh
    Force-refresh a phone's DNC cache row. Body: ``{"phone": ..., "source": "us_dnc"}``.

GET  /api/compliance/stats
    Rollup: total cache rows, expired, DNC, non-DNC. Powers the
    dashboard "compliance health" panel.

PATCH /api/campaigns/{id}/compliance
    Update the per-campaign compliance settings:
    ``dnc_check_enabled``, ``time_window_enabled``, ``time_window_start``,
    ``time_window_end``. The PATCH on ``/api/campaigns/{id}`` already
    accepts these fields, but a dedicated endpoint gives the frontend
    a single URL to POST the compliance section to.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from compliance.dnc import (
    DEFAULT_SOURCE,
    bulk_check_dnc,
    cache_stats,
    check_dnc,
    get_cache_row,
    refresh_cache,
)
from compliance.time_window import (
    DEFAULT_TZ,
    bulk_filter_window,
    get_timezone,
    is_in_window,
)
from webhooks._phase_b_ctx import _tenant_id
from webhooks.storage import get_store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["compliance"])


# ──────────────────────────── helpers ────────────────────────────────────────


def _campaign_phones(
    tenant_id: str, campaign_id: str, store
) -> list[dict]:
    """Return ``[{"id": contact_id, "phone": "..."}]`` for the campaign.

    Order matches the campaign's ``contact_ids`` list so the preview
    is deterministic (helps the UI highlight the first skipped row).
    """
    camp = store.get_campaign(tenant_id, campaign_id)
    if not camp:
        raise HTTPException(404, f"campaign {campaign_id} not found")
    cids = camp.get("contact_ids") or []
    if not cids:
        return []
    out: list[dict] = []
    for cid in cids:
        c = store.get_contact(tenant_id, cid)
        if not c:
            continue
        out.append({"id": cid, "phone": c.get("phone") or ""})
    return out


# ──────────────────────────── preview ───────────────────────────────────────


@router.get("/compliance/preview")
def preview(
    request: Request,
    campaign_id: Optional[str] = None,
    limit: int = 1000,
) -> dict:
    """Run DNC + time-of-day against the campaign's contacts.

    Parameters
    ----------
    campaign_id : str, optional
        Required. Falls through to ``400`` if missing.
    limit : int
        Cap the contact-list scan (default 1000 — the API still returns
        total counts in case the user wants the full picture).

    Returns
    -------
    dict
        ``total`` (contact count) + ``will_dial`` + ``skipped_dnc`` +
        ``skipped_time`` + a small sample of the first skipped rows so
        the UI can show "why".
    """
    if not campaign_id:
        raise HTTPException(400, "campaign_id is required")
    if limit < 1 or limit > 5000:
        raise HTTPException(400, "limit must be 1..5000")
    tenant_id = _tenant_id(request)
    store = get_store()
    rows = _campaign_phones(tenant_id, campaign_id, store)
    if not rows:
        return {
            "campaign_id": campaign_id,
            "total": 0,
            "will_dial": 0,
            "skipped_dnc": 0,
            "skipped_time": 0,
            "skipped_total": 0,
            "dnc_enabled": False,
            "time_window_enabled": False,
            "time_window": (8, 21),
            "sample_skipped": [],
        }
    camp = store.get_campaign(tenant_id, campaign_id)
    dnc_enabled = bool(int(camp.get("dnc_check_enabled", 1) or 0))
    tw_enabled = bool(int(camp.get("time_window_enabled", 1) or 0))
    tw_start = int(camp.get("time_window_start", 8) or 8)
    tw_end = int(camp.get("time_window_end", 21) or 21)
    window = (tw_start, tw_end)

    phones = [r["phone"] for r in rows]
    dnc_results = bulk_check_dnc(phones) if dnc_enabled else {p: False for p in phones}
    tw_results = bulk_filter_window(phones, window) if tw_enabled else {p: True for p in phones}

    skipped_dnc = 0
    skipped_time = 0
    will_dial = 0
    sample: list[dict] = []
    for r in rows:
        phone = r["phone"]
        dnc_hit = bool(dnc_results.get(phone))
        in_window = bool(tw_results.get(phone))
        if dnc_hit:
            skipped_dnc += 1
            if len(sample) < 25:
                sample.append({
                    "contact_id": r["id"], "phone": phone, "reason": "dnc",
                })
            continue
        if not in_window:
            skipped_time += 1
            if len(sample) < 25:
                sample.append({
                    "contact_id": r["id"], "phone": phone, "reason": "time_window",
                    "timezone": get_timezone(phone),
                })
            continue
        will_dial += 1

    return {
        "campaign_id": campaign_id,
        "total": len(rows),
        "will_dial": will_dial,
        "skipped_dnc": skipped_dnc,
        "skipped_time": skipped_time,
        "skipped_total": skipped_dnc + skipped_time,
        "dnc_enabled": dnc_enabled,
        "time_window_enabled": tw_enabled,
        "time_window": list(window),
        "sample_skipped": sample,
    }


# ──────────────────────────── DNC ───────────────────────────────────────────


@router.get("/compliance/dnc")
def dnc_lookup(
    request: Request,
    phone: str,
    source: str = DEFAULT_SOURCE,
    force_refresh: int = 0,
) -> dict:
    """Look up a single phone's DNC status (cached for 30 days)."""
    if not phone:
        raise HTTPException(400, "phone is required")
    # Calling _tenant_id to ensure the auth middleware populated the
    # tenant context. We don't actually need the value here.
    _ = _tenant_id(request)
    is_dnc = check_dnc(phone, source=source, force_refresh=bool(int(force_refresh or 0)))
    row = get_cache_row(phone, source=source)
    return {
        "phone": phone,
        "source": source,
        "is_dnc": bool(is_dnc),
        "checked_at": (row or {}).get("checked_at"),
        "expires_at": (row or {}).get("expires_at"),
    }


@router.post("/compliance/dnc/refresh")
async def dnc_refresh(request: Request) -> dict:
    """Force-refresh a single DNC cache row. Body: ``{"phone": ...}``."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    phone = (body.get("phone") or "").strip()
    if not phone:
        raise HTTPException(400, "phone is required")
    source = (body.get("source") or DEFAULT_SOURCE).strip() or DEFAULT_SOURCE
    _ = _tenant_id(request)
    row = refresh_cache(phone, source=source)
    return {
        "ok": True,
        "phone": phone,
        "source": source,
        "is_dnc": bool(int((row or {}).get("is_dnc", 0) or 0)),
        "checked_at": (row or {}).get("checked_at"),
        "expires_at": (row or {}).get("expires_at"),
    }


@router.get("/compliance/stats")
def compliance_stats(request: Request) -> dict:
    """Rollup for the dashboard: cache size + DNC counts."""
    _ = _tenant_id(request)
    return cache_stats()


# ──────────────────────────── campaign-level compliance settings ────────────


@router.patch("/campaigns/{campaign_id}/compliance")
async def update_compliance(campaign_id: str, request: Request) -> dict:
    """Update the compliance fields on a campaign.

    Body keys: ``dnc_check_enabled`` (bool), ``time_window_enabled``
    (bool), ``time_window_start`` (int 0-23), ``time_window_end`` (int 1-24).
    All optional — only the keys you send are applied.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON body must be an object")
    fields: dict = {}
    if "dnc_check_enabled" in body:
        fields["dnc_check_enabled"] = 1 if bool(body["dnc_check_enabled"]) else 0
    if "time_window_enabled" in body:
        fields["time_window_enabled"] = 1 if bool(body["time_window_enabled"]) else 0
    if "time_window_start" in body:
        try:
            tw = int(body["time_window_start"])
        except (TypeError, ValueError):
            raise HTTPException(400, "time_window_start must be int")
        if not 0 <= tw <= 23:
            raise HTTPException(400, "time_window_start must be 0..23")
        fields["time_window_start"] = tw
    if "time_window_end" in body:
        try:
            tw = int(body["time_window_end"])
        except (TypeError, ValueError):
            raise HTTPException(400, "time_window_end must be int")
        if not 1 <= tw <= 24:
            raise HTTPException(400, "time_window_end must be 1..24")
        fields["time_window_end"] = tw
    if not fields:
        raise HTTPException(400, "no compliance fields supplied")
    tenant_id = _tenant_id(request)
    store = get_store()
    camp = store.get_campaign(tenant_id, campaign_id)
    if not camp:
        raise HTTPException(404, f"campaign {campaign_id} not found")
    if camp["status"] not in ("draft", "scheduled", "paused"):
        raise HTTPException(
            409,
            f"campaign in status '{camp['status']}' is not editable",
        )
    updated = store.update_campaign(tenant_id, campaign_id, **fields)
    return {"ok": True, "campaign": updated}
