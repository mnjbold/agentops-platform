"""Set up a Telnyx SIP Credential Connection.

The connection is created without a phone number (which costs $2 upfront:
$1 setup + $1 first month). The user can either:
  1. Top up balance to $2+ then run scripts/assign_sip_number.py
  2. Move an existing number from a Call Control App to this SIP connection

Output: scripts/sip_credentials.json (gitignored!) with the username,
password, and connection ID for any softphone (Zoiper, Linphone, MicroSIP, Bria).
"""
import json
import os
import secrets
import string
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
load_dotenv()

key = os.getenv("TELNYX_ORGANIZATION_API_KEY")
if not key:
    print("ERROR: TELNYX_ORGANIZATION_API_KEY not set")
    sys.exit(1)

WEBHOOK = os.getenv("WEBHOOK_BASE_URL", "https://bk-jr-api.aixlabs.fun") + "/webhooks/telnyx"
HEADERS = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
TIMEOUT = 20


def gen_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(24))


suffix = str(int(time.time()))[-6:]
conn_name = f"W3J Principal SIP {suffix}"
user_name = f"w3jprincipal{suffix}"
password = gen_password()

print("=== Creating Credential Connection ===")
print(f"  name:      {conn_name}")
print(f"  user_name: {user_name}")
print(f"  password:  {password[:4]}...{password[-4:]} (24 chars, saved to file)")

r = httpx.post(
    "https://api.telnyx.com/v2/credential_connections",
    json={
        "connection_name": conn_name,
        "user_name": user_name,
        "password": password,
        "webhook_event_url": WEBHOOK,
        "webhook_event_failover_url": WEBHOOK,
        "webhook_api_version": "2",
        "active": True,
    },
    headers=HEADERS,
    timeout=TIMEOUT,
)
if r.status_code >= 300:
    print(f"FAIL: {r.status_code} {r.text[:400]}")
    sys.exit(1)
conn_data = r.json().get("data", {})
conn_id = conn_data.get("id")
print(f"  id: {conn_id}")

# Save credentials + connection info
creds = {
    "connection_id": conn_id,
    "connection_name": conn_name,
    "user_name": user_name,
    "password": password,
    "webhook_url": WEBHOOK,
    "softphone_setup": {
        "server": "sip.telnyx.com",
        "transport": "TLS",
        "port": 5061,
        "username": user_name,
        "password": password,
        "display_name": "W3J Principal",
        # Caller ID: assign a number later (see attach_sip_number)
        "caller_id": None,
    },
    "next_steps": {
        "1": "Top up Telnyx balance to $2+ (https://portal.telnyx.com/#/app/billing)",
        "2": "Or move an existing number: PATCH /v2/phone_numbers/{phone} {\"connection_id\": \"" + conn_id + "\"}",
        "3": "Configure softphone (Zoiper / Linphone / MicroSIP) with the credentials above",
        "4": "Test: call the assigned number — the softphone should ring",
        "5": "Update dispatcher/service.py PRINCIPAL_NUMBER to the new number (currently the user's cell)",
    },
}
out = Path(__file__).resolve().parent / "sip_credentials.json"
out.write_text(json.dumps(creds, indent=2))
print()
print(f"=== Credentials saved to {out} ===")
print()
print("=== Softphone setup (Zoiper, Linphone, MicroSIP, Bria) ===")
print(f"  Server:     sip.telnyx.com")
print(f"  Transport:  TLS")
print(f"  Port:       5061")
print(f"  Username:   {user_name}")
print(f"  Password:   {password}")
print(f"  Caller ID:  (assign a number first)")
print()
print("=== Next: assign a number ===")
print(f"  - Top up to $2+ at https://portal.telnyx.com/#/app/billing")
print(f"  - Then run:  python scripts/attach_sip_number.py  <connection_id>")
print(f"  - Or PATCH manually:")
print(f'    curl -X PATCH https://api.telnyx.com/v2/phone_numbers/+1XXXXXXXXXX \\')
print(f'      -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \\')
print(f'      -d \'{{"connection_id": "{conn_id}"}}\'')
