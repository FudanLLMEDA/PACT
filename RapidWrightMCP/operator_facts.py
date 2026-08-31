"""Exact connectivity recovery for restricted DSP recurrence boundaries."""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from typing import Any, Optional


FACTS_VERSION = 2
_VECTOR_PIN_RE = re.compile(r"^(?P<name>[A-Z_]+)\[(?P<index>\d+)\]$")
_SUPPORTED_FFS = {"FDRE"}


class OperatorFactsError(ValueError):
    """Raised when exact recurrence facts are missing or ambiguous."""


def _as_list(value) -> list:
    return [] if value is None else list(value)


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


def _parent_net(netlist, pin):
    hierarchical = pin.getHierarchicalNet()
    if hierarchical is None:
        raise OperatorFactsError(f"pin {pin} has no hierarchical net")
    parent = netlist.getParentNet(hierarchical)
    if parent is None:
        raise OperatorFactsError(f"pin {pin} has no canonical parent net")
    return parent


def _net_name(net) -> str:
    return str(net.getHierarchicalNetName())


def _unique_source(net, context: str):
    sources = _as_list(net.getLeafHierPortInsts(True, False, True))
    if len(sources) != 1:
        raise OperatorFactsError(f"{context} has {len(sources)} leaf drivers")
    return sources[0]


def _pin_name(pin) -> str:
    return str(pin.getPortInst().getName())


def _cell_name(pin) -> str:
    return str(pin.getFullHierarchicalInstName())


def _cell_type(pin) -> str:
    cell_type = pin.getCellType()
    return str(cell_type.getName()).upper() if cell_type is not None else ""


def _instance_properties(pin) -> dict[str, str]:
    instance = pin.getPortInst().getCellInst()
    properties = instance.getPropertiesMap()
    result = {}
    try:
        entries = properties.entrySet()
        for entry in entries:
            value = entry.getValue()
            result[str(entry.getKey())] = str(value.getValue())
    except AttributeError:
        for key, value in dict(properties).items():
            raw = value.getValue() if hasattr(value, "getValue") else value
            result[str(key)] = str(raw)
    return dict(sorted(result.items()))


def _physical_cell_fact(design, name: str) -> dict[str, Any]:
    cell = design.getCell(name)
    if cell is None or not bool(cell.isPlaced()):
        raise OperatorFactsError(f"logical cell is absent or unplaced: {name}")
    site = cell.getSite()
    return {
        "cell": name,
        "type": str(cell.getType()).upper(),
        "site": str(site.getName()) if site is not None else "",
        "bel": str(cell.getBELName()),
    }


def _hier_pin(netlist, name: str):
    pin = netlist.getHierPortInstFromName(name)
    if pin is None:
        raise OperatorFactsError(f"required logical pin is missing: {name}")
    return pin


def _dsp_child_properties(netlist, dsp_name: str) -> dict[str, Any]:
    result = {}
    for child_name in (
        "DSP_C_DATA_INST", "DSP_ALU_INST", "DSP_OUTPUT_INST",
        "DSP_MULTIPLIER_INST", "DSP_A_B_DATA_INST", "DSP_M_DATA_INST",
    ):
        full_name = f"{dsp_name}/{child_name}"
        child = netlist.getHierCellInstFromName(full_name)
        if child is None:
            continue
        properties = child.getInst().getPropertiesMap()
        normalized = {}
        try:
            entries = properties.entrySet()
            for entry in entries:
                value = entry.getValue()
                normalized[str(entry.getKey())] = str(value.getValue())
        except AttributeError:
            for key, value in dict(properties).items():
                raw = value.getValue() if hasattr(value, "getValue") else value
                normalized[str(key)] = str(raw)
        result[child_name] = {
            "type": str(child.getCellType().getName()).upper(),
            "properties": dict(sorted(normalized.items())),
        }
    return result


def _logical_cell_properties(netlist, cell_name: str) -> dict[str, str]:
    cell = netlist.getCellInstFromHierName(cell_name)
    if cell is None:
        raise OperatorFactsError(f"logical cell instance is absent: {cell_name}")
    result = {}
    properties = cell.getPropertiesMap()
    try:
        entries = properties.entrySet()
        for entry in entries:
            value = entry.getValue()
            result[str(entry.getKey())] = str(value.getValue())
    except AttributeError:
        for key, value in dict(properties).items():
            raw = value.getValue() if hasattr(value, "getValue") else value
            result[str(key)] = str(raw)
    return result


def _property_bit(value: Any, key: str) -> int:
    raw = str(value).strip().lower()
    if raw in {"0", "0.0", "false", "1'b0", "1'h0", "1'd0", "32'd0"}:
        return 0
    if raw in {"1", "1.0", "true", "1'b1", "1'h1", "1'd1", "32'd1"}:
        return 1
    raise OperatorFactsError(f"{key} is not a one-bit property")


def _property_bool(properties: dict[str, str], key: str) -> bool:
    raw = str(properties.get(key, "0")).strip().lower()
    if raw in {"0", "false", "1'b0"}:
        return False
    if raw in {"1", "true", "1'b1"}:
        return True
    raise OperatorFactsError(f"{key} has a non-boolean value")


def _prove_recursive_lut_equivalence(
    design,
    reference_facts: dict[str, Any],
    candidate_facts: dict[str, Any],
    *,
    depth: int,
    visited: set[tuple[str, str]],
    memo: dict[tuple[str, str], dict[str, Any]],
    facts_cache: dict[str, dict[str, Any]],
) -> Optional[dict[str, Any]]:
    if (
        reference_facts.get("kind") != "lut"
        or candidate_facts.get("kind") != "lut"
        or reference_facts.get("primitive") != candidate_facts.get("primitive")
    ):
        return None
    reference_inputs = sorted((reference_facts.get("inputs") or {}).items())
    candidate_inputs = sorted((candidate_facts.get("inputs") or {}).items())
    if not reference_inputs or len(reference_inputs) != len(candidate_inputs):
        return None
    netlist = design.getNetlist()
    reference_pins = [
        _hier_pin(netlist, f"{reference_facts['cell']}/{name}")
        for name, _fact in reference_inputs
    ]
    candidate_pins = [
        _hier_pin(netlist, f"{candidate_facts['cell']}/{name}")
        for name, _fact in candidate_inputs
    ]
    compatible: dict[tuple[int, int], dict[str, Any]] = {}
    for reference_index, reference_pin in enumerate(reference_pins):
        for candidate_index, candidate_pin in enumerate(candidate_pins):
            try:
                compatible[(reference_index, candidate_index)] = (
                    _prove_control_pin_equivalence(
                        design,
                        reference_pin,
                        candidate_pin,
                        depth=depth + 1,
                        visited=visited,
                        memo=memo,
                        facts_cache=facts_cache,
                    )
                )
            except OperatorFactsError:
                continue

    for permutation in itertools.permutations(range(len(candidate_inputs))):
        if any(
            (reference_index, permutation[reference_index]) not in compatible
            for reference_index in range(len(reference_inputs))
        ):
            continue
        reference_truth = int(reference_facts["truth_table_hex"], 16)
        candidate_truth = int(candidate_facts["truth_table_hex"], 16)
        equivalent = True
        for assignment in range(1 << len(reference_inputs)):
            reference_index = 0
            candidate_index = 0
            for index, (_name, reference_input) in enumerate(reference_inputs):
                bit = (assignment >> index) & 1
                reference_index |= (
                    bit ^ int(bool(reference_input.get("inverted")))
                ) << index
                candidate_position = permutation[index]
                candidate_input = candidate_inputs[candidate_position][1]
                candidate_index |= (
                    bit ^ int(bool(candidate_input.get("inverted")))
                ) << candidate_position
            if ((reference_truth >> reference_index) & 1) != (
                (candidate_truth >> candidate_index) & 1
            ):
                equivalent = False
                break
        if equivalent:
            return {
                "method": "recursive_lut_truth_table_with_proved_input_bijection",
                "assignments_checked": 1 << len(reference_inputs),
                "input_proofs": [
                    {
                        "reference_pin": reference_inputs[index][0],
                        "candidate_pin": candidate_inputs[permutation[index]][0],
                        "proof": compatible[(index, permutation[index])],
                    }
                    for index in range(len(reference_inputs))
                ],
            }
    return None


def _prove_control_pin_equivalence(
    design,
    reference_pin,
    candidate_pin,
    *,
    depth: int = 0,
    visited: Optional[set[tuple[str, str]]] = None,
    memo: Optional[dict[tuple[str, str], dict[str, Any]]] = None,
    facts_cache: Optional[dict[str, dict[str, Any]]] = None,
) -> dict[str, Any]:
    if depth > 4:
        raise OperatorFactsError("control equivalence recursion depth exceeds bound")
    if visited is None:
        visited = set()
    if memo is None:
        memo = {}
    if facts_cache is None:
        facts_cache = {}
    identity = (str(reference_pin), str(candidate_pin))
    if identity in memo:
        return memo[identity]
    if identity in visited:
        raise OperatorFactsError("control equivalence graph contains a cycle")
    visited = set(visited)
    visited.add(identity)
    netlist = design.getNetlist()
    reference_parent = _parent_net(netlist, reference_pin)
    candidate_parent = _parent_net(netlist, candidate_pin)
    reference_source = _unique_source(reference_parent, str(reference_pin))
    candidate_source = _unique_source(candidate_parent, str(candidate_pin))
    base = {
        "reference_net": _net_name(reference_parent),
        "candidate_net": _net_name(candidate_parent),
        "reference_driver": str(reference_source),
        "candidate_driver": str(candidate_source),
    }
    if reference_parent == candidate_parent:
        proof = {
            **base,
            "status": "proved",
            "method": "identical_canonical_parent_net",
        }
        memo[identity] = proof
        return proof
    try:
        from .rapidwright_tools import (
            _prove_equivalent_source_facts,
            _source_cell_facts,
        )
    except ImportError:
        from rapidwright_tools import (
            _prove_equivalent_source_facts,
            _source_cell_facts,
        )
    try:
        reference_source_name = str(reference_source)
        candidate_source_name = str(candidate_source)
        reference_facts = facts_cache.get(reference_source_name)
        if reference_facts is None:
            reference_facts = _source_cell_facts(design, reference_source)
            facts_cache[reference_source_name] = reference_facts
        candidate_facts = facts_cache.get(candidate_source_name)
        if candidate_facts is None:
            candidate_facts = _source_cell_facts(design, candidate_source)
            facts_cache[candidate_source_name] = candidate_facts
        proved, equivalence = _prove_equivalent_source_facts(
            reference_facts, candidate_facts
        )
    except Exception as exc:
        raise OperatorFactsError(
            "control source equivalence extraction failed "
            f"({reference_source} vs {candidate_source}): {exc}"
        ) from exc
    if not proved:
        recursive = _prove_recursive_lut_equivalence(
            design,
            reference_facts,
            candidate_facts,
            depth=depth,
            visited=visited,
            memo=memo,
            facts_cache=facts_cache,
        )
        if recursive is not None:
            proved = True
            equivalence = recursive
    if not proved:
        raise OperatorFactsError(
            "control source transition relations differ "
            f"({reference_source} vs {candidate_source}): "
            + json.dumps({
                "failure": equivalence,
                "reference": reference_facts,
                "candidate": candidate_facts,
            }, sort_keys=True, separators=(",", ":"))
        )
    proof = {
        **base,
        "status": "proved",
        "method": equivalence["method"],
        "reference_source_facts": reference_facts,
        "candidate_source_facts": candidate_facts,
        "equivalence": equivalence,
    }
    memo[identity] = proof
    return proof


def _recover_control_equivalence_group(
    design,
    role: str,
    indexed_pins: list[tuple[int, Any]],
) -> dict[str, Any]:
    if not indexed_pins or [index for index, _pin in indexed_pins] != list(
        range(len(indexed_pins))
    ):
        raise OperatorFactsError(f"{role} control group does not cover every relay bit")
    reference = indexed_pins[0][1]
    members = []
    comparisons = []
    for bit, pin in indexed_pins:
        parent = _parent_net(design.getNetlist(), pin)
        source = _unique_source(parent, str(pin))
        members.append({
            "bit": bit,
            "pin": str(pin),
            "canonical_net": _net_name(parent),
            "driver_pin": str(source),
        })
        try:
            comparison = _prove_control_pin_equivalence(design, reference, pin)
        except OperatorFactsError as exc:
            raise OperatorFactsError(f"{role} bit {bit}: {exc}") from exc
        comparison["bit"] = bit
        comparisons.append(comparison)
    group = {
        "role": role,
        "representative_bit": 0,
        "representative_net": members[0]["canonical_net"],
        "members": members,
        "comparisons": comparisons,
        "status": "proved",
        "complete_bit_coverage": True,
    }
    group["group_sha256"] = _digest(group)
    return group


def recover_control_pin_equivalence_groups(
    design,
    pin_names: list[str],
    *,
    role: str,
) -> dict[str, Any]:
    """Partition physical control nets only by exact source equivalence.

    Physical optimization may replicate a scalar CE/reset source under several
    net names.  Unproved comparisons remain separate groups; this routine never
    relaxes a schedule based on naming similarity.
    """
    if (
        not isinstance(role, str)
        or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", role) is None
    ):
        raise OperatorFactsError("control equivalence role is invalid")
    if (
        not isinstance(pin_names, list)
        or not pin_names
        or len(pin_names) > 256
        or any(
            not isinstance(name, str) or not name or len(name) > 2048
            for name in pin_names
        )
    ):
        raise OperatorFactsError("control equivalence pins are invalid")

    netlist = design.getNetlist()
    records = []
    seen_nets = set()
    for name in pin_names:
        pin = _hier_pin(netlist, name)
        parent = _parent_net(netlist, pin)
        canonical_net = _net_name(parent)
        seen_nets.add(canonical_net)
        source = _unique_source(parent, name)
        records.append({
            "pin_name": name,
            "pin": pin,
            "canonical_net": canonical_net,
            "driver_pin": str(source),
        })
    if not records:
        raise OperatorFactsError("control equivalence pins resolve to no nets")

    groups: list[dict[str, Any]] = []
    rejections: list[dict[str, str]] = []
    for record in records:
        for group in groups:
            # Several exact sink pins can resolve to the same physical net.
            # Retain every pin as an identity member so complete_pin_coverage
            # describes the requested pin set, not merely unique net names.
            if any(
                member["canonical_net"] == record["canonical_net"]
                for member in group["members"]
            ):
                group["members"].append({
                    "pin_name": record["pin_name"],
                    "canonical_net": record["canonical_net"],
                    "driver_pin": record["driver_pin"],
                })
                break
            try:
                proof = _prove_control_pin_equivalence(
                    design, group["reference_pin"], record["pin"]
                )
            except OperatorFactsError as exc:
                rejections.append({
                    "representative_net": str(group["representative_net"]),
                    "candidate_net": str(record["canonical_net"]),
                    "reason": str(exc),
                })
                continue
            group["members"].append({
                "pin_name": record["pin_name"],
                "canonical_net": record["canonical_net"],
                "driver_pin": record["driver_pin"],
            })
            group["comparisons"].append(proof)
            break
        else:
            groups.append({
                "representative_net": record["canonical_net"],
                "reference_pin": record["pin"],
                "members": [{
                    "pin_name": record["pin_name"],
                    "canonical_net": record["canonical_net"],
                    "driver_pin": record["driver_pin"],
                }],
                "comparisons": [],
            })

    aliases = {}
    public_groups = []
    for ordinal, group in enumerate(groups):
        representative = str(group["representative_net"])
        members = list(group["members"])
        for member in members:
            aliases[str(member["canonical_net"])] = representative
        public_group = {
            "group_ordinal": ordinal,
            "representative_net": representative,
            "members": members,
            "comparisons": list(group["comparisons"]),
            "status": "proved",
        }
        public_group["group_sha256"] = _digest(public_group)
        public_groups.append(public_group)
    return {
        "status": "success",
        "kind": "proved_control_pin_equivalence_groups",
        "version": 1,
        "role": role,
        "complete_pin_coverage": True,
        "input_pin_count": len(pin_names),
        "unique_net_count": len(seen_nets),
        "group_count": len(public_groups),
        "aliased_net_count": sum(
            max(0, len({
                member["canonical_net"] for member in group["members"]
            }) - 1)
            for group in public_groups
        ),
        "net_aliases": aliases,
        "groups": public_groups,
        "rejections": rejections,
    }


def recover_source_preg_control_contract(
    design,
    boundary_facts: dict[str, Any],
) -> dict[str, Any]:
    """Prove that enabling source PREG exactly replaces the external relay FF."""
    netlist = design.getNetlist()
    source_dsp = str(boundary_facts.get("source_dsp") or "")
    state_width = boundary_facts.get("state_width")
    dsp_width = boundary_facts.get("dsp_c_width")
    controls = boundary_facts.get("controls")
    relay_bits = boundary_facts.get("relay_bits")
    if (
        not source_dsp
        or not isinstance(state_width, int)
        or not isinstance(dsp_width, int)
        or not isinstance(controls, dict)
        or not isinstance(relay_bits, list)
        or len(relay_bits) != state_width
    ):
        raise OperatorFactsError("boundary facts cannot prove a source PREG replacement")

    output_name = f"{source_dsp}/DSP_OUTPUT_INST"
    output = netlist.getHierCellInstFromName(output_name)
    if output is None or str(output.getCellType().getName()).upper() != "DSP_OUTPUT":
        raise OperatorFactsError("source DSP_OUTPUT instance is absent or has the wrong type")
    properties = _dsp_child_properties(netlist, source_dsp).get(
        "DSP_OUTPUT_INST", {}
    ).get("properties", {})
    if "PREG" not in properties:
        raise OperatorFactsError("source DSP PREG property is absent")
    implementation_preg = _property_bit(properties["PREG"], "PREG")
    macro_properties = _logical_cell_properties(netlist, source_dsp)
    if "PREG" not in macro_properties:
        raise OperatorFactsError("source DSP logical PREG property is absent")
    current_preg = _property_bit(macro_properties["PREG"], "logical PREG")

    control_groups = boundary_facts.get("control_equivalence_groups")
    endpoint = boundary_facts.get("relay_endpoint_contract")
    if not isinstance(control_groups, dict) or set(control_groups) != {"C", "CE", "R"}:
        raise OperatorFactsError("boundary facts omit proved relay control groups")
    if not isinstance(endpoint, dict) or endpoint.get("status") != "proved":
        raise OperatorFactsError("boundary facts omit the relay endpoint contract")
    pin_contract = {
        "CLK": "C",
        "CEP": "CE",
        "RSTP": "R",
    }
    control_pins = {}
    for pin_name, group_name in pin_contract.items():
        pin = _hier_pin(
            netlist,
            f"{source_dsp}/{pin_name}"
            if current_preg == 0
            else f"{output_name}/{pin_name}",
        )
        parent = _parent_net(netlist, pin)
        group = control_groups[group_name]
        claimed_group_digest = group.get("group_sha256")
        group_payload = dict(group)
        group_payload.pop("group_sha256", None)
        if (
            group.get("status") != "proved"
            or group.get("complete_bit_coverage") is not True
            or not isinstance(claimed_group_digest, str)
            or _digest(group_payload) != claimed_group_digest
        ):
            raise OperatorFactsError(f"relay {group_name} control group digest is invalid")
        control_record = {
            "pin": str(pin),
            "net": _net_name(parent),
            "relay_group": group_name,
            "relay_group_sha256": claimed_group_digest,
        }
        source = _unique_source(parent, str(pin))
        control_record["driver_pin"] = str(source)
        if current_preg == 0:
            control_record.update({
                "relationship_to_relay": "rewire_to_relay_for_preg_enable",
                "target_relay_pin": str(group["members"][0]["pin"]),
            })
        else:
            control_record["relationship_to_relay"] = (
                "independent_preserved_source_stage"
            )
        control_pins[pin_name] = control_record

    dsp_inversions = {
        "clock_inverted": _property_bool(macro_properties, "IS_CLK_INVERTED"),
        "ce_inverted": _property_bool(macro_properties, "IS_CEP_INVERTED"),
        "reset_inverted": _property_bool(macro_properties, "IS_RSTP_INVERTED"),
    }
    if current_preg == 0 and dsp_inversions != endpoint.get("inversions"):
        raise OperatorFactsError("source DSP control polarity differs from relay FFs")

    p_fanout = []
    for bit in range(dsp_width):
        pin = _hier_pin(netlist, f"{output_name}/P[{bit}]")
        hierarchical = pin.getHierarchicalNet()
        sinks = []
        net_name = None
        if hierarchical is not None:
            parent = netlist.getParentNet(hierarchical)
            if parent is None:
                raise OperatorFactsError(f"source P[{bit}] has no canonical parent net")
            source = _unique_source(parent, f"{output_name}/P[{bit}]")
            if str(source) != str(pin):
                raise OperatorFactsError(f"source P[{bit}] driver identity is ambiguous")
            sinks = sorted(
                str(item)
                for item in _as_list(parent.getLeafHierPortInsts(False, True, True))
            )
            net_name = _net_name(parent)
        expected = [f"{relay_bits[bit]['ff']['cell']}/D"] if bit < state_width else []
        if sinks != expected:
            raise OperatorFactsError(
                f"source P[{bit}] has side fanout or does not exclusively drive the relay"
            )
        p_fanout.append({"bit": bit, "net": net_name, "sinks": sinks})

    pcout_fanout = []
    for bit in range(dsp_width):
        pin = _hier_pin(netlist, f"{output_name}/PCOUT[{bit}]")
        hierarchical = pin.getHierarchicalNet()
        sinks = []
        net_name = None
        if hierarchical is not None:
            parent = netlist.getParentNet(hierarchical)
            if parent is None:
                raise OperatorFactsError(f"source PCOUT[{bit}] has no canonical parent net")
            source = _unique_source(parent, f"{output_name}/PCOUT[{bit}]")
            if str(source) != str(pin):
                raise OperatorFactsError(f"source PCOUT[{bit}] driver identity is ambiguous")
            sinks = sorted(
                str(item)
                for item in _as_list(parent.getLeafHierPortInsts(False, True, True))
            )
            net_name = _net_name(parent)
        if sinks:
            raise OperatorFactsError(
                f"enabling source PREG would alter connected PCOUT[{bit}] fanout"
            )
        pcout_fanout.append({"bit": bit, "net": net_name, "sinks": sinks})

    contract = {
        "kind": "source_dsp_preg_control_contract",
        "version": FACTS_VERSION,
        "source_dsp": source_dsp,
        "dsp_output": output_name,
        "current_preg": current_preg,
        "implementation_preg": implementation_preg,
        "target_preg": 1,
        "preg_action": "enable" if current_preg == 0 else "preserve",
        "relay_stage_accounting": (
            "replaced_by_source_preg"
            if current_preg == 0
            else "absorbed_into_srl_transport"
        ),
        "controls": control_pins,
        "inversions": dsp_inversions,
        "p_output_fanout": p_fanout,
        "pcout_output_fanout": pcout_fanout,
        "exclusive_external_relay_replacement": True,
    }
    contract["contract_sha256"] = _digest(contract)
    return contract


