"""Network quality scoring (issue #28).

A small pure module so the test suite can exercise the formula
without spinning up the FastAPI app. The shape mirrors the
``network_quality_log`` row the browser's WebRTC ``getStats()`` poller
writes into the DB.

Score formula
-------------
The score is a 0..100 integer (higher = better). We blend three
penalty components:

* RTT  : ``min(40, rtt_ms / 5)``           (40 penalty points max)
* Jitter: ``min(40, jitter_ms * 2)``         (40 penalty points max)
* Loss : ``min(40, packet_loss_pct * 4)``  (40 penalty points max)

Total penalty is clamped to 100; score is ``max(0, 100 - penalty)``.
The three maxima are equal-weight by design — RTT, jitter, and loss
each contribute up to ~33 % of the score.

The thresholds are deliberately conservative for a v1: most healthy
calls sit at 85+ on this scale; anything below 50 is a "you should
look at this" alert. We can re-tune with real traffic data.
"""
from __future__ import annotations

from typing import Optional


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def compute_score(
    rtt_ms: Optional[float],
    jitter_ms: Optional[float],
    packet_loss_pct: Optional[float],
) -> int:
    """Return a 0..100 quality score for the given sample.

    Any of the inputs may be ``None`` (the WebRTC `getStats` report
    occasionally omits one of the fields). Missing values contribute
    0 penalty so the score doesn't over-penalise a partial sample.
    """
    penalty = 0.0
    if rtt_ms is not None:
        penalty += _clamp(float(rtt_ms) / 5.0, 0.0, 40.0)
    if jitter_ms is not None:
        penalty += _clamp(float(jitter_ms) * 2.0, 0.0, 40.0)
    if packet_loss_pct is not None:
        # Loss is already in 0..100 percent. 10 % loss is "really bad".
        penalty += _clamp(float(packet_loss_pct) * 4.0, 0.0, 40.0)
    score = 100 - _clamp(penalty, 0.0, 100.0)
    return int(round(score))


def score_label(score: Optional[int]) -> str:
    """Human-readable bucket for the dashboard."""
    if score is None:
        return "unknown"
    if score >= 85:
        return "excellent"
    if score >= 70:
        return "good"
    if score >= 50:
        return "fair"
    if score >= 25:
        return "poor"
    return "bad"
