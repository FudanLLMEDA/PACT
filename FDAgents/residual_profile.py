"""Live measured residual-family profiles for bounded action composition."""

from __future__ import annotations

import re
from typing import Any

PROFILE_SCHEMA = "measured_residual_profile_v1"
_SLACK = re.compile(r"Slack \([^)]*\)\s*:\s*([-+]?[0-9]+(?:\.[0-9]+)?)ns")
_SOURCE = re.compile(r"^\s*Source:\s+(\S+)", re.MULTILINE)
_DESTINATION = re.compile(r"^\s*Destination:\s+(\S+)", re.MULTILINE)
_LEVELS = re.compile(r"^\s*Logic Levels:\s*.*?\(([^)]*)\)", re.MULTILINE)
_INDEXED_CELL = re.compile(r"^(.*)\[(\d+)\](?:/[A-Za-z0-9_]+)?$")


def _cell_from_pin(pin: str) -> str:
    return re.sub(r"/(?:D|Q|C|CE|R|S|DIN[A-Z]*\[\d+\]|DOUT[A-Z]*\[\d+\])$", "", pin)


def parse_live_timing_paths(report: str, *, limit: int = 300) -> list[dict[str, Any]]:
    if not isinstance(report, str):
        raise ValueError("timing report must be text")
    sections = re.split(r"(?=Slack \()", report)
    rows = []
    for section in sections:
        slack = _SLACK.search(section)
        source = _SOURCE.search(section)
        destination = _DESTINATION.search(section)
        if slack is None or source is None or destination is None:
            continue
        levels = _LEVELS.search(section)
        rows.append({
            "rank": len(rows) + 1,
            "slack_ns": float(slack.group(1)),
            "source_pin": source.group(1),
            "destination_pin": destination.group(1),
            "logic_levels": levels.group(1) if levels else "",
            "contains_constructive_cells": "fdagents_cacc_" in section,
            "contains_memory_forward_cells": "fdagents_memfwd_" in section,
            "section": section,
        })
        if len(rows) >= limit:
            break
    if not rows:
        raise ValueError("live timing report contained no setup paths")
    return rows


def classify_residual_family(row: dict[str, Any]) -> str:
    source = str(row.get("source_pin") or "")
    destination = str(row.get("destination_pin") or "")
    levels = str(row.get("logic_levels") or "")
    combined = f"{source} {destination}"
    if row.get("contains_memory_forward_cells"):
        return "memory_write_buffer_forward"
    destination_cell = _cell_from_pin(destination)
    if (
        row.get("contains_constructive_cells")
        or ("CARRY" in levels and _INDEXED_CELL.fullmatch(destination_cell))
    ):
        return "constructive_accumulator"
    if (
        "RAMB" in destination
        or "URAM" in source
        or re.search(r"/(?:DIN|ENARDEN|WEA)\[?", destination)
    ):
        return "hard_memory"
    if "DSP_" in levels or "DSP48" in combined:
        return "registered_dsp"
    if re.search(r"/(?:CE|ENARDEN|ENBWREN)$", destination):
        return "control"
    if "CARRY" in levels:
        return "other_carry"
    return "other"


def build_measured_residual_profile(
    report: str,
    *,
    artifact_sha256: str,
    global_wns_ns: float,
    fmax_mhz: float,
    top_n: int = 300,
    source_label: str = "live_vivado_report_timing",
) -> dict:
    rows = parse_live_timing_paths(report, limit=top_n)
    families: dict[str, dict[str, Any]] = {}
    for row in rows:
        family = classify_residual_family(row)
        facts = families.setdefault(family, {
            "best_rank": row["rank"],
            "count": 0,
            "worst_slack_ns": row["slack_ns"],
        })
        facts["count"] += 1
        facts["worst_slack_ns"] = min(
            float(facts["worst_slack_ns"]), float(row["slack_ns"])
        )
    return {
        "schema": PROFILE_SCHEMA,
        "artifact_sha256": artifact_sha256,
        "global_wns_ns": float(global_wns_ns),
        "fmax_mhz": float(fmax_mhz),
        "top_n": int(top_n),
        "families": families,
        "source_artifacts": [source_label],
    }


def constructive_endpoint_stems(report: str, *, limit: int = 300) -> list[str]:
    """Return ranked private indexed destination stems; no semantic claim."""
    stems = []
    for row in parse_live_timing_paths(report, limit=limit):
        cell = _cell_from_pin(row["destination_pin"])
        match = _INDEXED_CELL.fullmatch(cell)
        if match is None or "CARRY" not in row["logic_levels"]:
            continue
        stem = match.group(1)
        if stem not in stems:
            stems.append(stem)
    return stems


def metaloop_hypothesis_counts(report: str, *, limit: int = 300) -> dict[str, int]:
    rows = parse_live_timing_paths(report, limit=limit)
    accumulator_stems = constructive_endpoint_stems(report, limit=limit)
    memory_cells = {
        _cell_from_pin(row["destination_pin"])
        for row in rows
        if classify_residual_family(row) == "hard_memory"
        and (
            "/DINADIN[" in row["destination_pin"]
            or "ram_reg_bram" in row["destination_pin"].lower()
        )
    }
    return {
        "constructive_accumulator_hypothesis_count": int(bool(accumulator_stems)),
        "memory_write_buffer_forward_hypothesis_count": int(bool(memory_cells)),
    }


def metaloop_live_wall_counts(report: str, *, limit: int = 300) -> dict[str, int]:
    """Count current-seed timing paths covered by each gated meta-loop family."""
    rows = parse_live_timing_paths(report, limit=limit)
    return {
        "constructive_accumulator_live_wall_covered_path_count": sum(
            classify_residual_family(row) == "constructive_accumulator"
            for row in rows
        ),
        "memory_write_buffer_forward_live_wall_covered_path_count": sum(
            classify_residual_family(row) == "hard_memory"
            for row in rows
        ),
    }


async def capture_live_timing_report(mcp, *, clock: str, top_n: int = 300) -> str:
    safe_clock = str(clock)
    if re.fullmatch(r"[A-Za-z0-9_./-]+", safe_clock) is None:
        raise ValueError("contest clock is unsafe for residual profiling")
    command = (
        f"set metaloop_clock [get_clocks -quiet {{{safe_clock}}}]; "
        "if {[llength $metaloop_clock] != 1} {error {contest clock unavailable}}; "
        "report_timing -return_string -delay_type max -sort_by slack "
        f"-max_paths {int(top_n)} -nworst 1 -input_pins -to $metaloop_clock"
    )
    return await mcp.call_vivado(
        "run_tcl", {"command": command}, timeout=900.0
    )
