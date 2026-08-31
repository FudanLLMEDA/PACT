"""
critical_net_reroute skill -- route-preserving reroute of small critical net sets.

This is a generic Vivado-only micro ECO. It does not remember benchmark or
instance names; it extracts current critical-path pins, resolves their nets in
the open design, reroutes only small candidate net windows, and accepts only
legal routed checkpoints.
"""

from __future__ import annotations

import json
import hashlib
import logging
import re
from pathlib import Path
from typing import Any, Optional

from .base import (
    SkillResult,
    calculate_fmax,
    parse_route_status_static,
    parse_timing_summary_static,
)

logger = logging.getLogger(__name__)

_DATA_PIN_RE = re.compile(r"/(?:I[0-5]|O|Q|D)$")
_DSP_SUBPRIM_RE = re.compile(r"/DSP_[A-Z_]+_INST/")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _tcl_quote(value: str) -> str:
    return "{" + value.replace("\\", "\\\\").replace("}", "\\}") + "}"


def _safe_name(value: str) -> str:
    return _SAFE_NAME_RE.sub("_", value).strip("_") or "candidate"


def _path_pins(path: Any) -> list[str]:
    if isinstance(path, list):
        return [str(pin) for pin in path if isinstance(pin, str)]
    if isinstance(path, dict):
        pins = path.get("pins") or path.get("path") or path.get("cell_pins") or []
        if isinstance(pins, list):
            return [str(pin) for pin in pins if isinstance(pin, str)]
    return []


def _candidate_pin_windows(
    critical_paths: list,
    *,
    num_paths: int = 5,
    max_pins: int = 12,
    window_size: int = 4,
    max_candidates: int = 6,
) -> list[list[str]]:
    """Build small ordered pin windows from worst critical paths."""
    windows: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    window_size = max(1, int(window_size))

    for path in critical_paths[: max(1, num_paths)]:
        data_pins = []
        for pin in _path_pins(path):
            if _DSP_SUBPRIM_RE.search(pin):
                continue
            if not _DATA_PIN_RE.search(pin):
                continue
            if pin not in data_pins:
                data_pins.append(pin)
            if len(data_pins) >= max_pins:
                break

        if not data_pins:
            continue
        if len(data_pins) <= window_size:
            candidates = [data_pins]
        else:
            candidates = [
                data_pins[start : start + window_size]
                for start in range(0, len(data_pins) - window_size + 1)
            ]

        for candidate in candidates:
            key = tuple(candidate)
            if key in seen:
                continue
            seen.add(key)
            windows.append(candidate)
            if len(windows) >= max_candidates:
                return windows

    return windows


def _parse_discovered_nets(output: str) -> list[str]:
    """Parse FDAGENTS_NETS lines emitted by the discovery Tcl."""
    nets: list[str] = []
    for line in str(output).splitlines():
        stripped = line.strip()
        if not stripped.startswith("FDAGENTS_NETS"):
            continue
        _, _, payload = stripped.partition(" ")
        for item in payload.split("|"):
            net = item.strip()
            if net and net not in nets:
                nets.append(net)
    return nets


_TIMING_NET_RE = re.compile(
    r"net\s+\(fo=(?P<fanout>\d+),[^)]*\)\s+"
    r"(?P<delay>-?\d+(?:\.\d+)?)\s+"
    r"-?\d+(?:\.\d+)?\s+"
    r"(?P<name>\S+)\s*$"
)


def _parse_ranked_route_nets(
    timing_report: str,
    *,
    max_nets: int,
    max_fanout: int,
    min_delay_ns: float = 0.0,
) -> list[dict[str, Any]]:
    """Extract route-delay-ranked nets from a textual Vivado timing report."""
    ranked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in str(timing_report).splitlines():
        match = _TIMING_NET_RE.search(line)
        if not match:
            continue
        name = match.group("name")
        if name in seen:
            continue
        fanout = int(match.group("fanout"))
        delay = float(match.group("delay"))
        if fanout > max_fanout or delay < min_delay_ns:
            continue
        if re.search(
            r"(^|/)(clk|clock)(/|$)|bufg|mmcm|pll|wclk|rclk|wrclk|rdclk|txoutclk|rxoutclk|userclk|coreclk",
            name,
            re.IGNORECASE,
        ):
            continue
        seen.add(name)
        ranked.append({"name": name, "fanout": fanout, "delay": delay})

    ranked.sort(key=lambda item: float(item["delay"]), reverse=True)
    return ranked[: max(1, int(max_nets))]


