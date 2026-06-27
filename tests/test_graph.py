"""Tests for graph repository — CRUD + recursive CTE traversal."""

import os
import tempfile

import pytest

from cc_star.cache.connection import CacheConnection
from cc_star.graph.repository import GraphRepository
from cc_star.graph.schema import ensure_graph_schema, drop_graph_schema


@pytest.fixture
def repo():
    """Create a fresh GraphRepository, cleanup after test."""
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "test_graph.db")
    cache = CacheConnection(db)
    ensure_graph_schema(cache)
    yield GraphRepository(cache)
    cache.close()
    try:
        os.unlink(db)
        os.rmdir(tmp)
    except OSError:
        pass


def _seed_entities(repo: GraphRepository) -> dict:
    ids = {}
    ids["plan"] = repo.add_entity(
        "cc-star v0.7.0 plan", "project",
        description="Architecture upgrade plan",
    )
    ids["tiger"] = repo.add_entity("虎哥", "person", description="COO")
    ids["kangshao"] = repo.add_entity("康少", "person", description="Engineer")
    ids["plan_file"] = repo.add_entity(
        "plan.md", "file", description="Plan document",
    )
    ids["decision_sqlite"] = repo.add_entity(
        "SQLite over Neo4j", "decision", description="Use SQLite",
    )
    ids["baoge"] = repo.add_entity("豹哥", "person", description="Architect")
    return ids


def _seed_relations(repo: GraphRepository, ids: dict) -> None:
    repo.add_relation(ids["plan_file"], ids["plan"], "references")
    repo.add_relation(ids["tiger"], ids["decision_sqlite"], "decided_by")
    repo.add_relation(ids["kangshao"], ids["decision_sqlite"], "decided_by")
    repo.add_relation(ids["baoge"], ids["plan_file"], "produces")
    repo.add_relation(ids["plan"], ids["decision_sqlite"], "references")


# ── Entity CRUD ──────────────────────────────────────────────

def test_add_and_get_entity(repo):
    eid = repo.add_entity("test entity", "project", description="a test")
    assert eid > 0

    entity = repo.get_entity(eid)
    assert entity["name"] == "test entity"
    assert entity["type"] == "project"
    assert entity["description"] == "a test"
    assert repo.entity_count() == 1


def test_get_entity_by_name(repo):
    repo.add_entity("unique-name", "skill")
    found = repo.get_entity_by_name("unique-name")
    assert found is not None
    assert found["type"] == "skill"

    missing = repo.get_entity_by_name("nonexistent")
    assert missing is None


def test_find_or_create(repo):
    eid1 = repo.find_or_create_entity("foo", "other")
    eid2 = repo.find_or_create_entity("foo", "other")
    assert eid1 == eid2
    assert repo.entity_count() == 1


def test_invalid_entity_type_raises(repo):
    with pytest.raises(ValueError):
        repo.add_entity("bad", "invalid_type")


def test_list_entities(repo):
    _seed_entities(repo)
    all_entities = repo.list_entities()
    assert len(all_entities) >= 6

    persons = repo.list_entities(entity_type="person")
    assert len(persons) == 3
    assert all(e["type"] == "person" for e in persons)


def test_search_entities_fts(repo):
    _seed_entities(repo)
    results = repo.search_entities("SQLite")
    assert len(results) > 0
    assert any("SQLite" in r["name"] for r in results)


def test_update_entity(repo):
    eid = repo.add_entity("old name", "other")
    repo.update_entity(eid, name="new name", description="updated")
    entity = repo.get_entity(eid)
    assert entity["name"] == "new name"
    assert entity["description"] == "updated"


def test_entity_counts_by_type(repo):
    _seed_entities(repo)
    counts = repo.entity_counts_by_type()
    assert counts.get("person") == 3
    assert counts.get("project") == 1


# ── Relation CRUD ────────────────────────────────────────────

def test_add_and_get_relations(repo):
    ids = _seed_entities(repo)
    _seed_relations(repo, ids)

    assert repo.relation_count() == 5
    rels = repo.get_relations(entity_id=ids["plan_file"])
    assert len(rels) >= 1

    decided = repo.get_relations(rel_type="decided_by")
    assert len(decided) == 2


def test_add_relation_by_name(repo):
    repo.add_relation_by_name("A", "B", "references")
    assert repo.entity_count() == 2
    assert repo.relation_count() == 1

    a = repo.get_entity_by_name("A")
    b = repo.get_entity_by_name("B")
    assert a is not None
    assert b is not None


