"""Generate schedule-preserving registered fixed-point operator bridges.

The plan is identity-free at the implementation layer: cell/net names live in
the current-DCP wiring manifest, while this module only consumes arithmetic and
schedule facts recovered from that DCP.  It deliberately emits no Tcl and does
not choose an action.
"""

from __future__ import annotations

import json
import re
from typing import Any


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _signed_literal(width: int, value: int) -> str:
    return f"-{width}'sd{abs(value)}" if value < 0 else f"{width}'sd{value}"


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def validate_registered_bridge_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Validate one recovered arithmetic/schedule template for RTL emission."""
    if not isinstance(plan, dict) or plan.get("version") != 1:
        raise ValueError("registered bridge plan version must be 1")
    module_name = str(plan.get("module_name") or "")
    if _IDENTIFIER.fullmatch(module_name) is None:
        raise ValueError("registered bridge module_name is not a Verilog identifier")
    width = _integer(plan.get("data_width"), "data_width")
    if not 2 <= width <= 64:
        raise ValueError("data_width must be in 2..64")
    raw_terms = plan.get("terms")
    coefficients = plan.get("coefficients")
    if raw_terms is None:
        coefficient_width = _integer(
            plan.get("coefficient_width"), "coefficient_width"
        )
        product_shift = _integer(plan.get("product_shift"), "product_shift")
        if not 2 <= coefficient_width <= 27:
            raise ValueError("coefficient_width must be in 2..27")
        if not 0 <= product_shift < width + coefficient_width:
            raise ValueError("product_shift lies outside the recovered product")
        if not isinstance(coefficients, list) or not 2 <= len(coefficients) <= 8:
            raise ValueError("coefficients must contain 2..8 recovered terms")
        legacy_term_count = len(coefficients)
    else:
        if not isinstance(raw_terms, list) or not 1 <= len(raw_terms) <= 8:
            raise ValueError("terms must contain 1..8 recovered terms")
        legacy_term_count = len(raw_terms)
    input_roles = plan.get("input_roles")
    if input_roles is None:
        input_roles = [f"input_{ordinal}" for ordinal in range(legacy_term_count)]
    if (
        not isinstance(input_roles, list)
        or not 1 <= len(input_roles) <= 8
        or any(
            not isinstance(role, str) or _IDENTIFIER.fullmatch(role) is None
            for role in input_roles
        )
        or len(set(input_roles)) != len(input_roles)
    ):
        raise ValueError("input_roles must be unique Verilog identifiers")
    input_widths = plan.get("input_widths")
    if input_widths is None:
        input_widths = [width] * len(input_roles)
    if (
        not isinstance(input_widths, list)
        or len(input_widths) != len(input_roles)
        or any(
            isinstance(value, bool) or not isinstance(value, int)
            or not 2 <= value <= 64
            for value in input_widths
        )
    ):
        raise ValueError("input_widths must match the recovered input roles")
    input_signed = plan.get("input_signed")
    if input_signed is None:
        input_signed = [True] * len(input_roles)
    if (
        not isinstance(input_signed, list)
        or len(input_signed) != len(input_roles)
        or any(not isinstance(value, bool) for value in input_signed)
    ):
        raise ValueError("input_signed must match the recovered input roles")
    input_contract = {
        role: (int(input_width), bool(signed))
        for role, input_width, signed in zip(
            input_roles, input_widths, input_signed
        )
    }
    if raw_terms is None:
        raw_terms = [
            {
                "kind": "fixed_product_slice",
                "source_role": role,
                "data_width": input_width,
                "data_signed": signed,
                "coefficient": coefficient,
                "coefficient_width": coefficient_width,
                "shift": product_shift,
                "output_width": width,
            }
            for role, input_width, signed, coefficient in zip(
                input_roles, input_widths, input_signed, coefficients
            )
        ]
    checked_terms = []
    for ordinal, raw in enumerate(raw_terms):
        if not isinstance(raw, dict) or raw.get("kind") not in {
            "fixed_product_slice", "passthrough",
        }:
            raise ValueError(f"terms[{ordinal}] has an unsupported kind")
        role = str(raw.get("source_role") or "")
        source_contract = input_contract.get(role)
        data_width = _integer(raw.get("data_width"), f"terms[{ordinal}].data_width")
        data_signed = raw.get("data_signed")
        shift = _integer(raw.get("shift"), f"terms[{ordinal}].shift")
        output_width = _integer(
            raw.get("output_width"), f"terms[{ordinal}].output_width"
        )
        if (
            source_contract is None
            or not isinstance(data_signed, bool)
            or source_contract != (data_width, data_signed)
            or output_width != width
            or shift < 0
        ):
            raise ValueError("recovered bridge term does not match its input role")
        checked_term = {
            "kind": str(raw["kind"]),
            "source_role": role,
            "data_width": data_width,
            "data_signed": bool(data_signed),
            "shift": shift,
            "output_width": output_width,
        }
        if raw["kind"] == "fixed_product_slice":
            term_coefficient = _integer(
                raw.get("coefficient"), f"terms[{ordinal}].coefficient"
            )
            term_coefficient_width = _integer(
                raw.get("coefficient_width"),
                f"terms[{ordinal}].coefficient_width",
            )
            if (
                not 2 <= term_coefficient_width <= 27
                or not -(1 << (term_coefficient_width - 1))
                <= term_coefficient
                <= (1 << (term_coefficient_width - 1)) - 1
                or term_coefficient == 0
                or shift >= data_width + term_coefficient_width
            ):
                raise ValueError("fixed-product bridge term is outside the DSP contract")
            checked_term.update({
                "coefficient": term_coefficient,
                "coefficient_width": term_coefficient_width,
            })
        elif shift > data_width:
            raise ValueError("passthrough bridge shift exceeds its source width")
        checked_terms.append(checked_term)
    if set(term["source_role"] for term in checked_terms) != set(input_roles):
        raise ValueError("every recovered bridge input must feed at least one term")
    product_terms = [
        term for term in checked_terms
        if term["kind"] == "fixed_product_slice"
    ]
    raw_groups = plan.get("groups")
    if raw_groups is None:
        raw_groups = [
            {
                "term_ordinals": [ordinal],
                "common_shift": int(term["shift"]),
                "output_width": width,
            }
            for ordinal, term in enumerate(checked_terms)
        ]
    if not isinstance(raw_groups, list) or not raw_groups:
        raise ValueError("registered bridge quantization groups are absent")
    checked_groups = []
    covered = []
    for group_ordinal, raw_group in enumerate(raw_groups):
        if not isinstance(raw_group, dict):
            raise ValueError("registered bridge quantization group is invalid")
        ordinals = raw_group.get("term_ordinals")
        common_shift = _integer(
            raw_group.get("common_shift"),
            f"groups[{group_ordinal}].common_shift",
        )
        group_output_width = _integer(
            raw_group.get("output_width"),
            f"groups[{group_ordinal}].output_width",
        )
        if (
            not isinstance(ordinals, list)
            or not ordinals
            or len(ordinals) != len(set(ordinals))
            or any(
                isinstance(value, bool) or not isinstance(value, int)
                or not 0 <= value < len(checked_terms)
                for value in ordinals
            )
            or group_output_width != width
            or common_shift < max(
                checked_terms[value]["shift"] for value in ordinals
            )
        ):
            raise ValueError("registered bridge quantization group is invalid")
        covered.extend(ordinals)
        checked_groups.append({
            "term_ordinals": list(ordinals),
            "common_shift": common_shift,
            "output_width": group_output_width,
        })
    if sorted(covered) != list(range(len(checked_terms))):
        raise ValueError("registered bridge groups do not partition the terms")
    checked = [int(term["coefficient"]) for term in product_terms]
    coefficient_width = max(
        (int(term["coefficient_width"]) for term in product_terms),
        default=0,
    )
    product_shift = max(
        (int(term["shift"]) for term in product_terms),
        default=0,
    )
    integration = str(
        plan.get("output_integration") or "replace_downstream_shell_v1"
    )
    if integration not in {
        "replace_downstream_shell_v1",
        "replace_observable_shell_truth_table_v1",
        "preserve_target_register_v1",
    }:
        raise ValueError("registered bridge output integration is unsupported")
    result = {
        "version": 1,
        "module_name": module_name,
        "data_width": width,
        "coefficient_width": coefficient_width,
        "product_shift": product_shift,
        "coefficients": checked,
        "input_roles": list(input_roles),
        "input_widths": list(input_widths),
        "input_signed": list(input_signed),
        "terms": checked_terms,
        "groups": checked_groups,
        "output_integration": integration,
    }
    raw_controls = plan.get("controls")
    if raw_controls:
        if (
            not isinstance(raw_controls, dict)
            or raw_controls.get("kind")
            != "exhaustive_registered_predicate_protocol_v1"
        ):
            raise ValueError("registered bridge control protocol is invalid")
        predicate_roles = raw_controls.get("predicate_roles")
        active_table = raw_controls.get("arithmetic_active_truth_table")
        phase_count = raw_controls.get("phase_count")
        arithmetic_phase_count = raw_controls.get("arithmetic_phase_count")
        recovery = str(raw_controls.get("recovery") or "")
        if (
            not isinstance(predicate_roles, list)
            or len(predicate_roles) > 8
            or len(set(predicate_roles)) != len(predicate_roles)
            or any(
                not isinstance(role, str)
                or _IDENTIFIER.fullmatch(role) is None
                for role in predicate_roles
            )
            or isinstance(active_table, bool)
            or not isinstance(active_table, int)
            or phase_count != 1 << len(predicate_roles)
            or not 0 < active_table < (1 << phase_count)
            or arithmetic_phase_count != active_table.bit_count()
            or recovery not in {
                "exhaustive_data_control_joint_replay",
                "bank_shared_control_protocol_replay",
            }
        ):
            raise ValueError("registered bridge control protocol is incomplete")
        result["controls"] = {
            "kind": "exhaustive_registered_predicate_protocol_v1",
            "predicate_roles": list(predicate_roles),
            "arithmetic_active_truth_table": active_table,
            "arithmetic_phase_count": arithmetic_phase_count,
            "phase_count": phase_count,
            "recovery": recovery,
        }
    if integration == "replace_downstream_shell_v1":
        gate_polarity = str(plan.get("output_gate_polarity") or "")
        if gate_polarity not in {"valid_high", "invalid_high"}:
            raise ValueError(
                "output_gate_polarity must be valid_high or invalid_high"
            )
        output_gate_role = str(plan.get("output_gate_role") or "output_gate")
        if _IDENTIFIER.fullmatch(output_gate_role) is None:
            raise ValueError("output_gate_role is not a Verilog identifier")
        result.update({
            "output_gate_polarity": gate_polarity,
            "output_gate_role": output_gate_role,
        })
    elif integration == "replace_observable_shell_truth_table_v1":
        control_roles = plan.get("shell_control_roles")
        has_alternate = plan.get("shell_has_alternate_data")
        truth_tables = plan.get("shell_truth_tables")
        if (
            not isinstance(control_roles, list)
            or len(control_roles) > 5
            or len(set(control_roles)) != len(control_roles)
            or any(
                not isinstance(role, str)
                or _IDENTIFIER.fullmatch(role) is None
                for role in control_roles
            )
            or not isinstance(has_alternate, bool)
            or not isinstance(truth_tables, list)
            or len(truth_tables) != width
        ):
            raise ValueError("observable shell truth-table schema is invalid")
        address_width = 1 + int(has_alternate) + len(control_roles)
        if address_width > 6:
            raise ValueError("observable shell truth table exceeds LUT width")
        table_limit = 1 << (1 << address_width)
        if any(
            isinstance(table, bool) or not isinstance(table, int)
            or not 0 <= table < table_limit
            for table in truth_tables
        ):
            raise ValueError("observable shell truth table is invalid")
        result.update({
            "shell_control_roles": list(control_roles),
            "shell_has_alternate_data": has_alternate,
            "shell_truth_tables": list(truth_tables),
        })
    else:
        control_roles = plan.get("control_roles")
        truth_tables = plan.get("control_truth_tables")
        includes_data_bit = plan.get(
            "control_truth_table_includes_data_bit", False
        )
        preserved_data_bits = plan.get(
            "control_preserved_data_bits", [False] * width
        )
        if (
            not isinstance(includes_data_bit, bool)
            or not isinstance(preserved_data_bits, list)
            or len(preserved_data_bits) != width
            or any(not isinstance(value, bool) for value in preserved_data_bits)
            or all(preserved_data_bits)
            or not isinstance(control_roles, list)
            or len(control_roles) > 6 - int(includes_data_bit)
            or len(set(control_roles)) != len(control_roles)
            or any(
                not isinstance(role, str)
                or _IDENTIFIER.fullmatch(role) is None
                for role in control_roles
            )
            or not isinstance(truth_tables, list)
            or len(truth_tables) != width
        ):
            raise ValueError("registered root control truth-table schema is invalid")
        address_width = len(control_roles) + int(includes_data_bit)
        table_limit = 1 << (1 << address_width)
        if any(
            isinstance(table, bool) or not isinstance(table, int)
            or not 0 <= table < table_limit
            for table in truth_tables
        ):
            raise ValueError("registered root control truth table is invalid")
        result.update({
            "control_roles": list(control_roles),
            "control_truth_table_includes_data_bit": includes_data_bit,
            "control_truth_tables": list(truth_tables),
            "control_preserved_data_bits": list(preserved_data_bits),
        })
    return result


def render_registered_bridge_verilog(plan: dict[str, Any]) -> str:
    """Render a bridge whose state cuts match the recovered source schedule.

    The first bank samples the incumbent input-register D/CE boundary.  The
    second bank is inferred into DSP BREG/PREG state, followed by the recovered
    wrapped accumulator register and final observable write register.  This is
    the crucial distinction from the rejected combinational local emitter.
    """
    plan = validate_registered_bridge_plan(plan)
    name = plan["module_name"]
    width = plan["data_width"]
    roles = plan["input_roles"]
    input_widths = plan["input_widths"]
    input_signed = plan["input_signed"]
    terms = plan["terms"]
    integration = plan["output_integration"]
    ports = [
        "    input wire clk,",
        *[
            f"    input wire ce_{role},"
            for role in roles
        ],
        "    input wire ce_product,",
    ]
    if integration == "replace_downstream_shell_v1":
        output_gate = plan["output_gate_role"]
        ports.extend([
            "    input wire ce_output,",
            "    input wire reset_output,",
            f"    input wire {output_gate},",
        ])
    elif integration == "replace_observable_shell_truth_table_v1":
        ports.extend([
            "    input wire ce_output,",
            "    input wire reset_output,",
            *[
                f"    input wire {role},"
                for role in plan["shell_control_roles"]
            ],
        ])
        if plan["shell_has_alternate_data"]:
            ports.append(
                f"    input wire [{width - 1}:0] alternate_data,"
            )
    else:
        ports.extend(
            f"    input wire {role}," for role in plan["control_roles"]
        )
        if any(plan["control_preserved_data_bits"]):
            ports.append(
                f"    input wire [{width - 1}:0] preserved_data,"
            )
    ports.extend([
        *[
            f"    input wire{' signed' if signed else ''} "
            f"[{input_width - 1}:0] {role}_d,"
            for role, input_width, signed in zip(
                roles, input_widths, input_signed
            )
        ],
        f"    output wire [{width - 1}:0] y",
    ])
    lines = [
        "// Generated from a current-DCP registered operator contract.",
        f"module {name} (",
        "\n".join(ports),
        ");",
    ]
    for role, input_width, signed in zip(roles, input_widths, input_signed):
        lines.append(
            f"  reg{' signed' if signed else ''} [{input_width - 1}:0] "
            f"{role}_q = {input_width}'d0;"
        )
    lines.extend(["", "  always @(posedge clk) begin"])
    for role in roles:
        lines.append(
            f"    if (ce_{role}) {role}_q <= {role}_d;"
        )
    lines.extend(["  end", ""])
    for ordinal, term in enumerate(terms):
        if term["kind"] == "fixed_product_slice":
            product_width = term["data_width"] + term["coefficient_width"]
            lines.append(
                "  (* use_dsp = \"yes\" *) "
                f"reg signed [{product_width - 1}:0] product_q_{ordinal} = "
                f"{product_width}'sd0;"
            )
        else:
            lines.append(
                f"  reg{' signed' if term['data_signed'] else ''} "
                f"[{term['data_width'] - 1}:0] residual_q_{ordinal} = "
                f"{term['data_width']}'d0;"
            )
    lines.append(
        f"  reg signed [{width - 1}:0] accumulated_q = {width}'sd0;"
    )
    if integration in {
        "replace_downstream_shell_v1",
        "replace_observable_shell_truth_table_v1",
    }:
        lines.append(f"  reg [{width - 1}:0] output_q = {width}'d0;")
    lines.append("")
    group_values = []
    for group_ordinal, group in enumerate(plan["groups"]):
        ordinals = group["term_ordinals"]
        common_shift = group["common_shift"]
        raw_widths = []
        for ordinal in ordinals:
            term = terms[ordinal]
            raw_width = (
                term["data_width"] + term["coefficient_width"]
                if term["kind"] == "fixed_product_slice"
                else term["data_width"]
            )
            raw_widths.append(raw_width + common_shift - term["shift"])
        group_width = max(
            common_shift + width,
            max(raw_widths),
        ) + (len(ordinals) - 1).bit_length() + 1
        aligned_names = []
        for local_ordinal, term_ordinal in enumerate(ordinals):
            term = terms[term_ordinal]
            source = (
                f"product_q_{term_ordinal}"
                if term["kind"] == "fixed_product_slice"
                else f"residual_q_{term_ordinal}"
            )
            raw_width = (
                term["data_width"] + term["coefficient_width"]
                if term["kind"] == "fixed_product_slice"
                else term["data_width"]
            )
            extended = f"group_{group_ordinal}_term_{local_ordinal}_extended"
            aligned = f"group_{group_ordinal}_term_{local_ordinal}_aligned"
            padding = group_width - raw_width
            fill = (
                f"{source}[{raw_width - 1}]"
                if term["data_signed"] else "1'b0"
            )
            lines.append(
                f"  wire signed [{group_width - 1}:0] {extended} = "
                f"{{{{{padding}{{{fill}}}}}, {source}}};"
            )
            left_shift = common_shift - term["shift"]
            lines.append(
                f"  wire signed [{group_width - 1}:0] {aligned} = "
                f"{extended} <<< {left_shift};"
            )
            aligned_names.append(aligned)
        total = f"group_{group_ordinal}_total"
        value = f"group_{group_ordinal}_value"
        lines.append(
            f"  wire signed [{group_width - 1}:0] {total} = "
            + " + ".join(aligned_names) + ";"
        )
        lines.append(
            f"  wire [{width - 1}:0] {value} = {total} >>> {common_shift};"
        )
        group_values.append(value)
    sum_expression = " + ".join(
        group_values
    )
    lines.extend([
        f"  wire signed [{width - 1}:0] accumulated_d = {sum_expression};",
        "",
        "  always @(posedge clk) begin",
        "    if (ce_product) begin",
    ])
    for ordinal, term in enumerate(terms):
        role = term["source_role"]
        if term["kind"] == "fixed_product_slice":
            operand = role + "_q"
            if not term["data_signed"]:
                operand = f"$signed({{1'b0, {operand}}})"
            lines.append(
                f"      product_q_{ordinal} <= {operand} * "
                f"{_signed_literal(term['coefficient_width'], term['coefficient'])};"
            )
        else:
            lines.append(
                f"      residual_q_{ordinal} <= {role}_q;"
            )
    lines.extend([
        "      accumulated_q <= accumulated_d;",
        "    end",
        "  end",
        "",
    ])
    if integration == "replace_downstream_shell_v1":
        lines.extend([
            "  always @(posedge clk) begin",
            f"    if (reset_output) output_q <= {width}'d0;",
            "    else if (ce_output)",
        ])
        if plan["output_gate_polarity"] == "valid_high":
            lines.append(
                f"      output_q <= {output_gate} ? accumulated_q : {width}'d0;"
            )
        else:
            lines.append(
                f"      output_q <= {output_gate} ? {width}'d0 : accumulated_q;"
            )
        lines.extend(["  end", "", "  assign y = output_q;"])
    elif integration == "replace_observable_shell_truth_table_v1":
        lines.extend([
            "  always @(posedge clk) begin",
            f"    if (reset_output) output_q <= {width}'d0;",
            "    else if (ce_output) output_q <= accumulated_q;",
            "  end",
            "",
        ])
        roles = plan["shell_control_roles"]
        has_alternate = plan["shell_has_alternate_data"]
        address_width = 1 + int(has_alternate) + len(roles)
        unique_tables = sorted(set(plan["shell_truth_tables"]))
        table_names = {}
        table_width = 1 << address_width
        hex_width = (table_width + 3) // 4
        for ordinal, table in enumerate(unique_tables):
            table_name = f"shell_table_{ordinal}"
            table_names[table] = table_name
            lines.append(
                f"  localparam [{table_width - 1}:0] {table_name} = "
                f"{table_width}'h{table:0{hex_width}x};"
            )
        for bit, table in enumerate(plan["shell_truth_tables"]):
            address_parts = [*reversed(roles)]
            if has_alternate:
                address_parts.append(f"alternate_data[{bit}]")
            address_parts.append(f"output_q[{bit}]")
            address = "{" + ", ".join(address_parts) + "}"
            lines.append(
                f"  wire [{address_width - 1}:0] shell_address_{bit} = "
                f"{address};"
            )
            lines.append(
                f"  assign y[{bit}] = {table_names[table]}"
                f"[shell_address_{bit}];"
            )
    else:
        roles = plan["control_roles"]
        includes_data_bit = plan["control_truth_table_includes_data_bit"]
        preserved_data_bits = plan["control_preserved_data_bits"]
        if roles or includes_data_bit:
            address_width = len(roles) + int(includes_data_bit)
            unique_tables = sorted(set(plan["control_truth_tables"]))
            table_names = {}
            table_width = 1 << address_width
            hex_width = (table_width + 3) // 4
            for ordinal, table in enumerate(unique_tables):
                table_name = f"control_table_{ordinal}"
                table_names[table] = table_name
                lines.append(
                    f"  localparam [{table_width - 1}:0] {table_name} = "
                    f"{table_width}'h{table:0{hex_width}x};"
                )
            for bit, table in enumerate(plan["control_truth_tables"]):
                if preserved_data_bits[bit]:
                    lines.append(f"  assign y[{bit}] = preserved_data[{bit}];")
                    continue
                if includes_data_bit:
                    address = "{" + ", ".join([
                        *reversed(roles), f"accumulated_q[{bit}]"
                    ]) + "}"
                    lines.append(
                        f"  wire [{address_width - 1}:0] control_address_{bit} = "
                        f"{address};"
                    )
                    lines.append(
                        f"  assign y[{bit}] = {table_names[table]}"
                        f"[control_address_{bit}];"
                    )
                else:
                    if bit == 0:
                        lines.append(
                            f"  wire [{address_width - 1}:0] control_address = "
                            + "{" + ", ".join(reversed(roles)) + "};"
                        )
                    lines.append(
                        f"  assign y[{bit}] = {table_names[table]}"
                        f"[control_address] ? accumulated_q[{bit}] : 1'b0;"
                    )
        else:
            for bit, preserved in enumerate(preserved_data_bits):
                source = (
                    f"preserved_data[{bit}]" if preserved
                    else f"accumulated_q[{bit}]"
                )
                lines.append(f"  assign y[{bit}] = {source};")
    lines.extend(["endmodule", ""])
    return "\n".join(lines)


def _wire_name(value: Any, label: str) -> str:
    value = str(value or "")
    if not value or "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError(f"{label} is not a valid recovered object name")
    return value


def _tcl_braced(value: str) -> str:
    return "{" + value.replace("\\", "\\\\").replace("}", "\\}") + "}"


def validate_registered_bridge_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    """Validate arithmetic modules plus current-DCP wiring recovered internally."""
    if not isinstance(bundle, dict) or bundle.get("version") != 1:
        raise ValueError("registered bridge bundle version must be 1")
    module_values = bundle.get("plans")
    if not isinstance(module_values, list) or not module_values:
        raise ValueError("registered bridge bundle has no module plans")
    modules = [validate_registered_bridge_plan(item) for item in module_values]
    module_by_name = {item["module_name"]: item for item in modules}
    if len(module_by_name) != len(modules):
        raise ValueError("registered bridge module names are not unique")
    instance_values = bundle.get("instances")
    if not isinstance(instance_values, list) or not instance_values:
        raise ValueError("registered bridge bundle has no instances")
    instances = []
    for ordinal, raw in enumerate(instance_values):
        if not isinstance(raw, dict):
            raise ValueError(f"instances[{ordinal}] must be an object")
        name = str(raw.get("name") or "")
        if _IDENTIFIER.fullmatch(name) is None:
            raise ValueError(f"instances[{ordinal}].name is not a safe identifier")
        module_name = str(raw.get("module_name") or "")
        module = module_by_name.get(module_name)
        if module is None:
            raise ValueError(f"instances[{ordinal}] references an unknown module")
        width = module["data_width"]
        input_width_by_role = dict(zip(
            module["input_roles"], module["input_widths"]
        ))
        input_d_nets = raw.get("input_d_nets")
        input_ce_nets = raw.get("input_ce_nets")
        if not isinstance(input_d_nets, dict) or set(input_d_nets) != set(
            module["input_roles"]
        ):
            raise ValueError(f"instances[{ordinal}] input D roles do not match")
        if not isinstance(input_ce_nets, dict) or set(input_ce_nets) != set(
            module["input_roles"]
        ):
            raise ValueError(f"instances[{ordinal}] input CE roles do not match")
        checked_d = {}
        checked_ce = {}
        for role in module["input_roles"]:
            nets = input_d_nets[role]
            input_width = input_width_by_role[role]
            if not isinstance(nets, list) or len(nets) != input_width:
                raise ValueError(
                    f"instances[{ordinal}] role {role} does not have "
                    f"{input_width} D nets"
                )
            checked_d[role] = [
                _wire_name(net, f"instances[{ordinal}].input_d_nets.{role}")
                for net in nets
            ]
            checked_ce[role] = _wire_name(
                input_ce_nets[role], f"instances[{ordinal}].input_ce_nets.{role}"
            )
        output_nets = raw.get("output_nets")
        if not isinstance(output_nets, list) or len(output_nets) != width:
            raise ValueError(f"instances[{ordinal}] output width does not match")
        checked_instance = {
            "name": name,
            "module_name": module_name,
            "input_d_nets": checked_d,
            "input_ce_nets": checked_ce,
            "clock_net": _wire_name(raw.get("clock_net"), "clock_net"),
            "product_ce_net": _wire_name(raw.get("product_ce_net"), "product_ce_net"),
            "output_nets": [
                _wire_name(net, f"instances[{ordinal}].output_nets")
                for net in output_nets
            ],
        }
        if module["output_integration"] == "replace_downstream_shell_v1":
            checked_instance.update({
                "output_ce_net": _wire_name(
                    raw.get("output_ce_net"), "output_ce_net"
                ),
                "output_reset_net": _wire_name(
                    raw.get("output_reset_net"), "output_reset_net"
                ),
                "output_gate_net": _wire_name(
                    raw.get("output_gate_net"), "output_gate_net"
                ),
            })
        elif (
            module["output_integration"]
            == "replace_observable_shell_truth_table_v1"
        ):
            shell_control_nets = raw.get("shell_control_nets")
            if (
                not isinstance(shell_control_nets, dict)
                or set(shell_control_nets) != set(module["shell_control_roles"])
            ):
                raise ValueError(
                    f"instances[{ordinal}] observable shell controls do not match"
                )
            checked_instance.update({
                "output_ce_net": _wire_name(
                    raw.get("output_ce_net"), "output_ce_net"
                ),
                "output_reset_net": _wire_name(
                    raw.get("output_reset_net"), "output_reset_net"
                ),
                "shell_control_nets": {
                    role: _wire_name(
                        shell_control_nets[role],
                        f"instances[{ordinal}].shell_control_nets.{role}",
                    )
                    for role in module["shell_control_roles"]
                },
            })
            alternate = raw.get("alternate_data_nets")
            if module["shell_has_alternate_data"]:
                if not isinstance(alternate, list) or len(alternate) != width:
                    raise ValueError(
                        f"instances[{ordinal}] alternate data width does not match"
                    )
                checked_instance["alternate_data_nets"] = [
                    _wire_name(
                        net, f"instances[{ordinal}].alternate_data_nets"
                    )
                    for net in alternate
                ]
            elif alternate not in (None, []):
                raise ValueError(
                    f"instances[{ordinal}] has unexpected alternate data nets"
                )
        else:
            control_nets = raw.get("control_nets")
            if (
                not isinstance(control_nets, dict)
                or set(control_nets) != set(module["control_roles"])
            ):
                raise ValueError(
                    f"instances[{ordinal}] root control roles do not match"
                )
            checked_instance["control_nets"] = {
                role: _wire_name(
                    control_nets[role],
                    f"instances[{ordinal}].control_nets.{role}",
                )
                for role in module["control_roles"]
            }
            preserved = raw.get("preserved_data_nets")
            if any(module["control_preserved_data_bits"]):
                if not isinstance(preserved, list) or len(preserved) != width:
                    raise ValueError(
                        f"instances[{ordinal}] preserved data width does not match"
                    )
                checked_instance["preserved_data_nets"] = [
                    _wire_name(
                        net, f"instances[{ordinal}].preserved_data_nets"
                    )
                    for net in preserved
                ]
            elif preserved not in (None, []):
                raise ValueError(
                    f"instances[{ordinal}] has unexpected preserved data nets"
                )
        instances.append(checked_instance)
    return {"version": 1, "plans": modules, "instances": instances}


def render_registered_bridge_insert_jython(bundle: dict[str, Any]) -> str:
    """Render generic RapidWright insertion for synthesized bridge modules."""
    bundle = validate_registered_bridge_bundle(bundle)
    module_names = [item["module_name"] for item in bundle["plans"]]
    instance_rows = [
        (item["name"], item["module_name"]) for item in bundle["instances"]
    ]
    return "\n".join([
        "from com.xilinx.rapidwright.design import Design",
        "from com.xilinx.rapidwright.design import Module as RWModule",
        "import sys",
        "",
        f"MODULE_NAMES = {module_names!r}",
        f"INSTANCES = {instance_rows!r}",
        "if len(sys.argv) != 3 + len(MODULE_NAMES):",
        "    raise RuntimeError('usage: insert.py input.dcp output.dcp module*.dcp')",
        "design = Design.readCheckpoint(sys.argv[1])",
        "modules = {}",
        "for name, path in zip(MODULE_NAMES, sys.argv[3:]):",
        "    modules[name] = RWModule(Design.readCheckpoint(path), False)",
        "for instance_name, module_name in INSTANCES:",
        "    instance = design.createModuleInst(instance_name, modules[module_name])",
        "    if instance is None:",
        "        raise RuntimeError('failed to insert ' + instance_name)",
        "    instance.unplace()",
        "design.getNetlist().resetParentNetMap()",
        "design.writeCheckpoint(sys.argv[2])",
        "print('REGISTERED_BRIDGE_INSERTED=' + str(len(INSTANCES)))",
        "",
    ])


def render_registered_bridge_connect_tcl(bundle: dict[str, Any]) -> str:
    """Render current-DCP wiring without embedding design identities in code."""
    bundle = validate_registered_bridge_bundle(bundle)
    lines = [
        "if {$argc != 2} { error {usage: connect.tcl inserted.dcp output.dcp} }",
        "proc rb_one {objects label} {",
        "  if {[llength $objects] != 1} { error \"$label expected one object, got [llength $objects]\" }",
        "  return [lindex $objects 0]",
        "}",
        "open_checkpoint [lindex $argv 0]",
        "set rb_rewire_net_names {}",
        "set rb_rewire_driver_cell_names {}",
    ]
    # Invalidate only the physical objects whose drivers are replaced.  A
    # global unplace destroys the consumer anchors needed by relative placement.
    for instance in bundle["instances"]:
        name = instance["name"]
        for bit, net in enumerate(instance["output_nets"]):
            pin = f"{name}/y[{bit}]"
            lines.extend([
                f"set rb_net [rb_one [get_nets -quiet {_tcl_braced(net)}] {_tcl_braced(pin + ' output net')} ]",
                "set rb_segments [get_nets -quiet -segments $rb_net]",
                "set rb_drivers [get_pins -quiet -leaf -of_objects $rb_segments -filter {DIRECTION == OUT}]",
                f"if {{[llength $rb_drivers] != 1}} {{ error {{{pin} old driver is not unique}} }}",
                "lappend rb_rewire_net_names {*}[get_property NAME $rb_segments]",
                "lappend rb_rewire_driver_cell_names {*}[get_property NAME [get_cells -quiet -of_objects $rb_drivers]]",
            ])
    lines.extend([
        "set rb_rewire_nets [get_nets -quiet [lsort -unique $rb_rewire_net_names]]",
        "set rb_rewire_driver_cells [get_cells -quiet [lsort -unique $rb_rewire_driver_cell_names]]",
        "if {[llength $rb_rewire_nets]} {route_design -unroute -nets $rb_rewire_nets}",
        "if {[llength $rb_rewire_driver_cells]} {unplace_cell $rb_rewire_driver_cells}",
    ])
    for instance in bundle["instances"]:
        name = instance["name"]
        module = next(
            item for item in bundle["plans"]
            if item["module_name"] == instance["module_name"]
        )
        scalar = {
            "clk": instance["clock_net"],
            "ce_product": instance["product_ce_net"],
        }
        if module["output_integration"] == "replace_downstream_shell_v1":
            scalar.update({
                "ce_output": instance["output_ce_net"],
                "reset_output": instance["output_reset_net"],
                module["output_gate_role"]: instance["output_gate_net"],
            })
        elif (
            module["output_integration"]
            == "replace_observable_shell_truth_table_v1"
        ):
            scalar.update({
                "ce_output": instance["output_ce_net"],
                "reset_output": instance["output_reset_net"],
                **instance["shell_control_nets"],
            })
        else:
            scalar.update(instance["control_nets"])
        for role in module["input_roles"]:
            scalar[f"ce_{role}"] = instance["input_ce_nets"][role]
        for pin, net in scalar.items():
            lines.extend([
                f"set rb_net [rb_one [get_nets -quiet {_tcl_braced(net)}] {_tcl_braced(name + '/' + pin + ' net')} ]",
                f"set rb_pin [rb_one [get_pins -quiet {_tcl_braced(name + '/' + pin)}] {_tcl_braced(name + '/' + pin)} ]",
                "connect_net -hierarchical -net $rb_net -objects $rb_pin",
            ])
        for role in module["input_roles"]:
            for bit, net in enumerate(instance["input_d_nets"][role]):
                pin = f"{name}/{role}_d[{bit}]"
                lines.extend([
                    f"set rb_net [rb_one [get_nets -quiet {_tcl_braced(net)}] {_tcl_braced(pin + ' net')} ]",
                    f"set rb_pin [rb_one [get_pins -quiet {_tcl_braced(pin)}] {_tcl_braced(pin)} ]",
                    "connect_net -hierarchical -net $rb_net -objects $rb_pin",
                ])
        if (
            module["output_integration"]
            == "replace_observable_shell_truth_table_v1"
            and module["shell_has_alternate_data"]
        ):
            for bit, net in enumerate(instance["alternate_data_nets"]):
                pin = f"{name}/alternate_data[{bit}]"
                lines.extend([
                    f"set rb_net [rb_one [get_nets -quiet {_tcl_braced(net)}] {_tcl_braced(pin + ' net')} ]",
                    f"set rb_pin [rb_one [get_pins -quiet {_tcl_braced(pin)}] {_tcl_braced(pin)} ]",
                    "connect_net -hierarchical -net $rb_net -objects $rb_pin",
                ])
        for bit, net in enumerate(instance["output_nets"]):
            pin = f"{name}/y[{bit}]"
            output_lines = [
                f"set rb_net [rb_one [get_nets -quiet {_tcl_braced(net)}] {_tcl_braced(pin + ' output net')} ]",
                "set rb_segments [get_nets -quiet -segments $rb_net]",
                "set rb_drivers [get_pins -quiet -leaf -of_objects $rb_segments -filter {DIRECTION == OUT}]",
                f"if {{[llength $rb_drivers] != 1}} {{ error {{{pin} old driver is not unique}} }}",
                # A routed hierarchical net can expose a driver on a distinct
                # logical segment. Disconnecting that pin from the requested
                # sink segment is a no-op in Vivado and leaves the pin occupied.
                # Always resolve the driver's actual logical net first.
                "set rb_driver_net [rb_one [get_nets -quiet -of_objects $rb_drivers] {old driver logical net}]",
                "disconnect_net -net $rb_driver_net -objects $rb_drivers",
            ]
            if (
                module["output_integration"] == "preserve_target_register_v1"
                and module["control_preserved_data_bits"][bit]
            ):
                preserved_pin = f"{name}/preserved_data[{bit}]"
                preserved_net = f"{name}_preserved_data_{bit}"
                output_lines.extend([
                    f"set rb_preserved_net [create_net {_tcl_braced(preserved_net)}]",
                    "connect_net -hierarchical -net $rb_preserved_net -objects $rb_drivers",
                    f"set rb_preserved_pin [rb_one [get_pins -quiet {_tcl_braced(preserved_pin)}] {_tcl_braced(preserved_pin)} ]",
                    "connect_net -hierarchical -net $rb_preserved_net -objects $rb_preserved_pin",
                ])
            output_lines.extend([
                f"set rb_pin [rb_one [get_pins -quiet {_tcl_braced(pin)}] {_tcl_braced(pin)} ]",
                "connect_net -hierarchical -net $rb_net -objects $rb_pin",
            ])
            lines.extend(output_lines)
    lines.extend([
        "write_checkpoint -force [lindex $argv 1]",
        "puts REGISTERED_BRIDGE_WIRING_DONE",
        "close_design",
        "exit",
        "",
    ])
    return "\n".join(lines)


def registered_bridge_port_connections(
    bundle: dict[str, Any],
) -> list[dict[str, str]]:
    """Return the exact logical port wiring for a generating bridge.

    The same normalized plan is consumed by the Vivado Tcl renderer and by
    the flat RapidWright insertion path used for macro-level OOC checkpoints.
    Keeping this projection pure makes the cross-backend interface directly
    testable without a JVM or a live design.
    """
    bundle = validate_registered_bridge_bundle(bundle)
    plans = {plan["module_name"]: plan for plan in bundle["plans"]}
    result: list[dict[str, str]] = []

    def add(instance: str, pin: str, net: str, direction: str = "input") -> None:
        result.append({
            "instance": instance,
            "pin": f"{instance}/{pin}",
            "net": net,
            "direction": direction,
        })

    for instance in bundle["instances"]:
        name = instance["name"]
        module = plans[instance["module_name"]]
        scalar = {
            "clk": instance["clock_net"],
            "ce_product": instance["product_ce_net"],
        }
        if module["output_integration"] == "replace_downstream_shell_v1":
            scalar.update({
                "ce_output": instance["output_ce_net"],
                "reset_output": instance["output_reset_net"],
                module["output_gate_role"]: instance["output_gate_net"],
            })
        elif module["output_integration"] == (
            "replace_observable_shell_truth_table_v1"
        ):
            scalar.update({
                "ce_output": instance["output_ce_net"],
                "reset_output": instance["output_reset_net"],
                **instance["shell_control_nets"],
            })
        else:
            scalar.update(instance["control_nets"])
        for role in module["input_roles"]:
            scalar[f"ce_{role}"] = instance["input_ce_nets"][role]
        for pin, net in scalar.items():
            add(name, pin, net)
        for role in module["input_roles"]:
            for bit, net in enumerate(instance["input_d_nets"][role]):
                add(name, f"{role}_d[{bit}]", net)
        if (
            module["output_integration"]
            == "replace_observable_shell_truth_table_v1"
            and module["shell_has_alternate_data"]
        ):
            for bit, net in enumerate(instance["alternate_data_nets"]):
                add(name, f"alternate_data[{bit}]", net)
        if (
            module["output_integration"] == "preserve_target_register_v1"
            and any(module["control_preserved_data_bits"])
        ):
            for bit, net in enumerate(instance["preserved_data_nets"]):
                if module["control_preserved_data_bits"][bit]:
                    add(name, f"preserved_data[{bit}]", net)
        for bit, net in enumerate(instance["output_nets"]):
            add(name, f"y[{bit}]", net, "output")
    return result


def _validate_uniform_registered_product_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Validate one schedule-preserving registered product-sum module."""
    if (
        not isinstance(plan, dict)
        or plan.get("version") != 1
        or plan.get("schedule_kind") != "registered_product_sum_v1"
    ):
        raise ValueError("registered product plan schema is unsupported")
    module_name = str(plan.get("module_name") or "")
    if _IDENTIFIER.fullmatch(module_name) is None:
        raise ValueError("registered product module name is invalid")
    width = _integer(plan.get("data_width"), "data_width")
    coefficient_width = _integer(plan.get("coefficient_width"), "coefficient_width")
    shift = _integer(plan.get("product_shift"), "product_shift")
    depth = _integer(plan.get("pipeline_depth"), "pipeline_depth")
    if not 2 <= width <= 64 or not 2 <= coefficient_width <= 27:
        raise ValueError("registered product width lies outside the DSP contract")
    if not 0 <= shift < width + coefficient_width or depth < 1:
        raise ValueError("registered product shift or depth is invalid")
    coefficients = plan.get("coefficients")
    roles = plan.get("input_roles")
    signed = plan.get("input_signed")
    if (
        not isinstance(coefficients, list)
        or not 2 <= len(coefficients) <= 8
        or not isinstance(roles, list)
        or len(roles) != len(coefficients)
        or len(set(roles)) != len(roles)
        or any(_IDENTIFIER.fullmatch(str(role)) is None for role in roles)
        or not isinstance(signed, list)
        or len(signed) != len(coefficients)
        or any(not isinstance(item, bool) for item in signed)
    ):
        raise ValueError("registered product term contract is invalid")
    lower = -(1 << (coefficient_width - 1))
    upper = (1 << (coefficient_width - 1)) - 1
    checked_coefficients = []
    for coefficient in coefficients:
        coefficient = _integer(coefficient, "coefficient")
        if not lower <= coefficient <= upper or coefficient == 0:
            raise ValueError("registered product coefficient is out of range")
        checked_coefficients.append(coefficient)
    edge = str(plan.get("clock_edge") or "")
    if edge not in {"posedge", "negedge"}:
        raise ValueError("registered product clock edge is invalid")
    product_resets = plan.get("product_reset_kinds")
    tail_resets = plan.get("tail_reset_kinds")
    allowed_resets = {"sync_clear", "async_clear"}
    if (
        not isinstance(product_resets, list)
        or len(product_resets) != len(roles)
        or any(item not in allowed_resets for item in product_resets)
        or not isinstance(tail_resets, list)
        or len(tail_resets) != depth - 1
        or any(item not in allowed_resets for item in tail_resets)
    ):
        raise ValueError("registered product reset schedule is invalid")
    return {
        "version": 1,
        "schedule_kind": "registered_product_sum_v1",
        "module_name": module_name,
        "data_width": width,
        "coefficient_width": coefficient_width,
        "product_shift": shift,
        "pipeline_depth": depth,
        "coefficients": checked_coefficients,
        "input_roles": [str(role) for role in roles],
        "input_signed": list(signed),
        "clock_edge": edge,
        "product_reset_kinds": list(product_resets),
        "tail_reset_kinds": list(tail_resets),
    }


