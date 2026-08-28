"""SQLite-backed store for multi-tenant SaaS primitives.

Tables
------
- tenants(id, name, created_at)
- contacts(id, tenant_id, name, phone, email, tags JSON, created_at)
- campaigns(id, tenant_id, name, type, from_number, message, contact_ids JSON,
           schedule_at, status, created_at, updated_at, started_at,
           completed_at, stats_json)
- scheduled_jobs(id, tenant_id, kind, payload_json, run_at, status,
                 created_at, last_error)
- deliveries(id, tenant_id, kind, contact_id, target, payload_summary,
             telnyx_id, status, error, created_at) — append-only

Design notes
------------
- One file, ``webhooks/agentops.db``, lives next to this script by default.
- Single ``sqlite3`` connection with ``check_same_thread=False`` so the
  FastAPI request thread and the scheduler daemon thread can both use it.
- A ``threading.Lock`` serialises *writes*; reads are lockless because
  sqlite3 in WAL/read-uncommitted mode is safe for concurrent readers
  (and we use the default isolation level — fully serialised writes
  inside the lock is enough for the v0.1 throughput target).
- All methods take an explicit ``tenant_id`` so the router layer is
  responsible for scoping. No implicit "current tenant" globals.
- JSON columns are stored as TEXT and serialised/deserialised here so
  callers see plain Python objects.
"""
from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger(__name__)

# Path of the SQLite file (next to this script by default)
_HERE = Path(__file__).resolve().parent
DEFAULT_DB_PATH = _HERE / "agentops.db"

# ---------------------------------------------------------------------------
# Schema — kept as a single string so init() is idempotent (CREATE IF NOT EXISTS).
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contacts (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    name        TEXT NOT NULL,
    phone       TEXT NOT NULL,
    email       TEXT,
    tags        TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);
CREATE INDEX IF NOT EXISTS idx_contacts_tenant ON contacts(tenant_id);

CREATE TABLE IF NOT EXISTS campaigns (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    name          TEXT NOT NULL,
    type          TEXT NOT NULL CHECK(type IN ('sms','call')),
    from_number   TEXT,
    message       TEXT,
    contact_ids   TEXT NOT NULL DEFAULT '[]',
    schedule_at   TEXT,
    status        TEXT NOT NULL CHECK(status IN
        ('draft','scheduled','running','paused','completed','failed')),
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    started_at    TEXT,
    completed_at  TEXT,
    stats_json    TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);
CREATE INDEX IF NOT EXISTS idx_campaigns_tenant ON campaigns(tenant_id);
CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status);

