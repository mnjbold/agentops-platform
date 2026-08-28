"""W3J Telephony Platform — Telnyx MCP and connector package.

This package provides a full Model Context Protocol (MCP) server that exposes
the entire Telnyx platform (voice, messaging, AI assistants, numbers, webhooks,
recordings, conferences, queues, fax, verify, storage, networking) as MCP tools,
plus an autonomous agent builder that creates new Telnyx AI voice agents on
demand.

Public entry points:
- ``telnyx_mcp.server``  — the FastMCP server (stdio + http transports)
- ``telnyx_mcp.clients.telnyx_client`` — thin wrapper around the Telnyx SDK
- ``telnyx_mcp.tools``  — the 40+ MCP tools grouped by surface
- ``telnyx_mcp.utils.env`` — flexible env loader that picks the right key

Brand: W3J LLC (w3jdev.com)  |  Contact: gwadmin@w3jdev.com
"""
__version__ = "0.1.0"
__all__ = ["__version__"]