def _validate_state_term(
    raw: Any,
    *,
    sources: dict[str, int],
    output_width: int,
) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("kind") not in {
        "fixed_product_slice", "passthrough",
    }:
        raise ValueError("registered state term kind is unsupported")
    source_role = str(raw.get("source_role") or "")
    source_width = sources.get(source_role)
    data_width = _integer(raw.get("data_width"), "term data_width")
    source_offset = _integer(raw.get("source_offset"), "term source_offset")
    shift = _integer(raw.get("shift"), "term shift")
    claimed_output = _integer(raw.get("output_width"), "term output_width")
    if (
        source_width is None
        or not 1 <= data_width <= source_width
        or not 0 <= source_offset <= source_width - data_width
        or shift < 0
        or claimed_output != output_width
        or not isinstance(raw.get("data_signed"), bool)
    ):
        raise ValueError("registered state term shape is invalid")
    checked = {
        "kind": str(raw["kind"]),
        "source_role": source_role,
        "source_offset": source_offset,
        "data_width": data_width,
        "data_signed": bool(raw["data_signed"]),
        "shift": shift,
        "output_width": output_width,
    }
    if raw["kind"] == "fixed_product_slice":
        coefficient = _integer(raw.get("coefficient"), "term coefficient")
        coefficient_width = _integer(
            raw.get("coefficient_width"), "term coefficient_width"
        )
        if (
            coefficient == 0
            or not 2 <= coefficient_width <= 27
            or not -(1 << (coefficient_width - 1))
            <= coefficient
            <= (1 << (coefficient_width - 1)) - 1
            or shift > data_width + coefficient_width - output_width
        ):
            raise ValueError("registered state product term is outside DSP contract")
        checked.update({
            "coefficient": coefficient,
            "coefficient_width": coefficient_width,
        })
    elif shift > data_width:
        raise ValueError("registered state passthrough shift is invalid")
    return checked


