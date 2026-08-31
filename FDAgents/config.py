"""Configuration loader for FDAgents.

All tunables live in YAML (see ``FDAgents/config.yaml``). This module loads
the packaged defaults, deep-merges an optional user file from
``FDAGENTS_CONFIG``, applies a small set of legacy environment overrides, and
validates the result against a lightweight schema so typos fail fast instead
of silently falling back to defaults.
"""

from __future__ import annotations

import copy
import logging
import math
import os
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).parent
DEFAULT_CONFIG_PATH = PACKAGE_DIR / "config.yaml"

# ---------------------------------------------------------------------------
# Schema: dotted path -> expected type(s). Only scalar leaves are validated;
# dict/list containers are checked structurally. Unknown keys raise.
# ---------------------------------------------------------------------------
_SCHEMA_TYPES: dict[str, tuple[type, ...]] = {
    "llm.model": (str,),
    "llm.token_budget_usd": (int, float),
    "llm.request_timeout_s": (int, float),
    "llm.retry.attempts": (int,),
    "llm.retry.initial_delay_s": (int, float),
    "llm.retry.max_delay_s": (int, float),
    "platform.id": (str,),
    "replay.policy_id": (str,),
    "replay.deadlines_s.characterize": (int, float),
    "replay.deadlines_s.basin": (int, float),
    "replay.deadlines_s.semantics": (int, float),
    "replay.deadlines_s.closeout": (int, float),
    "replay.deadlines_s.submission": (int, float),
    "replay.thresholds.minimum_feature_count": (int,),
    "replay.thresholds.route_dominated_min_ratio": (int, float),
    "replay.thresholds.high_util_min_pct": (int, float),
    "replay.thresholds.deep_logic_min_levels": (int,),
    "replay.thresholds.high_fanout_min_count": (int,),
    "replay.thresholds.clock_fallback_max_closure_ratio": (int, float),
    "replay.thresholds.semantic_preflight_max_age_s": (int, float),
    "replay.thresholds.low_gain_max_delta_mhz": (int, float),
    "replay.thresholds.saturation_minimum_consecutive": (int,),
    "run.time_limit_s": (int, float),
    "run.time_safety_s": (int, float),
    "run.max_rounds": (int,),
    "run.accept_min_delta_ns": (int, float),
    "run.candidate_beam_width": (int,),
    "run.enabling_candidate_pool_width": (int,),
    "run.enabling_composition_max_depth": (int,),
    "run.enabling_residual_regression_tolerance_ns": (int, float),
    "run.enabling_local_improvement_epsilon_ns": (int, float),
    "run.promotion_min_delta_mhz": (int, float),
    "run.max_skill_candidates_per_action": (int,),
    "run.candidate_beam_diversity_enabled": (bool,),
    "run.candidate_beam_diversity_min_common_dimensions": (int,),
    "run.candidate_commit_reserve_s": (int, float),
    "run.central_measurement_estimate_s": (int, float),
    "run.max_consecutive_vetoes": (int,),
    "concurrent_proof.enabled": (bool,),
    "concurrent_proof.cheap_replay_vectors": (int,),
    "concurrent_proof.vm_slot": (str,),
    "concurrent_proof.timeouts_s.qf_bv": (int, float),
    "concurrent_proof.timeouts_s.robdd": (int, float),
    "concurrent_proof.timeouts_s.sat": (int, float),
    "action_menu.top_k": (int,),
    "action_menu.max_build_retries": (int,),
    "saturation.level1.enabled": (bool,),
    "saturation.level1.low_gain_max_delta_mhz": (int, float),
    "saturation.level1.minimum_consecutive": (int,),
    "saturation.level1.strategy_family_by_skill": (dict,),
    "saturation.level1.max_scopes_in_llm_state": (int,),
    "artifacts.level1_gc_enabled": (bool,),
    "equivalence.vectors": (int,),
    "equivalence.precheck_vectors": (int,),
    "equivalence.time_reserve_s": (int, float),
    "score_scheduling.enabled": (bool,),
    "score_scheduling.calibration_history_limit": (int,),
    "score_scheduling.calibration_records_in_llm_state": (int,),
    "action_review.enabled": (bool,),
    "action_review.registry_path": (str,),
    "action_review.registry_max_records": (int,),
    "action_review.transaction_history_limit": (int,),
    "action_review.exact_history_limit": (int,),
    "action_review.family_history_limit": (int,),
    "bo.enabled": (bool,),
    "bo.model_root": (str,),
    "bo.runtime_python": (str,),
    "bo.timeout_s": (int, float),
    "bo.time.enabled": (bool,),
    "bo.time.model_root": (str,),
    "bo.time.reserve_s": (int, float),
    "submission.validator_timeout_s": (int, float),
    "submission.export_guard_s": (int, float),
    "submission.process_kill_grace_s": (int, float),
    "submission.max_artifact_bytes": (int,),
    "submission.max_evidence_bytes": (int,),
    "analysis.fanout_num_paths": (int,),
    "analysis.fanout_min_fanout": (int,),
    "analysis.profile_num_paths": (int,),
    "analysis.candidate_signature_num_paths": (int,),
    "analysis.candidate_signature_timeout_s": (int, float),
    "analysis.registered_product_sum_preflight_budget_s": (int, float),
    "analysis.registered_product_sum_family_deadline_s": (int, float),
    "skills.candidate_runtime_estimates_s": (dict,),
    "legality.contest_clock": (str,),
    "legality.require_fully_routed": (bool,),
    "legality.require_hold_clean": (bool,),
    "legality.require_pulse_width_clean": (bool,),
    "legality.custom_call_budget": (int,),
    "legality.allowed_custom_tools": (dict,),
    "knowledge.cases_path": (str,),
    "knowledge.skills_guide_path": (str,),
    "knowledge.max_cases_in_prompt": (int,),
    "knowledge.min_match_score": (int, float),
    "react.enabled": (bool,),
    "react.max_tool_turns": (int,),
    "react.stage_max_tool_turns": (dict,),
    "react.terminal_repair_turns": (int,),
    "react.max_seed_switches_per_decision": (int,),
    "react.knowledge_stage_budget_s": (int, float),
    "react.knowledge_stage_enabled": (bool,),
    "react.provider_failure_retries": (int,),
    "react.tool_choice": (str,),
    "react.grep.max_pattern_chars": (int,),
    "react.grep.max_files": (int,),
    "react.grep.max_matches_per_call": (int,),
    "react.grep.max_output_bytes_per_call": (int,),
    "react.grep.max_context_lines": (int,),
    "react.grep.max_line_chars": (int,),
    "react.grep.timeout_s": (int, float),
    "react.grep.max_calls_report_stage": (int,),
    "react.grep.max_calls_research_stage": (int,),
    "react.grep.max_total_output_bytes": (int,),
}

