"""Construct and serialize the four normalized registered-arithmetic examples."""

from __future__ import annotations

from pathlib import Path

from ..registered_arithmetic_ir import (
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
    ReplaceableOldCone,
    ResetSetSemantics,
    RetainedSideConsumers,
    ShiftedPassthroughTerm,
    WordBoundary,
)


def _word(word_id: str, width: int, signed: bool, cycle: int) -> WordBoundary:
    return WordBoundary(
        word_id=word_id,
        width=width,
        signed=signed,
        lsb_index=0,
        endpoint_ids=tuple(f"{word_id}[{bit}]" for bit in range(width)),
        cycle_alignment=cycle,
    )


def _control(control_id: str, clock_id: str) -> ControlSemantics:
    return ControlSemantics(
        control_id=control_id,
        clock=ClockIdentity(clock_id, "normalized_net", "posedge"),
        clock_enable=ClockEnableSemantics(
            signal_id=f"{control_id}.ce",
            active_level="high",
            asserted_behavior="advance",
            deasserted_behavior="hold",
            semantic_source="normalized_fixture_explicit",
        ),
        reset_set=ResetSetSemantics(
            kind="synchronous_reset",
            signal_id=f"{control_id}.reset",
            active_level="high",
            value=0,
            priority="before_ce",
            semantic_source="normalized_fixture_explicit",
        ),
        init_value=0,
        hold_behavior="hold",
        zero_behavior="reset_or_selected_zero_arm",
        alternate_arm_behavior="none",
        valid_behavior="not_observable",
        backpressure_behavior="not_observable",
    )


def _assemble(
    *,
    kind: PlanKind,
    source_sha: str,
    candidate_id: str,
    inputs: tuple[WordBoundary, ...],
    output: WordBoundary,
    terms: tuple,
    losses: tuple[NumericLossBoundary, ...],
    groups: tuple[ModularAccumulationGroup, ...],
    output_cycle: int,
    operation: str,
    old_cone: tuple[str, ...],
    wall_kind: str,
) -> RegisteredOperatorPlan:
    control = _control("operator_control", "clk_main")
    plan = RegisteredOperatorPlan(
        schema_version=SCHEMA_VERSION,
        plan_kind=kind,
        source_artifact_sha256=source_sha,
        candidate_family_id=candidate_id,
        observable_word_boundaries=(*inputs, output),
        registered_input_words=tuple(
            RegisteredInputWord(item, "explicit_fixture_registered_word")
            for item in inputs
        ),
        data_terms=terms,
        loss_boundaries=losses,
        accumulation_groups=groups,
        control_semantics=(control,),
        register_stage_schedule=RegisterStageSchedule(
            input_cycle_alignment=tuple(
                CycleAlignment(item.word_id, item.cycle_alignment, "operator_input_cycle")
                for item in inputs
            ),
            output_cycle_alignment=(
                CycleAlignment(output.word_id, output_cycle, "operator_input_cycle"),
            ),
            stages=(RegisterStage(
                stage_id="registered_operator_stage",
                cycle_index=output_cycle,
                input_word_ids=tuple(item.word_id for item in inputs),
                output_word_ids=(output.word_id,),
                control_semantics_id=control.control_id,
                operation=operation,
                proof_obligations=(
                    "exact_bit_vector_data_obligation",
                    "cycle_alignment_obligation",
                    "control_transition_obligation",
                ),
            ),),
        ),
        output_shell=OutputShell(
            output_word_ids=(output.word_id,),
            integration_behavior="preserve_registered_observable_shell",
            control_arm_ids=(),
            proof_obligations=("output_shell_truth_table_and_register_schedule",),
        ),
        replaceable_old_cone=ReplaceableOldCone(
            object_ids=old_cone,
            closure_status="fixture_closed",
            proof_obligations=("old_arithmetic_driver_sweep",),
        ),
        retained_side_consumers=RetainedSideConsumers(
            consumers=(),
            coverage_status="fixture_proves_no_side_consumers",
            proof_obligations=("complete_side_consumer_inventory",),
        ),
        expected_wall_coverage=ExpectedWallCoverage(
            evidence_kind=wall_kind,
            critical_path_count=8,
            expected_fraction_ppm=850_000,
            evidence_id=f"{candidate_id}:wall",
        ),
    )
    return plan.validate()


