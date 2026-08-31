"""Fresh, read-only recovery of generic registered operator boundaries.

The detector is deliberately permissive: it enumerates live register families
without deciding whether a rewrite should run.  Recovery then binds one family
to the currently loaded DCP and records exact structural facts plus explicit
semantic proof gaps.  It never authorizes mutation.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any, Optional


VERSION = "sequential-operator-contract-v3"
MAX_PATHS = 64
MAX_PATH_ITEMS = 64
MAX_FAMILIES = 64
MAX_MEMBERS = 4096
MAX_TRACE_NODES_PER_MEMBER = 256
MAX_TRACE_NODES_TOTAL = 8192
_REGISTER_RE = re.compile(r"^FD(?:RE|SE|CE|PE)(?:_1)?$")
_INDEX_RE = re.compile(r"\[(\d+)\]")
_REGISTER_SCHEMAS = {
    "FDRE": {"data": "D", "output": "Q", "clock": "C", "enable": "CE", "state": "R"},
    "FDSE": {"data": "D", "output": "Q", "clock": "C", "enable": "CE", "state": "S"},
    "FDCE": {"data": "D", "output": "Q", "clock": "C", "enable": "CE", "state": "CLR"},
    "FDPE": {"data": "D", "output": "Q", "clock": "C", "enable": "CE", "state": "PRE"},
}


class SequentialContractError(ValueError):
    """The selected current-DCP boundary cannot be recovered safely."""


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


def _judgment_step(
    condition: str,
    *,
    state: str,
    scope: str,
    evidence: dict[str, Any],
    missing_evidence: list[str],
    next_discriminator: dict[str, Any],
) -> dict[str, Any]:
    if state not in {"supported", "contradicted", "unresolved"}:
        raise SequentialContractError(
            f"invalid sequential judgment state {state!r}"
        )
    return {
        "condition": condition,
        "state": state,
        "scope": scope,
        "evidence": evidence,
        "missing_evidence": missing_evidence,
        "next_discriminator": next_discriminator,
    }


def _operator_discriminator_options() -> list[dict[str, Any]]:
    """Expose competing semantic questions without dispatching one of them."""
    return [
        {
            "hypothesis": "consumer_boundary_contraction",
            "tool": "recover_consumer_driven_operator_contract",
            "question": (
                "does this internal registered family feed a narrower or otherwise "
                "specialized observable consumer boundary whose complete data, "
                "control, and latency contract explains the timing wall"
            ),
            "use_live_evidence": [
                "wall coverage versus endpoint ownership",
                "producer and downstream observable word dimensions",
                "consumer control protocol and pipeline latency",
            ],
        },
        {
            "hypothesis": "local_fixed_point_product",
            "tool": "screen_fixed_point_dsp_contract",
            "question": (
                "does the selected local registered D-cone itself close as a "
                "product, shift, wrap, or accumulation contract"
            ),
            "use_live_evidence": [
                "local product or shifted-term structure",
                "signedness and loss boundaries",
                "registered schedule support",
            ],
        },
        {
            "hypothesis": "generic_boolean_arithmetic_compressor",
            "candidate_target": "proved_arithmetic_compressor_v1",
            "question": (
                "is the useful boundary a bounded exact Boolean arithmetic cone "
                "without sufficient evidence for a higher-level product contract"
            ),
            "use_live_evidence": [
                "bounded cone completeness",
                "exact source-function recoverability",
                "expected wall overlap of the emitted compressor",
            ],
        },
    ]


def _luna_discriminator_choice(question: str) -> dict[str, Any]:
    return {
        "decision_owner": "luna",
        "mode": "compare_live_semantic_hypotheses",
        "question": question,
        "options": _operator_discriminator_options(),
        "selection_rule": (
            "cite current live wall, boundary, data, control, and latency evidence; "
            "no option is a hard trigger or default"
        ),
    }


def _safe_call(obj, method: str, default=None):
    try:
        value = getattr(obj, method)()
        return default if value is None else value
    except Exception:
        return default


def _base_register_type(cell_type: str) -> str:
    return str(cell_type).upper().removesuffix("_1")


def _normalized_replica_stem(name: str) -> str:
    value = re.sub(r"\[\s*\d+\s*\]", "[]", str(name))
    value = re.sub(r"(?<![A-Za-z])\d+(?![A-Za-z])", "#", value)
    return re.sub(r"(?<=[_.$/])\d+(?=[_.$/]|$)", "#", value)


def _family_key(name: str) -> tuple[str, str]:
    matches = list(_INDEX_RE.finditer(str(name)))
    if matches:
        match = matches[-1]
        stem = str(name)[:match.start()] + "[]" + str(name)[match.end():]
        return "indexed_word", stem
    return "replica", _normalized_replica_stem(name)


def _bit_mapping(names: list[str], grouping: str) -> dict[str, Any]:
    if grouping != "indexed_word":
        return {
            "status": "replica_unordered",
            "member_count": len(names),
            "indices": [],
        }
    indices = []
    for name in names:
        matches = list(_INDEX_RE.finditer(name))
        if not matches:
            return {"status": "mixed_or_missing", "member_count": len(names), "indices": []}
        indices.append(int(matches[-1].group(1)))
    ordered = sorted(indices)
    unique = len(set(ordered)) == len(ordered)
    contiguous = bool(ordered) and ordered == list(range(ordered[0], ordered[-1] + 1))
    status = "explicit_unique_contiguous" if unique and contiguous else (
        "explicit_unique_sparse" if unique else "duplicate_or_ambiguous"
    )
    return {
        "status": status,
        "member_count": len(names),
        "indices": ordered,
        "min_index": ordered[0] if ordered else None,
        "max_index": ordered[-1] if ordered else None,
    }


def _physical_fact(cell) -> dict[str, Any]:
    placed = bool(_safe_call(cell, "isPlaced", False))
    site = _safe_call(cell, "getSite") if placed else None
    tile = _safe_call(site, "getTile") if site is not None else None
    region = _safe_call(tile, "getClockRegion") if tile is not None else None
    return {
        "placed": placed,
        "site": str(_safe_call(site, "getName", "")) if site is not None else "",
        "bel": str(_safe_call(cell, "getBELName", "")),
        "tile_x": _safe_call(tile, "getTileXCoordinate"),
        "tile_y": _safe_call(tile, "getTileYCoordinate"),
        "clock_region": str(region) if region is not None else "",
    }


def _resolve_path_cell(design, token: str):
    candidate = str(token)
    while candidate:
        try:
            cell = design.getCell(candidate)
        except Exception:
            cell = None
        if cell is not None:
            return cell
        if "/" not in candidate:
            return None
        candidate = candidate.rsplit("/", 1)[0]
    return None


def _validate_paths(critical_paths_data) -> list[list[str]]:
    if not isinstance(critical_paths_data, list) or not critical_paths_data:
        raise SequentialContractError("critical_paths_data must be a nonempty array")
    if len(critical_paths_data) > MAX_PATHS:
        raise SequentialContractError(f"critical_paths_data exceeds {MAX_PATHS} paths")
    result = []
    for index, path in enumerate(critical_paths_data):
        if not isinstance(path, list) or not path or len(path) > MAX_PATH_ITEMS:
            raise SequentialContractError(
                f"critical_paths_data[{index}] must contain 1..{MAX_PATH_ITEMS} items"
            )
        if any(not isinstance(item, str) or not item or len(item) > 1024 for item in path):
            raise SequentialContractError(f"critical_paths_data[{index}] has an invalid item")
        result.append(list(path))
    return result


def _path_wall_coverage(
    member_names: list[str], resolved_paths: list[list[str]]
) -> dict[str, Any]:
    """Describe where one family sits in the ordered live timing wall.

    ``resolved_paths`` preserves the worst-first order supplied by Vivado.  The
    result deliberately contains ranks and counts only: object names remain in
    the trusted handoff while Luna receives enough evidence to distinguish an
    easy-to-prove family from one that actually owns the worst endpoints.
    """
    members = set(member_names)
    covered = [
        index for index, path in enumerate(resolved_paths, start=1)
        if members.intersection(path)
    ]
    endpoint_covered = [
        index for index, path in enumerate(resolved_paths, start=1)
        if path and path[-1] in members
    ]
    path_count = len(resolved_paths)

    def count_within(limit: int) -> int:
        return sum(index <= min(limit, path_count) for index in covered)

    return {
        "ordered_path_count": path_count,
        "covered_path_count": len(covered),
        "covered_path_ranks": covered,
        "earliest_covered_rank": covered[0] if covered else None,
        "covers_worst_path": 1 in covered,
        "endpoint_path_count": len(endpoint_covered),
        "endpoint_path_ranks": endpoint_covered,
        "covers_worst_endpoint": 1 in endpoint_covered,
        "top5_covered_count": count_within(5),
        "top10_covered_count": count_within(10),
        "all_path_coverage_ratio": (
            round(len(covered) / path_count, 4) if path_count else 0.0
        ),
        "rank_semantics": "rank_1_is_worst_in_current_live_path_sample",
    }


def _family_evidence_scope(coverage: dict[str, Any]) -> str:
    """Classify observed path role without inferring operator ownership."""
    covered = int(coverage.get("covered_path_count") or 0)
    endpoint = int(coverage.get("endpoint_path_count") or 0)
    if covered <= 0:
        return "unresolved_registered_family"
    if endpoint <= 0:
        return "non_endpoint_internal_support_family"
    if endpoint == covered:
        return "endpoint_owner_family"
    return "mixed_internal_and_endpoint_family"


def mine_sequential_write_families(
    design,
    *,
    critical_paths_data: list[list[str]],
    max_families: int = 32,
    design_sha256: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Enumerate live sequential families; no family is an action trigger."""
    paths = _validate_paths(critical_paths_data)
    if isinstance(max_families, bool) or not isinstance(max_families, int) or not 1 <= max_families <= MAX_FAMILIES:
        raise SequentialContractError(f"max_families must be an integer in 1..{MAX_FAMILIES}")

    groups: dict[tuple[str, str, str], list[Any]] = {}
    for cell in _as_list(design.getCells()):
        cell_type = str(cell.getType()).upper()
        if _REGISTER_RE.fullmatch(cell_type) is None:
            continue
        name = str(cell.getName())
        grouping, stem = _family_key(name)
        groups.setdefault((grouping, stem, _base_register_type(cell_type)), []).append(cell)

    resolved_paths: list[list[str]] = []
    for path in paths:
        names = []
        for token in path:
            cell = _resolve_path_cell(design, token)
            if cell is None:
                continue
            name = str(cell.getName())
            if not names or names[-1] != name:
                names.append(name)
        resolved_paths.append(names)
    critical_names = {name for path in resolved_paths for name in path}

    families = []
    for (grouping, stem, cell_type), members in groups.items():
        members = sorted(members, key=lambda item: str(item.getName()))
        member_names = [str(item.getName()) for item in members]
        critical_members = sorted(set(member_names) & critical_names)
        if not critical_members:
            continue
        if len(member_names) > MAX_MEMBERS:
            continue
        coverage = _path_wall_coverage(member_names, resolved_paths)
        path_count = int(coverage["covered_path_count"])
        physical = [_physical_fact(item) for item in members]
        xs = sorted({int(item["tile_x"]) for item in physical if isinstance(item.get("tile_x"), (int, float))})
        regions = sorted({item["clock_region"] for item in physical if item.get("clock_region")})
        mapping = _bit_mapping(member_names, grouping)
        identity = {
            "design_sha256": design_sha256,
            "grouping": grouping,
            "stem": stem,
            "cell_type": cell_type,
            "members": member_names,
        }
        families.append({
            "candidate_id": "sequential:" + _digest(identity)[:20],
            "kind": "sequential_write_family_hypothesis",
            "evidence_scope": _family_evidence_scope(coverage),
            "grouping": grouping,
            "cell_type": cell_type,
            "member_count": len(member_names),
            "critical_member_count": len(critical_members),
            "critical_path_count": path_count,
            "critical_path_coverage": coverage,
            "bit_mapping": mapping,
            "physical_footprint": {
                "placed_member_count": sum(bool(item["placed"]) for item in physical),
                "tile_x_min": xs[0] if xs else None,
                "tile_x_max": xs[-1] if xs else None,
                "tile_x_span": xs[-1] - xs[0] if len(xs) >= 2 else 0,
                "clock_region_count": len(regions),
            },
            # Full names remain inside the trusted handoff.  Public summaries
            # intentionally omit these fields.
            "members": member_names,
            "critical_members": critical_members,
            "proof_status": "hypothesis_only",
            "mutation_eligible": False,
        })
    families.sort(key=lambda item: (
        -item["critical_path_count"],
        -item["critical_member_count"],
        -item["member_count"],
        item["candidate_id"],
    ))
    return families[:max_families]


