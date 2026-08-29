"""Supabase connector — write call events and leads to a Supabase table.

Used to plug the Telnyx telephony layer into Bijou AI's existing
multi-tenant Supabase backend (each tenant has tenant_id; the connector
respects that filter so W3J LLC events and Bijou AI events are properly
isolated).

Set in .env:
    SUPABASE_URL=https://xxx.supabase.co
    SUPABASE_SERVICE_KEY=eyJ...
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from connectors.base import CallEvent

log = logging.getLogger(__name__)


class SupabaseConnector:
    name = "supabase"

    def __init__(self) -> None:
        self.url = os.getenv("SUPABASE_URL")
        self.key = os.getenv("SUPABASE_SERVICE_KEY")
        self._client: Any = None

    def _ensure(self) -> bool:
        if not (self.url and self.key):
            return False
        if self._client is not None:
            return True
        try:
            from supabase import create_client  # type: ignore
            self._client = create_client(self.url, self.key)
            return True
        except Exception as e:
            log.warning("Supabase init failed: %s", e)
            return False

    def write_event(self, event: CallEvent) -> bool:
        if not self._ensure():
            return False
        try:
            row = event.to_row()
            row["extra_json"] = event.extra  # Supabase prefers JSONB
            self._client.table("call_events").insert(row).execute()
            return True
        except Exception as e:
            log.warning("Supabase write_event failed: %s", e)
            return False

    def write_lead(self, lead: dict[str, Any]) -> bool:
        if not self._ensure():
            return False
        try:
            self._client.table("leads").insert(lead).execute()
            return True
        except Exception as e:
            log.warning("Supabase write_lead failed: %s", e)
            return False

    def is_healthy(self) -> bool:
        return self._ensure()
