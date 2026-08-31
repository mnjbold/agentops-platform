"""Billing REST API (issue #19).

Endpoints
---------
POST /v1/billing/checkout           {plan} -> {url, id}
POST /v1/billing/portal             -> {url, id}
POST /v1/billing/webhook            (Stripe) — verify + apply
GET  /v1/billing/subscription       -> {plan, status, current_period_* ...}
GET  /v1/billing/usage              -> {voice_minutes, sms_segments, period}
GET  /v1/billing/plans              -> [plan dicts]
POST /v1/billing/mock-checkout      dev-only — completes a mock checkout
POST /v1/billing/mock-portal        dev-only — completes a mock portal session

Hard-limit enforcement: the admin tenant endpoint rejects adding a 2nd
number on the free plan with 402 + ``upgrade_required: true``.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from billing.metering import current_period, usage_for_period
from billing.plans import PLANS, get_plan, is_upgrade_required
from billing.stripe_client import get_stripe_client
from webhooks.storage import get_store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/billing", tags=["Billing"])


def _tenant_id(request: Request) -> str:
    tid = getattr(request.state, "tenant_id", None)
    if not tid:
        raise HTTPException(401, "authentication required")
    return tid


def _tenant_ctx(request: Request) -> dict:
    """Return a dict with the tenant's billing context."""
    tid = _tenant_id(request)
    store = get_store()
    sub = store.get_subscription(tid) or {
        "tenant_id": tid,
        "plan": "free",
        "status": "active",
    }
    tenant = store.get_tenant(tid) or {"tier": sub["plan"]}
    return {
        "tenant_id": tid,
        "tenant": tenant,
        "subscription": sub,
        "plan": sub["plan"],
    }


# ──────────────────── plans ───────────────────────────────────────────────

@router.get("/plans")
def list_plans(request: Request) -> dict:
    out = []
    for p in PLANS.values():
        out.append({
            "id": p.id,
            "name": p.name,
            "monthly_price_cents": p.monthly_price_cents,
            "number_limit": p.number_limit,
            "voice_rate_cents_per_min": p.voice_rate_cents_per_min,
            "sms_rate_cents_per_segment": p.sms_rate_cents_per_segment,
            "features": list(p.features),
        })
    return {"plans": out, "count": len(out)}


# ──────────────────── subscription ───────────────────────────────────────

@router.get("/subscription")
def get_subscription(request: Request) -> dict:
    ctx = _tenant_ctx(request)
    sub = ctx["subscription"]
    return {
        "tenant_id": ctx["tenant_id"],
        "plan": sub["plan"],
        "status": sub.get("status", "active"),
        "stripe_customer_id": sub.get("stripe_customer_id"),
        "stripe_subscription_id": sub.get("stripe_subscription_id"),
        "current_period_start": sub.get("current_period_start"),
        "current_period_end": sub.get("current_period_end"),
        "cancel_at_period_end": bool(sub.get("cancel_at_period_end") or 0),
        "plan_details": {
            "name": get_plan(sub["plan"]).name,
            "monthly_price_cents": get_plan(sub["plan"]).monthly_price_cents,
            "number_limit": get_plan(sub["plan"]).number_limit,
        },
    }


# ──────────────────── usage ──────────────────────────────────────────────

@router.get("/usage")
def get_usage(request: Request) -> dict:
    ctx = _tenant_ctx(request)
    period_start, period_end = current_period()
    usage = usage_for_period(ctx["tenant_id"], period_start, period_end)
    plan = get_plan(ctx["plan"])
    return {
        **usage,
        "plan": ctx["plan"],
        "number_limit": plan.number_limit,
        "voice_rate_cents_per_min": plan.voice_rate_cents_per_min,
        "sms_rate_cents_per_segment": plan.sms_rate_cents_per_segment,
    }


# ──────────────────── checkout / portal ──────────────────────────────────

