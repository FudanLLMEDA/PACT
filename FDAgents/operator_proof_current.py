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


SEMANTICS_VERSION = "operator-bv-v1"
AUTHORIZATION_VERSION = 1
POST_EMISSION_VERSION = 1
OPERATOR_CANDIDATE_PROOF_VERSION = "operator-rewrite-v1"
STRICT_VALIDATOR_PROTOCOL = "dcp-structural-xsim-v1"
STRICT_STIMULUS_PROTOCOL = "lfsr32-deadbeef-v1"
MIN_POST_EMISSION_VECTORS = 1000
MIN_POST_EMISSION_PRECHECK_VECTORS = 100
_SHA256_RE = re.compile(r"[0-9a-f]{64}")

OPERATOR_TEMPLATE_BINDINGS = {
    "memory_semantic_repack_v1": {
        "rewrite_kind": "memory_semantic_repack",
        "proof_template": "BRAM_MEMORY_SEMANTIC_REPACK_V1",
    },
    "fixed_point_dsp_rebuild_v1": {
        "rewrite_kind": "fabric_to_dsp",
        "proof_template": "DSP48E2_MUL_SHIFT_ACCUM_V1",
    },
    "dsp_srl_recurrence_cut_v1": {
        "rewrite_kind": "dsp_recurrence_cut_transport",
        "proof_template": "DSP_PREG_SRL_RECURRENCE_CUT_V1",
    },
    "dsp_mul_two_operand_v1": {
        "rewrite_kind": "fabric_to_dsp_two_operand",
        "proof_template": "DSP48E2_MUL_TWO_OPERAND_V1",
    },
    "dsp_register_absorb_v1": {
        "rewrite_kind": "fabric_ff_to_dsp_input_reg",
        "proof_template": "DSP48E2_INPUT_REG_ABSORB_V1",
    },
    "dsp_pipeline_rebalance_v1": {
        "rewrite_kind": "dsp_pipeline_rebalance",
        "proof_template": "DSP48E2_MREG_REBALANCE_V1",
    },
    "registered_arithmetic_cone_v1": {
        "rewrite_kind": "registered_arithmetic_cone",
        "proof_template": "REGARITH_PRODUCT_SUM_V1",
        "proof_templates": (
            "REGARITH_PRODUCT_SUM_V1",
            "DSP48E2_MUL_SHIFT_ACCUM_V1",
            "DSP48E2_WIDE_CONST_MUL_V1",
        ),
    },
    "constructive_accumulator_rebuild_v1": {
        "rewrite_kind": "constructive_accumulator_rebuild",
        "proof_template": "REGARITH_CONSTRUCTIVE_ACCUMULATOR_V1",
    },
    "memory_write_buffer_forward_v1": {
        "rewrite_kind": "memory_write_buffer_forward",
        "proof_template": "BRAM_WRITE_BUFFER_FORWARD_LOCAL_MITER_V1",
    },
    "exact_control_to_data_state_equation_v1": {
        "rewrite_kind": "exact_control_to_data_state_equation",
        "proof_template": "EXACT_CONTROL_TO_DATA_STATE_EQUATION_V1",
    },
}


def fixed_point_contract_set_has_executor_authority(
    value: Any, *, expected_source_sha256: str
) -> bool:
    """Return whether a fresh fixed-point census may enter its executor.

    A positive anonymous mining count is only a hypothesis.  The fixed-point
    executor additionally requires a closed, exact-seed contract-set envelope
    before it can inspect candidates and prove a rewrite.  Keeping this check
    shared by menu construction and execution prevents a hypothesis-only row
    from being advertised as a window-consuming proof attempt when the same
    current-seed probe has already shown that its authority envelope is
    malformed or empty.
    """
    if not isinstance(value, dict):
        return False
    candidates = value.get("candidates")
    candidate_count = value.get("candidate_count")
    return bool(
        value.get("kind") == "fixed_point_product_contract_facts_set"
        and value.get("version") == 1
        and value.get("fresh_rediscovery") is True
        and value.get("design_sha256") == expected_source_sha256
        and value.get("proof_status")
        == "fixed_point_facts_extracted_unproved"
        and value.get("mutation_eligible") is False
        and isinstance(candidate_count, int)
        and not isinstance(candidate_count, bool)
        and candidate_count >= 1
        and isinstance(candidates, list)
        and len(candidates) == candidate_count
    )


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
    accumulate: Optional[bool] = None
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
class MemoryPrimitiveSpec:
    primitive: str
    ram_mode: str
    read_width_a: int
    read_width_b: int
    write_width_a: int
    write_width_b: int
    output_register_a: int
    output_register_b: int
    write_mode_a: str
    write_mode_b: str
    read_address_collision: str
    simulation_collision_check: Optional[str]
    initialization_sha256: str
    property_sha256: str
    connected_pin_widths_sha256: str


@dataclass(frozen=True)
class MemoryRepackPlan:
    template: str
    primitive: str
    ram_mode: str
    semantic_contract_sha256: str


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
    if plan.template not in {
        "DSP48E2_MUL_SHIFT_ACCUM_V1",
        "DSP48E2_WIDE_CONST_MUL_V1",
    }:
        raise OperatorProofError("unsupported DSP rewrite template")
    if plan.template == "DSP48E2_WIDE_CONST_MUL_V1":
        if not 28 <= spec.data_width <= 35:
            raise OperatorProofError("wide DSP data width must be 28..35")
        if spec.coefficient_width > 27:
            raise OperatorProofError("wide DSP coefficient exceeds the A multiplier port")
        if spec.accumulate:
            raise OperatorProofError("wide DSP product template does not accumulate")
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


def _memory_write_output(z3, mode, old_word, old_output, write_data, write_enable):
    if mode == "WRITE_FIRST":
        return z3.If(write_enable, write_data, old_word)
    if mode == "READ_FIRST":
        return old_word
    if mode == "NO_CHANGE":
        return z3.If(write_enable, old_output, old_word)
    raise OperatorProofError("memory write mode is unsupported")


