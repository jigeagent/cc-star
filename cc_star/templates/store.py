#!/usr/bin/env python3
"""
Stop Hook — cc-star conversation storage + memory promotion.

Reads transcript → stores to cache.db → optionally promotes to native memory.
Config cascade: env var → config.yaml → template-baked default.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from cc_star.cache.connection import CacheConnection
from cc_star.cache.schema import ensure_schema
from cc_star.cache.traces import TraceRepository
from cc_star.memos.id import new_id
from cc_star.memos.types import TraceRow

# ── Runtime config ──
try:
    from cc_star.config import ConfigManager
    _CFG_MGR = ConfigManager()
    _CFG = _CFG_MGR.load()
    _GET = lambda k, d=None: _CFG_MGR.get(k) or d
except Exception:
    _GET = lambda k, d=None: d

CACHE_PATH = os.path.expanduser(os.environ.get("CC_STAR_CACHE_PATH", "$cache_path"))
OV_URL = os.environ.get("CC_STAR_OV_URL", _GET("ov.url", "$ov_url"))
OV_ENABLED = os.environ.get("CC_STAR_OV_ENABLED", "$ov_enabled") in ("1", "true", "True")
NATIVE_MEMORY_PATH = os.path.expanduser(
    os.environ.get("CC_STAR_MEMORY_PATH", _GET("memory.memory_path", "$memory_path"))
)
if not NATIVE_MEMORY_PATH:
    _codex_home = os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex"))
    NATIVE_MEMORY_PATH = os.path.join(_codex_home, "memories", "extensions", "cc-star")
PROMOTE_ENABLED = os.environ.get("CC_STAR_PROMOTE_ENABLED", str(_GET("memory.promote_enabled", "True"))) in ("1", "true", "True")
PROMOTE_THRESHOLD = int(os.environ.get("CC_STAR_PROMOTE_THRESHOLD", str(_GET("memory.promote_threshold", "3"))))
PROMOTE_MIN_LENGTH = int(os.environ.get("CC_STAR_PROMOTE_MIN_LENGTH", str(_GET("memory.promote_min_length", "50"))))
PROMOTE_COOLDOWN_DAYS = int(os.environ.get("CC_STAR_PROMOTE_COOLDOWN_DAYS", str(_GET("memory.promote_cooldown_days", "7"))))
MAX_RETRIES = 5
RETRY_DELAY_MS = 150
TRANSCRIPT_POLL_TIMEOUT = 3.0

# ── Promote tracking file ──
_PROMOTE_LOG = Path(CACHE_PATH).parent / "promote_log.jsonl"


# ── Transcript reading ──


def read_transcript_safe(path: str, max_retries: int = MAX_RETRIES) -> list[dict] | None:
    """Read and parse transcript JSONL, retrying until turn is complete."""
    if not os.path.isfile(path):
        return None

    deadline = time.time() + TRANSCRIPT_POLL_TIMEOUT

    for attempt in range(1, max_retries + 1):
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError:
            time.sleep(RETRY_DELAY_MS / 1000)
            continue

        lines = content.strip().split("\n")
        entries = []
        for line in lines:
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        if not entries:
            if attempt < max_retries:
                time.sleep(RETRY_DELAY_MS / 1000)
                continue
            return None

        last = entries[-1]
        if last.get("type") == "system" and last.get("subtype") == "turn_duration":
            return entries

        if attempt < max_retries and time.time() < deadline:
            time.sleep(RETRY_DELAY_MS / 1000)
        else:
            return entries

    return None


def extract_turn(entries: list[dict]) -> tuple[str, str, str, str] | None:
    """Extract last user/assistant turn from parsed transcript entries."""
    user_content = ""
    assistant_content = ""
    session_id = ""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    for entry in entries:
        if entry.get("type") == "system" and entry.get("subtype") == "session":
            session_id = entry.get("session_id", entry.get("id", ""))

        ts = entry.get("timestamp") or entry.get("created_at", "")
        if ts:
            timestamp = ts

        if entry.get("type") == "user":
            msg = entry.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                user_content = content.strip()

        if entry.get("type") == "assistant":
            msg = entry.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                assistant_content = content.strip()

        if "role" in entry and "content" in entry:
            content = entry["content"]
            if isinstance(content, str) and content.strip():
                if entry["role"] == "user":
                    user_content = content.strip()
                elif entry["role"] == "assistant":
                    assistant_content = content.strip()

    if not user_content and not assistant_content:
        return None

    return user_content, assistant_content, session_id, timestamp


def try_sync_ov(trace: TraceRow) -> bool:
    """Try to sync a single trace to OpenViking."""
    if not OV_URL or not OV_ENABLED:
        return False
    try:
        from cc_star.ov.client import OpenVikingClient
        client = OpenVikingClient(base_url=OV_URL, timeout=3.0)
        uri = f"viking://resources/$agent_name/memos/traces/{trace.id}.json"
        client.content_write(uri, trace.to_dict())
        return True
    except Exception as e:
        print(f"[store] OV sync error: {e}", file=sys.stderr)
        return False


# ── Memory Promotion ──


def _content_hash(text: str) -> str:
    """Hash content for dedup."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _check_promote_cooldown(topic: str) -> bool:
    """Check if a topic is still in cooldown period."""
    if not _PROMOTE_LOG.is_file():
        return True
    try:
        now = datetime.now(timezone.utc)
        for line in _PROMOTE_LOG.read_text(encoding="utf-8").strip().split("\n"):
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("topic") == topic:
                promoted_at = datetime.fromisoformat(record["promoted_at"])
                delta = (now - promoted_at).days
                if delta < PROMOTE_COOLDOWN_DAYS:
                    return False
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    return True


