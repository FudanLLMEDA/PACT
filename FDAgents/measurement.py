"""Authoritative central measurement of canonical candidate checkpoints."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Optional

from . import legality
from .config import Config
from .skills.base import (
    calculate_fmax,
    parse_route_status_static,
    parse_timing_summary_static,
)


def failed_measurement(reason: str = "") -> dict:
    return {
        "clock_period_ns": None,
        "wns_ns": None,
        "fmax_mhz": None,
        "tns_ns": None,
        "failing_endpoints": None,
        "total_endpoints": None,
        "whs_ns": None,
        "wpws_ns": None,
        "route_legal": None,
        "placed_legal": None,
        "unplaced_primitives": None,
        "hold_legal": None,
        "pulse_legal": None,
        "clock_identity_preserved": None,
        "timing_xdc_preserved": None,
        "constraint_identity_preserved": None,
        "measurement_complete": False,
        "eligible_for_beam": False,
        "legality_reason": reason,
    }


def measurement_legality(measurement: dict) -> Optional[bool]:
    """Return observed physical legality, preserving incomplete data as unknown."""
    flags = [
        measurement.get("placed_legal"),
        measurement.get("route_legal"),
        measurement.get("hold_legal"),
        measurement.get("pulse_legal"),
    ]
    # New central measurements always bind the contest clock and timing XDC.
    # Historical transaction records predate that field; retain their already
    # observed four-way legality instead of silently reclassifying them as an
    # unknown measurement during resume or archival.
    if any(
        key in measurement
        for key in (
            "constraint_identity_preserved",
            "clock_identity_preserved",
            "timing_xdc_preserved",
        )
    ):
        flags.append(measurement.get("constraint_identity_preserved"))
    if any(value is False for value in flags):
        return False
    if measurement.get("measurement_complete") is not True:
        return None
    if all(value is True for value in flags):
        return True
    return None


async def _contest_clock_period(mcp, cfg: Config) -> Optional[float]:
    clock_name = str(cfg.get("legality.contest_clock", "clk_fpl26contest"))
    try:
        return await mcp.get_clock_period(clock_name, allow_fallback=False)
    except TypeError:
        # Older test doubles and external MCP clients expose the no-arg API.
        return await mcp.get_clock_period()


async def _contest_clock_wns(mcp, cfg: Config) -> Optional[float]:
    clock_name = str(cfg.get("legality.contest_clock", "clk_fpl26contest"))
    try:
        return await mcp.get_wns(clock_name, allow_fallback=False)
    except TypeError:
        # Compatibility for test doubles; FDAgents' MCP client is always strict.
        try:
            return await mcp.get_wns(clock_name)
        except TypeError:
            return await mcp.get_wns()


def _tcl_braced(value: Path | str) -> str:
    text = str(value)
    if "}" in text:
        raise ValueError("Tcl path contains an unsupported closing brace")
    return "{" + text + "}"


def _canonical_timing_xdc(path: Path) -> str:
    commands = []
    for line in path.read_text(encoding="utf-8", errors="strict").splitlines():
        normalized = line.strip()
        if not normalized or normalized.startswith("#"):
            continue
        commands.append(normalized)
    return "\n".join(commands)


async def _capture_constraint_identity(
    mcp,
    dcp_path: Path,
    cfg: Config,
    xdc_path: Path,
) -> tuple[dict, str]:
    """Reopen one DCP and capture its clock plus exported timing XDC."""
    await mcp.call_vivado(
        "open_checkpoint", {"dcp_path": str(dcp_path)}, timeout=600.0
    )
    clock_name = str(cfg.get("legality.contest_clock", "clk_fpl26contest"))
    command = (
        f"set fdagents_clock [get_clocks -quiet {_tcl_braced(clock_name)}]; "
        "if {[llength $fdagents_clock] != 1} {error {contest clock is absent or ambiguous}}; "
        "set fdagents_clock [lindex $fdagents_clock 0]; "
        'puts "FDAGENTS_CLOCK_PERIOD=[get_property PERIOD $fdagents_clock]"; '
        'puts "FDAGENTS_CLOCK_WAVEFORM=[get_property WAVEFORM $fdagents_clock]"; '
        'puts "FDAGENTS_CLOCK_SOURCE=[join [get_property SOURCE_PINS $fdagents_clock] {|}]"; '
        f"write_xdc -force -exclude_physical {_tcl_braced(xdc_path)}"
    )
    response = await mcp.call_vivado(
        "run_tcl", {"command": command}, timeout=300.0
    )
    values = {}
    for key in ("PERIOD", "WAVEFORM", "SOURCE"):
        match = re.search(rf"FDAGENTS_CLOCK_{key}=([^\r\n]*)", response)
        if match is None:
            raise RuntimeError(f"contest clock identity missing {key}")
        values[key.lower()] = match.group(1).strip()
    if not xdc_path.is_file():
        raise RuntimeError("timing XDC export was not created")
    return values, _canonical_timing_xdc(xdc_path)


async def measure_candidate(
    mcp,
    dcp_path: Path,
    cfg: Config,
    *,
    reference_dcp_path: Optional[Path] = None,
) -> dict:
    """Open and freshly measure exactly ``dcp_path``, failing closed.

    No ``SkillResult``, cached WNS, or prior live-design value participates in
    this result.  Timing, route status, and contest-clock period are queried
    after the canonical checkpoint has been opened.
    """
    dcp_path = Path(dcp_path).expanduser().resolve()
    measurement = failed_measurement()
    if not dcp_path.is_file():
        measurement["legality_reason"] = f"candidate artifact missing: {dcp_path}"
        return measurement

    clock_identity_preserved = True
    timing_xdc_preserved = True
    reference_xdc_sha256 = None
    candidate_xdc_sha256 = None
    try:
        if reference_dcp_path is None:
            await mcp.call_vivado(
                "open_checkpoint", {"dcp_path": str(dcp_path)}, timeout=600.0
            )
        else:
            reference_dcp_path = Path(reference_dcp_path).expanduser().resolve()
            if not reference_dcp_path.is_file():
                raise FileNotFoundError(
                    f"constraint reference artifact missing: {reference_dcp_path}"
                )
            identity_root = Path(mcp.run_dir) / ".measurement_identity"
            identity_root.mkdir(parents=True, exist_ok=True)
            tag = hashlib.sha256(str(dcp_path).encode("utf-8")).hexdigest()[:16]
            reference_xdc = identity_root / f"{tag}.source_timing.xdc"
            candidate_xdc = identity_root / f"{tag}.candidate_timing.xdc"
            reference_clock, reference_text = await _capture_constraint_identity(
                mcp, reference_dcp_path, cfg, reference_xdc
            )
            candidate_clock, candidate_text = await _capture_constraint_identity(
                mcp, dcp_path, cfg, candidate_xdc
            )
            clock_identity_preserved = reference_clock == candidate_clock
            timing_xdc_preserved = reference_text == candidate_text
            reference_xdc_sha256 = hashlib.sha256(
                reference_text.encode("utf-8")
            ).hexdigest()
            candidate_xdc_sha256 = hashlib.sha256(
                candidate_text.encode("utf-8")
            ).hexdigest()
    except Exception as exc:  # noqa: BLE001
        measurement["legality_reason"] = (
            f"candidate constraint identity/open failed: {exc}"
        )
        return measurement

    try:
        timing_report = await mcp.call_vivado(
            "report_timing_summary", {}, timeout=300.0
        )
        route_report = await mcp.call_vivado(
            "report_route_status", {}, timeout=120.0
        )
        placement_report = await mcp.call_vivado(
            "run_tcl",
            {
                "command": (
                    "puts \"FDAGENTS_UNPLACED_PRIMITIVES:"
                    "[llength [get_cells -hier -quiet -filter "
                    "{IS_PRIMITIVE && STATUS == UNPLACED "
                    "&& REF_NAME != GND && REF_NAME != VCC}]]\""
                )
            },
            timeout=120.0,
        )
        clock_period = await _contest_clock_period(mcp, cfg)
        contest_wns = await _contest_clock_wns(mcp, cfg)
    except Exception as exc:  # noqa: BLE001
        measurement["legality_reason"] = f"central measurement failed: {exc}"
        return measurement

    timing = parse_timing_summary_static(timing_report)
    route = parse_route_status_static(route_report)
    wns = contest_wns
    tns = timing.get("tns")
    whs = timing.get("whs")
    wpws = timing.get("wpws")
    routed_ok = route.get("routed_ok")
    placement_match = re.search(
        r"FDAGENTS_UNPLACED_PRIMITIVES:(\d+)", placement_report
    )
    unplaced_primitives = (
        int(placement_match.group(1)) if placement_match is not None else None
    )
    complete = all(
        value is not None
        for value in (
            clock_period,
            wns,
            tns,
            whs,
            wpws,
            routed_ok,
            unplaced_primitives,
        )
    )
    route_legal = routed_ok is True if routed_ok is not None else None
    placed_legal = (
        unplaced_primitives == 0 if unplaced_primitives is not None else None
    )
    hold_legal = whs >= 0.0 if whs is not None else None
    pulse_legal = wpws >= 0.0 if wpws is not None else None
    verdict = legality.check_result(
        routed_ok=routed_ok,
        whs=whs,
        wpws=wpws,
        cfg=cfg,
    )
    all_contest_legal = (
        placed_legal is True
        and route_legal is True
        and hold_legal is True
        and pulse_legal is True
        and clock_identity_preserved is True
        and timing_xdc_preserved is True
    )
    if not complete:
        reason = "central measurement incomplete"
    elif not verdict.ok:
        reason = verdict.reason
    elif placed_legal is not True:
        reason = f"candidate has {unplaced_primitives} unplaced primitive(s)"
    elif clock_identity_preserved is not True:
        reason = "candidate contest clock identity differs from immutable input"
    elif timing_xdc_preserved is not True:
        reason = "candidate timing XDC differs from immutable input"
    elif not all_contest_legal:
        reason = "candidate is not placement/route/hold/pulse legal"
    else:
        reason = ""
    fmax = calculate_fmax(wns, clock_period)
    eligible = bool(complete and all_contest_legal and verdict.ok and fmax is not None)
    return {
        "clock_period_ns": float(clock_period) if clock_period is not None else None,
        "wns_ns": float(wns) if wns is not None else None,
        "fmax_mhz": float(fmax) if fmax is not None else None,
        "tns_ns": float(tns) if tns is not None else None,
        "failing_endpoints": timing.get("failing_endpoints"),
        "total_endpoints": timing.get("total_endpoints"),
        "whs_ns": float(whs) if whs is not None else None,
        "wpws_ns": float(wpws) if wpws is not None else None,
        "route_legal": route_legal,
        "placed_legal": placed_legal,
        "unplaced_primitives": unplaced_primitives,
        "hold_legal": hold_legal,
        "pulse_legal": pulse_legal,
        "clock_identity_preserved": clock_identity_preserved,
        "timing_xdc_preserved": timing_xdc_preserved,
        "constraint_identity_preserved": bool(
            clock_identity_preserved and timing_xdc_preserved
        ),
        "reference_timing_xdc_sha256": reference_xdc_sha256,
        "candidate_timing_xdc_sha256": candidate_xdc_sha256,
        "measurement_complete": bool(complete),
        "eligible_for_beam": eligible,
        "legality_reason": reason,
    }
