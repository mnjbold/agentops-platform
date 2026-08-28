#!/usr/bin/env python3
"""Sign a webhook payload for posting to the local dashboard server.

Usage
-----
    # Sign a JSON file and print headers
    python scripts/sign_webhook.py payload.json

    # Sign stdin
    echo '{"event_type":"call.initiated"}' | python scripts/sign_webhook.py -

    # Sign and POST directly
    python scripts/sign_webhook.py payload.json --post https://bk-jr-api.aixlabs.fun/webhooks/telnyx

The script reads WEBHOOK_HMAC_SECRET from .env (in the project root) or
the environment, computes a ``t=...,v1=...`` signature, and prints the
two headers plus a ready-to-run curl command.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_env_secret() -> str:
    """Read WEBHOOK_HMAC_SECRET from env or project .env file."""
    sec = os.environ.get("WEBHOOK_HMAC_SECRET", "").strip()
    if sec:
        return sec
    env_path = _PROJECT_ROOT / ".env"
    if not env_path.exists():
        return ""
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == "WEBHOOK_HMAC_SECRET":
            return v.strip().strip('"').strip("'")
    return ""


def main() -> int:
    p = argparse.ArgumentParser(description="Sign a webhook payload for the local dashboard server.")
    p.add_argument("payload", help="Path to a JSON file, or '-' for stdin.")
    p.add_argument("--post", metavar="URL", help="POST the signed payload to this URL after signing.")
    p.add_argument("--ts", type=int, default=None, help="Override the timestamp (default: now).")
    args = p.parse_args()

    secret = _load_env_secret()
    if not secret:
        print("ERROR: WEBHOOK_HMAC_SECRET is not set in env or .env", file=sys.stderr)
        return 2

    # Read payload
    if args.payload == "-":
        body = sys.stdin.buffer.read()
    else:
        body = Path(args.payload).read_bytes()
    # Validate it parses
    try:
        json.loads(body.decode("utf-8"))
    except Exception as e:
        print(f"ERROR: payload is not valid JSON: {e}", file=sys.stderr)
        return 2

    ts = args.ts if args.ts is not None else int(time.time())
    sys.path.insert(0, str(_PROJECT_ROOT))
    from webhooks.security import sign_payload  # type: ignore
    sig = sign_payload(secret, ts, body)

    print(f"X-Webhook-Timestamp: {ts}")
    print(f"X-Webhook-Signature: {sig}")
    if args.post:
        req = urllib.request.Request(
            args.post,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Timestamp": str(ts),
                "X-Webhook-Signature": sig,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                print(f"\nPOST {args.post}\n  status: {resp.status}")
                data = resp.read().decode("utf-8", errors="replace")
                if data:
                    print(f"  body:   {data[:400]}")
        except urllib.error.HTTPError as e:
            print(f"\nPOST {args.post}\n  status: {e.code}\n  body:   {e.read().decode('utf-8', errors='replace')[:400]}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