def _validate_branched_registered_product_plan(
    plan: dict[str, Any],
) -> dict[str, Any]:
    if (
        not isinstance(plan, dict)
        or plan.get("version") != 1
        or plan.get("schedule_kind") != "registered_branch_state_dag_v1"
    ):
        raise ValueError("registered branch state DAG schema is unsupported")
    module_name = str(plan.get("module_name") or "")
    if _IDENTIFIER.fullmatch(module_name) is None:
        raise ValueError("registered branch module name is invalid")
    output_width = _integer(plan.get("data_width"), "data_width")
    depth = _integer(plan.get("pipeline_depth"), "pipeline_depth")
    edge = str(plan.get("clock_edge") or "")
    if not 2 <= output_width <= 64 or depth != 2 or edge not in {
        "posedge", "negedge",
    }:
        raise ValueError("registered branch output schedule is invalid")
    raw_inputs = plan.get("inputs")
    if not isinstance(raw_inputs, list) or not 1 <= len(raw_inputs) <= 8:
        raise ValueError("registered branch inputs are invalid")
    inputs = []
    input_widths = {}
    for raw in raw_inputs:
        if not isinstance(raw, dict):
            raise ValueError("registered branch input is not an object")
        role = str(raw.get("role") or "")
        width = _integer(raw.get("width"), "registered branch input width")
        if (
            _IDENTIFIER.fullmatch(role) is None
            or role in input_widths
            or not 2 <= width <= 64
        ):
            raise ValueError("registered branch input identity is invalid")
        input_widths[role] = width
        inputs.append({"role": role, "width": width})
    raw_states = plan.get("states")
    if not isinstance(raw_states, list) or not 2 <= len(raw_states) <= 16:
        raise ValueError("registered branch states are invalid")
    allowed_resets = {"sync_clear", "async_clear"}
    states = []
    state_widths = {}
    for raw in raw_states:
        if not isinstance(raw, dict):
            raise ValueError("registered branch state is not an object")
        role = str(raw.get("role") or "")
        width = _integer(raw.get("width"), "registered branch state width")
        target_lsb = _integer(raw.get("target_lsb"), "registered branch target_lsb")
        reset_kind = str(raw.get("reset_kind") or "")
        if (
            _IDENTIFIER.fullmatch(role) is None
            or role in state_widths
            or not 2 <= width <= 64
            or target_lsb < 0
            or reset_kind not in allowed_resets
        ):
            raise ValueError("registered branch state identity is invalid")
        raw_terms = raw.get("terms")
        if not isinstance(raw_terms, list) or not 1 <= len(raw_terms) <= 8:
            raise ValueError("registered branch state has no expression")
        terms = [
            _validate_state_term(
                term, sources=input_widths, output_width=width
            )
            for term in raw_terms
        ]
        state_widths[role] = width
        states.append({
            "role": role,
            "width": width,
            "target_lsb": target_lsb,
            "reset_kind": reset_kind,
            "terms": terms,
        })
    raw_output = plan.get("output_state")
    if not isinstance(raw_output, dict):
        raise ValueError("registered branch output state is absent")
    if (
        _integer(raw_output.get("width"), "output state width") != output_width
        or str(raw_output.get("reset_kind") or "") not in allowed_resets
        or not isinstance(raw_output.get("terms"), list)
        or not raw_output["terms"]
    ):
        raise ValueError("registered branch output state is invalid")
    output_terms = [
        _validate_state_term(
            term, sources=state_widths, output_width=output_width
        )
        for term in raw_output["terms"]
    ]
    if {term["source_role"] for term in output_terms} != set(state_widths):
        raise ValueError("registered branch output does not cover every state")
    raw_groups = raw_output.get("groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise ValueError("registered branch output quantization groups are absent")
    groups = []
    covered = []
    for raw_group in raw_groups:
        if not isinstance(raw_group, dict):
            raise ValueError("registered branch output group is not an object")
        ordinals = raw_group.get("term_ordinals")
        common_shift = _integer(
            raw_group.get("common_shift"), "output group common_shift"
        )
        destination_lsb = _integer(
            raw_group.get("destination_lsb"), "output group destination_lsb"
        )
        group_width = _integer(
            raw_group.get("output_width"), "output group output_width"
        )
        if (
            not isinstance(ordinals, list)
            or not ordinals
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value < len(output_terms)
                for value in ordinals
            )
            or len(set(ordinals)) != len(ordinals)
            or common_shift < max(output_terms[value]["shift"] for value in ordinals)
            or destination_lsb < 0
            or group_width < 1
            or destination_lsb + group_width > output_width
        ):
            raise ValueError("registered branch output group is invalid")
        covered.extend(ordinals)
        groups.append({
            "term_ordinals": list(ordinals),
            "common_shift": common_shift,
            "destination_lsb": destination_lsb,
            "output_width": group_width,
        })
    if sorted(covered) != list(range(len(output_terms))):
        raise ValueError("registered branch output groups do not partition terms")
    return {
        "version": 1,
        "schedule_kind": "registered_branch_state_dag_v1",
        "module_name": module_name,
        "data_width": output_width,
        "pipeline_depth": 2,
        "clock_edge": edge,
        "inputs": inputs,
        "states": states,
        "output_state": {
            "width": output_width,
            "reset_kind": str(raw_output["reset_kind"]),
            "terms": output_terms,
            "groups": groups,
        },
    }


