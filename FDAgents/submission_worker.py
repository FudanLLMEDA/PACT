"""Killable strict-validator subprocess used by submission finalization."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

from .artifacts import atomic_write_json, sha256_file
from .operator_proof import STRICT_STIMULUS_PROTOCOL, STRICT_VALIDATOR_PROTOCOL


async def _terminate_process_group(process, kill_grace_s: float) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=kill_grace_s)
        return
    except asyncio.TimeoutError:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await process.wait()


async def run_strict_validation_worker(
    source_dcp: Path,
    candidate_dcp: Path,
    report_path: Path,
    *,
    vectors: int,
    precheck_vectors: int,
    timeout_s: float,
    deadline_monotonic: float | None = None,
    kill_grace_s: float = 10.0,
    max_report_bytes: int = 67_108_864,
) -> dict:
    """Run the fixed strict worker with cancellation-safe process-group cleanup."""
    source = Path(source_dcp).resolve()
    candidate = Path(candidate_dcp).resolve()
    report_file = Path(report_path).resolve()
    for name, path in (("source", source), ("candidate", candidate)):
        if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
            raise ValueError(f"strict validation {name} DCP is not a regular artifact")
    for name, value, minimum in (
        ("vectors", vectors, 1),
        ("precheck_vectors", precheck_vectors, 0),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"{name} is outside its allowed range")
    if precheck_vectors > vectors:
        raise ValueError("precheck_vectors cannot exceed vectors")
    if report_file.parent.is_symlink() or not report_file.parent.is_dir():
        raise ValueError("strict validation report parent is unavailable")
    if report_file.exists():
        if report_file.is_symlink() or not report_file.is_file():
            raise ValueError("strict validation report path is unsafe")
        report_file.unlink()
    effective_timeout = float(timeout_s)
    if deadline_monotonic is not None:
        effective_timeout = min(
            effective_timeout, float(deadline_monotonic) - time.monotonic()
        )
    if effective_timeout <= 0:
        raise asyncio.TimeoutError("strict validation deadline is exhausted")
    source_before = (source.stat().st_size, sha256_file(source))
    candidate_before = (candidate.stat().st_size, sha256_file(candidate))
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "FDAgents.submission_worker",
        str(source),
        str(candidate),
        str(report_file),
        "--vectors",
        str(vectors),
        "--precheck-vectors",
        str(precheck_vectors),
        cwd=str(Path(__file__).resolve().parents[1]),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        await asyncio.wait_for(process.wait(), timeout=effective_timeout)
    except asyncio.CancelledError:
        await asyncio.shield(_terminate_process_group(process, kill_grace_s))
        raise
    except asyncio.TimeoutError:
        await _terminate_process_group(process, kill_grace_s)
        raise
    if report_file.is_symlink() or not report_file.is_file():
        raise RuntimeError("strict validator did not produce a regular report")
    if report_file.stat().st_size <= 0 or report_file.stat().st_size > max_report_bytes:
        raise RuntimeError("strict validator report size is outside bounds")
    report = json.loads(report_file.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise RuntimeError("strict validator report is not an object")
    report["process_exit_code"] = process.returncode
    atomic_write_json(report_file, report)
    source_after = (source.stat().st_size, sha256_file(source))
    candidate_after = (candidate.stat().st_size, sha256_file(candidate))
    if source_after != source_before or candidate_after != candidate_before:
        raise RuntimeError("strict validation artifact identity changed")
    if (
        report.get("root_sha256") != source_before[1]
        or report.get("root_sha256_after") != source_before[1]
        or report.get("candidate_sha256") != candidate_before[1]
        or report.get("candidate_sha256_after") != candidate_before[1]
    ):
        raise RuntimeError("strict validator report identity mismatch")
    return report


async def run(args) -> int:
    from validate_dcps import DCPValidator

    root = Path(args.root)
    candidate = Path(args.candidate)
    report = {
        "schema_version": 1,
        "strict_mode": True,
        "validator_protocol": STRICT_VALIDATOR_PROTOCOL,
        "stimulus_protocol": STRICT_STIMULUS_PROTOCOL,
        "vectors": args.vectors,
        "precheck_vectors": args.precheck_vectors,
        "root_sha256": sha256_file(root),
        "root_size_bytes": root.stat().st_size,
        "candidate_sha256": sha256_file(candidate),
        "candidate_size_bytes": candidate.stat().st_size,
        "phase1_passed": False,
        "phase2_passed": False,
        "phase2_skipped": False,
        "infrastructure_failure": False,
    }
    validator = DCPValidator(
        root,
        candidate,
        num_vectors=args.vectors,
        precheck_vectors=args.precheck_vectors,
    )
    try:
        await validator.start_servers()
        await validator.validate()
        report.update({
            "phase1_passed": validator.phase1_passed,
            "phase2_passed": validator.phase2_passed,
            "phase2_skipped": validator.phase2_skipped,
            "infrastructure_failure": validator.infrastructure_failure,
        })
    except Exception as exc:  # noqa: BLE001
        report["infrastructure_failure"] = True
        report["error"] = str(exc)[:500]
    finally:
        await validator.cleanup()
        report["root_sha256_after"] = sha256_file(root)
        report["candidate_sha256_after"] = sha256_file(candidate)
        atomic_write_json(Path(args.report), report)
    passed = bool(
        report["phase1_passed"]
        and report["phase2_passed"]
        and not report["phase2_skipped"]
        and not report["infrastructure_failure"]
        and report["root_sha256"] == report["root_sha256_after"]
        and report["candidate_sha256"] == report["candidate_sha256_after"]
    )
    return 0 if passed else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root")
    parser.add_argument("candidate")
    parser.add_argument("report")
    parser.add_argument("--vectors", type=int, default=200)
    parser.add_argument("--precheck-vectors", type=int, default=100)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
