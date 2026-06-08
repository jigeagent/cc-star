"""Feedback signal model — user feedback on memory traces.

Ported from hermes-next v0.4.0 FeedbackSignal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Optional


FeedbackPolarity = Literal["positive", "negative", "neutral"]


@dataclass
class FeedbackSignal:
    """A single feedback event from a user or implicit signal.

    Used by cc-star's native memory to track which memories are
    valuable (positive) vs which should be deprioritized (negative).
    """

    trace_id: str
    polarity: FeedbackPolarity = "neutral"
    magnitude: float = 1.0
    """0.0-1.0. < 0.3 = weak signal, enough for anti-pattern tagging."""

    text: Optional[str] = None
    """Free-text correction. Used for anti-pattern marking."""

    source: str = "user"
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()
