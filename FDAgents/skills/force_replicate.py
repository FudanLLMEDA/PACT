"""
force_replicate skill -- Vivado force replication with configurable unroute scope.

This captures the reusable post-route replication flow as a first-class skill:

    resolve target net
    route_design -unroute
    phys_opt_design -force_replication_on_nets $net
    phys_opt_design -force_replication_on_nets $net
    phys_opt_design -directive AggressiveExplore
    route_design -directive AlternateCLBRouting
    phys_opt_design -directive AggressiveExplore
    route_design

Use global scope on source/control nets where Vivado needs a full unrouted
design before post-route force replication will act. Use net scope only for
controlled local experiments. For late intermediate LUT-output nets, use
per_net_unroute instead.
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

logger = logging.getLogger(__name__)


def _tcl_quote(value: str) -> str:
    return "{" + value.replace("\\", "\\\\").replace("}", "\\}") + "}"


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "target"


_NET_UNROUTE_SCOPE_ALIASES = {
    "net",
    "local",
    "target",
    "target_net",
    "per_net",
    "per-net",
    "single_net",
}

_GLOBAL_UNROUTE_SCOPE_ALIASES = {
    "",
    "all",
    "design",
    "full",
    "full_design",
    "global",
}


def _normalize_unroute_scope(value: object) -> str:
    normalized = str(value or "global").strip().lower().replace("-", "_")
    if normalized in _NET_UNROUTE_SCOPE_ALIASES:
        return "net"
    if normalized in _GLOBAL_UNROUTE_SCOPE_ALIASES:
        return "global"
    return "global"


def _build_clock_like_net_filter_tcl() -> str:
    return (
        "proc fdagents_clock_like_net {nname} { "
        "if {[regexp -nocase {(^|/|_)(ap_)?(clk|clock)(/|_|$)} $nname]} { return 1 }; "
        "if {[regexp -nocase {(^|/|_)aclk(/|_|$)} $nname]} { return 1 }; "
        "if {[regexp -nocase {(wclk|rclk|wrclk|rdclk|txoutclk|rxoutclk|userclk|coreclk)} $nname]} { return 1 }; "
        "if {[regexp -nocase {(bufg|mmcm|pll)} $nname]} { return 1 }; "
        "return 0 }"
    )


def _build_auto_highfanout_group_tcl(params: dict) -> str:
    num_paths = max(1, int(params.get("num_paths", 24)))
    max_nets = max(1, int(params.get("max_nets", 18)))
    endpoint_min_fanout = max(1, int(params.get("endpoint_min_fanout", 4)))
    fallback_min_fanout = max(1, int(params.get("fallback_min_fanout", 64)))
    passes = max(1, min(int(params.get("passes", 2)), 4))
    route_directive = str(params.get("route_directive", "NoTimingRelaxation") or "")
    route_tns_cleanup = bool(params.get("route_tns_cleanup", True))
    pre_place_post_place_opt = bool(params.get("pre_place_post_place_opt", True))
    post_rep_phys_opt = str(params.get("post_rep_phys_opt", "AggressiveFanoutOpt") or "")
    final_phys_opt = str(params.get("final_phys_opt", "Explore") or "")
    final_route = bool(params.get("final_route", False))
    max_threads = params.get("max_threads")

    route_cmd_parts = ["route_design"]
    if route_directive:
        route_cmd_parts.extend(["-directive", route_directive])
    if route_tns_cleanup:
        route_cmd_parts.append("-tns_cleanup")
    route_cmd = " ".join(route_cmd_parts)

    rep_lines = [
        "phys_opt_design -force_replication_on_nets $nets"
        for _ in range(passes)
    ]
    flow = ["route_design -unroute"]
    if pre_place_post_place_opt:
        flow.append("place_design -post_place_opt")
    flow.extend(rep_lines)
    if post_rep_phys_opt:
        flow.append(f"phys_opt_design -directive {post_rep_phys_opt}")
    flow.append(route_cmd)
    if final_phys_opt:
        flow.append(f"phys_opt_design -directive {final_phys_opt}")
    if final_route:
        flow.append("route_design")

    commands = []
    if max_threads is not None:
        commands.append(f"set_param general.maxThreads {int(max_threads)}")

    commands.extend([
        _build_clock_like_net_filter_tcl(),
        "set fdagents_nets {}",
        (
            "foreach tp "
            f"[get_timing_paths -quiet -setup -max_paths {num_paths} -nworst 1] "
            "{ foreach prop {STARTPOINT_PIN ENDPOINT_PIN} { "
            "set pin [get_property $prop $tp]; "
            "if {$pin eq \"\"} { continue }; "
            "foreach n [get_nets -quiet -of_objects $pin] { "
            "set nname [get_property NAME $n]; "
            "if {[fdagents_clock_like_net $nname]} { continue }; "
            "set fanout 0; catch {set fanout [get_property FLAT_PIN_COUNT $n]}; "
            f"if {{$fanout >= {endpoint_min_fanout}}} {{ lappend fdagents_nets $n }} "
            "} } }"
        ),
        (
            f"if {{[llength $fdagents_nets] < 4}} {{ "
            "foreach n [get_nets -quiet -hierarchical] { "
            "set nname [get_property NAME $n]; "
            "if {[fdagents_clock_like_net $nname]} { continue }; "
            "set fanout 0; catch {set fanout [get_property FLAT_PIN_COUNT $n]}; "
            f"if {{$fanout >= {fallback_min_fanout}}} {{ lappend fdagents_nets $n }}; "
            f"if {{[llength $fdagents_nets] >= {max_nets}}} {{ break }} "
            "} }"
        ),
        "set nets [lsort -unique $fdagents_nets]",
        f"if {{[llength $nets] > {max_nets}}} {{ set nets [lrange $nets 0 {max_nets - 1}] }}",
        "if {[llength $nets] == 0} { error {auto_highfanout_group selected no nets} }",
        'puts "FDAGENTS auto_highfanout_group nets=[llength $nets]"',
        (
            "foreach n $nets { set fanout 0; "
            "catch {set fanout [get_property FLAT_PIN_COUNT $n]}; "
            'puts "FDAGENTS auto_highfanout_group_net [get_property NAME $n] fanout=$fanout" }'
        ),
        *flow,
    ])
    return "; ".join(commands)


class ForceReplicateSkill:
    """Global unroute + double force replication + route recovery."""

    name = "force_replicate"

    async def execute(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        clock_period: Optional[float],
    ) -> SkillResult:
        run_dir = mcp.run_dir
        safe = _safe_name(target)
        output_dcp = run_dir / f"force_replicate_{safe}.dcp"

        passes = int(params.get("passes", 2))
        passes = max(1, min(passes, 4))
        route_directive = params.get("route_directive", "AlternateCLBRouting")
        post_phys_opt = bool(params.get("post_phys_opt", True))
        pre_place_post_place_opt = bool(params.get("pre_place_post_place_opt", False))
        route_tns_cleanup = bool(params.get("route_tns_cleanup", False))
        post_rep_phys_opt = str(params.get("post_rep_phys_opt", "AggressiveExplore") or "")
        final_phys_opt = str(params.get("final_phys_opt", "AggressiveExplore") or "")
        final_route = bool(params.get("final_route", True))
        raw_unroute_scope = params.get("unroute_scope", "global")
        unroute_scope = _normalize_unroute_scope(raw_unroute_scope)
        allow_missing = bool(params.get("allow_missing", False))

        logger.info(
            "[force_replicate] net=%s passes=%d unroute_scope=%s route_directive=%s",
            target,
            passes,
            unroute_scope,
            route_directive,
        )
        if raw_unroute_scope != unroute_scope:
            logger.info(
                "[force_replicate] normalized unroute_scope=%r -> %s",
                raw_unroute_scope,
                unroute_scope,
            )

        try:
            if str(target) == "auto_highfanout_group":
                tcl = _build_auto_highfanout_group_tcl(params)
            else:
                rep_lines = "\n".join(
                    "phys_opt_design -force_replication_on_nets $net"
                    for _ in range(passes)
                )
                unroute_cmd = (
                    "route_design -unroute -nets $net"
                    if unroute_scope == "net"
                    else "route_design -unroute"
                )
                route_cmd_parts = ["route_design"]
                if route_directive:
                    route_cmd_parts.extend(["-directive", str(route_directive)])
                if route_tns_cleanup:
                    route_cmd_parts.append("-tns_cleanup")
                route_cmd = " ".join(route_cmd_parts)

                post_phys_lines = []
                if pre_place_post_place_opt:
                    post_phys_lines.append("place_design -post_place_opt")
                if post_phys_opt:
                    if post_rep_phys_opt:
                        post_phys_lines.append(
                            f"phys_opt_design -directive {post_rep_phys_opt}"
                        )
                    post_phys_lines.append(route_cmd)
                    if final_phys_opt:
                        post_phys_lines.append(
                            f"phys_opt_design -directive {final_phys_opt}"
                        )
                    if final_route:
                        post_phys_lines.append("route_design")
                else:
                    post_phys_lines.append("route_design")

                # VivadoMCP's run_tcl talks to an interactive prompt. Sending a
                # multiline script returns after the first prompt and leaves the
                # remaining lines queued in Vivado. Keep this as one Tcl command.
                have_net_check = "set fdagents_have_net [expr {[llength $net] > 0}]"
                if allow_missing:
                    missing_check = (
                        'if {!$fdagents_have_net} { '
                        'puts "FDAGENTS force_replicate target_not_found=$target" '
                        '} else { '
                        'puts "FDAGENTS force_replicate target=[get_property NAME $net]" '
                        "}"
                    )
                else:
                    missing_check = (
                        'if {!$fdagents_have_net} { '
                        'puts "ERROR: net not found: $target"; '
                        'error "net not found: $target" '
                        '} else { '
                        'puts "FDAGENTS force_replicate target=[get_property NAME $net]" '
                        "}"
                    )
                rep_commands = [
                    f"if {{$fdagents_have_net}} {{ {line} }}"
                    for line in rep_lines.splitlines()
                ]

                commands = [
                    f"set target {_tcl_quote(target)}",
                    "set net [get_nets -quiet $target]",
                    (
                        "if {[llength $net] == 0} { "
                        "foreach candidate [get_nets -hier -quiet *] { "
                        "set candidate_name [get_property NAME $candidate]; "
                        "if {[string first $target $candidate_name] >= 0} { "
                        "set net $candidate; break "
                        "} "
                        "} "
                        "} else { "
                        "set net [lindex $net 0] "
                        "}"
                    ),
                    have_net_check,
                    missing_check,
                    unroute_cmd,
                    *(
                        ["place_design -post_place_opt"]
                        if pre_place_post_place_opt and post_phys_opt
                        else []
                    ),
                    *rep_commands,
                    *(
                        line
                        for line in post_phys_lines
                        if line != "place_design -post_place_opt"
                    ),
                ]
                tcl = "; ".join(commands)
            await mcp.call_vivado(
                "run_tcl",
                {"command": tcl, "timeout": 3600},
                timeout=3900.0,
            )

            rs = await mcp.call_vivado("report_route_status", {}, timeout=120.0)
            route = parse_route_status_static(rs)
            is_legal = bool(route.get("routed_ok", True))

            after_wns = await mcp.get_wns()
            if after_wns is None:
                ts = await mcp.call_vivado("report_timing_summary", {}, timeout=300.0)
                parsed = parse_timing_summary_static(ts)
                after_wns = parsed.get("wns") or before_wns

            delta = after_wns - before_wns
            if delta > 0.001:
                await mcp.call_vivado(
                    "write_checkpoint",
                    {"dcp_path": str(output_dcp.resolve()), "force": True},
                    timeout=600.0,
                )
            else:
                output_dcp = run_dir / "current.dcp"

            fmax_b = calculate_fmax(before_wns, clock_period)
            fmax_a = calculate_fmax(after_wns, clock_period)
            short_target = target[-50:]
            if fmax_b and fmax_a:
                summary = (
                    f"force_replicate:{short_target} "
                    f"wns {before_wns:.3f}->{after_wns:.3f} "
                    f"fmax {fmax_b:.1f}->{fmax_a:.1f} MHz delta={delta:+.3f}"
                )
            else:
                summary = (
                    f"force_replicate:{short_target} "
                    f"wns {before_wns:.3f}->{after_wns:.3f} delta={delta:+.3f}"
                )

            return SkillResult(
                success=True,
                before_wns=before_wns,
                after_wns=after_wns,
                delta_wns=delta,
                is_legal=is_legal,
                output_dcp=output_dcp,
                summary=summary,
            )

        except Exception as e:
            logger.error("[force_replicate] %s failed: %s", target, e)
            return SkillResult.failure(before_wns, str(e), output_dcp)
