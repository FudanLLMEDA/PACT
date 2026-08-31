"""Root-to-final signoff evidence and frozen semantic-rebuild holdouts."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .operator_proof_current import _digest, _validated_strict_report
from .semantic_rebuild import validate_semantic_rebuild_manifest


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MD5_RE = re.compile(r"[0-9a-f]{32}")
_DCP_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+\.dcp")
FROZEN_HOLDOUT_FILES = (
    "boom_soc_2025.1_v2.dcp",
    "fir_systolic_transposed_routed_2025.1.dcp",
    "vtr_mcml_2025.1_v2.dcp",
)
_DIRECT_CHECKS = {
    "all_actions_emitted",
    "all_actions_postchecked",
    "consumer_dependencies_replayed",
    "relevant_nets_fully_routed",
}
_EXECUTION_BUNDLE_FIELDS = {
    "kind", "version", "input_dcp_sha256", "semantic_manifest_sha256",
    "nodes", "execution_order", "cross_action_edges", "transaction_policy",
    "rollback_policy", "semantic_parameter_policy",
    "external_semantic_parameters", "authority", "mutation_eligible",
    "bundle_sha256",
}
_NODE_ID_RE = re.compile(r"micro:[0-9a-f]{24}")


class SemanticSignoffError(ValueError):
    """Raised when execution, strict validation, or holdout evidence drifts."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _md5_file(path: Path) -> str:
    digest = hashlib.md5()  # noqa: S324 - verifies the official legacy manifest.
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_direct_execution_evidence(
    manifest: dict[str, Any],
    bundle: dict[str, Any],
    candidate_dcp_sha256: str,
    checks: dict[str, Any],
) -> dict[str, Any]:
    validated = validate_semantic_rebuild_manifest(manifest)
    if (
        not isinstance(bundle, dict)
        or set(bundle) != _EXECUTION_BUNDLE_FIELDS
        or bundle.get("kind") != "semantic_rebuild_execution_bundle"
        or bundle.get("version") != 1
        or bundle.get("input_dcp_sha256") != validated["input_dcp_sha256"]
        or bundle.get("semantic_manifest_sha256") != validated["manifest_sha256"]
        or bundle.get("execution_order") != validated["execution_order"]
        or bundle.get("transaction_policy") != "single_source_atomic_commit"
        or bundle.get("external_semantic_parameters") != {}
        or bundle.get("mutation_eligible") is not False
    ):
        raise SemanticSignoffError("semantic execution bundle does not bind the manifest")
    payload = dict(bundle)
    claimed_bundle = payload.pop("bundle_sha256", None)
    if not isinstance(claimed_bundle, str) or _digest(payload) != claimed_bundle:
        raise SemanticSignoffError("semantic execution bundle digest mismatch")
    if not isinstance(candidate_dcp_sha256, str) or _SHA256_RE.fullmatch(
        candidate_dcp_sha256
    ) is None:
        raise SemanticSignoffError("candidate DCP identity is invalid")
    if (
        not isinstance(checks, dict)
        or set(checks) != _DIRECT_CHECKS
        or any(value is not True for value in checks.values())
    ):
        raise SemanticSignoffError("direct execution checks are incomplete")
    evidence = {
        "kind": "semantic_rebuild_direct_execution_evidence",
        "version": 1,
        "status": "proved",
        "authority": "candidate_measurement_only",
        "source": {"dcp_sha256": validated["input_dcp_sha256"]},
        "candidate": {"dcp_sha256": candidate_dcp_sha256},
        "semantic_manifest_sha256": validated["manifest_sha256"],
        "execution_bundle_sha256": claimed_bundle,
        "execution_order": list(validated["execution_order"]),
        "checks": dict(checks),
        "final_submission_authority": False,
    }
    evidence["evidence_sha256"] = _digest(evidence)
    return evidence