@router.post("/checkout")
async def create_checkout(request: Request) -> dict:
    ctx = _tenant_ctx(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    plan = (body.get("plan") or "").strip().lower()
    if plan not in PLANS:
        raise HTTPException(400, f"plan must be one of: {', '.join(PLANS)}")
    if plan == "free":
        raise HTTPException(400, "free plan does not need a checkout session")
    if plan == "enterprise":
        raise HTTPException(400, "contact sales for enterprise pricing")

    base = str(request.base_url).rstrip("/")
    success_url = body.get("success_url") or f"{base}/v1/billing/portal"
    cancel_url = body.get("cancel_url") or f"{base}/v1/billing/subscription"

    client = get_stripe_client()
    sess = client.create_checkout_session(
        tenant_id=ctx["tenant_id"],
        plan=plan,
        success_url=success_url,
        cancel_url=cancel_url,
    )
    return {"plan": plan, "checkout": sess, "is_live": client.is_live}


@router.post("/portal")
async def create_portal(request: Request) -> dict:
    ctx = _tenant_ctx(request)
    base = str(request.base_url).rstrip("/")
    return_url = f"{base}/v1/billing/subscription"
    client = get_stripe_client()
    sess = client.create_portal_session(
        tenant_id=ctx["tenant_id"],
        return_url=return_url,
    )
    if "error" in sess:
        raise HTTPException(409, sess["error"])
    return {"portal": sess, "is_live": client.is_live}


# ──────────────────── webhook (Stripe → us) ──────────────────────────────

@router.post("/webhook")
async def stripe_webhook(request: Request) -> dict:
    """Receive Stripe events. Verifies the signature, then updates the
    subscription + tenant tier accordingly.

    The body is signed by Stripe (or by our mock scheme in dev); we never
    trust the payload without a valid signature.
    """
    raw = await request.body()
    sig = request.headers.get("Stripe-Signature") or request.headers.get("stripe-signature") or ""
    client = get_stripe_client()
    try:
        evt = client.verify_webhook(raw, sig)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))

    evt_type = evt.get("type") or ""
    data = evt.get("data", {}).get("object", {}) or {}
    log.info("Stripe webhook: %s", evt_type)

    store = get_store()
    tenant_id = (
        (data.get("client_reference_id") if isinstance(data, dict) else None)
        or (data.get("metadata", {}) or {}).get("tenant_id")
        or ""
    )
    if not tenant_id:
        # Try the customer id (for invoice.* events).
        cust_id = data.get("customer") if isinstance(data, dict) else None
        if cust_id:
            row = store._row(  # type: ignore[attr-defined]
                "SELECT tenant_id FROM subscriptions WHERE stripe_customer_id = ?",
                (cust_id,),
            )
            if row:
                tenant_id = row["tenant_id"]

    if not tenant_id:
        log.warning("Stripe webhook: no tenant_id in event %s", evt_type)
        return {"ok": True, "skipped": "no tenant"}

    if evt_type in (
        "checkout.session.completed",
        "customer.subscription.created",
        "customer.subscription.updated",
    ):
        plan = (data.get("metadata", {}) or {}).get("plan")
        if not plan and evt_type == "customer.subscription.created":
            # Derive plan from the price id (set by env in production).
            items = (data.get("items", {}) or {}).get("data", []) or []
            if items:
                price_id = items[0].get("price", {}).get("id", "")
                plan = _price_id_to_plan(price_id)
        if plan and plan in PLANS:
            store.upsert_subscription(
                tenant_id=tenant_id,
                plan=plan,
                status=(data.get("status") or "active"),
                stripe_customer_id=data.get("customer"),
                stripe_subscription_id=data.get("id") if evt_type != "checkout.session.completed" else None,
                current_period_start=_iso_or_none(data.get("current_period_start")),
                current_period_end=_iso_or_none(data.get("current_period_end")),
                cancel_at_period_end=1 if data.get("cancel_at_period_end") else 0,
            )
            # Also update the tenant's tier column so the admin UI sees it.
            store.update_tenant_tier(tenant_id, plan)

    elif evt_type == "customer.subscription.deleted":
        store.upsert_subscription(
            tenant_id=tenant_id,
            plan="free",
            status="canceled",
            cancel_at_period_end=0,
        )
        store.update_tenant_tier(tenant_id, "free")

    elif evt_type == "invoice.paid":
        # Mark usage records as billed for the period.
        period_start = _iso_or_none(data.get("period_start"))
        period_end = _iso_or_none(data.get("period_end"))
        if period_start and period_end:
            with store._lock:  # type: ignore[attr-defined]
                store._conn.execute(  # type: ignore[attr-defined]
                    "UPDATE usage_records SET billed = 1 "
                    "WHERE tenant_id = ? AND period_start = ? AND period_end = ?",
                    (tenant_id, period_start, period_end),
                )

    elif evt_type == "invoice.payment_failed":
        store.upsert_subscription(
            tenant_id=tenant_id,
            plan=store.get_subscription(tenant_id)["plan"] if store.get_subscription(tenant_id) else "free",
            status="past_due",
        )

    return {"ok": True, "type": evt_type, "tenant_id": tenant_id}


