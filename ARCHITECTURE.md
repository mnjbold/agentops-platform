# Architecture

System design + the key decisions we made and why. Read this before proposing
changes that touch the architecture.

---

## Bird's-eye view

```
┌──────────────────────────────────────────────────────────────────────┐
│                          Browser (PWA)                                │
│  ┌────────┐  ┌─────────┐  ┌──────────┐  ┌─────────┐  ┌──────────────┐│
│  │Dialer  │  │Messages │  │ History  │  │Campaigns│  │  Recordings  ││
│  └────────┘  └─────────┘  └──────────┘  └─────────┘  └──────────────┘│
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │  WebRTC client (Telnyx SDK) + WS subscriber for real-time    │    │
│  └──────────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────────┘
              │ HTTPS (REST)            │ WSS (events)
              ▼                          ▼
┌──────────────────────────────────────────────────────────────────────┐
│                   Cloudflare Tunnel (bk-jr-api.aixlabs.fun)          │
└──────────────────────────────────────────────────────────────────────┘
              │
              ▼
┌──────────────────────────────────────────────────────────────────────┐
│                       FastAPI (uvicorn, 1 worker)                     │
│  ┌──────────────────┐  ┌────────────────────┐  ┌────────────────────┐  │
│  │  Dashboard API    │  │ Webhook receiver   │  │ WS broker           │  │
│  │  /api/* (30+     │  │  POST              │  │  in-process pubsub  │  │
│  │  endpoints)       │  │  /webhooks/telnyx  │  │                     │  │
│  └──────────────────┘  └────────────────────┘  └────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │  SaaS scheduler thread (in-process; future: Redis-locked)    │    │
│  └──────────────────────────────────────────────────────────────┘    │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │  SQLite (one .db file per tenant under webhooks/)            │    │
│  └──────────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────────┘
              │ HTTPS (REST)
              ▼
┌──────────────────────────────────────────────────────────────────────┐
│                          Telnyx API                                   │
│  · Phone numbers · Call control apps · AI assistants · Recordings    │
│  · Messaging (MMS) · Webhook delivery                                 │
└──────────────────────────────────────────────────────────────────────┘
```

**Two processes total:** static nginx container (frontend) + FastAPI (backend).
**One database:** SQLite per tenant. **One external dependency:** Telnyx.

---

## Key decisions

### D1 — single-file HTML + Tailwind via CDN (no build step)
**Why:** the softphone started as a 200-line prototype; turning it into a Vite
build would have added 10× complexity (npm, deps, hot reload, version drift) for
zero user benefit. The CDN approach means: edit HTML, refresh browser, done.
**Cost:** bundle size is ~250KB uncompressed (we ship fewer icons than a typical
SaaS). Acceptable for the softphone use case.
**When this changes:** if we need code-splitting, server-side rendering, or
shared component libraries, the build pipeline pays for itself.

### D2 — Python FastAPI for the backend
**Why:** async-first, type hints, automatic OpenAPI docs, easy to deploy as
a single process. The team has Python skills. Telnyx's Python SDK is the most
mature of their SDKs.
**Alternatives considered:** Node + Express (would match the frontend JS), Go
(better concurrency but smaller ecosystem for Telnyx).
**When this changes:** if we hit CPU-bound workloads (e.g. call recording
transcription), Python's GIL becomes a problem — split those workers into Go.

### D3 — SQLite per tenant, no separate DB server
**Why:** zero ops for a single-server deployment. SQLite handles thousands of
writes/sec; our load is dozens. The per-tenant file also makes tenant data
deletion trivial (`rm tenant_xyz.db`).
**Migration path:** when we need real concurrency, the schema is SQLAlchemy so
swap in Postgres + connection pool without code changes. We'd add a
`pg_advisory_lock` around the scheduler instead of the file lock.

### D4 — Cloudflare tunnel (not direct Coolify expose)
**Why:** the webhook receiver and the API have a public URL on a stable domain
(`bk-jr-api.aixlabs.fun`) even though they're behind a NAT'd dev machine.
Cloudflare handles TLS, bot protection, and DDoS.
**Cost:** Cloudflare's free tier is enough; we don't pay anything.
**Alternative:** ngrok, fly.io. Tunnel was the path of least ops for dev.

### D5 — In-process WS broker (no Redis yet)
**Why:** the only subscriber is the softphone, and there's only one server
process. Adding Redis is overkill until we go multi-worker.
**Migration path:** swap the `WsBroker` for a Redis-pubsub-backed one. The
contract on the client side (just JSON events) won't change.

### D6 — `Connection: close` on every response
**Why:** the softphone polls every 2-3s. With keep-alive, each poll opens a
new TCP connection and the OS holds it in CLOSE_WAIT for minutes (Windows
default linger). After ~30 min the FD budget is exhausted and the server
stops accepting connections. Forcing close shrinks the CLOSE_WAIT window
to ~10s, well under the polling interval.
**Cost:** no keep-alive means slightly more TCP setup per poll. Cheap.

