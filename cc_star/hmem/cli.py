"""H-MEM CLI subcommands for cc-star.

Registered into the main cc-star CLI as the `hmem` command group.
Usage:
    cc-star hmem build              # Build hierarchy from flat traces
    cc-star hmem status             # Hierarchy status
    cc-star hmem query "..."        # Test a hierarchical retrieval
    cc-star hmem tree               # Visualize hierarchy
    cc-star hmem rebuild-index      # Rebuild FAISS indexes
    cc-star hmem decay              # Run decay scheduler
    cc-star hmem feedback           # Show feedback stats
    cc-star hmem eval               # Evaluate retrieval quality
    cc-star hmem compare            # A/B compare with baseline
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cc_star.cache.connection import CacheConnection
from cc_star.cache.schema import ensure_schema
from cc_star.cache.traces import TraceRepository
from cc_star.config import ConfigManager
from cc_star.hmem.store import HierarchicalStore
from cc_star.hmem.router import IndexRouter
from cc_star.hmem.decay import EbbinghausDecay
from cc_star.hmem.migration import HierarchyMigration
from cc_star.hmem.evaluation import HMemEvaluator


def _get_hmem_store() -> tuple[HierarchicalStore, CacheConnection]:
    """Get H-MEM store using the cc-star data directory."""
    cfg = ConfigManager()
    data_dir = cfg.data_dir
    hmem_path = data_dir / "hmem.db"
    cache = CacheConnection(str(hmem_path))
    store = HierarchicalStore(cache)
    return store, cache


def _get_trace_repo() -> tuple[TraceRepository, CacheConnection]:
    """Get the trace repository from the main cc-star cache."""
    cfg = ConfigManager()
    data_dir = cfg.data_dir
    cache_path = data_dir / "cache.db"
    cache = CacheConnection(str(cache_path))
    ensure_schema(cache)
    repo = TraceRepository(cache)
    return repo, cache


def cmd_build(args: Any) -> None:
    """Build H-MEM hierarchy from flat traces."""
    trace_repo, trace_cache = _get_trace_repo()
    hmem_store, hmem_cache = _get_hmem_store()

    dry_run = getattr(args, "dry_run", False)

    if dry_run:
        total = trace_repo.count()
        embedded = trace_repo.count_embedded()
        existing = hmem_store.count_nodes()
        result = {
            "status": "dry_run",
            "total_traces": total,
            "embedded_traces": embedded,
            "existing_hmem_nodes": existing,
            "dry_run": True,
        }
        json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
        trace_cache.close()
        hmem_cache.close()
        return

    migration = HierarchyMigration(
        trace_repo=trace_repo,
        hmem_store=hmem_store,
        n_domains=getattr(args, "domains", 8),
        n_categories_per_domain=getattr(args, "categories", 5),
    )
    result = migration.run()

    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    trace_cache.close()
    hmem_cache.close()


def cmd_status(args: Any) -> None:
    """Show H-MEM hierarchy status + hybrid retriever status."""
    hmem_store, cache = _get_hmem_store()
    router = IndexRouter(hmem_store)

    stats = hmem_store.stats()
    router_ready = router.is_ready

    result: dict[str, Any] = {
        "hmem_enabled": True,
        "hierarchy": stats,
        "indexes_ready": {
            "domain": router._indexes["domain"].is_built if hasattr(router, "_indexes") else False,
            "category": router._indexes["category"].is_built if hasattr(router, "_indexes") else False,
            "trace": router._indexes["trace"].is_built if hasattr(router, "_indexes") else False,
            "episode": router._indexes["episode"].is_built if hasattr(router, "_indexes") else False,
        },
        "router_ready": router_ready,
    }

    # Beam search status
    result["beam_search"] = "enabled (default beam_width=3)"

    # Hybrid retriever status
    try:
        if router._hybrid:
            hybrid_stats = router._hybrid.stats()
            result["hybrid_retriever"] = hybrid_stats
        else:
            result["hybrid_retriever"] = {"built": False, "note": "not initialized"}
    except Exception:
        result["hybrid_retriever"] = {"built": False}

    # Feedback stats
    try:
        feedback_count = hmem_store._count_feedback()
        result["feedback_events"] = feedback_count
    except Exception:
        pass

    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    cache.close()


def cmd_query(args: Any) -> None:
    """Test a hierarchical retrieval query (with beam search + hybrid)."""
    hmem_store, cache = _get_hmem_store()
    router = IndexRouter(hmem_store)

    query = args.query
    top_k = getattr(args, "top_k", 5)
    beam_width = getattr(args, "beam_width", 3)
    mode = getattr(args, "mode", "hybrid")  # hybrid | hierarchy | flat

    if not router.is_ready:
        print(json.dumps({
            "error": "H-MEM indexes not built. Run 'cc-star hmem build' first.",
            "hint": "cc-star hmem build",
        }, ensure_ascii=False))
        cache.close()
        return

    # Configure retrieval mode
    if mode == "flat":
        # Use pure flat search (no hierarchy)
        from cc_star.cache.vector import EmbeddingEngine
        from cc_star.hmem.indexing import EMBED_DIM

        query_vec = EmbeddingEngine().embed_query(query)
        if query_vec and len(query_vec) == EMBED_DIM:
            episodes = router._flat_fallback(query_vec, top_k, 0.0)
            result = {
                "query": query,
                "mode": "flat_global_search",
                "results": [
                    {
                        "episode_id": ep.episode_id,
                        "trace_title": ep.trace_title,
                        "effective_score": round(ep.effective_score, 4),
                        "content_preview": ep.content[:150],
                    }
                    for ep in episodes
                ],
            }
            json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
            cache.close()
            return

    if mode == "hierarchy":
        # Pure hierarchical, no hybrid, no fallback
        query_vec_result = __import__("cc_star.cache.vector", fromlist=["EmbeddingEngine"]).EmbeddingEngine().embed_query(query)
        from cc_star.hmem.indexing import EMBED_DIM

        if not query_vec_result or len(query_vec_result) != EMBED_DIM:
            print(json.dumps({"error": "query embedding failed"}, ensure_ascii=False))
            cache.close()
            return

        episodes = router._beam_search_retrieve(query, query_vec_result, top_k, 0.25, beam_width)
        result = {
            "query": query,
            "mode": "hierarchy_only",
            "beam_width": beam_width,
            "results": [
                {
                    "episode_id": ep.episode_id,
                    "trace_title": ep.trace_title,
                    "score": round(ep.score, 4),
                    "effective_score": round(ep.effective_score, 4),
                    "content_preview": ep.content[:150],
                }
                for ep in episodes
            ],
        }
        json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
        cache.close()
        return

    # Default: full hybrid mode
    episodes = router.retrieve(query, top_k=top_k, beam_width=beam_width)

    result = {
        "query": query,
        "mode": "hybrid (hierarchy + beam + fallback + bm25)",
        "num_results": len(episodes),
        "results": [
            {
                "episode_id": ep.episode_id,
                "trace_title": ep.trace_title,
                "weight": round(ep.weight, 3),
                "score": round(ep.score, 4),
                "effective_score": round(ep.effective_score, 4),
                "content_preview": ep.content[:150],
            }
            for ep in episodes
        ],
    }

    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    cache.close()


def cmd_tree(args: Any) -> None:
    """Visualize the hierarchy tree."""
    hmem_store, cache = _get_hmem_store()

    depth = getattr(args, "depth", 2)
    domains = hmem_store.get_domain_roots()

    if not domains:
        print("(empty hierarchy — run 'cc-star hmem build' first)")
        cache.close()
        return

    lines = [f"H-MEM Hierarchy ({len(domains)} domains)\n"]

    for dom in domains:
        lines.append(f"  📁 {dom.title}  [{dom.id}]")
        if depth >= 1:
            children = hmem_store.get_children(dom.id)
            for cat in children:
                lines.append(f"    ├─ 📂 {cat.title}  [{cat.id}]")
                if depth >= 2:
                    grandchildren = hmem_store.get_children(cat.id)
                    for tr in grandchildren:
                        lines.append(f"    │  ├─ 📄 {tr.title or '(no title)'}  [{tr.id}]")
                        if depth >= 3:
                            episodes = hmem_store.get_children(tr.id)
                            for ep in episodes[:3]:
                                preview = (ep.content or "")[:60].replace("\n", " ")
                                lines.append(f"    │  │  ├─ 🎬 {preview}...  [{ep.id}]")
                            if len(episodes) > 3:
                                lines.append(f"    │  │  └─ ... and {len(episodes) - 3} more")

    print("\n".join(lines))
    cache.close()


def cmd_rebuild_index(args: Any) -> None:
    """Rebuild FAISS indexes from current hierarchy data."""
    hmem_store, cache = _get_hmem_store()
    router = IndexRouter(hmem_store)
    router.build_indexes()

    result = {
        "status": "ok",
        "indexes": {
            "domain": router._indexes["domain"].size,
            "category": router._indexes["category"].size,
            "trace": router._indexes["trace"].size,
            "episode": router._indexes["episode"].size,
        },
        "hybrid_built": router._hybrid and router._hybrid.is_ready if router._hybrid else False,
    }
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    cache.close()


def cmd_decay(args: Any) -> None:
    """Run Ebbinghaus decay scheduler."""
    hmem_store, cache = _get_hmem_store()

    full = getattr(args, "full", False)
    dry_run = getattr(args, "dry_run", False)

    decay = EbbinghausDecay()
    if full:
        result = decay.run_full_decay(hmem_store, dry_run=dry_run)
    else:
        result = decay.run_scheduled_decay(hmem_store, dry_run=dry_run)

    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    cache.close()


def cmd_feedback(args: Any) -> None:
    """Show feedback statistics."""
    hmem_store, cache = _get_hmem_store()

    limit = getattr(args, "limit", 20)
    node_id = getattr(args, "node_id", None)

    if node_id:
        logs = hmem_store.get_feedback_for_node(node_id, limit=limit)
        result = {
            "node_id": node_id,
            "feedback_count": len(logs),
            "events": [
                {
                    "type": l.feedback_type,
                    "weight": f"{l.weight_before:.2f} → {l.weight_after:.2f}",
                    "session": l.session_id[:12],
                    "time": l.created_at[:19],
                    "message": (l.user_message or "")[:100],
                }
                for l in logs
            ],
        }
    else:
        from cc_star.hmem.models import Layer
        episode_nodes = hmem_store.get_layer_nodes(Layer.EPISODE)
        total_feedback = hmem_store._count_feedback()
        nodes_with_feedback = sum(
            1 for n in episode_nodes if n.approval_count > 0 or n.rebuttal_count > 0
        )
        result = {
            "total_feedback_events": total_feedback,
            "nodes_with_feedback": nodes_with_feedback,
            "total_episode_nodes": len(episode_nodes),
        }

    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    cache.close()


def cmd_eval(args: Any) -> None:
    """Evaluate H-MEM retrieval quality (recall/MRR/precision)."""
    hmem_store, cache = _get_hmem_store()
    router = IndexRouter(hmem_store)

    if not router.is_ready:
        print(json.dumps({"error": "H-MEM not built yet. Run 'cc-star hmem build' first."}))
        cache.close()
        return

    num_queries = getattr(args, "num_queries", 20)
    k_values = [1, 3, 5, 10]
    flat_baseline = getattr(args, "flat_baseline", False)

    evaluator = HMemEvaluator(hmem_store, router)
    queries = evaluator.build_test_queries(num_queries=num_queries)

    if not queries:
        print(json.dumps({"error": "Not enough episode data to build test queries."}))
        cache.close()
        return

    result = {
        "status": "ok",
        "test_queries": len(queries),
        "k_values": k_values,
        "results": {},
    }

    # Hybrid (full) mode
    hybrid_eval = evaluator.evaluate(
        queries=queries, k_values=k_values,
        router_kwargs={"enable_hybrid": True, "enable_fallback": True, "beam_width": 3},
    )
    result["results"]["hybrid"] = hybrid_eval.summary()

    # Hierarchy-only mode (no hybrid, no fallback)
    hierarchy_eval = evaluator.evaluate(
        queries=queries, k_values=k_values,
        router_kwargs={"enable_hybrid": False, "enable_fallback": False, "beam_width": 3},
    )
    result["results"]["hierarchy_only"] = hierarchy_eval.summary()

    # Single-path mode (beam_width=1, no hybrid, no fallback — old behavior)
    single_eval = evaluator.evaluate(
        queries=queries, k_values=k_values,
        router_kwargs={"enable_hybrid": False, "enable_fallback": False, "beam_width": 1},
    )
    result["results"]["single_path"] = single_eval.summary()

    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    cache.close()


def cmd_compare(args: Any) -> None:
    """A/B compare old (single-path) vs new (beam + hybrid) routing."""
    hmem_store, cache = _get_hmem_store()
    router = IndexRouter(hmem_store)

    if not router.is_ready:
        print(json.dumps({"error": "H-MEM not built yet."}))
        cache.close()
        return

    num_queries = getattr(args, "num_queries", 20)

    evaluator = HMemEvaluator(hmem_store, router)
    queries = evaluator.build_test_queries(num_queries=num_queries)

    if not queries:
        print(json.dumps({"error": "Not enough episode data."}))
        cache.close()
        return

    comparison = evaluator.compare_routers(router, router, queries=queries, num_queries=num_queries)
    json.dump(comparison, sys.stdout, indent=2, ensure_ascii=False)
    cache.close()
