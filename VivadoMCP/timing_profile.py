"""Compact timing-path profiling helpers for Vivado report_timing output."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any


_CLOCK_NET_RE = re.compile(
    r"(^clk$|/clk$|_clk_|clock|BUFG|MMCM|PLL|TXOUTCLK|RXOUTCLK|USERCLK|CORECLK)",
    re.IGNORECASE,
)
_SITE_RE = re.compile(r"\b(SLICE|DSP48E2|RAMB18|RAMB36|URAM288)_X(\d+)Y(\d+)\b")
_SPREAD_SPAN_X_THRESHOLD = 30
_SPREAD_SPAN_Y_THRESHOLD = 50


def build_timing_path_profile(report: str, max_paths: int = 50) -> dict[str, Any]:
    """Parse a Vivado timing report into a compact, LLM-friendly profile."""
    paths = _parse_timing_paths(report)[:max_paths]

    if not paths:
        return {
            "path_count": 0,
            "dominant_bottleneck": "unknown",
            "summary": "No timing paths parsed from report_timing output.",
        }

    slacks = [p["slack_ns"] for p in paths if p.get("slack_ns") is not None]
    route_pcts = [p["route_pct"] for p in paths if p.get("route_pct") is not None]
    logic_pcts = [p["logic_pct"] for p in paths if p.get("logic_pct") is not None]
    logic_levels = [p["logic_levels"] for p in paths if p.get("logic_levels") is not None]

    net_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "name": "",
            "fanout": 0,
            "path_ids": set(),
            "occurrences": 0,
            "total_delay_ns": 0.0,
            "max_delay_ns": 0.0,
        }
    )
    ref_hist = Counter()
    sources = Counter()
    destinations = Counter()
    site_type_hist = Counter()
    all_slice_sites = []
    path_spreads = []

    for idx, path in enumerate(paths, 1):
        if path.get("source"):
            sources[path["source"]] += 1
        if path.get("destination"):
            destinations[path["destination"]] += 1
        ref_hist.update(path.get("logic_histogram", {}))
        sites = path.get("sites", [])
        site_type_hist.update(site["type"] for site in sites)
        all_slice_sites.extend(site for site in sites if site["type"] == "SLICE")
        if path.get("placement_spread"):
            path_spreads.append(path["placement_spread"])
        for net in path.get("nets", []):
            name = net["name"]
            if _is_clock_net(name):
                continue
            entry = net_stats[name]
            entry["name"] = name
            entry["fanout"] = max(entry["fanout"], net["fanout"])
            entry["path_ids"].add(idx)
            entry["occurrences"] += 1
            entry["total_delay_ns"] += net["delay_ns"]
            entry["max_delay_ns"] = max(entry["max_delay_ns"], net["delay_ns"])

    top_nets = []
    for entry in net_stats.values():
        top_nets.append(
            {
                "name": entry["name"],
                "fanout": entry["fanout"],
                "path_count": len(entry["path_ids"]),
                "occurrences": entry["occurrences"],
                "total_delay_ns": round(entry["total_delay_ns"], 3),
                "max_delay_ns": round(entry["max_delay_ns"], 3),
            }
        )
    top_nets.sort(
        key=lambda item: (
            -item["path_count"],
            -item["fanout"],
            -item["max_delay_ns"],
            item["name"],
        )
    )

    dominant = _classify_bottleneck(paths, top_nets)
    avg_route_pct = _avg(route_pcts)
    avg_logic_pct = _avg(logic_pcts)
    avg_logic_levels = _avg(logic_levels)
    placement_spread = _aggregate_placement_spread(
        all_slice_sites,
        path_spreads,
        site_type_hist,
    )

    profile = {
        "path_count": len(paths),
        "worst_slack_ns": min(slacks) if slacks else None,
        "avg_slack_ns": round(_avg(slacks), 3) if slacks else None,
        "avg_route_pct": round(avg_route_pct, 1) if route_pcts else None,
        "avg_logic_pct": round(avg_logic_pct, 1) if logic_pcts else None,
        "dominant_bottleneck": dominant,
        "route_dominated_paths": sum(
            1 for p in paths if (p.get("route_pct") or 0.0) >= 65.0
        ),
        "logic_levels": {
            "avg": round(avg_logic_levels, 1) if logic_levels else None,
            "max": max(logic_levels) if logic_levels else None,
        },
        "logic_histogram": dict(ref_hist.most_common(12)),
        "top_sources": _counter_top(sources, 8),
        "top_destinations": _counter_top(destinations, 8),
        "top_nets": top_nets[:12],
        "path_samples": [
            {
                "slack_ns": p.get("slack_ns"),
                "source": p.get("source"),
                "destination": p.get("destination"),
                "route_pct": p.get("route_pct"),
                "logic_levels": p.get("logic_levels"),
                "span_x": (p.get("placement_spread") or {}).get("span_x"),
                "span_y": (p.get("placement_spread") or {}).get("span_y"),
                "top_net": p.get("nets", [{}])[0].get("name") if p.get("nets") else None,
            }
            for p in paths[:5]
        ],
    }
    if placement_spread:
        profile["placement_spread"] = placement_spread
    return profile


def _parse_timing_paths(report: str) -> list[dict[str, Any]]:
    sections = re.split(r"(?=^Slack \()", report, flags=re.MULTILINE)
    paths = []
    for section in sections:
        if not section.startswith("Slack ("):
            continue
        path = _parse_timing_path_section(section)
        if path:
            paths.append(path)
    return paths


def _parse_timing_path_section(section: str) -> dict[str, Any]:
    slack = _float_match(r"Slack \(.*?\)\s*:\s*(-?\d+(?:\.\d+)?)ns", section)
    source = _str_match(r"^\s*Source:\s+(\S+)", section)
    destination = _str_match(r"^\s*Destination:\s+(\S+)", section)

    data = re.search(
        r"Data Path Delay:\s*(-?\d+(?:\.\d+)?)ns\s+"
        r"\(logic\s+(-?\d+(?:\.\d+)?)ns\s+\((\d+(?:\.\d+)?)%\)\s+"
        r"route\s+(-?\d+(?:\.\d+)?)ns\s+\((\d+(?:\.\d+)?)%\)\)",
        section,
    )
    data_path_delay = logic_delay = logic_pct = route_delay = route_pct = None
    if data:
        data_path_delay = float(data.group(1))
        logic_delay = float(data.group(2))
        logic_pct = float(data.group(3))
        route_delay = float(data.group(4))
        route_pct = float(data.group(5))

    levels_match = re.search(r"Logic Levels:\s*(\d+)\s*(?:\((.*?)\))?", section)
    logic_levels = None
    logic_hist = {}
    if levels_match:
        logic_levels = int(levels_match.group(1))
        logic_hist = _parse_logic_histogram(levels_match.group(2) or "")

    nets = []
    sites = []
    for line in section.splitlines():
        net = _parse_net_line(line)
        if net:
            nets.append(net)
        sites.extend(_parse_sites(line))

    nets.sort(key=lambda item: (-item["delay_ns"], -item["fanout"], item["name"]))
    placement_spread = _path_placement_spread(sites)

    return {
        "slack_ns": slack,
        "source": source,
        "destination": destination,
        "data_path_delay_ns": data_path_delay,
        "logic_delay_ns": logic_delay,
        "logic_pct": logic_pct,
        "route_delay_ns": route_delay,
        "route_pct": route_pct,
        "logic_levels": logic_levels,
        "logic_histogram": logic_hist,
        "nets": nets,
        "sites": sites,
        "placement_spread": placement_spread,
    }


def _parse_net_line(line: str) -> dict[str, Any] | None:
    if "net (fo=" not in line:
        return None
    match = re.search(r"net\s+\(fo=(\d+),\s*([^)]+)\)\s+(-?\d+(?:\.\d+)?)", line)
    if not match:
        return None
    parts = line.split()
    if not parts:
        return None
    name = parts[-1]
    if name in {"(IN)", "(OUT)"}:
        return None
    return {
        "name": name,
        "fanout": int(match.group(1)),
        "route_status": match.group(2).strip(),
        "delay_ns": float(match.group(3)),
    }


def _parse_logic_histogram(text: str) -> dict[str, int]:
    hist = {}
    for key, value in re.findall(r"([A-Za-z0-9_]+)=(\d+)", text):
        hist[key] = hist.get(key, 0) + int(value)
    return hist


def _parse_sites(line: str) -> list[dict[str, Any]]:
    sites = []
    for match in _SITE_RE.finditer(line):
        sites.append(
            {
                "type": match.group(1),
                "x": int(match.group(2)),
                "y": int(match.group(3)),
            }
        )
    return sites


def _path_placement_spread(sites: list[dict[str, Any]]) -> dict[str, Any] | None:
    slice_sites = [site for site in sites if site["type"] == "SLICE"]
    if not slice_sites:
        return None
    return _spread_from_sites(slice_sites)


def _aggregate_placement_spread(
    all_slice_sites: list[dict[str, Any]],
    path_spreads: list[dict[str, Any]],
    site_type_hist: Counter,
) -> dict[str, Any] | None:
    if not all_slice_sites and not path_spreads:
        return None

    bbox = _spread_from_sites(all_slice_sites) if all_slice_sites else {}
    span_xs = [spread["span_x"] for spread in path_spreads]
    span_ys = [spread["span_y"] for spread in path_spreads]
    spread_path_count = sum(
        1
        for spread in path_spreads
        if spread["span_x"] >= _SPREAD_SPAN_X_THRESHOLD
        or spread["span_y"] >= _SPREAD_SPAN_Y_THRESHOLD
    )

    return {
        "path_count_with_sites": len(path_spreads),
        "site_type_counts": dict(site_type_hist.most_common()),
        "bbox_span_x": bbox.get("span_x"),
        "bbox_span_y": bbox.get("span_y"),
        "avg_path_span_x": round(_avg(span_xs), 1) if span_xs else None,
        "avg_path_span_y": round(_avg(span_ys), 1) if span_ys else None,
        "max_path_span_x": max(span_xs) if span_xs else None,
        "max_path_span_y": max(span_ys) if span_ys else None,
        "spread_path_count": spread_path_count,
        "spread_threshold": {
            "span_x": _SPREAD_SPAN_X_THRESHOLD,
            "span_y": _SPREAD_SPAN_Y_THRESHOLD,
        },
    }


def _spread_from_sites(sites: list[dict[str, Any]]) -> dict[str, Any]:
    xs = [site["x"] for site in sites]
    ys = [site["y"] for site in sites]
    return {
        "x_min": min(xs),
        "x_max": max(xs),
        "y_min": min(ys),
        "y_max": max(ys),
        "span_x": max(xs) - min(xs),
        "span_y": max(ys) - min(ys),
    }


def _classify_bottleneck(paths: list[dict[str, Any]], top_nets: list[dict[str, Any]]) -> str:
    route_pcts = [p["route_pct"] for p in paths if p.get("route_pct") is not None]
    logic_pcts = [p["logic_pct"] for p in paths if p.get("logic_pct") is not None]
    levels = [p["logic_levels"] for p in paths if p.get("logic_levels") is not None]

    avg_route = _avg(route_pcts)
    avg_logic = _avg(logic_pcts)
    avg_levels = _avg(levels)
    repeated_high_fanout = any(
        n["fanout"] >= 100 and n["path_count"] >= 3 for n in top_nets
    )

    if repeated_high_fanout:
        return "fanout"
    if avg_route >= 65.0:
        return "routing"
    if avg_levels >= 4.0 and avg_logic >= 35.0:
        return "logic_depth"
    return "mixed"


def _counter_top(counter: Counter, limit: int) -> list[dict[str, Any]]:
    return [
        {"name": name, "path_count": count}
        for name, count in counter.most_common(limit)
    ]


def _is_clock_net(name: str) -> bool:
    return bool(_CLOCK_NET_RE.search(name))


def _avg(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _float_match(pattern: str, text: str) -> float | None:
    match = re.search(pattern, text, flags=re.MULTILINE)
    return float(match.group(1)) if match else None


def _str_match(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, flags=re.MULTILINE)
    return match.group(1) if match else None
