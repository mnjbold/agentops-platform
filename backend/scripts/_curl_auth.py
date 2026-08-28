"""Direct curl-equivalent test of the org key."""
import os
import httpx
from dotenv import load_dotenv

load_dotenv()
org_key = os.getenv("TELNYX_ORGANIZATION_API_KEY")
api_key = os.getenv("TELNYX_API_KEY")

print(f"Org key: {org_key[:25]}... (length {len(org_key) if org_key else 0})")
print(f"API key (JWT): {api_key[:25]}... (length {len(api_key) if api_key else 0})")
print()

for label, key in [("ORG_KEY (Bearer)", org_key), ("API_KEY_JWT (Bearer)", api_key)]:
    print(f"--- {label} ---")
    r = httpx.get(
        "https://api.telnyx.com/v2/balance",
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        timeout=20,
    )
    print(f"  Status: {r.status_code}")
    print(f"  Body: {r.text[:400]}")
    print()

# Also try with X-API-KEY header
print("--- ORG_KEY (X-API-KEY header) ---")
r = httpx.get(
    "https://api.telnyx.com/v2/balance",
    headers={"X-API-KEY": org_key, "Accept": "application/json"},
    timeout=20,
)
print(f"  Status: {r.status_code}")
print(f"  Body: {r.text[:400]}")
