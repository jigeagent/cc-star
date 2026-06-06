"""OpenViking session lifecycle management."""

from __future__ import annotations

import logging
from typing import Any, Optional

from cc_star.ov.client import OpenVikingClient

logger = logging.getLogger(__name__)


class OVSession:
    """Manages an OpenViking session for a single conversation."""

    def __init__(
        self,
        client: OpenVikingClient,
        session_id: str,
        agent_name: str = "default",
    ):
        self._client = client
        self.session_id = session_id
        self.agent_name = agent_name
        self._ov_session_id: Optional[str] = None
        self._message_ids: list[str] = []

    async def create(self, metadata: Optional[dict[str, Any]] = None) -> bool:
        """Create a new OpenViking session."""
        try:
            payload = {
                "id": self.session_id,
                "agent": self.agent_name,
            }
            if metadata:
                payload["metadata"] = metadata

            # Use the underlying HTTP client directly
            resp = self._client._client.post(
                "/api/v1/sessions",
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            self._ov_session_id = data.get("id", self.session_id)
            logger.info("OV session created: %s", self._ov_session_id)
            return True
        except Exception as e:
            logger.warning("Failed to create OV session: %s", e)
            return False

    async def add_message(
        self,
        role: str,
        content: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Optional[str]:
        """Add a message to the session."""
        if not self._ov_session_id:
            logger.warning("No active OV session to add message to")
            return None
        try:
            payload: dict[str, Any] = {
                "role": role,
                "content": content,
            }
            if metadata:
                payload["metadata"] = metadata

            resp = self._client._client.post(
                f"/api/v1/sessions/{self._ov_session_id}/messages",
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            msg_id = data.get("id", "")
            self._message_ids.append(msg_id)
            return msg_id
        except Exception as e:
            logger.warning("Failed to add message: %s", e)
            return None

    async def commit(self) -> bool:
        """Commit the session (triggers memory extraction on OV side)."""
        if not self._ov_session_id:
            logger.warning("No active OV session to commit")
            return False
        try:
            resp = self._client._client.post(
                f"/api/v1/sessions/{self._ov_session_id}/commit",
            )
            resp.raise_for_status()
            logger.info("OV session committed: %s", self._ov_session_id)
            return True
        except Exception as e:
            logger.warning("Failed to commit OV session: %s", e)
            return False

    @property
    def message_count(self) -> int:
        return len(self._message_ids)
