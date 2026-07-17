"""MCP server exposing cc-star hmem as Claude Code tools.

Provides:
- hmem_search: Semantic + BM25 hybrid memory search
- hmem_status: Hierarchy health and stats
- hmem_build: Trigger rebuild of the H-MEM hierarchy

Usage (standalone):
    python -m cc_star.mcp.server

Usage (via Claude Code MCP config):
    {
        "mcpServers": {
            "cc-star": {
                "command": "python",
                "args": ["-m", "cc_star.mcp.server"]
            }
        }
    }
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import anyio

from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolResult,
    ServerCapabilities,
    TextContent,
    Tool,
    ToolsCapability,
)

from cc_star import __version__
from cc_star.cache.connection import CacheConnection
from cc_star.cache.vector import EmbeddingEngine
from cc_star.config import ConfigManager
from cc_star.hmem.store import HierarchicalStore
from cc_star.hmem.router import IndexRouter

logger = None  # will be set in main


# ── Lazy store/router singleton ──

_store: HierarchicalStore | None = None
_router: IndexRouter | None = None


def _get_store() -> HierarchicalStore:
    global _store
    if _store is None:
        cfg = ConfigManager()
        data_dir = cfg.data_dir
        hmem_path = data_dir / "hmem.db"
        if not hmem_path.is_file():
            raise RuntimeError(
                "hmem.db not found. Run 'cc-star hmem build' first, "
                "or ensure cc-star is properly initialized."
            )
        cache = CacheConnection(str(hmem_path))
        _store = HierarchicalStore(cache)
    return _store


def _get_router() -> IndexRouter:
    global _router
    if _router is None:
        store = _get_store()
        router = IndexRouter(store)
        router.build_indexes()
        _router = router
    return _router


# ── Tool implementations ──


async def _search(query: str, top_k: int = 5) -> list[dict[str, Any]]:
    """Run hmem hybrid search and return structured results."""
    router = _get_router()
    if not router.is_ready:
        return [{"error": "hmem not ready — run 'cc-star hmem build' first"}]

    results = router.retrieve(query, top_k=top_k)
    return [
        {
            "episode_id": r.episode_id,
            "trace_title": r.trace_title,
            "content": r.content[:500],
            "score": round(r.score, 4),
            "effective_score": round(r.effective_score, 4),
            "weight": round(r.weight, 3),
            "domain_id": r.domain_id,
        }
        for r in results
    ]


async def _status() -> dict[str, Any]:
    """Return hmem hierarchy and index status."""
    router = _get_router()
    store = _get_store()

    stats = store.stats()
    router_ready = router.is_ready

    info: dict[str, Any] = {
        "hmem_enabled": True,
        "version": __version__,
        "hierarchy": stats,
        "indexes_ready": {
            "domain": router._indexes["domain"].is_built,
            "category": router._indexes["category"].is_built,
            "trace": router._indexes["trace"].is_built,
            "episode": router._indexes["episode"].is_built,
        },
        "router_ready": router_ready,
    }

    if router._hybrid and router._hybrid.is_ready:
        info["hybrid"] = router._hybrid.stats()

    info["cached_episodes"] = len(router._cached_episodes) if router._cache_valid else 0
    info["cached_traces"] = len(router._cached_trace_map) if router._cache_valid else 0

    return info


async def _build() -> dict[str, Any]:
    """Trigger hmem hierarchy rebuild."""
    from cc_star.cache.schema import ensure_schema
    from cc_star.cache.traces import TraceRepository

    cfg = ConfigManager()
    data_dir = cfg.data_dir

    cache = CacheConnection(str(data_dir / "cache.db"))
    ensure_schema(cache)
    repo = TraceRepository(cache)

    store = _get_store()
    from cc_star.hmem.migration import HierarchyMigration

    migration = HierarchyMigration(repo, store)
    result = migration.run()

    # Rebuild router with new data
    global _router
    _router = None
    router = _get_router()

    cache.close_all()
    result["indexes"] = {
        "domain": router._indexes["domain"].size,
        "category": router._indexes["category"].size,
        "trace": router._indexes["trace"].size,
        "episode": router._indexes["episode"].size,
    }
    return result


# ── MCP Server setup ──


def create_server() -> Server:
    """Create and configure the MCP server with hmem tools."""
    server = Server("cc-star")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="hmem_search",
                title="H-MEM Hybrid Search",
                description=(
                    "Search Claude Code's hierarchical memory using BM25 + vector hybrid retrieval. "
                    "Returns semantically relevant conversation episodes with relevance scores. "
                    "Best for recalling past decisions, discussions, code patterns, and context."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Natural language search query",
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "Number of results (1-20)",
                            "default": 5,
                            "minimum": 1,
                            "maximum": 20,
                        },
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="hmem_status",
                title="H-MEM Status",
                description=(
                    "Show the current status of the hierarchical memory system: "
                    "node counts per layer, index readiness, cache state, and hybrid retriever stats."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
            Tool(
                name="hmem_build",
                title="Rebuild H-MEM Hierarchy",
                description=(
                    "Rebuild the hierarchical memory index from all available traces. "
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "confirm": {
                            "type": "boolean",
                            "description": "Set to true to confirm rebuild",
                            "default": False,
                        },
                    },
                    "required": ["confirm"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> CallToolResult:
        if arguments is None:
            arguments = {}

        try:
            if name == "hmem_search":
                query = arguments.get("query", "")
                if not query:
                    return CallToolResult(
                        content=[TextContent(type="text", text="Error: query is required")],
                        isError=True,
                    )
                top_k = min(int(arguments.get("top_k", 5)), 20)
                results = await _search(query, top_k=top_k)
                text = json.dumps(results, indent=2, ensure_ascii=False)
                return CallToolResult(
                    content=[TextContent(type="text", text=text)]
                )

            elif name == "hmem_status":
                info = await _status()
                text = json.dumps(info, indent=2, ensure_ascii=False)
                return CallToolResult(
                    content=[TextContent(type="text", text=text)]
                )

            elif name == "hmem_build":
                confirm = arguments.get("confirm", False)
                if not confirm:
                    return CallToolResult(
                        content=[TextContent(
                            type="text",
                            text="Rebuild requires confirmation. Set confirm=true to proceed.",
                        )],
                        isError=True,
                    )
                result = await _build()
                text = json.dumps(result, indent=2, ensure_ascii=False)
                return CallToolResult(
                    content=[TextContent(type="text", text=text)]
                )

            else:
                return CallToolResult(
                    content=[TextContent(type="text", text=f"Unknown tool: {name}")],
                    isError=True,
                )

        except Exception as e:
            return CallToolResult(
                content=[TextContent(type="text", text=f"Error: {e}")],
                isError=True,
            )

    return server


# ── Entry point ──


async def main() -> None:
    """Run the MCP server on stdio."""
    server = create_server()

    # Pre-warm embedding engine
    try:
        EmbeddingEngine().embed_query("warmup")
    except Exception:
        pass

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="cc-star",
                server_version=__version__,
                capabilities=ServerCapabilities(
                    tools=ToolsCapability(listChanged=False),
                ),
                instructions=(
                    "cc-star hierarchical memory system for Claude Code. "
                    "Use hmem_search to find relevant past conversations, decisions, and code patterns. "
                    "Use hmem_status to check memory health. Use hmem_build to rebuild the index."
                ),
            ),
        )


if __name__ == "__main__":
    anyio.run(main)
