"""Voice surface — dial, transfer, answer, hangup, recording, AI assistant control."""
from telnyx_mcp.server import mcp
from telnyx_mcp.clients.telnyx_client import get_client


@mcp.tool()
def telnyx_dial(
    to: str,
    from_: str,
    connection_id: str,
    answering_machine_detection: str | None = None,
    client_state: str | None = None,
    command_timeout_secs: int | None = None,
) -> dict:
    """Place an outbound call from ``from_`` (one of your Telnyx numbers) to ``to``.

    The call is controlled by a sequence of webhooks to your ``connection_id``'s
    webhook URL. Returns the initial call resource (call_control_id, etc.).

    Args:
        to: Destination number (E.164).
        from_: Caller ID (E.164, must be one of your Telnyx numbers).
        connection_id: Call Control Application connection ID.
        answering_machine_detection: "basic" | "premium" — detect answering machines.
        client_state: Opaque state to pass through webhooks.
        command_timeout_secs: Default 30s; max 120s.
    """
    return get_client().dial(
        to=to,
        from_=from_,
        connection_id=connection_id,
        answering_machine_detection=answering_machine_detection,
        client_state=client_state,
        command_timeout_secs=command_timeout_secs,
    )


@mcp.tool()
def telnyx_transfer_call(
    call_control_id: str,
    to: str,
    from_: str | None = None,
    audio_url: str | None = None,
    time_limit_secs: int | None = None,
) -> dict:
    """Transfer an in-progress call to a new destination.

    This is how a receptionist AI forwards a call to a real human. The
    destination hears the original call audio + any ``audio_url`` played first.

    Args:
        call_control_id: The active call to transfer.
        to: Destination number (E.164) or SIP URI.
        from_: New caller ID shown to ``to`` (defaults to original from).
        audio_url: URL of audio to play to ``to`` before connecting (e.g. intro).
        time_limit_secs: Maximum total call duration after transfer.

    Returns:
        The transfer command response (eventually emits call.bridged webhook).
    """
    return get_client().transfer_call(
        call_control_id=call_control_id,
        to=to,
        from_=from_,
        audio_url=audio_url,
        time_limit_secs=time_limit_secs,
    )


@mcp.tool()
def telnyx_hangup_call(call_control_id: str) -> dict:
    """End an in-progress call."""
    return get_client().hangup_call(call_control_id)


@mcp.tool()
def telnyx_answer_call(call_control_id: str) -> dict:
    """Answer an incoming call (required before further commands on a call)."""
    return get_client().answer_call(call_control_id)


@mcp.tool()
def telnyx_reject_call(call_control_id: str) -> dict:
    """Reject an incoming call (sends busy signal)."""
    return get_client().reject_call(call_control_id)


@mcp.tool()
def telnyx_start_ai_assistant(
    call_control_id: str,
    assistant_id: str,
    client_state: str | None = None,
) -> dict:
    """Start a Telnyx AI Assistant on an in-progress call.

    The AI Assistant takes over the audio: it speaks via TTS, listens via STT
    (Deepgram by default), follows its system prompt, and can invoke its
    configured tools (transfer, hangup, gather, etc.).
    """
    return get_client().start_ai_assistant(
        call_control_id=call_control_id,
        assistant_id=assistant_id,
        client_state=client_state,
    )


@mcp.tool()
def telnyx_stop_ai_assistant(call_control_id: str) -> dict:
    """Stop the active AI Assistant on a call (returns audio to the call)."""
    return get_client().stop_ai_assistant(call_control_id)


@mcp.tool()
def telnyx_list_recordings(limit: int = 50) -> list[dict]:
    """List call recordings, most recent first."""
    return get_client().list_recordings(page_size=min(limit, 100))
