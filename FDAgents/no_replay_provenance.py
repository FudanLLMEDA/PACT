"""Run-level provenance gate for no-replay action experiments.

This module is deliberately independent of Agent, LLM, Vivado, and action
backends.  A valid source SHA is necessary but not sufficient: an artifact is
admitted only when it is the immutable source for this run or a descendant
created under the same run workspace and linked to an admitted parent.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "no-replay-provenance-v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_RUN_ID = re.compile(r"nr-[0-9a-f]{16,64}")
_FRESH_ORIGINS = frozenset({"current_source", "same_run_descendant"})


class NoReplayProvenanceError(ValueError):
    """A dependency is not admissible for the current no-replay run."""

    def __init__(self, reason_code: str, message: str):
        self.reason_code = reason_code
        super().__init__(f"{reason_code}: {message}")


def sha256_file(path: Path) -> str:
    """Hash one regular file without relying on its filename."""
    candidate = Path(path).resolve()
    if not candidate.is_file():
        raise NoReplayProvenanceError(
            "PROVENANCE_INPUT_FILE_REQUIRED", str(candidate)
        )
    digest = hashlib.sha256()
    with candidate.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_development_allowlist(
    manifest_path: Path,
    source_artifact_sha256: str,
) -> dict[str, Any]:
    """Admit one source only from a development-only, hash-bound allowlist."""
    path = Path(manifest_path).resolve()
    if path.is_symlink() or not path.is_file():
        raise NoReplayProvenanceError(
            "PROVENANCE_DEVELOPMENT_ALLOWLIST_REQUIRED", str(path)
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NoReplayProvenanceError(
            "PROVENANCE_DEVELOPMENT_ALLOWLIST_INVALID", str(path)
        ) from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != "no-replay-dcp-development-allowlist-v1"
        or value.get("role_scope") != "development"
        or not isinstance(value.get("records"), list)
    ):
        raise NoReplayProvenanceError(
            "PROVENANCE_DEVELOPMENT_ALLOWLIST_SCHEMA", str(path)
        )
    source_sha = _check_sha(source_artifact_sha256, field="source_artifact_sha256")
    matches = [
        record for record in value["records"]
        if isinstance(record, Mapping)
        and record.get("source_dcp_sha256") == source_sha
    ]
    if len(matches) != 1:
        raise NoReplayProvenanceError(
            "PROVENANCE_SOURCE_NOT_IN_DEVELOPMENT_ALLOWLIST", source_sha
        )
    record = matches[0]
    if (
        record.get("effective_role") != "development"
        or record.get("development_allowed") is not True
        or record.get("role_conflict") is True
        or not record.get("lineage_group")
    ):
        raise NoReplayProvenanceError(
            "PROVENANCE_SOURCE_ROLE_NOT_DEVELOPMENT", source_sha
        )
    return dict(record)


def validate_engineering_regression_manifest(
    manifest_path: Path,
    source_artifact_sha256: str,
) -> dict[str, Any]:
    """Admit an explicitly authorized source for engineering regression.

    This scope exists to find runtime defects across the official regression
    corpus.  It does not change a record's training role and cannot be cited as
    unbiased development/holdout generalization evidence.
    """
    path = Path(manifest_path).resolve()
    if path.is_symlink() or not path.is_file():
        raise NoReplayProvenanceError(
            "PROVENANCE_ENGINEERING_REGRESSION_MANIFEST_REQUIRED", str(path)
        )
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NoReplayProvenanceError(
            "PROVENANCE_ENGINEERING_REGRESSION_MANIFEST_INVALID", str(path)
        ) from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version")
        != "no-replay-dcp-engineering-regression-v1"
        or value.get("role_scope") != "engineering_regression"
        or value.get("no_replay") is not True
        or value.get("not_generalization_evidence") is not True
        or not isinstance(value.get("records"), list)
    ):
        raise NoReplayProvenanceError(
            "PROVENANCE_ENGINEERING_REGRESSION_MANIFEST_SCHEMA", str(path)
        )
    source_sha = _check_sha(source_artifact_sha256, field="source_artifact_sha256")
    matches = [
        dict(record)
        for record in value["records"]
        if isinstance(record, Mapping)
        and record.get("source_dcp_sha256") == source_sha
    ]
    if not matches:
        raise NoReplayProvenanceError(
            "PROVENANCE_SOURCE_NOT_IN_ENGINEERING_REGRESSION_MANIFEST", source_sha
        )
    for record in matches:
        if (
            record.get("execution_scope") != "engineering_regression"
            or record.get("no_replay") is not True
            or not isinstance(record.get("regression_slot"), str)
            or not record["regression_slot"]
        ):
            raise NoReplayProvenanceError(
                "PROVENANCE_SOURCE_ROLE_NOT_ENGINEERING_REGRESSION", source_sha
            )
    return {
        "source_dcp_sha256": source_sha,
        "execution_scope": "engineering_regression",
        "not_generalization_evidence": True,
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "matching_slots": [record["regression_slot"] for record in matches],
        "matching_records": matches,
    }


def _check_sha(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise NoReplayProvenanceError(
            "PROVENANCE_SHA256_INVALID", f"{field} must be lowercase SHA-256"
        )
    return value


def _check_run_id(value: Any) -> str:
    if not isinstance(value, str) or _RUN_ID.fullmatch(value) is None:
        raise NoReplayProvenanceError(
            "PROVENANCE_RUN_ID_INVALID", "run_id is not a no-replay run id"
        )
    return value


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class NoReplayRun:
    """Immutable identity and filesystem boundary for one cold run."""

    run_id: str
    source_artifact_sha256: str
    source_path: Path
    run_root: Path

    def __post_init__(self) -> None:
        _check_run_id(self.run_id)
        _check_sha(self.source_artifact_sha256, field="source_artifact_sha256")
        raw_source = Path(self.source_path).expanduser()
        raw_root = Path(self.run_root).expanduser()
        if raw_source.is_symlink() or raw_root.is_symlink():
            raise NoReplayProvenanceError(
                "PROVENANCE_SYMLINK_BOUNDARY", "source and run root must be regular paths"
            )
        source = raw_source.resolve()
        root = raw_root.resolve()
        if not source.is_file():
            raise NoReplayProvenanceError(
                "PROVENANCE_INPUT_FILE_REQUIRED", str(source)
            )
        if not root.is_dir():
            raise NoReplayProvenanceError(
                "PROVENANCE_RUN_ROOT_REQUIRED", str(root)
            )
        if sha256_file(source) != self.source_artifact_sha256:
            raise NoReplayProvenanceError(
                "PROVENANCE_SOURCE_SHA_MISMATCH", str(source)
            )
        object.__setattr__(self, "source_path", source)
        object.__setattr__(self, "run_root", root)


@dataclass(frozen=True, slots=True)
class ArtifactReceipt:
    """Hash-bound admission receipt for one current-run artifact."""

    schema_version: str
    run_id: str
    source_artifact_sha256: str
    artifact_sha256: str
    direct_input_artifact_sha256: str
    parent_artifact_sha256: str | None
    path: str
    origin: str
    created_in_run: bool
    imported: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "source_artifact_sha256": self.source_artifact_sha256,
            "artifact_sha256": self.artifact_sha256,
            "direct_input_artifact_sha256": self.direct_input_artifact_sha256,
            "parent_artifact_sha256": self.parent_artifact_sha256,
            "path": self.path,
            "origin": self.origin,
            "created_in_run": self.created_in_run,
            "imported": self.imported,
        }


class NoReplayLedger:
    """Admit only the source and freshly created same-run descendants."""

    def __init__(self, run: NoReplayRun):
        self.run = run
        self._receipts_by_path: dict[str, ArtifactReceipt] = {}
        self._receipts_by_sha: dict[str, list[ArtifactReceipt]] = {}

    @property
    def receipts(self) -> Mapping[str, ArtifactReceipt]:
        return dict(self._receipts_by_path)

    def _record(self, receipt: ArtifactReceipt) -> ArtifactReceipt:
        existing = self._receipts_by_path.get(receipt.path)
        if existing is not None:
            if existing.as_dict() != receipt.as_dict():
                raise NoReplayProvenanceError(
                    "PROVENANCE_PATH_ALREADY_ADMITTED", receipt.path
                )
            return existing
        self._receipts_by_path[receipt.path] = receipt
        self._receipts_by_sha.setdefault(receipt.artifact_sha256, []).append(receipt)
        return receipt

    def _check_path(self, path: Path, *, source: bool) -> Path:
        resolved = Path(path).resolve()
        if source:
            if resolved != self.run.source_path:
                raise NoReplayProvenanceError(
                    "PROVENANCE_SOURCE_PATH_MISMATCH", str(resolved)
                )
        elif not _inside(resolved, self.run.run_root):
            raise NoReplayProvenanceError(
                "PROVENANCE_EXTERNAL_ARTIFACT", str(resolved)
            )
        return resolved

    def admit_source(self) -> ArtifactReceipt:
        """Admit the immutable source file exactly once."""
        source = self._check_path(self.run.source_path, source=True)
        receipt = ArtifactReceipt(
            schema_version=SCHEMA_VERSION,
            run_id=self.run.run_id,
            source_artifact_sha256=self.run.source_artifact_sha256,
            artifact_sha256=self.run.source_artifact_sha256,
            direct_input_artifact_sha256=self.run.source_artifact_sha256,
            parent_artifact_sha256=None,
            path=str(source),
            origin="current_source",
            created_in_run=True,
            imported=False,
        )
        return self._record(receipt)

    def admit_descendant(
        self,
        *,
        artifact_path: Path,
        artifact_sha256: str,
        parent_artifact_sha256: str,
    ) -> ArtifactReceipt:
        """Admit one child only when its parent is current-run material."""
        artifact_sha256 = _check_sha(artifact_sha256, field="artifact_sha256")
        parent_artifact_sha256 = _check_sha(
            parent_artifact_sha256, field="parent_artifact_sha256"
        )
        if not self._receipts_by_sha.get(parent_artifact_sha256):
            raise NoReplayProvenanceError(
                "PROVENANCE_PARENT_NOT_ADMITTED", parent_artifact_sha256
            )
        path = self._check_path(Path(artifact_path), source=False)
        if not path.is_file():
            raise NoReplayProvenanceError(
                "PROVENANCE_ARTIFACT_FILE_REQUIRED", str(path)
            )
        if sha256_file(path) != artifact_sha256:
            raise NoReplayProvenanceError(
                "PROVENANCE_ARTIFACT_SHA_MISMATCH", str(path)
            )
        receipt = ArtifactReceipt(
            schema_version=SCHEMA_VERSION,
            run_id=self.run.run_id,
            source_artifact_sha256=self.run.source_artifact_sha256,
            artifact_sha256=artifact_sha256,
            direct_input_artifact_sha256=parent_artifact_sha256,
            parent_artifact_sha256=parent_artifact_sha256,
            path=str(path),
            origin="same_run_descendant",
            created_in_run=True,
            imported=False,
        )
        return self._record(receipt)

    def require(self, receipt: Mapping[str, Any] | ArtifactReceipt) -> ArtifactReceipt:
        """Validate a receipt and its full parent chain against this ledger."""
        value = receipt.as_dict() if isinstance(receipt, ArtifactReceipt) else dict(receipt)
        required = {
            "schema_version", "run_id", "source_artifact_sha256",
            "artifact_sha256", "direct_input_artifact_sha256", "path",
            "origin", "created_in_run", "imported",
        }
        if set(value) != required | {"parent_artifact_sha256"}:
            raise NoReplayProvenanceError(
                "PROVENANCE_RECEIPT_SCHEMA_INVALID", "unexpected or missing fields"
            )
        if value.get("schema_version") != SCHEMA_VERSION:
            raise NoReplayProvenanceError(
                "PROVENANCE_SCHEMA_UNSUPPORTED", str(value.get("schema_version"))
            )
        if value.get("run_id") != self.run.run_id:
            raise NoReplayProvenanceError(
                "PROVENANCE_CROSS_RUN", str(value.get("run_id"))
            )
        if value.get("source_artifact_sha256") != self.run.source_artifact_sha256:
            raise NoReplayProvenanceError(
                "PROVENANCE_SOURCE_MISMATCH", str(value.get("source_artifact_sha256"))
            )
        artifact_sha = _check_sha(value.get("artifact_sha256"), field="artifact_sha256")
        _check_sha(value.get("direct_input_artifact_sha256"), field="direct_input_artifact_sha256")
        parent = value.get("parent_artifact_sha256")
        if parent is not None:
            _check_sha(parent, field="parent_artifact_sha256")
        if value.get("origin") not in _FRESH_ORIGINS:
            raise NoReplayProvenanceError(
                "PROVENANCE_HISTORICAL_ORIGIN", str(value.get("origin"))
            )
        if value.get("created_in_run") is not True or value.get("imported") is not False:
            raise NoReplayProvenanceError(
                "PROVENANCE_IMPORTED_ARTIFACT", artifact_sha
            )
        admitted = self._receipts_by_path.get(str(Path(str(value["path"])).resolve()))
        if admitted is None or admitted.as_dict() != value:
            raise NoReplayProvenanceError(
                "PROVENANCE_RECEIPT_NOT_ADMITTED", artifact_sha
            )
        if parent is not None:
            if not any(item.artifact_sha256 == parent for item in self._receipts_by_sha.get(parent, ())):
                raise NoReplayProvenanceError(
                    "PROVENANCE_PARENT_NOT_ADMITTED", parent
                )
        return admitted


def new_run(
    *,
    source_artifact_sha256: str,
    source_path: Path,
    run_root: Path,
    run_id: str | None = None,
) -> NoReplayRun:
    """Create a fresh run identity; callers may pass a deterministic test id."""
    selected = run_id or f"nr-{uuid.uuid4().hex}"
    return NoReplayRun(
        run_id=selected,
        source_artifact_sha256=source_artifact_sha256,
        source_path=source_path,
        run_root=run_root,
    )
