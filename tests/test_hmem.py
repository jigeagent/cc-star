"""Tests for H-MEM hierarchical memory module."""

from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import datetime, timezone

import numpy as np
import pytest

from cc_star.cache.connection import CacheConnection
from cc_star.cache.vector import EmbeddingEngine
from cc_star.hmem.models import (
    Layer, HierarchyNode, SearchResult, EpisodeResult,
    FeedbackType, FeedbackLog, DecayConfig,
)
from cc_star.hmem.store import HierarchicalStore
from cc_star.hmem.indexing import LayerIndex, EMBED_DIM


# ═══════════════════════════════════════════════════════════════════
# 1. Data Models
# ═══════════════════════════════════════════════════════════════════

class TestHierarchyNode:
    def test_create_domain_node(self):
        node = HierarchyNode(
            id="dom_001",
            layer=Layer.DOMAIN,
            title="项目管理",
            embedding=[0.1] * EMBED_DIM,
        )
        assert node.layer == Layer.DOMAIN
        assert node.weight == 1.0
        assert node.access_count == 0
        assert node.effective_weight == 1.0

    def test_create_episode_node(self):
        node = HierarchyNode(
            id="epi_001",
            layer=Layer.EPISODE,
            content="关于Sprint规划的详细讨论...",
            parent_id="trc_001",
        )
        assert node.layer == Layer.EPISODE
        assert node.content == "关于Sprint规划的详细讨论..."

    def test_to_row_and_back(self):
        n1 = HierarchyNode(
            id="test_001",
            layer=Layer.CATEGORY,
            parent_id="dom_001",
            self_index=5,
            sub_indices=[0, 1, 2],
            title="Test",
            summary="Summary",
            content="Content",
            embedding=[0.5] * EMBED_DIM,
            weight=0.8,
            access_count=3,
            approval_count=1,
            metadata={"source": "test"},
        )
        row = n1.to_row()
        assert row[0] == "test_001"
        assert row[1] == "category"
        assert json.loads(row[5]) == [0, 1, 2]

    def test_touch_updates_access_count(self):
        node = HierarchyNode(id="t1", layer=Layer.EPISODE)
        old_count = node.access_count
        node.touch()
        assert node.access_count == old_count + 1
        assert node.last_accessed_at != ""

    def test_effective_weight_min_floor(self):
        node = HierarchyNode(id="t1", layer=Layer.EPISODE, weight=0.001)
        assert node.effective_weight == 0.01

    def test_feedback_type_enum(self):
        assert FeedbackType.APPROVAL.value == "approval"
        assert FeedbackType.REBUTTAL.value == "rebuttal"
        assert FeedbackType.NO_FEEDBACK.value == "no_feedback"


class TestFeedbackLog:
    def test_create_and_roundtrip(self):
        log = FeedbackLog(
            id="fl_001",
            node_id="epi_001",
            feedback_type=FeedbackType.APPROVAL,
            session_id="sess_001",
            user_message="对，就是这样",
            weight_before=0.5,
            weight_after=0.6,
            created_at="2026-07-15T10:00:00Z",
        )
        assert log.feedback_type == FeedbackType.APPROVAL
        assert log.weight_before == 0.5
        row = log.to_row()
        assert row[0] == "fl_001"
        assert row[2] == "approval"


