# Compliance & Legal — W3J Telephony Platform

This document is the **hard line** the user (and the W3J AI) must respect
when deploying voice agents. Violating these is the #1 way to get shut down
or sued.

---

## 1. Voice cloning consent (CRITICAL)

**Never clone a voice without written consent from the voice owner.**

### US state laws (as of 2026-07-31)
- **California** — Cal. Civ. Code § 3344 (right of publicity); AB 2602 (2024) bans AI replicas of deceased performers without consent.
- **Illinois** — BIPA + right of publicity.
- **New York** — NY Civ. Rights Law § 50/51 (right of publicity).
- **Tennessee** — ELVIS Act (2024) — explicitly bans unauthorized AI voice clones.
- **Texas** — Texas Property Code § 26.012.

All require written consent for commercial use of a person's voice.

### What "written consent" means
- A document the person signs (DocuSign, HelloSign) explicitly authorizing
  W3J to create an AI voice clone from the audio they provide.
- Must include: what the clone is for, how long it can be used, how it
  will be stored, who has access.
- Must be kept on file for the lifetime of the clone + 3 years.

### For the W3J personal twin
- The user IS the voice owner. Self-consent is valid.
- Keep a written statement: "I, Muhammad Nurunnabi, authorize W3J LLC to
  create an AI voice clone from my audio for use in [list]." Signed and
  dated.

---

## 2. TCPA (Telephone Consumer Protection Act) — OUTBOUND

For **outbound** calls (dial, transfer, etc.) you must:

1. **Prior express written consent** for any marketing/promotional call
   to a wireless number. Form must be clear, conspicuous, and disclose that
   the consumer is agreeing to receive autodialed/prerecorded calls.
2. **Identify yourself** at the start of the call (the AI assistant's
   greeting is fine if it names the company).
3. **Provide an opt-out** mechanism. Honor it within 10 business days.
4. **Time-of-day restrictions**: no calls before 8am or after 9pm in the
   called party's local time.
5. **Honor the Do-Not-Call Registry** (US) for telemarketing. Exempt if
   the consumer gave prior express written consent.
6. **Maintain records** of consent for 5 years.

### What the AI Assistant system prompt must include
- "If the caller says 'stop calling' or 'do not call' or 'take me off your
  list', confirm the opt-out, add them to the internal DNC list, and end
  the call politely."
- "If the caller asks who you are, name the company and that this is an
  AI assistant."
- "If it's before 8am or after 9pm in the caller's local time, do not
  start a sales pitch. Take a message."

### Personal twin outbound
- The user is calling on their own behalf (not a business) — different rules.
- The user is responsible, not us. We provide the tool; they use it.

---

## 3. Recording consent

### One-party vs two-party
- **Federal (US)**: one-party consent. You can record if at least one
  party consents. The AI assistant counts.
- **California, Connecticut, Delaware, Florida, Illinois, Maryland,
  Massachusetts, Montana, New Hampshire, Oregon, Pennsylvania, Vermont,
  Washington**: **two-party / all-party consent**. EVERY party must
  consent.

### What the AI Assistant system prompt must include
- "If the caller asks if the call is being recorded, say yes and explain
  that the recording is for quality and training purposes."
- "If the caller asks to be removed from recordings, end the call politely
  and note their preference."

### For the W3J personal twin
- You're the one party. The other party needs to be informed. The system
  prompt for the twin should say: "Hi, this is Nurun's AI assistant —
  calls may be recorded for quality."

---

## 4. GDPR (EU)

For any EU caller:

1. **Right to be informed** at the start of the call (we record / we use
   AI / we process your data for X).
2. **Right to access / rectify / delete** — the AI must be able to forward
   such requests to a human within 30 days.
3. **Data minimization** — don't collect more than needed. Don't record
   voice without consent.
4. **DPA** — for any data going to a non-EU processor (Telnyx, OpenAI,
   Anthropic, ElevenLabs, Cartesia, etc.), ensure a Data Processing
   Agreement is in place.

