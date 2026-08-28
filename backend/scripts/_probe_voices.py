"""Probe which TTS voice IDs Telnyx accepts."""
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

load_dotenv()
key = os.getenv("TELNYX_ORGANIZATION_API_KEY")

candidates = [
    "Telnyx.OpenAI.tts-1.nova",
    "Telnyx.OpenAI.tts-1-hd.nova",
    "openai/tts-1.nova",
    "OpenAI.tts-1",
    "openai.tts-1",
    "AWS.Polly.Joanna",
    "Azure.en-US-JennyNeural",
    "Telnyx.KokoroTTS.af_heart",  # baseline
]

for v in candidates:
    safe = v.replace(".", "_").replace("/", "_")[:50]
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
        # clean up
        httpx.delete(
            f"https://api.telnyx.com/v2/ai/assistants/{aid}",
            headers={"Authorization": f"Bearer {key}"}, timeout=10,
        )
        print(f"  [OK]   {v}  ->  {aid}")
    else:
        print(f"  [FAIL] {v}  ->  {r.status_code}  {r.text[:200]}")
