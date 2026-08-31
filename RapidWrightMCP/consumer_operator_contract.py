"""Live, name-private recovery of consumer-driven registered arithmetic.

The public Agent sees only dimensions, control semantics, wall coverage and
competing operator classes.  Exact hierarchy, nets and the executable bundle
remain in the trusted probe handoff.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
import re
from typing import Any, Optional

try:
    from .sequential_contract import recover_sequential_operator_contract
except ImportError:
    from sequential_contract import recover_sequential_operator_contract


_INDEXED_PORT = re.compile(r"^(?P<stem>.+)\[(?P<index>\d+)\]$")
_INDEX_TOKEN = re.compile(r"\[(\d+)\]")
_REGISTER_TYPES = {"FDRE", "FDSE", "FDCE", "FDPE"}
_QUOTIENT_ROLE = re.compile(r"quo(?:tient)?", re.IGNORECASE)
_NUMERATOR_ROLE = re.compile(r"numer", re.IGNORECASE)
_DENOMINATOR_ROLE = re.compile(r"denom|divisor", re.IGNORECASE)
_OVERFLOW_ROLE = re.compile(r"overflow", re.IGNORECASE)
_STAGE_ROLE = re.compile(
    r"(?:stage|quo(?:tient)?|pipe|result|reg)[^0-9]{0,8}(\d+)",
    re.IGNORECASE,
)


class ConsumerOperatorContractError(ValueError):
    """Raised when a live consumer contract is missing or ambiguous."""


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
    return hashlib.sha256(json.dumps(
        _stable(value), sort_keys=True, separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")).hexdigest()


def _rewrite_identifiers(prefix: str, selected: dict[str, Any]) -> tuple[str, str]:
    """Return deterministic private module/instance names for one live cut.

    Consumer reconstruction is compositional: a promoted DCP may already
    contain an earlier generated rewrite while a different current-wall owner
    needs the same operator template.  Fixed global names make RapidWright
    resolve the old instance and Vivado then rejects reconnecting its pins.
    Bind both identities to the exact private owner/output cut so independent
    current-seed rewrites can coexist without exposing object names publicly.
    """
    suffix = _digest({
        "operator": selected.get("operator"),
        "owner_name": selected.get("owner_name"),
        "distance": selected.get("distance"),
        "output_nets": [
            str(net)
            for group in (
                selected.get("quotient_groups")
                or [selected.get("output_group") or {}]
            )
            if isinstance(group, dict)
            for net in (group.get("nets") or [])
        ],
    })[:12]
    module_name = f"{prefix}_{suffix}"
    return module_name, f"{module_name}_inst"


def _current_seed_rewrite_realization(netlist, bundle: Optional[dict]) -> dict:
    """Return a name-private census of this exact rewrite in the live seed."""
    if not isinstance(bundle, dict):
        return {
            "status": "not_applicable",
            "expected_instance_count": 0,
            "realized_instance_count": 0,
        }
    instances = list(bundle.get("instances") or [])
    realized_count = sum(
        netlist.getHierCellInstFromName(str(instance.get("name") or ""))
        is not None
        for instance in instances
        if isinstance(instance, dict)
    )
    expected_count = len(instances)
    if realized_count == 0:
        status = "not_realized"
    elif realized_count == expected_count:
        status = "already_realized_current_seed"
    else:
        status = "partial_namespace_conflict_current_seed"
    return {
        "status": status,
        "expected_instance_count": expected_count,
        "realized_instance_count": realized_count,
    }


def _parent_net(netlist, pin):
    hierarchical = pin.getHierarchicalNet()
    if hierarchical is None:
        raise ConsumerOperatorContractError("boundary pin has no hierarchical net")
    parent = netlist.getParentNet(hierarchical)
    if parent is None:
        raise ConsumerOperatorContractError("boundary pin has no canonical parent net")
    return parent


def _net_name(net) -> str:
    return str(net.getHierarchicalNetName())


def _pin_name(pin) -> str:
    return str(pin.getPortInst().getName())


def _cell_name(pin) -> str:
    return str(pin.getFullHierarchicalInstName())


def _cell_type(pin) -> str:
    cell_type = pin.getCellType()
    return str(cell_type.getName()).upper() if cell_type is not None else ""


def _properties(pin) -> dict[str, str]:
    values = pin.getPortInst().getCellInst().getPropertiesMap()
    result = {}
    try:
        entries = values.entrySet()
        for entry in entries:
            value = entry.getValue()
            result[str(entry.getKey())] = str(value.getValue())
    except AttributeError:
        for key, value in dict(values).items():
            raw = value.getValue() if hasattr(value, "getValue") else value
            result[str(key)] = str(raw)
    return result


def _common_parent(names: list[str]) -> str:
    parents = [str(name).rsplit("/", 1)[0] for name in names if "/" in str(name)]
    if not parents:
        raise ConsumerOperatorContractError("registered family has no hierarchy owner")
    tokens = [item.split("/") for item in parents]
    common = []
    for values in zip(*tokens):
        if len(set(values)) != 1:
            break
        common.append(values[0])
    if not common:
        raise ConsumerOperatorContractError("registered family common owner is top level")
    return "/".join(common)


def _ancestor_names(common_owner: str, limit: int = 8) -> list[str]:
    result = []
    current = str(common_owner)
    while current and len(result) < limit:
        result.append(current)
        current = current.rsplit("/", 1)[0] if "/" in current else ""
    return result


def _indexed_boundary_groups(netlist, owner) -> dict[str, list[dict[str, Any]]]:
    groups: dict[tuple[str, str], dict[int, str]] = {}
    for pin in _as_list(owner.getHierPortInsts()):
        direction = "input" if bool(pin.isInput()) else "output" if bool(pin.isOutput()) else ""
        match = _INDEXED_PORT.fullmatch(_pin_name(pin))
        if not direction or match is None:
            continue
        index = int(match.group("index"))
        key = (direction, match.group("stem"))
        # For a hierarchical boundary pin RapidWright already exposes the
        # externally observable net.  Calling getParentNet() again can move an
        # output back onto its locally-renumbered alias (for example a narrow
        # consumer slice becomes [0:N-1]) and destroys the original bit-role
        # evidence.  Primitive pins still use _parent_net() elsewhere.
        hierarchical_net = pin.getHierarchicalNet()
        if hierarchical_net is None:
            raise ConsumerOperatorContractError(
                "indexed boundary pin has no hierarchical net"
            )
        net = _net_name(hierarchical_net)
        previous = groups.setdefault(key, {}).setdefault(index, net)
        if previous != net:
            raise ConsumerOperatorContractError("indexed boundary bit is ambiguous")
    result: dict[str, list[dict[str, Any]]] = {"input": [], "output": []}
    for (direction, stem), indexed in groups.items():
        indices = sorted(indexed)
        if not indices or indices != list(range(indices[-1] + 1)):
            continue
        result[direction].append({
            "stem": stem,
            "width": len(indices),
            "nets": [indexed[index] for index in indices],
        })
    for rows in result.values():
        rows.sort(key=lambda item: (str(item["stem"]), int(item["width"])))
    return result


def _resolve_path_cell_name(design, token: str) -> Optional[str]:
    """Resolve a timing-path pin token to its physical cell."""
    candidate = str(token)
    while candidate:
        try:
            cell = design.getCell(candidate)
        except Exception:
            cell = None
        if cell is not None:
            return str(cell.getName())
        if "/" not in candidate:
            return None
        candidate = candidate.rsplit("/", 1)[0]
    return None


def _canonical_net_sink_cells(netlist, net_name: str) -> set[str]:
    """Return the leaf sink cells reached by one hierarchy-boundary net."""
    try:
        logical = netlist.getHierNetFromName(str(net_name))
    except Exception:
        logical = None
    if logical is None:
        return set()
    try:
        parent = netlist.getParentNet(logical)
    except Exception:
        parent = None
    parent = parent or logical
    return {
        _cell_name(pin)
        for pin in _as_list(parent.getLeafHierPortInsts(False, True, True))
        if _cell_name(pin)
    }


def _canonical_parent_net(netlist, net_name: str):
    try:
        logical = netlist.getHierNetFromName(str(net_name))
    except Exception:
        logical = None
    if logical is None:
        return None
    try:
        return netlist.getParentNet(logical) or logical
    except Exception:
        return logical


def _canonical_net_name(netlist, net_name: str) -> Optional[str]:
    parent = _canonical_parent_net(netlist, net_name)
    return _net_name(parent) if parent is not None else None


def _indexed_role_net(net_name: str, role: re.Pattern[str]) -> Optional[int]:
    match = _INDEXED_PORT.fullmatch(str(net_name))
    if match is None or role.search(match.group("stem")) is None:
        return None
    return int(match.group("index"))


def _trace_owner_semantic_cut(
    netlist,
    *,
    owner_name: str,
    output_nets: list[str],
    expand_overflow: bool = False,
) -> dict[str, Any]:
    """Trace output shells back to quotient and overflow semantic nets."""
    owner_prefix = owner_name + "/"
    pending = list(output_nets)
    visited = set()
    quotient_nets: dict[int, set[str]] = {}
    overflow_nets = set()
    external_frontier = set()
    primitive_count = 0
    while pending:
        raw_name = str(pending.pop())
        parent = _canonical_parent_net(netlist, raw_name)
        if parent is None:
            continue
        name = _net_name(parent)
        if name in visited:
            continue
        visited.add(name)
        quotient_index = _indexed_role_net(name, _QUOTIENT_ROLE)
        if quotient_index is not None:
            quotient_nets.setdefault(quotient_index, set()).add(name)
            continue
        if not expand_overflow and _OVERFLOW_ROLE.search(name):
            overflow_nets.add(name)
            continue
        sources = _as_list(parent.getLeafHierPortInsts(True, False, True))
        internal_sources = [
            source for source in sources
            if _cell_name(source).startswith(owner_prefix)
        ]
        if len(sources) != 1 or len(internal_sources) != 1:
            external_frontier.add(name)
            continue
        source = internal_sources[0]
        source_type = _cell_type(source)
        if source_type in _REGISTER_TYPES or source_type in {"SRL16E", "SRLC32E"}:
            external_frontier.add(name)
            continue
        if re.fullmatch(r"LUT[1-6]|CARRY[48]|MUXF[789]", source_type) is None:
            external_frontier.add(name)
            continue
        primitive_count += 1
        if primitive_count > 20000:
            raise ConsumerOperatorContractError(
                "consumer shell semantic-cut trace exceeded its bound"
            )
        cell = source.getFullHierarchicalInst()
        for pin in _as_list(cell.getHierPortInsts()):
            if not bool(pin.isInput()):
                continue
            input_parent = _parent_net(netlist, pin)
            if input_parent is not None:
                pending.append(_net_name(input_parent))
    return {
        "quotient_nets": {
            index: sorted(names) for index, names in sorted(quotient_nets.items())
        },
        "overflow_nets": sorted(overflow_nets),
        "external_frontier_nets": sorted(external_frontier),
        "traced_primitive_count": primitive_count,
    }


def _lut_init(pin, width: int) -> Optional[int]:
    raw = str(_properties(pin).get("INIT") or "")
    match = re.fullmatch(rf"{1 << width}'h([0-9a-fA-F]+)", raw)
    if match is None:
        return None
    value = int(match.group(1), 16)
    return value if value < (1 << (1 << width)) else None


def _recover_quotient_or_indices(netlist, net_name: str) -> Optional[list[int]]:
    """Prove that a current-source net is an OR of quotient bits."""
    memo: dict[str, Optional[set[int]]] = {}
    active: set[str] = set()

    def recover(raw_name: str) -> Optional[set[int]]:
        parent = _canonical_parent_net(netlist, raw_name)
        if parent is None:
            return None
        name = _net_name(parent)
        if name in memo:
            cached = memo[name]
            return None if cached is None else set(cached)
        quotient_index = _indexed_role_net(name, _QUOTIENT_ROLE)
        if quotient_index is not None:
            return {quotient_index}
        if name in active:
            return None
        sources = _as_list(parent.getLeafHierPortInsts(True, False, True))
        if len(sources) != 1:
            memo[name] = None
            return None
        source = sources[0]
        source_type = _cell_type(source)
        if source_type == "GND":
            memo[name] = set()
            return set()
        match = re.fullmatch(r"LUT([1-6])", source_type)
        if match is None:
            memo[name] = None
            return None
        width = int(match.group(1))
        init = _lut_init(source, width)
        if init is None:
            memo[name] = None
            return None
        cell = source.getFullHierarchicalInst()
        by_pin = {
            _pin_name(pin): pin
            for pin in _as_list(cell.getHierPortInsts())
            if bool(pin.isInput())
        }
        if set(by_pin) != {f"I{index}" for index in range(width)}:
            memo[name] = None
            return None

        selected_positions = None
        for selected_mask in range(1 << width):
            if all(
                ((init >> assignment) & 1)
                == int(bool(assignment & selected_mask))
                for assignment in range(1 << width)
            ):
                selected_positions = [
                    index for index in range(width)
                    if selected_mask & (1 << index)
                ]
                break
        if selected_positions is None:
            memo[name] = None
            return None
        active.add(name)
        indices: set[int] = set()
        for position in selected_positions:
            try:
                input_name = _net_name(
                    _parent_net(netlist, by_pin[f"I{position}"])
                )
            except ConsumerOperatorContractError:
                active.remove(name)
                memo[name] = None
                return None
            recovered = recover(input_name)
            if recovered is None:
                active.remove(name)
                memo[name] = None
                return None
            indices.update(recovered)
        active.remove(name)
        memo[name] = set(indices)
        return indices

    recovered = recover(net_name)
    return sorted(recovered) if recovered else None


def _recover_primitive_shell(
    netlist,
    *,
    owner_name: str,
    owner,
    output_nets: list[str],
    quotient_low: int,
    quotient_high: int,
    overflow_net: str,
    clock_net: str,
    overflow_semantics: Optional[dict[str, list[int]]] = None,
    semantic_net_bits: Optional[dict[str, int]] = None,
    diagnostics: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Recover an exact combinational owner shell as an anonymous DAG."""
    owner_prefix = owner_name + "/"
    boundary = _indexed_boundary_groups(netlist, owner)
    boundary_inputs = {
        name
        for group in boundary["input"]
        for net in group["nets"]
        if (name := _canonical_net_name(netlist, str(net))) is not None
    }
    overflow_name = _canonical_net_name(netlist, overflow_net)
    semantic_overflows = {
        canonical: sorted(set(indices))
        for raw_name, indices in (overflow_semantics or {}).items()
        if (canonical := _canonical_net_name(netlist, raw_name)) is not None
        and indices
    }
    semantic_bits = {
        canonical: int(index)
        for raw_name, index in (semantic_net_bits or {}).items()
        if (canonical := _canonical_net_name(netlist, raw_name)) is not None
    }
    clock_name = _canonical_net_name(netlist, clock_net)
    external_names: list[str] = []
    external_ordinals: dict[str, int] = {}
    opaque_primitives: dict[str, str] = {}
    nodes: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    active: set[str] = set()
    failure: dict[str, Any] = {}
    cofactor_reduction_count = 0
    cofactor_pruned_input_count = 0

    def refuse(reason: str, **detail: Any) -> None:
        if not failure:
            failure.update({"reason": reason, **detail})

    def external(name: str) -> dict[str, Any]:
        if name not in external_ordinals:
            external_ordinals[name] = len(external_names)
            external_names.append(name)
        return {"kind": "external", "ordinal": external_ordinals[name]}

    def direct_reference(name: str, sources: list) -> Optional[dict[str, Any]]:
        """Resolve semantic/frontier sources without entering an internal cone."""
        if clock_name is not None and name == clock_name:
            return {"kind": "clock"}
        if name in semantic_bits:
            return {"kind": "quotient_bit", "index": semantic_bits[name]}
        quotient_index = _indexed_role_net(name, _QUOTIENT_ROLE)
        if quotient_index is not None and quotient_low <= quotient_index <= quotient_high:
            return {"kind": "quotient_bit", "index": quotient_index}
        if overflow_name is not None and name == overflow_name:
            return {"kind": "overflow"}
        if name in semantic_overflows:
            return {
                "kind": "quotient_or",
                "indices": semantic_overflows[name],
            }
        if name in boundary_inputs:
            return {"kind": "external_name", "name": name}
        internal = [pin for pin in sources if _cell_name(pin).startswith(owner_prefix)]
        if len(sources) == 1 and not internal:
            source_type = _cell_type(sources[0])
            if source_type == "GND":
                return {"kind": "constant", "value": False}
            if source_type == "VCC":
                return {"kind": "constant", "value": True}
            return {"kind": "external_name", "name": name}
        return None

    def materialize(reference: dict[str, Any]) -> dict[str, Any]:
        if reference.get("kind") == "external_name":
            return external(str(reference["name"]))
        return dict(reference)

    def reduced_lut(
        source,
        cell,
        source_type: str,
        input_pins: list,
    ) -> Optional[tuple[str, int, list[dict[str, Any]]] | dict[str, Any]]:
        """Eliminate an internal LUT cone only when exact cofactors prove it dead."""
        nonlocal cofactor_reduction_count, cofactor_pruned_input_count
        match = re.fullmatch(r"LUT([1-6])", source_type)
        if match is None:
            return None
        width = int(match.group(1))
        init = _lut_init(source, width)
        if init is None:
            return None
        by_pin = {_pin_name(pin): pin for pin in input_pins}
        if set(by_pin) != {f"I{index}" for index in range(width)}:
            return None
        direct: list[Optional[dict[str, Any]]] = []
        unresolved_positions = []
        for index in range(width):
            parent = _parent_net(netlist, by_pin[f"I{index}"])
            name = _net_name(parent)
            sources = _as_list(parent.getLeafHierPortInsts(True, False, True))
            reference = direct_reference(name, sources)
            direct.append(reference)
            if reference is None:
                unresolved_positions.append(index)
        if not unresolved_positions:
            return None

        variable_refs: list[dict[str, Any]] = []
        variable_keys: list[tuple] = []
        pin_variables: list[Optional[int]] = []
        for reference in direct:
            if reference is None or reference.get("kind") == "constant":
                pin_variables.append(None)
                continue
            key = tuple(sorted(reference.items()))
            if key not in variable_keys:
                variable_keys.append(key)
                variable_refs.append(reference)
            pin_variables.append(variable_keys.index(key))
        reduced_init = 0
        for assignment in range(1 << len(variable_refs)):
            observed = set()
            for unknown_assignment in range(1 << len(unresolved_positions)):
                address = 0
                unknown_ordinal = 0
                for position, reference in enumerate(direct):
                    if reference is None:
                        bit = (unknown_assignment >> unknown_ordinal) & 1
                        unknown_ordinal += 1
                    elif reference.get("kind") == "constant":
                        bit = int(bool(reference.get("value")))
                    else:
                        bit = (assignment >> int(pin_variables[position])) & 1
                    address |= bit << position
                observed.add((init >> address) & 1)
            if len(observed) != 1:
                return None
            reduced_init |= next(iter(observed)) << assignment
        cofactor_reduction_count += 1
        cofactor_pruned_input_count += len(unresolved_positions)
        if not variable_refs:
            return {"kind": "constant", "value": bool(reduced_init & 1)}
        checked_refs = [materialize(reference) for reference in variable_refs]
        return f"LUT{len(checked_refs)}", reduced_init, [
            {"pin": f"I{index}", "source": reference}
            for index, reference in enumerate(checked_refs)
        ]

    def resolve(raw_name: str) -> Optional[dict[str, Any]]:
        parent = _canonical_parent_net(netlist, raw_name)
        if parent is None:
            refuse("net_unresolved")
            return None
        name = _net_name(parent)
        sources = _as_list(parent.getLeafHierPortInsts(True, False, True))
        direct = direct_reference(name, sources)
        if direct is not None:
            return materialize(direct)
        internal = [pin for pin in sources if _cell_name(pin).startswith(owner_prefix)]
        if len(sources) != 1 or len(internal) != 1:
            refuse(
                "driver_not_unique",
                source_count=len(sources),
                internal_source_count=len(internal),
            )
            return None
        source = internal[0]
        source_type = _cell_type(source)
        if source_type == "GND":
            return {"kind": "constant", "value": False}
        if source_type == "VCC":
            return {"kind": "constant", "value": True}
        if re.fullmatch(r"LUT[1-6]|CARRY[48]|MUXF[789]|FDRE", source_type) is None:
            # The shell only has to reproduce the owner's external behaviour.
            # An internal primitive outside the Boolean algebra keeps driving
            # this net after the rewrite, because the cleanup sweep retains
            # every cell in the fanin of the re-emitted instance.  Read it as
            # an opaque input instead of refusing the whole recovery.
            opaque_primitives[name] = source_type
            return external(name)
        cell_name = _cell_name(source)
        if cell_name in active:
            refuse("combinational_cycle")
            return None
        node = nodes.get(cell_name)
        if node is None:
            active.add(cell_name)
            cell = source.getFullHierarchicalInst()
            input_pins = sorted(
                (pin for pin in _as_list(cell.getHierPortInsts()) if pin.isInput()),
                key=_pin_name,
            )
            reduction = reduced_lut(source, cell, source_type, input_pins)
            if isinstance(reduction, dict):
                active.remove(cell_name)
                return reduction
            if reduction is not None:
                reduced_type, reduced_init, inputs = reduction
                node = {
                    "primitive": reduced_type,
                    "properties": {"init": reduced_init},
                    "inputs": inputs,
                }
                nodes[cell_name] = node
                order.append(cell_name)
                active.remove(cell_name)
                return {"kind": "node", "cell": cell_name, "pin": "O"}
            inputs = []
            for pin in input_pins:
                try:
                    input_name = _net_name(_parent_net(netlist, pin))
                except ConsumerOperatorContractError:
                    refuse("input_net_unresolved", primitive=source_type)
                    active.remove(cell_name)
                    return None
                reference = resolve(input_name)
                if reference is None:
                    active.remove(cell_name)
                    return None
                inputs.append({"pin": _pin_name(pin), "source": reference})
            properties: dict[str, Any] = {}
            lut_match = re.fullmatch(r"LUT([1-6])", source_type)
            if lut_match is not None:
                init = _lut_init(source, int(lut_match.group(1)))
                if init is None:
                    refuse("lut_init_invalid", primitive=source_type)
                    active.remove(cell_name)
                    return None
                properties["init"] = init
            elif source_type in {"CARRY4", "CARRY8"}:
                properties["carry_type"] = str(
                    _properties(source).get("CARRY_TYPE") or "SINGLE_CY8"
                )
            elif source_type == "FDRE":
                raw_init = str(_properties(source).get("INIT") or "1'h0")
                if raw_init not in {"0", "1'b0", "1'h0", "1'H0"}:
                    refuse("register_init_unsupported", primitive=source_type)
                    active.remove(cell_name)
                    return None
                properties["init"] = 0
            node = {
                "primitive": source_type,
                "properties": properties,
                "inputs": inputs,
            }
            nodes[cell_name] = node
            order.append(cell_name)
            active.remove(cell_name)
        return {"kind": "node", "cell": cell_name, "pin": _pin_name(source)}

    outputs = []
    for net_name in output_nets:
        reference = resolve(str(net_name))
        if reference is None:
            if diagnostics is not None:
                diagnostics["primitive_shell_refusal"] = dict(failure)
            return None
        outputs.append(reference)
    if opaque_primitives:
        # An opaque input can swallow the very cone the cut was meant to
        # replace, leaving a shell that forwards the owner's original nets and
        # proves trivially while implementing nothing.  Recovery is only
        # complete when the shell still reads the semantic bits.
        reads_semantic_bit = any(
            reference.get("kind") in {"quotient_bit", "quotient_or", "overflow"}
            for reference in (
                [item["source"] for node in nodes.values() for item in node["inputs"]]
                + outputs
            )
        )
        if not reads_semantic_bit:
            if diagnostics is not None:
                diagnostics["primitive_shell_refusal"] = {
                    "reason": "opaque_shell_reads_no_semantic_bit",
                    "opaque_net_count": len(opaque_primitives),
                    "opaque_primitives": sorted(set(opaque_primitives.values()))[:4],
                }
            return None
    if diagnostics is not None:
        diagnostics["opaque_external_count"] = len(opaque_primitives)
        diagnostics["opaque_external_primitives"] = sorted(
            set(opaque_primitives.values())
        )[:4]
    index_by_name = {name: index for index, name in enumerate(order)}

    def anonymize(reference: dict[str, Any]) -> dict[str, Any]:
        checked = dict(reference)
        if checked.get("kind") == "node":
            checked["node"] = index_by_name[str(checked.pop("cell"))]
        return checked

    anonymous_nodes = []
    for name in order:
        node = nodes[name]
        anonymous_nodes.append({
            "primitive": node["primitive"],
            "properties": dict(node["properties"]),
            "inputs": [
                {"pin": item["pin"], "source": anonymize(item["source"])}
                for item in node["inputs"]
            ],
        })
    return {
        "kind": "primitive_boolean_dag_v1",
        "external_count": len(external_names),
        "nodes": anonymous_nodes,
        "outputs": [anonymize(reference) for reference in outputs],
        "external_nets": external_names,
        "cofactor_reduction_count": cofactor_reduction_count,
        "cofactor_pruned_input_count": cofactor_pruned_input_count,
    }


