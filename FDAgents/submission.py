"""Fail-closed strict submission staging, evidence, and verified export."""

from __future__ import annotations

import asyncio
import argparse
import copy
import json
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import candidate_graph as graph_ops
from .artifacts import (
    atomic_copy_file,
    atomic_write_json,
    sha256_file,
    validate_owned_candidate_path,
)
from .config import load_config
from .memory import Memory
from .recurrence_lineage import (
    validate_full_recurrence_lineage,
)
from .submission_worker import run_strict_validation_worker


@dataclass(frozen=True)
class ArtifactIdentity:
    path: str
    size_bytes: int
    sha256: str


def inspect_artifact(path: Path, max_size_bytes: int) -> ArtifactIdentity:
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError(f"submission artifact is missing, symlinked, or not regular: {path}")
    before = candidate.stat()
    if before.st_size <= 0 or before.st_size > max_size_bytes:
        raise ValueError(f"submission artifact size is outside bounds: {before.st_size}")
    digest = sha256_file(candidate)
    after = candidate.stat()
    if (
        before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise RuntimeError(f"submission artifact changed while hashing: {path}")
    return ArtifactIdentity(str(candidate.resolve()), before.st_size, digest)


def copy_verified(
    source: Path,
    destination: Path,
    expected: ArtifactIdentity,
    max_size_bytes: int,
) -> ArtifactIdentity:
    current = inspect_artifact(source, max_size_bytes)
    if current.size_bytes != expected.size_bytes or current.sha256 != expected.sha256:
        raise RuntimeError("submission source identity changed before copy")
    atomic_copy_file(source, destination, expected_sha256=expected.sha256)
    published = inspect_artifact(destination, max_size_bytes)
    if published.size_bytes != expected.size_bytes or published.sha256 != expected.sha256:
        raise RuntimeError("published submission output identity mismatch")
    return published


async def _run_worker(
    root: Path,
    candidate: Path,
    report_path: Path,
    cfg,
    timeout_s: float,
) -> dict:
    return await run_strict_validation_worker(
        root,
        candidate,
        report_path,
        vectors=int(cfg.get("equivalence.vectors", 200)),
        precheck_vectors=int(cfg.get("equivalence.precheck_vectors", 100)),
        timeout_s=timeout_s,
        kill_grace_s=float(cfg.get("submission.process_kill_grace_s", 10)),
        max_report_bytes=int(cfg.get("submission.max_evidence_bytes", 67108864)),
    )


def _strict_pass(report: dict, root: ArtifactIdentity, candidate: ArtifactIdentity) -> bool:
    return bool(
        report.get("schema_version") == 1
        and report.get("strict_mode") is True
        and report.get("root_sha256") == root.sha256
        and report.get("root_size_bytes") == root.size_bytes
        and report.get("candidate_sha256") == candidate.sha256
        and report.get("candidate_size_bytes") == candidate.size_bytes
        and report.get("phase1_passed") is True
        and report.get("phase2_passed") is True
        and report.get("phase2_skipped") is False
        and report.get("infrastructure_failure") is False
        and report.get("process_exit_code") == 0
    )


async def finalize_submission(
    memory,
    *,
    run_dir: Path,
    output_dcp: Path,
    cfg,
    equivalence_runner=None,
    persist_memory: bool = True,
) -> dict:
    """Publish optimized bytes only after durable hash-bound strict evidence."""
    submission_dir = Path(run_dir) / "submission"
    submission_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = submission_dir / "manifest.json"
    max_bytes = int(cfg.get("submission.max_artifact_bytes", 4294967296))
    graph = memory.candidate_graph
    root_id = graph["root_candidate_id"]
    best_id = graph["current_best_candidate_id"]
    root_node = memory.candidate_node(root_id)
    best_node = memory.candidate_node(best_id)
    root_path = validate_owned_candidate_path(
        run_dir, root_id, Path(root_node["artifact_path"])
    )
    root_identity = inspect_artifact(root_path, max_bytes)
    if (
        root_identity.sha256 != root_node.get("artifact_sha256")
        or root_identity.size_bytes != root_node.get("artifact_size_bytes")
        or root_identity.sha256 != memory.run_manifest.get("root_sha256")
    ):
        immutable_input = Path(str(memory.run_manifest.get("input_dcp") or ""))
        input_identity = inspect_artifact(immutable_input, max_bytes)
        if input_identity.sha256 != memory.run_manifest.get("root_sha256"):
            raise RuntimeError("immutable submission root identity changed")
        root_path = immutable_input
        root_identity = input_identity
    copy_verified(root_path, output_dcp, root_identity, max_bytes)
    manifest = {
        "schema_version": 1,
        "state": "identity_committed",
        "updated_at": time.time(),
        "root": asdict(root_identity),
        "selected_candidate_id": best_id,
        "output_path": str(Path(output_dcp).resolve()),
    }
    atomic_write_json(manifest_path, manifest)
    if best_id == root_id:
        manifest.update({
            "state": "fallback_committed",
            "fallback_reason": "identity_is_graph_best",
            "output": asdict(root_identity),
        })
        atomic_write_json(manifest_path, manifest)
        return manifest
    staged_root = None
    staged_candidate = None
    try:
        if memory.time_remaining() <= float(cfg.get("submission.export_guard_s", 30)):
            raise asyncio.TimeoutError("submission finalization reserve exhausted")
        candidate_path = validate_owned_candidate_path(
            run_dir, best_id, Path(best_node["artifact_path"])
        )
        candidate_identity = inspect_artifact(candidate_path, max_bytes)
        if (
            candidate_identity.sha256 != best_node.get("artifact_sha256")
            or candidate_identity.size_bytes != best_node.get("artifact_size_bytes")
            or not best_node.get("eligible_for_beam")
            or best_node.get("placed_legal") is not True
        ):
            raise RuntimeError("graph best identity or central legality changed")
        validate_full_recurrence_lineage(memory, run_dir, best_node)
        attempt = Path(tempfile.mkdtemp(prefix="attempt-", dir=submission_dir))
        staged_root = attempt / "golden.dcp"
        staged_candidate = attempt / "revised.dcp"
        copy_verified(root_path, staged_root, root_identity, max_bytes)
        copy_verified(candidate_path, staged_candidate, candidate_identity, max_bytes)
        report_path = attempt / "validator-report.json"
        manifest.update({
            "state": "validating",
            "candidate": asdict(candidate_identity),
            "attempt_dir": str(attempt.relative_to(run_dir)),
        })
        atomic_write_json(manifest_path, manifest)
        timeout_s = min(
            float(cfg.get("submission.validator_timeout_s", 480)),
            max(
                0.0,
                memory.time_remaining()
                - float(cfg.get("submission.export_guard_s", 30)),
            ),
        )
        if timeout_s <= 0:
            raise asyncio.TimeoutError
        if equivalence_runner is None:
            report = await _run_worker(
                staged_root, staged_candidate, report_path, cfg, timeout_s
            )
        else:
            evidence = await asyncio.wait_for(
                equivalence_runner(staged_root, staged_candidate), timeout=timeout_s
            )
            report = {
                "schema_version": 1,
                "strict_mode": True,
                "root_sha256": root_identity.sha256,
                "root_size_bytes": root_identity.size_bytes,
                "candidate_sha256": candidate_identity.sha256,
                "candidate_size_bytes": candidate_identity.size_bytes,
                "phase1_passed": evidence.get("phase1_passed"),
                "phase2_passed": evidence.get("phase2_passed"),
                "phase2_skipped": evidence.get("phase2_skipped"),
                "infrastructure_failure": evidence.get("infrastructure_failure"),
                "process_exit_code": 0,
            }
            atomic_write_json(report_path, report)
        report_identity = inspect_artifact(
            report_path,
            int(cfg.get("submission.max_evidence_bytes", 67108864)),
        )
        staged_root_identity = inspect_artifact(staged_root, max_bytes)
        staged_candidate_identity = inspect_artifact(staged_candidate, max_bytes)
        if staged_root_identity.sha256 != root_identity.sha256:
            raise RuntimeError("staged root changed during validation")
        if staged_candidate_identity.sha256 != candidate_identity.sha256:
            raise RuntimeError("staged candidate changed during validation")
        passed = _strict_pass(report, root_identity, candidate_identity)
        graph_ops.record_equivalence(
            best_node,
            status="passed" if passed else "failed",
            method="strict_subprocess_structural_and_simulation",
            checked_at=time.time(),
            root_sha256=root_identity.sha256,
            candidate_sha256=candidate_identity.sha256,
            report_path=str(report_path),
        )
        if not passed:
            raise RuntimeError("strict validator evidence did not authorize candidate")
        manifest.update({
            "state": "validated",
            "report": asdict(report_identity),
            "validation": report,
        })
        if persist_memory:
            memory.save(Path(run_dir) / "memory.json")
        atomic_write_json(manifest_path, manifest)
        if memory.time_remaining() <= float(cfg.get("submission.export_guard_s", 30)):
            raise asyncio.TimeoutError("optimized export guard reached")
        output_identity = copy_verified(
            staged_candidate, output_dcp, candidate_identity, max_bytes
        )
        manifest.update({
            "state": "committed",
            "output": asdict(output_identity),
            "updated_at": time.time(),
        })
        atomic_write_json(manifest_path, manifest)
        staged_root.unlink(missing_ok=True)
        staged_candidate.unlink(missing_ok=True)
        return manifest
    except Exception as exc:  # noqa: BLE001
        copy_verified(root_path, output_dcp, root_identity, max_bytes)
        manifest.update({
            "state": "fallback_committed",
            "fallback_reason": f"{type(exc).__name__}: {exc}"[:300],
            "output": asdict(root_identity),
            "updated_at": time.time(),
        })
        atomic_write_json(manifest_path, manifest)
        for staged in (staged_root, staged_candidate):
            if staged is not None:
                staged.unlink(missing_ok=True)
        return manifest


async def _dry_run(args) -> int:
    run_dir = Path(args.run_dir).resolve()
    memory_path = run_dir / "memory.json"
    memory = copy.deepcopy(Memory.load(memory_path))
    memory.budget["start_time"] = time.time()
    memory.budget["time_limit_s"] = float(args.time_limit)
    memory.budget["time_elapsed"] = 0.0
    memory.budget["time_remaining"] = float(args.time_limit)
    manifest = await finalize_submission(
        memory,
        run_dir=run_dir,
        output_dcp=Path(args.output).resolve(),
        cfg=load_config(args.config),
        persist_memory=False,
    )
    return 0 if manifest.get("state") == "committed" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Strict finalizer dry run")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--time-limit", type=float, default=900.0)
    return asyncio.run(_dry_run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
