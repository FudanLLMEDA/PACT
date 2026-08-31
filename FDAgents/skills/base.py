"""
Base types and shared utility functions for FDAgents skills.

parse_timing_summary_static and parse_route_status_static are copied verbatim
from dcp_optimizer.py (lines 51 and 142).
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class SkillResult:
    success: bool
    before_wns: float
    after_wns: float
    delta_wns: float        # after - before, positive = improvement
    is_legal: bool          # route_status shows no routing errors
    output_dcp: Path
    summary: str            # compact summary for LLM (<200 chars)
    error_msg: str = ""
    details: Optional[str] = None  # optional full raw response (e.g. custom MCP tool output)

    @classmethod
    def failure(cls, before_wns: float, error_msg: str, output_dcp: Path) -> "SkillResult":
        return cls(
            success=False,
            before_wns=before_wns,
            after_wns=before_wns,
            delta_wns=0.0,
            is_legal=False,
            output_dcp=output_dcp,
            summary=f"FAILED: {error_msg[:150]}",
            error_msg=error_msg,
        )


def parse_timing_summary_static(timing_report: str) -> dict:
    """
    Parse timing summary report to extract WNS, TNS, and failing endpoints.

    Copied from dcp_optimizer.py:51.
    """
    result = {
        "wns": None,
        "tns": None,
        "failing_endpoints": None,
        "total_endpoints": None,
        "whs": None,
        "ths": None,
        "ths_failing_endpoints": None,
        "ths_total_endpoints": None,
        "wpws": None,
        "tpws": None,
        "tpws_failing_endpoints": None,
        "tpws_total_endpoints": None,
        "min_period_required_ns": None,
        "min_period_actual_ns": None,
        "min_period_slack_ns": None,
    }

    lines = timing_report.split("\n")

    def attach_min_period_checks() -> None:
        checks = []
        for item in lines:
            if not item.strip().startswith("Min Period"):
                continue
            nums = re.findall(r"[-+]?\d+\.\d+", item)
            if len(nums) < 3:
                continue
            required, actual, slack = (float(nums[0]), float(nums[1]), float(nums[2]))
            checks.append((required, actual, slack))
        if not checks:
            return
        required, actual, slack = max(checks, key=lambda row: row[0])
        result["min_period_required_ns"] = required
        result["min_period_actual_ns"] = actual
        result["min_period_slack_ns"] = slack

    header_idx = -1
    for i, line in enumerate(lines):
        if "WNS(ns)" in line and "TNS(ns)" in line:
            header_idx = i
            header_line = line
            break

    if header_idx == -1:
        return result
    has_hold = "WHS(ns)" in header_line and "THS(ns)" in header_line
    has_pulse = "WPWS(ns)" in header_line and "TPWS(ns)" in header_line

    data_idx = header_idx + 2
    if data_idx >= len(lines):
        return result

    data_line = lines[data_idx].strip()
    if not data_line:
        return result

    parts = data_line.split()
    if not parts:
        return result

    def parse_float(token: str):
        if token in {"NA", "N/A"}:
            return None
        return float(token)

    def parse_int(token: str):
        if token in {"NA", "N/A"}:
            return None
        return int(token)

    try:
        if len(parts) >= 1:
            result["wns"] = parse_float(parts[0])
        if len(parts) >= 2:
            result["tns"] = parse_float(parts[1])
        if len(parts) >= 3:
            result["failing_endpoints"] = parse_int(parts[2])
        if len(parts) >= 4:
            result["total_endpoints"] = parse_int(parts[3])
        offset = 4
        if has_hold:
            if len(parts) >= offset + 1:
                result["whs"] = parse_float(parts[offset])
            if len(parts) >= offset + 2:
                result["ths"] = parse_float(parts[offset + 1])
            if len(parts) >= offset + 3:
                result["ths_failing_endpoints"] = parse_int(parts[offset + 2])
            if len(parts) >= offset + 4:
                result["ths_total_endpoints"] = parse_int(parts[offset + 3])
            offset += 4
        if has_pulse:
            if len(parts) >= offset + 1:
                result["wpws"] = parse_float(parts[offset])
            if len(parts) >= offset + 2:
                result["tpws"] = parse_float(parts[offset + 1])
            if len(parts) >= offset + 3:
                result["tpws_failing_endpoints"] = parse_int(parts[offset + 2])
            if len(parts) >= offset + 4:
                result["tpws_total_endpoints"] = parse_int(parts[offset + 3])
    except (ValueError, IndexError):
        pass

    attach_min_period_checks()
    return result


def parse_route_status_static(route_report: str) -> dict:
    """
    Parse Vivado report_route_status output.

    Copied from dcp_optimizer.py:142.
    """
    result = {
        "routable_nets": None,
        "fully_routed_nets": None,
        "nets_with_routing_errors": None,
        "routed_ok": None,
    }

    patterns = {
        "routable_nets": r"# of routable nets[.\s:]+(\d+)",
        "fully_routed_nets": r"# of fully routed nets[.\s:]+(\d+)",
        "nets_with_routing_errors": r"# of nets with routing errors[.\s:]+(\d+)",
    }

    for key, pattern in patterns.items():
        match = re.search(pattern, route_report, re.IGNORECASE)
        if match:
            result[key] = int(match.group(1))

    if (
        result["routable_nets"] is not None
        and result["fully_routed_nets"] is not None
        and result["nets_with_routing_errors"] is not None
    ):
        result["routed_ok"] = (
            result["fully_routed_nets"] == result["routable_nets"]
            and result["nets_with_routing_errors"] == 0
        )

    return result


def calculate_fmax(wns: Optional[float], clock_period: Optional[float]) -> Optional[float]:
    """
    Calculate achievable Fmax in MHz.

    Copied from dcp_optimizer.py:363 (DCPOptimizerBase.calculate_fmax).
    """
    if clock_period is None or clock_period <= 0 or wns is None:
        return None
    achievable = clock_period - wns
    if achievable <= 0:
        return None
    return 1000.0 / achievable