def validate_registered_product_plan(plan: dict[str, Any]) -> dict[str, Any]:
    if isinstance(plan, dict) and plan.get("schedule_kind") == (
        "registered_branch_state_dag_v1"
    ):
        return _validate_branched_registered_product_plan(plan)
    return _validate_uniform_registered_product_plan(plan)


def _always_header(edge: str, reset_kind: str, reset_name: str) -> str:
    sensitivity = f"{edge} clk"
    if reset_kind == "async_clear":
        sensitivity += f" or posedge {reset_name}"
    return f"  always @({sensitivity}) begin"


def _render_state_term(
    lines: list[str],
    *,
    prefix: str,
    term: dict[str, Any],
    source_expr: str,
) -> str:
    data_width = term["data_width"]
    output_width = term["output_width"]
    bits = f"{prefix}_bits"
    lines.append(f"  wire [{data_width - 1}:0] {bits} = {source_expr};")
    value = f"{prefix}_value"
    if term["kind"] == "passthrough":
        extended_width = max(data_width, term["shift"] + output_width)
        extended = f"{prefix}_extended"
        padding = extended_width - data_width
        if padding:
            fill = f"{bits}[{data_width - 1}]" if term["data_signed"] else "1'b0"
            lines.append(
                f"  wire [{extended_width - 1}:0] {extended} = "
                f"{{{{{padding}{{{fill}}}}}, {bits}}};"
            )
        else:
            lines.append(f"  wire [{extended_width - 1}:0] {extended} = {bits};")
        lines.append(
            f"  wire [{output_width - 1}:0] {value} = "
            f"{extended} >> {term['shift']};"
        )
        return value

    coefficient_width = term["coefficient_width"]
    coefficient = term["coefficient"]
    coefficient_wire = f"{prefix}_coefficient"
    lines.append(
        f"  wire signed [{coefficient_width - 1}:0] {coefficient_wire} = "
        f"{_signed_literal(coefficient_width, coefficient)};"
    )
    if term["data_signed"]:
        signed_width = data_width
        data_wire = f"{prefix}_data"
        lines.append(
            f"  wire signed [{data_width - 1}:0] {data_wire} = {bits};"
        )
    else:
        signed_width = data_width + 1
        data_wire = f"{prefix}_data"
        lines.append(
            f"  wire signed [{data_width}:0] {data_wire} = {{1'b0, {bits}}};"
        )
    wide_product_width = signed_width + coefficient_width
    product_width = data_width + coefficient_width
    product = f"{prefix}_product"
    product_bits = f"{prefix}_product_bits"
    lines.append(
        f"  (* use_dsp = \"yes\" *) wire signed "
        f"[{wide_product_width - 1}:0] {product} = "
        f"{data_wire} * {coefficient_wire};"
    )
    lines.append(
        f"  wire [{product_width - 1}:0] {product_bits} = "
        f"{product}[{product_width - 1}:0];"
    )
    lines.append(
        f"  wire [{output_width - 1}:0] {value} = "
        f"{product_bits} >> {term['shift']};"
    )
    return value


