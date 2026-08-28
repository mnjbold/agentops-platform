# W3J Telephony Platform — Architecture

A Telnyx-backed platform for building, deploying, and selling AI voice agents
at scale. Built at `C:\Users\W3jde\local-projects\w3j-projects\telnyx`.

---

## 1. What this is

A complete telephony platform that:

1. Exposes the **entire Telnyx API** as a Model Context Protocol (MCP) server
   that any MCP-compatible client (Claude Desktop, Cursor, Windsurf, OpenCode,
   our own portal) can call as native tools.
2. Runs an **autonomous agent builder** that takes a YAML/JSON spec and
   deploys a complete voice agent (number + call control app + AI Assistant
   + routing map) in one call.
3. Hosts a **webhook receiver** that auto-starts the right AI Assistant when
   any inbound call lands on any of our numbers.
4. Plugs into a **registry of connectors** (SQLite always-on, plus Google
   Sheets, Supabase, WhatsApp, Telegram when their env vars are present).
5. Ships with **three pre-built agents** for the user's own businesses
   (W3J LLC, Bijou AI, W3J personal twin) — these are the demos and the
   first customers.

The intent is to be a **telephony platform as a service** that W3J can
sell to clients: "Send us your brand voice and your call flow, we ship
a production AI agent by Friday."

---

## 2. Repository layout

```
telnyx/
├── .env                            # Telnyx creds (org key + public key)
├── README.md                       # you are here (ARCHITECTURE.md)
├── docs/
│   ├── ARCHITECTURE.md             # this file
│   ├── KNOWLEDGE_BASE.md           # every Telnyx API we wrap, with links
│   ├── SALES.md                    # how to sell this, pricing, scripts
│   ├── COMPLIANCE.md               # TCPA, GDPR, voice-clone consent
│   └── API.md                      # our connector API
├── telnyx_mcp/                     # ← THE MCP SERVER
│   ├── server.py                   # FastMCP, entry point
│   ├── clients/telnyx_client.py    # SDK wrapper, 25+ helpers
│   ├── tools/                      # 30+ MCP tools
│   │   ├── numbers.py              # search, buy, configure
│   │   ├── voice.py                # dial, transfer, answer, hangup
│   │   ├── assistants.py           # AI Assistant CRUD
│   │   ├── infrastructure.py       # call control apps, profiles
│   │   ├── messaging.py            # SMS
│   │   └── utility.py              # balance, health, summary
│   └── utils/env.py                # flexible env loader
├── agent_builder/                  # ← AUTONOMOUS AGENT BUILDER
│   ├── builder.py                  # AgentBuilder class + AgentSpec
│   └── templates/                  # (future: scaffold new agents)
├── webhooks/                       # ← CALL EVENT RECEIVER
│   ├── server.py                   # FastAPI app
│   └── handlers/                   # dispatch by event type
│       ├── base.py                 # base handler
│       └── default.py              # routes to AI assistant, writes to sinks
├── agents/                         # ← PRE-BUILT AGENT CONFIGS
│   ├── w3j-llc-concierge/          # w3jdev.com's AI receptionist
│   ├── bijou-ai-concierge/         # mybijou.xyz's voice concierge
│   └── w3j-personal-twin/          # calls screen + transfer to +60 112 111 3249
├── connectors/                     # ← PLUG-IN DATA SINKS
│   ├── base.py                     # CallEvent dataclass + protocol
│   ├── sqlite.py                   # always-on local fallback
│   ├── google_sheets.py            # write to a spreadsheet
│   ├── supabase.py                 # write to Supabase (Bijou multi-tenant)
│   ├── whatsapp.py                 # Telnyx WhatsApp Business
│   ├── telegram.py                 # Telegram bot (chat interface to agent)
│   └── registry.py                 # fan-out writer
├── portal/                         # (next session) Composio-style UI
├── scripts/
│   ├── deploy_all_agents.py        # one-shot deploy for the 3 agents
│   ├── smoke_test.py               # end-to-end boot test
│   └── _probe_*.py                 # diagnostic scripts (kept for re-runs)
└── tests/                          # (next session) pytest suite
```

---

## 3. The data flow (one inbound call)

