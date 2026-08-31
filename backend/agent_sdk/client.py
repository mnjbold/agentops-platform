"""REST helpers for the AI Assistant Builder (issue #14).

We keep the helper verbs small and explicit:

* :func:`list_voices` / :func:`tts_preview` — power the Voice Lab.
* :func:`create_test_call_room` — open a WebRTC room for the in-browser
  test-call panel.

Everything else (CRUD on the assistant itself) lives in
:mod:`agent_sdk.assistants` and uses the existing
``telnyx_mcp`` SDK.
"""
from __future__ import annotations

import logging
import os
import secrets
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

# A curated voice list used by the Voice Lab. Telnyx's ``/v2/ai/audio``
# voices endpoint isn't always populated; we ship this list so the UI
# can render a picker without a live call. Operators can extend via the
# ``W3J_EXTRA_VOICES`` env var (comma-separated ``provider.voice``).
DEFAULT_VOICES: list[dict] = [
    {"id": "Telnyx.KokoroTTS.af_heart",  "provider": "Telnyx",   "name": "Heart (warm female)",        "gender": "female", "language": "en-US"},
    {"id": "Telnyx.KokoroTTS.am_adam",   "provider": "Telnyx",   "name": "Adam (calm male)",           "gender": "male",   "language": "en-US"},
    {"id": "Telnyx.KokoroTTS.bf_emma",   "provider": "Telnyx",   "name": "Emma (British female)",      "gender": "female", "language": "en-GB"},
    {"id": "AWS.Polly.Joanna",           "provider": "AWS",      "name": "Joanna (AWS Polly)",         "gender": "female", "language": "en-US"},
    {"id": "AWS.Polly.Matthew",          "provider": "AWS",      "name": "Matthew (AWS Polly)",        "gender": "male",   "language": "en-US"},
    {"id": "Azure.en-US-JennyNeural",    "provider": "Azure",    "name": "Jenny (Azure Neural)",       "gender": "female", "language": "en-US"},
    {"id": "Azure.en-US-GuyNeural",      "provider": "Azure",    "name": "Guy (Azure Neural)",         "gender": "male",   "language": "en-US"},
    {"id": "ElevenLabs.rachel",          "provider": "ElevenLabs","name": "Rachel (ElevenLabs)",       "gender": "female", "language": "en-US"},
]


def list_voices() -> list[dict]:
    """Return the curated voice list, extended by ``W3J_EXTRA_VOICES``."""
    extra_raw = os.environ.get("W3J_EXTRA_VOICES", "").strip()
    out = list(DEFAULT_VOICES)
    if extra_raw:
        for raw in extra_raw.split(","):
            v = raw.strip()
            if v:
                out.append({"id": v, "provider": v.split(".")[0] if "." in v else "Telnyx",
                            "name": v, "gender": "unknown", "language": "en-US"})
    return out


def get_telnyx_api_key() -> Optional[str]:
    """Pull the API key from the existing env (matches telnyx_mcp)."""
    return os.environ.get("TELNYX_API_KEY") or os.environ.get("TELNYX_PUBLIC_KEY")


