"""Campaign AI outbound mode + voicemail drop (issues #17, #18).

Endpoints
---------
POST /api/campaigns/{id}/voicemail-audio    upload MP3/WAV (multipart)
GET  /api/campaigns/{id}/voicemail-audio    download the stored audio
GET  /api/campaigns/{id}/handoffs           list handoff events

Patches
-------
PATCH /api/campaigns/{id} now accepts the new optional fields:
``outbound_mode`` ('human' | 'ai_then_human' | 'voicemail_drop'),
``assistant_id``, ``voicemail_no_answer_action`` ('hangup' | 'voicemail' | 'retry'),
``ring_timeout_secs``.

The actual *behaviour* lives in
:func:`process_outbound_call` — called from the power-dialer scheduler
path. It consults the campaign's mode and either attaches an AI
assistant or drops the voicemail audio if no-answer / AMD-detected.
"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, Response

from telnyx_mcp.clients.telnyx_client import get_client

from webhooks.storage import get_store
from webhooks._phase_b_ctx import _tenant_id

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["campaigns-phase-b"])

# Per-tenant voicemail drop audio lives on disk under the backend root.
_STORAGE_ROOT = Path(__file__).resolve().parents[2] / "storage" / "voicemail_drops"
_STORAGE_ROOT.mkdir(parents=True, exist_ok=True)

ALLOWED_AUDIO_TYPES = {"audio/mpeg": ".mp3", "audio/wav": ".wav",
                       "audio/x-wav": ".wav", "audio/wave": ".wav"}


# ──────────────────────── storage helpers ────────────────────────


def _audio_path(tenant_id: str, campaign_id: str, ext: str) -> Path:
    safe_tenant = "".join(c for c in tenant_id if c.isalnum() or c in "-_")
    safe_camp = "".join(c for c in campaign_id if c.isalnum() or c in "-_")
    return _STORAGE_ROOT / safe_tenant / f"{safe_camp}{ext}"


# ──────────────────────── upload / download ────────────────────────


@router.post("/campaigns/{campaign_id}/voicemail-audio")
async def upload_voicemail_audio(
    campaign_id: str,
    request: Request,
    file: UploadFile = File(...),
) -> dict:
    """Store a per-campaign voicemail drop audio (MP3 or WAV)."""
    store = get_store()
    tenant_id = _tenant_id(request)
    camp = store.get_campaign(tenant_id, campaign_id)
    if not camp:
        raise HTTPException(404, f"campaign {campaign_id} not found")
    ctype = (file.content_type or "").lower()
    ext = ALLOWED_AUDIO_TYPES.get(ctype)
    if ext is None:
        # Be lenient: some browsers send application/octet-stream.
        if file.filename and file.filename.lower().endswith(".wav"):
            ext = ".wav"
        elif file.filename and file.filename.lower().endswith(".mp3"):
            ext = ".mp3"
        else:
            raise HTTPException(415, "audio must be MP3 or WAV")
    payload = await file.read()
    if not payload:
        raise HTTPException(400, "empty file")
    # Hard cap at 10 MB to keep the on-disk budget sane.
    if len(payload) > 10 * 1024 * 1024:
        raise HTTPException(413, "audio file too large (max 10MB)")
    path = _audio_path(tenant_id, campaign_id, ext)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    # Build a public-ish URL the frontend can <audio src=...>. The
    # download endpoint requires the same tenant context, so we route
    # via the API rather than serving the file directly.
    public_url = f"/api/campaigns/{campaign_id}/voicemail-audio"
    # Stash on the campaign row.
    with store._lock:
        store._conn.execute(
            "UPDATE campaigns SET voicemail_audio_url = ?, updated_at = ? "
            "WHERE tenant_id = ? AND id = ?",
            (public_url, store._utcnow(), tenant_id, campaign_id),
        )
    return {
        "ok": True,
        "campaign_id": campaign_id,
        "voicemail_audio_url": public_url,
        "size": len(payload),
        "content_type": ctype,
    }


@router.get("/campaigns/{campaign_id}/voicemail-audio")
def get_voicemail_audio(campaign_id: str, request: Request) -> Response:
    store = get_store()
    tenant_id = _tenant_id(request)
    camp = store.get_campaign(tenant_id, campaign_id)
    if not camp:
        raise HTTPException(404, f"campaign {campaign_id} not found")
    url = camp.get("voicemail_audio_url")
    if not url:
        raise HTTPException(404, "no voicemail audio uploaded for this campaign")
    # The URL is relative; reconstruct the on-disk path.
    ext = ".mp3"
    for candidate in (".mp3", ".wav"):
        if _audio_path(tenant_id, campaign_id, candidate).exists():
            ext = candidate
            break
    path = _audio_path(tenant_id, campaign_id, ext)
    if not path.exists():
        raise HTTPException(404, "audio file not found on disk")
    media = "audio/mpeg" if ext == ".mp3" else "audio/wav"
    return FileResponse(path, media_type=media, filename=path.name)


# ──────────────────────── handoffs log ────────────────────────


@router.get("/campaigns/{campaign_id}/handoffs")
def list_handoffs(campaign_id: str, request: Request) -> dict:
    store = get_store()
    tenant_id = _tenant_id(request)
    camp = store.get_campaign(tenant_id, campaign_id)
    if not camp:
        raise HTTPException(404, f"campaign {campaign_id} not found")
    return {"handoffs": store.list_campaign_handoffs(tenant_id, campaign_id)}


# ──────────────────────── outbound call processor ────────────────────────


def process_outbound_call(
    tenant_id: str,
    payload: dict,
) -> dict:
    """Consult the campaign mode and act on the call outcome.

    Called from the power-dialer scheduler when a call leg has reached
    a terminal state (no-answer / answered). Returns a small dict the
    caller logs into ``deliveries``. The function is best-effort —
    it never raises; webhook / scheduler paths must not break on
    voicemail-drop failures.
    """
    campaign_id = payload.get("campaign_id")
    contact_id = payload.get("contact_id")
    call_id = payload.get("call_id") or payload.get("telnyx_id")
    to = payload.get("to")
    if not campaign_id:
        return {"skipped": "no campaign_id"}
    store = get_store()
    camp = store.get_campaign(tenant_id, campaign_id)
    if not camp:
        return {"skipped": "campaign not found"}
    mode = camp.get("outbound_mode") or "human"
    # The dispatcher hands us an "outcome" key in the payload if
    # known: 'no-answer' | 'machine' | 'answered' | 'failed'.
    outcome = payload.get("outcome") or "answered"
    if mode == "voicemail_drop" and outcome in ("no-answer", "machine"):
        return _drop_voicemail(tenant_id, camp, to, call_id, contact_id, payload)
    if mode == "ai_then_human" and outcome == "answered":
        return _attach_ai_assistant(tenant_id, camp, call_id, contact_id, payload)
    return {"skipped": f"mode={mode} outcome={outcome} no action"}


def _drop_voicemail(
    tenant_id: str,
    campaign: dict,
    to: Optional[str],
    call_id: Optional[str],
    contact_id: Optional[str],
    payload: dict,
) -> dict:
    """Play the campaign's voicemail audio on the call and hang up."""
    audio_url = campaign.get("voicemail_audio_url")
    if not audio_url:
        return {"skipped": "no voicemail audio uploaded"}
    if not call_id:
        return {"skipped": "no call_id (cannot playback)"}
    api_key = None
    try:
        c = get_client()
        api_key = c.creds.api_key
    except Exception:
        pass
    if not api_key:
        return {"skipped": "no TELNYX_API_KEY; cannot playback"}
    try:
        # Resolve the on-disk file path for the playback API.
        # The audio_url is relative like /api/campaigns/{id}/voicemail-audio.
        # We need a *public* URL Telnyx can GET. The simplest path is to
        # read the file from disk and base64-encode it (Telnyx supports
        # inline audio via the speak API's payload field).
        cid = campaign["id"]
        path = None
        for ext in (".mp3", ".wav"):
            candidate = _audio_path(tenant_id, cid, ext)
            if candidate.exists():
                path = candidate
                break
        if path is None:
            return {"error": "audio file missing on disk"}
        import base64
        audio_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        # Use the speak API with the audio inline as a data URL.
        r = httpx.post(
            f"https://api.telnyx.com/v2/calls/{call_id}/actions/speak",
            json={
                "payload": f"data:audio/mpeg;base64,{audio_b64}",
                "voice": "Telnyx.KokoroTTS.af_heart",
                "language": "en-US",
                "command_id": f"vmdrop_{campaign['id']}",
            },
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=20,
        )
        if r.status_code >= 400:
            return {"error": f"Telnyx {r.status_code}: {r.text[:200]}"}
        # Hangup after the drop (best effort).
        try:
            httpx.post(
                f"https://api.telnyx.com/v2/calls/{call_id}/actions/hangup",
                json={"command_id": f"vmhang_{campaign['id']}"},
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                timeout=10,
            )
        except Exception:
            pass
        # Log a delivery row + bump stats.
        store = get_store()
        store.record_delivery(
            tenant_id, "call", to or "?", "voicemail_dropped",
            contact_id=contact_id,
            telnyx_id=call_id,
            payload_summary=_summary_for(campaign, payload, kind="voicemail_drop"),
        )
        store.campaign_stats_bump(tenant_id, campaign["id"], "voicemail", 1)
        return {"ok": True, "action": "voicemail_dropped"}
    except Exception as e:
        log.warning("voicemail drop failed: %s", e)
        return {"error": str(e)}


def _attach_ai_assistant(
    tenant_id: str,
    campaign: dict,
    call_id: Optional[str],
    contact_id: Optional[str],
    payload: dict,
) -> dict:
    """Attach the campaign's assistant to the live call leg."""
    assistant_id = campaign.get("assistant_id")
    if not call_id or not assistant_id:
        return {"skipped": "missing call_id or assistant_id"}
    try:
        c = get_client()
        c.start_ai_assistant(call_id, assistant_id)
    except Exception as e:
        return {"error": f"start_ai_assistant failed: {e}"}
    # Record the handoff (the human eventually picks up via whisper).
    store = get_store()
    store.record_campaign_handoff(
        tenant_id, campaign["id"], "ai_then_human",
        contact_id=contact_id, call_id=call_id)
    return {"ok": True, "action": "ai_attached", "assistant_id": assistant_id}


def _summary_for(campaign: dict, payload: dict, *, kind: str) -> str:
    parts = [f"campaign={campaign['id']}", f"kind={kind}"]
    if payload.get("to"):
        parts.append(f"to={payload['to']}")
    if payload.get("from"):
        parts.append(f"from={payload['from']}")
    return " ".join(parts)[:200]
