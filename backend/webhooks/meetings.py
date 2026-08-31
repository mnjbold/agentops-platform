"""Video meetings API (issue #26).

Endpoints (mounted under /api):
  POST /api/meetings                       — create a Daily room + return join info
  GET  /api/meetings                       — list the tenant's meetings
  GET  /api/meetings/{id}                  — fetch one meeting row
  POST /api/meetings/{id}/join             — mint a per-user Daily join token
  POST /api/meetings/{id}/end              — mark ended + kick everyone
  POST /api/webhooks/daily                 — handle Daily webhooks (events +
                                             recording ready-to-download)

The Daily client is in :mod:`connectors.daily` and is stub-first — when
``DAILY_API_KEY`` is unset every call returns a synthetic room + token so
the dashboard renders end-to-end without a Daily account.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request

from connectors.daily import get_daily_client
from webhooks.storage import get_store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["meetings"])


# ──────────────────────────── helpers ───────────────────────────────────────


def _tenant_id(request: Request) -> str:
    """Phase A: ``request.state.tenant_id`` is populated by the auth
    middleware. Fall back to the legacy X-Tenant-Id header for tests."""
    tid = getattr(request.state, "tenant_id", None)
    if tid:
        return tid
    tid = request.headers.get("X-Tenant-Id") or request.headers.get("x-tenant-id")
    return (tid or "default").strip() or "default"


def _user_id(request: Request) -> Optional[str]:
    """Return the JWT-resolved user id (or None if API-key-only)."""
    ctx = getattr(request.state, "tenant_ctx", None)
    if ctx is not None and getattr(ctx, "user", None):
        return ctx.user.id
    return None


def _user_name(request: Request) -> str:
    """Best-effort display name for the join token."""
    ctx = getattr(request.state, "tenant_ctx", None)
    if ctx is not None and getattr(ctx, "user", None):
        return ctx.user.email or ctx.user.id or "Host"
    return "Host"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ──────────────────────────── create / list / get ───────────────────────────


@router.post("/meetings")
async def create_meeting(request: Request) -> dict:
    """Create a Daily room and return ``{id, room_url, join_url, host_token}``.

    Body: ``{"title": "..."}``. The host token lets the creator claim the
    "owner" seat without an extra round-trip.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    title = (body.get("title") or "").strip() or "Untitled meeting"
    tid = _tenant_id(request)
    host_user_id = _user_id(request)
    host_name = _user_name(request)

    daily = get_daily_client()
    room = daily.create_room(name_hint="meet", enable_recording=False)
    if not isinstance(room, dict) or not room.get("url") or not room.get("name"):
        # Real API error path
        if isinstance(room, dict) and room.get("ok") is False:
            raise HTTPException(502, f"Daily create_room failed: {room.get('error')}")
        log.warning("Daily create_room returned unexpected shape: %r", room)
        raise HTTPException(502, "Daily create_room returned an unexpected response")

    store = get_store()
    meeting = store.create_meeting(
        tenant_id=tid,
        title=title,
        room_url=room["url"],
        room_name=room["name"],
        host_user_id=host_user_id,
    )
    tok = daily.create_meeting_token(
        room_name=room["name"],
        user_name=host_name,
        is_owner=True,
    )
    host_token = tok.get("token") if isinstance(tok, dict) else None
    return {
        "ok": True,
        "id": meeting["id"],
        "title": meeting["title"],
        "room_url": meeting["room_url"],
        "room_name": meeting["room_name"],
        "join_url": meeting["room_url"],
        "host_token": host_token,
        "stub": bool(room.get("stub")),
    }


@router.get("/meetings")
def list_meetings(request: Request, limit: int = 50, offset: int = 0) -> dict:
    """List the tenant's meetings, newest first."""
    if limit < 1 or limit > 200:
        raise HTTPException(400, "limit must be 1..200")
    tid = _tenant_id(request)
    rows = get_store().list_meetings(tid, limit=limit, offset=offset)
    return {"meetings": rows, "count": len(rows)}


