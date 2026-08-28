"""Check post-test state: call state + recording + new balance."""
import os
import sys
import httpx
from dotenv import load_dotenv
from pathlib import Path
from datetime import datetime, timezone, timedelta

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

key = os.environ.get("TELNYX_ORGANIZATION_API_KEY")
cci = "v3:OOwDvv2Aigf_vIpq1hAfectYCwPx8NBImLR1fZhCJ1s2nkrp7ElIvw"

print("=" * 60)
print("POST-TEST STATE CHECK")
print("=" * 60)

# 1. Final call state
print("\n[1] Final call state from Telnyx")
r = httpx.get(f"https://api.telnyx.com/v2/calls/{cci}", headers={"Authorization": f"Bearer {key}"}, timeout=10)
print(f"  HTTP {r.status_code}")
d = r.json().get("data", {})
print(f"  state:        {d.get('state')}")
print(f"  is_alive:     {d.get('is_alive')}")
print(f"  call_duration: {d.get('call_duration')}s")
print(f"  hangup_cause: {d.get('hangup_cause')}")

# 2. Recordings
print("\n[2] Recordings in last 10 min")
start = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S")
r = httpx.get(
    "https://api.telnyx.com/v2/recordings",
    params={"filter[created_at][gte]": start, "page[size]": 10},
    headers={"Authorization": f"Bearer {key}"},
    timeout=10,
)
print(f"  HTTP {r.status_code}")
recs = r.json().get("data", [])
print(f"  count: {len(recs)}")
for rec in recs:
    created = rec.get("created_at", "")
    fmt = rec.get("recording_format", "")
    dur = rec.get("duration_secs", "")
    leg = (rec.get("call_leg_id", "") or "")[:30]
    url = (rec.get("recording_urls", {}).get("mp3", "") if isinstance(rec.get("recording_urls"), dict) else "")
    print(f"    - {created}  {fmt}  dur={dur}s  leg={leg}")
    if url:
        print(f"      mp3_url: {url[:80]}...")

# 3. New balance
print("\n[3] Current balance")
r = httpx.get("https://api.telnyx.com/v2/balance", headers={"Authorization": f"Bearer {key}"}, timeout=10)
b = r.json().get("data", {})
print(f"  balance: ${b.get('balance')} {b.get('currency')}")

# 4. Recent webhook events (last 5)
print("\n[4] Most recent webhook events")
from connectors.registry import get_registry
reg = get_registry()
sqlite = next(c for c in reg._connectors if c.name == "sqlite")
events = sqlite.recent_events(limit=5)
for e in events:
    et = e.get("event_type", "")
    cc = (e.get("call_control_id", "") or "")[:50]
    ts = e.get("timestamp", "")
    print(f"  {ts}  {et:30s}  cci={cc}")

print()
print("=" * 60)
