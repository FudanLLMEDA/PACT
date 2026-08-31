"""Closed, hash-bound execution of operator-owned semantic replay manifests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from ..artifacts import sha256_file
from ..operator_proof import (
    MIN_POST_EMISSION_PRECHECK_VECTORS,
    MIN_POST_EMISSION_VECTORS,
    STRICT_STIMULUS_PROTOCOL,
    STRICT_VALIDATOR_PROTOCOL,
)
from ..submission_worker import _terminate_process_group, run_strict_validation_worker
from .base import SkillOutput, SkillResult


NAME = "semantic_replay"
TARGET = "auto"
PROOF_VERSION = "semantic-replay-v1"
MANIFEST_ENV = "FDAGENTS_SEMANTIC_REPLAY_MANIFEST"
MANIFEST_SHA256_ENV = "FDAGENTS_SEMANTIC_REPLAY_MANIFEST_SHA256"
VIVADO_BIN_ENV = "FDAGENTS_VIVADO_BIN"
VITIS_HLS_BIN_ENV = "FDAGENTS_VITIS_HLS_BIN"
RAPIDWRIGHT_PYTHON_ENV = "FDAGENTS_RAPIDWRIGHT_PYTHON"
MAX_CANDIDATES = 1
MAX_PIPELINE_STAGES = 8
_PACKAGED_REPLAY_ROOT = Path(__file__).resolve().parent.parent / "semantic_replay_bundles"
# Empty on purpose.  This catalog keyed a shipped replay bundle by the SHA256
# of a specific benchmark input, which is the benchmark-specific identifier the
# development protocol forbids, and every result we have measured was measured
# without it: cold isolation forbids replay-derived actions, so the regression
# runs cannot reach this path at all.
#
# Scored, the two benchmarks that had bundles are the two that spent their
# window for nothing -- optical-flow 2287s for +4.93 MHz and vtr_mcml_v2 3138s
# for +1.89 -- while semantic_replay alone is estimated at 3000s of a 3600s
# budget.  Without a bundle the same designs reach +88.00 and +3.23 here.
# Ship the behaviour we measured.
_PACKAGED_REPLAY_CATALOG: dict[str, dict[str, str]] = {}
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ID_RE = re.compile(r"[a-z][a-z0-9_]{0,47}")
_V1_MANIFEST_FIELDS = {
    "schema_version",
    "stage_type",
    "source_dcp_sha256",
    "assets",
    "script_asset_id",
    "tclargs",
    "candidate_relative_path",
}
_V2_MANIFEST_FIELDS = {
    "schema_version",
    "source_dcp_sha256",
    "assets",
    "stages",
    "final_stage_id",
}
_V2_STAGE_FIELDS = {
    "vitis_hls_v1": {
        "id", "type", "asset_ids", "script_asset_id", "output_relative_path",
    },
    "vivado_batch_tcl_v1": {
        "id", "type", "script_asset_id", "args", "output_relative_path",
    },
    "rapidwright_python_v1": {
        "id", "type", "script_asset_id", "args", "output_relative_path",
    },
}
_PROOF_STAGE_TYPES = {"vivado_batch_tcl_v1", "typed_pipeline_v1"}
_STAGE_LOG_NAME = ".semantic_replay.log"
_EXECUTION_PARAMS = {
    "input_dcp",
    "max_candidates",
    "implementation_timeout_s",
    "validation_timeout_s",
    "validation_reserve_s",
    "min_candidate_remaining_s",
    "_execution_deadline_monotonic",
    "_candidate_commit_reserve_s",
    "_source_artifact_sha256",
}
_STRICT_REPORT_FIELDS = {
    "schema_version",
    "strict_mode",
    "validator_protocol",
    "stimulus_protocol",
    "vectors",
    "precheck_vectors",
    "root_sha256",
    "root_sha256_after",
    "root_size_bytes",
    "candidate_sha256",
    "candidate_sha256_after",
    "candidate_size_bytes",
    "phase1_passed",
    "phase2_passed",
    "phase2_skipped",
    "infrastructure_failure",
    "process_exit_code",
}


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, allow_nan=False))


def _identity(value: Any, name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 identity")
    return value


def _safe_relative_path(raw: Any, name: str) -> PurePosixPath:
    if not isinstance(raw, str) or not raw or "\\" in raw or "\x00" in raw:
        raise ValueError(f"{name} must be a nonempty POSIX relative path")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{name} must remain below the replay root")
    return path


def _safe_stage_relative_path(raw: Any, name: str) -> PurePosixPath:
    path = _safe_relative_path(raw, name)
    if path.parts[0] == _STAGE_LOG_NAME:
        raise ValueError(f"{name} uses a reserved stage path")
    return path


def _regular_file_below(root: Path, relative: PurePosixPath, name: str) -> Path:
    candidate = root.joinpath(*relative.parts)
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"{name} cannot traverse symlinks")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{name} escapes the replay root") from exc
    if not resolved.is_file() or resolved.stat().st_size <= 0:
        raise ValueError(f"{name} is not a nonempty regular file")
    return resolved


def _existing_output_below(root: Path, relative: PurePosixPath, name: str) -> Path:
    candidate = root.joinpath(*relative.parts)
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"{name} cannot traverse symlinks")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{name} escapes its stage workspace") from exc
    if not resolved.exists() or not (resolved.is_file() or resolved.is_dir()):
        raise ValueError(f"{name} is missing or has an unsupported file type")
    if resolved.is_file() and resolved.stat().st_size <= 0:
        raise ValueError(f"{name} is an empty file")
    if resolved.is_dir():
        has_entry = False
        for descendant in resolved.rglob("*"):
            has_entry = True
            if descendant.is_symlink():
                raise ValueError(f"{name} contains a symlink")
            if not (descendant.is_file() or descendant.is_dir()):
                raise ValueError(f"{name} contains an unsupported file type")
        if not has_entry:
            raise ValueError(f"{name} is an empty directory")
    return resolved


def _nonexisting_path_below(root: Path, relative: PurePosixPath, name: str) -> Path:
    candidate = root.joinpath(*relative.parts)
    cursor = root
    for part in relative.parts[:-1]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"{name} cannot traverse symlinks")
        if not cursor.exists():
            break
        if not cursor.is_dir():
            raise ValueError(f"{name} parent is not a directory")
    if candidate.exists() or candidate.is_symlink():
        raise ValueError(f"{name} must not exist before stage execution")
    return candidate


def _load_assets(
    manifest_path: Path, raw_assets: Any, *, maximum: int, unique_paths: bool
) -> tuple[list[dict[str, str]], dict[str, Path]]:
    if not isinstance(raw_assets, list) or not 1 <= len(raw_assets) <= maximum:
        raise ValueError(f"semantic replay assets must contain 1..{maximum} records")
    root = manifest_path.parent.resolve()
    assets: dict[str, Path] = {}
    normalized_assets = []
    relative_paths: set[str] = set()
    for record in raw_assets:
        if not isinstance(record, dict) or set(record) != {
            "id", "relative_path", "sha256"
        }:
            raise ValueError("semantic replay asset record is malformed")
        asset_id = record.get("id")
        if not isinstance(asset_id, str) or _ID_RE.fullmatch(asset_id) is None:
            raise ValueError("semantic replay asset id is invalid")
        if asset_id in assets:
            raise ValueError("semantic replay asset ids must be unique")
        expected_sha256 = _identity(record.get("sha256"), f"asset {asset_id}")
        relative = _safe_relative_path(record.get("relative_path"), f"asset {asset_id}")
        if unique_paths and relative.as_posix() in relative_paths:
            raise ValueError("semantic replay asset relative paths must be unique")
        asset_path = _regular_file_below(root, relative, f"asset {asset_id}")
        if asset_path.stat().st_size > 67_108_864:
            raise ValueError(f"asset {asset_id} exceeds the size bound")
        if sha256_file(asset_path) != expected_sha256:
            raise ValueError(f"asset {asset_id} identity mismatch")
        assets[asset_id] = asset_path
        relative_paths.add(relative.as_posix())
        normalized_assets.append({
            "id": asset_id,
            "relative_path": relative.as_posix(),
            "sha256": expected_sha256,
        })
    return normalized_assets, assets


def _normalize_v2_arg(
    entry: Any,
    *,
    stage_type: str,
    assets: dict[str, Path],
    prior_stage_ids: set[str],
) -> dict[str, str]:
    if not isinstance(entry, dict):
        raise ValueError("semantic replay stage argument is malformed")
    kind = entry.get("kind")
    if stage_type == "vivado_batch_tcl_v1":
        if kind == "asset" and set(entry) == {"kind", "asset_id"}:
            if entry.get("asset_id") not in assets:
                raise ValueError("semantic replay stage argument references an unknown asset")
            return {"kind": kind, "asset_id": entry["asset_id"]}
    if (
        stage_type in {"vivado_batch_tcl_v1", "rapidwright_python_v1"}
        and kind == "source_dcp"
        and set(entry) == {"kind"}
    ):
        return {"kind": kind}
    if kind == "stage_output" and set(entry) == {
        "kind", "stage_id", "relative_path"
    }:
        stage_id = entry.get("stage_id")
        if stage_id not in prior_stage_ids:
            raise ValueError("semantic replay stage argument has a forward or unknown reference")
        relative = _safe_stage_relative_path(
            entry.get("relative_path"), "stage_output relative_path"
        )
        return {
            "kind": kind,
            "stage_id": stage_id,
            "relative_path": relative.as_posix(),
        }
    if kind == "stage_output_target" and set(entry) == {"kind", "relative_path"}:
        relative = _safe_stage_relative_path(
            entry.get("relative_path"), "stage_output_target relative_path"
        )
        return {"kind": kind, "relative_path": relative.as_posix()}
    raise ValueError(f"semantic replay {stage_type} argument kind is unsupported")


def load_bound_manifest() -> tuple[dict[str, Any], str, dict[str, Path]]:
    """Load the hidden manifest and verify every transitive input artifact."""
    raw_path = os.environ.get(MANIFEST_ENV, "")
    expected_manifest_sha256 = os.environ.get(MANIFEST_SHA256_ENV, "")
    _identity(expected_manifest_sha256, MANIFEST_SHA256_ENV)
    if not raw_path or "\x00" in raw_path:
        raise ValueError(f"{MANIFEST_ENV} is not configured")
    manifest_path = Path(raw_path).expanduser()
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("semantic replay manifest must be a regular non-symlink file")
    manifest_path = manifest_path.resolve()
    manifest_bytes = manifest_path.read_bytes()
    if not manifest_bytes or len(manifest_bytes) > 65_536:
        raise ValueError("semantic replay manifest size is outside bounds")
    actual_manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if actual_manifest_sha256 != expected_manifest_sha256:
        raise ValueError("semantic replay manifest identity mismatch")
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("semantic replay manifest must be an object")
    schema_version = manifest.get("schema_version")
    expected_fields = {
        1: _V1_MANIFEST_FIELDS,
        2: _V2_MANIFEST_FIELDS,
    }.get(schema_version)
    if expected_fields is None:
        raise ValueError("unsupported semantic replay manifest version")
    if set(manifest) != expected_fields:
        raise ValueError("semantic replay manifest has missing or unsupported fields")
    _identity(manifest.get("source_dcp_sha256"), "manifest source_dcp_sha256")
    normalized_assets, assets = _load_assets(
        manifest_path,
        manifest.get("assets"),
        maximum=16 if schema_version == 1 else 32,
        unique_paths=schema_version == 2,
    )

    if schema_version == 2:
        raw_stages = manifest.get("stages")
        if not isinstance(raw_stages, list) or not 1 <= len(raw_stages) <= MAX_PIPELINE_STAGES:
            raise ValueError(
                f"semantic replay stages must contain 1..{MAX_PIPELINE_STAGES} records"
            )
        normalized_stages = []
        prior_stage_ids: set[str] = set()
        for raw_stage in raw_stages:
            if not isinstance(raw_stage, dict):
                raise ValueError("semantic replay stage record is malformed")
            stage_type = raw_stage.get("type")
            stage_fields = _V2_STAGE_FIELDS.get(stage_type)
            if stage_fields is None:
                raise ValueError("unsupported semantic replay stage type")
            if set(raw_stage) != stage_fields:
                raise ValueError("semantic replay stage has missing or unsupported fields")
            stage_id = raw_stage.get("id")
            if not isinstance(stage_id, str) or _ID_RE.fullmatch(stage_id) is None:
                raise ValueError("semantic replay stage id is invalid")
            if stage_id in prior_stage_ids:
                raise ValueError("semantic replay stage ids must be unique")
            script_id = raw_stage.get("script_asset_id")
            if script_id not in assets:
                raise ValueError("semantic replay stage script asset is unavailable")
            output_relative = _safe_stage_relative_path(
                raw_stage.get("output_relative_path"),
                f"stage {stage_id} output_relative_path",
            )
            normalized_stage: dict[str, Any] = {
                "id": stage_id,
                "type": stage_type,
                "script_asset_id": script_id,
                "output_relative_path": output_relative.as_posix(),
            }
            if stage_type == "vitis_hls_v1":
                asset_ids = raw_stage.get("asset_ids")
                if (
                    not isinstance(asset_ids, list)
                    or not 1 <= len(asset_ids) <= 32
                    or any(not isinstance(item, str) for item in asset_ids)
                    or len(set(asset_ids)) != len(asset_ids)
                    or any(item not in assets for item in asset_ids)
                ):
                    raise ValueError("semantic replay Vitis asset_ids are invalid")
                if script_id not in asset_ids:
                    raise ValueError("semantic replay Vitis script must be a copied asset")
                copied_paths = [
                    PurePosixPath(next(
                        record["relative_path"]
                        for record in normalized_assets
                        if record["id"] == asset_id
                    ))
                    for asset_id in asset_ids
                ]
                for index, path in enumerate(copied_paths):
                    if (
                        path == output_relative
                        or path in output_relative.parents
                        or output_relative in path.parents
                    ):
                        raise ValueError(
                            "semantic replay Vitis output conflicts with a copied asset"
                        )
                    for other in copied_paths[index + 1:]:
                        if path in other.parents or other in path.parents:
                            raise ValueError("semantic replay Vitis copied asset paths conflict")
                normalized_stage["asset_ids"] = list(asset_ids)
            else:
                raw_args = raw_stage.get("args")
                if not isinstance(raw_args, list) or len(raw_args) > 16:
                    raise ValueError("semantic replay stage args must contain 0..16 entries")
                normalized_stage["args"] = [
                    _normalize_v2_arg(
                        entry,
                        stage_type=stage_type,
                        assets=assets,
                        prior_stage_ids=prior_stage_ids,
                    )
                    for entry in raw_args
                ]
            normalized_stages.append(normalized_stage)
            prior_stage_ids.add(stage_id)
        final_stage_id = manifest.get("final_stage_id")
        if final_stage_id not in prior_stage_ids:
            raise ValueError("semantic replay final_stage_id is unavailable")
        return {
            "schema_version": 2,
            "source_dcp_sha256": manifest["source_dcp_sha256"],
            "assets": normalized_assets,
            "stages": normalized_stages,
            "final_stage_id": final_stage_id,
        }, actual_manifest_sha256, assets

    if manifest.get("stage_type") != "vivado_batch_tcl_v1":
        raise ValueError("unsupported semantic replay stage type")
    script_id = manifest.get("script_asset_id")
    if script_id not in assets:
        raise ValueError("semantic replay script asset is unavailable")

    raw_args = manifest.get("tclargs")
    if not isinstance(raw_args, list) or not 1 <= len(raw_args) <= 16:
        raise ValueError("semantic replay tclargs must contain 1..16 typed entries")
    normalized_args = []
    for entry in raw_args:
        if not isinstance(entry, dict):
            raise ValueError("semantic replay tclarg entry is malformed")
        kind = entry.get("kind")
        if kind == "asset" and set(entry) == {"kind", "asset_id"}:
            if entry.get("asset_id") not in assets:
                raise ValueError("semantic replay tclarg references an unknown asset")
            normalized_args.append({"kind": kind, "asset_id": entry["asset_id"]})
        elif kind in {"source_dcp", "output_dir"} and set(entry) == {"kind"}:
            normalized_args.append({"kind": kind})
        else:
            raise ValueError("semantic replay tclarg kind is unsupported")
    candidate_relative = _safe_relative_path(
        manifest.get("candidate_relative_path"), "candidate_relative_path"
    )
    normalized = {
        **manifest,
        "assets": normalized_assets,
        "tclargs": normalized_args,
        "candidate_relative_path": candidate_relative.as_posix(),
    }
    return normalized, actual_manifest_sha256, assets


def bind_packaged_semantic_replay(
    source_sha256: str, *, cold_isolation: bool = False
) -> Optional[dict[str, Any]]:
    """Bind a packaged source rebuild when the immutable input hash matches."""
    source_sha256 = _identity(source_sha256, "source_sha256")
    if cold_isolation:
        return None
    if os.environ.get(MANIFEST_ENV) or os.environ.get(MANIFEST_SHA256_ENV):
        return None
    record = _PACKAGED_REPLAY_CATALOG.get(source_sha256)
    if record is None:
        return None

    manifest_relative = _safe_relative_path(
        record["manifest_relative_path"], "packaged manifest_relative_path"
    )
    manifest_path = _regular_file_below(
        _PACKAGED_REPLAY_ROOT.resolve(), manifest_relative, "packaged replay manifest"
    )
    expected_manifest_sha256 = _identity(
        record["manifest_sha256"], "packaged manifest_sha256"
    )
    if sha256_file(manifest_path) != expected_manifest_sha256:
        raise ValueError("packaged semantic replay manifest identity mismatch")

    os.environ[MANIFEST_ENV] = str(manifest_path)
    os.environ[MANIFEST_SHA256_ENV] = expected_manifest_sha256
    try:
        manifest, actual_manifest_sha256, _assets = load_bound_manifest()
        if (
            actual_manifest_sha256 != expected_manifest_sha256
            or manifest["source_dcp_sha256"] != source_sha256
        ):
            raise ValueError("packaged semantic replay source binding mismatch")
    except Exception:
        os.environ.pop(MANIFEST_ENV, None)
        os.environ.pop(MANIFEST_SHA256_ENV, None)
        raise

    return {
        "schema_version": 1,
        "kind": "packaged_semantic_replay",
        "bundle_id": record["bundle_id"],
        "source_dcp_sha256": source_sha256,
        "manifest_relative_path": manifest_relative.as_posix(),
        "manifest_sha256": expected_manifest_sha256,
    }


def clear_packaged_semantic_replay(binding: Optional[dict[str, Any]]) -> None:
    """Remove only the environment binding installed by the packaged catalog."""
    if not isinstance(binding, dict):
        return
    relative = _safe_relative_path(
        binding.get("manifest_relative_path"), "packaged manifest_relative_path"
    )
    expected_path = str(
        _regular_file_below(
            _PACKAGED_REPLAY_ROOT.resolve(), relative, "packaged replay manifest"
        )
    )
    if (
        os.environ.get(MANIFEST_ENV) == expected_path
        and os.environ.get(MANIFEST_SHA256_ENV) == binding.get("manifest_sha256")
    ):
        os.environ.pop(MANIFEST_ENV, None)
        os.environ.pop(MANIFEST_SHA256_ENV, None)


def build_semantic_replay_proof(
    *,
    manifest_sha256: str,
    stage_type: str,
    source_sha256: str,
    candidate_sha256: str,
    asset_sha256s: list[str],
    strict_report: dict[str, Any],
) -> dict[str, Any]:
    """Build the exact proof accepted by the agent's high-risk gate."""
    authorization = {
        "kind": "semantic_replay_authorization",
        "version": 1,
        "manifest_sha256": _identity(manifest_sha256, "manifest_sha256"),
        "stage_type": stage_type,
        "source_dcp_sha256": _identity(source_sha256, "source_sha256"),
        "asset_sha256s": sorted(
            _identity(value, "asset_sha256") for value in asset_sha256s
        ),
    }
    if stage_type not in _PROOF_STAGE_TYPES or not authorization["asset_sha256s"]:
        raise ValueError("semantic replay authorization is unsupported")
    authorization["authorization_id"] = _digest(authorization)
    report = _validated_strict_report(
        strict_report,
        source_sha256=source_sha256,
        candidate_sha256=candidate_sha256,
    )
    certificate = {
        "kind": "semantic_replay_post_emission_validation",
        "version": 1,
        "status": "proved",
        "authority": "candidate_measurement_only",
        "source_dcp_sha256": source_sha256,
        "candidate_dcp_sha256": _identity(candidate_sha256, "candidate_sha256"),
        "authorization_id": authorization["authorization_id"],
        "strict_validation": report,
        "claims": {
            "hash_bound_closed_stage": True,
            "strict_structural_and_bounded_simulation": True,
            "formal_whole_dcp_equivalence": False,
            "final_submission_authority": False,
        },
    }
    certificate["certificate_id"] = _digest(certificate)
    return {
        "proof_version": PROOF_VERSION,
        "skill": NAME,
        "source_artifact_sha256": source_sha256,
        "artifact_sha256": candidate_sha256,
        "replay_authorization": authorization,
        "post_emission_validation": certificate,
    }


