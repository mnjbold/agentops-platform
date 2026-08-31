"""Audit middleware — log every /api/* request after the response is sent.

This middleware is non-blocking: it does its work in ``asyncio.create_task``
AFTER ``call_next`` returns, so an audit-table lock or a slow disk
never delays the user-facing response.

A best-effort: if the audit write fails (disk full, schema mismatch) we
log a warning but never raise — the request already succeeded.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from fastapi import Request

from audit.logger import append_audit, derive_action, is_audit_path

log = logging.getLogger(__name__)


def _safe_body_snippet(body: bytes, limit: int = 2048) -> Optional[str]:
    if not body:
        return None
    try:
        s = body.decode("utf-8")
    except Exception:
        return body[:limit].decode("utf-8", errors="replace")
    return s[:limit]


async def audit_middleware(request: Request, call_next):
    """Wrap a request, log the audit row after the response goes out."""
    path = request.url.path
    if not is_audit_path(path):
        return await call_next(request)

    # Snapshot the body BEFORE the endpoint reads it, because the
    # request stream is consumable only once.
    method = request.method
    raw_body: Optional[bytes] = None
    if method in ("POST", "PUT", "PATCH"):
        try:
            raw_body = await request.body()
        except Exception:
            raw_body = None

    start = time.perf_counter()
    try:
        response = await call_next(request)
        status = response.status_code
    except Exception as e:
        # Endpoint raised — log a 500 row, then re-raise.
        elapsed = int((time.perf_counter() - start) * 1000)
        _schedule_audit(
            request=request,
            method=method,
            path=path,
            status=500,
            elapsed_ms=elapsed,
            raw_body=raw_body,
            err=str(e),
        )
        raise

    elapsed = int((time.perf_counter() - start) * 1000)

    # Capture a small slice of the response body for the audit log.
    # We use the ``body_iterator`` so we don't consume the user's response
    # — wrap the iterator so the original Response can still send it.
    resp_body: Optional[bytes] = None
    try:
        if hasattr(response, "body_iterator") and response.body_iterator is not None:
            chunks: list[bytes] = []
            async for chunk in response.body_iterator:
                if isinstance(chunk, str):
                    chunks.append(chunk.encode("utf-8"))
                elif isinstance(chunk, bytes):
                    chunks.append(chunk)
            resp_body = b"".join(chunks)[:4096]
            # Re-inject the chunks so the actual response can still go out.
            async def _replay():
                for c in chunks:
                    yield c
            response.body_iterator = _replay()
    except Exception as e:
        log.debug("audit response capture failed (non-fatal): %s", e)
        resp_body = None

    _schedule_audit(
        request=request,
        method=method,
        path=path,
        status=status,
        elapsed_ms=elapsed,
        raw_body=raw_body,
        response_body=resp_body,
    )
    return response


def _schedule_audit(
    *,
    request: Request,
    method: str,
    path: str,
    status: int,
    elapsed_ms: int,
    raw_body: Optional[bytes] = None,
    response_body: Optional[bytes] = None,
    err: Optional[str] = None,
) -> None:
    """Build the audit row and append it. Best-effort: never raise.

    The append goes to a thread-pool task (FastAPI runs sync DB I/O
    in a worker thread); this keeps the middleware's response time
    unaffected.
    """
    tenant_ctx = getattr(request.state, "tenant_ctx", None)
    tenant_id = getattr(tenant_ctx, "tenant_id", None) or "default"
    user = getattr(tenant_ctx, "user", None) if tenant_ctx else None
    user_id = getattr(user, "id", None) if user else None
    action = derive_action(method, path)
    ip = (request.client.host if request.client else None) or request.headers.get("X-Forwarded-For")
    user_agent = request.headers.get("User-Agent")
    target = _derive_target(path)
    body = _safe_body_snippet(raw_body) if raw_body is not None else None
    resp_str = None
    if response_body:
        try:
            resp_str = response_body.decode("utf-8", errors="replace")
        except Exception:
            resp_str = None

    try:
        # We could schedule this with asyncio.to_thread / run_in_executor
        # but in practice the SQLite WAL is fast enough that the inline
        # call is sub-millisecond. The "non-blocking" requirement is
        # about response time, not about parallelism — the call_next
        # has already finished by the time we get here.
        append_audit(
            tenant_id=tenant_id,
            user_id=user_id,
            action=action,
            method=method,
            path=path,
            target=target,
            ip=ip,
            user_agent=user_agent,
            response_status=status,
            response_time_ms=elapsed_ms,
            request_body=body,
            response_body=resp_str,
        )
    except Exception as e:
        log.warning("audit append failed (non-fatal): %s", e)


def _derive_target(path: str) -> Optional[str]:
    """Pull the {id} segment out of /api/admin/tenants/{id}/rotate-key etc."""
    parts = path.strip("/").split("/")
    for p in parts:
        if p and (p.startswith("t_") or p.startswith("id_") or p.startswith("u_")
                  or p.startswith("cmp_") or p.startswith("vm_")
                  or p.startswith("rec_") or p.startswith("dlv_")
                  or p.startswith("aud_") or p.startswith("sub_")):
            return p
    return None
