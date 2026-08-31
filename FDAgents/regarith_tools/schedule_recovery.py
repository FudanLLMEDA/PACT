"""Recover explicit control semantics and register-stage cycle schedules."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .common import envelope, fail, object_digest, read_json


_CONTROL_FIELDS = {
    "control_id", "clock", "clock_enable", "reset_set", "init_value",
    "hold_behavior", "zero_behavior", "alternate_arm_behavior",
    "valid_behavior", "backpressure_behavior",
}
_CLOCK_FIELDS = {"identity", "identity_kind", "edge"}
_CE_FIELDS = {"signal_id", "active_level", "asserted_behavior", "deasserted_behavior", "semantic_source"}
_RESET_FIELDS = {"kind", "signal_id", "active_level", "value", "priority", "semantic_source"}
_STAGE_FIELDS = {
    "stage_id", "cycle_index", "input_word_ids", "output_word_ids",
    "control_semantics_id", "operation", "proof_obligations",
}


def recover_control_and_schedule(family: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(family, Mapping):
        fail("SCHEDULE_INVALID_FAMILY", "family facts must be an object")
    source = str(family.get("source_artifact_sha256") or "")
    candidate = str(family.get("candidate_family_id") or "")
    words = family.get("words")
    if not isinstance(words, list) or not words:
        fail("SCHEDULE_NO_WORDS", "word boundaries are absent")
    word_ids = {item.get("word_id") for item in words if isinstance(item, Mapping)}
    controls_raw = family.get("controls")
    if not isinstance(controls_raw, list) or not controls_raw:
        fail("SCHEDULE_NO_CONTROLS", "control semantics are absent")
    controls = []
    control_ids = set()
    clocks = set()
    for index, raw in enumerate(controls_raw):
        path = f"family.controls[{index}]"
        if not isinstance(raw, Mapping) or set(raw) != _CONTROL_FIELDS:
            fail("SCHEDULE_CONTROL_SCHEMA", "control fields are missing or unknown", path)
        if not isinstance(raw["clock"], Mapping) or set(raw["clock"]) != _CLOCK_FIELDS:
            fail("SCHEDULE_CLOCK_SCHEMA", "clock identity/edge is incomplete", path)
        if raw["clock"]["edge"] not in {"posedge", "negedge"}:
            fail("SCHEDULE_UNKNOWN_CLOCK_EDGE", "clock edge is unsupported", path)
        if not isinstance(raw["clock_enable"], Mapping) or set(raw["clock_enable"]) != _CE_FIELDS:
            fail("SCHEDULE_CE_SCHEMA", "CE behavior is incomplete", path)
        if raw["clock_enable"]["deasserted_behavior"] not in {"hold", "not_applicable"}:
            fail("SCHEDULE_UNKNOWN_CE_HOLD", "CE bubble behavior is unknown", path)
        if not isinstance(raw["reset_set"], Mapping) or set(raw["reset_set"]) != _RESET_FIELDS:
            fail("SCHEDULE_RESET_SCHEMA", "reset/set behavior is incomplete", path)
        if raw["reset_set"]["kind"] not in {
            "none", "synchronous_reset", "asynchronous_reset",
            "synchronous_set", "asynchronous_set", "preserved_external_shell",
        }:
            fail("SCHEDULE_UNKNOWN_RESET_SET", "reset/set kind is unsupported", path)
        if isinstance(raw["init_value"], bool) or not isinstance(raw["init_value"], int):
            fail("SCHEDULE_UNKNOWN_INIT", "INIT value must be explicit", path)
        control_id = raw["control_id"]
        if control_id in control_ids:
            fail("SCHEDULE_DUPLICATE_CONTROL", "control identity repeats", path)
        control_ids.add(control_id)
        clocks.add((raw["clock"]["identity"], raw["clock"]["edge"]))
        controls.append(dict(raw))
    if len(clocks) != 1:
        fail("SCHEDULE_CROSS_CLOCK", "registered family crosses clock identities or edges")

    stages_raw = family.get("stages")
    if not isinstance(stages_raw, list) or not stages_raw:
        fail("SCHEDULE_NO_STAGES", "register-stage schedule is absent")
    stages = []
    for index, raw in enumerate(stages_raw):
        path = f"family.stages[{index}]"
        if not isinstance(raw, Mapping) or set(raw) != _STAGE_FIELDS:
            fail("SCHEDULE_STAGE_SCHEMA", "stage fields are missing or unknown", path)
        if raw["cycle_index"] is None or isinstance(raw["cycle_index"], bool) or not isinstance(raw["cycle_index"], int):
            fail("SCHEDULE_UNRESOLVED_ALIGNMENT", "stage cycle is unresolved", path)
        if raw["control_semantics_id"] not in control_ids:
            fail("SCHEDULE_UNKNOWN_STAGE_CONTROL", "stage references an unknown control", path)
        if any(item not in word_ids for item in raw["input_word_ids"] + raw["output_word_ids"]):
            fail("SCHEDULE_UNKNOWN_STAGE_WORD", "stage references an unknown word", path)
        if not raw["proof_obligations"]:
            fail("SCHEDULE_EMPTY_PROOF_OBLIGATIONS", "stage proof obligations are absent", path)
        stages.append(dict(raw))
    inputs = [item for item in words if item.get("registered") is True and item["word_id"] not in set(family["output_shell"]["output_word_ids"])]
    outputs = [item for item in words if item["word_id"] in set(family["output_shell"]["output_word_ids"])]
    if not inputs or not outputs or any(item.get("cycle_alignment") is None for item in inputs + outputs):
        fail("SCHEDULE_UNRESOLVED_ALIGNMENT", "per-input/output cycle alignment is incomplete")
    schedule = {
        "input_cycle_alignment": [
            {"word_id": item["word_id"], "cycle": item["cycle_alignment"], "reference": "operator_input_cycle"}
            for item in inputs
        ],
        "output_cycle_alignment": [
            {"word_id": item["word_id"], "cycle": item["cycle_alignment"], "reference": "operator_input_cycle"}
            for item in outputs
        ],
        "stages": stages,
    }
    normalized = {"control_semantics": controls, "register_stage_schedule": schedule}
    return envelope(
        "control_register_schedule_recovery",
        source_artifact_sha256=source,
        candidate_family_id=candidate,
        candidate_hash=object_digest(normalized),
        status="success",
        payload=normalized,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("family", type=Path)
    args = parser.parse_args()
    import json
    try:
        result = recover_control_and_schedule(read_json(args.family))
    except Exception as exc:
        from .common import rejection_from_exception
        result = rejection_from_exception("control_register_schedule_recovery", exc)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
