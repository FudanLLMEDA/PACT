"""Vivado route/reopen measurement subprocess wrapper with no decision authority."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from typing import Any, Callable, Sequence

from .common import envelope, fail, file_sha256, object_digest


def build_vivado_measurement_command(
    *,
    vivado_executable: Path | str,
    source_dcp: Path | str,
    candidate_dcp: Path | str,
    tcl_script: Path | str,
    report_json: Path | str,
) -> tuple[str, ...]:
    executable = str(Path(vivado_executable).expanduser())
    if not executable:
        fail("MEASURE_VIVADO_PATH_EMPTY", "Vivado executable path is required")
    source = Path(source_dcp).expanduser().resolve()
    candidate = Path(candidate_dcp).expanduser().resolve()
    script = Path(tcl_script).expanduser().resolve()
    report = Path(report_json).expanduser().resolve()
    # Inputs must exist even for dry-run command construction; the report is an
    # output and therefore need not exist.
    file_sha256(source)
    file_sha256(candidate)
    if not script.is_file() or script.is_symlink():
        fail("MEASURE_SCRIPT_UNAVAILABLE", "measurement Tcl script is unavailable", str(script))
    return (
        executable,
        "-mode", "batch",
        "-nojournal", "-nolog",
        "-source", str(script),
        "-tclargs", str(source), str(candidate), str(report),
    )


def route_reopen_measurement(
    *,
    vivado_executable: Path | str,
    source_dcp: Path | str,
    candidate_dcp: Path | str,
    tcl_script: Path | str,
    report_json: Path | str,
    execute: bool = False,
    timeout_s: float = 3600.0,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    command = build_vivado_measurement_command(
        vivado_executable=vivado_executable,
        source_dcp=source_dcp,
        candidate_dcp=candidate_dcp,
        tcl_script=tcl_script,
        report_json=report_json,
    )
    source_sha = file_sha256(source_dcp)
    candidate_sha = file_sha256(candidate_dcp)
    payload = {
        "command": list(command),
        "execution_requested": execute,
        "timeout_s": timeout_s,
        "candidate_artifact_sha256": candidate_sha,
    }
    if execute:
        executable = Path(command[0])
        if not executable.is_file():
            fail("MEASURE_VIVADO_UNAVAILABLE", "explicit Vivado executable does not exist", str(executable))
        completed = runner(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        payload.update({
            "returncode": int(completed.returncode),
            "stdout": str(completed.stdout),
            "stderr": str(completed.stderr),
        })
        status = "success" if completed.returncode == 0 else "rejected"
        reasons = [] if status == "success" else [{
            "reason_code": "MEASURE_SUBPROCESS_FAILED",
            "message": f"Vivado exited with status {completed.returncode}",
            "path": "subprocess.returncode",
        }]
    else:
        payload["returncode"] = None
        status = "prepared"
        reasons = []
    return envelope(
        "route_reopen_measurement_wrapper",
        source_artifact_sha256=source_sha,
        candidate_family_id=None,
        candidate_hash=candidate_sha,
        status=status,
        payload=payload,
        failure_reasons=reasons,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vivado", required=True)
    parser.add_argument("--source-dcp", required=True)
    parser.add_argument("--candidate-dcp", required=True)
    parser.add_argument("--script", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--timeout-s", type=float, default=3600.0)
    args = parser.parse_args()
    import json
    try:
        result = route_reopen_measurement(
            vivado_executable=args.vivado,
            source_dcp=args.source_dcp,
            candidate_dcp=args.candidate_dcp,
            tcl_script=args.script,
            report_json=args.report,
            execute=args.execute,
            timeout_s=args.timeout_s,
        )
    except Exception as exc:
        from .common import rejection_from_exception
        result = rejection_from_exception("route_reopen_measurement_wrapper", exc)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
