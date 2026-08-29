"""Telegram connector — bidirectional chat interface for the agent.

Use for the W3J personal twin: user sends "call John and connect me" to a
Telegram bot, the bot dials John via Telnyx, bridges when John answers.

Requires:
    TELEGRAM_BOT_TOKEN — from @BotFather
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from connectors.base import CallEvent

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


class TelegramConnector:
    name = "telegram"

    def __init__(self) -> None:
        self.token = os.getenv("TELEGRAM_BOT_TOKEN")

    def is_healthy(self) -> bool:
        return bool(self.token)

    def send_text(self, chat_id: int | str, text: str) -> dict:
        if not self.is_healthy():
            return {"ok": False, "error": "Telegram not configured"}
        try:
            r = httpx.post(
                TELEGRAM_API.format(token=self.token, method="sendMessage"),
                json={"chat_id": chat_id, "text": text},
                timeout=15,
            )
            r.raise_for_status()
            return {"ok": True, "response": r.json()}
        except Exception as e:
            log.warning("Telegram send failed: %s", e)
            return {"ok": False, "error": str(e)}

    def get_updates(self, offset: int | None = None, timeout: int = 30) -> list[dict]:
        if not self.is_healthy():
            return []
        try:
            params: dict[str, Any] = {"timeout": timeout}
            if offset:
                params["offset"] = offset
            r = httpx.get(
                TELEGRAM_API.format(token=self.token, method="getUpdates"),
                params=params,
                timeout=timeout + 5,
            )
            r.raise_for_status()
            return r.json().get("result", [])
        except Exception as e:
            log.warning("Telegram getUpdates failed: %s", e)
            return []

    def write_event(self, event: CallEvent) -> bool:  # noqa: D401
        return False

    def write_lead(self, lead: dict[str, Any]) -> bool:  # noqa: D401
        return False
