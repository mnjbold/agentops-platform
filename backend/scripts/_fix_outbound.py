"""Assign the existing 'Default' outbound voice profile to all 3 new call control apps."""
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

load_dotenv()
key = os.getenv("TELNYX_ORGANIZATION_API_KEY")

# Default outbound voice profile (id verified earlier)
DEFAULT_OVP = "2949637839123383864"

# The 3 new apps (from the deploy report)
APPS = {
    "W3J LLC Concierge App":     "3016337470611523194",
    "Bijou AI Concierge App":    "3016337410440037999",
    "W3J Personal Twin App":     "3016341331493520609",
}

for name, app_id in APPS.items():
    print(f"Assigning Default OVP to {name} ({app_id})...")
    r = httpx.patch(
        f"https://api.telnyx.com/v2/call_control_applications/{app_id}",
        json={"outbound": {"outbound_voice_profile_id": DEFAULT_OVP}},
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        timeout=15,
    )
    print(f"  status: {r.status_code}")
    if r.status_code >= 400:
        print(f"  body:   {r.text[:400]}")
    else:
        body = r.json()
        d = body.get("data", body)
        out = d.get("outbound", {})
        print(f"  outbound now: {out}")
    print()
