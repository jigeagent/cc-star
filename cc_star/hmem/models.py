"""H-MEM data models — HierarchyNode, SearchResult, FeedbackLog and supporting types."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


class Layer(str, Enum):
    """The four layers of the H-MEM hierarchy."""

    DOMAIN = "domain"
    CATEGORY = "category"
    TRACE = "trace"
    EPISODE = "episode"


@dataclass
class HierarchyNode:
    """A single node in the H-MEM four-layer hierarchy.

    Corresponds to the paper's vector definition:
        v_i^(L) = [e_i^(L), self_index, p_i1, ..., p_iK]
    """

    id: str
    layer: Layer
    parent_id: str | None = None
    domain_id: str | None = None       # Root domain id for fast ancestry lookup
    self_index: int = 0                # Position within its layer
    sub_indices: list[int] = field(default_factory=list)  # Children indices in next layer

    # Content
    title: str = ""
    summary: str = ""
    content: str = ""

    # Vector embedding (384-dim BGE)
    embedding: list[float] | None = None

    # Weight and decay
    weight: float = 1.0
    access_count: int = 0
    last_accessed_at: str = ""
    created_at: str = ""
    updated_at: str = ""

    # Feedback counters
    approval_count: int = 0
    rebuttal_count: int = 0

    # Metadata
    source_trace_id: str | None = None   # Link back to original flat trace
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Normalize embedding to a plain list (numpy arrays not JSON-serializable)."""
        if self.embedding is not None and hasattr(self.embedding, 'tolist'):
            self.embedding = self.embedding.tolist()

    def to_row(self) -> tuple:
        """Convert to SQLite row tuple."""
        return (
            self.id,
            self.layer.value,
            self.parent_id,
            self.domain_id,
            self.self_index,
            json.dumps(self.sub_indices),
            self.title,
            self.summary,
            self.content,
            json.dumps(self.embedding) if self.embedding is not None else None,
            self.weight,
            self.access_count,
            self.last_accessed_at,
            self.created_at,
            self.updated_at,
            self.approval_count,
            self.rebuttal_count,
            self.source_trace_id,
            json.dumps(self.metadata, ensure_ascii=False, default=str),
        )

    @classmethod
    def from_row(cls, row: Any) -> "HierarchyNode":
        """Create from sqlite3.Row."""
        return cls(
            id=row["id"],
            layer=Layer(row["layer"]),
            parent_id=row["parent_id"],
            domain_id=row["domain_id"],
            self_index=row["self_index"],
            sub_indices=json.loads(row["sub_indices"]) if row["sub_indices"] else [],
            title=row["title"] or "",
            summary=row["summary"] or "",
            content=row["content"] or "",
            embedding=json.loads(row["embedding"]) if row["embedding"] else None,
            weight=row["weight"],
            access_count=row["access_count"],
            last_accessed_at=row["last_accessed_at"] or "",
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
            approval_count=row["approval_count"],
            rebuttal_count=row["rebuttal_count"],
            source_trace_id=row["source_trace_id"],
            metadata=json.loads(row["metadata"]) if isinstance(row["metadata"], str) else {},
        )

    def touch(self) -> None:
        """Update access tracking fields."""
        self.access_count += 1
        self.last_accessed_at = datetime.now(timezone.utc).isoformat()

    @property
    def effective_weight(self) -> float:
        """Effective weight = stored weight (already decayed + feedback-modulated)."""
        return max(self.weight, 0.01)


@dataclass
class SearchResult:
    """Result from a layer-level search."""

    node_id: str
    score: float


@dataclass
class EpisodeResult:
    """Final retrieval result containing episode content."""

    episode_id: str
    content: str
    weight: float
    trace_title: str
    domain_id: str
    score: float
    effective_score: float


class FeedbackType(str, Enum):
    """User feedback types for memory weight regulation."""

    APPROVAL = "approval"
    REBUTTAL = "rebuttal"
    NO_FEEDBACK = "no_feedback"


@dataclass
class FeedbackLog:
    """A single feedback event record."""

    id: str
    node_id: str
    feedback_type: FeedbackType
    session_id: str
    user_message: str = ""
    llm_analysis: str = ""
    weight_before: float = 1.0
    weight_after: float = 1.0
    created_at: str = ""

    def to_row(self) -> tuple:
        return (
            self.id,
            self.node_id,
            self.feedback_type.value,
            self.session_id,
            self.user_message,
            self.llm_analysis,
            self.weight_before,
            self.weight_after,
            self.created_at,
        )

    @classmethod
    def from_row(cls, row: Any) -> "FeedbackLog":
        return cls(
            id=row["id"],
            node_id=row["node_id"],
            feedback_type=FeedbackType(row["feedback_type"]),
            session_id=row["session_id"],
            user_message=row["user_message"] or "",
            llm_analysis=row["llm_analysis"] or "",
            weight_before=row["weight_before"],
            weight_after=row["weight_after"],
            created_at=row["created_at"] or "",
        )


@dataclass
class DecayConfig:
    """Ebbinghaus forgetting curve parameters."""

    ebbinghaus_k: float = 1.84       # Ebbinghaus curve constant
    access_bonus_cap: float = 1.0    # Max bonus from access frequency
    feedback_approval_mult: float = 1.2   # Weight multiplier on approval
    feedback_rebuttal_mult: float = 0.5   # Weight multiplier on rebuttal
    min_weight: float = 0.01         # Floor for weight
    max_weight: float = 5.0          # Ceiling for weight
    decay_interval_hours: float = 4.0  # How often the decay scheduler runs
