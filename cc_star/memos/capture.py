"""L1 Trace capture — records interaction turns as MemOS traces."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from cc_star.memos.id import new_id
from cc_star.memos.types import TraceRow
from cc_star.ov.client import OpenVikingClient

logger = logging.getLogger(__name__)


def capture_trace(
    client: OpenVikingClient,
    session_id: str,
    turn_index: int,
    user_content: str,
    assistant_content: str,
    agent_name: str = "default",
    tags: Optional[list[str]] = None,
    metadata: Optional[dict] = None,
) -> Optional[TraceRow]:
    """Capture a single interaction turn as an L1 Trace.

    Steps:
    1. Generate a UUID v7 ID for the trace
    2. Compute embedding via OpenViking API
    3. Build TraceRow and persist to OpenViking storage
    4. Return the TraceRow for local caching
    """
    trace_id = new_id()
    created_at = datetime.now(timezone.utc).isoformat()

    # Compute combined text for embedding
    embed_text = f"User: {user_content}\nAssistant: {assistant_content}"

    # Get embedding from OpenViking
    embedding = client.embed(embed_text)

    trace = TraceRow(
        id=trace_id,
        session_id=session_id,
        turn_index=turn_index,
        user_content=user_content,
        assistant_content=assistant_content,
        embedding=embedding,
        tags=tags or [],
        metadata=metadata or {},
        created_at=created_at,
    )

    # Persist to OpenViking
    uri = f"viking://resources/{agent_name}/memos/traces/{trace_id}.json"
    content = json.dumps(trace.to_dict(), ensure_ascii=False, default=str)
    success = client.content_write(
        uri=uri,
        content=content,
        content_type="application/json",
        metadata={
            "type": "trace",
            "session_id": session_id,
            "turn_index": turn_index,
            "agent": agent_name,
        },
    )

    if not success:
        logger.warning("Failed to persist trace %s to OpenViking", trace_id)
        return None

    logger.info("Trace %s captured (turn %d)", trace_id, turn_index)
    return trace