def build_simple_signed_fixed_product_example() -> RegisteredOperatorPlan:
    """One DSP-sized signed product using Optical-like shift/accumulator facts."""
    source = _word("tensor_component", 27, True, 0)
    output = _word("tensor_product_q", 32, True, 1)
    product_loss = NumericLossBoundary(
        "tensor_product_slice", "tensor_product", 46, 32, 14,
        "none", "modular_wrap", "none", "discard_low_bits", 0,
    )
    wrap_loss = NumericLossBoundary(
        "tensor_accumulator_wrap", "tensor_accumulator", 32, 32, 0,
        "none", "modular_wrap", "none", "none", 0,
    )
    term = QuantizedProductTerm(
        "tensor_fixed_product", source.word_id, 27, True,
        170026, 19, False, 46, 32, True,
        product_loss.boundary_id, 1,
    )
    group = ModularAccumulationGroup(
        "tensor_product_group", (term.term_id,), 32, 32,
        "after_term_loss", "modular_wrap", wrap_loss.boundary_id,
        ("DSP48E2_MUL_SHIFT_ACCUM_V1:fixed_point_product_slice_and_wrap",),
    )
    return _assemble(
        kind=PlanKind.SINGLE_FIXED_PRODUCT,
        source_sha="1" * 64,
        candidate_id="example:optical-simple-signed-product",
        inputs=(source,), output=output, terms=(term,),
        losses=(product_loss, wrap_loss), groups=(group,), output_cycle=1,
        operation="DSP48E2_MUL_SHIFT_ACCUM_V1",
        old_cone=("tensor_mul_lut_cloud", "tensor_mul_carry_chain"),
        wall_kind="optical_tensor_fixed_point_fixture",
    )


def build_optical_product_sum_example() -> RegisteredOperatorPlan:
    left = _word("optical_term_left", 32, True, 0)
    right = _word("optical_term_right", 32, True, 0)
    output = _word("optical_product_sum_q", 32, True, 2)
    left_loss = NumericLossBoundary(
        "left_product_truncation", "left_product", 51, 32, 19,
        "none", "modular_wrap", "none", "discard_low_bits", 0,
    )
    right_loss = NumericLossBoundary(
        "right_product_truncation", "right_product", 51, 32, 19,
        "none", "modular_wrap", "none", "discard_low_bits", 0,
    )
    sum_loss = NumericLossBoundary(
        "product_sum_wrap", "post_term_loss_accumulation", 34, 32, 0,
        "none", "modular_wrap", "none", "none", 0,
    )
    terms = (
        QuantizedProductTerm(
            "left_quantized_product", left.word_id, 32, True,
            170026, 19, False, 51, 32, True, left_loss.boundary_id, 1,
        ),
        QuantizedProductTerm(
            "right_quantized_product", right.word_id, 32, True,
            -155104, 19, True, 51, 32, True, right_loss.boundary_id, 1,
        ),
    )
    group = ModularAccumulationGroup(
        "optical_modular_product_sum",
        tuple(term.term_id for term in terms),
        34, 32, "after_term_loss", "modular_wrap", sum_loss.boundary_id,
        ("each_product_truncated_before_modular_accumulation",),
    )
    return _assemble(
        kind=PlanKind.QUANTIZED_PRODUCT_SUM,
        source_sha="2" * 64,
        candidate_id="example:optical-registered-product-sum",
        inputs=(left, right), output=output, terms=terms,
        losses=(left_loss, right_loss, sum_loss), groups=(group,), output_cycle=2,
        operation="registered_fir_like_quantized_product_sum",
        old_cone=("post_v7_product_0", "post_v7_product_1", "post_v7_sum"),
        wall_kind="optical_post_tensor_registered_product_sum_fixture",
    )


