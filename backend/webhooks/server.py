"""FastAPI webhook server for Telnyx call events.

Endpoints
---------
POST /webhooks/telnyx        — main event receiver (configurable)
GET  /health                 — health check
POST /admin/test_event       — simulate an event (dev only)
GET  /admin/routing          — current agent routing map
POST /admin/routing          — set agent routing map

Usage:
    python -m webhooks.server --port 8080

In production, point the Telnyx Call Control App's webhook URL at this
server's /webhooks/telnyx path (e.g. via ngrok, cloudflared, or fly.io).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

# Make the project root importable when run as a script
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, Depends  # noqa: E402

from telnyx_mcp.clients.telnyx_client import get_client  # noqa: E402
from webhooks.handlers.base import WebhookContext  # noqa: E402
from webhooks.handlers.default import DefaultEventHandler  # noqa: E402
from webhooks.handlers.dispatch import router as dispatch_router  # noqa: E402
from webhooks.dashboard_api import (  # noqa: E402
    router as dashboard_router,
    ws_broker,
    publish_telnyx_event,
)
from webhooks.security import (  # noqa: E402
    SIG_HEADER,
    TS_HEADER,
    is_enabled as hmac_enabled,
    verify_signature,
)

log = logging.getLogger(__name__)

app = FastAPI(title="W3J Telephony Platform — Webhook Receiver")

# CORS for the agentops.getbijou.xyz dashboard (and any *.getbijou.xyz subdomain)
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://agentops.getbijou.xyz",
        "https://portal.getbijou.xyz",
        "https://w3j-telephony.getbijou.xyz",
        "http://localhost:5173",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def close_connection_middleware(request: Request, call_next):
    """Force ``Connection: close`` on every response.

    Why
    ---
    The softphone dashboard polls ``/api/state`` / ``/api/calls/recent`` /
    ``/api/recordings`` / ``/api/messages/threads`` / ``/api/numbers`` every
    2-3s. With HTTP/1.1 keep-alive, the browser opens a new TCP connection
    per poll, the server hands the response back, but the OS holds the
    socket in CLOSE_WAIT until the *next* poll cycle (because of the
    Windows TCP linger default). After ~30 minutes, we hit the per-process
    FD / ephemeral-port budget and uvicorn stops accepting new connections
    (the softphone sees 502s through the tunnel).

    Forcing close after every response shortens the CLOSE_WAIT window from
    minutes to ~10 seconds, which is well under the polling interval. The
    tunnel / Cloudflare handle connection re-use upstream.
    """
    response = await call_next(request)
    response.headers["Connection"] = "close"
    return response

# Process-wide routing map: E.164 → assistant_id
_routing: dict[str, str] = {}

# Process-wide active dispatch sessions: session_id -> {callee_cci, principal_cci, conference_id}
# Set by dispatcher.service.dial_and_bridge(). The webhook handler uses
# this to create the conference on the first leg's answer and join the
# second leg on its answer.
_dispatch_sessions: dict[str, dict] = {}


def register_dispatch_session(session_id: str, callee_cci: str, principal_cci: str) -> None:
    """Register a pending dispatch session with both legs."""
    _dispatch_sessions[session_id] = {
        "callee_cci": callee_cci,
        "principal_cci": principal_cci,
        "conference_id": None,  # set when first leg answers
    }
    log.info("Dispatch session registered: %s", session_id)


def get_dispatch_session(session_id: str) -> Optional[dict]:
    return _dispatch_sessions.get(session_id)


def set_dispatch_conference(session_id: str, conference_id: str) -> None:
    sess = _dispatch_sessions.get(session_id)
    if sess:
        sess["conference_id"] = conference_id
        log.info("Dispatch session %s: conference %s created", session_id, conference_id)


def clear_dispatch_session(session_id: str) -> None:
    _dispatch_sessions.pop(session_id, None)


def set_active_dispatch(conference_id: str, principal_call_control_id: str) -> None:
    """Legacy — kept for backward compat. New code uses register_dispatch_session."""
    log.warning("set_active_dispatch called (legacy); use register_dispatch_session instead")


def load_routing_from_disk(path: Path = _PROJECT_ROOT / "routing.json") -> None:
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                _routing.update(data)
                log.info("Loaded %d routing entries from %s", len(data), path)
        except Exception as e:
            log.warning("Failed to load routing from %s: %s", path, e)


def save_routing_to_disk(path: Path = _PROJECT_ROOT / "routing.json") -> None:
    try:
        path.write_text(json.dumps(_routing, indent=2, sort_keys=True))
    except Exception as e:
        log.warning("Failed to save routing to %s: %s", path, e)


# Load on import
load_routing_from_disk()

# Webhook signing status — set WEBHOOK_HMAC_SECRET in the env to require
# signed POSTs to /webhooks/telnyx and /admin/test_event. Empty/unset
# means we accept anything (back-compat for the live Telnyx integration).
if hmac_enabled():
    log.info("Webhook HMAC signing: ENABLED (header %s required)", SIG_HEADER)
else:
    log.warning(
        "Webhook HMAC signing: DISABLED — %s is not set. "
        "Anyone who can reach /webhooks/telnyx can post events. "
        "Run `python -m webhooks.security.random_secret` to generate one, "
        "set WEBHOOK_HMAC_SECRET in .env, and restart.",
        "WEBHOOK_HMAC_SECRET",
    )


async def _require_signed_body(request: Request) -> bytes:
    """Read the raw body of an incoming webhook and verify its HMAC.

    Returns the parsed JSON dict on success, or raises HTTP 401/400.
    Use as a FastAPI dependency on any POST endpoint that should be
    authenticated.
    """
    raw = await request.body()
    sig = request.headers.get(SIG_HEADER)
    ts = request.headers.get(TS_HEADER)
    ok, reason = verify_signature(raw, sig, ts)
    if not ok:
        log.warning("Webhook signature rejected: %s (path=%s)", reason, request.url.path)
        raise HTTPException(status_code=401, detail=f"signature: {reason}")
    try:
        import json as _json
        return _json.loads(raw.decode("utf-8")) if raw else {}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

# Mount the dispatcher + specialist-switch webhook routes
app.include_router(dispatch_router)

# Mount the dashboard REST API
app.include_router(dashboard_router)

# Load the specialist assistant_id map (so connect_specialist knows what to switch to)
def load_specialist_mapping(path: Path = _PROJECT_ROOT / "agents" / "specialists" / "assistants.json") -> None:
    if not path.exists():
        log.info("No specialist mapping at %s (run scripts/deploy_specialists.py to generate)", path)
        return
    try:
        import json
        mapping = json.loads(path.read_text())
        from webhooks.handlers.dispatch import set_specialist_assistant_ids
        set_specialist_assistant_ids(mapping)
        log.info("Loaded %d specialist mappings from %s", len(mapping), path)
    except Exception as e:
        log.warning("Failed to load specialist mapping from %s: %s", path, e)


load_specialist_mapping()

_handler = DefaultEventHandler(agent_routing=_routing)


def set_routing(routing: dict[str, str]) -> None:
    """Set the entire routing map (replaces existing)."""
    _routing.clear()
    _routing.update(routing)
    _handler.agent_routing = _routing
    save_routing_to_disk()


def add_routing(phone: str, assistant_id: str) -> None:
    """Add or update a single routing entry."""
    _routing[phone] = assistant_id
    _handler.agent_routing = _routing
    save_routing_to_disk()


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "service": "w3j-telephony-webhooks",
        "routing_count": len(_routing),
    }


@app.post("/webhooks/telnyx", dependencies=[Depends(_require_signed_body)])
async def receive_telnyx_event(request: Request) -> dict:
    """Main Telnyx event receiver. Handles the 'event_type' field from
    any Telnyx webhook payload (call.initiated, call.answered, etc.).
    """
    # The body has already been read and HMAC-verified by the dependency.
    try:
        event = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    log.debug("Telnyx event received: %s", json.dumps(event, default=str)[:500])
    ctx = WebhookContext(event, client=get_client())
    result = _handler.handle(ctx)

    # Push a real-time event to every connected browser tab. We do this
    # AFTER the handler so we don't change existing webhook behavior; if
    # the handler raises, the WS publish is skipped (the handler caught
    # the exception internally and returned a status dict).
    try:
        published = publish_telnyx_event(ctx.event_type, ctx.payload)
        if published:
            log.debug("WS broadcast: %s", published["type"])
    except Exception as e:
        # Broker errors must never break the webhook contract.
        log.warning("WS publish failed: %s", e)
    return result


@app.get("/admin/routing")
def get_routing() -> dict:
    return {"routing": _routing, "count": len(_routing)}


@app.post("/admin/routing")
async def set_routing_endpoint(request: Request) -> dict:
    body = await request.json()
    routing = body.get("routing", {})
    if not isinstance(routing, dict):
        raise HTTPException(400, "routing must be a dict {phone: assistant_id}")
    set_routing({str(k): str(v) for k, v in routing.items()})
    return {"ok": True, "routing": _routing}


@app.post("/admin/test_event", dependencies=[Depends(_require_signed_body)])
async def test_event(request: Request) -> dict:
    """Dev-only: simulate a Telnyx event without actually calling."""
    event = await request.json()
    log.warning("TEST EVENT (not from Telnyx): %s", json.dumps(event, default=str)[:300])
    ctx = WebhookContext(event, client=get_client())
    result = _handler.handle(ctx)
    # Mirror the production webhook path: also broadcast via WS so the
    # browser can be tested without a real phone call.
    try:
        published = publish_telnyx_event(ctx.event_type, ctx.payload)
        if published:
            log.warning("WS broadcast (test): %s", published["type"])
    except Exception as e:
        log.warning("WS publish (test) failed: %s", e)
    return result


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    """Real-time event push to the browser softphone.

    Protocol
    --------
    The client opens ``wss://<host>/ws?session_token=<w3j_session_token>``.
    No auth beyond the token (single-tenant v0.2). The server pushes JSON
    events of the shape ``{"type": "call.incoming", "data": {...}}``.

    The server does not require any client-to-server messages after upgrade
    — the connection is one-way. We do still read incoming frames so the
    keepalive / disconnect detection works and so a client can send
    ``{"type": "ping"}`` if it wants.
    """
    token = websocket.query_params.get("session_token", "") or ""
    await websocket.accept()
    # Cache the running loop so the sync webhook handler can publish to us.
    ws_broker.attach_loop(asyncio.get_running_loop())
    q = ws_broker.subscribe(token)
    log.info("WS connect: session=%r broker=%s", token or "<broadcast>", ws_broker.stats())
    try:
        # Send a tiny hello so the client can confirm the upgrade.
        await websocket.send_json({"type": "ws.hello", "data": {"session_token": token}})

        # Heartbeat: many proxies / sandboxed browsers close idle WS
        # connections after ~30s. Send a tiny ping every 20s so the
        # connection looks "active" and doesn't get killed.
        async def heartbeat() -> None:
            try:
                while True:
                    await asyncio.sleep(20)
                    await websocket.send_json({"type": "ws.ping", "data": {"ts": asyncio.get_event_loop().time()}})
            except (WebSocketDisconnect, asyncio.CancelledError):
                return
            except Exception:
                return
        hb_task = asyncio.create_task(heartbeat())

        while True:
            # Concurrently drain the broker queue AND the client receive.
            # We don't expect client→server messages, but reading them
            # keeps the connection healthy and surfaces a clean close.
            send_task = asyncio.create_task(q.get())
            recv_task = asyncio.create_task(websocket.receive_text())
            done, pending = await asyncio.wait(
                {send_task, recv_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            if recv_task in done:
                # Client sent a frame or disconnected. Check which.
                try:
                    _ = recv_task.result()
                    # Got a real text frame. We don't process client→server
                    # messages today; keep looping.
                    continue
                except WebSocketDisconnect:
                    log.info("WS client disconnected cleanly: session=%r", token or "<broadcast>")
                    return
                except Exception:
                    # Any other error: assume disconnect and exit.
                    return
            # send_task in done
            try:
                event = send_task.result()
                await websocket.send_json(event)
            except Exception as e:
                log.debug("WS send failed: %s", e)
                return
    except WebSocketDisconnect:
        pass
    except Exception as e:
        # Suppress the "Cannot call receive once a disconnect has been
        # received" race that FastAPI emits during clean shutdowns. Any
        # other exception is logged at warning level.
        msg = str(e)
        if "disconnect" not in msg.lower():
            log.warning("WS loop error: %s", e)
    finally:
        try:
            hb_task.cancel()
        except Exception:
            pass
        ws_broker.unsubscribe(token, q)
        try:
            await websocket.close()
        except Exception:
            pass
        log.info("WS disconnect: session=%r broker=%s", token or "<broadcast>", ws_broker.stats())


@app.get("/admin/ws/stats")
def ws_stats() -> dict:
    """Lightweight diagnostic for the broker — useful in dev."""
    return ws_broker.stats()


def main() -> int:
    import uvicorn
    parser = argparse.ArgumentParser(description="W3J Telephony webhook receiver")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    uvicorn.run("webhooks.server:app", host=args.host, port=args.port, reload=args.reload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