def _validated_strict_report(
    report: dict[str, Any], *, source_sha256: str, candidate_sha256: str
) -> dict[str, Any]:
    if not isinstance(report, dict) or set(report) != _STRICT_REPORT_FIELDS:
        raise ValueError("semantic replay strict report fields are invalid")
    if (
        report.get("schema_version") != 1
        or report.get("strict_mode") is not True
        or report.get("validator_protocol") != STRICT_VALIDATOR_PROTOCOL
        or report.get("stimulus_protocol") != STRICT_STIMULUS_PROTOCOL
        or report.get("root_sha256") != source_sha256
        or report.get("root_sha256_after") != source_sha256
        or report.get("candidate_sha256") != candidate_sha256
        or report.get("candidate_sha256_after") != candidate_sha256
        or report.get("phase1_passed") is not True
        or report.get("phase2_passed") is not True
        or report.get("phase2_skipped") is not False
        or report.get("infrastructure_failure") is not False
        or report.get("process_exit_code") != 0
    ):
        raise ValueError("semantic replay strict validation did not pass")
    vectors = report.get("vectors")
    precheck = report.get("precheck_vectors")
    if (
        isinstance(vectors, bool)
        or not isinstance(vectors, int)
        or vectors < MIN_POST_EMISSION_VECTORS
        or isinstance(precheck, bool)
        or not isinstance(precheck, int)
        or precheck < MIN_POST_EMISSION_PRECHECK_VECTORS
        or precheck > vectors
    ):
        raise ValueError("semantic replay strict validation coverage is insufficient")
    for key in ("root_size_bytes", "candidate_size_bytes"):
        value = report.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"semantic replay strict report {key} is invalid")
    return _json_copy(report)


