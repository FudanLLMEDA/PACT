"""Vivado report parsers used by the agent's initial analysis.

Moved verbatim from the old agent.py so the MCP tool output formats keep
parsing identically.
"""

from __future__ import annotations

import re


def parse_high_fanout_nets(report: str) -> list[tuple[str, int, int]]:
    """Parse critical high-fanout net report -> [(name, fanout, paths), ...]"""
    nets: list[tuple[str, int, int]] = []
    in_section = False
    for line in report.split("\n"):
        if "Paths" in line and "Fanout" in line and "Parent Net Name" in line:
            in_section = True
            continue
        if not in_section:
            continue
        if line.startswith("---") or not line.strip():
            continue
        if line.startswith("==="):
            break
        parts = line.split()
        if len(parts) >= 3:
            try:
                path_count = int(parts[0])
                fanout = int(parts[1])
                net_name = parts[2]
                if not net_name.startswith(("get_", "ERROR", "WARNING")):
                    nets.append((net_name, fanout, path_count))
            except ValueError:
                continue
    return nets


def parse_high_fanout_timing_evidence(report: str) -> dict[str, dict[str, float | int]]:
    """Parse delay ownership retained for each executable parent-net target."""
    evidence: dict[str, dict[str, float | int]] = {}
    pattern = re.compile(
        r"^\s*(\d+)\s+(\d+)\s+(\S+).*?"
        r"TimingTotalDelayNs=(-?\d+(?:\.\d+)?)\s+"
        r"TimingMaxDelayNs=(-?\d+(?:\.\d+)?)\s*$"
    )
    for line in report.splitlines():
        match = pattern.match(line)
        if match is None:
            continue
        paths, fanout, net_name, total_delay, max_delay = match.groups()
        evidence[net_name] = {
            "path_count": int(paths),
            "fanout": int(fanout),
            "total_delay_ns": max(0.0, float(total_delay)),
            "max_delay_ns": max(0.0, float(max_delay)),
        }
    return evidence


def parse_high_fanout_capabilities(report: str) -> dict[str, dict[str, str]]:
    """Return exact RapidWright fanout backend compatibility by net name."""
    capabilities: dict[str, dict[str, str]] = {}
    pattern = re.compile(
        r"^\s*\d+\s+\d+\s+(\S+)\s+"
        r"DriverType=(\S+)\s+RapidWrightFanout=(supported|unsupported|unknown)"
        r"(?:\s+.*)?$"
    )
    for line in report.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        net_name, driver_type, status = match.groups()
        capabilities[net_name] = {
            "driver_type": driver_type,
            "rapidwright_fanout": status,
        }
    return capabilities


def parse_utilization_report(report: str) -> dict:
    """Parse key resource counts and LUT utilization from report_utilization."""
    result: dict = {}
    for line in report.splitlines():
        if "|" not in line:
            continue
        fields = [field.strip() for field in line.split("|") if field.strip()]
        if len(fields) < 2:
            continue
        name = fields[0].lower()
        used_text = fields[1].replace(",", "")
        try:
            used = int(float(used_text))
        except ValueError:
            continue
        pct = None
        if len(fields) >= 5:
            try:
                pct = float(fields[-1].replace("%", "").replace(",", ""))
            except ValueError:
                pct = None

        if name == "clb luts":
            result["luts"] = used
            if pct is not None:
                result["lut_util_pct"] = pct
        elif name == "clb registers":
            result["ffs"] = used
        elif name == "dsps":
            result["dsp"] = used
        elif name == "block ram tile":
            result["bram"] = used
        elif name == "uram":
            result["uram"] = used
    return result


def parse_congestion_report(report: str) -> dict:
    """Parse compact placer/router congestion-window statistics."""
    result = {
        "placer": {"window_count": 0, "max_level": None},
        "router": {"window_count": 0, "max_level": None},
    }
    section = None
    saw_section = False
    columns: dict[str, int] = {}

    for raw_line in report.splitlines():
        line = raw_line.strip()
        lowered = line.lower()
        if "placer final level congestion reporting" in lowered:
            section = "placer"
            saw_section = True
            columns = {}
            continue
        if "router initial congestion" in lowered:
            section = "router"
            saw_section = True
            columns = {}
            continue
        if "slr net crossing reporting" in lowered:
            section = None
            columns = {}
            continue
        if section is None:
            continue

        no_windows = re.search(
            r"no congestion windows are found above level\s+(\d+)",
            lowered,
        )
        if no_windows:
            level = int(no_windows.group(1))
            result[section]["no_windows_above_level"] = level
            continue

        if "|" not in line:
            continue
        fields = [field.strip() for field in line.strip("|").split("|")]
        normalized = [field.lower() for field in fields]
        if all(name in normalized for name in ("direction", "type", "level", "window")):
            columns = {name: normalized.index(name) for name in normalized}
            continue
        if not columns or columns.get("level", len(fields)) >= len(fields):
            continue

        match = re.search(r"\d+", fields[columns["level"]])
        if not match:
            continue
        level = int(match.group(0))
        bucket = result[section]
        bucket["window_count"] += 1
        previous = bucket["max_level"]
        bucket["max_level"] = level if previous is None else max(previous, level)

    return result if saw_section else {}
