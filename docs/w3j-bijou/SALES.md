# How to sell W3J Telephony Agents

This document is the playbook W3J uses to sell the platform built in
`C:\Users\W3jde\local-projects\w3j-projects\telnyx`.

Target customer: any business that takes 10+ inbound calls/week and currently
pays a receptionist, an answering service, or a BPO.

---

## What we sell

A **production AI voice agent** in 1-2 weeks, priced like a senior employee,
deployed on the client's own Telnyx number (or ported to one).

### The product tiers

| Tier | Price (one-time) | Price (monthly) | What you get |
|------|------------------|-----------------|--------------|
| **Starter** — single agent, 1 number, basic call flow | $1,500 | $200 | 1 AI Assistant, 1 CA number, 500 min/mo, basic call routing, leads to Google Sheet |
| **Pro** — multi-agent, multi-number, custom voice | $5,000 | $500 | Up to 3 agents, up to 3 numbers (any US area), custom voice clone, 2,000 min/mo, CRM/Sheets/Supabase integration, call recordings + transcripts |
| **Enterprise** — full platform access, multi-tenant, portal | $15,000+ | $1,500+ | Unlimited agents & numbers, all integrations, the connector portal, white-label option, dedicated support |

> **The wedge**: the customer is paying $2,000-3,000/month for a part-time
> receptionist who sleeps at 11pm. We charge $200-500/month for an agent
> that never sleeps, never takes MC, and (with Manglish / voice clone) sounds
> like a local. Same economics as Bijou AI's WhatsApp agent at RM 299/mo.

---

## Demo script (the 5-minute pitch)

> "Hey, you're paying [receptionist / answering service / nobody] to handle
> your inbound calls. I'm guessing they miss the after-hours ones, they're
> slow on weekends, and you don't have transcripts of what they said.
>
> I can give you an AI receptionist in your own voice, on your own number,
> that takes every call 24/7. It books appointments, qualifies leads,
> transfers urgent ones to your cell, and writes everything to a Google
> Sheet or your CRM.
>
> Cost: $1,500 setup, $200/month. That's about a week of your receptionist's
> salary for a year of coverage.
>
> Want to try it on a real call next week? I'll spin up a number in your
> area code, clone your voice (or use a default), and you can call it
> yourself."

---

## The 3-minute live demo (when they say yes)

1. Show them the dashboard: 11 existing AI assistants, 3 numbers, 2 call
   control apps, all running.
2. Pick their industry, spin up a new agent with `python scripts/deploy_all_agents.py --only <name>`
3. Call the number from your cell.
4. While the call is live, show them the agent's transcript appearing in
   the Google Sheet (or `w3j_telephony.db` via `sqlite3`).
5. Ask the agent "transfer me to a human" → it bridges to your cell.

---

## The first 5 customers (in priority order)

| # | Vertical | Why | Wedge |
|---|----------|-----|-------|
| 1 | Bold Connect internal (Amanda replacement) | already a paying client | save $500/mo on existing receptionist |
| 2 | Bijou AI inbound sales (Bijou tenants + prospects) | already a paying product | upsell RM 299/mo → RM 499/mo with voice |
| 3 | Bold Business internal (HR, sales) | sister company | showcase for VC pitches |
| 4 | KL/MY restaurants / salons / clinics | Manglish persona is a moat | one-call-close |
| 5 | Bay Area / LA tech founders (personal twin use case) | high-LTV, understand tech | $5k setup, $500/mo |

---

## The price-breakdown you tell them

> $1,500 setup covers:
> - 2-hour discovery call (your call flow, your voice, your CRM)
> - Voice clone (ElevenLabs Professional, $1-5/mo ongoing)
> - Telnyx number in your area code ($1-3/mo)
> - AI Assistant build + tools (booking, lead capture, transfer)
> - 1 round of revisions
> - Day-of launch
>
> $200/mo covers:
> - Up to 500 minutes (US/CA) of inbound + outbound calls
> - 1 number
> - Per-call logging to your CRM / Sheet
> - Up to 4 voice-clone updates per year
> - Email support, 24-hour response time

---

## What to NEVER do

- Don't promise a voice clone in less than 1 week. ElevenLabs needs clean
  audio and 1-2 days of generation.
- Don't promise integration with a CRM we haven't vetted. The connectors
  are plug-and-play, but Salesforce / HubSpot / Pipedrive / Zoho all have
  quirks. Always run a sandbox first.
- Don't sell voice cloning to a client before you have written consent from
  the voice owner. EU AI Act and many US states (CA, IL, TX, etc.) require
  explicit consent for AI voice generation.
- Don't promise TCPA compliance. That's on the client. We provide
  opt-out detection and time-of-day respect; they must obtain prior
  express written consent for outbound.
- Don't demo a number that costs us real money unless the customer is
  on a paid contract. Track demo costs in `w3j_telephony.db`.

---

## Sales ops

- **Lead source**: LinkedIn, Facebook groups (already your #1 channel per
  the Bijou playbook), referrals.
- **CRM**: nothing for now. Google Sheet at first. Then HubSpot.
- **Contract**: Stripe checkout link, generated from the portal (next session).
- **Invoicing**: manual for first 5 customers, then automate.
- **Onboarding**: 1-hour Zoom, 3-5 day build, 30-min handoff.
- **Support**: email + Telegram bot (uses our existing bot).

---

## The single-line pitch (for DMs, cold emails, ads)

> "AI receptionist on your own number, in your own voice, 24/7 — $200/mo."

---

## What you'd be selling in 6 months (with the portal built)

A **self-serve portal** where the client:
1. Signs up (Google SSO)
2. Picks a Telnyx number (or ports one)
3. Uploads 5 min of audio (for voice clone) OR picks a built-in voice
4. Describes their business in plain English (the system prompt is generated)
5. Configures integrations: Sheets, HubSpot, Salesforce, WhatsApp, Telegram
6. Goes live, watches the dashboard

That's the Composio-style portal the user asked about. We have the
backend (`connectors/`, `agent_builder/`, MCP). Frontend is next session.

---

## What to charge for the portal (per-client)

| Tier | Setup | Monthly |
|------|-------|---------|
| **Self-serve** | $0 | $99/mo (500 min, 1 number) |
| **Managed** | $500 | $499/mo (5,000 min, 5 numbers, voice clone, integrations) |
| **White-label** | $5,000 | $999/mo (rebrand, custom domain, unlimited) |

At 20 self-serve customers: $20k MRR. At 5 white-label: $25k MRR. The
5,000 SMBs in your network is the upper bound.
