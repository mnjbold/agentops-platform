"""Transactional email API (issue #27).

Endpoints (mounted under /api):
  POST /api/email/send                     — send via configured provider
  GET  /api/email/templates                — list the tenant's templates
  POST /api/email/templates                — create a template
  GET  /api/email/templates/{id}           — fetch one
  DELETE /api/email/templates/{id}         — delete one
  POST /api/email/templates/{id}/render    — render with supplied variables
  POST /api/email/send-template            — convenience: pick a template
                                              by id, render, send
  GET  /api/email/messages                 — list recent messages
  GET  /api/email/messages/{id}            — fetch one
  POST /api/webhooks/email/{provider}      — inbound webhook (provider name
                                              in the path; the route maps
                                              the payload via the same
                                              adapter's handle_inbound)
  POST /api/webhooks/email/inbound-test    — dev/test route: synthesise an
                                              inbound message without a real
                                              provider (handy for the UI
                                              while the production adapters
                                              are stubs)

The provider is resolved via :mod:`email_providers`; the default ``dev``
adapter writes every send to ``backend/email_outbox/outbox-YYYY-MM-DD.log``
so the dashboard's email channel is exercisable in dev / test.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request

from email_providers import get_provider
from email_providers.templates import extract_variables
from webhooks.storage import get_store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["email"])


# ──────────────────────────── helpers ───────────────────────────────────────


def _tenant_id(request: Request) -> str:
    """Phase A: ``request.state.tenant_id`` is populated by the auth
    middleware. Fall back to the legacy X-Tenant-Id header for tests."""
    tid = getattr(request.state, "tenant_id", None)
    if tid:
        return tid
    tid = request.headers.get("X-Tenant-Id") or request.headers.get("x-tenant-id")
    return (tid or "default").strip() or "default"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ──────────────────────────── send ──────────────────────────────────────────


@router.post("/email/send")
async def send_email(request: Request) -> dict:
    """Send a single email through the configured provider.

    Body: ``{to, from, subject, body, html?, reply_to?, template_id?,
    variables?}``.

    If ``template_id`` is set, ``subject`` and ``body`` are ignored and the
    template is rendered with the supplied ``variables``. ``from`` and
    ``reply_to`` come from the request body; the operator's default
    sender address falls back to ``noreply@agentops.local`` so the dev
    flow works out of the box.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON body must be an object")
    return await _send_handler(
        request,
        to=(body.get("to") or "").strip(),
        from_addr=(body.get("from") or "").strip(),
        reply_to=(body.get("reply_to") or "").strip() or None,
        subject=(body.get("subject") or "").strip(),
        body_text=(body.get("body") or "").strip(),
        html=body.get("html"),
        template_id=(body.get("template_id") or "").strip() or None,
        variables=body.get("variables") or {},
    )


async def _send_handler(
    request: Request,
    *,
    to: str,
    from_addr: str,
    reply_to: Optional[str],
    subject: str,
    body_text: str,
    html: Optional[str],
    template_id: Optional[str],
    variables: dict,
) -> dict:
    """Shared send path. Returns the JSON response dict.

    Kept private (no ``@router`` decorator) so the route layer is the only
    place callers see a URL.
    """
    from_addr = from_addr or "noreply@agentops.local"
    if not to:
        raise HTTPException(400, "to is required")

    tid = _tenant_id(request)
    store = get_store()

    # Either render a template or use the explicit subject/body.
    template = None
    if template_id:
        template = store.get_email_template(tid, template_id)
        if not template:
            raise HTTPException(404, f"template {template_id} not found")
        # Render the templates so the row we persist (and the message the
        # provider actually sends) reflect the final values, not the
        # raw ``{{name}}`` placeholders.
        from email_providers.templates import render_template as _render
        subject = _render(template["subject_template"], variables, html_safe=False)
        body_text = _render(template["body_template"], variables, html_safe=True)
        # The caller's ``html`` may be a literal template string when
        # template_id is set; render that too if it's non-empty.
        if html:
            html = _render(html, variables, html_safe=True)
    if not subject:
        raise HTTPException(400, "subject is required (or supply template_id)")

    provider = get_provider()
    pre_row = store.insert_email_message(
        tenant_id=tid,
        provider=provider.name,
        direction="outbound",
        from_addr=from_addr,
        to_addr=to,
        subject=subject,
        body=body_text,
        html=html,
        status="queued",
    )

    try:
        if template:
            result = provider.send_template(
                to=to,
                from_addr=from_addr,
                template_id=template_id or template["id"],
                variables=variables,
                subject_template=template["subject_template"],
                body_template=template["body_template"],
                html_template=html,
                reply_to=reply_to,
            )
        else:
            result = provider.send(
                to=to,
                from_addr=from_addr,
                subject=subject,
                body=body_text,
                html=html,
                reply_to=reply_to,
            )
    except NotImplementedError as e:
        store.update_email_message_status(tid, pre_row["id"], "failed", error=str(e))
        return {"ok": False, "error": str(e), "stub": True, "id": pre_row["id"]}
    except Exception as e:
        log.exception("email send failed: %s", e)
        store.update_email_message_status(tid, pre_row["id"], "failed", error=str(e))
        return {"ok": False, "error": str(e), "id": pre_row["id"]}

    if isinstance(result, dict) and result.get("ok"):
        store.update_email_message_status(
            tid, pre_row["id"], "sent", error=None,
        )
        return {
            "ok": True,
            "id": pre_row["id"],
            "provider": provider.name,
            "message_id": result.get("message_id"),
            "stub": bool(result.get("stub")),
        }
    err = (result or {}).get("error") if isinstance(result, dict) else "unknown"
    store.update_email_message_status(tid, pre_row["id"], "failed", error=str(err))
    return {"ok": False, "error": str(err), "id": pre_row["id"]}


