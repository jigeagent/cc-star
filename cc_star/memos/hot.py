"""hot.md — cross-session working-memory continuation.

Maintains a lightweight status snapshot so the next session knows what was
being worked on, without waiting for an FTS5 query.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path


HOT_FILENAME = "hot.md"
DEFAULT_MAX_AGE_HOURS = 24
DEFAULT_MAX_TOKENS = 500


def hot_path(data_dir: str | Path) -> Path:
    """Resolve the hot.md path under a cc-star data directory."""
    return Path(os.path.expanduser(data_dir)) / HOT_FILENAME


def _extract_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Extract YAML-ish frontmatter from hot.md content.

    Returns (meta_dict, body_without_frontmatter).
    This is NOT a full YAML parser — just key:value lines between --- markers.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text

    meta: dict[str, str] = {}
    body_start = 1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            body_start = i + 1
            break
        if ":" in lines[i]:
            key, _, val = lines[i].partition(":")
            meta[key.strip()] = val.strip()

    body = "\n".join(lines[body_start:]).strip()
    return meta, body


def read_hot(data_dir: str | Path, max_age_hours: int = DEFAULT_MAX_AGE_HOURS) -> str | None:
    """Read hot.md content. Returns None if missing, empty, or stale.

    Staleness: if the frontmatter's updated_at is older than max_age_hours,
    the content is still returned but prefixed with a staleness notice.
    """
    path = hot_path(data_dir)
    if not path.is_file():
        return None

    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None

    if not content:
        return None

    meta, body = _extract_frontmatter(content)
    if not body:
        return None

    # Check staleness
    updated_str = meta.get("updated_at", "")
    is_stale = False
    if updated_str:
        try:
            updated = datetime.fromisoformat(updated_str)
            age_seconds = (datetime.now(timezone.utc) - updated).total_seconds()
            is_stale = age_seconds > max_age_hours * 3600
        except (ValueError, TypeError):
            pass

    if is_stale:
        updated_display = updated_str[:16] if updated_str else "未知"
        return f"[hot.md 上次更新: {updated_display} ({max_age_hours}h 前), 可能需要回顾]\n\n{body}"

    return body


def write_hot(
    data_dir: str | Path,
    *,
    project: str = "",
    status: str = "",
    blocked: str = "",
    summary: str = "",
    next_steps: str = "",
    body_text: str = "",
) -> Path:
    """Write or update hot.md with the current session status.

    Returns the path to the written file.
    """
    path = hot_path(data_dir)
    now = datetime.now(timezone.utc).isoformat()

    # Read existing to preserve fields not being overwritten
    existing_meta: dict[str, str] = {}
    existing_body = ""
    if path.is_file():
        try:
            existing_meta, existing_body = _extract_frontmatter(
                path.read_text(encoding="utf-8")
            )
        except OSError:
            pass

    meta: dict[str, str] = {}
    meta["updated_at"] = now
    meta["created_at"] = existing_meta.get("created_at", now)

    # Only write non-empty fields
    for key, val in [("project", project), ("status", status), ("blocked", blocked),
                     ("summary", summary), ("next", next_steps)]:
        if val:
            meta[key] = val

    # Build body (use provided, or existing, or auto-generate from meta)
    if body_text:
        body = body_text
    elif existing_body:
        body = existing_body
    else:
        body_parts = []
        for key, label in [("project", "项目"), ("status", "状态"), ("blocked", "阻塞"),
                           ("summary", "摘要"), ("next", "下一步")]:
            val = meta.get(key, "")
            if val and val != key:  # don't echo the key itself
                body_parts.append(f"- **{label}**: {val}")
        body = "\n".join(body_parts) if body_parts else ""

    # Write file
    lines = ["---"]
    for k, v in meta.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    if body:
        lines.append("")
        lines.append(body)
    lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def format_hot_context(hot_content: str, max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
    """Format hot.md content for injection as system message context.

    Truncates to approximate token count (rough: 4 chars ≈ 1 token).
    """
    if not hot_content:
        return ""

    # Rough truncation: ~4 chars per token
    max_chars = max_tokens * 4
    if len(hot_content) > max_chars:
        hot_content = hot_content[:max_chars] + "\n... [hot.md truncated]"

    return (
        "## Previous session status\n"
        "The following is a snapshot of what was being worked on in the last "
        "session. Use it to continue seamlessly.\n\n"
        f"{hot_content}\n"
    )
