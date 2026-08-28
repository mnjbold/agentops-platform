"""Probe the right shape for a webhook tool on a Telnyx AI Assistant."""
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
load_dotenv()
key = os.getenv("TELNYX_ORGANIZATION_API_KEY")

# Try the canonical shape
shape = {
    "type": "webhook",
    "webhook": {
        "name": "test_connect_specialist",
        "description": "Test webhook tool",
        "url": "https://example.com/webhook",
        "method": "POST",
        "body_parameters": {
            "type": "object",
            "properties": {
                "specialist": {"type": "string", "description": "Which specialist"},
                "context": {"type": "string", "description": "Why"},
            },
            "required": ["specialist"],
        },
    },
}

r = httpx.post(
    "https://api.telnyx.com/v2/ai/assistants",
    json={
        "name": "_test_webhook_tool",
        "instructions": "test",
        "model": "openai/gpt-4o",
        "tools": [shape],
    },
    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    timeout=15,
)
print(f"Status: {r.status_code}")
print(f"Body: {r.text[:600]}")
if r.status_code < 300:
    aid = r.json()["data"]["id"]
    httpx.delete(f"https://api.telnyx.com/v2/ai/assistants/{aid}",
                 headers={"Authorization": f"Bearer {key}"}, timeout=10)
    print("Cleaned up test assistant.")
