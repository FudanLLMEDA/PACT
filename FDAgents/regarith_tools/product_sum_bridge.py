"""Standalone OOC-module DCP bridge for exact registered product sums."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

from ..registered_arithmetic_ir import (
    AccumulationOrder,
    ModularAccumulationGroup,
    PartitionedAccumulationGroup,
    PlanKind,
    QuantizedProductTerm,
)
from .common import canonical_json, fail, object_digest, plan_input


BACKEND_ID = "registered_product_sum_dcp_bridge"
BRIDGE_SCHEMA_VERSION = "regarith-product-sum-bridge-v2"
PLACEMENT_MANIFEST_VERSION = 1
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DSP_SITE = re.compile(r"DSP48E2_X(\d+)Y(\d+)")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _identifier(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_]", "_", value)
    if not token or token[0].isdigit():
        token = "regarith_" + token
    return token


def _literal(width: int, value: int) -> str:
    return f"-{width}'sd{abs(value)}" if value < 0 else f"{width}'sd{value}"


def _order(group: Any) -> str:
    value = group.accumulation_order
    return value.value if isinstance(value, AccumulationOrder) else str(value)


def render_product_sum_bridge_verilog(
    plan_value: Any, *, module_name: str | None = None
) -> dict[str, Any]:
    """Render exact lookback-registered arithmetic feeding preserved output FFs.

    Each emitted term register mirrors the incumbent input FDRE value by
    sampling that FDRE's D net with the same CE and INIT.  Product registers are
    DSP-attributed, allowing Vivado to absorb the shadow state into MREG/PREG.
    The two partition reductions then run in parallel before the final modular
    sum.  This preserves the Q-to-D external schedule while removing the
    multiplier stages from the output register's combinational path.
    """
    plan = plan_input(plan_value)
    if PlanKind(plan.plan_kind) != PlanKind.QUANTIZED_PRODUCT_SUM:
        fail("BRIDGE_PLAN_KIND_UNSUPPORTED", "bridge accepts only product sums")
    if len(plan.accumulation_groups) != 1:
        fail("BRIDGE_GROUP_COUNT_UNSUPPORTED", "bridge requires one final group")
    products = [term for term in plan.data_terms if isinstance(term, QuantizedProductTerm)]
    if len(products) != len(plan.data_terms) or len(products) < 2:
        fail("BRIDGE_TERM_KIND_UNSUPPORTED", "bridge requires only product terms")
    output_id = plan.output_shell.output_word_ids[0]
    output = next(word for word in plan.observable_word_boundaries if word.word_id == output_id)
    words = {word.word_id: word for word in plan.observable_word_boundaries}
    losses = {loss.boundary_id: loss for loss in plan.loss_boundaries}
    terms = {term.term_id: term for term in products}
    name = _identifier(module_name or f"regarith_ps_{plan.plan_digest()[:12]}")
    if _IDENTIFIER.fullmatch(name) is None:
        fail("BRIDGE_MODULE_NAME_INVALID", "module name is not a safe identifier")

    helper_lines: list[str] = []
    dsp_instance_suffixes: list[str] = []

    def add_registered_multiplier(
        helper_name: str, data_width: int, product_width: int, coefficient_width: int,
        coefficient: int,
    ) -> None:
        # Preserve this boundary so synthesis cannot fold reconstruction or
        # partition additions into the registered multiplier's post-MREG ALU.
        # The helper still permits the product register itself to become MREG
        # or PREG inside one DSP48E2.
        helper_lines.extend([
            '(* keep_hierarchy = "yes" *)',
            f"module {helper_name} (",
            "    input wire clk,",
            "    input wire ce,",
            f"    input wire signed [{data_width - 1}:0] data,",
            '    (* use_dsp = "yes", shreg_extract = "no" *)',
            f"    output reg signed [{product_width - 1}:0] q",
            ");",
            f"  initial q = {product_width}'sd0;",
            "  always @(posedge clk) begin",
            f"    if (ce) q <= data * {_literal(coefficient_width, coefficient)};",
            "  end",
            "endmodule",
            "",
        ])

    for index, term in enumerate(products):
        if term.coefficient == 1:
            continue
        if term.data_width > 27:
            low_width = 17
            high_width = term.data_width - low_width
            high_operand_width = high_width + (0 if term.data_signed else 1)
            add_registered_multiplier(
                f"{name}_term_{index}_low_mul",
                low_width + 1,
                low_width + 1 + term.coefficient_width,
                term.coefficient_width,
                term.coefficient,
            )
            add_registered_multiplier(
                f"{name}_term_{index}_high_mul",
                high_operand_width,
                high_operand_width + term.coefficient_width,
                term.coefficient_width,
                term.coefficient,
            )
            dsp_instance_suffixes.extend([
                f"term_{index}_low_mul_i/q_reg",
                f"term_{index}_high_mul_i/q_reg",
            ])
        else:
            data_width = term.data_width if term.data_signed else term.data_width + 1
            add_registered_multiplier(
                f"{name}_term_{index}_mul",
                data_width,
                data_width + term.coefficient_width,
                term.coefficient_width,
                term.coefficient,
            )
            dsp_instance_suffixes.append(f"term_{index}_mul_i/q_reg")

    lines = [
        "// Generated from an exact registered product-sum contract.",
        f"// plan_digest={plan.plan_digest()}",
        *helper_lines,
        f"module {name} (",
        "    input wire clk,",
    ]
    for index, _term in enumerate(products):
        lines.append(f"    input wire ce_input_{index},")
    for index, term in enumerate(products):
        signed = " signed" if term.data_signed else ""
        lines.append(
            f"    input wire{signed} [{term.data_width - 1}:0] input_{index},"
        )
    lines.extend([f"    output wire [{output.width - 1}:0] y", ");", ""])
    term_index = {term.term_id: index for index, term in enumerate(products)}
    group = plan.accumulation_groups[0]
    order = _order(group)

    term_sources: dict[str, tuple[str, int, bool]] = {}
    for index, term in enumerate(products):
        if term.coefficient == 1:
            shadow = f"term_{index}_shadow_q"
            signed = " signed" if term.data_signed else ""
            lines.extend([
                f"  (* shreg_extract = \"no\" *) reg{signed} "
                f"[{term.data_width - 1}:0] {shadow} = {term.data_width}'d0;",
                "  always @(posedge clk) begin",
                f"    if (ce_input_{index}) {shadow} <= input_{index};",
                "  end",
                "",
            ])
            term_sources[term.term_id] = (shadow, term.data_width, term.data_signed)
            continue
        if term.data_width > 27:
            # Split at the native signed wide-multiply boundary.  Both partial
            # products register in parallel DSPs; reconstruction occurs after
            # the registers, so no unregistered DSP-to-DSP cascade remains.
            low_width = 17
            high_width = term.data_width - low_width
            low_data = f"term_{index}_low_data"
            high_data = f"term_{index}_high_data"
            low_product_width = low_width + 1 + term.coefficient_width
            high_operand_width = high_width + (0 if term.data_signed else 1)
            high_product_width = high_operand_width + term.coefficient_width
            low_q = f"term_{index}_low_product_q"
            high_q = f"term_{index}_high_product_q"
            product_width = term.data_width + term.coefficient_width
            combined = f"term_{index}_product_q"
            lines.append(
                f"  wire signed [{low_width}:0] {low_data} = "
                f"{{1'b0, input_{index}[{low_width - 1}:0]}};"
            )
            if term.data_signed:
                lines.append(
                    f"  wire signed [{high_width - 1}:0] {high_data} = "
                    f"input_{index}[{term.data_width - 1}:{low_width}];"
                )
            else:
                lines.append(
                    f"  wire signed [{high_width}:0] {high_data} = "
                    f"{{1'b0, input_{index}[{term.data_width - 1}:{low_width}]}};"
                )
            lines.extend([
                f"  wire signed [{low_product_width - 1}:0] {low_q};",
                f"  wire signed [{high_product_width - 1}:0] {high_q};",
                f"  (* keep_hierarchy = \"yes\" *) {name}_term_{index}_low_mul "
                f"term_{index}_low_mul_i (.clk(clk), .ce(ce_input_{index}), "
                f".data({low_data}), .q({low_q}));",
                f"  (* keep_hierarchy = \"yes\" *) {name}_term_{index}_high_mul "
                f"term_{index}_high_mul_i (.clk(clk), .ce(ce_input_{index}), "
                f".data({high_data}), .q({high_q}));",
                f"  wire signed [{product_width - 1}:0] {combined}_low = {low_q};",
                f"  wire signed [{product_width - 1}:0] {combined}_high = {high_q};",
                f"  wire signed [{product_width - 1}:0] {combined} = "
                f"{combined}_low + ({combined}_high <<< {low_width});",
                "",
            ])
            term_sources[term.term_id] = (combined, product_width, True)
        else:
            data = f"term_{index}_multiply_data"
            if term.data_signed:
                data_width = term.data_width
                lines.append(
                    f"  wire signed [{data_width - 1}:0] {data} = input_{index};"
                )
            else:
                data_width = term.data_width + 1
                lines.append(
                    f"  wire signed [{data_width - 1}:0] {data} = "
                    f"{{1'b0, input_{index}}};"
                )
            product_width = data_width + term.coefficient_width
            shadow = f"term_{index}_product_q"
            lines.extend([
                f"  wire signed [{product_width - 1}:0] {shadow};",
                f"  (* keep_hierarchy = \"yes\" *) {name}_term_{index}_mul "
                f"term_{index}_mul_i (.clk(clk), .ce(ce_input_{index}), "
                f".data({data}), .q({shadow}));",
                "",
            ])
            term_sources[term.term_id] = (shadow, product_width, True)

    if isinstance(group, PartitionedAccumulationGroup):
        if order != AccumulationOrder.PARTITIONED_FULL_PRECISION_THEN_SHIFT.value:
            fail("BRIDGE_ORDER_MISMATCH", "typed partition order is inconsistent")
        partition_values = []
        for partition_index, partition in enumerate(group.partitions):
            rows = []
            for local_index, term_id in enumerate(partition.term_ids):
                term = terms[term_id]
                width = partition.accumulator_width
                source, source_width, source_signed = term_sources[term_id]
                extension = width - source_width
                fill = f"{source}[{source_width - 1}]" if source_signed else "1'b0"
                data = f"partition_{partition_index}_term_{local_index}_data"
                if extension:
                    lines.append(
                        f"  wire signed [{width - 1}:0] {data} = "
                        f"{{{{{extension}{{{fill}}}}}, {source}}};"
                    )
                else:
                    lines.append(
                        f"  wire signed [{width - 1}:0] {data} = {source};"
                    )
                rows.append(data)
            total = f"partition_{partition_index}_total"
            sliced = f"partition_{partition_index}_sliced"
            value = f"partition_{partition_index}_value"
            loss = losses[partition.loss_boundary_id]
            lines.append(
                f"  wire signed [{partition.accumulator_width - 1}:0] {total} = "
                + " + ".join(rows) + ";"
            )
            lines.append(
                f"  wire [{partition.result_width - 1}:0] {sliced} = "
                f"{total}[{loss.slice_lsb + partition.result_width - 1}:{loss.slice_lsb}];"
            )
            extension = group.accumulator_width - partition.result_width
            if extension:
                fill = f"{sliced}[{partition.result_width - 1}]" if partition.output_extension == "sign_extend" else "1'b0"
                lines.append(
                    f"  wire [{group.accumulator_width - 1}:0] {value} = "
                    f"{{{{{extension}{{{fill}}}}}, {sliced}}};"
                )
            else:
                lines.append(
                    f"  wire [{group.accumulator_width - 1}:0] {value} = {sliced};"
                )
            partition_values.append(value)
        lines.append(
            f"  wire [{group.accumulator_width - 1}:0] modular_sum = "
            + " + ".join(partition_values) + ";"
        )
    elif isinstance(group, ModularAccumulationGroup) and order == "after_term_loss":
        values = []
        for index, term in enumerate(products):
            loss = losses[term.loss_boundary_id]
            value = f"term_{index}"
            source, _source_width, _source_signed = term_sources[term.term_id]
            lines.append(
                f"  wire [{group.accumulator_width - 1}:0] {value} = "
                f"{source}[{loss.slice_lsb + group.accumulator_width - 1}:{loss.slice_lsb}];"
            )
            values.append(value)
        lines.append(
            f"  wire [{group.accumulator_width - 1}:0] modular_sum = "
            + " + ".join(values) + ";"
        )
    else:
        fail("BRIDGE_ACCUMULATION_ORDER_UNSUPPORTED", f"unsupported order {order}")
    lines.extend([f"  assign y = modular_sum[{output.width - 1}:0];", "endmodule", ""])
    verilog = "\n".join(lines)
    return {
        "schema_version": BRIDGE_SCHEMA_VERSION,
        "backend_id": BACKEND_ID,
        "module_name": name,
        "plan_digest": plan.plan_digest(),
        "proof_template_id": plan.register_stage_schedule.stages[0].operation,
        "verilog": verilog,
        "verilog_sha256": hashlib.sha256(verilog.encode("ascii")).hexdigest(),
        "expected_product_count": len(products),
        "expected_dsp48e2_count": sum(
            (term.data_width + 26) // 27
            for term in products
            if abs(term.coefficient) not in {1}
            and (abs(term.coefficient) & (abs(term.coefficient) - 1)) != 0
        ),
        "expected_partition_count": (
            len(group.partitions) if isinstance(group, PartitionedAccumulationGroup) else len(products)
        ),
        "expected_shadow_register_count": len(products),
        "dsp_instance_suffixes": dsp_instance_suffixes,
        "lookback_pipeline_enabled": True,
        "preserves_output_registers": True,
        "mutation_allowed": False,
    }


def prepare_product_sum_bridge(
    plan_value: Any, facts: Mapping[str, Any], *, module_name: str | None = None
) -> dict[str, Any]:
    """Bind rendered RTL to exact source/output endpoints and old-cone inventory."""
    plan = plan_input(plan_value)
    rendered = render_product_sum_bridge_verilog(plan, module_name=module_name)
    if not isinstance(facts, Mapping) or facts.get("candidate_id") != plan.candidate_family_id:
        fail("BRIDGE_FACTS_PLAN_MISMATCH", "facts do not identify the validated plan")
    boundary = facts.get("boundary")
    if not isinstance(boundary, Mapping) or boundary.get("closure_complete") is not True:
        fail("BRIDGE_CLOSURE_INCOMPLETE", "bridge requires exact closed cone facts")
    if boundary.get("side_consumers") != []:
        fail("BRIDGE_SIDE_CONSUMERS_UNSUPPORTED", "retained side consumers are nonempty")
    raw_inputs = boundary.get("input_buses")
    raw_outputs = boundary.get("output_bus")
    raw_cone = boundary.get("cone_cells")
    if not all(isinstance(value, list) for value in (raw_inputs, raw_outputs, raw_cone)):
        fail("BRIDGE_BOUNDARY_INCOMPLETE", "bridge endpoint inventories are absent")
    inputs = []
    input_q_endpoints = []
    input_controls = []
    for index, row in enumerate(raw_inputs):
        q_endpoints = row.get("endpoints") if isinstance(row, Mapping) else None
        if not isinstance(q_endpoints, list):
            fail("BRIDGE_LOOKBACK_BOUNDARY_INCOMPLETE", f"input {index} Q endpoints are absent")
        dynamic_cells = [
            str(endpoint["cell"])
            for endpoint in q_endpoints
            if isinstance(endpoint, Mapping) and endpoint != {"const": 0}
        ]
        if not dynamic_cells:
            fail("BRIDGE_LOOKBACK_BOUNDARY_INCOMPLETE", f"input {index} has no registered bits")
        endpoints = [
            {"const": 0}
            if endpoint == {"const": 0}
            else {"pin": f"{endpoint['cell']}/D"}
            for endpoint in q_endpoints
        ]
        inputs.append([
            {"const": 0}
            if endpoint == {"const": 0}
            else {"pin": str(endpoint["pin"])}
            for endpoint in endpoints
        ])
        input_q_endpoints.append([
            {"const": 0}
            if endpoint == {"const": 0}
            else {"pin": str(endpoint["pin"])}
            for endpoint in q_endpoints
        ])
        input_controls.append({
            "clock_pin": f"{dynamic_cells[0]}/C",
            "ce_pin": f"{dynamic_cells[0]}/CE",
            "reset_pin": f"{dynamic_cells[0]}/R",
            "register_cells": dynamic_cells,
            "init": 0,
            "reset_kind": "none",
        })
    if len(inputs) != rendered["expected_product_count"]:
        fail("BRIDGE_LOOKBACK_TERM_COUNT_MISMATCH", "lookback inputs do not match product terms")
    outputs = [str(row["pin"]) for row in raw_outputs]
    old_cone = [{"cell": str(row["cell"]), "type": str(row["type"])} for row in raw_cone]
    manifest = {
        **{key: value for key, value in rendered.items() if key != "verilog"},
        "source_artifact_sha256": plan.source_artifact_sha256,
        "region_sha256": str(facts.get("region_sha256") or ""),
        "candidate_id": plan.candidate_family_id,
        "instance_name": _identifier(rendered["module_name"] + "_i"),
        "input_endpoints": inputs,
        "registered_input_q_endpoints": input_q_endpoints,
        "input_controls": input_controls,
        "clock_source_pin": str(boundary.get("clock_pin") or ""),
        "lookback_semantics": "shadow_register_equals_incumbent_input_fdre_q",
        "output_pins": outputs,
        "old_cone": old_cone,
        "old_cone_count": len(old_cone),
        "side_consumer_count": 0,
    }
    manifest["manifest_digest"] = object_digest(manifest)
    return {"rendered": rendered, "manifest": manifest}


def allocate_product_sum_dsp_placement(
    bridge_manifest: Mapping[str, Any], oracle_slots: list[dict[str, Any]],
) -> dict[str, Any]:
    """Select two deterministic adjacent DSP pairs from measured free slots."""
    raw = dict(bridge_manifest)
    claimed = raw.pop("manifest_digest", None)
    if not isinstance(claimed, str) or object_digest(raw) != claimed:
        fail("BRIDGE_PLACEMENT_MANIFEST_INVALID", "bridge manifest digest mismatch")
    source_sha = str(bridge_manifest.get("source_artifact_sha256") or "")
    region_sha = str(bridge_manifest.get("region_sha256") or "")
    if _SHA256.fullmatch(source_sha) is None or _SHA256.fullmatch(region_sha) is None:
        fail("BRIDGE_PLACEMENT_SOURCE_INVALID", "bridge source identity is incomplete")
    suffixes = bridge_manifest.get("dsp_instance_suffixes")
    if not isinstance(suffixes, list) or len(suffixes) != 4 or len(set(suffixes)) != 4:
        fail("BRIDGE_PLACEMENT_TOPOLOGY_INVALID", "four unique DSP instances are required")

    expected_fields = {
        "site", "bel", "source_dcp_sha256", "region_sha256",
        "clock_route_ok", "distance",
    }
    slots: list[dict[str, Any]] = []
    for index, slot in enumerate(oracle_slots):
        if not isinstance(slot, dict) or set(slot) != expected_fields:
            fail("BRIDGE_PLACEMENT_ORACLE_INVALID", f"oracle slot {index} schema mismatch")
        match = _DSP_SITE.fullmatch(str(slot.get("site") or ""))
        distance = slot.get("distance")
        if (
            match is None or slot.get("bel") != "DSP48E2"
            or slot.get("source_dcp_sha256") != source_sha
            or slot.get("region_sha256") != region_sha
            or slot.get("clock_route_ok") is not True
            or isinstance(distance, bool) or not isinstance(distance, int) or distance < 0
        ):
            fail("BRIDGE_PLACEMENT_ORACLE_INVALID", f"oracle slot {index} is invalid or stale")
        slots.append(dict(slot))
    if len({slot["site"] for slot in slots}) != len(slots):
        fail("BRIDGE_PLACEMENT_ORACLE_INVALID", "oracle slots contain duplicate sites")

    by_xy = {}
    for slot in slots:
        match = _DSP_SITE.fullmatch(slot["site"])
        by_xy[(int(match.group(1)), int(match.group(2)))] = slot
    pairs = []
    for (x_coord, y_coord), lower in by_xy.items():
        upper = by_xy.get((x_coord, y_coord + 1))
        if upper is not None:
            pairs.append((lower, upper))
    choices = []
    for left_index, left in enumerate(pairs):
        for right in pairs[left_index + 1:]:
            sites = {left[0]["site"], left[1]["site"], right[0]["site"], right[1]["site"]}
            if len(sites) == 4:
                ordered = sorted((left, right), key=lambda pair: pair[0]["site"])
                choices.append(ordered)
    if not choices:
        fail("BRIDGE_PLACEMENT_ORACLE_INSUFFICIENT", "two disjoint adjacent DSP pairs are required")
    selected = min(
        choices,
        key=lambda choice: (
            sum(slot["distance"] for pair in choice for slot in pair),
            tuple(slot["site"] for pair in choice for slot in pair),
        ),
    )
    selected_slots = [slot for pair in selected for slot in pair]
    placement = {
        "kind": "product_sum_placement_manifest",
        "version": PLACEMENT_MANIFEST_VERSION,
        "source": {"dcp_sha256": source_sha, "region_sha256": region_sha},
        "candidate_id": bridge_manifest["candidate_id"],
        "plan_digest": bridge_manifest["plan_digest"],
        "bridge_manifest_digest": claimed,
        "placements": [
            {"cell_suffix": suffix, "site": slot["site"], "bel": "DSP48E2"}
            for suffix, slot in zip(suffixes, selected_slots, strict=True)
        ],
        "authority": "pre_insertion_only",
    }
    placement["manifest_digest"] = object_digest(placement)
    return placement


def render_bridge_placement_tcl(
    bridge_manifest: Mapping[str, Any], placement: Mapping[str, Any],
) -> str:
    """Render fail-closed placement commands for the selected DSP instances."""
    payload = dict(placement)
    claimed = payload.pop("manifest_digest", None)
    if (
        placement.get("kind") != "product_sum_placement_manifest"
        or placement.get("version") != PLACEMENT_MANIFEST_VERSION
        or placement.get("bridge_manifest_digest") != bridge_manifest.get("manifest_digest")
        or not isinstance(claimed, str) or object_digest(payload) != claimed
    ):
        fail("BRIDGE_PLACEMENT_MANIFEST_INVALID", "placement manifest is stale or modified")
    instance = str(bridge_manifest["instance_name"])
    lines = []
    for index, row in enumerate(placement.get("placements") or []):
        cell = f"{instance}/{row['cell_suffix']}"
        site = str(row["site"])
        lines.extend([
            f"set rb_dsp_{index} [rb_one [get_cells -quiet {_brace(cell)}] {_brace(cell)}]",
            f"set rb_site_{index} [rb_one [get_sites -quiet {_brace(site)}] {_brace(site)}]",
            f"if {{[llength [get_cells -quiet -of_objects $rb_site_{index}]] != 0}} {{error {{{site} is occupied}}}}",
            f"place_cell $rb_dsp_{index} $rb_site_{index}",
            f"if {{[get_property LOC $rb_dsp_{index}] ne {{{site}}}}} {{error {{DSP placement {index} failed}}}}",
        ])
    if len(lines) != 20:
        fail("BRIDGE_PLACEMENT_TOPOLOGY_INVALID", "placement must contain four DSP rows")
    return "\n".join(lines) + "\n"


def render_bridge_synth_tcl(manifest: Mapping[str, Any], rtl_path: str, out_dir: str) -> str:
    module = str(manifest["module_name"])
    return "\n".join([
        f"read_verilog {{{rtl_path}}}",
        f"synth_design -top {module} -part xcvu3p-ffvc1517-2-e -mode out_of_context -flatten_hierarchy rebuilt -retiming",
        "create_clock -period 2.000 -name bridge_clock [get_ports clk]",
        "set_input_delay 0.000 -clock bridge_clock [get_ports -quiet {input_* ce_input_*}]",
        "set_max_delay 2.000 -from [all_registers] -to [get_ports y]",
        "opt_design -directive ExploreWithRemap",
        f"report_timing -delay_type max -max_paths 50 -file {{{out_dir}/bridge_post_synth_timing.rpt}}",
        f"report_timing -delay_type max -from [get_ports -quiet input_*] -to [all_registers] -max_paths 50 -file {{{out_dir}/bridge_input_to_register_timing.rpt}}",
        f"report_timing -delay_type max -from [all_registers] -to [get_ports y] -max_paths 50 -file {{{out_dir}/bridge_register_to_output_timing.rpt}}",
        f"report_utilization -file {{{out_dir}/bridge_utilization.rpt}}",
        f"report_timing_summary -delay_type min_max -max_paths 20 -file {{{out_dir}/bridge_timing_summary.rpt}}",
        f"write_checkpoint -force {{{out_dir}/bridge.dcp}}",
        f"write_edif -force {{{out_dir}/bridge.edf}}",
        "exit", "",
    ])


def render_bridge_insert_python(manifest: Mapping[str, Any]) -> str:
    instance = str(manifest["instance_name"])
    return "\n".join([
        "import sys, jpype",
        "from RapidWrightMCP import rapidwright_tools",
        "rapidwright_tools.initialize_rapidwright(jvm_max_memory='12G')",
        "Design = jpype.JClass('com.xilinx.rapidwright.design.Design')",
        "Module = jpype.JClass('com.xilinx.rapidwright.design.Module')",
        "design = Design.readCheckpoint(sys.argv[1])",
        "module = Module(Design.readCheckpoint(sys.argv[2]), False)",
        f"instance = design.createModuleInst({instance!r}, module)",
        "if instance is None: raise RuntimeError('failed to insert product-sum bridge')",
        "instance.unplace()",
        "design.getNetlist().resetParentNetMap()",
        "design.writeCheckpoint(sys.argv[3])",
        "print('REGARITH_PRODUCT_SUM_INSERTED=1')", "",
    ])


def _brace(value: str) -> str:
    return "{" + value.replace("\\", "\\\\").replace("}", "\\}") + "}"


def render_bridge_connect_tcl(
    manifest: Mapping[str, Any], inserted_dcp: str, output_dcp: str, *,
    defer_input_connections: bool = False,
) -> str:
    """Connect exact endpoints, replace output drivers, and sweep the old cone."""
    name = str(manifest["instance_name"])
    lines = [
        f"open_checkpoint {{{inserted_dcp}}}",
        "proc rb_one {objects label} {if {[llength $objects] != 1} {error \"$label expected one object, got [llength $objects]\"}; return [lindex $objects 0]}",
        "proc rb_same_net {left right label} {set a [get_nets -quiet -segments -of_objects $left]; set b [get_nets -quiet -segments -of_objects $right]; foreach n $a {if {[lsearch -exact $b $n] >= 0} {return}}; error \"$label net mismatch\"}",
        "set rb_gnd [create_cell -reference GND regarith_ps_bridge_gnd]",
        "set rb_gnd_net [create_net regarith_ps_bridge_gnd_net]",
        "connect_net -net $rb_gnd_net -objects [get_pins $rb_gnd/G]",
    ]
    clock_source = str(manifest["clock_source_pin"])
    lines.extend([
        f"set rb_source [rb_one [get_pins -quiet {_brace(clock_source)}] {_brace(clock_source)}]",
        "set rb_net [rb_one [get_nets -quiet -of_objects $rb_source] {bridge clock net}]",
        f"set rb_sink [rb_one [get_pins -quiet {_brace(name + '/clk')}] {_brace(name + '/clk')}]",
        "connect_net -hierarchical -net $rb_net -objects $rb_sink",
    ])
    for input_index, control in enumerate(manifest["input_controls"]):
        ce_source = str(control["ce_pin"])
        ce_sink = f"{name}/ce_input_{input_index}"
        register_cells = " ".join(_brace(str(cell)) for cell in control["register_cells"])
        representative = str(control["register_cells"][0])
        lines.extend([
            f"set rb_reference_clock [rb_one [get_pins -quiet {_brace(representative + '/C')}] {{input {input_index} reference clock}}]",
            f"set rb_reference_ce [rb_one [get_pins -quiet {_brace(representative + '/CE')}] {{input {input_index} reference CE}}]",
            f"set rb_reference_reset [rb_one [get_pins -quiet {_brace(representative + '/R')}] {{input {input_index} reference reset}}]",
            f"foreach rb_cell_name [list {register_cells}] {{",
            "  set rb_cell [rb_one [get_cells -quiet [list $rb_cell_name]] {lookback register}]",
            "  if {[get_property REF_NAME $rb_cell] ne {FDRE} || [get_property INIT $rb_cell] ne {1'b0}} {error {lookback register primitive/INIT mismatch}}",
            "  rb_same_net [rb_one [get_pins -quiet $rb_cell_name/C] {lookback clock}] $rb_reference_clock {lookback clock}",
            "  rb_same_net [rb_one [get_pins -quiet $rb_cell_name/CE] {lookback CE}] $rb_reference_ce {lookback CE}",
            "  rb_same_net [rb_one [get_pins -quiet $rb_cell_name/R] {lookback reset}] $rb_reference_reset {lookback reset}",
            "}",
            "set rb_reset_drivers [get_pins -quiet -leaf -of_objects [get_nets -quiet -segments -of_objects $rb_reference_reset] -filter {DIRECTION == OUT}]",
            "if {[llength $rb_reset_drivers] != 1 || [get_property REF_NAME [get_cells -of_objects $rb_reset_drivers]] ne {GND}} {error {lookback reset is not constant zero}}",
            f"set rb_source [rb_one [get_pins -quiet {_brace(ce_source)}] {_brace(ce_source)}]",
            "set rb_net [rb_one [get_nets -quiet -of_objects $rb_source] {bridge CE net}]",
            f"set rb_sink [rb_one [get_pins -quiet {_brace(ce_sink)}] {_brace(ce_sink)}]",
            "connect_net -hierarchical -net $rb_net -objects $rb_sink",
        ])
    if not defer_input_connections:
        lines.extend(_render_bridge_input_connections(manifest))
    for bit, output_pin in enumerate(manifest["output_pins"]):
        source = f"{name}/y[{bit}]"
        lines.extend([
            f"set rb_sink [rb_one [get_pins -quiet {_brace(str(output_pin))}] {_brace(str(output_pin))}]",
            "set rb_net [rb_one [get_nets -quiet -of_objects $rb_sink] {output D net}]",
            "set rb_drivers [get_pins -quiet -leaf -of_objects [get_nets -segments $rb_net] -filter {DIRECTION == OUT}]",
            "if {[llength $rb_drivers] != 1} {error \"output old driver is not unique\"}",
            "route_design -unroute -nets $rb_net",
            "disconnect_net -net $rb_net -objects $rb_drivers",
            f"set rb_source [rb_one [get_pins -quiet {_brace(source)}] {_brace(source)}]",
            "connect_net -hierarchical -net $rb_net -objects $rb_source",
        ])
    old_cells = " ".join(_brace(str(row["cell"])) for row in manifest["old_cone"])
    lines.extend([
        f"set rb_old_cells [get_cells -quiet [list {old_cells}]]",
        f"if {{[llength $rb_old_cells] != {manifest['old_cone_count']}}} {{error \"old cone identity/count mismatch\"}}",
        "remove_cell $rb_old_cells",
        f"if {{[llength [get_cells -quiet [list {old_cells}]]] != 0}} {{error \"old cone sweep failed\"}}",
        f"write_checkpoint -force {{{output_dcp}}}",
        "puts REGARITH_PRODUCT_SUM_WIRING_DONE", "close_design", "exit", "",
    ])
    return "\n".join(lines)


def _render_bridge_input_connections(manifest: Mapping[str, Any]) -> list[str]:
    name = str(manifest["instance_name"])
    lines = []
    for input_index, endpoints in enumerate(manifest["input_endpoints"]):
        for bit, endpoint in enumerate(endpoints):
            sink = f"{name}/input_{input_index}[{bit}]"
            if endpoint == {"const": 0}:
                lines.append(
                    f"connect_net -hierarchical -net $rb_gnd_net -objects [rb_one [get_pins -quiet {_brace(sink)}] {_brace(sink)}]"
                )
            else:
                source = str(endpoint["pin"])
                lines.extend([
                    f"set rb_source [rb_one [get_pins -quiet {_brace(source)}] {_brace(source)}]",
                    f"set rb_net [rb_one [get_nets -quiet -of_objects $rb_source] {_brace(source + ' net')}]",
                    f"set rb_sink [rb_one [get_pins -quiet {_brace(sink)}] {_brace(sink)}]",
                    "connect_net -hierarchical -net $rb_net -objects $rb_sink",
                ])
    return lines


def render_bridge_deferred_input_tcl(manifest: Mapping[str, Any]) -> str:
    """Connect lookback D taps after placement to avoid rebuilding parent carry shapes."""
    lines = [
        "set rb_gnd_nets [get_nets -quiet regarith_ps_bridge_gnd_net]",
        "if {[llength $rb_gnd_nets] == 0} {",
        "  set rb_gnd [create_cell -reference GND regarith_ps_bridge_gnd]",
        "  set rb_gnd_net [create_net regarith_ps_bridge_gnd_net]",
        "  connect_net -net $rb_gnd_net -objects [get_pins $rb_gnd/G]",
        "} else {set rb_gnd_net [rb_one $rb_gnd_nets {bridge ground net}]}",
        *_render_bridge_input_connections(manifest),
        "puts REGARITH_PRODUCT_SUM_DEFERRED_INPUTS_DONE",
        "",
    ]
    return "\n".join(lines)


__all__ = [
    "BACKEND_ID", "BRIDGE_SCHEMA_VERSION", "PLACEMENT_MANIFEST_VERSION",
    "prepare_product_sum_bridge", "allocate_product_sum_dsp_placement",
    "render_product_sum_bridge_verilog", "render_bridge_synth_tcl",
    "render_bridge_insert_python", "render_bridge_connect_tcl",
    "render_bridge_deferred_input_tcl", "render_bridge_placement_tcl",
]
