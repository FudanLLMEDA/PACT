"""
endpoint_bel_move skill -- generic same-site endpoint FF/BEL repack.

The skill discovers endpoint registers from current critical timing paths and
tries to move an endpoint FF from AFF..HFF to the paired AFF2..HFF2 BEL in the
same SLICE. It does not accept remembered instance/site names; all candidates
come from live timing in the input checkpoint.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from .base import (
    SkillResult,
    calculate_fmax,
    parse_route_status_static,
    parse_timing_summary_static,
)

logger = logging.getLogger(__name__)


def _endpoint_move_tcl(candidate_index: int, num_paths: int) -> str:
    """Single-command Tcl for VivadoMCP's interactive prompt."""
    return (
        f"set timing_text [report_timing -return_string -max_paths {num_paths} "
        "-delay_type max -sort_by slack -nworst 1]; "
        "set endpoints {}; "
        "foreach line [split $timing_text \"\\n\"] { "
        "if {[regexp {^\\s*Destination:\\s+(\\S+)} $line -> ep_from_report]} { "
        "lappend endpoints $ep_from_report "
        "} "
        "}; "
        "if {[llength $endpoints] == 0} { "
        "set clk [get_clocks -quiet clk_fpl26contest]; "
        "if {[llength $clk] == 0} { set paths [get_timing_paths -quiet "
        f"-delay_type max -max_paths {num_paths} -nworst 1 -setup] "
        "} else { set paths [get_timing_paths -quiet -delay_type max "
        f"-max_paths {num_paths} -nworst 1 -setup -group $clk] "
        "}; "
        "foreach p $paths { "
        "set ep [get_property ENDPOINT_PIN $p]; "
        "if {$ep ne {}} { lappend endpoints $ep } "
        "} "
        "}; "
        "set candidates {}; "
        "foreach ep $endpoints { "
        "if {$ep eq {}} { continue }; "
        "set ep_pin [get_pins -quiet $ep]; "
        "if {[llength $ep_pin] == 0} { set ep_pin $ep }; "
        "set cell [get_cells -quiet -of_objects $ep_pin]; "
        "if {[llength $cell] == 0 && [regexp {^(.+)/[^/]+$} $ep -> endpoint_cell_name]} { "
        "set cell [get_cells -quiet $endpoint_cell_name] "
        "}; "
        "if {[llength $cell] != 1} { continue }; "
        "set ref [get_property REF_NAME $cell]; "
        "if {![regexp {^FD} $ref]} { continue }; "
        "set loc [get_property LOC $cell]; "
        "set bel [get_property BEL $cell]; "
        "if {![regexp {([A-H])FF$} $bel -> bel_letter]} { continue }; "
        "set from_bel ${bel_letter}FF; "
        "set target_bel ${bel_letter}FF2; "
        "set site [get_sites -quiet $loc]; "
        "if {[llength $site] != 1} { continue }; "
        "set target_bel_obj {}; "
        "foreach bel_obj [get_bels -quiet -of_objects $site] { "
        "set target_name [get_property NAME $bel_obj]; "
        "if {![regexp {([A-H]FF2?)$} $target_name -> target_leaf]} { continue }; "
        "if {$target_leaf eq $target_bel} { set target_bel_obj $bel_obj; break } "
        "}; "
        "if {[llength $target_bel_obj] != 1} { continue }; "
        "set occupying_cells [get_cells -quiet -of_objects $target_bel_obj]; "
        "if {[llength $occupying_cells] != 0} { continue }; "
        "lappend candidates [list $cell $loc $from_bel $target_bel] "
        "}; "
        f"if {{[llength $candidates] <= {candidate_index}}} {{ "
        f"error \"no endpoint FF BEL-move candidate at index {candidate_index}\" "
        "}; "
        f"set cand [lindex $candidates {candidate_index}]; "
        "set cell [lindex $cand 0]; "
        "set loc [lindex $cand 1]; "
        "set from_bel [lindex $cand 2]; "
        "set target_bel [lindex $cand 3]; "
        "set route_nets {}; "
        "foreach pin [get_pins -quiet -of_objects $cell] { "
        "foreach net [get_nets -quiet -of_objects $pin] { "
        "set nn [get_property NAME $net]; "
        "if {[regexp -nocase {(^|[/_])(clk|clock)($|[/_])|bufg|mmcm|pll} $nn]} { continue }; "
        "lappend route_nets $net "
        "} "
        "}; "
        "set route_nets [lsort -unique $route_nets]; "
        "if {[llength $route_nets] == 0} { error \"endpoint move has no non-clock route nets\" }; "
        "catch {set_property IS_LOC_FIXED false $cell}; "
        "catch {set_property IS_BEL_FIXED false $cell}; "
        "set_property BEL $target_bel $cell; "
        "set_property LOC $loc $cell; "
        "route_design -unroute -nets $route_nets; "
        "if {[catch {route_design -nets $route_nets -delay} route_msg]} { "
        "route_design -nets $route_nets "
        "}; "
        "route_design -preserve -tns_cleanup; "
        "puts \"FDAGENTS endpoint_bel_move cell=[get_property NAME $cell] "
        "from=$from_bel to=$target_bel nets=[llength $route_nets]\""
    )


