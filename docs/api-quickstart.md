# agentops API quickstart

5-minute time-to-first-call. The full reference is generated from the
FastAPI app — open [`/v1/docs`](/v1/docs) for the live Swagger UI or
[`/v1/openapi.json`](/v1/openapi.json) for the raw spec. A Postman
collection is at [`/v1/docs/postman.json`](/v1/docs/postman.json).

> All `/v1/*` endpoints are tenant-scoped. The caller's tenant is
> resolved from the JWT `tid` claim or the `X-Api-Key` header. The
> `/api/admin/*` paths are operator-only and do not appear in the
> public docs.

## 1. Sign up + get an API key

The first admin is created automatically on first boot. The bootstrap
log line looks like:

```
DEFAULT TENANT ADMIN (one-time setup):
  email:    admin@default.local
  password: <random>
```

Use the password to log in (or rotate immediately via
`/api/admin/tenants/default/rotate-key`).

## 2. Exchange email + password for a JWT

```bash
curl -X POST https://bkjr-api.getbijou.xyz/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@default.local","password":"<the password>"}'
```

The response carries `access_token` (24h JWT) and the `tenant_id` you
should use everywhere. The JWT's `tid` claim must match the `X-Api-Key`
tenant on every subsequent call.

## 3. First API call

```bash
curl https://bkjr-api.getbijou.xyz/v1/analytics/overview?preset=7d \
  -H 'Authorization: Bearer <jwt>' \
  -H 'X-Api-Key: <api key>'
```

If the JWT is enough on its own you can drop the `X-Api-Key`. The two
auth methods are interchangeable: `Authorization: Bearer` for browser
sessions, `X-Api-Key` for server-to-server scripts.

## Auth

There are two ways to authenticate:

| Method | Header | Best for |
| --- | --- | --- |
| **API key** | `X-Api-Key: w3j_…` | Server-to-server scripts. Bcrypt-hashed at rest. Shown once on tenant create + on rotate. |
| **JWT** | `Authorization: Bearer <jwt>` | Browser sessions. 24h TTL. Includes `tid` (tenant) and `sub` (user id) claims. |

The middleware in `webhooks/server.py` accepts either. Cross-tenant
access is rejected with `403`.

## Pagination

All list endpoints follow the same convention:

- `limit` (default 50, max 500) — page size
- `offset` (default 0) — rows to skip
- Response always carries `count` (rows in this page) and the request
  echo of `limit` + `offset` for client-side paging.

Example:

```bash
curl "https://bkjr-api.getbijou.xyz/v1/audit?limit=100&offset=0" \
  -H 'Authorization: Bearer <jwt>'
```

## Rate limits

- 100 requests / 60s per `(tenant_id, route)` bucket. 429s carry a
  `Retry-After` header in seconds.
- Bursting past the limit returns:

  ```json
  {"detail": "rate limit exceeded", "retry_after": 17}
  ```

  with `Retry-After: 17` set on the response.
- Override in dev with `W3J_RATE_LIMIT` and `W3J_RATE_WINDOW` env vars.

## Webhook signing

If `WEBHOOK_HMAC_SECRET` is set, every inbound `/webhooks/telnyx` and
`/admin/test_event` POST must carry:

- `X-Webhook-Signature` — HMAC-SHA256(secret, raw_body), hex
- `X-Webhook-Timestamp` — current Unix seconds (replay protection)
- ±300s timestamp window

If the secret is *not* set the server logs a loud warning and accepts
unsigned posts (the v0.1 single-tenant default).

## Code samples

### curl

```bash
# List recent calls
curl https://bkjr-api.getbijou.xyz/api/calls/recent?limit=10 \
  -H 'X-Api-Key: w3j_…'

# Send an SMS
curl -X POST https://bkjr-api.getbijou.xyz/api/sms/send \
  -H 'X-Api-Key: w3j_…' \
  -H 'Content-Type: application/json' \
  -d '{"to":"+15551110000","from":"+15552220000","text":"hi from agentops"}'

# Export the audit log as CSV
curl https://bkjr-api.getbijou.xyz/v1/audit/export?format=csv \
  -H 'Authorization: Bearer <jwt>' \
  -o audit.csv
```

### Python

```python
import requests

API = "https://bkjr-api.getbijou.xyz"
HEADERS = {"X-Api-Key": "w3j_…"}

# Overview for the last 7 days
r = requests.get(f"{API}/v1/analytics/overview",
                 params={"preset": "7d"}, headers=HEADERS)
r.raise_for_status()
print(r.json()["current"]["total_calls"])

# List voicemails
r = requests.get(f"{API}/api/voicemails",
                 params={"unread": "true", "limit": 50},
                 headers=HEADERS)
for v in r.json()["voicemails"]:
    print(v["from_number"], v["transcript"][:80])
```

### Node

```js
const API = 'https://bkjr-api.getbijou.xyz';
const headers = { 'X-Api-Key': process.env.AGENTOPS_KEY };

// Send SMS
const sms = await fetch(`${API}/api/sms/send`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json', ...headers },
  body: JSON.stringify({ to: '+15551110000', text: 'hello' }),
});
console.log(await sms.json());

// Stream the analytics overview
const r = await fetch(`${API}/v1/analytics/overview?preset=30d&compare=1`, { headers });
const { current, previous, delta } = await r.json();
console.log(`calls: ${current.total_calls} (Δ ${delta.total_calls})`);
```

## Versioning

Endpoints under `/v1/*` are stable. New routes land here. Anything
without a `/vN` prefix is internal and may change between minor
versions.

## Postman

Import the collection:

1. Open Postman → *File → Import*.
2. Paste the URL: `https://bkjr-api.getbijou.xyz/v1/docs/postman.json`.
3. Set the `{{jwt}}` and `{{base_url}}` variables before running.

The collection reads the same `Authorization: Bearer {{jwt}}` and
`X-Api-Key` headers as the curl examples above.
