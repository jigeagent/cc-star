"""H-MEM Evaluation Framework — measure retrieval quality.

Provides:
    1. recall@k / MRR / NDCG metrics
    2. Test query sets built from existing traces
    3. A/B comparison between old and new router

Usage:
    evaluator = HMemEvaluator(store, router)
    results = evaluator.evaluate(test_queries, k_values=[1, 5, 10])
    print(results.summary())
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass, field
from typing import Any, Optional

from cc_star.cache.traces import TraceRepository
from cc_star.hmem.models import Layer, EpisodeResult
from cc_star.hmem.store import HierarchicalStore
from cc_star.hmem.router import IndexRouter

logger = logging.getLogger(__name__)


@dataclass
class QuerySample:
    """A single test query with its expected relevant episode IDs."""

    query: str
    relevant_ids: list[str]  # Episode IDs that should be relevant
    source_trace_id: str = ""


@dataclass
class EvalResult:
    """Evaluation results for a single query."""

    query: str
    retrieved: list[EpisodeResult]
    relevant_ids: set[str]
    k: int

    @property
    def recall(self) -> float:
        """Recall@k: fraction of relevant results retrieved."""
        if not self.relevant_ids:
            return 0.0
        retrieved_ids = {r.episode_id for r in self.retrieved}
        hits = len(retrieved_ids & self.relevant_ids)
        return hits / len(self.relevant_ids)

    @property
    def precision(self) -> float:
        """Precision@k: fraction of retrieved results that are relevant."""
        if not self.retrieved:
            return 0.0
        retrieved_ids = {r.episode_id for r in self.retrieved}
        hits = len(retrieved_ids & self.relevant_ids)
        return hits / len(self.retrieved)

    @property
    def reciprocal_rank(self) -> float:
        """RR: 1 / rank of first relevant result (0 if none found)."""
        for rank, r in enumerate(self.retrieved, start=1):
            if r.episode_id in self.relevant_ids:
                return 1.0 / rank
        return 0.0

    @property
    def ndcg(self) -> float:
        """NDCG@k: normalized discounted cumulative gain."""
        dcg = 0.0
        idcg = 0.0
        # DCG: relevance at each rank, discounted
        for rank, r in enumerate(self.retrieved, start=1):
            rel = 1.0 if r.episode_id in self.relevant_ids else 0.0
            dcg += (2**rel - 1) / math.log2(rank + 1)

        # IDCG: perfect ranking (all relevant at top)
        num_rel = min(len(self.relevant_ids), len(self.retrieved))
        for rank in range(1, num_rel + 1):
            idcg += 1.0 / math.log2(rank + 1)

        return dcg / idcg if idcg > 0 else 0.0


@dataclass
class EvalSummary:
    """Aggregated evaluation results across all test queries."""

    num_queries: int
    k_values: list[int]
    results: dict[int, list[EvalResult]]  # k → list of per-query results

    def recall_at(self, k: int) -> float:
        """Mean recall@k across all queries."""
        if k not in self.results or not self.results[k]:
            return 0.0
        return sum(r.recall for r in self.results[k]) / len(self.results[k])

    def mrr(self, k: int) -> float:
        """Mean Reciprocal Rank@k."""
        if k not in self.results or not self.results[k]:
            return 0.0
        return sum(r.reciprocal_rank for r in self.results[k]) / len(self.results[k])

    def precision_at(self, k: int) -> float:
        """Mean precision@k."""
        if k not in self.results or not self.results[k]:
            return 0.0
        return sum(r.precision for r in self.results[k]) / len(self.results[k])

    def summary(self) -> dict[str, Any]:
        """Return full summary as a dict (for CLI output)."""
        result = {
            "num_queries": self.num_queries,
            "metrics": {},
        }
        for k in sorted(self.k_values):
            result["metrics"][f"recall@{k}"] = round(self.recall_at(k), 4)
            result["metrics"][f"mrr@{k}"] = round(self.mrr(k), 4)
            result["metrics"][f"precision@{k}"] = round(self.precision_at(k), 4)
        return result

    def __str__(self) -> str:
        d = self.summary()
        lines = [f"Evaluation: {d['num_queries']} queries"]
        for k in sorted(d["metrics"].keys()):
            lines.append(f"  {k}: {d['metrics'][k]:.4f}")
        return "\n".join(lines)


class HMemEvaluator:
    """H-MEM retrieval quality evaluator.

    Builds test queries from existing traces (using trace summaries as queries
    and their episodes as relevant results), then measures recall/MRR/NDCG.
    """

    def __init__(
        self,
        store: HierarchicalStore,
        router: IndexRouter,
        trace_repo: Optional[TraceRepository] = None,
    ):
        self._store = store
        self._router = router
        self._trace_repo = trace_repo

    def build_test_queries(
        self,
        num_queries: int = 20,
        min_content_len: int = 50,
        seed: int = 42,
    ) -> list[QuerySample]:
        """Build test queries from existing episode data.

        Strategy: use episode titles/summaries as queries and the episode
        itself as the expected relevant result.
        """
        episodes = self._store.get_layer_nodes(Layer.EPISODE)
        candidates = [
            ep for ep in episodes
            if (ep.title or ep.summary or ep.content) and len(ep.content or "") >= min_content_len
        ]

        if len(candidates) < num_queries:
            # Use all available
            selected = candidates
        else:
            rng = random.Random(seed)
            selected = rng.sample(candidates, num_queries)

        queries: list[QuerySample] = []
        for ep in selected:
            query_text = ep.title or ep.summary or ep.content[:150] or ""
            queries.append(QuerySample(
                query=query_text[:200],
                relevant_ids=[ep.id],
                source_trace_id=ep.source_trace_id or "",
            ))

        return queries

    def evaluate(
        self,
        queries: Optional[list[QuerySample]] = None,
        num_queries: int = 20,
        k_values: Optional[list[int]] = None,
        router_kwargs: Optional[dict] = None,
        flat_baseline: bool = False,
    ) -> EvalSummary:
        """Run evaluation.

        Args:
            queries: Pre-built test queries (or None to auto-build).
            num_queries: Number of queries to auto-build.
            k_values: k values for recall@k / MRR@k.
            router_kwargs: Additional kwargs to pass to router.retrieve().
            flat_baseline: If True, compare against flat (non-hierarchical) search.

        Returns:
            EvalSummary with per-k results.
        """
        if k_values is None:
            k_values = [1, 3, 5, 10]
        if router_kwargs is None:
            router_kwargs = {}

        if queries is None:
            queries = self.build_test_queries(num_queries=num_queries)

        if not queries:
            logger.warning("No test queries available for evaluation")
            return EvalSummary(0, k_values, {})

        results: dict[int, list[EvalResult]] = {k: [] for k in k_values}

        for q in queries:
            if not q.query.strip():
                continue

            # Retrieve using router
            try:
                retrieved = self._router.retrieve(q.query, top_k=max(k_values), **router_kwargs)
            except Exception as e:
                logger.warning("Query failed '%s': %s", q.query[:50], e)
                retrieved = []

            relevant_set = set(q.relevant_ids)

            for k in k_values:
                top_k_results = retrieved[:k]
                results[k].append(EvalResult(
                    query=q.query,
                    retrieved=top_k_results,
                    relevant_ids=relevant_set,
                    k=k,
                ))

        return EvalSummary(len(queries), k_values, results)

    def compare_routers(
        self,
        old_router: IndexRouter,
        new_router: IndexRouter,
        queries: Optional[list[QuerySample]] = None,
        num_queries: int = 20,
        k_values: Optional[list[int]] = None,
    ) -> dict[str, Any]:
        """A/B comparison between two router configurations.

        Returns:
            Dict with 'old' and 'new' summaries plus 'improvements'.
        """
        if queries is None:
            queries = self.build_test_queries(num_queries=num_queries)
        if k_values is None:
            k_values = [1, 3, 5, 10]

        old_results = self.evaluate(
            queries=queries, k_values=k_values,
            router_kwargs={"enable_hybrid": False, "enable_fallback": False, "beam_width": 1},
        )
        new_results = self.evaluate(
            queries=queries, k_values=k_values,
        )

        improvements = {}
        for k in k_values:
            old_r = old_results.recall_at(k)
            new_r = new_results.recall_at(k)
            improvements[f"recall@{k}"] = {
                "old": round(old_r, 4),
                "new": round(new_r, 4),
                "delta": round(new_r - old_r, 4),
                "pct": f"{((new_r - old_r) / max(old_r, 0.001)) * 100:+.1f}%",
            }

        return {
            "num_queries": len(queries),
            "old_router": old_results.summary(),
            "new_router": new_results.summary(),
            "improvements": improvements,
        }
