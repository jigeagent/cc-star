"""HierarchicalStore — SQLite persistence for the H-MEM four-layer hierarchy.

Schema:
    hierarchy_nodes — unified table for all four layers with self-referencing parent
    feedback_log    — user feedback events for memory weight regulation
    hmem_config     — H-MEM module configuration key-value store
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from cc_star.cache.connection import CacheConnection
from cc_star.hmem.models import (
    Layer,
    HierarchyNode,
    FeedbackLog,
    FeedbackType,
)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS hierarchy_nodes (
    id              TEXT PRIMARY KEY,
    layer           TEXT NOT NULL,
    parent_id       TEXT,
    domain_id       TEXT,
    self_index      INTEGER NOT NULL DEFAULT 0,
    sub_indices     TEXT DEFAULT '[]',
    title           TEXT DEFAULT '',
    summary         TEXT DEFAULT '',
    content         TEXT DEFAULT '',
    embedding       BLOB,
    weight          REAL NOT NULL DEFAULT 1.0,
    access_count    INTEGER NOT NULL DEFAULT 0,
    last_accessed_at TEXT DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    approval_count  INTEGER NOT NULL DEFAULT 0,
    rebuttal_count  INTEGER NOT NULL DEFAULT 0,
    source_trace_id TEXT,
    metadata        TEXT DEFAULT '{}',
    FOREIGN KEY (parent_id) REFERENCES hierarchy_nodes(id)
);

CREATE INDEX IF NOT EXISTS idx_hn_layer ON hierarchy_nodes(layer);
CREATE INDEX IF NOT EXISTS idx_hn_parent ON hierarchy_nodes(parent_id);
CREATE INDEX IF NOT EXISTS idx_hn_domain ON hierarchy_nodes(domain_id);
CREATE INDEX IF NOT EXISTS idx_hn_self_idx ON hierarchy_nodes(layer, self_index);

CREATE TABLE IF NOT EXISTS feedback_log (
    id              TEXT PRIMARY KEY,
    node_id         TEXT NOT NULL,
    feedback_type   TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    user_message    TEXT DEFAULT '',
    llm_analysis    TEXT DEFAULT '',
    weight_before   REAL DEFAULT 1.0,
    weight_after    REAL DEFAULT 1.0,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (node_id) REFERENCES hierarchy_nodes(id)
);

CREATE INDEX IF NOT EXISTS idx_fl_node ON feedback_log(node_id);
CREATE INDEX IF NOT EXISTS idx_fl_session ON feedback_log(session_id);

CREATE TABLE IF NOT EXISTS hmem_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class HierarchicalStore:
    """Persistent store for the H-MEM hierarchy."""

    def __init__(self, cache: CacheConnection):
        self._cache = cache
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._cache.conn.executescript(SCHEMA_SQL)
        self._cache.conn.commit()

    # ── Node CRUD ──

    def insert_node(self, node: HierarchyNode) -> None:
        """Insert a single hierarchy node."""
        self._cache.execute(
            """INSERT OR REPLACE INTO hierarchy_nodes
            (id, layer, parent_id, domain_id, self_index, sub_indices,
             title, summary, content, embedding,
             weight, access_count, last_accessed_at,
             created_at, updated_at,
             approval_count, rebuttal_count,
             source_trace_id, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            node.to_row(),
        )

    def insert_nodes_batch(self, nodes: list[HierarchyNode]) -> None:
        """Batch insert multiple nodes."""
        rows = [n.to_row() for n in nodes]
        self._cache.executemany(
            """INSERT OR REPLACE INTO hierarchy_nodes
            (id, layer, parent_id, domain_id, self_index, sub_indices,
             title, summary, content, embedding,
             weight, access_count, last_accessed_at,
             created_at, updated_at,
             approval_count, rebuttal_count,
             source_trace_id, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )

    def get_node(self, node_id: str) -> Optional[HierarchyNode]:
        """Get a single node by ID."""
        row = self._cache.execute(
            "SELECT * FROM hierarchy_nodes WHERE id = ?", (node_id,)
        ).fetchone()
        return HierarchyNode.from_row(row) if row else None

    def get_children(self, parent_id: str) -> list[HierarchyNode]:
        """Get all direct children of a node."""
        rows = self._cache.execute(
            "SELECT * FROM hierarchy_nodes WHERE parent_id = ? ORDER BY self_index",
            (parent_id,),
        ).fetchall()
        return [HierarchyNode.from_row(r) for r in rows]

    def get_nodes_by_indices(self, layer: str, indices: list[int]) -> list[HierarchyNode]:
        """Get nodes in a specific layer by their self_index values."""
        if not indices:
            return []
        placeholders = ",".join("?" for _ in indices)
        rows = self._cache.execute(
            f"SELECT * FROM hierarchy_nodes WHERE layer = ? AND self_index IN ({placeholders})",
            (layer, *indices),
        ).fetchall()
        return [HierarchyNode.from_row(r) for r in rows]

    def get_nodes_by_ids(self, ids: list[str]) -> list[HierarchyNode]:
        """Get multiple nodes by their IDs."""
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = self._cache.execute(
            f"SELECT * FROM hierarchy_nodes WHERE id IN ({placeholders})", ids
        ).fetchall()
        return [HierarchyNode.from_row(r) for r in rows]

    def get_layer_nodes(self, layer: str) -> list[HierarchyNode]:
        """Get all nodes in a given layer."""
        rows = self._cache.execute(
            "SELECT * FROM hierarchy_nodes WHERE layer = ? ORDER BY self_index",
            (layer,),
        ).fetchall()
        return [HierarchyNode.from_row(r) for r in rows]

    def get_domain_roots(self) -> list[HierarchyNode]:
        """Get all root domain nodes."""
        rows = self._cache.execute(
            "SELECT * FROM hierarchy_nodes WHERE layer = 'domain' AND parent_id IS NULL"
        ).fetchall()
        return [HierarchyNode.from_row(r) for r in rows]

    def update_node(self, node: HierarchyNode) -> None:
        """Update mutable fields of an existing node."""
        node.updated_at = datetime.now(timezone.utc).isoformat()
        self._cache.execute(
            """UPDATE hierarchy_nodes SET
                parent_id = ?, domain_id = ?, self_index = ?, sub_indices = ?,
                title = ?, summary = ?, content = ?, embedding = ?,
                weight = ?, access_count = ?, last_accessed_at = ?,
                updated_at = ?,
                approval_count = ?, rebuttal_count = ?,
                source_trace_id = ?, metadata = ?
            WHERE id = ?""",
            (
                node.parent_id, node.domain_id, node.self_index,
                json.dumps(node.sub_indices),
                node.title, node.summary, node.content,
                json.dumps(node.embedding) if node.embedding is not None else None,
                node.weight, node.access_count, node.last_accessed_at,
                node.updated_at,
                node.approval_count, node.rebuttal_count,
                node.source_trace_id,
                json.dumps(node.metadata, ensure_ascii=False, default=str),
                node.id,
            ),
        )

    def update_weight(self, node_id: str, weight: float) -> None:
        """Quick weight update without loading the full node."""
        now = datetime.now(timezone.utc).isoformat()
        self._cache.execute(
            "UPDATE hierarchy_nodes SET weight = ?, updated_at = ? WHERE id = ?",
            (weight, now, node_id),
        )

    def delete_node(self, node_id: str) -> None:
        """Delete a node and its children."""
        children = self.get_children(node_id)
        for child in children:
            self.delete_node(child.id)
        self._cache.execute(
            "DELETE FROM hierarchy_nodes WHERE id = ?", (node_id,)
        )

    def count_nodes(self, layer: str | None = None) -> int:
        """Count nodes, optionally filtered by layer."""
        if layer:
            row = self._cache.execute(
                "SELECT COUNT(*) FROM hierarchy_nodes WHERE layer = ?", (layer,)
            ).fetchone()
        else:
            row = self._cache.execute(
                "SELECT COUNT(*) FROM hierarchy_nodes"
            ).fetchone()
        return row[0] if row else 0

    def count_dirty(self) -> int:
        """Count nodes with embedding IS NULL (need backfill)."""
        row = self._cache.execute(
            "SELECT COUNT(*) FROM hierarchy_nodes WHERE embedding IS NULL"
        ).fetchone()
        return row[0] if row else 0

    # ── Feedback CRUD ──

    def insert_feedback(self, log: FeedbackLog) -> None:
        """Record a feedback event."""
        self._cache.execute(
            """INSERT INTO feedback_log
            (id, node_id, feedback_type, session_id,
             user_message, llm_analysis,
             weight_before, weight_after, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            log.to_row(),
        )

    def get_feedback_for_node(self, node_id: str, limit: int = 50) -> list[FeedbackLog]:
        """Get feedback history for a node."""
        rows = self._cache.execute(
            "SELECT * FROM feedback_log WHERE node_id = ? ORDER BY created_at DESC LIMIT ?",
            (node_id, limit),
        ).fetchall()
        return [FeedbackLog.from_row(r) for r in rows]

    def get_feedback_for_session(self, session_id: str) -> list[FeedbackLog]:
        """Get all feedback events for a session."""
        rows = self._cache.execute(
            "SELECT * FROM feedback_log WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        ).fetchall()
        return [FeedbackLog.from_row(r) for r in rows]

    # ── Config (key-value for H-MEM module settings) ──

    def get_config(self, key: str, default: Any = None) -> Any:
        """Get a config value from hmem_config table."""
        row = self._cache.execute(
            "SELECT value FROM hmem_config WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return row["value"]

    def set_config(self, key: str, value: Any) -> None:
        """Set a config value (JSON-serialized)."""
        self._cache.execute(
            "INSERT OR REPLACE INTO hmem_config (key, value) VALUES (?, ?)",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    # ── Statistics ──

    def stats(self) -> dict[str, Any]:
        """Return hierarchy statistics."""
        return {
            "domains": self.count_nodes(Layer.DOMAIN),
            "categories": self.count_nodes(Layer.CATEGORY),
            "traces": self.count_nodes(Layer.TRACE),
            "episodes": self.count_nodes(Layer.EPISODE),
            "total": self.count_nodes(),
            "dirty_embeddings": self.count_dirty(),
            "feedback_events": self._count_feedback(),
        }

    def _count_feedback(self) -> int:
        row = self._cache.execute("SELECT COUNT(*) FROM feedback_log").fetchone()
        return row[0] if row else 0

    # ── Weight decay batch operations ──

    def get_nodes_needing_decay(self, interval_hours: float = 4.0) -> list[HierarchyNode]:
        """Get leaf nodes (episode layer) accessed more than interval_hours ago."""
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=interval_hours)).isoformat()
        rows = self._cache.execute(
            """SELECT * FROM hierarchy_nodes
            WHERE layer = 'episode'
              AND (last_accessed_at = '' OR last_accessed_at < ?)
            ORDER BY last_accessed_at ASC
            LIMIT 500""",
            (cutoff,),
        ).fetchall()
        return [HierarchyNode.from_row(r) for r in rows]

    def batch_update_weights(
        self, updates: list[tuple[float, int, str, str]]
    ) -> None:
        """Batch update (weight, access_count, last_accessed_at, updated_at) by id."""
        self._cache.executemany(
            """UPDATE hierarchy_nodes
            SET weight = ?, access_count = ?, last_accessed_at = ?, updated_at = ?
            WHERE id = ?""",
            updates,
        )

    def close(self) -> None:
        self._cache.close()