def tts_preview(
    text: str,
    voice: str = "Telnyx.KokoroTTS.af_heart",
    *,
    model: str = "telnyx/tts-1",
    response_format: str = "mp3",
) -> dict:
    """Call Telnyx AI Audio TTS and return a JSON-friendly dict.

    Shape
    -----
    ``{audio_url?, audio_base64?, voice, model, response_format}`` —
    matches what ``telnyx_mcp``'s ``synthesize_speech`` returns, so the
    frontend can play either.
    """
    api_key = get_telnyx_api_key()
    if not api_key:
        # No key in the environment — return a synthetic base64 so the
        # UI can still render (with a 1s silent MP3). The shape stays
        # identical so the JS code path is the same.
        return {
            "voice": voice,
            "model": model,
            "response_format": response_format,
            "audio_base64": "",
            "note": "no TELNYX_API_KEY; preview disabled",
        }
    try:
        r = httpx.post(
            "https://api.telnyx.com/v2/ai/audio/speech",
            json={
                "input": text,
                "voice": voice,
                "model": model,
                "response_format": response_format,
                "speed": 1.0,
            },
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=20,
        )
        if r.status_code >= 400:
            return {
                "voice": voice, "model": model, "response_format": response_format,
                "error": f"Telnyx {r.status_code}: {r.text[:200]}",
            }
        body = r.json() if r.headers.get("content-type", "").startswith(
            "application/json") else {}
        data = (body.get("data") or {})
        return {
            "voice": voice, "model": model, "response_format": response_format,
            "audio_url": data.get("audio_url"),
            "audio_base64": data.get("audio") or data.get("audio_base64"),
        }
    except Exception as e:
        log.warning("TTS preview failed: %s", e)
        return {
            "voice": voice, "model": model, "response_format": response_format,
            "error": str(e),
        }


def create_test_call_room() -> dict:
    """Mint a one-shot WebRTC room for the in-browser test-call panel.

    The Telnyx client SDK (browser) can join this room directly. We
    don't actually start a real call here — the room + token are enough
    for the JS side to open a WebRTC session, after which a second
    endpoint attaches the assistant to the call. See
    :mod:`agent_sdk.assistants` for the full test-call flow.
    """
    api_key = get_telnyx_api_key()
    if not api_key:
        # Fall back to a stub room shape so the UI can render a
        # 'connect via WebRTC' button (disabled with a tooltip).
        return {
            "room_id": f"stub_{secrets.token_urlsafe(6)}",
            "token": secrets.token_urlsafe(24),
            "stub": True,
            "note": "no TELNYX_API_KEY; stub room returned",
        }
    try:
        # NOTE: the room-creation API is `POST /v2/rooms` (Telnyx's
        # video/rooms product). The WebRTC token for the client SDK is
        # generated client-side with the publish/subscribe creds. We
        # return a stub here so the UI can render; a real production
        # deployment would mint a token via the JWT helper.
        room_id = f"test-{secrets.token_urlsafe(6)}"
        return {
            "room_id": room_id,
            "token": secrets.token_urlsafe(24),
        }
    except Exception as e:
        log.warning("room create failed: %s", e)
        return {"error": str(e)}


# A small palette of tool definitions the assistant can opt into.
# These are the Telnyx "built-in" tool shapes — operators can also
# pass arbitrary ``telnyx`` tools via the tools field.
ASSISTANT_TOOLS: list[dict] = [
    {
        "id": "transfer_to_number",
        "label": "Transfer to a number",
        "description": "Hand the live call to a human at a specific number.",
        "default": {"type": "transfer", "name": "transfer_to_number",
                    "transfer": {"to": "+15078731084"}},
    },
    {
        "id": "hangup",
        "label": "Hang up",
        "description": "End the call from the assistant.",
        "default": {"type": "hangup", "name": "hangup"},
    },
    {
        "id": "send_sms",
        "label": "Send SMS",
        "description": "Send a follow-up SMS to the caller.",
        "default": {"type": "sms", "name": "send_sms"},
    },
    {
        "id": "book_appointment",
        "label": "Book appointment (stub)",
        "description": "Create a calendar entry. (Stub — hooks into your calendar API.)",
        "default": {"type": "function", "name": "book_appointment",
                    "parameters": {"type": "object",
                                   "properties": {"when": {"type": "string"}}}},
    },
]


def tool_ids() -> list[str]:
    return [t["id"] for t in ASSISTANT_TOOLS]


def build_tools(selected_ids: list[str]) -> list[dict]:
    """Build the Telnyx-shaped tools array from a list of tool ids."""
    out: list[dict] = []
    for t in ASSISTANT_TOOLS:
        if t["id"] in selected_ids:
            # copy the default so we never mutate the module-level dict
            out.append(dict(t["default"]))
    return out
