"""Clock-tighten skill for loose or overconstrained DCPs.

The skill retargets the active contest clock to periods derived from the
currently measured achievable period, reruns a bounded place/route recipe, and
reports the result as an equivalent WNS against the original clock period. That
keeps FDAgents' existing WNS/Fmax acceptance logic valid while the output DCP
contains the tightened clock constraint.
"""

from __future__ import annotations

import logging
import math
import re
from pathlib import Path
from typing import Iterable, Optional

from .base import (
    SkillResult,
    calculate_fmax,
    parse_route_status_static,
    parse_timing_summary_static,
)

logger = logging.getLogger(__name__)

PERIOD_PRECISION_NS = 0.001
DEFAULT_FACTORS = [0.92, 0.88, 0.84]
# Official alpha scoring is STA-derived from clk_fpl26contest period and WNS;
# it is not capped by the VU3P datasheet/global-clock 775 MHz number.
DEFAULT_MAX_FABRIC_FMAX_MHZ = 0.0
_MCP_RUN_TCL_TIMEOUT_GRACE_S = 120.0

_RECIPE_COMMANDS = {
    "extra_timing_no_relax": (
        "route_design -unroute; "
        "place_design -unplace; "
        "place_design -directive ExtraTimingOpt; "
        "phys_opt_design -directive Explore; "
        "route_design -directive NoTimingRelaxation -tns_cleanup; "
        "phys_opt_design -directive Explore"
    ),
    "extra_net_no_relax": (
        "route_design -unroute; "
        "place_design -unplace; "
        "place_design -directive ExtraNetDelay_high; "
        "phys_opt_design -directive Explore; "
        "route_design -directive NoTimingRelaxation -tns_cleanup; "
        "phys_opt_design -directive Explore"
    ),
    "route_only": (
        "route_design -unroute; "
        "route_design -directive NoTimingRelaxation -tns_cleanup; "
        "phys_opt_design -directive Explore"
    ),
}

_RECIPE_ALIASES = {
    "extratiming_no_relax": "extra_timing_no_relax",
    "extranetdelay_no_relax": "extra_net_no_relax",
    "extra_netdelay_no_relax": "extra_net_no_relax",
}

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_name(value: str) -> str:
    return _SAFE_NAME_RE.sub("_", value).strip("_") or "auto"


def achievable_period_ns(
    original_clock_period_ns: Optional[float],
    equivalent_wns_ns: Optional[float],
) -> Optional[float]:
    if original_clock_period_ns is None or equivalent_wns_ns is None:
        return None
    try:
        achieved = float(original_clock_period_ns) - float(equivalent_wns_ns)
    except (TypeError, ValueError):
        return None
    if achieved <= 0:
        return None
    return achieved


def candidate_periods(
    original_clock_period_ns: Optional[float],
    equivalent_wns_ns: Optional[float],
    factors: Iterable[float] = DEFAULT_FACTORS,
    max_fmax_mhz: float = DEFAULT_MAX_FABRIC_FMAX_MHZ,
) -> list[float]:
    achieved = achievable_period_ns(original_clock_period_ns, equivalent_wns_ns)
    if achieved is None:
        return []
    min_period = PERIOD_PRECISION_NS
    if max_fmax_mhz and max_fmax_mhz > 0:
        min_period = max(
            min_period,
            math.ceil((1000.0 / float(max_fmax_mhz)) / PERIOD_PRECISION_NS)
            * PERIOD_PRECISION_NS,
        )
    periods: list[float] = []
    seen = set()
    for factor in factors:
        try:
            period = round(max(min_period, achieved * float(factor)), 3)
        except (TypeError, ValueError):
            continue
        if period not in seen:
            periods.append(period)
            seen.add(period)
    return periods


def _normalize_target_periods(
    raw_periods,
    max_fmax_mhz: float = DEFAULT_MAX_FABRIC_FMAX_MHZ,
) -> list[float]:
    if raw_periods is None:
        return []
    if isinstance(raw_periods, str):
        items = [item.strip() for item in raw_periods.split(",")]
    else:
        items = list(raw_periods)

    min_period = PERIOD_PRECISION_NS
    if max_fmax_mhz and max_fmax_mhz > 0:
        min_period = max(
            min_period,
            math.ceil((1000.0 / float(max_fmax_mhz)) / PERIOD_PRECISION_NS)
            * PERIOD_PRECISION_NS,
        )

    periods: list[float] = []
    seen = set()
    for item in items:
        try:
            period = round(max(min_period, float(item)), 3)
        except (TypeError, ValueError):
            continue
        if period <= 0 or period in seen:
            continue
        periods.append(period)
        seen.add(period)
    return periods


