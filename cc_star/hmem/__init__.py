"""H-MEM: Hierarchical Memory for cc-star.

Full implementation of the H-MEM paper (Sun & Zeng, arXiv:2507.22925):
- Four-layer semantic hierarchy (Domain → Category → Trace → Episode)
- Index-based routing with FAISS per-layer search
- Beam search routing (multi-path, no single-point failure)
- BM25 + Vector hybrid search with RRF fusion
- Global flat fallback when hierarchy is insufficient
- Ebbinghaus forgetting curve + user-feedback-driven weight regulation
- Evaluation framework with recall@k, MRR, precision metrics
"""

from cc_star.hmem.models import (
    Layer,
    HierarchyNode,
    SearchResult,
    EpisodeResult,
    FeedbackType,
    FeedbackLog,
    DecayConfig,
)
from cc_star.hmem.store import HierarchicalStore
from cc_star.hmem.indexing import LayerIndex
from cc_star.hmem.router import IndexRouter
from cc_star.hmem.hybrid import HybridRetriever
from cc_star.hmem.decay import EbbinghausDecay
from cc_star.hmem.feedback import FeedbackProcessor
from cc_star.hmem.evaluation import HMemEvaluator, EvalSummary, QuerySample

__all__ = [
    "Layer",
    "HierarchyNode",
    "SearchResult",
    "EpisodeResult",
    "FeedbackType",
    "FeedbackLog",
    "DecayConfig",
    "HierarchicalStore",
    "LayerIndex",
    "IndexRouter",
    "HybridRetriever",
    "EbbinghausDecay",
    "FeedbackProcessor",
    "HMemEvaluator",
    "EvalSummary",
    "QuerySample",
]
