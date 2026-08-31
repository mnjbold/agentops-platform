"""Audit log package (issue #20).

- ``middleware`` — the FastAPI middleware that logs every /api/* request
- ``logger``    — pure helper: build the audit row dict + append
- ``archiver``  — nightly cron: move old rows into audit_log_archive

The FastAPI router lives in ``webhooks.audit``.
"""
from __future__ import annotations

from audit.logger import (  # noqa: F401
    append_audit,
    derive_action,
    is_audit_path,
)
from audit.archiver import archive_audit  # noqa: F401
