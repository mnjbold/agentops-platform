"""Smoke test the MCP server by importing it in-process and calling
telnyx_health_check + telnyx_account_summary directly.

This bypasses stdio/HTTP transports and proves the tools are wired and
can actually hit the Telnyx API.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import telnyx_mcp.server
import telnyx_mcp.tools.utility
import telnyx_mcp.tools.numbers
import telnyx_mcp.tools.voice
import telnyx_mcp.tools.assistants
import telnyx_mcp.tools.infrastructure
import telnyx_mcp.tools.messaging

mcp = telnyx_mcp.server.mcp

# FastMCP stores tools internally; pull them out
print(f"Server: {mcp.name}")
print()

# Try to enumerate registered tools
try:
    tools_dict = mcp._tool_manager._tools if hasattr(mcp, "_tool_manager") else None
except Exception as e:
    tools_dict = None

if tools_dict is not None:
    print(f"Registered tools: {len(tools_dict)}")
    for name in sorted(tools_dict.keys()):
        print(f"  - {name}")
    print()
else:
    print("(could not enumerate tools directly)")

# Invoke health check directly through the tool function
# We import the tool modules and call the function directly
print("=== Direct invocation: telnyx_health_check ===")
from telnyx_mcp.tools.utility import telnyx_health_check
result = telnyx_health_check()
print(f"  result: {result}")
print()

print("=== Direct invocation: telnyx_account_summary ===")
from telnyx_mcp.tools.utility import telnyx_account_summary
result = telnyx_account_summary()
# Print a slimmed version
slim = {k: v for k, v in result.items() if k != "owned_numbers"}
for k, v in slim.items():
    print(f"  {k}: {v}")
print(f"  owned_numbers: [{len(result.get('owned_numbers', []))}]")
for n in result.get("owned_numbers", []):
    print(f"    - {n.get('phone_number')} | conn={n.get('connection_id')}")
print()

print("=== Direct invocation: telnyx_list_voice_clones ===")
from telnyx_mcp.tools.infrastructure import telnyx_list_voice_clones
clones = telnyx_list_voice_clones()
print(f"  voice clones: {len(clones)}")
for c in clones[:5]:
    print(f"    - {c.get('id')} | name={c.get('name', '?')}")
