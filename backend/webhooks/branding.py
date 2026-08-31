"""White-label tenant theming (issue #30).

Each tenant can override the platform's chrome with their own:

* logo / favicon URLs
* primary + accent colour
* support email
* custom domain (CNAME → bkjr-api.getbijou.xyz by default)

The brand is stored on ``tenants.brand_json`` as a JSON blob. The
public ``GET /api/tenant/brand?subdomain=acme`` (and the
``Host:``-header lookup) reads this blob and returns the safe-for-public
shape (no secrets, no internal ids). The admin endpoint
``PUT /api/admin/tenants/{id}/brand`` mutates it.

Custom domain routing
---------------------
The same FastAPI app serves every tenant. On each request the
``Host`` header is matched against:

1. ``tenants.brand_json.custom_domain`` (if set + verified)
2. ``<subdomain>.agentops.com`` style subdomains (e.g. ``acme.agentops.com``
   looks up tenant id ``acme``)
3. The default apex host (``bkjr-api.getbijou.xyz``) — no per-tenant brand

The brand is exposed in JSON to the frontend (login page + shell)
so it can apply CSS variables BEFORE first paint — see the inline
script block in ``frontend/index.html``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import socket
from typing import Optional

log = logging.getLogger(__name__)

# Sensible defaults — these match the rest of the design system.
DEFAULT_BRAND: dict = {
    "logo_url": "",
    "favicon_url": "",
    "primary_color": "#7c5cff",
    "accent_color": "#22c55e",
    "name": "agentops",
    "support_email": "",
    "custom_domain": "",
    "custom_domain_verified": False,
}

# A safe-for-public subset. Internal fields (none today, but future-
# proofing) are stripped before being served to the login page.
_PUBLIC_KEYS = (
    "logo_url",
    "favicon_url",
    "primary_color",
    "accent_color",
    "name",
    "support_email",
    "custom_domain",
    "custom_domain_verified",
)

# Subdomain match: ``acme.agentops.com`` → tenant id ``acme``.
# Apex (www, app, bkjr-api) doesn't resolve to a tenant.
_APEX_LABELS = {"www", "app", "bkjr-api", "api", "agentops", ""}
_SUBDOMAIN_RE = re.compile(r"^([a-z0-9][a-z0-9-]{0,62})\.agentops\.com$", re.IGNORECASE)


# ─────────────────────────── domain parsing ──────────────────────────────────


def tenant_from_host(host: str) -> Optional[str]:
    """Return the tenant id for a given ``Host`` header, or None.

    Resolution order:
      1. If ``host`` matches ``<subdomain>.agentops.com`` → ``subdomain``
      2. If ``host`` matches a stored ``custom_domain`` → return that
         tenant's id
      3. Otherwise None (apex / bkjr-api.getbijou.xyz)
    """
    if not host:
        return None
    h = host.strip().lower().split(":")[0]
    m = _SUBDOMAIN_RE.match(h)
    if m:
        sub = m.group(1)
        if sub and sub not in _APEX_LABELS:
            return sub
    # Custom domain lookup
    try:
        from webhooks.storage import get_store  # noqa: WPS433
        store = get_store()
        t = store.find_tenant_by_domain(h)
        if t:
            return t.get("id")
    except Exception as e:
        log.debug("custom_domain lookup failed: %s", e)
    return None


def is_apex_host(host: str) -> bool:
    """True when the host is the platform apex (no tenant brand)."""
    if not host:
        return True
    h = host.strip().lower().split(":")[0]
    # bkjr-api.getbijou.xyz and friends
    if h.endswith(".getbijou.xyz") or h == "getbijou.xyz":
        return True
    if h.endswith(".agentops.com") and h not in _APEX_LABELS:
        return False
    return h in _APEX_LABELS


# ─────────────────────────── brand normalisation ────────────────────────────


_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")


def _normalise_color(value: Optional[str], fallback: str) -> str:
    if not value or not isinstance(value, str):
        return fallback
    v = value.strip()
    if not _HEX_COLOR_RE.match(v):
        return fallback
    return v


def normalise_brand(brand: Optional[dict]) -> dict:
    """Return a brand dict with safe defaults + validated colours.

    Used both on read (so the public endpoint never returns partial
    data) and on write (so the admin PUT stores a clean shape).
    """
    out = dict(DEFAULT_BRAND)
    if isinstance(brand, dict):
        for k in _PUBLIC_KEYS:
            v = brand.get(k, out[k])
            if k.endswith("_color"):
                v = _normalise_color(v if isinstance(v, str) else None, out[k])
            elif isinstance(v, str):
                v = v.strip()
            elif isinstance(v, bool):
                pass
            else:
                v = out[k]
            out[k] = v
    # Coerce the bool explicitly (admin might send a truthy string)
    out["custom_domain_verified"] = bool(out.get("custom_domain_verified"))
    return out


def public_brand(brand: Optional[dict]) -> dict:
    """Return the safe-for-public subset of a brand dict.

    This is the shape served to the login page — no internal ids, no
    tokens, no Telnyx refs.
    """
    n = normalise_brand(brand)
    return {k: n.get(k) for k in _PUBLIC_KEYS}


# ─────────────────────────── CNAME verification ─────────────────────────────


def cname_target() -> str:
    """Where the tenant's ``custom_domain`` should CNAME to.

    Override per-region with the ``BRAND_CNAME_TARGET`` env var so
    the US and EU apps can point at their own load balancer.
    """
    return (
        os.environ.get("BRAND_CNAME_TARGET", "").strip()
        or "brand.bkjr-api.getbijou.xyz"
    )


def verify_custom_domain(domain: str) -> dict:
    """Best-effort CNAME verification for ``domain``.

    Returns ``{"verified": bool, "expected": str, "actual": str|None,
    "reason": str|None}``. Never raises.
    """
    domain = (domain or "").strip().lower()
    expected = cname_target()
    if not domain:
        return {
            "verified": False,
            "expected": expected,
            "actual": None,
            "reason": "empty domain",
        }
    try:
        infos = socket.getaddrinfo(domain, None)
    except Exception as e:
        return {
            "verified": False,
            "expected": expected,
            "actual": None,
            "reason": f"dns: {e}",
        }
    # CNAME shows up in the ``canonname`` field of getaddrinfo. If the
    # user pointed an A record instead we can't tell, so we accept
    # anything that resolves and let the operator check externally.
    targets = sorted({i[3] for i in infos if i and i[3]})
    if not targets:
        return {
            "verified": False,
            "expected": expected,
            "actual": None,
            "reason": "no resolution",
        }
    matched = any(t.rstrip(".").lower() == expected.rstrip(".").lower() for t in targets)
    return {
        "verified": matched,
        "expected": expected,
        "actual": targets[0] if len(targets) == 1 else targets,
        "reason": None if matched else "cname does not match expected target",
    }
