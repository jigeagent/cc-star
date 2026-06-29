"""Tests for cc-star hot.md — cross-session working-memory continuation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cc_star.memos.hot import (
    _extract_frontmatter,
    format_hot_context,
    hot_path,
    read_hot,
    write_hot,
)


class TestHotPath:
    """Test path resolution."""

    def test_hot_path_resolves(self, tmp_path):
        """Test hot.md path resolves under data dir."""
        p = hot_path(tmp_path)
        assert p.name == "hot.md"
        assert p.parent == tmp_path


class TestFrontmatter:
    """Test frontmatter extraction."""

    def test_extract_empty(self):
        """Test empty content returns no meta."""
        meta, body = _extract_frontmatter("")
        assert meta == {}
        assert body == ""

    def test_extract_no_frontmatter(self):
        """Test content without --- markers."""
        meta, body = _extract_frontmatter("just content")
        assert meta == {}
        assert body == "just content"

    def test_extract_basic(self):
        """Test basic frontmatter extraction."""
        text = "---\nkey: value\n---\nbody text"
        meta, body = _extract_frontmatter(text)
        assert meta["key"] == "value"
        assert body == "body text"

    def test_extract_multiple_fields(self):
        """Test multiple frontmatter fields."""
        text = "---\nproject: cc-star\nstatus: done\n---\nWorking on v0.7.1"
        meta, body = _extract_frontmatter(text)
        assert meta["project"] == "cc-star"
        assert meta["status"] == "done"
        assert "v0.7.1" in body


class TestReadHot:
    """Test reading hot.md."""

    def test_read_missing(self, tmp_path):
        """Test reading non-existent hot.md returns None."""
        assert read_hot(tmp_path) is None

    def test_read_empty(self, tmp_path):
        """Test reading empty hot.md returns None."""
        hot_path(tmp_path).write_text("   ", encoding="utf-8")
        assert read_hot(tmp_path) is None

    def test_read_valid(self, tmp_path):
        """Test reading valid hot.md returns body."""
        text = "---\nupdated_at: 2099-01-01T00:00:00\n---\nWorking on Phase 5"
        hot_path(tmp_path).write_text(text, encoding="utf-8")
        result = read_hot(tmp_path)
        assert result is not None
        assert "Phase 5" in result

    def test_read_stale(self, tmp_path):
        """Test reading stale hot.md (>24h) returns with staleness notice."""
        old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        text = f"---\nupdated_at: {old}\n---\nOld content"
        hot_path(tmp_path).write_text(text, encoding="utf-8")
        result = read_hot(tmp_path, max_age_hours=24)
        assert result is not None
        assert "24h" in result or "前" in result
        assert "Old content" in result


class TestWriteHot:
    """Test writing hot.md."""

    def test_write_basic(self, tmp_path):
        """Test basic hot.md write."""
        p = write_hot(tmp_path, project="cc-star", status="开发中",
                       summary="Working on hot.md")
        assert p.is_file()
        content = p.read_text(encoding="utf-8")
        assert "cc-star" in content
        assert "开发中" in content
        assert "updated_at" in content

    def test_write_preserves_created_at(self, tmp_path):
        """Test re-writing preserves original created_at."""
        p = write_hot(tmp_path, project="v1")
        c1 = p.read_text(encoding="utf-8")

        import time
        time.sleep(0.01)

        write_hot(tmp_path, project="v2")
        c2 = p.read_text(encoding="utf-8")

        # Extract created_at values
        def get_created(text: str) -> str:
            for line in text.split("\n"):
                if line.startswith("created_at:"):
                    return line.split(":", 1)[1].strip()
            return ""

        assert get_created(c1) == get_created(c2)

    def test_write_empty_body(self, tmp_path):
        """Test write with only frontmatter works."""
        p = write_hot(tmp_path, project="test")
        content = p.read_text(encoding="utf-8")
        assert "project: test" in content


class TestFormatHot:
    """Test hot.md context formatting."""

    def test_format_empty(self):
        """Test formatting empty content returns empty string."""
        assert format_hot_context("") == ""

    def test_format_basic(self):
        """Test basic context formatting."""
        result = format_hot_context("Working on Phase 5")
        assert "Previous session" in result
        assert "Phase 5" in result

    def test_format_truncation(self):
        """Test truncation at max_tokens."""
        long_text = "hello world " * 500  # ~6000 chars
        result = format_hot_context(long_text, max_tokens=100)
        assert "truncated" in result
        assert len(result) < 600