def build_fir_recurrence_transport_example() -> RegisteredOperatorPlan:
    state = _word("fir_recurrence_state", 32, False, 0)
    output = _word("fir_delayed_state", 32, False, 64)
    transport_loss = NumericLossBoundary(
        "fir_transport_no_loss", "recurrence_transport", 32, 32, 0,
        "none", "modular_wrap", "none", "none", 0,
    )
    term = ShiftedPassthroughTerm(
        "fir_state_transport", state.word_id, 0, 32, False,
        transport_loss.boundary_id, 64,
    )
    group = ModularAccumulationGroup(
        "fir_transport_group", (term.term_id,), 32, 32,
        "after_term_loss", "modular_wrap", transport_loss.boundary_id,
        ("DSP_PREG_SRL_RECURRENCE_CUT_V1:transaction_age_transport",),
    )
    return _assemble(
        kind=PlanKind.RECURRENCE_TRANSPORT_CUT,
        source_sha="3" * 64,
        candidate_id="example:fir-recurrence-transport",
        inputs=(state,), output=output, terms=(term,),
        losses=(transport_loss,), groups=(group,), output_cycle=64,
        operation="DSP_PREG_SRL_RECURRENCE_CUT_V1",
        old_cone=("fir_dsp_recurrence_carrier", "fir_long_transport_bank"),
        wall_kind="fir_recurrence_transport_fixture",
    )


def build_vtr_root_divider_rejection_example() -> RegisteredOperatorPlan:
    state = _word("vtr_divider_recurrence_state", 33, False, 0)
    output = _word("vtr_root_digit_state", 33, False, 34)
    boundary = NumericLossBoundary(
        "vtr_recurrence_no_loss", "root_divider_recurrence", 33, 33, 0,
        "none", "modular_wrap", "none", "none", 0,
    )
    term = ShiftedPassthroughTerm(
        "vtr_root_divider_state", state.word_id, 0, 33, False,
        boundary.boundary_id, 34,
    )
    group = ModularAccumulationGroup(
        "vtr_root_divider_group", (term.term_id,), 33, 33,
        "after_term_loss", "modular_wrap", boundary.boundary_id,
        ("classify_root_digit_and_divider_feedback_recurrence",),
    )
    return _assemble(
        kind=PlanKind.UNSUPPORTED_RECURRENCE_ROOT_DIVIDER,
        source_sha="4" * 64,
        candidate_id="example:vtr-root-divider-recurrence",
        inputs=(state,), output=output, terms=(term,),
        losses=(boundary,), groups=(group,), output_cycle=34,
        operation="unsupported_root_divider_recurrence_classification",
        old_cone=("sqrt_root_digit_feedback", "divider_final_digit_feedback"),
        wall_kind="vtr_root_divider_correct_abstention_fixture",
    )


def build_all_examples() -> dict[str, RegisteredOperatorPlan]:
    return {
        "simple_signed_fixed_product.json": build_simple_signed_fixed_product_example(),
        "optical_quantized_product_sum.json": build_optical_product_sum_example(),
        "fir_recurrence_transport.json": build_fir_recurrence_transport_example(),
        "vtr_root_divider_rejection.json": build_vtr_root_divider_rejection_example(),
    }


def write_examples(directory: Path | None = None) -> tuple[Path, ...]:
    target = directory or Path(__file__).resolve().parent
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for name, plan in build_all_examples().items():
        path = target / name
        path.write_text(plan.to_canonical_json() + "\n", encoding="ascii")
        written.append(path)
    return tuple(written)


if __name__ == "__main__":
    for output_path in write_examples():
        print(output_path)
