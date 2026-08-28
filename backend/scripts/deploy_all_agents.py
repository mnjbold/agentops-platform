"""Deploy all agents in the agents/ directory.

Usage:
    python scripts/deploy_all_agents.py --dry-run            # show what would be created
    python scripts/deploy_all_agents.py --only w3j-llc-concierge  # deploy one
    python scripts/deploy_all_agents.py --webhook-url https://your-public.url/webhooks/telnyx
    python scripts/deploy_all_agents.py                      # full deploy

Each agent spec is loaded from agents/<name>/spec.yaml. After deployment,
the script:
  - saves the deployment report to agents/<name>/deployment.json
  - prints a one-line summary
  - exits non-zero if any deployment failed
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent_builder.builder import AgentBuilder, AgentSpec  # noqa: E402

AGENTS_DIR = _PROJECT_ROOT / "agents"
DEFAULT_WEBHOOK = os.getenv("WEBHOOK_BASE_URL", "https://bk-jr-api.aixlabs.fun") + "/webhooks/telnyx"


def load_specs(only: str | None = None) -> list[tuple[str, AgentSpec]]:
    out: list[tuple[str, AgentSpec]] = []
    for agent_dir in sorted(AGENTS_DIR.iterdir()):
        if not agent_dir.is_dir():
            continue
        if only and agent_dir.name != only:
            continue
        spec_path = agent_dir / "spec.yaml"
        if not spec_path.exists():
            print(f"  [skip] {agent_dir.name} — no spec.yaml")
            continue
        spec = AgentSpec.from_yaml(spec_path)
        out.append((agent_dir.name, spec))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy all W3J telephony agents")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be created")
    parser.add_argument("--only", type=str, default=None, help="Deploy only this agent directory name")
    parser.add_argument(
        "--webhook-url",
        type=str,
        default=DEFAULT_WEBHOOK,
        help=f"Public URL that Telnyx will POST call events to (default: {DEFAULT_WEBHOOK})",
    )
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    specs = load_specs(args.only)
    if not specs:
        print("No agent specs found.")
        return 1

    print(f"Found {len(specs)} agent spec(s) to {'preview' if args.dry_run else 'deploy'}:")
    for name, spec in specs:
        print(f"  - {name}: assistant='{spec.name}', country={spec.country_code}, area={spec.area_code or 'n/a'}")
    print()

    if not args.dry_run and not args.yes:
        print("This will:")
        print("  - Create Telnyx AI Assistants (idempotent on name)")
        print("  - Create Call Control Applications pointing at the webhook URL")
        print("  - Search and order NEW phone numbers (each costs $1 + $1/mo)")
        print("  - Update routing map")
        print()
        try:
            input("Press Enter to continue, Ctrl-C to abort: ")
        except KeyboardInterrupt:
            print("\nAborted.")
            return 130

    builder = AgentBuilder()
    failures: list[str] = []
    for name, spec in specs:
        spec.webhook_url = args.webhook_url
        print(f"=== {name} ===")
        if args.dry_run:
            print(json.dumps({"would_deploy": spec.to_dict()}, indent=2, default=str))
            continue
        try:
            t0 = time.time()
            result = builder.build(spec)
            elapsed = time.time() - t0
            # Save report
            report_path = AGENTS_DIR / name / "deployment.json"
            report_path.write_text(json.dumps(result, indent=2, default=str))
            if result.get("errors"):
                print(f"  ! {len(result['errors'])} error(s): {result['errors']}")
                failures.append(name)
            else:
                print(f"  [OK] Deployed in {elapsed:.1f}s")
                if result.get("phone_number"):
                    print(f"    number: {result['phone_number']}")
                if result.get("assistant", {}).get("id"):
                    print(f"    assistant: {result['assistant']['id']}")
                if result.get("call_control_app", {}).get("id"):
                    print(f"    call_control_app: {result['call_control_app']['id']}")
                if result.get("routing_added"):
                    print(f"    routing: {result['phone_number']} -> {result['assistant']['id']}")
        except Exception as e:
            print(f"  [FAIL] Deploy failed: {e}")
            failures.append(name)
        print()

    if failures:
        print(f"FAILED agents: {failures}")
        return 2
    if not args.dry_run:
        print("All agents deployed. Verify with: telnyx_account_summary")
    return 0


if __name__ == "__main__":
    sys.exit(main())
