"""Probe Telnyx SDK namespaces - AI, voice clones, calls, messages."""
import os
import telnyx
from dotenv import load_dotenv

load_dotenv()
telnyx.api_key = os.getenv("TELNYX_API_KEY")
t = telnyx.Telnyx()

print("=== AI namespace ===")
if hasattr(t, "ai"):
    ai = t.ai
    print("  ai methods:", sorted([x for x in dir(ai) if not x.startswith("_")]))

print()
print("=== Voice Clones ===")
if hasattr(t, "voice_clones"):
    vc = t.voice_clones
    print("  voice_clones methods:", sorted([x for x in dir(vc) if not x.startswith("_")]))
    try:
        ls = vc.list(page={"size": 5})
        print(f"  Existing voice clones: {len(ls.data)}")
        for c in ls.data:
            print(f"    - {getattr(c, 'id', '?')} | name={getattr(c, 'name', '?')}")
    except Exception as e:
        print(f"  list failed: {e}")

print()
print("=== Voice Designs ===")
if hasattr(t, "voice_designs"):
    vd = t.voice_designs
    print("  voice_designs methods:", sorted([x for x in dir(vd) if not x.startswith("_")]))
    try:
        ls = vd.list(page={"size": 5})
        print(f"  Existing voice designs: {len(ls.data)}")
    except Exception as e:
        print(f"  list failed: {e}")

print()
print("=== Calls (call control commands) ===")
calls = t.calls
print("  calls methods:", sorted([m for m in dir(calls) if not m.startswith("_") and callable(getattr(calls, m, None))]))
if hasattr(calls, "actions"):
    print("  calls.actions methods:", sorted([m for m in dir(calls.actions) if not m.startswith("_") and callable(getattr(calls.actions, m, None))]))

print()
print("=== Live: Phone Numbers (owned) ===")
try:
    nums = t.phone_numbers.list(page={"size": 10})
    print(f"  Owned: {len(nums.data)}")
    for n in nums.data:
        print(f"    - {n.phone_number} | status={n.status} | conn_id={n.connection_id}")
except Exception as e:
    print(f"  failed: {e}")

print()
print("=== Live: AI Assistants (telnyx.ai.assistants) ===")
if hasattr(t, "ai") and hasattr(t.ai, "assistants"):
    try:
        a = t.ai.assistants.list(page={"size": 5})
        print(f"  AI assistants: {len(a.data)}")
        for x in a.data:
            print(f"    - id={x.id} | name={getattr(x, 'name', '?')}")
    except Exception as e:
        print(f"  failed: {e}")
else:
    print("  t.ai.assistants not found; trying alternate paths")
    if hasattr(t, "assistants"):
        print("  t.assistants exists")
    print("  ai subnamespace:", [x for x in dir(t.ai) if not x.startswith("_")])

print()
print("=== Live: Available CA numbers (213) ===")
try:
    avail = t.available_phone_numbers.list(
        filter={"country_code": "US", "national_destination_code": "213", "limit": 5}
    )
    print(f"  Available 213 numbers: {len(avail.data)}")
    for n in avail.data:
        print(f"    - {n.phone_number} | features={getattr(n, 'features', '?')}")
except Exception as e:
    print(f"  failed: {e}")

print()
print("=== Live: Balance ===")
try:
    bal = t.balance.retrieve()
    print(f"  Balance: {bal.data if hasattr(bal, 'data') else bal}")
except Exception as e:
    print(f"  failed: {e}")

print()
print("=== Live: Call Control Applications ===")
try:
    apps = t.call_control_applications.list(page={"size": 10})
    print(f"  Apps: {len(apps.data)}")
    for a in apps.data:
        print(f"    - id={a.id} | name={a.application_name} | conn_id={a.call_control_connection_id}")
except Exception as e:
    print(f"  failed: {e}")
