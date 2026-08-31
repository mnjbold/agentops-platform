"""Daily.co client (issue #26).

A thin wrapper around the Daily REST API. The :class:`DailyClient` is
responsible for:

* creating rooms (``POST /v1/rooms``)
* minting meeting tokens (``POST /v1/meeting-tokens``)
* deleting rooms on host-leave (``DELETE /v1/rooms/{id}``)

The client is **stub-first**: when ``DAILY_API_KEY`` is unset (the dev /
test / CI default) every call returns a synthetic room + token so the
dashboard is fully exercisable without a Daily account. The stub
response is loud in the logs so the operator notices the gap.
"""
from __future__ import annotations

import logging
import os
import secrets
import time
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

_API_BASE = "https://api.daily.co/v1"
_DEFAULT_TTL = 60 * 60  # 1 hour — long enough for a real meeting


class DailyClient:
    """Thin Daily.co client. Stubs gracefully when ``DAILY_API_KEY`` is unset."""

    def __init__(self, api_key: Optional[str] = None, *, base_url: str = _API_BASE) -> None:
        self.api_key = (api_key or os.environ.get("DAILY_API_KEY") or "").strip()
        self.base_url = base_url.rstrip("/")
        # The client is created lazily so tests that never call the API
        # don't pay the import cost.
        self._client: Optional[httpx.Client] = None

    @property
    def is_stub(self) -> bool:
        return not self.api_key

    # ──────────────────────────── HTTP helpers ─────────────────────────────────

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=20.0,
            )
        return self._client

    def _stub_room(self, *, name_hint: str = "meet") -> dict[str, Any]:
        """Return a fake room object that matches the Daily shape enough
        for the dashboard to render the join URL."""
        rid = f"stub_{secrets.token_hex(8)}"
        room_name = f"{name_hint}-{secrets.token_hex(4)}"
        return {
            "id": rid,
            "name": room_name,
            "url": f"https://stub.daily.co/{room_name}",
            "privacy": "private",
            "stub": True,
        }

    def _stub_token(
        self, *, room_name: str, user_name: str, is_owner: bool, ttl: int
    ) -> dict[str, Any]:
        return {
            "token": f"stub_tok_{secrets.token_hex(12)}",
            "room_name": room_name,
            "user_name": user_name,
            "is_owner": is_owner,
            "expires_at": int(time.time()) + ttl,
            "stub": True,
        }

    # ──────────────────────────── public API ───────────────────────────────────

    def create_room(
        self,
        *,
        name_hint: str = "meet",
        privacy: str = "private",
        expires_in: int = _DEFAULT_TTL,
        enable_recording: bool = False,
    ) -> dict[str, Any]:
        """Create a new Daily room. Returns the room dict."""
        if self.is_stub:
            log.info("DailyClient stub: create_room name_hint=%s", name_hint)
            return self._stub_room(name_hint=name_hint)
        body = {
            "privacy": privacy,
            "properties": {
                "exp": int(time.time()) + int(expires_in),
                "enable_chat": True,
                "enable_screenshare": True,
                "start_video_off": False,
                "start_audio_off": False,
            },
        }
        if enable_recording:
            body["properties"]["enable_recording"] = "cloud"
        try:
            r = self._http().post("/rooms", json=body)
        except Exception as e:
            log.warning("Daily create_room transport error: %s", e)
            return {"ok": False, "error": str(e)}
        if r.status_code >= 400:
            return {"ok": False, "error": f"Daily {r.status_code}: {r.text[:300]}"}
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        return data

    def create_meeting_token(
        self,
        *,
        room_name: str,
        user_name: str,
        is_owner: bool = False,
        ttl: int = _DEFAULT_TTL,
    ) -> dict[str, Any]:
        """Mint a short-lived token for a specific room + user.

        Returns ``{"token": str, "expires_at": int, "stub"?: bool}``.
        """
        if self.is_stub:
            log.info(
                "DailyClient stub: create_meeting_token room=%s user=%s owner=%s",
                room_name, user_name, is_owner,
            )
            return self._stub_token(
                room_name=room_name, user_name=user_name,
                is_owner=is_owner, ttl=ttl,
            )
        body = {
            "properties": {
                "room_name": room_name,
                "user_name": user_name,
                "is_owner": bool(is_owner),
                "exp": int(time.time()) + int(ttl),
            }
        }
        try:
            r = self._http().post("/meeting-tokens", json=body)
        except Exception as e:
            log.warning("Daily create_meeting_token transport error: %s", e)
            return {"ok": False, "error": str(e)}
        if r.status_code >= 400:
            return {"ok": False, "error": f"Daily {r.status_code}: {r.text[:300]}"}
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        return data

    def delete_room(self, room_name: str) -> dict[str, Any]:
        """Delete a room. Best-effort: failures are logged but not raised
        because the UI has already moved on by the time this is called."""
        if self.is_stub:
            log.info("DailyClient stub: delete_room room=%s", room_name)
            return {"ok": True, "stub": True}
        try:
            r = self._http().delete(f"/rooms/{room_name}")
        except Exception as e:
            log.warning("Daily delete_room transport error: %s", e)
            return {"ok": False, "error": str(e)}
        if r.status_code >= 400 and r.status_code != 404:
            return {"ok": False, "error": f"Daily {r.status_code}: {r.text[:300]}"}
        return {"ok": True}


# Module-level singleton so the meetings router doesn't recreate the client
# (and the underlying httpx.Client) on every request.
_singleton: Optional[DailyClient] = None


def get_daily_client() -> DailyClient:
    global _singleton
    if _singleton is None:
        _singleton = DailyClient()
        if _singleton.is_stub:
            log.info("DailyClient initialised in STUB mode (DAILY_API_KEY unset)")
    return _singleton


def reset_daily_client() -> None:
    """Test helper: drop the cached singleton so a new key takes effect."""
    global _singleton
    if _singleton is not None and _singleton._client is not None:
        try:
            _singleton._client.close()
        except Exception:
            pass
    _singleton = None
