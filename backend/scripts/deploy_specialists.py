"""Deploy the 6 specialist agents + 1 dispatcher.

These are *latent* AI Assistants — they don't get their own numbers or
call control apps. Instead, the W3J LLC concierge's connect_to_specialist
tool POSTs to our webhook, which stops the current AI and starts the
specialist on the same call.

Also updates the existing 3 agents (W3J LLC, Bijou, Personal Twin) to
the new Azure voices and new system prompts (no more "have a great day"
auto-goodbye).

After deploy, saves the specialist name -> assistant_id map to
agents/specialists/assistants.json for the webhook to load.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent_builder.builder import AgentBuilder, AgentSpec, build_agent  # noqa: E402

WEBHOOK_URL = os.getenv("WEBHOOK_BASE_URL", "https://bk-jr-api.aixlabs.fun") + "/webhooks/telnyx"

# Top-level agents: full rebuild (number + app + assistant + routing)
TOP_LEVEL = [
    "w3j-llc-concierge",
    "bijou-ai-concierge",
    "w3j-personal-twin",
]

# Specialists: just create the AI Assistant, no number/app (latent)
SPECIALISTS = [
    "specialists/cs-agent",
    "specialists/sales-agent",
    "specialists/tech-support-agent",
    "specialists/dev-agent",
    "specialists/automation-agent",
    "specialists/consultants-agent",
    "specialists/dispatcher-agent",
]

# The mapping the W3J LLC concierge will use in connect_to_specialist(name)
SPECIALIST_KEYS = {
    "specialists/cs-agent":              "cs",
    "specialists/sales-agent":           "sales",
    "specialists/tech-support-agent":     "tech_support",
    "specialists/dev-agent":             "dev",
    "specialists/automation-agent":      "automation",
    "specialists/consultants-agent":     "consultants",
    "specialists/dispatcher-agent":      "nurun",  # "nurun" key maps to the dispatcher (acts as personal dispatcher)
}


def deploy_specialist(builder: AgentBuilder, agent_dir: str) -> dict:
    """Deploy one specialist (no number, no app — just the AI Assistant)."""
    spec = AgentSpec.from_yaml(f"agents/{agent_dir}/spec.yaml")
    # Strip out the number/app fields — specialists don't need them
    spec.buy_number = False
    spec.specific_number = None
    spec.call_control_app_name = None
    spec.webhook_url = WEBHOOK_URL
    # Build (will skip number/app since buy_number=False and specific_number=None)
    result = builder.build(spec)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy specialists + update top-level agents")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    args = parser.parse_args()

    if not args.dry_run and not args.yes:
        print("This will:")
        print("  - UPDATE the 3 top-level agents (W3J LLC, Bijou, Personal Twin) with new voices/prompts")
        print(f"  - CREATE {len(SPECIALISTS)} specialist AI Assistants (no numbers, no apps)")
        print(f"  - Register the specialist name -> assistant_id map for the webhook")
        print()
        try:
            input("Press Enter to continue, Ctrl-C to abort: ")
        except KeyboardInterrupt:
            print("\nAborted.")
            return 130

    builder = AgentBuilder()
    mapping: dict[str, str] = {}

    # 1. Top-level agents — full rebuild (idempotent — updates existing)
    if not args.dry_run:
        print("=== Top-level agents ===")
        for name in TOP_LEVEL:
            spec = AgentSpec.from_yaml(f"agents/{name}/spec.yaml")
            spec.webhook_url = WEBHOOK_URL
            result = builder.build(spec)
            if result.get("errors"):
                print(f"  [FAIL] {name}: {result['errors']}")
            else:
                ast = result.get("assistant") or {}
                num = result.get("phone_number") or "?"
                print(f"  [OK]   {name}  ->  ast={ast.get('id', '?')[:30]}...  num={num}")
        print()

    # 2. Specialists — just create the AI Assistant
    print("=== Specialists ===")
    for agent_dir in SPECIALISTS:
        if args.dry_run:
            spec = AgentSpec.from_yaml(f"agents/{agent_dir}/spec.yaml")
            print(f"  [DRY] {agent_dir}: would create AI Assistant '{spec.name}' with voice {spec.voice}")
            continue
        try:
            result = deploy_specialist(builder, agent_dir)
            if result.get("errors"):
                print(f"  [FAIL] {agent_dir}: {result['errors']}")
                continue
            ast = result.get("assistant") or {}
            ast_id = ast.get("id")
            key = SPECIALIST_KEYS.get(agent_dir, agent_dir)
            mapping[key] = ast_id
            print(f"  [OK]   {agent_dir}  ->  ast={ast_id}  key='{key}'")
        except Exception as e:
            print(f"  [FAIL] {agent_dir}: {e}")
    print()

    if args.dry_run:
        print("Dry-run only — nothing changed.")
        return 0

    # 3. Save the mapping
    out_path = _PROJECT_ROOT / "agents" / "specialists" / "assistants.json"
    out_path.write_text(json.dumps(mapping, indent=2))
    print(f"Mapping saved: {out_path}")
    print(json.dumps(mapping, indent=2))
    print()

    # 4. Wire the webhook handler with the mapping (in-process)
    try:
        from webhooks.handlers.dispatch import set_specialist_assistant_ids
        set_specialist_assistant_ids(mapping)
        print(f"Webhook handler updated: {len(mapping)} specialist mappings registered")
    except Exception as e:
        print(f"WARNING: could not update webhook handler in-process ({e}); restart webhook server to apply.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
