"""Workflow engine tests (issue #13)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


SIMPLE_GRAPH = {
    "entry_node_id": "greet",
    "nodes": [
        {"id": "greet", "type": "greeting", "params": {"text": "Hello!"}},
        {"id": "menu",  "type": "menu",     "params": {
            "prompt": "Press 1",
            "options": {"1": "fwd"},
        }},
        {"id": "fwd",   "type": "forward",  "params": {"to": "+15551234567"}},
        {"id": "hup",   "type": "hangup",   "params": {}},
    ],
    "edges": [
        {"from": "greet", "to": "menu"},
        {"from": "menu",  "to": "fwd"},
        {"from": "fwd",   "to": "hup"},
    ],
}


def test_workflow_create_list_get(client, store):
    """POST + GET /api/workflows round-trips a graph."""
    r = client.post("/api/workflows",
                    json={"name": "My IVR", "graph": SIMPLE_GRAPH},
                    headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    wid = body["workflow"]["id"]
    assert wid.startswith("wf_")
    assert body["workflow"]["version"] == 1
    # graph stored as a string in storage but exposed as a dict
    assert body["workflow"]["graph"]["nodes"][0]["type"] == "greeting"

    r = client.get("/api/workflows", headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200
    assert any(w["id"] == wid for w in r.json()["workflows"])

    r = client.get(f"/api/workflows/{wid}", headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200
    assert r.json()["workflow"]["name"] == "My IVR"


def test_workflow_rejects_cycle(client, store):
    """A workflow with a cycle is rejected at save time (issue #13 acceptance)."""
    graph = {
        "nodes": [
            {"id": "a", "type": "greeting", "params": {}},
            {"id": "b", "type": "menu",     "params": {"options": {"1": "a"}}},
        ],
        "edges": [
            {"from": "a", "to": "b"},
            {"from": "b", "to": "a"},
        ],
    }
    r = client.post("/api/workflows",
                    json={"name": "Cyclic", "graph": graph},
                    headers={"X-Tenant-Id": "default"})
    assert r.status_code == 400
    assert "cycle" in r.json()["detail"].lower()


def test_workflow_test_walks_graph(client, store):
    """POST /api/workflows/{id}/test returns a trace in <1s (acceptance)."""
    # Create
    r = client.post("/api/workflows",
                    json={"name": "TestTrace", "graph": SIMPLE_GRAPH},
                    headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200, r.text
    wid = r.json()["workflow"]["id"]

    # Walk it with no digit (the 'menu' node falls through to its first
    # edge with no condition → 'fwd').
    r = client.post(f"/api/workflows/{wid}/test",
                    json={"call_id": "test-1", "from": "+15550000001", "to": "+15078731084"},
                    headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200, r.text
    body = r.json()
    actions = [h["action"] for h in body["history"]]
    assert actions[0] == "speak"  # greeting
    assert "menu" in actions
    # The test engine should have walked at least 3 nodes (greet → menu → fwd).
    assert len(body["history"]) >= 3


def test_workflow_template_instantiation(client, store):
    """POST /api/workflows/from-template/{name} creates from a starter."""
    r = client.post("/api/workflows/from-template/basic-ivr",
                    headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["template"] == "basic-ivr"
    assert body["workflow"]["name"] == "Basic IVR"
    # The template's nodes should be in the new workflow.
    node_ids = [n["id"] for n in body["workflow"]["graph"]["nodes"]]
    assert "greet" in node_ids
    assert "menu" in node_ids


def test_workflow_assign_to_number(client, store):
    """Assign a workflow to a phone number, retrieve it, then clear it."""
    # Register a number first.
    store.upsert_phone_number("default", "+15125550100", country_code="US")
    n = store.get_phone_number_by_phone("default", "+15125550100")
    assert n is not None

    # Create a workflow.
    r = client.post("/api/workflows",
                    json={"name": "AssignTest", "graph": SIMPLE_GRAPH},
                    headers={"X-Tenant-Id": "default"})
    wid = r.json()["workflow"]["id"]

    # Assign.
    r = client.post(f"/api/numbers/{n['id']}/workflow",
                    json={"workflow_id": wid},
                    headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200, r.text
    assert r.json()["number"]["assignment_kind"] == "workflow"

    # Read back.
    r = client.get(f"/api/numbers/{n['id']}/workflow",
                   headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200
    assert r.json()["workflow"]["id"] == wid

    # And the engine can resolve it for an inbound call.
    resolved = store.find_workflow_for_number("default", "+15125550100")
    assert resolved is not None
    assert resolved["id"] == wid

    # Clear.
    r = client.post(f"/api/numbers/{n['id']}/workflow",
                    json={"workflow_id": None},
                    headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200
    assert r.json()["number"]["assignment_kind"] is None


def test_workflow_forward_agent_routes_to_online_agent(client, store, monkeypatch):
    """Issue #38: forward_agent picks an online agent with a matching
    skill, flips their presence to ``on_call``, and calls
    ``transfer_call`` on the Telnyx client with the agent's
    ``assigned_number``.
    """
    import bcrypt
    from webhooks import workflow_engine

    # 1. Seed: an agent user with the 'sales' skill, an assigned
    #    direct-DID, and an 'online' presence row.
    ph = bcrypt.hashpw(b"pw", bcrypt.gensalt(rounds=4)).decode("utf-8")
    store.create_user("u_alice", "default", "alice@default.local", ph, "agent")
    store.update_user_agent(
        "default", "u_alice", assigned_number="+15078731099",
    )
    store.set_user_skills("default", "u_alice", ["sales"])
    store.upsert_presence("default", "u_alice", "online")

    # 2. Mock the Telnyx client: ``transfer_call`` and (optionally)
    #    ``playback_start`` become MagicMocks so the engine's
    #    best-effort calls are no-ops.
    mock_client = MagicMock()
    mock_client.transfer_call.return_value = {"ok": True}

    def _fake_get_client():
        return mock_client
    monkeypatch.setattr(workflow_engine, "get_client", _fake_get_client)

    # 3. Create a workflow with a single forward_agent node bound to
    #    the 'sales' skill (the new ``data.skill`` shape).
    graph = {
        "entry_node_id": "fa",
        "nodes": [
            {"id": "fa", "type": "forward_agent", "data": {"skill": "sales"}},
        ],
        "edges": [],
    }
    r = client.post(
        "/api/workflows",
        json={"name": "FA-Sales", "graph": graph},
        headers={"X-Tenant-Id": "default"},
    )
    assert r.status_code == 200, r.text
    wid = r.json()["workflow"]["id"]

    # 4. Run the engine directly (not via the dry-run /test endpoint,
    #    which would short-circuit the client call).
    wf = store.get_workflow("default", wid)
    ctx = workflow_engine.CallContext({
        "call_id": "test-call-1",
        "tenant_id": "default",
        "from": "+15555550100",
        "to": "+15078731084",
    })
    engine = workflow_engine.WorkflowEngine(wf["graph"], client=mock_client)
    engine.run(ctx)

    # 5. The agent's presence flipped to on_call with the call id.
    pres = store.get_presence("default", "u_alice")
    assert pres is not None
    assert pres["status"] == "on_call"
    assert pres["current_call_id"] == "test-call-1"

    # 6. The Telnyx client's transfer_call was invoked with the
    #    agent's assigned number + the call's control id.
    mock_client.transfer_call.assert_called_once()
    args, kwargs = mock_client.transfer_call.call_args
    assert kwargs.get("call_control_id") == "test-call-1"
    assert kwargs.get("to") == "+15078731099"

    # 7. The history captured the forward so the UI timeline can render it.
    actions = [h.get("action") for h in ctx.history]
    assert "forward_agent" in actions
    fwd = next(h for h in ctx.history if h.get("action") == "forward_agent")
    assert fwd.get("result") == "forwarded"
    assert fwd.get("to_user") == "u_alice"
