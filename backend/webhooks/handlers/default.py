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


def _telnyx_to_call_status(event_type: str, notes: str = "") -> str:
    """Map a Telnyx call event to a coarse status for the calls table."""
    if event_type == "call.initiated":
        return "initiated"
    if event_type == "call.ringing":
        return "ringing"
    if event_type == "call.answered":
        return "answered"
    if event_type == "call.hangup":
        return "completed"
    if event_type == "call.completed":
        return "completed"
    if event_type == "call.busy":
        return "busy"
    if event_type == "call.no-answer":
        return "no-answer"
    if event_type == "call.failed":
        return "failed"
    # Fallback: use the notes prefix if it looks like one of our internal tags
    for tag in ("incoming", "answered_no_routing", "started_assistant", "error"):
        if tag in (notes or ""):
            return tag
    return event_type


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
        """Persist to all configured connectors AND to Appwrite (the SaaS data layer)."""
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

        # Also persist to Appwrite (the new SaaS data layer).
        # Best-effort: never let an Appwrite failure break the webhook handler.
        try:
            self._write_to_appwrite(ctx, notes=notes)
        except Exception as e:
            log.warning("Failed to write event to Appwrite: %s", e)

    def _write_to_appwrite(self, ctx: WebhookContext, notes: str = "") -> None:
        """Write the webhook event to Appwrite collections.

        Strategy:
        - Always: append to `telnyx_events` (audit log)
        - Call events (call.*): upsert a row in `calls` keyed by call_control_id
        - Message events (message.*): upsert a row in `messages` keyed by message_id
        """
        from appx.repos import telnyx_events as events_repo
        from datetime import datetime, timezone

        from_ = _extract_phone(ctx.payload.get("from")) or ""
        to_ = _extract_phone(ctx.payload.get("to")) or ""
        direction = ctx.payload.get("direction") or ""
        event_type = ctx.event_type or "unknown"
        now = datetime.now(timezone.utc).isoformat()

        # 1) Audit log (always)
        try:
            events_repo.append(
                event_type=event_type,
                call_control_id=ctx.call_control_id or "",
                from_number=from_,
                to_number=to_,
                direction=direction,
                payload=ctx.payload if isinstance(ctx.payload, dict) else {"raw": str(ctx.payload)},
                received_at=now,
            )
        except Exception as e:
            log.debug("events_repo.append failed: %s", e)

        # 2) Call events
        if event_type.startswith("call."):
            try:
                from appx.repos import calls as calls_repo
                # Map Telnyx event_type → our status
                status = _telnyx_to_call_status(event_type, notes)
                started_at = ctx.payload.get("start_time") or now
                answered_at = ctx.payload.get("answer_time") or (now if event_type == "call.answered" else None)
                ended_at = ctx.payload.get("end_time") or (now if event_type in ("call.hangup", "call.completed") else None)
                duration = 0
                if isinstance(started_at, str) and isinstance(ended_at, str):
                    try:
                        from datetime import datetime
                        s = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                        e = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
                        duration = max(0, int((e - s).total_seconds()))
                    except Exception:
                        pass
                if ctx.call_control_id:
                    calls_repo.upsert_call(
                        tenant_id="default",
                        call_control_id=ctx.call_control_id,
                        direction=direction or "unknown",
                        from_number=from_,
                        to_number=to_,
                        from_name="",
                        to_name="",
                        status=status,
                        started_at=started_at,
                        answered_at=answered_at,
                        ended_at=ended_at,
                        duration_seconds=duration,
                        has_recording=bool(ctx.payload.get("recording_id")),
                        recording_url=ctx.payload.get("recording_urls", [None])[0] if ctx.payload.get("recording_urls") else "",
                        assistant_id=ctx.agent_id or "",
                    )
            except Exception as e:
                log.debug("calls_repo.upsert_call failed: %s", e)

        # 3) Message events
        if event_type.startswith("message."):
            try:
                from appx.repos import messages as messages_repo
                msg_id = ctx.payload.get("id") or ctx.payload.get("message_id") or ""
                if msg_id:
                    body = ctx.payload.get("text") or ctx.payload.get("body") or ""
                    media = ctx.payload.get("media", []) or []
                    media_urls = [m.get("url", "") for m in media if isinstance(m, dict)]
                    direction = direction or ("inbound" if event_type == "message.received" else "outbound")
                    received = now if event_type == "message.received" else None
                    sent = now if event_type in ("message.sent", "message.delivered") else None
                    messages_repo.upsert_message(
                        tenant_id="default",
                        message_id=msg_id,
                        direction=direction,
                        from_number=from_,
                        to_number=to_,
                        body=body,
                        media_urls=media_urls,
                        status=event_type.split(".")[-1],
                        sent_at=sent,
                        received_at=received,
                    )
            except Exception as e:
                log.debug("messages_repo.upsert_message failed: %s", e)

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
        # Phase A: also persist + transcribe via the dedicated handler.
        try:
            from webhooks.voicemail_handler import handle_recording_saved
            handle_recording_saved(ctx.event_type or "recording.ready", ctx.payload)
        except Exception as e:
            log.warning("voicemail_handler.handle_recording_saved failed (non-fatal): %s", e)
        return f"recording:{rec_id}"

    def event_recording_saved(self, ctx: WebhookContext) -> str:
        # Telnyx renamed the event from recording.ready to recording.saved
        # at some point — both call the same pipeline.
        return self.event_recording_ready(ctx)

    def event_message_received(self, ctx: WebhookContext) -> str:
        from_ = _extract_phone(ctx.payload.get("from"))
        text = ctx.payload.get("text") or ctx.payload.get("body")
        self._log_event(ctx, notes=f"sms:{text[:100] if text else ''}")
        return "logged"
