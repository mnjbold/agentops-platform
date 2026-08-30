"""Multi-tenant primitives: API-key auth, encrypted secrets, JWT sessions, rate limiting.

This module is the v1 spine of the Phase A backend (issues #2, #3, #6, #9):

* **API keys** — opaque tokens hashed with bcrypt, looked up per request.
* **Tenant secrets** — Fernet-encrypted per-tenant key/value store with a master
  key from the env (or auto-generated to disk on first run).
* **JWT sessions** — HS256, signed with the same master key, carry the
  ``tenant_id`` so the user can talk across endpoints without resending the
  API key on every call.
* **Rate limit** — in-memory token-bucket per ``(tenant_id, route)``.

All four are exposed as FastAPI dependencies so route handlers stay short:

    from webhooks.tenancy import get_current_tenant, get_current_user

    @router.get("/api/contacts")
    def list_contacts(td: TenantDep = Depends(get_current_tenant)):
        return store.list_contacts(td.tenant_id)

``TenantDep`` is a small dataclass so we can hang more fields on it later
(role, user id, etc.) without breaking every signature.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import bcrypt
import jwt
from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)

# ──────────────────────────── master key ───────────────────────────────────
# A single 32-byte secret is the root of both the JWT signing key and the
# Fernet key. We derive the Fernet key via SHA-256 (32 bytes -> url-safe
# base64 of 32 bytes == valid Fernet key).
#
# In production the operator sets TENANT_SECRET_MASTER_KEY in .env. If it
# is not set on first run, we generate a fresh one and write it next to
# the .env file so restarts are stable. The file is git-ignored.

_ENV_VAR = "TENANT_SECRET_MASTER_KEY"
_KEY_FILE_NAME = ".tenant_master_key"
_MASTER_KEY_BYTES = 32  # 256-bit
_JWT_ALGORITHM = "HS256"
_JWT_TTL_SECS = 24 * 60 * 60  # 24 h

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_KEY_FILE_DEFAULT = _PROJECT_ROOT / "backend" / _KEY_FILE_NAME


def _load_master_key() -> bytes:
    """Return the 32-byte master key from env, key file, or fresh generation."""
    raw = os.environ.get(_ENV_VAR, "").strip()
    if raw:
        # Operators may paste a 32-byte hex (64 chars) or any string; we
        # normalise via SHA-256 so Fernet always gets exactly 32 bytes.
        h = hashlib.sha256(raw.encode("utf-8")).digest()
        log.info("Loaded TENANT_SECRET_MASTER_KEY from env (hashed to 32 bytes)")
        return h
    # Try the on-disk file (git-ignored). Operator-friendly: first run
    # creates it, restarts reuse it.
    key_path = Path(os.environ.get("TENANT_SECRET_KEY_FILE", str(_KEY_FILE_DEFAULT)))
    try:
        if key_path.exists():
            data = key_path.read_bytes().strip()
            if len(data) >= 32:
                return hashlib.sha256(data).digest()
    except Exception as e:
        log.warning("Failed to read key file %s: %s", key_path, e)
    # First run — generate, persist, warn loudly.
    fresh = secrets.token_bytes(_MASTER_KEY_BYTES)
    try:
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_bytes(fresh)
        # Restrict perms on POSIX; on Windows the ACL is fine.
        try:
            os.chmod(key_path, 0o600)
        except Exception:
            pass
        log.warning(
            "TENANT_SECRET_MASTER_KEY not set; generated fresh key and wrote to %s. "
            "Add TENANT_SECRET_MASTER_KEY to your .env for portable deployments.",
            key_path,
        )
    except Exception as e:
        log.error("Could not persist master key to %s: %s", key_path, e)
    return hashlib.sha256(fresh).digest()


_MASTER_KEY = _load_master_key()


def _derive_fernet_key(master: bytes) -> bytes:
    """Fernet keys are 32 url-safe-base64 bytes. We base64-encode the SHA-256
    digest of the master key so the same master serves both JWT (raw bytes)
    and Fernet (base64)."""
    import base64
    return base64.urlsafe_b64encode(hashlib.sha256(master).digest())


_FERNET = Fernet(_derive_fernet_key(_MASTER_KEY))


# ──────────────────────────── API keys ──────────────────────────────────────


def _new_api_key() -> str:
    """Opaque, URL-safe, 32 bytes of entropy. Returned ONCE to the caller."""
    return "w3j_" + secrets.token_urlsafe(32)


def hash_api_key(api_key: str) -> str:
    """bcrypt-hash an API key. Cost 12 is the 2026 sweet spot."""
    return bcrypt.hashpw(api_key.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_api_key(api_key: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(api_key.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ──────────────────────────── Fernet secret box ─────────────────────────────


def encrypt_secret(plaintext: str) -> str:
    return _FERNET.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> Optional[str]:
    try:
        return _FERNET.decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


# ──────────────────────────── JWT ───────────────────────────────────────────


def issue_jwt(user_id: str, tenant_id: str, role: str = "admin", ttl: int = _JWT_TTL_SECS) -> tuple[str, int]:
    """Return ``(token, expires_at_unix)``.

    The token carries the user id, tenant id, and role. ``tenant_id`` must
    match the X-Api-Key's tenant (enforced by the auth middleware).
    """
    now = int(time.time())
    exp = now + ttl
    payload = {
        "sub": user_id,
        "tid": tenant_id,
        "role": role,
        "iat": now,
        "exp": exp,
    }
    token = jwt.encode(payload, _MASTER_KEY, algorithm=_JWT_ALGORITHM)
    return token, exp


def decode_jwt(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, _MASTER_KEY, algorithms=[_JWT_ALGORITHM])
    except jwt.PyJWTError as e:
        log.debug("JWT decode failed: %s", e)
        return None


# ──────────────────────────── Rate limit ────────────────────────────────────
# Per (tenant_id, route) token bucket. 100 requests / 60s by default.
# In-memory only — a single-process FastAPI. Restarts reset the buckets.

_DEFAULT_LIMIT = 100
_DEFAULT_WINDOW = 60  # seconds


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class RateLimiter:
    def __init__(self, limit: int = _DEFAULT_LIMIT, window: int = _DEFAULT_WINDOW) -> None:
        self.limit = limit
        self.window = window
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self._lock = threading.Lock()

    def check(self, key: str, route: str) -> tuple[bool, int]:
        """Return ``(allowed, retry_after_seconds)``.

        ``key`` is normally the tenant id but can be "anon" for unauthed
        requests. ``route`` is the request path.
        """
        bucket_key = (key, route)
        now = time.monotonic()
        with self._lock:
            b = self._buckets.get(bucket_key)
            if b is None:
                b = _Bucket(tokens=float(self.limit), last_refill=now)
                self._buckets[bucket_key] = b
            # Refill: add (elapsed / window) * limit tokens
            elapsed = now - b.last_refill
            if elapsed > 0:
                refill = (elapsed / self.window) * self.limit
                b.tokens = min(float(self.limit), b.tokens + refill)
                b.last_refill = now
            if b.tokens >= 1:
                b.tokens -= 1
                return True, 0
            # Compute Retry-After: how long until 1 token is available.
            needed = 1 - b.tokens
            retry = max(1, int(round((needed / self.limit) * self.window)))
            return False, retry

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


# Module-level singleton — one rate limiter for the whole process.
# The limit + window are env-overridable so tests can use a smaller
# bucket (the live default is 100 req / 60 s).
_limit = int(os.environ.get("W3J_RATE_LIMIT", str(_DEFAULT_LIMIT)))
_window = int(os.environ.get("W3J_RATE_WINDOW", str(_DEFAULT_WINDOW)))
_rate_limiter = RateLimiter(limit=_limit, window=_window)


def rate_limit_check(tenant_id: str, route: str) -> tuple[bool, int]:
    return _rate_limiter.check(tenant_id or "anon", route)


def rate_limit_reset() -> None:
    """Test helper — reset the in-memory buckets."""
    _rate_limiter.reset()


def rate_limit_set(limit: int, window: int = 60) -> None:
    """Test helper — replace the limiter with one tuned to the test's needs."""
    global _rate_limiter
    _rate_limiter = RateLimiter(limit=limit, window=window)