def verify_semantic_replay_proof(
    proof: dict[str, Any], *, source_sha256: str, candidate_sha256: str
) -> bool:
    """Replay a semantic-replay authorization and strict certificate."""
    try:
        required = {
            "proof_version",
            "skill",
            "source_artifact_sha256",
            "artifact_sha256",
            "replay_authorization",
            "post_emission_validation",
        }
        if not isinstance(proof, dict) or set(proof) != required:
            return False
        if (
            proof.get("proof_version") != PROOF_VERSION
            or proof.get("skill") != NAME
            or proof.get("source_artifact_sha256") != source_sha256
            or proof.get("artifact_sha256") != candidate_sha256
        ):
            return False
        authorization = proof.get("replay_authorization")
        authorization_fields = {
            "kind",
            "version",
            "manifest_sha256",
            "stage_type",
            "source_dcp_sha256",
            "asset_sha256s",
            "authorization_id",
        }
        if not isinstance(authorization, dict) or set(authorization) != authorization_fields:
            return False
        authorization_payload = dict(authorization)
        authorization_id = authorization_payload.pop("authorization_id")
        assets = authorization.get("asset_sha256s")
        if (
            authorization.get("kind") != "semantic_replay_authorization"
            or authorization.get("version") != 1
            or authorization.get("stage_type") not in _PROOF_STAGE_TYPES
            or authorization.get("source_dcp_sha256") != source_sha256
            or _SHA256_RE.fullmatch(str(authorization.get("manifest_sha256"))) is None
            or not isinstance(assets, list)
            or not assets
            or assets != sorted(assets)
            or any(_SHA256_RE.fullmatch(str(item)) is None for item in assets)
            or authorization_id != _digest(authorization_payload)
        ):
            return False
        certificate = proof.get("post_emission_validation")
        certificate_fields = {
            "kind",
            "version",
            "status",
            "authority",
            "source_dcp_sha256",
            "candidate_dcp_sha256",
            "authorization_id",
            "strict_validation",
            "claims",
            "certificate_id",
        }
        if not isinstance(certificate, dict) or set(certificate) != certificate_fields:
            return False
        certificate_payload = dict(certificate)
        certificate_id = certificate_payload.pop("certificate_id")
        if (
            certificate.get("kind") != "semantic_replay_post_emission_validation"
            or certificate.get("version") != 1
            or certificate.get("status") != "proved"
            or certificate.get("authority") != "candidate_measurement_only"
            or certificate.get("source_dcp_sha256") != source_sha256
            or certificate.get("candidate_dcp_sha256") != candidate_sha256
            or certificate.get("authorization_id") != authorization_id
            or certificate.get("claims") != {
                "hash_bound_closed_stage": True,
                "strict_structural_and_bounded_simulation": True,
                "formal_whole_dcp_equivalence": False,
                "final_submission_authority": False,
            }
            or certificate_id != _digest(certificate_payload)
        ):
            return False
        _validated_strict_report(
            certificate.get("strict_validation"),
            source_sha256=source_sha256,
            candidate_sha256=candidate_sha256,
        )
        return True
    except (TypeError, ValueError):
        return False


