"""Synthetic call simulator (issue #24).

This module walks the campaign's workflow synthetically — no Telnyx
calls, no PII exposure, no real spend. The downstream dialer asks
:func:`run_synthetic_batch` for ``n`` simulated calls and we return a
list of :class:`SyntheticCall` rows that the API layer persists to
``synthetic_calls`` + summarises for the UI chart.

Outcomes
--------
Each call gets one outcome from a configurable distribution:

* ``all_answer``        — 100% ``answer``           (happy-path test)
* ``all_voicemail``     — 100% ``voicemail``        (AMD / VM-drop test)
* ``all_no_answer``     — 100% ``no_answer``
* ``all_busy``          — 100% ``busy``
* ``all_failed``        — 100% ``failed``
* ``mixed``             — weighted:
  ``answer=0.6, voicemail=0.2, no_answer=0.1, busy=0.05, failed=0.05``
* ``custom_weights``    — a dict like
  ``{"answer": 0.7, "voicemail": 0.3}``; missing outcomes default to 0.

For each outcome we also generate a tiny transcript snippet so the UI
can show "this is what a voicemail drop sounds like" without running
the real voice stack. Tool calls are also generated for ``answer``
outcomes so the campaign-mode handoff log gets a realistic
demonstration.

Performance
-----------
The simulator is pure Python — no I/O — so 100 calls finish in well
under 5 seconds (typically <50ms). The ``run_synthetic_batch`` function
takes a ``store`` argument so the test suite can pass a fresh
in-memory Store; production code uses the singleton via
:func:`webhooks.storage.get_store`.
"""
from __future__ import annotations

import logging
import random
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional, Union

log = logging.getLogger(__name__)


# Valid outcome vocabulary. Mirrors what ``campaign_stats_bump`` and
# the call log accept. Keep in sync with ``webhooks.workflow_engine``.
VALID_OUTCOMES: tuple[str, ...] = (
    "answer", "voicemail", "no_answer", "busy", "failed",
)


# Default distribution for ``"mixed"``. Sums to 1.0.
MIXED_DEFAULT_WEIGHTS: dict[str, float] = {
    "answer":    0.60,
    "voicemail": 0.20,
    "no_answer": 0.10,
    "busy":      0.05,
    "failed":    0.05,
}


# Outcome → canned transcript snippet. The campaign's actual script
# (e.g. the AI assistant's greeting) is *not* substituted here — this
# is a stub so the synthetic log has a realistic body. Real integration
# would template these against ``campaign.message`` / ``workflow.entry``.
_TRANSCRIPTS: dict[str, str] = {
    "answer":    "[agent] Hi, this is the agentops test caller. "
                  "[user] Hello, who's calling? "
                  "[agent] Running a synthetic test of your AI workflow. "
                  "Have a great day. "
                  "[hangup]",
    "voicemail": "[beep] Hi, you've reached the voicemail of the test "
                  "recipient. Please leave a message. "
                  "[agent] [voicemail drop played]",
    "no_answer": "[ring... ring... ring...] no-answer after 25s",
    "busy":      "[fast-busy] line busy",
    "failed":    "[error] SIP 503 — carrier unavailable",
}

# Outcome → canned tool calls (only ``answer`` and ``voicemail``
# produce them). Mirrors the workflow engine's tool-call log shape.
_TOOL_CALLS: dict[str, list[dict]] = {
    "answer": [
        {"name": "greeting",         "args": {}, "result": "ok"},
        {"name": "transfer_to_human", "args": {"queue": "sales"}, "result": "queued"},
    ],
    "voicemail": [
        {"name": "play_audio", "args": {"url": "campaign-voicemail.mp3"},
         "result": "delivered"},
    ],
    "no_answer": [
        {"name": "hangup", "args": {"reason": "no-answer"}, "result": "ok"},
    ],
    "busy": [],
    "failed": [],
}


@dataclass
class SyntheticCall:
    """One row in the synthetic_calls table.

    Fields are flat strings/ints so they serialise cleanly into the
    SQLite insert. ``id`` is a synthetic row id (used by the API for
    subsequent reads); the simulator assigns fresh ones so the same
    campaign can be tested repeatedly without collision.
    """
    id: str
    campaign_id: str
    contact_id: Optional[str]
    outcome: str
    started_at: str
    ended_at: str
    transcript: str
    tool_calls: list[dict] = field(default_factory=list)

    def to_db_tuple(self) -> tuple:
        import json
        return (
            self.id, self.campaign_id, self.contact_id, self.outcome,
            self.started_at, self.ended_at, self.transcript,
            json.dumps(self.tool_calls),
        )


# ────────────────────────── distribution helpers ────────────────────────────


def _normalise_weights(weights: dict[str, float]) -> dict[str, float]:
    """Validate the weights dict and return a clean copy.

    - Drops unknown outcome keys (with a debug log).
    - Coerces negatives to 0.
    - Renormalises to sum to 1.0 (raises ``ValueError`` if the total
      is zero — caller passed an empty dict).
    """
    out: dict[str, float] = {}
    for k, v in weights.items():
        if k not in VALID_OUTCOMES:
            log.debug("dropping unknown outcome %r from weights", k)
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            fv = 0.0
        out[k] = max(0.0, fv)
    total = sum(out.values())
    if total <= 0:
        raise ValueError("weights must have a positive total")
    return {k: v / total for k, v in out.items()}