### What the AI Assistant system prompt must include (for EU numbers)
- "This call may be recorded. If you do not consent, please tell me and I
  will end the call."

---

## 5. AI Act (EU, in force 2025-2026)

The EU AI Act categorizes voice cloning as a **"limited risk"** AI system
but requires:
- Transparency: caller must be informed they're talking to an AI.
- The AI must be capable of identifying itself as AI on request.
- Deepfakes of real people are banned without explicit consent.

### What the AI Assistant system prompt must include
- "If asked 'are you a bot?' or 'are you AI?', confirm: 'Yes, I'm an AI
  assistant. I'm [voice] powered by Telnyx and [model]'."

---

## 6. PDPA (Malaysia)

The personal twin's call transfers will go to a Malaysian number. PDPA
applies if any caller data is stored in Malaysia.

- Bijou AI is already PDPA-compliant via the multi-tenant Supabase backend.
- The Telnyx webhook receiver (in this repo) stores call events in
  SQLite / Sheets / Supabase. If any of these are in Malaysia, we need a
  Privacy Notice and an opt-out path.

---

## 7. Number porting

If a client wants to port an existing number to Telnyx:
- Process: https://developers.telnyx.com/docs/numbers/porting
- Time: 7-10 business days for US, longer for international.
- The agent builder doesn't port numbers; the user (W3J) handles that
  via the Telnyx portal + LOA (Letter of Authorization) from the client.

---

## 8. What we provide (the platform's compliance)

- **Opt-out detection** in the system prompt (caller says "stop", the
  agent acknowledges and hangs up).
- **Time-of-day respect** in business_hours config.
- **DNC list** in the SQLite sink (manual list; auto-populated from
  opt-outs during calls).
- **Recording opt-out** announced in the AI's first reply.
- **AI identity disclosure** in the system prompt.
- **Audit log** of every call event in SQLite / Sheets / Supabase.
- **Webhook signing verification** — `WEBHOOK_SIGNING_SECRET` env var
  (TODO: implement HMAC verification on the webhook receiver).

---

## 9. What the CLIENT is responsible for

- Obtaining prior express written consent for outbound (TCPA).
- Maintaining their own DNC list (we help, but it's their data).
- Privacy Policy and Terms of Service on their own website.
- State-specific recording consent notices (CA, FL, IL, etc.).
- Contractual opt-out mechanism in their CRM.
- EU AI Act / GDPR compliance if they have EU customers.

---

## 10. What W3J LLC is responsible for

- Keeping this compliance document updated as laws change.
- Maintaining the consent record for the W3J voice clone.
- Verifying that any voice clone we create has a valid consent on file.
- Honoring the DNC list across all of our clients.
- Annual review of Telnyx / OpenAI / Anthropic / ElevenLabs DPAs.

---

## 11. Red flags — walk away from the deal

- Client asks you to clone a celebrity's voice.
- Client asks you to make calls "anonymously" or spoof caller ID.
- Client refuses to provide prior consent for outbound campaigns.
- Client wants to record without consent.
- Client wants to impersonate a government agency or a real person.
- Client is in a regulated industry (health, finance, legal) and won't
  provide their compliance documentation.

These are all legal landmines. The right answer is always "no, here's
what we can do legally."

---

## 12. Useful links

- TCPA: https://www.fcc.gov/consumers/guides/stop-unwanted-robocalls-and-texts
- TCPA consent forms: https://www.ecfr.gov/current/title-47/chapter-I/subchapter-A/part-64
- ELVIS Act (TN): https://legiscan.com/TN/text/HB2091/id/2762613
- AB 2602 (CA, deceased performers): https://leginfo.legislature.ca.gov/faces/billNavClient.xhtml?bill_id=202320240AB2602
- EU AI Act: https://artificialintelligenceact.eu/
- PDPA (Malaysia): https://www.pdpc.gov.my/
- Telnyx privacy: https://telnyx.com/privacy-policy
