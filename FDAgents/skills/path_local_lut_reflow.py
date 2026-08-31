"""Path-local LUT reflow skill.

This Vivado-only ECO selects primitive LUTs from the current worst setup paths,
unroutes only their adjacent nets, unplaces only those LUTs, then runs a bounded
incremental place/route cleanup. It is intentionally profile-derived and never
uses benchmark, hierarchy, or net names.
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

_MCP_RUN_TCL_TIMEOUT_GRACE_S = 120.0
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_DIRECTIVE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _safe_name(value: str) -> str:
    return _SAFE_NAME_RE.sub("_", value).strip("_") or "auto"


def _safe_directive(value: Optional[str], default: str) -> str:
    text = str(value or default).strip()
    if not _DIRECTIVE_RE.match(text):
        return default
    return text


def _parse_marker_wns(output: str) -> Optional[float]:
    match = re.search(r"FDAGENTS_REFLOW_WNS\s+(-?\d+(?:\.\d+)?)", str(output))
    if not match:
        return None
    return float(match.group(1))


def _candidate_offsets(max_cells: int, max_candidates: int) -> list[int]:
    max_cells = max(1, int(max_cells))
    max_candidates = max(1, int(max_candidates))
    return [idx * max_cells for idx in range(max_candidates)]


def build_path_local_lut_reflow_tcl(
    *,
    num_paths: int,
    max_cells: int,
    candidate_offset: int,
    place_directive: str = "Quick",
    route_directive: str = "eco_preserve",
    post_phys_opt: Optional[str] = None,
) -> str:
    """Build a bounded path-local LUT re-place/re-route Tcl command."""
    num_paths = max(1, int(num_paths))
    max_cells = max(1, int(max_cells))
    candidate_offset = max(0, int(candidate_offset))
    end_index = candidate_offset + max_cells - 1
    place_directive = _safe_directive(place_directive, "Quick")
    route_mode = str(route_directive or "eco_preserve").strip().lower()
    route_directive = _safe_directive(route_directive, "NoTimingRelaxation")
    post_phys_opt = _safe_directive(post_phys_opt, "") if post_phys_opt else ""

    commands = [
        "set fdagents_path_cells {}",
        (
            "foreach tp "
            f"[get_timing_paths -quiet -setup -max_paths {num_paths} -nworst 1 -sort_by slack] "
            "{ foreach cell [get_cells -quiet -of_objects $tp] { "
            "set ref [get_property REF_NAME $cell]; "
            "if {![string match {LUT*} $ref]} { continue }; "
            "if {[get_property IS_PRIMITIVE $cell] != 1} { continue }; "
            "if {[get_property DONT_TOUCH $cell] == 1} { continue }; "
            "set cname [get_property NAME $cell]; "
            "if {[lsearch -exact $fdagents_path_cells $cname] < 0} { "
            "lappend fdagents_path_cells $cname } } }"
        ),
        "set fdagents_unique_cells $fdagents_path_cells",
        (
            "set fdagents_cell_names "
            f"[lrange $fdagents_unique_cells {candidate_offset} {end_index}]"
        ),
        "set fdagents_cells [get_cells -quiet $fdagents_cell_names]",
        "if {[llength $fdagents_cells] == 0} { error {FDAGENTS no LUT cells selected for path-local reflow} }",
        "set fdagents_nets {}",
        (
            "foreach cell $fdagents_cells { "
            "foreach pin [get_pins -quiet -of_objects $cell] { "
            "foreach net [get_nets -quiet -of_objects $pin] { "
            "set nname [get_property NAME $net]; "
            "if {[regexp -nocase {(^|/)(clk|clock)(/|$)|bufg|mmcm|pll} $nname]} { continue }; "
            "if {[lsearch -exact $fdagents_nets $nname] < 0} { lappend fdagents_nets $nname } "
            "} } }"
        ),
        "set fdagents_route_nets [get_nets -quiet $fdagents_nets]",
        'puts "FDAGENTS_PATH_LOCAL_LUT_REFLOW cells=[llength $fdagents_cells] nets=[llength $fdagents_route_nets]"',
        "if {[llength $fdagents_route_nets] > 0} { route_design -unroute -nets $fdagents_route_nets }",
        "unplace_cell $fdagents_cells",
        "set fdagents_prev_lut_drc {}",
        "set fdagents_have_lut_drc [expr {![catch {get_param drc.disableLUTOverUtilError} fdagents_prev_lut_drc]}]",
        "if {$fdagents_have_lut_drc} { catch {set_param drc.disableLUTOverUtilError 1} }",
        (
            f"if {{[catch {{ place_design -directive {place_directive} }} fdagents_place_err]}} "
            "{ if {$fdagents_have_lut_drc} { catch {set_param drc.disableLUTOverUtilError $fdagents_prev_lut_drc} }; "
            "error $fdagents_place_err }"
        ),
        "if {$fdagents_have_lut_drc} { catch {set_param drc.disableLUTOverUtilError $fdagents_prev_lut_drc} }",
    ]
    if route_mode in {"eco", "eco_preserve", "preserve_eco"}:
        commands.extend(
            [
                (
                    "if {[catch { route_design -eco } fdagents_route_err]} { "
                    'puts "FDAGENTS_REFLOW_ROUTE_ECO_FAIL $fdagents_route_err"; '
                    "catch { route_design -nets $fdagents_route_nets } "
                    "}"
                ),
                "catch { route_design -preserve -tns_cleanup }",
            ]
        )
    else:
        commands.append(f"route_design -directive {route_directive}")
    if post_phys_opt:
        commands.append(f"phys_opt_design -directive {post_phys_opt}")
    commands.extend(
        [
            "set tp [get_timing_paths -quiet -setup -max_paths 1]",
            (
                'if {$tp eq {}} { puts "FDAGENTS_REFLOW_WNS NA" } '
                'else { puts "FDAGENTS_REFLOW_WNS [get_property SLACK $tp]" }'
            ),
        ]
    )
    return "; ".join(commands)


class PathLocalLutReflowSkill:
    """Sweep bounded path-local LUT reflow candidates."""

    name = "path_local_lut_reflow"

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
        output_dcp = run_dir / f"path_local_lut_reflow_{_safe_name(target)}.dcp"
        num_paths = int(params.get("num_paths", 8))
        max_cells = int(params.get("max_cells", 12))
        max_candidates = int(params.get("max_candidates", 2))
        max_iterations = max(1, int(params.get("max_iterations", 1)))
        accept_min_delta = float(params.get("accept_min_delta_ns", 0.0))
        place_directive = str(params.get("place_directive", "Quick"))
        route_directive = str(params.get("route_directive", "eco_preserve"))
        post_phys_opt = params.get("post_phys_opt")
        recipe_timeout = float(params.get("recipe_timeout_s", 1200.0))

        attempts: list[dict] = []
        best: Optional[dict] = None
        current_input = str(input_dcp)
        current_before_wns = before_wns
        accepted_iterations = 0

        logger.info(
            "[path_local_lut_reflow] target=%r paths=%d cells=%d candidates=%d iterations=%d",
            target,
            num_paths,
            max_cells,
            max_candidates,
            max_iterations,
        )

        try:
            for iteration in range(1, max_iterations + 1):
                iteration_best: Optional[dict] = None
                for idx, offset in enumerate(
                    _candidate_offsets(max_cells, max_candidates),
                    1,
                ):
                    if max_iterations == 1 or iteration == 1:
                        candidate_dcp = run_dir / (
                            f"path_local_lut_reflow_{idx:02d}_off{offset}.dcp"
                        )
                    else:
                        candidate_dcp = run_dir / (
                            f"path_local_lut_reflow_iter{iteration:02d}_{idx:02d}_off{offset}.dcp"
                        )
                    attempt = {
                        "iteration": iteration,
                        "candidate": idx,
                        "offset": offset,
                        "input_dcp": current_input,
                        "output_dcp": str(candidate_dcp),
                    }
                    try:
                        await mcp.call_vivado(
                            "open_checkpoint",
                            {"dcp_path": current_input},
                            timeout=600.0,
                        )
                        tcl = build_path_local_lut_reflow_tcl(
                            num_paths=num_paths,
                            max_cells=max_cells,
                            candidate_offset=offset,
                            place_directive=place_directive,
                            route_directive=route_directive,
                            post_phys_opt=str(post_phys_opt) if post_phys_opt else None,
                        )
                        tcl_output = await mcp.call_vivado(
                            "run_tcl",
                            {"command": tcl, "timeout": recipe_timeout},
                            timeout=recipe_timeout + _MCP_RUN_TCL_TIMEOUT_GRACE_S,
                        )
                        route_report = await mcp.call_vivado(
                            "report_route_status", {}, timeout=180.0
                        )
                        route = parse_route_status_static(route_report)
                        is_legal = bool(route.get("routed_ok"))
                        after_wns = _parse_marker_wns(tcl_output)
                        if after_wns is None:
                            after_wns = await mcp.get_wns()
                        if after_wns is None:
                            timing_report = await mcp.call_vivado(
                                "report_timing_summary", {}, timeout=300.0
                            )
                            timing = parse_timing_summary_static(timing_report)
                            after_wns = timing.get("wns")
                        if after_wns is None:
                            raise RuntimeError("failed to parse path-local reflow timing")
                        await mcp.call_vivado(
                            "write_checkpoint",
                            {"dcp_path": str(candidate_dcp.resolve()), "force": True},
                            timeout=600.0,
                        )
                        attempt.update(
                            {
                                "after_wns": after_wns,
                                "delta_wns": after_wns - current_before_wns,
                                "is_legal": is_legal,
                                "route": route,
                            }
                        )
                        if is_legal and (
                            iteration_best is None
                            or after_wns > float(iteration_best["after_wns"])
                        ):
                            iteration_best = attempt
                    except Exception as exc:
                        attempt.update({"error": str(exc), "is_legal": False})
                        logger.warning(
                            "[path_local_lut_reflow] iteration %d candidate %d failed: %s",
                            iteration,
                            idx,
                            exc,
                        )
                    attempts.append(attempt)

                if iteration_best is None:
                    break

                if best is None or float(iteration_best["after_wns"]) > float(best["after_wns"]):
                    best = iteration_best

                delta = float(iteration_best["after_wns"]) - current_before_wns
                if delta < accept_min_delta:
                    break

                accepted_iterations += 1
                current_input = str(Path(iteration_best["output_dcp"]).resolve())
                current_before_wns = float(iteration_best["after_wns"])

            if best is None:
                return SkillResult.failure(
                    before_wns,
                    "no legal path-local LUT reflow candidates: "
                    + json.dumps(attempts)[:300],
                    output_dcp,
                )

            output_dcp = Path(best["output_dcp"])
            after_wns = float(best["after_wns"])
            delta = after_wns - before_wns
            await mcp.call_vivado(
                "open_checkpoint",
                {"dcp_path": str(output_dcp.resolve())},
                timeout=600.0,
            )
            fmax_b = calculate_fmax(before_wns, clock_period)
            fmax_a = calculate_fmax(after_wns, clock_period)
            if fmax_b and fmax_a:
                summary = (
                    f"path_local_lut_reflow iterations={accepted_iterations} "
                    f"best={best['iteration']}.{best['candidate']} "
                    f"wns {before_wns:.3f}->{after_wns:.3f} "
                    f"fmax {fmax_b:.1f}->{fmax_a:.1f} MHz delta={delta:+.3f}"
                )
            else:
                summary = (
                    f"path_local_lut_reflow iterations={accepted_iterations} "
                    f"best={best['iteration']}.{best['candidate']} "
                    f"wns {before_wns:.3f}->{after_wns:.3f} delta={delta:+.3f}"
                )
            return SkillResult(
                success=True,
                before_wns=before_wns,
                after_wns=after_wns,
                delta_wns=delta,
                is_legal=True,
                output_dcp=output_dcp,
                summary=summary,
                details=json.dumps({"attempts": attempts}, default=str)[:4000],
            )
        except Exception as exc:
            logger.error("[path_local_lut_reflow] failed: %s", exc)
            return SkillResult.failure(before_wns, str(exc), output_dcp)
