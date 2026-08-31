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
from fastapi.responses import JSONResponse  # noqa: E402  (used by /v1/openapi.json)

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
from webhooks.tenancy import (  # noqa: E402
    TenantContext,
    UserContext,
    decode_jwt,
    rate_limit_check,
    verify_api_key,
)
from webhooks.storage import get_store  # noqa: E402
from webhooks.admin_api import router as admin_router  # noqa: E402
from webhooks.auth_api import router as auth_router, create_initial_user  # noqa: E402
from webhooks.voicemail_api import router as voicemail_router  # noqa: E402
from webhooks.workflow_engine import router as workflow_router  # noqa: E402
from webhooks.numbers import router as numbers_router  # noqa: E402
from webhooks.campaigns_extra import router as campaigns_extra_router  # noqa: E402
from webhooks.synthetic_api import router as synthetic_api_router  # noqa: E402
from webhooks.compliance_api import router as compliance_api_router  # noqa: E402
from agent_sdk.assistants import router as assistants_router  # noqa: E402
# Phase E-A live-agent surface (issues #31, #32). Mounted before the
# Phase B business routers so its WebSocket at /api/agents/me/events
# wins over any future /v1/* overlap.
from webhooks.presence import router as presence_router, start_presence_sweeper  # noqa: E402
# Phase B business surface (issues #16, #19, #20, #21). The other Phase B
# worker owns #13/14/15/17/18 — we only mount /v1/* routers that don't
# touch their tables/handlers.
from webhooks.analytics import router as analytics_router  # noqa: E402
from webhooks.billing import router as billing_router  # noqa: E402
from webhooks.audit import router as audit_router  # noqa: E402
from webhooks.openapi_custom import (  # noqa: E402
    branded_swagger_html,
    build_postman_collection,
    build_public_spec,
)
from audit.middleware import audit_middleware  # noqa: E402

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
    # Future-proof: allow any *.getbijou.xyz subdomain without having to
    # add each one to the allow_origins list. The list above takes
    # precedence over the regex.
    allow_origin_regex=r"https://[a-z0-9-]+\.getbijou\.xyz",
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
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


# ──────────────────────────── auth + rate limit middleware ──────────────────
# Per request we resolve a TenantContext from:
#   1) ``X-Api-Key`` (per-tenant) — bcrypt-matched against tenant_secrets.api_key_hash
#   2) ``Authorization: Bearer <jwt>`` (per-user) — decoded, ``tid`` claim
#      must match the X-Api-Key tenant OR the X-Tenant-Id header
#   3) legacy fallback: ``X-Tenant-Id`` header alone (default tenant only)
#
# Routes that opt out of JWT auth list themselves in ``_JWT_EXEMPT_PREFIXES``
# below. Webhook ingestion (signed via WEBHOOK_HMAC_SECRET) is exempt so
# Telnyx's POSTs still flow during deploys when no JWT is in play.
# WebSocket upgrades are skipped entirely (handled in the ws_endpoint).
#
# Auth + rate-limit live in ONE middleware because Starlette runs
# middlewares in reverse-add order — splitting them would let the rate
# limiter see the request *before* auth has populated request.state.
_JWT_EXEMPT_PREFIXES = (
    "/api/auth/login",
    "/api/auth/me",       # 'me' is read by the frontend on app load
    "/api/health",
    "/api/docs",
    "/api/openapi.json",
    "/docs",
    "/openapi.json",
    "/webhooks/telnyx",   # signed separately by WEBHOOK_HMAC_SECRET
    "/admin/test_event",  # signed separately
    "/health",
    # Phase B business surface — the docs/Postman/audit export routes
    # are public; the data routes live under /v1/* and are auth-gated
    # by the same middleware.
    "/v1/docs",
    "/v1/openapi.json",
)


def _is_exempt(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in _JWT_EXEMPT_PREFIXES)


