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

-- ────────────── Phase B backend (issues #13, #14, #15, #17, #18) ──────────────

-- workflows (#13): the visual no-code IVR builder stores a JSON graph.
-- `graph_json` is a free-form JSON object: {nodes:[{id,type,...}],
-- edges:[{from,to,condition?}], settings:{...}}. The executor (in
-- workflow_engine.py) walks the graph starting at `entry_node_id`.
CREATE TABLE IF NOT EXISTS workflows (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    name          TEXT NOT NULL,
    graph_json    TEXT NOT NULL DEFAULT '{}',
    entry_node_id TEXT,
    version       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);
CREATE INDEX IF NOT EXISTS idx_workflows_tenant ON workflows(tenant_id);

-- A "number" is a Telnyx phone number owned by the tenant. The assignment
-- column on the row is the simplest possible pointer — workflow_id OR
-- assistant_id OR 'direct'. The richer number_assignments table (#15)
-- keeps a historical log + supports multiple kinds per row.
CREATE TABLE IF NOT EXISTS phone_numbers (
    id                TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL,
    phone_number      TEXT NOT NULL,
    telnyx_id         TEXT,
    country_code      TEXT,
    monthly_cost      REAL,
    per_minute_rate   REAL,
    assignment_kind   TEXT,   -- 'workflow' | 'assistant' | 'direct' | NULL
    assignment_target TEXT,   -- workflow_id | assistant_id | 'inbox'
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);
CREATE INDEX IF NOT EXISTS idx_phone_numbers_tenant ON phone_numbers(tenant_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_phone_numbers_tenant_phone
    ON phone_numbers(tenant_id, phone_number);

-- Historical log of number assignments (#15). Every time a number's
-- assignment changes we append a row; the current state is denormalised
-- on phone_numbers.assignment_*.
CREATE TABLE IF NOT EXISTS number_assignments (
    id           TEXT PRIMARY KEY,
    tenant_id    TEXT NOT NULL,
    number_id    TEXT NOT NULL,
    kind         TEXT NOT NULL,
    target_id    TEXT,
    created_at   TEXT NOT NULL,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id),
    FOREIGN KEY (number_id) REFERENCES phone_numbers(id)
);
CREATE INDEX IF NOT EXISTS idx_number_assignments_tenant
    ON number_assignments(tenant_id);
CREATE INDEX IF NOT EXISTS idx_number_assignments_number
    ON number_assignments(number_id);

-- assistants (#14): a tenant-scoped mirror of the Telnyx AI assistant.
-- telnyx_id is the actual resource id we round-trip with; everything else
-- is a local cache + config snapshot.
CREATE TABLE IF NOT EXISTS assistants (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    name          TEXT NOT NULL,
    telnyx_id     TEXT,
    voice         TEXT,
    system_prompt TEXT,
    model         TEXT,
    tools_json    TEXT NOT NULL DEFAULT '[]',
    greeting      TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);
CREATE INDEX IF NOT EXISTS idx_assistants_tenant ON assistants(tenant_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_assistants_tenant_telnyx
    ON assistants(tenant_id, telnyx_id);

-- Live transcript + tool-call log per assistant test call (#14). Append-only.
CREATE TABLE IF NOT EXISTS assistant_call_log (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    assistant_id  TEXT NOT NULL,
    call_id       TEXT,
    role          TEXT NOT NULL,    -- 'user' | 'assistant' | 'tool' | 'system'
    content       TEXT,
    tool_name     TEXT,
    tool_args     TEXT,
    created_at    TEXT NOT NULL,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);
CREATE INDEX IF NOT EXISTS idx_assistant_call_log_call
    ON assistant_call_log(tenant_id, call_id);
CREATE INDEX IF NOT EXISTS idx_assistant_call_log_assistant
    ON assistant_call_log(tenant_id, assistant_id);

-- Campaign AI handoff log (#17). One row per (campaign, contact) when
-- the AI hands the call off to a human agent. `mode` is the campaign
-- mode at the time of handoff; useful for analytics.
CREATE TABLE IF NOT EXISTS campaign_handoffs (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    campaign_id     TEXT NOT NULL,
    contact_id      TEXT,
    call_id         TEXT,
    mode            TEXT NOT NULL,   -- 'ai_then_human' | 'human' | 'voicemail_drop'
    human_agent_id  TEXT,
    transferred_at  TEXT NOT NULL,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);
CREATE INDEX IF NOT EXISTS idx_campaign_handoffs_tenant
    ON campaign_handoffs(tenant_id);
CREATE INDEX IF NOT EXISTS idx_campaign_handoffs_campaign
    ON campaign_handoffs(tenant_id, campaign_id);

-- ────────────── Phase B business surface (issues #16, #19, #20) ──────────────
-- The other Phase B worker owns #13/14/15/17/18 (workflows/agents/numbers/handoff).
-- This block covers analytics rollup (#16), billing (#19), and the append-only
-- audit log (#20). They are added to storage.py because everything else
-- (tenants, deliveries, contacts) is here too, and the new screens join
-- against the existing tables.

-- #16 — flat per-tenant per-day rollup. A nightly cron rebuilds it from
-- the deliveries + call event tables; the analytics API can also
-- backfill on the fly for the requested window.
CREATE TABLE IF NOT EXISTS analytics_rollup_daily (
    tenant_id   TEXT NOT NULL,
    day         TEXT NOT NULL,           -- 'YYYY-MM-DD' UTC
    calls_in    INTEGER NOT NULL DEFAULT 0,
    calls_out   INTEGER NOT NULL DEFAULT 0,
    sms_in      INTEGER NOT NULL DEFAULT 0,
    sms_out     INTEGER NOT NULL DEFAULT 0,
    spend_cents INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, day)
);
CREATE INDEX IF NOT EXISTS idx_rollup_day ON analytics_rollup_daily(day);

-- #19 — Stripe-driven subscription + usage. We keep a denormalised
-- ``tenant_id → plan`` here so the dashboard and rate-limit gate can
-- read it without joining Stripe on every request.
CREATE TABLE IF NOT EXISTS subscriptions (
    id                      TEXT PRIMARY KEY,
    tenant_id               TEXT NOT NULL UNIQUE,
    plan                    TEXT NOT NULL DEFAULT 'free',
    status                  TEXT NOT NULL DEFAULT 'active',
    stripe_customer_id      TEXT,
    stripe_subscription_id  TEXT,
    current_period_start    TEXT,
    current_period_end      TEXT,
    cancel_at_period_end    INTEGER NOT NULL DEFAULT 0,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_subs_stripe ON subscriptions(stripe_customer_id);
CREATE INDEX IF NOT EXISTS idx_subs_status ON subscriptions(status);

CREATE TABLE IF NOT EXISTS usage_records (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK(kind IN ('voice_minutes','sms_segments','numbers')),
    quantity        INTEGER NOT NULL DEFAULT 0,
    period_start    TEXT NOT NULL,
    period_end      TEXT NOT NULL,
    billed          INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_tenant_period ON usage_records(tenant_id, period_start, period_end);
CREATE INDEX IF NOT EXISTS idx_usage_kind ON usage_records(kind);

-- #20 — append-only audit log. The middleware writes one row per /api/*
-- request. Triggers raise on UPDATE/DELETE so history cannot be rewritten
-- (an operator can only move rows into audit_log_archive).
CREATE TABLE IF NOT EXISTS audit_log (
    id               TEXT PRIMARY KEY,
    tenant_id        TEXT NOT NULL,
    user_id          TEXT,
    action           TEXT NOT NULL,
    target           TEXT,
    ip               TEXT,
    user_agent       TEXT,
    request_id       TEXT,
    method           TEXT,
    path             TEXT,
    response_status  INTEGER,
    response_time_ms INTEGER,
    request_body     TEXT,
    response_body    TEXT,
    timestamp        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_tenant_ts ON audit_log(tenant_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id);

CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only; use audit_log_archive instead');
END;

CREATE TABLE IF NOT EXISTS audit_log_archive (
    id               TEXT PRIMARY KEY,
    tenant_id        TEXT NOT NULL,
    user_id          TEXT,
    action           TEXT NOT NULL,
    target           TEXT,
    ip               TEXT,
    user_agent       TEXT,
    request_id       TEXT,
    method           TEXT,
    path             TEXT,
    response_status  INTEGER,
    response_time_ms INTEGER,
    request_body     TEXT,
    response_body    TEXT,
    timestamp        TEXT NOT NULL,
    archived_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_arch_tenant_ts ON audit_log_archive(tenant_id, timestamp);

-- ────────────── Phase C compliance + mass test (issues #24, #25) ──────────────
-- The other Phase C worker owns #22/23 (WhatsApp + SMS blast). This block
-- adds (a) the synthetic-call log used by the test-mode simulator and
-- (b) the DNC cache table used by the compliance pre-flight.

-- #24 — synthetic_calls: one row per simulated call leg. ``outcome`` is
-- free-form (matches the campaign mode's outcome vocabulary), and
-- ``tool_calls_json`` holds the assistant's tool-call log when the
-- campaign mode is ai/voicemail_drop.
CREATE TABLE IF NOT EXISTS synthetic_calls (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    campaign_id     TEXT NOT NULL,
    contact_id      TEXT,
    outcome         TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    transcript      TEXT,
    tool_calls_json TEXT NOT NULL DEFAULT '[]',
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);
CREATE INDEX IF NOT EXISTS idx_synthetic_calls_tenant ON synthetic_calls(tenant_id);
CREATE INDEX IF NOT EXISTS idx_synthetic_calls_campaign ON synthetic_calls(tenant_id, campaign_id);

-- #25 — dnc_cache: the per-phone, per-source deny-list. Composite PK so
-- a phone can have one row per source (e.g. us_dnc, ca_dnc, internal).
-- ``expires_at`` is what :func:`compliance.dnc._cache_valid` checks.
CREATE TABLE IF NOT EXISTS dnc_cache (
    id          TEXT PRIMARY KEY,
    phone       TEXT NOT NULL,
    source      TEXT NOT NULL,
    is_dnc      INTEGER NOT NULL DEFAULT 0,
    checked_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    UNIQUE (phone, source)
);
CREATE INDEX IF NOT EXISTS idx_dnc_cache_phone ON dnc_cache(phone);
CREATE INDEX IF NOT EXISTS idx_dnc_cache_expires ON dnc_cache(expires_at);

-- #22 — WhatsApp templates and messages
CREATE TABLE IF NOT EXISTS whatsapp_templates (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    telnyx_id   TEXT NOT NULL,
    name        TEXT NOT NULL,
    language    TEXT NOT NULL DEFAULT 'en',
    variables   TEXT NOT NULL DEFAULT '[]',
    status      TEXT NOT NULL DEFAULT 'approved',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (tenant_id, name)
);
CREATE INDEX IF NOT EXISTS idx_wa_tpl_tenant ON whatsapp_templates(tenant_id);

CREATE TABLE IF NOT EXISTS whatsapp_messages (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    direction   TEXT NOT NULL,
    remote      TEXT NOT NULL,
    from_number TEXT NOT NULL,
    to_number   TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    telnyx_id   TEXT,
    status      TEXT NOT NULL DEFAULT 'received',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wa_msg_tenant ON whatsapp_messages(tenant_id);
CREATE INDEX IF NOT EXISTS idx_wa_msg_remote ON whatsapp_messages(tenant_id, remote);

-- #23 — Suppression list and SMS replies
CREATE TABLE IF NOT EXISTS suppression_list (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    phone       TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT 'manual',
    source      TEXT NOT NULL DEFAULT 'manual',
    note        TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (tenant_id, phone)
);
CREATE INDEX IF NOT EXISTS idx_supp_tenant ON suppression_list(tenant_id);

CREATE TABLE IF NOT EXISTS sms_replies (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    campaign_id   TEXT,
    contact_id    TEXT,
    from_number   TEXT NOT NULL,
    body          TEXT NOT NULL DEFAULT '',
    received_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sms_reply_tenant ON sms_replies(tenant_id);
CREATE INDEX IF NOT EXISTS idx_sms_reply_campaign ON sms_replies(tenant_id, campaign_id);

-- #27 — Email templates and messages
CREATE TABLE IF NOT EXISTS email_templates (
    id                TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL,
    name              TEXT NOT NULL,
    subject_template  TEXT NOT NULL,
    body_template     TEXT NOT NULL,
    variables         TEXT NOT NULL DEFAULT '[]',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (tenant_id, name)
);
CREATE INDEX IF NOT EXISTS idx_email_tpl_tenant ON email_templates(tenant_id);

CREATE TABLE IF NOT EXISTS email_messages (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    provider    TEXT NOT NULL DEFAULT 'dev',
    direction   TEXT NOT NULL,
    from_addr   TEXT NOT NULL,
    to_addr     TEXT NOT NULL,
    subject     TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL DEFAULT '',
    html        TEXT,
    status      TEXT NOT NULL DEFAULT 'queued',
    error       TEXT,
    sent_at     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_email_msg_tenant ON email_messages(tenant_id);
CREATE INDEX IF NOT EXISTS idx_email_msg_to ON email_messages(tenant_id, to_addr);

-- #26 — Meetings
CREATE TABLE IF NOT EXISTS meetings (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    host_user_id    TEXT,
    title           TEXT NOT NULL DEFAULT '',
    room_url        TEXT NOT NULL DEFAULT '',
    room_name       TEXT NOT NULL DEFAULT '',
    started_at      TEXT,
    ended_at        TEXT,
    recording_url   TEXT,
    participants    TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'created',
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meetings_tenant ON meetings(tenant_id);

-- #28 — Network quality log
CREATE TABLE IF NOT EXISTS network_quality_log (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    call_id         TEXT,
    rtt_ms          REAL NOT NULL DEFAULT 0,
    jitter_ms       REAL NOT NULL DEFAULT 0,
    packet_loss_pct REAL NOT NULL DEFAULT 0,
    score           REAL NOT NULL DEFAULT 0,
    timestamp       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nq_tenant ON network_quality_log(tenant_id, timestamp);

-- ────────────── Phase E Live Agent v1 (issues #31, #32, #34) ─────────────
-- #31 — Agent presence: one row per (tenant, user). `status` is the
-- v1 enum online | away | busy | on_call | offline. `last_seen` is the
-- ISO timestamp of the most recent heartbeat; the periodic offline
-- sweeper (see webhooks.presence) flips 'online' to 'offline' when
-- last_seen < now - 90s.
CREATE TABLE IF NOT EXISTS agent_presence (
    tenant_id        TEXT NOT NULL,
    user_id          TEXT NOT NULL,
    status           TEXT NOT NULL CHECK(status IN
                       ('online','away','busy','on_call','offline')),
    last_seen        TEXT NOT NULL,
    current_call_id  TEXT,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (tenant_id, user_id),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);
CREATE INDEX IF NOT EXISTS idx_presence_tenant_status
    ON agent_presence(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_presence_last_seen
    ON agent_presence(tenant_id, last_seen);

-- #31 — Agent skills: one row per (tenant, user, skill). `level` is an
-- optional 0-100 proficiency hint, defaults to 50. Used by #32's queue
-- (skill-tag match) and #34's forward_agent node (skill routing).
CREATE TABLE IF NOT EXISTS agent_skills (
    tenant_id     TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    skill         TEXT NOT NULL,
    level         INTEGER NOT NULL DEFAULT 50,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (tenant_id, user_id, skill),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);
CREATE INDEX IF NOT EXISTS idx_agent_skills_skill
    ON agent_skills(tenant_id, skill);

-- #32 — Call queue: FIFO of pending human-handoff calls. ``status``
-- progresses queued → assigned → answered. ``abandoned`` happens when
-- the caller hangs up before being answered.
CREATE TABLE IF NOT EXISTS call_queue (
    id                 TEXT PRIMARY KEY,
    tenant_id          TEXT NOT NULL,
    call_id            TEXT NOT NULL,
    enqueued_at        TEXT NOT NULL,
    priority           INTEGER NOT NULL DEFAULT 0,
    skill_tags_json    TEXT NOT NULL DEFAULT '[]',
    assigned_user_id   TEXT,
    status             TEXT NOT NULL CHECK(status IN
                         ('queued','assigned','answered','abandoned')),
    dequeued_at        TEXT,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);
CREATE INDEX IF NOT EXISTS idx_call_queue_tenant_status
    ON call_queue(tenant_id, status, priority DESC, enqueued_at ASC);
CREATE INDEX IF NOT EXISTS idx_call_queue_call
    ON call_queue(tenant_id, call_id);
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
            self._run_migrations_phase_b()
            self._run_migrations_phase_c()
            self._run_migrations_phase_d()
            self._run_migrations_phase_e()
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

    def _run_migrations_phase_b(self) -> None:
        """Phase B (issues #13, #14, #15, #17, #18) migrations.

        - ``campaigns.outbound_mode`` (default 'human')
        - ``campaigns.assistant_id`` (nullable FK to assistants)
        - ``campaigns.voicemail_audio_url`` (nullable — populated on upload)
        - ``campaigns.voicemail_no_answer_action`` ('hangup'|'voicemail'|'retry')
        - ``campaigns.ring_timeout_secs`` (per-campaign no-answer timeout)

        Each ALTER is guarded with a PRAGMA check so the migration is
        idempotent across restarts and across pre-Phase-B databases.
        """
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(campaigns)").fetchall()}
        if "outbound_mode" not in cols:
            self._conn.execute(
                "ALTER TABLE campaigns ADD COLUMN outbound_mode TEXT NOT NULL DEFAULT 'human'"
            )
            log.info("Migrated campaigns.outbound_mode")
        if "assistant_id" not in cols:
            self._conn.execute(
                "ALTER TABLE campaigns ADD COLUMN assistant_id TEXT"
            )
            log.info("Migrated campaigns.assistant_id")
        if "voicemail_audio_url" not in cols:
            self._conn.execute(
                "ALTER TABLE campaigns ADD COLUMN voicemail_audio_url TEXT"
            )
            log.info("Migrated campaigns.voicemail_audio_url")
        if "voicemail_no_answer_action" not in cols:
            self._conn.execute(
                "ALTER TABLE campaigns ADD COLUMN voicemail_no_answer_action "
                "TEXT NOT NULL DEFAULT 'hangup'"
            )
            log.info("Migrated campaigns.voicemail_no_answer_action")
        if "ring_timeout_secs" not in cols:
            self._conn.execute(
                "ALTER TABLE campaigns ADD COLUMN ring_timeout_secs INTEGER NOT NULL DEFAULT 25"
            )
            log.info("Migrated campaigns.ring_timeout_secs")

    def _run_migrations_phase_c(self) -> None:
        """Phase C (issues #24, #25) migrations.

        - ``campaigns.test_mode`` (0/1) — when set, the dialer walks the
          workflow synthetically rather than hitting Telnyx.
        - ``campaigns.dnc_check_enabled`` (0/1) — default on.
        - ``campaigns.time_window_enabled`` (0/1) — default on.
        - ``campaigns.time_window_start`` / ``campaigns.time_window_end``
          — local-time bounds (default 8 → 21, matching the TCPA).

        Each ALTER is guarded with a PRAGMA check so the migration is
        idempotent across restarts and across pre-Phase-C databases.
        """
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(campaigns)").fetchall()}
        if "test_mode" not in cols:
            self._conn.execute(
                "ALTER TABLE campaigns ADD COLUMN test_mode INTEGER NOT NULL DEFAULT 0"
            )
            log.info("Migrated campaigns.test_mode")
        if "dnc_check_enabled" not in cols:
            self._conn.execute(
                "ALTER TABLE campaigns ADD COLUMN dnc_check_enabled INTEGER NOT NULL DEFAULT 1"
            )
            log.info("Migrated campaigns.dnc_check_enabled")
        if "time_window_enabled" not in cols:
            self._conn.execute(
                "ALTER TABLE campaigns ADD COLUMN time_window_enabled INTEGER NOT NULL DEFAULT 1"
            )
            log.info("Migrated campaigns.time_window_enabled")
        if "time_window_start" not in cols:
            self._conn.execute(
                "ALTER TABLE campaigns ADD COLUMN time_window_start INTEGER NOT NULL DEFAULT 8"
            )
            log.info("Migrated campaigns.time_window_start")
        if "time_window_end" not in cols:
            self._conn.execute(
                "ALTER TABLE campaigns ADD COLUMN time_window_end INTEGER NOT NULL DEFAULT 21"
            )
            log.info("Migrated campaigns.time_window_end")

    def _run_migrations_phase_d(self) -> None:
        """Phase D migrations for issues #26, #27, #28, #29, #30.

        - ``phone_numbers.whatsapp_enabled`` (0/1) — used by the Numbers
          tab UI to show a green WhatsApp badge.
        - ``tenants.region`` ('us' or 'eu') — used by multi-region routing.
        - ``tenants.region_lock`` (0/1) — when on, the tenant's data is
          pinned to one region; no cross-region fallback.
        - ``tenants.brand_json`` (TEXT) — JSON blob: logo, colors, custom
          domain, support email. Used by the white-label login page.
        - ``tenants.custom_domain`` (TEXT) — e.g. ``acme.agentops.com``.

        All ALTERs are guarded by a PRAGMA check so the migration is
        idempotent.
        """
        # phone_numbers.whatsapp_enabled
        pn_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(phone_numbers)").fetchall()}
        if "whatsapp_enabled" not in pn_cols:
            self._conn.execute(
                "ALTER TABLE phone_numbers ADD COLUMN whatsapp_enabled INTEGER NOT NULL DEFAULT 0"
            )
            log.info("Migrated phone_numbers.whatsapp_enabled")

        # tenants.* Phase D columns
        t_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(tenants)").fetchall()}
        if "region" not in t_cols:
            self._conn.execute(
                "ALTER TABLE tenants ADD COLUMN region TEXT NOT NULL DEFAULT 'us'"
            )
            log.info("Migrated tenants.region")
        if "region_lock" not in t_cols:
            self._conn.execute(
                "ALTER TABLE tenants ADD COLUMN region_lock INTEGER NOT NULL DEFAULT 0"
            )
            log.info("Migrated tenants.region_lock")
        if "brand_json" not in t_cols:
            self._conn.execute(
                "ALTER TABLE tenants ADD COLUMN brand_json TEXT"
            )
            log.info("Migrated tenants.brand_json")
        if "custom_domain" not in t_cols:
            self._conn.execute(
                "ALTER TABLE tenants ADD COLUMN custom_domain TEXT"
            )
            log.info("Migrated tenants.custom_domain")

    def _run_migrations_phase_e(self) -> None:
        """Phase E migrations for issues #31, #34.

        - ``users.assigned_number`` (nullable E.164 string) — the
          human agent's direct DID that the workflow engine can
          transfer to when ``forward_agent`` finds them idle.
        - ``users.display_name`` (nullable) — a friendlier label than
          the email, used in the agent dashboard's recent-calls list
          and presence roster.

        Each ALTER is guarded by a PRAGMA check so the migration is
        idempotent across restarts and across pre-Phase-E databases.
        """
        u_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(users)").fetchall()}
        if "assigned_number" not in u_cols:
            self._conn.execute(
                "ALTER TABLE users ADD COLUMN assigned_number TEXT"
            )
            log.info("Migrated users.assigned_number")
        if "display_name" not in u_cols:
            self._conn.execute(
                "ALTER TABLE users ADD COLUMN display_name TEXT"
            )
            log.info("Migrated users.display_name")

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
                   "schedule_at", "status",
                   # Phase C — compliance + test-mode flags (#24, #25)
                   "test_mode", "dnc_check_enabled", "time_window_enabled",
                   "time_window_start", "time_window_end"}
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

    # ──────────────── Phase C — synthetic_calls + dnc_cache (#24, #25) ────────────

    def insert_synthetic_call(
        self,
        tenant_id: str,
        campaign_id: str,
        outcome: str,
        *,
        contact_id: Optional[str] = None,
        started_at: Optional[str] = None,
        ended_at: Optional[str] = None,
        transcript: Optional[str] = None,
        tool_calls: Optional[list[dict]] = None,
    ) -> dict:
        """Insert one row in ``synthetic_calls``. Returns the inserted row.

        ``started_at``/``ended_at`` default to ``now`` and ``now + 1s``
        respectively (the simulator doesn't care about real durations).
        """
        sid = _new_id("syn")
        started = started_at or _utcnow()
        ended = ended_at or _utcnow()
        if not isinstance(started, str):
            started = started.isoformat()
        if not isinstance(ended, str):
            ended = ended.isoformat()
        tool_calls_json = json.dumps(tool_calls or [])
        with self._lock:
            self._conn.execute(
                "INSERT INTO synthetic_calls("
                "id, tenant_id, campaign_id, contact_id, outcome, "
                "started_at, ended_at, transcript, tool_calls_json"
                ") VALUES (?,?,?,?,?,?,?,?,?)",
                (sid, tenant_id, campaign_id, contact_id, outcome,
                 started, ended, transcript, tool_calls_json),
            )
        return self.get_synthetic_call(tenant_id, sid)  # type: ignore[return-value]

    def get_synthetic_call(self, tenant_id: str, call_id: str) -> Optional[dict]:
        row = self._row(
            "SELECT * FROM synthetic_calls "
            "WHERE tenant_id = ? AND id = ?",
            (tenant_id, call_id),
        )
        if not row:
            return None
        try:
            row["tool_calls"] = json.loads(row.get("tool_calls_json") or "[]")
        except json.JSONDecodeError:
            row["tool_calls"] = []
        return row

    def list_synthetic_calls(
        self,
        tenant_id: str,
        campaign_id: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict]:
        """List synthetic calls, newest first. ``tool_calls_json`` is
        decoded into a ``tool_calls`` list for callers."""
        if campaign_id:
            rows = self._rows(
                "SELECT * FROM synthetic_calls "
                "WHERE tenant_id = ? AND campaign_id = ? "
                "ORDER BY started_at DESC LIMIT ?",
                (tenant_id, campaign_id, int(limit)),
            )
        else:
            rows = self._rows(
                "SELECT * FROM synthetic_calls WHERE tenant_id = ? "
                "ORDER BY started_at DESC LIMIT ?",
                (tenant_id, int(limit)),
            )
        for r in rows:
            try:
                r["tool_calls"] = json.loads(r.get("tool_calls_json") or "[]")
            except json.JSONDecodeError:
                r["tool_calls"] = []
        return rows

    def synthetic_call_outcome_summary(
        self, tenant_id: str, campaign_id: str
    ) -> dict[str, int]:
        """Return ``{outcome: count}`` for one campaign's synthetic calls."""
        rows = self._rows(
            "SELECT outcome, COUNT(*) AS c FROM synthetic_calls "
            "WHERE tenant_id = ? AND campaign_id = ? "
            "GROUP BY outcome",
            (tenant_id, campaign_id),
        )
        return {r["outcome"]: int(r["c"] or 0) for r in rows}

    def dnc_lookup(self, phone: str, source: str = "us_dnc") -> Optional[dict]:
        """Read-only cache row lookup. Returns ``None`` on miss. The
        ``compliance.dnc`` module owns the upsert; this is the read
        side so the API layer can render ``last_checked_at``."""
        return self._row(
            "SELECT * FROM dnc_cache WHERE phone = ? AND source = ?",
            (phone, source),
        )

    # ──────────────── Phase B (issues #13, #14, #15, #17, #18) ────────────────
    # ─────────────────────────── workflows (#13) ─────────────────────────────

    def create_workflow(
        self,
        tenant_id: str,
        name: str,
        graph_json: dict,
        entry_node_id: Optional[str] = None,
    ) -> dict:
        wid = _new_id("wf")
        now = _utcnow()
        with self._lock:
            self.ensure_tenant(tenant_id)
            self._conn.execute(
                "INSERT INTO workflows("
                "id, tenant_id, name, graph_json, entry_node_id, "
                "version, created_at, updated_at"
                ") VALUES (?,?,?,?,?,?,?,?)",
                (
                    wid, tenant_id, name,
                    json.dumps(graph_json or {}),
                    entry_node_id,
                    1, now, now,
                ),
            )
        wf = self.get_workflow(tenant_id, wid)
        assert wf is not None
        return wf

    def list_workflows(self, tenant_id: str) -> list[dict]:
        rows = self._rows(
            "SELECT * FROM workflows WHERE tenant_id = ? ORDER BY updated_at DESC",
            (tenant_id,),
        )
        for r in rows:
            r["graph"] = _safe_json_dict(r.get("graph_json"))
        return rows

    def get_workflow(self, tenant_id: str, workflow_id: str) -> Optional[dict]:
        row = self._row(
            "SELECT * FROM workflows WHERE tenant_id = ? AND id = ?",
            (tenant_id, workflow_id),
        )
        if row:
            row["graph"] = _safe_json_dict(row.get("graph_json"))
        return row

    def update_workflow(
        self,
        tenant_id: str,
        workflow_id: str,
        *,
        name: Optional[str] = None,
        graph_json: Optional[dict] = None,
        entry_node_id: Optional[str] = None,
    ) -> Optional[dict]:
        existing = self.get_workflow(tenant_id, workflow_id)
        if not existing:
            return None
        sets: list[str] = []
        vals: list[Any] = []
        if name is not None:
            sets.append("name = ?")
            vals.append(name)
        if graph_json is not None:
            sets.append("graph_json = ?")
            vals.append(json.dumps(graph_json))
        if entry_node_id is not None:
            sets.append("entry_node_id = ?")
            vals.append(entry_node_id)
        if not sets:
            return existing
        # Bump version on every save so the webhook handler can detect a
        # mid-call graph change and log a warning (we don't auto-restart
        # in-flight calls).
        sets.append("version = version + 1")
        sets.append("updated_at = ?")
        vals.append(_utcnow())
        vals.extend([tenant_id, workflow_id])
        with self._lock:
            self._conn.execute(
                f"UPDATE workflows SET {', '.join(sets)} "
                "WHERE tenant_id = ? AND id = ?",
                tuple(vals),
            )
        return self.get_workflow(tenant_id, workflow_id)

    def delete_workflow(self, tenant_id: str, workflow_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM workflows WHERE tenant_id = ? AND id = ?",
                (tenant_id, workflow_id),
            )
            return cur.rowcount > 0

    # ─────────────────────────── phone numbers (#15) ────────────────────────

    def upsert_phone_number(
        self,
        tenant_id: str,
        phone_number: str,
        *,
        telnyx_id: Optional[str] = None,
        country_code: Optional[str] = None,
        monthly_cost: Optional[float] = None,
        per_minute_rate: Optional[float] = None,
    ) -> dict:
        """Insert or update a phone number row. Idempotent on
        (tenant_id, phone_number) so re-syncing from Telnyx doesn't dup.
        """
        now = _utcnow()
        with self._lock:
            self.ensure_tenant(tenant_id)
            existing = self._conn.execute(
                "SELECT id FROM phone_numbers "
                "WHERE tenant_id = ? AND phone_number = ?",
                (tenant_id, phone_number),
            ).fetchone()
            if existing is None:
                nid = _new_id("num")
                self._conn.execute(
                    "INSERT INTO phone_numbers("
                    "id, tenant_id, phone_number, telnyx_id, country_code, "
                    "monthly_cost, per_minute_rate, created_at, updated_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?)",
                    (nid, tenant_id, phone_number, telnyx_id, country_code,
                     monthly_cost, per_minute_rate, now, now),
                )
            else:
                # Refill any non-null fields provided.
                sets: list[str] = []
                vals: list[Any] = []
                if telnyx_id is not None:
                    sets.append("telnyx_id = ?")
                    vals.append(telnyx_id)
                if country_code is not None:
                    sets.append("country_code = ?")
                    vals.append(country_code)
                if monthly_cost is not None:
                    sets.append("monthly_cost = ?")
                    vals.append(monthly_cost)
                if per_minute_rate is not None:
                    sets.append("per_minute_rate = ?")
                    vals.append(per_minute_rate)
                if sets:
                    sets.append("updated_at = ?")
                    vals.append(now)
                    vals.extend([tenant_id, existing[0]])
                    self._conn.execute(
                        f"UPDATE phone_numbers SET {', '.join(sets)} "
                        "WHERE tenant_id = ? AND id = ?",
                        tuple(vals),
                    )
        return self.get_phone_number_by_phone(tenant_id, phone_number)  # type: ignore[return-value]

    def get_phone_number(self, tenant_id: str, number_id: str) -> Optional[dict]:
        return self._row(
            "SELECT * FROM phone_numbers WHERE tenant_id = ? AND id = ?",
            (tenant_id, number_id),
        )

    def get_phone_number_by_phone(
        self, tenant_id: str, phone_number: str
    ) -> Optional[dict]:
        return self._row(
            "SELECT * FROM phone_numbers WHERE tenant_id = ? AND phone_number = ?",
            (tenant_id, phone_number),
        )

    def list_phone_numbers(self, tenant_id: str) -> list[dict]:
        return self._rows(
            "SELECT * FROM phone_numbers WHERE tenant_id = ? "
            "ORDER BY created_at DESC",
            (tenant_id,),
        )

    def find_workflow_for_number(
        self, tenant_id: str, phone_number: str
    ) -> Optional[dict]:
        """Resolve a called E.164 number to its assigned workflow, if any.

        Used by the webhook handler to kick off the DAG for inbound calls.
        """
        return self._row(
            "SELECT w.* FROM workflows w "
            "JOIN phone_numbers n ON n.tenant_id = w.tenant_id "
            "WHERE n.tenant_id = ? AND n.phone_number = ? "
            "AND n.assignment_kind = 'workflow' AND n.assignment_target = w.id",
            (tenant_id, phone_number),
        )

    def set_number_assignment(
        self,
        tenant_id: str,
        number_id: str,
        kind: Optional[str],
        target_id: Optional[str],
    ) -> Optional[dict]:
        """Set the assignment of a number. Pass kind=None to clear.

        Also appends a row to number_assignments as a historical log.
        """
        if kind is not None and kind not in ("workflow", "assistant", "direct"):
            raise ValueError(f"invalid assignment kind: {kind}")
        with self._lock:
            existing = self._row(
                "SELECT id FROM phone_numbers WHERE tenant_id = ? AND id = ?",
                (tenant_id, number_id),
            )
            if existing is None:
                return None
            self._conn.execute(
                "UPDATE phone_numbers SET assignment_kind = ?, "
                "assignment_target = ?, updated_at = ? "
                "WHERE tenant_id = ? AND id = ?",
                (kind, target_id if kind else None, _utcnow(),
                 tenant_id, number_id),
            )
            if kind:
                aid = _new_id("asgn")
                self._conn.execute(
                    "INSERT INTO number_assignments("
                    "id, tenant_id, number_id, kind, target_id, created_at"
                    ") VALUES (?,?,?,?,?,?)",
                    (aid, tenant_id, number_id, kind, target_id, _utcnow()),
                )
        return self.get_phone_number(tenant_id, number_id)

    def delete_phone_number(self, tenant_id: str, number_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM phone_numbers WHERE tenant_id = ? AND id = ?",
                (tenant_id, number_id),
            )
            return cur.rowcount > 0

    def list_number_assignments(
        self, tenant_id: str, number_id: str
    ) -> list[dict]:
        return self._rows(
            "SELECT * FROM number_assignments "
            "WHERE tenant_id = ? AND number_id = ? ORDER BY created_at DESC",
            (tenant_id, number_id),
        )

    # ─────────────────────────── assistants (#14) ────────────────────────────

    def create_assistant(
        self,
        tenant_id: str,
        name: str,
        telnyx_id: Optional[str] = None,
        voice: Optional[str] = None,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        tools: Optional[list[dict]] = None,
        greeting: Optional[str] = None,
    ) -> dict:
        aid = _new_id("ast")
        now = _utcnow()
        with self._lock:
            self.ensure_tenant(tenant_id)
            self._conn.execute(
                "INSERT INTO assistants("
                "id, tenant_id, name, telnyx_id, voice, system_prompt, "
                "model, tools_json, greeting, created_at, updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (aid, tenant_id, name, telnyx_id, voice, system_prompt,
                 model, json.dumps(tools or []), greeting, now, now),
            )
        return self.get_assistant(tenant_id, aid)  # type: ignore[return-value]

    def list_assistants(self, tenant_id: str) -> list[dict]:
        rows = self._rows(
            "SELECT * FROM assistants WHERE tenant_id = ? "
            "ORDER BY updated_at DESC",
            (tenant_id,),
        )
        for r in rows:
            r["tools"] = _safe_json_list(r.get("tools_json"))
        return rows

    def get_assistant(self, tenant_id: str, assistant_id: str) -> Optional[dict]:
        row = self._row(
            "SELECT * FROM assistants WHERE tenant_id = ? AND id = ?",
            (tenant_id, assistant_id),
        )
        if row:
            row["tools"] = _safe_json_list(row.get("tools_json"))
        return row

    def get_assistant_by_telnyx_id(
        self, tenant_id: str, telnyx_id: str
    ) -> Optional[dict]:
        row = self._row(
            "SELECT * FROM assistants WHERE tenant_id = ? AND telnyx_id = ?",
            (tenant_id, telnyx_id),
        )
        if row:
            row["tools"] = _safe_json_list(row.get("tools_json"))
        return row

    def update_assistant(
        self,
        tenant_id: str,
        assistant_id: str,
        *,
        name: Optional[str] = None,
        telnyx_id: Optional[str] = None,
        voice: Optional[str] = None,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        tools: Optional[list[dict]] = None,
        greeting: Optional[str] = None,
    ) -> Optional[dict]:
        existing = self.get_assistant(tenant_id, assistant_id)
        if not existing:
            return None
        sets: list[str] = []
        vals: list[Any] = []
        for col, val in (
            ("name", name), ("telnyx_id", telnyx_id), ("voice", voice),
            ("system_prompt", system_prompt), ("model", model),
            ("greeting", greeting),
        ):
            if val is not None:
                sets.append(f"{col} = ?")
                vals.append(val)
        if tools is not None:
            sets.append("tools_json = ?")
            vals.append(json.dumps(tools))
        if not sets:
            return existing
        sets.append("updated_at = ?")
        vals.append(_utcnow())
        vals.extend([tenant_id, assistant_id])
        with self._lock:
            self._conn.execute(
                f"UPDATE assistants SET {', '.join(sets)} "
                "WHERE tenant_id = ? AND id = ?",
                tuple(vals),
            )
        return self.get_assistant(tenant_id, assistant_id)

    def delete_assistant(self, tenant_id: str, assistant_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM assistants WHERE tenant_id = ? AND id = ?",
                (tenant_id, assistant_id),
            )
            return cur.rowcount > 0

    def append_assistant_log(
        self,
        tenant_id: str,
        assistant_id: str,
        role: str,
        content: Optional[str] = None,
        *,
        call_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        tool_args: Optional[dict] = None,
    ) -> dict:
        """Append a row to assistant_call_log (live transcript + tool calls)."""
        lid = _new_id("alg")
        with self._lock:
            self._conn.execute(
                "INSERT INTO assistant_call_log("
                "id, tenant_id, assistant_id, call_id, role, content, "
                "tool_name, tool_args, created_at"
                ") VALUES (?,?,?,?,?,?,?,?,?)",
                (lid, tenant_id, assistant_id, call_id, role, content,
                 tool_name,
                 json.dumps(tool_args) if tool_args is not None else None,
                 _utcnow()),
            )
        return {"id": lid, "tenant_id": tenant_id, "assistant_id": assistant_id,
                "call_id": call_id, "role": role, "content": content,
                "tool_name": tool_name, "tool_args": tool_args}

    def list_assistant_call_log(
        self, tenant_id: str, assistant_id: str, limit: int = 100
    ) -> list[dict]:
        rows = self._rows(
            "SELECT * FROM assistant_call_log "
            "WHERE tenant_id = ? AND assistant_id = ? "
            "ORDER BY created_at ASC LIMIT ?",
            (tenant_id, assistant_id, int(limit)),
        )
        for r in rows:
            if r.get("tool_args"):
                try:
                    r["tool_args"] = json.loads(r["tool_args"])
                except json.JSONDecodeError:
                    r["tool_args"] = None
        return rows

    # ──────────────────── analytics rollup (#16) ───────────────────────────
    # Aggregations happen on the deliveries table on the fly; the rollup
    # table is just a flat snapshot the dashboard reads in O(1) rows.
    # The cron job (see ``backend/analytics/aggregator.py``) refreshes
    # ``day = yesterday`` every night.

    def get_rollup_window(
        self, tenant_id: str, day_from: str, day_to: str
    ) -> list[dict]:
        return self._rows(
            "SELECT day, calls_in, calls_out, sms_in, sms_out, spend_cents "
            "FROM analytics_rollup_daily "
            "WHERE tenant_id = ? AND day BETWEEN ? AND ? "
            "ORDER BY day ASC",
            (tenant_id, day_from, day_to),
        )

    def upsert_rollup_day(
        self,
        tenant_id: str,
        day: str,
        calls_in: int = 0,
        calls_out: int = 0,
        sms_in: int = 0,
        sms_out: int = 0,
        spend_cents: int = 0,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO analytics_rollup_daily("
                "tenant_id, day, calls_in, calls_out, sms_in, sms_out, spend_cents"
                ") VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(tenant_id, day) DO UPDATE SET "
                "calls_in=excluded.calls_in, calls_out=excluded.calls_out, "
                "sms_in=excluded.sms_in, sms_out=excluded.sms_out, "
                "spend_cents=excluded.spend_cents",
                (tenant_id, day, calls_in, calls_out, sms_in, sms_out, spend_cents),
            )

    def count_deliveries_in_window(
        self,
        tenant_id: str,
        day_from: str,
        day_to: str,
    ) -> dict:
        """Live count of deliveries grouped by (kind_bucket, direction) for
        a window. Used by the analytics screen when the rollup is empty
        (e.g. just-initialised DB, no cron has run yet).

        The ``kind`` column on ``deliveries`` is freeform; we bucket:
        - calls: kind in ('outbound_call','inbound_call','call') — counted
          by status (anything not 'failed' counts as a call placed)
        - sms:   kind in ('sms','outbound_sms','inbound_sms') — counted
          regardless of status
        """
        sql = (
            "SELECT "
            "  SUM(CASE WHEN (kind LIKE '%call%' OR kind = 'call') THEN 1 ELSE 0 END) AS calls_total, "
            "  SUM(CASE WHEN (kind LIKE 'inbound%' OR kind = 'inbound_call') AND (kind LIKE '%call%' OR kind = 'call') THEN 1 ELSE 0 END) AS calls_in, "
            "  SUM(CASE WHEN (kind LIKE 'outbound%' OR kind = 'outbound_call') AND (kind LIKE '%call%' OR kind = 'call') THEN 1 ELSE 0 END) AS calls_out, "
            "  SUM(CASE WHEN kind LIKE '%sms%' OR kind = 'sms' THEN 1 ELSE 0 END) AS sms_total, "
            "  SUM(CASE WHEN kind LIKE 'inbound%' AND (kind LIKE '%sms%' OR kind = 'sms') THEN 1 ELSE 0 END) AS sms_in, "
            "  SUM(CASE WHEN kind LIKE 'outbound%' AND (kind LIKE '%sms%' OR kind = 'sms') THEN 1 ELSE 0 END) AS sms_out "
            "FROM deliveries "
            "WHERE tenant_id = ? AND substr(created_at, 1, 10) BETWEEN ? AND ?"
        )
        row = self._row(sql, (tenant_id, day_from, day_to)) or {}
        return {
            "calls_total": int(row.get("calls_total") or 0),
            "calls_in": int(row.get("calls_in") or 0),
            "calls_out": int(row.get("calls_out") or 0),
            "sms_total": int(row.get("sms_total") or 0),
            "sms_in": int(row.get("sms_in") or 0),
            "sms_out": int(row.get("sms_out") or 0),
        }

    # ──────────────────── subscriptions + usage (#19) ──────────────────────

    def get_subscription(self, tenant_id: str) -> Optional[dict]:
        return self._row(
            "SELECT * FROM subscriptions WHERE tenant_id = ?",
            (tenant_id,),
        )

    def upsert_subscription(
        self,
        tenant_id: str,
        plan: str,
        status: str = "active",
        stripe_customer_id: Optional[str] = None,
        stripe_subscription_id: Optional[str] = None,
        current_period_start: Optional[str] = None,
        current_period_end: Optional[str] = None,
        cancel_at_period_end: int = 0,
    ) -> dict:
        now = _utcnow()
        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM subscriptions WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
            if existing is None:
                sid = _new_id("sub")
                self._conn.execute(
                    "INSERT INTO subscriptions("
                    "id, tenant_id, plan, status, stripe_customer_id, "
                    "stripe_subscription_id, current_period_start, "
                    "current_period_end, cancel_at_period_end, "
                    "created_at, updated_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, tenant_id, plan, status, stripe_customer_id,
                     stripe_subscription_id, current_period_start,
                     current_period_end, int(cancel_at_period_end), now, now),
                )
            else:
                self._conn.execute(
                    "UPDATE subscriptions SET "
                    "plan=?, status=?, stripe_customer_id=COALESCE(?, stripe_customer_id), "
                    "stripe_subscription_id=COALESCE(?, stripe_subscription_id), "
                    "current_period_start=COALESCE(?, current_period_start), "
                    "current_period_end=COALESCE(?, current_period_end), "
                    "cancel_at_period_end=?, updated_at=? "
                    "WHERE tenant_id=?",
                    (plan, status, stripe_customer_id, stripe_subscription_id,
                     current_period_start, current_period_end,
                     int(cancel_at_period_end), now, tenant_id),
                )
        sub = self.get_subscription(tenant_id)
        assert sub is not None
        return sub

    def record_usage(
        self,
        tenant_id: str,
        kind: str,
        quantity: int,
        period_start: str,
        period_end: str,
    ) -> dict:
        if kind not in ("voice_minutes", "sms_segments", "numbers"):
            raise ValueError(f"invalid usage kind: {kind}")
        uid = _new_id("usg")
        with self._lock:
            self._conn.execute(
                "INSERT INTO usage_records("
                "id, tenant_id, kind, quantity, period_start, period_end, "
                "billed, created_at"
                ") VALUES (?,?,?,?,?,?,0,?)",
                (uid, tenant_id, kind, int(quantity),
                 period_start, period_end, _utcnow()),
            )
        return {"id": uid, "tenant_id": tenant_id, "kind": kind,
                "quantity": quantity, "period_start": period_start,
                "period_end": period_end}

    def sum_usage_in_period(
        self, tenant_id: str, kind: str, period_start: str, period_end: str
    ) -> int:
        row = self._row(
            "SELECT COALESCE(SUM(quantity), 0) AS total "
            "FROM usage_records "
            "WHERE tenant_id = ? AND kind = ? "
            "AND period_start = ? AND period_end = ?",
            (tenant_id, kind, period_start, period_end),
        )
        return int((row or {}).get("total") or 0)

    # ─────────────────────── audit log (#20) ───────────────────────────────

    def append_audit(self, entry: dict) -> str:
        """Append one row to audit_log. Triggers forbid UPDATE/DELETE.

        The caller (middleware) passes a fully-prepared dict so we can
        keep the SQL minimal. The id is generated here.
        """
        aid = _new_id("aud")
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit_log("
                "id, tenant_id, user_id, action, target, ip, user_agent, "
                "request_id, method, path, response_status, response_time_ms, "
                "request_body, response_body, timestamp"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    aid,
                    entry.get("tenant_id") or "default",
                    entry.get("user_id"),
                    entry.get("action") or "unknown",
                    entry.get("target"),
                    entry.get("ip"),
                    entry.get("user_agent"),
                    entry.get("request_id"),
                    entry.get("method"),
                    entry.get("path"),
                    entry.get("response_status"),
                    entry.get("response_time_ms"),
                    entry.get("request_body"),
                    entry.get("response_body"),
                    entry.get("timestamp") or _utcnow(),
                ),
            )
        return aid

    def list_audit(
        self,
        tenant_id: str,
        user_id: Optional[str] = None,
        action: Optional[str] = None,
        day_from: Optional[str] = None,
        day_to: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        where = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if user_id:
            where.append("user_id = ?")
            params.append(user_id)
        if action:
            where.append("action = ?")
            params.append(action)
        if day_from:
            where.append("substr(timestamp, 1, 10) >= ?")
            params.append(day_from)
        if day_to:
            where.append("substr(timestamp, 1, 10) <= ?")
            params.append(day_to)
        params.extend([int(limit), int(offset)])
        sql = (
            f"SELECT id, tenant_id, user_id, action, target, ip, user_agent, "
            f"request_id, method, path, response_status, response_time_ms, "
            f"timestamp FROM audit_log "
            f"WHERE {' AND '.join(where)} "
            f"ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        )
        return self._rows(sql, tuple(params))

    def get_audit(self, tenant_id: str, audit_id: str) -> Optional[dict]:
        return self._row(
            "SELECT * FROM audit_log WHERE tenant_id = ? AND id = ?",
            (tenant_id, audit_id),
        )

    def archive_audit_before(self, cutoff_iso: str) -> int:
        """Move audit rows older than ``cutoff_iso`` into audit_log_archive.
        Triggers block the DELETE; we copy + DELETE in a single transaction.
        Returns the number of rows archived.

        ``zstd`` is optional — if installed, request/response bodies are
        compressed before write. Otherwise the bodies are moved as plain
        text. Either way, the response is identical.
        """
        zstd = None
        try:
            import zstandard as zstd  # type: ignore
        except Exception:
            zstd = None
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, tenant_id, user_id, action, target, ip, user_agent, "
                "request_id, method, path, response_status, response_time_ms, "
                "request_body, response_body, timestamp "
                "FROM audit_log WHERE timestamp < ?",
                (cutoff_iso,),
            ).fetchall()
            if not rows:
                return 0
            archived_at = _utcnow()
            for r in rows:
                req_body = r[12]
                res_body = r[13]
                if zstd is not None:
                    try:
                        cctx = zstd.ZstdCompressor()
                        if req_body is not None:
                            req_body = cctx.compress(req_body.encode("utf-8")).decode("ascii")
                        if res_body is not None:
                            res_body = cctx.compress(res_body.encode("utf-8")).decode("ascii")
                    except Exception:
                        pass
                self._conn.execute(
                    "INSERT OR REPLACE INTO audit_log_archive("
                    "id, tenant_id, user_id, action, target, ip, user_agent, "
                    "request_id, method, path, response_status, response_time_ms, "
                    "request_body, response_body, timestamp, archived_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8],
                     r[9], r[10], r[11], req_body, res_body, r[14], archived_at),
                )
            # Now drop the originals. The DELETE trigger RAISE(ABORT)... but
            # the archive_audit_before path is the one allowed escape hatch
            # so we disable the trigger for this statement. Pragmatic, not
            # pure; the alternative is to use a separate audit_log_active
            # view, but the v1 simplicity wins.
            try:
                self._conn.execute("DROP TRIGGER IF EXISTS audit_log_no_delete")
                cur = self._conn.execute(
                    "DELETE FROM audit_log WHERE timestamp < ?",
                    (cutoff_iso,),
                )
                deleted = cur.rowcount
                # Recreate the trigger for normal operation.
                self._conn.execute(
                    "CREATE TRIGGER IF NOT EXISTS audit_log_no_delete "
                    "BEFORE DELETE ON audit_log "
                    "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only; "
                    "use audit_log_archive instead'); END"
                )
            except Exception as e:
                # Make sure the trigger comes back even if something failed.
                self._conn.execute(
                    "CREATE TRIGGER IF NOT EXISTS audit_log_no_delete "
                    "BEFORE DELETE ON audit_log "
                    "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only; "
                    "use audit_log_archive instead'); END"
                )
                raise
            return int(deleted)

    # ─────────────────────────── campaign handoffs (#17) ───────────────────

    def record_campaign_handoff(
        self,
        tenant_id: str,
        campaign_id: str,
        mode: str,
        *,
        contact_id: Optional[str] = None,
        call_id: Optional[str] = None,
        human_agent_id: Optional[str] = None,
    ) -> dict:
        hid = _new_id("ho")
        with self._lock:
            self._conn.execute(
                "INSERT INTO campaign_handoffs("
                "id, tenant_id, campaign_id, contact_id, call_id, mode, "
                "human_agent_id, transferred_at"
                ") VALUES (?,?,?,?,?,?,?,?)",
                (hid, tenant_id, campaign_id, contact_id, call_id, mode,
                 human_agent_id, _utcnow()),
            )
        return {"id": hid, "tenant_id": tenant_id, "campaign_id": campaign_id,
                "contact_id": contact_id, "call_id": call_id, "mode": mode,
                "human_agent_id": human_agent_id}

    def list_campaign_handoffs(
        self, tenant_id: str, campaign_id: str
    ) -> list[dict]:
        return self._rows(
            "SELECT * FROM campaign_handoffs "
            "WHERE tenant_id = ? AND campaign_id = ? ORDER BY transferred_at DESC",
            (tenant_id, campaign_id),
        )
    # ───────────────────── #22 WhatsApp ──────────────────────────────
    def upsert_whatsapp_template(
        self, tenant_id: str, telnyx_id: str, name: str,
        language: str = "en", variables: Optional[list] = None,
        status: str = "approved",
    ) -> dict:
        now = _utcnow()
        variables = variables or []
        existing = self._row(
            "SELECT id FROM whatsapp_templates WHERE tenant_id = ? AND name = ?",
            (tenant_id, name),
        )
        if existing:
            self._exec(
                "UPDATE whatsapp_templates SET telnyx_id=?, language=?, variables=?, "
                "status=?, updated_at=? WHERE id=?",
                (telnyx_id, language, json.dumps(variables), status, now, existing["id"]),
            )
            return self.get_whatsapp_template(tenant_id, existing["id"]) or {}
        tid = _new_id("wapl")
        self._exec(
            "INSERT INTO whatsapp_templates(id, tenant_id, telnyx_id, name, language, "
            "variables, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (tid, tenant_id, telnyx_id, name, language, json.dumps(variables),
             status, now, now),
        )
        return self.get_whatsapp_template(tenant_id, tid) or {}

    def get_whatsapp_template(self, tenant_id: str, template_id: str) -> Optional[dict]:
        row = self._row(
            "SELECT * FROM whatsapp_templates WHERE tenant_id = ? AND id = ?",
            (tenant_id, template_id),
        )
        if row and "variables" in row:
            try:
                row["variables"] = json.loads(row["variables"]) if row["variables"] else []
            except (json.JSONDecodeError, TypeError):
                row["variables"] = []
        return row

    def get_whatsapp_template_by_name(self, tenant_id: str, name: str) -> Optional[dict]:
        row = self._row(
            "SELECT * FROM whatsapp_templates WHERE tenant_id = ? AND name = ?",
            (tenant_id, name),
        )
        if row and "variables" in row:
            try:
                row["variables"] = json.loads(row["variables"]) if row["variables"] else []
            except (json.JSONDecodeError, TypeError):
                row["variables"] = []
        return row

    def list_whatsapp_templates(self, tenant_id: str) -> list[dict]:
        rows = self._rows(
            "SELECT * FROM whatsapp_templates WHERE tenant_id = ? ORDER BY name",
            (tenant_id,),
        )
        for r in rows:
            try:
                r["variables"] = json.loads(r["variables"]) if r["variables"] else []
            except (json.JSONDecodeError, TypeError):
                r["variables"] = []
        return rows

    def insert_whatsapp_message(
        self, tenant_id: str, direction: str, remote: str,
        from_number: str, to_number: str, body: str = "",
        telnyx_id: Optional[str] = None, status: str = "received",
    ) -> dict:
        mid = _new_id("wamsg")
        now = _utcnow()
        self._exec(
            "INSERT INTO whatsapp_messages(id, tenant_id, direction, remote, from_number, "
            "to_number, body, telnyx_id, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (mid, tenant_id, direction, remote, from_number, to_number, body,
             telnyx_id, status, now),
        )
        return {"id": mid, "tenant_id": tenant_id, "direction": direction,
                "remote": remote, "from_number": from_number, "to_number": to_number,
                "body": body, "telnyx_id": telnyx_id, "status": status, "created_at": now}

    def list_whatsapp_threads(self, tenant_id: str) -> list[dict]:
        return self._rows(
            "SELECT remote, MAX(created_at) AS last_at, "
            "  (SELECT body FROM whatsapp_messages w2 "
            "   WHERE w2.tenant_id = ? AND w2.remote = m.remote "
            "   ORDER BY w2.created_at DESC LIMIT 1) AS last_body "
            "FROM whatsapp_messages m WHERE tenant_id = ? "
            "GROUP BY remote ORDER BY last_at DESC",
            (tenant_id, tenant_id),
        )

    # ───────────────────── #23 Suppression + SMS replies ────────────
    def add_suppression(
        self, tenant_id: str, phone: str, reason: str = "manual",
        source: str = "manual", note: Optional[str] = None,
    ) -> dict:
        now = _utcnow()
        sid = _new_id("supp")
        try:
            self._exec(
                "INSERT INTO suppression_list(id, tenant_id, phone, reason, source, note, "
                "created_at) VALUES (?,?,?,?,?,?,?)",
                (sid, tenant_id, phone, reason, source, note, now),
            )
        except sqlite3.IntegrityError:
            self._exec(
                "UPDATE suppression_list SET reason=?, source=?, note=? "
                "WHERE tenant_id=? AND phone=?",
                (reason, source, note, tenant_id, phone),
            )
            row = self._row(
                "SELECT * FROM suppression_list WHERE tenant_id = ? AND phone = ?",
                (tenant_id, phone),
            )
            return row or {"id": sid, "tenant_id": tenant_id, "phone": phone}
        return {"id": sid, "tenant_id": tenant_id, "phone": phone,
                "reason": reason, "source": source, "note": note, "created_at": now}

    def remove_suppression(self, tenant_id: str, phone: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM suppression_list WHERE tenant_id = ? AND phone = ?",
            (tenant_id, phone),
        )
        return cur.rowcount > 0

    def is_suppressed(self, tenant_id: str, phone: str) -> bool:
        row = self._row(
            "SELECT 1 AS hit FROM suppression_list WHERE tenant_id = ? AND phone = ?",
            (tenant_id, phone),
        )
        return bool(row)

    def list_suppression(
        self, tenant_id: str, reason: Optional[str] = None,
        limit: int = 100, offset: int = 0,
    ) -> list[dict]:
        if reason:
            return self._rows(
                "SELECT * FROM suppression_list WHERE tenant_id = ? AND reason = ? "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (tenant_id, reason, limit, offset),
            )
        return self._rows(
            "SELECT * FROM suppression_list WHERE tenant_id = ? "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (tenant_id, limit, offset),
        )

    def count_suppression(self, tenant_id: str, reason: Optional[str] = None) -> int:
        if reason:
            row = self._row(
                "SELECT COUNT(*) AS n FROM suppression_list WHERE tenant_id = ? AND reason = ?",
                (tenant_id, reason),
            )
        else:
            row = self._row(
                "SELECT COUNT(*) AS n FROM suppression_list WHERE tenant_id = ?",
                (tenant_id,),
            )
        return int(row["n"]) if row else 0

    def insert_sms_reply(
        self, tenant_id: str, from_number: str, body: str,
        campaign_id: Optional[str] = None, contact_id: Optional[str] = None,
    ) -> dict:
        rid = _new_id("smsr")
        now = _utcnow()
        self._exec(
            "INSERT INTO sms_replies(id, tenant_id, campaign_id, contact_id, from_number, "
            "body, received_at) VALUES (?,?,?,?,?,?,?)",
            (rid, tenant_id, campaign_id, contact_id, from_number, body, now),
        )
        return {"id": rid, "tenant_id": tenant_id, "campaign_id": campaign_id,
                "contact_id": contact_id, "from_number": from_number, "body": body,
                "received_at": now}

    def list_sms_replies(
        self, tenant_id: str, campaign_id: Optional[str] = None,
    ) -> list[dict]:
        if campaign_id:
            return self._rows(
                "SELECT * FROM sms_replies WHERE tenant_id = ? AND campaign_id = ? "
                "ORDER BY received_at DESC",
                (tenant_id, campaign_id),
            )
        return self._rows(
            "SELECT * FROM sms_replies WHERE tenant_id = ? ORDER BY received_at DESC",
            (tenant_id,),
        )

    def find_active_campaign_for_inbound(
        self, tenant_id: str, from_number: str,
    ) -> Optional[dict]:
        contact = self._row(
            "SELECT id FROM contacts WHERE tenant_id = ? AND phone = ?",
            (tenant_id, from_number),
        )
        if not contact:
            return None
        rows = self._rows(
            "SELECT id, name, contact_ids FROM campaigns "
            "WHERE tenant_id = ? AND status = 'running'",
            (tenant_id,),
        )
        for c in rows:
            try:
                cids = json.loads(c.get("contact_ids") or "[]")
            except (json.JSONDecodeError, TypeError):
                cids = []
            if contact["id"] in cids:
                return {"id": c["id"], "name": c.get("name", "")}
        return None

    def upsert_phone_number(
        self, tenant_id: str, phone: str, telnyx_id: Optional[str] = None,
        country_code: Optional[str] = None, whatsapp_enabled: bool = False,
    ) -> dict:
        now = _utcnow()
        existing = self._row(
            "SELECT * FROM phone_numbers WHERE tenant_id = ? AND phone = ?",
            (tenant_id, phone),
        )
        if existing:
            updates = []
            params: list = []
            if telnyx_id is not None:
                updates.append("telnyx_id = ?")
                params.append(telnyx_id)
            if country_code is not None:
                updates.append("country_code = ?")
                params.append(country_code)
            if updates:
                params.extend([tenant_id, phone])
                self._exec(
                    f"UPDATE phone_numbers SET {', '.join(updates)} "
                    f"WHERE tenant_id = ? AND phone = ?",
                    tuple(params),
                )
            return self._row(
                "SELECT * FROM phone_numbers WHERE tenant_id = ? AND phone = ?",
                (tenant_id, phone),
            ) or {}
        nid = _new_id("pn")
        self._exec(
            "INSERT INTO phone_numbers(id, tenant_id, phone, telnyx_id, country_code, "
            "whatsapp_enabled, created_at) VALUES (?,?,?,?,?,?,?)",
            (nid, tenant_id, phone, telnyx_id, country_code,
             1 if whatsapp_enabled else 0, now),
        )
        return {"id": nid, "tenant_id": tenant_id, "phone": phone,
                "telnyx_id": telnyx_id, "country_code": country_code,
                "whatsapp_enabled": 1 if whatsapp_enabled else 0, "created_at": now}

    def create_contact(
        self, tenant_id: str, name: str, phone: str,
        email: Optional[str] = None, tags: Optional[list] = None,
    ) -> dict:
        cid = _new_id("c")
        now = _utcnow()
        self._exec(
            "INSERT INTO contacts(id, tenant_id, name, phone, email, tags, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (cid, tenant_id, name, phone, email,
             json.dumps(tags or []), now),
        )
        return {"id": cid, "tenant_id": tenant_id, "name": name, "phone": phone,
                "email": email, "tags": tags or [], "created_at": now}

    def get_contact_by_phone(self, tenant_id: str, phone: str) -> Optional[dict]:
        return self._row(
            "SELECT * FROM contacts WHERE tenant_id = ? AND phone = ?",
            (tenant_id, phone),
        )

    # ───────────────────── #27 Email ─────────────────────────────────
    def create_email_template(
        self, tenant_id: str, name: str, subject_template: str,
        body_template: str, variables: Optional[list] = None,
    ) -> dict:
        now = _utcnow()
        tid = _new_id("emtpl")
        self._exec(
            "INSERT INTO email_templates(id, tenant_id, name, subject_template, "
            "body_template, variables, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (tid, tenant_id, name, subject_template, body_template,
             json.dumps(variables or []), now, now),
        )
        return {"id": tid, "tenant_id": tenant_id, "name": name,
                "subject_template": subject_template, "body_template": body_template,
                "variables": variables or [], "created_at": now, "updated_at": now}

    def list_email_templates(self, tenant_id: str) -> list[dict]:
        rows = self._rows(
            "SELECT * FROM email_templates WHERE tenant_id = ? ORDER BY name",
            (tenant_id,),
        )
        for r in rows:
            try:
                r["variables"] = json.loads(r["variables"]) if r["variables"] else []
            except (json.JSONDecodeError, TypeError):
                r["variables"] = []
        return rows

    def get_email_template(self, tenant_id: str, template_id: str) -> Optional[dict]:
        row = self._row(
            "SELECT * FROM email_templates WHERE tenant_id = ? AND id = ?",
            (tenant_id, template_id),
        )
        if row and "variables" in row:
            try:
                row["variables"] = json.loads(row["variables"]) if row["variables"] else []
            except (json.JSONDecodeError, TypeError):
                row["variables"] = []
        return row

    def delete_email_template(self, tenant_id: str, template_id: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM email_templates WHERE tenant_id = ? AND id = ?",
            (tenant_id, template_id),
        )
        return cur.rowcount > 0

    def insert_email_message(
        self, tenant_id: str, provider: str, direction: str,
        from_addr: str, to_addr: str, subject: str = "", body: str = "",
        html: Optional[str] = None, status: str = "queued",
        sent_at: Optional[str] = None, error: Optional[str] = None,
    ) -> dict:
        mid = _new_id("em")
        now = _utcnow()
        ts = sent_at or now
        self._exec(
            "INSERT INTO email_messages(id, tenant_id, provider, direction, from_addr, "
            "to_addr, subject, body, html, status, error, sent_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, tenant_id, provider, direction, from_addr, to_addr, subject, body,
             html, status, error, ts, now),
        )
        return {"id": mid, "tenant_id": tenant_id, "provider": provider,
                "direction": direction, "from_addr": from_addr, "to_addr": to_addr,
                "subject": subject, "body": body, "html": html, "status": status,
                "error": error, "sent_at": ts, "created_at": now}

    def update_email_message_status(
        self, tenant_id: str, email_id: str, status: str, error: Optional[str] = None,
    ) -> None:
        if error is not None:
            self._exec(
                "UPDATE email_messages SET status=?, error=? WHERE tenant_id=? AND id=?",
                (status, error, tenant_id, email_id),
            )
        else:
            self._exec(
                "UPDATE email_messages SET status=? WHERE tenant_id=? AND id=?",
                (status, tenant_id, email_id),
            )

    def get_email_message(self, tenant_id: str, email_id: str) -> Optional[dict]:
        return self._row(
            "SELECT * FROM email_messages WHERE tenant_id = ? AND id = ?",
            (tenant_id, email_id),
        )

    def list_email_messages(
        self, tenant_id: str, to_addr: Optional[str] = None,
        from_addr: Optional[str] = None, limit: int = 50, offset: int = 0,
    ) -> list[dict]:
        where = ["tenant_id = ?"]
        params: list = [tenant_id]
        if to_addr:
            where.append("to_addr = ?")
            params.append(to_addr)
        if from_addr:
            where.append("from_addr = ?")
            params.append(from_addr)
        params.extend([limit, offset])
        return self._rows(
            f"SELECT * FROM email_messages WHERE {' AND '.join(where)} "
            f"ORDER BY sent_at DESC LIMIT ? OFFSET ?",
            tuple(params),
        )

    # ───────────────────── #26 Meetings ──────────────────────────────
    def create_meeting(
        self, tenant_id: str, title: str = "", host_user_id: Optional[str] = None,
        room_url: str = "", room_name: str = "",
    ) -> dict:
        mid = _new_id("mtg")
        now = _utcnow()
        self._exec(
            "INSERT INTO meetings(id, tenant_id, host_user_id, title, room_url, "
            "room_name, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (mid, tenant_id, host_user_id, title, room_url, room_name, "created", now),
        )
        return {"id": mid, "tenant_id": tenant_id, "host_user_id": host_user_id,
                "title": title, "room_url": room_url, "room_name": room_name,
                "status": "created", "created_at": now,
                "started_at": None, "ended_at": None, "recording_url": None,
                "participants": []}

    def get_meeting(self, tenant_id: str, meeting_id: str) -> Optional[dict]:
        row = self._row(
            "SELECT * FROM meetings WHERE tenant_id = ? AND id = ?",
            (tenant_id, meeting_id),
        )
        if row and "participants" in row:
            try:
                row["participants"] = json.loads(row["participants"]) if row["participants"] else []
            except (json.JSONDecodeError, TypeError):
                row["participants"] = []
        return row

    def list_meetings(self, tenant_id: str, limit: int = 50, offset: int = 0) -> list[dict]:
        rows = self._rows(
            "SELECT * FROM meetings WHERE tenant_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (tenant_id, limit, offset),
        )
        for r in rows:
            try:
                r["participants"] = json.loads(r["participants"]) if r["participants"] else []
            except (json.JSONDecodeError, TypeError):
                r["participants"] = []
        return rows

    def update_meeting_participants(
        self, tenant_id: str, meeting_id: str, participants: list,
    ) -> None:
        self._exec(
            "UPDATE meetings SET participants=? WHERE tenant_id=? AND id=?",
            (json.dumps(participants), tenant_id, meeting_id),
        )

    def update_meeting_started(self, tenant_id: str, meeting_id: str) -> None:
        now = _utcnow()
        self._exec(
            "UPDATE meetings SET status=?, started_at=? WHERE tenant_id=? AND id=?",
            ("active", now, tenant_id, meeting_id),
        )

    def update_meeting_ended(self, tenant_id: str, meeting_id: str) -> None:
        now = _utcnow()
        self._exec(
            "UPDATE meetings SET status=?, ended_at=? WHERE tenant_id=? AND id=?",
            ("ended", now, tenant_id, meeting_id),
        )

    def update_meeting_recording(self, tenant_id: str, meeting_id: str, url: str) -> None:
        self._exec(
            "UPDATE meetings SET recording_url=? WHERE tenant_id=? AND id=?",
            (url, tenant_id, meeting_id),
        )

    def append_meeting_participant(self, tenant_id: str, meeting_id: str, name: str) -> None:
        row = self._row(
            "SELECT participants FROM meetings WHERE tenant_id = ? AND id = ?",
            (tenant_id, meeting_id),
        )
        if not row:
            return
        try:
            ppl = json.loads(row["participants"]) if row["participants"] else []
        except (json.JSONDecodeError, TypeError):
            ppl = []
        if name not in ppl:
            ppl.append(name)
        self._exec(
            "UPDATE meetings SET participants=? WHERE tenant_id=? AND id=?",
            (json.dumps(ppl), tenant_id, meeting_id),
        )

    def remove_meeting_participant(self, tenant_id: str, meeting_id: str, name: str) -> None:
        row = self._row(
            "SELECT participants FROM meetings WHERE tenant_id = ? AND id = ?",
            (tenant_id, meeting_id),
        )
        if not row:
            return
        try:
            ppl = json.loads(row["participants"]) if row["participants"] else []
        except (json.JSONDecodeError, TypeError):
            ppl = []
        if name in ppl:
            ppl.remove(name)
        self._exec(
            "UPDATE meetings SET participants=? WHERE tenant_id=? AND id=?",
            (json.dumps(ppl), tenant_id, meeting_id),
        )

    def delete_meeting(self, tenant_id: str, meeting_id: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM meetings WHERE tenant_id = ? AND id = ?",
            (tenant_id, meeting_id),
        )
        return cur.rowcount > 0

    # ───────────────────── #28 Network quality ───────────────────────
    def insert_network_quality(
        self, tenant_id: str, call_id: Optional[str],
        rtt_ms: float, jitter_ms: float, packet_loss_pct: float, score: float,
    ) -> dict:
        nid = _new_id("nq")
        now = _utcnow()
        self._exec(
            "INSERT INTO network_quality_log(id, tenant_id, call_id, rtt_ms, jitter_ms, "
            "packet_loss_pct, score, timestamp) VALUES (?,?,?,?,?,?,?,?)",
            (nid, tenant_id, call_id, rtt_ms, jitter_ms, packet_loss_pct, score, now),
        )
        return {"id": nid, "tenant_id": tenant_id, "call_id": call_id,
                "rtt_ms": rtt_ms, "jitter_ms": jitter_ms, "packet_loss_pct": packet_loss_pct,
                "score": score, "timestamp": now}

    def list_network_quality(
        self, tenant_id: str, from_ts: Optional[str] = None,
        to_ts: Optional[str] = None, limit: int = 200,
    ) -> list[dict]:
        where = ["tenant_id = ?"]
        params: list = [tenant_id]
        if from_ts:
            where.append("timestamp >= ?")
            params.append(from_ts)
        if to_ts:
            where.append("timestamp <= ?")
            params.append(to_ts)
        params.append(limit)
        return self._rows(
            f"SELECT * FROM network_quality_log WHERE {' AND '.join(where)} "
            f"ORDER BY timestamp DESC LIMIT ?",
            tuple(params),
        )

    def network_quality_summary(self, tenant_id: str) -> dict:
        row = self._row(
            "SELECT AVG(rtt_ms) AS avg_rtt, AVG(jitter_ms) AS avg_jitter, "
            "AVG(packet_loss_pct) AS avg_loss, AVG(score) AS avg_score, "
            "COUNT(*) AS n FROM network_quality_log WHERE tenant_id = ?",
            (tenant_id,),
        )
        if not row or not row.get("n"):
            return {"n": 0, "avg_rtt": 0, "avg_jitter": 0,
                    "avg_loss": 0, "avg_score": 100}
        return {
            "n": int(row["n"]),
            "avg_rtt": float(row["avg_rtt"] or 0),
            "avg_jitter": float(row["avg_jitter"] or 0),
            "avg_loss": float(row["avg_loss"] or 0),
            "avg_score": float(row["avg_score"] or 0),
        }

    # ───────────────────── #29 Multi-region ──────────────────────────
    def update_tenant_region(
        self, tenant_id: str, region: str, region_lock: bool = False,
    ) -> None:
        self._exec(
            "UPDATE tenants SET region = ?, region_lock = ? WHERE id = ?",
            (region, 1 if region_lock else 0, tenant_id),
        )

    def list_tenants_by_region(self, region: str) -> list[dict]:
        return self._rows(
            "SELECT * FROM tenants WHERE region = ? ORDER BY id",
            (region,),
        )

    def find_tenant_by_custom_domain(self, domain: str) -> Optional[dict]:
        return self._row(
            "SELECT * FROM tenants WHERE custom_domain = ?",
            (domain,),
        )

    # ───────────────────── #30 White-label branding ──────────────────
    def get_tenant_brand(self, tenant_id: str) -> dict:
        row = self._row(
            "SELECT brand_json FROM tenants WHERE id = ?",
            (tenant_id,),
        )
        if not row or not row.get("brand_json"):
            return {}
        try:
            return json.loads(row["brand_json"])
        except (json.JSONDecodeError, TypeError):
            return {}

    def update_tenant_brand(self, tenant_id: str, brand: dict) -> None:
        self._exec(
            "UPDATE tenants SET brand_json = ? WHERE id = ?",
            (json.dumps(brand), tenant_id),
        )

    def update_tenant_custom_domain(self, tenant_id: str, domain: str) -> None:
        self._exec(
            "UPDATE tenants SET custom_domain = ? WHERE id = ?",
            (domain, tenant_id),
        )

    # ───────────────────── users listing (#31) ─────────────────────
    def list_users(self, tenant_id: str) -> list[dict]:
        """Return every user for the tenant. Safe to call on the request
        thread — reads are lockless. Joins nothing; presence + skills
        are fetched by the API layer (issue #31)."""
        return self._rows(
            "SELECT id, tenant_id, email, role, assigned_number, display_name, "
            "created_at FROM users WHERE tenant_id = ? ORDER BY created_at",
            (tenant_id,),
        )

    def update_user_agent(
        self,
        tenant_id: str,
        user_id: str,
        *,
        assigned_number: Optional[str] = None,
        display_name: Optional[str] = None,
    ) -> Optional[dict]:
        """Set the agent-direct DID and/or display name. Both arguments
        are nullable; pass ``None`` to leave a field unchanged. Used by
        the admin API and by the seed script."""
        sets: list[str] = []
        params: list[Any] = []
        if assigned_number is not None:
            sets.append("assigned_number = ?")
            params.append(assigned_number or None)
        if display_name is not None:
            sets.append("display_name = ?")
            params.append(display_name or None)
        if not sets:
            return self.get_user_by_id(user_id)
        params.extend([tenant_id, user_id])
        with self._lock:
            self._conn.execute(
                f"UPDATE users SET {', '.join(sets)} "
                "WHERE tenant_id = ? AND id = ?",
                tuple(params),
            )
        return self.get_user_by_id(user_id)

    # ───────────────────── #31 Agent presence ──────────────────────
    def upsert_presence(
        self,
        tenant_id: str,
        user_id: str,
        status: str,
        current_call_id: Optional[str] = None,
    ) -> dict:
        """Insert or update one agent's presence row. Bumps ``last_seen``
        and ``updated_at`` to now. Returns the persisted row."""
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                "INSERT INTO agent_presence(tenant_id, user_id, status, "
                "last_seen, current_call_id, updated_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(tenant_id, user_id) DO UPDATE SET "
                "status = excluded.status, "
                "last_seen = excluded.last_seen, "
                "current_call_id = excluded.current_call_id, "
                "updated_at = excluded.updated_at",
                (tenant_id, user_id, status, now, current_call_id, now),
            )
        return self.get_presence(tenant_id, user_id)  # type: ignore[return-value]

    def get_presence(self, tenant_id: str, user_id: str) -> Optional[dict]:
        return self._row(
            "SELECT * FROM agent_presence WHERE tenant_id = ? AND user_id = ?",
            (tenant_id, user_id),
        )

    def list_presence(self, tenant_id: str) -> list[dict]:
        return self._rows(
            "SELECT * FROM agent_presence WHERE tenant_id = ? "
            "ORDER BY last_seen DESC",
            (tenant_id,),
        )

    def touch_presence(self, tenant_id: str, user_id: str) -> None:
        """Bump ``last_seen`` to now without changing ``status``."""
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                "INSERT INTO agent_presence(tenant_id, user_id, status, "
                "last_seen, current_call_id, updated_at) "
                "VALUES (?,?,?,?,NULL,?) "
                "ON CONFLICT(tenant_id, user_id) DO UPDATE SET "
                "last_seen = excluded.last_seen, "
                "updated_at = excluded.updated_at",
                (tenant_id, user_id, "offline", now, now),
            )

    def sweep_stale_presence(
        self, tenant_id: str, *, idle_secs: int = 90, now: Optional[str] = None,
    ) -> int:
        """Auto-flip any ``status='online'`` row whose ``last_seen`` is
        older than ``idle_secs`` to ``offline``."""
        from datetime import datetime, timedelta, timezone
        if now is None:
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=idle_secs)).isoformat()
        else:
            cutoff = (datetime.fromisoformat(now.replace("Z", "+00:00")) -
                      timedelta(seconds=idle_secs)).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE agent_presence SET status = 'offline', "
                "current_call_id = NULL, updated_at = ? "
                "WHERE tenant_id = ? AND status = 'online' AND last_seen < ?",
                (_utcnow(), tenant_id, cutoff),
            )
            return cur.rowcount

    def find_idle_online_agent(
        self,
        tenant_id: str,
        skill: Optional[str] = None,
    ) -> Optional[dict]:
        """Return the longest-idle 'online' user (or first matching skill)."""
        params: list[Any] = [tenant_id]
        skill_clause = ""
        if skill:
            skill_clause = (
                " AND EXISTS (SELECT 1 FROM agent_skills s "
                " WHERE s.tenant_id = p.tenant_id "
                "   AND s.user_id = p.user_id "
                "   AND LOWER(s.skill) = LOWER(?))"
            )
            params.append(skill)
        sql = (
            "SELECT p.tenant_id, p.user_id, p.status, p.last_seen, "
            "       p.current_call_id, p.updated_at, "
            "       u.email, u.role, u.assigned_number, u.display_name "
            "FROM agent_presence p "
            "LEFT JOIN users u ON u.tenant_id = p.tenant_id AND u.id = p.user_id "
            "WHERE p.tenant_id = ? AND p.status = 'online' "
            f"{skill_clause} "
            "ORDER BY p.last_seen ASC LIMIT 1"
        )
        return self._row(sql, tuple(params))

    # ───────────────────── #31 Agent skills ────────────────────────
    def set_user_skills(
        self,
        tenant_id: str,
        user_id: str,
        skills: list,
    ) -> list[dict]:
        """Replace the user's skill set atomically."""
        if not isinstance(skills, list):
            raise ValueError("skills must be a list")
        now = _utcnow()
        rows: list[tuple[str, str, int, str]] = []
        for s in skills:
            if isinstance(s, str):
                rows.append((tenant_id, user_id, s, 50))
            elif isinstance(s, dict):
                tag = s.get("skill")
                if not tag or not isinstance(tag, str):
                    continue
                try:
                    lvl = int(s.get("level", 50))
                except (TypeError, ValueError):
                    lvl = 50
                lvl = max(0, min(100, lvl))
                rows.append((tenant_id, user_id, tag, lvl))
        with self._lock:
            self._conn.execute(
                "DELETE FROM agent_skills WHERE tenant_id = ? AND user_id = ?",
                (tenant_id, user_id),
            )
            for (tid, uid, tag, lvl) in rows:
                self._conn.execute(
                    "INSERT INTO agent_skills(tenant_id, user_id, skill, level, "
                    "updated_at) VALUES (?,?,?,?,?)",
                    (tid, uid, tag, lvl, now),
                )
        return self.get_user_skills(tenant_id, user_id)

    def get_user_skills(self, tenant_id: str, user_id: str) -> list[dict]:
        return self._rows(
            "SELECT skill, level, updated_at FROM agent_skills "
            "WHERE tenant_id = ? AND user_id = ? ORDER BY skill",
            (tenant_id, user_id),
        )

    def list_tenant_skills(self, tenant_id: str) -> list[dict]:
        """Distinct skill tags used in this tenant + count of users
        holding each. Used by the workflow editor's skill dropdown."""
        return self._rows(
            "SELECT skill, COUNT(*) AS user_count FROM agent_skills "
            "WHERE tenant_id = ? GROUP BY skill ORDER BY skill",
            (tenant_id,),
        )

    # ───────────────────── #32 Call queue ──────────────────────────
    def enqueue_call(
        self,
        tenant_id: str,
        call_id: str,
        skill_tags: Optional[list] = None,
        priority: int = 0,
    ) -> dict:
        """Append a call to the queue. Idempotent on (tenant_id,
        call_id, status='queued')."""
        if not isinstance(priority, int):
            try:
                priority = int(priority)
            except (TypeError, ValueError):
                priority = 0
        tags = skill_tags if isinstance(skill_tags, list) else []
        tags_json = json.dumps(tags)
        now = _utcnow()
        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM call_queue "
                "WHERE tenant_id = ? AND call_id = ? AND status = 'queued'",
                (tenant_id, call_id),
            ).fetchone()
            if existing:
                return self._row("SELECT * FROM call_queue WHERE id = ?", (existing[0],))  # type: ignore[return-value]
            qid = _new_id("q")
            self._conn.execute(
                "INSERT INTO call_queue(id, tenant_id, call_id, enqueued_at, "
                "priority, skill_tags_json, assigned_user_id, status, dequeued_at) "
                "VALUES (?,?,?,?,?,?,NULL,'queued', NULL)",
                (qid, tenant_id, call_id, now, priority, tags_json),
            )
        return self._row("SELECT * FROM call_queue WHERE id = ?", (qid,))  # type: ignore[return-value]

    def dequeue_for_user(
        self,
        tenant_id: str,
        user_id: str,
        *,
        user_skills: Optional[list] = None,
    ) -> Optional[dict]:
        """Pop the highest-priority oldest queued call whose skill tags
        intersect with the agent's skill set (if both are non-empty).
        Returns the row (status now 'assigned') or None."""
        with self._lock:
            rows = self._rows(
                "SELECT * FROM call_queue "
                "WHERE tenant_id = ? AND status = 'queued' "
                "ORDER BY priority DESC, enqueued_at ASC",
                (tenant_id,),
            )
            if not rows:
                return None
            agent_skill_set: set[str] = set()
            for s in (user_skills or []):
                if isinstance(s, dict):
                    tag = s.get("skill")
                else:
                    tag = s
                if isinstance(tag, str):
                    agent_skill_set.add(tag.lower())
            chosen: Optional[dict] = None
            for r in rows:
                if not agent_skill_set:
                    chosen = r
                    break
                try:
                    tags = json.loads(r.get("skill_tags_json") or "[]")
                except json.JSONDecodeError:
                    tags = []
                if any(isinstance(t, str) and t.lower() in agent_skill_set
                       for t in tags):
                    chosen = r
                    break
            if chosen is None:
                return None
            now = _utcnow()
            self._conn.execute(
                "UPDATE call_queue SET status = 'assigned', "
                "assigned_user_id = ?, dequeued_at = ? "
                "WHERE id = ?",
                (user_id, now, chosen["id"]),
            )
            return self._row("SELECT * FROM call_queue WHERE id = ?", (chosen["id"],))

    def get_queue_position(self, tenant_id: str, call_id: str) -> Optional[dict]:
        """Return {position, ahead, eta_s} for ``call_id`` or None."""
        row = self._row(
            "SELECT * FROM call_queue "
            "WHERE tenant_id = ? AND call_id = ? AND status = 'queued'",
            (tenant_id, call_id),
        )
        if not row:
            return None
        ahead = self._row(
            "SELECT COUNT(*) AS n FROM call_queue "
            "WHERE tenant_id = ? AND status = 'queued' AND "
            "       (priority > ? OR (priority = ? AND enqueued_at < ?))",
            (tenant_id, row.get("priority") or 0,
             row.get("priority") or 0, row.get("enqueued_at") or ""),
        )
        n_ahead = int((ahead or {}).get("n") or 0)
        return {"position": n_ahead + 1, "ahead": n_ahead, "eta_s": n_ahead * 30}

    def get_queue_stats(self, tenant_id: str) -> dict:
        """Counts for the queue dashboard tile."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        today = now.date().isoformat()
        oldest = self._row(
            "SELECT MIN(enqueued_at) AS oldest FROM call_queue "
            "WHERE tenant_id = ? AND status = 'queued'",
            (tenant_id,),
        )
        longest_wait_s = 0
        oldest_iso = (oldest or {}).get("oldest")
        if oldest_iso:
            try:
                dt = datetime.fromisoformat(oldest_iso.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                longest_wait_s = max(0, int((now - dt).total_seconds()))
            except Exception:
                pass
        waiting_row = self._row(
            "SELECT COUNT(*) AS n FROM call_queue "
            "WHERE tenant_id = ? AND status IN ('queued','assigned')",
            (tenant_id,),
        )
        abandoned_today = self._row(
            "SELECT COUNT(*) AS n FROM call_queue "
            "WHERE tenant_id = ? AND status = 'abandoned' "
            "       AND enqueued_at >= ?",
            (tenant_id, today),
        )
        answered_today = self._row(
            "SELECT COUNT(*) AS n FROM call_queue "
            "WHERE tenant_id = ? AND status = 'answered' "
            "       AND enqueued_at >= ?",
            (tenant_id, today),
        )
        return {
            "waiting": int((waiting_row or {}).get("n") or 0),
            "longest_wait_s": longest_wait_s,
            "abandoned_today": int((abandoned_today or {}).get("n") or 0),
            "answered_today": int((answered_today or {}).get("n") or 0),
        }

    def mark_abandoned(self, tenant_id: str, call_id: str) -> Optional[dict]:
        """Flip the call's queue row to 'abandoned'. Idempotent."""
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                "UPDATE call_queue SET status = 'abandoned', "
                "dequeued_at = COALESCE(dequeued_at, ?) "
                "WHERE tenant_id = ? AND call_id = ? "
                "      AND status IN ('queued','assigned')",
                (now, tenant_id, call_id),
            )
        return self._row(
            "SELECT * FROM call_queue "
            "WHERE tenant_id = ? AND call_id = ? "
            "ORDER BY enqueued_at DESC LIMIT 1",
            (tenant_id, call_id),
        )

    def mark_answered(self, tenant_id: str, call_id: str) -> Optional[dict]:
        """Flip the call's queue row to 'answered'."""
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                "UPDATE call_queue SET status = 'answered', "
                "dequeued_at = COALESCE(dequeued_at, ?) "
                "WHERE tenant_id = ? AND call_id = ? "
                "      AND status = 'assigned'",
                (now, tenant_id, call_id),
            )
        return self._row(
            "SELECT * FROM call_queue "
            "WHERE tenant_id = ? AND call_id = ? "
            "ORDER BY enqueued_at DESC LIMIT 1",
            (tenant_id, call_id),
        )

    def list_queue(self, tenant_id: str, status: Optional[str] = None) -> list[dict]:
        where = "tenant_id = ?"
        params: list[Any] = [tenant_id]
        if status:
            where += " AND status = ?"
            params.append(status)
        return self._rows(
            f"SELECT * FROM call_queue WHERE {where} "
            "ORDER BY priority DESC, enqueued_at ASC",
            tuple(params),
        )


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