# ──────────────────────────── Tenant dependency shape ───────────────────────
# FastAPI dependencies return small dataclasses so route signatures stay
# tidy and we can hang more fields later without breaking callers.


@dataclass
class TenantContext:
    """A scoped view of the request's tenant. Empty ``user`` is OK for
    API-key-only requests; the JWT path fills it in."""
    tenant_id: str
    source: str  # "api_key" | "header" | "jwt"
    user: Optional["UserContext"] = None
    raw_api_key: Optional[str] = field(default=None, repr=False)


@dataclass
class UserContext:
    id: str
    email: str
    role: str
    tenant_id: str


# ──────────────────────────── Master key access (for tests) ─────────────────


def get_master_key_hex() -> str:
    """Return the master key as hex — only used by tests."""
    return _MASTER_KEY.hex()


def get_fernet() -> Fernet:
    return _FERNET


# ──────────────────────────── FastAPI dependency ────────────────────────────
# The auth middleware has already populated ``request.state.tenant_ctx``
# before the request reaches the endpoint. The ``get_current_tenant``
# dependency is a thin convenience that pulls it out so endpoint
# signatures read more naturally:
#
#     @router.get("/api/contacts")
#     def list_contacts(tenant: TenantContext = Depends(get_current_tenant)):
#         store.list_contacts(tenant.tenant_id)
#
# If the middleware is correctly wired, ``tenant`` is always populated for
# non-exempt ``/api/*`` paths (the middleware itself returns 401 first).


def get_current_tenant(request) -> TenantContext:
    """FastAPI dependency: return the auth-resolved TenantContext.

    Raises 401 if the middleware didn't populate one (shouldn't happen on
    ``/api/*`` since the middleware short-circuits, but keeps the
    contract honest for endpoints called from tests / other routers).
    """
    from fastapi import HTTPException  # local import to avoid pulling fastapi at module import
    ctx = getattr(request.state, "tenant_ctx", None)
    if ctx is None:
        raise HTTPException(401, "authentication required")
    return ctx
