"""Typed runtime policy for protocol-declared cold replay isolation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


AUDIT_SCHEMA = "fdagents-cold-isolation-audit-v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")


class IsolationPolicyError(RuntimeError):
    """A cold-isolation assertion is absent, malformed, or inconsistent."""

    def __init__(self, code: str, detail: str = ""):
        self.code = str(code)
        self.detail = str(detail)
        super().__init__(f"{self.code}: {self.detail}" if self.detail else self.code)


@dataclass(frozen=True)
class IsolationPolicy:
    cold_isolation: bool = False
    replay_derived_actions_forbidden: bool = False
    audit_path: str = ""
    audit_sha256: str = ""
    protocol_sha256: str = ""
    deployment_manifest_sha256: str = ""

    def as_run_artifact(self) -> dict[str, Any]:
        return {
            "schema_version": AUDIT_SCHEMA,
            "cold_isolation": self.cold_isolation,
            "replay_derived_actions_forbidden": (
                self.replay_derived_actions_forbidden
            ),
            "audit_path": self.audit_path,
            "audit_sha256": self.audit_sha256,
            "protocol_sha256": self.protocol_sha256,
            "deployment_manifest_sha256": self.deployment_manifest_sha256,
        }


def _sha(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise IsolationPolicyError("COLD_ISOLATION_AUDIT_INVALID", field)
    return value


def load_isolation_policy(path: Path | None) -> IsolationPolicy:
    """Load a launcher-produced audit; no audit preserves normal non-cold mode."""
    if path is None:
        return IsolationPolicy()
    audit_path = Path(path).expanduser().resolve()
    if not audit_path.is_file() or audit_path.is_symlink():
        raise IsolationPolicyError("COLD_ISOLATION_AUDIT_UNAVAILABLE")
    raw = audit_path.read_bytes()
    try:
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise IsolationPolicyError("COLD_ISOLATION_AUDIT_INVALID", str(exc)) from exc
    if not isinstance(value, Mapping) or value.get("schema_version") != AUDIT_SCHEMA:
        raise IsolationPolicyError("COLD_ISOLATION_AUDIT_SCHEMA_MISMATCH")
    cold = value.get("cold_isolation") is True
    forbidden = value.get("replay_derived_actions_forbidden") is True
    if cold != forbidden:
        raise IsolationPolicyError("COLD_ISOLATION_AUDIT_POLICY_MISMATCH")
    if cold and (
        value.get("deployment_assertion") != "passed"
        or value.get("forbidden_path_count") != 0
        or value.get("forbidden_paths") != []
    ):
        raise IsolationPolicyError("COLD_ISOLATION_DEPLOYMENT_UNPROVED")
    return IsolationPolicy(
        cold_isolation=cold,
        replay_derived_actions_forbidden=forbidden,
        audit_path=str(audit_path),
        audit_sha256=hashlib.sha256(raw).hexdigest(),
        protocol_sha256=_sha(value.get("protocol_sha256"), "protocol_sha256"),
        deployment_manifest_sha256=_sha(
            value.get("deployment_manifest_sha256"),
            "deployment_manifest_sha256",
        ),
    )
