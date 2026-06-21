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
    query_dim = len(query_embedding) if query_embedding is not None else 0
    for cid, emb in candidates:
        if emb and len(emb) == query_dim:
            sim = cosine_similarity(query_embedding, emb)
            scores.append((cid, sim))

    scores.sort(key=lambda x: x[1], reverse=True)
    return scores[:k]


class EmbeddingEngine:
    """Lazy-loaded fastembed FlagEmbedding wrapper (singleton).

    Downloads the ~60MB model on first use; subsequent calls use cached instance.
    Thread-safe for reads; model loading is synchronized internally.
    """

    _instance: Optional["EmbeddingEngine"] = None
    _model = None
    _model_name = "BAAI/bge-small-en-v1.5"

    def __new__(cls) -> "EmbeddingEngine":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def _get_model(self):
        if self._model is None:
            from fastembed import TextEmbedding

            type(self)._model = TextEmbedding(
                model_name=self._model_name,
                max_length=512,
            )
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Batch embed a list of texts. Returns list of 384-d unit vectors."""
        if not texts:
            return []
        model = self._get_model()
        return list(model.embed(texts))

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query string."""
        if not text:
            text = " "
        result = self.embed([text])
        return result[0] if result else []


# Keep legacy function for backward compatibility
def compute_embedding(text: str) -> list[float]:
    """Embed text using fastembed (replaced random fallback)."""
    return EmbeddingEngine().embed_query(text)
