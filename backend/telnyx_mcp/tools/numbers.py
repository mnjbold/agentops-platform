"""Numbers surface — search, buy, configure phone numbers."""
from telnyx_mcp.server import mcp
from telnyx_mcp.clients.telnyx_client import get_client


@mcp.tool()
def telnyx_search_available_numbers(
    country_code: str = "US",
    area_code: str | None = None,
    locality: str | None = None,
    administrative_area: str | None = None,
    features: list[str] | None = None,
    limit: int = 10,
) -> list[dict]:
    """Search the Telnyx inventory for available phone numbers to purchase.

    Args:
        country_code: ISO country code, e.g. "US", "CA", "GB", "MY".
        area_code: National destination code / area code filter, e.g. "213", "510", "415".
        locality: City filter, e.g. "Los Angeles", "San Francisco".
        administrative_area: State/province filter, e.g. "CA", "NY".
        features: List of features required, e.g. ["sms", "voice", "mms", "fax"].
        limit: Maximum numbers to return (1-50).

    Returns:
        List of available number dicts with phone_number, cost, features, region.
    """
    return get_client().search_available_numbers(
        country_code=country_code,
        area_code=area_code,
        locality=locality,
        administrative_area=administrative_area,
        features=features,
        limit=min(limit, 50),
    )


@mcp.tool()
def telnyx_order_numbers(phone_numbers: list[str]) -> dict:
    """Buy one or more phone numbers in a single order.

    Each phone number must be in E.164 format (e.g. "+12135551234") and must
    have been returned by a recent search. The cost is $1/number one-time +
    $1-3/month per number depending on country/type.

    Returns:
        The order dict with order id, status, and ordered numbers.
    """
    return get_client().order_numbers(phone_numbers)


@mcp.tool()
def telnyx_list_owned_numbers() -> list[dict]:
    """List all phone numbers owned by this Telnyx account."""
    return get_client().list_owned_numbers()


@mcp.tool()
def telnyx_get_number(phone_number: str) -> dict:
    """Get full details for a single owned phone number.

    Args:
        phone_number: E.164 format, e.g. "+12135551234".
    """
    return get_client().get_number(phone_number)


@mcp.tool()
def telnyx_update_number(
    phone_number: str,
    connection_id: str | None = None,
    billing_group_id: str | None = None,
    messaging_profile_id: str | None = None,
) -> dict:
    """Update routing/billing for an owned phone number.

    To send the number to a voice agent, set connection_id to the call control
    app's connection_id. For SMS, set messaging_profile_id.
    """
    return get_client().update_number(
        phone_number,
        connection_id=connection_id,
        billing_group_id=billing_group_id,
        messaging_profile_id=messaging_profile_id,
    )