def validate_direct_execution_evidence(value: Any) -> dict[str, Any]:
    expected = {
        "kind", "version", "status", "authority", "source", "candidate",
        "semantic_manifest_sha256", "execution_bundle_sha256",
        "execution_order", "checks", "final_submission_authority",
        "evidence_sha256",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise SemanticSignoffError("direct execution evidence schema is invalid")
    payload = dict(value)
    claimed = payload.pop("evidence_sha256", None)
    source = value.get("source")
    candidate = value.get("candidate")
    if (
        not isinstance(claimed, str)
        or _digest(payload) != claimed
        or value.get("kind") != "semantic_rebuild_direct_execution_evidence"
        or value.get("version") != 1
        or value.get("status") != "proved"
        or value.get("authority") != "candidate_measurement_only"
        or value.get("final_submission_authority") is not False
        or not isinstance(source, dict)
        or not isinstance(candidate, dict)
        or set(source) != {"dcp_sha256"}
        or set(candidate) != {"dcp_sha256"}
        or _SHA256_RE.fullmatch(str(source.get("dcp_sha256") or "")) is None
        or _SHA256_RE.fullmatch(str(candidate.get("dcp_sha256") or "")) is None
        or _SHA256_RE.fullmatch(
            str(value.get("semantic_manifest_sha256") or "")
        ) is None
        or _SHA256_RE.fullmatch(
            str(value.get("execution_bundle_sha256") or "")
        ) is None
        or not isinstance(value.get("execution_order"), list)
        or not value["execution_order"]
        or len(set(value["execution_order"])) != len(value["execution_order"])
        or any(
            not isinstance(node_id, str) or _NODE_ID_RE.fullmatch(node_id) is None
            for node_id in value["execution_order"]
        )
        or not isinstance(value.get("checks"), dict)
        or set(value["checks"]) != _DIRECT_CHECKS
        or any(item is not True for item in value["checks"].values())
    ):
        raise SemanticSignoffError("direct execution evidence did not replay")
    return json.loads(json.dumps(value, sort_keys=True))


def build_semantic_signoff_record(
    execution_evidence: dict[str, Any], strict_report: dict[str, Any]
) -> dict[str, Any]:
    evidence = validate_direct_execution_evidence(execution_evidence)
    source_sha = evidence["source"]["dcp_sha256"]
    candidate_sha = evidence["candidate"]["dcp_sha256"]
    try:
        report = _validated_strict_report(
            strict_report,
            expected_source_sha256=source_sha,
            expected_candidate_sha256=candidate_sha,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SemanticSignoffError(f"strict root-to-final validation failed: {exc}") from exc
    record = {
        "kind": "semantic_rebuild_root_to_final_signoff",
        "version": 1,
        "status": "passed",
        "authority": "validation_evidence_only",
        "source": {
            "dcp_sha256": source_sha,
            "size_bytes": report["root_size_bytes"],
        },
        "candidate": {
            "dcp_sha256": candidate_sha,
            "size_bytes": report["candidate_size_bytes"],
        },
        "semantic_manifest_sha256": evidence["semantic_manifest_sha256"],
        "execution_bundle_sha256": evidence["execution_bundle_sha256"],
        "execution_evidence_sha256": evidence["evidence_sha256"],
        "strict_report_sha256": _digest(report),
        "checks": {
            "source_identity_stable": True,
            "candidate_identity_stable": True,
            "structural_validation_passed": True,
            "simulation_validation_passed": True,
            "phase2_not_skipped": True,
        },
        "claims": {
            "strict_root_to_final_validation": True,
            "formal_whole_dcp_equivalence": False,
            "final_submission_authority": False,
        },
    }
    record["signoff_sha256"] = _digest(record)
    return record


def load_frozen_holdout_manifest(
    manifest_path: Path,
    *,
    benchmark_dir: Path | None = None,
) -> dict[str, Any]:
    import yaml

    path = Path(manifest_path).resolve(strict=True)
    if path.is_symlink() or not path.is_file():
        raise SemanticSignoffError("holdout manifest is not a regular file")
    raw = path.read_bytes()
    parsed = yaml.safe_load(raw) or {}
    entries = parsed.get("benchmarks")
    if not isinstance(entries, list) or not entries:
        raise SemanticSignoffError("holdout manifest benchmark list is unavailable")
    seen = set()
    holdouts = []
    for entry in entries:
        if not isinstance(entry, dict) or not {"file", "role", "md5"} <= set(entry):
            raise SemanticSignoffError("holdout manifest entry schema is invalid")
        filename = entry.get("file")
        role = entry.get("role")
        md5 = entry.get("md5")
        if (
            not isinstance(filename, str)
            or _DCP_NAME_RE.fullmatch(filename) is None
            or filename in seen
            or role not in {"train", "test"}
            or not isinstance(md5, str)
            or _MD5_RE.fullmatch(md5) is None
        ):
            raise SemanticSignoffError("holdout manifest entry identity is invalid")
        seen.add(filename)
        if role == "test":
            holdouts.append({"file": filename, "md5": md5})
    holdouts.sort(key=lambda item: item["file"])
    if tuple(item["file"] for item in holdouts) != tuple(sorted(FROZEN_HOLDOUT_FILES)):
        raise SemanticSignoffError("frozen holdout membership drifted")
    if benchmark_dir is not None:
        root = Path(benchmark_dir).resolve(strict=True)
        if root.is_symlink() or not root.is_dir():
            raise SemanticSignoffError("holdout benchmark directory is unavailable")
        for item in holdouts:
            dcp = root / item["file"]
            if dcp.is_symlink() or not dcp.is_file() or _md5_file(dcp) != item["md5"]:
                raise SemanticSignoffError(
                    f"holdout DCP identity mismatch: {item['file']}"
                )
    result = {
        "kind": "frozen_semantic_rebuild_holdouts",
        "version": 1,
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "holdouts": holdouts,
        "knowledge_policy": "train_only_no_holdout_trajectory_input",
        "semantic_parameter_policy": "fresh_recovered_facts_only",
    }
    result["descriptor_sha256"] = _digest(result)
    return result


def build_holdout_validation_jobs(
    descriptor: dict[str, Any],
    *,
    benchmark_dir: Path,
    run_root: Path,
    python_executable: str,
    vivado_executable: str,
    max_candidates: int = 64,
    action_limit: int = 2,
    timeout_s: int = 1800,
) -> dict[str, Any]:
    payload = dict(descriptor) if isinstance(descriptor, dict) else {}
    claimed = payload.pop("descriptor_sha256", None)
    if (
        descriptor.get("kind") != "frozen_semantic_rebuild_holdouts"
        or not isinstance(claimed, str)
        or _digest(payload) != claimed
        or descriptor.get("knowledge_policy")
        != "train_only_no_holdout_trajectory_input"
    ):
        raise SemanticSignoffError("frozen holdout descriptor did not replay")
    for name, value, low, high in (
        ("max_candidates", max_candidates, 1, 256),
        ("action_limit", action_limit, 1, 256),
        ("timeout_s", timeout_s, 1, 7200),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise SemanticSignoffError(f"{name} is outside its safety bound")
    if action_limit > max_candidates:
        raise SemanticSignoffError("action_limit exceeds max_candidates")
    benchmark_root = Path(benchmark_dir).resolve(strict=True)
    output_root = Path(run_root).resolve()
    if benchmark_root.is_symlink() or not benchmark_root.is_dir():
        raise SemanticSignoffError("holdout benchmark directory is unavailable")
    if output_root.is_symlink() or not output_root.is_dir():
        raise SemanticSignoffError("holdout run root is unavailable")
    jobs = []
    for item in descriptor["holdouts"]:
        source = benchmark_root / item["file"]
        if source.is_symlink() or not source.is_file():
            raise SemanticSignoffError(f"holdout DCP is unavailable: {item['file']}")
        stem = Path(item["file"]).stem
        work = output_root / stem
        candidate = work / "candidate.dcp"
        execution = work / "memory_repack_result.json"
        strict_report = work / "strict-report.json"
        signoff = work / "signoff.json"
        jobs.append({
            "benchmark": item["file"],
            "input_md5": item["md5"],
            "work_dir": str(work),
            "no_candidate_policy": "record_not_applicable_without_mutation_or_signoff",
            "signoff_condition": "rebuild_status_is_success",
            "rebuild_command": [
                python_executable,
                "scripts/validate_memory_repack_direct.py",
                str(source),
                str(candidate),
                "--work-dir", str(work),
                "--vivado", vivado_executable,
                "--max-candidates", str(max_candidates),
                "--action-limit", str(action_limit),
                "--timeout-s", str(timeout_s),
            ],
            "signoff_command": [
                python_executable,
                "scripts/signoff_semantic_rebuild.py",
                str(source),
                str(candidate),
                str(execution),
                str(strict_report),
                str(signoff),
                "--timeout-s", str(timeout_s),
            ],
        })
    result = {
        "kind": "semantic_rebuild_holdout_jobs",
        "version": 1,
        "descriptor_sha256": descriptor["descriptor_sha256"],
        "jobs": jobs,
        "external_semantic_parameters": {},
        "authority": "execution_plan_only",
    }
    result["jobs_sha256"] = _digest(result)
    return result
