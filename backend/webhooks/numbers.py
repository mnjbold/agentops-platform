"""Phone number provisioning (issue #15).

Endpoints
---------
GET    /api/numbers/available?area_code=512&country=US&has_voice=1&has_mms=1
                                             search Telnyx inventory
POST   /api/numbers/buy                      body: {phone_number, connection_id?,
                                                 billing_group_id?, messaging_profile_id?}
                                             actually purchase a number
GET    /api/numbers                          list owned numbers + current assignment
GET    /api/numbers/{id}                     one number + assignment history
PATCH  /api/numbers/{id}/assignment          body: {kind, target_id}
DELETE /api/numbers/{id}                     release a number

All write paths write a row to ``number_assignments`` for audit. The
``assignment`` PATCH is the *single* mutator for a number's routing.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request

from telnyx_mcp.clients.telnyx_client import get_client

from webhooks.storage import get_store
from webhooks._phase_b_ctx import _tenant_id

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["numbers"])


# Map the URL-side feature filter (?has_voice=1) to the Telnyx
# available-numbers ``features`` array.
_FEATURE_FLAGS = ("voice", "sms", "mms", "fax")


def _features_from_query(params: dict) -> list[str]:
    feats: list[str] = []
    for f in _FEATURE_FLAGS:
        if str(params.get(f"has_{f}", "")).lower() in ("1", "true", "yes"):
            feats.append(f)
    return feats


@router.get("/numbers/available")
def search_available(
    request: Request,
    area_code: Optional[str] = None,
    country: str = "US",
    locality: Optional[str] = None,
    administrative_area: Optional[str] = None,
    has_voice: Optional[str] = None,
    has_sms: Optional[str] = None,
    has_mms: Optional[str] = None,
    has_fax: Optional[str] = None,
    limit: int = 10,
) -> dict:
    """Proxy Telnyx's number search.

    Any HTTP error from Telnyx bubbles up as 502 so the UI can show a
    real message instead of a generic 500.
    """
    _ = _tenant_id(request)  # ensure auth (mirrors admin_api pattern)
    features = _features_from_query({
        "has_voice": has_voice, "has_sms": has_sms,
        "has_mms": has_mms, "has_fax": has_fax,
    })
    try:
        c = get_client()
        rows = c.search_available_numbers(
            country_code=country,
            area_code=area_code,
            locality=locality,
            administrative_area=administrative_area,
            features=features or None,
            limit=max(1, min(int(limit), 50)),
        )
    except Exception as e:
        log.warning("Telnyx number search failed: %s", e)
        raise HTTPException(502, f"Telnyx search failed: {e}")
    return {
        "available": [
            {
                "phone_number": r.get("phone_number"),
                "country_code": r.get("country_iso_alpha2") or country,
                "region": r.get("region") or administrative_area,
                "locality": r.get("locality"),
                "features": r.get("features") or [],
                "monthly_cost": r.get("monthly_cost"),
                "per_minute_rate": (
                    r.get("cost_information", {}).get("minute_cost")
                    if isinstance(r.get("cost_information"), dict) else None
                ),
                "best_value": r.get("best_value"),
            }
            for r in rows
        ],
        "count": len(rows),
    }


@router.post("/numbers/buy")
async def buy_number(request: Request) -> dict:
    """Purchase a number via Telnyx + register it locally.

    Body: ``{phone_number, connection_id?, billing_group_id?,
    messaging_profile_id?}``. The local row is upserted on success so
    the next ``/api/numbers`` list call sees the new number within a
    second (we don't poll Telnyx after the order).
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    phone = (body.get("phone_number") or "").strip()
    if not phone:
        raise HTTPException(400, "phone_number is required")
    try:
        c = get_client()
        order = c.order_numbers([phone])
    except Exception as e:
        log.warning("Telnyx order failed: %s", e)
        raise HTTPException(502, f"Telnyx order failed: {e}")
    # Apply the connection / billing attributes if the caller asked.
    try:
        c.update_number(
            phone,
            connection_id=body.get("connection_id"),
            billing_group_id=body.get("billing_group_id"),
            messaging_profile_id=body.get("messaging_profile_id"),
        )
    except Exception as e:
        log.warning("Telnyx update_number after buy failed: %s", e)
    store = get_store()
    row = store.upsert_phone_number(
        _tenant_id(request),
        phone,
        telnyx_id=phone,
        country_code=body.get("country_code") or "US",
    )
    return {"ok": True, "order": order, "number": row}


@router.get("/numbers")
def list_owned(request: Request) -> dict:
    """List the tenant's owned numbers with their current assignment.

    Numbers come from the local ``phone_numbers`` table; the
    assignment columns are denormalised there.
    """
    store = get_store()
    rows = store.list_phone_numbers(_tenant_id(request))
    return {"numbers": rows, "count": len(rows)}


@router.get("/numbers/{number_id}")
def get_number(number_id: str, request: Request) -> dict:
    store = get_store()
    tenant_id = _tenant_id(request)
    n = store.get_phone_number(tenant_id, number_id)
    if not n:
        raise HTTPException(404, f"number {number_id} not found")
    history = store.list_number_assignments(tenant_id, number_id)
    return {"number": n, "assignments": history}


@router.patch("/numbers/{number_id}/assignment")
async def update_assignment(number_id: str, request: Request) -> dict:
    """Change a number's routing.

    Body: ``{kind: "workflow"|"assistant"|"direct"|null,
    target_id?: "wf_..."|"ast_..."|"inbox"}``. Passing ``null`` clears
    the assignment (the number still exists; the inbound path goes to
    the default handler).
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    kind = body.get("kind")
    target_id = body.get("target_id")
    if kind is not None and kind not in ("workflow", "assistant", "direct"):
        raise HTTPException(400, "kind must be one of: workflow, assistant, direct, null")
    if kind in ("workflow", "assistant") and not target_id:
        raise HTTPException(400, f"target_id is required when kind={kind}")
    store = get_store()
    n = store.set_number_assignment(
        _tenant_id(request), number_id, kind, target_id)
    if n is None:
        raise HTTPException(404, f"number {number_id} not found")
    return {"ok": True, "number": n}


@router.delete("/numbers/{number_id}")
def release_number(number_id: str, request: Request) -> dict:
    """Release a number back to Telnyx + drop the local row.

    Note: the Telnyx SDK v4 doesn't expose a ``phone_numbers.delete``
    for purchased numbers; the standard path is to call
    ``POST /v2/number_orders`` with a 'release' sub-action, but the SDK
    v4 doesn't have a clean helper for that yet. We delete the local
    row (the source of truth for the dashboard) and log a TODO so the
    operator knows to confirm the release in the Telnyx portal.
    """
    store = get_store()
    tenant_id = _tenant_id(request)
    n = store.get_phone_number(tenant_id, number_id)
    if not n:
        raise HTTPException(404, f"number {number_id} not found")
    log.warning(
        "Number release: local row deleted for %s; the operator must "
        "confirm the release in the Telnyx portal (SDK v4 has no "
        "clean release helper).", n["phone_number"],
    )
    ok = store.delete_phone_number(tenant_id, number_id)
    return {"ok": ok, "id": number_id, "phone_number": n["phone_number"]}
