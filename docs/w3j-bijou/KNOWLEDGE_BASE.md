# Telnyx Knowledge Base — what we wrap and how

Every Telnyx surface this platform uses, with the SDK call, the API path,
and the gotchas. The MCP server is the unified surface; this is the reference.

Last verified: 2026-07-31 against Telnyx Python SDK v4.170.0.

---

## Authentication

**Org key (preferred):**
```
TELNYX_ORGANIZATION_API_KEY=KEY<your-org-key-here>
```
Used as `Authorization: Bearer <key>` on every request. SDK v4 requires
passing the key to the constructor (`Telnyx(api_key=...)`), not just
`telnyx.api_key = ...`. Get your key from https://portal.telnyx.com/#/api-keys.

**JWT key (for inference only):**
```
TELNYX_API_KEY=eyJhbGc...  # scope: ie_model
```
Works for `/v2/ai/...` endpoints but NOT for the rest. The SDK uses
`http.auth_headers` and refuses if there's no constructor key.

**Verification:**
```python
c = get_client()
print(c.api.balance.retrieve())
```

---

## Phone numbers

### Search inventory
`GET https://api.telnyx.com/v2/available_phone_numbers?filter[country_code]=US&filter[national_destination_code]=213`
- Country code required, area code optional, locality optional, features optional.
- Returns up to N available numbers with cost.

```python
avail = client.available_phone_numbers.list(
    filter={"country_code": "US", "national_destination_code": "213"}
)
```

### Buy
`POST /v2/number_orders` with `{"phone_numbers": [{"phone_number": "+12135551234"}]}`
- Returns the order; number becomes active within seconds.
- $1/number one-time + $1-3/month per number.

```python
order = client.number_orders.create(phone_numbers=["+12135551234"])
```

### Configure
`PATCH /v2/phone_numbers/{id}` with `{"connection_id": "...", "messaging_profile_id": "..."}`
- `connection_id` = the call control app's connection_id
- `messaging_profile_id` = the messaging profile's id

```python
client.phone_numbers.update("+12135551234", connection_id=app_id)
```

### California area codes (verified live)
- **213** — Los Angeles
- **510** — Oakland / East Bay
- **415 / 628** — San Francisco
- **341** — Fremont (overlay of 510)
- **925** — Pittsburg West
- **951** — Riverside

---

## Voice: call control

All commands take a `call_control_id` returned by Dial or a webhook.

### Dial (place outbound call)
`POST /v2/calls`
```json
{
  "to": "+15551234567",
  "from": "+12135551234",
  "connection_id": "3005971750132385535"
}
```
- Returns `call_leg_id` for correlating webhooks.
- `answering_machine_detection`: "basic" or "premium".
- `client_state`: opaque state, returned in webhooks.

### Answer / Hangup / Reject
`POST /v2/calls/{call_control_id}/actions/answer`
`POST /v2/calls/{call_control_id}/actions/hangup`
`POST /v2/calls/{call_control_id}/actions/reject`

### Transfer (the killer feature for the personal twin)
`POST /v2/calls/{call_control_id}/actions/transfer`
```json
{ "to": "+601121113249", "from_": "+12135551234" }
```
- **$0.10 surcharge per transfer**
- Can be SIP REFER (no Telnyx in audio path after bridge) or Telnyx-mediated.
- `time_limit_secs`: max call duration after transfer.

### Bridge
`POST /v2/calls/{call_control_id}/actions/bridge` with the other `call_control_id`.

### Speak
`POST /v2/calls/{call_control_id}/actions/speak` with `{"payload": "text", "voice": "female", "language": "en-US"}`

### Gather (DTMF / speech)
`POST /v2/calls/{call_control_id}/actions/gather` — collect digits or speech.
`POST /v2/calls/{call_control_id}/actions/gather_using_ai` — semantic gather via LLM.

### Recording
`POST /v2/calls/{call_control_id}/actions/record_start` / `record_stop`.
- Webhook `recording.ready` gives the URL.
- Storage: Telnyx-hosted (free for 30 days) or custom (S3, GCS).

### Streaming
`POST /v2/calls/{call_control_id}/actions/streaming_start` — stream audio to a websocket.

---

## AI Assistants (the core product)

### CRUD
- `POST /v2/ai/assistants` — create
- `GET /v2/ai/assistants` — list
- `GET /v2/ai/assistants/{id}` — retrieve
- `PATCH /v2/ai/assistants/{id}` — update
- `DELETE /v2/ai/assistants/{id}` — delete

### Key fields
- `name` — friendly name (used for idempotent deploys)
- `instructions` — the system prompt (rich text, supports Jinja)
- `model` — `"openai/gpt-4o"`, `"anthropic/claude-3-5-sonnet"`, `"moonshotai/Kimi-K2.5"`, etc.
- `voice` — TTS voice ID
- `transcription.engine` — `"deepgram/nova-3"` (default)
- `tools` — array of tool defs (transfer, hangup, custom webhook, etc.)
- `dynamic_variables.webhook_url` — for client-side variable injection
- `telephony_settings` — caller ID, recording, etc.

