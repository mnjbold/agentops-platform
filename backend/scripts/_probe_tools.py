"""Find the J.A.R.V.I.S. assistant and dump its tools/voice/transcription format."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telnyx_mcp.clients.telnyx_client import get_client, to_dict

c = get_client()
assts = c.list_assistants()
for a in assts:
    name = a.get("name") or ""
    if "jarvis" in name.lower() or "windows" in name.lower():
        print(f"=== {name} ({a.get('id')}) ===")
        for k, v in a.items():
            if k in ("name", "id"):
                continue
            if isinstance(v, (list, dict)):
                import json
                print(f"  {k}: {json.dumps(v, indent=2, default=str)[:500]}")
            else:
                print(f"  {k}: {v}")
        print()
