"""
fresh_place_route skill -- disruptive full re-place/re-route recipe sweep.

This captures the generic Vivado-side manual recipe for HLS/CARRY-heavy
route-dominated designs: unroute, unplace, try timing-driven placement
directives, route with an aggressive timing directive, then run a final
post-route Explore pass.
It deliberately discovers no benchmark-specific instances and applies only
tool-level directives to the current best checkpoint.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from .base import (
    SkillResult,
    calculate_fmax,
    parse_route_status_static,
    parse_timing_summary_static,
)
from .endpoint_bel_move import _endpoint_move_tcl

logger = logging.getLogger(__name__)

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")

DEFAULT_RECIPES = [
    {
        "name": "extranetdelay_no_relax",
        "command": (
            "route_design -unroute; "
            "place_design -unplace; "
            "place_design -directive ExtraNetDelay_high; "
            "phys_opt_design -directive Explore; "
            "route_design -directive NoTimingRelaxation -tns_cleanup; "
            "phys_opt_design -directive Explore"
        ),
    },
    {
        "name": "extratiming_no_relax",
        "command": (
            "route_design -unroute; "
            "place_design -unplace; "
            "place_design -directive ExtraTimingOpt; "
            "phys_opt_design -directive Explore; "
            "route_design -directive NoTimingRelaxation -tns_cleanup; "
            "phys_opt_design -directive Explore"
        ),
    },
    {
        "name": "altspread_explore",
        "command": (
            "route_design -unroute; "
            "place_design -unplace; "
            "place_design -directive AltSpreadLogic_high; "
            "phys_opt_design -directive AggressiveExplore; "
            "route_design -directive Explore -tns_cleanup; "
            "phys_opt_design -directive Explore"
        ),
    },
]

_DEFAULT_RECIPE_BY_NAME = {recipe["name"]: dict(recipe) for recipe in DEFAULT_RECIPES}


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


class FreshPlaceRouteSkill:
    """Try bounded full place/route recipes from current best DCP."""

    name = "fresh_place_route"

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
        recipes = _normalize_recipes(params.get("recipes"))
        max_candidates = max(1, min(int(params.get("max_candidates", 2)), len(recipes)))
        recipe_timeout = float(params.get("recipe_timeout_s", 5400.0))
        endpoint_after = bool(params.get("endpoint_bel_move_after", False))
        endpoint_num_paths = max(1, min(int(params.get("endpoint_num_paths", 20)), 80))
        endpoint_max_candidates = max(
            1, min(int(params.get("endpoint_max_candidates", 6)), 30)
        )
        output_dcp = run_dir / f"fresh_place_route_{safe_target}.dcp"

        if not recipes:
            return SkillResult.failure(before_wns, "no fresh place/route recipes selected", output_dcp)

        recipes = recipes[:max_candidates]
        attempts: list[dict] = []
        best_seen: Optional[dict] = None

        logger.info(
            "[fresh_place_route] target=%r recipes=%d",
            target,
            len(recipes),
        )

        try:
            for idx, recipe in enumerate(recipes, 1):
                name = str(recipe["name"])
                command = str(recipe["command"])
                candidate_dcp = run_dir / (
                    f"fresh_place_route_{idx:02d}_{_safe_name(name)}.dcp"
                )
                attempt = {
                    "name": name,
                    "command": command,
                    "output_dcp": str(candidate_dcp),
                }

                try:
                    await mcp.call_vivado(
                        "open_checkpoint",
                        {"dcp_path": input_dcp},
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
                        after_wns = parsed.get("wns") or before_wns

                    attempt.update(
                        {
                            "after_wns": after_wns,
                            "delta_wns": after_wns - before_wns,
                            "is_legal": is_legal,
                            "route": route,
                        }
                    )

                    if is_legal:
                        if endpoint_after:
                            seed_dcp = run_dir / (
                                f"fresh_place_route_{idx:02d}_{_safe_name(name)}_seed.dcp"
                            )
                            attempt["output_dcp"] = str(seed_dcp)
                        else:
                            seed_dcp = candidate_dcp
                            attempt["output_dcp"] = str(candidate_dcp)

                        await mcp.call_vivado(
                            "write_checkpoint",
                            {"dcp_path": str(seed_dcp.resolve()), "force": True},
                            timeout=600.0,
                        )
                        if best_seen is None or after_wns > best_seen["after_wns"]:
                            best_seen = dict(attempt)

                        if endpoint_after:
                            endpoint_attempts = []
                            for ep_idx in range(endpoint_max_candidates):
                                ep_dcp = run_dir / (
                                    f"fresh_place_route_{idx:02d}_{_safe_name(name)}"
                                    f"_endpoint_{ep_idx + 1:02d}.dcp"
                                )
                                ep_attempt = {
                                    "name": f"{name}+endpoint_{ep_idx + 1:02d}",
                                    "command": command,
                                    "endpoint_candidate_index": ep_idx,
                                    "output_dcp": str(ep_dcp),
                                }
                                try:
                                    await mcp.call_vivado(
                                        "open_checkpoint",
                                        {"dcp_path": str(seed_dcp.resolve())},
                                        timeout=600.0,
                                    )
                                    ep_output = await mcp.call_vivado(
                                        "run_tcl",
                                        {
                                            "command": _endpoint_move_tcl(
                                                ep_idx, endpoint_num_paths
                                            ),
                                            "timeout": recipe_timeout,
                                        },
                                        timeout=recipe_timeout,
                                    )
                                    lowered_output = str(ep_output).lower()
                                    if (
                                        "no endpoint ff bel-move candidate"
                                        in lowered_output
                                        or "endpoint move has no non-clock route nets"
                                        in lowered_output
                                        or "error:" in lowered_output
                                    ):
                                        raise RuntimeError(str(ep_output)[:500])

                                    ep_route_report = await mcp.call_vivado(
                                        "report_route_status", {}, timeout=120.0
                                    )
                                    ep_route = parse_route_status_static(ep_route_report)
                                    ep_legal = bool(ep_route.get("routed_ok"))
                                    ep_wns = await mcp.get_wns()
                                    if ep_wns is None:
                                        ts = await mcp.call_vivado(
                                            "report_timing_summary", {}, timeout=300.0
                                        )
                                        parsed = parse_timing_summary_static(ts)
                                        ep_wns = parsed.get("wns") or after_wns

                                    ep_attempt.update(
                                        {
                                            "after_wns": ep_wns,
                                            "delta_wns": ep_wns - before_wns,
                                            "is_legal": ep_legal,
                                            "route": ep_route,
                                        }
                                    )

                                    if ep_legal:
                                        await mcp.call_vivado(
                                            "write_checkpoint",
                                            {
                                                "dcp_path": str(ep_dcp.resolve()),
                                                "force": True,
                                            },
                                            timeout=600.0,
                                        )
                                        if (
                                            best_seen is None
                                            or ep_wns > best_seen["after_wns"]
                                        ):
                                            best_seen = dict(ep_attempt)

                                    logger.info(
                                        "[fresh_place_route] %s endpoint idx=%d "
                                        "legal=%s wns %.3f delta=%+.3f",
                                        name,
                                        ep_idx,
                                        ep_legal,
                                        ep_wns,
                                        ep_wns - before_wns,
                                    )
                                except Exception as e:
                                    ep_attempt.update({"error": str(e), "is_legal": False})
                                    logger.info(
                                        "[fresh_place_route] %s endpoint idx=%d skipped: %s",
                                        name,
                                        ep_idx,
                                        e,
                                    )

                                endpoint_attempts.append(ep_attempt)

                            attempt["endpoint_attempts"] = endpoint_attempts

                    logger.info(
                        "[fresh_place_route] %s legal=%s wns %.3f delta=%+.3f",
                        name,
                        is_legal,
                        after_wns,
                        after_wns - before_wns,
                    )
                except Exception as e:
                    attempt.update({"error": str(e), "is_legal": False})
                    logger.warning("[fresh_place_route] %s failed: %s", name, e)

                attempts.append(attempt)

            if best_seen is None:
                return SkillResult.failure(
                    before_wns,
                    f"no legal fresh place/route candidates; attempts={attempts}",
                    output_dcp,
                )

            output_dcp = run_dir / str(best_seen["output_dcp"]).rsplit("/", 1)[-1]
            after_wns = float(best_seen["after_wns"])
            await mcp.call_vivado(
                "open_checkpoint",
                {"dcp_path": str(output_dcp.resolve())},
                timeout=600.0,
            )

            fmax_b = calculate_fmax(before_wns, clock_period)
            fmax_a = calculate_fmax(after_wns, clock_period)
            name = best_seen["name"]
            if fmax_b and fmax_a:
                summary = (
                    f"fresh_place_route:{name} wns {before_wns:.3f}->{after_wns:.3f} "
                    f"fmax {fmax_b:.1f}->{fmax_a:.1f} MHz"
                )
            else:
                summary = (
                    f"fresh_place_route:{name} wns {before_wns:.3f}->{after_wns:.3f} "
                    f"delta={after_wns - before_wns:+.3f}"
                )

            return SkillResult(
                success=True,
                before_wns=before_wns,
                after_wns=after_wns,
                delta_wns=after_wns - before_wns,
                is_legal=True,
                output_dcp=output_dcp,
                summary=summary,
                details={"attempts": attempts, "best": best_seen},
            )

        except Exception as e:
            logger.error("[fresh_place_route] failed: %s", e)
            return SkillResult.failure(before_wns, str(e), output_dcp)
