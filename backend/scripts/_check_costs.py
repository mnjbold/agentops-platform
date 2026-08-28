"""Check number costs across California area codes."""
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
load_dotenv()
key = os.getenv("TELNYX_ORGANIZATION_API_KEY")
HEADERS = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

for ac in ["213", "310", "323", "341", "415", "510", "628", "650", "707", "818", "925", "951"]:
    r = httpx.get(
        "https://api.telnyx.com/v2/available_phone_numbers",
        params={
            "filter[country_code]": "US",
            "filter[national_destination_code]": ac,
            "filter[limit]": 1,
            "filter[best_effort]": "false",
        },
        headers=HEADERS, timeout=15,
    )
    data = r.json().get("data", [])
    if data:
        n = data[0]
        cost = n.get("cost_information", {})
        upfront = cost.get("upfront_cost", "?")
        monthly = cost.get("monthly_cost", "?")
        print(f"  {ac}: {n['phone_number']}  upfront=${upfront}  monthly=${monthly}")
    else:
        print(f"  {ac}: no numbers")
