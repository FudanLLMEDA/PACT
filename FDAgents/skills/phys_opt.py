"""Run one fixed phys_opt_design directive or flag preset."""

import logging
import time
from pathlib import Path
from types import MappingProxyType
from typing import Optional

from .base import (
    SkillResult,
    calculate_fmax,
    parse_route_status_static,
    parse_timing_summary_static,
    unexpected_execution_params,
)

logger = logging.getLogger(__name__)

PHYS_OPT_PRESET_ARGS = MappingProxyType({
    "Default": MappingProxyType({"directive": "Default"}),
    "Explore": MappingProxyType({"directive": "Explore"}),
    "AggressiveExplore": MappingProxyType({"directive": "AggressiveExplore"}),
    "AggressiveFanoutOpt": MappingProxyType({"directive": "AggressiveFanoutOpt"}),
    "AddRetime": MappingProxyType({"directive": "AddRetime"}),
    "equ_drivers": MappingProxyType({"equ_drivers_opt": True}),
    "routing_critical_pin": MappingProxyType(
        {"routing_opt": True, "critical_pin_opt": True}
    ),
    "placement_critical_cell": MappingProxyType(
        {"placement_opt": True, "critical_cell_opt": True}
    ),
    "clock_opt": MappingProxyType({"clock_opt": True}),
})
PHYS_OPT_PRESETS = tuple(PHYS_OPT_PRESET_ARGS)
PHYS_OPT_PATH_GROUP_POLICY = (
    "omitted: VivadoMCP path_groups accepts timing path-group names; "
    "the configured contest clock is a clock object name"
)

_PHYS_OPT_MECHANISMS = {
    "Default": "run Vivado's default routed-design physical optimization",
    "Explore": "explore general routed-design physical optimizations",
    "AggressiveExplore": "run the aggressive general physical-optimization search",
    "AggressiveFanoutOpt": "target high-fanout timing pressure with aggressive replication",
    "AddRetime": "attempt register retiming on timing-limiting sequential paths",
    "equ_drivers": "merge equivalent drivers where the routed design proves equivalence",
    "routing_critical_pin": "optimize routing and critical input-pin assignment",
    "placement_critical_cell": "adjust placement of timing-critical cells",
    "clock_opt": "apply routed clock-network physical optimization",
}


def phys_opt_catalog() -> dict[str, dict[str, object]]:
    """Return fixed preset mechanics for evidence-gated menu construction."""
    return {
        target: {
            "fixed_args": dict(args),
            "mechanism_summary": _PHYS_OPT_MECHANISMS[target],
            "timeout_s": 600.0,
            "destructive_scope": "routed_physical_optimization",
        }
        for target, args in PHYS_OPT_PRESET_ARGS.items()
    }


def resolve_phys_opt_args(target: str) -> dict:
    args = PHYS_OPT_PRESET_ARGS.get(target)
    if args is None:
        raise ValueError(f"unknown phys_opt preset {target!r}")
    return dict(args)


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

    @staticmethod
    def _tool_timeout_s(params: dict, default: float = 600.0) -> float:
        """Let the tool have the window the action was actually granted.

        A fixed 600s cap is unrelated to the window the scheduler hands out,
        and on a large design it throws away an action that was going to
        finish.  Scored, boom_soc_v2 granted phys_opt/AggressiveFanoutOpt
        1891s; phys_opt_design was killed at 600.1s, the restore that followed
        was killed at 600.1s too, restart_vivado at 60s, and the run spent its
        whole 3124s window to produce no candidate at all -- 0.00 MHz where
        this design gains 10.75 here.  Failing early did not save the window;
        it cost it.
        """
        deadline = params.get("_execution_deadline_monotonic")
        if deadline is None:
            return default
        remaining = float(deadline) - time.monotonic()
        if remaining <= 0.0:
            return default
        # Never overrun the granted deadline, and never sit below the timeout
        # that was in force before, so a short window cannot make this worse.
        return max(60.0, remaining)

    async def execute(
        self,
        mcp,
        target: str,                # directive name, e.g. "AggressiveExplore"
        params: dict,
        before_wns: float,
        clock_period: Optional[float],
    ) -> SkillResult:
        try:
            mcp_args = resolve_phys_opt_args(target)
        except ValueError as exc:
            return SkillResult.failure(
                before_wns, str(exc), Path(mcp.run_dir) / "phys_opt_invalid.dcp"
            )
        ignored = unexpected_execution_params(
            params, {"input_dcp", "min_remaining_s"}
        )
        if ignored:
            return SkillResult.failure(
                before_wns,
                f"phys_opt preset parameters are fixed; rejected {ignored}",
                Path(mcp.run_dir) / "phys_opt_invalid.dcp",
            )
        preset = target
        run_dir = mcp.run_dir
        output_dcp = run_dir / f"phys_opt_{preset}.dcp"

        logger.info(
            "[phys_opt] preset=%s args=%s path_groups=%s",
            preset,
            mcp_args,
            PHYS_OPT_PATH_GROUP_POLICY,
        )

        try:
            result = await mcp.call_vivado(
                "phys_opt_design",
                mcp_args,
                timeout=self._tool_timeout_s(params),
            )
            logger.debug(f"[phys_opt] result: {result[:500]}")

            # Surface WARNINGs — they often explain why a directive was a no-op
            warnings = _extract_warnings(result)
            for w in warnings:
                logger.warning(f"[phys_opt/{preset}] {w}")

            # Measure WNS after
            after_wns = await mcp.get_wns()
            if after_wns is None:
                ts = await mcp.call_vivado("report_timing_summary", {}, timeout=300.0)
                parsed = parse_timing_summary_static(ts)
                after_wns = parsed.get("wns") or before_wns

            # Check route status
            rs = await mcp.call_vivado("report_route_status", {}, timeout=120.0)
            route = parse_route_status_static(rs)
            is_legal = bool(route.get("routed_ok"))

            delta = after_wns - before_wns
            if not is_legal:
                return SkillResult.failure(
                    before_wns, "candidate is not fully routed", output_dcp
                )
            await mcp.call_vivado(
                "write_checkpoint",
                {"dcp_path": str(output_dcp.resolve()), "force": True},
                timeout=self._tool_timeout_s(params),
            )

            fmax_before = calculate_fmax(before_wns, clock_period)
            fmax_after = calculate_fmax(after_wns, clock_period)
            if fmax_before and fmax_after:
                summary = (
                    f"phys_opt:{preset} wns {before_wns:.3f}→{after_wns:.3f} "
                    f"fmax {fmax_before:.1f}→{fmax_after:.1f} MHz delta={delta:+.3f}"
                )
            else:
                summary = f"phys_opt:{preset} wns {before_wns:.3f}→{after_wns:.3f} delta={delta:+.3f}"

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
                summary=summary[:120],
            )

        except Exception as e:
            logger.error(f"[phys_opt] {preset} failed: {e}")
            return SkillResult.failure(before_wns, str(e), output_dcp)
