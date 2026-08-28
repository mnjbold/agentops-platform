"""Appwrite data layer for agentops.

This package wraps each Appwrite collection in a typed repository. The
existing FastAPI endpoints in `webhooks/dashboard_api.py` can either:
  - call these directly for the new Appwrite-backed data
  - keep using Telnyx API for live data (numbers, balance, live calls)
  - or read-through Telnyx for the first hit and fall back to Appwrite
    for historical queries

All write paths go to Appwrite so we have a single source of truth for
the contact / call / message history.
"""
from appx.repos import calls, contacts, campaigns, messages, scheduled_jobs, telnyx_events

__all__ = ["calls", "contacts", "campaigns", "messages", "scheduled_jobs", "telnyx_events"]
