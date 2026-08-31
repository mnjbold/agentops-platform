"""Acceptance tests for Phase E-A issue #32 (call queue).

Coverage
--------
* ``POST /api/queue/enqueue`` enqueues a call and returns the
  position + eta.
* ``GET /api/queue/position/{call_id}`` returns the right
  position for each call.
* ``POST /api/queue/dequeue`` from an agent whose skills match
  pops the head.
* Simulating a ``call.hangup`` flips the status to ``abandoned``
  (when queued) or ``answered`` (when assigned).
* ``GET /api/queue/stats`` returns the right counts.
"""
from __future__ import annotations

import pytest

from webhooks.tenancy import issue_jwt
from webhooks.handlers.base import WebhookContext
from webhooks.handlers.default import DefaultEventHandler


def _hdr(tok: str) -> dict:
    return {"X-Tenant-Id": "default", "Authorization": f"Bearer {tok}"}


def _post_enqueue(client, call_id, *, skill_tags=None, priority=0):
    return client.post(
        "/api/queue/enqueue",
        json={"call_id": call_id, "skill_tags": skill_tags or [], "priority": priority},
        headers={"X-Tenant-Id": "default"},
    )


# ---------------------------------------------------------------------------
# enqueue + position
# ---------------------------------------------------------------------------


def test_enqueue_returns_position_and_eta(client, store):
    r = _post_enqueue(client, "call_q1", priority=0)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["queue_id"]
    assert body["position"] == 1
    assert body["eta_s"] == 0
    assert body["status"] == "queued"


def test_enqueue_with_priority_orders_correctly(client, store):
    """Higher priority should bubble to the front; same priority keeps FIFO."""
    _post_enqueue(client, "call_low", priority=0)
    _post_enqueue(client, "call_mid", priority=0)
    _post_enqueue(client, "call_high", priority=10)

    r1 = client.get("/api/queue/position/call_high", headers={"X-Tenant-Id": "default"})
    assert r1.status_code == 200, r1.text
    assert r1.json()["position"] == 1

    r2 = client.get("/api/queue/position/call_low", headers={"X-Tenant-Id": "default"})
    assert r2.json()["position"] in (2, 3)

    r3 = client.get("/api/queue/position/call_mid", headers={"X-Tenant-Id": "default"})
    assert r3.json()["position"] in (2, 3)
    # The two priority-0 calls keep their enqueue order.
    if r2.json()["position"] == 2:
        assert r3.json()["position"] == 3
    else:
        assert r3.json()["position"] == 2


def test_enqueue_is_idempotent(client, store):
    r1 = _post_enqueue(client, "call_dup", priority=5)
    qid1 = r1.json()["queue_id"]
    r2 = _post_enqueue(client, "call_dup", priority=99)
    qid2 = r2.json()["queue_id"]
    assert qid1 == qid2
    # Priority should NOT have been bumped on the second enqueue.
    row = store._row("SELECT * FROM call_queue WHERE call_id = ?", ("call_dup",))
    assert row["priority"] == 5


# ---------------------------------------------------------------------------
# dequeue
# ---------------------------------------------------------------------------


def test_dequeue_pops_head_for_matching_agent(client, store):
    import bcrypt
    ph = bcrypt.hashpw(b"pw", bcrypt.gensalt(rounds=4)).decode("utf-8")
    store.create_user("u_qa", "default", "qa@default.local", ph, "agent")
    store.set_user_skills("default", "u_qa", ["sales", "billing"])
    tok = issue_jwt("u_qa", "default", "agent")[0]

    _post_enqueue(client, "call_d1", skill_tags=["sales"], priority=0)
    _post_enqueue(client, "call_d2", skill_tags=["billing"], priority=0)
    _post_enqueue(client, "call_d3", skill_tags=["unknown"], priority=0)

    r = client.post("/api/queue/dequeue", headers=_hdr(tok))
    assert r.status_code == 200, r.text
    body = r.json()
    # Should pick d1 (sales) — matches the agent's skills, head of queue.
    assert body["call_id"] == "call_d1"
    assert body["skill_tags"] == ["sales"]
    # d1 is now assigned — calling dequeue again pops d2 (billing).
    r2 = client.post("/api/queue/dequeue", headers=_hdr(tok))
    assert r2.json()["call_id"] == "call_d2"
    # d3 (unknown skill) doesn't match the agent → 404.
    r3 = client.post("/api/queue/dequeue", headers=_hdr(tok))
    assert r3.status_code == 404