def _resolve_executable(executable: str, unavailable: str) -> str:
    if not executable or "\x00" in executable:
        raise RuntimeError(unavailable)
    if "/" in executable:
        path = Path(executable).expanduser().absolute()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise RuntimeError(unavailable)
        return str(path)
    resolved = shutil.which(executable)
    if not resolved:
        raise RuntimeError(unavailable)
    return str(Path(resolved).absolute())


def _vivado_executable() -> str:
    configured = os.environ.get(VIVADO_BIN_ENV, "").strip()
    return _resolve_executable(
        configured or shutil.which("vivado") or "",
        "Vivado executable is unavailable",
    )


def _vitis_hls_executable() -> str:
    configured = os.environ.get(VITIS_HLS_BIN_ENV, "").strip()
    return _resolve_executable(
        configured or shutil.which("vitis-run") or "",
        "Vitis HLS executable is unavailable",
    )


def _rapidwright_python_executable() -> str:
    configured = os.environ.get(RAPIDWRIGHT_PYTHON_ENV, "").strip()
    return _resolve_executable(
        configured or sys.executable,
        "RapidWright Python executable is unavailable",
    )


async def _run_fixed_process(
    *,
    argv: tuple[str, ...],
    cwd: Path,
    log_path: Path,
    timeout_s: float,
    label: str,
    env: Optional[dict[str, str]] = None,
) -> None:
    if (
        not argv
        or any(not isinstance(item, str) or not item or "\x00" in item for item in argv)
        or timeout_s <= 0
    ):
        raise ValueError("semantic replay fixed process invocation is invalid")
    with log_path.open("xb") as log_file:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdout=log_file,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout_s)
        except asyncio.CancelledError:
            await asyncio.shield(_terminate_process_group(process, 10.0))
            raise
        except asyncio.TimeoutError:
            await _terminate_process_group(process, 10.0)
            raise
    if process.returncode != 0:
        raise RuntimeError(f"{label} exited with status {process.returncode}")


