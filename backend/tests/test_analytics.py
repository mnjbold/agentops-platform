"""Analytics tests (issue #16).

Coverage:
* /v1/analytics/overview returns zero counts when the rollup is empty
* /v1/analytics/overview counts deliveries in the window
* /v1/analytics/assistants returns one row per assistant with KPIs
* /v1/analytics/export.csv returns a byte-stable CSV with the expected header
* compare=1 includes a previous-period block with the right shape
* windows.resolve_window honours the preset (today, 7d, this-month)
"""
from __future__ import annotations

from datetime import datetime, timezone


def _seed_call(store, tenant_id, kind, target="+15551110000", day=None):
    """Insert one deliveries row at a specific day."""
    day = day or datetime.now(timezone.utc).date().isoformat()
    store._conn.execute(  # type: ignore[attr-defined]
        "INSERT INTO deliveries(id, tenant_id, kind, target, status, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (f"dlv_{kind}_{day}_{target[-4:]}", tenant_id, kind, target,
         "delivered", f"{day}T12:00:00+00:00"),
    )


def test_analytics_overview_empty_rollup(client, store):
    """Empty DB returns all-zero counts; not an error."""
    r = client.get("/v1/analytics/overview", headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["current"]["total_calls"] == 0
    assert body["current"]["total_sms"] == 0
    assert body["current"]["spend_cents"] == 0
    assert len(body["current"]["busiest_hours"]) == 24
    assert body["current"]["top_agents"] == []


def test_analytics_overview_counts_deliveries(client, store):
    """Insert a few deliveries and confirm the overview reflects them."""
    today = datetime.now(timezone.utc).date().isoformat()
    _seed_call(store, "default", "outbound_call", "+15551110001", day=today)
    _seed_call(store, "default", "inbound_call",  "+15551110002", day=today)
    _seed_call(store, "default", "outbound_sms",   "+15551110003", day=today)
    _seed_call(store, "default", "inbound_sms",    "+15551110004", day=today)

    r = client.get("/v1/analytics/overview?preset=today",
                   headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200, r.text
    body = r.json()
    cur = body["current"]
    assert cur["total_calls"] == 2
    assert cur["calls_in"] == 1
    assert cur["calls_out"] == 1
    assert cur["total_sms"] == 2
    assert cur["sms_in"] == 1
    assert cur["sms_out"] == 1


def test_analytics_assistants_returns_per_assistant(client, store):
    """Two assistants + log rows → two rows in the per-assistant list."""
    a = store.create_assistant("default", "Sales Bot")
    b = store.create_assistant("default", "Support Bot")
    # Log a couple of calls for each.
    store.append_assistant_log("default", a["id"], "user", "hi", call_id="c1")
    store.append_assistant_log("default", a["id"], "assistant", "hello", call_id="c1")
    store.append_assistant_log("default", b["id"], "user", "help", call_id="c2")

    r = client.get("/v1/analytics/assistants?preset=7d",
                   headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200, r.text
    body = r.json()
    names = [row["name"] for row in body["assistants"]]
    assert "Sales Bot" in names
    assert "Support Bot" in names


def test_analytics_export_csv_byte_stable(client, store):
    """Two calls to /v1/analytics/export.csv with the same window return
    the same bytes (header is identical even when the rollup is empty)."""
    h1 = {"X-Tenant-Id": "default"}
    h2 = {"X-Tenant-Id": "default"}
    r1 = client.get("/v1/analytics/export.csv?preset=7d", headers=h1)
    r2 = client.get("/v1/analytics/export.csv?preset=7d", headers=h2)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.content == r2.content
    # Header line.
    assert r1.text.splitlines()[0] == "day,calls_in,calls_out,sms_in,sms_out,spend_cents"
    # Content-Disposition for browser download.
    assert "attachment" in r1.headers.get("Content-Disposition", "")


def test_analytics_overview_compare(client, store):
    """compare=1 returns a previous window + delta block."""
    r = client.get("/v1/analytics/overview?preset=7d&compare=1",
                   headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "previous" in body
    assert "delta" in body
    for k in ("total_calls", "total_sms", "spend_cents"):
        assert k in body["delta"]


def test_windows_resolve_presets():
    from analytics.windows import resolve_window, previous_window
    # today
    f, t = resolve_window(preset="today")
    assert f == t
    # 7d
    f, t = resolve_window(preset="7d")
    from datetime import date
    d1 = date.fromisoformat(f)
    d2 = date.fromisoformat(t)
    assert (d2 - d1).days == 6
    # this-month
    f, t = resolve_window(preset="this-month")
    d1 = date.fromisoformat(f)
    assert d1.day == 1
    # previous_window of 7d is also 7d
    pf, pt = previous_window(f, t)
    pf_d, pt_d = date.fromisoformat(pf), date.fromisoformat(pt)
    assert (pt_d - pf_d).days == (d2 - d1).days
