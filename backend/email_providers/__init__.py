"""Email provider adapters (issue #27).

Pluggable interface so we can wire SES / SendGrid / Postmark / Resend when
the provider is approved. The :class:`EmailProvider` ABC defines the
contract; :class:`DevProvider` is the concrete dev-mode implementation that
just appends every send to a log file so the dashboard's email channel
is exercisable without a real provider.

The registry below maps the configured provider name (``EMAIL_PROVIDER`` env
var, default ``dev``) to a singleton. The webhook router uses
:func:`get_provider` to resolve a provider for an inbound webhook.
"""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Optional

from .templates import render_template, extract_variables

log = logging.getLogger(__name__)


# Re-export so callers can `from email_providers import render_template`.
render_template_safe = render_template  # legacy alias used by some callers


class EmailProvider(ABC):
    """Contract every email adapter implements.

    Implementations should be safe to instantiate once per process — the
    registry caches them — and should NOT raise on transport failures;
    return a ``{"ok": False, "error": "..."}`` dict instead so the
    dashboard can show the error inline.
    """

    name: str = "abstract"

    @abstractmethod
    def send(
        self,
        *,
        to: str,
        from_addr: str,
        subject: str,
        body: str,
        html: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> dict[str, Any]:
        """Send a single message. Returns ``{"ok": bool, "message_id"?: str,
        "error"?: str, "stub"?: bool}``."""

    def send_template(
        self,
        *,
        to: str,
        from_addr: str,
        template_id: str,
        variables: dict[str, Any],
        subject_template: str,
        body_template: str,
        html_template: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> dict[str, Any]:
        """Render ``subject_template`` / ``body_template`` against
        ``variables`` and dispatch via :meth:`send`. Subclasses can
        override to delegate to a provider-native template engine
        (e.g. SendGrid Dynamic Templates) but the default just does
        a ``str.format_map``-style substitution."""
        from .templates import render_template
        rendered_subject = render_template(subject_template, variables)
        rendered_body = render_template(body_template, variables)
        rendered_html = (
            render_template(html_template, variables) if html_template else None
        )
        return self.send(
            to=to,
            from_addr=from_addr,
            subject=rendered_subject,
            body=rendered_body,
            html=rendered_html,
            reply_to=reply_to,
        )

    @abstractmethod
    def handle_inbound(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Parse a provider-specific inbound webhook payload and return a
        normalised dict with at least:

        ``{"from_addr": str, "to_addr": str, "subject": str, "body": str,
        "html"?: str, "provider_message_id"?: str}``

        Raises ``ValueError`` if the payload is malformed so the caller can
        400 it."""


# ──────────────────────────── Registry ─────────────────────────────────────
# The registry is lazy: a provider is instantiated on first use, not on
# import. That keeps tests fast (no httpx client spun up if you never call
# the email API) and keeps the dev provider available even when env vars
# are unset.

_PROVIDERS: dict[str, type[EmailProvider]] = {}


def register_provider(name: str, cls: type[EmailProvider]) -> None:
    """Register an adapter under ``name`` (e.g. ``"ses"``)."""
    _PROVIDERS[name] = cls


def get_provider(name: Optional[str] = None) -> EmailProvider:
    """Return a singleton provider instance.

    The name comes from (in order):
    1. the ``name`` arg
    2. the ``EMAIL_PROVIDER`` env var
    3. ``"dev"`` — the log-to-file implementation, the safe default when
       nothing is configured.

    The singleton is cached per ``name`` so the underlying HTTP client (if
    any) is reused across calls.
    """
    name = (name or os.environ.get("EMAIL_PROVIDER") or "dev").strip().lower()
    if name not in _PROVIDERS:
        # Lazy import: the stubs raise NotImplementedError on send(), but
        # they are still importable so the operator can flip EMAIL_PROVIDER
        # to one of them once a key is approved.
        from . import email_ses, email_sendgrid, email_postmark, email_resend, email_dev
        # Trigger registration as a side effect of import.
        for _mod in (email_ses, email_sendgrid, email_postmark, email_resend, email_dev):
            pass
    if name not in _PROVIDERS:
        raise ValueError(f"Unknown email provider: {name!r}")
    if not hasattr(get_provider, "_singletons"):
        get_provider._singletons = {}  # type: ignore[attr-defined]
    singletons: dict = get_provider._singletons  # type: ignore[attr-defined]
    inst = singletons.get(name)
    if inst is None:
        inst = _PROVIDERS[name]()
        singletons[name] = inst
        log.info("Email provider initialised: %s", name)
    return inst


# Public re-exports for callers that prefer `from email_providers import EmailProvider`.
__all__ = [
    "EmailProvider",
    "get_provider",
    "register_provider",
    "render_template_safe",
]
