"""
Memory promotion & lifecycle management for cc-star.

Responsibilities:
1. Cache DB size limit enforcement (delete oldest traces when over limit)
2. Native memory dedup (remove identical/redundant files)
3. Background promotion scan (promote frequently-accessed traces)

Usage:
    python -m cc_star.promote              # full maintenance run
    python -m cc_star.promote --dry-run     # preview without changes
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cc_star.cache.connection import CacheConnection
from cc_star.cache.schema import ensure_schema
from cc_star.cache.traces import TraceRepository
from cc_star.config import ConfigManager


# ── Helpers ──


def _load_config() -> dict[str, Any]:
    """Load merged config."""
    return ConfigManager().load()


def _get_cfg(key: str, default: Any = None) -> Any:
    """Get config value by dotted key path via ConfigManager."""
    cfg_mgr = ConfigManager()
    val = cfg_mgr.get(key)
    return val if val is not None else default


def _cachedb_path() -> str:
    raw = _get_cfg("storage.path", "~/.cc-star/data")
    return os.path.expanduser(os.path.join(raw, "cache.db"))


def _native_memory_path() -> str:
    raw = os.environ.get("CC_STAR_MEMORY_PATH", "") or _get_cfg("memory.memory_path", "")
    return os.path.expanduser(raw) if raw else ""


def _max_cache_mb() -> int:
    return int(os.environ.get("CC_STAR_MAX_CACHE_MB", str(_get_cfg("memory.max_cache_mb", "500"))))


def _promote_threshold() -> int:
    return int(os.environ.get("CC_STAR_PROMOTE_THRESHOLD", str(_get_cfg("memory.promote_threshold", "3"))))


# ── Cache maintenance ──


def _ensure_repo() -> tuple[CacheConnection, TraceRepository] | None:
    """Open cache db and return (conn, repo) or None on failure."""
    try:
        cache = CacheConnection(_cachedb_path())
        ensure_schema(cache)
        repo = TraceRepository(cache)
        return cache, repo
    except Exception as e:
        print(f"[promote] cache open failed: {e}", file=sys.stderr)
        return None


def enforce_cache_limit(dry_run: bool = False) -> dict[str, Any]:
    """Delete oldest traces if cache.db exceeds max_cache_mb.

    Returns summary dict.
    """
    result: dict[str, Any] = {"action": "enforce_limit", "dry_run": dry_run}

    db_path = _cachedb_path()
    if not os.path.isfile(db_path):
        result["status"] = "no_db"
        return result

    size_mb = os.path.getsize(db_path) / (1024 * 1024)
    max_mb = _max_cache_mb()
    result["size_mb"] = round(size_mb, 1)
    result["max_mb"] = max_mb

    if size_mb <= max_mb:
        result["status"] = "under_limit"
        return result

    # Need to prune — target 80% of limit
    target_mb = max_mb * 0.8
    conn_repo = _ensure_repo()
    if conn_repo is None:
        result["status"] = "error"
        return result

    cache, repo = conn_repo

    try:
        total = repo.count()
        result["total_traces"] = total

        # Delete in batches until under target
        deleted = 0
        while os.path.getsize(db_path) / (1024 * 1024) > target_mb:
            if dry_run:
                break
            # Delete oldest 100 traces
            oldest = repo.list_recent(limit=100)
            if not oldest or len(oldest) < 2:
                break
            oldest_ts = oldest[-1].created_at
            if not oldest_ts:
                break
            count = repo.delete_old(oldest_ts)
            if count == 0:
                break
            deleted += count

        result["deleted"] = deleted if not dry_run else "dry_run"
        result["remaining_mb"] = round(os.path.getsize(db_path) / (1024 * 1024), 1)

    except Exception as e:
        result["status"] = f"error: {e}"
    finally:
        cache.close_all()

    result["status"] = "ok"
    return result


# ── Native memory dedup ──


def dedup_native_memory(dry_run: bool = False) -> dict[str, Any]:
    """Remove duplicate/redundant native memory files.

    Uses content hash comparison to find near-identical files.
    """
    result: dict[str, Any] = {"action": "dedup_native", "dry_run": dry_run}
    mem_path = _native_memory_path()
    if not mem_path or not Path(mem_path).is_dir():
        result["status"] = "no_native_memory"
        return result

    files = sorted(Path(mem_path).glob("*.md"))
    result["total_files"] = len(files)

    # Group by content hash (first 32 chars of SHA256 of content)
    seen_hashes: dict[str, list[Path]] = {}
    for fpath in files:
        try:
            content = fpath.read_text(encoding="utf-8")
            import hashlib
            h = hashlib.sha256(content.encode("utf-8")).hexdigest()[:32]
            seen_hashes.setdefault(h, []).append(fpath)
        except OSError:
            continue

    # Keep the first (oldest) file per hash, remove rest
    removed: list[str] = []
    kept: list[str] = []
    for h, dupes in seen_hashes.items():
        if len(dupes) <= 1:
            kept.append(dupes[0].name)
            continue
        # Sort by name (which often includes date), keep first
        dupes.sort()
        kept.append(dupes[0].name)
        for f in dupes[1:]:
            removed.append(f.name)
            if not dry_run:
                try:
                    # Rename to .bak instead of delete, safety net
                    bak = f.with_suffix(f.suffix + ".bak")
                    if not bak.is_file():
                        f.rename(bak)
                except OSError:
                    pass

    result["kept"] = len(kept)
    result["removed"] = removed if not dry_run else f"dry_run ({len(removed)} would remove)"
    result["status"] = "ok"
    return result


# ── Promote hot traces → native memory ──


def promote_hot_traces(dry_run: bool = False) -> dict[str, Any]:
    """Scan cache.db for frequently-accessed traces and promote to native memory.

    Uses simple heuristic: traces with high reward or frequent keywords.
    """
    result: dict[str, Any] = {"action": "promote_hot", "dry_run": dry_run}
    mem_path = _native_memory_path()
    if not mem_path:
        result["status"] = "no_native_memory"
        return result

    conn_repo = _ensure_repo()
    if conn_repo is None:
        result["status"] = "error"
        return result

    cache, repo = conn_repo
    promoted: list[str] = []

    try:
        # Get recent traces with highest reward
        all_high = repo.search_fts("archived OR decision OR 架构 OR 决策 OR 协议 OR 方案", limit=20)
        all_high += repo.search_fts("设计 OR 规则 OR 配置 OR 总结 OR 结论 OR 记忆", limit=20)

        # Dedup by id
        seen_ids = set()
        candidates = []
        for t in all_high:
            if t.id not in seen_ids:
                seen_ids.add(t.id)
                candidates.append(t)

        # Score candidates: reward + content length
        def _score(t: Any) -> float:
            s = float(t.reward or 0)
            content = (t.user_content or "") + " " + (t.assistant_content or "")
            s += min(len(content) / 500, 1.0)  # bonus for longer content
            return s

        candidates.sort(key=_score, reverse=True)
        candidates = candidates[:10]

        # Check cooldown
        promote_log = Path(_cachedb_path()).parent / "promote_log.jsonl"
        now = datetime.now(timezone.utc)
        promoted_topics = set()
        if promote_log.is_file():
            for line in promote_log.read_text(encoding="utf-8").strip().split("\n"):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    promoted_topics.add(rec.get("topic", ""))
                except (json.JSONDecodeError, KeyError):
                    pass

        native_dir = Path(mem_path)
        native_dir.mkdir(parents=True, exist_ok=True)

        for t in candidates:
            combined = (t.user_content or "") + " " + (t.assistant_content or "")
            topic = (combined or "")[:40].strip()
            if not topic or topic in promoted_topics:
                continue

            safe = re.sub(r'[^\w一-鿿\-]', '_', topic)[:30].strip("_").lower()
            if not safe:
                continue

            fpath = native_dir / f"promoted_{safe}.md"
            if fpath.is_file():
                continue

            md = _render_hot_memory(t.user_content or "", t.assistant_content or "", topic)
            promoted.append(fpath.name)

            if not dry_run:
                fpath.write_text(md, encoding="utf-8")
                # Log promotion
                promote_log.parent.mkdir(parents=True, exist_ok=True)
                with open(str(promote_log), "a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "topic": topic,
                        "filepath": str(fpath),
                        "promoted_at": now.isoformat(),
                        "source_trace_id": t.id,
                    }, ensure_ascii=False) + "\n")

        result["promoted"] = promoted if not dry_run else f"dry_run ({len(promoted)} would promote)"
        result["count"] = len(promoted)
        result["status"] = "ok"

    except Exception as e:
        result["status"] = f"error: {e}"
    finally:
        cache.close_all()

    return result


def _render_hot_memory(user: str, assistant: str, topic: str) -> str:
    """Render a trace as a native memory markdown file."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        f"# {topic[:60]}",
        "",
        f"> 自动晋升 · {today}",
        "",
    ]
    if user:
        lines.append("## 上下文")
        lines.append("")
        lines.append(user[:500])
        lines.append("")
    if assistant:
        lines.append("## 输出")
        lines.append("")
        lines.append(assistant[:1000])
        lines.append("")
    lines.append("---")
    lines.append(f"_promoted by cc-star · {today}_")
    return "\n".join(lines)


# ── Maintenance runner ──


def run_maintenance(dry_run: bool = False) -> dict[str, Any]:
    """Run full maintenance cycle."""
    results = {
        "cache_limit": enforce_cache_limit(dry_run=dry_run),
        "native_dedup": dedup_native_memory(dry_run=dry_run),
        "hot_promote": promote_hot_traces(dry_run=dry_run),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return results


def main() -> None:
    """CLI entry point."""
    dry_run = "--dry-run" in sys.argv or "-n" in sys.argv

    if "--cache-only" in sys.argv:
        results = enforce_cache_limit(dry_run=dry_run)
    elif "--dedup-only" in sys.argv:
        results = dedup_native_memory(dry_run=dry_run)
    elif "--promote-only" in sys.argv:
        results = promote_hot_traces(dry_run=dry_run)
    else:
        results = run_maintenance(dry_run=dry_run)

    json.dump(results, sys.stdout, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
