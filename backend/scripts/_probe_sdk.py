"""Probe the Telnyx SDK v4 to map the resource surface."""
import os
import telnyx
from dotenv import load_dotenv

load_dotenv()
telnyx.api_key = os.getenv("TELNYX_API_KEY")

print("=== Telnyx SDK Resource Surface ===")
print(f"SDK version: {telnyx.__version__ if hasattr(telnyx, '__version__') else 'unknown'}")
print()
print("Resources with create/list/delete methods:")
for name in sorted(dir(telnyx)):
    if name.startswith("_") or not name[0].isupper():
        continue
    obj = getattr(telnyx, name)
    has_create = hasattr(obj, "create")
    has_list = hasattr(obj, "list")
    if has_create or has_list:
        methods = [m for m in dir(obj) if not m.startswith("_") and callable(getattr(obj, m, None))]
        crud = [m for m in methods if m in ("create", "list", "retrieve", "update", "delete", "modify")]
        print(f"  {name:40s}  CRUD: {','.join(crud) if crud else 'other'}")

print()
print("=== Live API smoke test ===")
try:
    nums = telnyx.PhoneNumber.list(page={"size": 5})
    print(f"  Numbers owned: {len(nums.data)}")
    for n in nums.data:
        print(f"    - {n.phone_number} | status={n.status}")
except Exception as e:
    print(f"  PhoneNumber.list failed: {e}")

try:
    apps = telnyx.CallControlApplication.list(page={"size": 5})
    print(f"  Call Control Apps: {len(apps.data)}")
    for a in apps.data:
        print(f"    - {a.id} | {a.application_name} | conn_id={a.call_control_connection_id or '-'}")
except Exception as e:
    print(f"  CallControlApplication.list failed: {e}")

try:
    profiles = telnyx.OutboundVoiceProfile.list(page={"size": 5})
    print(f"  Outbound Voice Profiles: {len(profiles.data)}")
except Exception as e:
    print(f"  OutboundVoiceProfile.list failed: {e}")

try:
    assistants = telnyx.Assistant.list(page={"size": 5})
    print(f"  AI Assistants: {len(assistants.data)}")
    for a in assistants.data:
        print(f"    - {a.id} | name={a.name} | model={getattr(a, 'model', '?')}")
except Exception as e:
    print(f"  Assistant.list failed: {e}")

try:
    available = telnyx.AvailablePhoneNumber.list(
        filter={"country_code": "US", "national_destination_code": "213", "limit": 3}
    )
    print(f"  Available 213-CA numbers: {len(available.data)}")
    for n in available.data[:3]:
        print(f"    - {n.phone_number} | cost={getattr(n, 'cost_information', '?')}")
except Exception as e:
    print(f"  AvailablePhoneNumber.list failed: {e}")

try:
    balance = telnyx.Balance.retrieve()
    print(f"  Balance: {getattr(balance, 'data', balance)}")
except Exception as e:
    print(f"  Balance.retrieve failed: {e}")
