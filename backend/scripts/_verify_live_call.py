"""Verify a self-call test fired the webhook and the AI picked up."""
import os
import sys
import json
import httpx
from dotenv import load_dotenv
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

key = os.environ.get("TELNYX_ORGANIZATION_API_KEY")
cci = "v3:OOwDvv2Aigf_vIpq1hAfectYCwPx8NBImLR1fZhCJ1s2nkrp7ElIvw"

print("=" * 60)
print("LIVE CALL VERIFICATION")
print("=" * 60)

# 1. Webhook events in SQLite
print("\n[1] Recent webhook events in SQLite")
from connectors.registry import get_registry
reg = get_registry()
sqlite = next(c for c in reg._connectors if c.name == "sqlite")
events = sqlite.recent_events(limit=15)
for e in events:
    et = e.get("event_type", "")
    cc = (e.get("call_control_id", "") or "")[:50]
    ts = e.get("timestamp", "")
    print(f"  {ts}  {et:30s}  cci={cc}")
print(f"  total: {len(events)}")

# 2. Call state from Telnyx
print("\n[2] Call state from Telnyx")
r = httpx.get(
    f"https://api.telnyx.com/v2/calls/{cci}",
    headers={"Authorization": f"Bearer {key}"},
    timeout=10,
)
print(f"  HTTP {r.status_code}")
try:
    d = r.json().get("data", {})
    print(f"  state:     {d.get('state')}")
    print(f"  is_alive:  {d.get('is_alive')}")
    print(f"  direction: {d.get('direction')}")
    print(f"  from:      {d.get('from', {}).get('phone_number')}")
    print(f"  to:        {d.get('to', {}).get('phone_number')}")
    if d.get("hangup_cause"):
        print(f"  hangup_cause: {d.get('hangup_cause')}")
    if d.get("call_duration"):
        print(f"  call_duration: {d.get('call_duration')}s")
except Exception as e:
    print(f"  parse err: {e}")
    print(r.text[:300])

# 3. Recordings (the AI session creates a recording)
print("\n[3] Recordings in last 5 min")
from datetime import datetime, timezone, timedelta
import base64

start = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
r = httpx.get(
    "https://api.telnyx.com/v2/recordings",
    params={"filter[created_at][gte]": start, "page[size]": 20},
    headers={"Authorization": f"Bearer {key}"},
    timeout=10,
)
print(f"  HTTP {r.status_code}")
try:
    recs = r.json().get("data", [])
    print(f"  count: {len(recs)}")
    for rec in recs[:5]:
        print(f"    - {rec.get('created_at')}  {rec.get('recording_format')}  dur={rec.get('duration_secs')}s  call={rec.get('call_leg_id','')[:30]}")
except Exception as e:
    print(f"  parse err: {e}")

# 4. Insight / conversation data (call summary)
print("\n[4] Recent conversations")
r = httpx.get(
    "https://api.telnyx.com/v2/insights/conversations",
    params={"page[size]": 5},
    headers={"Authorization": f"Bearer {key}"},
    timeout=10,
)
print(f"  HTTP {r.status_code}")
try:
    convos = r.json().get("data", [])
    print(f"  count: {len(convos)}")
    for c in convos[:5]:
        print(f"    - {c.get('created_at')}  name={c.get('name','')[:50]}  type={c.get('type','')}")
except Exception as e:
    print(f"  parse err: {e}")

print()
print("=" * 60)
