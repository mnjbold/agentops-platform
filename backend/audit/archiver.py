"""Audit log archiver — move old rows into audit_log_archive (issue #20).

Retention target: 7 years. Run nightly via the cron hook in server.py.
The active ``audit_log`` table stays small; the archive can grow as
long as needed.

``zstd`` is used if installed; otherwise we move plain. Either way the
output is byte-stable: re-archiving the same row doesn't change the
archive contents.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from webhooks.storage import get_store

log = logging.getLogger(__name__)


def archive_audit(retention_days: int = 365 * 7) -> int:
    """Archive everything older than ``retention_days``. Returns the
    number of rows moved. Safe to call on an empty table (returns 0)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    store = get_store()
    n = store.archive_audit_before(cutoff)
    if n:
        log.info("audit archive: moved %d rows older than %s", n, cutoff)
    return n
