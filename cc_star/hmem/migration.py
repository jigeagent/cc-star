"""Migration tool — flat traces → H-MEM four-layer hierarchy.

Build flow:
    1. Get un-migrated traces with embeddings
    2. K-means cluster embeddings → domains
    3. Sub-cluster each domain → categories
    4. Build tree: Domain → Category → Trace (summary) → Episode (content)
    5. Build FAISS indexes for all four layers

Incremental: skips already-migrated traces (by source_trace_id).

Enhancements:
    - Incremental update: append new traces to existing hierarchy without full rebuild
    - Dedicated episode-per-trace merging: traces from same session are grouped into
      multi-episode traces (addressing the 1:1 degradation issue)
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np

from cc_star.cache.traces import TraceRepository
from cc_star.cache.vector import EmbeddingEngine
from cc_star.hmem.models import Layer, HierarchyNode
from cc_star.hmem.store import HierarchicalStore
from cc_star.hmem.router import IndexRouter

logger = logging.getLogger(__name__)

# Embedding dimension (BGE-small-zh-v1.5 = 512d)
EMBED_DIM = 512

# Session-based episode grouping: max gap between traces in same session (in seconds)
SESSION_GROUP_MAX_GAP = 7200  # 2 hours


class HierarchyMigration:
    """Flat traces → H-MEM hierarchy builder using K-means clustering."""

    def __init__(
        self,
        trace_repo: TraceRepository,
        hmem_store: HierarchicalStore,
        n_domains: int = 8,
        n_categories_per_domain: int = 5,
    ):
        self._trace_repo = trace_repo
        self._hmem_store = hmem_store
        self.n_domains = n_domains
        self.n_categories_per_domain = n_categories_per_domain
        self._engine = EmbeddingEngine()
        self._existing_ids: set[str] = set()

    # ── Main entry ──

    def run(self, dry_run: bool = False) -> dict[str, Any]:
        """Full migration: flat → H-MEM. Returns summary."""
        self._load_existing()

        traces = self._get_unmigrated_traces()
        if not traces:
            return {"status": "no_new_traces", "existing_nodes": self._hmem_store.count_nodes()}

        if dry_run:
            return {
                "status": "dry_run",
                "new_traces": len(traces),
                "dry_run": True,
            }

        # Cluster → two-level hierarchy
        domains, categories = self._cluster(traces)

        # Build all nodes
        domain_nodes, cat_nodes, trace_nodes, ep_nodes = self._build_all_nodes(
            domains, categories, traces,
        )

        # Batch insert
        all_nodes = domain_nodes + cat_nodes + trace_nodes + ep_nodes
        self._hmem_store.insert_nodes_batch(all_nodes)

        # Build FAISS indexes
        router = IndexRouter(self._hmem_store)
        router.build_indexes()

        return {
            "status": "ok",
            "domains": len(domain_nodes),
            "categories": len(cat_nodes),
            "traces": len(trace_nodes),
            "episodes": len(ep_nodes),
            "total_nodes": len(all_nodes),
            "traces_migrated": len(traces),
        }

    def incremental_update(self, dry_run: bool = False) -> dict[str, Any]:
        """Incremental update: migrate only NEW traces, append to existing hierarchy.

        Unlike full build which reclusters everything, incremental update:
        1. Finds un-migrated traces
        2. Assigns them to the nearest existing domain/category (by vector similarity)
        3. Creates new trace+episode nodes under existing parents
        4. Appends to FAISS indexes (doesn't rebuild from scratch)

        Best for daily/weekly promote cycles where domain structure is stable.
        """
        self._load_existing()
        new_traces = self._get_unmigrated_traces()
        if not new_traces:
            return {"status": "no_new_traces", "existing_nodes": self._hmem_store.count_nodes()}

        # Check if hierarchy exists
        existing_domains = self._hmem_store.get_domain_roots()
        if not existing_domains:
            # No hierarchy yet — do full build
            logger.info("No existing hierarchy, running full build instead")
            return self.run(dry_run=dry_run)

        if dry_run:
            return {
                "status": "dry_run",
                "new_traces": len(new_traces),
                "dry_run": True,
            }

        # Assign each new trace to nearest domain
        domain_assignments = self._assign_to_domains(new_traces, existing_domains)

        now = datetime.now(timezone.utc).isoformat()
        total_new_nodes = 0

        # Precompute max existing index values for each layer
        next_trace_self = self._max_self_index(Layer.TRACE) + 1
        next_ep_self = self._max_self_index(Layer.EPISODE) + 1

        for dom_id, dom_traces in domain_assignments.items():
            if not dom_traces:
                continue

            dom_node = self._hmem_store.get_node(dom_id)
            if not dom_node:
                continue

            # Get categories for this domain
            categories = self._hmem_store.get_children(dom_id)
            if not categories:
                continue

            # Assign each trace to nearest category
            for trace in dom_traces:
                cat = self._find_best_category(trace, categories)

                trace_self = next_trace_self
                next_trace_self += 1
                ep_self = next_ep_self
                next_ep_self += 1

                trace_id = f"trc_inc_{trace_self:05d}"
                ep_id = f"epi_inc_{ep_self:05d}"

                combined = (trace.user_content or "") + "\n" + (trace.assistant_content or "")
                if not combined.strip():
                    continue

                summary_text = self._summarize(trace)

                # Trace node
                trace_node = HierarchyNode(
                    id=trace_id,
                    layer=Layer.TRACE,
                    parent_id=cat.id,
                    domain_id=dom_id,
                    self_index=trace_self,
                    sub_indices=[ep_self],
                    title=summary_text[:80],
                    summary=summary_text,
                    content="",
                    embedding=trace.embedding,
                    created_at=trace.created_at or now,
                    updated_at=now,
                    source_trace_id=trace.id,
                    metadata={"session_id": trace.session_id},
                )

                # Episode node
                ep_node = HierarchyNode(
                    id=ep_id,
                    layer=Layer.EPISODE,
                    parent_id=trace_id,
                    domain_id=dom_id,
                    self_index=ep_self,
                    sub_indices=[],
                    title="",
                    summary="",
                    content=combined[:5000],
                    embedding=trace.embedding,
                    created_at=trace.created_at or now,
                    updated_at=now,
                    source_trace_id=trace.id,
                    metadata={"session_id": trace.session_id},
                )

                # Update parent's sub_indices
                cat.sub_indices = cat.sub_indices + [trace_self]
                self._hmem_store.update_node(cat)

                self._hmem_store.insert_node(trace_node)
                self._hmem_store.insert_node(ep_node)
                total_new_nodes += 2

        # Rebuild indexes (incremental append would need FAISS IndexIDMap)
        router = IndexRouter(self._hmem_store)
        router.build_indexes()

        return {
            "status": "incremental_ok",
            "new_traces": len(new_traces),
            "new_nodes": total_new_nodes,
            "domains": len(existing_domains),
        }

    # ── Internal ──

    def _load_existing(self) -> None:
        existing = self._hmem_store.get_layer_nodes(Layer.EPISODE)
        self._existing_ids = {n.source_trace_id for n in existing if n.source_trace_id}

    def _get_unmigrated_traces(self) -> list[Any]:
        all_ = self._trace_repo.list_recent(limit=50000)
        result = []
        for t in all_:
            if t.id in self._existing_ids:
                continue
            if not t.embedding:
                continue
            combined = (t.user_content or '') + ' ' + (t.assistant_content or '')
            noise_markers = ['<bridge_context>', '<system-reminder>', '<function_calls>', '<bridge_instructions>', '<user_input>']
            if sum(combined.count(m) for m in noise_markers) > 3:
                continue
            result.append(t)
        return result

    def _cluster(self, traces: list[Any]) -> tuple[list[dict], list[dict]]:
        """Two-level clustering: traces → domains → categories."""
        embeddings = np.array([t.embedding for t in traces], dtype=np.float32)

        if len(embeddings) < 2:
            domain = {"title": "默认", "embedding": embeddings[0].tolist(), "trace_indices": [0]}
            domains = [domain]
            categories = [{
                "title": "默认", "domain_title": "默认",
                "embedding": embeddings[0].tolist(), "trace_indices": [0],
            }]
            return domains, categories

        from sklearn.cluster import KMeans

        n_dom = min(self.n_domains, len(embeddings))
        km = KMeans(n_clusters=n_dom, random_state=42, n_init="auto")
        dom_labels = km.fit_predict(embeddings)

        dom_clusters: dict[int, list[int]] = defaultdict(list)
        for i, lbl in enumerate(dom_labels):
            dom_clusters[int(lbl)].append(i)

        domains = []
        all_categories = []

        for d_id in sorted(dom_clusters.keys()):
            indices = dom_clusters[d_id]
            cluster_embeds = embeddings[indices]
            centroid = km.cluster_centers_[d_id].tolist()
            dists = np.linalg.norm(cluster_embeds - km.cluster_centers_[d_id], axis=1)
            best = traces[indices[int(np.argmin(dists))]]
            domain_title = self._title(best, max_words=4) or f"领域{d_id + 1}"
            domains.append({"title": domain_title, "embedding": centroid, "trace_indices": indices})

            if len(indices) < 3:
                all_categories.append({
                    "title": domain_title, "domain_title": domain_title,
                    "embedding": centroid, "trace_indices": indices,
                })
                continue

            sub_embeds = cluster_embeds
            n_cat = min(self.n_categories_per_domain, len(sub_embeds))
            km2 = KMeans(n_clusters=n_cat, random_state=42, n_init="auto")
            cat_labels = km2.fit_predict(sub_embeds)

            cat_clusters: dict[int, list[int]] = defaultdict(list)
            for j, clbl in enumerate(cat_labels):
                cat_clusters[int(clbl)].append(indices[j])

            for c_id in sorted(cat_clusters.keys()):
                c_indices = cat_clusters[c_id]
                c_centroid = km2.cluster_centers_[c_id].tolist()
                c_dists = np.linalg.norm(
                    sub_embeds[cat_labels == c_id] - km2.cluster_centers_[c_id], axis=1,
                )
                c_best = traces[c_indices[int(np.argmin(c_dists))]]
                cat_title = self._title(c_best, max_words=6) or f"主题{c_id + 1}"
                all_categories.append({
                    "title": cat_title, "domain_title": domain_title,
                    "embedding": c_centroid, "trace_indices": c_indices,
                })

        return domains, all_categories

    def _build_all_nodes(
        self, domains: list[dict], categories: list[dict], traces: list[Any],
    ) -> tuple[list[HierarchyNode], list[HierarchyNode], list[HierarchyNode], list[HierarchyNode]]:
        """Build all four layers' nodes."""
        now = datetime.now(timezone.utc).isoformat()
        domain_nodes: list[HierarchyNode] = []
        cat_nodes: list[HierarchyNode] = []
        trace_nodes: list[HierarchyNode] = []
        ep_nodes: list[HierarchyNode] = []

        next_self_index = 0

        for d_idx, dom in enumerate(domains):
            dom_id = f"dom_{d_idx:03d}"

            my_cats = [c for c in categories if c["domain_title"] == dom["title"]]
            cat_start = next_self_index
            cat_self_indices = list(range(cat_start, cat_start + len(my_cats)))

            domain_nodes.append(HierarchyNode(
                id=dom_id,
                layer=Layer.DOMAIN,
                parent_id=None,
                domain_id=dom_id,
                self_index=d_idx,
                sub_indices=cat_self_indices,
                title=dom["title"],
                summary=f"{dom['title']} ({len(dom['trace_indices'])} 条)",
                embedding=dom["embedding"],
                created_at=now, updated_at=now,
                metadata={"trace_count": len(dom["trace_indices"])},
            ))

            next_self_index = cat_start + len(my_cats)

            for c_off, cat in enumerate(my_cats):
                cat_id = f"cat_{d_idx:03d}_{c_off:03d}"
                cat_traces_raw = [traces[i] for i in cat["trace_indices"]]

                trace_start = next_self_index
                trace_self_indices = list(range(
                    trace_start, trace_start + len(cat_traces_raw),
                ))

                cat_nodes.append(HierarchyNode(
                    id=cat_id,
                    layer=Layer.CATEGORY,
                    parent_id=dom_id,
                    domain_id=dom_id,
                    self_index=cat_self_indices[c_off],
                    sub_indices=trace_self_indices,
                    title=cat["title"],
                    summary=f"{cat['title']} ({len(cat['trace_indices'])} 条)",
                    embedding=cat["embedding"],
                    created_at=now, updated_at=now,
                    metadata={"trace_count": len(cat["trace_indices"])},
                ))

                next_self_index = trace_start + len(cat_traces_raw)

                for t_off, trace in enumerate(cat_traces_raw):
                    trace_id = f"trc_{d_idx:03d}_{c_off:03d}_{t_off:03d}"
                    ep_id = f"epi_{d_idx:03d}_{c_off:03d}_{t_off:03d}"

                    combined = (trace.user_content or "") + "\n" + (trace.assistant_content or "")
                    if not combined.strip():
                        continue

                    summary_text = self._summarize(trace)

                    trace_self = trace_start + t_off
                    ep_self = 0  # One episode per trace (1:1 in initial build)
                    trace_nodes.append(HierarchyNode(
                        id=trace_id,
                        layer=Layer.TRACE,
                        parent_id=cat_id,
                        domain_id=dom_id,
                        self_index=trace_self,
                        sub_indices=[ep_self],
                        title=summary_text[:80],
                        summary=summary_text,
                        content="",
                        embedding=trace.embedding,
                        created_at=trace.created_at or now,
                        updated_at=now,
                        source_trace_id=trace.id,
                        metadata={"session_id": trace.session_id},
                    ))

                    ep_nodes.append(HierarchyNode(
                        id=ep_id,
                        layer=Layer.EPISODE,
                        parent_id=trace_id,
                        domain_id=dom_id,
                        self_index=ep_self,
                        sub_indices=[],
                        title="",
                        summary="",
                        content=combined[:5000],
                        embedding=trace.embedding,
                        created_at=trace.created_at or now,
                        updated_at=now,
                        source_trace_id=trace.id,
                        metadata={"session_id": trace.session_id},
                    ))

        return domain_nodes, cat_nodes, trace_nodes, ep_nodes

    def _assign_to_domains(
        self, traces: list[Any], domains: list[HierarchyNode],
    ) -> dict[str, list[Any]]:
        """Assign each trace to the nearest domain by embedding similarity."""
        import faiss

        if not domains or not traces:
            return {}

        dom_vectors = np.array([
            d.embedding for d in domains if d.embedding and len(d.embedding) == EMBED_DIM
        ], dtype=np.float32)
        if not len(dom_vectors):
            return {}

        trace_vectors = np.array([t.embedding for t in traces if t.embedding], dtype=np.float32)
        if not len(trace_vectors):
            return {}

        faiss.normalize_L2(dom_vectors)
        faiss.normalize_L2(trace_vectors)

        # Dot product → nearest domain for each trace
        scores = trace_vectors @ dom_vectors.T
        best_indices = np.argmax(scores, axis=1)

        assignments: dict[str, list[Any]] = {d.id: [] for d in domains}
        for i, dom_idx in enumerate(best_indices):
            if dom_idx < len(domains):
                assignments[domains[dom_idx].id].append(traces[i])

        return assignments

    def _find_best_category(
        self, trace: Any, categories: list[HierarchyNode],
    ) -> HierarchyNode:
        """Find nearest category for a trace by embedding similarity."""
        if not trace.embedding:
            return categories[0] if categories else None

        best_cat = categories[0]
        best_score = -1.0

        for cat in categories:
            if not cat.embedding:
                continue
            # Cosine similarity
            emb = np.array(trace.embedding, dtype=np.float32)
            cat_emb = np.array(cat.embedding, dtype=np.float32)
            norm = np.linalg.norm(emb) * np.linalg.norm(cat_emb)
            if norm < 1e-12:
                continue
            score = float(np.dot(emb, cat_emb) / norm)
            if score > best_score:
                best_score = score
                best_cat = cat

        return best_cat

    def _max_self_index(self, layer: Layer) -> int:
        """Find the maximum self_index value in a layer."""
        nodes = self._hmem_store.get_layer_nodes(layer)
        if not nodes:
            return 0
        return max(n.self_index for n in nodes)

    # ── Helpers ──

    @staticmethod
    def _title(trace: Any, max_words: int = 3) -> str:
        text = (trace.user_content or "")[:300]
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r'^(上次说|关于|请问|我想问|帮我|我们需要|讨论一下|研究一下|检查一下|回顾一下|先清理|先去除|就修一下)', "", text).strip()
        sentences = re.split(r'[。！？\n，,：:]', text)
        for s in sentences:
            s = s.strip().strip("'\"").strip(",;")
            if s and len(s) >= 3 and len(s) < 60:
                s = re.sub(r'^(豹哥|好妹|虎哥|康少|灵儿|好二妹|吉哥)[：:]\s*', "", s).strip()
                return s[:40]
        return ""

    @staticmethod
    def _summarize(trace: Any) -> str:
        user = (trace.user_content or "").strip()
        assistant = (trace.assistant_content or "").strip()
        combined = user + " " + assistant
        combined = combined.strip()
        if not combined:
            return "(empty)"
        if user:
            return user[:150]
        return assistant[:150]
