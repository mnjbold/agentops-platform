"""AI Assistant surface — CRUD on Telnyx AI voice assistants."""
from telnyx_mcp.server import mcp
from telnyx_mcp.clients.telnyx_client import get_client


@mcp.tool()
def telnyx_list_assistants() -> list[dict]:
    """List all Telnyx AI Assistants in this account."""
    return get_client().list_assistants()


@mcp.tool()
def telnyx_get_assistant(assistant_id: str) -> dict:
    """Get the full configuration of one AI Assistant (instructions, model, voice, tools)."""
    return get_client().get_assistant(assistant_id)


@mcp.tool()
def telnyx_create_assistant(
    name: str,
    instructions: str,
    model: str = "openai/gpt-4o",
    voice: str = "Telnyx.KokoroTTS.af_heart",
    transcription_engine: str = "deepgram/nova-3",
    tools: list[dict] | None = None,
) -> dict:
    """Create a new Telnyx AI Assistant.

    Args:
        name: Human-friendly name (e.g. "W3J Concierge", "Bijou Lead Screener").
        instructions: The system prompt — should include the agent's role, voice
            persona, what to do for each common caller intent, and explicit
            knowledge of when to transfer / take a message / hang up.
        model: LLM identifier. Recommended: "openai/gpt-4o", "anthropic/claude-3-5-sonnet",
            "moonshotai/Kimi-K2.5" (used by the user's existing assistants).
        voice: TTS voice. Built-in options include "Telnyx.KokoroTTS.af_heart",
            "Telnyx.KokoroTTS.am_adam", "AWS.Polly.Joanna", "Azure.en-US-JennyNeural".
            For voice clones, use a custom voice_id from telnyx_list_voice_clones.
        transcription_engine: STT engine, default "deepgram/nova-3".
        tools: List of tool definitions for the assistant (transfer, hangup, custom
            webhooks, etc.). For most receptionist use-cases the default built-in
            tools are enough.

    Returns:
        The created assistant dict with assistant_id.
    """
    return get_client().create_assistant(
        name=name,
        instructions=instructions,
        model=model,
        voice=voice,
        transcription_engine=transcription_engine,
        tools=tools,
    )


@mcp.tool()
def telnyx_update_assistant(
    assistant_id: str,
    name: str | None = None,
    instructions: str | None = None,
    model: str | None = None,
    voice: str | None = None,
    tools: list[dict] | None = None,
) -> dict:
    """Update an existing Telnyx AI Assistant (any subset of fields)."""
    return get_client().update_assistant(
        assistant_id=assistant_id,
        name=name,
        instructions=instructions,
        model=model,
        voice=voice,
        tools=tools,
    )


@mcp.tool()
def telnyx_delete_assistant(assistant_id: str) -> dict:
    """Delete an AI Assistant. Active calls using it will keep working but
    new calls assigned to it will fail."""
    return get_client().delete_assistant(assistant_id)
