"""Trace repository — local SQLite CRUD for traces with batch operations."""

from __future__ import annotations

import json
from typing import Any, Optional

from cc_star.cache.connection import CacheConnection
from cc_star.cache.schema import ensure_schema
from cc_star.memos.types import TraceRow


class TraceRepository:
    """Persist and query traces locally."""

    def __init__(self, cache: CacheConnection):
        self._cache = cache
        ensure_schema(cache)
        # Prepared statements
        self._insert_sql = (
            "INSERT OR REPLACE INTO traces "
            "(id, session_id, turn_index, user_content, assistant_content, "
            "embedding, reward, tags, metadata, created_at, synced) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )

    def insert(self, trace: TraceRow) -> None:
        """Insert a single trace into local cache."""
        self._cache.execute(
            self._insert_sql,
            self._trace_to_row(trace),
        )

    def insert_batch(self, traces: list[TraceRow]) -> None:
        """Batch insert multiple traces (faster than individual inserts)."""
        rows = [self._trace_to_row(t) for t in traces]
        self._cache.executemany(self._insert_sql, rows)

    def get(self, trace_id: str) -> Optional[TraceRow]:
        """Get a trace by ID."""
        row = self._cache.execute(
            "SELECT * FROM traces WHERE id = ?", (trace_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_trace(row)

    def list_by_session(self, session_id: str, limit: int = 50) -> list[TraceRow]:
        """List traces for a session, ordered by turn index."""
        rows = self._cache.execute(
            "SELECT * FROM traces WHERE session_id = ? ORDER BY turn_index ASC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [self._row_to_trace(r) for r in rows]

    def search_fts(self, query: str, limit: int = 8) -> list[TraceRow]:
        """Full-text search on traces using FTS5."""
        # Strip surrogate characters and control chars that crash FTS5
        query = query.encode("utf-8", "surrogatepass").decode("utf-8", "replace")
        query = "".join(c for c in query if c.isprintable() or c in (" ", "\n", "\t"))
        safe = query.replace('"', '""')
        rows = self._cache.execute(
            """
            SELECT t.* FROM traces t
            JOIN traces_fts fts ON t.rowid = fts.rowid
            WHERE traces_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (safe, limit),
        ).fetchall()
        return [self._row_to_trace(r) for r in rows]

    def list_recent(self, limit: int = 20) -> list[TraceRow]:
        """List most recent traces."""
        rows = self._cache.execute(
            "SELECT * FROM traces ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_trace(r) for r in rows]

    def count(self) -> int:
        """Total trace count."""
        row = self._cache.execute(
            "SELECT COUNT(*) as cnt FROM traces"
        ).fetchone()
        return row["cnt"] if row else 0

    def mark_synced(self, trace_id: str) -> None:
        """Mark a trace as synced to OpenViking."""
        self._cache.execute(
            "UPDATE traces SET synced = 1 WHERE id = ?", (trace_id,)
        )

    def mark_synced_batch(self, trace_ids: list[str]) -> None:
        """Batch mark multiple traces as synced."""
        rows = [(tid,) for tid in trace_ids]
        self._cache.executemany(
            "UPDATE traces SET synced = 1 WHERE id = ?", rows,
        )

    def get_unsynced(self, limit: int = 50) -> list[TraceRow]:
        """Get traces that haven't been synced to OpenViking yet."""
        rows = self._cache.execute(
            "SELECT * FROM traces WHERE synced = 0 ORDER BY created_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_trace(r) for r in rows]

    def delete_old(self, before_timestamp: str) -> int:
        """Delete traces older than a timestamp. Returns count deleted."""
        cursor = self._cache.execute(
            "DELETE FROM traces WHERE created_at < ?", (before_timestamp,)
        )
        return cursor.rowcount

    def get_all_embeddings(self, limit: int = 1000) -> list[tuple[str, list[float]]]:
        """Get all (id, embedding) pairs for bulk similarity search."""
        rows = self._cache.execute(
            "SELECT id, embedding FROM traces WHERE embedding IS NOT NULL LIMIT ?",
            (limit,),
        ).fetchall()
        result = []
        for r in rows:
            if r["embedding"]:
                try:
                    emb = json.loads(r["embedding"])
                    if emb:
                        result.append((r["id"], emb))
                except (json.JSONDecodeError, TypeError):
                    pass
        return result

    @staticmethod
    def _trace_to_row(trace: TraceRow) -> tuple:
        return (
            trace.id,
            trace.session_id,
            trace.turn_index,
            trace.user_content,
            trace.assistant_content,
            json.dumps(trace.embedding) if trace.embedding else None,
            trace.reward,
            json.dumps(trace.tags, ensure_ascii=False),
            json.dumps(trace.metadata, ensure_ascii=False, default=str),
            trace.created_at,
            0,
        )

    @staticmethod
    def _row_to_trace(row: Any) -> TraceRow:
        return TraceRow(
            id=row["id"],
            session_id=row["session_id"],
            turn_index=row["turn_index"],
            user_content=row["user_content"],
            assistant_content=row["assistant_content"],
            embedding=json.loads(row["embedding"]) if row["embedding"] else None,
            reward=row["reward"],
            tags=json.loads(row["tags"]) if isinstance(row["tags"], str) else [],
            metadata=json.loads(row["metadata"]) if isinstance(row["metadata"], str) else {},
            created_at=row["created_at"],
        )