def _resolve_tenant_id_from_api_key(plaintext_key: str) -> Optional[str]:
    """Bcrypt-match the plaintext against every tenant's stored hash.
    Linear scan is fine for v1 (single-digit tenants)."""
    if not plaintext_key:
        return None
    store = get_store()
    for t in store.list_tenants():
        h = t.get("api_key_hash")
        if not h:
            continue
        if verify_api_key(plaintext_key, h):
            return t["id"]
    return None


@app.middleware("http")
async def auth_and_rate_limit_middleware(request: Request, call_next):
    """Combined auth + rate-limit. See the docstring above for why these
    live together."""
    path = request.url.path

    # WebSocket upgrades are handled by the endpoint itself, not middleware.
    if request.scope.get("type") == "websocket":
        return await call_next(request)

    api_key = (
        request.headers.get("X-Api-Key")
        or request.headers.get("x-api-key")
        or ""
    ).strip()
    auth = (request.headers.get("Authorization") or "").strip()
    bearer = ""
    if auth.lower().startswith("bearer "):
        bearer = auth.split(" ", 1)[1].strip()

    tenant_id: Optional[str] = None
    user: Optional[UserContext] = None
    source = "none"

    if api_key:
        tenant_id = _resolve_tenant_id_from_api_key(api_key)
        if not tenant_id:
            # Don't 401 here for exempt paths — let the endpoint decide.
            if path.startswith("/api/") and not _is_exempt(path):
                return _json_error(401, "invalid X-Api-Key")
        else:
            source = "api_key"

    if tenant_id is None and not api_key:
        # Backward-compat: X-Tenant-Id alone defaults to "default" tenant
        # for callers that haven't migrated to API keys yet.
        legacy = (request.headers.get("X-Tenant-Id") or request.headers.get("x-tenant-id") or "").strip()
        if legacy:
            store = get_store()
            if store.get_tenant(legacy):
                tenant_id = legacy
                source = "header"
                log.warning(
                    "DEPRECATED: %s used X-Tenant-Id without X-Api-Key; "
                    "generate a tenant API key and migrate to X-Api-Key.",
                    path,
                )
        elif path.startswith("/api/") and not _is_exempt(path):
            # Public health/docs/auth endpoints are exempt. For everything
            # else, default tenant is allowed without an API key for the
            # v0.1 single-tenant case (matches the pre-Phase-A behaviour).
            store = get_store()
            if store.get_tenant("default"):
                tenant_id = "default"
                source = "header"

    # Decode a JWT if present and bind it to the tenant.
    if bearer:
        claims = decode_jwt(bearer)
        if claims is None:
            if path.startswith("/api/") and not _is_exempt(path):
                return _json_error(401, "invalid or expired token")
        else:
            jwt_tenant = claims.get("tid")
            jwt_user_id = claims.get("sub")
            # Tenant binding: the JWT's tid must match the API key's tenant
            # (or the X-Tenant-Id header) — otherwise 403.
            if tenant_id and jwt_tenant and tenant_id != jwt_tenant:
                return _json_error(403, f"token tenant {jwt_tenant} does not match request tenant {tenant_id}")
            if tenant_id is None and jwt_tenant:
                tenant_id = jwt_tenant
                source = source if source != "none" else "jwt"
            if jwt_user_id and tenant_id:
                store = get_store()
                u = store.get_user_by_id(jwt_user_id)
                if u and u["tenant_id"] == tenant_id:
                    user = UserContext(
                        id=u["id"],
                        email=u["email"],
                        role=u.get("role") or "admin",
                        tenant_id=u["tenant_id"],
                    )

    # Final gate: protected /api/* routes need *some* auth context.
    if path.startswith("/api/") and not _is_exempt(path):
        if tenant_id is None:
            return _json_error(401, "authentication required")

    # Build the context and stash on request.state.
    ctx = TenantContext(
        tenant_id=tenant_id or "default",
        source=source,
        user=user,
        raw_api_key=api_key or None,
    )
    request.state.tenant_ctx = ctx
    request.state.tenant_id = ctx.tenant_id  # convenience for endpoints

    # Rate limit (after auth so we have a tenant id). Skip exempt paths
    # and WebSocket upgrades.
    if path.startswith("/api/") and not _is_exempt(path):
        allowed, retry = rate_limit_check(tenant_id or "anon", path)
        if not allowed:
            log.warning("Rate limit hit: tenant=%s path=%s retry=%ds", tenant_id, path, retry)
            from fastapi.responses import JSONResponse
            resp = JSONResponse(
                status_code=429,
                content={"detail": "rate limit exceeded", "retry_after": retry},
            )
            resp.headers["Retry-After"] = str(retry)
            resp.headers["Connection"] = "close"
            return resp

    return await call_next(request)


