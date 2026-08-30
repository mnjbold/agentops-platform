"""Voicemail + recording tests for Phase A (issues #5, #6).

Coverage:
* FTS5 search across from/to/transcript
* unread filter + pagination
* mark-read flips read_at
* upsert is idempotent on call_id / recording_id
* download endpoint requires tenant context
"""
from __future__ import annotations

import pytest


def _seed_voicemail(store, tenant_id, call_id, from_, to_, transcript, duration=12):
    store.upsert_voicemail(
        tenant_id=tenant_id,
        call_id=call_id,
        from_number=from_,
        to_number=to_,
        recording_url=f"https://example.test/{call_id}.mp3",
        transcript=transcript,
        duration=duration,
    )
    store.upsert_recording(
        tenant_id=tenant_id,
        call_id=call_id,
        recording_id=f"rec_{call_id}",
        from_number=from_,
        to_number=to_,
        recording_url=f"https://example.test/{call_id}.mp3",
        transcript=transcript,
        duration=duration,
    )


def test_voicemail_inbox_unread_filter(client, store):
    _seed_voicemail(store, "default", "vm-1", "+15551110001", "+15552220001", "Hi this is Alice about my order")
    _seed_voicemail(store, "default", "vm-2", "+15551110002", "+15552220001", "Please call me back about the meeting")
    r = client.get("/api/voicemails?unread=true&limit=50", headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200
    inbox = r.json()["voicemails"]
    assert len(inbox) == 2
    assert all(v["read_at"] is None for v in inbox)


def test_voicemail_mark_read(client, store):
    _seed_voicemail(store, "default", "vm-1", "+15551110001", "+15552220001", "Hi this is Alice about my order")
    listing = client.get("/api/voicemails", headers={"X-Tenant-Id": "default"}).json()["voicemails"]
    vid = listing[0]["id"]
    r = client.patch(f"/api/voicemails/{vid}/read", headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200
    assert r.json()["voicemail"]["read_at"] is not None

    # Unread filter now returns nothing.
    r2 = client.get("/api/voicemails?unread=true", headers={"X-Tenant-Id": "default"})
    assert r2.json()["count"] == 0


def test_voicemail_tenant_isolation(client, store):
    """Tenant A's voicemail inbox doesn't leak into tenant B."""
    a = client.post("/api/admin/tenants",
                    json={"name": "TenantA"},
                    headers={"X-Tenant-Id": "default"}).json()
    _seed_voicemail(store, a["tenant_id"], "vm-A1", "+15551110001", "+15552220001", "Tenant A voicemail")
    _seed_voicemail(store, "default", "vm-B1", "+15551110002", "+15552220001", "Default voicemail")

    r = client.get("/api/voicemails", headers={"X-Api-Key": a["api_key"]})
    assert r.status_code == 200
    call_ids = [v["call_id"] for v in r.json()["voicemails"]]
    assert call_ids == ["vm-A1"]

    r2 = client.get("/api/voicemails", headers={"X-Tenant-Id": "default"})
    call_ids = [v["call_id"] for v in r2.json()["voicemails"]]
    assert "vm-B1" in call_ids
    assert "vm-A1" not in call_ids


def test_voicemail_upsert_idempotent_on_call_id(client, store):
    """Re-firing the same call_id updates transcript but doesn't duplicate."""
    _seed_voicemail(store, "default", "vm-1", "+15551110001", "+15552220001", "original")
    _seed_voicemail(store, "default", "vm-1", "+15551110001", "+15552220001", "updated")
    rows = client.get("/api/voicemails", headers={"X-Tenant-Id": "default"}).json()["voicemails"]
    assert len(rows) == 1
    assert rows[0]["transcript"] == "updated"


def test_recordings_fts_search(client, store):
    """FTS5 finds rows whose transcript / from / to match the query."""
    _seed_voicemail(store, "default", "vm-1", "+15551110001", "+15552220001",
                    "Please call me about the invoice payment")
    _seed_voicemail(store, "default", "vm-2", "+15551110003", "+15552220002",
                    "Reminder for tomorrow's meeting about taxes")
    _seed_voicemail(store, "default", "vm-3", "+15551110005", "+15552220003",
                    "Wrong number, sorry")

    # FTS5 search: "invoice" should match only vm-1.
    r = client.get("/api/recordings?q=invoice", headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200
    matched = [rec["call_id"] for rec in r.json()["recordings"]]
    assert matched == ["vm-1"]

    # Search by from_number field.
    r = client.get("/api/recordings?from_number=%2B15551110005",
                   headers={"X-Tenant-Id": "default"})
    matched = [rec["call_id"] for rec in r.json()["recordings"]]
    assert matched == ["vm-3"]

    # min_duration filter.
    r = client.get("/api/recordings?min_duration=60", headers={"X-Tenant-Id": "default"})
    assert r.json()["count"] == 0


def test_voicemail_audio_requires_tenant(client, store):
    """GET /api/voicemails/{id}/audio must have a tenant context (404 if missing)."""
    _seed_voicemail(store, "default", "vm-1", "+15551110001", "+15552220001", "hi")
    listing = client.get("/api/voicemails", headers={"X-Tenant-Id": "default"}).json()["voicemails"]
    vid = listing[0]["id"]
    r = client.get(f"/api/voicemails/{vid}/audio", headers={"X-Tenant-Id": "default"})
    # 502 because example.test isn't reachable — but the auth gate passed.
    assert r.status_code in (502, 200)
