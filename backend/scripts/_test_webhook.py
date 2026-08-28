"""Quick smoke test of the webhook server: post a synthetic event, verify it lands in SQLite."""
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "http://127.0.0.1:8082"

# 1. health
r = httpx.get(f"{BASE}/health", timeout=5)
print("HEALTH:", r.status_code, r.json())

# 2. routing
r = httpx.get(f"{BASE}/admin/routing", timeout=5)
print("ROUTING:", r.status_code, r.json())

# 3. post a synthetic call.initiated
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
r = httpx.post(f"{BASE}/webhooks/telnyx", json=event, timeout=5)
print("WEBHOOK:", r.status_code, r.text[:200])

# 4. post call.answered (should try to start an AI assistant; will fail if no routing)
event2 = {
    "event_type": "call.answered",
    "data": {
        "event_type": "call.answered",
        "payload": {
            "call_control_id": "smoke-test-002",
            "direction": "incoming",
            "from": {"phone_number": "+15551234567"},
            "to": {"phone_number": "+12135551234"},
        },
    },
}
r = httpx.post(f"{BASE}/webhooks/telnyx", json=event2, timeout=5)
print("WEBHOOK answered:", r.status_code, r.text[:200])

# 5. verify SQLite got both
from connectors.registry import get_registry
reg = get_registry()
sqlite = next(c for c in reg._connectors if c.name == "sqlite")
events = sqlite.recent_events(limit=5)
print(f"SQLite rows: {len(events)}")
for e in events[:3]:
    print(f"  - {e['timestamp']} {e['event_type']} cci={e['call_control_id']}")
