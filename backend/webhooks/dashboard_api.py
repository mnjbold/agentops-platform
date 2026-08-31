"""Dashboard REST API — exposed by the webhook server.

Endpoints (mounted under /api):
  GET  /api/state                                — full snapshot (numbers, agents, calls, balance)
  GET  /api/numbers                              — owned phone numbers
  GET  /api/agents                               — AI assistants (curated top-level)
  GET  /api/calls/recent?limit=20                — recent call events from SQLite
  GET  /api/calls/live                           — calls that answered in the last 5 min
                                                   and have not yet hung up / hit AMD
  GET  /api/recordings?limit=10                  — recent recordings from Telnyx
  GET  /api/recordings/audio/{recording_id}      — proxy a recording MP3 (avoid CORS)
  POST /api/dial                                 — place outbound call {to, from, webhook_url?}
  POST /api/sms                                  — send SMS {to, from, text}
  GET  /api/messages/recent?limit=50             — recent inbound+outbound SMS from Telnyx
  GET  /api/messages/threads?limit=30            — SMS threads grouped by remote number
  POST /api/sms/send                             — send SMS via SDK {to, from?, text} (UI-friendly)
  GET  /api/balance                              — current balance
  POST /api/login                                — simple mock login (returns a session token)

Authentication: NONE for v0.1 (single-tenant localhost-only). For production,
mount behind a reverse proxy that requires an auth header.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sys
import threading
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from connectors.registry import get_registry
from telnyx_mcp.clients.telnyx_client import get_client, to_dict

# Make the `appx` package importable. The canonical copy lives in
# `agentops-platform/backend/appx/` (the new monorepo). This server still
# runs from W3J-BIJOU PROJECT/webhooks/, so we add the monorepo's backend
# dir to sys.path. When we fully migrate the running server into
# agentops-platform/backend/, this shim becomes a no-op (backend/ is
# already on sys.path then).
_APPX_PARENT = Path(__file__).resolve().parent.parent.parent / "agentops-platform" / "backend"
if _APPX_PARENT.exists() and str(_APPX_PARENT) not in sys.path:
    sys.path.insert(0, str(_APPX_PARENT))


def _resolve_tenant(request: Optional[Request] = None) -> str:
    """Resolve the tenant id.

    Priority:
    1. ``request.state.tenant_id`` — set by the Phase A auth middleware
       after validating an X-Api-Key or JWT.
    2. ``X-Tenant-Id`` header — legacy single-tenant header (still works
       for back-compat; the middleware logs a deprecation warning).
    3. ``"default"`` — fallback for tests / single-tenant MVP.
    """
    if request is None:
        return "default"
    tid = getattr(request.state, "tenant_id", None)
    if tid:
        return tid
    tid = request.headers.get("X-Tenant-Id") or request.headers.get("x-tenant-id")
    return (tid or "default").strip() or "default"

log = logging.getLogger(__name__)
# Uvicorn reconfigures the root logger during startup and may reset the
# effective level to WARNING. Force this module's logger to INFO and
# attach our own stderr handler so scheduler ticks + campaign launches
# always show up regardless of what the parent handler setup does.
# ``propagate=False`` prevents the message being printed again by the
# root handler (which may also exist after ``basicConfig``).
log.setLevel(logging.INFO)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))
    log.addHandler(_h)
    log.propagate = False

router = APIRouter(prefix="/api", tags=["dashboard"])


# ───────────────────────────── WebSocket broker ─────────────────────────────
# Thread-safe pubsub that lets the webhook handler (sync FastAPI route) push
# real-time events to browser tabs connected over WebSocket. Each browser tab
# identifies itself with a session_token from localStorage; the broker
# delivers events to that tab's queue. Events published with an empty/None
# session_token are broadcast to all currently-connected subscribers.
#
# The bridge between the sync webhook handler and the async broker is the
# ``publish_event`` helper below: it schedules a coroutine on the running
# event loop (or runs an ad-hoc one) so the webhook side can stay sync.
class WsBroker:
    """Async pubsub keyed by session_token (browser tab id).

    - subscribe(token) -> queue. Each subscriber gets its own asyncio.Queue.
      A single tab may subscribe multiple times (e.g. during reconnect).
    - publish(token, event) -> schedules the queue.put_nowait on the running
      loop. If no loop is running, it falls back to a sync put-nowait on each
      queue (the WS handler drains the queue from its own coroutine, so a
      sync put into the queue is safe — the consumer is on the event loop).
    - publish_now(token, event) -> synchronous put, used by non-async callers.
    """

    def __init__(self) -> None:
        # token -> set[asyncio.Queue]
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)
        # A regular (non-async) lock guards the dict; the queues themselves
        # are thread-safe by asyncio design.
        self._lock = threading.Lock()
        # Cached running loop for the common case (the WS endpoint lives
        # on the same process as the webhook handler).
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Called by the WS endpoint at accept time so publish() can schedule."""
        self._loop = loop

    def subscribe(self, token: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        with self._lock:
            self._subs[token].add(q)
        return q

    def unsubscribe(self, token: str, q: asyncio.Queue) -> None:
        with self._lock:
            subs = self._subs.get(token)
            if subs is None:
                return
            subs.discard(q)
            if not subs:
                self._subs.pop(token, None)

    def publish(self, token: Optional[str], event: dict) -> None:
        """Schedule the delivery on the running loop. Safe to call from sync
        code (webhook handler). If no loop is cached, falls back to a sync
        put which is still safe because the consumer is the WS handler.
        """
        if self._loop is not None and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self._apublish(token, event), self._loop)
                return
            except Exception:
                pass
        # Fallback: sync put. The consumer is always the WS coroutine on the
        # event loop, but Queue.put_nowait is safe to call from any thread.
        self._publish_sync(token, event)

    def _publish_sync(self, token: Optional[str], event: dict) -> None:
        with self._lock:
            if token:
                qs = list(self._subs.get(token, ()))
            else:
                qs = [q for subs in self._subs.values() for q in subs]
        for q in qs:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Drop the oldest, then push the new one so the most recent
                # event always wins for a slow consumer.
                try:
                    q.get_nowait()
                except Exception:
                    pass
                try:
                    q.put_nowait(event)
                except Exception:
                    pass

    async def _apublish(self, token: Optional[str], event: dict) -> None:
        self._publish_sync(token, event)

    def stats(self) -> dict:
        with self._lock:
            return {
                "tokens": len(self._subs),
                "total_queues": sum(len(s) for s in self._subs.values()),
                "tokens_detail": {t: len(s) for t, s in self._subs.items()},
            }


# Process-wide singleton. The webhook handler in server.py imports this
# instance to publish events as Telnyx webhooks arrive.
ws_broker = WsBroker()

# Curated top-level agents (the ones actually wired to numbers).
# Specialists + test voices are filtered out of the dashboard list.
_CURATED_AGENT_SUBSTRINGS = (
    "W3J LLC Concierge",
    "W3J Personal Twin",
    "Bijou AI Concierge",
    "W3J Outbound Dispatcher",
    "W3J Sales",
    "W3J CS",
    "W3J Tech Support",
    "W3J Consultants",
    "W3J Automation",
    "W3J Dev",
    "Bold Business IT Support",
    "J.A.R.V.I.S.",
    "Multilingual Support",
    "Multi-Department Router",
    "High-Intent Lead Screener",
    "B2B Demo Qualification",
    "Service Appointment Booking",
    "Outreach Dialer",
    "Amanda QA",
)


def _is_curated(name: str) -> bool:
    return any(s.lower() in (name or "").lower() for s in _CURATED_AGENT_SUBSTRINGS)


@router.get("/state")
def get_state() -> dict:
    """Full dashboard snapshot — one call gives the UI everything."""
    c = get_client()
    out: dict[str, Any] = {
        "ok": True,
        "ts": datetime.now(timezone.utc).isoformat(),
        "service": "w3j-telephony-dashboard",
    }
    # balance
    try:
        bal = c.api.balance.retrieve()
        d = to_dict(bal)
        out["balance"] = {
            "balance": d.get("balance"),
            "currency": d.get("currency"),
            "credit_limit": d.get("credit_limit"),
            "available_credit": d.get("available_credit"),
        }
    except Exception as e:
        out["balance"] = {"error": str(e)}

    # numbers
    try:
        nums = c.list_all(c.api.phone_numbers.list(), 50)
        out["numbers"] = [
            {
                "phone_number": n.get("phone_number"),
                "country_code": n.get("country_iso_alpha2"),
                "type": n.get("phone_number_type"),
                "status": n.get("status"),
                "connection_id": n.get("connection_id"),
                "connection_name": n.get("connection_name"),
                "messaging_profile_name": n.get("messaging_profile_name"),
            }
            for n in nums
        ]
    except Exception as e:
        out["numbers"] = []
        out["numbers_error"] = str(e)

    # agents (curated)
    try:
        asst = c.list_all(c.api.ai.assistants.list(), 100)
        out["agents"] = [
            {
                "id": a.get("id"),
                "name": a.get("name"),
                "model": a.get("model"),
                "voice": (a.get("voice_settings") or {}).get("voice") if isinstance(a.get("voice_settings"), dict) else None,
                "greeting": a.get("greeting"),
                "created_at": a.get("created_at"),
            }
            for a in asst
            if _is_curated(a.get("name", ""))
        ]
    except Exception as e:
        out["agents"] = []
        out["agents_error"] = str(e)

    # recent call events from SQLite
    try:
        reg = get_registry()
        sqlite = next(cn for cn in reg._connectors if cn.name == "sqlite")
        evs = sqlite.recent_events(limit=20)
        out["recent_events"] = evs
        out["recent_events_count"] = len(evs)
    except Exception as e:
        out["recent_events"] = []
        out["recent_events_error"] = str(e)

    return out