def _log_promotion(topic: str, filepath: str) -> None:
    """Log a promotion event."""
    try:
        _PROMOTE_LOG.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "topic": topic,
            "filepath": filepath,
            "promoted_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(str(_PROMOTE_LOG), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _extract_topic(user_content: str, assistant_content: str) -> str:
    """Extract a short topic label from content."""
    combined = user_content + " " + assistant_content
    # Try first h2/h3 heading
    for line in combined.split("\n"):
        line = line.strip()
        if line.startswith("## ") or line.startswith("### "):
            return line.lstrip("#").strip()[:40]
    # Try first meaningful line
    for line in combined.split("\n"):
        line = line.strip()
        if line and len(line) > 5 and not line.startswith("#") and not line.startswith("{"):
            return line[:40]
    return "memory"


def _render_memory_md(user_content: str, assistant_content: str) -> str:
    """Render a conversation turn as a markdown memory file."""
    topic = _extract_topic(user_content, assistant_content)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    lines = [
        f"# {topic}",
        f"",
        f"> 自动晋升 · {today}",
        f"",
    ]

    if user_content:
        lines.append("## 用户输入")
        lines.append("")
        lines.append(user_content[:500])
        lines.append("")

    if assistant_content:
        lines.append("## 助手回复")
        lines.append("")
        lines.append(assistant_content[:1000])
        lines.append("")

    lines.append("---")
    lines.append(f"_自动晋升记忆 · {today}_")
    return "\n".join(lines)


def _should_promote(user_content: str, assistant_content: str) -> bool:
    """Determine if this turn should be promoted to native memory."""
    if not PROMOTE_ENABLED:
        return False
    if not NATIVE_MEMORY_PATH:
        return False

    combined = user_content + " " + assistant_content
    if len(combined) < PROMOTE_MIN_LENGTH:
        return False

    # Filter: skip bridge_context noise (feishu bridge, system messages)
    if user_content.strip().startswith("<bridge_context>"):
        return False

    # Promote keywords — content that should be remembered long-term
    promote_keywords = [
        "架构", "决策", "协议", "规则", "标准", "规范",
        "方案", "设计", "架构图", "配置",
        "记忆", "记录", "总结", "结论",
        "archived", "decision", "protocol", "standard",
        "architecture", "design", "config",
    ]

    text_lower = combined.lower()
    for kw in promote_keywords:
        if kw in text_lower:
            return True

    return False


def _do_promote(user_content: str, assistant_content: str) -> None:
    """Check conditions and promote to native memory."""
    if not _should_promote(user_content, assistant_content):
        return

    topic = _extract_topic(user_content, assistant_content)
    if not _check_promote_cooldown(topic):
        return

    native_dir = Path(NATIVE_MEMORY_PATH)
    native_dir.mkdir(parents=True, exist_ok=True)

    # Generate filename from topic
    safe_name = re.sub(r'[^\w一-鿿\-]', '_', topic)[:40].strip("_").lower()
    if not safe_name:
        safe_name = f"promoted_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    filepath = native_dir / f"{safe_name}.md"

    # Dedup: if file with same hash exists, skip
    content_hash = _content_hash(user_content + assistant_content)
    for existing in native_dir.glob("*.md"):
        try:
            if content_hash in existing.read_text(encoding="utf-8"):
                return  # Already promoted
        except OSError:
            continue

    # Write
    try:
        md = _render_memory_md(user_content, assistant_content)
        filepath.write_text(md, encoding="utf-8")
        _log_promotion(topic, str(filepath))
        print(f"[store] promoted → {filepath.name}", file=sys.stderr)
    except OSError as e:
        print(f"[store] promote write error: {e}", file=sys.stderr)


# ── Main ──


def main() -> None:
    """Main hook handler."""
    try:
        input_data = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(0)

    transcript_path = input_data.get("transcript_path", "")
    if not transcript_path:
        sys.exit(0)

    entries = read_transcript_safe(transcript_path)
    if not entries:
        print("[store] no transcript entries found", file=sys.stderr)
        sys.exit(0)

    turn = extract_turn(entries)
    if not turn:
        sys.exit(0)

    user_content, assistant_content, session_id, timestamp = turn
    if not user_content and not assistant_content:
        sys.exit(0)

    tags = $tags
    trace = TraceRow(
        id=new_id(),
        session_id=session_id or "unknown",
        turn_index=0,
        user_content=user_content,
        assistant_content=assistant_content,
        tags=tags,
        created_at=timestamp,
    )

    # Store to cache.db
    try:
        cache = CacheConnection(CACHE_PATH)
        ensure_schema(cache)
        repo = TraceRepository(cache)
        repo.insert(trace)
    except Exception as e:
        print(f"[store] cache write error: {e}", file=sys.stderr)
        sys.exit(0)

    # Try OV sync
    synced = try_sync_ov(trace)
    if synced:
        try:
            repo.mark_synced(trace.id)
        except Exception:
            pass

    cache.close_all()

    # Memory promotion (best-effort, after main storage)
    _do_promote(user_content, assistant_content)


if __name__ == "__main__":
    main()
