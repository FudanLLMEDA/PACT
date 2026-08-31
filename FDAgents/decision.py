"""
decision.py — rule-based fast filter and deep-analysis trigger for FDAgents.

The rule layer runs without any LLM call. Only when it returns None does the
main loop ask the LLM for a decision.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .skills import DEEP_ONLY_SKILLS

logger = logging.getLogger(__name__)

# Safety margin: stop optimization if less than this many seconds remain
TIME_SAFETY_S = 300.0

# Deep-mode thresholds
DEEP_TOKEN_THRESHOLD_USD = 0.50
DEEP_TIME_THRESHOLD_S = 1200.0
DEEP_MAX_CALLS = 3

# Force-skip threshold: if the last N history entries are all the same
# non-custom skill AND none of them improved WNS enough to be accepted,
# the rule layer blacklists every remaining candidate of that skill so
# the LLM stops grinding on it. Observed on route-heavy profiles where
# consecutive fanout attempts against different nets all had near-zero delta
# while other skills were improving elsewhere.
SAME_SKILL_SPAM_N = 3
SAME_SKILL_SPAM_DELTA = 0.005


def _has_active_skill_cooldown(memory, skill: str) -> bool:
    current_best_round = memory.current_best.get("round")
    return any(
        entry.get("skill") == skill
        and entry.get("target") == "*"
        and entry.get("severity") == "cooldown"
        and entry.get("best_round") == current_best_round
        for entry in memory.blacklist
    )


@dataclass
class Action:
    kind: str           # "save_and_exit" | "rollback" | "skip_skill" | None (→ ask LLM)
    skill: Optional[str] = None
    target: Optional[str] = None
    params: Optional[dict] = None
    reason: str = ""


def quick_filter(memory) -> Optional[Action]:
    """
    Return a deterministic Action when the rule layer can decide, else None.

    Saves one LLM call when the answer is obvious.
    """
    # Budget exhausted → exit
    if not memory.budget_ok():
        logger.info("[decision] budget exhausted → save_and_exit")
        return Action("save_and_exit", reason="budget exhausted")

    recipe_id = str(memory.baseline.get("recipe_id") or "")
    clock_tighten_is_terminal = (
        recipe_id == "CLOCK_TIGHTEN_SWEEP"
        or recipe_id == "UNDEREXPLORED_CLOCK_REPLAY"
        or recipe_id == "DATUNER_DIRECTIVE_CLOCK_SWEEP"
    )
    if clock_tighten_is_terminal and any(
        entry.get("skill") == "clock_tighten" and entry.get("accepted")
        for entry in memory.history
    ):
        logger.info("[decision] accepted clock_tighten recipe → save_and_exit")
        return Action("save_and_exit", reason="clock_tighten accepted")

    # Two consecutive failures on the same (skill, target) → rollback.
    #
    # Gating:
    #   1. Skip deep-only skills (e.g. 'custom'). Custom probes are
    #      exploratory — they always record accepted=False by design, so
    #      this rule would fire on every repeated probe and loop forever.
    #   2. Skip if the pair is already blacklisted. The rollback handler in
    #      agent.py blacklists the pair; this guard prevents the check
    #      from firing again on the same history pattern (infinite loop).
    history = memory.history
    if len(history) >= 2:
        last = history[-1]
        prev = history[-2]
        already_blacklisted = any(
            b["skill"] == last["skill"] and b["target"] == last["target"]
            for b in memory.blacklist
        )
        if (
            last["skill"] not in DEEP_ONLY_SKILLS
            and not last["accepted"]
            and not prev["accepted"]
            and last["skill"] == prev["skill"]
            and last["target"] == prev["target"]
            and not already_blacklisted
        ):
            logger.info(f"[decision] two consecutive failures on {last['skill']}/{last['target']} → rollback")
            return Action(
                "rollback",
                skill=last["skill"],
                target=last["target"],
                reason=f"two consecutive failures on {last['skill']}/{last['target']}",
            )

    # Same-skill spam: if the last N rounds are all the same non-custom skill
    # and none of them cleared the acceptance threshold, the LLM is stuck
    # retrying variants of a skill that isn't working on this design.
    # Issue a skip_skill action; the agent.py handler blacklists every
    # remaining target of that skill.
    if len(history) >= SAME_SKILL_SPAM_N:
        recent = history[-SAME_SKILL_SPAM_N:]
        recent_skills = {r["skill"] for r in recent}
        if (
            len(recent_skills) == 1
            and next(iter(recent_skills)) not in DEEP_ONLY_SKILLS
            and all(not r.get("accepted") for r in recent)
            and all(r["delta_wns"] < SAME_SKILL_SPAM_DELTA for r in recent)
        ):
            spam_skill = recent[0]["skill"]
            # Avoid re-firing: only act if there are still candidates to kill
            if (
                memory.remaining_candidates.get(spam_skill)
                and not _has_active_skill_cooldown(memory, spam_skill)
            ):
                logger.info(
                    f"[decision] {spam_skill} tried {SAME_SKILL_SPAM_N}x with "
                    f"delta<{SAME_SKILL_SPAM_DELTA} each → skip_skill"
                )
                return Action(
                    "skip_skill",
                    skill=spam_skill,
                    reason=f"{spam_skill} had {SAME_SKILL_SPAM_N} consecutive non-improvements",
                )

    # All candidates exhausted and deep mode already used up → exit
    all_empty = all(len(v) == 0 for v in memory.remaining_candidates.values())
    if all_empty and memory.deep_calls_used >= DEEP_MAX_CALLS:
        logger.info("[decision] all candidates exhausted and deep budget used → save_and_exit")
        return Action("save_and_exit", reason="all candidates and deep budget exhausted")

    # No deterministic answer
    return None


def should_deep_analyze(memory) -> bool:
    """
    True when the LLM should receive raw tool output for deep analysis.

    Conditions:
    - token_remaining > $0.50 (at least half budget remains)
    - time_remaining > 1200s (at least 20 minutes)
    - deep_calls_used < 3
    - all standard candidates are exhausted
    """
    budget_ok = memory.budget.get("tokens_remaining", 0) > DEEP_TOKEN_THRESHOLD_USD
    time_ok = memory.budget.get("time_remaining", 0) > DEEP_TIME_THRESHOLD_S
    calls_ok = memory.deep_calls_used < DEEP_MAX_CALLS
    all_empty = all(len(v) == 0 for v in memory.remaining_candidates.values())
    return budget_ok and time_ok and calls_ok and all_empty


def should_batch_fanout(memory) -> bool:
    """
    True if there are multiple fanout candidates and no recent fanout success.
    Batching reduces round-trips: optimize all nets before routing.
    """
    fanout_candidates = memory.remaining_candidates.get("fanout_opt", [])
    if len(fanout_candidates) < 2:
        return False

    # Don't batch if a previous fanout attempt failed badly
    recent_fanout = [
        r for r in memory.history[-5:]
        if r["skill"] == "fanout_opt" and not r["accepted"]
    ]
    return len(recent_fanout) == 0
