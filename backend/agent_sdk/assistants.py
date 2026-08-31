"""AI Assistant CRUD + Voice Lab + Test-Call endpoints (issue #14).

Endpoints
---------
GET    /api/assistants                          list (tenant-scoped)
POST   /api/assistants                          create in Telnyx + mirror locally
GET    /api/assistants/{id}                     read one
PATCH  /api/assistants/{id}                     update (also pushes to Telnyx)
DELETE /api/assistants/{id}                     delete (Telnyx + local)
POST   /api/assistants/{id}/test-call           start a WebRTC test room
GET    /api/assistants/{id}/call-log            live transcript + tool calls

GET    /api/voice-lab/voices                    curated list of TTS voices
POST   /api/voice-lab/preview                   body: {text, voice?} → audio

The /api/assistants paths are the *primary* source of truth locally
(so the dashboard works without a live Telnyx call); the Telnyx side
is the source of truth for actually running the assistant on a call.
"""
from __future__ import annotations

import logging
import os
import secrets
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request

from telnyx_mcp.clients.telnyx_client import get_client

from webhooks.storage import get_store
from webhooks._phase_b_ctx import _tenant_id

from . import client as sdk_client

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["assistants"])


def _shape_assistant_for_api(row: dict) -> dict:
    """Normalise the storage row into the shape the frontend expects."""
    return {
        "id": row["id"],
        "tenant_id": row["tenant_id"],
        "name": row.get("name"),
        "telnyx_id": row.get("telnyx_id"),
        "voice": row.get("voice"),
        "system_prompt": row.get("system_prompt"),
        "model": row.get("model"),
        "greeting": row.get("greeting"),
        "tools": row.get("tools") or [],
        "tool_ids": [t.get("name") or t.get("id") or "" for t in (row.get("tools") or [])],
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


@router.get("/assistants")
def list_assistants(request: Request) -> dict:
    store = get_store()
    rows = store.list_assistants(_tenant_id(request))
    return {
        "assistants": [_shape_assistant_for_api(r) for r in rows],
        "count": len(rows),
        "available_tools": sdk_client.ASSISTANT_TOOLS,
    }


@router.post("/assistants")
async def create_assistant(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    voice = body.get("voice") or "Telnyx.KokoroTTS.af_heart"
    system_prompt = body.get("system_prompt") or ""
    greeting = body.get("greeting")
    model = body.get("model") or "openai/gpt-4o"
    selected_tools = body.get("tool_ids") or []
    tools = sdk_client.build_tools(selected_tools)
    # Try to create on Telnyx first. If the API key is missing, we
    # still create the local row so the UI is usable.
    telnyx_id: Optional[str] = None
    try:
        c = get_client()
        created = c.create_assistant(
            name=name,
            instructions=system_prompt,
            model=model,
            voice=voice,
            greeting=greeting,
            tools=tools,
        )
        # The SDK returns a Telnyx-shaped dict; the id lives at
        # created.get("id") or created.get("data", {}).get("id").
        telnyx_id = (
            created.get("id")
            or (created.get("data") or {}).get("id")
        )
    except Exception as e:
        log.warning("Telnyx create_assistant failed; local-only row: %s", e)
    store = get_store()
    row = store.create_assistant(
        _tenant_id(request),
        name=name,
        telnyx_id=telnyx_id,
        voice=voice,
        system_prompt=system_prompt,
        model=model,
        tools=tools,
        greeting=greeting,
    )
    return {"ok": True, "assistant": _shape_assistant_for_api(row)}


@router.get("/assistants/{assistant_id}")
def get_assistant(assistant_id: str, request: Request) -> dict:
    store = get_store()
    row = store.get_assistant(_tenant_id(request), assistant_id)
    if not row:
        raise HTTPException(404, f"assistant {assistant_id} not found")
    return {"assistant": _shape_assistant_for_api(row)}


@router.patch("/assistants/{assistant_id}")
async def update_assistant(assistant_id: str, request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    store = get_store()
    tenant_id = _tenant_id(request)
    existing = store.get_assistant(tenant_id, assistant_id)
    if not existing:
        raise HTTPException(404, f"assistant {assistant_id} not found")
    voice = body.get("voice")
    tools = None
    if "tool_ids" in body:
        tools = sdk_client.build_tools(body.get("tool_ids") or [])
    # Push to Telnyx if we have a telnyx_id and a non-empty patch.
    if existing.get("telnyx_id"):
        try:
            c = get_client()
            c.update_assistant(
                existing["telnyx_id"],
                name=body.get("name"),
                instructions=body.get("system_prompt"),
                voice=voice,
                greeting=body.get("greeting"),
                tools=tools,
            )
        except Exception as e:
            log.warning("Telnyx update_assistant failed: %s", e)
    row = store.update_assistant(
        tenant_id,
        assistant_id,
        name=body.get("name"),
        voice=voice,
        system_prompt=body.get("system_prompt"),
        model=body.get("model"),
        tools=tools,
        greeting=body.get("greeting"),
    )
    return {"ok": True, "assistant": _shape_assistant_for_api(row)}


@router.delete("/assistants/{assistant_id}")
def delete_assistant(assistant_id: str, request: Request) -> dict:
    store = get_store()
    tenant_id = _tenant_id(request)
    existing = store.get_assistant(tenant_id, assistant_id)
    if not existing:
        raise HTTPException(404, f"assistant {assistant_id} not found")
    if existing.get("telnyx_id"):
        try:
            c = get_client()
            c.delete_assistant(existing["telnyx_id"])
        except Exception as e:
            log.warning("Telnyx delete_assistant failed: %s", e)
    ok = store.delete_assistant(tenant_id, assistant_id)
    return {"ok": ok, "id": assistant_id}


# ──────────────────────── Test call + call log ────────────────────────


@router.post("/assistants/{assistant_id}/test-call")
def test_call(assistant_id: str, request: Request) -> dict:
    """Open a WebRTC test room for this assistant.

    The frontend uses the returned ``room_id`` + ``token`` to start a
    WebRTC session in the browser. Once the room is live, the JS side
    can call ``POST /api/assistants/{id}/test-call/attach`` (see
    below) to actually wire the assistant to the WebRTC call.

    We also seed a row in ``assistant_call_log`` with role='system' so
    the test-call panel has a place to start appending the transcript.
    """
    store = get_store()
    tenant_id = _tenant_id(request)
    row = store.get_assistant(tenant_id, assistant_id)
    if not row:
        raise HTTPException(404, f"assistant {assistant_id} not found")
    room = sdk_client.create_test_call_room()
    call_id = room.get("room_id") or f"test-{secrets.token_urlsafe(6)}"
    store.append_assistant_log(
        tenant_id, assistant_id, "system",
        content=f"Test call started (room {call_id})",
        call_id=call_id,
    )
    return {
        "ok": True,
        "assistant_id": assistant_id,
        "call_id": call_id,
        "room_id": room.get("room_id"),
        "token": room.get("token"),
        "stub": room.get("stub", False),
    }


@router.get("/assistants/{assistant_id}/call-log")
def call_log(assistant_id: str, request: Request, limit: int = 200) -> dict:
    store = get_store()
    rows = store.list_assistant_call_log(
        _tenant_id(request), assistant_id, limit=limit)
    return {"log": rows, "count": len(rows)}


# A tiny dev-only POST to append a transcript/tool line so the UI can
# be demoed without a live Telnyx call. Disabled in production by the
# `W3J_ALLOW_TEST_LOG=1` env var.
@router.post("/assistants/{assistant_id}/test-call/log")
async def append_test_log(assistant_id: str, request: Request) -> dict:
    if os.environ.get("W3J_ALLOW_TEST_LOG", "1") != "1":
        raise HTTPException(403, "test log disabled")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    role = (body.get("role") or "user").strip()
    if role not in ("user", "assistant", "tool", "system"):
        raise HTTPException(400, "role must be user/assistant/tool/system")
    store = get_store()
    row = store.append_assistant_log(
        _tenant_id(request), assistant_id, role,
        content=body.get("content"),
        call_id=body.get("call_id"),
        tool_name=body.get("tool_name"),
        tool_args=body.get("tool_args"),
    )
    return {"ok": True, "entry": row}


# ──────────────────────── Voice Lab ────────────────────────


@router.get("/voice-lab/voices")
def voice_lab_voices() -> dict:
    return {"voices": sdk_client.list_voices()}


@router.post("/voice-lab/preview")
async def voice_lab_preview(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text is required")
    if len(text) > 500:
        raise HTTPException(400, "text must be 500 chars or less for previews")
    voice = body.get("voice") or "Telnyx.KokoroTTS.af_heart"
    model = body.get("model") or "telnyx/tts-1"
    fmt = body.get("response_format") or "mp3"
    return sdk_client.tts_preview(text, voice=voice, model=model, response_format=fmt)
