"""SQLite-backed store for multi-tenant SaaS primitives.

Tables
------
- tenants(id, name, tier, api_key_hash, created_at, updated_at)
- tenant_secrets(tenant_id, key, value_encrypted, created_at, rotated_at)
- users(id, tenant_id, email, password_hash, role, created_at)
- contacts(id, tenant_id, name, phone, email, tags JSON, created_at)
- campaigns(id, tenant_id, name, type, from_number, message, contact_ids JSON,
           schedule_at, status, created_at, updated_at, started_at,
           completed_at, stats_json)
- scheduled_jobs(id, tenant_id, kind, payload_json, run_at, status,
                 created_at, last_error)
- deliveries(id, tenant_id, kind, contact_id, target, payload_summary,
             telnyx_id, status, error, created_at) — append-only
- voicemails(id, tenant_id, call_id, from_number, to_number, recording_url,
             transcript, duration, created_at, read_at)
- recordings(id, tenant_id, call_id, recording_id, from_number, to_number,
             recording_url, transcript, duration, created_at)
- recordings_fts (FTS5 virtual table on from_number, to_number, transcript)

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
- FTS5 is available in the Python 3.12 stdlib sqlite3 build; we use it
  for the recordings search endpoint (issue #6).
"""
from __future__ import annotations

import hmac
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
    tier        TEXT NOT NULL DEFAULT 'free',
    api_key_hash TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
-- Indexes for new columns are created in _run_migrations() AFTER the
-- ALTER TABLE so that an existing pre-Phase-A database (where the
-- columns don't exist yet) doesn't fail the CREATE INDEX.

CREATE TABLE IF NOT EXISTS tenant_secrets (
    tenant_id        TEXT NOT NULL,
    key              TEXT NOT NULL,
    value_encrypted  TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    rotated_at       TEXT NOT NULL,
    PRIMARY KEY (tenant_id, key),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);
CREATE INDEX IF NOT EXISTS idx_secrets_tenant ON tenant_secrets(tenant_id);

CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    email         TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'admin',
    created_at    TEXT NOT NULL,
    UNIQUE (tenant_id, email),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);
CREATE INDEX IF NOT EXISTS idx_users_tenant ON users(tenant_id);

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

