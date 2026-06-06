"""OpenViking REST client wrapper with connection pooling and retry.

Wraps the OpenViking SDK's REST API for Hermes Next usage.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

_RETRYABLE_STATUSES = {502, 503, 504}


class OpenVikingError(Exception):
    """Base error for OpenViking operations."""

    def __init__(self, message: str, status_code: int = 0, body: str = ""):
        self.status_code = status_code
        self.body = body
        super().__init__(f"[{status_code}] {message}")


class OpenVikingClient:
    """Thin HTTP client for OpenViking REST API with connection pooling."""

    def __init__(
        self,
        base_url: str = "http://localhost:1933",
        api_key: Optional[str] = None,
        timeout: float = 30.0,
        max_retries: int = 3,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._timeout = timeout
        self._max_retries = max_retries
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # Connection pool: 10 connections, keep-alive
        # httpx >=0.28 removed PoolLimits; pass limits directly to transport
        try:
            transport = httpx.HTTPTransport(
                retries=max_retries,
                pool_limits=httpx.PoolLimits(
                    max_connections=10,
                    max_keepalive_connections=5,
                ),
            )
        except AttributeError:
            # httpx >= 0.28: PoolLimits removed, use max_connections directly
            transport = httpx.HTTPTransport(
                retries=max_retries,
                max_connections=10,
                max_keepalive_connections=5,
            )
        self._client = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=httpx.Timeout(timeout, connect=5.0),
            transport=transport,
        )

    # ── Health ──────────────────────────────────────────────

    def health(self) -> bool:
        """Check if the OpenViking server is reachable."""
        try:
            resp = self._client.get("/health")
            return resp.status_code < 500
        except httpx.HTTPError:
            return False

    # ── Search ──────────────────────────────────────────────

    def search_find(
        self,
        query: str,
        k: int = 16,
        agent: Optional[str] = None,
        resource_type: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Semantic search returning top-k results."""
        payload: dict[str, Any] = {"query": query, "k": k}
        if agent:
            payload["agent"] = agent
        if resource_type:
            payload["resource_type"] = resource_type
        try:
            resp = self._client.post("/api/v1/search/find", json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data.get("results", data if isinstance(data, list) else [])
        except httpx.HTTPError as e:
            logger.warning("search_find failed: %s", e)
            return []

    def search_search(
        self,
        query: str,
        k: int = 16,
        agent: Optional[str] = None,
        context: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        """Deep search with context."""
        payload: dict[str, Any] = {"query": query, "k": k}
        if agent:
            payload["agent"] = agent
        if context:
            payload["context"] = context
        try:
            resp = self._client.post("/api/v1/search/search", json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data.get("results", data if isinstance(data, list) else [])
        except httpx.HTTPError as e:
            logger.warning("search_search failed: %s", e)
            return []

    # ── Content ─────────────────────────────────────────────

    def content_read(self, uri: str) -> Optional[str]:
        """Read content by OpenViking URI."""
        try:
            resp = self._client.get("/api/v1/content/read", params={"uri": uri})
            resp.raise_for_status()
            data = resp.json()
            return data.get("content", data.get("data", ""))
        except httpx.HTTPError as e:
            logger.warning("content_read(%s) failed: %s", uri, e)
            return None

    def content_write(
        self,
        uri: str,
        content: str,
        content_type: str = "text/markdown",
        metadata: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Write content to OpenViking storage."""
        payload: dict[str, Any] = {
            "uri": uri,
            "content": content,
            "content_type": content_type,
        }
        if metadata:
            payload["metadata"] = metadata
        try:
            resp = self._client.post("/api/v1/content/write", json=payload)
            resp.raise_for_status()
            return True
        except httpx.HTTPError as e:
            logger.warning("content_write(%s) failed: %s", uri, e)
            return False

    # ── Embed ───────────────────────────────────────────────

    def embed(self, text: str) -> Optional[list[float]]:
        """Get embedding vector for a text string."""
        try:
            resp = self._client.post("/api/v1/embed", json={"text": text})
            resp.raise_for_status()
            data = resp.json()
            return data.get("embedding", data.get("vector"))
        except httpx.HTTPError as e:
            logger.warning("embed failed: %s", e)
            return None

    # ── Lifecycle ───────────────────────────────────────────

    def close(self) -> None:
        """Close underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> "OpenVikingClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