def _semantic_cut_coverage(
    design,
    netlist,
    *,
    cut_nets: list[str],
    critical_paths_data: list[list[str]],
) -> dict[str, Any]:
    """Measure exact cut-net occurrence on pin-level timing paths.

    A cell may occur on a timing path through an input unrelated to the
    proposed cut.  Cell endpoint intersection therefore cannot establish
    that replacing a net removes the path.  Resolve each supplied timing-path
    pin back to its canonical logical net and count coverage only when that
    exact net belongs to the semantic cut.  Cell-only paths deliberately
    resolve no edges and fail closed.
    """
    canonical_cut_nets = set()
    cut_driver_pins = set()
    cut_sink_pins = set()
    for net_name in cut_nets:
        parent = _canonical_parent_net(netlist, net_name)
        if parent is None:
            continue
        canonical_cut_nets.add(_net_name(parent))
        cut_driver_pins.update(
            f"{_cell_name(pin)}/{_pin_name(pin)}"
            for pin in _as_list(parent.getLeafHierPortInsts(True, False, True))
            if _cell_name(pin) and _pin_name(pin)
        )
        cut_sink_pins.update(
            f"{_cell_name(pin)}/{_pin_name(pin)}"
            for pin in _as_list(parent.getLeafHierPortInsts(False, True, True))
            if _cell_name(pin) and _pin_name(pin)
        )

    def path_pin_net(token: str) -> Optional[str]:
        try:
            pin = netlist.getHierPortInstFromName(str(token))
        except Exception:
            pin = None
        if pin is None:
            return None
        try:
            parent = _parent_net(netlist, pin)
        except ConsumerOperatorContractError:
            return None
        return _net_name(parent)

    covered = []
    resolved_pin_counts = []
    for rank, path in enumerate(critical_paths_data, start=1):
        path_nets = {
            name for token in path
            if (name := path_pin_net(token)) is not None
        }
        resolved_pin_counts.append(len(path_nets))
        if path_nets.intersection(canonical_cut_nets):
            covered.append(rank)
    path_count = len(critical_paths_data)
    return {
        "ordered_path_count": path_count,
        "covered_path_count": len(covered),
        "covered_path_ranks": covered,
        "covers_worst_path": 1 in covered,
        "top5_covered_count": sum(rank <= min(5, path_count) for rank in covered),
        "top10_covered_count": sum(rank <= min(10, path_count) for rank in covered),
        "all_path_coverage_ratio": (
            round(len(covered) / path_count, 4) if path_count else 0.0
        ),
        "coverage_basis": "exact_timing_path_pin_net_intersects_semantic_cut",
        "semantic_cut_net_count": len(set(cut_nets)),
        "canonical_semantic_cut_net_count": len(canonical_cut_nets),
        "semantic_cut_driver_pin_count": len(cut_driver_pins),
        "semantic_cut_sink_pin_count": len(cut_sink_pins),
        "resolved_path_net_counts": resolved_pin_counts,
        "pin_level_path_required": True,
    }


