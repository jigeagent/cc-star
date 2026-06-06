"""RRF (Reciprocal Rank Fusion) and MMR (Maximum Marginal Relevance) rankers."""

from __future__ import annotations

import math
from typing import Any


def rrf_merge(
    rankings: list[list[dict[str, Any]]],
    k: int = 60,
    score_key: str = "score",
) -> list[dict[str, Any]]:
    """Merge multiple ranked lists using Reciprocal Rank Fusion.

    Each result dict must have an 'id' field for deduplication.

    Args:
        rankings: List of ranked result lists.
        k: RRF constant (default 60).
        score_key: Key to store the RRF score.

    Returns:
        Merged and sorted list of unique results.
    """
    rrf_scores: dict[str, float] = {}
    result_map: dict[str, dict[str, Any]] = {}

    for ranked_list in rankings:
        for rank, item in enumerate(ranked_list, 1):
            item_id = item.get("id", item.get("uri", str(hash(str(item)))))
            if item_id not in result_map:
                result_map[item_id] = dict(item)
            rrf_scores[item_id] = rrf_scores.get(item_id, 0.0) + 1.0 / (k + rank)

    merged = []
    for item_id, score in rrf_scores.items():
        result = dict(result_map[item_id])
        result[score_key] = score
        merged.append(result)

    merged.sort(key=lambda x: x.get(score_key, 0), reverse=True)
    return merged


def mmr_diversify(
    results: list[dict[str, Any]],
    query_embedding: list[float] | None = None,
    lambda_: float = 0.7,
    k: int = 8,
    score_key: str = "score",
    embedding_key: str = "embedding",
) -> list[dict[str, Any]]:
    """Diversify results using Maximum Marginal Relevance.

    Args:
        results: Candidate items (must have score/embedding).
        query_embedding: Optional query embedding for relevance.
        lambda_: Trade-off between relevance (1.0) and diversity (0.0).
        k: Number of results to return.
        score_key: Key for relevance scores.
        embedding_key: Key for item embeddings.

    Returns:
        Diversified subset of results.
    """
    if not results:
        return []

    if query_embedding is None:
        # Without query embedding, MMR falls back to score-only sorting
        return sorted(results, key=lambda x: x.get(score_key, 0), reverse=True)[:k]

    selected: list[dict[str, Any]] = []
    candidates = list(results)

    while len(selected) < k and candidates:
        mmr_scores = []
        for i, cand in enumerate(candidates):
            # Relevance = similarity to query
            rel = cand.get(score_key, 0)

            # Diversity = max similarity to already selected
            if selected and embedding_key in cand:
                cand_emb = cand[embedding_key]
                max_sim = max(
                    _cosine_sim(cand_emb, s.get(embedding_key, []))
                    for s in selected
                    if embedding_key in s
                )
            else:
                max_sim = 0.0

            mmr = lambda_ * rel - (1 - lambda_) * max_sim
            mmr_scores.append((i, mmr))

        best_idx = max(mmr_scores, key=lambda x: x[1])[0]
        selected.append(candidates.pop(best_idx))

    return selected


def _cosine_sim(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a < 1e-12 or norm_b < 1e-12:
        return 0.0
    return dot / (norm_a * norm_b)
