"""Multi-tier retrieval from OpenViking."""

from __future__ import annotations

import logging
from typing import Any, Optional

from cc_star.ov.client import OpenVikingClient

logger = logging.getLogger(__name__)


def retrieve_semantic(
    client: OpenVikingClient,
    query: str,
    k: int = 16,
    agent: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Tier 1: Semantic search via OpenViking vector store."""
    return client.search_find(query=query, k=k, agent=agent)


def retrieve_deep(
    client: OpenVikingClient,
    query: str,
    k: int = 8,
    agent: Optional[str] = None,
    context: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Tier 2: Context-aware deep search via OpenViking."""
    return client.search_search(query=query, k=k, agent=agent, context=context)


def retrieve_timeline(
    client: OpenVikingClient,
    agent: str = "default",
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Tier 3: Recent timeline of traces."""
    try:
        resp = client._client.get(
            "/api/v1/content/list",
            params={
                "prefix": f"viking://resources/{agent}/memos/traces/",
                "limit": limit,
                "sort": "created_at",
                "order": "desc",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("results", [])
    except Exception as e:
        logger.warning("timeline retrieval failed: %s", e)
        return []


def retrieve_policies(
    client: OpenVikingClient,
    query: str,
    agent: str = "default",
    k: int = 4,
) -> list[dict[str, Any]]:
    """Tier 4: Policy-specific retrieval."""
    return client.search_find(
        query=query,
        k=k,
        agent=agent,
        resource_type="policy",
    )


def format_results(results: list[dict[str, Any]]) -> str:
    """Format search results into a compact string for context injection."""
    if not results:
        return ""

    lines = ["## 相关记忆", ""]
    for i, r in enumerate(results[:8], 1):
        content = r.get("content", r.get("text", ""))
        score = r.get("score", r.get("relevance", ""))
        source = ""
        if isinstance(content, str) and len(content) > 300:
            content = content[:300] + "..."
        score_str = f" (score: {score:.3f})" if isinstance(score, (int, float)) else ""
        lines.append(f"{i}.{score_str} {content}")
        if source:
            lines[-1] += f" [{source}]"

    return "\n".join(lines)