_KNOWN_SECTIONS = {
    "llm",
    "platform",
    "replay",
    "run",
    "concurrent_proof",
    "action_menu",
    "saturation",
    "artifacts",
    "equivalence",
    "score_scheduling",
    "action_review",
    "bo",
    "submission",
    "analysis",
    "legality",
    "skills",
    "knowledge",
    "react",
    "vivado_mcp",
    "rapidwright_mcp",
}

# Legacy env overrides kept for Makefile/docs compatibility.
_ENV_OVERRIDES: dict[str, tuple[str, ...]] = {
    "FDAGENTS_MODEL": ("llm", "model"),
    "FDAGENTS_TOKEN_BUDGET": ("llm", "token_budget_usd"),
    "FDAGENTS_MAX_ROUNDS": ("run", "max_rounds"),
    "FDAGENTS_ACCEPT_MIN_DELTA_NS": ("run", "accept_min_delta_ns"),
    "FDAGENTS_TIME_SAFETY_S": ("run", "time_safety_s"),
    "FDAGENTS_ACTION_EVIDENCE_REGISTRY": ("action_review", "registry_path"),
    "FDAGENTS_BO_ENABLE": ("bo", "enabled"),
    "FDAGENTS_BO_MODEL_ROOT": ("bo", "model_root"),
    "FDAGENTS_BO_PYTHON": ("bo", "runtime_python"),
    "FDAGENTS_BO_TIME_ENABLE": ("bo", "time", "enabled"),
    "FDAGENTS_BO_TIME_MODEL_ROOT": ("bo", "time", "model_root"),
    "FDAGENTS_BO_TIME_RESERVE_S": ("bo", "time", "reserve_s"),
}


class ConfigError(ValueError):
    """Raised when the configuration file is malformed."""


