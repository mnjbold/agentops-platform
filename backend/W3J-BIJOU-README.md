# W3J Telephony Platform

A Telnyx-backed platform for building, deploying, and selling AI voice agents
at scale. Owns the full surface: numbers, voice, AI assistants, recording,
messaging, webhooks, and a pluggable connector layer.

## Quick start

```bash
# Install
cd C:\Users\W3jde\local-projects\w3j-projects\telnyx
uv venv .venv --python 3.12 --seed
.venv\Scripts\Activate.ps1
uv pip install telnyx fastmcp fastapi 'uvicorn[standard]' python-dotenv pydantic httpx websockets

# Health check
python -m telnyx_mcp.utils.env
python -m telnyx_mcp.clients.telnyx_client

# Start MCP server (stdio — for Claude Desktop / Cursor / Windsurf)
python -m telnyx_mcp.server

# Start MCP server (HTTP — for any MCP client)
python -m telnyx_mcp.server --transport http --host 0.0.0.0 --port 8765

# Start webhook receiver
python -m webhooks --port 8080

# Deploy all agents
python scripts/deploy_all_agents.py --dry-run    # preview
python scripts/deploy_all_agents.py              # real deploy

# End-to-end smoke test
python scripts/smoke_test.py
```

## What's in here

| Path | What |
|------|------|
| `telnyx_mcp/` | The MCP server (30+ tools, full Telnyx surface) |
| `agent_builder/` | Autonomous agent deployer (YAML spec → live agent) |
| `webhooks/` | FastAPI receiver for Telnyx call events |
| `agents/` | 3 pre-built agent configs (W3J LLC / Bijou AI / personal twin) |
| `connectors/` | SQLite + Google Sheets + Supabase + WhatsApp + Telegram |
| `scripts/` | Deploy / smoke test / diagnostics |
| `docs/` | ARCHITECTURE, KNOWLEDGE_BASE, SALES, COMPLIANCE, API |

## Three pre-built agents (live demos)

| Agent | California area | What it does |
|-------|-----------------|--------------|
| W3J LLC Concierge | 213 (LA) | Answers for w3jdev.com, qualifies leads, takes messages |
| Bijou AI Concierge | 510 (Oakland) | Manglish voice, RM 299/mo pitch, agency reseller pivot |
| W3J Personal Twin | 213 (LA) | Screens calls, transfers to +60 112 111 3249 (Malaysia) |

## Reading order

1. **[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)** — what the system is
2. **[`docs/KNOWLEDGE_BASE.md`](docs/KNOWLEDGE_BASE.md)** — every Telnyx API we wrap
3. **[`docs/SALES.md`](docs/SALES.md)** — how to sell this
4. **[`docs/COMPLIANCE.md`](docs/COMPLIANCE.md)** — TCPA, GDPR, voice clone consent
5. **[`docs/API.md`](docs/API.md)** — connector / MCP / Python APIs

## License

W3J LLC — internal use. Not for redistribution.
