"""Centralized env loading for the W3J Telephony Platform.

We accept *three* credential shapes and pick the most powerful one:

1. ``TELNYX_ORGANIZATION_API_KEY``  — V2 org key (``KEY019...``), full access.
2. ``TELNYX_API_KEY``               — JWT scoped to a product (e.g. ``ie_model``).
3. ``TELNYX_PUBLIC_API_KEY``        — public/embed-only, NOT for API calls.

We also pick up optional integration keys (Google Sheets, Supabase, ElevenLabs,
Cartesia, OpenAI) when present and silently no-op when absent — so the
telephony core always boots, even if a connector is not yet wired.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Load .env from the project root (where the user placed it)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ENV_PATH = _PROJECT_ROOT / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH, override=False)

log = logging.getLogger(__name__)


@dataclass
class TelnyxCreds:
    """Resolved Telnyx credentials."""

    api_key: str
    source: str  # which env var won
    key_type: str  # "organization" | "jwt_ie_model" | "jwt_other" | "unknown"

    @property
    def is_full_access(self) -> bool:
        """Full-access keys are org keys (KEY019…) or unscoped JWTs."""
        return self.key_type in ("organization",)


@dataclass
class Integrations:
    """Optional integration keys. None = disabled."""

    google_sheets_credentials_path: Optional[str] = None
    google_sheets_spreadsheet_id: Optional[str] = None
    supabase_url: Optional[str] = None
    supabase_service_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    elevenlabs_api_key: Optional[str] = None
    cartesia_api_key: Optional[str] = None
    telegram_bot_token: Optional[str] = None
    whatsapp_business_account_id: Optional[str] = None
    whatsapp_access_token: Optional[str] = None
    webhook_base_url: Optional[str] = None  # public URL for Telnyx webhooks
    webhook_signing_secret: Optional[str] = None
    webrtc_username: Optional[str] = None  # SIP/WebRTC credential user_name
    webrtc_password: Optional[str] = None  # SIP/WebRTC credential password
    webrtc_connection_id: Optional[str] = None  # SIP/WebRTC credential connection id

    @property
    def enabled(self) -> list[str]:
        return [k for k, v in self.__dict__.items() if v is not None and k != "enabled"]


def _classify_key(value: str) -> str:
    if value.startswith("KEY"):
        return "organization"
    if value.startswith("eyJ"):
        # JWT — try to read the scope claim
        try:
            import base64
            import json
            payload_b64 = value.split(".")[1]
            # pad
            payload_b64 += "=" * (-len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            scope = payload.get("scope", "")
            if scope == "ie_model":
                return "jwt_ie_model"
            return f"jwt_{scope}" if scope else "jwt_other"
        except Exception:
            return "jwt_other"
    return "unknown"


def load_telnyx_creds() -> TelnyxCreds:
    """Pick the best Telnyx credential available.

    Priority:
        1. ``TELNYX_ORGANIZATION_API_KEY``  (full access, recommended)
        2. ``TELNYX_API_KEY``               (may be scoped; warn if so)

    Raises:
        RuntimeError: when no usable key is found.
    """
    org = os.getenv("TELNYX_ORGANIZATION_API_KEY")
    if org:
        return TelnyxCreds(api_key=org, source="TELNYX_ORGANIZATION_API_KEY", key_type=_classify_key(org))

    api = os.getenv("TELNYX_API_KEY")
    if api:
        kt = _classify_key(api)
        if kt == "jwt_ie_model":
            log.warning(
                "TELNYX_API_KEY is scoped to ie_model (Inference Engine only). "
                "Most API calls will fail. Set TELNYX_ORGANIZATION_API_KEY for full access."
            )
        return TelnyxCreds(api_key=api, source="TELNYX_API_KEY", key_type=kt)

    raise RuntimeError(
        "No Telnyx credentials found. Set TELNYX_ORGANIZATION_API_KEY "
        "(preferred) or TELNYX_API_KEY in .env"
    )


def load_integrations() -> Integrations:
    """Return an Integrations object with whichever optional keys are present."""
    return Integrations(
        google_sheets_credentials_path=os.getenv("GOOGLE_SHEETS_CREDENTIALS_PATH"),
        google_sheets_spreadsheet_id=os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID"),
        supabase_url=os.getenv("SUPABASE_URL"),
        supabase_service_key=os.getenv("SUPABASE_SERVICE_KEY"),
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        elevenlabs_api_key=os.getenv("ELEVENLABS_API_KEY"),
        cartesia_api_key=os.getenv("CARTESIA_API_KEY"),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
        whatsapp_business_account_id=os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID"),
        whatsapp_access_token=os.getenv("WHATSAPP_ACCESS_TOKEN"),
        webhook_base_url=os.getenv("WEBHOOK_BASE_URL"),
        webhook_signing_secret=os.getenv("WEBHOOK_SIGNING_SECRET"),
        webrtc_username=os.getenv("TELNYX_WEBRTC_USERNAME"),
        webrtc_password=os.getenv("TELNYX_WEBRTC_PASSWORD"),
        webrtc_connection_id=os.getenv("TELNYX_WEBRTC_CONNECTION_ID"),
    )


def project_root() -> Path:
    return _PROJECT_ROOT


if __name__ == "__main__":
    # Quick CLI to print the resolved state (no secrets printed)
    c = load_telnyx_creds()
    i = load_integrations()
    print(f"Telnyx key:  source={c.source}  type={c.key_type}  full_access={c.is_full_access}")
    print(f"Integrations enabled: {i.enabled or '(none)'}")
