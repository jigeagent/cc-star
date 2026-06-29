#!/usr/bin/env python3
"""
SessionStart Hook — cc-star session startup check.

Checks OV health + reads last session summary + injects hot.md context.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

OV_URL = os.environ.get("CC_STAR_OV_URL", "$ov_url")
SESSIONS_FILE = Path(os.path.expanduser("$sessions_file"))
HOT_PATH = Path(os.path.expanduser("$hot_path"))
HOT_ENABLED = os.environ.get("CC_STAR_HOT_ENABLED", "$hot_enabled") in ("1", "true", "True")
HOT_MAX_AGE_HOURS = int(os.environ.get("CC_STAR_HOT_MAX_AGE", "$hot_max_age_hours"))


def check_ov_health() -> bool:
    """Check OpenViking connectivity."""
    url = OV_URL
    if not url:
        return False
    try:
        import httpx
        r = httpx.get(f"{url}/health", timeout=2.0)
        return r.status_code == 200
    except Exception:
        return False


def last_session_summary() -> str | None:
    """Read last session info from sessions.jsonl."""
    if not SESSIONS_FILE.is_file():
        return None
    try:
        lines = SESSIONS_FILE.read_text(encoding="utf-8").strip().split("\n")
        if not lines:
            return None
        last = json.loads(lines[-1])
        prompt = last.get("first_prompt", "")
        turns = last.get("turn_count", 0)
        if prompt:
            return f"Last session: {turns} turns | {prompt[:60]}"
    except (OSError, json.JSONDecodeError):
        pass
    return None


def read_hot_context() -> str | None:
    """Read hot.md if enabled and return formatted context string."""
    if not HOT_ENABLED:
        return None
    if not HOT_PATH.is_file():
        return None
    try:
        content = HOT_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not content:
        return None

    # Simple frontmatter extract
    lines = content.split("\n")
    meta: dict[str, str] = {}
    body_start = 1
    if lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                body_start = i + 1
                break
            if ":" in lines[i]:
                k, _, v = lines[i].partition(":")
                meta[k.strip()] = v.strip()

    body = "\n".join(lines[body_start:]).strip()
    if not body:
        return None

    # Check staleness
    updated_str = meta.get("updated_at", "")
    is_stale = False
    if updated_str:
        try:
            from datetime import datetime, timezone
            updated = datetime.fromisoformat(updated_str)
            age = (datetime.now(timezone.utc) - updated).total_seconds()
            is_stale = age > HOT_MAX_AGE_HOURS * 3600
        except (ValueError, TypeError):
            pass

    if is_stale:
        return (
            f"[hot.md 上次更新: {updated_str[:16]} ({HOT_MAX_AGE_HOURS}h 前), 可能需要回顾]\n\n"
            f"{body}\n"
        )

    return body


def main() -> None:
    ov_ok = check_ov_health()
    msg_parts = []
    if ov_ok:
        msg_parts.append("OV:online")
    else:
        msg_parts.append("OV:offline (local mode)")

    last = last_session_summary()
    if last:
        msg_parts.append(last)

    system_msg = " | ".join(msg_parts)

    # Inject hot.md context if available
    hot_text = read_hot_context()
    if hot_text:
        hot_block = (
            "\n\n## 上次会话工作状态\n"
            "以下是上次会话结束时的工作快照，据此无缝续接：\n\n"
            f"{hot_text}\n"
        )
        system_msg += hot_block

    output = {"systemMessage": system_msg}
    json.dump(output, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
