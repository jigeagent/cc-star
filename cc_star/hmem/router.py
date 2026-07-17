"""IndexRouter — layer-by-layer retrieval with H-MEM index-based routing.

Implements the paper's core algorithm with key enhancements:
    1. Beam search: keep Top-N candidates at each layer, not just one
    2. Global flat fallback: when hierarchy fails, search all episodes directly
    3. Hybrid search: BM25 reranking of hierarchical results

Core algorithm:
    1. Encode query → semantic vector
    2. FAISS search @ Domain Layer (Top-K beam)
    3. Route via sub_indices → Category Layer (search children of ALL beam candidates)
    4. Route via sub_indices → Trace Layer
    5. Retrieve Episode Layer content
    6. Sort by weight × similarity
    7. Fallback: if hierarchy returns < top_k, supplement with flat global search
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np

from cc_star.hmem.models import Layer, HierarchyNode, SearchResult, EpisodeResult
from cc_star.hmem.store import HierarchicalStore
from cc_star.hmem.indexing import LayerIndex, EMBED_DIM
from cc_star.hmem.hybrid import HybridRetriever
from cc_star.cache.vector import EmbeddingEngine

logger = logging.getLogger(__name__)


class IndexRouter:
    """Index-based routing engine — layer-by-layer retrieval with beam search.

    Key improvements over the basic single-path algorithm:
    - Beam search: keeps Top-N at each layer (not just Top-1)
    - Global fallback: flat search when hierarchy yields insufficient results
    - Hybrid reranking: optional BM25 blending via HybridRetriever

    Usage:
        router = IndexRouter(store)
        router.build_indexes()          # Build FAISS indexes from store
        results = router.retrieve(query)  # Retrieve episodes
    """

    def __init__(self, store: HierarchicalStore):
        self._store = store
        self._indexes: dict[str, LayerIndex] = {
            Layer.DOMAIN: LayerIndex(Layer.DOMAIN),
            Layer.CATEGORY: LayerIndex(Layer.CATEGORY),
            Layer.TRACE: LayerIndex(Layer.TRACE),
            Layer.EPISODE: LayerIndex(Layer.EPISODE),
        }
        self._hybrid: Optional[HybridRetriever] = None
        self._built = False

        # ── In-memory caches (invalidated on build_indexes) ──
        self._cache_valid = False
        self._cached_episodes: list[HierarchyNode] = []
        self._cached_valid_episodes: list[HierarchyNode] = []  # with valid embedding + content
        self._cached_vectors: Optional[np.ndarray] = None       # (N, EMBED_DIM) float32
        self._cached_trace_map: dict[str, HierarchyNode] = {}   # node_id → trace node

    # ── Index management ──

    def _build_cache(self) -> None:
        """Build all in-memory caches from store data."""
        # Cache episode nodes
        self._cached_episodes = self._store.get_layer_nodes(Layer.EPISODE)
        self._cached_valid_episodes = [
            ep for ep in self._cached_episodes
            if ep.embedding and len(ep.embedding) == EMBED_DIM and ep.content
        ]
        if self._cached_valid_episodes:
            self._cached_vectors = np.array(
                [ep.embedding for ep in self._cached_valid_episodes], dtype=np.float32,
            )
        else:
            self._cached_vectors = None

        # Cache trace node lookup
        traces = self._store.get_layer_nodes(Layer.TRACE)
        self._cached_trace_map = {t.id: t for t in traces}

        self._cache_valid = True
        logger.info(
            "Router cache: %d episodes (%d valid), %d traces",
            len(self._cached_episodes), len(self._cached_valid_episodes), len(traces),
        )

    def _invalidate_cache(self) -> None:
        """Invalidate in-memory caches (called when data may have changed)."""
        self._cache_valid = False
        self._cached_episodes = []
        self._cached_valid_episodes = []
        self._cached_vectors = None
        self._cached_trace_map = {}

    def _ensure_cache(self) -> None:
        """Lazy-build cache if invalid."""
        if not self._cache_valid:
            self._build_cache()

    def build_indexes(self) -> None:
        """Build all four FAISS indexes from current store data."""
        for layer in Layer:
            nodes = self._store.get_layer_nodes(layer)
            self._indexes[layer].build(nodes)
        self._built = True

        # Build in-memory caches
        self._build_cache()

        # Lazy-init hybrid retriever
        if self._hybrid is None:
            try:
                self._hybrid = HybridRetriever(self._store)
                self._hybrid.build()
            except Exception as e:
                logger.warning("HybridRetriever init failed (non-fatal): %s", e)

        logger.info(
            "IndexRouter: all 4 indexes built — %s",
            {l: self._indexes[l].size for l in Layer},
        )

    def ensure_indexes(self) -> None:
        """Lazy-build indexes if not yet built."""
        if not self._built:
            self.build_indexes()

    @property
    def is_ready(self) -> bool:
        """Check if at least the Domain index is populated."""
        self.ensure_indexes()
        return self._indexes[Layer.DOMAIN].is_built

    # ── Core retrieval ──

    def retrieve(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.25,
        beam_width: int = 3,
        enable_hybrid: bool = True,
        enable_fallback: bool = True,
    ) -> list[EpisodeResult]:
        """Hierarchical retrieval with beam search + fallback.

        Args:
            query: Natural language query string.
            top_k: Number of final results.
            min_score: Minimum similarity score to consider relevant.
            beam_width: How many candidates to keep at each layer (>=1).
                        Larger = more coverage but slower.
            enable_hybrid: Whether to use BM25 for reranking.
            enable_fallback: Whether to fallback to flat global search.

        Returns:
            Ranked list of EpisodeResult sorted by effective_score descending.
        """
        self.ensure_indexes()

        query_vec = EmbeddingEngine().embed_query(query)
        if query_vec is None or len(query_vec) != EMBED_DIM:
            return []

        # ── Phase 1: Hierarchical beam search ──
        hierarchy_results = self._beam_search_retrieve(
            query, query_vec, top_k, min_score, beam_width,
        )

        # ── Phase 2: Fallback — supplement with flat global search ──
        if enable_fallback and len(hierarchy_results) < top_k:
            fallback_k = top_k - len(hierarchy_results)
            fallback = self._flat_fallback(query_vec, fallback_k, min_score)

            # Merge, dedup by episode_id
            seen_ids = {r.episode_id for r in hierarchy_results}
            for fr in fallback:
                if fr.episode_id not in seen_ids:
                    hierarchy_results.append(fr)
                    seen_ids.add(fr.episode_id)

        # ── Phase 3: Hybrid reranking (BM25 reorder) ──
        if enable_hybrid and self._hybrid and self._hybrid.is_ready:
            hierarchy_results = self._hybrid_rerank(
                query, query_vec, hierarchy_results, top_k,
            )

        # Final sort and trim
        hierarchy_results.sort(key=lambda x: x.effective_score, reverse=True)
        return hierarchy_results[:top_k]

    # ── Beam search ──

    def _beam_search_retrieve(
        self,
        query: str,
        query_vec: list[float],
        top_k: int,
        min_score: float,
        beam_width: int,
    ) -> list[EpisodeResult]:
        """Beam search through the hierarchy.

        At each layer, keep up to beam_width candidates.
        This prevents single-path error propagation.
        """
        # Step 1: Domain layer — search all
        domain_results = self._search_layer(Layer.DOMAIN, query_vec, k=beam_width)
        domain_results = [r for r in domain_results if r.score >= min_score]
        if not domain_results:
            return []

        # Step 2: Category layer — search children of ALL domain candidates
        category_results = self._route_and_search(
            domain_results, Layer.CATEGORY, query_vec, k=beam_width,
        )
        if not category_results:
            return []

        # Step 3: Trace layer — search children of ALL category candidates
        trace_results = self._route_and_search(
            category_results, Layer.TRACE, query_vec, k=top_k * 2,
        )
        if not trace_results:
            return []

        # Step 4: Fetch episode content
        episodes = self._fetch_episodes(trace_results, top_k=top_k * 2)
        return episodes

    # ── Flat fallback ──

    def _flat_fallback(
        self,
        query_vec: list[float],
        top_k: int,
        min_score: float,
    ) -> list[EpisodeResult]:
        """Flat global search: search ALL episodes directly (cached).

        Uses in-memory cached vectors to avoid SQLite + numpy rebuild per query.
        """
        self._ensure_cache()

        import faiss

        valid = self._cached_valid_episodes
        vectors = self._cached_vectors
        if not valid or vectors is None:
            return []

        query_arr = np.array([query_vec], dtype=np.float32)
        faiss.normalize_L2(vectors)
        faiss.normalize_L2(query_arr)

        scores = query_arr @ vectors.T
        top_k_actual = min(top_k * 2, len(valid))
        top_indices = np.argsort(scores[0])[::-1][:top_k_actual]

        results: list[EpisodeResult] = []
        for i in top_indices:
            ep = valid[i]
            if scores[0][i] < min_score:
                continue
            trace_node = self._cached_trace_map.get(ep.parent_id) if ep.parent_id else None

            results.append(EpisodeResult(
                episode_id=ep.id,
                content=ep.content,
                weight=ep.weight,
                trace_title=trace_node.title if trace_node else "",
                domain_id=ep.domain_id or "",
                score=float(scores[0][i]),
                effective_score=ep.weight * float(scores[0][i]),
            ))

        results.sort(key=lambda x: x.effective_score, reverse=True)
        return results[:top_k]

    # ── Hybrid reranking ──

    def _hybrid_rerank(
        self,
        query: str,
        query_vec: list[float],
        candidates: list[EpisodeResult],
        top_k: int,
    ) -> list[EpisodeResult]:
        """Rerank hierarchical results using BM25 signal.

        Takes candidates from hierarchy, scores them with BM25,
        then fuses the BM25 score with the existing vector score.
        """
        if not self._hybrid or not candidates:
            return candidates

        # Get BM25 scores for candidates
        bm25_results = dict(self._hybrid._bm25.search(query, k=top_k * 3))

        # Blend scores
        for ep in candidates:
            bm25_score = bm25_results.get(ep.episode_id, 0.0)
            # Fuse: 0.6 * vector + 0.4 * normalized BM25
            fused = 0.6 * ep.score + 0.4 * min(bm25_score, 1.0)
            ep.effective_score = ep.weight * fused

        candidates.sort(key=lambda x: x.effective_score, reverse=True)
        return candidates

    # ── Single-layer search helpers ──

    def _search_layer(
        self, layer: Layer, query_vec: list[float], k: int,
    ) -> list[SearchResult]:
        """Search a single layer's FAISS index."""
        index = self._indexes.get(layer)
        if not index or not index.is_built:
            return []
        return index.search(query_vec, k=k)

    def _route_and_search(
        self,
        parents: list[SearchResult],
        child_layer: Layer,
        query_vec: list[float],
        k: int,
    ) -> list[SearchResult]:
        """Route from parent results to child layer via sub_indices, then search.

        Key efficiency trick: instead of searching the entire child layer,
        we only search the subset that the parents' sub_indices point to.
        Uses cached trace/domain/category nodes when possible.
        """
        # Collect all child indices from ALL parent nodes
        all_child_indices: set[int] = set()
        for parent in parents:
            # Try cache first (for trace layer), fallback to SQLite
            node = self._cached_trace_map.get(parent.node_id) if child_layer == Layer.TRACE else None
            if node is None:
                node = self._store.get_node(parent.node_id)
            if node and node.sub_indices:
                all_child_indices.update(node.sub_indices)

        if not all_child_indices:
            return []

        # Fetch child nodes by their self_index values
        children = self._store.get_nodes_by_indices(
            child_layer, list(all_child_indices),
        )
        if not children:
            return []

        # Search only within this subset (numpy dot product — fast for subsets)
        return self._search_subset(children, query_vec, k=k)

    def _search_subset(
        self,
        candidates: list[HierarchyNode],
        query_vec: list[float],
        k: int,
    ) -> list[SearchResult]:
        """Search within a small subset of nodes using numpy dot product."""
        import faiss

        vectors = np.array([c.embedding for c in candidates], dtype=np.float32)
        query_arr = np.array([query_vec], dtype=np.float32)
        faiss.normalize_L2(vectors)
        faiss.normalize_L2(query_arr)

        scores = query_arr @ vectors.T
        top_k = min(k, len(candidates))
        top_indices = np.argsort(scores[0])[::-1][:top_k]

        return [
            SearchResult(
                node_id=candidates[i].id,
                score=float(scores[0][i]),
            )
            for i in top_indices
        ]

    def _fetch_episodes(
        self,
        trace_results: list[SearchResult],
        top_k: int,
    ) -> list[EpisodeResult]:
        """Retrieve episode content from trace node children.

        Follows trace sub_indices to episode nodes, collects content,
        sorts by effective_score = weight × similarity.
        Uses in-memory caches to avoid SQLite lookups.
        """
        episodes: list[EpisodeResult] = []

        for tr in trace_results:
            # Use cached trace map instead of SQLite get_node()
            trace_node = self._cached_trace_map.get(tr.node_id)
            if not trace_node or not trace_node.sub_indices:
                continue

            # Use cached episodes for child lookup
            child_indices_set = set(trace_node.sub_indices)
            matched = [ep for ep in self._cached_episodes if ep.self_index in child_indices_set and ep.parent_id == trace_node.id]
            for ep in matched:
                if not ep or not ep.content:
                    continue
                episodes.append(EpisodeResult(
                    episode_id=ep.id,
                    content=ep.content,
                    weight=ep.weight,
                    trace_title=trace_node.title,
                    domain_id=ep.domain_id or "",
                    score=tr.score,
                    effective_score=ep.weight * tr.score,
                ))

        episodes.sort(key=lambda x: x.effective_score, reverse=True)
        return episodes[:top_k]

    # ── Utility: debug ──

    def retrieve_debug(
        self, query: str, top_k: int = 3,
    ) -> dict:
        """Same as retrieve() but returns debug info for diagnostics.

        Returns dict with retrieval_path (each layer's results) and final episodes.
        """
        self.ensure_indexes()
        query_vec = EmbeddingEngine().embed_query(query)

        debug: dict = {
            "query": query,
            "embedding_dim": len(query_vec) if query_vec is not None else 0,
            "path": [],
            "beam_width": 3,  # default beam width for debug
            "hybrid_enabled": self._hybrid is not None and self._hybrid.is_ready if self._hybrid else False,
        }

        if query_vec is None:
            return debug

        # Domain
        domain_results = self._search_layer(Layer.DOMAIN, query_vec, k=3)
        debug["path"].append({
            "layer": "domain",
            "results": [
                {"node_id": r.node_id, "score": round(r.score, 4)}
                for r in domain_results
            ],
        })

        if not domain_results:
            debug["final"] = []
            debug["fallback_used"] = False
            return debug

        # Category
        cat_results = self._route_and_search(
            domain_results, Layer.CATEGORY, query_vec, k=3,
        )
        debug["path"].append({
            "layer": "category",
            "results": [
                {"node_id": r.node_id, "score": round(r.score, 4)}
                for r in cat_results
            ],
        })

        # Trace
        trace_results = self._route_and_search(
            cat_results, Layer.TRACE, query_vec, k=top_k,
        )
        debug["path"].append({
            "layer": "trace",
            "results": [
                {"node_id": r.node_id, "score": round(r.score, 4)}
                for r in trace_results
            ],
        })

        # Final episodes
        episodes = self._fetch_episodes(trace_results, top_k)
        debug["final"] = [
            {
                "episode_id": ep.episode_id,
                "trace_title": ep.trace_title,
                "weight": round(ep.weight, 3),
                "score": round(ep.score, 4),
                "effective_score": round(ep.effective_score, 4),
                "content_preview": ep.content[:120],
            }
            for ep in episodes
        ]

        # If hierarchy returned thin results, log fallback availability
        if len(episodes) < top_k:
            debug["fallback_available"] = True
            self._ensure_cache()
            # Run flat fallback to show what it would find
            fallback = self._flat_fallback(query_vec, top_k, 0.25)
            debug["fallback_results"] = len(fallback)
            debug["fallback_samples"] = [
                {
                    "episode_id": ep.episode_id,
                    "score": round(ep.score, 4),
                    "content_preview": ep.content[:100],
                }
                for ep in fallback[:3]
            ]
        else:
            debug["fallback_used"] = False

        return debug
