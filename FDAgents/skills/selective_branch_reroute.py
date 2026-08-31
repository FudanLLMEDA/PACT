"""Certificate-gated RapidWright sink-branch repair with Vivado reroute."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

from .base import (
    SkillOutput,
    SkillResult,
    calculate_fmax,
    open_rapidwright_dcp_in_vivado,
    parse_route_status_static,
)

logger = logging.getLogger(__name__)

SELECTIVE_ROUTE_DIRECTIVE_ALLOWLIST = (
    "Default",
)
MAX_BRANCHES = 4
MAX_CANDIDATES = 4
MAX_CRITICAL_PATHS = 16


def _payload(raw: Any) -> dict:
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        raise ValueError("MCP tool returned a non-object response")
    if value.get("status") == "error" or "error" in value:
        raise RuntimeError(str(value.get("error") or value.get("message")))
    return value


def _critical_paths(raw: Any) -> list[list[str]]:
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, list):
        raise ValueError("critical pin report did not return an inline path list")
    paths = []
    for path in value[:MAX_CRITICAL_PATHS]:
        if isinstance(path, list):
            pins = [pin for pin in path if isinstance(pin, str)][:64]
            if pins:
                paths.append(pins)
    return paths


def _tcl_quote(value: str) -> str:
    return "{" + str(value).replace("\\", "\\\\").replace("}", "\\}") + "}"


def _build_selective_route_tcl(net_names: list[str], directive: str) -> str:
    """Build one fixed-template command; no public raw Tcl reaches this function."""
    if directive not in SELECTIVE_ROUTE_DIRECTIVE_ALLOWLIST:
        raise ValueError(f"route directive {directive!r} is not allowlisted")
    if not net_names or len(net_names) > MAX_BRANCHES:
        raise ValueError(f"net_names must contain 1..{MAX_BRANCHES} affected nets")
    nets = " ".join(_tcl_quote(name) for name in net_names)
    return "; ".join([
        f"set fdagents_branch_net_names [list {nets}]",
        "set fdagents_branch_nets [get_nets -quiet $fdagents_branch_net_names]",
        "if {[llength $fdagents_branch_nets] != [llength $fdagents_branch_net_names]} { error {FDAGENTS affected net missing} }",
        "route_design -preserve",
    ])


def _normalize_verified_connections(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_BRANCHES:
        raise ValueError(f"verified_connections must contain 1..{MAX_BRANCHES} objects")
    result = []
    seen = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or set(item) != {"physical_net", "sink_pin"}:
            raise ValueError(
                f"verified_connections[{index}] must contain only physical_net and sink_pin"
            )
        net = item["physical_net"]
        sink = item["sink_pin"]
        if not isinstance(net, str) or not net or len(net) > 1024:
            raise ValueError(f"verified_connections[{index}].physical_net is invalid")
        if not isinstance(sink, str) or not sink or len(sink) > 1024:
            raise ValueError(f"verified_connections[{index}].sink_pin is invalid")
        key = (net, sink)
        if key in seen:
            raise ValueError(f"duplicate verified connection {net} -> {sink}")
        seen.add(key)
        result.append({"physical_net": net, "sink_pin": sink})
    return result


def _analysis_summary(connection: dict) -> dict:
    return {
        "logical_net": connection.get("logical_net", "unknown"),
        "physical_net": connection.get("physical_net", "unknown"),
        "source_pin": connection.get("source_pin", "unknown"),
        "sink_pin": connection.get("sink_pin", "unknown"),
        "pip_count": connection.get("sink_specific_pip_count", "unknown"),
        "shared_trunk_count": connection.get("shared_trunk_count", "unknown"),
        "manhattan_distance_tiles": connection.get("manhattan_distance_tiles", "unknown"),
        "route_length_tiles": connection.get("route_length_tiles", "unknown"),
        "detour_ratio": connection.get("detour_ratio", "unknown"),
        "recommended_action": connection.get("recommended_action", "reject"),
    }


def _rank_safe_connections(analysis: dict, min_detour_ratio: float) -> list[dict]:
    safe = []
    for connection in analysis.get("connections") or []:
        ratio = connection.get("detour_ratio")
        if (
            connection.get("recommended_action") == "selective_unroute_candidate"
            and isinstance(connection.get("mutation_target"), dict)
            and isinstance(ratio, (int, float))
            and ratio >= min_detour_ratio
        ):
            safe.append(connection)
    safe.sort(
        key=lambda item: (
            float(item.get("detour_ratio", 0.0)),
            int(item.get("sink_specific_pip_count", 0)),
        ),
        reverse=True,
    )
    return safe


def _candidate_path(run_dir: Path, ordinal: int, connection: dict, directive: str) -> Path:
    identity = "|".join([
        str(connection.get("physical_net", "")),
        str(connection.get("sink_pin", "")),
        directive,
    ])
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return run_dir / f"selective_branch_{ordinal:02d}_{digest}.dcp"


class SelectiveBranchRerouteSkill:
    name = "selective_branch_reroute"

    async def execute(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        clock_period: Optional[float],
    ) -> SkillResult:
        started_all = time.monotonic()
        run_dir = mcp.run_dir
        output_dcp = run_dir / "selective_branch_reroute_failed.dcp"
        input_dcp = str(params.get("input_dcp") or run_dir / "current_best.dcp")
        max_branches = int(params.get("max_branches", 2))
        max_candidates = int(params.get("max_candidates", MAX_CANDIDATES))
        num_paths = int(params.get("num_paths", 8))
        min_detour_ratio = float(params.get("min_detour_ratio", 1.3))
        contest_clock = str(params.get("_contest_clock", "clk_fpl26contest"))
        directives = list(params.get("route_directives") or ["Default"])
        attempts = []
        candidates = []

        try:
            if target not in {"auto_high_detour", "verified_connections"}:
                raise ValueError("target must be auto_high_detour or verified_connections")
            if not 1 <= max_branches <= MAX_BRANCHES:
                raise ValueError(f"max_branches must be 1..{MAX_BRANCHES}")
            if not 1 <= max_candidates <= MAX_CANDIDATES:
                raise ValueError(f"max_candidates must be 1..{MAX_CANDIDATES}")
            if not 1 <= num_paths <= MAX_CRITICAL_PATHS:
                raise ValueError(f"num_paths must be 1..{MAX_CRITICAL_PATHS}")
            if not directives or len(directives) > MAX_CANDIDATES:
                raise ValueError(f"route_directives must contain 1..{MAX_CANDIDATES} names")
            if any(item not in SELECTIVE_ROUTE_DIRECTIVE_ALLOWLIST for item in directives):
                raise ValueError("route_directives contains a non-allowlisted value")

            await mcp.call_rw(
                "initialize_rapidwright", {"jvm_max_memory": "8G"}, timeout=120.0
            )
            await mcp.call_vivado(
                "open_checkpoint", {"dcp_path": input_dcp}, timeout=600.0
            )
            seed_route = parse_route_status_static(await mcp.call_vivado(
                "report_route_status", {}, timeout=180.0
            ))
            if not seed_route.get("routed_ok"):
                raise ValueError(
                    "selective preserve reroute requires a fully routed input checkpoint"
                )

            if target == "auto_high_detour":
                raw_paths = await mcp.call_vivado(
                    "extract_critical_path_pins",
                    {"num_paths": num_paths, "clock": contest_clock},
                    timeout=600.0,
                )
                paths = _critical_paths(raw_paths)
                if not paths:
                    raise ValueError("contest-clock critical pin report returned no paths")
                await mcp.call_rw(
                    "read_checkpoint", {"dcp_path": input_dcp}, timeout=600.0
                )
                initial = _payload(await mcp.call_rw(
                    "analyze_route_connections",
                    {
                        "critical_paths_data": paths,
                        "max_connections": min(32, max(8, max_branches * 4)),
                    },
                    timeout=600.0,
                ))
                selected = _rank_safe_connections(initial, min_detour_ratio)[:max_branches]
                if not selected:
                    raise ValueError("no high-detour connection has a safe branch certificate")
                plans = [[{
                    "physical_net": item["physical_net"],
                    "sink_pin": item["sink_pin"],
                }] for item in selected]
            else:
                verified = _normalize_verified_connections(
                    params.get("verified_connections")
                )
                plans = [verified[:max_branches]]

            produced = 0
            for plan_index, plan in enumerate(plans, 1):
                if produced >= max_candidates:
                    break
                plan_started = time.monotonic()

                # Every selection starts from the exact immutable seed. Certificates
                # are deliberately refreshed after this load and never cross siblings.
                await mcp.call_rw(
                    "read_checkpoint", {"dcp_path": input_dcp}, timeout=600.0
                )
                fresh_analysis = _payload(await mcp.call_rw(
                    "analyze_route_connections",
                    {"connections": plan, "max_connections": len(plan)},
                    timeout=600.0,
                ))
                fresh_connections = fresh_analysis.get("connections") or []
                if len(fresh_connections) != len(plan):
                    raise RuntimeError("fresh exact-seed analysis did not resolve every connection")
                mutation_targets = []
                for connection in fresh_connections:
                    if connection.get("recommended_action") != "selective_unroute_candidate":
                        raise RuntimeError(
                            f"fresh analysis rejected {connection.get('physical_net')} -> "
                            f"{connection.get('sink_pin')}: {connection.get('recommended_action')}"
                        )
                    mutation_targets.append(connection["mutation_target"])

                mutation = _payload(await mcp.call_rw(
                    "selective_unroute_sink_branches",
                    {"connections": mutation_targets},
                    timeout=600.0,
                ))
                affected_nets = [
                    str(item["physical_net"])
                    for item in mutation.get("affected_nets") or []
                ]
                if not affected_nets or len(mutation.get("unrouted_sinks") or []) != len(plan):
                    raise RuntimeError("RapidWright selective-unroute postcondition manifest is incomplete")

                # Structural preflight is read-only. The mutation tool already
                # normalized and checked the exact PIP delta before any write.
                _payload(await mcp.call_rw("get_design_info", {}, timeout=300.0))
                rw_dcp = run_dir / f"selective_branch_plan_{plan_index:02d}_rw.dcp"
                _payload(await mcp.call_rw(
                    "write_checkpoint",
                    {"dcp_path": str(rw_dcp), "overwrite": True},
                    timeout=600.0,
                ))
                if not rw_dcp.is_file():
                    raise RuntimeError("RapidWright scratch checkpoint was not created")

                for directive in directives:
                    if produced >= max_candidates:
                        break
                    candidate_started = time.monotonic()
                    candidate_dcp = _candidate_path(
                        run_dir, produced + 1, fresh_connections[0], directive
                    )
                    label = (
                        f"selective_branch_reroute plan={plan_index} "
                        f"branches={len(plan)} route=preserve"
                    )[:120]
                    record = {
                        "label": label,
                        "connection_analysis": [
                            _analysis_summary(item) for item in fresh_connections
                        ],
                        "affected_nets": affected_nets,
                        "preservation": {
                            "preserved_sink_count": mutation.get("preserved_sink_count", 0),
                            "unrouted_sink_count": len(mutation.get("unrouted_sinks") or []),
                            "before_pip_digests": mutation.get("before_pip_digests", {}),
                            "after_pip_digests": mutation.get("after_pip_digests", {}),
                        },
                        "route_directive": directive,
                    }
                    try:
                        await open_rapidwright_dcp_in_vivado(
                            mcp, rw_dcp, timeout=600.0
                        )
                        await mcp.call_vivado(
                            "run_tcl",
                            {"command": _build_selective_route_tcl(affected_nets, directive)},
                            timeout=float(params.get("route_timeout_s", 1200.0)),
                        )
                        route_report = await mcp.call_vivado(
                            "report_route_status", {}, timeout=180.0
                        )
                        route = parse_route_status_static(route_report)
                        record["route_status"] = route
                        record["status"] = "legal" if route.get("routed_ok") else "illegal"
                        local_wns = await mcp.get_wns(
                            contest_clock, allow_fallback=False
                        )
                        record["local_vivado_wns"] = local_wns
                        if route.get("routed_ok"):
                            await mcp.call_vivado(
                                "write_checkpoint",
                                {"dcp_path": str(candidate_dcp.resolve()), "force": True},
                                timeout=600.0,
                            )
                            if candidate_dcp.is_file():
                                candidates.append(SkillOutput(candidate_dcp, label))
                                produced += 1
                    except Exception as exc:
                        record.update({"status": "error", "error": type(exc).__name__})
                    record["runtime_s"] = round(time.monotonic() - candidate_started, 6)
                    attempts.append(record)
                if not attempts:
                    attempts.append({
                        "status": "skipped",
                        "connection_analysis": [
                            _analysis_summary(item) for item in fresh_connections
                        ],
                        "affected_nets": affected_nets,
                        "runtime_s": round(time.monotonic() - plan_started, 6),
                    })

            if not candidates:
                return SkillResult.failure(
                    before_wns,
                    "no fully routed selective branch candidate",
                    output_dcp,
                    details={
                        "connection_analysis_summary": [
                            item for attempt in attempts
                            for item in attempt.get("connection_analysis", [])
                        ],
                        "affected_nets": sorted({
                            net for attempt in attempts
                            for net in attempt.get("affected_nets", [])
                        }),
                        "preservation_results": [
                            attempt.get("preservation") for attempt in attempts
                            if attempt.get("preservation")
                        ],
                        "attempts": attempts,
                        "runtime_s": round(time.monotonic() - started_all, 6),
                        "acceptance_authority": "Vivado central measurement only",
                    },
                )

            output_dcp = candidates[0].output_dcp
            local_values = [
                item.get("local_vivado_wns") for item in attempts
                if isinstance(item.get("local_vivado_wns"), (int, float))
            ]
            after_wns = max(local_values) if local_values else before_wns
            fmax_before = calculate_fmax(before_wns, clock_period)
            fmax_after = calculate_fmax(after_wns, clock_period)
            summary = (
                f"selective_branch_reroute candidates={len(candidates)} "
                f"local_wns={before_wns:.3f}->{after_wns:.3f}"
            )
            if fmax_before is not None and fmax_after is not None:
                summary += f" local_fmax={fmax_before:.1f}->{fmax_after:.1f}MHz"
            return SkillResult(
                success=True,
                before_wns=before_wns,
                after_wns=after_wns,
                delta_wns=after_wns - before_wns,
                is_legal=True,
                output_dcp=output_dcp,
                summary=summary[:200],
                details={
                    "connection_analysis_summary": [
                        item for attempt in attempts
                        for item in attempt.get("connection_analysis", [])
                    ],
                    "affected_nets": sorted({
                        net for attempt in attempts
                        for net in attempt.get("affected_nets", [])
                    }),
                    "preservation_results": [
                        attempt.get("preservation") for attempt in attempts
                        if attempt.get("preservation")
                    ],
                    "attempts": attempts,
                    "runtime_s": round(time.monotonic() - started_all, 6),
                    "acceptance_authority": "Vivado central measurement only",
                },
                candidates=tuple(candidates),
            )
        except Exception as exc:
            logger.error("[selective_branch_reroute] failed: %s", exc)
            return SkillResult.failure(
                before_wns,
                str(exc),
                output_dcp,
                details={
                    "connection_analysis_summary": [],
                    "affected_nets": [],
                    "preservation_results": [],
                    "attempts": attempts,
                    "runtime_s": round(time.monotonic() - started_all, 6),
                    "acceptance_authority": "Vivado central measurement only",
                },
            )
