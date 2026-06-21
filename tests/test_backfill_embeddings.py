"""Tests for embedding backfill."""

import os, tempfile, time
from pathlib import Path
from cc_star.cache.connection import CacheConnection
from cc_star.cache.schema import ensure_schema
from cc_star.cache.traces import TraceRepository
from cc_star.memos.types import TraceRow


def _seed_unembedded(repo: TraceRepository):
    for i in range(5):
        repo.insert(TraceRow(
            id=f"be_{i}", session_id="s1", turn_index=i,
            user_content=f"test {i}", assistant_content=f"response {i}",
            embedding=None, reward=0.0, tags=[], metadata={},
            created_at="2026-01-01T00:00:00Z",
        ))


def _cleanup_path(db_path: str):
    """Remove db file and WAL/SHM artifacts."""
    for suffix in ("", "-wal", "-shm"):
        p = db_path + suffix
        try:
            os.unlink(p)
        except OSError:
            pass


def test_backfill_embeddings_idempotent():
    # Use a temp directory so we control the path fully
    tmp_dir = tempfile.mkdtemp()
    db_path = os.path.join(tmp_dir, "test_cache.db")
    try:
        cache = CacheConnection(db_path)
        ensure_schema(cache)
        repo = TraceRepository(cache)
        _seed_unembedded(repo)

        from cc_star.promote import backfill_embeddings
        result = backfill_embeddings(cache_path=db_path)
        assert result["processed"] == 5, f"expected 5 processed, got {result}"
        assert result["total"] == 5, f"expected 5 total, got {result}"

        # Second run: nothing to backfill (idempotent)
        result2 = backfill_embeddings(cache_path=db_path)
        assert result2["processed"] == 0, f"expected 0 on second run, got {result2}"
    finally:
        _cleanup_path(db_path)
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass
