"""DNC (Do-Not-Call) compliance (issue #25).

Wraps the DNC lookup behind a 30-day cache so the dialer doesn't hit
the upstream provider on every contact. The v1 ships with a seeded
"deny-list" of demo numbers — production wiring to a real DNC API
(DNC.com, OK calling, etc.) is a one-line swap in :func:`_source_lookup`.

Public surface
--------------
* :func:`check_dnc`           — single phone → ``True`` (is on DNC) | ``False``
* :func:`bulk_check_dnc`      — list of phones → ``{phone: bool}``
* :func:`refresh_cache`       — force-refresh a single row (admin)
* :func:`get_cache_row`       — read the cache row without triggering a lookup

The on-disk store lives in ``dnc_cache`` (see :mod:`webhooks.storage`).
The cache row is identified by ``(phone, source)`` and stamped with
``checked_at`` + ``expires_at``; :func:`_cache_valid` honours the
expiry so we never serve a stale row.

Dependencies
------------
* :mod:`webhooks.storage` for the underlying ``Store`` (injected via
  the optional ``store`` arg; defaults to the module-level singleton).
* :mod:`compliance.time_window` is *not* used here — keep the modules
  decoupled so a test can exercise one without the other.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

log = logging.getLogger(__name__)

# Default source identifier used by the API layer; the schema permits
# multiple sources (e.g. "us_dnc", "ca_dnc", "internal_suppress") in
# the future, so every cache row carries its source.
DEFAULT_SOURCE = "us_dnc"

# Re-check interval. 30 days matches the DNC.com refresh SLA.
RECHECK_DAYS = 30

# v1 seed list — a handful of known-bad numbers so the demo + tests
# have something to assert against. Production replaces this with a
# real HTTP call to the DNC provider in ``_source_lookup`` below.
SEED_DNC_NUMBERS: set[str] = {
    "+15551234567",   # telco IVR test number
    "+15559876543",   # DNC scrub service seed
    "+18005551212",   # toll-free DNC demo
    "+18005553434",   # toll-free DNC demo #2
}


# ──────────────────────────── time helpers ──────────────────────────────────


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        # Python's fromisoformat accepts the trailing +00:00 we emit
        return datetime.fromisoformat(s)
    except Exception:
        return None


# ──────────────────────────── upstream source stub ─────────────────────────
# Production wiring: swap this for an httpx call to a real DNC API
# (DNC.com, OK calling, etc.) and pass the API key via env / tenant
# secret. The function returns ``bool`` (True = on DNC).

def _source_lookup(phone: str, source: str = DEFAULT_SOURCE) -> bool:
    """Pure-Python v1 stub. Real HTTP call goes here later.

    The seed list is curated; anything else is "not on DNC" by default.
    Future: call DNC.com and return their ``is_dnc`` field.
    """
    if not phone:
        return False
    # Normalise to E.164 with the leading '+' for the seed set.
    if not phone.startswith("+"):
        # Best-effort: assume NANP if it's 10/11 digits, otherwise skip
        digits = "".join(c for c in phone if c.isdigit())
        if len(digits) == 10:
            phone = "+1" + digits
        elif len(digits) == 11 and digits.startswith("1"):
            phone = "+" + digits
        else:
            return False
    return phone in SEED_DNC_NUMBERS


# ──────────────────────────── storage helpers ───────────────────────────────
# The DNC module needs a Store. We use a tiny indirection so tests can
# pass a fixture store (the conftest patches ``webhooks.storage._store``).

def _store():
    """Return the process-wide Store singleton."""
    # Local import to keep this module importable from tests without
    # the rest of the FastAPI app being live.
    from webhooks.storage import get_store
    return get_store()


# ──────────────────────────── cache row I/O ─────────────────────────────────


def _cache_valid(row: dict) -> bool:
    if not row:
        return False
    exp = _parse_iso(row.get("expires_at") or "")
    if not exp:
        return False
    return exp > _utcnow()


def _upsert_cache(
    store,
    phone: str,
    source: str,
    is_dnc: bool,
    *,
    checked_at: Optional[datetime] = None,
    expires_at: Optional[datetime] = None,
) -> dict:
    """Insert or update one cache row. The store's :func:`_exec` method
    takes a raw SQL string; we hit it directly because the schema is
    Phase C and lives in :mod:`webhooks.storage`."""
    checked_at = checked_at or _utcnow()
    expires_at = expires_at or (checked_at + timedelta(days=RECHECK_DAYS))
    checked_iso = _iso(checked_at)
    expires_iso = _iso(expires_at)
    is_dnc_int = 1 if is_dnc else 0
    with store._lock:
        existing = store._conn.execute(
            "SELECT id FROM dnc_cache WHERE phone = ? AND source = ?",
            (phone, source),
        ).fetchone()
        if existing is None:
            new_id = f"dnc_{int(_utcnow().timestamp() * 1000)}_{abs(hash((phone, source))) % 100000}"
            store._conn.execute(
                "INSERT INTO dnc_cache(id, phone, source, is_dnc, "
                "checked_at, expires_at) VALUES (?,?,?,?,?,?)",
                (new_id, phone, source, is_dnc_int, checked_iso, expires_iso),
            )
        else:
            store._conn.execute(
                "UPDATE dnc_cache SET is_dnc = ?, checked_at = ?, "
                "expires_at = ? WHERE id = ?",
                (is_dnc_int, checked_iso, expires_iso, existing[0]),
            )
    return get_cache_row(phone, source=source, store=store) or {}


def get_cache_row(phone: str, source: str = DEFAULT_SOURCE, store=None) -> Optional[dict]:
    """Return the raw cache row (or ``None``). Exposed so the API
    layer can show "last checked at" without re-querying the source."""
    store = store or _store()
    if not phone or not source:
        return None
    row = store._row(
        "SELECT * FROM dnc_cache WHERE phone = ? AND source = ?",
        (phone, source),
    )
    if row is None:
        return None
    # Normalise: store has INTEGER for is_dnc, callers want bool.
    row["is_dnc"] = bool(int(row.get("is_dnc") or 0))
    return row


# ──────────────────────────── public surface ────────────────────────────────


def check_dnc(
    phone: str,
    source: str = DEFAULT_SOURCE,
    *,
    store=None,
    force_refresh: bool = False,
) -> bool:
    """Return True if ``phone`` is on the DNC list.

    Behaviour
    ---------
    1. Look up the cache row.
    2. If it's fresh (``expires_at`` in the future) and we're not
       ``force_refresh``-ing, return the cached value.
    3. Otherwise call :func:`_source_lookup` and write the new row.
    4. Fall back to "not on DNC" (False) on any storage error so the
       dialer doesn't get wedged by a missing table during boot.
    """
    if not phone:
        return False
    store = store or _store()
    try:
        if not force_refresh:
            row = get_cache_row(phone, source=source, store=store)
            if row and _cache_valid(row):
                return bool(row["is_dnc"])
        is_dnc = bool(_source_lookup(phone, source))
        _upsert_cache(store, phone, source, is_dnc)
        return is_dnc
    except Exception as e:
        log.warning("DNC check failed for %s (source=%s): %s", phone, source, e)
        return False


def bulk_check_dnc(
    phones: Iterable[str],
    source: str = DEFAULT_SOURCE,
    *,
    store=None,
) -> dict[str, bool]:
    """Return ``{phone: is_dnc}`` for every phone. Preserves duplicates."""
    return {p: check_dnc(p, source=source, store=store) for p in phones}


def refresh_cache(
    phone: str,
    source: str = DEFAULT_SOURCE,
    *,
    store=None,
) -> dict:
    """Force-refresh one row and return the updated row."""
    store = store or _store()
    is_dnc = bool(_source_lookup(phone, source))
    return _upsert_cache(store, phone, source, is_dnc)


def cache_stats(*, store=None) -> dict:
    """Return a tiny summary: total rows, expired, DNC, non-DNC.

    The dashboard uses this to surface "we re-checked 124 numbers this
    week" type copy without writing extra SQL.
    """
    store = store or _store()
    try:
        total = store._row("SELECT COUNT(*) AS c FROM dnc_cache") or {"c": 0}
        dnc = store._row(
            "SELECT COUNT(*) AS c FROM dnc_cache WHERE is_dnc = 1"
        ) or {"c": 0}
        now_iso = _iso(_utcnow())
        expired = store._row(
            "SELECT COUNT(*) AS c FROM dnc_cache WHERE expires_at <= ?",
            (now_iso,),
        ) or {"c": 0}
    except Exception:
        return {"total": 0, "dnc": 0, "non_dnc": 0, "expired": 0, "error": "table missing"}
    total_n = int(total.get("c") or 0)
    dnc_n = int(dnc.get("c") or 0)
    return {
        "total": total_n,
        "dnc": dnc_n,
        "non_dnc": total_n - dnc_n,
        "expired": int(expired.get("c") or 0),
    }
