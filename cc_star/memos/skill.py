"""Skill crystallization pipeline — converts validated policies into
reusable, versioned skills with human-readable usage guides.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from cc_star.memos.types import PolicyRow, SkillRow

logger = logging.getLogger(__name__)


@dataclass
class SkillCrystallizerConfig:
    """Configuration for skill crystallization."""

    min_policy_confidence: float = 0.5
    """Minimum confidence for a policy to be crystallized into a skill."""

    min_activation_count: int = 2
    """Minimum times a policy must be activated before crystallization."""

    max_skills: int = 50
    """Maximum number of crystallized skills."""

    version_increment_on_update: bool = True
    """Auto-increment version when a skill is updated."""


class SkillCrystallizer:
    """Crystallizes validated policies into reusable skills."""

    def __init__(self, config: Optional[SkillCrystallizerConfig] = None):
        self._config = config or SkillCrystallizerConfig()

    # ── Crystallization ───────────────────────────────────

    def crystallize(
        self,
        policy: PolicyRow,
        existing_skills: Optional[list[SkillRow]] = None,
    ) -> Optional[SkillRow]:
        """Crystallize a single policy into a skill.

        Args:
            policy: The policy to crystallize.
            existing_skills: Current skills for deduplication.

        Returns:
            A SkillRow if the policy meets criteria, None otherwise.
        """
        if policy.confidence < self._config.min_policy_confidence:
            logger.debug(
                "Policy %s confidence %.2f below threshold %.2f",
                policy.name, policy.confidence, self._config.min_policy_confidence,
            )
            return None

        if policy.activation_count < self._config.min_activation_count:
            logger.debug(
                "Policy %s activation count %d below threshold %d",
                policy.name, policy.activation_count, self._config.min_activation_count,
            )
            return None

        # Check if skill already exists with this name
        if existing_skills:
            existing = next(
                (s for s in existing_skills if s.name == policy.name),
                None,
            )
            if existing:
                return self._update_from_policy(existing, policy)

        # Generate usage guide from policy
        usage_guide = self._generate_usage_guide(policy)

        skill = SkillRow(
            name=policy.name,
            description=policy.description,
            usage_guide=usage_guide,
            source_policy_ids=[policy.id],
            version=1,
            metadata={
                "crystallized_at": datetime.now(timezone.utc).isoformat(),
                "source_confidence": policy.confidence,
            },
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        logger.info("Crystallized skill: %s (v1)", skill.name)
        return skill

    def batch_crystallize(
        self,
        policies: list[PolicyRow],
        existing_skills: Optional[list[SkillRow]] = None,
    ) -> list[SkillRow]:
        """Crystallize multiple policies into skills."""
        existing = existing_skills or []
        new_skills: list[SkillRow] = []

        for policy in policies:
            skill = self.crystallize(policy, existing_skills=existing)
            if skill:
                # Deduplicate against new skills too
                if not any(s.name == skill.name for s in new_skills):
                    new_skills.append(skill)
                    existing.append(skill)

            if len(new_skills) + len(existing) > self._config.max_skills:
                logger.warning("Max skills (%d) reached, stopping crystallization", self._config.max_skills)
                break

        return new_skills

    # ── Skill Update ──────────────────────────────────────

    def update_skill(
        self,
        skill: SkillRow,
        new_policies: list[PolicyRow],
    ) -> SkillRow:
        """Update an existing skill with new policies, merging insights."""
        merged_policy_ids = list(set(
            skill.source_policy_ids + [p.id for p in new_policies]
        ))

        # Combine descriptions
        descriptions = [skill.description]
        for p in new_policies:
            if p.description and p.description not in descriptions:
                descriptions.append(p.description)

        merged_description = " | ".join(descriptions)

        # Extend usage guide
        new_guides = []
        for p in new_policies:
            if p.action_template:
                new_guides.append(self._generate_usage_guide(p))
        extended_guide = skill.usage_guide
        if new_guides:
            extended_guide = skill.usage_guide + "\n\n---\n\n" + "\n\n".join(new_guides)

        new_version = skill.version + 1 if self._config.version_increment_on_update else skill.version

        return SkillRow(
            name=skill.name,
            description=merged_description[:500],
            usage_guide=extended_guide,
            source_policy_ids=merged_policy_ids,
            version=new_version,
            metadata={
                **skill.metadata,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "updated_policies": [p.id for p in new_policies],
            },
            created_at=skill.created_at,
        )

    # ── Usage Guide Generation ────────────────────────────

    @staticmethod
    def _generate_usage_guide(policy: PolicyRow) -> str:
        """Generate a human-readable usage guide from a policy."""
        lines = [
            f"### {policy.name}",
            "",
            policy.description,
            "",
        ]

        if policy.trigger_pattern:
            lines.append("**When to use:**")
            lines.append("")
            # Remove bold markers for readability
            trigger = policy.trigger_pattern.replace("**", "")
            lines.append(f"- User input contains: \"{trigger[:120]}\"")
            lines.append("")

        if policy.action_template:
            lines.append("**Suggested approach:**")
            lines.append("")
            action = policy.action_template.replace("**", "")
            lines.append(f"- {action[:200]}")
            lines.append("")

        lines.append(f"*Confidence: {policy.confidence:.2f}*")
        lines.append(f"*Activations: {policy.activation_count}*")

        return "\n".join(lines)

    # ── Helpers ───────────────────────────────────────────

    @staticmethod
    def _update_from_policy(
        existing: SkillRow,
        policy: PolicyRow,
    ) -> SkillRow:
        """Update existing skill with new policy data."""
        merged_ids = list(set(existing.source_policy_ids + [policy.id]))
        return SkillRow(
            name=existing.name,
            description=existing.description or policy.description,
            usage_guide=existing.usage_guide,
            source_policy_ids=merged_ids,
            version=existing.version + 1,
            metadata={
                **existing.metadata,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "added_policy": policy.id,
            },
            created_at=existing.created_at,
        )
