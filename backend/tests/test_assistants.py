"""AI assistant tests (issue #14)."""
from __future__ import annotations

import pytest


def test_assistants_crud_local(client, store):
    """Create + list + read + delete an assistant. Telnyx side is best-effort
    (no real key in CI), so we verify the local row + tool shape."""
    payload = {
        "name": "Sales Bot",
        "system_prompt": "You are a sales agent for Acme Corp.",
        "voice": "Telnyx.KokoroTTS.af_heart",
        "model": "openai/gpt-4o",
        "greeting": "Hi! How can I help?",
        "tool_ids": ["transfer_to_number", "hangup"],
    }
    r = client.post("/api/assistants", json=payload,
                    headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    aid = body["assistant"]["id"]
    assert aid.startswith("ast_")
    assert body["assistant"]["name"] == "Sales Bot"
    # The 2 selected tools should round-trip with their 'name' field.
    tool_names = sorted(t.get("name") for t in body["assistant"]["tools"])
    assert tool_names == ["hangup", "transfer_to_number"]

    # List
    r = client.get("/api/assistants", headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200
    assert any(a["id"] == aid for a in r.json()["assistants"])

    # Get
    r = client.get(f"/api/assistants/{aid}", headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200
    assert r.json()["assistant"]["name"] == "Sales Bot"

    # Patch (update voice + tools)
    r = client.patch(
        f"/api/assistants/{aid}",
        json={"voice": "AWS.Polly.Matthew", "tool_ids": ["send_sms"]},
        headers={"X-Tenant-Id": "default"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["assistant"]["voice"] == "AWS.Polly.Matthew"
    assert [t["name"] for t in r.json()["assistant"]["tools"]] == ["send_sms"]

    # Delete
    r = client.delete(f"/api/assistants/{aid}", headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200
    r = client.get(f"/api/assistants/{aid}", headers={"X-Tenant-Id": "default"})
    assert r.status_code == 404


def test_voice_lab_voices_listing(client, store):
    """/api/voice-lab/voices returns the curated list (>=6 voices)."""
    r = client.get("/api/voice-lab/voices", headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200
    voices = r.json()["voices"]
    assert len(voices) >= 6
    # Each voice has id + name + provider
    for v in voices:
        assert "id" in v and v["id"]
        assert "name" in v
        assert "provider" in v


def test_voice_lab_preview_text_required(client, store):
    """/api/voice-lab/preview requires text."""
    r = client.post("/api/voice-lab/preview", json={},
                    headers={"X-Tenant-Id": "default"})
    assert r.status_code == 400


def test_assistant_test_call_seeds_log(client, store):
    """POST /api/assistants/{id}/test-call returns a room + token, and
    seeds a 'system' row in the call log."""
    # Create the assistant first.
    r = client.post("/api/assistants",
                    json={"name": "TestBot", "system_prompt": "You are a test."},
                    headers={"X-Tenant-Id": "default"})
    aid = r.json()["assistant"]["id"]

    r = client.post(f"/api/assistants/{aid}/test-call",
                    headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["room_id"]
    assert body["token"]
    call_id = body["call_id"]

    # The call log should now have a system row.
    r = client.get(f"/api/assistants/{aid}/call-log",
                   headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200
    log = r.json()["log"]
    assert any(e["role"] == "system" and e["call_id"] == call_id for e in log)