def _build_discovery_tcl(pin_window: list[str], max_fanout: int) -> str:
    pins = " ".join(_tcl_quote(pin) for pin in pin_window)
    loop_body = "; ".join(
        [
            "set pin [get_pins -quiet $pin_name]",
            "if {[llength $pin] == 0} { continue }",
            "set net [get_nets -quiet -of_objects $pin]",
            "if {[llength $net] == 0} { continue }",
            "set net [lindex $net 0]",
            "set net_name [get_property NAME $net]",
            (
                "if {[regexp -nocase "
                "{(^|/)(clk|clock)(/|$)|bufg|mmcm|pll|wclk|rclk|wrclk|rdclk|"
                "txoutclk|rxoutclk|userclk|coreclk} $net_name]} { continue }"
            ),
            "set sinks [filter [get_pins -quiet -of_objects $net] {DIRECTION == IN}]",
            f"if {{[llength $sinks] > {int(max_fanout)}}} {{ continue }}",
            (
                "if {[lsearch -exact $fdagents_net_names $net_name] < 0} "
                "{ lappend fdagents_net_names $net_name }"
            ),
        ]
    )
    return "; ".join(
        [
            "puts {FDAGENTS_DISCOVER_CRITICAL_NET_REROUTE}",
            f"set fdagents_pin_names [list {pins}]",
            "set fdagents_net_names {}",
            f"foreach pin_name $fdagents_pin_names {{ {loop_body} }}",
            "puts \"FDAGENTS_NETS [join $fdagents_net_names {|}]\"",
        ]
    )


def _build_reroute_tcl(
    net_names: list[str],
    *,
    route_mode: str = "delay_group",
    post_phys_opt: Optional[str] = None,
    pressure_path_count: int = 0,
    pressure_value_ns: Optional[float] = None,
    source_xdc: Optional[str] = None,
) -> str:
    nets = " ".join(_tcl_quote(net) for net in net_names)
    route_mode = str(route_mode or "delay_group").strip().lower()
    pressure_path_count = max(0, int(pressure_path_count or 0))

    if route_mode == "explore_group":
        route_lines = [
            "route_design -unroute -nets $nets",
            "route_design -directive Explore",
        ]
    elif route_mode == "auto_group":
        route_lines = [
            "route_design -unroute -nets $nets",
            (
                "if {[catch {route_design -nets $nets -auto_delay} fdagents_err]} "
                "{ puts \"FDAGENTS_AUTO_ROUTE_FAILED $fdagents_err\"; "
                "route_design -nets $nets }"
            ),
            "route_design -preserve -tns_cleanup",
        ]
    elif route_mode == "nt_group":
        route_lines = [
            "route_design -unroute -nets $nets",
            (
                "if {[catch {route_design -directive NoTimingRelaxation -nets $nets} fdagents_err]} "
                "{ puts \"FDAGENTS_NT_ROUTE_FAILED $fdagents_err\"; "
                "route_design -nets $nets }"
            ),
            "route_design -preserve -tns_cleanup",
        ]
    else:
        route_lines = [
            "route_design -unroute -nets $nets",
            (
                "if {[catch {route_design -nets $nets -delay} fdagents_err]} "
                "{ puts \"FDAGENTS_DELAY_ROUTE_FAILED $fdagents_err\"; "
                "route_design -nets $nets }"
            ),
            "route_design -preserve -tns_cleanup",
        ]

    if post_phys_opt:
        route_lines.append(f"phys_opt_design -directive {str(post_phys_opt).strip()}")

    commands = [
        f"set fdagents_net_names [list {nets}]",
        "set nets [get_nets -quiet $fdagents_net_names]",
        "if {[llength $nets] == 0} { error {FDAGENTS no nets to reroute} }",
    ]
    if pressure_path_count and pressure_value_ns is not None:
        commands.extend(
            [
                "set fdagents_pressure_pairs {}",
                (
                    "foreach path "
                    f"[get_timing_paths -quiet -setup -max_paths {pressure_path_count} -nworst 1] "
                    "{ set sp [get_property STARTPOINT_PIN $path]; "
                    "set ep [get_property ENDPOINT_PIN $path]; "
                    "if {$sp ne \"\" && $ep ne \"\"} { "
                    f"if {{![catch {{set_max_delay {float(pressure_value_ns):.3f} -datapath_only -from $sp -to $ep}}]}} "
                    "{ lappend fdagents_pressure_pairs [list $sp $ep] } "
                    "} }"
                ),
            ]
        )
    commands.extend(route_lines)
    if pressure_path_count and pressure_value_ns is not None:
        commands.append("reset_timing -quiet")
        if source_xdc:
            commands.append(f"read_xdc {_tcl_quote(str(source_xdc))}")
        commands.append("update_timing")
    return "; ".join(commands)


