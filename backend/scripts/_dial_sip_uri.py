"""Test: dial the user's softphone via SIP URI (no number required)."""
import base64
import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
load_dotenv()
key = os.getenv("TELNYX_ORGANIZATION_API_KEY")
creds = json.load(open(Path(__file__).resolve().parent / "sip_credentials.json"))

sip_to = f"sip:{creds['user_name']}@sip.telnyx.com"
print(f"Connection: {creds['connection_id']}")
print(f"User:       {creds['user_name']}")
print(f"Dialing:    {sip_to}")
print()

# client_state must be base64-encoded
cs = base64.b64encode(json.dumps({"purpose": "sip_test", "user": creds['user_name']}).encode()).decode()

r = httpx.post(
    "https://api.telnyx.com/v2/calls",
    json={
        "to": sip_to,
        "from": "+18444618814",
        "connection_id": "3016341331493520609",
        "client_state": cs,
    },
    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    timeout=15,
)
print(f"status: {r.status_code}")
print(f"body:   {r.text[:500]}")