def _vector_input_pins(hier_cell, vector_name: str) -> list[tuple[int, Any]]:
    pins = []
    for pin in _as_list(hier_cell.getHierPortInsts()):
        match = _VECTOR_PIN_RE.fullmatch(_pin_name(pin).upper())
        if match is None or match.group("name") != vector_name:
            continue
        if not bool(pin.isInput()):
            continue
        pins.append((int(match.group("index")), pin))
    pins.sort(key=lambda item: item[0])
    if not pins or [index for index, _ in pins] != list(range(len(pins))):
        raise OperatorFactsError(f"{vector_name} input pins are absent or non-contiguous")
    return pins


def _source_output_bit(source_pin) -> tuple[str, int]:
    match = _VECTOR_PIN_RE.fullmatch(_pin_name(source_pin).upper())
    if match is None or match.group("name") != "P":
        raise OperatorFactsError(f"relay D source is not a DSP P bit: {source_pin}")
    cell = _cell_name(source_pin)
    suffix = "/DSP_OUTPUT_INST"
    if not cell.endswith(suffix):
        raise OperatorFactsError(f"relay D source is not DSP_OUTPUT: {source_pin}")
    return cell[:-len(suffix)], int(match.group("index"))


def recover_dsp_recurrence_contract(
    design,
    sink_dsp: str,
    *,
    design_sha256: Optional[str] = None,
) -> dict[str, Any]:
    """Recover a variable-width FF relay feeding one DSP C input exactly."""
    if not isinstance(sink_dsp, str) or not sink_dsp or len(sink_dsp) > 1024:
        raise OperatorFactsError("sink_dsp is invalid")
    netlist = design.getNetlist()
    c_data_name = f"{sink_dsp}/DSP_C_DATA_INST"
    c_data = netlist.getHierCellInstFromName(c_data_name)
    if c_data is None or str(c_data.getCellType().getName()).upper() != "DSP_C_DATA":
        raise OperatorFactsError("sink DSP_C_DATA instance is absent or has the wrong type")
    c_pins = _vector_input_pins(c_data, "C")

    c_sources = []
    for c_index, c_pin in c_pins:
        parent = _parent_net(netlist, c_pin)
        source = _unique_source(parent, f"{c_data_name}/C[{c_index}]")
        if _pin_name(source).upper() != "Q" or _cell_type(source) not in _SUPPORTED_FFS:
            raise OperatorFactsError(
                f"C[{c_index}] is not driven by one supported FF Q"
            )
        c_sources.append({
            "c_index": c_index,
            "c_net": _net_name(parent),
            "ff": _cell_name(source),
            "ff_type": _cell_type(source),
            "q_pin": str(source),
        })

    unique_prefix = []
    seen = set()
    extension_started = False
    for item in c_sources:
        source_name = item["ff"]
        if source_name in seen:
            extension_started = True
        elif extension_started:
            raise OperatorFactsError("DSP C input has a non-prefix repeated-source mapping")
        else:
            seen.add(source_name)
            unique_prefix.append(source_name)
    state_width = len(unique_prefix)
    if state_width < 2:
        raise OperatorFactsError("recovered recurrence state is narrower than two bits")
    sign_source = unique_prefix[-1]
    if any(item["ff"] != sign_source for item in c_sources[state_width:]):
        raise OperatorFactsError("upper DSP C bits are not one sign-extension source")

    relay_control_pins = {"C": [], "CE": [], "R": []}
    relay_endpoint_contract = None
    source_dsp = None
    relay_bits = []
    for bit_index, ff_name in enumerate(unique_prefix):
        if c_sources[bit_index]["ff"] != ff_name:
            raise OperatorFactsError("DSP C bit order is not a unique relay prefix")
        pins = {
            name: _hier_pin(netlist, f"{ff_name}/{name}")
            for name in ("D", "Q", "C", "CE", "R")
        }
        d_net = _parent_net(netlist, pins["D"])
        d_source = _unique_source(d_net, f"{ff_name}/D")
        bit_source_dsp, source_bit = _source_output_bit(d_source)
        if source_bit != bit_index:
            raise OperatorFactsError(
                f"relay bit {bit_index} is driven by source P[{source_bit}]"
            )
        if source_dsp is None:
            source_dsp = bit_source_dsp
        elif source_dsp != bit_source_dsp:
            raise OperatorFactsError("relay D bits do not share one source DSP")

        for control_name in relay_control_pins:
            relay_control_pins[control_name].append((bit_index, pins[control_name]))
        properties = _instance_properties(pins["Q"])
        endpoint = {
            "primitive": _cell_type(pins["Q"]),
            "init": 0,
            "inversions": {
                "clock_inverted": _property_bool(properties, "IS_C_INVERTED"),
                "ce_inverted": _property_bool(properties, "IS_CE_INVERTED"),
                "reset_inverted": _property_bool(properties, "IS_R_INVERTED"),
            },
        }
        if relay_endpoint_contract is None:
            relay_endpoint_contract = endpoint
        elif endpoint != relay_endpoint_contract:
            raise OperatorFactsError("relay FF endpoint contracts are not identical")

        q_net = _parent_net(netlist, pins["Q"])
        q_sinks = sorted(
            str(pin) for pin in _as_list(q_net.getLeafHierPortInsts(False, True, True))
        )
        expected_sinks = [f"{c_data_name}/C[{bit_index}]"]
        if bit_index == state_width - 1:
            expected_sinks.extend(
                f"{c_data_name}/C[{index}]"
                for index in range(state_width, len(c_sources))
            )
        if q_sinks != sorted(expected_sinks):
            raise OperatorFactsError(
                f"relay Q bit {bit_index} has side fanout or missing C sinks"
            )
        init = properties.get("INIT")
        if init is not None and str(init).lower() not in {"0", "1'b0", "1'h0", "false"}:
            raise OperatorFactsError(f"relay FF {ff_name} has nonzero INIT")
        relay_bits.append({
            "index": bit_index,
            "ff": _physical_cell_fact(design, ff_name),
            "d_net": _net_name(d_net),
            "d_driver_pin": str(d_source),
            "q_net": _net_name(q_net),
            "q_sink_pins": q_sinks,
            "properties": properties,
        })

    if source_dsp is None or source_dsp == sink_dsp:
        raise OperatorFactsError("source and sink DSP stages are absent or identical")
    control_equivalence_groups = {
        name: _recover_control_equivalence_group(design, role, relay_control_pins[name])
        for name, role in (
            ("C", "relay_clock"), ("CE", "relay_enable"), ("R", "relay_reset")
        )
    }
    relay_endpoint_contract = {
        **(relay_endpoint_contract or {}),
        "status": "proved",
    }
    controls = {
        "clock_net": control_equivalence_groups["C"]["representative_net"],
        "ce_net": control_equivalence_groups["CE"]["representative_net"],
        "reset_net": control_equivalence_groups["R"]["representative_net"],
    }
    facts = {
        "status": "success",
        "kind": "dsp_recurrence_contract_facts",
        "version": FACTS_VERSION,
        "source_dsp": source_dsp,
        "sink_dsp": sink_dsp,
        "state_width": state_width,
        "dsp_c_width": len(c_sources),
        "extension": "sign" if state_width < len(c_sources) else "none",
        "controls": controls,
        "controls_semantics": "representative_nets_with_proved_per_bit_equivalence",
        "control_equivalence_groups": control_equivalence_groups,
        "relay_endpoint_contract": relay_endpoint_contract,
        "relay_bits": relay_bits,
        "source_dsp_children": _dsp_child_properties(netlist, source_dsp),
        "sink_dsp_children": _dsp_child_properties(netlist, sink_dsp),
        "proof_status": "extracted_unproved",
        "boundary_extraction_complete": True,
        "proof_input_eligible": True,
        "mutation_eligible": False,
        "missing_rewrite_facts": [
            "bypass_stage_manifest",
            "transport_valid_transactions",
            "reinjection_dsp_and_pins",
            "emitted_transport_topology",
            "clock_footprint_delta",
        ],
    }
    if design_sha256 is not None:
        facts["design_sha256"] = design_sha256
    facts["region_sha256"] = _digest(facts)
    return facts


def _dsp_clock_region(design, dsp_name: str) -> str:
    cell = design.getCell(f"{dsp_name}/DSP_ALU_INST")
    if cell is None or not bool(cell.isPlaced()) or cell.getSite() is None:
        raise OperatorFactsError(f"DSP ALU is absent or unplaced: {dsp_name}")
    region = cell.getSite().getTile().getClockRegion()
    if region is None:
        raise OperatorFactsError(f"DSP clock region is unavailable: {dsp_name}")
    return str(region)


def _next_cascade_dsp(netlist, dsp_name: str, width: int) -> Optional[str]:
    next_dsp = None
    for bit in range(width):
        output = _hier_pin(netlist, f"{dsp_name}/DSP_OUTPUT_INST/PCOUT[{bit}]")
        parent = _parent_net(netlist, output)
        sinks = _as_list(parent.getLeafHierPortInsts(False, True, True))
        matching = [
            pin for pin in sinks
            if _pin_name(pin).upper() == f"PCIN[{bit}]"
            and _cell_name(pin).endswith("/DSP_ALU_INST")
        ]
        if not matching:
            if bit == 0:
                return None
            raise OperatorFactsError("DSP cascade terminates on only part of PCOUT")
        if len(matching) != 1 or len(sinks) != 1:
            raise OperatorFactsError("DSP PCOUT has ambiguous or side-fanout sinks")
        candidate = _cell_name(matching[0]).removesuffix("/DSP_ALU_INST")
        if next_dsp is None:
            next_dsp = candidate
        elif next_dsp != candidate:
            raise OperatorFactsError("DSP PCOUT bits do not share one cascade sink")
    return next_dsp


def _all_vector_inputs_are_static_zero(netlist, cell_name: str, vector: str) -> bool:
    cell = netlist.getHierCellInstFromName(cell_name)
    if cell is None:
        raise OperatorFactsError(f"required DSP child is missing: {cell_name}")
    for _index, pin in _vector_input_pins(cell, vector):
        source = _unique_source(_parent_net(netlist, pin), str(pin))
        if _cell_type(source) != "GND" or _pin_name(source).upper() != "G":
            return False
    return True


def _static_input_bit(netlist, pin, context: str) -> int:
    source = _unique_source(_parent_net(netlist, pin), context)
    source_type = _cell_type(source)
    source_pin = _pin_name(source).upper()
    if source_type == "GND" and source_pin == "G":
        return 0
    if source_type == "VCC" and source_pin == "P":
        return 1
    raise OperatorFactsError(f"{context} is not driven by a static bit")


def _optional_static_vector(netlist, cell_name: str, vector: str) -> Optional[dict[str, Any]]:
    cell = netlist.getHierCellInstFromName(cell_name)
    if cell is None:
        raise OperatorFactsError(f"required DSP child is missing: {cell_name}")
    matching = []
    for pin in _as_list(cell.getHierPortInsts()):
        match = _VECTOR_PIN_RE.fullmatch(_pin_name(pin).upper())
        if match is not None and match.group("name") == vector and bool(pin.isInput()):
            matching.append((int(match.group("index")), pin))
    if not matching:
        return None
    matching.sort(key=lambda item: item[0])
    if [index for index, _pin in matching] != list(range(len(matching))):
        raise OperatorFactsError(f"{cell_name}/{vector} is non-contiguous")
    bits = [
        _static_input_bit(netlist, pin, f"{cell_name}/{vector}[{index}]")
        for index, pin in matching
    ]
    return {
        "width": len(bits),
        "bits_lsb_first": bits,
        "value": sum(bit << index for index, bit in enumerate(bits)),
    }


def recover_dsp_arithmetic_control_contract(design, dsp_name: str) -> dict[str, Any]:
    """Recover static DSP mode controls needed for modular-add reassociation."""
    netlist = design.getNetlist()
    vectors = {}
    for child, names in (
        ("DSP_ALU_INST", ("ALUMODE", "OPMODE", "CARRYINSEL")),
        ("DSP_A_B_DATA_INST", ("INMODE",)),
    ):
        for name in names:
            value = _optional_static_vector(
                netlist, f"{dsp_name}/{child}", name
            )
            if value is not None:
                vectors[name] = value
    carry_pin = netlist.getHierPortInstFromName(f"{dsp_name}/DSP_ALU_INST/CARRYIN")
    carryin = (
        _static_input_bit(netlist, carry_pin, f"{dsp_name}/DSP_ALU_INST/CARRYIN")
        if carry_pin is not None else None
    )
    if "ALUMODE" not in vectors or "OPMODE" not in vectors:
        raise OperatorFactsError("DSP arithmetic mode vectors are incomplete")
    if vectors["ALUMODE"]["value"] != 0 or carryin not in {None, 0}:
        raise OperatorFactsError("DSP stage is not configured for non-subtracting addition")
    contract = {
        "kind": "dsp_arithmetic_control_contract",
        "version": FACTS_VERSION,
        "dsp": dsp_name,
        "vectors": vectors,
        "carryin": carryin,
        "children": _dsp_child_properties(netlist, dsp_name),
        "arithmetic": "modular_add_candidate",
    }
    contract["contract_sha256"] = _digest(contract)
    return contract


def _decode_dsp48e2_opmode(value: int) -> dict[str, int]:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 512:
        raise OperatorFactsError("DSP48E2 OPMODE must be a 9-bit value")
    return {
        "x": value & 0b11,
        "y": (value >> 2) & 0b11,
        "z": (value >> 4) & 0b111,
        "w": (value >> 7) & 0b11,
    }


def recover_all_dsp_recurrence_boundaries(
    design,
    *,
    design_sha256: Optional[str] = None,
    max_boundaries: int = 32,
) -> list[dict[str, Any]]:
    """Scan DSP C-data cells and retain only exact FF/P recurrence boundaries."""
    if (
        isinstance(max_boundaries, bool) or not isinstance(max_boundaries, int)
        or not 1 <= max_boundaries <= 64
    ):
        raise OperatorFactsError("max_boundaries must be 1..64")
    suffix = "/DSP_C_DATA_INST"
    sink_dsps = sorted({
        str(cell.getName())[:-len(suffix)]
        for cell in _as_list(design.getCells())
        if str(cell.getType()).upper() == "DSP_C_DATA"
        and str(cell.getName()).endswith(suffix)
    })
    recovered = []
    for sink_dsp in sink_dsps:
        try:
            recovered.append(recover_dsp_recurrence_contract(
                design, sink_dsp, design_sha256=design_sha256
            ))
        except OperatorFactsError:
            continue
        if len(recovered) > max_boundaries:
            raise OperatorFactsError("exact recurrence boundary count exceeds bound")
    return recovered


def _periodic_recurrence_delay(
    boundaries: list[dict[str, Any]],
    cascade_successor,
) -> tuple[int, int, list[int]]:
    source_to_sink = {
        str(item["source_dsp"]): str(item["sink_dsp"]) for item in boundaries
    }
    if len(source_to_sink) != len(boundaries) or len(boundaries) < 2:
        raise OperatorFactsError("at least two unique recurrence boundaries are required")
    source_set = set(source_to_sink)
    intervals = []
    for start in sorted(source_set):
        current = start
        visited = {current}
        for distance in range(1, 1025):
            next_dsp = (
                source_to_sink[current]
                if current in source_to_sink
                else cascade_successor(current)
            )
            if next_dsp is None:
                break
            if next_dsp in source_set:
                intervals.append(distance)
                break
            if next_dsp in visited:
                raise OperatorFactsError("combined recurrence graph contains a cycle")
            visited.add(next_dsp)
            current = next_dsp
    if not intervals or len(set(intervals)) != 1:
        raise OperatorFactsError("recurrence boundary spacing is absent or non-periodic")
    period = intervals[0]
    if period < 4 or period % 2:
        raise OperatorFactsError("recurrence period cannot form a balanced half cut")
    delay = period // 2 - 1
    return period, delay, sorted(intervals)


