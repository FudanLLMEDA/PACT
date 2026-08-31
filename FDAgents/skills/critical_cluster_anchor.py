"""
critical_cluster_anchor skill -- generic critical-path bbox anchoring.

This captures the reusable critical-locality pattern without benchmark names:
collect cells and nets from current worst timing paths, build a soft SLICE bbox
around the placed critical cells, unplace only those cells, then ECO place and
preserve-route the selected local nets.
"""

from __future__ import annotations

import logging
from typing import Optional

from .base import (
    SkillResult,
    calculate_fmax,
    parse_route_status_static,
    parse_timing_summary_static,
)

logger = logging.getLogger(__name__)


def _adaptive_cluster_parameters(params: dict) -> dict[str, int]:
    """Scale discovery breadth from configured lower bounds and path count."""
    num_paths = max(1, min(int(params.get("num_paths", 12)), 64))
    base_cells = max(1, int(params.get("max_cells", 24)))
    cell_cap = max(base_cells, int(params.get("max_cells_cap", 96)))
    cells_per_path = max(1, int(params.get("cells_per_path", 4)))
    max_cells = min(cell_cap, max(base_cells, num_paths * cells_per_path))

    base_nets = max(0, int(params.get("max_nets", 12)))
    net_cap = max(base_nets, int(params.get("max_nets_cap", 48)))
    nets_per_path = max(0, int(params.get("nets_per_path", 2)))
    max_nets = min(net_cap, max(base_nets, num_paths * nets_per_path))

    base_pad_x = max(0, int(params.get("bbox_pad_x", 4)))
    base_pad_y = max(0, int(params.get("bbox_pad_y", 8)))
    scale_steps = max(0, (max_cells - 1) // base_cells)
    bbox_pad_x = base_pad_x + scale_steps * max(
        0, int(params.get("bbox_pad_x_per_scale", 2))
    )
    bbox_pad_y = base_pad_y + scale_steps * max(
        0, int(params.get("bbox_pad_y_per_scale", 4))
    )
    return {
        "num_paths": num_paths,
        "max_cells": max_cells,
        "max_nets": max_nets,
        "bbox_pad_x": bbox_pad_x,
        "bbox_pad_y": bbox_pad_y,
    }


def _tcl_quote(value: str) -> str:
    return "{" + value.replace("\\", "\\\\").replace("}", "\\}") + "}"


def _critical_cluster_anchor_tcl(
    *,
    num_paths: int = 12,
    max_cells: int = 24,
    max_nets: int = 12,
    bbox_pad_x: int = 4,
    bbox_pad_y: int = 8,
) -> str:
    """Build a single-prompt Tcl command for timing-driven cluster anchoring."""
    num_paths = max(1, int(num_paths))
    max_cells = max(1, int(max_cells))
    max_nets = max(0, int(max_nets))
    bbox_pad_x = max(0, int(bbox_pad_x))
    bbox_pad_y = max(0, int(bbox_pad_y))

    collect = (
        # Vivado 2025.1 returns Tcl-iterable object collections and does not
        # define the Synopsys-style ``foreach_in_collection`` command.
        "foreach path $fdagents_paths { "
        "foreach pin [get_pins -quiet -of_objects $path] { "
        "set cell [lindex [get_cells -quiet -of_objects $pin] 0]; "
        "if {$cell ne \"\" && [lsearch -exact $fdagents_cells $cell] < 0} { "
        "set loc [get_property LOC $cell]; "
        "if {[regexp {^SLICE_X([0-9]+)Y([0-9]+)$} $loc -> x y]} { "
        "lappend fdagents_cells $cell; "
        "if {$minx < 0 || $x < $minx} { set minx $x }; "
        "if {$maxx < 0 || $x > $maxx} { set maxx $x }; "
        "if {$miny < 0 || $y < $miny} { set miny $y }; "
        "if {$maxy < 0 || $y > $maxy} { set maxy $y }; "
        "} "
        "}; "
        "set net [lindex [get_nets -quiet -of_objects $pin] 0]; "
        "if {$net ne \"\" && [llength $fdagents_nets] < __MAX_NETS__} { "
        "set net_name [get_property NAME $net]; "
        "set is_clock 0; catch {set is_clock [get_property IS_CLOCK $net]}; "
        "if {$net_name ne \"\" && ![regexp -nocase {(^|/)(clk|clock)(/|$)|bufg|mmcm|pll|^(GND|VCC)$} $net_name] && !($is_clock eq \"1\" || $is_clock eq \"true\")} { "
        "if {[lsearch -exact $fdagents_nets $net] < 0} { lappend fdagents_nets $net } "
        "} "
        "}; "
        "if {[llength $fdagents_cells] >= __MAX_CELLS__} { break } "
        "}; "
        "if {[llength $fdagents_cells] >= __MAX_CELLS__} { break } "
        "}"
    )
    collect = collect.replace("__MAX_NETS__", str(max_nets)).replace(
        "__MAX_CELLS__", str(max_cells)
    )

    commands = [
        "update_timing",
        (
            "set fdagents_paths "
            f"[get_timing_paths -quiet -setup -max_paths {num_paths} -nworst 1]"
        ),
        "set fdagents_cells {}; set fdagents_nets {}; set minx -1; set maxx -1; set miny -1; set maxy -1",
        collect,
        "if {[llength $fdagents_cells] == 0 || $minx < 0} { error {FDAGENTS cluster_anchor_bbox no placed SLICE critical cells} }",
        # A path may cross one member of a packed LUT/MUX or LUT/FF shape.
        # Unplacing only that member can leave an impossible partial shape and
        # make the placer finish with UNKNOWN unplaced instances.  Expand the
        # mutation scope from the exact current seed to every movable primitive
        # sharing the selected SLICE sites before creating the local pblock.
        "set fdagents_sites [get_sites -quiet -of_objects $fdagents_cells]",
        "set fdagents_site_cells [get_cells -quiet -of_objects $fdagents_sites -filter {IS_PRIMITIVE == 1 && IS_LOC_FIXED != 1 && IS_BEL_FIXED != 1}]",
        "if {[llength $fdagents_site_cells] > 0} { set fdagents_cells $fdagents_site_cells }",
        f"set minx [expr {{$minx - {bbox_pad_x}}}]; if {{$minx < 0}} {{ set minx 0 }}",
        f"set miny [expr {{$miny - {bbox_pad_y}}}]; if {{$miny < 0}} {{ set miny 0 }}",
        f"set maxx [expr {{$maxx + {bbox_pad_x}}}]",
        f"set maxy [expr {{$maxy + {bbox_pad_y}}}]",
        "set fdagents_range \"SLICE_X${minx}Y${miny}:SLICE_X${maxx}Y${maxy}\"",
        "catch {delete_pblocks [get_pblocks -quiet fdagents_cluster_anchor]}",
        "create_pblock fdagents_cluster_anchor",
        "add_cells_to_pblock [get_pblocks fdagents_cluster_anchor] $fdagents_cells",
        "resize_pblock [get_pblocks fdagents_cluster_anchor] -add $fdagents_range",
        "set_property IS_SOFT true [get_pblocks fdagents_cluster_anchor]",
        "if {[llength $fdagents_nets] > 0} { route_design -unroute -nets $fdagents_nets }",
        "unplace_cell $fdagents_cells",
        # ``place_design -eco`` is Versal-only in Vivado 2025.1.  Emitting it
        # under ``catch`` is still unsafe because the MCP preserves Vivado's
        # ERROR line as a generated-Tcl failure.  Use the portable fallback
        # directly on every architecture.
        "place_design -directive ExtraTimingOpt",
        "phys_opt_design -placement_opt",
        (
            "if {[catch {route_design -preserve} fdagents_route_err]} { "
            "puts \"FDAGENTS cluster_anchor_bbox preserve_route_failed=$fdagents_route_err\"; "
            "route_design "
            "}"
        ),
        "phys_opt_design -routing_opt",
        "puts \"FDAGENTS cluster_anchor_bbox range=$fdagents_range cells=[llength $fdagents_cells] nets=[llength $fdagents_nets]\"",
    ]
    return "; ".join(commands)


class CriticalClusterAnchorSkill:
    """Timing-driven soft bbox anchor around current critical SLICE cells."""

    name = "critical_cluster_anchor"

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
        output_dcp = run_dir / "critical_cluster_anchor.dcp"
        adaptive = _adaptive_cluster_parameters(params)
        num_paths = adaptive["num_paths"]
        max_cells = adaptive["max_cells"]
        max_nets = adaptive["max_nets"]
        bbox_pad_x = adaptive["bbox_pad_x"]
        bbox_pad_y = adaptive["bbox_pad_y"]

        logger.info(
            "[critical_cluster_anchor] paths=%d max_cells=%d max_nets=%d",
            num_paths,
            max_cells,
            max_nets,
        )

        try:
            await mcp.call_vivado(
                "open_checkpoint",
                {"dcp_path": str(input_dcp)},
                timeout=600.0,
            )
            await mcp.call_vivado(
                "run_tcl",
                {
                    "command": _critical_cluster_anchor_tcl(
                        num_paths=num_paths,
                        max_cells=max_cells,
                        max_nets=max_nets,
                        bbox_pad_x=bbox_pad_x,
                        bbox_pad_y=bbox_pad_y,
                    ),
                    "timeout": 3600,
                },
                timeout=3900.0,
            )

            route_report = await mcp.call_vivado("report_route_status", {}, timeout=120.0)
            route = parse_route_status_static(route_report)
            is_legal = bool(route.get("routed_ok"))

            after_wns = await mcp.get_wns()
            if after_wns is None:
                ts = await mcp.call_vivado("report_timing_summary", {}, timeout=300.0)
                parsed = parse_timing_summary_static(ts)
                after_wns = parsed.get("wns") or before_wns

            delta = after_wns - before_wns
            if not is_legal:
                return SkillResult.failure(
                    before_wns, "candidate is not fully routed", output_dcp
                )
            await mcp.call_vivado(
                "write_checkpoint",
                {"dcp_path": str(output_dcp.resolve()), "force": True},
                timeout=600.0,
            )

            fmax_before = calculate_fmax(before_wns, clock_period)
            fmax_after = calculate_fmax(after_wns, clock_period)
            if fmax_before and fmax_after:
                summary = (
                    "critical_cluster_anchor "
                    f"wns {before_wns:.3f}->{after_wns:.3f} "
                    f"fmax {fmax_before:.1f}->{fmax_after:.1f} MHz "
                    f"delta={delta:+.3f}"
                )
            else:
                summary = (
                    "critical_cluster_anchor "
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
            logger.error("[critical_cluster_anchor] failed: %s", e)
            return SkillResult.failure(before_wns, str(e), output_dcp)