### Voices
- Built-in: `Telnyx.KokoroTTS.af_heart` (warm female), `am_adam` (male), `bf_emma` (British female)
- AWS Polly: `AWS.Polly.Joanna`, `AWS.Polly.Matthew`
- Azure: `Azure.en-US-JennyNeural`
- Custom: `telnyx_create_voice_clone` → use the returned voice_id

### Start / stop mid-call
`POST /v2/calls/{call_control_id}/actions/ai_assistant_start`
`POST /v2/calls/{call_control_id}/actions/ai_assistant_stop`

### TeXML: start assistant via TeXML
```xml
<Response>
  <Connect>
    <AIAssistant id="assistant-..." />
  </Connect>
</Response>
```

### Outbound via TeXML
```
POST /v2/texml/ai_calls
{
  "from": "+12135551234",
  "to": "+15551234567",
  "assistant_id": "assistant-...",
  "connection_id": "..."
}
```

### Deepgram STT
- Now available across TeXML and Voice API (Oct 2025).
- Engine: `deepgram/nova-2` or `deepgram/nova-3`.
- 30+ languages and dialects.

---

## Voice clones / designs

### Create voice clone (upload audio)
`POST /v2/voice_clones` with multipart form data.
- Audio: 1-5 min of clean speech (WAV/MP3).
- Returns a voice_id usable as `voice:` in an assistant.

### Voice designs (text-described)
`POST /v2/voice_designs` with `{"prompt": "warm British female, mid-30s"}`
- Returns a voice_id after generation (~1 min).
- Multiple "versions" can be tested before committing.

```python
client.voice_clones.create_from_upload(audio_path="...", name="W3J Twin")
client.voice_designs.create(prompt="...", name="Test voice 1")
```

---

## Messaging

### Send SMS
`POST /v2/messages`
```json
{
  "from": "+12135551234",
  "to": "+15551234567",
  "text": "Hello from W3J AI"
}
```

### WhatsApp Business
`POST /v2/whatsapp/messages` with Telnyx-hosted WhatsApp Business account.

---

## Webhooks

### Telnyx → our server events
- `call.initiated` — outbound or inbound call started
- `call.answered` — call picked up
- `call.hangup` — call ended
- `call.bridged` — two legs connected
- `call.machine.detection.ended` — AMD result (if enabled)
- `call.ai_assistant.started` / `.ended` — AI assistant lifecycle
- `recording.ready` — recording URL available
- `message.received` — inbound SMS/MMS/WhatsApp
- `message.sent` / `message.delivered` — outbound lifecycle

### Our server → Telnyx command API
`POST /v2/calls/{id}/actions/...` — we issue commands in response to events.

### Webhook signing (for production)
- Telnyx signs webhooks with HMAC-SHA256.
- `WEBHOOK_SIGNING_SECRET` env var + signature verification.
- Currently NO verification in the dev webhook (TODO for production).

---

## MCP server vs SDK vs raw API

| Need | Use |
|------|-----|
| Quick action from an AI client | **MCP tool** (e.g. `telnyx_dial(...)`) |
| Reusable Python code | `clients/telnyx_client.py` (25+ helpers) |
| One-off or advanced | `client.api.<resource>.<method>(...)` (150+ resources) |
| Custom integration | Direct REST via `httpx` with `Authorization: Bearer <key>` |

---

## Pricing (as of 2026-07-31)

- **Numbers**: $1 one-time + $1-3/month per number (CA local $1/mo)
- **Inbound voice**: ~$0.005/min
- **Outbound voice**: ~$0.01/min US, varies by destination
- **AI Assistant**: per-minute, varies by model. Typical gpt-4o: $0.05-0.10/min
- **Recording**: $0.002/min
- **Transfer**: $0.10 per transfer
- **SMS**: ~$0.01 per segment
- **MMS**: ~$0.02 per message
- **Voice clones**: free to create, no per-use fee
- **Voice designs**: free, but design generation is ~1 min compute

**A typical 5-min AI call with transfer**: $0.05 (inbound) + $0.50 (5 min AI) + $0.10 (transfer) = ~$0.65

---

## Reference links

- Telnyx docs: https://developers.telnyx.com/docs/overview
- API reference: https://developers.telnyx.com/api-reference/overview
- Telnyx official MCP server: https://github.com/team-telnyx/telnyx-mcp-server
- Telnyx TeXML AI Assistant: https://developers.telnyx.com/docs/inference/ai-assistants/no-code-voice-assistant
- Voice API commands: https://developers.telnyx.com/docs/voice/programmable-voice/voice-api-commands-and-resources
- Deepgram STT integration: https://telnyx.com/release-notes/deepgram-stt-voice-api-texml
- SIP Refer transfers: https://telnyx.com/release-notes/transfer-calls-with-sip-refer-live
