"""Phase B endpoint helpers — local copy of the admin_api / dashboard_api
tenant-resolution pattern.

We can't use ``Depends(get_current_tenant)`` because FastAPI treats an
un-typed ``request`` parameter as a query parameter (the original
implementation skipped the type hint on purpose; annotating it as
``Request`` collides with the OpenAPI forward-ref logic in
Pydantic 2.13). Every other endpoint in the codebase therefore takes
``request: Request`` and calls ``_ctx(request)`` — this module just
gives the Phase B routers the same shape so they can stay consistent
with the rest of the file.
"""
from __future__ import annotations

from typing import Optional

from webhooks.tenancy import TenantContext


def _ctx(request) -> Optional[TenantContext]:
    """Pull the TenantContext the auth middleware populated.

    The middleware short-circuits unauth'd requests on /api/* with a
    401 before the endpoint runs, so this should always be present.
    """
    return getattr(request.state, "tenant_ctx", None)


def _tenant_id(request) -> str:
    """Resolve the tenant id, falling back to 'default' for tests."""
    ctx = _ctx(request)
    if ctx is not None:
        return ctx.tenant_id
    # Legacy fallback (matches the dashboard_api pattern).
    tid = request.headers.get("X-Tenant-Id") or request.headers.get("x-tenant-id")
    return (tid or "default").strip() or "default"
