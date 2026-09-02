#!/usr/bin/env python3
"""Cross-region health checker (issue #29).

Runs every 30s and hits ``/api/health?region=auto`` on each region.
When a probe returns ``status != 'ok'`` the script posts a Discord
webhook (if ``DISCORD_WEBHOOK_URL`` is set) and writes a JSON line to
``--log-file`` so an external dashboard can pick it up.

Usage::

    BACKEND_REGION=us \
    DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/... \
    python ops/failover/healthcheck.py --interval 30

The interval is in seconds. ``--once`` runs a single check and exits
(useful in CI / on-demand). Ctrl-C exits cleanly.

DNS-based probe
---------------
We don't trust TCP alone — a server can be listening but the app is
broken. The endpoint returns a structured matrix, so we just parse
JSON. A failed DNS resolution shows up as ``status="unknown"`` and is
treated as a soft failure (no Discord ping) so the operator isn't
spammed during a Coolify redeploy.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

LOG = logging.getLogger("healthcheck")

REGION_HOSTS = {
    "us": os.environ.get("US_HEALTH_HOST", "us.bkjr-api.getbijou.xyz"),
    "eu": os.environ.get("EU_HEALTH_HOST", "eu.bkjr-api.getbijou.xyz"),
}

# Soft-fail (no alert): DNS doesn't resolve OR the other region isn't
# deployed yet. We only alert on hard HTTP failures.
_SOFT_FAIL_STATUSES = {"unknown"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _probe(host: str, timeout: float = 5.0) -> dict:
    url = f"https://{host}/api/health?region=auto"
    started = time.monotonic()
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(url, timeout=timeout, context=ctx) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        latency_ms = int((time.monotonic() - started) * 1000)
        return {
            "host": host,
            "status": "ok" if resp.status == 200 else "down",
            "http_status": resp.status,
            "latency_ms": latency_ms,
            "body": _safe_json(body),
            "checked_at": _now(),
        }
    except urllib.error.HTTPError as e:
        latency_ms = int((time.monotonic() - started) * 1000)
        return {
            "host": host,
            "status": "down",
            "http_status": e.code,
            "latency_ms": latency_ms,
            "error": f"http {e.code}",
            "checked_at": _now(),
        }
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        return {
            "host": host,
            "status": "down",
            "error": str(e)[:200],
            "checked_at": _now(),
        }


def _safe_json(raw: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw[:200]


def _post_discord(webhook_url: str, content: str) -> bool:
    if not webhook_url:
        return False
    payload = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        LOG.warning("Discord post failed: %s", e)
        return False


def run_once(webhook_url: str | None, log_path: str | None) -> int:
    """Run one probe cycle across all regions. Return 0 on success, 1 if any hard failure."""
    results: list[dict] = []
    for region, host in REGION_HOSTS.items():
        results.append({"region": region, **_probe(host)})

    any_hard_fail = any(
        r["status"] not in ("ok", *_SOFT_FAIL_STATUSES) for r in results
    )

    payload = {
        "checked_at": _now(),
        "results": results,
        "any_hard_fail": any_hard_fail,
    }
    line = json.dumps(payload, default=str)
    print(line, flush=True)
    if log_path:
        try:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as e:
            LOG.warning("Failed to write log %s: %s", log_path, e)

    if any_hard_fail and webhook_url:
        bad = [r for r in results if r["status"] not in ("ok", *_SOFT_FAIL_STATUSES)]
        msg = "🚨 agentops region health failure\n" + "\n".join(
            f"• {r['region']} ({r['host']}): {r['status']} "
            f"{r.get('http_status', '')} {r.get('error', '')}"
            for r in bad
        )
        _post_discord(webhook_url, msg)

    return 1 if any_hard_fail else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="agentops region health checker")
    parser.add_argument("--interval", type=float, default=30.0,
                        help="Seconds between checks (default 30)")
    parser.add_argument("--once", action="store_true",
                        help="Run a single check and exit (CI / on-demand)")
    parser.add_argument("--log-file", default=os.environ.get("HEALTHCHECK_LOG", ""),
                        help="Append a JSON line per cycle to this file")
    args = parser.parse_args()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    webhook_url = (os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()
    log_path = args.log_file.strip() or None

    if args.once:
        return run_once(webhook_url, log_path)

    LOG.info("Starting healthcheck loop: interval=%.1fs, regions=%s",
             args.interval, list(REGION_HOSTS))
    try:
        while True:
            run_once(webhook_url, log_path)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        LOG.info("healthcheck loop exiting (Ctrl-C)")
        return 0


if __name__ == "__main__":
    sys.exit(main())
