"""Multi-tenant + auth tests for Phase A (issues #2, #3, #6, #9).

Coverage:
* tenant creation + API key auth
* cross-tenant isolation (tenant A's key cannot read tenant B's data)
* JWT login + bearer token
* rate limit (429 + Retry-After)
* X-Tenant-Id deprecation fallback
* per-tenant secret store (encryption round-trip + missing-key semantics)
"""
from __future__ import annotations

import time

import pytest


def _create_tenant(client, name: str = "Acme", tier: str = "free") -> dict:
    """Helper: POST /api/admin/tenants with the default-tenant auth.
    The default tenant is the only one allowed to mint new tenants in v1.
    """
    # No auth header → middleware falls back to X-Tenant-Id: default.
    r = client.post("/api/admin/tenants",
                    json={"name": name, "tier": tier},
                    headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200, r.text
    return r.json()


def test_tenants_create_returns_key_once(client):
    """POST /api/admin/tenants returns tenant_id + api_key exactly once."""
    body = _create_tenant(client, name="Acme")
    assert body["tenant_id"].startswith("t_")
    assert body["api_key"].startswith("w3j_")
    assert body["name"] == "Acme"
    assert body["tier"] == "free"
    # The key is bcrypt-hashed; we never re-expose the plaintext.
    r2 = client.get(f"/api/admin/tenants/{body['tenant_id']}",
                    headers={"X-Api-Key": body["api_key"]})
    assert r2.status_code == 200
    assert "api_key" not in r2.json()["tenant"]


def test_contacts_tenant_isolation(client, store):
    """Tenant A's key cannot read tenant B's contacts (issue #2 acceptance)."""
    a = _create_tenant(client, name="Alpha")
    b = _create_tenant(client, name="Beta")

    # Seed a contact in each tenant via the store (bypassing the API).
    store.create_contact(a["tenant_id"], "Ada", "+15551110001")
    store.create_contact(b["tenant_id"], "Bob", "+15551110002")

    # Alpha's key sees only its contact.
    r = client.get("/api/contacts", headers={"X-Api-Key": a["api_key"]})
    assert r.status_code == 200
    names = [c["name"] for c in r.json()["contacts"]]
    assert names == ["Ada"]

    # Beta's key sees only its contact.
    r = client.get("/api/contacts", headers={"X-Api-Key": b["api_key"]})
    assert r.status_code == 200
    names = [c["name"] for c in r.json()["contacts"]]
    assert names == ["Bob"]

    # Cross-tenant call to /api/admin/tenants/{other}/secrets → 403
    r = client.get(f"/api/admin/tenants/{b['tenant_id']}/secrets",
                   headers={"X-Api-Key": a["api_key"]})
    assert r.status_code == 403


def test_invalid_api_key_returns_401(client):
    r = client.get("/api/contacts", headers={"X-Api-Key": "w3j_bogus"})
    assert r.status_code == 401


def test_legacy_x_tenant_id_still_works(client, store):
    """X-Tenant-Id without X-Api-Key still works (with deprecation log)."""
    store.create_contact("default", "Legacy", "+15551110000")
    r = client.get("/api/contacts", headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200
    names = [c["name"] for c in r.json()["contacts"]]
    assert "Legacy" in names


def test_secret_store_encryption_roundtrip(client, store):
    """PUT/GET /api/admin/tenants/{id}/secrets/{key} encrypts at rest."""
    a = _create_tenant(client, name="Acme2")
    headers = {"X-Api-Key": a["api_key"]}
    r = client.put(
        f"/api/admin/tenants/{a['tenant_id']}/secrets/telnyx_api_key",
        json={"value": "KEY123_topsecret"},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    # GET returns only the key + timestamps, NEVER the value.
    r = client.get(f"/api/admin/tenants/{a['tenant_id']}/secrets",
                   headers=headers)
    assert r.status_code == 200
    keys = [s["key"] for s in r.json()["secrets"]]
    assert "telnyx_api_key" in keys
    for s in r.json()["secrets"]:
        assert "value" not in s
        assert "value_encrypted" not in s

    # The stored row in the DB is ciphertext, not plaintext.
    ciphertext = store.get_secret(a["tenant_id"], "telnyx_api_key")
    assert ciphertext is not None
    assert "KEY123" not in ciphertext  # not the plaintext

    # The helper decrypts back to the plaintext.
    from webhooks.admin_api import get_tenant_secret
    assert get_tenant_secret(a["tenant_id"], "telnyx_api_key") == "KEY123_topsecret"


def test_jwt_login_happy_path(client, store):
    """POST /api/auth/login with the seeded admin user returns a JWT."""
    # Seed an admin user manually (bypassing the .env migration).
    from webhooks.auth_api import create_initial_user
    from webhooks.tenancy import issue_jwt
    create_initial_user(tenant_id="default", email="admin@default.local",
                        password="hunter2", role="admin")

    r = client.post("/api/auth/login",
                    json={"email": "admin@default.local", "password": "hunter2"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "Bearer"
    assert body["tenant_id"] == "default"
    assert body["user"]["email"] == "admin@default.local"
    assert body["expires_at"] > int(time.time())

    # /api/auth/me decodes the JWT and returns the user.
    r2 = client.get("/api/auth/me",
                    headers={"Authorization": f"Bearer {body['access_token']}"})
    assert r2.status_code == 200
    me = r2.json()["user"]
    assert me["email"] == "admin@default.local"
    assert me["tenant_id"] == "default"


def test_jwt_login_wrong_password(client):
    from webhooks.auth_api import create_initial_user
    create_initial_user(tenant_id="default", email="admin@default.local",
                        password="hunter2", role="admin")
    r = client.post("/api/auth/login",
                    json={"email": "admin@default.local", "password": "WRONG"})
    assert r.status_code == 401


def test_rate_limit_enforced(client, store, monkeypatch):
    """Bursting past the limit returns 429 with a Retry-After header.

    We shrink the bucket via ``rate_limit_set`` so the test is fast.
    """
    from webhooks import tenancy
    tenancy.rate_limit_set(limit=5, window=60)
    tenancy.rate_limit_reset()

    a = _create_tenant(client, name="RL")
    headers = {"X-Api-Key": a["api_key"]}
    statuses = []
    for _ in range(10):
        r = client.get("/api/contacts", headers=headers)
        statuses.append(r.status_code)
    ok = sum(1 for s in statuses if s == 200)
    throttled = sum(1 for s in statuses if s == 429)
    assert ok == 5, f"expected 5 200s, got {ok}"
    assert throttled == 5, f"expected 5 429s, got {throttled}"
    # The 429s carry a Retry-After header.
    throttled_resp = client.get("/api/contacts", headers=headers)
    assert throttled_resp.status_code == 429
    assert "Retry-After" in throttled_resp.headers
    assert int(throttled_resp.headers["Retry-After"]) >= 1


def test_health_exempt_from_rate_limit(client):
    """/health is in the exempt list — should never 429."""
    # Burst it; none should 429.
    for _ in range(110):
        r = client.get("/health")
        assert r.status_code == 200