def recover_recurrence_rewrite_plan(
    design,
    boundary_facts: dict[str, Any],
    *,
    all_boundaries: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Derive a balanced cut from exact periodic boundaries and cascade topology."""
    if boundary_facts.get("kind") != "dsp_recurrence_contract_facts":
        raise OperatorFactsError("boundary facts have the wrong kind")
    state_width = boundary_facts.get("state_width")
    dsp_width = boundary_facts.get("dsp_c_width")
    if not isinstance(state_width, int) or not isinstance(dsp_width, int):
        raise OperatorFactsError("boundary facts omit state or DSP width")
    sink_dsp = str(boundary_facts.get("sink_dsp") or "")
    netlist = design.getNetlist()
    if all_boundaries is None:
        all_boundaries = recover_all_dsp_recurrence_boundaries(
            design,
            design_sha256=boundary_facts.get("design_sha256"),
        )
    else:
        all_boundaries = list(all_boundaries)
    exact_selected = [
        item for item in all_boundaries
        if item.get("source_dsp") == boundary_facts.get("source_dsp")
        and item.get("sink_dsp") == sink_dsp
        and item.get("region_sha256") == boundary_facts.get("region_sha256")
    ]
    if len(exact_selected) != 1:
        raise OperatorFactsError("selected boundary is absent from exact full-design scan")

    def successor(dsp_name: str) -> Optional[str]:
        return _next_cascade_dsp(netlist, dsp_name, dsp_width)

    recurrence_period, transport_delay, intervals = _periodic_recurrence_delay(
        all_boundaries, successor
    )
    bypass_stages = []
    current = sink_dsp
    seen = set()
    for _ in range(transport_delay):
        if current in seen:
            raise OperatorFactsError("bypass cascade contains a cycle")
        seen.add(current)
        bypass_stages.append(current)
        next_dsp = successor(current)
        if next_dsp is None:
            raise OperatorFactsError("cascade ends before the balanced reinjection point")
        current = next_dsp
    reinjection_dsp = current
    if not _all_vector_inputs_are_static_zero(
        netlist, f"{sink_dsp}/DSP_ALU_INST", "PCIN"
    ):
        raise OperatorFactsError("boundary sink PCIN is not static zero")
    for dsp_name in bypass_stages[1:]:
        if not _all_vector_inputs_are_static_zero(
            netlist, f"{dsp_name}/DSP_C_DATA_INST", "C"
        ):
            raise OperatorFactsError("bypassed DSP C input is not static zero")
    if not _all_vector_inputs_are_static_zero(
        netlist, f"{reinjection_dsp}/DSP_C_DATA_INST", "C"
    ):
        raise OperatorFactsError("reinjection DSP C input is not static zero")

    stage_records = []
    for dsp_name in [*bypass_stages, reinjection_dsp]:
        cell = design.getCell(f"{dsp_name}/DSP_ALU_INST")
        stage_records.append({
            "dsp": dsp_name,
            "site": str(cell.getSiteName()),
            "clock_region": _dsp_clock_region(design, dsp_name),
            "children": _dsp_child_properties(netlist, dsp_name),
            "arithmetic_control_contract": recover_dsp_arithmetic_control_contract(
                design, dsp_name
            ),
        })
    contracts = [record["arithmetic_control_contract"] for record in stage_records]
    reference_children = contracts[0]["children"]
    reference_vectors = {
        key: value for key, value in contracts[0]["vectors"].items()
        if key != "OPMODE"
    }
    for contract in contracts[1:]:
        if contract["children"] != reference_children or {
            key: value for key, value in contract["vectors"].items()
            if key != "OPMODE"
        } != reference_vectors or contract["carryin"] != contracts[0]["carryin"]:
            raise OperatorFactsError("DSP pipeline or non-OPMODE controls differ across cut")
    boundary_opmode = contracts[0]["vectors"]["OPMODE"]["value"]
    bypass_opmodes = [
        contract["vectors"]["OPMODE"]["value"] for contract in contracts[1:]
    ]
    if not bypass_opmodes or len(set(bypass_opmodes)) != 1:
        raise OperatorFactsError("bypass DSP OPMODE values are not uniform")
    bypass_opmode = bypass_opmodes[0]
    boundary_muxes = _decode_dsp48e2_opmode(boundary_opmode)
    bypass_muxes = _decode_dsp48e2_opmode(bypass_opmode)
    if boundary_muxes != {"x": 1, "y": 1, "z": 3, "w": 0}:
        raise OperatorFactsError("boundary DSP is not exact M+C modular addition")
    if bypass_muxes != {"x": 1, "y": 1, "z": 1, "w": 0}:
        raise OperatorFactsError("bypass DSP is not exact M+PCIN modular addition")
    target_reinjection_opmode = bypass_opmode | (0b11 << 7)
    reinjection_properties = _logical_cell_properties(netlist, reinjection_dsp)
    if "CREG" not in reinjection_properties:
        raise OperatorFactsError("reinjection DSP logical CREG property is absent")
    current_reinjection_creg = _property_bit(
        reinjection_properties["CREG"], "logical CREG"
    )
    arithmetic_rewrite_contract = {
        "kind": "dsp_recurrence_arithmetic_rewrite_contract",
        "version": FACTS_VERSION,
        "boundary": {
            "dsp": stage_records[0]["dsp"],
            "opmode": boundary_opmode,
            "operation": "M_PLUS_C",
        },
        "bypass": {
            "opmode": bypass_opmode,
            "operation": "M_PLUS_PCIN",
            "stage_count": len(stage_records) - 1,
        },
        "reinjection": {
            "dsp": reinjection_dsp,
            "current_opmode": bypass_opmode,
            "target_opmode": target_reinjection_opmode,
            "target_muxes": _decode_dsp48e2_opmode(target_reinjection_opmode),
            "current_creg": current_reinjection_creg,
            "target_creg": 0,
            "creg_action": (
                "disable" if current_reinjection_creg == 1 else "preserve"
            ),
            "operation": "M_PLUS_PCIN_PLUS_C",
        },
        "arithmetic": "modulo_2_to_state_width",
    }
    arithmetic_rewrite_contract["contract_sha256"] = _digest(
        arithmetic_rewrite_contract
    )
    plan = {
        "kind": "dsp_recurrence_rewrite_plan_facts",
        "version": FACTS_VERSION,
        "state_width": state_width,
        "dsp_width": dsp_width,
        "source_dsp": boundary_facts["source_dsp"],
        "source_dsp_site": str(
            design.getCell(
                f"{boundary_facts['source_dsp']}/DSP_ALU_INST"
            ).getSiteName()
        ),
        "boundary_sink_dsp": sink_dsp,
        "recurrence_boundary_count": len(all_boundaries),
        "recurrence_period_transactions": recurrence_period,
        "observed_boundary_intervals": intervals,
        "bypass_stages": bypass_stages,
        "reinjection_dsp": reinjection_dsp,
        "transport_valid_transactions": len(bypass_stages),
        "boundary_clock_region": _dsp_clock_region(design, sink_dsp),
        "reinjection_clock_region": _dsp_clock_region(design, reinjection_dsp),
        "cascade_stages": stage_records,
        "source_preg_control_contract": recover_source_preg_control_contract(
            design, boundary_facts
        ),
        "reinjection_arithmetic_contract": stage_records[-1][
            "arithmetic_control_contract"
        ],
        "arithmetic_rewrite_contract": arithmetic_rewrite_contract,
        "proof_status": "extracted_unproved",
    }
    plan["plan_sha256"] = _digest(plan)
    return plan


def complete_recurrence_rewrite_facts(
    boundary_facts: dict[str, Any],
    rewrite_plan: dict[str, Any],
) -> dict[str, Any]:
    """Bind boundary and chain recovery into one proof-input region digest."""
    if rewrite_plan.get("state_width") != boundary_facts.get("state_width"):
        raise OperatorFactsError("rewrite plan width does not match boundary facts")
    complete = dict(boundary_facts)
    complete.pop("region_sha256", None)
    complete["rewrite_plan"] = rewrite_plan
    complete["proof_status"] = "rewrite_facts_extracted_unproved"
    complete["missing_rewrite_facts"] = [
        "emitted_transport_topology",
        "srl_placement_and_control_groups",
        "clock_footprint_delta",
        "post_emission_equivalence",
    ]
    complete["region_sha256"] = _digest(complete)
    return complete


def recover_dsp_recurrence_family_contract(
    design,
    *,
    design_sha256: str,
    max_boundaries: int = 32,
) -> dict[str, Any]:
    """Recover one complete periodic recurrence family from the current DCP."""
    boundaries = recover_all_dsp_recurrence_boundaries(
        design,
        design_sha256=design_sha256,
        max_boundaries=max_boundaries,
    )
    if len(boundaries) < 2:
        raise OperatorFactsError("no multi-boundary DSP recurrence family was recovered")
    members = [
        complete_recurrence_rewrite_facts(
            boundary,
            recover_recurrence_rewrite_plan(
                design, boundary, all_boundaries=boundaries
            ),
        )
        for boundary in boundaries
    ]
    widths = {int(member["state_width"]) for member in members}
    dsp_widths = {int(member["dsp_c_width"]) for member in members}
    delays = {
        int(member["rewrite_plan"]["transport_valid_transactions"])
        for member in members
    }
    periods = {
        int(member["rewrite_plan"]["recurrence_period_transactions"])
        for member in members
    }
    if not all(len(values) == 1 for values in (widths, dsp_widths, delays, periods)):
        raise OperatorFactsError("recurrence family members have incompatible contracts")

    relay_cells = [
        str(bit["ff"]["cell"])
        for member in members
        for bit in member["relay_bits"]
    ]
    source_dsps = [str(member["source_dsp"]) for member in members]
    sink_dsps = [str(member["sink_dsp"]) for member in members]
    reinjection_dsps = [
        str(member["rewrite_plan"]["reinjection_dsp"]) for member in members
    ]
    rewrite_windows = [
        set(str(stage) for stage in member["rewrite_plan"]["bypass_stages"])
        | {str(member["rewrite_plan"]["reinjection_dsp"])}
        for member in members
    ]
    windows_disjoint = all(
        rewrite_windows[left].isdisjoint(rewrite_windows[right])
        for left in range(len(rewrite_windows))
        for right in range(left + 1, len(rewrite_windows))
    )
    composition = {
        "unique_relay_cells": len(relay_cells) == len(set(relay_cells)),
        "unique_source_dsps": len(source_dsps) == len(set(source_dsps)),
        "unique_sink_dsps": len(sink_dsps) == len(set(sink_dsps)),
        "unique_reinjection_dsps": len(reinjection_dsps) == len(set(reinjection_dsps)),
        "rewrite_windows_disjoint": windows_disjoint,
    }
    if not all(composition.values()):
        raise OperatorFactsError("recurrence family rewrite footprints overlap")
    composition["contract_sha256"] = _digest(composition)

    ordered = sorted(members, key=lambda member: member["region_sha256"])
    family = {
        "kind": "dsp_recurrence_family_contract_facts",
        "version": 1,
        "status": "success",
        "design_sha256": design_sha256,
        "boundary_count": len(ordered),
        "state_width": next(iter(widths)),
        "dsp_c_width": next(iter(dsp_widths)),
        "recurrence_period_transactions": next(iter(periods)),
        "transport_valid_transactions": next(iter(delays)),
        "boundary_region_sha256s": [member["region_sha256"] for member in ordered],
        "boundaries": ordered,
        "composition_contract": composition,
        "proof_status": "family_rewrite_facts_extracted_unproved",
        "mutation_eligible": False,
    }
    family["family_contract_sha256"] = _digest(family)
    family["family_id"] = "recurrence-family:" + family["family_contract_sha256"][:20]
    return family


def propose_recurrence_placement_candidates(
    design,
    facts: dict[str, Any],
    *,
    max_srl_sites: int = 32,
    max_ce_clone_sites: int = 16,
) -> dict[str, Any]:
    """Rank bounded global sites; Vivado proves exact leaf reuse and legality."""
    for name, value, maximum in (
        ("max_srl_sites", max_srl_sites, 1024),
        ("max_ce_clone_sites", max_ce_clone_sites, 128),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise OperatorFactsError(f"{name} must be in 1..{maximum}")
    rewrite = facts.get("rewrite_plan")
    if not isinstance(rewrite, dict):
        raise OperatorFactsError("complete recurrence facts omit rewrite_plan")
    stages = rewrite.get("cascade_stages")
    if not isinstance(stages, list) or len(stages) < 2:
        raise OperatorFactsError("rewrite plan omits cascade placement")
    anchor_tiles = []
    for dsp_name in (rewrite.get("source_dsp"), stages[-1].get("dsp")):
        if not isinstance(dsp_name, str) or not dsp_name:
            raise OperatorFactsError("recurrence placement anchor is invalid")
        cell = design.getCell(f"{dsp_name}/DSP_ALU_INST")
        if cell is None or not bool(cell.isPlaced()) or cell.getTile() is None:
            raise OperatorFactsError("recurrence placement anchor is absent")
        anchor_tiles.append(cell.getTile())
    regions = {
        str(record.get("clock_region"))
        for record in stages if isinstance(record, dict) and record.get("clock_region")
    }
    if not regions:
        raise OperatorFactsError("incumbent cascade clock regions are absent")

    ranked_srl = []
    ranked_clones = []
    for site in _as_list(design.getDevice().getAllSites()):
        site_type = str(site.getSiteTypeEnum()).upper()
        if site_type not in {"SLICEM", "SLICEL"}:
            continue
        tile = site.getTile()
        region = tile.getClockRegion() if tile is not None else None
        if tile is None or region is None:
            continue
        anchor_distances = tuple(
            int(tile.getManhattanDistance(anchor)) for anchor in anchor_tiles
        )
        score = sum(anchor_distances)
        site_inst = design.getSiteInstFromSite(site)
        site_cells = [] if site_inst is None else _as_list(site_inst.getCells())
        record = {
            "score": score,
            "source_distance": anchor_distances[0],
            "reinjection_distance": anchor_distances[1],
            "site": str(site.getName()),
            "occupied": bool(site_cells),
            "clock_candidate": any(
                str(cell.getType()).upper().startswith(("FD", "SRL"))
                for cell in site_cells
            ),
        }
        if site_type == "SLICEM" and record["clock_candidate"]:
            ranked_srl.append(record)
        elif site_type == "SLICEL":
            ranked_clones.append(record)
    # A preexisting slice clock leaf needs a placed sequential witness. Vivado
    # still proves the exact target net, slice half, and free-BEL state.
    ranked_srl.sort(
        key=lambda item: (
            not item["clock_candidate"],
            not item["occupied"],
            item["score"],
            item["site"],
        )
    )
    ranked_clones.sort(key=lambda item: (item["score"], item["site"]))
    selected_srl = ranked_srl[:max_srl_sites]
    selected_clones = ranked_clones[:max_ce_clone_sites]
    if not selected_srl or not selected_clones:
        raise OperatorFactsError("no nearby recurrence placement candidates were found")
    return {
        "kind": "recurrence_placement_candidates",
        "version": FACTS_VERSION,
        "source_dcp_sha256": facts.get("design_sha256"),
        "region_sha256": facts.get("region_sha256"),
        "incumbent_clock_regions": sorted(regions),
        "srl_candidates": [
            {
                "site": item["site"],
                "bel": f"{letter}6LUT",
                "source_distance": item["source_distance"],
                "reinjection_distance": item["reinjection_distance"],
            }
            for item in selected_srl
            for letter in "ABCDEFGH"
            if (
                design.getSiteInstFromSite(design.getDevice().getSite(item["site"])) is None
                or all(
                    str(cell.getBELName()) not in {f"{letter}5LUT", f"{letter}6LUT"}
                    for cell in _as_list(
                        design.getSiteInstFromSite(
                            design.getDevice().getSite(item["site"])
                        ).getCells()
                    )
                )
            )
        ],
        "ce_clone_candidates": [
            {
                "site": item["site"],
                "bel": f"{letter}6LUT",
                "source_distance": item["source_distance"],
                "reinjection_distance": item["reinjection_distance"],
            }
            for item in selected_clones
            for letter in "ABCDEFGH"
            if (
                design.getSiteInstFromSite(design.getDevice().getSite(item["site"])) is None
                or all(
                    str(cell.getBELName()) not in {f"{letter}5LUT", f"{letter}6LUT"}
                    for cell in _as_list(
                        design.getSiteInstFromSite(
                            design.getDevice().getSite(item["site"])
                        ).getCells()
                    )
                )
            )
        ],
        "ranking": "sequential_slicem_only_then_global_sum_manhattan_to_boundary_and_reinjection",
        "authority": "candidate_only_vivado_measurement_required",
    }


# ---------------------------------------------------------------------------
# Fabric fixed-point product recovery (fixed_point_dsp_rebuild_v1)
#
# The cone-graph analysis and coefficient recovery below are pure Python over
# plain dict inputs (JVM-free, unit-testable). Only the thin adapters at the
# bottom read live RapidWright Design objects.
# ---------------------------------------------------------------------------

FIXED_POINT_FACTS_VERSION = 1
_FIXED_POINT_MAX_CONE_CELLS = 1024
_FIXED_POINT_MAX_SHIFT = 32
# Total budget of z3-verified coefficient candidates per recover call, shared
# across the unsigned attempt and the signed retry so runtime stays bounded.
_FIXED_POINT_MAX_CEGIS_ROUNDS = 24
# Input-side boundary FFs may be any D-flop variant (synthesis freely maps
# set/reset flavors); the value on Q is a plain registered data bit either
# way. Sink-side requirements stay FDRE-only via _SUPPORTED_FFS.
_FIXED_POINT_INPUT_FFS = {"FDRE", "FDSE", "FDCE", "FDPE"}
_FIXED_POINT_MAX_SPARSE_BUS_INDEX = 63
_FIXED_POINT_WRAPPER_KEYS = ("candidate_id", "fresh_rediscovery", "runtime_s")
_BUS_NAME_RE = re.compile(r"^(?P<base>.+)\[(?P<index>\d+)\]$")
_LUT_TYPE_RE = re.compile(r"^LUT([1-6])$")
_CARRY_TYPES = {"CARRY4": 4, "CARRY8": 8}
_INIT_LITERAL_RE = re.compile(r"(?:(\d+)'[sS]?([bBoOdDhH]))?([0-9a-fA-F]+)")
_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")


def _lut_init_int(value: Any, size: int) -> int:
    """Parse a Verilog-style LUT INIT literal into a truth-table integer."""
    if isinstance(value, bool):
        raise OperatorFactsError("LUT INIT is not a literal")
    if isinstance(value, int):
        number = value
    else:
        text = str(value).strip().replace("_", "")
        match = _INIT_LITERAL_RE.fullmatch(text)
        if match is None:
            raise OperatorFactsError("LUT INIT contains unsupported or unknown digits")
        declared, radix_token, digits = match.groups()
        radix = {
            None: 10, "b": 2, "o": 8, "d": 10, "h": 16,
        }[radix_token.lower() if radix_token else None]
        number = int(digits, radix)
        if declared is not None and int(declared) != (1 << size):
            raise OperatorFactsError(
                f"LUT INIT width is {declared}, expected {1 << size}"
            )
    if not 0 <= number < (1 << (1 << size)):
        raise OperatorFactsError("LUT INIT value exceeds the truth-table width")
    return number


def _is_const_zero_source(src: Any) -> bool:
    return (
        isinstance(src, dict)
        and src.get("kind") == "const"
        and not bool(src.get("value"))
    )


def _cone_used_operand_bits(cone: dict[str, Any], kind: str) -> set[int]:
    """Collect every bit index of one operand kind referenced in the cone."""
    references = list(cone.get("outputs") or [])
    cells = cone.get("cells")
    if isinstance(cells, dict):
        for record in cells.values():
            if isinstance(record, dict) and isinstance(record.get("inputs"), dict):
                references.extend(record["inputs"].values())
    return {
        reference["bit"]
        for reference in references
        if isinstance(reference, dict)
        and reference.get("kind") == kind
        and isinstance(reference.get("bit"), int)
        and not isinstance(reference.get("bit"), bool)
    }


def _cone_used_input_bits(cone: dict[str, Any]) -> set[int]:
    """Collect every input-bus bit index referenced anywhere in the cone."""
    return _cone_used_operand_bits(cone, "input")


def _validate_fixed_point_cone(cone: Any) -> None:
    if not isinstance(cone, dict):
        raise OperatorFactsError("cone must be an object")
    width = cone.get("input_width")
    if isinstance(width, bool) or not isinstance(width, int) or not 2 <= width <= 64:
        raise OperatorFactsError("cone input width must be 2..64")
    outputs = cone.get("outputs")
    if not isinstance(outputs, list) or not 1 <= len(outputs) <= 64:
        raise OperatorFactsError("cone output bus must contain 1..64 bits")
    accumulator_width = cone.get("accumulator_width")
    if accumulator_width is not None and accumulator_width != len(outputs):
        raise OperatorFactsError(
            "cone accumulator width must equal the sink bus width"
        )
    hole_bits = cone.get("input_hole_bits")
    if hole_bits is not None:
        if (
            not isinstance(hole_bits, list)
            or any(
                isinstance(item, bool)
                or not isinstance(item, int)
                or not 0 <= item < width
                for item in hole_bits
            )
            or hole_bits != sorted(set(hole_bits))
        ):
            raise OperatorFactsError(
                "cone input_hole_bits must be a sorted list of unique in-range "
                "bit indices"
            )
        # A hole is an index with no FF at all; a bit the cone merely ignores
        # is NOT a hole (the reference product still depends on it, so exact
        # verification fails closed unless the coefficient makes it irrelevant).
        if set(hole_bits) & _cone_used_input_bits(cone):
            raise OperatorFactsError(
                "cone input_hole_bits overlap input bits the cone reads"
            )
    cells = cone.get("cells")
    if not isinstance(cells, dict) or len(cells) > _FIXED_POINT_MAX_CONE_CELLS:
        raise OperatorFactsError(
            f"cone must contain at most {_FIXED_POINT_MAX_CONE_CELLS} cells"
        )


def _cone_bit_builder(cone: dict[str, Any], z3, data, accumulator, operands=None):
    """Return src -> z3 Bool resolver over the validated cone graph.

    ``data`` (when present) resolves ``{"kind": "input"}`` references; extra
    named operand buses (e.g. ``input_a``/``input_b``) may be supplied via the
    ``operands`` mapping. Single-bus callers are unchanged.
    """
    cells = cone["cells"]
    buses: dict[str, Any] = dict(operands or {})
    if data is not None:
        buses["input"] = data
    evaluated: dict[str, dict[str, Any]] = {}
    in_progress: set[str] = set()

    def bit(src):
        if not isinstance(src, dict):
            raise OperatorFactsError("cone source reference is malformed")
        kind = src.get("kind")
        if kind == "const":
            return z3.BoolVal(bool(src.get("value")))
        if kind in buses:
            vector = buses[kind]
            index = src.get("bit")
            if isinstance(index, bool) or not isinstance(index, int) \
                    or not 0 <= index < vector.size():
                raise OperatorFactsError("cone input bit index is out of range")
            return z3.Extract(index, index, vector) == z3.BitVecVal(1, 1)
        if kind == "acc":
            if accumulator is None:
                raise OperatorFactsError("cone references an absent accumulator bus")
            index = src.get("bit")
            if isinstance(index, bool) or not isinstance(index, int) \
                    or not 0 <= index < accumulator.size():
                raise OperatorFactsError("cone accumulator bit index is out of range")
            return z3.Extract(index, index, accumulator) == z3.BitVecVal(1, 1)
        if kind == "cell":
            outputs = cell_outputs(str(src.get("cell")))
            pin = str(src.get("pin"))
            if pin not in outputs:
                raise OperatorFactsError(f"cone references unknown cell pin {pin}")
            return outputs[pin]
        raise OperatorFactsError(f"cone source kind {kind!r} is unsupported")

    def cell_outputs(name: str) -> dict[str, Any]:
        if name in evaluated:
            return evaluated[name]
        if name in in_progress:
            raise OperatorFactsError("cone contains a combinational cycle")
        record = cells.get(name)
        if not isinstance(record, dict):
            raise OperatorFactsError(f"cone references an absent cell {name}")
        in_progress.add(name)
        cell_type = str(record.get("type", "")).upper()
        inputs = record.get("inputs") or {}
        lut = _LUT_TYPE_RE.fullmatch(cell_type)
        if lut is not None:
            size = int(lut.group(1))
            init = _lut_init_int(record.get("init"), size)
            pins = []
            for index in range(size):
                src = inputs.get(f"I{index}")
                if src is None:
                    raise OperatorFactsError(
                        f"LUT cell {name} input I{index} is unconnected"
                    )
                pins.append(bit(src))
            minterms = []
            for assignment in range(1 << size):
                if not (init >> assignment) & 1:
                    continue
                minterms.append(z3.And(*[
                    pins[index] if (assignment >> index) & 1 else z3.Not(pins[index])
                    for index in range(size)
                ]))
            result = {"O": z3.Or(*minterms) if minterms else z3.BoolVal(False)}
        elif cell_type in _CARRY_TYPES:
            chain_width = _CARRY_TYPES[cell_type]
            ci_src = inputs.get("CI")
            cyinit_src = inputs.get("CYINIT")
            if ci_src is not None and cyinit_src is not None and not (
                _is_const_zero_source(ci_src) or _is_const_zero_source(cyinit_src)
            ):
                raise OperatorFactsError(
                    f"carry cell {name} drives both CI and CYINIT"
                )
            ci_top = inputs.get("CI_TOP")
            if ci_top is not None and not _is_const_zero_source(ci_top):
                raise OperatorFactsError(
                    f"carry cell {name} uses an unsupported CI_TOP"
                )

            def optional(pin_name):
                src = inputs.get(pin_name)
                return z3.BoolVal(False) if src is None else bit(src)

            carry = z3.Or(optional("CI"), optional("CYINIT"))
            result = {}
            for index in range(chain_width):
                select = optional(f"S[{index}]")
                generate = optional(f"DI[{index}]")
                result[f"O[{index}]"] = z3.Xor(select, carry)
                carry = z3.If(select, carry, generate)
                result[f"CO[{index}]"] = carry
        else:
            raise OperatorFactsError(
                f"cone cell {name} has unsupported primitive {cell_type!r}"
            )
        in_progress.discard(name)
        evaluated[name] = result
        return result

    return bit


def _bits_to_bitvector(z3, bits):
    parts = [
        z3.If(item, z3.BitVecVal(1, 1), z3.BitVecVal(0, 1)) for item in bits
    ]
    if len(parts) == 1:
        return parts[0]
    return z3.Concat(*reversed(parts))


def _decomposed_bitvector_equivalence(
    z3,
    actual,
    expected,
    *,
    width: int,
    constrain,
    timeout_ms: int,
):
    """Prove equality with a bounded per-output-bit fallback.

    Wide LUT/CARRY miters can time out in QF_BV even when each output-bit cone
    is easy. The fallback is exact: equality is accepted only when every bit's
    mismatch query is independently UNSAT under the same constraints.
    """
    import time

    solver = z3.SolverFor("QF_BV")
    solver.set(timeout=timeout_ms)
    constrain(solver)
    solver.add(actual != expected)
    verdict = solver.check()
    diagnostics = {
        "monolithic_result": str(verdict),
        "monolithic_reason_unknown": (
            solver.reason_unknown() if verdict == z3.unknown else None
        ),
        "bitwise_fallback_used": verdict == z3.unknown,
        "bitblast_fallback_count": 0,
        "proved_bit_count": 0,
        "failure_bit": None,
        "failure_result": None,
        "failure_reason_unknown": None,
    }
    if verdict != z3.unknown:
        return verdict, solver, diagnostics

    deadline = time.monotonic() + timeout_ms / 1000.0
    for index in range(width):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            diagnostics.update({
                "failure_bit": index,
                "failure_result": "unknown",
                "failure_reason_unknown": "shared bitwise deadline exhausted",
            })
            return z3.unknown, solver, diagnostics
        bit_solver = z3.SolverFor("QF_BV")
        bit_solver.set(timeout=max(1, int(remaining * 500.0)))
        constrain(bit_solver)
        mismatch = (
            z3.Extract(index, index, actual)
            != z3.Extract(index, index, expected)
        )
        bit_solver.add(mismatch)
        bit_verdict = bit_solver.check()
        if bit_verdict == z3.unknown:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                goal = z3.Goal()
                constraints = z3.Solver()
                constrain(constraints)
                goal.add(*constraints.assertions(), mismatch)
                tactic = z3.TryFor(
                    z3.Then(
                        "simplify", "propagate-values", "solve-eqs",
                        "elim-uncnstr", "bit-blast", "aig", "sat",
                    ),
                    max(1, int(remaining * 1000.0)),
                )
                diagnostics["bitblast_fallback_count"] += 1
                try:
                    expression = z3.simplify(tactic(goal).as_expr())
                    if z3.is_false(expression):
                        bit_verdict = z3.unsat
                    elif z3.is_true(expression):
                        bit_verdict = z3.sat
                except z3.Z3Exception:
                    pass
        if bit_verdict != z3.unsat:
            diagnostics.update({
                "failure_bit": index,
                "failure_result": str(bit_verdict),
                "failure_reason_unknown": (
                    bit_solver.reason_unknown()
                    if bit_verdict == z3.unknown else None
                ),
            })
            return bit_verdict, bit_solver, diagnostics
        diagnostics["proved_bit_count"] = index + 1
    return z3.unsat, solver, diagnostics


def recover_cone_modular_affine(
    cone: dict[str, Any],
    *,
    timeout_ms: int = 120000,
) -> dict[str, Any]:
    """Recover an exact modular linear combination over named input buses.

    The cone schema supplies ``operand_widths`` as ``kind -> width`` and uses
    those kinds in source references. Coefficients are recovered from unit
    probes and then accepted only when one symbolic bit-vector miter is unsat.
    """
    import z3

    if not isinstance(cone, dict):
        raise OperatorFactsError("affine cone must be an object")
    widths = cone.get("operand_widths")
    if (
        not isinstance(widths, dict)
        or not 2 <= len(widths) <= 16
        or any(
            not isinstance(name, str)
            or not re.fullmatch(r"input_[0-9]+", name)
            or isinstance(width, bool)
            or not isinstance(width, int)
            or not 1 <= width <= 64
            for name, width in widths.items()
        )
    ):
        raise OperatorFactsError("affine cone operand_widths are invalid")
    outputs = cone.get("outputs")
    if not isinstance(outputs, list) or not 1 <= len(outputs) <= 64:
        raise OperatorFactsError("affine cone output bus must contain 1..64 bits")
    cells = cone.get("cells")
    if (
        not isinstance(cells, dict)
        or not cells
        or len(cells) > _FIXED_POINT_MAX_CONE_CELLS
    ):
        raise OperatorFactsError("affine cone cell inventory is invalid")
    if (
        isinstance(timeout_ms, bool)
        or not isinstance(timeout_ms, int)
        or not 1 <= timeout_ms <= 600000
    ):
        raise OperatorFactsError("timeout_ms must be an integer in 1..600000")

    ordered = sorted(widths, key=lambda name: int(name.split("_")[1]))
    if ordered != [f"input_{index}" for index in range(len(ordered))]:
        raise OperatorFactsError("affine cone operand names must be contiguous")
    operands = {
        name: z3.BitVec(f"affine_{name}", widths[name]) for name in ordered
    }
    bit = _cone_bit_builder(
        cone, z3, None, None, operands=operands
    )
    cone_value = _bits_to_bitvector(z3, [bit(src) for src in outputs])
    output_width = len(outputs)
    mask = (1 << output_width) - 1

    def observe(values: dict[str, int]) -> int:
        substitutions = [
            (operands[name], z3.BitVecVal(values.get(name, 0), widths[name]))
            for name in ordered
        ]
        value = z3.simplify(z3.substitute(cone_value, *substitutions))
        if not isinstance(value, z3.BitVecNumRef):
            raise OperatorFactsError("affine cone did not evaluate to a constant")
        return value.as_long()

    constant = observe({})
    if constant != 0:
        raise OperatorFactsError("affine cone has a nonzero constant term")
    coefficients = {
        name: observe({name: 1}) & mask for name in ordered
    }
    if not any(coefficients.values()):
        raise OperatorFactsError("affine cone computes a constant zero")

    terms = []
    for name in ordered:
        operand = operands[name]
        width = widths[name]
        if width < output_width:
            resized = z3.ZeroExt(output_width - width, operand)
        elif width > output_width:
            resized = z3.Extract(output_width - 1, 0, operand)
        else:
            resized = operand
        terms.append(
            resized * z3.BitVecVal(coefficients[name], output_width)
        )
    reference = terms[0]
    for term in terms[1:]:
        reference = reference + term
    verifier = z3.SolverFor("QF_BV")
    verifier.set(timeout=timeout_ms)
    verifier.add(cone_value != reference)
    verdict = verifier.check()
    if verdict == z3.unknown:
        raise OperatorFactsError(
            f"affine cone verification returned unknown: {verifier.reason_unknown()}"
        )
    if verdict != z3.unsat:
        witness = verifier.model()
        counterexample = {
            name: witness.eval(operands[name], model_completion=True).as_long()
            for name in ordered
        }
        raise OperatorFactsError(
            "cone is not an exact modular affine combination; "
            f"counterexample={counterexample}"
        )
    return {
        "status": "proved",
        "method": "unit_probes_with_exact_modular_bitvector_verification",
        "output_width": output_width,
        "operand_widths": {name: widths[name] for name in ordered},
        "coefficients": coefficients,
        "verification": {
            "name": "cone_equals_modular_affine_combination",
            "result": "unsat",
        },
    }


def recover_cone_multiplier(
    cone: dict[str, Any],
    *,
    timeout_ms: int = 120000,
    max_shift: int = _FIXED_POINT_MAX_SHIFT,
) -> dict[str, Any]:
    """Recover and exactly verify one constant coefficient for a fabric cone.

    The cone is built as a bit-vector expression of the input bus; the
    candidate coefficient is solved from basis-vector evaluations, then
    the equivalence ``cone(x[, acc]) == [acc +] Extract(x * coeff)`` is
    verified with a z3 unsat check refined CEGIS-style. There is no
    sampling-only acceptance path.

    Sparse input buses declare ``input_hole_bits``: indices with no source
    FF at all. Hole bits are constrained to zero on both sides of the
    equivalence (the emitted DSP ties those A bits to GND). The data
    operand is first modeled unsigned; when that fails, one signed retry
    models the reference product with a sign-extended data operand (the
    coefficient stays unsigned non-negative) and reports ``data_signed``.
    The total number of z3-verified coefficient candidates across both
    attempts is capped at ``_FIXED_POINT_MAX_CEGIS_ROUNDS``.
    """
    import z3

    _validate_fixed_point_cone(cone)
    for name, value, low, high in (
        ("timeout_ms", timeout_ms, 1, 600000),
        ("max_shift", max_shift, 0, 32),
    ):
        if isinstance(value, bool) or not isinstance(value, int) \
                or not low <= value <= high:
            raise OperatorFactsError(f"{name} must be an integer in {low}..{high}")
    data_width = cone["input_width"]
    term_width = len(cone["outputs"])
    accumulate = cone.get("accumulator_width") is not None
    hole_bits = sorted(cone.get("input_hole_bits") or [])
    hole_mask = 0
    for index in hole_bits:
        hole_mask |= 1 << index
    data = z3.BitVec("fixed_point_data", data_width)
    accumulator = (
        z3.BitVec("fixed_point_accumulator", term_width) if accumulate else None
    )
    bit = _cone_bit_builder(cone, z3, data, accumulator)
    if not cone["cells"]:
        raise OperatorFactsError("cone contains no LUT or CARRY logic")
    cone_value = _bits_to_bitvector(z3, [bit(src) for src in cone["outputs"]])

    coeff_width = max_shift + term_width
    product_bits = data_width + coeff_width
    coeff = z3.BitVec("fixed_point_coefficient", coeff_width)
    verified_attempts = 0

    def solve(data_signed: bool) -> dict[str, Any]:
        nonlocal verified_attempts
        extend = z3.SignExt if data_signed else z3.ZeroExt

        def reference(coeff_expr):
            product = extend(product_bits - data_width, data) * z3.ZeroExt(
                product_bits - coeff_width, coeff_expr
            )
            term = z3.Extract(max_shift + term_width - 1, max_shift, product)
            return accumulator + term if accumulate else term

        reference_expr = reference(coeff)

        def substitutions(x_value, acc_value):
            pairs = [(data, z3.BitVecVal(x_value, data_width))]
            if accumulate:
                pairs.append((accumulator, z3.BitVecVal(acc_value, term_width)))
            return pairs

        def observe(x_value, acc_value):
            value = z3.simplify(
                z3.substitute(cone_value, *substitutions(x_value, acc_value))
            )
            if not isinstance(value, z3.BitVecNumRef):
                raise OperatorFactsError("cone did not evaluate to a constant")
            return value.as_long()

        solver = z3.Optimize()
        solver.set(timeout=timeout_ms)
        # Minimizing the raw coefficient makes recovery canonical: without it
        # any low-order junk below the shift that can never carry into the
        # observed slice would satisfy the exact check and change the
        # recovered constant.
        solver.minimize(coeff)

        def add_probe(x_value, acc_value):
            observed = observe(x_value, acc_value)
            solver.add(
                z3.substitute(reference_expr, *substitutions(x_value, acc_value))
                == z3.BitVecVal(observed, term_width)
            )

        # Every probe keeps hole bits at zero, matching the GND ties of the
        # emitted DSP data operand.
        all_ones = ((1 << data_width) - 1) & ~hole_mask
        probes = [(0, 0), (all_ones, 0)]
        probes.extend(
            (1 << index, 0)
            for index in range(data_width)
            if not (hole_mask >> index) & 1
        )
        if accumulate:
            probes.extend([
                (0, 1), (1 & ~hole_mask, 1), (all_ones, (1 << term_width) - 1),
            ])
        for probe in probes:
            add_probe(*probe)

        rounds = 0
        coefficient_model = None
        while True:
            rounds += 1
            verified_attempts += 1
            if verified_attempts > _FIXED_POINT_MAX_CEGIS_ROUNDS:
                raise OperatorFactsError("z3_attempt_budget_exhausted")
            outcome = solver.check()
            if outcome == z3.unsat:
                raise OperatorFactsError(
                    "cone is not an exact constant-coefficient product"
                )
            if outcome == z3.unknown:
                raise OperatorFactsError("coefficient solve returned unknown")
            coefficient_model = solver.model().eval(
                coeff, model_completion=True
            ).as_long()
            verifier = z3.Solver()
            verifier.set(timeout=timeout_ms)
            verifier.add(
                cone_value != reference(z3.BitVecVal(coefficient_model, coeff_width))
            )
            for index in hole_bits:
                verifier.add(
                    z3.Extract(index, index, data) == z3.BitVecVal(0, 1)
                )
            verdict = verifier.check()
            if verdict == z3.unsat:
                break
            if verdict == z3.unknown:
                raise OperatorFactsError("coefficient verification returned unknown")
            witness = verifier.model()
            counter_x = witness.eval(data, model_completion=True).as_long()
            counter_acc = (
                witness.eval(accumulator, model_completion=True).as_long()
                if accumulate else 0
            )
            add_probe(counter_x & ~hole_mask, counter_acc)

        if coefficient_model == 0:
            raise OperatorFactsError("cone computes a constant zero, not a product")
        trailing_zeros = (coefficient_model & -coefficient_model).bit_length() - 1
        strip = min(trailing_zeros, max_shift)
        coefficient = coefficient_model >> strip
        shift = max_shift - strip
        coefficient_width = max(1, coefficient.bit_length())
        product_width = max(data_width + coefficient_width, shift + term_width)
        return {
            "status": "proved",
            "method": "cegis_basis_probes_with_exact_bitvector_verification",
            "data_width": data_width,
            "term_width": term_width,
            "coefficient": coefficient,
            "coefficient_width": coefficient_width,
            "shift": shift,
            "product_width": product_width,
            "accumulate": accumulate,
            "data_signed": data_signed,
            "cegis_rounds": rounds,
            "verification": {
                "name": "fixed_point_cone_equals_shifted_product",
                "result": "unsat",
            },
        }

    try:
        return solve(False)
    except OperatorFactsError as unsigned_error:
        if str(unsigned_error) == "z3_attempt_budget_exhausted":
            raise
        try:
            return solve(True)
        except OperatorFactsError as signed_error:
            if str(signed_error) == "z3_attempt_budget_exhausted":
                raise
            raise unsigned_error from None


def recover_cone_shifted_product_sum(
    cone: dict[str, Any],
    *,
    timeout_ms: int = 120000,
    max_shift: int = _FIXED_POINT_MAX_SHIFT,
) -> dict[str, Any]:
    """Recover a constant-product sum with an exactly proved loss order.

    Each named input is projected into a conventional single-input cone and
    proved by ``recover_cone_multiplier``. The projected proofs are necessary
    but not sufficient: final symbolic miters try independent, grouped, and
    partitioned full-precision loss orders and accept only an exact unsat result
    against the original multi-input cone for all simultaneous input values.
    """
    import z3

    if not isinstance(cone, dict):
        raise OperatorFactsError("shifted-product-sum cone must be an object")
    widths = cone.get("operand_widths")
    hole_bits = cone.get("operand_hole_bits")
    outputs = cone.get("outputs")
    cells = cone.get("cells")
    if (
        not isinstance(widths, dict)
        or not 2 <= len(widths) <= 16
        or any(
            not isinstance(name, str)
            or not re.fullmatch(r"input_[0-9]+", name)
            or isinstance(width, bool)
            or not isinstance(width, int)
            or not 1 <= width <= 64
            for name, width in widths.items()
        )
    ):
        raise OperatorFactsError("shifted-product-sum operand_widths are invalid")
    ordered = sorted(widths, key=lambda name: int(name.split("_")[1]))
    if ordered != [f"input_{index}" for index in range(len(ordered))]:
        raise OperatorFactsError(
            "shifted-product-sum operand names must be contiguous"
        )
    if hole_bits is None:
        hole_bits = {name: [] for name in ordered}
    if (
        not isinstance(hole_bits, dict)
        or set(hole_bits) != set(ordered)
        or any(
            not isinstance(hole_bits[name], list)
            or any(
                isinstance(index, bool)
                or not isinstance(index, int)
                or not 0 <= index < widths[name]
                for index in hole_bits[name]
            )
            or len(hole_bits[name]) != len(set(hole_bits[name]))
            for name in ordered
        )
    ):
        raise OperatorFactsError(
            "shifted-product-sum operand_hole_bits are invalid"
        )
    if not isinstance(outputs, list) or not 1 <= len(outputs) <= 64:
        raise OperatorFactsError(
            "shifted-product-sum output bus must contain 1..64 bits"
        )
    if (
        not isinstance(cells, dict)
        or not cells
        or len(cells) > _FIXED_POINT_MAX_CONE_CELLS
    ):
        raise OperatorFactsError("shifted-product-sum cell inventory is invalid")

    def project(selected: str) -> dict[str, Any]:
        projected = json.loads(json.dumps({"cells": cells, "outputs": outputs}))

        def rewrite(source: Any) -> None:
            if not isinstance(source, dict):
                return
            kind = source.get("kind")
            if kind not in widths:
                return
            if kind == selected:
                source["kind"] = "input"
            else:
                source.clear()
                source.update({"kind": "const", "value": 0})

        for cell in projected["cells"].values():
            for source in cell.get("inputs", {}).values():
                rewrite(source)
        for source in projected["outputs"]:
            rewrite(source)
        projected.update({
            "input_width": widths[selected],
            "input_hole_bits": sorted(hole_bits[selected]),
        })
        return projected

    terms = []
    for name in ordered:
        try:
            recovery = recover_cone_multiplier(
                project(name), timeout_ms=timeout_ms, max_shift=max_shift
            )
        except OperatorFactsError as error:
            raise OperatorFactsError(
                f"could not recover independent term for {name}: {error}"
            ) from None
        terms.append({"operand": name, **recovery})

    operands = {
        name: z3.BitVec(f"shifted_sum_{name}", widths[name]) for name in ordered
    }

    def constrain_holes(solver) -> None:
        for name in ordered:
            for index in hole_bits[name]:
                solver.add(
                    z3.Extract(index, index, operands[name])
                    == z3.BitVecVal(0, 1)
                )

    def bitblast_verdict(reference):
        """Fallback for exact arithmetic miters that stall QF_BV search."""
        goal = z3.Goal()
        constraints = []
        for name in ordered:
            for index in hole_bits[name]:
                constraints.append(
                    z3.Extract(index, index, operands[name])
                    == z3.BitVecVal(0, 1)
                )
        goal.add(*(constraints + [cone_value != reference]))
        tactic = z3.TryFor(
            z3.Then(
                "simplify", "propagate-values", "solve-eqs",
                "elim-uncnstr", "bit-blast", "aig", "sat",
            ),
            timeout_ms,
        )
        try:
            expression = z3.simplify(tactic(goal).as_expr())
        except z3.Z3Exception:
            return z3.unknown
        if z3.is_false(expression):
            return z3.unsat
        if z3.is_true(expression):
            return z3.sat
        return z3.unknown

    bit = _cone_bit_builder(cone, z3, None, None, operands=operands)
    cone_value = _bits_to_bitvector(z3, [bit(source) for source in outputs])
    output_width = len(outputs)
    reference_terms = []
    for term in terms:
        operand = operands[term["operand"]]
        input_width = term["data_width"]
        coefficient_width = term["coefficient_width"]
        product_width = term["product_width"]
        extend = z3.SignExt if term["data_signed"] else z3.ZeroExt
        product = extend(product_width - input_width, operand) * z3.ZeroExt(
            product_width - coefficient_width,
            z3.BitVecVal(term["coefficient"], coefficient_width),
        )
        reference_terms.append(
            z3.Extract(
                term["shift"] + output_width - 1,
                term["shift"],
                product,
            )
        )
    reference = reference_terms[0]
    for term in reference_terms[1:]:
        reference = reference + term
    verification_attempts = []

    def check_reference(label, candidate, candidate_timeout_ms=timeout_ms):
        result, candidate_solver, diagnostics = _decomposed_bitvector_equivalence(
            z3,
            cone_value,
            candidate,
            width=output_width,
            constrain=constrain_holes,
            timeout_ms=candidate_timeout_ms,
        )
        verification_attempts.append({"label": label, **diagnostics})
        return result, candidate_solver

    verdict, verifier = check_reference(
        "sum_of_independently_shifted_products", reference
    )
    combination = "sum_of_independently_shifted_products"
    common_shift = None
    adjusted_coefficients = None
    groups = [{
        "operands": [term["operand"]],
        "shift": term["shift"],
        "coefficients": {term["operand"]: term["coefficient"]},
        "operand_signedness": {term["operand"]: term["data_signed"]},
    } for term in terms]

    def canonical_retained_coefficient(term):
        """Choose the signed residue visible above this term's loss boundary."""
        retained_width = term["shift"] + output_width
        modulus = 1 << retained_width
        coefficient = term["coefficient"] & (modulus - 1)
        if coefficient >= (modulus >> 1):
            coefficient -= modulus
        return coefficient

    def coefficient_storage_width(coefficient):
        if coefficient < 0:
            return max(1, (~coefficient).bit_length() + 1)
        return max(1, coefficient.bit_length())

    def signedness_options(group_terms):
        choices = []
        for term in group_terms:
            values = [term["data_signed"]]
            # A projection ending below the source sign-extension region
            # cannot distinguish unsigned from signed.  Preserve both exact
            # hypotheses for the whole-cone miter instead of silently fixing
            # the unsigned-first local choice.
            if (
                term["data_signed"] is False
                and term["data_width"] >= term["shift"] + output_width
            ):
                values.append(True)
            choices.append(values)
        for selected in itertools.product(*choices):
            yield {
                term["operand"]: selected[index]
                for index, term in enumerate(group_terms)
            }

    def common_group(
        group_terms, result_width=output_width, operand_signedness=None
    ):
        if operand_signedness is None:
            operand_signedness = {
                term["operand"]: term["data_signed"] for term in group_terms
            }
        group_shift = max(term["shift"] for term in group_terms)
        group_coefficients = {
            term["operand"]: canonical_retained_coefficient(term)
            << (group_shift - term["shift"])
            for term in group_terms
        }
        group_width = max(
            group_shift + result_width,
            *(
                term["data_width"]
                + coefficient_storage_width(
                    group_coefficients[term["operand"]]
                )
                for term in group_terms
            ),
        ) + len(group_terms).bit_length()
        numerator_terms = []
        for term in group_terms:
            operand = operands[term["operand"]]
            extend = (
                z3.SignExt
                if operand_signedness[term["operand"]]
                else z3.ZeroExt
            )
            numerator_terms.append(
                extend(group_width - term["data_width"], operand)
                * z3.BitVecVal(
                    group_coefficients[term["operand"]], group_width
                )
            )
        numerator = numerator_terms[0]
        for numerator_term in numerator_terms[1:]:
            numerator = numerator + numerator_term
        expression = z3.Extract(
            group_shift + result_width - 1, group_shift, numerator
        )
        group_signed = all(
            operand_signedness[term["operand"]] for term in group_terms
        )
        if result_width < output_width:
            resize = z3.SignExt if group_signed else z3.ZeroExt
            expression = resize(output_width - result_width, expression)
        metadata = {
            "operands": [term["operand"] for term in group_terms],
            "shift": group_shift,
            "coefficients": group_coefficients,
            "operand_signedness": {
                term["operand"]: operand_signedness[term["operand"]]
                for term in group_terms
            },
            "result_width": result_width,
            "result_signed": group_signed,
        }
        return expression, metadata

    if verdict != z3.unsat:
        # Fixed-point datapaths commonly add full-precision numerators and
        # round only once. Independent projections cannot see carries from
        # discarded low bits, so retry the unique common-denominator form.
        common_shift = max(term["shift"] for term in terms)
        adjusted_coefficients = {
            term["operand"]: term["coefficient"]
            << (common_shift - term["shift"])
            for term in terms
        }
        for signedness in signedness_options(terms):
            common_reference, common_metadata = common_group(
                terms, operand_signedness=signedness
            )
            verdict, common_verifier = check_reference(
                "sum_full_precision_then_common_shift", common_reference
            )
            verifier = common_verifier
            if verdict == z3.unsat:
                combination = "sum_full_precision_then_common_shift"
                groups = [common_metadata]
                break
    if verdict != z3.unsat:
        # A pipeline may round precomputed terms independently while retaining
        # full precision only inside one multiply-add pair. Prefer pairs whose
        # recovered shifts are closest, then prove every candidate exactly.
        pair_indices = sorted(
            itertools.combinations(range(len(terms)), 2),
            key=lambda pair: (
                abs(terms[pair[0]]["shift"] - terms[pair[1]]["shift"]),
                pair,
            ),
        )
        for pair in pair_indices:
            pair_set = set(pair)
            pair_terms = [terms[index] for index in pair]
            for signedness in signedness_options(pair_terms):
                pair_expression, pair_metadata = common_group(
                    pair_terms, operand_signedness=signedness
                )
                candidate = pair_expression
                candidate_groups = [pair_metadata]
                for index, term_expression in enumerate(reference_terms):
                    if index in pair_set:
                        continue
                    candidate = candidate + term_expression
                    candidate_groups.append(groups[index])
                pair_verdict, pair_verifier = check_reference(
                    "grouped_full_precision_then_shift", candidate
                )
                if pair_verdict == z3.unsat:
                    verdict = pair_verdict
                    verifier = pair_verifier
                    combination = "grouped_full_precision_then_shift"
                    groups = candidate_groups
                    common_shift = None
                    adjusted_coefficients = None
                    break
                if pair_verdict == z3.unknown:
                    continue
            if verdict == z3.unsat:
                break
    if verdict != z3.unsat and len(terms) <= 6:
        def partitions(indices):
            if not indices:
                yield ()
                return
            first = indices[0]
            for remainder in partitions(indices[1:]):
                yield ((first,), *remainder)
                for group_index in range(len(remainder)):
                    merged = tuple(sorted((first, *remainder[group_index])))
                    yield tuple(sorted(
                        (*remainder[:group_index], merged,
                         *remainder[group_index + 1:]),
                        key=lambda group: group[0],
                    ))

        unique_partitions = set(partitions(tuple(range(len(terms)))))
        candidates = []
        for partition in unique_partitions:
            multi_groups = [group for group in partition if len(group) > 1]
            if len(partition) in {1, len(terms)}:
                continue
            spread = sum(
                max(terms[index]["shift"] for index in group)
                - min(terms[index]["shift"] for index in group)
                for group in multi_groups
            )
            candidates.append((spread, partition))
        for _, partition in sorted(candidates):
            group_options = []
            for group in partition:
                if len(group) == 1:
                    index = group[0]
                    operand = terms[index]["operand"]
                    singleton_metadata = {
                        **groups[index],
                        "coefficients": {
                            operand: canonical_retained_coefficient(terms[index])
                        },
                    }
                    group_options.append([(
                        reference_terms[index], singleton_metadata
                    )])
                else:
                    group_terms = [terms[index] for index in group]
                    widths_to_try = [output_width]
                    if output_width > 1:
                        widths_to_try.append(output_width - 1)
                    group_options.append([
                        common_group(
                            group_terms, result_width,
                            operand_signedness=signedness,
                        )
                        for result_width in widths_to_try
                        for signedness in signedness_options(group_terms)
                    ])
            for selected_groups in itertools.product(*group_options):
                candidate = selected_groups[0][0]
                for expression, _ in selected_groups[1:]:
                    candidate = candidate + expression
                partition_verdict, partition_verifier = check_reference(
                    "partitioned_full_precision_then_shift",
                    candidate,
                    min(timeout_ms, 30000),
                )
                metadata_rows = [metadata for _, metadata in selected_groups]
                needs_ambiguity_proof = any(
                    any(value < 0 for value in metadata["coefficients"].values())
                    or any(
                        metadata.get("operand_signedness", {}).get(
                            term["operand"], term["data_signed"]
                        ) != term["data_signed"]
                        for term in terms
                        if term["operand"] in metadata["operands"]
                    )
                    for metadata in metadata_rows
                )
                if partition_verdict == z3.unknown and needs_ambiguity_proof:
                    partition_verdict = bitblast_verdict(candidate)
                if partition_verdict == z3.unsat:
                    verdict = partition_verdict
                    verifier = partition_verifier
                    combination = "partitioned_full_precision_then_shift"
                    groups = [metadata for _, metadata in selected_groups]
                    common_shift = None
                    adjusted_coefficients = None
                    break
                if partition_verdict == z3.unknown:
                    continue
            if verdict == z3.unsat:
                break
    if verdict != z3.unsat:
        term_summary = [{
            "operand": term["operand"],
            "coefficient": term["coefficient"],
            "shift": term["shift"],
            "data_signed": term["data_signed"],
        } for term in terms]
        raise OperatorFactsError(
            "cone is not the sum of its independently recovered terms; "
            f"terms={term_summary}; "
            f"verdict={verdict}; diagnostics={verification_attempts}"
        )
    return {
        "status": "proved",
        "method": "independent_shifted_products_with_exact_sum_verification",
        "output_width": output_width,
        "operand_widths": {name: widths[name] for name in ordered},
        "operand_hole_bits": {
            name: sorted(hole_bits[name]) for name in ordered
        },
        "terms": terms,
        "combination": combination,
        "common_shift": common_shift,
        "adjusted_coefficients": adjusted_coefficients,
        "groups": groups,
        "verification": {
            "name": "cone_equals_recovered_shifted_product_sum",
            "result": "unsat",
        },
        "verification_decomposition": verification_attempts,
    }


def build_fixed_point_product_facts(
    recovery: dict[str, Any],
    boundary: dict[str, Any],
    *,
    sink_width: int,
    design_sha256: str,
) -> dict[str, Any]:
    """Bind one proved cone into a hash-bound extracted_unproved facts dict."""
    if not isinstance(recovery, dict) or recovery.get("status") != "proved":
        raise OperatorFactsError("cone recovery is absent or unproved")
    if not isinstance(design_sha256, str) \
            or _SHA256_HEX_RE.fullmatch(design_sha256) is None:
        raise OperatorFactsError("design_sha256 must be a lowercase SHA-256 digest")
    required = {"input_bus", "output_bus", "cone_cells", "clock_pin", "ce_pin"}
    if not isinstance(boundary, dict) or not required <= set(boundary):
        raise OperatorFactsError("cone boundary facts are incomplete")
    if sink_width != recovery.get("term_width"):
        raise OperatorFactsError("sink bus width differs from the proved term width")
    spec = {
        "data_width": recovery["data_width"],
        "coefficient_width": recovery["coefficient_width"],
        "coefficient": recovery["coefficient"],
        "product_width": recovery["product_width"],
        "shift": recovery["shift"],
        "term_width": recovery["term_width"],
        "accumulator_width": sink_width,
        "product_latency": 1,
        "data_signed": bool(recovery.get("data_signed", False)),
        "coefficient_signed": False,
        "term_signed": False,
        "accumulate": bool(recovery.get("accumulate", False)),
        "overflow": "wrap",
    }
    payload = {
        "kind": "fixed_point_product_contract_facts",
        "version": FIXED_POINT_FACTS_VERSION,
        "status": "success",
        "design_sha256": design_sha256,
        "proof_status": "fixed_point_facts_extracted_unproved",
        "mutation_eligible": False,
        "spec": spec,
        "boundary": {
            "input_bus": list(boundary["input_bus"]),
            "output_bus": list(boundary["output_bus"]),
            "cone_cells": list(boundary["cone_cells"]),
            "clock_pin": boundary["clock_pin"],
            "ce_pin": boundary["ce_pin"],
        },
    }
    # The self-digest follows the recurrence wrapper-key convention: the
    # region digest is computed with region_sha256 and the wrapper keys
    # (candidate_id, fresh_rediscovery, runtime_s) absent from the payload.
    region_sha256 = _digest(payload)
    facts = dict(payload)
    facts["fresh_rediscovery"] = True
    facts["candidate_id"] = "fixedpoint:" + region_sha256[:20]
    facts["region_sha256"] = region_sha256
    return facts


def _fixed_point_bus_groups(design) -> dict[str, list[str]]:
    """Group supported FFs into complete contiguous indexed buses by name.

    A synthesized product slice may retain only registers ``[lsb..msb]`` with
    ``lsb > 0``. The cone outputs are normalized by sorted register order; the
    original indices remain present in the boundary cell names.
    """
    groups: dict[str, dict[int, str]] = {}
    for cell in _as_list(design.getCells()):
        if str(cell.getType()).upper() not in _SUPPORTED_FFS:
            continue
        name = str(cell.getName())
        match = _BUS_NAME_RE.fullmatch(name)
        if match is None:
            continue
        groups.setdefault(match.group("base"), {})[int(match.group("index"))] = name
    buses = {}
    for base, members in groups.items():
        width = len(members)
        if (
            2 <= width <= 64
            and max(members) <= _FIXED_POINT_MAX_SPARSE_BUS_INDEX
            and sorted(members) == list(range(min(members), max(members) + 1))
        ):
            buses[base] = [members[index] for index in sorted(members)]
    return buses


def _fixed_point_sparse_bus_groups(design) -> dict[str, dict[int, str]]:
    """Group input-side FFs into sparse index->cell buses by base name.

    Synthesis constant-folds individual bus bits away, so a named input bus
    may have holes and need not start at bit 0. Data bit positions stay the
    raw indices (a missing low bit is a hole tied to zero; offsets are never
    renormalized). Requires at least two present bits and a maximum index of
    _FIXED_POINT_MAX_SPARSE_BUS_INDEX so data_width = max_index + 1 is 2..64.
    """
    groups: dict[str, dict[int, str]] = {}
    for cell in _as_list(design.getCells()):
        if str(cell.getType()).upper() not in _FIXED_POINT_INPUT_FFS:
            continue
        name = str(cell.getName())
        match = _BUS_NAME_RE.fullmatch(name)
        if match is None:
            continue
        groups.setdefault(match.group("base"), {})[int(match.group("index"))] = name
    return {
        base: dict(members)
        for base, members in groups.items()
        if len(members) >= 2
        and max(members) <= _FIXED_POINT_MAX_SPARSE_BUS_INDEX
    }


def _shared_canonical_net(netlist, pins: list, context: str) -> str:
    names = {
        _net_name(_parent_net(netlist, pin)) for pin in pins
    }
    if len(names) != 1:
        raise OperatorFactsError(f"{context} does not share one canonical net")
    return next(iter(names))


def _walk_registered_cone(
    design,
    netlist,
    sink_base: str,
    sink_ffs: list[str],
    sparse_buses: dict[str, dict[int, str]],
) -> tuple[dict[str, Any], list, list, dict[str, tuple[str, int]]]:
    """Walk backward from one sink FF bus through LUT/CARRY primitives only.

    Shared by the single-bus fixed-point trace and the two-operand trace; the
    callers assign bus roles over the returned boundary references.
    """
    cells: dict[str, dict[str, Any]] = {}
    pending: list[tuple[str, str]] = []
    boundary_refs: list[dict[str, Any]] = []

    def enqueue(cell_name: str, cell_type: str) -> None:
        if cell_name in cells:
            return
        if len(cells) >= _FIXED_POINT_MAX_CONE_CELLS:
            raise OperatorFactsError(
                f"cone exceeds the {_FIXED_POINT_MAX_CONE_CELLS}-cell bound"
            )
        cells[cell_name] = {"type": cell_type, "inputs": {}}
        pending.append((cell_name, cell_type))

    def resolve(pin, context: str) -> dict[str, Any]:
        parent = _parent_net(netlist, pin)
        source = _unique_source(parent, context)
        source_type = _cell_type(source)
        source_pin_name = _pin_name(source).upper()
        source_cell = _cell_name(source)
        if source_type == "GND" and source_pin_name == "G":
            return {"kind": "const", "value": 0}
        if source_type == "VCC" and source_pin_name == "P":
            return {"kind": "const", "value": 1}
        if source_type in _FIXED_POINT_INPUT_FFS and source_pin_name == "Q":
            reference = {"kind": "boundary_ff", "cell": source_cell}
            boundary_refs.append(reference)
            return reference
        if _LUT_TYPE_RE.fullmatch(source_type) is not None \
                and source_pin_name == "O":
            enqueue(source_cell, source_type)
            return {"kind": "cell", "cell": source_cell, "pin": "O"}
        vector = _VECTOR_PIN_RE.fullmatch(source_pin_name)
        if source_type in _CARRY_TYPES and vector is not None \
                and vector.group("name") in {"O", "CO"}:
            enqueue(source_cell, source_type)
            return {"kind": "cell", "cell": source_cell, "pin": source_pin_name}
        raise OperatorFactsError(
            f"{context}: cone driver {source_type}/{source_pin_name} is unsupported"
        )

    def resolve_optional(pin_name: str) -> Optional[dict[str, Any]]:
        pin = netlist.getHierPortInstFromName(pin_name)
        if pin is None:
            return None
        if pin.getHierarchicalNet() is None:
            return {"kind": "const", "value": 0}
        return resolve(pin, pin_name)

    outputs = []
    for ff_name in sink_ffs:
        pin = _hier_pin(netlist, f"{ff_name}/D")
        outputs.append(resolve(pin, f"{ff_name}/D"))

    while pending:
        cell_name, cell_type = pending.pop()
        record = cells[cell_name]
        lut = _LUT_TYPE_RE.fullmatch(cell_type)
        if lut is not None:
            size = int(lut.group(1))
            properties = _logical_cell_properties(netlist, cell_name)
            if "INIT" not in properties:
                raise OperatorFactsError(f"LUT cell {cell_name} has no INIT")
            record["init"] = _lut_init_int(properties["INIT"], size)
            for index in range(size):
                pin = _hier_pin(netlist, f"{cell_name}/I{index}")
                record["inputs"][f"I{index}"] = resolve(
                    pin, f"{cell_name}/I{index}"
                )
        else:
            chain_width = _CARRY_TYPES[cell_type]
            for index in range(chain_width):
                for prefix in ("S", "DI"):
                    source = resolve_optional(f"{cell_name}/{prefix}[{index}]")
                    if source is not None:
                        record["inputs"][f"{prefix}[{index}]"] = source
            for scalar in ("CI", "CYINIT", "CI_TOP"):
                source = resolve_optional(f"{cell_name}/{scalar}")
                if source is not None:
                    record["inputs"][scalar] = source

    if not cells:
        raise OperatorFactsError("cone contains no LUT or CARRY logic")

    parsed_boundary: dict[str, tuple[str, int]] = {}
    for reference in boundary_refs:
        name = reference["cell"]
        match = _BUS_NAME_RE.fullmatch(name)
        if match is None or (
            match.group("base") != sink_base
            and match.group("base") not in sparse_buses
        ):
            raise OperatorFactsError(
                "cone input boundary FF is not part of a registered bus"
            )
        parsed_boundary[name] = (
            match.group("base"), int(match.group("index"))
        )
    return cells, outputs, boundary_refs, parsed_boundary


def _trace_fixed_point_cone(
    design,
    netlist,
    sink_base: str,
    sink_ffs: list[str],
    sparse_buses: dict[str, dict[int, str]],
) -> tuple[dict[str, Any], dict[str, Any], int]:
    """Trace one sink FF bus into a single-data-bus fixed-point cone."""
    cells, outputs, boundary_refs, parsed_boundary = _walk_registered_cone(
        design, netlist, sink_base, sink_ffs, sparse_buses
    )
    data_bases = sorted({
        base for base, _index in parsed_boundary.values() if base != sink_base
    })
    if len(data_bases) != 1:
        raise OperatorFactsError(
            "cone input boundary is not a single registered data bus"
        )
    data_base = data_bases[0]
    uses_accumulator = any(
        base == sink_base for base, _index in parsed_boundary.values()
    )
    sink_start = int(_BUS_NAME_RE.fullmatch(sink_ffs[0]).group("index"))
    for reference in boundary_refs:
        base, index = parsed_boundary[reference["cell"]]
        reference.pop("cell", None)
        reference["kind"] = "acc" if base == sink_base else "input"
        reference["bit"] = index - sink_start if base == sink_base else index

    # Sparse data bus: bit positions are the raw indices; indices with no FF
    # cell at all are holes tied to zero (never renormalized away).
    sparse_bus = sparse_buses[data_base]
    data_width = max(sparse_bus) + 1
    input_hole_bits = [
        index for index in range(data_width) if index not in sparse_bus
    ]
    data_ffs = [sparse_bus[index] for index in sorted(sparse_bus)]
    sink_clock_pins = [_hier_pin(netlist, f"{name}/C") for name in sink_ffs]
    data_clock_pins = [_hier_pin(netlist, f"{name}/C") for name in data_ffs]
    _shared_canonical_net(
        netlist, sink_clock_pins + data_clock_pins, "cone clock boundary"
    )
    sink_ce_pins = [_hier_pin(netlist, f"{name}/CE") for name in sink_ffs]
    _shared_canonical_net(netlist, sink_ce_pins, "sink CE boundary")

    cone = {
        "input_width": data_width,
        "accumulator_width": len(sink_ffs) if uses_accumulator else None,
        "input_hole_bits": input_hole_bits,
        "cells": cells,
        "outputs": outputs,
    }
    boundary = {
        "input_bus": [
            {"cell": sparse_bus[index], "pin": f"{sparse_bus[index]}/Q"}
            if index in sparse_bus else {"const": 0}
            for index in range(data_width)
        ],
        "output_bus": [{"cell": name, "pin": f"{name}/D"} for name in sink_ffs],
        "cone_cells": [
            {"cell": name, "type": cells[name]["type"]}
            for name in sorted(cells)
        ],
        "clock_pin": f"{sink_ffs[0]}/C",
        "ce_pin": f"{sink_ffs[0]}/CE",
    }
    return cone, boundary, len(sink_ffs)


def recover_fixed_point_product_contract(
    design,
    design_sha256: str,
    max_candidates: int = 8,
) -> dict[str, Any]:
    """Autonomously recover proved fabric fixed-point product cones.

    Discovery is benchmark-agnostic: every complete FF sink bus is tried;
    cones walk backward through LUT1-6/CARRY4/CARRY8 primitives only, are
    bounded to the shared cone-cell cap, and must present a single registered data bus
    (optionally plus the sink bus itself as a wrap accumulator). The data
    bus may be sparse: bit indices synthesis constant-folded away are holes
    tied to zero at their raw positions. Semantics are recovered exactly
    from LUT INIT truth tables and carry chains with a z3 bit-vector unsat
    verification (unsigned first, then one signed-data retry).
    """
    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) \
            or not 1 <= max_candidates <= 64:
        raise OperatorFactsError("max_candidates must be 1..64")
    if not isinstance(design_sha256, str) \
            or _SHA256_HEX_RE.fullmatch(design_sha256) is None:
        raise OperatorFactsError("design_sha256 must be a lowercase SHA-256 digest")
    netlist = design.getNetlist()
    buses = _fixed_point_bus_groups(design)
    sparse_buses = _fixed_point_sparse_bus_groups(design)
    candidates = []
    rejections: dict[str, int] = {}
    for sink_base in sorted(buses):
        if len(candidates) >= max_candidates:
            break
        try:
            cone, boundary, sink_width = _trace_fixed_point_cone(
                design, netlist, sink_base, buses[sink_base], sparse_buses
            )
            recovery = recover_cone_multiplier(cone)
            facts = build_fixed_point_product_facts(
                recovery, boundary,
                sink_width=sink_width,
                design_sha256=design_sha256,
            )
        except OperatorFactsError as exc:
            reason = str(exc)
            rejections[reason] = rejections.get(reason, 0) + 1
            continue
        candidates.append(facts)
    result = {
        "status": "success",
        "kind": "fixed_point_product_contract_facts_set",
        "version": FIXED_POINT_FACTS_VERSION,
        "design_sha256": design_sha256,
        "proof_status": "fixed_point_facts_extracted_unproved",
        "mutation_eligible": False,
        "fresh_rediscovery": True,
        "candidates": candidates,
        "candidate_count": len(candidates),
    }
    if not candidates:
        result["reason"] = (
            "no FF-bounded LUT/CARRY cone proved an exact constant-coefficient "
            "fixed-point product"
        )
        result["rejection_histogram"] = dict(sorted(rejections.items())[:16])
    return result


