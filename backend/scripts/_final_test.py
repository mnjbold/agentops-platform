"""Verify Malaysian phone normalization + webhook is up with specialist mapping."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dispatcher.service import normalize_phone
import httpx

# Phone normalization
cases = [
    ("+60121234567",   "+60121234567"),
    ("+1 213 555 1234", "+12135551234"),
    ("213 555 1234",   "+12135551234"),
    ("011 1234 5678",  "+60112345678"),
    ("01121234567",    "+601121234567"),
    ("+60 12 123 4567", "+60121234567"),
    ("18005551234",    "+18005551234"),
    ("  +12135551234", "+12135551234"),
    ("+44 20 7946 0958", "+442079460958"),
]
print("=== Phone normalization ===")
ok = 0
for raw, expected in cases:
    got = normalize_phone(raw)
    status = "OK" if got == expected else "FAIL"
    if got == expected:
        ok += 1
    print(f"  [{status}] normalize_phone({raw!r:30}) = {got!r}  (expected {expected!r})")
print(f"  {ok}/{len(cases)} passed\n")

# Webhook health + specialist mapping loaded
print("=== Webhook receiver ===")
r = httpx.get("http://127.0.0.1:8080/health", timeout=5)
print(f"  health: {r.status_code} {r.json()}")