def _parent_net(netlist, pin):
    hierarchical = pin.getHierarchicalNet()
    if hierarchical is None:
        raise SequentialContractError(f"pin {pin} has no hierarchical net")
    parent = netlist.getParentNet(hierarchical)
    if parent is None:
        raise SequentialContractError(f"pin {pin} has no canonical parent net")
    return parent


def _net_name(net) -> str:
    return str(net.getHierarchicalNetName())


def _pin_cell_name(pin) -> str:
    value = _safe_call(pin, "getFullHierarchicalInstName", "")
    return str(value or "")


def _pin_cell_type(pin) -> str:
    cell_type = _safe_call(pin, "getCellType")
    return str(_safe_call(cell_type, "getName", "")).upper() if cell_type is not None else "TOP_PORT"


def _pin_name(pin) -> str:
    port_inst = _safe_call(pin, "getPortInst")
    return str(_safe_call(port_inst, "getName", ""))


def _properties(pin) -> dict[str, str]:
    port_inst = _safe_call(pin, "getPortInst")
    cell_inst = _safe_call(port_inst, "getCellInst") if port_inst is not None else None
    values = _safe_call(cell_inst, "getPropertiesMap", {}) if cell_inst is not None else {}
    result = {}
    try:
        for entry in values.entrySet():
            raw = entry.getValue()
            result[str(entry.getKey())] = str(raw.getValue())
    except AttributeError:
        try:
            for key, value in dict(values).items():
                raw = value.getValue() if hasattr(value, "getValue") else value
                result[str(key)] = str(raw)
        except Exception:
            pass
    return dict(sorted(result.items()))


def _pin_fact(pin) -> dict[str, str]:
    return {
        "cell": _pin_cell_name(pin),
        "cell_type": _pin_cell_type(pin),
        "pin": _pin_name(pin),
    }