class Config:
    """Read-only dotted-path access over the merged config dict."""

    def __init__(self, data: dict):
        self._data = data

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return copy.deepcopy(node)

    def require(self, path: str) -> Any:
        sentinel = object()
        value = self.get(path, sentinel)
        if value is sentinel:
            raise ConfigError(f"missing required config key: {path}")
        return value

    def section(self, name: str) -> dict:
        value = self.get(name, {})
        return value if isinstance(value, dict) else {}

    def skill_defaults(self, skill_name: str) -> dict:
        defaults = self.get(f"skills.defaults.{skill_name}", {})
        return defaults if isinstance(defaults, dict) else {}

    def resolve_path(self, path_str: str) -> Path:
        p = Path(path_str)
        return p if p.is_absolute() else PACKAGE_DIR / p

    def as_dict(self) -> dict:
        return copy.deepcopy(self._data)


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _coerce(raw: str, types: tuple[type, ...]) -> Any:
    if bool in types:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if int in types and float not in types:
        return int(raw)
    if float in types or int in types:
        return float(raw)
    return raw


def _apply_env_overrides(data: dict) -> None:
    for env_name, key_path in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_name)
        if raw is None or raw == "":
            continue
        node = data
        for part in key_path[:-1]:
            node = node.setdefault(part, {})
        dotted = ".".join(key_path)
        types = _SCHEMA_TYPES.get(dotted, (str,))
        try:
            node[key_path[-1]] = _coerce(raw, types)
        except ValueError as e:
            raise ConfigError(f"bad env override {env_name}={raw!r}: {e}") from e


_REPLAY_NESTED_KEYS = {
    "policy_id": None,
    "deadlines_s": {
        "characterize": None,
        "basin": None,
        "semantics": None,
        "closeout": None,
        "submission": None,
    },
    "thresholds": {
        "minimum_feature_count": None,
        "route_dominated_min_ratio": None,
        "high_util_min_pct": None,
        "deep_logic_min_levels": None,
        "high_fanout_min_count": None,
        "clock_fallback_max_closure_ratio": None,
        "semantic_preflight_max_age_s": None,
        "low_gain_max_delta_mhz": None,
        "saturation_minimum_consecutive": None,
    },
}


def _validate_replay_structure(data: dict) -> None:
    """Replay is stricter than legacy sections: every nested key is closed."""

    replay = data.get("replay")
    if not isinstance(replay, dict):
        raise ConfigError("config key replay must be a mapping")

    def reject_policy_maps(node: dict, path: str) -> None:
        for key, value in node.items():
            normalized = str(key).lower().replace("-", "_")
            if isinstance(value, dict) and any(
                token in normalized for token in ("target", "benchmark", "action")
            ):
                raise ConfigError(
                    f"config key {path}.{key} cannot define a replay policy map"
                )
            if isinstance(value, dict):
                reject_policy_maps(value, f"{path}.{key}")

    def validate_node(node: object, schema: dict, path: str) -> None:
        if not isinstance(node, dict):
            raise ConfigError(f"config key {path} must be a mapping")
        unknown = set(node) - set(schema)
        if unknown:
            raise ConfigError(
                f"unknown replay config key(s) under {path}: {sorted(unknown)}"
            )
        for key, child_schema in schema.items():
            if key not in node:
                continue
            child = node[key]
            child_path = f"{path}.{key}"
            if child_schema is None:
                if isinstance(child, dict):
                    raise ConfigError(f"config key {child_path} must be a scalar")
            else:
                validate_node(child, child_schema, child_path)

    reject_policy_maps(replay, "replay")
    validate_node(replay, _REPLAY_NESTED_KEYS, "replay")


