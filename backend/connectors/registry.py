"""Connector registry — instantiate all enabled connectors and fan-out writes.

Always includes SQLite (works without any config). Adds Google Sheets,
Supabase, WhatsApp, Telegram if their env vars are present.
"""
from __future__ import annotations

import logging
from typing import Any

from connectors.base import CallEvent
from connectors.sqlite import SQLiteConnector

log = logging.getLogger(__name__)


class ConnectorRegistry:
    def __init__(self) -> None:
        self._connectors: list[Any] = [SQLiteConnector()]
        # Optional connectors — added on demand
        try:
            from connectors.google_sheets import GoogleSheetsConnector
            self._connectors.append(GoogleSheetsConnector())
        except Exception as e:
            log.debug("google_sheets not wired: %s", e)
        try:
            from connectors.supabase import SupabaseConnector
            self._connectors.append(SupabaseConnector())
        except Exception as e:
            log.debug("supabase not wired: %s", e)
        try:
            from connectors.whatsapp import WhatsAppConnector
            self._connectors.append(WhatsAppConnector())
        except Exception as e:
            log.debug("whatsapp not wired: %s", e)
        try:
            from connectors.telegram import TelegramConnector
            self._connectors.append(TelegramConnector())
        except Exception as e:
            log.debug("telegram not wired: %s", e)
        log.info(
            "ConnectorRegistry: %d connectors wired (%s)",
            len(self._connectors),
            ", ".join(c.name for c in self._connectors),
        )

    def write_event(self, event: CallEvent) -> list[str]:
        written: list[str] = []
        for c in self._connectors:
            if not hasattr(c, "write_event"):
                continue
            try:
                if c.write_event(event):
                    written.append(c.name)
            except Exception as e:
                log.warning("%s.write_event failed: %s", c.name, e)
        return written

    def write_lead(self, lead: dict) -> list[str]:
        written: list[str] = []
        for c in self._connectors:
            if not hasattr(c, "write_lead"):
                continue
            try:
                if c.write_lead(lead):
                    written.append(c.name)
            except Exception as e:
                log.warning("%s.write_lead failed: %s", c.name, e)
        return written

    def health(self) -> dict:
        return {c.name: c.is_healthy() for c in self._connectors}


_singleton: ConnectorRegistry | None = None


def get_registry() -> ConnectorRegistry:
    global _singleton
    if _singleton is None:
        _singleton = ConnectorRegistry()
    return _singleton
