"""HermesNextProvider — MemoryProvider implementation for Hermes Agent."""

from __future__ import annotations

import functools
import json
import logging
from typing import Any, Optional

from cc_star.hn_config import HermesNextConfig
from cc_star.memos.capture import capture_trace
from cc_star.memos.id import new_id
from cc_star.memos.retrieval import (
    format_results,
    retrieve_deep,
    retrieve_policies,
    retrieve_semantic,
    retrieve_timeline,
)
from cc_star.memos.types import TraceRow
from cc_star.ov.client import OpenVikingClient
from cc_star.ov.session import OVSession

logger = logging.getLogger(__name__)


class HermesNextProvider:
    """Hermes Agent memory provider backed by OpenViking + MemOS cognitive engine."""

    def __init__(self, config: Optional[HermesNextConfig] = None):
        self._config = config or HermesNextConfig()
        self._client: Optional[OpenVikingClient] = None
        self._session: Optional[OVSession] = None
        self._agent_name: str = self._config.agent.name
        self._turn_index: int = 0
        self._initialized: bool = False
        # Embedding cache: {text: embedding}
        self._embed_cache: dict[str, list[float]] = {}
        self._embed_cache_max: int = 256

    def _get_embedding(self, text: str) -> Optional[list[float]]:
        """Cached embedding lookup."""
        if not self._client:
            return None
        if text in self._embed_cache:
            return self._embed_cache[text]
        if len(self._embed_cache) >= self._embed_cache_max:
            # Simple evict: clear half
            evict = len(self._embed_cache) // 2
            for k in list(self._embed_cache)[:evict]:
                del self._embed_cache[k]
        emb = self._client.embed(text)
        if emb:
            self._embed_cache[text] = emb
        return emb

    # ── Required MemoryProvider interface ───────────────────

    @property
    def name(self) -> str:
        return "hermes-next"

    def is_available(self) -> bool:
        """Check if the OpenViking server is reachable."""
        if not self._client:
            return False
        return self._client.health()

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        """Establish OpenViking connection and open a session."""
        cfg = self._config
        self._client = OpenVikingClient(
            base_url=cfg.openviking.base_url,
            api_key=cfg.openviking.api_key,
            timeout=cfg.openviking.timeout,
            max_retries=cfg.openviking.max_retries,
        )
        self._agent_name = kwargs.get("agent_name", cfg.agent.name)
        self._session = OVSession(
            client=self._client,
            session_id=session_id,
            agent_name=self._agent_name,
        )
        self._turn_index = 0
        self._initialized = True
        logger.info(
            "HermesNextProvider initialized (session=%s, agent=%s)",
            session_id,
            self._agent_name,
        )

    def prefetch(self, query: str, *, session_id: str) -> str:
        """Unified retrieval pipeline: fetch relevant context before a turn.

        Combines semantic search, deep search, policy matching, and timeline.
        """
        if not self._client:
            return ""

        agent = self._agent_name

        # Parallel retrieval from multiple tiers
        semantic_results = retrieve_semantic(self._client, query, agent=agent)
        deep_results = retrieve_deep(self._client, query, agent=agent)
        policy_results = retrieve_policies(self._client, query, agent=agent)

        # Combine and deduplicate by ID
        seen: set[str] = set()
        combined: list[dict[str, Any]] = []

        for results, weight in [
            (semantic_results, 1.0),
            (deep_results, 0.8),
            (policy_results, 0.6),
        ]:
            for r in results:
                rid = r.get("id", r.get("uri", ""))
                if rid and rid not in seen:
                    r["_weight"] = r.get("_weight", 1.0) * weight
                    seen.add(rid)
                    combined.append(r)

        # Sort by relevance score or weight
        combined.sort(
            key=lambda x: (
                x.get("score", x.get("relevance", x.get("_weight", 0)))
                if isinstance(x.get("score"), (int, float))
                else 0
            ),
            reverse=True,
        )

        return format_results(combined)

    def sync_turn(  # noqa: PLR0913
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str,
        metadata: Optional[dict[str, Any]] = None,
        tags: Optional[list[str]] = None,
    ) -> None:
        """Record a single interaction turn as an L1 Trace."""
        if not self._client:
            logger.warning("Provider not initialized, skipping turn capture")
            return

        self._turn_index += 1
        capture_trace(
            client=self._client,
            session_id=session_id,
            turn_index=self._turn_index,
            user_content=user_content,
            assistant_content=assistant_content,
            agent_name=self._agent_name,
            tags=tags,
            metadata={
                **(metadata or {}),
                "source": "hermes-next",
            },
        )

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Return tool schemas exposed to the Hermes agent."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "memos_search",
                    "description": "Search across all memory traces for relevant context",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Search query for relevant memories",
                            },
                            "k": {
                                "type": "integer",
                                "description": "Number of results to return (max 32)",
                                "default": 8,
                            },
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "memos_get",
                    "description": "Read a specific trace by its ID",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "trace_id": {
                                "type": "string",
                                "description": "ID of the trace to read",
                            },
                        },
                        "required": ["trace_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "memos_timeline",
                    "description": "View recent memory activity timeline",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "limit": {
                                "type": "integer",
                                "description": "Number of recent entries",
                                "default": 8,
                            },
                        },
                        "required": [],
                    },
                },
            },
        ]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any]) -> str:
        """Dispatch a tool call from the agent."""
        if not self._client:
            return "Error: provider not initialized"

        if tool_name == "memos_search":
            query = args.get("query", "")
            k = min(args.get("k", 8), 32)
            results = retrieve_semantic(self._client, query, k=k, agent=self._agent_name)
            return format_results(results) or "No relevant memories found."

        if tool_name == "memos_get":
            trace_id = args.get("trace_id", "")
            uri = f"viking://resources/{self._agent_name}/memos/traces/{trace_id}.json"
            content = self._client.content_read(uri)
            if content:
                try:
                    data = json.loads(content)
                    trace = TraceRow.from_dict(data)
                    return (
                        f"## Trace: {trace.id}\n"
                        f"**User:** {trace.user_content}\n"
                        f"**Assistant:** {trace.assistant_content}\n"
                        f"**Tags:** {', '.join(trace.tags) if trace.tags else 'none'}\n"
                        f"**Created:** {trace.created_at}"
                    )
                except (json.JSONDecodeError, KeyError) as e:
                    return f"Error parsing trace: {e}"
            return f"Trace {trace_id} not found."

        if tool_name == "memos_timeline":
            limit = min(args.get("limit", 8), 50)
            results = retrieve_timeline(self._client, agent=self._agent_name, limit=limit)
            if not results:
                return "No recent activity."
            lines = ["## Recent Memory Timeline", ""]
            for r in results:
                created = r.get("created_at", r.get("timestamp", "?"))
                summary = r.get("content", r.get("text", ""))[:100]
                lines.append(f"- [{created}] {summary}")
            return "\n".join(lines)

        return f"Unknown tool: {tool_name}"

    def shutdown(self) -> None:
        """Close the OpenViking connection."""
        if self._client:
            self._client.close()
        self._initialized = False
        logger.info("HermesNextProvider shut down")

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        """Called when the Hermes session ends — commit OV session."""
        if self._session:
            import asyncio

            try:
                asyncio.run(self._session.commit())
            except Exception as e:
                logger.warning("Session commit failed: %s", e)

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
        """Generate a memory summary for context compression."""
        if not messages:
            return ""
        recent = messages[-6:]
        summary_parts: list[str] = []
        for msg in recent:
            role = msg.get("role", "?")
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > 100:
                content = content[:100] + "..."
            summary_parts.append(f"[{role}] {content}")
        return "\n".join(summary_parts[-3:])

    def on_delegation(
        self,
        task: str,
        result: str,
        *,
        session_id: str,
        target_agent: Optional[str] = None,
    ) -> None:
        """Record sub-agent delegation as a tagged trace."""
        if not self._client:
            return
        self._turn_index += 1
        tags = ["delegation"]
        if target_agent:
            tags.append(f"agent:{target_agent}")
        capture_trace(
            client=self._client,
            session_id=session_id,
            turn_index=self._turn_index,
            user_content=f"[Delegation to {target_agent or 'sub-agent'}] {task}",
            assistant_content=result,
            agent_name=self._agent_name,
            tags=tags,
            metadata={"type": "delegation", "target_agent": target_agent},
        )
