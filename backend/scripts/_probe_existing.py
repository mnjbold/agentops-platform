"""Inspect existing Telnyx resources to see what's already set up."""
import os
from telnyx_mcp.clients.telnyx_client import get_client, to_dict

c = get_client()

print("=== Existing AI Assistants ===")
assts = c.list_assistants()
print(f"  count: {len(assts)}")
for a in assts:
    print(f"  id={a.get('id')}")
    print(f"  name={a.get('name')}")
    print(f"  model={a.get('model')}")
    print(f"  voice={a.get('voice')}")
    print(f"  instructions (first 200 chars): {(a.get('instructions') or '')[:200]}")
    print(f"  created: {a.get('created_at')}")
    print(f"  updated: {a.get('updated_at')}")
    print()

print("=== Existing Call Control Apps ===")
for app in c.list_call_control_apps():
    print(f"  id={app.get('id')}")
    print(f"  name={app.get('application_name')}")
    print(f"  webhook_event_url={app.get('webhook_event_url')}")
    print(f"  webhook_api_version={app.get('webhook_api_version')}")
    print(f"  webhook_timeout_secs={app.get('webhook_timeout_secs')}")
    print(f"  first_command_timeout={app.get('first_command_timeout')}")
    print(f"  active={app.get('active')}")
    print(f"  conn_id={app.get('call_control_connection_id')}")
    print()

print("=== Existing Outbound Voice Profiles ===")
for p in c.list_outbound_profiles():
    print(f"  id={p.get('id')}")
    print(f"  name={p.get('name')}")
    print(f"  concurrent_calls={p.get('concurrent_calls')}")
    print(f"  traffic_type={p.get('traffic_type')}")
    print(f"  usage_payment_method={p.get('usage_payment_method')}")
    print()

print("=== Existing Numbers (full detail) ===")
for n in c.list_owned_numbers():
    print(f"  {n.get('phone_number')}")
    print(f"    status: {n.get('status')}")
    print(f"    conn_id: {n.get('connection_id')}")
    print(f"    messaging_profile_id: {n.get('messaging_profile_id')}")
    print(f"    billing_group_id: {n.get('billing_group_id')}")
    print(f"    features: {n.get('features')}")
    print(f"    purchased_at: {n.get('purchased_at')}")
    print()
