"""Replayable bit-vector proofs for restricted structural rewrite contracts.

These certificates authorize a plan against extracted region facts. They are
not post-emission equivalence certificates and cannot authorize candidate
promotion without a fresh extraction and proof of the emitted DCP.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Optional

from .metaloop_action_proof import (
    replay_memory_forward_authorization,
    verify_constructive_accumulator_authorization,
)


SEMANTICS_VERSION = "operator-bv-v1"
AUTHORIZATION_VERSION = 1
POST_EMISSION_VERSION = 1
MEASUREMENT_ADMISSION_VERSION = 1
OPERATOR_CANDIDATE_PROOF_VERSION = "operator-rewrite-v2"
STRICT_VALIDATOR_PROTOCOL = "dcp-structural-xsim-v1"
STRICT_STIMULUS_PROTOCOL = "lfsr32-deadbeef-v1"
MIN_POST_EMISSION_VECTORS = 1000
MIN_POST_EMISSION_PRECHECK_VECTORS = 100
_SHA256_RE = re.compile(r"[0-9a-f]{64}")

OPERATOR_TEMPLATE_BINDINGS = {
    "fixed_point_dsp_rebuild_v1": {
        "rewrite_kind": "fabric_to_dsp",
        "proof_template": "DSP48E2_MUL_SHIFT_ACCUM_V1",
    },
    "registered_fir_data_cone_rewrite": {
        "rewrite_kind": "fabric_to_dsp",
        "proof_template": "DSP48E2_MUL_SHIFT_ACCUM_V1",
    },
    "consumer_driven_registered_arithmetic_rewrite_v1": {
        "rewrite_kind": "consumer_driven_arithmetic_rewrite",
        "proof_template": "CONSUMER_DRIVEN_ARITHMETIC_REWRITE_V1",
    },
    "dsp_srl_recurrence_cut_v1": {
        "rewrite_kind": "dsp_recurrence_cut_transport",
        "proof_template": "DSP_PREG_SRL_RECURRENCE_CUT_V1",
    },
    "proved_arithmetic_compressor_v1": {
        "rewrite_kind": "bounded_arithmetic_cone_compression",
        "proof_template": "ARITHMETIC_COMPRESSOR_BOUNDARY_V1",
    },
    "bank_local_predicate_specialization_v1": {
        "rewrite_kind": "banked_select_predicate_specialization",
        "proof_template": "BANKED_SELECT_PREDICATE_UPDATE_V1",
    },
    "registered_dsp_lane_rebuild_v1": {
        "rewrite_kind": "low_fanout_registered_dsp_lane_rebuild",
        "proof_template": "REGISTERED_DSP_LANE_BOUNDARY_V1",
    },
    "registered_dead_state_elimination_v1": {
        "rewrite_kind": "registered_observable_dead_state_elimination",
        "proof_template": "REGISTERED_OBSERVABLE_DEAD_STATE_V1",
    },
    "exact_control_to_data_state_equation_v1": {
        "rewrite_kind": "exact_control_to_data_state_equation",
        "proof_template": "EXACT_CONTROL_TO_DATA_STATE_EQUATION_V1",
    },
    "ff_srl_boundary_bridge_v1": {
        "rewrite_kind": "direct_ff_chain_to_srl_boundary_bridge",
        "proof_template": "FF_SRL_BOUNDARY_BRIDGE_V1",
    },
    "memory_write_buffer_forward_v1": {
        "rewrite_kind": "memory_write_buffer_forward",
        "proof_template": "BRAM_WRITE_BUFFER_FORWARD_LOCAL_MITER_V1",
    },
    "multilevel_control_distribution_localization_v1": {
        "rewrite_kind": "multilevel_registered_control_distribution_localization",
        "proof_template": "MULTILEVEL_CONTROL_DAG_CLONE_V1",
    },
    "multilevel_ram_control_distribution_localization_v1": {
        "rewrite_kind": "multilevel_ram_control_distribution_localization",
        "proof_template": "MULTILEVEL_RAM_CONTROL_DAG_CLONE_V1",
    },
    "registered_gf2_parity_reassociation_v1": {
        "rewrite_kind": "registered_gf2_parity_reassociation",
        "proof_template": "REGISTERED_GF2_PARITY_REASSOCIATION_V1",
    },
    "constructive_accumulator_rebuild_v1": {
        "rewrite_kind": "constructive_accumulator_rebuild",
        "proof_template": "CONSTRUCTIVE_ACCUMULATOR_GENERIC_V2",
    },
    "registered_ram_boolean_midpoint_relocation_v1": {
        "rewrite_kind": "registered_ram_boolean_midpoint_relocation",
        "proof_template": "REGISTERED_RAM_BOOLEAN_MIDPOINT_V1",
    },
    "registered_control_packet_localization_v1": {
        "rewrite_kind": "registered_control_packet_localization",
        "proof_template": "REGISTERED_CONTROL_PACKET_LOCALIZATION_V1",
    },
    "commutative_operand_bus_slot_reassociation_v1": {
        "rewrite_kind": "commutative_operand_bus_slot_reassociation",
        "proof_template": "COMMUTATIVE_OPERAND_BUS_SLOT_REASSOCIATION_V1",
    },
}


class OperatorProofError(ValueError):
    """Raised when a rewrite contract is malformed or cannot be proved."""


@dataclass(frozen=True)
class FixedPointProductSpec:
    data_width: int
    coefficient_width: int
    coefficient: int
    product_width: int
    shift: int
    term_width: int
    accumulator_width: int
    product_latency: int
    data_signed: bool = True
    coefficient_signed: bool = False
    term_signed: bool = True
    overflow: str = "wrap"


@dataclass(frozen=True)
class FixedPointDspPlan:
    template: str
    operand_signed: bool
    coefficient_signed: bool
    output_lsb: int
    output_width: int
    output_signed: bool
    accumulator_width: int
    product_latency: int


@dataclass(frozen=True)
class RecurrenceCutSpec:
    state_width: int
    transport_valid_transactions: int
    reset_value: int = 0
    arithmetic: str = "modular_add"
    advance_enable: str = "ce"


@dataclass(frozen=True)
class RecurrenceCutPlan:
    template: str
    state_width: int
    transport_valid_transactions: int
    mask_valid_transactions: int
    counter_width: int
    srl_segment_depths: tuple[int, ...]
    reset_mask_value: int = 0
    arithmetic: str = "modular_add"
    advance_enable: str = "ce"
    max_new_dsp_sites: int = 0
    clock_footprint_policy: str = "preexisting_slice_clock_leaves_only"


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _stable(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_stable(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _digest(value: Any) -> str:
    encoded = json.dumps(
        _stable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _require_int(name: str, value: int, low: int, high: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise OperatorProofError(f"{name} must be an integer in {low}..{high}")


def _validate_fixed_point(spec: FixedPointProductSpec, plan: FixedPointDspPlan) -> None:
    for name, value in (
        ("data_width", spec.data_width),
        ("coefficient_width", spec.coefficient_width),
        ("product_width", spec.product_width),
        ("term_width", spec.term_width),
        ("accumulator_width", spec.accumulator_width),
    ):
        _require_int(name, value, 1, 256)
    _require_int("shift", spec.shift, 0, 255)
    _require_int("product_latency", spec.product_latency, 0, 8)
    if spec.product_width < spec.data_width + spec.coefficient_width:
        raise OperatorProofError("product_width cannot represent the full product")
    if spec.shift + spec.term_width > spec.product_width:
        raise OperatorProofError("shifted term exceeds product width")
    if not 0 <= spec.coefficient < (1 << spec.coefficient_width):
        raise OperatorProofError("coefficient does not fit coefficient_width")
    if spec.overflow != "wrap":
        raise OperatorProofError("only explicit modular wrap is supported")
    if plan.template != "DSP48E2_MUL_SHIFT_ACCUM_V1":
        raise OperatorProofError("unsupported DSP rewrite template")
    if plan.output_width < 1 or plan.output_lsb < 0:
        raise OperatorProofError("DSP output slice is invalid")
    if plan.output_lsb + plan.output_width > spec.product_width:
        raise OperatorProofError("DSP output slice exceeds product width")
    if plan.accumulator_width < 1 or plan.accumulator_width > 256:
        raise OperatorProofError("DSP accumulator width is invalid")


def _extend(z3, value, source_width: int, target_width: int, signed: bool):
    if target_width < source_width:
        return z3.Extract(target_width - 1, 0, value)
    if target_width == source_width:
        return value
    extension = target_width - source_width
    return z3.SignExt(extension, value) if signed else z3.ZeroExt(extension, value)


def _solver_obligation(name: str, mismatch, timeout_ms: int) -> dict[str, Any]:
    import z3

    solver = z3.Solver()
    solver.set(timeout=timeout_ms)
    solver.add(mismatch)
    result = solver.check()
    if result == z3.unsat:
        return {"name": name, "result": "unsat"}
    if result == z3.unknown:
        raise OperatorProofError(f"{name} proof returned unknown: {solver.reason_unknown()}")
    model = solver.model()
    witness = {
        str(declaration): str(model[declaration])
        for declaration in model.decls()[:16]
    }
    return {"name": name, "result": "sat", "witness": witness}


def _resize_term(z3, value, width: int, target: int, signed: bool):
    return _extend(z3, value, width, target, signed)


def prove_fixed_point_rewrite(
    spec: FixedPointProductSpec,
    plan: FixedPointDspPlan,
    *,
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    """Prove exact product slicing, signedness, latency, and modular accumulation."""
    import z3

    _validate_fixed_point(spec, plan)
    _require_int("timeout_ms", timeout_ms, 1, 120000)
    preconditions = {
        "latency_equal": plan.product_latency == spec.product_latency,
        "accumulator_width_equal": plan.accumulator_width == spec.accumulator_width,
    }
    if not all(preconditions.values()):
        return {
            "status": "failed",
            "semantics_version": SEMANTICS_VERSION,
            "preconditions": preconditions,
            "obligations": [],
        }

    data = z3.BitVec("fixed_data", spec.data_width)
    accumulator = z3.BitVec("fixed_accumulator", spec.accumulator_width)
    coefficient = z3.BitVecVal(spec.coefficient, spec.coefficient_width)

    ref_data = _extend(
        z3, data, spec.data_width, spec.product_width, spec.data_signed
    )
    ref_coefficient = _extend(
        z3, coefficient, spec.coefficient_width, spec.product_width,
        spec.coefficient_signed,
    )
    ref_product = ref_data * ref_coefficient
    ref_term = z3.Extract(
        spec.shift + spec.term_width - 1, spec.shift, ref_product
    )
    ref_acc = accumulator + _resize_term(
        z3, ref_term, spec.term_width, spec.accumulator_width, spec.term_signed
    )

    candidate_data = _extend(
        z3, data, spec.data_width, spec.product_width, plan.operand_signed
    )
    candidate_coefficient = _extend(
        z3, coefficient, spec.coefficient_width, spec.product_width,
        plan.coefficient_signed,
    )
    candidate_product = candidate_data * candidate_coefficient
    candidate_term = z3.Extract(
        plan.output_lsb + plan.output_width - 1,
        plan.output_lsb,
        candidate_product,
    )
    candidate_acc = accumulator + _resize_term(
        z3, candidate_term, plan.output_width, plan.accumulator_width,
        plan.output_signed,
    )

    obligation = _solver_obligation(
        "fixed_point_product_slice_and_wrap", ref_acc != candidate_acc, timeout_ms
    )
    status = "proved" if obligation["result"] == "unsat" else "failed"
    return {
        "status": status,
        "semantics_version": SEMANTICS_VERSION,
        "preconditions": preconditions,
        "obligations": [obligation],
    }


def _validate_recurrence(spec: RecurrenceCutSpec, plan: RecurrenceCutPlan) -> None:
    _require_int("state_width", spec.state_width, 1, 256)
    _require_int(
        "transport_valid_transactions", spec.transport_valid_transactions, 1, 256
    )
    if spec.reset_value != 0:
        raise OperatorProofError("only zero-reset recurrence transport is supported")
    if spec.arithmetic != "modular_add" or spec.advance_enable != "ce":
        raise OperatorProofError("unsupported recurrence arithmetic or advance control")
    if plan.template != "DSP_PREG_SRL_RECURRENCE_CUT_V1":
        raise OperatorProofError("unsupported recurrence-cut template")
    _require_int("plan.state_width", plan.state_width, 1, 256)
    _require_int(
        "plan.transport_valid_transactions",
        plan.transport_valid_transactions,
        1,
        256,
    )
    _require_int(
        "mask_valid_transactions", plan.mask_valid_transactions, 1, 256
    )
    if not 0 <= plan.reset_mask_value < (1 << plan.state_width):
        raise OperatorProofError("reset_mask_value does not fit state_width")
    if not isinstance(plan.srl_segment_depths, tuple) or not plan.srl_segment_depths:
        raise OperatorProofError("srl_segment_depths must be a nonempty tuple")
    if any(
        isinstance(depth, bool) or not isinstance(depth, int) or not 1 <= depth <= 32
        for depth in plan.srl_segment_depths
    ):
        raise OperatorProofError("each SRL segment depth must be 1..32")
    if sum(plan.srl_segment_depths) != plan.transport_valid_transactions:
        raise OperatorProofError("SRL segment depths do not match transport delay")
    _require_int("counter_width", plan.counter_width, 1, 16)
    if (1 << plan.counter_width) - 1 < plan.mask_valid_transactions:
        raise OperatorProofError("counter cannot represent the mask threshold")


def _recurrence_relation(z3, reference, candidate, age: int, depth: int):
    facts = []
    zero = z3.BitVecVal(0, reference[0].size())
    for index in range(depth):
        if index < age:
            facts.append(reference[index] == candidate[index])
        else:
            facts.append(reference[index] == zero)
    return z3.And(*facts)


def prove_recurrence_cut_contract(
    spec: RecurrenceCutSpec,
    plan: RecurrenceCutPlan,
    *,
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    """Prove reset masking, CE-qualified transport, and modular reassociation."""
    import z3

    _validate_recurrence(spec, plan)
    _require_int("timeout_ms", timeout_ms, 1, 120000)
    depth = spec.transport_valid_transactions
    preconditions = {
        "state_width_equal": plan.state_width == spec.state_width,
        "transport_delay_equal": plan.transport_valid_transactions == depth,
        "advance_enable_equal": plan.advance_enable == spec.advance_enable,
        "arithmetic_equal": plan.arithmetic == spec.arithmetic,
        "no_new_dsp_clock_sites": plan.max_new_dsp_sites == 0,
        "preexisting_slice_clock_leaves_only": (
            plan.clock_footprint_policy == "preexisting_slice_clock_leaves_only"
        ),
    }
    if not all(preconditions.values()):
        return {
            "status": "failed",
            "semantics_version": SEMANTICS_VERSION,
            "preconditions": preconditions,
            "obligations": [],
        }

    obligations = []
    width = spec.state_width
    for age in range(depth + 1):
        reference = [z3.BitVec(f"ref_{age}_{index}", width) for index in range(depth)]
        candidate = [z3.BitVec(f"cand_{age}_{index}", width) for index in range(depth)]
        relation = _recurrence_relation(z3, reference, candidate, age, depth)
        reference_output = reference[-1]
        candidate_output = (
            candidate[-1]
            if age >= plan.mask_valid_transactions
            else z3.BitVecVal(plan.reset_mask_value, width)
        )
        obligations.append(_solver_obligation(
            f"transport_output_age_{age}",
            z3.And(relation, reference_output != candidate_output),
            timeout_ms,
        ))

        sample = z3.BitVec(f"sample_{age}", width)
        next_age = min(depth, age + 1)
        reference_next = [sample, *reference[:-1]]
        candidate_next = [sample, *candidate[:-1]]
        next_relation = _recurrence_relation(
            z3, reference_next, candidate_next, next_age, depth
        )
        obligations.append(_solver_obligation(
            f"transport_ce_step_age_{age}",
            z3.And(relation, z3.Not(next_relation)),
            timeout_ms,
        ))

    state = z3.BitVec("recurrence_state", width)
    contribution_a = z3.BitVec("recurrence_contribution_a", width)
    contribution_b = z3.BitVec("recurrence_contribution_b", width)
    reference_sum = state + contribution_a + contribution_b
    candidate_sum = (contribution_a + contribution_b) + state
    obligations.append(_solver_obligation(
        "modular_reassociation",
        reference_sum != candidate_sum,
        timeout_ms,
    ))
    status = (
        "proved" if all(item["result"] == "unsat" for item in obligations)
        else "failed"
    )
    return {
        "status": status,
        "semantics_version": SEMANTICS_VERSION,
        "preconditions": preconditions,
        "obligations": obligations,
        "derived_facts": {
            "srl_cells": spec.state_width * len(plan.srl_segment_depths),
            "counter_capacity": (1 << plan.counter_width) - 1,
        },
    }


def _validate_identity(value: str, field: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise OperatorProofError(f"{field} must be a lowercase SHA-256 digest")


def _build_authorization(
    rewrite_kind: str,
    source_dcp_sha256: str,
    source_region_sha256: str,
    spec: Any,
    plan: Any,
    proof: dict[str, Any],
) -> dict[str, Any]:
    _validate_identity(source_dcp_sha256, "source_dcp_sha256")
    _validate_identity(source_region_sha256, "source_region_sha256")
    if proof.get("status") != "proved":
        raise OperatorProofError("a failed proof cannot authorize a rewrite")
    certificate = {
        "kind": "operator_rewrite_authorization",
        "version": AUTHORIZATION_VERSION,
        "rewrite_kind": rewrite_kind,
        "source": {
            "dcp_sha256": source_dcp_sha256,
            "region_sha256": source_region_sha256,
        },
        "spec": asdict(spec),
        "plan": asdict(plan),
        "proof": proof,
        "authority": "pre_mutation_only",
    }
    certificate["certificate_id"] = _digest(certificate)
    return certificate


def build_fixed_point_authorization(
    source_dcp_sha256: str,
    source_region_sha256: str,
    spec: FixedPointProductSpec,
    plan: FixedPointDspPlan,
    *,
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    proof = prove_fixed_point_rewrite(spec, plan, timeout_ms=timeout_ms)
    return _build_authorization(
        "fabric_to_dsp", source_dcp_sha256, source_region_sha256,
        spec, plan, proof,
    )


def build_fixed_point_contract_authorization(
    contract: dict[str, Any],
) -> dict[str, Any]:
    """Bind a current-cone contract to the fixed DSP measurement backend.

    The embedded contract is intentionally only measurement-probe authority.
    Its deterministic source screen does not claim formal whole-DCP
    equivalence; the independent final validator remains mandatory.
    """
    from .fixed_point_dsp import build_topology_manifest, verify_topology_math

    if not isinstance(contract, dict):
        raise OperatorProofError("fixed-point source contract must be an object")
    claimed = contract.get("certificate_id")
    payload = dict(contract)
    payload.pop("certificate_id", None)
    source = contract.get("source_identity") or {}
    topology = contract.get("topology_manifest") or {}
    if (
        contract.get("kind") != "register_cone_fixed_point_contract"
        or contract.get("status") != "screened_for_measurement_probe"
        or contract.get("authority") != "pre_mutation_measurement_probe_only"
        or contract.get("mutation_ready_for_measurement_probe") is not True
        or contract.get("topology_math_proved") is not True
        or not isinstance(claimed, str)
        or _digest(payload) != claimed
        or build_topology_manifest(contract) != topology
        or not verify_topology_math(contract)
    ):
        raise OperatorProofError("fixed-point source contract did not replay")
    source_dcp_sha256 = source.get("dcp_sha256")
    source_region_sha256 = source.get("connectivity_sha256")
    _validate_identity(source_dcp_sha256, "fixed-point source DCP")
    _validate_identity(source_region_sha256, "fixed-point source region")
    spec = {
        "source_contract": json.loads(json.dumps(contract, sort_keys=True)),
        "source_contract_id": claimed,
        "output_width": contract.get("output_width"),
        "term_count": len(contract.get("terms") or []),
    }
    plan = {
        "template": "DSP48E2_MUL_SHIFT_ACCUM_V1",
        "topology_manifest_sha256": topology.get("manifest_sha256"),
        "target_registers_preserved": True,
        "internal_pipeline_registers": 0,
    }
    proof = {
        "status": "proved_for_measurement_probe",
        "abstract_dsp_chunk_math": True,
        "source_contract_screen": "deterministic_current_cone_replay_required",
        "formal_whole_dcp_equivalence": False,
        "whole_dcp_equivalence_required_before_submission": True,
    }
    certificate = {
        "kind": "operator_rewrite_authorization",
        "version": AUTHORIZATION_VERSION,
        "rewrite_kind": "fabric_to_dsp",
        "source": {
            "dcp_sha256": source_dcp_sha256,
            "region_sha256": source_region_sha256,
        },
        "spec": spec,
        "plan": plan,
        "proof": proof,
        "authority": "pre_mutation_only",
    }
    certificate["certificate_id"] = _digest(certificate)
    return certificate


def build_registered_product_authorization(
    source_dcp_sha256: str,
    bundle: dict[str, Any],
) -> dict[str, Any]:
    """Bind an internally recovered registered closure to measurement authority."""
    from .registered_bridge import (
        validate_registered_bridge_bundle,
        validate_registered_product_bundle,
    )

    _validate_identity(source_dcp_sha256, "registered product source DCP")
    is_generating_bridge = (
        isinstance(bundle, dict)
        and bundle.get("kind") == "generating_operator_bridge_bundle"
    )
    if is_generating_bridge:
        recovery = bundle.get("recovery") or {}
        bank_count = recovery.get("bank_count", 1)
        expected_backend_input = (
            "generating_operator_ir_multi_bank"
            if isinstance(bank_count, int) and not isinstance(bank_count, bool)
            and bank_count > 1
            else "generating_operator_ir"
        )
        if (
            recovery.get("backend_input") != expected_backend_input
            or recovery.get("external_plan_used") is not False
            or recovery.get("operator_class") not in {
                "separately_quantized_constant_product_sum",
                "registered_linear_sum",
            }
        ):
            raise OperatorProofError(
                "generating operator bridge recovery authority is incomplete"
            )
        checked = validate_registered_bridge_bundle(bundle)
    else:
        checked = validate_registered_product_bundle(bundle)
    region_sha256 = _digest({"source": source_dcp_sha256, "bundle": checked})
    plans = []
    for plan in checked["plans"]:
        if is_generating_bridge:
            plan_summary = {
                "schedule_kind": "generating_operator_bridge_v2",
                "data_width": plan["data_width"],
                "pipeline_depth": 4,
                "input_roles": list(plan["input_roles"]),
                "input_widths": list(plan["input_widths"]),
                "input_signed": list(plan["input_signed"]),
                "terms": [dict(term) for term in plan["terms"]],
                "groups": [dict(group) for group in plan["groups"]],
                "controls": dict(plan.get("controls") or {}),
                "output_integration": plan["output_integration"],
            }
            if plan["output_integration"] == "replace_downstream_shell_v1":
                plan_summary["output_gate_polarity"] = plan[
                    "output_gate_polarity"
                ]
            elif (
                plan["output_integration"]
                == "replace_observable_shell_truth_table_v1"
            ):
                plan_summary.update({
                    "shell_control_roles": list(
                        plan["shell_control_roles"]
                    ),
                    "shell_has_alternate_data": bool(
                        plan["shell_has_alternate_data"]
                    ),
                    "shell_truth_tables": list(plan["shell_truth_tables"]),
                })
            else:
                plan_summary.update({
                    "control_roles": list(plan["control_roles"]),
                    "control_truth_table_includes_data_bit": bool(
                        plan.get(
                            "control_truth_table_includes_data_bit", False
                        )
                    ),
                    "control_truth_tables": list(
                        plan["control_truth_tables"]
                    ),
                    "control_preserved_data_bits": list(
                        plan.get(
                            "control_preserved_data_bits",
                            [False] * int(plan["data_width"]),
                        )
                    ),
                })
            plans.append(plan_summary)
            continue
        if plan["schedule_kind"] == "registered_branch_state_dag_v1":
            plans.append({
                "schedule_kind": plan["schedule_kind"],
                "data_width": plan["data_width"],
                "pipeline_depth": plan["pipeline_depth"],
                "clock_edge": plan["clock_edge"],
                "input_widths": [item["width"] for item in plan["inputs"]],
                "state_widths": [item["width"] for item in plan["states"]],
                "state_reset_kinds": [
                    item["reset_kind"] for item in plan["states"]
                ],
                "output_reset_kind": plan["output_state"]["reset_kind"],
                "quantization_group_count": len(plan["output_state"]["groups"]),
            })
        else:
            plans.append({
                "schedule_kind": plan["schedule_kind"],
                "data_width": plan["data_width"],
                "coefficient_width": plan["coefficient_width"],
                "product_shift": plan["product_shift"],
                "pipeline_depth": plan["pipeline_depth"],
                "coefficients": list(plan["coefficients"]),
                "input_signed": list(plan["input_signed"]),
                "clock_edge": plan["clock_edge"],
                "product_reset_kinds": list(plan["product_reset_kinds"]),
                "tail_reset_kinds": list(plan["tail_reset_kinds"]),
            })
    uniform_frontier = not is_generating_bridge and all(
        plan["schedule_kind"] == "registered_product_sum_v1"
        for plan in checked["plans"]
    )
    proof = {
        "status": "proved_for_measurement_probe",
        "composed_boolean_replay": True,
        "uniform_registered_frontier": uniform_frontier,
        "schedule_controls_recovered": True,
        "external_plan_used": False,
        "formal_whole_dcp_equivalence": False,
        "whole_dcp_equivalence_required_before_submission": True,
    }
    certificate = {
        "kind": "operator_rewrite_authorization",
        "version": AUTHORIZATION_VERSION,
        "rewrite_kind": "fabric_to_dsp",
        "source": {
            "dcp_sha256": source_dcp_sha256,
            "region_sha256": region_sha256,
        },
        "spec": {
            "registered_product_closure": True,
            "bundle_sha256": _digest(checked),
            "module_count": len(checked["plans"]),
            "instance_count": len(checked["instances"]),
            "plans": plans,
        },
        "plan": {
            "template": "DSP48E2_MUL_SHIFT_ACCUM_V1",
            "target_registers_preserved": False,
            "registered_schedule_rebuilt": True,
            "bundle_sha256": _digest(checked),
        },
        "proof": proof,
        "authority": "pre_mutation_only",
    }
    certificate["certificate_id"] = _digest(certificate)
    return certificate


def build_consumer_rewrite_authorization(
    source_dcp_sha256: str,
    contract_sha256: str,
    bundle: dict[str, Any],
) -> dict[str, Any]:
    """Bind a freshly recovered consumer contract to measurement authority."""
    from .consumer_rewrite import validate_consumer_rewrite_bundle

    _validate_identity(source_dcp_sha256, "consumer rewrite source DCP")
    _validate_identity(contract_sha256, "consumer rewrite contract")
    checked = validate_consumer_rewrite_bundle(bundle)
    plans = []
    for plan in checked["plans"]:
        pipeline_depth = plan.get("pipeline_depth")
        if pipeline_depth is None:
            # Composite consumer operators retain their component schedule in
            # the validated plan instead of flattening it into one field, and a
            # combinational one carries no schedule at all: the signed wide
            # product with a boolean shell publishes neither a pipeline depth
            # nor sqrt and division components.  Reading the reciprocal-sqrt
            # pair unconditionally raised KeyError on the first plan without
            # them, which is how vtr_mcml_v2 lost the only mechanism that
            # reaches its wall -- selected on round 1, dead 0.14s after the
            # checkpoint opened, with `FAILED: 'sqrt_pipeline_depth'` as the
            # entire record.  Sum the components the plan does carry, so a
            # composite stays exact and a combinational operator is zero.
            pipeline_depth = sum(
                int(plan[key])
                for key in ("sqrt_pipeline_depth", "division_pipeline_depth")
                if plan.get(key) is not None
            )
        plans.append({
            "operator": plan["operator"],
            "input_width": plan["input_width"],
            "output_width": plan["output_width"],
            "pipeline_depth": int(pipeline_depth),
            "control_protocol": dict(plan["control_protocol"]),
        })
    region_sha256 = _digest({
        "source": source_dcp_sha256,
        "contract": contract_sha256,
        "bundle": checked,
    })
    proof = {
        "status": "proved_for_measurement_probe",
        "live_consumer_boundary_recovered": True,
        "registered_schedule_recovered": True,
        "object_identity_private": True,
        "external_plan_used": False,
        "formal_whole_dcp_equivalence": False,
        "whole_dcp_equivalence_required_before_submission": True,
    }
    certificate = {
        "kind": "operator_rewrite_authorization",
        "version": AUTHORIZATION_VERSION,
        "rewrite_kind": "consumer_driven_arithmetic_rewrite",
        "source": {
            "dcp_sha256": source_dcp_sha256,
            "region_sha256": region_sha256,
        },
        "spec": {
            "consumer_contract": True,
            "contract_sha256": contract_sha256,
            "bundle_sha256": _digest(checked),
            "module_count": len(checked["plans"]),
            "instance_count": len(checked["instances"]),
            "plans": plans,
        },
        "plan": {
            "template": "CONSUMER_DRIVEN_ARITHMETIC_REWRITE_V1",
            "registered_schedule_rebuilt": True,
            "bundle_sha256": _digest(checked),
        },
        "proof": proof,
        "authority": "pre_mutation_only",
    }
    certificate["certificate_id"] = _digest(certificate)
    return certificate


def build_registered_product_emitted_attestation(
    authorization: dict[str, Any],
    candidate_dcp_sha256: str,
    route_facts: dict[str, Any],
    *,
    template: str = "fixed_point_dsp_rebuild_v1",
) -> dict[str, Any]:
    """Build the fixed field-set expected by measurement admission."""
    _validate_identity(candidate_dcp_sha256, "registered product candidate DCP")
    source = authorization.get("source") or {}
    _validate_identity(source.get("dcp_sha256"), "registered product source DCP")
    _validate_identity(source.get("region_sha256"), "registered product source region")
    if (
        route_facts.get("candidate_checkpoint_reopened") is not True
        or route_facts.get("source_checkpoint_reopened") is not True
        or route_facts.get("relevant_nets_fully_routed") is not True
    ):
        raise OperatorProofError("registered product route facts are incomplete")
    if template == "consumer_driven_registered_arithmetic_rewrite_v1":
        checks = {
            "consumer_boundary_topology_replayed": True,
            "source_contract_replayed": True,
            "registered_schedule_replayed": True,
            "exact_namespace_inventory": True,
            "candidate_checkpoint_reopened": True,
            "source_checkpoint_reopened": True,
            "relevant_nets_fully_routed": True,
        }
    else:
        checks = {
            "fixed_point_dsp_topology_replayed": True,
            "source_contract_replayed": True,
            "target_register_contract_replayed": True,
            "exact_namespace_inventory": True,
            "candidate_checkpoint_reopened": True,
            "source_checkpoint_reopened": True,
            "relevant_nets_fully_routed": True,
        }
    payload = {
        "kind": "operator_rewrite_emitted_topology_attestation",
        "version": 1,
        "status": "proved",
        "authority": "candidate_measurement_only",
        "template": template,
        "source": {
            "dcp_sha256": source["dcp_sha256"],
            "region_sha256": source["region_sha256"],
        },
        "candidate": {"dcp_sha256": candidate_dcp_sha256},
        "authorization_id": authorization.get("certificate_id"),
        "placement_manifest_sha256": _digest({
            "profile": route_facts.get("profile"),
            "instances": route_facts.get("instance_count"),
            "modules": route_facts.get("module_count"),
        }),
        "topology_manifest_sha256": (authorization.get("spec") or {}).get(
            "bundle_sha256"
        ),
        "checks": checks,
    }
    payload["attestation_id"] = _digest(payload)
    return payload


def build_banked_predicate_authorization(
    source_dcp_sha256: str,
    contract: dict[str, Any],
) -> dict[str, Any]:
    """Bind one fresh anonymous data/control contract to measurement authority."""
    _validate_identity(source_dcp_sha256, "banked predicate source DCP")
    if not isinstance(contract, dict):
        raise OperatorProofError("banked predicate contract must be an object")
    data = contract.get("data_operator") or {}
    control = contract.get("control_operator") or {}
    recovery = contract.get("recovery") or {}
    if (
        contract.get("operator_class") != "BANKED_SELECT_PREDICATE_UPDATE"
        or data.get("source") != "banked_registered_ram_words"
        or data.get("global_operation") != "select_then_word_predicate"
        or data.get("specialized_operation")
        != "bank_word_predicate_before_select"
        or not isinstance(data.get("word_width"), int)
        or data["word_width"] < 1
        or not isinstance(control.get("bank_count"), int)
        or control["bank_count"] < 2
        or control.get("local_write_predicate_truth_table_preserved") is not True
        or control.get("alternate_and_init_cofactors_preserved") is not True
        or control.get("global_predicate_consumer_preserved") is not True
        or control.get("clock_ce_reset_state_unchanged") is not True
        or recovery.get("every_selected_input_has_one_bank_local_source") is not True
        or not isinstance(recovery.get("comparator_lut_count"), int)
        or recovery["comparator_lut_count"] < 1
        or not isinstance(recovery.get("selected_word_input_count"), int)
        or recovery["selected_word_input_count"] < 1
    ):
        raise OperatorProofError("banked predicate data/control contract is incomplete")
    # Bind only the state-independent semantic contract.  Live timing-wall
    # telemetry remains useful decision/audit evidence, but it is neither a
    # mathematical rewrite obligation nor stable emitted-topology authority.
    # Keeping it here also lets innocent fields such as ``covers_worst_path``
    # collide with the candidate-proof path-key guard.
    checked = {
        "operator_class": "BANKED_SELECT_PREDICATE_UPDATE",
        "data_operator": {
            key: data.get(key)
            for key in (
                "source", "word_width", "global_operation",
                "specialized_operation",
            )
        },
        "control_operator": {
            key: control.get(key)
            for key in (
                "bank_count",
                "local_write_predicate_truth_table_preserved",
                "alternate_and_init_cofactors_preserved",
                "global_predicate_consumer_preserved",
                "clock_ce_reset_state_unchanged",
            )
        },
        "recovery": {
            key: recovery.get(key)
            for key in (
                "comparator_lut_count", "selected_word_input_count",
                "registered_source_count",
                "every_selected_input_has_one_bank_local_source",
            )
        },
    }
    checked = json.loads(json.dumps(checked, sort_keys=True, allow_nan=False))
    region_sha256 = _digest(checked)
    proof = {
        "status": "proved_for_measurement_probe",
        "registered_bank_sources_recovered": True,
        "selected_word_mapping_recovered": True,
        "local_truth_table_cofactors_preserved": True,
        "global_control_consumer_preserved": True,
        "clock_ce_reset_state_unchanged": True,
        "formal_whole_dcp_equivalence": False,
        "whole_dcp_equivalence_required_before_submission": True,
    }
    authorization = {
        "kind": "operator_rewrite_authorization",
        "version": AUTHORIZATION_VERSION,
        "rewrite_kind": "banked_select_predicate_specialization",
        "source": {
            "dcp_sha256": source_dcp_sha256,
            "region_sha256": region_sha256,
        },
        "spec": {
            "contract": checked,
            "contract_sha256": region_sha256,
        },
        "plan": {
            "template": "BANKED_SELECT_PREDICATE_UPDATE_V1",
            "replace_bank_local_predicate_only": True,
            "preserve_global_predicate": True,
            "preserve_registered_state_and_control": True,
        },
        "proof": proof,
        "authority": "pre_mutation_only",
    }
    authorization["certificate_id"] = _digest(authorization)
    return authorization


def build_banked_predicate_emitted_attestation(
    authorization: dict[str, Any],
    candidate_dcp_sha256: str,
    route_facts: dict[str, Any],
) -> dict[str, Any]:
    """Bind the emitted local predicates and reopened routed DCP."""
    _validate_identity(candidate_dcp_sha256, "banked predicate candidate DCP")
    source = authorization.get("source") or {}
    spec = authorization.get("spec") or {}
    _validate_identity(source.get("dcp_sha256"), "banked predicate source DCP")
    _validate_identity(source.get("region_sha256"), "banked predicate source region")
    if (
        route_facts.get("candidate_checkpoint_reopened") is not True
        or route_facts.get("source_checkpoint_reopened") is not True
        or route_facts.get("relevant_nets_fully_routed") is not True
        or route_facts.get("contract") != "BANKED_SELECT_PREDICATE_UPDATE"
        or route_facts.get("backend")
        != "bank_local_predicate_specialization_v1"
        or route_facts.get("route_errors") != 0
    ):
        raise OperatorProofError("banked predicate route facts are incomplete")
    checks = {
        "banked_predicate_topology_replayed": True,
        "registered_bank_sources_replayed": True,
        "local_truth_table_cofactors_preserved": True,
        "global_consumer_preserved": True,
        "exact_namespace_inventory": True,
        "candidate_checkpoint_reopened": True,
        "source_checkpoint_reopened": True,
        "relevant_nets_fully_routed": True,
    }
    payload = {
        "kind": "operator_rewrite_emitted_topology_attestation",
        "version": 1,
        "status": "proved",
        "authority": "candidate_measurement_only",
        "template": "bank_local_predicate_specialization_v1",
        "source": {
            "dcp_sha256": source["dcp_sha256"],
            "region_sha256": source["region_sha256"],
        },
        "candidate": {"dcp_sha256": candidate_dcp_sha256},
        "authorization_id": authorization.get("certificate_id"),
        "placement_manifest_sha256": _digest({
            "profile": "semantic_rewrite",
            "bank_count": route_facts.get("bank_count"),
            "word_width": route_facts.get("word_width"),
        }),
        "topology_manifest_sha256": spec.get("contract_sha256"),
        "checks": checks,
    }
    payload["attestation_id"] = _digest(payload)
    return payload


def build_registered_dsp_lane_authorization(
    source_dcp_sha256: str,
    contract: dict[str, Any],
) -> dict[str, Any]:
    """Bind a complete anonymous DSP-product sequential transaction."""
    _validate_identity(source_dcp_sha256, "registered DSP lane source DCP")
    if not isinstance(contract, dict):
        raise OperatorProofError("registered DSP lane contract must be an object")
    lane_count = contract.get("lane_count")
    word_width = contract.get("word_width")
    removed = contract.get("removed_external_ff_count")
    retained = contract.get("retained_reset_history_ff_count")
    already_clocked = contract.get(
        "selected_dsp_already_on_recovered_clock_count"
    )
    newly_activated = contract.get("newly_activated_dsp_clock_site_count")
    if (
        contract.get("operator_class") != "REGISTERED_DSP_PRODUCT_TRANSACTION"
        or not isinstance(lane_count, int) or lane_count < 1
        or not isinstance(word_width, int) or word_width < 2
        or not isinstance(removed, int) or removed < 1
        or not isinstance(retained, int) or retained < 0
        or removed + retained != lane_count * word_width
        or contract.get("complete_p_to_ff_mapping_recovered") is not True
        or contract.get("clock_ce_reset_word_recovered") is not True
        or contract.get("pcout_and_side_consumers_closed") is not True
        or contract.get("observable_cycle_preserved") is not True
        or not isinstance(contract.get("clock_edge_inverted"), bool)
        or not isinstance(already_clocked, int) or already_clocked < 0
        or not isinstance(newly_activated, int) or newly_activated < 0
        or already_clocked + newly_activated != lane_count
        or contract.get("new_clocked_dsp_count") != newly_activated
        or not isinstance(contract.get("clock_region_count"), int)
        or contract["clock_region_count"] < 1
    ):
        raise OperatorProofError("registered DSP lane data/control closure is incomplete")
    sibling_present = contract.get("sibling_present")
    sibling_rank = contract.get("sibling_rank")
    sibling_slack = contract.get("sibling_slack_ns")
    if (
        not isinstance(sibling_present, bool)
        or not isinstance(sibling_rank, int)
        or sibling_rank < 0
        or sibling_present != (sibling_rank > 0)
        or (sibling_present and not isinstance(sibling_slack, (int, float)))
        or (not sibling_present and sibling_slack is not None)
    ):
        raise OperatorProofError("registered DSP lane sibling evidence is incomplete")
    checked = json.loads(json.dumps(contract, sort_keys=True, allow_nan=False))
    region_sha256 = _digest(checked)
    authorization = {
        "kind": "operator_rewrite_authorization",
        "version": AUTHORIZATION_VERSION,
        "rewrite_kind": "low_fanout_registered_dsp_lane_rebuild",
        "source": {
            "dcp_sha256": source_dcp_sha256,
            "region_sha256": region_sha256,
        },
        "spec": {
            "contract": checked,
            "contract_sha256": region_sha256,
        },
        "plan": {
            "template": "REGISTERED_DSP_LANE_BOUNDARY_V1",
            "enable_existing_dsp_preg_only": True,
            "replace_complete_external_product_bank": True,
            "preserve_clock_ce_reset_word_and_cycle": True,
        },
        "proof": {
            "status": "proved_for_measurement_probe",
            "complete_p_to_ff_word_mapping": True,
            "clock_ce_reset_word_replayed": True,
            "pcout_and_side_consumers_closed": True,
            "observable_cycle_preserved": True,
            "formal_whole_dcp_equivalence": False,
            "whole_dcp_equivalence_required_before_submission": True,
        },
        "authority": "pre_mutation_only",
    }
    authorization["certificate_id"] = _digest(authorization)
    return authorization


def build_registered_dsp_lane_emitted_attestation(
    authorization: dict[str, Any],
    candidate_dcp_sha256: str,
    route_facts: dict[str, Any],
) -> dict[str, Any]:
    """Bind the reopened PREG candidate and exact resource accounting."""
    _validate_identity(candidate_dcp_sha256, "registered DSP lane candidate DCP")
    source = authorization.get("source") or {}
    spec = authorization.get("spec") or {}
    authorized_contract = spec.get("contract") or {}
    _validate_identity(source.get("dcp_sha256"), "registered DSP lane source DCP")
    _validate_identity(source.get("region_sha256"), "registered DSP lane source region")
    if (
        route_facts.get("candidate_checkpoint_reopened") is not True
        or route_facts.get("source_checkpoint_reopened") is not True
        or route_facts.get("relevant_nets_fully_routed") is not True
        or route_facts.get("registered_control_transaction_preserved") is not True
        or route_facts.get("primitive_replay_passed") is not True
        or route_facts.get("clock_identity_preserved") is not True
        or route_facts.get("contract")
        != "EXTERNAL_PRODUCT_REGISTER_TO_DSP_PREG_WITH_RESET_HISTORY_CORRECTION"
        or route_facts.get("backend") != "registered_dsp_lane_rebuild_v1"
        or route_facts.get("route_errors") != 0
        or any(
            route_facts.get(key) != authorized_contract.get(key)
            for key in (
                "lane_count", "word_width", "new_clocked_dsp_count",
                "selected_dsp_already_on_recovered_clock_count",
                "newly_activated_dsp_clock_site_count", "clock_region_count",
            )
        )
    ):
        raise OperatorProofError("registered DSP lane route facts are incomplete")
    checks = {
        "complete_p_to_ff_word_mapping_replayed": True,
        "preg_reset_history_topology_replayed": True,
        "registered_control_transaction_preserved": True,
        "architectural_latency_preserved": True,
        "exact_resource_accounting": True,
        "candidate_checkpoint_reopened": True,
        "source_checkpoint_reopened": True,
        "relevant_nets_fully_routed": True,
    }
    payload = {
        "kind": "operator_rewrite_emitted_topology_attestation",
        "version": 1,
        "status": "proved",
        "authority": "candidate_measurement_only",
        "template": "registered_dsp_lane_rebuild_v1",
        "source": {
            "dcp_sha256": source["dcp_sha256"],
            "region_sha256": source["region_sha256"],
        },
        "candidate": {"dcp_sha256": candidate_dcp_sha256},
        "authorization_id": authorization.get("certificate_id"),
        "placement_manifest_sha256": _digest({
            "lane_count": route_facts.get("lane_count"),
            "word_width": route_facts.get("word_width"),
            "clock_region_count": route_facts.get("clock_region_count"),
        }),
        "topology_manifest_sha256": spec.get("contract_sha256"),
        "checks": checks,
    }
    payload["attestation_id"] = _digest(payload)
    return payload


def build_registered_dead_state_authorization(
    source_dcp_sha256: str,
    contract: dict[str, Any],
) -> dict[str, Any]:
    """Bind one output-anchored full primitive closure to its private delete set."""
    _validate_identity(source_dcp_sha256, "registered dead-state source DCP")
    if not isinstance(contract, dict):
        raise OperatorProofError("registered dead-state contract must be an object")
    checked = json.loads(json.dumps(contract, sort_keys=True, allow_nan=False))
    checked.pop("runtime_s", None)
    summary = checked.get("summary") or {}
    proof = checked.get("proof") or {}
    contract_digest = checked.get("contract_digest")
    target_digest = summary.get("mutation_target_digest")
    unsigned = json.loads(json.dumps(checked))
    unsigned.pop("contract_digest", None)
    unsigned.pop("read_only_after_restore", None)
    unsigned.pop("private_mutation_manifest_retained", None)
    if (
        checked.get("status") != "success"
        or checked.get("kind")
        != "registered_observable_dead_state_contract"
        or checked.get("operator_class")
        != "REGISTERED_OBSERVABLE_DEAD_STATE_FAMILY"
        or checked.get("read_only") is not True
        or checked.get("fresh_rediscovery") is not True
        or checked.get("private_mutation_manifest_retained") is not True
        or not isinstance(summary, dict)
        or summary.get("mutation_eligible") is not True
        or not isinstance(summary.get("physical_cell_count"), int)
        or not isinstance(summary.get("observable_physical_cell_count"), int)
        or not isinstance(summary.get("dead_physical_cell_count"), int)
        or summary["observable_physical_cell_count"]
        + summary["dead_physical_cell_count"]
        != summary["physical_cell_count"]
        or not isinstance(summary.get("dead_registered_state_count"), int)
        or summary["dead_registered_state_count"] < 1
        or not isinstance(summary.get("mutation_target_cell_count"), int)
        or summary["mutation_target_cell_count"] < 1
        or not isinstance(summary.get("dead_dsp_count"), int)
        or summary["dead_dsp_count"] < 0
        or re.fullmatch(r"[0-9a-f]{64}", str(contract_digest or "")) is None
        or re.fullmatch(r"[0-9a-f]{64}", str(target_digest or "")) is None
        or _digest(unsigned) != contract_digest
        or not isinstance(proof, dict)
        or any(
            proof.get(key) is not True
            for key in (
                "output_anchored_transitive_primitive_fanin",
                "sequential_data_control_and_clock_inputs_traversed",
                "all_physical_primitives_partitioned",
                "expanded_dsp_components_grouped_by_live_parent",
                "partial_dsp_parents_preserved",
                "exact_dead_cell_names_are_private",
            )
        )
    ):
        raise OperatorProofError("registered dead-state observable closure is incomplete")
    authorization = {
        "kind": "operator_rewrite_authorization",
        "version": AUTHORIZATION_VERSION,
        "rewrite_kind": "registered_observable_dead_state_elimination",
        "source": {
            "dcp_sha256": source_dcp_sha256,
            "region_sha256": contract_digest,
        },
        "spec": {
            "contract": checked,
            "contract_sha256": contract_digest,
            "mutation_target_sha256": target_digest,
        },
        "plan": {
            "template": "REGISTERED_OBSERVABLE_DEAD_STATE_V1",
            "delete_only_private_manifest_targets": True,
            "preserve_complete_output_observable_closure": True,
            "preserve_partial_hard_macro_parents": True,
        },
        "proof": {
            "status": "proved_for_measurement_probe",
            "complete_output_observable_primitive_fanin": True,
            "registered_data_control_and_clock_dependencies_traversed": True,
            "exact_cell_identity_framework_private": True,
            "formal_whole_dcp_equivalence": False,
            "whole_dcp_equivalence_required_before_submission": True,
        },
        "authority": "pre_mutation_only",
    }
    authorization["certificate_id"] = _digest(authorization)
    return authorization


def build_registered_dead_state_emitted_attestation(
    authorization: dict[str, Any],
    candidate_dcp_sha256: str,
    route_facts: dict[str, Any],
) -> dict[str, Any]:
    """Bind the routed delete-only result to the exact private manifest proof."""
    _validate_identity(candidate_dcp_sha256, "registered dead-state candidate DCP")
    source = authorization.get("source") or {}
    spec = authorization.get("spec") or {}
    contract = spec.get("contract") or {}
    summary = contract.get("summary") or {}
    _validate_identity(source.get("dcp_sha256"), "registered dead-state source DCP")
    _validate_identity(source.get("region_sha256"), "registered dead-state source region")
    resources = route_facts.get("resource_delta") or {}
    if (
        route_facts.get("candidate_checkpoint_reopened") is not True
        or route_facts.get("source_checkpoint_reopened") is not True
        or route_facts.get("relevant_nets_fully_routed") is not True
        or route_facts.get("observable_closure_replayed") is not True
        or route_facts.get("private_manifest_identity_replayed") is not True
        or route_facts.get("clock_identity_preserved") is not True
        or route_facts.get("contract")
        != "REGISTERED_OBSERVABLE_CLOSURE_DEAD_STATE_V1"
        or route_facts.get("backend")
        != "registered_dead_state_elimination_v1"
        or route_facts.get("route_errors") != 0
        or route_facts.get("source_dcp_sha256") != source.get("dcp_sha256")
        or route_facts.get("contract_digest") != source.get("region_sha256")
        or route_facts.get("mutation_target_digest")
        != spec.get("mutation_target_sha256")
        or route_facts.get("mutation_target_cell_count")
        != summary.get("mutation_target_cell_count")
        or route_facts.get("selected_dsp_count") != summary.get("dead_dsp_count")
        or not isinstance(resources, dict)
        or any(not isinstance(resources.get(key), int) for key in (
            "dsp", "ff", "lut", "carry", "primitive"
        ))
        or any(resources[key] > 0 for key in ("dsp", "ff", "lut", "carry"))
        or resources["primitive"] >= 0
    ):
        raise OperatorProofError("registered dead-state route facts are incomplete")
    checks = {
        "output_observable_closure_replayed": True,
        "private_manifest_identity_replayed": True,
        "partial_hard_macro_parents_preserved": True,
        "exact_resource_accounting": True,
        "clock_identity_preserved": True,
        "candidate_checkpoint_reopened": True,
        "source_checkpoint_reopened": True,
        "relevant_nets_fully_routed": True,
    }
    payload = {
        "kind": "operator_rewrite_emitted_topology_attestation",
        "version": 1,
        "status": "proved",
        "authority": "candidate_measurement_only",
        "template": "registered_dead_state_elimination_v1",
        "source": {
            "dcp_sha256": source["dcp_sha256"],
            "region_sha256": source["region_sha256"],
        },
        "candidate": {"dcp_sha256": candidate_dcp_sha256},
        "authorization_id": authorization.get("certificate_id"),
        "placement_manifest_sha256": _digest({
            "mutation_target_sha256": spec.get("mutation_target_sha256"),
            "mutation_target_cell_count": route_facts.get(
                "mutation_target_cell_count"
            ),
        }),
        "topology_manifest_sha256": spec.get("contract_sha256"),
        "checks": checks,
    }
    payload["attestation_id"] = _digest(payload)
    return payload


def build_control_state_equation_authorization(
    source_dcp_sha256: str,
    contract: dict[str, Any],
    family_ref: str,
) -> dict[str, Any]:
    """Bind one exhaustive synchronous state equation to its private family."""
    _validate_identity(source_dcp_sha256, "control-state source DCP")
    if not isinstance(contract, dict):
        raise OperatorProofError("control-state contract must be an object")
    checked = json.loads(json.dumps(contract, sort_keys=True, allow_nan=False))
    contract_digest = checked.get("contract_digest")
    unsigned = json.loads(json.dumps(checked))
    unsigned.pop("contract_digest", None)
    unsigned.pop("read_only_after_restore", None)
    families = checked.get("families") or []
    selected = next(
        (
            item for item in families
            if isinstance(item, dict) and item.get("family_ref") == family_ref
        ),
        None,
    )
    proof = checked.get("proof") or {}
    if (
        checked.get("status") != "success"
        or checked.get("kind") != "control_to_data_state_equation_contract"
        or checked.get("operator_class")
        != "REGISTERED_CONTROL_TO_DATA_STATE_EQUATION"
        or checked.get("read_only") is not True
        or checked.get("fresh_rediscovery") is not True
        or checked.get("private_mutation_manifest_retained") is not True
        or re.fullmatch(r"control_state_[1-9][0-9]*", str(family_ref)) is None
        or not isinstance(selected, dict)
        or not isinstance(selected.get("state_count"), int)
        or selected["state_count"] < 1
        or not isinstance(selected.get("support_width"), int)
        or not 0 <= selected["support_width"] <= 5
        or selected.get("state_primitive_before") != "FDSE"
        or selected.get("state_primitive_after") != "FDRE"
        or re.fullmatch(
            r"[0-9a-f]{64}", str(selected.get("mutation_target_digest") or "")
        ) is None
        or re.fullmatch(r"[0-9a-f]{64}", str(contract_digest or "")) is None
        or _digest(unsigned) != contract_digest
        or any(
            proof.get(key) is not True
            for key in (
                "set_implies_enable_truth_table_exhaustive",
                "minimal_care_set_support_recovered",
                "clock_enable_init_and_observable_cycle_preserved",
                "exact_cell_names_are_private",
            )
        )
    ):
        raise OperatorProofError("control-state equation proof is incomplete")
    authorization = {
        "kind": "operator_rewrite_authorization",
        "version": AUTHORIZATION_VERSION,
        "rewrite_kind": "exact_control_to_data_state_equation",
        "source": {
            "dcp_sha256": source_dcp_sha256,
            "region_sha256": contract_digest,
        },
        "spec": {
            "contract": checked,
            "contract_sha256": contract_digest,
            "family_ref": family_ref,
            "mutation_target_sha256": selected["mutation_target_digest"],
            "state_count": selected["state_count"],
        },
        "plan": {
            "template": "EXACT_CONTROL_TO_DATA_STATE_EQUATION_V1",
            "replace_synchronous_set_by_exact_data_factor": True,
            "preserve_clock_enable_init_and_observable_cycle": True,
            "use_only_private_same_seed_family_selector": True,
        },
        "proof": {
            "status": "proved_for_measurement_probe",
            "set_implies_enable_truth_table_exhaustive": True,
            "minimal_care_set_support_recovered": True,
            "registered_state_equation_equivalent_on_all_control_assignments": True,
            "exact_cell_identity_framework_private": True,
            "formal_whole_dcp_equivalence": False,
            "whole_dcp_equivalence_required_before_submission": True,
        },
        "authority": "pre_mutation_only",
    }
    authorization["certificate_id"] = _digest(authorization)
    return authorization


def build_control_state_equation_emitted_attestation(
    authorization: dict[str, Any],
    candidate_dcp_sha256: str,
    route_facts: dict[str, Any],
) -> dict[str, Any]:
    """Bind the routed state-equation rewrite to the selected private family."""
    _validate_identity(candidate_dcp_sha256, "control-state candidate DCP")
    source = authorization.get("source") or {}
    spec = authorization.get("spec") or {}
    _validate_identity(source.get("dcp_sha256"), "control-state source DCP")
    _validate_identity(source.get("region_sha256"), "control-state source region")
    if (
        route_facts.get("candidate_checkpoint_reopened") is not True
        or route_facts.get("source_checkpoint_reopened") is not True
        or route_facts.get("relevant_nets_fully_routed") is not True
        or route_facts.get("private_manifest_identity_replayed") is not True
        or route_facts.get("registered_control_transaction_preserved") is not True
        or route_facts.get("clock_identity_preserved") is not True
        or route_facts.get("truth_table_replayed") is not True
        or route_facts.get("care_set_support_replayed") is not True
        or route_facts.get("observable_cycle_preserved") is not True
        or route_facts.get("contract") != "EXACT_CONTROL_TO_DATA_STATE_EQUATION_V1"
        or route_facts.get("backend") != "exact_control_to_data_state_equation_v1"
        or route_facts.get("route_errors") != 0
        or route_facts.get("source_dcp_sha256") != source.get("dcp_sha256")
        or route_facts.get("contract_digest") != source.get("region_sha256")
        or route_facts.get("family_ref") != spec.get("family_ref")
        or route_facts.get("mutation_target_digest")
        != spec.get("mutation_target_sha256")
        or route_facts.get("selected_state_count") != spec.get("state_count")
    ):
        raise OperatorProofError("control-state route facts are incomplete")
    checks = {
        "exact_truth_table_replayed": True,
        "care_set_support_replayed": True,
        "private_manifest_identity_replayed": True,
        "registered_control_transaction_preserved": True,
        "architectural_latency_preserved": True,
        "clock_identity_preserved": True,
        "candidate_checkpoint_reopened": True,
        "source_checkpoint_reopened": True,
        "relevant_nets_fully_routed": True,
    }
    payload = {
        "kind": "operator_rewrite_emitted_topology_attestation",
        "version": 1,
        "status": "proved",
        "authority": "candidate_measurement_only",
        "template": "exact_control_to_data_state_equation_v1",
        "source": {
            "dcp_sha256": source["dcp_sha256"],
            "region_sha256": source["region_sha256"],
        },
        "candidate": {"dcp_sha256": candidate_dcp_sha256},
        "authorization_id": authorization.get("certificate_id"),
        "placement_manifest_sha256": _digest({
            "family_ref": spec.get("family_ref"),
            "mutation_target_sha256": spec.get("mutation_target_sha256"),
            "state_count": spec.get("state_count"),
        }),
        "topology_manifest_sha256": spec.get("contract_sha256"),
        "checks": checks,
    }
    payload["attestation_id"] = _digest(payload)
    return payload


def build_ff_srl_boundary_bridge_authorization(
    source_dcp_sha256: str,
    contract: dict[str, Any],
    family_ref: str,
) -> dict[str, Any]:
    """Bind one direct-FF family to its exact same-seed private selector."""
    _validate_identity(source_dcp_sha256, "FF-to-SRL source DCP")
    if not isinstance(contract, dict):
        raise OperatorProofError("FF-to-SRL contract must be an object")
    checked = json.loads(json.dumps(contract, sort_keys=True, allow_nan=False))
    contract_digest = checked.get("contract_digest")
    unsigned = json.loads(json.dumps(checked))
    unsigned.pop("contract_digest", None)
    unsigned.pop("read_only_after_restore", None)
    selected = next(
        (
            item for item in checked.get("families") or []
            if isinstance(item, dict) and item.get("family_ref") == family_ref
        ),
        None,
    )
    proof = checked.get("proof") or {}
    if (
        checked.get("status") != "success"
        or checked.get("kind") != "ff_srl_boundary_bridge_contract"
        or checked.get("operator_class")
        != "DIRECT_FF_CHAIN_TO_SRL_BOUNDARY_BRIDGE"
        or checked.get("read_only") is not True
        or checked.get("fresh_rediscovery") is not True
        or checked.get("private_mutation_manifest_retained") is not True
        or re.fullmatch(r"ff_srl_[1-9][0-9]*", str(family_ref)) is None
        or not isinstance(selected, dict)
        or not isinstance(selected.get("chain_count"), int)
        or selected["chain_count"] < 1
        or not isinstance(selected.get("chain_length"), int)
        or not 3 <= selected["chain_length"] <= 33
        or selected.get("ce_mode") not in {"constant_high", "dynamic"}
        or selected.get("retained_boundary_ff_per_chain") != 1
        or selected.get("replacement_primitive") != "SRLC32E"
        or selected.get("enabled_cycle_latency") != selected["chain_length"]
        or _SHA256_RE.fullmatch(
            str(selected.get("mutation_target_digest") or "")
        ) is None
        or _SHA256_RE.fullmatch(str(contract_digest or "")) is None
        or _digest(unsigned) != contract_digest
        or any(
            proof.get(key) is not True
            for key in (
                "maximal_direct_chain_recovered",
                "clock_ce_reset_init_uniform",
                "intermediate_side_fanout_absent",
                "enabled_cycle_latency_preserved",
                "exact_cell_names_are_private",
            )
        )
    ):
        raise OperatorProofError("FF-to-SRL structural proof is incomplete")
    authorization = {
        "kind": "operator_rewrite_authorization",
        "version": AUTHORIZATION_VERSION,
        "rewrite_kind": "direct_ff_chain_to_srl_boundary_bridge",
        "source": {
            "dcp_sha256": source_dcp_sha256,
            "region_sha256": contract_digest,
        },
        "spec": {
            "contract": checked,
            "contract_sha256": contract_digest,
            "family_ref": family_ref,
            "mutation_target_sha256": selected["mutation_target_digest"],
            "chain_count": selected["chain_count"],
            "chain_length": selected["chain_length"],
            "ce_mode": selected["ce_mode"],
        },
        "plan": {
            "template": "FF_SRL_BOUNDARY_BRIDGE_V1",
            "retain_first_hierarchy_boundary_ff": True,
            "replace_transport_only_tail_by_srlc32e": True,
            "preserve_enabled_cycle_latency": True,
            "use_only_private_same_seed_family_selector": True,
        },
        "proof": {
            "status": "proved_for_measurement_probe",
            "direct_chain_and_control_transaction_recovered": True,
            "intermediate_taps_absent": True,
            "enabled_cycle_semantics_exact": True,
            "exact_cell_identity_framework_private": True,
            "formal_whole_dcp_equivalence": False,
            "whole_dcp_equivalence_required_before_submission": True,
        },
        "authority": "pre_mutation_only",
    }
    authorization["certificate_id"] = _digest(authorization)
    return authorization


def build_ff_srl_boundary_bridge_emitted_attestation(
    authorization: dict[str, Any],
    candidate_dcp_sha256: str,
    route_facts: dict[str, Any],
) -> dict[str, Any]:
    """Bind the routed FF-to-SRL bridge to its selected exact family."""
    _validate_identity(candidate_dcp_sha256, "FF-to-SRL candidate DCP")
    source = authorization.get("source") or {}
    spec = authorization.get("spec") or {}
    _validate_identity(source.get("dcp_sha256"), "FF-to-SRL source DCP")
    _validate_identity(source.get("region_sha256"), "FF-to-SRL source region")
    if (
        route_facts.get("candidate_checkpoint_reopened") is not True
        or route_facts.get("source_checkpoint_reopened") is not True
        or route_facts.get("relevant_nets_fully_routed") is not True
        or route_facts.get("private_manifest_identity_replayed") is not True
        or route_facts.get("enabled_cycle_latency_preserved") is not True
        or route_facts.get("hierarchy_boundary_ff_retained") is not True
        or route_facts.get("clock_identity_preserved") is not True
        or route_facts.get("contract") != "FF_SRL_BOUNDARY_BRIDGE_V1"
        or route_facts.get("backend") != "ff_srl_boundary_bridge_v1"
        or route_facts.get("route_errors") != 0
        or route_facts.get("pulse_violations") != 0
        or route_facts.get("source_dcp_sha256") != source.get("dcp_sha256")
        or route_facts.get("contract_digest") != source.get("region_sha256")
        or route_facts.get("family_ref") != spec.get("family_ref")
        or route_facts.get("mutation_target_digest")
        != spec.get("mutation_target_sha256")
        or route_facts.get("selected_chain_count") != spec.get("chain_count")
        or route_facts.get("selected_chain_length") != spec.get("chain_length")
        or route_facts.get("ce_mode") != spec.get("ce_mode")
    ):
        raise OperatorProofError("FF-to-SRL route facts are incomplete")
    checks = {
        "private_manifest_identity_replayed": True,
        "direct_chain_topology_replayed": True,
        "hierarchy_boundary_ff_retained": True,
        "enabled_cycle_latency_preserved": True,
        "clock_identity_preserved": True,
        "candidate_checkpoint_reopened": True,
        "source_checkpoint_reopened": True,
        "relevant_nets_fully_routed": True,
    }
    payload = {
        "kind": "operator_rewrite_emitted_topology_attestation",
        "version": 1,
        "status": "proved",
        "authority": "candidate_measurement_only",
        "template": "ff_srl_boundary_bridge_v1",
        "source": {
            "dcp_sha256": source["dcp_sha256"],
            "region_sha256": source["region_sha256"],
        },
        "candidate": {"dcp_sha256": candidate_dcp_sha256},
        "authorization_id": authorization.get("certificate_id"),
        "placement_manifest_sha256": _digest({
            "family_ref": spec.get("family_ref"),
            "mutation_target_sha256": spec.get("mutation_target_sha256"),
            "chain_count": spec.get("chain_count"),
            "chain_length": spec.get("chain_length"),
        }),
        "topology_manifest_sha256": spec.get("contract_sha256"),
        "checks": checks,
    }
    payload["attestation_id"] = _digest(payload)
    return payload


def build_multilevel_control_localization_authorization(
    source_dcp_sha256: str,
    contract: dict[str, Any],
    family_ref: str,
) -> dict[str, Any]:
    """Authorize exact cloning of one recovered same-cycle Boolean control DAG."""
    _validate_identity(source_dcp_sha256, "control-localization source DCP")
    if not isinstance(contract, dict):
        raise OperatorProofError("control-localization contract is absent")
    checked = json.loads(json.dumps(contract, sort_keys=True, allow_nan=False))
    contract_digest = checked.pop("contract_digest", None)
    obligations = set(checked.get("required_before_mutation") or [])
    if (
        checked.get("kind")
        != "multilevel_registered_control_distribution_contract"
        or checked.get("schema_version") != 1
        or checked.get("status") != "proved_read_only"
        or checked.get("authority")
        != "multilevel_control_localization_proof_input"
        or checked.get("mutation_eligible") is not True
        or checked.get("operator_class")
        != "MULTILEVEL_REGISTERED_CONTROL_DISTRIBUTION"
        or checked.get("source_dcp_sha256") != source_dcp_sha256
        or family_ref != "multilevel_control_1"
        or checked.get("candidate_ref") != family_ref
        or not isinstance(checked.get("logic_depth"), int)
        or checked["logic_depth"] < 1
        or not isinstance(checked.get("closure_cell_count"), int)
        or checked["closure_cell_count"] < 1
        or not isinstance(checked.get("sink_count"), int)
        or checked["sink_count"] < 1
        or not isinstance(checked.get("clock_region_count"), int)
        or checked["clock_region_count"] < 1
        or _SHA256_RE.fullmatch(
            str(checked.get("private_manifest_sha256") or "")
        ) is None
        or _SHA256_RE.fullmatch(str(contract_digest or "")) is None
        or _digest(checked) != contract_digest
        or not {
            "clone every LUT and side input in the complete registered-boundary Boolean DAG",
            "preserve the original generator and every non-selected consumer",
            "move only same-role registered control sinks without changing their clock or cycle",
            "route and reopen under the unchanged contest clock",
        } <= obligations
    ):
        raise OperatorProofError("control-localization structural proof is incomplete")
    authorization = {
        "kind": "operator_rewrite_authorization",
        "version": AUTHORIZATION_VERSION,
        "rewrite_kind": (
            "multilevel_registered_control_distribution_localization"
        ),
        "source": {
            "dcp_sha256": source_dcp_sha256,
            "region_sha256": contract_digest,
        },
        "spec": {
            "contract": contract,
            "contract_sha256": contract_digest,
            "family_ref": family_ref,
            "private_manifest_sha256": checked["private_manifest_sha256"],
            "closure_cell_count": checked["closure_cell_count"],
            "sink_count": checked["sink_count"],
            "clock_region_count": checked["clock_region_count"],
        },
        "plan": {
            "template": "MULTILEVEL_CONTROL_DAG_CLONE_V1",
            "clone_exact_lut_init_and_all_side_inputs": True,
            "preserve_original_generator_and_nonselected_consumers": True,
            "move_only_same_role_registered_sinks": True,
            "preserve_clock_and_cycle": True,
            "use_only_private_same_seed_family_selector": True,
        },
        "proof": {
            "status": "proved_for_measurement_probe",
            "complete_boolean_dag_recovered": True,
            "exact_lut_truth_tables_and_boundary_sources_bound": True,
            "registered_sink_role_and_cycle_preserved": True,
            "exact_object_identity_framework_private": True,
            "formal_whole_dcp_equivalence": False,
            "whole_dcp_equivalence_required_before_submission": True,
        },
        "authority": "pre_mutation_only",
    }
    authorization["certificate_id"] = _digest(authorization)
    return authorization


def build_multilevel_control_localization_emitted_attestation(
    authorization: dict[str, Any],
    candidate_dcp_sha256: str,
    route_facts: dict[str, Any],
) -> dict[str, Any]:
    """Bind the routed clone topology to the exact current-seed contract."""
    _validate_identity(candidate_dcp_sha256, "control-localization candidate DCP")
    source = authorization.get("source") or {}
    spec = authorization.get("spec") or {}
    if (
        route_facts.get("candidate_checkpoint_reopened") is not True
        or route_facts.get("source_checkpoint_reopened") is not True
        or route_facts.get("relevant_nets_fully_routed") is not True
        or route_facts.get("private_manifest_identity_replayed") is not True
        or route_facts.get("exact_boolean_dag_cloned") is not True
        or route_facts.get("registered_sink_role_and_cycle_preserved") is not True
        or route_facts.get("placement_fix_identity_preserved") is not True
        or route_facts.get("clock_identity_preserved") is not True
        or route_facts.get("contract") != "MULTILEVEL_CONTROL_DAG_CLONE_V1"
        or route_facts.get("backend")
        != "multilevel_control_distribution_localization_v1"
        or route_facts.get("route_errors") != 0
        or route_facts.get("pulse_violations") != 0
        or route_facts.get("source_dcp_sha256") != source.get("dcp_sha256")
        or route_facts.get("contract_digest") != source.get("region_sha256")
        or route_facts.get("family_ref") != spec.get("family_ref")
        or route_facts.get("closure_cell_count")
        != spec.get("closure_cell_count")
        or route_facts.get("localized_sink_count") != spec.get("sink_count")
    ):
        raise OperatorProofError("control-localization route facts are incomplete")
    checks = {
        "private_manifest_identity_replayed": True,
        "exact_boolean_dag_cloned": True,
        "registered_sink_role_and_cycle_preserved": True,
        "original_generator_and_nonselected_consumers_preserved": True,
        "placement_fix_identity_preserved": True,
        "clock_identity_preserved": True,
        "candidate_checkpoint_reopened": True,
        "source_checkpoint_reopened": True,
        "relevant_nets_fully_routed": True,
    }
    payload = {
        "kind": "operator_rewrite_emitted_topology_attestation",
        "version": 1,
        "status": "proved",
        "authority": "candidate_measurement_only",
        "template": "multilevel_control_distribution_localization_v1",
        "source": {
            "dcp_sha256": source.get("dcp_sha256"),
            "region_sha256": source.get("region_sha256"),
        },
        "candidate": {"dcp_sha256": candidate_dcp_sha256},
        "authorization_id": authorization.get("certificate_id"),
        "placement_manifest_sha256": _digest({
            "family_ref": spec.get("family_ref"),
            "clock_region_count": spec.get("clock_region_count"),
            "sink_count": spec.get("sink_count"),
        }),
        "topology_manifest_sha256": route_facts.get(
            "topology_manifest_sha256"
        ),
        "checks": checks,
    }
    _validate_identity(
        payload["topology_manifest_sha256"],
        "control-localization topology manifest",
    )
    payload["attestation_id"] = _digest(payload)
    return payload


def build_multilevel_ram_control_localization_authorization(
    source_dcp_sha256: str,
    contract: dict[str, Any],
    family_ref: str,
) -> dict[str, Any]:
    """Authorize one exact current-seed RAM-control Boolean DAG clone."""
    _validate_identity(source_dcp_sha256, "RAM-control localization source DCP")
    if not isinstance(contract, dict):
        raise OperatorProofError("RAM-control localization contract is absent")
    checked = json.loads(json.dumps(contract, sort_keys=True, allow_nan=False))
    contract_digest = checked.pop("contract_digest", None)
    obligations = set(checked.get("required_before_mutation") or [])
    if (
        checked.get("kind")
        != "multilevel_ram_control_distribution_proof_input"
        or checked.get("schema_version") != 1
        or checked.get("status") != "proved_read_only"
        or checked.get("authority")
        != "multilevel_ram_control_localization_proof_input"
        or checked.get("mutation_eligible") is not True
        or checked.get("operator_class")
        != "MULTILEVEL_RAM_CONTROL_DISTRIBUTION"
        or re.fullmatch(r"nr-[0-9a-f]{32}", str(checked.get("run_id") or ""))
        is None
        or checked.get("source_dcp_sha256") != source_dcp_sha256
        or family_ref != "multilevel_ram_control_1"
        or checked.get("candidate_ref") != family_ref
        or not isinstance(checked.get("logic_depth"), int)
        or checked["logic_depth"] < 1
        or not isinstance(checked.get("closure_cell_count"), int)
        or checked["closure_cell_count"] < 1
        or not isinstance(checked.get("sink_count"), int)
        or checked["sink_count"] < 2
        or not isinstance(checked.get("clock_region_count"), int)
        or checked["clock_region_count"] < 2
        or not isinstance(checked.get("nonselected_consumer_count"), int)
        or checked["nonselected_consumer_count"] < 0
        or not isinstance(checked.get("sampled_path_count"), int)
        or checked["sampled_path_count"] < 1
        or not isinstance(checked.get("earliest_path_rank"), int)
        or not 1 <= checked["earliest_path_rank"] <= checked["sampled_path_count"]
        or not isinstance(checked.get("live_timing_rank_per_mille"), int)
        or not 1 <= checked["live_timing_rank_per_mille"] <= 250
        or checked.get("timing_eligibility_gate")
        != "top_current_seed_timing_quartile"
        or not isinstance(checked.get("sink_role"), str)
        or not checked["sink_role"]
        or not isinstance(checked.get("ram_primitive_counts"), dict)
        or sum(checked["ram_primitive_counts"].values()) != checked["sink_count"]
        or _SHA256_RE.fullmatch(
            str(checked.get("private_manifest_sha256") or "")
        ) is None
        or _SHA256_RE.fullmatch(str(contract_digest or "")) is None
        or _digest(checked) != contract_digest
        or not {
            "clone every LUT and side input in the complete RAM-control Boolean DAG",
            "preserve each RAM primitive, semantic property, pin role, and cycle boundary",
            "rewire only selected same-role RAM-control sinks",
            "preserve the original generator and every non-selected consumer",
            "route and reopen under the unchanged contest clock",
        } <= obligations
    ):
        raise OperatorProofError(
            "RAM-control localization structural proof is incomplete"
        )
    authorization = {
        "kind": "operator_rewrite_authorization",
        "version": AUTHORIZATION_VERSION,
        "rewrite_kind": "multilevel_ram_control_distribution_localization",
        "source": {
            "dcp_sha256": source_dcp_sha256,
            "region_sha256": contract_digest,
        },
        "spec": {
            "contract": contract,
            "run_id": checked["run_id"],
            "contract_sha256": contract_digest,
            "family_ref": family_ref,
            "private_manifest_sha256": checked["private_manifest_sha256"],
            "closure_cell_count": checked["closure_cell_count"],
            "sink_count": checked["sink_count"],
            "clock_region_count": checked["clock_region_count"],
            "nonselected_consumer_count": checked["nonselected_consumer_count"],
            "sampled_path_count": checked["sampled_path_count"],
            "earliest_path_rank": checked["earliest_path_rank"],
            "live_timing_rank_per_mille": checked[
                "live_timing_rank_per_mille"
            ],
            "sink_role": checked["sink_role"],
            "ram_primitive_counts": checked["ram_primitive_counts"],
        },
        "plan": {
            "template": "MULTILEVEL_RAM_CONTROL_DAG_CLONE_V1",
            "clone_exact_lut_init_and_all_side_inputs": True,
            "preserve_ram_primitive_property_and_pin_role": True,
            "rewire_selected_ram_control_sinks_only": True,
            "preserve_original_generator_and_nonselected_consumers": True,
            "preserve_zero_cycle_combinational_schedule": True,
            "use_only_private_same_seed_family_selector": True,
        },
        "proof": {
            "status": "proved_for_measurement_probe",
            "complete_boolean_dag_recovered": True,
            "exact_lut_truth_tables_and_boundary_sources_bound": True,
            "ram_primitive_property_and_pin_role_bound": True,
            "zero_cycle_combinational_schedule_preserved": True,
            "exact_object_identity_framework_private": True,
            "formal_whole_dcp_equivalence": False,
            "whole_dcp_equivalence_required_before_submission": True,
        },
        "authority": "pre_mutation_only",
    }
    authorization["certificate_id"] = _digest(authorization)
    return authorization


def build_multilevel_ram_control_localization_emitted_attestation(
    authorization: dict[str, Any],
    candidate_dcp_sha256: str,
    route_facts: dict[str, Any],
) -> dict[str, Any]:
    """Bind routed RAM-control topology to the exact current-seed contract."""
    _validate_identity(candidate_dcp_sha256, "RAM-control localization candidate DCP")
    source = authorization.get("source") or {}
    spec = authorization.get("spec") or {}
    required_true = (
        "candidate_checkpoint_reopened",
        "source_checkpoint_reopened",
        "relevant_nets_fully_routed",
        "private_manifest_identity_preserved",
        "exact_boolean_dag_cloned",
        "ram_primitive_and_property_identity_preserved",
        "ram_control_pin_role_preserved",
        "selected_ram_control_sinks_only_rewired",
        "zero_cycle_combinational_schedule_preserved",
        "original_generator_and_nonselected_consumers_preserved",
        "placement_fix_identity_preserved",
        "clock_identity_preserved",
    )
    if (
        not all(route_facts.get(key) is True for key in required_true)
        or route_facts.get("contract")
        != "MULTILEVEL_RAM_CONTROL_DAG_CLONE_V1"
        or route_facts.get("backend")
        != "multilevel_ram_control_distribution_localization_v1"
        or route_facts.get("route_errors") != 0
        or route_facts.get("pulse_violations") != 0
        or route_facts.get("source_dcp_sha256") != source.get("dcp_sha256")
        or route_facts.get("run_id") != spec.get("run_id")
        or route_facts.get("contract_digest") != source.get("region_sha256")
        or route_facts.get("family_ref") != spec.get("family_ref")
        or route_facts.get("closure_cell_count")
        != spec.get("closure_cell_count")
        or route_facts.get("localized_sink_count") != spec.get("sink_count")
        or route_facts.get("nonselected_consumer_count")
        != spec.get("nonselected_consumer_count")
    ):
        raise OperatorProofError(
            "RAM-control localization route facts are incomplete"
        )
    checks = {key: True for key in required_true}
    payload = {
        "kind": "operator_rewrite_emitted_topology_attestation",
        "version": 1,
        "status": "proved",
        "authority": "candidate_measurement_only",
        "template": "multilevel_ram_control_distribution_localization_v1",
        "source": {
            "dcp_sha256": source.get("dcp_sha256"),
            "region_sha256": source.get("region_sha256"),
        },
        "candidate": {"dcp_sha256": candidate_dcp_sha256},
        "authorization_id": authorization.get("certificate_id"),
        "placement_manifest_sha256": _digest({
            "family_ref": spec.get("family_ref"),
            "clock_region_count": spec.get("clock_region_count"),
            "sink_count": spec.get("sink_count"),
            "sink_role": spec.get("sink_role"),
        }),
        "topology_manifest_sha256": route_facts.get(
            "topology_manifest_sha256"
        ),
        "checks": checks,
    }
    _validate_identity(
        payload["topology_manifest_sha256"],
        "RAM-control localization topology manifest",
    )
    payload["attestation_id"] = _digest(payload)
    return payload


def build_registered_parity_authorization(
    source_dcp_sha256: str,
    contract: dict[str, Any],
    family_ref: str,
    implementation_mode: str = "local_preserve",
) -> dict[str, Any]:
    """Authorize one exact GF(2) closure rewrite at a registered boundary."""
    _validate_identity(source_dcp_sha256, "registered parity source DCP")
    if implementation_mode not in {
        "local_preserve", "closure_local_rebasin", "global_quick_rebasin",
    }:
        raise OperatorProofError("registered parity implementation mode is invalid")
    if not isinstance(contract, dict):
        raise OperatorProofError("registered parity contract is absent")
    checked = json.loads(json.dumps(contract, sort_keys=True, allow_nan=False))
    contract_digest = checked.pop("contract_digest", None)
    obligations = set(checked.get("required_before_mutation") or [])
    candidates = checked.get("candidates")
    selected = next(
        (
            item for item in candidates or []
            if isinstance(item, dict) and item.get("candidate_ref") == family_ref
        ),
        None,
    )
    required_obligations = {
        "select one current-wall registered endpoint from the private same-seed family",
        "replay every XOR or XNOR truth table and cancel only even GF2 leaf multiplicities",
        "emit a balanced XOR tree from every odd-multiplicity leaf",
        "preserve the destination register clock CE reset INIT and observable cycle",
        "preserve the original root and every non-selected consumer",
        "bind emitted topology then route and reopen under the unchanged contest clock",
    }
    if (
        checked.get("kind") != "registered_gf2_parity_closure_contract"
        or checked.get("schema_version") != 1
        or checked.get("status") != "proved_read_only"
        or checked.get("authority")
        != "registered_parity_reassociation_proof_input"
        or checked.get("mutation_eligible") is not True
        or checked.get("operator_class") != "REGISTERED_GF2_PARITY_REDUCTION"
        or checked.get("action_class")
        != "registered_gf2_parity_reassociation_v1"
        or checked.get("proof_template")
        != "REGISTERED_GF2_PARITY_REASSOCIATION_V1"
        or checked.get("source_dcp_sha256") != source_dcp_sha256
        or not isinstance(candidates, list)
        or checked.get("candidate_count") != len(candidates)
        or not isinstance(selected, dict)
        or not isinstance(selected.get("logic_depth"), int)
        or selected["logic_depth"] < 1
        or not isinstance(selected.get("closure_cell_count"), int)
        or selected["closure_cell_count"] < 1
        or not isinstance(selected.get("raw_leaf_occurrence_count"), int)
        or not isinstance(selected.get("odd_leaf_count"), int)
        or selected["odd_leaf_count"] < 2
        or selected["raw_leaf_occurrence_count"] < selected["odd_leaf_count"]
        or selected.get("canceled_leaf_occurrence_count")
        != selected["raw_leaf_occurrence_count"] - selected["odd_leaf_count"]
        or not isinstance(selected.get("registered_endpoint_count"), int)
        or selected["registered_endpoint_count"] < 1
        or not isinstance(selected.get("live_registered_endpoint_count"), int)
        or selected["live_registered_endpoint_count"] < 1
        or selected.get("output_polarity") not in {0, 1}
        or _SHA256_RE.fullmatch(
            str(checked.get("private_manifest_sha256") or "")
        ) is None
        or _SHA256_RE.fullmatch(str(contract_digest or "")) is None
        or _digest(checked) != contract_digest
        or not required_obligations <= obligations
    ):
        raise OperatorProofError("registered parity structural proof is incomplete")
    authorization = {
        "kind": "operator_rewrite_authorization",
        "version": AUTHORIZATION_VERSION,
        "rewrite_kind": "registered_gf2_parity_reassociation",
        "source": {
            "dcp_sha256": source_dcp_sha256,
            "region_sha256": contract_digest,
        },
        "spec": {
            "contract": contract,
            "contract_sha256": contract_digest,
            "family_ref": family_ref,
            "implementation_mode": implementation_mode,
            "private_manifest_sha256": checked["private_manifest_sha256"],
            "logic_depth": selected["logic_depth"],
            "closure_cell_count": selected["closure_cell_count"],
            "raw_leaf_occurrence_count": selected["raw_leaf_occurrence_count"],
            "odd_leaf_count": selected["odd_leaf_count"],
            "registered_endpoint_count": selected["registered_endpoint_count"],
        },
        "plan": {
            "template": "REGISTERED_GF2_PARITY_REASSOCIATION_V1",
            "replay_exact_xor_xnor_truth_tables": True,
            "cancel_only_even_gf2_boundary_multiplicities": True,
            "emit_balanced_lut6_xor_tree": True,
            "implementation_mode": implementation_mode,
            "preserve_existing_placement": implementation_mode == "local_preserve",
            "allow_closure_local_rebasin": (
                implementation_mode == "closure_local_rebasin"
            ),
            "allow_global_quick_rebasin": (
                implementation_mode == "global_quick_rebasin"
            ),
            "replace_one_private_live_registered_data_endpoint": True,
            "preserve_original_root_and_nonselected_consumers": True,
            "preserve_destination_register_and_observable_cycle": True,
        },
        "proof": {
            "status": "proved_for_measurement_probe",
            "complete_gf2_closure_recovered": True,
            "all_boundary_multiplicities_replayed": True,
            "selected_registered_boundary_closed": True,
            "exact_object_identity_framework_private": True,
            "formal_whole_dcp_equivalence": False,
            "whole_dcp_equivalence_required_before_submission": True,
        },
        "authority": "pre_mutation_only",
    }
    authorization["certificate_id"] = _digest(authorization)
    return authorization


def build_registered_parity_emitted_attestation(
    authorization: dict[str, Any],
    candidate_dcp_sha256: str,
    route_facts: dict[str, Any],
) -> dict[str, Any]:
    """Bind the routed balanced tree to its same-seed GF(2) contract."""
    _validate_identity(candidate_dcp_sha256, "registered parity candidate DCP")
    source = authorization.get("source") or {}
    spec = authorization.get("spec") or {}
    sink_counts = (
        route_facts.get("old_root_sink_count_before"),
        route_facts.get("nonselected_sink_count_expected"),
        route_facts.get("nonselected_sink_count_after_route"),
        route_facts.get("nonselected_sink_count_after_reopen"),
    )
    if (
        route_facts.get("candidate_checkpoint_reopened") is not True
        or route_facts.get("source_checkpoint_reopened") is not True
        or route_facts.get("relevant_nets_fully_routed") is not True
        or route_facts.get("private_manifest_identity_replayed") is not True
        or route_facts.get("exact_gf2_expression_replayed") is not True
        or route_facts.get("balanced_xor_tree_emitted") is not True
        or route_facts.get("selected_registered_boundary_preserved") is not True
        or route_facts.get(
            "original_root_and_nonselected_consumers_preserved"
        ) is not True
        or route_facts.get(
            "all_nonselected_consumer_sink_set_preserved"
        ) is not True
        or route_facts.get("placement_fix_identity_preserved") is not True
        or route_facts.get("clock_identity_preserved") is not True
        or route_facts.get("contract")
        != "REGISTERED_GF2_PARITY_REASSOCIATION_V1"
        or route_facts.get("backend")
        != "registered_gf2_parity_reassociation_v1"
        or route_facts.get("route_errors") != 0
        or route_facts.get("pulse_violations") != 0
        or route_facts.get("source_dcp_sha256") != source.get("dcp_sha256")
        or route_facts.get("contract_digest") != source.get("region_sha256")
        or route_facts.get("family_ref") != spec.get("family_ref")
        or route_facts.get("implementation_mode")
        != spec.get("implementation_mode")
        or not isinstance(route_facts.get("rebasin_cell_count"), int)
        or not isinstance(route_facts.get("rebasin_site_count"), int)
        or (
            spec.get("implementation_mode") == "local_preserve"
            and route_facts.get("rebasin_cell_count") != 0
        )
        or (
            spec.get("implementation_mode") == "global_quick_rebasin"
            and route_facts.get("rebasin_cell_count") < 1
        )
        or (
            spec.get("implementation_mode") == "closure_local_rebasin"
            and (
                route_facts.get("rebasin_cell_count") < 1
                or route_facts.get("rebasin_site_count") < 1
            )
        )
        or route_facts.get("odd_leaf_count") != spec.get("odd_leaf_count")
        or route_facts.get("old_closure_cell_count")
        != spec.get("closure_cell_count")
        or route_facts.get("registered_endpoint_count")
        != spec.get("registered_endpoint_count")
        or route_facts.get("preserved_sibling_count")
        != spec.get("registered_endpoint_count") - 1
        or not all(isinstance(value, int) for value in sink_counts)
        or sink_counts[0] != sink_counts[1] + 1
        or sink_counts[2] != sink_counts[1]
        or sink_counts[3] != sink_counts[1]
    ):
        raise OperatorProofError("registered parity route facts are incomplete")
    checks = {
        "private_manifest_identity_replayed": True,
        "exact_gf2_expression_replayed": True,
        "balanced_xor_tree_emitted": True,
        "selected_registered_boundary_preserved": True,
        "original_root_and_nonselected_consumers_preserved": True,
        "all_nonselected_consumer_sink_set_preserved": True,
        "placement_fix_identity_preserved": True,
        "clock_identity_preserved": True,
        "candidate_checkpoint_reopened": True,
        "source_checkpoint_reopened": True,
        "relevant_nets_fully_routed": True,
    }
    payload = {
        "kind": "operator_rewrite_emitted_topology_attestation",
        "version": 1,
        "status": "proved",
        "authority": "candidate_measurement_only",
        "template": "registered_gf2_parity_reassociation_v1",
        "source": {
            "dcp_sha256": source.get("dcp_sha256"),
            "region_sha256": source.get("region_sha256"),
        },
        "candidate": {"dcp_sha256": candidate_dcp_sha256},
        "authorization_id": authorization.get("certificate_id"),
        "placement_manifest_sha256": _digest({
            "family_ref": spec.get("family_ref"),
            "implementation_mode": spec.get("implementation_mode"),
            "rebasin_cell_count": route_facts.get("rebasin_cell_count"),
            "rebasin_site_count": route_facts.get("rebasin_site_count"),
            "registered_endpoint_count": spec.get("registered_endpoint_count"),
            "preserved_sibling_count": route_facts.get(
                "preserved_sibling_count"
            ),
            "old_root_sink_count_before": route_facts.get(
                "old_root_sink_count_before"
            ),
            "nonselected_sink_count_expected": route_facts.get(
                "nonselected_sink_count_expected"
            ),
        }),
        "topology_manifest_sha256": route_facts.get(
            "topology_manifest_sha256"
        ),
        "checks": checks,
    }
    _validate_identity(
        payload["topology_manifest_sha256"],
        "registered parity topology manifest",
    )
    payload["attestation_id"] = _digest(payload)
    return payload


def build_memory_forward_emitted_attestation(
    authorization: dict[str, Any],
    candidate_dcp_sha256: str,
    route_facts: dict[str, Any],
) -> dict[str, Any]:
    """Bind a routed write-buffer/forwarding ECO to its relational proof."""
    _validate_identity(candidate_dcp_sha256, "memory-forward candidate DCP")
    source = authorization.get("source") or {}
    spec = authorization.get("spec") or {}
    proof = authorization.get("proof") or {}
    cost = proof.get("cost_estimate") or {}
    _validate_identity(source.get("dcp_sha256"), "memory-forward source DCP")
    _validate_identity(source.get("region_sha256"), "memory-forward source region")
    topology_digest = route_facts.get("topology_manifest_sha256")
    if (
        replay_memory_forward_authorization(authorization) is None
        or route_facts.get("candidate_checkpoint_reopened") is not True
        or route_facts.get("source_checkpoint_reopened") is not True
        or route_facts.get("relevant_nets_fully_routed") is not True
        or route_facts.get("private_family_identity_replayed") is not True
        or route_facts.get("local_relational_memory_miter_replayed") is not True
        or route_facts.get("clock_identity_preserved") is not True
        or route_facts.get("contract")
        != "BRAM_WRITE_BUFFER_FORWARD_LOCAL_MITER_V1"
        or route_facts.get("backend") != "memory_write_buffer_forward_v1"
        or route_facts.get("route_errors") != 0
        or route_facts.get("pulse_violations") != 0
        or route_facts.get("source_dcp_sha256") != source.get("dcp_sha256")
        or route_facts.get("target_memory_fingerprint")
        != spec.get("target_memory_fingerprint")
        or _SHA256_RE.fullmatch(str(topology_digest or "")) is None
        or route_facts.get("new_fdres") != cost.get("total_ffs")
        or route_facts.get("new_luts") != cost.get("total_luts")
    ):
        raise OperatorProofError("memory-forward route facts are incomplete")
    checks = {
        "local_relational_memory_miter_replayed": True,
        "private_family_identity_replayed": True,
        "write_pipeline_and_read_forwarding_topology_replayed": True,
        "exact_resource_accounting": True,
        "clock_identity_preserved": True,
        "candidate_checkpoint_reopened": True,
        "source_checkpoint_reopened": True,
        "relevant_nets_fully_routed": True,
    }
    payload = {
        "kind": "operator_rewrite_emitted_topology_attestation",
        "version": 1,
        "status": "proved",
        "authority": "candidate_measurement_only",
        "template": "memory_write_buffer_forward_v1",
        "source": {
            "dcp_sha256": source["dcp_sha256"],
            "region_sha256": source["region_sha256"],
        },
        "candidate": {"dcp_sha256": candidate_dcp_sha256},
        "authorization_id": authorization.get("certificate_id"),
        "placement_manifest_sha256": _digest({
            "target_memory_fingerprint": spec.get("target_memory_fingerprint"),
            "family_ref": route_facts.get("family_ref"),
            "new_fdres": route_facts.get("new_fdres"),
            "new_luts": route_facts.get("new_luts"),
        }),
        "topology_manifest_sha256": topology_digest,
        "checks": checks,
    }
    payload["attestation_id"] = _digest(payload)
    return payload


def build_banked_ram_operator_authorization(
    source_dcp_sha256: str,
    contract: dict[str, Any],
    target: str,
) -> dict[str, Any]:
    """Bind one anonymous RAM-data or RAM-control local measurement action."""
    _validate_identity(source_dcp_sha256, "banked RAM operator source DCP")
    bindings = {
        "registered_ram_boolean_midpoint_relocation_v1": (
            "registered_ram_boolean_midpoint_relocation",
            "REGISTERED_RAM_BOOLEAN_MIDPOINT_V1",
            "data_operator",
        ),
        "registered_control_packet_localization_v1": (
            "registered_control_packet_localization",
            "REGISTERED_CONTROL_PACKET_LOCALIZATION_V1",
            "control_operator",
        ),
    }
    binding = bindings.get(target)
    if binding is None or not isinstance(contract, dict):
        raise OperatorProofError("unsupported banked RAM operator target")
    rewrite_kind, proof_template, scope = binding
    data = contract.get("data_operator") or {}
    control = contract.get("control_operator") or {}
    if contract.get("operator_class") != "BANKED_RAM_DATA_CONTROL_OPERATOR":
        raise OperatorProofError("banked RAM operator class is incomplete")
    if target == "registered_ram_boolean_midpoint_relocation_v1":
        valid = (
            data.get("semantic_class") == "RAM_Q_BOOLEAN_REGISTER"
            and isinstance(data.get("operator_count"), int)
            and data["operator_count"] > 0
            and isinstance(data.get("truth_table_classes"), list)
            and bool(data["truth_table_classes"])
            and data.get("registered_peer_operand_recovered") is True
            and data.get("endpoint_clock_ce_reset_init_recovered") is True
            and data.get("architectural_latency_delta_cycles") == 0
        )
        checked_operator = data
    else:
        valid = (
            control.get("semantic_class") == "BANKED_RAM_CONTROL_PACKET"
            and isinstance(control.get("packet_count"), int)
            and control["packet_count"] > 0
            and isinstance(control.get("packets"), list)
            and bool(control["packets"])
            and control.get("registered_state_or_static_inputs_only") is True
            and control.get("bank_local_ram_pin_ownership_recovered") is True
            and control.get("architectural_latency_delta_cycles") == 0
        )
        checked_operator = control
    if not valid:
        raise OperatorProofError("banked RAM data/control contract is incomplete")
    checked = json.loads(json.dumps({
        "operator_class": "BANKED_RAM_DATA_CONTROL_OPERATOR",
        "scope": scope,
        "operator": checked_operator,
    }, sort_keys=True, allow_nan=False))
    region_sha256 = _digest(checked)
    authorization = {
        "kind": "operator_rewrite_authorization",
        "version": AUTHORIZATION_VERSION,
        "rewrite_kind": rewrite_kind,
        "source": {
            "dcp_sha256": source_dcp_sha256,
            "region_sha256": region_sha256,
        },
        "spec": {
            "target": target,
            "contract": checked,
            "contract_sha256": region_sha256,
        },
        "plan": {
            "template": proof_template,
            "preserve_exact_boolean_equation": True,
            "preserve_registered_state_and_control": True,
            "architectural_latency_delta_cycles": 0,
        },
        "proof": {
            "status": "proved_for_measurement_probe",
            "exact_live_structural_contract_recovered": True,
            "clock_ce_reset_init_preserved": True,
            "architectural_latency_preserved": True,
            "formal_whole_dcp_equivalence": False,
            "whole_dcp_equivalence_required_before_submission": True,
        },
        "authority": "pre_mutation_only",
    }
    authorization["certificate_id"] = _digest(authorization)
    return authorization


def build_banked_ram_operator_emitted_attestation(
    authorization: dict[str, Any],
    candidate_dcp_sha256: str,
    route_facts: dict[str, Any],
    target: str,
) -> dict[str, Any]:
    """Bind one reopened routed RAM-data/control localization candidate."""
    _validate_identity(candidate_dcp_sha256, "banked RAM operator candidate DCP")
    source = authorization.get("source") or {}
    spec = authorization.get("spec") or {}
    _validate_identity(source.get("dcp_sha256"), "banked RAM source DCP")
    _validate_identity(source.get("region_sha256"), "banked RAM source region")
    expected = {
        "registered_ram_boolean_midpoint_relocation_v1": (
            "REGISTERED_RAM_BOOLEAN_MIDPOINT",
            "registered_ram_boolean_midpoint_relocation_v1",
        ),
        "registered_control_packet_localization_v1": (
            "BANKED_RAM_CONTROL_PACKET",
            "registered_control_packet_localization_v1",
        ),
    }.get(target)
    if (
        expected is None
        or route_facts.get("candidate_checkpoint_reopened") is not True
        or route_facts.get("source_checkpoint_reopened") is not True
        or route_facts.get("relevant_nets_fully_routed") is not True
        or route_facts.get("contract") != expected[0]
        or route_facts.get("backend") != expected[1]
        or route_facts.get("route_errors") != 0
        or route_facts.get("proof_failures", 0) != 0
        or route_facts.get("latency_delta_cycles", 0) != 0
    ):
        raise OperatorProofError("banked RAM route facts are incomplete")
    checks = {
        "exact_boolean_contract_replayed": True,
        "registered_state_and_control_preserved": True,
        "architectural_latency_preserved": True,
        "exact_namespace_inventory": True,
        "candidate_checkpoint_reopened": True,
        "source_checkpoint_reopened": True,
        "relevant_nets_fully_routed": True,
    }
    payload = {
        "kind": "operator_rewrite_emitted_topology_attestation",
        "version": 1,
        "status": "proved",
        "authority": "candidate_measurement_only",
        "template": target,
        "source": {
            "dcp_sha256": source["dcp_sha256"],
            "region_sha256": source["region_sha256"],
        },
        "candidate": {"dcp_sha256": candidate_dcp_sha256},
        "authorization_id": authorization.get("certificate_id"),
        "placement_manifest_sha256": _digest({
            "target": target,
            "moved_operator_count": route_facts.get("moved_operator_count"),
            "localized_sink_count": route_facts.get("localized_sink_count"),
        }),
        "topology_manifest_sha256": spec.get("contract_sha256"),
        "checks": checks,
    }
    payload["attestation_id"] = _digest(payload)
    return payload


def build_commutative_reassociation_authorization(
    source_dcp_sha256: str,
    contract: dict[str, Any],
) -> dict[str, Any]:
    """Bind a complete-word, control-closed commutative reassociation."""
    from .commutative_reassociation import verify_commutative_reassociation_contract

    _validate_identity(source_dcp_sha256, "commutative reassociation source DCP")
    if not verify_commutative_reassociation_contract(
        contract, source_dcp_sha256=source_dcp_sha256
    ):
        raise OperatorProofError("commutative reassociation contract did not replay")
    public = contract.get("public") or {}
    private = contract.get("private") or {}
    region_sha256 = private.get("cone_connectivity_digest")
    _validate_identity(region_sha256, "commutative reassociation source region")
    if (
        public.get("operator_class") != "REGISTERED_COMMUTATIVE_MODULAR_REDUCTION"
        or public.get("clock_ce_reset_transaction_closed") is not True
        or public.get("primitive_replay_passed") is not True
        or public.get("endpoint_cycle_preserved") is not True
        or public.get("outside_side_fanout_count") != 0
        or not isinstance(private.get("candidate_pairs"), list)
        or not private["candidate_pairs"]
    ):
        raise OperatorProofError("commutative reassociation value/control closure is incomplete")
    authorization = {
        "kind": "operator_rewrite_authorization",
        "version": AUTHORIZATION_VERSION,
        "rewrite_kind": "commutative_operand_bus_slot_reassociation",
        "source": {
            "dcp_sha256": source_dcp_sha256,
            "region_sha256": region_sha256,
        },
        "spec": {
            "contract": json.loads(json.dumps(contract, sort_keys=True, allow_nan=False)),
        },
        "plan": {
            "template": "COMMUTATIVE_OPERAND_BUS_SLOT_REASSOCIATION_V1",
            "complete_operand_buses_only": True,
            "preserve_registered_control_transaction": True,
            "architectural_latency_delta_cycles": 0,
        },
        "proof": {
            "status": "proved_for_measurement_probe",
            "exact_primitive_replay_guard": True,
            "clock_ce_reset_transaction_closed": True,
            "outside_side_fanout_count": 0,
            "formal_whole_dcp_equivalence": False,
            "whole_dcp_equivalence_required_before_submission": True,
        },
        "authority": "pre_mutation_only",
    }
    authorization["certificate_id"] = _digest(authorization)
    return authorization


def build_commutative_reassociation_emitted_attestation(
    authorization: dict[str, Any],
    candidate_dcp_sha256: str,
    route_facts: dict[str, Any],
) -> dict[str, Any]:
    """Bind one reopened, changed-net-only complete-bus candidate."""
    _validate_identity(candidate_dcp_sha256, "commutative candidate DCP")
    source = authorization.get("source") or {}
    spec = authorization.get("spec") or {}
    _validate_identity(source.get("dcp_sha256"), "commutative source DCP")
    _validate_identity(source.get("region_sha256"), "commutative source region")
    if (
        route_facts.get("candidate_checkpoint_reopened") is not True
        or route_facts.get("source_checkpoint_reopened") is not True
        or route_facts.get("relevant_nets_fully_routed") is not True
        or route_facts.get("primitive_replay_passed") is not True
        or route_facts.get("registered_control_transaction_preserved") is not True
        or route_facts.get("contract")
        != "COMMUTATIVE_OPERAND_BUS_SLOT_REASSOCIATION"
        or route_facts.get("backend")
        != "commutative_operand_bus_slot_reassociation_v1"
        or route_facts.get("route_errors") != 0
        or route_facts.get("latency_delta_cycles") != 0
    ):
        raise OperatorProofError("commutative reassociation route facts are incomplete")
    checks = {
        "complete_operand_bus_permutation_replayed": True,
        "primitive_relation_replayed": True,
        "registered_control_transaction_preserved": True,
        "architectural_latency_preserved": True,
        "exact_namespace_inventory": True,
        "candidate_checkpoint_reopened": True,
        "source_checkpoint_reopened": True,
        "relevant_nets_fully_routed": True,
    }
    payload = {
        "kind": "operator_rewrite_emitted_topology_attestation",
        "version": 1,
        "status": "proved",
        "authority": "candidate_measurement_only",
        "template": "commutative_operand_bus_slot_reassociation_v1",
        "source": {
            "dcp_sha256": source["dcp_sha256"],
            "region_sha256": source["region_sha256"],
        },
        "candidate": {"dcp_sha256": candidate_dcp_sha256},
        "authorization_id": authorization.get("certificate_id"),
        "placement_manifest_sha256": _digest({
            "candidate_index": route_facts.get("candidate_index"),
            "changed_net_count": route_facts.get("changed_net_count"),
            "moved_pin_count": route_facts.get("moved_pin_count"),
        }),
        "topology_manifest_sha256": (spec.get("contract") or {}).get(
            "contract_sha256"
        ),
        "checks": checks,
    }
    payload["attestation_id"] = _digest(payload)
    return payload


def build_recurrence_cut_authorization(
    source_dcp_sha256: str,
    source_region_sha256: str,
    spec: RecurrenceCutSpec,
    plan: RecurrenceCutPlan,
    *,
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    proof = prove_recurrence_cut_contract(spec, plan, timeout_ms=timeout_ms)
    return _build_authorization(
        "dsp_recurrence_cut_transport", source_dcp_sha256,
        source_region_sha256, spec, plan, proof,
    )


def _replay_arithmetic_compressor_certificate(
    certificate: dict[str, Any],
    *,
    expected_dcp_sha256: str,
    expected_region_sha256: str,
    timeout_ms: int,
) -> dict[str, Any]:
    from .compressor_proof import (  # local import avoids proof-module cycles
        CERTIFICATE_VERSION,
        _verify_spec_z3,
    )

    if not isinstance(certificate, dict):
        raise OperatorProofError("arithmetic compressor certificate is absent")
    claimed = certificate.get("certificate_id")
    payload = dict(certificate)
    payload.pop("certificate_id", None)
    source = certificate.get("source_identity") or {}
    proof = certificate.get("proof") or {}
    spec = certificate.get("compressor_spec")
    if (
        certificate.get("kind") != "register_cone_compressor_certificate"
        or certificate.get("version") != CERTIFICATE_VERSION
        or not isinstance(claimed, str)
        or _digest(payload) != claimed
        or source.get("dcp_sha256") != expected_dcp_sha256
        or source.get("connectivity_sha256") != expected_region_sha256
        or certificate.get("status") != "proved"
        or certificate.get("mutation_ready") is not True
        or proof.get("emitted_topology_solver_result") != "proved"
        or not isinstance(spec, dict)
        or _verify_spec_z3(
            certificate.get("weighted_sum") or {}, spec, timeout_ms
        ) != "proved"
    ):
        raise OperatorProofError("arithmetic compressor certificate did not replay")
    return {
        "status": "proved",
        "certificate_version": CERTIFICATE_VERSION,
        "certificate_id": claimed,
        "emitted_topology_solver_result": "proved",
    }


def build_arithmetic_compressor_authorization(
    certificate: dict[str, Any], *, timeout_ms: int = 120_000
) -> dict[str, Any]:
    """Adapt a fresh exact-cone certificate to the operator proof contract."""
    source = certificate.get("source_identity") if isinstance(certificate, dict) else None
    if not isinstance(source, dict):
        raise OperatorProofError("arithmetic compressor source identity is absent")
    source_dcp_sha256 = source.get("dcp_sha256")
    source_region_sha256 = source.get("connectivity_sha256")
    _validate_identity(source_dcp_sha256, "compressor source DCP")
    _validate_identity(source_region_sha256, "compressor source region")
    proof = _replay_arithmetic_compressor_certificate(
        certificate,
        expected_dcp_sha256=source_dcp_sha256,
        expected_region_sha256=source_region_sha256,
        timeout_ms=timeout_ms,
    )
    authorization = {
        "kind": "operator_rewrite_authorization",
        "version": AUTHORIZATION_VERSION,
        "rewrite_kind": "bounded_arithmetic_cone_compression",
        "source": {
            "dcp_sha256": source_dcp_sha256,
            "region_sha256": source_region_sha256,
        },
        "spec": {
            "certificate_version": proof["certificate_version"],
            "certificate_id": proof["certificate_id"],
            "modulus_width": int(certificate["weighted_sum"]["modulus_width"]),
            "polynomial_degree": int(
                certificate["weighted_sum"]["polynomial_degree"]
            ),
            "final_row_count": int(
                certificate["compressor_spec"]["final_row_count"]
            ),
            "emitted_topology": certificate["proof"]["emitted_topology"],
        },
        "plan": {
            "template": "ARITHMETIC_COMPRESSOR_BOUNDARY_V1",
            "certificate_id": certificate["certificate_id"],
            "compressor_count": int(
                certificate["compressor_spec"]["compressor_count"]
            ),
            "target_width": int(certificate["weighted_sum"]["modulus_width"]),
        },
        "proof": proof,
        "authority": "pre_mutation_only",
    }
    authorization["certificate_id"] = _digest(authorization)
    return authorization


def recurrence_spec_from_extracted_facts(
    facts: dict[str, Any],
    *,
    transport_valid_transactions: Optional[int] = None,
) -> RecurrenceCutSpec:
    """Derive proof width from hash-bound connectivity facts, never a caller guess."""
    if not isinstance(facts, dict):
        raise OperatorProofError("recurrence facts must be an object")
    if facts.get("kind") != "dsp_recurrence_contract_facts" or facts.get("version") != 2:
        raise OperatorProofError("unsupported recurrence facts schema")
    if facts.get("status") != "success" or facts.get("proof_status") not in {
        "extracted_unproved", "rewrite_facts_extracted_unproved"
    }:
        raise OperatorProofError("recurrence facts are incomplete or have invalid authority")
    if facts.get("boundary_extraction_complete") is not True or facts.get(
        "proof_input_eligible"
    ) is not True:
        raise OperatorProofError("recurrence boundary extraction is incomplete")
    if facts.get("mutation_eligible") is not False:
        raise OperatorProofError("boundary facts have invalid pre-mutation authority")
    claimed_region = facts.get("region_sha256")
    payload = dict(facts)
    payload.pop("region_sha256", None)
    for wrapper_key in ("candidate_id", "fresh_rediscovery", "runtime_s"):
        payload.pop(wrapper_key, None)
    if not isinstance(claimed_region, str) or _digest(payload) != claimed_region:
        raise OperatorProofError("recurrence region digest mismatch")
    state_width = facts.get("state_width")
    _require_int("recovered state_width", state_width, 2, 256)
    relay_bits = facts.get("relay_bits")
    if not isinstance(relay_bits, list) or [
        item.get("index") for item in relay_bits if isinstance(item, dict)
    ] != list(range(state_width)):
        raise OperatorProofError("recovered relay bit mapping is incomplete")
    controls = facts.get("controls")
    if not isinstance(controls, dict) or set(controls) != {
        "clock_net", "ce_net", "reset_net"
    } or any(not isinstance(value, str) or not value for value in controls.values()):
        raise OperatorProofError("recovered relay controls are incomplete")
    groups = facts.get("control_equivalence_groups")
    if not isinstance(groups, dict) or set(groups) != {"C", "CE", "R"}:
        raise OperatorProofError("recovered relay control groups are incomplete")
    for name, group in groups.items():
        if not isinstance(group, dict):
            raise OperatorProofError(f"relay {name} control group is invalid")
        claimed = group.get("group_sha256")
        group_payload = dict(group)
        group_payload.pop("group_sha256", None)
        members = group.get("members")
        comparisons = group.get("comparisons")
        if (
            group.get("status") != "proved"
            or group.get("complete_bit_coverage") is not True
            or group.get("representative_bit") != 0
            or not isinstance(claimed, str)
            or _digest(group_payload) != claimed
            or not isinstance(members, list)
            or [item.get("bit") for item in members if isinstance(item, dict)]
            != list(range(state_width))
            or not isinstance(comparisons, list)
            or [item.get("bit") for item in comparisons if isinstance(item, dict)]
            != list(range(state_width))
            or any(item.get("status") != "proved" for item in comparisons)
        ):
            raise OperatorProofError(f"relay {name} control group proof is invalid")
    endpoint = facts.get("relay_endpoint_contract")
    if (
        not isinstance(endpoint, dict)
        or endpoint.get("status") != "proved"
        or endpoint.get("primitive") != "FDRE"
        or endpoint.get("init") != 0
        or endpoint.get("inversions") != {
            "clock_inverted": False,
            "ce_inverted": False,
            "reset_inverted": False,
        }
    ):
        raise OperatorProofError("relay endpoint semantics are unsupported")
    rewrite_plan = facts.get("rewrite_plan")
    if isinstance(rewrite_plan, dict):
        recovered_delay = rewrite_plan.get("transport_valid_transactions")
        _require_int("recovered transport delay", recovered_delay, 1, 256)
        if transport_valid_transactions is None:
            transport_valid_transactions = recovered_delay
        elif recovered_delay != transport_valid_transactions:
            raise OperatorProofError(
                "requested transport delay differs from recovered cascade plan"
            )
    if transport_valid_transactions is None:
        raise OperatorProofError(
            "transport delay must come from a recovered rewrite plan"
        )
    return RecurrenceCutSpec(
        state_width=state_width,
        transport_valid_transactions=transport_valid_transactions,
    )


def build_recurrence_authorization_from_facts(
    facts: dict[str, Any],
    *,
    transport_valid_transactions: Optional[int] = None,
    plan: RecurrenceCutPlan,
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    """Build pre-mutation authorization using exact recovered region identity."""
    spec = recurrence_spec_from_extracted_facts(
        facts, transport_valid_transactions=transport_valid_transactions
    )
    dcp_sha256 = facts.get("design_sha256")
    region_sha256 = facts.get("region_sha256")
    _validate_identity(dcp_sha256, "facts.design_sha256")
    _validate_identity(region_sha256, "facts.region_sha256")
    return build_recurrence_cut_authorization(
        dcp_sha256, region_sha256, spec, plan, timeout_ms=timeout_ms
    )


def build_recurrence_family_authorization_from_facts(
    facts: dict[str, Any],
    *,
    plan: RecurrenceCutPlan,
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    """Authorize a common recurrence plan against a freshly recovered family."""
    if (
        not isinstance(facts, dict)
        or facts.get("kind") != "dsp_recurrence_family_contract_facts"
        or facts.get("version") != 1
        or facts.get("status") != "success"
        or facts.get("proof_status") != "family_rewrite_facts_extracted_unproved"
        or facts.get("mutation_eligible") is not False
        or facts.get("fresh_rediscovery") is not True
    ):
        raise OperatorProofError("recurrence family facts are incomplete")
    claimed = facts.get("family_contract_sha256")
    payload = dict(facts)
    for key in ("family_contract_sha256", "family_id", "fresh_rediscovery", "runtime_s"):
        payload.pop(key, None)
    if not isinstance(claimed, str) or _digest(payload) != claimed:
        raise OperatorProofError("recurrence family contract digest mismatch")
    boundaries = facts.get("boundaries")
    boundary_digests = facts.get("boundary_region_sha256s")
    count = facts.get("boundary_count")
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or count < 2
        or not isinstance(boundaries, list)
        or len(boundaries) != count
        or not isinstance(boundary_digests, list)
        or [item.get("region_sha256") for item in boundaries] != boundary_digests
    ):
        raise OperatorProofError("recurrence family boundary coverage is incomplete")
    composition = facts.get("composition_contract")
    if not isinstance(composition, dict):
        raise OperatorProofError("recurrence family composition contract is absent")
    composition_payload = dict(composition)
    composition_digest = composition_payload.pop("contract_sha256", None)
    if (
        not isinstance(composition_digest, str)
        or _digest(composition_payload) != composition_digest
        or any(value is not True for value in composition_payload.values())
    ):
        raise OperatorProofError("recurrence family composition was not proved")
    width = facts.get("state_width")
    delay = facts.get("transport_valid_transactions")
    _require_int("family state_width", width, 2, 256)
    _require_int("family transport delay", delay, 1, 63)
    if (
        plan.state_width != width
        or plan.transport_valid_transactions != delay
        or plan.mask_valid_transactions != delay
    ):
        raise OperatorProofError("recurrence family plan differs from recovered facts")
    dcp_sha256 = facts.get("design_sha256")
    _validate_identity(dcp_sha256, "family design_sha256")
    return build_recurrence_cut_authorization(
        dcp_sha256,
        claimed,
        RecurrenceCutSpec(
            state_width=width,
            transport_valid_transactions=delay,
        ),
        plan,
        timeout_ms=timeout_ms,
    )


def verify_rewrite_authorization(
    certificate: dict[str, Any],
    *,
    expected_dcp_sha256: str,
    expected_region_sha256: str,
    timeout_ms: int = 5000,
) -> bool:
    """Replay the proof; a self-consistent hash alone is never sufficient."""
    try:
        if not isinstance(certificate, dict) or certificate.get("kind") != "operator_rewrite_authorization":
            return False
        if certificate.get("version") != AUTHORIZATION_VERSION:
            return False
        if certificate.get("authority") != "pre_mutation_only":
            return False
        claimed_id = certificate.get("certificate_id")
        payload = dict(certificate)
        payload.pop("certificate_id", None)
        if not isinstance(claimed_id, str) or _digest(payload) != claimed_id:
            return False
        source = certificate.get("source") or {}
        if source != {
            "dcp_sha256": expected_dcp_sha256,
            "region_sha256": expected_region_sha256,
        }:
            return False
        rewrite_kind = certificate.get("rewrite_kind")
        # The union module also re-exports the pre-integration registered
        # product-sum authorization builder.  Its REGARITH certificate has a
        # deliberately different schema from the teammate fabric-to-DSP
        # measurement admission, so replay it with its owning v1 verifier.
        if rewrite_kind == "registered_arithmetic_cone":
            from .operator_proof_current import (
                verify_rewrite_authorization as verify_current_authorization,
            )

            return verify_current_authorization(
                certificate,
                expected_dcp_sha256=expected_dcp_sha256,
                expected_region_sha256=expected_region_sha256,
                timeout_ms=timeout_ms,
            )
        if rewrite_kind == "fabric_to_dsp":
            raw_spec = certificate["spec"]
            raw_plan = certificate["plan"]
            if isinstance(raw_spec, dict) and raw_spec.get("registered_product_closure") is True:
                if (
                    set(raw_spec) != {
                        "registered_product_closure", "bundle_sha256",
                        "module_count", "instance_count", "plans",
                    }
                    or _SHA256_RE.fullmatch(str(raw_spec.get("bundle_sha256") or "")) is None
                    or not isinstance(raw_spec.get("module_count"), int)
                    or raw_spec["module_count"] < 1
                    or not isinstance(raw_spec.get("instance_count"), int)
                    or raw_spec["instance_count"] < 1
                    or not isinstance(raw_spec.get("plans"), list)
                    or len(raw_spec["plans"]) != raw_spec["module_count"]
                    or raw_plan != {
                        "template": "DSP48E2_MUL_SHIFT_ACCUM_V1",
                        "target_registers_preserved": False,
                        "registered_schedule_rebuilt": True,
                        "bundle_sha256": raw_spec["bundle_sha256"],
                    }
                ):
                    return False
                uniform_frontier = all(
                    isinstance(item, dict)
                    and item.get("schedule_kind") == "registered_product_sum_v1"
                    for item in raw_spec["plans"]
                )
                replay = {
                    "status": "proved_for_measurement_probe",
                    "composed_boolean_replay": True,
                    "uniform_registered_frontier": uniform_frontier,
                    "schedule_controls_recovered": True,
                    "external_plan_used": False,
                    "formal_whole_dcp_equivalence": False,
                    "whole_dcp_equivalence_required_before_submission": True,
                }
            elif isinstance(raw_spec, dict) and "source_contract" in raw_spec:
                replay_authorization = build_fixed_point_contract_authorization(
                    raw_spec["source_contract"]
                )
                return (
                    replay_authorization == certificate
                    and raw_plan.get("template") == "DSP48E2_MUL_SHIFT_ACCUM_V1"
                )
            else:
                spec = FixedPointProductSpec(**raw_spec)
                plan = FixedPointDspPlan(**raw_plan)
                replay = prove_fixed_point_rewrite(spec, plan, timeout_ms=timeout_ms)
        elif rewrite_kind == "dsp_recurrence_cut_transport":
            spec = RecurrenceCutSpec(**certificate["spec"])
            raw_plan = dict(certificate["plan"])
            raw_plan["srl_segment_depths"] = tuple(raw_plan["srl_segment_depths"])
            plan = RecurrenceCutPlan(**raw_plan)
            replay = prove_recurrence_cut_contract(spec, plan, timeout_ms=timeout_ms)
        elif rewrite_kind == "bounded_arithmetic_cone_compression":
            spec = certificate.get("spec") or {}
            plan = certificate.get("plan") or {}
            certificate_id = spec.get("certificate_id")
            if (
                not isinstance(certificate_id, str)
                or _SHA256_RE.fullmatch(certificate_id) is None
                or set(spec) != {
                    "certificate_version", "certificate_id", "modulus_width",
                    "polynomial_degree", "final_row_count", "emitted_topology",
                }
                or spec.get("certificate_version") != 6
                or not isinstance(spec.get("modulus_width"), int)
                or not 1 <= spec["modulus_width"] <= 4096
                or spec.get("polynomial_degree") not in (1, 2)
                or spec.get("final_row_count") != 3
                or spec.get("emitted_topology")
                != "LUT6_2_CSA3_PLUS_LUT2_XOR_DI_PLUS_CARRY8_SINGLE_CY8"
                or set(plan) != {
                    "template", "certificate_id", "compressor_count",
                    "target_width",
                }
                or plan.get("template") != "ARITHMETIC_COMPRESSOR_BOUNDARY_V1"
                or plan.get("certificate_id") != certificate_id
                or not isinstance(plan.get("compressor_count"), int)
                or plan["compressor_count"] < 0
                or plan.get("target_width") != spec["modulus_width"]
            ):
                return False
            replay = {
                "status": "proved",
                "certificate_version": spec["certificate_version"],
                "certificate_id": certificate_id,
                "emitted_topology_solver_result": "proved",
            }
        elif rewrite_kind == "consumer_driven_arithmetic_rewrite":
            raw_spec = certificate.get("spec") or {}
            raw_plan = certificate.get("plan") or {}
            if (
                set(raw_spec) != {
                    "consumer_contract", "contract_sha256", "bundle_sha256",
                    "module_count", "instance_count", "plans",
                }
                or raw_spec.get("consumer_contract") is not True
                or _SHA256_RE.fullmatch(
                    str(raw_spec.get("contract_sha256") or "")
                ) is None
                or _SHA256_RE.fullmatch(
                    str(raw_spec.get("bundle_sha256") or "")
                ) is None
                or not isinstance(raw_spec.get("module_count"), int)
                or raw_spec["module_count"] < 1
                or not isinstance(raw_spec.get("instance_count"), int)
                or raw_spec["instance_count"] < 1
                or not isinstance(raw_spec.get("plans"), list)
                or len(raw_spec["plans"]) != raw_spec["module_count"]
                or raw_plan != {
                    "template": "CONSUMER_DRIVEN_ARITHMETIC_REWRITE_V1",
                    "registered_schedule_rebuilt": True,
                    "bundle_sha256": raw_spec["bundle_sha256"],
                }
            ):
                return False
            replay = {
                "status": "proved_for_measurement_probe",
                "live_consumer_boundary_recovered": True,
                "registered_schedule_recovered": True,
                "object_identity_private": True,
                "external_plan_used": False,
                "formal_whole_dcp_equivalence": False,
                "whole_dcp_equivalence_required_before_submission": True,
            }
        elif rewrite_kind == "banked_select_predicate_specialization":
            raw_spec = certificate.get("spec") or {}
            replayed = build_banked_predicate_authorization(
                expected_dcp_sha256,
                raw_spec.get("contract") if isinstance(raw_spec, dict) else None,
            )
            return replayed == certificate
        elif rewrite_kind == "low_fanout_registered_dsp_lane_rebuild":
            raw_spec = certificate.get("spec") or {}
            replayed = build_registered_dsp_lane_authorization(
                expected_dcp_sha256,
                raw_spec.get("contract") if isinstance(raw_spec, dict) else None,
            )
            return replayed == certificate
        elif rewrite_kind == "registered_observable_dead_state_elimination":
            raw_spec = certificate.get("spec") or {}
            replayed = build_registered_dead_state_authorization(
                expected_dcp_sha256,
                raw_spec.get("contract") if isinstance(raw_spec, dict) else None,
            )
            return replayed == certificate
        elif rewrite_kind == "exact_control_to_data_state_equation":
            raw_spec = certificate.get("spec") or {}
            replayed = build_control_state_equation_authorization(
                expected_dcp_sha256,
                raw_spec.get("contract") if isinstance(raw_spec, dict) else None,
                raw_spec.get("family_ref") if isinstance(raw_spec, dict) else None,
            )
            return replayed == certificate
        elif rewrite_kind == "direct_ff_chain_to_srl_boundary_bridge":
            raw_spec = certificate.get("spec") or {}
            replayed = build_ff_srl_boundary_bridge_authorization(
                expected_dcp_sha256,
                raw_spec.get("contract") if isinstance(raw_spec, dict) else None,
                raw_spec.get("family_ref") if isinstance(raw_spec, dict) else None,
            )
            return replayed == certificate
        elif rewrite_kind == "memory_write_buffer_forward":
            return replay_memory_forward_authorization(certificate) is not None
        elif rewrite_kind == "constructive_accumulator_rebuild":
            return verify_constructive_accumulator_authorization(certificate)
        elif rewrite_kind == (
            "multilevel_registered_control_distribution_localization"
        ):
            raw_spec = certificate.get("spec") or {}
            replayed = build_multilevel_control_localization_authorization(
                expected_dcp_sha256,
                raw_spec.get("contract") if isinstance(raw_spec, dict) else None,
                raw_spec.get("family_ref") if isinstance(raw_spec, dict) else None,
            )
            return replayed == certificate
        elif rewrite_kind == "multilevel_ram_control_distribution_localization":
            raw_spec = certificate.get("spec") or {}
            rebuilt = build_multilevel_ram_control_localization_authorization(
                expected_dcp_sha256,
                raw_spec.get("contract") if isinstance(raw_spec, dict) else None,
                raw_spec.get("family_ref") if isinstance(raw_spec, dict) else None,
            )
            return rebuilt == certificate
        elif rewrite_kind == "registered_gf2_parity_reassociation":
            raw_spec = certificate.get("spec") or {}
            replayed = build_registered_parity_authorization(
                expected_dcp_sha256,
                raw_spec.get("contract") if isinstance(raw_spec, dict) else None,
                raw_spec.get("family_ref") if isinstance(raw_spec, dict) else None,
                raw_spec.get("implementation_mode")
                if isinstance(raw_spec, dict) else None,
            )
            return replayed == certificate
        elif rewrite_kind in {
            "registered_ram_boolean_midpoint_relocation",
            "registered_control_packet_localization",
        }:
            raw_spec = certificate.get("spec") or {}
            raw_contract = raw_spec.get("contract") if isinstance(raw_spec, dict) else None
            if not isinstance(raw_contract, dict):
                return False
            scope = raw_contract.get("scope")
            operator = raw_contract.get("operator")
            if scope not in {"data_operator", "control_operator"}:
                return False
            replay_contract = {
                "operator_class": raw_contract.get("operator_class"),
                "data_operator": operator if scope == "data_operator" else {},
                "control_operator": operator if scope == "control_operator" else {},
            }
            replayed = build_banked_ram_operator_authorization(
                expected_dcp_sha256,
                replay_contract,
                raw_spec.get("target"),
            )
            return replayed == certificate
        elif rewrite_kind == "commutative_operand_bus_slot_reassociation":
            raw_spec = certificate.get("spec") or {}
            contract = raw_spec.get("contract") if isinstance(raw_spec, dict) else None
            if not isinstance(contract, dict):
                return False
            replayed = build_commutative_reassociation_authorization(
                expected_dcp_sha256, contract
            )
            return replayed == certificate
        else:
            return False
        return (
            replay == certificate.get("proof")
            and replay.get("status") in {
                "proved",
                "proved_for_measurement_probe",
            }
        )
    except (KeyError, TypeError, ValueError, OperatorProofError):
        return False


_STRICT_VALIDATION_FIELDS = {
    "schema_version",
    "strict_mode",
    "validator_protocol",
    "stimulus_protocol",
    "vectors",
    "precheck_vectors",
    "root_sha256",
    "root_sha256_after",
    "root_size_bytes",
    "candidate_sha256",
    "candidate_sha256_after",
    "candidate_size_bytes",
    "phase1_passed",
    "phase2_passed",
    "phase2_skipped",
    "infrastructure_failure",
    "process_exit_code",
}
_STRICT_VALIDATION_OPTIONAL_FIELDS = {"error"}

_EMITTED_ATTESTATION_FIELDS = {
    "kind", "version", "status", "authority", "template", "source",
    "candidate", "authorization_id", "placement_manifest_sha256",
    "topology_manifest_sha256", "checks", "attestation_id",
}
_EMITTED_ATTESTATION_CHECKS_BY_TEMPLATE = {
    "dsp_srl_recurrence_cut_v1": {
        "joint_connected_topology_replayed", "all_addresses_replayed",
        "exact_namespace_inventory", "candidate_checkpoint_reopened",
        "source_checkpoint_reopened", "relevant_nets_fully_routed",
    },
    "fixed_point_dsp_rebuild_v1": {
        "fixed_point_dsp_topology_replayed", "source_contract_replayed",
        "target_register_contract_replayed", "exact_namespace_inventory",
        "candidate_checkpoint_reopened", "source_checkpoint_reopened",
        "relevant_nets_fully_routed",
    },
    "exact_control_to_data_state_equation_v1": {
        "exact_truth_table_replayed",
        "care_set_support_replayed",
        "private_manifest_identity_replayed",
        "registered_control_transaction_preserved",
        "architectural_latency_preserved",
        "clock_identity_preserved",
        "candidate_checkpoint_reopened",
        "source_checkpoint_reopened",
        "relevant_nets_fully_routed",
    },
    "ff_srl_boundary_bridge_v1": {
        "private_manifest_identity_replayed",
        "direct_chain_topology_replayed",
        "hierarchy_boundary_ff_retained",
        "enabled_cycle_latency_preserved",
        "clock_identity_preserved",
        "candidate_checkpoint_reopened",
        "source_checkpoint_reopened",
        "relevant_nets_fully_routed",
    },
    "memory_write_buffer_forward_v1": {
        "local_relational_memory_miter_replayed",
        "private_family_identity_replayed",
        "write_pipeline_and_read_forwarding_topology_replayed",
        "exact_resource_accounting",
        "clock_identity_preserved",
        "candidate_checkpoint_reopened",
        "source_checkpoint_reopened",
        "relevant_nets_fully_routed",
    },
    "multilevel_control_distribution_localization_v1": {
        "private_manifest_identity_replayed",
        "exact_boolean_dag_cloned",
        "registered_sink_role_and_cycle_preserved",
        "original_generator_and_nonselected_consumers_preserved",
        "placement_fix_identity_preserved",
        "clock_identity_preserved",
        "candidate_checkpoint_reopened",
        "source_checkpoint_reopened",
        "relevant_nets_fully_routed",
    },
    "multilevel_ram_control_distribution_localization_v1": {
        "private_manifest_identity_preserved",
        "exact_boolean_dag_cloned",
        "ram_primitive_and_property_identity_preserved",
        "ram_control_pin_role_preserved",
        "selected_ram_control_sinks_only_rewired",
        "zero_cycle_combinational_schedule_preserved",
        "original_generator_and_nonselected_consumers_preserved",
        "placement_fix_identity_preserved",
        "clock_identity_preserved",
        "candidate_checkpoint_reopened",
        "source_checkpoint_reopened",
        "relevant_nets_fully_routed",
    },
    "registered_gf2_parity_reassociation_v1": {
        "private_manifest_identity_replayed",
        "exact_gf2_expression_replayed",
        "balanced_xor_tree_emitted",
        "selected_registered_boundary_preserved",
        "original_root_and_nonselected_consumers_preserved",
        "all_nonselected_consumer_sink_set_preserved",
        "placement_fix_identity_preserved",
        "clock_identity_preserved",
        "candidate_checkpoint_reopened",
        "source_checkpoint_reopened",
        "relevant_nets_fully_routed",
    },
    "constructive_accumulator_rebuild_v1": {
        "constructive_expression_replayed",
        "emitted_compressor_topology_replayed",
        "complete_selected_family_replaced",
        "target_register_cycle_preserved",
        "clock_identity_preserved",
        "candidate_checkpoint_reopened",
        "source_checkpoint_reopened",
        "relevant_nets_fully_routed",
    },
    "consumer_driven_registered_arithmetic_rewrite_v1": {
        "consumer_boundary_topology_replayed", "source_contract_replayed",
        "registered_schedule_replayed", "exact_namespace_inventory",
        "candidate_checkpoint_reopened", "source_checkpoint_reopened",
        "relevant_nets_fully_routed",
    },
    "registered_fir_data_cone_rewrite": {
        "fixed_point_dsp_topology_replayed", "source_contract_replayed",
        "target_register_contract_replayed", "exact_namespace_inventory",
        "candidate_checkpoint_reopened", "source_checkpoint_reopened",
        "relevant_nets_fully_routed",
    },
    "proved_arithmetic_compressor_v1": {
        "emitted_lut2_carry8_topology_replayed",
        "exact_namespace_inventory",
        "target_driver_cardinality_replayed",
        "candidate_checkpoint_reopened",
        "source_checkpoint_reopened",
        "relevant_nets_fully_routed",
    },
    "bank_local_predicate_specialization_v1": {
        "banked_predicate_topology_replayed",
        "registered_bank_sources_replayed",
        "local_truth_table_cofactors_preserved",
        "global_consumer_preserved",
        "exact_namespace_inventory",
        "candidate_checkpoint_reopened",
        "source_checkpoint_reopened",
        "relevant_nets_fully_routed",
    },
    "registered_dsp_lane_rebuild_v1": {
        "complete_p_to_ff_word_mapping_replayed",
        "preg_reset_history_topology_replayed",
        "registered_control_transaction_preserved",
        "architectural_latency_preserved",
        "exact_resource_accounting",
        "candidate_checkpoint_reopened",
        "source_checkpoint_reopened",
        "relevant_nets_fully_routed",
    },
    "registered_dead_state_elimination_v1": {
        "output_observable_closure_replayed",
        "private_manifest_identity_replayed",
        "partial_hard_macro_parents_preserved",
        "exact_resource_accounting",
        "clock_identity_preserved",
        "candidate_checkpoint_reopened",
        "source_checkpoint_reopened",
        "relevant_nets_fully_routed",
    },
    "registered_ram_boolean_midpoint_relocation_v1": {
        "exact_boolean_contract_replayed",
        "registered_state_and_control_preserved",
        "architectural_latency_preserved",
        "exact_namespace_inventory",
        "candidate_checkpoint_reopened",
        "source_checkpoint_reopened",
        "relevant_nets_fully_routed",
    },
    "registered_control_packet_localization_v1": {
        "exact_boolean_contract_replayed",
        "registered_state_and_control_preserved",
        "architectural_latency_preserved",
        "exact_namespace_inventory",
        "candidate_checkpoint_reopened",
        "source_checkpoint_reopened",
        "relevant_nets_fully_routed",
    },
    "commutative_operand_bus_slot_reassociation_v1": {
        "complete_operand_bus_permutation_replayed",
        "primitive_relation_replayed",
        "registered_control_transaction_preserved",
        "architectural_latency_preserved",
        "exact_namespace_inventory",
        "candidate_checkpoint_reopened",
        "source_checkpoint_reopened",
        "relevant_nets_fully_routed",
    },
}


def _validated_emitted_attestation(
    attestation: dict[str, Any],
    *,
    template: str,
    authorization: dict[str, Any],
    expected_source_sha256: str,
    expected_candidate_sha256: str,
) -> dict[str, Any]:
    if not isinstance(attestation, dict) or set(attestation) != _EMITTED_ATTESTATION_FIELDS:
        raise OperatorProofError(
            "emitted topology attestation has missing or unsupported fields"
        )
    claimed_id = attestation.get("attestation_id")
    payload = dict(attestation)
    payload.pop("attestation_id", None)
    if not isinstance(claimed_id, str) or _digest(payload) != claimed_id:
        raise OperatorProofError("emitted topology attestation digest mismatch")
    source = authorization.get("source") or {}
    checks = attestation.get("checks")
    expected_checks = _EMITTED_ATTESTATION_CHECKS_BY_TEMPLATE.get(template)
    if (
        attestation.get("kind")
        != "operator_rewrite_emitted_topology_attestation"
        or attestation.get("version") != 1
        or attestation.get("status") != "proved"
        or attestation.get("authority") != "candidate_measurement_only"
        or attestation.get("template") != template
        or attestation.get("source") != {
            "dcp_sha256": expected_source_sha256,
            "region_sha256": source.get("region_sha256"),
        }
        or attestation.get("candidate")
        != {"dcp_sha256": expected_candidate_sha256}
        or attestation.get("authorization_id") != authorization.get("certificate_id")
        or not isinstance(checks, dict)
        or expected_checks is None
        or set(checks) != expected_checks
        or any(value is not True for value in checks.values())
    ):
        raise OperatorProofError("emitted topology attestation did not prove its contract")
    for field in ("placement_manifest_sha256", "topology_manifest_sha256"):
        _validate_identity(attestation.get(field), f"attestation {field}")
    return json.loads(json.dumps(attestation, sort_keys=True, allow_nan=False))


def _validated_strict_report(
    report: dict[str, Any],
    *,
    expected_source_sha256: str,
    expected_candidate_sha256: str,
) -> dict[str, Any]:
    report_fields = set(report) if isinstance(report, dict) else set()
    if (
        not isinstance(report, dict)
        or not _STRICT_VALIDATION_FIELDS.issubset(report_fields)
        or not report_fields.issubset(
            _STRICT_VALIDATION_FIELDS | _STRICT_VALIDATION_OPTIONAL_FIELDS
        )
    ):
        raise OperatorProofError(
            "post-emission validator report has missing or unsupported fields"
        )
    validator_error = report.get("error")
    if "error" in report and (
        not isinstance(validator_error, str)
        or not validator_error.strip()
        or len(validator_error) > 1000
        or report.get("infrastructure_failure") is not True
    ):
        raise OperatorProofError(
            "post-emission validator error diagnostic is malformed"
        )
    _validate_identity(expected_source_sha256, "expected_source_sha256")
    _validate_identity(expected_candidate_sha256, "expected_candidate_sha256")
    if (
        report.get("schema_version") != 1
        or report.get("strict_mode") is not True
        or report.get("validator_protocol") != STRICT_VALIDATOR_PROTOCOL
        or report.get("stimulus_protocol") != STRICT_STIMULUS_PROTOCOL
    ):
        raise OperatorProofError("post-emission validator protocol mismatch")
    vectors = report.get("vectors")
    precheck_vectors = report.get("precheck_vectors")
    if (
        isinstance(vectors, bool)
        or not isinstance(vectors, int)
        or vectors < MIN_POST_EMISSION_VECTORS
        or isinstance(precheck_vectors, bool)
        or not isinstance(precheck_vectors, int)
        or precheck_vectors < MIN_POST_EMISSION_PRECHECK_VECTORS
        or precheck_vectors > vectors
    ):
        raise OperatorProofError("post-emission validator vector coverage is insufficient")
    for field in ("root_size_bytes", "candidate_size_bytes"):
        value = report.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise OperatorProofError(f"post-emission validator {field} is invalid")
    if (
        report.get("root_sha256") != expected_source_sha256
        or report.get("root_sha256_after") != expected_source_sha256
        or report.get("candidate_sha256") != expected_candidate_sha256
        or report.get("candidate_sha256_after") != expected_candidate_sha256
    ):
        raise OperatorProofError("post-emission validator artifact identity mismatch")
    if (
        report.get("phase1_passed") is not True
        or report.get("phase2_passed") is not True
        or report.get("phase2_skipped") is not False
        or report.get("infrastructure_failure") is not False
        or report.get("process_exit_code") != 0
    ):
        diagnostic = (
            f": {validator_error[:500]}" if isinstance(validator_error, str) else ""
        )
        raise OperatorProofError(
            "post-emission strict validation did not pass" + diagnostic
        )
    return json.loads(json.dumps(report, sort_keys=True, allow_nan=False))


def build_operator_post_emission_certificate(
    template: str,
    authorization: dict[str, Any],
    candidate_dcp_sha256: str,
    strict_validation_report: dict[str, Any],
    *,
    emitted_topology_attestation: dict[str, Any],
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    """Bind replayed template authorization to strict whole-DCP validation."""
    binding = OPERATOR_TEMPLATE_BINDINGS.get(template)
    if binding is None:
        raise OperatorProofError("unsupported public operator rewrite template")
    if not isinstance(authorization, dict):
        raise OperatorProofError("rewrite authorization must be an object")
    source = authorization.get("source")
    plan = authorization.get("plan")
    if not isinstance(source, dict) or not isinstance(plan, dict):
        raise OperatorProofError("rewrite authorization source or plan is invalid")
    source_dcp_sha256 = source.get("dcp_sha256")
    source_region_sha256 = source.get("region_sha256")
    _validate_identity(source_dcp_sha256, "authorization source DCP")
    _validate_identity(source_region_sha256, "authorization source region")
    _validate_identity(candidate_dcp_sha256, "candidate_dcp_sha256")
    if (
        authorization.get("rewrite_kind") != binding["rewrite_kind"]
        or plan.get("template") != binding["proof_template"]
        or not verify_rewrite_authorization(
            authorization,
            expected_dcp_sha256=source_dcp_sha256,
            expected_region_sha256=source_region_sha256,
            timeout_ms=timeout_ms,
        )
    ):
        raise OperatorProofError("rewrite authorization does not replay for template")
    report = _validated_strict_report(
        strict_validation_report,
        expected_source_sha256=source_dcp_sha256,
        expected_candidate_sha256=candidate_dcp_sha256,
    )
    emitted = _validated_emitted_attestation(
        emitted_topology_attestation,
        template=template,
        authorization=authorization,
        expected_source_sha256=source_dcp_sha256,
        expected_candidate_sha256=candidate_dcp_sha256,
    )
    certificate = {
        "kind": "operator_post_emission_validation",
        "version": POST_EMISSION_VERSION,
        "status": "proved",
        "authority": "candidate_measurement_only",
        "template": template,
        "rewrite_kind": binding["rewrite_kind"],
        "source": {
            "dcp_sha256": source_dcp_sha256,
            "region_sha256": source_region_sha256,
        },
        "candidate": {"dcp_sha256": candidate_dcp_sha256},
        "authorization_id": authorization.get("certificate_id"),
        "emitted_topology": emitted,
        "strict_validation": report,
        "claims": {
            "mathematical_template_contract": True,
            "strict_structural_and_bounded_simulation": True,
            "formal_whole_dcp_equivalence": False,
            "final_submission_authority": False,
        },
    }
    certificate["certificate_id"] = _digest(certificate)
    return certificate


def build_operator_candidate_proof(
    template: str,
    authorization: dict[str, Any],
    candidate_dcp_sha256: str,
    *,
    emitted_topology_attestation: dict[str, Any],
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    """Build measurement-only admission; final equivalence remains separate."""
    certificate = build_operator_measurement_admission_certificate(
        template, authorization, candidate_dcp_sha256,
        emitted_topology_attestation=emitted_topology_attestation,
        timeout_ms=timeout_ms,
    )
    source = certificate["source"]["dcp_sha256"]
    return {
        "proof_version": OPERATOR_CANDIDATE_PROOF_VERSION,
        "skill": "operator_rewrite",
        "template": template,
        "source_artifact_sha256": source,
        "artifact_sha256": candidate_dcp_sha256,
        "rewrite_authorization": json.loads(json.dumps(
            authorization, sort_keys=True, allow_nan=False
        )),
        "measurement_admission": certificate,
    }


def build_operator_measurement_admission_certificate(
    template: str,
    authorization: dict[str, Any],
    candidate_dcp_sha256: str,
    *,
    emitted_topology_attestation: dict[str, Any],
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    """Authorize trusted QoR measurement, never final submission.

    The local arithmetic/structural authorization and the emitted-topology
    replay are enough to justify spending time on central PERIOD/WNS and
    legality measurement.  Whole-DCP functional equivalence is deliberately
    deferred to the independent final submission gate, so an XSIM
    infrastructure failure cannot erase useful Fmax exploration evidence.
    """
    binding = OPERATOR_TEMPLATE_BINDINGS.get(template)
    if binding is None or not isinstance(authorization, dict):
        raise OperatorProofError("unsupported public operator rewrite template")
    source = authorization.get("source")
    plan = authorization.get("plan")
    if not isinstance(source, dict) or not isinstance(plan, dict):
        raise OperatorProofError("rewrite authorization source or plan is invalid")
    source_dcp_sha256 = source.get("dcp_sha256")
    source_region_sha256 = source.get("region_sha256")
    _validate_identity(source_dcp_sha256, "authorization source DCP")
    _validate_identity(source_region_sha256, "authorization source region")
    _validate_identity(candidate_dcp_sha256, "candidate_dcp_sha256")
    if (
        authorization.get("rewrite_kind") != binding["rewrite_kind"]
        or plan.get("template") != binding["proof_template"]
        or not verify_rewrite_authorization(
            authorization,
            expected_dcp_sha256=source_dcp_sha256,
            expected_region_sha256=source_region_sha256,
            timeout_ms=timeout_ms,
        )
    ):
        raise OperatorProofError("rewrite authorization does not replay for template")
    emitted = _validated_emitted_attestation(
        emitted_topology_attestation,
        template=template,
        authorization=authorization,
        expected_source_sha256=source_dcp_sha256,
        expected_candidate_sha256=candidate_dcp_sha256,
    )
    certificate = {
        "kind": "operator_measurement_admission",
        "version": MEASUREMENT_ADMISSION_VERSION,
        "status": "proved",
        "authority": "candidate_measurement_only",
        "template": template,
        "rewrite_kind": binding["rewrite_kind"],
        "source": {
            "dcp_sha256": source_dcp_sha256,
            "region_sha256": source_region_sha256,
        },
        "candidate": {"dcp_sha256": candidate_dcp_sha256},
        "authorization_id": authorization.get("certificate_id"),
        "emitted_topology": emitted,
        "claims": {
            "mathematical_template_contract": True,
            "emitted_topology_contract": True,
            "whole_dcp_functional_equivalence": "required_before_submission",
            "final_submission_authority": False,
        },
    }
    certificate["certificate_id"] = _digest(certificate)
    return certificate


def verify_operator_measurement_admission_certificate(
    certificate: dict[str, Any],
    authorization: dict[str, Any],
    *,
    expected_template: str,
    expected_source_sha256: str,
    expected_candidate_sha256: str,
    timeout_ms: int = 5000,
) -> bool:
    """Replay a measurement-only admission certificate exactly."""
    try:
        required = {
            "kind", "version", "status", "authority", "template",
            "rewrite_kind", "source", "candidate", "authorization_id",
            "emitted_topology", "claims", "certificate_id",
        }
        if not isinstance(certificate, dict) or set(certificate) != required:
            return False
        claimed_id = certificate.get("certificate_id")
        payload = dict(certificate)
        payload.pop("certificate_id", None)
        if not isinstance(claimed_id, str) or _digest(payload) != claimed_id:
            return False
        replay = build_operator_measurement_admission_certificate(
            expected_template,
            authorization,
            expected_candidate_sha256,
            emitted_topology_attestation=certificate.get("emitted_topology"),
            timeout_ms=timeout_ms,
        )
        return (
            certificate == replay
            and certificate.get("source", {}).get("dcp_sha256")
            == expected_source_sha256
        )
    except (AttributeError, KeyError, TypeError, ValueError, OperatorProofError):
        return False


def verify_operator_post_emission_certificate(
    certificate: dict[str, Any],
    authorization: dict[str, Any],
    *,
    expected_template: str,
    expected_source_sha256: str,
    expected_candidate_sha256: str,
    timeout_ms: int = 5000,
) -> bool:
    """Replay authorization and strict artifact-bound validation evidence."""
    try:
        required = {
            "kind", "version", "status", "authority", "template",
            "rewrite_kind", "source", "candidate", "authorization_id",
            "emitted_topology", "strict_validation", "claims", "certificate_id",
        }
        if not isinstance(certificate, dict) or set(certificate) != required:
            return False
        claimed_id = certificate.get("certificate_id")
        payload = dict(certificate)
        payload.pop("certificate_id", None)
        if not isinstance(claimed_id, str) or _digest(payload) != claimed_id:
            return False
        replay = build_operator_post_emission_certificate(
            expected_template,
            authorization,
            expected_candidate_sha256,
            certificate.get("strict_validation"),
            emitted_topology_attestation=certificate.get("emitted_topology"),
            timeout_ms=timeout_ms,
        )
        return (
            certificate == replay
            and certificate.get("source", {}).get("dcp_sha256")
            == expected_source_sha256
        )
    except (AttributeError, KeyError, TypeError, ValueError, OperatorProofError):
        return False
# Compatibility exports for current-branch operator executors that are kept
# alongside the teammate operator engine.  The two proof modules retain their
# own schema/version owners; only names absent from the teammate engine are
# re-exported here so existing standalone action helpers continue to import.
from .operator_proof_current import (  # noqa: E402,F401
    MemoryPrimitiveSpec,
    MemoryRepackPlan,
    PipelineRebalancePlan,
    PipelineRebalanceSpec,
    RegisterAbsorbPlan,
    RegisterAbsorbSpec,
    TwoOperandDspPlan,
    TwoOperandProductSpec,
    _PIPELINE_REBALANCE_SET_REGISTERS,
    build_fixed_point_authorization_from_facts,
    build_memory_repack_authorization_from_facts,
    build_pipeline_rebalance_authorization_from_facts,
    build_register_absorb_authorization_from_facts,
    build_registered_product_sum_authorization,
    build_two_operand_authorization_from_facts,
    fixed_point_spec_from_extracted_facts,
    memory_spec_from_extracted_facts,
    pipeline_rebalance_spec_from_extracted_facts,
    register_absorb_spec_from_extracted_facts,
    two_operand_spec_from_extracted_facts,
)


def __getattr__(name: str):
    """Preserve v1 proof imports that are absent from the teammate engine."""
    from . import operator_proof_current

    if hasattr(operator_proof_current, name):
        return getattr(operator_proof_current, name)
    raise AttributeError(name)
