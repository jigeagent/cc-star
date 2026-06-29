"""Tests for graph_extract.py hook — entity extraction + graph.db integration.

Core extraction tests now validate against cc_star.graph.extractor
(the module that graph_extract.py imports from).
"""

import json
import os
import tempfile

import pytest

from cc_star.graph.extractor import (
    clean_text,
    extract_entities,
    extract_entities_regex,
    is_junk_entity,
    load_spacy,
)


# ── Text cleaning ────────────────────────────────────────────

def test_clean_text_strips_bridge_context():
    text = """<bridge_context>
{"chatId":"oc_301","chatType":"p2p","senderId":"ou_4c9","senderType":"user"}
</bridge_context>
<user_input>
{"text":"虎哥审了cc-star v0.7.0方案"}
</user_input>"""
    cleaned = clean_text(text)
    assert "虎哥" in cleaned
    assert "chatId" not in cleaned
    assert "bridge_context" not in cleaned
    assert "user_input" not in cleaned


def test_clean_text_strips_quoted_message():
    text = """<quoted_message id="om_xxx" sender_name="test">old message</quoted_message>
虎哥说用SQLite"""
    cleaned = clean_text(text)
    assert "虎哥" in cleaned
    assert "quoted_message" not in cleaned


def test_clean_text_strips_json_prefix():
    text = '{"chatId":"oc_301","chatType":"p2p"}实际消息内容'
    cleaned = clean_text(text)
    assert "实际消息内容" in cleaned
    assert "chatId" not in cleaned


# ── Junk entity filtering ────────────────────────────────────

@pytest.mark.parametrize("name", [
    "c41", "1234", "", "a", "{json", '"quoted',
    "ou_f3aa5aa1a6243f6a8b8917ba19505893",
    "oc_301563a7a7a4b1823e0c1f45c41c53cc",
    "om_x100b6c8aaa5a0cacc2adab7a65acc04",
    "a1b2c3d4e5f6a7b8",
    "c41c53cc",
])
def test_is_junk_entity_positive(name):
    assert is_junk_entity(name), f"should reject: {name}"


@pytest.mark.parametrize("name", [
    "虎哥", "康少", "SQLite", "cc-star", "Neo4j",
    "cc-star v0.7.0", "Agent Plan", "graph.db",
    "D:/WorkBuddy/workspace/plans/cc-star-v0.7.0-architecture-upgrade.md",
])
def test_is_junk_entity_negative(name):
    assert not is_junk_entity(name), f"should accept: {name}"


# ── Regex extraction ─────────────────────────────────────────

def test_extract_entities_regex_team():
    text = "虎哥和康少审了方案，好妹也看了。吉哥最终拍板。"
    entities = extract_entities_regex(text)
    names = {e["name"] for e in entities}
    assert "虎哥" in names
    assert "康少" in names
    assert "好妹" in names
    assert "吉哥" in names


def test_extract_entities_regex_technical():
    text = "cc-star v0.7.0 用 SQLite 做 graph.db，不用 Neo4j。用递归CTE查询。"
    entities = extract_entities_regex(text)
    names = {e["name"] for e in entities}
    assert "cc-star" in names
    assert "SQLite" in names
    assert "Neo4j" in names
    assert "graph.db" in names
    assert "递归CTE" in names


def test_extract_entities_regex_files():
    text = "方案在 D:/WorkBuddy/workspace/plans/cc-star-v0.7.0-architecture-upgrade.md"
    entities = extract_entities_regex(text)
    names = {e["name"] for e in entities}
    assert "D:/WorkBuddy/workspace/plans/cc-star-v0.7.0-architecture-upgrade.md" in names


def test_extract_entities_regex_decision():
    text = "决定：用SQLite不用Neo4j。判断：凌晨3点跑。裁决：采纳康少方案。"
    entities = extract_entities_regex(text)
    decisions = [e for e in entities if e["type"] == "decision"]
    # "SQLite" and "Neo4j" are also decision-type entities from regex rules
    assert len(decisions) == 5


# ── Mixed extraction (regex + spaCy) ─────────────────────────

def test_extract_entities_mixed():
    text = "虎哥在北京审查了cc-star v0.7.0方案，2025年6月决定用SQLite。"
    nlp = load_spacy()
    entities = extract_entities(text, nlp)
    names = {e["name"] for e in entities}
    # Regex hits
    assert "虎哥" in names
    assert "cc-star" in names
    assert "SQLite" in names
    # spaCy NER hits
    assert "北京" in names
    # Dedup: 2025年6月 should only appear once
    date_count = sum(1 for e in entities if e["name"] == "2025年6月")
    assert date_count <= 1


def test_extract_entities_empty_text():
    entities = extract_entities("", None)
    assert len(entities) == 0


def test_extract_entities_no_spacy():
    text = "虎哥审了SQLite方案"
    entities = extract_entities(text, None)
    names = {e["name"] for e in entities}
    # Regex-only should still work
    assert "虎哥" in names
    assert "SQLite" in names


# ── spaCy model ──────────────────────────────────────────────

def test_spacy_model_available():
    nlp = load_spacy()
    assert nlp is not None, "zh_core_web_sm should be installed"
