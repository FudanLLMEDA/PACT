"""
phys_opt skill — runs phys_opt_design with multiple Vivado directives.

Tries directives in the order specified by params["directive_order"].
Accepts the first directive that improves WNS, then stops.
"""

import logging
from pathlib import Path
from typing import Optional

from .base import SkillResult, parse_timing_summary_static, parse_route_status_static, calculate_fmax

logger = logging.getLogger(__name__)

DEFAULT_DIRECTIVES = ["Default", "AggressiveExplore", "AggressiveFanoutOpt", "AddRetime"]


def _extract_warnings(output: str, limit: int = 3) -> list[str]:
    """Return the first `limit` WARNING/CRITICAL WARNING lines from a Vivado log."""
    warnings = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith(("WARNING:", "CRITICAL WARNING:")):
            warnings.append(stripped[:200])
            if len(warnings) >= limit:
                break
    return warnings


class PhysOptSkill:
    """Runs phys_opt_design with the given directive."""

    name = "phys_opt"

    async def execute(
        self,
        mcp,
        target: str,                # directive name, e.g. "AggressiveExplore"
        params: dict,
        before_wns: float,
        clock_period: Optional[float],
    ) -> SkillResult:
        directive = target
        run_dir = mcp.run_dir
        output_dcp = run_dir / f"phys_opt_{directive}.dcp"

        logger.info(f"[phys_opt] directive={directive}")

        try:
            # Run phys_opt_design
            result = await mcp.call_vivado(
                "phys_opt_design",
                {"directive": directive},
                timeout=600.0,
            )
            logger.debug(f"[phys_opt] result: {result[:500]}")

            # Surface WARNINGs — they often explain why a directive was a no-op
            warnings = _extract_warnings(result)
            for w in warnings:
                logger.warning(f"[phys_opt/{directive}] {w}")

            # Measure WNS after
            after_wns = await mcp.get_wns()
            if after_wns is None:
                ts = await mcp.call_vivado("report_timing_summary", {}, timeout=300.0)
                parsed = parse_timing_summary_static(ts)
                after_wns = parsed.get("wns") or before_wns

            # Check route status
            rs = await mcp.call_vivado("report_route_status", {}, timeout=120.0)
            route = parse_route_status_static(rs)
            is_legal = bool(route.get("routed_ok", True))  # phys_opt doesn't unroute by default

            # Write checkpoint if improvement
            delta = after_wns - before_wns
            if delta > 0.001:
                await mcp.call_vivado(
                    "write_checkpoint",
                    {"dcp_path": str(output_dcp.resolve()), "force": True},
                    timeout=600.0,
                )
            else:
                output_dcp = run_dir / "current.dcp"  # unchanged

            fmax_before = calculate_fmax(before_wns, clock_period)
            fmax_after = calculate_fmax(after_wns, clock_period)
            if fmax_before and fmax_after:
                summary = (
                    f"phys_opt:{directive} wns {before_wns:.3f}→{after_wns:.3f} "
                    f"fmax {fmax_before:.1f}→{fmax_after:.1f} MHz delta={delta:+.3f}"
                )
            else:
                summary = f"phys_opt:{directive} wns {before_wns:.3f}→{after_wns:.3f} delta={delta:+.3f}"

            # Attribute no-op runs to the first relevant warning, if any
            if abs(delta) < 0.001 and warnings:
                summary += f" | warn: {warnings[0][:120]}"

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
            logger.error(f"[phys_opt] {directive} failed: {e}")
            return SkillResult.failure(before_wns, str(e), run_dir / "current.dcp")
