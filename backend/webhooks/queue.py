"""Call queue API (Phase E-A, issue #32).

Endpoints
---------
POST /api/queue/enqueue              add a call to the queue
POST /api/queue/dequeue              pop the next call for the calling agent
GET  /api/queue/position/{call_id}   current position of a specific call
GET  /api/queue/stats                waiting / longest_wait / today counts

The queue is a simple FIFO with a ``priority`` tie-breaker and a
``skill_tags`` intersection against the agent's skill set. The
storage layer is in :mod:`webhooks.storage`; the route layer is
intentionally thin so the v1 surface is one file to read.

The webhook hookup for ``call.hangup`` lives in
:mod:`webhooks.handlers.default` and flips any ``queued`` /
``assigned`` row for that call to ``abandoned`` (or ``answered`` if
it was already assigned when the hangup arrived — the agent
actually picked up).
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response

from webhooks.storage import get_store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/queue", tags=["queue"])


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _tenant_id(request: Request) -> str:
    return getattr(request.state, "tenant_id", None) or "default"


def _current_user_id(request: Request) -> str:
    ctx = getattr(request.state, "tenant_ctx", None)
    if ctx and getattr(ctx, "user", None) and ctx.user.id:
        return ctx.user.id
    # No JWT (legacy header-only auth): fall back to the default admin
    # user for the dev tenant. Keeps the dashboard + tests usable.
    store = get_store()
    u = store.get_user(_tenant_id(request), "admin@default.local")
    if u:
        return u["id"]
    users = store.list_users(_tenant_id(request))
    if users:
        return users[0]["id"]
    raise HTTPException(401, "no user context and no users in tenant")


def _parse_skill_tags(raw) -> list:
    """Normalise the ``skill_tags`` body field.

    Accepts:
    * ``["sales", "billing"]`` (list of strings)
    * ``"sales,billing"`` (comma-separated string)
    * ``[]`` / None (no tags)
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(s).strip() for s in raw if isinstance(s, (str, int)) and str(s).strip()]
    if isinstance(raw, str):
        return [s.strip() for s in raw.split(",") if s.strip()]
    return []


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/enqueue")
async def enqueue(request: Request) -> dict:
    """Add a call to the queue.

    Body: ``{call_id, skill_tags?, priority?}``. ``call_id`` is the
    Telnyx ``call_control_id`` (or our synthetic call id when the
    campaign is in test mode). Idempotent — re-enqueueing the same
    call returns the existing row.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    call_id = (body.get("call_id") or "").strip()
    if not call_id:
        raise HTTPException(400, "call_id is required")
    skill_tags = _parse_skill_tags(body.get("skill_tags"))
    # Issue #40: a single primary ``skill`` (string) is merged into
    # skill_tags so the existing match logic continues to work without
    # a code change.
    primary_skill = (body.get("skill") or "").strip()
    if primary_skill and primary_skill not in skill_tags:
        skill_tags.append(primary_skill)
    try:
        priority = int(body.get("priority") or 0)
    except (TypeError, ValueError):
        priority = 0
    tid = _tenant_id(request)
    store = get_store()
    row = store.enqueue_call(
        tid, call_id,
        skill_tags=skill_tags,
        priority=priority,
        skill=primary_skill or None,
    )
    pos = store.get_queue_position(tid, call_id) or {"position": 1, "ahead": 0, "eta_s": 0}
    return {
        "ok": True,
        "queue_id": row["id"],
        "position": pos["position"],
        "eta_s": pos["eta_s"],
        "status": row["status"],
    }


@router.post("/dequeue")
def dequeue(request: Request) -> dict:
    """Pop the next call for the calling agent.

    The agent's skill set is fetched from storage and intersected
    with each queued call's ``skill_tags``. The head of the queue is
    returned; its row is flipped to ``assigned`` and
    ``assigned_user_id`` is set to the caller. Returns 410 if the
    popped row is in an unexpected state (defensive — shouldn't
    happen in v1 but keeps the contract honest).
    """
    tid = _tenant_id(request)
    uid = _current_user_id(request)
    store = get_store()
    user_skills = store.get_user_skills(tid, uid)
    row = store.dequeue_for_user(tid, uid, user_skills=user_skills)
    if not row:
        raise HTTPException(404, "no queued calls match this agent")
    # 410 is what the brief asks for "if abandoned" — but the
    # storage layer's dequeue only returns rows whose status was
    # 'queued' at lock time. The 410 path is left in for completeness
    # in case a parallel webhook flipped the row mid-call; in practice
    # it's a defensive 404.
    if row.get("status") not in ("assigned",):
        raise HTTPException(410, f"call in unexpected status: {row.get('status')}")
    tags = []
    try:
        tags = json.loads(row.get("skill_tags_json") or "[]")
    except json.JSONDecodeError:
        tags = []
    return {
        "ok": True,
        "call_id": row.get("call_id"),
        "queue_id": row.get("id"),
        "skill_tags": tags,
        "enqueued_at": row.get("enqueued_at"),
        "position_was": 1,  # the head — by construction
    }


@router.get("/position/{call_id}")
def position(call_id: str, request: Request) -> dict:
    """Return ``{position, ahead, eta_s}`` for ``call_id``.

    404 if the call isn't currently queued.
    """
    tid = _tenant_id(request)
    store = get_store()
    pos = store.get_queue_position(tid, call_id)
    if not pos:
        raise HTTPException(404, f"call {call_id!r} is not queued")
    return {"ok": True, "call_id": call_id, **pos}


@router.get("/stats")
def stats(request: Request, skill: Optional[str] = None) -> dict:
    """Return ``{waiting, longest_wait_s, abandoned_today, answered_today}``.

    Optional ``?skill=sales`` filter — only counts rows whose
    ``skill_tags`` contains the skill. Issue #40.
    """
    tid = _tenant_id(request)
    store = get_store()
    return {"ok": True, "skill": skill, **store.get_queue_stats(tid, skill=skill)}


@router.get("/list")
def list_q(
    request: Request,
    status: Optional[str] = None,
    skill: Optional[str] = None,
) -> dict:
    """List queue rows for the tenant (debug + dashboard widget).

    Optional ``?status=queued|assigned|answered|abandoned`` filter and
    ``?skill=sales`` filter (issue #40).
    """
    tid = _tenant_id(request)
    store = get_store()
    rows = store.list_queue(tid, status=status, skill=skill)
    # Decode skill_tags JSON for the response.
    for r in rows:
        try:
            r["skill_tags"] = json.loads(r.get("skill_tags_json") or "[]")
        except json.JSONDecodeError:
            r["skill_tags"] = []
    return {"ok": True, "count": len(rows), "skill": skill, "items": rows}
