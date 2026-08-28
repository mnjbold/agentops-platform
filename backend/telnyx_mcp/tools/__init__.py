"""Telnyx MCP tools package init.

Tools are auto-registered by the server from submodules of this package.
Each submodule registers its tools via ``@mcp.tool()`` against the shared
``mcp`` FastMCP instance imported from ``telnyx_mcp.server``.
"""
