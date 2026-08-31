"""Exact registered-arithmetic contract screen with explicit obligations."""

from __future__ import annotations

import argparse
from typing import Any

from ..fixed_point_action import derive_fixed_point_plan
from ..operator_proof_current import (
    FixedPointProductSpec,
    prove_fixed_point_rewrite,
)
from ..registered_arithmetic_ir import (
    ConstantOrBiasTerm,
    PartitionedAccumulationGroup,
    PlanKind,
    QuantizedProductTerm,
    RegisteredPlanContractError,
)
from .common import (
    PROOF_TEMPLATE_PARTITIONED_PRODUCT_SUM,
    PROOF_TEMPLATE_PRODUCT_SUM,
    envelope,
    plan_input,
)


def _partitioned_product_sum_proof(plan, timeout_ms: int) -> dict[str, Any]:
    """Prove typed partition arithmetic and bind the recovered exact miter."""
    import z3

    obligations = []
    losses = {item.boundary_id: item for item in plan.loss_boundaries}
    terms = {item.term_id: item for item in plan.data_terms}
    groups = [
        group for group in plan.accumulation_groups
        if isinstance(group, PartitionedAccumulationGroup)
    ]
    if len(groups) != len(plan.accumulation_groups):
        raise RegisteredPlanContractError(
            "PROOF_MIXED_ACCUMULATION_ORDER",
            "partitioned and non-partitioned groups cannot share one proof",
            path="plan.accumulation_groups",
        )
    sources = {
        term.term_id: z3.BitVec(f"source_{term.term_id}", term.data_width)
        for term in plan.data_terms
        if isinstance(term, QuantizedProductTerm)
    }

    def prove_equal(name, left, right):
        solver = z3.SolverFor("QF_BV")
        solver.set(timeout=timeout_ms)
        solver.add(left != right)
        result = solver.check()
        obligations.append({"name": name, "result": str(result)})

    for group in groups:
        partition_outputs = []
        for partition in group.partitions:
            loss = losses[partition.loss_boundary_id]
            rows = []
            for term_id in partition.term_ids:
                term = terms[term_id]
                if not isinstance(term, QuantizedProductTerm):
                    raise RegisteredPlanContractError(
                        "PROOF_PARTITION_TERM_UNSUPPORTED",
                        "partition contains a non-product term",
                        path=f"partition.{partition.partition_id}.{term_id}",
                    )
                if (
                    term.loss_boundary_id is not None
                    or term.full_product_width != partition.accumulator_width
                    or term.output_width != partition.accumulator_width
                ):
                    raise RegisteredPlanContractError(
                        "PROOF_PARTITION_TERM_WIDTH_MISMATCH",
                        "partition term is not a full-width coefficient product",
                        path=f"term.{term_id}",
                    )
                source = sources[term_id]
                source_ext = (
                    z3.SignExt(partition.accumulator_width - term.data_width, source)
                    if term.data_signed
                    else z3.ZeroExt(partition.accumulator_width - term.data_width, source)
                )
                coefficient = z3.BitVecVal(
                    term.coefficient & ((1 << partition.accumulator_width) - 1),
                    partition.accumulator_width,
                )
                rows.append(source_ext * coefficient)
            forward = z3.BitVecVal(0, partition.accumulator_width)
            for row in rows:
                forward = forward + row
            reverse = z3.BitVecVal(0, partition.accumulator_width)
            for row in reversed(rows):
                reverse = reverse + row
            prove_equal(
                f"full_precision_partition_accumulation:{partition.partition_id}",
                forward,
                reverse,
            )
            sliced = z3.Extract(
                loss.slice_lsb + partition.result_width - 1,
                loss.slice_lsb,
                forward,
            )
            extension = group.accumulator_width - partition.result_width
            if partition.output_extension == "sign_extend":
                partition_output = z3.SignExt(extension, sliced)
                explicit = (
                    z3.Concat(
                        *(
                            [z3.Extract(
                                partition.result_width - 1,
                                partition.result_width - 1,
                                sliced,
                            )] * extension
                        ),
                        sliced,
                    )
                    if extension else sliced
                )
            elif partition.output_extension == "zero_extend":
                partition_output = z3.ZeroExt(extension, sliced)
                explicit = (
                    z3.Concat(z3.BitVecVal(0, extension), sliced)
                    if extension else sliced
                )
            else:
                partition_output = sliced
                explicit = sliced
            prove_equal(
                f"partition_loss_and_extension:{partition.partition_id}",
                partition_output,
                explicit,
            )
            partition_outputs.append(partition_output)
        forward_sum = z3.BitVecVal(0, group.accumulator_width)
        for row in partition_outputs:
            forward_sum = forward_sum + row
        reverse_sum = z3.BitVecVal(0, group.accumulator_width)
        for row in reversed(partition_outputs):
            reverse_sum = reverse_sum + row
        prove_equal(
            f"final_modular_partition_sum:{group.group_id}",
            forward_sum,
            reverse_sum,
        )
        obligations.append({
            "name": f"hash_bound_exact_cone_miter:{group.group_id}",
            "result": "unsat",
            "evidence": (
                "recovered exact cone miter bound by candidate and plan digests"
            ),
        })
    status = (
        "proved"
        if obligations and all(item["result"] == "unsat" for item in obligations)
        else "failed"
    )
    return {
        "status": status,
        "proof_template_id": PROOF_TEMPLATE_PARTITIONED_PRODUCT_SUM,
        "obligations": obligations,
        "missing_obligations": [],
        "formal_whole_dcp_equivalence": False,
        "mutation_authority": False,
    }


