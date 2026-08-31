#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from FDAgents.skills.base import parse_route_status_static, parse_timing_summary_static
from usage_accounting import UNKNOWN, parse_fdagents_usage


FIELDS = [
    "target",
    "verify_status",
    "verified_fmax_mhz",
    "wns",
    "whs",
    "wpws",
    "route_errors",
    "output_dcp",
    "run_dir",
    "verify_time_s",
    "wall_time_s",
    "agent_elapsed_s",
    "input_tokens",
    "cached_input_tokens",
    "uncached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
    "cost_usd",
    "cost_upper_bound_usd",
    "usage_source",
]


def parse_timing_requirement_ns(timing_report: str) -> float | None:
    match = re.search(r"Requirement:\s*(-?\d+(?:\.\d+)?)ns", timing_report)
    return float(match.group(1)) if match else None


def _fmax_from_timing(timing: dict, timing_report: str) -> float | None:
    requirement = parse_timing_requirement_ns(timing_report)
    wns = timing.get("wns")
    if requirement is None or wns is None:
        return None
    achieved = requirement - float(wns)
    if achieved <= 0:
        return None
    return 1000.0 / achieved


def classify_reports(route_report: str, timing_report: str) -> dict[str, object]:
    if not route_report or not timing_report:
        return {"verify_status": "missing_report", "verified_fmax_mhz": ""}
    route = parse_route_status_static(route_report)
    timing = parse_timing_summary_static(timing_report)
    route_errors = route.get("nets_with_routing_errors")
    fmax = _fmax_from_timing(timing, timing_report)
    if not route.get("routed_ok"):
        return {
            "verify_status": "route_fail",
            "verified_fmax_mhz": fmax if fmax is not None else "",
            "route_errors": route_errors if route_errors is not None else "",
        }
    if timing.get("whs") is not None and float(timing["whs"]) < 0:
        return {"verify_status": "hold_fail", "verified_fmax_mhz": fmax if fmax is not None else "", "wns": timing.get("wns"), "whs": timing.get("whs")}
    if timing.get("ths") is not None and float(timing["ths"]) < 0:
        return {"verify_status": "hold_fail", "verified_fmax_mhz": fmax if fmax is not None else "", "wns": timing.get("wns"), "whs": timing.get("whs")}
    if timing.get("wpws") is not None and float(timing["wpws"]) < 0:
        return {"verify_status": "pulse_fail", "verified_fmax_mhz": fmax if fmax is not None else "", "wns": timing.get("wns"), "wpws": timing.get("wpws")}
    if timing.get("tpws") is not None and float(timing["tpws"]) < 0:
        return {"verify_status": "pulse_fail", "verified_fmax_mhz": fmax if fmax is not None else "", "wns": timing.get("wns"), "wpws": timing.get("wpws")}
    return {
        "verify_status": "verified" if fmax is not None else "timing_missing",
        "verified_fmax_mhz": fmax if fmax is not None else "",
        "wns": timing.get("wns") if timing.get("wns") is not None else "",
        "whs": timing.get("whs") if timing.get("whs") is not None else "",
        "wpws": timing.get("wpws") if timing.get("wpws") is not None else "",
        "route_errors": route_errors if route_errors is not None else "",
    }


def _verify_with_vivado(output_dcp: Path, verify_dir: Path, vivado: str) -> tuple[str, str]:
    verify_dir.mkdir(parents=True, exist_ok=True)
    route_rpt = verify_dir / "route_status.rpt"
    timing_rpt = verify_dir / "timing_summary_minmax.rpt"
    tcl = verify_dir / "verify.tcl"
    tcl.write_text(
        "\n".join(
            [
                f"open_checkpoint {{{output_dcp.resolve()}}}",
                f"report_route_status -file {{{route_rpt.resolve()}}}",
                f"report_timing_summary -delay_type min_max -file {{{timing_rpt.resolve()}}}",
                "exit",
                "",
            ]
        )
    )
    subprocess.run(
        [vivado, "-mode", "batch", "-source", str(tcl.resolve())],
        cwd=str(verify_dir),
        stdout=(verify_dir / "vivado_stdout.log").open("w"),
        stderr=subprocess.STDOUT,
        check=False,
    )
    return (
        route_rpt.read_text() if route_rpt.exists() else "",
        timing_rpt.read_text() if timing_rpt.exists() else "",
    )


def verify_outputs(results_csv: Path, output_csv: Path, vivado: str = "vivado") -> Path:
    with results_csv.open(newline="") as f:
        rows = list(csv.DictReader(f))
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            start = time.time()
            out = {field: row.get(field, "") for field in FIELDS}
            out["target"] = row.get("target", "")
            output_dcp = Path(row.get("output_dcp", ""))
            if not output_dcp.exists():
                fallback = Path(row.get("run_dir") or "") / "agent_run" / "current_best.dcp"
                if fallback.exists():
                    output_dcp = fallback
            out["output_dcp"] = str(output_dcp)
            out["run_dir"] = row.get("run_dir", "")
            _repair_fdagents_usage_from_console(row, out)
            if not output_dcp.exists():
                out["verify_status"] = "missing_output"
            else:
                verify_dir = Path(row.get("run_dir") or output_dcp.parent) / "verify"
                route_report, timing_report = _verify_with_vivado(output_dcp, verify_dir, vivado)
                classified = classify_reports(route_report, timing_report)
                out.update(classified)
                if isinstance(out.get("verified_fmax_mhz"), float):
                    out["verified_fmax_mhz"] = f"{out['verified_fmax_mhz']:.6f}"
            out["verify_time_s"] = f"{time.time() - start:.3f}"
            writer.writerow(out)
            f.flush()
            os.fsync(f.fileno())
    return output_csv


def _needs_usage_repair(row: dict[str, str]) -> bool:
    return any(row.get(key) in ("", UNKNOWN, None) for key in ("input_tokens", "output_tokens", "total_tokens", "cost_usd"))


def _repair_fdagents_usage_from_console(row: dict[str, str], out: dict[str, object]) -> None:
    if not _needs_usage_repair(row):
        return
    run_dir = Path(row.get("run_dir") or "")
    console = run_dir / "console.log"
    if not console.exists():
        return
    usage = parse_fdagents_usage(console.read_text(errors="replace"))
    repaired = usage.as_row()
    if any(repaired.get(key) in ("", UNKNOWN, None) for key in ("input_tokens", "output_tokens", "total_tokens", "cost_usd")):
        return
    for key in (
        "agent_elapsed_s",
        "input_tokens",
        "cached_input_tokens",
        "uncached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
        "cost_usd",
        "cost_upper_bound_usd",
        "usage_source",
    ):
        out[key] = repaired.get(key, out.get(key, ""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--vivado", default="vivado")
    args = parser.parse_args(argv)
    verify_outputs(args.results, args.output, args.vivado)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
