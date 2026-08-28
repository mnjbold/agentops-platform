"""Telnyx MCP server — exposes the entire Telnyx platform as MCP tools.

Usage (stdio transport, for Claude Desktop):
    uvx --from . telnyx-mcp-server

Or directly:
    python -m telnyx_mcp.server

Usage (streamable HTTP transport, for remote MCP clients):
    python -m telnyx_mcp.server --transport http --port 8765
"""
from __future__ import annotations

import argparse
import logging
import sys
from typing import Literal

from fastmcp import FastMCP

from telnyx_mcp.utils.env import load_integrations, load_telnyx_creds

log = logging.getLogger(__name__)

# Single shared FastMCP instance — all tool modules attach to it
mcp = FastMCP(
    name="W3J Telephony Platform (Telnyx)",
    instructions="""
You are connected to the W3J Telephony Platform — a Telnyx-backed system for
building, deploying, and managing AI voice agents at scale.

## What you can do
- **Numbers**: search, buy, configure US/CA/MY/GB numbers (try California area
  codes 213/510/415/341 for local presence)
- **Voice**: dial out, transfer calls, answer, hangup, reject, start/stop AI
  Assistant on a live call
- **AI Assistants**: CRUD voice assistants with custom instructions, model,
  voice, and tools
- **Infrastructure**: Call Control Apps, Outbound Voice Profiles, Messaging
  Profiles (the routing layers)
- **Voice cloning**: list custom voice clones and voice designs for TTS
- **Messaging**: send SMS from any owned number
- **Recordings**: list call recordings

## Always start with telnyx_health_check
Run it before any state-changing operation to confirm credentials work.

## When the user asks for a new agent
1. telnyx_search_available_numbers → pick a number
2. telnyx_order_numbers → buy it
3. telnyx_create_call_control_app → webhook to your server
4. telnyx_create_assistant → with detailed system prompt
5. telnyx_update_number → point the number at the call control app

## When the user asks to test
Use telnyx_dial to call a real number (e.g. your cell) from an agent's
number. The webhook events will stream to your call control app.

## MCP tools available: see tools/ package.
""",
)

# Import all tool modules so their @mcp.tool() decorators register.
# Order matters only for readability; mcp.tool() is just registration.
import telnyx_mcp.tools.utility  # noqa: E402, F401
import telnyx_mcp.tools.numbers  # noqa: E402, F401
import telnyx_mcp.tools.voice  # noqa: E402, F401
import telnyx_mcp.tools.assistants  # noqa: E402, F401
import telnyx_mcp.tools.infrastructure  # noqa: E402, F401
import telnyx_mcp.tools.messaging  # noqa: E402, F401
import telnyx_mcp.tools.dispatcher  # noqa: E402, F401


def main() -> int:
    parser = argparse.ArgumentParser(
        description="W3J Telephony Platform — Telnyx MCP server"
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http", "sse"),
        default="stdio",
        help="Transport to expose MCP on (default: stdio for Claude Desktop)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for http/sse transports (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Port for http/sse transports (default: 8765)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Sanity check on boot — fail loud if creds are missing/wrong scope
    creds = load_telnyx_creds()
    integrations = load_integrations()
    log.info(
        "Telnyx MCP server starting | key=%s type=%s full_access=%s",
        creds.source, creds.key_type, creds.is_full_access,
    )
    if integrations.enabled:
        log.info("Integrations wired: %s", ", ".join(integrations.enabled))
    if not creds.is_full_access:
        log.warning(
            "Running with non-full-access key (%s). "
            "Most write operations will fail with 401.",
            creds.key_type,
        )

    transport: Literal["stdio", "http", "sse"] = args.transport
    if transport == "stdio":
        mcp.run(transport="stdio")
    elif transport == "http":
        # streamable-http
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
    else:  # sse
        mcp.run(transport="sse", host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
