"""Integrity and runtime validation for optional predictor packages."""

from __future__ import annotations

import hashlib
import json
import string
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Mapping, Optional

from packaging.specifiers import SpecifierSet
from packaging.version import Version


RUNTIME_MANIFEST = "runtime_package_manifest.json"
REQUIRED_ARTIFACTS = {
    "qor_ranker": {"best_optimized_top5_ranker.joblib"},
    "runtime_dispatcher": {"best_unified_time_predictor.joblib"},
}
REQUIRED_HELPERS = {
    "qor_ranker": {"train_optimized_delta_models.py"},
    "runtime_dispatcher": {"train_unified_time_models.py"},
}


class PredictorPackageError(RuntimeError):
    """Raised when a predictor package is corrupt or ABI-incompatible."""


@dataclass(frozen=True)
class PredictorValidation:
    model_root: Path
    package_kind: str
    feature_schema_version: str
    dataset_id: str
    training_commit: str
    artifact_hashes: dict[str, str]

    def compact_provenance(self) -> dict:
        return {
            "package_kind": self.package_kind,
            "feature_schema_version": self.feature_schema_version,
            "dataset_id": self.dataset_id,
            "training_commit": self.training_commit,
            "artifact_hashes": dict(self.artifact_hashes),
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _installed_version(distribution: str) -> Optional[str]:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


def validate_model_package(
    model_root: Path,
    *,
    expected_kind: Optional[str] = None,
    installed_versions: Optional[Mapping[str, str]] = None,
) -> PredictorValidation:
    root = Path(model_root).expanduser().resolve()
    manifest_path = root / RUNTIME_MANIFEST
    if not manifest_path.is_file():
        raise PredictorPackageError(f"predictor manifest missing: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PredictorPackageError(f"invalid predictor manifest: {exc}") from exc
    if manifest.get("schema_version") != 1:
        raise PredictorPackageError(
            f"unsupported predictor schema_version={manifest.get('schema_version')!r}"
        )
    package_kind = str(manifest.get("package_kind") or "")
    if expected_kind and package_kind != expected_kind:
        raise PredictorPackageError(
            f"unexpected predictor package kind {package_kind!r}; "
            f"expected {expected_kind!r}"
        )
    for field in ("feature_schema_version", "dataset_id", "training_commit"):
        if not str(manifest.get(field) or "").strip():
            raise PredictorPackageError(f"predictor manifest missing {field}")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise PredictorPackageError("predictor manifest contains no artifacts")
    required_artifacts = REQUIRED_ARTIFACTS.get(package_kind, set())
    missing_artifacts = sorted(required_artifacts - set(artifacts))
    if missing_artifacts:
        raise PredictorPackageError(
            "predictor manifest omits required artifacts: "
            + ", ".join(missing_artifacts)
        )
    missing_helpers = sorted(
        name
        for name in REQUIRED_HELPERS.get(package_kind, set())
        if not (root / name).is_file()
    )
    if missing_helpers:
        raise PredictorPackageError(
            "predictor package omits required helpers: "
            + ", ".join(missing_helpers)
        )
    verified: dict[str, str] = {}
    for name, record in artifacts.items():
        if not isinstance(record, Mapping):
            raise PredictorPackageError(f"invalid predictor artifact record for {name!r}")
        artifact = (root / str(name)).resolve()
        try:
            artifact.relative_to(root)
        except ValueError as exc:
            raise PredictorPackageError(
                f"predictor artifact escapes package root: {name!r}"
            ) from exc
        expected = str(record.get("sha256") or "").lower()
        if len(expected) != 64 or any(char not in string.hexdigits for char in expected):
            raise PredictorPackageError(f"invalid SHA-256 for {name}")
        if not artifact.is_file():
            raise PredictorPackageError(f"predictor artifact missing: {artifact}")
        actual = _sha256(artifact)
        if actual != expected:
            raise PredictorPackageError(
                f"predictor checksum mismatch for {name}: expected {expected}, got {actual}"
            )
        verified[str(name)] = actual

    versions = dict(installed_versions or {})
    versions.setdefault(
        "python",
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    )
    distributions = {
        "joblib": "joblib",
        "numpy": "numpy",
        "scikit-learn": "scikit-learn",
        "xgboost": "xgboost",
    }
    for key, distribution in distributions.items():
        if key not in versions:
            installed = _installed_version(distribution)
            if installed is not None:
                versions[key] = installed
    constraints = manifest.get("validated_runtime") or {}
    if not isinstance(constraints, Mapping):
        raise PredictorPackageError("predictor validated_runtime must be a mapping")
    for package_name, constraint in constraints.items():
        installed = versions.get(str(package_name))
        if installed is None:
            raise PredictorPackageError(
                f"predictor runtime dependency missing: {package_name}{constraint}"
            )
        if Version(installed) not in SpecifierSet(str(constraint)):
            raise PredictorPackageError(
                f"predictor ABI mismatch: {package_name} {installed} "
                f"does not satisfy {constraint}"
            )
    return PredictorValidation(
        model_root=root,
        package_kind=package_kind,
        feature_schema_version=str(manifest["feature_schema_version"]),
        dataset_id=str(manifest["dataset_id"]),
        training_commit=str(manifest["training_commit"]),
        artifact_hashes=verified,
    )
