"""Place a real test call to verify the live agents work end-to-end.

Uses Telnyx's /v2/texml/ai_calls/{connection_id} endpoint which places an
outbound call AND starts the AI assistant in one API call (no webhook
roundtrip required). Perfect for live testing.

After the call connects, the user picks up +60 112 111 3249 and hears the
AI assistant speak the configured greeting.
"""
import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

load_dotenv()

# W3J LLC Concierge — safest test (no transfer triggers)
W3J_LLC_FROM = "+13079999692"
W3J_LLC_CONN = "3016337470611523194"
W3J_LLC_ASSISTANT = "assistant-3a40bbc4-7630-442a-b009-031300a5afb0"

# W3J Personal Twin — same number from spec, will say "Hey, this is Nurun's AI"
PERSONAL_TWIN_FROM = "+18444618814"
PERSONAL_TWIN_CONN = "3016341331493520609"
PERSONAL_TWIN_ASSISTANT = "assistant-2260cf49-d38e-47c6-94cb-9aa110555df7"

# Bijou AI — Manglish voice
BIJOU_FROM = "+13204280793"
BIJOU_CONN = "3016337410440037999"
BIJOU_ASSISTANT = "assistant-36e1b342-531c-41ab-86c4-8f8bda025ec9"

# User's cell — destination for all test calls
USER_CELL = "+601121113249"

key = os.getenv("TELNYX_ORGANIZATION_API_KEY")
if not key:
    print("ERROR: TELNYX_ORGANIZATION_API_KEY not set in .env")
    sys.exit(1)


def place_ai_call(label: str, from_num: str, conn_id: str, assistant_id: str, to: str = USER_CELL) -> dict:
    """Place an outbound call that auto-starts the AI assistant."""
    print(f"\n=== Placing call: {label} ===")
    print(f"  from:  {from_num}")
    print(f"  to:    {to}")
    print(f"  conn:  {conn_id}")
    print(f"  ast:   {assistant_id}")
    r = httpx.post(
        f"https://api.telnyx.com/v2/texml/ai_calls/{conn_id}",
        json={
            "From": from_num,
            "To": to,
            "AIAssistantId": assistant_id,
        },
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    print(f"  status: {r.status_code}")
    print(f"  body:   {r.text[:600]}")
    return {
        "label": label,
        "status": r.status_code,
        "body": r.text[:600],
    }


if __name__ == "__main__":
    # The W3J LLC Concierge is the safest first test (no transfer triggers,
    # no personal assistant edge cases). Start with that.
    target = sys.argv[1] if len(sys.argv) > 1 else "w3j-llc"

    if target == "w3j-llc":
        result = place_ai_call("W3J LLC Concierge", W3J_LLC_FROM, W3J_LLC_CONN, W3J_LLC_ASSISTANT)
    elif target == "twin":
        result = place_ai_call("W3J Personal Twin", PERSONAL_TWIN_FROM, PERSONAL_TWIN_CONN, PERSONAL_TWIN_ASSISTANT)
    elif target == "bijou":
        result = place_ai_call("Bijou AI Concierge", BIJOU_FROM, BIJOU_CONN, BIJOU_ASSISTANT)
    elif target == "all":
        # Place all 3 in sequence so you can test all 3 in one go
        results = []
        for label, f, c, a in [
            ("W3J LLC", W3J_LLC_FROM, W3J_LLC_CONN, W3J_LLC_ASSISTANT),
            ("Bijou AI", BIJOU_FROM, BIJOU_CONN, BIJOU_ASSISTANT),
            ("Personal Twin", PERSONAL_TWIN_FROM, PERSONAL_TWIN_CONN, PERSONAL_TWIN_ASSISTANT),
        ]:
            results.append(place_ai_call(label, f, c, a))
        print("\n=== Summary ===")
        for r in results:
            print(f"  {r['label']}: status {r['status']}")
    else:
        print(f"Unknown target: {target}. Use: w3j-llc | twin | bijou | all")
        sys.exit(1)

    print("\nNext: pick up +60 112 111 3249. You should hear the AI assistant speak the configured greeting.")