async def _run_vivado_stage(
    *, script: Path, args: list[str], cwd: Path, log_path: Path, timeout_s: float
) -> None:
    await _run_fixed_process(
        argv=(
            _vivado_executable(),
            "-mode",
            "batch",
            "-nolog",
            "-nojournal",
            "-notrace",
            "-source",
            str(script),
            "-tclargs",
            *args,
        ),
        cwd=cwd,
        log_path=log_path,
        timeout_s=timeout_s,
        label="Vivado replay stage",
    )


async def _run_vitis_hls_stage(
    *, script: Path, cwd: Path, log_path: Path, timeout_s: float
) -> None:
    await _run_fixed_process(
        argv=(_vitis_hls_executable(), "--mode", "hls", "--tcl", str(script)),
        cwd=cwd,
        log_path=log_path,
        timeout_s=timeout_s,
        label="Vitis HLS replay stage",
    )


async def _run_rapidwright_python_stage(
    *, script: Path, args: list[str], cwd: Path, log_path: Path, timeout_s: float
) -> None:
    env = os.environ.copy()
    mcp_python_root = Path(__file__).resolve().parents[2] / "RapidWrightMCP"
    if mcp_python_root.is_dir():
        current_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            str(mcp_python_root)
            if not current_pythonpath
            else f"{mcp_python_root}{os.pathsep}{current_pythonpath}"
        )
    await _run_fixed_process(
        argv=(_rapidwright_python_executable(), str(script), *args),
        cwd=cwd,
        log_path=log_path,
        timeout_s=timeout_s,
        label="RapidWright Python replay stage",
        env=env,
    )


