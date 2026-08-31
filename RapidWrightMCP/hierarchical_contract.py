"""Read-only structural contracts for implemented hierarchical operators."""

from __future__ import annotations

import hashlib
import heapq
import json
import re
from collections import Counter, deque
from typing import Any, Optional


CONTRACT_VERSION = 1
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_BUS_BIT_RE = re.compile(r"^(?P<base>.*)\[(?P<index>\d+)\]$")
_SEQUENTIAL_TYPES = (
    "FD", "LD", "RAM", "URAM", "SRL", "DSP", "FIFO", "BUFG", "MMCME", "PLLE",
)
_COMBINATIONAL_TYPES = (
    "LUT", "CARRY", "MUXF", "MUXCY", "XORCY", "AND", "NAND", "OR", "NOR",
    "XOR", "XNOR", "INV", "BUF", "GND", "VCC",
)
_CONTROL_PINS = {
    "C", "CE", "CLR", "CLK", "G", "GE", "PRE", "R", "RD", "RE", "RST", "S",
    "SET", "SR", "WE", "WCLK",
}
_FUNCTIONAL_PROPERTY_RE = re.compile(
    r"^(?:INIT|INITP|SRVAL|IS_|WRITE_MODE|READ_WIDTH|WRITE_WIDTH|DO[AB]_REG|"
    r"RAM_MODE|CLOCKING_MODE|EN_ECC|AREG|BREG|CREG|DREG|MREG|PREG|ADREG|"
    r"ACASCREG|BCASCREG|INMODEREG|OPMODEREG|ALUMODEREG|CARRYINREG|"
    r"CARRYINSELREG|CARRY_TYPE|USE_|A_INPUT|B_INPUT|AMULTSEL|BMULTSEL)",
    re.IGNORECASE,
)