def _candidate_output_path(run_dir: Path, idx: int, net_names: list[str]) -> Path:
    digest = hashlib.sha1("\n".join(net_names).encode("utf-8")).hexdigest()[:12]
    return run_dir / f"critical_net_reroute_{idx:02d}_{digest}.dcp"


class CriticalNetRerouteSkill:
    """Sweep small critical net windows with route-preserving Vivado ECO."""

    name = "critical_net_reroute"

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
        output_dcp = run_dir / "critical_net_reroute_best.dcp"
        num_paths = int(params.get("num_paths", 5))
        max_pins = int(params.get("max_pins", 12))
        window_size = int(params.get("window_size", 4))
        max_candidates = int(params.get("max_candidates", 6))
        max_fanout = int(params.get("max_fanout", 80))
        max_nets = int(params.get("max_nets", max_pins))
        selection_mode = str(params.get("selection_mode", "window")).strip().lower()
        route_mode = str(params.get("route_mode", "delay_group"))
        pressure_path_count = int(params.get("pressure_path_count", 0))
        pressure_value_ns = params.get("pressure_value_ns")
        source_xdc = None

        logger.info(
            "[critical_net_reroute] target=%r paths=%d max_candidates=%d",
            target,
            num_paths,
            max_candidates,
        )

        try:
            await mcp.call_vivado(
                "open_checkpoint",
                {"dcp_path": str(input_dcp)},
                timeout=600.0,
            )
            if pressure_path_count and pressure_value_ns is not None:
                source_xdc = str((run_dir / "critical_net_reroute_source.xdc").resolve())
                await mcp.call_vivado(
                    "run_tcl",
                    {
                        "command": f"write_xdc -force {_tcl_quote(source_xdc)}",
                        "timeout": 300,
                    },
                    timeout=360.0,
                )
            direct_net_candidates: list[dict[str, Any]] = []
            windows: list[list[str]] = []
            if selection_mode in {"high_delay", "top3"}:
                timing_report = await mcp.call_vivado(
                    "run_tcl",
                    {
                        "command": (
                            "report_timing -return_string -delay_type max "
                            f"-max_paths {max(1, num_paths)} -nworst 1 -sort_by slack"
                        )
                    },
                    timeout=600.0,
                )
                direct_net_candidates = _parse_ranked_route_nets(
                    str(timing_report),
                    max_nets=max_nets,
                    max_fanout=max_fanout,
                    min_delay_ns=float(params.get("min_route_delay_ns", 0.0)),
                )
                if not direct_net_candidates:
                    return SkillResult.failure(
                        before_wns,
                        f"no ranked route nets found for selection_mode={selection_mode}",
                        output_dcp,
                    )
            else:
                pins_file = run_dir / "critical_net_reroute_pins.json"
                await mcp.call_vivado(
                    "extract_critical_path_pins",
                    {"num_paths": num_paths, "output_file": str(pins_file)},
                    timeout=600.0,
                )
                if not pins_file.exists():
                    return SkillResult.failure(
                        before_wns,
                        "extract_critical_path_pins produced no file",
                        output_dcp,
                    )

                critical_paths = json.loads(pins_file.read_text())
                windows = _candidate_pin_windows(
                    critical_paths,
                    num_paths=num_paths,
                    max_pins=max_pins,
                    window_size=window_size,
                    max_candidates=max_candidates,
                )
                if not windows:
                    return SkillResult.failure(
                        before_wns,
                        "no critical data-pin windows found",
                        output_dcp,
                    )

            attempts: list[dict] = []
            best: Optional[dict] = None
            seen_net_sets: set[tuple[str, ...]] = set()

            candidate_specs: list[dict[str, Any]] = []
            if direct_net_candidates:
                candidate_specs.append(
                    {
                        "pins": [],
                        "nets": [str(item["name"]) for item in direct_net_candidates],
                        "ranked_nets": direct_net_candidates,
                    }
                )
            else:
                candidate_specs = [{"pins": window} for window in windows]

            for idx, candidate_spec in enumerate(candidate_specs, 1):
                pin_window = list(candidate_spec.get("pins") or [])
                await mcp.call_vivado(
                    "open_checkpoint",
                    {"dcp_path": str(input_dcp)},
                    timeout=600.0,
                )
                if candidate_spec.get("nets"):
                    net_names = list(candidate_spec["nets"])
                else:
                    discovered = await mcp.call_vivado(
                        "run_tcl",
                        {"command": _build_discovery_tcl(pin_window, max_fanout)},
                        timeout=300.0,
                    )
                    net_names = _parse_discovered_nets(discovered)
                net_key = tuple(net_names)
                attempt = {
                    "candidate": idx,
                    "pins": pin_window,
                    "nets": net_names,
                    "selection_mode": selection_mode,
                }
                if candidate_spec.get("ranked_nets"):
                    attempt["ranked_nets"] = candidate_spec["ranked_nets"]
                if not net_names or net_key in seen_net_sets:
                    attempt["skipped"] = "no_nets_or_duplicate"
                    attempts.append(attempt)
                    continue
                seen_net_sets.add(net_key)

                candidate_dcp = _candidate_output_path(run_dir, idx, net_names)
                try:
                    await mcp.call_vivado(
                        "run_tcl",
                        {
                            "command": _build_reroute_tcl(
                                net_names,
                                route_mode=route_mode,
                                post_phys_opt=params.get("post_phys_opt"),
                                pressure_path_count=pressure_path_count,
                                pressure_value_ns=pressure_value_ns,
                                source_xdc=source_xdc,
                            )
                        },
                        timeout=float(params.get("route_timeout_s", 1800.0)),
                    )
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

                    await mcp.call_vivado(
                        "write_checkpoint",
                        {"dcp_path": str(candidate_dcp.resolve()), "force": True},
                        timeout=600.0,
                    )
                    attempt.update(
                        {
                            "after_wns": after_wns,
                            "delta_wns": after_wns - before_wns,
                            "is_legal": is_legal,
                            "route": route,
                            "output_dcp": str(candidate_dcp),
                        }
                    )
                    if is_legal and (best is None or after_wns > best["after_wns"]):
                        best = attempt
                except Exception as e:
                    attempt.update({"error": str(e), "is_legal": False})
                attempts.append(attempt)

            if best is None:
                return SkillResult.failure(
                    before_wns,
                    "no legal critical_net_reroute candidates: "
                    + json.dumps(attempts, default=str)[:300],
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
                    f"critical_net_reroute nets={len(best['nets'])} "
                    f"wns {before_wns:.3f}->{after_wns:.3f} "
                    f"fmax {fmax_b:.1f}->{fmax_a:.1f} MHz delta={delta:+.3f}"
                )
            else:
                summary = (
                    f"critical_net_reroute nets={len(best['nets'])} "
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
                details=json.dumps(
                    {"best": best, "attempts": attempts},
                    default=str,
                    separators=(",", ":"),
                )[:4000],
            )

        except Exception as e:
            logger.error("[critical_net_reroute] failed: %s", e)
            return SkillResult.failure(before_wns, str(e), output_dcp)
