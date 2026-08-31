"""DCP-only registered observable-closure recovery.

The public result contains only aggregate evidence.  Exact dead cell names are
private mutation handoff data and must not be placed in an LLM prompt.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, deque
from typing import Any, Iterable


VERSION = 1
_REGISTERED_STATE = re.compile(r"^(?:FD|LD|SRL|DSP48|RAMB|URAM|FIFO)")


class RegisteredDeadStateError(ValueError):
    """Raised when exact observable-closure recovery cannot fail closed."""


def _as_list(value) -> list:
    return [] if value is None else list(value)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
    ).hexdigest()


def _backward_closure(
    seeds: Iterable[str], upstream: dict[str, set[str]]
) -> set[str]:
    """Pure graph kernel used by the RapidWright adapter and focused tests."""
    live: set[str] = set()
    queue = deque(str(item) for item in seeds)
    while queue:
        current = queue.popleft()
        if current in live:
            continue
        live.add(current)
        queue.extend(sorted(upstream.get(current, ())))
    return live


def _cell_type(cell) -> str:
    return str(cell.getType()).upper()


def _cell_name(cell) -> str:
    return str(cell.getName())


def _source_cell_name(pin) -> str:
    return str(pin.getFullHierarchicalInstName())


def recover_registered_dead_state_contract(design) -> dict[str, Any]:
    """Recover the complete physical primitive closure of observable outputs."""
    cells = _as_list(design.getCells())
    cell_by_name = {_cell_name(cell): cell for cell in cells}
    if not cell_by_name:
        raise RegisteredDeadStateError("checkpoint contains no physical cells")

    netlist = design.getNetlist()
    seed_names: set[str] = set()
    output_port_bit_count = 0
    for port in _as_list(netlist.getTopCell().getPorts()):
        if not bool(port.isOutput()):
            continue
        for net in _as_list(port.getInternalNets()):
            if net is None:
                continue
            output_port_bit_count += 1
            hierarchical = netlist.getHierNetFromName(str(net.getName()))
            parent = netlist.getParentNet(hierarchical)
            if parent is None:
                raise RegisteredDeadStateError(
                    f"canonical top-output net is missing for {net.getName()}"
                )
            for source in _as_list(
                parent.getLeafHierPortInsts(True, False, True)
            ):
                source_name = _source_cell_name(source)
                if source_name in cell_by_name:
                    seed_names.add(source_name)
    if not output_port_bit_count or not seed_names:
        raise RegisteredDeadStateError(
            "checkpoint top outputs resolve to no physical primitive drivers"
        )

    upstream: dict[str, set[str]] = {}
    preserved_unbound_names: set[str] = set()
    nonphysical_driver_names: set[str] = set()
    for name, cell in cell_by_name.items():
        hier_cell = cell.getEDIFHierCellInst()
        if hier_cell is None:
            # Routethrough/packing-only physical cells may have no EDIF owner.
            # Preserve them conservatively instead of classifying them dead.
            preserved_unbound_names.add(name)
            upstream[name] = set()
            continue
        dependencies: set[str] = set()
        for pin in _as_list(hier_cell.getHierPortInsts()):
            if not bool(pin.isInput()):
                continue
            hierarchical_net = pin.getHierarchicalNet()
            if hierarchical_net is None:
                # An unconnected primitive input has no observable dependency.
                continue
            parent = netlist.getParentNet(hierarchical_net)
            if parent is None:
                raise RegisteredDeadStateError(
                    f"canonical parent net is missing for {pin}"
                )
            for source in _as_list(
                parent.getLeafHierPortInsts(True, False, True)
            ):
                source_name = _source_cell_name(source)
                if source_name == name:
                    continue
                if source_name in cell_by_name:
                    dependencies.add(source_name)
                else:
                    # Top-level input ports are legal graph roots.  A leaf
                    # primitive driver missing from the physical design is not.
                    source_type = source.getCellType()
                    if source_type is not None and bool(source_type.isPrimitive()):
                        nonphysical_driver_names.add(source_name)
        upstream[name] = dependencies

    seeds = sorted(seed_names | preserved_unbound_names)
    observable = _backward_closure(seeds, upstream)
    dead_names = sorted(set(cell_by_name) - observable)
    dead_name_set = set(dead_names)
    dead_types = Counter(_cell_type(cell_by_name[name]) for name in dead_names)
    dead_registered_leaf_count = sum(
        count for kind, count in dead_types.items()
        if _REGISTERED_STATE.match(kind) and kind != "DSP48E2"
    )

    # RapidWright expands each DSP48E2 into physical DSP_* implementation
    # components.  Vivado mutation addresses the enclosing logical DSP48E2.
    # Infer the component population for every parent from the live DCP and
    # authorize parent deletion only when every component is outside the
    # observable closure.
    dsp_groups: dict[str, set[str]] = {}
    dsp_component_names: set[str] = set()
    for name, cell in cell_by_name.items():
        kind = _cell_type(cell)
        if kind == "DSP48E2":
            dsp_groups.setdefault(name, set()).add(name)
            dsp_component_names.add(name)
        elif kind.startswith("DSP_") and "/" in name:
            parent = name.rsplit("/", 1)[0]
            parent_inst = netlist.getHierCellInstFromName(parent)
            parent_type = (
                str(parent_inst.getCellType().getName()).upper()
                if parent_inst is not None else ""
            )
            if parent_type != "DSP48E2":
                raise RegisteredDeadStateError(
                    f"expanded DSP component has no DSP48E2 parent: {name}"
                )
            dsp_groups.setdefault(parent, set()).add(name)
            dsp_component_names.add(name)
    dead_dsp_parents = sorted(
        parent for parent, members in dsp_groups.items()
        if members and members.issubset(dead_name_set)
    )
    partial_dsp_parents = sorted(
        parent for parent, members in dsp_groups.items()
        if members & dead_name_set and not members.issubset(dead_name_set)
    )
    mutation_names = sorted(
        (dead_name_set - dsp_component_names) | set(dead_dsp_parents)
    )
    dead_dsp = len(dead_dsp_parents)
    total_dsp = len(dsp_groups)
    dead_registered = dead_registered_leaf_count + dead_dsp
    mutation_target_digest = _digest(mutation_names)
    summary = {
        "physical_cell_count": len(cell_by_name),
        "observable_physical_cell_count": len(observable),
        "dead_physical_cell_count": len(dead_names),
        "top_output_port_bit_count": output_port_bit_count,
        "top_output_driver_anchor_count": len(seed_names),
        "preserved_unbound_physical_cell_count": len(preserved_unbound_names),
        "nonphysical_primitive_driver_count": len(nonphysical_driver_names),
        "total_dsp_count": total_dsp,
        "dead_dsp_count": dead_dsp,
        "partial_dead_dsp_parent_count": len(partial_dsp_parents),
        "dead_registered_state_count": dead_registered,
        "mutation_target_cell_count": len(mutation_names),
        "mutation_target_digest": mutation_target_digest,
        "mutation_eligible": bool(dead_names and dead_registered),
        "dead_type_counts": dict(sorted(dead_types.items())),
    }
    if summary["observable_physical_cell_count"] + summary[
        "dead_physical_cell_count"
    ] != summary["physical_cell_count"]:
        raise RegisteredDeadStateError("observable/dead cell accounting diverged")
    contract = {
        "status": "success",
        "kind": "registered_observable_dead_state_contract",
        "version": VERSION,
        "read_only": True,
        "fresh_rediscovery": True,
        "operator_class": "REGISTERED_OBSERVABLE_DEAD_STATE_FAMILY",
        "summary": summary,
        "proof": {
            "output_anchored_transitive_primitive_fanin": True,
            "sequential_data_control_and_clock_inputs_traversed": True,
            "all_physical_primitives_partitioned": True,
            "expanded_dsp_components_grouped_by_live_parent": True,
            "partial_dsp_parents_preserved": True,
            "exact_dead_cell_names_are_private": True,
            "does_not_establish_positive_routed_qor": True,
        },
    }
    private_manifest = {
        "version": VERSION,
        "contract_digest": _digest(contract),
        "mutation_target_digest": mutation_target_digest,
        "dead_cell_names": mutation_names,
    }
    contract["contract_digest"] = private_manifest["contract_digest"]
    return {"public_contract": contract, "private_manifest": private_manifest}