def _render_branched_registered_product_verilog(plan: dict[str, Any]) -> str:
    ports = ["    input wire clk,"]
    for state in plan["states"]:
        ports.extend([
            f"    input wire ce_{state['role']},",
            f"    input wire reset_{state['role']},",
        ])
    ports.extend([
        "    input wire ce_output,",
        "    input wire reset_output,",
    ])
    for item in plan["inputs"]:
        ports.append(
            f"    input wire [{item['width'] - 1}:0] {item['role']},"
        )
    ports.append(f"    output wire [{plan['data_width'] - 1}:0] y")
    lines = [
        "// Generated from a heterogeneous-CE registered arithmetic state DAG.",
        f"module {plan['module_name']} (",
        "\n".join(ports),
        ");",
        "",
    ]
    input_widths = {item["role"]: item["width"] for item in plan["inputs"]}
    state_widths = {item["role"]: item["width"] for item in plan["states"]}
    for state_index, state in enumerate(plan["states"]):
        values = []
        for term_index, term in enumerate(state["terms"]):
            source_role = term["source_role"]
            source_width = input_widths[source_role]
            offset = term["source_offset"]
            width = term["data_width"]
            source = source_role
            if offset or width != source_width:
                source = f"{source_role}[{offset} +: {width}]"
            values.append(_render_state_term(
                lines,
                prefix=f"state_{state_index}_term_{term_index}",
                term=term,
                source_expr=source,
            ))
        role = state["role"]
        width = state["width"]
        lines.extend([
            f"  wire [{width - 1}:0] {role}_d = " + " + ".join(values) + ";",
            f"  reg [{width - 1}:0] {role}_q = {width}'d0;",
            _always_header(
                plan["clock_edge"], state["reset_kind"], f"reset_{role}"
            ),
            f"    if (reset_{role}) {role}_q <= {width}'d0;",
            f"    else if (ce_{role}) {role}_q <= {role}_d;",
            "  end",
            "",
        ])
    width = plan["data_width"]
    output_terms = plan["output_state"]["terms"]
    output_values = []
    for group_index, group in enumerate(plan["output_state"]["groups"]):
        ordinals = group["term_ordinals"]
        raw_widths = []
        for ordinal in ordinals:
            term = output_terms[ordinal]
            signed_data_width = term["data_width"] + (not term["data_signed"])
            raw_widths.append(
                signed_data_width
                + (term.get("coefficient_width", 0) if term["kind"] == "fixed_product_slice" else 0)
                + group["common_shift"] - term["shift"]
            )
        accumulator_width = max(
            width + group["common_shift"],
            max(raw_widths),
        ) + len(ordinals).bit_length() + 1
        rows = []
        for local_index, ordinal in enumerate(ordinals):
            term = output_terms[ordinal]
            source_role = term["source_role"]
            source_width = state_widths[source_role]
            offset = term["source_offset"]
            data_width = term["data_width"]
            source = f"{source_role}_q"
            if offset or data_width != source_width:
                source = f"{source_role}_q[{offset} +: {data_width}]"
            prefix = f"output_group_{group_index}_term_{local_index}"
            bits = f"{prefix}_bits"
            lines.append(f"  wire [{data_width - 1}:0] {bits} = {source};")
            if term["data_signed"]:
                data = f"{prefix}_data"
                data_signed_width = data_width
                lines.append(
                    f"  wire signed [{data_width - 1}:0] {data} = {bits};"
                )
            else:
                data = f"{prefix}_data"
                data_signed_width = data_width + 1
                lines.append(
                    f"  wire signed [{data_width}:0] {data} = {{1'b0, {bits}}};"
                )
            raw = data
            raw_width = data_signed_width
            if term["kind"] == "fixed_product_slice":
                coefficient_width = term["coefficient_width"]
                coefficient = f"{prefix}_coefficient"
                raw = f"{prefix}_product"
                raw_width += coefficient_width
                lines.extend([
                    f"  wire signed [{coefficient_width - 1}:0] {coefficient} = "
                    f"{_signed_literal(coefficient_width, term['coefficient'])};",
                    f"  (* use_dsp = \"yes\" *) wire signed [{raw_width - 1}:0] "
                    f"{raw} = {data} * {coefficient};",
                ])
            row = f"{prefix}_row"
            aligned = f"{prefix}_aligned"
            shift = group["common_shift"] - term["shift"]
            lines.extend([
                f"  wire signed [{accumulator_width - 1}:0] {row} = {raw};",
                f"  wire signed [{accumulator_width - 1}:0] {aligned} = "
                f"{row} <<< {shift};",
            ])
            rows.append(aligned)
        total = f"output_group_{group_index}_total"
        quantized = f"output_group_{group_index}_quantized"
        value = f"output_group_{group_index}_value"
        placed = f"output_group_{group_index}_placed"
        total_attribute = (
            '(* use_dsp = "yes" *) '
            if any(
                output_terms[ordinal]["kind"] == "fixed_product_slice"
                for ordinal in ordinals
            )
            else ""
        )
        lines.extend([
            f"  {total_attribute}wire signed [{accumulator_width - 1}:0] {total} = "
            + " + ".join(rows) + ";",
            f"  wire signed [{accumulator_width - 1}:0] {quantized} = "
            f"{total} >>> {group['common_shift']};",
            f"  wire [{group['output_width'] - 1}:0] {value} = "
            f"{quantized}[{group['output_width'] - 1}:0];",
        ])
        lower = group["destination_lsb"]
        upper = width - lower - group["output_width"]
        pieces = []
        if upper:
            pieces.append(f"{upper}'d0")
        pieces.append(value)
        if lower:
            pieces.append(f"{lower}'d0")
        placed_expr = pieces[0] if len(pieces) == 1 else "{" + ", ".join(pieces) + "}"
        lines.append(f"  wire [{width - 1}:0] {placed} = {placed_expr};")
        output_values.append(placed)
    lines.extend([
        f"  wire [{width - 1}:0] output_d = " + " + ".join(output_values) + ";",
        f"  reg [{width - 1}:0] output_q = {width}'d0;",
        _always_header(
            plan["clock_edge"], plan["output_state"]["reset_kind"], "reset_output"
        ),
        f"    if (reset_output) output_q <= {width}'d0;",
        "    else if (ce_output) output_q <= output_d;",
        "  end",
        "",
        "  assign y = output_q;",
        "endmodule",
        "",
    ])
    return "\n".join(lines)


