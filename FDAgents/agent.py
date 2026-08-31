"""
agent.py — main orchestrator for FDAgents.

Usage:
    python -m FDAgents.agent \\
        --input  input.dcp \\
        --output /tmp/out.dcp \\
        --model  gpt-4o-mini \\
        --time-limit 3600

Architecture:
  - Python calls MCP tools directly (no LLM in the loop for tool calls)
  - Tool output parsed to compact summary (<200 chars)
  - LLM sees only summary + compact state JSON (~3K tokens, fixed)
  - LLM calls: analyze_timing(1) + decide_next_action(N) + tune_params + emergency
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from .mcp_client import MCPClient
from .memory import Memory, _blacklist_params_signature
from .llm import LLMClient
from . import decision as rules
from .recipe_planner import load_default_knowledge_rows, plan_recipe
from .skills import SKILLS, DEEP_ONLY_SKILLS
from .skills.base import parse_timing_summary_static
from .skills.force_replicate import _normalize_unroute_scope

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger(__name__)

load_dotenv(Path(__file__).parent.parent / ".env", override=False)

DEFAULT_MODEL = (
    os.environ.get("FDAGENTS_MODEL")
    or os.environ.get("OPENAI_MODEL")
    or os.environ.get("ANTHROPIC_MODEL")
    or "gpt-4o-mini"
)
TOKEN_BUDGET_USD = float(os.environ.get("FDAGENTS_TOKEN_BUDGET", "1.0"))

# Absolute hard cap on optimization rounds — last-line sanity check against
# rules-layer bugs (e.g. patch H fixed a decision.py rollback loop that
# burned 6M iterations on fdagents_run-20260414_193107 before the time
# budget expired). Under normal operation we do ~15-30 rounds per run.
MAX_ROUNDS = int(os.environ.get("FDAGENTS_MAX_ROUNDS", "300"))
FORCE_REPLICATE_MIN_FANOUT = int(os.environ.get("FDAGENTS_FORCE_REPLICATE_MIN_FANOUT", "30"))
FORCE_REPLICATE_MAX_CANDIDATES = int(os.environ.get("FDAGENTS_FORCE_REPLICATE_MAX_CANDIDATES", "6"))


class RuleOnlyLLMStats:
    """LLM-compatible zero-usage stats for --no-llm runs."""

    model = "rule-only"
    call_count = 0
    total_cost = 0.0
    total_input_tokens = 0
    total_cached_input_tokens = 0
    total_uncached_input_tokens = 0
    total_output_tokens = 0



SOFT_BLACKLIST_HARD_LOSS_NS = float(
    os.environ.get("FDAGENTS_SOFT_BLACKLIST_HARD_LOSS_NS", "0.05")
)
ACCEPT_MIN_DELTA_NS = float(os.environ.get("FDAGENTS_ACCEPT_MIN_DELTA_NS", "0.0005"))
PARAM_VARIANT_ACCEPTED_SKILLS = {"force_replicate"}
ROUTE_FANOUT_ACCEPTED_RETRY_LIMIT = int(
    os.environ.get("FDAGENTS_ROUTE_FANOUT_ACCEPTED_RETRY_LIMIT", "0")
)
LLM_DECISION_RETRY_ATTEMPTS = int(os.environ.get("FDAGENTS_LLM_DECISION_RETRIES", "8"))
LLM_DECISION_RETRY_INITIAL_DELAY_S = float(
    os.environ.get("FDAGENTS_LLM_DECISION_RETRY_INITIAL_DELAY_S", "5.0")
)
LLM_DECISION_RETRY_MAX_DELAY_S = float(
    os.environ.get("FDAGENTS_LLM_DECISION_RETRY_MAX_DELAY_S", "60.0")
)


def _call_llm_with_retry(
    call_fn,
    *,
    description: str,
    max_attempts: int = LLM_DECISION_RETRY_ATTEMPTS,
    initial_delay_s: float = LLM_DECISION_RETRY_INITIAL_DELAY_S,
    max_delay_s: float = LLM_DECISION_RETRY_MAX_DELAY_S,
    sleep_fn=time.sleep,
) -> dict:
    attempts = max(1, int(max_attempts))
    delay_s = max(0.0, float(initial_delay_s))
    cap_s = max(delay_s, float(max_delay_s))
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            return call_fn()
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                logger.error(
                    "[llm] %s failed after %d attempts; no fallback action will be used",
                    description,
                    attempts,
                )
                raise
            logger.warning(
                "[llm] %s failed on attempt %d/%d: %s; retrying in %.1fs",
                description,
                attempt,
                attempts,
                exc,
                delay_s,
            )
            sleep_fn(delay_s)
            delay_s = min(cap_s, delay_s * 2 if delay_s > 0 else 0.0)

    raise RuntimeError(f"{description} retry loop exited unexpectedly: {last_error}")


def _accepts_result(result, params: Optional[dict] = None) -> bool:
    params = params or {}
    try:
        min_delta = float(params.get("accept_min_delta_ns", ACCEPT_MIN_DELTA_NS))
    except (TypeError, ValueError):
        min_delta = ACCEPT_MIN_DELTA_NS
    return bool(result.is_legal and result.delta_wns > min_delta)


def _is_blacklisted(
    memory,
    skill: str,
    target: str,
    params: Optional[dict] = None,
    *,
    include_soft: bool = True,
) -> bool:
    params_sig = _blacklist_params_signature(params)
    current_best_round = memory.current_best.get("round")
    for entry in memory.blacklist:
        if entry.get("skill") != skill:
            continue
        entry_target = entry.get("target")
        if entry_target not in {target, "*"}:
            continue

        severity = entry.get("severity", "hard")
        if severity == "hard":
            return True

        if not include_soft:
            continue

        best_round = entry.get("best_round")
        if best_round is not None and best_round != current_best_round:
            continue

        if severity == "cooldown":
            return True

        if severity == "soft" and entry.get("params_sig", "{}") == params_sig:
            return True
    return False


def _blacklist_severity_for_result(result) -> str:
    if not result.success or not result.is_legal:
        return "hard"
    if result.delta_wns < -SOFT_BLACKLIST_HARD_LOSS_NS:
        return "hard"
    return "soft"


def _was_accepted(
    memory,
    skill: str,
    target: str,
    params: Optional[dict] = None,
) -> bool:
    allow_route_fanout_retry = _route_fanout_accepted_retry_allowed(
        memory,
        skill,
        target,
        params,
    )
    for h in memory.history:
        if not (h["skill"] == skill and h["target"] == target and h["accepted"]):
            continue
        if skill in PARAM_VARIANT_ACCEPTED_SKILLS:
            if params is None:
                continue
            if _blacklist_params_signature(h.get("params")) == _blacklist_params_signature(params):
                return True
            continue
        if allow_route_fanout_retry:
            continue
        return True
    return False


def _route_fanout_accepted_retry_allowed(
    memory,
    skill: str,
    target: str,
    params: Optional[dict],
) -> bool:
    """Fanout ECOs can become beneficial again after another accepted fanout move."""
    if skill != "fanout_opt":
        return False
    if not _timing_profile_is_route_dominated(memory.baseline.get("timing_profile")):
        return False
    if params is None:
        return False

    params_sig = _blacklist_params_signature(params)
    current_best_round = memory.current_best.get("round")
    accepted_rounds = [
        h.get("round")
        for h in memory.history
        if h.get("skill") == skill
        and h.get("target") == target
        and h.get("accepted")
        and _blacklist_params_signature(h.get("params")) == params_sig
    ]
    if not accepted_rounds:
        return False
    if current_best_round in accepted_rounds:
        return False
    return len(accepted_rounds) < ROUTE_FANOUT_ACCEPTED_RETRY_LIMIT


def _prepend_unique(existing: list[str], preferred: list[str]) -> list[str]:
    result = []
    for item in [*preferred, *existing]:
        if item not in result:
            result.append(item)
    return result


PBLOCK_FAMILY_SKILLS = {"pblock", "pblock_sweep"}

DEEP_FREEFORM_TARGET_SKILLS = {
    "cell_replace",
    "lut_merge",
    "lut_pin_swap",
    "pblock",
    "pblock_sweep",
}
TARGET_PROVENANCE_REQUIRED_SKILLS = {
    "fanout_opt",
    "force_replicate",
    "per_net_unroute",
    "checkpoint_import",
}
FALLBACK_SKILL_PRIORITY = [
    "clock_tighten",
    "phys_opt",
    "endpoint_bel_move",
    "fresh_place_route",
    "pblock_sweep",
    "pblock",
    "force_replicate",
    "fanout_opt",
    "per_net_unroute",
    "critical_net_reroute",
    "critical_cluster_anchor",
    "path_local_lut_reflow",
    "cell_replace",
    "lut_merge",
    "lut_pin_swap",
    "post_route_cleanup",
    "checkpoint_import",
]
FALLBACK_PARAMS_BY_SKILL = {
    # Keep automatic fallback from spending 4-5 minutes on the historical
    # most-aggressive force flow. The LLM can still request global+Alternate.
    "force_replicate": {
        "passes": 1,
        "unroute_scope": "global",
        "route_directive": "Default",
    },
}
PBLOCK_RELEASE_SEED_PARAMS = {
    "release_after_place": True,
    "place_directive": "Explore",
    "post_route_phys_opt": "AggressiveFanoutOpt",
    "target_ff_multiplier": 0.0,
    "guidance_only": True,
    "validate_resources": False,
    "max_expansion_attempts": 0,
}
PBLOCK_HARD_MACRO_SEED_PARAMS = {
    "release_after_place": False,
    "place_directive": "Default",
    "pre_route_phys_opt": "Explore",
    "route_directive": "Explore",
    "post_route_phys_opt": "AggressiveExplore",
    "target_ff_multiplier": 0.0,
    "guidance_only": True,
    "validate_resources": False,
    "max_expansion_attempts": 0,
}


def _requires_remaining_candidate(skill_name: str, deep_mode_active: bool) -> bool:
    if skill_name in DEEP_ONLY_SKILLS:
        return False
    if deep_mode_active and skill_name in DEEP_FREEFORM_TARGET_SKILLS:
        return False
    if skill_name in TARGET_PROVENANCE_REQUIRED_SKILLS:
        return True
    return True


def _is_hard_macro_pblock_sweep_refusal(entry: dict) -> bool:
    if entry.get("skill") != "pblock_sweep":
        return False
    if entry.get("accepted"):
        return False
    text = " ".join(
        str(entry.get(key, ""))
        for key in ("summary", "reason", "message")
    ).lower()
    return "pblock_sweep" in text and "hard macro" in text


def _pblock_sweep_hard_macro_refused(memory) -> bool:
    return any(_is_hard_macro_pblock_sweep_refusal(entry) for entry in memory.history)


def _pblock_has_been_tried(memory) -> bool:
    return any(
        entry.get("skill") == "pblock"
        or (
            entry.get("skill") == "pblock_sweep"
            and not _is_hard_macro_pblock_sweep_refusal(entry)
        )
        for entry in memory.history
    )


def _pblock_candidate_available(memory) -> bool:
    return any(memory.remaining_candidates.get(skill) for skill in PBLOCK_FAMILY_SKILLS)


def _timing_profile_is_slice_route_dominated(profile: Optional[dict]) -> bool:
    if not isinstance(profile, dict) or not profile:
        return False

    placement_spread = profile.get("placement_spread") or {}
    site_counts = placement_spread.get("site_type_counts") or {}
    if not site_counts:
        return False
    non_slice_sites = {
        str(site_type)
        for site_type, count in site_counts.items()
        if int(count or 0) > 0 and str(site_type).upper() != "SLICE"
    }
    if non_slice_sites:
        return False

    avg_route_pct = float(profile.get("avg_route_pct") or 0.0)
    path_count = int(profile.get("path_count") or 0)
    route_dominated_paths = int(profile.get("route_dominated_paths") or 0)
    route_path_ratio = route_dominated_paths / path_count if path_count else 0.0
    spread_paths = int(placement_spread.get("spread_path_count") or 0)

    return (
        avg_route_pct >= 70.0
        or route_path_ratio >= 0.75
        or (avg_route_pct >= 60.0 and spread_paths > 0)
    )


def _should_prioritize_pblock_sweep(memory) -> bool:
    """Try generic slice pblock sweep before mutating high-fanout placement."""
    if _should_prioritize_post_route_cleanup(memory):
        return False
    if _pblock_has_been_tried(memory):
        return False
    bottleneck = str(memory.baseline.get("bottleneck") or "").lower()
    if "fanout" in bottleneck:
        for target in memory.remaining_candidates.get("force_replicate", []):
            if not _is_blacklisted(memory, "force_replicate", target):
                return False
    for target in memory.remaining_candidates.get("pblock_sweep", []):
        if not _is_blacklisted(memory, "pblock_sweep", target):
            break
    else:
        return False
    return _timing_profile_is_slice_route_dominated(
        memory.baseline.get("timing_profile")
    )


def _should_prioritize_dynamic_pblock(memory) -> bool:
    if _should_prioritize_post_route_cleanup(memory):
        return False
    if not _pblock_sweep_hard_macro_refused(memory):
        return False
    if _pblock_has_been_tried(memory):
        return False
    if not memory.remaining_candidates.get("pblock"):
        return False
    return any(
        not _is_blacklisted(memory, "pblock", target)
        for target in memory.remaining_candidates.get("pblock", [])
    )


def _should_prioritize_post_route_cleanup(memory) -> bool:
    """Prefer bounded late cleanup for near-closure route-dominated designs."""
    profile = memory.baseline.get("timing_profile")
    route_dominated = _timing_profile_is_route_dominated(profile)
    preferred_target = "pre_lut_directive" if route_dominated else "auto"
    target = _available_target(memory, "post_route_cleanup", preferred_target)
    if target is None and route_dominated:
        target = _available_target(memory, "post_route_cleanup", "post_lut_cleanup")
    if target is None:
        target = _available_target(memory, "post_route_cleanup")
    if target is None:
        return False
    if _time_budget_blocks_skill(
        memory,
        "post_route_cleanup",
        _merge_skill_params_for_target(memory, "post_route_cleanup", target, {}),
    ):
        return False

    try:
        current_wns = float(memory.current_best.get("wns"))
    except (TypeError, ValueError):
        return False
    if current_wns >= 0:
        return False

    if route_dominated:
        if _available_target(memory, "post_route_cleanup", "pre_lut_directive") is not None:
            return current_wns >= -0.400
        if _available_target(memory, "post_route_cleanup", "post_lut_cleanup") is not None:
            return current_wns >= -0.325 or (
                current_wns >= -0.400 and _route_lut_pin_swap_attempted(memory)
            )
        return current_wns >= -0.400

    clock_period = memory.baseline.get("clock_period")
    try:
        clock_period = float(clock_period)
    except (TypeError, ValueError):
        clock_period = None
    close_threshold = max(0.35, 0.25 * clock_period) if clock_period else 0.35
    if route_dominated:
        close_threshold = max(close_threshold, 0.40)
    if current_wns < -close_threshold:
        return False

    if not isinstance(profile, dict):
        return False
    dominant = str(profile.get("dominant_bottleneck") or "").lower()
    avg_route_pct = float(profile.get("avg_route_pct") or 0.0)
    path_count = int(profile.get("path_count") or 0)
    route_dominated_paths = int(profile.get("route_dominated_paths") or 0)
    route_ratio = route_dominated_paths / path_count if path_count else 0.0
    return dominant == "routing" or avg_route_pct >= 65.0 or route_ratio >= 0.65


def _timing_profile_is_route_dominated(profile: Optional[dict]) -> bool:
    if not isinstance(profile, dict):
        return False
    dominant = str(profile.get("dominant_bottleneck") or "").lower()
    avg_route_pct = float(profile.get("avg_route_pct") or 0.0)
    path_count = int(profile.get("path_count") or 0)
    route_dominated_paths = int(profile.get("route_dominated_paths") or 0)
    route_ratio = route_dominated_paths / path_count if path_count else 0.0
    return dominant == "routing" or avg_route_pct >= 65.0 or route_ratio >= 0.65


def _should_prioritize_critical_net_reroute(memory) -> bool:
    """Try route-preserving micro ECO after broad cleanup is exhausted."""
    try:
        current_wns = float(memory.current_best.get("wns"))
    except (TypeError, ValueError):
        return False
    if current_wns >= 0:
        return False

    if _timing_profile_is_route_dominated(memory.baseline.get("timing_profile")):
        if not _timing_profile_is_route_dominated(memory.baseline.get("timing_profile")):
            return False
        if (
            -0.400 <= current_wns < -0.385
            and _available_target(memory, "critical_net_reroute", "early_window") is not None
        ):
            return True
        final_polish_ready = current_wns >= -0.325 or (
            current_wns >= -0.400 and _route_post_lut_cleanup_attempted(memory)
        )
        if final_polish_ready:
            return (
                _available_target(memory, "critical_net_reroute", "high_delay10") is not None
                or _available_target(memory, "critical_net_reroute", "top3_final") is not None
            )
        if _available_target(memory, "critical_net_reroute", "auto") is not None:
            return (
                current_wns >= -0.400
                and not _should_prioritize_post_route_cleanup(memory)
                and not _should_prioritize_pblock_sweep(memory)
            )
        return False

    target = _available_target(memory, "critical_net_reroute", "auto")
    if target is None:
        return False
    if _should_prioritize_post_route_cleanup(memory):
        return False
    if _should_prioritize_pblock_sweep(memory):
        return False

    clock_period = memory.baseline.get("clock_period")
    try:
        clock_period = float(clock_period)
    except (TypeError, ValueError):
        clock_period = None
    close_threshold = max(0.40, 0.26 * clock_period) if clock_period else 0.40
    if current_wns < -close_threshold:
        return False

    return _timing_profile_is_route_dominated(memory.baseline.get("timing_profile"))


def _should_prioritize_lut_pin_swap(memory) -> bool:
    """Let route-clean near-closure profiles use RW LUT pin swaps before more fanout."""
    if not _timing_profile_is_route_dominated(memory.baseline.get("timing_profile")):
        return False
    if _route_lut_pin_swap_attempted(memory):
        return False
    target = _available_target(memory, "lut_pin_swap", "auto")
    if target is None:
        return False
    if _should_prioritize_post_route_cleanup(memory):
        return False
    if _should_prioritize_critical_net_reroute(memory):
        return False
    try:
        current_wns = float(memory.current_best.get("wns"))
    except (TypeError, ValueError):
        return False
    if not _timing_profile_is_route_dominated(memory.baseline.get("timing_profile")):
        return False
    phys_explore_pending = (
        _available_target(memory, "phys_opt", "Explore") is not None
        and not _route_phys_opt_explore_attempted(memory)
    )
    if phys_explore_pending and _route_pre_lut_cleanup_attempted(memory):
        return False
    if current_wns >= -0.365:
        return True
    return (
        current_wns >= -0.400
        and _route_pre_lut_cleanup_attempted(memory)
        and not phys_explore_pending
    )


def _should_prioritize_route_phys_opt(memory) -> bool:
    """Try phys_opt Explore before LUT pin swap on route-dominated near-closure profiles."""
    if not _timing_profile_is_route_dominated(memory.baseline.get("timing_profile")):
        return False
    if _available_target(memory, "phys_opt", "Explore") is None:
        return False
    if _route_phys_opt_explore_attempted(memory):
        return False
    if _should_prioritize_post_route_cleanup(memory):
        return False
    if _should_prioritize_critical_net_reroute(memory):
        return False
    try:
        current_wns = float(memory.current_best.get("wns"))
    except (TypeError, ValueError):
        return False
    if not (-0.405 <= current_wns < -0.340):
        return False
    return (
        _route_pre_lut_cleanup_attempted(memory)
        and _timing_profile_is_route_dominated(memory.baseline.get("timing_profile"))
    )


def _route_pre_lut_cleanup_attempted(memory) -> bool:
    return any(
        h.get("skill") == "post_route_cleanup"
        and h.get("target") == "pre_lut_directive"
        for h in memory.history
    )


def _route_phys_opt_explore_attempted(memory) -> bool:
    return any(
        h.get("skill") == "phys_opt" and h.get("target") == "Explore"
        for h in memory.history
    )


def _route_lut_pin_swap_attempted(memory) -> bool:
    return any(h.get("skill") == "lut_pin_swap" for h in memory.history)


def _route_post_lut_cleanup_attempted(memory) -> bool:
    return any(
        h.get("skill") == "post_route_cleanup"
        and h.get("target") == "post_lut_cleanup"
        for h in memory.history
    )


def _route_final_reroute_attempted(memory) -> bool:
    return any(
        h.get("skill") == "critical_net_reroute"
        and h.get("target") in {"high_delay10", "top3_final"}
        for h in memory.history
    )


def _route_final_polish_ready(memory) -> bool:
    try:
        current_wns = float(memory.current_best.get("wns"))
    except (TypeError, ValueError):
        return False
    return current_wns >= -0.325 or (
        current_wns >= -0.400 and _route_post_lut_cleanup_attempted(memory)
    )


def _route_target_not_ready(memory, skill_name: str, target: str) -> bool:
    if not _timing_profile_is_route_dominated(memory.baseline.get("timing_profile")):
        return False
    try:
        current_wns = float(memory.current_best.get("wns"))
    except (TypeError, ValueError):
        current_wns = None
    if (
        skill_name == "fanout_opt"
        and current_wns is not None
        and current_wns >= -0.340
        and _route_lut_pin_swap_attempted(memory)
    ):
        return True
    if skill_name == "critical_net_reroute" and target in {"high_delay10", "top3_final"}:
        return not _route_final_polish_ready(memory)
    if skill_name == "post_route_cleanup" and target == "post_lut_cleanup":
        return not _route_lut_pin_swap_attempted(memory)
    return False


def _defer_structural_auto_skill(memory, skill_name: str) -> bool:
    bottleneck = memory.baseline.get("bottleneck", "unknown")
    if skill_name == "lut_pin_swap" and _should_prioritize_lut_pin_swap(memory):
        return False
    if skill_name == "cell_replace":
        matching_bottleneck = bottleneck == "placement"
    elif skill_name == "lut_merge":
        matching_bottleneck = bottleneck == "logic_depth"
    elif skill_name == "lut_pin_swap":
        matching_bottleneck = bottleneck in {"routing", "mixed"}
    else:
        return False

    if matching_bottleneck:
        return False
    if _pblock_candidate_available(memory) and not _pblock_has_been_tried(memory):
        return True
    return bool(
        memory.remaining_candidates.get("force_replicate")
        or memory.remaining_candidates.get("per_net_unroute")
    )


def _available_recipe_core_skill(memory) -> Optional[str]:
    """Return the first untried core skill for a high-confidence recipe."""
    recipe_id = memory.baseline.get("recipe_id")
    if not recipe_id:
        return None
    core_skills = memory.baseline.get("recipe_core_skills") or []
    for skill_name in core_skills:
        candidates = memory.remaining_candidates.get(skill_name, [])
        for cand in candidates:
            params = _merge_skill_params_for_target(memory, skill_name, cand, {})
            params = _fit_skill_scope_to_budget(memory, skill_name, params)
            if _time_budget_blocks_skill(memory, skill_name, params):
                continue
            if _is_blacklisted(memory, skill_name, cand, params):
                continue
            if _was_accepted(memory, skill_name, cand, params):
                continue
            return skill_name
    return None


def _action_override_reason(
    memory,
    action_dict: dict,
    skill_name: str,
    target: str,
    *,
    deep_mode_active: bool,
    params: Optional[dict] = None,
) -> Optional[str]:
    """Return the reason an LLM action must be overridden, else None."""
    is_deep_only = skill_name in DEEP_ONLY_SKILLS
    action_params = action_dict.get("params", {}) if params is None else params

    if "error" in action_dict:
        return f"LLM error: {action_dict.get('error')}"
    if skill_name not in SKILLS:
        return f"unknown skill {skill_name!r}"
    if is_deep_only and not deep_mode_active:
        return f"{skill_name!r} is deep-mode-only"
    if is_deep_only and memory.custom_budget_left() <= 0:
        return (
            f"{skill_name!r} budget exhausted "
            f"({memory.custom_calls_used}/{memory.CUSTOM_CALL_BUDGET})"
        )
    if not is_deep_only and _time_budget_blocks_skill(memory, skill_name, action_params):
        return f"{skill_name!r} needs more remaining time"
    recipe_core_skill = _available_recipe_core_skill(memory)
    if (
        not is_deep_only
        and recipe_core_skill is not None
        and skill_name != recipe_core_skill
    ):
        return (
            f"recipe {memory.baseline.get('recipe_id')} should try "
            f"{recipe_core_skill} before {skill_name}"
        )
    if not is_deep_only and _is_blacklisted(
        memory,
        skill_name,
        target,
        action_params,
    ):
        return f"({skill_name},{target!r}) is blacklisted"
    if not is_deep_only and _defer_structural_auto_skill(memory, skill_name):
        return f"{skill_name!r} deferred until pblock is tried"
    if (
        not is_deep_only
        and _route_target_not_ready(memory, skill_name, target)
    ):
        return f"route-profile staged target {target!r} is not ready"
    if (
        not is_deep_only
        and skill_name != "post_route_cleanup"
        and _should_prioritize_post_route_cleanup(memory)
    ):
        return "near-closure route-dominated profile should try post_route_cleanup"
    if (
        not is_deep_only
        and skill_name != "pblock"
        and _should_prioritize_dynamic_pblock(memory)
    ):
        return "hard-macro pblock_sweep refusal should try dynamic pblock"
    if (
        not is_deep_only
        and skill_name != "pblock_sweep"
        and _should_prioritize_pblock_sweep(memory)
    ):
        return "slice route-dominated profile should try pblock_sweep first"
    if (
        not is_deep_only
        and skill_name != "critical_net_reroute"
        and _should_prioritize_critical_net_reroute(memory)
    ):
        return "near-closure route-dominated profile should try critical_net_reroute"
    if (
        not is_deep_only
        and skill_name != "phys_opt"
        and _should_prioritize_route_phys_opt(memory)
    ):
        return "route-profile pre-LUT cleanup should try phys_opt Explore"
    if (
        not is_deep_only
        and skill_name != "lut_pin_swap"
        and _should_prioritize_lut_pin_swap(memory)
    ):
        return "route-profile near-closure should try lut_pin_swap"
    if not is_deep_only and _was_accepted(memory, skill_name, target, action_params):
        return f"({skill_name},{target!r}) already accepted"
    if (
        not is_deep_only
        and _requires_remaining_candidate(skill_name, deep_mode_active)
        and target not in memory.remaining_candidates.get(skill_name, [])
    ):
        return f"({skill_name},{target!r}) not in remaining candidates"

    return None


def _time_budget_blocks_skill(memory, skill_name: str, params: Optional[dict]) -> bool:
    """Avoid starting multi-candidate long routes too late in a 1h run."""
    try:
        remaining = float(memory.budget.get("time_remaining", 0.0))
    except (TypeError, ValueError):
        remaining = 0.0
    params = params or {}
    min_remaining = params.get("min_remaining_s")
    if min_remaining is not None:
        try:
            return remaining < float(min_remaining)
        except (TypeError, ValueError):
            pass
    def _int_param(name: str, default: int) -> int:
        try:
            return int(params.get(name, default))
        except (TypeError, ValueError):
            return default

    if skill_name == "force_replicate":
        unroute_scope = str(params.get("unroute_scope", "global")).lower()
        full_unroute = unroute_scope in {"", "all", "design", "full", "full_design", "global"}
        return full_unroute and remaining < 1200.0
    if skill_name == "fresh_place_route":
        max_candidates = _int_param("max_candidates", 2)
        if remaining < 1200.0:
            return True
        return max_candidates > 1 and remaining < 2400.0
    if skill_name == "clock_tighten":
        max_candidates = _int_param("max_candidates", 3)
        # A sweep that stops at the first legal candidate costs ~one place&route
        # pass regardless of how many factors/recipes are listed, so it must not
        # be charged the multi-candidate floor — doing so starves the single most
        # valuable action (the clock-period sweep) on positive-slack / cold
        # designs whose recipe is exactly this stop-after-first-legal form.
        stop_first = bool(params.get("stop_after_first_legal"))
        effective_multi = max_candidates > 1 and not stop_first
        if remaining < 900.0:
            return True
        return effective_multi and remaining < 1200.0
    if skill_name == "pblock_sweep":
        max_candidates = _int_param("max_candidates", 4)
        if remaining < 1200.0:
            return True
        return max_candidates > 1 and remaining < 2400.0
    if skill_name == "pblock":
        return remaining < 1200.0
    if skill_name == "post_route_cleanup":
        max_candidates = max(1, _int_param("max_candidates", 8))
        max_iterations = max(1, _int_param("max_iterations", 2))
        attempts = max_candidates * max_iterations
        if attempts == 1:
            return remaining < 300.0
        if remaining < 900.0:
            return True
        if attempts > 4 and remaining < 2400.0:
            return True
        return attempts > 1 and remaining < 1800.0
    return False


# Long, multi-candidate skills that should degrade their scope to fit the
# remaining time budget rather than be abandoned for cheaper (and, on
# positive-slack / cold designs, ineffective) fine-grained skills.
_SCOPE_DEGRADABLE_SKILLS = frozenset(
    {"clock_tighten", "fresh_place_route", "pblock_sweep", "post_route_cleanup"}
)


def _fit_skill_scope_to_budget(memory, skill_name: str, params: Optional[dict]) -> dict:
    """Return params trimmed so a degradable skill fits the remaining budget.

    If the skill already fits at full scope, params are returned unchanged. If it
    is time-blocked but a reduced single-candidate sweep would fit, the reduced
    params are returned (so the high-value action still runs, just smaller). If
    even the reduced scope cannot fit, the original params are returned (the
    caller's time-budget check then still blocks it). Only skills in
    `_SCOPE_DEGRADABLE_SKILLS` are ever trimmed.
    """
    params = dict(params or {})
    if skill_name not in _SCOPE_DEGRADABLE_SKILLS:
        return params
    if not _time_budget_blocks_skill(memory, skill_name, params):
        return params
    reduced = dict(params)
    reduced["max_candidates"] = 1
    if "max_iterations" in reduced:
        reduced["max_iterations"] = 1
    factors = reduced.get("factors")
    if isinstance(factors, (list, tuple)) and len(factors) > 1:
        reduced["factors"] = list(factors)[:1]
    recipes = reduced.get("recipes")
    if isinstance(recipes, (list, tuple)) and len(recipes) > 1:
        reduced["recipes"] = list(recipes)[:1]
    if _time_budget_blocks_skill(memory, skill_name, reduced):
        return params
    return reduced


def _is_clock_like_net_name(net_name: str) -> bool:
    lowered = net_name.lower()
    parts = [
        re.sub(r"(?:\[\d+\])+$", "", p.lower())
        for p in net_name.replace("[", "/").split("/")
        if p
    ]
    if any(p in {"clk", "clock"} for p in parts):
        return True
    if any(
        p in {"ap_clk", "aclk"}
        or p.startswith(("clk_", "clock_"))
        or p.endswith(("_clk", "_clock", "_aclk"))
        for p in parts
    ):
        return True
    clock_fragments = (
        "bufg",
        "mmcm",
        "pll",
        "wclk",
        "rclk",
        "wrclk",
        "rdclk",
        "txoutclk",
        "rxoutclk",
        "userclk",
        "coreclk",
    )
    return any(fragment in lowered for fragment in clock_fragments)


def _is_unsuitable_force_replicate_net_name(net_name: str) -> bool:
    """Filter targets that describe hard-macro pins rather than data/control nets."""
    if _is_clock_like_net_name(net_name):
        return True
    leaf = net_name.rsplit("/", 1)[-1].upper()
    leaf = re.sub(r"(?:\[\d+\])+$", "", leaf)
    hard_macro_leaf_patterns = (
        r"ADDR[A-D](?:\d+)?",
        r"RAM[A-D](?:_[A-Z]+\d*)?",
        r"DOUT[A-D]DOUT(?:\d+)?",
        r"DIN[A-D]DIN(?:\d+)?",
        r"DO[A-D]DO(?:\d+)?",
        r"DI[A-D]DI(?:\d+)?",
        r"CLK[A-D]?(?:RDCLK|WRCLK)?",
        r"[WR]CLK",
        r"WE[A-D]?(?:\d+)?",
        r"EN[A-D]?(?:\d+)?",
    )
    return any(re.fullmatch(pattern, leaf) for pattern in hard_macro_leaf_patterns)


def _is_unsuitable_fanout_opt_net_name(net_name: str) -> bool:
    """Filter RapidWright fanout targets that are usually memory/address plumbing."""
    if _is_clock_like_net_name(net_name):
        return True

    lowered = net_name.lower()
    memory_address_patterns = (
        r"(?:^|[/_])addr(?:ess)?\d*(?:$|[/_\[])",
        r"(?:^|[/_])address\d*(?:$|[/_\[])",
        r"(?:^|[/_])addr_reg(?:$|[/_\[])",
    )
    return any(re.search(pattern, lowered) for pattern in memory_address_patterns)


def _fanout_opt_candidates_from_fanout(
    fanout_nets: list[tuple[str, int, int]],
    min_fanout: int = 100,
    max_candidates: int = 10,
) -> list[str]:
    """Pick broadcast/control-like fanout nets suitable for RapidWright splitting."""
    candidates = []
    for net_name, fanout, _path_count in fanout_nets:
        if fanout < min_fanout:
            continue
        if _is_unsuitable_fanout_opt_net_name(net_name):
            continue
        if net_name in candidates:
            continue
        candidates.append(net_name)
        if len(candidates) >= max_candidates:
            break
    return candidates


def _force_replicate_candidates_from_fanout(
    fanout_nets: list[tuple[str, int, int]],
    min_fanout: int = FORCE_REPLICATE_MIN_FANOUT,
    max_candidates: int = FORCE_REPLICATE_MAX_CANDIDATES,
) -> list[str]:
    """Pick route-dominant source/control net candidates from critical fanout data."""
    ranked = sorted(
        fanout_nets,
        key=lambda item: (-item[2], -item[1], item[0]),
    )
    candidates = []
    for net_name, fanout, _path_count in ranked:
        if fanout < min_fanout:
            continue
        if _is_unsuitable_force_replicate_net_name(net_name):
            continue
        if net_name in candidates:
            continue
        candidates.append(net_name)
        if len(candidates) >= max_candidates:
            break
    return candidates


def _force_replicate_candidates_from_timing_report(
    timing_report: str,
    min_fanout: int = FORCE_REPLICATE_MIN_FANOUT,
    max_candidates: int = FORCE_REPLICATE_MAX_CANDIDATES,
) -> list[str]:
    """
    Extract source-side high-fanout data nets from full critical path reports.

    Global force replication is most useful on the first non-clock net launched
    by a source FF/LUT/control driver. Later intermediate nets are better left
    to per_net_unroute.
    """
    candidates = []
    sections = re.split(r"(?m)^Slack \(", timing_report)
    for section in sections[1:]:
        source_ref_match = re.search(r"rising edge-triggered cell\s+(\S+)", section)
        source_ref = source_ref_match.group(1) if source_ref_match else ""
        if source_ref and not (
            source_ref.startswith("FD")
            or source_ref.startswith("LD")
            or source_ref.startswith("LUT")
        ):
            continue

        seen_source_q = False
        for line in section.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if seen_source_q and stripped.startswith("---"):
                break

            if not seen_source_q:
                # Timing reports list the source clock pin section first,
                # then a separator, then the launched Q/O data pin.
                if re.search(r"/(?:Q|O)(?:\s|$)", stripped):
                    seen_source_q = True
                continue

            match = re.search(
                r"net\s+\(fo=(\d+),\s*(?:routed|estimated|unset)\)\s+"
                r"([+-]?\d+(?:\.\d+)?)",
                line,
            )
            if not match:
                continue
            fanout = int(match.group(1))

            parts = line.split()
            net_name = ""
            for part in reversed(parts):
                if part.startswith("("):
                    continue
                if "/" in part or "[" in part or "_" in part:
                    net_name = part
                    break
            if not net_name:
                continue
            if fanout < min_fanout:
                continue
            if _is_unsuitable_force_replicate_net_name(net_name):
                continue
            if net_name not in candidates:
                candidates.append(net_name)
            break

        if len(candidates) >= max_candidates:
            break
    return candidates


def _initial_force_replicate_candidates(analysis: dict) -> list[str]:
    """Seed force-replication targets from initial critical fanout evidence."""
    return _force_replicate_candidates_from_fanout(
        analysis.get("fanout_nets") or []
    )


def _add_force_replicate_candidates(memory, candidates: list[str]) -> list[str]:
    """Promote current force-replicate candidates that are still actionable."""
    existing = [
        candidate
        for candidate in memory.remaining_candidates.get("force_replicate", [])
        if not _is_unsuitable_force_replicate_net_name(candidate)
    ]
    if memory.baseline.get("recipe_id") == "PBLOCK_CRITICAL_HIGHFANOUT":
        # Preserve the profile-driven group-replication recipe. Promoting freshly
        # reported square-root cone nets turns the run back into the weak
        # grouped-fanout sequence seen in earlier weak-route experiments.
        return [
            candidate for candidate in candidates
            if candidate
            and candidate not in existing
            and not _is_unsuitable_force_replicate_net_name(candidate)
        ]
    global_probe_targets = [
        candidate
        for candidate in existing
        if _force_replicate_needs_global_probe(memory, candidate)
    ]
    promoted = []
    added = []
    for candidate in candidates:
        if not candidate:
            continue
        if candidate in promoted:
            continue
        if _is_unsuitable_force_replicate_net_name(candidate):
            continue
        if _is_blacklisted(
            memory,
            "force_replicate",
            candidate,
            include_soft=False,
        ):
            continue
        if _was_accepted(memory, "force_replicate", candidate):
            continue
        promoted.append(candidate)
        if candidate not in existing:
            added.append(candidate)
    remaining_existing = [
        candidate
        for candidate in existing
        if candidate not in promoted and candidate not in global_probe_targets
    ]
    promoted = [candidate for candidate in promoted if candidate not in global_probe_targets]
    if global_probe_targets or promoted or remaining_existing:
        memory.remaining_candidates["force_replicate"] = (
            global_probe_targets + promoted + remaining_existing
        )
    return added


def _force_replicate_needs_global_probe(memory, target: str) -> bool:
    global_params = dict(FALLBACK_PARAMS_BY_SKILL["force_replicate"])
    if _is_blacklisted(memory, "force_replicate", target, global_params):
        return False
    if _was_accepted(memory, "force_replicate", target, global_params):
        return False

    current_best_round = memory.current_best.get("round")
    for h in memory.history:
        if (
            h.get("skill") == "force_replicate"
            and h.get("target") == target
            and h.get("accepted")
            and h.get("round") == current_best_round
            and _normalize_unroute_scope(
                h.get("params", {}).get("unroute_scope", "global")
            )
            == "net"
        ):
            return True
    return False


def _should_refresh_force_replicate_candidates(memory, accepted_skill: str) -> bool:
    """Refresh source-side replicate candidates only after placement context settles."""
    if accepted_skill in {"pblock", "pblock_sweep", "force_replicate"}:
        return True
    return not _pblock_candidate_available(memory)


def _generic_deterministic_analysis_from_initial(analysis: dict) -> dict:
    """Build a generic startup analysis from measured timing data only."""
    profile = analysis.get("timing_profile") or {}
    fanout_nets = analysis.get("fanout_nets") or []
    max_fanout = max((int(item[1]) for item in fanout_nets), default=0)
    eligible_fanout = _fanout_opt_candidates_from_fanout(fanout_nets)

    dominant = str(profile.get("dominant_bottleneck") or "").lower()
    logic_levels = profile.get("logic_levels") or {}
    max_logic = int(logic_levels.get("max") or 0)
    avg_route_pct = float(profile.get("avg_route_pct") or 0.0)
    path_count = int(profile.get("path_count") or 0)
    route_dominated_paths = int(profile.get("route_dominated_paths") or 0)
    route_ratio = route_dominated_paths / path_count if path_count else 0.0
    fresh_place_recipes = ["extranetdelay_no_relax", "extratiming_no_relax"]
    fresh_place_max_candidates = 2
    endpoint_after_fresh = False
    pblock_seed_params = None
    force_replicate_params = None
    cluster_anchor_params = {
        "num_paths": 12,
        "max_cells": 24,
        "max_nets": 12,
        "bbox_pad_x": 4,
        "bbox_pad_y": 8,
    }
    slice_locality = _timing_profile_is_slice_route_dominated(profile)

    if max_fanout >= 100 and not eligible_fanout and max_logic >= 7 and avg_route_pct >= 45.0:
        bottleneck = "mixed"
        fresh_place_recipes = ["extranetdelay_no_relax"]
        fresh_place_max_candidates = 1
        endpoint_after_fresh = True
        pblock_seed_params = dict(PBLOCK_RELEASE_SEED_PARAMS)
        order = [
            "pblock",
            "fresh_place_route",
            "endpoint_bel_move",
            "phys_opt",
            "post_route_cleanup",
            "critical_net_reroute",
            "lut_pin_swap",
            "cell_replace",
            "per_net_unroute",
            "force_replicate",
            "pblock_sweep",
        ]
    elif max_fanout >= 100:
        bottleneck = "fanout"
        force_replicate_params = {
            "passes": 2,
            "unroute_scope": "global",
            "pre_place_post_place_opt": True,
            "route_directive": "NoTimingRelaxation",
            "route_tns_cleanup": True,
            "post_rep_phys_opt": "AggressiveFanoutOpt",
            "final_phys_opt": "Explore",
            "final_route": False,
        }
        order = [
            "force_replicate",
            "fanout_opt",
            "fresh_place_route",
            "phys_opt",
            "pblock_sweep",
            "critical_cluster_anchor",
            "post_route_cleanup",
            "critical_net_reroute",
            "endpoint_bel_move",
            "per_net_unroute",
            "lut_pin_swap",
            "cell_replace",
        ]
    elif dominant == "logic_depth" or max_logic >= 5:
        bottleneck = "logic_depth"
        order = [
            "lut_merge",
            "fresh_place_route",
            "phys_opt",
            "pblock_sweep",
            "post_route_cleanup",
            "critical_net_reroute",
            "endpoint_bel_move",
            "lut_pin_swap",
            "cell_replace",
        ]
    elif dominant == "placement" and slice_locality:
        bottleneck = "placement"
        order = [
            "critical_cluster_anchor",
            "pblock_sweep",
            "pblock",
            "fresh_place_route",
            "cell_replace",
            "phys_opt",
            "post_route_cleanup",
            "critical_net_reroute",
            "endpoint_bel_move",
            "lut_pin_swap",
        ]
    elif dominant == "placement":
        bottleneck = "placement"
        order = [
            "pblock_sweep",
            "pblock",
            "fresh_place_route",
            "cell_replace",
            "phys_opt",
            "post_route_cleanup",
            "critical_net_reroute",
            "endpoint_bel_move",
            "lut_pin_swap",
        ]
    elif dominant == "mixed" and 45.0 <= avg_route_pct < 65.0 and max_logic <= 4:
        bottleneck = "mixed"
        order = [
            "fresh_place_route",
            "phys_opt",
            "post_route_cleanup",
            "critical_net_reroute",
            "pblock_sweep",
            "critical_cluster_anchor",
            "endpoint_bel_move",
            "per_net_unroute",
            "lut_pin_swap",
            "cell_replace",
        ]
    elif dominant == "routing" or avg_route_pct >= 65.0 or route_ratio >= 0.65:
        bottleneck = "routing"
        order = [
            "phys_opt",
            "fresh_place_route",
            "post_route_cleanup",
            "critical_net_reroute",
            "endpoint_bel_move",
            "critical_cluster_anchor",
            "pblock_sweep",
            "lut_pin_swap",
            "per_net_unroute",
            "force_replicate",
            "fanout_opt",
            "cell_replace",
        ]
    else:
        bottleneck = "mixed"
        order = [
            "phys_opt",
            "fresh_place_route",
            "pblock_sweep",
            "critical_cluster_anchor",
            "fanout_opt",
            "force_replicate",
            "post_route_cleanup",
            "critical_net_reroute",
            "endpoint_bel_move",
            "lut_pin_swap",
            "cell_replace",
        ]

    if max_fanout > 1500:
        split_factor = 5
    elif max_fanout > 500:
        split_factor = 4
    elif max_fanout >= 100:
        split_factor = 3
    else:
        split_factor = 2

    fresh_place_params = {
        "recipes": fresh_place_recipes,
        "max_candidates": fresh_place_max_candidates,
    }
    if endpoint_after_fresh:
        fresh_place_params.update(
            {
                "endpoint_bel_move_after": True,
                "endpoint_num_paths": 20,
                "endpoint_max_candidates": 6,
            }
        )

    initial_params = {
        "phys_opt": {
            "directive_order": [
                "AggressiveFanoutOpt",
                "AggressiveExplore",
                "AddRetime",
                "Default",
            ]
        },
        "fanout_opt": {"split_factor": split_factor},
        "cell_replace": {"detour_threshold": 2.0},
        "fresh_place_route": fresh_place_params,
        "endpoint_bel_move": {"num_paths": 12, "max_candidates": 6},
        "critical_cluster_anchor": cluster_anchor_params,
    }
    if pblock_seed_params is not None:
        initial_params["pblock"] = pblock_seed_params
    if force_replicate_params is not None:
        initial_params["force_replicate"] = force_replicate_params

    return {
        "bottleneck": bottleneck,
        "suggested_skill_order": order,
        "initial_params": initial_params,
        "reason": "rule-only analysis from timing_profile and critical fanout nets",
    }


def _deterministic_analysis_from_initial(
    analysis: dict,
    design_id: Optional[str] = None,
) -> dict:
    """Build startup analysis, letting high-confidence recipes lead."""
    generic = _generic_deterministic_analysis_from_initial(analysis)
    recipe = plan_recipe(
        analysis,
        design_id=design_id,
        knowledge_rows=load_default_knowledge_rows(),
    )
    if recipe is None or recipe.confidence < 0.75:
        return generic

    initial_params = dict(generic.get("initial_params") or {})
    for skill_name, params in recipe.initial_params.items():
        merged = dict(initial_params.get(skill_name) or {})
        merged.update(params)
        initial_params[skill_name] = merged

    return {
        **generic,
        "bottleneck": recipe.bottleneck,
        "suggested_skill_order": recipe.skill_order,
        "initial_params": initial_params,
        "recipe_id": recipe.recipe_id,
        "recipe_confidence": recipe.confidence,
        "recipe_reason": recipe.reason,
        "recipe_core_skills": recipe.core_skills,
        "recipe_budget_hint_s": recipe.budget_hint_s,
        "recipe_equivalence_risk": recipe.equivalence_risk,
        "recipe_learned_fmax_mhz": recipe.learned_fmax_mhz,
        "recipe_knowledge_evidence": recipe.knowledge_evidence,
        "reason": f"{recipe.recipe_id}: {recipe.reason}",
    }


def _should_stop_design_specific_recipe(
    memory,
    replay_known_benchmark: bool = False,
) -> bool:
    """No-op compatibility hook; benchmark-name replay recipes are disabled."""
    return False


def _seed_design_specific_candidates(
    memory,
    input_dcp: Path,
    replay_known_benchmark: bool = False,
) -> None:
    """No-op compatibility hook; profile rules seed candidates instead."""
    return None


def _seed_initial_param_target_candidates(memory) -> None:
    initial_params = memory.baseline.get("initial_params") or {}
    if not isinstance(initial_params, dict):
        return
    for skill_name, params in initial_params.items():
        if not isinstance(params, dict):
            continue
        raw_targets = params.get("target_candidates")
        if isinstance(raw_targets, str):
            targets = [raw_targets]
        elif isinstance(raw_targets, (list, tuple)):
            targets = [str(target) for target in raw_targets if str(target)]
        else:
            continue
        if not targets:
            continue
        if bool(params.get("replace_target_candidates", False)):
            memory.remaining_candidates[skill_name] = targets
        else:
            memory.remaining_candidates[skill_name] = _prepend_unique(
                memory.remaining_candidates.get(skill_name, []),
                targets,
            )


def _available_target(
    memory,
    skill: str,
    preferred: Optional[str] = None,
    params: Optional[dict] = None,
) -> Optional[str]:
    candidates = memory.remaining_candidates.get(skill, [])
    if preferred is not None:
        candidates = [preferred] if preferred in candidates else []
    for target in candidates:
        if _route_target_not_ready(memory, skill, target):
            continue
        candidate_params = params
        if candidate_params is None:
            candidate_params = _merge_skill_params_for_target(memory, skill, target, {})
        if _is_blacklisted(memory, skill, target, candidate_params):
            continue
        if _was_accepted(memory, skill, target, candidate_params):
            continue
        return target
    return None


def _checkpoint_exists(target: str) -> bool:
    path = Path(target).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.exists()


def _available_checkpoint(memory, skill: str = "checkpoint_import") -> Optional[str]:
    for target in memory.remaining_candidates.get(skill, []):
        if _is_blacklisted(memory, skill, target):
            continue
        if _was_accepted(memory, skill, target):
            continue
        if _checkpoint_exists(target):
            return target
    return None


def _pick_design_specific_recipe(
    memory,
    replay_known_benchmark: bool = False,
) -> Optional[tuple[str, str, dict]]:
    """No-op compatibility hook; benchmark-name replay recipes are disabled."""
    return None


def _fallback_params_for_skill(memory, skill_name: str) -> dict:
    initial_params = memory.baseline.get("initial_params") or {}
    params = dict(FALLBACK_PARAMS_BY_SKILL.get(skill_name, {}))
    if skill_name == "pblock" and _pblock_sweep_hard_macro_refused(memory):
        params.update(PBLOCK_HARD_MACRO_SEED_PARAMS)
    if isinstance(initial_params, dict) and isinstance(initial_params.get(skill_name), dict):
        params.update(initial_params[skill_name])
    return params


def _merge_skill_params(memory, skill_name: str, params: Optional[dict]) -> dict:
    merged = _fallback_params_for_skill(memory, skill_name)
    merged.update(dict(params or {}))
    return merged


def _merge_skill_params_for_target(
    memory,
    skill_name: str,
    target: str,
    params: Optional[dict] = None,
) -> dict:
    merged = _merge_skill_params(memory, skill_name, params)
    target_params = merged.pop("target_params", None)
    if isinstance(target_params, dict):
        specific = target_params.get(target)
        if isinstance(specific, dict):
            merged.update(specific)
    return merged


def _pick_fallback(memory) -> Optional[tuple[str, str, dict]]:
    """First (skill, target) with remaining candidates that isn't blacklisted."""
    recipe_core_skill = _available_recipe_core_skill(memory)
    if recipe_core_skill is not None:
        for cand in memory.remaining_candidates.get(recipe_core_skill, []):
            if _route_target_not_ready(memory, recipe_core_skill, cand):
                continue
            candidate_params = _merge_skill_params_for_target(
                memory, recipe_core_skill, cand, {}
            )
            candidate_params = _fit_skill_scope_to_budget(
                memory, recipe_core_skill, candidate_params
            )
            if _time_budget_blocks_skill(memory, recipe_core_skill, candidate_params):
                continue
            if (
                not _is_blacklisted(memory, recipe_core_skill, cand, candidate_params)
                and not _was_accepted(memory, recipe_core_skill, cand, candidate_params)
            ):
                return recipe_core_skill, cand, candidate_params

    suggested = [
        str(skill_name)
        for skill_name in memory.baseline.get("suggested_skill_order", [])
        if skill_name in memory.remaining_candidates
    ]
    skill_names = [
        skill_name
        for skill_name in [*suggested, *FALLBACK_SKILL_PRIORITY]
        if skill_name in memory.remaining_candidates
    ]
    skill_names = _prepend_unique([], skill_names)
    if _should_prioritize_post_route_cleanup(memory) and "post_route_cleanup" in skill_names:
        skill_names = ["post_route_cleanup"] + [
            skill_name for skill_name in skill_names if skill_name != "post_route_cleanup"
        ]
    if _should_prioritize_dynamic_pblock(memory) and "pblock" in skill_names:
        skill_names = ["pblock"] + [
            skill_name for skill_name in skill_names if skill_name != "pblock"
        ]
    if _should_prioritize_pblock_sweep(memory) and "pblock_sweep" in skill_names:
        skill_names = ["pblock_sweep"] + [
            skill_name for skill_name in skill_names if skill_name != "pblock_sweep"
        ]
    if (
        _should_prioritize_critical_net_reroute(memory)
        and "critical_net_reroute" in skill_names
    ):
        skill_names = ["critical_net_reroute"] + [
            skill_name for skill_name in skill_names
            if skill_name != "critical_net_reroute"
        ]
    if _should_prioritize_route_phys_opt(memory) and "phys_opt" in skill_names:
        skill_names = ["phys_opt"] + [
            skill_name for skill_name in skill_names if skill_name != "phys_opt"
        ]
    if _should_prioritize_lut_pin_swap(memory) and "lut_pin_swap" in skill_names:
        skill_names = ["lut_pin_swap"] + [
            skill_name for skill_name in skill_names if skill_name != "lut_pin_swap"
        ]
    skill_names.extend(
        skill_name
        for skill_name in memory.remaining_candidates
        if skill_name not in skill_names
    )
    for skill_name in skill_names:
        if _defer_structural_auto_skill(memory, skill_name):
            continue
        candidates = memory.remaining_candidates.get(skill_name, [])
        for cand in candidates:
            if _route_target_not_ready(memory, skill_name, cand):
                continue
            candidate_params = _merge_skill_params_for_target(memory, skill_name, cand, {})
            if _time_budget_blocks_skill(memory, skill_name, candidate_params):
                continue
            if (
                not _is_blacklisted(memory, skill_name, cand, candidate_params)
                and not _was_accepted(memory, skill_name, cand, candidate_params)
            ):
                return skill_name, cand, candidate_params
    return None


async def _refresh_force_replicate_candidates(mcp: MCPClient, memory) -> list[str]:
    """Refresh force-replicate candidates from the currently open routed design."""
    timing_report = await mcp.call_vivado(
        "run_tcl",
        {
            "command": (
                "report_timing -return_string -max_paths 20 -nworst 1 "
                "-path_type full_clock_expanded"
            ),
            "timeout": 300,
        },
        timeout=360.0,
    )
    source_side_candidates = _force_replicate_candidates_from_timing_report(
        timing_report
    )
    report = await mcp.call_vivado(
        "get_critical_high_fanout_nets",
        {"num_paths": 50, "min_fanout": 20, "exclude_clocks": True},
        timeout=600.0,
    )
    fanout_nets = _parse_high_fanout_nets(report)
    candidates = [
        *source_side_candidates,
        *_force_replicate_candidates_from_fanout(fanout_nets),
    ]
    return _add_force_replicate_candidates(memory, candidates)


def _sync_budget_from_llm(memory, llm_client, budget_usd: float) -> None:
    """Update memory.budget.tokens_spent from the LLM client's real usage."""
    memory.budget["tokens_spent"] = llm_client.total_cost
    memory.budget["tokens_remaining"] = max(0.0, budget_usd - llm_client.total_cost)


# ------------------------------------------------------------------
# Initial analysis (Python-only, no LLM)
# ------------------------------------------------------------------

async def initial_analysis(mcp: MCPClient, input_dcp: Path) -> dict:
    """
    Open DCP, gather baseline timing, detect high-fanout nets.
    Returns compact analysis dict for LLM analyze_timing call.
    """
    logger.info("[agent] Opening DCP in Vivado for initial analysis...")
    await mcp.call_vivado(
        "open_checkpoint",
        {"dcp_path": str(input_dcp.resolve())},
        timeout=600.0,
    )

    # Clock period
    clock_period = await mcp.get_clock_period()

    # Baseline timing
    ts_report = await mcp.call_vivado("report_timing_summary", {}, timeout=300.0)
    ts = parse_timing_summary_static(ts_report)
    baseline_wns = await mcp.get_wns() or ts.get("wns") or 0.0

    # min_fanout=20 keeps mid-fanout intermediate LUT outputs visible for
    # surgical per-net reroute; higher fanout nets feed broader replication.
    fanout_report = await mcp.call_vivado(
        "get_critical_high_fanout_nets",
        {"num_paths": 50, "min_fanout": 20, "exclude_clocks": True},
        timeout=600.0,
    )
    fanout_nets = _parse_high_fanout_nets(fanout_report)

    timing_profile = {}
    try:
        profile_raw = await mcp.call_vivado(
            "report_timing_path_profile",
            {"num_paths": 40},
            timeout=600.0,
        )
        parsed_profile = json.loads(profile_raw)
        if isinstance(parsed_profile, dict):
            timing_profile = parsed_profile
            logger.info(
                "[agent] Timing profile: paths=%s bottleneck=%s route=%s%% "
                "logic_levels=%s spread_paths=%s/%s",
                timing_profile.get("path_count"),
                timing_profile.get("dominant_bottleneck"),
                timing_profile.get("avg_route_pct"),
                (timing_profile.get("logic_levels") or {}).get("avg"),
                (timing_profile.get("placement_spread") or {}).get("spread_path_count"),
                (timing_profile.get("placement_spread") or {}).get(
                    "path_count_with_sites"
                ),
            )
    except Exception as e:
        logger.warning(f"[agent] Timing path profile unavailable: {e}")

    utilization = {}
    try:
        util_raw = await mcp.call_vivado(
            "run_tcl",
            {"command": "report_utilization -return_string"},
            timeout=300.0,
        )
        utilization = _parse_utilization_report(util_raw)
        if utilization:
            logger.info(
                "[agent] Utilization: LUTs=%s LUT%%=%s FFs=%s DSP=%s BRAM=%s URAM=%s",
                utilization.get("luts"),
                utilization.get("lut_util_pct"),
                utilization.get("ffs"),
                utilization.get("dsp"),
                utilization.get("bram"),
                utilization.get("uram"),
            )
    except Exception as e:
        logger.warning(f"[agent] Utilization report unavailable: {e}")

    # Build timing summary (compact, ≤400 chars)
    timing_summary = (
        f"WNS={baseline_wns:.3f}ns TNS={ts.get('tns', '?')} "
        f"failing_ep={ts.get('failing_endpoints', '?')} "
        f"clock={clock_period}ns"
    )
    if len(timing_summary) > 400:
        timing_summary = timing_summary[:400]

    return {
        "baseline_wns": baseline_wns,
        "clock_period": clock_period,
        "fanout_nets": fanout_nets,
        "timing_summary": timing_summary,
        "fanout_count": len(fanout_nets),
        "timing_profile": timing_profile,
        "utilization": utilization,
    }


def _parse_high_fanout_nets(report: str) -> list[tuple[str, int, int]]:
    """Parse critical high-fanout net report → [(name, fanout, paths), ...]"""
    nets = []
    in_section = False
    for line in report.split("\n"):
        if "Paths" in line and "Fanout" in line and "Parent Net Name" in line:
            in_section = True
            continue
        if not in_section:
            continue
        if line.startswith("---") or not line.strip():
            continue
        if line.startswith("==="):
            break
        parts = line.split()
        if len(parts) >= 3:
            try:
                path_count = int(parts[0])
                fanout = int(parts[1])
                net_name = parts[2]
                if not net_name.startswith(("get_", "ERROR", "WARNING")):
                    nets.append((net_name, fanout, path_count))
            except ValueError:
                continue
    return nets


def _parse_utilization_report(report: str) -> dict:
    """Parse key resource counts and LUT utilization from report_utilization."""
    result = {}
    for line in report.splitlines():
        if "|" not in line:
            continue
        fields = [field.strip() for field in line.split("|") if field.strip()]
        if len(fields) < 2:
            continue
        name = fields[0].lower()
        used_text = fields[1].replace(",", "")
        try:
            used = int(float(used_text))
        except ValueError:
            continue
        pct = None
        if len(fields) >= 5:
            try:
                pct = float(fields[-1].replace("%", "").replace(",", ""))
            except ValueError:
                pct = None

        if name == "clb luts":
            result["luts"] = used
            if pct is not None:
                result["lut_util_pct"] = pct
        elif name == "clb registers":
            result["ffs"] = used
        elif name == "dsps":
            result["dsp"] = used
        elif name == "block ram tile":
            result["bram"] = used
        elif name == "uram":
            result["uram"] = used
    return result


# ------------------------------------------------------------------
# Main optimization loop
# ------------------------------------------------------------------

async def run(
    input_dcp: Path,
    output_dcp: Path,
    model: str = DEFAULT_MODEL,
    time_limit: float = 3600.0,
    debug: bool = False,
    run_dir: Optional[Path] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    force_llm_analysis: bool = False,
    replay_known_benchmark: bool = False,
    use_llm: bool = True,
) -> int:
    """
    Main entry point.  Returns 0 on success, non-zero on failure.
    """
    if run_dir is None:
        ts = time.strftime("%Y%m%d_%H%M%S")
        run_dir = Path.cwd() / f"fdagents_run-{ts}"
    else:
        run_dir = run_dir.expanduser().resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"Run directory: {run_dir}")

    mcp = MCPClient(run_dir, debug=debug)
    if use_llm:
        llm = LLMClient(
            model=model,
            api_key=api_key,
            base_url=base_url,
            skill_md_path=Path(__file__).parent / "SKILL.md",
        )
    else:
        llm = RuleOnlyLLMStats()
    memory_path = run_dir / "memory.json"
    current_best_dcp = run_dir / "current_best.dcp"

    deadline = time.time() + time_limit

    try:
        await mcp.start_servers()

        # ---------------------------------------------------------------
        # Step 1: Initial analysis (Python, no LLM)
        # ---------------------------------------------------------------
        logger.info("[agent] Initial analysis...")
        analysis = await initial_analysis(mcp, input_dcp)

        baseline_wns = analysis["baseline_wns"]
        clock_period = analysis["clock_period"]
        fanout_nets = analysis["fanout_nets"]

        # Copy input DCP as current_best baseline
        shutil.copy(input_dcp, current_best_dcp)

        # ---------------------------------------------------------------
        # Step 2: Analyze timing.
        # ---------------------------------------------------------------
        recipe_analysis = None
        if not force_llm_analysis:
            recipe_analysis = _deterministic_analysis_from_initial(
                analysis,
                design_id=input_dcp.stem,
            )
        if (
            recipe_analysis
            and recipe_analysis.get("recipe_confidence", 0.0) >= 0.75
        ):
            llm_analysis = recipe_analysis
            logger.info(
                "[agent] RecipePlanner selected %s confidence=%.2f; skipping LLM analysis",
                recipe_analysis.get("recipe_id"),
                float(recipe_analysis.get("recipe_confidence") or 0.0),
            )
        elif use_llm:
            logger.info("[agent] LLM: analyze_timing...")
            mem_temp = Memory.init(
                design_id=input_dcp.stem,
                baseline_wns=baseline_wns,
                clock_period=clock_period,
                bottleneck="unknown",
                input_dcp=str(current_best_dcp),
                time_limit_s=time_limit,
                token_budget_usd=TOKEN_BUDGET_USD,
            )

            llm_analysis = llm.call("analyze_timing", mem_temp, {
                "timing_summary": analysis["timing_summary"],
                "fanout_count": analysis["fanout_count"],
                "timing_profile": analysis.get("timing_profile", {}),
            })
            _sync_budget_from_llm(mem_temp, llm, TOKEN_BUDGET_USD)
        else:
            logger.info("[agent] Rule-only analyze_timing...")
            llm_analysis = _deterministic_analysis_from_initial(
                analysis,
                design_id=input_dcp.stem,
            )

        bottleneck = llm_analysis.get("bottleneck", "mixed")
        suggested_order = llm_analysis.get("suggested_skill_order", ["phys_opt", "cell_replace", "fanout_opt"])
        initial_params = llm_analysis.get("initial_params", {})
        logger.info(f"[agent] bottleneck={bottleneck} order={suggested_order}")

        # ---------------------------------------------------------------
        # Step 3: Initialize memory with LLM guidance
        # ---------------------------------------------------------------
        # Build phys_opt candidates from directive_order
        phys_order = initial_params.get("phys_opt", {}).get(
            "directive_order", ["AggressiveFanoutOpt", "AddRetime", "AggressiveExplore", "Default"]
        )
        # Partition the critical-fanout list by fanout value:
        #   - fanout_opt (broadcast-class): ≥100, RW-side driver replication
        #   - per_net_unroute (surgical): 20-99, intermediate LUT outputs
        fanout_candidates = _fanout_opt_candidates_from_fanout(fanout_nets)
        force_replicate_candidates = _initial_force_replicate_candidates(analysis)
        per_net_unroute_candidates = [
            n for n, fo, _ in fanout_nets if 20 <= fo < 100
        ][:8]

        mem = Memory.init(
            design_id=input_dcp.stem,
            baseline_wns=baseline_wns,
            clock_period=clock_period,
            bottleneck=bottleneck,
            input_dcp=str(current_best_dcp),
            time_limit_s=time_limit,
            token_budget_usd=TOKEN_BUDGET_USD,
        )
        mem.remaining_candidates["phys_opt"] = phys_order
        mem.remaining_candidates["fanout_opt"] = fanout_candidates
        mem.remaining_candidates["cell_replace"] = ["auto"]
        mem.remaining_candidates["lut_merge"] = ["auto"]
        mem.remaining_candidates["lut_pin_swap"] = ["auto"]
        mem.remaining_candidates["post_route_cleanup"] = ["auto"]
        mem.remaining_candidates["critical_net_reroute"] = ["auto"]
        mem.remaining_candidates["path_local_lut_reflow"] = ["auto"]
        mem.remaining_candidates["fresh_place_route"] = ["auto"]
        mem.remaining_candidates["endpoint_bel_move"] = ["auto"]
        mem.remaining_candidates["clock_tighten"] = ["auto"]
        mem.remaining_candidates["per_net_unroute"] = per_net_unroute_candidates
        mem.remaining_candidates["force_replicate"] = force_replicate_candidates
        mem.remaining_candidates["pblock_sweep"] = ["auto"]
        mem.remaining_candidates["pblock"] = [""]  # empty = dynamic
        mem.remaining_candidates["critical_cluster_anchor"] = ["auto"]
        if analysis.get("timing_profile"):
            mem.baseline["timing_profile"] = analysis["timing_profile"]
        if analysis.get("utilization"):
            mem.baseline["utilization"] = analysis["utilization"]
        mem.baseline["suggested_skill_order"] = suggested_order
        mem.baseline["initial_params"] = initial_params
        for key in (
            "recipe_id",
            "recipe_confidence",
            "recipe_reason",
            "recipe_core_skills",
            "recipe_budget_hint_s",
            "recipe_equivalence_risk",
            "recipe_learned_fmax_mhz",
            "recipe_knowledge_evidence",
        ):
            if key in llm_analysis:
                mem.baseline[key] = llm_analysis[key]
        _seed_initial_param_target_candidates(mem)
        _seed_design_specific_candidates(
            mem,
            input_dcp,
            replay_known_benchmark=replay_known_benchmark,
        )
        _sync_budget_from_llm(mem, llm, TOKEN_BUDGET_USD)
        mem.save(memory_path)

        logger.info(
            f"[agent] Baseline WNS={baseline_wns:.3f}ns clock={clock_period}ns "
            f"fanout_nets={len(fanout_candidates)} "
            f"force_replicate_candidates={len(force_replicate_candidates)}"
        )

        # ---------------------------------------------------------------
        # Step 4: Main optimization loop
        # ---------------------------------------------------------------
        round_num = 0
        while mem.budget_ok(deadline) and round_num < MAX_ROUNDS:
            round_num += 1
            logger.info(f"\n[agent] === Round {round_num} ===")

            # Quick rule filter
            quick = rules.quick_filter(mem)
            if quick:
                if quick.kind == "save_and_exit":
                    logger.info("[agent] Rule: save_and_exit")
                    break
                elif quick.kind == "rollback":
                    logger.info(f"[agent] Rule: rollback — {quick.reason}")
                    # Blacklist the offending pair so the next iteration's
                    # validation step short-circuits to a fallback skill
                    # instead of letting the LLM re-pick the same failing
                    # target. decision.quick_filter itself now skips the
                    # rollback check when the pair is already blacklisted,
                    # so this guarantees the loop can make progress.
                    mem.blacklist_add(
                        quick.skill, quick.target,
                        f"rollback: {quick.reason}",
                    )
                    mem.save(memory_path)
                    continue
                elif quick.kind == "skip_skill":
                    # Cool down the stuck skill for the current best checkpoint
                    # without destroying its candidate list. If a later action
                    # improves current_best, the same candidates can be
                    # reconsidered in the new timing context.
                    skill_to_skip = quick.skill
                    remaining = list(mem.remaining_candidates.get(skill_to_skip, []))
                    logger.warning(
                        f"[agent] Rule: skip_skill — {quick.reason}; "
                        f"cooling down {skill_to_skip} with {len(remaining)} remaining targets"
                    )
                    mem.blacklist_add(
                        skill_to_skip,
                        "*",
                        f"skip_skill: {quick.reason}",
                        severity="cooldown",
                        best_round=mem.current_best.get("round"),
                    )
                    mem.save(memory_path)
                    continue

            if _should_stop_design_specific_recipe(
                mem,
                replay_known_benchmark=replay_known_benchmark,
            ):
                logger.info(
                    "[agent] Known-benchmark replay threshold reached; save_and_exit"
                )
                break

            # Determine action: deterministic known recipe, deep mode, or normal LLM
            recipe_action = _pick_design_specific_recipe(
                mem,
                replay_known_benchmark=replay_known_benchmark,
            )
            if recipe_action:
                skill_name, target, params = recipe_action
                action_dict = {}
                deep_mode_active = False
                logger.info(
                    f"[agent] Design-specific recipe action: "
                    f"{skill_name}/{target!r}"
                )
            else:
                deep_mode_active = use_llm and rules.should_deep_analyze(mem)

            if not recipe_action and deep_mode_active:
                logger.info("[agent] Deep analysis mode triggered")
                raw_timing = await mcp.call_vivado("report_timing_summary", {}, timeout=300.0)
                # In deep mode we allow the LLM to pick the `custom` skill to
                # invoke arbitrary MCP tools. Budget is tracked separately in
                # memory.custom_calls_used.
                custom_left = mem.custom_budget_left()
                action_dict = _call_llm_with_retry(
                    lambda: llm.call_deep(
                        "deep_analysis",
                        mem,
                        raw_timing,
                        extra={"custom_calls_remaining": custom_left},
                    ),
                    description="deep_analysis",
                )
                # NOTE: deep_calls_used is NOT incremented here. It is
                # incremented only when a non-custom (real optimization)
                # action gets executed below. Custom probes are exploratory
                # queries and should not burn the deep-optimization budget
                # — otherwise the LLM diagnoses the design with probes,
                # learns exactly what to do, and then loses its deep slot
                # before it can apply the fix. Observed on
                # fdagents_run-20260414_205737.
                # Extract recommended action from deep analysis
                rec = action_dict.get("recommended_action", {})
                skill_name = rec.get("skill", "phys_opt")
                target = rec.get("target", "Default")
                params = rec.get("params", {})
            elif not recipe_action and use_llm:
                # Normal LLM decision
                action_dict = _call_llm_with_retry(
                    lambda: llm.call("decide_next_action", mem),
                    description="decide_next_action",
                )
                skill_name = action_dict.get("skill", "phys_opt")
                target = action_dict.get("target", "Default")
                params = action_dict.get("params", {})
            elif not recipe_action:
                fb = _pick_fallback(mem)
                if fb is None:
                    logger.info("[agent] Rule-only mode has no fallback candidates; ending loop")
                    break
                skill_name, target, params = fb
                action_dict = {}
                logger.info(
                    "[agent] Rule-only fallback action: %s/%r",
                    skill_name,
                    target,
                )

            # Sync budget from real usage
            _sync_budget_from_llm(mem, llm, TOKEN_BUDGET_USD)

            # ----- Validate LLM choice -----
            # DEEP_ONLY_SKILLS (custom) are gated: allowed only in deep mode
            # AND only while the custom budget has calls left. They don't
            # participate in the blacklist / remaining_candidates logic
            # because there's no fixed target list.
            override_reason = None
            is_deep_only = skill_name in DEEP_ONLY_SKILLS
            if not is_deep_only:
                params = _merge_skill_params(mem, skill_name, params)
                params = _fit_skill_scope_to_budget(mem, skill_name, params)

            override_reason = _action_override_reason(
                mem,
                action_dict,
                skill_name,
                target,
                deep_mode_active=deep_mode_active,
                params=params,
            )

            if override_reason:
                fb = _pick_fallback(mem)
                if fb is None:
                    logger.info(
                        f"[agent] No fallback candidates ({override_reason}); ending loop"
                    )
                    break
                old_choice = f"{skill_name}/{target!r}"
                skill_name, target, params = fb
                is_deep_only = False
                params = _merge_skill_params(mem, skill_name, params)
                params = _fit_skill_scope_to_budget(mem, skill_name, params)
                logger.warning(
                    f"[agent] Override LLM ({override_reason}); was {old_choice} → {skill_name}/{target!r}"
                )

            logger.info(f"[agent] Action: skill={skill_name} target={target!r} params={params}")

            skill = SKILLS[skill_name]

            # Add input_dcp to params if needed
            if "input_dcp" not in params:
                params["input_dcp"] = str(current_best_dcp)

            before_wns = mem.current_best["wns"]

            # Execute skill
            result = await skill.execute(
                mcp=mcp,
                target=target,
                params=params,
                before_wns=before_wns,
                clock_period=clock_period,
            )

            logger.info(f"[agent] Result: {result.summary}")

            # Custom skill is exploratory — its result is a probe, not an
            # optimization step. Record the response into memory so the
            # next deep-mode LLM call can reason about it, skip the normal
            # accept/blacklist logic, and don't touch current_best.
            if is_deep_only and skill_name == "custom":
                server, _, tool_name = str(target).partition(":")
                mem.record_custom_response(
                    server=server,
                    tool=tool_name,
                    arguments=params.get("arguments", {}),
                    response=result.details or result.summary,
                    description=str(params.get("description", "")),
                )
                mem.record(
                    skill=skill_name,
                    target=target,
                    before_wns=before_wns,
                    after_wns=result.after_wns,
                    accepted=False,   # never "accepted" in the optimization sense
                    params=params,
                    summary=result.summary,
                )
                logger.info(
                    f"[agent] Custom probe recorded ({mem.custom_calls_used}/"
                    f"{mem.CUSTOM_CALL_BUDGET}); no blacklist applied"
                )
                mem.save(memory_path)
                continue

            accepted = _accepts_result(result, params)
            mem.record(
                skill=skill_name,
                target=target,
                before_wns=before_wns,
                after_wns=result.after_wns,
                accepted=accepted,
                params=params,
                summary=result.summary,
            )

            # A real (non-custom) optimization action executed in deep mode
            # consumes one deep slot. Custom probes above already `continue`d
            # past this point so they don't burn the budget.
            if deep_mode_active:
                mem.deep_calls_used += 1

            if accepted:
                # Update best DCP
                shutil.copy(result.output_dcp, current_best_dcp)
                from .memory import _calculate_fmax
                fmax = _calculate_fmax(result.after_wns, clock_period)
                mem.update_best(result.after_wns, fmax, str(current_best_dcp))
                # Copy to final output immediately (so partial results are saved)
                shutil.copy(current_best_dcp, output_dcp)
                if _should_refresh_force_replicate_candidates(mem, skill_name):
                    try:
                        added_force = await _refresh_force_replicate_candidates(mcp, mem)
                        if added_force:
                            logger.info(
                                "[agent] Added force_replicate candidates from current timing: %s",
                                added_force,
                            )
                    except Exception as e:
                        logger.warning(
                            "[agent] force_replicate candidate refresh failed: %s",
                            e,
                        )
                logger.info(
                    f"[agent] Accepted: WNS {before_wns:.3f}→{result.after_wns:.3f} (+{result.delta_wns:.3f}ns)"
                )
            else:
                blacklist_severity = _blacklist_severity_for_result(result)
                mem.blacklist_add(
                    skill_name,
                    target,
                    "no improvement or illegal route",
                    severity=blacklist_severity,
                    params=params,
                    best_round=mem.current_best.get("round"),
                )
                logger.info(
                    f"[agent] Rejected: delta={result.delta_wns:.3f} "
                    f"legal={result.is_legal} blacklist={blacklist_severity}"
                )

                # Roll back Vivado's in-memory design to current_best.
                # Every mutating skill (phys_opt / fanout_opt / cell_replace
                # / lut_merge / pblock) leaves Vivado with its trial result
                # loaded, whether the trial succeeded or not. Without this
                # reopen the next round (and any deep-mode report_timing_
                # summary probe) runs on a degraded state. Observed on
                # fdagents_run-20260414_231918 round 5→6: lut_merge went
                # -1.094 → -1.236, rejected, but round 6 phys_opt then ran
                # on the -1.236 state and could not recover.
                try:
                    await mcp.call_vivado(
                        "open_checkpoint",
                        {"dcp_path": str(current_best_dcp.resolve())},
                        timeout=600.0,
                    )
                    logger.info(
                        f"[agent] Rolled back Vivado state to current_best "
                        f"(wns={mem.current_best['wns']:.3f})"
                    )
                except Exception as e:
                    logger.error(f"[agent] Failed to roll back Vivado state: {e}")

            mem.save(memory_path)

        if round_num >= MAX_ROUNDS:
            logger.warning(
                f"[agent] MAX_ROUNDS={MAX_ROUNDS} reached — check rule layer "
                "for runaway bug (normal runs use ~15-30 rounds)"
            )

        # ---------------------------------------------------------------
        # Step 5: Final output
        # ---------------------------------------------------------------
        if current_best_dcp.exists():
            shutil.copy(current_best_dcp, output_dcp)

        elapsed = time.time() - (deadline - time_limit)
        final_wns = mem.current_best["wns"]
        final_fmax = mem.current_best.get("fmax")

        print("\n" + "=" * 60)
        print("FDAgents Optimization Summary")
        print("=" * 60)
        print(f"  Rounds:           {round_num}")
        print(f"  LLM calls:        {llm.call_count}")
        print(f"  LLM model:        {llm.model}")
        print(f"  LLM tokens:       in {llm.total_input_tokens:,} / out {llm.total_output_tokens:,}")
        print(f"  LLM cached input:  {getattr(llm, 'total_cached_input_tokens', 0):,}")
        print(f"  LLM uncached input:{getattr(llm, 'total_uncached_input_tokens', llm.total_input_tokens):,}")
        print(f"  LLM cost:         ${llm.total_cost:.4f}")
        print(f"  Baseline WNS:     {baseline_wns:.3f} ns")
        print(f"  Final WNS:        {final_wns:.3f} ns")
        print(f"  WNS improvement:  {final_wns - baseline_wns:+.3f} ns")
        if final_fmax:
            print(f"  Final Fmax:       {final_fmax:.2f} MHz")
        print(f"  Elapsed:          {elapsed:.0f}s")
        print(f"  Output DCP:       {output_dcp}")
        print(f"  Run directory:    {run_dir}")
        print("=" * 60)

        return 0

    except KeyboardInterrupt:
        logger.info("[agent] Interrupted")
        if current_best_dcp.exists():
            shutil.copy(current_best_dcp, output_dcp)
        return 130
    except Exception:
        logger.exception("[agent] Fatal error")
        if current_best_dcp.exists():
            shutil.copy(current_best_dcp, output_dcp)
        return 1
    finally:
        await mcp.cleanup()


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------

