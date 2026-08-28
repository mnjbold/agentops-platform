"""Full Telnyx live smoke test - using org API key + correct pagination."""
import os
import telnyx
from dotenv import load_dotenv

load_dotenv()
# Org key works for full API; the JWT API key is scoped to ie_model
telnyx.api_key = os.getenv("TELNYX_ORGANIZATION_API_KEY") or os.getenv("TELNYX_API_KEY")
print(f"Using API key: {telnyx.api_key[:20]}...")
print()

t = telnyx.Telnyx()

print("=== Balance ===")
try:
    bal = t.balance.retrieve()
    print(f"  Balance data: {bal.data if hasattr(bal, 'data') else bal}")
except Exception as e:
    print(f"  failed: {e}")

print()
print("=== Owned Phone Numbers ===")
try:
    nums = t.phone_numbers.list(page_size=10)
    data = list(nums)
    print(f"  Owned: {len(data)}")
    for n in data:
        print(f"    - {n.phone_number} | status={n.status} | conn_id={n.connection_id}")
except Exception as e:
    print(f"  failed: {e}")

print()
print("=== Available CA 213 numbers ===")
try:
    avail = t.available_phone_numbers.list(
        filter={"country_code": "US", "national_destination_code": "213"},
        page_size=5,
    )
    data = list(avail)
    print(f"  Available 213: {len(data)}")
    for n in data:
        print(f"    - {n.phone_number} | features={getattr(n, 'features', '?')} | cost={getattr(n, 'cost_information', '?')}")
except Exception as e:
    print(f"  failed: {e}")

print()
print("=== Call Control Applications ===")
try:
    apps = t.call_control_applications.list(page_size=10)
    data = list(apps)
    print(f"  Apps: {len(data)}")
    for a in data:
        print(f"    - id={a.id} | name={a.application_name} | conn_id={a.call_control_connection_id}")
except Exception as e:
    print(f"  failed: {e}")

print()
print("=== Outbound Voice Profiles ===")
try:
    p = t.outbound_voice_profiles.list(page_size=10)
    data = list(p)
    print(f"  Profiles: {len(data)}")
    for x in data:
        print(f"    - id={x.id} | name={x.name}")
except Exception as e:
    print(f"  failed: {e}")

print()
print("=== AI Assistants ===")
try:
    a = t.ai.assistants.list(page_size=10)
    data = list(a)
    print(f"  Assistants: {len(data)}")
    for x in data:
        print(f"    - id={x.id} | name={getattr(x, 'name', '?')}")
except Exception as e:
    print(f"  failed: {e}")

print()
print("=== Voice Clones ===")
try:
    vc = t.voice_clones.list()
    data = list(vc)
    print(f"  Voice clones: {len(data)}")
    for x in data:
        print(f"    - id={x.id} | name={getattr(x, 'name', '?')}")
except Exception as e:
    print(f"  failed: {e}")

print()
print("=== Voice Designs ===")
try:
    vd = t.voice_designs.list()
    data = list(vd)
    print(f"  Voice designs: {len(data)}")
    for x in data:
        print(f"    - id={x.id} | name={getattr(x, 'name', '?')}")
except Exception as e:
    print(f"  failed: {e}")
