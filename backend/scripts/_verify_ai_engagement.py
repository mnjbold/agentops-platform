"""Verify the AI actually engaged on the call (not just answered)."""
import os, sys, httpx
from dotenv import load_dotenv
from pathlib import Path
from datetime import datetime, timezone, timedelta

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

key = os.environ.get("TELNYX_ORGANIZATION_API_KEY")
call_leg_id = "0c43147c-a195-11f1-8f3e-debaecefaa94"

print("=" * 60)
print("AI ENGAGEMENT VERIFICATION")
print("=" * 60)

# 1. Conversation insights by call_leg_id
print("\n[1] Conversation insights for our test call_leg")
for endpoint in ["/v2/conversations/insights", "/v2/ai/conversations", "/v2/insights"]:
    r = httpx.get(
        f"https://api.telnyx.com{endpoint}",
        params={"filter[call_leg_id]": call_leg_id, "page[size]": 5},
        headers={"Authorization": f"Bearer {key}"},
        timeout=10,
    )
    print(f"  GET {endpoint} -> HTTP {r.status_code}")
    if r.status_code == 200:
        body = r.json()
        items = body.get("data", [])
        print(f"    found {len(items)} items")
        for it in items[:2]:
            print(f"    - keys: {list(it.keys())[:8]}")
            for k, v in list(it.items())[:6]:
                v_str = str(v)[:80]
                print(f"        {k}: {v_str}")
        if items:
            break

# 2. Get full recording URLs (try v2/recordings then v2/ai/recordings)
print("\n[2] Recording URLs")
start = (datetime.now(timezone.utc) - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%S")
r = httpx.get(
    "https://api.telnyx.com/v2/recordings",
    params={"filter[created_at][gte]": start, "page[size]": 10},
    headers={"Authorization": f"Bearer {key}"},
    timeout=10,
)
recs = r.json().get("data", [])
print(f"  v2/recordings count: {len(recs)}")
for rec in recs:
    print(f"    - id={rec.get('id','')[:30]}")
    print(f"      call_leg_id: {rec.get('call_leg_id','')}")
    print(f"      status: {rec.get('status')}")
    print(f"      duration: {rec.get('duration_secs')}")
    print(f"      channels: {rec.get('channels')}")
    print(f"      format: {rec.get('recording_format')}")
    urls = rec.get("recording_urls", {})
    if isinstance(urls, dict):
        for fmt, url in urls.items():
            print(f"      {fmt}_url: {url[:90]}...")

# 3. Check webhook handler log for the test call
print("\n[3] Webhook receiver log tail")
log_path = PROJECT_ROOT / "wh_run.log"
if log_path.exists():
    text = log_path.read_text(errors="ignore")
    lines = text.splitlines()
    for line in lines[-25:]:
        if "v3:OOwD" in line or "3079999692" in line or "ai" in line.lower() or "assistant" in line.lower():
            print(f"  {line[:200]}")
    if not any("v3:OOwD" in l for l in lines):
        print("  (no log lines for the test CCI found)")

print()
print("=" * 60)
