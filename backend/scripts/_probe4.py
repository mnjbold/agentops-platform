"""Final SDK test - pass key to constructor."""
import os
import telnyx
from dotenv import load_dotenv

load_dotenv()
org_key = os.getenv("TELNYX_ORGANIZATION_API_KEY")

# Pass key to constructor
t = telnyx.Telnyx(api_key=org_key)
print(f"Client api_key set: {bool(t.api_key)}, first chars: {t.api_key[:15] if t.api_key else 'None'}")
print()

print("=== Balance ===")
bal = t.balance.retrieve()
print(f"  data: {bal.data if hasattr(bal, 'data') else bal}")
print()

print("=== Available CA 213 numbers (filter syntax) ===")
import inspect
from telnyx.resources.available_phone_numbers import AvailablePhoneNumbersResource
print(f"  list sig: {inspect.signature(AvailablePhoneNumbersResource.list)}")
print()

# Try the simplest list call
try:
    avail = t.available_phone_numbers.list()
    data = list(avail)
    print(f"  All available (no filter): {len(data)}")
except Exception as e:
    print(f"  list() failed: {e}")

# Try with filter
try:
    avail = t.available_phone_numbers.list(
        country_code="US",
        national_destination_code="213",
    )
    data = list(avail)
    print(f"  213 with kwargs: {len(data)}")
    for n in data[:3]:
        print(f"    - {n.phone_number} | features={getattr(n, 'features', '?')}")
except Exception as e:
    print(f"  kwargs failed: {e}")