def _candidate_output_groups(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    groups = candidate.get("quotient_groups")
    if isinstance(groups, list):
        return [group for group in groups if isinstance(group, dict)]
    group = candidate.get("output_group")
    return [group] if isinstance(group, dict) else []


def _complete_owner_output_cut(owner, candidate: dict[str, Any]) -> dict[str, Any]:
    """Prove that a candidate replaces every observable owner output net."""
    owner_outputs = {
        _net_name(net)
        for pin in _as_list(owner.getHierPortInsts())
        if bool(pin.isOutput())
        and (net := pin.getHierarchicalNet()) is not None
    }
    selected_outputs = {
        str(net)
        for group in _candidate_output_groups(candidate)
        for net in (group.get("nets") or [])
    }
    selected_outputs.update(
        str(item.get("net"))
        for item in (candidate.get("scalar_companions") or [])
        if isinstance(item, dict) and item.get("net")
    )
    selected_outputs.update(
        str(net) for net in (candidate.get("shell_output_nets") or [])
    )
    return {
        "complete_owner_output_cut": bool(owner_outputs)
        and owner_outputs == selected_outputs,
        "owner_output_net_count": len(owner_outputs),
        "selected_complete_cut_net_count": len(selected_outputs),
        "unselected_owner_output_net_count": len(owner_outputs - selected_outputs),
        "non_owner_selected_output_net_count": len(selected_outputs - owner_outputs),
    }


def _output_cut_coverage(
    design,
    netlist,
    *,
    owner_name: str,
    selected_groups: list[dict[str, Any]],
    critical_paths_data: list[list[str]],
) -> dict[str, Any]:
    """Measure whether the proposed output cut actually exits live paths.

    A registered family appearing on a path proves only that the family is
    involved.  It does not prove that replacing one output of its hierarchy
    removes that path.  Coverage is therefore counted only when the first
    downstream cell outside the owner is a sink of a selected output net.
    """
    owner_prefix = owner_name + "/"
    selected_nets = {
        str(net)
        for group in selected_groups
        for net in (group.get("nets") or [])
    }
    selected_sink_cells: set[str] = set()
    for net_name in selected_nets:
        selected_sink_cells.update(
            _canonical_net_sink_cells(netlist, net_name)
        )

    covered_ranks = []
    owner_path_ranks = []
    resolved_exit_ranks = []
    downstream_exit_cells = []
    for rank, path in enumerate(critical_paths_data, start=1):
        cells = []
        for token in path:
            name = _resolve_path_cell_name(design, token)
            if name and (not cells or cells[-1] != name):
                cells.append(name)
        owner_indices = [
            index for index, name in enumerate(cells)
            if name == owner_name or name.startswith(owner_prefix)
        ]
        if not owner_indices:
            continue
        owner_path_ranks.append(rank)
        last_owner_index = max(owner_indices)
        downstream = next(
            (
                name for name in cells[last_owner_index + 1:]
                if name != owner_name and not name.startswith(owner_prefix)
            ),
            None,
        )
        if downstream is None:
            continue
        resolved_exit_ranks.append(rank)
        downstream_exit_cells.append(downstream)
        if downstream in selected_sink_cells:
            covered_ranks.append(rank)

    path_count = len(critical_paths_data)
    return {
        "ordered_path_count": path_count,
        "owner_path_count": len(owner_path_ranks),
        "owner_path_ranks": owner_path_ranks,
        "resolved_exit_path_count": len(resolved_exit_ranks),
        "resolved_exit_path_ranks": resolved_exit_ranks,
        "covered_path_count": len(covered_ranks),
        "covered_path_ranks": covered_ranks,
        "covers_worst_path": 1 in covered_ranks,
        "top5_covered_count": sum(rank <= min(5, path_count) for rank in covered_ranks),
        "top10_covered_count": sum(rank <= min(10, path_count) for rank in covered_ranks),
        "all_path_coverage_ratio": (
            round(len(covered_ranks) / path_count, 4) if path_count else 0.0
        ),
        "coverage_basis": "selected_output_net_dominates_owner_path_exit",
        "selected_output_net_count": len(selected_nets),
        "selected_output_sink_cell_count": len(selected_sink_cells),
        "private_downstream_exit_cells": downstream_exit_cells,
    }


def _owner_output_cut_profiles(
    design,
    netlist,
    *,
    owner_name: str,
    owner,
    critical_paths_data: list[list[str]],
) -> list[dict[str, Any]]:
    """Return private, source-derived coverage for every indexed owner output."""
    profiles = []
    for group in _indexed_boundary_groups(netlist, owner)["output"]:
        profiles.append({
            "stem": str(group["stem"]),
            "width": int(group["width"]),
            "nets": list(group["nets"]),
            "coverage": _output_cut_coverage(
                design,
                netlist,
                owner_name=owner_name,
                selected_groups=[group],
                critical_paths_data=critical_paths_data,
            ),
        })
    profiles.sort(key=lambda item: (
        bool(item["coverage"].get("covers_worst_path")),
        int(item["coverage"].get("covered_path_count") or 0),
        int(item["width"]),
    ), reverse=True)
    return profiles


def _owner_primitive_counts(netlist, owner) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for child in _as_list(netlist.getAllLeafDescendants(owner)):
        cell_type = child.getCellType()
        counts[str(cell_type.getName()).upper() if cell_type is not None else "UNKNOWN"] += 1
    return dict(sorted(counts.items()))


def _control_net(netlist, cell_name: str, pins: tuple[str, ...]) -> Optional[str]:
    found = []
    for pin_name in pins:
        pin = netlist.getHierPortInstFromName(f"{cell_name}/{pin_name}")
        if pin is not None:
            found.append(_net_name(_parent_net(netlist, pin)))
    unique = sorted(set(found))
    return unique[0] if len(unique) == 1 else None


def _constant_role(name: Optional[str]) -> Optional[int]:
    text = str(name or "").lower()
    if "<const1>" in text or text.endswith(("/vcc", "/vcc/p")) or text == "vcc":
        return 1
    if "<const0>" in text or text.endswith(("/gnd", "/gnd/g")) or text == "gnd":
        return 0
    return None


def _zero_init(value: str) -> bool:
    text = str(value or "").strip().lower().replace("_", "")
    if text in {"0", "1'b0", "1'h0"}:
        return True
    if text and set(text) == {"0"}:
        return True
    match = re.fullmatch(r"\d+'[hbo]([0]+)", text)
    return match is not None


def _owner_control_contract(netlist, owner) -> dict[str, Any]:
    """Recover the complete registered schedule inside a consumer owner.

    The observable hierarchy outputs need not themselves be driven by FFs: a
    registered pipeline can end in a small combinational slice.  Therefore the
    schedule is proved from every state primitive in the selected owner rather
    than inferred from the output-port driver type.
    """
    schemas = {
        "FDRE": (("C",), ("CE",), ("R",)),
        "FDSE": (("C",), ("CE",), ("S",)),
        "FDCE": (("C",), ("CE",), ("CLR",)),
        "FDPE": (("C",), ("CE",), ("PRE",)),
        "SRL16E": (("CLK",), ("CE",), ()),
        "SRLC32E": (("CLK",), ("CE",), ()),
    }
    facts = []
    for child in _as_list(netlist.getAllLeafDescendants(owner)):
        cell_type_node = child.getCellType()
        cell_type = (
            str(cell_type_node.getName()).upper()
            if cell_type_node is not None else ""
        )
        schema = schemas.get(cell_type)
        if schema is None:
            continue
        cell_name = str(child.getFullHierarchicalInstName())
        clock_pins, enable_pins, state_pins = schema
        probe_pin = netlist.getHierPortInstFromName(
            f"{cell_name}/{clock_pins[0]}"
        )
        if probe_pin is None:
            raise ConsumerOperatorContractError(
                "consumer state primitive has no clock pin"
            )
        facts.append({
            "cell_type": cell_type,
            "clock": _control_net(netlist, cell_name, clock_pins),
            "enable": _control_net(netlist, cell_name, enable_pins),
            "state": (
                _control_net(netlist, cell_name, state_pins)
                if state_pins else None
            ),
            "init": str(_properties(probe_pin).get("INIT", "")),
        })
    if not facts:
        raise ConsumerOperatorContractError("consumer registered state set is empty")
    clocks = {item["clock"] for item in facts}
    enables = {_constant_role(item["enable"]) for item in facts}
    states = {
        _constant_role(item["state"])
        for item in facts if item["state"] is not None
    }
    bad_init_count = sum(not _zero_init(item["init"]) for item in facts)
    unknown_enables = sorted({
        str(item["enable"])
        for item in facts if _constant_role(item["enable"]) is None
    })[:4]
    unknown_states = sorted({
        str(item["state"])
        for item in facts
        if item["state"] is not None and _constant_role(item["state"]) is None
    })[:4]
    bad_inits = sorted({
        str(item["init"])
        for item in facts if not _zero_init(item["init"])
    })[:4]
    if (
        len(clocks) != 1 or None in clocks
        or enables != {1}
        or states != {0}
        or bad_init_count
    ):
        raise ConsumerOperatorContractError(
            "consumer registered schedule is not free-running zero-state: "
            f"clock_classes={len(clocks)} clock_missing={None in clocks} "
            f"enable_roles={sorted(str(item) for item in enables)} "
            f"state_roles={sorted(str(item) for item in states)} "
            f"bad_init_count={bad_init_count} state_count={len(facts)} "
            f"unknown_enables={unknown_enables} "
            f"unknown_states={unknown_states} bad_inits={bad_inits}"
        )
    primitive_counts = Counter(item["cell_type"] for item in facts)
    return {
        "clock_net": next(iter(clocks)),
        "clock_edge": "rising",
        "advance": "every_cycle",
        "reset": "inactive",
        "register_init": 0,
        "registered_state_count": len(facts),
        "registered_primitive_type_counts": dict(sorted(primitive_counts.items())),
    }


def _pipeline_depth(output_groups: list[dict[str, Any]], word_width: int) -> tuple[Optional[int], str]:
    hints = []
    for group in output_groups:
        stem = re.sub(r"\[\d+\]", "", str(group["stem"]))
        hints.extend(int(value) for value in _STAGE_ROLE.findall(stem))
    positive = [value for value in hints if value > 0]
    if not positive:
        return None, "no_zero_based_registered_stage_hint"
    depth = max(positive) + 2
    if not 2 <= depth <= word_width:
        return None, "registered_stage_hint_out_of_range"
    return depth, "zero_based_registered_stage_plus_input_output"


def _indexed_net_slice(nets: list[str]) -> Optional[tuple[int, int]]:
    """Return one exact ascending bit slice carried by boundary nets."""
    parsed = []
    for net in nets:
        match = _INDEXED_PORT.fullmatch(str(net))
        if match is None:
            return None
        parsed.append((match.group("stem"), int(match.group("index"))))
    if not parsed or len({stem for stem, _ in parsed}) != 1:
        return None
    indices = [index for _, index in parsed]
    if indices != list(range(indices[0], indices[0] + len(indices))):
        return None
    return indices[0], indices[-1]


def _scalar_companion_outputs(netlist, owner_name: str, owner) -> list[dict[str, Any]]:
    """Recover scalar outputs owned by the same arithmetic hierarchy.

    A narrow data rewrite may otherwise leave the entire original recurrence
    alive through one completion/idle/control output.  This helper does not
    claim sequential equivalence: it extracts only a conservative
    measurement-probe candidate when the scalar has one local LUT driver and
    its all-zero truth-table value is exact.  Luna and central measurement
    still own whether the wider cut is retained.
    """
    companions = []
    for pin in _as_list(owner.getHierPortInsts()):
        if not bool(pin.isOutput()) or _INDEXED_PORT.fullmatch(_pin_name(pin)):
            continue
        hierarchical_net = pin.getHierarchicalNet()
        if hierarchical_net is None:
            continue
        sources = _as_list(
            hierarchical_net.getLeafHierPortInsts(True, False, True)
        )
        if len(sources) != 1:
            continue
        source = sources[0]
        lut_match = re.fullmatch(r"LUT([1-6])", _cell_type(source))
        source_name = _cell_name(source)
        if (
            lut_match is None
            or _pin_name(source).upper() != "O"
            or not source_name.startswith(owner_name + "/")
        ):
            continue
        size = int(lut_match.group(1))
        init = str(_properties(source).get("INIT", ""))
        init_match = re.fullmatch(
            rf"{1 << size}'h([0-9a-fA-F]+)", init
        )
        if init_match is None:
            continue
        # Verify that every LUT address input is structurally present before
        # using address zero as an exact quiescent truth-table observation.
        if any(
            netlist.getHierPortInstFromName(f"{source_name}/I{index}") is None
            for index in range(size)
        ):
            continue
        companions.append({
            "net": _net_name(hierarchical_net),
            "quiescent_value": int(init_match.group(1), 16) & 1,
            "driver_lut_width": size,
            "basis": "local_scalar_lut_zero_address_truth_table",
        })
    return companions


def _digit_recurrence_signature(
    netlist,
    owner,
    input_width: int,
    *,
    diagnostics: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Recognize a staged wide-state plus taper-state recurrence.

    The signature is intentionally independent of hierarchy and signal names.
    Synthesized stage-family names are used only to group physical register
    copies; support requires the characteristic width populations and the
    complete free-running control contract separately.
    """
    exact_families: dict[tuple[str, str, str, int], set[int]] = {}
    for child in _as_list(netlist.getAllLeafDescendants(owner)):
        cell_type_node = child.getCellType()
        cell_type = (
            str(cell_type_node.getName()).upper()
            if cell_type_node is not None else ""
        )
        if cell_type not in _REGISTER_TYPES:
            continue
        leaf = str(child.getFullHierarchicalInstName()).rsplit("/", 1)[-1]
        bit_tokens = list(_INDEX_TOKEN.finditer(leaf))
        if not bit_tokens:
            continue
        bit_token = bit_tokens[-1]
        prefix = leaf[:bit_token.start()]
        suffix = leaf[bit_token.end():]
        stage_tokens = list(re.finditer(r"\d+", prefix))
        if not stage_tokens:
            continue
        stage_token = stage_tokens[-1]
        stage = int(stage_token.group())
        normalized_suffix = re.sub(r"\d+", "#", suffix)
        exact_stem = prefix + "[]" + normalized_suffix
        normalized = (
            prefix[:stage_token.start()]
            + "#"
            + prefix[stage_token.end():]
            + "[]"
            + normalized_suffix
        )
        exact_families.setdefault(
            (exact_stem, cell_type, normalized, stage), set()
        ).add(int(bit_token.group(1)))

    series: dict[tuple[str, str, int], list[tuple[int, int]]] = {}
    for (
        _exact_stem, cell_type, normalized, stage
    ), indices_set in exact_families.items():
        indices = sorted(indices_set)
        if not indices:
            continue
        steps = {
            right - left for left, right in zip(indices, indices[1:])
        }
        if any(step <= 0 for step in steps) or len(steps) > 1:
            continue
        # Synthesized packed state may retain the source bit positions even
        # when only every Nth bit survives.  Such a word is still complete
        # when its indices form one regular progression; requiring stride 1
        # incorrectly discards exact taper-state recurrences.
        index_stride = next(iter(steps), 1)
        series.setdefault((normalized, cell_type, index_stride), []).append(
            (stage, len(indices))
        )

    ordered_series = [
        (sorted(set(values)), index_stride)
        for (_normalized, _cell_type, index_stride), values in series.items()
        if len({stage for stage, _ in values}) >= 4
    ]
    if diagnostics is not None:
        diagnostics["indexed_register_family_count"] = len(exact_families)
        diagnostics["multi_stage_series_profiles"] = sorted(
            (
                {
                    "stage_count": len(values),
                    "stages": [stage for stage, _ in values],
                    "widths": [width for _, width in values],
                    "index_stride": index_stride,
                }
                for values, index_stride in ordered_series
            ),
            key=lambda item: (-item["stage_count"], item["widths"]),
        )[:12]
    wide = [
        values for values in ordered_series
        if min(width for _, width in values[0]) >= input_width - 2
        and max(width for _, width in values[0]) <= input_width
    ]
    taper = []
    for values, _index_stride in ordered_series:
        widths = [width for _, width in values]
        if (
            max(widths) <= input_width // 2
            and all(left > right for left, right in zip(widths, widths[1:]))
        ):
            taper.append(values)
    if not wide or not taper:
        return None
    wide_stage_count = max(len(values) for values, _index_stride in wide)
    taper_stage_count = max(len(values) for values in taper)
    if abs(wide_stage_count - taper_stage_count) > 1:
        return None
    pipeline_depth = max(wide_stage_count, taper_stage_count) + 1
    return {
        "pipeline_depth": pipeline_depth,
        "wide_state_stage_count": wide_stage_count,
        "taper_state_stage_count": taper_stage_count,
        "stage_population_agreement": True,
        "evidence_basis": (
            "anonymous_registered_width_series_plus_boundary_bit_slice"
        ),
    }


def _quantized_sqrt_candidate(
    netlist,
    owner_name: str,
    owner,
    *,
    distance: int,
    diagnostics: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Recover a quantized digit-recurrence sqrt from live structure."""
    boundary = _indexed_boundary_groups(netlist, owner)
    scalar_companions = _scalar_companion_outputs(
        netlist, owner_name, owner
    )
    if diagnostics is not None:
        profile = {
            "distance": distance,
            "input_group_count": len(boundary["input"]),
            "output_group_count": len(boundary["output"]),
        }
        if distance <= 1:
            profile.update({
                "input_widths": sorted(
                    int(group["width"]) for group in boundary["input"]
                ),
                "output_widths": sorted(
                    int(group["width"]) for group in boundary["output"]
                ),
            })
        diagnostics.setdefault("owner_boundary_profiles", []).append(profile)
    candidates = []
    for input_group in boundary["input"]:
        full_width = int(input_group["width"])
        if not 16 <= full_width <= 128 or full_width % 2:
            continue
        if diagnostics is not None:
            diagnostics["eligible_even_input_count"] = int(
                diagnostics.get("eligible_even_input_count") or 0
            ) + 1
        signature_diagnostics = (
            {} if diagnostics is not None and distance == 0 else None
        )
        signature = _digit_recurrence_signature(
            netlist,
            owner,
            full_width,
            diagnostics=signature_diagnostics,
        )
        if diagnostics is not None and signature_diagnostics is not None:
            diagnostics["selected_owner_recurrence"] = signature_diagnostics
        if signature is None:
            continue
        if diagnostics is not None:
            diagnostics["recurrence_signature_count"] = int(
                diagnostics.get("recurrence_signature_count") or 0
            ) + 1
        for output_group in boundary["output"]:
            output_width = int(output_group["width"])
            output_slice = _indexed_net_slice(list(output_group["nets"]))
            if output_slice is None:
                continue
            if diagnostics is not None:
                diagnostics["indexed_output_slice_count"] = int(
                    diagnostics.get("indexed_output_slice_count") or 0
                ) + 1
            output_lsb, _output_msb = output_slice
            reduced_low = 2 * output_lsb
            reduced_width = full_width - reduced_low
            if (
                output_lsb <= 0
                or reduced_width < 4
                or reduced_width % 2
                or not 1 <= output_width < reduced_width // 2
                or not 1 <= int(signature["pipeline_depth"]) <= reduced_width // 2
            ):
                continue
            if diagnostics is not None:
                diagnostics["quantized_identity_count"] = int(
                    diagnostics.get("quantized_identity_count") or 0
                ) + 1
            candidates.append({
                "operator": "unsigned_quantized_sqrt_digit_recurrence",
                "owner_name": owner_name,
                "distance": distance,
                "full_input": input_group,
                "output_group": output_group,
                "output_slice_lsb": output_lsb,
                "reduced_input_low_bit": reduced_low,
                "reduced_input_nets": list(input_group["nets"])[reduced_low:],
                "pipeline_depth": int(signature["pipeline_depth"]),
                "recurrence_signature": signature,
                "scalar_companions": scalar_companions,
                "semantic_score": (
                    full_width - reduced_width + full_width - output_width
                    + 8 * len(scalar_companions)
                ),
            })
    if not candidates:
        return None
    candidates.sort(key=lambda item: (
        item["distance"],
        item["semantic_score"],
        len(item["reduced_input_nets"]),
    ), reverse=True)
    return candidates[0]


def _integer_sqrt_candidate(
    netlist,
    owner_name: str,
    owner,
    *,
    distance: int,
) -> Optional[dict[str, Any]]:
    """Recover a full-width integer sqrt digit recurrence.

    The emitter and bundle validator have always supported this operator, but
    the live constructor previously considered only a quantized root whose
    observable boundary starts above bit zero.  A complete root naturally
    starts at bit zero, so that omission made a structurally recovered full
    recurrence unreachable to the Agent.
    """
    boundary = _indexed_boundary_groups(netlist, owner)
    scalar_companions = _scalar_companion_outputs(
        netlist, owner_name, owner
    )
    candidates = []
    for input_group in boundary["input"]:
        full_width = int(input_group["width"])
        if not 16 <= full_width <= 128 or full_width % 2:
            continue
        signature = _digit_recurrence_signature(
            netlist, owner, full_width
        )
        if signature is None:
            continue
        root_width = full_width // 2
        if not 1 <= int(signature["pipeline_depth"]) <= root_width:
            continue
        for output_group in boundary["output"]:
            output_slice = _indexed_net_slice(list(output_group["nets"]))
            if output_slice is None:
                continue
            output_lsb, output_msb = output_slice
            if (
                int(output_group["width"]) != root_width
                or output_lsb != 0
                or output_msb != root_width - 1
            ):
                continue
            candidates.append({
                "operator": "unsigned_integer_sqrt_digit_recurrence",
                "owner_name": owner_name,
                "distance": distance,
                "full_input": input_group,
                "output_group": output_group,
                "pipeline_depth": int(signature["pipeline_depth"]),
                "recurrence_signature": signature,
                "scalar_companions": scalar_companions,
                "semantic_score": (
                    full_width - root_width
                    + 8 * len(scalar_companions)
                ),
            })
    if not candidates:
        return None
    candidates.sort(key=lambda item: (
        item["distance"],
        item["semantic_score"],
    ), reverse=True)
    return candidates[0]


def _reciprocal_sqrt_candidate(
    design,
    netlist,
    owner_name: str,
    owner,
    *,
    distance: int,
    design_sha256: Optional[str],
    critical_paths_data: list[list[str]],
    diagnostics: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Recover a live sqrt -> constant divider -> consumer-shell cut."""
    boundary = _indexed_boundary_groups(netlist, owner)
    root_candidates = []
    for input_group in boundary["input"]:
        input_width = int(input_group["width"])
        if not 16 <= input_width <= 128 or input_width % 2:
            continue
        signature = _digit_recurrence_signature(netlist, owner, input_width)
        if signature is None:
            continue
        root_width = input_width // 2
        for output_group in boundary["output"]:
            output_slice = _indexed_net_slice(list(output_group["nets"]))
            if (
                output_slice == (0, root_width - 1)
                and int(output_group["width"]) == root_width
            ):
                root_candidates.append((input_group, output_group, signature))
    if diagnostics is not None:
        diagnostics["composite_root_candidate_count"] = len(root_candidates)
    if len(root_candidates) != 1 or not design_sha256:
        if diagnostics is not None:
            diagnostics["composite_refusal"] = "root_candidate_not_unique"
        return None
    input_group, root_group, signature = root_candidates[0]
    root_width = int(root_group["width"])

    root_net_names = {
        name for net in root_group["nets"]
        if (name := _canonical_net_name(netlist, str(net))) is not None
    }
    downstream_names = set()
    for net_name in root_group["nets"]:
        for sink_name in _canonical_net_sink_cells(netlist, str(net_name)):
            current = sink_name.rsplit("/", 1)[0] if "/" in sink_name else ""
            for _ in range(8):
                if not current:
                    break
                if not current.startswith(owner_name + "/"):
                    downstream_names.add(current)
                current = current.rsplit("/", 1)[0] if "/" in current else ""

    divider_candidates = []
    for divider_name in sorted(downstream_names):
        divider = netlist.getHierCellInstFromName(divider_name)
        if divider is None:
            continue
        divider_boundary = _indexed_boundary_groups(netlist, divider)
        matching_inputs = []
        for group in divider_boundary["input"]:
            canonical = {
                name for net in group["nets"]
                if (name := _canonical_net_name(netlist, str(net))) is not None
            }
            if canonical == root_net_names:
                matching_inputs.append(group)
        quotient_groups = []
        for group in divider_boundary["output"]:
            output_slice = _indexed_net_slice(list(group["nets"]))
            if (
                output_slice is not None
                and output_slice[0] > 0
                and output_slice[1] == 2 * root_width - 1
            ):
                quotient_groups.append(group)
        depth, depth_basis = _pipeline_depth(quotient_groups, 2 * root_width)
        if len(matching_inputs) != 1 or not quotient_groups or depth is None:
            continue
        try:
            divider_control = _owner_control_contract(netlist, divider)
        except ConsumerOperatorContractError:
            continue
        divider_candidates.append({
            "owner_name": divider_name,
            "owner": divider,
            "input_group": matching_inputs[0],
            "quotient_groups": quotient_groups,
            "pipeline_depth": depth,
            "pipeline_depth_basis": depth_basis,
            "control_contract": divider_control,
        })
    if diagnostics is not None:
        diagnostics["composite_divider_candidate_count"] = len(divider_candidates)
        diagnostics["composite_downstream_owner_count"] = len(downstream_names)
    if len(divider_candidates) != 1:
        if diagnostics is not None:
            diagnostics["composite_refusal"] = "divider_candidate_not_unique"
        return None
    divider = divider_candidates[0]

    # The owner outputs other than the root form the current consumer shell.
    root_raw_nets = {str(net) for net in root_group["nets"]}
    shell_output_nets = []
    for pin in _as_list(owner.getHierPortInsts()):
        if not bool(pin.isOutput()):
            continue
        hierarchical = pin.getHierarchicalNet()
        if hierarchical is None:
            continue
        name = _net_name(hierarchical)
        if name not in root_raw_nets:
            shell_output_nets.append(name)
    shell_cut = _trace_owner_semantic_cut(
        netlist,
        owner_name=owner_name,
        output_nets=shell_output_nets,
    )
    quotient_nets = shell_cut["quotient_nets"]
    overflow_nets = shell_cut["overflow_nets"]
    raw_quotient_indices = sorted(quotient_nets)
    if diagnostics is not None:
        diagnostics["composite_shell_quotient_indices"] = raw_quotient_indices
        diagnostics["composite_shell_overflow_net_count"] = len(overflow_nets)
        diagnostics["composite_shell_quotient_copy_counts"] = {
            str(index): len(quotient_nets[index]) for index in raw_quotient_indices
        }
    if not raw_quotient_indices or not overflow_nets:
        if diagnostics is not None:
            diagnostics["composite_refusal"] = "shell_semantic_cut_incomplete"
        return None
    overflow_profiles = []
    overflow_indices_set = set()
    for overflow_net in overflow_nets:
        overflow_trace = _trace_owner_semantic_cut(
            netlist,
            owner_name=owner_name,
            output_nets=[overflow_net],
            expand_overflow=True,
        )
        indices = sorted(overflow_trace["quotient_nets"])
        overflow_indices_set.update(indices)
        overflow_profiles.append({
            "net": overflow_net,
            "quotient_indices": indices,
        })
    overflow_indices = sorted(overflow_indices_set)
    proven_overflow_semantics: dict[str, list[int]] = {}
    if len(overflow_nets) > 1:
        for profile in overflow_profiles:
            exact_indices = _recover_quotient_or_indices(
                netlist, str(profile["net"])
            )
            if exact_indices != list(profile["quotient_indices"]):
                proven_overflow_semantics = {}
                break
            proven_overflow_semantics[str(profile["net"])] = exact_indices
    if diagnostics is not None:
        diagnostics["composite_overflow_quotient_indices"] = overflow_indices
        diagnostics["composite_overflow_profiles"] = overflow_profiles
        diagnostics["composite_proven_overflow_or_count"] = len(
            proven_overflow_semantics
        )
    if len(overflow_nets) == 1:
        quotient_indices = (
            [
                index for index in raw_quotient_indices
                if index < overflow_indices[0]
            ]
            if overflow_indices else []
        )
        overflow_range = overflow_indices
    else:
        # Replicated or independently reduced overflow outputs need not be one
        # contiguous OR.  Preserve their exact gates in the primitive shell;
        # only the directly observed low quotient prefix defines the slice.
        quotient_indices = []
        for index in range(
            raw_quotient_indices[0], raw_quotient_indices[-1] + 1
        ):
            if index not in raw_quotient_indices:
                break
            quotient_indices.append(index)
        referenced_high = max(
            raw_quotient_indices + overflow_indices,
            default=quotient_indices[-1] if quotient_indices else -1,
        )
        overflow_high = max(
            quotient_indices[-1] + 1 if quotient_indices else 0,
            referenced_high,
        )
        overflow_range = list(range(
            quotient_indices[-1] + 1,
            overflow_high + 1,
        )) if quotient_indices else []
    if diagnostics is not None:
        diagnostics["composite_selected_quotient_indices"] = quotient_indices
        diagnostics["composite_selected_overflow_range"] = overflow_range
    if (
        not quotient_indices
        or quotient_indices
        != list(range(quotient_indices[0], quotient_indices[-1] + 1))
        or not overflow_range
        or overflow_range[0] != quotient_indices[-1] + 1
        or overflow_range[-1] >= 2 * root_width - 1
        or len(overflow_nets) == 1
        and (
            overflow_indices
            != list(range(overflow_indices[0], overflow_indices[-1] + 1))
            or any(
                index not in overflow_indices
                for index in raw_quotient_indices
                if index > quotient_indices[-1]
            )
        )
    ):
        if diagnostics is not None:
            diagnostics["composite_refusal"] = "overflow_semantics_incomplete"
        return None

    try:
        try:
            from .hierarchical_contract import recover_hierarchical_operator_contract
            from .primitive_simulator import PrimitiveContractSimulator
        except ImportError:
            from hierarchical_contract import recover_hierarchical_operator_contract
            from primitive_simulator import PrimitiveContractSimulator
        divider_contract = recover_hierarchical_operator_contract(
            design,
            design_sha256,
            divider["owner_name"],
            max_region_cells=50000,
            max_consumer_cells=50000,
        )
        simulator = PrimitiveContractSimulator(divider_contract)
        output = None
        for _ in range(int(divider["pipeline_depth"]) + 2):
            output = simulator.step({str(divider["input_group"]["stem"]): 1})
        quotient_lsb = int(_indexed_net_slice(
            list(divider["quotient_groups"][0]["nets"])
        )[0])
        observed = []
        for group in divider["quotient_groups"]:
            value = int((output or {}).get(str(group["stem"]), -1))
            if value >= 0 and value < (1 << (int(group["width"]) - 1)):
                observed.append(value)
        if len(set(observed)) != 1:
            return None
        numerator_constant = observed[0] << quotient_lsb
    except Exception as exc:
        if diagnostics is not None:
            diagnostics["composite_refusal"] = "divider_simulation_failed"
            diagnostics["composite_simulation_error"] = str(exc)
        return None
    if numerator_constant <= 0:
        if diagnostics is not None:
            diagnostics["composite_refusal"] = "numerator_constant_not_positive"
        return None

    live_path_cells = {
        name for path in critical_paths_data for token in path
        if (name := _resolve_path_cell_name(design, token)) is not None
    }

    def cut_net_priority(net_name: str) -> tuple[int, str]:
        parent = _canonical_parent_net(netlist, net_name)
        sources = (
            _as_list(parent.getLeafHierPortInsts(True, False, True))
            if parent is not None else []
        )
        on_live_path = any(_cell_name(pin) in live_path_cells for pin in sources)
        return (int(on_live_path), net_name)

    selected_quotient_nets = [
        sorted(quotient_nets[index], key=cut_net_priority, reverse=True)[0]
        for index in quotient_indices
    ]
    quotient_cut_coverage = _semantic_cut_coverage(
        design,
        netlist,
        cut_nets=selected_quotient_nets,
        critical_paths_data=critical_paths_data,
    )
    if diagnostics is not None:
        diagnostics["composite_quotient_cut_coverage"] = quotient_cut_coverage
    primitive_shell = _recover_primitive_shell(
        netlist,
        owner_name=owner_name,
        owner=owner,
        output_nets=shell_output_nets,
        quotient_low=quotient_indices[0],
        quotient_high=max(raw_quotient_indices + overflow_indices),
        # With multiple overflow outputs, retain each exact Boolean function
        # in the recovered primitive shell instead of conflating the nets.
        overflow_net=overflow_nets[0] if len(overflow_nets) == 1 else "",
        clock_net=str(divider["control_contract"]["clock_net"]),
        overflow_semantics=proven_overflow_semantics,
        diagnostics=diagnostics,
    )
    if primitive_shell is None:
        if diagnostics is not None:
            diagnostics["composite_refusal"] = "primitive_shell_recovery_incomplete"
        return None
    if diagnostics is not None:
        diagnostics["composite_shell_node_count"] = len(primitive_shell["nodes"])
        diagnostics["composite_shell_external_count"] = primitive_shell[
            "external_count"
        ]
        diagnostics["composite_shell_output_count"] = len(
            primitive_shell["outputs"]
        )
    semantic_cut_nets = shell_output_nets + list(root_group["nets"])
    cut_coverage = _semantic_cut_coverage(
        design,
        netlist,
        cut_nets=semantic_cut_nets,
        critical_paths_data=critical_paths_data,
    )
    return {
        "operator": "unsigned_constant_reciprocal_sqrt_slice",
        "owner_name": owner_name,
        "distance": distance,
        "full_input": input_group,
        "output_group": root_group,
        "pipeline_depth": int(signature["pipeline_depth"]),
        "recurrence_signature": signature,
        "divider_owner_name": divider["owner_name"],
        "division_pipeline_depth": int(divider["pipeline_depth"]),
        "division_pipeline_depth_basis": divider["pipeline_depth_basis"],
        "numerator_width": 2 * root_width,
        "numerator_constant": numerator_constant,
        "quotient_low_bit": quotient_indices[0],
        "quotient_high_bit": quotient_indices[-1],
        "overflow_low_bit": overflow_range[0],
        "overflow_high_bit": overflow_range[-1],
        "quotient_nets": selected_quotient_nets,
        "quotient_net_groups": [
            list(quotient_nets[index]) for index in quotient_indices
        ],
        "quotient_cut_coverage": quotient_cut_coverage,
        "overflow_net": overflow_nets[0],
        "primitive_shell": primitive_shell,
        "shell_output_nets": shell_output_nets,
        "output_cut_coverage": cut_coverage,
        "semantic_cut_diagnostics": {
            "shell_trace": shell_cut,
            "overflow_trace": overflow_trace,
            "divider_output_group_count": len(divider["quotient_groups"]),
            "numerator_low_discarded_bit_count": quotient_lsb,
        },
        "semantic_score": (
            1000
            + 20 * int(bool(cut_coverage.get("covers_worst_path")))
            + int(cut_coverage.get("covered_path_count") or 0)
        ),
    }


def _downstream_registered_control_contract(
    netlist, *, owner_name: str, output_nets: list[str]
) -> dict[str, Any]:
    """Recover the clock boundary after a purely combinational owner."""
    owner_prefix = owner_name + "/"
    endpoint_cells = set()
    clocks = set()
    for net_name in output_nets:
        parent = _canonical_parent_net(netlist, net_name)
        if parent is None:
            raise ConsumerOperatorContractError("owner output net is unresolved")
        for sink in _as_list(parent.getLeafHierPortInsts(False, True, True)):
            cell_name = _cell_name(sink)
            if not cell_name or cell_name.startswith(owner_prefix):
                continue
            if _cell_type(sink) not in {"FDRE", "FDSE", "FDCE", "FDPE"}:
                raise ConsumerOperatorContractError(
                    "combinational owner output has a non-register consumer"
                )
            endpoint_cells.add(cell_name)
            clock = _control_net(netlist, cell_name, ("C",))
            if clock is None:
                raise ConsumerOperatorContractError(
                    "downstream register clock is unresolved"
                )
            clocks.add(clock)
    if not endpoint_cells or len(clocks) != 1:
        raise ConsumerOperatorContractError(
            "combinational owner has no single-clock registered boundary"
        )
    return {
        "clock_net": next(iter(clocks)),
        "clock_edge": "rising",
        "advance": "every_cycle",
        "reset": "inactive",
        "register_init": 0,
        "registered_state_count": 0,
        "downstream_registered_endpoint_count": len(endpoint_cells),
        "registered_primitive_type_counts": {},
    }


def _owner_internal_indexed_net_groups(
    owner_name: str, owner
) -> list[dict[str, Any]]:
    groups: dict[str, dict[int, str]] = {}
    for net in _as_list(owner.getCellType().getNets()):
        raw_name = str(net.getName())
        match = _INDEXED_PORT.fullmatch(raw_name)
        if match is None:
            continue
        groups.setdefault(match.group("stem"), {})[int(match.group("index"))] = (
            f"{owner_name}/{raw_name}"
        )
    result = []
    for stem, indexed in groups.items():
        indices = sorted(indexed)
        if not indices or indices != list(range(indices[0], indices[-1] + 1)):
            continue
        result.append({
            "stem": stem,
            "low": indices[0],
            "high": indices[-1],
            "nets": [indexed[index] for index in indices],
        })
    return result


def _dsp_operand_lane_signatures(
    netlist, *, owner_name: str, net_name: str, bit: int
) -> set[tuple[str, str]]:
    parent = _canonical_parent_net(netlist, net_name)
    if parent is None:
        return set()
    owner_prefix = owner_name + "/"
    result = set()
    for sink in _as_list(parent.getLeafHierPortInsts(False, True, True)):
        cell_name = _cell_name(sink)
        match = re.fullmatch(r"([AB])\[(\d+)\]", _pin_name(sink))
        if (
            cell_name.startswith(owner_prefix)
            and _cell_type(sink) == "DSP_A_B_DATA"
            and match is not None
            and int(match.group(2)) == bit
        ):
            result.add((cell_name, match.group(1)))
    return result


def _wide_product_boolean_shell_candidate(
    design,
    netlist,
    owner_name: str,
    owner,
    *,
    distance: int,
    critical_paths_data: list[list[str]],
    diagnostics: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Recover a combinational wide product plus its exact Boolean shell.

    The product identity is established anonymously from two shifted operand
    buses entering the same DSP A/B lane-0/1 topology.  Candidate internal
    product cuts are tried mechanically; only a cut that lets every owner
    output resolve without entering a DSP primitive is retained.
    """
    primitive_counts = _owner_primitive_counts(netlist, owner)
    dsp_count = int(primitive_counts.get("DSP_MULTIPLIER") or 0)
    if dsp_count < 2:
        return None
    boundary = _indexed_boundary_groups(netlist, owner)
    wide_inputs = [
        group for group in boundary["input"]
        if 15 <= int(group["width"]) <= 63
    ]
    scalar_inputs = [
        group for group in boundary["input"] if int(group["width"]) == 1
    ]
    operands = []
    for group in wide_inputs:
        lane_one = _dsp_operand_lane_signatures(
            netlist,
            owner_name=owner_name,
            net_name=str(group["nets"][0]),
            bit=1,
        )
        if not lane_one:
            continue
        companions = []
        for scalar in scalar_inputs:
            lane_zero = _dsp_operand_lane_signatures(
                netlist,
                owner_name=owner_name,
                net_name=str(scalar["nets"][0]),
                bit=0,
            )
            overlap = len(lane_one.intersection(lane_zero))
            if overlap:
                companions.append((overlap, str(scalar["stem"]), scalar))
        if not companions:
            continue
        companions.sort(key=lambda item: (item[0], item[1]), reverse=True)
        companion = companions[0][2]
        operands.append({
            "width": int(group["width"]) + 1,
            "nets": list(companion["nets"]) + list(group["nets"]),
            "lane_overlap": companions[0][0],
        })
    if len(operands) != 2 or operands[0]["width"] != operands[1]["width"]:
        return None
    operand_width = int(operands[0]["width"])
    product_width = 2 * operand_width
    if not 16 <= operand_width <= 36:
        return None

    output_groups = list(boundary["output"])
    indexed_output_nets = {
        str(net) for group in output_groups for net in group["nets"]
    }
    scalar_outputs = []
    for pin in _as_list(owner.getHierPortInsts()):
        if not bool(pin.isOutput()) or _INDEXED_PORT.fullmatch(_pin_name(pin)):
            continue
        net = pin.getHierarchicalNet()
        if net is not None and _net_name(net) not in indexed_output_nets:
            scalar_outputs.append({"net": _net_name(net)})
    output_nets = [
        str(net) for group in output_groups for net in group["nets"]
    ] + [str(item["net"]) for item in scalar_outputs]
    if not output_nets:
        return None

    internal_groups = _owner_internal_indexed_net_groups(owner_name, owner)
    product_groups = [
        group for group in internal_groups
        if len(group["nets"]) >= operand_width
        and 0 <= int(group["low"]) < product_width
        and int(group["high"]) < product_width
    ]
    high_bit_nets = [
        group["nets"][product_width - 1 - int(group["low"])]
        for group in internal_groups
        if int(group["low"]) <= product_width - 1 <= int(group["high"])
    ]
    shell_candidates = []
    shell_refusals: list[dict[str, Any]] = []
    attempt_count = 0
    for group in product_groups:
        semantic = {
            str(net): int(group["low"]) + offset
            for offset, net in enumerate(group["nets"])
        }
        variants = [semantic]
        if int(group["high"]) == product_width - 2:
            variants = [
                {**semantic, str(high_net): product_width - 1}
                for high_net in high_bit_nets
                if str(high_net) not in semantic
            ]
        for semantic_bits in variants:
            shell_diagnostics: dict[str, Any] = {}
            attempt_count += 1
            shell = _recover_primitive_shell(
                netlist,
                owner_name=owner_name,
                owner=owner,
                output_nets=output_nets,
                quotient_low=min(semantic_bits.values()),
                quotient_high=max(semantic_bits.values()),
                overflow_net="",
                clock_net="",
                semantic_net_bits=semantic_bits,
                diagnostics=shell_diagnostics,
            )
            if shell is not None:
                shell_candidates.append((
                    int(shell_diagnostics.get("opaque_external_count") or 0),
                    len(shell["nodes"]),
                    -len(semantic_bits),
                    min(semantic_bits.values()),
                    shell,
                    semantic_bits,
                    shell_diagnostics,
                ))
            elif shell_diagnostics:
                shell_refusals.append(shell_diagnostics)
    if not shell_candidates:
        if diagnostics is not None:
            diagnostics["wide_product_shell_refusal"] = (
                "no_internal_product_cut_resolved_complete_owner_outputs"
            )
            # Every shell attempt records why it failed and the result was
            # dropped, so the refusal named the outcome and never the cause.
            # Most attempts still refuse without a reason at all; an empty
            # list here says the shell gave up at one of those points.
            diagnostics["wide_product_shell_attempt_refusals"] = shell_refusals[:8]
            diagnostics["wide_product_shell_attempt_count"] = attempt_count
        return None
    # A shell that expands every internal cone states more about the owner
    # than one that retains part of it opaquely, so rank opacity ahead of
    # size and leave the ordering of fully expanded shells unchanged.
    shell_candidates.sort(key=lambda item: item[:4])
    _, _, _, product_low, shell, semantic_bits, selected_diagnostics = (
        shell_candidates[0]
    )
    if diagnostics is not None:
        # The per-attempt record is dropped once an attempt succeeds, so
        # publish how much of the winning shell was retained opaquely.
        diagnostics["wide_product_shell_opaque_external_count"] = (
            selected_diagnostics.get("opaque_external_count") or 0
        )
        diagnostics["wide_product_shell_opaque_primitives"] = (
            selected_diagnostics.get("opaque_external_primitives") or []
        )
        diagnostics["wide_product_shell_attempt_count"] = attempt_count
    product_high = max(semantic_bits.values())
    control = _downstream_registered_control_contract(
        netlist, owner_name=owner_name, output_nets=output_nets
    )
    coverage = _semantic_cut_coverage(
        design,
        netlist,
        cut_nets=list(semantic_bits),
        critical_paths_data=critical_paths_data,
    )
    return {
        "operator": "signed_wide_product_boolean_shell",
        "owner_name": owner_name,
        "distance": distance,
        "operand_width": operand_width,
        "operands": operands,
        "product_low_bit": product_low,
        "product_high_bit": product_high,
        "signed_a": True,
        "signed_b": True,
        "primitive_shell": shell,
        "shell_output_nets": output_nets,
        "quotient_groups": output_groups,
        "scalar_companions": scalar_outputs,
        "control_contract": control,
        "output_cut_coverage": coverage,
        "semantic_score": 2000 + max(0, dsp_count * 10 - len(shell["nodes"])),
        "product_cut_net_count": len(semantic_bits),
    }


def _divider_candidate(
    netlist,
    owner_name: str,
    owner,
    *,
    distance: int,
) -> Optional[dict[str, Any]]:
    boundary = _indexed_boundary_groups(netlist, owner)
    inputs = boundary["input"]
    outputs = boundary["output"]
    candidates = []
    for numerator in inputs:
        if _NUMERATOR_ROLE.search(str(numerator["stem"])) is None:
            continue
        for denominator in inputs:
            if denominator is numerator or denominator["width"] != numerator["width"]:
                continue
            word_width = int(numerator["width"])
            if not 8 <= word_width <= 64:
                continue
            quotient_groups = [
                group for group in outputs
                if int(group["width"]) == 2 * word_width - 1
                and _QUOTIENT_ROLE.search(str(group["stem"])) is not None
            ]
            if not quotient_groups:
                continue
            other_same_width_inputs = sum(
                int(group["width"]) == word_width for group in inputs
            ) - 2
            role_score = 2 + int(
                _DENOMINATOR_ROLE.search(str(denominator["stem"])) is not None
            )
            candidates.append({
                "operator": "unsigned_fractional_divider_slice",
                "owner_name": owner_name,
                "distance": distance,
                "word_width": word_width,
                "numerator": numerator,
                "denominator": denominator,
                "quotient_groups": quotient_groups,
                "role_score": role_score,
                "semantic_score": role_score,
                "unrelated_word_group_count": max(0, other_same_width_inputs),
            })
    if not candidates:
        return None
    candidates.sort(key=lambda item: (
        -item["distance"],
        len(item["quotient_groups"]),
        item["role_score"],
        -item["unrelated_word_group_count"],
    ), reverse=True)
    return candidates[0]


def recover_consumer_driven_operator_contract(
    design,
    *,
    critical_paths_data: list[list[str]],
    candidate_id: str,
    design_sha256: Optional[str] = None,
) -> dict[str, Any]:
    """Recover one executable consumer-driven contract from a live family."""
    sequential = recover_sequential_operator_contract(
        design,
        critical_paths_data=critical_paths_data,
        candidate_id=candidate_id,
        design_sha256=design_sha256,
    )
    members = [
        str(item.get("cell") or "")
        for item in sequential.get("member_facts") or []
        if isinstance(item, dict) and item.get("cell")
    ]
    common_owner = _common_parent(members)
    netlist = design.getNetlist()
    candidates = []
    candidate_diagnostics: dict[str, Any] = {}
    for distance, owner_name in enumerate(_ancestor_names(common_owner)):
        owner = netlist.getHierCellInstFromName(owner_name)
        if owner is None:
            continue
        owner_candidates = [
            (
                _reciprocal_sqrt_candidate(
                    design,
                    netlist,
                    owner_name,
                    owner,
                    distance=distance,
                    design_sha256=design_sha256,
                    critical_paths_data=critical_paths_data,
                    diagnostics=candidate_diagnostics,
                )
                if distance == 0 else None
            ),
            _divider_candidate(
                netlist, owner_name, owner, distance=distance
            ),
            _integer_sqrt_candidate(
                netlist, owner_name, owner, distance=distance
            ),
            _quantized_sqrt_candidate(
                netlist,
                owner_name,
                owner,
                distance=distance,
                diagnostics=candidate_diagnostics,
            ),
        ]
        for candidate in owner_candidates:
            if candidate is None:
                continue
            candidate["primitive_type_counts"] = _owner_primitive_counts(
                netlist, owner
            )
            candidates.append(candidate)
    path_owner_names = []
    seen_path_owners = set()
    for path in critical_paths_data:
        for token in path:
            cell_name = _resolve_path_cell_name(design, token)
            if cell_name is None:
                continue
            for owner_name in _ancestor_names(cell_name.rsplit("/", 1)[0], limit=5):
                if owner_name not in seen_path_owners:
                    seen_path_owners.add(owner_name)
                    path_owner_names.append(owner_name)
    for distance, owner_name in enumerate(path_owner_names):
        owner = netlist.getHierCellInstFromName(owner_name)
        if owner is None:
            continue
        candidate = _wide_product_boolean_shell_candidate(
            design,
            netlist,
            owner_name,
            owner,
            distance=distance,
            critical_paths_data=critical_paths_data,
            diagnostics=candidate_diagnostics,
        )
        if candidate is None:
            continue
        candidate["primitive_type_counts"] = _owner_primitive_counts(
            netlist, owner
        )
        candidates.append(candidate)
    if not candidates:
        raise ConsumerOperatorContractError(
            "no consumer boundary matches a supported registered arithmetic "
            "contract; anonymous_diagnostics="
            + json.dumps(candidate_diagnostics, sort_keys=True)
        )
    validated_candidates = []
    rejected_scope_count = 0
    for candidate in candidates:
        owner = netlist.getHierCellInstFromName(candidate["owner_name"])
        if owner is None:
            rejected_scope_count += 1
            continue
        if not isinstance(candidate.get("control_contract"), dict):
            try:
                candidate["control_contract"] = _owner_control_contract(
                    netlist, owner
                )
            except ConsumerOperatorContractError:
                rejected_scope_count += 1
                continue
        owner_path_coverage = _output_cut_coverage(
            design,
            netlist,
            owner_name=str(candidate["owner_name"]),
            selected_groups=_candidate_output_groups(candidate),
            critical_paths_data=critical_paths_data,
        )
        if "output_cut_coverage" not in candidate:
            candidate["output_cut_coverage"] = owner_path_coverage
        else:
            for key in (
                "owner_path_count", "owner_path_ranks",
                "resolved_exit_path_count", "resolved_exit_path_ranks",
            ):
                candidate["output_cut_coverage"][key] = (
                    owner_path_coverage[key]
                )
        candidate["output_cut_coverage"].update(
            _complete_owner_output_cut(owner, candidate)
        )
        coverage = candidate["output_cut_coverage"]
        if coverage["complete_owner_output_cut"]:
            # Replacing every observable owner output necessarily removes each
            # live path segment inside that owner.  The first external sink is
            # only needed to prove coverage for a partial output cut.
            covered_ranks = list(coverage.get("owner_path_ranks") or [])
            path_count = int(coverage.get("ordered_path_count") or 0)
            coverage.update({
                "covered_path_count": len(covered_ranks),
                "covered_path_ranks": covered_ranks,
                "covers_worst_path": 1 in covered_ranks,
                "top5_covered_count": sum(
                    rank <= min(5, path_count) for rank in covered_ranks
                ),
                "top10_covered_count": sum(
                    rank <= min(10, path_count) for rank in covered_ranks
                ),
                "all_path_coverage_ratio": (
                    round(len(covered_ranks) / path_count, 4)
                    if path_count else 0.0
                ),
                "coverage_basis": (
                    "complete_owner_replacement_dominates_internal_live_path"
                ),
            })
        candidate["owner_output_cut_profiles"] = _owner_output_cut_profiles(
            design,
            netlist,
            owner_name=str(candidate["owner_name"]),
            owner=owner,
            critical_paths_data=critical_paths_data,
        )
        validated_candidates.append(candidate)
    if not validated_candidates:
        raise ConsumerOperatorContractError(
            "no consumer boundary has a complete registered control contract"
        )
    # A semantic match is useful only when its selected outputs actually cut
    # the current timing wall. Prefer worst-path domination before semantic
    # breadth or ancestor distance.
    def candidate_cuts_worst(item: dict[str, Any]) -> bool:
        coverage = item.get("output_cut_coverage") or {}
        return bool(
            coverage.get("covers_worst_path")
            or coverage.get("complete_owner_output_cut")
            and 1 in (coverage.get("owner_path_ranks") or [])
        )

    def candidate_structural_contraction(item: dict[str, Any]) -> float:
        """Rank exact semantic cuts ahead of composites that retain the owner."""
        producer_count = sum(
            int(value or 0)
            for value in (item.get("primitive_type_counts") or {}).values()
        )
        if producer_count <= 0:
            return 0.0
        shell = item.get("primitive_shell")
        if not isinstance(shell, dict):
            # Pure semantic operators do not copy an anonymous primitive shell.
            return 1.0
        retained_count = len(shell.get("nodes") or [])
        return max(0.0, min(1.0, 1.0 - retained_count / producer_count))

    validated_candidates.sort(key=lambda item: (
        candidate_cuts_worst(item),
        candidate_structural_contraction(item),
        int((item.get("output_cut_coverage") or {}).get("covered_path_count") or 0),
        item["distance"],
        item.get("semantic_score", 0),
        len(item.get("quotient_groups") or []),
        item.get("role_score", 0),
        -item.get("unrelated_word_group_count", 0),
    ), reverse=True)
    selected = validated_candidates[0]
    cut_coverage = selected["output_cut_coverage"]
    cut_dominates_live_wall = candidate_cuts_worst(selected)
    control = selected["control_contract"]
    operator = str(selected["operator"])
    semantic_recovery: dict[str, Any]
    if operator == "signed_wide_product_boolean_shell":
        operand_width = int(selected["operand_width"])
        rewrite_input_nets = [
            str(net)
            for operand in selected["operands"]
            for net in operand["nets"]
        ]
        primitive_shell = dict(selected["primitive_shell"])
        primitive_shell_external_nets = list(
            primitive_shell.pop("external_nets")
        )
        output_nets = list(selected["shell_output_nets"])
        action_eligible = cut_dominates_live_wall
        consumer_input_widths = [operand_width, operand_width]
        observable_output_widths = [len(output_nets)]
        depth = 0
        depth_basis = "combinational_owner_before_existing_registered_boundary"
        module_name, instance_name = _rewrite_identifiers(
            "fdagents_wide_product_shell", selected
        )
        bundle = {
            "version": 1,
            "kind": "registered_consumer_rewrite_bundle",
            "plans": [{
                "version": 1,
                "module_name": module_name,
                "operator": operator,
                "input_width": 2 * operand_width,
                "output_width": len(output_nets),
                "operand_width": operand_width,
                "product_low_bit": int(selected["product_low_bit"]),
                "product_high_bit": int(selected["product_high_bit"]),
                "signed_a": bool(selected["signed_a"]),
                "signed_b": bool(selected["signed_b"]),
                "primitive_shell": primitive_shell,
                "control_protocol": {
                    key: control[key]
                    for key in (
                        "clock_edge", "advance", "reset", "register_init"
                    )
                },
                "absorbed_control_values": [],
            }],
            "instances": [{
                "name": instance_name,
                "module_name": module_name,
                "clock_net": control["clock_net"],
                "input_nets": rewrite_input_nets,
                "output_nets": output_nets,
                "output_alias_nets": [[] for _net in output_nets],
                "absorbed_control_nets": [],
                "cleanup_scope": selected["owner_name"],
                "primitive_shell_external_nets": primitive_shell_external_nets,
                "boolean_shells": [],
                "semantic_slices": [],
            }],
        } if action_eligible else None
        semantic_candidates = [{
            "operator_class": operator,
            "state": (
                "supported_for_measurement_probe"
                if action_eligible else "partial"
            ),
            "evidence": [
                "current_source_two_shifted_operand_dsp_lane_boundaries",
                "complete_internal_product_cut",
                "complete_observable_boolean_shell",
                "existing_registered_output_schedule_preserved",
            ],
        }]
        semantic_recovery = {
            "operator_class": operator,
            "data_semantics": "signed_two_operand_product_with_exact_boolean_shell",
            "control_semantics": "combinational_owner_before_existing_registers",
            "operand_width": operand_width,
            "product_low_bit": int(selected["product_low_bit"]),
            "product_high_bit": int(selected["product_high_bit"]),
            "primitive_shell_node_count": len(primitive_shell["nodes"]),
            "product_cut_net_count": int(selected["product_cut_net_count"]),
        }
    elif operator == "unsigned_fractional_divider_slice":
        output_groups = selected["quotient_groups"]
        depth, depth_basis = _pipeline_depth(
            output_groups, selected["word_width"]
        )
        action_eligible = depth is not None and cut_dominates_live_wall
        consumer_input_widths = [
            selected["word_width"], selected["word_width"]
        ]
        observable_output_widths = [
            int(group["width"]) for group in output_groups
        ]
        output_nets = [
            net for group in output_groups for net in group["nets"]
        ]
        module_name, instance_name = _rewrite_identifiers(
            "fdagents_consumer_arithmetic_rewrite", selected
        )
        bundle = {
            "version": 1,
            "kind": "registered_consumer_rewrite_bundle",
            "plans": [{
                "version": 1,
                "module_name": module_name,
                "operator": "unsigned_fractional_divider_slice",
                "input_width": 2 * selected["word_width"],
                "output_width": sum(
                    int(group["width"]) for group in output_groups
                ),
                "word_width": selected["word_width"],
                "pipeline_depth": depth,
                "output_copies": len(output_groups),
                "control_protocol": {
                    key: control[key]
                    for key in (
                        "clock_edge", "advance", "reset", "register_init"
                    )
                },
            }],
            "instances": [{
                "name": instance_name,
                "module_name": module_name,
                "clock_net": control["clock_net"],
                "input_nets": (
                    list(selected["denominator"]["nets"])
                    + list(selected["numerator"]["nets"])
                ),
                "output_nets": output_nets,
                "cleanup_scope": selected["owner_name"],
            }],
        } if action_eligible else None
        semantic_candidates = [
            {
                "operator_class": "unsigned_fractional_divider_consumer_slice",
                "state": (
                    "supported_for_measurement_probe"
                    if action_eligible else "partial"
                ),
                "evidence": [
                    "two_equal_width_input_words",
                    "quotient_role_boundary",
                    "two_word_minus_one_observable_width",
                    "free_running_registered_output_schedule",
                    "wide_lut_carry_state_on_current_timing_wall",
                ],
            },
            {
                "operator_class": "wide_product_or_reduction_slice",
                "state": "contradicted_by_quotient_boundary_role",
            },
        ]
        semantic_recovery = {
            "operator_class": operator,
            "data_semantics": "equal_width_fractional_division",
            "control_semantics": "complete_free_running_registered_owner",
        }
    elif operator == "unsigned_constant_reciprocal_sqrt_slice":
        sqrt_depth = int(selected["pipeline_depth"])
        division_depth = int(selected["division_pipeline_depth"])
        depth = sqrt_depth + division_depth
        depth_basis = (
            str(selected["recurrence_signature"]["evidence_basis"])
            + "+"
            + str(selected["division_pipeline_depth_basis"])
        )
        rewrite_input_nets = list(selected["full_input"]["nets"])
        root_output_nets = list(selected["output_group"]["nets"])
        primitive_shell = dict(selected["primitive_shell"])
        primitive_shell_external_nets = list(
            primitive_shell.pop("external_nets")
        )
        shell_output_nets = list(selected["shell_output_nets"])
        producer_primitive_count = sum(
            int(value or 0)
            for value in selected["primitive_type_counts"].values()
        )
        retained_shell_count = len(primitive_shell["nodes"])
        quotient_cut_coverage = selected.get("quotient_cut_coverage") or {}
        use_internal_quotient_cut = bool(
            producer_primitive_count
            and retained_shell_count / producer_primitive_count >= 0.5
            and quotient_cut_coverage.get("covers_worst_path") is True
            and int(quotient_cut_coverage.get("covered_path_count") or 0) > 0
            and len(selected["quotient_nets"])
            == int(selected["quotient_high_bit"])
            - int(selected["quotient_low_bit"])
            + 1
        )
        if use_internal_quotient_cut:
            output_nets = list(selected["quotient_nets"])
            output_alias_nets = [
                [net for net in group if net != selected_net]
                for selected_net, group in zip(
                    output_nets, selected["quotient_net_groups"]
                )
            ]
            cut_coverage = quotient_cut_coverage
            cut_dominates_live_wall = True
            emitted_primitive_shell = None
            primitive_shell_external_nets = []
            expose_root = False
            expose_quotient = True
            expose_overflow = False
        else:
            output_nets = root_output_nets + shell_output_nets
            output_alias_nets = [[] for _net in output_nets]
            emitted_primitive_shell = primitive_shell
            expose_root = True
            expose_quotient = False
            expose_overflow = False
        action_eligible = cut_dominates_live_wall
        consumer_input_widths = [len(rewrite_input_nets)]
        observable_output_widths = (
            [len(output_nets)]
            if use_internal_quotient_cut
            else [len(root_output_nets), len(shell_output_nets)]
        )
        module_name, instance_name = _rewrite_identifiers(
            "fdagents_reciprocal_sqrt_rewrite", selected
        )
        bundle = {
            "version": 1,
            "kind": "registered_consumer_rewrite_bundle",
            "plans": [{
                "version": 1,
                "module_name": module_name,
                "operator": operator,
                "input_width": len(rewrite_input_nets),
                "output_width": len(output_nets),
                "numerator_width": int(selected["numerator_width"]),
                "numerator_constant": int(selected["numerator_constant"]),
                "quotient_low_bit": int(selected["quotient_low_bit"]),
                "quotient_high_bit": int(selected["quotient_high_bit"]),
                "overflow_low_bit": int(selected["overflow_low_bit"]),
                "overflow_high_bit": int(selected["overflow_high_bit"]),
                "sqrt_pipeline_depth": sqrt_depth,
                "division_pipeline_depth": division_depth,
                "expose_root": expose_root,
                "expose_quotient": expose_quotient,
                "expose_overflow": expose_overflow,
                "primitive_shell": emitted_primitive_shell,
                "control_protocol": {
                    key: control[key]
                    for key in (
                        "clock_edge", "advance", "reset", "register_init"
                    )
                },
                "absorbed_control_values": [],
                "boolean_shells": [],
                "semantic_slices": [],
            }],
            "instances": [{
                "name": instance_name,
                "module_name": module_name,
                "clock_net": control["clock_net"],
                "input_nets": rewrite_input_nets,
                "output_nets": output_nets,
                "output_alias_nets": output_alias_nets,
                "absorbed_control_nets": [],
                "cleanup_scope": (
                    selected["divider_owner_name"]
                    if use_internal_quotient_cut
                    else selected["owner_name"]
                ),
                "primitive_shell_external_nets": primitive_shell_external_nets,
                "boolean_shells": [],
                "semantic_slices": [],
            }],
        }
        semantic_candidates = [{
            "operator_class": operator,
            "state": "supported_for_measurement_probe",
            "evidence": [
                "current_source_integer_sqrt_recurrence",
                "single_input_constant_numerator_divider",
                "observable_quotient_slice_and_overflow_cut",
                "semantic_cut_intersects_current_worst_path",
                "free_running_zero_state_schedule",
            ],
        }, {
            "operator_class": "standalone_integer_sqrt",
            "state": "contradicted_by_live_downstream_quotient_cut",
        }]
        semantic_recovery = {
            "operator_class": operator,
            "data_semantics": "constant_reciprocal_of_unsigned_floor_sqrt_slice",
            "control_semantics": "complete_free_running_registered_chain",
            "sqrt_recurrence_signature": selected["recurrence_signature"],
            "quotient_low_bit": int(selected["quotient_low_bit"]),
            "quotient_high_bit": int(selected["quotient_high_bit"]),
            "overflow_low_bit": int(selected["overflow_low_bit"]),
            "overflow_high_bit": int(selected["overflow_high_bit"]),
            "sqrt_pipeline_depth": sqrt_depth,
            "division_pipeline_depth": division_depth,
            "complete_owner_output_shell_recovered": not use_internal_quotient_cut,
            "rewrite_cut": (
                "internal_quotient_semantic_cut"
                if use_internal_quotient_cut
                else "complete_owner_output_cut"
            ),
            "primitive_shell_node_count": (
                0 if use_internal_quotient_cut
                else len(primitive_shell["nodes"])
            ),
            "primitive_shell_external_count": (
                0 if use_internal_quotient_cut
                else primitive_shell["external_count"]
            ),
            "numerator_low_discarded_bit_count": int(
                selected["semantic_cut_diagnostics"][
                    "numerator_low_discarded_bit_count"
                ]
            ),
        }
    elif operator in {
        "unsigned_integer_sqrt_digit_recurrence",
        "unsigned_quantized_sqrt_digit_recurrence",
    }:
        depth = int(selected["pipeline_depth"])
        depth_basis = str(
            selected["recurrence_signature"]["evidence_basis"]
        )
        if operator == "unsigned_integer_sqrt_digit_recurrence":
            rewrite_input_nets = list(selected["full_input"]["nets"])
        else:
            rewrite_input_nets = list(selected["reduced_input_nets"])
        output_group = selected["output_group"]
        output_nets = list(output_group["nets"])
        scalar_companions = list(selected.get("scalar_companions") or [])
        action_eligible = cut_dominates_live_wall
        consumer_input_widths = [len(rewrite_input_nets)]
        observable_output_widths = [int(output_group["width"])]
        module_name, instance_name = _rewrite_identifiers(
            "fdagents_sqrt_rewrite", selected
        )
        bundle = {
            "version": 1,
            "kind": "registered_consumer_rewrite_bundle",
            "plans": [{
                "version": 1,
                "module_name": module_name,
                "operator": operator,
                "input_width": len(rewrite_input_nets),
                "output_width": len(output_nets),
                "pipeline_depth": depth,
                "control_protocol": {
                    key: control[key]
                    for key in (
                        "clock_edge", "advance", "reset", "register_init"
                    )
                },
                "absorbed_control_values": [
                    int(item["quiescent_value"])
                    for item in scalar_companions
                ],
            }],
            "instances": [{
                "name": instance_name,
                "module_name": module_name,
                "clock_net": control["clock_net"],
                "input_nets": rewrite_input_nets,
                "output_nets": output_nets,
                "absorbed_control_nets": [
                    str(item["net"]) for item in scalar_companions
                ],
                "cleanup_scope": selected["owner_name"],
            }],
        }
        operator_evidence = [
            "contiguous_observable_root_slice",
            "wide_registered_state_series",
            "monotone_taper_state_series",
            "free_running_zero_state_schedule",
            "local_scalar_companion_truth_table",
        ]
        semantic_recovery = {
            "operator_class": operator,
            "recurrence_signature": selected["recurrence_signature"],
            "scalar_companion_control_count": len(scalar_companions),
            "scalar_companion_control_basis": sorted({
                str(item["basis"]) for item in scalar_companions
            }),
            "scalar_companion_equivalence_scope": (
                "measurement_probe_quiescent_value_not_formal_sequential_proof"
            ),
            "control_semantics": "complete_free_running_registered_owner",
        }
        if operator == "unsigned_integer_sqrt_digit_recurrence":
            operator_evidence.append("complete_zero_based_root_word")
            semantic_recovery.update({
                "data_semantics": "full_width_unsigned_floor_sqrt",
                "observable_output_lsb": 0,
                "reduced_input_low_bit": 0,
            })
            competing_candidate = {
                "operator_class": "unsigned_quantized_sqrt_digit_recurrence",
                "state": "contradicted_by_complete_zero_based_root_word",
            }
        else:
            operator_evidence.append("paired_input_bit_elision_identity")
            semantic_recovery.update({
                "data_semantics": (
                    "floor_sqrt_quantization_commutes_with_even_input_shift"
                ),
                "observable_output_lsb": selected["output_slice_lsb"],
                "reduced_input_low_bit": selected["reduced_input_low_bit"],
            })
            competing_candidate = {
                "operator_class": "unsigned_full_width_integer_sqrt",
                "state": "contradicted_by_narrow_observable_slice",
            }
        semantic_candidates = [{
            "operator_class": operator,
            "state": "supported_for_measurement_probe",
            "evidence": operator_evidence,
        }, competing_candidate]
    else:  # pragma: no cover - candidate constructors are closed above
        raise ConsumerOperatorContractError(
            f"unsupported live consumer operator class: {operator}"
        )

    if bundle is not None:
        try:
            from FDAgents.consumer_rewrite import validate_consumer_rewrite_bundle
            bundle = validate_consumer_rewrite_bundle(bundle)
        except (ImportError, ValueError) as exc:
            raise ConsumerOperatorContractError(
                f"live consumer bundle validation failed: {exc}"
            ) from exc
    current_seed_realization = _current_seed_rewrite_realization(
        netlist, bundle
    )
    realization_status = current_seed_realization["status"]
    if realization_status != "not_realized":
        action_eligible = False
    coverage = cut_coverage
    public_contract = {
        "producer_registered_width": int(
            (sequential.get("bit_mapping") or {}).get("member_count") or 0
        ),
        "producer_primitive_type_counts": selected["primitive_type_counts"],
        "current_wall_coverage": {
            key: coverage.get(key)
            for key in (
                "covered_path_count", "ordered_path_count",
                "covers_worst_path", "top5_covered_count", "top10_covered_count",
                "owner_path_count", "resolved_exit_path_count", "coverage_basis",
            )
        },
        "consumer_input_word_widths": consumer_input_widths,
        "observable_output_word_widths": observable_output_widths,
        "producer_to_consumer_contraction": {
            "registered_state_bits": int(
                (sequential.get("bit_mapping") or {}).get("member_count") or 0
            ),
            "observable_output_bits": sum(observable_output_widths),
            "output_copy_count": len(observable_output_widths),
        },
        "control_protocol": {
            "clock_edge": control["clock_edge"],
            "advance": control["advance"],
            "reset": control["reset"],
            "register_init": control["register_init"],
            "registered_state_count": control["registered_state_count"],
            "registered_primitive_type_counts": control[
                "registered_primitive_type_counts"
            ],
        },
        "pipeline_depth": depth,
        "pipeline_depth_basis": depth_basis,
        "semantic_candidates": semantic_candidates,
        "semantic_recovery": semantic_recovery,
        "current_seed_realization": current_seed_realization,
        "missing_proof": [
            "whole_dcp_functional_equivalence",
            "routed_fmax_payoff",
        ],
        "predicted_surviving_wall": "unknown_until_post_route_reprofile",
    }
    result = {
        "status": "success",
        "kind": "consumer_driven_operator_contract",
        "version": 1,
        "read_only": True,
        "fresh_rediscovery": True,
        "candidate_id": candidate_id,
        "design_sha256": design_sha256,
        "public_contract": public_contract,
        "private_contract": {
            "selected_operator": operator,
            "selected_owner": selected["owner_name"],
            "bundle": bundle,
            "output_cut_coverage": cut_coverage,
            "owner_output_cut_profiles": selected[
                "owner_output_cut_profiles"
            ],
            "candidate_count": len(candidates),
            "validated_candidate_count": len(validated_candidates),
            "rejected_scope_count": rejected_scope_count,
            "candidate_diagnostics": candidate_diagnostics,
        },
        "action_assessment": {
            "status": (
                "executable"
                if action_eligible
                else "live_output_cut_not_dominating"
                if not cut_dominates_live_wall
                else realization_status
                if realization_status != "not_realized"
                else "probe_only"
            ),
            "authority": (
                "current_seed_netlist_realization_census"
                if realization_status != "not_realized"
                else "current_seed_output_cut_domination"
            ),
            "candidate_action": (
                {
                    "skill": "operator_rewrite",
                    "target": "consumer_driven_registered_arithmetic_rewrite_v1",
                    "params": {"family_ref": "framework_resolves_current_ref"},
                }
                if action_eligible else None
            ),
            "not_a_hard_trigger": True,
            "luna_owns_final_selection": True,
        },
        "mutation_eligible": False,
        "proof_status": (
            "current_seed_rewrite_already_realized"
            if realization_status == "already_realized_current_seed"
            else "current_seed_rewrite_namespace_conflict"
            if realization_status == "partial_namespace_conflict_current_seed"
            else "live_consumer_output_cut_not_dominating"
            if not cut_dominates_live_wall
            else "live_consumer_contract_extracted_unproved"
        ),
    }
    if not cut_dominates_live_wall:
        result["typed_refusal"] = {
            "code": "LIVE_OUTPUT_CUT_NOT_DOMINATING",
            "reason": (
                "the selected semantic output group does not cut the current "
                "worst path at the recovered owner boundary"
            ),
        }
    result["contract_sha256"] = _digest(result)
    return result


__all__ = [
    "ConsumerOperatorContractError",
    "recover_consumer_driven_operator_contract",
]
