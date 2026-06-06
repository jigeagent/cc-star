"""L2 Policy induction — candidate pool → induction → activation.

Extracts reusable behavioral patterns from rewarded traces.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from cc_star.memos.id import new_id
from cc_star.memos.types import PolicyRow, TraceRow

logger = logging.getLogger(__name__)


@dataclass
class PolicyConfig:
    """Configuration for policy induction."""

    min_confidence: float = 0.15
    """Minimum confidence for a policy to be considered active."""

    min_traces_for_induction: int = 3
    """Minimum number of similar traces needed to induce a policy."""

    embedding_sim_threshold: float = 0.75
    """Cosine similarity threshold for grouping traces."""

    activation_sim_threshold: float = 0.6
    """Similarity threshold for activating a policy from context."""

    max_policies: int = 100
    """Maximum number of stored policies."""

    confidence_alpha: float = 0.3
    """Smoothing factor for confidence updates (0-1). Higher = more weight to new evidence."""

    pattern_max_examples: int = 5
    """Max exemplar traces stored per policy."""


class PolicyInducer:
    """Induces behavioral policies from trace patterns."""

    def __init__(self, config: Optional[PolicyConfig] = None):
        self._config = config or PolicyConfig()

    # ── Candidate Pool ────────────────────────────────────

    def build_candidate_pool(
        self,
        traces: list[TraceRow],
        pool: Optional[list[list[TraceRow]]] = None,
    ) -> list[list[TraceRow]]:
        """Group similar traces into candidate pools for induction.

        Uses embedding cosine similarity to cluster traces.
        """
        if pool is None:
            pool = []

        # Filter to traces with embeddings and meaningful reward
        candidates = [
            t for t in traces
            if t.embedding and len(t.embedding) > 0
        ]

        if not candidates:
            return pool

        # Greedy clustering by embedding similarity
        assigned = set()
        clusters: list[list[TraceRow]] = []

        for i, seed in enumerate(candidates):
            if seed.id in assigned:
                continue
            cluster = [seed]
            assigned.add(seed.id)

            for j in range(i + 1, len(candidates)):
                if candidates[j].id in assigned:
                    continue
                sim = self._cosine_sim(
                    seed.embedding, candidates[j].embedding  # type: ignore[arg-type]
                )
                if sim >= self._config.embedding_sim_threshold:
                    cluster.append(candidates[j])
                    assigned.add(candidates[j].id)

            if len(cluster) >= self._config.min_traces_for_induction:
                clusters.append(cluster)

        return clusters

    # ── Induction ─────────────────────────────────────────

    def induce(
        self,
        cluster: list[TraceRow],
    ) -> Optional[PolicyRow]:
        """Induce a policy from a cluster of similar traces.

        Extracts the common pattern (trigger + action) and computes confidence.
        """
        if len(cluster) < self._config.min_traces_for_induction:
            return None

        # Merge user content to find trigger pattern
        user_contents = [t.user_content for t in cluster]
        assistant_contents = [t.assistant_content for t in cluster]

        trigger_pattern = self._extract_pattern(user_contents)
        action_template = self._extract_pattern(assistant_contents)

        # Compute name from trigger keywords
        name = self._generate_name(trigger_pattern)

        # Confidence = average reward * cluster size factor
        avg_reward = sum(t.reward for t in cluster) / len(cluster)
        size_factor = min(1.0, len(cluster) / 10.0)
        confidence = max(0.0, avg_reward * 0.5 + size_factor * 0.5)

        # Description
        description = (
            f"Policy induced from {len(cluster)} similar interactions. "
            f"Trigger: {trigger_pattern[:100]}"
        )

        # Compute centroid embedding
        centroid = self._compute_centroid([t.embedding for t in cluster if t.embedding])

        return PolicyRow(
            id=f"pol_{new_id(20)}",
            name=name,
            description=description,
            trigger_pattern=trigger_pattern,
            action_template=action_template,
            embedding=centroid,
            confidence=round(confidence, 4),
            activation_count=0,
            source_trace_ids=[t.id for t in cluster],
            metadata={
                "cluster_size": len(cluster),
                "avg_reward": avg_reward,
                "induction_timestamp": datetime.now(timezone.utc).isoformat(),
            },
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def batch_induce(
        self,
        traces: list[TraceRow],
        existing_policies: Optional[list[PolicyRow]] = None,
    ) -> list[PolicyRow]:
        """Run full induction pipeline: build pools → induce → merge."""
        clusters = self.build_candidate_pool(traces)
        new_policies: list[PolicyRow] = []

        for cluster in clusters:
            policy = self.induce(cluster)
            if policy:
                # Check for duplicates with existing policies
                if existing_policies and self._find_duplicate(policy, existing_policies):
                    logger.debug("Skipping duplicate policy: %s", policy.name)
                    continue
                new_policies.append(policy)

        # Merge with existing
        if existing_policies:
            combined = self._merge_policies(existing_policies, new_policies)
            # Enforce cap
            if len(combined) > self._config.max_policies:
                combined.sort(key=lambda p: p.confidence, reverse=True)
                combined = combined[: self._config.max_policies]
            return combined

        return new_policies

    # ── Activation ────────────────────────────────────────

    def activate(
        self,
        context: str,
        policies: list[PolicyRow],
        context_embedding: Optional[list[float]] = None,
        k: int = 3,
    ) -> list[tuple[PolicyRow, float]]:
        """Find policies relevant to the given context.

        Returns:
            List of (policy, activation_score) tuples sorted by score.
        """
        if not policies:
            return []

        scored: list[tuple[PolicyRow, float]] = []

        for policy in policies:
            if policy.confidence < self._config.min_confidence:
                continue

            # Score from embedding similarity
            emb_score = 0.0
            if context_embedding and policy.embedding:
                emb_score = self._cosine_sim(context_embedding, policy.embedding)

            # Score from keyword overlap
            kw_score = self._keyword_overlap(context, policy.trigger_pattern)

            # Score from pattern match (regex)
            pattern_score = self._pattern_match(context, policy.trigger_pattern)

            # Combined score
            activation = (
                emb_score * 0.4 +
                kw_score * 0.3 +
                pattern_score * 0.3
            ) * policy.confidence

            if activation >= self._config.activation_sim_threshold:
                scored.append((policy, round(activation, 4)))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:k]

    # ── Confidence ────────────────────────────────────────

    def update_confidence(
        self,
        policy: PolicyRow,
        activation_success: bool,
    ) -> PolicyRow:
        """Update policy confidence after an activation event."""
        alpha = self._config.confidence_alpha
        signal = 1.0 if activation_success else -0.3
        new_confidence = (1 - alpha) * policy.confidence + alpha * signal
        new_confidence = max(0.0, min(1.0, new_confidence))

        return PolicyRow(
            id=policy.id,
            name=policy.name,
            description=policy.description,
            trigger_pattern=policy.trigger_pattern,
            action_template=policy.action_template,
            embedding=policy.embedding,
            confidence=round(new_confidence, 4),
            activation_count=policy.activation_count + 1,
            source_trace_ids=policy.source_trace_ids,
            metadata=dict(policy.metadata) if policy.metadata else {},
            created_at=policy.created_at,
        )

    # ── Internal helpers ──────────────────────────────────

    @staticmethod
    def _extract_pattern(contents: list[str]) -> str:
        """Extract a common pattern from a list of text contents."""
        if not contents:
            return ""

        if len(contents) == 1:
            return contents[0][:200]

        # Find common tokens by frequency analysis
        all_tokens: list[list[str]] = []
        for c in contents:
            tokens = re.findall(r'\b\w+\b', c.lower())
            all_tokens.append(set(tokens))

        common = set.intersection(*all_tokens) if all_tokens else set()
        # Use the first content as template, highlighting common tokens
        first = contents[0]
        if common:
            # Mark common tokens
            for token in sorted(common, key=len, reverse=True):
                first = re.sub(
                    rf'\b{re.escape(token)}\b', f'**{token}**',
                    first, flags=re.IGNORECASE,
                )
        return first[:200]

    @staticmethod
    def _generate_name(trigger_pattern: str) -> str:
        """Generate a human-readable policy name from the trigger pattern."""
        # Extract key nouns/verbs
        words = re.findall(r'\*\*(\w+)\*\*', trigger_pattern)
        if not words:
            words = re.findall(r'\b([A-Za-z]{4,})\b', trigger_pattern)
        if words:
            name = "_".join(w.lower() for w in words[:4])
            return name[:60]
        return f"policy_{new_id(10).lower()}"

    @staticmethod
    def _compute_centroid(embeddings: list[list[float]]) -> list[float]:
        """Compute centroid of multiple embedding vectors."""
        if not embeddings:
            return []
        import numpy as np
        arr = np.array(embeddings)
        centroid = np.mean(arr, axis=0)
        # Normalize
        norm = np.linalg.norm(centroid)
        if norm > 1e-12:
            centroid = centroid / norm
        return centroid.tolist()

    @staticmethod
    def _cosine_sim(a: list[float], b: list[float]) -> float:
        """Compute cosine similarity between two vectors."""
        import numpy as np
        va = np.array(a, dtype=np.float64)
        vb = np.array(b, dtype=np.float64)
        norm_a = np.linalg.norm(va)
        norm_b = np.linalg.norm(vb)
        if norm_a < 1e-12 or norm_b < 1e-12:
            return 0.0
        return float(np.dot(va, vb) / (norm_a * norm_b))

    @staticmethod
    def _keyword_overlap(text: str, pattern: str) -> float:
        """Compute keyword overlap score between text and pattern."""
        text_words = set(re.findall(r'\b\w+\b', text.lower()))
        pattern_words = set(re.findall(r'\b\w+\b', pattern.lower()))
        if not pattern_words:
            return 0.0
        intersection = text_words & pattern_words
        return len(intersection) / len(pattern_words)

    @staticmethod
    def _pattern_match(text: str, pattern: str) -> float:
        """Check if pattern regex matches the text."""
        # Extract bold tokens as required terms
        required = re.findall(r'\*\*(\w+)\*\*', pattern)
        if not required:
            return 0.0
        text_lower = text.lower()
        matches = sum(1 for r in required if r.lower() in text_lower)
        return matches / len(required)

    @staticmethod
    def _find_duplicate(policy: PolicyRow, existing: list[PolicyRow]) -> bool:
        """Check if a policy is a duplicate of an existing one."""
        for ex in existing:
            if ex.name == policy.name:
                return True
            if ex.embedding and policy.embedding:
                sim = PolicyInducer._cosine_sim(ex.embedding, policy.embedding)
                if sim > 0.95:
                    return True
        return False

    @staticmethod
    def _merge_policies(
        existing: list[PolicyRow],
        new_policies: list[PolicyRow],
    ) -> list[PolicyRow]:
        """Merge new policies into existing list, avoiding duplicates."""
        existing_map = {p.id: p for p in existing}
        for p in new_policies:
            # Check by name
            name_match = next(
                (e for e in existing_map.values() if e.name == p.name),
                None,
            )
            if name_match:
                # Merge source traces
                merged_traces = list(
                    set(name_match.source_trace_ids + p.source_trace_ids)
                )
                existing_map[name_match.id] = PolicyRow(
                    id=name_match.id,
                    name=name_match.name,
                    description=p.description,
                    trigger_pattern=p.trigger_pattern,
                    action_template=p.action_template,
                    embedding=p.embedding,
                    confidence=max(name_match.confidence, p.confidence),
                    activation_count=name_match.activation_count,
                    source_trace_ids=merged_traces,
                    metadata={
                        **name_match.metadata,
                        "merged_at": datetime.now(timezone.utc).isoformat(),
                    },
                    created_at=name_match.created_at,
                )
            else:
                existing_map[p.id] = p
        return list(existing_map.values())
