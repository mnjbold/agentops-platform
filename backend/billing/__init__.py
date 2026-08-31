"""Billing package — Stripe + plans + usage metering (issue #19).

Public surface:
- ``plans``          — plan definitions + rate lookups
- ``stripe_client``  — wraps the Stripe SDK (stub if SDK not installed)
- ``metering``       — increment usage when calls/SMS complete
- ``service``        — top-level service object used by the router

The FastAPI router lives in ``webhooks.billing``.
"""
from __future__ import annotations

from billing.plans import (  # noqa: F401
    PLANS,
    Plan,
    get_plan,
    is_upgrade_required,
    voice_rate_cents_per_min,
    sms_rate_cents_per_segment,
    number_limit,
)
from billing.stripe_client import (  # noqa: F401
    StripeClient,
    StripeNotConfigured,
    get_stripe_client,
)
from billing.metering import (  # noqa: F401
    record_voice_minutes,
    record_sms_segment,
    current_period,
)
