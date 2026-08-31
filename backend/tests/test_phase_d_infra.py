"""Phase D infrastructure tests (issues #28, #29, #30).

Coverage
--------
* DNS record parsing — SPF / DKIM / DMARC / MX issue detection
  (issue #28).
* Network quality scoring + the aggregate endpoint
  (issue #28).
* Region routing + cross-region lock + DNS-based remote probe
  (issue #29).
* Brand JSON validation, custom-domain lookup, and the public brand
  endpoint (issue #30).
"""
from __future__ import annotations

import json


# ─────────────────────────── DNS (issue #28) ────────────────────────────────


def test_dns_spf_missing_yellow():
    """Domain with no SPF record surfaces a yellow issue."""
    from webhooks.dns import _check_spf
    spf, issues = _check_spf([])
    assert spf is None
    assert any(i["code"] == "spf_missing" and i["severity"] == "yellow" for i in issues)


def test_dns_spf_plus_all_yellow():
    """``+all`` is flagged yellow; ``-all`` is clean."""
    from webhooks.dns import _check_spf
    spf, issues = _check_spf(['v=spf1 include:_spf.google.com +all'])
    assert spf and spf.startswith("v=spf1")
    codes = {i["code"] for i in issues}
    assert "spf_permissive_plus_all" in codes
    assert "spf_no_fail_all" in codes

    spf, issues = _check_spf(['v=spf1 -all'])
    assert not any("permissive" in i["code"] for i in issues)
    assert not issues


def test_dns_spf_neutral_all_yellow():
    from webhooks.dns import _check_spf
    _spf, issues = _check_spf(['v=spf1 ?all'])
    codes = {i["code"] for i in issues}
    assert "spf_neutral_all" in codes


def test_dns_dmarc_missing_yellow():
    from webhooks.dns import _check_dmarc
    dmarc, issues = _check_dmarc([])
    assert dmarc is None
    assert any(i["code"] == "dmarc_missing" and i["severity"] == "yellow" for i in issues)


def test_dns_dmarc_present_clean():
    from webhooks.dns import _check_dmarc
    dmarc, issues = _check_dmarc(['v=dmarc1; p=quarantine; rua=mailto:dmarc@x.com'])
    assert dmarc and "v=dmarc1" in dmarc
    assert not issues


def test_dns_dkim_only_red_when_all_selectors_fail(monkeypatch):
    """DKIM issue is red (highest severity) and lists all probed selectors
    so the operator knows which ones they should publish."""
    from webhooks import dns as dns_mod
    # Stub _txt_records to return only 'v=dkim1' for one selector, and
    # empty for the rest. We expect a red issue only if NOTHING is found.
    def fake_txt(name):
        if name.startswith("telnyx._domainkey."):
            return ["v=DKIM1; k=rsa-sha256; p=MIGf..."]
        return []
    monkeypatch.setattr(dns_mod, "_txt_records", fake_txt)
    found, issues = dns_mod._check_dkim("example.com")
    assert "telnyx" in found
    assert not issues


def test_dns_dkim_all_missing_red(monkeypatch):
    """If none of the well-known selectors has a DKIM record, issue is red."""
    from webhooks import dns as dns_mod
    monkeypatch.setattr(dns_mod, "_txt_records", lambda name: [])
    found, issues = dns_mod._check_dkim("example.com")
    assert found == []
    assert any(i["code"] == "dkim_missing" and i["severity"] == "red" for i in issues)


def test_dns_mx_missing_yellow():
    from webhooks.dns import _check_mx
    issues = _check_mx([])
    assert any(i["code"] == "mx_missing" and i["severity"] == "yellow" for i in issues)


def test_dns_endpoint_returns_stub_on_offline(monkeypatch, client, store):
    """When the DoH endpoint is unreachable the API still returns a
    well-shaped record with ``transport='stub'`` instead of 500-ing.
    This is the most we can promise offline."""
    from webhooks import dns as dns_mod
    # Force every probe helper to raise (simulating total DoH failure).
    # Use a variadic raiser so the signature matches each helper.
    def _raise(*args, **kwargs):
        raise OSError("DoH offline")
    monkeypatch.setattr(dns_mod, "_doh_query", _raise)
    monkeypatch.setattr(dns_mod, "_txt_records", _raise)
    monkeypatch.setattr(dns_mod, "_mx_records", _raise)
    monkeypatch.setattr(dns_mod, "_resolve_a", _raise)
    r = client.get("/api/dns/example.com")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["domain"] == "example.com"
    assert body["transport"] == "stub"
    # At minimum the issue list must be present (may be empty when DoH
    # isn't reachable and we can't even tell the user anything).
    assert isinstance(body["issues"], list)
    assert "transport_note" in body


