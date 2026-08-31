"""Multi-region deployment (issue #29).

This module is the runtime side of the multi-region split:

* ``BACKEND_REGION`` env (e.g. ``eu``) determines which SQLite file the
  process reads (US = ``agentops.db``, EU = ``agentops_eu.db``).
* ``tenants.region`` / ``tenants.region_lock`` enforce data residency
  at the storage layer. The auth middleware refuses to write a row
  for a region-locked tenant from the wrong region.
* ``GET /api/health?region=auto`` probes the local DB + the *other*
  region's API via DNS (no shared state required for the heartbeat).
* The :func:`apply_region_routing` hook runs at app startup so the
  correct DB file is open before the first request.

DNS-based cross-region probe
----------------------------
The /api/health endpoint resolves ``us.bkjr-api.getbijou.xyz`` and
``eu.bkjr-api.getbijou.xyz`` to the deployed hosts and hits ``/health``
on each. If DNS doesn't resolve (the other region isn't deployed
yet) the probe reports ``{"status": "unknown", "reason": "dns"}``
instead of failing the call.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Optional

log = logging.getLogger(__name__)

VALID_REGIONS = ("us", "eu")

# Public DNS records for the cross-region health check. The two apps
# are exposed as separate subdomains of the platform's apex domain.
_REGION_HOST = {
    "us": "us.bkjr-api.getbijou.xyz",
    "eu": "eu.bkjr-api.getbijou.xyz",
}

# Local labels for the cross-region probe (the local app reports
# itself as 'us' or 'eu' based on BACKEND_REGION).
_LOCAL_HOST_FALLBACK = {
    "us": "bkjr-api.getbijou.xyz",
    "eu": "bkjr-api.getbijou.xyz",  # EU domain in production
}


# ─────────────────────────── startup hook ────────────────────────────────────


def current_region() -> str:
    """Return this process's configured region ('us' or 'eu')."""
    raw = (os.environ.get("BACKEND_REGION") or "").strip().lower()
    if raw in VALID_REGIONS:
        return raw
    return "us"


def apply_region_routing() -> dict:
    """Run once at app startup.

    * Re-points the Store singleton at the region-specific SQLite file
      (so the EU process reads ``agentops_eu.db``).
    * Logs the active region so operators can see the wiring in
      ``webhook.out.log``.

    Returns a small dict describing what was applied, for /api/health
    to surface without re-reading the env.
    """
    region = current_region()
    # Touch the storage module so its _resolve_db_path() runs against
    # the env we just read.
    try:
        from webhooks.storage import reset_store_for_region  # noqa: WPS433
        reset_store_for_region()
    except Exception as e:
        # Storage may not be importable in the unit-test context.
        log.debug("reset_store_for_region skipped: %s", e)
    log.info("Multi-region routing applied: BACKEND_REGION=%s", region)
    return {
        "region": region,
        "db_path": _db_path_for(region),
    }


def _db_path_for(region: str) -> str:
    try:
        from webhooks.storage import _resolve_db_path  # noqa: WPS433
        return str(_resolve_db_path())
    except Exception:
        return f"agentops{'_eu' if region == 'eu' else ''}.db"


# ─────────────────────────── region-lock enforcement ─────────────────────────


class RegionLockError(Exception):
    """Raised when a write would cross a tenant's region lock."""


def assert_write_allowed(tenant_id: str, tenant_region: Optional[str]) -> None:
    """Refuse to write for a region-locked tenant from the wrong region.

    Called by the storage layer's mutating helpers when the caller
    passes a tenant row. The other-region app is expected to bounce
    its write attempt; this exception maps to a 403 in the API layer.
    """
    if not tenant_id:
        return
    if not tenant_region:
        # Unconfigured region → legacy row. We allow writes so the
        # single-region v0 path keeps working.
        return
    if tenant_region not in VALID_REGIONS:
        return
    if tenant_region == current_region():
        return
    raise RegionLockError(
        f"tenant {tenant_id} is locked to region '{tenant_region}'; "
        f"this process serves '{current_region()}'"
    )


# ─────────────────────────── cross-region health ─────────────────────────────


def _dns_resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        return True
    except Exception:
        return False


def _probe_remote_health(region: str, timeout: float = 3.0) -> dict:
    """Hit /health on the remote region's host; report status.

    Returns::

        {"region": "eu", "host": "eu.bkjr-api.getbijou.xyz",
         "status": "ok" | "down" | "unknown",
         "latency_ms": 42, "checked_at": "..."}
    """
    host = _REGION_HOST.get(region, "")
    if not host or not _dns_resolves(host):
        return {
            "region": region,
            "host": host or None,
            "status": "unknown",
            "reason": "dns",
            "checked_at": _now_iso(),
        }
    url = f"https://{host}/health"
    started = time.monotonic()
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(url, timeout=timeout, context=ctx) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = {"raw": body[:200]}
        latency = int((time.monotonic() - started) * 1000)
        return {
            "region": region,
            "host": host,
            "status": "ok" if resp.status == 200 else "down",
            "http_status": resp.status,
            "latency_ms": latency,
            "payload": payload,
            "checked_at": _now_iso(),
        }
    except urllib.error.HTTPError as e:
        latency = int((time.monotonic() - started) * 1000)
        return {
            "region": region,
            "host": host,
            "status": "down",
            "http_status": e.code,
            "latency_ms": latency,
            "reason": f"http {e.code}",
            "checked_at": _now_iso(),
        }
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        return {
            "region": region,
            "host": host,
            "status": "down",
            "reason": str(e)[:200],
            "checked_at": _now_iso(),
        }


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def get_health_summary() -> dict:
    """Return the local region status + cross-region probes.

    Used by ``GET /api/health?region=auto``. Always returns a dict so
    the dashboard can render the matrix even when one region is down.
    """
    local_region = current_region()
    other_regions = [r for r in VALID_REGIONS if r != local_region]
    local_db_ok, local_db_size = _local_db_health()
    return {
        "local": {
            "region": local_region,
            "db_path": _db_path_for(local_region),
            "db_ok": local_db_ok,
            "db_size_bytes": local_db_size,
            "checked_at": _now_iso(),
        },
        "remote": [_probe_remote_health(r) for r in other_regions],
        "checked_at": _now_iso(),
    }


def _local_db_health() -> tuple[bool, int]:
    """Best-effort check that the local SQLite file is readable."""
    try:
        from webhooks.storage import get_store  # noqa: WPS433
        store = get_store()
        row = store._row("SELECT COUNT(*) AS n FROM tenants")  # type: ignore[attr-defined]
        n = int((row or {}).get("n") or 0)
        # Get the on-disk size for a tiny diag.
        try:
            size = store.path.stat().st_size  # type: ignore[attr-defined]
        except Exception:
            size = 0
        return True, size
    except Exception as e:
        log.debug("local db health check failed: %s", e)
        return False, 0
