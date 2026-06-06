"""L3 World Model abstraction — clustering traces/policies into concepts
and extracting subject-predicate-object triples for relational reasoning.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from cc_star.memos.id import new_id
from cc_star.memos.types import PolicyRow, TraceRow

logger = logging.getLogger(__name__)


@dataclass
class Concept:
    """A cluster of related entities forming a conceptual node."""

    id: str
    label: str
    description: str = ""
    embedding: list[float] | None = None
    member_trace_ids: list[str] = field(default_factory=list)
    member_policy_ids: list[str] = field(default_factory=list)
    triple_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "embedding": self.embedding,
            "member_trace_ids": self.member_trace_ids,
            "member_policy_ids": self.member_policy_ids,
            "triple_ids": self.triple_ids,
            "metadata": self.metadata,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Concept":
        return cls(
            id=data["id"],
            label=data.get("label", ""),
            description=data.get("description", ""),
            embedding=data.get("embedding"),
            member_trace_ids=data.get("member_trace_ids", []),
            member_policy_ids=data.get("member_policy_ids", []),
            triple_ids=data.get("triple_ids", []),
            metadata=data.get("metadata", {}),
            created_at=data.get("created_at", ""),
        )


@dataclass
class Triple:
    """A subject-predicate-object triple representing a relational fact."""

    id: str
    subject: str
    predicate: str
    object_: str
    confidence: float = 1.0
    source_trace_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "subject": self.subject,
            "predicate": self.predicate,
            "object": self.object_,
            "confidence": self.confidence,
            "source_trace_id": self.source_trace_id,
            "metadata": self.metadata,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Triple":
        return cls(
            id=data["id"],
            subject=data.get("subject", ""),
            predicate=data.get("predicate", ""),
            object_=data.get("object", ""),
            confidence=data.get("confidence", 1.0),
            source_trace_id=data.get("source_trace_id", ""),
            metadata=data.get("metadata", {}),
            created_at=data.get("created_at", ""),
        )


@dataclass
class WorldModelConfig:
    """Configuration for world model operations."""

    cluster_sim_threshold: float = 0.7
    """Cosine similarity threshold for concept clustering."""

    min_traces_for_concept: int = 2
    """Minimum traces to form a concept."""

    triple_min_confidence: float = 0.3
    """Minimum confidence for a triple to be stored."""

    max_concepts: int = 200
    """Maximum number of stored concepts."""


class WorldModel:
    """L3 World Model — clusters traces/policies into concepts and
    extracts relational triples for reasoning.
    """

    def __init__(self, config: Optional[WorldModelConfig] = None):
        self._config = config or WorldModelConfig()
        self._concepts: dict[str, Concept] = {}
        self._triples: dict[str, Triple] = {}

    # ── Concept Management ────────────────────────────────

    def cluster(
        self,
        traces: Optional[list[TraceRow]] = None,
        policies: Optional[list[PolicyRow]] = None,
    ) -> list[Concept]:
        """Cluster traces and policies into concepts.

        Returns new concepts discovered.
        """
        new_concepts: list[Concept] = []

        if traces:
            # Group traces by embedding similarity
            trace_concepts = self._cluster_traces(traces)
            new_concepts.extend(trace_concepts)

        if policies:
            policy_concepts = self._cluster_policies(policies)
            new_concepts.extend(policy_concepts)

        # Store and deduplicate
        for concept in new_concepts:
            existing = self._find_similar_concept(concept)
            if existing:
                self._merge_concepts(existing.id, concept.id)
            else:
                self._concepts[concept.id] = concept

        return new_concepts

    def get_concept(self, concept_id: str) -> Optional[Concept]:
        return self._concepts.get(concept_id)

    def list_concepts(self) -> list[Concept]:
        return list(self._concepts.values())

    def find_concepts_by_label(self, label: str) -> list[Concept]:
        """Find concepts whose label contains the query string."""
        query = label.lower()
        return [
            c for c in self._concepts.values()
            if query in c.label.lower()
        ]

    # ── Triple Extraction ─────────────────────────────────

    def extract_triples(
        self,
        trace: TraceRow,
    ) -> list[Triple]:
        """Extract subject-predicate-object triples from trace content."""
        triples: list[Triple] = []
        text = f"{trace.user_content}\n{trace.assistant_content}"

        # Pattern 1: "[Subject] is/are [Object]" or "[Subject] was/were [Object]"
        is_pattern = re.findall(
            r'(\w+(?:\s+\w+){0,3})\s+(?:is|are|was|were)\s+(\w+(?:[\s,]\w+){0,5})',
            text,
        )
        for subj, obj in is_pattern:
            triples.append(self._make_triple(
                subject=subj.strip(),
                predicate="is_a",
                object_=obj.strip().rstrip(".,"),
                trace_id=trace.id,
            ))

        # Pattern 2: "[Subject] uses/uses/requires [Object]"
        uses_pattern = re.findall(
            r'(\w+(?:\s+\w+){0,3})\s+(?:uses?|requires?|needs|runs|implements)\s+(\w+(?:[\s,]\w+){0,5})',
            text,
        )
        for subj, obj in uses_pattern:
            triples.append(self._make_triple(
                subject=subj.strip(),
                predicate="uses",
                object_=obj.strip().rstrip(".,"),
                trace_id=trace.id,
            ))

        # Pattern 3: "[Subject] provides/enables/supports [Object]"
        provides_pattern = re.findall(
            r'(\w+(?:\s+\w+){0,3})\s+(?:provides?|enables?|supports?|creates?|generates?)\s+(\w+(?:[\s,]\w+){0,5})',
            text,
        )
        for subj, obj in provides_pattern:
            triples.append(self._make_triple(
                subject=subj.strip(),
                predicate="provides",
                object_=obj.strip().rstrip(".,"),
                trace_id=trace.id,
            ))

        # Pattern 4: "[Subject] depends on / built on / part of [Object]"
        dep_pattern = re.findall(
            r'(\w+(?:\s+\w+){0,3})\s+(?:depends?\s+on|built\s+(?:on|upon|with)|part\s+of|based\s+on)\s+(\w+(?:[\s,]\w+){0,5})',
            text,
        )
        for subj, obj in dep_pattern:
            triples.append(self._make_triple(
                subject=subj.strip(),
                predicate="depends_on",
                object_=obj.strip().rstrip(".,"),
                trace_id=trace.id,
            ))

        # Filter by confidence
        triples = [t for t in triples if t.confidence >= self._config.triple_min_confidence]

        # Store new triples
        for t in triples:
            self._triples[t.id] = t

        return triples

    def get_triple(self, triple_id: str) -> Optional[Triple]:
        return self._triples.get(triple_id)

    def query_triples(
        self,
        subject: Optional[str] = None,
        predicate: Optional[str] = None,
        object_: Optional[str] = None,
    ) -> list[Triple]:
        """Query triples by subject, predicate, or object."""
        results = list(self._triples.values())

        if subject:
            results = [t for t in results if subject.lower() in t.subject.lower()]
        if predicate:
            results = [t for t in results if predicate.lower() in t.predicate.lower()]
        if object_:
            results = [t for t in results if object_.lower() in t.object_.lower()]

        results.sort(key=lambda t: t.confidence, reverse=True)
        return results

    def list_triples(self) -> list[Triple]:
        return list(self._triples.values())

    # ── Serialization ─────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "concepts": {k: v.to_dict() for k, v in self._concepts.items()},
            "triples": {k: v.to_dict() for k, v in self._triples.items()},
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        config: Optional[WorldModelConfig] = None,
    ) -> "WorldModel":
        wm = cls(config)
        for cid, cdata in data.get("concepts", {}).items():
            wm._concepts[cid] = Concept.from_dict(cdata)
        for tid, tdata in data.get("triples", {}).items():
            wm._triples[tid] = Triple.from_dict(tdata)
        return wm

    # ── Internal ──────────────────────────────────────────

    def _cluster_traces(self, traces: list[TraceRow]) -> list[Concept]:
        """Greedy clustering of traces by embedding similarity."""
        candidates = [t for t in traces if t.embedding and len(t.embedding) > 0]
        assigned = set()
        concepts: list[Concept] = []

        for i, seed in enumerate(candidates):
            if seed.id in assigned:
                continue
            members = [seed]
            assigned.add(seed.id)

            for j in range(i + 1, len(candidates)):
                if candidates[j].id in assigned:
                    continue
                sim = self._cosine_sim(
                    seed.embedding, candidates[j].embedding  # type: ignore[arg-type]
                )
                if sim >= self._config.cluster_sim_threshold:
                    members.append(candidates[j])
                    assigned.add(candidates[j].id)

            if len(members) >= self._config.min_traces_for_concept:
                label = self._derive_label(members)
                centroid = self._compute_centroid(
                    [m.embedding for m in members if m.embedding]
                )
                concepts.append(Concept(
                    id=f"cpt_{new_id(20)}",
                    label=label,
                    description=f"Concept from {len(members)} related traces",
                    embedding=centroid,
                    member_trace_ids=[m.id for m in members],
                    created_at=datetime.now(timezone.utc).isoformat(),
                ))

        return concepts

    def _cluster_policies(self, policies: list[PolicyRow]) -> list[Concept]:
        """Cluster policies into higher-level concepts."""
        candidates = [p for p in policies if p.embedding and len(p.embedding) > 0]
        assigned = set()
        concepts: list[Concept] = []

        # Group by name prefix
        name_groups: dict[str, list[PolicyRow]] = {}
        for p in candidates:
            prefix = p.name.split("_")[0] if "_" in p.name else p.name[:15]
            if prefix not in name_groups:
                name_groups[prefix] = []
            name_groups[prefix].append(p)

        for prefix, group in name_groups.items():
            if len(group) >= 2:
                centroid = self._compute_centroid(
                    [p.embedding for p in group if p.embedding]
                )
                concepts.append(Concept(
                    id=f"cpt_{new_id(20)}",
                    label=f"{prefix}_patterns",
                    description=f"Policy group: {len(group)} related patterns",
                    embedding=centroid,
                    member_policy_ids=[p.id for p in group],
                    created_at=datetime.now(timezone.utc).isoformat(),
                ))
                for p in group:
                    assigned.add(p.id)

        return concepts

    def _find_similar_concept(self, concept: Concept) -> Optional[Concept]:
        """Find if a similar concept already exists."""
        if not concept.embedding:
            return None
        for existing in self._concepts.values():
            if existing.embedding:
                sim = self._cosine_sim(concept.embedding, existing.embedding)
                if sim > 0.92:
                    return existing
        return None

    def _merge_concepts(self, target_id: str, source_id: str) -> None:
        """Merge source concept into target concept."""
        target = self._concepts.get(target_id)
        source = self._concepts.get(source_id)
        if not target or not source:
            return

        merged_traces = list(set(target.member_trace_ids + source.member_trace_ids))
        merged_policies = list(set(target.member_policy_ids + source.member_policy_ids))

        self._concepts[target_id] = Concept(
            id=target.id,
            label=target.label,
            description=f"Merged: {target.description} | {source.description}",
            embedding=self._compute_centroid(
                [e for e in [target.embedding, source.embedding] if e]
            ),
            member_trace_ids=merged_traces,
            member_policy_ids=merged_policies,
            triple_ids=list(set(target.triple_ids + source.triple_ids)),
            metadata={
                **target.metadata,
                "merged_from": source_id,
                "merged_at": datetime.now(timezone.utc).isoformat(),
            },
            created_at=target.created_at,
        )
        del self._concepts[source_id]

    def _make_triple(
        self,
        subject: str,
        predicate: str,
        object_: str,
        trace_id: str,
    ) -> Triple:
        return Triple(
            id=f"tri_{new_id(20)}",
            subject=subject,
            predicate=predicate,
            object_=object_,
            confidence=0.5,
            source_trace_id=trace_id,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    @staticmethod
    def _derive_label(members: list[TraceRow]) -> str:
        """Derive a human-readable label from trace members."""
        # Use most common non-stopword across user contents
        stopwords = {
            "the", "a", "an", "is", "are", "was", "were", "to", "of",
            "in", "for", "on", "and", "or", "with", "about", "what",
            "how", "why", "can", "do", "does", "did", "will", "would",
            "could", "should", "has", "have", "had", "been", "being",
            "it", "its", "that", "this", "these", "those", "i", "you",
        }
        word_counts: dict[str, int] = {}
        for m in members:
            words = re.findall(r'\b([A-Za-z]{4,})\b', m.user_content.lower())
            for w in words:
                if w not in stopwords:
                    word_counts[w] = word_counts.get(w, 0) + 1

        if not word_counts:
            return f"concept_{new_id(10).lower()}"

        sorted_words = sorted(word_counts, key=word_counts.get, reverse=True)
        top = sorted_words[:3]
        return "_".join(top)

    @staticmethod
    def _compute_centroid(embeddings: list[list[float]]) -> list[float]:
        if not embeddings:
            return []
        import numpy as np
        arr = np.array(embeddings)
        centroid = np.mean(arr, axis=0)
        norm = np.linalg.norm(centroid)
        if norm > 1e-12:
            centroid = centroid / norm
        return centroid.tolist()

    @staticmethod
    def _cosine_sim(a: list[float], b: list[float]) -> float:
        import numpy as np
        va = np.array(a, dtype=np.float64)
        vb = np.array(b, dtype=np.float64)
        norm_a = np.linalg.norm(va)
        norm_b = np.linalg.norm(vb)
        if norm_a < 1e-12 or norm_b < 1e-12:
            return 0.0
        return float(np.dot(va, vb) / (norm_a * norm_b))
