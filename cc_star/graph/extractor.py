"""NER extractor — spaCy + regex entity extraction for cc-star context graph.

Consolidates the entity extraction pipeline previously split across
graph_extract.py (hook) and consolidation_worker.py into one importable module.
"""

from __future__ import annotations

import re


# Regex patterns for domain entities spaCy misses
REGEX_PATTERNS: list[tuple[str, str, str]] = [
    # (regex, entity_type, description)
    # Team members (nicknames) — no \b, Chinese chars break word boundary
    (r"(?<![a-zA-Z0-9])虎哥(?![a-zA-Z0-9])", "person", "COO, direction judge"),
    (r"(?<![a-zA-Z0-9])吉哥(?![a-zA-Z0-9])", "person", "final decision maker"),
    (r"(?<![a-zA-Z0-9])康少(?![a-zA-Z0-9])", "person", "engineering reviewer"),
    (r"(?<![a-zA-Z0-9])好妹(?![a-zA-Z0-9])", "person", "content operations"),
    (r"(?<![a-zA-Z0-9])好二妹(?![a-zA-Z0-9])", "person", "theory + strategy"),
    (r"(?<![a-zA-Z0-9])好灵儿(?![a-zA-Z0-9])", "person", "theory + strategy (alias)"),
    (r"(?<![a-zA-Z0-9])灵儿(?![a-zA-Z0-9])", "person", "visual agent"),
    (r"(?<![a-zA-Z0-9])豹哥(?![a-zA-Z0-9])", "person", "architect + executor"),
    # Technical entities — use lookaround instead of \b (Chinese chars are \w)
    (r"(?<![a-zA-Z0-9])cc-star(?![a-zA-Z0-9])", "project", "Claude Code memory upgrade kit"),
    (r"(?<![a-zA-Z0-9])cdx-brain(?![a-zA-Z0-9])", "project", "Codex platform knowledge management"),
    (r"(?<![a-zA-Z0-9])OpenViking(?![a-zA-Z0-9])", "project", "shared team memory"),
    (r"(?<![a-zA-Z0-9])SQLite(?![a-zA-Z0-9])", "decision", "database engine choice"),
    (r"(?<![a-zA-Z0-9])Neo4j(?![a-zA-Z0-9])", "decision", "graph database (rejected)"),
    (r"(?<![a-zA-Z0-9])FTS5(?![a-zA-Z0-9])", "skill", "full-text search engine"),
    (r"(?<![a-zA-Z0-9])fastembed(?![a-zA-Z0-9])", "skill", "embedding engine"),
    (r"(?<![a-zA-Z0-9])spaCy(?![a-zA-Z0-9])", "skill", "NER pipeline"),
    (r"(?<![a-zA-Z0-9])zh_core_web_sm(?![a-zA-Z0-9])", "skill", "Chinese spaCy model"),
    # Common project/file patterns
    (r"(?<![a-zA-Z0-9])cc-star-v?\d+\.\d+\.\d+(?![a-zA-Z0-9])", "project", "cc-star version"),
    (r"(?<![a-zA-Z0-9])graph\.db(?![a-zA-Z0-9])", "file", "context graph database"),
    (r"(?<![a-zA-Z0-9])cache\.db(?![a-zA-Z0-9])", "file", "FTS5 + vector cache"),
    (r"(?<![a-zA-Z0-9])sessions\.jsonl(?![a-zA-Z0-9])", "file", "session summary log"),
    (r"(?<![a-zA-Z0-9])promote_log\.jsonl(?![a-zA-Z0-9])", "file", "promotion log"),
    (r"(?<![a-zA-Z0-9])MEMORY\.md(?![a-zA-Z0-9])", "file", "memory index"),
    (r"(?<![a-zA-Z0-9])settings\.json(?![a-zA-Z0-9])", "file", "Claude Code configuration"),
    # File paths
    (r"[A-Z]:/[^\s,，。；;]{5,80}\.(?:md|py|json|yaml|db|txt)", "file", "local file"),
    # Decision markers
    (r"(?:决定|决策|判断|裁决)[：:]\s*(.+?)(?:[。，,;]|$)", "decision", "recorded decision"),
    # Project/version references
    (r"v\d+\.\d+\.\d+", "project", "version reference"),
    # Model references
    (r"(?<![a-zA-Z0-9])deepseek-v\d[^\s,，。；;]{0,20}", "skill", "AI model"),
    (r"(?<![a-zA-Z0-9])glm-\d+[^\s,，。；;]{0,20}", "skill", "AI model"),
    (r"(?<![a-zA-Z0-9])claude[^\s,，。；;]{0,30}", "skill", "AI model"),
    (r"(?<![a-zA-Z0-9])doubao[^\s,，。；;]{0,30}", "skill", "AI model"),
    # Plan / Agent Plan references
    (r"(?<![a-zA-Z0-9])Agent Plan(?![a-zA-Z0-9])", "project", "火山方舟 Agent Plan"),
    (r"(?<![a-zA-Z0-9])(?:Phase|阶段)\s*[1-7](?![a-zA-Z0-9])", "task", "project phase"),
    # Architecture concepts
    (r"(?<![a-zA-Z0-9])Context Graph(?![a-zA-Z0-9])", "skill", "context graph architecture"),
    (r"(?<![a-zA-Z0-9])Consolidation Worker(?![a-zA-Z0-9])", "skill", "nightly consolidation"),
    (r"(?:递归\s*CTE|Recursive CTE)", "skill", "graph traversal technique"),
]


