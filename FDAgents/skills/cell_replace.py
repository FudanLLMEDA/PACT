"""
cell_replace skill — critical-path cell re-placement via RapidWright.

Flow: extract_critical_path_pins → analyze_net_detour → optimize_cell_placement → route → verify.
"""

import json
import logging
from pathlib import Path
from typing import Optional

from .base import SkillResult, parse_timing_summary_static, parse_route_status_static, calculate_fmax

logger = logging.getLogger(__name__)


class CellReplaceSkill:
    """Re-place critical-path cells to reduce wire detour."""

    name = "cell_replace"

    async def execute(
        self,
        mcp,
        target: str,                # cell name (or comma-separated list)
        params: dict,               # {detour_threshold: float, input_dcp: str, num_paths: int}
        before_wns: float,
        clock_period: Optional[float],
    ) -> SkillResult:
        run_dir = mcp.run_dir
        detour_threshold = float(params.get("detour_threshold", 2.0))
        input_dcp = params.get("input_dcp")
        num_paths = int(params.get("num_paths", 10))
        output_dcp = run_dir / f"cell_replace_thr{detour_threshold}.dcp"

        logger.info(f"[cell_replace] target={target} detour_threshold={detour_threshold}")

        try:
            # Step 1: Extract critical path pins from Vivado
            pins_file = run_dir / "critical_path_pins.json"
            await mcp.call_vivado(
                "extract_critical_path_pins",
                {"num_paths": num_paths, "output_file": str(pins_file)},
                timeout=600.0,
            )

            if not pins_file.exists():
                return SkillResult.failure(before_wns, "extract_critical_path_pins produced no file", output_dcp)

            critical_paths = json.loads(pins_file.read_text())
            logger.info(f"[cell_replace] extracted {len(critical_paths)} paths")

            # Step 2: Open DCP in RapidWright and analyze detour
            await mcp.call_rw(
                "initialize_rapidwright",
                {"jvm_max_memory": "8G"},
                timeout=120.0,
            )

            dcp_to_open = input_dcp or str(run_dir / "current_best.dcp")
            await mcp.call_rw(
                "read_checkpoint",
                {"dcp_path": dcp_to_open},
                timeout=600.0,
            )

            detour_result = await mcp.call_rw(
                "analyze_net_detour",
                {"input_file": str(pins_file), "detour_threshold": detour_threshold},
                timeout=300.0,
            )

            analysis = json.loads(detour_result) if isinstance(detour_result, str) else detour_result
            if "error" in analysis:
                return SkillResult.failure(before_wns, f"analyze_net_detour: {analysis['error']}", output_dcp)

            # If target cell explicitly provided, use it; otherwise pick from candidates
            if target and target != "auto":
                cell_names = [c.strip() for c in target.split(",")]
            else:
                candidates = analysis.get("candidates", [])
                if not candidates:
                    return SkillResult.failure(before_wns, "No candidates from analyze_net_detour", output_dcp)
                # Focus on worst 2 paths
                cell_names = list({
                    str(c["cell"]) for c in candidates if c.get("path", 0) <= 2
                })
                if not cell_names:
                    cell_names = [str(candidates[0]["cell"])]

            logger.info(f"[cell_replace] targeting cells: {cell_names}")

            # Step 3: Optimize cell placement
            opt_result = await mcp.call_rw(
                "optimize_cell_placement",
                {"cell_names": cell_names},
                timeout=300.0,
            )
            logger.debug(f"[cell_replace] optimize_cell_placement: {opt_result[:500]}")

            # Write RapidWright DCP
            rw_dcp = run_dir / "cell_replace_rw.dcp"
            await mcp.call_rw(
                "write_checkpoint",
                {"dcp_path": str(rw_dcp), "overwrite": True},
                timeout=600.0,
            )

            if not rw_dcp.exists():
                return SkillResult.failure(before_wns, "RapidWright DCP not created", output_dcp)

            # Step 4: Open optimized DCP in Vivado and route
            await mcp.call_vivado(
                "open_checkpoint",
                {"dcp_path": str(rw_dcp)},
                timeout=600.0,
            )

            await mcp.call_vivado(
                "route_design",
                {"directive": "Default"},
                timeout=3600.0,
            )

            # Check route status
            rs = await mcp.call_vivado("report_route_status", {}, timeout=120.0)
            route = parse_route_status_static(rs)
            is_legal = bool(route.get("routed_ok"))

            # Measure WNS
            after_wns = await mcp.get_wns()
            if after_wns is None:
                ts = await mcp.call_vivado("report_timing_summary", {}, timeout=300.0)
                parsed = parse_timing_summary_static(ts)
                after_wns = parsed.get("wns") or before_wns

            delta = after_wns - before_wns

            # Write output DCP
            await mcp.call_vivado(
                "write_checkpoint",
                {"dcp_path": str(output_dcp.resolve()), "force": True},
                timeout=600.0,
            )

            cells_short = ",".join(c.split("/")[-1] for c in cell_names[:3])
            fmax_b = calculate_fmax(before_wns, clock_period)
            fmax_a = calculate_fmax(after_wns, clock_period)
            if fmax_b and fmax_a:
                summary = (
                    f"cell_replace thr={detour_threshold} cells=[{cells_short}] "
                    f"wns {before_wns:.3f}→{after_wns:.3f} fmax {fmax_b:.1f}→{fmax_a:.1f} MHz"
                )
            else:
                summary = (
                    f"cell_replace thr={detour_threshold} cells=[{cells_short}] "
                    f"wns {before_wns:.3f}→{after_wns:.3f} delta={delta:+.3f}"
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
            logger.error(f"[cell_replace] failed: {e}")
            return SkillResult.failure(before_wns, str(e), output_dcp)
