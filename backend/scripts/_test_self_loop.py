"""Quick test: dial +60 112 111 3249 from +1 844 461 8814 (personal twin)."""
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
load_dotenv()
key = os.getenv("TELNYX_ORGANIZATION_API_KEY")
if not key:
    print("ERROR: no key")
    sys.exit(1)

# Personal twin's call control app + assistant
CONN_ID = "3016341331493520609"
ASSISTANT = "assistant-2260cf49-d38e-47c6-94cb-9aa110555df7"

print("Dialing +60 112 111 3249 from +1 844 461 8814 (personal twin, Azure Guy voice)...")
r = httpx.post(
    f"https://api.telnyx.com/v2/texml/ai_calls/{CONN_ID}",
    json={"From": "+18444618814", "To": "+601121113249", "AIAssistantId": ASSISTANT},
    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    timeout=30,
)
print(f"  status: {r.status_code}")
print(f"  body:   {r.text[:400]}")
if r.status_code == 200:
    print()
    print(">>> PICK UP +60 112 111 3249 NOW <<<")
    print("You should hear: 'Hey, this is Nurun's AI assistant. What's up?' (Azure Guy voice)")
    print()
    print("Test scenarios:")
    print("  - Say 'hi'  -> AI responds naturally")
    print("  - Say 'I'm Nurun'  -> AI might try to transfer (which will loop)")
    print("  - Say 'never mind'  -> AI says something natural, doesn't say 'have a great day'")
