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
import time
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Optional

from .base import (
    SkillOutput,
    SkillResult,
    calculate_fmax,
    candidate_budget_exhausted,
    continuation_opportunity_would_be_lost,
    parse_route_status_static,
    parse_timing_summary_static,
)
from .endpoint_bel_move import _endpoint_move_tcl
from .phys_opt import PHYS_OPT_PRESET_ARGS

logger = logging.getLogger(__name__)

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")

SHARED_POST_PLACE_ROUTE_RACE = "shared_post_place_route_race"
PRESSURE_PERIOD_MIN_NS = 0.5
PRESSURE_PERIOD_MAX_NS = 100.0
ROUTE_DIRECTIVE_ALLOWLIST = (
    "Default",
    "Explore",
    "AggressiveExplore",
    "NoTimingRelaxation",
    "MoreGlobalIterations",
    "HigherDelayCost",
    "AdvancedSkewModeling",
    "AlternateCLBRouting",
    "RuntimeOptimized",
    "Quick",
)


@dataclass(frozen=True, slots=True)
class PostPlaceRecipe:
    place_directive: str
    phys_opt_preset: str


POST_PLACE_RECIPE_REGISTRY = MappingProxyType({
    "extranetdelay_explore": PostPlaceRecipe("ExtraNetDelay_high", "Explore"),
    "extratiming_explore": PostPlaceRecipe("ExtraTimingOpt", "Explore"),
    "altspread_aggressive": PostPlaceRecipe(
        "AltSpreadLogic_high", "AggressiveExplore"
    ),
})

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


def _tcl_braced_path(path: Path) -> str:
    value = str(Path(path).resolve())
    if "}" in value:
        raise ValueError("timing XDC path contains an unsupported closing brace")
    return "{" + value + "}"


def _canonical_timing_xdc(path: Path) -> str:
    commands = []
    for line in path.read_text(encoding="utf-8", errors="strict").splitlines():
        normalized = line.strip()
        if not normalized or normalized.startswith("#"):
            continue
        commands.append(normalized)
    return "\n".join(commands)


async def _export_timing_xdc(mcp, path: Path) -> str:
    """Export the live non-physical timing constraints for stage rollback."""
    output = Path(path).resolve()
    output.unlink(missing_ok=True)
    await mcp.call_vivado(
        "run_tcl",
        {
            "command": (
                "write_xdc -force -exclude_physical "
                + _tcl_braced_path(output)
            ),
            "timeout": 300.0,
        },
        timeout=300.0,
    )
    if output.is_symlink() or not output.is_file() or output.stat().st_size <= 0:
        raise RuntimeError("fresh place/route timing XDC export was not created")
    return _canonical_timing_xdc(output)


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
                f"unknown fresh_place_route recipe {name_or_command!r}; "
                "custom Tcl requires a recipe object with name and command"
            )

    return normalized


def _strategy_recipes(
    regime: str,
    route_directives: object,
    *,
    route_tns_cleanup: bool = True,
) -> list[dict[str, str]]:
    """Compile one typed current-seed strategy without accepting raw Tcl."""
    if regime not in {"preserve_constraints", "balanced", "aggressive"}:
        raise ValueError(f"unknown fresh-place strategy regime {regime!r}")
    if not isinstance(route_directives, (list, tuple)) or not route_directives:
        raise ValueError("strategy route_directives must be a nonempty list")
    place_directive, phys_directive = {
        "preserve_constraints": ("Default", "Explore"),
        "balanced": ("ExtraTimingOpt", "Explore"),
        "aggressive": ("ExtraNetDelay_high", "AggressiveExplore"),
    }[regime]
    recipes = []
    cleanup_option = " -tns_cleanup" if route_tns_cleanup else ""
    for directive in route_directives:
        if not isinstance(directive, str) or directive not in ROUTE_DIRECTIVE_ALLOWLIST:
            raise ValueError(f"route directive {directive!r} is not allowlisted")
        name = _safe_name(f"{regime}_{directive}")
        recipes.append({
            "name": name,
            "command": (
                "route_design -unroute; place_design -unplace; "
                f"place_design -directive {place_directive}; "
                f"phys_opt_design -directive {phys_directive}; "
                f"route_design -directive {directive}{cleanup_option}; "
                "phys_opt_design -directive Explore"
            ),
        })
    return recipes


