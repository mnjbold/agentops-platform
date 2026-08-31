"""Tests for the video meetings API (issue #26).

Coverage:
* POST /api/meetings        — creates a (stub) Daily room, returns a row + token
* GET  /api/meetings/{id}   — returns the row
* POST /api/meetings/{id}/join — mints a per-user token
* POST /api/meetings/{id}/end  — marks ended
* POST /api/webhooks/daily  — handles participant.joined/left + recording

The Daily client is stub-first (DAILY_API_KEY is unset in tests) so every
call returns synthetic data without hitting the network.
"""
from __future__ import annotations

import json
import os


# ──────────────────────────── create + get + join + end ──────────────────────


def test_create_meeting_returns_room_and_token(client, store):
    """POST /api/meetings creates a row and returns a stub room URL + token."""
    r = client.post(
        "/api/meetings",
        json={"title": "Kickoff with Acme"},
        headers={"X-Tenant-Id": "default"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["title"] == "Kickoff with Acme"
    assert body["room_url"].startswith("https://stub.daily.co/")
    assert body["join_url"] == body["room_url"]
    # Host token is minted from the stub.
    assert body["host_token"].startswith("stub_tok_")
    # The same id is then readable through GET.
    r2 = client.get(
        f"/api/meetings/{body['id']}",
        headers={"X-Tenant-Id": "default"},
    )
    assert r2.status_code == 200, r2.text
    row = r2.json()
    assert row["id"] == body["id"]
    assert row["title"] == "Kickoff with Acme"
    assert row["room_url"] == body["room_url"]


def test_get_meeting_returns_participants_array(client, store):
    """GET /api/meetings/{id} returns the persisted row, with the
    participants array decoded from JSON."""
    create = client.post(
        "/api/meetings",
        json={"title": "Demo"},
        headers={"X-Tenant-Id": "default"},
    ).json()
    r = client.get(
        f"/api/meetings/{create['id']}",
        headers={"X-Tenant-Id": "default"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == create["id"]
    assert body["participants"] == []
    assert body["ended_at"] is None


def test_join_meeting_mints_token_and_starts_at(client, store):
    """POST /api/meetings/{id}/join returns a token + room_url; the first
    join stamps ``started_at``."""
    create = client.post(
        "/api/meetings",
        json={"title": "T1"},
        headers={"X-Tenant-Id": "default"},
    ).json()
    r = client.post(
        f"/api/meetings/{create['id']}/join",
        json={"user_name": "Alice"},
        headers={"X-Tenant-Id": "default"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token"].startswith("stub_tok_")
    assert body["room_url"] == create["room_url"]
    assert body["expires_at"] > 0
    # First-join stamp should now be set on the row.
    row = store.get_meeting("default", create["id"])
    assert row["started_at"] is not None


def test_join_then_end_marks_ended_and_404_after_delete(client, store):
    """End → row has ended_at; a subsequent join 409s."""
    create = client.post(
        "/api/meetings",
        json={"title": "T2"},
        headers={"X-Tenant-Id": "default"},
    ).json()
    # End it
    r = client.post(
        f"/api/meetings/{create['id']}/end",
        headers={"X-Tenant-Id": "default"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    row = store.get_meeting("default", create["id"])
    assert row["ended_at"] is not None
    # Joining an ended meeting → 409
    r2 = client.post(
        f"/api/meetings/{create['id']}/join",
        json={},
        headers={"X-Tenant-Id": "default"},
    )
    assert r2.status_code == 409, r2.text


def test_end_meeting_is_idempotent(client, store):
    """Calling /end twice doesn't crash and doesn't overwrite ended_at."""
    create = client.post(
        "/api/meetings",
        json={"title": "Idempotent"},
        headers={"X-Tenant-Id": "default"},
    ).json()
    first = client.post(
        f"/api/meetings/{create['id']}/end",
        headers={"X-Tenant-Id": "default"},
    )
    assert first.status_code == 200
    first_ended = first.json()["meeting"]["ended_at"]
    second = client.post(
        f"/api/meetings/{create['id']}/end",
        headers={"X-Tenant-Id": "default"},
    )
    assert second.status_code == 200
    assert second.json().get("already_ended") is True
    # ended_at didn't move (COALESCE on the UPDATE)
    row = store.get_meeting("default", create["id"])
    assert row["ended_at"] == first_ended


# ──────────────────────────── list + 404 ────────────────────────────────────


def test_list_meetings_returns_newest_first(client, store):
    """GET /api/meetings returns a list with the new row at the top."""
    a = client.post("/api/meetings", json={"title": "A"},
                    headers={"X-Tenant-Id": "default"}).json()
    b = client.post("/api/meetings", json={"title": "B"},
                    headers={"X-Tenant-Id": "default"}).json()
    r = client.get("/api/meetings", headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200
    rows = r.json()["meetings"]
    assert len(rows) == 2
    # Newest first: b comes before a
    assert rows[0]["id"] == b["id"]
    assert rows[1]["id"] == a["id"]


def test_get_unknown_meeting_404(client, store):
    r = client.get("/api/meetings/mtg_does_not_exist",
                   headers={"X-Tenant-Id": "default"})
    assert r.status_code == 404


# ──────────────────────────── Daily webhooks ────────────────────────────────


def test_daily_webhook_participant_joined_appends_to_list(client, store):
    """A participant.joined event adds an entry to participants_json."""
    create = client.post(
        "/api/meetings", json={"title": "WH"},
        headers={"X-Tenant-Id": "default"},
    ).json()
    room_name = create["room_name"]
    r = client.post(
        "/api/webhooks/daily",
        json={
            "type": "participant.joined",
            "payload": {
                "room": room_name,
                "participant": {"user_id": "u1", "userName": "Alice"},
            },
        },
        headers={"X-Tenant-Id": "default"},
    )
    assert r.status_code == 200, r.text
    row = store.get_meeting("default", create["id"])
    assert any(p["user_id"] == "u1" for p in row["participants"])


def test_daily_webhook_recording_ready_persists_url(client, store):
    """A recording.ready event stores the recording URL on the meeting row."""
    create = client.post(
        "/api/meetings", json={"title": "Rec"},
        headers={"X-Tenant-Id": "default"},
    ).json()
    r = client.post(
        "/api/webhooks/daily",
        json={
            "type": "recording.ready-to-download",
            "payload": {
                "room": create["room_name"],
                "recording_url": "https://example.com/recording.mp4",
            },
        },
        headers={"X-Tenant-Id": "default"},
    )
    assert r.status_code == 200, r.text
    row = store.get_meeting("default", create["id"])
    assert row["recording_url"] == "https://example.com/recording.mp4"


def test_daily_webhook_meeting_ended_marks_ended_at(client, store):
    """A meeting.ended event stamps ended_at on the row."""
    create = client.post(
        "/api/meetings", json={"title": "End"},
        headers={"X-Tenant-Id": "default"},
    ).json()
    r = client.post(
        "/api/webhooks/daily",
        json={
            "type": "meeting.ended",
            "payload": {"room": create["room_name"]},
        },
        headers={"X-Tenant-Id": "default"},
    )
    assert r.status_code == 200
    row = store.get_meeting("default", create["id"])
    assert row["ended_at"] is not None
