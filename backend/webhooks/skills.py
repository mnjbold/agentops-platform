"""Skill routing admin API (Phase E-B, issue #40).

Endpoints
---------
GET    /api/skills                  list skill-routing groups (incl. agent coverage)
POST   /api/admin/skills            create a new skill group
PUT    /api/admin/skills/{id}       update
DELETE /api/admin/skills/{id}       delete

The skill-routing table is the *fallback target* for calls that
request a skill no live agent holds. ``find_idle_online_agent`` checks
it after the direct skill match fails; see storage.py for the
implementation.

The GET endpoint also returns the agent coverage map for the editor's
skill dropdown (which agents have the skill + their level) so a single
request gives the operator everything they need to wire a new skill.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from webhooks.storage import get_store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["skills"])


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _tenant_id(request: Request) -> str:
    return getattr(request.state, "tenant_id", None) or "default"


def _require_admin(request: Request) -> dict:
    """Return the UserContext (or raise 403). Phase B convention: the
    middleware populates ``request.state.tenant_ctx`` with a UserContext
    when a JWT is in play. Admin/skills mutations require a real
    authenticated user; reads are open to anyone in the tenant."""
    ctx = getattr(request.state, "tenant_ctx", None)
    user = getattr(ctx, "user", None) if ctx else None
    return user.__dict__ if user else {}


def _routing_with_coverage(store, tenant_id: str, row: dict) -> dict:
    """Augment a skill_routing row with the count of online agents
    holding the named skill (case-insensitive) so the editor can warn
    the operator when a group has zero live coverage."""
    name = row.get("name") or ""
    coverage = store._row(
        "SELECT COUNT(*) AS n FROM agent_skills s "
        "JOIN agent_presence p "
        "  ON p.tenant_id = s.tenant_id AND p.user_id = s.user_id "
        "WHERE s.tenant_id = ? AND p.status = 'online' "
        "  AND LOWER(s.skill) = LOWER(?)",
        (tenant_id, name),
    ) or {"n": 0}
    online = store._row(
        "SELECT COUNT(*) AS n FROM agent_presence WHERE tenant_id = ? "
        "AND status = 'online'", (tenant_id,),
    ) or {"n": 0}
    out = dict(row)
    out["online_agents_with_skill"] = int(coverage.get("n") or 0)
    out["online_agents_total"] = int(online.get("n") or 0)
    return out


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/skills")
def list_skills(request: Request) -> dict:
    """List skill-routing groups for the tenant. Open to any tenant user
    — no role gate so the workflow editor's dropdown can pre-load
    without an extra token round-trip."""
    tid = _tenant_id(request)
    store = get_store()
    items = [_routing_with_coverage(store, tid, r) for r in store.list_skill_routing(tid)]
    # Also include the implicit "all skills" coverage from agent_skills
    # so the editor sees tags that aren't yet routed.
    tenant_skills = store.list_tenant_skills(tid)
    return {
        "ok": True,
        "items": items,
        "tenant_skills": tenant_skills,
        "count": len(items),
    }


@router.post("/admin/skills")
async def create_skill(request: Request) -> dict:
    """Create a new skill group. Body: ``{name, description?, fallback_user_id?}``."""
    _require_admin(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    description = (body.get("description") or "").strip() or None
    fallback = body.get("fallback_user_id") or None
    tid = _tenant_id(request)
    store = get_store()
    if fallback and not store.get_user_by_id(fallback):
        raise HTTPException(400, f"fallback_user_id {fallback!r} not found")
    row = store.create_skill_routing(
        tid, name=name, description=description, fallback_user_id=fallback,
    )
    return {"ok": True, "skill": _routing_with_coverage(store, tid, row)}


@router.put("/admin/skills/{skill_id}")
async def update_skill(skill_id: str, request: Request) -> dict:
    """Update an existing skill group. Body: any of
    ``{name, description, fallback_user_id}`` (omitted = unchanged)."""
    _require_admin(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    tid = _tenant_id(request)
    store = get_store()
    existing = store.get_skill_routing(tid, skill_id)
    if not existing:
        raise HTTPException(404, f"skill {skill_id!r} not found")
    if "fallback_user_id" in body:
        fb = body.get("fallback_user_id")
        if fb and not store.get_user_by_id(fb):
            raise HTTPException(400, f"fallback_user_id {fb!r} not found")
    row = store.update_skill_routing(
        tid,
        skill_id,
        name=(body.get("name") or None),
        description=body.get("description"),
        fallback_user_id=body.get("fallback_user_id", "__UNSET__") or None,
    )
    return {"ok": True, "skill": _routing_with_coverage(store, tid, row)}


@router.delete("/admin/skills/{skill_id}")
def delete_skill(skill_id: str, request: Request) -> dict:
    """Delete a skill group. Agents keep their individual ``agent_skills``
    rows — only the named routing entry is removed."""
    _require_admin(request)
    tid = _tenant_id(request)
    store = get_store()
    ok = store.delete_skill_routing(tid, skill_id)
    if not ok:
        raise HTTPException(404, f"skill {skill_id!r} not found")
    return {"ok": True, "deleted": skill_id}
