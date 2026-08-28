"""Dump each agent's deployment.json summary."""
import json
from pathlib import Path

for name in ['bijou-ai-concierge', 'w3j-llc-concierge', 'w3j-personal-twin']:
    p = Path(f'agents/{name}/deployment.json')
    if not p.exists():
        print(f"=== {name} ===  no deployment.json")
        continue
    d = json.loads(p.read_text())
    print(f"=== {name} ===")
    print(f"  phone_number:       {d.get('phone_number')}")
    a = d.get("assistant") or {}
    app = d.get("call_control_app") or {}
    print(f"  assistant.id:      {a.get('id')}")
    print(f"  assistant.name:    {a.get('name')}")
    print(f"  call_control_app.id: {app.get('id')}")
    print(f"  routing_added:     {d.get('routing_added')}")
    if d.get("errors"):
        print(f"  errors:            {d['errors']}")
    print()
