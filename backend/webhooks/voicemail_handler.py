"""Voicemail + recording webhook handler (Phase A issues #5, #6).

When Telnyx fires ``recording.saved`` we:
  1. fetch the recording URL (Telnyx returns a short-lived signed URL),
  2. upsert a row in ``recordings`` keyed by Telnyx recording id,
  3. if the call was a voicemail (AMD returned ``machine`` and the
     caller hung up without an answered assistant), mirror the row
     into ``voicemails``,
  4. kick off an async transcription via Telnyx's
     ``/v2/ai/audio/transcriptions`` endpoint and store the result.

The transcription is best-effort — if the API call fails we log and
move on; the recording is still searchable via FTS once the operator
manually transcribes it (or a later webhook re-fires).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

from telnyx_mcp.clients.telnyx_client import get_client
from webhooks.storage import get_store

log = logging.getLogger(__name__)


def _extract_phone(field) -> Optional[str]:
    """Lenient phone extraction (matches the rest of the handlers)."""
    if field is None:
        return None
    if isinstance(field, str):
        return field
    if isinstance(field, dict):
        return field.get("phone_number") or field.get("e164") or field.get("number")
    if isinstance(field, list):
        for item in field:
            n = _extract_phone(item)
            if n:
                return n
        return None
    return str(field)


def _telnyx_api_base() -> str:
    return (os.environ.get("TELNYX_API_BASE") or "https://api.telnyx.com").rstrip("/")


def _auth_headers() -> dict:
    c = get_client()
    return {"Authorization": f"Bearer {c.creds.api_key}"}


def _recording_url(payload: dict) -> Optional[str]:
    """Pick the best MP3 URL out of the various shapes Telnyx uses.

    The recording.saved event provides ``recording_urls`` (or
    ``download_urls``) keyed by format. Prefer ``mp3``.
    """
    urls = payload.get("recording_urls") or payload.get("download_urls") or {}
    if isinstance(urls, dict):
        return urls.get("mp3") or urls.get("wav")
    if isinstance(urls, list) and urls:
        first = urls[0]
        if isinstance(first, dict):
            return first.get("mp3") or first.get("wav")
        if isinstance(first, str):
            return first
    return payload.get("recording_url") or None


def _transcribe(recording_url: str) -> Optional[str]:
    """Call Telnyx's transcription endpoint. Returns the transcript text
    or ``None`` on any failure (caller logs and continues)."""
    if not recording_url:
        return None
    base = _telnyx_api_base()
    url = f"{base}/v2/ai/audio/transcriptions"
    try:
        # Telnyx accepts either a remote URL or a base64 blob. Passing
        # the remote URL is simplest and lets Telnyx stream the file
        # without us downloading the whole thing.
        r = httpx.post(
            url,
            json={
                "model": "openai/whisper-large-v3-turbo",
                "file_url": recording_url,
            },
            headers={**_auth_headers(), "Content-Type": "application/json"},
            timeout=60,
        )
        if r.status_code >= 400:
            log.warning("Telnyx transcription HTTP %d: %s", r.status_code, r.text[:200])
            return None
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        return (j.get("text") or (j.get("data") or {}).get("text") or "").strip() or None
    except Exception as e:
        log.warning("Transcription call failed: %s", e)
        return None


def _is_voicemail(payload: dict) -> bool:
    """Heuristic: AMD says machine, no assistant was started, and the
    call has ended. This is best-effort — Telnyx's voicemail detection
    is imperfect and the field names vary by event version."""
    amd = payload.get("machine_detection_result")
    if amd and str(amd).lower() in ("machine", "voicemail", "fax"):
        return True
    # Older events put it under call_control_app metadata.
    extra = payload.get("call_control_app_metadata") or {}
    if extra.get("voicemail") is True:
        return True
    # Some events expose a `recording_type` field.
    rt = payload.get("recording_type") or ""
    if str(rt).lower() in ("voicemail", "vm"):
        return True
    return False


def handle_recording_saved(event_type: str, payload: dict) -> dict:
    """Called from the default event handler on ``recording.saved`` and
    ``recording.ready``.

    Returns a small status dict for the handler to log. Never raises —
    a failure here must not bubble up and 500 the webhook.
    """
    try:
        tenant_id = "default"  # webhooks are single-tenant for v1
        rec_id = payload.get("recording_id") or payload.get("id")
        call_id = payload.get("call_control_id") or payload.get("call_session_id")
        from_ = _extract_phone(payload.get("from"))
        to_ = _extract_phone(payload.get("to"))
        rec_url = _recording_url(payload)
        duration = payload.get("duration_secs")
        if duration is None:
            duration = payload.get("duration")
        try:
            duration = int(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration = None

        if not rec_id:
            log.warning("recording.saved missing recording_id; skipping")
            return {"status": "skipped", "reason": "no recording_id"}

        store = get_store()
        # Upsert recording row (idempotent on recording_id).
        rec_row = store.upsert_recording(
            tenant_id=tenant_id,
            call_id=call_id,
            recording_id=rec_id,
            from_number=from_,
            to_number=to_,
            recording_url=rec_url,
            duration=duration,
        )

        # If this looks like a voicemail, mirror into voicemails.
        is_vm = _is_voicemail(payload)
        if is_vm:
            store.upsert_voicemail(
                tenant_id=tenant_id,
                call_id=call_id,
                from_number=from_,
                to_number=to_,
                recording_url=rec_url,
                duration=duration,
            )
            log.info("Voicemail recorded: call=%s rec=%s", call_id, rec_id)

        # Best-effort transcription.
        transcript = None
        if rec_url:
            transcript = _transcribe(rec_url)
            if transcript:
                store.upsert_recording(
                    tenant_id=tenant_id,
                    call_id=call_id,
                    recording_id=rec_id,
                    from_number=from_,
                    to_number=to_,
                    recording_url=rec_url,
                    transcript=transcript,
                    duration=duration,
                )
                if is_vm:
                    store.upsert_voicemail(
                        tenant_id=tenant_id,
                        call_id=call_id,
                        from_number=from_,
                        to_number=to_,
                        recording_url=rec_url,
                        transcript=transcript,
                        duration=duration,
                    )
                log.info("Transcript for %s: %d chars", rec_id, len(transcript))
            else:
                log.debug("No transcript for %s (best-effort)", rec_id)

        return {
            "status": "ok",
            "recording_id": rec_id,
            "is_voicemail": is_vm,
            "transcript_chars": len(transcript) if transcript else 0,
            "row_id": rec_row.get("id") if isinstance(rec_row, dict) else None,
        }
    except Exception as e:
        log.exception("handle_recording_saved failed: %s", e)
        return {"status": "error", "error": str(e)}