@router.get("/numbers")
def get_numbers() -> dict:
    c = get_client()
    try:
        nums = c.list_all(c.api.phone_numbers.list(), 50)
    except Exception as e:
        raise HTTPException(500, f"Telnyx error: {e}")
    return {
        "numbers": [
            {
                "phone_number": n.get("phone_number"),
                "country_code": n.get("country_iso_alpha2"),
                "type": n.get("phone_number_type"),
                "status": n.get("status"),
                "connection_name": n.get("connection_name"),
            }
            for n in nums
        ]
    }


@router.get("/agents")
def get_agents() -> dict:
    c = get_client()
    try:
        asst = c.list_all(c.api.ai.assistants.list(), 100)
    except Exception as e:
        raise HTTPException(500, f"Telnyx error: {e}")
    return {
        "agents": [
            {
                "id": a.get("id"),
                "name": a.get("name"),
                "model": a.get("model"),
                "voice": (a.get("voice_settings") or {}).get("voice") if isinstance(a.get("voice_settings"), dict) else None,
                "greeting": (a.get("greeting") or "")[:120],
                "created_at": a.get("created_at"),
            }
            for a in asst
            if _is_curated(a.get("name", ""))
        ]
    }


@router.get("/calls/recent")
def recent_calls(request: Request, limit: int = 20) -> dict:
    """Recent call history from the SaaS data layer (Appwrite `calls`).

    The webhook handler writes inbound and outbound call events to Appwrite
    as the source of truth. We also fall back to the SQLite event log if
    Appwrite is unreachable, so the dashboard never goes blank.
    """
    cap = min(max(int(limit), 1), 200)
    tid = _resolve_tenant(request)
    # Primary: Appwrite
    try:
        from appx.repos import calls as calls_repo
        docs = calls_repo.list_recent(tid, limit=cap)
        shaped = [_shape_appwrite_call_for_ui(d) for d in docs]
        return {"events": shaped, "count": len(shaped), "source": "appwrite"}
    except Exception as e:
        log.warning("Appwrite /calls/recent failed, falling back to sqlite: %s", e)
    # Fallback: SQLite event log (older installs before Appwrite write-through)
    try:
        reg = get_registry()
        sqlite = next(cn for cn in reg._connectors if cn.name == "sqlite")
        evs = sqlite.recent_events(limit=cap)
        return {"events": evs, "count": len(evs), "source": "sqlite"}
    except Exception as e:
        log.warning("sqlite /calls/recent fallback also failed: %s", e)
        return {"events": [], "count": 0, "error": str(e)}


def _shape_appwrite_call_for_ui(doc: dict) -> dict:
    """Map an Appwrite `calls` document to the dashboard's expected event shape.

    The frontend reads ``event_type``, ``from_number``, ``to_number``,
    ``timestamp`` from this list — same field names the old SQLite events
    used, so the existing UI works without changes.
    """
    return {
        "id": doc.get("call_control_id") or doc.get("$id"),
        "event_type": f"call.{doc.get('status', 'unknown')}",
        "call_control_id": doc.get("call_control_id") or doc.get("$id"),
        "direction": doc.get("direction", ""),
        "from_number": doc.get("from_number", ""),
        "to_number": doc.get("to_number", ""),
        "from_name": doc.get("from_name", ""),
        "to_name": doc.get("to_name", ""),
        "timestamp": doc.get("started_at") or doc.get("$createdAt") or doc.get("ended_at"),
        "started_at": doc.get("started_at"),
        "answered_at": doc.get("answered_at"),
        "ended_at": doc.get("ended_at"),
        "duration_seconds": doc.get("duration_seconds", 0),
        "status": doc.get("status", "unknown"),
        "has_recording": doc.get("has_recording", False),
        "recording_url": doc.get("recording_url", ""),
        "assistant_id": doc.get("assistant_id", ""),
    }


@router.get("/recordings/telnyx")
def recent_recordings(limit: int = 10) -> dict:
    """Live Telnyx recordings (last 7 days, newest first). The Phase A
    FTS5 search lives at ``/api/recordings`` (see ``voicemail_api.py``);
    this endpoint is kept under ``/api/recordings/telnyx`` so dashboards
    that want the live Telnyx view can still get it without colliding
    with the local search.
    """
    c = get_client()
    try:
        start = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
        r = c.api.recordings.list(filter={"created_at": {"gte": start}}, page_size=min(limit, 50))
        recs = c.list_all(r, limit)
        return {
            "recordings": [
                {
                    "id": rec.get("id"),
                    "created_at": rec.get("created_at"),
                    "duration_secs": rec.get("duration_secs"),
                    "channels": rec.get("channels"),
                    "call_leg_id": rec.get("call_leg_id"),
                    "format": rec.get("recording_format"),
                    "status": rec.get("status"),
                    "urls": rec.get("recording_urls"),
                }
                for rec in recs
            ]
        }
    except Exception as e:
        return {"recordings": [], "error": str(e)}


@router.get("/balance")
def get_balance() -> dict:
    c = get_client()
    try:
        bal = c.api.balance.retrieve()
        d = to_dict(bal)
        return {
            "balance": d.get("balance"),
            "currency": d.get("currency"),
            "credit_limit": d.get("credit_limit"),
            "available_credit": d.get("available_credit"),
        }
    except Exception as e:
        raise HTTPException(500, f"Telnyx error: {e}")


@router.post("/dial")
async def api_dial(request: Request) -> dict:
    """Place an outbound call. JSON body: {to, from?, webhook_url?, connection_id?}

    Also upserts a row in Appwrite ``calls`` so the dashboard's call history
    is populated before the webhook handler sees the call.initiated event.
    Telnyx is the source of truth for the call itself; Appwrite is the
    source of truth for the local dashboard view.
    """
    body = await request.json()
    to = body.get("to")
    if not to:
        raise HTTPException(400, "to is required (E.164)")

    from_num = body.get("from")
    webhook_url = body.get("webhook_url", "https://bk-jr-api.aixlabs.fun/webhooks/telnyx")
    connection_id = body.get("connection_id")

    c = get_client()
    # If no from, default to the CLEAN softphone line (no AI assistant attached).
    # The other 3 owned numbers (+18444618814, +13079999692, +13204280793) all have
    # AI assistants on their call control apps, which intercept outbound calls
    # (greeting, whisper, etc). +15078731084 is wired to "W3J Softphone Clean Line"
    # (app 3035787979835574155) with no AI, so the call goes through cleanly.
    if not from_num:
        from_num = "+15078731084"
    if not connection_id:
        # Find the connection_id for the from number
        try:
            nums = c.list_all(c.api.phone_numbers.list(), 50)
            for n in nums:
                if n.get("phone_number") == from_num:
                    connection_id = n.get("connection_id")
                    break
        except Exception:
            pass
        if not connection_id:
            # Fallback: fetch call control applications and use the first one.
            # We do NOT hardcode an ID — Telnyx rejects unknown connection_ids
            # and a stale literal here was the root cause of failed outbound dials.
            try:
                apps = c.list_all(c.api.call_control_applications.list(), 50)
                if apps:
                    connection_id = apps[0].get("id")
            except Exception:
                pass
        if not connection_id:
            raise HTTPException(
                400,
                "Could not determine connection_id for the 'from' number. "
                "Set connection_id explicitly, or check that the from number "
                "has a call control application assigned in Telnyx Mission Control.",
            )

    try:
        # Use the official Telnyx Python SDK (not raw httpx) so we get
        # typed responses, automatic retry, and consistent auth.
        dial_result = c.dial(
            to=to,
            from_=from_num,
            connection_id=connection_id,
            client_state=f"dial:{to}",
        )
        # Normalize to a JSON-friendly shape.
        data = dial_result.get("data") if isinstance(dial_result, dict) and "data" in dial_result else dial_result
        call_control_id = (data or {}).get("call_control_id") or ""
        # Telnyx dial returns 200 on success; SDK raises on HTTP >= 400.
        body_json = {"data": data} if not isinstance(dial_result, dict) or "data" not in dial_result else dial_result
        out = {"status": 200, "body": body_json}
        # Best-effort: also write to Appwrite so the dashboard sees the call
        # before the webhook handler updates it. The webhook handler will
        # later upsert with full Telnyx metadata (status, duration, recording).
        try:
            if call_control_id:
                from appx.repos import calls as calls_repo
                now_iso = datetime.now(timezone.utc).isoformat()
                calls_repo.upsert_call(
                    tenant_id=_resolve_tenant(request),
                    call_control_id=call_control_id,
                    direction="outbound",
                    from_number=from_num,
                    to_number=to,
                    status="initiated",
                    started_at=now_iso,
                )
        except Exception as e:
            log.warning("Appwrite upsert_call after /dial failed (non-fatal): %s", e)
        return out
    except Exception as e:
        # Surface a structured 502 so the frontend can show a useful message.
        log.exception("Telnyx dial error")
        raise HTTPException(502, f"Telnyx dial error: {e}")


