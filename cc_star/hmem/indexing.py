"""LayerIndex — per-layer FAISS vector index for H-MEM.

Each layer (domain, category, trace, episode) gets its own FAISS flat index.
Indices are rebuilt from scratch on promote (incremental FAISS is complex and
unnecessary at cc-star scale: ~50 domains, ~500 categories, ~3000 traces).

H-MEM paper reference:
    v_i^(L) = [e_i^(L), self_index, p_i1, ..., p_iK]
    → We store self_index and sub_indices as metadata, not in the vector.
    → FAISS index holds only the semantic vector e_i^(L).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np

from cc_star.hmem.models import Layer, HierarchyNode, SearchResult

logger = logging.getLogger(__name__)

# Dimension of BGE-small-zh-v1.5 embeddings
EMBED_DIM = 512


class LayerIndex:
    """Per-layer FAISS index for a specific H-MEM layer."""

    def __init__(self, layer: str):
        self.layer = layer
        self._index: Optional["faiss.Index"] = None
        self._id_map: dict[int, str] = {}  # FAISS position → node_id
        self._node_map: dict[str, int] = {}  # node_id → FAISS position
        self._size = 0

    def build(self, nodes: list[HierarchyNode]) -> None:
        """Build or rebuild the FAISS index from a list of nodes.

        Only nodes with valid embeddings are indexed.
        """
        import faiss

        valid = [n for n in nodes if n.embedding and len(n.embedding) == EMBED_DIM]
        if not valid:
            self._index = None
            self._id_map = {}
            self._node_map = {}
            self._size = 0
            logger.warning("LayerIndex[%s]: no valid nodes to index", self.layer)
            return

        vectors = np.array([n.embedding for n in valid], dtype=np.float32)

        # Normalize for inner product search (cosine similarity equivalent)
        faiss.normalize_L2(vectors)

        self._index = faiss.IndexFlatIP(EMBED_DIM)
        self._index.add(vectors)

        self._size = len(valid)
        self._id_map = {i: n.id for i, n in enumerate(valid)}
        self._node_map = {n.id: i for i, n in enumerate(valid)}

        logger.info(
            "LayerIndex[%s]: built with %d/%d nodes",
            self.layer, self._size, len(nodes),
        )

    def search(self, query_vec: list[float], k: int = 10) -> list[SearchResult]:
        """Search top-k in this layer's index.

        Returns empty list if index is not built or query is invalid.
        """
        if self._index is None or self._size == 0:
            return []

        if query_vec is None or not isinstance(query_vec, (list, np.ndarray)) or len(query_vec) != EMBED_DIM:
            return []

        vec = np.array([query_vec], dtype=np.float32)
        import faiss
        faiss.normalize_L2(vec)

        actual_k = min(k, self._size)
        scores, indices = self._index.search(vec, actual_k)

        results: list[SearchResult] = []
        for idx, score in zip(indices[0], scores[0]):
            if idx == -1 or idx not in self._id_map:
                continue
            results.append(SearchResult(
                node_id=self._id_map[idx],
                score=float(score),
            ))
        return results

    @property
    def is_built(self) -> bool:
        """Whether this index has been built and has data."""
        return self._index is not None and self._size > 0

    @property
    def size(self) -> int:
        return self._size

    def save(self, path: Path | str) -> None:
        """Persist FAISS index to disk.

        Note: id_map and node_map are NOT saved here — they're reconstructed
        from the database on rebuild. FAISS index is the only persistent artifact.
        """
        import faiss

        if isinstance(path, str):
            path = Path(path)

        if self._index is None:
            logger.warning("LayerIndex[%s]: nothing to save", self.layer)
            return

        path.parent.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self._index, str(path))
        logger.info("LayerIndex[%s]: saved to %s", self.layer, path)

    def load(self, path: Path | str) -> bool:
        """Load FAISS index from disk."""
        import faiss

        if isinstance(path, str):
            path = Path(path)

        if not path.is_file():
            return False

        self._index = faiss.read_index(str(path))
        self._size = self._index.ntotal
        # id_map/node_map must be rebuilt separately from the database
        logger.info(
            "LayerIndex[%s]: loaded from %s (%d vectors)",
            self.layer, path, self._size,
        )
        return True

    def rebuild_id_map(self, nodes: list[HierarchyNode]) -> None:
        """Rebuild the id_map/node_map from a list of nodes (after loading index).

        Must be called after load() to make the index usable for search,
        because FAISS index doesn't store node IDs.
        """
        valid = [n for n in nodes if n.embedding and len(n.embedding) == EMBED_DIM]
        self._id_map = {i: n.id for i, n in enumerate(valid)}
        self._node_map = {n.id: i for i, n in enumerate(valid)}
        self._size = len(valid)
        logger.info(
            "LayerIndex[%s]: rebuilt id_map with %d entries",
            self.layer, self._size,
        )
