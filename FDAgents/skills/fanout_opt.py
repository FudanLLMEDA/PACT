"""High-fanout net splitting through a private RapidWright ECO selector."""

import hashlib
import logging
import re
from pathlib import Path
from typing import Optional

from .base import (
    SkillResult,
    calculate_fmax,
    open_rapidwright_dcp_in_vivado,
    parse_route_status_static,
    parse_timing_summary_static,
)

logger = logging.getLogger(__name__)


def _net_tag(net_name: str) -> str:
    readable = re.sub(r"[^A-Za-z0-9_.-]+", "_", net_name.rsplit("/", 1)[-1]).strip("_")
    digest = hashlib.sha1(net_name.encode("utf-8")).hexdigest()[:10]
    if len(readable) > 36:
        readable = "net"
    return f"{readable}_{digest}" if readable else digest


def _failure_class(result_text: str) -> str:
    if "hierarchy_delivery_unavailable" in result_text:
        return "hierarchy_delivery_unavailable"
    if "Couldn't find cell instance" in result_text:
        return "hierarchy_delivery_exception"
    if "unsupported" in result_text.lower():
        return "backend_unsupported"
    return "backend_delivery_error"


class FanoutOptSkill:
    """Split one privately selected high-fanout net, then re-route in Vivado."""

    name = "fanout_opt"

    async def execute(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        clock_period: Optional[float],
    ) -> SkillResult:
        run_dir = mcp.run_dir
        split_factor = int(params.get("split_factor", 2))
        input_dcp = params.get("input_dcp")
        private_targets = params.get("_fanout_auto_targets")
        auto_selected = isinstance(private_targets, list)
        if auto_selected:
            targets = []
            seen = set()
            for item in private_targets:
                name = str(item or "").strip()
                if name and name not in seen:
                    seen.add(name)
                    targets.append(name)
            if not targets or targets[0] != target:
                return SkillResult.failure(
                    before_wns,
                    "fanout_opt private selector inventory is invalid",
                    run_dir / "fanout_opt_invalid_selector.dcp",
                )
        else:
            targets = [target]

        output_dcp = run_dir / f"fanout_opt_{_net_tag(target)}.dcp"
        logger.info(
            "[fanout_opt] selector=%s targets=%d split_factor=%d",
            "auto" if auto_selected else "explicit",
            len(targets),
            split_factor,
        )

        try:
            await mcp.call_rw(
                "initialize_rapidwright",
                {"jvm_max_memory": "8G"},
                timeout=120.0,
            )

            dcp_to_open = input_dcp or str(run_dir / "current_best.dcp")
            selected_net = ""
            failures = []
            for ordinal, net_name in enumerate(targets, start=1):
                # Failed Java ECOs can partially mutate the live design. Each
                # fallback therefore reloads the unchanged authoritative seed.
                await mcp.call_rw(
                    "read_checkpoint",
                    {"dcp_path": dcp_to_open},
                    timeout=600.0,
                )
                try:
                    result = await mcp.call_rw(
                        "optimize_fanout",
                        {"net_name": net_name, "split_factor": split_factor},
                        timeout=300.0,
                    )
                    result_text = str(result)
                except Exception as exc:
                    # MCP translates a structured tool error into an exception.
                    # This is a target-local delivery failure, not a reason to
                    # abandon the ordered private auto inventory.
                    result_text = str(exc)
                logger.debug(
                    "[fanout_opt] target %d/%d result: %s",
                    ordinal,
                    len(targets),
                    result_text[:500],
                )
                if "success" not in result_text.lower():
                    failures.append(_failure_class(result_text))
                    logger.error(
                        "[fanout_opt] private target %d/%d failed: %s",
                        ordinal,
                        len(targets),
                        result_text[:500],
                    )
                    continue
                selected_net = net_name
                break

            if not selected_net:
                message = (
                    f"fanout_opt auto selector exhausted {len(targets)} "
                    "live supported targets"
                    if auto_selected
                    else "fanout_opt explicit target delivery failed"
                )
                return SkillResult.failure(
                    before_wns,
                    message,
                    output_dcp,
                    details={
                        "attempt_count": len(targets),
                        "failure_classes": failures[:16],
                    },
                )

            tag = _net_tag(selected_net)
            output_dcp = run_dir / f"fanout_opt_{tag}.dcp"
            rw_dcp = run_dir / f"rw_fanout_{tag}.dcp"
            await mcp.call_rw(
                "write_checkpoint",
                {"dcp_path": str(rw_dcp), "overwrite": True},
                timeout=600.0,
            )
            if not rw_dcp.is_file() or rw_dcp.stat().st_size <= 0:
                return SkillResult.failure(
                    before_wns, "RapidWright DCP not created", output_dcp
                )

            await open_rapidwright_dcp_in_vivado(mcp, rw_dcp, timeout=300.0)
            await mcp.call_vivado(
                "route_design", {"directive": "Default"}, timeout=600.0
            )
            rs = await mcp.call_vivado(
                "report_route_status", {"show_errors": True}, timeout=120.0
            )
            route = parse_route_status_static(rs)
            is_legal = bool(route.get("routed_ok"))

            after_wns = await mcp.get_wns()
            if after_wns is None:
                ts = await mcp.call_vivado(
                    "report_timing_summary", {}, timeout=300.0
                )
                parsed = parse_timing_summary_static(ts)
                after_wns = parsed.get("wns")
                if after_wns is None:
                    after_wns = before_wns
            delta = after_wns - before_wns

            await mcp.call_vivado(
                "write_checkpoint",
                {"dcp_path": str(output_dcp.resolve()), "force": True},
                timeout=600.0,
            )
            if not output_dcp.is_file() or output_dcp.stat().st_size <= 0:
                return SkillResult.failure(
                    before_wns, "Vivado output DCP not created", output_dcp
                )

            subject = (
                "framework-selected live net"
                if auto_selected
                else selected_net[-40:]
            )
            fmax_b = calculate_fmax(before_wns, clock_period)
            fmax_a = calculate_fmax(after_wns, clock_period)
            if fmax_b and fmax_a:
                summary = (
                    f"fanout:{subject} split={split_factor} "
                    f"wns {before_wns:.3f}->{after_wns:.3f} "
                    f"fmax {fmax_b:.1f}->{fmax_a:.1f} MHz"
                )
            else:
                summary = (
                    f"fanout:{subject} split={split_factor} "
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
        except Exception as exc:
            logger.error("[fanout_opt] delivery failed: %s", exc)
            return SkillResult.failure(before_wns, str(exc), output_dcp)