def _remaining_timeout(
    params: dict[str, Any], *, requested: float, reserve_s: float
) -> float:
    deadline = params.get("_execution_deadline_monotonic")
    if deadline is None:
        return requested
    return min(requested, float(deadline) - time.monotonic() - reserve_s)


def _stage_timeout(implementation_deadline: float) -> float:
    remaining = implementation_deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("semantic replay implementation budget is exhausted")
    return remaining


def _typed_stage_args(
    stage: dict[str, Any],
    *,
    input_dcp: Path,
    assets: dict[str, Path],
    workspaces: dict[str, Path],
    workspace: Path,
) -> list[str]:
    args = []
    for entry in stage["args"]:
        kind = entry["kind"]
        if kind == "asset":
            args.append(str(assets[entry["asset_id"]]))
        elif kind == "source_dcp":
            args.append(str(input_dcp))
        elif kind == "stage_output":
            prior_workspace = workspaces[entry["stage_id"]]
            relative = _safe_stage_relative_path(
                entry["relative_path"], "stage_output relative_path"
            )
            args.append(str(_existing_output_below(
                prior_workspace, relative, "semantic replay prior stage output"
            )))
        elif kind == "stage_output_target":
            relative = _safe_stage_relative_path(
                entry["relative_path"], "stage_output_target relative_path"
            )
            args.append(str(_nonexisting_path_below(
                workspace, relative, "semantic replay stage output target"
            )))
        else:
            raise ValueError("semantic replay stage argument kind is unsupported")
    return args