def _control_summary(member_facts: list[dict[str, Any]], role: str) -> dict[str, Any]:
    values = [item.get("controls", {}).get(role) for item in member_facts]
    missing = sum(not isinstance(item, dict) for item in values)
    nets = sorted({str(item.get("net")) for item in values if isinstance(item, dict)})
    inversions = sorted({str(item.get("inversion")) for item in values if isinstance(item, dict)})
    if missing:
        status = "missing"
    elif len(nets) == 1 and len(inversions) == 1:
        status = "exact_same_net_and_polarity"
    else:
        status = "mixed"
    return {
        "status": status,
        "member_count": len(values),
        "missing_member_count": missing,
        "distinct_net_count": len(nets),
        "distinct_polarity_count": len(inversions),
        "representative_net": nets[0] if len(nets) == 1 else None,
    }


def _hierarchy(name: str) -> str:
    return str(name).rsplit("/", 1)[0] if "/" in str(name) else "<top>"


def _is_trace_boundary(cell_type: str) -> bool:
    value = str(cell_type).upper()
    return (
        value == "TOP_PORT"
        or _REGISTER_RE.fullmatch(value) is not None
        or value.startswith(("DSP", "RAMB", "URAM"))
    )


def _trace_one_data_cone(netlist, drivers: list[dict[str, str]]) -> dict[str, Any]:
    queue = [
        (item.get("cell", ""), item.get("cell_type", ""), 0)
        for item in drivers
    ]
    visited = set()
    type_counts: Counter[str] = Counter()
    boundary_types: Counter[str] = Counter()
    max_depth = 0
    truncated = False
    while queue:
        cell_name, cell_type, depth = queue.pop(0)
        key = (str(cell_name), str(cell_type))
        if key in visited:
            continue
        if len(visited) >= MAX_TRACE_NODES_PER_MEMBER:
            truncated = True
            break
        visited.add(key)
        normalized_type = str(cell_type or "UNKNOWN").upper()
        type_counts[normalized_type] += 1
        max_depth = max(max_depth, depth)
        if _is_trace_boundary(normalized_type) or not cell_name:
            boundary_types[normalized_type] += 1
            continue
        cell = None
        if callable(getattr(netlist, "getHierCellInstFromName", None)):
            try:
                cell = netlist.getHierCellInstFromName(cell_name)
            except Exception:
                cell = None
        if cell is None:
            boundary_types[normalized_type] += 1
            continue
        inputs = []
        for pin in _as_list(_safe_call(cell, "getHierPortInsts", [])):
            try:
                if bool(pin.isInput()):
                    inputs.append(pin)
            except Exception:
                continue
        if not inputs:
            boundary_types[normalized_type] += 1
            continue
        for pin in inputs:
            try:
                parent = _parent_net(netlist, pin)
                sources = _as_list(parent.getLeafHierPortInsts(True, False, True))
            except Exception:
                sources = []
            if not sources:
                boundary_types["UNRESOLVED_SOURCE"] += 1
            for source in sources:
                queue.append((_pin_cell_name(source), _pin_cell_type(source), depth + 1))
    signature = {
        "type_counts": dict(sorted(type_counts.items())),
        "boundary_type_counts": dict(sorted(boundary_types.items())),
        "max_depth": max_depth,
        "truncated": truncated,
    }
    return {
        **signature,
        "node_count": len(visited),
        "signature_sha256": _digest(signature),
    }


def _trace_family_data_cones(netlist, member_facts: list[dict[str, Any]]) -> dict[str, Any]:
    traces = []
    total_nodes = 0
    family_truncated = False
    for item in member_facts:
        if total_nodes >= MAX_TRACE_NODES_TOTAL:
            family_truncated = True
            break
        drivers = list(item.get("data", {}).get("drivers") or [])
        trace = _trace_one_data_cone(netlist, drivers)
        traces.append({"ordinal": item["ordinal"], **trace})
        total_nodes += int(trace.get("node_count") or 0)
    signature_counts = Counter(
        item["signature_sha256"] for item in traces if item.get("signature_sha256")
    )
    type_counts: Counter[str] = Counter()
    boundary_types: Counter[str] = Counter()
    for trace in traces:
        type_counts.update(trace.get("type_counts") or {})
        boundary_types.update(trace.get("boundary_type_counts") or {})
    return {
        "status": "partial" if family_truncated or any(item["truncated"] for item in traces) else "complete",
        "traced_member_count": len(traces),
        "untraced_member_count": len(member_facts) - len(traces),
        "total_node_count": total_nodes,
        "max_depth": max((item["max_depth"] for item in traces), default=0),
        "primitive_type_counts": dict(sorted(type_counts.items())),
        "boundary_type_counts": dict(sorted(boundary_types.items())),
        "distinct_cone_signature_count": len(signature_counts),
        "largest_repeated_signature_member_count": max(signature_counts.values(), default=0),
        "member_traces": traces,
    }


def _early_semantic_hypotheses(
    *,
    bit_mapping: dict[str, Any],
    driver_types: Counter[str],
    data_cone_traceback: dict[str, Any],
) -> dict[str, Any]:
    """Expose word-level arithmetic directions before expensive closure proof.

    The result is intentionally a hypothesis, not mutation authority.  It uses
    only anonymous live structure: the registered word boundary, its endpoint
    arithmetic carrier, and the carry/LUT structure behind that carrier.
    """
    member_count = int(bit_mapping.get("member_count") or 0)
    contiguous_word = bit_mapping.get("status") == "explicit_unique_contiguous"
    endpoint_carry_count = int(driver_types.get("CARRY8", 0))
    endpoint_driver_count = sum(int(value) for value in driver_types.values())
    primitive_counts = data_cone_traceback.get("primitive_type_counts") or {}
    traced_carry_count = int(primitive_counts.get("CARRY8") or 0)
    traced_lut_count = sum(
        int(primitive_counts.get(f"LUT{width}") or 0) for width in range(1, 7)
    )
    nested_carry_count = max(0, traced_carry_count - endpoint_carry_count)
    carry_endpoint_word = bool(
        contiguous_word
        and member_count
        and endpoint_driver_count == member_count
        and endpoint_carry_count == member_count
    )
    multi_level_carry_lut = bool(nested_carry_count and traced_lut_count)

    return {
        "version": 1,
        "authority": "early_structural_hypothesis_not_equivalence_proof",
        "word_boundary": {
            "registered_contiguous_word": contiguous_word,
            "width": member_count,
            "endpoint_driver_type_counts": dict(sorted(driver_types.items())),
        },
        "observed_expression_shape": {
            "carry_driven_registered_word": carry_endpoint_word,
            "multi_level_carry_lut_cone": multi_level_carry_lut,
            "endpoint_carry_count": endpoint_carry_count,
            "nested_carry_count": nested_carry_count,
            "lut_count": traced_lut_count,
            "trace_depth": data_cone_traceback.get("max_depth"),
            "largest_repeated_cone_member_count": data_cone_traceback.get(
                "largest_repeated_signature_member_count"
            ),
        },
        "operator_classes": [
            {
                "operator_class": "registered_add_sub_or_accumulate",
                "state": "structurally_supported" if carry_endpoint_word else "unresolved",
                "mechanism": (
                    "a complete registered word is driven bitwise by carry-chain "
                    "outputs, the native carrier for add/sub/accumulate"
                ),
            },
            {
                "operator_class": "shift_add_constant_multiply_or_mac",
                "state": (
                    "plausible_requires_word_level_proof"
                    if carry_endpoint_word and multi_level_carry_lut
                    else "unresolved"
                ),
                "mechanism": (
                    "additional carry/LUT levels behind the endpoint word can "
                    "represent shifted partial sums, a fixed product, or a MAC"
                ),
            },
        ],
        "not_yet_recovered": [
            "exact_add_vs_subtract_polarity",
            "constant_coefficients_and_signedness",
            "shift_truncation_rounding_saturation_or_wrap",
            "accumulation_order",
            "clock_enable_state_schedule",
        ],
        "discriminator_options": _operator_discriminator_options(),
        "next_discriminator": _luna_discriminator_choice(
            "decide whether the selected endpoint family is a complete local "
            "arithmetic operator or an internal producer whose meaning is only "
            "closed at a downstream consumer boundary"
        ),
    }


