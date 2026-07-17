"""Hybrid retrieval — BM25 + Vector search with Reciprocal Rank Fusion.

Adds exact keyword matching to complement FAISS semantic search.
Critical for code queries where function names, error codes, and
symbols must match exactly — pure vector search often misses these.

Usage:
    hybrid = HybridRetriever(store)
    hybrid.build()                          # Build BM25 index from episode content
    results = hybrid.search(query, top_k=5) # RRF-fused BM25 + vector results
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import defaultdict
from typing import Any, Optional

from cc_star.hmem.models import HierarchyNode, Layer, EpisodeResult
from cc_star.hmem.store import HierarchicalStore

logger = logging.getLogger(__name__)

# ── Lightweight BM25 implementation (no dependency on rank_bm25) ──


class _BM25Index:
    """Pure-Python BM25 Okapi implementation.

    No external dependencies — uses only stdlib math.
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._doc_freq: dict[str, int] = {}
        self._doc_lens: list[int] = []
        self._doc_texts: list[str] = []
        self._doc_ids: list[str] = []
        self._avgdl: float = 0.0
        self._total_docs: int = 0
        self._built = False

    def build(self, docs: list[tuple[str, str]]) -> None:
        """Build BM25 index from (doc_id, text) pairs."""
        self._doc_ids = []
        self._doc_texts = []
        self._doc_lens = []
        self._doc_freq = defaultdict(int)
        self._total_docs = len(docs)

        if not docs:
            self._built = True
            return

        for doc_id, text in docs:
            tokens = self._tokenize(text)
            self._doc_ids.append(doc_id)
            self._doc_texts.append(text)
            self._doc_lens.append(len(tokens))
            # Count document frequency
            seen = set()
            for t in tokens:
                if t not in seen:
                    self._doc_freq[t] += 1
                    seen.add(t)

        self._avgdl = sum(self._doc_lens) / max(len(self._doc_lens), 1)
        self._built = True
        logger.info(
            "BM25Index: built with %d docs, avgdl=%.1f, vocab=%d",
            self._total_docs, self._avgdl, len(self._doc_freq),
        )

    def search(self, query: str, k: int = 10) -> list[tuple[str, float]]:
        """Search BM25 index, return (doc_id, score) pairs."""
        if not self._built or self._total_docs == 0:
            return []

        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []

        scores = [0.0] * self._total_docs
        idf_cache: dict[str, float] = {}

        for qt in query_tokens:
            if qt not in self._doc_freq:
                continue
            if qt not in idf_cache:
                n_q = self._doc_freq[qt]
                idf_cache[qt] = math.log(
                    (self._total_docs - n_q + 0.5) / (n_q + 0.5) + 1.0
                )
            idf = idf_cache[qt]

            # Score each document
            for i in range(self._total_docs):
                # Count term frequency in this doc
                tf = self._term_frequency(qt, self._doc_texts[i])
                if tf == 0:
                    continue
                tf_weighted = (tf * (self.k1 + 1)) / (
                    tf + self.k1 * (1 - self.b + self.b * self._doc_lens[i] / self._avgdl)
                )
                scores[i] += idf * tf_weighted

        # Sort by score descending
        indexed = [(self._doc_ids[i], scores[i]) for i in range(self._total_docs) if scores[i] > 0]
        indexed.sort(key=lambda x: x[1], reverse=True)
        return indexed[:k]

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """Tokenize Chinese + English text.

        For Chinese: extracts 2-gram (bigram) characters.
        For English: lowercase, split on non-alphanumeric.
        Mixed: preserves tokens like 'recall@k', 'IndexFlatIP', 'bge-small-zh-v1.5'.
        """
        text = text.lower()
        tokens: list[str] = []

        # Extract English-like words (including @, -, _, . in identifiers)
        words = re.findall(r'[a-z0-9@_.\-/]+', text)
        tokens.extend(w for w in words if len(w) >= 2)

        # Extract Chinese bigrams
        cjk_chars = re.findall(r'[一-鿿㐀-䶿]', text)
        for i in range(len(cjk_chars) - 1):
            bigram = cjk_chars[i] + cjk_chars[i + 1]
            tokens.append(bigram)

        return tokens

    @staticmethod
    def _term_frequency(term: str, text: str) -> int:
        """Count occurrences of a term in text (case-insensitive)."""
        return text.lower().count(term)

    @property
    def is_built(self) -> bool:
        return self._built

    @property
    def size(self) -> int:
        return self._total_docs


# ── Reciprocal Rank Fusion ──


