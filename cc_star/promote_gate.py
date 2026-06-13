"""Validation gate for cc-star memory promotion.

移植自 SkillOpt evaluate_gate() 纯函数逻辑。
在 cache.db → native memory 晋升时加上验证门控：
- candidate 必须优于 current 才接受
- 追踪 current_score + best_score 双线
- reject 不丢信息，记入拒绝缓冲

核心差异：
  SkillOpt 的 gate 基于任务执行结果（hard/soft score）
  cc-star gate 基于 _score_trace() 打分 + FTS5 相似度
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal


GateAction = Literal["accept_new_best", "accept", "reject"]
GateMetric = Literal["hard", "soft", "mixed"]

_GATE_STATE_FILE = "gate_state.json"


@dataclass
class GateResult:
    """Immutable outcome of the promotion gate."""

    action: GateAction
    candidate_id: str
    candidate_score: float
    current_score: float
    best_score: float
    best_id: str
    best_step: int


@dataclass
class GateState:
    """Persistent gate state — tracks current/best across promote runs."""

    current_score: float = 0.0
    current_id: str = ""
    best_score: float = 0.0
    best_id: str = ""
    best_step: int = 0
    promote_count: int = 0
    reject_count: int = 0

    def to_dict(self) -> dict:
        return {
            "current_score": self.current_score,
            "current_id": self.current_id,
            "best_score": self.best_score,
            "best_id": self.best_id,
            "best_step": self.best_step,
            "promote_count": self.promote_count,
            "reject_count": self.reject_count,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GateState":
        return cls(
            current_score=float(d.get("current_score", 0.0)),
            current_id=str(d.get("current_id", "")),
            best_score=float(d.get("best_score", 0.0)),
            best_id=str(d.get("best_id", "")),
            best_step=int(d.get("best_step", 0)),
            promote_count=int(d.get("promote_count", 0)),
            reject_count=int(d.get("reject_count", 0)),
        )


# ── Pure gate function (from SkillOpt evaluate_gate) ──


def select_gate_score(
    hard: float,
    soft: float,
    metric: GateMetric = "mixed",
    mixed_weight: float = 0.3,
) -> float:
    """Project (hard, soft) onto a single comparison score.

    - hard: 基于 _score_trace() 的刚性分数（0-10）
    - soft: 基于 FTS5 相似度或引用次数的柔性分数（0-10）
    - mixed: 加权融合
    """
    if metric == "hard":
        return float(hard)
    if metric == "soft":
        return float(soft)
    if metric == "mixed":
        w = max(0.0, min(1.0, float(mixed_weight)))
        return (1.0 - w) * float(hard) + w * float(soft)
    raise ValueError(f"unknown gate metric {metric!r}")


def evaluate_gate(
    candidate_id: str,
    candidate_hard: float,
    current_state: GateState,
    global_step: int,
    *,
    candidate_soft: float = 0.0,
    metric: GateMetric = "mixed",
    mixed_weight: float = 0.3,
) -> GateResult:
    """Gate decision: compare candidate score to current/best.

    Args:
        candidate_id: Trace ID being evaluated.
        candidate_hard: _score_trace() result for the candidate.
        current_state: Current gate state (current/best scores).
        global_step: Promote run counter (for best_step tracking).
        candidate_soft: Softer metric (FTS5 similarity, optional).
        metric: Comparison metric. Default mixed.
        mixed_weight: Weight on soft when metric=mixed.

    Returns:
        GateResult with action and updated state.
    """
    cand_score = select_gate_score(
        candidate_hard, candidate_soft, metric, mixed_weight,
    )

    if cand_score > current_state.current_score:
        if cand_score > current_state.best_score:
            return GateResult(
                action="accept_new_best",
                candidate_id=candidate_id,
                candidate_score=cand_score,
                current_score=cand_score,
                best_score=cand_score,
                best_id=candidate_id,
                best_step=global_step,
            )
        return GateResult(
            action="accept",
            candidate_id=candidate_id,
            candidate_score=cand_score,
            current_score=cand_score,
            best_score=current_state.best_score,
            best_id=current_state.best_id,
            best_step=current_state.best_step,
        )
    return GateResult(
        action="reject",
        candidate_id=candidate_id,
        candidate_score=cand_score,
        current_score=current_state.current_score,
        best_score=current_state.best_score,
        best_id=current_state.best_id,
        best_step=current_state.best_step,
    )


# ── State persistence ──


def gate_state_path() -> Path:
    """Path to gate state JSON file."""
    # Same directory as promote_log.jsonl
    raw = os.environ.get("CC_STAR_CACHE_PATH", "")
    if raw:
        base = Path(raw).parent
    else:
        base = Path.home() / ".cc-star" / "data"
    return base / _GATE_STATE_FILE


def load_gate_state() -> GateState:
    """Load persistent gate state."""
    path = gate_state_path()
    if path.is_file():
        try:
            return GateState.from_dict(json.loads(path.read_text("utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return GateState()


def save_gate_state(state: GateState) -> None:
    """Save gate state to disk."""
    path = gate_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_dict(), indent=2, ensure_ascii=False), "utf-8")


def update_gate_state(state: GateState, result: GateResult) -> GateState:
    """Update gate state from a gate result."""
    state.current_score = result.current_score
    state.current_id = result.candidate_id if result.action != "reject" else state.current_id
    if result.action == "accept_new_best":
        state.best_score = result.best_score
        state.best_id = result.best_id
        state.best_step = result.best_step
    if result.action in ("accept", "accept_new_best"):
        state.promote_count += 1
    else:
        state.reject_count += 1
    return state


# ── Soft metric helpers ──


def _cjk_bigrams(text: str) -> set[str]:
    """Extract CJK character bigrams from text for fuzzy matching."""
    import re
    cjk_chars = re.findall(r'[一-鿿㐀-䶿]', text)
    return {cjk_chars[i] + cjk_chars[i + 1] for i in range(len(cjk_chars) - 1)}


def compute_soft_score(
    candidate_text: str,
    existing_contents: list[str],
) -> float:
    """Compute a soft score (0-10) based on similarity to existing memories.

    Uses CJK bigrams + English keyword overlap — avoids API calls.
    Used as 'soft' metric when metric='mixed'.
    """
    if not candidate_text or not existing_contents:
        return 0.0

    import re
    # CJK bigrams
    c_bigrams = _cjk_bigrams(candidate_text)
    # English keywords (3+ chars)
    c_en = set(re.findall(r'[a-z0-9_\-]{3,}', candidate_text.lower()))
    c_tokens = c_bigrams | c_en

    if not c_tokens:
        return 0.0

    max_overlap = 0.0
    for existing in existing_contents:
        e_bigrams = _cjk_bigrams(existing)
        e_en = set(re.findall(r'[a-z0-9_\-]{3,}', existing.lower()))
        e_tokens = e_bigrams | e_en
        if not e_tokens:
            continue
        overlap = len(c_tokens & e_tokens) / max(len(c_tokens | e_tokens), 1)
        max_overlap = max(max_overlap, overlap)

    # Map Jaccard similarity [0,1] to score [0,10]
    return round(max_overlap * 10.0, 2)


# ── Reject buffer ──


_REJECT_LOG = "reject_log.jsonl"


def reject_log_path() -> Path:
    raw = os.environ.get("CC_STAR_CACHE_PATH", "")
    if raw:
        base = Path(raw).parent
    else:
        base = Path.home() / ".cc-star" / "data"
    return base / _REJECT_LOG


def log_rejection(result: GateResult, reason: str = "") -> None:
    """Log a rejected candidate for later analysis."""
    path = reject_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "candidate_id": result.candidate_id,
        "candidate_score": result.candidate_score,
        "current_score": result.current_score,
        "best_score": result.best_score,
        "reason": reason,
        "rejected_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with open(str(path), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def load_recent_rejections(n: int = 5) -> list[dict]:
    """Load most recent rejection records."""
    path = reject_log_path()
    if not path.is_file():
        return []
    try:
        lines = path.read_text("utf-8").strip().split("\n")
        records = []
        for line in lines:
            if line.strip():
                records.append(json.loads(line))
        return records[-n:]
    except (OSError, json.JSONDecodeError):
        return []
