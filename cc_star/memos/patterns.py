"""Pattern capture — extract user preference patterns from conversation text.

Tracks common behavioral patterns like "记住用…" / "以后都用…" / "always use…"
via regex matching + persistent counter. When a pattern hits ≥ threshold,
promote_gate is triggered for native memory promotion.

Data file: pattern_counter.jsonl (co-located with cache.db)
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

# ── Pattern definitions ──

PatternDef = NamedTuple("PatternDef", [("id", str), ("label", str), ("regex", re.Pattern)])

_PATTERNS: list[PatternDef] = [
    # Chinese preference indicators
    PatternDef("prefer_use", "偏好使用", re.compile(
        r"(记住用|以后都用|默认用|优先用|习惯用|改用|改成|prefer\s+to\s+use|prefer\s+using|always\s+use|default\s+to|switch\s+to)\s*[：:\s]*([^\n。.；;]{2,60})",
        re.IGNORECASE,
    )),
    PatternDef("avoid_use", "避免使用", re.compile(
        r"(不要用|别用|避免用|不用|少用|排斥|弃用|don'?t\s+use|avoid\s+using|stop\s+using|never\s+use)\s*[：:\s]*([^\n。.；;]{2,60})",
        re.IGNORECASE,
    )),
    PatternDef("remember_rule", "记住规则", re.compile(
        r"(记住|谨记|注意|规则|原则|规矩|公约|remember\s+(that\s+)?|note\s+that|rule\s*[：:\s]|principle\s*[：:\s])[^\n。.；;]{5,120}",
        re.IGNORECASE,
    )),
    PatternDef("archived_decision", "决策记录", re.compile(
        r"(决策|决定|结论|确认|敲定|拍板|decided?|conclusion|confirmed|settled?)\s*[：:\s][^\n。.；;]{8,120}",
        re.IGNORECASE,
    )),
    PatternDef("architecture_ref", "架构引用", re.compile(
        r"(架构|体系|模块|组件|服务|层\b|接口|协议|schema|architecture|module|component|service|protocol)\s*[：:\s][^\n。.；;]{6,80}",
        re.IGNORECASE,
    )),
]

# ── Counter data path ──

_COUNTER_FILE = "pattern_counter.jsonl"


def _counter_path() -> Path:
    """Resolve pattern_counter.jsonl next to cache.db."""
    data_dir = os.environ.get("CC_STAR_DATA_DIR", "")
    if data_dir:
        return Path(os.path.expanduser(data_dir)) / _COUNTER_FILE
    # Fallback: next to promote_log.jsonl
    cache_path = os.environ.get("CC_STAR_CACHE_PATH", "")
    if cache_path:
        return Path(os.path.expanduser(cache_path)).parent / _COUNTER_FILE
    return Path.home() / ".cc-star" / "data" / _COUNTER_FILE


# ── Counter record ──

class CounterRecord(NamedTuple):
    pattern_id: str
    value: str  # the matched target (e.g. "deepseek-v4-flash")
    count: int
    first_seen: str  # ISO timestamp
    last_seen: str   # ISO timestamp
    promoted: bool   # already promoted to native memory?


def _load_counters() -> dict[str, list[dict]]:
    """Load all counter records, grouped by pattern_id."""
    result: dict[str, list[dict]] = {}
    path = _counter_path()
    if not path.is_file():
        return result
    try:
        for line in path.read_text(encoding="utf-8").strip().split("\n"):
            if not line.strip():
                continue
            rec = json.loads(line)
            pid = rec.get("pattern_id", "unknown")
            result.setdefault(pid, []).append(rec)
    except (OSError, json.JSONDecodeError):
        pass
    return result


def _save_counters(groups: dict[str, list[dict]]) -> None:
    """Write all counter records back to disk."""
    path = _counter_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for recs in groups.values():
        for rec in recs:
            lines.append(json.dumps(rec, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── Public API ──


def scan(text: str) -> list[tuple[PatternDef, str]]:
    """Scan text for all pattern matches.

    Returns list of (pattern_def, matched_value) tuples.
    The matched_value is the extracted target (e.g. "deepseek-v4-flash").
    """
    results: list[tuple[PatternDef, str]] = []
    for pat in _PATTERNS:
        for match in pat.regex.finditer(text):
            # Get the target group (group 2 for prefer/avoid patterns, group 1 for single-group patterns)
            if match.lastindex and match.lastindex >= 2:
                value = match.group(2).strip()
            else:
                value = match.group(1).strip()
            # Clean up: remove the prefix word from value
            for prefix in ["记住用", "以后都用", "默认用", "优先用", "习惯用", "改用", "改成",
                           "不要用", "别用", "避免用", "不用", "少用", "排斥", "弃用",
                           "记住", "谨记", "注意", "规则", "原则", "规矩", "公约"]:
                if value.startswith(prefix):
                    value = value[len(prefix):].strip()
            if value and len(value) >= 2:
                results.append((pat, value))
    return results


def increment(text: str) -> list[dict]:
    """Scan text, increment counters for matches, return newly-matched records.

    Returns list of counter records that were incremented (useful for logging).
    """
    matches = scan(text)
    if not matches:
        return []

    groups = _load_counters()
    now = datetime.now(timezone.utc).isoformat()
    updated_records = []

    for pat, value in matches:
        recs = groups.setdefault(pat.id, [])
        # Find existing record for this value
        found = False
        for rec in recs:
            if rec.get("value") == value:
                rec["count"] += 1
                rec["last_seen"] = now
                found = True
                updated_records.append(dict(rec))
                break
        if not found:
            rec = {
                "pattern_id": pat.id,
                "label": pat.label,
                "value": value,
                "count": 1,
                "first_seen": now,
                "last_seen": now,
                "promoted": False,
            }
            recs.append(rec)
            updated_records.append(dict(rec))

    _save_counters(groups)
    return updated_records


def get_ready_for_promotion(threshold: int = 3) -> list[dict]:
    """Get records that have reached the threshold but haven't been promoted yet."""
    groups = _load_counters()
    ready: list[dict] = []
    for recs in groups.values():
        for rec in recs:
            if rec["count"] >= threshold and not rec.get("promoted", False):
                ready.append(rec)
    return ready


