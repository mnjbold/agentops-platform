"""SQLite connector — always-on local fallback.

Creates a ``w3j_telephony.db`` in the project root with two tables:
``call_events`` and ``leads``. Queryable via any sqlite3 client.

This is the *always-works* sink: if Supabase/Sheets/anything else fails,
events still land in SQLite so you never lose data.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional

from connectors.base import CallEvent, now_iso

log = logging.getLogger(__name__)


class SQLiteConnector:
    name = "sqlite"

    def __init__(self, db_path: Path = Path(__file__).resolve().parents[1] / "w3j_telephony.db") -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS call_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    call_control_id TEXT,
                    agent_id TEXT,
                    direction TEXT,
                    from_number TEXT,
                    to_number TEXT,
                    duration_seconds INTEGER,
                    recording_url TEXT,
                    transcript TEXT,
                    notes TEXT,
                    extra_json TEXT,
                    timestamp TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_call_events_ts ON call_events(timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_call_events_cci ON call_events(call_control_id);

                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT,
                    name TEXT,
                    phone TEXT,
                    email TEXT,
                    company TEXT,
                    notes TEXT,
                    extra_json TEXT,
                    timestamp TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_leads_ts ON leads(timestamp DESC);
                """
            )
            log.info("SQLite connector ready at %s", self.db_path)

    def write_event(self, event: CallEvent) -> bool:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO call_events
                    (event_type, call_control_id, agent_id, direction, from_number, to_number,
                     duration_seconds, recording_url, transcript, notes, extra_json, timestamp)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event.event_type,
                    event.call_control_id,
                    event.agent_id,
                    event.direction,
                    event.from_number,
                    event.to_number,
                    event.duration_seconds,
                    event.recording_url,
                    event.transcript,
                    event.notes,
                    json.dumps(event.extra or {}),
                    event.timestamp,
                ),
            )
        return True

    def write_lead(self, lead: dict[str, Any]) -> bool:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO leads (source, name, phone, email, company, notes, extra_json, timestamp)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    lead.get("source"),
                    lead.get("name"),
                    lead.get("phone"),
                    lead.get("email"),
                    lead.get("company"),
                    lead.get("notes"),
                    json.dumps({k: v for k, v in lead.items() if k not in {"source", "name", "phone", "email", "company", "notes"}}),
                    now_iso(),
                ),
            )
        return True

    def recent_events(self, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM call_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_leads(self, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM leads ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def is_healthy(self) -> bool:
        try:
            with self._conn() as c:
                c.execute("SELECT 1").fetchone()
            return True
        except Exception:
            return False
