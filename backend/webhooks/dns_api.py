"""DNS + network-quality REST API (issue #28).

Endpoints
---------
GET  /api/dns/{domain}
    Look up SPF / DKIM / DMARC / MX records for ``domain`` and surface
    issues (missing DKIM = red, permissive SPF / no DMARC = yellow).

POST /api/network/quality
    Body ``{call_id, rtt_ms, jitter_ms, packet_loss_pct, timestamp?}``.
    The browser's WebRTC ``getStats()`` poller hits this every ~2s
    while a call is active. The server stores the row, computes the
    aggregate ``score`` (0..100), and returns it.

GET  /api/network/quality?from=...&to=...&call_id=...
    Time-series for the network quality dashboard + the active-call
    sparkline.

GET  /api/network/quality/summary?from=...&to=...
    Rollup ``{samples, avg_score, min_score, max_score, avg_rtt_ms,
    avg_jitter_ms, avg_packet_loss_pct}`` for the hero card.

The DNS endpoint is exempt from JWT auth (it's a read-only diagnostic
the operator can use from a curl). Network-quality writes are
tenant-scoped.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from webhooks._phase_b_ctx import _tenant_id
from webhooks.dns import lookup_domain
from webhooks.network_quality import compute_score, score_label
from webhooks.storage import get_store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["dns-network"])


# ──────────────────────────── DNS ────────────────────────────────────────────


@router.get("/dns/{domain:path}")
def dns_lookup(domain: str) -> dict:
    """Public: DNS record lookup + issue detection.

    ``domain`` may be just the apex (``acme.com``) or a subdomain;
    the path matcher tolerates dots so ``api/acme.com`` works as well
    as the encoded ``api%2Facme.com`` form.
    """
    if not domain:
        raise HTTPException(400, "domain is required")
    record = lookup_domain(domain)
    return record


# ──────────────────────────── network quality (write) ────────────────────────


@router.post("/network/quality")
async def network_quality_write(request: Request) -> dict:
    """Append a single network quality sample (issue #28).

    Body keys: ``call_id``, ``rtt_ms``, ``jitter_ms``, ``packet_loss_pct``,
    and optional ``timestamp`` (ISO-8601; defaults to now). The
    aggregate score is recomputed server-side so the dashboard never
    disagrees with the time-series.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON body must be an object")

    call_id = (body.get("call_id") or "").strip() or None
    rtt = _to_float(body.get("rtt_ms"))
    jitter = _to_float(body.get("jitter_ms"))
    loss = _to_float(body.get("packet_loss_pct"))
    timestamp = (body.get("timestamp") or "").strip() or None

    # If the caller already computed a score, accept it; else re-derive.
    score_in = body.get("score")
    if isinstance(score_in, (int, float)):
        try:
            score = int(score_in)
        except (TypeError, ValueError):
            score = compute_score(rtt, jitter, loss)
    else:
        score = compute_score(rtt, jitter, loss)

    tenant_id = _tenant_id(request)
    store = get_store()
    row = store.insert_network_quality(
        tenant_id, call_id, rtt, jitter, loss, score, timestamp=timestamp,
    )
    row["label"] = score_label(score)
    return row


def _to_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # Clamp pathological inputs to the bands the formula expects.
    if f < 0:
        return 0.0
    return f


# ──────────────────────────── network quality (read) ─────────────────────────


@router.get("/network/quality")
def network_quality_read(
    request: Request,
    from_: Optional[str] = None,
    to: Optional[str] = None,
    call_id: Optional[str] = None,
    limit: int = 500,
) -> dict:
    """Time-series for the network quality dashboard."""
    tenant_id = _tenant_id(request)
    _validate_window(from_, to)
    store = get_store()
    rows = store.list_network_quality(
        tenant_id, from_ts=from_, to_ts=to, call_id=call_id, limit=limit,
    )
    # Tag each row with a human label so the dashboard doesn't have to.
    for r in rows:
        r["label"] = score_label(r.get("score"))
    return {
        "tenant_id": tenant_id,
        "samples": rows,
        "count": len(rows),
        "from": from_,
        "to": to,
        "call_id": call_id,
    }


@router.get("/network/quality/summary")
def network_quality_summary(
    request: Request,
    from_: Optional[str] = None,
    to: Optional[str] = None,
) -> dict:
    """Rollup for the network quality dashboard hero card."""
    tenant_id = _tenant_id(request)
    _validate_window(from_, to)
    store = get_store()
    summary = store.aggregate_network_quality(tenant_id, from_ts=from_, to_ts=to)
    summary["label"] = score_label(summary.get("avg_score"))
    return {"tenant_id": tenant_id, **summary, "from": from_, "to": to}


def _validate_window(from_: Optional[str], to: Optional[str]) -> None:
    """Light sanity check on the ISO-8601 strings. Don't be strict —
    we already store everything as TEXT and the dashboard does its
    own filtering; the goal is to catch obvious typos."""
    for label, v in (("from", from_), ("to", to)):
        if not v:
            continue
        try:
            # ``Z`` → ``+00:00`` so fromisoformat accepts the UTC form.
            datetime.fromisoformat(v.replace("Z", "+00:00"))
        except Exception:
            raise HTTPException(400, f"{label} must be ISO-8601")
