"""Phone number provisioning tests (issue #15)."""
from __future__ import annotations

import pytest


def test_numbers_list_and_upsert(client, store):
    """GET /api/numbers returns the tenant's owned numbers (upserted
    locally without hitting Telnyx)."""
    # Seed two numbers directly.
    store.upsert_phone_number("default", "+15125550100",
                              telnyx_id="+15125550100", country_code="US",
                              monthly_cost=1.0, per_minute_rate=0.02)
    store.upsert_phone_number("default", "+15125550101",
                              telnyx_id="+15125550101", country_code="US",
                              monthly_cost=1.0, per_minute_rate=0.02)

    r = client.get("/api/numbers", headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200, r.text
    nums = r.json()["numbers"]
    assert len(nums) == 2
    phones = {n["phone_number"] for n in nums}
    assert "+15125550100" in phones
    assert "+15125550101" in phones


def test_number_assignment_patch(client, store):
    """PATCH /api/numbers/{id}/assignment changes the assignment
    and writes a history row."""
    n = store.upsert_phone_number("default", "+15125550100", country_code="US")
    assert n["assignment_kind"] is None

    # First create a workflow to assign.
    wf_graph = {
        "entry_node_id": "greet",
        "nodes": [
            {"id": "greet", "type": "greeting", "params": {"text": "Hi"}},
            {"id": "hup",   "type": "hangup",   "params": {}},
        ],
        "edges": [{"from": "greet", "to": "hup"}],
    }
    r = client.post("/api/workflows",
                    json={"name": "AsnTest", "graph": wf_graph},
                    headers={"X-Tenant-Id": "default"})
    wid = r.json()["workflow"]["id"]

    # Assign the workflow.
    r = client.patch(f"/api/numbers/{n['id']}/assignment",
                     json={"kind": "workflow", "target_id": wid},
                     headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["number"]["assignment_kind"] == "workflow"
    assert body["number"]["assignment_target"] == wid

    # Read it back via GET /api/numbers/{id} — should include the
    # history.
    r = client.get(f"/api/numbers/{n['id']}",
                   headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200
    body = r.json()
    assert body["number"]["assignment_kind"] == "workflow"
    assert any(a["kind"] == "workflow" and a["target_id"] == wid
               for a in body["assignments"])

    # Clear it.
    r = client.patch(f"/api/numbers/{n['id']}/assignment",
                     json={"kind": None},
                     headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200
    assert r.json()["number"]["assignment_kind"] is None


def test_number_release(client, store):
    """DELETE /api/numbers/{id} drops the local row."""
    n = store.upsert_phone_number("default", "+15125550199", country_code="US")
    r = client.delete(f"/api/numbers/{n['id']}",
                      headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # Gone from list.
    r = client.get("/api/numbers", headers={"X-Tenant-Id": "default"})
    assert all(num["id"] != n["id"] for num in r.json()["numbers"])


def test_number_assignment_rejects_invalid_kind(client, store):
    n = store.upsert_phone_number("default", "+15125550100", country_code="US")
    r = client.patch(f"/api/numbers/{n['id']}/assignment",
                     json={"kind": "bogus"},
                     headers={"X-Tenant-Id": "default"})
    assert r.status_code == 400
