"""Graph repository — CRUD + recursive CTE traversal for context graph."""

from __future__ import annotations

import json
from typing import Any

from cc_star.cache.connection import CacheConnection
from cc_star.graph.schema import ensure_graph_schema


# Valid entity and relation types
ENTITY_TYPES = frozenset({
    "project", "file", "decision", "source",
    "person", "task", "skill", "other",
})
RELATION_TYPES = frozenset({
    "references", "depends_on", "produces",
    "decided_by", "cites", "related_to",
})


class GraphRepository:
    """Persist and query the context graph."""

    def __init__(self, cache: CacheConnection):
        self._cache = cache
        ensure_graph_schema(cache)

    # ── Entity CRUD ──────────────────────────────────────────

    def add_entity(
        self,
        name: str,
        entity_type: str,
        description: str = "",
        metadata: dict | None = None,
        created_at: str = "",
    ) -> int:
        """Insert an entity and return its ID."""
        if entity_type not in ENTITY_TYPES:
            raise ValueError(f"Invalid entity type: {entity_type}")
        if not created_at:
            from datetime import datetime, timezone
            created_at = datetime.now(timezone.utc).isoformat()

        cursor = self._cache.execute(
            """INSERT INTO entities (name, type, description, metadata, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (name, entity_type, description,
             json.dumps(metadata or {}, ensure_ascii=False),
             created_at, created_at),
        )
        return cursor.lastrowid

    def get_entity(self, entity_id: int) -> dict | None:
        """Get an entity by ID."""
        row = self._cache.execute(
            "SELECT * FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        return self._row_to_entity(row) if row else None

    def get_entity_by_name(self, name: str) -> dict | None:
        """Get an entity by exact name match."""
        row = self._cache.execute(
            "SELECT * FROM entities WHERE name = ?", (name,)
        ).fetchone()
        return self._row_to_entity(row) if row else None

    def find_or_create_entity(
        self,
        name: str,
        entity_type: str,
        description: str = "",
        metadata: dict | None = None,
        created_at: str = "",
    ) -> int:
        """Return existing entity ID or create a new one."""
        existing = self.get_entity_by_name(name)
        if existing:
            return existing["id"]
        return self.add_entity(name, entity_type, description, metadata, created_at)

    def list_entities(
        self,
        entity_type: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """List entities, optionally filtered by type."""
        if entity_type:
            rows = self._cache.execute(
                "SELECT * FROM entities WHERE type = ? ORDER BY updated_at DESC LIMIT ?",
                (entity_type, limit),
            ).fetchall()
        else:
            rows = self._cache.execute(
                "SELECT * FROM entities ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_entity(r) for r in rows]

    def search_entities(self, query: str, limit: int = 10) -> list[dict]:
        """FTS5 search on entity name and description."""
        query = query.encode("utf-8", "surrogatepass").decode("utf-8", "replace")
        query = "".join(c for c in query if c.isprintable() or c in (" ", "\n", "\t"))
        safe = query.replace('"', '""')
        rows = self._cache.execute(
            """SELECT e.* FROM entities e
               JOIN entities_fts fts ON e.rowid = fts.rowid
               WHERE entities_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (safe, limit),
        ).fetchall()
        return [self._row_to_entity(r) for r in rows]

    def update_entity(
        self,
        entity_id: int,
        name: str | None = None,
        description: str | None = None,
        metadata: dict | None = None,
    ) -> bool:
        """Update entity fields. Returns True if a row was changed."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        sets = ["updated_at = ?"]
        params: list[Any] = [now]

        if name is not None:
            sets.append("name = ?")
            params.append(name)
        if description is not None:
            sets.append("description = ?")
            params.append(description)
        if metadata is not None:
            sets.append("metadata = ?")
            params.append(json.dumps(metadata, ensure_ascii=False))

        params.append(entity_id)
        cursor = self._cache.execute(
            f"UPDATE entities SET {', '.join(sets)} WHERE id = ?",
            tuple(params),
        )
        return cursor.rowcount > 0

    def entity_count(self) -> int:
        row = self._cache.execute("SELECT COUNT(*) as cnt FROM entities").fetchone()
        return row["cnt"] if row else 0

    def entity_counts_by_type(self) -> dict[str, int]:
        rows = self._cache.execute(
            "SELECT type, COUNT(*) as cnt FROM entities GROUP BY type"
        ).fetchall()
        return {r["type"]: r["cnt"] for r in rows}

    # ── Relation CRUD ────────────────────────────────────────

    def add_relation(
        self,
        source_id: int,
        target_id: int,
        rel_type: str,
        weight: float = 1.0,
        metadata: dict | None = None,
        created_at: str = "",
    ) -> int:
        """Insert a relation and return its ID."""
        if rel_type not in RELATION_TYPES:
            raise ValueError(f"Invalid relation type: {rel_type}")
        if not created_at:
            from datetime import datetime, timezone
            created_at = datetime.now(timezone.utc).isoformat()

        cursor = self._cache.execute(
            """INSERT INTO relations (source_id, target_id, type, weight, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (source_id, target_id, rel_type, weight,
             json.dumps(metadata or {}, ensure_ascii=False),
             created_at),
        )
        return cursor.lastrowid

    def add_relation_by_name(
        self,
        source_name: str,
        target_name: str,
        rel_type: str,
        weight: float = 1.0,
        metadata: dict | None = None,
    ) -> int:
        """Insert a relation by entity names (auto-creates if needed)."""
        src_id = self.find_or_create_entity(source_name, "other")
        tgt_id = self.find_or_create_entity(target_name, "other")
        return self.add_relation(src_id, tgt_id, rel_type, weight, metadata)

    def get_relations(
        self,
        entity_id: int | None = None,
        rel_type: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Get relations, optionally filtered by entity or type."""
        if entity_id and rel_type:
            rows = self._cache.execute(
                """SELECT * FROM relations
                   WHERE (source_id = ? OR target_id = ?) AND type = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (entity_id, entity_id, rel_type, limit),
            ).fetchall()
        elif entity_id:
            rows = self._cache.execute(
                """SELECT * FROM relations
                   WHERE source_id = ? OR target_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (entity_id, entity_id, limit),
            ).fetchall()
        elif rel_type:
            rows = self._cache.execute(
                "SELECT * FROM relations WHERE type = ? ORDER BY created_at DESC LIMIT ?",
                (rel_type, limit),
            ).fetchall()
        else:
            rows = self._cache.execute(
                "SELECT * FROM relations ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_relation(r) for r in rows]

    def relation_count(self) -> int:
        row = self._cache.execute("SELECT COUNT(*) as cnt FROM relations").fetchone()
        return row["cnt"] if row else 0

    # ── Timeline CRUD ────────────────────────────────────────

    def add_event(
        self,
        event_type: str,
        entity_id: int | None = None,
        session_id: str = "",
        payload: dict | None = None,
        created_at: str = "",
    ) -> int:
        """Insert a timeline event and return its ID."""
        if not created_at:
            from datetime import datetime, timezone
            created_at = datetime.now(timezone.utc).isoformat()

        cursor = self._cache.execute(
            """INSERT INTO timeline (entity_id, session_id, event_type, payload, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (entity_id, session_id, event_type,
             json.dumps(payload or {}, ensure_ascii=False),
             created_at),
        )
        return cursor.lastrowid

    def get_events(
        self,
        entity_id: int | None = None,
        session_id: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Get timeline events with optional filters."""
        conditions = []
        params: list[Any] = []

        if entity_id is not None:
            conditions.append("entity_id = ?")
            params.append(entity_id)
        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        rows = self._cache.execute(
            f"SELECT * FROM timeline {where} ORDER BY created_at DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def event_count(self) -> int:
        row = self._cache.execute("SELECT COUNT(*) as cnt FROM timeline").fetchone()
        return row["cnt"] if row else 0

    def get_failed_events(self, limit: int = 50) -> list[dict]:
        """Get graph_extract_failed events for worker remediation."""
        return self.get_events(event_type="graph_extract_failed", limit=limit)

    # ── Recursive CTE traversal ──────────────────────────────

    def get_subgraph(
        self,
        entity_id: int,
        max_depth: int = 3,
        direction: str = "both",
    ) -> dict:
        """Return connected subgraph around an entity using recursive CTE.

        Args:
            entity_id: Starting entity.
            max_depth: Max hops from the starting entity.
            direction: 'outgoing', 'incoming', or 'both'.

        Returns:
            {"entity": {...}, "neighbors": [...], "relations": [...]}
        """
        center = self.get_entity(entity_id)
        if not center:
            return {"entity": None, "neighbors": [], "relations": []}

        if direction == "outgoing":
            direction_sql = "r.source_id = closure.entity_id"
        elif direction == "incoming":
            direction_sql = "r.target_id = closure.entity_id"
        else:
            direction_sql = "(r.source_id = closure.entity_id OR r.target_id = closure.entity_id)"

        rows = self._cache.execute(
            f"""WITH RECURSIVE closure(entity_id, depth) AS (
                    VALUES(?, 0)
                    UNION
                    SELECT
                        CASE WHEN r.source_id = closure.entity_id
                             THEN r.target_id ELSE r.source_id END,
                        closure.depth + 1
                    FROM closure
                    JOIN relations r ON {direction_sql}
                    WHERE closure.depth < ?
                )
                SELECT DISTINCT closure.entity_id, closure.depth
                FROM closure
                ORDER BY closure.depth""",
            (entity_id, max_depth),
        ).fetchall()

        entity_ids = [r["entity_id"] for r in rows]
        relations = self._get_relations_between(entity_ids)

        return {
            "entity": center,
            "neighbors": [
                {**self.get_entity(eid), "depth": depth}
                for eid, depth in [(r["entity_id"], r["depth"]) for r in rows]
                if eid != entity_id
            ],
            "relations": relations,
        }

    def trace_decision_chain(
        self,
        start_entity_id: int,
        max_depth: int = 10,
    ) -> list[dict]:
        """Trace a decision chain by following 'decided_by' and 'references' edges.

        Returns an ordered list of entities from start back to root decisions.
        """
        rows = self._cache.execute(
            """WITH RECURSIVE chain(entity_id, depth, path) AS (
                    VALUES(?, 0, CAST(? AS TEXT))
                    UNION
                    SELECT
                        r.source_id,
                        chain.depth + 1,
                        chain.path || ',' || CAST(r.source_id AS TEXT)
                    FROM chain
                    JOIN relations r ON r.target_id = chain.entity_id
                    WHERE r.type IN ('decided_by', 'references', 'depends_on')
                      AND chain.depth < ?
                      AND ',' || chain.path || ',' NOT LIKE '%,' || CAST(r.source_id AS TEXT) || ',%'
                )
                SELECT DISTINCT e.*, chain.depth
                FROM chain
                JOIN entities e ON e.id = chain.entity_id
                ORDER BY chain.depth""",
            (start_entity_id, str(start_entity_id), max_depth),
        ).fetchall()

        results = [self._row_to_entity(r) for r in rows]
        for i, row in enumerate(rows):
            results[i]["depth"] = row["depth"]
        return results

    # ── Stats ─────────────────────────────────────────────────

    def stats(self) -> dict:
        """Return graph statistics."""
        return {
            "entities": self.entity_count(),
            "relations": self.relation_count(),
            "events": self.event_count(),
            "entity_types": self.entity_counts_by_type(),
            "failed_events": len(self.get_failed_events(limit=1000)),
        }

    # ── Helpers ───────────────────────────────────────────────

    def _get_relations_between(self, entity_ids: list[int]) -> list[dict]:
        """Get all relations where both ends are in the given ID set."""
        if not entity_ids:
            return []
        placeholders = ",".join("?" * len(entity_ids))
        rows = self._cache.execute(
            f"""SELECT * FROM relations
                WHERE source_id IN ({placeholders})
                  AND target_id IN ({placeholders})
                ORDER BY created_at DESC""",
            tuple(entity_ids) * 2,
        ).fetchall()
        return [self._row_to_relation(r) for r in rows]

    @staticmethod
    def _row_to_entity(row: Any) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "type": row["type"],
            "description": row["description"],
            "metadata": (
                json.loads(row["metadata"])
                if isinstance(row["metadata"], str) else row["metadata"]
            ),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _row_to_relation(row: Any) -> dict:
        return {
            "id": row["id"],
            "source_id": row["source_id"],
            "target_id": row["target_id"],
            "type": row["type"],
            "weight": row["weight"],
            "metadata": (
                json.loads(row["metadata"])
                if isinstance(row["metadata"], str) else row["metadata"]
            ),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _row_to_event(row: Any) -> dict:
        return {
            "id": row["id"],
            "entity_id": row["entity_id"],
            "session_id": row["session_id"],
            "event_type": row["event_type"],
            "payload": (
                json.loads(row["payload"])
                if isinstance(row["payload"], str) else row["payload"]
            ),
            "created_at": row["created_at"],
        }