### D7 — HMAC-SHA256 webhook signing
**Why:** the Telnyx webhook itself doesn't sign, so we trust any POST. Until
we add AI assistants that pull in third-party callbacks, the attack surface
is small. Signing is opt-in (empty `WEBHOOK_HMAC_SECRET` → accepts all) so
the existing Telnyx integration keeps working.
**Format:** Stripe-style `t=<unix>,v1=<hex>` over `f"{ts}.{raw_body}"`. 5-min
max skew. The secret lives in `.env` and rotates by redeploying.

### D8 — Clean outbound line separate from AI-attached lines
**Why:** every Telnyx AI assistant is attached to a call control app. If you
dial outbound from that line, the AI intercepts the call. So outbound
campaigns need a "clean" line with no AI. We auto-purchased
`+1 507 873 1084` (Minnesota) for this and created a new call control app
"Clean Line" with no assistant attached.

### D9 — Telnyx SDK v4 for control actions, httpx for everything else
**Why:** the Telnyx SDK is more idiomatic for things like `client.calls.create`
and `client.numbers.list`. But it doesn't expose call control actions like
`reject` / `hangup` / `answer` cleanly. For those, we call
`POST /v2/calls/{cci}/actions/{action}` directly via httpx with
`Authorization: Bearer <KEY>`.

### D10 — Six-state softphone FSM (not a 12-state matrix)
**Why:** the `ominicontacto` reference uses a 12-state matrix with `Initial,
Connecting, Ready, Dialing, OnCall, OnHold, ReceivingCall, OnIncoming,
OnAnswer, OnHangup, OnTransfer, OnError`. We use 6:
`Initial → Connecting → Ready → Dialing → OnCall ↔ OnHold`,
plus `ReceivingCall` as a parallel state.
**Why simpler:** most of those 12 states are momentary transitions, not real
states the UI needs to know about. The simplification cut ~150 lines of FSM
code without losing any user-visible feature.

---

## Data model

### `webhooks/agentops.db` (SQLite, per tenant)

```sql
contacts (
  id, tenant_id, name, phone, email, tags JSON, created_at
)

campaigns (
  id, tenant_id, name, type ('sms'|'voice'), from_number, message,
  contact_ids JSON, schedule_at, status ('draft'|'scheduled'|'running'|'paused'|'done'|'failed'),
  stats_json, created_at, updated_at, started_at, completed_at
)

scheduled_jobs (
  id, tenant_id, kind ('sms_send'|'campaign_launch'),
  payload JSON, run_at, status, last_error, created_at
)
```

Routing (`routing.json`, loaded on server boot):
```json
{ "+18138223579": "ai-assistant-1e760daa", ... }
```

---

## Auth & secrets

| Secret | Where | Rotation |
|---|---|---|
| Telnyx API key | `.env` | Manually via Telnyx portal |
| WebRTC credential | `.env` | Re-create via API |
| Webhook HMAC secret | `.env` | Generate with `python -m webhooks.security` |
| Coolify API token | `local-projects/.env.coolify` | Manually via Coolify UI |
| GitHub PAT (`mybijouai-creator`) | macOS keyring via `gh auth` | `gh auth refresh` |

Per-tenant secrets (future): KMS-encrypted in tenant DB.

---

## Failure modes we know about

| Symptom | Cause | Fix |
|---|---|---|
| Tunnel goes 502 | cloudflared process died | `Start-Process` it back (Task Scheduler entry TBD) |
| Softphone polls die | CLOSE_WAIT pile-up | `Connection: close` middleware |
| Outbound calls hit AI | line has AI attached | use the clean line `+1 507 873 1084` |
| Webhook signed with local ts | PowerShell `Get-Date -UFormat %s` is local | use `[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()` |
| `.env` parse error | PowerShell `Add-Content` wrote Windows-1252 | ASCII-only comments |
| Deploy doesn't fire | GitHub App on mnjbold account not installed | install on the org/user that owns the repo |

---

## What we explicitly chose NOT to do

- **GraphQL** — REST is fine for a handful of dashboard tabs.
- **Kubernetes** — Coolify + 1 server is enough until proven otherwise.
- **Microservices** — there's no benefit at this scale; one FastAPI process
  serves everything and the WS broker fits in memory.
- **Vite/React** — see D1.
- **Telnyx AgentSDK (StatefulActor)** — Beta, requires the `telnyx` CLI on
  every dev machine. The current FastAPI webhook handler does the job.
- **WebSocket authentication beyond `session_token`** — the softphone only
  connects from the same origin over WSS, and the API is the same auth story.

---

## Future architecture (when we cross 100 paying tenants)

- Multi-worker uvicorn behind a reverse proxy (Caddy / nginx)
- Redis for WS pubsub + distributed locks on the scheduler
- Postgres for shared analytics, SQLite stays per-tenant
- Separate read-replica API for the dashboard
- Multi-region: active-active with health-checked failover

Until then, keep the simple version.
