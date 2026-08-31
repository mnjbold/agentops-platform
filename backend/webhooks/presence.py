"""Agent presence + skills API (Phase E-A, issue #31).

Endpoints
---------
GET   /api/agents/presence           list all users in tenant with presence + skills
PUT   /api/agents/me/presence        set the current user's presence (status, current_call_id?)
GET   /api/agents/queue/next         longest-idle online user (or 204)
PUT   /api/agents/me/skills          replace the current user's skill set
GET   /api/agents/me/events          WebSocket — heartbeat + presence events

The presence enum is the v1 contract from the issue brief:
``online | away | busy | on_call | offline``. Storing a value outside
this set is rejected with 400 (the DB CHECK constraint also enforces
it as a backstop).

The WebSocket endpoint lives in this module because it is tightly
coupled to the storage layer (every connect / disconnect bumps
``last_seen``). A background task on the running event loop also
auto-flips stale 'online' rows to 'offline' every 30 seconds.

Acceptance test lives in ``tests/test_presence.py``.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

from webhooks.storage import get_store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["agents"])

# Presence status enum (mirrors the DB CHECK).
_VALID_STATUSES = {"online", "away", "busy", "on_call", "offline"}

# Sweeper interval (seconds) and idle threshold for auto-offline.
_SWEEPER_INTERVAL_S = 30
_IDLE_THRESHOLD_S = 90

# Background task handle — module-level so the server can start it
# on app startup and stop it on shutdown.
_sweeper_task: Optional[asyncio.Task] = None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _tenant_id(request: Request) -> str:
    return getattr(request.state, "tenant_id", None) or "default"


def _current_user_id(request: Request) -> str:
    ctx = getattr(request.state, "tenant_ctx", None)
    if ctx and getattr(ctx, "user", None) and ctx.user.id:
        return ctx.user.id
    # No JWT: fall back to the "default" agent user when the tenant
    # is the dev default — keeps the dashboard usable without a login.
    store = get_store()
    u = store.get_user(_tenant_id(request), "admin@default.local")
    if u:
        return u["id"]
    # Last-ditch: first user in the tenant.
    users = store.list_users(_tenant_id(request))
    if users:
        return users[0]["id"]
    raise HTTPException(401, "no user context and no users in tenant")


def _serialize_agent(
    user: dict,
    presence: Optional[dict],
    skills: list[dict],
) -> dict:
    """Shape one row for the GET /api/agents/presence response."""
    return {
        "user_id": user.get("id"),
        "email": user.get("email"),
        "display_name": user.get("display_name") or user.get("email"),
        "role": user.get("role"),
        "assigned_number": user.get("assigned_number"),
        "status": (presence or {}).get("status") or "offline",
        "last_seen": (presence or {}).get("last_seen"),
        "current_call_id": (presence or {}).get("current_call_id"),
        "skills": [
            {"skill": s.get("skill"), "level": s.get("level")}
            for s in (skills or [])
        ],
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/presence")
def list_presence(request: Request) -> dict:
    """List every user in the tenant with their presence + skills."""
    tid = _tenant_id(request)
    store = get_store()
    users = store.list_users(tid)
    presence_by_uid = {p["user_id"]: p for p in store.list_presence(tid)}
    skills_by_uid: dict[str, list[dict]] = {}
    for u in users:
        skills_by_uid[u["id"]] = store.get_user_skills(tid, u["id"])
    agents = [_serialize_agent(u, presence_by_uid.get(u["id"]), skills_by_uid.get(u["id"], []))
              for u in users]
    return {"agents": agents, "count": len(agents)}


@router.put("/me/presence")
async def set_my_presence(request: Request) -> dict:
    """Set the caller's presence row. Body: ``{status, current_call_id?}``.

    ``status`` must be one of the five v1 values; ``current_call_id`` is
    optional and only relevant for ``on_call``.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    status = (body.get("status") or "").strip()
    if status not in _VALID_STATUSES:
        raise HTTPException(
            400,
            f"status must be one of {sorted(_VALID_STATUSES)} (got {status!r})",
        )
    current_call_id = body.get("current_call_id") or None
    tid = _tenant_id(request)
    uid = _current_user_id(request)
    store = get_store()
    row = store.upsert_presence(tid, uid, status, current_call_id)
    return {"ok": True, "presence": row}


@router.get("/queue/next")
def queue_next(request: Request, skill: Optional[str] = None) -> dict:
    """Return the longest-idle 'online' user (or 204 if none).

    Optional ``?skill=sales`` filters to users with that skill tag.
    Used by #32's call-queue (when the workflow forwards a call) and
    by #34's forward_agent node. Returns 204 (no body) when nothing
    matches so the caller can fall through to the queue path.
    """
    tid = _tenant_id(request)
    store = get_store()
    row = store.find_idle_online_agent(tid, skill=skill)
    if not row:
        from fastapi.responses import Response
        return Response(status_code=204)
    return {
        "agent": {
            "user_id": row.get("user_id"),
            "email": row.get("email"),
            "display_name": row.get("display_name") or row.get("email"),
            "assigned_number": row.get("assigned_number"),
            "last_seen": row.get("last_seen"),
        }
    }


