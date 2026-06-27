"""Bark push notification helper for cc-star hooks.

Bark is a free iOS push tool: https://github.com/Finb/Bark
Install from App Store, get device key, POST to https://api.day.app/{key}/{title}/{body}

Env config:
  CC_STAR_BARK_KEY      — Bark device key (required to enable)
  CC_STAR_BARK_SERVER   — custom server URL (optional, defaults to api.day.app)
"""

from __future__ import annotations

import json
import os
import sys
from urllib.request import Request, urlopen
from urllib.error import URLError


def _bark_key() -> str:
    return os.environ.get("CC_STAR_BARK_KEY", "")


def _bark_server() -> str:
    return os.environ.get("CC_STAR_BARK_SERVER", "https://api.day.app")


def is_enabled() -> bool:
    """Check if Bark push is configured."""
    return bool(_bark_key())


def push(title: str, body: str = "", group: str = "cc-star") -> bool:
    """Send a Bark push notification.

    Returns True on success, False on failure (never raises).
    """
    key = _bark_key()
    if not key:
        return False

    server = _bark_server()
    url = f"{server}/{key}/{title}"

    payload = {
        "body": body,
        "group": group,
        "isArchive": 1,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    try:
        req = Request(url, data=data, headers={"Content-Type": "application/json"})
        resp = urlopen(req, timeout=5)
        result = json.loads(resp.read().decode("utf-8"))
        return result.get("code") == 200
    except (URLError, OSError, json.JSONDecodeError) as e:
        print(f"[notify] Bark push failed: {e}", file=sys.stderr)
        return False


def notify_session_end(summary: str, turn_count: int = 0) -> bool:
    """Push a session-end notification via Bark.

    Args:
        summary: Short summary of what was done this session.
        turn_count: Number of turns in this session.

    Returns True if push succeeded.
    """
    if not is_enabled():
        return False

    # Truncate for push notification display
    short = summary.strip()[:120]
    if len(summary) > 120:
        short += "…"

    title = "✅ cc-star 任务完成"
    body = short
    if turn_count:
        body = f"[{turn_count}轮] {short}"

    return push(title, body)


if __name__ == "__main__":
    # Quick test
    if not is_enabled():
        print("Bark not configured. Set CC_STAR_BARK_KEY env var.")
        sys.exit(1)

    ok = push("cc-star 测试", "这是一条测试推送", group="cc-star-test")
    print(f"Push {'OK' if ok else 'FAILED'}")
