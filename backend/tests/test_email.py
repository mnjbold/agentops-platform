"""Tests for the transactional email API (issue #27).

Coverage:
* POST /api/email/send            — sends via the dev provider
* GET /api/email/templates        + POST creates + duplicate 409
* POST /api/email/templates/{id}/render — {{var}} substitution
* POST /api/webhooks/email/inbound-test — routes to the email_messages
                                          table and the contact list
* GET  /api/messages/threads?channel=email — surfaces email threads
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from email_providers import get_provider
from email_providers.templates import extract_variables, render_template


# ──────────────────────────── template rendering ────────────────────────────


def test_template_renderer_substitutes_placeholders():
    """`{{name}}` → value, missing → empty string, HTML-escaped by default."""
    out = render_template("Hello {{name}}!", {"name": "<b>Alice</b>"})
    # HTML escaping converts the angle brackets to entities.
    assert out == "Hello &lt;b&gt;Alice&lt;/b&gt;!"


def test_template_renderer_missing_variable_is_empty():
    out = render_template("Hi {{first_name}} {{last_name}}", {"first_name": "A"})
    assert out == "Hi A "


def test_template_extract_variables_lists_placeholders():
    variables = extract_variables("Hi {{first_name}}, your code is {{code}}.")
    assert variables == ["first_name", "code"]


def test_dev_provider_is_default_and_writes_outbox(tmp_path, monkeypatch):
    """The default provider is ``dev`` and writes to the outbox dir."""
    # Reset the cached singleton so a fresh DevProvider picks up the new
    # outbox path.
    import email_providers as ep
    if hasattr(ep.get_provider, "_singletons"):
        ep.get_provider._singletons.clear()
    monkeypatch.setattr(
        "email_providers.email_dev._OUTBOX", tmp_path, raising=True
    )
    monkeypatch.delenv("EMAIL_PROVIDER", raising=False)
    p = get_provider()
    assert p.name == "dev"
    res = p.send(
        to="alice@example.com",
        from_addr="ops@agentops.local",
        subject="Hi",
        body="hello",
    )
    assert res["ok"] is True
    # The outbox file should have one JSON line.
    outbox_files = list(tmp_path.iterdir())
    assert len(outbox_files) == 1
    line = outbox_files[0].read_text().strip()
    record = json.loads(line)
    assert record["to"] == "alice@example.com"


# ──────────────────────────── API: templates ─────────────────────────────────


def test_create_template_lists_and_409_on_duplicate(client, store):
    """Round-trip: create → list → 409 on duplicate name."""
    r = client.post(
        "/api/email/templates",
        json={
            "name": "welcome",
            "subject_template": "Hi {{first_name}}",
            "body_template": "Welcome to {{company}}, {{first_name}}!",
        },
        headers={"X-Tenant-Id": "default"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    tpl = body["template"]
    assert tpl["name"] == "welcome"
    # variables were auto-extracted from the templates
    assert set(tpl["variables"]) == {"first_name", "company"}

    # List
    r2 = client.get("/api/email/templates",
                    headers={"X-Tenant-Id": "default"})
    assert r2.status_code == 200
    assert any(t["name"] == "welcome" for t in r2.json()["templates"])

    # Duplicate
    r3 = client.post(
        "/api/email/templates",
        json={
            "name": "welcome",
            "subject_template": "x",
            "body_template": "y",
        },
        headers={"X-Tenant-Id": "default"},
    )
    assert r3.status_code == 409


def test_render_endpoint_substitutes_variables(client, store):
    """POST /email/templates/{id}/render returns the rendered subject + body."""
    r = client.post(
        "/api/email/templates",
        json={
            "name": "reminder",
            "subject_template": "Reminder for {{first_name}}",
            "body_template": "Hi {{first_name}}, your meeting is at {{time}}.",
        },
        headers={"X-Tenant-Id": "default"},
    ).json()
    tpl_id = r["template"]["id"]
    r2 = client.post(
        f"/api/email/templates/{tpl_id}/render",
        json={"variables": {"first_name": "Bob", "time": "3pm"}},
        headers={"X-Tenant-Id": "default"},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["subject"] == "Reminder for Bob"
    assert "Hi Bob" in body["body"]
    assert "3pm" in body["body"]


# ──────────────────────────── API: send ─────────────────────────────────────


def test_send_email_persists_outbound_row(client, store, tmp_path, monkeypatch):
    """POST /email/send writes a row to email_messages and a line to the
    dev outbox (so the dashboard's thread list picks it up)."""
    import email_providers as ep
    if hasattr(ep.get_provider, "_singletons"):
        ep.get_provider._singletons.clear()
    # Redirect outbox to a tmp dir so we don't pollute the repo.
    import email_providers.email_dev as dev_mod
    monkeypatch.setattr(dev_mod, "_OUTBOX", tmp_path, raising=True)
    # Force a fresh DevProvider instance with the redirected outbox.
    if hasattr(ep.get_provider, "_singletons"):
        ep.get_provider._singletons.clear()
    r = client.post(
        "/api/email/send",
        json={
            "to": "alice@example.com",
            "from": "ops@agentops.local",
            "subject": "Greetings",
            "body": "Welcome aboard!",
        },
        headers={"X-Tenant-Id": "default"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["provider"] == "dev"
    # Outbox got the record
    assert any(tmp_path.iterdir())
    # The store has the outbound row in 'sent' state
    msgs = store.list_email_messages("default", to_addr="alice@example.com")
    assert len(msgs) == 1
    assert msgs[0]["direction"] == "outbound"
    assert msgs[0]["status"] == "sent"


def test_send_template_uses_template_variables(client, store):
    """POST /email/send-template renders the template, then sends."""
    create = client.post(
        "/api/email/templates",
        json={
            "name": "t1",
            "subject_template": "Hi {{name}}",
            "body_template": "Welcome, {{name}}!",
        },
        headers={"X-Tenant-Id": "default"},
    ).json()
    tpl_id = create["template"]["id"]
    r = client.post(
        "/api/email/send-template",
        json={
            "to": "bob@example.com",
            "template_id": tpl_id,
            "variables": {"name": "Bob"},
        },
        headers={"X-Tenant-Id": "default"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    msgs = store.list_email_messages("default", to_addr="bob@example.com")
    assert len(msgs) == 1
    # Subject should have been rendered with the variable (the dev provider
    # just writes the rendered values to disk — we read the store to verify).
    assert msgs[0]["subject"] == "Hi Bob"


# ──────────────────────────── API: inbound + channel routing ────────────────


def test_inbound_test_route_inserts_received_message(client, store):
    """POST /webhooks/email/inbound-test inserts a row and matches a contact
    by email when one exists."""
    # Seed a contact with the from address
    c = store.create_contact(
        "default", "Alice", "+15078731084",
        email="alice@example.com", tags=[],
    )
    r = client.post(
        "/api/webhooks/email/inbound-test",
        json={
            "from_addr": "alice@example.com",
            "to_addr": "ops@agentops.local",
            "subject": "Hello",
            "body": "Just saying hi",
        },
        headers={"X-Tenant-Id": "default"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    # Look up the message
    msg = store.get_email_message("default", body["id"])
    assert msg["direction"] == "inbound"
    assert msg["status"] == "received"
    assert msg["from_addr"] == "alice@example.com"


def test_messages_threads_email_channel_returns_email_threads(client, store):
    """The /api/messages/threads endpoint with channel=email returns the
    email channel's threads; ``all`` merges email + sms/whatsapp."""
    # Seed two inbound + one outbound email
    store.insert_email_message(
        "default", "dev", "inbound",
        from_addr="alice@example.com",
        to_addr="ops@agentops.local",
        subject="S1", body="hi", status="received",
    )
    store.insert_email_message(
        "default", "dev", "inbound",
        from_addr="bob@example.com",
        to_addr="ops@agentops.local",
        subject="S2", body="yo", status="received",
    )
    store.insert_email_message(
        "default", "dev", "outbound",
        from_addr="ops@agentops.local",
        to_addr="carol@example.com",
        subject="S3", body="hi carol", status="sent",
    )
    r = client.get(
        "/api/messages/threads?channel=email",
        headers={"X-Tenant-Id": "default"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["channel"] == "email"
    # Three unique remote addresses → three threads
    remotes = {t["remote"] for t in body["threads"]}
    assert remotes == {
        "alice@example.com", "bob@example.com", "carol@example.com"
    }
    # Every thread is marked as email
    for t in body["threads"]:
        assert t["channel"] == "email"