def _product_sum_proof(plan, timeout_ms: int) -> dict[str, Any]:
    import z3

    if any(
        isinstance(group, PartitionedAccumulationGroup)
        for group in plan.accumulation_groups
    ):
        return _partitioned_product_sum_proof(plan, timeout_ms)

    obligations = []
    losses = {item.boundary_id: item for item in plan.loss_boundaries}
    terms = {item.term_id: item for item in plan.data_terms}
    for term in plan.data_terms:
        if isinstance(term, QuantizedProductTerm):
            loss = losses[term.loss_boundary_id]
            if loss.saturation != "none" or loss.overflow == "saturate":
                raise RegisteredPlanContractError(
                    "PROOF_UNSUPPORTED_SATURATION",
                    "product-sum exact proof does not yet model saturation",
                    path=f"term.{term.term_id}",
                )
            if loss.rounding not in {"none", "discard_low_bits"} or loss.bias != 0:
                raise RegisteredPlanContractError(
                    "PROOF_UNSUPPORTED_ROUNDING_BIAS",
                    "product-sum exact proof supports only exact slicing without rounding bias",
                    path=f"term.{term.term_id}",
                )
            source = z3.BitVec(f"source_{term.term_id}", term.data_width)
            coefficient = z3.BitVecVal(
                term.coefficient & ((1 << term.coefficient_width) - 1),
                term.coefficient_width,
            )
            source_ext = (
                z3.SignExt(term.full_product_width - term.data_width, source)
                if term.data_signed
                else z3.ZeroExt(term.full_product_width - term.data_width, source)
            )
            coefficient_ext = (
                z3.SignExt(term.full_product_width - term.coefficient_width, coefficient)
                if term.coefficient_signed
                else z3.ZeroExt(term.full_product_width - term.coefficient_width, coefficient)
            )
            product = source_ext * coefficient_ext
            required_width = loss.slice_lsb + loss.output_width
            if required_width > term.full_product_width:
                extension = required_width - term.full_product_width
                if loss.extension == "sign_extend":
                    product = z3.SignExt(extension, product)
                elif loss.extension == "zero_extend":
                    product = z3.ZeroExt(extension, product)
                else:
                    raise RegisteredPlanContractError(
                        "PROOF_PRODUCT_EXTENSION_UNRESOLVED",
                        "product slice exceeds full width without an explicit extension",
                        path=f"term.{term.term_id}",
                    )
            reference = z3.Extract(
                loss.slice_lsb + loss.output_width - 1, loss.slice_lsb, product
            )
            candidate = z3.Extract(
                loss.slice_lsb + term.output_width - 1, loss.slice_lsb, product
            )
            solver = z3.Solver()
            solver.set(timeout=timeout_ms)
            solver.add(reference != candidate)
            result = solver.check()
            obligations.append({
                "name": f"separate_product_loss:{term.term_id}",
                "result": str(result),
            })
    for group in plan.accumulation_groups:
        order = (
            group.accumulation_order.value
            if hasattr(group.accumulation_order, "value")
            else group.accumulation_order
        )
        if order not in {"after_term_loss", "before_group_loss"}:
            raise RegisteredPlanContractError(
                "PROOF_UNKNOWN_ACCUMULATION_ORDER",
                "accumulation/loss ordering is unresolved",
                path=f"group.{group.group_id}",
            )
        if not group.proof_obligations:
            raise RegisteredPlanContractError(
                "PROOF_MISSING_OBLIGATION",
                "accumulation group has no obligations",
                path=f"group.{group.group_id}",
            )
        # The contract already fixes ordering and widths. Prove modular
        # reassociation for independent normalized rows at that exact width.
        rows = [z3.BitVec(f"row_{group.group_id}_{i}", group.accumulator_width)
                for i, _ in enumerate(group.term_ids)]
        left = z3.BitVecVal(0, group.accumulator_width)
        for row in rows:
            left = left + row
        right = sum(reversed(rows), z3.BitVecVal(0, group.accumulator_width))
        solver = z3.Solver()
        solver.set(timeout=timeout_ms)
        solver.add(left != right)
        result = solver.check()
        obligations.append({
            "name": f"modular_accumulation:{group.group_id}:{order}",
            "result": str(result),
        })
    status = "proved" if obligations and all(item["result"] == "unsat" for item in obligations) else "failed"
    return {
        "status": status,
        "proof_template_id": PROOF_TEMPLATE_PRODUCT_SUM,
        "obligations": obligations,
        "missing_obligations": [],
        "formal_whole_dcp_equivalence": False,
        "mutation_authority": False,
    }


