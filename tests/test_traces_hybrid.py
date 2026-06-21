"""Tests for hybrid search in TraceRepository."""

import json
import os
import tempfile
from cc_star.cache.connection import CacheConnection
from cc_star.cache.traces import TraceRepository
from cc_star.cache.vector import EmbeddingEngine
from cc_star.memos.types import TraceRow


def _make_cache(db_path: str) -> CacheConnection:
    cache = CacheConnection(db_path)
    from cc_star.cache.schema import ensure_schema
    ensure_schema(cache)
    return cache


def _seed_traces(repo: TraceRepository):
    """Insert sample traces for testing."""
    traces = [
        TraceRow(
            id=f"t{i}",
            session_id="s1",
            turn_index=i,
            user_content=f"user message {i}",
            assistant_content=f"关于 Python 编程的讨论内容 {i}测试中文",
            embedding=None,
            reward=0.0,
            tags=[],
            metadata={},
            created_at=f"2026-01-0{i+1}T00:00:00Z",
        )
        for i in range(3)
    ]
    for t in traces:
        repo.insert(t)


def test_search_hybrid_returns_results():
    db_path = tempfile.mktemp(suffix=".db")
    try:
        cache = _make_cache(db_path)
        repo = TraceRepository(cache)
        _seed_traces(repo)

        results = repo.search_hybrid("Python", limit=5)
        assert len(results) > 0
        assert any("Python" in (r.assistant_content or "") for r in results)
    finally:
        cache.close()
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_search_hybrid_fallback_when_no_embedding():
    """Should degrade to FTS5 when embeddings are missing."""
    db_path = tempfile.mktemp(suffix=".db")
    try:
        cache = _make_cache(db_path)
        repo = TraceRepository(cache)
        _seed_traces(repo)

        # Without backfill, embeddings are NULL — should still return FTS5 results
        results = repo.search_hybrid("message", limit=5)
        assert len(results) > 0
    finally:
        cache.close()
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_update_embedding():
    db_path = tempfile.mktemp(suffix=".db")
    try:
        cache = _make_cache(db_path)
        repo = TraceRepository(cache)
        _seed_traces(repo)

        emb = [0.1] * 384
        repo.update_embedding("t1", emb)
        t = repo.get("t1")
        assert t is not None
        assert t.embedding == emb
    finally:
        cache.close()
        if os.path.exists(db_path):
            os.unlink(db_path)
