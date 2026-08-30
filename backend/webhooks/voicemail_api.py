"""Voicemail + recording API (Phase A issues #5, #6).

Endpoints
---------
GET   /api/voicemails?unread=true&limit=50   paginated inbox
PATCH /api/voicemails/{id}/read              mark read
GET   /api/voicemails/{id}/audio             stream the MP3 (auth required)
GET   /api/recordings?q=...&from=...         FTS5 search
GET   /api/recordings/{id}/download?format=mp3  download with Content-Disposition

The recording webhook handler lives in
``webhooks/voicemail_handler.py`` and is wired into the
``DefaultEventHandler`` flow.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse

from webhooks.storage import get_store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["voicemail"])


# ──────────────────────────── voicemails ────────────────────────────────────


@router.get("/voicemails")
def list_voicemails(
    request: Request,
    unread: bool = False,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    tid = getattr(request.state, "tenant_id", None) or "default"
    store = get_store()
    vms = store.list_voicemails(tid, unread_only=unread, limit=limit, offset=offset)
    return {"voicemails": vms, "count": len(vms), "limit": limit, "offset": offset}


@router.patch("/voicemails/{voicemail_id}/read")
def mark_voicemail_read(voicemail_id: str, request: Request) -> dict:
    tid = getattr(request.state, "tenant_id", None) or "default"
    store = get_store()
    vm = store.mark_voicemail_read(tid, voicemail_id)
    if not vm:
        raise HTTPException(404, f"voicemail {voicemail_id} not found")
    return {"ok": True, "voicemail": vm}


@router.get("/voicemails/{voicemail_id}/audio")
def stream_voicemail_audio(voicemail_id: str, request: Request) -> Response:
    """Stream the voicemail MP3 by fetching the recording URL and proxying
    the bytes back. The URL may be a Telnyx short-lived signed URL; we
    re-fetch a fresh one if it has expired (the stored URL is the same
    one, but Telnyx URLs are valid for 5 min by default — a second
    request may need a fresh ``recordings.retrieve`` call)."""
    tid = getattr(request.state, "tenant_id", None) or "default"
    store = get_store()
    vm = store.get_voicemail(tid, voicemail_id)
    if not vm:
        raise HTTPException(404, f"voicemail {voicemail_id} not found")
    rec_url = vm.get("recording_url")
    if not rec_url:
        raise HTTPException(404, "no recording url for this voicemail")
    try:
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            r = client.get(rec_url)
            r.raise_for_status()
            audio = r.content
    except httpx.HTTPError as e:
        raise HTTPException(502, f"failed to fetch audio: {e}")
    return Response(
        content=audio,
        media_type="audio/mpeg",
        headers={"Content-Disposition": f'inline; filename="voicemail-{voicemail_id}.mp3"'},
    )


# ──────────────────────────── recordings ───────────────────────────────────


@router.get("/recordings")
def search_recordings(
    request: Request,
    q: Optional[str] = None,
    from_number: Optional[str] = None,
    to_number: Optional[str] = None,
    min_duration: Optional[int] = Query(None, ge=0),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    tid = getattr(request.state, "tenant_id", None) or "default"
    store = get_store()
    rows = store.search_recordings(
        tid,
        q=q,
        from_number=from_number,
        to_number=to_number,
        min_duration=min_duration,
        limit=limit,
        offset=offset,
    )
    return {"recordings": rows, "count": len(rows), "limit": limit, "offset": offset}


@router.get("/recordings/{recording_id}/download")
def download_recording(
    recording_id: str,
    request: Request,
    format: str = Query("mp3", pattern="^(mp3|wav)$"),
) -> Response:
    """Stream a recording with ``Content-Disposition: attachment`` so the
    browser saves it rather than trying to play it inline."""
    tid = getattr(request.state, "tenant_id", None) or "default"
    store = get_store()
    rec = store.get_recording(tid, recording_id)
    if not rec:
        raise HTTPException(404, f"recording {recording_id} not found")
    rec_url = rec.get("recording_url")
    if not rec_url:
        raise HTTPException(404, "no recording url stored")
    try:
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            r = client.get(rec_url)
            r.raise_for_status()
            audio = r.content
    except httpx.HTTPError as e:
        raise HTTPException(502, f"failed to fetch recording: {e}")
    media = "audio/mpeg" if format == "mp3" else "audio/wav"
    return Response(
        content=audio,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="recording-{recording_id}.{format}"'},
    )
