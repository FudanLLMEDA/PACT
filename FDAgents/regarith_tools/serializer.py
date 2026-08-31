"""Build, canonicalize, serialize, and hash-bind a RegisteredOperatorPlan."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..registered_arithmetic_ir import RegisteredOperatorPlan
from .common import envelope, fail, object_digest, plan_input, read_json
from .data_recovery import recover_data_terms_and_losses
from .schedule_recovery import recover_control_and_schedule


def plan_from_family_facts(family: Mapping[str, Any]) -> RegisteredOperatorPlan:
    if not isinstance(family, Mapping):
        fail("SERIALIZER_INVALID_FAMILY", "family facts must be an object")
    data = recover_data_terms_and_losses(family)
    schedule = recover_control_and_schedule(family)
    words = family.get("words")
    boundaries = []
    registered = []
    output_ids = set(family["output_shell"]["output_word_ids"])
    for item in words:
        boundary = {
            "word_id": item["word_id"],
            "width": item["width"],
            "signed": item["signed"],
            "lsb_index": item["lsb_index"],
            "endpoint_ids": [endpoint["endpoint_id"] for endpoint in item["endpoints"]],
            "cycle_alignment": item["cycle_alignment"],
        }
        boundaries.append(boundary)
        if item["registered"] and item["word_id"] not in output_ids:
            registered.append({"boundary": boundary, "register_semantics": item["register_semantics"]})
    retained = family["retained_side_consumers"]
    plan_dict = {
        "schema_version": "regarith-ir-v1",
        "plan_kind": family["plan_kind"],
        "source_artifact_sha256": family["source_artifact_sha256"],
        "candidate_family_id": family["candidate_family_id"],
        "observable_word_boundaries": boundaries,
        "registered_input_words": registered,
        "data_terms": data["data_terms"],
        "loss_boundaries": data["loss_boundaries"],
        "accumulation_groups": data["accumulation_groups"],
        "control_semantics": schedule["control_semantics"],
        "register_stage_schedule": schedule["register_stage_schedule"],
        "output_shell": family["output_shell"],
        "replaceable_old_cone": family["replaceable_old_cone"],
        "retained_side_consumers": {
            "consumers": retained["consumers"],
            "coverage_status": retained["coverage_status"],
            "proof_obligations": retained["proof_obligations"],
        },
        "expected_wall_coverage": family["expected_wall_coverage"],
    }
    return RegisteredOperatorPlan.from_dict(plan_dict)


def bind_registered_plan(plan_or_family: Any) -> dict[str, Any]:
    if isinstance(plan_or_family, Mapping) and "words" in plan_or_family:
        plan = plan_from_family_facts(plan_or_family)
    else:
        plan = plan_input(plan_or_family)
    canonical = plan.to_canonical_json()
    return envelope(
        "registered_plan_serializer",
        source_artifact_sha256=plan.source_artifact_sha256,
        candidate_family_id=plan.candidate_family_id,
        candidate_hash=plan.plan_digest(),
        status="success",
        payload={
            "ir_schema_version": plan.schema_version,
            "plan_digest": plan.plan_digest(),
            "canonical_json": canonical,
            "plan": json.loads(canonical),
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--family-facts", action="store_true")
    args = parser.parse_args()
    import json as json_module
    try:
        raw = read_json(args.input)
        result = bind_registered_plan(raw)
    except Exception as exc:
        from .common import rejection_from_exception
        result = rejection_from_exception("registered_plan_serializer", exc)
    print(json_module.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