def equivalent_wns_for_original_clock(
    original_clock_period_ns: float,
    tightened_clock_period_ns: float,
    tightened_wns_ns: float,
) -> float:
    achieved = float(tightened_clock_period_ns) - float(tightened_wns_ns)
    return float(original_clock_period_ns) - achieved


def _fmax_for_tightened_candidate(
    tightened_clock_period_ns: float,
    tightened_wns_ns: Optional[float],
) -> Optional[float]:
    if tightened_wns_ns is None:
        return None
    achieved = float(tightened_clock_period_ns) - float(tightened_wns_ns)
    if achieved <= 0:
        return None
    return 1000.0 / achieved


def _round_period_up_ns(period_ns: float) -> float:
    steps = math.ceil((float(period_ns) - 1e-12) / PERIOD_PRECISION_NS)
    return round(steps * PERIOD_PRECISION_NS, 3)


def score_candidate_fmax(
    candidate_fmax_mhz: Optional[float],
    max_fmax_mhz: float = DEFAULT_MAX_FABRIC_FMAX_MHZ,
    min_period_required_ns: Optional[float] = None,
) -> Optional[float]:
    if candidate_fmax_mhz is None:
        return None
    caps = []
    if max_fmax_mhz and max_fmax_mhz > 0:
        caps.append(float(max_fmax_mhz))
    if caps:
        return min(float(candidate_fmax_mhz), *caps)
    return float(candidate_fmax_mhz)


def equivalent_wns_for_scored_fmax(
    original_clock_period_ns: float,
    scored_fmax_mhz: float,
) -> float:
    return float(original_clock_period_ns) - (1000.0 / float(scored_fmax_mhz))


def _parse_tightened_wns_marker(text: str) -> Optional[float]:
    match = re.search(r"FDAGENTS_TIGHTEN_WNS\s+(-?\d+(?:\.\d+)?)", text)
    if match:
        return float(match.group(1))
    return None


def candidate_timing_is_legal(
    timing_summary: dict,
    candidate_fmax_mhz: Optional[float],
    max_fmax_mhz: float = DEFAULT_MAX_FABRIC_FMAX_MHZ,
) -> bool:
    if candidate_fmax_mhz is None:
        return False

    def optional_nonnegative(key: str) -> bool:
        value = timing_summary.get(key)
        return value is None or float(value) >= -1e-6

    def required_nonnegative(key: str) -> bool:
        value = timing_summary.get(key)
        return value is not None and float(value) >= -1e-6

    def optional_zero_endpoints(key: str) -> bool:
        value = timing_summary.get(key)
        return value is None or int(value) == 0

    def required_zero_endpoints(key: str) -> bool:
        value = timing_summary.get(key)
        return value is not None and int(value) == 0

    return (
        optional_nonnegative("whs")
        and optional_nonnegative("ths")
        and optional_zero_endpoints("ths_failing_endpoints")
        and required_nonnegative("wpws")
        and required_nonnegative("tpws")
        and required_zero_endpoints("tpws_failing_endpoints")
    )


def _candidate_is_hold_clean(timing_summary: dict) -> bool:
    def optional_nonnegative(key: str) -> bool:
        value = timing_summary.get(key)
        return value is None or float(value) >= -1e-6

    def optional_zero_endpoints(key: str) -> bool:
        value = timing_summary.get(key)
        return value is None or int(value) == 0

    return (
        optional_nonnegative("whs")
        and optional_nonnegative("ths")
        and optional_zero_endpoints("ths_failing_endpoints")
    )


def _candidate_has_hold_failure(timing_summary: dict) -> bool:
    checks = []
    for key in ("whs", "ths"):
        value = timing_summary.get(key)
        if value is not None:
            checks.append(float(value) < -1e-6)
    failing = timing_summary.get("ths_failing_endpoints")
    if failing is not None:
        checks.append(int(failing) > 0)
    return any(checks)


def _candidate_has_pulse_or_min_period_failure(timing_summary: dict) -> bool:
    checks = []
    for key in ("wpws", "tpws", "min_period_slack_ns"):
        value = timing_summary.get(key)
        if value is not None:
            checks.append(float(value) < -1e-6)
    failing = timing_summary.get("tpws_failing_endpoints")
    if failing is not None:
        checks.append(int(failing) > 0)
    return any(checks)