def propose_fixed_point_placement_candidates(
    design,
    design_sha256: str,
    region_sha256: str,
    max_sites: int = 64,
) -> dict[str, Any]:
    """Rank idle DSP48E2 sites by Manhattan distance from the cone centroid."""
    if isinstance(max_sites, bool) or not isinstance(max_sites, int) \
            or not 1 <= max_sites <= 256:
        raise OperatorFactsError("max_sites must be 1..256")
    if not isinstance(region_sha256, str) \
            or _SHA256_HEX_RE.fullmatch(region_sha256) is None:
        raise OperatorFactsError("region_sha256 must be a lowercase SHA-256 digest")
    facts_set = recover_fixed_point_product_contract(design, design_sha256)
    matches = [
        item for item in facts_set.get("candidates", [])
        if item.get("region_sha256") == region_sha256
    ]
    if len(matches) != 1:
        raise OperatorFactsError(
            "region is absent from the fresh fixed-point rediscovery"
        )
    boundary = matches[0]["boundary"]
    coordinates = []
    for entry in [*boundary["input_bus"], *boundary["output_bus"]]:
        if "cell" not in entry:
            continue  # const hole entries have no placed cell
        name = str(entry["cell"])
        cell = design.getCell(name)
        if cell is None or not bool(cell.isPlaced()) or cell.getSite() is None:
            raise OperatorFactsError(f"boundary cell is absent or unplaced: {name}")
        tile = cell.getSite().getTile()
        if tile is None:
            raise OperatorFactsError(f"boundary cell tile is unavailable: {name}")
        coordinates.append((
            int(tile.getTileXCoordinate()), int(tile.getTileYCoordinate())
        ))
    centroid_x = round(sum(x for x, _y in coordinates) / len(coordinates))
    centroid_y = round(sum(y for _x, y in coordinates) / len(coordinates))
    slots = []
    for site in _as_list(design.getDevice().getAllSites()):
        if str(site.getSiteTypeEnum()).upper() != "DSP48E2":
            continue
        site_inst = design.getSiteInstFromSite(site)
        if site_inst is not None and _as_list(site_inst.getCells()):
            continue
        tile = site.getTile()
        if tile is None:
            continue
        distance = (
            abs(int(tile.getTileXCoordinate()) - centroid_x)
            + abs(int(tile.getTileYCoordinate()) - centroid_y)
        )
        slots.append({
            "site": str(site.getName()),
            "bel": "DSP48E2",
            "distance": distance,
            "clock_route_ok": True,
            "source_dcp_sha256": design_sha256,
            "region_sha256": region_sha256,
        })
    slots.sort(key=lambda item: (item["distance"], item["site"]))
    return {
        "status": "success",
        "source_dcp_sha256": design_sha256,
        "region_sha256": region_sha256,
        "fresh_rediscovery": True,
        "slots": slots[:max_sites],
    }


