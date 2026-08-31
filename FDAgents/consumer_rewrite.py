"""Validated DCP wiring bundles for consumer-driven arithmetic rewrites."""

from __future__ import annotations

import re
from typing import Any


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SHELL_ROLES = {
    "signed_fixed_point_operand_select",
    "consumer_control_shell",
}


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


def _wire_name(value: Any, label: str) -> str:
    text = str(value or "")
    if not text or any(ch in text for ch in "\r\n\0"):
        raise ValueError(f"{label} is invalid")
    return text


def _control_protocol(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "clock_edge", "advance", "reset", "register_init",
    }:
        raise ValueError("consumer rewrite control protocol is incomplete")
    protocol = {
        "clock_edge": str(value.get("clock_edge") or ""),
        "advance": str(value.get("advance") or ""),
        "reset": str(value.get("reset") or ""),
        "register_init": _integer(value.get("register_init"), "register_init"),
    }
    if protocol != {
        "clock_edge": "rising",
        "advance": "every_cycle",
        "reset": "inactive",
        "register_init": 0,
    }:
        raise ValueError("consumer rewrite control protocol is unsupported")
    return protocol


def _boolean_shells(value: Any, *, quotient_low: int, quotient_high: int) -> list[dict[str, Any]]:
    if value in (None, []):
        return []
    if not isinstance(value, list):
        raise ValueError("consumer Boolean shells must be a list")
    checked = []
    names = set()
    for shell_ordinal, raw_shell in enumerate(value):
        if not isinstance(raw_shell, dict):
            raise ValueError("consumer Boolean shell is invalid")
        name = str(raw_shell.get("name") or "")
        role = str(raw_shell.get("semantic_role") or "")
        bits = raw_shell.get("bits")
        if (
            _IDENTIFIER.fullmatch(name) is None
            or name in names
            or role not in _SHELL_ROLES
            or not isinstance(bits, list)
            or not bits
        ):
            raise ValueError("consumer Boolean shell identity is invalid")
        names.add(name)
        checked_bits = []
        external_count = 0
        replay_count = 0
        for bit_ordinal, raw_bit in enumerate(bits):
            if not isinstance(raw_bit, dict):
                raise ValueError("consumer Boolean shell bit is invalid")
            lut_width = _integer(raw_bit.get("lut_width"), "lut_width")
            lut_init = _integer(raw_bit.get("lut_init"), "lut_init")
            sources = raw_bit.get("sources")
            if (
                not 1 <= lut_width <= 6
                or not 0 <= lut_init < (1 << (1 << lut_width))
                or not isinstance(sources, list)
                or len(sources) != lut_width
            ):
                raise ValueError("consumer Boolean shell LUT is invalid")
            checked_sources = []
            for source in sources:
                if not isinstance(source, dict):
                    raise ValueError("consumer Boolean shell source is invalid")
                kind = str(source.get("kind") or "")
                if kind == "quotient_bit":
                    index = _integer(source.get("index"), "quotient bit")
                    if not quotient_low <= index <= quotient_high:
                        raise ValueError("consumer Boolean shell quotient bit is unavailable")
                    checked_sources.append({"kind": kind, "index": index})
                elif kind == "overflow":
                    checked_sources.append({"kind": kind})
                elif kind == "constant":
                    checked_sources.append({
                        "kind": kind,
                        "value": _boolean(source.get("value"), "constant value"),
                    })
                elif kind == "external":
                    checked_sources.append({"kind": kind, "ordinal": external_count})
                    external_count += 1
                else:
                    raise ValueError("consumer Boolean shell source kind is unsupported")
            replay_count += 1 << lut_width
            checked_bits.append({
                "lut_width": lut_width,
                "lut_init": lut_init,
                "sources": checked_sources,
            })
        raw_control = raw_shell.get("control_semantics") or {}
        if not isinstance(raw_control, dict):
            raise ValueError("consumer Boolean shell control semantics are invalid")
        control = {
            "kind": str(raw_control.get("kind") or "preserved_boolean_dag_v1"),
            "word_shared_predicate_count": _integer(
                raw_control.get("word_shared_predicate_count", 0),
                "word shared predicate count",
            ),
            "bit_local_state_count": _integer(
                raw_control.get("bit_local_state_count", 0),
                "bit local state count",
            ),
            "truth_table_replay_count": _integer(
                raw_control.get("truth_table_replay_count", replay_count),
                "truth table replay count",
            ),
        }
        if (
            control["kind"] != "preserved_boolean_dag_v1"
            or control["word_shared_predicate_count"] < 0
            or control["bit_local_state_count"] < 0
            or control["truth_table_replay_count"] != replay_count
        ):
            raise ValueError("consumer Boolean shell control contract is incomplete")
        checked.append({
            "name": name,
            "semantic_role": role,
            "bits": checked_bits,
            "external_count": external_count,
            "control_semantics": control,
        })
    return checked


def _semantic_slices(value: Any, *, quotient_low: int, quotient_high: int) -> list[dict[str, Any]]:
    if value in (None, []):
        return []
    if not isinstance(value, list):
        raise ValueError("consumer semantic slices must be a list")
    checked = []
    names = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("consumer semantic slice is invalid")
        name = str(raw.get("name") or "")
        word_width = _integer(raw.get("word_width"), "semantic word width")
        layer_width = _integer(raw.get("layer_width"), "semantic layer width")
        word_count = _integer(raw.get("word_count"), "semantic word count")
        raw_maps = raw.get("word_select_map")
        raw_outputs = raw.get("output_sources")
        if (
            _IDENTIFIER.fullmatch(name) is None
            or name in names
            or raw.get("kind") != "layered_branch_magnitude_v1"
            or word_width != 32
            or not 1 <= layer_width <= 4
            or not 2 <= word_count <= 32
            or not isinstance(raw_maps, list)
            or len(raw_maps) != 2
            or not all(
                isinstance(row, list) and len(row) == 1 << layer_width
                for row in raw_maps
            )
            or not isinstance(raw_outputs, list)
            or not raw_outputs
        ):
            raise ValueError("consumer semantic slice contract is invalid")
        names.add(name)
        select_map = []
        for row in raw_maps:
            checked_row = [_integer(index, "semantic word index") for index in row]
            if any(not 0 <= index < word_count for index in checked_row):
                raise ValueError("consumer semantic word index is invalid")
            select_map.append(checked_row)
        outputs = []
        for source in raw_outputs:
            if not isinstance(source, dict):
                raise ValueError("consumer semantic output source is invalid")
            kind = str(source.get("kind") or "")
            if kind == "magnitude_bit":
                index = _integer(source.get("index"), "semantic magnitude bit")
                if not 0 <= index < word_width:
                    raise ValueError("consumer semantic magnitude bit is invalid")
                outputs.append({"kind": kind, "index": index})
            elif kind == "constant":
                outputs.append({
                    "kind": kind,
                    "value": _boolean(source.get("value"), "semantic constant"),
                })
            else:
                raise ValueError("consumer semantic output source kind is unsupported")
        proof = raw.get("proof")
        if not isinstance(proof, dict) or set(proof) != {
            "data_semantics", "control_semantics", "random_replay_trials",
        }:
            raise ValueError("consumer semantic proof is incomplete")
        replay_trials = _integer(proof.get("random_replay_trials"), "random replay trials")
        if (
            proof.get("data_semantics") != "signed_word_abs_or_saturated_reciprocal"
            or proof.get("control_semantics") != "branch_direction_layer_select"
            or replay_trials < 2
            or quotient_low < 0
            or quotient_high - quotient_low + 1 < 31
        ):
            raise ValueError("consumer semantic proof is unsupported")
        checked.append({
            "name": name,
            "kind": "layered_branch_magnitude_v1",
            "word_width": word_width,
            "layer_width": layer_width,
            "word_count": word_count,
            "word_select_map": select_map,
            "output_sources": outputs,
            "proof": {
                "data_semantics": proof["data_semantics"],
                "control_semantics": proof["control_semantics"],
                "random_replay_trials": replay_trials,
            },
        })
    return checked


