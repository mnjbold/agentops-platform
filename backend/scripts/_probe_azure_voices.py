"""Probe the most natural Azure neural voices available on Telnyx."""
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

load_dotenv()
key = os.getenv("TELNYX_ORGANIZATION_API_KEY")

# Azure neural voices that sound the most human
candidates = [
    "Azure.en-US-AriaNeural",
    "Azure.en-US-DavisNeural",
    "Azure.en-US-JennyNeural",
    "Azure.en-US-GuyNeural",
    "Azure.en-US-SaraNeural",
    "Azure.en-US-TonyNeural",
    "Azure.en-US-NancyNeural",
]

for v in candidates:
    safe = v.replace(".", "_")[:50]
    name = f"_test_voice_{safe}"
    r = httpx.post(
        "https://api.telnyx.com/v2/ai/assistants",
        json={
            "name": name,
            "instructions": "You are a test. Say hi.",
            "model": "openai/gpt-4o",
            "voice_settings": {"voice": v, "language_boost": "auto"},
            "transcription": {"model": "deepgram/nova-3", "language": "en"},
        },
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        timeout=15,
    )
    if r.status_code < 300:
        d = r.json().get("data", {})
        aid = d.get("id", "?")
        httpx.delete(
            f"https://api.telnyx.com/v2/ai/assistants/{aid}",
            headers={"Authorization": f"Bearer {key}"}, timeout=10,
        )
        print(f"  [OK]   {v}  ->  {aid}")
    else:
        print(f"  [FAIL] {v}  ->  {r.status_code}  {r.text[:150]}")