def _json_error(status: int, detail: str):
    """Helper to return a JSONResponse from middleware."""
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=status, content={"detail": detail})

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

# Mount the Phase B telephony surface FIRST so the new /api/numbers
# endpoints (#15) take precedence over the legacy /api/numbers in
# dashboard_api.py. FastAPI matches routes in registration order.
app.include_router(numbers_router)

# Mount the dashboard REST API
app.include_router(dashboard_router)

# Mount the Phase A admin (tenants, secrets), auth (JWT login), and
# voicemail/recording routers.
app.include_router(admin_router)
app.include_router(auth_router)
app.include_router(voicemail_router)

# Mount the Phase B routers: workflows (#13),
# campaigns extras (#17 + #18), and AI assistants (#14).
app.include_router(workflow_router)
app.include_router(campaigns_extra_router)
app.include_router(assistants_router)

# Phase C — compliance + mass test (issues #24, #25). The other Phase C
# worker owns #22/23 (WhatsApp + SMS blast) and is mounted separately.
app.include_router(synthetic_api_router)
app.include_router(compliance_api_router)

# Phase B business surface (#16, #19, #20). Mounted at /v1/* so the
# public docs can advertise a stable v1 contract.
app.include_router(analytics_router)
app.include_router(billing_router)
app.include_router(audit_router)

# Phase C #22 + #23 — WhatsApp + SMS blast (rescued; storage methods
# live in storage.py, the routers were authored by the C1 worker).
try:
    from webhooks.whatsapp_api import router as whatsapp_router
    from webhooks.suppression_api import router as suppression_router
    from webhooks.sms_scheduler import router as sms_scheduler_router
    app.include_router(whatsapp_router)
    app.include_router(suppression_router)
    app.include_router(sms_scheduler_router)
except Exception as _exc:  # pragma: no cover
    log.warning("Phase C outbound routers not mounted: %s", _exc)

# Phase E-A — live-agent presence (#31) + call queue (#32). The
# queue router is loaded lazily so issue #31's commit can ship
# without issue #32's router (each issue is pushed independently).
app.include_router(presence_router)
try:
    from webhooks.queue import router as queue_router
    app.include_router(queue_router)
except Exception as _exc:  # pragma: no cover
    log.warning("Phase E queue router not mounted yet: %s", _exc)

# Phase E-B #40 — skill routing admin. Mounted alongside queue so the
# editor's skill dropdown and the dashboard's chips can fetch /api/skills
# in a single request.
try:
    from webhooks.skills import router as skills_router
    app.include_router(skills_router)
except Exception as _exc:  # pragma: no cover
    log.warning("Phase E-B skills router not mounted: %s", _exc)

# Phase D #26 + #27 — Meetings (Daily.co) + Email (provider-agnostic).
try:
    from webhooks.meetings import router as meetings_router
    from webhooks.email import router as email_router
    app.include_router(meetings_router)
    app.include_router(email_router)
except Exception as _exc:  # pragma: no cover
    log.warning("Phase D adjacent routers not mounted: %s", _exc)

# Phase D #28 + #29 + #30 — DNS, network quality, regions, branding.
try:
    from webhooks.dns_api import router as dns_router
    from webhooks.network_quality import router as network_quality_router
    from webhooks.regions_api import router as regions_router
    from webhooks.branding_api import router as branding_router
    app.include_router(dns_router)
    app.include_router(network_quality_router)
    app.include_router(regions_router)
    app.include_router(branding_router)
except Exception as _exc:  # pragma: no cover
    log.warning("Phase D infra routers not mounted: %s", _exc)


