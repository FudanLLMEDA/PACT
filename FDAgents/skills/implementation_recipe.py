"""Typed implementation mechanisms compiled into current-seed backend plans."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, Optional

from .. import tcl_recipes
from .base import SkillResult
from .clock_tighten import ClockTightenSkill
from .fresh_place_route import FreshPlaceRouteSkill
from .post_route_cleanup import PostRouteCleanupSkill


class ImplementationRecipePreset(str, Enum):
    DERIVED_PRESSURE_REPLACE_ROUTE = "derived_pressure_replace_route_v1"
    FULL_REIMPLEMENTATION_RETIME = "full_reimplementation_retime_v1"
    ROUTED_CRITICAL_CLEANUP = "routed_critical_cleanup_v1"


@dataclass(frozen=True)
class ImplementationRecipeSpec:
    backend_skill: str
    builder: Callable[..., str]
    builder_kwargs: Mapping[str, object]
    backend_params: Mapping[str, object]
    mechanism_summary: str
    destructive_scope: str


def _spec(
    backend_skill: str,
    builder: Callable[..., str],
    *,
    builder_kwargs: Optional[dict] = None,
    backend_params: Optional[dict] = None,
    mechanism_summary: str,
    destructive_scope: str,
) -> ImplementationRecipeSpec:
    def freeze(value):
        if isinstance(value, list):
            return tuple(freeze(item) for item in value)
        if isinstance(value, dict):
            return MappingProxyType({key: freeze(item) for key, item in value.items()})
        return value

    return ImplementationRecipeSpec(
        backend_skill=backend_skill,
        builder=builder,
        builder_kwargs=MappingProxyType({
            key: freeze(value) for key, value in (builder_kwargs or {}).items()
        }),
        backend_params=MappingProxyType({
            key: freeze(value) for key, value in (backend_params or {}).items()
        }),
        mechanism_summary=str(mechanism_summary),
        destructive_scope=str(destructive_scope),
    )


_registry: dict[str, ImplementationRecipeSpec] = {
    "derived_pressure_replace_route_v1": _spec(
        "clock_tighten",
        tcl_recipes.high_util_target_period_fast_route_command,
        backend_params={
            "recipe_timeout_s": 3000,
            "recover_partial_candidate_on_timeout": True,
            "stop_after_first_legal": True,
            "max_fmax_mhz": 0.0, "min_remaining_s": 2400,
        },
        mechanism_summary=(
            "derive temporary pressure periods from the current measured "
            "achievable period, then re-place and re-route with critical-net "
            "replication and AlternateCLBRouting"
        ),
        destructive_scope="full_unplace_unroute",
    ),
    "full_reimplementation_retime_v1": _spec(
        "fresh_place_route",
        tcl_recipes.full_unplace_retime_route_command,
        backend_params={"max_candidates": 1, "recipe_timeout_s": 2400,
                        "min_remaining_s": 1800},
        mechanism_summary=(
            "fully unplace and unroute the current seed, run Explore and "
            "ExtraTimingOpt, then bounded retiming/physical optimization and "
            "an alternate-route fallback"
        ),
        destructive_scope="full_unplace_unroute",
    ),
    "routed_critical_cleanup_v1": _spec(
        "post_route_cleanup",
        tcl_recipes.clockopt_criticalpin_cleanup_command,
        backend_params={"max_candidates": 1, "max_iterations": 1,
                        "recipe_timeout_s": 1200, "min_remaining_s": 600},
        mechanism_summary=(
            "retain the routed incumbent while applying clock, critical-pin, "
            "critical-cell and placement cleanup with one bounded reroute"
        ),
        destructive_scope="routed_cleanup_with_bounded_reroute",
    ),
}

IMPLEMENTATION_RECIPE_REGISTRY: Mapping[str, ImplementationRecipeSpec] = (
    MappingProxyType(_registry)
)
IMPLEMENTATION_RECIPE_TARGETS = tuple(item.value for item in ImplementationRecipePreset)

_SCHEDULING_PARAMS = {"min_remaining_s", "min_candidate_remaining_s"}
_STRATEGY_PARAMS = {
    "strategy_regime",
    "pressure_factors",
    "max_candidates",
    "route_directives",
    "cleanup_recipe",
}
_INTERNAL_SCHEDULING_PARAMS = {
    "input_dcp",
    "_execution_deadline_monotonic",
    "_candidate_commit_reserve_s",
}


def is_scheduling_param(name: str) -> bool:
    return name in _SCHEDULING_PARAMS or name.endswith("_timeout_s")


def is_strategy_param(name: str) -> bool:
    """Return whether current-seed parameterization owns this public field."""
    return name in _STRATEGY_PARAMS


def _is_execution_param(name: str) -> bool:
    # A leading underscore marks context the framework injects on its own
    # behalf, never a parameter a menu selection can carry; see
    # unexpected_execution_params in .base.  Only rejection is relaxed here --
    # what gets forwarded to the backend is decided separately below.
    return (
        name.startswith("_")
        or is_scheduling_param(name)
        or is_strategy_param(name)
        or name in _INTERNAL_SCHEDULING_PARAMS
    )


def build_implementation_recipe(target: str) -> str:
    """Build trusted Tcl for one exact enum target."""
    spec = IMPLEMENTATION_RECIPE_REGISTRY.get(target)
    if spec is None:
        raise ValueError(f"unknown implementation_recipe preset {target!r}")
    return spec.builder(**dict(spec.builder_kwargs))


def implementation_recipe_catalog() -> dict[str, dict[str, object]]:
    """Public catalog without Tcl or user-selectable backend mechanics."""
    return {
        target: {
            "backend": spec.backend_skill,
            "mechanism_summary": spec.mechanism_summary,
            "destructive_scope": spec.destructive_scope,
            "fixed_recipe_args": copy.deepcopy(dict(spec.builder_kwargs)),
            "fixed_params": {
                key: copy.deepcopy(value)
                for key, value in spec.backend_params.items()
                if not is_scheduling_param(key)
            },
            "scheduling": {
                key: copy.deepcopy(value)
                for key, value in spec.backend_params.items()
                if is_scheduling_param(key)
            },
        }
        for target, spec in IMPLEMENTATION_RECIPE_REGISTRY.items()
    }


class ImplementationRecipeSkill:
    """Compile typed current-seed strategy fields into an existing backend."""

    name = "implementation_recipe"

    def __init__(self, backends: Optional[Mapping[str, object]] = None):
        self._backends = dict(backends or {
            "clock_tighten": ClockTightenSkill(),
            "fresh_place_route": FreshPlaceRouteSkill(),
            "post_route_cleanup": PostRouteCleanupSkill(),
        })

    async def execute(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        clock_period: Optional[float],
    ) -> SkillResult:
        spec = IMPLEMENTATION_RECIPE_REGISTRY.get(target)
        output_dcp = Path(mcp.run_dir) / f"implementation_recipe_{target}.dcp"
        if spec is None:
            return SkillResult.failure(
                before_wns, f"unknown implementation_recipe preset {target!r}", output_dcp
            )
        unknown = sorted(key for key in params if not _is_execution_param(str(key)))
        if unknown:
            return SkillResult.failure(
                before_wns,
                f"implementation_recipe parameters are fixed; rejected {unknown}",
                output_dcp,
            )

        backend = self._backends[spec.backend_skill]
        backend_params = copy.deepcopy(dict(spec.backend_params))
        for key, value in params.items():
            if is_scheduling_param(str(key)) or str(key) in _INTERNAL_SCHEDULING_PARAMS:
                backend_params[key] = copy.deepcopy(value)
        regime = str(params.get("strategy_regime") or "balanced")
        if regime not in {"preserve", "balanced", "rebuild"}:
            return SkillResult.failure(
                before_wns,
                f"implementation_recipe rejected strategy_regime {regime!r}",
                output_dcp,
            )
        max_candidates = params.get("max_candidates", 1)
        if (
            isinstance(max_candidates, bool)
            or not isinstance(max_candidates, int)
            or not 1 <= max_candidates <= 4
        ):
            return SkillResult.failure(
                before_wns,
                "implementation_recipe max_candidates must be in 1..4",
                output_dcp,
            )
        if spec.backend_skill == "clock_tighten":
            factors = params.get("pressure_factors")
            if (
                not isinstance(factors, (list, tuple))
                or not 1 <= len(factors) <= 3
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not 0.5 <= float(value) < 1.0
                    for value in factors
                )
            ):
                return SkillResult.failure(
                    before_wns,
                    "implementation_recipe requires 1..3 current-seed pressure_factors",
                    output_dcp,
                )
            backend_params["factors"] = [float(value) for value in factors]
            backend_params["max_candidates"] = min(max_candidates, len(factors))
        elif spec.backend_skill == "fresh_place_route":
            factors = params.get("pressure_factors")
            if (
                not isinstance(factors, (list, tuple))
                or not 1 <= len(factors) <= 3
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not 0.5 <= float(value) < 1.0
                    for value in factors
                )
            ):
                return SkillResult.failure(
                    before_wns,
                    "implementation_recipe requires 1..3 current-seed pressure_factors",
                    output_dcp,
                )
            route_directives = params.get("route_directives")
            if (
                not isinstance(route_directives, (list, tuple))
                or not route_directives
                or any(not isinstance(value, str) for value in route_directives)
            ):
                return SkillResult.failure(
                    before_wns,
                    "implementation_recipe requires typed route_directives",
                    output_dcp,
                )
            backend_params.update({
                "pressure_regime": {
                    "preserve": "preserve_constraints",
                    "balanced": "balanced",
                    "rebuild": "aggressive",
                }[regime],
                "pressure_factors": [float(value) for value in factors],
                "route_directives": list(route_directives),
                "max_candidates": max_candidates,
            })
        elif spec.backend_skill == "post_route_cleanup":
            cleanup_recipe = params.get("cleanup_recipe")
            allowed_cleanup = {
                "phys_routing",
                "phys_explore",
                "phys_critical_pin",
                "phys_aggr_explore",
                "route_higher_tns_phys_routing",
                "route_aggr_tns_phys_explore",
            }
            if cleanup_recipe not in allowed_cleanup:
                return SkillResult.failure(
                    before_wns,
                    "implementation_recipe requires an allowlisted current-seed cleanup_recipe",
                    output_dcp,
                )
            backend_params.update({
                "recipes": [str(cleanup_recipe)],
                "max_candidates": 1,
                "max_iterations": 1,
            })
        else:  # pragma: no cover - registry construction owns this invariant
            return SkillResult.failure(
                before_wns,
                f"implementation_recipe backend {spec.backend_skill!r} is unsupported",
                output_dcp,
            )
        return await backend.execute(
            mcp, target, backend_params, before_wns, clock_period
        )
