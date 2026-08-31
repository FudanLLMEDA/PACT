"""CLI and machine-readable wrapper for the typed lowering selector."""

from __future__ import annotations

import argparse
import re
from typing import Any

from ..registered_arithmetic_ir import select_lowering
from .common import envelope, object_digest, plan_input


_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def reject_root_divider_classification(classification: Any) -> dict[str, Any]:
    """Preserve a structural root/divider classifier's abstention authority."""
    if not isinstance(classification, dict):
        from .common import fail
        fail("LOWERING_INVALID_CLASSIFICATION", "classification must be an object")
    required = {
        "classification", "source_artifact_sha256", "family_fingerprint",
        "structural_evidence", "proof_status",
    }
    if set(classification) != required:
        from .common import fail
        fail("LOWERING_CLASSIFICATION_SCHEMA", "classification fields are incomplete")
    if classification["classification"] != "root_divider_recurrence":
        from .common import fail
        fail("LOWERING_CLASSIFICATION_KIND", "only root/divider abstention is accepted")
    source_sha = classification["source_artifact_sha256"]
    fingerprint = classification["family_fingerprint"]
    if (
        not isinstance(source_sha, str)
        or _SHA256_RE.fullmatch(source_sha) is None
        or not isinstance(fingerprint, str)
        or _SHA256_RE.fullmatch(fingerprint) is None
        or classification["proof_status"] != "structural_classification_only"
    ):
        from .common import fail
        fail("LOWERING_CLASSIFICATION_AUTHORITY", "classification identity or authority is invalid")
    evidence = classification["structural_evidence"]
    if not isinstance(evidence, dict) or any(
        isinstance(evidence.get(key), bool)
        or not isinstance(evidence.get(key), int)
        or evidence[key] < 1
        for key in (
            "root_scoped_cell_count", "divider_scoped_cell_count",
            "sequential_cell_count", "arithmetic_cell_count",
        )
    ):
        from .common import fail
        fail("LOWERING_INSUFFICIENT_ROOT_DIVIDER_EVIDENCE", "structural recurrence evidence is incomplete")
    if object_digest(evidence) != fingerprint:
        from .common import fail
        fail("LOWERING_CLASSIFICATION_DIGEST_MISMATCH", "classification fingerprint does not bind its evidence")
    reason = (
        "root/divider recurrence is structurally classified from the current "
        "netlist and is unsupported by FIR recurrence-transport and product-sum "
        "data-cone lowerings"
    )
    return envelope(
        "typed_lowering_selector",
        source_artifact_sha256=source_sha,
        candidate_family_id=f"root-divider:{fingerprint[:20]}",
        candidate_hash=fingerprint,
        status="rejected",
        payload={
            "decision": {
                "status": "rejected",
                "backend_id": None,
                "reason": reason,
                "plan_kind": "unsupported_recurrence_root_divider",
            },
            "fallback_plan_kind": None,
        },
        failure_reasons=[{
            "reason_code": "LOWERING_ROOT_DIVIDER_RECURRENCE_REJECTED",
            "message": reason,
            "path": "classification.classification",
        }],
    )


def select_plan_lowering(plan_value: Any) -> dict[str, Any]:
    plan = plan_input(plan_value)
    decision = select_lowering(plan)
    return envelope(
        "typed_lowering_selector",
        source_artifact_sha256=plan.source_artifact_sha256,
        candidate_family_id=plan.candidate_family_id,
        candidate_hash=plan.plan_digest(),
        status="success" if decision.status.value == "selected" else "rejected",
        payload={
            "decision": {
                "status": decision.status.value,
                "backend_id": decision.backend_id,
                "reason": decision.reason,
                "plan_kind": decision.plan_kind.value,
            },
            "fallback_plan_kind": None,
        },
        failure_reasons=([] if decision.status.value == "selected" else [{
            "reason_code": (
                "LOWERING_INTEGRATED_DCP_BACKEND_UNAVAILABLE"
                if decision.reason.startswith("integrated_dcp_backend_unavailable:")
                else "LOWERING_TYPED_REJECTION"
            ),
            "message": decision.reason,
            "path": "plan.plan_kind",
        }]),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan")
    args = parser.parse_args()
    import json
    try:
        result = select_plan_lowering(args.plan)
    except Exception as exc:
        from .common import rejection_from_exception
        result = rejection_from_exception("typed_lowering_selector", exc)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