def render_registered_product_verilog(plan: dict[str, Any]) -> str:
    """Render a product-PREG plus recovered tail-register schedule."""
    plan = validate_registered_product_plan(plan)
    if plan["schedule_kind"] == "registered_branch_state_dag_v1":
        return _render_branched_registered_product_verilog(plan)
    width = plan["data_width"]
    coefficient_width = plan["coefficient_width"]
    product_width = width + coefficient_width
    roles = plan["input_roles"]
    depth = plan["pipeline_depth"]
    ports = ["    input wire clk,"]
    for role in roles:
        ports.extend([
            f"    input wire ce_product_{role},",
            f"    input wire reset_product_{role},",
        ])
    for index in range(depth - 1):
        ports.extend([
            f"    input wire ce_tail_{index},",
            f"    input wire reset_tail_{index},",
        ])
    for role, signed in zip(roles, plan["input_signed"]):
        signed_token = " signed" if signed else ""
        ports.append(f"    input wire{signed_token} [{width - 1}:0] {role},")
    ports.append(f"    output wire [{width - 1}:0] y")
    lines = [
        "// Generated from a current-DCP registered product closure.",
        f"module {plan['module_name']} (",
        "\n".join(ports),
        ");",
        "",
    ]
    for index, role in enumerate(roles):
        lines.append(
            f"  (* use_dsp = \"yes\" *) reg signed [{product_width - 1}:0] "
            f"product_q_{index} = {product_width}'sd0;"
        )
        reset = f"reset_product_{role}"
        lines.append(_always_header(
            plan["clock_edge"], plan["product_reset_kinds"][index], reset
        ))
        lines.extend([
            f"    if ({reset}) product_q_{index} <= {product_width}'sd0;",
            f"    else if (ce_product_{role}) product_q_{index} <= "
            f"{role} * {_signed_literal(coefficient_width, plan['coefficients'][index])};",
            "  end",
            f"  wire signed [{width - 1}:0] term_{index} = "
            f"product_q_{index} >>> {plan['product_shift']};",
            "",
        ])
    sum_expression = " + ".join(f"term_{index}" for index in range(len(roles)))
    lines.append(f"  wire signed [{width - 1}:0] sum_q = {sum_expression};")
    previous = "sum_q"
    for index, reset_kind in enumerate(plan["tail_reset_kinds"]):
        reset = f"reset_tail_{index}"
        lines.extend([
            f"  reg [{width - 1}:0] tail_q_{index} = {width}'d0;",
            _always_header(plan["clock_edge"], reset_kind, reset),
            f"    if ({reset}) tail_q_{index} <= {width}'d0;",
            f"    else if (ce_tail_{index}) tail_q_{index} <= {previous};",
            "  end",
            "",
        ])
        previous = f"tail_q_{index}"
    lines.extend([f"  assign y = {previous};", "endmodule", ""])
    return "\n".join(lines)


