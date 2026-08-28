# AGENTS.md — guidelines for AI coding agents

If you're an AI agent picking up work in this repo, read this first.

---

## The shape of the project

- **Frontend** (`frontend/`) is a single HTML file (`index.html`) + a few
  static assets. No build step. Tailwind comes from the CDN. JS is plain ES6.
  Tab modules (e.g. `campaigns.js`) are defer-loaded.
- **Backend** (`backend/`) is a FastAPI app. Entry point is `python -m webhooks.server`
  (which imports `webhooks/dashboard_api.py` for the REST API). SQLite for storage.
- **Webhook flow:** Telnyx → Cloudflare tunnel → `bk-jr-api.aixlabs.fun`
  → `/webhooks/telnyx` on our server → handler in `webhooks/handlers/`.

## How to make changes

### Frontend
1. Edit `frontend/index.html` directly.
2. If adding a new tab, create `frontend/<tabname>.js` with an IIFE module,
   include it via `<script src="<tabname>.js" defer></script>` in `index.html`.
3. Bump the version stamp in the visible UI (search for `v0.4.x` in `index.html`).
4. Hard-refresh the browser to bypass service worker cache.
5. Commit + push. The GitHub webhook triggers a Coolify auto-deploy within ~2 min.

### Backend
1. Edit the relevant `webhooks/*.py` file.
2. If adding a new endpoint, register it via the `@router.get/.post/...` decorator.
3. If touching a new Telnyx API surface, prefer `httpx` (already wired in
   `dashboard_api.py`) over the SDK unless the verb is gated by the SDK.
4. Restart the server: `Stop-Process -Id <pid> -Force` then
   `Start-Process ... -m webhooks.server --port 8080 ...`.
5. Smoke test with a curl from local + a tunnel-side test (browser-based) before
   declaring done.

## What NOT to do

- **Don't introduce a build step to the frontend** (Vite, Webpack, esbuild, etc.).
  The single-file HTML + Tailwind CDN is the architecture; don't fight it.
- **Don't add a database server.** SQLite per tenant is the design. (Appwrite
  is opt-in; see `backend/appwrite/`. Don't add a Postgres server unless we
  need cross-tenant analytics that SQLite can't deliver.)
- **Don't bypass the webhook handler.** Every Telnyx event goes through
  `webhooks/handlers/base.py:WebhookContext` and a typed handler in `handlers/`.
  Don't add a parallel path.
- **Don't use the `.env` file as a config store.** It's for secrets only.
  Anything non-secret belongs in the code or a per-tenant DB row.
- **Don't put a real `.env` in the repo.** Use `.env.example` and readme it.
- **Don't put a real `settings.json` in the repo.** It's git-ignored.

## Key code paths

| What | Where |
|---|---|
| Webhook receiver (Telnyx) | `backend/webhooks/server.py:receive_telnyx_event` |
| Dashboard API endpoints | `backend/webhooks/dashboard_api.py` (30+ `@router.*` routes) |
| Webhook HMAC signing | `backend/webhooks/security.py` |
| Tenant storage (SQLite) | `backend/webhooks/storage.py` |
| Scheduler thread (in-process) | `backend/webhooks/dashboard_api.py` (starts at import time) |
| WebSocket broker | `backend/webhooks/dashboard_api.py:ws_broker` |
| Telnyx SDK client | `backend/telnyx_mcp/clients/telnyx_client.py` |
| Frontend dialer FSM | `frontend/index.html` (6-state FSM, search for `fsm`) |
| Frontend campaigns tab | `frontend/campaigns.js` |
| Frontend PWA shell | `frontend/manifest.json` + `frontend/sw.js` |

## Test paths

- Manual smoke: `python -m webhooks.security.check` prints whether
  `WEBHOOK_HMAC_SECRET` is set.
- Manual webhook signing: `python backend/scripts/sign_webhook.py payload.json`.
- Tunnel-side smoke: open the live frontend in a browser, watch console +
  Network tab for the `connection: close` response header.
- Deployment smoke: push a commit to `main`, watch
  https://coolify.getbijou.xyz for a new deployment, verify the live URL
  reflects the change within 2-3 minutes.

## Style

- **Python:** type hints everywhere, Pydantic for input models, FastAPI
  dependency-injection for shared resources. Match the existing module
  style in `webhooks/dashboard_api.py`.
- **JS:** ES6, async/await, plain DOM (no jQuery, no React). Match the
  style in `frontend/index.html`.
- **Commits:** imperative mood subject line ("add foo", not "added foo").
  Body explains WHY, not WHAT.
- **PRs:** small, focused, test before push. If a change requires a secret
  rotation or env var change, list that in the PR description.

## When you're stuck

- The live state of Telnyx resources is in the **Telnyx portal** — phone
  numbers, call control apps, AI assistants. Don't guess; query via API.
- The live state of Coolify is in the **Coolify dashboard**
  (https://coolify.getbijou.xyz).
- The webhook server logs are at `%TEMP%\webhook.out.log` and
  `%TEMP%\webhook.err.log` on the dev machine.
- The cloudflared tunnel config is at
  `C:\Users\W3jde\.cloudflared\bk-jr-config.yml`.
- The Coolify API token is at `C:\Users\W3jde\local-projects\.env.coolify`.

## Hard rules (don't violate these)

1. **No real secrets in the repo.** Use `.env.example` as the template; the
   real `.env` is local-only and git-ignored.
2. **No new dependencies without a `requirements.txt` update** (backend)
   or a comment in `index.html` (frontend, where deps come from CDN).
3. **No test that requires a live Telnyx account to pass** in CI. The
   webhook server has unit-testable logic in `webhooks/security.py` and
   `webhooks/storage.py`; cover those. Anything that hits the Telnyx API
   is integration-only and manual.
4. **No silent failures.** If a webhook handler can't process an event,
   log it at WARNING and return 200 to Telnyx (so they don't retry forever).
   If a critical path errors, log at ERROR and bubble up.
5. **No tab-switch loops in the browser.** If you're verifying a UI change
   in the in-app browser and the page re-renders too fast for stable
   selectors, use the backend API instead. Don't burn 5+ tool turns
   fighting the embedded browser.