def _classifications(
    *,
    structural_complete: bool,
    mapping_status: str,
    controls_common: bool,
    unique_drivers: bool,
    driver_types: Counter,
    consumer_types: Counter,
    member_hierarchies: set[str],
    driver_hierarchies: set[str],
    consumer_hierarchies: set[str],
    tile_x_span: int,
) -> list[dict[str, Any]]:
    indexed = mapping_status == "explicit_unique_contiguous"
    dsp_feedback = bool(driver_types) and bool(consumer_types) and all(
        value.startswith("DSP") for value in driver_types
    ) and any(value.startswith("DSP") for value in consumer_types)
    hierarchy_crossing = bool(
        (driver_hierarchies - member_hierarchies)
        or (consumer_hierarchies - member_hierarchies)
    )
    return [
        {
            "kind": "complete_write_boundary",
            "evidence_state": "structurally_supported" if structural_complete and indexed else "probe_incomplete",
            "evidence": {
                "bit_mapping_exact": indexed,
                "controls_common": controls_common,
                "unique_data_driver_per_member": unique_drivers,
            },
            "missing_proof": [
                "registered_observable_closure",
                "transaction_latency_and_protocol",
                "loss_signedness_and_arithmetic_identity",
            ],
        },
        {
            "kind": "shadow_data_boundary",
            "evidence_state": "proof_candidate" if structural_complete and indexed and hierarchy_crossing else "weak_or_absent",
            "evidence": {
                "complete_registered_write": structural_complete and indexed,
                "control_shell_reusable": controls_common,
                "hierarchy_crossing_observed": hierarchy_crossing,
            },
            "missing_proof": [
                "complete_observable_data_outputs",
                "retained_control_fifo_and_backpressure_contract",
                "cycle_lockstep_equivalence",
            ],
        },
        {
            "kind": "recurrence_boundary",
            "evidence_state": "proof_candidate" if structural_complete and dsp_feedback else "weak_or_absent",
            "evidence": {
                "registered_feedback_carrier": dsp_feedback,
                "common_controls": controls_common,
                "ordered_state_bits": indexed,
            },
            "missing_proof": [
                "exact_recurrence_edge_and_period",
                "modulo_or_loss_boundary",
                "bubble_reset_and_reinjection_schedule",
            ],
        },
        {
            "kind": "relay_bank_boundary",
            "evidence_state": "proof_candidate" if structural_complete and indexed and tile_x_span > 0 else "weak_or_absent",
            "evidence": {
                "ordered_bank": indexed,
                "common_controls": controls_common,
                "physical_tile_x_span": tile_x_span,
            },
            "missing_proof": [
                "enabled_transaction_delay",
                "no_intermediate_taps_or_side_consumers",
                "clock_footprint_and_owner_preserving_mapping",
            ],
        },
    ]


def _operator_scope_census(
    selected: dict[str, Any], families: list[dict[str, Any]]
) -> dict[str, Any]:
    """Compare one registered family with anonymous live wall peers."""
    coverage = selected.get("critical_path_coverage") or {}
    selected_ranks = {
        int(value) for value in coverage.get("covered_path_ranks") or []
    }
    path_count = int(coverage.get("ordered_path_count") or 0)
    peers = []
    for family in families:
        if family.get("candidate_id") == selected.get("candidate_id"):
            continue
        peer_coverage = family.get("critical_path_coverage") or {}
        peer_ranks = {
            int(value) for value in peer_coverage.get("covered_path_ranks") or []
        }
        additional = sorted(peer_ranks - selected_ranks)
        overlap = sorted(peer_ranks & selected_ranks)
        peers.append({
            "candidate_id": family.get("candidate_id"),
            "evidence_scope": _family_evidence_scope(peer_coverage),
            "covered_path_count": int(
                peer_coverage.get("covered_path_count") or 0
            ),
            "additional_path_count": len(additional),
            "additional_path_ranks": additional,
            "overlap_path_count": len(overlap),
            "covers_worst_path": bool(
                peer_coverage.get("covers_worst_path")
            ),
        })
    peers.sort(key=lambda item: (
        -item["additional_path_count"],
        -item["covered_path_count"],
        str(item.get("candidate_id") or ""),
    ))
    uncovered = sorted(set(range(1, path_count + 1)) - selected_ranks)
    return {
        "selected_scope": _family_evidence_scope(coverage),
        "complete_generating_operator_contract_recovered": False,
        "ordered_path_count": path_count,
        "selected_covered_path_count": len(selected_ranks),
        "selected_covered_path_ranks": sorted(selected_ranks),
        "uncovered_path_count": len(uncovered),
        "uncovered_path_ranks": uncovered,
        "peer_family_count": len(peers),
        "peer_families": peers,
        "interpretation": (
            "internal path support, endpoint ownership, and even full sampled-"
            "wall coverage are distinct evidence roles; none alone establishes "
            "the complete generating operator or replica closure"
        ),
    }