@router.get("/meetings/{meeting_id}")
def get_meeting(meeting_id: str, request: Request) -> dict:
    """Return a single meeting row (participants, recording_url, …)."""
    tid = _tenant_id(request)
    row = get_store().get_meeting(tid, meeting_id)
    if not row:
        raise HTTPException(404, f"meeting {meeting_id} not found")
    return row


# ──────────────────────────── join / end ────────────────────────────────────


@router.post("/meetings/{meeting_id}/join")
async def join_meeting(meeting_id: str, request: Request) -> dict:
    """Mint a per-user Daily token for ``meeting_id``.

    Body (all optional): ``{"user_name": "...", "is_owner": false}``.

    Returns ``{"token": <daily_room_token>, "room_url": ..., "expires_at": <unix>}``.
    Stamps the meeting's ``started_at`` the first time anyone joins.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    tid = _tenant_id(request)
    store = get_store()
    meeting = store.get_meeting(tid, meeting_id)
    if not meeting:
        raise HTTPException(404, f"meeting {meeting_id} not found")
    if meeting.get("ended_at"):
        raise HTTPException(409, "meeting has already ended")
    user_name = (
        (body.get("user_name") or "").strip()
        or _user_name(request)
        or "Guest"
    )
    # The host is the user who created the meeting, if we know who they are.
    is_owner = bool(body.get("is_owner")) or (
        _user_id(request) is not None
        and meeting.get("host_user_id")
        and meeting["host_user_id"] == _user_id(request)
    )
    daily = get_daily_client()
    tok = daily.create_meeting_token(
        room_name=meeting["room_name"],
        user_name=user_name,
        is_owner=is_owner,
    )
    if not isinstance(tok, dict) or not tok.get("token"):
        if isinstance(tok, dict) and tok.get("ok") is False:
            raise HTTPException(502, f"Daily token failed: {tok.get('error')}")
        raise HTTPException(502, "Daily token returned an unexpected response")
    # First-join stamp.
    store.update_meeting_started(tid, meeting_id)
    return {
        "ok": True,
        "token": tok["token"],
        "room_url": meeting["room_url"],
        "room_name": meeting["room_name"],
        "is_owner": is_owner,
        "expires_at": tok.get("expires_at"),
        "stub": bool(tok.get("stub")),
    }


@router.post("/meetings/{meeting_id}/end")
def end_meeting(meeting_id: str, request: Request) -> dict:
    """Mark the meeting as ended and best-effort delete the Daily room
    (which kicks every remaining participant)."""
    tid = _tenant_id(request)
    store = get_store()
    meeting = store.get_meeting(tid, meeting_id)
    if not meeting:
        raise HTTPException(404, f"meeting {meeting_id} not found")
    if meeting.get("ended_at"):
        return {"ok": True, "meeting": meeting, "already_ended": True}
    daily = get_daily_client()
    try:
        daily.delete_room(meeting["room_name"])
    except Exception as e:
        # Don't block the host from leaving on a transport hiccup.
        log.warning("Daily delete_room failed (non-fatal): %s", e)
    updated = store.update_meeting_ended(tid, meeting_id)
    return {"ok": True, "meeting": updated}


# ──────────────────────────── Daily webhook ─────────────────────────────────
# Daily's webhook payload (https://docs.daily.co/reference/webhooks) wraps
# every event under ``{"type": "meeting.ended", "payload": {...}}``. The
# shapes we care about:
#   meeting.ended               → mark ended + capture duration
#   participant.joined          → add to participants_json
#   participant.left            → mark left_at
#   recording.ready-to-download → store recording_url
#
# Daily signs the body with HMAC-SHA256 using the API key as the secret
# (header ``Authorization: Bearer <api_key>`` for simple validation, or
# ``X-Webhook-Signature`` for the strict one). When ``DAILY_API_KEY`` is
# unset (dev/test) we skip validation so the local simulation works.


@router.post("/webhooks/daily")
async def daily_webhook(request: Request) -> dict:
    """Receive a Daily webhook. Always returns 200 (Daily retries on
    non-2xx; we'd rather log + swallow than bounce their retry loop)."""
    try:
        body = await request.json()
    except Exception as e:
        log.warning("Daily webhook: bad JSON: %s", e)
        return {"ok": False, "error": "bad json"}

    # ── optional auth: when DAILY_API_KEY is set, require the same
    # value as the Authorization bearer. This is Daily's simple option;
    # the production-grade alternative is X-Webhook-Signature HMAC.
    expected = (os.environ.get("DAILY_API_KEY") or "").strip()
    if expected:
        auth = (request.headers.get("Authorization") or "").strip()
        if not auth.lower().startswith("bearer ") or auth.split(" ", 1)[1].strip() != expected:
            log.warning("Daily webhook: bad/missing bearer; rejecting")
            raise HTTPException(401, "bad signature")

    evt = body.get("type") or body.get("event") or ""
    payload = body.get("payload") or {}
    room = payload.get("room") or ""
    log.info("Daily webhook: type=%s room=%s", evt, room)

    store = get_store()
    meeting = None
    if room:
        # Daily sends the room name in ``payload.room``. Look it up across
        # tenants — for the v1 single-tenant case this is unambiguous.
        for tid in [t["id"] for t in store.list_tenants()]:
            for m in store.list_meetings(tid, limit=200):
                if m.get("room_name") == room:
                    meeting = m
                    break
            if meeting:
                break

    if evt in ("meeting.ended", "meeting_left"):
        if meeting:
            store.update_meeting_ended(meeting["tenant_id"], meeting["id"])
    elif evt in ("participant.joined", "participant.left"):
        if meeting:
            _record_participant_event(store, meeting, payload, joined=(evt == "participant.joined"))
    elif evt in ("recording.ready-to-download", "recording.ready_to_download", "recording.ready"):
        if meeting:
            rec = (
                payload.get("recording_url")
                or payload.get("url")
                or (payload.get("recording") or {}).get("url")
            )
            if rec:
                store.update_meeting_recording(meeting["tenant_id"], meeting["id"], rec)
    else:
        # Unknown event — log + accept so Daily doesn't retry forever.
        log.info("Daily webhook: ignoring unknown event type=%s", evt)

    return {"ok": True}


def _record_participant_event(store, meeting: dict, payload: dict, *, joined: bool) -> None:
    """Append (or update) the participants_json array for ``meeting``."""
    try:
        current = json.loads(meeting.get("participants_json") or "[]")
    except json.JSONDecodeError:
        current = []
    if not isinstance(current, list):
        current = []
    participant = payload.get("participant") or {}
    user_id = (
        participant.get("user_id")
        or participant.get("userName")
        or participant.get("user_name")
        or participant.get("session_id")
        or "anon"
    )
    user_name = (
        participant.get("userName")
        or participant.get("user_name")
        or user_id
    )
    now_iso = _now()
    # Find an existing open entry for the same user_id.
    for entry in current:
        if entry.get("user_id") == user_id and not entry.get("left_at"):
            if joined:
                # duplicate join — refresh name
                entry["user_name"] = user_name
            else:
                entry["left_at"] = now_iso
            store.update_meeting_participants(meeting["tenant_id"], meeting["id"], current)
            return
    # New entry
    new_entry = {
        "user_id": user_id,
        "user_name": user_name,
        "joined_at": now_iso if joined else None,
        "left_at": None if joined else now_iso,
    }
    current.append(new_entry)
    store.update_meeting_participants(meeting["tenant_id"], meeting["id"], current)


# ──────────────────────────── tenant_id resolver for tests ───────────────────
# Some integration tests bypass the auth middleware and just hit the
# endpoints with a header. Expose a single helper so the meetings router
# is one of the few that always resolves a tenant.
def _resolve_tenant_for_test(request: Request) -> str:
    return _tenant_id(request)
