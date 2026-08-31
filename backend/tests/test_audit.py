"""Audit tests (issue #20).

Coverage:
* A GET to /api/contacts produces an audit row with the right shape
* audit_log UPDATE/DELETE triggers RAISE — the table is append-only
* /v1/audit lists rows in reverse-chronological order
* /v1/audit/export.csv is byte-stable: same query → same bytes
* /v1/audit/{id} returns the full row including bodies
"""
from __future__ import annotations

import csv
import io
import sqlite3
import time

import pytest


def _wait_for_audit(client, store, tenant_id="default", max_wait=2.0):
    """Poll the audit table until at least one row appears (or timeout)."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        rows = store.list_audit(tenant_id, limit=10)
        if rows:
            return rows
        time.sleep(0.05)
    return []


def test_audit_middleware_logs_request(client, store):
    """A successful GET to /api/contacts creates an audit row."""
    # Fire the request.
    r = client.get("/api/contacts", headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200

    # The audit append is sync (SQLite WAL) so no wait needed in
    # practice, but we still give the middleware a moment.
    rows = _wait_for_audit(client, store)
    assert rows, "expected at least one audit row"
    # Find the contacts row.
    contacts_rows = [r for r in rows if r.get("path") == "/api/contacts"]
    assert contacts_rows, f"no /api/contacts row in {rows}"
    row = contacts_rows[0]
    assert row["action"] == "contacts.list"
    assert row["method"] == "GET"
    assert row["response_status"] == 200
    assert row["tenant_id"] == "default"
    assert row["response_time_ms"] >= 0


def test_audit_log_is_append_only(store):
    """The triggers raise on UPDATE/DELETE — the table is append-only."""
    # Insert one row directly.
    aid = store.append_audit({
        "tenant_id": "default",
        "action": "test.append",
        "method": "GET",
        "path": "/test",
        "response_status": 200,
        "response_time_ms": 1,
    })

    # UPDATE must raise.
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(  # type: ignore[attr-defined]
            "UPDATE audit_log SET action = 'tampered' WHERE id = ?",
            (aid,),
        )

    # DELETE must raise.
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(  # type: ignore[attr-defined]
            "DELETE FROM audit_log WHERE id = ?",
            (aid,),
        )


def test_audit_list_endpoint(client, store):
    """After a few requests, /v1/audit returns rows in reverse-chrono order."""
    # Seed a few rows directly so we don't depend on middleware timing.
    for i in range(3):
        store.append_audit({
            "tenant_id": "default",
            "action": f"test.action_{i}",
            "method": "GET",
            "path": f"/x/{i}",
            "response_status": 200,
            "response_time_ms": 1 + i,
            "timestamp": f"2026-08-31T00:00:0{i}+00:00",
        })
    r = client.get("/v1/audit?limit=10", headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) >= 3
    # Newest first.
    assert items[0]["action"] == "test.action_2"


def test_audit_export_csv_byte_stable(client, store):
    """Same query → same bytes."""
    store.append_audit({
        "tenant_id": "default",
        "action": "test.export",
        "method": "POST",
        "path": "/x",
        "response_status": 201,
        "response_time_ms": 5,
    })
    h = {"X-Tenant-Id": "default"}
    r1 = client.get("/v1/audit/export?format=csv", headers=h)
    r2 = client.get("/v1/audit/export?format=csv", headers=h)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.content == r2.content
    # Header check.
    first = r1.text.splitlines()[0]
    assert first.startswith("id,tenant_id,user_id,action,target,")
    # Content-Disposition for browser download.
    assert "attachment" in r1.headers.get("Content-Disposition", "")


def test_audit_get_one_returns_full_row(client, store):
    """GET /v1/audit/{id} returns the full row including bodies."""
    aid = store.append_audit({
        "tenant_id": "default",
        "action": "test.read",
        "method": "POST",
        "path": "/x",
        "target": "x_42",
        "response_status": 200,
        "response_time_ms": 7,
        "request_body": '{"hello":"world"}',
        "response_body": '{"ok":true}',
    })
    r = client.get(f"/v1/audit/{aid}", headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200, r.text
    item = r.json()["item"]
    assert item["id"] == aid
    assert item["action"] == "test.read"
    assert item["request_body"] == '{"hello":"world"}'
    assert item["response_body"] == '{"ok":true}'


def test_audit_action_derivation():
    """derive_action normalises common paths."""
    from audit.logger import derive_action
    assert derive_action("GET", "/api/contacts") == "contacts.list"
    assert derive_action("POST", "/api/auth/login") == "auth.login"
    assert derive_action("GET", "/api/auth/me") == "auth.read"
    assert derive_action("POST", "/api/admin/tenants/t_abc/rotate-key") == "admin.tenants.rotate_key"
    assert derive_action("DELETE", "/api/contacts/c1") == "contacts.delete"
