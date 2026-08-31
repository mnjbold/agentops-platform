"""Acceptance tests for Phase E-A issue #31 (agent presence).

Coverage
--------
* ``GET /api/agents/presence`` returns the tenant's users with
  presence + skills rows joined in.
* ``PUT /api/agents/me/presence`` validates the status enum and
  upserts the row.
* ``GET /api/agents/queue/next`` returns the longest-idle online user
  and respects manual rewind of ``last_seen`` (the acceptance test
  in the issue brief).
* The periodic sweep flips stale 'online' rows to 'offline'.
"""
from __future__ import annotations

import time

import pytest

from webhooks.tenancy import issue_jwt


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _hdr(tok: str) -> dict:
    return {"X-Tenant-Id": "default", "Authorization": f"Bearer {tok}"}


def _seed_two_users(store, client) -> tuple[str, str, str, str]:
    """Create two agent users and return (uid_a, uid_b, jwt_a, jwt_b)."""
    import bcrypt
    ph = bcrypt.hashpw(b"pw", bcrypt.gensalt(rounds=4)).decode("utf-8")
    store.create_user("u_alice", "default", "alice@default.local", ph, "agent")
    store.create_user("u_bob", "default", "bob@default.local", ph, "agent")
    tok_a = issue_jwt("u_alice", "default", "agent")[0]
    tok_b = issue_jwt("u_bob", "default", "agent")[0]
    return "u_alice", "u_bob", tok_a, tok_b


# ---------------------------------------------------------------------------
# GET /api/agents/presence
# ---------------------------------------------------------------------------


def test_presence_list_returns_users_with_skills(client, store):
    """The roster endpoint joins users + presence + skills in one shot."""
    _seed_two_users(store, client)
    r = client.get("/api/agents/presence",
                    headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    by_uid = {a["user_id"]: a for a in body["agents"]}
    assert set(by_uid.keys()) == {"u_alice", "u_bob"}
    # No presence set yet → offline
    assert by_uid["u_alice"]["status"] == "offline"
    assert by_uid["u_alice"]["last_seen"] is None


# ---------------------------------------------------------------------------
# PUT /api/agents/me/presence
# ---------------------------------------------------------------------------


def test_set_presence_validates_status(client, store):
    _, _, tok_a, _ = _seed_two_users(store, client)
    r = client.put(
        "/api/agents/me/presence",
        json={"status": "vibing"},
        headers=_hdr(tok_a),
    )
    assert r.status_code == 400
    assert "status" in r.json()["detail"]


def test_set_presence_persists_and_bumps_last_seen(client, store):
    _, _, tok_a, _ = _seed_two_users(store, client)
    r = client.put(
        "/api/agents/me/presence",
        json={"status": "online"},
        headers=_hdr(tok_a),
    )
    assert r.status_code == 200, r.text
    assert r.json()["presence"]["status"] == "online"
    assert r.json()["presence"]["last_seen"]


# ---------------------------------------------------------------------------
# GET /api/agents/queue/next (the acceptance test)
# ---------------------------------------------------------------------------


def test_queue_next_returns_longest_idle_then_rewinds(client, store):
    """The exact acceptance scenario from the issue brief:

    1. Two users → both online.
    2. GET /api/agents/queue/next returns the first (oldest last_seen).
    3. Rewind the other user's last_seen so it's now the older one.
    4. GET /api/agents/queue/next returns the OTHER user.
    """
    _, _, tok_a, tok_b = _seed_two_users(store, client)
    # Both come online. Alice first so her last_seen is older.
    r1 = client.put("/api/agents/me/presence",
                    json={"status": "online"}, headers=_hdr(tok_a))
    assert r1.status_code == 200, r1.text
    # Tiny sleep so the timestamps differ
    time.sleep(0.05)
    r2 = client.put("/api/agents/me/presence",
                    json={"status": "online"}, headers=_hdr(tok_b))
    assert r2.status_code == 200, r2.text

    r = client.get("/api/agents/queue/next",
                   headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200, r.text
    assert r.json()["agent"]["user_id"] == "u_alice"

    # Rewind Bob's last_seen to long ago so he's the oldest now.
    store._exec(
        "UPDATE agent_presence SET last_seen = ? WHERE user_id = 'u_bob'",
        ("2000-01-01T00:00:00+00:00",),
    )

    r = client.get("/api/agents/queue/next",
                   headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200, r.text
    assert r.json()["agent"]["user_id"] == "u_bob"


def test_queue_next_204_when_no_agents(client, store):
    """No presence rows → 204 No Content."""
    _seed_two_users(store, client)  # users exist, but no presence rows
    r = client.get("/api/agents/queue/next",
                   headers={"X-Tenant-Id": "default"})
    assert r.status_code == 204


# ---------------------------------------------------------------------------
# Sweeper
# ---------------------------------------------------------------------------


def test_sweep_stale_presence_flips_online_to_offline(client, store):
    _, uid_b, _, _ = _seed_two_users(store, client)
    # Make Bob online, then rewind his last_seen to > 90s ago.
    store.upsert_presence("default", uid_b, "online")
    store._exec(
        "UPDATE agent_presence SET last_seen = ? WHERE user_id = ?",
        ("2000-01-01T00:00:00+00:00", uid_b),
    )
    flipped = store.sweep_stale_presence("default", idle_secs=90)
    assert flipped == 1
    row = store.get_presence("default", uid_b)
    assert row is not None
    assert row["status"] == "offline"
    assert row["current_call_id"] is None


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


def test_set_my_skills_persists_and_lists(client, store):
    _, _, tok_a, _ = _seed_two_users(store, client)
    r = client.put(
        "/api/agents/me/skills",
        json={"skills": ["sales", {"skill": "support", "level": 80}]},
        headers=_hdr(tok_a),
    )
    assert r.status_code == 200, r.text
    skills = r.json()["skills"]
    by_skill = {s["skill"]: s for s in skills}
    assert by_skill["sales"]["level"] == 50
    assert by_skill["support"]["level"] == 80

    # Roster surfaces the skills too
    r = client.get("/api/agents/presence",
                   headers={"X-Tenant-Id": "default"})
    body = r.json()
    alice = next(a for a in body["agents"] if a["user_id"] == "u_alice")
    sset = {s["skill"] for s in alice["skills"]}
    assert sset == {"sales", "support"}


def test_queue_next_skill_filter(client, store):
    """With a skill filter, only users holding that skill are eligible."""
    import bcrypt
    ph = bcrypt.hashpw(b"pw", bcrypt.gensalt(rounds=4)).decode("utf-8")
    store.create_user("u_alice", "default", "alice@default.local", ph, "agent")
    store.create_user("u_bob", "default", "bob@default.local", ph, "agent")
    store.set_user_skills("default", "u_alice", ["sales"])
    store.set_user_skills("default", "u_bob", ["billing"])
    store.upsert_presence("default", "u_alice", "online")
    store.upsert_presence("default", "u_bob", "online")
    r = client.get("/api/agents/queue/next?skill=sales",
                   headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200
    assert r.json()["agent"]["user_id"] == "u_alice"
    r = client.get("/api/agents/queue/next?skill=billing",
                   headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200
    assert r.json()["agent"]["user_id"] == "u_bob"
    r = client.get("/api/agents/queue/next?skill=nosuch",
                   headers={"X-Tenant-Id": "default"})
    assert r.status_code == 204
