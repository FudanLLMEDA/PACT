"""Common checkpoint gate. Skill-local `is_legal` means routing only.

Restore the input timing constraints before measuring any candidate. This
removes exploratory clock changes and timing exceptions, not physical ECOs.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import signal
import sys
from pathlib import Path

from .skills.base import parse_route_status_static, parse_timing_summary_static


def tcl_word(value: object) -> str:
    text = str(value)
    for old, new in (("\\", "\\\\"), ('"', '\\"'), ("$", "\\$"), ("[", "\\["), ("\n", "\\n"), ("\r", "\\r")):
        text = text.replace(old, new)
    return '"' + text + '"'


CLOCK_SNAPSHOT = '''
foreach c [lsort [get_clocks -quiet]] {
    puts "PACT_CLOCK [list [get_property NAME $c] [get_property PERIOD $c] [get_property WAVEFORM $c] [get_property IS_GENERATED $c]]"
}
'''


def clock_lines(report: str) -> list[str]:
    return sorted(line.strip() for line in report.splitlines() if line.startswith("PACT_CLOCK "))


async def capture_constraints(mcp, directory: Path) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    xdc = (directory / "original_timing.xdc").resolve()
    report = await mcp.call_vivado("run_tcl", {"command":
        f"write_xdc -force -type timing {tcl_word(xdc)}\n" + CLOCK_SNAPSHOT}, timeout=300.0)
    clocks = clock_lines(report)
    if not xdc.is_file() or not clocks:
        raise RuntimeError("Cannot capture input timing constraints and clocks")
    (directory / "original_clocks.json").write_text(json.dumps(clocks, indent=2) + "\n")
    return {"xdc": str(xdc), "clocks": clocks}


def classify_reports(route_report: str, timing_report: str) -> dict:
    route = parse_route_status_static(route_report)
    timing = parse_timing_summary_static(timing_report)
    timing = {k: None if isinstance(v, float) and not math.isfinite(v) else v
              for k, v in timing.items()}
    checks = {"routing": "passed" if route["routed_ok"] is True else "failed"}
    for name, keys in (("setup", ("wns",)), ("hold", ("whs", "ths")),
                       ("pulse_width", ("wpws", "tpws"))):
        values = [timing.get(k) for k in keys]
        if any(v is None or not math.isfinite(v) for v in values):
            checks[name] = "missing"
        elif name != "setup" and any(v < 0 for v in values):
            checks[name] = "failed"
        else:
            # Negative setup slack is expected for timing-closure inputs.
            checks[name] = "measured" if name == "setup" else "passed"
    return {"passed": all(v in ("passed", "measured") for v in checks.values()),
            "checks": checks, "timing": timing, "route": route,
            "functional": "not_checked", "formal_equivalence": "not_checked"}


async def validate_checkpoint(mcp, candidate: Path, normalized: Path,
                              constraints: dict, evidence_dir: Path) -> dict:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    result = {"passed": False, "checks": {}, "functional": "not_checked",
              "formal_equivalence": "not_checked"}
    try:
        await mcp.call_vivado("open_checkpoint", {"dcp_path": str(candidate.resolve())}, timeout=600.0)
        command = ("reset_timing\n"
                   f"read_xdc {tcl_word(constraints['xdc'])}\nupdate_timing\n" + CLOCK_SNAPSHOT +
                   '\nputs "PACT_CONSTRAINTS_RESTORED"')
        restored = await mcp.call_vivado("run_tcl", {"command": command}, timeout=300.0)
        (evidence_dir / "restoration.log").write_text(restored)
        if "PACT_CONSTRAINTS_RESTORED" not in restored or clock_lines(restored) != constraints["clocks"]:
            raise RuntimeError("Original clock names, periods or waveforms were not restored")
        await mcp.call_vivado("write_checkpoint", {"dcp_path": str(normalized.resolve()), "force": True}, timeout=600.0)
        # Do not accept only the in-memory state: reopen the exported DCP.
        await mcp.call_vivado("open_checkpoint", {"dcp_path": str(normalized.resolve())}, timeout=600.0)
        replay_clocks = await mcp.call_vivado("run_tcl", {"command": CLOCK_SNAPSHOT})
        if clock_lines(replay_clocks) != constraints["clocks"]:
            raise RuntimeError("Reopened checkpoint clocks differ from the input")
        route = await mcp.call_vivado("report_route_status", {}, timeout=120.0)
        timing = await mcp.call_vivado("run_tcl", {"command":
            "report_timing_summary -delay_type min_max -report_unconstrained"}, timeout=300.0)
        (evidence_dir / "route_status.rpt").write_text(route)
        (evidence_dir / "timing_summary.rpt").write_text(timing)
        result = classify_reports(route, timing)
        result["checks"].update(constraints="passed", checkpoint_reopen="passed")
    except Exception as exc:
        result["error"] = str(exc)
    (evidence_dir / "validation.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    return result


async def check_functional(input_dcp: Path, output_dcp: Path, directory: Path,
                           timeout: float) -> dict:
    """Run the existing structural + simulation checker, never a formal proof."""
    directory.mkdir(parents=True, exist_ok=True)
    report = (directory / "functional.json").resolve()
    command = [sys.executable, str(Path(__file__).resolve().parents[1] / "validate_dcps.py"),
               str(input_dcp.resolve()), str(output_dcp.resolve()),
               "--precheck-vectors", "50", "--vectors", "200", "--report-json", str(report),
               "--work-dir", str(directory.resolve())]
    with (directory / "functional.log").open("w") as log:
        proc = await asyncio.create_subprocess_exec(*command, stdout=log, stderr=log, start_new_session=True)
        try:
            rc = await asyncio.wait_for(proc.wait(), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # The process may exit at the timeout boundary.
            await proc.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            return {"status": "timeout", "report": str(report)}
    data = json.loads(report.read_text()) if report.exists() else {}
    passed = rc == 0 and data.get("phase1_passed") is True and data.get("phase2_passed") is True and not data.get("phase2_skipped") and not data.get("infrastructure_failure")
    return {"status": "passed" if passed else "failed", "exit_code": rc,
            "report": str(report), "formal_equivalence": "not_checked"}
