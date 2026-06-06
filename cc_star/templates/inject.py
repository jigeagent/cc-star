#!/usr/bin/env python3
"""
UserPromptSubmit Hook — cc-star memory retrieval injection.

Reads user prompt -> cache.db FTS5 + optional OV semantic search -> additionalContext.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from cc_star.cache.connection import CacheConnection
from cc_star.cache.schema import ensure_schema
from cc_star.cache.traces import TraceRepository
from cc_star.retrieval.ranker import rrf_merge

CACHE_PATH = os.path.expanduser("$cache_path")
OV_URL = os.environ.get("CC_STAR_OV_URL", "$ov_url")
OV_ENABLED = os.environ.get("CC_STAR_OV_ENABLED", "$ov_enabled") in ("1", "true", "True")
MIN_WORDS = 3
MAX_MEMORIES = $max_inject


def sanitize_query(text: str) -> str:
    """Remove surrogate characters and control chars that break FTS5/HTTP."""
    if not text:
        return ""
    try:
        text = text.encode("utf-8", "surrogatepass").decode("utf-8", "replace")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return "".join(c for c in text if c.isprintable() or c in (" ", "\n", "\t"))


def count_tokens(text: str) -> int:
    """Count words/CJK chars for prompt length check."""
    if not text:
        return 0
    text = text.strip()
    if not text:
        return 0
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u3400' <= c <= '\u4dbf')
    non_cjk = len([w for w in text.replace(''.join(c for c in text if '\u4e00' <= c <= '\u9fff' or '\u3400' <= c <= '\u4dbf'), ' ').split() if w])
    return cjk + non_cjk


def search_local(repo: TraceRepository, query: str, limit: int = 8) -> list[dict]:
    """Search local cache.db FTS5."""
    results = []
    try:
        traces = repo.search_fts(query, limit=limit)
        for t in traces:
            results.append({
                "id": t.id,
                "session_id": t.session_id,
                "user_content": t.user_content,
                "assistant_content": t.assistant_content,
                "reward": t.reward,
                "tags": t.tags,
                "created_at": t.created_at,
                "source": "local",
                "score": 1.0,
            })
    except Exception as e:
        print(f"[inject] FTS5 search error: {e}", file=sys.stderr)
    return results


def search_ov(query: str, limit: int = 8) -> list[dict]:
    """Search OpenViking semantic."""
    if not OV_URL or not OV_ENABLED:
        return []
    results = []
    try:
        from cc_star.ov.client import OpenVikingClient
        client = OpenVikingClient(base_url=OV_URL, timeout=3.0)
        ov_results = client.search_find(query=query, k=limit)
        for r in ov_results:
            results.append({
                "id": r.get("id", ""),
                "session_id": r.get("session_id", ""),
                "user_content": r.get("user_content", r.get("content", "")),
                "assistant_content": r.get("assistant_content", ""),
                "reward": r.get("reward", 0.0),
                "tags": r.get("tags", []),
                "created_at": r.get("created_at", ""),
                "source": "ov",
                "score": r.get("score", r.get("relevance", 0.5)),
            })
    except Exception as e:
        print(f"[inject] OV search error: {e}", file=sys.stderr)
    return results


def format_memory_block(m: dict) -> str:
    """Format a single memory as text block for additionalContext."""
    lines = [f"[Past Memory] session={m['session_id'][:12]} | {m.get('created_at', '')[:10]}"]
    if m.get("tags"):
        lines.append(f"  tags: {', '.join(m['tags'][:3])}")
    lines.append(f"  user: {m['user_content'][:200]}")
    if m['assistant_content']:
        lines.append(f"  assistant: {m['assistant_content'][:200]}")
    return "\n".join(lines)


def main() -> None:
    """Main hook handler."""
    try:
        input_data = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        print(json.dumps({"systemMessage": "inject: invalid input"}))
        sys.exit(0)

    prompt = sanitize_query(input_data.get("prompt", ""))
    if not prompt or count_tokens(prompt) < MIN_WORDS:
        sys.exit(0)

    # Init cache
    try:
        cache = CacheConnection(CACHE_PATH)
        ensure_schema(cache)
        repo = TraceRepository(cache)
    except Exception as e:
        print(f"[inject] cache init error: {e}", file=sys.stderr)
        sys.exit(0)

    # Dual-channel search
    t0 = time.time()
    local_results = search_local(repo, prompt, limit=8)
    ov_results = search_ov(prompt, limit=8)
    elapsed = time.time() - t0

    # Merge via RRF
    merged = rrf_merge([local_results, ov_results], k=60)
    merged = merged[:MAX_MEMORIES]

    if not merged:
        sys.exit(0)

    # Build additionalContext
    context = []
    for m in merged:
        context.append({
            "text": format_memory_block(m),
            "source": f"cc-star/{m['source']}",
            "priority": float(m.get("score", 0.5)),
        })

    total = len(merged)
    local_n = sum(1 for m in merged if m["source"] == "local")
    ov_n = sum(1 for m in merged if m["source"] == "ov")

    output = {
        "additionalContext": context,
        "systemMessage": (
            f"{total} memories injected "
            f"(FTS5:{local_n} OV:{ov_n} {elapsed:.1f}s)"
        ),
    }

    json.dump(output, sys.stdout, ensure_ascii=False)
    cache.close_all()


if __name__ == "__main__":
    main()
