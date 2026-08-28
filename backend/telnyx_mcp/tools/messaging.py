"""Messaging surface — SMS / MMS / WhatsApp sends."""
from telnyx_mcp.server import mcp
from telnyx_mcp.clients.telnyx_client import get_client


@mcp.tool()
def telnyx_send_sms(
    from_: str,
    to: str,
    text: str,
    messaging_profile_id: str | None = None,
    webhook_url: str | None = None,
) -> dict:
    """Send an SMS message from one of your Telnyx numbers.

    Args:
        from_: Sender phone number (E.164, must be one of your numbers).
        to: Recipient phone number (E.164).
        text: Message body (up to 1600 chars; longer is split into segments).
        messaging_profile_id: Optional profile to use (default: any active profile).
        webhook_url: URL to receive delivery status callbacks.

    Returns:
        The message resource with id, status, and segment count.
    """
    return get_client().send_sms(
        from_=from_,
        to=to,
        text=text,
        messaging_profile_id=messaging_profile_id,
        webhook_url=webhook_url,
    )