def _normalize_pressure_periods(params: dict) -> list[float]:
    """Parse optional clock-pressure periods from skill params."""
    raw = params.get("pressure_periods")
    if raw is None:
        raw = params.get("pressure_period_ns")
    if raw is None:
        return []
    if isinstance(raw, str):
        items = [item.strip() for item in raw.split(",") if item.strip()]
    elif isinstance(raw, (int, float)):
        items = [raw]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        raise ValueError("pressure periods must be a number, string, or list")

    periods: list[float] = []
    seen: set[float] = set()
    for item in items:
        period = round(float(item), 3)
        if not (PRESSURE_PERIOD_MIN_NS <= period <= PRESSURE_PERIOD_MAX_NS):
            raise ValueError(
                f"pressure period {period} ns outside "
                f"[{PRESSURE_PERIOD_MIN_NS}, {PRESSURE_PERIOD_MAX_NS}]"
            )
        if period not in seen:
            seen.add(period)
            periods.append(period)
    return periods


def build_pressure_wrapped_command(
    command: str,
    pressure_period_ns: float,
    clock_name: str,
    official_xdc_path: str,
) -> str:
    """Wrap a recipe so it runs under a temporary realistic clock period.

    The wrapper snapshots the checkpoint's official constraints, retargets the
    contest clock to ``pressure_period_ns`` for placement/routing, then removes
    the temporary constraint via reset_timing + read_xdc so the emitted DCP is
    measured and written under the exact official clock.
    """
    period = round(float(pressure_period_ns), 3)
    if not (PRESSURE_PERIOD_MIN_NS <= period <= PRESSURE_PERIOD_MAX_NS):
        raise ValueError(
            f"pressure period {period} ns outside "
            f"[{PRESSURE_PERIOD_MIN_NS}, {PRESSURE_PERIOD_MAX_NS}]"
        )
    prologue = [
        f"set fdagents_pressure_xdc {{{official_xdc_path}}}",
        "write_xdc -force $fdagents_pressure_xdc",
        f"set fdagents_pclk [get_clocks -quiet {{{clock_name}}}]",
        "if {$fdagents_pclk eq {}} { set fdagents_pclk [lindex [get_clocks -quiet] 0] }",
        "if {$fdagents_pclk eq {}} { error {no clock found for pressure wrap} }",
        "set fdagents_pcname [get_property NAME $fdagents_pclk]",
        "set fdagents_pperiod [get_property PERIOD $fdagents_pclk]",
        "set fdagents_psrc [get_property SOURCE_PINS $fdagents_pclk]",
        "set fdagents_psrc_objs [get_pins -quiet $fdagents_psrc]",
        (
            "if {$fdagents_psrc_objs eq {}} "
            "{ set fdagents_psrc_objs [get_ports -quiet $fdagents_psrc] }"
        ),
        "set fdagents_psrc_obj [lindex $fdagents_psrc_objs 0]",
        "if {$fdagents_psrc_obj eq {}} { error {pressure clock source not found} }",
        f"create_clock -period {period:.3f} -name $fdagents_pcname $fdagents_psrc_obj",
        f'puts "FDAGENTS_PRESSURE_CLOCK $fdagents_pcname {period:.3f}"',
    ]
    epilogue = [
        "reset_timing -quiet",
        "read_xdc $fdagents_pressure_xdc",
        "update_timing",
        "set fdagents_prclk [get_clocks -quiet $fdagents_pcname]",
        "if {$fdagents_prclk eq {}} { error {pressure clock restoration failed} }",
        (
            "if {[get_property PERIOD $fdagents_prclk] ne $fdagents_pperiod} "
            "{ error {pressure clock restoration failed} }"
        ),
        'puts "FDAGENTS_PRESSURE_RESTORED $fdagents_pcname $fdagents_pperiod"',
    ]
    joined_prologue = "; ".join(" ".join(part.split()) for part in prologue)
    joined_epilogue = "; ".join(" ".join(part.split()) for part in epilogue)
    return f"{joined_prologue}; {command}; {joined_epilogue}"


