"""Final live smoke test - all surfaces, correct SDK usage."""
import os
import telnyx
from dotenv import load_dotenv

load_dotenv()
t = telnyx.Telnyx(api_key=os.getenv("TELNYX_ORGANIZATION_API_KEY"))

print("=== Owned Phone Numbers ===")
nums = t.phone_numbers.list(page_size=20)
data = list(nums)
print(f"  Owned: {len(data)}")
for n in data:
    print(f"    - {n.phone_number} | status={n.status} | conn_id={n.connection_id}")

print()
print("=== Available CA 213 numbers ===")
avail = t.available_phone_numbers.list(
    filter={"country_code": "US", "national_destination_code": "213"}
)
data = list(avail)
print(f"  Available 213: {len(data)}")
for n in data[:5]:
    print(f"    - {n.phone_number} | features={getattr(n, 'features', '?')}")

print()
print("=== Available CA 510 numbers (Oakland) ===")
avail = t.available_phone_numbers.list(
    filter={"country_code": "US", "national_destination_code": "510"}
)
data = list(avail)
print(f"  Available 510: {len(data)}")
for n in data[:5]:
    print(f"    - {n.phone_number}")

print()
print("=== Call Control Applications ===")
apps = t.call_control_applications.list(page_size=20)
data = list(apps)
print(f"  Apps: {len(data)}")
for a in data:
    print(f"    - id={a.id} | name={a.application_name} | conn_id={a.call_control_connection_id}")

print()
print("=== Outbound Voice Profiles ===")
p = t.outbound_voice_profiles.list(page_size=20)
data = list(p)
print(f"  Profiles: {len(data)}")
for x in data:
    print(f"    - id={x.id} | name={x.name}")

print()
print("=== AI Assistants ===")
a = t.ai.assistants.list()
data = list(a)
print(f"  Assistants: {len(data)}")
for x in data:
    print(f"    - id={x.id} | name={getattr(x, 'name', '?')}")

print()
print("=== Voice Clones ===")
vc = t.voice_clones.list()
data = list(vc)
print(f"  Voice clones: {len(data)}")
for x in data:
    print(f"    - id={x.id} | name={getattr(x, 'name', '?')}")

print()
print("=== TeXML Applications ===")
try:
    tx = t.texml_applications.list(page_size=20)
    data = list(tx)
    print(f"  TeXML apps: {len(data)}")
    for x in data:
        print(f"    - id={x.id} | name={x.friendly_name}")
except Exception as e:
    print(f"  failed: {e}")

print()
print("=== Connections (SIP) ===")
try:
    c = t.connections.list(page_size=20)
    data = list(c)
    print(f"  Connections: {len(data)}")
    for x in data:
        print(f"    - id={x.id} | name={x.connection_name} | type={x.active} | record_type={x.record_type}")
except Exception as e:
    print(f"  failed: {e}")

print()
print("=== Messaging Profiles ===")
try:
    mp = t.messaging_profiles.list(page_size=20)
    data = list(mp)
    print(f"  Profiles: {len(data)}")
    for x in data:
        print(f"    - id={x.id} | name={x.name}")
except Exception as e:
    print(f"  failed: {e}")
