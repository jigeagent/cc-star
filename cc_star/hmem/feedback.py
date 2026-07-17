"""User feedback processing for H-MEM dynamic memory regulation.

Analyses user replies to detect approval/rebuttal toward cited memories,
then adjusts the memory weight accordingly.

Paper reference:
    Approval  → weight × 1.2
    No feedback → weight decays naturally (Ebbinghaus)
    Rebuttal  → weight × 0.5
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from cc_star.hmem.models import FeedbackType, FeedbackLog, DecayConfig
from cc_star.hmem.store import HierarchicalStore

logger = logging.getLogger(__name__)

# Fast-path keywords for approval detection
APPROVAL_PATTERNS: list[re.Pattern] = [
    re.compile(p) for p in [
        r"^对[的，,.]",
        r"^是[的，,.]",
        r"^没错",
        r"^就是这样",
        r"^是的",
        r"^对呀",
        r"^好的",
        r"^同意",
        r"^正确",
        r"^就是这个",
        r"^说得对",
        r"^是的，没错",
        r"^可以，就这样",
    ]
]

# Fast-path keywords for rebuttal detection
REBUTTAL_PATTERNS: list[re.Pattern] = [
    re.compile(p) for p in [
        r"^不对",
        r"^不是",
        r"^错了",
        r"^不对吧",
        r"^不是这样",
        r"^你搞错了",
        r"^更正",
        r"^不是那个意思",
        r"^我不是这个意思",
        r"^你说错了",
        r"^不对，",
        r"^不是，",
    ]
]

# Weight multipliers
APPROVAL_MULTIPLIER = 1.2
REBUTTAL_MULTIPLIER = 0.5
NO_FEEDBACK_MULTIPLIER = 1.0


class FeedbackProcessor:
    """Process user feedback to adjust H-MEM weights.

    Two-tier detection:
        Tier 1 (fast path): keyword/regex matching — returns in μs
        Tier 2 (LLM path):  LLM-based analysis — for ambiguous/compound replies
    """

    def __init__(
        self,
        store: HierarchicalStore,
        config: Optional[DecayConfig] = None,
        llm_available: bool = False,
    ):
        self._store = store
        self.config = config or DecayConfig()
        self._llm_available = llm_available

    def process(
        self,
        session_id: str,
        user_message: str,
        cited_node_ids: list[str],
        assistant_message: str = "",
    ) -> list[FeedbackLog]:
        """Process a user reply for feedback on cited memory nodes.

        Args:
            session_id: The current session ID.
            user_message: The user's reply text.
            cited_node_ids: IDs of hierarchy nodes that were cited in the AI's
                            previous message (set by the caller — the AI knows
                            which nodes it retrieved).
            assistant_message: The AI's previous message (for context).

        Returns:
            List of FeedbackLog entries created.
        """
        if not cited_node_ids:
            return []

        # Determine overall feedback type from user message
        ftype = self._classify_feedback(user_message)

        logs: list[FeedbackLog] = []
        for node_id in cited_node_ids:
            node = self._store.get_node(node_id)
            if not node:
                continue

            old_weight = node.weight

            if ftype == FeedbackType.APPROVAL:
                new_weight = old_weight * self.config.feedback_approval_mult
                node.approval_count += 1
            elif ftype == FeedbackType.REBUTTAL:
                new_weight = old_weight * self.config.feedback_rebuttal_mult
                node.rebuttal_count += 1
            else:
                new_weight = old_weight  # no immediate change; natural decay handles it

            new_weight = max(self.config.min_weight, min(self.config.max_weight, new_weight))
            node.weight = new_weight
            self._store.update_node(node)

            log_entry = FeedbackLog(
                id=str(uuid4()),
                node_id=node_id,
                feedback_type=ftype,
                session_id=session_id,
                user_message=user_message[:500],
                llm_analysis="",
                weight_before=old_weight,
                weight_after=new_weight,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            self._store.insert_feedback(log_entry)
            logs.append(log_entry)

            logger.info(
                "Feedback[%s] node=%s type=%s weight=%.2f→%.2f",
                session_id[:8], node_id[:12], ftype, old_weight, new_weight,
            )

        return logs

    def process_from_assistant_text(
        self,
        session_id: str,
        user_message: str,
        assistant_message: str,
    ) -> list[FeedbackLog]:
        """Convenience: extract cited node IDs from assistant message,
        then process feedback.

        Looks for patterns like [hmem:node_id] in the assistant message,
        which the AI would insert when citing hierarchy nodes.
        """
        # Extract referenced node IDs from the assistant message
        cited_ids = re.findall(r'\[hmem:([a-z0-9_]+)\]', assistant_message)
        if not cited_ids:
            return []

        return self.process(session_id, user_message, cited_ids, assistant_message)

    def _classify_feedback(self, user_message: str) -> FeedbackType:
        """Two-tier feedback classification.

        Tier 1: Regex fast path (μs)
        Tier 2: LLM (if available, for ambiguous cases)
        """
        msg = user_message.strip()

        # Tier 1: Fast path via keyword patterns
        for pattern in APPROVAL_PATTERNS:
            if pattern.search(msg):
                return FeedbackType.APPROVAL

        for pattern in REBUTTAL_PATTERNS:
            if pattern.search(msg):
                return FeedbackType.REBUTTAL

        # Tier 2: LLM analysis (if available)
        if self._llm_available:
            return self._llm_classify(user_message)

        # Default: no feedback (let natural decay handle it)
        return FeedbackType.NO_FEEDBACK

    def _llm_classify(self, user_message: str) -> FeedbackType:
        """LLM-based feedback classification.

        Placeholder — actual LLM call to be integrated later.
        """
        # TODO: Implement LLM-based classification
        # For now, smart heuristic: check for explicit positive/negative markers
        positive_markers = ["对", "是", "好", "同意", "可以", "没错", "正确"]
        negative_markers = ["不", "错", "误", "反", "改", "更正"]

        msg = user_message.strip()
        has_positive = any(m in msg for m in positive_markers)
        has_negative = any(m in msg for m in negative_markers)

        if has_positive and not has_negative:
            return FeedbackType.APPROVAL
        if has_negative and not has_positive:
            return FeedbackType.REBUTTAL

        return FeedbackType.NO_FEEDBACK