# Common helper used by both reject and hangup endpoints
def _telnyx_call_action(call_control_id: str, action: str) -> dict:
    """POST a call control action (reject/hangup/answer) to Telnyx and return status."""
    c = get_client()
    url = f"https://api.telnyx.com/v2/calls/{call_control_id}/actions/{action}"
    # Both reject and hangup accept an optional `command_id` for idempotency.
    body = {"command_id": secrets.token_hex(8)}
    try:
        r = httpx.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {c.creds.api_key}", "Content-Type": "application/json"},
            timeout=10,
        )
        return {
            "status": r.status_code,
            "body": r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:500],
        }
    except Exception as e:
        raise HTTPException(500, f"Telnyx {action} error: {e}")


@router.post("/calls/{call_control_id}/reject")
async def api_reject_call(call_control_id: str) -> dict:
    """Reject an inbound ringing call on the Telnyx side. This is the action
    the softphone's 'decline' button MUST trigger, otherwise the WebRTC media
    path stays open even after the UI modal is hidden.
    """
    return _telnyx_call_action(call_control_id, "reject")


@router.post("/calls/{call_control_id}/hangup")
async def api_hangup_call(call_control_id: str) -> dict:
    """Hang up an active or just-answered call."""
    return _telnyx_call_action(call_control_id, "hangup")


@router.post("/calls/{call_control_id}/answer")
async def api_answer_call(call_control_id: str) -> dict:
    """Answer a ringing call. Useful as a fallback when the browser WebRTC
    SDK does not deliver the `call.state === 'ringing'` notification in time.
    """
    return _telnyx_call_action(call_control_id, "answer")


@router.post("/voice/tts")
async def api_voice_tts(request: Request) -> dict:
    """Synthesize text to speech via Telnyx AI Audio (TTS).

    Body: ``{text, voice?, model?, response_format?, speed?}``
    Returns: ``{audio_url?, audio_base64?, voice, model, response_format}``

    The frontend (system-agent voice mode) calls this instead of
    ``window.speechSynthesis.speak`` so the agent uses the same voice
    identity as the live call agents, all routed through the user's
    Telnyx account.
    """
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text is required")
    voice = body.get("voice") or "Telnyx.Ultra.a5136bf9-224c-4d76-b823-52bd5efcffcc"
    model = body.get("model") or "telnyx/tts-1"
    response_format = body.get("response_format") or "mp3"
    try:
        speed = float(body.get("speed") or 1.0)
    except (TypeError, ValueError):
        speed = 1.0
    try:
        c = get_client()
        result = c.synthesize_speech(
            text=text,
            voice=voice,
            model=model,
            response_format=response_format,
            speed=speed,
        )
        return {
            "audio_url": result.get("audio_url"),
            "audio_base64": result.get("audio_base64"),
            "voice": voice,
            "model": model,
            "response_format": response_format,
        }
    except Exception as e:
        log.exception("TTS failed")
        raise HTTPException(502, f"Telnyx TTS error: {e}")


@router.post("/voice/stt")
async def api_voice_stt(request: Request) -> dict:
    """Transcribe an audio blob (base64) to text via Telnyx AI Audio (STT).

    Body: ``{audio_base64, model?, language?, mime_type?}``
    Returns: ``{text, model, language}``

    The frontend MediaRecorder captures audio, base64-encodes it, and
    POSTs here. The result goes back into the agent conversation.
    """
    body = await request.json()
    audio_b64 = (body.get("audio_base64") or "").strip()
    if not audio_b64:
        raise HTTPException(400, "audio_base64 is required")
    model = body.get("model") or "openai/whisper-large-v3-turbo"
    language = body.get("language") or "en"
    try:
        c = get_client()
        result = c.transcribe_audio(
            audio_b64=audio_b64,
            model=model,
            language=language,
        )
        # Telnyx transcription response shape: {"text": "..."} or {data:{text:...}}
        text = result.get("text") or (result.get("data") or {}).get("text") or ""
        return {"text": text.strip(), "model": model, "language": language}
    except Exception as e:
        log.exception("STT failed")
        raise HTTPException(502, f"Telnyx STT error: {e}")


@router.post("/sms")
async def api_sms(request: Request) -> dict:
    """Send an SMS. JSON body: {to, from?, text}

    Also upserts a row in Appwrite ``messages`` so the dashboard sees the
    outgoing SMS immediately. Mirrors ``/api/sms/send`` (the UI-friendly
    version that returns ``{ok, id, ...}``) but uses the older
    ``{status, body}`` shape for backwards compat.
    """
    body = await request.json()
    to = body.get("to")
    text = body.get("text")
    if not to or not text:
        raise HTTPException(400, "to and text are required")

    from_num = body.get("from", "+18444618814")
    c = get_client()
    try:
        r = httpx.post(
            "https://api.telnyx.com/v2/messages",
            json={"to": to, "from": from_num, "text": text, "type": "SMS"},
            headers={"Authorization": f"Bearer {c.creds.api_key}", "Content-Type": "application/json"},
            timeout=15,
        )
        body_json = (
            r.json()
            if r.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        # Best-effort Appwrite write so the dashboard history is complete
        # regardless of which endpoint the caller uses.
        try:
            msg_id = (body_json.get("data") or {}).get("id")
            if msg_id and r.status_code < 400:
                from appx.repos import messages as messages_repo
                now_iso = datetime.now(timezone.utc).isoformat()
                messages_repo.upsert_message(
                    tenant_id=_resolve_tenant(request),
                    message_id=msg_id,
                    direction="outbound",
                    from_number=from_num,
                    to_number=to,
                    body=text,
                    status="queued",
                    sent_at=now_iso,
                )
        except Exception as e:
            log.warning("Appwrite upsert_message after /sms failed (non-fatal): %s", e)
        return {
            "status": r.status_code,
            "body": body_json if body_json else (r.text[:500] if r.text else ""),
        }
    except Exception as e:
        raise HTTPException(500, f"Telnyx SMS error: {e}")


@router.get("/webrtc/credentials")
def get_webrtc_credentials() -> dict:
    """Return the Telnyx WebRTC credential for the browser softphone.

    The credential was created via the Telnyx API on 2026-08-27 and stored in .env.
    The browser softphone auto-fetches this on login so the user never has to paste.
    """
    from telnyx_mcp.utils.env import load_integrations
    integ = load_integrations()
    if not integ.webrtc_username or not integ.webrtc_password:
        return {"ok": False, "error": "WebRTC credentials not configured on server"}
    return {
        "ok": True,
        "login": integ.webrtc_username,
        "password": integ.webrtc_password,
        "connection_id": integ.webrtc_connection_id,
    }


@router.post("/login")
async def api_login(request: Request) -> dict:
    """Mock login. v0.1: any non-empty username/password returns a session token.
    Real auth comes in v2 with the multi-tenant shell.
    """
    body = await request.json()
    user = body.get("user", "")
    pw = body.get("password", "")
    if not user or not pw:
        raise HTTPException(400, "user and password required")
    return {
        "ok": True,
        "token": secrets.token_urlsafe(24),
        "user": user,
        "role": "admin",
        "issued_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/calls/live")
def live_calls() -> dict:
    """Calls that have answered in the last 5 minutes but have not yet
    produced a ``call.hangup`` or ``call.machine.detection.ended`` event.

    Telnyx does not expose a "list active calls" API, so we infer the
    live set from the SQLite event sink. We scan the most recent 200
    events, filter to a 5-minute window, group by ``call_control_id``,
    and keep any CCI whose most recent terminal-state marker is
    ``call.answered``.
    """
    reg = get_registry()
    sqlite = next(cn for cn in reg._connectors if cn.name == "sqlite")
    try:
        evs = sqlite.recent_events(limit=200)
    except Exception as e:
        raise HTTPException(500, f"sqlite error: {e}")

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=5)

    # Group events by call_control_id, keep only those inside the window.
    by_cci: dict[str, list[dict]] = {}
    for ev in evs:
        cci = ev.get("call_control_id")
        ts_str = ev.get("timestamp")
        if not cci or not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except Exception:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < window_start:
            continue
        by_cci.setdefault(cci, []).append({**ev, "_ts": ts})

    live: list[dict] = []
    for cci, cci_evs in by_cci.items():
        # Oldest first so we can scan for an answer followed by a terminal event.
        cci_evs.sort(key=lambda e: e["_ts"])
        answered_at: Optional[datetime] = None
        ended = False
        from_n: Optional[str] = None
        to_n: Optional[str] = None
        for ev in cci_evs:
            et = ev.get("event_type", "")
            if et == "call.answered":
                answered_at = ev["_ts"]
                from_n = ev.get("from_number") or from_n
                to_n = ev.get("to_number") or to_n
            elif et in ("call.hangup", "call.machine.detection.ended"):
                if answered_at is not None:
                    ended = True
                    break
        if answered_at and not ended:
            try:
                duration_so_far_secs: Optional[int] = int((now - answered_at).total_seconds())
            except Exception:
                duration_so_far_secs = None
            live.append(
                {
                    "call_control_id": cci,
                    "from": from_n,
                    "to": to_n,
                    "answered_at": answered_at.isoformat(),
                    "duration_so_far_secs": duration_so_far_secs,
                }
            )

    return {"live": live, "count": len(live)}