async def _run_typed_pipeline(
    *,
    manifest: dict[str, Any],
    assets: dict[str, Path],
    input_dcp: Path,
    action_dir: Path,
    implementation_deadline: float,
) -> Path:
    pipeline_root = action_dir / "semantic_replay_pipeline"
    pipeline_root.mkdir(mode=0o700, parents=False, exist_ok=False)
    asset_records = {record["id"]: record for record in manifest["assets"]}
    workspaces: dict[str, Path] = {}
    declared_outputs: dict[str, PurePosixPath] = {}
    copied_asset_paths: list[tuple[Path, str]] = []

    for stage in manifest["stages"]:
        stage_id = stage["id"]
        stage_type = stage["type"]
        workspace = pipeline_root / stage_id
        workspace.mkdir(mode=0o700, parents=False, exist_ok=False)
        workspaces[stage_id] = workspace
        output_relative = _safe_stage_relative_path(
            stage["output_relative_path"], f"stage {stage_id} output_relative_path"
        )
        _nonexisting_path_below(
            workspace, output_relative, f"stage {stage_id} declared output"
        )
        log_path = workspace / _STAGE_LOG_NAME

        if stage_type == "vitis_hls_v1":
            copied_assets: dict[str, Path] = {}
            for asset_id in stage["asset_ids"]:
                relative = _safe_relative_path(
                    asset_records[asset_id]["relative_path"], f"asset {asset_id}"
                )
                destination = workspace.joinpath(*relative.parts)
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if destination.exists() or destination.is_symlink():
                    raise ValueError("semantic replay copied asset destination already exists")
                shutil.copyfile(assets[asset_id], destination)
                copied = _regular_file_below(workspace, relative, f"copied asset {asset_id}")
                if sha256_file(copied) != asset_records[asset_id]["sha256"]:
                    raise RuntimeError(f"copied semantic replay asset {asset_id} changed")
                copied_assets[asset_id] = copied
                copied_asset_paths.append((copied, asset_records[asset_id]["sha256"]))
            hls_script = copied_assets[stage["script_asset_id"]]
            await _run_vitis_hls_stage(
                script=hls_script,
                cwd=hls_script.parent,
                log_path=log_path,
                timeout_s=_stage_timeout(implementation_deadline),
            )
        else:
            args = _typed_stage_args(
                stage,
                input_dcp=input_dcp,
                assets=assets,
                workspaces=workspaces,
                workspace=workspace,
            )
            if stage_type == "vivado_batch_tcl_v1":
                await _run_vivado_stage(
                    script=assets[stage["script_asset_id"]],
                    args=args,
                    cwd=workspace,
                    log_path=log_path,
                    timeout_s=_stage_timeout(implementation_deadline),
                )
            else:
                await _run_rapidwright_python_stage(
                    script=assets[stage["script_asset_id"]],
                    args=args,
                    cwd=workspace,
                    log_path=log_path,
                    timeout_s=_stage_timeout(implementation_deadline),
                )
        stage_output = _existing_output_below(
            workspace, output_relative, f"stage {stage_id} declared output"
        )
        if stage_type == "vitis_hls_v1" and not stage_output.is_dir():
            raise ValueError("semantic replay Vitis output must be an RTL directory")
        declared_outputs[stage_id] = output_relative

    for stage_id, output_relative in declared_outputs.items():
        _existing_output_below(
            workspaces[stage_id], output_relative, f"stage {stage_id} declared output"
        )
    if any(
        path.is_symlink() or not path.is_file() or sha256_file(path) != expected_sha256
        for path, expected_sha256 in copied_asset_paths
    ):
        raise RuntimeError("a copied semantic replay asset changed during implementation")
    final_stage_id = manifest["final_stage_id"]
    return _regular_file_below(
        workspaces[final_stage_id],
        declared_outputs[final_stage_id],
        "semantic replay candidate",
    )