def _validate(data: dict) -> None:
    unknown = set(data) - _KNOWN_SECTIONS
    if unknown:
        raise ConfigError(
            f"unknown config section(s): {sorted(unknown)}; "
            f"known sections: {sorted(_KNOWN_SECTIONS)}"
        )
    _validate_replay_structure(data)
    concurrent = data.get("concurrent_proof")
    expected_concurrent = {
        "enabled", "cheap_replay_vectors", "vm_slot", "timeouts_s"
    }
    if not isinstance(concurrent, dict) or set(concurrent) != expected_concurrent:
        raise ConfigError(
            "concurrent_proof must contain enabled, cheap_replay_vectors, "
            "vm_slot, and timeouts_s"
        )
    timeouts = concurrent.get("timeouts_s")
    if not isinstance(timeouts, dict) or set(timeouts) != {"qf_bv", "robdd", "sat"}:
        raise ConfigError(
            "concurrent_proof.timeouts_s must contain exactly qf_bv, robdd, and sat"
        )
    action_menu = data.get("action_menu")
    if not isinstance(action_menu, dict) or set(action_menu) != {
        "top_k", "max_build_retries"
    }:
        raise ConfigError(
            "action_menu must contain exactly top_k and max_build_retries"
        )
    for dotted, types in _SCHEMA_TYPES.items():
        node: Any = data
        found = True
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                found = False
                break
            node = node[part]
        if not found:
            continue
        # bool is a subclass of int; disallow bool where int/float expected
        if isinstance(node, bool) and bool not in types:
            raise ConfigError(f"config key {dotted} must be {types}, got bool")
        if not isinstance(node, types):
            raise ConfigError(
                f"config key {dotted} must be {'/'.join(t.__name__ for t in types)}, "
                f"got {type(node).__name__} ({node!r})"
            )
    if concurrent["cheap_replay_vectors"] != 512:
        raise ConfigError("concurrent_proof.cheap_replay_vectors must be exactly 512")
    if not concurrent["vm_slot"].strip():
        raise ConfigError("concurrent_proof.vm_slot must be nonempty")
    for name, value in timeouts.items():
        if not math.isfinite(float(value)) or float(value) <= 0.0:
            raise ConfigError(
                f"concurrent_proof.timeouts_s.{name} must be finite and positive"
            )
    if float(timeouts["qf_bv"]) >= float(timeouts["robdd"]):
        raise ConfigError("concurrent proof QF_BV timeout must be tighter than ROBDD")
    menu_top_k = data.get("action_menu", {}).get("top_k")
    if menu_top_k is not None and not 1 <= menu_top_k <= 16:
        raise ConfigError("config key action_menu.top_k must be in [1, 16]")
    menu_retries = data.get("action_menu", {}).get("max_build_retries")
    if menu_retries is not None and not 0 <= menu_retries <= 4:
        raise ConfigError("config key action_menu.max_build_retries must be in [0, 4]")
    replay = data.get("replay", {})
    if replay.get("policy_id") != "generic_basin_then_semantics_v1":
        raise ConfigError(
            "config key replay.policy_id must be "
            "'generic_basin_then_semantics_v1'"
        )
    deadlines = replay.get("deadlines_s", {})
    deadline_names = (
        "characterize",
        "basin",
        "semantics",
        "closeout",
        "submission",
    )
    deadline_values = [deadlines.get(name) for name in deadline_names]
    if any(
        value is None
        or not math.isfinite(float(value))
        or float(value) <= 0.0
        for value in deadline_values
    ) or any(
        float(left) >= float(right)
        for left, right in zip(deadline_values, deadline_values[1:])
    ):
        raise ConfigError(
            "replay deadlines must be finite, positive, and strictly increasing"
        )
    replay_thresholds = replay.get("thresholds", {})
    for key in (
        "minimum_feature_count",
        "deep_logic_min_levels",
        "high_fanout_min_count",
        "saturation_minimum_consecutive",
    ):
        if replay_thresholds.get(key, 0) < 1:
            raise ConfigError(f"config key replay.thresholds.{key} must be at least 1")
    for key in (
        "route_dominated_min_ratio",
        "high_util_min_pct",
        "clock_fallback_max_closure_ratio",
        "semantic_preflight_max_age_s",
        "low_gain_max_delta_mhz",
    ):
        value = replay_thresholds.get(key)
        if value is None or not math.isfinite(float(value)):
            raise ConfigError(f"config key replay.thresholds.{key} must be finite")
    route_ratio = float(replay_thresholds["route_dominated_min_ratio"])
    if not 0.0 <= route_ratio <= 1.0:
        raise ConfigError(
            "config key replay.thresholds.route_dominated_min_ratio must be in [0, 1]"
        )
    high_util = float(replay_thresholds["high_util_min_pct"])
    if not 0.0 <= high_util <= 100.0:
        raise ConfigError(
            "config key replay.thresholds.high_util_min_pct must be in [0, 100]"
        )
    if float(replay_thresholds["semantic_preflight_max_age_s"]) <= 0.0:
        raise ConfigError(
            "config key replay.thresholds.semantic_preflight_max_age_s must be positive"
        )
    if float(replay_thresholds["low_gain_max_delta_mhz"]) < 0.0:
        raise ConfigError(
            "config key replay.thresholds.low_gain_max_delta_mhz must be nonnegative"
        )
    llm = data.get("llm", {})
    request_timeout = llm.get("request_timeout_s")
    if request_timeout is not None and (
        not math.isfinite(float(request_timeout)) or float(request_timeout) <= 0
    ):
        raise ConfigError("config key llm.request_timeout_s must be finite and positive")
    retry = llm.get("retry", {}) or {}
    retry_attempts = retry.get("attempts")
    if retry_attempts is not None and not 1 <= retry_attempts <= 6:
        raise ConfigError("config key llm.retry.attempts must be in [1, 6]")
    initial_delay = retry.get("initial_delay_s")
    max_delay = retry.get("max_delay_s")
    for key, value in (
        ("initial_delay_s", initial_delay),
        ("max_delay_s", max_delay),
    ):
        if value is not None and (
            not math.isfinite(float(value)) or float(value) < 0
        ):
            raise ConfigError(
                f"config key llm.retry.{key} must be finite and nonnegative"
            )
    if (
        initial_delay is not None
        and max_delay is not None
        and initial_delay > max_delay
    ):
        raise ConfigError(
            "config key llm.retry.initial_delay_s cannot exceed max_delay_s"
        )
    beam_width = data.get("run", {}).get("candidate_beam_width")
    if beam_width is not None and beam_width < 1:
        raise ConfigError("config key run.candidate_beam_width must be at least 1")
    for key in ("enabling_candidate_pool_width", "enabling_composition_max_depth"):
        value = data.get("run", {}).get(key)
        if value is not None and value < 1:
            raise ConfigError(f"config key run.{key} must be at least 1")
    tolerance = data.get("run", {}).get(
        "enabling_residual_regression_tolerance_ns"
    )
    if tolerance is not None and (
        not math.isfinite(float(tolerance)) or float(tolerance) < 0.0
    ):
        raise ConfigError(
            "config key run.enabling_residual_regression_tolerance_ns must be "
            "finite and nonnegative"
        )
    epsilon = data.get("run", {}).get("enabling_local_improvement_epsilon_ns")
    if epsilon is not None and (
        not math.isfinite(float(epsilon)) or float(epsilon) <= 0.0
    ):
        raise ConfigError(
            "config key run.enabling_local_improvement_epsilon_ns must be "
            "finite and positive"
        )
    promotion_delta = data.get("run", {}).get("promotion_min_delta_mhz")
    if promotion_delta is not None and promotion_delta < 0:
        raise ConfigError(
            "config key run.promotion_min_delta_mhz must be nonnegative"
        )
    skill_candidate_cap = data.get("run", {}).get("max_skill_candidates_per_action")
    if skill_candidate_cap is not None and skill_candidate_cap < 1:
        raise ConfigError(
            "config key run.max_skill_candidates_per_action must be at least 1"
        )
    minimum_dimensions = data.get("run", {}).get(
        "candidate_beam_diversity_min_common_dimensions"
    )
    if minimum_dimensions is not None and minimum_dimensions < 1:
        raise ConfigError(
            "config key run.candidate_beam_diversity_min_common_dimensions "
            "must be at least 1"
        )
    signature_paths = data.get("analysis", {}).get("candidate_signature_num_paths")
    if signature_paths is not None and not 1 <= signature_paths <= 80:
        raise ConfigError(
            "config key analysis.candidate_signature_num_paths must be in [1, 80]"
        )
    profile_paths = data.get("analysis", {}).get("profile_num_paths")
    if profile_paths is not None and not 1 <= profile_paths <= 80:
        raise ConfigError(
            "config key analysis.profile_num_paths must be in [1, 80]"
        )
    signature_timeout = data.get("analysis", {}).get("candidate_signature_timeout_s")
    if signature_timeout is not None and (
        not math.isfinite(float(signature_timeout)) or signature_timeout <= 0
    ):
        raise ConfigError(
            "config key analysis.candidate_signature_timeout_s must be finite and positive"
        )
    knowledge_budget = data.get("react", {}).get("knowledge_stage_budget_s")
    if knowledge_budget is not None and (
        not math.isfinite(float(knowledge_budget))
        or not 10 <= float(knowledge_budget) <= 600
    ):
        raise ConfigError(
            "config key react.knowledge_stage_budget_s must be in [10, 600]"
        )
    preflight_budget = data.get("analysis", {}).get(
        "registered_product_sum_preflight_budget_s"
    )
    if preflight_budget is not None and (
        not math.isfinite(float(preflight_budget))
        or not 30 <= float(preflight_budget) <= 1800
    ):
        raise ConfigError(
            "config key analysis.registered_product_sum_preflight_budget_s "
            "must be in [30, 1800]"
        )
    family_deadline = data.get("analysis", {}).get(
        "registered_product_sum_family_deadline_s"
    )
    if family_deadline is not None and (
        not math.isfinite(float(family_deadline))
        or not 30 <= float(family_deadline) <= 900
    ):
        raise ConfigError(
            "config key analysis.registered_product_sum_family_deadline_s "
            "must be in [30, 900]"
        )
    for key in ("candidate_commit_reserve_s", "central_measurement_estimate_s"):
        value = data.get("run", {}).get(key)
        if value is not None and (
            not math.isfinite(float(value)) or float(value) < 0
        ):
            raise ConfigError(f"config key run.{key} must be finite and nonnegative")
    candidate_estimates = data.get("skills", {}).get(
        "candidate_runtime_estimates_s", {}
    ) or {}
    for value in candidate_estimates.values():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value <= 0
        ):
            raise ConfigError(
                "config key skills.candidate_runtime_estimates_s values must be "
                "finite positive numbers"
            )
    platform_id = data.get("platform", {}).get("id")
    if platform_id is not None and not platform_id.strip():
        raise ConfigError("config key platform.id must be nonempty")
    level1 = data.get("saturation", {}).get("level1", {})
    threshold = level1.get("low_gain_max_delta_mhz")
    if threshold is not None and (
        not math.isfinite(float(threshold)) or threshold < 0
    ):
        raise ConfigError(
            "config key saturation.level1.low_gain_max_delta_mhz must be nonnegative"
        )
    minimum = level1.get("minimum_consecutive")
    if minimum is not None and minimum < 1:
        raise ConfigError(
            "config key saturation.level1.minimum_consecutive must be at least 1"
        )
    max_scopes = level1.get("max_scopes_in_llm_state")
    if max_scopes is not None and max_scopes < 1:
        raise ConfigError(
            "config key saturation.level1.max_scopes_in_llm_state must be at least 1"
        )
    families = level1.get("strategy_family_by_skill") or {}
    if any(
        not isinstance(skill, str)
        or not skill.strip()
        or not isinstance(family, str)
        or not family.strip()
        for skill, family in families.items()
    ):
        raise ConfigError(
            "config key saturation.level1.strategy_family_by_skill must map "
            "nonempty strings to nonempty strings"
        )
    vectors = data.get("equivalence", {}).get("vectors")
    precheck = data.get("equivalence", {}).get("precheck_vectors")
    reserve = data.get("equivalence", {}).get("time_reserve_s")
    if vectors is not None and vectors < 1:
        raise ConfigError("config key equivalence.vectors must be at least 1")
    if precheck is not None and precheck < 0:
        raise ConfigError("config key equivalence.precheck_vectors must be nonnegative")
    if reserve is not None and reserve < 0:
        raise ConfigError("config key equivalence.time_reserve_s must be nonnegative")
    scheduling = data.get("score_scheduling", {})
    history_limit = scheduling.get("calibration_history_limit")
    state_limit = scheduling.get("calibration_records_in_llm_state")
    if history_limit is not None and history_limit < 1:
        raise ConfigError(
            "config key score_scheduling.calibration_history_limit must be at least 1"
        )
    if state_limit is not None and state_limit < 1:
        raise ConfigError(
            "config key score_scheduling.calibration_records_in_llm_state "
            "must be at least 1"
        )
    if (
        history_limit is not None
        and state_limit is not None
        and state_limit > history_limit
    ):
        raise ConfigError(
            "score scheduling LLM-state record limit cannot exceed history limit"
        )
    action_review = data.get("action_review", {})
    for key in (
        "registry_max_records",
        "transaction_history_limit",
        "exact_history_limit",
        "family_history_limit",
    ):
        value = action_review.get(key)
        if value is not None and value < 1:
            raise ConfigError(f"config key action_review.{key} must be at least 1")
    bo = data.get("bo", {})
    for key in ("model_root", "runtime_python"):
        value = bo.get(key)
        if bo.get("enabled") and (not isinstance(value, str) or not value.strip()):
            raise ConfigError(f"config key bo.{key} must be nonempty when BO is enabled")
    bo_timeout = bo.get("timeout_s")
    if bo_timeout is not None and (
        not math.isfinite(float(bo_timeout)) or float(bo_timeout) <= 0
    ):
        raise ConfigError("config key bo.timeout_s must be finite and positive")
    bo_time = bo.get("time", {}) or {}
    if bo.get("enabled") and bo_time.get("enabled") and not str(
        bo_time.get("model_root") or ""
    ).strip():
        raise ConfigError(
            "config key bo.time.model_root must be nonempty when runtime BO is enabled"
        )
    bo_reserve = bo_time.get("reserve_s")
    if bo_reserve is not None and (
        not math.isfinite(float(bo_reserve)) or float(bo_reserve) < 0
    ):
        raise ConfigError("config key bo.time.reserve_s must be finite and nonnegative")
    submission = data.get("submission", {})
    for key in ("validator_timeout_s", "export_guard_s", "process_kill_grace_s"):
        value = submission.get(key)
        if value is not None and (
            not math.isfinite(float(value)) or float(value) <= 0
        ):
            raise ConfigError(f"config key submission.{key} must be finite and positive")
    for key in ("max_artifact_bytes", "max_evidence_bytes"):
        value = submission.get(key)
        if value is not None and value < 1:
            raise ConfigError(f"config key submission.{key} must be positive")
    react = data.get("react", {})
    if react.get("tool_choice") not in {None, "auto"}:
        raise ConfigError("config key react.tool_choice must be 'auto'")
    positive_react_keys = {
        "max_tool_turns": react.get("max_tool_turns"),
        "terminal_repair_turns": react.get("terminal_repair_turns"),
        "max_seed_switches_per_decision": react.get(
            "max_seed_switches_per_decision"
        ),
        **{
            f"grep.{key}": value
            for key, value in (react.get("grep", {}) or {}).items()
        },
        **{
            f"stage_max_tool_turns.{key}": value
            for key, value in (react.get("stage_max_tool_turns", {}) or {}).items()
        },
    }
    for key, value in positive_react_keys.items():
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0
        ):
            raise ConfigError(f"config key react.{key} must be finite and positive")
    grep = react.get("grep", {}) or {}
    per_call = grep.get("max_output_bytes_per_call")
    total = grep.get("max_total_output_bytes")
    calls = max(
        int(grep.get("max_calls_report_stage") or 0),
        int(grep.get("max_calls_research_stage") or 0),
    )
    if per_call is not None and total is not None and calls > 0:
        # A per-call ceiling above total/calls lets early calls starve every
        # later grep in the same stage, which then fails outright.
        if int(per_call) * calls > int(total):
            raise ConfigError(
                "config key react.grep.max_output_bytes_per_call must not exceed "
                "max_total_output_bytes divided by the largest stage call budget"
            )


def load_config(user_path: Optional[str | Path] = None) -> Config:
    """Load packaged defaults, merge optional user YAML, env overrides."""
    with DEFAULT_CONFIG_PATH.open() as f:
        data = yaml.safe_load(f) or {}

    candidate = user_path or os.environ.get("FDAGENTS_CONFIG")
    if candidate:
        user_file = Path(candidate).expanduser()
        if not user_file.exists():
            raise ConfigError(f"FDAGENTS_CONFIG file not found: {user_file}")
        with user_file.open() as f:
            user_data = yaml.safe_load(f) or {}
        if not isinstance(user_data, dict):
            raise ConfigError(f"config file {user_file} must contain a mapping")
        data = _deep_merge(data, user_data)
        logger.info("[config] merged user config from %s", user_file)

    _apply_env_overrides(data)
    _validate(data)
    return Config(data)


_cached: Optional[Config] = None


def get_config() -> Config:
    """Process-wide cached config (call sites that don't need injection)."""
    global _cached
    if _cached is None:
        _cached = load_config()
    return _cached


def reset_config_cache() -> None:
    global _cached
    _cached = None
