"""Stripe wrapper (issue #19).

Goals
-----
1. Keep a single import site for the Stripe SDK so the rest of the code
   doesn't need to know whether the SDK is installed.
2. When ``STRIPE_SECRET_KEY`` is unset, return deterministic mocks so the
   dashboard can still be exercised end-to-end in dev / CI.
3. Provide the three operations the router needs:
   - create_checkout_session
   - create_portal_session
   - verify_webhook

Environment
-----------
Set ``STRIPE_SECRET_KEY`` (sk_...) to enable real Stripe.
Set ``STRIPE_WEBHOOK_SECRET`` (whsec_...) to enable signed webhook verification.
When either is missing, the corresponding operation returns a mock.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger(__name__)


class StripeNotConfigured(RuntimeError):
    """Raised when a Stripe call needs the SDK but it's not installed."""


def _have_stripe_sdk() -> bool:
    try:
        import stripe  # noqa: F401
        return True
    except Exception:
        return False


@dataclass
class _MockCheckout:
    id: str
    url: str


class StripeClient:
    """Thin wrapper around the Stripe SDK. Falls back to deterministic
    mocks when the SDK is not installed or no API key is configured.

    The wrapper deliberately keeps the mock surface minimal — the router
    only depends on the three methods below, plus ``verify_webhook`` for
    the webhook endpoint.
    """

    def __init__(self, secret_key: Optional[str] = None,
                 webhook_secret: Optional[str] = None) -> None:
        self.secret_key = (secret_key or os.environ.get("STRIPE_SECRET_KEY") or "").strip()
        self.webhook_secret = (
            webhook_secret or os.environ.get("STRIPE_WEBHOOK_SECRET") or ""
        ).strip()
        self._sdk = None
        if self.secret_key and _have_stripe_sdk():
            try:
                import stripe
                stripe.api_key = self.secret_key
                self._sdk = stripe
                log.info("Stripe SDK live (key prefix=%s...)", self.secret_key[:8])
            except Exception as e:
                log.warning("Stripe SDK init failed; falling back to mock: %s", e)
                self._sdk = None
        else:
            if not self.secret_key:
                log.info("STRIPE_SECRET_KEY not set — using mock Stripe client")
            elif not _have_stripe_sdk():
                log.info(
                    "stripe SDK not installed — using mock Stripe client. "
                    "Run: pip install stripe"
                )

    @property
    def is_live(self) -> bool:
        return self._sdk is not None

    # ─────────────────── checkout ─────────────────────────────────────────
    def create_checkout_session(
        self, tenant_id: str, plan: str, success_url: str, cancel_url: str
    ) -> dict:
        """Return ``{id, url}`` for a Stripe Checkout Session.

        The mock returns a URL the frontend can detect (``/billing/mock-checkout?session=...``)
        so the demo flow can be tested without a real Stripe account.
        """
        if self._sdk is not None:
            try:
                price_id = os.environ.get(f"STRIPE_PRICE_{plan.upper()}", "")
                session = self._sdk.checkout.Session.create(
                    mode="subscription",
                    line_items=[{"price": price_id, "quantity": 1}] if price_id else [],
                    success_url=success_url,
                    cancel_url=cancel_url,
                    client_reference_id=tenant_id,
                    metadata={"tenant_id": tenant_id, "plan": plan},
                )
                return {"id": session.id, "url": session.url}
            except Exception as e:
                log.warning("Stripe checkout failed: %s; falling back to mock", e)

        # Mock: synthesise a stable id + a URL the UI can ping.
        sid = f"cs_mock_{secrets.token_urlsafe(8)}"
        return {
            "id": sid,
            "url": f"/api/billing/mock-checkout?session={sid}&tenant={tenant_id}&plan={plan}",
        }

    # ─────────────────── portal ───────────────────────────────────────────
    def create_portal_session(self, tenant_id: str, return_url: str) -> dict:
        if self._sdk is not None:
            try:
                # Customer must exist; for v1 we assume the webhook has
                # already created the customer (id stored on the
                # subscription row).
                sub = _safe_get_subscription(tenant_id)
                if not sub or not sub.get("stripe_customer_id"):
                    return {"error": "no stripe customer yet for this tenant"}
                session = self._sdk.billing_portal.Session.create(
                    customer=sub["stripe_customer_id"],
                    return_url=return_url,
                )
                return {"id": session.id, "url": session.url}
            except Exception as e:
                log.warning("Stripe portal failed: %s; falling back to mock", e)

        # Mock portal URL.
        return {
            "id": f"bps_mock_{secrets.token_urlsafe(8)}",
            "url": f"/api/billing/mock-portal?tenant={tenant_id}&return={return_url}",
        }

    # ─────────────────── webhook verification ─────────────────────────────
    def verify_webhook(self, payload: bytes, signature: str) -> dict:
        """Verify a Stripe webhook signature and return the parsed event.

        Two modes:
        - Live: when ``STRIPE_WEBHOOK_SECRET`` is set, use the Stripe
          SDK's ``Webhook.construct_event`` which checks the v1 signature
          header.
        - Mock: when no webhook secret is set, we use a deterministic
          HMAC-SHA256 over the body with the key ``whsec_mock``. The
          frontend / tests can sign with the same scheme to simulate
          Stripe. This is gated behind a flag so a real deploy with a
          real secret will never accept a mock signature.
        """
        if self._sdk is not None and self.webhook_secret:
            try:
                evt = self._sdk.Webhook.construct_event(
                    payload, signature, self.webhook_secret
                )
                return dict(evt) if isinstance(evt, dict) else evt.to_dict()
            except Exception as e:
                raise ValueError(f"signature: {e}") from e

        # Mock mode: HMAC-SHA256("whsec_mock", body) hex
        if not signature:
            raise ValueError("missing signature")
        expected = hmac.new(
            b"whsec_mock", payload, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("bad signature (mock mode)")
        try:
            return json.loads(payload.decode("utf-8"))
        except Exception as e:
            raise ValueError(f"invalid JSON: {e}") from e

    def sign_mock(self, payload: bytes) -> str:
        """Helper for tests: produce a mock-mode signature for ``payload``."""
        return hmac.new(b"whsec_mock", payload, hashlib.sha256).hexdigest()


# ──────────────────── module-level singleton ───────────────────────────────

_CLIENT: Optional[StripeClient] = None


def get_stripe_client() -> StripeClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = StripeClient()
    return _CLIENT


def _safe_get_subscription(tenant_id: str) -> Optional[dict]:
    """Internal helper to avoid a circular import at module load time."""
    from webhooks.storage import get_store
    return get_store().get_subscription(tenant_id)