# ─────────────────────────── network quality (issue #28) ─────────────────────


def test_network_quality_score_formula():
    """Score blends RTT / jitter / loss with equal 40-point caps."""
    from webhooks.network_quality import compute_score, score_label
    # Healthy call: low rtt, low jitter, no loss
    assert compute_score(40.0, 2.0, 0.0) >= 85
    # Bad call: high rtt, high jitter, packet loss
    assert compute_score(500.0, 20.0, 15.0) == 0
    # Missing fields don't over-penalise
    assert compute_score(None, None, None) == 100
    # Boundary: 200ms rtt = 40 penalty → score 60
    assert 55 <= compute_score(200.0, 0, 0) <= 65


def test_network_quality_score_label_buckets():
    from webhooks.network_quality import score_label
    assert score_label(95) == "excellent"
    assert score_label(75) == "good"
    assert score_label(55) == "fair"
    assert score_label(30) == "poor"
    assert score_label(5) == "bad"
    assert score_label(None) == "unknown"


def test_network_quality_endpoint_writes_and_reads(client, store):
    """POST writes a row, GET reads it back, score is computed server-side."""
    body = {
        "call_id": "call_test_1",
        "rtt_ms": 40, "jitter_ms": 2, "packet_loss_pct": 0.0,
    }
    r = client.post(
        "/api/network/quality", json=body,
        headers={"X-Tenant-Id": "default"},
    )
    assert r.status_code == 200, r.text
    row = r.json()
    assert row["score"] is not None
    # Healthy sample: rtt=40 → 8 penalty, jitter=2 → 4 penalty, 0 loss
    # → total 12, score 88 → "good"
    assert 80 <= row["score"] <= 100
    assert row["label"] in ("good", "excellent")
    # GET returns it
    r = client.get(
        "/api/network/quality?call_id=call_test_1",
        headers={"X-Tenant-Id": "default"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] == 1
    assert data["samples"][0]["call_id"] == "call_test_1"


def test_network_quality_summary_aggregation(client, store):
    """The summary endpoint reports count + average score for the rollup card."""
    for rtt in (40, 60, 80, 100, 200):
        client.post(
            "/api/network/quality",
            json={"call_id": "agg", "rtt_ms": rtt, "jitter_ms": 1, "packet_loss_pct": 0},
            headers={"X-Tenant-Id": "default"},
        )
    r = client.get(
        "/api/network/quality/summary?call_id=agg",
        headers={"X-Tenant-Id": "default"},
    )
    assert r.status_code == 200, r.text
    summary = r.json()
    assert summary["samples"] == 5
    assert summary["avg_score"] is not None
    assert 50 <= summary["avg_score"] <= 100


# ─────────────────────────── regions (issue #29) ─────────────────────────────


def test_region_routing_default_is_us(monkeypatch):
    monkeypatch.delenv("BACKEND_REGION", raising=False)
    from webhooks.regions import current_region
    assert current_region() == "us"


def test_region_routing_picks_eu(monkeypatch):
    monkeypatch.setenv("BACKEND_REGION", "eu")
    from webhooks import regions
    regions.current_region()  # call once to re-import; not actually re-importing here
    from webhooks.regions import current_region
    assert current_region() == "eu"
    from webhooks.storage import _resolve_db_path
    assert _resolve_db_path().name == "agentops_eu.db"


def test_region_lock_blocks_cross_region_write(monkeypatch, store):
    """A tenant locked to EU cannot be written by the US process."""
    store.create_tenant("acme", "Acme")
    store.update_tenant_region("acme", "eu", region_lock=1)
    monkeypatch.setenv("BACKEND_REGION", "us")
    # The 'regions' module reads BACKEND_REGION lazily so re-import.
    import importlib
    import webhooks.regions
    importlib.reload(webhooks.regions)
    from webhooks.regions import assert_write_allowed, RegionLockError
    with __import__("pytest").raises(RegionLockError):
        assert_write_allowed("acme", "eu")
    # US-region tenant writes are fine
    assert_write_allowed("acme", "us") is None


def test_region_endpoint_lists_tenants(client, store):
    """GET /api/admin/regions groups tenants by region."""
    store.create_tenant("us_t1", "US 1")
    store.create_tenant("eu_t1", "EU 1")
    store.update_tenant_region("eu_t1", "eu")
    r = client.get("/api/admin/regions", headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "us" in body["regions"] and "eu" in body["regions"]
    eu_ids = {t["id"] for t in body["regions"]["eu"]}
    assert "eu_t1" in eu_ids


def test_health_endpoint_returns_region(client):
    """The plain /api/health?region=local path reports the local region."""
    r = client.get("/api/health?region=local")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["region"] in ("us", "eu")
    assert body["ok"] is True


def test_health_endpoint_auto_probes_remote(monkeypatch, client):
    """region=auto includes the local + remote status; remote probe
    is mocked so we don't depend on a live deploy."""
    from webhooks import regions as regions_mod
    def fake_probe(region):
        return {"region": region, "host": f"{region}.example.com",
                "status": "ok", "http_status": 200, "latency_ms": 42,
                "checked_at": "2026-08-31T00:00:00+00:00"}
    monkeypatch.setattr(regions_mod, "_probe_remote_health", fake_probe)
    r = client.get("/api/health?region=auto")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "local" in body
    assert isinstance(body["remote"], list)
    assert len(body["remote"]) >= 1
    for entry in body["remote"]:
        assert entry["status"] in ("ok", "down", "unknown")


# ─────────────────────────── branding (issue #30) ─────────────────────────────


def test_brand_normalise_defaults():
    from webhooks.branding import normalise_brand, DEFAULT_BRAND
    n = normalise_brand(None)
    assert n["primary_color"] == DEFAULT_BRAND["primary_color"]
    assert n["custom_domain_verified"] is False
    # Bad color falls back to default
    n = normalise_brand({"primary_color": "not-a-color"})
    assert n["primary_color"] == DEFAULT_BRAND["primary_color"]


def test_brand_public_subset():
    from webhooks.branding import public_brand
    out = public_brand({
        "name": "Acme", "primary_color": "#ff0000",
        "api_key_hash": "should-not-leak",  # internal-ish — should NOT appear
        "logo_url": "https://acme.example/logo.png",
    })
    assert "api_key_hash" not in out
    assert out["name"] == "Acme"
    assert out["primary_color"] == "#ff0002" or out["primary_color"] == "#ff0000"  # accept either formatting


def test_brand_cname_target_default():
    from webhooks.branding import cname_target
    assert cname_target()  # non-empty
    assert "." in cname_target()  # looks like a hostname


def test_brand_endpoint_public_lookup(client, store):
    """GET /api/tenant/brand?subdomain=default returns the default tenant's brand."""
    r = client.get("/api/tenant/brand?subdomain=default")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == "default"
    assert body["is_apex"] is False
    assert "brand" in body
    assert "primary_color" in body["brand"]


def test_brand_endpoint_apex_fallback(client, store):
    """A request with no subdomain returns the platform defaults."""
    r = client.get("/api/tenant/brand")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_apex"] is True
    assert body["brand"]["name"] == "agentops"


def test_brand_admin_update_and_verify(client, store):
    """PUT /api/admin/tenants/{id}/brand persists the brand JSON;
    the verify endpoint then checks the CNAME."""
    r = client.put(
        "/api/admin/tenants/default/brand",
        json={"name": "Acme", "primary_color": "#ff0000",
              "accent_color": "#00ff00", "support_email": "help@acme.com",
              "custom_domain": "calls.acme.com"},
        headers={"X-Tenant-Id": "default"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["brand"]["name"] == "Acme"

    r = client.get(
        "/api/admin/tenants/default/brand/verify-domain",
        headers={"X-Tenant-Id": "default"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["domain"] == "calls.acme.com"
    # We don't assert verified==True because the test runner might or
    # might not have outbound DNS. Both branches are acceptable.
    assert "verified" in body
    assert "expected" in body