def _expand_recipes_with_pressure(
    recipes: list[dict[str, str]],
    pressure_periods: list[float],
    clock_name: str,
    official_xdc_path: str,
) -> list[dict[str, str]]:
    if not pressure_periods:
        return recipes
    expanded: list[dict[str, str]] = []
    for period in pressure_periods:
        for recipe in recipes:
            expanded.append(
                {
                    "name": _safe_name(f"{recipe['name']}_press{period:.3f}ns"),
                    "command": build_pressure_wrapped_command(
                        str(recipe["command"]),
                        period,
                        clock_name,
                        official_xdc_path,
                    ),
                }
            )
    return expanded


def _post_place_prefix_tcl(recipe_name: str) -> str:
    """Build Tcl only from the closed post-place recipe registry."""
    recipe = POST_PLACE_RECIPE_REGISTRY.get(recipe_name)
    if recipe is None:
        raise ValueError(f"unknown post_place_recipe {recipe_name!r}")
    return (
        "route_design -unroute; "
        "place_design -unplace; "
        f"place_design -directive {recipe.place_directive}; "
        f"phys_opt_design -directive {recipe.phys_opt_preset}"
    )


def _ordered_route_directives(raw) -> list[str]:
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError("route_directives must be a nonempty list")
    directives: list[str] = []
    seen = set()
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("route_directives must contain nonempty strings")
        directive = item.strip()
        if directive not in ROUTE_DIRECTIVE_ALLOWLIST:
            raise ValueError(
                f"unknown route directive {directive!r}; "
                f"allowed directives: {list(ROUTE_DIRECTIVE_ALLOWLIST)}"
            )
        if directive not in seen:
            seen.add(directive)
            directives.append(directive)
    return directives