```
caller dials +1 (213) 555-1234  (W3J LLC's CA number)
        │
        ▼
Telnyx PSTN → Call Control App (webhook: https://your-host/webhooks/telnyx)
        │  event: call.initiated
        ▼
webhooks/server.py → DefaultEventHandler.event_call_initiated
        │
        ├── writes to SQLite, Sheets, Supabase (configured sinks)
        │
        ▼  (on call.answered)
event_call_answered
        │
        ├── looks up agent_routing[+12135551234] → assistant_id
        ├── calls client.start_ai_assistant(call_control_id, assistant_id)
        └── AI Assistant takes over the audio (TTS + STT)
                │
                ▼
        assistant follows its system_prompt:
          - greets
          - answers
          - on "transfer to Nurun" → tools.transfer_call → +60 112 111 3249
          - on "goodbye" → hangup_call
        │
        ▼  (on call.hangup / recording.ready)
event_call_hangup / event_recording_ready
        │
        ├── writes to all sinks
        └── post-call summary to a Google Sheet for the client
```

---

## 4. The data flow (one agent spec → one live agent)

```
$ python scripts/deploy_all_agents.py --only w3j-llc-concierge

  AgentBuilder.build(spec)
    │
    ├── 1. _find_existing_assistant("W3J LLC Concierge")
    │     - if found → update instructions
    │     - else      → create_assistant(...)
    │
    ├── 2. _upsert_call_control_app(name, webhook_url)
    │     - if found → reuse
    │     - else      → create_call_control_app(...)
    │
    ├── 3. _provision_number(spec)
    │     - if spec.specific_number → use it
    │     - elif spec.buy_number     → search + order_numbers([pick])
    │     - else                     → skip
    │
    ├── 4. update_number(phone, connection_id=app.id)
    │
    └── 5. add_routing(phone, assistant_id)  # persisted to routing.json
```

All steps are **idempotent on the agent name** — re-run any time to update
the instructions or model without losing the number.

---

## 5. The MCP server

The server exposes 30+ tools grouped by surface:

| Surface             | Tools                                                                          |
|---------------------|--------------------------------------------------------------------------------|
| `numbers`           | `telnyx_search_available_numbers`, `telnyx_order_numbers`, `telnyx_list_owned_numbers`, `telnyx_get_number`, `telnyx_update_number` |
| `voice`             | `telnyx_dial`, `telnyx_transfer_call`, `telnyx_hangup_call`, `telnyx_answer_call`, `telnyx_reject_call`, `telnyx_start_ai_assistant`, `telnyx_stop_ai_assistant`, `telnyx_list_recordings` |
| `assistants`        | `telnyx_list_assistants`, `telnyx_get_assistant`, `telnyx_create_assistant`, `telnyx_update_assistant`, `telnyx_delete_assistant` |
| `infrastructure`    | `telnyx_list_call_control_apps`, `telnyx_create_call_control_app`, `telnyx_list_outbound_voice_profiles`, `telnyx_create_outbound_voice_profile`, `telnyx_list_messaging_profiles`, `telnyx_create_messaging_profile`, `telnyx_list_voice_clones`, `telnyx_list_voice_designs` |
| `messaging`         | `telnyx_send_sms` |
| `utility`           | `telnyx_get_balance`, `telnyx_account_summary`, `telnyx_health_check` |

Run it:
```bash
# stdio (Claude Desktop / Cursor / Windsurf / OpenCode)
python -m telnyx_mcp.server

# HTTP (remote clients)
python -m telnyx_mcp.server --transport http --host 0.0.0.0 --port 8765
```

Or via `uvx` (per the official Telnyx MCP pattern):
```bash
uvx --from . telnyx-mcp-server
```

---

## 6. The webhook receiver

Single FastAPI app on `webhooks/server.py`:
- `POST /webhooks/telnyx` — main event receiver
- `GET  /health`           — health
- `GET  /admin/routing`    — current agent routing
- `POST /admin/routing`    — set routing
- `POST /admin/test_event` — dev-only synthetic event

Runs on:
```bash
python -m webhooks --port 8080 --host 0.0.0.0
```

