"""Google Sheets connector — write call events and leads to a spreadsheet.

Lazy-loads gspread + google-auth. Set in .env:
    GOOGLE_SHEETS_CREDENTIALS_PATH=/path/to/service-account.json
    GOOGLE_SHEETS_SPREADSHEET_ID=1AbC...xyz

If either is missing, the connector no-ops gracefully.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from connectors.base import CallEvent

log = logging.getLogger(__name__)


class GoogleSheetsConnector:
    name = "google_sheets"

    def __init__(self) -> None:
        self.creds_path = os.getenv("GOOGLE_SHEETS_CREDENTIALS_PATH")
        self.spreadsheet_id = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID")
        self._client: Any = None
        self._sh: Any = None
        self._call_ws: Any = None
        self._lead_ws: Any = None

    def _ensure(self) -> bool:
        if not (self.creds_path and self.spreadsheet_id):
            return False
        if self._client is not None:
            return True
        try:
            import gspread  # type: ignore
            from google.oauth2.service_account import Credentials  # type: ignore
            scopes = [
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ]
            creds = Credentials.from_service_account_file(self.creds_path, scopes=scopes)
            self._client = gspread.authorize(creds)
            self._sh = self._client.open_by_key(self.spreadsheet_id)
            self._call_ws = self._sh.worksheet("Call Events") if "Call Events" in [w.title for w in self._sh.worksheets()] else self._sh.add_worksheet("Call Events", rows=1000, cols=20)
            self._lead_ws = self._sh.worksheet("Leads") if "Leads" in [w.title for w in self._sh.worksheets()] else self._sh.add_worksheet("Leads", rows=1000, cols=15)
            return True
        except Exception as e:
            log.warning("Google Sheets connector init failed: %s", e)
            return False

    def write_event(self, event: CallEvent) -> bool:
        if not self._ensure():
            return False
        try:
            row = list(event.to_row().values())
            self._call_ws.append_row(row, value_input_option="USER_ENTERED")
            return True
        except Exception as e:
            log.warning("Sheets write_event failed: %s", e)
            return False

    def write_lead(self, lead: dict[str, Any]) -> bool:
        if not self._ensure():
            return False
        try:
            row = [
                lead.get("timestamp"),
                lead.get("source"),
                lead.get("name"),
                lead.get("phone"),
                lead.get("email"),
                lead.get("company"),
                lead.get("notes"),
            ]
            self._lead_ws.append_row(row, value_input_option="USER_ENTERED")
            return True
        except Exception as e:
            log.warning("Sheets write_lead failed: %s", e)
            return False

    def is_healthy(self) -> bool:
        return self._ensure()
