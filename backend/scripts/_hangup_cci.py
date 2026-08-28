"""Hang up a call by call_control_id."""
import os, sys, httpx
from dotenv import load_dotenv
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

key = os.environ.get("TELNYX_ORGANIZATION_API_KEY")
cci = sys.argv[1] if len(sys.argv) > 1 else "v3:OOwDvv2Aigf_vIpq1hAfectYCwPx8NBImLR1fZhCJ1s2nkrp7ElIvw"

print(f"Hanging up call {cci}")
r = httpx.post(
    f"https://api.telnyx.com/v2/calls/{cci}/actions/hangup",
    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    json={"command_id": f"hangup-{cci[:8]}"},
    timeout=10,
)
print(f"  HTTP {r.status_code}: {r.text[:200]}")
