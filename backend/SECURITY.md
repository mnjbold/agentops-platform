# SECURITY.md — Bijou Voice (Telnyx concierge) — W3J-BIJOU PROJECT

> This project has previously held live Telnyx credentials, a MiniMax API key, and live SIP
> username/password in plaintext on local disk. **No actual secret values are reproduced in
> this file, in any commit, or in any issue.** This file documents the *policy* and the
> *rotation procedure* — it is safe to commit.

## Current exposure status (2026-08-23)

| Asset | Location | Status |
|---|---|---|
| Telnyx JWT, org API key, public key | `.env` (project root) | On local disk only. Never committed (this project is not a git repo). |
| Live SIP username/password | `scripts/sip_credentials.json` | On local disk only. Never committed. |
| MiniMax API key | `.env` | On local disk only. Never committed. |
| Org key (also in human-readable form) | `docs/KNOWLEDGE_BASE.md` | On local disk. **Recommend scrubbing from any future commits** — this file should hold the procedure, not the value. |

**Git history exposure:** None — this project is not yet under version control. If you
`git init` it, the new `.gitignore` at the root will protect the sensitive files from day one.

**Disk exposure:** Anyone with read access to the machine can read `.env` and
`sip_credentials.json`. This is the real residual risk.

## Recommended rotation (do this once, then re-evaluate)

The risk model is: a plaintext secret sitting on local disk is *de facto* exposed to any
process or human with read access. Rotation eliminates that residual risk.

1. **Telnyx org API key**
   - Telnyx Portal → Settings → API Keys → revoke the current key → issue a new one
   - Update the value in your local `.env`
   - Re-deploy any service that uses the key (e.g., the concierge agent)
   - Old key is dead; no client should reference it after this point

2. **Telnyx JWT**
   - Same flow — revoke in portal, generate new, update local `.env`
   - Any webhook or API client using the JWT will need a fresh re-auth

3. **MiniMax API key**
   - Rotate at the MiniMax platform
   - Update local `.env`

4. **SIP username/password**
   - Telnyx Portal → Voice → SIP Connections → rotate the credential
   - Update `scripts/sip_credentials.json` only after rotation succeeds

5. **Verify**
   - Restart the concierge and any service that consumed the old values
   - Confirm outbound voice, inbound voice, SMS still works end-to-end

## What goes in `.env.example` (the only safe thing to commit)

`KEY_NAME=value-here` — placeholders only. Real values go in `.env`, which is `.gitignore`'d.

```env
# Telnyx
TELNYX_API_KEY=
TELNYX_PUBLIC_KEY=
TELNYX_JWT=

# MiniMax
MiniMax_API_KEY=

# Supabase (per-tenant when wired into Bijou)
SUPABASE_URL=
SUPABASE_SERVICE_KEY=
```

## What you do NOT do

- Paste a real key into a chat, an issue, a commit, a doc, a screenshot
- Commit `.env`, `*.env.local`, `*credentials*.json`, `*.key`, `*.pem`
- Store the same secret in two places — single source of truth, rotated in place
- Use the same key in two projects — separate keys per environment (dev/staging/prod)

## Reporting a leak

If you believe a real value has been committed, shared, or screenshotted:

1. Rotate the affected credential immediately (steps above)
2. Search git history (`git log -p --all -S 'KEY_NAME='` if it ever was a repo)
3. Notify the project owner
4. Update this SECURITY.md with the incident + remediation

## Acknowledgement

Project owner: W3J (@W3JDev on GitHub, mybijouai-creator org for the canonical mirror).
Last reviewed: 2026-08-23.
