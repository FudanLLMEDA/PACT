"""Conservative standalone Verilog emitter for validated product-sum plans."""

from __future__ import annotations

import argparse
import re
from typing import Any

from ..registered_arithmetic_ir import (
    ConstantOrBiasTerm,
    ControlSelectedArm,
    PlanKind,
    PartitionedAccumulationGroup,
    QuantizedProductTerm,
)
from .common import envelope, fail, object_digest, plan_input


def _identifier(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_]", "_", value)
    if not token or token[0].isdigit():
        token = "regarith_" + token
    return token


def render_product_sum_verilog(plan_value: Any, *, module_name: str = "regarith_product_sum") -> dict[str, Any]:
    plan = plan_input(plan_value)
    if PlanKind(plan.plan_kind) != PlanKind.QUANTIZED_PRODUCT_SUM:
        fail("EMITTER_PLAN_KIND_UNSUPPORTED", "this emitter accepts only quantized_product_sum")
    if len(plan.control_semantics) != 1:
        fail("EMITTER_NONUNIFORM_CONTROL_NOT_PORTED", "heterogeneous stage controls require the future DAG emitter")
    if any(isinstance(item, ControlSelectedArm) for item in plan.data_terms):
        fail("EMITTER_CONTROL_SHELL_NOT_PORTED", "alternate/hold/zero control-selected shells are not emitted yet")
    if any(
        isinstance(group, PartitionedAccumulationGroup)
        for group in plan.accumulation_groups
    ):
        fail(
            "EMITTER_PARTITIONED_PRODUCT_SUM_BACKEND_UNAVAILABLE",
            "partitioned full-precision product sums require the future validated DCP bridge",
        )
    if len(plan.accumulation_groups) != 1 or plan.accumulation_groups[0].accumulation_order != "after_term_loss":
        fail("EMITTER_ACCUM_BEFORE_TRUNC_NOT_PORTED", "only separately truncated product sums are emitted")
    products = [item for item in plan.data_terms if isinstance(item, QuantizedProductTerm)]
    constants = [item for item in plan.data_terms if isinstance(item, ConstantOrBiasTerm)]
    losses = {item.boundary_id: item for item in plan.loss_boundaries}
    output_id = plan.output_shell.output_word_ids[0]
    output = next(item for item in plan.observable_word_boundaries if item.word_id == output_id)
    lines = [
        "// Generated from a validated REGARITH_PRODUCT_SUM_V1 plan.",
        f"module {_identifier(module_name)} (",
        "  input wire clk,",
        "  input wire ce,",
        "  input wire reset,",
    ]
    for index, term in enumerate(products):
        signed = " signed" if term.data_signed else ""
        lines.append(f"  input wire{signed} [{term.data_width - 1}:0] in_{index},")
    lines.append(f"  output reg [{output.width - 1}:0] y"); lines.append(");")
    expressions = []
    for index, term in enumerate(products):
        loss = losses[term.loss_boundary_id]
        if loss.saturation != "none" or loss.rounding not in {"none", "discard_low_bits"} or loss.bias != 0:
            fail("EMITTER_NUMERIC_BOUNDARY_NOT_PORTED", "saturation/rounding-bias product terms are typed rejections")
        coeff = f"{term.coefficient_width}'sd{term.coefficient}"
        lines.append(
            f"  (* use_dsp = \"yes\" *) wire signed [{term.full_product_width - 1}:0] product_{index} = $signed(in_{index}) * {coeff};"
        )
        lines.append(
            f"  wire signed [{term.output_width - 1}:0] term_{index} = product_{index}[{loss.slice_lsb + term.output_width - 1}:{loss.slice_lsb}];"
        )
        expressions.append(f"term_{index}")
    expressions.extend(f"{item.width}'sd{item.value}" for item in constants)
    lines.append(f"  wire [{output.width - 1}:0] next_y = " + " + ".join(expressions) + ";")
    control = plan.control_semantics[0]
    edge = control.clock.edge
    reset_kind = control.reset_set.kind
    if reset_kind not in {"synchronous_reset", "asynchronous_reset"} or control.reset_set.value != 0:
        fail("EMITTER_RESET_SET_NOT_PORTED", "emitter currently supports only explicit clear-to-zero")
    sensitivity = f"{edge} clk" + (" or posedge reset" if reset_kind == "asynchronous_reset" else "")
    lines.extend([
        f"  always @({sensitivity}) begin",
        "    if (reset) y <= '0;",
        "    else if (ce) y <= next_y;",
        "  end",
        "endmodule",
        "",
    ])
    verilog = "\n".join(lines)
    return envelope(
        "registered_product_sum_verilog_emitter",
        source_artifact_sha256=plan.source_artifact_sha256,
        candidate_family_id=plan.candidate_family_id,
        candidate_hash=plan.plan_digest(),
        status="success",
        payload={
            "proof_template_id": "REGARITH_PRODUCT_SUM_V1",
            "module_name": _identifier(module_name),
            "verilog": verilog,
            "verilog_sha256": object_digest(verilog),
            "post_synthesis_inventory_required": True,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan")
    parser.add_argument("--module-name", default="regarith_product_sum")
    args = parser.parse_args()
    import json
    try:
        result = render_product_sum_verilog(args.plan, module_name=args.module_name)
    except Exception as exc:
        from .common import rejection_from_exception
        result = rejection_from_exception("registered_product_sum_verilog_emitter", exc)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
