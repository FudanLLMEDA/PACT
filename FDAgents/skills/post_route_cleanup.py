"""
post_route_cleanup skill — generic post-route timing cleanup recipe sweep.

This skill is intentionally profile/feedback driven rather than benchmark
specific. It starts from the current best routed checkpoint, independently
tries a small set of conservative post-route Vivado recipes, keeps only legal
routed checkpoints, and greedily repeats from the best improved checkpoint.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Optional

from .base import (
    SkillOutput,
    SkillResult,
    candidate_budget_exhausted,
    calculate_fmax,
    parse_route_status_static,
    parse_timing_summary_static,
)

logger = logging.getLogger(__name__)

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")

# Match clock_tighten: the MCP call is given a little more than the Tcl budget
# so a clean tool-side timeout is reported rather than a transport one, and a
# pass below the floor cannot reach a checkpoint, so starting it only converts
# remaining window into a timeout.  The floor is measured: `phys_critical_pin`
# took 270s on ispd16 and `phys_routing` about 240s on corescore.
_MCP_RUN_TCL_TIMEOUT_GRACE_S = 120.0
_MIN_RECIPE_TIMEOUT_S = 240.0

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
    if isinstance(raw_recipes, dict):
        raw_items = [raw_recipes]
    elif isinstance(raw_recipes, str):
        raw_items = [item.strip() for item in raw_recipes.split(",")]
    else:
        raw_items = list(raw_recipes)

    for item in raw_items:
        if not item:
            continue
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            command = str(item.get("command") or "").strip()
            if not name or not command:
                raise ValueError("custom recipes require nonempty name and command")
            normalized.append({"name": _safe_name(name), "command": command})
            continue

        name_or_command = str(item).strip()
        if name_or_command in _DEFAULT_RECIPE_BY_NAME:
            normalized.append(dict(_DEFAULT_RECIPE_BY_NAME[name_or_command]))
        else:
            raise ValueError(
                f"unknown post_route_cleanup recipe {name_or_command!r}; "
                "custom Tcl requires a recipe object with name and command"
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
        # Every source of recipe_timeout_s here is a static default, so it
        # states no per-action intent and must not cap the window the
        # scheduler granted.  It is the fallback for when no deadline arrives.
        recipe_timeout = float(params.get("recipe_timeout_s", 3600.0))
        try:
            execution_deadline = params.get("_execution_deadline_monotonic")
            execution_deadline = (
                None if execution_deadline is None else float(execution_deadline)
            )
        except (TypeError, ValueError):
            execution_deadline = None
        try:
            commit_reserve_s = float(params.get("_candidate_commit_reserve_s") or 0.0)
        except (TypeError, ValueError):
            commit_reserve_s = 0.0

        if not recipes:
            return SkillResult.failure(
                before_wns,
                "no cleanup recipes selected",
                output_dcp,
                details={
                    "attempts": [],
                    "candidates": [],
                    "budget_stopped": False,
                    "unstarted": [],
                },
            )

        recipes = recipes[:max_candidates]
        attempts: list[dict] = []
        feedback_attempts: list[dict] = []
        candidate_feedback: list[dict] = []
        candidate_outputs: list[SkillOutput] = []
        accepted: list[dict] = []
        current_dcp = str(input_dcp)
        current_wns = before_wns
        best_seen: Optional[dict] = None
        budget_stopped = False
        unstarted: list[str] = []
        stop_sweep = False

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
                    label = (
                        f"post_route_cleanup recipe={name} variant={idx:02d}"
                    )[:120]
                    if candidate_budget_exhausted(
                        params, completed_attempts=len(feedback_attempts)
                    ):
                        budget_stopped = True
                        unstarted = [
                            (
                                "post_route_cleanup "
                                f"iteration={future_iteration:02d} "
                                f"recipe={future_recipe['name']}"
                            )[:120]
                            for future_iteration in range(iteration, max_iterations + 1)
                            for future_recipe in (
                                recipes[idx - 1 :]
                                if future_iteration == iteration
                                else recipes
                            )
                        ]
                        stop_sweep = True
                        break
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
                    # Stop while there is still time to keep what the sweep has
                    # already won.  Counting attempts does not bound wall clock,
                    # so a two-recipe sweep ran ispd16 past the run's own
                    # deadline and returned nothing at all from a 2,462s window.
                    attempt_timeout = recipe_timeout
                    if execution_deadline is not None:
                        attempt_timeout = (
                            execution_deadline
                            - time.monotonic()
                            - commit_reserve_s
                            - _MCP_RUN_TCL_TIMEOUT_GRACE_S
                        )
                    if attempt_timeout < _MIN_RECIPE_TIMEOUT_S:
                        logger.info(
                            "[post_route_cleanup] %s skipped: execution window "
                            "%.0fs is below %.0fs",
                            name, attempt_timeout, _MIN_RECIPE_TIMEOUT_S,
                        )
                        budget_stopped = True
                        stop_sweep = True
                        break
                    attempt["recipe_timeout_s"] = round(attempt_timeout, 3)
                    started = time.monotonic()

                    try:
                        await mcp.call_vivado(
                            "open_checkpoint",
                            {"dcp_path": current_dcp},
                            timeout=600.0,
                        )
                        await mcp.call_vivado(
                            "run_tcl",
                            {"command": command, "timeout": attempt_timeout},
                            timeout=attempt_timeout + _MCP_RUN_TCL_TIMEOUT_GRACE_S,
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
                    feedback = {
                        "label": label,
                        "status": (
                            "legal" if attempt.get("is_legal") else
                            "error" if "error" in attempt else "illegal"
                        ),
                        "local_metrics": {
                            "wns": attempt.get("after_wns"),
                            "delta_wns": attempt.get("total_delta_wns"),
                        },
                        "runtime_s": time.monotonic() - started,
                    }
                    feedback_attempts.append(feedback)
                    if iteration == 1 and attempt.get("is_legal"):
                        candidate_outputs.append(SkillOutput(candidate_dcp, label))
                        candidate_feedback.append(feedback)

                if stop_sweep:
                    break
                if iteration_best is None:
                    break

                iteration_delta = iteration_best["after_wns"] - current_wns
                if iteration_delta <= accept_min_delta:
                    break

                accepted.append(iteration_best)
                current_dcp = iteration_best["output_dcp"]
                current_wns = iteration_best["after_wns"]

            if best_seen is None:
                error = "no legal post-route cleanup candidates"
                return SkillResult(
                    success=False,
                    before_wns=before_wns,
                    after_wns=before_wns,
                    delta_wns=0.0,
                    is_legal=False,
                    output_dcp=output_dcp,
                    summary=f"FAILED: {error}",
                    error_msg=error,
                    details={
                        "attempts": feedback_attempts,
                        "candidates": candidate_feedback,
                        "budget_stopped": budget_stopped,
                        "unstarted": unstarted,
                        "ancestry": "iterations after 1 descend from the prior local winner",
                    },
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
            if budget_stopped:
                summary += f" budget_stop {len(feedback_attempts)} attempts"

            return SkillResult(
                success=True,
                before_wns=before_wns,
                after_wns=after_wns,
                delta_wns=delta,
                is_legal=True,
                output_dcp=output_dcp,
                summary=summary,
                details={
                    "attempts": feedback_attempts,
                    "candidates": candidate_feedback,
                    "budget_stopped": budget_stopped,
                    "unstarted": unstarted,
                    "ancestry": "iterations after 1 descend from the prior local winner",
                },
                candidates=tuple(candidate_outputs),
            )

        except Exception as e:
            logger.error("[post_route_cleanup] failed: %s", e)
            return SkillResult.failure(
                before_wns,
                str(e),
                output_dcp,
                details={
                    "attempts": feedback_attempts,
                    "candidates": candidate_feedback,
                    "budget_stopped": budget_stopped,
                    "unstarted": unstarted,
                    "ancestry": "iterations after 1 descend from the prior local winner",
                },
            )
