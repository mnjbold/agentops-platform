"""Synthetic call API (issue #24).

Endpoints
---------
POST /api/campaigns/{id}/test         — run N synthetic calls
GET  /api/campaigns/{id}/test-log     — list the rows from the last test run
GET  /api/campaigns/{id}/test-summary — outcome distribution for the chart

The simulator lives in :mod:`synthetic_caller` and is a pure function;
this router just wires it to the persistence layer + tenant auth.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from synthetic_caller import (
    VALID_OUTCOMES,
    aggregate_distribution,
    run_synthetic_batch,
)
from webhooks._phase_b_ctx import _tenant_id
from webhooks.storage import get_store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["campaigns-phase-c"])

# Hard cap on synthetic batch size — the test SLA is "100 in <5s" but
# the cap is 10x that for power users who want a bigger sample.
MAX_BATCH = 1000


@router.post("/campaigns/{campaign_id}/test")
async def run_test(campaign_id: str, request: Request) -> dict:
    """Run a batch of synthetic calls against ``campaign_id``.

    Body
    ----
    ``{"n": 100, "distribution": "mixed"}`` (or 'all_answer' / 'all_voicemail' /
    a custom weights dict).

    Behaviour
    ---------
    1. Confirm the campaign exists for this tenant.
    2. Generate N synthetic calls via :func:`run_synthetic_batch`.
    3. Insert them in one transaction (much faster than 1-by-1).
    4. Return the per-outcome counts + the elapsed_ms.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON body must be an object")
    try:
        n = int(body.get("n", 100))
    except (TypeError, ValueError):
        raise HTTPException(400, "n must be an integer")
    if n < 1 or n > MAX_BATCH:
        raise HTTPException(400, f"n must be 1..{MAX_BATCH}")
    distribution = body.get("distribution", "mixed")
    custom_weights = body.get("custom_weights")
    seed = body.get("seed")
    if seed is not None:
        try:
            seed = int(seed)
        except (TypeError, ValueError):
            raise HTTPException(400, "seed must be an integer")

    tenant_id = _tenant_id(request)
    store = get_store()
    camp = store.get_campaign(tenant_id, campaign_id)
    if not camp:
        raise HTTPException(404, f"campaign {campaign_id} not found")

    # Resolve the contact list (if any) so the synthetic rows are
    # traceable to a real contact. Falls back to None when empty.
    contact_ids: list[str] = []
    for cid in camp.get("contact_ids") or []:
        if store.get_contact(tenant_id, cid):
            contact_ids.append(cid)

    t0 = time.perf_counter()
    try:
        calls = run_synthetic_batch(
            campaign_id,
            n,
            distribution,
            contact_ids=contact_ids,
            custom_weights=custom_weights,
            seed=seed,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    # Bulk insert in a single transaction. Faster than 1-by-1 for
    # large N and keeps the table consistent on mid-batch errors.
    with store._lock:
        cur = store._conn.executemany(
            "INSERT INTO synthetic_calls("
            "id, tenant_id, campaign_id, contact_id, outcome, "
            "started_at, ended_at, transcript, tool_calls_json"
            ") VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (c.id, tenant_id, c.campaign_id, c.contact_id, c.outcome,
                 c.started_at, c.ended_at, c.transcript, json.dumps(c.tool_calls))
                for c in calls
            ],
        )
        inserted = cur.rowcount if cur is not None else 0

    distribution_counts = aggregate_distribution(calls)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return {
        "ok": True,
        "campaign_id": campaign_id,
        "n": int(n),
        "inserted": int(inserted),
        "elapsed_ms": elapsed_ms,
        "distribution": distribution_counts,
        "outcomes_observed": sorted(distribution_counts.keys()),
        "seed": seed,
    }


@router.get("/campaigns/{campaign_id}/test-log")
def list_test_log(
    campaign_id: str,
    request: Request,
    limit: int = 200,
) -> dict:
    """List the synthetic call rows for one campaign, newest first.

    The UI uses this to render the call log with the purple 'TEST'
    badge — see :mod:`frontend.src.screens.campaigns`.
    """
    tenant_id = _tenant_id(request)
    store = get_store()
    camp = store.get_campaign(tenant_id, campaign_id)
    if not camp:
        raise HTTPException(404, f"campaign {campaign_id} not found")
    if limit < 1 or limit > 1000:
        raise HTTPException(400, "limit must be 1..1000")
    rows = store.list_synthetic_calls(
        tenant_id, campaign_id=campaign_id, limit=limit,
    )
    return {"campaign_id": campaign_id, "synthetic_calls": rows, "count": len(rows)}


@router.get("/campaigns/{campaign_id}/test-summary")
def test_summary(campaign_id: str, request: Request) -> dict:
    """Outcome distribution for the chart.

    Aggregates over the *whole* synthetic call history for this
    campaign (not just the last run). The UI re-renders on every test
    so the chart shows the cumulative distribution.
    """
    tenant_id = _tenant_id(request)
    store = get_store()
    camp = store.get_campaign(tenant_id, campaign_id)
    if not camp:
        raise HTTPException(404, f"campaign {campaign_id} not found")
    counts = store.synthetic_call_outcome_summary(tenant_id, campaign_id)
    total = sum(int(v or 0) for v in counts.values())
    return {
        "campaign_id": campaign_id,
        "total": total,
        "distribution": counts,
        "outcomes_observed": sorted(counts.keys()),
    }
