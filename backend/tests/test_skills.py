"""Acceptance tests for Phase E-B issue #40 (skill-based routing).

Coverage
--------
* ``find_idle_online_agent(tenant, skill)`` returns the user with that
  skill (case-insensitive).
* When no qualified agent is online and the skill_routing row points at
  a fallback user, the fallback is returned.
* When no qualified agent AND no fallback (or the fallback is offline),
  ``None`` is returned.
* ``GET /api/queue/stats?skill=sales`` filters the counts to that skill.
* Skill routing CRUD round-trips through ``/api/admin/skills``.
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


def _seed_skilled_users(store) -> tuple[str, str, str]:
    """Create two agents: Alice (sales) + Bob (support) + a fallback
    user (Charlie) who has no skills. Returns (uid_alice, uid_bob, uid_charlie).
    """
    ph = bcrypt.hashpw(b"pw", bcrypt.gensalt(rounds=4)).decode("utf-8")
    store.create_user("u_alice", "default", "alice@default.local", ph, "agent")
    store.create_user("u_bob", "default", "bob@default.local", ph, "agent")
    store.create_user("u_charlie", "default", "charlie@default.local", ph, "agent")
    store.set_user_skills("default", "u_alice", ["Sales"])
    store.set_user_skills("default", "u_bob", ["Support"])
    return "u_alice", "u_bob", "u_charlie"


# ---------------------------------------------------------------------------
# find_idle_online_agent + skill_routing fallback
# ---------------------------------------------------------------------------


def test_find_idle_online_agent_skill_filter(client, store):
    """Two users, one with 'sales' — find_idle_online_agent('sales')
    must return the sales user, not the support one."""
    alice, bob, _ = _seed_skilled_users(store)
    store.upsert_presence("default", alice, "online")
    store.upsert_presence("default", bob, "online")
    row = store.find_idle_online_agent("default", skill="sales")
    assert row is not None
    assert row["user_id"] == alice
    # case-insensitive
    row2 = store.find_idle_online_agent("default", skill="SALES")
    assert row2 is not None
    assert row2["user_id"] == alice


def test_find_idle_online_agent_no_match_returns_fallback(client, store):
    """No qualified agent online, but the skill_routing row points at
    Charlie who IS online → Charlie wins."""
    alice, bob, charlie = _seed_skilled_users(store)
    store.upsert_presence("default", alice, "online")
    store.upsert_presence("default", bob, "online")
    store.upsert_presence("default", charlie, "online")
    # Alice (sales) and Bob (support) online; nobody has 'billing'.
    # Make Charlie the fallback for 'billing'.
    store.create_skill_routing(
        "default", name="billing", description="billing team",
        fallback_user_id=charlie,
    )
    row = store.find_idle_online_agent("default", skill="billing")
    assert row is not None
    assert row["user_id"] == charlie


def test_find_idle_online_agent_no_match_no_fallback_returns_none(client, store):
    """No qualified agent AND no fallback → None."""
    alice, bob, _ = _seed_skilled_users(store)
    store.upsert_presence("default", alice, "online")
    store.upsert_presence("default", bob, "online")
    row = store.find_idle_online_agent("default", skill="niche-skill")
    assert row is None


def test_find_idle_online_agent_fallback_offline_returns_none(client, store):
    """Fallback user configured but not online → None."""
    _, _, charlie = _seed_skilled_users(store)
    # Charlie is the fallback but we DON'T mark him online.
    store.create_skill_routing(
        "default", name="premium", fallback_user_id=charlie,
    )
    row = store.find_idle_online_agent("default", skill="premium")
    assert row is None


# ---------------------------------------------------------------------------
# Skill routing admin API CRUD
# ---------------------------------------------------------------------------


def test_skill_routing_crud_round_trip(client, store):
    _seed_skilled_users(store)
    # Create
    r = client.post(
        "/api/admin/skills",
        json={"name": "Sales", "description": "Sales team", "fallback_user_id": "u_charlie"},
        headers={"X-Tenant-Id": "default"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skill"]["name"] == "Sales"
    assert body["skill"]["fallback_user_id"] == "u_charlie"
    assert body["skill"]["online_agents_with_skill"] == 0  # charlie not online
    sid = body["skill"]["id"]

    # List
    r = client.get("/api/skills", headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200
    items = r.json()["items"]
    assert any(it["id"] == sid for it in items)

    # Update
    r = client.put(
        f"/api/admin/skills/{sid}",
        json={"description": "Updated desc"},
        headers={"X-Tenant-Id": "default"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["skill"]["description"] == "Updated desc"
    assert r.json()["skill"]["name"] == "Sales"  # unchanged

    # Delete
    r = client.delete(f"/api/admin/skills/{sid}", headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200
    # And it's gone
    r = client.put(
        f"/api/admin/skills/{sid}",
        json={"description": "x"},
        headers={"X-Tenant-Id": "default"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Queue stats + list filter
# ---------------------------------------------------------------------------


def test_queue_stats_filters_by_skill(client, store):
    """Three calls queued: one sales, one support, one untagged.
    ``?skill=sales`` should report waiting=1."""
    _post = lambda cid, tag: client.post(
        "/api/queue/enqueue",
        json={"call_id": cid, "skill": tag, "priority": 0},
        headers={"X-Tenant-Id": "default"},
    )
    assert _post("c_sales", "sales").status_code == 200
    assert _post("c_support", "support").status_code == 200
    assert _post("c_untagged", None).status_code == 200

    r = client.get("/api/queue/stats?skill=sales", headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skill"] == "sales"
    assert body["waiting"] == 1

    # No filter → 3
    r = client.get("/api/queue/stats", headers={"X-Tenant-Id": "default"})
    assert r.json()["waiting"] == 3


def test_queue_list_filters_by_skill(client, store):
    for cid, tag in [("c_a", "sales"), ("c_b", "support"), ("c_c", "sales")]:
        client.post(
            "/api/queue/enqueue",
            json={"call_id": cid, "skill": tag, "priority": 0},
            headers={"X-Tenant-Id": "default"},
        )
    r = client.get("/api/queue/list?skill=sales", headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 2
    for it in items:
        assert "sales" in (it.get("skill_tags") or [])


# ---------------------------------------------------------------------------
# enqueue_call accepts ``skill`` and merges into skill_tags
# ---------------------------------------------------------------------------


def test_enqueue_skill_field_merges_into_tags(client, store):
    r = client.post(
        "/api/queue/enqueue",
        json={"call_id": "c_only_skill", "skill": "billing"},
        headers={"X-Tenant-Id": "default"},
    )
    assert r.status_code == 200
    row = store._row("SELECT * FROM call_queue WHERE call_id = 'c_only_skill'")
    import json as _json
    tags = _json.loads(row["skill_tags_json"])
    assert "billing" in tags
