"""agent_sdk — Telnyx AI Assistant Builder (issue #14).

The brief says: "Use the **Harness Core (Way 3)** approach — fastest
start. Use the AI Adapter + Comms Adapter." The Harness Core / StatefulActor
pattern lives in the Telnyx Edge runtime (TypeScript), which isn't
available in our Python backend. The brief also explicitly tells us
to use the AI Assistant CRUD (``/v2/ai/assistants`` REST) — that
*is* the public Python surface today, and it's how the existing
``telnyx_mcp`` client already talks to Telnyx.

This module wraps that surface for the assistant-builder UI:

* :mod:`agent_sdk.client` — thin REST helpers that go directly to
  Telnyx (``httpx``) for the verb-shaped surfaces that the SDK v4
  doesn't expose cleanly (voice list, TTS preview, calls/transfer to
  assistant).
* :mod:`agent_sdk.assistants` — the FastAPI router with
  ``/api/assistants`` CRUD, ``/api/voice-lab/*``, and
  ``/api/assistants/{id}/test-call``.
"""
from __future__ import annotations

from . import client, assistants  # noqa: F401  (re-exports)
