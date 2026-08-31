"""Supervisor audio-routing API (Phase E-B, issues #41, #42, #43).

Endpoints
---------
POST /api/calls/{call_id}/supervisor/monitor  silent-listen to a call (#41)
POST /api/calls/{call_id}/supervisor/whisper  coach the agent, agent can hear (#42)
POST /api/calls/{call_id}/supervisor/barge    3-way conference (#43)
GET  /api/calls/{call_id}/supervisor          list active + recent sessions
POST /api/supervisor/sessions/{id}/end        close one session
POST /api/supervisor/sessions/end_for_call    close every open session on a call

What this DOES
--------------
- Persists a ``supervisor_sessions`` row (storage.py) for each
  monitor / whisper / barge so the dashboard's listening panel and the
  agent's call view agree on who's on the call.
- Appends a ``participants`` JSON entry to the call record with
  ``role=<mode>``. The Phase B call record lives in Appwrite, but for
  v1 we expose a derived list on the GET endpoint so the UI's contract
  is stable.

What this does NOT (yet)
------------------------
- Real Telnyx audio routing. ``telnyx_client.call_actions.send_sip_info``
  is the right SDK call for silent monitor (issue #41), but the actual
  whisper / barge audio plumbing is provider-version-dependent
  (see #42 / #43) and ships in v1.1. The storage + UI are the v1
  contract; the audio path is best-effort and never blocks the API.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from webhooks.storage import get_store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["supervisor"])


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _tenant_id(request: Request) -> str:
    return getattr(request.state, "tenant_id", None) or "default"


def _ctx(request: Request) -> Optional[dict]:
    return getattr(request.state, "tenant_ctx", None)


def _require_supervisor_role(request: Request) -> dict:
    """Reject the request unless the caller's user has role in
    ``{supervisor, admin}``. Returns the UserContext dict on success.

    The middleware populates ``request.state.tenant_ctx.user`` when a
    JWT is present. API-key-only requests (no JWT) fall through with an
    empty dict — those are treated as 'no role' and 403'd. This matches
    the Phase B convention for sensitive write endpoints.
    """
    ctx = _ctx(request)
    user = getattr(ctx, "user", None) if ctx else None
    role = (user.role or "").lower() if user else ""
    if role not in ("supervisor", "admin"):
        raise HTTPException(
            403,
            f"supervisor role required (got role={role or 'none'!r})",
        )
    return user.__dict__ if user else {}


def _best_effort_audio_route(call_id: str, mode: str, user_id: str) -> dict:
    """Attempt the provider-side audio path. Always returns a dict; never
    raises. v1 contract is the storage row; the audio plumbing is v1.1.

    For #41 (monitor): try ``telnyx_client.call_actions.send_sip_info``
    which is the SDK path closest to a silent-monitor SIP INFO. The
    method isn't on the client today, so we no-op + log; future-proof
    so a future v1.1 swap is a one-line addition.
    """
    try:
        from telnyx_mcp.clients.telnyx_client import get_client
        client = get_client()
        actions = getattr(client, "call_actions", None)
        send = getattr(actions, "send_sip_info", None) if actions else None
        if send is not None:
            try:
                return {"ok": True, "audio_routed": True,
                        "result": send(call_id, f"x-supervisor-mode={mode}")}
            except Exception as e:  # pragma: no cover - network
                log.debug("send_sip_info raised (non-fatal): %s", e)
        # v1: provider-specific audio plumbing is not yet wired.
        return {"ok": True, "audio_routed": False,
                "note": "telnyx_client.call_actions.send_sip_info not present; v1 ships storage only"}
    except Exception as e:
        log.debug("audio routing path failed (non-fatal): %s", e)
        return {"ok": True, "audio_routed": False, "error": str(e)}


def _derived_participants(
    store, tenant_id: str, call_id: str,
) -> list[dict]:
    """Build the participants list the issue brief asks for. One entry
    per active supervisor session on the call."""
    out: list[dict] = []
    for s in store.list_active_supervisor_sessions(tenant_id, call_id):
        out.append({
            "user_id": s.get("supervisor_user_id"),
            "role": s.get("mode"),  # monitor | whisper | barge
            "session_id": s.get("id"),
            "joined_at": s.get("joined_at"),
        })
    return out


# ---------------------------------------------------------------------------
# POST endpoints
# ---------------------------------------------------------------------------


@router.post("/calls/{call_id}/supervisor/monitor")
async def start_monitor(call_id: str, request: Request) -> dict:
    """#41 — silent-listen. Body: ``{supervisor_user_id}``.

    Records a new monitor session and attempts the (provider-version-
    dependent) SIP-INFO audio path. The agent's presence is NOT
    flipped — monitor is a read-only audio leg.
    """
    _require_supervisor_role(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    sup = (body.get("supervisor_user_id") or "").strip()
    if not sup:
        raise HTTPException(400, "supervisor_user_id is required")
    tid = _tenant_id(request)
    store = get_store()
    if not store.get_user_by_id(sup):
        raise HTTPException(400, f"supervisor_user_id {sup!r} not found")
    row = store.start_supervisor_session(
        tid, call_id, supervisor_user_id=sup, mode="monitor",
    )
    audio = _best_effort_audio_route(call_id, "monitor", sup)
    return {
        "ok": True,
        "mode": "monitor",
        "session": row,
        "participants": _derived_participants(store, tid, call_id),
        "audio": audio,
    }


@router.post("/calls/{call_id}/supervisor/whisper")
async def start_whisper(call_id: str, request: Request) -> dict:
    """#42 — coach the agent. Body: ``{supervisor_user_id}``.

    Records a new whisper session. Real audio routing requires a 2-leg
    conference with audio gated to the agent leg only — Telnyx's 2026-Q3
    API does not expose a native "whisper" mode, so v1 ships the
    storage + UI; the v1.1 work is a one-call conference bridge.
    """
    _require_supervisor_role(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    sup = (body.get("supervisor_user_id") or "").strip()
    if not sup:
        raise HTTPException(400, "supervisor_user_id is required")
    tid = _tenant_id(request)
    store = get_store()
    if not store.get_user_by_id(sup):
        raise HTTPException(400, f"supervisor_user_id {sup!r} not found")
    row = store.start_supervisor_session(
        tid, call_id, supervisor_user_id=sup, mode="whisper",
    )
    audio = _best_effort_audio_route(call_id, "whisper", sup)
    return {
        "ok": True,
        "mode": "whisper",
        "session": row,
        "participants": _derived_participants(store, tid, call_id),
        "audio": audio,
        "note": "real whisper audio routing is a v1.1 (2-leg conference + agent-only leg)",
    }


@router.post("/calls/{call_id}/supervisor/barge")
async def start_barge(call_id: str, request: Request) -> dict:
    """#43 — 3-way conference. Body: ``{supervisor_user_id}``."""
    _require_supervisor_role(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    sup = (body.get("supervisor_user_id") or "").strip()
    if not sup:
        raise HTTPException(400, "supervisor_user_id is required")
    tid = _tenant_id(request)
    store = get_store()
    if not store.get_user_by_id(sup):
        raise HTTPException(400, f"supervisor_user_id {sup!r} not found")
    row = store.start_supervisor_session(
        tid, call_id, supervisor_user_id=sup, mode="barge",
    )
    audio = _best_effort_audio_route(call_id, "barge", sup)
    return {
        "ok": True,
        "mode": "barge",
        "session": row,
        "participants": _derived_participants(store, tid, call_id),
        "audio": audio,
    }


# ---------------------------------------------------------------------------
# Read + lifecycle endpoints
# ---------------------------------------------------------------------------


@router.get("/calls/{call_id}/supervisor")
def list_call_sessions(
    call_id: str, request: Request,
    include_closed: bool = False,
) -> dict:
    """List supervisor sessions for a call. The active set is what the
    dashboard's participants panel + agent's listening badge bind to."""
    tid = _tenant_id(request)
    store = get_store()
    sessions = store.list_supervisor_sessions_for_call(
        tid, call_id, include_closed=include_closed,
    )
    return {
        "ok": True,
        "call_id": call_id,
        "participants": _derived_participants(store, tid, call_id),
        "sessions": sessions,
        "count": len(sessions),
    }


@router.post("/supervisor/sessions/{session_id}/end")
def end_session(session_id: str, request: Request) -> dict:
    """Close one supervisor session (idempotent)."""
    _require_supervisor_role(request)
    tid = _tenant_id(request)
    store = get_store()
    row = store.end_supervisor_session(tid, session_id)
    if not row:
        raise HTTPException(404, f"session {session_id!r} not found")
    return {
        "ok": True,
        "session": row,
        "participants": _derived_participants(store, tid, row.get("call_id")),
    }


@router.post("/supervisor/sessions/end_for_call/{call_id}")
def end_for_call(call_id: str, request: Request) -> dict:
    """Hangup hook: close every open session on the call."""
    _require_supervisor_role(request)
    tid = _tenant_id(request)
    store = get_store()
    n = store.end_supervisor_sessions_for_call(tid, call_id)
    return {"ok": True, "call_id": call_id, "closed": n}
