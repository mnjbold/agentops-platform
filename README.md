# agentops — Cloud Telephony Platform

A self-hosted, multi-tenant telephony platform built on [Telnyx](https://telnyx.com). Replaces
RingCentral / OpenPhone / Aircall for teams that want full control of their voice + SMS stack,
with native AI agent routing, campaign/blast-dial, and a PWA softphone that works on any
modern browser.

**Live demo:** [agentops.getbijou.xyz](https://agentops.getbijou.xyz)
**Live API:** [bk-jr-api.aixlabs.fun](https://bk-jr-api.aixlabs.fun/api/state)

---

## The product

- **WebRTC softphone** — full dialer + active-call controls in the browser, no install needed.
  PWA-installable on iOS / Android / desktop.
- **SMS / MMS inbox** — threaded conversations, scheduled sends, mass broadcasts.
- **AI agent routing** — connect inbound numbers to any of your Telnyx AI assistants.
  Manual override / dispatch flow included.
- **Campaign board** — create outbound voice or SMS missions, pick audience, schedule,
  track live progress.
- **Recording playback + call history** — searchable archive, per-number filtering.
- **Multi-tenant SaaS** — `X-Tenant-Id` header partitions data per workspace (default
  tenant for solo / dev use).

Everything runs as a single FastAPI process + a single static-file nginx container, with
no database server to manage (SQLite per tenant). Deploys via Coolify with auto-deploy
on push to GitHub.

---

## Repo layout

```
agentops-platform/
├── README.md              # this file
├── ROADMAP.md             # 6-month plan with milestones
├── ARCHITECTURE.md        # system design + key decisions
├── AGENTS.md              # guidelines for AI coding agents
├── LICENSE                 # MIT
│
├── frontend/              # PWA softphone (static files, no build step)
│   ├── index.html         # entire app — single file, Tailwind via CDN
│   ├── campaigns.js       # campaigns tab (defer-loaded)
│   ├── manifest.json      # PWA manifest
│   ├── sw.js              # service worker
│   ├── nginx.conf         # SPA + PWA headers
│   ├── Dockerfile         # nginx:alpine image
│   └── icons/             # PWA icons
│
├── backend/               # FastAPI webhook server + REST API
│   ├── __main__.py        # entry point
│   ├── webhooks/          # webhook receiver, dashboard API, scheduler
│   ├── telnyx_mcp/        # Telnyx SDK wrappers (assistants, voice, messaging, etc.)
│   ├── scripts/           # one-off deploy / smoke / sign helpers
│   ├── requirements.txt   # pip freeze
│   ├── routing.json       # phone → assistant routing map
│   ├── SECURITY.md        # webhook signing, secrets, threat model
│   └── W3J-BIJOU-README.md # legacy backend notes (kept for context)
│
├── docs/                  # this repo's documentation
│   ├── w3j-bijou/         # legacy backend docs (API, ARCHITECTURE, COMPLIANCE, …)
│   └── (TBD) deployment.md, operations.md
│
├── ops/                   # deployment + ops (TBD)
│
└── .github/workflows/     # CI (TBD)
```

---

## Quick start

### 1. Run the backend (FastAPI + webhook server)

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
# Edit .env with your Telnyx keys + webhook secret
python -m webhooks.server --port 8080
```

Server is now serving:
- `POST /webhooks/telnyx` — webhook receiver (HMAC-signed in production)
- `GET  /api/state` — current server status
- `GET  /api/numbers` / `/api/calls/recent` / `/api/messages/recent` etc.
- `GET  /api/contacts` / `/api/campaigns` / `/api/sms/scheduled` / `/api/sms/broadcast`
- `WS   /ws` — real-time event push to the browser

### 2. Run the frontend (static PWA)

```powershell
cd frontend
docker build -t agentops-frontend .
docker run --rm -p 8081:80 agentops-frontend
```

Open <http://localhost:8081>. The softphone auto-connects to the backend on
`http://127.0.0.1:8080` by default. Override with the `window.__API_BASE__` config
or a reverse proxy in production.

### 3. Deploy to Coolify

Both `frontend/` and `backend/` have their own Dockerfiles. Coolify builds them as
separate services:

- **Frontend** (this repo, build dir `frontend/`) → https://agentops.getbijou.xyz
- **Backend** (manual setup — see `ops/coolify-backend.md` once written) →
  https://bk-jr-api.aixlabs.fun

Auto-deploy is wired via a GitHub webhook pointing to
`https://<coolify-host>/webhooks/source/github/events/manual` (HMAC-signed with the
`manual_webhook_secret_github` saved in the app config).

---

## Status

| Component | State |
|---|---|
| Backend webhook receiver | ✅ v1, production |
| Backend dashboard API | ✅ v1, 30+ endpoints |
| Webhook HMAC signing | ✅ opt-in, secret in `.env` |
| Frontend PWA softphone | ✅ v0.4.4, production |
| Multi-tenant contacts / campaigns | ✅ shipped, frontend-tab wired |
| Scheduled SMS + mass broadcast | ✅ shipped |
| Power dialer (mass outbound) | ✅ backend done, UI next |
| Real-time WS push to browser | ✅ in production |
| Inbound AI agent routing | ✅ 36 assistants, 14 call-control apps |
| Outbound without AI interception | ✅ clean line (+1 507 873 1084) |
| Appwrite BaaS integration | 🟡 client + sample function in place, endpoint not yet live |
| Telnyx AgentSDK / StatefulActor | ⏸️ deferred — Beta, requires telnyx CLI |

---

## Tech stack

**Backend**
- Python 3.12
- FastAPI 0.141 + uvicorn (ASGI, HTTP/1.1)
- httpx (async Telnyx API calls)
- Telnyx Python SDK v4 (control actions the REST API doesn't expose)
- SQLite per tenant (no server to run)
- HMAC-SHA256 webhook signing (Stripe-style wire format)

**Frontend**
- Single-file HTML + Tailwind via CDN (no build step)
- @telnyx/webrtc UMD bundle for browser dialer
- PWA: manifest.json + service worker + install prompt
- Plain JS (no framework), defer-loaded tab modules (`campaigns.js`)

**Infra**
- Coolify (self-hosted PaaS) — app UUIDs per service
- Cloudflare tunnel (`bk-jr-api.aixlabs.fun`) — webhook receiver + dashboard API
- GitHub → Coolify webhook for CI/CD on push

---

## License

MIT. See `LICENSE`.
