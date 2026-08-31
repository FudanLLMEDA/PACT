"""Typed mechanics for resolving semantic skill targets into concrete actions."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from .skills.clock_tighten import _RECIPE_ALIASES, _RECIPE_COMMANDS
from .skills.fresh_place_route import (
    DEFAULT_RECIPES as FRESH_RECIPES,
    POST_PLACE_RECIPE_REGISTRY,
    ROUTE_DIRECTIVE_ALLOWLIST,
    SHARED_POST_PLACE_ROUTE_RACE,
)
from .skills.implementation_recipe import (
    IMPLEMENTATION_RECIPE_REGISTRY,
    is_strategy_param as is_implementation_strategy_param,
    is_scheduling_param as is_implementation_scheduling_param,
)
from .skills.phys_opt import PHYS_OPT_PRESET_ARGS
from .skills.post_route_cleanup import DEFAULT_RECIPES as CLEANUP_RECIPES
from .skills.per_net_unroute import (
    DEFAULT_MAX_FANOUT as PER_NET_DEFAULT_MAX_FANOUT,
    DEFAULT_MIN_FANOUT as PER_NET_DEFAULT_MIN_FANOUT,
    DEFAULT_NUM_PATHS as PER_NET_DEFAULT_NUM_PATHS,
    DEFAULT_REPLICATION_PASSES as PER_NET_DEFAULT_REPLICATION_PASSES,
    DEFAULT_ROUTE_TIMEOUT_S as PER_NET_DEFAULT_ROUTE_TIMEOUT_S,
)
from .skills.hard_macro_move import (
    DEFAULT_ATTEMPT_TIMEOUT_S as HARD_MACRO_DEFAULT_ATTEMPT_TIMEOUT_S,
    DEFAULT_MAX_CANDIDATES as HARD_MACRO_DEFAULT_MAX_CANDIDATES,
    DEFAULT_MAX_SITE_DISTANCE as HARD_MACRO_DEFAULT_MAX_SITE_DISTANCE,
    DEFAULT_NUM_PATHS as HARD_MACRO_DEFAULT_NUM_PATHS,
    MAX_CANDIDATES as HARD_MACRO_MAX_CANDIDATES,
    MAX_NUM_PATHS as HARD_MACRO_MAX_NUM_PATHS,
    MAX_SITE_DISTANCE as HARD_MACRO_MAX_SITE_DISTANCE,
)
from .skills.selective_branch_reroute import (
    MAX_BRANCHES as SELECTIVE_MAX_BRANCHES,
    MAX_CANDIDATES as SELECTIVE_MAX_CANDIDATES,
    MAX_CRITICAL_PATHS as SELECTIVE_MAX_CRITICAL_PATHS,
    SELECTIVE_ROUTE_DIRECTIVE_ALLOWLIST,
    _normalize_verified_connections,
)
from .skills.structure_relocation import (
    MAX_CANDIDATES as STRUCTURE_MAX_CANDIDATES,
    STRUCTURE_RELOCATION_TARGETS,
    STRUCTURE_ROUTE_DIRECTIVES,
    normalize_offsets,
)
from .skills.equivalent_source_remap import (
    AUTO_TARGET as EQUIVALENT_SOURCE_AUTO_TARGET,
    MAX_CANDIDATES as EQUIVALENT_SOURCE_MAX_CANDIDATES,
    MAX_MIN_DISTANCE as EQUIVALENT_SOURCE_MAX_MIN_DISTANCE,
)
from .skills.operator_rewrite import (
    MAX_CANDIDATES as OPERATOR_REWRITE_MAX_CANDIDATES,
    OPERATOR_REWRITE_REGISTRY,
)
from .skills.semantic_replay import MAX_CANDIDATES as SEMANTIC_REPLAY_MAX_CANDIDATES
from .replay import ACTION_TEMPLATES, ReplayAction


class CapabilityError(ValueError):
    """Raised when an action cannot honor the requested target semantics."""


def registered_arithmetic_capability_schema() -> dict[str, Any]:
    """Publish the Phase-4 schema without registering a mutation target."""
    from .regarith_tools.agent_evidence import (
        registered_arithmetic_capability_schema as tool_schema,
    )

    return tool_schema()


@dataclass(frozen=True)
class ResolvedAction:
    skill: str
    canonical_target: str
    execution_target: str
    resolved_params: dict
    concrete_action: dict
    action_fingerprint: str
    canonicalization_repairs: tuple[dict, ...] = ()

    @property
    def params(self) -> dict:
        return self.resolved_params

    @property
    def concrete_action_data(self) -> dict:
        return self.concrete_action

    @property
    def fingerprint(self) -> str:
        return self.action_fingerprint

    @property
    def physical_fingerprint(self) -> str:
        return self.action_fingerprint


_META_KEYS = {"target_candidates", "target_params", "replace_target_candidates"}
_SCHEDULING_KEYS = {
    "input_dcp",
    "min_remaining_s",
    "min_candidate_remaining_s",
    "validation_reserve_s",
}

_FRESH_BY_NAME = {str(item["name"]): dict(item) for item in FRESH_RECIPES}
_CLEANUP_BY_NAME = {str(item["name"]): dict(item) for item in CLEANUP_RECIPES}

_CLEANUP_TARGET_FALLBACKS = {
    "pre_lut_directive": {
        "recipes": [
            "phys_explore",
            "route_aggr_phys_explore",
            "route_higher_tns_phys_explore",
        ],
        "max_candidates": 3,
        "max_iterations": 2,
        "min_remaining_s": 1200,
        "recipe_timeout_s": 900,
    },
    "post_lut_cleanup": {
        "recipes": ["route_higher_tns_phys_explore"],
        "max_candidates": 1,
        "max_iterations": 1,
        "recipe_timeout_s": 900,
    },
}

_REROUTE_TARGET_FALLBACKS = {
    "worst_source": {
        "selection_mode": "worst_source",
        "route_mode": "explore_group",
        "post_phys_opt": None,
        "pressure_path_count": 0,
        "pressure_value_ns": None,
        "num_paths": 1,
        "max_candidates": 1,
        "max_fanout": 256,
        "route_timeout_s": 900,
    },
    "boundary_link": {
        "selection_mode": "boundary_link",
        "route_mode": "delay_group",
        "post_phys_opt": None,
        "pressure_path_count": 0,
        "pressure_value_ns": None,
        "num_paths": 32,
        "max_candidates": 1,
        "max_fanout": 96,
        "route_timeout_s": 900,
    },
    "early_window": {
        "selection_mode": "window",
        "route_mode": "explore_group",
        "post_phys_opt": "Explore",
        "num_paths": 1,
        "max_candidates": 3,
        "max_fanout": 256,
        "route_timeout_s": 900,
    },
    "high_delay10": {
        "selection_mode": "high_delay",
        "route_mode": "explore_group",
        "post_phys_opt": "Explore",
        "num_paths": 3,
        "max_nets": 10,
        "max_fanout": 512,
        "route_timeout_s": 900,
    },
    "top3_final": {
        "selection_mode": "top3",
        "route_mode": "explore_group",
        "post_phys_opt": "Explore",
        "num_paths": 1,
        "max_nets": 3,
        "max_fanout": 512,
        "route_timeout_s": 900,
    },
}

_SHARED_ROUTE_RACE_KEYS = {
    "flow_mode",
    "post_place_recipe",
    "route_directives",
    "post_route_phys_opt_preset",
    "max_candidates",
    "min_remaining_s",
    "min_candidate_remaining_s",
    "recipe_timeout_s",
    "prefix_timeout_s",
    "route_timeout_s",
    "post_route_phys_opt_timeout_s",
}


def _is_scheduling_key(key: str) -> bool:
    return key in _SCHEDULING_KEYS or key.endswith("_timeout_s")


def _physical_params(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            str(key): _physical_params(value)
            for key, value in node.items()
            if key not in _META_KEYS and not _is_scheduling_key(str(key))
        }
    if isinstance(node, (list, tuple)):
        return [_physical_params(value) for value in node]
    return node


def _stable_data(node: Any) -> Any:
    if isinstance(node, dict):
        return {str(key): _stable_data(value) for key, value in node.items()}
    if isinstance(node, (list, tuple)):
        return [_stable_data(value) for value in node]
    if node is None or isinstance(node, (bool, int, float, str)):
        return node
    return str(node)


def _same_typed_value(left: Any, right: Any) -> bool:
    """Compare target-owned values without treating bool as int."""
    if type(left) is not type(right):
        return False
    return left == right


def _recipe_items(raw: Any) -> list[Any]:
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    if isinstance(raw, (list, tuple)):
        return list(raw)
    raise CapabilityError("recipes must be a name, list of names, or recipe objects")


def _normalize_recipes(skill: str, raw: Any) -> list[dict[str, str]]:
    if raw is None:
        return []
    if skill == "fresh_place_route":
        registry = _FRESH_BY_NAME
        aliases: dict[str, str] = {}
    elif skill == "post_route_cleanup":
        registry = _CLEANUP_BY_NAME
        aliases = {}
    elif skill == "clock_tighten":
        registry = {
            name: {"name": name, "command": command}
            for name, command in _RECIPE_COMMANDS.items()
        }
        aliases = _RECIPE_ALIASES
    else:
        return copy.deepcopy(raw)

    normalized: list[dict[str, str]] = []
    for item in _recipe_items(raw):
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            command = str(item.get("command") or "").strip()
            canonical = aliases.get(name, name)
            trusted = registry.get(canonical)
            if (
                not name
                or not command
                or trusted is None
                or command != str(trusted["command"])
            ):
                raise CapabilityError(
                    "public recipe objects are forbidden; select a registered target"
                )
            normalized.append(copy.deepcopy(trusted))
            continue
        if not isinstance(item, str) or not item.strip():
            raise CapabilityError("recipes must contain names or recipe objects")
        requested = item.strip()
        canonical = aliases.get(requested, requested)
        recipe = registry.get(canonical)
        if recipe is None:
            raise CapabilityError(
                f"unknown {skill} recipe {requested!r}; custom Tcl requires "
                "a registered typed target"
            )
        normalized.append(copy.deepcopy(recipe))
    return normalized


def _normalize_recipe_param(skill: str, params: dict) -> dict:
    normalized = copy.deepcopy(params)
    if "recipes" in normalized:
        normalized["recipes"] = _normalize_recipes(skill, normalized["recipes"])
    return normalized


def _target_preset(
    skill: str,
    target: str,
    explicit_params: dict,
    target_presets: dict,
) -> tuple[str, Optional[dict]]:
    if skill == "fresh_place_route" and target in _FRESH_BY_NAME:
        return target, {"recipes": [_FRESH_BY_NAME[target]], "max_candidates": 1}
    if skill == "post_route_cleanup" and target in _CLEANUP_BY_NAME:
        return target, {"recipes": [_CLEANUP_BY_NAME[target]], "max_candidates": 1}
    if skill == "clock_tighten":
        canonical = _RECIPE_ALIASES.get(target, target)
        command = _RECIPE_COMMANDS.get(canonical)
        if command is not None:
            return canonical, {"recipes": [{"name": canonical, "command": command}]}

    skill_presets = target_presets.get(skill, {})
    if not isinstance(skill_presets, dict):
        skill_presets = {}
    if target in skill_presets:
        preset = copy.deepcopy(skill_presets[target])
        if skill == "critical_net_reroute" and target in _REROUTE_TARGET_FALLBACKS:
            semantic = copy.deepcopy(_REROUTE_TARGET_FALLBACKS[target])
            if target in {"worst_source", "boundary_link"}:
                # These selectors attest a specific live-discovery mechanism.
                # Configuration may tune scheduling, but not its physical meaning.
                for key, value in preset.items():
                    if _is_scheduling_key(str(key)):
                        semantic[key] = value
            else:
                semantic.update(preset)
            preset = semantic
        return target, preset

    if skill == "post_route_cleanup":
        if target in _CLEANUP_TARGET_FALLBACKS:
            return target, copy.deepcopy(_CLEANUP_TARGET_FALLBACKS[target])
    if skill == "critical_net_reroute" and target in _REROUTE_TARGET_FALLBACKS:
        return target, copy.deepcopy(_REROUTE_TARGET_FALLBACKS[target])

    return target, None


def _fingerprint_action(skill: str, target: str, params: dict) -> tuple[dict, str]:
    concrete_action = {
        "skill": skill,
        "target": target,
        "params": _stable_data(_physical_params(params)),
    }
    canonical_json = json.dumps(
        concrete_action,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return concrete_action, hashlib.sha256(canonical_json.encode("ascii")).hexdigest()


def _resolve_shared_route_race_action(
    requested_target: str,
    params: dict,
    defaults: dict,
    target_presets: dict,
) -> ResolvedAction:
    repairs: list[dict] = []
    canonical_target = "auto"
    if requested_target != canonical_target:
        repairs.append({
            "kind": "flow_owned_target",
            "field": "target",
            "requested": requested_target,
            "canonical": canonical_target,
            "owner": SHARED_POST_PLACE_ROUTE_RACE,
        })
    skill_presets = target_presets.get("fresh_place_route", {}) or {}
    if not isinstance(skill_presets, dict):
        raise CapabilityError("fresh_place_route matched presets must be an object")
    auto_preset = skill_presets.get("auto", {}) or {}
    if not isinstance(auto_preset, dict):
        raise CapabilityError("fresh_place_route auto preset must be an object")

    resolved: dict = {}
    for source in (defaults, auto_preset, params):
        for key, value in source.items():
            key = str(key)
            if key in _META_KEYS or key not in _SHARED_ROUTE_RACE_KEYS:
                raise CapabilityError(
                    "shared_post_place_route_race rejects parameter " + repr(key)
                )
            resolved[key] = copy.deepcopy(value)

    if resolved.get("flow_mode") != SHARED_POST_PLACE_ROUTE_RACE:
        raise CapabilityError(
            f"unknown fresh_place_route flow_mode {resolved.get('flow_mode')!r}"
        )
    recipe_name = resolved.get("post_place_recipe")
    if not isinstance(recipe_name, str) or recipe_name not in POST_PLACE_RECIPE_REGISTRY:
        raise CapabilityError(
            f"unknown post_place_recipe {recipe_name!r}; allowed recipes: "
            f"{list(POST_PLACE_RECIPE_REGISTRY)}"
        )

    raw_directives = resolved.get("route_directives")
    if not isinstance(raw_directives, (list, tuple)) or not raw_directives:
        raise CapabilityError("route_directives must be a nonempty list")
    directives = []
    seen = set()
    for item in raw_directives:
        if not isinstance(item, str) or not item.strip():
            raise CapabilityError(
                "route_directives must contain nonempty directive names"
            )
        directive = item.strip()
        if directive not in ROUTE_DIRECTIVE_ALLOWLIST:
            raise CapabilityError(
                f"unknown route directive {directive!r}; allowed directives: "
                f"{list(ROUTE_DIRECTIVE_ALLOWLIST)}"
            )
        if directive not in seen:
            seen.add(directive)
            directives.append(directive)
    resolved["route_directives"] = directives

    preset = resolved.get("post_route_phys_opt_preset")
    if preset in (None, ""):
        resolved["post_route_phys_opt_preset"] = None
    elif not isinstance(preset, str) or preset not in PHYS_OPT_PRESET_ARGS:
        raise CapabilityError(
            f"unknown post_route_phys_opt_preset {preset!r}; allowed presets: "
            f"{list(PHYS_OPT_PRESET_ARGS)}"
        )

    max_candidates = resolved.get("max_candidates", len(directives))
    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int):
        raise CapabilityError("max_candidates must be an integer")
    if max_candidates < 1:
        raise CapabilityError("max_candidates must be at least 1")
    resolved["max_candidates"] = min(max_candidates, len(directives))

    for key in (
        "min_remaining_s",
        "min_candidate_remaining_s",
        "recipe_timeout_s",
        "prefix_timeout_s",
        "route_timeout_s",
        "post_route_phys_opt_timeout_s",
    ):
        if key not in resolved:
            continue
        value = resolved[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CapabilityError(f"{key} must be a number")
        if value < 0:
            raise CapabilityError(f"{key} must be nonnegative")

    concrete_action, fingerprint = _fingerprint_action(
        "fresh_place_route", canonical_target, resolved
    )
    return ResolvedAction(
        skill="fresh_place_route",
        canonical_target=canonical_target,
        execution_target=canonical_target,
        resolved_params=resolved,
        concrete_action=concrete_action,
        action_fingerprint=fingerprint,
        canonicalization_repairs=tuple(repairs),
    )


def _resolve_fixed_preset_action(
    skill: str,
    requested_target: str,
    params: dict,
    defaults: dict,
    target_presets: dict,
) -> ResolvedAction:
    registry = (
        IMPLEMENTATION_RECIPE_REGISTRY
        if skill == "implementation_recipe"
        else PHYS_OPT_PRESET_ARGS
    )
    if requested_target == "auto":
        raise CapabilityError(f"{skill} requires an explicit fixed preset target")
    if requested_target not in registry:
        raise CapabilityError(
            f"unknown {skill} preset {requested_target!r}; "
            f"allowed targets: {sorted(registry)}"
        )

    public_forbidden = {
        "recipes", "command", "tcl", "script", "tcl_ref", "backend_skill"
    }
    skill_presets = target_presets.get(skill, {}) or {}
    if not isinstance(skill_presets, dict):
        raise CapabilityError(f"{skill} matched presets must be an object")
    preset_params = skill_presets.get(requested_target, {})
    if not isinstance(preset_params, dict):
        raise CapabilityError(f"{skill} matched preset parameters must be an object")
    sources = (defaults, preset_params, params)
    resolved: dict = {}
    if skill == "implementation_recipe":
        spec = IMPLEMENTATION_RECIPE_REGISTRY[requested_target]
        resolved.update({
            key: copy.deepcopy(value)
            for key, value in spec.backend_params.items()
            if is_implementation_scheduling_param(str(key))
        })
    for source in sources:
        for key, value in source.items():
            key = str(key)
            if key in _META_KEYS:
                raise CapabilityError(f"{skill} rejects ignored parameter {key!r}")
            if key in public_forbidden:
                raise CapabilityError(f"{skill} forbids public parameter {key!r}")
            allowed = (
                (
                    is_implementation_scheduling_param(key)
                    or is_implementation_strategy_param(key)
                )
                if skill == "implementation_recipe"
                else key == "min_remaining_s"
            )
            if not allowed:
                raise CapabilityError(f"{skill} rejects unknown parameter {key!r}")
            if (
                key in resolved
                and resolved[key] != value
                and not is_implementation_scheduling_param(key)
            ):
                raise CapabilityError(
                    f"{skill} preset {requested_target!r} has conflicting {key!r} values"
                )
            resolved[key] = copy.deepcopy(value)

    if skill == "implementation_recipe":
        required_fields = {
            "derived_pressure_replace_route_v1": {
                "strategy_regime", "max_candidates", "pressure_factors",
            },
            "full_reimplementation_retime_v1": {
                "strategy_regime", "max_candidates", "pressure_factors",
                "route_directives",
            },
            "routed_critical_cleanup_v1": {
                "strategy_regime", "max_candidates", "cleanup_recipe",
            },
        }[requested_target]
        missing = sorted(field for field in required_fields if field not in resolved)
        if missing:
            raise CapabilityError(
                f"implementation_recipe {requested_target!r} is missing "
                f"current-seed fields {missing}"
            )

    concrete_action, fingerprint = _fingerprint_action(
        skill, requested_target, resolved
    )
    return ResolvedAction(
        skill=skill,
        canonical_target=requested_target,
        execution_target=requested_target,
        resolved_params=resolved,
        concrete_action=concrete_action,
        action_fingerprint=fingerprint,
    )


def _resolve_selective_branch_action(
    requested_target: str,
    params: dict,
    defaults: dict,
) -> ResolvedAction:
    allowed_targets = {"auto_high_detour", "verified_connections"}
    if requested_target not in allowed_targets:
        raise CapabilityError(
            "selective_branch_reroute target must be auto_high_detour or verified_connections"
        )
    allowed_keys = {
        "max_branches", "max_candidates", "num_paths", "min_detour_ratio",
        "route_directives", "verified_connections", "route_timeout_s",
        "min_candidate_remaining_s",
    }
    resolved: dict = {}
    for source_name, source in (("defaults", defaults), ("params", params)):
        for key, value in source.items():
            key = str(key)
            if key in _META_KEYS:
                raise CapabilityError(
                    f"selective_branch_reroute rejects ignored parameter {key!r}"
                )
            if key == "input_dcp" and source_name == "params":
                raise CapabilityError("LLM-provided DCP paths are forbidden")
            if key not in allowed_keys:
                raise CapabilityError(
                    f"selective_branch_reroute rejects parameter {key!r}"
                )
            resolved[key] = copy.deepcopy(value)

    def bounded_int(key: str, default: int, maximum: int) -> int:
        value = resolved.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise CapabilityError(f"{key} must be an integer in 1..{maximum}")
        return value

    resolved["max_branches"] = bounded_int(
        "max_branches", 2, SELECTIVE_MAX_BRANCHES
    )
    resolved["max_candidates"] = bounded_int(
        "max_candidates", SELECTIVE_MAX_CANDIDATES, SELECTIVE_MAX_CANDIDATES
    )
    resolved["num_paths"] = bounded_int(
        "num_paths", 8, SELECTIVE_MAX_CRITICAL_PATHS
    )
    ratio = resolved.get("min_detour_ratio", 1.3)
    if (
        isinstance(ratio, bool)
        or not isinstance(ratio, (int, float))
        or not 1.0 <= float(ratio) <= 20.0
    ):
        raise CapabilityError("min_detour_ratio must be a number in 1.0..20.0")
    resolved["min_detour_ratio"] = float(ratio)

    raw_directives = resolved.get("route_directives", ["Default"])
    if not isinstance(raw_directives, (list, tuple)) or not 1 <= len(raw_directives) <= 4:
        raise CapabilityError("route_directives must contain 1..4 fixed directive names")
    directives = []
    for value in raw_directives:
        if not isinstance(value, str) or value not in SELECTIVE_ROUTE_DIRECTIVE_ALLOWLIST:
            raise CapabilityError(
                f"route directive {value!r} is not allowlisted; allowed: "
                f"{list(SELECTIVE_ROUTE_DIRECTIVE_ALLOWLIST)}"
            )
        if value not in directives:
            directives.append(value)
    resolved["route_directives"] = directives

    if "route_timeout_s" in resolved:
        timeout = resolved["route_timeout_s"]
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 3600:
            raise CapabilityError("route_timeout_s must be a number in 1..3600")
    if "min_candidate_remaining_s" in resolved:
        reserve = resolved["min_candidate_remaining_s"]
        if isinstance(reserve, bool) or not isinstance(reserve, (int, float)) or not 0 <= reserve <= 3600:
            raise CapabilityError("min_candidate_remaining_s must be a number in 0..3600")

    if requested_target == "verified_connections":
        try:
            resolved["verified_connections"] = _normalize_verified_connections(
                resolved.get("verified_connections")
            )
        except ValueError as exc:
            raise CapabilityError(str(exc)) from exc
    elif "verified_connections" in resolved:
        raise CapabilityError(
            "auto_high_detour does not accept explicit verified_connections"
        )

    concrete_action, fingerprint = _fingerprint_action(
        "selective_branch_reroute", requested_target, resolved
    )
    return ResolvedAction(
        skill="selective_branch_reroute",
        canonical_target=requested_target,
        execution_target=requested_target,
        resolved_params=resolved,
        concrete_action=concrete_action,
        action_fingerprint=fingerprint,
    )


def _resolve_structure_relocation_action(
    requested_target: str,
    params: dict,
    defaults: dict,
) -> ResolvedAction:
    if requested_target not in STRUCTURE_RELOCATION_TARGETS:
        raise CapabilityError(
            "structure_relocation target must be carry_cloud or hard_macro_cascade"
        )
    allowed_keys = {"structure_id", "offsets", "max_candidates", "route_directive"}
    resolved = {}
    for source_name, source in (("defaults", defaults), ("params", params)):
        for key, value in source.items():
            key = str(key)
            if key in _META_KEYS:
                raise CapabilityError(
                    f"structure_relocation rejects ignored parameter {key!r}"
                )
            if key == "input_dcp" and source_name == "params":
                raise CapabilityError("LLM-provided DCP paths are forbidden")
            if key not in allowed_keys:
                raise CapabilityError(
                    f"structure_relocation rejects parameter {key!r}"
                )
            resolved[key] = copy.deepcopy(value)

    structure_id = resolved.get("structure_id", "auto")
    if not isinstance(structure_id, str) or (
        structure_id != "auto"
        and not re.fullmatch(
            rf"{requested_target}:[0-9a-f]{{20}}", structure_id
        )
    ):
        raise CapabilityError("structure_id must be auto or a digest-bound typed ID")
    resolved["structure_id"] = structure_id
    try:
        resolved["offsets"] = [
            [col, row] for col, row in normalize_offsets(resolved.get("offsets"))
        ]
    except ValueError as exc:
        raise CapabilityError(str(exc)) from exc
    max_candidates = resolved.get("max_candidates", STRUCTURE_MAX_CANDIDATES)
    if (
        isinstance(max_candidates, bool) or not isinstance(max_candidates, int)
        or not 1 <= max_candidates <= STRUCTURE_MAX_CANDIDATES
    ):
        raise CapabilityError(
            f"max_candidates must be an integer in 1..{STRUCTURE_MAX_CANDIDATES}"
        )
    resolved["max_candidates"] = max_candidates
    directive = resolved.get("route_directive", "Default")
    if not isinstance(directive, str) or directive not in STRUCTURE_ROUTE_DIRECTIVES:
        raise CapabilityError(
            f"route_directive must be one of {list(STRUCTURE_ROUTE_DIRECTIVES)}"
        )
    resolved["route_directive"] = directive
    concrete_action, fingerprint = _fingerprint_action(
        "structure_relocation", requested_target, resolved
    )
    return ResolvedAction(
        skill="structure_relocation",
        canonical_target=requested_target,
        execution_target=requested_target,
        resolved_params=resolved,
        concrete_action=concrete_action,
        action_fingerprint=fingerprint,
    )


def _resolve_equivalent_source_action(
    requested_target: str,
    params: dict,
    defaults: dict,
) -> ResolvedAction:
    if requested_target != EQUIVALENT_SOURCE_AUTO_TARGET and not re.fullmatch(
        r"[0-9a-f]{64}", requested_target
    ):
        raise CapabilityError(
            "equivalent_source_remap target must be auto_nearest_proven or a certificate_id"
        )
    allowed = {"max_candidates", "min_distance"}
    resolved = {}
    for source_name, source in (("defaults", defaults), ("params", params)):
        for key, value in source.items():
            key = str(key)
            if key == "input_dcp" and source_name == "params":
                raise CapabilityError("LLM-provided DCP paths are forbidden")
            if key not in allowed:
                raise CapabilityError(
                    f"equivalent_source_remap rejects public parameter {key!r}"
                )
            resolved[key] = copy.deepcopy(value)
    maximums = {
        "max_candidates": EQUIVALENT_SOURCE_MAX_CANDIDATES,
        "min_distance": EQUIVALENT_SOURCE_MAX_MIN_DISTANCE,
    }
    defaults_by_key = {"max_candidates": 4, "min_distance": 1}
    for key, maximum in maximums.items():
        value = resolved.get(key, defaults_by_key[key])
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise CapabilityError(f"{key} must be an integer in 1..{maximum}")
        resolved[key] = value
    concrete_action, fingerprint = _fingerprint_action(
        "equivalent_source_remap", requested_target, resolved
    )
    return ResolvedAction(
        skill="equivalent_source_remap",
        canonical_target=requested_target,
        execution_target=requested_target,
        resolved_params=resolved,
        concrete_action=concrete_action,
        action_fingerprint=fingerprint,
    )


def _resolve_operator_rewrite_action(
    requested_target: str,
    params: dict,
    defaults: dict,
) -> ResolvedAction:
    if requested_target not in OPERATOR_REWRITE_REGISTRY:
        raise CapabilityError(
            "operator_rewrite requires one exact registered template target; "
            f"allowed targets: {sorted(OPERATOR_REWRITE_REGISTRY)}"
        )
    allowed = {
        "max_candidates", "route_timeout_s", "min_candidate_remaining_s",
    }
    if requested_target in {
        "registered_arithmetic_cone_v1", "dsp_srl_recurrence_cut_v1",
    }:
        allowed.add("family_fingerprint")
    teammate_family_targets = {
        "proved_arithmetic_compressor_v1",
        "fixed_point_dsp_rebuild_v1",
        "registered_fir_data_cone_rewrite",
        "consumer_driven_registered_arithmetic_rewrite_v1",
        "constructive_accumulator_rebuild_v1",
        "exact_control_to_data_state_equation_v1",
        "ff_srl_boundary_bridge_v1",
        "memory_write_buffer_forward_v1",
        "multilevel_control_distribution_localization_v1",
        "registered_gf2_parity_reassociation_v1",
    }
    if requested_target in teammate_family_targets:
        allowed.add("family_ref")
    if requested_target == "proved_arithmetic_compressor_v1":
        allowed.add("qor_profiles")
    if requested_target == "registered_gf2_parity_reassociation_v1":
        allowed.add("implementation_mode")
    resolved = {}
    for source_name, source in (("defaults", defaults), ("params", params)):
        for key, value in source.items():
            key = str(key)
            if key == "input_dcp" and source_name == "params":
                raise CapabilityError("LLM-provided DCP paths are forbidden")
            if key in _META_KEYS or key not in allowed:
                raise CapabilityError(
                    f"operator_rewrite rejects public parameter {key!r}"
                )
            resolved[key] = copy.deepcopy(value)

    max_candidates = resolved.get("max_candidates", OPERATOR_REWRITE_MAX_CANDIDATES)
    if (
        isinstance(max_candidates, bool)
        or not isinstance(max_candidates, int)
        or max_candidates != OPERATOR_REWRITE_MAX_CANDIDATES
    ):
        raise CapabilityError(
            f"max_candidates must be exactly {OPERATOR_REWRITE_MAX_CANDIDATES}"
        )
    resolved["max_candidates"] = max_candidates
    family_ref = resolved.get("family_ref")
    if family_ref is not None:
        family_patterns = {
            "exact_control_to_data_state_equation_v1": r"control_state_[1-9][0-9]*",
            "ff_srl_boundary_bridge_v1": r"ff_srl_[1-9][0-9]*",
            "memory_write_buffer_forward_v1": r"memory_forward_[1-9][0-9]*",
            "multilevel_control_distribution_localization_v1": r"multilevel_control_[1-9][0-9]*",
            "registered_gf2_parity_reassociation_v1": r"registered_parity_[1-9][0-9]*",
        }
        pattern = family_patterns.get(requested_target, r"sequential_[1-9][0-9]*")
        if not isinstance(family_ref, str) or re.fullmatch(pattern, family_ref) is None:
            raise CapabilityError(
                f"{requested_target} received an invalid current-seed family_ref"
            )
    if "qor_profiles" in resolved:
        profiles = resolved["qor_profiles"]
        if (
            not isinstance(profiles, list)
            or len(profiles) != max_candidates
            or any(item not in {"compact_timing", "net_delay_focus"} for item in profiles)
        ):
            raise CapabilityError("qor_profiles must contain registered unique profiles")
    if "implementation_mode" in resolved and resolved["implementation_mode"] not in {
        "local_preserve", "closure_local_rebasin", "global_quick_rebasin",
    }:
        raise CapabilityError("invalid registered parity implementation_mode")
    if requested_target == "registered_gf2_parity_reassociation_v1":
        resolved.setdefault("implementation_mode", "local_preserve")
    if requested_target == "registered_arithmetic_cone_v1":
        fingerprint = resolved.get("family_fingerprint")
        if not isinstance(fingerprint, str) or re.fullmatch(
            r"[0-9a-f]{64}", fingerprint
        ) is None:
            raise CapabilityError(
                "registered_arithmetic_cone_v1 requires one lowercase "
                "family_fingerprint from the current decision packet"
            )
    elif requested_target == "dsp_srl_recurrence_cut_v1" and (
        "family_fingerprint" in resolved
        and (
            not isinstance(resolved["family_fingerprint"], str)
            or re.fullmatch(r"[0-9a-f]{64}", resolved["family_fingerprint"])
            is None
        )
    ):
        raise CapabilityError(
            "dsp_srl_recurrence_cut_v1 family_fingerprint must be one "
            "lowercase digest from the current decision packet"
        )
    for key, low, high in (
        ("route_timeout_s", 1.0, 3600.0),
        ("min_candidate_remaining_s", 0.0, 3600.0),
    ):
        if key not in resolved:
            continue
        value = resolved[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not low <= float(value) <= high
        ):
            raise CapabilityError(f"{key} must be a number in {low:g}..{high:g}")

    concrete_action, fingerprint = _fingerprint_action(
        "operator_rewrite", requested_target, resolved
    )
    return ResolvedAction(
        skill="operator_rewrite",
        canonical_target=requested_target,
        execution_target=requested_target,
        resolved_params=resolved,
        concrete_action=concrete_action,
        action_fingerprint=fingerprint,
    )


def _resolve_semantic_replay_action(
    requested_target: str,
    params: dict,
    defaults: dict,
) -> ResolvedAction:
    if requested_target != "auto":
        raise CapabilityError("semantic_replay accepts only target='auto'")
    allowed = {
        "max_candidates",
        "implementation_timeout_s",
        "validation_timeout_s",
        "validation_reserve_s",
        "min_candidate_remaining_s",
    }
    resolved = {}
    for source_name, source in (("defaults", defaults), ("params", params)):
        for key, value in source.items():
            key = str(key)
            if key == "input_dcp" and source_name == "params":
                raise CapabilityError("LLM-provided DCP paths are forbidden")
            if key in _META_KEYS or key not in allowed:
                raise CapabilityError(
                    f"semantic_replay rejects public parameter {key!r}"
                )
            resolved[key] = copy.deepcopy(value)
    max_candidates = resolved.get("max_candidates", SEMANTIC_REPLAY_MAX_CANDIDATES)
    if (
        isinstance(max_candidates, bool)
        or not isinstance(max_candidates, int)
        or max_candidates != SEMANTIC_REPLAY_MAX_CANDIDATES
    ):
        raise CapabilityError(
            f"max_candidates must be exactly {SEMANTIC_REPLAY_MAX_CANDIDATES}"
        )
    resolved["max_candidates"] = max_candidates
    for key, default in (
        ("implementation_timeout_s", 2400.0),
        ("validation_timeout_s", 1800.0),
        ("validation_reserve_s", 900.0),
        ("min_candidate_remaining_s", 0.0),
    ):
        value = resolved.get(key, default)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0.0 <= float(value) <= 3600.0
            or (key in {"implementation_timeout_s", "validation_timeout_s"}
                and float(value) <= 0.0)
        ):
            raise CapabilityError(f"semantic_replay {key} is out of range")
        resolved[key] = float(value)
    concrete, fingerprint = _fingerprint_action(
        "semantic_replay", "auto", resolved
    )
    return ResolvedAction(
        skill="semantic_replay",
        canonical_target="auto",
        execution_target="auto",
        resolved_params=resolved,
        concrete_action=concrete,
        action_fingerprint=fingerprint,
    )


def _resolve_protected_reroute_action(
    requested_target: str,
    params: dict,
    defaults: dict,
    target_presets: dict,
) -> ResolvedAction:
    canonical = copy.deepcopy(_REROUTE_TARGET_FALLBACKS[requested_target])
    allowed = set(canonical)
    configured = (target_presets.get("critical_net_reroute") or {}).get(
        requested_target, {}
    )
    if not isinstance(configured, dict):
        configured = {}
    for source in (defaults, configured):
        for key, value in source.items():
            key = str(key)
            if key in allowed and _is_scheduling_key(key):
                canonical[key] = copy.deepcopy(value)

    public_action_fields = {
        "num_paths", "max_candidates", "max_fanout", "route_mode",
        "post_phys_opt",
    }
    for key, value in params.items():
        key = str(key)
        if key in _META_KEYS:
            raise CapabilityError(
                f"critical_net_reroute/{requested_target} rejects ignored "
                f"parameter {key!r}"
            )
        if (
            key == "input_dcp"
            or key not in allowed
            or (key not in public_action_fields and not _is_scheduling_key(key))
        ):
            raise CapabilityError(
                f"critical_net_reroute/{requested_target} rejects public "
                f"parameter {key!r}"
            )
        canonical[key] = copy.deepcopy(value)

    for key, low, high in (
        ("num_paths", 1, 64),
        ("max_candidates", 1, 1),
        ("max_fanout", 1, 1024),
    ):
        value = canonical.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not low <= value <= high
        ):
            raise CapabilityError(
                f"critical_net_reroute/{requested_target} {key} must be an "
                f"integer in {low}..{high}"
            )
    if requested_target == "worst_source" and canonical["num_paths"] != 1:
        raise CapabilityError("worst_source discovers exactly one live source")
    if canonical.get("route_mode") not in {"delay_group", "explore_group"}:
        raise CapabilityError(
            "protected critical-net reroute route_mode must be delay_group or "
            "explore_group"
        )
    if canonical.get("post_phys_opt") not in {None, "Explore"}:
        raise CapabilityError(
            "protected critical-net reroute post_phys_opt must be null or Explore"
        )
    route_timeout = canonical.get("route_timeout_s", 900)
    if (
        isinstance(route_timeout, bool)
        or not isinstance(route_timeout, (int, float))
        or not 1 <= float(route_timeout) <= 3600
    ):
        raise CapabilityError(
            "protected critical-net reroute route_timeout_s must be in 1..3600"
        )
    concrete, fingerprint = _fingerprint_action(
        "critical_net_reroute", requested_target, canonical
    )
    return ResolvedAction(
        skill="critical_net_reroute",
        canonical_target=requested_target,
        execution_target=requested_target,
        resolved_params=canonical,
        concrete_action=concrete,
        action_fingerprint=fingerprint,
        canonicalization_repairs=(),
    )


def _resolve_endpoint_action(
    requested_target: str,
    params: dict,
    defaults: dict,
) -> ResolvedAction:
    if requested_target != "auto":
        raise CapabilityError("endpoint_bel_move accepts only target='auto'")
    allowed = {
        "num_paths",
        "max_candidates",
        "attempt_timeout_s",
        "min_candidate_remaining_s",
    }
    resolved = {}
    for source_name, source in (("defaults", defaults), ("params", params)):
        for key, value in source.items():
            key = str(key)
            if key in _META_KEYS:
                continue
            if key == "input_dcp" or key not in allowed:
                raise CapabilityError(
                    f"endpoint_bel_move rejects public parameter {key!r}"
                )
            resolved[key] = copy.deepcopy(value)
    for key, default, maximum in (
        ("num_paths", 12, 50),
        ("max_candidates", 6, 20),
    ):
        value = resolved.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise CapabilityError(f"endpoint_bel_move {key} must be in 1..{maximum}")
        resolved[key] = value
    for key, default in (
        ("attempt_timeout_s", 1800.0),
        ("min_candidate_remaining_s", 0.0),
    ):
        value = resolved.get(key, default)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0.0 <= float(value) <= 3600.0
            or (key == "attempt_timeout_s" and float(value) <= 0.0)
        ):
            raise CapabilityError(f"endpoint_bel_move {key} is out of range")
        resolved[key] = value
    concrete, fingerprint = _fingerprint_action(
        "endpoint_bel_move", "auto", resolved
    )
    return ResolvedAction(
        skill="endpoint_bel_move",
        canonical_target="auto",
        execution_target="auto",
        resolved_params=resolved,
        concrete_action=concrete,
        action_fingerprint=fingerprint,
    )


def _resolve_per_net_unroute_action(
    requested_target: str,
    params: dict,
    defaults: dict,
) -> ResolvedAction:
    """Resolve the public auto selector to one bounded conservative recipe."""
    if requested_target != "auto":
        raise CapabilityError("per_net_unroute accepts only target='auto'")
    allowed = {
        "num_paths",
        "min_fanout",
        "max_fanout",
        "replication_passes",
        "route_timeout_s",
    }
    resolved = {}
    for source_name, source in (("defaults", defaults), ("params", params)):
        for key, value in source.items():
            key = str(key)
            if key == "input_dcp" and source_name == "params":
                raise CapabilityError("LLM-provided DCP paths are forbidden")
            if key in _META_KEYS or key not in allowed:
                raise CapabilityError(
                    f"per_net_unroute rejects public parameter {key!r}"
                )
            resolved[key] = copy.deepcopy(value)

    for key, default, maximum in (
        ("num_paths", PER_NET_DEFAULT_NUM_PATHS, 100),
        ("min_fanout", PER_NET_DEFAULT_MIN_FANOUT, 256),
        ("max_fanout", PER_NET_DEFAULT_MAX_FANOUT, 512),
        ("replication_passes", PER_NET_DEFAULT_REPLICATION_PASSES, 2),
    ):
        value = resolved.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise CapabilityError(f"per_net_unroute {key} must be in 1..{maximum}")
        resolved[key] = value
    if resolved["min_fanout"] > resolved["max_fanout"]:
        raise CapabilityError("per_net_unroute min_fanout must not exceed max_fanout")

    route_timeout = resolved.get("route_timeout_s", PER_NET_DEFAULT_ROUTE_TIMEOUT_S)
    if (
        isinstance(route_timeout, bool)
        or not isinstance(route_timeout, (int, float))
        or not 1.0 <= float(route_timeout) <= 3600.0
    ):
        raise CapabilityError("per_net_unroute route_timeout_s must be in 1..3600")
    resolved["route_timeout_s"] = float(route_timeout)

    concrete, fingerprint = _fingerprint_action(
        "per_net_unroute", "auto", resolved
    )
    return ResolvedAction(
        skill="per_net_unroute",
        canonical_target="auto",
        execution_target="auto",
        resolved_params=resolved,
        concrete_action=concrete,
        action_fingerprint=fingerprint,
    )


def _resolve_hard_macro_move_action(
    requested_target: str,
    params: dict,
    defaults: dict,
) -> ResolvedAction:
    if requested_target != "auto":
        raise CapabilityError("hard_macro_move accepts only target='auto'")
    allowed = {
        "num_paths", "max_candidates", "max_site_distance",
        "attempt_timeout_s", "min_candidate_remaining_s",
    }
    resolved = {}
    for source_name, source in (("defaults", defaults), ("params", params)):
        for key, value in source.items():
            key = str(key)
            if key in _META_KEYS:
                continue
            if key == "input_dcp" and source_name == "params":
                raise CapabilityError("LLM-provided DCP paths are forbidden")
            if key not in allowed:
                raise CapabilityError(
                    f"hard_macro_move rejects public parameter {key!r}"
                )
            resolved[key] = copy.deepcopy(value)
    for key, default, maximum in (
        ("num_paths", HARD_MACRO_DEFAULT_NUM_PATHS, HARD_MACRO_MAX_NUM_PATHS),
        ("max_candidates", HARD_MACRO_DEFAULT_MAX_CANDIDATES, HARD_MACRO_MAX_CANDIDATES),
        ("max_site_distance", HARD_MACRO_DEFAULT_MAX_SITE_DISTANCE, HARD_MACRO_MAX_SITE_DISTANCE),
    ):
        value = resolved.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise CapabilityError(f"hard_macro_move {key} must be in 1..{maximum}")
        resolved[key] = value
    for key, default in (
        ("attempt_timeout_s", HARD_MACRO_DEFAULT_ATTEMPT_TIMEOUT_S),
        ("min_candidate_remaining_s", 0.0),
    ):
        value = resolved.get(key, default)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0.0 <= float(value) <= 3600.0
            or (key == "attempt_timeout_s" and float(value) <= 0.0)
        ):
            raise CapabilityError(f"hard_macro_move {key} is out of range")
        resolved[key] = float(value)
    concrete, fingerprint = _fingerprint_action("hard_macro_move", "auto", resolved)
    return ResolvedAction(
        skill="hard_macro_move",
        canonical_target="auto",
        execution_target="auto",
        resolved_params=resolved,
        concrete_action=concrete,
        action_fingerprint=fingerprint,
    )


def resolve_skill_action(
    skill: str,
    target: Optional[str],
    params: Optional[dict] = None,
    *,
    defaults: Optional[dict] = None,
    target_presets: Optional[dict] = None,
    cold_isolation: bool = False,
) -> ResolvedAction:
    """Resolve defaults, matched-case semantics, and explicit action params."""
    if not isinstance(skill, str) or not skill.strip():
        raise CapabilityError("skill must be a nonempty name")
    skill = skill.strip()
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise CapabilityError("params must be an object")
    private_params = sorted(str(key) for key in params if str(key).startswith("_"))
    if private_params:
        raise CapabilityError(
            f"caller-supplied private parameters are forbidden: {private_params}"
        )
    if defaults is not None and not isinstance(defaults, dict):
        raise CapabilityError("skill defaults must be an object")
    if target_presets is not None and not isinstance(target_presets, dict):
        raise CapabilityError("target presets must be an object")
    if cold_isolation:
        # Historical/matched presets have no parameter authority in a cold
        # run. Keep only scheduler-owned time windows; the exact action fields
        # must come from the current seed-bound Action Card.
        defaults = {
            str(key): copy.deepcopy(value)
            for key, value in (defaults or {}).items()
            if _is_scheduling_key(str(key))
        }
        cold_target_presets: dict[str, dict[str, dict]] = {}
        for preset_skill, target_map in (target_presets or {}).items():
            if not isinstance(target_map, dict):
                continue
            filtered_targets = {}
            for preset_target, preset_params in target_map.items():
                if not isinstance(preset_params, dict):
                    continue
                scheduling = {
                    str(key): copy.deepcopy(value)
                    for key, value in preset_params.items()
                    if _is_scheduling_key(str(key))
                }
                if scheduling:
                    filtered_targets[str(preset_target)] = scheduling
            if filtered_targets:
                cold_target_presets[str(preset_skill)] = filtered_targets
        target_presets = cold_target_presets

    requested_target = str(target if target is not None else "auto").strip()
    if not requested_target:
        requested_target = "auto"
    requested_flow_mode = params.get(
        "flow_mode", (defaults or {}).get("flow_mode")
    )
    if skill == "fresh_place_route" and requested_flow_mode is not None:
        if requested_flow_mode != SHARED_POST_PLACE_ROUTE_RACE:
            raise CapabilityError(
                f"unknown fresh_place_route flow_mode {requested_flow_mode!r}"
            )
        return _resolve_shared_route_race_action(
            requested_target,
            params,
            defaults or {},
            target_presets or {},
        )
    if skill == "critical_net_reroute" and requested_target in {
        "worst_source", "boundary_link"
    }:
        return _resolve_protected_reroute_action(
            requested_target,
            params,
            defaults or {},
            target_presets or {},
        )
    if skill == "endpoint_bel_move":
        return _resolve_endpoint_action(
            requested_target,
            params,
            defaults or {},
        )
    if skill == "per_net_unroute":
        return _resolve_per_net_unroute_action(
            requested_target,
            params,
            defaults or {},
        )
    if skill == "hard_macro_move":
        return _resolve_hard_macro_move_action(
            requested_target,
            params,
            defaults or {},
        )
    if skill in {"implementation_recipe", "phys_opt"}:
        return _resolve_fixed_preset_action(
            skill,
            requested_target,
            params,
            defaults or {},
            target_presets or {},
        )
    if skill == "selective_branch_reroute":
        return _resolve_selective_branch_action(
            requested_target,
            params,
            defaults or {},
        )
    if skill == "structure_relocation":
        return _resolve_structure_relocation_action(
            requested_target,
            params,
            defaults or {},
        )
    if skill == "equivalent_source_remap":
        return _resolve_equivalent_source_action(
            requested_target,
            params,
            defaults or {},
        )
    if skill == "operator_rewrite":
        return _resolve_operator_rewrite_action(
            requested_target,
            params,
            defaults or {},
        )
    if skill == "semantic_replay" and cold_isolation:
        raise CapabilityError("COLD_ISOLATION_SEMANTIC_REPLAY_FORBIDDEN")
    if skill == "semantic_replay":
        return _resolve_semantic_replay_action(
            requested_target,
            params,
            defaults or {},
        )
    canonical_target = requested_target
    explicit = _normalize_recipe_param(
        skill,
        {key: value for key, value in params.items() if key not in _META_KEYS},
    )
    presets = target_presets or {}

    preset: Optional[dict] = None
    if requested_target != "auto":
        canonical_target, preset = _target_preset(
            skill, requested_target, explicit, presets
        )
        target_is_implemented = (
            skill not in {
                "fresh_place_route",
                "post_route_cleanup",
                "critical_net_reroute",
                "clock_tighten",
                "endpoint_bel_move",
            }
            or preset is not None
        )
        if skill == "endpoint_bel_move":
            target_is_implemented = False
        if not target_is_implemented:
            raise CapabilityError(
                f"{skill} target {requested_target!r} has no implementation semantics"
            )
    else:
        skill_presets = presets.get(skill, {})
        if isinstance(skill_presets, dict) and "auto" in skill_presets:
            preset = copy.deepcopy(skill_presets["auto"])

    preset = _normalize_recipe_param(
        skill,
        {key: value for key, value in (preset or {}).items() if key not in _META_KEYS},
    )
    resolved = copy.deepcopy(defaults or {})
    resolved.update(preset)

    repairs: list[dict] = []
    for key, value in explicit.items():
        if (
            key in preset
            and not _is_scheduling_key(str(key))
            and not _same_typed_value(value, preset[key])
        ):
            repairs.append({
                "kind": "target_locked_parameter",
                "field": str(key),
                "requested": _stable_data(value),
                "canonical": _stable_data(preset[key]),
                "owner": f"{skill}:{canonical_target}",
            })
            continue
        resolved[key] = copy.deepcopy(value)
    resolved = _normalize_recipe_param(skill, resolved)

    if skill == "fresh_place_route" and resolved.get("endpoint_bel_move_after"):
        raise CapabilityError(
            "endpoint_bel_move_after is unsupported; use the standalone "
            "proof-gated endpoint_bel_move action"
        )
    if skill == "critical_net_reroute":
        protected_modes = {
            "worst_source": "worst_source",
            "source": "worst_source",
            "boundary_link": "boundary_link",
        }
        required_target = protected_modes.get(
            str(resolved.get("selection_mode", "")).strip().lower()
        )
        if required_target is not None and canonical_target != required_target:
            raise CapabilityError(
                f"selection_mode={resolved.get('selection_mode')!r} requires "
                f"target={required_target!r}"
            )

    execution_target = canonical_target
    if skill == "pblock" and canonical_target == "auto":
        execution_target = ""
    concrete_action, fingerprint = _fingerprint_action(
        skill, execution_target, resolved
    )
    return ResolvedAction(
        skill=skill,
        canonical_target=canonical_target,
        execution_target=execution_target,
        resolved_params=resolved,
        concrete_action=concrete_action,
        action_fingerprint=fingerprint,
        canonicalization_repairs=tuple(repairs),
    )


def resolve_replay_action(action: ReplayAction) -> ResolvedAction:
    """Resolve one exact code-owned replay template without config mechanics."""
    if not isinstance(action, ReplayAction):
        raise CapabilityError("replay action must be a ReplayAction")
    registered = ACTION_TEMPLATES.get(action.action_id)
    if registered is None or action != registered:
        raise CapabilityError("replay action does not match the code-owned catalog")
    resolved = resolve_skill_action(
        action.skill,
        action.target,
        action.params_dict(),
        defaults={},
        target_presets={},
    )
    if (
        resolved.skill != action.skill
        or resolved.canonical_target != action.target
        or resolved.execution_target != action.target
        or resolved.canonicalization_repairs
    ):
        raise CapabilityError(
            f"replay action {action.action_id!r} did not resolve exactly"
        )
    return resolved
