"""Default event handler — logs everything and dispatches in-flight actions
to the agent builder (so an incoming call automatically starts the right AI
Assistant based on the phone number that was called). Also writes to all
configured connectors (SQLite, Sheets, Supabase, etc).
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from connectors.base import CallEvent
from connectors.registry import get_registry
from telnyx_mcp.clients.telnyx_client import get_client

from webhooks.handlers.base import BaseEventHandler, WebhookContext, now_iso

log = logging.getLogger(__name__)


def _extract_phone(field) -> Optional[str]:
    """Telnyx webhooks put phones in either ``"+1555..."`` or
    ``{"phone_number": "+1555..."}`` depending on the event type. Be lenient."""
    if field is None:
        return None
    if isinstance(field, str):
        return field
    if isinstance(field, dict):
        return field.get("phone_number") or field.get("e164") or field.get("number")
    return str(field)


class DefaultEventHandler(BaseEventHandler):
    """Routes calls to the right AI Assistant based on the called number.

    Looks up the number in ``AGENT_ROUTING`` (set at deploy time) and:
    * answers the call
    * starts the configured AI Assistant
    * logs the call event to all enabled connectors
    """

    def __init__(self, agent_routing: Optional[dict[str, str]] = None) -> None:
        super().__init__(log_to_stdout=True)
        # Map "called E.164" → "assistant_id"
        self.agent_routing = agent_routing or {}
        self.registry = get_registry()

    def _log_event(self, ctx: WebhookContext, notes: str = "") -> None:
        """Persist to all configured connectors."""
        try:
            from_ = _extract_phone(ctx.payload.get("from"))
            to_ = _extract_phone(ctx.payload.get("to"))
            event = CallEvent(
                event_type=ctx.event_type or "unknown",
                call_control_id=ctx.call_control_id,
                agent_id=ctx.agent_id,
                direction=ctx.payload.get("direction"),
                from_number=from_,
                to_number=to_,
                notes=notes,
                extra={"call_control_app_id": ctx.call_control_app_id},
            )
            written = self.registry.write_event(event)
            log.debug("event written to: %s", written)
        except Exception as e:
            log.warning("Failed to write event to connectors: %s", e)

    def event_call_initiated(self, ctx: WebhookContext) -> str:
        called = _extract_phone(ctx.payload.get("to"))
        from_ = _extract_phone(ctx.payload.get("from"))
        log.info("Incoming call to %s from %s (cci=%s)", called, from_, ctx.call_control_id)
        self._log_event(ctx, notes="incoming")
        return "logged"

    def event_call_answered(self, ctx: WebhookContext) -> str:
        # Check for dispatch client_state FIRST (special flow).
        # client_state is base64-encoded by the dispatcher, decode it.
        cs = ctx.get_client_state()
        if cs and cs.get("purpose") == "dispatch":
            return self._handle_dispatch_answer(ctx, cs)
        # For inbound: the called number (to) is the agent's number — look up by `to`.
        # For outbound: the from number is the agent's number — look up by `from`.
        # Either side matches → start the configured AI assistant.
        called = _extract_phone(ctx.payload.get("to"))
        from_ = _extract_phone(ctx.payload.get("from"))
        direction = ctx.payload.get("direction")
        assistant_id = None
        if called and called in self.agent_routing:
            assistant_id = self.agent_routing[called]
        elif from_ and from_ in self.agent_routing:
            assistant_id = self.agent_routing[from_]
        if not assistant_id:
            self._log_event(ctx, notes="answered_no_routing")
            return "no_routing"
        try:
            ctx.client.start_ai_assistant(ctx.call_control_id, assistant_id)
            log.info(
                "Started assistant %s on call %s (direction=%s, to=%s, from=%s)",
                assistant_id, ctx.call_control_id, direction, called, from_,
            )
            self._log_event(ctx, notes=f"started_assistant:{assistant_id}")
            return f"started_assistant:{assistant_id}"
        except Exception as e:
            log.exception("Failed to start assistant: %s", e)
            self._log_event(ctx, notes=f"error:{e}")
            return f"error:{e}"

    def _handle_dispatch_answer(self, ctx: WebhookContext, cs: dict) -> str:
        """Bridge a dispatch leg into the conference. Creates the conference
        on the first leg's answer, joins the second leg."""
        from webhooks.server import (
            clear_dispatch_session, get_dispatch_session, set_dispatch_conference,
        )
        session_id = cs.get("session_id")
        role = cs.get("role")  # "callee" or "principal"
        if not session_id:
            log.warning("Dispatch event missing session_id: %s", cs)
            return "dispatch_no_session"
        sess = get_dispatch_session(session_id)
        if not sess:
            log.warning("Dispatch session %s not registered", session_id)
            return "dispatch_unknown_session"
        conf_id = sess.get("conference_id")
        try:
            if conf_id is None:
                # First leg to answer — create the conference with this leg
                conf_name = f"dispatch-{session_id}"
                conf = ctx.client.api.conferences.create(
                    name=conf_name,
                    call_control_id=ctx.call_control_id,
                )
                conf_id = conf.id if hasattr(conf, "id") else conf.data.id
                set_dispatch_conference(session_id, conf_id)
                log.info(
                    "Dispatch %s: created conference %s from %s leg %s",
                    session_id, conf_id, role, ctx.call_control_id,
                )
                self._log_event(ctx, notes=f"dispatch_created_conf:{conf_id}")
                return f"dispatch_created_conf:{conf_id}"
            else:
                # Second leg to answer — join the existing conference
                log.info(
                    "Dispatch %s: joining %s leg %s to conference %s",
                    session_id, role, ctx.call_control_id, conf_id,
                )
                ctx.client.api.calls.actions.join_conference(ctx.call_control_id, conf_id)
                self._log_event(ctx, notes=f"dispatch_joined:{conf_id}")
                # Both legs are now in the conf — clear the session
                clear_dispatch_session(session_id)
                return f"dispatch_joined:{conf_id}"
        except Exception as e:
            log.exception("Dispatch %s: %s leg failed", session_id, role)
            self._log_event(ctx, notes=f"dispatch_error:{e}")
            return f"dispatch_error:{e}"

    def event_call_hangup(self, ctx: WebhookContext) -> str:
        self._log_event(ctx, notes="hangup")
        return "logged"

    def event_call_bridged(self, ctx: WebhookContext) -> str:
        self._log_event(ctx, notes="bridged")
        return "logged"

    def event_call_ai_assistant_started(self, ctx: WebhookContext) -> str:
        self._log_event(ctx, notes="ai_assistant_started")
        return "logged"

    def event_call_ai_assistant_ended(self, ctx: WebhookContext) -> str:
        self._log_event(ctx, notes="ai_assistant_ended")
        return "logged"

    def event_recording_ready(self, ctx: WebhookContext) -> str:
        rec_id = ctx.payload.get("recording_id") or ctx.payload.get("id")
        self._log_event(ctx, notes=f"recording:{rec_id}")
        return f"recording:{rec_id}"

    def event_message_received(self, ctx: WebhookContext) -> str:
        from_ = _extract_phone(ctx.payload.get("from"))
        text = ctx.payload.get("text") or ctx.payload.get("body")
        self._log_event(ctx, notes=f"sms:{text[:100] if text else ''}")
        return "logged"