def validate_registered_product_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    if (
        not isinstance(bundle, dict)
        or bundle.get("version") != 2
        or bundle.get("kind") != "registered_product_bundle"
    ):
        raise ValueError("registered product bundle schema is unsupported")
    raw_plans = bundle.get("plans")
    if not isinstance(raw_plans, list) or not raw_plans:
        raise ValueError("registered product bundle has no plans")
    plans = [validate_registered_product_plan(item) for item in raw_plans]
    plan_by_name = {plan["module_name"]: plan for plan in plans}
    if len(plan_by_name) != len(plans):
        raise ValueError("registered product module names are not unique")
    raw_instances = bundle.get("instances")
    if not isinstance(raw_instances, list) or not raw_instances:
        raise ValueError("registered product bundle has no instances")
    instances = []
    names = set()
    for ordinal, raw in enumerate(raw_instances):
        if not isinstance(raw, dict):
            raise ValueError("registered product instance is not an object")
        name = str(raw.get("name") or "")
        module_name = str(raw.get("module_name") or "")
        plan = plan_by_name.get(module_name)
        if _IDENTIFIER.fullmatch(name) is None or name in names or plan is None:
            raise ValueError("registered product instance identity is invalid")
        names.add(name)
        width = plan["data_width"]
        inputs = raw.get("input_nets")
        if plan["schedule_kind"] == "registered_branch_state_dag_v1":
            input_widths = {item["role"]: item["width"] for item in plan["inputs"]}
            roles = list(input_widths)
        else:
            roles = list(plan["input_roles"])
            input_widths = {role: width for role in roles}
        if not isinstance(inputs, dict) or set(inputs) != set(roles):
            raise ValueError("registered product input roles do not match")
        checked_inputs = {}
        for role in roles:
            nets = inputs[role]
            if not isinstance(nets, list) or len(nets) != input_widths[role]:
                raise ValueError("registered product input width does not match")
            checked_inputs[role] = [_wire_name(net, "input net") for net in nets]
        outputs = raw.get("output_nets")
        if not isinstance(outputs, list) or len(outputs) != width:
            raise ValueError("registered product output width does not match")
        common = {
            "name": name,
            "module_name": module_name,
            "input_nets": checked_inputs,
            "clock_net": _wire_name(raw.get("clock_net"), "clock net"),
            "output_nets": [_wire_name(net, "output net") for net in outputs],
        }
        if plan["schedule_kind"] == "registered_branch_state_dag_v1":
            state_roles = {state["role"] for state in plan["states"]}
            state_ce = raw.get("state_ce_nets")
            state_reset = raw.get("state_reset_nets")
            if not all(
                isinstance(item, dict) and set(item) == state_roles
                for item in (state_ce, state_reset)
            ):
                raise ValueError("registered branch state controls do not match")
            instances.append({
                **common,
                "state_ce_nets": {
                    role: _wire_name(state_ce[role], "state CE")
                    for role in sorted(state_roles)
                },
                "state_reset_nets": {
                    role: _wire_name(state_reset[role], "state reset")
                    for role in sorted(state_roles)
                },
                "output_ce_net": _wire_name(
                    raw.get("output_ce_net"), "output CE"
                ),
                "output_reset_net": _wire_name(
                    raw.get("output_reset_net"), "output reset"
                ),
            })
        else:
            product_ce = raw.get("product_ce_nets")
            product_reset = raw.get("product_reset_nets")
            if not all(
                isinstance(item, dict) and set(item) == set(roles)
                for item in (product_ce, product_reset)
            ):
                raise ValueError("registered product controls do not match")
            tail_ce = raw.get("tail_ce_nets")
            tail_reset = raw.get("tail_reset_nets")
            if (
                not isinstance(tail_ce, list)
                or len(tail_ce) != plan["pipeline_depth"] - 1
                or not isinstance(tail_reset, list)
                or len(tail_reset) != len(tail_ce)
            ):
                raise ValueError("registered product tail controls do not match")
            instances.append({
                **common,
                "product_ce_nets": {
                    role: _wire_name(product_ce[role], "product CE")
                    for role in roles
                },
                "product_reset_nets": {
                    role: _wire_name(product_reset[role], "product reset")
                    for role in roles
                },
                "tail_ce_nets": [_wire_name(net, "tail CE") for net in tail_ce],
                "tail_reset_nets": [
                    _wire_name(net, "tail reset") for net in tail_reset
                ],
            })
    return {"version": 2, "kind": "registered_product_bundle", "plans": plans, "instances": instances}


