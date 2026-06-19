#!/usr/bin/env python3
"""
UserPromptSubmit Hook — cc-star memory retrieval injection.

Reads user prompt → cache.db FTS5 + native memory + OpenViking → additionalContext.
Three sources fused via RRF merge (config cascade: env var → config.yaml → baked).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from cc_star.cache.connection import CacheConnection
from cc_star.cache.schema import ensure_schema
from cc_star.cache.traces import TraceRepository
from cc_star.retrieval.ranker import rrf_merge

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
MIN_WORDS = 3
MAX_MEMORIES = int(os.environ.get("CC_STAR_MAX_INJECT", _GET("memory.max_inject", "$max_inject")))
MAX_INJECT_NATIVE = int(os.environ.get("CC_STAR_MAX_INJECT_NATIVE", _GET("memory.max_inject_native", "3")))
CODEX_HOME = os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex"))


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
    cjk = sum(1 for c in text if '一' <= c <= '鿿' or '㐀' <= c <= '䶿')
    non_cjk = len([w for w in text.replace(''.join(c for c in text if '一' <= c <= '鿿' or '㐀' <= c <= '䶿'), ' ').split() if w])
    return cjk + non_cjk


def _tokenize(text: str) -> set[str]:
    """Tokenize text into lowercase keywords for native memory matching."""
    text = text.lower()
    # Extract CJK bigrams
    cjk_chars = re.findall(r'[一-鿿㐀-䶿]', text)
    bigrams = set()
    for i in range(len(cjk_chars) - 1):
        bigrams.add(cjk_chars[i] + cjk_chars[i + 1])
    # English words
    words = set(re.findall(r'[a-z0-9_\-]{3,}', text))
    return bigrams | words


# ── Source 1: Local cache.db FTS5 ──


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


# ── Source 2: Native memory (~/.claude/memory/*.md) ──


def search_native(query: str, limit: int = 5) -> list[dict]:
    """Search native memory .md files by keyword overlap."""
    if not NATIVE_MEMORY_PATH or not Path(NATIVE_MEMORY_PATH).is_dir():
        return []
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    results = []
    for fpath in sorted(Path(NATIVE_MEMORY_PATH).glob("*.md")):
        try:
            text = fpath.read_text(encoding="utf-8")
        except OSError:
            continue
        file_tokens = _tokenize(text)
        overlap = query_tokens & file_tokens
        if not overlap:
            continue
        score = len(overlap) / max(len(query_tokens), 1)
        # Extract title from first heading or filename
        title = ""
        for line in text.split("\n"):
            if line.startswith("# "):
                title = line.lstrip("# ").strip()
                break
        if not title:
            title = fpath.stem

        results.append({
            "id": f"native:{fpath.name}",
            "session_id": fpath.stem,
            "user_content": f"【核心记忆】{title}\n\n{text[:300]}",
            "assistant_content": "",
            "reward": score,
            "tags": ["native", "core"],
            "created_at": "",
            "source": "native",
            "score": score,
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:limit]


# ── Source 2b: Codex native extensions ──


def search_codex_extensions(query: str, limit: int = 5) -> list[dict]:
    """Search Codex extensions/ directory for native memory files."""
    ext_dir = Path(CODEX_HOME) / "memories" / "extensions"
    if not ext_dir.is_dir():
        return []
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []
    results = []
    for fpath in sorted(ext_dir.glob("**/*.md")):
        try:
            text = fpath.read_text(encoding="utf-8")
        except OSError:
            continue
        file_tokens = _tokenize(text)
        overlap = query_tokens & file_tokens
        if not overlap:
            continue
        score = len(overlap) / max(len(query_tokens), 1)
        title = ""
        for line in text.split("\n"):
            if line.startswith("# "):
                title = line.lstrip("# ").strip()
                break
        if not title:
            title = fpath.stem
        rel = fpath.relative_to(ext_dir)
        results.append({
            "id": f"codex_ext:{rel}",
            "session_id": str(rel),
            "user_content": f"【Codex记忆】{title}\n\n{text[:300]}",
            "assistant_content": "",
            "reward": score,
            "tags": ["codex", "extension"],
            "created_at": "",
            "source": "codex_ext",
            "score": score,
        })
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:limit]


# ── Source 3: OpenViking ──


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
    src_label = {"local": "对话记忆", "native": "核心记忆", "ov": "共享记忆"}.get(m["source"], m["source"])
    lines = [f"[{src_label}] session={m['session_id'][:12]} | {m.get('created_at', '')[:10]}"]
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

    # Tri-channel search
    t0 = time.time()
    local_results = search_local(repo, prompt, limit=8)
    native_results = search_native(prompt, limit=MAX_INJECT_NATIVE)
    codex_results = search_codex_extensions(prompt, limit=MAX_INJECT_NATIVE)
    ov_results = search_ov(prompt, limit=8)
    elapsed = time.time() - t0

    # Merge via RRF
    merged = rrf_merge([local_results, native_results, codex_results, ov_results], k=60)
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
    native_n = sum(1 for m in merged if m["source"] == "native")
    codex_n = sum(1 for m in merged if m["source"] == "codex_ext")
    ov_n = sum(1 for m in merged if m["source"] == "ov")

    output = {
        "additionalContext": context,
        "systemMessage": (
            f"{total} memories injected "
            f"(FTS5:{local_n} 核心:{native_n} OV:{ov_n} {elapsed:.1f}s)"
        ),
    }

    json.dump(output, sys.stdout, ensure_ascii=False)
    cache.close_all()


if __name__ == "__main__":
    main()