class SemanticReplaySkill:
    """Produce one fresh candidate through a hidden, typed replay manifest."""

    name = NAME

    async def execute(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        clock_period: Optional[float],
    ) -> SkillResult:
        del clock_period
        started = time.monotonic()
        action_dir = Path(mcp.run_dir).resolve()
        failed_dcp = action_dir / "semantic_replay_failed.dcp"
        try:
            if target != TARGET:
                raise ValueError("semantic_replay accepts only target='auto'")
            unknown = sorted(str(key) for key in params if str(key) not in _EXECUTION_PARAMS)
            if unknown:
                raise ValueError(f"semantic_replay rejected parameters {unknown}")
            if params.get("max_candidates", MAX_CANDIDATES) != MAX_CANDIDATES:
                raise ValueError("semantic_replay max_candidates must be exactly 1")
            raw_input_dcp = Path(str(params.get("input_dcp") or "")).expanduser()
            if raw_input_dcp.is_symlink() or not raw_input_dcp.is_file():
                raise ValueError("semantic_replay exact input DCP is unavailable")
            input_dcp = raw_input_dcp.resolve()
            source_sha256 = sha256_file(input_dcp)
            expected_source = params.get("_source_artifact_sha256")
            if expected_source != source_sha256:
                raise ValueError("semantic_replay source identity mismatch")

            manifest, manifest_sha256, assets = load_bound_manifest()
            if manifest["source_dcp_sha256"] != source_sha256:
                raise ValueError("semantic replay manifest is not bound to the source DCP")
            commit_reserve = max(
                float(params.get("_candidate_commit_reserve_s", 0.0)),
                float(params.get("min_candidate_remaining_s", 0.0)),
            )
            validation_reserve = max(
                commit_reserve, float(params.get("validation_reserve_s", 900.0))
            )
            implementation_timeout = _remaining_timeout(
                params,
                requested=float(params.get("implementation_timeout_s", 2400.0)),
                reserve_s=validation_reserve,
            )
            if implementation_timeout <= 0:
                raise TimeoutError("semantic replay implementation budget is exhausted")
            implementation_deadline = time.monotonic() + implementation_timeout
            if manifest["schema_version"] == 1:
                stage_dir = action_dir / "semantic_replay_stage"
                stage_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
                output_dir = stage_dir / "implementation"
                output_dir.mkdir(mode=0o700)
                script = assets[manifest["script_asset_id"]]
                tclargs = []
                for entry in manifest["tclargs"]:
                    if entry["kind"] == "asset":
                        tclargs.append(str(assets[entry["asset_id"]]))
                    elif entry["kind"] == "source_dcp":
                        tclargs.append(str(input_dcp))
                    else:
                        tclargs.append(str(output_dir))
                await _run_vivado_stage(
                    script=script,
                    args=tclargs,
                    cwd=stage_dir,
                    log_path=stage_dir / "vivado.log",
                    timeout_s=implementation_timeout,
                )
                candidate_relative = _safe_relative_path(
                    manifest["candidate_relative_path"], "candidate_relative_path"
                )
                candidate = _regular_file_below(
                    output_dir, candidate_relative, "semantic replay candidate"
                )
                authorization_stage_type = manifest["stage_type"]
                candidate_label = "semantic replay hash-bound Vivado candidate"
            else:
                candidate = await _run_typed_pipeline(
                    manifest=manifest,
                    assets=assets,
                    input_dcp=input_dcp,
                    action_dir=action_dir,
                    implementation_deadline=implementation_deadline,
                )
                authorization_stage_type = "typed_pipeline_v1"
                candidate_label = "semantic replay hash-bound typed pipeline candidate"
            if (
                input_dcp.is_symlink()
                or not input_dcp.is_file()
                or sha256_file(input_dcp) != source_sha256
            ):
                raise RuntimeError("semantic replay source changed during implementation")
            expected_assets = {
                record["id"]: record["sha256"] for record in manifest["assets"]
            }
            if any(
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_size <= 0
                or path.stat().st_size > 67_108_864
                or sha256_file(path) != expected_assets[asset_id]
                for asset_id, path in assets.items()
            ):
                raise RuntimeError("semantic replay asset changed during implementation")
            candidate_sha256 = sha256_file(candidate)

            validation_timeout = _remaining_timeout(
                params,
                requested=float(params.get("validation_timeout_s", 1800.0)),
                reserve_s=commit_reserve,
            )
            if validation_timeout <= 0:
                raise TimeoutError("semantic replay validation budget is exhausted")
            deadline = params.get("_execution_deadline_monotonic")
            validation_deadline = (
                float(deadline) - commit_reserve if deadline is not None else None
            )
            strict_report = await run_strict_validation_worker(
                input_dcp,
                candidate,
                action_dir / "semantic_replay_validation.json",
                vectors=MIN_POST_EMISSION_VECTORS,
                precheck_vectors=MIN_POST_EMISSION_PRECHECK_VECTORS,
                timeout_s=validation_timeout,
                deadline_monotonic=validation_deadline,
            )
            proof = build_semantic_replay_proof(
                manifest_sha256=manifest_sha256,
                stage_type=authorization_stage_type,
                source_sha256=source_sha256,
                candidate_sha256=candidate_sha256,
                asset_sha256s=list(expected_assets.values()),
                strict_report=strict_report,
            )
            evidence = {
                "schema_version": 1,
                "stage_type": authorization_stage_type,
                "manifest_sha256": manifest_sha256,
                "source_dcp_sha256": source_sha256,
                "candidate_dcp_sha256": candidate_sha256,
                "asset_sha256s": sorted(expected_assets.values()),
                "strict_validation_vectors": MIN_POST_EMISSION_VECTORS,
                "strict_validation_precheck_vectors": (
                    MIN_POST_EMISSION_PRECHECK_VECTORS
                ),
                "acceptance_authority": "central measurement and final strict gate",
            }
            output = SkillOutput(
                candidate,
                candidate_label,
                evidence,
                proof,
            )
            return SkillResult(
                success=True,
                before_wns=before_wns,
                after_wns=before_wns,
                delta_wns=0.0,
                is_legal=True,
                output_dcp=candidate,
                summary="semantic_replay candidate=1 proof=semantic-replay-v1",
                details={
                    "proof_version": PROOF_VERSION,
                    "stage_type": authorization_stage_type,
                    "manifest_sha256": manifest_sha256,
                    "runtime_s": round(time.monotonic() - started, 6),
                    "final_submission_authority": False,
                },
                candidates=(output,),
            )
        except Exception as exc:  # noqa: BLE001
            return SkillResult.failure(
                before_wns,
                str(exc),
                failed_dcp,
                details={
                    "proof_version": PROOF_VERSION,
                    "runtime_s": round(time.monotonic() - started, 6),
                },
            )
