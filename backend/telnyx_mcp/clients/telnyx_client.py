"""Thin, opinionated wrapper around the official ``telnyx`` Python SDK.

Goals
-----
* Singleton client so the MCP server and webhook handler share one connection
  pool and one credential.
* Helpers for the 20 operations we use most (search/buy numbers, dial, transfer,
  AI assistant lifecycle, send SMS, list recordings, etc.).
* Sensible pagination — the SDK v4 returns iterators that page transparently;
  we expose ``list_all(...)`` for "give me everything in one list".
* ``to_dict(...)`` that knows how to flatten Telnyx SDK objects (Pydantic
  models) into JSON-safe primitives, so MCP tools can return them directly.

Anything more exotic (TeXML apps, custom storage credentials, full TeXML verbs)
goes through ``client.telnyx.<resource>`` directly — we never wrap what we
don't need.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Iterable, Iterator, Optional

import telnyx
from pydantic import BaseModel

from telnyx_mcp.utils.env import TelnyxCreds, load_telnyx_creds

log = logging.getLogger(__name__)

# Module-level singleton (lazy)
_client_singleton: Optional["TelnyxClient"] = None


def to_dict(obj: Any) -> Any:
    """Recursively convert Telnyx SDK objects (Pydantic v2) to JSON-safe dicts.

    Handles:
    * Pydantic ``BaseModel`` → ``model_dump()`` (or ``.data`` wrapper)
    * SDK response wrappers that have a ``.data`` attribute containing the
      actual resource (Telnyx v4 wraps single-resource responses this way)
    * ``list`` / ``tuple`` → list
    * ``dict`` → dict
    * scalars → as-is
    """
    if obj is None:
        return None
    if isinstance(obj, BaseModel):
        # Unwrap a `.data` wrapper if present (Telnyx v4 SDK pattern).
        if hasattr(obj, "data") and obj.data is not None and isinstance(obj.data, BaseModel):
            return to_dict(obj.data)
        try:
            return obj.model_dump(mode="json", exclude_none=True)
        except Exception:
            return obj.model_dump(exclude_none=True)
    if isinstance(obj, (list, tuple)):
        return [to_dict(x) for x in obj]
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    if isinstance(obj, (str, int, float, bool)):
        return obj
    # Fallback: stringify
    return str(obj)


class TelnyxClient:
    """Friendly handle to the Telnyx SDK with the most common helpers inlined.

    The full SDK surface (~150 resources) is always reachable via
    ``self.api.<resource>`` for one-off or advanced calls.
    """

    def __init__(self, creds: Optional[TelnyxCreds] = None) -> None:
        self.creds = creds or load_telnyx_creds()
        # SDK v4 requires the key on the constructor (not just the module)
        self.api = telnyx.Telnyx(api_key=self.creds.api_key)
        if not self.creds.is_full_access:
            log.warning(
                "TelnyxClient running with limited credentials (%s). "
                "Most write operations will fail.",
                self.creds.key_type,
            )

    # ───────────────────────────── low-level passthroughs ───────────────────
    @property
    def raw(self) -> telnyx.Telnyx:
        """Return the underlying SDK client for advanced operations."""
        return self.api

    def list_all(self, paginator: Iterable[Any], max_items: int = 1000) -> list[dict]:
        """Materialize a paginated SDK response into a list of dicts.

        The Telnyx SDK v4 paginator iterates as ``(key, value)`` pairs, where
        ``key`` is usually ``"data"`` and ``value`` is the list of model objects.
        We flatten one level, return each model as a dict, and stop at
        ``max_items`` to keep MCP responses bounded.
        """
        out: list[dict] = []
        try:
            for item in paginator:
                # Unwrap (key, value) tuple shape from Telnyx SDK v4 paginator
                if isinstance(item, tuple) and len(item) == 2:
                    _key, value = item
                else:
                    value = item
                # If the value is itself a list, iterate it; else it's a single model
                if isinstance(value, (list, tuple)):
                    for sub in value:
                        out.append(to_dict(sub))
                        if len(out) >= max_items:
                            return out
                else:
                    out.append(to_dict(value))
                    if len(out) >= max_items:
                        return out
        except StopIteration:
            pass
        except Exception as e:
            log.warning("list_all: pagination truncated by error: %s", e)
        return out

    # ───────────────────────────── numbers ──────────────────────────────────
    def search_available_numbers(
        self,
        country_code: str = "US",
        area_code: Optional[str] = None,
        locality: Optional[str] = None,
        administrative_area: Optional[str] = None,
        features: Optional[list[str]] = None,
        limit: int = 10,
    ) -> list[dict]:
        """Search the Telnyx inventory for available numbers.

        ``features`` may include: ``"sms"``, ``"voice"``, ``"mms"``, ``"fax"``.
        """
        flt: dict[str, Any] = {"country_code": country_code, "limit": limit}
        if area_code:
            flt["national_destination_code"] = area_code
        if locality:
            flt["locality"] = locality
        if administrative_area:
            flt["administrative_area"] = administrative_area
        if features:
            flt["features"] = features
        avail = self.api.available_phone_numbers.list(filter=flt)
        return self.list_all(avail, max_items=limit)

    def order_numbers(self, phone_numbers: list[str]) -> dict:
        """Buy one or more numbers in a single order. Returns the order dict."""
        order = self.api.number_orders.create(
            phone_numbers=phone_numbers,
        )
        return to_dict(order)

    def list_owned_numbers(self, page_size: int = 50) -> list[dict]:
        nums = self.api.phone_numbers.list(page_size=page_size)
        return self.list_all(nums)

    def get_number(self, phone_number: str) -> dict:
        n = self.api.phone_numbers.retrieve(phone_number)
        return to_dict(n)

    def update_number(
        self,
        phone_number: str,
        *,
        connection_id: Optional[str] = None,
        billing_group_id: Optional[str] = None,
        messaging_profile_id: Optional[str] = None,
    ) -> dict:
        kwargs: dict[str, Any] = {}
        if connection_id:
            kwargs["connection_id"] = connection_id
        if billing_group_id:
            kwargs["billing_group_id"] = billing_group_id
        if messaging_profile_id:
            kwargs["messaging_profile_id"] = messaging_profile_id
        n = self.api.phone_numbers.update(phone_number, **kwargs)
        return to_dict(n)

    # ───────────────────────────── call control apps ───────────────────────
    def list_call_control_apps(self) -> list[dict]:
        apps = self.api.call_control_applications.list(page_size=100)
        return self.list_all(apps)

    def create_call_control_app(
        self,
        application_name: str,
        webhook_event_url: str,
        *,
        webhook_api_version: str = "2",
        webhook_timeout_secs: Optional[int] = None,
        first_command_timeout: bool = True,
        active: bool = True,
    ) -> dict:
        kwargs: dict[str, Any] = {
            "application_name": application_name,
            "webhook_event_url": webhook_event_url,
            "webhook_api_version": webhook_api_version,
            "first_command_timeout": first_command_timeout,
            "active": active,
        }
        if webhook_timeout_secs is not None:
            kwargs["webhook_timeout_secs"] = webhook_timeout_secs
        app = self.api.call_control_applications.create(**kwargs)
        return to_dict(app)

    def delete_call_control_app(self, app_id: str) -> dict:
        result = self.api.call_control_applications.delete(app_id)
        return to_dict(result)

    # ───────────────────────────── outbound voice profiles ──────────────────
    def list_outbound_profiles(self) -> list[dict]:
        profs = self.api.outbound_voice_profiles.list(page_size=100)
        return self.list_all(profs)

    def create_outbound_profile(
        self,
        name: str,
        *,
        concurrent_calls: int = 10,
        traffic_type: str = "conversational",
    ) -> dict:
        prof = self.api.outbound_voice_profiles.create(
            name=name,
            concurrent_calls=concurrent_calls,
            traffic_type=traffic_type,
        )
        return to_dict(prof)

    # ───────────────────────────── voice: dial / control / transfer ────────
    def dial(
        self,
        to: str,
        from_: str,
        connection_id: str,
        *,
        answering_machine_detection: Optional[str] = None,
        client_state: Optional[str] = None,
        command_timeout_secs: Optional[int] = None,
        timeout: int = 30,
    ) -> dict:
        kwargs: dict[str, Any] = {
            "to": to,
            "from_": from_,
            "connection_id": connection_id,
        }
        if answering_machine_detection:
            kwargs["answering_machine_detection"] = answering_machine_detection
        if client_state:
            kwargs["client_state"] = client_state
        if command_timeout_secs is not None:
            kwargs["command_timeout_secs"] = command_timeout_secs
        call = self.api.calls.dial(**kwargs)
        return to_dict(call)

    def transfer_call(
        self,
        call_control_id: str,
        to: str,
        *,
        from_: Optional[str] = None,
        answering_machine_detection: Optional[str] = None,
        custom_headers: Optional[list[dict]] = None,
        sip_headers: Optional[list[dict]] = None,
        time_limit_secs: Optional[int] = None,
        audio_url: Optional[str] = None,
    ) -> dict:
        kwargs: dict[str, Any] = {"to": to}
        if from_:
            kwargs["from_"] = from_
        if answering_machine_detection:
            kwargs["answering_machine_detection"] = answering_machine_detection
        if custom_headers:
            kwargs["custom_headers"] = custom_headers
        if sip_headers:
            kwargs["sip_headers"] = sip_headers
        if time_limit_secs is not None:
            kwargs["time_limit_secs"] = time_limit_secs
        if audio_url:
            kwargs["audio_url"] = audio_url
        result = self.api.calls.actions.transfer(call_control_id, **kwargs)
        return to_dict(result)

    def hangup_call(self, call_control_id: str) -> dict:
        return to_dict(self.api.calls.actions.hangup(call_control_id))

    def answer_call(self, call_control_id: str) -> dict:
        return to_dict(self.api.calls.actions.answer(call_control_id))

    def reject_call(self, call_control_id: str) -> dict:
        return to_dict(self.api.calls.actions.reject(call_control_id))

    def start_ai_assistant(
        self,
        call_control_id: str,
        assistant_id: str,
        *,
        client_state: Optional[str] = None,
        command_timeout_secs: Optional[int] = None,
    ) -> dict:
        # SDK v4 takes `assistant={"id": ...}`, not `assistant_id=...`
        kwargs: dict[str, Any] = {"assistant": {"id": assistant_id}}
        if client_state:
            kwargs["client_state"] = client_state
        if command_timeout_secs is not None:
            kwargs["command_timeout_secs"] = command_timeout_secs
        return to_dict(self.api.calls.actions.start_ai_assistant(call_control_id, **kwargs))

    def stop_ai_assistant(self, call_control_id: str) -> dict:
        return to_dict(self.api.calls.actions.stop_ai_assistant(call_control_id))

    # ───────────────────────────── AI assistants ───────────────────────────
    def list_assistants(self) -> list[dict]:
        a = self.api.ai.assistants.list()
        return self.list_all(a)

    def get_assistant(self, assistant_id: str) -> dict:
        a = self.api.ai.assistants.retrieve(assistant_id)
        return to_dict(a)

    def create_assistant(
        self,
        name: str,
        instructions: str,
        *,
        model: str = "openai/gpt-4o",
        voice: str = "Telnyx.KokoroTTS.af_heart",
        transcription_engine: str = "deepgram/nova-3",
        transcription_language: str = "en",
        greeting: Optional[str] = None,
        telephony_settings: Optional[dict] = None,
        tools: Optional[list[dict]] = None,
        dynamic_variables: Optional[dict] = None,
    ) -> dict:
        """Create a new Telnyx AI Assistant.

        Common voices (see ``voice_clones``/``voice_designs`` for custom):
        * ``Telnyx.KokoroTTS.af_heart`` (warm female, default)
        * ``Telnyx.KokoroTTS.am_adam`` (male)
        * ``Telnyx.KokoroTTS.bf_emma`` (British female)
        * ``AWS.Polly.Joanna``  /  ``AWS.Polly.Matthew``  /  ``Azure.en-US-JennyNeural``
        """
        kwargs: dict[str, Any] = {
            "name": name,
            "instructions": instructions,
            "model": model,
            "voice_settings": {"voice": voice, "language_boost": "auto"},
            "transcription": {"model": transcription_engine, "language": transcription_language},
        }
        if greeting:
            kwargs["greeting"] = greeting
        if telephony_settings:
            kwargs["telephony_settings"] = telephony_settings
        if tools:
            kwargs["tools"] = tools
        if dynamic_variables:
            kwargs["dynamic_variables"] = dynamic_variables
        a = self.api.ai.assistants.create(**kwargs)
        return to_dict(a)

    def update_assistant(
        self,
        assistant_id: str,
        *,
        name: Optional[str] = None,
        instructions: Optional[str] = None,
        model: Optional[str] = None,
        voice: Optional[str] = None,
        greeting: Optional[str] = None,
        tools: Optional[list[dict]] = None,
    ) -> dict:
        kwargs: dict[str, Any] = {}
        if name:
            kwargs["name"] = name
        if instructions:
            kwargs["instructions"] = instructions
        if model:
            kwargs["model"] = model
        if voice:
            kwargs["voice_settings"] = {"voice": voice, "language_boost": "auto"}
        if greeting:
            kwargs["greeting"] = greeting
        if tools:
            kwargs["tools"] = tools
        a = self.api.ai.assistants.update(assistant_id, **kwargs)
        return to_dict(a)

    def delete_assistant(self, assistant_id: str) -> dict:
        return to_dict(self.api.ai.assistants.delete(assistant_id))

    # ───────────────────────────── messaging ────────────────────────────────
    def list_messaging_profiles(self) -> list[dict]:
        m = self.api.messaging_profiles.list(page_size=100)
        return self.list_all(m)

    def create_messaging_profile(
        self, name: str, *, webhook_url: Optional[str] = None
    ) -> dict:
        kwargs: dict[str, Any] = {"name": name}
        if webhook_url:
            kwargs["webhook_url"] = webhook_url
        m = self.api.messaging_profiles.create(**kwargs)
        return to_dict(m)

    def send_sms(
        self,
        from_: str,
        to: str,
        text: str,
        *,
        messaging_profile_id: Optional[str] = None,
        webhook_url: Optional[str] = None,
    ) -> dict:
        kwargs: dict[str, Any] = {"from_": from_, "to": to, "text": text}
        if messaging_profile_id:
            kwargs["messaging_profile_id"] = messaging_profile_id
        if webhook_url:
            kwargs["webhook_url"] = webhook_url
        return to_dict(self.api.messages.create(**kwargs))

    # ───────────────────────────── recordings ──────────────────────────────
    def list_recordings(self, page_size: int = 50) -> list[dict]:
        recs = self.api.recordings.list(page_size=page_size)
        return self.list_all(recs)

    # ───────────────────────────── voice clones / designs ──────────────────
    def list_voice_clones(self) -> list[dict]:
        return self.list_all(self.api.voice_clones.list())

    def list_voice_designs(self) -> list[dict]:
        return self.list_all(self.api.voice_designs.list())


def get_client() -> TelnyxClient:
    """Process-wide singleton (lazy-initialized)."""
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = TelnyxClient()
    return _client_singleton


if __name__ == "__main__":
    # CLI smoke: list numbers, assistants, balance
    c = get_client()
    bal = c.api.balance.retrieve()
    print("Balance:", to_dict(bal))
    print()
    nums = c.list_owned_numbers()
    print(f"Owned numbers: {len(nums)}")
    for n in nums:
        print(f"  - {n.get('phone_number')} | conn_id={n.get('connection_id')} | status={n.get('status')}")
    print()
    print(f"Assistants: {len(c.list_assistants())}")
    print(f"Call control apps: {len(c.list_call_control_apps())}")
    print(f"Outbound profiles: {len(c.list_outbound_profiles())}")
    print(f"Messaging profiles: {len(c.list_messaging_profiles())}")
    print(f"Voice clones: {len(c.list_voice_clones())}")