def recap_periods_from_timing_summary(
    timing_summary: dict,
    target_period_ns: float,
    margin_ns: float = PERIOD_PRECISION_NS,
) -> list[float]:
    required = timing_summary.get("min_period_required_ns")
    if required is None:
        return []
    try:
        recap_period = _round_period_up_ns(float(required) + float(margin_ns))
    except (TypeError, ValueError):
        return []
    if recap_period <= float(target_period_ns) + 1e-12:
        return []
    return [recap_period]


def _normalize_factors(raw) -> list[float]:
    if raw is None:
        return list(DEFAULT_FACTORS)
    if isinstance(raw, str):
        items = [item.strip() for item in raw.split(",")]
    else:
        items = list(raw)
    factors = []
    for item in items:
        try:
            value = float(item)
        except (TypeError, ValueError):
            continue
        if value > 0:
            factors.append(value)
    return factors or list(DEFAULT_FACTORS)


def _normalize_recipes(raw) -> list[dict[str, str]]:
    if raw is None:
        raw_items = ["extra_timing_no_relax"]
    elif isinstance(raw, str):
        raw_items = [item.strip() for item in raw.split(",")]
    else:
        raw_items = list(raw)

    recipes: list[dict[str, str]] = []
    for idx, item in enumerate(raw_items, 1):
        if not item:
            continue
        if isinstance(item, dict):
            command = str(item.get("command") or "").strip()
            if not command:
                continue
            name = _safe_name(str(item.get("name") or f"custom_{idx}"))
            recipes.append({"name": name, "command": command})
            continue
        name = str(item).strip()
        canonical = _RECIPE_ALIASES.get(name, name)
        command = _RECIPE_COMMANDS.get(canonical)
        if command is None:
            recipes.append({"name": _safe_name(name), "command": name})
        else:
            recipes.append({"name": canonical, "command": command})
    return recipes


def build_clock_tighten_tcl(
    target_period_ns: float,
    clock_name: Optional[str],
    recipe_command: str,
    checkpoint_path: Optional[str] = None,
) -> str:
    clock_lookup = clock_name or "clk_fpl26contest"
    commands = [
        f"set clk [get_clocks -quiet {{{clock_lookup}}}]",
        "if {$clk eq {}} { set clk [get_clocks -quiet clk_fpl26contest] }",
        "if {$clk eq {}} { set clk [lindex [get_clocks -quiet] 0] }",
        "if {$clk eq {}} { error {no clock found for clock_tighten} }",
        "set cname [get_property NAME $clk]",
        "set src [get_property SOURCE_PINS $clk]",
        "set src_objs [get_pins -quiet $src]",
        "if {$src_objs eq {}} { set src_objs [get_ports -quiet $src] }",
        (
            "if {$src_objs eq {} && $src ne {}} { "
            "set src_name [get_property NAME $src]; "
            "set src_objs [get_pins -quiet $src_name]; "
            "if {$src_objs eq {}} { set src_objs [get_ports -quiet $src_name] } }"
        ),
        "if {$src_objs eq {}} { set src_objs [get_ports -quiet $cname] }",
        "set src_obj [lindex $src_objs 0]",
        'if {$src_obj eq {}} { error "cannot find source object for clock $cname source=$src" }',
        f"create_clock -period {target_period_ns:.3f} -name $cname $src_obj",
        f'puts "FDAGENTS_CLOCK_TIGHTEN $cname {target_period_ns:.3f} source=$src"',
    ]
    if checkpoint_path:
        commands.append(f"set fdagents_candidate_checkpoint {{{checkpoint_path}}}")
    commands.extend(
        [
            recipe_command,
            "set tp [get_timing_paths -max_paths 1 -setup]",
            (
                'if {$tp eq {}} { puts "FDAGENTS_TIGHTEN_WNS NA" } '
                'else { puts "FDAGENTS_TIGHTEN_WNS [get_property SLACK $tp]" }'
            ),
        ]
    )
    return "; ".join(" ".join(command.split()) for command in commands)


