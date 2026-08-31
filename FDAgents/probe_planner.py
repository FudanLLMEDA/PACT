"""Generic information-value cards for current-checkpoint read-only probes."""

from __future__ import annotations

from collections.abc import Mapping


PROBE_DECISION_METADATA = {
    "report_route_status": (
        "route_completion_status", 80.0,
        "check whether unrouted or partially routed connections own the wall",
    ),
    "report_timing_summary": (
        "global_timing_status", 95.0,
        "recover missing setup, hold, pulse-width, and clock timing status",
    ),
    "get_wns": (
        "scalar_setup_slack", 20.0,
        "recover the current setup slack when no bound value is available",
    ),
    "get_critical_high_fanout_nets": (
        "critical_fanout_census", 90.0,
        "identify whether live high-fanout nets own critical timing paths",
    ),
    "extract_critical_path_cells": (
        "critical_path_cell_structure", 85.0,
        "recover logic and physical structure along current critical paths",
    ),
    "extract_critical_path_pins": (
        "critical_path_pin_structure", 75.0,
        "recover pin-level boundaries along current critical paths",
    ),
    "report_timing_path_profile": (
        "timing_delay_composition", 100.0,
        "separate route, logic-depth, fanout, and placement ownership",
    ),
    "analyze_carry_clouds": (
        "carry_chain_structure", 65.0,
        "test whether carry-chain topology contributes to the live wall",
    ),
    "analyze_hard_macro_cascades": (
        "hard_macro_cascade_structure", 65.0,
        "test whether hard-macro cascade topology contributes to the live wall",
    ),
    "get_design_info": (
        "independent_design_structure", 30.0,
        "recover an independent current-checkpoint structural summary",
    ),
    "report_approx_timing": (
        "independent_approximate_timing", 60.0,
        "obtain an independent structural timing estimate when timing facts are missing",
    ),
}


def probe_fact_already_available(tool: str, evidence: Mapping[str, object]) -> bool:
    availability = evidence.get("telemetry_availability")
    availability = availability if isinstance(availability, Mapping) else {}
    profile = evidence.get("timing_profile")
    has_profile = bool(
        availability.get("timing_profile") is True
        or isinstance(profile, Mapping) and int(profile.get("path_count") or 0) > 0
    )
    if tool == "report_timing_summary":
        return bool(evidence.get("timing_summary"))
    if tool == "report_route_status":
        return bool(
            evidence.get("route_legal") is True
            or evidence.get("route_status")
        )
    if tool == "get_wns":
        return isinstance(evidence.get("baseline_wns"), (int, float)) or isinstance(
            evidence.get("wns"), (int, float)
        )
    if tool == "get_critical_high_fanout_nets":
        return bool(availability.get("fanout") is True or evidence.get("fanout_nets"))
    if tool in {
        "extract_critical_path_cells",
        "extract_critical_path_pins",
        "report_timing_path_profile",
        "report_approx_timing",
    }:
        return has_profile
    if tool == "analyze_carry_clouds":
        return bool(evidence.get("carry_clouds"))
    if tool == "analyze_hard_macro_cascades":
        return bool(evidence.get("hard_macro_cascades"))
    if tool == "get_design_info":
        return bool(evidence.get("utilization") and has_profile)
    return False


def probe_decision_display(tool: str) -> dict:
    observation_kind, ranking_score, why_now = PROBE_DECISION_METADATA.get(
        tool,
        (
            f"registered_{tool}",
            10.0,
            "close one missing fact about the exact current checkpoint",
        ),
    )
    return {
        "action_family": "probe",
        "observation_kind": observation_kind,
        "decision_confidence": "uncertain",
        "ranking_score": ranking_score,
        "why_now": why_now,
        "expected_mechanism": "read-only current-session observation",
        "risk": "low",
    }


__all__ = [
    "PROBE_DECISION_METADATA",
    "probe_decision_display",
    "probe_fact_already_available",
]
