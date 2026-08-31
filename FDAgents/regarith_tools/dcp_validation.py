"""Read-only real-DCP extraction helpers for the Phase 2 fixture matrix."""

from __future__ import annotations

import re
import sys
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..registered_arithmetic_ir import (
    SCHEMA_VERSION,
    AccumulationOrder,
    ClockEnableSemantics,
    ClockIdentity,
    ControlSemantics,
    CycleAlignment,
    ExpectedWallCoverage,
    FullPrecisionAccumulationPartition,
    ModularAccumulationGroup,
    NumericLossBoundary,
    OutputShell,
    PlanKind,
    PartitionedAccumulationGroup,
    QuantizedProductTerm,
    RegisterStage,
    RegisterStageSchedule,
    RegisteredInputWord,
    RegisteredOperatorPlan,
    ReplaceableOldCone,
    ResetSetSemantics,
    RetainedSideConsumers,
    ShiftedPassthroughTerm,
    WordBoundary,
)
from .common import fail, object_digest
from .miner import _default_dcp_loader, _mine_design


def load_local_dcp(path: Path | str, *, edif_path: Path | str | None = None):
    resolved_edif = (
        None if edif_path is None else Path(edif_path).expanduser().resolve()
    )
    return _default_dcp_loader(
        Path(path).expanduser().resolve(), resolved_edif
    )


def mine_loaded_design(design: Any, source_sha256: str) -> dict[str, Any]:
    return dict(_mine_design(design, source_sha256, None))


def _operator_facts_module():
    repo_root = Path(__file__).resolve().parents[2]
    rw_mcp = repo_root / "RapidWrightMCP"
    if str(rw_mcp) not in sys.path:
        sys.path.insert(0, str(rw_mcp))
    import operator_facts
    return operator_facts


def recover_fixed_point_facts(design: Any, source_sha256: str, *, max_candidates: int = 64) -> dict[str, Any]:
    return _operator_facts_module().recover_fixed_point_product_contract(
        design, source_sha256, max_candidates=max_candidates
    )