Routing map persists to `routing.json` in the project root, survives restarts.

---

## 7. The connector registry

Every event is fanned out to all enabled connectors. Always-on:

- **SQLite** (`w3j_telephony.db` in the project root) — never loses data.

Optional (set env vars in `.env`):

- **Google Sheets** — `GOOGLE_SHEETS_CREDENTIALS_PATH` + `GOOGLE_SHEETS_SPREADSHEET_ID`
- **Supabase** — `SUPABASE_URL` + `SUPABASE_SERVICE_KEY`
- **WhatsApp** — `WHATSAPP_BUSINESS_ACCOUNT_ID` + `WHATSAPP_ACCESS_TOKEN`
- **Telegram** — `TELEGRAM_BOT_TOKEN`

The first 4 are sinks (write `CallEvent`s). WhatsApp and Telegram are also
channels (send outbound messages / receive commands).

---

## 8. The three pre-built agents

| Agent               | California area | Greeting                          | Transfer to            |
|---------------------|-----------------|-----------------------------------|------------------------|
| W3J LLC Concierge   | 213 (LA)        | "Thanks for calling W3J LLC…"     | collects lead, no live transfer |
| Bijou AI Concierge  | 510 (Oakland)   | "Hello boss, Bijou AI concierge…" | no live transfer, takes message |
| W3J Personal Twin   | 213 (LA)        | "Hey, this is Nurun's AI assistant. What's up?" | **+60 112 111 3249** (Malaysia) |

Each lives at `agents/<name>/`:
- `spec.yaml` — AgentSpec (consumed by the agent builder)
- `knowledge.json` — structured facts the agent can reference
- `test_scenarios.md` — manual test cases for verifying the agent

Deploy all three:
```bash
python scripts/deploy_all_agents.py           # full deploy (asks confirmation)
python scripts/deploy_all_agents.py --dry-run # show what would happen
```

---

## 9. Environment variables (`.env`)

| Variable | Required? | Notes |
|----------|-----------|-------|
| `TELNYX_ORGANIZATION_API_KEY` | **Yes** (preferred) | V2 org key (`KEY019...`), full access. |
| `TELNYX_API_KEY`             | Yes if no org key | JWT scoped to one product (e.g. `ie_model`). |
| `TELNYX_PUBLIC_API_KEY`      | No              | Public/embed-only. |
| `GOOGLE_SHEETS_CREDENTIALS_PATH` | No        | Service account JSON. |
| `GOOGLE_SHEETS_SPREADSHEET_ID` | No          | Google Sheet to write call events to. |
| `SUPABASE_URL`               | No              | For Bijou multi-tenant integration. |
| `SUPABASE_SERVICE_KEY`       | No              | Service role key (bypasses RLS — server-only). |
| `OPENAI_API_KEY`             | No              | For ad-hoc OpenAI calls. |
| `ELEVENLABS_API_KEY`         | No (next session) | For voice cloning (W3J personal twin). |
| `CARTESIA_API_KEY`           | No (next session) | For real-time voice cloning. |
| `TELEGRAM_BOT_TOKEN`         | No              | For the personal twin's chat interface. |
| `WHATSAPP_BUSINESS_ACCOUNT_ID` | No            | For WhatsApp Business messages. |
| `WHATSAPP_ACCESS_TOKEN`       | No              | Long-lived WhatsApp token. |
| `WEBHOOK_BASE_URL`           | No              | Public base URL for Telnyx webhooks. |

---

## 10. What's deferred to next session

1. **Voice cloning** for the personal twin (ElevenLabs or Cartesia integration). MVP uses Telnyx built-in voice.
2. **Connector portal** — a `portal/` web app (FastAPI + static HTML) that lets the user manage agents, connectors, and view call events in a browser. Login-gated.
3. **Bijou AI ↔ Telnyx deep integration** — wire call features into the existing Bijou multi-tenant Supabase backend.
4. **pytest suite** — currently only ad-hoc `_probe_*.py` scripts.
5. **Voice clone download** — for offline batch generation of pre-canned responses.
6. **Multi-language voice tests** — Manglish, Bahasa Malaysia, etc.
7. **Conference bridging** for multi-party calls.