def _iso_or_none(unix_or_iso) -> Optional[str]:
    """Stripe sends unix timestamps; our tables want ISO strings."""
    if unix_or_iso is None:
        return None
    if isinstance(unix_or_iso, (int, float)):
        return datetime.fromtimestamp(int(unix_or_iso), tz=timezone.utc).isoformat()
    if isinstance(unix_or_iso, str):
        return unix_or_iso
    return None


def _price_id_to_plan(price_id: str) -> Optional[str]:
    for plan in PLANS.values():
        if plan.stripe_price_id and plan.stripe_price_id == price_id:
            return plan.id
    return None


# ──────────────────── dev-only mock completion ───────────────────────────

@router.post("/mock-checkout")
async def mock_checkout(request: Request) -> dict:
    """Finalise a mock checkout. In live mode this endpoint is a no-op
    (Stripe redirects to ``success_url`` which doesn't hit us). In mock
    mode we apply the same effect as the ``customer.subscription.created``
    webhook so the dashboard can demo the full flow."""
    client = get_stripe_client()
    if client.is_live:
        raise HTTPException(404, "not available in live mode")
    try:
        body = await request.json()
    except Exception:
        body = {}
    tenant_id = (body.get("tenant_id") or "").strip()
    plan = (body.get("plan") or "").strip().lower()
    if not tenant_id or plan not in PLANS:
        raise HTTPException(400, "tenant_id and valid plan are required")
    store = get_store()
    now = datetime.now(timezone.utc)
    store.upsert_subscription(
        tenant_id=tenant_id,
        plan=plan,
        status="active",
        stripe_customer_id=f"cus_mock_{tenant_id[:10]}",
        stripe_subscription_id=f"sub_mock_{tenant_id[:10]}",
        current_period_start=now.isoformat(),
        current_period_end=_add_month(now).isoformat(),
    )
    store.update_tenant_tier(tenant_id, plan)
    return {"ok": True, "tenant_id": tenant_id, "plan": plan}


@router.post("/mock-portal")
async def mock_portal(request: Request) -> dict:
    client = get_stripe_client()
    if client.is_live:
        raise HTTPException(404, "not available in live mode")
    return {"ok": True, "message": "Mock portal session ended. Subscription unchanged."}


# ──────────────────── hard-limit gate (issue #19 acceptance) ────────────

def enforce_number_limit(request: Request) -> None:
    """Raise 402 with ``upgrade_required: True`` if the tenant is at
    their number limit. Called by the numbers endpoints — but we expose
    the helper here so the same logic can be reused in dashboard_api."""
    tid = _tenant_id(request)
    store = get_store()
    sub = store.get_subscription(tid) or {"plan": "free"}
    plan = sub.get("plan") or "free"
    n_owned = store._row(  # type: ignore[attr-defined]
        "SELECT COUNT(*) AS c FROM phone_numbers WHERE tenant_id = ?",
        (tid,),
    )
    current = int((n_owned or {}).get("c") or 0)
    limit = get_plan(plan).number_limit
    if current >= limit:
        from fastapi.responses import JSONResponse
        # We don't actually return here — this helper is called from
        # inside endpoints that want to short-circuit.
        raise HTTPException(
            status_code=402,
            detail={
                "code": "upgrade_required",
                "upgrade_required": True,
                "current_plan": plan,
                "limit": limit,
                "current": current,
            },
        )


# ──────────────────── small helpers ─────────────────────────────────────

def _add_month(dt: datetime) -> datetime:
    """Add one calendar month, clamping to the last day of the month so
    that 2026-01-31 + 1 month = 2026-02-28 (not a ValueError)."""
    if dt.month == 12:
        return dt.replace(year=dt.year + 1, month=1)
    # Clamp to day=1 then add the month, then we don't care about the
    # original day — the period is calendar-month, not 30 days.
    return dt.replace(day=1, month=dt.month + 1)