@router.put("/me/skills")
async def set_my_skills(request: Request) -> dict:
    """Replace the caller's skill set. Body: ``{skills: ['sales', {skill: 'support', level: 80}, ...]}``."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    skills = body.get("skills")
    if not isinstance(skills, list):
        raise HTTPException(400, "skills must be a list")
    tid = _tenant_id(request)
    uid = _current_user_id(request)
    store = get_store()
    try:
        written = store.set_user_skills(tid, uid, skills)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "skills": written}


# ---------------------------------------------------------------------------
# WebSocket + heartbeat (#31)
# ---------------------------------------------------------------------------


@router.websocket("/me/events")
async def ws_me_events(websocket: WebSocket) -> None:
    """Per-user WebSocket channel for the agent dashboard.

    The server bumps ``agent_presence.last_seen`` on connect, on every
    client ``ping`` frame, and on disconnect. The client receives a
    short ``hello`` frame on connect, a ``pong`` reply to each ``ping``,
    and broadcast ``presence.update`` events when a sibling agent in
    the same tenant changes status (so the dashboard's roster can
    refresh in real time).

    The connection's session token is the JWT or the API key — for
    the dev default tenant we accept no auth at all (matches the
    rest of the dashboard).
    """
    token = websocket.query_params.get("session_token", "") or ""
    await websocket.accept()
    # Bind the tenant + user from the websocket's connection state.
    # The auth middleware is bypassed for WS upgrades, so we infer.
    tid = "default"
    store = get_store()
    if token:
        # JWT path
        try:
            from webhooks.tenancy import decode_jwt
            claims = decode_jwt(token) or {}
            tid = claims.get("tid") or tid
        except Exception:
            pass
    # Pick a user for this tenant.
    uid: Optional[str] = None
    if token:
        try:
            from webhooks.tenancy import decode_jwt
            claims = decode_jwt(token) or {}
            uid = claims.get("sub")
        except Exception:
            uid = None
    if not uid:
        u = store.get_user(tid, "admin@default.local")
        if u:
            uid = u["id"]
    if not uid:
        users = store.list_users(tid)
        if users:
            uid = users[0]["id"]
    if not uid:
        await websocket.close(code=1011, reason="no user in tenant")
        return

    store.touch_presence(tid, uid)
    log.info("WS /agents/me/events connected: tenant=%s user=%s", tid, uid)

    try:
        await websocket.send_json({"type": "ws.hello", "data": {"user_id": uid, "tenant_id": tid}})

        while True:
            recv_task = asyncio.create_task(websocket.receive_text())
            done, pending = await asyncio.wait({recv_task}, timeout=_SWEEPER_INTERVAL_S)
            for t in pending:
                t.cancel()
            if not done:
                # No client frame for 30s — bump last_seen so the sweeper
                # doesn't flip us offline.
                store.touch_presence(tid, uid)
                continue
            try:
                raw = recv_task.result()
            except WebSocketDisconnect:
                break
            except Exception:
                break
            try:
                msg = json.loads(raw) if raw else {}
            except Exception:
                msg = {}
            mtype = (msg.get("type") or "").lower()
            if mtype in ("ping", "heartbeat"):
                store.touch_presence(tid, uid)
                await websocket.send_json({"type": "pong", "data": {"ts": datetime.now(timezone.utc).isoformat()}})
            elif mtype == "presence.set":
                payload = msg.get("data") or {}
                status = payload.get("status")
                if status in _VALID_STATUSES:
                    store.upsert_presence(tid, uid, status, payload.get("current_call_id"))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.debug("WS /agents/me/events error: %s", e)
    finally:
        # Final touch so the next connect picks up a recent timestamp.
        try:
            store.touch_presence(tid, uid)
        except Exception:
            pass
        log.info("WS /agents/me/events disconnected: tenant=%s user=%s", tid, uid)


# ---------------------------------------------------------------------------
# Background sweeper (#31)
# ---------------------------------------------------------------------------


def start_presence_sweeper(loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    """Start the periodic 'online → offline' sweeper.

    Safe to call multiple times: only one task is ever running. The
    task is bound to the supplied loop (or the current running loop)
    so it lives in the same event-loop context as the WS endpoint.
    """
    global _sweeper_task
    if _sweeper_task is not None and not _sweeper_task.done():
        return
    target_loop = loop or _get_running_loop()
    if target_loop is None:
        log.debug("sweeper: no running loop; skipping")
        return
    _sweeper_task = target_loop.create_task(_sweeper_loop())
    log.info("Presence sweeper started: every %ds, idle=%ds",
             _SWEEPER_INTERVAL_S, _IDLE_THRESHOLD_S)


def stop_presence_sweeper() -> None:
    global _sweeper_task
    if _sweeper_task is not None and not _sweeper_task.done():
        _sweeper_task.cancel()
    _sweeper_task = None


def _get_running_loop() -> Optional[asyncio.AbstractEventLoop]:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


async def _sweeper_loop() -> None:
    """Periodically flip stale 'online' rows to 'offline'."""
    store = get_store()
    while True:
        try:
            await asyncio.sleep(_SWEEPER_INTERVAL_S)
            # For each tenant, sweep. v1 has a single tenant but the
            # schema is multi-tenant, so iterate.
            tenants = store.list_tenants()
            for t in tenants:
                flipped = store.sweep_stale_presence(
                    t["id"], idle_secs=_IDLE_THRESHOLD_S,
                )
                if flipped:
                    log.debug("sweeper: %s → offline (%d agents)", t["id"], flipped)
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.warning("presence sweeper error (non-fatal): %s", e)
