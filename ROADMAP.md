# Roadmap

This is the working plan for getting `agentops` from "personal-tooling-grade" to
"ship-to-other-people-grade". It's organized as quarters with concrete milestones.
Each milestone is sized to fit in a single focused push (1-3 working days).

We optimize for **shipping things people can use** over **architectural purity**.
The repo started as a 1-app React-less PWA bolted onto a webhook server; the goal
is to harden it into a proper multi-tenant SaaS while keeping the parts that work.

---

## Q1 (now — Nov 2026): "stop breaking at scale"

**Theme:** kill the recurring outages, make multi-tenant real, hire the long-term infra.

### M1 — Fix the CLOSE_WAIT pile-up (DONE 2026-08-28)
- Added `Connection: close` middleware on every API response. ✅
- Single-worker uvicorn was the only thing keeping the demo alive.
- **Acceptance:** zero `CLOSE_WAIT > 30s` in 24h of production polling.

### M2 — Webhook signing (DONE 2026-08-28)
- HMAC-SHA256, opt-in via `WEBHOOK_HMAC_SECRET`, Stripe-style `t=...,v1=...`. ✅
- `scripts/sign_webhook.py` for signing test events.
- **Acceptance:** unsigned POST → 401, signed POST → 200, secret rotatable.

### M3 — Real multi-tenant backend (IN PROGRESS)
- Store schema includes `tenant_id`; tenant resolution from `X-Tenant-Id` header. ✅
- **Remaining:** tenant-scoped rate limits, tenant creation flow, tenant key issuance.
- **Acceptance:** two tenants in the same DB see zero leakage; per-tenant rate limits
  enforce; creating a tenant is one HTTP call from the admin UI.

### M4 — Persist softphone secrets properly
- Move WebRTC creds from `.env` to a per-tenant secret store (encrypted at rest).
- **Acceptance:** no plain-text secrets in `.env`; rotating a tenant's creds doesn't
  require a server restart.

### M5 — Replace single-worker uvicorn with multi-worker
- `uvicorn --workers 2` with a Redis-backed WS broker so events from worker A reach
  WS clients on worker B.
- **Acceptance:** 2 sustained clients on 2 workers, every call event reaches the
  browser within 1s; no duplicate scheduler jobs (file-lock on the SQLite queue).

---

## Q2 (Dec 2026 — Feb 2027): "real product surface"

**Theme:** add the features paying teams would expect; lock down auth.

### M6 — Tenant auth
- API key issuance, scoped per tenant. Replace the current `X-Tenant-Id: default`
  trust with signed tokens.
- **Acceptance:** unauthenticated request → 401; valid key + wrong tenant → 403.

### M7 — Recordings UX
- In-page audio player with waveform. Download as MP3.
- Search by phone number, agent, date range, duration, transcription (if any).
- **Acceptance:** from "Recordings" tab, any past call plays in <500ms without leaving
  the page.

### M8 — SMS composer v2
- Rich-text templates with variables (`{{first_name}}`, `{{agent_name}}`).
- Per-recipient throttling (Telnyx rate limits).
- Reply capture: route inbound replies to the right campaign / contact.
- **Acceptance:** send a 1000-recipient broadcast at 10 msg/sec without rate-limit
  errors; replies land in the campaign's inbox.

### M9 — Power dialer UI
- Frontend tab for the existing `/api/calls/power-dialer/start` endpoint.
- Live progress: which contact is being called, who picked up, who needs a retry.
- **Acceptance:** one-click launch a 50-contact outbound campaign; UI updates as each
  call resolves; one-click retry for unanswered.

---

## Q3 (Mar 2027 — May 2027): "agents that actually work"

**Theme:** make the AI assistant integration deep enough to be the default.

### M10 — Agent handoff analytics
- Dashboard tab: per-assistant call volume, average handle time, transfer rate,
  fallback rate. Drill into individual calls with the full transcript.
- **Acceptance:** for any assistant, you can see "this week vs last week" without
  exporting CSVs.

### M11 — Outbound campaigns with AI agent
- Currently outbound calls use a clean line (no AI). Add the option to attach an
  AI assistant to outbound legs, with handoff to a human on demand.
- **Acceptance:** create a campaign, pick "AI dials, human on connect" mode, see
  live human-handoff events in the UI.

### M12 — Custom assistant builder
- Web UI for creating a new Telnyx AI assistant (greeting, system prompt, tools,
  transfer numbers) without leaving the platform.
- **Acceptance:** an admin user can create + activate a new assistant end-to-end in
  under 5 minutes.

### M13 — Voicemail drop
- Pre-record a message, drop it on unanswered calls instead of ringing out.
- **Acceptance:** scheduled campaign "leave voicemail if no answer" mode replays
  the same audio file to all unanswered, lands the result in the call log.

---

## Q4 (Jun 2027 — Aug 2027): "first paying tenant"

**Theme:** harden for external users; first paid customer; multi-region readiness.

### M14 — Billing
- Stripe subscription per tenant. Free tier = 1 number, $29/mo = 5 numbers, custom
  for more. Meter overage (per-minute voice, per-segment SMS).
- **Acceptance:** a tenant can sign up, add a card, get an API key, hit the rate
  limit, see the overage charge on their next invoice.

### M15 — Audit log + compliance export
- Every API call, every webhook, every auth event → append-only audit log.
- Export as CSV / JSON for compliance reviews.
- **Acceptance:** for any tenant, "show me everything user X did in March" returns
  the full trail; the export is byte-stable across runs.

### M16 — Multi-region
- US-east and EU-west deployments, with cross-region failover for the dashboard
  and region-pinned data per tenant.
- **Acceptance:** a tenant in EU can specify EU-only data residency; failover test
  restores service within 5 minutes.

### M17 — Public API docs
- OpenAPI spec generated from FastAPI, hosted at `/docs` for tenants, branded and
  versioned.
- **Acceptance:** a third-party developer can sign up, get an API key, and
  programmatically send an SMS / place a call using only the docs — no help needed.

---

## Deferred (no quarter yet, but on the radar)

- **Telnyx AgentSDK** — StatefulActor pattern is Beta, requires `telnyx` CLI on
  every dev machine. Revisit when the SDK stabilises or if a specific use case
  (long-running agent state) demands it.
- **Voice AI features** — real-time transcription, sentiment scoring, live agent
  coaching. Requires picking a provider (Deepgram / AssemblyAI / etc.).
- **Mobile native apps** — PWA is good enough for now; revisit if PWA install rate
  stays <20% after 6 months of public launch.
- **Multi-language** — UI is English-only; internationalisation is a Q1 2028 problem.

---

## How we make decisions

A change goes on the roadmap if it answers YES to one of:

1. Does it unblock paying users from using the product? (revenue blocker)
2. Does it remove a class of incidents we've had twice? (stability)
3. Does it let one person do work that currently takes three? (operational leverage)

If none of those, it goes on the deferred list or gets dropped. The roadmap is
short on purpose — a 4-item roadmap is more honest than a 20-item wishlist.

---

## Tracking

- **GitHub Projects** — `<repo>/projects/1` (kanban: Backlog / This Quarter / In Progress / Done)
- **Weekly review** — every Monday, move cards, kill anything stuck > 2 weeks
- **Quarterly retro** — what we shipped, what we killed, what surprised us

The ROADMAP file is the source of truth. The board is a mirror.