def _weights_for(
    distribution: Union[str, dict],
    custom_weights: Optional[dict[str, float]] = None,
) -> dict[str, float]:
    """Resolve the ``distribution`` arg into a normalised weights dict.

    Order of precedence
    -------------------
    1. ``custom_weights`` (a dict) — always wins.
    2. ``distribution`` if it's already a dict.
    3. The named presets ('all_answer', 'mixed', ...).
    """
    if custom_weights:
        return _normalise_weights(custom_weights)
    if isinstance(distribution, dict):
        return _normalise_weights(distribution)
    if not distribution:
        return _normalise_weights(MIXED_DEFAULT_WEIGHTS)
    key = str(distribution).strip().lower()
    if key in ("all_answer", "answer", "all-answer"):
        return {"answer": 1.0}
    if key in ("all_voicemail", "voicemail", "all-voicemail"):
        return {"voicemail": 1.0}
    if key in ("all_no_answer", "no_answer", "all-no-answer", "no-answer"):
        return {"no_answer": 1.0}
    if key in ("all_busy", "busy", "all-busy"):
        return {"busy": 1.0}
    if key in ("all_failed", "failed", "all-failed"):
        return {"failed": 1.0}
    if key in ("mixed", "default", "weighted"):
        return _normalise_weights(MIXED_DEFAULT_WEIGHTS)
    raise ValueError(f"unknown distribution: {distribution!r}")


def _sample_outcome(weights: dict[str, float], rng: random.Random) -> str:
    """Weighted random pick. Falls back to ``"answer"`` if the dict is empty."""
    if not weights:
        return "answer"
    outcomes = list(weights.keys())
    probs = list(weights.values())
    return rng.choices(outcomes, weights=probs, k=1)[0]


# ────────────────────────── transcript / tool-call gen ──────────────────────


def _gen_transcript(outcome: str, *, contact_id: Optional[str] = None) -> str:
    base = _TRANSCRIPTS.get(outcome, "")
    if not contact_id:
        return base
    return f"[contact={contact_id}] {base}"


def _gen_tool_calls(outcome: str) -> list[dict]:
    return list(_TOOL_CALLS.get(outcome, []))


# ────────────────────────── main public surface ─────────────────────────────


def run_synthetic_batch(
    campaign_id: str,
    n: int,
    distribution: Union[str, dict] = "mixed",
    *,
    contact_ids: Optional[Iterable[str]] = None,
    custom_weights: Optional[dict[str, float]] = None,
    seed: Optional[int] = None,
) -> list[SyntheticCall]:
    """Generate ``n`` synthetic calls without hitting Telnyx.

    Parameters
    ----------
    campaign_id : str
        The campaign these calls belong to. Stored verbatim on each row.
    n : int
        Number of synthetic calls to generate. Capped at 10000 (use a
        larger batch for real load tests; the test suite needs <5s for
        100, so 10k is a generous upper bound).
    distribution : str | dict
        See :func:`_weights_for`. Defaults to ``"mixed"``.
    contact_ids : iterable, optional
        If provided, the ``i``-th call is associated with the ``i``-th
        contact. If shorter than ``n`` we cycle; if ``None`` we just
        leave ``contact_id`` empty.
    custom_weights : dict, optional
        See :func:`_weights_for`. Wins over ``distribution``.
    seed : int, optional
        Seed the RNG for deterministic tests. Production code passes
        ``None`` to use ``random.SystemRandom`` (cryptographic).

    Returns
    -------
    list[SyntheticCall]
        One row per simulated call, ready to insert into
        ``synthetic_calls``. The caller (the API layer) is responsible
        for the database write — this module is pure.
    """
    if n <= 0:
        raise ValueError(f"n must be positive, got {n}")
    if n > 10000:
        raise ValueError(f"n too large: {n} (max 10000)")
    weights = _weights_for(distribution, custom_weights)
    rng = random.Random(seed) if seed is not None else random.SystemRandom()
    contact_list = list(contact_ids) if contact_ids else []
    # If the contact list is shorter than n, cycle through it. The
    # caller asked for N calls; we do N even if it means reusing
    # contacts (the test logs are populated by call, not by contact).
    if contact_list:
        n_contacts = len(contact_list)
    else:
        n_contacts = 0
    # Build a tiny time-slot so each call has a different started_at.
    now = datetime.now(timezone.utc)
    out: list[SyntheticCall] = []
    for i in range(int(n)):
        outcome = _sample_outcome(weights, rng)
        # Spread the start times 1ms apart so ORDER BY started_at is stable.
        started = now.replace(microsecond=(i * 1000) % 1_000_000)
        # End is a tiny "call duration" depending on outcome.
        if outcome == "answer":
            ended = started.replace(microsecond=((started.microsecond + 18_000_000) % 1_000_000))
        elif outcome == "voicemail":
            ended = started.replace(microsecond=((started.microsecond + 5_000_000) % 1_000_000))
        elif outcome == "no_answer":
            ended = started.replace(microsecond=((started.microsecond + 25_000_000) % 1_000_000))
        else:
            ended = started
        cid = contact_list[i % n_contacts] if n_contacts else None
        row_id = f"syn_{int(started.timestamp() * 1000)}_{i:04d}_{secrets.token_hex(2)}"
        out.append(SyntheticCall(
            id=row_id,
            campaign_id=campaign_id,
            contact_id=cid,
            outcome=outcome,
            started_at=started.isoformat(),
            ended_at=ended.isoformat(),
            transcript=_gen_transcript(outcome, contact_id=cid),
            tool_calls=_gen_tool_calls(outcome),
        ))
    return out


def aggregate_distribution(calls: Iterable[SyntheticCall]) -> dict[str, int]:
    """Return ``{outcome: count}`` for a list of synthetic calls.

    Used by the API layer to build the chart payload without touching
    the database.
    """
    counts: dict[str, int] = {o: 0 for o in VALID_OUTCOMES}
    for c in calls:
        counts[c.outcome] = counts.get(c.outcome, 0) + 1
    # Drop zero rows so the chart is smaller.
    return {k: v for k, v in counts.items() if v > 0}
