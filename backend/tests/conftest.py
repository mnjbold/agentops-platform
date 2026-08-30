"""Test fixtures shared across the Phase A backend test suite.

The tests use an in-memory SQLite so they run without a real DB file.
Each test gets its own fresh store via the ``store`` and ``client`` fixtures.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Make the backend root importable.
_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# Set required env vars BEFORE importing the app.
os.environ.setdefault("TENANT_SECRET_MASTER_KEY", "test-master-key-do-not-use-in-prod")
os.environ.setdefault("TELNYX_API_KEY", "stub")
os.environ.setdefault("APPWRITE_API_KEY", "stub")
os.environ.setdefault("APPWRITE_PROJECT_ID", "stub")
os.environ.setdefault("APPWRITE_ENDPOINT", "https://cloud.appwrite.io/v1")

from webhooks import storage  # noqa: E402
from webhooks.storage import Store  # noqa: E402


@pytest.fixture
def store(monkeypatch) -> Store:
    """Fresh in-memory SQLite for every test.

    We monkeypatch the module-level ``_store`` and ``DEFAULT_DB_PATH`` so
    ``get_store()`` returns our test instance. The store's ``init()`` is
    called eagerly so all tables and migrations are in place.
    """
    fresh = Store(":memory:")
    fresh.init()
    monkeypatch.setattr(storage, "_store", fresh)
    return fresh


@pytest.fixture
def client(store: Store) -> TestClient:
    """FastAPI TestClient. The store fixture has already wired the
    in-memory DB, so any module that calls ``get_store()`` sees it."""
    # Import lazily so the env-var setup above runs first.
    from webhooks.server import app  # noqa: E402
    # Reset rate limiter so tests don't cross-pollute buckets.
    from webhooks import tenancy  # noqa: E402
    tenancy.rate_limit_reset()
    return TestClient(app)
