"""Shared deterministic envelopes and CLI mechanics for standalone tools."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

from ..registered_arithmetic_ir import (
    RegisteredOperatorPlan,
    RegisteredPlanContractError,
)


TOOLS_SCHEMA_VERSION = "regarith-tools-v1"
FACTS_SCHEMA_VERSION = "regarith-netlist-facts-v1"
PROOF_TEMPLATE_PRODUCT_SUM = "REGARITH_PRODUCT_SUM_V1"
PROOF_TEMPLATE_PARTITIONED_PRODUCT_SUM = "REGARITH_PARTITIONED_PRODUCT_SUM_V1"


class RegarithToolError(ValueError):
    def __init__(self, reason_code: str, message: str, *, path: str = "input"):
        self.reason_code = reason_code
        self.message = message
        self.path = path
        super().__init__(f"{reason_code} at {path}: {message}")


def fail(reason_code: str, message: str, path: str = "input") -> None:
    raise RegarithToolError(reason_code, message, path=path)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    )


def object_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("ascii")).hexdigest()


def file_sha256(path: Path | str) -> str:
    candidate = Path(path)
    if not candidate.is_file() or candidate.is_symlink():
        fail("ARTIFACT_UNAVAILABLE", "artifact must be a regular non-symlink file", str(candidate))
    with candidate.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path: Path | str) -> Any:
    candidate = Path(path)
    if not candidate.is_file() or candidate.is_symlink():
        fail("JSON_INPUT_UNAVAILABLE", "JSON input must be a regular file", str(candidate))
    try:
        return json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail("INVALID_JSON_INPUT", str(exc), str(candidate))


def plan_input(value: RegisteredOperatorPlan | Mapping[str, Any] | Path | str) -> RegisteredOperatorPlan:
    if isinstance(value, RegisteredOperatorPlan):
        return value.validate()
    if isinstance(value, (Path, str)):
        value = read_json(value)
    if isinstance(value, Mapping) and value.get("plan") is not None:
        value = value["plan"]
    if not isinstance(value, Mapping):
        fail("INVALID_PLAN_INPUT", "plan input must be an object")
    return RegisteredOperatorPlan.from_dict(value)


def envelope(
    tool: str,
    *,
    source_artifact_sha256: str,
    candidate_family_id: str | None,
    candidate_hash: str | None,
    status: str,
    payload: Mapping[str, Any] | None = None,
    failure_reasons: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    result = {
        "schema_version": TOOLS_SCHEMA_VERSION,
        "tool": tool,
        "status": status,
        "source_artifact_sha256": source_artifact_sha256,
        "candidate_family_id": candidate_family_id,
        "candidate_hash": candidate_hash,
        "failure_reasons": list(failure_reasons or []),
        "mutation_allowed": False,
        "llm_decision_authority": False,
    }
    if payload:
        result.update(payload)
    result["result_digest"] = object_digest(result)
    return result


def rejection_from_exception(tool: str, exc: Exception, *, source_sha: str = "") -> dict[str, Any]:
    if isinstance(exc, (RegarithToolError, RegisteredPlanContractError)):
        reason = {
            "reason_code": exc.reason_code,
            "message": exc.message,
            "path": exc.path,
        }
    else:
        reason = {
            "reason_code": "TOOL_EXECUTION_FAILED",
            "message": f"{type(exc).__name__}: {exc}",
            "path": "tool",
        }
    return envelope(
        tool,
        source_artifact_sha256=source_sha,
        candidate_family_id=None,
        candidate_hash=None,
        status="rejected",
        failure_reasons=[reason],
    )


def cli_main(run: Callable[[], Mapping[str, Any]]) -> None:
    try:
        result = run()
        print(json.dumps(result, indent=2, sort_keys=True))
    except Exception as exc:  # CLI preserves typed failures as JSON.
        print(json.dumps(rejection_from_exception("cli", exc), indent=2, sort_keys=True))
        sys.exit(2)
