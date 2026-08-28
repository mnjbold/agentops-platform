"""Appwrite Function: receives Telnyx webhooks and writes to Appwrite DB.

This runs in Appwrite's serverless runtime (Python 3.12). It mirrors the
behavior of the local `webhooks/handlers/default.py` but stores event data
in Appwrite Database instead of (or in addition to) pushing to the WS broker.

Deploy with:
    appwrite deploy function

Requires the Appwrite CLI. The function id is set in appwrite.json.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

# Appwrite Function runtime provides these as globals
# (see https://appwrite.io/docs/products/functions/develop-python)
try:
    from appwrite.client import Client
    from appwrite.services.databases import Databases
    from appwrite.input_file import InputFile
    from appwrite.exception import AppwriteException
except ImportError:  # local dev fallback
    Client = None  # type: ignore
    Databases = None  # type: ignore
    AppwriteException = Exception  # type: ignore

log = logging.getLogger("telnyx_webhook")
log.setLevel(logging.INFO)


def main(context):  # type: ignore[no-untyped-def]
    """Appwrite Function entry point.

    `context.req` is the incoming HTTP request (method, body, headers).
    `context.res` is the response we build and return.
    """
    if context.req.method != "POST":
        return context.res.json({"error": "method not allowed"}, 405)

    try:
        body = json.loads(context.req.body or "{}")
    except json.JSONDecodeError as e:
        return context.res.json({"error": f"invalid json: {e}"}, 400)

    event_type = body.get("event_type", "")
    payload = body.get("payload", {}) or {}

    # Optional: write a row to Appwrite DB so we can build dashboards
    # without a separate FastAPI process. Disabled by default — uncomment
    # when the Appwrite instance is reachable.
    #
    # if Client is not None and os.environ.get("APPWRITE_DATABASE_ID"):
    #     client = Client()
    #     client.set_endpoint(os.environ["APPWRITE_ENDPOINT"])
    #     client.set_project(os.environ["APPWRITE_PROJECT_ID"])
    #     client.set_key(os.environ["APPWRITE_API_KEY"])
    #     db = Databases(client)
    #     db.create_document(
    #         database_id=os.environ["APPWRITE_DATABASE_ID"],
    #         collection_id=os.environ.get("APPWRITE_COLLECTION_EVENTS", "telnyx_events"),
    #         document_id="unique()",
    #         data={
    #             "event_type": event_type,
    #             "received_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
    #             "call_control_id": payload.get("call_control_id"),
    #             "from": (payload.get("from") or {}).get("phone_number"),
    #             "to": (payload.get("to") or {}).get("phone_number"),
    #             "raw": json.dumps(body)[:8000],
    #         },
    #     )

    log.info("Telnyx event: %s", event_type)
    return context.res.json({"handled": True, "event_type": event_type}, 200)


if __name__ == "__main__":
    # Local dev: simulate a request so we can run the function from CLI
    fake_context = type("Ctx", (), {
        "req": type("Req", (), {"method": "POST", "body": sys.stdin.read()})(),
        "res": type("Res", (), {"json": staticmethod(lambda d, c=200: (print(json.dumps(d)), c))})(),
    })()
    main(fake_context)
