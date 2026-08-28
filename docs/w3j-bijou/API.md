# W3J Telephony Platform — Connector API

This is the **internal** API for the connector layer. MCP clients call the
MCP tools; Python clients call `telnyx_mcp.clients.telnyx_client`. The
"connector" API is the thin layer that sinks call events to external systems
(Sheets, Supabase, WhatsApp, Telegram).

---

## 1. The MCP API

The MCP server exposes ~30 tools. See `ARCHITECTURE.md` § 5 for the table.

### Boot
```bash
# stdio (Claude Desktop / Cursor / Windsurf / OpenCode / mavis)
python -m telnyx_mcp.server

# HTTP (any MCP client)
python -m telnyx_mcp.server --transport http --host 0.0.0.0 --port 8765
```

### Example tool call
```json
{
  "tool": "telnyx_search_available_numbers",
  "arguments": {
    "country_code": "US",
    "area_code": "213",
    "limit": 5
  }
}
```

Returns:
```json
[
  { "phone_number": "+12135550100", "cost_information": {...}, "features": [...] },
  ...
]
```

---

## 2. The Python client API

```python
from telnyx_mcp.clients.telnyx_client import get_client

c = get_client()

# Numbers
c.search_available_numbers(country_code="US", area_code="213", limit=5)
c.order_numbers(["+12135550100"])
c.list_owned_numbers()
c.get_number("+12135550100")
c.update_number("+12135550100", connection_id="30059...")

# AI Assistants
c.list_assistants()
c.create_assistant(
    name="My Concierge",
    instructions="You are a friendly receptionist...",
    voice="Telnyx.KokoroTTS.af_heart",
)
c.update_assistant(assistant_id="assistant-...", instructions="...")
c.delete_assistant(assistant_id="assistant-...")

# Voice
c.dial(to="+1555...", from_="+1213...", connection_id="...")
c.transfer_call(call_control_id="...", to="+601121113249")
c.hangup_call(call_control_id="...")
c.answer_call(call_control_id="...")
c.start_ai_assistant(call_control_id="...", assistant_id="assistant-...")

# Infrastructure
c.list_call_control_apps()
c.create_call_control_app(application_name="W3J Concierge", webhook_event_url="https://...")
c.list_outbound_profiles()
c.list_messaging_profiles()
c.list_voice_clones()
c.list_voice_designs()
c.list_recordings()

# SMS
c.send_sms(from_="+1213...", to="+1555...", text="Hello from W3J AI")
```

---

## 3. The agent builder API

```python
from agent_builder.builder import AgentBuilder, AgentSpec, build_agent

# From a YAML file
spec = AgentSpec.from_yaml("agents/w3j-llc-concierge/spec.yaml")

# From a dict
spec = AgentSpec(
    name="My Agent",
    instructions="You are ...",
    country_code="US",
    area_code="213",
    buy_number=True,
    webhook_url="https://your-server/webhooks/telnyx",
)

# Deploy
result = build_agent(spec)
print(result["assistant"]["id"], result["phone_number"], result["routing_added"])
```

The result dict contains:
- `assistant` — created/updated AI Assistant dict
- `call_control_app` — created/reused Call Control App
- `phone_number` — the new E.164 (if buy_number=True)
- `number_updated` — the updated PhoneNumber record
- `routing_added` — boolean, was the routing map updated
- `errors` — list of error messages (empty on success)

---

## 4. The webhook API

### POST /webhooks/telnyx
Receives Telnyx events. Body shape:
```json
{
  "event_type": "call.initiated",
  "data": {
    "event_type": "call.initiated",
    "occurred_at": "...",
    "payload": {
      "call_control_id": "...",
      "direction": "incoming",
      "from": {"phone_number": "+15551234567"},
      "to": {"phone_number": "+12135551234"}
    }
  }
}
```

Returns:
```json
{ "handled": true, "method": "event_call_initiated", "result": "logged" }
```

### GET /admin/routing
Returns the current routing map:
```json
{ "routing": {"+12135551234": "assistant-..."}, "count": 1 }
```