async def _strict_contest_wns(mcp, contest_clock: str) -> Optional[float]:
    """Return only a clock-scoped value; unsupported strict APIs are incomplete."""
    try:
        value = await mcp.get_wns(contest_clock, allow_fallback=False)
    except Exception:  # noqa: BLE001
        return None
    return float(value) if value is not None else None


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
        if params.get("endpoint_bel_move_after"):
            return SkillResult.failure(
                before_wns,
                "endpoint_bel_move_after is unsupported; run the standalone "
                "proof-gated endpoint_bel_move action",
                Path(mcp.run_dir) / "fresh_place_route_unsafe_endpoint_composite.dcp",
            )
        flow_mode = params.get("flow_mode")
        if flow_mode == SHARED_POST_PLACE_ROUTE_RACE:
            if params.get("pressure_periods") is not None or params.get(
                "pressure_period_ns"
            ) is not None:
                return SkillResult.failure(
                    before_wns,
                    "clock pressure is not supported in shared_post_place_route_race",
                    Path(mcp.run_dir) / "fresh_place_route_invalid_pressure.dcp",
                )
            return await self._execute_shared_post_place_route_race(
                mcp, target, params, before_wns, clock_period
            )
        if flow_mode is not None:
            return SkillResult.failure(
                before_wns,
                f"unknown fresh_place_route flow_mode {flow_mode!r}",
                Path(mcp.run_dir) / "fresh_place_route_invalid_flow_mode.dcp",
            )

        run_dir = mcp.run_dir
        input_dcp = params.get("input_dcp") or str(run_dir / "current_best.dcp")
        safe_target = _safe_name(target or "auto")
        try:
            strategy_regime = params.get("strategy_regime")
            if strategy_regime is None:
                # Backward-compatible execution of already resolved local
                # packets; new cold parameterization uses strategy_regime so
                # directive selection is not mislabeled as clock pressure.
                strategy_regime = params.get("pressure_regime")
            if strategy_regime is not None:
                recipes = _strategy_recipes(
                    str(strategy_regime),
                    params.get("route_directives"),
                    route_tns_cleanup=bool(
                        params.get("route_tns_cleanup", True)
                    ),
                )
            else:
                recipes = _normalize_recipes(params.get("recipes"))
        except (TypeError, ValueError) as exc:
            return SkillResult.failure(
                before_wns,
                f"invalid current-seed implementation strategy: {exc}",
                Path(run_dir) / f"fresh_place_route_{safe_target}.dcp",
            )
        effective_params = dict(params)
        factors = params.get("pressure_factors")
        if factors is not None:
            if (
                clock_period is None
                or not isinstance(factors, (list, tuple))
                or not 1 <= len(factors) <= 3
            ):
                return SkillResult.failure(
                    before_wns,
                    "current-seed pressure_factors require a measured clock and 1..3 factors",
                    Path(run_dir) / f"fresh_place_route_{safe_target}.dcp",
                )
            achieved_period = float(clock_period) - float(before_wns)
            periods = []
            for factor in factors:
                if (
                    isinstance(factor, bool)
                    or not isinstance(factor, (int, float))
                    or not 0.5 <= float(factor) < 1.0
                ):
                    return SkillResult.failure(
                        before_wns,
                        "current-seed pressure factor must be in [0.5, 1.0)",
                        Path(run_dir) / f"fresh_place_route_{safe_target}.dcp",
                    )
                periods.append(round(achieved_period * float(factor), 3))
            effective_params["pressure_periods"] = periods
        try:
            pressure_periods = _normalize_pressure_periods(effective_params)
        except (TypeError, ValueError) as exc:
            return SkillResult.failure(
                before_wns,
                f"invalid clock pressure periods: {exc}",
                Path(run_dir) / f"fresh_place_route_{safe_target}.dcp",
            )
        if pressure_periods:
            pressure_clock_name = str(
                params.get("clock_name")
                or getattr(mcp, "target_clock", "")
                or "clk_fpl26contest"
            )
            official_xdc_path = str(
                (Path(run_dir) / f"fresh_place_route_{safe_target}_official.xdc")
                .resolve()
            )
            recipes = _expand_recipes_with_pressure(
                recipes,
                pressure_periods,
                pressure_clock_name,
                official_xdc_path,
            )
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
        candidate_outputs: list[SkillOutput] = []
        best_seen: Optional[dict] = None
        budget_stopped = False
        adaptive_early_return: Optional[dict] = None

        logger.info(
            "[fresh_place_route] target=%r recipes=%d",
            target,
            len(recipes),
        )

        try:
            for idx, recipe in enumerate(recipes, 1):
                if candidate_budget_exhausted(
                    params,
                    completed_attempts=len(candidate_outputs),
                ):
                    budget_stopped = True
                    break
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
                attempt_started = time.monotonic()

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
                        if not endpoint_after:
                            candidate_outputs.append(SkillOutput(candidate_dcp, name))
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

                attempt["runtime_s"] = round(
                    max(0.0, time.monotonic() - attempt_started), 6
                )
                attempts.append(attempt)
                if best_seen is not None:
                    adaptive_early_return = continuation_opportunity_would_be_lost(
                        params,
                        best_delta_wns=float(best_seen["after_wns"]) - before_wns,
                        observed_candidate_runtime_s=float(attempt["runtime_s"]),
                        remaining_candidates=len(recipes) - idx,
                    )
                    if adaptive_early_return is not None:
                        logger.info(
                            "[fresh_place_route] adaptive early return after %d/%d: %s",
                            idx,
                            len(recipes),
                            adaptive_early_return["reason"],
                        )
                        break

            if best_seen is None:
                return SkillResult.failure(
                    before_wns,
                    f"no legal fresh place/route candidates; attempts={attempts}",
                    output_dcp,
                )

            output_dcp = run_dir / str(best_seen["output_dcp"]).rsplit("/", 1)[-1]
            after_wns = float(best_seen["after_wns"])
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
            if budget_stopped:
                summary += f" budget_stop {len(attempts)}/{len(recipes)}"
            if adaptive_early_return is not None:
                summary += f" adaptive_stop {len(attempts)}/{len(recipes)}"

            return SkillResult(
                success=True,
                before_wns=before_wns,
                after_wns=after_wns,
                delta_wns=after_wns - before_wns,
                is_legal=True,
                output_dcp=output_dcp,
                summary=summary,
                details={
                    "attempts": attempts,
                    "best": best_seen,
                    "requested_candidate_count": len(recipes),
                    "attempted_candidate_count": len(attempts),
                    "completed_candidate_count": len(candidate_outputs) or 1,
                    "budget_stopped": budget_stopped,
                    "adaptive_early_return": adaptive_early_return,
                },
                candidates=tuple(candidate_outputs),
            )

        except Exception as e:
            logger.error("[fresh_place_route] failed: %s", e)
            return SkillResult.failure(before_wns, str(e), output_dcp)

    async def _execute_shared_post_place_route_race(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        clock_period: Optional[float],
    ) -> SkillResult:
        run_dir = Path(mcp.run_dir)
        output_dcp = run_dir / "fresh_place_route_shared_route_race.dcp"

        # Validate every physical selector before opening or mutating Vivado.
        try:
            if target != "auto":
                raise ValueError(
                    "shared_post_place_route_race requires target='auto'"
                )
            recipe_name = params.get("post_place_recipe")
            if not isinstance(recipe_name, str) or recipe_name not in POST_PLACE_RECIPE_REGISTRY:
                raise ValueError(
                    f"unknown post_place_recipe {recipe_name!r}; allowed recipes: "
                    f"{list(POST_PLACE_RECIPE_REGISTRY)}"
                )
            directives = _ordered_route_directives(params.get("route_directives"))
            post_route_preset = params.get("post_route_phys_opt_preset")
            if post_route_preset in (None, ""):
                post_route_preset = None
            elif (
                not isinstance(post_route_preset, str)
                or post_route_preset not in PHYS_OPT_PRESET_ARGS
            ):
                raise ValueError(
                    f"unknown post_route_phys_opt_preset {post_route_preset!r}; "
                    f"allowed presets: {list(PHYS_OPT_PRESET_ARGS)}"
                )
            if params.get("endpoint_bel_move_after"):
                raise ValueError(
                    "shared_post_place_route_race cannot be combined with "
                    "endpoint_bel_move_after"
                )
            max_candidates = params.get("max_candidates", len(directives))
            if isinstance(max_candidates, bool) or not isinstance(max_candidates, int):
                raise ValueError("max_candidates must be an integer")
            if max_candidates < 1:
                raise ValueError("max_candidates must be at least 1")
            directives = directives[:max_candidates]
            prefix_command = _post_place_prefix_tcl(recipe_name)
        except (TypeError, ValueError) as exc:
            return SkillResult.failure(before_wns, str(exc), output_dcp)

        input_dcp = str(
            Path(params.get("input_dcp") or run_dir / "current_best.dcp")
            .expanduser()
            .resolve()
        )
        prefix_timeout = float(
            params.get("prefix_timeout_s", params.get("recipe_timeout_s", 5400.0))
        )
        route_timeout = float(
            params.get("route_timeout_s", params.get("recipe_timeout_s", 5400.0))
        )
        post_route_timeout = float(
            params.get(
                "post_route_phys_opt_timeout_s",
                params.get("recipe_timeout_s", 5400.0),
            )
        )
        contest_clock = str(
            params.get("_contest_clock") or "clk_fpl26contest"
        ).strip()
        post_place_dcp = run_dir / (
            f"fresh_place_route_post_place_{_safe_name(recipe_name)}.dcp"
        )
        source_xdc = run_dir / "fresh_place_route_source_timing.xdc"
        prefix_xdc = run_dir / "fresh_place_route_post_place_timing.xdc"
        attempts: list[dict] = []
        candidate_outputs: list[SkillOutput] = []
        best_seen: Optional[dict] = None
        budget_stopped = False

        try:
            await mcp.call_vivado(
                "open_checkpoint", {"dcp_path": input_dcp}, timeout=600.0
            )
            source_timing_xdc = await _export_timing_xdc(mcp, source_xdc)
            await mcp.call_vivado(
                "run_tcl",
                {"command": prefix_command, "timeout": prefix_timeout},
                timeout=prefix_timeout,
            )
            prefix_constraint_fallback = False
            if await _export_timing_xdc(mcp, prefix_xdc) != source_timing_xdc:
                # Pre-route phys_opt may replicate a register named in a timing
                # exception.  Re-run the deterministic placement prefix without
                # that structural substage; placement itself is constraint
                # preserving and remains the Luna-selected physical action.
                await mcp.call_vivado(
                    "open_checkpoint", {"dcp_path": input_dcp}, timeout=600.0
                )
                recipe = POST_PLACE_RECIPE_REGISTRY[recipe_name]
                place_only_command = (
                    "route_design -unroute; place_design -unplace; "
                    f"place_design -directive {recipe.place_directive}"
                )
                await mcp.call_vivado(
                    "run_tcl",
                    {"command": place_only_command, "timeout": prefix_timeout},
                    timeout=prefix_timeout,
                )
                if await _export_timing_xdc(mcp, prefix_xdc) != source_timing_xdc:
                    raise RuntimeError(
                        "fresh place/route placement changed immutable timing XDC"
                    )
                prefix_constraint_fallback = True
            pre_route_wns = await _strict_contest_wns(mcp, contest_clock)
            pre_route_estimate = {
                "official": False,
                "status": "complete" if pre_route_wns is not None else "incomplete",
                "contest_clock": contest_clock,
                "wns_ns": pre_route_wns,
            }
            await mcp.call_vivado(
                "write_checkpoint",
                {"dcp_path": str(post_place_dcp.resolve()), "force": True},
                timeout=600.0,
            )

            for idx, directive in enumerate(directives, 1):
                if candidate_budget_exhausted(
                    params,
                    completed_attempts=len(candidate_outputs),
                ):
                    budget_stopped = True
                    break

                suffix = f"_{_safe_name(post_route_preset)}" if post_route_preset else ""
                candidate_dcp = run_dir / (
                    f"fresh_place_route_{idx:02d}_{_safe_name(directive)}{suffix}.dcp"
                )
                label = directive + (
                    f"+{post_route_preset}" if post_route_preset else ""
                )
                attempt = {
                    "route_directive": directive,
                    "post_route_phys_opt_preset": post_route_preset,
                    "output_dcp": str(candidate_dcp),
                }
                try:
                    await mcp.call_vivado(
                        "open_checkpoint",
                        {"dcp_path": str(post_place_dcp.resolve())},
                        timeout=600.0,
                    )
                    await mcp.call_vivado(
                        "route_design",
                        {"directive": directive, "timeout": route_timeout},
                        timeout=route_timeout,
                    )
                    route_xdc = run_dir / (
                        f"fresh_place_route_{idx:02d}_{_safe_name(directive)}"
                        "_route_timing.xdc"
                    )
                    if await _export_timing_xdc(mcp, route_xdc) != source_timing_xdc:
                        raise RuntimeError(
                            "fresh place/route routing changed immutable timing XDC"
                        )
                    post_route_constraint_fallback = False
                    if post_route_preset:
                        route_only_dcp = run_dir / (
                            f"fresh_place_route_{idx:02d}_{_safe_name(directive)}"
                            "_constraint_safe_route_only.dcp"
                        )
                        await mcp.call_vivado(
                            "write_checkpoint",
                            {
                                "dcp_path": str(route_only_dcp.resolve()),
                                "force": True,
                            },
                            timeout=600.0,
                        )
                        phys_args = dict(PHYS_OPT_PRESET_ARGS[post_route_preset])
                        phys_args["timeout"] = post_route_timeout
                        await mcp.call_vivado(
                            "phys_opt_design",
                            phys_args,
                            timeout=post_route_timeout,
                        )
                        post_phys_xdc = run_dir / (
                            f"fresh_place_route_{idx:02d}_{_safe_name(directive)}"
                            "_post_phys_timing.xdc"
                        )
                        if (
                            await _export_timing_xdc(mcp, post_phys_xdc)
                            != source_timing_xdc
                        ):
                            await mcp.call_vivado(
                                "open_checkpoint",
                                {"dcp_path": str(route_only_dcp.resolve())},
                                timeout=600.0,
                            )
                            post_route_constraint_fallback = True

                    route_report = await mcp.call_vivado(
                        "report_route_status", {}, timeout=120.0
                    )
                    route = parse_route_status_static(route_report)
                    is_legal = bool(route.get("routed_ok"))
                    after_wns = await _strict_contest_wns(mcp, contest_clock)
                    attempt.update({
                        "after_wns": after_wns,
                        "is_legal": is_legal,
                        "route": route,
                        "timing_xdc_preserved": True,
                        "post_route_phys_opt_constraint_fallback": (
                            post_route_constraint_fallback
                        ),
                    })
                    if after_wns is not None:
                        attempt["delta_wns"] = after_wns - before_wns

                    if is_legal:
                        await mcp.call_vivado(
                            "write_checkpoint",
                            {
                                "dcp_path": str(candidate_dcp.resolve()),
                                "force": True,
                            },
                            timeout=600.0,
                        )
                        emitted_label = (
                            directive + "+constraint_safe_route_only"
                            if post_route_constraint_fallback
                            else label
                        )
                        candidate_outputs.append(
                            SkillOutput(candidate_dcp, emitted_label)
                        )
                        if (
                            best_seen is None
                            or (
                                after_wns is not None
                                and (
                                    best_seen.get("after_wns") is None
                                    or after_wns > best_seen["after_wns"]
                                )
                            )
                        ):
                            best_seen = dict(attempt)
                except Exception as exc:  # noqa: BLE001
                    attempt.update({"error": str(exc), "is_legal": False})
                    logger.warning(
                        "[fresh_place_route] route sibling %s failed: %s",
                        directive,
                        exc,
                    )
                attempts.append(attempt)

            details = {
                "flow_mode": SHARED_POST_PLACE_ROUTE_RACE,
                "post_place_recipe": recipe_name,
                "post_place_checkpoint": str(post_place_dcp),
                "source_timing_xdc": str(source_xdc),
                "pre_route_phys_opt_constraint_fallback": (
                    prefix_constraint_fallback
                ),
                "pre_route_estimate": pre_route_estimate,
                "attempts": attempts,
                "requested_route_directives": directives,
                "completed_route_directives": [
                    attempt["route_directive"] for attempt in attempts
                ],
                "unstarted_route_directives": directives[len(attempts):],
                "requested_candidate_count": len(directives),
                "attempted_candidate_count": len(attempts),
                "completed_candidate_count": len(candidate_outputs),
                "budget_stopped": budget_stopped,
            }
            if best_seen is None:
                return SkillResult(
                    success=False,
                    before_wns=before_wns,
                    after_wns=before_wns,
                    delta_wns=0.0,
                    is_legal=False,
                    output_dcp=output_dcp,
                    summary=(
                        "FAILED: no legal shared post-place route siblings"
                        + (" before budget stop" if budget_stopped else "")
                    ),
                    error_msg="no legal shared post-place route siblings",
                    details=details,
                )

            after_wns = best_seen.get("after_wns")
            reported_wns = float(after_wns) if after_wns is not None else before_wns
            output_dcp = Path(best_seen["output_dcp"])
            summary = (
                f"fresh_place_route:{recipe_name} route race "
                f"{len(candidate_outputs)}/{len(attempts)} legal"
            )
            if after_wns is not None:
                summary += f" best_wns={after_wns:.3f}"
            if budget_stopped:
                summary += f" budget_stop {len(attempts)}/{len(directives)}"
            details["best"] = best_seen
            return SkillResult(
                success=True,
                before_wns=before_wns,
                after_wns=reported_wns,
                delta_wns=reported_wns - before_wns,
                is_legal=True,
                output_dcp=output_dcp,
                summary=summary,
                details=details,
                candidates=tuple(candidate_outputs),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[fresh_place_route] shared route race failed: %s", exc)
            return SkillResult.failure(before_wns, str(exc), output_dcp)