def _product_sum_trace(
    design: Any,
    sink_base: str,
    sink_ffs: list[str],
    sparse_buses: Mapping[str, Mapping[int, str]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    module = _operator_facts_module()
    netlist = design.getNetlist()
    cells, outputs, boundary_refs, parsed = module._walk_registered_cone(
        design, netlist, sink_base, sink_ffs, sparse_buses
    )
    data_bases = sorted({
        base for base, _index in parsed.values() if base != sink_base
    })
    if not 2 <= len(data_bases) <= 16:
        fail("DCP_PRODUCT_SUM_OPERAND_COUNT", "cone does not have 2..16 registered operands")
    if any(base == sink_base for base, _index in parsed.values()):
        fail("DCP_PRODUCT_SUM_ACCUMULATOR", "self-accumulating cone is not a product sum")
    widths = {base: max(sparse_buses[base]) + 1 for base in data_bases}
    holes = {
        base: [index for index in range(widths[base]) if index not in sparse_buses[base]]
        for base in data_bases
    }
    roles = {base: f"input_{index}" for index, base in enumerate(data_bases)}
    for reference in boundary_refs:
        base, index = parsed[reference["cell"]]
        reference.pop("cell", None)
        reference["kind"] = roles[base]
        reference["bit"] = index

    sink_set = set(sink_ffs)
    source_pins = set()
    for record in cells.values():
        for source in record["inputs"].values():
            if source.get("kind") == "cell":
                source_pins.add((source["cell"], source["pin"]))
    for source in outputs:
        if source.get("kind") == "cell":
            source_pins.add((source["cell"], source["pin"]))
    side_consumers = []
    for cell_name, pin_name in sorted(source_pins):
        pin = module._hier_pin(netlist, f"{cell_name}/{pin_name}")
        parent = module._parent_net(netlist, pin)
        sinks = list(parent.getLeafHierPortInsts(False, True, True))
        for sink in sinks:
            sink_cell = module._cell_name(sink)
            if sink_cell not in cells and sink_cell not in sink_set:
                side_consumers.append({
                    "source_pin": f"{cell_name}/{pin_name}",
                    "sink_pin": f"{sink_cell}/{module._pin_name(sink)}",
                })

    input_ffs = [
        sparse_buses[base][index]
        for base in data_bases for index in sorted(sparse_buses[base])
    ]
    module._shared_canonical_net(
        netlist,
        [module._hier_pin(netlist, f"{name}/C") for name in sink_ffs + input_ffs],
        "product-sum clock boundary",
    )
    clock_net = module._shared_canonical_net(
        netlist,
        [module._hier_pin(netlist, f"{name}/C") for name in sink_ffs],
        "product-sum output clock",
    )
    ce_net = module._shared_canonical_net(
        netlist,
        [module._hier_pin(netlist, f"{name}/CE") for name in sink_ffs],
        "product-sum output CE",
    )
    reset_pins = [module._hier_pin(netlist, f"{name}/R") for name in sink_ffs]
    reset_net = module._shared_canonical_net(
        netlist, reset_pins, "product-sum output reset"
    )
    reset_source = module._unique_source(
        module._parent_net(netlist, reset_pins[0]), "product-sum output reset"
    )
    reset_kind = (
        "none"
        if module._cell_type(reset_source) == "GND"
        and module._pin_name(reset_source).upper() == "G"
        else "synchronous_reset"
    )
    primitive_types = {str(design.getCell(name).getType()).upper() for name in sink_ffs}
    init_values = {
        module._logical_cell_properties(netlist, name).get("INIT")
        for name in sink_ffs
    }
    if primitive_types != {"FDRE"} or init_values != {"1'b0"}:
        fail("DCP_PRODUCT_SUM_CONTROL_UNSUPPORTED", "output register primitive or INIT is unsupported")

    cone = {
        "operand_widths": {roles[base]: widths[base] for base in data_bases},
        "operand_hole_bits": {roles[base]: holes[base] for base in data_bases},
        "cells": cells,
        "outputs": outputs,
    }
    boundary = {
        "input_buses": [
            {
                "role": roles[base],
                "width": widths[base],
                "hole_bits": holes[base],
                "endpoints": [
                    {
                        "cell": sparse_buses[base][index],
                        "pin": f"{sparse_buses[base][index]}/Q",
                    }
                    if index in sparse_buses[base] else {"const": 0}
                    for index in range(widths[base])
                ],
            }
            for base in data_bases
        ],
        "output_bus": [
            {"cell": name, "pin": f"{name}/D"} for name in sink_ffs
        ],
        "cone_cells": [
            {"cell": name, "type": cells[name]["type"]} for name in sorted(cells)
        ],
        "clock_pin": f"{sink_ffs[0]}/C",
        "ce_pin": f"{sink_ffs[0]}/CE",
        "reset_pin": f"{sink_ffs[0]}/R",
        "control": {
            "primitive": "FDRE",
            "init": 0,
            "clock_net": clock_net,
            "ce_net": ce_net,
            "reset_kind": reset_kind,
            "reset_net": reset_net,
        },
        "side_consumers": side_consumers,
        "closure_complete": not side_consumers,
    }
    return cone, boundary


def recover_product_sum_facts(
    design: Any,
    source_sha256: str,
    *,
    max_attempts: int = 64,
    max_candidates: int = 16,
    min_operands: int = 2,
    preferred_output_width: int | None = None,
    preferred_output_cells: tuple[str, ...] | list[str] = (),
    timeout_ms: int = 30000,
) -> dict[str, Any]:
    """Recover exact registered product sums and their explicit loss order."""
    if not isinstance(source_sha256, str) or _SHA256_RE.fullmatch(source_sha256) is None:
        fail("DCP_PRODUCT_SUM_SOURCE_IDENTITY", "source identity must be a lowercase SHA-256")
    for name, value, high in (
        ("max_attempts", max_attempts, 512),
        ("max_candidates", max_candidates, 64),
        ("min_operands", min_operands, 16),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= high:
            fail("DCP_PRODUCT_SUM_LIMIT", f"{name} is outside its supported range")
    if (
        not isinstance(preferred_output_cells, (tuple, list))
        or len(preferred_output_cells) > 16
        or any(
            not isinstance(cell, str) or not cell or len(cell) > 1024
            for cell in preferred_output_cells
        )
    ):
        fail(
            "DCP_PRODUCT_SUM_TARGETS_INVALID",
            "preferred output cells must be a bounded list of exact runtime cell ids",
        )
    preferred_cells = set(preferred_output_cells)
    module = _operator_facts_module()
    buses = module._fixed_point_bus_groups(design)
    sparse_buses = module._fixed_point_sparse_bus_groups(design)
    structural = []
    structural_rejections = []
    for sink_base in sorted(buses):
        try:
            cone, boundary = _product_sum_trace(
                design, sink_base, buses[sink_base], sparse_buses
            )
        except Exception as exc:
            structural_rejections.append({"sink_base": sink_base, "reason": str(exc)})
            continue
        operand_count = len(cone["operand_widths"])
        if operand_count < min_operands:
            continue
        structural.append({
            "sink_base": sink_base,
            "sink_width": len(buses[sink_base]),
            "operand_count": operand_count,
            "cone_cell_count": len(cone["cells"]),
            "closure_complete": boundary["closure_complete"],
            "preferred_live_output": any(
                cell in preferred_cells for cell in buses[sink_base]
            ),
        })
    structural.sort(key=lambda item: (
        -item["preferred_live_output"],
        -(preferred_output_width is not None and item["sink_width"] == preferred_output_width),
        -item["operand_count"], -item["cone_cell_count"], item["sink_base"],
    ))

    candidates = []
    attempts = []
    for summary in structural[:max_attempts]:
        if len(candidates) >= max_candidates:
            break
        sink_base = summary["sink_base"]
        try:
            cone, boundary = _product_sum_trace(
                design, sink_base, buses[sink_base], sparse_buses
            )
            recovery = module.recover_cone_shifted_product_sum(
                cone, timeout_ms=timeout_ms
            )
            payload = {
                "kind": "shifted_product_sum_contract_facts",
                "version": 1,
                "status": "success",
                "design_sha256": source_sha256,
                "proof_status": "exact_cone_miter_unsat",
                "mutation_eligible": False,
                "sink_width": len(buses[sink_base]),
                "recovery": recovery,
                "boundary": boundary,
            }
            region_sha = object_digest(payload)
            facts = {
                **payload,
                "candidate_id": f"productsum:{region_sha[:20]}",
                "region_sha256": region_sha,
                "fresh_rediscovery": True,
            }
            candidates.append(facts)
            attempts.append({**summary, "status": "success", "candidate_id": facts["candidate_id"]})
        except Exception as exc:
            attempts.append({**summary, "status": "rejected", "reason": str(exc)})
    result = {
        "kind": "shifted_product_sum_contract_facts_set",
        "version": 1,
        "status": "success",
        "design_sha256": source_sha256,
        "proof_status": "exact_cone_miter_unsat_for_each_candidate",
        "mutation_eligible": False,
        "fresh_rediscovery": True,
        "structural_candidate_count": len(structural),
        "attempted_candidate_count": len(attempts),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "attempts": attempts,
        "structural_rejection_count": len(structural_rejections),
        "preferred_output_cell_count": len(preferred_cells),
        "preferred_structural_candidate_count": sum(
            item["preferred_live_output"] for item in structural
        ),
    }
    if not candidates:
        result["reason"] = "no registered multi-input cone proved an exact shifted-product sum"
    return result


_PRODUCT_SUM_FACT_FIELDS = {
    "kind", "version", "status", "design_sha256", "proof_status",
    "mutation_eligible", "sink_width", "recovery", "boundary",
    "candidate_id", "region_sha256", "fresh_rediscovery",
}


def product_sum_plan_from_facts(
    facts: Mapping[str, Any],
    *,
    critical_paths: list[list[str]] | None = None,
) -> RegisteredOperatorPlan:
    if not isinstance(facts, Mapping) or set(facts) != _PRODUCT_SUM_FACT_FIELDS:
        fail("DCP_PRODUCT_SUM_FACTS_INCOMPLETE", "product-sum facts schema is incomplete")
    if (
        facts.get("kind") != "shifted_product_sum_contract_facts"
        or facts.get("version") != 1
        or facts.get("status") != "success"
        or facts.get("proof_status") != "exact_cone_miter_unsat"
        or facts.get("mutation_eligible") is not False
        or facts.get("fresh_rediscovery") is not True
    ):
        fail("DCP_PRODUCT_SUM_FACTS_INCOMPLETE", "product-sum fact authority is incomplete")
    payload = dict(facts)
    region_sha = payload.pop("region_sha256")
    candidate_id = payload.pop("candidate_id")
    payload.pop("fresh_rediscovery")
    if (
        not isinstance(region_sha, str)
        or _SHA256_RE.fullmatch(region_sha) is None
        or object_digest(payload) != region_sha
        or candidate_id != f"productsum:{region_sha[:20]}"
    ):
        fail("DCP_PRODUCT_SUM_DIGEST_MISMATCH", "product-sum region digest is invalid")
    recovery = facts["recovery"]
    boundary = facts["boundary"]
    if critical_paths is not None and (
        not isinstance(critical_paths, list)
        or len(critical_paths) > 16
        or any(
            not isinstance(path, list)
            or len(path) > 256
            or any(
                not isinstance(cell, str) or not cell or len(cell) > 1024
                for cell in path
            )
            for path in critical_paths
        )
    ):
        fail(
            "DCP_PRODUCT_SUM_CRITICAL_PATHS_INVALID",
            "critical path evidence is outside the bounded structural schema",
        )
    if (
        not isinstance(recovery, Mapping)
        or recovery.get("status") != "proved"
        or recovery.get("combination") not in {
            "sum_of_independently_shifted_products",
            "partitioned_full_precision_then_shift",
        }
        or recovery.get("verification") != {
            "name": "cone_equals_recovered_shifted_product_sum",
            "result": "unsat",
        }
    ):
        fail(
            "DCP_PRODUCT_SUM_ORDER_UNSUPPORTED",
            "product-sum accumulation/loss order is not represented",
        )
    if not isinstance(boundary, Mapping) or boundary.get("closure_complete") is not True or boundary.get("side_consumers") != []:
        fail("DCP_PRODUCT_SUM_CLOSURE_INCOMPLETE", "product-sum cone has retained side consumers")
    raw_terms = recovery.get("terms")
    raw_inputs = boundary.get("input_buses")
    width = facts.get("sink_width")
    if (
        isinstance(width, bool) or not isinstance(width, int) or width < 1
        or not isinstance(raw_terms, list) or len(raw_terms) < 2
        or not isinstance(raw_inputs, list) or len(raw_inputs) != len(raw_terms)
    ):
        fail("DCP_PRODUCT_SUM_FACTS_INCOMPLETE", "product-sum words or terms are incomplete")
    inputs_by_role = {item.get("role"): item for item in raw_inputs if isinstance(item, Mapping)}
    combination = str(recovery["combination"])
    words = []
    registered_inputs = []
    terms = []
    losses = []
    raw_by_role: dict[str, Mapping[str, Any]] = {}
    word_by_role: dict[str, WordBoundary] = {}
    for index, raw in enumerate(raw_terms):
        if not isinstance(raw, Mapping):
            fail("DCP_PRODUCT_SUM_FACTS_INCOMPLETE", f"term {index} is malformed")
        role = raw.get("operand")
        input_row = inputs_by_role.get(role)
        if input_row is None or raw.get("term_width") != width:
            fail("DCP_PRODUCT_SUM_FACTS_INCOMPLETE", f"term {index} does not bind an input word")
        endpoints = input_row.get("endpoints")
        if not isinstance(endpoints, list) or len(endpoints) != raw.get("data_width"):
            fail("DCP_PRODUCT_SUM_FACTS_INCOMPLETE", f"input word {index} is incomplete")
        endpoint_ids = []
        for bit, endpoint in enumerate(endpoints):
            if endpoint == {"const": 0}:
                endpoint_ids.append(f"const_zero[{bit}]")
            elif isinstance(endpoint, Mapping) and set(endpoint) == {"cell", "pin"}:
                endpoint_ids.append(str(endpoint["pin"]))
            else:
                fail("DCP_PRODUCT_SUM_FACTS_INCOMPLETE", f"input endpoint {index}:{bit} is malformed")
        word = WordBoundary(
            f"product_sum_input_{index}", int(raw["data_width"]),
            bool(raw["data_signed"]), 0, tuple(endpoint_ids), 0,
        )
        words.append(word)
        registered_inputs.append(RegisteredInputWord(word, "exact_registered_Q_product_sum_frontier"))
        raw_by_role[str(role)] = raw
        word_by_role[str(role)] = word

    accumulation_group: ModularAccumulationGroup | PartitionedAccumulationGroup
    if combination == "sum_of_independently_shifted_products":
        for index, raw in enumerate(raw_terms):
            loss = NumericLossBoundary(
                f"product_sum_term_loss_{index}", f"product_sum_term_{index}",
                int(raw["product_width"]), width, int(raw["shift"]),
                "none", "modular_wrap", "none",
                "discard_low_bits" if int(raw["shift"]) else "none", 0,
            )
            term = QuantizedProductTerm(
                f"product_sum_term_{index}", word_by_role[str(raw["operand"])].word_id,
                int(raw["data_width"]), bool(raw["data_signed"]),
                int(raw["coefficient"]), int(raw["coefficient_width"]), False,
                int(raw["product_width"]), width, False, loss.boundary_id, 1,
            )
            losses.append(loss)
            terms.append(term)
    else:
        raw_groups = recovery.get("groups")
        if not isinstance(raw_groups, list) or len(raw_groups) < 2:
            fail(
                "DCP_PRODUCT_SUM_PARTITIONS_INCOMPLETE",
                "partitioned product sum must contain at least two partitions",
            )
        role_locations: dict[str, tuple[int, Mapping[str, Any]]] = {}
        normalized_groups: list[dict[str, Any]] = []
        for group_index, raw_group in enumerate(raw_groups):
            if not isinstance(raw_group, Mapping):
                fail(
                    "DCP_PRODUCT_SUM_PARTITIONS_INCOMPLETE",
                    f"partition {group_index} is malformed",
                )
            operands = raw_group.get("operands")
            coefficients = raw_group.get("coefficients")
            operand_signedness = raw_group.get("operand_signedness")
            shift = raw_group.get("shift")
            if (
                not isinstance(operands, list)
                or not operands
                or len(operands) != len(set(operands))
                or not isinstance(coefficients, Mapping)
                or set(coefficients) != set(operands)
                or (
                    operand_signedness is not None
                    and (
                        not isinstance(operand_signedness, Mapping)
                        or set(operand_signedness) != set(operands)
                        or any(
                            not isinstance(operand_signedness[role], bool)
                            for role in operands
                        )
                    )
                )
                or isinstance(shift, bool)
                or not isinstance(shift, int)
                or shift < 0
            ):
                fail(
                    "DCP_PRODUCT_SUM_PARTITIONS_INCOMPLETE",
                    f"partition {group_index} operands, coefficients, or shift are incomplete",
                )
            if any(role not in raw_by_role or role in role_locations for role in operands):
                fail(
                    "DCP_PRODUCT_SUM_PARTITION_COVERAGE",
                    "partition operands are unknown or repeated",
                )
            if any(
                isinstance(coefficients[role], bool)
                or not isinstance(coefficients[role], int)
                for role in operands
            ):
                fail(
                    "DCP_PRODUCT_SUM_PARTITIONS_INCOMPLETE",
                    f"partition {group_index} coefficients are unsupported",
                )
            if len(operands) == 1:
                role = str(operands[0])
                projected = raw_by_role[role]
                result_width = width
                result_signed = bool(projected["data_signed"])
                if (
                    shift != projected.get("shift")
                    or coefficients[role] != projected.get("coefficient")
                ):
                    fail(
                        "DCP_PRODUCT_SUM_PARTITIONS_INCOMPLETE",
                        "singleton partition differs from its exact projected term",
                    )
            else:
                result_width = raw_group.get("result_width")
                result_signed = raw_group.get("result_signed")
                if (
                    isinstance(result_width, bool)
                    or not isinstance(result_width, int)
                    or not 1 <= result_width <= width
                    or not isinstance(result_signed, bool)
                ):
                    fail(
                        "DCP_PRODUCT_SUM_PARTITIONS_INCOMPLETE",
                        f"partition {group_index} result contract is incomplete",
                    )
            accumulator_width = max(
                shift + result_width,
                *(
                    int(raw_by_role[str(role)]["data_width"])
                    + (
                        max(1, (~int(coefficients[role])).bit_length() + 1)
                        if int(coefficients[role]) < 0
                        else max(1, int(coefficients[role]).bit_length())
                    )
                    for role in operands
                ),
            ) + len(operands).bit_length()
            normalized_groups.append({
                "operands": tuple(str(role) for role in operands),
                "coefficients": {str(role): int(coefficients[role]) for role in operands},
                "operand_signedness": {
                    str(role): (
                        bool(operand_signedness[role])
                        if operand_signedness is not None
                        else bool(raw_by_role[str(role)]["data_signed"])
                    )
                    for role in operands
                },
                "shift": shift,
                "result_width": result_width,
                "result_signed": result_signed,
                "accumulator_width": accumulator_width,
            })
            for role in operands:
                role_locations[str(role)] = (group_index, raw_group)
        if set(role_locations) != set(raw_by_role):
            fail(
                "DCP_PRODUCT_SUM_PARTITION_COVERAGE",
                "partitions do not cover every recovered term exactly once",
            )

        partitions = []
        term_id_by_role = {}
        for role, (group_index, _) in role_locations.items():
            signed = bool(normalized_groups[group_index]["operand_signedness"][role])
            old_word = word_by_role[role]
            if old_word.signed != signed:
                word = WordBoundary(
                    old_word.word_id, old_word.width, signed, old_word.lsb_index,
                    old_word.endpoint_ids, old_word.cycle_alignment,
                )
                word_index = words.index(old_word)
                words[word_index] = word
                registered_inputs[word_index] = RegisteredInputWord(
                    word, registered_inputs[word_index].register_semantics
                )
                word_by_role[role] = word
        for role_index, raw in enumerate(raw_terms):
            role = str(raw["operand"])
            group_index, _ = role_locations[role]
            group = normalized_groups[group_index]
            coefficient = group["coefficients"][role]
            coefficient_width = (
                max(1, (~coefficient).bit_length() + 1)
                if coefficient < 0 else max(1, coefficient.bit_length())
            )
            term_id = f"product_sum_term_{role_index}"
            term_id_by_role[role] = term_id
            terms.append(QuantizedProductTerm(
                term_id, word_by_role[role].word_id,
                int(raw["data_width"]),
                bool(group["operand_signedness"][role]),
                coefficient, coefficient_width, coefficient < 0,
                int(group["accumulator_width"]),
                int(group["accumulator_width"]),
                bool(group["operand_signedness"][role]), None, 1,
            ))
        for group_index, group in enumerate(normalized_groups):
            loss = NumericLossBoundary(
                f"product_sum_partition_loss_{group_index}",
                f"product_sum_partition_{group_index}",
                int(group["accumulator_width"]), int(group["result_width"]),
                int(group["shift"]), "none", "modular_wrap", "none",
                "discard_low_bits" if int(group["shift"]) else "none", 0,
            )
            losses.append(loss)
            extension = (
                "none" if int(group["result_width"]) == width
                else "sign_extend" if bool(group["result_signed"])
                else "zero_extend"
            )
            partitions.append(FullPrecisionAccumulationPartition(
                f"product_sum_partition_{group_index}",
                tuple(term_id_by_role[role] for role in group["operands"]),
                int(group["accumulator_width"]), int(group["result_width"]),
                bool(group["result_signed"]), extension, loss.boundary_id,
                (
                    "REGARITH_PARTITIONED_PRODUCT_SUM_V1:full_precision_accumulation",
                    "REGARITH_PARTITIONED_PRODUCT_SUM_V1:single_partition_loss",
                ),
            ))
    output_endpoints = boundary.get("output_bus")
    if not isinstance(output_endpoints, list) or len(output_endpoints) != width:
        fail("DCP_PRODUCT_SUM_FACTS_INCOMPLETE", "product-sum output word is incomplete")
    output_word = WordBoundary(
        "product_sum_output", width, False, 0,
        tuple(str(item["pin"]) for item in output_endpoints), 1,
    )
    words.append(output_word)
    group_loss = NumericLossBoundary(
        "product_sum_group_wrap", "product_sum_accumulation", width, width,
        0, "none", "modular_wrap", "none", "none", 0,
    )
    losses.append(group_loss)
    if combination == "sum_of_independently_shifted_products":
        accumulation_group = ModularAccumulationGroup(
            "product_sum_group", tuple(term.term_id for term in terms),
            width, width, AccumulationOrder.AFTER_TERM_LOSS,
            "modular_wrap", group_loss.boundary_id,
            ("REGARITH_PRODUCT_SUM_V1:exact_cone_miter_unsat",),
        )
    else:
        accumulation_group = PartitionedAccumulationGroup(
            "product_sum_group", tuple(term.term_id for term in terms),
            tuple(partitions), width, width,
            AccumulationOrder.PARTITIONED_FULL_PRECISION_THEN_SHIFT,
            "modular_wrap", group_loss.boundary_id,
            ("REGARITH_PARTITIONED_PRODUCT_SUM_V1:exact_cone_miter_unsat",),
        )
    control_raw = boundary.get("control")
    if not isinstance(control_raw, Mapping) or control_raw.get("primitive") != "FDRE" or control_raw.get("init") != 0:
        fail("DCP_PRODUCT_SUM_CONTROL_UNSUPPORTED", "product-sum output controls are incomplete")
    reset_kind = str(control_raw.get("reset_kind"))
    if reset_kind not in {"none", "synchronous_reset"}:
        fail("DCP_PRODUCT_SUM_CONTROL_UNSUPPORTED", "product-sum reset kind is unsupported")
    control = ControlSemantics(
        "product_sum_output_control",
        ClockIdentity(str(control_raw["clock_net"]), "recovered_net", "posedge"),
        ClockEnableSemantics(str(control_raw["ce_net"]), "high", "advance", "hold", "exact_FDRE_boundary"),
        ResetSetSemantics(
            reset_kind,
            None if reset_kind == "none" else str(control_raw["reset_net"]),
            "always" if reset_kind == "none" else "high",
            None if reset_kind == "none" else 0,
            "not_applicable" if reset_kind == "none" else "before_ce",
            "exact_FDRE_boundary",
        ),
        0, "hold", "none" if reset_kind == "none" else "reset_or_selected_zero_arm",
        "none", "not_observable", "not_observable",
    )
    coverage_cells = {
        str(item["cell"]) for item in boundary["cone_cells"]
    } | {
        str(item["cell"]) for item in boundary["output_bus"]
        if isinstance(item, Mapping) and isinstance(item.get("cell"), str)
    }
    covered_paths = (
        0
        if critical_paths is None
        else sum(
            any(
                token == cell or token.startswith(cell + "/")
                for token in path
                for cell in coverage_cells
            )
            for path in critical_paths
        )
    )
    coverage = (
        ExpectedWallCoverage(
            "not_available_without_promoted_timing_payload", 0, 0, region_sha
        )
        if critical_paths is None
        else ExpectedWallCoverage(
            "exact_current_dcp_path_cell_overlap",
            covered_paths,
            (
                int(1_000_000 * covered_paths / len(critical_paths))
                if critical_paths else 0
            ),
            object_digest({
                "source_artifact_sha256": facts["design_sha256"],
                "critical_paths": critical_paths,
            }),
        )
    )
    plan = RegisteredOperatorPlan(
        SCHEMA_VERSION, PlanKind.QUANTIZED_PRODUCT_SUM,
        str(facts["design_sha256"]), str(candidate_id), tuple(words),
        tuple(registered_inputs), tuple(terms), tuple(losses),
        (accumulation_group,),
        (control,),
        RegisterStageSchedule(
            tuple(CycleAlignment(word.word_id, 0, "operator_input_cycle") for word in words[:-1]),
            (CycleAlignment(output_word.word_id, 1, "operator_input_cycle"),),
            (RegisterStage(
                "product_sum_output_stage", 1,
                tuple(word.word_id for word in words[:-1]),
                (output_word.word_id,), control.control_id,
                (
                    "REGARITH_PRODUCT_SUM_V1"
                    if combination == "sum_of_independently_shifted_products"
                    else "REGARITH_PARTITIONED_PRODUCT_SUM_V1"
                ),
                (
                    ("separate_term_loss_equal", "modular_sum_equal", "output_cycle_equal")
                    if combination == "sum_of_independently_shifted_products"
                    else (
                        "partition_full_precision_equal",
                        "partition_loss_and_extension_equal",
                        "final_modular_sum_equal",
                        "output_cycle_equal",
                    )
                ),
            ),),
        ),
        OutputShell((output_word.word_id,), "preserve_existing_FDRE_output_shell", (), ("exact_output_D_boundary",)),
        ReplaceableOldCone(
            tuple(str(item["cell"]) for item in boundary["cone_cells"]),
            "exact_closed_product_sum_cone",
            ("exact_cone_miter_unsat", "no_retained_side_consumers"),
        ),
        RetainedSideConsumers((), "complete_no_side_consumers", ("fanout_inventory_complete",)),
        coverage,
    )
    return plan.validate()


def select_product_sum_candidate(
    facts_set: Mapping[str, Any], *, min_siblings: int = 1
) -> tuple[Mapping[str, Any], dict[str, Any]]:
    candidates = facts_set.get("candidates") if isinstance(facts_set, Mapping) else None
    if not isinstance(candidates, list):
        fail("DCP_PRODUCT_SUM_SELECTION_EMPTY", "product-sum candidate set is absent")
    groups: dict[str, list[Mapping[str, Any]]] = {}
    rejected = []
    for facts in candidates:
        try:
            plan = product_sum_plan_from_facts(facts)
        except Exception as exc:
            rejected.append({"candidate_id": facts.get("candidate_id"), "reason": str(exc)})
            continue
        losses_by_id = {loss.boundary_id: loss for loss in plan.loss_boundaries}
        signature = object_digest({
            "terms": [
                {
                    "data_width": term.data_width,
                    "data_signed": term.data_signed,
                    "coefficient": term.coefficient,
                    "coefficient_width": term.coefficient_width,
                    "full_product_width": term.full_product_width,
                    "output_width": term.output_width,
                    "slice_lsb": (
                        None
                        if term.loss_boundary_id is None
                        else losses_by_id[term.loss_boundary_id].slice_lsb
                    ),
                }
                for term in plan.data_terms
            ],
            "accumulation_groups": [
                {
                    "kind": type(group).__name__,
                    "order": (
                        group.accumulation_order.value
                        if isinstance(group.accumulation_order, AccumulationOrder)
                        else group.accumulation_order
                    ),
                    "partitions": [
                        {
                            "term_indices": [
                                next(
                                    index for index, term in enumerate(plan.data_terms)
                                    if term.term_id == term_id
                                )
                                for term_id in partition.term_ids
                            ],
                            "accumulator_width": partition.accumulator_width,
                            "result_width": partition.result_width,
                            "result_signed": partition.result_signed,
                            "output_extension": partition.output_extension,
                            "slice_lsb": losses_by_id[
                                partition.loss_boundary_id
                            ].slice_lsb,
                        }
                        for partition in (
                            group.partitions
                            if isinstance(group, PartitionedAccumulationGroup)
                            else ()
                        )
                    ],
                }
                for group in plan.accumulation_groups
            ],
            "output_width": facts["sink_width"],
        })
        groups.setdefault(signature, []).append(facts)
    eligible = [
        (signature, members) for signature, members in groups.items()
        if len(members) >= min_siblings
    ]
    if not eligible:
        fail("DCP_PRODUCT_SUM_SELECTION_EMPTY", "no representable product-sum sibling family meets coverage")
    signature, members = max(eligible, key=lambda item: (
        len(item[1]),
        len(item[1][0]["boundary"]["cone_cells"]),
        len(item[1][0]["recovery"]["terms"]),
        item[0],
    ))
    selected = min(members, key=lambda item: item["candidate_id"])
    return selected, {
        "semantic_signature": signature,
        "sibling_count": len(members),
        "sibling_candidate_ids": sorted(item["candidate_id"] for item in members),
        "representable_family_count": len(groups),
        "rejected_candidates": rejected,
        "selection": "max_semantic_sibling_support_then_closed_cone_size",
    }


def recover_recurrence_family_facts(design: Any, source_sha256: str) -> dict[str, Any]:
    return _operator_facts_module().recover_dsp_recurrence_family_contract(
        design, design_sha256=source_sha256, max_boundaries=32
    )


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_RECURRENCE_FAMILY_FIELDS = {
    "kind", "version", "status", "design_sha256", "boundary_count",
    "state_width", "dsp_c_width", "recurrence_period_transactions",
    "transport_valid_transactions", "boundary_region_sha256s", "boundaries",
    "composition_contract", "proof_status", "mutation_eligible",
    "family_contract_sha256", "family_id",
}


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        fail("DCP_RECURRENCE_FACTS_INCOMPLETE", f"{field} must be a positive integer")
    return value


def _validate_recurrence_family_facts(facts: Any) -> tuple[int, int, list[Mapping[str, Any]]]:
    if not isinstance(facts, Mapping) or set(facts) != _RECURRENCE_FAMILY_FIELDS:
        fail("DCP_RECURRENCE_FACTS_INCOMPLETE", "exact recurrence family fields are incomplete")
    if (
        facts.get("kind") != "dsp_recurrence_family_contract_facts"
        or facts.get("version") != 1
        or facts.get("status") != "success"
        or facts.get("proof_status") != "family_rewrite_facts_extracted_unproved"
        or facts.get("mutation_eligible") is not False
    ):
        fail("DCP_RECURRENCE_FACTS_INCOMPLETE", "recurrence family authority is incomplete")
    design_sha = facts.get("design_sha256")
    family_sha = facts.get("family_contract_sha256")
    if not isinstance(design_sha, str) or _SHA256_RE.fullmatch(design_sha) is None:
        fail("DCP_RECURRENCE_FACTS_INCOMPLETE", "recurrence design identity is malformed")
    if not isinstance(family_sha, str) or _SHA256_RE.fullmatch(family_sha) is None:
        fail("DCP_RECURRENCE_FACTS_INCOMPLETE", "recurrence family identity is malformed")
    family_payload = dict(facts)
    family_payload.pop("family_contract_sha256")
    family_payload.pop("family_id")
    if object_digest(family_payload) != family_sha or facts.get("family_id") != f"recurrence-family:{family_sha[:20]}":
        fail("DCP_RECURRENCE_DIGEST_MISMATCH", "recurrence family digest binding is invalid")

    width = _positive_int(facts.get("state_width"), "state_width")
    dsp_width = _positive_int(facts.get("dsp_c_width"), "dsp_c_width")
    period = _positive_int(
        facts.get("recurrence_period_transactions"),
        "recurrence_period_transactions",
    )
    delay = _positive_int(
        facts.get("transport_valid_transactions"), "transport_valid_transactions"
    )
    if dsp_width < width or period <= delay:
        fail("DCP_RECURRENCE_FACTS_INCOMPLETE", "recurrence width or transaction period is inconsistent")
    count = _positive_int(facts.get("boundary_count"), "boundary_count")
    boundaries = facts.get("boundaries")
    region_ids = facts.get("boundary_region_sha256s")
    if not isinstance(boundaries, list) or count < 2 or len(boundaries) != count:
        fail("DCP_RECURRENCE_FACTS_INCOMPLETE", "recurrence boundary family is incomplete")
    if not isinstance(region_ids, list) or len(region_ids) != count:
        fail("DCP_RECURRENCE_FACTS_INCOMPLETE", "recurrence boundary identities are incomplete")

    composition = facts.get("composition_contract")
    if not isinstance(composition, Mapping):
        fail("DCP_RECURRENCE_COMPOSITION_INCOMPLETE", "recurrence composition contract is absent")
    composition_payload = dict(composition)
    composition_sha = composition_payload.pop("contract_sha256", None)
    required_composition = {
        "unique_relay_cells", "unique_source_dsps", "unique_sink_dsps",
        "unique_reinjection_dsps", "rewrite_windows_disjoint",
    }
    if (
        set(composition_payload) != required_composition
        or any(composition_payload[key] is not True for key in required_composition)
        or not isinstance(composition_sha, str)
        or object_digest(composition_payload) != composition_sha
    ):
        fail("DCP_RECURRENCE_COMPOSITION_INCOMPLETE", "recurrence composition is not proved disjoint")

    validated = []
    recovered_regions = []
    relay_cells = []
    for index, boundary in enumerate(boundaries):
        if not isinstance(boundary, Mapping):
            fail("DCP_RECURRENCE_FACTS_INCOMPLETE", f"boundary {index} is not an object")
        region_sha = boundary.get("region_sha256")
        boundary_payload = dict(boundary)
        boundary_payload.pop("region_sha256", None)
        if (
            not isinstance(region_sha, str)
            or _SHA256_RE.fullmatch(region_sha) is None
            or object_digest(boundary_payload) != region_sha
        ):
            fail("DCP_RECURRENCE_DIGEST_MISMATCH", f"boundary {index} digest binding is invalid")
        if (
            boundary.get("design_sha256") != design_sha
            or boundary.get("state_width") != width
            or boundary.get("dsp_c_width") != dsp_width
            or boundary.get("status") != "success"
            or boundary.get("proof_status") != "rewrite_facts_extracted_unproved"
            or boundary.get("mutation_eligible") is not False
        ):
            fail("DCP_RECURRENCE_FACTS_INCOMPLETE", f"boundary {index} authority is incomplete")
        relay_bits = boundary.get("relay_bits")
        if not isinstance(relay_bits, list) or len(relay_bits) != width:
            fail("DCP_RECURRENCE_FACTS_INCOMPLETE", f"boundary {index} relay word is incomplete")
        try:
            ordered = sorted(relay_bits, key=lambda item: int(item["index"]))
        except (KeyError, TypeError, ValueError):
            fail("DCP_RECURRENCE_FACTS_INCOMPLETE", f"boundary {index} relay indices are malformed")
        if [item.get("index") for item in ordered] != list(range(width)):
            fail("DCP_RECURRENCE_FACTS_INCOMPLETE", f"boundary {index} relay indices are not contiguous")
        for bit in ordered:
            ff = bit.get("ff")
            if (
                not isinstance(ff, Mapping)
                or not isinstance(ff.get("cell"), str)
                or not ff["cell"]
                or not isinstance(bit.get("d_driver_pin"), str)
                or not bit["d_driver_pin"]
                or not isinstance(bit.get("q_net"), str)
                or not bit["q_net"]
            ):
                fail("DCP_RECURRENCE_FACTS_INCOMPLETE", f"boundary {index} relay endpoint is incomplete")
            relay_cells.append(ff["cell"])
        endpoint = boundary.get("relay_endpoint_contract")
        if (
            not isinstance(endpoint, Mapping)
            or endpoint.get("status") != "proved"
            or endpoint.get("primitive") != "FDRE"
            or endpoint.get("init") != 0
            or endpoint.get("inversions") != {
                "ce_inverted": False, "clock_inverted": False,
                "reset_inverted": False,
            }
        ):
            fail("DCP_RECURRENCE_CONTROL_UNSUPPORTED", f"boundary {index} endpoint controls are unsupported")
        controls = boundary.get("controls")
        if not isinstance(controls, Mapping) or any(
            not isinstance(controls.get(key), str) or not controls[key]
            for key in ("clock_net", "ce_net", "reset_net")
        ):
            fail("DCP_RECURRENCE_CONTROL_UNSUPPORTED", f"boundary {index} controls are incomplete")
        rewrite = boundary.get("rewrite_plan")
        if (
            not isinstance(rewrite, Mapping)
            or rewrite.get("transport_valid_transactions") != delay
            or rewrite.get("recurrence_period_transactions") != period
        ):
            fail("DCP_RECURRENCE_FACTS_INCOMPLETE", f"boundary {index} transport schedule is inconsistent")
        recovered_regions.append(region_sha)
        validated.append(boundary)
    if recovered_regions != region_ids or recovered_regions != sorted(recovered_regions):
        fail("DCP_RECURRENCE_DIGEST_MISMATCH", "ordered recurrence boundary identities do not match")
    if len(relay_cells) != len(set(relay_cells)):
        fail("DCP_RECURRENCE_COMPOSITION_INCOMPLETE", "recurrence relay cells overlap")
    return width, delay, validated


def recurrence_plan_from_facts(facts: dict[str, Any]) -> RegisteredOperatorPlan:
    width, delay, boundaries = _validate_recurrence_family_facts(facts)
    words = []
    inputs = []
    terms = []
    losses = []
    groups = []
    control_semantics = []
    input_alignments = []
    output_alignments = []
    stages = []
    output_ids = []
    old_objects = []
    for index, boundary in enumerate(boundaries):
        prefix = f"recurrence_{index}"
        relay_bits = sorted(boundary["relay_bits"], key=lambda item: int(item["index"]))
        input_word = WordBoundary(
            f"{prefix}_source_state", width, False, 0,
            tuple(str(item["d_driver_pin"]) for item in relay_bits), 0,
        )
        output_word = WordBoundary(
            f"{prefix}_delayed_state", width, False, 0,
            tuple(str(item["q_net"]) for item in relay_bits), delay,
        )
        loss = NumericLossBoundary(
            f"{prefix}_transport_no_loss", "recurrence_transport", width, width,
            0, "none", "modular_wrap", "none", "none", 0,
        )
        term = ShiftedPassthroughTerm(
            f"{prefix}_transport_term", input_word.word_id, 0, width, False,
            loss.boundary_id, delay,
        )
        controls = boundary["controls"]
        control = ControlSemantics(
            f"{prefix}_transport_control",
            ClockIdentity(str(controls["clock_net"]), "recovered_net", "posedge"),
            ClockEnableSemantics(str(controls["ce_net"]), "high", "advance", "hold", "exact_recurrence_boundary"),
            ResetSetSemantics("synchronous_reset", str(controls["reset_net"]), "high", 0, "before_ce", "FDRE_exact_endpoint"),
            0, "hold", "reset_or_selected_zero_arm", "none", "not_observable", "not_observable",
        )
        words.extend((input_word, output_word))
        inputs.append(RegisteredInputWord(input_word, "exact_DSP_P_to_FDRE_recurrence_frontier"))
        terms.append(term)
        losses.append(loss)
        groups.append(ModularAccumulationGroup(
            f"{prefix}_transport_group", (term.term_id,), width, width,
            "after_term_loss", "modular_wrap", loss.boundary_id,
            ("DSP_PREG_SRL_RECURRENCE_CUT_V1:transaction_age_transport",),
        ))
        control_semantics.append(control)
        input_alignments.append(CycleAlignment(input_word.word_id, 0, "valid_transaction"))
        output_alignments.append(CycleAlignment(output_word.word_id, delay, "valid_transaction"))
        stages.append(RegisterStage(
            f"{prefix}_transport_stage", delay, (input_word.word_id,),
            (output_word.word_id,), control.control_id,
            "DSP_PREG_SRL_RECURRENCE_CUT_V1",
            ("transport_delay_equal", "CE_qualified_age_step", "zero_reset_mask"),
        ))
        output_ids.append(output_word.word_id)
        old_objects.extend(str(item["ff"]["cell"]) for item in relay_bits)
    plan = RegisteredOperatorPlan(
        SCHEMA_VERSION, PlanKind.RECURRENCE_TRANSPORT_CUT,
        facts["design_sha256"], facts["family_id"],
        tuple(words), tuple(inputs), tuple(terms), tuple(losses), tuple(groups),
        tuple(control_semantics),
        RegisterStageSchedule(
            tuple(input_alignments), tuple(output_alignments), tuple(stages),
        ),
        OutputShell(tuple(output_ids), "recurrence_transport_boundary", (), ("exact_recurrence_family_composition",)),
        ReplaceableOldCone(tuple(old_objects), "exact_recurrence_relay_family", ("unique_relay_cells", "disjoint_rewrite_windows")),
        RetainedSideConsumers((), "relay_Q_fanout_exactly_DSP_C_inputs", ("no_relay_side_fanout",)),
        ExpectedWallCoverage(
            "exact_recurrence_boundary_family", int(facts["boundary_count"]),
            1_000_000, facts["family_contract_sha256"],
        ),
    )
    return plan.validate()


_ROOT_RE = re.compile(r"(?:^|[/_.])(sqrt|root)(?:$|[/_.\[])|sqrt|root", re.I)
_DIV_RE = re.compile(r"(?:^|[/_.])(div|quot|remainder)(?:$|[/_.\[])|divider|quotient", re.I)


def classify_root_divider_recurrence(design: Any, source_sha256: str) -> dict[str, Any]:
    """Classify semantic recurrence from structural hierarchy, never design name."""
    if not isinstance(source_sha256, str) or _SHA256_RE.fullmatch(source_sha256) is None:
        fail("DCP_ROOT_DIVIDER_SOURCE_IDENTITY", "source identity must be a lowercase SHA-256")
    records = []
    for cell in list(design.getCells()):
        name = str(cell.getName())
        cell_type = str(cell.getType()).upper()
        scope = "root" if _ROOT_RE.search(name) else "divider" if _DIV_RE.search(name) else None
        if scope is not None:
            records.append((scope, name, cell_type))
    counts = Counter(scope for scope, _name, _type in records)
    sequential = [row for row in records if row[2].startswith(("FD", "SRL"))]
    arithmetic = [row for row in records if row[2].startswith(("CARRY", "DSP", "LUT"))]
    indexed_sequential = [
        row for row in sequential if re.search(r"\[\d+\]", row[1]) is not None
    ]
    type_counts = Counter(row[2] for row in records)
    evidence = {
        "root_scoped_cell_count": counts["root"],
        "divider_scoped_cell_count": counts["divider"],
        "sequential_cell_count": len(sequential),
        "indexed_sequential_cell_count": len(indexed_sequential),
        "arithmetic_cell_count": len(arithmetic),
        "carry_cell_count": sum(row[2].startswith("CARRY") for row in records),
        "dsp_internal_cell_count": sum(row[2].startswith("DSP") for row in records),
        "primitive_type_counts": dict(sorted(type_counts.items())),
    }
    if counts["root"] < 1 or counts["divider"] < 1 or len(sequential) < 2 or len(arithmetic) < 1:
        fail(
            "DCP_ROOT_DIVIDER_CLASSIFICATION_INCOMPLETE",
            "netlist lacks complete root, divider, sequential, and arithmetic structural evidence",
        )
    fingerprint = object_digest(evidence)
    return {
        "classification": "root_divider_recurrence",
        "source_artifact_sha256": source_sha256,
        "family_fingerprint": fingerprint,
        "structural_evidence": evidence,
        "proof_status": "structural_classification_only",
    }


__all__ = [
    "load_local_dcp", "mine_loaded_design", "recover_fixed_point_facts",
    "recover_product_sum_facts", "product_sum_plan_from_facts",
    "select_product_sum_candidate",
    "recover_recurrence_family_facts", "recurrence_plan_from_facts",
    "classify_root_divider_recurrence",
]