@router.get("/recordings/audio/{recording_id}")
def proxy_recording_audio(recording_id: str) -> Response:
    """Proxy a Telnyx recording MP3 to the browser to avoid CORS.

    Telnyx signs the download URL with a short-lived AWS sig; we fetch a
    fresh URL via ``recordings.retrieve`` and stream the bytes back as
    ``audio/mpeg``.
    """
    c = get_client()
    try:
        rec = c.api.recordings.retrieve(recording_id)
    except Exception as e:
        raise HTTPException(500, f"Telnyx retrieve error: {e}")
    rec_d = to_dict(rec)
    # Telnyx's actual field is `download_urls`; the older list endpoint
    # exposed `recording_urls`. Accept both so we work with either shape.
    urls = rec_d.get("recording_urls") or rec_d.get("download_urls") or {}
    mp3_url = urls.get("mp3") if isinstance(urls, dict) else None
    if not mp3_url:
        raise HTTPException(404, "no mp3 url found for this recording")
    try:
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            r = client.get(mp3_url)
            r.raise_for_status()
            audio_bytes = r.content
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Failed to fetch recording: {e}")
    return Response(content=audio_bytes, media_type="audio/mpeg")


@router.get("/messages/recent")
def recent_messages(request: Request, limit: int = 50) -> dict:
    """Recent inbound + outbound SMS from the SaaS data layer (Appwrite `messages`).

    The webhook handler writes every message event (inbound + outbound) to
    Appwrite as the source of truth. Falls back to the Telnyx MDR endpoint
    if Appwrite is unreachable, so the dashboard never goes blank.
    """
    cap = min(max(int(limit), 1), 200)
    tid = _resolve_tenant(request)
    # Primary: Appwrite
    try:
        from appx.repos import messages as messages_repo
        docs = messages_repo.list_recent(tid, limit=cap)
        out = [_shape_appwrite_message_for_ui(d) for d in docs]
        return {"messages": out, "count": len(out), "source": "appwrite"}
    except Exception as e:
        log.warning("Appwrite /messages/recent failed, falling back to MDR: %s", e)
    # Fallback: Telnyx MDR
    c = get_client()
    try:
        mdrs = _list_mdr_messages(c, cap, sort="-created_at")
    except Exception as e:
        return {"messages": [], "count": 0, "error": f"Appwrite and Telnyx both failed: {e}"}
    out = [_shape_message_for_ui(m) for m in mdrs[:cap]]
    return {"messages": out, "count": len(out), "source": "telnyx-mdr"}


def _shape_appwrite_message_for_ui(doc: dict) -> dict:
    """Map an Appwrite `messages` document to the dashboard's expected shape."""
    direction = doc.get("direction", "")
    from_n = doc.get("from_number", "")
    to_n = doc.get("to_number", "")
    return {
        "id": doc.get("message_id") or doc.get("$id"),
        "direction": direction,
        "from": {"phone_number": from_n} if from_n else None,
        "to": [{"phone_number": to_n}] if to_n else [],
        "text": doc.get("body", ""),
        "created_at": doc.get("sent_at") or doc.get("received_at") or doc.get("$createdAt"),
        "completed_at": doc.get("$updatedAt"),
        "read": None,
        "type": "SMS",
        "errors": None,
        "media_urls": doc.get("media_urls", []),
    }


@router.get("/messages/threads")
def message_threads(request: Request, limit: int = 30, channel: Optional[str] = None) -> dict:
    """SMS threads: one entry per remote phone number, latest message wins.

    Backed by Appwrite `messages` (single source of truth). The "remote"
    number is the OTHER side of the conversation (i.e. not the softphone's
    owned number for the tenant). Falls back to the Telnyx MDR grouping
    if Appwrite is unreachable.

    Reads up to 500 recent messages and groups in Python (Appwrite has no
    native group-by; for thousands of threads a real aggregation is needed,
    but the MVP is single-user).

    ``channel`` filter: ``whatsapp`` returns the local ``whatsapp_messages``
    table; ``email`` returns ``email_messages`` grouped by ``from_addr``;
    ``None`` returns SMS only.
    """
    cap = min(max(int(limit), 1), 30)
    tid = _resolve_tenant(request)

    # Channel-specific fast path (issues #22 WhatsApp, #27 Email)
    if channel == "whatsapp":
        store = get_store()
        rows = store.list_whatsapp_threads(tid)
        threads = [{
            "remote": r.get("remote"),
            "last_message": r.get("last_body", ""),
            "last_at": r.get("last_at"),
            "channel": "whatsapp",
        } for r in rows[:cap]]
        return {"threads": threads, "count": len(threads),
                "channel": "whatsapp", "source": "local"}
    if channel == "email":
        store = get_store()
        rows = store.list_email_messages(tid, limit=500)
        seen: dict = {}
        for r in rows:
            addr = r.get("from_addr") if r.get("direction") == "inbound" else r.get("to_addr")
            if not addr or addr in seen:
                continue
            seen[addr] = {
                "remote": addr,
                "last_message": r.get("subject", ""),
                "last_at": r.get("sent_at"),
                "channel": "email",
            }
        threads = list(seen.values())[:cap]
        return {"threads": threads, "count": len(threads),
                "channel": "email", "source": "local"}
    # Primary: Appwrite
    try:
        from appx.repos import messages as messages_repo
        docs = messages_repo.list_recent(tid, limit=500)
        threads = _group_messages_into_threads(docs, cap=cap)
        return {"threads": threads, "count": len(threads), "source": "appwrite"}
    except Exception as e:
        log.warning("Appwrite /messages/threads failed, falling back to MDR: %s", e)
    # Fallback: Telnyx MDR
    c = get_client()
    try:
        msgs = _list_mdr_messages(c, 200, sort="-created_at")
    except Exception as e:
        return {"threads": [], "count": 0, "error": f"Appwrite and Telnyx both failed: {e}"}

    threads_mdr: dict[str, dict] = {}
    for m in msgs:
        direction = m.get("direction")
        cli = m.get("cli")
        cld = m.get("cld")
        remote: Optional[str] = None
        if direction == "inbound" and cli:
            remote = cli
        elif direction == "outbound" and cld:
            remote = cld
        if not remote:
            continue
        ts = m.get("created_at") or ""
        slot = threads_mdr.get(remote)
        if slot is None:
            threads_mdr[remote] = {
                "remote": remote,
                "last_message": m.get("text"),
                "last_direction": direction,
                "last_at": ts,
                "unread_count": 0,
                "message_count": 0,
            }
            slot = threads_mdr[remote]
        slot["message_count"] += 1
        if direction == "inbound":
            slot["unread_count"] += 1

    thread_list = sorted(
        threads_mdr.values(), key=lambda t: t.get("last_at") or "", reverse=True
    )[:cap]
    return {"threads": thread_list, "count": len(thread_list), "source": "telnyx-mdr"}


def _group_messages_into_threads(docs: list[dict], cap: int) -> list[dict]:
    """Group Appwrite `messages` documents into per-remote threads.

    "Remote" = the number on the OTHER side of the conversation. For
    inbound messages (where the remote party texted us) the remote is
    ``from_number``. For outbound (where we texted them) the remote is
    ``to_number``. We don't know which side is "us" without a tenant
    number list, so the dedupe is by (from, to) pair to make sure
    a back-and-forth between two numbers collapses into one thread.
    """
    threads: dict[tuple[str, str], dict] = {}
    for d in docs:
        from_n = d.get("from_number") or ""
        to_n = d.get("to_number") or ""
        if not from_n and not to_n:
            continue
        # Sorted pair so A→B and B→A collapse into one thread
        pair = tuple(sorted([from_n, to_n]))
        if pair not in threads:
            threads[pair] = {
                "remote": from_n if from_n and from_n != pair[0] else to_n,
                "last_message": d.get("body", ""),
                "last_direction": d.get("direction", ""),
                "last_at": d.get("sent_at") or d.get("received_at") or d.get("$createdAt"),
                "unread_count": 0,
                "message_count": 0,
            }
        slot = threads[pair]
        slot["message_count"] += 1
        if d.get("direction") == "inbound":
            slot["unread_count"] += 1
    thread_list = sorted(
        threads.values(), key=lambda t: t.get("last_at") or "", reverse=True
    )[:cap]
    return thread_list