def _default_output_dcp_path(input_dcp: Path, timestamp: Optional[str] = None) -> Path:
    """Return the contest-visible default output path for an input checkpoint."""
    if timestamp is None:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
    return input_dcp.parent / f"{input_dcp.stem}_optimized-{timestamp}.dcp"


def main():
    parser = argparse.ArgumentParser(
        description="FDAgents — Hybrid Skill+LLM FPGA Optimizer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m FDAgents.agent input.dcp
  python -m FDAgents.agent input.dcp --output out.dcp
  python -m FDAgents.agent fpl26_contest_benchmarks/example/input.dcp
        """,
    )
    parser.add_argument("input_dcp", type=Path, help="Input design checkpoint (.dcp)")
    parser.add_argument(
        "--output", "-o",
        type=Path,
        dest="output_dcp",
        default=None,
        help="Output optimized checkpoint (.dcp). "
             "Default: <input_name>_optimized-<timestamp>.dcp next to input",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="LLM model name")
    parser.add_argument("--time-limit", type=float, default=3600.0, help="Time limit in seconds")
    parser.add_argument("--debug", action="store_true", help="Debug mode (MCP logs to console)")
    parser.add_argument("--run-dir", type=Path, default=None, help="Explicit run directory")
    parser.add_argument(
        "--force-llm-analysis",
        action="store_true",
        help="Use the LLM startup analyze_timing call even during explicit known-benchmark replay.",
    )
    parser.add_argument(
        "--replay-known-benchmark",
        action="store_true",
        help=(
            "Enable local benchmark-name replay recipes. Disabled by default "
            "so contest runs remain anonymous/profile-driven."
        ),
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Run with deterministic rule/fallback decisions only; no LLM API calls.",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OPENAI_API_KEY"),
        help="LLM API key (default: OPENAI_API_KEY env var)",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("OPENAI_BASE_URL"),
        help="LLM base URL (default: OPENAI_BASE_URL env var)",
    )
    args = parser.parse_args()

    # Validate input
    if not args.input_dcp.exists():
        print(f"Error: Input file not found: {args.input_dcp}", file=sys.stderr)
        sys.exit(1)

    # Generate default output path matching the contest harness contract.
    if args.output_dcp is None:
        args.output_dcp = _default_output_dcp_path(args.input_dcp)

    args.output_dcp.parent.mkdir(parents=True, exist_ok=True)

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    rc = asyncio.run(run(
        input_dcp=args.input_dcp,
        output_dcp=args.output_dcp,
        model=args.model,
        time_limit=args.time_limit,
        debug=args.debug,
        run_dir=args.run_dir,
        api_key=args.api_key,
        base_url=args.base_url,
        force_llm_analysis=args.force_llm_analysis,
        replay_known_benchmark=args.replay_known_benchmark,
        use_llm=not args.no_llm,
    ))
    sys.exit(rc)


if __name__ == "__main__":
    main()
