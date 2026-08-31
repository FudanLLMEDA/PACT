"""Fail-closed adapters from existing fixed-point extraction facts to the IR."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

from .registered_arithmetic_ir import (
    SCHEMA_VERSION,
    ClockEnableSemantics,
    ClockIdentity,
    ControlSemantics,
    CycleAlignment,
    ExpectedWallCoverage,
    ModularAccumulationGroup,
    NumericLossBoundary,
    OutputShell,
    PlanKind,
    QuantizedProductTerm,
    RegisterStage,
    RegisterStageSchedule,
    RegisteredInputWord,
    RegisteredOperatorPlan,
    RegisteredPlanContractError,
    ReplaceableOldCone,
    ResetSetSemantics,
    RetainedSideConsumers,
    WordBoundary,
)


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CANDIDATE_RE = re.compile(r"fixedpoint:[0-9a-f]{20}")
_SPEC_FIELDS = {
    "data_width",
    "coefficient_width",
    "coefficient",
    "product_width",
    "shift",
    "term_width",
    "accumulator_width",
    "product_latency",
    "data_signed",
    "coefficient_signed",
    "term_signed",
    "accumulate",
    "overflow",
}
_BOUNDARY_FIELDS = {
    "input_bus",
    "output_bus",
    "cone_cells",
    "clock_pin",
    "ce_pin",
}


def _error(code: str, message: str, path: str) -> None:
    raise RegisteredPlanContractError(code, message, path=path)


def _stable_digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _int(spec: Mapping[str, Any], name: str, low: int, high: int) -> int:
    if name not in spec:
        code = (
            "ADAPTER_MISSING_SHIFT"
            if name == "shift"
            else "ADAPTER_MISSING_OUTPUT_TRUNCATION"
            if name == "term_width"
            else "ADAPTER_MISSING_SPEC_FIELD"
        )
        _error(code, f"fixed-point spec is missing {name}", f"facts.spec.{name}")
    value = spec[name]
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        _error(
            "ADAPTER_INVALID_SPEC_FIELD",
            f"{name} must be an integer in {low}..{high}",
            f"facts.spec.{name}",
        )
    return value


def _bool(spec: Mapping[str, Any], name: str) -> bool:
    if name not in spec:
        _error(
            "ADAPTER_MISSING_SPEC_FIELD",
            f"fixed-point spec is missing {name}",
            f"facts.spec.{name}",
        )
    value = spec[name]
    if not isinstance(value, bool):
        _error(
            "ADAPTER_INVALID_SPEC_FIELD",
            f"{name} must be boolean",
            f"facts.spec.{name}",
        )
    return value


def _bus_endpoints(
    value: Any, *, width: int, path: str, allow_const_zero: bool
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) != width:
        _error(
            "ADAPTER_INCOMPLETE_WORD_BOUNDARY",
            f"bus must cover exactly {width} bits",
            path,
        )
    endpoints = []
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if allow_const_zero and isinstance(item, dict) and item == {"const": 0}:
            endpoints.append(f"const_zero[{index}]")
            continue
        if (
            not isinstance(item, dict)
            or set(item) != {"cell", "pin"}
            or not isinstance(item.get("cell"), str)
            or not item["cell"]
            or not isinstance(item.get("pin"), str)
            or not item["pin"]
            or not item["pin"].startswith(item["cell"] + "/")
        ):
            _error(
                "ADAPTER_INCOMPLETE_WORD_BOUNDARY",
                "bus entry must be a recovered cell/pin pair",
                item_path,
            )
        endpoints.append(item["pin"])
    if len(set(endpoints)) != len(endpoints):
        _error("ADAPTER_DUPLICATE_WORD_ENDPOINT", "bus endpoints repeat", path)
    return tuple(endpoints)


def _validate_facts(facts: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    if not isinstance(facts, Mapping):
        _error("ADAPTER_INVALID_FACTS", "fixed-point facts must be an object", "facts")
    if facts.get("kind") != "fixed_point_product_contract_facts" or facts.get("version") != 1:
        _error("ADAPTER_UNSUPPORTED_FACTS_SCHEMA", "unsupported fixed-point fact schema", "facts")
    if (
        facts.get("status") != "success"
        or facts.get("proof_status") != "fixed_point_facts_extracted_unproved"
        or facts.get("mutation_eligible") is not False
        or facts.get("fresh_rediscovery") is not True
    ):
        _error("ADAPTER_INVALID_FACT_AUTHORITY", "facts are incomplete or stale", "facts")
    design = facts.get("design_sha256")
    region = facts.get("region_sha256")
    candidate = facts.get("candidate_id")
    if not isinstance(design, str) or _SHA256_RE.fullmatch(design) is None:
        _error("ADAPTER_INVALID_DESIGN_ID", "design identity is not a SHA-256", "facts.design_sha256")
    if not isinstance(region, str) or _SHA256_RE.fullmatch(region) is None:
        _error("ADAPTER_INVALID_REGION_ID", "region identity is not a SHA-256", "facts.region_sha256")
    if not isinstance(candidate, str) or _CANDIDATE_RE.fullmatch(candidate) is None:
        _error("ADAPTER_INVALID_CANDIDATE_ID", "candidate identity is malformed", "facts.candidate_id")
    payload = dict(facts)
    payload.pop("region_sha256", None)
    for key in ("candidate_id", "fresh_rediscovery", "runtime_s"):
        payload.pop(key, None)
    if _stable_digest(payload) != region or candidate != "fixedpoint:" + region[:20]:
        _error("ADAPTER_REGION_DIGEST_MISMATCH", "facts do not bind their region identity", "facts.region_sha256")
    spec = facts.get("spec")
    boundary = facts.get("boundary")
    if not isinstance(spec, Mapping):
        _error("ADAPTER_MISSING_SPEC", "fixed-point spec is absent", "facts.spec")
    # Detect the two required negative cases before reporting the full set.
    if "shift" not in spec:
        _error("ADAPTER_MISSING_SHIFT", "fixed-point shift amount is absent", "facts.spec.shift")
    if "term_width" not in spec:
        _error(
            "ADAPTER_MISSING_OUTPUT_TRUNCATION",
            "fixed-point output truncation width is absent",
            "facts.spec.term_width",
        )
    if set(spec) != _SPEC_FIELDS:
        _error(
            "ADAPTER_INCOMPLETE_SPEC",
            f"spec fields differ: missing={sorted(_SPEC_FIELDS - set(spec))}, "
            f"unknown={sorted(set(spec) - _SPEC_FIELDS)}",
            "facts.spec",
        )
    if not isinstance(boundary, Mapping) or set(boundary) != _BOUNDARY_FIELDS:
        _error("ADAPTER_INCOMPLETE_BOUNDARY", "fixed-point boundary fields are incomplete", "facts.boundary")
    return spec, boundary


def adapt_fixed_point_contract_facts(facts: dict[str, Any]) -> RegisteredOperatorPlan:
    """Convert one real ``fixed_point_dsp_rebuild_v1`` contract to the common IR.

    Control facts absent from the extractor are not guessed from net names.  The
    only internal-stage control values represented here are fixed properties of
    the existing DSP lowering: positive edge, shared recovered CE/hold, no
    internal reset, and zero INIT. External register reset/set behavior remains
    explicitly preserved by the output-shell contract.
    """
    spec, boundary = _validate_facts(facts)
    data_width = _int(spec, "data_width", 1, 256)
    coefficient_width = _int(spec, "coefficient_width", 1, 256)
    coefficient = _int(spec, "coefficient", 0, (1 << coefficient_width) - 1)
    product_width = _int(spec, "product_width", 1, 512)
    shift = _int(spec, "shift", 0, 511)
    term_width = _int(spec, "term_width", 1, 256)
    accumulator_width = _int(spec, "accumulator_width", 1, 256)
    latency = _int(spec, "product_latency", 0, 8)
    data_signed = _bool(spec, "data_signed")
    coefficient_signed = _bool(spec, "coefficient_signed")
    term_signed = _bool(spec, "term_signed")
    accumulate = _bool(spec, "accumulate")
    if product_width < data_width + coefficient_width:
        _error("ADAPTER_INVALID_PRODUCT_WIDTH", "product width is not full precision", "facts.spec.product_width")
    if shift + term_width > product_width:
        _error(
            "ADAPTER_INVALID_OUTPUT_TRUNCATION",
            "shifted output slice exceeds the product",
            "facts.spec.term_width",
        )
    if spec.get("overflow") != "wrap":
        _error("ADAPTER_UNSUPPORTED_OVERFLOW", "existing proof supports only wrap", "facts.spec.overflow")

    input_endpoints = _bus_endpoints(
        boundary["input_bus"], width=data_width,
        path="facts.boundary.input_bus", allow_const_zero=True,
    )
    output_endpoints = _bus_endpoints(
        boundary["output_bus"], width=term_width,
        path="facts.boundary.output_bus", allow_const_zero=False,
    )
    clock_pin = boundary["clock_pin"]
    ce_pin = boundary["ce_pin"]
    if not isinstance(clock_pin, str) or not clock_pin or not isinstance(ce_pin, str) or not ce_pin:
        _error("ADAPTER_INCOMPLETE_CONTROL_BOUNDARY", "clock/CE boundary pins are absent", "facts.boundary")
    cone = boundary["cone_cells"]
    if not isinstance(cone, list) or not cone:
        _error("ADAPTER_INCOMPLETE_OLD_CONE", "replaceable cone is empty", "facts.boundary.cone_cells")
    cone_ids = []
    for index, item in enumerate(cone):
        if (
            not isinstance(item, dict)
            or set(item) != {"cell", "type"}
            or not isinstance(item.get("cell"), str)
            or not item["cell"]
            or not isinstance(item.get("type"), str)
            or not item["type"]
        ):
            _error("ADAPTER_INCOMPLETE_OLD_CONE", "cone cell record is malformed", f"facts.boundary.cone_cells[{index}]")
        cone_ids.append(item["cell"])

    input_word = WordBoundary(
        word_id="fixed_point_input",
        width=data_width,
        signed=data_signed,
        lsb_index=0,
        endpoint_ids=input_endpoints,
        cycle_alignment=0,
    )
    output_word = WordBoundary(
        word_id="fixed_point_output",
        width=term_width,
        signed=term_signed,
        lsb_index=0,
        endpoint_ids=output_endpoints,
        cycle_alignment=latency,
    )
    term_loss = NumericLossBoundary(
        boundary_id="product_slice",
        applies_to="fixed_product",
        input_width=product_width,
        output_width=term_width,
        slice_lsb=shift,
        extension="none",
        overflow="modular_wrap",
        saturation="none",
        rounding="discard_low_bits" if shift else "none",
        bias=0,
    )
    group_loss = NumericLossBoundary(
        boundary_id="accumulator_wrap",
        applies_to="fixed_product_accumulation",
        input_width=accumulator_width,
        output_width=accumulator_width,
        slice_lsb=0,
        extension="none",
        overflow="modular_wrap",
        saturation="none",
        rounding="none",
        bias=0,
    )
    term = QuantizedProductTerm(
        term_id="fixed_product_term",
        source_word_id=input_word.word_id,
        data_width=data_width,
        data_signed=data_signed,
        coefficient=coefficient,
        coefficient_width=coefficient_width,
        coefficient_signed=coefficient_signed,
        full_product_width=product_width,
        output_width=term_width,
        output_signed=term_signed,
        loss_boundary_id=term_loss.boundary_id,
        cycle_alignment=latency,
    )
    control = ControlSemantics(
        control_id="fixed_point_dsp_stage_control",
        clock=ClockIdentity(
            identity=clock_pin,
            identity_kind="recovered_boundary_clock_pin",
            edge="posedge",
        ),
        clock_enable=ClockEnableSemantics(
            signal_id=ce_pin,
            active_level="high",
            asserted_behavior="advance",
            deasserted_behavior="hold",
            semantic_source="DSP48E2_MUL_SHIFT_ACCUM_V1_existing_emitter",
        ),
        reset_set=ResetSetSemantics(
            kind="none",
            signal_id=None,
            active_level="always",
            value=None,
            priority="not_applicable",
            semantic_source="DSP48E2_MUL_SHIFT_ACCUM_V1_RST_pins_tied_inactive",
        ),
        init_value=0,
        hold_behavior="hold",
        zero_behavior="none",
        alternate_arm_behavior="none",
        valid_behavior="not_observable_in_fixed_point_contract",
        backpressure_behavior="not_observable_in_fixed_point_contract",
    )
    plan_kind = (
        PlanKind.WIDE_FIXED_PRODUCT
        if data_width > 27 and accumulate is False
        else PlanKind.SINGLE_FIXED_PRODUCT
    )
    plan = RegisteredOperatorPlan(
        schema_version=SCHEMA_VERSION,
        plan_kind=plan_kind,
        source_artifact_sha256=facts["design_sha256"],
        candidate_family_id=facts["candidate_id"],
        observable_word_boundaries=(input_word, output_word),
        registered_input_words=(RegisteredInputWord(
            boundary=input_word,
            register_semantics="recovered_registered_input_bus",
        ),),
        data_terms=(term,),
        loss_boundaries=(term_loss, group_loss),
        accumulation_groups=(ModularAccumulationGroup(
            group_id="fixed_product_group",
            term_ids=(term.term_id,),
            accumulator_width=accumulator_width,
            output_width=accumulator_width,
            accumulation_order="after_term_loss",
            overflow="modular_wrap",
            loss_boundary_id=group_loss.boundary_id,
            proof_obligations=("DSP48E2_MUL_SHIFT_ACCUM_V1:fixed_point_product_slice_and_wrap",),
        ),),
        control_semantics=(control,),
        register_stage_schedule=RegisterStageSchedule(
            input_cycle_alignment=(CycleAlignment(input_word.word_id, 0, "operator_input_cycle"),),
            output_cycle_alignment=(CycleAlignment(output_word.word_id, latency, "operator_input_cycle"),),
            stages=(RegisterStage(
                stage_id="fixed_point_product_stage",
                cycle_index=latency,
                input_word_ids=(input_word.word_id,),
                output_word_ids=(output_word.word_id,),
                control_semantics_id=control.control_id,
                operation="DSP48E2_MUL_SHIFT_ACCUM_V1",
                proof_obligations=("product_latency_equal", "accumulator_width_equal"),
            ),),
        ),
        output_shell=OutputShell(
            output_word_ids=(output_word.word_id,),
            integration_behavior="preserve_recovered_output_register_shell",
            control_arm_ids=(),
            proof_obligations=("whole_design_port_contract_replayed",),
        ),
        replaceable_old_cone=ReplaceableOldCone(
            object_ids=tuple(cone_ids),
            closure_status="fixed_point_boundary_closed",
            proof_obligations=("fixed_point_dsp_topology_replayed", "old_cone_fully_disconnected"),
        ),
        retained_side_consumers=RetainedSideConsumers(
            consumers=(),
            coverage_status="fixed_point_extractor_closed_cone_no_retained_consumers",
            proof_obligations=("fixed_point_boundary_consumer_coverage",),
        ),
        expected_wall_coverage=ExpectedWallCoverage(
            evidence_kind="not_available_in_fixed_point_contract",
            critical_path_count=0,
            expected_fraction_ppm=0,
            evidence_id=facts["region_sha256"],
        ),
    )
    return plan.validate()


# Short aliases for direct standalone callers.
adapt_fixed_point_facts = adapt_fixed_point_contract_facts
fixed_point_facts_to_registered_plan = adapt_fixed_point_contract_facts


__all__ = [
    "adapt_fixed_point_contract_facts",
    "adapt_fixed_point_facts",
    "fixed_point_facts_to_registered_plan",
]