def load_spacy():
    """Lazy-load spaCy model (loaded once per invocation, then cached by Python)."""
    try:
        import spacy
        return spacy.load("zh_core_web_sm")
    except Exception:
        return None


def clean_text(text: str) -> str:
    """Strip bridge metadata JSON and noise from session text.

    sessions.jsonl first_prompt can contain raw bridge_context JSON
    followed by the actual user message. We extract only the user part.
    """
    if not text:
        return ""

    # Strip bridge_context JSON block
    text = re.sub(r"<bridge_context>\s*\{[^}]*\}\s*</bridge_context>", "", text, flags=re.DOTALL)
    # Strip quoted_message blocks
    text = re.sub(r"<quoted_message[^>]*>.*?</quoted_message>", "", text, flags=re.DOTALL)
    # Strip interactive_card blocks
    text = re.sub(r"<interactive_card>.*?</interactive_card>", "", text, flags=re.DOTALL)
    # Strip bridge_instructions blocks
    text = re.sub(r"<bridge_instructions>.*?</bridge_instructions>", "", text, flags=re.DOTALL)
    # Strip <user_input> wrappers
    text = re.sub(r"<user_input>|</user_input>", "", text)
    # Strip leading JSON objects (bridge metadata)
    text = re.sub(r'^\s*\{[^{}]*"chatId"[^{}]*\}\s*', "", text)
    # Strip remaining XML tags
    text = re.sub(r"<[^>]+>", "", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()

    return text


def is_junk_entity(name: str) -> bool:
    """Filter out entities that are clearly JSON fragments, IDs, or noise."""
    if len(name) < 2:
        return True
    if len(name) > 80:
        return True
    # Pure digits
    if re.match(r"^\d+$", name):
        return True
    # Looks like a JSON fragment
    if name.startswith("{") or name.startswith('"') or name.startswith("}"):
        return True
    # UUID / hex ID fragments
    if re.match(r"^[a-f0-9]{8,}$", name, re.IGNORECASE):
        return True
    # OpenID / chat ID fragments (partial or full)
    if re.match(r"^[a-f0-9]+$", name) and len(name) <= 30:
        return True
    if re.match(r"^ou_[a-f0-9]+$", name):
        return True
    if re.match(r"^oc_[a-f0-9]+$", name):
        return True
    if re.match(r"^om_[a-z0-9]+$", name):
        return True
    # Single alphanumeric fragments (chatId remnants)
    if re.match(r"^[a-z]\d+$", name, re.IGNORECASE) and len(name) <= 20:
        return True
    return False


def extract_entities_regex(text: str) -> list[dict]:
    """Regex-based entity extraction for domain terms spaCy misses."""
    entities = []
    seen = set()

    for pattern, entity_type, description in REGEX_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            name = match.group(1) if match.lastindex else match.group(0)
            name = name.strip()
            if not name or name in seen:
                continue
            seen.add(name)
            entities.append({
                "name": name,
                "type": entity_type,
                "description": description,
            })

    return entities


def extract_entities(text: str, nlp) -> list[dict]:
    """Run spaCy NER + regex rules on text. Returns deduplicated entity list."""
    if not text.strip():
        return []

    entities = []
    seen = set()

    # 1. Regex rules first (higher priority for domain terms)
    for ent in extract_entities_regex(text):
        if is_junk_entity(ent["name"]):
            continue
        seen.add(ent["name"])
        entities.append(ent)

    # 2. spaCy NER for standard named entities
    if nlp is not None:
        doc = nlp(text[:100000])
        type_map = {
            "PERSON": "person",
            "ORG": "person",
            "GPE": "other",
            "LOC": "other",
            "DATE": "other",
            "TIME": "other",
            "MONEY": "other",
            "PERCENT": "other",
            "PRODUCT": "project",
            "WORK_OF_ART": "file",
            "EVENT": "task",
            "FAC": "other",
        }
        for ent in doc.ents:
            name = ent.text.strip()
            if not name or name in seen or is_junk_entity(name):
                continue
            seen.add(name)
            entities.append({
                "name": name,
                "type": type_map.get(ent.label_, "other"),
                "description": f"NER: {ent.label_}",
            })

    return entities