class EndpointBelMoveSkill:
    """Try same-site FF->FF2 moves for endpoint registers on critical paths."""

    name = "endpoint_bel_move"

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
        num_paths = max(1, min(int(params.get("num_paths", 12)), 50))
        max_candidates = max(1, min(int(params.get("max_candidates", 6)), 20))
        attempt_timeout = float(params.get("attempt_timeout_s", 1800.0))
        output_dcp = run_dir / "endpoint_bel_move.dcp"
        attempts: list[dict] = []
        best_seen: Optional[dict] = None

        logger.info(
            "[endpoint_bel_move] target=%r paths=%d candidates=%d",
            target,
            num_paths,
            max_candidates,
        )

        try:
            for idx in range(max_candidates):
                candidate_dcp = run_dir / f"endpoint_bel_move_{idx + 1:02d}.dcp"
                attempt = {"candidate_index": idx, "output_dcp": str(candidate_dcp)}
                try:
                    await mcp.call_vivado(
                        "open_checkpoint",
                        {"dcp_path": input_dcp},
                        timeout=600.0,
                    )
                    tcl = _endpoint_move_tcl(idx, num_paths)
                    tool_output = await mcp.call_vivado(
                        "run_tcl",
                        {"command": tcl, "timeout": attempt_timeout},
                        timeout=attempt_timeout,
                    )
                    tool_output_text = str(tool_output)
                    lowered_output = tool_output_text.lower()
                    if (
                        "no endpoint ff bel-move candidate" in lowered_output
                        or "endpoint move has no non-clock route nets" in lowered_output
                        or "error:" in lowered_output
                    ):
                        raise RuntimeError(tool_output_text[:500])

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
                            "tool_output": tool_output_text[:500],
                        }
                    )

                    if is_legal:
                        await mcp.call_vivado(
                            "write_checkpoint",
                            {"dcp_path": str(candidate_dcp.resolve()), "force": True},
                            timeout=600.0,
                        )
                        if best_seen is None or after_wns > best_seen["after_wns"]:
                            best_seen = dict(attempt)

                    logger.info(
                        "[endpoint_bel_move] idx=%d legal=%s wns %.3f delta=%+.3f",
                        idx,
                        is_legal,
                        after_wns,
                        after_wns - before_wns,
                    )
                except Exception as e:
                    attempt.update({"error": str(e), "is_legal": False})
                    logger.info("[endpoint_bel_move] idx=%d skipped: %s", idx, e)

                attempts.append(attempt)

            if best_seen is None:
                return SkillResult.failure(
                    before_wns,
                    f"no legal endpoint BEL moves; attempts={attempts}",
                    output_dcp,
                )

            output_dcp = Path(str(best_seen["output_dcp"]))
            after_wns = float(best_seen["after_wns"])
            await mcp.call_vivado(
                "open_checkpoint",
                {"dcp_path": str(output_dcp.resolve())},
                timeout=600.0,
            )

            fmax_b = calculate_fmax(before_wns, clock_period)
            fmax_a = calculate_fmax(after_wns, clock_period)
            if fmax_b and fmax_a:
                summary = (
                    f"endpoint_bel_move idx={best_seen['candidate_index']} "
                    f"wns {before_wns:.3f}->{after_wns:.3f} "
                    f"fmax {fmax_b:.1f}->{fmax_a:.1f} MHz"
                )
            else:
                summary = (
                    f"endpoint_bel_move idx={best_seen['candidate_index']} "
                    f"wns {before_wns:.3f}->{after_wns:.3f} "
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
                details=str({"attempts": attempts, "best": best_seen}),
            )

        except Exception as e:
            logger.error("[endpoint_bel_move] failed: %s", e)
            return SkillResult.failure(before_wns, str(e), output_dcp)