### POST /admin/routing
Body: `{"routing": {"+12135551234": "assistant-..."}}` — replaces the map.

### GET /health
Returns: `{"ok": true, "service": "w3j-telephony-webhooks", "routing_count": N}`

---

## 5. The connector API

```python
from connectors.registry import get_registry
from connectors.base import CallEvent

reg = get_registry()

# Every call event lands in every enabled connector
event = CallEvent(
    event_type="call.answered",
    call_control_id="...",
    agent_id="assistant-...",
    direction="incoming",
    from_number="+15551234567",
    to_number="+12135551234",
    notes="started_assistant:assistant-...",
)
written = reg.write_event(event)
print(f"Written to: {written}")  # e.g. ["sqlite", "google_sheets"]
```

Connectors automatically enabled when their env vars are present:
- SQLite (always)
- Google Sheets (if `GOOGLE_SHEETS_CREDENTIALS_PATH` + `GOOGLE_SHEETS_SPREADSHEET_ID`)
- Supabase (if `SUPABASE_URL` + `SUPABASE_SERVICE_KEY`)
- WhatsApp (if `WHATSAPP_BUSINESS_ACCOUNT_ID` + `WHATSAPP_ACCESS_TOKEN`)
- Telegram (if `TELEGRAM_BOT_TOKEN`)

Health check: `reg.health()` returns `{name: bool}`.

---

## 6. Add a new MCP tool

```python
# telnyx_mcp/tools/my_new_surface.py
from telnyx_mcp.server import mcp
from telnyx_mcp.clients.telnyx_client import get_client

@mcp.tool()
def telnyx_my_new_tool(arg1: str, arg2: int = 10) -> dict:
    """Description shown in the MCP client."""
    return get_client().api.<resource>.<method>(...)
```

Then add the import to `telnyx_mcp/server.py`:
```python
import telnyx_mcp.tools.my_new_surface  # noqa: E402, F401
```

The tool is auto-registered.

---

## 7. Add a new connector

```python
# connectors/my_new_sink.py
from connectors.base import CallEvent
import os

class MyNewSink:
    name = "my_new_sink"

    def __init__(self):
        self.token = os.getenv("MY_NEW_SINK_TOKEN")

    def is_healthy(self):
        return bool(self.token)

    def write_event(self, event: CallEvent) -> bool:
        # ... write to your backend ...
        return True

    def write_lead(self, lead: dict) -> bool:
        return True
```

Add to `connectors/registry.py`:
```python
from connectors.my_new_sink import MyNewSink
self._connectors.append(MyNewSink())
```

Restart the webhook server. Done.

---

## 8. Add a new agent

```bash
mkdir agents/my-new-agent
```

Create `agents/my-new-agent/spec.yaml`:
```yaml
name: My New Agent
instructions: |
  You are ...
country_code: US
area_code: "415"
buy_number: true
webhook_url: https://your-server/webhooks/telnyx
model: openai/gpt-4o
voice: Telnyx.KokoroTTS.af_heart
```

Deploy:
```bash
python scripts/deploy_all_agents.py --only my-new-agent
```

The agent is live, the number is assigned, the routing is registered.

---

## 9. Production checklist

- [ ] `WEBHOOK_BASE_URL` points to a public HTTPS endpoint (e.g.
      `https://bk-jr-api.aixlabs.fun/webhooks/telnyx`)
- [ ] `WEBHOOK_SIGNING_SECRET` set and HMAC verification implemented
      in `webhooks/server.py`
- [ ] Telnyx Call Control App's `webhook_event_url` matches `WEBHOOK_BASE_URL`
- [ ] Voice clone consent on file (if using clone)
- [ ] System prompts include TCPA / GDPR / AI Act disclosures
- [ ] DNC list in the SQLite sink, reviewed weekly
- [ ] Recording consent announcement in every system prompt
- [ ] Telnyx account has $50+ balance for testing
- [ ] Daily cron checks `telnyx_get_balance` and alerts if < $5
- [ ] Uptime monitoring on `/health` endpoint of webhook receiver
