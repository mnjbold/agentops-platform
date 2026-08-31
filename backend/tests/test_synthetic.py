"""Synthetic test-mode tests (issue #24).

Coverage:
* run_synthetic_batch generates N rows with deterministic outcomes when
  seeded; the distribution matches the named preset.
* All-100% distributions yield the single outcome.
* The custom_weights path resolves into a normalised dict.
* aggregate_distribution produces the per-outcome counts.
* POST /api/campaigns/{id}/test inserts N rows, returns elapsed_ms,
  and the rows have a valid outcome from the campaign's vocabulary.
* GET /api/campaigns/{id}/test-log returns the rows.
* GET /api/campaigns/{id}/test-summary returns the totals.
* 100 synthetic calls finish in <5s (the test's SLA assertion).
"""
from __future__ import annotations

import time

import pytest


# ─────────────────────── synthetic_caller pure module ───────────────────────


def test_synthetic_batch_size_zero_raises():
    from synthetic_caller import run_synthetic_batch
    with pytest.raises(ValueError):
        run_synthetic_batch("cmp_1", 0, "mixed", seed=1)


def test_synthetic_batch_size_negative_raises():
    from synthetic_caller import run_synthetic_batch
    with pytest.raises(ValueError):
        run_synthetic_batch("cmp_1", -5, "mixed", seed=1)


def test_synthetic_all_answer_distribution():
    """The 'all_answer' preset yields only 'answer' outcomes."""
    from synthetic_caller import run_synthetic_batch, aggregate_distribution
    calls = run_synthetic_batch("cmp_1", 50, "all_answer", seed=42)
    assert len(calls) == 50
    assert all(c.outcome == "answer" for c in calls)
    dist = aggregate_distribution(calls)
    assert dist == {"answer": 50}


def test_synthetic_all_voicemail_distribution():
    from synthetic_caller import run_synthetic_batch
    calls = run_synthetic_batch("cmp_1", 30, "all_voicemail", seed=1)
    assert all(c.outcome == "voicemail" for c in calls)
    assert all("[voicemail drop played]" in c.transcript for c in calls)


def test_synthetic_mixed_distribution_is_normalised():
    """The mixed preset yields a mix but never a wrong outcome."""
    from synthetic_caller import run_synthetic_batch, VALID_OUTCOMES
    calls = run_synthetic_batch("cmp_1", 200, "mixed", seed=7)
    outcomes = {c.outcome for c in calls}
    # Must be a subset of the valid vocabulary
    assert outcomes.issubset(set(VALID_OUTCOMES))
    # With 200 samples and 60% answer weight, we expect a reasonable
    # share of answers (no statistical claim, just sanity).
    n_answer = sum(1 for c in calls if c.outcome == "answer")
    assert n_answer >= 60   # 30% of 200


def test_synthetic_custom_weights_takes_precedence():
    """custom_weights overrides the named distribution."""
    from synthetic_caller import run_synthetic_batch
    calls = run_synthetic_batch(
        "cmp_1", 50, "all_answer",
        custom_weights={"voicemail": 1.0},
        seed=99,
    )
    # Even though the distribution string is 'all_answer',
    # custom_weights should win → all voicemail.
    assert all(c.outcome == "voicemail" for c in calls)


def test_synthetic_seed_is_deterministic():
    from synthetic_caller import run_synthetic_batch
    a = run_synthetic_batch("cmp_1", 50, "mixed", seed=123)
    b = run_synthetic_batch("cmp_1", 50, "mixed", seed=123)
    assert [c.outcome for c in a] == [c.outcome for c in b]
    # Different seed → likely different sequence.
    c = run_synthetic_batch("cmp_1", 50, "mixed", seed=124)
    # Allow a slim chance of equality; the assertion is loose.
    assert isinstance(c, list)


def test_synthetic_invalid_distribution_raises():
    from synthetic_caller import run_synthetic_batch
    with pytest.raises(ValueError):
        run_synthetic_batch("cmp_1", 10, "not-a-real-preset")


def test_synthetic_cycles_contact_ids():
    """If contact_ids is shorter than n, the simulator cycles through."""
    from synthetic_caller import run_synthetic_batch
    calls = run_synthetic_batch(
        "cmp_1", 5, "all_answer",
        contact_ids=["c1", "c2"], seed=1,
    )
    cids = [c.contact_id for c in calls]
    assert cids == ["c1", "c2", "c1", "c2", "c1"]