# ──────────────────── audit middleware (#20) ──────────────────────────────
# Append a row to audit_log for every /api/* (and /v1/*) request AFTER
# the response is sent. The middleware does not block the response; if
# the append fails we log a warning and move on.
@app.middleware("http")
async def _audit_mw(request: Request, call_next):
    return await audit_middleware(request, call_next)


# ──────────────────── public OpenAPI (#21) ───────────────────────────────
# /v1/docs and /v1/openapi.json are the public docs. Internal /api/admin/*
# is hidden from the spec; everything else under /v1/* + /api/* (minus
# admin) is shown. Postman collection is at /v1/docs/postman.json.
@app.get("/v1/openapi.json", include_in_schema=False)
def v1_openapi_json() -> JSONResponse:
    spec = build_public_spec(app)
    return JSONResponse(spec)


@app.get("/v1/docs", include_in_schema=False)
def v1_docs():
    return branded_swagger_html(openapi_url="/v1/openapi.json", title="agentops API v1")


@app.get("/v1/docs/postman.json", include_in_schema=False)
def v1_postman() -> JSONResponse:
    return JSONResponse(build_postman_collection(app))

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

# ──────────────────────── Phase A startup migration ────────────────────────
# On every boot, push legacy .env credentials into the default tenant's
# secret store. We log a warning so the operator knows to remove them
# from .env (so the secret store is the only source of truth).
def _migrate_legacy_env_to_secrets() -> None:
    try:
        from webhooks.tenancy import encrypt_secret
        store = get_store()
        if not store.get_tenant("default"):
            return
        # Mapping: .env key -> secret-store key
        env_map = {
            "TELNYX_WEBRTC_USERNAME": "telnyx_webrtc_username",
            "TELNYX_WEBRTC_PASSWORD": "telnyx_webrtc_password",
            "TELNYX_WEBRTC_CONNECTION_ID": "telnyx_webrtc_connection_id",
            "TELNYX_SOFTPHONE_FROM": "telnyx_softphone_from",
            "TELNYX_SOFTPHONE_CONNECTION_ID": "telnyx_softphone_connection_id",
        }
        any_migrated = False
        for env_key, secret_key in env_map.items():
            val = os.environ.get(env_key)
            if not val:
                continue
            existing = store.get_secret("default", secret_key)
            if existing is not None:
                # Don't overwrite — operator may have rotated manually.
                continue
            store.upsert_secret("default", secret_key, encrypt_secret(val))
            any_migrated = True
            log.warning(
                "Migrated %s from .env into default tenant secret store "
                "(key=%s). Remove it from backend/.env so the secret store "
                "is the only source of truth.",
                env_key, secret_key,
            )
        if any_migrated:
            # Seed the admin user on first boot (only if no users exist
            # for the default tenant yet).
            if not store.get_user("default", f"admin@default.local"):
                import secrets as _s
                # Dev-mode: honor a known password so the dev script can
                # print credentials the user can actually type. In prod,
                # leave the env unset and a random password is generated.
                pwd = os.environ.get("BACKEND_DEV_PASSWORD") or _s.token_urlsafe(18)
                create_initial_user(
                    tenant_id="default",
                    email="admin@default.local",
                    password=pwd,
                    role="admin",
                )
                log.warning(
                    "=================================================================\n"
                    "  DEFAULT TENANT ADMIN (one-time setup):\n"
                    "    email:    admin@default.local\n"
                    "    password: %s\n"
                    "  Use POST /api/auth/login to exchange this for a JWT.\n"
                    "  This password is NOT recoverable — rotate via /api/admin/tenants/default/rotate-key + /api/auth/reset.\n"
                    "=================================================================",
                    pwd,
                )
    except Exception as e:
        log.warning("Legacy env->secret migration failed (non-fatal): %s", e)


_migrate_legacy_env_to_secrets()

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


@app.on_event("startup")
async def _on_startup() -> None:
    """Background tasks that need the running event loop."""
    try:
        start_presence_sweeper()
    except Exception as e:
        log.warning("could not start presence sweeper: %s", e)


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
