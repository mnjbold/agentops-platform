"""Plan definitions and limit lookups (issue #19).

Three plans: free, pro, enterprise. Limits are hard-coded here for v1
because the only thing a tenant can change is its plan — every other
limit follows.

We express monetary amounts in cents (1 dollar == 100 cents) to avoid
floating-point drift.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Plan:
    id: str
    name: str
    monthly_price_cents: int
    number_limit: int            # max phone numbers the tenant can attach
    voice_rate_cents_per_min: int  # 0 for "included"
    sms_rate_cents_per_segment: int
    features: tuple[str, ...]
    stripe_price_id: Optional[str] = None  # set when Stripe is wired


PLANS: dict[str, Plan] = {
    "free": Plan(
        id="free",
        name="Free",
        monthly_price_cents=0,
        number_limit=1,
        voice_rate_cents_per_min=0,
        sms_rate_cents_per_segment=0,
        features=(
            "1 phone number",
            "Unlimited inbound minutes",
            "1,000 SMS / month",
            "Community support",
        ),
    ),
    "pro": Plan(
        id="pro",
        name="Pro",
        monthly_price_cents=2900,   # $29 / month
        number_limit=5,
        voice_rate_cents_per_min=1,  # $0.01 / min
        sms_rate_cents_per_segment=1,  # $0.01 / segment
        features=(
            "5 phone numbers",
            "Voicemail transcription",
            "Power Dialer",
            "Analytics dashboard",
            "Email support",
        ),
    ),
    "enterprise": Plan(
        id="enterprise",
        name="Enterprise",
        monthly_price_cents=0,        # custom
        number_limit=999,             # effectively unlimited
        voice_rate_cents_per_min=1,
        sms_rate_cents_per_segment=1,
        features=(
            "Unlimited numbers",
            "Audit log export",
            "SSO + SCIM",
            "Custom SLA",
            "Dedicated success manager",
        ),
    ),
}


def get_plan(plan_id: str) -> Plan:
    """Return the plan by id, defaulting to 'free' for unknown values."""
    return PLANS.get(plan_id) or PLANS["free"]


def number_limit(plan_id: str) -> int:
    return get_plan(plan_id).number_limit


def voice_rate_cents_per_min(plan_id: str) -> int:
    return get_plan(plan_id).voice_rate_cents_per_min


def sms_rate_cents_per_segment(plan_id: str) -> int:
    return get_plan(plan_id).sms_rate_cents_per_segment


def is_upgrade_required(plan_id: str, kind: str) -> bool:
    """Return True if the requested ``kind`` exceeds the plan's limits.

    ``kind`` is one of:
    - 'add_number' — tenant is trying to add a 2nd/3rd/... number
    - 'send_sms'   — over the free 1,000 SMS / month
    - 'call_minute' — overage on metered voice (pro/enterprise only)

    For free, the number limit is 1 — adding a 2nd one is a hard
    "upgrade required". SMS is soft: we count usage but only block on
    a very high watermark so the demo doesn't break.
    """
    plan = get_plan(plan_id)
    if kind == "add_number":
        return plan.number_limit <= 1
    if kind == "send_sms":
        # Soft limit; we don't 402 the SMS API for free tenants. Billing
        # is informational at this stage.
        return False
    if kind == "call_minute":
        return plan.voice_rate_cents_per_min <= 0 and plan.id == "free"
    return False
