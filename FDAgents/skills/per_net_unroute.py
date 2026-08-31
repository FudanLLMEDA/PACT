"""
per_net_unroute skill — surgical fix for a single critical net.

Recipe (vivado tcl only — no RapidWright round-trip):
    set net [get_nets <net>]
    route_design -unroute -nets $net
    phys_opt_design -force_replication_on_nets $net
    phys_opt_design -force_replication_on_nets $net
    route_design

Why per-net unroute (and not a global `route_design -unroute`)?
    A globally-unrouted design loses the rest of its careful local optimum and
    re-routes from scratch — typically dropping 20-50 MHz before phys_opt can
    recover. Per-net unroute preserves every other net's routing; only the
    chosen net's connections get reworked, and the surrounding placement is
    untouched. This is meant for late-stage local cleanup after broader
    placement and replication knobs have saturated.

Target picking:
    The public `auto` target selects one non-clock, mid-fanout net from the
    worst paths before any mutation. Good candidates are the *intermediate*
    high-fanout LUT outputs on the critical path (fanout 20-100 in a cluster),
    NOT the source-FF Q-net of the worst path — Vivado's source-FF rebalancer
    (force_replication on FF outputs) often refuses with "[Physopt 32-745] WNS
    magnitude too large for post-route phys_opt".
"""

import logging
import re
from typing import Optional

from ..parsers import parse_high_fanout_nets
from .base import (
    SkillResult,
    parse_timing_summary_static,
    parse_route_status_static,
    calculate_fmax,
)

logger = logging.getLogger(__name__)

AUTO_TARGET = "auto"
DEFAULT_NUM_PATHS = 5
DEFAULT_MIN_FANOUT = 20
DEFAULT_MAX_FANOUT = 100
DEFAULT_REPLICATION_PASSES = 2
DEFAULT_ROUTE_TIMEOUT_S = 1800.0


def _parse_high_fanout_candidates(report: str, max_fanout: int) -> list[str]:
    """Parse the same bounded table and exact net column as analysis.

    The report appends driver capability columns after the net name.  A local
    regex previously absorbed those columns into the Tcl selector, while a
    narrower path census could independently hide analyzer-certified rows.
    Sharing the parser keeps the discovery contract identical without carrying
    a historical selector into execution.
    """
    candidates = []
    seen = set()
    for net_name, fanout, _path_count in parse_high_fanout_nets(str(report or "")):
        if fanout > max_fanout or net_name in seen:
            continue
        seen.add(net_name)
        candidates.append(net_name)
    return candidates


def _tcl_quote(net_name: str) -> str:
    return "{" + net_name.replace("\\", "\\\\").replace("}", "\\}") + "}"


class PerNetUnrouteSkill:
    """Per-net unroute + double force_replicate + route. Vivado-only."""

    name = "per_net_unroute"

    async def execute(
        self,
        mcp,
        target: str,                # net name
        params: dict,
        before_wns: float,
        clock_period: Optional[float],
    ) -> SkillResult:
        run_dir = mcp.run_dir
        net_name = str(target or AUTO_TARGET).strip() or AUTO_TARGET
        num_paths = int(params.get("num_paths", DEFAULT_NUM_PATHS))
        min_fanout = int(params.get("min_fanout", DEFAULT_MIN_FANOUT))
        max_fanout = int(params.get("max_fanout", DEFAULT_MAX_FANOUT))
        replication_passes = int(
            params.get("replication_passes", DEFAULT_REPLICATION_PASSES)
        )
        route_timeout_s = float(
            params.get("route_timeout_s", DEFAULT_ROUTE_TIMEOUT_S)
        )
        output_dcp = run_dir / "per_net_unroute_auto.dcp"

        try:
            if net_name == AUTO_TARGET:
                discovery = await mcp.call_vivado(
                    "get_critical_high_fanout_nets",
                    {
                        "num_paths": num_paths,
                        "min_fanout": min_fanout,
                        "exclude_clocks": True,
                    },
                    timeout=600.0,
                )
                candidates = _parse_high_fanout_candidates(discovery, max_fanout)
                if not candidates:
                    return SkillResult.failure(
                        before_wns,
                        "auto preflight found no non-clock critical net with "
                        f"fanout in {min_fanout}..{max_fanout}",
                        output_dcp,
                        details={"preflight_candidate_count": 0},
                    )
                net_name = candidates[0]
                logger.info(
                    "[per_net_unroute] auto preflight candidates=%d selected=%s",
                    len(candidates),
                    net_name,
                )

            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", net_name).strip("_") or "net"
            output_dcp = run_dir / f"per_net_unroute_{safe}.dcp"
            net_tcl = _tcl_quote(net_name)
            logger.info(
                "[per_net_unroute] net=%s replication_passes=%d",
                net_name,
                replication_passes,
            )

            # 1. Per-net unroute. If the net doesn't exist or isn't routable,
            # bail out before mutating anything else.
            unroute_cmd = (
                f"set net [get_nets -quiet {net_tcl}]; "
                f"if {{[llength $net] == 0}} {{ "
                f"  puts \"ERROR: net not found\"; "
                f"}} else {{ "
                f"  route_design -unroute -nets $net; "
                f"  puts \"OK\"; "
                f"}}"
            )
            unroute_out = await mcp.call_vivado(
                "run_tcl", {"command": unroute_cmd}, timeout=300.0,
            )
            if "ERROR" in unroute_out and "OK" not in unroute_out:
                return SkillResult.failure(
                    before_wns,
                    f"net not found or unroute failed: {unroute_out[:200]}",
                    output_dcp,
                )

            # 2. Two passes of force_replication on the same net. The second
            # pass is what produces the FF/LUT clone; the first pass alone
            # often only inserts a buffer.
            for i in range(1, replication_passes + 1):
                rep_cmd = (
                    f"set net [get_nets {net_tcl}]; "
                    f"phys_opt_design -force_replication_on_nets $net"
                )
                try:
                    await mcp.call_vivado(
                        "run_tcl", {"command": rep_cmd}, timeout=600.0,
                    )
                except Exception as e:
                    # phys_opt sometimes throws "WNS magnitude too large" on
                    # the second pass — keep going with whatever we have.
                    logger.warning(f"[per_net_unroute] rep{i} warn: {e}")

            # 3. Route the unrouted segment back. Plain route_design is
            # enough — AlternateCLBRouting tends to undo the local optimum.
            await mcp.call_vivado(
                "route_design", {}, timeout=route_timeout_s,
            )

            # 4. Measure timing + route legality.
            rs = await mcp.call_vivado("report_route_status", {}, timeout=120.0)
            route = parse_route_status_static(rs)
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

            fmax_b = calculate_fmax(before_wns, clock_period)
            fmax_a = calculate_fmax(after_wns, clock_period)
            short_net = net_name[-50:]
            if fmax_b and fmax_a:
                summary = (
                    f"per_net_unroute:{short_net} "
                    f"wns {before_wns:.3f}→{after_wns:.3f} "
                    f"fmax {fmax_b:.1f}→{fmax_a:.1f} MHz delta={delta:+.3f}"
                )
            else:
                summary = (
                    f"per_net_unroute:{short_net} "
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
            logger.error(f"[per_net_unroute] {net_name} failed: {e}")
            return SkillResult.failure(before_wns, str(e), output_dcp)
