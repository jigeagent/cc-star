"""ID generation — UUID v7 (time-ordered) + Crockford base32 encoding."""

from __future__ import annotations

import time
import uuid
from typing import Optional

# Crockford Base32 alphabet (no I,L,O,U — avoids confusion)
_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# Reverse lookup for decoding
_CROCKFORD_REVERSE = {c: i for i, c in enumerate(_CROCKFORD_ALPHABET)}


def _uuid7() -> uuid.UUID:
    """Generate a UUID v7 (time-ordered Unix ms + random)."""
    # UUID v7 layout:
    #   48-bit Unix ms timestamp  | 4-bit version (7) | 12-bit random
    #   2-bit variant (10)        | 62-bit random
    timestamp_ms = int(time.time() * 1000)

    # Create 16 bytes
    bytes_ = bytearray(16)

    # Bytes 0-5: timestamp (big-endian)
    bytes_[0] = (timestamp_ms >> 40) & 0xFF
    bytes_[1] = (timestamp_ms >> 32) & 0xFF
    bytes_[2] = (timestamp_ms >> 24) & 0xFF
    bytes_[3] = (timestamp_ms >> 16) & 0xFF
    bytes_[4] = (timestamp_ms >> 8) & 0xFF
    bytes_[5] = timestamp_ms & 0xFF

    # Bytes 6-7: version + random
    rand_a = int.from_bytes(uuid.uuid4().bytes[:2], "big")
    bytes_[6] = (0x70 | (rand_a >> 8)) & 0xFF  # version 7 (0111)
    bytes_[7] = rand_a & 0xFF

    # Bytes 8-15: variant + random
    rand_b = int.from_bytes(uuid.uuid4().bytes[:8], "big")
    bytes_[8] = (0x80 | (rand_b >> 58)) & 0xFF  # variant RFC 4122 (10)
    for i in range(7):
        bytes_[9 + i] = (rand_b >> (52 - 8 * i)) & 0xFF

    return uuid.UUID(bytes=bytes(bytes_))


def new_id(length: int = 26) -> str:
    """Generate a Crockford-base32-encoded UUID v7 ID.

    Args:
        length: Truncated length (default 26 chars gives ~130 bits).

    Returns:
        Compact, time-ordered, URL-safe ID string.
    """
    raw = _uuid7()
    # Take the 128-bit UUID as an integer
    val = int(raw)
    chars: list[str] = []
    for _ in range(length):
        chars.append(_CROCKFORD_ALPHABET[val & 0x1F])
        val >>= 5
    # Reverse because we extracted LSB first
    return "".join(reversed(chars))


def timestamp_from_id(id_str: str) -> Optional[int]:
    """Extract Unix timestamp (ms) from a Crockford-base32 UUID v7.

    Returns None if the ID is too short.
    """
    if len(id_str) < 10:
        return None
    # First 10 Crockford chars hold 50 bits of timestamp
    ts = 0
    for c in id_str[:10]:
        ts = (ts << 5) | _CROCKFORD_REVERSE.get(c.upper(), 0)
    return ts