class HierarchicalContractError(ValueError):
    """Raised when a complete read-only operator contract cannot be extracted."""


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
    payload = json.dumps(
        _stable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _properties(instance) -> dict[str, str]:
    values = {}
    properties = instance.getPropertiesMap()
    try:
        entries = properties.entrySet()
        for entry in entries:
            value = entry.getValue()
            values[str(entry.getKey())] = str(value.getValue())
    except AttributeError:
        for key, value in dict(properties).items():
            raw = value.getValue() if hasattr(value, "getValue") else value
            values[str(key)] = str(raw)
    return dict(sorted(values.items()))


def _functional_properties(instance) -> dict[str, str]:
    return {
        key: value
        for key, value in _properties(instance).items()
        if _FUNCTIONAL_PROPERTY_RE.match(key)
    }


def _cell_name(pin) -> str:
    return str(pin.getFullHierarchicalInstName())


def _pin_name(pin) -> str:
    return str(pin.getPortInst().getName())


def _cell_type(pin) -> str:
    cell_type = pin.getCellType()
    return str(cell_type.getName()).upper() if cell_type is not None else "TOP_PORT"


def _inside(name: str, instance_name: str) -> bool:
    return name == instance_name or name.startswith(instance_name + "/")


def _parent_net(netlist, pin):
    hierarchical = pin.getHierarchicalNet()
    if hierarchical is None:
        return None
    parent = netlist.getParentNet(hierarchical)
    if parent is None:
        raise HierarchicalContractError(f"pin {pin} has no canonical parent net")
    return parent


def _net_name(net) -> str:
    return str(net.getHierarchicalNetName())


def _pin_direction(pin) -> str:
    if bool(pin.isInput()):
        return "input"
    if bool(pin.isOutput()):
        return "output"
    return "inout"


def _port_direction(port) -> str:
    if bool(port.isInput()):
        return "input"
    if bool(port.isOutput()):
        return "output"
    return "inout"


def _physical_binding(design, cell_name: str) -> Optional[dict[str, str]]:
    cell = design.getCell(cell_name)
    if cell is None or not bool(cell.isPlaced()):
        return None
    site = cell.getSite()
    return {
        "site": str(site.getName()) if site is not None else "",
        "bel": str(cell.getBELName()),
    }


def _leaf_record(design, netlist, leaf, instance_name: str) -> dict[str, Any]:
    name = str(leaf.getFullHierarchicalInstName())
    pins = []
    for pin in sorted(_as_list(leaf.getHierPortInsts()), key=str):
        parent = _parent_net(netlist, pin)
        pins.append({
            "pin": _pin_name(pin),
            "direction": _pin_direction(pin),
            "canonical_net": _net_name(parent) if parent is not None else None,
        })
    record = {
        "name": name,
        "relative_name": (
            name[len(instance_name) + 1:] if _inside(name, instance_name) else name
        ),
        "type": str(leaf.getCellType().getName()).upper(),
        "functional_properties": _functional_properties(leaf.getInst()),
        "pins": pins,
    }
    binding = _physical_binding(design, name)
    if binding is not None:
        record["physical"] = binding
    return record


def _canonical_net_record(net, instance_name: str) -> dict[str, Any]:
    sources = _as_list(net.getLeafHierPortInsts(True, False, True))
    sinks = _as_list(net.getLeafHierPortInsts(False, True, True))
    source_names = {str(pin) for pin in sources}
    sink_names = {str(pin) for pin in sinks}
    pins = []
    for pin in sorted({str(pin): pin for pin in sources + sinks}.values(), key=str):
        identity = str(pin)
        roles = []
        if identity in source_names:
            roles.append("source")
        if identity in sink_names:
            roles.append("sink")
        name = _cell_name(pin)
        pins.append({
            "cell": name,
            "pin": _pin_name(pin),
            "type": _cell_type(pin),
            "direction": _pin_direction(pin),
            "roles": roles,
            "inside_region": _inside(name, instance_name),
        })
    return {
        "name": _net_name(net),
        "source_count": len(sources),
        "sink_count": len(sinks),
        "pins": pins,
    }


def _is_sequential(cell_type: str) -> bool:
    value = cell_type.upper()
    return value.startswith(_SEQUENTIAL_TYPES)


def _is_combinational(cell_type: str) -> bool:
    value = cell_type.upper()
    return value.startswith(_COMBINATIONAL_TYPES)


def recover_static_delay_endpoint_contract(cell: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Decode a statically addressed SRL endpoint from its extracted pin facts."""
    cell_type = str(cell.get("type", "")).upper()
    address_width = {"SRL16E": 4, "SRLC32E": 5}.get(cell_type)
    if address_width is None:
        return None
    pins = {pin.get("pin"): pin for pin in cell.get("pins", [])}
    bits = []
    for index in range(address_width):
        pin = pins.get(f"A[{index}]") or pins.get(f"A{index}")
        net_name = str(pin.get("canonical_net") if pin is not None else "")
        if net_name.endswith("/<const0>") or net_name in {"<const0>", "GLOBAL_LOGIC0"}:
            bits.append(0)
        elif net_name.endswith("/<const1>") or net_name in {"<const1>", "GLOBAL_LOGIC1"}:
            bits.append(1)
        else:
            return {
                "cell": cell.get("name"),
                "type": cell_type,
                "static_address": False,
                "address_width": address_width,
                "mutation_eligible": False,
            }
    address = sum(bit << index for index, bit in enumerate(bits))
    return {
        "cell": cell.get("name"),
        "type": cell_type,
        "static_address": True,
        "address_width": address_width,
        "address_bits_lsb_first": bits,
        "address_value": address,
        "delay_cycles": address + 1,
        "clock_net": (pins.get("CLK") or {}).get("canonical_net"),
        "enable_net": (pins.get("CE") or {}).get("canonical_net"),
        "data_input_net": (pins.get("D") or {}).get("canonical_net"),
        "data_output_net": (pins.get("Q") or {}).get("canonical_net"),
        "functional_properties": cell.get("functional_properties", {}),
        "mutation_eligible": False,
    }


def _boundary_groups(boundary: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[tuple[Optional[int], str]]] = {}
    for item in boundary:
        port = item["port"]
        match = _BUS_BIT_RE.fullmatch(port)
        base = match.group("base") if match else port
        index = int(match.group("index")) if match else None
        grouped.setdefault((item["direction"], base), []).append((index, port))
    result = []
    for (direction, base), members in sorted(grouped.items()):
        indices = sorted(index for index, _port in members if index is not None)
        result.append({
            "direction": direction,
            "base": base,
            "width": len(members),
            "indices": indices,
            "contiguous_zero_based": indices == list(range(len(indices))) if indices else None,
        })
    return result


def normalized_topology_sha256(
    cells: list[dict[str, Any]], nets: list[dict[str, Any]]
) -> str:
    """Return a name- and placement-independent color-refinement digest."""
    cell_by_name = {cell["name"]: cell for cell in cells}
    net_by_name = {net["name"]: net for net in nets}
    cell_labels = {
        name: _digest({
            "kind": "cell",
            "type": cell["type"],
            "functional_properties": cell.get("functional_properties", {}),
        })
        for name, cell in cell_by_name.items()
    }
    net_labels = {name: _digest({"kind": "net"}) for name in net_by_name}
    for _round in range(8):
        next_cells = {}
        for name, cell in cell_by_name.items():
            neighbours = []
            for pin in cell.get("pins", []):
                net_name = pin.get("canonical_net")
                if net_name in net_labels:
                    neighbours.append((
                        pin.get("pin"), pin.get("direction"), net_labels[net_name]
                    ))
            next_cells[name] = _digest({
                "self": cell_labels[name],
                "neighbours": sorted(neighbours),
            })
        next_nets = {}
        for name, net in net_by_name.items():
            neighbours = []
            for pin in net.get("pins", []):
                cell_name = pin.get("cell")
                if cell_name in cell_labels:
                    neighbours.append((
                        tuple(pin.get("roles", [])), pin.get("pin"),
                        pin.get("direction"), cell_labels[cell_name],
                    ))
            next_nets[name] = _digest({
                "self": net_labels[name],
                "neighbours": sorted(neighbours),
            })
        cell_labels, net_labels = next_cells, next_nets
    return _digest({
        "cell_colors": sorted(cell_labels.values()),
        "net_colors": sorted(net_labels.values()),
    })


def _latency_matrix(
    cell_records: list[dict[str, Any]],
    boundary: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    adjacency: dict[str, list[tuple[str, int]]] = {}
    for cell in cell_records:
        inputs = [
            pin for pin in cell["pins"]
            if pin["direction"] == "input" and pin["canonical_net"] is not None
        ]
        outputs = [
            pin for pin in cell["pins"]
            if pin["direction"] == "output" and pin["canonical_net"] is not None
        ]
        sequential = _is_sequential(cell["type"])
        if sequential:
            inputs = [pin for pin in inputs if pin["pin"].upper() == "D"]
        else:
            inputs = [pin for pin in inputs if pin["pin"].upper() not in _CONTROL_PINS]
        weight = 1 if sequential else 0
        for source in inputs:
            for sink in outputs:
                adjacency.setdefault(source["canonical_net"], []).append(
                    (sink["canonical_net"], weight)
                )

    groups: dict[tuple[str, str], set[str]] = {}
    for item in boundary:
        match = _BUS_BIT_RE.fullmatch(item["port"])
        base = match.group("base") if match else item["port"]
        if item["canonical_net"] is not None:
            groups.setdefault((item["direction"], base), set()).add(
                item["canonical_net"]
            )
    reverse: dict[str, list[str]] = {}
    for source, edges in adjacency.items():
        for target, _weight in edges:
            reverse.setdefault(target, []).append(source)
    inputs = {base: nets for (direction, base), nets in groups.items() if direction == "input"}
    outputs = {base: nets for (direction, base), nets in groups.items() if direction == "output"}
    result = []
    for input_base, start_nets in sorted(inputs.items()):
        distance = {net: 0 for net in start_nets}
        queue = [(0, net) for net in start_nets]
        heapq.heapify(queue)
        while queue:
            depth, net = heapq.heappop(queue)
            if depth != distance.get(net):
                continue
            for target, weight in adjacency.get(net, []):
                candidate = depth + weight
                if candidate < distance.get(target, 1 << 30):
                    distance[target] = candidate
                    heapq.heappush(queue, (candidate, target))
        for output_base, target_nets in sorted(outputs.items()):
            depths = [distance[net] for net in target_nets if net in distance]
            if not depths:
                continue
            forward_reachable = set(distance)
            reverse_reachable = set(target_nets)
            reverse_queue = list(target_nets)
            while reverse_queue:
                target = reverse_queue.pop()
                for source in reverse.get(target, []):
                    if source not in reverse_reachable:
                        reverse_reachable.add(source)
                        reverse_queue.append(source)
            relevant = forward_reachable & reverse_reachable
            indegree = {net: 0 for net in relevant}
            for source in relevant:
                for target, _weight in adjacency.get(source, []):
                    if target in relevant:
                        indegree[target] += 1
            topo_queue = deque(sorted(
                net for net, degree in indegree.items() if degree == 0
            ))
            topological = []
            while topo_queue:
                source = topo_queue.popleft()
                topological.append(source)
                for target, _weight in adjacency.get(source, []):
                    if target not in indegree:
                        continue
                    indegree[target] -= 1
                    if indegree[target] == 0:
                        topo_queue.append(target)
            cycle_free = len(topological) == len(relevant)
            maximum_depths = []
            if cycle_free:
                longest = {net: 0 for net in start_nets if net in relevant}
                for source in topological:
                    if source not in longest:
                        continue
                    for target, weight in adjacency.get(source, []):
                        if target in relevant:
                            longest[target] = max(
                                longest.get(target, -(1 << 30)),
                                longest[source] + weight,
                            )
                maximum_depths = [
                    longest[net]
                    for net in target_nets
                    if net in distance and net in longest
                ]
            exact_depth = None
            if cycle_free and len(maximum_depths) == len(depths):
                paired = [
                    (distance[net], longest[net])
                    for net in target_nets
                    if net in distance and net in longest
                ]
                if paired and all(low == high for low, high in paired) \
                        and len({low for low, _high in paired}) == 1:
                    exact_depth = paired[0][0]
            record = {
                "input_group": input_base,
                "output_group": output_base,
                "reachable_output_bits": len(depths),
                "output_width": len(target_nets),
                "complete_output_coverage": len(depths) == len(target_nets),
                "minimum_register_crossings": min(depths),
                "maximum_of_minimum_register_crossings": max(depths),
                "uniform_minimum_register_crossings": len(set(depths)) == 1,
                "relevant_dataflow_cycle_free": cycle_free,
                "exact_uniform_register_crossings": exact_depth,
            }
            if maximum_depths:
                record["minimum_of_maximum_register_crossings"] = min(maximum_depths)
                record["maximum_register_crossings"] = max(maximum_depths)
            result.append(record)
    return result


def discover_hierarchical_operators(
    design,
    *,
    cell_type: str,
    max_candidates: int = 64,
) -> list[dict[str, str]]:
    """Discover exact logical cell types without relying on physical names."""
    if not isinstance(cell_type, str) or not cell_type or len(cell_type) > 256:
        raise HierarchicalContractError("cell_type is invalid")
    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) \
            or not 1 <= max_candidates <= 1024:
        raise HierarchicalContractError("max_candidates must be 1..1024")
    expected = cell_type.upper()
    queue = deque([design.getNetlist().getTopHierCellInst()])
    found = []
    visited = 0
    while queue:
        current = queue.popleft()
        visited += 1
        if visited > 5_000_000:
            raise HierarchicalContractError("logical hierarchy scan exceeded its bound")
        current_type = str(current.getCellType().getName())
        if current_type.upper() == expected:
            found.append({
                "instance": str(current.getFullHierarchicalInstName()),
                "cell_type": current_type,
            })
            if len(found) >= max_candidates:
                break
        for child in _as_list(current.getCellType().getCellInsts()):
            queue.append(current.getChild(child))
    return sorted(found, key=lambda item: item["instance"])


def recover_hierarchical_operator_contract(
    design,
    design_sha256: str,
    instance_name: str,
    *,
    max_region_cells: int = 100_000,
    max_consumer_cells: int = 100_000,
) -> dict[str, Any]:
    """Extract one operator and every output consumer to its first register."""
    if not isinstance(design_sha256, str) or _SHA256_RE.fullmatch(design_sha256) is None:
        raise HierarchicalContractError("design_sha256 must be a lowercase SHA-256 digest")
    if not isinstance(instance_name, str) or not instance_name or len(instance_name) > 2048:
        raise HierarchicalContractError("instance_name is invalid")
    for name, value in (
        ("max_region_cells", max_region_cells),
        ("max_consumer_cells", max_consumer_cells),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1_000_000:
            raise HierarchicalContractError(f"{name} must be 1..1000000")

    netlist = design.getNetlist()
    region = netlist.getHierCellInstFromName(instance_name)
    if region is None:
        raise HierarchicalContractError(f"operator instance is absent: {instance_name}")
    leaves = sorted(_as_list(netlist.getAllLeafDescendants(region)), key=str)
    if not leaves:
        raise HierarchicalContractError("operator has no primitive descendants")
    if len(leaves) > max_region_cells:
        raise HierarchicalContractError(
            f"operator has {len(leaves)} leaves, exceeding max_region_cells"
        )

    cell_records = [
        _leaf_record(design, netlist, leaf, instance_name) for leaf in leaves
    ]
    touched_nets = {}
    for leaf in leaves:
        for pin in _as_list(leaf.getHierPortInsts()):
            parent = _parent_net(netlist, pin)
            if parent is not None:
                touched_nets[_net_name(parent)] = parent

    boundary = []
    output_nets = {}
    connected_ports = {
        _pin_name(pin): pin for pin in _as_list(region.getHierPortInsts())
    }
    declared_ports = []
    for port in _as_list(region.getCellType().getPorts()):
        for index in _as_list(port.getBitBlastedIndices()):
            declared_ports.append((
                str(port.getPortInstNameFromPort(index)),
                _port_direction(port),
            ))
    for port_name, direction in sorted(declared_ports):
        pin = connected_ports.get(port_name)
        parent = _parent_net(netlist, pin) if pin is not None else None
        net_name = _net_name(parent) if parent is not None else None
        boundary.append({
            "port": port_name,
            "direction": direction,
            "canonical_net": net_name,
            "connected": parent is not None,
        })
        if parent is not None:
            touched_nets[net_name] = parent
            if direction == "output":
                output_nets[net_name] = parent

    region_net_records = [
        _canonical_net_record(net, instance_name)
        for _name, net in sorted(touched_nets.items())
    ]

    consumer_cells = {}
    endpoints = {}
    opaque_types = Counter()
    consumer_nets = {}
    pending = deque(output_nets.values())
    visited_nets = set()
    while pending:
        net = pending.popleft()
        name = _net_name(net)
        if name in visited_nets:
            continue
        visited_nets.add(name)
        consumer_nets[name] = net
        for sink in _as_list(net.getLeafHierPortInsts(False, True, True)):
            sink_cell_name = _cell_name(sink)
            if _inside(sink_cell_name, instance_name):
                continue
            sink_type = _cell_type(sink)
            sink_cell = sink.getFullHierarchicalInst()
            if _is_sequential(sink_type):
                endpoints.setdefault(
                    sink_cell_name,
                    _leaf_record(design, netlist, sink_cell, instance_name),
                )
                continue
            if not _is_combinational(sink_type):
                opaque_types[sink_type] += 1
                endpoints.setdefault(
                    sink_cell_name,
                    _leaf_record(design, netlist, sink_cell, instance_name),
                )
                continue
            if sink_cell_name in consumer_cells:
                continue
            if len(consumer_cells) >= max_consumer_cells:
                raise HierarchicalContractError(
                    "consumer traversal exceeded max_consumer_cells"
                )
            record = _leaf_record(design, netlist, sink_cell, instance_name)
            consumer_cells[sink_cell_name] = record
            for cell_pin in _as_list(sink_cell.getHierPortInsts()):
                parent = _parent_net(netlist, cell_pin)
                if parent is None:
                    continue
                consumer_nets[_net_name(parent)] = parent
                if bool(cell_pin.isOutput()):
                    pending.append(parent)

    consumer_net_records = [
        _canonical_net_record(net, instance_name)
        for _name, net in sorted(consumer_nets.items())
    ]
    primitive_counts = Counter(cell["type"] for cell in cell_records)
    consumer_counts = Counter(cell["type"] for cell in consumer_cells.values())
    endpoint_counts = Counter(cell["type"] for cell in endpoints.values())
    static_delay_contracts = [
        contract
        for cell in sorted(endpoints.values(), key=lambda item: item["name"])
        if (contract := recover_static_delay_endpoint_contract(cell)) is not None
    ]
    delay_cycles = Counter(
        contract["delay_cycles"]
        for contract in static_delay_contracts
        if contract.get("static_address") is True
    )
    latency = _latency_matrix(cell_records, boundary)
    normalized = {
        "operator_type": str(region.getCellType().getName()),
        "primitive_type_counts": dict(sorted(primitive_counts.items())),
        "functional_property_multiset_sha256": _digest(sorted(
            (
                cell["type"],
                json.dumps(
                    cell["functional_properties"],
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ),
            )
            for cell in cell_records
        )),
        "boundary_groups": _boundary_groups(boundary),
        "latency_matrix": latency,
        "consumer_type_counts": dict(sorted(consumer_counts.items())),
        "sequential_endpoint_type_counts": dict(sorted(endpoint_counts.items())),
        "static_delay_cycle_counts": {
            str(cycles): count for cycles, count in sorted(delay_cycles.items())
        },
        "region_topology_sha256": normalized_topology_sha256(
            cell_records, region_net_records
        ),
    }
    contract = {
        "status": "success",
        "kind": "hierarchical_operator_contract",
        "version": CONTRACT_VERSION,
        "authority": "exact_input_dcp_read_only_structural_contract",
        "design_sha256": design_sha256,
        "operator": {
            "instance": instance_name,
            "cell_type": str(region.getCellType().getName()),
            "leaf_count": len(cell_records),
            "primitive_type_counts": dict(sorted(primitive_counts.items())),
            "cells": cell_records,
            "nets": region_net_records,
        },
        "boundary": {
            "ports": boundary,
            "groups": _boundary_groups(boundary),
            "input_count": sum(item["direction"] == "input" for item in boundary),
            "output_count": sum(item["direction"] == "output" for item in boundary),
            "complete": True,
        },
        "consumer_region": {
            "combinational_cells": sorted(consumer_cells.values(), key=lambda item: item["name"]),
            "nets": consumer_net_records,
            "first_sequential_endpoints": sorted(endpoints.values(), key=lambda item: item["name"]),
            "combinational_type_counts": dict(sorted(consumer_counts.items())),
            "endpoint_type_counts": dict(sorted(endpoint_counts.items())),
            "static_delay_contracts": static_delay_contracts,
            "static_delay_cycle_counts": {
                str(cycles): count for cycles, count in sorted(delay_cycles.items())
            },
            "opaque_endpoint_type_counts": dict(sorted(opaque_types.items())),
            "complete_to_first_sequential_boundary": not opaque_types,
        },
        "sequential_depth_lower_bounds": latency,
        "normalized_contract": normalized,
        "proof_status": "structural_contract_extracted_semantics_unproved",
        "mutation_eligible": False,
        "limitations": [
            "sequential depths are minimum register-crossing lower bounds, not a cycle-equivalence proof",
            "primitive connectivity and LUT INIT values do not by themselves identify a unique high-level arithmetic expression",
            "consumer traversal stops at the first sequential or opaque primitive boundary",
        ],
    }
    digest_payload = dict(contract)
    contract["contract_sha256"] = _digest(digest_payload)
    return contract
