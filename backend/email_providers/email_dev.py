"""Dev-mode email provider (issue #27, default).

The :class:`DevProvider` is the concrete adapter used when no real
provider is configured. It doesn't talk to any external service; every
``send`` call appends a JSON line to a per-tenant log file under
``backend/email_outbox/`` so the operator can inspect what would have
been sent. Inbound webhooks are not supported in dev mode — the dashboard
can still synthesise test rows via the inbound-test endpoint (see the
``webhooks/email`` router) for development.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import EmailProvider, register_provider

log = logging.getLogger(__name__)

_OUTBOX = Path(__file__).resolve().parent / "email_outbox"
_OUTBOX.mkdir(exist_ok=True)


class DevProvider(EmailProvider):
    """Log every send to a file. No external HTTP."""

    name = "dev"

    def __init__(self) -> None:
        self.outbox = _OUTBOX

    def send(
        self,
        *,
        to: str,
        from_addr: str,
        subject: str,
        body: str,
        html: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> dict[str, Any]:
        msg_id = f"dev_{secrets.token_hex(8)}"
        record = {
            "message_id": msg_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "to": to,
            "from": from_addr,
            "subject": subject,
            "body": body,
            "html": html,
            "reply_to": reply_to,
        }
        # One file per day so the operator can find yesterday's sends fast.
        day_file = self.outbox / f"outbox-{datetime.now(timezone.utc).date().isoformat()}.log"
        try:
            with day_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            log.warning("DevProvider failed to write outbox: %s", e)
            return {"ok": False, "error": f"outbox write failed: {e}", "stub": True}
        log.info("DevProvider: wrote %s to %s", msg_id, day_file)
        return {
            "ok": True,
            "message_id": msg_id,
            "stub": True,
            "outbox_path": str(day_file),
        }

    def handle_inbound(self, payload: dict[str, Any]) -> dict[str, Any]:
        # The dev provider does not have a real inbound webhook, but the
        # test endpoint still calls this so the data layer is exercised.
        from_addr = (
            payload.get("from_addr")
            or payload.get("from")
            or payload.get("sender")
            or ""
        )
        to_addr = (
            payload.get("to_addr")
            or payload.get("to")
            or payload.get("recipient")
            or ""
        )
        subject = payload.get("subject") or ""
        body = payload.get("body") or payload.get("text") or ""
        html = payload.get("html")
        if not from_addr or not to_addr:
            raise ValueError("from_addr and to_addr are required in inbound payload")
        return {
            "from_addr": from_addr,
            "to_addr": to_addr,
            "subject": subject,
            "body": body,
            "html": html,
            "provider_message_id": payload.get("message_id") or f"dev_in_{secrets.token_hex(6)}",
        }


register_provider("dev", DevProvider)
