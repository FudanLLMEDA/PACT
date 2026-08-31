"""Certificate-bound v3 compressor lowering for constructively proved accumulators."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .compressor_proof import (
    ConeGraph,
    _compressor_schedule,
    _digest,
    _verify_schedule_structure,
)
from .constructive_accumulator import _boundary_words, _target_source_indices


SCHEMA_VERSION = "constructive-accumulator-lowering-v2"
PROOF_TEMPLATE_ID = "CONSTRUCTIVE_ACCUMULATOR_GENERIC_V2"
_GENERIC_FINAL_ADDER_SPEC = {
    "kind": "LUT6_2_CSA3_PLUS_LUT2_XOR_DI_PLUS_CARRY8_SINGLE_CY8",
    "compressor_primitive": "LUT6_2",
    "compressor_init": "64'h96969696E8E8E8E8",
    "compressor_sum_output": "O6",
    "compressor_carry_output": "O5",
    "final_xor_primitive": "LUT2",
    "final_xor_init": "4'h6",
    "carry_primitive": "CARRY8",
    "carry_type": "SINGLE_CY8",
    "s_source": "LUT2.O",
    "di_source": "CSA_SUM",
    "shifted_operand": "CSA_CARRY_PREVIOUS_COLUMN",
    "initial_ci": 0,
    "ci_top": 0,
    "sum_output": "O",
    "carry_output": "CO",
}


def _constructive_compressor_schedule(
    width: int, constant: int, coefficients: dict[str, int]
) -> dict[str, Any]:
    """Build the compact acyclic three-row schedule used by this backend.

    Incoming carries produced by a lower column are counted when choosing how
    much of the current column to reduce, but are never consumed in the same
    stage.  This keeps the schedule acyclic while avoiding the later generic
    scheduler's unnecessary half-adder wave on this sparse affine operator.
    """
    modulus = 1 << width
    signed_terms: dict[int, list[tuple[str, int]]] = {1: [], -1: []}
    for source, raw_coefficient in sorted(coefficients.items()):
        signed = (
            raw_coefficient
            if raw_coefficient < (modulus >> 1)
            else raw_coefficient - modulus
        )
        sign = -1 if signed < 0 else 1
        value = abs(signed)
        column = 0
        while value:
            if value & 1:
                digit = 2 - (value & 3)
                signed_terms[sign * digit].append((source, column))
                value -= digit
            value >>= 1
            column += 1

    packed_rows: list[dict[str, Any]] = []
    for sign in (1, -1):
        rows: list[dict[int, str]] = []
        for source, column in sorted(
            signed_terms[sign], key=lambda item: (item[1], item[0])
        ):
            if column >= width:
                continue
            row = next(
                (candidate for candidate in rows if column not in candidate), None
            )
            if row is None:
                row = {}
                rows.append(row)
            row[column] = source
        packed_rows.extend({"sign": sign, "bits": row} for row in rows)

    columns: list[list[str]] = [[] for _ in range(width)]
    for bit in range(width):
        if (constant >> bit) & 1:
            columns[bit].append("CONST1")
    inverted_tokens = set()
    row_manifest = []
    for row_index, row in enumerate(packed_rows):
        bits = row["bits"]
        sign = row["sign"]
        manifested = []
        for bit in range(width):
            source = bits.get(bit)
            token = source if sign > 0 else (
                "CONST1" if source is None else f"NOT({source})"
            )
            if source is not None and sign < 0:
                inverted_tokens.add(token)
            if token is not None:
                columns[bit].append(token)
                manifested.append({"column": bit, "token": token})
        if sign < 0:
            columns[0].append("CONST1")
        row_manifest.append({
            "row": row_index,
            "sign": sign,
            "two_complement_correction": sign < 0,
            "bits": manifested,
        })

    initial_heights = [len(column) for column in columns]
    operations = []
    ordinal = 0
    stage = 0
    final_row_count = 3
    while any(len(column) > final_row_count for column in columns):
        next_columns: list[list[str]] = [[] for _ in range(width)]
        for bit in range(width):
            current = sorted(columns[bit])
            reductions: list[int] = []
            if stage == 0:
                reductions = [3] * (len(current) // 3)
            else:
                needed = max(
                    0, len(next_columns[bit]) + len(current) - final_row_count
                )
                available = len(current)
                while needed > 0:
                    if needed >= 2 and available >= 3:
                        reductions.append(3)
                        available -= 3
                        needed -= 2
                    elif available >= 2:
                        reductions.append(2)
                        available -= 2
                        needed -= 1
                    else:
                        break
            for input_count in reductions:
                inputs = [current.pop(0) for _ in range(input_count)]
                sum_name = f"csa_{ordinal}_sum"
                carry_name = f"csa_{ordinal}_carry"
                ordinal += 1
                next_columns[bit].append(sum_name)
                if bit + 1 < width:
                    next_columns[bit + 1].append(carry_name)
                operations.append({
                    "stage": stage,
                    "column": bit,
                    "input_count": input_count,
                    "inputs": inputs,
                    "sum": sum_name,
                    "carry": carry_name if bit + 1 < width else None,
                    "primitive": "LUT6_2",
                    "init": "64'h96969696E8E8E8E8",
                    "i5_tie": 1,
                    "sum_output": "O6",
                    "carry_output": "O5",
                })
            next_columns[bit].extend(current)
        columns = next_columns
        stage += 1
    rows = [[] for _ in range(final_row_count)]
    for column in columns:
        for row_index in range(final_row_count):
            rows[row_index].append(
                column[row_index] if len(column) > row_index else "CONST0"
            )
    return {
        "width": width,
        "signed_digit_rows": row_manifest,
        "positive_row_count": sum(row["sign"] > 0 for row in packed_rows),
        "negative_row_count": sum(row["sign"] < 0 for row in packed_rows),
        "inverted_token_count": len(inverted_tokens),
        "initial_column_heights": initial_heights,
        "max_source_priority": 0,
        "max_final_priority": stage,
        "stage_count": stage,
        "compressor_count": len(operations),
        "compressors": operations,
        "final_row_count": final_row_count,
        "final_rows": rows,
        "final_adder": dict(_GENERIC_FINAL_ADDER_SPEC),
    }


# Keep the generic compressor builder unchanged for its other production
# actions; this module deliberately owns the compact affine schedule above.
_compressor_schedule = _constructive_compressor_schedule


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", value)


def _token_expression(token: str, token_wires: dict[str, str]) -> str:
    if token == "CONST0":
        return "1'b0"
    if token == "CONST1":
        return "1'b1"
    match = re.fullmatch(r"NOT\((.*)\)", token)
    if match is not None:
        return "~" + token_wires[match.group(1)]
    return token_wires[token]


def build_constructive_lowering(
    cone_root: Path | str,
    extraction_path: Path | str,
    *,
    coefficient: int | None = None,
    solver_timeout_ms: int = 120_000,
) -> dict[str, Any]:
    graph = ConeGraph.load(cone_root)
    extraction_path = Path(extraction_path)
    extraction = json.loads(extraction_path.read_text(encoding="utf-8"))
    siblings = extraction.get("siblings")
    if (
        extraction.get("status") != "proved"
        or extraction.get("mutation_eligible") is not True
        or not isinstance(siblings, list)
        or len(siblings) != 1
    ):
        raise ValueError("constructive extraction is absent or not mutation-eligible")
    proof = siblings[0]
    recovered_coefficient = int(
        (proof.get("derived_expression") or {}).get("coefficient") or 0
    )
    operand_order = (proof.get("derived_expression") or {}).get(
        "operand_order", "natural"
    )
    if coefficient is not None and coefficient != recovered_coefficient:
        raise ValueError("constructive lowering coefficient override changed recovery")
    if (
        proof["source"]["dcp_sha256"] != graph.manifest["dcp_sha256"]
        or proof["source"]["target_stem"] != graph.manifest["target_stem"]
        or proof["source"]["connectivity_sha256"] != graph.connectivity_digest()
        or recovered_coefficient <= 0
        or operand_order not in {"natural", "swapped"}
        or proof["canonical_comparison"]["matched"] is not True
        or proof["random_falsification_guard"]["samples"] != 10_000
        or proof["random_falsification_guard"]["passed"] is not True
    ):
        raise ValueError("constructive extraction does not bind the current cone")

    first, second = _boundary_words(
        graph, swap_operands=operand_order == "swapped"
    )
    target_positions = _target_source_indices(graph)
    width = target_positions[-1] + 1
    modulus = 1 << width
    coefficients: dict[str, int] = {}
    source_nets: dict[str, str] = {}
    for word in (first, second):
        for index, net in sorted(word.nets_by_index.items()):
            label = f"{word.role}[{index}]"
            value = 1 << index
            if word is first:
                value *= recovered_coefficient
            if index == word.signed_width - 1:
                value = -value
            coefficients[label] = value % modulus
            source_nets[label] = net
    weighted_sum = {
        "modulus_width": width,
        "constant": 0,
        "coefficients": coefficients,
        "quadratic_coefficients": [],
        "polynomial_degree": 1,
        "zero_coefficient_count": 0,
    }
    spec = _compressor_schedule(width, 0, coefficients)
    if _verify_schedule_structure(weighted_sum, spec) is None:
        raise ValueError("constructive compressor schedule failed structural proof")
    # The schedule checker establishes exact token/column conservation.  Close
    # the only local arithmetic lemma exhaustively rather than sending the
    # already constructively extracted 69-input function back through SMT.
    for assignment in range(8):
        bits = [(assignment >> index) & 1 for index in range(3)]
        parity = bits[0] ^ bits[1] ^ bits[2]
        majority = int(sum(bits) >= 2)
        if sum(bits) != parity + 2 * majority:
            raise ValueError("local full-adder identity failed")
    for assignment in range(4):
        bits = [(assignment >> index) & 1 for index in range(2)]
        if sum(bits) != (bits[0] ^ bits[1]) + 2 * (bits[0] & bits[1]):
            raise ValueError("local half-adder identity failed")
    spec_result = "proved_constructively"

    old_cells = sorted(graph.cells)
    certificate: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "constructive_registered_accumulator_lowering",
        "proof_template_id": PROOF_TEMPLATE_ID,
        "status": "proved",
        "source_identity": {
            "dcp_sha256": graph.manifest["dcp_sha256"],
            "design": graph.manifest["design"],
            "part": graph.manifest["part"],
            "target_stem": graph.manifest["target_stem"],
            "connectivity_sha256": graph.connectivity_digest(),
        },
        "constructive_proof": {
            "artifact_sha256": hashlib.sha256(extraction_path.read_bytes()).hexdigest(),
            "expression_sha256": proof["canonical_comparison"]["actual_expression_sha256"],
            "coefficient": recovered_coefficient,
            "operand_order": operand_order,
            "method": proof["authority"]["method"],
            "sat_or_smt_used_for_extraction": False,
            "random_falsification_samples": 10_000,
        },
        "weighted_sum": weighted_sum,
        "compressor_spec": spec,
        "emitted_topology_proof": {
            "method": "structural_column_weight_check_plus_shallow_full_adder_miter",
            "result": spec_result,
        },
        "integration": {
            "source_nets": source_nets,
            "target_d_nets": [row["d_net"] for row in graph.targets],
            "target_numerator_positions": target_positions,
            "replaceable_old_cells": old_cells,
            "replaceable_old_cell_count": len(old_cells),
            "retains_existing_output_fdres": True,
            "cycle_timing_change": 0,
            "side_fanout_count": len(graph.side_fanout),
        },
        "implementation_cost": {
            "compressor_lut6_2_count": spec["compressor_count"],
            "inverter_lut_count": spec["inverted_token_count"],
            "final_lut6_2_count": width,
            "carry8_count": (width + 7) // 8,
            "total_lut_upper_bound": (
                spec["compressor_count"]
                + spec["inverted_token_count"]
                + width
            ),
            "compressor_stage_count": spec["stage_count"],
            "existing_cell_count": len(graph.cells),
        },
        "mutation_ready": not graph.side_fanout,
    }
    certificate["certificate_id"] = _digest(certificate)
    return certificate


def render_verilog(certificate: dict[str, Any], *, module_name: str) -> str:
    if certificate.get("status") != "proved" or certificate.get("mutation_ready") is not True:
        raise ValueError("only a proved constructive lowering can be rendered")
    spec = certificate["compressor_spec"]
    source_labels = list(certificate["weighted_sum"]["coefficients"])
    ports = [f"input wire {_safe(label)}" for label in source_labels]
    output_width = len(certificate["integration"]["target_d_nets"])
    ports.append(f"output wire [{output_width - 1}:0] y")
    lines = [
        "// Exact constructive accumulator compressor lowering.",
        f"// certificate_id={certificate['certificate_id']}",
        f"module {module_name} (",
        "  " + ",\n  ".join(ports),
        ");",
    ]
    token_wires = {label: _safe(label) for label in source_labels}
    for ordinal, operation in enumerate(spec["compressors"]):
        name = f"csa_{ordinal}"
        sum_wire = f"{name}_sum"
        carry_wire = f"{name}_carry"
        lines.append(f"  wire {sum_wire};")
        if operation["carry"] is not None:
            lines.append(f"  wire {carry_wire};")
        inputs = [
            _token_expression(token, token_wires) for token in operation["inputs"]
        ]
        inputs.extend(["1'b0"] * (5 - len(inputs)))
        o5 = carry_wire if operation["carry"] is not None else ""
        lines.extend([
            f"  (* DONT_TOUCH = \"yes\" *) LUT6_2 #(.INIT({operation['init']})) {name} (",
            f"    .O6({sum_wire}), .O5({o5}),",
            f"    .I0({inputs[0]}), .I1({inputs[1]}), .I2({inputs[2]}),",
            f"    .I3({inputs[3]}), .I4({inputs[4]}), .I5(1'b1));",
        ])
        token_wires[operation["sum"]] = sum_wire
        if operation["carry"] is not None:
            token_wires[operation["carry"]] = carry_wire

    width = int(spec["width"])
    for position in range(width):
        name = f"final_fa_{position}"
        rows = [
            _token_expression(spec["final_rows"][row][position], token_wires)
            for row in range(3)
        ]
        lines.extend([
            f"  wire final_s_{position};",
            f"  wire final_di_{position};",
            f"  (* DONT_TOUCH = \"yes\" *) LUT6_2 #(.INIT(64'h96969696E8E8E8E8)) {name} (",
            f"    .O6(final_s_{position}), .O5(final_di_{position}),",
            f"    .I0({rows[0]}), .I1({rows[1]}), .I2({rows[2]}),",
            "    .I3(1'b0), .I4(1'b0), .I5(1'b1));",
        ])
    carry_count = (width + 7) // 8
    for index in range(carry_count):
        lines.extend([
            f"  wire [7:0] final_o_{index};",
            f"  wire [7:0] final_co_{index};",
        ])
        s_values = [
            f"final_s_{position}" if position < width else "1'b0"
            for position in range(index * 8, index * 8 + 8)
        ]
        di_values = [
            f"final_di_{position}" if position < width else "1'b0"
            for position in range(index * 8, index * 8 + 8)
        ]
        ci = "1'b0" if index == 0 else f"final_co_{index - 1}[7]"
        lines.extend([
            f"  (* DONT_TOUCH = \"yes\" *) CARRY8 #(.CARRY_TYPE(\"SINGLE_CY8\")) final_carry_{index} (",
            f"    .O(final_o_{index}), .CO(final_co_{index}), .CI({ci}), .CI_TOP(1'b0),",
            "    .S({" + ",".join(reversed(s_values)) + "}),",
            "    .DI({" + ",".join(reversed(di_values)) + "}));",
        ])
    for bit, position in enumerate(certificate["integration"]["target_numerator_positions"]):
        carry, lane = divmod(int(position), 8)
        lines.append(f"  assign y[{bit}] = final_o_{carry}[{lane}];")
    lines.extend(["endmodule", ""])
    return "\n".join(lines)


def render_ooc_tcl(
    *, module_name: str, verilog_path: Path, output_dir: Path, part: str
) -> str:
    return "\n".join([
        f"read_verilog {{{verilog_path}}}",
        f"synth_design -top {module_name} -part {part} -mode out_of_context -flatten_hierarchy none",
        "set_max_delay 2.000 -from [all_inputs] -to [all_outputs]",
        "opt_design -directive ExploreWithRemap",
        "place_design -directive ExtraTimingOpt",
        "phys_opt_design -directive AggressiveExplore",
        "route_design -directive NoTimingRelaxation",
        f"report_timing -delay_type max -max_paths 50 -file {{{output_dir / 'timing.rpt'}}}",
        f"report_timing_summary -delay_type min_max -max_paths 20 -file {{{output_dir / 'timing_summary.rpt'}}}",
        f"report_utilization -file {{{output_dir / 'utilization.rpt'}}}",
        f"report_route_status -file {{{output_dir / 'route_status.rpt'}}}",
        f"write_checkpoint -force {{{output_dir / 'lowering.dcp'}}}",
        "exit",
        "",
    ])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cone_root", type=Path)
    parser.add_argument("extraction", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--module-name", default="constructive_accumulator_v1")
    args = parser.parse_args()
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        raise SystemExit("output directory must be absent or empty")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    certificate = build_constructive_lowering(args.cone_root, args.extraction)
    verilog_path = args.output_dir / "lowering.v"
    verilog = render_verilog(certificate, module_name=args.module_name)
    verilog_path.write_text(verilog, encoding="utf-8")
    (args.output_dir / "certificate.json").write_text(
        json.dumps(certificate, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (args.output_dir / "run_ooc.tcl").write_text(
        render_ooc_tcl(
            module_name=args.module_name,
            verilog_path=verilog_path.resolve(),
            output_dir=args.output_dir.resolve(),
            part=certificate["source_identity"]["part"],
        ),
        encoding="utf-8",
    )
    print(json.dumps({
        "certificate_id": certificate["certificate_id"],
        "module_name": args.module_name,
        "output_dir": str(args.output_dir),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
