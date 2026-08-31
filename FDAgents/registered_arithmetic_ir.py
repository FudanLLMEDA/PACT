"""Standalone, fail-closed IR for registered arithmetic rewrites.

This module deliberately has no Agent, LLM, skill, or DCP-tool dependencies.
It is a semantic interchange contract; it grants no mutation authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, fields, is_dataclass
from enum import Enum
from typing import Any, Mapping, TypeVar


SCHEMA_VERSION = "regarith-ir-v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class PlanKind(str, Enum):
    SINGLE_FIXED_PRODUCT = "single_fixed_product"
    WIDE_FIXED_PRODUCT = "wide_fixed_product"
    QUANTIZED_PRODUCT_SUM = "quantized_product_sum"
    MULTI_STAGE_PRODUCT_DAG = "multi_stage_product_dag"
    RECURRENCE_TRANSPORT_CUT = "recurrence_transport_cut"
    UNSUPPORTED_RECURRENCE_ROOT_DIVIDER = (
        "unsupported_recurrence_root_divider"
    )


class LoweringStatus(str, Enum):
    SELECTED = "selected"
    REJECTED = "rejected"


class AccumulationOrder(str, Enum):
    """Loss/accumulation ordering with no implicit reassociation."""

    AFTER_TERM_LOSS = "after_term_loss"
    BEFORE_GROUP_LOSS = "before_group_loss"
    PARTITIONED_FULL_PRECISION_THEN_SHIFT = (
        "partitioned_full_precision_then_shift"
    )


class RegisteredPlanContractError(ValueError):
    """A machine-readable failure of the registered-plan contract."""

    def __init__(self, reason_code: str, message: str, *, path: str = "plan"):
        self.reason_code = reason_code
        self.path = path
        self.message = message
        super().__init__(f"{reason_code} at {path}: {message}")


def _fail(reason_code: str, message: str, path: str = "plan") -> None:
    raise RegisteredPlanContractError(reason_code, message, path=path)


@dataclass(frozen=True)
class WordBoundary:
    word_id: str
    width: int
    signed: bool
    lsb_index: int
    endpoint_ids: tuple[str, ...]
    cycle_alignment: int | None


@dataclass(frozen=True)
class RegisteredInputWord:
    boundary: WordBoundary
    register_semantics: str


@dataclass(frozen=True)
class NumericLossBoundary:
    boundary_id: str
    applies_to: str
    input_width: int
    output_width: int
    slice_lsb: int
    extension: str
    overflow: str
    saturation: str
    rounding: str
    bias: int


@dataclass(frozen=True)
class QuantizedProductTerm:
    term_id: str
    source_word_id: str
    data_width: int
    data_signed: bool
    coefficient: int
    coefficient_width: int
    coefficient_signed: bool
    full_product_width: int
    output_width: int
    output_signed: bool
    loss_boundary_id: str | None
    cycle_alignment: int | None


@dataclass(frozen=True)
class ShiftedPassthroughTerm:
    term_id: str
    source_word_id: str
    shift: int
    output_width: int
    output_signed: bool
    loss_boundary_id: str | None
    cycle_alignment: int | None


@dataclass(frozen=True)
class ConstantOrBiasTerm:
    term_id: str
    value: int
    width: int
    signed: bool
    cycle_alignment: int | None


@dataclass(frozen=True)
class ControlSelectedArm:
    term_id: str
    predicate_id: str
    selected_term_ids: tuple[str, ...]
    behavior: str
    alternate_arm_id: str | None
    cycle_alignment: int | None


DataTerm = (
    QuantizedProductTerm
    | ShiftedPassthroughTerm
    | ConstantOrBiasTerm
    | ControlSelectedArm
)


@dataclass(frozen=True)
class ModularAccumulationGroup:
    group_id: str
    term_ids: tuple[str, ...]
    accumulator_width: int
    output_width: int
    accumulation_order: str
    overflow: str
    loss_boundary_id: str | None
    proof_obligations: tuple[str, ...]


@dataclass(frozen=True)
class FullPrecisionAccumulationPartition:
    """One full-precision numerator followed by one explicit loss boundary."""

    partition_id: str
    term_ids: tuple[str, ...]
    accumulator_width: int
    result_width: int
    result_signed: bool
    output_extension: str
    loss_boundary_id: str
    proof_obligations: tuple[str, ...]


@dataclass(frozen=True)
class PartitionedAccumulationGroup:
    """A modular sum of independently quantized full-precision partitions."""

    group_id: str
    term_ids: tuple[str, ...]
    partitions: tuple[FullPrecisionAccumulationPartition, ...]
    accumulator_width: int
    output_width: int
    accumulation_order: AccumulationOrder | str
    overflow: str
    loss_boundary_id: str
    proof_obligations: tuple[str, ...]


AccumulationGroup = ModularAccumulationGroup | PartitionedAccumulationGroup


@dataclass(frozen=True)
class ClockIdentity:
    identity: str
    identity_kind: str
    edge: str


@dataclass(frozen=True)
class ClockEnableSemantics:
    signal_id: str | None
    active_level: str
    asserted_behavior: str
    deasserted_behavior: str
    semantic_source: str


@dataclass(frozen=True)
class ResetSetSemantics:
    kind: str
    signal_id: str | None
    active_level: str
    value: int | None
    priority: str
    semantic_source: str


@dataclass(frozen=True)
class ControlSemantics:
    control_id: str
    clock: ClockIdentity
    clock_enable: ClockEnableSemantics
    reset_set: ResetSetSemantics
    init_value: int
    hold_behavior: str
    zero_behavior: str
    alternate_arm_behavior: str
    valid_behavior: str
    backpressure_behavior: str


@dataclass(frozen=True)
class CycleAlignment:
    word_id: str
    cycle: int | None
    reference: str


@dataclass(frozen=True)
class RegisterStage:
    stage_id: str
    cycle_index: int | None
    input_word_ids: tuple[str, ...]
    output_word_ids: tuple[str, ...]
    control_semantics_id: str
    operation: str
    proof_obligations: tuple[str, ...]


@dataclass(frozen=True)
class RegisterStageSchedule:
    input_cycle_alignment: tuple[CycleAlignment, ...]
    output_cycle_alignment: tuple[CycleAlignment, ...]
    stages: tuple[RegisterStage, ...]


@dataclass(frozen=True)
class OutputShell:
    output_word_ids: tuple[str, ...]
    integration_behavior: str
    control_arm_ids: tuple[str, ...]
    proof_obligations: tuple[str, ...]


@dataclass(frozen=True)
class ReplaceableOldCone:
    object_ids: tuple[str, ...]
    closure_status: str
    proof_obligations: tuple[str, ...]


@dataclass(frozen=True)
class RetainedSideConsumer:
    consumer_id: str
    source_word_id: str
    retained_behavior: str


@dataclass(frozen=True)
class RetainedSideConsumers:
    consumers: tuple[RetainedSideConsumer, ...]
    coverage_status: str
    proof_obligations: tuple[str, ...]


@dataclass(frozen=True)
class ExpectedWallCoverage:
    evidence_kind: str
    critical_path_count: int
    expected_fraction_ppm: int
    evidence_id: str


@dataclass(frozen=True)
class LoweringDecision:
    status: LoweringStatus
    backend_id: str | None
    reason: str
    plan_kind: PlanKind


@dataclass(frozen=True)
class RegisteredOperatorPlan:
    schema_version: str
    plan_kind: PlanKind | str
    source_artifact_sha256: str
    candidate_family_id: str
    observable_word_boundaries: tuple[WordBoundary, ...]
    registered_input_words: tuple[RegisteredInputWord, ...]
    data_terms: tuple[DataTerm, ...]
    loss_boundaries: tuple[NumericLossBoundary, ...]
    accumulation_groups: tuple[AccumulationGroup, ...]
    control_semantics: tuple[ControlSemantics, ...]
    register_stage_schedule: RegisterStageSchedule
    output_shell: OutputShell
    replaceable_old_cone: ReplaceableOldCone
    retained_side_consumers: RetainedSideConsumers
    expected_wall_coverage: ExpectedWallCoverage

    def validate(self) -> "RegisteredOperatorPlan":
        return validate_registered_operator_plan(self)

    def normalize(self) -> "RegisteredOperatorPlan":
        return validate_registered_operator_plan(self)

    def to_canonical_json(self) -> str:
        self.validate()
        return json.dumps(
            _canonical(self),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )

    def plan_digest(self) -> str:
        return hashlib.sha256(self.to_canonical_json().encode("ascii")).hexdigest()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RegisteredOperatorPlan":
        return registered_operator_plan_from_dict(value)


def _canonical(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {
            field.name: _canonical(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    _fail("UNSERIALIZABLE_FIELD", f"unsupported value type {type(value).__name__}")


def _nonempty(value: Any, code: str, path: str) -> None:
    if not isinstance(value, str) or not value:
        _fail(code, "must be a nonempty string", path)


def _integer(value: Any, low: int, high: int, code: str, path: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        _fail(code, f"must be an integer in {low}..{high}", path)


def _check_alignment(value: int | None, path: str) -> None:
    if value is None:
        _fail("MISSING_CYCLE_ALIGNMENT", "cycle alignment is unresolved", path)
    _integer(value, -1_000_000, 1_000_000, "INVALID_CYCLE_ALIGNMENT", path)


_RESET_KINDS = {
    "none",
    "synchronous_reset",
    "asynchronous_reset",
    "synchronous_set",
    "asynchronous_set",
    "preserved_external_shell",
}


def validate_registered_operator_plan(
    plan: RegisteredOperatorPlan,
) -> RegisteredOperatorPlan:
    if not isinstance(plan, RegisteredOperatorPlan):
        _fail("INVALID_PLAN_TYPE", "expected RegisteredOperatorPlan")
    if plan.schema_version != SCHEMA_VERSION:
        _fail("UNKNOWN_SCHEMA_VERSION", "schema_version is not supported", "plan.schema_version")
    try:
        kind = plan.plan_kind if isinstance(plan.plan_kind, PlanKind) else PlanKind(plan.plan_kind)
    except (TypeError, ValueError):
        _fail("UNKNOWN_PLAN_KIND", "plan kind is not registered", "plan.plan_kind")
    if not isinstance(plan.source_artifact_sha256, str) or _SHA256_RE.fullmatch(
        plan.source_artifact_sha256
    ) is None:
        _fail("INVALID_SOURCE_ARTIFACT_SHA256", "must be a lowercase SHA-256", "plan.source_artifact_sha256")
    _nonempty(plan.candidate_family_id, "MISSING_CANDIDATE_FAMILY_ID", "plan.candidate_family_id")

    if not plan.observable_word_boundaries:
        _fail("MISSING_WORD_BOUNDARY", "observable word boundaries are empty")
    words: dict[str, WordBoundary] = {}
    for index, word in enumerate(plan.observable_word_boundaries):
        path = f"plan.observable_word_boundaries[{index}]"
        if not isinstance(word, WordBoundary):
            _fail("INVALID_WORD_BOUNDARY", "wrong boundary type", path)
        _nonempty(word.word_id, "INVALID_WORD_BOUNDARY", path + ".word_id")
        if word.word_id in words:
            _fail("DUPLICATE_WORD_ID", "word id is repeated", path + ".word_id")
        _integer(word.width, 1, 1_000_000, "INVALID_WORD_WIDTH", path + ".width")
        if not isinstance(word.signed, bool):
            _fail("INVALID_SIGNEDNESS", "signed must be boolean", path + ".signed")
        _check_alignment(word.cycle_alignment, path + ".cycle_alignment")
        if not word.endpoint_ids or any(not isinstance(item, str) or not item for item in word.endpoint_ids):
            _fail("MISSING_WORD_ENDPOINTS", "word endpoints are incomplete", path + ".endpoint_ids")
        words[word.word_id] = word

    if not plan.registered_input_words:
        _fail("MISSING_REGISTERED_INPUT", "registered input words are empty")
    for index, item in enumerate(plan.registered_input_words):
        path = f"plan.registered_input_words[{index}]"
        if not isinstance(item, RegisteredInputWord) or item.boundary.word_id not in words:
            _fail("INVALID_REGISTERED_INPUT", "input does not bind an observable boundary", path)
        _nonempty(item.register_semantics, "MISSING_REGISTER_SEMANTICS", path + ".register_semantics")

    losses: dict[str, NumericLossBoundary] = {}
    for index, loss in enumerate(plan.loss_boundaries):
        path = f"plan.loss_boundaries[{index}]"
        if not isinstance(loss, NumericLossBoundary):
            _fail("INVALID_LOSS_BOUNDARY", "wrong loss boundary type", path)
        _nonempty(loss.boundary_id, "INVALID_LOSS_BOUNDARY", path + ".boundary_id")
        if loss.boundary_id in losses:
            _fail("DUPLICATE_LOSS_BOUNDARY", "loss boundary id is repeated", path)
        _integer(loss.input_width, 1, 1_000_000, "INVALID_LOSS_WIDTH", path + ".input_width")
        _integer(loss.output_width, 1, 1_000_000, "INVALID_LOSS_WIDTH", path + ".output_width")
        _integer(loss.slice_lsb, 0, loss.input_width - 1, "INVALID_LOSS_SLICE", path + ".slice_lsb")
        if loss.slice_lsb + loss.output_width > loss.input_width and loss.extension == "none":
            _fail("INVALID_LOSS_SLICE", "slice exceeds input without extension", path)
        if loss.extension not in {"none", "sign_extend", "zero_extend"}:
            _fail("UNKNOWN_EXTENSION", "extension semantics are unknown", path + ".extension")
        if loss.overflow not in {"none", "modular_wrap", "saturate"}:
            _fail("UNKNOWN_OVERFLOW", "overflow semantics are unknown", path + ".overflow")
        if loss.saturation not in {"none", "signed", "unsigned"}:
            _fail("UNKNOWN_SATURATION", "saturation semantics are unknown", path + ".saturation")
        if loss.rounding not in {"none", "discard_low_bits", "round_half_up", "round_to_even"}:
            _fail("UNKNOWN_ROUNDING", "rounding semantics are unknown", path + ".rounding")
        losses[loss.boundary_id] = loss

    partitioned_term_ids = {
        term_id
        for group in plan.accumulation_groups
        if isinstance(group, PartitionedAccumulationGroup)
        for term_id in group.term_ids
    }
    term_ids: set[str] = set()
    arithmetic_terms: list[DataTerm] = []
    control_arms: list[ControlSelectedArm] = []
    for index, term in enumerate(plan.data_terms):
        path = f"plan.data_terms[{index}]"
        if not isinstance(term, (QuantizedProductTerm, ShiftedPassthroughTerm, ConstantOrBiasTerm, ControlSelectedArm)):
            _fail("UNKNOWN_DATA_TERM", "data term type is unsupported", path)
        _nonempty(term.term_id, "INVALID_TERM_ID", path + ".term_id")
        if term.term_id in term_ids:
            _fail("DUPLICATE_TERM_ID", "term id is repeated", path + ".term_id")
        term_ids.add(term.term_id)
        _check_alignment(term.cycle_alignment, path + ".cycle_alignment")
        if isinstance(term, (QuantizedProductTerm, ShiftedPassthroughTerm)):
            if term.source_word_id not in words:
                _fail("UNKNOWN_SOURCE_WORD", "term source word is absent", path + ".source_word_id")
            partitioned_product = (
                isinstance(term, QuantizedProductTerm)
                and term.term_id in partitioned_term_ids
            )
            if (
                not partitioned_product
                and (not term.loss_boundary_id or term.loss_boundary_id not in losses)
            ):
                _fail("MISSING_LOSS_BOUNDARY", "term has no resolved loss boundary", path + ".loss_boundary_id")
            if (
                partitioned_product
                and term.loss_boundary_id is not None
            ):
                _fail(
                    "PARTITIONED_TERM_HAS_INDEPENDENT_LOSS",
                    "partitioned full-precision terms cannot also have term-local loss",
                    path + ".loss_boundary_id",
                )
            arithmetic_terms.append(term)
        elif isinstance(term, QuantizedProductTerm):  # pragma: no cover - kept for type narrowing
            arithmetic_terms.append(term)
        elif isinstance(term, ConstantOrBiasTerm):
            _integer(term.width, 1, 1_000_000, "INVALID_TERM_WIDTH", path + ".width")
            arithmetic_terms.append(term)
        else:
            if term.behavior not in {"data", "zero", "hold", "alternate_arm"}:
                _fail("UNKNOWN_CONTROL_ARM_BEHAVIOR", "control arm behavior is unknown", path + ".behavior")
            control_arms.append(term)
    # Product-specific width checks are separate so no failed product-sum can
    # be reinterpreted as a simpler plan kind.
    products = [term for term in arithmetic_terms if isinstance(term, QuantizedProductTerm)]
    for term in products:
        if term.full_product_width < term.data_width + term.coefficient_width:
            _fail("INVALID_PRODUCT_WIDTH", "full product cannot represent its operands", f"term.{term.term_id}")
    if kind in {PlanKind.SINGLE_FIXED_PRODUCT, PlanKind.WIDE_FIXED_PRODUCT} and len(products) != 1:
        _fail("FIXED_PRODUCT_TERM_COUNT", "fixed-product plan requires exactly one product term")
    if kind == PlanKind.QUANTIZED_PRODUCT_SUM and len(products) < 2:
        _fail("PRODUCT_SUM_TERM_COUNT", "quantized product sum requires at least two product terms")

    if not plan.accumulation_groups:
        _fail("MISSING_ACCUMULATION_GROUP", "accumulation groups are empty")
    grouped_terms: set[str] = set()
    partition_ids: set[str] = set()
    for index, group in enumerate(plan.accumulation_groups):
        path = f"plan.accumulation_groups[{index}]"
        if not isinstance(
            group, (ModularAccumulationGroup, PartitionedAccumulationGroup)
        ) or not group.term_ids:
            _fail("INVALID_ACCUMULATION_GROUP", "group is absent or empty", path)
        if any(term_id not in term_ids for term_id in group.term_ids):
            _fail("UNKNOWN_GROUP_TERM", "group references an unknown term", path + ".term_ids")
        if group.overflow != "modular_wrap":
            _fail("UNKNOWN_ACCUMULATION_OVERFLOW", "only explicit modular wrap is represented", path + ".overflow")
        order = (
            group.accumulation_order.value
            if isinstance(group.accumulation_order, AccumulationOrder)
            else group.accumulation_order
        )
        expected_orders = {
            "after_term_loss", "before_group_loss",
            "partitioned_full_precision_then_shift",
        }
        if order not in expected_orders:
            _fail("UNKNOWN_ACCUMULATION_ORDER", "accumulation/loss ordering is unknown", path)
        if isinstance(group, PartitionedAccumulationGroup):
            if order != "partitioned_full_precision_then_shift":
                _fail(
                    "PARTITIONED_ACCUMULATION_ORDER_MISMATCH",
                    "partitioned group must declare partitioned full-precision order",
                    path + ".accumulation_order",
                )
            if kind != PlanKind.QUANTIZED_PRODUCT_SUM:
                _fail(
                    "PARTITIONED_ACCUMULATION_PLAN_KIND",
                    "partitioned accumulation is only represented for product sums",
                    path,
                )
            if len(group.partitions) < 2:
                _fail(
                    "PARTITIONED_ACCUMULATION_COUNT",
                    "partitioned accumulation requires at least two partitions",
                    path + ".partitions",
                )
            member_ids: list[str] = []
            for part_index, partition in enumerate(group.partitions):
                part_path = f"{path}.partitions[{part_index}]"
                if not isinstance(partition, FullPrecisionAccumulationPartition):
                    _fail("INVALID_ACCUMULATION_PARTITION", "wrong partition type", part_path)
                _nonempty(
                    partition.partition_id,
                    "INVALID_ACCUMULATION_PARTITION",
                    part_path + ".partition_id",
                )
                if partition.partition_id in partition_ids:
                    _fail(
                        "DUPLICATE_ACCUMULATION_PARTITION",
                        "partition id is repeated",
                        part_path + ".partition_id",
                    )
                partition_ids.add(partition.partition_id)
                if not partition.term_ids or any(
                    term_id not in group.term_ids for term_id in partition.term_ids
                ):
                    _fail(
                        "INVALID_ACCUMULATION_PARTITION_TERMS",
                        "partition terms are empty or outside the parent group",
                        part_path + ".term_ids",
                    )
                member_ids.extend(partition.term_ids)
                _integer(
                    partition.accumulator_width, 1, 1_000_000,
                    "INVALID_PARTITION_WIDTH", part_path + ".accumulator_width",
                )
                _integer(
                    partition.result_width, 1, partition.accumulator_width,
                    "INVALID_PARTITION_WIDTH", part_path + ".result_width",
                )
                if not isinstance(partition.result_signed, bool):
                    _fail(
                        "INVALID_PARTITION_SIGNEDNESS",
                        "partition result_signed must be boolean",
                        part_path + ".result_signed",
                    )
                if partition.output_extension not in {
                    "none", "sign_extend", "zero_extend"
                }:
                    _fail(
                        "UNKNOWN_PARTITION_EXTENSION",
                        "partition output extension is unknown",
                        part_path + ".output_extension",
                    )
                expected_extension = (
                    "none"
                    if partition.result_width == group.accumulator_width
                    else "sign_extend" if partition.result_signed else "zero_extend"
                )
                if partition.result_width > group.accumulator_width or (
                    partition.output_extension != expected_extension
                ):
                    _fail(
                        "PARTITION_EXTENSION_MISMATCH",
                        "partition extension does not match its result and group widths",
                        part_path + ".output_extension",
                    )
                loss = losses.get(partition.loss_boundary_id)
                if (
                    loss is None
                    or loss.input_width != partition.accumulator_width
                    or loss.output_width != partition.result_width
                ):
                    _fail(
                        "PARTITION_LOSS_BOUNDARY_MISMATCH",
                        "partition loss boundary does not bind its exact widths",
                        part_path + ".loss_boundary_id",
                    )
                if not partition.proof_obligations:
                    _fail(
                        "EMPTY_PROOF_OBLIGATIONS",
                        "partition proof obligations are empty",
                        part_path + ".proof_obligations",
                    )
            if len(member_ids) != len(set(member_ids)) or set(member_ids) != set(group.term_ids):
                _fail(
                    "PARTITION_TERM_COVERAGE_MISMATCH",
                    "partitions must cover every group term exactly once",
                    path + ".partitions",
                )
        elif order == "partitioned_full_precision_then_shift":
            _fail(
                "PARTITIONED_ACCUMULATION_TYPE_REQUIRED",
                "partitioned order requires the typed partitioned group",
                path,
            )
        if not group.loss_boundary_id or group.loss_boundary_id not in losses:
            _fail("MISSING_LOSS_BOUNDARY", "group has no resolved output loss boundary", path + ".loss_boundary_id")
        if not group.proof_obligations:
            _fail("EMPTY_PROOF_OBLIGATIONS", "accumulation proof obligations are empty", path)
        grouped_terms.update(group.term_ids)
    arithmetic_ids = {term.term_id for term in arithmetic_terms}
    if not arithmetic_ids.issubset(grouped_terms):
        _fail("UNGROUPED_DATA_TERM", "not every arithmetic term belongs to a group")

    if not plan.control_semantics:
        _fail("MISSING_CONTROL_SEMANTICS", "control semantics are empty")
    control_ids = set()
    for index, control in enumerate(plan.control_semantics):
        path = f"plan.control_semantics[{index}]"
        if not isinstance(control, ControlSemantics):
            _fail("INVALID_CONTROL_SEMANTICS", "wrong control type", path)
        _nonempty(control.control_id, "INVALID_CONTROL_ID", path + ".control_id")
        control_ids.add(control.control_id)
        if control.clock.edge not in {"posedge", "negedge"}:
            _fail("UNKNOWN_CLOCK_EDGE", "clock edge is unknown", path + ".clock.edge")
        _nonempty(control.clock.identity, "MISSING_CLOCK_IDENTITY", path + ".clock.identity")
        if control.clock_enable.active_level not in {"high", "low", "always"}:
            _fail("UNKNOWN_CE_SEMANTICS", "CE active level is unknown", path + ".clock_enable")
        if control.clock_enable.deasserted_behavior not in {"hold", "not_applicable"}:
            _fail("UNKNOWN_HOLD_BEHAVIOR", "CE deasserted behavior is unknown", path + ".clock_enable")
        if control.reset_set.kind not in _RESET_KINDS:
            _fail("UNKNOWN_RESET_KIND", "reset/set kind is unknown", path + ".reset_set.kind")
        if control.hold_behavior not in {"hold", "not_applicable"}:
            _fail("UNKNOWN_HOLD_BEHAVIOR", "hold behavior is unknown", path + ".hold_behavior")
        if control.zero_behavior not in {"none", "reset_or_selected_zero_arm"}:
            _fail("UNKNOWN_ZERO_BEHAVIOR", "zero behavior is unknown", path + ".zero_behavior")
        if control.alternate_arm_behavior not in {"none", "explicit_control_selected_arm"}:
            _fail("UNKNOWN_ALTERNATE_ARM_BEHAVIOR", "alternate-arm behavior is unknown", path + ".alternate_arm_behavior")

    schedule = plan.register_stage_schedule
    if not isinstance(schedule, RegisterStageSchedule) or not schedule.stages:
        _fail("MISSING_REGISTER_SCHEDULE", "register-stage schedule is empty")
    for collection_name, alignments in (
        ("input_cycle_alignment", schedule.input_cycle_alignment),
        ("output_cycle_alignment", schedule.output_cycle_alignment),
    ):
        if not alignments:
            _fail("MISSING_CYCLE_ALIGNMENT", "alignment set is empty", f"plan.register_stage_schedule.{collection_name}")
        for index, alignment in enumerate(alignments):
            if alignment.word_id not in words:
                _fail("UNKNOWN_ALIGNMENT_WORD", "alignment references an unknown word", f"{collection_name}[{index}]")
            _check_alignment(alignment.cycle, f"{collection_name}[{index}].cycle")
    for index, stage in enumerate(schedule.stages):
        path = f"plan.register_stage_schedule.stages[{index}]"
        _check_alignment(stage.cycle_index, path + ".cycle_index")
        if stage.control_semantics_id not in control_ids:
            _fail("UNKNOWN_STAGE_CONTROL", "stage control contract is absent", path)
        if not stage.proof_obligations:
            _fail("EMPTY_PROOF_OBLIGATIONS", "stage proof obligations are empty", path)

    for arm in control_arms:
        if any(item not in term_ids for item in arm.selected_term_ids):
            _fail("UNKNOWN_CONTROL_ARM_TERM", "control arm references an unknown term", f"term.{arm.term_id}")
    if not plan.output_shell.proof_obligations:
        _fail("EMPTY_PROOF_OBLIGATIONS", "output-shell proof obligations are empty", "plan.output_shell")
    if not plan.replaceable_old_cone.proof_obligations:
        _fail("EMPTY_PROOF_OBLIGATIONS", "old-cone proof obligations are empty", "plan.replaceable_old_cone")
    if not plan.retained_side_consumers.proof_obligations:
        _fail("EMPTY_PROOF_OBLIGATIONS", "side-consumer proof obligations are empty", "plan.retained_side_consumers")
    return plan


_LOWERING_BACKENDS = {
    PlanKind.SINGLE_FIXED_PRODUCT: "existing_dsp48e2_fixed_point_emitter",
    PlanKind.WIDE_FIXED_PRODUCT: "existing_adjacent_pair_wide_dsp_emitter",
    PlanKind.QUANTIZED_PRODUCT_SUM: "registered_product_sum_dcp_bridge",
    PlanKind.MULTI_STAGE_PRODUCT_DAG: "ooc_module_explicit_inventory_emitter",
    PlanKind.RECURRENCE_TRANSPORT_CUT: "fir_dsp_srl_recurrence_backend",
}


def select_lowering(plan: RegisteredOperatorPlan) -> LoweringDecision:
    """Select exactly one typed backend, preserving rejection authority."""
    plan.validate()
    kind = plan.plan_kind if isinstance(plan.plan_kind, PlanKind) else PlanKind(plan.plan_kind)
    if kind == PlanKind.UNSUPPORTED_RECURRENCE_ROOT_DIVIDER:
        return LoweringDecision(
            status=LoweringStatus.REJECTED,
            backend_id=None,
            reason=(
                "root/divider recurrence is unsupported by registered arithmetic "
                "data-cone and FIR recurrence-transport lowerings"
            ),
            plan_kind=kind,
        )
    return LoweringDecision(
        status=LoweringStatus.SELECTED,
        backend_id=_LOWERING_BACKENDS[kind],
        reason=f"validated {kind.value} contract selected its dedicated backend",
        plan_kind=kind,
    )


T = TypeVar("T")


def _strict_dataclass(cls: type[T], raw: Any, path: str, converters: Mapping[str, Any] | None = None) -> T:
    if not isinstance(raw, Mapping):
        _fail("INVALID_FIELD_TYPE", "expected an object", path)
    expected = {field.name for field in fields(cls)}
    missing = expected - set(raw)
    unknown = set(raw) - expected
    if missing:
        _fail("MISSING_FIELD", f"missing fields: {sorted(missing)}", path)
    if unknown:
        _fail("UNKNOWN_FIELD", f"unknown fields: {sorted(unknown)}", path)
    values = dict(raw)
    for name, converter in (converters or {}).items():
        values[name] = converter(values[name])
    try:
        return cls(**values)
    except (TypeError, ValueError) as exc:
        _fail("INVALID_FIELD_TYPE", str(exc), path)


def _tuple_of(cls: type[T], path: str):
    def convert(raw: Any) -> tuple[T, ...]:
        if not isinstance(raw, (list, tuple)):
            _fail("INVALID_FIELD_TYPE", "expected an array", path)
        return tuple(_strict_dataclass(cls, item, f"{path}[{index}]") for index, item in enumerate(raw))
    return convert


def registered_operator_plan_from_dict(value: Mapping[str, Any]) -> RegisteredOperatorPlan:
    """Strictly decode canonical-compatible data; missing/unknown keys fail closed."""
    def words(raw: Any) -> tuple[WordBoundary, ...]:
        return _tuple_of(WordBoundary, "plan.observable_word_boundaries")(raw)

    def inputs(raw: Any) -> tuple[RegisteredInputWord, ...]:
        if not isinstance(raw, (list, tuple)):
            _fail("INVALID_FIELD_TYPE", "expected an array", "plan.registered_input_words")
        return tuple(
            _strict_dataclass(
                RegisteredInputWord,
                item,
                f"plan.registered_input_words[{index}]",
                {"boundary": lambda row: _strict_dataclass(WordBoundary, row, "registered_input.boundary")},
            )
            for index, item in enumerate(raw)
        )

    term_classes = (QuantizedProductTerm, ShiftedPassthroughTerm, ConstantOrBiasTerm, ControlSelectedArm)

    def terms(raw: Any) -> tuple[DataTerm, ...]:
        if not isinstance(raw, (list, tuple)):
            _fail("INVALID_FIELD_TYPE", "expected an array", "plan.data_terms")
        result = []
        for index, item in enumerate(raw):
            if not isinstance(item, Mapping):
                _fail("INVALID_FIELD_TYPE", "term is not an object", f"plan.data_terms[{index}]")
            matches = [cls for cls in term_classes if set(item) == {field.name for field in fields(cls)}]
            if len(matches) != 1:
                _fail("UNKNOWN_DATA_TERM", "term fields do not identify exactly one typed term", f"plan.data_terms[{index}]")
            converters = {"selected_term_ids": tuple} if matches[0] is ControlSelectedArm else None
            result.append(_strict_dataclass(matches[0], item, f"plan.data_terms[{index}]", converters))
        return tuple(result)

    def groups(raw: Any) -> tuple[AccumulationGroup, ...]:
        if not isinstance(raw, (list, tuple)):
            _fail("INVALID_FIELD_TYPE", "expected an array", "plan.accumulation_groups")
        result = []
        modular_fields = {field.name for field in fields(ModularAccumulationGroup)}
        partitioned_fields = {
            field.name for field in fields(PartitionedAccumulationGroup)
        }
        for index, item in enumerate(raw):
            path = f"plan.accumulation_groups[{index}]"
            if not isinstance(item, Mapping):
                _fail("INVALID_FIELD_TYPE", "group is not an object", path)
            if set(item) == modular_fields:
                result.append(_strict_dataclass(
                    ModularAccumulationGroup, item, path,
                    {"term_ids": tuple, "proof_obligations": tuple},
                ))
            elif set(item) == partitioned_fields:
                result.append(_strict_dataclass(
                    PartitionedAccumulationGroup, item, path, {
                        "term_ids": tuple,
                        "partitions": lambda rows: tuple(
                            _strict_dataclass(
                                FullPrecisionAccumulationPartition,
                                row,
                                f"{path}.partitions[{part_index}]",
                                {
                                    "term_ids": tuple,
                                    "proof_obligations": tuple,
                                },
                            )
                            for part_index, row in enumerate(rows)
                        ),
                        "proof_obligations": tuple,
                    },
                ))
            else:
                _fail(
                    "UNKNOWN_ACCUMULATION_GROUP",
                    "group fields do not identify one typed accumulation group",
                    path,
                )
        return tuple(result)

    def controls(raw: Any) -> tuple[ControlSemantics, ...]:
        if not isinstance(raw, (list, tuple)):
            _fail("INVALID_FIELD_TYPE", "expected an array", "plan.control_semantics")
        return tuple(_strict_dataclass(
            ControlSemantics, item, f"plan.control_semantics[{index}]", {
                "clock": lambda row: _strict_dataclass(ClockIdentity, row, "control.clock"),
                "clock_enable": lambda row: _strict_dataclass(ClockEnableSemantics, row, "control.clock_enable"),
                "reset_set": lambda row: _strict_dataclass(ResetSetSemantics, row, "control.reset_set"),
            },
        ) for index, item in enumerate(raw))

    def schedule(raw: Any) -> RegisterStageSchedule:
        return _strict_dataclass(RegisterStageSchedule, raw, "plan.register_stage_schedule", {
            "input_cycle_alignment": _tuple_of(CycleAlignment, "schedule.input_cycle_alignment"),
            "output_cycle_alignment": _tuple_of(CycleAlignment, "schedule.output_cycle_alignment"),
            "stages": lambda rows: tuple(_strict_dataclass(
                RegisterStage, row, f"schedule.stages[{index}]", {
                    "input_word_ids": tuple,
                    "output_word_ids": tuple,
                    "proof_obligations": tuple,
                },
            ) for index, row in enumerate(rows)),
        })

    def output_shell(raw: Any) -> OutputShell:
        return _strict_dataclass(OutputShell, raw, "plan.output_shell", {
            "output_word_ids": tuple, "control_arm_ids": tuple, "proof_obligations": tuple,
        })

    def old_cone(raw: Any) -> ReplaceableOldCone:
        return _strict_dataclass(ReplaceableOldCone, raw, "plan.replaceable_old_cone", {
            "object_ids": tuple, "proof_obligations": tuple,
        })

    def side_consumers(raw: Any) -> RetainedSideConsumers:
        return _strict_dataclass(RetainedSideConsumers, raw, "plan.retained_side_consumers", {
            "consumers": _tuple_of(RetainedSideConsumer, "retained_side_consumers.consumers"),
            "proof_obligations": tuple,
        })

    plan = _strict_dataclass(RegisteredOperatorPlan, value, "plan", {
        "plan_kind": lambda raw: raw,
        "observable_word_boundaries": words,
        "registered_input_words": inputs,
        "data_terms": terms,
        "loss_boundaries": _tuple_of(NumericLossBoundary, "plan.loss_boundaries"),
        "accumulation_groups": groups,
        "control_semantics": controls,
        "register_stage_schedule": schedule,
        "output_shell": output_shell,
        "replaceable_old_cone": old_cone,
        "retained_side_consumers": side_consumers,
        "expected_wall_coverage": lambda raw: _strict_dataclass(ExpectedWallCoverage, raw, "plan.expected_wall_coverage"),
    })
    return plan.validate()


def normalize(plan: RegisteredOperatorPlan | Mapping[str, Any]) -> RegisteredOperatorPlan:
    if isinstance(plan, RegisteredOperatorPlan):
        return plan.validate()
    if isinstance(plan, Mapping):
        return registered_operator_plan_from_dict(plan)
    _fail("INVALID_PLAN_TYPE", "normalize expects a plan or plan object")


__all__ = [
    "SCHEMA_VERSION", "PlanKind", "LoweringStatus", "LoweringDecision",
    "AccumulationOrder",
    "RegisteredPlanContractError", "WordBoundary", "RegisteredInputWord",
    "NumericLossBoundary", "QuantizedProductTerm", "ShiftedPassthroughTerm",
    "ConstantOrBiasTerm", "ModularAccumulationGroup",
    "FullPrecisionAccumulationPartition", "PartitionedAccumulationGroup",
    "ControlSelectedArm",
    "ClockIdentity", "ClockEnableSemantics", "ResetSetSemantics",
    "ControlSemantics", "CycleAlignment", "RegisterStage",
    "RegisterStageSchedule", "OutputShell", "ReplaceableOldCone",
    "RetainedSideConsumer", "RetainedSideConsumers", "ExpectedWallCoverage",
    "RegisteredOperatorPlan", "validate_registered_operator_plan", "normalize",
    "registered_operator_plan_from_dict", "select_lowering",
]
