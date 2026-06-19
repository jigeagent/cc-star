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
from cc_star.promote_gate import (
    evaluate_gate, load_gate_state, save_gate_state, update_gate_state,
    log_rejection, load_recent_rejections, compute_soft_score,
    GateState, GateResult,
)


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
    if raw:
        return os.path.expanduser(raw)
    codex_home = os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex"))
    codex_ext = os.path.join(codex_home, "memories", "extensions", "cc-star")
    if os.path.isdir(os.path.dirname(os.path.dirname(codex_ext))):
        return codex_ext
    return ""


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

# ── Gate config (移植自 SkillOpt) ──
GATE_ENABLED = _env_or("promote.gate_enabled", "CC_STAR_GATE_ENABLED", "True") in ("1", "true", "True")
"""是否启用验证门控。关闭时退化为旧的单向晋升（force-accept）。"""

GATE_METRIC = _env_or("promote.gate_metric", "CC_STAR_GATE_METRIC", "mixed")
"""门控指标: hard | soft | mixed。"""

GATE_MIXED_WEIGHT = float(_env_or("promote.gate_mixed_weight", "CC_STAR_GATE_MIXED_WEIGHT", "0.3"))
"""mixed 模式下 soft 的权重。"""
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
    - Metadata penalty (deduct for JSON/XML injected content)
    - Conversational bonus (reward for real discussion flow)
    - Normalised to 0-10 scale.
    """
    combined = (user_content or "") + " " + (assistant_content or "")
    if not combined.strip():
        return 0.0

    length = len(combined)
    if length < PROMOTE_MIN_LENGTH:
        return 0.0

    # ── Metadata penalty: JSON-heavy / XML-tag-heavy content ──
    json_ratio = combined.count("{") / max(length, 1)
    xml_tag_count = len(re.findall(r'<[^>]+>', combined))
    xml_tag_ratio = xml_tag_count / max(length, 1)
    meta_ratio = json_ratio + xml_tag_ratio
    metadata_penalty = min(meta_ratio * 100, 8.0)  # up to -8 points

    # ── Specific metadata pattern penalty ──
    # bridge_context / system-reminder / instructions blocks
    meta_patterns = [
        r'<bridge_context>', r'<system-reminder>', r'<function_calls>',
        r'<user_input>', r'<quoted_message>', r'<interactive_card>',
        r'<bridge_instructions>',
        r'"chatId":', r'"senderId":', r'"botOpenId":', r'"chatType"',
    ]
    pattern_penalty = sum(5 for p in meta_patterns if re.search(p, combined))
    pattern_penalty = min(pattern_penalty, 8.0)  # cap at -8

    # ── Conversational bonus ──
    # Real conversations have: 问答结构、自然语言、换行分段
    has_natural_lang = 0
    if "?" in combined or "？" in combined:
        has_natural_lang += 1
    if "：" in combined or ":" in combined:
        has_natural_lang += 1
    if combined.count("\n") >= 3:
        has_natural_lang += 1
    has_chinese = bool(re.findall(r'[一-鿿]', combined))
    conversation_bonus = min(has_natural_lang * 1.0, 2.0) + (0.5 if has_chinese else 0.0)  # reduced cap

    # ── Base score: 2-6 based on length ──
    length_score = min(max((length / 500) * 3, 2.0), 6.0)

    # ── Keyword density bonus: up to +4 ──
    text_lower = combined.lower()
    kw_hits = sum(1 for kw in PROMOTE_KEYWORDS if kw.lower() in text_lower)
    keyword_bonus = min(kw_hits * 0.5, 2.5)  # reduced cap: avoid 10.0 saturation

    score = length_score + keyword_bonus + conversation_bonus - metadata_penalty - pattern_penalty
    return round(max(min(score, 10.0), 0.0), 2)


def _compute_diversity_penalty(combined, existing_contents):
    if not combined or not existing_contents:
        return 0.0
    cjk = re.findall(r"[一-鿿㐀-䶿]", combined)
    c_bigrams = {cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)}
    c_en = set(re.findall(r"[a-z0-9_\-]{3,}", combined.lower()))
    c_tokens = c_bigrams | c_en
    if not c_tokens:
        return 0.0
    max_j = 0.0
    for existing in existing_contents:
        ec = re.findall(r"[一-鿿㐀-䶿]", existing)
        eb = {ec[i] + ec[i + 1] for i in range(len(ec) - 1)}
        ee = set(re.findall(r"[a-z0-9_\-]{3,}", existing.lower()))
        et = eb | ee
        if not et:
            continue
        jaccard = len(c_tokens & et) / max(len(c_tokens | et), 1)
        if jaccard > max_j:
            max_j = jaccard
    if max_j <= 0.3:
        return 0.0
    return round(min((max_j - 0.3) * 4.5, 3.0), 2)


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
            if score >= PROMOTE_THRESHOLD_SCORE and existing_contents:
                dp = _compute_diversity_penalty(
                    (t.user_content or "") + " " + (t.assistant_content or ""),
                    existing_contents,
                )
                score = round(max(score - dp, 0.0), 2)
            if score >= PROMOTE_THRESHOLD_SCORE:
                seen[t.id] = (t, score)

        if not seen:
            result["status"] = "no_candidates"
            return result

        # Sort by score descending, take top N
        candidates = sorted(seen.values(), key=lambda x: x[1], reverse=True)[:PROMOTE_CANDIDATES_MAX]

        # ── Gate setup (移植自 SkillOpt) ──
        gate_state = load_gate_state() if GATE_ENABLED else GateState()
        global_step = gate_state.promote_count + 1
        gate_results: list[GateResult] = []

        native_dir = Path(mem_path)
        native_dir.mkdir(parents=True, exist_ok=True)

        # Load existing native memory contents for soft score
        existing_contents = []
        if GATE_METRIC in ("soft", "mixed") and native_dir.is_dir():
            for f in native_dir.glob("*.md"):
                try:
                    existing_contents.append(f.read_text("utf-8"))
                except OSError:
                    continue

        for t, hard_score in candidates:
            combined = (t.user_content or "") + " " + (t.assistant_content or "")
            topic = combined.strip()[:60]
            if not topic:
                continue

            # Cooldown check (before gate — cheap filter)
            if _is_on_cooldown(topic, t.id):
                continue

            # Gate evaluation
            if GATE_ENABLED:
                soft_score = compute_soft_score(combined, existing_contents)
                gate_result = evaluate_gate(
                    candidate_id=t.id,
                    candidate_hard=hard_score,
                    current_state=gate_state,
                    global_step=global_step,
                    candidate_soft=soft_score,
                    metric=GATE_METRIC,
                    mixed_weight=GATE_MIXED_WEIGHT,
                )
                gate_results.append(gate_result)

                if gate_result.action == "reject":
                    if not dry_run:
                        log_rejection(gate_result, reason=f"score {hard_score} <= current {gate_state.current_score}")
                        sys.stderr.write(f"[promote] ✗ REJECT {t.id[:12]} (hard={hard_score} gate={gate_result.candidate_score:.2f} <= {gate_state.current_score:.2f})\n")
                    continue
            else:
                # Gate disabled: force-accept (legacy behavior)
                gate_result = GateResult(
                    action="accept", candidate_id=t.id,
                    candidate_score=hard_score,
                    current_score=hard_score,
                    best_score=hard_score,
                    best_id=t.id,
                    best_step=global_step,
                )
                gate_results.append(gate_result)

            # Accept — write to native memory
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
                # Update gate state
                gate_state = update_gate_state(gate_state, gate_result)
                action_label = {"accept_new_best": "🏆", "accept": "↑", "reject": "✗", "force_accept": "→"}.get(
                    gate_result.action, "?"
                )
                sys.stderr.write(
                    f"[promote] {action_label} {fpath.name} "
                    f"(hard={hard_score} gate={gate_result.candidate_score:.2f} "
                    f"current={gate_state.current_score:.2f} "
                    f"best={gate_state.best_score:.2f})\n"
                )

        # Save gate state
        if not dry_run and GATE_ENABLED:
            save_gate_state(gate_state)

        result["promoted"] = promoted if not dry_run else f"dry_run ({len(promoted)} would promote)"
        result["count"] = len(promoted)
        result["gate"] = {
            "enabled": GATE_ENABLED,
            "metric": GATE_METRIC,
            "current_score": gate_state.current_score,
            "best_score": gate_state.best_score,
            "best_id": gate_state.best_id[:12] if gate_state.best_id else "",
            "promote_count": gate_state.promote_count,
            "reject_count": gate_state.reject_count,
            "n_accepted": sum(1 for gr in gate_results if gr.action in ("accept", "accept_new_best")),
            "n_rejected": sum(1 for gr in gate_results if gr.action == "reject"),
        }
        result["status"] = "ok"
    except Exception as e:
        result["status"] = f"error: {e}"
    finally:
        cache.close_all()

    return result


# ── 4. MEMORY.md capacity management ──

NATIVE_MEMORY_MAX_CHARS = 5_000
"""Target max chars for the Claude Code MEMORY.md file. When exceeded,
oldest auto-promoted entries are removed."""

ANTI_PATTERN_MARKER = "<!-- cc-star:anti-pattern -->"


def trim_memory_md(dry_run: bool = False) -> dict[str, Any]:
    """Trim MEMORY.md when it exceeds the target size.

    Removes oldest auto-promoted entries (marked with cc-star style headers)
    until under limit. Manual entries are preserved.

    Ported from hermes-next v0.4.0 NativeMemoryClient.trim_to_fit().
    """
    result: dict[str, Any] = {"action": "trim_memory_md", "dry_run": dry_run}
    mem_path = _native_memory_path()
    if not mem_path:
        result["status"] = "no_native_memory"
        return result

    md_file = Path(mem_path) / "MEMORY.md"
    if not md_file.is_file():
        result["status"] = "no_memory_md"
        return result

    try:
        content = md_file.read_text(encoding="utf-8")
        size = len(content)
        result["size_chars"] = size
        result["max_chars"] = NATIVE_MEMORY_MAX_CHARS

        if size <= NATIVE_MEMORY_MAX_CHARS:
            result["status"] = "under_limit"
            return result

        # Split into sections by heading, keep manual ones
        lines = content.split("\n")
        kept: list[str] = []
        current_section: list[str] = []
        in_auto = False
        removed = 0

        def flush_section():
            nonlocal current_section, in_auto, removed
            if not current_section:
                return
            section_text = "\n".join(current_section)
            is_manual = any(
                line.strip().startswith("#") and "auto-promoted" not in line.lower()
                for line in current_section[:3]
            )
            if is_manual or not in_auto:
                kept.extend(current_section)
            else:
                removed += 1
            current_section = []
            in_auto = False

        for line in lines:
            if line.startswith("# "):
                flush_section()
                in_auto = "auto-promoted" in line.lower()
            current_section.append(line)
        flush_section()

        if dry_run:
            result["would_remove"] = removed
            result["status"] = "would_trim"
            return result

        result["removed_sections"] = removed
        new_content = "\n".join(kept)
        md_file.write_text(new_content, encoding="utf-8")
        result["new_size_chars"] = len(new_content)
        result["status"] = "ok"
    except Exception as e:
        result["status"] = f"error: {e}"

    return result


# ── 5. Anti-pattern marking ──


def mark_anti_pattern(trace_id: str, user_text: str, assistant_text: str) -> dict[str, Any]:
    """Mark a promoted memory as an anti-pattern based on negative feedback.

    Adds a visible ANTI-PATTERN banner to the promoted markdown file so
    Claude Code sees it clearly in the next session.

    Ported from hermes-next v0.4.0 Decision Repair concept.
    """
    result: dict[str, Any] = {"action": "mark_anti_pattern"}
    mem_path = _native_memory_path()
    if not mem_path:
        result["status"] = "no_native_memory"
        return result

    # Find the promoted file for this trace
    native_dir = Path(mem_path)
    if not native_dir.is_dir():
        result["status"] = "no_native_dir"
        return result

    target_file = None
    for f in native_dir.glob("promoted_*.md"):
        content = f.read_text(encoding="utf-8")
        if trace_id in content:
            target_file = f
            break

    if not target_file:
        result["status"] = "no_matching_file"
        return result

    content = target_file.read_text(encoding="utf-8")
    if ANTI_PATTERN_MARKER in content:
        result["status"] = "already_marked"
        return result

    # Prepend anti-pattern banner
    banner = (
        f"{ANTI_PATTERN_MARKER}\n"
        f"> ⚠️ **反模式 / Anti-Pattern** — 用户反馈该模式不推荐使用\n"
        f"> 反馈内容: {user_text[:200]}\n"
        f"> 标记时间: {datetime.now(timezone.utc).isoformat()}\n"
        f"\n"
    )
    target_file.write_text(banner + content, encoding="utf-8")
    result["status"] = "marked"
    result["file"] = target_file.name
    return result


# ── 6. Startup recovery (lightweight) ──


def check_startup_health() -> dict[str, Any]:
    """Lightweight startup recovery check.

    Uses a timestamp file to detect unclean shutdowns and reports
    any anomalies. Unlike hermes-next's full session_state recovery,
    cc-star uses a simple file-lock approach suitable for single-user
    Claude Code scenarios.

    Ported from hermes-next v0.4.0 session_state recovery concept.
    """
    result: dict[str, Any] = {"action": "startup_health"}
    db_path = _cachedb_path()
    data_dir = Path(db_path).parent
    health_file = data_dir / ".cc_star_health"

    now = datetime.now(timezone.utc)

    if health_file.is_file():
        try:
            last_ts = datetime.fromisoformat(health_file.read_text(encoding="utf-8").strip())
            elapsed_hours = (now - last_ts).total_seconds() / 3600
            result["last_healthy"] = last_ts.isoformat()
            result["elapsed_hours"] = round(elapsed_hours, 1)

            if elapsed_hours > 4:
                result["status"] = "long_gap"
                result["warning"] = (
                    f"上次健康关闭距今 {elapsed_hours:.0f} 小时，"
                    f"建议运行 cc-star promote 检查记忆一致性"
                )
            else:
                result["status"] = "healthy"
        except (ValueError, OSError) as e:
            result["status"] = "unreadable"
            result["error"] = str(e)
    else:
        result["status"] = "first_run"
        result["warning"] = "未检测到上次健康关闭记录——首次运行或非正常退出"

    # Write health timestamp
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        health_file.write_text(now.isoformat(), encoding="utf-8")
        result["health_written"] = True
    except OSError as e:
        result["health_written"] = False
        result["health_error"] = str(e)

    return result


# ── Maintenance runner ──


def run_maintenance(dry_run: bool = False) -> dict[str, Any]:
    """Run full maintenance cycle: cache limit → dedup → promote → trim."""
    results = {
        "cache_limit": enforce_cache_limit(dry_run=dry_run),
        "native_dedup": dedup_native_memory(dry_run=dry_run),
        "hot_promote": promote_hot_traces(dry_run=dry_run),
        "trim_memory_md": trim_memory_md(dry_run=dry_run),
        "startup_health": check_startup_health(),
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