def mark_promoted(pattern_id: str, value: str) -> None:
    """Mark a specific record as promoted."""
    groups = _load_counters()
    for recs in groups.values():
        for rec in recs:
            if rec["pattern_id"] == pattern_id and rec["value"] == value:
                rec["promoted"] = True
                _save_counters(groups)
                return


def get_active_patterns(threshold: int = 2) -> str:
    """Format active patterns as markdown for SessionStart injection.

    Returns empty string if nothing meaningful.
    """
    groups = _load_counters()
    lines = ["**活跃行为模式：**"]
    has_any = False
    for pid, recs in groups.items():
        for rec in recs:
            if rec["count"] >= threshold:
                label = rec.get("label", pid)
                status = "✅ 已固化" if rec.get("promoted") else f"🔄 {rec['count']}次"
                lines.append(f"- [{label}] {rec['value']} ({status})")
                has_any = True
    if not has_any:
        return ""
    return "\n".join(lines)


def render_preference_md(pattern_id: str, value: str, count: int) -> str:
    """Render a pattern preference as a native memory markdown file."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    label_map = {
        "prefer_use": "偏好",
        "avoid_use": "避免",
        "remember_rule": "规则",
        "archived_decision": "决策",
        "architecture_ref": "架构",
    }
    label = label_map.get(pattern_id, "模式")

    return (
        f"# 用户{label}: {value}\n"
        f"\n"
        f"> 自动捕获 · {today}\n"
        f"\n"
        f"**来源：** pattern_counter（{count} 次命中后自动晋升）\n"
        f"\n"
        f"**内容：** {value}\n"
        f"\n"
        f"**类型：** {label}\n"
        f"**置信度：** {'高' if count >= 5 else '中' if count >= 3 else '低'}\n"
        f"\n"
        f"---\n"
        f"_由 cc-star pattern_capture 自动生成 · {today}_\n"
    )
