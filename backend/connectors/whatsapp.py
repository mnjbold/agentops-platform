"""WhatsApp connector — Telnyx-hosted WhatsApp Business API.

Use for:
* Receiving SMS-equivalent on WhatsApp (text replies from the user)
* Sending outbound WhatsApp messages to the user (e.g. "Your call is being
  transferred to +60 112 111 3249, please hold")

Requires:
* WHATSAPP_BUSINESS_ACCOUNT_ID — Telnyx WhatsApp Business account id
* WHATSAPP_ACCESS_TOKEN       — long-lived access token

Telnyx docs: https://developers.telnyx.com/docs/messaging/whatsapp
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from connectors.base import CallEvent

log = logging.getLogger(__name__)

TELNYX_WA_BASE = "https://api.telnyx.com/v2/whatsapp"


class WhatsAppConnector:
    name = "whatsapp"

    def __init__(self) -> None:
        self.account_id = os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID")
        self.access_token = os.getenv("WHATSAPP_ACCESS_TOKEN")

    def is_healthy(self) -> bool:
        return bool(self.account_id and self.access_token)

    def send_text(self, to_phone: str, text: str, from_phone: str | None = None) -> dict:
        if not self.is_healthy():
            return {"ok": False, "error": "WhatsApp not configured"}
        try:
            r = httpx.post(
                f"{TELNYX_WA_BASE}/messages",
                json={
                    "to": to_phone,
                    "from": from_phone,
                    "type": "text",
                    "text": {"body": text},
                },
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/json",
                },
                timeout=15,
            )
            r.raise_for_status()
            return {"ok": True, "response": r.json()}
        except Exception as e:
            log.warning("WhatsApp send failed: %s", e)
            return {"ok": False, "error": str(e)}

    # The protocol only requires write_event / write_lead / is_healthy.
    def write_event(self, event: CallEvent) -> bool:  # noqa: D401
        """No-op: WhatsApp is an outbound channel, not a sink."""
        return False

    def write_lead(self, lead: dict[str, Any]) -> bool:  # noqa: D401
        return False
