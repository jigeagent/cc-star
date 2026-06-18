#!/usr/bin/env python3
"""
PreCompact / PostCompact Hook — cc-star context compression protection.

Save: saves STATUS.md / MEMORY.md / Snapshot -> tmp JSON before compression.
Restore: restores from tmp JSON -> additionalContext after compression.

Config cascade: env var > config.yaml > template-baked default.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# ── Runtime config (cascade: env var → config.yaml → baked fallback) ──
try:
    from cc_star.config import ConfigManager
    _CFG_MGR = ConfigManager()
    _CFG = _CFG_MGR.load()
    _GET = lambda k, d=None: _CFG_MGR.get(k) or d  # noqa: E731
except Exception:
    _GET = lambda k, d=None: d                      # standalone fallback


def _resolve(key: str, env: str, baked: str) -> str:
    """Resolve a config value: env var → config.yaml → baked."""
    return os.environ.get(env) or _GET(key) or baked


DATA_DIR = Path(os.path.expanduser(
    _resolve("storage.path", "CC_STAR_DATA_DIR", "$data_dir")
))
TMP_FILE = DATA_DIR / "compact_state.json"

MEMORY_PATH = os.path.expanduser(
    _resolve("memory.memory_path", "CC_STAR_MEMORY_PATH", "$memory_path")
)
MEMORY_FILE = Path(MEMORY_PATH) if MEMORY_PATH else None

STATUS_PATH = os.path.expanduser(
    _resolve("memory.status_path", "CC_STAR_STATUS_PATH", "$status_path")
)
STATUS_FILE = Path(STATUS_PATH) if STATUS_PATH else Path(os.path.expanduser("~/STATUS.md"))

SNAPSHOT_PATH = os.path.expanduser(
    _resolve("memory.snapshot_path", "CC_STAR_SNAPSHOT_PATH", "$snapshot_path")
)
SNAPSHOT_FILE = Path(SNAPSHOT_PATH) if SNAPSHOT_PATH else Path(os.path.expanduser("~/_openviking_snapshot.md"))


def find_memory_file() -> Path | None:
    """Auto-detect MEMORY.md if memory_path not explicitly configured."""
    if MEMORY_FILE is not None:
        return MEMORY_FILE if MEMORY_FILE.is_file() else None
    projects_dir = Path(os.path.expanduser("~/.claude/projects"))
    if not projects_dir.is_dir():
        return None
    for proj in projects_dir.iterdir():
        mem = proj / "memory" / "MEMORY.md"
        if mem.is_file():
            return mem
    return None


def safe_read(path: Path) -> str | None:
    """Read file if exists, return None otherwise."""
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    except OSError:
        pass
    return None


# -- Save (PreCompact) ----------------------------------------


def do_save() -> None:
    """Save current memory state before compression."""
    mem_file = find_memory_file()

    state = {
        "status_md": safe_read(STATUS_FILE),
        "memory_md": safe_read(mem_file) if mem_file else None,
        "snapshot_md": safe_read(SNAPSHOT_FILE),
        "saved_at": __import__("datetime").datetime.now().isoformat(),
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TMP_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    parts = []
    if state["status_md"]:
        parts.append("STATUS.md")
    if state["memory_md"]:
        parts.append("MEMORY.md")
    if state["snapshot_md"]:
        parts.append("snapshot")
    sys.stderr.write(f"[compact] saved: {', '.join(parts) if parts else 'nothing'}\n")


# -- Restore (PostCompact) ------------------------------------


def do_restore() -> None:
    """Restore memory state after compression."""
    if not TMP_FILE.is_file():
        sys.exit(0)

    try:
        state = json.loads(TMP_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        sys.exit(0)

    context_parts = []

    # 1. MEMORY.md — always inject
    memory_md = state.get("memory_md", "")
    if memory_md:
        lines = memory_md.split("\n")
        if len(lines) > 200:
            memory_md = "\n".join(lines[:200]) + "\n\n[truncated...]"
        context_parts.append({
            "text": f"## Memory Guide\n\n{memory_md}",
            "source": "cc-star/memory",
            "priority": 0.9,
        })

    # 2. Memory Snapshot
    snapshot = state.get("snapshot_md", "")
    if snapshot:
        lines = snapshot.split("\n")
        if len(lines) > 100:
            snapshot = "\n".join(lines[:100]) + "\n\n[truncated...]"
        context_parts.append({
            "text": f"## Memory Snapshot\n\n{snapshot}",
            "source": "cc-star/snapshot",
            "priority": 0.7,
        })

    # 3. STATUS.md
    status = state.get("status_md", "")
    if status:
        context_parts.append({
            "text": f"## Status\n\n{status[:1000]}",
            "source": "cc-star/status",
            "priority": 0.5,
        })

    # Clean up tmp file
    try:
        TMP_FILE.unlink()
    except OSError:
        pass

    if context_parts:
        output = {"additionalContext": context_parts}
        json.dump(output, sys.stdout, ensure_ascii=False)


# -- Entry ----------------------------------------------------


def main() -> None:
    """Dispatch based on CLAUDE_HOOKS_EVENT."""
    event = os.environ.get("CLAUDE_HOOKS_EVENT", "")
    mode = sys.argv[1] if len(sys.argv) > 1 else event

    if mode in ("precompact", "PreCompact", "save"):
        do_save()
    elif mode in ("postcompact", "PostCompact", "restore"):
        do_restore()
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