# ═══════════════════════════════════════════════════════════════════
# 2. HierarchicalStore
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def hmem_store():
    """Create a temporary H-MEM store for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    cache = CacheConnection(db_path)
    store = HierarchicalStore(cache)
    yield store
    cache.close()
    os.unlink(db_path)


class TestHierarchicalStore:
    def test_insert_and_get_node(self, hmem_store):
        node = HierarchyNode(
            id="dom_001", layer=Layer.DOMAIN,
            title="Test Domain",
            embedding=[0.1] * EMBED_DIM,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        hmem_store.insert_node(node)

        fetched = hmem_store.get_node("dom_001")
        assert fetched is not None
        assert fetched.id == "dom_001"
        assert fetched.title == "Test Domain"
        assert fetched.layer == Layer.DOMAIN

    def test_insert_and_get_children(self, hmem_store):
        # Create parent
        parent = HierarchyNode(
            id="dom_001", layer=Layer.DOMAIN,
            self_index=0,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        hmem_store.insert_node(parent)

        # Create children
        for i in range(3):
            child = HierarchyNode(
                id=f"cat_{i:03d}", layer=Layer.CATEGORY,
                parent_id="dom_001",
                self_index=i + 1,
                created_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:00:00Z",
            )
            hmem_store.insert_node(child)

        children = hmem_store.get_children("dom_001")
        assert len(children) == 3
        assert all(c.layer == Layer.CATEGORY for c in children)
        assert all(c.parent_id == "dom_001" for c in children)

    def test_get_layer_nodes(self, hmem_store):
        for i in range(5):
            n = HierarchyNode(
                id=f"epi_{i:03d}", layer=Layer.EPISODE,
                self_index=i,
                created_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:00:00Z",
            )
            hmem_store.insert_node(n)

        nodes = hmem_store.get_layer_nodes(Layer.EPISODE)
        assert len(nodes) == 5

    def test_get_nodes_by_indices(self, hmem_store):
        for i in range(5):
            n = HierarchyNode(
                id=f"n_{i:03d}", layer=Layer.CATEGORY,
                self_index=i,
                created_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:00:00Z",
            )
            hmem_store.insert_node(n)

        found = hmem_store.get_nodes_by_indices(Layer.CATEGORY, [0, 2, 4])
        assert len(found) == 3
        ids = [n.id for n in found]
        assert "n_000" in ids
        assert "n_002" in ids
        assert "n_004" in ids

    def test_update_node(self, hmem_store):
        node = HierarchyNode(
            id="t1", layer=Layer.EPISODE,
            title="Old",
            weight=0.5,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        hmem_store.insert_node(node)

        node.title = "New Title"
        node.weight = 0.9
        hmem_store.update_node(node)

        fetched = hmem_store.get_node("t1")
        assert fetched.title == "New Title"
        assert fetched.weight == 0.9

    def test_update_weight(self, hmem_store):
        node = HierarchyNode(
            id="t1", layer=Layer.EPISODE,
            weight=0.5,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        hmem_store.insert_node(node)
        hmem_store.update_weight("t1", 0.3)

        fetched = hmem_store.get_node("t1")
        assert fetched.weight == 0.3

    def test_count_nodes(self, hmem_store):
        for i in range(3):
            n = HierarchyNode(
                id=f"d_{i:03d}", layer=Layer.DOMAIN,
                self_index=i,
                created_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:00:00Z",
            )
            hmem_store.insert_node(n)
        for i in range(7):
            n = HierarchyNode(
                id=f"e_{i:03d}", layer=Layer.EPISODE,
                self_index=i,
                created_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:00:00Z",
            )
            hmem_store.insert_node(n)

        assert hmem_store.count_nodes() == 10
        assert hmem_store.count_nodes(Layer.DOMAIN) == 3
        assert hmem_store.count_nodes(Layer.EPISODE) == 7

    def test_insert_feedback(self, hmem_store):
        node = HierarchyNode(
            id="epi_001", layer=Layer.EPISODE,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        hmem_store.insert_node(node)

        log = FeedbackLog(
            id="fl_001", node_id="epi_001",
            feedback_type=FeedbackType.APPROVAL,
            session_id="s1",
            created_at="2026-01-01T00:00:00Z",
        )
        hmem_store.insert_feedback(log)

        logs = hmem_store.get_feedback_for_node("epi_001")
        assert len(logs) == 1
        assert logs[0].feedback_type == FeedbackType.APPROVAL

    def test_config_kv(self, hmem_store):
        hmem_store.set_config("test_key", "hello")
        assert hmem_store.get_config("test_key") == "hello"

        hmem_store.set_config("nested", {"a": 1, "b": 2})
        assert hmem_store.get_config("nested") == {"a": 1, "b": 2}

        assert hmem_store.get_config("nonexistent", 42) == 42

    def test_batch_update_weights(self, hmem_store):
        for i in range(3):
            n = HierarchyNode(
                id=f"n_{i:03d}", layer=Layer.EPISODE,
                weight=1.0, access_count=0,
                created_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:00:00Z",
            )
            hmem_store.insert_node(n)

        now = datetime.now(timezone.utc).isoformat()
        updates = [
            (0.5, 1, now, now, "n_000"),
            (0.3, 2, now, now, "n_001"),
            (0.7, 3, now, now, "n_002"),
        ]
        hmem_store.batch_update_weights(updates)

        assert hmem_store.get_node("n_000").weight == 0.5
        assert hmem_store.get_node("n_001").weight == 0.3
        assert hmem_store.get_node("n_002").weight == 0.7

    def test_stats(self, hmem_store):
        for i in range(2):
            hmem_store.insert_node(HierarchyNode(
                id=f"d_{i:03d}", layer=Layer.DOMAIN,
                self_index=i,
                created_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:00:00Z",
            ))
        for i in range(5):
            hmem_store.insert_node(HierarchyNode(
                id=f"e_{i:03d}", layer=Layer.EPISODE,
                self_index=i,
                created_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:00:00Z",
            ))

        stats = hmem_store.stats()
        assert stats["domains"] == 2
        assert stats["episodes"] == 5
        assert stats["total"] == 7


# ═══════════════════════════════════════════════════════════════════
# 3. LayerIndex (FAISS)
# ═══════════════════════════════════════════════════════════════════

class TestLayerIndex:
    def test_build_and_search(self):
        """Build a FAISS index with synthetic vectors and search it."""
        index = LayerIndex(Layer.DOMAIN)

        nodes = [
            HierarchyNode(id=f"n_{i}", layer=Layer.DOMAIN,
                          embedding=_normalized_vec(i))
            for i in range(10)
        ]
        index.build(nodes)
        assert index.is_built
        assert index.size == 10

        # Search with the first node's vector
        query = _normalized_vec(0)
        results = index.search(query, k=3)
        assert len(results) == 3
        # First result should be the same node (score ~= 1.0)
        assert results[0].node_id == "n_0"
        assert abs(results[0].score - 1.0) < 0.01

    def test_empty_index(self):
        index = LayerIndex(Layer.DOMAIN)
        assert not index.is_built
        assert index.search([0.1] * EMBED_DIM) == []

    def test_build_no_valid_embeddings(self):
        nodes = [HierarchyNode(id="n1", layer=Layer.DOMAIN)]
        index = LayerIndex(Layer.DOMAIN)
        index.build(nodes)
        assert not index.is_built

    def test_save_and_load(self):
        import tempfile, os
        index = LayerIndex(Layer.CATEGORY)
        nodes = [
            HierarchyNode(id=f"n_{i}", layer=Layer.CATEGORY,
                          embedding=_normalized_vec(i))
            for i in range(5)
        ]
        index.build(nodes)

        with tempfile.NamedTemporaryFile(suffix=".faiss", delete=False) as f:
            path = f.name
        try:
            index.save(path)
            assert os.path.getsize(path) > 0

            # Load into new index
            index2 = LayerIndex(Layer.CATEGORY)
            loaded = index2.load(path)
            assert loaded
            assert index2.size == 5
            # Need to rebuild id_map (stored separately from FAISS file)
            index2.rebuild_id_map(nodes)
            assert index2.is_built

            query = _normalized_vec(0)
            results = index2.search(query, k=3)
            assert len(results) > 0
        finally:
            os.unlink(path)


# ═══════════════════════════════════════════════════════════════════
# 4. Router (integration with store)
# ═══════════════════════════════════════════════════════════════════

class TestRouterIntegration:
    @pytest.fixture
    def store_with_hierarchy(self, hmem_store):
        """Build a test hierarchy: 2 domains, 3 categories, traces, episodes.
        Uses the real EmbeddingEngine so vectors match real queries.
        """
        from cc_star.cache.vector import EmbeddingEngine
        engine = EmbeddingEngine()
        now = "2026-01-01T00:00:00Z"

        # Use real embeddings for topics
        dom0_emb = engine.embed_query("项目管理")
        dom1_emb = engine.embed_query("技术架构")
        cat00_emb = engine.embed_query("敏捷开发")
        cat01_emb = engine.embed_query("风险管理")
        cat10_emb = engine.embed_query("系统设计")
        tr_emb_a = engine.embed_query("Sprint规划讨论")
        tr_emb_b = engine.embed_query("Story Point估算")
        tr_emb_c = engine.embed_query("风险评估流程")

        # Domain 0 — 项目管理
        hmem_store.insert_node(HierarchyNode(
            id="dom_000", layer=Layer.DOMAIN,
            self_index=0, sub_indices=[1, 2],
            title="项目管理",
            embedding=dom0_emb,
            created_at=now, updated_at=now,
        ))
        # Category 0-0
        hmem_store.insert_node(HierarchyNode(
            id="cat_000_000", layer=Layer.CATEGORY,
            parent_id="dom_000", domain_id="dom_000",
            self_index=1, sub_indices=[3, 4],
            title="敏捷开发",
            embedding=cat00_emb,
            created_at=now, updated_at=now,
        ))
        # Category 0-1
        hmem_store.insert_node(HierarchyNode(
            id="cat_000_001", layer=Layer.CATEGORY,
            parent_id="dom_000", domain_id="dom_000",
            self_index=2, sub_indices=[5],
            title="风险管理",
            embedding=cat01_emb,
            created_at=now, updated_at=now,
        ))

        # Trace 0-0-0
        hmem_store.insert_node(HierarchyNode(
            id="trc_000_000_000", layer=Layer.TRACE,
            parent_id="cat_000_000", domain_id="dom_000",
            self_index=3, sub_indices=[0],
            title="Sprint规划讨论",
            embedding=tr_emb_a,
            created_at=now, updated_at=now,
        ))
        hmem_store.insert_node(HierarchyNode(
            id="epi_000_000_000", layer=Layer.EPISODE,
            parent_id="trc_000_000_000", domain_id="dom_000",
            self_index=0,
            content="我们讨论了Q2 Sprint规划，决定将周期从2周改为3周",
            embedding=tr_emb_a,
            created_at=now, updated_at=now,
            weight=0.9,
        ))

        # Trace 0-0-1
        hmem_store.insert_node(HierarchyNode(
            id="trc_000_000_001", layer=Layer.TRACE,
            parent_id="cat_000_000", domain_id="dom_000",
            self_index=4, sub_indices=[0],
            title="Story Point估算",
            embedding=tr_emb_b,
            created_at=now, updated_at=now,
        ))
        hmem_store.insert_node(HierarchyNode(
            id="epi_000_000_001", layer=Layer.EPISODE,
            parent_id="trc_000_000_001", domain_id="dom_000",
            self_index=0,
            content="引入Story Point估算方法，代替时间估算",
            embedding=tr_emb_b,
            created_at=now, updated_at=now,
            weight=0.8,
        ))

        # Trace 0-1-0
        hmem_store.insert_node(HierarchyNode(
            id="trc_000_001_000", layer=Layer.TRACE,
            parent_id="cat_000_001", domain_id="dom_000",
            self_index=5, sub_indices=[0],
            title="风险评估流程",
            embedding=tr_emb_c,
            created_at=now, updated_at=now,
        ))
        hmem_store.insert_node(HierarchyNode(
            id="epi_000_001_000", layer=Layer.EPISODE,
            parent_id="trc_000_001_000", domain_id="dom_000",
            self_index=0,
            content="每周一次风险评估会议，识别新风险和缓解措施",
            embedding=tr_emb_c,
            created_at=now, updated_at=now,
            weight=0.7,
        ))

        return hmem_store

    def test_router_retrieval_basic(self, store_with_hierarchy):
        from cc_star.hmem.router import IndexRouter
        store = store_with_hierarchy
        router = IndexRouter(store)
        router.build_indexes()

        assert router.is_ready
        # Search with a query matching domain 0's content
        results = router.retrieve("项目管理", top_k=3)
        if results:
            for r in results:
                assert r.domain_id == "dom_000"

    def test_router_debug_output(self, store_with_hierarchy):
        from cc_star.hmem.router import IndexRouter
        store = store_with_hierarchy
        router = IndexRouter(store)
        router.build_indexes()

        debug = router.retrieve_debug("Sprint规划", top_k=2)
        assert "query" in debug
        assert "path" in debug
        assert len(debug["path"]) >= 2
        if debug["final"]:
            ep = debug["final"][0]
            assert "episode_id" in ep
            assert "effective_score" in ep


# ═══════════════════════════════════════════════════════════════════
# 5. Ebbinghaus Decay
# ═══════════════════════════════════════════════════════════════════

class TestEbbinghausDecay:
    def test_fresh_memory_high_weight(self):
        from cc_star.hmem.decay import EbbinghausDecay
        decay = EbbinghausDecay()
        # 0 days since access, accessed 1 time
        factor = decay.compute_decay_factor(0, 1, 0, 0)
        assert factor > 0.9  # Should be close to 1.0

    def test_old_memory_low_weight(self):
        from cc_star.hmem.decay import EbbinghausDecay
        decay = EbbinghausDecay()
        # 30 days since access, no access
        factor = decay.compute_decay_factor(30, 0, 0, 0)
        assert factor < 0.5  # Should be significantly decayed

    def test_approval_boosts_weight(self):
        from cc_star.hmem.decay import EbbinghausDecay
        decay = EbbinghausDecay()
        no_approval = decay.compute_decay_factor(7, 1, 0, 0)
        with_approval = decay.compute_decay_factor(7, 1, 1, 0)
        assert with_approval > no_approval

    def test_rebuttal_reduces_weight(self):
        from cc_star.hmem.decay import EbbinghausDecay
        decay = EbbinghausDecay()
        no_rebuttal = decay.compute_decay_factor(7, 1, 0, 0)
        with_rebuttal = decay.compute_decay_factor(7, 1, 0, 1)
        assert with_rebuttal < no_rebuttal

    def test_frequent_access_slows_decay(self):
        from cc_star.hmem.decay import EbbinghausDecay
        decay = EbbinghausDecay()
        # 7 days: Ebbinghaus base = exp(-7/1.84) ≈ 0.022
        low_access = decay.compute_decay_factor(7, 0, 0, 0)
        high_access = decay.compute_decay_factor(7, 10, 0, 0)
        assert high_access > low_access

    def test_weight_never_below_min(self):
        from cc_star.hmem.decay import EbbinghausDecay
        decay = EbbinghausDecay()
        factor = decay.compute_decay_factor(365 * 10, 0, 0, 0)
        assert factor >= decay.config.min_weight

    def test_weight_never_above_max(self):
        from cc_star.hmem.decay import EbbinghausDecay
        decay = EbbinghausDecay()
        factor = decay.compute_decay_factor(0, 1000, 100, 0)
        assert factor <= decay.config.max_weight

    def test_compute_on_node(self):
        from cc_star.hmem.decay import EbbinghausDecay
        decay = EbbinghausDecay()
        from datetime import datetime, timezone, timedelta
        old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        node = HierarchyNode(
            id="t1", layer=Layer.EPISODE,
            weight=1.0, access_count=3,
            last_accessed_at=old,
            approval_count=0, rebuttal_count=0,
        )
        new_weight = decay.compute(node)
        assert 0 < new_weight < 1.0  # Should have decayed but not to zero


# ═══════════════════════════════════════════════════════════════════
# 6. FeedbackProcessor
# ═══════════════════════════════════════════════════════════════════

class TestFeedbackProcessor:
    @pytest.fixture
    def store_with_node(self, hmem_store):
        node = HierarchyNode(
            id="epi_001", layer=Layer.EPISODE,
            weight=1.0, source_trace_id="tr_001",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        hmem_store.insert_node(node)
        return hmem_store

    def test_approval_detection_keyword(self, store_with_node):
        from cc_star.hmem.feedback import FeedbackProcessor
        fp = FeedbackProcessor(store_with_node)
        logs = fp.process("s1", "对，就是这样", ["epi_001"])
        assert len(logs) == 1
        assert logs[0].feedback_type == FeedbackType.APPROVAL
        assert logs[0].weight_after == pytest.approx(1.0 * 1.2)

    def test_rebuttal_detection_keyword(self, store_with_node):
        from cc_star.hmem.feedback import FeedbackProcessor
        fp = FeedbackProcessor(store_with_node)
        logs = fp.process("s1", "不对，你搞错了", ["epi_001"])
        assert len(logs) == 1
        assert logs[0].feedback_type == FeedbackType.REBUTTAL
        assert logs[0].weight_after == pytest.approx(1.0 * 0.5)

    def test_no_feedback_detection(self, store_with_node):
        from cc_star.hmem.feedback import FeedbackProcessor
        fp = FeedbackProcessor(store_with_node)
        logs = fp.process("s1", "好，那接下来我们讨论预算", ["epi_001"])
        assert len(logs) == 1
        assert logs[0].feedback_type == FeedbackType.NO_FEEDBACK
        # No immediate weight change
        assert logs[0].weight_after == pytest.approx(1.0)

    def test_empty_cited_nodes(self, store_with_node):
        from cc_star.hmem.feedback import FeedbackProcessor
        fp = FeedbackProcessor(store_with_node)
        logs = fp.process("s1", "对", [])
        assert logs == []

    def test_nonexistent_node(self, store_with_node):
        from cc_star.hmem.feedback import FeedbackProcessor
        fp = FeedbackProcessor(store_with_node)
        logs = fp.process("s1", "对", ["nonexistent"])
        assert logs == []

    def test_extract_from_assistant_text(self, store_with_node):
        from cc_star.hmem.feedback import FeedbackProcessor
        fp = FeedbackProcessor(store_with_node)
        assistant = "根据之前讨论 [hmem:epi_001]，Sprint周期改为3周"
        logs = fp.process_from_assistant_text("s1", "对，就是这样", assistant)
        assert len(logs) == 1
        assert logs[0].node_id == "epi_001"

    def test_positive_marker_llm_fallback(self, store_with_node):
        """Test smart heuristic when LLM is not available."""
        from cc_star.hmem.feedback import FeedbackProcessor
        fp = FeedbackProcessor(store_with_node)
        # Should detect approval via markers
        ftype = fp._classify_feedback("同意你的分析")
        assert ftype == FeedbackType.APPROVAL

    def test_japanese_no_match(self, store_with_node):
        """Non-Chinese approval should go to no_feedback."""
        from cc_star.hmem.feedback import FeedbackProcessor
        fp = FeedbackProcessor(store_with_node)
        ftype = fp._classify_feedback("That's correct")
        # The keyword patterns are Chinese regexes, so English goes to heuristic
        assert ftype in (FeedbackType.NO_FEEDBACK, FeedbackType.APPROVAL)


# ═══════════════════════════════════════════════════════════════════
# 7. DecayConfig
# ═══════════════════════════════════════════════════════════════════

class TestDecayConfig:
    def test_default_values(self):
        config = DecayConfig()
        assert config.ebbinghaus_k == 1.84
        assert config.feedback_approval_mult == 1.2
        assert config.feedback_rebuttal_mult == 0.5
        assert config.min_weight == 0.01
        assert config.max_weight == 5.0

    def test_custom_values(self):
        config = DecayConfig(ebbinghaus_k=2.0, min_weight=0.1)
        assert config.ebbinghaus_k == 2.0
        assert config.min_weight == 0.1


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

def _normalized_vec(seed: int) -> list[float]:
    """Create a normalized 384-dim vector from a seed."""
    rng = np.random.RandomState(seed)
    v = rng.randn(EMBED_DIM).astype(np.float32)
    v /= np.linalg.norm(v)
    return v.tolist()
