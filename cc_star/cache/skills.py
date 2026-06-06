"""Skill repository — local SQLite CRUD for skills."""

from __future__ import annotations

import json
from typing import Any, Optional

from cc_star.cache.connection import CacheConnection
from cc_star.cache.schema import ensure_schema
from cc_star.memos.types import SkillRow


class SkillRepository:
    """Persist and query skills locally."""

    def __init__(self, cache: CacheConnection):
        self._cache = cache
        ensure_schema(cache)

    def insert(self, skill: SkillRow) -> None:
        """Insert a skill into local cache."""
        conn = self._cache.conn
        conn.execute(
            """
            INSERT OR REPLACE INTO skills
                (name, description, usage_guide, source_policy_ids,
                 version, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                skill.name,
                skill.description,
                skill.usage_guide,
                json.dumps(skill.source_policy_ids, ensure_ascii=False),
                skill.version,
                json.dumps(skill.metadata, ensure_ascii=False, default=str),
                skill.created_at,
            ),
        )
        conn.commit()

    def get(self, name: str) -> Optional[SkillRow]:
        """Get a skill by name."""
        row = self._cache.conn.execute(
            "SELECT * FROM skills WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_skill(row)

    def list_all(self) -> list[SkillRow]:
        """List all skills."""
        rows = self._cache.conn.execute(
            "SELECT * FROM skills ORDER BY name ASC"
        ).fetchall()
        return [self._row_to_skill(r) for r in rows]

    def search(self, query: str, limit: int = 10) -> list[SkillRow]:
        """Search skills by name or description."""
        like = f"%{query}%"
        rows = self._cache.conn.execute(
            "SELECT * FROM skills WHERE name LIKE ? OR description LIKE ? LIMIT ?",
            (like, like, limit),
        ).fetchall()
        return [self._row_to_skill(r) for r in rows]

    def delete(self, name: str) -> None:
        """Delete a skill by name."""
        self._cache.conn.execute("DELETE FROM skills WHERE name = ?", (name,))
        self._cache.conn.commit()

    def count(self) -> int:
        """Total skill count."""
        row = self._cache.conn.execute("SELECT COUNT(*) as cnt FROM skills").fetchone()
        return row["cnt"] if row else 0

    @staticmethod
    def _row_to_skill(row: Any) -> SkillRow:
        return SkillRow(
            name=row["name"],
            description=row["description"],
            usage_guide=row["usage_guide"],
            source_policy_ids=json.loads(row["source_policy_ids"])
            if isinstance(row["source_policy_ids"], str)
            else [],
            version=row["version"],
            metadata=json.loads(row["metadata"]) if isinstance(row["metadata"], str) else {},
            created_at=row["created_at"],
        )
