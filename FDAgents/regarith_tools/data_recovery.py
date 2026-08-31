"""Recover typed data terms and exact numeric-loss boundaries from closure facts."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..registered_arithmetic_ir import PlanKind
from .common import envelope, fail, object_digest, read_json


_TERM_FIELDS = {
    "quantized_product": {
        "term_id", "source_word_id", "data_width", "data_signed", "coefficient",
        "coefficient_width", "coefficient_signed", "full_product_width",
        "output_width", "output_signed", "loss_boundary_id", "cycle_alignment",
    },
    "shifted_passthrough": {
        "term_id", "source_word_id", "shift", "output_width", "output_signed",
        "loss_boundary_id", "cycle_alignment",
    },
    "constant_or_bias": {"term_id", "value", "width", "signed", "cycle_alignment"},
    "control_selected_arm": {
        "term_id", "predicate_id", "selected_term_ids", "behavior",
        "alternate_arm_id", "cycle_alignment",
    },
}
_LOSS_FIELDS = {
    "boundary_id", "applies_to", "input_width", "output_width", "slice_lsb",
    "extension", "overflow", "saturation", "rounding", "bias",
}
_GROUP_FIELDS = {
    "group_id", "term_ids", "accumulator_width", "output_width",
    "accumulation_order", "overflow", "loss_boundary_id", "proof_obligations",
}


def _family(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping) and "siblings" in value:
        siblings = value["siblings"]
        if not isinstance(siblings, list) or len(siblings) != 1:
            fail("DATA_FAMILY_SELECTION_REQUIRED", "exactly one sibling must be selected explicitly")
        value = siblings[0]
    if not isinstance(value, Mapping):
        fail("DATA_INVALID_FAMILY", "family facts must be an object")
    return value


def recover_data_terms_and_losses(family_facts: Mapping[str, Any]) -> dict[str, Any]:
    family = _family(family_facts)
    source = family.get("source_artifact_sha256") or family_facts.get("source_artifact_sha256")
    candidate = family.get("candidate_family_id")
    plan_kind = family.get("plan_kind")
    try:
        kind = PlanKind(plan_kind)
    except (TypeError, ValueError):
        fail("DATA_UNKNOWN_PLAN_KIND", "family plan kind is unsupported", "family.plan_kind")
    raw_losses = family.get("losses")
    if not isinstance(raw_losses, list) or not raw_losses:
        fail("DATA_MISSING_LOSS_BOUNDARIES", "numeric loss-boundary set is empty")
    losses = []
    loss_ids = set()
    for index, raw in enumerate(raw_losses):
        path = f"family.losses[{index}]"
        if not isinstance(raw, Mapping) or set(raw) != _LOSS_FIELDS:
            fail("DATA_LOSS_SCHEMA", "loss boundary has missing or unknown fields", path)
        if raw["boundary_id"] in loss_ids:
            fail("DATA_DUPLICATE_LOSS", "loss boundary identity repeats", path)
        if raw["extension"] not in {"none", "sign_extend", "zero_extend"}:
            fail("DATA_UNKNOWN_EXTENSION", "extension semantics are unsupported", path)
        if raw["overflow"] not in {"none", "modular_wrap", "saturate"}:
            fail("DATA_UNKNOWN_OVERFLOW", "overflow semantics are unsupported", path)
        if raw["saturation"] not in {"none", "signed", "unsigned"}:
            fail("DATA_UNKNOWN_SATURATION", "saturation semantics are unsupported", path)
        if raw["rounding"] not in {"none", "discard_low_bits", "round_half_up", "round_to_even"}:
            fail("DATA_UNKNOWN_ROUNDING", "rounding semantics are unsupported", path)
        loss_ids.add(raw["boundary_id"])
        losses.append(dict(raw))

    raw_terms = family.get("terms")
    if not isinstance(raw_terms, list) or not raw_terms:
        fail("DATA_NO_TERMS", "family has no recovered data terms")
    terms = []
    term_ids = set()
    product_count = 0
    for index, raw in enumerate(raw_terms):
        path = f"family.terms[{index}]"
        if not isinstance(raw, Mapping):
            fail("DATA_TERM_SCHEMA", "data term must be an object", path)
        term_kind = raw.get("term_kind")
        expected = _TERM_FIELDS.get(str(term_kind))
        if expected is None:
            fail("DATA_UNSUPPORTED_TERM_KIND", "term kind is unsupported", path)
        if set(raw) != expected | {"term_kind"}:
            fail("DATA_TERM_SCHEMA", "term has missing or unknown fields", path)
        term = {key: raw[key] for key in expected}
        if term["term_id"] in term_ids:
            fail("DATA_DUPLICATE_TERM", "term identity repeats", path)
        term_ids.add(term["term_id"])
        if term_kind in {"quantized_product", "shifted_passthrough"}:
            if not term["loss_boundary_id"] or term["loss_boundary_id"] not in loss_ids:
                fail("DATA_MISSING_TERM_LOSS", "term loss boundary is unresolved", path)
        if term_kind == "quantized_product":
            product_count += 1
            if term["full_product_width"] < term["data_width"] + term["coefficient_width"]:
                fail("DATA_INCOMPLETE_PRODUCT_WIDTH", "product does not retain full operand precision", path)
        terms.append(term)
    if kind == PlanKind.QUANTIZED_PRODUCT_SUM and product_count < 2:
        fail(
            "DATA_PRODUCT_SUM_INCOMPLETE",
            "quantized product-sum recovery requires at least two complete products; no fixed-product fallback is permitted",
        )
    if kind in {PlanKind.SINGLE_FIXED_PRODUCT, PlanKind.WIDE_FIXED_PRODUCT} and product_count != 1:
        fail("DATA_FIXED_PRODUCT_INCOMPLETE", "fixed-product family requires exactly one product")

    raw_groups = family.get("accumulation_groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        fail("DATA_NO_ACCUMULATION_GROUP", "accumulation ordering is absent")
    groups = []
    covered = set()
    for index, raw in enumerate(raw_groups):
        path = f"family.accumulation_groups[{index}]"
        if not isinstance(raw, Mapping) or set(raw) != _GROUP_FIELDS:
            fail("DATA_GROUP_SCHEMA", "accumulation group fields are incomplete", path)
        if raw["accumulation_order"] not in {"after_term_loss", "before_group_loss"}:
            fail("DATA_UNKNOWN_ACCUMULATION_ORDER", "loss/accumulation ordering is unresolved", path)
        if not isinstance(raw["term_ids"], list) or any(item not in term_ids for item in raw["term_ids"]):
            fail("DATA_GROUP_UNKNOWN_TERM", "group references an unknown term", path)
        if raw["loss_boundary_id"] not in loss_ids:
            fail("DATA_MISSING_GROUP_LOSS", "group output loss boundary is unresolved", path)
        if not raw["proof_obligations"]:
            fail("DATA_EMPTY_PROOF_OBLIGATIONS", "group has no exact proof obligations", path)
        groups.append(dict(raw))
        covered.update(raw["term_ids"])
    arithmetic_ids = {
        term["term_id"] for raw, term in zip(raw_terms, terms)
        if raw["term_kind"] != "control_selected_arm"
    }
    if not arithmetic_ids.issubset(covered):
        fail("DATA_UNGROUPED_TERM", "not every arithmetic term has explicit loss ordering")
    normalized = {"data_terms": terms, "loss_boundaries": losses, "accumulation_groups": groups}
    return envelope(
        "data_term_loss_boundary_recovery",
        source_artifact_sha256=str(source or ""),
        candidate_family_id=str(candidate or ""),
        candidate_hash=object_digest(normalized),
        status="success",
        payload={"plan_kind": kind.value, **normalized},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("family", type=Path)
    args = parser.parse_args()
    import json
    try:
        result = recover_data_terms_and_losses(read_json(args.family))
    except Exception as exc:
        from .common import rejection_from_exception
        result = rejection_from_exception("data_term_loss_boundary_recovery", exc)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