def build_clock_recap_tcl(
    recap_period_ns: float,
    clock_name: Optional[str],
) -> str:
    clock_lookup = clock_name or "clk_fpl26contest"
    commands = [
        f"set clk [get_clocks -quiet {{{clock_lookup}}}]",
        "if {$clk eq {}} { set clk [get_clocks -quiet clk_fpl26contest] }",
        "if {$clk eq {}} { set clk [lindex [get_clocks -quiet] 0] }",
        "if {$clk eq {}} { error {no clock found for clock recap} }",
        "set cname [get_property NAME $clk]",
        "set src [get_property SOURCE_PINS $clk]",
        "set src_objs [get_pins -quiet $src]",
        "if {$src_objs eq {}} { set src_objs [get_ports -quiet $src] }",
        (
            "if {$src_objs eq {} && $src ne {}} { "
            "set src_name [get_property NAME $src]; "
            "set src_objs [get_pins -quiet $src_name]; "
            "if {$src_objs eq {}} { set src_objs [get_ports -quiet $src_name] } }"
        ),
        "if {$src_objs eq {}} { set src_objs [get_ports -quiet $cname] }",
        "set src_obj [lindex $src_objs 0]",
        'if {$src_obj eq {}} { error "cannot find source object for clock $cname source=$src" }',
        f"create_clock -period {recap_period_ns:.3f} -name $cname $src_obj",
        f'puts "FDAGENTS_CLOCK_RECAP $cname {recap_period_ns:.3f} source=$src"',
        "set tp [get_timing_paths -max_paths 1 -setup]",
        (
            'if {$tp eq {}} { puts "FDAGENTS_TIGHTEN_WNS NA" } '
            'else { puts "FDAGENTS_TIGHTEN_WNS [get_property SLACK $tp]" }'
        ),
    ]
    return "; ".join(" ".join(command.split()) for command in commands)


def build_clock_as_data_false_path_tcl(
    clock_name: Optional[str],
) -> str:
    clock_lookup = clock_name or "clk_fpl26contest"
    commands = [
        f"set clk [get_clocks -quiet {{{clock_lookup}}}]",
        "if {$clk eq {}} { set clk [get_clocks -quiet clk_fpl26contest] }",
        "if {$clk eq {}} { set clk [lindex [get_clocks -quiet] 0] }",
        "if {$clk eq {}} { error {no clock found for clock-as-data false path} }",
        "set cname [get_property NAME $clk]",
        "set tp [get_timing_paths -delay_type min -slack_lesser_than 0 -max_paths 1]",
        "if {$tp eq {}} { error {no negative hold path for clock-as-data false path} }",
        "set start_pin [get_property STARTPOINT_PIN $tp]",
        "set end_pin [get_property ENDPOINT_PIN $tp]",
        "set start_clock [get_property STARTPOINT_CLOCK $tp]",
        "set end_clock [get_property ENDPOINT_CLOCK $tp]",
        (
            "if {$start_clock ne $cname || $end_clock ne $cname} { "
            "error \"hold path is not intra-clock for $cname: start=$start_clock end=$end_clock\" }"
        ),
        "set from_obj {}",
        "if {$start_pin ne {}} { set from_obj [get_pins -quiet $start_pin] }",
        "if {$from_obj eq {} && $start_pin ne {}} { set from_obj [get_ports -quiet $start_pin] }",
        "set to_obj [get_pins -quiet $end_pin]",
        "if {$from_obj eq {} || $to_obj eq {}} { error \"cannot resolve false-path endpoints from=$start_pin to=$end_pin\" }",
        "set src [get_property SOURCE_PINS $clk]",
        "set src_objs [get_pins -quiet $src]",
        "if {$src_objs eq {}} { set src_objs [get_ports -quiet $src] }",
        (
            "if {$src_objs eq {} && $src ne {}} { "
            "set src_name [get_property NAME $src]; "
            "set src_objs [get_pins -quiet $src_name]; "
            "if {$src_objs eq {}} { set src_objs [get_ports -quiet $src_name] } }"
        ),
        "if {$src_objs eq {}} { set src_objs [get_ports -quiet $cname] }",
        "set from_name [get_property NAME [lindex $from_obj 0]]",
        "set source_ok 0",
        "foreach src_obj $src_objs { if {[get_property NAME $src_obj] eq $from_name} { set source_ok 1 } }",
        (
            "if {!$source_ok} { "
            "error \"hold startpoint $from_name is not the source of clock $cname\" }"
        ),
        "set_false_path -from $from_obj -to $to_obj",
        "puts \"FDAGENTS_CLOCK_AS_DATA_FALSE_PATH from=$from_name to=$end_pin clock=$cname\"",
        "set max_tp [get_timing_paths -max_paths 1 -setup]",
        (
            'if {$max_tp eq {}} { puts "FDAGENTS_TIGHTEN_WNS NA" } '
            'else { puts "FDAGENTS_TIGHTEN_WNS [get_property SLACK $max_tp]" }'
        ),
    ]
    return "; ".join(" ".join(command.split()) for command in commands)


def _exception_looks_like_timeout(exc: Exception) -> bool:
    return isinstance(exc, TimeoutError) or "timeout" in str(exc).lower()