# ---------------------------------------------------------------------------
# Shared DSP48E2 discovery helpers (register absorb + pipeline rebalance)
#
# Vivado-written DCPs represent a DSP48E2 as a NON-LEAF macro cell: an EDIF
# cell whose type name is DSP48E2 containing the DSP_* internal leaf
# primitives (DSP_ALU, DSP_A_B_DATA, ...). Freshly created post-ECO cells
# are single LEAF primitives of the same type. Discovery therefore must
# enumerate DSP48E2 instances regardless of leaf-ness, resolve placement
# through either the macro's own physical cell or one of its placed
# internal leaves, and collapse leaf pins inside the macro back onto the
# macro boundary pin names. All property and pin lookups stay name-based on
# the macro boundary (getCellInstFromHierName / getHierPortInstFromName),
# which is leaf-agnostic.
# ---------------------------------------------------------------------------

_DSP48E2_MACRO_CHILD_INSTS = (
    "DSP_ALU_INST",
    "DSP_A_B_DATA_INST",
    "DSP_C_DATA_INST",
    "DSP_MULTIPLIER_INST",
    "DSP_M_DATA_INST",
    "DSP_OUTPUT_INST",
    "DSP_PREADD_DATA_INST",
    "DSP_PREADD_INST",
)
_DSP48E2_SCAN_INSTANCE_CAP = 5_000_000
_NO_DSP48E2_REJECTION = "no DSP48E2 instances in the logical netlist"


