"""Reward backpropagation engine for MemOS cognitive pipeline.

Maps outcome signals back through trace sequences to compute
per-trace reward values with temporal discounting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

import numpy as np

from cc_star.memos.types import TraceRow

logger = logging.getLogger(__name__)


class OutcomeSignal(Enum):
    """Types of outcome signals that can trigger reward computation."""

    TASK_SUCCESS = "task_success"
    TASK_FAILURE = "task_failure"
    USER_THUMBS_UP = "user_thumbs_up"
    USER_THUMBS_DOWN = "user_thumbs_down"
    USER_CORRECTION = "user_correction"
    DELEGATION_SUCCESS = "delegation_success"
    DELEGATION_FAILURE = "delegation_failure"
    TOOL_SUCCESS = "tool_success"
    TOOL_ERROR = "tool_error"
    MANUAL_REWARD = "manual_reward"


# Base reward values for each signal type
_SIGNAL_REWARDS: dict[OutcomeSignal, float] = {
    OutcomeSignal.TASK_SUCCESS: 1.0,
    OutcomeSignal.TASK_FAILURE: -0.8,
    OutcomeSignal.USER_THUMBS_UP: 1.0,
    OutcomeSignal.USER_THUMBS_DOWN: -1.0,
    OutcomeSignal.USER_CORRECTION: 0.5,
    OutcomeSignal.DELEGATION_SUCCESS: 0.6,
    OutcomeSignal.DELEGATION_FAILURE: -0.5,
    OutcomeSignal.TOOL_SUCCESS: 0.3,
    OutcomeSignal.TOOL_ERROR: -0.4,
    OutcomeSignal.MANUAL_REWARD: 0.0,  # caller specifies value
}


@dataclass
class RewardConfig:
    """Configuration for reward computation."""

    temporal_discount: float = 0.85
    """Discount factor per step backward (0-1). Lower = faster decay."""

    proximity_bonus: float = 0.2
    """Extra reward for the immediate predecessor turn."""

    min_reward: float = -2.0
    """Clipped minimum per-trace reward."""

    max_reward: float = 2.0
    """Clipped maximum per-trace reward."""

    backpropagation_steps: int = 10
    """Max number of steps to backpropagate through."""


class RewardEngine:
    """Computes and backpropagates reward signals through trace sequences."""

    def __init__(self, config: Optional[RewardConfig] = None):
        self._config = config or RewardConfig()

    # ── Public API ────────────────────────────────────────

    def apply_outcome(
        self,
        traces: list[TraceRow],
        signal: OutcomeSignal,
        target_turn_index: Optional[int] = None,
        manual_value: float = 0.0,
        metadata: Optional[dict[str, Any]] = None,
    ) -> list[TraceRow]:
        """Apply an outcome signal to a trace sequence.

        Args:
            traces: Chronologically ordered trace list.
            signal: Type of outcome signal.
            target_turn_index: Which turn the signal applies to
                (None = last turn).
            manual_value: Custom reward when signal is MANUAL_REWARD.
            metadata: Extra info attached to the signal.

        Returns:
            Updated traces with rewards applied.
        """
        if not traces:
            return traces

        if target_turn_index is None:
            target_idx = len(traces) - 1
        else:
            target_idx = next(
                (i for i, t in enumerate(traces) if t.turn_index == target_turn_index),
                len(traces) - 1,
            )

        # Base reward from signal type
        base_reward = _SIGNAL_REWARDS.get(signal, 0.0)
        if signal == OutcomeSignal.MANUAL_REWARD:
            base_reward = manual_value

        logger.info(
            "Applying %s (reward=%.2f) at turn %d",
            signal.value, base_reward, traces[target_idx].turn_index if traces else -1,
        )

        # Backpropagate
        updated = self._backpropagate(traces, target_idx, base_reward)

        # Annotate metadata
        timestamp = datetime.now(timezone.utc).isoformat()
        for t in updated:
            if t.metadata is None:
                t.metadata = {}
            if "rewards" not in t.metadata:
                t.metadata["rewards"] = []
            t.metadata["rewards"].append({
                "signal": signal.value,
                "value": t.reward,
                "timestamp": timestamp,
                **(metadata or {}),
            })

        return updated

    def compute_session_reward(
        self,
        traces: list[TraceRow],
        session_success: bool = True,
    ) -> list[TraceRow]:
        """Compute reward for an entire session.

        Rewards the last turn with TASK_SUCCESS/FAILURE, backpropagates.
        """
        signal = (
            OutcomeSignal.TASK_SUCCESS
            if session_success
            else OutcomeSignal.TASK_FAILURE
        )
        return self.apply_outcome(traces, signal)

    # ── Internal ──────────────────────────────────────────

    def _backpropagate(
        self,
        traces: list[TraceRow],
        target_idx: int,
        base_reward: float,
    ) -> list[TraceRow]:
        """Backpropagate reward from target backward through trace chain."""
        cfg = self._config
        max_steps = min(cfg.backpropagation_steps, target_idx + 1)
        updated = list(traces)

        for offset in range(max_steps):
            idx = target_idx - offset
            if idx < 0:
                break

            # Temporal discount: reward decays with distance from signal
            discount = cfg.temporal_discount ** offset

            # Proximity bonus for immediate predecessor
            bonus = cfg.proximity_bonus if offset == 1 else 0.0

            reward = base_reward * discount + bonus

            # Clip
            reward = max(cfg.min_reward, min(cfg.max_reward, reward))

            trace = updated[idx]
            updated[idx] = TraceRow(
                id=trace.id,
                session_id=trace.session_id,
                turn_index=trace.turn_index,
                user_content=trace.user_content,
                assistant_content=trace.assistant_content,
                embedding=trace.embedding,
                reward=reward,
                tags=trace.tags,
                metadata=dict(trace.metadata) if trace.metadata else {},
                created_at=trace.created_at,
            )

        return updated

    @staticmethod
    def compute_embedding_similarity(
        traces: list[TraceRow],
        query_embedding: list[float],
    ) -> dict[str, float]:
        """Compute similarity-based reward for each trace.

        Useful for rewarding traces similar to a high-value query.
        """
        from cc_star.cache.vector import cosine_similarity

        rewards: dict[str, float] = {}
        for trace in traces:
            if trace.embedding and len(trace.embedding) > 0:
                sim = cosine_similarity(query_embedding, trace.embedding)
                rewards[trace.id] = float(sim)
            else:
                rewards[trace.id] = 0.0
        return rewards

    @staticmethod
    def aggregate_rewards(traces: list[TraceRow]) -> dict[str, float]:
        """Aggregate reward statistics for a trace set."""
        if not traces:
            return {"mean": 0.0, "std": 0.0, "min": 0.0, "max": 0.0, "total": 0.0}

        rewards = [t.reward for t in traces]
        arr = np.array(rewards)
        return {
            "mean": float(np.mean(arr)),
            "std": float(np.std(arr)),
            "min": float(np.min(arr)),
            "max": float(np.max(arr)),
            "total": float(np.sum(arr)),
        }