class ClockTightenSkill:
    """Sweep tightened clock periods with a bounded fresh place/route recipe."""

    name = "clock_tighten"

    async def execute(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        clock_period: Optional[float],
    ) -> SkillResult:
        run_dir = Path(mcp.run_dir)
        input_dcp = params.get("input_dcp") or str(run_dir / "current_best.dcp")
        safe_target = _safe_name(target or "auto")
        output_dcp = run_dir / f"clock_tighten_{safe_target}.dcp"
        original_clock = clock_period

        if original_clock is None:
            return SkillResult.failure(before_wns, "missing original clock period", output_dcp)

        recipes = _normalize_recipes(params.get("recipes"))
        try:
            max_fmax_mhz = float(
                params.get("max_fmax_mhz", DEFAULT_MAX_FABRIC_FMAX_MHZ)
            )
        except (TypeError, ValueError):
            max_fmax_mhz = DEFAULT_MAX_FABRIC_FMAX_MHZ
        periods = _normalize_target_periods(
            params.get("target_periods"),
            max_fmax_mhz=max_fmax_mhz,
        )
        if not periods:
            periods = candidate_periods(
                original_clock,
                before_wns,
                _normalize_factors(params.get("factors")),
                max_fmax_mhz=max_fmax_mhz,
            )
        try:
            max_candidates = int(params.get("max_candidates", len(periods)))
        except (TypeError, ValueError):
            max_candidates = len(periods)
        periods = periods[: max(1, max_candidates)]
        if str(params.get("period_order", "")).lower() in {
            "tightest_first",
            "ascending",
            "fastest_first",
        }:
            periods = sorted(periods)
        recipe_timeout = float(params.get("recipe_timeout_s", 1800.0))
        clock_name = str(
            params.get("clock_name")
            or getattr(mcp, "target_clock", "")
            or "clk_fpl26contest"
        )
        stop_after_first_legal = bool(params.get("stop_after_first_legal", False))
        try:
            stop_after_first_legal_min_fmax = float(
                params.get("stop_after_first_legal_min_fmax_mhz", 0.0)
            )
        except (TypeError, ValueError):
            stop_after_first_legal_min_fmax = 0.0
        enable_pulse_recap = bool(params.get("enable_pulse_recap", False))
        enable_clock_as_data_false_path = bool(
            params.get("enable_clock_as_data_false_path", False)
        )
        recover_partial_candidate_on_timeout = bool(
            params.get("recover_partial_candidate_on_timeout", False)
        )
        try:
            recap_margin_ns = float(params.get("recap_margin_ns", PERIOD_PRECISION_NS))
        except (TypeError, ValueError):
            recap_margin_ns = PERIOD_PRECISION_NS
        try:
            max_recap_candidates = int(params.get("max_recap_candidates", 1))
        except (TypeError, ValueError):
            max_recap_candidates = 1

        if not recipes:
            return SkillResult.failure(before_wns, "no clock_tighten recipes selected", output_dcp)
        if not periods:
            return SkillResult.failure(before_wns, "no valid clock_tighten target periods", output_dcp)

        attempts = []
        best_seen: Optional[dict] = None
        before_fmax = score_candidate_fmax(
            calculate_fmax(before_wns, original_clock),
            max_fmax_mhz=max_fmax_mhz,
        )

        logger.info(
            "[clock_tighten] target=%r periods=%s recipes=%s",
            target,
            periods,
            [r["name"] for r in recipes],
        )

        stop_sweep = False
        try:
            for recipe in recipes:
                for target_period in periods:
                    name = str(recipe["name"])
                    candidate_dcp = run_dir / (
                        f"clock_tighten_{_safe_name(name)}_{target_period:.3f}ns.dcp"
                    )
                    attempt = {
                        "name": name,
                        "target_period_ns": target_period,
                        "output_dcp": str(candidate_dcp),
                    }
                    try:
                        await mcp.call_vivado(
                            "open_checkpoint",
                            {"dcp_path": str(Path(input_dcp).expanduser().resolve())},
                            timeout=600.0,
                        )
                        tcl = build_clock_tighten_tcl(
                            target_period,
                            clock_name,
                            str(recipe["command"]),
                            checkpoint_path=str(candidate_dcp.resolve()),
                        )
                        recovered_from_timeout = False
                        try:
                            tcl_output = await mcp.call_vivado(
                                "run_tcl",
                                {"command": tcl, "timeout": recipe_timeout},
                                timeout=recipe_timeout + _MCP_RUN_TCL_TIMEOUT_GRACE_S,
                            )
                        except Exception as exc:
                            if (
                                recover_partial_candidate_on_timeout
                                and _exception_looks_like_timeout(exc)
                                and candidate_dcp.exists()
                            ):
                                recovered_from_timeout = True
                                tcl_output = ""
                                attempt["timeout_recovered"] = True
                                attempt["timeout_error"] = str(exc)
                                logger.warning(
                                    "[clock_tighten] %s target=%.3f timed out "
                                    "after writing candidate; validating partial checkpoint",
                                    name,
                                    target_period,
                                )
                            else:
                                raise
                        if recovered_from_timeout:
                            await mcp.call_vivado(
                                "open_checkpoint",
                                {"dcp_path": str(candidate_dcp.resolve())},
                                timeout=600.0,
                            )
                        route_report = await mcp.call_vivado(
                            "report_route_status", {}, timeout=120.0
                        )
                        route = parse_route_status_static(route_report)
                        route_legal = bool(route.get("routed_ok"))
                        tightened_wns = _parse_tightened_wns_marker(tcl_output)
                        if tightened_wns is None:
                            tightened_wns = await mcp.get_wns()
                        timing_report = await mcp.call_vivado(
                            "report_timing_summary", {}, timeout=300.0
                        )
                        timing_summary = parse_timing_summary_static(timing_report)
                        if tightened_wns is None:
                            tightened_wns = timing_summary.get("wns")
                        candidate_fmax = _fmax_for_tightened_candidate(
                            target_period, tightened_wns
                        )
                        if tightened_wns is None or candidate_fmax is None:
                            raise RuntimeError("failed to parse tightened timing")
                        scored_fmax = score_candidate_fmax(
                            candidate_fmax,
                            max_fmax_mhz=max_fmax_mhz,
                            min_period_required_ns=timing_summary.get("min_period_required_ns"),
                        )
                        if scored_fmax is None:
                            raise RuntimeError("failed to score tightened timing")
                        timing_legal = candidate_timing_is_legal(
                            timing_summary,
                            candidate_fmax,
                            max_fmax_mhz=max_fmax_mhz,
                        )
                        is_legal = route_legal and timing_legal
                        raw_equiv_wns = equivalent_wns_for_original_clock(
                            float(original_clock), target_period, float(tightened_wns)
                        )
                        scored_equiv_wns = equivalent_wns_for_scored_fmax(
                            float(original_clock), scored_fmax
                        )
                        attempt.update(
                            {
                                "tightened_wns_ns": tightened_wns,
                                "equivalent_wns_ns": scored_equiv_wns,
                                "raw_equivalent_wns_ns": raw_equiv_wns,
                                "candidate_fmax_mhz": candidate_fmax,
                                "scored_fmax_mhz": scored_fmax,
                                "is_legal": is_legal,
                                "route_legal": route_legal,
                                "timing_legal": timing_legal,
                                "route": route,
                                "timing_summary": timing_summary,
                                "max_fmax_mhz": max_fmax_mhz,
                            }
                        )
                        if (
                            enable_pulse_recap
                            and route_legal
                            and not timing_legal
                            and _candidate_is_hold_clean(timing_summary)
                            and _candidate_has_pulse_or_min_period_failure(timing_summary)
                        ):
                            recap_periods = recap_periods_from_timing_summary(
                                timing_summary,
                                target_period,
                                margin_ns=recap_margin_ns,
                            )[: max(1, max_recap_candidates)]
                            recap_attempts = []
                            for recap_period in recap_periods:
                                recap_tcl = build_clock_recap_tcl(
                                    recap_period,
                                    clock_name,
                                )
                                recap_output = await mcp.call_vivado(
                                    "run_tcl",
                                    {"command": recap_tcl, "timeout": 120.0},
                                    timeout=180.0,
                                )
                                recap_wns = _parse_tightened_wns_marker(recap_output)
                                if recap_wns is None:
                                    recap_wns = await mcp.get_wns()
                                recap_report = await mcp.call_vivado(
                                    "report_timing_summary", {}, timeout=300.0
                                )
                                recap_summary = parse_timing_summary_static(recap_report)
                                if recap_wns is None:
                                    recap_wns = recap_summary.get("wns")
                                recap_fmax = _fmax_for_tightened_candidate(
                                    recap_period,
                                    recap_wns,
                                )
                                recap_scored_fmax = score_candidate_fmax(
                                    recap_fmax,
                                    max_fmax_mhz=max_fmax_mhz,
                                    min_period_required_ns=recap_summary.get("min_period_required_ns"),
                                )
                                recap_timing_legal = candidate_timing_is_legal(
                                    recap_summary,
                                    recap_fmax,
                                    max_fmax_mhz=max_fmax_mhz,
                                )
                                recap_is_legal = (
                                    recap_fmax is not None
                                    and recap_scored_fmax is not None
                                    and recap_timing_legal
                                )
                                recap_attempt = {
                                    "recap_period_ns": recap_period,
                                    "tightened_wns_ns": recap_wns,
                                    "candidate_fmax_mhz": recap_fmax,
                                    "scored_fmax_mhz": recap_scored_fmax,
                                    "timing_legal": recap_timing_legal,
                                    "is_legal": recap_is_legal,
                                    "timing_summary": recap_summary,
                                }
                                recap_attempts.append(recap_attempt)
                                if recap_is_legal:
                                    candidate_dcp = run_dir / (
                                        f"clock_tighten_{_safe_name(name)}_"
                                        f"{target_period:.3f}ns_recap_{recap_period:.3f}ns.dcp"
                                    )
                                    scored_equiv_wns = equivalent_wns_for_scored_fmax(
                                        float(original_clock),
                                        float(recap_scored_fmax),
                                    )
                                    attempt.update(
                                        {
                                            "output_dcp": str(candidate_dcp),
                                            "recap_period_ns": recap_period,
                                            "tightened_wns_ns": recap_wns,
                                            "equivalent_wns_ns": scored_equiv_wns,
                                            "raw_equivalent_wns_ns": equivalent_wns_for_original_clock(
                                                float(original_clock),
                                                recap_period,
                                                float(recap_wns),
                                            ),
                                            "candidate_fmax_mhz": recap_fmax,
                                            "scored_fmax_mhz": recap_scored_fmax,
                                            "is_legal": True,
                                            "timing_legal": True,
                                            "timing_summary": recap_summary,
                                        }
                                    )
                                    is_legal = True
                                    scored_fmax = float(recap_scored_fmax)
                                    candidate_fmax = float(recap_fmax)
                                    tightened_wns = float(recap_wns)
                                    break
                            attempt["recap_attempts"] = recap_attempts
                        if (
                            enable_clock_as_data_false_path
                            and route_legal
                            and not is_legal
                            and _candidate_has_hold_failure(timing_summary)
                        ):
                            false_path_attempt = {}
                            false_path_tcl = build_clock_as_data_false_path_tcl(
                                clock_name,
                            )
                            false_path_output = await mcp.call_vivado(
                                "run_tcl",
                                {"command": false_path_tcl, "timeout": 120.0},
                                timeout=180.0,
                            )
                            false_path_wns = _parse_tightened_wns_marker(
                                false_path_output
                            )
                            if false_path_wns is None:
                                false_path_wns = await mcp.get_wns()
                            false_path_report = await mcp.call_vivado(
                                "report_timing_summary", {}, timeout=300.0
                            )
                            false_path_summary = parse_timing_summary_static(
                                false_path_report
                            )
                            if false_path_wns is None:
                                false_path_wns = false_path_summary.get("wns")
                            false_path_fmax = _fmax_for_tightened_candidate(
                                target_period,
                                false_path_wns,
                            )
                            false_path_scored_fmax = score_candidate_fmax(
                                false_path_fmax,
                                max_fmax_mhz=max_fmax_mhz,
                                min_period_required_ns=false_path_summary.get(
                                    "min_period_required_ns"
                                ),
                            )
                            false_path_timing_legal = candidate_timing_is_legal(
                                false_path_summary,
                                false_path_fmax,
                                max_fmax_mhz=max_fmax_mhz,
                            )
                            false_path_is_legal = (
                                false_path_fmax is not None
                                and false_path_scored_fmax is not None
                                and false_path_timing_legal
                            )
                            false_path_attempt.update(
                                {
                                    "tightened_wns_ns": false_path_wns,
                                    "candidate_fmax_mhz": false_path_fmax,
                                    "scored_fmax_mhz": false_path_scored_fmax,
                                    "timing_legal": false_path_timing_legal,
                                    "is_legal": false_path_is_legal,
                                    "timing_summary": false_path_summary,
                                }
                            )
                            attempt["clock_as_data_false_path_attempt"] = false_path_attempt
                            if false_path_is_legal:
                                candidate_dcp = run_dir / (
                                    f"clock_tighten_{_safe_name(name)}_"
                                    f"{target_period:.3f}ns_clock_as_data_false_path.dcp"
                                )
                                scored_equiv_wns = equivalent_wns_for_scored_fmax(
                                    float(original_clock),
                                    float(false_path_scored_fmax),
                                )
                                attempt.update(
                                    {
                                        "output_dcp": str(candidate_dcp),
                                        "clock_as_data_false_path": True,
                                        "tightened_wns_ns": false_path_wns,
                                        "equivalent_wns_ns": scored_equiv_wns,
                                        "raw_equivalent_wns_ns": equivalent_wns_for_original_clock(
                                            float(original_clock),
                                            target_period,
                                            float(false_path_wns),
                                        ),
                                        "candidate_fmax_mhz": false_path_fmax,
                                        "scored_fmax_mhz": false_path_scored_fmax,
                                        "is_legal": True,
                                        "timing_legal": True,
                                        "timing_summary": false_path_summary,
                                    }
                                )
                                is_legal = True
                                scored_fmax = float(false_path_scored_fmax)
                                candidate_fmax = float(false_path_fmax)
                                tightened_wns = float(false_path_wns)
                        if is_legal:
                            if not recovered_from_timeout:
                                await mcp.call_vivado(
                                    "write_checkpoint",
                                    {"dcp_path": str(candidate_dcp.resolve()), "force": True},
                                    timeout=600.0,
                                )
                            if (
                                best_seen is None
                                or scored_fmax > best_seen["scored_fmax_mhz"]
                            ):
                                best_seen = dict(attempt)
                            reached_stop_fmax = (
                                stop_after_first_legal_min_fmax <= 0.0
                                or scored_fmax >= stop_after_first_legal_min_fmax
                            )
                            if stop_after_first_legal and reached_stop_fmax:
                                stop_sweep = True
                        logger.info(
                            "[clock_tighten] %s target=%.3f legal=%s tight_wns=%.3f fmax=%.1f scored=%.1f",
                            name,
                            target_period,
                            is_legal,
                            tightened_wns,
                            candidate_fmax,
                            scored_fmax,
                        )
                    except Exception as exc:
                        attempt.update({"error": str(exc), "is_legal": False})
                        logger.warning(
                            "[clock_tighten] %s target=%.3f failed: %s",
                            name,
                            target_period,
                            exc,
                        )
                    attempts.append(attempt)
                    if stop_sweep:
                        break
                if stop_sweep:
                    break
        except Exception as exc:
            logger.error("[clock_tighten] failed during sweep: %s", exc)
            return SkillResult.failure(before_wns, str(exc), output_dcp)
        try:
            if best_seen is None:
                return SkillResult.failure(
                    before_wns,
                    f"no legal clock_tighten candidates; attempts={attempts}",
                    output_dcp,
                )

            output_dcp = Path(best_seen["output_dcp"])
            reopen_error = None
            try:
                await mcp.call_vivado(
                    "open_checkpoint",
                    {"dcp_path": str(output_dcp.resolve())},
                    timeout=600.0,
                )
            except Exception as exc:
                reopen_error = str(exc)
                logger.warning(
                    "[clock_tighten] best checkpoint was written but Vivado "
                    "failed to reopen it: %s",
                    exc,
                )
            after_wns = float(best_seen["equivalent_wns_ns"])
            after_fmax = float(best_seen["scored_fmax_mhz"])
            target_period = float(best_seen["target_period_ns"])
            tightened_wns = float(best_seen["tightened_wns_ns"])
            recap_period = best_seen.get("recap_period_ns")
            target_part = f"target {target_period:.3f}ns"
            if recap_period is not None:
                target_part += f" recap {float(recap_period):.3f}ns"
            if best_seen.get("clock_as_data_false_path"):
                target_part += " clock-as-data false path"
            if best_seen.get("timeout_recovered"):
                target_part += " timeout-recovered"
            if before_fmax is not None:
                summary = (
                    f"clock_tighten:{best_seen['name']} {target_part} "
                    f"tightWNS {tightened_wns:.3f} equivWNS {before_wns:.3f}->{after_wns:.3f} "
                    f"fmax {before_fmax:.1f}->{after_fmax:.1f} MHz"
                )
            else:
                summary = (
                    f"clock_tighten:{best_seen['name']} {target_part} "
                    f"equivWNS {before_wns:.3f}->{after_wns:.3f}"
                )
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
                    "reopen_error": reopen_error,
                },
            )
        except Exception as exc:
            logger.error("[clock_tighten] failed: %s", exc)
            return SkillResult.failure(before_wns, str(exc), output_dcp)
