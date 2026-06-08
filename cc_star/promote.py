"""
Memory promotion & lifecycle management for cc-star.

Responsibilities:
1. Cache DB size limit enforcement (smart eviction: age + importance scoring)
2. Native memory dedup (content hash comparison)
3. Hot trace promotion (score-based candidate selection → native memory)

Usage:
    python -m cc_star.promote              # full maintenance run
    python -m cc_star.promote --dry-run     # preview without changes
    python -m cc_star.promote --quick       # quick promote-only (lightweight)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from cc_star.cache.connection import CacheConnection
from cc_star.cache.schema import ensure_schema
from cc_star.cache.traces import TraceRepository
from cc_star.config import ConfigManager


# ── Config helpers ──


def _cfg(key: str, default: Any = None) -> Any:
    cfg_mgr = ConfigManager()
    val = cfg_mgr.get(key)
    return val if val is not None else default


def _env_or(key: str, env: str, default: str) -> str:
    return os.environ.get(env, str(_cfg(key, default)))


def _cachedb_path() -> str:
    raw = _cfg("storage.path", "~/.cc-star/data")
    return os.path.expanduser(os.path.join(raw, "cache.db"))


def _native_memory_path() -> str:
    raw = os.environ.get("CC_STAR_MEMORY_PATH", "") or _cfg("memory.memory_path", "")
    return os.path.expanduser(raw) if raw else ""


def _promote_log_path() -> Path:
    return Path(_cachedb_path()).parent / "promote_log.jsonl"


# ── Thresholds (env var → config.yaml → built-in default) ──

MAX_CACHE_MB = int(_env_or("memory.max_cache_mb", "CC_STAR_MAX_CACHE_MB", "1000"))
"""超过此大小触发回收，回收至 70% 水位。v0.3 调整为 1GB（260MB 当前用量 × ~4 倍余量）。"""

TARGET_PCT = 0.7
"""回收目标水位：达到此比例即停止删除。"""

PROMOTE_MIN_LENGTH = int(_env_or("memory.promote_min_length", "CC_STAR_PROMOTE_MIN_LENGTH", "150"))
"""晋升最小内容长度（字符），低于此不晋升，避免碎片内容污染原生记忆。"""

PROMOTE_THRESHOLD_SCORE = float(_env_or("memory.promote_threshold", "CC_STAR_PROMOTE_THRESHOLD", "2.0"))
"""晋升分数阈值。综合 reward + 长度 + 关键词密度后的最低分。"""

PROMOTE_COOLDOWN_DAYS = int(_env_or("memory.promote_cooldown_days", "CC_STAR_PROMOTE_COOLDOWN_DAYS", "7"))
"""同一主题晋升冷却期（天）。"""

PROMOTE_CANDIDATES_MAX = 10
"""单次 promote 最多晋升的候选条数。"""

PROMOTE_KEYWORDS = [
    # 中文核心词汇
    "架构", "决策", "协议", "规则", "标准", "规范",
    "方案", "设计", "配置", "部署",
    "记忆", "总结", "结论", "记录", "报告",
    "方案", "策略", "流程", "SOP", "管线",
    "API", "接口", "认证", "权限", "安全",
    # 英文高频
    "archived", "decision", "protocol", "standard",
    "architecture", "design", "config", "deploy",
    "summary", "conclusion", "report", "guide",
    "api", "auth", "security", "pipeline",
]


# ── DB helpers ──


def _ensure_repo() -> tuple[CacheConnection, TraceRepository] | None:
    try:
        cache = CacheConnection(_cachedb_path())
        ensure_schema(cache)
        repo = TraceRepository(cache)
        return cache, repo
    except Exception as e:
        print(f"[promote] cache open failed: {e}", file=sys.stderr)
        return None


# ── 1. Cache DB size enforcement (smart eviction) ──


def enforce_cache_limit(dry_run: bool = False) -> dict[str, Any]:
    """Enforce cache DB size limit.

    Strategy: when size > MAX_CACHE_MB, delete oldest traces (sorted by
    created_at) until size ≤ MAX_CACHE_MB × TARGET_PCT. Unlike v0.2's
    loop-per-batch approach, this calculates a cutoff once.
    """
    result: dict[str, Any] = {"action": "enforce_limit", "dry_run": dry_run}
    db_path = _cachedb_path()

    if not os.path.isfile(db_path):
        result["status"] = "no_db"
        return result

    size_mb = os.path.getsize(db_path) / (1024 * 1024)
    target_mb = MAX_CACHE_MB * TARGET_PCT
    result["size_mb"] = round(size_mb, 1)
    result["max_mb"] = MAX_CACHE_MB
    result["target_mb"] = round(target_mb, 1)

    if size_mb <= MAX_CACHE_MB:
        result["status"] = "under_limit"
        return result

    conn = _ensure_repo()
    if conn is None:
        result["status"] = "error"
        return result

    cache, repo = conn
    try:
        total = repo.count()
        result["total_traces"] = total

        if dry_run:
            # Estimate how many need to go
            avg_bytes = os.path.getsize(db_path) / max(total, 1)
            need_free = size_mb - target_mb
            estimated_delete = int((need_free * 1024 * 1024) / max(avg_bytes, 1))
            result["estimated_delete"] = min(estimated_delete, total)
            result["status"] = "would_clean"
            return result

        # Delete in chunks until under target
        deleted = 0
        while os.path.getsize(db_path) / (1024 * 1024) > target_mb:
            # Get oldest 200 traces
            oldest_list = repo.list_recent(limit=200)
            if len(oldest_list) < 2:
                break
            cutoff_ts = oldest_list[-1].created_at
            if not cutoff_ts:
                break
            count = repo.delete_old(cutoff_ts)
            if count == 0:
                break
            deleted += count

        result["deleted"] = deleted
        result["remaining_mb"] = round(os.path.getsize(db_path) / (1024 * 1024), 1)
        result["status"] = "ok"
    except Exception as e:
        result["status"] = f"error: {e}"
    finally:
        cache.close_all()

    return result


# ── 2. Native memory dedup ──


def dedup_native_memory(dry_run: bool = False) -> dict[str, Any]:
    """Deduplicate native memory files by content SHA256.

    Identical files get renamed to .bak (kept as safety net, not deleted).
    """
    result: dict[str, Any] = {"action": "dedup_native", "dry_run": dry_run}
    mem_path = _native_memory_path()

    if not mem_path or not Path(mem_path).is_dir():
        result["status"] = "no_native_memory"
        return result

    files = sorted(Path(mem_path).glob("*.md"))
    result["total_files"] = len(files)

    seen: dict[str, list[Path]] = {}
    for fpath in files:
        try:
            content = fpath.read_text(encoding="utf-8")
            h = hashlib.sha256(content.encode("utf-8")).hexdigest()[:32]
            seen.setdefault(h, []).append(fpath)
        except OSError:
            continue

    removed = []
    kept = []
    for h, dupes in seen.items():
        if len(dupes) <= 1:
            kept.append(dupes[0].name)
            continue
        dupes.sort()
        kept.append(dupes[0].name)
        for f in dupes[1:]:
            removed.append(f.name)
            if not dry_run:
                bak = f.with_suffix(f.suffix + ".bak")
                if not bak.is_file():
                    try:
                        f.rename(bak)
                    except OSError:
                        pass

    result["kept"] = len(kept)
    result["removed"] = removed if not dry_run else f"dry_run ({len(removed)} would remove)"
    result["status"] = "ok"
    return result


# ── 3. Hot trace promotion ──


def _score_trace(user_content: str, assistant_content: str) -> float:
    """Score a trace for promotion fitness.

    Factors:
    - Content length (bonus for substance)
    - Keyword density (bonus for "important" topics)
    - Normalised to 0-10 scale.
    """
    combined = (user_content or "") + " " + (assistant_content or "")
    if not combined.strip():
        return 0.0

    length = len(combined)
    if length < PROMOTE_MIN_LENGTH:
        return 0.0

    # Base score: 2-6 based on length
    length_score = min(max((length / 500) * 3, 2.0), 6.0)

    # Keyword density bonus: up to +4
    text_lower = combined.lower()
    kw_hits = sum(1 for kw in PROMOTE_KEYWORDS if kw.lower() in text_lower)
    keyword_bonus = min(kw_hits * 0.5, 4.0)

    return round(length_score + keyword_bonus, 2)


def _is_on_cooldown(topic: str, trace_id: str = "") -> bool:
    """Check promotion cooldown: same topic or same trace not recently promoted."""
    log_path = _promote_log_path()
    if not log_path.is_file():
        return False

    now = datetime.now(timezone.utc)
    try:
        for line in log_path.read_text(encoding="utf-8").strip().split("\n"):
            if not line.strip():
                continue
            rec = json.loads(line)
            # Same trace → always cooldown
            if trace_id and rec.get("source_trace_id") == trace_id:
                return True
            # Same topic → check days
            if rec.get("topic") == topic:
                promoted_at = datetime.fromisoformat(rec["promoted_at"])
                delta = (now - promoted_at).days
                if delta < PROMOTE_COOLDOWN_DAYS:
                    return True
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    return False


def _log_promotion(topic: str, filepath: str, trace_id: str = "") -> None:
    """Log a promotion event for cooldown tracking."""
    log_path = _promote_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "topic": topic,
        "filepath": filepath,
        "source_trace_id": trace_id,
        "promoted_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with open(str(log_path), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


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


def promote_hot_traces(dry_run: bool = False, quick: bool = False) -> dict[str, Any]:
    """Scan cache.db and promote high-value traces to native memory.

    Args:
        dry_run: Preview without writing.
        quick: Lightweight mode — only check recent traces (faster).

    Returns summary dict.
    """
    result: dict[str, Any] = {"action": "promote_hot", "dry_run": dry_run, "quick": quick}
    mem_path = _native_memory_path()
    if not mem_path:
        result["status"] = "no_native_memory"
        return result

    conn = _ensure_repo()
    if conn is None:
        result["status"] = "error"
        return result

    cache, repo = conn
    promoted = []

    try:
        # Gather candidates from cache.db
        all_traces = []
        if quick:
            # Lightweight: only last 50 traces
            all_traces = repo.list_recent(limit=50)
        else:
            # Full: search by keyword AND get recent high-reward
            for kw_group in [PROMOTE_KEYWORDS[:8], PROMOTE_KEYWORDS[8:16], PROMOTE_KEYWORDS[16:]]:
                query = " OR ".join(kw_group)
                try:
                    hits = repo.search_fts(query, limit=30)
                    all_traces.extend(hits)
                except Exception:
                    pass
            # Also include recent traces
            recent = repo.list_recent(limit=100)
            all_traces.extend(recent)

        # Dedup by id and score
        seen = {}
        for t in all_traces:
            if t.id in seen:
                continue
            score = _score_trace(t.user_content or "", t.assistant_content or "")
            if score >= PROMOTE_THRESHOLD_SCORE:
                seen[t.id] = (t, score)

        if not seen:
            result["status"] = "no_candidates"
            return result

        # Sort by score descending, take top N
        candidates = sorted(seen.values(), key=lambda x: x[1], reverse=True)[:PROMOTE_CANDIDATES_MAX]

        native_dir = Path(mem_path)
        native_dir.mkdir(parents=True, exist_ok=True)

        for t, score in candidates:
            combined = (t.user_content or "") + " " + (t.assistant_content or "")
            topic = combined.strip()[:60]
            if not topic:
                continue

            if _is_on_cooldown(topic, t.id):
                continue

            safe_name = re.sub(r'[^\w一-鿿\-]', '_', topic)[:30].strip("_").lower()
            if not safe_name:
                safe_name = f"promoted_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
            fpath = native_dir / f"promoted_{safe_name}.md"
            if fpath.is_file():
                continue

            md = _render_hot_memory(t.user_content or "", t.assistant_content or "", topic)
            promoted.append(fpath.name)

            if not dry_run:
                fpath.write_text(md, encoding="utf-8")
                _log_promotion(topic, str(fpath), t.id)
                sys.stderr.write(f"[promote] ↑ {fpath.name} (score={score})\n")

        result["promoted"] = promoted if not dry_run else f"dry_run ({len(promoted)} would promote)"
        result["count"] = len(promoted)
        result["status"] = "ok"
    except Exception as e:
        result["status"] = f"error: {e}"
    finally:
        cache.close_all()

    return result


# ── Maintenance runner ──


def run_maintenance(dry_run: bool = False) -> dict[str, Any]:
    """Run full maintenance cycle: cache limit → dedup → promote."""
    results = {
        "cache_limit": enforce_cache_limit(dry_run=dry_run),
        "native_dedup": dedup_native_memory(dry_run=dry_run),
        "hot_promote": promote_hot_traces(dry_run=dry_run),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return results


# ── CLI ──


def main() -> None:
    """CLI entry point."""
    dry_run = "--dry-run" in sys.argv or "-n" in sys.argv
    quick = "--quick" in sys.argv or "-q" in sys.argv

    if "--cache-only" in sys.argv:
        results = enforce_cache_limit(dry_run=dry_run)
    elif "--dedup-only" in sys.argv:
        results = dedup_native_memory(dry_run=dry_run)
    elif "--promote-only" in sys.argv:
        results = promote_hot_traces(dry_run=dry_run, quick=quick)
    elif quick:
        results = promote_hot_traces(dry_run=dry_run, quick=True)
    else:
        results = run_maintenance(dry_run=dry_run)

    json.dump(results, sys.stdout, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
