"""Connector base class. Every concrete connector implements ``write_event``
and/or ``write_lead`` (and optionally ``read_leads``)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

log = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CallEvent:
    """Normalized call event we log to all configured sinks."""
    event_type: str
    call_control_id: Optional[str] = None
    agent_id: Optional[str] = None
    direction: Optional[str] = None  # "incoming" | "outgoing"
    from_number: Optional[str] = None
    to_number: Optional[str] = None
    duration_seconds: Optional[int] = None
    recording_url: Optional[str] = None
    transcript: Optional[str] = None
    notes: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=now_iso)

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        # Flatten extra for spreadsheet/DB rows
        extra = d.pop("extra") or {}
        for k, v in extra.items():
            d.setdefault(k, v)
        return d


class Connector(Protocol):
    """Protocol every sink implements. Concrete connectors may implement
    only the methods they need; missing methods are silently skipped at the
    call site (``if hasattr(connector, "write_event")``)."""

    name: str

    def write_event(self, event: CallEvent) -> bool: ...
    def write_lead(self, lead: dict[str, Any]) -> bool: ...
    def is_healthy(self) -> bool: ...
