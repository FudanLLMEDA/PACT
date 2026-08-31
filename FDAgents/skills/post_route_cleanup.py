"""
post_route_cleanup skill — generic post-route timing cleanup recipe sweep.

This skill is intentionally profile/feedback driven rather than benchmark
specific. It starts from the current best routed checkpoint, independently
tries a small set of conservative post-route Vivado recipes, keeps only legal
routed checkpoints, and greedily repeats from the best improved checkpoint.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

from .base import (
    SkillResult,
    calculate_fmax,
    parse_route_status_static,
    parse_timing_summary_static,
)

logger = logging.getLogger(__name__)

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")

DEFAULT_RECIPES = [
    {
        "name": "phys_routing",
        "command": "phys_opt_design -routing_opt",
    },
    {
        "name": "phys_explore",
        "command": "phys_opt_design -directive Explore",
    },
    {
        "name": "route_explore_phys_explore",
        "command": "route_design -directive Explore; phys_opt_design -directive Explore",
    },
    {
        "name": "route_aggr_phys_explore",
        "command": (
            "route_design -directive AggressiveExplore; "
            "phys_opt_design -directive Explore"
        ),
    },
    {
        "name": "route_higher_tns_phys_explore",
        "command": (
            "route_design -directive HigherDelayCost -tns_cleanup; "
            "phys_opt_design -directive Explore"
        ),
    },
    {
        "name": "route_higher_tns_phys_routing",
        "command": (
            "route_design -directive HigherDelayCost -tns_cleanup; "
            "phys_opt_design -routing_opt"
        ),
    },
    {
        "name": "route_moreglobal_tns_phys_explore",
        "command": (
            "route_design -directive MoreGlobalIterations -tns_cleanup; "
            "phys_opt_design -directive Explore"
        ),
    },
    {
        "name": "route_aggr_tns_phys_explore",
        "command": (
            "route_design -directive AggressiveExplore -tns_cleanup; "
            "phys_opt_design -directive Explore"
        ),
    },
    {
        "name": "phys_critical_pin",
        "command": "phys_opt_design -critical_pin_opt",
    },
    {
        "name": "phys_aggr_explore",
        "command": "phys_opt_design -directive AggressiveExplore",
    },
]

_DEFAULT_RECIPE_BY_NAME = {
    str(recipe["name"]): dict(recipe) for recipe in DEFAULT_RECIPES
}


def _safe_name(value: str) -> str:
    return _SAFE_NAME_RE.sub("_", value).strip("_") or "recipe"


def _normalize_recipes(raw_recipes) -> list[dict[str, str]]:
    if raw_recipes is None:
        return [dict(recipe) for recipe in DEFAULT_RECIPES]

    normalized: list[dict[str, str]] = []
    if isinstance(raw_recipes, str):
        raw_items = [item.strip() for item in raw_recipes.split(",")]
    else:
        raw_items = list(raw_recipes)

    for idx, item in enumerate(raw_items, 1):
        if not item:
            continue
        if isinstance(item, dict):
            command = str(item.get("command") or "").strip()
            if not command:
                continue
            name = str(item.get("name") or f"custom_{idx}").strip()
            normalized.append({"name": _safe_name(name), "command": command})
            continue

        name_or_command = str(item).strip()
        if name_or_command in _DEFAULT_RECIPE_BY_NAME:
            normalized.append(dict(_DEFAULT_RECIPE_BY_NAME[name_or_command]))
        else:
            normalized.append(
                {
                    "name": _safe_name(name_or_command)[:80],
                    "command": name_or_command,
                }
            )

    return normalized


class PostRouteCleanupSkill:
    """Sweep generic post-route cleanup Tcl recipes from current best DCP."""

    name = "post_route_cleanup"

    async def execute(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        clock_period: Optional[float],
    ) -> SkillResult:
        run_dir = mcp.run_dir
        input_dcp = params.get("input_dcp") or str(run_dir / "current_best.dcp")
        safe_target = _safe_name(target or "auto")
        output_dcp = run_dir / f"post_route_cleanup_{safe_target}.dcp"
        recipes = _normalize_recipes(params.get("recipes"))
        max_candidates = max(1, min(int(params.get("max_candidates", 8)), len(recipes)))
        max_iterations = max(1, min(int(params.get("max_iterations", 2)), 3))
        accept_min_delta = float(params.get("accept_min_delta", 0.0005))
        recipe_timeout = float(params.get("recipe_timeout_s", 3600.0))

        if not recipes:
            return SkillResult.failure(before_wns, "no cleanup recipes selected", output_dcp)

        recipes = recipes[:max_candidates]
        attempts: list[dict] = []
        accepted: list[dict] = []
        current_dcp = str(input_dcp)
        current_wns = before_wns
        best_seen: Optional[dict] = None

        logger.info(
            "[post_route_cleanup] target=%r recipes=%d iterations=%d",
            target,
            len(recipes),
            max_iterations,
        )

        try:
            for iteration in range(1, max_iterations + 1):
                iteration_best: Optional[dict] = None

                for idx, recipe in enumerate(recipes, 1):
                    name = str(recipe["name"])
                    command = str(recipe["command"])
                    candidate_dcp = run_dir / (
                        f"post_route_cleanup_iter{iteration:02d}_{idx:02d}_{_safe_name(name)}.dcp"
                    )
                    attempt = {
                        "iteration": iteration,
                        "name": name,
                        "command": command,
                        "input_dcp": current_dcp,
                        "output_dcp": str(candidate_dcp),
                    }

                    try:
                        await mcp.call_vivado(
                            "open_checkpoint",
                            {"dcp_path": current_dcp},
                            timeout=600.0,
                        )
                        await mcp.call_vivado(
                            "run_tcl",
                            {"command": command, "timeout": recipe_timeout},
                            timeout=recipe_timeout,
                        )

                        route_report = await mcp.call_vivado(
                            "report_route_status", {}, timeout=120.0
                        )
                        route = parse_route_status_static(route_report)
                        is_legal = bool(route.get("routed_ok"))

                        after_wns = await mcp.get_wns()
                        if after_wns is None:
                            ts = await mcp.call_vivado(
                                "report_timing_summary", {}, timeout=300.0
                            )
                            parsed = parse_timing_summary_static(ts)
                            after_wns = parsed.get("wns") or current_wns

                        await mcp.call_vivado(
                            "write_checkpoint",
                            {"dcp_path": str(candidate_dcp.resolve()), "force": True},
                            timeout=600.0,
                        )

                        attempt.update(
                            {
                                "after_wns": after_wns,
                                "delta_wns": after_wns - current_wns,
                                "total_delta_wns": after_wns - before_wns,
                                "is_legal": is_legal,
                                "route": route,
                            }
                        )

                        if is_legal and (
                            iteration_best is None
                            or after_wns > iteration_best["after_wns"]
                        ):
                            iteration_best = attempt
                        if is_legal and (
                            best_seen is None
                            or after_wns > best_seen["after_wns"]
                        ):
                            best_seen = attempt

                    except Exception as e:
                        attempt.update({"error": str(e), "is_legal": False})

                    attempts.append(attempt)

                if iteration_best is None:
                    break

                iteration_delta = iteration_best["after_wns"] - current_wns
                if iteration_delta <= accept_min_delta:
                    break

                accepted.append(iteration_best)
                current_dcp = iteration_best["output_dcp"]
                current_wns = iteration_best["after_wns"]

            if best_seen is None:
                return SkillResult.failure(
                    before_wns,
                    "no legal post-route cleanup candidates: " + json.dumps(attempts)[:300],
                    output_dcp,
                )

            final_attempt = accepted[-1] if accepted else best_seen
            output_dcp = Path(final_attempt["output_dcp"])
            after_wns = float(final_attempt["after_wns"])
            delta = after_wns - before_wns

            # Leave Vivado loaded with the checkpoint represented by this result
            # so a following accepted optimization starts from the same state.
            await mcp.call_vivado(
                "open_checkpoint",
                {"dcp_path": str(output_dcp)},
                timeout=600.0,
            )

            fmax_b = calculate_fmax(before_wns, clock_period)
            fmax_a = calculate_fmax(after_wns, clock_period)
            if fmax_b and fmax_a:
                summary = (
                    f"post_route_cleanup best={final_attempt['name']} "
                    f"iters={len(accepted)} wns {before_wns:.3f}->{after_wns:.3f} "
                    f"fmax {fmax_b:.1f}->{fmax_a:.1f} MHz delta={delta:+.3f}"
                )
            else:
                summary = (
                    f"post_route_cleanup best={final_attempt['name']} "
                    f"iters={len(accepted)} wns {before_wns:.3f}->{after_wns:.3f} "
                    f"delta={delta:+.3f}"
                )

            return SkillResult(
                success=True,
                before_wns=before_wns,
                after_wns=after_wns,
                delta_wns=delta,
                is_legal=True,
                output_dcp=output_dcp,
                summary=summary,
                details=json.dumps(
                    {"accepted": accepted, "attempts": attempts},
                    default=str,
                )[:4000],
            )

        except Exception as e:
            logger.error("[post_route_cleanup] failed: %s", e)
            return SkillResult.failure(before_wns, str(e), output_dcp)
