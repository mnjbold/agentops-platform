"""Acceptance tests for Phase E-B issues #41, #42, #43 (supervisor monitor / whisper / barge).

Coverage
--------
* ``POST /api/calls/{call_id}/supervisor/monitor`` records a session
  with ``mode='monitor'`` and adds the supervisor to the call's
  participants list. (#41)
* ``POST /api/calls/{call_id}/supervisor/whisper`` records a session
  with ``mode='whisper'``. (#42)
* ``POST /api/calls/{call_id}/supervisor/barge`` records a session
  with ``mode='barge'``. (#43)
* ``GET /api/calls/{call_id}/supervisor`` returns the participants
  with the right role labels.
* ``POST /api/supervisor/sessions/{id}/end`` closes one session.
* ``POST /api/supervisor/sessions/end_for_call/{call_id}`` closes all
  open sessions for a call.
* Non-supervisor roles are rejected (403).
"""
from __future__ import annotations

import bcrypt
import pytest

from webhooks.tenancy import issue_jwt


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _hdr(tok: str) -> dict:
    return {"X-Tenant-Id": "default", "Authorization": f"Bearer {tok}"}


def _seed_supervisor(store) -> dict:
    """One agent + one supervisor + their tokens. Idempotent — safe to
    call multiple times in the same test (the conftest fixture gives
    every test a fresh DB, but each helper invocation must be safe
    within a single test)."""
    ph = bcrypt.hashpw(b"pw", bcrypt.gensalt(rounds=4)).decode("utf-8")
    if not store.get_user("default", "agent@default.local"):
        store.create_user("u_agent", "default", "agent@default.local", ph, "agent")
    if not store.get_user("default", "super@default.local"):
        store.create_user("u_super", "default", "super@default.local", ph, "supervisor")
    if not store.get_user("default", "admin@default.local"):
        store.create_user("u_admin", "default", "admin@default.local", ph, "admin")
    store.upsert_presence("default", "u_agent", "online")
    return {
        "sup_tok": issue_jwt("u_super", "default", "supervisor")[0],
        "adm_tok": issue_jwt("u_admin", "default", "admin")[0],
        "agt_tok": issue_jwt("u_agent", "default", "agent")[0],
    }


# ---------------------------------------------------------------------------
# #41 — Monitor
# ---------------------------------------------------------------------------


def test_monitor_creates_session_with_role(client, store):
    """Monitor records a session and the call's participants list
    includes the supervisor with role='monitor'."""
    _seed_supervisor(store)
    r = client.post(
        "/api/calls/call_m1/supervisor/monitor",
        json={"supervisor_user_id": "u_super"},
        headers={"X-Tenant-Id": "default", "Authorization": f"Bearer {_seed_supervisor(store)['sup_tok']}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "monitor"
    assert body["session"]["mode"] == "monitor"
    assert body["session"]["supervisor_user_id"] == "u_super"
    # participants derived from the active sessions
    assert any(p["user_id"] == "u_super" and p["role"] == "monitor"
               for p in body["participants"])


def test_monitor_requires_supervisor_role(client, store):
    """An 'agent' (no supervisor/admin role) gets 403 on monitor."""
    _seed_supervisor(store)
    r = client.post(
        "/api/calls/call_m2/supervisor/monitor",
        json={"supervisor_user_id": "u_super"},
        headers={"X-Tenant-Id": "default", "Authorization": f"Bearer {_seed_supervisor(store)['agt_tok']}"},
    )
    assert r.status_code == 403
    assert "supervisor" in r.json()["detail"].lower()


def test_monitor_idempotent_on_repeat(client, store):
    """Re-issuing monitor on the same (call, supervisor, mode) does
    NOT create a duplicate active row."""
    toks = _seed_supervisor(store)
    hdr = {"X-Tenant-Id": "default", "Authorization": f"Bearer {toks['sup_tok']}"}
    body = {"supervisor_user_id": "u_super"}
    r1 = client.post("/api/calls/call_idem/supervisor/monitor", json=body, headers=hdr)
    r2 = client.post("/api/calls/call_idem/supervisor/monitor", json=body, headers=hdr)
    assert r1.status_code == 200
    assert r2.status_code == 200
    sessions = store.list_active_supervisor_sessions("default", "call_idem")
    assert len(sessions) == 1


def test_monitor_rejects_unknown_supervisor(client, store):
    toks = _seed_supervisor(store)
    r = client.post(
        "/api/calls/call_m3/supervisor/monitor",
        json={"supervisor_user_id": "u_nope"},
        headers={"X-Tenant-Id": "default", "Authorization": f"Bearer {toks['sup_tok']}"},
    )
    assert r.status_code == 400
    assert "not found" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Lifecycle: end / end_for_call
# ---------------------------------------------------------------------------


def test_end_session_closes_one(client, store):
    toks = _seed_supervisor(store)
    hdr = {"X-Tenant-Id": "default", "Authorization": f"Bearer {toks['sup_tok']}"}
    r = client.post(
        "/api/calls/call_end/supervisor/monitor",
        json={"supervisor_user_id": "u_super"}, headers=hdr,
    )
    sid = r.json()["session"]["id"]
    r2 = client.post(f"/api/supervisor/sessions/{sid}/end", headers=hdr)
    assert r2.status_code == 200, r2.text
    # Now the call has zero active sessions
    r3 = client.get("/api/calls/call_end/supervisor", headers={"X-Tenant-Id": "default"})
    assert r3.json()["count"] == 0


def test_end_for_call_closes_all(client, store):
    toks = _seed_supervisor(store)
    hdr = {"X-Tenant-Id": "default", "Authorization": f"Bearer {toks['sup_tok']}"}
    # Two distinct supervisors on the same call
    store.create_user("u_super2", "default", "super2@default.local",
                      bcrypt.hashpw(b"pw", bcrypt.gensalt(rounds=4)).decode("utf-8"),
                      "supervisor")
    for sup in ("u_super", "u_super2"):
        client.post(
            "/api/calls/call_all/supervisor/monitor",
            json={"supervisor_user_id": sup}, headers=hdr,
        )
    assert len(store.list_active_supervisor_sessions("default", "call_all")) == 2
    r = client.post("/api/supervisor/sessions/end_for_call/call_all", headers=hdr)
    assert r.status_code == 200
    assert r.json()["closed"] == 2


# ---------------------------------------------------------------------------
# #42 — Whisper (storage only, real audio is v1.1)
# ---------------------------------------------------------------------------


def test_whisper_creates_session_with_role(client, store):
    toks = _seed_supervisor(store)
    r = client.post(
        "/api/calls/call_w1/supervisor/whisper",
        json={"supervisor_user_id": "u_super"},
        headers={"X-Tenant-Id": "default", "Authorization": f"Bearer {toks['sup_tok']}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "whisper"
    assert body["session"]["mode"] == "whisper"
    assert any(p["user_id"] == "u_super" and p["role"] == "whisper"
               for p in body["participants"])


# ---------------------------------------------------------------------------
# #43 — Barge
# ---------------------------------------------------------------------------


def test_barge_creates_session_with_role(client, store):
    toks = _seed_supervisor(store)
    r = client.post(
        "/api/calls/call_b1/supervisor/barge",
        json={"supervisor_user_id": "u_super"},
        headers={"X-Tenant-Id": "default", "Authorization": f"Bearer {toks['sup_tok']}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "barge"
    assert body["session"]["mode"] == "barge"
    assert any(p["user_id"] == "u_super" and p["role"] == "barge"
               for p in body["participants"])
