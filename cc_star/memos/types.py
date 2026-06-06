"""MemOS data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class TraceRow:
    """A single L1 Trace — captures a raw interaction turn.

    Stored in OpenViking as: viking://resources/{agent}/memos/traces/{id}.json
    """

    id: str
    session_id: str
    turn_index: int
    user_content: str
    assistant_content: str
    embedding: list[float] | None = None
    reward: float = 0.0
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "turn_index": self.turn_index,
            "user_content": self.user_content,
            "assistant_content": self.assistant_content,
            "embedding": self.embedding,
            "reward": self.reward,
            "tags": self.tags,
            "metadata": self.metadata,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TraceRow":
        return cls(
            id=data["id"],
            session_id=data.get("session_id", ""),
            turn_index=data.get("turn_index", 0),
            user_content=data.get("user_content", ""),
            assistant_content=data.get("assistant_content", ""),
            embedding=data.get("embedding"),
            reward=data.get("reward", 0.0),
            tags=data.get("tags", []),
            metadata=data.get("metadata", {}),
            created_at=data.get("created_at", ""),
        )


@dataclass
class PolicyRow:
    """L2 Policy — induced pattern from multiple traces.

    Stored in OpenViking as: viking://resources/{agent}/memos/policies/{id}.json
    """

    id: str
    name: str
    description: str
    trigger_pattern: str
    action_template: str
    embedding: list[float] | None = None
    confidence: float = 0.0
    activation_count: int = 0
    source_trace_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "trigger_pattern": self.trigger_pattern,
            "action_template": self.action_template,
            "embedding": self.embedding,
            "confidence": self.confidence,
            "activation_count": self.activation_count,
            "source_trace_ids": self.source_trace_ids,
            "metadata": self.metadata,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PolicyRow":
        return cls(
            id=data["id"],
            name=data.get("name", ""),
            description=data.get("description", ""),
            trigger_pattern=data.get("trigger_pattern", ""),
            action_template=data.get("action_template", ""),
            embedding=data.get("embedding"),
            confidence=data.get("confidence", 0.0),
            activation_count=data.get("activation_count", 0),
            source_trace_ids=data.get("source_trace_ids", []),
            metadata=data.get("metadata", {}),
            created_at=data.get("created_at", ""),
        )


@dataclass
class SkillRow:
    """Crystallized Skill — a reusable capability derived from policies.

    Stored in OpenViking as: viking://resources/{agent}/memos/skills/{name}.md
    """

    name: str
    description: str
    usage_guide: str
    source_policy_ids: list[str] = field(default_factory=list)
    version: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def to_markdown(self) -> str:
        lines = [
            f"# {self.name}",
            "",
            self.description,
            "",
            "## Usage",
            "",
            self.usage_guide,
            "",
            f"Version: {self.version}",
        ]
        if self.source_policy_ids:
            lines.append(f"Source policies: {', '.join(self.source_policy_ids)}")
        return "\n".join(lines)

    @classmethod
    def from_markdown(cls, name: str, content: str) -> "SkillRow":
        parts = content.split("\n\n")
        description = parts[1] if len(parts) > 1 else ""
        usage_guide = parts[3] if len(parts) > 3 else ""
        return cls(
            name=name,
            description=description,
            usage_guide=usage_guide,
        )