def test_synthetic_tool_calls_present_for_answered_outcomes():
    from synthetic_caller import run_synthetic_batch
    calls = run_synthetic_batch("cmp_1", 10, "all_answer", seed=1)
    for c in calls:
        assert c.tool_calls, "answered calls should have at least one tool call"
        names = {t["name"] for t in c.tool_calls}
        assert "greeting" in names


def test_synthetic_aggregate_distribution_handles_empty():
    from synthetic_caller import aggregate_distribution
    assert aggregate_distribution([]) == {}


# ─────────────────────────── /test endpoint ───────────────────────────────


def test_run_test_endpoint_inserts_n_rows_and_returns_distribution(client, store):
    """POST /api/campaigns/{id}/test creates rows + returns counts."""
    tenant = "default"
    camp = store.create_campaign(
        tenant, "Test 1", type_="call",
        from_number="+15078731084",
    )
    r = client.post(
        f"/api/campaigns/{camp['id']}/test",
        json={"n": 50, "distribution": "all_answer"},
        headers={"X-Tenant-Id": tenant},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["n"] == 50
    assert body["inserted"] == 50
    assert body["distribution"] == {"answer": 50}
    assert body["outcomes_observed"] == ["answer"]
    # elapsed_ms should be a small number (synthetic generation is fast).
    assert 0 <= body["elapsed_ms"] < 5000


def test_run_test_endpoint_validation_n_out_of_range(client, store):
    tenant = "default"
    camp = store.create_campaign(
        tenant, "Test 2", type_="call",
        from_number="+15078731084",
    )
    r = client.post(
        f"/api/campaigns/{camp['id']}/test",
        json={"n": 0, "distribution": "all_answer"},
        headers={"X-Tenant-Id": tenant},
    )
    assert r.status_code == 400
    r2 = client.post(
        f"/api/campaigns/{camp['id']}/test",
        json={"n": 10000, "distribution": "all_answer"},
        headers={"X-Tenant-Id": tenant},
    )
    assert r2.status_code == 400


def test_run_test_endpoint_unknown_campaign(client, store):
    r = client.post(
        "/api/campaigns/cmp_does_not_exist/test",
        json={"n": 10, "distribution": "all_answer"},
        headers={"X-Tenant-Id": "default"},
    )
    assert r.status_code == 404


def test_test_log_endpoint_returns_inserted_rows(client, store):
    tenant = "default"
    camp = store.create_campaign(
        tenant, "Test log", type_="call",
        from_number="+15078731084",
    )
    client.post(
        f"/api/campaigns/{camp['id']}/test",
        json={"n": 10, "distribution": "all_voicemail"},
        headers={"X-Tenant-Id": tenant},
    )
    r = client.get(
        f"/api/campaigns/{camp['id']}/test-log?limit=50",
        headers={"X-Tenant-Id": tenant},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 10
    assert all(c["outcome"] == "voicemail" for c in body["synthetic_calls"])
    # Each row has the required fields the UI needs.
    sample = body["synthetic_calls"][0]
    for k in ("id", "campaign_id", "outcome", "started_at", "ended_at",
              "transcript", "tool_calls"):
        assert k in sample, f"missing {k}"


def test_test_summary_endpoint_returns_counts(client, store):
    tenant = "default"
    camp = store.create_campaign(
        tenant, "Test summary", type_="call",
        from_number="+15078731084",
    )
    # Run three different distributions on the same campaign.
    for dist in ("all_answer", "all_voicemail", "all_no_answer"):
        client.post(
            f"/api/campaigns/{camp['id']}/test",
            json={"n": 7, "distribution": dist},
            headers={"X-Tenant-Id": tenant},
        )
    r = client.get(
        f"/api/campaigns/{camp['id']}/test-summary",
        headers={"X-Tenant-Id": tenant},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 21
    assert body["distribution"] == {
        "answer": 7, "voicemail": 7, "no_answer": 7,
    }


def test_synthetic_100_in_under_5s(client, store):
    """The acceptance criterion: 100 calls in <5s."""
    tenant = "default"
    camp = store.create_campaign(
        tenant, "Speed", type_="call",
        from_number="+15078731084",
    )
    r = client.post(
        f"/api/campaigns/{camp['id']}/test",
        json={"n": 100, "distribution": "mixed"},
        headers={"X-Tenant-Id": tenant},
    )
    body = r.json()
    assert r.status_code == 200
    assert body["inserted"] == 100
    assert body["elapsed_ms"] < 5000, (
        f"100 synthetic calls should finish in <5s, got {body['elapsed_ms']}ms"
    )
