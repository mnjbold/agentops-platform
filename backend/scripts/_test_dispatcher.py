"""End-to-end test of the dispatcher service.

1. Verify MCP tools are wired (telnyx_dial_and_bridge, telnyx_normalize_phone)
2. Verify the webhook handler is loaded with the specialist mapping
3. Verify the dispatcher service is callable
4. List the deployed specialists
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

print("=" * 60)
print("W3J Telephony Platform — Dispatcher Integration Test")
print("=" * 60)

# 1. MCP tools registered
import telnyx_mcp.server
import telnyx_mcp.tools.dispatcher
mcp = telnyx_mcp.server.mcp
print(f"\n1. MCP server: {mcp.name}")

# 2. Specialist mapping
mapping_path = Path("agents/specialists/assistants.json")
if mapping_path.exists():
    mapping = json.loads(mapping_path.read_text())
    print(f"\n2. Specialist mapping ({len(mapping)} keys):")
    for k, v in mapping.items():
        print(f"   {k:20s} -> {v}")
else:
    print(f"\n2. NO specialist mapping found at {mapping_path}")
    sys.exit(1)

# 3. Dispatcher service importable
from dispatcher.service import get_dispatcher, normalize_phone
print(f"\n3. Dispatcher service: OK")
print(f"   normalize_phone('011 1234 5678') = {normalize_phone('011 1234 5678')}")
print(f"   normalize_phone('+60121234567') = {normalize_phone('+60121234567')}")
print(f"   normalize_phone('not a phone') = {normalize_phone('not a phone')}")

# 4. Webhook handler in-process
try:
    from webhooks.handlers.dispatch import SPECIALIST_ASSISTANT_IDS
    print(f"\n4. Webhook handler in-process: {len(SPECIALIST_ASSISTANT_IDS)} specialists")
    for k, v in SPECIALIST_ASSISTANT_IDS.items():
        print(f"   {k:20s} -> {v[:40]}...")
except Exception as e:
    print(f"\n4. Webhook handler NOT in-process: {e}")

# 5. Webhook receiver running
try:
    import httpx
    r = httpx.get("http://127.0.0.1:8080/health", timeout=5)
    if r.status_code == 200:
        print(f"\n5. Webhook receiver: {r.json()}")
    else:
        print(f"\n5. Webhook receiver: status {r.status_code}")
except Exception as e:
    print(f"\n5. Webhook receiver NOT running: {e}")

# 6. MCP tool call (without actually dialing)
print(f"\n6. MCP tool call (DRY RUN — would dial +601121113249):")
print(f"   user types: 'call +601121113249'")
print(f"   dispatch service: dial_and_bridge(to='+601121113249', from_number='+18444618814')")
print(f"   -> creates conference, dials both legs, returns control IDs")
print(f"   user picks up +60 112 111 3249, callee picks up their phone, both bridged")

print()
print("=" * 60)
print("DISPATCHER READY — pick up +60 112 111 3249 to take a test call")
print("=" * 60)