def _iterate_logical_dsp48e2_instances(netlist):
    """Yield hierarchical instances whose cell type is exactly DSP48E2.

    Walks the logical hierarchy from the top instance so both the leaf form
    (post-ECO fresh cells) and the macro form (Vivado-written DCPs) are
    found; a leaf-only enumeration yields zero DSP48E2 on real Vivado
    designs because only the internal DSP_* primitives are leaves. The walk
    never descends into a DSP48E2 boundary and deduplicates by full
    hierarchical instance name. Falls back to the leaf enumeration when the
    netlist adapter provides no top-instance walk.
    """
    seen: set[str] = set()
    top_getter = getattr(netlist, "getTopHierCellInst", None)
    if callable(top_getter):
        queue = [top_getter()]
        visited = 0
        while queue:
            current = queue.pop()
            if current is None:
                continue
            visited += 1
            if visited > _DSP48E2_SCAN_INSTANCE_CAP:
                raise OperatorFactsError(
                    "DSP48E2 hierarchy scan exceeded the instance bound"
                )
            cell_type = current.getCellType()
            if str(cell_type.getName()).upper() == "DSP48E2":
                name = str(current.getFullHierarchicalInstName())
                if name not in seen:
                    seen.add(name)
                    yield current
                continue
            for child in _as_list(cell_type.getCellInsts()):
                queue.append(current.getChild(child))
        return
    try:
        leaves = _as_list(netlist.getAllLeafHierCellInstances())
    except TypeError:
        leaves = _as_list(netlist.getAllLeafHierCellInstances(False))
    except AttributeError:
        leaves = []
    for inst in leaves:
        if str(inst.getCellType().getName()).upper() != "DSP48E2":
            continue
        name = str(inst.getFullHierarchicalInstName())
        if name not in seen:
            seen.add(name)
            yield inst


def _logical_dsp48e2_names(netlist) -> list[str]:
    return sorted({
        str(inst.getFullHierarchicalInstName())
        for inst in _iterate_logical_dsp48e2_instances(netlist)
    })


def _placed_dsp_site(design, dsp_name: str) -> str:
    """Resolve the placed site name for either DSP48E2 physical form.

    The leaf form places one physical cell under the DSP's own name; the
    macro form has no physical cell for the boundary name, only the placed
    DSP_* internal leaves, all of which share the one DSP48E2 site.
    """
    lookups = [dsp_name] + [
        f"{dsp_name}/{child}" for child in _DSP48E2_MACRO_CHILD_INSTS
    ]
    for name in lookups:
        cell = design.getCell(name)
        if cell is None or not bool(cell.isPlaced()):
            continue
        site = cell.getSite()
        if site is not None:
            return str(site.getName())
    raise OperatorFactsError(f"DSP cell is absent or unplaced: {dsp_name}")


def _dsp_boundary_pin_name(dsp_name: str, pin_name: str) -> str:
    """Collapse a macro-internal leaf pin onto its DSP48E2 boundary pin.

    Leaf-form pins and pins outside the DSP pass through unchanged, so
    fanout comparisons against boundary pin names stay exact (unknown
    internal ports simply never match an expected boundary pin).
    """
    prefix = f"{dsp_name}/"
    if pin_name.startswith(prefix):
        remainder = pin_name[len(prefix):]
        child, _sep, port = remainder.partition("/")
        if port and child in _DSP48E2_MACRO_CHILD_INSTS:
            return f"{dsp_name}/{port}"
    return pin_name


# ---------------------------------------------------------------------------
# DSP input register absorption recovery (dsp_register_absorb_v1)
#
# Fabric FDRE register buses that feed the A or B input of a DSP48E2 whose
# hardened input register is currently disabled (AREG/BREG == 0). Absorbing
# the bus into the DSP input register (0 -> 1) moves one register stage into
# the hard macro while preserving port-path register counts. Recovery here is
# read-only: it extracts and hash-binds the exact boundary; the transition
# proof lives in FDAgents.operator_proof. Pure Python over duck-typed
# adapters (JVM-free), following the fixed-point/two-operand conventions.
# ---------------------------------------------------------------------------

REGISTER_ABSORB_FACTS_VERSION = 1
# FDRE only: sync reset-to-zero with CE matches the hardened DSP input
# register semantics exactly. FDCE is async (excluded); FDSE sets to one
# (excluded).
_REGISTER_ABSORB_FFS = {"FDRE"}
_REGISTER_ABSORB_MAX_SCAN_BITS = 48
# (side, vector, reg property, cascade reg property, stage-1 CE pin,
#  stage-2 CE pin, RST pin, bus width cap)
_REGISTER_ABSORB_SIDES = (
    ("a", "A", "AREG", "ACASCREG", "CEA1", "CEA2", "RSTA", 30),
    ("b", "B", "BREG", "BCASCREG", "CEB1", "CEB2", "RSTB", 18),
)


def _register_absorb_int_property(properties: dict[str, str], key: str) -> int:
    raw = str(properties.get(key, "")).strip()
    match = re.fullmatch(r"(?:\d+'[sS]?[dD])?(\d+)", raw)
    if match is None:
        raise OperatorFactsError(
            f"DSP property {key} is absent or non-integer"
        )
    return int(match.group(1))


def _recover_register_absorb_candidate(
    design,
    netlist,
    dsp_name: str,
    side_spec: tuple,
    design_sha256: str,
) -> dict[str, Any]:
    """Recover one absorbable FF bus on one DSP side, or reject exactly."""
    side, vector, reg_key, cascreg_key, ce1_pin, ce2_pin, rst_pin, width_cap = (
        side_spec
    )
    properties = _logical_cell_properties(netlist, dsp_name)
    for key in (reg_key, cascreg_key):
        if _register_absorb_int_property(properties, key) != 0:
            raise OperatorFactsError(
                f"DSP {side}-side input register is not disabled ({key}!=0)"
            )
    dsp_site = _placed_dsp_site(design, dsp_name)

    # The absorbed stage's hardened controls must currently be tied to GND so
    # the fabric CE/RST nets can be remapped onto them without side effects.
    for pin_name in (ce1_pin, ce2_pin, rst_pin):
        pin = _hier_pin(netlist, f"{dsp_name}/{pin_name}")
        if _static_input_bit(netlist, pin, f"{dsp_name}/{pin_name}") != 0:
            raise OperatorFactsError(
                f"DSP {pin_name} is not tied to constant GND"
            )

    # --- contiguous low FF prefix, then GND / MSB-replicated upper bits ----
    ff_bus: list[tuple[str, Any]] = []  # (cell name, canonical Q net)
    seen_ffs = set()
    entries: list[tuple[int, str, Any]] = []
    for index in range(_REGISTER_ABSORB_MAX_SCAN_BITS):
        pin = netlist.getHierPortInstFromName(f"{dsp_name}/{vector}[{index}]")
        if pin is None:
            break
        if pin.getHierarchicalNet() is None:
            entries.append((index, "open", None))
            continue
        parent = _parent_net(netlist, pin)
        source = _unique_source(parent, f"{dsp_name}/{vector}[{index}]")
        source_type = _cell_type(source)
        source_pin = _pin_name(source).upper()
        if source_type == "GND" and source_pin == "G":
            entries.append((index, "gnd", None))
        elif source_type in _REGISTER_ABSORB_FFS and source_pin == "Q":
            entries.append((index, "ff", (_cell_name(source), parent)))
        else:
            entries.append((index, "other", None))
    for index, kind, payload in entries:
        if index != len(ff_bus) or kind != "ff" or payload[0] in seen_ffs:
            break
        ff_bus.append(payload)
        seen_ffs.add(payload[0])
    bus_width = len(ff_bus)
    if bus_width < 2:
        raise OperatorFactsError(
            f"DSP {vector} input low bits are not a fabric FDRE register bus"
        )
    if bus_width > width_cap:
        raise OperatorFactsError(
            f"DSP {vector} FF bus width exceeds the {width_cap}-bit absorb cap"
        )
    msb_net_name = _net_name(ff_bus[-1][1])
    replicated_indices = []
    for index, kind, payload in entries[bus_width:]:
        if kind in {"gnd", "open"}:
            continue
        if kind == "ff" and _net_name(payload[1]) == msb_net_name:
            replicated_indices.append(index)
            continue
        raise OperatorFactsError(
            f"DSP {vector} upper bits are not GND or MSB sign replication"
        )

    # --- sole fanout: removing the FF may not change any other logic -------
    ff_names = [name for name, _net in ff_bus]
    for bit_index, (ff_name, q_net) in enumerate(ff_bus):
        # Macro-form DSPs surface leaf sinks on the internal DSP_* pins;
        # collapse them onto the macro boundary before the exact compare.
        sinks = sorted({
            _dsp_boundary_pin_name(dsp_name, str(pin))
            for pin in _as_list(q_net.getLeafHierPortInsts(False, True, True))
        })
        expected = [f"{dsp_name}/{vector}[{bit_index}]"]
        if bit_index == bus_width - 1:
            expected.extend(
                f"{dsp_name}/{vector}[{index}]"
                for index in replicated_indices
            )
        if sinks != sorted(expected):
            raise OperatorFactsError(
                f"FF Q bit {bit_index} has fanout beyond its DSP input pin"
            )

    # --- uniform controls shared with the DSP clock, INIT-zero bus ---------
    clock_pins = [_hier_pin(netlist, f"{name}/C") for name in ff_names]
    clock_pins.append(_hier_pin(netlist, f"{dsp_name}/CLK"))
    _shared_canonical_net(netlist, clock_pins, "absorb clock boundary")
    ce_pins = [_hier_pin(netlist, f"{name}/CE") for name in ff_names]
    _shared_canonical_net(netlist, ce_pins, "absorb CE boundary")
    reset_pins = [_hier_pin(netlist, f"{name}/R") for name in ff_names]
    _shared_canonical_net(netlist, reset_pins, "absorb reset boundary")
    reset_source = _unique_source(
        _parent_net(netlist, reset_pins[0]), f"{ff_names[0]}/R"
    )
    if _cell_type(reset_source) == "GND" \
            and _pin_name(reset_source).upper() == "G":
        reset_kind = "none"
        reset_pin = None
    else:
        reset_kind = "sync_reset_zero"
        reset_pin = f"{ff_names[0]}/R"
    for ff_name in ff_names:
        init = _logical_cell_properties(netlist, ff_name).get("INIT")
        if init is not None and str(init).lower() not in {
            "0", "1'b0", "1'h0", "false"
        }:
            raise OperatorFactsError(f"absorb FF {ff_name} has nonzero INIT")

    payload = {
        "kind": "register_absorb_contract_facts",
        "version": REGISTER_ABSORB_FACTS_VERSION,
        "status": "success",
        "design_sha256": design_sha256,
        "proof_status": "register_absorb_facts_extracted_unproved",
        "mutation_eligible": False,
        "spec": {
            "bus_width": bus_width,
            "dsp_side": side,
            "ce_present": True,
            "reset_kind": reset_kind,
            "init_zero": True,
            "absorb_stages": 1,
        },
        "boundary": {
            "ff_bus": [
                {"cell": name, "pin": f"{name}/Q"} for name in ff_names
            ],
            "driver_nets_complete": True,
            "dsp_cell": dsp_name,
            "dsp_site": dsp_site,
            "dsp_side": side,
            "clock_pin": f"{ff_names[0]}/C",
            "ce_pin": f"{ff_names[0]}/CE",
            "reset_pin": reset_pin,
        },
    }
    # The self-digest follows the recurrence wrapper-key convention: the
    # region digest is computed with region_sha256 and the wrapper keys
    # (candidate_id, fresh_rediscovery, runtime_s) absent from the payload.
    region_sha256 = _digest(payload)
    facts = dict(payload)
    facts["fresh_rediscovery"] = True
    facts["candidate_id"] = "regabsorb:" + region_sha256[:20]
    facts["region_sha256"] = region_sha256
    return facts


def recover_register_absorb_contract(
    design,
    design_sha256: str,
    max_candidates: int = 8,
) -> dict[str, Any]:
    """Autonomously recover absorbable DSP48E2 input register buses.

    A candidate is one (DSP48E2 cell, side "a"|"b") pair where the hardened
    input register is disabled (AREG/BREG and ACASCREG/BCASCREG == 0), the
    low input bits form a contiguous bus of distinct fabric FDRE Q drivers
    (upper bits GND or MSB sign replication only), every FF Q net has the DSP
    input pin as its sole fanout, the bus shares one clock net with the DSP
    CLK plus uniform CE and R nets with INIT-zero FFs, and the DSP-side
    CEA1/CEA2/RSTA (or CEB1/CEB2/RSTB) pins are tied to GND. No placement
    proposer is needed: the DSP is already placed and absorption is in-place.
    """
    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) \
            or not 1 <= max_candidates <= 64:
        raise OperatorFactsError("max_candidates must be 1..64")
    if not isinstance(design_sha256, str) \
            or _SHA256_HEX_RE.fullmatch(design_sha256) is None:
        raise OperatorFactsError("design_sha256 must be a lowercase SHA-256 digest")
    netlist = design.getNetlist()
    # Logical-hierarchy enumeration: Vivado-written DCPs carry DSP48E2 only
    # as a non-leaf macro cell, so neither the physical cell list nor a
    # leaf-only walk ever finds one on real designs.
    dsp_names = _logical_dsp48e2_names(netlist)
    candidates = []
    rejections: dict[str, int] = {}
    if not dsp_names:
        rejections[_NO_DSP48E2_REJECTION] = 1
    for dsp_name in dsp_names:
        for side_spec in _REGISTER_ABSORB_SIDES:
            if len(candidates) >= max_candidates:
                break
            try:
                candidates.append(_recover_register_absorb_candidate(
                    design, netlist, dsp_name, side_spec, design_sha256
                ))
            except OperatorFactsError as exc:
                reason = str(exc)
                rejections[reason] = rejections.get(reason, 0) + 1
        if len(candidates) >= max_candidates:
            break
    result = {
        "status": "success",
        "kind": "register_absorb_contract_facts_set",
        "version": REGISTER_ABSORB_FACTS_VERSION,
        "design_sha256": design_sha256,
        "proof_status": "register_absorb_facts_extracted_unproved",
        "mutation_eligible": False,
        "fresh_rediscovery": True,
        "candidates": candidates,
        "candidate_count": len(candidates),
    }
    if not candidates:
        result["reason"] = (
            "no DSP48E2 A or B input presents an absorbable fabric FDRE "
            "register bus"
        )
        result["rejection_histogram"] = dict(sorted(rejections.items())[:16])
    return result


# ---------------------------------------------------------------------------
# DSP multiplier pipeline rebalance recovery (dsp_pipeline_rebalance_v1)
#
# The smallest true Leiserson-Saxe move: one lag step on the multiplier node
# of a single DSP48E2 (forward AREG/BREG -> MREG, backward MREG -> AREG/BREG).
# Port-path register counts are invariant by construction. Recovery here is
# read-only: it extracts and hash-binds the exact boundary; the transition
# proof lives in FDAgents.operator_proof. Pure Python over duck-typed
# adapters (JVM-free), following the register-absorb conventions.
# ---------------------------------------------------------------------------

PIPELINE_REBALANCE_FACTS_VERSION = 1
# Static mode: one 48-bit SIMD lane multiplying direct (non-cascade) A2/B2
# operands. Any other configuration changes which internal paths the moved
# register cuts, so recovery fails closed on exact property values.
_PIPELINE_REBALANCE_STATIC_PROPERTIES = (
    ("USE_MULT", "MULTIPLY"),
    ("AMULTSEL", "A"),
    ("BMULTSEL", "B"),
    ("A_INPUT", "DIRECT"),
    ("B_INPUT", "DIRECT"),
    ("USE_SIMD", "ONE48"),
)
_PIPELINE_REBALANCE_ZERO_INVERSION_PROPERTIES = (
    ("IS_INMODE_INVERTED", 5),
    ("IS_OPMODE_INVERTED", 9),
    ("IS_RSTA_INVERTED", 1),
    ("IS_RSTB_INVERTED", 1),
    ("IS_RSTM_INVERTED", 1),
)
# direction -> exact register property values before the move. ACASCREG and
# BCASCREG must track AREG/BREG so the property-plus-control-pin ECO stays
# legal. Forward is listed first: candidate order is (dsp_cell, direction).
_PIPELINE_REBALANCE_DIRECTIONS = (
    ("forward", (
        ("AREG", 1), ("BREG", 1), ("ACASCREG", 1), ("BCASCREG", 1),
        ("MREG", 0),
    )),
    ("backward", (
        ("AREG", 0), ("BREG", 0), ("ACASCREG", 0), ("BCASCREG", 0),
        ("MREG", 1),
    )),
)
# OPMODE[1:0] = 01 selects X = M-low and OPMODE[3:2] = 01 selects Y = M-high:
# the multiplier output feeds the ALU. Higher OPMODE bits steer the Z/W legs,
# which the moved register never touches, so they may be anything.
_PIPELINE_REBALANCE_OPMODE_BITS = ((0, 1), (1, 0), (2, 1), (3, 0))
_PIPELINE_REBALANCE_INMODE_WIDTH = 5
_PIPELINE_REBALANCE_CASCADE_OUTPUTS = (("ACOUT", 30), ("BCOUT", 18))
_PIPELINE_REBALANCE_CASCADE_INPUTS = (("ACIN", 30), ("BCIN", 18))
_PIPELINE_REBALANCE_OPERAND_CAPS = (("A", 30, "a_width"), ("B", 18, "b_width"))
# direction -> (shared CE pins, GND-tied CE pins, shared reset pins,
#               GND-tied reset pins). The shared pins carry the moved
#               registers' controls; the GND ties guarantee the vacated pins
#               can receive the remapped nets without side effects.
_PIPELINE_REBALANCE_CONTROLS = {
    "forward": (
        ("CEA2", "CEB2"), ("CEA1", "CEB1", "CEM"),
        ("RSTA", "RSTB"), ("RSTM",),
    ),
    "backward": (
        ("CEM",), ("CEA1", "CEA2", "CEB1", "CEB2"),
        ("RSTM",), ("RSTA", "RSTB"),
    ),
}


def _pipeline_rebalance_string_property(
    properties: dict[str, str], key: str, expected: str
) -> None:
    raw = str(properties.get(key, "")).strip().strip('"').upper()
    if raw != expected:
        raise OperatorFactsError(f"DSP property {key} is not {expected}")


def _pipeline_rebalance_zero_property(
    properties: dict[str, Any], key: str, width: int
) -> None:
    """Require an exactly parsed zero, including known omitted DSP defaults."""
    known_default = any(
        property_key == key
        for property_key, _width in _PIPELINE_REBALANCE_ZERO_INVERSION_PROPERTIES
    )
    # Vivado DCP/EDIF omits these five DSP48E2 inversion properties at their
    # documented zero defaults. A direct Vivado property query then returns
    # blank, so absent/empty means observed default zero only for this closed
    # list, not an unknown value or a generic missing-property fallback.
    if known_default and (
        key not in properties
        or isinstance(properties[key], str) and not properties[key].strip()
    ):
        return
    if key not in properties:
        raise OperatorFactsError(f"DSP property {key} is absent or malformed")
    value = properties[key]
    parsed = None
    if isinstance(value, int) and not isinstance(value, bool):
        parsed = value
    elif isinstance(value, str):
        raw = value.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] == '"':
            raw = raw[1:-1].strip()
        if raw.lower() in {"false", "true"}:
            parsed = int(raw.lower() == "true")
        elif re.fullmatch(r"\d+", raw):
            parsed = int(raw, 10)
        else:
            match = re.fullmatch(
                r"(?P<width>\d+)'[sS]?(?P<base>[bBdDhH])(?P<digits>[0-9a-fA-F]+)",
                raw,
            )
            literal_width = int(match.group("width")) if match is not None else 0
            if match is not None and literal_width > 0:
                base = {"b": 2, "d": 10, "h": 16}[match.group("base").lower()]
                try:
                    parsed = int(match.group("digits"), base)
                except ValueError:
                    parsed = None
                if parsed is not None and parsed >= 1 << literal_width:
                    parsed = None
    if parsed is None or parsed < 0:
        raise OperatorFactsError(f"DSP property {key} is absent or malformed")
    if parsed != 0:
        raise OperatorFactsError(f"DSP property {key} is not all zero")