@router.post("/sms/send")
async def api_sms_send(request: Request) -> dict:
    """Send an SMS via the Telnyx REST API. JSON body: ``{to, from?, text}``.

    Defaults ``from`` to the W3J Personal Twin (``+18444618814``) when not
    provided. We deliberately do NOT raise ``HTTPException`` on Telnyx
    errors — the dashboard expects a 200 with ``ok: false`` so it can
    render the error inline next to the compose box.

    Also upserts a row in Appwrite ``messages`` so the dashboard's message
    list shows the outgoing SMS before the webhook handler confirms it.
    The webhook handler will later update the row with delivered/queued status.

    Note: the Telnyx Python SDK v4 ``MessagesResource`` has no generic
    ``create()`` method, only number-type-specific ones (``send_long_code``,
    ``send_short_code``, ``send_number_pool``, ``send``). To stay
    compatible with the toll-free Personal Twin number without hardcoding
    a "long code vs short code vs toll-free" decision tree here, we hit
    ``/v2/messages`` directly via httpx — same pattern the older ``/api/sms``
    endpoint already uses, and what the SDK itself does under the hood.
    """
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "Invalid JSON body"}
    to = body.get("to")
    text = body.get("text")
    if not to or not text:
        raise HTTPException(400, "to and text are required")

    from_num = body.get("from") or "+18444618814"
    c = get_client()
    try:
        r = httpx.post(
            "https://api.telnyx.com/v2/messages",
            json={"to": to, "from": from_num, "text": text, "type": "SMS"},
            headers={
                "Authorization": f"Bearer {c.creds.api_key}",
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        if r.status_code >= 400:
            return {"ok": False, "error": f"Telnyx {r.status_code}: {r.text[:300]}"}
        body_json = (
            r.json()
            if r.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        msg_id = (body_json.get("data") or {}).get("id")
        # Best-effort: write to Appwrite immediately so the dashboard shows
        # the message as "queued/sent" before the webhook handler confirms.
        try:
            if msg_id:
                from appx.repos import messages as messages_repo
                now_iso = datetime.now(timezone.utc).isoformat()
                messages_repo.upsert_message(
                    tenant_id=_resolve_tenant(request),
                    message_id=msg_id,
                    direction="outbound",
                    from_number=from_num,
                    to_number=to,
                    body=text,
                    status="queued",
                    sent_at=now_iso,
                )
        except Exception as e:
            log.warning("Appwrite upsert_message after /sms/send failed (non-fatal): %s", e)
        return {
            "ok": True,
            "id": msg_id,
            "to": to,
            "from": from_num,
            "text": text,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ───────────────────────────── SMS helpers ──────────────────────────────────
# Telnyx SDK v4's ``MessagesResource`` only exposes send-side methods
# (send_long_code, send_short_code, send_number_pool, send_group_mms, …);
# it has no ``list()``. The only way to enumerate historical messages is
# the **Message Detail Records (MDR)** endpoint
# ``GET /v2/detail_records?filter[record_type]=messaging``. The MDR
# records carry metadata (cli/cld/direction/cost/timestamps) but NOT the
# text body. Per-message ``text`` is available via
# ``GET /v2/messages/{id}`` for messages younger than 10 days (Telnyx
# retention window). For older messages, ``text`` will be ``None``.


def _list_mdr_messages(
    c: Any, page_size: int, sort: str = "-created_at"
) -> list[dict]:
    """Fetch MDR records (messaging) and return them as a list of dicts.

    Telnyx returns them newest-first by default; we pass ``sort`` for
    explicitness. Wrapped in one httpx call so the dashboard endpoints
    stay simple and we can swap implementations later if Telnyx adds a
    proper list endpoint.
    """
    url = "https://api.telnyx.com/v2/detail_records"
    params = {
        "filter[record_type]": "messaging",
        "page[size]": min(max(int(page_size), 1), 200),
        "sort": sort,
    }
    headers = {"Authorization": f"Bearer {c.creds.api_key}"}
    r = httpx.get(url, params=params, headers=headers, timeout=20)
    r.raise_for_status()
    j = r.json()
    return j.get("data") or []


def _enrich_text_via_retrieve(c: Any, message_id: str) -> Optional[str]:
    """Best-effort fetch of the text body for a single message.

    Telnyx only retains message bodies for 10 days, so older IDs will
    fail; we swallow the failure and return ``None`` so the caller can
    fall back to displaying "(no preview)".
    """
    try:
        r = httpx.get(
            f"https://api.telnyx.com/v2/messages/{message_id}",
            headers={"Authorization": f"Bearer {c.creds.api_key}"},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        j = r.json()
        d = (j or {}).get("data") or {}
        return d.get("text")
    except Exception:
        return None


def _shape_message_for_ui(m: dict) -> dict:
    """Map an MDR record to the dashboard UI's expected shape.

    MDR's ``cli`` (caller) and ``cld`` (callee) become the
    ``from``/``to`` envelope the UI already knows how to render. We
    only attempt a per-message retrieve if the record is younger than
    10 days (the Telnyx text-retention window) — older messages return
    ``text: null`` and the UI can fall back to "(no preview)".
    """
    direction = m.get("direction")
    cli = m.get("cli")
    cld = m.get("cld")
    if direction == "inbound":
        from_obj = {"phone_number": cli} if cli else None
        to_list = [{"phone_number": cld}] if cld else []
    else:
        from_obj = {"phone_number": cli} if cli else None
        to_list = [{"phone_number": cld}] if cld else []

    text: Optional[str] = None
    msg_id = m.get("id")
    if msg_id and m.get("created_at"):
        try:
            ts = datetime.fromisoformat(m["created_at"].replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - ts) <= timedelta(days=10):
                # Lazy import to avoid a circular: we already have httpx.
                c = get_client()
                text = _enrich_text_via_retrieve(c, msg_id)
        except Exception:
            pass

    return {
        "id": msg_id,
        "direction": direction,
        "from": from_obj,
        "to": to_list,
        "text": text,
        "created_at": m.get("created_at"),
        "completed_at": m.get("completed_at"),
        "read": None,  # MDR doesn't carry a per-message read flag
        "type": m.get("message_type") or "SMS",
        "errors": m.get("errors"),
    }


# ───────────────────────────── WebSocket event shaping ──────────────────────
# Translates a WebhookContext (from webhooks/handlers/base.py) into the
# JSON shape the browser softphone expects. Kept here in dashboard_api.py
# so the shape lives next to the broker that publishes it.
_EVENT_TYPE_TO_BROKER_TYPE = {
    "call.initiated": "call.incoming",
    "call.answered": "call.answered",
    "call.hangup": "call.hangup",
    "call.machine.detection.ended": "call.machine.detection.ended",
    "recording.saved": "recording.ready",
    "recording.ready": "recording.ready",
    "message.received": "sms.received",
    "sms.message.received": "sms.received",
}


def _extract_phone_any(field) -> Optional[str]:
    """Like _extract_phone in handlers/default.py but tolerant of
    test_event payloads that may pass `from`/`to` as bare strings, dicts,
    or ``{phone_number: ...}`` envelopes. Kept here to avoid a circular
    import between webhooks/handlers and webhooks/dashboard_api.
    """
    if field is None:
        return None
    if isinstance(field, str):
        return field
    if isinstance(field, dict):
        return field.get("phone_number") or field.get("e164") or field.get("number")
    if isinstance(field, list):
        for item in field:
            n = _extract_phone_any(item)
            if n:
                return n
        return None
    return str(field)


def shape_event_for_ws(event_type: Optional[str], payload: dict) -> Optional[dict]:
    """Map a raw Telnyx event_type to the WS event name and data envelope.

    Returns ``None`` for event types the dashboard doesn't care about, so
    the caller can short-circuit. Always returns a small ``data`` dict with
    at least ``call_control_id`` / ``from`` / ``to`` when the event matches.
    """
    if not event_type:
        return None
    broker_type = _EVENT_TYPE_TO_BROKER_TYPE.get(event_type)
    if not broker_type:
        return None
    data: dict[str, Any] = {
        "event_type": event_type,
        "call_control_id": payload.get("call_control_id") or payload.get("id"),
        "from": _extract_phone_any(payload.get("from")),
        "to": _extract_phone_any(payload.get("to")),
        "direction": payload.get("direction"),
        "occurred_at": payload.get("occurred_at") or payload.get("created_at"),
    }
    if event_type == "recording.saved" or event_type == "recording.ready":
        data["recording_id"] = payload.get("recording_id") or payload.get("id")
        rec_urls = payload.get("recording_urls") or payload.get("download_urls") or {}
        if isinstance(rec_urls, dict):
            data["mp3_url"] = rec_urls.get("mp3")
        data["duration_secs"] = payload.get("duration_secs")
    if event_type == "message.received" or event_type == "sms.message.received":
        data["text"] = payload.get("text") or payload.get("body")
        data["message_id"] = payload.get("id")
    if event_type == "call.hangup":
        # Helpful for the "missed call" toast: was this hangup after an
        # answer, or before (caller hung up before we picked up)?
        data["hangup_cause"] = payload.get("hangup_cause") or payload.get("cause")
        data["hangup_source"] = payload.get("hangup_source")
    return {"type": broker_type, "data": data}


def publish_telnyx_event(event_type: Optional[str], payload: dict) -> Optional[dict]:
    """Shape ``payload`` and publish it to the WS broker.

    Returns the shaped event (useful for logging) or None if the event
    type is not interesting to the softphone. Broadcasts to all current
    subscribers because v0.2.x is single-tenant — every browser tab is
    in the same tenant and should see the call. Per-session filtering
    will be re-introduced when the multi-tenant shell lands.
    """
    evt = shape_event_for_ws(event_type, payload)
    if not evt:
        return None
    try:
        ws_broker.publish(None, evt)  # broadcast
    except Exception as e:  # broker must never break the webhook
        log.warning("ws_broker.publish failed: %s", e)
    return evt


# ════════════════════════════════════════════════════════════════════════════
#   SaaS PRIMITIVES — multi-tenant contacts, campaigns, scheduled jobs,
#   mass SMS, and power dialer.
#
#   All endpoints are tenant-scoped via the ``X-Tenant-Id`` header (default
#   ``"default"``). No auth is enforced yet — that is a separate workstream.
#   Scheduler runs as a daemon thread started at import time so the
#   dispatch loop survives any in-process FastAPI restart bug.
# ════════════════════════════════════════════════════════════════════════════

import time as _time  # noqa: E402  (kept here so the section is self-contained)

from webhooks.storage import (  # noqa: E402
    Store,
    get_store,
    _parse_iso,
    _utcnow,
)


# ──────────────────────── tenant helper ────────────────────────────────────
def _tenant_id(request: Request) -> str:
    """Resolve the current tenant id for the request.

    v0.1 read ``X-Tenant-Id`` directly. v1 (Phase A) prefers the
    ``request.state.tenant_id`` set by the auth middleware (which has
    already validated the X-Api-Key or JWT). Falls back to the header
    for callers that haven't migrated yet, and finally to ``"default"``
    for the single-tenant MVP.
    """
    tid = getattr(request.state, "tenant_id", None)
    if tid:
        return tid
    tid = request.headers.get("X-Tenant-Id") or request.headers.get("x-tenant-id")
    return (tid or "default").strip() or "default"


# ──────────────────────── contacts ─────────────────────────────────────────
@router.get("/contacts")
def list_contacts(request: Request) -> dict:
    """List contacts for the current tenant (X-Tenant-Id header)."""
    store = get_store()
    contacts = store.list_contacts(_tenant_id(request))
    return {"contacts": contacts, "count": len(contacts)}


@router.post("/contacts")
async def create_contact(request: Request) -> dict:
    """Create a new contact. Body: ``{name, phone, email?, tags?}``."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    name = (body.get("name") or "").strip()
    phone = (body.get("phone") or "").strip()
    if not name or not phone:
        raise HTTPException(400, "name and phone are required")
    email = body.get("email")
    tags = body.get("tags") or []
    if not isinstance(tags, list):
        tags = [str(tags)]
    store = get_store()
    contact = store.create_contact(_tenant_id(request), name, phone, email, tags)
    return {"ok": True, "contact": contact}


@router.patch("/contacts/{contact_id}")
async def update_contact(contact_id: str, request: Request) -> dict:
    """Partial update: any of ``name``, ``phone``, ``email``, ``tags``."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    tid = _tenant_id(request)
    store = get_store()
    contact = store.update_contact(
        tid, contact_id,
        name=body.get("name"),
        phone=body.get("phone"),
        email=body.get("email"),
        tags=body.get("tags"),
    )
    if not contact:
        raise HTTPException(404, f"contact {contact_id} not found")
    return {"ok": True, "contact": contact}


@router.delete("/contacts/{contact_id}")
def delete_contact(contact_id: str, request: Request) -> dict:
    """Delete a contact by id."""
    tid = _tenant_id(request)
    store = get_store()
    ok = store.delete_contact(tid, contact_id)
    if not ok:
        raise HTTPException(404, f"contact {contact_id} not found")
    return {"ok": True, "id": contact_id}


# ──────────────────────── campaigns ────────────────────────────────────────
@router.get("/campaigns")
def list_campaigns(request: Request) -> dict:
    store = get_store()
    return {"campaigns": store.list_campaigns(_tenant_id(request))}


@router.post("/campaigns")
async def create_campaign(request: Request) -> dict:
    """Create a campaign.

    Body: ``{name, type, from_number?, message?, contact_ids: [...], schedule_at?}``.

    If ``schedule_at`` is in the future, status starts as ``scheduled``;
    otherwise ``draft`` (the user must call ``/launch`` to actually run it).

    Optional Phase C flags: ``test_mode`` (0/1), ``dnc_check_enabled``
    (0/1, default 1), ``time_window_enabled`` (0/1, default 1),
    ``time_window_start`` (int 0-23, default 8), ``time_window_end``
    (int 1-24, default 21). See issues #24 and #25.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    name = (body.get("name") or "").strip()
    type_ = (body.get("type") or "").strip()
    if not name or type_ not in ("sms", "call"):
        raise HTTPException(400, "name and type ('sms'|'call') are required")
    store = get_store()
    campaign = store.create_campaign(
        _tenant_id(request),
        name=name,
        type_=type_,
        from_number=body.get("from_number"),
        message=body.get("message"),
        contact_ids=body.get("contact_ids") or [],
        schedule_at=body.get("schedule_at"),
    )
    # Apply Phase C flags if provided. We do this as a second PATCH so
    # the create_campaign signature stays Phase A clean.
    patch_fields: dict = {}
    if "test_mode" in body:
        patch_fields["test_mode"] = 1 if bool(body["test_mode"]) else 0
    if "dnc_check_enabled" in body:
        patch_fields["dnc_check_enabled"] = 1 if bool(body["dnc_check_enabled"]) else 0
    if "time_window_enabled" in body:
        patch_fields["time_window_enabled"] = 1 if bool(body["time_window_enabled"]) else 0
    if "time_window_start" in body:
        try:
            patch_fields["time_window_start"] = int(body["time_window_start"])
        except (TypeError, ValueError):
            pass
    if "time_window_end" in body:
        try:
            patch_fields["time_window_end"] = int(body["time_window_end"])
        except (TypeError, ValueError):
            pass
    if patch_fields:
        campaign = store.update_campaign(_tenant_id(request), campaign["id"], **patch_fields) or campaign
    return {"ok": True, "campaign": campaign}


@router.get("/campaigns/{campaign_id}")
def get_campaign(campaign_id: str, request: Request) -> dict:
    store = get_store()
    campaign = store.get_campaign(_tenant_id(request), campaign_id)
    if not campaign:
        raise HTTPException(404, f"campaign {campaign_id} not found")
    return {"campaign": campaign}


@router.patch("/campaigns/{campaign_id}")
async def update_campaign(campaign_id: str, request: Request) -> dict:
    """Partial update. Only allowed in draft/scheduled/paused status."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    tid = _tenant_id(request)
    store = get_store()
    existing = store.get_campaign(tid, campaign_id)
    if not existing:
        raise HTTPException(404, f"campaign {campaign_id} not found")
    if existing["status"] not in ("draft", "scheduled", "paused"):
        raise HTTPException(
            409,
            f"campaign in status '{existing['status']}' is not editable",
        )
    campaign = store.update_campaign(
        tid, campaign_id,
        name=body.get("name"),
        from_number=body.get("from_number"),
        message=body.get("message"),
        contact_ids=body.get("contact_ids"),
        schedule_at=body.get("schedule_at"),
    )
    return {"ok": True, "campaign": campaign}


@router.delete("/campaigns/{campaign_id}")
def delete_campaign(campaign_id: str, request: Request) -> dict:
    """Delete a campaign. Forbidden while it's running."""
    tid = _tenant_id(request)
    store = get_store()
    existing = store.get_campaign(tid, campaign_id)
    if not existing:
        raise HTTPException(404, f"campaign {campaign_id} not found")
    if existing["status"] == "running":
        raise HTTPException(409, "cannot delete a running campaign — pause it first")
    ok = store.delete_campaign(tid, campaign_id)
    return {"ok": ok, "id": campaign_id}


@router.post("/campaigns/{campaign_id}/launch")
def launch_campaign(campaign_id: str, request: Request) -> dict:
    """Launch a campaign.

    - If ``schedule_at`` is in the future, status remains ``scheduled``
      (the scheduler will pick it up at run time).
    - If past or absent, transitions to ``running`` and enqueues one
      job per contact for the scheduler to dispatch.
    """
    tid = _tenant_id(request)
    store = get_store()
    camp = store.get_campaign(tid, campaign_id)
    if not camp:
        raise HTTPException(404, f"campaign {campaign_id} not found")
    if camp["status"] == "running":
        return {"ok": True, "campaign": camp, "note": "already running"}
    schedule_at = camp.get("schedule_at")
    run_at_iso = _utcnow()
    if schedule_at:
        try:
            run_dt = _parse_iso(schedule_at)
            if run_dt and run_dt > datetime.now(timezone.utc):
                # Future schedule: stay in 'scheduled', enqueue jobs at
                # the right time so the scheduler picks them up later.
                run_at_iso = schedule_at
        except Exception:
            pass

    # Determine job kind and payload from campaign type.
    contact_ids = camp.get("contact_ids") or []
    if not contact_ids:
        raise HTTPException(400, "campaign has no contacts")

    if camp["type"] == "sms":
        job_kind = "campaign_sms"
    else:
        job_kind = "campaign_call"

    enqueued = 0
    for cid in contact_ids:
        contact = store.get_contact(tid, cid)
        if not contact:
            continue
        payload = {
            "campaign_id": campaign_id,
            "contact_id": cid,
            "from": camp.get("from_number"),
            "to": contact.get("phone"),
            "text": camp.get("message"),
        }
        store.enqueue_job(tid, job_kind, payload, run_at_iso)
        enqueued += 1

    # Transition to running (or leave scheduled if future).
    if run_at_iso == _utcnow() or (
        schedule_at
        and _parse_iso(schedule_at)
        and _parse_iso(schedule_at) <= datetime.now(timezone.utc)
    ):
        store.set_campaign_status(tid, campaign_id, "running",
                                  started_at=_utcnow())
    else:
        store.set_campaign_status(tid, campaign_id, "scheduled")

    camp = store.get_campaign(tid, campaign_id)
    return {"ok": True, "campaign": camp, "enqueued": enqueued}


@router.post("/campaigns/{campaign_id}/pause")
def pause_campaign(campaign_id: str, request: Request) -> dict:
    """Pause a running campaign. In-flight jobs continue but no new ones."""
    tid = _tenant_id(request)
    store = get_store()
    camp = store.get_campaign(tid, campaign_id)
    if not camp:
        raise HTTPException(404, f"campaign {campaign_id} not found")
    if camp["status"] not in ("running", "scheduled"):
        raise HTTPException(409, f"cannot pause a campaign in '{camp['status']}'")
    store.set_campaign_status(tid, campaign_id, "paused")
    return {"ok": True, "campaign": store.get_campaign(tid, campaign_id)}


@router.post("/campaigns/{campaign_id}/resume")
def resume_campaign(campaign_id: str, request: Request) -> dict:
    """Resume a paused campaign. The scheduler re-checks on next tick."""
    tid = _tenant_id(request)
    store = get_store()
    camp = store.get_campaign(tid, campaign_id)
    if not camp:
        raise HTTPException(404, f"campaign {campaign_id} not found")
    if camp["status"] != "paused":
        raise HTTPException(409, f"cannot resume a campaign in '{camp['status']}'")
    # Re-queue any pending jobs that haven't fired yet. We use the original
    # schedule_at if present, else 'now'.
    run_at = camp.get("schedule_at") or _utcnow()
    contact_ids = camp.get("contact_ids") or []
    job_kind = "campaign_sms" if camp["type"] == "sms" else "campaign_call"
    for cid in contact_ids:
        contact = store.get_contact(tid, cid)
        if not contact:
            continue
        # Avoid double-enqueue: only enqueue if the contact has no
        # non-terminal delivery yet.
        existing_dlv = [
            d for d in store.list_deliveries(tid, contact_id=cid, limit=50)
            if f"campaign={campaign_id}" in (d.get("payload_summary") or "")
        ]
        if existing_dlv:
            continue
        payload = {
            "campaign_id": campaign_id,
            "contact_id": cid,
            "from": camp.get("from_number"),
            "to": contact.get("phone"),
            "text": camp.get("message"),
        }
        store.enqueue_job(tid, job_kind, payload, run_at)
    store.set_campaign_status(tid, campaign_id, "running")
    return {"ok": True, "campaign": store.get_campaign(tid, campaign_id)}


@router.get("/campaigns/{campaign_id}/status")
def campaign_status(campaign_id: str, request: Request) -> dict:
    """Live progress: total contacts, per-stat counters, and per-contact rows."""
    tid = _tenant_id(request)
    store = get_store()
    camp = store.get_campaign(tid, campaign_id)
    if not camp:
        raise HTTPException(404, f"campaign {campaign_id} not found")
    contact_ids = camp.get("contact_ids") or []
    per_contact: list[dict] = []
    for cid in contact_ids:
        c = store.get_contact(tid, cid)
        if not c:
            continue
        dlvs = store.list_deliveries(tid, contact_id=cid, limit=10)
        # Filter to deliveries tied to this campaign.
        dlvs = [d for d in dlvs
                if f"campaign={campaign_id}" in (d.get("payload_summary") or "")]
        per_contact.append({
            "contact_id": cid,
            "name": c.get("name"),
            "phone": c.get("phone"),
            "deliveries": dlvs,
            "last_status": dlvs[0]["status"] if dlvs else None,
            "last_telnyx_id": dlvs[0].get("telnyx_id") if dlvs else None,
        })
    return {
        "campaign": camp,
        "total": len(contact_ids),
        "stats": camp.get("stats") or {},
        "per_contact": per_contact,
    }


# ──────────────────────── scheduled SMS ────────────────────────────────────
@router.post("/sms/schedule")
async def schedule_sms(request: Request) -> dict:
    """Enqueue a single SMS for future delivery. Body: ``{to, from?, text, run_at}``."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    to = (body.get("to") or "").strip()
    text = body.get("text")
    run_at = body.get("run_at")
    if not to or not text or not run_at:
        raise HTTPException(400, "to, text, and run_at are required")
    # If run_at is in the past, reject — that's a one-shot send, not a schedule.
    try:
        run_dt = _parse_iso(run_at)
    except Exception:
        raise HTTPException(400, "run_at must be an ISO-8601 timestamp")
    if not run_dt:
        raise HTTPException(400, "run_at is not a valid timestamp")
    if run_dt <= datetime.now(timezone.utc):
        raise HTTPException(400, "run_at must be in the future for /sms/schedule")
    tid = _tenant_id(request)
    store = get_store()
    job = store.enqueue_job(
        tid, "sms",
        {"to": to, "from": body.get("from"), "text": text},
        run_at,
    )
    return {"ok": True, "job": job}


@router.get("/sms/scheduled")
def list_scheduled_sms(request: Request) -> dict:
    tid = _tenant_id(request)
    store = get_store()
    jobs = store.list_jobs(tid, status="pending")
    sms_jobs = [j for j in jobs if j.get("kind") == "sms"]
    return {"scheduled": sms_jobs, "count": len(sms_jobs)}


@router.delete("/sms/scheduled/{job_id}")
def cancel_scheduled_sms(job_id: str, request: Request) -> dict:
    tid = _tenant_id(request)
    store = get_store()
    ok = store.cancel_job(tid, job_id)
    if not ok:
        raise HTTPException(404, f"job {job_id} not found or already running")
    return {"ok": True, "id": job_id}


# ──────────────────────── mass SMS broadcast ───────────────────────────────
@router.post("/sms/broadcast")
async def broadcast_sms(request: Request) -> dict:
    """Send the same SMS to a list of contacts right now.

    Body: ``{from?, text, contact_ids: [...]}``. Returns per-contact results
    plus aggregate counts. Each send is logged in ``deliveries``.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    text = body.get("text")
    contact_ids = body.get("contact_ids") or []
    from_num = body.get("from")
    if not text or not isinstance(contact_ids, list) or not contact_ids:
        raise HTTPException(400, "text and contact_ids (non-empty list) are required")

    tid = _tenant_id(request)
    store = get_store()
    c = get_client()
    results: list[dict] = []
    sent = 0
    failed = 0
    for cid in contact_ids:
        contact = store.get_contact(tid, cid)
        if not contact:
            results.append({"contact_id": cid, "status": "error",
                            "error": "contact not found"})
            failed += 1
            continue
        to = contact.get("phone")
        use_from = from_num or "+18444618814"
        try:
            r = httpx.post(
                "https://api.telnyx.com/v2/messages",
                json={"to": to, "from": use_from, "text": text, "type": "SMS"},
                headers={
                    "Authorization": f"Bearer {c.creds.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=15,
            )
            if r.status_code >= 400:
                err = f"Telnyx {r.status_code}: {r.text[:200]}"
                store.record_delivery(tid, "sms", to, "failed",
                                      contact_id=cid, error=err)
                results.append({"contact_id": cid, "status": "failed",
                                "error": err})
                failed += 1
                continue
            body_json = (r.json() if r.headers.get("content-type", "")
                         .startswith("application/json") else {})
            telnyx_id = (body_json.get("data") or {}).get("id")
            store.record_delivery(tid, "sms", to, "sent",
                                  contact_id=cid, telnyx_id=telnyx_id,
                                  payload_summary=f"to={to} from={use_from}")
            results.append({"contact_id": cid, "status": "sent",
                            "telnyx_id": telnyx_id})
            sent += 1
        except Exception as e:
            store.record_delivery(tid, "sms", to, "failed",
                                  contact_id=cid, error=str(e))
            results.append({"contact_id": cid, "status": "failed",
                            "error": str(e)})
            failed += 1

    return {"ok": True, "sent": sent, "failed": failed, "results": results}


# ──────────────────────── power dialer ─────────────────────────────────────
@router.post("/calls/power-dialer/start")
async def power_dialer_start(request: Request) -> dict:
    """Start a power-dialer session over a list of contacts.

    Body: ``{from, contact_ids, pacing_secs?}``. The first contact is
    dialled immediately, the rest are spaced by ``pacing_secs`` (default 0).
    Each step is a ``power_dialer_step`` scheduled job, which the scheduler
    dispatches as a single outbound call.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    from_num = body.get("from")
    contact_ids = body.get("contact_ids") or []
    pacing = int(body.get("pacing_secs") or 0)
    if not from_num or not isinstance(contact_ids, list) or not contact_ids:
        raise HTTPException(400, "from and contact_ids (non-empty) are required")
    if pacing < 0:
        raise HTTPException(400, "pacing_secs must be >= 0")

    tid = _tenant_id(request)
    store = get_store()
    session_id = _new_id_session()  # type: ignore[name-defined]
    enqueued = 0
    now = datetime.now(timezone.utc)
    for i, cid in enumerate(contact_ids):
        contact = store.get_contact(tid, cid)
        if not contact:
            continue
        run_at = (now + timedelta(seconds=i * pacing)).isoformat()
        payload = {
            "session_id": session_id,
            "step": i,
            "contact_id": cid,
            "from": from_num,
            "to": contact.get("phone"),
        }
        store.enqueue_job(tid, "power_dialer_step", payload, run_at)
        enqueued += 1
    return {
        "ok": True,
        "session_id": session_id,
        "total": enqueued,
        "status": "running",
    }


@router.get("/calls/power-dialer/{session_id}/status")
def power_dialer_status(session_id: str, request: Request) -> dict:
    """Live progress for a power-dialer session."""
    tid = _tenant_id(request)
    store = get_store()
    # Pull all jobs across all statuses for this tenant; filter to ones
    # whose payload carries the session id.
    all_jobs: list[dict] = []
    for s in ("pending", "running", "done", "failed", "cancelled"):
        all_jobs.extend(store.list_jobs(tid, status=s))
    session_jobs = [j for j in all_jobs
                    if (j.get("payload") or {}).get("session_id") == session_id]
    session_jobs.sort(key=lambda j: (j.get("payload") or {}).get("step", 0))

    per_step: list[dict] = []
    for j in session_jobs:
        cid = (j.get("payload") or {}).get("contact_id")
        contact = store.get_contact(tid, cid) if cid else None
        dlvs = store.list_deliveries(tid, contact_id=cid, limit=5) if cid else []
        per_step.append({
            "job_id": j["id"],
            "step": (j.get("payload") or {}).get("step"),
            "contact_id": cid,
            "name": contact.get("name") if contact else None,
            "phone": contact.get("phone") if contact else None,
            "job_status": j["status"],
            "last_error": j.get("last_error"),
            "deliveries": dlvs,
        })

    return {
        "session_id": session_id,
        "total": len(per_step),
        "completed": sum(1 for s in per_step if s["job_status"] == "done"),
        "failed": sum(1 for s in per_step if s["job_status"] == "failed"),
        "pending": sum(1 for s in per_step if s["job_status"] == "pending"),
        "running": sum(1 for s in per_step if s["job_status"] == "running"),
        "steps": per_step,
    }


# ════════════════════════════════════════════════════════════════════════════
#   ASYNC SCHEDULER THREAD
#
#   Polls ``scheduled_jobs`` every 15 s, claims any pending jobs whose
#   ``run_at <= now`` (atomically, under the store lock), and dispatches
#   them by kind. Telnyx is hit via raw ``httpx`` because the SDK v4 has
#   no ``messages.create`` / generic ``calls`` helpers.
# ════════════════════════════════════════════════════════════════════════════

def _dispatch_sms_job(job: dict) -> tuple[bool, Optional[str], Optional[str]]:
    """Send a one-off SMS via Telnyx. Returns (ok, telnyx_id, error)."""
    p = job.get("payload") or {}
    to = p.get("to")
    text = p.get("text")
    from_num = p.get("from") or "+18444618814"
    if not to or not text:
        return (False, None, "missing to/text in payload")
    c = get_client()
    r = httpx.post(
        "https://api.telnyx.com/v2/messages",
        json={"to": to, "from": from_num, "text": text, "type": "SMS"},
        headers={
            "Authorization": f"Bearer {c.creds.api_key}",
            "Content-Type": "application/json",
        },
        timeout=15,
    )
    if r.status_code >= 400:
        return (False, None, f"Telnyx {r.status_code}: {r.text[:200]}")
    body_json = (r.json() if r.headers.get("content-type", "")
                 .startswith("application/json") else {})
    telnyx_id = (body_json.get("data") or {}).get("id")
    return (True, telnyx_id, None)


def _dispatch_call_job(job: dict) -> tuple[bool, Optional[str], Optional[str]]:
    """Place an outbound call via Telnyx /v2/calls."""
    p = job.get("payload") or {}
    to = p.get("to")
    from_num = p.get("from") or "+15078731084"
    if not to:
        return (False, None, "missing to in payload")
    c = get_client()
    # Resolve connection_id from the from number (default to clean line).
    connection_id: Optional[str] = None
    try:
        nums = c.list_all(c.api.phone_numbers.list(), 50)
        for n in nums:
            if n.get("phone_number") == from_num:
                connection_id = n.get("connection_id")
                break
    except Exception:
        pass
    if not connection_id:
        # Fallback: first available call control app.
        try:
            apps = c.list_all(c.api.call_control_applications.list(), 50)
            if apps:
                connection_id = apps[0].get("id")
        except Exception:
            pass
    if not connection_id:
        return (False, None, "no connection_id for from number")
    r = httpx.post(
        "https://api.telnyx.com/v2/calls",
        json={
            "to": to,
            "from": from_num,
            "connection_id": connection_id,
            "webhook_url": "https://bk-jr-api.aixlabs.fun/webhooks/telnyx",
        },
        headers={
            "Authorization": f"Bearer {c.creds.api_key}",
            "Content-Type": "application/json",
        },
        timeout=20,
    )
    if r.status_code >= 400:
        return (False, None, f"Telnyx {r.status_code}: {r.text[:200]}")
    body_json = (r.json() if r.headers.get("content-type", "")
                 .startswith("application/json") else {})
    telnyx_id = (body_json.get("data") or {}).get("call_control_id") \
        or (body_json.get("data") or {}).get("id")
    return (True, telnyx_id, None)


def _run_scheduler_tick(store: Store) -> int:
    """One scheduler pass. Returns number of jobs dispatched (success or fail)."""
    now = _utcnow()
    due = store.claim_due_jobs(now)
    if not due:
        # Log at INFO so operators can confirm the scheduler is alive from
        # the standard log file. The brief explicitly requires this line to
        # be "visible in the webhook.out.log" — a debug-level message would
        # be filtered out at the default root level.
        log.info("scheduler tick at %s: no due jobs", now)
        return 0
    log.info("scheduler tick at %s: %d due job(s)", now, len(due))
    for job in due:
        jid = job["id"]
        tenant = job["tenant_id"]
        kind = job["kind"]
        payload = job.get("payload") or {}
        target = payload.get("to") or "?"
        try:
            if kind in ("sms", "campaign_sms"):
                ok, telnyx_id, err = _dispatch_sms_job(job)
                if ok:
                    store.record_delivery(tenant, "sms", target, "sent",
                                          contact_id=payload.get("contact_id"),
                                          telnyx_id=telnyx_id,
                                          payload_summary=_summary(payload))
                    if kind == "campaign_sms" and payload.get("campaign_id"):
                        store.campaign_stats_bump(tenant, payload["campaign_id"],
                                                   "sent", 1)
                    store.mark_job_done(jid)
                else:
                    store.record_delivery(tenant, "sms", target, "failed",
                                          contact_id=payload.get("contact_id"),
                                          error=err,
                                          payload_summary=_summary(payload))
                    if kind == "campaign_sms" and payload.get("campaign_id"):
                        store.campaign_stats_bump(tenant, payload["campaign_id"],
                                                   "failed", 1)
                    store.mark_job_failed(jid, err or "unknown")
            elif kind in ("campaign_call", "power_dialer_step"):
                ok, telnyx_id, err = _dispatch_call_job(job)
                if ok:
                    store.record_delivery(tenant, "call", target, "sent",
                                          contact_id=payload.get("contact_id"),
                                          telnyx_id=telnyx_id,
                                          payload_summary=_summary(payload))
                    if kind == "campaign_call" and payload.get("campaign_id"):
                        store.campaign_stats_bump(tenant, payload["campaign_id"],
                                                   "sent", 1)
                    store.mark_job_done(jid)
                else:
                    store.record_delivery(tenant, "call", target, "failed",
                                          contact_id=payload.get("contact_id"),
                                          error=err,
                                          payload_summary=_summary(payload))
                    if kind == "campaign_call" and payload.get("campaign_id"):
                        store.campaign_stats_bump(tenant, payload["campaign_id"],
                                                   "failed", 1)
                    store.mark_job_failed(jid, err or "unknown")
            else:
                store.mark_job_failed(jid, f"unknown kind: {kind}")
        except Exception as e:
            log.exception("scheduler dispatch error job=%s", jid)
            store.mark_job_failed(jid, f"dispatch exception: {e}")
    return len(due)


def _summary(payload: dict) -> str:
    """Compact summary for the deliveries table."""
    parts = []
    if "campaign_id" in payload:
        parts.append(f"campaign={payload['campaign_id']}")
    if "session_id" in payload:
        parts.append(f"session={payload['session_id']}")
    if "to" in payload:
        parts.append(f"to={payload['to']}")
    if "from" in payload:
        parts.append(f"from={payload['from']}")
    return " ".join(parts)[:200]


def _new_id_session() -> str:
    import secrets as _s
    return f"pd_{_s.token_urlsafe(10)}"


_scheduler_started = False
_scheduler_lock = threading.Lock()


def _scheduler_loop() -> None:
    """Daemon loop: poll the store every 15s."""
    # Tiny sleep at startup so the FastAPI app finishes wiring up
    # before we start hitting Telnyx from this thread.
    _time.sleep(2)
    while True:
        try:
            store = get_store()
            # _run_scheduler_tick already logs "scheduler tick at <ts>: …"
            # at INFO level, so the operator sees one line per 15 s.
            _run_scheduler_tick(store)
        except Exception as e:
            log.exception("scheduler loop error: %s", e)
        _time.sleep(15)


def start_scheduler_once() -> None:
    """Idempotent: start the daemon thread the first time it's called."""
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        # Eager-init the store so the DB file + tables are ready before
        # the scheduler starts claiming jobs.
        get_store()
        t = threading.Thread(target=_scheduler_loop, name="saas-scheduler",
                             daemon=True)
        t.start()
        _scheduler_started = True
        log.info("saas scheduler thread started")


# Auto-start on import. The server boots the dashboard router as part of
# its lifespan, so any process that imports ``dashboard_api`` (server.py,
# a unit test, etc.) gets a scheduler thread. ``start_scheduler_once``
# is idempotent, so test reimports are safe.
#
# Set ``W3J_DISABLE_SCHEDULER=1`` to skip the auto-start (used by the
# pytest suite so the daemon thread doesn't keep the test process alive
# after the test client returns).
if os.environ.get("W3J_DISABLE_SCHEDULER", "").strip() != "1":
    start_scheduler_once()

