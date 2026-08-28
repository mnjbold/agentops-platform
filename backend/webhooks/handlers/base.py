"""Webhook base class — common helpers for all event handlers.

Each Telnyx event (call.initiated, call.answered, call.hangup, call.ai_assistant.started,
recording.ready, message.received, etc.) is routed to a handler method based on the
event type. Handlers can:
- log to console / file
- write to a database (Supabase / Google Sheets / SQLite)
- enqueue follow-up actions (transfer, send_sms, hangup)
- trigger other agents (e.g. Telegram bot on incoming SMS)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class WebhookContext:
    """Per-request context passed to every handler.

    Stores the raw event, the routing config (which agent / which number),
    and a reference to the TelnyxClient so handlers can dial/transfer/etc.
    """

    def __init__(
        self,
        event: dict,
        *,
        client: Any = None,
        agent_id: Optional[str] = None,
        call_control_app_id: Optional[str] = None,
    ) -> None:
        self.event = event
        self.client = client
        self.agent_id = agent_id
        self.call_control_app_id = call_control_app_id
        self.event_type = event.get("event_type") or event.get("data", {}).get("event_type")
        self.payload = event.get("data", {}).get("payload", event.get("payload", {}))
        self.call_control_id = (
            self.payload.get("call_control_id")
            or self.payload.get("id")
        )

    def get_client_state(self) -> Optional[dict]:
        """Decode client_state from the payload. Telnyx returns it as-is,
        so if we base64-encoded it on the dial, we need to base64-decode here.

        Returns the parsed dict, or None if not present / not parseable.
        """
        import base64
        import json
        raw = self.payload.get("client_state")
        if not raw:
            return None
        if not isinstance(raw, str):
            return None
        # Try base64-decode first
        for candidate in (raw,):
            try:
                decoded = base64.b64decode(candidate, validate=True).decode("utf-8", errors="strict")
                return json.loads(decoded)
            except Exception:
                pass
        # Fall back to raw JSON
        try:
            return json.loads(raw)
        except Exception:
            return None

    def as_log_line(self) -> str:
        return json.dumps(
            {
                "ts": now_iso(),
                "event_type": self.event_type,
                "call_control_id": self.call_control_id,
                "agent_id": self.agent_id,
                "from": self.payload.get("from"),
                "to": self.payload.get("to"),
                "direction": self.payload.get("direction"),
            },
            default=str,
        )


class BaseEventHandler:
    """Base class — subclass and override event_* methods you care about."""

    def __init__(self, *, log_to_stdout: bool = True) -> None:
        self.log_to_stdout = log_to_stdout

    def handle(self, ctx: WebhookContext) -> dict:
        """Dispatch to the right event_* method. Returns a small status dict."""
        method_name = f"event_{self._event_slug(ctx.event_type)}"
        method = getattr(self, method_name, self.event_unknown)
        try:
            result = method(ctx)
            if self.log_to_stdout:
                log.info("[%s] %s -> %s", ctx.event_type, method_name, result)
            return {"handled": True, "method": method_name, "result": result}
        except Exception as e:
            log.exception("[%s] handler failed", ctx.event_type)
            return {"handled": False, "error": str(e), "method": method_name}

    @staticmethod
    def _event_slug(event_type: Optional[str]) -> str:
        if not event_type:
            return "unknown"
        return event_type.replace(".", "_")

    # ───── default handlers (override in subclasses) ─────
    def event_call_initiated(self, ctx: WebhookContext) -> str:
        return "noop"

    def event_call_answered(self, ctx: WebhookContext) -> str:
        return "noop"

    def event_call_hangup(self, ctx: WebhookContext) -> str:
        return "noop"

    def event_call_bridged(self, ctx: WebhookContext) -> str:
        return "noop"

    def event_call_ai_assistant_started(self, ctx: WebhookContext) -> str:
        return "noop"

    def event_call_ai_assistant_ended(self, ctx: WebhookContext) -> str:
        return "noop"

    def event_recording_ready(self, ctx: WebhookContext) -> str:
        return "noop"

    def event_message_received(self, ctx: WebhookContext) -> str:
        return "noop"

    def event_unknown(self, ctx: WebhookContext) -> str:
        log.debug("Unhandled event type: %s", ctx.event_type)
        return "unhandled"