def _recover_pipeline_rebalance_candidate(
    design,
    netlist,
    dsp_name: str,
    direction: str,
    register_values: tuple,
    design_sha256: str,
) -> dict[str, Any]:
    """Recover one MREG-lag rebalance boundary on one DSP, or reject exactly."""
    properties = _logical_cell_properties(netlist, dsp_name)
    for key, expected in _PIPELINE_REBALANCE_STATIC_PROPERTIES:
        _pipeline_rebalance_string_property(properties, key, expected)
    for key, width in _PIPELINE_REBALANCE_ZERO_INVERSION_PROPERTIES:
        _pipeline_rebalance_zero_property(properties, key, width)
    for key, expected in register_values:
        if _register_absorb_int_property(properties, key) != expected:
            raise OperatorFactsError(
                f"DSP {direction} rebalance requires {key}=={expected}"
            )
    dsp_site = _placed_dsp_site(design, dsp_name)
    _parent_net(netlist, _hier_pin(netlist, f"{dsp_name}/CLK"))

    # Static operand select (direct A2/B2) and X=01 Y=01 (M path) ALU feed.
    for index in range(_PIPELINE_REBALANCE_INMODE_WIDTH):
        pin_name = f"{dsp_name}/INMODE[{index}]"
        if _static_input_bit(netlist, _hier_pin(netlist, pin_name), pin_name) != 0:
            raise OperatorFactsError(
                f"DSP INMODE[{index}] is not tied to constant GND"
            )
    for index, expected in _PIPELINE_REBALANCE_OPMODE_BITS:
        pin_name = f"{dsp_name}/OPMODE[{index}]"
        if _static_input_bit(
            netlist, _hier_pin(netlist, pin_name), pin_name
        ) != expected:
            raise OperatorFactsError(
                "DSP OPMODE X/Y multiplexers do not select the M path"
            )

    # No cascade traffic on the moved side: the lag must stay inside one DSP.
    for vector, width in _PIPELINE_REBALANCE_CASCADE_OUTPUTS:
        for index in range(width):
            pin = netlist.getHierPortInstFromName(
                f"{dsp_name}/{vector}[{index}]"
            )
            if pin is None or pin.getHierarchicalNet() is None:
                continue
            loads = _as_list(
                _parent_net(netlist, pin).getLeafHierPortInsts(
                    False, True, True
                )
            )
            if loads:
                raise OperatorFactsError(
                    f"DSP {vector} cascade output drives downstream loads"
                )
    for vector, width in _PIPELINE_REBALANCE_CASCADE_INPUTS:
        for index in range(width):
            pin = netlist.getHierPortInstFromName(
                f"{dsp_name}/{vector}[{index}]"
            )
            if pin is not None and pin.getHierarchicalNet() is not None:
                raise OperatorFactsError(
                    f"DSP {vector} cascade input is connected"
                )

    # Uniform controls: one shared CE net on the currently-active registers
    # (a real signal is allowed), GND ties on the vacated side, and one
    # shared reset net (or none) mirrored across the moved registers.
    shared_ce, gnd_ce, shared_rst, gnd_rst = (
        _PIPELINE_REBALANCE_CONTROLS[direction]
    )
    ce_pins = [_hier_pin(netlist, f"{dsp_name}/{pin}") for pin in shared_ce]
    _shared_canonical_net(netlist, ce_pins, "rebalance CE boundary")
    for pin_name in gnd_ce:
        full_name = f"{dsp_name}/{pin_name}"
        if _static_input_bit(netlist, _hier_pin(netlist, full_name), full_name) != 0:
            raise OperatorFactsError(
                f"DSP {pin_name} is not tied to constant GND"
            )
    reset_pins = [_hier_pin(netlist, f"{dsp_name}/{pin}") for pin in shared_rst]
    _shared_canonical_net(netlist, reset_pins, "rebalance reset boundary")
    reset_source = _unique_source(
        _parent_net(netlist, reset_pins[0]), f"{dsp_name}/{shared_rst[0]}"
    )
    if _cell_type(reset_source) == "GND" \
            and _pin_name(reset_source).upper() == "G":
        reset_kind = "none"
        reset_pin = None
    else:
        reset_kind = "sync_reset_zero"
        reset_pin = f"{dsp_name}/{shared_rst[0]}"
    for pin_name in gnd_rst:
        full_name = f"{dsp_name}/{pin_name}"
        if _static_input_bit(netlist, _hier_pin(netlist, full_name), full_name) != 0:
            raise OperatorFactsError(
                f"DSP {pin_name} is not tied to constant GND"
            )

    # Effective operand widths: the highest non-constant driven bit + 1.
    widths = {}
    for vector, cap, key in _PIPELINE_REBALANCE_OPERAND_CAPS:
        width = 0
        for index in range(cap):
            pin = netlist.getHierPortInstFromName(
                f"{dsp_name}/{vector}[{index}]"
            )
            if pin is None or pin.getHierarchicalNet() is None:
                continue
            source = _unique_source(
                _parent_net(netlist, pin), f"{dsp_name}/{vector}[{index}]"
            )
            source_type = _cell_type(source)
            source_pin = _pin_name(source).upper()
            if (source_type == "GND" and source_pin == "G") \
                    or (source_type == "VCC" and source_pin == "P"):
                continue
            width = index + 1
        if width < 1:
            raise OperatorFactsError(
                f"DSP {vector} operand presents no non-constant data bits"
            )
        widths[key] = width

    payload = {
        "kind": "pipeline_rebalance_contract_facts",
        "version": PIPELINE_REBALANCE_FACTS_VERSION,
        "status": "success",
        "design_sha256": design_sha256,
        "proof_status": "pipeline_rebalance_facts_extracted_unproved",
        "mutation_eligible": False,
        "spec": {
            "a_width": widths["a_width"],
            "b_width": widths["b_width"],
            "direction": direction,
            "ce_present": True,
            "reset_kind": reset_kind,
            "init_zero": True,
        },
        "boundary": {
            "dsp_cell": dsp_name,
            "dsp_site": dsp_site,
            "clock_pin": f"{dsp_name}/CLK",
            "ce_pin": f"{dsp_name}/{shared_ce[0]}",
            "reset_pin": reset_pin,
            "a_width": widths["a_width"],
            "b_width": widths["b_width"],
        },
    }
    # The self-digest follows the recurrence wrapper-key convention: the
    # region digest is computed with region_sha256 and the wrapper keys
    # (candidate_id, fresh_rediscovery, runtime_s) absent from the payload.
    region_sha256 = _digest(payload)
    facts = dict(payload)
    facts["fresh_rediscovery"] = True
    facts["candidate_id"] = "rebalance:" + region_sha256[:20]
    facts["region_sha256"] = region_sha256
    return facts


def recover_pipeline_rebalance_contract(
    design,
    design_sha256: str,
    max_candidates: int = 8,
) -> dict[str, Any]:
    """Autonomously recover DSP48E2 AREG/BREG<->MREG rebalance boundaries.

    A candidate is one (DSP48E2 cell, direction) pair where the multiplier
    runs in static MULTIPLY M-path mode (INMODE GND-tied, OPMODE X=01 Y=01)
    on direct operands with no cascade traffic, exactly one side of the
    AREG/BREG-vs-MREG register pair is enabled (with ACASCREG/BCASCREG
    tracking AREG/BREG), the active registers share one CE net and one reset
    net (or none), the vacated registers' CE/RST pins are GND-tied, and the
    DSP is placed. Forward moves AREG/BREG into MREG; backward is the
    inverse. No placement proposer is needed: the move is in-place.
    """
    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) \
            or not 1 <= max_candidates <= 64:
        raise OperatorFactsError("max_candidates must be 1..64")
    if not isinstance(design_sha256, str) \
            or _SHA256_HEX_RE.fullmatch(design_sha256) is None:
        raise OperatorFactsError("design_sha256 must be a lowercase SHA-256 digest")
    netlist = design.getNetlist()
    # Logical-hierarchy enumeration: Vivado-written DCPs carry DSP48E2 only
    # as a non-leaf macro cell, so neither the physical cell list nor a
    # leaf-only walk ever finds one on real designs.
    dsp_names = _logical_dsp48e2_names(netlist)
    candidates = []
    rejections: dict[str, int] = {}
    if not dsp_names:
        rejections[_NO_DSP48E2_REJECTION] = 1
    for dsp_name in dsp_names:
        for direction, register_values in _PIPELINE_REBALANCE_DIRECTIONS:
            if len(candidates) >= max_candidates:
                break
            try:
                candidates.append(_recover_pipeline_rebalance_candidate(
                    design, netlist, dsp_name, direction, register_values,
                    design_sha256,
                ))
            except OperatorFactsError as exc:
                reason = str(exc)
                rejections[reason] = rejections.get(reason, 0) + 1
        if len(candidates) >= max_candidates:
            break
    result = {
        "status": "success",
        "kind": "pipeline_rebalance_contract_facts_set",
        "version": PIPELINE_REBALANCE_FACTS_VERSION,
        "design_sha256": design_sha256,
        "proof_status": "pipeline_rebalance_facts_extracted_unproved",
        "mutation_eligible": False,
        "fresh_rediscovery": True,
        "candidates": candidates,
        "candidate_count": len(candidates),
    }
    if not candidates:
        result["reason"] = (
            "no DSP48E2 presents an eligible AREG/BREG<->MREG pipeline "
            "rebalance boundary"
        )
        result["rejection_histogram"] = dict(sorted(rejections.items())[:16])
    return result


# ---------------------------------------------------------------------------
# Fabric two-operand product recovery (dsp_mul_two_operand_v1)
#
# Variable x variable multiply cones: the boundary presents exactly TWO
# registered data buses (optionally plus the sink bus itself as a wrap
# accumulator). There is no coefficient to solve, so recovery uses cheap
# concrete probes to determine the product slice shift and the operand
# signedness, then runs a single exact z3 bit-vector unsat verification.
# Pure Python over plain dict inputs (JVM-free); thin adapters at the bottom.
# ---------------------------------------------------------------------------

TWO_OPERAND_FACTS_VERSION = 1
# Total budget of z3-verified candidates per recover call, shared across the
# ambiguous-signedness iteration and the term_signed retry.
_TWO_OPERAND_MAX_Z3_ATTEMPTS = 24
_TWO_OPERAND_REJECT = "cone is not an exact two-operand variable product"


def _validate_two_operand_cone(cone: Any) -> None:
    if not isinstance(cone, dict):
        raise OperatorFactsError("cone must be an object")
    for name in ("a_width", "b_width"):
        width = cone.get(name)
        if isinstance(width, bool) or not isinstance(width, int) \
                or not 2 <= width <= 64:
            raise OperatorFactsError(f"cone {name} must be 2..64")
    outputs = cone.get("outputs")
    if not isinstance(outputs, list) or not 1 <= len(outputs) <= 64:
        raise OperatorFactsError("cone output bus must contain 1..64 bits")
    accumulator_width = cone.get("accumulator_width")
    if accumulator_width is not None and accumulator_width != len(outputs):
        raise OperatorFactsError(
            "cone accumulator width must equal the sink bus width"
        )
    for holes_key, kind, width_key in (
        ("input_hole_bits_a", "input_a", "a_width"),
        ("input_hole_bits_b", "input_b", "b_width"),
    ):
        hole_bits = cone.get(holes_key)
        if hole_bits is None:
            continue
        width = cone[width_key]
        if (
            not isinstance(hole_bits, list)
            or any(
                isinstance(item, bool)
                or not isinstance(item, int)
                or not 0 <= item < width
                for item in hole_bits
            )
            or hole_bits != sorted(set(hole_bits))
        ):
            raise OperatorFactsError(
                f"cone {holes_key} must be a sorted list of unique in-range "
                "bit indices"
            )
        # A hole is an index with no FF at all; a bit the cone merely ignores
        # is NOT a hole (the reference product still depends on it, so exact
        # verification fails closed unless it is genuinely irrelevant).
        if set(hole_bits) & _cone_used_operand_bits(cone, kind):
            raise OperatorFactsError(
                f"cone {holes_key} overlap input bits the cone reads"
            )
    cells = cone.get("cells")
    if not isinstance(cells, dict) or len(cells) > _FIXED_POINT_MAX_CONE_CELLS:
        raise OperatorFactsError(
            f"cone must contain at most {_FIXED_POINT_MAX_CONE_CELLS} cells"
        )


def recover_cone_two_operand(
    cone: dict[str, Any],
    *,
    timeout_ms: int = 120000,
    max_shift: int = _FIXED_POINT_MAX_SHIFT,
) -> dict[str, Any]:
    """Recover and exactly verify one variable x variable product cone.

    There is no coefficient CEGIS. The shift is located first from concrete
    single-bit probes ``(a=1<<j, b=1<<base_b)``: the smallest probe whose
    observed slice is a nonzero power of two fixes ``shift = position -
    log2(observed)``. The operand signedness pair is then chosen from concrete
    sign probes ``(a=all-ones, b=1)`` and ``(a=1, b=all-ones)`` compared
    against signed/unsigned reference predictions. When the probes are
    decisive exactly ONE z3 unsat verification runs::

        cone(a, b[, acc]) == [acc +] resize(Extract(shift+term_width-1,
                                                    shift, ext_a(a)*ext_b(b)))

    with ``product_width = a_width + b_width`` and every declared hole bit
    constrained to zero on both sides. When the probes are ambiguous the <= 4
    signedness combinations are iterated with the same check, each counted
    against the shared ``_TWO_OPERAND_MAX_Z3_ATTEMPTS`` budget.

    ``term_signed = a_signed or b_signed``: the slice resize signedness
    follows the signed product when either operand is signed. That choice is
    verified inside the same unsat check; when the resize is semantically
    visible (``term_width < sink width``) and the check fails, one flipped
    ``term_signed`` retry runs (budget-counted).
    """
    import z3

    _validate_two_operand_cone(cone)
    for name, value, low, high in (
        ("timeout_ms", timeout_ms, 1, 600000),
        ("max_shift", max_shift, 0, 32),
    ):
        if isinstance(value, bool) or not isinstance(value, int) \
                or not low <= value <= high:
            raise OperatorFactsError(f"{name} must be an integer in {low}..{high}")
    a_width = cone["a_width"]
    b_width = cone["b_width"]
    sink_width = len(cone["outputs"])
    accumulate = cone.get("accumulator_width") is not None
    holes_a = sorted(cone.get("input_hole_bits_a") or [])
    holes_b = sorted(cone.get("input_hole_bits_b") or [])
    hole_mask_a = 0
    for index in holes_a:
        hole_mask_a |= 1 << index
    hole_mask_b = 0
    for index in holes_b:
        hole_mask_b |= 1 << index
    operand_a = z3.BitVec("two_operand_a", a_width)
    operand_b = z3.BitVec("two_operand_b", b_width)
    accumulator = (
        z3.BitVec("two_operand_accumulator", sink_width) if accumulate else None
    )
    bit = _cone_bit_builder(
        cone, z3, None, accumulator,
        operands={"input_a": operand_a, "input_b": operand_b},
    )
    if not cone["cells"]:
        raise OperatorFactsError("cone contains no LUT or CARRY logic")
    cone_value = _bits_to_bitvector(z3, [bit(src) for src in cone["outputs"]])
    product_width = a_width + b_width

    def observe(a_value: int, b_value: int, acc_value: int = 0) -> int:
        pairs = [
            (operand_a, z3.BitVecVal(a_value & ~hole_mask_a, a_width)),
            (operand_b, z3.BitVecVal(b_value & ~hole_mask_b, b_width)),
        ]
        if accumulate:
            pairs.append((accumulator, z3.BitVecVal(acc_value, sink_width)))
        value = z3.simplify(z3.substitute(cone_value, *pairs))
        if not isinstance(value, z3.BitVecNumRef):
            raise OperatorFactsError("cone did not evaluate to a constant")
        return value.as_long()

    present_a = [
        index for index in range(a_width) if not (hole_mask_a >> index) & 1
    ]
    present_b = [
        index for index in range(b_width) if not (hole_mask_b >> index) & 1
    ]
    if len(present_a) < 2 or len(present_b) < 2:
        raise OperatorFactsError(
            "cone operand buses must each present at least two data bits"
        )

    # --- shift discovery from concrete single-bit probes -------------------
    base_b = present_b[0]
    shift = None
    for probe_bit in present_a:
        observed = observe(1 << probe_bit, 1 << base_b)
        if observed == 0 or observed & (observed - 1):
            continue  # zero (below the slice) or sign-smeared: keep probing
        candidate = probe_bit + base_b - (observed.bit_length() - 1)
        if 0 <= candidate <= max_shift:
            shift = candidate
            break
    if shift is None or product_width <= shift:
        raise OperatorFactsError(_TWO_OPERAND_REJECT)
    term_width = min(sink_width, product_width - shift)

    # --- signedness selection from concrete sign probes --------------------
    all_ones_a = ((1 << a_width) - 1) & ~hole_mask_a
    all_ones_b = ((1 << b_width) - 1) & ~hole_mask_b
    sign_probes = [
        (all_ones_a, 1 << base_b),
        (1 << present_a[0], all_ones_b),
    ]
    observed_signs = [observe(*probe) for probe in sign_probes]

    def as_signed(value: int, width: int) -> int:
        return value - (1 << width) if (value >> (width - 1)) & 1 else value

    def predict(
        a_value: int, b_value: int,
        a_signed: bool, b_signed: bool, term_signed: bool,
        acc_value: int = 0,
    ) -> int:
        left = as_signed(a_value, a_width) if a_signed else a_value
        right = as_signed(b_value, b_width) if b_signed else b_value
        product = (left * right) % (1 << product_width)
        term = (product >> shift) & ((1 << term_width) - 1)
        resized = as_signed(term, term_width) if term_signed else term
        return (acc_value + resized) % (1 << sink_width)

    combos = [(False, False), (True, False), (False, True), (True, True)]
    candidates = [
        (a_signed, b_signed)
        for a_signed, b_signed in combos
        if all(
            predict(pa, pb, a_signed, b_signed, a_signed or b_signed) == observed
            for (pa, pb), observed in zip(sign_probes, observed_signs)
        )
    ]
    if not candidates:
        raise OperatorFactsError(_TWO_OPERAND_REJECT)

    # --- exact bit-vector verification -------------------------------------
    attempts = 0

    def verify(a_signed: bool, b_signed: bool, term_signed: bool) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts > _TWO_OPERAND_MAX_Z3_ATTEMPTS:
            raise OperatorFactsError("z3_attempt_budget_exhausted")
        extend_a = z3.SignExt if a_signed else z3.ZeroExt
        extend_b = z3.SignExt if b_signed else z3.ZeroExt
        product = extend_a(product_width - a_width, operand_a) * extend_b(
            product_width - b_width, operand_b
        )
        term = z3.Extract(shift + term_width - 1, shift, product)
        if term_width == sink_width:
            resized = term
        else:
            resized = (z3.SignExt if term_signed else z3.ZeroExt)(
                sink_width - term_width, term
            )
        reference = accumulator + resized if accumulate else resized
        verifier = z3.Solver()
        verifier.set(timeout=timeout_ms)
        verifier.add(cone_value != reference)
        for index in holes_a:
            verifier.add(
                z3.Extract(index, index, operand_a) == z3.BitVecVal(0, 1)
            )
        for index in holes_b:
            verifier.add(
                z3.Extract(index, index, operand_b) == z3.BitVecVal(0, 1)
            )
        verdict = verifier.check()
        if verdict == z3.unknown:
            raise OperatorFactsError(
                "two-operand verification returned unknown"
            )
        return verdict == z3.unsat

    proved = None
    for a_signed, b_signed in candidates:
        term_signed = a_signed or b_signed
        if verify(a_signed, b_signed, term_signed):
            proved = (a_signed, b_signed, term_signed)
            break
        # The resize is only semantically visible on narrow terms; when the
        # widths are equal the flipped check is the identical formula.
        if term_width != sink_width and verify(
            a_signed, b_signed, not term_signed
        ):
            proved = (a_signed, b_signed, not term_signed)
            break
    if proved is None:
        raise OperatorFactsError(_TWO_OPERAND_REJECT)
    return {
        "status": "proved",
        "method": (
            "concrete_probe_shift_and_signedness_with_exact_bitvector_"
            "verification"
        ),
        "a_width": a_width,
        "b_width": b_width,
        "product_width": product_width,
        "shift": shift,
        "term_width": term_width,
        "sink_width": sink_width,
        "accumulate": accumulate,
        "a_signed": proved[0],
        "b_signed": proved[1],
        "term_signed": proved[2],
        "z3_attempts": attempts,
        "verification": {
            "name": "two_operand_cone_equals_sliced_product",
            "result": "unsat",
        },
    }


def build_two_operand_product_facts(
    recovery: dict[str, Any],
    boundary: dict[str, Any],
    *,
    sink_width: int,
    design_sha256: str,
) -> dict[str, Any]:
    """Bind one proved two-operand cone into hash-bound extracted facts."""
    if not isinstance(recovery, dict) or recovery.get("status") != "proved":
        raise OperatorFactsError("cone recovery is absent or unproved")
    if not isinstance(design_sha256, str) \
            or _SHA256_HEX_RE.fullmatch(design_sha256) is None:
        raise OperatorFactsError("design_sha256 must be a lowercase SHA-256 digest")
    required = {
        "input_bus_a", "input_bus_b", "output_bus", "cone_cells",
        "clock_pin", "ce_pin",
    }
    if not isinstance(boundary, dict) or not required <= set(boundary):
        raise OperatorFactsError("cone boundary facts are incomplete")
    if sink_width != recovery.get("sink_width"):
        raise OperatorFactsError(
            "sink bus width differs from the proved accumulator width"
        )
    spec = {
        "a_width": recovery["a_width"],
        "b_width": recovery["b_width"],
        "product_width": recovery["a_width"] + recovery["b_width"],
        "shift": recovery["shift"],
        "term_width": recovery["term_width"],
        "accumulator_width": sink_width,
        "product_latency": 1,
        "a_signed": bool(recovery["a_signed"]),
        "b_signed": bool(recovery["b_signed"]),
        "term_signed": bool(recovery["term_signed"]),
        "overflow": "wrap",
    }
    payload = {
        "kind": "two_operand_product_contract_facts",
        "version": TWO_OPERAND_FACTS_VERSION,
        "status": "success",
        "design_sha256": design_sha256,
        "proof_status": "two_operand_facts_extracted_unproved",
        "mutation_eligible": False,
        "spec": spec,
        "boundary": {
            "input_bus_a": list(boundary["input_bus_a"]),
            "input_bus_b": list(boundary["input_bus_b"]),
            "output_bus": list(boundary["output_bus"]),
            "cone_cells": list(boundary["cone_cells"]),
            "clock_pin": boundary["clock_pin"],
            "ce_pin": boundary["ce_pin"],
        },
    }
    # The self-digest follows the recurrence wrapper-key convention: the
    # region digest is computed with region_sha256 and the wrapper keys
    # (candidate_id, fresh_rediscovery, runtime_s) absent from the payload.
    region_sha256 = _digest(payload)
    facts = dict(payload)
    facts["fresh_rediscovery"] = True
    facts["candidate_id"] = "twooperand:" + region_sha256[:20]
    facts["region_sha256"] = region_sha256
    return facts