def prove_memory_repack(
    spec: MemoryPrimitiveSpec,
    plan: MemoryRepackPlan,
    *,
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    """Prove that a canonical repack preserves the recovered BRAM contract."""
    import z3

    if spec.primitive not in {"RAMB18E2", "RAMB36E2"}:
        raise OperatorProofError("memory primitive is unsupported")
    if spec.ram_mode not in {"SDP", "TDP"}:
        raise OperatorProofError("memory RAM mode is unsupported")
    for name, width in (
        ("read_width_a", spec.read_width_a),
        ("read_width_b", spec.read_width_b),
        ("write_width_a", spec.write_width_a),
        ("write_width_b", spec.write_width_b),
    ):
        _require_int(name, width, 0, 72)
    for name, value in (
        ("output_register_a", spec.output_register_a),
        ("output_register_b", spec.output_register_b),
    ):
        _require_int(name, value, 0, 1)
    for mode in (spec.write_mode_a, spec.write_mode_b):
        if mode not in {"NO_CHANGE", "READ_FIRST", "WRITE_FIRST"}:
            raise OperatorProofError("memory write mode is unsupported")
    for field, value in (
        ("initialization_sha256", spec.initialization_sha256),
        ("property_sha256", spec.property_sha256),
        ("connected_pin_widths_sha256", spec.connected_pin_widths_sha256),
    ):
        _validate_identity(value, field)
    _require_int("timeout_ms", timeout_ms, 1, 120000)
    expected_digest = _digest(asdict(spec))
    preconditions = {
        "template_registered": plan.template == "BRAM_MEMORY_SEMANTIC_REPACK_V1",
        "primitive_preserved": plan.primitive == spec.primitive,
        "ram_mode_preserved": plan.ram_mode == spec.ram_mode,
        "semantic_contract_preserved": plan.semantic_contract_sha256 == expected_digest,
        "collision_contract_explicit": bool(spec.read_address_collision),
    }
    if not all(preconditions.values()):
        return {
            "status": "failed",
            "semantics_version": SEMANTICS_VERSION,
            "preconditions": preconditions,
            "obligations": [],
        }
    obligations = []
    for port, read_width, write_width, mode in (
        ("a", spec.read_width_a, spec.write_width_a, spec.write_mode_a),
        ("b", spec.read_width_b, spec.write_width_b, spec.write_mode_b),
    ):
        width = max(read_width, write_width, 1)
        old_word = z3.BitVec(f"memory_{port}_old_word", width)
        old_output = z3.BitVec(f"memory_{port}_old_output", width)
        write_data = z3.BitVec(f"memory_{port}_write_data", width)
        write_enable = z3.Bool(f"memory_{port}_write_enable")
        reference_memory = z3.If(write_enable, write_data, old_word)
        candidate_memory = z3.If(write_enable, write_data, old_word)
        obligations.append(_solver_obligation(
            f"memory_port_{port}_state_transition",
            reference_memory != candidate_memory,
            timeout_ms,
        ))
        reference_output = _memory_write_output(
            z3, mode, old_word, old_output, write_data, write_enable
        )
        candidate_output = _memory_write_output(
            z3, mode, old_word, old_output, write_data, write_enable
        )
        obligations.append(_solver_obligation(
            f"memory_port_{port}_write_mode",
            reference_output != candidate_output,
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
            "semantic_contract_sha256": expected_digest,
            "port_count": 2,
        },
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


def build_memory_repack_authorization(
    source_dcp_sha256: str,
    source_region_sha256: str,
    spec: MemoryPrimitiveSpec,
    plan: MemoryRepackPlan,
    *,
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    proof = prove_memory_repack(spec, plan, timeout_ms=timeout_ms)
    return _build_authorization(
        "memory_semantic_repack",
        source_dcp_sha256,
        source_region_sha256,
        spec,
        plan,
        proof,
    )


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
        if rewrite_kind == "memory_semantic_repack":
            spec = MemoryPrimitiveSpec(**certificate["spec"])
            plan = MemoryRepackPlan(**certificate["plan"])
            replay = prove_memory_repack(spec, plan, timeout_ms=timeout_ms)
        elif rewrite_kind == "fabric_to_dsp":
            spec = FixedPointProductSpec(**certificate["spec"])
            plan = FixedPointDspPlan(**certificate["plan"])
            replay = prove_fixed_point_rewrite(spec, plan, timeout_ms=timeout_ms)
        elif rewrite_kind == "fabric_to_dsp_two_operand":
            spec = TwoOperandProductSpec(**certificate["spec"])
            plan = TwoOperandDspPlan(**certificate["plan"])
            replay = prove_two_operand_rewrite(spec, plan, timeout_ms=timeout_ms)
        elif rewrite_kind == "fabric_ff_to_dsp_input_reg":
            spec = RegisterAbsorbSpec(**certificate["spec"])
            plan = RegisterAbsorbPlan(**certificate["plan"])
            replay = prove_register_absorb(spec, plan, timeout_ms=timeout_ms)
        elif rewrite_kind == "dsp_pipeline_rebalance":
            spec = PipelineRebalanceSpec(**certificate["spec"])
            raw_plan = dict(certificate["plan"])
            raw_moves = raw_plan.get("set_registers")
            if not isinstance(raw_moves, (list, tuple)):
                return False
            normalized_moves = []
            for move in raw_moves:
                if not isinstance(move, (list, tuple)) or len(move) != 2:
                    return False
                normalized_moves.append((move[0], move[1]))
            raw_plan["set_registers"] = tuple(normalized_moves)
            plan = PipelineRebalancePlan(**raw_plan)
            replay = prove_pipeline_rebalance(spec, plan, timeout_ms=timeout_ms)
        elif rewrite_kind == "dsp_recurrence_cut_transport":
            spec = RecurrenceCutSpec(**certificate["spec"])
            raw_plan = dict(certificate["plan"])
            raw_plan["srl_segment_depths"] = tuple(raw_plan["srl_segment_depths"])
            plan = RecurrenceCutPlan(**raw_plan)
            replay = prove_recurrence_cut_contract(spec, plan, timeout_ms=timeout_ms)
        elif rewrite_kind == "constructive_accumulator_rebuild":
            from .metaloop_action_proof import (
                verify_constructive_accumulator_authorization,
            )

            if not verify_constructive_accumulator_authorization(certificate):
                return False
            replay = certificate.get("proof")
        elif rewrite_kind == "memory_write_buffer_forward":
            from .metaloop_action_proof import replay_memory_forward_authorization

            replay = replay_memory_forward_authorization(certificate)
            if replay is None:
                return False
        elif rewrite_kind == "registered_arithmetic_cone":
            replay = _replay_registered_product_sum_authorization(
                certificate, timeout_ms=timeout_ms
            )
        else:
            return False
        return replay == certificate.get("proof") and replay.get("status") == "proved"
    except (KeyError, TypeError, ValueError, OperatorProofError):
        return False


def _replay_registered_product_sum_authorization(
    certificate: dict[str, Any], *, timeout_ms: int
) -> dict[str, Any]:
    """Replay every canonical instance plan in one complete-family bundle."""
    from .regarith_tools.common import plan_input
    from .regarith_tools.proof import screen_and_prove

    spec = certificate.get("spec")
    plan = certificate.get("plan")
    if (
        not isinstance(spec, dict)
        or set(spec)
        != {
            "backend_id", "bundle_digest", "emission_result_digest",
            "recovered_plan_digest", "instance_count", "module_count", "run_id",
        }
        or re.fullmatch(r"nr-[0-9a-f]{32}", str(spec.get("run_id") or "")) is None
        or spec.get("backend_id") != "registered_complete_family_product_sum_v1"
        or not isinstance(plan, dict)
        or set(plan)
        != {"template", "ir_plans", "plan_digests", "proof_result_digests"}
        or plan.get("template") != "REGARITH_PRODUCT_SUM_V1"
        or not isinstance(plan.get("ir_plans"), list)
        or not plan["ir_plans"]
        or len(plan["ir_plans"]) != spec.get("instance_count")
        or not isinstance(plan.get("proof_result_digests"), list)
        or len(plan["proof_result_digests"]) != len(plan["ir_plans"])
        or not isinstance(plan.get("plan_digests"), list)
        or len(plan["plan_digests"]) != len(plan["ir_plans"])
    ):
        raise OperatorProofError("registered product-sum authorization schema is invalid")
    results = [
        screen_and_prove(plan_input(raw), timeout_ms=timeout_ms)
        for raw in plan["ir_plans"]
    ]
    if any(
        result.get("status") != "success"
        or (result.get("proof") or {}).get("status") != "proved"
        for result in results
    ):
        raise OperatorProofError("registered product-sum instance proof did not replay")
    digests = [result["result_digest"] for result in results]
    if digests != plan["proof_result_digests"]:
        raise OperatorProofError("registered product-sum proof digest coverage changed")
    recovered_plan_digests = [
        plan_input(raw).plan_digest() for raw in plan["ir_plans"]
    ]
    if recovered_plan_digests != plan["plan_digests"]:
        raise OperatorProofError("registered product-sum recovered plan digests changed")
    if _digest(recovered_plan_digests) != spec.get("recovered_plan_digest"):
        raise OperatorProofError("registered product-sum recovered plan binding changed")
    return {
        "status": "proved",
        "proof_template": "REGARITH_PRODUCT_SUM_V1",
        "instance_count": len(results),
        "proof_result_digests": digests,
        "recovered_plan_digest": spec["recovered_plan_digest"],
        "complete_family_coverage": True,
    }


def build_registered_product_sum_authorization(
    emission: dict[str, Any],
    *,
    run_id: str,
) -> dict[str, Any]:
    """Bind complete-family production IR/proofs to one exact source bundle."""
    if not isinstance(emission, dict):
        raise OperatorProofError("registered product-sum emission must be an object")
    source = emission.get("source_artifact_sha256")
    bundle_digest = emission.get("bundle_digest")
    result_digest = emission.get("result_digest")
    for value, label in (
        (source, "source artifact"),
        (bundle_digest, "bundle digest"),
        (result_digest, "emission digest"),
    ):
        _validate_identity(value, label)
    if re.fullmatch(r"nr-[0-9a-f]{32}", str(run_id or "")) is None:
        raise OperatorProofError("registered product-sum run_id is invalid")
    ir_plans = emission.get("ir_plans")
    plan_digests = emission.get("plan_digests")
    proof_digests = emission.get("proof_result_digests")
    if (
        not isinstance(ir_plans, list)
        or not isinstance(plan_digests, list)
        or not isinstance(proof_digests, list)
        or len(plan_digests) != len(ir_plans)
    ):
        raise OperatorProofError("registered product-sum emission proof inventory is absent")
    recovered_plan_digest = _digest(plan_digests)
    certificate = {
        "kind": "operator_rewrite_authorization",
        "version": AUTHORIZATION_VERSION,
        "rewrite_kind": "registered_arithmetic_cone",
        "source": {
            "dcp_sha256": source,
            "region_sha256": bundle_digest,
        },
        "spec": {
            "backend_id": emission.get("backend_id"),
            "bundle_digest": bundle_digest,
            "emission_result_digest": result_digest,
            "recovered_plan_digest": recovered_plan_digest,
            "instance_count": emission.get("instance_count"),
            "module_count": emission.get("module_count"),
            "run_id": run_id,
        },
        "plan": {
            "template": "REGARITH_PRODUCT_SUM_V1",
            "ir_plans": ir_plans,
            "plan_digests": plan_digests,
            "proof_result_digests": proof_digests,
        },
        "proof": {},
        "authority": "pre_mutation_only",
    }
    certificate["proof"] = _replay_registered_product_sum_authorization(
        certificate, timeout_ms=5000
    )
    certificate["certificate_id"] = _digest(certificate)
    return certificate


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

_EMITTED_ATTESTATION_FIELDS = {
    "kind", "version", "status", "authority", "template", "source",
    "candidate", "authorization_id", "placement_manifest_sha256",
    "topology_manifest_sha256", "checks", "attestation_id",
}
_EMITTED_ATTESTATION_CHECKS_BY_TEMPLATE = {
    "registered_arithmetic_cone_v1": {
        "current_run_source_and_generation_bound",
        "registered_bundle_wiring_verified",
        "complete_old_driver_disconnection_verified",
        "exact_module_inventory",
        "current_run_generated_module_structure_verified",
        "current_run_ooc_timing_gate_verified",
        "relative_placement_binding_applied_or_typed_degraded",
        "readable_edif_sidecar_written",
        "candidate_checkpoint_reopened",
        "source_checkpoint_reopened",
        "relevant_nets_fully_routed",
    },
    "memory_semantic_repack_v1": {
        "memory_primitive_replayed", "memory_properties_replayed",
        "memory_boundary_replayed", "consumer_dependencies_replayed",
        "whole_design_port_contract_replayed", "exact_namespace_inventory",
        "closed_top_identity_replayed", "candidate_checkpoint_reopened",
        "source_checkpoint_reopened", "relevant_nets_fully_routed",
    },
    "dsp_srl_recurrence_cut_v1": {
        "joint_connected_topology_replayed", "all_addresses_replayed",
        "exact_namespace_inventory", "candidate_checkpoint_reopened",
        "source_checkpoint_reopened", "relevant_nets_fully_routed",
    },
    "fixed_point_dsp_rebuild_v1": {
        "fixed_point_dsp_topology_replayed", "whole_design_port_contract_replayed",
        "exact_namespace_inventory", "closed_top_identity_rename_replayed",
        "candidate_checkpoint_reopened", "source_checkpoint_reopened",
        "relevant_nets_fully_routed",
    },
    "dsp_mul_two_operand_v1": {
        "two_operand_dsp_topology_replayed", "whole_design_port_contract_replayed",
        "exact_namespace_inventory", "closed_top_identity_rename_replayed",
        "candidate_checkpoint_reopened", "source_checkpoint_reopened",
        "relevant_nets_fully_routed",
    },
    "dsp_register_absorb_v1": {
        "register_absorb_topology_replayed", "whole_design_port_contract_replayed",
        "exact_namespace_inventory", "closed_top_identity_rename_replayed",
        "candidate_checkpoint_reopened", "source_checkpoint_reopened",
        "relevant_nets_fully_routed",
    },
    "dsp_pipeline_rebalance_v1": {
        "rebalance_topology_replayed", "whole_design_port_contract_replayed",
        "exact_namespace_inventory", "closed_top_identity_rename_replayed",
        "candidate_checkpoint_reopened", "source_checkpoint_reopened",
        "relevant_nets_fully_routed",
    },
    "constructive_accumulator_rebuild_v1": {
        "fresh_seed_bound_recovery", "canonical_expression_match",
        "old_cone_fully_removed", "target_driver_closure_replayed",
        "exact_namespace_inventory", "candidate_checkpoint_reopened",
        "source_checkpoint_reopened", "relevant_nets_fully_routed",
    },
    "memory_write_buffer_forward_v1": {
        "fresh_seed_bound_recovery", "read_first_port_contract_replayed",
        "write_boundary_replayed", "read_consumer_closure_replayed",
        "exact_namespace_inventory", "candidate_checkpoint_reopened",
        "source_checkpoint_reopened", "relevant_nets_fully_routed",
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
    if not isinstance(report, dict) or set(report) != _STRICT_VALIDATION_FIELDS:
        raise OperatorProofError(
            "post-emission validator report has missing or unsupported fields"
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
        raise OperatorProofError("post-emission strict validation did not pass")
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
    strict_validation_report: dict[str, Any],
    *,
    emitted_topology_attestation: dict[str, Any],
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    """Build the exact bounded proof object accepted by the agent gate."""
    certificate = build_operator_post_emission_certificate(
        template,
        authorization,
        candidate_dcp_sha256,
        strict_validation_report,
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
        "post_emission_validation": certificate,
    }


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


_MEMORY_CANDIDATE_ID_RE = re.compile(r"memory:[0-9a-f]{20}")


def memory_spec_from_extracted_facts(
    facts: dict[str, Any],
) -> MemoryPrimitiveSpec:
    """Recover a flat proof spec from fresh configured-memory DCP facts."""
    if not isinstance(facts, dict):
        raise OperatorProofError("memory facts must be an object")
    if facts.get("kind") != "memory_primitive_contract_facts" or facts.get("version") != 1:
        raise OperatorProofError("unsupported memory facts schema")
    if (
        facts.get("status") != "success"
        or facts.get("proof_status") != "memory_primitive_facts_extracted_unproved"
        or facts.get("mutation_eligible") is not False
        or facts.get("fresh_rediscovery") is not True
    ):
        raise OperatorProofError("memory facts have invalid pre-mutation authority")
    _validate_identity(facts.get("design_sha256"), "facts.design_sha256")
    candidate_id = facts.get("candidate_id")
    if not isinstance(candidate_id, str) or _MEMORY_CANDIDATE_ID_RE.fullmatch(candidate_id) is None:
        raise OperatorProofError("memory candidate identity is invalid")
    claimed_region = facts.get("region_sha256")
    payload = dict(facts)
    payload.pop("region_sha256", None)
    for wrapper_key in ("candidate_id", "fresh_rediscovery", "runtime_s"):
        payload.pop(wrapper_key, None)
    if not isinstance(claimed_region, str) or _digest(payload) != claimed_region:
        raise OperatorProofError("memory region digest mismatch")
    if candidate_id != "memory:" + claimed_region[:20]:
        raise OperatorProofError("memory candidate identity does not bind the region")
    spec = facts.get("spec")
    properties = facts.get("properties")
    if not isinstance(spec, dict) or not isinstance(properties, dict):
        raise OperatorProofError("memory spec or properties are unavailable")
    ports = spec.get("ports")
    collision = spec.get("collision")
    connected = spec.get("connected_pin_widths")
    if (
        not isinstance(ports, dict)
        or set(ports) != {"a", "b"}
        or not isinstance(collision, dict)
        or set(collision) != {"read_address", "simulation_check"}
        or not isinstance(connected, dict)
    ):
        raise OperatorProofError("memory nested semantic spec is malformed")
    expected_spec_fields = {
        "primitive", "ram_mode", "ram_mode_source", "ports", "collision",
        "connected_pin_widths", "initialization_sha256",
        "initialization_property_count", "property_sha256",
    }
    if set(spec) != expected_spec_fields:
        raise OperatorProofError("memory semantic spec fields are incomplete")
    expected_port_fields = {
        "read_width", "write_width", "output_register", "write_mode"
    }
    if any(not isinstance(ports[name], dict) or set(ports[name]) != expected_port_fields for name in ("a", "b")):
        raise OperatorProofError("memory port semantic fields are incomplete")
    initialization = {
        key: value
        for key, value in properties.items()
        if str(key).startswith("INIT") or str(key).startswith("SRVAL")
    }
    if (
        _digest(properties) != spec.get("property_sha256")
        or _digest(initialization) != spec.get("initialization_sha256")
        or len(initialization) != spec.get("initialization_property_count")
    ):
        raise OperatorProofError("memory property or initialization digest drifted")
    return MemoryPrimitiveSpec(
        primitive=spec["primitive"],
        ram_mode=spec["ram_mode"],
        read_width_a=ports["a"]["read_width"],
        read_width_b=ports["b"]["read_width"],
        write_width_a=ports["a"]["write_width"],
        write_width_b=ports["b"]["write_width"],
        output_register_a=ports["a"]["output_register"],
        output_register_b=ports["b"]["output_register"],
        write_mode_a=ports["a"]["write_mode"],
        write_mode_b=ports["b"]["write_mode"],
        read_address_collision=collision["read_address"],
        simulation_collision_check=collision["simulation_check"],
        initialization_sha256=spec["initialization_sha256"],
        property_sha256=spec["property_sha256"],
        connected_pin_widths_sha256=_digest(connected),
    )


def build_memory_repack_authorization_from_facts(
    facts: dict[str, Any],
    plan: MemoryRepackPlan,
    *,
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    spec = memory_spec_from_extracted_facts(facts)
    return build_memory_repack_authorization(
        facts["design_sha256"],
        facts["region_sha256"],
        spec,
        plan,
        timeout_ms=timeout_ms,
    )


_FIXED_POINT_CANDIDATE_ID_RE = re.compile(r"fixedpoint:[0-9a-f]{20}")
_FIXED_POINT_SPEC_INT_FIELDS = (
    "data_width", "coefficient_width", "coefficient", "product_width",
    "shift", "term_width", "accumulator_width", "product_latency",
)
_FIXED_POINT_SPEC_BOOL_FIELDS = (
    "data_signed", "coefficient_signed", "term_signed", "accumulate",
)


def fixed_point_spec_from_extracted_facts(
    facts: dict[str, Any],
) -> FixedPointProductSpec:
    """Recover the exact product contract from hash-bound extracted facts."""
    if not isinstance(facts, dict):
        raise OperatorProofError("fixed-point facts must be an object")
    if (
        facts.get("kind") != "fixed_point_product_contract_facts"
        or facts.get("version") != 1
    ):
        raise OperatorProofError("unsupported fixed-point facts schema")
    if facts.get("status") != "success" or facts.get("proof_status") != (
        "fixed_point_facts_extracted_unproved"
    ):
        raise OperatorProofError(
            "fixed-point facts are incomplete or have invalid authority"
        )
    if facts.get("mutation_eligible") is not False:
        raise OperatorProofError(
            "fixed-point facts have invalid pre-mutation authority"
        )
    if facts.get("fresh_rediscovery") is not True:
        raise OperatorProofError("fixed-point facts are not a fresh rediscovery")
    _validate_identity(facts.get("design_sha256"), "facts.design_sha256")
    candidate_id = facts.get("candidate_id")
    if (
        not isinstance(candidate_id, str)
        or _FIXED_POINT_CANDIDATE_ID_RE.fullmatch(candidate_id) is None
    ):
        raise OperatorProofError("fixed-point candidate identity is invalid")
    claimed_region = facts.get("region_sha256")
    payload = dict(facts)
    payload.pop("region_sha256", None)
    for wrapper_key in ("candidate_id", "fresh_rediscovery", "runtime_s"):
        payload.pop(wrapper_key, None)
    if not isinstance(claimed_region, str) or _digest(payload) != claimed_region:
        raise OperatorProofError("fixed-point region digest mismatch")
    spec = facts.get("spec")
    expected_fields = set(
        _FIXED_POINT_SPEC_INT_FIELDS + _FIXED_POINT_SPEC_BOOL_FIELDS
    ) | {"overflow"}
    legacy_fields = expected_fields - {"accumulate"}
    if not isinstance(spec, dict) or set(spec) not in (expected_fields, legacy_fields):
        raise OperatorProofError("recovered fixed-point spec fields are incomplete")
    for name in _FIXED_POINT_SPEC_INT_FIELDS:
        if isinstance(spec[name], bool) or not isinstance(spec[name], int):
            raise OperatorProofError(f"recovered spec {name} must be an integer")
    for name in _FIXED_POINT_SPEC_BOOL_FIELDS:
        if name == "accumulate" and name not in spec:
            continue
        if not isinstance(spec[name], bool):
            raise OperatorProofError(f"recovered spec {name} must be a boolean")
    if not isinstance(spec["overflow"], str):
        raise OperatorProofError("recovered spec overflow must be a string")
    return FixedPointProductSpec(**spec)


def build_fixed_point_authorization_from_facts(
    facts: dict[str, Any],
    plan: FixedPointDspPlan,
    *,
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    """Build pre-mutation authorization using exact recovered region identity."""
    spec = fixed_point_spec_from_extracted_facts(facts)
    dcp_sha256 = facts.get("design_sha256")
    region_sha256 = facts.get("region_sha256")
    _validate_identity(dcp_sha256, "facts.design_sha256")
    _validate_identity(region_sha256, "facts.region_sha256")
    return build_fixed_point_authorization(
        dcp_sha256, region_sha256, spec, plan, timeout_ms=timeout_ms
    )


@dataclass(frozen=True)
class TwoOperandProductSpec:
    a_width: int
    b_width: int
    product_width: int
    shift: int
    term_width: int
    accumulator_width: int
    product_latency: int
    a_signed: bool = True
    b_signed: bool = True
    term_signed: bool = True
    overflow: str = "wrap"


@dataclass(frozen=True)
class TwoOperandDspPlan:
    template: str
    a_signed: bool
    b_signed: bool
    output_lsb: int
    output_width: int
    output_signed: bool
    accumulator_width: int
    product_latency: int


def _validate_two_operand(
    spec: TwoOperandProductSpec, plan: TwoOperandDspPlan
) -> None:
    for name, value in (
        ("a_width", spec.a_width),
        ("b_width", spec.b_width),
        ("product_width", spec.product_width),
        ("term_width", spec.term_width),
        ("accumulator_width", spec.accumulator_width),
    ):
        _require_int(name, value, 1, 256)
    _require_int("shift", spec.shift, 0, 255)
    _require_int("product_latency", spec.product_latency, 0, 8)
    if spec.product_width < spec.a_width + spec.b_width:
        raise OperatorProofError("product_width cannot represent the full product")
    if spec.shift + spec.term_width > spec.product_width:
        raise OperatorProofError("shifted term exceeds product width")
    if spec.overflow != "wrap":
        raise OperatorProofError("only explicit modular wrap is supported")
    for name, value in (
        ("spec.a_signed", spec.a_signed),
        ("spec.b_signed", spec.b_signed),
        ("spec.term_signed", spec.term_signed),
        ("plan.a_signed", plan.a_signed),
        ("plan.b_signed", plan.b_signed),
        ("plan.output_signed", plan.output_signed),
    ):
        if not isinstance(value, bool):
            raise OperatorProofError(f"{name} must be a boolean")
    if plan.template != "DSP48E2_MUL_TWO_OPERAND_V1":
        raise OperatorProofError("unsupported DSP rewrite template")
    if plan.output_width < 1 or plan.output_lsb < 0:
        raise OperatorProofError("DSP output slice is invalid")
    if plan.output_lsb + plan.output_width > spec.product_width:
        raise OperatorProofError("DSP output slice exceeds product width")
    if plan.accumulator_width < 1 or plan.accumulator_width > 256:
        raise OperatorProofError("DSP accumulator width is invalid")


def prove_two_operand_rewrite(
    spec: TwoOperandProductSpec,
    plan: TwoOperandDspPlan,
    *,
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    """Prove exact variable-product slicing, signedness, and modular accumulation."""
    import z3

    _validate_two_operand(spec, plan)
    _require_int("timeout_ms", timeout_ms, 1, 120000)
    preconditions = {
        "latency_equal": plan.product_latency == spec.product_latency,
        "accumulator_width_equal": plan.accumulator_width == spec.accumulator_width,
        "operand_signedness_equal": (
            plan.a_signed == spec.a_signed and plan.b_signed == spec.b_signed
        ),
    }
    if not all(preconditions.values()):
        return {
            "status": "failed",
            "semantics_version": SEMANTICS_VERSION,
            "preconditions": preconditions,
            "obligations": [],
        }

    operand_a = z3.BitVec("two_operand_a", spec.a_width)
    operand_b = z3.BitVec("two_operand_b", spec.b_width)
    accumulator = z3.BitVec("two_operand_accumulator", spec.accumulator_width)

    ref_a = _extend(
        z3, operand_a, spec.a_width, spec.product_width, spec.a_signed
    )
    ref_b = _extend(
        z3, operand_b, spec.b_width, spec.product_width, spec.b_signed
    )
    ref_product = ref_a * ref_b
    ref_term = z3.Extract(
        spec.shift + spec.term_width - 1, spec.shift, ref_product
    )
    ref_acc = accumulator + _resize_term(
        z3, ref_term, spec.term_width, spec.accumulator_width, spec.term_signed
    )

    candidate_a = _extend(
        z3, operand_a, spec.a_width, spec.product_width, plan.a_signed
    )
    candidate_b = _extend(
        z3, operand_b, spec.b_width, spec.product_width, plan.b_signed
    )
    candidate_product = candidate_a * candidate_b
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
        "two_operand_product_slice_and_wrap", ref_acc != candidate_acc, timeout_ms
    )
    status = "proved" if obligation["result"] == "unsat" else "failed"
    return {
        "status": status,
        "semantics_version": SEMANTICS_VERSION,
        "preconditions": preconditions,
        "obligations": [obligation],
    }


def build_two_operand_authorization(
    source_dcp_sha256: str,
    source_region_sha256: str,
    spec: TwoOperandProductSpec,
    plan: TwoOperandDspPlan,
    *,
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    proof = prove_two_operand_rewrite(spec, plan, timeout_ms=timeout_ms)
    return _build_authorization(
        "fabric_to_dsp_two_operand", source_dcp_sha256, source_region_sha256,
        spec, plan, proof,
    )


_TWO_OPERAND_CANDIDATE_ID_RE = re.compile(r"twooperand:[0-9a-f]{20}")
_TWO_OPERAND_SPEC_INT_FIELDS = (
    "a_width", "b_width", "product_width", "shift", "term_width",
    "accumulator_width", "product_latency",
)
_TWO_OPERAND_SPEC_BOOL_FIELDS = (
    "a_signed", "b_signed", "term_signed",
)


def two_operand_spec_from_extracted_facts(
    facts: dict[str, Any],
) -> TwoOperandProductSpec:
    """Recover the exact two-operand product contract from hash-bound facts."""
    if not isinstance(facts, dict):
        raise OperatorProofError("two-operand facts must be an object")
    if (
        facts.get("kind") != "two_operand_product_contract_facts"
        or facts.get("version") != 1
    ):
        raise OperatorProofError("unsupported two-operand facts schema")
    if facts.get("status") != "success" or facts.get("proof_status") != (
        "two_operand_facts_extracted_unproved"
    ):
        raise OperatorProofError(
            "two-operand facts are incomplete or have invalid authority"
        )
    if facts.get("mutation_eligible") is not False:
        raise OperatorProofError(
            "two-operand facts have invalid pre-mutation authority"
        )
    if facts.get("fresh_rediscovery") is not True:
        raise OperatorProofError("two-operand facts are not a fresh rediscovery")
    _validate_identity(facts.get("design_sha256"), "facts.design_sha256")
    candidate_id = facts.get("candidate_id")
    if (
        not isinstance(candidate_id, str)
        or _TWO_OPERAND_CANDIDATE_ID_RE.fullmatch(candidate_id) is None
    ):
        raise OperatorProofError("two-operand candidate identity is invalid")
    claimed_region = facts.get("region_sha256")
    payload = dict(facts)
    payload.pop("region_sha256", None)
    for wrapper_key in ("candidate_id", "fresh_rediscovery", "runtime_s"):
        payload.pop(wrapper_key, None)
    if not isinstance(claimed_region, str) or _digest(payload) != claimed_region:
        raise OperatorProofError("two-operand region digest mismatch")
    spec = facts.get("spec")
    expected_fields = set(
        _TWO_OPERAND_SPEC_INT_FIELDS + _TWO_OPERAND_SPEC_BOOL_FIELDS
    ) | {"overflow"}
    if not isinstance(spec, dict) or set(spec) != expected_fields:
        raise OperatorProofError("recovered two-operand spec fields are incomplete")
    for name in _TWO_OPERAND_SPEC_INT_FIELDS:
        if isinstance(spec[name], bool) or not isinstance(spec[name], int):
            raise OperatorProofError(f"recovered spec {name} must be an integer")
    for name in _TWO_OPERAND_SPEC_BOOL_FIELDS:
        if not isinstance(spec[name], bool):
            raise OperatorProofError(f"recovered spec {name} must be a boolean")
    if not isinstance(spec["overflow"], str):
        raise OperatorProofError("recovered spec overflow must be a string")
    return TwoOperandProductSpec(**spec)


def build_two_operand_authorization_from_facts(
    facts: dict[str, Any],
    plan: TwoOperandDspPlan,
    *,
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    """Build pre-mutation authorization using exact recovered region identity."""
    spec = two_operand_spec_from_extracted_facts(facts)
    dcp_sha256 = facts.get("design_sha256")
    region_sha256 = facts.get("region_sha256")
    _validate_identity(dcp_sha256, "facts.design_sha256")
    _validate_identity(region_sha256, "facts.region_sha256")
    return build_two_operand_authorization(
        dcp_sha256, region_sha256, spec, plan, timeout_ms=timeout_ms
    )


@dataclass(frozen=True)
class RegisterAbsorbSpec:
    bus_width: int          # 1..30 (a side <=30, b side <=18)
    dsp_side: str           # "a" | "b"
    ce_present: bool        # FF bus has CE (FDRE/FDSE with CE net) — always modeled
    reset_kind: str         # "none" | "sync_reset_zero"
    init_zero: bool         # must be True
    absorb_stages: int      # must be 1


@dataclass(frozen=True)
class RegisterAbsorbPlan:
    template: str           # "DSP48E2_INPUT_REG_ABSORB_V1"
    dsp_side: str
    target_reg: str         # "AREG" | "BREG"
    target_value: int       # must be 1
    ce_mapped: bool         # CE net moves to CEA2/CEB2
    reset_mapped: bool      # True iff reset_kind == "sync_reset_zero"


_REGISTER_ABSORB_SIDE_WIDTH_CAPS = {"a": 30, "b": 18}
_REGISTER_ABSORB_SIDE_REGS = {"a": "AREG", "b": "BREG"}
_REGISTER_ABSORB_RESET_KINDS = {"none", "sync_reset_zero"}


def _validate_register_absorb(
    spec: RegisterAbsorbSpec, plan: RegisterAbsorbPlan
) -> None:
    if spec.dsp_side not in _REGISTER_ABSORB_SIDE_WIDTH_CAPS:
        raise OperatorProofError('spec.dsp_side must be "a" or "b"')
    if plan.dsp_side not in _REGISTER_ABSORB_SIDE_WIDTH_CAPS:
        raise OperatorProofError('plan.dsp_side must be "a" or "b"')
    _require_int(
        f"bus_width for side {spec.dsp_side!r}",
        spec.bus_width,
        1,
        _REGISTER_ABSORB_SIDE_WIDTH_CAPS[spec.dsp_side],
    )
    for name, value in (
        ("spec.ce_present", spec.ce_present),
        ("spec.init_zero", spec.init_zero),
        ("plan.ce_mapped", plan.ce_mapped),
        ("plan.reset_mapped", plan.reset_mapped),
    ):
        if not isinstance(value, bool):
            raise OperatorProofError(f"{name} must be a boolean")
    if spec.init_zero is not True:
        raise OperatorProofError(
            "only INIT-zero fabric FF buses may be absorbed"
        )
    if spec.reset_kind not in _REGISTER_ABSORB_RESET_KINDS:
        raise OperatorProofError(
            'reset_kind must be "none" or "sync_reset_zero"'
        )
    if isinstance(spec.absorb_stages, bool) or spec.absorb_stages != 1:
        raise OperatorProofError("absorb_stages must be exactly 1")
    if plan.template != "DSP48E2_INPUT_REG_ABSORB_V1":
        raise OperatorProofError("unsupported DSP rewrite template")
    if plan.target_reg != _REGISTER_ABSORB_SIDE_REGS[plan.dsp_side]:
        raise OperatorProofError(
            "plan.target_reg does not correspond to plan.dsp_side"
        )
    if isinstance(plan.target_value, bool) or plan.target_value != 1:
        raise OperatorProofError("plan.target_value must be exactly 1")
    if plan.ce_mapped is not True:
        raise OperatorProofError("plan.ce_mapped must be True")
    if plan.reset_mapped != (spec.reset_kind == "sync_reset_zero"):
        raise OperatorProofError(
            "plan.reset_mapped disagrees with spec.reset_kind"
        )


def prove_register_absorb(
    spec: RegisterAbsorbSpec,
    plan: RegisterAbsorbPlan,
    *,
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    """Prove single-step transition equivalence of FF-bus absorption.

    Model A: fabric FDRE stage feeding an unregistered DSP input; the value
    entering the multiplier at cycle t+1 is the fabric register's next state.
    Model B: a direct wire into the DSP internal input register with RST/CE
    mapped. Internal latency moves; port-path register counts are preserved.
    """
    import z3

    _validate_register_absorb(spec, plan)
    _require_int("timeout_ms", timeout_ms, 1, 120000)
    preconditions = {
        "side_reg_consistent": (
            plan.dsp_side == spec.dsp_side
            and plan.target_reg == _REGISTER_ABSORB_SIDE_REGS[spec.dsp_side]
        ),
        "single_stage": spec.absorb_stages == 1,
    }
    if not all(preconditions.values()):
        return {
            "status": "failed",
            "semantics_version": SEMANTICS_VERSION,
            "preconditions": preconditions,
            "obligations": [],
        }

    width = spec.bus_width
    data = z3.BitVec("absorb_data", width)
    fabric_q = z3.BitVec("absorb_fabric_q", width)
    dsp_r = z3.BitVec("absorb_dsp_r", width)
    ce = z3.Bool("absorb_ce")
    if spec.reset_kind == "sync_reset_zero":
        reset = z3.Bool("absorb_reset")
    else:
        reset = z3.BoolVal(False)
    zero = z3.BitVecVal(0, width)

    # Model A: q' = reset ? 0 : (ce ? d : q); multiplier sees q' at t+1.
    fabric_next = z3.If(reset, zero, z3.If(ce, data, fabric_q))
    fabric_mult_view = fabric_next
    # Model B: r' = reset ? 0 : (ce ? d : r); multiplier sees r' at t+1.
    dsp_next = z3.If(reset, zero, z3.If(ce, data, dsp_r))
    dsp_mult_view = dsp_next

    step_equivalent = z3.Implies(
        fabric_q == dsp_r,
        z3.And(
            fabric_next == dsp_next,
            fabric_mult_view == dsp_mult_view,
        ),
    )
    init_equivalent = zero == zero  # both models initialize to zero
    obligation = _solver_obligation(
        "register_absorb_transport",
        z3.Not(z3.And(step_equivalent, init_equivalent)),
        timeout_ms,
    )
    status = "proved" if obligation["result"] == "unsat" else "failed"
    return {
        "status": status,
        "semantics_version": SEMANTICS_VERSION,
        "preconditions": preconditions,
        "obligations": [obligation],
    }


def build_register_absorb_authorization(
    source_dcp_sha256: str,
    source_region_sha256: str,
    spec: RegisterAbsorbSpec,
    plan: RegisterAbsorbPlan,
    *,
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    proof = prove_register_absorb(spec, plan, timeout_ms=timeout_ms)
    return _build_authorization(
        "fabric_ff_to_dsp_input_reg", source_dcp_sha256, source_region_sha256,
        spec, plan, proof,
    )


_REGISTER_ABSORB_CANDIDATE_ID_RE = re.compile(r"regabsorb:[0-9a-f]{20}")
_REGISTER_ABSORB_SPEC_INT_FIELDS = ("bus_width", "absorb_stages")
_REGISTER_ABSORB_SPEC_BOOL_FIELDS = ("ce_present", "init_zero")
_REGISTER_ABSORB_SPEC_STR_FIELDS = ("dsp_side", "reset_kind")


def register_absorb_spec_from_extracted_facts(
    facts: dict[str, Any],
) -> RegisterAbsorbSpec:
    """Recover the exact register-absorb contract from hash-bound facts."""
    if not isinstance(facts, dict):
        raise OperatorProofError("register-absorb facts must be an object")
    if (
        facts.get("kind") != "register_absorb_contract_facts"
        or facts.get("version") != 1
    ):
        raise OperatorProofError("unsupported register-absorb facts schema")
    if facts.get("status") != "success" or facts.get("proof_status") != (
        "register_absorb_facts_extracted_unproved"
    ):
        raise OperatorProofError(
            "register-absorb facts are incomplete or have invalid authority"
        )
    if facts.get("mutation_eligible") is not False:
        raise OperatorProofError(
            "register-absorb facts have invalid pre-mutation authority"
        )
    if facts.get("fresh_rediscovery") is not True:
        raise OperatorProofError(
            "register-absorb facts are not a fresh rediscovery"
        )
    _validate_identity(facts.get("design_sha256"), "facts.design_sha256")
    candidate_id = facts.get("candidate_id")
    if (
        not isinstance(candidate_id, str)
        or _REGISTER_ABSORB_CANDIDATE_ID_RE.fullmatch(candidate_id) is None
    ):
        raise OperatorProofError("register-absorb candidate identity is invalid")
    claimed_region = facts.get("region_sha256")
    payload = dict(facts)
    payload.pop("region_sha256", None)
    for wrapper_key in ("candidate_id", "fresh_rediscovery", "runtime_s"):
        payload.pop(wrapper_key, None)
    if not isinstance(claimed_region, str) or _digest(payload) != claimed_region:
        raise OperatorProofError("register-absorb region digest mismatch")
    spec = facts.get("spec")
    expected_fields = set(
        _REGISTER_ABSORB_SPEC_INT_FIELDS
        + _REGISTER_ABSORB_SPEC_BOOL_FIELDS
        + _REGISTER_ABSORB_SPEC_STR_FIELDS
    )
    if not isinstance(spec, dict) or set(spec) != expected_fields:
        raise OperatorProofError(
            "recovered register-absorb spec fields are incomplete"
        )
    for name in _REGISTER_ABSORB_SPEC_INT_FIELDS:
        if isinstance(spec[name], bool) or not isinstance(spec[name], int):
            raise OperatorProofError(f"recovered spec {name} must be an integer")
    for name in _REGISTER_ABSORB_SPEC_BOOL_FIELDS:
        if not isinstance(spec[name], bool):
            raise OperatorProofError(f"recovered spec {name} must be a boolean")
    for name in _REGISTER_ABSORB_SPEC_STR_FIELDS:
        if not isinstance(spec[name], str):
            raise OperatorProofError(f"recovered spec {name} must be a string")
    return RegisterAbsorbSpec(**spec)


def build_register_absorb_authorization_from_facts(
    facts: dict[str, Any],
    plan: RegisterAbsorbPlan,
    *,
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    """Build pre-mutation authorization using exact recovered region identity."""
    spec = register_absorb_spec_from_extracted_facts(facts)
    dcp_sha256 = facts.get("design_sha256")
    region_sha256 = facts.get("region_sha256")
    _validate_identity(dcp_sha256, "facts.design_sha256")
    _validate_identity(region_sha256, "facts.region_sha256")
    return build_register_absorb_authorization(
        dcp_sha256, region_sha256, spec, plan, timeout_ms=timeout_ms
    )


@dataclass(frozen=True)
class PipelineRebalanceSpec:
    a_width: int            # 1..30 (hardened A port)
    b_width: int            # 1..18 (hardened B port)
    direction: str          # "forward" | "backward"
    ce_present: bool        # shared CE across the moved registers
    reset_kind: str         # "none" | "sync_reset_zero"
    init_zero: bool         # must be True


@dataclass(frozen=True)
class PipelineRebalancePlan:
    template: str           # "DSP48E2_MREG_REBALANCE_V1"
    direction: str
    set_registers: tuple    # exact ((name, value), ...) register moves
    ce_mapped: bool         # CE net moves between CEA2/CEB2 and CEM
    reset_mapped: bool      # True iff reset_kind == "sync_reset_zero"


_PIPELINE_REBALANCE_DIRECTIONS = {"forward", "backward"}
_PIPELINE_REBALANCE_SET_REGISTERS = {
    "forward": (("AREG", 0), ("BREG", 0), ("MREG", 1)),
    "backward": (("AREG", 1), ("BREG", 1), ("MREG", 0)),
}
_PIPELINE_REBALANCE_RESET_KINDS = {"none", "sync_reset_zero"}


def _validate_pipeline_rebalance(
    spec: PipelineRebalanceSpec, plan: PipelineRebalancePlan
) -> None:
    _require_int("a_width", spec.a_width, 1, 30)
    _require_int("b_width", spec.b_width, 1, 18)
    if spec.direction not in _PIPELINE_REBALANCE_DIRECTIONS:
        raise OperatorProofError(
            'spec.direction must be "forward" or "backward"'
        )
    if plan.direction not in _PIPELINE_REBALANCE_DIRECTIONS:
        raise OperatorProofError(
            'plan.direction must be "forward" or "backward"'
        )
    for name, value in (
        ("spec.ce_present", spec.ce_present),
        ("spec.init_zero", spec.init_zero),
        ("plan.ce_mapped", plan.ce_mapped),
        ("plan.reset_mapped", plan.reset_mapped),
    ):
        if not isinstance(value, bool):
            raise OperatorProofError(f"{name} must be a boolean")
    if spec.init_zero is not True:
        raise OperatorProofError(
            "only INIT-zero register stages may be rebalanced"
        )
    if spec.reset_kind not in _PIPELINE_REBALANCE_RESET_KINDS:
        raise OperatorProofError(
            'reset_kind must be "none" or "sync_reset_zero"'
        )
    if plan.template != "DSP48E2_MREG_REBALANCE_V1":
        raise OperatorProofError("unsupported DSP rewrite template")
    expected_moves = _PIPELINE_REBALANCE_SET_REGISTERS[plan.direction]
    if (
        not isinstance(plan.set_registers, tuple)
        or len(plan.set_registers) != 3
        or any(
            not isinstance(move, tuple)
            or len(move) != 2
            or not isinstance(move[0], str)
            or isinstance(move[1], bool)
            or not isinstance(move[1], int)
            for move in plan.set_registers
        )
        or plan.set_registers != expected_moves
    ):
        raise OperatorProofError(
            "plan.set_registers must be the exact register moves"
            " for plan.direction"
        )
    if plan.ce_mapped is not True:
        raise OperatorProofError("plan.ce_mapped must be True")
    if plan.reset_mapped != (spec.reset_kind == "sync_reset_zero"):
        raise OperatorProofError(
            "plan.reset_mapped disagrees with spec.reset_kind"
        )


def prove_pipeline_rebalance(
    spec: PipelineRebalanceSpec,
    plan: PipelineRebalancePlan,
    *,
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    """Prove 1-step-induction bisimulation of the MREG lag around the multiplier.

    Source (forward view): input stage (a2, b2) with AREG=BREG=1, MREG=0; the
    ALU input at cycle t is a2(t)*b2(t). Target: AREG=BREG=0, MREG=1; the ALU
    input at cycle t is mreg(t). Relation R: mreg == a2*b2 over the concrete
    full-width product (which also discharges the zero-init case, 0 == 0*0).
    The backward move proves the same symmetric relation; only the recorded
    direction differs. Port-path register counts are invariant by construction.
    """
    import z3

    _validate_pipeline_rebalance(spec, plan)
    _require_int("timeout_ms", timeout_ms, 1, 120000)
    preconditions = {
        "direction_consistent": plan.direction == spec.direction,
        "register_moves_exact": (
            plan.set_registers
            == _PIPELINE_REBALANCE_SET_REGISTERS[spec.direction]
        ),
    }
    if not all(preconditions.values()):
        return {
            "status": "failed",
            "semantics_version": SEMANTICS_VERSION,
            "preconditions": preconditions,
            "obligations": [],
        }

    a_width = spec.a_width
    b_width = spec.b_width
    product_width = a_width + b_width

    def multiply(a_term, b_term):
        return z3.ZeroExt(b_width, a_term) * z3.ZeroExt(a_width, b_term)

    a_in = z3.BitVec("rebalance_a_in", a_width)
    b_in = z3.BitVec("rebalance_b_in", b_width)
    a2 = z3.BitVec("rebalance_a2", a_width)
    b2 = z3.BitVec("rebalance_b2", b_width)
    mreg = z3.BitVec("rebalance_mreg", product_width)
    ce = z3.Bool("rebalance_ce")
    controls = [ce]
    if spec.reset_kind == "sync_reset_zero":
        reset = z3.Bool("rebalance_reset")
        controls.append(reset)
    else:
        reset = z3.BoolVal(False)
    zero_a = z3.BitVecVal(0, a_width)
    zero_b = z3.BitVecVal(0, b_width)
    zero_m = z3.BitVecVal(0, product_width)

    # Source: a2' = rst ? 0 : (ce ? a_in : a2); b2' likewise.
    a2_next = z3.If(reset, zero_a, z3.If(ce, a_in, a2))
    b2_next = z3.If(reset, zero_b, z3.If(ce, b_in, b2))
    # Target: mreg' = rst ? 0 : (ce ? a_in*b_in : mreg).
    mreg_next = z3.If(reset, zero_m, z3.If(ce, multiply(a_in, b_in), mreg))

    relation = mreg == multiply(a2, b2)
    relation_next = mreg_next == multiply(a2_next, b2_next)

    # Exhaustive case split over the shared boolean controls: the mismatch
    # is unsat iff every control assignment is unsat, and substituting the
    # concrete controls collapses the ITE-fed multiplier the solver cannot
    # bit-blast against its retimed counterpart within the timeout.
    step_mismatch = z3.And(relation, z3.Not(relation_next))
    step_cases = [step_mismatch]
    for control in controls:
        step_cases = [
            z3.substitute(case, (control, z3.BoolVal(value)))
            for case in step_cases
            for value in (True, False)
        ]

    obligations = [
        _solver_obligation(
            "rebalance_init",
            zero_m != multiply(zero_a, zero_b),
            timeout_ms,
        ),
        _solver_obligation(
            "rebalance_step",
            z3.Or(*step_cases),
            timeout_ms,
        ),
        _solver_obligation(
            "rebalance_output",
            z3.And(relation, multiply(a2, b2) != mreg),
            timeout_ms,
        ),
    ]
    status = (
        "proved" if all(item["result"] == "unsat" for item in obligations)
        else "failed"
    )
    return {
        "status": status,
        "semantics_version": SEMANTICS_VERSION,
        "preconditions": preconditions,
        "obligations": obligations,
    }


def build_pipeline_rebalance_authorization(
    source_dcp_sha256: str,
    source_region_sha256: str,
    spec: PipelineRebalanceSpec,
    plan: PipelineRebalancePlan,
    *,
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    proof = prove_pipeline_rebalance(spec, plan, timeout_ms=timeout_ms)
    return _build_authorization(
        "dsp_pipeline_rebalance", source_dcp_sha256, source_region_sha256,
        spec, plan, proof,
    )


_PIPELINE_REBALANCE_CANDIDATE_ID_RE = re.compile(r"rebalance:[0-9a-f]{20}")
_PIPELINE_REBALANCE_SPEC_INT_FIELDS = ("a_width", "b_width")
_PIPELINE_REBALANCE_SPEC_BOOL_FIELDS = ("ce_present", "init_zero")
_PIPELINE_REBALANCE_SPEC_STR_FIELDS = ("direction", "reset_kind")


def pipeline_rebalance_spec_from_extracted_facts(
    facts: dict[str, Any],
) -> PipelineRebalanceSpec:
    """Recover the exact pipeline-rebalance contract from hash-bound facts."""
    if not isinstance(facts, dict):
        raise OperatorProofError("pipeline-rebalance facts must be an object")
    if (
        facts.get("kind") != "pipeline_rebalance_contract_facts"
        or facts.get("version") != 1
    ):
        raise OperatorProofError("unsupported pipeline-rebalance facts schema")
    if facts.get("status") != "success" or facts.get("proof_status") != (
        "pipeline_rebalance_facts_extracted_unproved"
    ):
        raise OperatorProofError(
            "pipeline-rebalance facts are incomplete or have invalid authority"
        )
    if facts.get("mutation_eligible") is not False:
        raise OperatorProofError(
            "pipeline-rebalance facts have invalid pre-mutation authority"
        )
    if facts.get("fresh_rediscovery") is not True:
        raise OperatorProofError(
            "pipeline-rebalance facts are not a fresh rediscovery"
        )
    _validate_identity(facts.get("design_sha256"), "facts.design_sha256")
    candidate_id = facts.get("candidate_id")
    if (
        not isinstance(candidate_id, str)
        or _PIPELINE_REBALANCE_CANDIDATE_ID_RE.fullmatch(candidate_id) is None
    ):
        raise OperatorProofError(
            "pipeline-rebalance candidate identity is invalid"
        )
    claimed_region = facts.get("region_sha256")
    payload = dict(facts)
    payload.pop("region_sha256", None)
    for wrapper_key in ("candidate_id", "fresh_rediscovery", "runtime_s"):
        payload.pop(wrapper_key, None)
    if not isinstance(claimed_region, str) or _digest(payload) != claimed_region:
        raise OperatorProofError("pipeline-rebalance region digest mismatch")
    spec = facts.get("spec")
    expected_fields = set(
        _PIPELINE_REBALANCE_SPEC_INT_FIELDS
        + _PIPELINE_REBALANCE_SPEC_BOOL_FIELDS
        + _PIPELINE_REBALANCE_SPEC_STR_FIELDS
    )
    if not isinstance(spec, dict) or set(spec) != expected_fields:
        raise OperatorProofError(
            "recovered pipeline-rebalance spec fields are incomplete"
        )
    for name in _PIPELINE_REBALANCE_SPEC_INT_FIELDS:
        if isinstance(spec[name], bool) or not isinstance(spec[name], int):
            raise OperatorProofError(f"recovered spec {name} must be an integer")
    for name in _PIPELINE_REBALANCE_SPEC_BOOL_FIELDS:
        if not isinstance(spec[name], bool):
            raise OperatorProofError(f"recovered spec {name} must be a boolean")
    for name in _PIPELINE_REBALANCE_SPEC_STR_FIELDS:
        if not isinstance(spec[name], str):
            raise OperatorProofError(f"recovered spec {name} must be a string")
    return PipelineRebalanceSpec(**spec)


def build_pipeline_rebalance_authorization_from_facts(
    facts: dict[str, Any],
    plan: PipelineRebalancePlan,
    *,
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    """Build pre-mutation authorization using exact recovered region identity."""
    spec = pipeline_rebalance_spec_from_extracted_facts(facts)
    dcp_sha256 = facts.get("design_sha256")
    region_sha256 = facts.get("region_sha256")
    _validate_identity(dcp_sha256, "facts.design_sha256")
    _validate_identity(region_sha256, "facts.region_sha256")
    return build_pipeline_rebalance_authorization(
        dcp_sha256, region_sha256, spec, plan, timeout_ms=timeout_ms
    )
