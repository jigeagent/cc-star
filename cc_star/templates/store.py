#!/usr/bin/env python3
"""
Stop Hook — cc-star conversation storage.

Reads transcript -> extracts last turn -> writes to cache.db -> optionally syncs OV.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from cc_star.cache.connection import CacheConnection
from cc_star.cache.schema import ensure_schema
from cc_star.cache.traces import TraceRepository
from cc_star.memos.id import new_id
from cc_star.memos.types import TraceRow

CACHE_PATH = os.path.expanduser("$cache_path")
OV_URL = os.environ.get("CC_STAR_OV_URL", "$ov_url")
OV_ENABLED = os.environ.get("CC_STAR_OV_ENABLED", "$ov_enabled") in ("1", "true", "True")
MAX_RETRIES = 5
RETRY_DELAY_MS = 150
TRANSCRIPT_POLL_TIMEOUT = 3.0


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

        # Check for turn_duration marker (turn is complete)
        last = entries[-1]
        if last.get("type") == "system" and last.get("subtype") == "turn_duration":
            return entries

        if attempt < max_retries and time.time() < deadline:
            time.sleep(RETRY_DELAY_MS / 1000)
        else:
            return entries

    return None


def extract_turn(entries: list[dict]) -> tuple[str, str, str, str] | None:
    """Extract last user/assistant turn from parsed transcript entries.

    Returns: (user_content, assistant_content, session_id, timestamp)
    """
    user_content = ""
    assistant_content = ""
    session_id = ""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    for entry in entries:
        # Track session_id
        if entry.get("type") == "system" and entry.get("subtype") == "session":
            session_id = entry.get("session_id", entry.get("id", ""))

        # Track timestamps from any entry
        ts = entry.get("timestamp") or entry.get("created_at", "")
        if ts:
            timestamp = ts

        # User message (not tool_result)
        if entry.get("type") == "user":
            msg = entry.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                user_content = content.strip()

        # Assistant message
        if entry.get("type") == "assistant":
            msg = entry.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                assistant_content = content.strip()

        # Also handle flat format: {"role": "user", "content": "..."}
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


def main() -> None:
    """Main hook handler."""
    try:
        input_data = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(0)

    transcript_path = input_data.get("transcript_path", "")
    if not transcript_path:
        sys.exit(0)

    # Read transcript
    entries = read_transcript_safe(transcript_path)
    if not entries:
        print("[store] no transcript entries found", file=sys.stderr)
        sys.exit(0)

    # Extract last turn
    turn = extract_turn(entries)
    if not turn:
        sys.exit(0)

    user_content, assistant_content, session_id, timestamp = turn
    if not user_content and not assistant_content:
        sys.exit(0)

    # Create trace
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

    # Try OV sync (non-blocking, best-effort)
    synced = try_sync_ov(trace)
    if synced:
        try:
            repo.mark_synced(trace.id)
        except Exception:
            pass

    cache.close_all()


if __name__ == "__main__":
    main()