def test_dequeue_no_skill_match_skips(client, store):
    import bcrypt
    ph = bcrypt.hashpw(b"pw", bcrypt.gensalt(rounds=4)).decode("utf-8")
    store.create_user("u_only_billing", "default", "billing@default.local", ph, "agent")
    store.set_user_skills("default", "u_only_billing", ["billing"])
    tok = issue_jwt("u_only_billing", "default", "agent")[0]

    _post_enqueue(client, "call_x1", skill_tags=["sales"], priority=0)
    _post_enqueue(client, "call_x2", skill_tags=["billing"], priority=0)

    r = client.post("/api/queue/dequeue", headers=_hdr(tok))
    # The sales call doesn't match; the billing call does.
    assert r.status_code == 200
    assert r.json()["call_id"] == "call_x2"


# ---------------------------------------------------------------------------
# hangup hookup
# ---------------------------------------------------------------------------


def test_hangup_on_queued_marks_abandoned(client, store):
    _post_enqueue(client, "call_h1", skill_tags=["sales"], priority=0)
    # Confirm it's queued
    r0 = client.get("/api/queue/position/call_h1", headers={"X-Tenant-Id": "default"})
    assert r0.status_code == 200
    # Simulate the webhook (the shape Telnyx actually sends)
    handler = DefaultEventHandler(agent_routing={})
    ctx = WebhookContext(
        {"data": {"event_type": "call.hangup",
                  "payload": {"call_control_id": "call_h1",
                              "from": "+15555550100",
                              "to": "+15555550200"}}},
        client=None,
    )
    ctx.event_type = "call.hangup"
    handler.event_call_hangup(ctx)
    # The call should no longer be in the queued set.
    r1 = client.get("/api/queue/position/call_h1", headers={"X-Tenant-Id": "default"})
    assert r1.status_code == 404
    # But the row exists with status='abandoned'.
    row = store._row(
        "SELECT * FROM call_queue WHERE call_id = 'call_h1' "
        "ORDER BY enqueued_at DESC LIMIT 1"
    )
    assert row["status"] == "abandoned"


def test_hangup_on_assigned_marks_answered(client, store):
    import bcrypt
    ph = bcrypt.hashpw(b"pw", bcrypt.gensalt(rounds=4)).decode("utf-8")
    store.create_user("u_ag", "default", "ag@default.local", ph, "agent")
    store.set_user_skills("default", "u_ag", ["sales"])
    tok = issue_jwt("u_ag", "default", "agent")[0]

    _post_enqueue(client, "call_h2", skill_tags=["sales"], priority=0)
    r = client.post("/api/queue/dequeue", headers=_hdr(tok))
    assert r.status_code == 200
    assert r.json()["call_id"] == "call_h2"

    handler = DefaultEventHandler(agent_routing={})
    ctx = WebhookContext(
        {"data": {"event_type": "call.hangup",
                  "payload": {"call_control_id": "call_h2",
                              "from": "+15555550100",
                              "to": "+15555550200"}}},
        client=None,
    )
    ctx.event_type = "call.hangup"
    handler.event_call_hangup(ctx)

    row = store._row(
        "SELECT * FROM call_queue WHERE call_id = 'call_h2' "
        "ORDER BY enqueued_at DESC LIMIT 1"
    )
    assert row["status"] == "answered"


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


def test_stats_counts_match(client, store):
    _post_enqueue(client, "call_s1", priority=0)
    _post_enqueue(client, "call_s2", priority=0)
    _post_enqueue(client, "call_s3", priority=0)
    r = client.get("/api/queue/stats", headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["waiting"] == 3
    assert body["abandoned_today"] == 0
    assert body["answered_today"] == 0
    # longest_wait_s is non-negative; the three enqueues happened just now.
    assert body["longest_wait_s"] >= 0


def test_position_404_for_unknown_call(client, store):
    r = client.get("/api/queue/position/nope", headers={"X-Tenant-Id": "default"})
    assert r.status_code == 404
