"""Emitted primitive inventory and replaced-old-cone sweep checker."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from typing import Any

from ..registered_arithmetic_ir import PlanKind, QuantizedProductTerm
from .common import envelope, fail, object_digest, plan_input, read_json


def check_emitted_topology(plan_value: Any, inventory: Mapping[str, Any]) -> dict[str, Any]:
    plan = plan_input(plan_value)
    if not isinstance(inventory, Mapping) or set(inventory) != {
        "source_artifact_sha256", "candidate_artifact_sha256", "cells",
        "output_drivers", "retained_side_consumers",
    }:
        fail("TOPOLOGY_INVENTORY_SCHEMA", "inventory fields are missing or unknown")
    if inventory["source_artifact_sha256"] != plan.source_artifact_sha256:
        fail("TOPOLOGY_SOURCE_MISMATCH", "inventory belongs to another source artifact")
    cells = inventory["cells"]
    if not isinstance(cells, list) or any(
        not isinstance(item, Mapping) or set(item) != {"object_id", "primitive"}
        for item in cells
    ):
        fail("TOPOLOGY_CELL_SCHEMA", "cell inventory is malformed")
    if len({item["object_id"] for item in cells}) != len(cells):
        fail("TOPOLOGY_DUPLICATE_CELL", "cell inventory repeats an object identity")
    product_count = sum(isinstance(item, QuantizedProductTerm) for item in plan.data_terms)
    expected_dsp = product_count * (2 if PlanKind(plan.plan_kind) == PlanKind.WIDE_FIXED_PRODUCT else 1)
    actual_dsp = sum(item["primitive"] == "DSP48E2" for item in cells)
    old = set(plan.replaceable_old_cone.object_ids)
    present = sorted(old & {item["object_id"] for item in cells})
    drivers = inventory["output_drivers"]
    if not isinstance(drivers, list) or any(
        not isinstance(item, Mapping) or set(item) != {"word_id", "driver_id"}
        or not isinstance(item["word_id"], str) or not item["word_id"]
        or not isinstance(item["driver_id"], str) or not item["driver_id"]
        for item in drivers
    ):
        fail("TOPOLOGY_OUTPUT_DRIVER_SCHEMA", "output-driver inventory is malformed")
    stale_drivers = sorted(
        item["driver_id"] for item in drivers if item["driver_id"] in old
    )
    expected_consumers = {item.consumer_id for item in plan.retained_side_consumers.consumers}
    if not isinstance(inventory["retained_side_consumers"], list) or any(
        not isinstance(item, str) or not item
        for item in inventory["retained_side_consumers"]
    ):
        fail("TOPOLOGY_SIDE_CONSUMER_SCHEMA", "retained-side-consumer inventory is malformed")
    actual_consumers = set(inventory["retained_side_consumers"])
    missing_consumers = sorted(expected_consumers - actual_consumers)
    reasons = []
    if actual_dsp != expected_dsp:
        reasons.append({"reason_code": "TOPOLOGY_DSP_INVENTORY_MISMATCH", "message": f"expected {expected_dsp}, observed {actual_dsp}", "path": "inventory.cells"})
    if present:
        reasons.append({"reason_code": "OLD_CONE_NOT_SWEPT", "message": f"old objects remain: {present}", "path": "inventory.cells"})
    if stale_drivers:
        reasons.append({"reason_code": "OLD_CONE_STILL_DRIVES_OUTPUT", "message": f"old drivers remain: {stale_drivers}", "path": "inventory.output_drivers"})
    if missing_consumers:
        reasons.append({"reason_code": "RETAINED_SIDE_CONSUMER_MISSING", "message": f"missing retained consumers: {missing_consumers}", "path": "inventory.retained_side_consumers"})
    normalized = {
        "expected_dsp48e2_count": expected_dsp,
        "actual_dsp48e2_count": actual_dsp,
        "remaining_old_cone_objects": present,
        "stale_old_cone_drivers": stale_drivers,
        "missing_retained_side_consumers": missing_consumers,
        "candidate_artifact_sha256": inventory["candidate_artifact_sha256"],
    }
    return envelope(
        "emitted_topology_old_cone_checker",
        source_artifact_sha256=plan.source_artifact_sha256,
        candidate_family_id=plan.candidate_family_id,
        candidate_hash=object_digest(normalized),
        status="success" if not reasons else "rejected",
        payload={"checks": normalized},
        failure_reasons=reasons,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan")
    parser.add_argument("inventory")
    args = parser.parse_args()
    import json
    try:
        result = check_emitted_topology(args.plan, read_json(args.inventory))
    except Exception as exc:
        from .common import rejection_from_exception
        result = rejection_from_exception("emitted_topology_old_cone_checker", exc)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