CREATE TABLE IF NOT EXISTS scheduled_jobs (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK(kind IN
        ('sms','campaign_sms','campaign_call','power_dialer_step')),
    payload_json  TEXT NOT NULL,
    run_at        TEXT NOT NULL,
    status        TEXT NOT NULL CHECK(status IN
        ('pending','running','done','failed','cancelled')),
    created_at    TEXT NOT NULL,
    last_error    TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_due ON scheduled_jobs(status, run_at);

CREATE TABLE IF NOT EXISTS deliveries (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    kind            TEXT NOT NULL,
    contact_id      TEXT,
    target          TEXT NOT NULL,
    payload_summary TEXT,
    telnyx_id       TEXT,
    status          TEXT NOT NULL,
    error           TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deliveries_tenant ON deliveries(tenant_id);
CREATE INDEX IF NOT EXISTS idx_deliveries_contact ON deliveries(contact_id);
CREATE INDEX IF NOT EXISTS idx_deliveries_campaign ON deliveries(kind, target);
"""


def _utcnow() -> str:
    """ISO-8601 UTC timestamp string."""
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str = "id") -> str:
    return f"{prefix}_{secrets.token_urlsafe(9)}"


class Store:
    """Thread-safe SQLite wrapper. Single shared connection."""

    def __init__(self, path: Path | str = DEFAULT_DB_PATH) -> None:
        self.path = Path(path)
        # RLock (reentrant) so helpers like ``ensure_tenant`` can be called
        # from inside a write method that already holds the lock without
        # deadlocking. The FastAPI request thread and the scheduler daemon
        # thread share this single connection; the lock makes writes
        # serialised while reads stay lockless.
        self._lock = threading.RLock()
        # check_same_thread=False so the FastAPI workers and the scheduler
        # thread can both share this connection safely under the lock.
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None
        )
        # WAL gives better concurrent-read behaviour and reduces the chance
        # of the reader seeing "database is locked" while the scheduler writes.
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        except sqlite3.DatabaseError as e:
            log.warning("PRAGMA setup failed: %s", e)

    # ─────────────────────────── init / teardown ───────────────────────────
    def init(self) -> None:
        """Create tables and seed the default tenant. Idempotent."""
        with self._lock:
            self._conn.executescript(_SCHEMA)
            cur = self._conn.execute(
                "SELECT id FROM tenants WHERE id = 'default'"
            )
            if cur.fetchone() is None:
                self._conn.execute(
                    "INSERT INTO tenants(id, name, created_at) VALUES (?, ?, ?)",
                    ("default", "Default Tenant", _utcnow()),
                )
                log.info("Seeded default tenant")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ──────────────────────────── helpers ──────────────────────────────────
    def _row(self, sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
        cur = self._conn.execute(sql, tuple(params))
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def _rows(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        cur = self._conn.execute(sql, tuple(params))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> None:
        with self._lock:
            self._conn.execute(sql, tuple(params))

    # ──────────────────────────── contacts ────────────────────────────────
    def ensure_tenant(self, tenant_id: str, name: Optional[str] = None) -> None:
        """Create the tenant row if it doesn't exist. Idempotent.

        The first contact / campaign write for a new tenant implicitly
        bootstraps the tenant row. v0.2 will move tenant creation behind
        a real auth + signup flow.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM tenants WHERE id = ?", (tenant_id,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO tenants(id, name, created_at) VALUES (?, ?, ?)",
                    (tenant_id, name or tenant_id, _utcnow()),
                )
                log.info("Auto-created tenant: %s", tenant_id)

    def list_contacts(self, tenant_id: str) -> list[dict]:
        rows = self._rows(
            "SELECT * FROM contacts WHERE tenant_id = ? "
            "ORDER BY created_at DESC",
            (tenant_id,),
        )
        for r in rows:
            try:
                r["tags"] = json.loads(r.get("tags") or "[]")
            except json.JSONDecodeError:
                r["tags"] = []
        return rows

    def get_contact(self, tenant_id: str, contact_id: str) -> Optional[dict]:
        row = self._row(
            "SELECT * FROM contacts WHERE tenant_id = ? AND id = ?",
            (tenant_id, contact_id),
        )
        if row:
            try:
                row["tags"] = json.loads(row.get("tags") or "[]")
            except json.JSONDecodeError:
                row["tags"] = []
        return row

    def create_contact(
        self,
        tenant_id: str,
        name: str,
        phone: str,
        email: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> dict:
        cid = _new_id("ct")
        tags_json = json.dumps(tags or [])
        with self._lock:
            self.ensure_tenant(tenant_id)
            self._conn.execute(
                "INSERT INTO contacts(id, tenant_id, name, phone, email, tags, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (cid, tenant_id, name, phone, email, tags_json, _utcnow()),
            )
        contact = self.get_contact(tenant_id, cid)
        assert contact is not None
        return contact

    def update_contact(
        self,
        tenant_id: str,
        contact_id: str,
        name: Optional[str] = None,
        phone: Optional[str] = None,
        email: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> Optional[dict]:
        existing = self.get_contact(tenant_id, contact_id)
        if not existing:
            return None
        sets: list[str] = []
        vals: list[Any] = []
        if name is not None:
            sets.append("name = ?")
            vals.append(name)
        if phone is not None:
            sets.append("phone = ?")
            vals.append(phone)
        if email is not None:
            sets.append("email = ?")
            vals.append(email)
        if tags is not None:
            sets.append("tags = ?")
            vals.append(json.dumps(tags))
        if not sets:
            return existing
        vals.extend([tenant_id, contact_id])
        with self._lock:
            self._conn.execute(
                f"UPDATE contacts SET {', '.join(sets)} "
                "WHERE tenant_id = ? AND id = ?",
                tuple(vals),
            )
        return self.get_contact(tenant_id, contact_id)

    def delete_contact(self, tenant_id: str, contact_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM contacts WHERE tenant_id = ? AND id = ?",
                (tenant_id, contact_id),
            )
            return cur.rowcount > 0

    # ──────────────────────────── campaigns ───────────────────────────────
    def list_campaigns(self, tenant_id: str) -> list[dict]:
        rows = self._rows(
            "SELECT * FROM campaigns WHERE tenant_id = ? "
            "ORDER BY created_at DESC",
            (tenant_id,),
        )
        for r in rows:
            r["contact_ids"] = _safe_json_list(r.get("contact_ids"))
            r["stats"] = _safe_json_dict(r.get("stats_json"))
        return rows

    def get_campaign(self, tenant_id: str, campaign_id: str) -> Optional[dict]:
        row = self._row(
            "SELECT * FROM campaigns WHERE tenant_id = ? AND id = ?",
            (tenant_id, campaign_id),
        )
        if not row:
            return None
        row["contact_ids"] = _safe_json_list(row.get("contact_ids"))
        row["stats"] = _safe_json_dict(row.get("stats_json"))
        return row

    def create_campaign(
        self,
        tenant_id: str,
        name: str,
        type_: str,
        from_number: Optional[str] = None,
        message: Optional[str] = None,
        contact_ids: Optional[list[str]] = None,
        schedule_at: Optional[str] = None,
    ) -> dict:
        if type_ not in ("sms", "call"):
            raise ValueError(f"invalid campaign type: {type_}")
        cid = _new_id("cmp")
        now = _utcnow()
        contact_ids_json = json.dumps(contact_ids or [])
        # Decide initial status: scheduled if run_at is in the future.
        status = "draft"
        if schedule_at:
            try:
                run_dt = _parse_iso(schedule_at)
                if run_dt and run_dt > datetime.now(timezone.utc):
                    status = "scheduled"
            except Exception:
                pass
        with self._lock:
            self.ensure_tenant(tenant_id)
            self._conn.execute(
                "INSERT INTO campaigns("
                "id, tenant_id, name, type, from_number, message, "
                "contact_ids, schedule_at, status, created_at, updated_at, "
                "started_at, completed_at, stats_json"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    cid, tenant_id, name, type_, from_number, message,
                    contact_ids_json, schedule_at, status, now, now,
                    None, None, "{}",
                ),
            )
        campaign = self.get_campaign(tenant_id, cid)
        assert campaign is not None
        return campaign

    def update_campaign(
        self,
        tenant_id: str,
        campaign_id: str,
        **fields: Any,
    ) -> Optional[dict]:
        existing = self.get_campaign(tenant_id, campaign_id)
        if not existing:
            return None
        allowed = {"name", "from_number", "message", "contact_ids",
                   "schedule_at", "status"}
        sets: list[str] = []
        vals: list[Any] = []
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            if k == "contact_ids":
                sets.append("contact_ids = ?")
                vals.append(json.dumps(v))
            else:
                sets.append(f"{k} = ?")
                vals.append(v)
        if not sets:
            return existing
        sets.append("updated_at = ?")
        vals.append(_utcnow())
        vals.extend([tenant_id, campaign_id])
        with self._lock:
            self._conn.execute(
                f"UPDATE campaigns SET {', '.join(sets)} "
                "WHERE tenant_id = ? AND id = ?",
                tuple(vals),
            )
        return self.get_campaign(tenant_id, campaign_id)

    def set_campaign_status(
        self,
        tenant_id: str,
        campaign_id: str,
        status: str,
        *,
        started_at: Optional[str] = None,
        completed_at: Optional[str] = None,
    ) -> None:
        if status not in ("draft", "scheduled", "running", "paused",
                          "completed", "failed"):
            raise ValueError(f"invalid status: {status}")
        sets = ["status = ?", "updated_at = ?"]
        vals: list[Any] = [status, _utcnow()]
        if started_at is not None:
            sets.append("started_at = ?")
            vals.append(started_at)
        if completed_at is not None:
            sets.append("completed_at = ?")
            vals.append(completed_at)
        vals.extend([tenant_id, campaign_id])
        with self._lock:
            self._conn.execute(
                f"UPDATE campaigns SET {', '.join(sets)} "
                "WHERE tenant_id = ? AND id = ?",
                tuple(vals),
            )

    def campaign_stats_bump(
        self, tenant_id: str, campaign_id: str, key: str, delta: int = 1
    ) -> None:
        """Increment a stats field on a campaign by ``delta`` (default +1)."""
        if key not in ("sent", "delivered", "failed", "answered",
                       "voicemail", "no_answer"):
            raise ValueError(f"invalid stat key: {key}")
        # SQLite JSON1 patch: read-modify-write inside the lock.
        with self._lock:
            row = self._conn.execute(
                "SELECT stats_json FROM campaigns "
                "WHERE tenant_id = ? AND id = ?",
                (tenant_id, campaign_id),
            ).fetchone()
            if row is None:
                return
            try:
                stats = json.loads(row[0] or "{}")
            except json.JSONDecodeError:
                stats = {}
            stats[key] = int(stats.get(key, 0)) + delta
            self._conn.execute(
                "UPDATE campaigns SET stats_json = ? "
                "WHERE tenant_id = ? AND id = ?",
                (json.dumps(stats), tenant_id, campaign_id),
            )

    def delete_campaign(self, tenant_id: str, campaign_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM campaigns WHERE tenant_id = ? AND id = ?",
                (tenant_id, campaign_id),
            )
            return cur.rowcount > 0

    # ──────────────────────── scheduled jobs ──────────────────────────────
    def enqueue_job(
        self,
        tenant_id: str,
        kind: str,
        payload: dict,
        run_at: str,
    ) -> dict:
        if kind not in ("sms", "campaign_sms", "campaign_call",
                        "power_dialer_step"):
            raise ValueError(f"invalid job kind: {kind}")
        jid = _new_id("job")
        with self._lock:
            self._conn.execute(
                "INSERT INTO scheduled_jobs("
                "id, tenant_id, kind, payload_json, run_at, status, created_at"
                ") VALUES (?,?,?,?,?,?,?)",
                (jid, tenant_id, kind, json.dumps(payload), run_at,
                 "pending", _utcnow()),
            )
        return {"id": jid, "tenant_id": tenant_id, "kind": kind,
                "payload": payload, "run_at": run_at, "status": "pending"}

    def list_jobs(self, tenant_id: str, status: Optional[str] = None) -> list[dict]:
        if status:
            rows = self._rows(
                "SELECT * FROM scheduled_jobs "
                "WHERE tenant_id = ? AND status = ? "
                "ORDER BY run_at ASC",
                (tenant_id, status),
            )
        else:
            rows = self._rows(
                "SELECT * FROM scheduled_jobs WHERE tenant_id = ? "
                "ORDER BY run_at ASC",
                (tenant_id,),
            )
        for r in rows:
            try:
                r["payload"] = json.loads(r.get("payload_json") or "{}")
            except json.JSONDecodeError:
                r["payload"] = {}
        return rows

    def claim_due_jobs(self, now_iso: str) -> list[dict]:
        """Atomically mark all pending jobs whose ``run_at <= now`` as running
        and return them so the scheduler can dispatch.

        The claim+return is done inside the lock so two scheduler ticks
        can't both grab the same job.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, tenant_id, kind, payload_json, run_at, created_at "
                "FROM scheduled_jobs "
                "WHERE status = 'pending' AND run_at <= ? "
                "ORDER BY run_at ASC LIMIT 200",
                (now_iso,),
            ).fetchall()
            if not rows:
                return []
            ids = [r[0] for r in rows]
            qmarks = ",".join("?" for _ in ids)
            self._conn.execute(
                f"UPDATE scheduled_jobs SET status = 'running' "
                f"WHERE id IN ({qmarks})",
                tuple(ids),
            )
        out: list[dict] = []
        for r in rows:
            try:
                payload = json.loads(r[3] or "{}")
            except json.JSONDecodeError:
                payload = {}
            out.append({
                "id": r[0],
                "tenant_id": r[1],
                "kind": r[2],
                "payload": payload,
                "run_at": r[4],
                "created_at": r[5],
            })
        return out

    def mark_job_done(self, job_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE scheduled_jobs SET status = 'done' WHERE id = ?",
                (job_id,),
            )

    def mark_job_failed(self, job_id: str, err: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE scheduled_jobs SET status = 'failed', last_error = ? "
                "WHERE id = ?",
                (str(err)[:500], job_id),
            )

    def cancel_job(self, tenant_id: str, job_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE scheduled_jobs SET status = 'cancelled' "
                "WHERE tenant_id = ? AND id = ? AND status = 'pending'",
                (tenant_id, job_id),
            )
            return cur.rowcount > 0

    # ────────────────────────── deliveries ────────────────────────────────
    def record_delivery(
        self,
        tenant_id: str,
        kind: str,
        target: str,
        status: str,
        *,
        contact_id: Optional[str] = None,
        payload_summary: Optional[str] = None,
        telnyx_id: Optional[str] = None,
        error: Optional[str] = None,
    ) -> dict:
        did = _new_id("dlv")
        with self._lock:
            self._conn.execute(
                "INSERT INTO deliveries("
                "id, tenant_id, kind, contact_id, target, payload_summary, "
                "telnyx_id, status, error, created_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?)",
                (did, tenant_id, kind, contact_id, target,
                 payload_summary, telnyx_id, status, error, _utcnow()),
            )
        return {"id": did, "tenant_id": tenant_id, "kind": kind,
                "contact_id": contact_id, "target": target, "status": status,
                "telnyx_id": telnyx_id, "error": error}

    def list_deliveries(
        self,
        tenant_id: str,
        contact_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        if contact_id:
            rows = self._rows(
                "SELECT * FROM deliveries "
                "WHERE tenant_id = ? AND contact_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (tenant_id, contact_id, int(limit)),
            )
        else:
            rows = self._rows(
                "SELECT * FROM deliveries WHERE tenant_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (tenant_id, int(limit)),
            )
        return rows

    def list_deliveries_for_campaign(
        self, tenant_id: str, campaign_id: str
    ) -> list[dict]:
        """Deliveries whose ``payload_summary`` contains the campaign id.

        Cheap string match — the campaign id is unique and only written
        by the scheduler itself, so this is safe and avoids an extra
        ``deliveries`` column for a v0.1 store.
        """
        marker = f"campaign={campaign_id}"
        rows = self._rows(
            "SELECT * FROM deliveries "
            "WHERE tenant_id = ? AND payload_summary LIKE ? "
            "ORDER BY created_at DESC",
            (tenant_id, f"%{marker}%"),
        )
        return rows


# ---------------------------------------------------------------------------
# Module-level singleton + helpers
# ---------------------------------------------------------------------------
_store: Optional[Store] = None
_store_lock = threading.Lock()


def get_store() -> Store:
    """Lazy singleton, initialised on first access."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = Store(DEFAULT_DB_PATH)
                _store.init()
    return _store


def _safe_json_list(raw: Any) -> list:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except json.JSONDecodeError:
        return []


def _safe_json_dict(raw: Any) -> dict:
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        return {}


def _parse_iso(s: str) -> Optional[datetime]:
    """Parse a Telnyx/ISO-8601 timestamp string into a tz-aware datetime."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None
