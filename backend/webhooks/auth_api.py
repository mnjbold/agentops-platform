"""Auth API — JWT-based session login (Phase A issue #9).

Endpoints
---------
POST /api/auth/login   {email, password} -> {access_token, expires_at, tenant_id}
GET  /api/auth/me      -> current user (requires Bearer token)

The login endpoint does not require a tenant header — the email is the
unique key (it includes the tenant slug, e.g. ``admin@default.local``).
Returns a 24h JWT signed with the same master key used for Fernet. The
token's ``tid`` claim must match the X-Api-Key tenant (or the
X-Tenant-Id header) on every subsequent request, otherwise the
middleware returns 403.
"""
from __future__ import annotations

import logging
import secrets
import time
from typing import Optional

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request

from webhooks.storage import get_store
from webhooks.tenancy import (
    TenantContext,
    issue_jwt,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ──────────────────────────── helpers ──────────────────────────────────────


def _ctx(request: Request) -> Optional[TenantContext]:
    """Pull the TenantContext the middleware populated. Returns None
    for the login endpoint (no auth required)."""
    return getattr(request.state, "tenant_ctx", None)


# ──────────────────────────── login ────────────────────────────────────────


@router.post("/login")
async def login(request: Request) -> dict:
    """Exchange ``{email, password}`` for a JWT.

    Email format is ``<user>@<tenant>.local`` — the part before ``@`` is
    the local part, the part between ``@`` and ``.local`` is the
    tenant id. This keeps the login form free of a tenant picker
    (single-tenant shells can hide the suffix; multi-tenant ones show it).
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    if not email or not password:
        raise HTTPException(400, "email and password are required")
    if "@" not in email:
        raise HTTPException(400, "email must be in the form <user>@<tenant>.local")
    local, _, host = email.partition("@")
    if not local or not host or not host.endswith(".local"):
        raise HTTPException(400, "email must be in the form <user>@<tenant>.local")
    tenant_id = host[: -len(".local")].strip()
    if not tenant_id:
        raise HTTPException(400, "tenant id is required in the email host")

    store = get_store()
    if not store.get_tenant(tenant_id):
        raise HTTPException(401, "invalid email or password")

    user = store.get_user(tenant_id, email)
    if not user:
        # Run a dummy bcrypt to equalise timing.
        bcrypt.checkpw(b"x", bcrypt.hashpw(b"y", bcrypt.gensalt(rounds=4)))
        raise HTTPException(401, "invalid email or password")
    if not bcrypt.checkpw(password.encode("utf-8"),
                          user["password_hash"].encode("utf-8")):
        raise HTTPException(401, "invalid email or password")

    token, exp = issue_jwt(user_id=user["id"], tenant_id=tenant_id, role=user.get("role") or "admin")
    log.info("Login OK: tenant=%s user=%s", tenant_id, email)
    return {
        "access_token": token,
        "expires_at": exp,
        "token_type": "Bearer",
        "tenant_id": tenant_id,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "role": user.get("role") or "admin",
        },
    }


@router.get("/me")
def me(request: Request) -> dict:
    """Return the current user. The auth middleware has already validated
    the JWT and populated ``request.state.tenant_ctx``."""
    ctx = _ctx(request)
    if ctx is None or ctx.user is None:
        raise HTTPException(401, "authentication required")
    return {
        "user": {
            "id": ctx.user.id,
            "email": ctx.user.email,
            "role": ctx.user.role,
            "tenant_id": ctx.user.tenant_id,
        }
    }


# ──────────────────────── user creation (used by admin_api) ────────────────


def create_initial_user(tenant_id: str, email: str, password: str, role: str = "admin") -> dict:
    """Create a new user row. Used by admin_api when minting a new tenant.

    Returns ``(user_dict, plaintext_password)`` — the caller prints the
    plaintext ONCE to the server log so the operator can copy it.
    """
    store = get_store()
    user_id = "u_" + secrets.token_urlsafe(8)
    pwd_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")
    user = store.create_user(
        user_id=user_id,
        tenant_id=tenant_id,
        email=email,
        password_hash=pwd_hash,
        role=role,
    )
    return user


# NB: never interpolate the password itself. Application logs are shipped to
# aggregators and retained far more widely than the database, so printing it
# here hands the admin account to anyone with log access. The operator already
# knows the value — they set the env var.
_BACKEND_DEV_PASSWORD_LOG = (
    "=================================================================\n"
    "  ADMIN PASSWORD RESET from BACKEND_DEV_PASSWORD env:\n"
    "    email:    %s\n"
    "    password: (not logged — it is the value of BACKEND_DEV_PASSWORD)\n"
    "  The env var is set, so the password is reset on EVERY boot; this\n"
    "  silently reverts any manual rotation. Unset it and redeploy to stop.\n"
    "================================================================="
)


def _reset_user_password(tenant_id: str, email: str, new_password: str) -> bool:
    """Reset the password for an existing user. Returns True if the user
    was found and updated, False otherwise. Called from the bootstrap
    path when ``BACKEND_DEV_PASSWORD`` is set in the env so the operator
    can recover a lost / randomly-generated password without SSH.
    """
    from webhooks.storage import get_store
    store = get_store()
    user = store.get_user(tenant_id, email)
    if not user:
        return False
    pwd_hash = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")
    with store._lock:  # type: ignore[attr-defined]
        store._conn.execute(  # type: ignore[attr-defined]
            "UPDATE users SET password_hash = ? WHERE tenant_id = ? AND email = ?",
            (pwd_hash, tenant_id, email),
        )
        store._conn.commit()  # type: ignore[attr-defined]
    return True
