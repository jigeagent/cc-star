"""Policy repository — local SQLite CRUD for policies."""

from __future__ import annotations

import json
from typing import Any, Optional

from cc_star.cache.connection import CacheConnection
from cc_star.cache.schema import ensure_schema
from cc_star.memos.types import PolicyRow


class PolicyRepository:
    """Persist and query policies locally."""

    def __init__(self, cache: CacheConnection):
        self._cache = cache
        ensure_schema(cache)

    def insert(self, policy: PolicyRow) -> None:
        """Insert a policy into local cache."""
        conn = self._cache.conn
        conn.execute(
            """
            INSERT OR REPLACE INTO policies
                (id, name, description, trigger_pattern, action_template,
                 embedding, confidence, activation_count, source_trace_ids,
                 metadata, created_at, synced)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                policy.id,
                policy.name,
                policy.description,
                policy.trigger_pattern,
                policy.action_template,
                json.dumps(policy.embedding) if policy.embedding else None,
                policy.confidence,
                policy.activation_count,
                json.dumps(policy.source_trace_ids, ensure_ascii=False),
                json.dumps(policy.metadata, ensure_ascii=False, default=str),
                policy.created_at,
                0,
            ),
        )
        conn.commit()

    def get(self, policy_id: str) -> Optional[PolicyRow]:
        """Get a policy by ID."""
        row = self._cache.conn.execute(
            "SELECT * FROM policies WHERE id = ?", (policy_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_policy(row)

    def list_active(self, min_confidence: float = 0.3, limit: int = 20) -> list[PolicyRow]:
        """List policies with confidence above threshold."""
        rows = self._cache.conn.execute(
            "SELECT * FROM policies WHERE confidence >= ? ORDER BY confidence DESC LIMIT ?",
            (min_confidence, limit),
        ).fetchall()
        return [self._row_to_policy(r) for r in rows]

    def increment_activation(self, policy_id: str) -> None:
        """Increment activation count for a policy."""
        self._cache.conn.execute(
            "UPDATE policies SET activation_count = activation_count + 1 WHERE id = ?",
            (policy_id,),
        )
        self._cache.conn.commit()

    def count(self) -> int:
        """Total policy count."""
        row = self._cache.conn.execute("SELECT COUNT(*) as cnt FROM policies").fetchone()
        return row["cnt"] if row else 0

    @staticmethod
    def _row_to_policy(row: Any) -> PolicyRow:
        return PolicyRow(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            trigger_pattern=row["trigger_pattern"],
            action_template=row["action_template"],
            embedding=json.loads(row["embedding"]) if row["embedding"] else None,
            confidence=row["confidence"],
            activation_count=row["activation_count"],
            source_trace_ids=json.loads(row["source_trace_ids"])
            if isinstance(row["source_trace_ids"], str)
            else [],
            metadata=json.loads(row["metadata"]) if isinstance(row["metadata"], str) else {},
            created_at=row["created_at"],
        )
