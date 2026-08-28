"""Utility surface — balance, account health, generic passthroughs."""
from telnyx_mcp.server import mcp
from telnyx_mcp.clients.telnyx_client import get_client, to_dict


@mcp.tool()
def telnyx_get_balance() -> dict:
    """Get current account balance (USD). Returns balance, available_credit, currency."""
    return to_dict(get_client().api.balance.retrieve())


@mcp.tool()
def telnyx_account_summary() -> dict:
    """One-shot summary of the whole account: balance, owned numbers, AI assistants,
    call control apps, outbound profiles, messaging profiles, voice clones.

    Useful for verifying state after a deploy or for status dashboards.
    """
    c = get_client()
    return {
        "balance": to_dict(c.api.balance.retrieve()).get("data", {}),
        "owned_numbers_count": len(c.list_owned_numbers()),
        "owned_numbers": c.list_owned_numbers(),
        "assistants_count": len(c.list_assistants()),
        "call_control_apps_count": len(c.list_call_control_apps()),
        "outbound_voice_profiles_count": len(c.list_outbound_profiles()),
        "messaging_profiles_count": len(c.list_messaging_profiles()),
        "voice_clones_count": len(c.list_voice_clones()),
    }


@mcp.tool()
def telnyx_health_check() -> dict:
    """Verify the MCP server can reach Telnyx and credentials are valid.

    Returns {"ok": true, "balance": ..., "key_type": ..., "source": ...}
    on success, or {"ok": false, "error": "..."} on failure.
    """
    c = get_client()
    try:
        bal = to_dict(c.api.balance.retrieve())
        return {
            "ok": True,
            "balance_usd": bal.get("data", {}).get("balance"),
            "available_credit": bal.get("data", {}).get("available_credit"),
            "currency": bal.get("data", {}).get("currency"),
            "key_source": c.creds.source,
            "key_type": c.creds.key_type,
            "full_access": c.creds.is_full_access,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
