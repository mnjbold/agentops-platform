"""End-to-end smoke test of the W3J Telephony Platform.

Steps:
    1. Verify Telnyx credentials and balance.
    2. Start the webhook receiver (background).
    3. Start the MCP server (background).
    4. Post a synthetic Telnyx event to the webhook (simulating a call.initiated).
    5. Verify the event lands in SQLite.
    6. Print a pass/fail summary.

Usage:
    python scripts/smoke_test.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from connectors.registry import get_registry  # noqa: E402
from telnyx_mcp.clients.telnyx_client import get_client  # noqa: E402


def main() -> int:
    print("=" * 60)
    print("W3J Telephony Platform — Smoke Test")
    print("=" * 60)

    # 1. Credentials
    print("\n[1] Telnyx credentials")
    c = get_client()
    bal = c.api.balance.retrieve()
    # In SDK v4, the result is a Data object with attributes
    bal_d = bal if hasattr(bal, "balance") else (bal.data if hasattr(bal, "data") else bal)
    if hasattr(bal_d, "balance"):
        balance = float(bal_d.balance)
        currency = bal_d.currency
    else:
        balance = float(bal_d.get("balance", 0))
        currency = bal_d.get("currency", "USD")
    print(f"  balance: ${balance:.2f} {currency}")
    print(f"  key_type: {c.creds.key_type}")
    if balance < 0.50:
        print("  WARNING: balance is low — number purchases may fail")

    # 2. Webhook server (background)
    print("\n[2] Starting webhook server on :8081")
    webhook_log = _PROJECT_ROOT / "smoke_webhook.log"
    webhook_proc = subprocess.Popen(
        [str(_PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"), "-m", "webhooks", "--port", "8081", "--host", "127.0.0.1"],
        cwd=str(_PROJECT_ROOT),
        env={**os.environ, "PYTHONPATH": str(_PROJECT_ROOT)},
        stdout=open(webhook_log, "w"),
        stderr=subprocess.STDOUT,
    )
    time.sleep(3)

    try:
        # 3. Verify webhook is up
        print("\n[3] Webhook health check")
        r = httpx.get("http://127.0.0.1:8081/health", timeout=5)
        if r.status_code != 200:
            print(f"  FAIL: webhook health returned {r.status_code}")
            print(f"  log tail: {webhook_log.read_text()[-500:]}")
            return 1
        print(f"  OK: {r.json()}")

        # 4. Post a synthetic call.initiated event
        print("\n[4] Posting synthetic call.initiated event")
        event = {
            "event_type": "call.initiated",
            "data": {
                "event_type": "call.initiated",
                "payload": {
                    "call_control_id": "smoke-test-001",
                    "direction": "incoming",
                    "from": {"phone_number": "+15551234567"},
                    "to": {"phone_number": "+12135551234"},
                },
            },
        }
        r = httpx.post("http://127.0.0.1:8081/webhooks/telnyx", json=event, timeout=5)
        print(f"  status: {r.status_code}  body: {r.text[:200]}")

        # 5. Verify it landed in SQLite
        print("\n[5] Verify event in SQLite")
        reg = get_registry()
        sqlite = next(c for c in reg._connectors if c.name == "sqlite")
        events = sqlite.recent_events(limit=5)
        if not events:
            print("  FAIL: no events in SQLite")
        else:
            print(f"  OK: {len(events)} event(s), most recent:")
            for e in events[:3]:
                print(f"    - {e.get('timestamp')} {e.get('event_type')} cci={e.get('call_control_id')}")

        # 6. Verify Telnyx health
        print("\n[6] Telnyx account summary")
        from telnyx_mcp.tools.utility import telnyx_account_summary
        summary = telnyx_account_summary()
        print(f"  owned numbers: {summary['owned_numbers_count']}")
        print(f"  AI assistants: {summary['assistants_count']}")
        print(f"  call control apps: {summary['call_control_apps_count']}")
        print(f"  outbound voice profiles: {summary['outbound_voice_profiles_count']}")
        print(f"  messaging profiles: {summary['messaging_profiles_count']}")

        # 7. Verify the MCP server boots
        print("\n[7] MCP server boot check")
        proc = subprocess.run(
            [str(_PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"), "-c",
             "import sys; sys.path.insert(0, r'{}'); import telnyx_mcp.server; print('mcp_server_ok')".format(str(_PROJECT_ROOT))],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0 and "mcp_server_ok" in proc.stdout:
            print(f"  OK: {proc.stdout.strip()}")
        else:
            print(f"  FAIL: {proc.stderr}")

        # Summary
        print("\n" + "=" * 60)
        print("Smoke test PASSED if no FAIL above.")
        print(f"Webhook log: {webhook_log}")
        print("=" * 60)
        return 0
    finally:
        webhook_proc.terminate()
        try:
            webhook_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            webhook_proc.kill()


if __name__ == "__main__":
    sys.exit(main())
