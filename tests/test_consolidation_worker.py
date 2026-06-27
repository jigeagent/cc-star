"""Tests for consolidation_worker.py — lock, state, task detection."""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

# Add worker dir to path
sys.path.insert(0, os.path.expanduser("~/.cc-star/worker"))
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    """Set up isolated environment for worker tests."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir()

    sessions_file = data_dir / "sessions.jsonl"
    promote_log = data_dir / "promote_log.jsonl"
    graph_db = data_dir / "graph.db"
    lock_file = worker_dir / "consolidation.lock"
    state_file = data_dir / "consolidation_state.json"

    # Patch paths
    monkeypatch.setattr("consolidation_worker.DATA_DIR", data_dir)
    monkeypatch.setattr("consolidation_worker.SESSIONS_FILE", sessions_file)
    monkeypatch.setattr("consolidation_worker.PROMOTE_LOG", promote_log)
    monkeypatch.setattr("consolidation_worker.GRAPH_DB", graph_db)
    monkeypatch.setattr("consolidation_worker.LOCK_FILE", lock_file)
    monkeypatch.setattr("consolidation_worker.STATE_FILE", state_file)

    yield {
        "data_dir": data_dir,
        "worker_dir": worker_dir,
        "sessions_file": sessions_file,
        "promote_log": promote_log,
        "graph_db": graph_db,
        "lock_file": lock_file,
        "state_file": state_file,
    }

    # Cleanup
    for f in [lock_file, state_file]:
        try:
            f.unlink(missing_ok=True)
        except OSError:
            pass


# ── Lock ──────────────────────────────────────────────────────

def test_acquire_lock(worker_env):
    import consolidation_worker as cw

    assert cw.acquire_lock()
    assert cw.LOCK_FILE.exists()

    # Second acquire should fail
    assert not cw.acquire_lock()

    cw.release_lock()
    assert not cw.LOCK_FILE.exists()


def test_stale_lock(worker_env):
    import consolidation_worker as cw

    # Create a stale lock (> 2 hours old)
    cw.LOCK_FILE.write_text("stale")
    stale_time = time.time() - 7201
    os.utime(str(cw.LOCK_FILE), (stale_time, stale_time))

    assert cw.acquire_lock()
    cw.release_lock()


# ── State ─────────────────────────────────────────────────────

def test_load_save_state(worker_env):
    import consolidation_worker as cw

    state = cw.load_state()
    assert state["last_run"] is None
    assert state["last_session_idx"] == 0

    state["last_run"] = "2026-06-27T00:00:00Z"
    state["last_session_idx"] = 5
    cw.save_state(state)

    loaded = cw.load_state()
    assert loaded["last_run"] == "2026-06-27T00:00:00Z"
    assert loaded["last_session_idx"] == 5


# ── Session reading ───────────────────────────────────────────

def test_read_sessions(worker_env):
    import consolidation_worker as cw

    sessions = [
        {"timestamp": "2026-06-27T10:00:00Z", "first_prompt": "task A"},
        {"timestamp": "2026-06-27T11:00:00Z", "first_prompt": "task B"},
        {"timestamp": "2026-06-26T09:00:00Z", "first_prompt": "yesterday"},
    ]
    cw.SESSIONS_FILE.write_text(
        "\n".join(json.dumps(s, ensure_ascii=False) for s in sessions),
        encoding="utf-8",
    )

    all_sessions = cw.read_sessions()
    assert len(all_sessions) == 3

    today = cw.read_sessions(date_filter="2026-06-27")
    assert len(today) == 2


# ── Task state detection ──────────────────────────────────────

def test_detect_task_states_done(worker_env):
    import consolidation_worker as cw

    sessions = [
        {"_idx": 0, "first_prompt": "做完了cc-star v0.7.0 Phase 4开发，搞定了consolidation_worker.py。"},
    ]
    count, lines = cw._detect_task_states(sessions)
    assert count >= 2
    assert any("task states detected" in l for l in lines)


def test_detect_task_states_blocked(worker_env):
    import consolidation_worker as cw

    sessions = [
        {"_idx": 0, "first_prompt": "Phase 5阻塞了，等待hooks.registry.json设计确认。"},
    ]
    count, lines = cw._detect_task_states(sessions)
    assert count >= 1


def test_detect_task_states_abandoned(worker_env):
    import consolidation_worker as cw

    sessions = [
        {"_idx": 0, "first_prompt": "放弃了Neo4j方案，改用SQLite。搁置了联邦图谱计划。"},
    ]
    count, lines = cw._detect_task_states(sessions)
    assert count >= 2


def test_detect_task_states_empty(worker_env):
    import consolidation_worker as cw

    sessions = [
        {"_idx": 0, "first_prompt": "今天天气不错。"},
    ]
    count, lines = cw._detect_task_states(sessions)
    assert count == 0


# ── Integration: consolidate ──────────────────────────────────

def test_consolidate_basic(worker_env):
    import consolidation_worker as cw

    sessions = [
        {"timestamp": "2026-06-27T10:00:00Z", "first_prompt": "虎哥审了SQLite方案，康少审了工程可行性。做完了Phase 1。"},
    ]
    cw.SESSIONS_FILE.write_text(
        "\n".join(json.dumps(s, ensure_ascii=False) for s in sessions),
        encoding="utf-8",
    )

    # Need graph.db schema
    from cc_star.cache.connection import CacheConnection
    from cc_star.graph.schema import ensure_graph_schema
    cache = CacheConnection(str(cw.GRAPH_DB))
    ensure_graph_schema(cache)
    cache.close()

    result = cw.consolidate(date_filter="2026-06-27")
    assert result["sessions_processed"] == 1
    assert result["entities_added"] >= 1
    assert result["task_relations_added"] >= 1


def test_consolidate_empty(worker_env):
    import consolidation_worker as cw

    result = cw.consolidate(date_filter="2026-06-27")
    assert result["sessions_processed"] == 0