@router.post("/email/send-template")
async def send_template_email(request: Request) -> dict:
    """Convenience wrapper that calls :func:`_send_handler` with a
    fixed shape — the dashboard uses this when the operator picks a
    template from the picker.

    Body: ``{to, template_id, variables}``.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON body must be an object")
    if not body.get("template_id"):
        raise HTTPException(400, "template_id is required")
    return await _send_handler(
        request,
        to=body.get("to") or "",
        from_addr=body.get("from") or "",
        reply_to=body.get("reply_to"),
        subject="",  # ignored when template_id is set
        body_text="",  # ignored when template_id is set
        html=body.get("html"),
        template_id=body["template_id"],
        variables=body.get("variables") or {},
    )


# ──────────────────────────── templates (CRUD) ──────────────────────────────


@router.get("/email/templates")
def list_templates(request: Request) -> dict:
    """List the tenant's email templates (alphabetical)."""
    tid = _tenant_id(request)
    rows = get_store().list_email_templates(tid)
    return {"templates": rows, "count": len(rows)}


@router.post("/email/templates")
async def create_template(request: Request) -> dict:
    """Create a new template.

    Body: ``{name, subject_template, body_template, variables?}``.

    If ``variables`` is omitted the API scans the templates for
    ``{{name}}`` placeholders and stores that list.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON body must be an object")
    name = (body.get("name") or "").strip()
    subject_template = (body.get("subject_template") or "").strip()
    body_template = (body.get("body_template") or "").strip()
    if not name or not subject_template or not body_template:
        raise HTTPException(400, "name, subject_template, body_template are required")
    variables = body.get("variables")
    if variables is None:
        # union of the placeholders found in subject + body
        seen: list[str] = []
        for v in (extract_variables(subject_template) + extract_variables(body_template)):
            if v not in seen:
                seen.append(v)
        variables = seen
    elif not isinstance(variables, list):
        raise HTTPException(400, "variables must be a list of strings")
    tid = _tenant_id(request)
    try:
        row = get_store().create_email_template(
            tenant_id=tid,
            name=name,
            subject_template=subject_template,
            body_template=body_template,
            variables=list(variables),
        )
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"template named {name!r} already exists")
    return {"ok": True, "template": row}


@router.get("/email/templates/{template_id}")
def get_template(template_id: str, request: Request) -> dict:
    tid = _tenant_id(request)
    row = get_store().get_email_template(tid, template_id)
    if not row:
        raise HTTPException(404, f"template {template_id} not found")
    return row


@router.delete("/email/templates/{template_id}")
def delete_template(template_id: str, request: Request) -> dict:
    tid = _tenant_id(request)
    ok = get_store().delete_email_template(tid, template_id)
    if not ok:
        raise HTTPException(404, f"template {template_id} not found")
    return {"ok": True, "id": template_id}


@router.post("/email/templates/{template_id}/render")
async def render_template(template_id: str, request: Request) -> dict:
    """Render a template with the supplied variables (no send).

    Body: ``{variables: {...}}``. Returns ``{subject, body, html?}`` so
    the dashboard can preview before sending.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    variables = body.get("variables") if isinstance(body, dict) else {}
    if not isinstance(variables, dict):
        raise HTTPException(400, "variables must be an object")
    tid = _tenant_id(request)
    row = get_store().get_email_template(tid, template_id)
    if not row:
        raise HTTPException(404, f"template {template_id} not found")
    from email_providers.templates import render_template as _r
    return {
        "subject": _r(row["subject_template"], variables, html_safe=False),
        "body": _r(row["body_template"], variables, html_safe=True),
    }


