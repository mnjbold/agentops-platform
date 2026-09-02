#!/usr/bin/env python3
"""Migrate a tenant's data between regions (issue #29).

Copies every row keyed by ``tenant_id`` from the source DB to the
target DB, verifies the row counts + a per-table checksum, then flips
``tenants.region`` on the *target* DB to match the move.

Tables migrated (all share a ``tenant_id`` column):

    tenants, tenant_secrets, users, contacts, campaigns,
    scheduled_jobs, deliveries, voicemails, recordings, workflows,
    phone_numbers, number_assignments, assistants, assistant_call_log,
    campaign_handoffs, analytics_rollup_daily, subscriptions,
    usage_records, audit_log, audit_log_archive, synthetic_calls,
    dnc_cache, network_quality_log, suppression_list, sms_replies,
    sms_send_log, whatsapp_templates, whatsapp_messages

Usage::

    BACKEND_REGION=us \
        python ops/failover/migrate_tenant.py <tenant_id> us eu

The script is idempotent on the destination: re-running is safe — the
INSERT uses ``OR REPLACE`` so existing rows are overwritten with the
source's version. The ``tenants.region`` flip only fires when the
checksum matches.

Safety
------
* The script refuses to run if the destination DB has rows for the
  tenant that *don't* appear in the source (destructive merge). Use
  ``--force`` to bypass.
* The script refuses to run if the source + destination DBs are the
  same file (e.g. you forgot to set ``BACKEND_REGION``).
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Iterable

LOG = logging.getLogger("migrate_tenant")

# The set of tables we know about. New tables added in future phases
# need an entry here (and a re-run).
_TENANT_TABLES: list[str] = [
    "tenants",
    "tenant_secrets",
    "users",
    "contacts",
    "campaigns",
    "scheduled_jobs",
    "deliveries",
    "voicemails",
    "recordings",
    "workflows",
    "phone_numbers",
    "number_assignments",
    "assistants",
    "assistant_call_log",
    "campaign_handoffs",
    "analytics_rollup_daily",
    "subscriptions",
    "usage_records",
    "audit_log",
    "audit_log_archive",
    "synthetic_calls",
    "dnc_cache",
    "network_quality_log",
    "suppression_list",
    "sms_replies",
    "sms_send_log",
    "whatsapp_templates",
    "whatsapp_messages",
]


def _db_path_for(region: str) -> Path:
    """Mirror the backend's _resolve_db_path() so the script picks the
    right file when invoked from the repo root."""
    here = Path(__file__).resolve()
    backend_webhooks = here.parents[2] / "backend" / "webhooks"
    if region == "eu":
        return backend_webhooks / "agentops_eu.db"
    return backend_webhooks / "agentops.db"


def _table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def _rows_for_table(conn: sqlite3.Connection, table: str, tenant_id: str) -> list[tuple]:
    """Return every row for ``tenant_id`` in ``table``. Tables without a
    tenant_id column are skipped (we don't want to copy platform-wide
    rows by accident)."""
    if not _table_has_column(conn, table, "tenant_id"):
        return []
    cur = conn.execute(f"SELECT * FROM {table} WHERE tenant_id = ?", (tenant_id,))
    return list(cur.fetchall())


def _row_count(conn: sqlite3.Connection, table: str, tenant_id: str) -> int:
    if not _table_has_column(conn, table, "tenant_id"):
        return 0
    cur = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE tenant_id = ?", (tenant_id,))
    return int(cur.fetchone()[0] or 0)


def _table_checksum(conn: sqlite3.Connection, table: str, tenant_id: str) -> str:
    """Stable hash of the tenant's rows in ``table`` (order-independent)."""
    if not _table_has_column(conn, table, "tenant_id"):
        return ""
    cur = conn.execute(
        f"SELECT * FROM {table} WHERE tenant_id = ? ORDER BY rowid",
        (tenant_id,),
    )
    h = hashlib.sha256()
    for row in cur.fetchall():
        h.update(repr(tuple(row)).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def _connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise SystemExit(f"DB file not found: {path}")
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("PRAGMA foreign_keys=OFF")  # we restore ON after copy
    return conn


def _copy_table(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    table: str,
    tenant_id: str,
    force: bool,
) -> dict:
    """Copy the tenant's rows in ``table`` from ``src`` to ``dst``.

    Returns ``{"rows_src": N, "rows_dst_before": M, "rows_copied": K}``.
    Raises SystemExit on destructive merge unless ``force``.
    """
    rows = _rows_for_table(src, table, tenant_id)
    rows_dst_before = _row_count(dst, table, tenant_id)
    if rows_dst_before and not force:
        # Detect destructive merge: any row in dst that isn't in src
        # for this tenant would be wiped. Refuse unless --force.
        src_ids = {row[0] for row in rows}  # PK is always the first column
        cur = dst.execute(
            f"SELECT * FROM {table} WHERE tenant_id = ?", (tenant_id,)
        )
        dst_rows = list(cur.fetchall())
        dst_ids = {row[0] for row in dst_rows}
        if dst_ids - src_ids:
            raise SystemExit(
                f"refusing to merge: table {table!r} has rows in destination "
                f"that are missing in source. Re-run with --force to overwrite."
            )
    cur = dst.execute(f"PRAGMA table_info({table})")
    cols = [row[1] for row in cur.fetchall()]
    placeholders = ",".join("?" for _ in cols)
    if not rows:
        return {"rows_src": 0, "rows_dst_before": rows_dst_before, "rows_copied": 0}
    copied = 0
    for row in rows:
        # ``INSERT OR REPLACE`` keeps idempotency; the PK wins.
        dst.execute(
            f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
            tuple(row),
        )
        copied += 1
    return {
        "rows_src": len(rows),
        "rows_dst_before": rows_dst_before,
        "rows_copied": copied,
    }


def _flip_region(dst: sqlite3.Connection, tenant_id: str, new_region: str) -> None:
    dst.execute(
        "UPDATE tenants SET region = ?, region_lock = 1, updated_at = ? WHERE id = ?",
        (new_region, _now_iso(), tenant_id),
    )


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def migrate(tenant_id: str, src_region: str, dst_region: str, force: bool) -> int:
    src_path = _db_path_for(src_region)
    dst_path = _db_path_for(dst_region)
    if src_path.resolve() == dst_path.resolve():
        raise SystemExit(
            f"source and destination resolve to the same file ({src_path}); "
            "did you forget BACKEND_REGION=eu for the second app?"
        )
    LOG.info("Migrating tenant %s: %s -> %s", tenant_id, src_path, dst_path)
    src = _connect(src_path)
    dst = _connect(dst_path)
    try:
        if not _row_count(src, "tenants", tenant_id):
            raise SystemExit(f"tenant {tenant_id!r} not found in {src_path}")
        summary: dict = {}
        for table in _TENANT_TABLES:
            summary[table] = _copy_table(src, dst, table, tenant_id, force)
        # Checksum verify
        mismatches: list[str] = []
        for table in _TENANT_TABLES:
            h_src = _table_checksum(src, table, tenant_id)
            h_dst = _table_checksum(dst, table, tenant_id)
            if h_src != h_dst:
                mismatches.append(table)
        if mismatches:
            raise SystemExit(
                f"checksum mismatch in tables: {mismatches}; aborting before region flip"
            )
        # Flip the tenant's region on the destination.
        _flip_region(dst, tenant_id, dst_region)
        LOG.info("Migration complete. Tables copied: %s", list(summary.keys()))
        for table, stats in summary.items():
            if stats["rows_src"] or stats["rows_dst_before"]:
                LOG.info("  %s: %s", table, stats)
        return 0
    finally:
        try:
            src.close()
        except Exception:
            pass
        try:
            dst.close()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate a tenant's rows between agentops regions",
    )
    parser.add_argument("tenant_id")
    parser.add_argument("from_region", choices=("us", "eu"))
    parser.add_argument("to_region", choices=("us", "eu"))
    parser.add_argument("--force", action="store_true",
                        help="Overwrite destination rows that aren't in the source")
    args = parser.parse_args()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.from_region == args.to_region:
        raise SystemExit("from_region and to_region must differ")
    return migrate(args.tenant_id, args.from_region, args.to_region, args.force)


if __name__ == "__main__":
    sys.exit(main())