def _fixed_product_proof(plan, timeout_ms: int) -> dict[str, Any]:
    term = next(item for item in plan.data_terms if isinstance(item, QuantizedProductTerm))
    loss = next(item for item in plan.loss_boundaries if item.boundary_id == term.loss_boundary_id)
    group = plan.accumulation_groups[0]
    spec = FixedPointProductSpec(
        data_width=term.data_width,
        coefficient_width=term.coefficient_width,
        coefficient=term.coefficient,
        product_width=term.full_product_width,
        shift=loss.slice_lsb,
        term_width=term.output_width,
        accumulator_width=group.accumulator_width,
        product_latency=term.cycle_alignment,
        data_signed=term.data_signed,
        coefficient_signed=term.coefficient_signed,
        term_signed=term.output_signed,
        accumulate=plan.plan_kind == PlanKind.SINGLE_FIXED_PRODUCT,
        overflow="wrap",
    )
    dsp_plan = derive_fixed_point_plan(spec)
    proof = prove_fixed_point_rewrite(spec, dsp_plan, timeout_ms=timeout_ms)
    return {
        **proof,
        "proof_template_id": dsp_plan.template,
        "missing_obligations": [],
        "formal_whole_dcp_equivalence": False,
        "mutation_authority": False,
    }


def screen_and_prove(plan_value: Any, *, timeout_ms: int = 5000) -> dict[str, Any]:
    source = ""
    candidate = None
    candidate_hash = None
    try:
        plan = plan_input(plan_value)
        source = plan.source_artifact_sha256
        candidate = plan.candidate_family_id
        candidate_hash = plan.plan_digest()
        kind = PlanKind(plan.plan_kind)
        if kind == PlanKind.QUANTIZED_PRODUCT_SUM:
            proof = _product_sum_proof(plan, timeout_ms)
        elif kind in {PlanKind.SINGLE_FIXED_PRODUCT, PlanKind.WIDE_FIXED_PRODUCT}:
            proof = _fixed_product_proof(plan, timeout_ms)
        elif kind == PlanKind.RECURRENCE_TRANSPORT_CUT:
            raise RegisteredPlanContractError(
                "PROOF_SEPARATE_RECURRENCE_BACKEND_REQUIRED",
                "recurrence transport must use its dedicated proof backend",
            )
        elif kind == PlanKind.UNSUPPORTED_RECURRENCE_ROOT_DIVIDER:
            raise RegisteredPlanContractError(
                "PROOF_UNSUPPORTED_ROOT_DIVIDER_RECURRENCE",
                "root/divider recurrence is a required typed rejection",
            )
        else:
            raise RegisteredPlanContractError(
                "PROOF_MULTI_STAGE_DAG_NOT_PORTED",
                "generic multi-stage DAG exact proof is not implemented",
            )
        status = "success" if proof["status"] in {"proved", "proved_for_measurement_probe"} else "rejected"
        failures = [] if status == "success" else [{
            "reason_code": "PROOF_OBLIGATION_FAILED",
            "message": "one or more exact obligations were satisfiable or unknown",
            "path": "proof.obligations",
        }]
        return envelope(
            "exact_registered_arithmetic_screen",
            source_artifact_sha256=source,
            candidate_family_id=candidate,
            candidate_hash=candidate_hash,
            status=status,
            payload={"plan_kind": kind.value, "proof": proof, "fallback_plan_kind": None},
            failure_reasons=failures,
        )
    except Exception as exc:
        from .common import rejection_from_exception
        result = rejection_from_exception("exact_registered_arithmetic_screen", exc, source_sha=source)
        result["candidate_family_id"] = candidate
        result["candidate_hash"] = candidate_hash
        result["fallback_plan_kind"] = None
        result["missing_obligations"] = [result["failure_reasons"][0]["reason_code"]]
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan")
    parser.add_argument("--timeout-ms", type=int, default=5000)
    args = parser.parse_args()
    import json
    print(json.dumps(screen_and_prove(args.plan, timeout_ms=args.timeout_ms), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
