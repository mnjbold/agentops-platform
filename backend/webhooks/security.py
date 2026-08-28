"""Webhook signing / verification helpers (HMAC-SHA256).

Why
---
The Telnyx webhook itself does not currently sign its POSTs to us, so we
historically trusted any POST to ``/webhooks/telnyx``. That is a real gap
for any third-party that starts hitting the endpoint, or for any spoofed
call event from the open Internet.

This module adds a thin HMAC-SHA256 layer that the server can opt into by
setting ``WEBHOOK_HMAC_SECRET`` in the environment. When set:

* Every ``POST /webhooks/telnyx`` and ``POST /admin/test_event`` must
  carry an ``X-Webhook-Signature`` header of the form
  ``t=<unix_ts>,v1=<hex_digest>``.
* The server recomputes ``HMAC-SHA256(secret, f"{ts}.{raw_body}")``
  and rejects with HTTP 401 if it doesn't match.
* The server also rejects requests whose timestamp is more than
  ``WEBHOOK_HMAC_MAX_SKEW`` seconds old (default 300), to prevent replay.

When ``WEBHOOK_HMAC_SECRET`` is NOT set, the server logs a one-line
warning at startup and accepts all POSTs (back-compat for the existing
Telnyx integration). Operators can flip the switch without redeploying
the Telnyx call control app.

Wire format
-----------
We follow the Stripe-style ``t=...,v1=...`` scheme so it's easy to
generate / verify from any tool::

    X-Webhook-Signature: t=1700000000,v1=4f3b2a...c1d

To sign a payload (e.g. for the test endpoint)::

    from webhooks.security import sign_payload
    sig = sign_payload(secret, ts, body_bytes)
    headers = {"X-Webhook-Signature": sig, "X-Webhook-Timestamp": str(ts)}
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import Optional

log = logging.getLogger(__name__)

# Header names (kept short so they fit in the existing Telnyx webhook config).
SIG_HEADER = "X-Webhook-Signature"
TS_HEADER = "X-Webhook-Timestamp"

# Maximum allowed clock skew (5 min) to bound replay risk.
DEFAULT_MAX_SKEW = 300


def _secret() -> str:
    """Read the configured shared secret. Empty string if not set."""
    return os.environ.get("WEBHOOK_HMAC_SECRET", "").strip()


def is_enabled() -> bool:
    """True iff signing is enabled (i.e. a secret is configured)."""
    return bool(_secret())


def _digest(secret: str, ts: int, body: bytes) -> str:
    """Compute the hex HMAC-SHA256 for the given timestamp + raw body."""
    msg = f"{ts}.".encode("utf-8") + body
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def sign_payload(secret: str, ts: int, body: bytes) -> str:
    """Return a ``t=<ts>,v1=<hex>`` signature header value for ``body``."""
    if not secret:
        raise ValueError("Cannot sign: empty secret")
    return f"t={ts},v1={_digest(secret, ts, body)}"


def verify_signature(
    body: bytes,
    signature_header: Optional[str],
    timestamp_header: Optional[str],
    *,
    max_skew: int = DEFAULT_MAX_SKEW,
) -> tuple[bool, str]:
    """Verify an incoming webhook POST.

    Returns ``(True, "ok")`` on success or ``(False, reason)`` on any
    failure. Reasons are short strings so the caller can log them.
    Never raises — invalid input just fails verification.
    """
    if not is_enabled():
        # Signing is off — caller should still treat this as a success.
        return True, "signing-disabled"

    if not signature_header:
        return False, "missing-signature"
    if not timestamp_header:
        return False, "missing-timestamp"

    # Parse "t=...,v1=..."
    parts: dict[str, str] = {}
    for piece in signature_header.split(","):
        if "=" not in piece:
            return False, "malformed-signature"
        k, _, v = piece.partition("=")
        parts[k.strip()] = v.strip()
    ts_str = parts.get("t")
    sig = parts.get("v1")
    if not ts_str or not sig:
        return False, "missing-timestamp-or-sig"

    try:
        ts = int(ts_str)
    except ValueError:
        return False, "bad-timestamp"

    now = int(time.time())
    if abs(now - ts) > max_skew:
        return False, f"stale-timestamp (skew={now - ts}s)"

    expected = _digest(_secret(), ts, body)
    if not hmac.compare_digest(expected, sig):
        return False, "signature-mismatch"
    return True, "ok"


def random_secret() -> str:
    """Generate a fresh 32-byte hex secret (suitable for the env var)."""
    return hashlib.sha256(os.urandom(32)).hexdigest()


if __name__ == "__main__":
    # Quick CLI: `python -m webhooks.security` prints a new secret.
    # Useful for first-time setup or rotation.
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        enabled = is_enabled()
        print(f"WEBHOOK_HMAC_SECRET set: {enabled}")
        if enabled:
            print(f"  secret prefix: {_secret()[:8]}...")
        sys.exit(0 if enabled else 1)
    print(random_secret())
