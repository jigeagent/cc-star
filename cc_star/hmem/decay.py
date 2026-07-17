"""Ebbinghaus forgetting curve decay for H-MEM dynamic memory regulation.

Implements the paper's memory weight decay:
    base = exp(-days_since_access / k)
    weight = base * (1 + freq_bonus) * feedback_mod

Where:
    k = 1.84 (Ebbinghaus constant)
    freq_bonus = min(1.0, access_count * 0.05)
    feedback_mod = 1.0 + 0.2*approval - 0.3*rebuttal (clamped [0.1, 3.0])
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Optional

from cc_star.hmem.models import DecayConfig, HierarchyNode
from cc_star.hmem.store import HierarchicalStore

logger = logging.getLogger(__name__)


class EbbinghausDecay:
    """Ebbinghaus forgetting curve decay processor.

    Usage:
        decay = EbbinghausDecay()
        new_weight = decay.compute(node)
    """

    def __init__(self, config: Optional[DecayConfig] = None):
        self.config = config or DecayConfig()

    def compute(self, node: HierarchyNode) -> float:
        """Compute the new decayed weight for a node.

        Args:
            node: The hierarchy node (must have access_count, last_accessed_at,
                  approval_count, rebuttal_count populated).

        Returns:
            New weight value (clamped to [min_weight, max_weight]).
        """
        days_since = self._days_since(node.last_accessed_at)

        # 1. Ebbinghaus base decay
        base = math.exp(-days_since / self.config.ebbinghaus_k)

        # 2. Access frequency bonus
        freq_bonus = min(
            self.config.access_bonus_cap,
            node.access_count * 0.05,
        )

        # 3. Feedback modulation
        feedback_mod = (
            1.0
            + self.config.feedback_approval_mult * (self.config.feedback_approval_mult - 1.0) * node.approval_count
            - (1.0 - self.config.feedback_rebuttal_mult) * node.rebuttal_count
        )
        feedback_mod = max(0.1, min(3.0, feedback_mod))

        # Combined
        weight = base * (1.0 + freq_bonus) * feedback_mod

        # Clamp
        return max(self.config.min_weight, min(self.config.max_weight, weight))

    def compute_decay_factor(
        self,
        days_since_access: float,
        access_count: int,
        approval_count: int = 0,
        rebuttal_count: int = 0,
    ) -> float:
        """Pure function: compute decay factor given stats (for testing)."""
        base = math.exp(-days_since_access / self.config.ebbinghaus_k)
        freq_bonus = min(self.config.access_bonus_cap, access_count * 0.05)
        feedback_mod = (
            1.0
            + (self.config.feedback_approval_mult - 1.0) * approval_count
            - (1.0 - self.config.feedback_rebuttal_mult) * rebuttal_count
        )
        feedback_mod = max(0.1, min(3.0, feedback_mod))
        weight = base * (1.0 + freq_bonus) * feedback_mod
        return max(self.config.min_weight, min(self.config.max_weight, weight))

    # ── Batch decay scheduler ──

    def run_scheduled_decay(
        self, store: HierarchicalStore, dry_run: bool = False,
    ) -> dict:
        """Run decay on all episode nodes that haven't been updated recently.

        Called periodically (every config.decay_interval_hours) from promote.

        Returns:
            Dict with processed count and stats.
        """
        nodes = store.get_nodes_needing_decay(self.config.decay_interval_hours)
        if not nodes:
            return {"processed": 0, "message": "no nodes need decay"}

        updates: list[tuple[float, int, str, str, str]] = []  # weight, access_count, last_accessed, updated_at, id
        stats = {"total": len(nodes), "changed": 0, "unchanged": 0}

        for node in nodes:
            new_weight = self.compute(node)
            # Only update if weight changed significantly
            if abs(new_weight - node.weight) < 0.001:
                stats["unchanged"] += 1
                continue

            stats["changed"] += 1
            now = datetime.now(timezone.utc).isoformat()
            updates.append((new_weight, node.access_count, node.last_accessed_at or now, now, node.id))

        if dry_run:
            return {
                "processed": len(nodes),
                "would_update": len(updates),
                "stats": stats,
                "dry_run": True,
            }

        if updates:
            store.batch_update_weights(updates)

        return {
            "processed": len(nodes),
            "updated": len(updates),
            "stats": stats,
        }

    def run_full_decay(
        self, store: HierarchicalStore, dry_run: bool = False,
    ) -> dict:
        """Run decay on ALL episode nodes (not just recently touched ones).

        This is the "full sweep" version — used during promote maintenance.
        """
        from cc_star.hmem.models import Layer
        nodes = store.get_layer_nodes(Layer.EPISODE)
        if not nodes:
            return {"processed": 0, "message": "no episode nodes"}

        updates: list[tuple[float, int, str, str, str]] = []
        stats = {"total": len(nodes), "changed": 0, "unchanged": 0}

        for node in nodes:
            new_weight = self.compute(node)
            if abs(new_weight - node.weight) < 0.001:
                stats["unchanged"] += 1
                continue
            stats["changed"] += 1
            now = datetime.now(timezone.utc).isoformat()
            updates.append((new_weight, node.access_count, node.last_accessed_at or now, now, node.id))

        if dry_run:
            return {"processed": len(nodes), "would_update": len(updates), "stats": stats, "dry_run": True}

        if updates:
            store.batch_update_weights(updates)

        return {"processed": len(nodes), "updated": len(updates), "stats": stats}

    @staticmethod
    def _days_since(iso_timestamp: str) -> float:
        """Calculate days since an ISO8601 timestamp."""
        if not iso_timestamp:
            return 30.0  # Default: assume stale
        try:
            dt = datetime.fromisoformat(iso_timestamp)
            # Ensure both are timezone-aware
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            delta = now - dt
            return delta.total_seconds() / 86400.0
        except (ValueError, TypeError):
            return 30.0
