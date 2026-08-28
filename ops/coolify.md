# Coolify deployment

This repo deploys via Coolify (self-hosted PaaS) using two apps:

| App | UUID | Public URL | What it serves |
|---|---|---|---|
| `agentops-dashboard` | `tjuixxkvpwndvjacdl7yufzh` | https://agentops.getbijou.xyz | Frontend (nginx + PWA) |
| `agentops-backend` (TBD) | TBD | https://bk-jr-api.aixlabs.fun | Backend (FastAPI webhook server) |

Both apps are in the `github-imports` project.

## Frontend deploy (DONE)

- **Build:** `dockerfile`, build dir = `/frontend`, base dir = `/frontend`
- **Source:** `mnjbold/agentops.git` (push mirror at `mybijouai-creator/agentops.git`)
- **Trigger:** GitHub webhook → `/webhooks/source/github/events/manual` (HMAC-signed)
- **Auto-deploy:** `is_auto_deploy_enabled = true` in the app config

To redeploy manually:
```powershell
$token = (Get-Content "$HOME\local-projects\.env.coolify" | Select-String "COOLIFY_API_TOKEN" | ForEach-Object { ($_ -split "=", 2)[1].Trim() }).Trim('"').Trim("'")
$headers = @{ "Authorization" = "Bearer $token"; "Accept" = "application/json" }
Invoke-RestMethod -Method Post -Uri "https://coolify.getbijou.xyz/api/v1/deploy?uuid=tjuixxkvpwndvjacdl7yufzh&force=true" -Headers $headers
```

## Backend deploy (TBD)

The backend currently runs as a local `python -m webhooks.server --port 8080`
process behind a Cloudflare tunnel. To move it into Coolify:

1. Add a Dockerfile to `backend/` that does:
   ```dockerfile
   FROM python:3.12-slim
   WORKDIR /app
   COPY requirements.txt .
   RUN pip install --no-cache-dir -r requirements.txt
   COPY . .
   EXPOSE 8080
   CMD ["python", "-m", "webhooks.server", "--host", "0.0.0.0", "--port", "8080"]
   ```
2. In Coolify, create a new app with build dir = `/backend`, source =
   `mnjbold/agentops.git` (same repo).
3. Add the same `WEBHOOK_HMAC_SECRET`, `TELNYX_API_KEY`, etc. as env vars in
   the app's environment settings.
4. Set the port to 8080. Set the healthcheck path to `/health`.
5. Add the same GitHub webhook so auto-deploy on push.

The tunnel (`bk-jr-api.aixlabs.fun` → `127.0.0.1:8080`) keeps working as long
as Cloudflare's DNS points at the same `cloudflared` process, regardless of
whether 8080 is local or in Coolify.

## GitHub webhooks

| Repo | Hook ID | URL | Secret |
|---|---|---|---|
| `mnjbold/agentops` | 671316823 | `https://coolify.getbijou.xyz/webhooks/source/github/events/manual` | `wDg5xBW8RsID5detWzV0hWZ5sbLrST2L3eifJZfI` |
| `mybijouai-creator/agentops` | 671316859 | same | same |

Both fire on `push` events. mnjbold is the source of truth; mybijouai is a
mirror.

## Why this isn't a one-click setup

The first app (frontend) was created by manually pointing Coolify at the public
repo URL with the `Public GitHub` source (id=0). That gave us auto-deploy via
the manual webhook once the secret was saved.

For the second app (backend), the same approach will work, but Coolify's
"Import" button on the GitHub-imports project requires a GitHub App to be
installed. The existing GitHub Apps in this Coolify instance are:
- id=0 — Public GitHub
- id=1 — coolify-ai-agent-project
- id=2 — w3jdev-github-coolify
- id=4 — mnjbold-coolify-app (org: "bold business")

If we want a "one-click import" UX, install one of these on the
`mybijouai-creator` account and grant it access to the repo. For now,
manual app creation with `git_repository: mybijouai-creator/agentops.git`
works fine.
