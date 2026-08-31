"""Current-run private checkpoints for proved consumer rewrites.

The checkpoint retains object-named wiring only inside the trusted framework.
Luna receives a live, de-identified contract and remains the decision owner.
No next action or policy preference is stored here.  A checkpoint is valid
only for one no-replay ``run_id`` and one exact current-seed artifact; legacy,
cross-run, and ancestor-seed entries fail closed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from .artifacts import atomic_write_json
from .consumer_rewrite import validate_consumer_rewrite_bundle


CONSUMER_CHECKPOINT_VERSION = 2
CONSUMER_CHECKPOINT_SCHEMA = "current-run-consumer-rewrite-checkpoint-v2"
CONSUMER_CHECKPOINT_PRODUCER = "FDAgents.consumer_checkpoint"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_RUN_ID = re.compile(r"nr-[0-9a-f]{16,64}")


def _stable_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")).hexdigest()


def _checked_sha256(value: Any, field: str) -> str:
    digest = str(value or "").lower()
    if _SHA256.fullmatch(digest) is None:
        raise ValueError(f"consumer checkpoint requires an exact {field} SHA256")
    return digest


def _checked_run_id(value: Any) -> str:
    run_id = str(value or "")
    if _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("consumer checkpoint requires a no-replay run_id")
    return run_id


def _sha256_file(path: Path) -> str:
    candidate = Path(path).expanduser().resolve()
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError("consumer checkpoint requires a regular current-seed DCP")
    digest = hashlib.sha256()
    with candidate.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cache_root(source_dcp: Path) -> Path:
    configured = os.environ.get("FPL26_CHECKPOINT_CACHE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve() / "consumer_rewrite"
    resolved = Path(source_dcp).expanduser().resolve()
    repository_root = next((
        parent for parent in (resolved.parent, *resolved.parents)
        if (parent / "FDAgents").is_dir()
    ), resolved.parent)
    return repository_root / ".fdagents_checkpoints" / "consumer_rewrite"


def consumer_checkpoint_path(
    source_dcp: Path,
    *,
    run_id: str,
    source_sha256: str,
    candidate_id: str,
) -> Path:
    checked_run_id = _checked_run_id(run_id)
    digest = _checked_sha256(source_sha256, "source")
    candidate_token = hashlib.sha256(
        str(candidate_id).encode("utf-8")
    ).hexdigest()[:20]
    return (
        _cache_root(source_dcp)
        / checked_run_id
        / digest
        / candidate_token
        / "consumer_checkpoint.json"
    )


def public_contract_from_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    """Derive only name-free operator facts from one validated bundle."""
    validated = validate_consumer_rewrite_bundle(bundle)
    plans = list(validated["plans"])
    input_widths = [int(plan["input_width"]) for plan in plans]
    output_widths = [int(plan["output_width"]) for plan in plans]
    pipeline_depths = []
    for plan in plans:
        if plan.get("pipeline_depth") is not None:
            pipeline_depths.append(int(plan["pipeline_depth"]))
        elif (
            plan.get("sqrt_pipeline_depth") is not None
            and plan.get("division_pipeline_depth") is not None
        ):
            pipeline_depths.append(
                int(plan["sqrt_pipeline_depth"])
                + int(plan["division_pipeline_depth"])
            )
    operators = sorted({str(plan["operator"]) for plan in plans})
    controls = [dict(plan["control_protocol"]) for plan in plans]
    return {
        "consumer_input_word_widths": input_widths,
        "observable_output_word_widths": output_widths,
        "pipeline_depth": (
            max(pipeline_depths) if pipeline_depths else None
        ),
        "pipeline_depth_basis": "current_run_proved_registered_schedule",
        "control_protocol": controls[0] if len(controls) == 1 else {
            "operator_count": len(controls),
            "all_equal": all(item == controls[0] for item in controls),
        },
        "semantic_candidates": [{
            "operator_class": operator,
            "state": "current_run_proved_checkpoint_available",
        } for operator in operators],
        "missing_proof": [
            "fresh_routed_fmax_payoff",
            "central_whole_dcp_equivalence",
        ],
        "predicted_surviving_wall": "unknown_until_post_route_reprofile",
    }


def publish_consumer_rewrite_checkpoint(
    source_dcp: Path,
    *,
    run_id: str,
    run_source_sha256: str,
    source_sha256: str,
    candidate_id: str,
    bundle: dict[str, Any],
) -> Path:
    checked_run_id = _checked_run_id(run_id)
    run_source_digest = _checked_sha256(run_source_sha256, "run source")
    digest = _checked_sha256(source_sha256, "source")
    if _sha256_file(source_dcp) != digest:
        raise ValueError("consumer checkpoint current-seed file SHA256 changed")
    candidate = str(candidate_id or "")
    if not candidate.startswith("sequential:"):
        raise ValueError("consumer checkpoint requires a live sequential candidate")
    validated = validate_consumer_rewrite_bundle(bundle)
    public_contract = public_contract_from_bundle(validated)
    dependencies_sha256 = _stable_digest({
        "direct_input_sha256": digest,
        "bundle": validated,
        "public_contract": public_contract,
    })
    contract_sha256 = _stable_digest({
        "schema_version": CONSUMER_CHECKPOINT_SCHEMA,
        "producer": CONSUMER_CHECKPOINT_PRODUCER,
        "run_id": checked_run_id,
        "run_source_sha256": run_source_digest,
        "direct_input_sha256": digest,
        "candidate_id": candidate,
        "dependencies_sha256": dependencies_sha256,
    })
    payload = {
        "version": CONSUMER_CHECKPOINT_VERSION,
        "kind": "current_run_consumer_rewrite_checkpoint",
        "schema_version": CONSUMER_CHECKPOINT_SCHEMA,
        "producer": CONSUMER_CHECKPOINT_PRODUCER,
        "run_id": checked_run_id,
        "run_source_sha256": run_source_digest,
        "direct_input_sha256": digest,
        "candidate_id": candidate,
        "dependencies_sha256": dependencies_sha256,
        "contract_sha256": contract_sha256,
        "public_contract": public_contract,
        "bundle": validated,
        "policy": {
            "contains_next_action": False,
            "luna_remains_decision_owner": True,
            "all_dependencies_current_run": True,
            "ancestor_seed_reuse": False,
        },
    }
    path = consumer_checkpoint_path(
        source_dcp,
        run_id=checked_run_id,
        source_sha256=digest,
        candidate_id=candidate,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, payload)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def load_consumer_rewrite_checkpoint(
    source_dcp: Path,
    *,
    run_id: str,
    run_source_sha256: str,
    source_sha256: str,
    candidate_id: str,
) -> dict[str, Any]:
    checked_run_id = _checked_run_id(run_id)
    run_source_digest = _checked_sha256(run_source_sha256, "run source")
    digest = _checked_sha256(source_sha256, "source")
    if _sha256_file(source_dcp) != digest:
        raise ValueError("consumer checkpoint current-seed file SHA256 changed")
    candidate = str(candidate_id or "")
    path = consumer_checkpoint_path(
        source_dcp,
        run_id=checked_run_id,
        source_sha256=digest,
        candidate_id=candidate,
    )
    if path.is_symlink() or not path.is_file():
        raise ValueError("source-bound consumer checkpoint is unavailable")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        payload.get("version") != CONSUMER_CHECKPOINT_VERSION
        or payload.get("kind") != "current_run_consumer_rewrite_checkpoint"
        or payload.get("schema_version") != CONSUMER_CHECKPOINT_SCHEMA
        or payload.get("producer") != CONSUMER_CHECKPOINT_PRODUCER
        or payload.get("run_id") != checked_run_id
        or payload.get("run_source_sha256") != run_source_digest
        or payload.get("direct_input_sha256") != digest
        or payload.get("candidate_id") != candidate
        or payload.get("policy", {}).get("all_dependencies_current_run") is not True
        or payload.get("policy", {}).get("ancestor_seed_reuse") is not False
    ):
        raise ValueError("current-run consumer checkpoint provenance changed")
    validated = validate_consumer_rewrite_bundle(payload.get("bundle") or {})
    public_contract = public_contract_from_bundle(validated)
    dependencies_sha256 = _stable_digest({
        "direct_input_sha256": digest,
        "bundle": validated,
        "public_contract": public_contract,
    })
    if payload.get("dependencies_sha256") != dependencies_sha256:
        raise ValueError("current-run consumer checkpoint dependencies changed")
    expected = _stable_digest({
        "schema_version": CONSUMER_CHECKPOINT_SCHEMA,
        "producer": CONSUMER_CHECKPOINT_PRODUCER,
        "run_id": checked_run_id,
        "run_source_sha256": run_source_digest,
        "direct_input_sha256": digest,
        "candidate_id": candidate,
        "dependencies_sha256": dependencies_sha256,
    })
    if payload.get("contract_sha256") != expected:
        raise ValueError("current-run consumer checkpoint contract changed")
    return {
        "path": str(path),
        "run_id": checked_run_id,
        "run_source_sha256": run_source_digest,
        "direct_input_sha256": digest,
        "candidate_id": candidate,
        "dependencies_sha256": dependencies_sha256,
        "contract_sha256": expected,
        "bundle": validated,
        "public_contract": public_contract,
    }


def load_unique_consumer_rewrite_checkpoint_for_source(
    source_dcp: Path,
    *,
    run_id: str,
    run_source_sha256: str,
    source_sha256: str,
) -> dict[str, Any]:
    """Load the sole proved contract attached to this run's exact seed.

    No ancestor or cross-run search occurs.  Multiple contracts are
    deliberately ambiguous because choosing between them is an Agent decision,
    not cache policy.
    """
    checked_run_id = _checked_run_id(run_id)
    run_source_digest = _checked_sha256(run_source_sha256, "run source")
    digest = _checked_sha256(source_sha256, "source")
    if _sha256_file(source_dcp) != digest:
        raise ValueError("consumer checkpoint current-seed file SHA256 changed")
    source_dir = _cache_root(source_dcp) / checked_run_id / digest
    if source_dir.is_symlink() or not source_dir.is_dir():
        raise ValueError("source-bound consumer checkpoint is unavailable")
    loaded = []
    for path in sorted(source_dir.glob("*/consumer_checkpoint.json")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            candidate_id = str(payload.get("candidate_id") or "")
            loaded.append(load_consumer_rewrite_checkpoint(
                source_dcp,
                run_id=checked_run_id,
                run_source_sha256=run_source_digest,
                source_sha256=digest,
                candidate_id=candidate_id,
            ))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            continue
    if not loaded:
        raise ValueError("source-bound consumer checkpoint is unavailable")
    if len(loaded) != 1:
        raise ValueError("source-bound consumer checkpoint is ambiguous")
    return loaded[0]


__all__ = [
    "CONSUMER_CHECKPOINT_PRODUCER",
    "CONSUMER_CHECKPOINT_SCHEMA",
    "CONSUMER_CHECKPOINT_VERSION",
    "consumer_checkpoint_path",
    "load_consumer_rewrite_checkpoint",
    "load_unique_consumer_rewrite_checkpoint_for_source",
    "public_contract_from_bundle",
    "publish_consumer_rewrite_checkpoint",
]
