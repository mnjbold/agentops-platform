"""Webhook handlers for the dispatcher and the W3J LLC specialist switch.

`POST /webhooks/telnyx/dispatch`        — invoked by the dispatcher AI's
                                         webhook tool `place_and_bridge`. Returns
                                         the dispatch result inline.

`POST /webhooks/telnyx/connect_specialist` — invoked by the W3J LLC concierge's
                                         webhook tool `connect_to_specialist`.
                                         Switches the active AI assistant on the
                                         live call to the requested specialist.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException, Request

from telnyx_mcp.clients.telnyx_client import get_client
from connectors.registry import get_registry
from connectors.base import CallEvent

load_dotenv()

log = logging.getLogger(__name__)

router = APIRouter()

# Specialist name -> Telnyx AI Assistant ID
# Populated at deploy time by scripts/deploy_specialists.py
SPECIALIST_ASSISTANT_IDS: dict[str, str] = {}


def set_specialist_assistant_ids(mapping: dict[str, str]) -> None:
    SPECIALIST_ASSISTANT_IDS.update(mapping)


@router.post("/webhooks/telnyx/dispatch")
async def dispatch(request: Request) -> dict:
    """Inbound webhook from the dispatcher's `place_and_bridge` tool.

    Telnyx calls this when the dispatcher AI's tool is invoked. The
    tool arguments include `to` and `context`. We dial the number and
    bridge to the principal, then return the call_control_ids.
    """
    body = await request.json()
    log.info("Dispatch webhook: %s", json.dumps(body, default=str)[:500])
    # Telnyx sends the tool call payload; extract the args
    args = body.get("arguments") or body.get("params") or body
    to = args.get("to") or args.get("phone")
    context = args.get("context")
    if not to:
        raise HTTPException(400, "Missing 'to' phone number in tool call")
    # Use the dispatcher service
    from dispatcher.service import get_dispatcher
    result = get_dispatcher().dial_and_bridge(to)
    # Log the dispatch
    try:
        get_registry().write_event(CallEvent(
            event_type="dispatch",
            to_number=to,
            notes=f"dispatch_via_webhook: {context}" if context else "dispatch_via_webhook",
            extra={"callee_cci": result.callee_call_control_id, "principal_cci": result.principal_call_control_id, "conf_id": result.conference_id},
        ))
    except Exception as e:
        log.warning("Failed to log dispatch: %s", e)
    return result.to_dict()


@router.post("/webhooks/telnyx/connect_specialist")
async def connect_specialist(request: Request) -> dict:
    """Inbound webhook from the W3J LLC concierge's `connect_to_specialist` tool.

    Telnyx calls this when the W3J LLC concierge AI invokes the tool. We:
    1. Look up the specialist's assistant_id by name
    2. Stop the current AI assistant on the call
    3. Start the specialist AI assistant on the same call
    """
    body = await request.json()
    log.info("Connect-specialist webhook: %s", json.dumps(body, default=str)[:500])
    args = body.get("arguments") or body.get("params") or body
    specialist_name = (args.get("specialist") or "").strip().lower()
    context = args.get("context")
    # Telnyx sends call_control_id via the call metadata; pull it from the
    # tool payload if present, otherwise look it up by recent call
    call_control_id = args.get("call_control_id") or body.get("call_control_id")

    if not specialist_name:
        raise HTTPException(400, "Missing 'specialist' name")
    if specialist_name not in SPECIALIST_ASSISTANT_IDS:
        raise HTTPException(400, f"Unknown specialist: {specialist_name!r}. Known: {list(SPECIALIST_ASSISTANT_IDS)}")

    target_assistant_id = SPECIALIST_ASSISTANT_IDS[specialist_name]

    if not call_control_id:
        raise HTTPException(400, "Missing call_control_id — cannot switch assistant")

    client = get_client()
    try:
        # 1. Stop the current AI
        client.stop_ai_assistant(call_control_id)
        log.info("Stopped current AI on call %s", call_control_id)
    except Exception as e:
        # Sometimes the AI isn't running yet (race); ignore
        log.warning("stop_ai_assistant failed (continuing): %s", e)
    try:
        # 2. Start the specialist AI
        client.start_ai_assistant(call_control_id, target_assistant_id, client_state=context)
        log.info("Started specialist %s (%s) on call %s", specialist_name, target_assistant_id, call_control_id)
    except Exception as e:
        log.exception("start_ai_assistant failed")
        raise HTTPException(500, f"Failed to start specialist: {e}")
    # Log the handoff
    try:
        get_registry().write_event(CallEvent(
            event_type="specialist_handoff",
            call_control_id=call_control_id,
            notes=f"to:{specialist_name}",
            extra={"from": "w3j-llc-concierge", "to_assistant_id": target_assistant_id, "context": context},
        ))
    except Exception:
        pass
    return {
        "success": True,
        "specialist": specialist_name,
        "assistant_id": target_assistant_id,
        "call_control_id": call_control_id,
        "message": f"Now speaking as the {specialist_name.replace('_', ' ')} specialist.",
    }