def test_invalid_relation_type_raises(repo):
    repo.add_entity("a", "other")
    repo.add_entity("b", "other")
    with pytest.raises(ValueError):
        repo.add_relation(1, 2, "bad_rel")


# ── Timeline CRUD ────────────────────────────────────────────

def test_add_and_get_events(repo):
    eid = repo.add_entity("session entity", "task")
    evt_id = repo.add_event(
        "graph_extract_ok", entity_id=eid,
        session_id="sesn_test123",
        payload={"entities_found": 5},
    )
    assert evt_id > 0

    events = repo.get_events(entity_id=eid)
    assert len(events) == 1
    assert events[0]["event_type"] == "graph_extract_ok"
    assert events[0]["payload"]["entities_found"] == 5


def test_get_failed_events(repo):
    repo.add_event("graph_extract_failed", session_id="s1",
                   payload={"error": "timeout"})
    repo.add_event("graph_extract_ok", session_id="s2")
    repo.add_event("graph_extract_failed", session_id="s3",
                   payload={"error": "spaCy load failed"})

    failed = repo.get_failed_events()
    assert len(failed) == 2
    assert all(e["event_type"] == "graph_extract_failed" for e in failed)


# ── Recursive CTE ────────────────────────────────────────────

def test_subgraph(repo):
    ids = _seed_entities(repo)
    _seed_relations(repo, ids)

    sg = repo.get_subgraph(ids["plan"])
    assert sg["entity"] is not None
    assert len(sg["neighbors"]) >= 2
    assert len(sg["relations"]) >= 2

    neighbor_ids = {n["id"] for n in sg["neighbors"]}
    assert ids["plan_file"] in neighbor_ids
    assert ids["decision_sqlite"] in neighbor_ids


def test_subgraph_max_depth(repo):
    ids = _seed_entities(repo)
    _seed_relations(repo, ids)

    sg = repo.get_subgraph(ids["plan"], max_depth=0)
    assert sg["entity"] is not None
    assert len(sg["neighbors"]) == 0

    sg = repo.get_subgraph(ids["plan"], max_depth=1)
    assert len(sg["neighbors"]) >= 2

    sg = repo.get_subgraph(ids["plan"], max_depth=3)
    all_ids = {n["id"] for n in sg["neighbors"]} | {sg["entity"]["id"]}
    assert ids["tiger"] in all_ids or ids["kangshao"] in all_ids


def test_trace_decision_chain(repo):
    ids = _seed_entities(repo)
    _seed_relations(repo, ids)

    chain = repo.trace_decision_chain(ids["decision_sqlite"])
    assert len(chain) >= 2

    names = {e["name"] for e in chain}
    assert "SQLite over Neo4j" in names
    assert "虎哥" in names or "康少" in names


def test_subgraph_direction_outgoing(repo):
    ids = _seed_entities(repo)
    _seed_relations(repo, ids)

    sg = repo.get_subgraph(ids["plan_file"], direction="outgoing")
    neighbor_ids = {n["id"] for n in sg["neighbors"]}
    assert ids["plan"] in neighbor_ids


def test_subgraph_direction_incoming(repo):
    ids = _seed_entities(repo)
    _seed_relations(repo, ids)

    sg = repo.get_subgraph(ids["decision_sqlite"], direction="incoming")
    neighbor_ids = {n["id"] for n in sg["neighbors"]}
    assert ids["tiger"] in neighbor_ids
    assert ids["kangshao"] in neighbor_ids


# ── Stats ────────────────────────────────────────────────────

def test_stats(repo):
    ids = _seed_entities(repo)
    _seed_relations(repo, ids)
    repo.add_event("graph_extract_ok", session_id="s1")
    repo.add_event("graph_extract_failed", session_id="s2")

    stats = repo.stats()
    assert stats["entities"] >= 6
    assert stats["relations"] >= 5
    assert stats["events"] == 2
    assert stats["failed_events"] == 1
    assert "person" in stats["entity_types"]


# ── Schema lifecycle ─────────────────────────────────────────

def test_drop_and_recreate():
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "test_graph.db")
    cache = CacheConnection(db)
    ensure_graph_schema(cache)
    repo = GraphRepository(cache)

    eid = repo.add_entity("test", "other")
    assert repo.entity_count() == 1

    drop_graph_schema(cache)
    ensure_graph_schema(cache)
    assert repo.entity_count() == 0

    cache.close()
    try:
        os.unlink(db)
        os.rmdir(tmp)
    except OSError:
        pass
