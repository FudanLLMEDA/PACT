"""
fanout_opt skill — high-fanout net splitting via RapidWright ECO.

Ported from FPGAOptimizerTest.run_test() in dcp_optimizer.py:1782.
Supports batching: optimizes multiple nets, then does a single Vivado route.
"""

import hashlib
import logging
import re
from pathlib import Path
from typing import Optional

from .base import SkillResult, parse_timing_summary_static, parse_route_status_static, calculate_fmax

logger = logging.getLogger(__name__)


def _net_tag(net_name: str) -> str:
    readable = re.sub(r"[^A-Za-z0-9_.-]+", "_", net_name.rsplit("/", 1)[-1]).strip("_")
    digest = hashlib.sha1(net_name.encode("utf-8")).hexdigest()[:10]
    if len(readable) > 36:
        readable = "net"
    return f"{readable}_{digest}" if readable else digest


class FanoutOptSkill:
    """Split high-fanout nets using RapidWright, then re-route in Vivado."""

    name = "fanout_opt"

    async def execute(
        self,
        mcp,
        target: str,                # net name
        params: dict,               # {split_factor: int, input_dcp: str}
        before_wns: float,
        clock_period: Optional[float],
    ) -> SkillResult:
        run_dir = mcp.run_dir
        net_name = target
        split_factor = int(params.get("split_factor", 2))
        input_dcp = params.get("input_dcp")
        tag = _net_tag(net_name)
        output_dcp = run_dir / f"fanout_opt_{tag}.dcp"

        logger.info(f"[fanout_opt] net={net_name} split_factor={split_factor}")

        try:
            # Initialize RapidWright if needed
            await mcp.call_rw(
                "initialize_rapidwright",
                {"jvm_max_memory": "8G"},
                timeout=120.0,
            )

            # Open DCP in RapidWright
            dcp_to_open = input_dcp or str(run_dir / "current_best.dcp")
            await mcp.call_rw(
                "read_checkpoint",
                {"dcp_path": dcp_to_open},
                timeout=600.0,
            )

            # Apply fanout optimization
            result = await mcp.call_rw(
                "optimize_fanout",
                {"net_name": net_name, "split_factor": split_factor},
                timeout=300.0,
            )
            logger.debug(f"[fanout_opt] optimize_fanout result: {result[:500]}")

            if "error" in result.lower() and "success" not in result.lower():
                return SkillResult.failure(before_wns, f"optimize_fanout error: {result[:200]}", output_dcp)

            # Write RapidWright DCP
            rw_dcp = run_dir / f"rw_fanout_{tag}.dcp"
            await mcp.call_rw(
                "write_checkpoint",
                {"dcp_path": str(rw_dcp), "overwrite": True},
                timeout=600.0,
            )

            if not rw_dcp.exists():
                return SkillResult.failure(before_wns, "RapidWright DCP not created", output_dcp)

            # Open in Vivado and re-route
            tcl_script = rw_dcp.with_suffix(".tcl")
            if tcl_script.exists():
                await mcp.call_vivado(
                    "run_tcl",
                    {"command": f"source {{{tcl_script}}}"},
                    timeout=300.0,
                )
            else:
                await mcp.call_vivado(
                    "open_checkpoint",
                    {"dcp_path": str(rw_dcp)},
                    timeout=300.0,
                )

            await mcp.call_vivado(
                "route_design",
                {"directive": "Default"},
                timeout=600.0,
            )

            # Check route status
            rs = await mcp.call_vivado("report_route_status", {"show_errors": True}, timeout=120.0)
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

            fmax_b = calculate_fmax(before_wns, clock_period)
            fmax_a = calculate_fmax(after_wns, clock_period)
            if fmax_b and fmax_a:
                summary = (
                    f"fanout:{net_name[-40:]} split={split_factor} "
                    f"wns {before_wns:.3f}→{after_wns:.3f} fmax {fmax_b:.1f}→{fmax_a:.1f} MHz"
                )
            else:
                summary = f"fanout:{net_name[-40:]} split={split_factor} wns {before_wns:.3f}→{after_wns:.3f} delta={delta:+.3f}"

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
            logger.error(f"[fanout_opt] {net_name} failed: {e}")
            return SkillResult.failure(before_wns, str(e), output_dcp)