def _generating_operator_closure(
    selected: dict[str, Any], families: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build an identity-free multi-family closure ledger for the live wall.

    Endpoint-owner families with identical path-rank populations often denote
    different registered cuts of one repeated lane.  Overlap is useful
    evidence, but neither identical coverage nor a union covering the sampled
    wall proves common arithmetic, schedule, loss, or observable boundaries.
    The ledger therefore joins every live family mechanically and reports
    explicit open obligations instead of promoting one family to a complete
    operator.
    """
    path_count = int(
        (selected.get("critical_path_coverage") or {}).get(
            "ordered_path_count"
        ) or 0
    )
    grouped: dict[tuple[int, ...], list[dict[str, Any]]] = {}
    union_ranks: set[int] = set()
    for family in families:
        coverage = family.get("critical_path_coverage") or {}
        ranks = tuple(sorted({
            int(value) for value in coverage.get("covered_path_ranks") or []
        }))
        if not ranks:
            continue
        union_ranks.update(ranks)
        grouped.setdefault(ranks, []).append(family)

    wall_groups = []
    selected_group_ref = None
    selected_id = selected.get("candidate_id")
    for ordinal, (ranks, members) in enumerate(
        sorted(grouped.items(), key=lambda item: (item[0][0], item[0])),
        start=1,
    ):
        group_ref = f"closure_group_{ordinal}"
        member_ids = [item.get("candidate_id") for item in members]
        endpoint_ids = [
            item.get("candidate_id")
            for item in members
            if int(
                (item.get("critical_path_coverage") or {}).get(
                    "endpoint_path_count"
                ) or 0
            )
        ]
        if selected_id in member_ids:
            selected_group_ref = group_ref
        wall_groups.append({
            "group_ref": group_ref,
            "covered_path_ranks": list(ranks),
            "covered_path_count": len(ranks),
            "family_count": len(members),
            "family_candidate_ids": member_ids,
            "endpoint_owner_candidate_ids": endpoint_ids,
            "non_endpoint_support_candidate_ids": [
                value for value in member_ids if value not in endpoint_ids
            ],
            "evidence_relation": "identical_live_path_rank_population",
        })

    relations = []
    for left_index, left in enumerate(wall_groups):
        left_ranks = set(left["covered_path_ranks"])
        for right in wall_groups[left_index + 1:]:
            overlap = sorted(left_ranks & set(right["covered_path_ranks"]))
            if overlap:
                relations.append({
                    "left_group_ref": left["group_ref"],
                    "right_group_ref": right["group_ref"],
                    "relation": "live_path_overlap",
                    "overlap_path_ranks": overlap,
                })

    uncovered = sorted(set(range(1, path_count + 1)) - union_ranks)
    return {
        "version": 1,
        "status": "partial",
        "authority": "read_only_multi_family_closure_evidence",
        "selected_family_scope": _family_evidence_scope(
            selected.get("critical_path_coverage") or {}
        ),
        "selected_group_ref": selected_group_ref,
        "complete_generating_operator_contract_recovered": False,
        "family_count": len(families),
        "wall_group_count": len(wall_groups),
        "wall_groups": wall_groups,
        "cross_group_relations": relations,
        "ordered_path_count": path_count,
        "all_family_covered_path_count": len(union_ranks),
        "all_family_covered_path_ranks": sorted(union_ranks),
        "uncovered_path_count": len(uncovered),
        "uncovered_path_ranks": uncovered,
        "closed_obligations": [
            "live_registered_family_role_census",
            "identical_and_overlapping_path_population_relations",
        ],
        "open_obligations": [
            "motif_occurrence_to_every_registered_family_mapping",
            "common_launch_and_input_word_closure",
            "replica_template_and_replica_count",
            "width_signedness_term_weight_and_each_loss_boundary",
            "clock_ce_reset_init_latency_ii_bubble_and_protocol",
            "complete_observable_and_constraint_owner_shell",
            "source_vs_emitted_topology_and_surviving_wall",
        ],
        "interpretation": (
            "the complete live family union is a recovery scope, not mutation "
            "authority; complete status requires every semantic, schedule, "
            "observable, and topology obligation to close"
        ),
    }


def _operator_judgment_chain(
    *,
    family: dict[str, Any],
    scope_census: dict[str, Any],
    bit_mapping: dict[str, Any],
    hierarchy_boundary: dict[str, int],
    recovery_issues: list[str],
    controls_common: bool,
    structural_complete: bool,
    data_cone_traceback: dict[str, Any],
    classifications: list[dict[str, Any]],
) -> dict[str, Any]:
    coverage = family.get("critical_path_coverage") or {}
    path_count = int(coverage.get("ordered_path_count") or 0)
    covered = int(coverage.get("covered_path_count") or 0)
    scope = (
        "whole_wall" if path_count and covered == path_count
        else "partial_wall" if covered else "unresolved"
    )
    return {
        "version": 2,
        "authority": "read_only_evidence_not_action_dispatch",
        "selected_family_scope": _family_evidence_scope(coverage),
        "effect_chain": [
            "evidence_normalized",
            "pattern_present",
            "complete_generating_operator_recovered",
            "semantic_and_schedule_contract_closed",
            "hierarchy_and_constraint_owner_boundary_preserved",
            "candidate_mapping_realized",
            "current_wall_materially_covered",
            "counterfactual_next_wall_attributed",
            "trusted_signoff_measured",
        ],
        "threshold_semantics": (
            "coverage, node count, depth, and footprint are continuous evidence; "
            "Luna compares them without a fixed dispatch threshold"
        ),
        "steps": [
            _judgment_step(
                "evidence_normalization",
                state="unresolved",
                scope="current_checkpoint_and_registered_boundary",
                evidence={
                    "ordered_path_count": path_count,
                    "registered_members_resolved": int(
                        bit_mapping.get("member_count") or 0
                    ),
                    "recovery_issue_count": len(recovery_issues),
                    "route_stage_status": "not_observed_by_structure_only_probe",
                    "timing_band_censoring_status": "not_observed_without_slack_vector",
                },
                missing_evidence=[
                    "trusted_fully_routed_status",
                    "complete_slack_vector_and_top_n_censoring_check",
                    "registered_observation_tail_normalization",
                    "transformed_timing_leaf_to_logical_parent_and_site_collapse",
                ],
                next_discriminator={
                    "source": "latest live timing and route reports",
                    "question": (
                        "normalize route stage, timing sample, and physical owners "
                        "before using path count as semantic replica coverage"
                    ),
                },
            ),
            _judgment_step(
                "dense_repeated_timing_wall",
                state="supported" if covered else "unresolved",
                scope=scope,
                evidence={
                    "selected_registered_family_coverage": coverage,
                    "observed_family_role": _family_evidence_scope(coverage),
                },
                missing_evidence=[
                    "wall_motif_to_complete_operator_replica_mapping"
                ],
                next_discriminator={
                    "source": "operator_mining.operator_judgment_chain",
                    "question": "compare endpoint ownership with the leading wall motif",
                },
            ),
            _judgment_step(
                "complete_generating_operator_scope",
                state="unresolved",
                scope=scope,
                evidence={
                    "scope_census": scope_census,
                    "observed_family_role_is_not_generating_operator_proof": True,
                },
                missing_evidence=[
                    "replica_template_and_replica_count",
                    "motif_to_all_registered_owner_families",
                    "complete_observable_operator_boundary",
                ],
                next_discriminator={
                    "tool": "operator_mining",
                    "question": (
                        "inspect peer registered families and widen only by "
                        "mechanically supported wall/replica coverage"
                    ),
                },
            ),
            _judgment_step(
                "arithmetic_loss_contract",
                state="unresolved",
                scope="selected_registered_family",
                evidence={
                    "data_cone_traceback_status": data_cone_traceback.get("status"),
                    "primitive_type_counts": data_cone_traceback.get(
                        "primitive_type_counts"
                    ) or {},
                    "distinct_cone_signature_count": data_cone_traceback.get(
                        "distinct_cone_signature_count"
                    ),
                },
                missing_evidence=[
                    "word_width_signedness_and_full_term_mapping",
                    "shift_truncation_rounding_saturation_or_wrap",
                    "accumulation_order_and_each_loss_boundary",
                    "formal_source_equivalence",
                ],
                next_discriminator=_luna_discriminator_choice(
                    "compare complete consumer-boundary semantics with a local "
                    "fixed-point interpretation and a generic exact compressor"
                ),
            ),
            _judgment_step(
                "schedule_control_observable_contract",
                state="unresolved",
                scope="registered_endpoint_boundary",
                evidence={
                    "common_clock_ce_reset_shell": controls_common,
                    "structural_boundary_recovered": structural_complete,
                    "classifications": classifications,
                },
                missing_evidence=[
                    "registered_observable_closure",
                    "latency_initiation_interval_bubbles_and_protocol",
                    "state_priority_and_full_schedule_equivalence",
                ],
                next_discriminator={
                    "kind": "semantic_obligation",
                    "question": (
                        "recover the complete transaction boundary before calling "
                        "the selected family a generating operator"
                    ),
                },
            ),
            _judgment_step(
                "hierarchy_constraint_owner_boundary",
                state="unresolved",
                scope="local_registered_endpoint_boundary",
                evidence={
                    "hierarchy_boundary": hierarchy_boundary,
                    "structural_boundary_recovered": structural_complete,
                    "local_boundary_support_is_not_whole_operator_authority": True,
                },
                missing_evidence=[
                    "complete_real_edif_or_hierarchy_owner_shell",
                    "constraint_owned_cell_families",
                    "launch_and_endpoint_route_ownership",
                    "pre_action_clock_sink_and_sequential_replica_sets",
                ],
                next_discriminator={
                    "kind": "semantic_obligation",
                    "question": (
                        "preserve every real hierarchy and constraint owner named "
                        "by the complete generating-operator transaction"
                    ),
                },
            ),
            _judgment_step(
                "emitted_topology_payoff",
                state="unresolved",
                scope="candidate_backend",
                evidence={
                    "source_traceback_status": data_cone_traceback.get("status"),
                    "source_node_count": data_cone_traceback.get("total_node_count"),
                    "source_max_depth": data_cone_traceback.get("max_depth"),
                },
                missing_evidence=[
                    "source_cells_actually_removed",
                    "new_dsp_lut_carry_and_register_counts",
                    "external_accumulator_depth",
                    "clock_footprint_delta",
                ],
                next_discriminator=_luna_discriminator_choice(
                    "choose which semantic boundary can produce an exact emitted "
                    "topology, then compare that topology with the source cone"
                ),
            ),
            _judgment_step(
                "mapping_realization_and_wall_overlap",
                state="unresolved",
                scope="candidate_backend_on_current_wall",
                evidence={
                    "selected_registered_family_coverage": coverage,
                    "uncovered_path_count": scope_census.get(
                        "uncovered_path_count"
                    ),
                    "local_structural_boundary_recovered": structural_complete,
                    "no_candidate_mapping_has_been_emitted": True,
                },
                missing_evidence=[
                    "candidate_mapping_realized_on_current_design",
                    "mapped_owner_boundary_preserved",
                    "material_complete_operator_wall_overlap",
                    "post_mapping_clock_sink_and_replica_delta",
                ],
                next_discriminator=_luna_discriminator_choice(
                    "choose a semantic probe while keeping realized mapping and "
                    "whole-wall overlap separate from local structural support"
                ),
            ),
            _judgment_step(
                "counterfactual_post_rewrite_wall",
                state="unresolved",
                scope="current_timing_band",
                evidence={
                    "selected_covered_path_ranks": scope_census.get(
                        "selected_covered_path_ranks"
                    ) or [],
                    "uncovered_path_ranks": scope_census.get(
                        "uncovered_path_ranks"
                    ) or [],
                    "anonymous_peer_registered_family_count": scope_census.get(
                        "peer_family_count"
                    ),
                },
                missing_evidence=[
                    "alternate_arms_of_same_endpoint_or_operator",
                    "uncovered_sibling_lanes_or_replicas",
                    "next_independent_timing_family_and_slack_bound",
                ],
                next_discriminator={
                    "decision_owner": "luna",
                    "question": (
                        "construct the surviving-family ledger that caps global "
                        "gain before selecting an implementation"
                    ),
                },
            ),
            _judgment_step(
                "semantic_vs_physical_alternative",
                state="unresolved",
                scope="decision",
                evidence={"physical_alternatives_remain_eligible": True},
                missing_evidence=[
                    "same_seed_physical_basin_response",
                    "whole_operator_semantic_scope_and_payoff",
                ],
                next_discriminator={
                    "decision_owner": "luna",
                    "question": (
                        "compare a semantic probe/rewrite with a physical action; "
                        "neither unknown nor local support is a forced choice"
                    ),
                },
            ),
            _judgment_step(
                "post_action_failure_attribution",
                state="unresolved",
                scope="future_measurement",
                evidence={
                    "failure_classes": [
                        "semantic_contract_failure",
                        "emitter_topology_failure",
                        "physical_basin_failure",
                    ]
                },
                missing_evidence=["live_mutation_and_reprofile_result"],
                next_discriminator={
                    "event": "post_action_reprofile",
                    "question": (
                        "attribute failure to semantics, emitted topology, or the "
                        "physical basin instead of rejecting the whole operator family"
                    ),
                },
            ),
        ],
    }


def recover_sequential_operator_contract(
    design,
    *,
    critical_paths_data: list[list[str]],
    candidate_id: str,
    design_sha256: Optional[str] = None,
) -> dict[str, Any]:
    """Freshly recover one current-DCP registered boundary and its proof gaps."""
    if not isinstance(candidate_id, str) or re.fullmatch(r"sequential:[0-9a-f]{20}", candidate_id) is None:
        raise SequentialContractError("candidate_id must be a sequential hypothesis ID")
    families = mine_sequential_write_families(
        design,
        critical_paths_data=critical_paths_data,
        max_families=MAX_FAMILIES,
        design_sha256=design_sha256,
    )
    matches = [item for item in families if item.get("candidate_id") == candidate_id]
    if len(matches) != 1:
        raise SequentialContractError("candidate is absent, stale, truncated, or ambiguous")
    family = matches[0]
    netlist = design.getNetlist()
    mapping = family["bit_mapping"]
    member_facts = []
    issues = []
    for ordinal, name in enumerate(family["members"]):
        physical = design.getCell(name)
        cell_type = _base_register_type(str(physical.getType()).upper())
        schema = _REGISTER_SCHEMAS.get(cell_type)
        index_matches = list(_INDEX_RE.finditer(name))
        fact = {
            "ordinal": ordinal,
            "bit_index": (
                int(index_matches[-1].group(1)) if index_matches else None
            ),
            "cell": name,
            "cell_type": cell_type,
            "physical": _physical_fact(physical),
            "controls": {},
        }
        if schema is None:
            issues.append(f"unsupported register type at member {ordinal}")
            member_facts.append(fact)
            continue
        pins = {}
        for role, pin_name in schema.items():
            pin = netlist.getHierPortInstFromName(f"{name}/{pin_name}")
            if pin is None:
                issues.append(f"missing {role} pin at member {ordinal}")
            else:
                pins[role] = pin
        if set(pins) != set(schema):
            member_facts.append(fact)
            continue
        props = _properties(pins["output"])
        inversion_keys = {
            "clock": "IS_C_INVERTED",
            "enable": "IS_CE_INVERTED",
            "state": {
                "FDRE": "IS_R_INVERTED", "FDSE": "IS_S_INVERTED",
                "FDCE": "IS_CLR_INVERTED", "FDPE": "IS_PRE_INVERTED",
            }[cell_type],
        }
        for role in ("clock", "enable", "state"):
            try:
                net = _parent_net(netlist, pins[role])
                fact["controls"][role] = {
                    "net": _net_name(net),
                    "inversion": props.get(inversion_keys[role], "0"),
                }
            except SequentialContractError as exc:
                issues.append(f"member {ordinal} {role}: {exc}")
        try:
            d_net = _parent_net(netlist, pins["data"])
            drivers = _as_list(d_net.getLeafHierPortInsts(True, False, True))
            fact["data"] = {
                "net": _net_name(d_net),
                "drivers": [_pin_fact(pin) for pin in drivers],
            }
            if len(drivers) != 1:
                issues.append(f"member {ordinal} data has {len(drivers)} drivers")
        except SequentialContractError as exc:
            issues.append(f"member {ordinal} data: {exc}")
        try:
            q_net = _parent_net(netlist, pins["output"])
            sinks = _as_list(q_net.getLeafHierPortInsts(False, True, True))
            fact["output"] = {
                "net": _net_name(q_net),
                "sinks": [_pin_fact(pin) for pin in sinks],
            }
            if not sinks:
                issues.append(f"member {ordinal} output has no observable structural sink")
        except SequentialContractError as exc:
            issues.append(f"member {ordinal} output: {exc}")
        fact["properties"] = props
        member_facts.append(fact)

    controls = {
        role: _control_summary(member_facts, role)
        for role in ("clock", "enable", "state")
    }
    controls_common = all(
        item["status"] == "exact_same_net_and_polarity" for item in controls.values()
    )
    driver_facts = [
        driver
        for item in member_facts
        for driver in (item.get("data", {}).get("drivers") or [])
    ]
    sink_facts = [
        sink
        for item in member_facts
        for sink in (item.get("output", {}).get("sinks") or [])
    ]
    driver_types = Counter(item["cell_type"] for item in driver_facts)
    consumer_types = Counter(item["cell_type"] for item in sink_facts)
    unique_drivers = all(len(item.get("data", {}).get("drivers") or []) == 1 for item in member_facts)
    all_have_sinks = all(bool(item.get("output", {}).get("sinks")) for item in member_facts)
    structural_complete = not issues and controls_common and unique_drivers and all_have_sinks
    data_cone_traceback = _trace_family_data_cones(netlist, member_facts)
    semantic_hypotheses = _early_semantic_hypotheses(
        bit_mapping=mapping,
        driver_types=driver_types,
        data_cone_traceback=data_cone_traceback,
    )
    member_hierarchies = {_hierarchy(item["cell"]) for item in member_facts}
    driver_hierarchies = {_hierarchy(item["cell"]) for item in driver_facts if item["cell"]}
    consumer_hierarchies = {_hierarchy(item["cell"]) for item in sink_facts if item["cell"]}
    tile_x_span = int((family.get("physical_footprint") or {}).get("tile_x_span") or 0)
    classifications = _classifications(
        structural_complete=structural_complete,
        mapping_status=str(mapping.get("status") or ""),
        controls_common=controls_common,
        unique_drivers=unique_drivers,
        driver_types=driver_types,
        consumer_types=consumer_types,
        member_hierarchies=member_hierarchies,
        driver_hierarchies=driver_hierarchies,
        consumer_hierarchies=consumer_hierarchies,
        tile_x_span=tile_x_span,
    )
    operator_scope_census = _operator_scope_census(family, families)
    generating_operator_closure = _generating_operator_closure(family, families)
    hierarchy_boundary = {
        "member_hierarchy_count": len(member_hierarchies),
        "driver_hierarchy_count": len(driver_hierarchies),
        "consumer_hierarchy_count": len(consumer_hierarchies),
    }
    operator_judgment_chain = _operator_judgment_chain(
        family=family,
        scope_census=operator_scope_census,
        bit_mapping=mapping,
        hierarchy_boundary=hierarchy_boundary,
        recovery_issues=issues,
        controls_common=controls_common,
        structural_complete=structural_complete,
        data_cone_traceback=data_cone_traceback,
        classifications=classifications,
    )
    result = {
        "status": "success",
        "kind": "sequential_operator_contract_facts",
        "version": VERSION,
        "read_only": True,
        "fresh_rediscovery": True,
        "candidate_id": candidate_id,
        "design_sha256": design_sha256,
        "family": family,
        "bit_mapping": mapping,
        "controls": controls,
        "member_facts": member_facts,
        "driver_shape": {
            "unique_driver_per_member": unique_drivers,
            "driver_type_counts": dict(sorted(driver_types.items())),
            "hierarchy_count": len(driver_hierarchies),
        },
        "consumer_shape": {
            "all_members_have_sinks": all_have_sinks,
            "sink_type_counts": dict(sorted(consumer_types.items())),
            "hierarchy_count": len(consumer_hierarchies),
            "sink_count": len(sink_facts),
        },
        "hierarchy_boundary": hierarchy_boundary,
        "data_cone_traceback": data_cone_traceback,
        "semantic_hypotheses": semantic_hypotheses,
        "operator_scope_census": operator_scope_census,
        "generating_operator_closure": generating_operator_closure,
        "operator_judgment_chain": operator_judgment_chain,
        "classifications": classifications,
        "structural_boundary_recovered": structural_complete,
        "proof_status": "extracted_unproved",
        "proof_input_eligible": structural_complete,
        "proof_input_scope": "selected_registered_family_boundary_only",
        "mutation_eligible": False,
        "operator_action_contract": {
            "action_type": "local_operator_measurement_probe",
            "scope": "selected_registered_family",
            "authority": "disposable_candidate_measurement_only",
            "complete_operator_rewrite_eligible": False,
            "reason": "complete_generating_operator_contract_is_partial",
        },
        "recovery_issues": issues,
        "global_proof_obligations": [
            "registered_observable_closure",
            "bit_word_signedness_truncation_and_loss_contract",
            "clock_ce_reset_init_and_bubble_sequences",
            "latency_transaction_boundary_valid_ready_and_backpressure",
            "clock_footprint_and_physical_owner_cost",
            "emitted_topology_bit_vector_or_sequential_equivalence",
            "route_hold_pulse_drc_clock_xdc_and_official_validation",
        ],
    }
    result["contract_sha256"] = _digest(result)
    return result


def _recover_replica_member_facts(
    design, family: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Recover the direct registered boundary for a replica candidate."""
    netlist = design.getNetlist()
    member_facts = []
    issues = []
    for ordinal, name in enumerate(family["members"]):
        physical = design.getCell(name)
        cell_type = _base_register_type(str(physical.getType()).upper())
        schema = _REGISTER_SCHEMAS.get(cell_type)
        index_matches = list(_INDEX_RE.finditer(name))
        fact = {
            "ordinal": ordinal,
            "bit_index": (
                int(index_matches[-1].group(1)) if index_matches else None
            ),
            "cell": name,
            "cell_type": cell_type,
            "physical": _physical_fact(physical),
            "controls": {},
        }
        if schema is None:
            issues.append(f"unsupported register type at member {ordinal}")
            member_facts.append(fact)
            continue
        pins = {}
        for role, pin_name in schema.items():
            pin = netlist.getHierPortInstFromName(f"{name}/{pin_name}")
            if pin is None:
                issues.append(f"missing {role} pin at member {ordinal}")
            else:
                pins[role] = pin
        if set(pins) != set(schema):
            member_facts.append(fact)
            continue
        props = _properties(pins["output"])
        inversion_keys = {
            "clock": "IS_C_INVERTED",
            "enable": "IS_CE_INVERTED",
            "state": {
                "FDRE": "IS_R_INVERTED",
                "FDSE": "IS_S_INVERTED",
                "FDCE": "IS_CLR_INVERTED",
                "FDPE": "IS_PRE_INVERTED",
            }[cell_type],
        }
        for role in ("clock", "enable", "state"):
            try:
                net = _parent_net(netlist, pins[role])
                fact["controls"][role] = {
                    "net": _net_name(net),
                    "inversion": props.get(inversion_keys[role], "0"),
                }
            except SequentialContractError as exc:
                issues.append(f"member {ordinal} {role}: {exc}")
        try:
            d_net = _parent_net(netlist, pins["data"])
            drivers = _as_list(d_net.getLeafHierPortInsts(True, False, True))
            fact["data"] = {
                "net": _net_name(d_net),
                "drivers": [_pin_fact(pin) for pin in drivers],
            }
            if len(drivers) != 1:
                issues.append(f"member {ordinal} data has {len(drivers)} drivers")
        except SequentialContractError as exc:
            issues.append(f"member {ordinal} data: {exc}")
        try:
            q_net = _parent_net(netlist, pins["output"])
            sinks = _as_list(q_net.getLeafHierPortInsts(False, True, True))
            fact["output"] = {
                "net": _net_name(q_net),
                "sinks": [_pin_fact(pin) for pin in sinks],
            }
            if not sinks:
                issues.append(
                    f"member {ordinal} output has no observable structural sink"
                )
        except SequentialContractError as exc:
            issues.append(f"member {ordinal} output: {exc}")
        fact["properties"] = props
        member_facts.append(fact)
    return member_facts, issues


def _control_partition_signature(
    member_facts: list[dict[str, Any]],
) -> dict[str, Any]:
    result = {}
    for role in ("clock", "enable", "state"):
        values = [item.get("controls", {}).get(role) for item in member_facts]
        nets = [item.get("net") if isinstance(item, dict) else None for item in values]
        classes: dict[Any, int] = {}
        partition = []
        for net in nets:
            if net not in classes:
                classes[net] = len(classes)
            partition.append(classes[net])
        result[role] = {
            "missing_count": sum(item is None for item in nets),
            "net_partition": partition,
            "inversions": [
                str(item.get("inversion")) if isinstance(item, dict) else None
                for item in values
            ],
        }
    return result


def _replica_signature(
    family: dict[str, Any],
    member_facts: list[dict[str, Any]],
    *,
    cone_traceback: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Return an identity-free structural signature for register-word replicas."""
    direct_driver_types = []
    for item in member_facts:
        direct_driver_types.append(
            sorted(
                str(driver.get("cell_type") or "")
                for driver in (item.get("data", {}).get("drivers") or [])
            )
        )
    signature = {
        "grouping": family.get("grouping"),
        "cell_type": family.get("cell_type"),
        "bit_mapping": family.get("bit_mapping"),
        "member_count": len(member_facts),
        "direct_driver_types_by_bit": direct_driver_types,
        "control_partition": _control_partition_signature(member_facts),
    }
    if cone_traceback is not None:
        signature["data_cone_signatures_by_bit"] = [
            item.get("signature_sha256")
            for item in (cone_traceback.get("member_traces") or [])
        ]
        signature["trace_status"] = cone_traceback.get("status")
    return signature


def recover_sequential_operator_replicas(
    design,
    *,
    critical_paths_data: list[list[str]],
    candidate_id: str,
    design_sha256: Optional[str] = None,
    max_replicas: int = 64,
) -> dict[str, Any]:
    """Find anonymous full-word replicas of one selected registered family.

    Matching uses direct-driver/control topology followed by a per-bit cone
    signature. It never uses benchmark names, hierarchy labels, placement
    coordinates, or timing-path ranks.
    """
    if (
        isinstance(max_replicas, bool)
        or not isinstance(max_replicas, int)
        or not 1 <= max_replicas <= MAX_FAMILIES
    ):
        raise SequentialContractError(
            f"max_replicas must be an integer in 1..{MAX_FAMILIES}"
        )
    selected = recover_sequential_operator_contract(
        design,
        critical_paths_data=critical_paths_data,
        candidate_id=candidate_id,
        design_sha256=design_sha256,
    )
    selected_family = selected["family"]
    selected_facts = selected["member_facts"]
    selected_trace = selected["data_cone_traceback"]
    selected_direct = _replica_signature(selected_family, selected_facts)
    selected_full = _replica_signature(
        selected_family, selected_facts, cone_traceback=selected_trace
    )
    critical_path_sets = [
        {str(cell) for cell in path}
        for path in critical_paths_data
        if isinstance(path, list)
    ]
    critical_cells = set().union(*critical_path_sets) if critical_path_sets else set()

    groups: dict[tuple[str, str, str], list[Any]] = {}
    for cell in _as_list(design.getCells()):
        cell_type = str(cell.getType()).upper()
        if _REGISTER_RE.fullmatch(cell_type) is None:
            continue
        name = str(cell.getName())
        grouping, stem = _family_key(name)
        groups.setdefault(
            (grouping, stem, _base_register_type(cell_type)), []
        ).append(cell)

    replicas = []
    exact_cone_replica_count = 0
    scanned_shape_matches = 0
    for (grouping, stem, cell_type), members in groups.items():
        if len(members) != len(selected_facts):
            continue
        members = sorted(members, key=lambda item: str(item.getName()))
        member_names = [str(item.getName()) for item in members]
        mapping = _bit_mapping(member_names, grouping)
        identity = {
            "design_sha256": design_sha256,
            "grouping": grouping,
            "stem": stem,
            "cell_type": cell_type,
            "members": member_names,
        }
        family = {
            "candidate_id": "sequential:" + _digest(identity)[:20],
            "kind": "sequential_write_family_hypothesis",
            "evidence_scope": "structural_replica_candidate",
            "grouping": grouping,
            "cell_type": cell_type,
            "member_count": len(member_names),
            "bit_mapping": mapping,
            "members": member_names,
        }
        facts, issues = _recover_replica_member_facts(design, family)
        if issues or _replica_signature(family, facts) != selected_direct:
            continue
        scanned_shape_matches += 1
        trace = _trace_family_data_cones(design.getNetlist(), facts)
        cone_signature_match = (
            _replica_signature(family, facts, cone_traceback=trace)
            == selected_full
        )
        if cone_signature_match:
            exact_cone_replica_count += 1
        replicas.append(
            {
                "candidate_id": family["candidate_id"],
                "family": family,
                "member_facts": facts,
                "data_cone_traceback": trace,
                # A recursive cone match is useful scheduling evidence, but it
                # is not semantic equivalence.  Different LUT decompositions
                # of the same registered arithmetic operator must reach the
                # closure/replay proof instead of being discarded here.
                "cone_signature_match": cone_signature_match,
                # Live timing overlap is a scheduling feature only.  It is
                # derived from the current DCP paths and never authorizes a
                # rewrite or exposes hierarchy identity to the LLM.
                "critical_path_member_count": len(
                    critical_cells.intersection(member_names)
                ),
                "critical_path_coverage_count": sum(
                    bool(path.intersection(member_names))
                    for path in critical_path_sets
                ),
                "is_selected": family["candidate_id"] == candidate_id,
            }
        )
        if len(replicas) >= max_replicas:
            break

    return {
        "status": "success",
        "kind": "sequential_operator_replica_family_facts",
        "version": 1,
        "read_only": True,
        "fresh_rediscovery": True,
        "selected_candidate_id": candidate_id,
        "replica_count": len(replicas),
        "exact_cone_replica_count": exact_cone_replica_count,
        "semantic_screen_candidate_count": len(replicas),
        "shape_match_count_before_cone_signature": scanned_shape_matches,
        "replicas": replicas,
        "matching_basis": [
            "complete_indexed_word_shape",
            "direct_driver_type_by_bit",
            "clock_enable_state_partition_and_polarity",
            "per_bit_recursive_cone_signature_as_soft_evidence",
            "registered_closure_expression_recovery_and_replay_as_acceptance",
        ],
        "excluded_identity_features": [
            "design_name",
            "hierarchy_name",
            "cell_name",
            "placement_coordinate",
            "timing_path_rank",
        ],
        "mutation_eligible": False,
    }


__all__ = [
    "MAX_FAMILIES",
    "SequentialContractError",
    "mine_sequential_write_families",
    "recover_sequential_operator_contract",
    "recover_sequential_operator_replicas",
]