# ──────────────────────────── messages log ──────────────────────────────────


@router.get("/email/messages")
def list_messages(
    request: Request,
    to: Optional[str] = None,
    from_addr: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Recent email_messages rows for the tenant, newest first."""
    if limit < 1 or limit > 200:
        raise HTTPException(400, "limit must be 1..200")
    tid = _tenant_id(request)
    rows = get_store().list_email_messages(
        tid,
        to_addr=to,
        from_addr=from_addr,
        limit=limit,
        offset=offset,
    )
    return {"messages": rows, "count": len(rows)}


@router.get("/email/messages/{email_id}")
def get_message(email_id: str, request: Request) -> dict:
    tid = _tenant_id(request)
    row = get_store().get_email_message(tid, email_id)
    if not row:
        raise HTTPException(404, f"email {email_id} not found")
    return row


# ──────────────────────────── inbound webhooks ──────────────────────────────


@router.post("/webhooks/email/inbound-test")
async def inbound_test(request: Request) -> dict:
    """Dev/test route: synthesise an inbound message without a real
    provider. The UI uses this to push a test email into the thread
    list while the production adapters are still stubs.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON body must be an object")
    from_addr = (body.get("from_addr") or body.get("from") or "").strip()
    to_addr = (body.get("to_addr") or body.get("to") or "").strip()
    if not from_addr or not to_addr:
        raise HTTPException(400, "from_addr and to_addr are required")
    tid = _tenant_id(request)
    store = get_store()
    row = store.insert_email_message(
        tenant_id=tid,
        provider="dev",
        direction="inbound",
        from_addr=from_addr,
        to_addr=to_addr,
        subject=body.get("subject") or "(no subject)",
        body=body.get("body") or body.get("text") or "",
        html=body.get("html"),
        status="received",
        sent_at=_now(),
    )
    return {"ok": True, "id": row["id"]}


@router.post("/webhooks/email/{provider}")
async def inbound_email(provider: str, request: Request) -> dict:
    """Receive an inbound email from ``provider`` (the path segment).

    The body is opaque provider JSON; the provider's
    :meth:`handle_inbound` is responsible for normalising it.

    After normalisation we:
    1. Look up the contact by ``from_addr`` (case-insensitive, ignoring
       ``+displayname``).
    2. Insert a row in ``email_messages`` with ``direction='inbound'`` and
       ``status='received'``.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON body must be an object")

    try:
        adapter = get_provider(provider)
    except ValueError:
        raise HTTPException(404, f"unknown email provider: {provider!r}")
    try:
        parsed = adapter.handle_inbound(body)
    except NotImplementedError as e:
        # The chosen provider doesn't have an inbound parser yet.
        return {"ok": False, "error": str(e), "stub": True}
    except ValueError as e:
        raise HTTPException(400, str(e))

    # ── find a contact for the sender (best-effort). The store has a list
    # of tenants; we run the lookup against the same tenant as the
    # provider (if the webhook was authenticated) or fall back to the
    # request's tenant context.
    tid = _tenant_id(request)
    store = get_store()
    from_addr = (parsed.get("from_addr") or "").strip()
    to_addr = (parsed.get("to_addr") or "").strip()
    subject = parsed.get("subject") or ""
    body_text = parsed.get("body") or ""
    body_html = parsed.get("html")

    # best-effort contact match — the table doesn't enforce uniqueness on
    # email so we just take the first match.
    contact_id: Optional[str] = None
    if from_addr:
        try:
            contact = store._row(  # type: ignore[attr-defined]
                "SELECT id FROM contacts WHERE tenant_id = ? "
                "AND LOWER(email) = LOWER(?) LIMIT 1",
                (tid, from_addr),
            )
            if contact:
                contact_id = contact["id"]
        except Exception:
            contact_id = None

    row = store.insert_email_message(
        tenant_id=tid,
        provider=adapter.name,
        direction="inbound",
        from_addr=from_addr,
        to_addr=to_addr,
        subject=subject,
        body=body_text,
        html=body_html,
        status="received",
        sent_at=_now(),
    )
    return {
        "ok": True,
        "id": row["id"],
        "contact_id": contact_id,
        "matched_contact": bool(contact_id),
        "stub": bool(parsed.get("stub")),
    }
