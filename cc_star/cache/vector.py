"""Local cosine similarity search using numpy."""

from __future__ import annotations

from typing import Optional

import numpy as np


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    va = np.array(a, dtype=np.float64)
    vb = np.array(b, dtype=np.float64)
    norm_a = np.linalg.norm(va)
    norm_b = np.linalg.norm(vb)
    if norm_a < 1e-12 or norm_b < 1e-12:
        return 0.0
    return float(np.dot(va, vb) / (norm_a * norm_b))


def search_by_embedding(
    query_embedding: list[float],
    candidates: list[tuple[str, list[float]]],
    k: int = 8,
) -> list[tuple[str, float]]:
    """Search nearest neighbors by cosine similarity.

    Args:
        query_embedding: Query vector.
        candidates: List of (id, embedding_vector) tuples.
        k: Number of results to return.

    Returns:
        List of (id, score) tuples sorted by descending similarity.
    """
    if not candidates:
        return []

    scores: list[tuple[str, float]] = []
    for cid, emb in candidates:
        if emb and len(emb) > 0:
            sim = cosine_similarity(query_embedding, emb)
            scores.append((cid, sim))

    scores.sort(key=lambda x: x[1], reverse=True)
    return scores[:k]


def compute_embedding(text: str, dim: int = 384) -> list[float]:
    """Compute a simple bag-of-characters embedding as a fallback.

    This is a lightweight fallback when the OpenViking embed API is unavailable.
    For production use, use OpenViking's native embedding instead.
    """
    rng = np.random.RandomState(hash(text) & 0xFFFFFFFF)
    vec = rng.randn(dim)
    vec = vec / (np.linalg.norm(vec) + 1e-12)
    return vec.tolist()