-- ────────────── Phase A backend (issues #5, #6) ──────────────
CREATE TABLE IF NOT EXISTS voicemails (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    call_id       TEXT,
    from_number   TEXT,
    to_number     TEXT,
    recording_url TEXT,
    transcript    TEXT,
    duration      INTEGER,
    created_at    TEXT NOT NULL,
    read_at       TEXT,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);
CREATE INDEX IF NOT EXISTS idx_voicemails_tenant ON voicemails(tenant_id);
CREATE INDEX IF NOT EXISTS idx_voicemails_unread ON voicemails(tenant_id, read_at);

CREATE TABLE IF NOT EXISTS recordings (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    call_id       TEXT,
    recording_id  TEXT,
    from_number   TEXT,
    to_number     TEXT,
    recording_url TEXT,
    transcript    TEXT,
    duration      INTEGER,
    created_at    TEXT NOT NULL,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);
CREATE INDEX IF NOT EXISTS idx_recordings_tenant ON recordings(tenant_id);
CREATE INDEX IF NOT EXISTS idx_recordings_recording_id ON recordings(recording_id);
CREATE INDEX IF NOT EXISTS idx_recordings_created_at ON recordings(tenant_id, created_at);

-- FTS5 virtual table. We keep it as a contentless table and use triggers
-- to populate from recordings. FTS5 is in the stdlib sqlite3 (3.12 ships
-- with FTS5 enabled by default).
CREATE VIRTUAL TABLE IF NOT EXISTS recordings_fts USING fts5(
    from_number,
    to_number,
    transcript,
    content='recordings',
    content_rowid='rowid'
);

-- Triggers to keep recordings_fts in sync with recordings. INSERT/UPDATE
-- rebuild the FTS row; DELETE removes it.
CREATE TRIGGER IF NOT EXISTS recordings_ai AFTER INSERT ON recordings BEGIN
    INSERT INTO recordings_fts(rowid, from_number, to_number, transcript)
    VALUES (new.rowid, new.from_number, new.to_number, COALESCE(new.transcript, ''));
END;
CREATE TRIGGER IF NOT EXISTS recordings_ad AFTER DELETE ON recordings BEGIN
    INSERT INTO recordings_fts(recordings_fts, rowid, from_number, to_number, transcript)
    VALUES('delete', old.rowid, old.from_number, old.to_number, COALESCE(old.transcript, ''));
END;
CREATE TRIGGER IF NOT EXISTS recordings_au AFTER UPDATE ON recordings BEGIN
    INSERT INTO recordings_fts(recordings_fts, rowid, from_number, to_number, transcript)
    VALUES('delete', old.rowid, old.from_number, old.to_number, COALESCE(old.transcript, ''));
    INSERT INTO recordings_fts(rowid, from_number, to_number, transcript)
    VALUES (new.rowid, new.from_number, new.to_number, COALESCE(new.transcript, ''));
END;
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
        """Create tables, run migrations, and seed the default tenant. Idempotent."""
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._run_migrations()
            cur = self._conn.execute(
                "SELECT id FROM tenants WHERE id = 'default'"
            )
            if cur.fetchone() is None:
                now = _utcnow()
                self._conn.execute(
                    "INSERT INTO tenants(id, name, tier, api_key_hash, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("default", "Default Tenant", "free", None, now, now),
                )
                log.info("Seeded default tenant")

    def _run_migrations(self) -> None:
        """One-shot migrations for pre-Phase-A databases.

        1. ``tenants`` was originally ``(id, name, created_at)``; Phase A
           adds ``tier, api_key_hash, updated_at``. ALTER TABLE ADD COLUMN
           is a no-op if the column already exists — we guard with a
           pragma check so the migration is idempotent.
        2. Indexes on the new columns are created AFTER the ALTER so the
           ``CREATE TABLE IF NOT EXISTS`` above doesn't trip on a missing
           column when upgrading an old DB.
        """
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(tenants)").fetchall()}
        if "tier" not in cols:
            self._conn.execute("ALTER TABLE tenants ADD COLUMN tier TEXT NOT NULL DEFAULT 'free'")
            log.info("Migrated tenants.tier")
        if "api_key_hash" not in cols:
            self._conn.execute("ALTER TABLE tenants ADD COLUMN api_key_hash TEXT")
            log.info("Migrated tenants.api_key_hash")
        if "updated_at" not in cols:
            self._conn.execute("ALTER TABLE tenants ADD COLUMN updated_at TEXT")
            # Backfill updated_at = created_at where missing.
            self._conn.execute(
                "UPDATE tenants SET updated_at = created_at WHERE updated_at IS NULL"
            )
            log.info("Migrated tenants.updated_at")
        # Idempotent index creation (only safe now that the columns exist).
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tenants_tier ON tenants(tier)"
        )

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

    # ──────────────────────────── tenants ────────────────────────────────
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
                now = _utcnow()
                self._conn.execute(
                    "INSERT INTO tenants(id, name, tier, api_key_hash, created_at, updated_at) "
                    "VALUES (?, ?, 'free', NULL, ?, ?)",
                    (tenant_id, name or tenant_id, now, now),
                )
                log.info("Auto-created tenant: %s", tenant_id)

    def create_tenant(
        self,
        tenant_id: str,
        name: str,
        tier: str = "free",
        api_key_hash: Optional[str] = None,
    ) -> dict:
        """Create a new tenant row. The caller (admin API) generated the
        API key and bcrypt hash; we never see the plaintext again."""
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                "INSERT INTO tenants(id, name, tier, api_key_hash, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (tenant_id, name, tier, api_key_hash, now, now),
            )
        return self.get_tenant(tenant_id)  # type: ignore[return-value]

    def get_tenant(self, tenant_id: str) -> Optional[dict]:
        return self._row("SELECT * FROM tenants WHERE id = ?", (tenant_id,))

    def list_tenants(self) -> list[dict]:
        """List all tenants. Admin-only — never call from a tenant endpoint."""
        return self._rows("SELECT * FROM tenants ORDER BY created_at DESC")

    def find_tenant_by_api_key(self, api_key_hash: str) -> Optional[dict]:
        """Return the first tenant whose ``api_key_hash`` matches the
        bcrypt digest. Bcrypt is salted, so the lookup is a linear scan;
        acceptable for the v1 tenant count (single-digit)."""
        rows = self._rows("SELECT * FROM tenants WHERE api_key_hash IS NOT NULL")
        for r in rows:
            if hmac.compare_digest((r.get("api_key_hash") or "").encode("utf-8"),
                                   api_key_hash.encode("utf-8")):
                return r
        return None

    def rotate_tenant_api_key(self, tenant_id: str, api_key_hash: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tenants SET api_key_hash = ?, updated_at = ? WHERE id = ?",
                (api_key_hash, _utcnow(), tenant_id),
            )

    def update_tenant_tier(self, tenant_id: str, tier: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tenants SET tier = ?, updated_at = ? WHERE id = ?",
                (tier, _utcnow(), tenant_id),
            )

    # ────────────────────── tenant_secrets ───────────────────────────────
    def upsert_secret(self, tenant_id: str, key: str, value_encrypted: str) -> None:
        """Insert or rotate a secret. ``rotated_at`` is bumped on every
        write so the admin UI can show 'last rotated' to operators."""
        now = _utcnow()
        with self._lock:
            existing = self._conn.execute(
                "SELECT created_at FROM tenant_secrets WHERE tenant_id = ? AND key = ?",
                (tenant_id, key),
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    "INSERT INTO tenant_secrets(tenant_id, key, value_encrypted, created_at, rotated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (tenant_id, key, value_encrypted, now, now),
                )
            else:
                self._conn.execute(
                    "UPDATE tenant_secrets SET value_encrypted = ?, rotated_at = ? "
                    "WHERE tenant_id = ? AND key = ?",
                    (value_encrypted, now, tenant_id, key),
                )

    def get_secret(self, tenant_id: str, key: str) -> Optional[str]:
        """Return the encrypted ciphertext for ``key`` — never the plaintext.
        Callers pass this through Fernet decryption (see ``webhooks.tenancy``)."""
        row = self._row(
            "SELECT value_encrypted FROM tenant_secrets WHERE tenant_id = ? AND key = ?",
            (tenant_id, key),
        )
        return (row or {}).get("value_encrypted")

    def list_secrets(self, tenant_id: str) -> list[dict]:
        """List the *keys* (no values) for the admin UI."""
        return self._rows(
            "SELECT key, created_at, rotated_at FROM tenant_secrets "
            "WHERE tenant_id = ? ORDER BY key",
            (tenant_id,),
        )

    def delete_secret(self, tenant_id: str, key: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM tenant_secrets WHERE tenant_id = ? AND key = ?",
                (tenant_id, key),
            )
            return cur.rowcount > 0

    # ────────────────────── users ────────────────────────────────────────
    def create_user(
        self,
        user_id: str,
        tenant_id: str,
        email: str,
        password_hash: str,
        role: str = "admin",
    ) -> dict:
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                "INSERT INTO users(id, tenant_id, email, password_hash, role, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, tenant_id, email, password_hash, role, now),
            )
        return {"id": user_id, "tenant_id": tenant_id, "email": email,
                "role": role, "created_at": now}

    def get_user(self, tenant_id: str, email: str) -> Optional[dict]:
        return self._row(
            "SELECT * FROM users WHERE tenant_id = ? AND email = ?",
            (tenant_id, email),
        )

    def get_user_by_id(self, user_id: str) -> Optional[dict]:
        return self._row("SELECT * FROM users WHERE id = ?", (user_id,))

    # ────────────────────── voicemails ───────────────────────────────────
    def upsert_voicemail(
        self,
        tenant_id: str,
        call_id: Optional[str],
        from_number: Optional[str],
        to_number: Optional[str],
        recording_url: Optional[str],
        transcript: Optional[str] = None,
        duration: Optional[int] = None,
    ) -> dict:
        """Idempotent on (tenant_id, call_id) so re-delivered Telnyx webhooks
        don't double-insert."""
        now = _utcnow()
        with self._lock:
            existing = None
            if call_id:
                existing = self._conn.execute(
                    "SELECT id FROM voicemails WHERE tenant_id = ? AND call_id = ?",
                    (tenant_id, call_id),
                ).fetchone()
            if existing is not None:
                # Refresh transcript / duration if we now have them.
                self._conn.execute(
                    "UPDATE voicemails SET transcript = COALESCE(?, transcript), "
                    "duration = COALESCE(?, duration) WHERE id = ?",
                    (transcript, duration, existing[0]),
                )
                vmail = self._row("SELECT * FROM voicemails WHERE id = ?", (existing[0],))
                return vmail  # type: ignore[return-value]
            vid = _new_id("vm")
            self._conn.execute(
                "INSERT INTO voicemails(id, tenant_id, call_id, from_number, to_number, "
                "recording_url, transcript, duration, created_at, read_at) "
                "VALUES (?,?,?,?,?,?,?,?,?, NULL)",
                (vid, tenant_id, call_id, from_number, to_number,
                 recording_url, transcript, duration, now),
            )
        return self._row("SELECT * FROM voicemails WHERE id = ?", (vid,))  # type: ignore[return-value]

    def list_voicemails(
        self,
        tenant_id: str,
        unread_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        where = "tenant_id = ?"
        params: list[Any] = [tenant_id]
        if unread_only:
            where += " AND read_at IS NULL"
        params.extend([int(limit), int(offset)])
        return self._rows(
            f"SELECT * FROM voicemails WHERE {where} "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            tuple(params),
        )

    def get_voicemail(self, tenant_id: str, voicemail_id: str) -> Optional[dict]:
        return self._row(
            "SELECT * FROM voicemails WHERE tenant_id = ? AND id = ?",
            (tenant_id, voicemail_id),
        )

    def mark_voicemail_read(self, tenant_id: str, voicemail_id: str) -> Optional[dict]:
        with self._lock:
            self._conn.execute(
                "UPDATE voicemails SET read_at = ? WHERE tenant_id = ? AND id = ? AND read_at IS NULL",
                (_utcnow(), tenant_id, voicemail_id),
            )
        return self.get_voicemail(tenant_id, voicemail_id)

    # ────────────────────── recordings ───────────────────────────────────
    def upsert_recording(
        self,
        tenant_id: str,
        call_id: Optional[str],
        recording_id: Optional[str],
        from_number: Optional[str],
        to_number: Optional[str],
        recording_url: Optional[str],
        transcript: Optional[str] = None,
        duration: Optional[int] = None,
    ) -> dict:
        now = _utcnow()
        with self._lock:
            existing = None
            if recording_id:
                existing = self._conn.execute(
                    "SELECT id FROM recordings WHERE tenant_id = ? AND recording_id = ?",
                    (tenant_id, recording_id),
                ).fetchone()
            if existing is None and call_id:
                existing = self._conn.execute(
                    "SELECT id FROM recordings WHERE tenant_id = ? AND call_id = ?",
                    (tenant_id, call_id),
                ).fetchone()
            if existing is not None:
                self._conn.execute(
                    "UPDATE recordings SET transcript = COALESCE(?, transcript), "
                    "duration = COALESCE(?, duration), recording_url = COALESCE(?, recording_url) "
                    "WHERE id = ?",
                    (transcript, duration, recording_url, existing[0]),
                )
                return self._row("SELECT * FROM recordings WHERE id = ?", (existing[0],))  # type: ignore[return-value]
            rid = _new_id("rec")
            self._conn.execute(
                "INSERT INTO recordings(id, tenant_id, call_id, recording_id, from_number, "
                "to_number, recording_url, transcript, duration, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (rid, tenant_id, call_id, recording_id, from_number, to_number,
                 recording_url, transcript, duration, now),
            )
        return self._row("SELECT * FROM recordings WHERE id = ?", (rid,))  # type: ignore[return-value]

    def get_recording(self, tenant_id: str, recording_id: str) -> Optional[dict]:
        """``recording_id`` here is the **internal** row id, not Telnyx's."""
        return self._row(
            "SELECT * FROM recordings WHERE tenant_id = ? AND id = ?",
            (tenant_id, recording_id),
        )

    def get_recording_by_telnyx_id(self, tenant_id: str, telnyx_recording_id: str) -> Optional[dict]:
        return self._row(
            "SELECT * FROM recordings WHERE tenant_id = ? AND recording_id = ?",
            (tenant_id, telnyx_recording_id),
        )

    def search_recordings(
        self,
        tenant_id: str,
        q: Optional[str] = None,
        from_number: Optional[str] = None,
        to_number: Optional[str] = None,
        min_duration: Optional[int] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """FTS5 + filter search. ``q`` is matched against the indexed
        ``from_number, to_number, transcript`` columns."""
        where = ["r.tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if q:
            # Sanitise the FTS5 query: split on whitespace, quote each
            # token, and OR them. This is the standard 'user search box'
            # behaviour — each word is matched independently.
            tokens = [t for t in q.replace('"', " ").split() if t]
            if tokens:
                fts_expr = " OR ".join(f'"{t}"' for t in tokens)
                where.append("r.rowid IN (SELECT rowid FROM recordings_fts WHERE recordings_fts MATCH ?)")
                params.append(fts_expr)
        if from_number:
            where.append("r.from_number = ?")
            params.append(from_number)
        if to_number:
            where.append("r.to_number = ?")
            params.append(to_number)
        if min_duration is not None:
            where.append("r.duration >= ?")
            params.append(int(min_duration))
        params.extend([int(limit), int(offset)])
        sql = (
            f"SELECT r.* FROM recordings r WHERE {' AND '.join(where)} "
            "ORDER BY r.created_at DESC LIMIT ? OFFSET ?"
        )
        return self._rows(sql, tuple(params))

    # ──────────────────────────── contacts ────────────────────────────────

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
