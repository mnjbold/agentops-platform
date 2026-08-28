"""One-shot script: bootstrap the Appwrite schema for agentops.

Creates (idempotently):
  - database "agentops"
  - collection "contacts"      — agentops contacts
  - collection "campaigns"     — outbound voice / SMS campaigns
  - collection "telnyx_events"  — audit log of inbound webhook events
  - collection "scheduled_jobs" — SaaS scheduler queue

Usage:
    # 1) ensure backend/settings.json exists with real credentials, OR
    # 2) set env vars: APPWRITE_API_KEY, APPWRITE_PROJECT_ID, APPWRITE_ENDPOINT
    # then:
    python -m appx.bootstrap
"""
from __future__ import annotations

import logging
import sys
import time

from appx.client import get_appwrite

log = logging.getLogger("appwrite.bootstrap")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# Schema definition. Keep this in sync with ARCHITECTURE.md § Data model.
DATABASE_ID = "agentops"

COLLECTIONS = [
    {
        "collectionId": "contacts",
        "name": "agentops contacts",
        "attributes": [
            {"key": "tenant_id", "type": "string", "size": 64, "required": True},
            {"key": "name", "type": "string", "size": 256, "required": False},
            {"key": "phone", "type": "string", "size": 32, "required": True},
            {"key": "email", "type": "string", "size": 256, "required": False},
            {"key": "tags", "type": "string", "size": 1024, "required": False, "array": True},
            {"key": "source", "type": "string", "size": 32, "required": False},
        ],
        "indexes": [
            {"key": "idx_tenant_phone", "type": "unique", "attributes": ["tenant_id", "phone"]},
            {"key": "idx_tenant", "type": "key", "attributes": ["tenant_id"]},
        ],
    },
    {
        "collectionId": "campaigns",
        "name": "agentops campaigns",
        "attributes": [
            {"key": "tenant_id", "type": "string", "size": 64, "required": True},
            {"key": "name", "type": "string", "size": 256, "required": True},
            {"key": "type", "type": "string", "size": 16, "required": True},  # 'sms' or 'voice'
            {"key": "from_number", "type": "string", "size": 32, "required": False},
            {"key": "message", "type": "string", "size": 4096, "required": False},
            {"key": "schedule_at", "type": "datetime", "required": False},
            {"key": "status", "type": "string", "size": 16, "required": True},
            {"key": "stats_json", "type": "string", "size": 1024, "required": False},
        ],
        "indexes": [
            {"key": "idx_tenant", "type": "key", "attributes": ["tenant_id"]},
            {"key": "idx_tenant_status", "type": "key", "attributes": ["tenant_id", "status"]},
        ],
    },
    {
        "collectionId": "telnyx_events",
        "name": "telnyx webhook events (audit log)",
        "attributes": [
            {"key": "event_type", "type": "string", "size": 64, "required": True},
            {"key": "call_control_id", "type": "string", "size": 128, "required": False},
            {"key": "from_number", "type": "string", "size": 32, "required": False},
            {"key": "to_number", "type": "string", "size": 32, "required": False},
            {"key": "direction", "type": "string", "size": 16, "required": False},
            {"key": "raw_json", "type": "string", "size": 16384, "required": False},
            {"key": "received_at", "type": "datetime", "required": True},
        ],
        "indexes": [
            {"key": "idx_event_type", "type": "key", "attributes": ["event_type"]},
            {"key": "idx_cci", "type": "key", "attributes": ["call_control_id"]},
            {"key": "idx_received", "type": "key", "attributes": ["received_at"]},
        ],
    },
    {
        "collectionId": "scheduled_jobs",
        "name": "agentops scheduled jobs (SMS, campaign launch)",
        "attributes": [
            {"key": "tenant_id", "type": "string", "size": 64, "required": True},
            {"key": "kind", "type": "string", "size": 32, "required": True},
            {"key": "payload_json", "type": "string", "size": 8192, "required": True},
            {"key": "run_at", "type": "datetime", "required": True},
            {"key": "status", "type": "string", "size": 16, "required": True},
            {"key": "last_error", "type": "string", "size": 2048, "required": False},
        ],
        "indexes": [
            {"key": "idx_run_at_status", "type": "key", "attributes": ["run_at", "status"]},
        ],
    },
]


def run() -> int:
    aw = get_appwrite()
    client = aw.client

    # 1. Create the database (idempotent — Appwrite returns 409 if exists)
    try:
        client.databases.create(database_id=DATABASE_ID, name="agentops production")
        log.info("Created database %s", DATABASE_ID)
    except Exception as e:
        if "already exists" in str(e).lower() or "409" in str(e):
            log.info("Database %s already exists", DATABASE_ID)
        else:
            log.error("Database create failed: %s", e)
            return 1

    # 2. Create each collection with attributes + indexes
    for col in COLLECTIONS:
        coll_id = col["collectionId"]
        try:
            client.databases.create_collection(
                database_id=DATABASE_ID,
                collection_id=coll_id,
                name=col["name"],
            )
            log.info("Created collection %s", coll_id)
        except Exception as e:
            if "already exists" in str(e).lower() or "409" in str(e):
                log.info("Collection %s already exists", coll_id)
            else:
                log.error("Collection %s create failed: %s", coll_id, e)
                return 1

        # Attributes — Appwrite returns 202 (Accepted, async build)
        for attr in col["attributes"]:
            try:
                kind = attr["type"]
                method = getattr(client.databases, f"create_{kind}_attribute", None)
                if not method:
                    log.error("unknown attr type: %s", kind)
                    return 1
                method(database_id=DATABASE_ID, collection_id=coll_id, **attr)
                log.info("  +attr %s (%s)", attr["key"], kind)
                time.sleep(0.3)  # Appwrite is async; let it settle
            except Exception as e:
                msg = str(e).lower()
                if "already exists" in msg or "409" in msg or "attribute_already_exists" in msg:
                    log.info("  attr %s already exists", attr["key"])
                else:
                    log.error("  attr %s failed: %s", attr["key"], e)
                    return 1

        # Indexes
        for idx in col["indexes"]:
            try:
                client.databases.create_index(
                    database_id=DATABASE_ID,
                    collection_id=coll_id,
                    key=idx["key"],
                    type=idx["type"],
                    attributes=idx["attributes"],
                )
                log.info("  +index %s", idx["key"])
                time.sleep(0.3)
            except Exception as e:
                msg = str(e).lower()
                if "already exists" in msg or "409" in msg or "index_already_exists" in msg:
                    log.info("  index %s already exists", idx["key"])
                else:
                    log.error("  index %s failed: %s", idx["key"], e)
                    return 1

    log.info("Done. Database: %s, collections: %s", DATABASE_ID, [c["collectionId"] for c in COLLECTIONS])
    return 0


if __name__ == "__main__":
    sys.exit(run())