def _trace_two_operand_cone(
    design,
    netlist,
    sink_base: str,
    sink_ffs: list[str],
    sparse_buses: dict[str, dict[int, str]],
) -> tuple[dict[str, Any], dict[str, Any], int]:
    """Trace one sink FF bus into a two-data-bus variable product cone."""
    cells, outputs, boundary_refs, parsed_boundary = _walk_registered_cone(
        design, netlist, sink_base, sink_ffs, sparse_buses
    )
    data_bases = sorted({
        base for base, _index in parsed_boundary.values() if base != sink_base
    })
    if len(data_bases) != 2:
        raise OperatorFactsError(
            "cone input boundary is not two registered data buses"
        )
    # Deterministic bus role assignment: bus A is the WIDER bus; ties break
    # toward the lexicographically smaller base name. Both buses are recorded.
    widths = {base: max(sparse_buses[base]) + 1 for base in data_bases}
    base_a = sorted(data_bases, key=lambda base: (-widths[base], base))[0]
    base_b = next(base for base in data_bases if base != base_a)
    uses_accumulator = any(
        base == sink_base for base, _index in parsed_boundary.values()
    )
    sink_start = int(_BUS_NAME_RE.fullmatch(sink_ffs[0]).group("index"))
    for reference in boundary_refs:
        base, index = parsed_boundary[reference["cell"]]
        reference.pop("cell", None)
        if base == sink_base:
            reference["kind"] = "acc"
        elif base == base_a:
            reference["kind"] = "input_a"
        else:
            reference["kind"] = "input_b"
        reference["bit"] = index - sink_start if base == sink_base else index

    # Sparse data buses: bit positions are the raw indices; indices with no
    # FF cell at all are holes tied to zero (never renormalized away).
    bus_a = sparse_buses[base_a]
    bus_b = sparse_buses[base_b]
    a_width = widths[base_a]
    b_width = widths[base_b]
    input_hole_bits_a = [
        index for index in range(a_width) if index not in bus_a
    ]
    input_hole_bits_b = [
        index for index in range(b_width) if index not in bus_b
    ]
    data_ffs = [bus_a[index] for index in sorted(bus_a)]
    data_ffs.extend(bus_b[index] for index in sorted(bus_b))
    sink_clock_pins = [_hier_pin(netlist, f"{name}/C") for name in sink_ffs]
    data_clock_pins = [_hier_pin(netlist, f"{name}/C") for name in data_ffs]
    _shared_canonical_net(
        netlist, sink_clock_pins + data_clock_pins, "cone clock boundary"
    )
    sink_ce_pins = [_hier_pin(netlist, f"{name}/CE") for name in sink_ffs]
    _shared_canonical_net(netlist, sink_ce_pins, "sink CE boundary")

    cone = {
        "a_width": a_width,
        "b_width": b_width,
        "accumulator_width": len(sink_ffs) if uses_accumulator else None,
        "input_hole_bits_a": input_hole_bits_a,
        "input_hole_bits_b": input_hole_bits_b,
        "cells": cells,
        "outputs": outputs,
    }
    boundary = {
        "input_bus_a": [
            {"cell": bus_a[index], "pin": f"{bus_a[index]}/Q"}
            if index in bus_a else {"const": 0}
            for index in range(a_width)
        ],
        "input_bus_b": [
            {"cell": bus_b[index], "pin": f"{bus_b[index]}/Q"}
            if index in bus_b else {"const": 0}
            for index in range(b_width)
        ],
        "output_bus": [{"cell": name, "pin": f"{name}/D"} for name in sink_ffs],
        "cone_cells": [
            {"cell": name, "type": cells[name]["type"]}
            for name in sorted(cells)
        ],
        "clock_pin": f"{sink_ffs[0]}/C",
        "ce_pin": f"{sink_ffs[0]}/CE",
    }
    return cone, boundary, len(sink_ffs)


def recover_two_operand_contract(
    design,
    design_sha256: str,
    max_candidates: int = 8,
) -> dict[str, Any]:
    """Autonomously recover proved fabric two-operand product cones.

    Discovery mirrors the fixed-point path: every complete FF sink bus is
    tried; cones walk backward through LUT1-6/CARRY4/CARRY8 primitives only,
    are bounded to the shared cell cap, and must present exactly TWO
    registered data buses (optionally plus the sink bus itself as a wrap
    accumulator). Single-bus cones are NOT reported here; they belong to
    recover_fixed_point_product_contract. Semantics are recovered exactly
    from LUT INIT truth tables and carry chains with a z3 bit-vector unsat
    verification (concrete probes select shift and signedness first).
    """
    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) \
            or not 1 <= max_candidates <= 64:
        raise OperatorFactsError("max_candidates must be 1..64")
    if not isinstance(design_sha256, str) \
            or _SHA256_HEX_RE.fullmatch(design_sha256) is None:
        raise OperatorFactsError("design_sha256 must be a lowercase SHA-256 digest")
    netlist = design.getNetlist()
    buses = _fixed_point_bus_groups(design)
    sparse_buses = _fixed_point_sparse_bus_groups(design)
    candidates = []
    rejections: dict[str, int] = {}
    for sink_base in sorted(buses):
        if len(candidates) >= max_candidates:
            break
        try:
            cone, boundary, sink_width = _trace_two_operand_cone(
                design, netlist, sink_base, buses[sink_base], sparse_buses
            )
            recovery = recover_cone_two_operand(cone)
            facts = build_two_operand_product_facts(
                recovery, boundary,
                sink_width=sink_width,
                design_sha256=design_sha256,
            )
        except OperatorFactsError as exc:
            reason = str(exc)
            rejections[reason] = rejections.get(reason, 0) + 1
            continue
        candidates.append(facts)
    result = {
        "status": "success",
        "kind": "two_operand_product_contract_facts_set",
        "version": TWO_OPERAND_FACTS_VERSION,
        "design_sha256": design_sha256,
        "proof_status": "two_operand_facts_extracted_unproved",
        "mutation_eligible": False,
        "fresh_rediscovery": True,
        "candidates": candidates,
        "candidate_count": len(candidates),
    }
    if not candidates:
        result["reason"] = (
            "no FF-bounded LUT/CARRY cone proved an exact two-operand "
            "variable product"
        )
        result["rejection_histogram"] = dict(sorted(rejections.items())[:16])
    return result


_MEMORY_PRIMITIVES = {"RAMB18E2", "RAMB36E2"}
_MEMORY_REQUIRED_PROPERTIES = {
    "READ_WIDTH_A",
    "READ_WIDTH_B",
    "WRITE_WIDTH_A",
    "WRITE_WIDTH_B",
    "DOA_REG",
    "DOB_REG",
    "WRITE_MODE_A",
    "WRITE_MODE_B",
    "RDADDR_COLLISION_HWCONFIG",
}
_MEMORY_MODES = {"SDP", "TDP"}
_MEMORY_WRITE_MODES = {"NO_CHANGE", "READ_FIRST", "WRITE_FIRST"}
_RTL_RAM_TYPE_MODES = {"RAM_SDP": "SDP", "RAM_TDP": "TDP"}


def _memory_property_int(properties: dict[str, str], key: str) -> int:
    raw = str(properties.get(key, "")).strip()
    if re.fullmatch(r"\d+", raw) is None:
        raise OperatorFactsError(f"memory property {key} is not an unsigned integer")
    return int(raw)


def _memory_pin_connectivity(netlist, logical_cell) -> tuple[list[dict[str, str]], dict[str, int]]:
    records = []
    families: dict[str, set[int]] = {}
    scalar = set()
    for pin in sorted(_as_list(logical_cell.getHierPortInsts()), key=lambda item: _pin_name(item)):
        name = _pin_name(pin)
        match = _VECTOR_PIN_RE.fullmatch(name)
        if match is None:
            scalar.add(name)
        else:
            families.setdefault(match.group("name"), set()).add(int(match.group("index")))
        try:
            net = _parent_net(netlist, pin)
            net_name = _net_name(net)
        except OperatorFactsError:
            net_name = ""
        records.append({
            "pin": name,
            "direction": "input" if bool(pin.isInput()) else "output",
            "net": net_name,
        })
    widths = {name: len(indices) for name, indices in sorted(families.items())}
    widths.update({name: 1 for name in sorted(scalar)})
    return records, dict(sorted(widths.items()))


def build_memory_primitive_contract_facts(
    *,
    design_sha256: str,
    cell_name: str,
    primitive: str,
    properties: dict[str, str],
    connectivity: list[dict[str, str]],
    connected_pin_widths: dict[str, int],
    physical: dict[str, Any],
) -> dict[str, Any]:
    """Bind one primitive's DCP-derived memory semantics without user knobs."""
    if not isinstance(design_sha256, str) or _SHA256_HEX_RE.fullmatch(design_sha256) is None:
        raise OperatorFactsError("design_sha256 must be a lowercase SHA-256 digest")
    if not isinstance(cell_name, str) or not cell_name:
        raise OperatorFactsError("memory cell name is unavailable")
    primitive = str(primitive).upper()
    if primitive not in _MEMORY_PRIMITIVES:
        raise OperatorFactsError("memory primitive is unsupported")
    if not isinstance(properties, dict):
        raise OperatorFactsError("memory semantic properties are incomplete")
    normalized_properties = {
        str(key).upper(): str(value).strip().upper()
        for key, value in properties.items()
    }
    if not _MEMORY_REQUIRED_PROPERTIES <= set(normalized_properties):
        raise OperatorFactsError("memory semantic properties are incomplete")
    if "RAM_MODE" in normalized_properties:
        mode = normalized_properties["RAM_MODE"]
        mode_source = "RAM_MODE"
    else:
        mode = _RTL_RAM_TYPE_MODES.get(normalized_properties.get("RTL_RAM_TYPE"))
        mode_source = "RTL_RAM_TYPE"
    if mode not in _MEMORY_MODES:
        raise OperatorFactsError("memory mode is not explicit in DCP properties")
    write_modes = {
        port: normalized_properties[f"WRITE_MODE_{port}"] for port in ("A", "B")
    }
    if any(value not in _MEMORY_WRITE_MODES for value in write_modes.values()):
        raise OperatorFactsError("memory write mode is unsupported")
    ports = {}
    for port in ("A", "B"):
        read_width = _memory_property_int(normalized_properties, f"READ_WIDTH_{port}")
        write_width = _memory_property_int(normalized_properties, f"WRITE_WIDTH_{port}")
        output_register = _memory_property_int(normalized_properties, f"DO{port}_REG")
        if not 0 <= output_register <= 1:
            raise OperatorFactsError("memory output register property must be 0 or 1")
        ports[port.lower()] = {
            "read_width": read_width,
            "write_width": write_width,
            "output_register": output_register,
            "write_mode": write_modes[port],
        }
    if mode == "TDP" and any(
        ports[port]["read_width"] == 0 and ports[port]["write_width"] == 0
        for port in ("a", "b")
    ):
        raise OperatorFactsError("TDP memory has an inactive logical port")
    if not isinstance(connectivity, list) or not connectivity:
        raise OperatorFactsError("memory pin connectivity is unavailable")
    normalized_connectivity = []
    seen_pins = set()
    for record in connectivity:
        if not isinstance(record, dict) or set(record) != {"pin", "direction", "net"}:
            raise OperatorFactsError("memory pin connectivity has an unsupported schema")
        pin = record.get("pin")
        direction = record.get("direction")
        net = record.get("net")
        if (
            not isinstance(pin, str)
            or not pin
            or pin in seen_pins
            or direction not in {"input", "output"}
            or not isinstance(net, str)
        ):
            raise OperatorFactsError("memory pin connectivity is invalid")
        seen_pins.add(pin)
        normalized_connectivity.append({
            "pin": pin,
            "direction": direction,
            "net": net,
        })
    normalized_connectivity.sort(key=lambda item: item["pin"])
    if not isinstance(connected_pin_widths, dict):
        raise OperatorFactsError("memory connected pin widths are unavailable")
    pin_widths = {}
    for name, width in connected_pin_widths.items():
        if (
            not isinstance(name, str)
            or not name
            or isinstance(width, bool)
            or not isinstance(width, int)
            or width < 1
        ):
            raise OperatorFactsError("memory connected pin width is invalid")
        pin_widths[name] = width
    if (
        not isinstance(physical, dict)
        or set(physical) != {"cell", "type", "site", "bel"}
        or physical.get("cell") != cell_name
        or str(physical.get("type") or "").upper() != primitive
    ):
        raise OperatorFactsError("memory physical binding is invalid")
    initialization = {
        key: value
        for key, value in normalized_properties.items()
        if key.startswith("INIT") or key.startswith("SRVAL")
    }
    spec = {
        "primitive": primitive,
        "ram_mode": mode,
        "ram_mode_source": mode_source,
        "ports": ports,
        "collision": {
            "read_address": normalized_properties["RDADDR_COLLISION_HWCONFIG"],
            "simulation_check": normalized_properties.get("SIM_COLLISION_CHECK"),
        },
        "connected_pin_widths": dict(sorted(pin_widths.items())),
        "initialization_sha256": _digest(initialization),
        "initialization_property_count": len(initialization),
        "property_sha256": _digest(normalized_properties),
    }
    payload = {
        "kind": "memory_primitive_contract_facts",
        "version": 1,
        "status": "success",
        "design_sha256": design_sha256,
        "proof_status": "memory_primitive_facts_extracted_unproved",
        "mutation_eligible": False,
        "spec": spec,
        "boundary": {
            "cell": cell_name,
            "physical": dict(physical),
            "pins": normalized_connectivity,
        },
        "properties": dict(sorted(normalized_properties.items())),
    }
    region_sha256 = _digest(payload)
    facts = dict(payload)
    facts.update({
        "fresh_rediscovery": True,
        "candidate_id": "memory:" + region_sha256[:20],
        "region_sha256": region_sha256,
    })
    return facts


def recover_memory_primitive_contracts(
    design,
    design_sha256: str,
    max_candidates: int = 64,
) -> dict[str, Any]:
    """Autonomously recover exact configured BRAM primitive contracts."""
    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) \
            or not 1 <= max_candidates <= 256:
        raise OperatorFactsError("max_candidates must be 1..256")
    if not isinstance(design_sha256, str) \
            or _SHA256_HEX_RE.fullmatch(design_sha256) is None:
        raise OperatorFactsError("design_sha256 must be a lowercase SHA-256 digest")
    netlist = design.getNetlist()
    candidates = []
    rejections: dict[str, int] = {}
    physical_cells = sorted(
        _as_list(design.getCells()), key=lambda cell: str(cell.getName())
    )
    for cell in physical_cells:
        primitive = str(cell.getType()).upper()
        if primitive not in _MEMORY_PRIMITIVES:
            continue
        if len(candidates) >= max_candidates:
            break
        name = str(cell.getName())
        try:
            logical = netlist.getHierCellInstFromName(name)
            if logical is None:
                raise OperatorFactsError("memory logical instance is absent")
            properties = _logical_cell_properties(netlist, name)
            connectivity, pin_widths = _memory_pin_connectivity(netlist, logical)
            candidates.append(build_memory_primitive_contract_facts(
                design_sha256=design_sha256,
                cell_name=name,
                primitive=primitive,
                properties=properties,
                connectivity=connectivity,
                connected_pin_widths=pin_widths,
                physical=_physical_cell_fact(design, name),
            ))
        except OperatorFactsError as exc:
            reason = str(exc)
            rejections[reason] = rejections.get(reason, 0) + 1
    result = {
        "status": "success",
        "kind": "memory_primitive_contract_facts_set",
        "version": 1,
        "design_sha256": design_sha256,
        "proof_status": "memory_primitive_facts_extracted_unproved",
        "mutation_eligible": False,
        "fresh_rediscovery": True,
        "candidates": candidates,
        "candidate_count": len(candidates),
    }
    if not candidates:
        result["reason"] = "no configured BRAM primitive exposed a complete semantic contract"
        result["rejection_histogram"] = dict(sorted(rejections.items())[:16])
    return result


def build_memory_consumer_dependency_facts(
    design,
    memory_facts: dict[str, Any],
) -> dict[str, Any]:
    """Recover exact leaf consumers of one hash-bound memory output boundary."""
    if not isinstance(memory_facts, dict):
        raise OperatorFactsError("memory producer facts must be an object")
    producer_region = memory_facts.get("region_sha256")
    producer_id = memory_facts.get("candidate_id")
    payload = dict(memory_facts)
    payload.pop("region_sha256", None)
    for key in ("candidate_id", "fresh_rediscovery", "runtime_s"):
        payload.pop(key, None)
    if (
        memory_facts.get("kind") != "memory_primitive_contract_facts"
        or memory_facts.get("fresh_rediscovery") is not True
        or not isinstance(producer_region, str)
        or _digest(payload) != producer_region
        or producer_id != "memory:" + producer_region[:20]
    ):
        raise OperatorFactsError("memory producer facts do not replay")
    boundary = memory_facts.get("boundary")
    if not isinstance(boundary, dict) or not isinstance(boundary.get("pins"), list):
        raise OperatorFactsError("memory producer boundary is unavailable")
    cell_name = boundary.get("cell")
    if not isinstance(cell_name, str) or not cell_name:
        raise OperatorFactsError("memory producer cell is unavailable")
    netlist = design.getNetlist()
    logical = netlist.getHierCellInstFromName(cell_name)
    if logical is None:
        raise OperatorFactsError("memory producer logical cell is absent")
    source_pins = {
        _pin_name(pin): pin for pin in _as_list(logical.getHierPortInsts())
    }
    outputs = []
    edges = []
    consumers: dict[tuple[str, str], dict[str, Any]] = {}
    output_boundary = sorted(
        (
            record for record in boundary["pins"]
            if isinstance(record, dict) and record.get("direction") == "output"
        ),
        key=lambda record: str(record.get("pin") or ""),
    )
    if not output_boundary:
        raise OperatorFactsError("memory producer has no connected output boundary")
    for record in output_boundary:
        if set(record) != {"pin", "direction", "net"}:
            raise OperatorFactsError("memory output boundary has an unsupported schema")
        pin_name = record.get("pin")
        expected_net = record.get("net")
        source_pin = source_pins.get(pin_name)
        if source_pin is None:
            raise OperatorFactsError("memory output pin disappeared from the DCP")
        try:
            parent = _parent_net(netlist, source_pin)
            net_name = _net_name(parent)
        except OperatorFactsError:
            if expected_net:
                raise
            outputs.append({"source_pin": pin_name, "net": "", "sinks": []})
            continue
        if net_name != expected_net:
            raise OperatorFactsError("memory output canonical net changed")
        sink_records = []
        for sink in _as_list(parent.getLeafHierPortInsts(False, True, True)):
            sink_cell = _cell_name(sink)
            sink_pin = _pin_name(sink)
            sink_type = _cell_type(sink)
            property_sha256 = _digest(
                _instance_properties(sink) if sink_type else {}
            )
            sink_record = {
                "cell": sink_cell,
                "pin": sink_pin,
                "type": sink_type,
                "property_sha256": property_sha256,
            }
            sink_records.append(sink_record)
            consumer = {
                "cell": sink_cell,
                "type": sink_type,
                "property_sha256": property_sha256,
            }
            consumer_key = (sink_cell, sink_type)
            if consumer_key in consumers and consumers[consumer_key] != consumer:
                raise OperatorFactsError("memory consumer properties are inconsistent")
            consumers[consumer_key] = consumer
            edges.append({
                "producer_candidate_id": producer_id,
                "source_pin": pin_name,
                "net": net_name,
                "consumer_cell": sink_cell,
                "consumer_pin": sink_pin,
                "consumer_type": sink_type,
            })
        sink_records.sort(key=lambda item: (item["cell"], item["pin"], item["type"]))
        outputs.append({
            "source_pin": pin_name,
            "net": net_name,
            "sinks": sink_records,
        })
    edges.sort(key=lambda item: (
        item["source_pin"], item["consumer_cell"], item["consumer_pin"]
    ))
    contract = {
        "kind": "memory_consumer_dependency_facts",
        "version": 1,
        "status": "success",
        "design_sha256": memory_facts["design_sha256"],
        "producer": {
            "candidate_id": producer_id,
            "region_sha256": producer_region,
            "cell": cell_name,
            "primitive": memory_facts["spec"]["primitive"],
        },
        "outputs": outputs,
        "consumer_cells": sorted(
            consumers.values(), key=lambda item: (item["cell"], item["type"])
        ),
        "dependency_edges": edges,
        "consumer_count": len(consumers),
        "fanout_extraction_complete": True,
        "proof_status": "memory_consumer_facts_extracted_unproved",
        "mutation_eligible": False,
    }
    region_sha256 = _digest(contract)
    contract.update({
        "fresh_rediscovery": True,
        "candidate_id": "memoryconsumer:" + region_sha256[:20],
        "region_sha256": region_sha256,
    })
    return contract


def recover_memory_consumer_dependencies(
    design,
    design_sha256: str,
    max_candidates: int = 64,
) -> dict[str, Any]:
    """Recover complete immediate consumer dependencies for configured BRAMs."""
    memory_set = recover_memory_primitive_contracts(
        design, design_sha256, max_candidates=max_candidates
    )
    candidates = [
        build_memory_consumer_dependency_facts(design, facts)
        for facts in memory_set["candidates"]
    ]
    return {
        "status": "success",
        "kind": "memory_consumer_dependency_facts_set",
        "version": 1,
        "design_sha256": design_sha256,
        "proof_status": "memory_consumer_facts_extracted_unproved",
        "mutation_eligible": False,
        "fresh_rediscovery": True,
        "candidates": candidates,
        "candidate_count": len(candidates),
    }


def propose_two_operand_placement_candidates(
    design,
    design_sha256: str,
    region_sha256: str,
    max_sites: int = 64,
) -> dict[str, Any]:
    """Rank idle DSP48E2 sites by Manhattan distance from the cone centroid."""
    if isinstance(max_sites, bool) or not isinstance(max_sites, int) \
            or not 1 <= max_sites <= 256:
        raise OperatorFactsError("max_sites must be 1..256")
    if not isinstance(region_sha256, str) \
            or _SHA256_HEX_RE.fullmatch(region_sha256) is None:
        raise OperatorFactsError("region_sha256 must be a lowercase SHA-256 digest")
    facts_set = recover_two_operand_contract(design, design_sha256)
    matches = [
        item for item in facts_set.get("candidates", [])
        if item.get("region_sha256") == region_sha256
    ]
    if len(matches) != 1:
        raise OperatorFactsError(
            "region is absent from the fresh two-operand rediscovery"
        )
    boundary = matches[0]["boundary"]
    coordinates = []
    for entry in [
        *boundary["input_bus_a"], *boundary["input_bus_b"],
        *boundary["output_bus"],
    ]:
        if "cell" not in entry:
            continue  # const hole entries have no placed cell
        name = str(entry["cell"])
        cell = design.getCell(name)
        if cell is None or not bool(cell.isPlaced()) or cell.getSite() is None:
            raise OperatorFactsError(f"boundary cell is absent or unplaced: {name}")
        tile = cell.getSite().getTile()
        if tile is None:
            raise OperatorFactsError(f"boundary cell tile is unavailable: {name}")
        coordinates.append((
            int(tile.getTileXCoordinate()), int(tile.getTileYCoordinate())
        ))
    centroid_x = round(sum(x for x, _y in coordinates) / len(coordinates))
    centroid_y = round(sum(y for _x, y in coordinates) / len(coordinates))
    slots = []
    for site in _as_list(design.getDevice().getAllSites()):
        if str(site.getSiteTypeEnum()).upper() != "DSP48E2":
            continue
        site_inst = design.getSiteInstFromSite(site)
        if site_inst is not None and _as_list(site_inst.getCells()):
            continue
        tile = site.getTile()
        if tile is None:
            continue
        distance = (
            abs(int(tile.getTileXCoordinate()) - centroid_x)
            + abs(int(tile.getTileYCoordinate()) - centroid_y)
        )
        slots.append({
            "site": str(site.getName()),
            "bel": "DSP48E2",
            "distance": distance,
            "clock_route_ok": True,
            "source_dcp_sha256": design_sha256,
            "region_sha256": region_sha256,
        })
    slots.sort(key=lambda item: (item["distance"], item["site"]))
    return {
        "status": "success",
        "source_dcp_sha256": design_sha256,
        "region_sha256": region_sha256,
        "fresh_rediscovery": True,
        "slots": slots[:max_sites],
    }
