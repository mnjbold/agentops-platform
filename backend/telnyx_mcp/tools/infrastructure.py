"""Infrastructure surface — call control apps, outbound voice profiles,
messaging profiles, voice clones / designs (custom voices)."""
from telnyx_mcp.server import mcp
from telnyx_mcp.clients.telnyx_client import get_client


# ───────────────────────────── call control apps ─────────────────────────
@mcp.tool()
def telnyx_list_call_control_apps() -> list[dict]:
    """List all Call Control Applications (the routing layer for inbound calls).

    Each app has a webhook_event_url — Telnyx POSTs call events there.
    """
    return get_client().list_call_control_apps()


@mcp.tool()
def telnyx_create_call_control_app(
    application_name: str,
    webhook_event_url: str,
    webhook_api_version: str = "2",
    webhook_timeout_secs: int | None = None,
    first_command_timeout: bool = True,
    active: bool = True,
) -> dict:
    """Create a Call Control Application.

    Args:
        application_name: Friendly name (e.g. "W3J Receptionist Webhooks").
        webhook_event_url: Public URL that receives call events.
        webhook_api_version: "1" (legacy) or "2" (current).
        webhook_timeout_secs: How long to wait for webhook response.
        first_command_timeout: Enforce timeout on the first command.
        active: If false, calls to numbers assigned to this app fail.
    """
    return get_client().create_call_control_app(
        application_name=application_name,
        webhook_event_url=webhook_event_url,
        webhook_api_version=webhook_api_version,
        webhook_timeout_secs=webhook_timeout_secs,
        first_command_timeout=first_command_timeout,
        active=active,
    )


@mcp.tool()
def telnyx_delete_call_control_app(app_id: str) -> dict:
    """Delete a Call Control Application (irreversible; numbers on it will lose routing)."""
    return get_client().delete_call_control_app(app_id)


# ───────────────────────────── outbound voice profiles ───────────────────
@mcp.tool()
def telnyx_list_outbound_voice_profiles() -> list[dict]:
    """List Outbound Voice Profiles (the routing layer for outbound calls)."""
    return get_client().list_outbound_profiles()


@mcp.tool()
def telnyx_create_outbound_voice_profile(
    name: str,
    concurrent_calls: int = 10,
    traffic_type: str = "conversational",
) -> dict:
    """Create an Outbound Voice Profile.

    Args:
        name: Friendly name.
        concurrent_calls: Maximum simultaneous outbound calls (1-50).
        traffic_type: "conversational" | "short_code" | "app_2_person" | ...
    """
    return get_client().create_outbound_profile(
        name=name,
        concurrent_calls=concurrent_calls,
        traffic_type=traffic_type,
    )


# ───────────────────────────── messaging profiles ────────────────────────
@mcp.tool()
def telnyx_list_messaging_profiles() -> list[dict]:
    """List Messaging Profiles (the routing layer for inbound/outbound SMS/MMS/WhatsApp)."""
    return get_client().list_messaging_profiles()


@mcp.tool()
def telnyx_create_messaging_profile(name: str, webhook_url: str | None = None) -> dict:
    """Create a Messaging Profile."""
    return get_client().create_messaging_profile(name=name, webhook_url=webhook_url)


# ───────────────────────────── voice clones / designs ───────────────────
@mcp.tool()
def telnyx_list_voice_clones() -> list[dict]:
    """List custom voice clones (uploaded audio used as a TTS voice)."""
    return get_client().list_voice_clones()


@mcp.tool()
def telnyx_list_voice_designs() -> list[dict]:
    """List AI-generated voice designs (text-described voices)."""
    return get_client().list_voice_designs()
