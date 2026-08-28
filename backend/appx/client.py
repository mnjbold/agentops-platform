"""Appwrite client wrapper for the agentops backend.

Loads credentials from `settings.json` (git-ignored) or env vars, and exposes
typed helpers for the most common operations.

Usage:
    from appx.client import get_appwrite
    aw = get_appwrite()
    print(aw.databases.list_documents(...))

The full Appwrite Python SDK reference is at:
    https://appwrite.io/docs/references/cloud/server-python

Wire format of settings.json (git-ignored, lives at backend/settings.json):
    {
      "env": {
        "APPWRITE_API_KEY=...",
        "APPWRITE_PROJECT_ID=...",
        "APPWRITE_ENDPOINT=..."
      }
    }

Or set the three env vars directly (no file needed):
    APPWRITE_API_KEY, APPWRITE_PROJECT_ID, APPWRITE_ENDPOINT
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# Where the local settings.json lives by convention
SETTINGS_PATH = Path(__file__).resolve().parent.parent / "settings.json"


def _parse_settings_file(path: Path) -> dict[str, str]:
    """Parse the loosely-formatted settings.json used by the user's tools.

    The file looks like:
        {
          "env": {
            "APPWRITE_API_KEY=...",
            "APPWRITE_PROJECT_ID=...",
            ...
          }
        }

    i.e. the values are unquoted k=v strings. We handle that here.
    """
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for line in raw.splitlines():
        m = re.match(r'^\s*"?([A-Z_][A-Z0-9_]*)=(.+?)"?\s*,?\s*$', line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _load_credentials() -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Resolve (api_key, project_id, endpoint) from env or settings.json.

    Env wins over file (so containerised deploys can override).
    """
    file_creds = _parse_settings_file(SETTINGS_PATH)

    api_key = os.environ.get("APPWRITE_API_KEY") or file_creds.get("APPWRITE_API_KEY")
    project_id = os.environ.get("APPWRITE_PROJECT_ID") or file_creds.get("APPWRITE_PROJECT_ID")
    endpoint = os.environ.get("APPWRITE_ENDPOINT") or file_creds.get("APPWRITE_ENDPOINT")

    return api_key, project_id, endpoint


_client = None


def get_appwrite():
    """Return a configured Appwrite Client (singleton)."""
    global _client
    if _client is not None:
        return _client

    api_key, project_id, endpoint = _load_credentials()
    if not api_key or not project_id or not endpoint:
        raise RuntimeError(
            "Appwrite credentials missing. Set APPWRITE_API_KEY, "
            "APPWRITE_PROJECT_ID, APPWRITE_ENDPOINT in env or "
            f"in {SETTINGS_PATH}"
        )

    # Import here so the dependency is optional until you wire it up.
    try:
        from appwrite.client import Client as AwClient
        from appwrite.services.account import Account
        from appwrite.services.databases import Databases
        from appwrite.services.users import Users
    except ImportError as e:
        raise RuntimeError(
            "appwrite SDK not installed. Run: pip install 'appwrite>=14,<15'"
        ) from e

    client = AwClient()
    client.set_endpoint(endpoint)
    client.set_project(project_id)
    client.set_key(api_key)
    _client = _Wrap(client)
    log.info("Appwrite client configured: project=%s endpoint=%s", project_id, endpoint)
    return _client


class _Wrap:
    """Thin namespace so callers do `aw.databases.list_documents(...)` etc."""

    def __init__(self, client):
        from appwrite.services.account import Account
        from appwrite.services.databases import Databases
        from appwrite.services.users import Users

        self.client = client
        self.account = Account(client)
        self.databases = Databases(client)
        self.users = Users(client)


def health() -> dict[str, Any]:
    """Quick reachability check. Returns dict with status, project_id, error.

    Never raises — used for /api/health so a missing Appwrite doesn't take
    the whole softphone down.
    """
    try:
        _, project_id, endpoint = _load_credentials()
        if not all([project_id, endpoint]):
            return {"configured": False, "status": "missing-creds"}
        from appwrite.services.databases import Databases
        client = get_appwrite().client
        # A 404 (database not found) still proves auth+endpoint are reachable.
        try:
            Databases(client).list_collections(database_id="agentops")
        except Exception as e:
            msg = str(e).lower()
            if "404" not in msg and "not found" not in msg:
                raise
        return {"configured": True, "status": "ok", "project_id": project_id, "endpoint": endpoint}
    except Exception as e:
        return {"configured": True, "status": "error", "error": str(e)[:200]}