def _rrf_merge(
    results: list[list[tuple[str, float]]],
    k: int = 60,
    top_n: int = 10,
) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion: merge multiple ranked lists.

    RRF score = Σ 1/(k + rank_i(d)) for each document across all lists.
    """
    rrf_scores: dict[str, float] = {}
    for ranked_list in results:
        for rank, (doc_id, _) in enumerate(ranked_list):
            rrf_scores[doc_id] = rrf_scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)

    sorted_docs = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)
    return sorted_docs[:top_n]


# ── Main HybridRetriever ──


class HybridRetriever:
    """BM25 + Vector hybrid search with RRF fusion.

    Two retrieval strategies:
        1. BM25: exact keyword matches (code symbols, error messages)
        2. Vector: semantic similarity (concepts, intent)

    Results are fused via Reciprocal Rank Fusion.
    """

    def __init__(self, store: HierarchicalStore):
        self._store = store
        self._bm25 = _BM25Index()
        self._episode_id_to_node: dict[str, HierarchyNode] = {}
        self._built = False

    def build(self) -> None:
        """Build BM25 index from all episode nodes."""
        episodes = self._store.get_layer_nodes(Layer.EPISODE)
        if not episodes:
            logger.warning("HybridRetriever: no episode nodes to index")
            self._built = True
            return

        # Build BM25 index
        docs = [(ep.id, ep.content or ep.summary or ep.title) for ep in episodes]
        self._bm25.build(docs)

        # Cache episode node lookup
        self._episode_id_to_node = {ep.id: ep for ep in episodes}

        self._built = True

    def search(
        self,
        query: str,
        query_vector: list[float],
        top_k: int = 5,
        bm25_weight: float = 0.4,
        vector_weight: float = 0.6,
    ) -> list[EpisodeResult]:
        """Hybrid search: BM25 + Vector → RRF → EpisodeResult.

        Args:
            query: Raw query text (for BM25).
            query_vector: Query embedding (for vector search).
            top_k: Number of final results.
            bm25_weight: Weight for BM25 scores (0.0 = pure vector).
            vector_weight: Weight for vector scores (0.0 = pure BM25).

        Returns:
            Ranked list of EpisodeResult.
        """
        if not self._built:
            self.build()

        # 1. BM25 search
        bm25_results: list[tuple[str, float]] = []
        if bm25_weight > 0:
            bm25_results = self._bm25.search(query, k=top_k * 3)

        # 2. Vector search — use flat similarity against all episodes
        vector_results: list[tuple[str, float]] = []
        if vector_weight > 0 and query_vector:
            vector_results = self._vector_search(query_vector, k=top_k * 3)

        # 3. RRF fusion
        lists_to_merge = []
        if bm25_results:
            lists_to_merge.append(bm25_results)
        if vector_results:
            lists_to_merge.append(vector_results)

        if not lists_to_merge:
            return []

        fused = _rrf_merge(lists_to_merge, top_n=top_k)

        # 4. Convert to EpisodeResult
        results: list[EpisodeResult] = []
        for ep_id, rrf_score in fused:
            node = self._episode_id_to_node.get(ep_id)
            if not node:
                continue
            # Find trace title
            trace_node = self._store.get_node(node.parent_id) if node.parent_id else None
            trace_title = trace_node.title if trace_node else (node.title or "")

            results.append(EpisodeResult(
                episode_id=ep_id,
                content=node.content or "",
                weight=node.weight,
                trace_title=trace_title,
                domain_id=node.domain_id or "",
                score=float(rrf_score),
                effective_score=node.weight * float(rrf_score),
            ))

        results.sort(key=lambda x: x.effective_score, reverse=True)
        return results

    def _vector_search(
        self, query_vector: list[float], k: int = 10,
    ) -> list[tuple[str, float]]:
        """Brute-force vector search against all episodes.

        Used as fallback when hierarchical routing fails.
        """
        from cc_star.hmem.indexing import EMBED_DIM
        import numpy as np
        import faiss

        episodes = self._store.get_layer_nodes(Layer.EPISODE)
        valid = [ep for ep in episodes if ep.embedding and len(ep.embedding) == EMBED_DIM]
        if not valid:
            return []

        vectors = np.array([ep.embedding for ep in valid], dtype=np.float32)
        query_arr = np.array([query_vector], dtype=np.float32)
        faiss.normalize_L2(vectors)
        faiss.normalize_L2(query_arr)

        scores = query_arr @ vectors.T
        top_k = min(k, len(valid))
        top_indices = np.argsort(scores[0])[::-1][:top_k]

        return [
            (valid[i].id, float(scores[0][i]))
            for i in top_indices
        ]

    @property
    def is_ready(self) -> bool:
        return self._built or len(self._episode_id_to_node) > 0

    def stats(self) -> dict[str, Any]:
        """Return hybrid retriever statistics."""
        return {
            "bm25_docs": self._bm25.size,
            "bm25_vocab": len(self._bm25._doc_freq) if self._built else 0,
            "cached_episodes": len(self._episode_id_to_node),
            "built": self._built,
        }
