"""Compliance tests (issue #25).

Coverage:
* DNC cache miss → upstream lookup → row written + flagged
* DNC cache hit (fresh) → upstream NOT called
* DNC cache hit (expired) → upstream re-called
* bulk_check_dnc returns one entry per phone
* time_window.get_timezone for known US area codes
* is_in_window honours recipient timezone (NY vs CA in the same UTC
  hour can have different local hours)
* bulk_filter_window returns one bool per phone
* /api/compliance/preview returns will_dial + skipped counts
* preview picks up dnc_check_enabled / time_window_enabled toggles
* /api/compliance/dnc caches + /refresh re-queries
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone


# ─────────────────────────── DNC (pure module) ─────────────────────────────


def test_dnc_cache_miss_triggers_upstream_and_writes_row(store):
    """Fresh store: no cache row → upstream called → row inserted."""
    from compliance.dnc import check_dnc, get_cache_row
    # Seed numbers include +15551234567; should be flagged
    assert check_dnc("+15551234567", store=store) is True
    row = get_cache_row("+15551234567", store=store)
    assert row is not None
    assert row["is_dnc"] is True
    assert row["checked_at"]
    assert row["expires_at"]


def test_dnc_cache_hit_does_not_recheck(store):
    """After the first call, the cache row is fresh → second call uses
    the cache. We assert by writing a poisoned row first (is_dnc=False)
    and confirming check_dnc returns False without consulting the
    upstream seed."""
    from compliance.dnc import check_dnc
    from compliance import dnc as dnc_mod
    # Insert a row that's the *opposite* of what the seed would say.
    store._conn.execute(  # type: ignore[attr-defined]
        "INSERT INTO dnc_cache(id, phone, source, is_dnc, checked_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("dnc_fresh", "+15551234567", "us_dnc", 0,
         datetime.now(timezone.utc).isoformat(),
         (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()),
    )
    # Bypass the seed because cache should be authoritative.
    assert check_dnc("+15551234567", store=store) is False
    # Sanity: a phone that's NOT in the seed + NOT in cache → not DNC.
    assert check_dnc("+15078731084", store=store) is False


def test_dnc_cache_expired_rechecks_upstream(store):
    """Expired row → re-check. We plant an expired row claiming the
    phone is NOT on DNC; the upstream seed says it IS. The expired
    cache must be ignored."""
    from compliance.dnc import check_dnc
    expired_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    store._conn.execute(  # type: ignore[attr-defined]
        "INSERT INTO dnc_cache(id, phone, source, is_dnc, checked_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("dnc_stale", "+15551234567", "us_dnc", 0,
         (datetime.now(timezone.utc) - timedelta(days=31)).isoformat(),
         expired_at),
    )
    assert check_dnc("+15551234567", store=store) is True


def test_dnc_force_refresh_bypasses_cache(store):
    """force_refresh=True ignores the fresh cache and re-queries."""
    from compliance.dnc import check_dnc
    # Plant a fresh row saying "not DNC" for a phone the seed flags.
    store._conn.execute(  # type: ignore[attr-defined]
        "INSERT INTO dnc_cache(id, phone, source, is_dnc, checked_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("dnc_fresh2", "+15551234567", "us_dnc", 0,
         datetime.now(timezone.utc).isoformat(),
         (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()),
    )
    # Without force → cache wins
    assert check_dnc("+15551234567", store=store) is False
    # With force → upstream re-checked → True
    assert check_dnc("+15551234567", store=store, force_refresh=True) is True


def test_bulk_check_dnc_returns_one_entry_per_phone(store):
    """bulk_check_dnc is a thin wrapper; just confirm shape and counts.

    Dict semantics naturally dedupe the input list, so the result
    contains one entry per unique phone.
    """
    from compliance.dnc import bulk_check_dnc
    res = bulk_check_dnc(["+15551234567", "+15078731084", "+15551234567"], store=store)
    # Dict dedupes → 2 unique phones
    assert len(res) == 2
    assert res["+15551234567"] is True
    assert res["+15078731084"] is False


def test_dnc_cache_stats_shape(store):
    """cache_stats returns the four counters the dashboard needs."""
    from compliance.dnc import cache_stats
    s = cache_stats(store=store)
    assert set(s.keys()) >= {"total", "dnc", "non_dnc", "expired"}
    assert s["total"] == 0
    assert s["dnc"] == 0


# ─────────────────────────── time-of-day (pure module) ─────────────────────


def test_timezone_known_us_area_codes():
    """A handful of well-known area codes map to the right IANA tz."""
    from compliance.time_window import get_timezone
    # NY
    assert get_timezone("+12125551234") == "America/New_York"
    # CA (LA)
    assert get_timezone("+13105551234") == "America/Los_Angeles"
    # Chicago
    assert get_timezone("+13125551234") == "America/Chicago"
    # Denver
    assert get_timezone("+13035551234") == "America/Denver"
    # Hawaii
    assert get_timezone("+18085551234") == "Pacific/Honolulu"


def test_timezone_country_fallback():
    """Non-NANP numbers fall through to the country map."""
    from compliance.time_window import get_timezone
    assert get_timezone("+442079812345") == "Europe/London"
    assert get_timezone("+81312345678") == "Asia/Tokyo"


def test_is_in_window_inclusive_start_exclusive_end():
    """8am local OK, 9pm not OK."""
    from compliance.time_window import is_in_window
    # Pick a phone whose timezone is deterministic and a window in that tz
    # NY: at 8:00am local, 12:00 UTC (EST) → in window
    at_8am_est = datetime(2026, 1, 15, 13, 0, tzinfo=timezone.utc)  # 8:00 EST
    assert is_in_window("+12125551234", now=at_8am_est) is True
    at_9pm_est = datetime(2026, 1, 15, 2 + 0, 0, tzinfo=timezone.utc)  # 21:00 EST
    # End is exclusive; 21:00 should be OUT
    assert is_in_window("+12125551234", now=at_9pm_est) is False
    # 20:59 in
    at_2059_est = datetime(2026, 1, 15, 1, 59, tzinfo=timezone.utc)
    assert is_in_window("+12125551234", now=at_2059_est) is True


def test_is_in_window_different_timezones_during_same_utc_hour():
    """NY at 7am ET = 4am PT. NY is OUT (before 8am), PT is OUT too.
    Now NY at 9am ET (13 UTC) = 6am PT — NY in, PT out.
    Use that to confirm tz-awareness."""
    from compliance.time_window import is_in_window
    # 13:00 UTC = 08:00 EST (winter) = 05:00 PST
    at_13_utc = datetime(2026, 1, 15, 13, 0, tzinfo=timezone.utc)
    assert is_in_window("+12125551234", now=at_13_utc) is True     # NY: 8am
    assert is_in_window("+13105551234", now=at_13_utc) is False    # LA: 5am


def test_bulk_filter_window_returns_dict():
    from compliance.time_window import bulk_filter_window
    res = bulk_filter_window(["+12125551234", "+13105551234"], now=datetime(2026, 1, 15, 13, 0, tzinfo=timezone.utc))
    assert res["+12125551234"] is True
    assert res["+13105551234"] is False


# ─────────────────────────── preview endpoint ──────────────────────────────


def _seed_contacts(store, tenant_id, n, area_code="212"):
    """Insert n contacts with predictable phone numbers."""
    ids = []
    for i in range(n):
        c = store.create_contact(
            tenant_id, f"Test {i}", f"+1{area_code}{5550000 + i:04d}"
        )
        ids.append(c["id"])
    return ids


def test_compliance_preview_skips_dnc_numbers(client, store):
    """1000 contacts, 50 on DNC. Preview reports skipped_dnc=50 and
    will_dial = total - 50 (assuming the rest are in-window)."""
    tenant = "default"
    # Seed the cache with 50 DNC numbers that match a contact phone prefix.
    for i in range(50):
        phone = f"+1212555{i:04d}"
        store._conn.execute(  # type: ignore[attr-defined]
            "INSERT INTO dnc_cache(id, phone, source, is_dnc, checked_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (f"dnc_{i}", phone, "us_dnc", 1,
             datetime.now(timezone.utc).isoformat(),
             (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()),
        )
    ids = _seed_contacts(store, tenant, 1000)
    camp = store.create_campaign(
        tenant, "Test camp", type_="call",
        from_number="+15078731084",
        contact_ids=ids,
    )

    r = client.get(
        f"/api/compliance/preview?campaign_id={camp['id']}",
        headers={"X-Tenant-Id": tenant},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1000
    assert body["skipped_dnc"] == 50
    # 950 are not on DNC. The time window check depends on current time;
    # we just assert will_dial is in (0, 950].
    assert 0 <= body["will_dial"] <= 950
    assert body["skipped_total"] == body["skipped_dnc"] + body["skipped_time"]


def test_compliance_preview_disabling_dnc_skips_nothing(client, store):
    """If dnc_check_enabled=False on the campaign, no contacts are
    skipped for DNC reasons even if their numbers are on the DNC list."""
    tenant = "default"
    phone = "+12125550100"
    store._conn.execute(  # type: ignore[attr-defined]
        "INSERT INTO dnc_cache(id, phone, source, is_dnc, checked_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("dnc_x", phone, "us_dnc", 1,
         datetime.now(timezone.utc).isoformat(),
         (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()),
    )
    c = store.create_contact(tenant, "Alice", phone)
    camp = store.create_campaign(
        tenant, "Test", type_="call", from_number="+15078731084",
        contact_ids=[c["id"]],
    )
    store.update_campaign(tenant, camp["id"], dnc_check_enabled=0)
    r = client.get(
        f"/api/compliance/preview?campaign_id={camp['id']}",
        headers={"X-Tenant-Id": tenant},
    )
    body = r.json()
    assert body["dnc_enabled"] is False
    assert body["skipped_dnc"] == 0


def test_compliance_preview_time_window_only(client, store):
    """Disable DNC; insert a contact; preview should report time window
    state without DNC skip."""
    tenant = "default"
    c = store.create_contact(tenant, "Bob", "+12125550101")
    camp = store.create_campaign(
        tenant, "T2", type_="call", from_number="+15078731084",
        contact_ids=[c["id"]],
    )
    store.update_campaign(tenant, camp["id"], dnc_check_enabled=0)
    r = client.get(
        f"/api/compliance/preview?campaign_id={camp['id']}",
        headers={"X-Tenant-Id": tenant},
    )
    body = r.json()
    assert body["dnc_enabled"] is False
    assert body["time_window_enabled"] is True
    # will_dial is 0 or 1 depending on the local time at +1 212.
    assert body["will_dial"] in (0, 1)


def test_compliance_dnc_endpoint_caches(client, store):
    """GET /api/compliance/dnc writes a cache row and is idempotent."""
    r1 = client.get(
        "/api/compliance/dnc?phone=%2B15551234567",
        headers={"X-Tenant-Id": "default"},
    )
    assert r1.status_code == 200, r1.text
    b1 = r1.json()
    assert b1["is_dnc"] is True
    assert b1["checked_at"]
    # Second call hits the cache (same phone, same source).
    r2 = client.get(
        "/api/compliance/dnc?phone=%2B15551234567",
        headers={"X-Tenant-Id": "default"},
    )
    assert r2.json()["checked_at"] == b1["checked_at"]


def test_compliance_dnc_refresh_endpoint(client, store):
    """POST /api/compliance/dnc/refresh forces a re-check."""
    client.get(
        "/api/compliance/dnc?phone=%2B15551234567",
        headers={"X-Tenant-Id": "default"},
    )
    r = client.post(
        "/api/compliance/dnc/refresh",
        json={"phone": "+15551234567"},
        headers={"X-Tenant-Id": "default"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_dnc"] is True
    assert body["phone"] == "+15551234567"


def test_compliance_stats_endpoint(client, store):
    """GET /api/compliance/stats returns a small summary dict."""
    r = client.get("/api/compliance/stats",
                   headers={"X-Tenant-Id": "default"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "total" in body
    assert "dnc" in body
    assert "expired" in body


def test_compliance_patch_campaign_updates_settings(client, store):
    """PATCH /api/campaigns/{id}/compliance sets the four fields."""
    tenant = "default"
    c = store.create_contact(tenant, "Eve", "+12125550102")
    camp = store.create_campaign(
        tenant, "T3", type_="call", from_number="+15078731084",
        contact_ids=[c["id"]],
    )
    r = client.patch(
        f"/api/campaigns/{camp['id']}/compliance",
        json={
            "dnc_check_enabled": False,
            "time_window_enabled": True,
            "time_window_start": 9,
            "time_window_end": 20,
        },
        headers={"X-Tenant-Id": tenant},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["campaign"]["dnc_check_enabled"] == 0
    assert body["campaign"]["time_window_enabled"] == 1
    assert body["campaign"]["time_window_start"] == 9
    assert body["campaign"]["time_window_end"] == 20