def _primitive_shell_reference(
    value: Any,
    *,
    label: str,
    node_limit: int,
    external_count: int,
    quotient_low: int,
    quotient_high: int,
    nodes: list[dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} is invalid")
    kind = str(value.get("kind") or "")
    if kind == "constant":
        return {"kind": kind, "value": _boolean(value.get("value"), label)}
    if kind in {"overflow", "clock"}:
        return {"kind": kind}
    if kind == "quotient_bit":
        index = _integer(value.get("index"), label)
        if not quotient_low <= index <= quotient_high:
            raise ValueError(f"{label} quotient bit is unavailable")
        return {"kind": kind, "index": index}
    if kind == "quotient_or":
        raw_indices = value.get("indices")
        if not isinstance(raw_indices, list) or not raw_indices:
            raise ValueError(f"{label} quotient OR is empty")
        indices = [_integer(index, label) for index in raw_indices]
        if (
            indices != sorted(set(indices))
            or any(not quotient_low <= index <= quotient_high for index in indices)
        ):
            raise ValueError(f"{label} quotient OR bits are unavailable")
        return {"kind": kind, "indices": indices}
    if kind == "external":
        ordinal = _integer(value.get("ordinal"), label)
        if not 0 <= ordinal < external_count:
            raise ValueError(f"{label} external ordinal is invalid")
        return {"kind": kind, "ordinal": ordinal}
    if kind == "node":
        ordinal = _integer(value.get("node"), label)
        pin = str(value.get("pin") or "")
        if not 0 <= ordinal < node_limit:
            raise ValueError(f"{label} node is not topologically available")
        primitive = nodes[ordinal]["primitive"]
        valid_pin = (
            pin == "O" and re.fullmatch(r"LUT[1-6]|MUXF[789]", primitive)
            or pin == "Q" and primitive == "FDRE"
            or re.fullmatch(r"(?:O|CO)\[(\d+)\]", pin) is not None
            and primitive in {"CARRY4", "CARRY8"}
            and int(re.fullmatch(r"(?:O|CO)\[(\d+)\]", pin).group(1))
            < int(primitive[-1])
        )
        if not valid_pin:
            raise ValueError(f"{label} node output pin is invalid")
        return {"kind": kind, "node": ordinal, "pin": pin}
    raise ValueError(f"{label} kind is unsupported")


def _primitive_shell(
    value: Any,
    *,
    quotient_low: int,
    quotient_high: int,
) -> dict[str, Any] | None:
    if value in (None, {}):
        return None
    if not isinstance(value, dict) or value.get("kind") != "primitive_boolean_dag_v1":
        raise ValueError("consumer primitive shell schema is unsupported")
    external_count = _integer(value.get("external_count"), "primitive external count")
    raw_nodes = value.get("nodes")
    raw_outputs = value.get("outputs")
    if (
        not 0 <= external_count <= 4096
        or not isinstance(raw_nodes, list)
        or not isinstance(raw_outputs, list)
        or not raw_outputs
        or len(raw_nodes) > 20000
        or len(raw_outputs) > 4096
    ):
        raise ValueError("consumer primitive shell dimensions are invalid")
    nodes: list[dict[str, Any]] = []
    for ordinal, raw in enumerate(raw_nodes):
        if not isinstance(raw, dict):
            raise ValueError("consumer primitive shell node is invalid")
        primitive = str(raw.get("primitive") or "").upper()
        inputs = raw.get("inputs")
        properties = raw.get("properties")
        if (
            re.fullmatch(r"LUT[1-6]|CARRY[48]|MUXF[789]|FDRE", primitive) is None
            or not isinstance(inputs, list)
            or not isinstance(properties, dict)
        ):
            raise ValueError("consumer primitive shell primitive is unsupported")
        checked_inputs = []
        pins = set()
        for item in inputs:
            if not isinstance(item, dict):
                raise ValueError("consumer primitive shell input is invalid")
            pin = str(item.get("pin") or "")
            if pin in pins:
                raise ValueError("consumer primitive shell input pin is duplicated")
            pins.add(pin)
            checked_inputs.append({
                "pin": pin,
                "source": _primitive_shell_reference(
                    item.get("source"),
                    label=f"primitive node {ordinal} input",
                    node_limit=ordinal,
                    external_count=external_count,
                    quotient_low=quotient_low,
                    quotient_high=quotient_high,
                    nodes=nodes,
                ),
            })
        checked_properties: dict[str, Any]
        lut = re.fullmatch(r"LUT([1-6])", primitive)
        if lut is not None:
            width = int(lut.group(1))
            init = _integer(properties.get("init"), "primitive LUT INIT")
            if pins != {f"I{index}" for index in range(width)} or not 0 <= init < (1 << (1 << width)):
                raise ValueError("consumer primitive shell LUT is invalid")
            checked_properties = {"init": init}
        elif primitive in {"CARRY4", "CARRY8"}:
            width = int(primitive[-1])
            if any(
                pin not in {"CI", "CI_TOP"}
                and re.fullmatch(r"(?:DI|S)\[(\d+)\]", pin) is None
                for pin in pins
            ) or any(
                (match := re.fullmatch(r"(?:DI|S)\[(\d+)\]", pin)) is not None
                and int(match.group(1)) >= width
                for pin in pins
            ):
                raise ValueError("consumer primitive shell carry input is invalid")
            carry_type = str(properties.get("carry_type") or "SINGLE_CY8")
            if carry_type not in {"SINGLE_CY8", "DUAL_CY4"}:
                raise ValueError("consumer primitive shell carry type is invalid")
            checked_properties = {"carry_type": carry_type}
        elif primitive.startswith("MUXF"):
            if pins != {"I0", "I1", "S"} or properties:
                raise ValueError("consumer primitive shell mux is invalid")
            checked_properties = {}
        else:
            if pins != {"C", "CE", "D", "R"} or _integer(
                properties.get("init"), "primitive register INIT"
            ) != 0:
                raise ValueError("consumer primitive shell register is invalid")
            checked_properties = {"init": 0}
        nodes.append({
            "primitive": primitive,
            "properties": checked_properties,
            "inputs": checked_inputs,
        })
    outputs = [
        _primitive_shell_reference(
            reference,
            label=f"primitive output {ordinal}",
            node_limit=len(nodes),
            external_count=external_count,
            quotient_low=quotient_low,
            quotient_high=quotient_high,
            nodes=nodes,
        )
        for ordinal, reference in enumerate(raw_outputs)
    ]
    return {
        "kind": "primitive_boolean_dag_v1",
        "external_count": external_count,
        "nodes": nodes,
        "outputs": outputs,
        "cofactor_reduction_count": _integer(
            value.get("cofactor_reduction_count", 0), "cofactor reduction count"
        ),
        "cofactor_pruned_input_count": _integer(
            value.get("cofactor_pruned_input_count", 0), "cofactor pruned input count"
        ),
    }


def validate_consumer_rewrite_plan(plan: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, dict) or plan.get("version") != 1:
        raise ValueError("consumer rewrite plan version must be 1")
    name = str(plan.get("module_name") or "")
    if _IDENTIFIER.fullmatch(name) is None:
        raise ValueError("consumer rewrite module name is invalid")
    operator = str(plan.get("operator") or "")
    input_width = _integer(plan.get("input_width"), "input_width")
    output_width = _integer(plan.get("output_width"), "output_width")
    control_protocol = _control_protocol(plan.get("control_protocol"))
    raw_absorbed_controls = plan.get("absorbed_control_values") or []
    if (
        not isinstance(raw_absorbed_controls, list)
        or any(value not in {0, 1, False, True} for value in raw_absorbed_controls)
    ):
        raise ValueError("absorbed control values must be Boolean constants")
    absorbed_control_values = [int(value) for value in raw_absorbed_controls]
    if operator in {
        "unsigned_integer_sqrt_digit_recurrence",
        "unsigned_quantized_sqrt_digit_recurrence",
    }:
        pipeline_depth = _integer(plan.get("pipeline_depth"), "pipeline_depth")
        root_width = input_width // 2
        if (
            not 4 <= input_width <= 128
            or input_width % 2
            or not 1 <= pipeline_depth <= root_width
            or (
                operator == "unsigned_integer_sqrt_digit_recurrence"
                and output_width != root_width
            )
            or (
                operator == "unsigned_quantized_sqrt_digit_recurrence"
                and not 1 <= output_width < root_width
            )
        ):
            raise ValueError("integer-sqrt width or pipeline depth is invalid")
        return {
            "version": 1,
            "module_name": name,
            "operator": operator,
            "input_width": input_width,
            "output_width": output_width,
            "pipeline_depth": pipeline_depth,
            "control_protocol": control_protocol,
            "absorbed_control_values": absorbed_control_values,
        }
    if operator == "unsigned_fractional_divider_slice":
        word_width = _integer(plan.get("word_width"), "word_width")
        pipeline_depth = _integer(plan.get("pipeline_depth"), "pipeline_depth")
        output_copies = _integer(plan.get("output_copies"), "output_copies")
        if (
            not 8 <= word_width <= 64
            or input_width != 2 * word_width
            or not 2 <= pipeline_depth <= word_width
            or output_copies < 1
            or output_width != output_copies * (2 * word_width - 1)
        ):
            raise ValueError("fractional-divider slice contract is invalid")
        return {
            "version": 1,
            "module_name": name,
            "operator": operator,
            "input_width": input_width,
            "output_width": output_width,
            "word_width": word_width,
            "pipeline_depth": pipeline_depth,
            "output_copies": output_copies,
            "control_protocol": control_protocol,
            "absorbed_control_values": absorbed_control_values,
        }
    if operator == "signed_wide_product_boolean_shell":
        operand_width = _integer(plan.get("operand_width"), "operand_width")
        product_low_bit = _integer(
            plan.get("product_low_bit"), "product_low_bit"
        )
        product_high_bit = _integer(
            plan.get("product_high_bit"), "product_high_bit"
        )
        signed_a = _boolean(plan.get("signed_a"), "signed_a")
        signed_b = _boolean(plan.get("signed_b"), "signed_b")
        primitive_shell = _primitive_shell(
            plan.get("primitive_shell"),
            quotient_low=product_low_bit,
            quotient_high=product_high_bit,
        )
        if (
            not 16 <= operand_width <= 36
            or input_width != 2 * operand_width
            or not 0 <= product_low_bit <= product_high_bit < 2 * operand_width
            or primitive_shell is None
            or output_width != len(primitive_shell["outputs"])
        ):
            raise ValueError("wide-product Boolean-shell contract is invalid")

        # This operator has no separately defined overflow predicate.  Fresh
        # recovery supplies exact product bits, constants, current clock, and
        # external current-DCP nets only; accepting an abstract divider-style
        # overflow reference here would silently invent semantics.
        references = list(primitive_shell["outputs"])
        for node in primitive_shell["nodes"]:
            references.extend(item["source"] for item in node["inputs"])
        if any(reference["kind"] == "overflow" for reference in references):
            raise ValueError("wide-product shell cannot reference overflow")
        return {
            "version": 1,
            "module_name": name,
            "operator": operator,
            "input_width": input_width,
            "output_width": output_width,
            "operand_width": operand_width,
            "product_low_bit": product_low_bit,
            "product_high_bit": product_high_bit,
            "signed_a": signed_a,
            "signed_b": signed_b,
            "primitive_shell": primitive_shell,
            "control_protocol": control_protocol,
            "absorbed_control_values": absorbed_control_values,
        }
    if operator == "unsigned_constant_reciprocal_sqrt_slice":
        numerator_width = _integer(plan.get("numerator_width"), "numerator_width")
        numerator_constant = _integer(
            plan.get("numerator_constant"), "numerator_constant"
        )
        quotient_low_bit = _integer(plan.get("quotient_low_bit"), "quotient_low_bit")
        quotient_high_bit = _integer(
            plan.get("quotient_high_bit"), "quotient_high_bit"
        )
        overflow_low_bit = _integer(
            plan.get("overflow_low_bit"), "overflow_low_bit"
        )
        overflow_high_bit = _integer(
            plan.get("overflow_high_bit"), "overflow_high_bit"
        )
        sqrt_depth = _integer(plan.get("sqrt_pipeline_depth"), "sqrt_pipeline_depth")
        division_depth = _integer(
            plan.get("division_pipeline_depth"), "division_pipeline_depth"
        )
        expose_root = _boolean(plan.get("expose_root", False), "expose_root")
        expose_quotient = _boolean(
            plan.get("expose_quotient", True), "expose_quotient"
        )
        expose_overflow = _boolean(
            plan.get("expose_overflow", expose_quotient), "expose_overflow"
        )
        primitive_shell = _primitive_shell(
            plan.get("primitive_shell"),
            quotient_low=quotient_low_bit,
            quotient_high=numerator_width - 1,
        )
        slice_width = quotient_high_bit - quotient_low_bit + 1
        division_iterations = numerator_width - quotient_low_bit
        expected_output_width = (slice_width if expose_quotient else 0) + (
            1 if expose_overflow else 0
        ) + (
            input_width // 2 if expose_root else 0
        ) + len((primitive_shell or {}).get("outputs") or [])
        if (
            not 4 <= input_width <= 128
            or input_width % 2
            or numerator_width != input_width
            or not 0 < numerator_constant < (1 << numerator_width)
            or not 0 <= quotient_low_bit <= quotient_high_bit < numerator_width
            or overflow_low_bit != quotient_high_bit + 1
            or not overflow_low_bit <= overflow_high_bit < numerator_width - 1
            or output_width != expected_output_width
            or not 1 <= sqrt_depth <= input_width // 2
            or not 1 <= division_depth <= division_iterations
            or not (
                expose_root or expose_quotient or expose_overflow
                or primitive_shell is not None
            )
        ):
            raise ValueError("reciprocal-sqrt slice contract is invalid")
        boolean_shells = _boolean_shells(
            plan.get("boolean_shells"),
            quotient_low=quotient_low_bit,
            quotient_high=quotient_high_bit,
        )
        semantic_slices = _semantic_slices(
            plan.get("semantic_slices"),
            quotient_low=quotient_low_bit,
            quotient_high=quotient_high_bit,
        )
        return {
            "version": 1,
            "module_name": name,
            "operator": operator,
            "input_width": input_width,
            "output_width": output_width,
            "numerator_width": numerator_width,
            "numerator_constant": numerator_constant,
            "quotient_low_bit": quotient_low_bit,
            "quotient_high_bit": quotient_high_bit,
            "overflow_low_bit": overflow_low_bit,
            "overflow_high_bit": overflow_high_bit,
            "sqrt_pipeline_depth": sqrt_depth,
            "division_pipeline_depth": division_depth,
            "expose_root": expose_root,
            "expose_quotient": expose_quotient,
            "expose_overflow": expose_overflow,
            "primitive_shell": primitive_shell,
            "control_protocol": control_protocol,
            "absorbed_control_values": absorbed_control_values,
            "boolean_shells": boolean_shells,
            "semantic_slices": semantic_slices,
        }
    raise ValueError("consumer rewrite operator is unsupported")


def validate_consumer_rewrite_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    if (
        not isinstance(bundle, dict)
        or bundle.get("version") != 1
        or bundle.get("kind") != "registered_consumer_rewrite_bundle"
    ):
        raise ValueError("consumer rewrite bundle schema is unsupported")
    raw_plans = bundle.get("plans")
    if not isinstance(raw_plans, list) or not raw_plans:
        raise ValueError("consumer rewrite bundle has no plans")
    plans = [validate_consumer_rewrite_plan(plan) for plan in raw_plans]
    by_name = {plan["module_name"]: plan for plan in plans}
    if len(by_name) != len(plans):
        raise ValueError("consumer rewrite module names are not unique")
    raw_instances = bundle.get("instances")
    if not isinstance(raw_instances, list) or not raw_instances:
        raise ValueError("consumer rewrite bundle has no instances")
    instances = []
    names = set()
    for raw in raw_instances:
        if not isinstance(raw, dict):
            raise ValueError("consumer rewrite instance is invalid")
        name = str(raw.get("name") or "")
        module_name = str(raw.get("module_name") or "")
        plan = by_name.get(module_name)
        if _IDENTIFIER.fullmatch(name) is None or name in names or plan is None:
            raise ValueError("consumer rewrite instance identity is invalid")
        names.add(name)
        inputs = raw.get("input_nets")
        outputs = raw.get("output_nets")
        if (
            not isinstance(inputs, list)
            or len(inputs) != plan["input_width"]
            or not isinstance(outputs, list)
            or len(outputs) != plan["output_width"]
        ):
            raise ValueError("consumer rewrite wiring width does not match")
        instances.append({
            "name": name,
            "module_name": module_name,
            "clock_net": _wire_name(raw.get("clock_net"), "clock net"),
            "input_nets": [_wire_name(net, "input net") for net in inputs],
            "output_nets": [_wire_name(net, "output net") for net in outputs],
            "output_alias_nets": [],
            "absorbed_control_nets": [],
            "cleanup_scope": (
                _wire_name(raw.get("cleanup_scope"), "cleanup scope")
                if raw.get("cleanup_scope") is not None
                else None
            ),
            "boolean_shells": [],
            "semantic_slices": [],
            "primitive_shell_external_nets": [],
        })
        raw_output_aliases = raw.get("output_alias_nets")
        if raw_output_aliases is None:
            raw_output_aliases = [[] for _net in outputs]
        if (
            not isinstance(raw_output_aliases, list)
            or len(raw_output_aliases) != len(outputs)
            or any(not isinstance(group, list) for group in raw_output_aliases)
        ):
            raise ValueError("consumer rewrite output alias wiring does not match")
        seen_output_nets = set(instances[-1]["output_nets"])
        for bit, group in enumerate(raw_output_aliases):
            aliases = [
                _wire_name(net, f"output bit {bit} alias net") for net in group
            ]
            if len(set(aliases)) != len(aliases):
                raise ValueError("consumer rewrite output alias nets are not unique")
            if any(net in seen_output_nets for net in aliases):
                raise ValueError("consumer rewrite output alias net is reused")
            seen_output_nets.update(aliases)
            instances[-1]["output_alias_nets"].append(aliases)
        raw_absorbed_control_nets = raw.get("absorbed_control_nets") or []
        if (
            not isinstance(raw_absorbed_control_nets, list)
            or len(raw_absorbed_control_nets)
            != len(plan.get("absorbed_control_values") or [])
        ):
            raise ValueError("absorbed control wiring does not match")
        instances[-1]["absorbed_control_nets"] = [
            _wire_name(net, "absorbed control net")
            for net in raw_absorbed_control_nets
        ]
        raw_primitive_external_nets = raw.get("primitive_shell_external_nets") or []
        primitive_external_count = int(
            (plan.get("primitive_shell") or {}).get("external_count") or 0
        )
        if (
            not isinstance(raw_primitive_external_nets, list)
            or len(raw_primitive_external_nets) != primitive_external_count
        ):
            raise ValueError("consumer primitive shell wiring does not match")
        instances[-1]["primitive_shell_external_nets"] = [
            _wire_name(net, "primitive shell external net")
            for net in raw_primitive_external_nets
        ]
        raw_shells = raw.get("boolean_shells") or []
        if not isinstance(raw_shells, list) or len(raw_shells) != len(
            plan.get("boolean_shells") or []
        ):
            raise ValueError("consumer Boolean shell wiring does not match")
        for shell_ordinal, (raw_shell, shell_plan) in enumerate(zip(
            raw_shells, plan.get("boolean_shells") or []
        )):
            if not isinstance(raw_shell, dict):
                raise ValueError("consumer Boolean shell wiring is invalid")
            external_nets = raw_shell.get("external_nets")
            output_nets = raw_shell.get("output_nets")
            if (
                not isinstance(external_nets, list)
                or len(external_nets) != shell_plan["external_count"]
                or not isinstance(output_nets, list)
                or len(output_nets) != len(shell_plan["bits"])
            ):
                raise ValueError("consumer Boolean shell wiring width does not match")
            instances[-1]["boolean_shells"].append({
                "external_nets": [
                    _wire_name(net, f"shell {shell_ordinal} external net")
                    for net in external_nets
                ],
                "output_nets": [
                    _wire_name(net, f"shell {shell_ordinal} output net")
                    for net in output_nets
                ],
            })
        raw_semantic = raw.get("semantic_slices") or []
        plan_semantic = plan.get("semantic_slices") or []
        if not isinstance(raw_semantic, list) or len(raw_semantic) != len(plan_semantic):
            raise ValueError("consumer semantic slice wiring does not match")
        for ordinal, (raw_slice, slice_plan) in enumerate(zip(raw_semantic, plan_semantic)):
            if not isinstance(raw_slice, dict):
                raise ValueError("consumer semantic slice wiring is invalid")
            words = raw_slice.get("word_nets")
            layers = raw_slice.get("layer_nets")
            outputs = raw_slice.get("output_nets")
            if (
                not isinstance(words, list)
                or len(words) != slice_plan["word_count"]
                or any(
                    not isinstance(word, list) or len(word) != slice_plan["word_width"]
                    for word in words
                )
                or not isinstance(layers, list)
                or len(layers) != slice_plan["layer_width"]
                or not isinstance(outputs, list)
                or len(outputs) != len(slice_plan["output_sources"])
            ):
                raise ValueError("consumer semantic slice wiring width does not match")
            instances[-1]["semantic_slices"].append({
                "word_nets": [
                    [_wire_name(net, f"semantic slice {ordinal} word net") for net in word]
                    for word in words
                ],
                "layer_nets": [
                    _wire_name(net, f"semantic slice {ordinal} layer net") for net in layers
                ],
                "direction_net": _wire_name(
                    raw_slice.get("direction_net"), f"semantic slice {ordinal} direction net"
                ),
                "branch_net": _wire_name(
                    raw_slice.get("branch_net"), f"semantic slice {ordinal} branch net"
                ),
                "output_nets": [
                    _wire_name(net, f"semantic slice {ordinal} output net") for net in outputs
                ],
            })
    return {
        "version": 1,
        "kind": "registered_consumer_rewrite_bundle",
        "plans": plans,
        "instances": instances,
    }


def _stage_sizes(iterations: int, depth: int) -> list[int]:
    base, extra = divmod(iterations, depth)
    return [base + (1 if index < extra else 0) for index in range(depth)]


def _render_integer_sqrt(plan: dict[str, Any]) -> str:
    name = plan["module_name"]
    input_width = plan["input_width"]
    output_width = plan["output_width"]
    root_width = input_width // 2
    depth = plan["pipeline_depth"]
    state_width = input_width + 2
    absorbed_control_ports = [
        f"    output wire absorbed_control_{index},"
        for index, _value in enumerate(plan.get("absorbed_control_values") or [])
    ]
    lines = [
        "// Generated from a current-DCP consumer/operator contract.",
        "// Control: rising edge, free-running, inactive reset, register INIT=0.",
        f"module {name} (",
        "    input wire clk,",
        f"    input wire [{input_width - 1}:0] x,",
        *absorbed_control_ports,
        f"    output wire [{output_width - 1}:0] y",
        ");",
        "",
    ]
    previous_rad = "x"
    previous_rem = f"{state_width}'d0"
    previous_root = f"{root_width}'d0"
    iteration = 0
    for stage, count in enumerate(_stage_sizes(root_width, depth)):
        rad_source = previous_rad
        rem_source = previous_rem
        root_source = previous_root
        for local in range(count):
            pair_msb = input_width - 1 - 2 * iteration
            prefix = f"s{stage}_i{local}"
            lines.extend([
                f"  wire [{state_width - 1}:0] {prefix}_shifted = "
                f"({rem_source} << 2) | {rad_source}[{pair_msb} -: 2];",
                f"  wire [{state_width - 1}:0] {prefix}_trial = "
                f"({{{{2{{1'b0}}}}, {root_source}}} << 2) | {state_width}'d1;",
                f"  wire {prefix}_take = {prefix}_shifted >= {prefix}_trial;",
                f"  wire [{state_width - 1}:0] {prefix}_rem = {prefix}_take ? "
                f"({prefix}_shifted - {prefix}_trial) : {prefix}_shifted;",
                f"  wire [{root_width - 1}:0] {prefix}_root = "
                f"({root_source} << 1) | {prefix}_take;",
            ])
            rem_source = f"{prefix}_rem"
            root_source = f"{prefix}_root"
            iteration += 1
        lines.extend([
            f"  reg [{input_width - 1}:0] rad_q_{stage} = {input_width}'d0;",
            f"  reg [{state_width - 1}:0] rem_q_{stage} = {state_width}'d0;",
            f"  reg [{root_width - 1}:0] root_q_{stage} = {root_width}'d0;",
            "  always @(posedge clk) begin",
            f"    rad_q_{stage} <= {rad_source};",
            f"    rem_q_{stage} <= {rem_source};",
            f"    root_q_{stage} <= {root_source};",
            "  end",
            "",
        ])
        previous_rad = f"rad_q_{stage}"
        previous_rem = f"rem_q_{stage}"
        previous_root = f"root_q_{stage}"
    if plan["operator"] == "unsigned_quantized_sqrt_digit_recurrence":
        lines.extend([
            f"  wire root_overflow = |root_q_{depth - 1}[{root_width - 1}:{output_width}];",
            f"  assign y = root_overflow ? {{{output_width}{{1'b1}}}} : "
            f"root_q_{depth - 1}[{output_width - 1}:0];",
        ])
    else:
        lines.append(f"  assign y = root_q_{depth - 1};")
    for index, value in enumerate(plan.get("absorbed_control_values") or []):
        lines.append(f"  assign absorbed_control_{index} = 1'b{value};")
    lines.extend(["endmodule", ""])
    return "\n".join(lines)


def _primitive_node_signal(node: int, pin: str) -> str:
    match = re.fullmatch(r"(O|CO)\[(\d+)\]", pin)
    if match is not None:
        return f"primitive_node_{node}_{match.group(1)}[{match.group(2)}]"
    return f"primitive_node_{node}_{pin}"


def _primitive_reference_expression(
    reference: dict[str, Any],
    *,
    quotient_low: int,
    quotient_signal: str,
    overflow_expression: str,
) -> str:
    kind = reference["kind"]
    if kind == "constant":
        return "1'b1" if reference["value"] else "1'b0"
    if kind == "clock":
        return "clk"
    if kind == "overflow":
        return overflow_expression
    if kind == "quotient_bit":
        return f"{quotient_signal}[{reference['index'] - quotient_low}]"
    if kind == "quotient_or":
        bits = ", ".join(
            f"{quotient_signal}[{index - quotient_low}]"
            for index in reversed(reference["indices"])
        )
        return f"|{{{bits}}}"
    if kind == "external":
        return f"primitive_ext_{reference['ordinal']}"
    if kind == "node":
        return _primitive_node_signal(reference["node"], reference["pin"])
    raise AssertionError("validated primitive reference is not rendered")


def _render_primitive_shell(
    shell: dict[str, Any],
    *,
    quotient_low: int,
    quotient_signal: str,
    overflow_expression: str,
) -> list[str]:
    lines = []
    for ordinal, node in enumerate(shell["nodes"]):
        primitive = node["primitive"]
        inputs = {
            item["pin"]: _primitive_reference_expression(
                item["source"],
                quotient_low=quotient_low,
                quotient_signal=quotient_signal,
                overflow_expression=overflow_expression,
            )
            for item in node["inputs"]
        }
        lut = re.fullmatch(r"LUT([1-6])", primitive)
        if lut is not None:
            width = int(lut.group(1))
            init_width = 1 << width
            hex_width = (init_width + 3) // 4
            lines.extend([
                f"  wire primitive_node_{ordinal}_O;",
                f"  {primitive} #(.INIT({init_width}'h{node['properties']['init']:0{hex_width}x})) "
                f"primitive_node_{ordinal}_inst (",
                *(f"    .I{index}({inputs[f'I{index}']})," for index in range(width)),
                f"    .O(primitive_node_{ordinal}_O)",
                "  );",
            ])
        elif primitive in {"CARRY4", "CARRY8"}:
            width = int(primitive[-1])
            di = ", ".join(reversed([
                inputs.get(f"DI[{index}]", "1'b0") for index in range(width)
            ]))
            select = ", ".join(reversed([
                inputs.get(f"S[{index}]", "1'b0") for index in range(width)
            ]))
            lines.extend([
                f"  wire [{width - 1}:0] primitive_node_{ordinal}_O;",
                f"  wire [{width - 1}:0] primitive_node_{ordinal}_CO;",
            ])
            parameter = (
                f" #(.CARRY_TYPE(\"{node['properties']['carry_type']}\"))"
                if primitive == "CARRY8" else ""
            )
            ci = inputs.get("CI", "1'b0")
            secondary_carry = inputs.get(
                "CI_TOP" if primitive == "CARRY8" else "CYINIT", "1'b0"
            )
            carry_inputs = [
                f"    .CI({ci}),",
                (
                    f"    .CI_TOP({secondary_carry}),"
                    if primitive == "CARRY8"
                    else f"    .CYINIT({secondary_carry}),"
                ),
            ]
            lines.extend([
                f"  {primitive}{parameter} primitive_node_{ordinal}_inst (",
                *carry_inputs,
                f"    .DI({{{di}}}),",
                f"    .S({{{select}}}),",
                f"    .O(primitive_node_{ordinal}_O),",
                f"    .CO(primitive_node_{ordinal}_CO)",
                "  );",
            ])
        elif primitive.startswith("MUXF"):
            lines.extend([
                f"  wire primitive_node_{ordinal}_O;",
                f"  {primitive} primitive_node_{ordinal}_inst (",
                f"    .I0({inputs['I0']}), .I1({inputs['I1']}),",
                f"    .S({inputs['S']}), .O(primitive_node_{ordinal}_O)",
                "  );",
            ])
        else:
            lines.extend([
                f"  wire primitive_node_{ordinal}_Q;",
                "  FDRE #(.INIT(1'b0)) "
                f"primitive_node_{ordinal}_inst (",
                f"    .C({inputs['C']}), .CE({inputs['CE']}),",
                f"    .D({inputs['D']}), .R({inputs['R']}),",
                f"    .Q(primitive_node_{ordinal}_Q)",
                "  );",
            ])
    return lines


def _render_reciprocal_sqrt_slice(plan: dict[str, Any]) -> str:
    name = plan["module_name"]
    input_width = plan["input_width"]
    output_width = plan["output_width"]
    root_width = input_width // 2
    sqrt_depth = plan["sqrt_pipeline_depth"]
    numerator_width = plan["numerator_width"]
    numerator_constant = plan["numerator_constant"]
    quotient_low = plan["quotient_low_bit"]
    quotient_high = plan["quotient_high_bit"]
    overflow_low = plan["overflow_low_bit"]
    overflow_high = plan["overflow_high_bit"]
    division_depth = plan["division_pipeline_depth"]
    expose_root = plan["expose_root"]
    expose_quotient = plan["expose_quotient"]
    expose_overflow = plan["expose_overflow"]
    primitive_shell = plan.get("primitive_shell")
    quotient_window_width = numerator_width - quotient_low
    slice_width = quotient_high - quotient_low + 1
    sqrt_state_width = input_width + 2
    division_state_width = root_width + 1
    constant_digits = (numerator_width + 3) // 4
    primitive_ports = [
        f"    input wire primitive_ext_{ordinal},"
        for ordinal in range((primitive_shell or {}).get("external_count", 0))
    ]
    shell_ports = []
    for shell_ordinal, shell in enumerate(plan.get("boolean_shells") or []):
        shell_ports.extend(
            f"    input wire shell_{shell_ordinal}_ext_{external},"
            for external in range(shell["external_count"])
        )
        shell_ports.append(
            f"    output wire [{len(shell['bits']) - 1}:0] shell_{shell_ordinal}_y,"
        )
    semantic_ports = []
    for ordinal, semantic in enumerate(plan.get("semantic_slices") or []):
        semantic_ports.extend(
            f"    input wire [{semantic['word_width'] - 1}:0] semantic_{ordinal}_word_{word},"
            for word in range(semantic["word_count"])
        )
        semantic_ports.extend([
            f"    input wire [{semantic['layer_width'] - 1}:0] semantic_{ordinal}_layer,",
            f"    input wire semantic_{ordinal}_direction,",
            f"    input wire semantic_{ordinal}_branch,",
            f"    output wire [{len(semantic['output_sources']) - 1}:0] semantic_{ordinal}_y,",
        ])
    absorbed_control_ports = [
        f"    output wire absorbed_control_{index},"
        for index, _value in enumerate(plan.get("absorbed_control_values") or [])
    ]
    lines = [
        "// Generated from a current-DCP registered consumer slice.",
        "// Control: rising edge, free-running, inactive reset, register INIT=0.",
        f"module {name} (",
        "    input wire clk,",
        f"    input wire [{input_width - 1}:0] x,",
        *primitive_ports,
        *shell_ports,
        *semantic_ports,
        *absorbed_control_ports,
        f"    output wire [{output_width - 1}:0] y",
        ");",
        f"  localparam [{numerator_width - 1}:0] NUMERATOR = "
        f"{numerator_width}'h{numerator_constant:0{constant_digits}X};",
        "",
    ]

    previous_rad = "x"
    previous_rem = f"{sqrt_state_width}'d0"
    previous_root = f"{root_width}'d0"
    iteration = 0
    for stage, count in enumerate(_stage_sizes(root_width, sqrt_depth)):
        rad_source = previous_rad
        rem_source = previous_rem
        root_source = previous_root
        for local in range(count):
            pair_msb = input_width - 1 - 2 * iteration
            prefix = f"sqrt_s{stage}_i{local}"
            lines.extend([
                f"  wire [{sqrt_state_width - 1}:0] {prefix}_shifted = "
                f"({rem_source} << 2) | {rad_source}[{pair_msb} -: 2];",
                f"  wire [{sqrt_state_width - 1}:0] {prefix}_trial = "
                f"({{{{2{{1'b0}}}}, {root_source}}} << 2) | "
                f"{sqrt_state_width}'d1;",
                f"  wire {prefix}_take = {prefix}_shifted >= {prefix}_trial;",
                f"  wire [{sqrt_state_width - 1}:0] {prefix}_rem = "
                f"{prefix}_take ? ({prefix}_shifted - {prefix}_trial) : "
                f"{prefix}_shifted;",
                f"  wire [{root_width - 1}:0] {prefix}_root = "
                f"({root_source} << 1) | {prefix}_take;",
            ])
            rem_source = f"{prefix}_rem"
            root_source = f"{prefix}_root"
            iteration += 1
        lines.extend([
            f"  reg [{input_width - 1}:0] sqrt_rad_q_{stage} = {input_width}'d0;",
            f"  reg [{sqrt_state_width - 1}:0] sqrt_rem_q_{stage} = "
            f"{sqrt_state_width}'d0;",
            f"  reg [{root_width - 1}:0] sqrt_root_q_{stage} = {root_width}'d0;",
            "  always @(posedge clk) begin",
            f"    sqrt_rad_q_{stage} <= {rad_source};",
            f"    sqrt_rem_q_{stage} <= {rem_source};",
            f"    sqrt_root_q_{stage} <= {root_source};",
            "  end",
            "",
        ])
        previous_rad = f"sqrt_rad_q_{stage}"
        previous_rem = f"sqrt_rem_q_{stage}"
        previous_root = f"sqrt_root_q_{stage}"

    previous_den = previous_root
    previous_div_rem = f"{division_state_width}'d0"
    previous_quotient = f"{quotient_window_width}'d0"
    division_iteration = 0
    for stage, count in enumerate(
        _stage_sizes(quotient_window_width, division_depth)
    ):
        den_source = previous_den
        rem_source = previous_div_rem
        quotient_source = previous_quotient
        for local in range(count):
            numerator_bit = numerator_width - 1 - division_iteration
            prefix = f"div_s{stage}_i{local}"
            lines.extend([
                f"  wire [{division_state_width - 1}:0] {prefix}_shifted = "
                f"({rem_source} << 1) | NUMERATOR[{numerator_bit}];",
                f"  wire {prefix}_take = {prefix}_shifted >= "
                f"{{1'b0, {den_source}}};",
                f"  wire [{division_state_width - 1}:0] {prefix}_rem = "
                f"{prefix}_take ? ({prefix}_shifted - {{1'b0, {den_source}}}) : "
                f"{prefix}_shifted;",
                f"  wire [{quotient_window_width - 1}:0] {prefix}_quotient = "
                f"({quotient_source} << 1) | {prefix}_take;",
            ])
            rem_source = f"{prefix}_rem"
            quotient_source = f"{prefix}_quotient"
            division_iteration += 1
        lines.extend([
            f"  reg [{root_width - 1}:0] div_den_q_{stage} = {root_width}'d0;",
            f"  reg [{division_state_width - 1}:0] div_rem_q_{stage} = "
            f"{division_state_width}'d0;",
            f"  reg [{quotient_window_width - 1}:0] div_quot_q_{stage} = "
            f"{quotient_window_width}'d0;",
            "  always @(posedge clk) begin",
            f"    div_den_q_{stage} <= {den_source};",
            f"    div_rem_q_{stage} <= {rem_source};",
            f"    div_quot_q_{stage} <= {quotient_source};",
            "  end",
            "",
        ])
        previous_den = f"div_den_q_{stage}"
        previous_div_rem = f"div_rem_q_{stage}"
        previous_quotient = f"div_quot_q_{stage}"

    overflow_lsb = overflow_low - quotient_low
    overflow_msb = overflow_high - quotient_low
    output_offset = 0
    if expose_root:
        lines.append(
            f"  assign y[{output_offset + root_width - 1}:{output_offset}] = "
            f"sqrt_root_q_{sqrt_depth - 1};"
        )
        output_offset += root_width
    overflow_expression = f"|{previous_quotient}[{overflow_msb}:{overflow_lsb}]"
    if expose_quotient:
        lines.append(
            f"  assign y[{output_offset + slice_width - 1}:{output_offset}] = "
            f"{previous_quotient}[{slice_width - 1}:0];"
        )
        output_offset += slice_width
    if expose_overflow:
        lines.append(
            f"  assign y[{output_offset}] = "
            f"{overflow_expression};"
        )
        output_offset += 1
    if primitive_shell is not None:
        lines.extend(_render_primitive_shell(
            primitive_shell,
            quotient_low=quotient_low,
            quotient_signal=previous_quotient,
            overflow_expression=overflow_expression,
        ))
        for ordinal, reference in enumerate(primitive_shell["outputs"]):
            expression = _primitive_reference_expression(
                reference,
                quotient_low=quotient_low,
                quotient_signal=previous_quotient,
                overflow_expression=overflow_expression,
            )
            lines.append(f"  assign y[{output_offset + ordinal}] = {expression};")
    for shell_ordinal, shell in enumerate(plan.get("boolean_shells") or []):
        external = 0
        for bit_ordinal, bit in enumerate(shell["bits"]):
            values = []
            for source in bit["sources"]:
                kind = source["kind"]
                if kind == "quotient_bit":
                    values.append(
                        f"{previous_quotient}[{source['index'] - quotient_low}]"
                    )
                elif kind == "overflow":
                    values.append(overflow_expression)
                elif kind == "constant":
                    values.append("1'b1" if source["value"] else "1'b0")
                else:
                    values.append(f"shell_{shell_ordinal}_ext_{external}")
                    external += 1
            address = "{" + ", ".join(reversed(values)) + "}"
            table_width = 1 << bit["lut_width"]
            hex_width = (table_width + 3) // 4
            lines.extend([
                f"  localparam [{table_width - 1}:0] shell_{shell_ordinal}_table_{bit_ordinal} = "
                f"{table_width}'h{bit['lut_init']:0{hex_width}x};",
                f"  wire [{bit['lut_width'] - 1}:0] shell_{shell_ordinal}_address_{bit_ordinal} = {address};",
                f"  assign shell_{shell_ordinal}_y[{bit_ordinal}] = "
                f"shell_{shell_ordinal}_table_{bit_ordinal}[shell_{shell_ordinal}_address_{bit_ordinal}];",
            ])
    for ordinal, semantic in enumerate(plan.get("semantic_slices") or []):
        lines.extend([
            f"  reg [{semantic['word_width'] - 1}:0] semantic_{ordinal}_selected_word;",
            "  always @* begin",
            f"    case ({{semantic_{ordinal}_direction, semantic_{ordinal}_layer}})",
        ])
        for direction, row in enumerate(semantic["word_select_map"]):
            for layer, word in enumerate(row):
                selector = (direction << semantic["layer_width"]) | layer
                selector_width = semantic["layer_width"] + 1
                lines.append(
                    f"      {selector_width}'d{selector}: semantic_{ordinal}_selected_word = "
                    f"semantic_{ordinal}_word_{word};"
                )
        lines.extend([
            f"      default: semantic_{ordinal}_selected_word = "
            f"semantic_{ordinal}_word_{semantic['word_select_map'][0][0]};",
            "    endcase",
            "  end",
            f"  wire [{semantic['word_width'] - 1}:0] semantic_{ordinal}_absolute_word = "
            f"semantic_{ordinal}_selected_word[{semantic['word_width'] - 1}] ? "
            f"(~semantic_{ordinal}_selected_word + {semantic['word_width']}'d1) : "
            f"semantic_{ordinal}_selected_word;",
            f"  wire [30:0] semantic_{ordinal}_reciprocal_magnitude = "
            f"{overflow_expression} ? 31'h7fffffff : {previous_quotient}[30:0];",
            f"  wire [31:0] semantic_{ordinal}_magnitude = semantic_{ordinal}_branch ? "
            f"semantic_{ordinal}_absolute_word : {{1'b0, semantic_{ordinal}_reciprocal_magnitude}};",
        ])
        for output, source in enumerate(semantic["output_sources"]):
            expression = (
                f"semantic_{ordinal}_magnitude[{source['index']}]"
                if source["kind"] == "magnitude_bit"
                else ("1'b1" if source["value"] else "1'b0")
            )
            lines.append(f"  assign semantic_{ordinal}_y[{output}] = {expression};")
    for index, value in enumerate(plan.get("absorbed_control_values") or []):
        lines.append(f"  assign absorbed_control_{index} = 1'b{value};")
    lines.extend(["endmodule", ""])
    return "\n".join(lines)


def _render_wide_product_boolean_shell(plan: dict[str, Any]) -> str:
    name = plan["module_name"]
    operand_width = plan["operand_width"]
    product_width = 2 * operand_width
    output_width = plan["output_width"]
    primitive_shell = plan["primitive_shell"]
    primitive_ports = [
        f"    input wire primitive_ext_{ordinal},"
        for ordinal in range(primitive_shell["external_count"])
    ]
    absorbed_control_ports = [
        f"    output wire absorbed_control_{index},"
        for index, _value in enumerate(plan.get("absorbed_control_values") or [])
    ]
    a_sign = f"x[{operand_width - 1}]" if plan["signed_a"] else "1'b0"
    b_sign = (
        f"x[{2 * operand_width - 1}]" if plan["signed_b"] else "1'b0"
    )
    lines = [
        "// Generated from an anonymous current-DCP wide-product contract.",
        "// The surrounding Boolean shell and existing cycle boundary are preserved.",
        f"module {name} (",
        "    input wire clk,",
        f"    input wire [{2 * operand_width - 1}:0] x,",
        *primitive_ports,
        *absorbed_control_ports,
        f"    output wire [{output_width - 1}:0] y",
        ");",
        f"  wire signed [{operand_width}:0] operand_a_ext = "
        f"{{{a_sign}, x[{operand_width - 1}:0]}};",
        f"  wire signed [{operand_width}:0] operand_b_ext = "
        f"{{{b_sign}, x[{2 * operand_width - 1}:{operand_width}]}};",
        f"  wire signed [{2 * operand_width + 1}:0] semantic_product_ext = "
        "operand_a_ext * operand_b_ext;",
        f"  wire [{product_width - 1}:0] semantic_product = "
        f"semantic_product_ext[{product_width - 1}:0];",
        "",
    ]
    lines.extend(_render_primitive_shell(
        primitive_shell,
        # semantic_product retains its natural full-product bit numbering.
        quotient_low=0,
        quotient_signal="semantic_product",
        overflow_expression="1'b0",
    ))
    for ordinal, reference in enumerate(primitive_shell["outputs"]):
        expression = _primitive_reference_expression(
            reference,
            quotient_low=0,
            quotient_signal="semantic_product",
            overflow_expression="1'b0",
        )
        lines.append(f"  assign y[{ordinal}] = {expression};")
    for index, value in enumerate(plan.get("absorbed_control_values") or []):
        lines.append(f"  assign absorbed_control_{index} = 1'b{value};")
    lines.extend(["endmodule", ""])
    return "\n".join(lines)


def _render_fractional_divider_slice(plan: dict[str, Any]) -> str:
    name = plan["module_name"]
    word_width = plan["word_width"]
    input_width = plan["input_width"]
    output_width = plan["output_width"]
    quotient_width = 2 * word_width
    copied_width = quotient_width - 1
    absorbed_control_ports = [
        f"    output wire absorbed_control_{index},"
        for index, _value in enumerate(plan.get("absorbed_control_values") or [])
    ]
    lines = [
        "// Generated from a current-DCP fractional-divider consumer contract.",
        "// x[WORD-1:0]=denominator; x[2*WORD-1:WORD]=numerator high word.",
        f"module {name} (",
        "    input wire clk,",
        f"    input wire [{input_width - 1}:0] x,",
        *absorbed_control_ports,
        f"    output wire [{output_width - 1}:0] y",
        ");",
        f"  wire [{word_width - 1}:0] denominator = x[{word_width - 1}:0];",
        f"  wire [{word_width - 1}:0] numerator_word = x[{input_width - 1}:{word_width}];",
        "  wire same_sign = numerator_word[" + str(word_width - 1) + "] == denominator[" + str(word_width - 1) + "];",
        f"  wire low_word_observable = (denominator != {word_width}'b0) &&",
        "      (numerator_word < denominator) && (same_sign || (numerator_word == "
        + f"{word_width}'b0));",
        "",
        "  reg overflow_q_0 = 1'b0;",
        f"  reg [{word_width - 1}:0] denominator_q_0 = {word_width}'b0;",
        f"  reg [{word_width - 1}:0] remainder_q_0 = {word_width}'b0;",
        f"  reg [{word_width - 1}:0] quotient_q_0 = {word_width}'b0;",
        "  always @(posedge clk) begin",
        "    overflow_q_0 <= !low_word_observable;",
        "    denominator_q_0 <= denominator;",
        "    remainder_q_0 <= numerator_word;",
        f"    quotient_q_0 <= {word_width}'b0;",
        "  end",
        "",
    ]
    bit = word_width - 1
    stage_count = plan["pipeline_depth"] - 1
    base, extra = divmod(word_width - 1, stage_count)
    # Back-load the extra digit iterations.  This preserves the observed
    # registered schedule used by existing 32-bit/20-cycle contracts while
    # allowing the live contract extractor to derive other legal widths and
    # depths instead of baking one design's latency into the emitter.
    iteration_counts = [
        base + (1 if index >= stage_count - extra else 0)
        for index in range(stage_count)
    ]
    for stage, iteration_count in enumerate(iteration_counts, start=1):
        overflow_source = f"overflow_q_{stage - 1}"
        denominator_source = f"denominator_q_{stage - 1}"
        remainder_source = f"remainder_q_{stage - 1}"
        quotient_source = f"quotient_q_{stage - 1}"
        for local in range(iteration_count):
            prefix = f"s{stage}_i{local}"
            lines.extend([
                f"  wire [{word_width}:0] {prefix}_shifted = "
                f"{{{remainder_source}, 1'b0}};",
                f"  wire {prefix}_take = !{overflow_source} && "
                f"({prefix}_shifted >= {{1'b0, {denominator_source}}});",
                f"  wire [{word_width - 1}:0] {prefix}_remainder = {overflow_source} ? "
                f"{remainder_source} : ({prefix}_take ? "
                f"({prefix}_shifted - {{1'b0, {denominator_source}}}) : "
                f"{prefix}_shifted[{word_width - 1}:0]);",
                f"  wire [{word_width - 1}:0] {prefix}_quotient = "
                f"({quotient_source} & ~({word_width}'d1 << {bit})) | "
                f"({prefix}_take ? ({word_width}'d1 << {bit}) : {word_width}'d0);",
            ])
            remainder_source = f"{prefix}_remainder"
            quotient_source = f"{prefix}_quotient"
            bit -= 1
        lines.extend([
            f"  reg overflow_q_{stage} = 1'b0;",
            f"  reg [{word_width - 1}:0] denominator_q_{stage} = {word_width}'b0;",
            f"  reg [{word_width - 1}:0] remainder_q_{stage} = {word_width}'b0;",
            f"  reg [{word_width - 1}:0] quotient_q_{stage} = {word_width}'b0;",
            "  always @(posedge clk) begin",
            f"    overflow_q_{stage} <= {overflow_source};",
            f"    denominator_q_{stage} <= {denominator_source};",
            f"    remainder_q_{stage} <= {remainder_source};",
            f"    quotient_q_{stage} <= {quotient_source};",
            "  end",
            "",
        ])
    if bit != 0:
        raise AssertionError(
            "fractional-divider schedule did not cover the observable quotient bits"
        )
    final_stage = plan["pipeline_depth"] - 1
    lines.extend([
        f"  wire [{quotient_width - 1}:0] semantic_quotient = overflow_q_{final_stage} ?",
        f"      {{{word_width}'d1, {word_width}'b0}} :",
        f"      {{{word_width}'b0, quotient_q_{final_stage}[{word_width - 1}:1], 1'b0}};",
    ])
    for copy in range(plan["output_copies"]):
        low = copy * copied_width
        high = low + copied_width - 1
        lines.append(
            f"  assign y[{high}:{low}] = semantic_quotient[{copied_width - 1}:0];"
        )
    for index, value in enumerate(plan.get("absorbed_control_values") or []):
        lines.append(f"  assign absorbed_control_{index} = 1'b{value};")
    lines.extend(["endmodule", ""])
    return "\n".join(lines)


def render_consumer_rewrite_verilog(plan: dict[str, Any]) -> str:
    plan = validate_consumer_rewrite_plan(plan)
    if plan["operator"] in {
        "unsigned_integer_sqrt_digit_recurrence",
        "unsigned_quantized_sqrt_digit_recurrence",
    }:
        return _render_integer_sqrt(plan)
    if plan["operator"] == "unsigned_constant_reciprocal_sqrt_slice":
        return _render_reciprocal_sqrt_slice(plan)
    if plan["operator"] == "unsigned_fractional_divider_slice":
        return _render_fractional_divider_slice(plan)
    if plan["operator"] == "signed_wide_product_boolean_shell":
        return _render_wide_product_boolean_shell(plan)
    raise AssertionError("validated consumer rewrite operator is not rendered")


def _tcl_braced(value: str) -> str:
    return "{" + value.replace("}", "\\}") + "}"


def render_consumer_rewrite_connect_tcl(bundle: dict[str, Any]) -> str:
    bundle = validate_consumer_rewrite_bundle(bundle)
    lines = [
        "if {$argc != 2} { error {usage: connect.tcl inserted.dcp output.dcp} }",
        "proc cr_one {objects label} {",
        "  if {[llength $objects] != 1} { error \"$label expected one object, got [llength $objects]\" }",
        "  return [lindex $objects 0]",
        "}",
        "open_checkpoint [lindex $argv 0]",
    ]
    for instance in bundle["instances"]:
        name = instance["name"]
        lines.extend([
            f"set cr_net [cr_one [get_nets -quiet {_tcl_braced(instance['clock_net'])}] {_tcl_braced(name + '/clk net')}]",
            f"set cr_pin [cr_one [get_pins -quiet {_tcl_braced(name + '/clk')}] {_tcl_braced(name + '/clk')}]",
            "connect_net -hierarchical -net $cr_net -objects $cr_pin",
        ])
        for bit, net in enumerate(instance["input_nets"]):
            pin = f"{name}/x[{bit}]"
            lines.extend([
                f"set cr_net [cr_one [get_nets -quiet {_tcl_braced(net)}] {_tcl_braced(pin + ' net')}]",
                f"set cr_pin [cr_one [get_pins -quiet {_tcl_braced(pin)}] {_tcl_braced(pin)}]",
                "connect_net -hierarchical -net $cr_net -objects $cr_pin",
            ])
        for ordinal, net in enumerate(
            instance.get("primitive_shell_external_nets") or []
        ):
            pin = f"{name}/primitive_ext_{ordinal}"
            lines.extend([
                f"set cr_net [cr_one [get_nets -quiet {_tcl_braced(net)}] {_tcl_braced(pin + ' net')}]",
                f"set cr_pin [cr_one [get_pins -quiet {_tcl_braced(pin)}] {_tcl_braced(pin)}]",
                "connect_net -hierarchical -net $cr_net -objects $cr_pin",
            ])
        for bit, net in enumerate(instance["output_nets"]):
            pin = f"{name}/y[{bit}]"
            lines.extend([
                f"set cr_net [cr_one [get_nets -quiet {_tcl_braced(net)}] {_tcl_braced(pin + ' output net')}]",
                "set cr_segments [get_nets -quiet -segments $cr_net]",
                "set cr_drivers [get_pins -quiet -leaf -of_objects $cr_segments -filter {DIRECTION == OUT}]",
                f"if {{[llength $cr_drivers] != 1}} {{ error {{{pin} old driver is not unique}} }}",
                "foreach cr_driver $cr_drivers {",
                "  set cr_driver_net [cr_one [get_nets -quiet -of_objects $cr_driver] {old driver segment}]",
                "  disconnect_net -net $cr_driver_net -objects $cr_driver",
                "}",
                f"set cr_pin [cr_one [get_pins -quiet {_tcl_braced(pin)}] {_tcl_braced(pin)}]",
                "connect_net -hierarchical -net $cr_net -objects $cr_pin",
            ])
            for alias_ordinal, alias_net in enumerate(
                instance.get("output_alias_nets", [])[bit]
            ):
                label = f"{pin} alias {alias_ordinal}"
                lines.extend([
                    f"set cr_alias_net [cr_one [get_nets -quiet {_tcl_braced(alias_net)}] {_tcl_braced(label + ' net')}]",
                    "set cr_alias_segments [get_nets -quiet -segments $cr_alias_net]",
                    "set cr_alias_sinks [get_pins -quiet -leaf -of_objects $cr_alias_segments -filter {DIRECTION == IN}]",
                    "set cr_alias_ports [get_ports -quiet -of_objects $cr_alias_segments]",
                    f"if {{![llength $cr_alias_sinks] && ![llength $cr_alias_ports]}} {{ error {{{label} has no observable consumer}} }}",
                    "foreach cr_alias_sink $cr_alias_sinks {",
                    "  set cr_alias_sink_net [cr_one [get_nets -quiet -of_objects $cr_alias_sink] {old alias sink segment}]",
                    "  disconnect_net -net $cr_alias_sink_net -objects $cr_alias_sink",
                    "  connect_net -hierarchical -net $cr_net -objects $cr_alias_sink",
                    "}",
                    "foreach cr_alias_port $cr_alias_ports {",
                    "  set cr_alias_port_net [cr_one [get_nets -quiet -of_objects $cr_alias_port] {old alias port segment}]",
                    "  disconnect_net -net $cr_alias_port_net -objects $cr_alias_port",
                    "  connect_net -hierarchical -net $cr_net -objects $cr_alias_port",
                    "}",
                ])
        for ordinal, net in enumerate(instance.get("absorbed_control_nets") or []):
            pin = f"{name}/absorbed_control_{ordinal}"
            lines.extend([
                f"set cr_net [cr_one [get_nets -quiet {_tcl_braced(net)}] {_tcl_braced(pin + ' output net')}]",
                "set cr_segments [get_nets -quiet -segments $cr_net]",
                "set cr_drivers [get_pins -quiet -leaf -of_objects $cr_segments -filter {DIRECTION == OUT}]",
                f"if {{[llength $cr_drivers] != 1}} {{ error {{{pin} old driver is not unique}} }}",
                "foreach cr_driver $cr_drivers {",
                "  set cr_driver_net [cr_one [get_nets -quiet -of_objects $cr_driver] {old absorbed control driver segment}]",
                "  disconnect_net -net $cr_driver_net -objects $cr_driver",
                "}",
                f"set cr_pin [cr_one [get_pins -quiet {_tcl_braced(pin)}] {_tcl_braced(pin)}]",
                "connect_net -hierarchical -net $cr_net -objects $cr_pin",
            ])
        for shell_ordinal, shell in enumerate(instance.get("boolean_shells") or []):
            for external, net in enumerate(shell["external_nets"]):
                pin = f"{name}/shell_{shell_ordinal}_ext_{external}"
                lines.extend([
                    f"set cr_net [cr_one [get_nets -quiet {_tcl_braced(net)}] {_tcl_braced(pin + ' net')}]",
                    f"set cr_pin [cr_one [get_pins -quiet {_tcl_braced(pin)}] {_tcl_braced(pin)}]",
                    "connect_net -hierarchical -net $cr_net -objects $cr_pin",
                ])
            for bit, net in enumerate(shell["output_nets"]):
                pin = f"{name}/shell_{shell_ordinal}_y[{bit}]"
                lines.extend([
                    f"set cr_net [cr_one [get_nets -quiet {_tcl_braced(net)}] {_tcl_braced(pin + ' output net')}]",
                    "set cr_segments [get_nets -quiet -segments $cr_net]",
                    "set cr_drivers [get_pins -quiet -leaf -of_objects $cr_segments -filter {DIRECTION == OUT}]",
                    f"if {{[llength $cr_drivers] != 1}} {{ error {{{pin} old driver is not unique}} }}",
                    "foreach cr_driver $cr_drivers {",
                    "  set cr_driver_net [cr_one [get_nets -quiet -of_objects $cr_driver] {old shell driver segment}]",
                    "  disconnect_net -net $cr_driver_net -objects $cr_driver",
                    "}",
                    f"set cr_pin [cr_one [get_pins -quiet {_tcl_braced(pin)}] {_tcl_braced(pin)}]",
                    "connect_net -hierarchical -net $cr_net -objects $cr_pin",
                ])
        for ordinal, semantic in enumerate(instance.get("semantic_slices") or []):
            for word, nets in enumerate(semantic["word_nets"]):
                for bit, net in enumerate(nets):
                    pin = f"{name}/semantic_{ordinal}_word_{word}[{bit}]"
                    lines.extend([
                        f"set cr_net [cr_one [get_nets -quiet {_tcl_braced(net)}] {_tcl_braced(pin + ' net')}]",
                        f"set cr_pin [cr_one [get_pins -quiet {_tcl_braced(pin)}] {_tcl_braced(pin)}]",
                        "connect_net -hierarchical -net $cr_net -objects $cr_pin",
                    ])
            for bit, net in enumerate(semantic["layer_nets"]):
                pin = f"{name}/semantic_{ordinal}_layer[{bit}]"
                lines.extend([
                    f"set cr_net [cr_one [get_nets -quiet {_tcl_braced(net)}] {_tcl_braced(pin + ' net')}]",
                    f"set cr_pin [cr_one [get_pins -quiet {_tcl_braced(pin)}] {_tcl_braced(pin)}]",
                    "connect_net -hierarchical -net $cr_net -objects $cr_pin",
                ])
            for role, net in (
                ("direction", semantic["direction_net"]),
                ("branch", semantic["branch_net"]),
            ):
                pin = f"{name}/semantic_{ordinal}_{role}"
                lines.extend([
                    f"set cr_net [cr_one [get_nets -quiet {_tcl_braced(net)}] {_tcl_braced(pin + ' net')}]",
                    f"set cr_pin [cr_one [get_pins -quiet {_tcl_braced(pin)}] {_tcl_braced(pin)}]",
                    "connect_net -hierarchical -net $cr_net -objects $cr_pin",
                ])
            for bit, net in enumerate(semantic["output_nets"]):
                pin = f"{name}/semantic_{ordinal}_y[{bit}]"
                lines.extend([
                    f"set cr_net [cr_one [get_nets -quiet {_tcl_braced(net)}] {_tcl_braced(pin + ' output net')}]",
                    "set cr_segments [get_nets -quiet -segments $cr_net]",
                    "set cr_drivers [get_pins -quiet -leaf -of_objects $cr_segments -filter {DIRECTION == OUT}]",
                    f"if {{[llength $cr_drivers] != 1}} {{ error {{{pin} old driver is not unique}} }}",
                    "foreach cr_driver $cr_drivers {",
                    "  set cr_driver_net [cr_one [get_nets -quiet -of_objects $cr_driver] {old semantic driver segment}]",
                    "  disconnect_net -net $cr_driver_net -objects $cr_driver",
                    "}",
                    f"set cr_pin [cr_one [get_pins -quiet {_tcl_braced(pin)}] {_tcl_braced(pin)}]",
                    "connect_net -hierarchical -net $cr_net -objects $cr_pin",
                ])
    cleanup_scopes = sorted({
        instance["cleanup_scope"]
        for instance in bundle["instances"]
        if instance.get("cleanup_scope")
    })
    for scope in cleanup_scopes:
        lines.extend([
            f"set cr_scope_name {_tcl_braced(scope)}",
            f"set cr_scope [cr_one [get_cells -quiet -hierarchical -filter "
            f"{{NAME == {_tcl_braced(scope)}}}] {_tcl_braced(scope)}]",
            "set cr_scope_outputs [get_pins -quiet -of_objects $cr_scope -filter {DIRECTION == OUT}]",
            "if {![llength $cr_scope_outputs]} { error {cleanup scope has no outputs} }",
            f"set cr_scope_cells [get_cells -quiet -hierarchical -filter "
            f"{{NAME =~ {_tcl_braced(scope + '/*')} && IS_PRIMITIVE}}]",
            "set cr_live [all_fanin -flat -only_cells -to $cr_scope_outputs]",
            "set cr_live_names [dict create]",
            "set cr_frontier_names {}",
            "foreach cr_cell $cr_live {",
            "  set cr_cell_name [get_property NAME $cr_cell]",
            "  if {![string match \"${cr_scope_name}/*\" $cr_cell_name]} { continue }",
            "  dict set cr_live_names $cr_cell_name 1",
            "  if {[get_property IS_SEQUENTIAL $cr_cell]} {",
            "    lappend cr_frontier_names $cr_cell_name",
            "  }",
            "}",
            "set cr_seen_seq [dict create]",
            "while {[llength $cr_frontier_names]} {",
            "  set cr_frontier [get_cells -quiet -hierarchical $cr_frontier_names]",
            "  set cr_next_names {}",
            "  foreach cr_seq $cr_frontier {",
            "    set cr_seq_name [get_property NAME $cr_seq]",
            "    dict set cr_seen_seq $cr_seq_name 1",
            "  }",
            "  set cr_state_inputs [get_pins -quiet -of_objects $cr_frontier -filter {DIRECTION == IN && REF_PIN_NAME != C && REF_PIN_NAME != CLK}]",
            "  if {![llength $cr_state_inputs]} { break }",
            "    set cr_state_cone [all_fanin -flat -only_cells -to $cr_state_inputs]",
            "    foreach cr_state_cell $cr_state_cone {",
            "      set cr_state_name [get_property NAME $cr_state_cell]",
            "      if {![string match \"${cr_scope_name}/*\" $cr_state_name]} { continue }",
            "      dict set cr_live_names $cr_state_name 1",
            "      if {[get_property IS_SEQUENTIAL $cr_state_cell] && ![dict exists $cr_seen_seq $cr_state_name]} {",
            "        lappend cr_next_names $cr_state_name",
            "      }",
            "    }",
            "  set cr_frontier_names [lsort -unique $cr_next_names]",
            "}",
            "set cr_dead_names {}",
            "foreach cr_cell $cr_scope_cells {",
            "  if {![dict exists $cr_live_names [get_property NAME $cr_cell]]} {",
            "    lappend cr_dead_names [get_property NAME $cr_cell]",
            "  }",
            "}",
            "if {[llength $cr_dead_names]} {",
            "  set cr_dead [get_cells -quiet -hierarchical $cr_dead_names]",
            "  catch {set_property DONT_TOUCH false $cr_dead}",
            "  remove_cell $cr_dead",
            "}",
            "if {[dict size $cr_live_names] == 0} {",
            "  catch {set_property DONT_TOUCH false $cr_scope}",
            "  remove_cell $cr_scope",
            f"  if {{[llength [get_cells -quiet -hierarchical -filter "
            f"{{NAME == {_tcl_braced(scope)} || NAME =~ {_tcl_braced(scope + '/*')}}}]]}} {{",
            "    error {complete consumer cleanup left the old hierarchy alive}",
            "  }",
            "}",
            f"puts \"CONSUMER_REWRITE_CLEANUP scope={scope} "
            "seed_cells=[llength $cr_live] "
            "removed=[llength $cr_dead_names] retained=[dict size $cr_live_names]\"",
        ])
    lines.extend([
        "write_checkpoint -force [lindex $argv 1]",
        "puts CONSUMER_REWRITE_WIRING_DONE",
        "close_design",
        "exit",
        "",
    ])
    return "\n".join(lines)


__all__ = [
    "render_consumer_rewrite_connect_tcl",
    "render_consumer_rewrite_verilog",
    "validate_consumer_rewrite_bundle",
    "validate_consumer_rewrite_plan",
]
