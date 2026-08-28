"""Dispatcher MCP tools — usable by MiniMax Code, Hermes, Claude Code, OpenClaw.

These expose the outbound call dispatcher as native MCP tools. Any MCP
client can call:
    telnyx_dial_and_bridge(to="+60121234567", context="checking in")
    telnyx_dial_outbound(to="+60121234567", from_number="+18444618814")

The dispatcher's own Telnyx AI Assistant (deployed on a number) is
instructed to use the webhook tool `place_and_bridge` for the same flow.
"""
from __future__ import annotations

import logging

from telnyx_mcp.server import mcp
from dispatcher.service import get_dispatcher, normalize_phone

log = logging.getLogger(__name__)


@mcp.tool()
def telnyx_dial_and_bridge(
    to: str,
    context: str | None = None,
    from_number: str = "+18444618814",
) -> dict:
    """Place an outbound call from ``from_number`` to ``to`` and bridge to the principal.

    Flow:
        1. Create a Telnyx conference room
        2. Dial the callee (``to``) from ``from_number``
        3. Dial the principal at +60 112 111 3249
        4. When both legs answer, join them to the conference

    Use this when the user (Nurun) wants to call someone and have the
    AI place the call, then connect him in. Same flow as the Telegram bot.

    Args:
        to: Phone number to call (E.164 or loose format).
        context: Optional reason for the call (logged for analytics).
        from_number: The number shown as caller ID (default: personal twin's toll-free).

    Returns:
        Dict with callee/principal call_control_ids, conference_id, and statuses.
    """
    log.info("MCP dial_and_bridge: to=%s context=%r from=%s", to, context, from_number)
    result = get_dispatcher().dial_and_bridge(to, from_number=from_number)
    if context:
        # Persist context alongside the dispatch — best-effort, no failure
        try:
            from connectors.registry import get_registry
            from connectors.base import CallEvent
            get_registry().write_event(CallEvent(
                event_type="dispatch",
                from_number=from_number,
                to_number=normalize_phone(to) or to,
                notes=f"dial_and_bridge: {context}",
            ))
        except Exception as e:
            log.debug("Context log write failed: %s", e)
    return result.to_dict()


@mcp.tool()
def telnyx_normalize_phone(raw: str) -> dict:
    """Parse a loose phone string and return E.164 (or None if invalid).

    Use this before calling telnyx_dial_and_bridge to make sure the
    number is in the right format.
    """
    return {"input": raw, "e164": normalize_phone(raw)}