def render_registered_product_insert_jython(bundle: dict[str, Any]) -> str:
    bundle = validate_registered_product_bundle(bundle)
    module_names = [plan["module_name"] for plan in bundle["plans"]]
    instance_rows = [(item["name"], item["module_name"]) for item in bundle["instances"]]
    return "\n".join([
        "from com.xilinx.rapidwright.design import Design",
        "from com.xilinx.rapidwright.design import Module as RWModule",
        "import sys",
        f"MODULE_NAMES = {module_names!r}",
        f"INSTANCES = {instance_rows!r}",
        "if len(sys.argv) != 3 + len(MODULE_NAMES):",
        "    raise RuntimeError('usage: insert.py input.dcp output.dcp module*.dcp')",
        "design = Design.readCheckpoint(sys.argv[1])",
        "modules = {name: RWModule(Design.readCheckpoint(path), False) for name, path in zip(MODULE_NAMES, sys.argv[3:])}",
        "for instance_name, module_name in INSTANCES:",
        "    instance = design.createModuleInst(instance_name, modules[module_name])",
        "    if instance is None: raise RuntimeError('failed to insert ' + instance_name)",
        "    instance.unplace()",
        "design.getNetlist().resetParentNetMap()",
        "design.writeCheckpoint(sys.argv[2])",
        "print('REGISTERED_PRODUCT_INSERTED=' + str(len(INSTANCES)))",
        "",
    ])


def render_registered_product_connect_tcl(bundle: dict[str, Any]) -> str:
    bundle = validate_registered_product_bundle(bundle)
    plans = {plan["module_name"]: plan for plan in bundle["plans"]}
    lines = [
        "if {$argc != 2} { error {usage: connect.tcl inserted.dcp output.dcp} }",
        "proc rb_one {objects label} {",
        "  if {[llength $objects] != 1} { error \"$label expected one object, got [llength $objects]\" }",
        "  return [lindex $objects 0]",
        "}",
        "open_checkpoint [lindex $argv 0]",
        "set rb_rewire_net_names {}",
        "set rb_rewire_driver_cell_names {}",
    ]
    for instance in bundle["instances"]:
        name = instance["name"]
        for bit, net in enumerate(instance["output_nets"]):
            pin = f"{name}/y[{bit}]"
            lines.extend([
                f"set rb_net [rb_one [get_nets -quiet {_tcl_braced(net)}] {_tcl_braced(pin + ' output net')}]",
                "set rb_segments [get_nets -quiet -segments $rb_net]",
                "set rb_drivers [get_pins -quiet -leaf -of_objects $rb_segments -filter {DIRECTION == OUT}]",
                f"if {{[llength $rb_drivers] != 1}} {{ error {{{pin} old driver is not unique}} }}",
                "lappend rb_rewire_net_names {*}[get_property NAME $rb_segments]",
                "lappend rb_rewire_driver_cell_names {*}[get_property NAME [get_cells -quiet -of_objects $rb_drivers]]",
            ])
    lines.extend([
        "set rb_rewire_nets [get_nets -quiet [lsort -unique $rb_rewire_net_names]]",
        "set rb_rewire_driver_cells [get_cells -quiet [lsort -unique $rb_rewire_driver_cell_names]]",
        "if {[llength $rb_rewire_nets]} {route_design -unroute -nets $rb_rewire_nets}",
        "if {[llength $rb_rewire_driver_cells]} {unplace_cell $rb_rewire_driver_cells}",
    ])
    for instance in bundle["instances"]:
        name = instance["name"]
        plan = plans[instance["module_name"]]
        scalar = {"clk": instance["clock_net"]}
        if plan["schedule_kind"] == "registered_branch_state_dag_v1":
            input_roles = [item["role"] for item in plan["inputs"]]
            for state in plan["states"]:
                role = state["role"]
                scalar[f"ce_{role}"] = instance["state_ce_nets"][role]
                scalar[f"reset_{role}"] = instance["state_reset_nets"][role]
            scalar["ce_output"] = instance["output_ce_net"]
            scalar["reset_output"] = instance["output_reset_net"]
        else:
            input_roles = list(plan["input_roles"])
            for role in input_roles:
                scalar[f"ce_product_{role}"] = instance["product_ce_nets"][role]
                scalar[f"reset_product_{role}"] = instance["product_reset_nets"][role]
            for index, net in enumerate(instance["tail_ce_nets"]):
                scalar[f"ce_tail_{index}"] = net
            for index, net in enumerate(instance["tail_reset_nets"]):
                scalar[f"reset_tail_{index}"] = net
        for pin, net in scalar.items():
            lines.extend([
                f"set rb_net [rb_one [get_nets -quiet {_tcl_braced(net)}] {_tcl_braced(name + '/' + pin + ' net')}]",
                f"set rb_pin [rb_one [get_pins -quiet {_tcl_braced(name + '/' + pin)}] {_tcl_braced(name + '/' + pin)}]",
                "connect_net -hierarchical -net $rb_net -objects $rb_pin",
            ])
        for role in input_roles:
            for bit, net in enumerate(instance["input_nets"][role]):
                pin = f"{name}/{role}[{bit}]"
                lines.extend([
                    f"set rb_net [rb_one [get_nets -quiet {_tcl_braced(net)}] {_tcl_braced(pin + ' net')}]",
                    f"set rb_pin [rb_one [get_pins -quiet {_tcl_braced(pin)}] {_tcl_braced(pin)}]",
                    "connect_net -hierarchical -net $rb_net -objects $rb_pin",
                ])
        for bit, net in enumerate(instance["output_nets"]):
            pin = f"{name}/y[{bit}]"
            lines.extend([
                f"set rb_net [rb_one [get_nets -quiet {_tcl_braced(net)}] {_tcl_braced(pin + ' output net')}]",
                "set rb_segments [get_nets -quiet -segments $rb_net]",
                "set rb_drivers [get_pins -quiet -leaf -of_objects $rb_segments -filter {DIRECTION == OUT}]",
                f"if {{[llength $rb_drivers] != 1}} {{ error {{{pin} old driver is not unique}} }}",
                "set rb_driver_net [rb_one [get_nets -quiet -of_objects $rb_drivers] {old driver logical net}]",
                "disconnect_net -net $rb_driver_net -objects $rb_drivers",
                f"set rb_pin [rb_one [get_pins -quiet {_tcl_braced(pin)}] {_tcl_braced(pin)}]",
                "connect_net -hierarchical -net $rb_net -objects $rb_pin",
            ])
    lines.extend([
        "write_checkpoint -force [lindex $argv 1]",
        "puts REGISTERED_PRODUCT_WIRING_DONE",
        "close_design",
        "exit",
        "",
    ])
    return "\n".join(lines)


__all__ = [
    "render_registered_bridge_connect_tcl",
    "registered_bridge_port_connections",
    "render_registered_bridge_insert_jython",
    "render_registered_bridge_verilog",
    "validate_registered_bridge_bundle",
    "validate_registered_bridge_plan",
    "render_registered_product_connect_tcl",
    "render_registered_product_insert_jython",
    "render_registered_product_verilog",
    "validate_registered_product_bundle",
    "validate_registered_product_plan",
]
