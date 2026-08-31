# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# Portions of this file consist of AI-generated content.
# SPDX-License-Identifier: Apache-2.0

"""
RapidWright Tools - Wrapper functions for RapidWright operations
Uses the rapidwright pip package for JPype integration, with RAPIDWRIGHT_PATH
and CLASSPATH pointing to the local RapidWright git submodule for Java classes.
"""
import logging
import copy
import hashlib
import itertools
import json
import os
import re
import secrets
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, Any, Optional

# This module is imported by ``RapidWrightMCP/server.py`` when that server is
# launched directly.  In that mode Python exposes ``RapidWrightMCP`` but not
# the repository root, while trusted mutation helpers import the sibling
# ``FDAgents`` package.  Make the package lookup independent of cwd/PYTHONPATH.
_REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.isdir(os.path.join(_REPOSITORY_ROOT, "FDAgents")):
    if _REPOSITORY_ROOT not in sys.path:
        sys.path.insert(0, _REPOSITORY_ROOT)

logger = logging.getLogger(__name__)

# Global state
_initialized = False
_current_design = None
_current_design_source = None
_current_design_source_sha256 = None
_current_design_mutated = False
_session_valid = True
_checkpoint_write_blocked = False
# The recurrence family recovery is a pure function of the loaded design and
# costs minutes on a large DCP, yet one mutation recovers it twice: once to
# read the contract and once inside placement proposal.  Memoize it for the
# unmutated design it describes, keyed by that design's own digest.
_recurrence_family_memo = None
_route_analysis_secret = secrets.token_bytes(32)
_route_analysis_certificates = {}
_equivalent_source_certificates = {}
_equivalent_source_proof_calls = 0
_equivalent_source_mutation_calls = 0
_rapidwright_session_epoch = secrets.token_hex(16)


_LUT_PIN_TOKEN_RE = re.compile(r"^(?:I[0-5]|A[1-6]|[A-H][1-6])$")
_FASTEST_PIN_ALIASES = {"FASTEST", "FASTEST_FREE", "AUTO"}
_MAX_ROUTE_ANALYSIS_CONNECTIONS = 32
# Keep the implementation contract aligned with the public MCP schema and the
# trusted 40-path handoff.  ``max_connections`` still bounds the expensive
# sink-specific work, so accepting the complete path context does not expand
# the mutation surface.
_MAX_ROUTE_ANALYSIS_PATHS = 64
_MAX_ROUTE_PATH_PINS = 64
_MAX_SELECTIVE_UNROUTE_CONNECTIONS = 4
_MAX_STRUCTURE_MEMBERS = 512
_MAX_STRUCTURE_SITES = 512
_MAX_RELOCATION_OFFSET = 32
_MAX_BOUNDARY_NETS = 32
_MAX_BOUNDARY_PINS = 256
_MAX_EQUIVALENT_SOURCE_CANDIDATES = 4
_MAX_EQUIVALENT_SOURCE_PROOF_CALLS = 16
_MAX_EQUIVALENT_SOURCE_MUTATION_CALLS = 8
_MAX_EQUIVALENT_SOURCE_LEAVES = 20000
_EQUIVALENT_SOURCE_PROOF_VERSION = "equivalent-source-v1"
_FF_PRIMITIVES = {
    "FDRE": ("D", "C", "CE", "R"),
    "FDRE_1": ("D", "C", "CE", "R"),
    "FDSE": ("D", "C", "CE", "S"),
    "FDSE_1": ("D", "C", "CE", "S"),
    "FDCE": ("D", "C", "CE", "CLR"),
    "FDCE_1": ("D", "C", "CE", "CLR"),
    "FDPE": ("D", "C", "CE", "PRE"),
    "FDPE_1": ("D", "C", "CE", "PRE"),
}


def _sha256_file(path: os.PathLike | str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reset_route_analysis_state() -> None:
    global _route_analysis_secret
    _route_analysis_secret = secrets.token_bytes(32)
    _route_analysis_certificates.clear()
    _equivalent_source_certificates.clear()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _data_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("ascii")).hexdigest()


def _parse_verilog_init(value: str, expected_bits: int) -> str:
    """Return a width-qualified canonical hex value; unknown or malformed bits fail."""
    if not isinstance(value, str):
        raise ValueError("INIT is not a string")
    text = value.strip().replace("_", "")
    match = re.fullmatch(r"(?:(\d+)'([sS]?)([bBoOdDhH]))?([0-9a-fA-F]+)", text)
    if match is None:
        raise ValueError("INIT contains unsupported or unknown digits")
    declared, _signed, radix_token, digits = match.groups()
    radix = {None: 10, "b": 2, "o": 8, "d": 10, "h": 16}[radix_token.lower() if radix_token else None]
    number = int(digits, radix)
    if declared is not None and int(declared) != expected_bits:
        raise ValueError(f"INIT width is {declared}, expected {expected_bits}")
    if number < 0 or number >= (1 << expected_bits):
        raise ValueError("INIT value exceeds primitive truth-table width")
    width = (expected_bits + 3) // 4
    return f"{expected_bits}'h{number:0{width}x}"


def _canonical_property_bool(inst, key: str) -> bool:
    value = inst.getProperty(key)
    if value is None:
        return False
    raw = str(value.getValue()).strip().lower()
    if raw in {"0", "1'b0", "false"}:
        return False
    if raw in {"1", "1'b1", "true"}:
        return True
    raise ValueError(f"{key} has a non-boolean value")


def _prove_equivalent_source_facts(reference: dict, candidate: dict) -> tuple[bool, dict]:
    """Pure strict proof kernel over normalized EDIF facts."""
    if reference.get("kind") != candidate.get("kind"):
        return False, {"code": "primitive_class_mismatch"}
    if reference.get("primitive") != candidate.get("primitive"):
        return False, {"code": "primitive_mismatch"}
    if reference.get("kind") == "ff":
        compared = ("init", "inputs", "inversions", "cycle_alignment")
        for key in compared:
            if reference.get(key) != candidate.get(key):
                return False, {"code": f"ff_{key}_mismatch", "field": key}
        return True, {
            "method": "identical_ff_transition_relation",
            "compared_fields": list(compared),
            "cycle_alignment": reference["cycle_alignment"],
        }
    if reference.get("kind") != "lut":
        return False, {"code": "unsupported_primitive"}

    ref_inputs = reference.get("inputs") or {}
    cand_inputs = candidate.get("inputs") or {}
    if len(ref_inputs) != len(cand_inputs) or not ref_inputs:
        return False, {"code": "lut_input_mapping_missing"}
    ref_items = sorted(ref_inputs.items())
    cand_items = sorted(cand_inputs.items())
    if len({(v["canonical_net"], bool(v["inverted"])) for _, v in ref_items}) != len(ref_items):
        return False, {"code": "lut_reference_input_mapping_ambiguous"}
    if len({(v["canonical_net"], bool(v["inverted"])) for _, v in cand_items}) != len(cand_items):
        return False, {"code": "lut_candidate_input_mapping_ambiguous"}

    ref_truth = int(reference["truth_table_hex"], 16)
    cand_truth = int(candidate["truth_table_hex"], 16)
    valid = []
    for permutation in itertools.permutations(range(len(cand_items))):
        if any(
            ref_items[index][1]["canonical_net"]
            != cand_items[permutation[index]][1]["canonical_net"]
            or bool(ref_items[index][1]["inverted"])
            != bool(cand_items[permutation[index]][1]["inverted"])
            for index in range(len(ref_items))
        ):
            continue
        mapping = [{
            "reference_pin": ref_items[index][0],
            "candidate_pin": cand_items[permutation[index]][0],
            "canonical_net": ref_items[index][1]["canonical_net"],
            "reference_inverted": bool(ref_items[index][1]["inverted"]),
            "candidate_inverted": bool(cand_items[permutation[index]][1]["inverted"]),
        } for index in range(len(ref_items))]
        equivalent = True
        for assignment in range(1 << len(ref_items)):
            ref_index = 0
            cand_index = 0
            for index, (_, ref_fact) in enumerate(ref_items):
                bit = (assignment >> index) & 1
                ref_index |= (bit ^ int(bool(ref_fact["inverted"]))) << index
                cand_position = permutation[index]
                cand_fact = cand_items[cand_position][1]
                cand_index |= (bit ^ int(bool(cand_fact["inverted"]))) << cand_position
            if ((ref_truth >> ref_index) & 1) != ((cand_truth >> cand_index) & 1):
                equivalent = False
                break
        if equivalent:
            valid.append(mapping)
    if not valid:
        return False, {"code": "lut_truth_table_or_input_mismatch"}
    if len(valid) != 1:
        return False, {"code": "lut_pin_permutation_ambiguous", "mapping_count": len(valid)}
    return True, {
        "method": "exhaustive_lut_truth_table_under_unique_pin_permutation",
        "pin_permutation": valid[0],
        "assignments_checked": 1 << len(ref_items),
    }


def _as_list(value) -> list:
    if value is None:
        return []
    return list(value)


def _safe_call(obj, method: str, default="unknown"):
    try:
        value = getattr(obj, method)()
        return default if value is None else value
    except Exception:
        return default


def _site_pin_key(pin) -> str:
    site_inst = pin.getSiteInst()
    site_name = str(site_inst.getName()) if site_inst is not None else "unknown_site"
    return f"{site_name}/{pin.getName()}"


def _pip_key(pip) -> str:
    if pip is None:
        return "<null-pip>"
    try:
        tile = str(pip.getTile().getName())
        start = str(pip.getStartWireName())
        end = str(pip.getEndWireName())
        reversed_flag = int(bool(pip.isReversed()))
        return f"{tile}|{start}|{end}|r={reversed_flag}"
    except Exception:
        return str(pip)


def _pip_digest(pips) -> str:
    keys = sorted(_pip_key(pip) for pip in _as_list(pips))
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()


def _pip_fixed(pip):
    try:
        return bool(pip.isPIPFixed())
    except Exception:
        return "unknown"


def _logical_net_name(net):
    try:
        logical = net.getLogicalHierNet()
        if logical is not None:
            return str(logical.getHierarchicalNetName())
    except Exception:
        pass
    try:
        logical = net.getLogicalNet()
        if logical is not None:
            return str(logical.getName())
    except Exception:
        pass
    return "unknown"


def _route_length_tiles(connection_pips):
    """Sum exact PIP endpoint tile Manhattan distances; unavailable stays unknown."""
    total = 0
    try:
        for pip in connection_pips:
            start = pip.getStartNode()
            end = pip.getEndNode()
            if start is None or end is None:
                return "unknown"
            total += int(start.getTile().getManhattanDistance(end.getTile()))
        return total
    except Exception:
        return "unknown"


def _intent_histogram(connection_pips):
    histogram: dict[str, int] = {}
    seen = set()
    try:
        for pip in connection_pips:
            for node in (pip.getStartNode(), pip.getEndNode()):
                if node is None:
                    continue
                key = str(node)
                if key in seen:
                    continue
                seen.add(key)
                intent = str(node.getIntentCode())
                histogram[intent] = histogram.get(intent, 0) + 1
        return dict(sorted(histogram.items()))
    except Exception:
        return "unknown"


def _resolve_sink_pin(design, net, sink_ref: str):
    sinks = _as_list(net.getSinkPins())
    exact = [pin for pin in sinks if _site_pin_key(pin) == sink_ref]
    if len(exact) == 1:
        return exact[0], None
    if len(exact) > 1:
        return None, "ambiguous sink site pin"

    short = [pin for pin in sinks if str(pin.getName()) == sink_ref]
    if len(short) == 1:
        return short[0], None
    if len(short) > 1:
        return None, "ambiguous sink pin name; use site_instance/pin"

    try:
        hier_pin = design.getNetlist().getHierPortInstFromName(sink_ref)
        if hier_pin is not None:
            routed_net = hier_pin.getRoutedPhysicalNet(design)
            routed_pins = _as_list(hier_pin.getAllRoutedSitePinInsts(design))
            if routed_net is not net:
                return None, "logical sink resolves to a different physical net"
            if len(routed_pins) != 1:
                return None, "logical pin does not resolve to exactly one routed sink site pin"
            routed_pin = routed_pins[0]
            if routed_pin.isOutPin():
                return None, "logical pin does not resolve to one routed sink site pin"
            if routed_pin not in sinks:
                return None, "resolved site pin is not a sink of the requested net"
            return routed_pin, None
    except Exception as exc:
        return None, f"sink resolution error: {exc}"
    return None, "sink pin not found"


def _connection_requests_from_critical_paths(design, critical_paths_data) -> tuple[list[dict], list[dict]]:
    requests = []
    errors = []
    seen = set()
    netlist = design.getNetlist()
    for path_index, path in enumerate(critical_paths_data):
        previous_cell = None
        for pin_ref in path:
            cell_name = pin_ref.rsplit("/", 1)[0] if "/" in pin_ref else pin_ref
            cross_cell = previous_cell is not None and cell_name != previous_cell
            previous_cell = cell_name
            if not cross_cell:
                continue
            try:
                hier_pin = netlist.getHierPortInstFromName(pin_ref)
                if hier_pin is None:
                    errors.append({"path": path_index, "pin": pin_ref, "error": "logical pin not found"})
                    continue
                net = hier_pin.getRoutedPhysicalNet(design)
                routed_pins = _as_list(hier_pin.getAllRoutedSitePinInsts(design))
                sink = routed_pins[0] if len(routed_pins) == 1 else None
                if net is None or sink is None or sink.isOutPin():
                    errors.append({"path": path_index, "pin": pin_ref, "error": "pin is not a routed sink connection"})
                    continue
                key = (str(net.getName()), _site_pin_key(sink))
                if key in seen:
                    continue
                seen.add(key)
                requests.append({"physical_net": key[0], "sink_pin": key[1], "requested_sink_pin": pin_ref})
            except Exception as exc:
                errors.append({"path": path_index, "pin": pin_ref, "error": str(exc)})
    return requests, errors


def _validate_route_analysis_input(connections, critical_paths_data, max_connections: int) -> None:
    if isinstance(max_connections, bool) or not isinstance(max_connections, int):
        raise ValueError("max_connections must be an integer")
    if not 1 <= max_connections <= _MAX_ROUTE_ANALYSIS_CONNECTIONS:
        raise ValueError(f"max_connections must be 1..{_MAX_ROUTE_ANALYSIS_CONNECTIONS}")
    if connections is not None:
        if not isinstance(connections, list) or len(connections) > _MAX_ROUTE_ANALYSIS_CONNECTIONS:
            raise ValueError(f"connections must be a list of at most {_MAX_ROUTE_ANALYSIS_CONNECTIONS}")
        for index, item in enumerate(connections):
            if not isinstance(item, dict):
                raise ValueError(f"connections[{index}] must be an object")
            if set(item) - {"physical_net", "net", "sink_pin"}:
                raise ValueError(f"connections[{index}] contains unsupported fields")
            net_name = item.get("physical_net", item.get("net"))
            if not isinstance(net_name, str) or not net_name or len(net_name) > 1024:
                raise ValueError(f"connections[{index}] physical_net is invalid")
            if not isinstance(item.get("sink_pin"), str) or not item["sink_pin"] or len(item["sink_pin"]) > 1024:
                raise ValueError(f"connections[{index}] sink_pin is invalid")
    if critical_paths_data is not None:
        if not isinstance(critical_paths_data, list) or len(critical_paths_data) > _MAX_ROUTE_ANALYSIS_PATHS:
            raise ValueError(f"critical_paths_data must contain at most {_MAX_ROUTE_ANALYSIS_PATHS} paths")
        for index, path in enumerate(critical_paths_data):
            if not isinstance(path, list) or len(path) > _MAX_ROUTE_PATH_PINS:
                raise ValueError(f"critical_paths_data[{index}] must contain at most {_MAX_ROUTE_PATH_PINS} pins")
            if any(not isinstance(pin, str) or not pin or len(pin) > 1024 for pin in path):
                raise ValueError(f"critical_paths_data[{index}] contains an invalid pin")
    if not connections and not critical_paths_data:
        raise ValueError("provide inline critical_paths_data or explicit connections")


def _certificate_payload(analysis: dict, removable_digest: str) -> str:
    values = [
        str(analysis["physical_net"]),
        str(analysis["sink_pin"]),
        str(analysis["before_net_pip_digest"]),
        str(analysis["before_sink_pip_digest"]),
        removable_digest,
    ]
    return "\n".join(values)


def _issue_route_certificate(analysis: dict, removable_pips) -> str:
    removable_digest = _pip_digest(removable_pips)
    payload = _certificate_payload(analysis, removable_digest)
    certificate = hashlib.sha256(_route_analysis_secret + payload.encode("utf-8")).hexdigest()
    _route_analysis_certificates[certificate] = {
        "physical_net": analysis["physical_net"],
        "sink_pin": analysis["sink_pin"],
        "before_net_pip_digest": analysis["before_net_pip_digest"],
        "before_sink_pip_digest": analysis["before_sink_pip_digest"],
        "removable_pip_digest": removable_digest,
    }
    return certificate


def _analyze_one_connection(design, design_tools, request: dict, *, issue_certificate: bool = True) -> dict:
    physical_net = str(request.get("physical_net", request.get("net", "")))
    sink_ref = str(request.get("sink_pin", ""))
    result = {
        "logical_net": "unknown",
        "physical_net": physical_net,
        "source_pin": "unknown",
        "sink_pin": sink_ref,
        "requested_sink_pin": request.get("requested_sink_pin", sink_ref),
        "sink_specific_pip_count": "unknown",
        "shared_trunk_count": "unknown",
        "shared_trunk_determinable": False,
        "manhattan_distance_tiles": "unknown",
        "route_length_tiles": "unknown",
        "route_length_basis": "PIP endpoint tile Manhattan sum",
        "detour_ratio": "unknown",
        "intent_histogram": "unknown",
        "is_clock": "unknown",
        "is_static": "unknown",
        "fixed_pip_count": "unknown",
        "has_fixed_routing": "unknown",
        "routing_locked": "unknown",
        "routing_lock_basis": "PIP.isPIPFixed",
        "gap": "unknown",
        "island_pip_count": "unknown",
        "error": None,
        "delay": "unknown",
        "recommended_action": "reject",
        "shared_trunk_safe": False,
        "before_net_pip_digest": "unknown",
        "before_sink_pip_digest": "unknown",
        "mutation_target": None,
    }
    net = design.getNet(physical_net)
    if net is None:
        result.update({"gap": True, "error": "physical net not found", "recommended_action": "reject_missing_net"})
        return result

    result["logical_net"] = _logical_net_name(net)
    result["is_clock"] = bool(net.isClockNet())
    result["is_static"] = bool(net.isStaticNet())
    net_pips = _as_list(net.getPIPs())
    result["before_net_pip_digest"] = _pip_digest(net_pips)
    fixed_values = [_pip_fixed(pip) for pip in net_pips]
    if all(value != "unknown" for value in fixed_values):
        result["fixed_pip_count"] = sum(bool(value) for value in fixed_values)
        result["has_fixed_routing"] = result["fixed_pip_count"] > 0
        result["routing_locked"] = result["has_fixed_routing"]

    source = net.getSource()
    if source is not None:
        result["source_pin"] = _site_pin_key(source)
    sink, sink_error = _resolve_sink_pin(design, net, sink_ref)
    if sink_error:
        result.update({"gap": True, "error": sink_error, "recommended_action": "reject_sink_resolution"})
        return result
    result["sink_pin"] = _site_pin_key(sink)

    try:
        target_path = _as_list(design_tools.getConnectionPIPs(sink))
        if any(pip is None for pip in target_path):
            raise RuntimeError("connection path contains a null PIP")
    except Exception as exc:
        result.update({"gap": True, "error": f"connection path error: {exc}", "recommended_action": "reject_gap"})
        return result

    result["sink_specific_pip_count"] = len(target_path)
    result["before_sink_pip_digest"] = _pip_digest(target_path)
    result["route_length_tiles"] = _route_length_tiles(target_path)
    result["intent_histogram"] = _intent_histogram(target_path)
    try:
        result["manhattan_distance_tiles"] = int(source.getTile().getManhattanDistance(sink.getTile()))
    except Exception:
        pass
    if (
        isinstance(result["route_length_tiles"], int)
        and isinstance(result["manhattan_distance_tiles"], int)
        and result["manhattan_distance_tiles"] > 0
    ):
        result["detour_ratio"] = round(result["route_length_tiles"] / result["manhattan_distance_tiles"], 6)

    all_paths = {}
    path_errors = []
    for other in _as_list(net.getSinkPins()):
        try:
            path = _as_list(design_tools.getConnectionPIPs(other))
            if any(pip is None for pip in path):
                raise RuntimeError("null PIP")
            all_paths[_site_pin_key(other)] = path
        except Exception as exc:
            path_errors.append(f"{_site_pin_key(other)}: {exc}")
    target_keys = {_pip_key(pip) for pip in target_path}
    other_keys = {
        _pip_key(pip)
        for key, path in all_paths.items()
        if key != result["sink_pin"]
        for pip in path
    }
    union_keys = {_pip_key(pip) for path in all_paths.values() for pip in path}
    net_keys = {_pip_key(pip) for pip in net_pips}
    result["shared_trunk_count"] = len(target_keys & other_keys)
    result["shared_trunk_determinable"] = not path_errors
    result["island_pip_count"] = len(net_keys - union_keys) if not path_errors else "unknown"
    result["gap"] = bool(path_errors) or (bool(net_pips) and not target_path)

    # Do not call getTrimmablePIPsFromPins() here. In this vendored version it
    # can update dual-driver/intra-site state while calculating the set. The
    # side-effect-free certificate is the exact target path minus every other
    # reconstructed sink path.
    removable = [pip for pip in target_path if _pip_key(pip) not in other_keys]
    removable_keys = {_pip_key(pip) for pip in removable}
    safety = (
        not path_errors
        and len(all_paths) > 1
        and result["island_pip_count"] == 0
        and not result["gap"]
        and bool(removable_keys)
        and removable_keys <= target_keys
        and not (removable_keys & other_keys)
        and result["has_fixed_routing"] is False
        and not result["is_clock"]
        and not result["is_static"]
    )
    result["shared_trunk_safe"] = safety
    if path_errors:
        result["error"] = "unable to reconstruct every sink path: " + "; ".join(path_errors[:4])
    if result["is_clock"]:
        result["recommended_action"] = "reject_clock"
    elif result["is_static"]:
        result["recommended_action"] = "reject_static"
    elif result["has_fixed_routing"] != False:
        result["recommended_action"] = "reject_fixed_or_lock_unknown"
    elif safety:
        result["recommended_action"] = "selective_unroute_candidate"
        if issue_certificate:
            certificate = _issue_route_certificate(result, removable)
            result["mutation_target"] = {
                "physical_net": result["physical_net"],
                "sink_pin": result["sink_pin"],
                "before_net_pip_digest": result["before_net_pip_digest"],
                "before_sink_pip_digest": result["before_sink_pip_digest"],
                "certificate": certificate,
            }
    else:
        result["recommended_action"] = "reject_unproven_branch"
    return result


def _analyze_route_connections_for_design(
    design,
    design_tools,
    *,
    connections=None,
    critical_paths_data=None,
    max_connections: int = 16,
) -> Dict[str, Any]:
    _validate_route_analysis_input(connections, critical_paths_data, max_connections)
    requests = []
    input_errors = []
    if connections:
        requests.extend({
            "physical_net": item.get("physical_net", item.get("net")),
            "sink_pin": item["sink_pin"],
        } for item in connections)
    if critical_paths_data:
        derived, input_errors = _connection_requests_from_critical_paths(design, critical_paths_data)
        requests.extend(derived)

    deduplicated = []
    seen = set()
    for request in requests:
        key = (request["physical_net"], request["sink_pin"])
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(request)
    truncated = len(deduplicated) > max_connections
    analyses = [
        _analyze_one_connection(design, design_tools, request)
        for request in deduplicated[:max_connections]
    ]
    return {
        "status": "success",
        "read_only": True,
        "connection_count": len(analyses),
        "connections": analyses,
        "input_errors": input_errors,
        "truncated": truncated,
        "timing_model_used": False,
    }


def _resolve_vivado_candidate(value: os.PathLike | str | None) -> Optional[str]:
    if not value:
        return None
    path = os.path.abspath(os.path.expanduser(str(value)))
    if os.path.isdir(path):
        path = os.path.join(path, "vivado")
    if os.path.isfile(path) and os.access(path, os.X_OK):
        return path
    return None


def _prepend_env_path(path: str) -> None:
    current = os.environ.get("PATH", "")
    parts = [part for part in current.split(os.pathsep) if part]
    if path in parts:
        return
    os.environ["PATH"] = path if not current else f"{path}{os.pathsep}{current}"


def _default_vivado_candidates() -> list[str]:
    candidates: list[str] = []
    for key in ("VIVADO_EXEC", "VIVADO_BIN"):
        value = os.environ.get(key)
        if value:
            candidates.append(value)
    xilinx_vivado = os.environ.get("XILINX_VIVADO")
    if xilinx_vivado:
        candidates.append(os.path.join(xilinx_vivado, "bin", "vivado"))
    discovered = shutil.which("vivado")
    if discovered:
        candidates.append(discovered)
    candidates.extend(
        [
            "/data/qlli/Xilinx/2025.1/Vivado/bin/vivado",
            "/opt/Xilinx/2025.1/Vivado/bin/vivado",
            "/tools/Xilinx/2025.1/Vivado/bin/vivado",
        ]
    )
    return candidates


def _configure_vivado_lookup(candidates: Optional[list[os.PathLike | str]] = None) -> Dict[str, str]:
    """Set Vivado lookup env vars when a usable executable can be discovered."""
    for candidate in candidates or _default_vivado_candidates():
        vivado_exec = _resolve_vivado_candidate(candidate)
        if vivado_exec is None:
            continue
        vivado_bin = os.path.dirname(vivado_exec)
        os.environ["VIVADO_EXEC"] = vivado_exec
        os.environ["VIVADO_BIN"] = vivado_exec
        _prepend_env_path(vivado_bin)
        return {
            "VIVADO_EXEC": vivado_exec,
            "VIVADO_BIN_DIR": vivado_bin,
        }
    return {}


def _configure_repo_local_rapidwright(project_root: Optional[os.PathLike | str] = None) -> Dict[str, str]:
    """Point Python and Java lookup paths at a RapidWright checkout.

    Resolution order:
      1. An externally provided RAPIDWRIGHT_PATH that actually exists
         (deployments reuse a shared checkout outside the repo).
      2. The repo-local ``RapidWright`` submodule.
      3. Neither exists: leave env untouched so the pip ``rapidwright``
         package falls back to its own bundled jars.
    """
    if project_root is None:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # ``rapidwright`` starts JPype during import.  Some remote shells carry a
    # stale JAVA_HOME even though the project Python environment provides a
    # complete jdk4py runtime.  Prefer the explicit environment only when it
    # contains libjvm; otherwise recover the runtime shipped with the active
    # interpreter.  This is environment discovery, not a tool fallback: the
    # same RapidWright checkout and Java classes are still used.
    java_home = os.environ.get("JAVA_HOME", "")
    java_candidates = [
        os.path.join(java_home, "lib", "server", "libjvm.so"),
        os.path.join(java_home, "jre", "lib", "amd64", "server", "libjvm.so"),
    ] if java_home else []
    if not any(os.path.isfile(path) for path in java_candidates):
        try:
            import jdk4py
            bundled_java = os.path.join(
                os.path.dirname(jdk4py.__file__), "java-runtime"
            )
            bundled_jvm = os.path.join(
                bundled_java, "lib", "server", "libjvm.so"
            )
            if os.path.isfile(bundled_jvm):
                os.environ["JAVA_HOME"] = bundled_java
        except ImportError:
            pass

    env_root = os.environ.get("RAPIDWRIGHT_PATH")
    repo_root_rw = os.path.join(str(project_root), "RapidWright")
    def usable_checkout(path: str) -> bool:
        jars = os.path.join(path, "jars")
        return (
            os.path.isdir(path)
            and os.path.isdir(jars)
            and any(name.endswith(".jar") for name in os.listdir(jars))
        )

    if env_root and usable_checkout(env_root):
        rw_root = env_root
    elif usable_checkout(repo_root_rw):
        rw_root = repo_root_rw
    else:
        logger.info(
            "No RapidWright checkout found (env=%r, repo=%r); "
            "using pip-bundled rapidwright jars",
            env_root,
            repo_root_rw,
        )
        if env_root:
            os.environ.pop("RAPIDWRIGHT_PATH", None)
            if env_root in os.environ.get("CLASSPATH", ""):
                os.environ.pop("CLASSPATH", None)
        return _configure_vivado_lookup()

    python_src = os.path.join(rw_root, "python", "src")

    os.environ["RAPIDWRIGHT_PATH"] = rw_root
    if not os.environ.get("CLASSPATH"):
        os.environ["CLASSPATH"] = f"{rw_root}/bin:{rw_root}/jars/*"
    if os.path.isdir(python_src) and python_src not in sys.path:
        sys.path.insert(0, python_src)
    vivado_env = _configure_vivado_lookup()

    env = {
        "RAPIDWRIGHT_PATH": rw_root,
        "CLASSPATH": os.environ["CLASSPATH"],
        "PYTHONPATH_ENTRY": python_src,
    }
    env.update(vivado_env)
    return env


def _normalize_lut_pin_swaps(pin_swaps: list) -> list[tuple[str, str]]:
    """
    Normalize user-facing LUT pin swap specs.

    Accepted forms:
      [["I0", "A6"], ["I1", "A5"]]
      [{"from": "I0", "to": "A6"}, {"old_pin": "I1", "new_pin": "FASTEST"}]

    Pin tokens can be logical LUT pins (I0..I5), physical BEL pins (A1..A6),
    or site pins (A1..H6, validated against the target cell later).
    """
    if not isinstance(pin_swaps, list):
        raise ValueError("pin_swaps must be a list")

    normalized: list[tuple[str, str]] = []
    seen_sources: set[str] = set()

    for idx, spec in enumerate(pin_swaps):
        if isinstance(spec, dict):
            old_pin = (
                spec.get("from")
                or spec.get("old_pin")
                or spec.get("source")
                or spec.get("src")
            )
            new_pin = (
                spec.get("to")
                or spec.get("new_pin")
                or spec.get("target")
                or spec.get("dst")
            )
        elif isinstance(spec, (list, tuple)) and len(spec) == 2:
            old_pin, new_pin = spec
        else:
            raise ValueError(
                f"pin_swaps[{idx}] must be a two-item list/tuple or dict"
            )

        if old_pin is None or new_pin is None:
            raise ValueError(f"pin_swaps[{idx}] is missing from/to pins")

        old_token = str(old_pin).strip().upper()
        new_token = str(new_pin).strip().upper()

        if not _LUT_PIN_TOKEN_RE.match(old_token):
            raise ValueError(f"invalid source LUT pin token: {old_pin!r}")
        if (
            new_token not in _FASTEST_PIN_ALIASES
            and not _LUT_PIN_TOKEN_RE.match(new_token)
        ):
            raise ValueError(f"invalid target LUT pin token: {new_pin!r}")
        if old_token in seen_sources:
            raise ValueError(f"duplicate source LUT pin token: {old_token}")

        seen_sources.add(old_token)
        normalized.append((old_token, new_token))

    return normalized


def _java_string_map_to_dict(java_map) -> dict[str, str]:
    result: dict[str, str] = {}
    if java_map is None:
        return result
    for entry in java_map.entrySet():
        result[str(entry.getKey())] = str(entry.getValue())
    return result


def _site_pin_name_for_physical(cell, physical_pin: str) -> str:
    bel_name = str(cell.getBELName())
    if not bel_name:
        raise ValueError("cell has no BEL name")
    return bel_name[0] + physical_pin[1]


def _physical_pin_exists(cell, physical_pin: str) -> bool:
    try:
        return cell.getBEL().getPin(physical_pin) is not None
    except Exception:
        return False


def _preferred_lut_physical_pins(cell) -> list[str]:
    # RapidWright LUTTools searches high-numbered pins first for speed.
    return [
        pin
        for pin in ("A6", "A5", "A4", "A3", "A2", "A1")
        if _physical_pin_exists(cell, pin)
    ]


def _resolve_lut_physical_pin(cell, pin_token: str) -> str:
    pin_token = str(pin_token).strip().upper()
    if pin_token.startswith("I"):
        physical = cell.getPhysicalPinMapping(pin_token)
        if physical is None:
            raise ValueError(
                f"logical pin {pin_token} is not mapped on cell {cell.getName()}"
            )
        physical = str(physical)
    elif pin_token.startswith("A"):
        physical = pin_token
    else:
        site_pin = pin_token
        expected_letter = str(cell.getBELName())[0]
        if site_pin[0] != expected_letter:
            raise ValueError(
                f"site pin {site_pin} does not belong to BEL {cell.getBELName()}"
            )
        physical = "A" + site_pin[1]

    if not _physical_pin_exists(cell, physical):
        raise ValueError(
            f"physical pin {physical} does not exist on BEL {cell.getBELName()}"
        )
    return physical


def _resolve_lut_target_physical_pin(cell, old_physical: str, pin_token: str, lut_tools) -> str:
    pin_token = str(pin_token).strip().upper()
    if pin_token in _FASTEST_PIN_ALIASES:
        fastest_free = lut_tools.getUnmappedPhysicalLUTInputPin(cell)
        if fastest_free is not None:
            return str(fastest_free)
        for candidate in _preferred_lut_physical_pins(cell):
            if candidate != old_physical:
                return candidate
        raise ValueError(f"no alternate physical LUT pin available on {cell.getName()}")
    return _resolve_lut_physical_pin(cell, pin_token)


def _split_lut_cell_pin_path(pin_path: str) -> tuple[str, str]:
    pin_text = str(pin_path).strip()
    if "/" not in pin_text:
        raise ValueError("pin path must be hierarchical cell path plus logical pin, e.g. cell/I0")
    cell_name, logical_pin = pin_text.rsplit("/", 1)
    if not cell_name or not logical_pin:
        raise ValueError("pin path must include both cell name and logical pin")
    logical_pin = logical_pin.upper()
    if not re.match(r"^I[0-5]$", logical_pin):
        raise ValueError(f"logical pin must be I0..I5, got {logical_pin!r}")
    return cell_name, logical_pin


def _empty_lut_pin_swap_candidate(
    pin_path: str,
    cell_name: Optional[str] = None,
    logical_pin: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "pin": pin_path,
        "cell": cell_name,
        "logical_pin": logical_pin,
        "cell_type": None,
        "site": None,
        "bel": None,
        "current_physical_pin": None,
        "current_site_pin": None,
        "fastest_target_physical_pin": None,
        "fastest_target_site_pin": None,
        "is_actionable": False,
        "reason": None,
    }


def _inspect_lut_pin_swap_candidates_for_design(
    cell_pins: list[str],
    design,
    lut_tools,
) -> Dict[str, Any]:
    candidates = []
    invalid = []

    if not isinstance(cell_pins, list):
        return {
            "status": "error",
            "error": "cell_pins must be a list of Vivado pin paths",
            "candidates": [],
            "invalid": [],
        }

    for pin_path in cell_pins:
        pin_text = str(pin_path)
        try:
            cell_name, logical_pin = _split_lut_cell_pin_path(pin_text)
        except ValueError as e:
            invalid.append({"pin": pin_text, "reason": str(e)})
            continue

        cell = design.getCell(cell_name)
        if cell is None:
            invalid.append({
                "pin": pin_text,
                "cell": cell_name,
                "logical_pin": logical_pin,
                "reason": f"Cell '{cell_name}' not found in design",
            })
            continue

        candidate = _empty_lut_pin_swap_candidate(pin_text, cell_name, logical_pin)
        candidate["cell_type"] = str(cell.getType() or "")
        candidate["site"] = str(cell.getSiteName()) if cell.getSiteName() is not None else None
        candidate["bel"] = str(cell.getBELName()) if cell.getBELName() is not None else None

        try:
            bel = cell.getBEL()
            if bel is None or not bel.isLUT():
                candidate["reason"] = f"Cell '{cell_name}' is not placed on a LUT BEL"
                candidates.append(candidate)
                continue
            if not cell.isPlaced() or cell.getSiteInst() is None:
                candidate["reason"] = f"Cell '{cell_name}' is not placed"
                candidates.append(candidate)
                continue

            cell_type = candidate["cell_type"]
            is_routethru = bool(getattr(cell, "isRoutethru", lambda: False)())
            if not cell_type.startswith("LUT") and not is_routethru:
                candidate["reason"] = (
                    f"Cell '{cell_name}' has type {cell_type}; only ordinary LUT "
                    "cells/routethrus are inspectable"
                )
                candidates.append(candidate)
                continue

            current_physical = _resolve_lut_physical_pin(cell, logical_pin)
            current_site = _site_pin_name_for_physical(cell, current_physical)
            target_physical = _resolve_lut_target_physical_pin(
                cell, current_physical, "FASTEST", lut_tools
            )
            target_site = _site_pin_name_for_physical(cell, target_physical)

            candidate["current_physical_pin"] = current_physical
            candidate["current_site_pin"] = current_site
            candidate["fastest_target_physical_pin"] = target_physical
            candidate["fastest_target_site_pin"] = target_site
            candidate["is_actionable"] = target_physical != current_physical
            candidate["reason"] = (
                "fastest target is available"
                if candidate["is_actionable"]
                else "fastest target resolves to the current pin"
            )
        except Exception as e:
            candidate["reason"] = str(e)

        candidates.append(candidate)

    return {
        "status": "success",
        "candidates": candidates,
        "invalid": invalid,
    }


def initialize_rapidwright(jvm_max_memory: str = "4G") -> Dict[str, Any]:
    """
    Initialize the RapidWright environment.

    Args:
        jvm_max_memory: Maximum JVM heap size (default: "4G")

    Returns:
        Dictionary with initialization status, version, and install path
    """
    global _initialized

    if _initialized:
        # Return version and path info even when already initialized
        try:
            import rapidwright
            import os
            from com.xilinx.rapidwright.device import Device
            version = str(Device.RAPIDWRIGHT_VERSION)
            install_path = os.path.dirname(rapidwright.__file__)
            rapidwright_path_env = os.environ.get('RAPIDWRIGHT_PATH')
            classpath = os.environ.get('CLASSPATH')
        except Exception:
            version = 'unknown'
            install_path = 'unknown'
            rapidwright_path_env = None

        result = {
            "status": "already_initialized",
            "message": "RapidWright already initialized",
            "rapidwright_version": version,
            "rapidwright_install_path": install_path
        }
        if rapidwright_path_env:
            result["RAPIDWRIGHT_PATH"] = rapidwright_path_env
        if classpath:
            result["CLASSPATH"] = classpath
        return result

    try:
        import os

        # Point at the repo-local RapidWright submodule so we always pick up
        # the expected Python and Java classes for this checkout.
        env_info = _configure_repo_local_rapidwright()
        rw_root = env_info.get("RAPIDWRIGHT_PATH")
        if rw_root:
            logger.info("Using RapidWright checkout at %s", rw_root)
        else:
            logger.info("Using pip-bundled RapidWright jar")

        # Import rapidwright - this automatically starts the JVM
        import rapidwright
        from com.xilinx.rapidwright.device import Device

        _initialized = True

        logger.info("RapidWright initialized successfully")

        # Test that we can access basic functionality
        device_count = len(Device.getAvailableDevices())

        # Get version and install path
        version = str(Device.RAPIDWRIGHT_VERSION)
        install_path = os.path.dirname(rapidwright.__file__)
        rapidwright_path_env = os.environ.get('RAPIDWRIGHT_PATH')
        classpath = os.environ.get('CLASSPATH')

        result = {
            "status": "success",
            "message": "RapidWright initialized successfully",
            "rapidwright_version": version,
            "rapidwright_install_path": install_path,
            "available_devices": device_count
        }
        if rapidwright_path_env:
            result["RAPIDWRIGHT_PATH"] = rapidwright_path_env
        if classpath:
            result["CLASSPATH"] = classpath
        return result

    except Exception as e:
        logger.error(f"Failed to initialize RapidWright: {e}")
        return {
            "status": "error",
            "message": f"Failed to initialize RapidWright: {str(e)}",
            "hint": "Make sure RapidWright is built ('make build-rapidwright'), RAPIDWRIGHT_PATH/CLASSPATH are set, and Java 11+ is installed"
        }


def get_supported_devices() -> Dict[str, Any]:
    """
    Get list of all FPGA devices supported by RapidWright, including families and part numbers.

    Returns:
        Dictionary with devices organized as a tree: Series -> FamilyType -> Devices
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}

    try:
        from com.xilinx.rapidwright.device import PartNameTools

        # Get all parts from RapidWright's part database
        all_parts = PartNameTools.getParts()

        # Build tree structure: Series -> FamilyType -> Devices (deduplicated)
        # Use sets to avoid duplicates since multiple parts map to the same device
        device_tree_sets = {}

        for part in all_parts:
            series_name = str(part.getSeries())
            family_name = str(part.getFamily())
            device_name = str(part.getDevice())

            # Initialize series level if needed
            if series_name not in device_tree_sets:
                device_tree_sets[series_name] = {}

            # Initialize family level if needed
            if family_name not in device_tree_sets[series_name]:
                device_tree_sets[series_name][family_name] = set()

            # Add device to family (set handles deduplication)
            device_tree_sets[series_name][family_name].add(device_name)

        # Convert sets to sorted lists for JSON serialization
        device_tree = {}
        total_devices = 0

        for series in device_tree_sets:
            device_tree[series] = {}
            for family in device_tree_sets[series]:
                device_tree[series][family] = sorted(device_tree_sets[series][family])
                total_devices += len(device_tree[series][family])

        # Calculate summary statistics
        series_count = len(device_tree)
        family_count = sum(len(families) for families in device_tree.values())

        return {
            "status": "success",
            "total_devices": total_devices,
            "series_count": series_count,
            "family_count": family_count,
            "device_tree": device_tree
        }

    except Exception as e:
        logger.error(f"Error getting supported devices: {e}")
        return {"error": str(e)}


def get_device_info(device_name: str) -> Dict[str, Any]:
    """
    Get detailed information about a specific device.

    Args:
        device_name: Name of the device (e.g., 'xcvu3p', 'xcku040')

    Returns:
        Dictionary with device information
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}

    try:
        from com.xilinx.rapidwright.device import Device

        device = Device.getDevice(device_name)
        if device is None:
            return {"error": f"Device '{device_name}' not found"}

        return {
            "status": "success",
            "name": str(device.getName()),
            "family": str(device.getFamilyType()),
            "series": str(device.getSeries()),
            "architecture": str(device.getArchitecture()),
            "rows": device.getRows(),
            "columns": device.getColumns(),
            "tile_count": device.getAllTiles().size(),
            "site_count": device.getAllSites().length
        }

    except Exception as e:
        logger.error(f"Error getting device info: {e}")
        return {"error": str(e)}


def read_checkpoint(dcp_path: str) -> Dict[str, Any]:
    """
    Read a design checkpoint (DCP) file.

    Args:
        dcp_path: Path to the DCP file

    Returns:
        Dictionary with load status and basic design info
    """
    global _current_design, _current_design_source, _current_design_source_sha256
    global _current_design_mutated, _session_valid, _checkpoint_write_blocked
    global _recurrence_family_memo
    # A failed load must not leave the previous design available to later tools.
    _current_design = None
    _current_design_source = None
    _current_design_source_sha256 = None
    _current_design_mutated = False
    _session_valid = True
    _checkpoint_write_blocked = False
    _recurrence_family_memo = None
    _reset_route_analysis_state()

    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}

    try:
        from pathlib import Path

        dcp_file = Path(dcp_path).expanduser().resolve()
        if not dcp_file.exists():
            return {"error": f"DCP file not found: {dcp_path}"}

        from com.xilinx.rapidwright.design import Design

        logger.info(f"Loading design from {dcp_file}")
        source_digest = _sha256_file(dcp_file)
        design = Design.readCheckpoint(str(dcp_file))
        if _sha256_file(dcp_file) != source_digest:
            raise RuntimeError("input checkpoint changed while it was being loaded")
        _current_design = design
        _current_design_source = str(dcp_file)
        _current_design_source_sha256 = source_digest

        return {
            "status": "success",
            "message": f"Design loaded successfully from {dcp_file.name}",
            "design_name": str(design.getName()),
            "device": str(design.getDevice().getName()),
            "part_name": str(design.getPartName()),
            "cell_count": design.getCells().size(),
            "net_count": design.getNets().size(),
            "design_sha256": source_digest,
        }

    except Exception as e:
        logger.error(f"Error loading design: {e}")
        return {"error": str(e)}


def write_checkpoint(dcp_path: str, overwrite: bool = False) -> Dict[str, Any]:
    """
    Write the current design to a checkpoint (DCP) file.

    Args:
        dcp_path: Path where the DCP file will be saved
        overwrite: If True, overwrite existing file; if False, error if file exists

    Returns:
        Dictionary with save status, bytes written, and encrypted IP info
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}

    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}

    if not _session_valid:
        return {"error": "RapidWright session is invalid after failed rollback; reload the exact input checkpoint"}
    if _checkpoint_write_blocked:
        return {"error": "checkpoint write blocked after failed relocation; explicitly reload the exact input checkpoint"}

    try:
        from com.xilinx.rapidwright.tests import CodePerfTracker
        from pathlib import Path
        import os

        output_file = Path(dcp_path).expanduser().resolve()

        # Check if file exists and overwrite is not set
        if output_file.exists() and not overwrite:
            return {
                "error": f"File '{output_file}' already exists. Set overwrite=True to replace it."
            }

        # Create parent directories if they don't exist
        output_file.parent.mkdir(parents=True, exist_ok=True)

        design = _current_design

        # Check if design contains encrypted cells before writing
        # Note: This method may not be available in all RapidWright versions
        try:
            contains_encrypted_ip = design.getNetlist().isEncrypted()
        except AttributeError:
            # Try alternative method or fall back to False
            try:
                contains_encrypted_ip = design.isNetlistEncrypted()
            except AttributeError:
                contains_encrypted_ip = False
                logger.warning("Could not determine if design contains encrypted IP")

        logger.info(f"Writing design checkpoint to {output_file}")
        design.writeCheckpoint(str(output_file))

        # Get file size
        bytes_written = output_file.stat().st_size

        # Check for accompanying Tcl script (generated for encrypted designs)
        tcl_script_path = str(output_file) + ".tcl"
        tcl_script_exists = os.path.exists(tcl_script_path)

        result = {
            "status": "success",
            "message": f"Design checkpoint saved successfully to {output_file.name}",
            "output_file": str(output_file),
            "bytes_written": bytes_written
        }

        # Add encrypted IP warning if applicable
        if contains_encrypted_ip:
            result["contains_encrypted_ip"] = True
            result["encrypted_ip_warning"] = (
                "This design contains encrypted IP. RapidWright has generated an "
                "accompanying Tcl script that is required to load this DCP in Vivado."
            )
            if tcl_script_exists:
                result["tcl_script_path"] = tcl_script_path

        logger.info(f"Design checkpoint saved: {bytes_written} bytes written")
        return result

    except Exception as e:
        logger.error(f"Error writing design checkpoint: {e}")
        return {"error": str(e)}


def insert_registered_product_modules(
    bundle_path: str,
    module_dcps: Dict[str, str],
    output_dcp: str,
) -> Dict[str, Any]:
    """Insert hash-bound OOC modules described by a recovered bundle."""
    global _current_design_mutated
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    if _current_design is None or _current_design_source_sha256 is None:
        return {"error": "No design loaded. Use read_checkpoint first."}
    if _current_design_mutated or not _session_valid or _checkpoint_write_blocked:
        return {"error": "registered product insertion requires a fresh source design"}
    try:
        from com.xilinx.rapidwright.design import Design
        from com.xilinx.rapidwright.design import Module as RWModule
        from FDAgents.registered_bridge import (
            validate_registered_bridge_bundle,
            validate_registered_product_bundle,
        )
        from FDAgents.consumer_rewrite import validate_consumer_rewrite_bundle

        bundle_file = Path(bundle_path).expanduser().resolve()
        output_file = Path(output_dcp).expanduser().resolve()
        if (
            bundle_file.is_symlink()
            or not bundle_file.is_file()
            or output_file.exists()
            or not output_file.parent.is_dir()
        ):
            raise ValueError("registered product bundle or output path is invalid")
        raw_bundle = json.loads(bundle_file.read_text(encoding="utf-8"))
        if raw_bundle.get("kind") == "generating_operator_bridge_bundle":
            bundle = validate_registered_bridge_bundle(raw_bundle)
        elif raw_bundle.get("kind") == "registered_consumer_rewrite_bundle":
            bundle = validate_consumer_rewrite_bundle(raw_bundle)
        else:
            bundle = validate_registered_product_bundle(raw_bundle)
        expected_names = {plan["module_name"] for plan in bundle["plans"]}
        if not isinstance(module_dcps, dict) or set(module_dcps) != expected_names:
            raise ValueError("registered product module checkpoint map does not match")
        modules = {}
        for name, raw_path in module_dcps.items():
            path = Path(raw_path).expanduser().resolve()
            if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
                raise ValueError("registered product module checkpoint is unavailable")
            module_design = Design.readCheckpoint(str(path))
            unresolved_ports = []
            module_netlist = module_design.getNetlist()
            for library in _as_list(module_netlist.getLibraries()):
                for cell in _as_list(library.getCells()):
                    for cell_inst in _as_list(cell.getCellInsts()):
                        for port_ordinal, port_inst in enumerate(
                            _as_list(cell_inst.getPortInsts())
                        ):
                            if port_inst.getPort() is None:
                                unresolved_ports.append({
                                    "cell": str(cell.getName()),
                                    "instance": str(cell_inst.getName()),
                                    "port_ordinal": port_ordinal,
                                })
            if unresolved_ports:
                raise RuntimeError(
                    "registered product module has unresolved EDIF ports: "
                    + json.dumps(unresolved_ports[:8], sort_keys=True)
                )
            modules[name] = RWModule(module_design, False)
        for item in bundle["instances"]:
            instance = _current_design.createModuleInst(
                item["name"], modules[item["module_name"]]
            )
            if instance is None:
                raise RuntimeError("RapidWright failed to insert registered product module")
            instance.unplace()
        _current_design.getNetlist().resetParentNetMap()
        _current_design.writeCheckpoint(str(output_file))
        if not output_file.is_file() or output_file.stat().st_size <= 0:
            raise RuntimeError("registered product insertion output is missing")
        _current_design_mutated = True
        return {
            "status": "success",
            "source_dcp_sha256": _current_design_source_sha256,
            "candidate_dcp_sha256": _sha256_file(output_file),
            "instance_count": len(bundle["instances"]),
            "module_count": len(bundle["plans"]),
        }
    except Exception as exc:
        logger.error("Registered product insertion failed: %s", exc)
        stacktrace = getattr(exc, "stacktrace", None)
        if callable(stacktrace):
            try:
                logger.error("Registered product Java stacktrace:\n%s", stacktrace())
            except Exception:
                pass
        return {"error": str(exc)}


def digest_successor_contract_region(
    boundary_net_names: list[str],
    emitted_instance_names: list[str] | None = None,
) -> Dict[str, Any]:
    """Digest route-invariant logical boundaries and an emitted hierarchy.

    This is a read-only verifier for registered-arithmetic successor
    certificates.  Boundary identity is the canonical parent net plus its full
    leaf-port set; emitted identity is relative physical cell name and primitive
    type below each exact emitted instance.  Placement and PIPs are deliberately
    excluded so ordinary route variance cannot invalidate a semantic contract.
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    if _current_design is None or _current_design_source_sha256 is None:
        return {"error": "No design loaded. Use read_checkpoint first."}
    try:
        names = list(boundary_net_names or [])
        prefixes = list(emitted_instance_names or [])
        if (
            not 1 <= len(names) <= 4096
            or len(set(names)) != len(names)
            or any(not isinstance(name, str) or not name or len(name) > 2048 for name in names)
            or len(prefixes) > 256
            or len(set(prefixes)) != len(prefixes)
            or any(not isinstance(name, str) or not name or len(name) > 1024 for name in prefixes)
        ):
            raise ValueError("successor contract region request is malformed")

        netlist = _current_design.getNetlist()
        boundary = []
        missing_nets = []
        for name in sorted(names):
            logical = netlist.getHierNetFromName(name)
            if logical is None:
                missing_nets.append(name)
                continue
            parent = netlist.getParentNet(logical) or logical
            source_ports = sorted(
                _hier_pin_name(pin)
                for pin in _as_list(parent.getLeafHierPortInsts(True, False, True))
            )
            if not source_ports:
                raise ValueError(f"successor boundary net has no source port: {name}")
            boundary.append({
                "requested_net": name,
                "canonical_parent_net": str(parent.getHierarchicalNetName()),
                "source_ports": source_ports,
            })
        if missing_nets:
            raise ValueError(
                "successor boundary nets are absent: "
                + json.dumps(missing_nets[:8], sort_keys=True)
            )

        emitted = []
        missing_instances = []
        physical_cells = _as_list(_current_design.getCells())
        for prefix in sorted(prefixes):
            rows = sorted(
                (
                    str(cell.getName())[len(prefix):].lstrip("/"),
                    str(cell.getType()).upper(),
                )
                for cell in physical_cells
                if str(cell.getName()) == prefix
                or str(cell.getName()).startswith(prefix + "/")
            )
            normalized = [
                {"relative_name": relative, "primitive_type": primitive}
                for relative, primitive in rows
            ]
            if not normalized:
                missing_instances.append(prefix)
                continue
            emitted.append({"instance_name": prefix, "cells": normalized})
        if missing_instances:
            raise ValueError(
                "successor emitted instances are absent: "
                + json.dumps(missing_instances[:8], sort_keys=True)
            )

        boundary_digest = _data_digest(boundary)
        boundary_fact_digests = [
            {
                "requested_net": row["requested_net"],
                "fact_digest": _data_digest(row),
            }
            for row in boundary
        ]
        emitted_digest = _data_digest(emitted)
        body = {
            "schema_version": "successor-contract-region-v1",
            "status": "verified",
            "read_only": True,
            "mutation_eligible": False,
            "design_sha256": _current_design_source_sha256,
            "boundary_net_count": len(boundary),
            "boundary_digest": boundary_digest,
            "boundary_fact_digests": boundary_fact_digests,
            "emitted_instance_count": len(emitted),
            "emitted_region_digest": emitted_digest,
        }
        return {**body, "verification_digest": _data_digest(body)}
    except Exception as exc:
        return {
            "status": "rejected",
            "read_only": True,
            "mutation_eligible": False,
            "design_sha256": _current_design_source_sha256,
            "error": str(exc),
        }


def _hier_pin_name(pin) -> str:
    return str(pin)


def _canonical_upstream_fact(netlist, pin) -> dict:
    hier_net = pin.getHierarchicalNet()
    if hier_net is None:
        raise ValueError(f"missing logical net on {_hier_pin_name(pin)}")
    parent = netlist.getParentNet(hier_net)
    if parent is None:
        raise ValueError(f"canonical parent net unavailable for {_hier_pin_name(pin)}")
    sources = _as_list(parent.getLeafHierPortInsts(True, False, True))
    if len(sources) != 1:
        raise ValueError(
            f"canonical net {parent.getHierarchicalNetName()} has {len(sources)} drivers"
        )
    source = sources[0]
    source_type = source.getCellType()
    if source_type is not None and (
        not bool(source_type.isPrimitive()) or bool(source_type.isLeafCellOrBlackBox())
        and not bool(source_type.isPrimitive())
    ):
        raise ValueError("canonical upstream driver is a black box or non-primitive leaf")
    return {
        "canonical_net": str(parent.getHierarchicalNetName()),
        "driver_pin": _hier_pin_name(source),
    }


def _instance_property(inst, key: str, expected_bits: int) -> str:
    prop = inst.getProperty(key)
    if prop is None:
        raise ValueError(f"missing {key} property")
    return _parse_verilog_init(str(prop.getValue()), expected_bits)


def _source_cell_facts(design, source_pin) -> dict:
    netlist = design.getNetlist()
    cell_type = source_pin.getCellType()
    if cell_type is None or not bool(cell_type.isPrimitive()):
        raise ValueError("source is not a supported primitive")
    inst = source_pin.getPortInst().getCellInst()
    primitive = str(cell_type.getName()).upper()
    cell_name = str(source_pin.getFullHierarchicalInstName())
    physical = design.getCell(cell_name)
    if physical is None or not bool(physical.isPlaced()):
        raise ValueError("source primitive is not placed")
    if bool(physical.isRoutethru()):
        raise ValueError("source primitive is a routethru")

    if primitive in _FF_PRIMITIVES:
        if str(source_pin.getPortInst().getName()).upper() != "Q":
            raise ValueError("FF source pin is not Q")
        inputs = {}
        inversions = {}
        for pin_name in _FF_PRIMITIVES[primitive]:
            port = inst.getPortInst(pin_name)
            if port is None or port.getNet() is None:
                raise ValueError(f"FF required pin {pin_name} is missing or unconnected")
            hier = netlist.getHierPortInstFromName(f"{cell_name}/{pin_name}")
            if hier is None or not bool(hier.isInput()):
                raise ValueError(f"FF required pin {pin_name} cannot be resolved")
            fact = _canonical_upstream_fact(netlist, hier)
            inputs[pin_name] = fact
            inversions[pin_name] = _canonical_property_bool(
                inst, f"IS_{pin_name}_INVERTED"
            )
        facts = {
            "kind": "ff",
            "primitive": primitive,
            "init": _instance_property(inst, "INIT", 1),
            "inputs": inputs,
            "inversions": inversions,
            "cycle_alignment": "same_initialized_single_ff_stage",
            "output_pin": "Q",
        }
    else:
        match = re.fullmatch(r"LUT([1-6])", primitive)
        if match is None:
            raise ValueError(f"unsupported primitive {primitive}")
        size = int(match.group(1))
        if str(source_pin.getPortInst().getName()).upper() != "O":
            raise ValueError("LUT source pin is not O")
        inputs = {}
        for index in range(size):
            pin_name = f"I{index}"
            port = inst.getPortInst(pin_name)
            if port is None or port.getNet() is None:
                raise ValueError(f"LUT required pin {pin_name} is missing or unconnected")
            hier = netlist.getHierPortInstFromName(f"{cell_name}/{pin_name}")
            if hier is None or not bool(hier.isInput()):
                raise ValueError(f"LUT required pin {pin_name} cannot be resolved")
            fact = _canonical_upstream_fact(netlist, hier)
            fact["inverted"] = _canonical_property_bool(
                inst, f"IS_{pin_name}_INVERTED"
            )
            inputs[pin_name] = fact
        init = _instance_property(inst, "INIT", 1 << size)
        facts = {
            "kind": "lut",
            "primitive": primitive,
            "init": init,
            "truth_table_hex": init.split("'h", 1)[1],
            "inputs": inputs,
            "output_pin": "O",
        }
    facts["cell"] = cell_name
    facts["signature_digest"] = _data_digest({
        key: value for key, value in facts.items() if key not in {"cell", "signature_digest"}
    })
    return facts


def _physical_distance(left_cell, right_cell) -> int:
    left = left_cell.getTile()
    right = right_cell.getTile()
    if left is None or right is None:
        raise ValueError("placed cell tile unavailable")
    return int(left.getManhattanDistance(right))


def _netlist_is_mechanically_unfolded(netlist) -> tuple[bool, str]:
    """Verify ECOTools' unfolded-netlist precondition without trusting metadata."""
    queue = [netlist.getTopHierCellInst()]
    seen_nonprimitive_types = set()
    visited = 0
    while queue:
        current = queue.pop()
        visited += 1
        if visited > _MAX_EQUIVALENT_SOURCE_LEAVES:
            return False, "hierarchy traversal bound exceeded"
        current_type = current.getCellType()
        if not bool(current_type.isPrimitive()):
            try:
                from java.lang import System as JavaSystem
                identity = int(JavaSystem.identityHashCode(current_type))
            except ImportError:
                identity = id(current_type)
            if identity in seen_nonprimitive_types:
                return False, "non-primitive EDIF cell definition is instantiated more than once"
            seen_nonprimitive_types.add(identity)
        for child in _as_list(current_type.getCellInsts()):
            child_type = child.getCellType()
            if bool(child_type.isPrimitive()):
                continue
            if bool(child_type.isLeafCellOrBlackBox()):
                return False, "black box encountered while checking unfolded hierarchy"
            queue.append(current.getChild(child))
    return True, "all non-primitive EDIF cell definitions are single-instantiated"


def _candidate_output_pin(netlist, hier_cell, output_pin: str):
    name = str(hier_cell.getFullHierarchicalInstName())
    return netlist.getHierPortInstFromName(f"{name}/{output_pin}")


def _certificate_core(
    design,
    sink_pin,
    reference_pin,
    candidate_pin,
    min_distance: int,
) -> tuple[dict, Optional[dict]]:
    netlist = design.getNetlist()
    reference_facts = _source_cell_facts(design, reference_pin)
    candidate_facts = _source_cell_facts(design, candidate_pin)
    proved, equivalence = _prove_equivalent_source_facts(reference_facts, candidate_facts)
    if not proved:
        return {}, equivalence
    sink_name = _hier_pin_name(sink_pin)
    sink_cell = sink_pin.getPhysicalCell(design)
    reference_cell = reference_pin.getPhysicalCell(design)
    candidate_cell = candidate_pin.getPhysicalCell(design)
    if sink_cell is None or reference_cell is None or candidate_cell is None:
        return {}, {"code": "physical_cell_missing"}
    sink_type = sink_pin.getCellType()
    if sink_type is None or not bool(sink_type.isPrimitive()):
        return {}, {"code": "sink_blackbox_or_nonprimitive_leaf"}
    if bool(sink_cell.isRoutethru()):
        return {}, {"code": "sink_routethru_forbidden"}
    current_distance = _physical_distance(reference_cell, sink_cell)
    candidate_distance = _physical_distance(candidate_cell, sink_cell)
    improvement = current_distance - candidate_distance
    if improvement < min_distance:
        return {}, {
            "code": "distance_improvement_below_minimum",
            "distance_improvement_tiles": improvement,
        }
    current_parent = netlist.getParentNet(reference_pin.getHierarchicalNet())
    candidate_parent = netlist.getParentNet(candidate_pin.getHierarchicalNet())
    if current_parent is None or candidate_parent is None or (
        str(current_parent.getHierarchicalNetName())
        == str(candidate_parent.getHierarchicalNetName())
    ):
        return {}, {"code": "source_output_net_missing_or_identical"}
    for role, parent, expected in (
        ("reference", current_parent, reference_pin),
        ("candidate", candidate_parent, candidate_pin),
    ):
        drivers = _as_list(parent.getLeafHierPortInsts(True, False, True))
        if len(drivers) != 1 or _hier_pin_name(drivers[0]) != _hier_pin_name(expected):
            return {}, {"code": f"{role}_output_multi_driver_or_ambiguous"}
    routed_sinks = _as_list(sink_pin.getAllRoutedSitePinInsts(design))
    if len(routed_sinks) != 1 or bool(routed_sinks[0].isOutPin()):
        return {}, {"code": "sink_physical_mapping_missing_or_ambiguous"}
    current_physical = sink_pin.getRoutedPhysicalNet(design)
    candidate_physical = candidate_pin.getRoutedPhysicalNet(design)
    if current_physical is None or candidate_physical is None:
        return {}, {"code": "source_physical_net_missing"}
    if current_physical.isStaticNet() or candidate_physical.isStaticNet():
        return {}, {"code": "static_net_forbidden"}
    unfolded, unfolded_reason = _netlist_is_mechanically_unfolded(netlist)
    proof_facts = {
        "reference": reference_facts,
        "candidate": candidate_facts,
        "equivalence": equivalence,
        "sink_pin": sink_name,
        "current_logical_net": str(current_parent.getHierarchicalNetName()),
        "candidate_logical_net": str(candidate_parent.getHierarchicalNetName()),
        "current_physical_net": str(current_physical.getName()),
        "candidate_physical_net": str(candidate_physical.getName()),
        "unrouted_sink_site_pin": _site_pin_key(routed_sinks[0]),
        "distance": {
            "current_tiles": current_distance,
            "candidate_tiles": candidate_distance,
            "improvement_tiles": improvement,
            "minimum_improvement_tiles": min_distance,
        },
        "mutation_preconditions": {
            "mechanically_unfolded": unfolded,
            "reason": unfolded_reason,
            "exact_logical_sink_pin": True,
            "single_physical_sink_mapping": True,
        },
        "excluded_reasoning": [
            "cell names", "current values", "sequential cone inference",
            "DSP/BRAM/CARRY equivalence",
        ],
    }
    return proof_facts, None


def _issue_equivalent_source_certificate(proof_facts: dict) -> dict:
    payload = {
        "proof_version": _EQUIVALENT_SOURCE_PROOF_VERSION,
        "status": "proved",
        "proof_type": "exact_source_equivalence",
        "design_sha256": _current_design_source_sha256,
        "session_epoch": _rapidwright_session_epoch,
        "source": {
            "current": proof_facts["reference"]["cell"],
            "proven_equivalent": proof_facts["candidate"]["cell"],
        },
        "sink": proof_facts["sink_pin"],
        "canonical_signature_digest": _data_digest({
            "reference": proof_facts["reference"]["signature_digest"],
            "candidate": proof_facts["candidate"]["signature_digest"],
            "equivalence": proof_facts["equivalence"],
        }),
        "distance_improvement_tiles": proof_facts["distance"]["improvement_tiles"],
        "proof_facts": proof_facts,
    }
    payload["certificate_id"] = _data_digest(payload)
    _equivalent_source_certificates[payload["certificate_id"]] = json.loads(
        _canonical_json(payload)
    )
    return payload


def _critical_sink_pins(design, critical_paths_data: list) -> tuple[list, list[dict]]:
    _validate_route_analysis_input(None, critical_paths_data, 1)
    netlist = design.getNetlist()
    sinks = []
    failures = []
    seen = set()
    for path_index, path in enumerate(critical_paths_data):
        for pin_name in path:
            pin = netlist.getHierPortInstFromName(pin_name)
            if pin is None:
                failures.append({"path_index": path_index, "sink": pin_name, "reason": "logical_pin_not_found"})
                continue
            if not bool(pin.isInput()) or pin.getCellType() is None:
                continue
            key = _hier_pin_name(pin)
            if key not in seen:
                seen.add(key)
                sinks.append(pin)
    return sinks, failures


def find_proven_equivalent_sources(
    critical_paths_data: list,
    max_candidates: int = 4,
    min_distance: int = 1,
) -> Dict[str, Any]:
    """Read-only exact FF/LUT source-equivalence discovery from inline path pins."""
    global _equivalent_source_proof_calls
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    if _current_design is None or not _session_valid or _current_design_mutated:
        return {"error": "an unmodified exact checkpoint must be freshly loaded"}
    if _equivalent_source_proof_calls >= _MAX_EQUIVALENT_SOURCE_PROOF_CALLS:
        return {"error": "equivalent-source proof call limit exhausted"}
    _equivalent_source_proof_calls += 1
    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) or not 1 <= max_candidates <= _MAX_EQUIVALENT_SOURCE_CANDIDATES:
        return {"error": f"max_candidates must be 1..{_MAX_EQUIVALENT_SOURCE_CANDIDATES}"}
    if isinstance(min_distance, bool) or not isinstance(min_distance, int) or not 1 <= min_distance <= 256:
        return {"error": "min_distance must be an integer in 1..256"}
    if not _current_design_source_sha256 or _sha256_file(_current_design_source) != _current_design_source_sha256:
        return {"error": "exact input checkpoint identity changed"}
    started = time.monotonic()
    failures = []
    certificates = []
    try:
        design = _current_design
        netlist = design.getNetlist()
        sinks, input_failures = _critical_sink_pins(design, critical_paths_data)
        failures.extend(input_failures)
        leaves = _as_list(netlist.getAllLeafHierCellInstances(False))
        if len(leaves) > _MAX_EQUIVALENT_SOURCE_LEAVES:
            raise ValueError("primitive leaf traversal bound exceeded")
        for sink in sinks:
            if len(certificates) >= max_candidates:
                break
            try:
                parent = netlist.getParentNet(sink.getHierarchicalNet())
                sources = _as_list(parent.getLeafHierPortInsts(True, False, True)) if parent is not None else []
                if len(sources) != 1:
                    raise ValueError(f"sink net has {len(sources)} drivers")
                reference = sources[0]
                reference_facts = _source_cell_facts(design, reference)
            except Exception as exc:
                failures.append({"sink": _hier_pin_name(sink), "reason": str(exc)})
                continue
            output_pin = reference_facts["output_pin"]
            for leaf in leaves:
                if len(certificates) >= max_candidates:
                    break
                candidate = _candidate_output_pin(netlist, leaf, output_pin)
                if candidate is None or candidate.equals(reference):
                    continue
                try:
                    facts, failure = _certificate_core(
                        design, sink, reference, candidate, min_distance
                    )
                    if failure is not None:
                        failures.append({
                            "sink": _hier_pin_name(sink),
                            "candidate_source": str(leaf.getFullHierarchicalInstName()),
                            "reason": failure,
                        })
                        continue
                    certificates.append(_issue_equivalent_source_certificate(facts))
                except Exception as exc:
                    failures.append({
                        "sink": _hier_pin_name(sink),
                        "candidate_source": str(leaf.getFullHierarchicalInstName()),
                        "reason": str(exc),
                    })
        certificates.sort(key=lambda item: (-item["distance_improvement_tiles"], item["certificate_id"]))
        return {
            "status": "success",
            "read_only": True,
            "proof_version": _EQUIVALENT_SOURCE_PROOF_VERSION,
            "design_sha256": _current_design_source_sha256,
            "session_epoch": _rapidwright_session_epoch,
            "candidate_count": len(certificates),
            "certificates": certificates,
            "fail_reasons": failures[:256],
            "fail_reasons_truncated": len(failures) > 256,
            "proof_call": _equivalent_source_proof_calls,
            "proof_call_limit": _MAX_EQUIVALENT_SOURCE_PROOF_CALLS,
            "runtime_s": round(time.monotonic() - started, 6),
        }
    except Exception as exc:
        return {
            "status": "error", "error": str(exc), "certificates": [],
            "fail_reasons": failures[:256], "read_only": True,
        }


def _logical_sink_digest(netlist, sink) -> str:
    parent = netlist.getParentNet(sink.getHierarchicalNet())
    facts = {
        "sink": _hier_pin_name(sink),
        "canonical_net": str(parent.getHierarchicalNetName()) if parent is not None else None,
        "ports": sorted(_hier_pin_name(pin) for pin in _as_list(parent.getLeafHierPortInsts(True, True, True))) if parent is not None else [],
    }
    return _data_digest(facts)


def _physical_net_digest(net) -> str:
    return _data_digest({
        "net": str(net.getName()),
        "pins": sorted(_site_pin_key(pin) for pin in _as_list(net.getPins())),
        "pips": sorted(_pip_key(pip) for pip in _as_list(net.getPIPs())),
    })


def _java_map_list(key, value):
    try:
        from java.util import ArrayList, HashMap
        values = ArrayList()
        values.add(value)
        result = HashMap()
        result.put(key, values)
        return result
    except ImportError:
        return {key: [value]}


def _rewire_sink_to_proven_source_for_design(design, certificate: dict, design_tools, eco_tools) -> dict:
    stored = _equivalent_source_certificates.get(certificate.get("certificate_id"))
    if stored is None or _canonical_json(stored) != _canonical_json(certificate):
        raise ValueError("certificate is missing, stale, incomplete, or modified")
    if certificate.get("proof_version") != _EQUIVALENT_SOURCE_PROOF_VERSION:
        raise ValueError("proof version mismatch")
    if certificate.get("design_sha256") != _current_design_source_sha256 or certificate.get("session_epoch") != _rapidwright_session_epoch:
        raise ValueError("certificate design/session identity mismatch")
    facts = certificate["proof_facts"]
    if not facts.get("mutation_preconditions", {}).get("mechanically_unfolded"):
        raise ValueError("ECOTools mutation disabled: unfolded EDIF was not mechanically proven")
    netlist = design.getNetlist()
    sink = netlist.getHierPortInstFromName(certificate["sink"])
    reference = netlist.getHierPortInstFromName(
        f"{certificate['source']['current']}/{facts['reference']['output_pin']}"
    )
    candidate = netlist.getHierPortInstFromName(
        f"{certificate['source']['proven_equivalent']}/{facts['candidate']['output_pin']}"
    )
    if sink is None or reference is None or candidate is None:
        raise ValueError("certificate pin identity no longer resolves")
    fresh_facts, failure = _certificate_core(
        design,
        sink,
        reference,
        candidate,
        int(facts["distance"]["minimum_improvement_tiles"]),
    )
    if failure is not None or _canonical_json(fresh_facts) != _canonical_json(facts):
        raise ValueError(f"fresh exact-seed reproof mismatch: {failure or 'facts changed'}")

    old_physical = sink.getRoutedPhysicalNet(design)
    new_physical = candidate.getRoutedPhysicalNet(design)
    sink_site_pins = _as_list(sink.getAllRoutedSitePinInsts(design))
    if old_physical is None or new_physical is None or len(sink_site_pins) != 1:
        raise ValueError("fresh physical sink mapping is not unique")
    before_logical = _logical_sink_digest(netlist, sink)
    before_physical = {
        str(old_physical.getName()): _physical_net_digest(old_physical),
        str(new_physical.getName()): _physical_net_digest(new_physical),
    }
    old_preserved = {
        _site_pin_key(pin): _pip_digest(design_tools.getConnectionPIPs(pin))
        for pin in _as_list(old_physical.getSinkPins())
        if _site_pin_key(pin) != _site_pin_key(sink_site_pins[0])
    }
    design_tools.unroutePins(old_physical, _java_pin_collection(sink_site_pins))
    eco_tools.disconnectNet(design, _java_pin_collection([sink]))
    candidate_parent = netlist.getParentNet(candidate.getHierarchicalNet())
    eco_tools.connectNet(design, _java_map_list(candidate_parent, sink), None)
    netlist.resetParentNetMap()

    rebound = netlist.getHierPortInstFromName(certificate["sink"])
    rebound_parent = netlist.getParentNet(rebound.getHierarchicalNet()) if rebound is not None else None
    if rebound_parent is None or str(rebound_parent.getHierarchicalNetName()) != facts["candidate_logical_net"]:
        raise RuntimeError("logical reconnect postcondition failed")
    rebound_sites = _as_list(rebound.getAllRoutedSitePinInsts(design))
    if len(rebound_sites) != 1 or bool(rebound_sites[0].isRouted()):
        raise RuntimeError("new sink branch is not uniquely unrouted")
    for pin in _as_list(old_physical.getSinkPins()):
        key = _site_pin_key(pin)
        if key in old_preserved and (
            not bool(pin.isRouted())
            or _pip_digest(design_tools.getConnectionPIPs(pin)) != old_preserved[key]
        ):
            raise RuntimeError(f"old-net preserved sink changed: {key}")
    after_physical = {
        str(old_physical.getName()): _physical_net_digest(old_physical),
        str(new_physical.getName()): _physical_net_digest(new_physical),
    }
    return {
        "status": "success",
        "logical_before_digest": before_logical,
        "logical_after_digest": _logical_sink_digest(netlist, rebound),
        "physical_before_digest": _data_digest(before_physical),
        "physical_after_digest": _data_digest(after_physical),
        "physical_net_digests_before": before_physical,
        "physical_net_digests_after": after_physical,
        "affected_nets": sorted({str(old_physical.getName()), str(new_physical.getName())}),
        "nets_requiring_route": [str(new_physical.getName())],
        "unrouted_sink": {"physical_net": str(new_physical.getName()), "sink_pin": _site_pin_key(rebound_sites[0])},
        "proof_certificate": certificate,
        "fresh_reproof": True,
        "selective_unroute": True,
        "checkpoint_written": False,
    }


def _load_eco_tools():
    from com.xilinx.rapidwright.eco import ECOTools
    return ECOTools


def rewire_sink_to_proven_source(certificate: dict) -> Dict[str, Any]:
    """Internal, complete-certificate-only mutation; intentionally not an MCP probe."""
    global _current_design_mutated, _session_valid, _checkpoint_write_blocked
    global _equivalent_source_mutation_calls
    if not _initialized or _current_design is None:
        return {"error": "No exact checkpoint design loaded"}
    if not _session_valid or _current_design_mutated:
        return {"error": "mutation requires one fresh, unmodified exact-seed load"}
    if _equivalent_source_mutation_calls >= _MAX_EQUIVALENT_SOURCE_MUTATION_CALLS:
        return {"error": "equivalent-source mutation call limit exhausted"}
    _equivalent_source_mutation_calls += 1
    try:
        if not isinstance(certificate, dict) or set(certificate) != {
            "proof_version", "status", "proof_type", "design_sha256", "session_epoch",
            "source", "sink", "canonical_signature_digest", "distance_improvement_tiles",
            "proof_facts", "certificate_id",
        }:
            raise ValueError("mutation requires the complete exact analysis certificate")
        if _sha256_file(_current_design_source) != _current_design_source_sha256:
            raise RuntimeError("exact input checkpoint identity changed")
        started = time.monotonic()
        result = _rewire_sink_to_proven_source_for_design(
            _current_design, certificate, _load_design_tools(), _load_eco_tools()
        )
        _current_design_mutated = True
        _checkpoint_write_blocked = False
        _equivalent_source_certificates.clear()
        result["runtime_s"] = round(time.monotonic() - started, 6)
        return result
    except Exception as exc:
        rolled_back = _reload_current_design_from_seed()
        _checkpoint_write_blocked = True
        return {
            "status": "error", "error": str(exc), "rolled_back": rolled_back,
            "session_valid": bool(_session_valid), "checkpoint_written": False,
            "write_blocked_until_explicit_reload": True,
        }


def analyze_route_connections(
    connections: Optional[list] = None,
    critical_paths_data: Optional[list] = None,
    max_connections: int = 16,
) -> Dict[str, Any]:
    """Read-only, sink-specific route-tree analysis with fail-closed certificates."""
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}
    if not _session_valid:
        return {"error": "RapidWright session is invalid; reload the exact input checkpoint"}
    if _current_design_mutated:
        return {"error": "RapidWright design was mutated; reload the exact input checkpoint"}
    try:
        started = time.monotonic()
        result = _analyze_route_connections_for_design(
            _current_design,
            _load_design_tools(),
            connections=connections,
            critical_paths_data=critical_paths_data,
            max_connections=max_connections,
        )
        result["runtime_s"] = round(time.monotonic() - started, 6)
        return result
    except Exception as exc:
        logger.error("Route connection analysis failed: %s", exc)
        return {"status": "error", "error": str(exc), "connections": []}


def operator_mining(
    critical_paths_data: Optional[list] = None,
    min_family_size: int = 3,
    max_families: int = 16,
    max_motif_cells: int = 8,
) -> Dict[str, Any]:
    """Read-only repeated-operator and transport hypothesis mining."""
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}
    if not _session_valid:
        return {"error": "RapidWright session is invalid; reload the exact input checkpoint"}
    if _current_design_mutated:
        return {"error": "RapidWright design was mutated; reload the exact input checkpoint"}
    try:
        try:
            from .operator_mining import mine_operator_structures
        except ImportError:
            from operator_mining import mine_operator_structures
        started = time.monotonic()
        result = mine_operator_structures(
            _current_design,
            critical_paths_data=critical_paths_data,
            min_family_size=min_family_size,
            max_families=max_families,
            max_motif_cells=max_motif_cells,
            design_sha256=_current_design_source_sha256,
        )
        result["runtime_s"] = round(time.monotonic() - started, 6)
        return result
    except Exception as exc:
        logger.error("Operator mining failed: %s", exc)
        return {"status": "error", "error": str(exc)}


def recover_recurrence_contract(
    critical_paths_data: list,
    candidate_id: str,
) -> Dict[str, Any]:
    """Freshly rediscover and exactly recover one mined recurrence boundary."""
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}
    if not _session_valid:
        return {"error": "RapidWright session is invalid; reload the exact input checkpoint"}
    if not isinstance(candidate_id, str) or re.fullmatch(
        r"recurrence:[0-9a-f]{20}", candidate_id
    ) is None:
        return {"error": "candidate_id must be a recurrence hypothesis ID"}
    try:
        try:
            from .operator_facts import (
                complete_recurrence_rewrite_facts,
                recover_dsp_recurrence_contract,
                recover_recurrence_rewrite_plan,
            )
            from .operator_mining import mine_operator_structures
        except ImportError:
            from operator_facts import (
                complete_recurrence_rewrite_facts,
                recover_dsp_recurrence_contract,
                recover_recurrence_rewrite_plan,
            )
            from operator_mining import mine_operator_structures
        started = time.monotonic()
        mining = mine_operator_structures(
            _current_design,
            critical_paths_data=critical_paths_data,
            min_family_size=2,
            max_families=32,
            max_motif_cells=8,
            design_sha256=_current_design_source_sha256,
        )
        matches = [
            item for item in mining.get("recurrence_boundary_families", [])
            if item.get("candidate_id") == candidate_id
        ]
        if len(matches) != 1:
            raise ValueError("candidate is absent, stale, truncated, or ambiguous")
        boundary = recover_dsp_recurrence_contract(
            _current_design,
            str(matches[0]["sink_dsp"]),
            design_sha256=_current_design_source_sha256,
        )
        rewrite_plan = recover_recurrence_rewrite_plan(_current_design, boundary)
        result = complete_recurrence_rewrite_facts(boundary, rewrite_plan)
        result["candidate_id"] = candidate_id
        result["fresh_rediscovery"] = True
        result["runtime_s"] = round(time.monotonic() - started, 6)
        return result
    except Exception as exc:
        logger.error("Recurrence contract recovery failed: %s", exc)
        return {"status": "error", "error": str(exc)}


def recover_sequential_operator_contract(
    critical_paths_data: list,
    candidate_id: str,
) -> Dict[str, Any]:
    """Freshly recover one generic sequential write boundary from the live DCP."""
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}
    if not _session_valid or _current_design_mutated:
        return {"error": "a valid unmodified RapidWright design is required"}
    if not isinstance(candidate_id, str) or re.fullmatch(
        r"sequential:[0-9a-f]{20}", candidate_id
    ) is None:
        return {"error": "candidate_id must be a sequential hypothesis ID"}
    try:
        try:
            from .sequential_contract import (
                recover_sequential_operator_contract as recover_contract,
            )
        except ImportError:
            from sequential_contract import (
                recover_sequential_operator_contract as recover_contract,
            )
        started = time.monotonic()
        result = recover_contract(
            _current_design,
            critical_paths_data=critical_paths_data,
            candidate_id=candidate_id,
            design_sha256=_current_design_source_sha256,
        )
        result["runtime_s"] = round(time.monotonic() - started, 6)
        return result
    except Exception as exc:
        logger.error("Sequential operator contract recovery failed: %s", exc)
        return {"status": "error", "error": str(exc)}


def recover_sequential_operator_replicas(
    critical_paths_data: list,
    candidate_id: str,
    max_replicas: int = 64,
) -> Dict[str, Any]:
    """Recover full-word structural replicas for an exact live family."""
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}
    if not _session_valid or _current_design_mutated:
        return {"error": "a valid unmodified RapidWright design is required"}
    if not isinstance(candidate_id, str) or re.fullmatch(
        r"sequential:[0-9a-f]{20}", candidate_id
    ) is None:
        return {"error": "candidate_id must be a sequential hypothesis ID"}
    if isinstance(max_replicas, bool) or not isinstance(max_replicas, int) or not 1 <= max_replicas <= 64:
        return {"error": "max_replicas must be in 1..64"}
    try:
        try:
            from .sequential_contract import (
                recover_sequential_operator_replicas as recover_replicas,
            )
        except ImportError:
            from sequential_contract import (
                recover_sequential_operator_replicas as recover_replicas,
            )
        started = time.monotonic()
        result = recover_replicas(
            _current_design,
            critical_paths_data=critical_paths_data,
            candidate_id=candidate_id,
            design_sha256=_current_design_source_sha256,
            max_replicas=max_replicas,
        )
        result["runtime_s"] = round(time.monotonic() - started, 6)
        return result
    except Exception as exc:
        logger.error("Sequential operator replica recovery failed: %s", exc)
        return {"status": "error", "error": str(exc)}


def recover_consumer_driven_operator_contract(
    critical_paths_data: list,
    candidate_id: str,
) -> Dict[str, Any]:
    """Recover an anonymous consumer/operator contract from the live DCP."""
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}
    if not _session_valid:
        return {"error": "RapidWright session is invalid; reload the exact input checkpoint"}
    if not isinstance(candidate_id, str) or re.fullmatch(
        r"sequential:[0-9a-f]{20}", candidate_id
    ) is None:
        return {"error": "candidate_id must be a sequential hypothesis ID"}
    try:
        try:
            from .consumer_operator_contract import (
                recover_consumer_driven_operator_contract as recover_contract,
            )
        except ImportError:
            from consumer_operator_contract import (
                recover_consumer_driven_operator_contract as recover_contract,
            )
        started = time.monotonic()
        result = recover_contract(
            _current_design,
            critical_paths_data=critical_paths_data,
            candidate_id=candidate_id,
            design_sha256=_current_design_source_sha256,
        )
        result["runtime_s"] = round(time.monotonic() - started, 6)
        return result
    except Exception as exc:
        logger.error("Consumer-driven operator contract recovery failed: %s", exc)
        return {"status": "error", "error": str(exc)}


def _write_registered_dead_state_manifest(
    path: Path,
    *,
    source_dcp_sha256: str,
    private_manifest: dict[str, Any],
) -> None:
    """Atomically retain exact mutation identities outside the LLM payload."""
    if (
        not path.is_absolute()
        or path.suffix.lower() != ".tsv"
        or path.parent.is_symlink()
        or not path.parent.is_dir()
        or path.is_symlink()
    ):
        raise ValueError("registered dead-state manifest path is unsafe")
    digest = str(private_manifest.get("contract_digest") or "")
    target_digest = str(private_manifest.get("mutation_target_digest") or "")
    names = private_manifest.get("dead_cell_names")
    if (
        re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or re.fullmatch(r"[0-9a-f]{64}", target_digest) is None
        or not isinstance(names, list)
        or len(set(names)) != len(names)
        or any(
            not isinstance(name, str)
            or not name
            or any(char in name for char in ("\t", "\r", "\n"))
            for name in names
        )
    ):
        raise ValueError("registered dead-state private manifest is invalid")
    rows = [
        f"source_dcp_sha256\t{source_dcp_sha256}",
        f"contract_digest\t{digest}",
        f"mutation_target_digest\t{target_digest}",
        f"cell_count\t{len(names)}",
        *(f"cell\t{name}" for name in names),
    ]
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("\n".join(rows) + "\n", encoding="utf-8")
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(path)


def recover_registered_dead_state_contract(
    source_dcp_sha256: str,
    manifest_path: str,
) -> Dict[str, Any]:
    """Recover output-observable closure and retain only its private delete set."""
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    if _current_design is None or not _current_design_source:
        return {"error": "No design loaded. Use read_checkpoint first."}
    if not _session_valid:
        return {"error": "RapidWright session is invalid; reload the exact input checkpoint"}
    digest = str(source_dcp_sha256 or "").lower()
    if (
        re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or digest != _current_design_source_sha256
    ):
        return {"status": "error", "error": "registered dead-state source identity mismatch"}
    try:
        if _sha256_file(_current_design_source) != digest:
            raise ValueError("registered dead-state loaded checkpoint identity changed")
        try:
            from .registered_dead_state import (
                recover_registered_dead_state_contract as recover_contract,
            )
        except ImportError:
            from registered_dead_state import (
                recover_registered_dead_state_contract as recover_contract,
            )
        started = time.monotonic()
        recovered = recover_contract(_current_design)
        public = recovered.get("public_contract")
        private = recovered.get("private_manifest")
        if not isinstance(public, dict) or not isinstance(private, dict):
            raise ValueError("registered dead-state recovery returned no closed contract")
        summary = public.get("summary") or {}
        if (
            private.get("contract_digest") != public.get("contract_digest")
            or private.get("mutation_target_digest")
            != summary.get("mutation_target_digest")
        ):
            raise ValueError("registered dead-state public/private handoff diverged")
        _write_registered_dead_state_manifest(
            Path(str(manifest_path)),
            source_dcp_sha256=digest,
            private_manifest=private,
        )
        public["private_mutation_manifest_retained"] = True
        public["runtime_s"] = round(time.monotonic() - started, 6)
        return public
    except Exception as exc:
        logger.error("Registered dead-state recovery failed: %s", exc)
        return {"status": "error", "error": str(exc)}


def prove_control_pin_equivalence_groups(
    pin_names: list[str],
    role: str = "CE",
) -> Dict[str, Any]:
    """Prove physical control-net replicas equivalent on the loaded DCP."""
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}
    if not _session_valid or _current_design_mutated:
        return {"error": "a valid unmodified RapidWright design is required"}
    try:
        try:
            from .operator_facts import recover_control_pin_equivalence_groups
        except ImportError:
            from operator_facts import recover_control_pin_equivalence_groups
        started = time.monotonic()
        result = recover_control_pin_equivalence_groups(
            _current_design, pin_names, role=role
        )
        result["design_sha256"] = _current_design_source_sha256
        result["runtime_s"] = round(time.monotonic() - started, 6)
        return result
    except Exception as exc:
        logger.error("Control pin equivalence recovery failed: %s", exc)
        return {"status": "error", "error": str(exc)}


def _recovered_recurrence_family(recover, max_boundaries: int) -> Dict[str, Any]:
    """Recover the family for the loaded design, reusing the session's result.

    Callers mutate what they receive, so hand out an independent copy and keep
    the memo private.  A mutated design is never served from the memo: only the
    checkpoint that was loaded, identified by its own digest, is memoized.
    """
    global _recurrence_family_memo
    key = (str(_current_design_source_sha256 or ""), int(max_boundaries))
    if not _current_design_mutated and _recurrence_family_memo is not None:
        cached_key, cached = _recurrence_family_memo
        if cached_key == key and key[0]:
            return copy.deepcopy(cached)
    family = recover(
        _current_design,
        design_sha256=_current_design_source_sha256,
        max_boundaries=max_boundaries,
    )
    if not _current_design_mutated and key[0] and isinstance(family, dict):
        _recurrence_family_memo = (key, copy.deepcopy(family))
    return family


def recover_recurrence_family_contract(
    max_boundaries: int = 32,
) -> Dict[str, Any]:
    """Recover every compatible periodic boundary directly from the loaded DCP."""
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}
    if not _session_valid:
        return {"error": "RapidWright session is invalid; reload the exact input checkpoint"}
    try:
        try:
            from .operator_facts import recover_dsp_recurrence_family_contract
        except ImportError:
            from operator_facts import recover_dsp_recurrence_family_contract
        started = time.monotonic()
        result = _recovered_recurrence_family(
            recover_dsp_recurrence_family_contract, max_boundaries
        )
        result["fresh_rediscovery"] = True
        result["runtime_s"] = round(time.monotonic() - started, 6)
        return result
    except Exception as exc:
        logger.error("Recurrence family recovery failed: %s", exc)
        return {"status": "error", "error": str(exc)}


def propose_recurrence_family_placement_candidates(
    max_srl_sites: int = 32,
    max_ce_clone_sites: int = 16,
) -> Dict[str, Any]:
    """Freshly recover a family and propose bounded global slots per boundary."""
    if not _initialized or _current_design is None or not _session_valid:
        return {"status": "error", "error": "a valid loaded RapidWright design is required"}
    try:
        try:
            from .operator_facts import (
                propose_recurrence_placement_candidates as propose_candidates,
                recover_dsp_recurrence_family_contract,
            )
        except ImportError:
            from operator_facts import (
                propose_recurrence_placement_candidates as propose_candidates,
                recover_dsp_recurrence_family_contract,
            )
        started = time.monotonic()
        family = _recovered_recurrence_family(
            recover_dsp_recurrence_family_contract, 32
        )
        proposals = [
            {
                "region_sha256": boundary["region_sha256"],
                "proposal": propose_candidates(
                    _current_design,
                    boundary,
                    max_srl_sites=max_srl_sites,
                    max_ce_clone_sites=max_ce_clone_sites,
                ),
            }
            for boundary in family["boundaries"]
        ]
        return {
            "kind": "recurrence_family_placement_candidates",
            "version": 1,
            "source_dcp_sha256": _current_design_source_sha256,
            "family_contract_sha256": family["family_contract_sha256"],
            "family_id": family["family_id"],
            "proposals": proposals,
            "fresh_rediscovery": True,
            "runtime_s": round(time.monotonic() - started, 6),
        }
    except Exception as exc:
        logger.error("Recurrence family placement proposal failed: %s", exc)
        return {"status": "error", "error": str(exc)}


def propose_recurrence_placement_candidates(
    critical_paths_data: list,
    candidate_id: str,
    max_srl_sites: int = 32,
    max_ce_clone_sites: int = 16,
) -> Dict[str, Any]:
    """Freshly recover one recurrence and rank bounded global placement slots."""
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}
    if not _session_valid:
        return {"error": "RapidWright session is invalid; reload the exact input checkpoint"}
    if not isinstance(candidate_id, str) or re.fullmatch(
        r"recurrence:[0-9a-f]{20}", candidate_id
    ) is None:
        return {"error": "candidate_id must be a recurrence hypothesis ID"}
    try:
        try:
            from .operator_facts import (
                complete_recurrence_rewrite_facts,
                propose_recurrence_placement_candidates as propose_candidates,
                recover_dsp_recurrence_contract,
                recover_recurrence_rewrite_plan,
            )
            from .operator_mining import mine_operator_structures
        except ImportError:
            from operator_facts import (
                complete_recurrence_rewrite_facts,
                propose_recurrence_placement_candidates as propose_candidates,
                recover_dsp_recurrence_contract,
                recover_recurrence_rewrite_plan,
            )
            from operator_mining import mine_operator_structures
        started = time.monotonic()
        mining = mine_operator_structures(
            _current_design,
            critical_paths_data=critical_paths_data,
            min_family_size=2,
            max_families=32,
            max_motif_cells=8,
            design_sha256=_current_design_source_sha256,
        )
        matches = [
            item for item in mining.get("recurrence_boundary_families", [])
            if item.get("candidate_id") == candidate_id
        ]
        if len(matches) != 1:
            raise ValueError("candidate is absent, stale, truncated, or ambiguous")
        boundary = recover_dsp_recurrence_contract(
            _current_design,
            str(matches[0]["sink_dsp"]),
            design_sha256=_current_design_source_sha256,
        )
        rewrite_plan = recover_recurrence_rewrite_plan(_current_design, boundary)
        facts = complete_recurrence_rewrite_facts(boundary, rewrite_plan)
        result = propose_candidates(
            _current_design,
            facts,
            max_srl_sites=max_srl_sites,
            max_ce_clone_sites=max_ce_clone_sites,
        )
        result["candidate_id"] = candidate_id
        result["fresh_rediscovery"] = True
        result["runtime_s"] = round(time.monotonic() - started, 6)
        return result
    except Exception as exc:
        logger.error("Recurrence placement proposal failed: %s", exc)
        return {"status": "error", "error": str(exc)}


def recover_fixed_point_contract(
    max_candidates: int = 8,
) -> Dict[str, Any]:
    """Autonomously rediscover and exactly recover fabric fixed-point cones."""
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}
    if not _session_valid:
        return {"error": "RapidWright session is invalid; reload the exact input checkpoint"}
    if _current_design_mutated:
        return {"error": "RapidWright design was mutated; reload the exact input checkpoint"}
    try:
        try:
            from .operator_facts import recover_fixed_point_product_contract
        except ImportError:
            from operator_facts import recover_fixed_point_product_contract
        started = time.monotonic()
        result = recover_fixed_point_product_contract(
            _current_design,
            _current_design_source_sha256,
            max_candidates=max_candidates,
        )
        result["fresh_rediscovery"] = True
        result["runtime_s"] = round(time.monotonic() - started, 6)
        return result
    except Exception as exc:
        logger.error("Fixed-point contract recovery failed: %s", exc)
        return {"status": "error", "error": str(exc)}


def recover_registered_product_sum_contract(
    max_candidates: int = 16,
    preferred_output_cells: Optional[list[str]] = None,
    timeout_ms: int = 30000,
) -> Dict[str, Any]:
    """Recover exact registered product-sum contracts from the loaded DCP."""
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}
    if not _session_valid or _current_design_mutated:
        return {"error": "a valid unmodified RapidWright design is required"}
    try:
        from FDAgents.regarith_tools.dcp_validation import recover_product_sum_facts

        started = time.monotonic()
        result = recover_product_sum_facts(
            _current_design,
            _current_design_source_sha256,
            max_candidates=max_candidates,
            preferred_output_cells=preferred_output_cells or (),
            timeout_ms=timeout_ms,
        )
        result["fresh_rediscovery"] = True
        result["runtime_s"] = round(time.monotonic() - started, 6)
        return result
    except Exception as exc:
        logger.error("Registered product-sum recovery failed: %s", exc)
        return {
            "status": "error",
            "error_code": "PRODUCT_SUM_RECOVERY_TOOL_ERROR",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


def propose_fixed_point_placement_candidates(
    region_sha256: str,
    max_sites: int = 64,
) -> Dict[str, Any]:
    """Freshly re-recover one fixed-point cone and rank idle DSP48E2 slots."""
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}
    if not _session_valid:
        return {"error": "RapidWright session is invalid; reload the exact input checkpoint"}
    if _current_design_mutated:
        return {"error": "RapidWright design was mutated; reload the exact input checkpoint"}
    if not isinstance(region_sha256, str) or re.fullmatch(
        r"[0-9a-f]{64}", region_sha256
    ) is None:
        return {"error": "region_sha256 must be a lowercase SHA-256 digest"}
    try:
        try:
            from .operator_facts import (
                propose_fixed_point_placement_candidates as propose_slots,
            )
        except ImportError:
            from operator_facts import (
                propose_fixed_point_placement_candidates as propose_slots,
            )
        started = time.monotonic()
        result = propose_slots(
            _current_design,
            _current_design_source_sha256,
            region_sha256,
            max_sites=max_sites,
        )
        result["runtime_s"] = round(time.monotonic() - started, 6)
        return result
    except Exception as exc:
        logger.error("Fixed-point placement proposal failed: %s", exc)
        return {"status": "error", "error": str(exc)}


def recover_two_operand_contract(
    max_candidates: int = 8,
) -> Dict[str, Any]:
    """Autonomously rediscover and exactly recover fabric two-operand cones."""
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}
    if not _session_valid:
        return {"error": "RapidWright session is invalid; reload the exact input checkpoint"}
    if _current_design_mutated:
        return {"error": "RapidWright design was mutated; reload the exact input checkpoint"}
    try:
        try:
            from .operator_facts import (
                recover_two_operand_contract as recover_contract,
            )
        except ImportError:
            from operator_facts import (
                recover_two_operand_contract as recover_contract,
            )
        started = time.monotonic()
        result = recover_contract(
            _current_design,
            _current_design_source_sha256,
            max_candidates=max_candidates,
        )
        result["fresh_rediscovery"] = True
        result["runtime_s"] = round(time.monotonic() - started, 6)
        return result
    except Exception as exc:
        logger.error("Two-operand contract recovery failed: %s", exc)
        return {"status": "error", "error": str(exc)}


def propose_two_operand_placement_candidates(
    region_sha256: str,
    max_sites: int = 64,
) -> Dict[str, Any]:
    """Freshly re-recover one two-operand cone and rank idle DSP48E2 slots."""
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}
    if not _session_valid:
        return {"error": "RapidWright session is invalid; reload the exact input checkpoint"}
    if _current_design_mutated:
        return {"error": "RapidWright design was mutated; reload the exact input checkpoint"}
    if not isinstance(region_sha256, str) or re.fullmatch(
        r"[0-9a-f]{64}", region_sha256
    ) is None:
        return {"error": "region_sha256 must be a lowercase SHA-256 digest"}
    try:
        try:
            from .operator_facts import (
                propose_two_operand_placement_candidates as propose_slots,
            )
        except ImportError:
            from operator_facts import (
                propose_two_operand_placement_candidates as propose_slots,
            )
        started = time.monotonic()
        result = propose_slots(
            _current_design,
            _current_design_source_sha256,
            region_sha256,
            max_sites=max_sites,
        )
        result["runtime_s"] = round(time.monotonic() - started, 6)
        return result
    except Exception as exc:
        logger.error("Two-operand placement proposal failed: %s", exc)
        return {"status": "error", "error": str(exc)}


def recover_register_absorb_contract(
    max_candidates: int = 8,
) -> Dict[str, Any]:
    """Autonomously rediscover absorbable DSP48E2 input register buses."""
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}
    if not _session_valid:
        return {"error": "RapidWright session is invalid; reload the exact input checkpoint"}
    if _current_design_mutated:
        return {"error": "RapidWright design was mutated; reload the exact input checkpoint"}
    try:
        try:
            from .operator_facts import (
                recover_register_absorb_contract as recover_contract,
            )
        except ImportError:
            from operator_facts import (
                recover_register_absorb_contract as recover_contract,
            )
        started = time.monotonic()
        result = recover_contract(
            _current_design,
            _current_design_source_sha256,
            max_candidates=max_candidates,
        )
        result["fresh_rediscovery"] = True
        result["runtime_s"] = round(time.monotonic() - started, 6)
        return result
    except Exception as exc:
        logger.error("Register-absorb contract recovery failed: %s", exc)
        return {"status": "error", "error": str(exc)}


def recover_pipeline_rebalance_contract(
    max_candidates: int = 8,
) -> Dict[str, Any]:
    """Autonomously rediscover DSP48E2 AREG/BREG<->MREG rebalance boundaries."""
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}
    if not _session_valid:
        return {"error": "RapidWright session is invalid; reload the exact input checkpoint"}
    if _current_design_mutated:
        return {"error": "RapidWright design was mutated; reload the exact input checkpoint"}
    try:
        try:
            from .operator_facts import (
                recover_pipeline_rebalance_contract as recover_contract,
            )
        except ImportError:
            from operator_facts import (
                recover_pipeline_rebalance_contract as recover_contract,
            )
        started = time.monotonic()
        result = recover_contract(
            _current_design,
            _current_design_source_sha256,
            max_candidates=max_candidates,
        )
        result["fresh_rediscovery"] = True
        result["runtime_s"] = round(time.monotonic() - started, 6)
        return result
    except Exception as exc:
        logger.error("Pipeline-rebalance contract recovery failed: %s", exc)
        return {"status": "error", "error": str(exc)}


def recover_memory_primitive_contracts(
    max_candidates: int = 64,
) -> Dict[str, Any]:
    """Autonomously recover configured BRAM semantics from the exact DCP."""
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}
    if not _session_valid:
        return {"error": "RapidWright session is invalid; reload the exact input checkpoint"}
    if _current_design_mutated:
        return {"error": "RapidWright design was mutated; reload the exact input checkpoint"}
    try:
        try:
            from .operator_facts import recover_memory_primitive_contracts as recover
        except ImportError:
            from operator_facts import recover_memory_primitive_contracts as recover
        started = time.monotonic()
        result = recover(
            _current_design,
            _current_design_source_sha256,
            max_candidates=max_candidates,
        )
        result["runtime_s"] = round(time.monotonic() - started, 6)
        return result
    except Exception as exc:
        logger.error("Memory primitive contract recovery failed: %s", exc)
        return {"status": "error", "error": str(exc)}


def recover_memory_consumer_dependencies(
    max_candidates: int = 64,
) -> Dict[str, Any]:
    """Recover hash-bound BRAM-to-leaf-consumer dependencies from the exact DCP."""
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}
    if not _session_valid:
        return {"error": "RapidWright session is invalid; reload the exact input checkpoint"}
    if _current_design_mutated:
        return {"error": "RapidWright design was mutated; reload the exact input checkpoint"}
    try:
        try:
            from .operator_facts import recover_memory_consumer_dependencies as recover
        except ImportError:
            from operator_facts import recover_memory_consumer_dependencies as recover
        started = time.monotonic()
        result = recover(
            _current_design,
            _current_design_source_sha256,
            max_candidates=max_candidates,
        )
        result["runtime_s"] = round(time.monotonic() - started, 6)
        return result
    except Exception as exc:
        logger.error("Memory consumer dependency recovery failed: %s", exc)
        return {"status": "error", "error": str(exc)}


def _java_pin_collection(pins):
    try:
        from java.util import ArrayList

        result = ArrayList()
        for pin in pins:
            result.add(pin)
        return result
    except ImportError:
        return list(pins)


def _load_design_tools():
    from com.xilinx.rapidwright.design import DesignTools

    return DesignTools


def _validate_mutation_targets(targets) -> None:
    if not isinstance(targets, list) or not 1 <= len(targets) <= _MAX_SELECTIVE_UNROUTE_CONNECTIONS:
        raise ValueError(
            f"connections must contain 1..{_MAX_SELECTIVE_UNROUTE_CONNECTIONS} analysis mutation targets"
        )
    required = {
        "physical_net", "sink_pin", "before_net_pip_digest",
        "before_sink_pip_digest", "certificate",
    }
    for index, target in enumerate(targets):
        if not isinstance(target, dict) or set(target) != required:
            raise ValueError(f"connections[{index}] must be an exact analysis mutation_target object")
        for key in required:
            value = target[key]
            limit = 1024 if key in {"physical_net", "sink_pin"} else 128
            if not isinstance(value, str) or not value or len(value) > limit:
                raise ValueError(f"connections[{index}].{key} is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", target["certificate"]):
            raise ValueError(f"connections[{index}].certificate is invalid")


def _selective_unroute_sink_branches_for_design(design, design_tools, targets) -> Dict[str, Any]:
    """Preflight all targets, mutate by net, then enforce exact preservation postconditions."""
    _validate_mutation_targets(targets)
    prepared = []
    seen = set()
    for target in targets:
        key = (target["physical_net"], target["sink_pin"])
        if key in seen:
            raise ValueError(f"duplicate connection target {key[0]} -> {key[1]}")
        seen.add(key)
        stored = _route_analysis_certificates.get(target["certificate"])
        if stored is None or any(stored.get(field) != target[field] for field in (
            "physical_net", "sink_pin", "before_net_pip_digest", "before_sink_pip_digest"
        )):
            raise ValueError(f"missing, stale, or mismatched analysis certificate for {key[0]} -> {key[1]}")
        analysis = _analyze_one_connection(design, design_tools, target, issue_certificate=False)
        if analysis.get("recommended_action") != "selective_unroute_candidate":
            raise ValueError(
                f"connection is no longer certificate-safe: {key[0]} -> {key[1]} "
                f"({analysis.get('recommended_action')})"
            )
        if analysis["before_net_pip_digest"] != target["before_net_pip_digest"]:
            raise ValueError(f"net PIP digest changed since analysis for {key[0]}")
        if analysis["before_sink_pip_digest"] != target["before_sink_pip_digest"]:
            raise ValueError(f"sink PIP digest changed since analysis for {key[0]} -> {key[1]}")
        net = design.getNet(target["physical_net"])
        sink, error = _resolve_sink_pin(design, net, target["sink_pin"])
        if error:
            raise ValueError(error)
        prepared.append((target, net, sink, analysis))

    by_net = {}
    for item in prepared:
        by_net.setdefault(item[0]["physical_net"], []).append(item)
    manifests = []
    expected_by_net = {}
    for net_name, items in by_net.items():
        net = items[0][1]
        selected_pins = [item[2] for item in items]
        selected_keys = {_site_pin_key(pin) for pin in selected_pins}
        before_pips = _as_list(net.getPIPs())
        before_keys = {_pip_key(pip) for pip in before_pips}
        selected_path_keys = set()
        preserved_before = {}
        preserved_paths = set()
        for pin in _as_list(net.getSinkPins()):
            path = _as_list(design_tools.getConnectionPIPs(pin))
            path_keys = {_pip_key(pip) for pip in path}
            if _site_pin_key(pin) in selected_keys:
                selected_path_keys.update(path_keys)
            else:
                preserved_paths.update(path_keys)
                preserved_before[_site_pin_key(pin)] = _pip_digest(path)
        removable_keys = selected_path_keys - preserved_paths
        if not preserved_before:
            raise ValueError(
                f"selection covers every sink on {net_name}; whole-net unroute is forbidden"
            )
        if not removable_keys:
            raise ValueError(f"batch has no removable sink branch PIPs on {net_name}")
        if not removable_keys <= selected_path_keys or removable_keys & preserved_paths:
            raise ValueError(f"shared-trunk preservation cannot be proven for batch on {net_name}")
        expected_by_net[net_name] = {
            "before_keys": before_keys,
            "removable_keys": removable_keys,
            "preserved_before": preserved_before,
            "selected_keys": selected_keys,
            "before_digest": _pip_digest(before_pips),
            "before_count": len(before_pips),
        }

    for net_name, items in by_net.items():
        design_tools.unroutePins(
            items[0][1], _java_pin_collection([item[2] for item in items])
        )
    design_tools.updatePinsIsRouted(design)

    unrouted_sinks = []
    preserved_sink_count = 0
    for net_name, items in by_net.items():
        net = items[0][1]
        expected = expected_by_net[net_name]
        after_pips = _as_list(net.getPIPs())
        after_keys = {_pip_key(pip) for pip in after_pips}
        if after_keys != expected["before_keys"] - expected["removable_keys"]:
            raise RuntimeError(f"postcondition failed: unexpected PIP delta on {net_name}")
        preservation = []
        for pin in _as_list(net.getSinkPins()):
            pin_key = _site_pin_key(pin)
            if pin_key in expected["selected_keys"]:
                if bool(pin.isRouted()):
                    raise RuntimeError(f"postcondition failed: selected sink remains routed: {pin_key}")
                unrouted_sinks.append({"physical_net": net_name, "sink_pin": pin_key})
                continue
            after_digest = _pip_digest(design_tools.getConnectionPIPs(pin))
            before_digest = expected["preserved_before"][pin_key]
            preserved = after_digest == before_digest and bool(pin.isRouted())
            if not preserved:
                raise RuntimeError(f"postcondition failed: preserved sink changed: {pin_key}")
            preserved_sink_count += 1
            preservation.append({
                "sink_pin": pin_key,
                "before_pip_digest": before_digest,
                "after_pip_digest": after_digest,
                "preserved": True,
            })
        manifests.append({
            "physical_net": net_name,
            "before_pip_digest": expected["before_digest"],
            "after_pip_digest": _pip_digest(after_pips),
            "before_pip_count": expected["before_count"],
            "after_pip_count": len(after_pips),
            "removed_pip_count": len(expected["removable_keys"]),
            "preserved_sinks": preservation,
        })
    return {
        "status": "success",
        "before_pip_digests": {item["physical_net"]: item["before_pip_digest"] for item in manifests},
        "after_pip_digests": {item["physical_net"]: item["after_pip_digest"] for item in manifests},
        "preserved_sink_count": preserved_sink_count,
        "unrouted_sinks": unrouted_sinks,
        "affected_nets": manifests,
        "checkpoint_written": False,
    }


def _reload_current_design_from_seed() -> bool:
    global _current_design, _current_design_mutated, _session_valid
    if not _current_design_source or not _current_design_source_sha256:
        _current_design = None
        _session_valid = False
        return False
    try:
        if _sha256_file(_current_design_source) != _current_design_source_sha256:
            raise RuntimeError("exact input checkpoint digest changed")
        from com.xilinx.rapidwright.design import Design

        reloaded = Design.readCheckpoint(_current_design_source)
        if _sha256_file(_current_design_source) != _current_design_source_sha256:
            raise RuntimeError("exact input checkpoint changed while being reloaded")
        _current_design = reloaded
        _current_design_mutated = False
        _session_valid = True
        _reset_route_analysis_state()
        return True
    except Exception as exc:
        logger.error("Exact-seed rollback failed: %s", exc)
        _current_design = None
        _session_valid = False
        _reset_route_analysis_state()
        return False


def _canonical_structure_payload(structure: dict) -> dict:
    if not isinstance(structure, dict) or set(structure) != {
        "structure_type", "cells", "sites", "edges"
    }:
        raise ValueError("structure must be an exact internal analysis payload")
    structure_type = structure.get("structure_type")
    if structure_type not in {"carry_cloud", "hard_macro_cascade"}:
        raise ValueError("structure_type is not relocatable")
    cells = structure.get("cells")
    sites = structure.get("sites")
    edges = structure.get("edges")
    if not isinstance(cells, list) or not 1 <= len(cells) <= _MAX_STRUCTURE_MEMBERS:
        raise ValueError(f"structure cells must contain 1..{_MAX_STRUCTURE_MEMBERS} members")
    if not isinstance(sites, list) or not 1 <= len(sites) <= _MAX_STRUCTURE_SITES:
        raise ValueError(f"structure sites must contain 1..{_MAX_STRUCTURE_SITES} sites")
    if not isinstance(edges, list) or len(edges) > _MAX_STRUCTURE_MEMBERS:
        raise ValueError("structure edges exceed the internal bound")

    required = {"name", "ref_name", "loc", "bel", "loc_fixed", "bel_fixed"}
    normalized_cells = []
    names = set()
    for index, cell in enumerate(cells):
        if not isinstance(cell, dict) or set(cell) != required:
            raise ValueError(f"structure.cells[{index}] is not an exact member record")
        item = {key: cell[key] for key in required}
        for key in ("name", "ref_name", "loc", "bel"):
            if not isinstance(item[key], str) or not item[key] or len(item[key]) > 1024:
                raise ValueError(f"structure.cells[{index}].{key} is invalid")
        for key in ("loc_fixed", "bel_fixed"):
            if not isinstance(item[key], bool):
                raise ValueError(f"structure.cells[{index}].{key} must be boolean")
        if item["name"] in names:
            raise ValueError("structure contains duplicate cell names")
        names.add(item["name"])
        normalized_cells.append(item)
    normalized_sites = []
    for site in sites:
        if not isinstance(site, str) or not site or len(site) > 256:
            raise ValueError("structure contains an invalid site")
        normalized_sites.append(site)
    if len(set(normalized_sites)) != len(normalized_sites):
        raise ValueError("structure contains duplicate sites")
    for edge in edges:
        if not isinstance(edge, dict):
            raise ValueError("structure edge must be an object")
        encoded = json.dumps(edge, sort_keys=True, separators=(",", ":"))
        if len(encoded) > 4096:
            raise ValueError("structure edge exceeds the internal bound")
    return {
        "structure_type": structure_type,
        "cells": sorted(
            normalized_cells, key=lambda item: (item["name"], item["ref_name"])
        ),
        "sites": sorted(normalized_sites),
        "edges": sorted(
            edges,
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        ),
    }


def _structure_content_digest(structure: dict) -> str:
    payload = _canonical_structure_payload(structure)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _strict_bool_call(obj, method: str) -> bool:
    function = getattr(obj, method, None)
    if function is None:
        raise ValueError(f"RapidWright object lacks required {method}() API")
    value = function()
    if not isinstance(value, (bool, int)):
        raise ValueError(f"RapidWright {method}() returned an unknown value")
    return bool(value)


def _current_member_record(cell) -> dict:
    if not _strict_bool_call(cell, "isPlaced") or cell.getSiteInst() is None:
        raise ValueError(f"cell {cell.getName()} is not placed")
    return {
        "name": str(cell.getName()),
        "ref_name": str(cell.getType()),
        "loc": str(cell.getSiteName()),
        "bel": str(cell.getBELName()),
        "loc_fixed": _strict_bool_call(cell, "isSiteFixed"),
        "bel_fixed": _strict_bool_call(cell, "isBELFixed"),
    }


def _placement_digest(records: list[dict]) -> str:
    values = [
        f"{item['name']}|{item['ref_name']}|{item['loc']}|{item['bel']}"
        for item in sorted(records, key=lambda value: value["name"])
    ]
    return hashlib.sha256("\n".join(values).encode("utf-8")).hexdigest()


def _tile_type(tile) -> str:
    value = tile.getTileTypeEnum()
    if value is None:
        raise ValueError("tile type is unknown")
    return str(value)


def _route_shape_digest(pips) -> str:
    values = []
    for pip in _as_list(pips):
        tile = pip.getTile()
        values.append(
            "|".join((
                _tile_type(tile), str(pip.getStartWireName()),
                str(pip.getEndWireName()), str(int(bool(pip.isReversed()))),
            ))
        )
    return hashlib.sha256("\n".join(sorted(values)).encode("utf-8")).hexdigest()


def _java_site_inst_collection(site_insts):
    try:
        from java.util import ArrayList

        result = ArrayList()
        for site_inst in site_insts:
            result.add(site_inst)
        return result
    except ImportError:
        return list(site_insts)


def _load_relocation_tools():
    from com.xilinx.rapidwright.design.tools import RelocationTools

    return RelocationTools


def _load_net_tools():
    from com.xilinx.rapidwright.design import NetTools

    return NetTools


def _relocate_structure_cluster_for_design(
    design,
    structure: dict,
    expected_digest: str,
    tile_col_offset: int,
    tile_row_offset: int,
    max_boundary_nets: int,
    max_boundary_pins: int,
    relocation_tools,
    design_tools,
    net_tools=None,
) -> Dict[str, Any]:
    """Preflight and relocate one exact analysis-derived SiteInst collection."""
    payload = _canonical_structure_payload(structure)
    if not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise ValueError("expected_digest is invalid")
    if _structure_content_digest(payload) != expected_digest:
        raise ValueError("structure content digest mismatch")
    for name, value in (("tile_col_offset", tile_col_offset), ("tile_row_offset", tile_row_offset)):
        if isinstance(value, bool) or not isinstance(value, int) or abs(value) > _MAX_RELOCATION_OFFSET:
            raise ValueError(f"{name} must be an integer in -{_MAX_RELOCATION_OFFSET}..{_MAX_RELOCATION_OFFSET}")
    if tile_col_offset == 0 and tile_row_offset == 0:
        raise ValueError("relocation offset must be nonzero")
    if (
        isinstance(max_boundary_nets, bool) or not isinstance(max_boundary_nets, int)
        or not 0 <= max_boundary_nets <= _MAX_BOUNDARY_NETS
    ):
        raise ValueError(f"max_boundary_nets must be 0..{_MAX_BOUNDARY_NETS}")
    if (
        isinstance(max_boundary_pins, bool) or not isinstance(max_boundary_pins, int)
        or not 0 <= max_boundary_pins <= _MAX_BOUNDARY_PINS
    ):
        raise ValueError(f"max_boundary_pins must be 0..{_MAX_BOUNDARY_PINS}")

    expected_by_name = {item["name"]: item for item in payload["cells"]}
    current_records = []
    site_insts_by_name = {}
    for name, expected in expected_by_name.items():
        cell = design.getCell(name)
        if cell is None:
            raise ValueError(f"structure cell missing: {name}")
        current = _current_member_record(cell)
        if current != expected:
            raise ValueError(f"structure member placement digest mismatch: {name}")
        if _strict_bool_call(cell, "isLocked") or current["loc_fixed"] or current["bel_fixed"]:
            raise ValueError(f"locked or fixed structure cell: {name}")
        current_records.append(current)
        site_insts_by_name[current["loc"]] = cell.getSiteInst()
    if set(site_insts_by_name) != set(payload["sites"]):
        raise ValueError("structure site set is incomplete or stale")

    member_names = set(expected_by_name)
    source_site_insts = set(site_insts_by_name.values())
    for site_name, site_inst in site_insts_by_name.items():
        if not _strict_bool_call(site_inst, "isPlaced"):
            raise ValueError(f"structure SiteInst is not placed: {site_name}")
        occupants = _as_list(site_inst.getCells())
        for occupant in occupants:
            occupant_name = str(occupant.getName())
            if occupant_name not in member_names:
                raise ValueError(f"mixed SiteInst contains structure-external cell: {site_name}")
            current = _current_member_record(occupant)
            if _strict_bool_call(occupant, "isLocked") or current["loc_fixed"] or current["bel_fixed"]:
                raise ValueError(f"locked or fixed cell in moved SiteInst: {occupant_name}")
        if {str(cell.getName()) for cell in occupants} != {
            name for name, item in expected_by_name.items() if item["loc"] == site_name
        }:
            raise ValueError(f"SiteInst member set is incomplete: {site_name}")

    destinations = []
    destination_names = set()
    for site_name in sorted(site_insts_by_name):
        site_inst = site_insts_by_name[site_name]
        source_site = site_inst.getSite()
        source_tile = source_site.getTile()
        destination_tile = source_tile.getTileXYNeighbor(tile_col_offset, tile_row_offset)
        if destination_tile is None:
            raise ValueError(f"destination tile does not exist for {site_name}")
        destination_site = source_site.getCorrespondingSite(
            source_site.getSiteTypeEnum(), destination_tile
        )
        if destination_site is None:
            raise ValueError(f"corresponding destination site does not exist for {site_name}")
        if str(destination_site.getSiteTypeEnum()) != str(source_site.getSiteTypeEnum()):
            raise ValueError(f"destination site type mismatch for {site_name}")
        destination_name = str(destination_site.getName())
        if destination_name in destination_names:
            raise ValueError("multiple source sites map to one destination site")
        destination_names.add(destination_name)
        occupying = design.getSiteInstFromSite(destination_site)
        if occupying is not None and occupying not in source_site_insts:
            raise ValueError(f"destination site is occupied: {destination_name}")
        destinations.append((site_inst, source_site, destination_site))

    # Normalize missing physical net/pin metadata before inspecting every
    # boundary. A rejected public mutation is exact-reloaded by the wrapper.
    design_tools.makePhysNetNamesConsistent(design)
    design_tools.createMissingSitePinInsts(design)
    if int(design_tools.updatePinsIsRouted(design)) != 0:
        raise ValueError("exact seed contains unrouted sink pins")

    boundary = []
    internal = []
    affected = []
    boundary_pin_count = 0
    for net in _as_list(design.getNets()):
        pins = _as_list(net.getPins())
        inside = [pin for pin in pins if pin.getSiteInst() in source_site_insts]
        if not inside:
            continue
        affected.append(net)
        outside = [pin for pin in pins if pin.getSiteInst() not in source_site_insts]
        if outside:
            boundary.append(net)
            boundary_pin_count += len(outside)
        else:
            internal.append(net)
        has_clock_sinks = False
        if net_tools is not None:
            has_clock_sinks = bool(net_tools.hasClockSinks(net))
        if bool(net.isClockNet()) or has_clock_sinks or bool(net.isStaticNet()):
            raise ValueError(f"clock or static net touches relocation cluster: {net.getName()}")
        for pip in _as_list(net.getPIPs()):
            if _pip_fixed(pip) is not False:
                raise ValueError(f"fixed or unknown PIP touches relocation cluster: {net.getName()}")
        source = net.getSource()
        if source is not None and source.getSiteInst() in source_site_insts:
            for pip in _as_list(net.getPIPs()):
                source_tile = pip.getTile()
                destination_tile = source_tile.getTileXYNeighbor(
                    tile_col_offset, tile_row_offset
                )
                if destination_tile is None:
                    raise ValueError(f"PIP destination tile is outside device: {net.getName()}")
                if _tile_type(source_tile) != _tile_type(destination_tile):
                    raise ValueError(f"PIP destination tile type mismatch: {net.getName()}")
    if len(boundary) > max_boundary_nets:
        raise ValueError("affected boundary net limit exceeded")
    if boundary_pin_count > max_boundary_pins:
        raise ValueError("affected boundary pin limit exceeded")

    before_internal = {
        str(net.getName()): {
            "pip_count": len(_as_list(net.getPIPs())),
            "shape_digest": _route_shape_digest(net.getPIPs()),
        }
        for net in internal
    }
    before_placement_digest = _placement_digest(current_records)
    moved_sites = [
        {"from": str(source.getName()), "to": str(destination.getName())}
        for _, source, destination in destinations
    ]
    succeeded = relocation_tools.relocate(
        design,
        _java_site_inst_collection([item[0] for item in destinations]),
        tile_col_offset,
        tile_row_offset,
    )
    if not bool(succeeded):
        raise RuntimeError("RelocationTools.relocate returned false")

    design_tools.makePhysNetNamesConsistent(design)
    design_tools.createMissingSitePinInsts(design)
    unrouted_pin_count = int(design_tools.updatePinsIsRouted(design))
    after_records = [_current_member_record(design.getCell(name)) for name in expected_by_name]
    after_sites = {item["loc"] for item in after_records}
    if after_sites != destination_names:
        raise RuntimeError("relocation placement postcondition failed")
    preservation = []
    preserved_count = 0
    for net in internal:
        name = str(net.getName())
        after_count = len(_as_list(net.getPIPs()))
        after_shape = _route_shape_digest(net.getPIPs())
        preserved = (
            after_count == before_internal[name]["pip_count"]
            and after_shape == before_internal[name]["shape_digest"]
        )
        preserved_count += int(preserved)
        preservation.append({
            "physical_net": name,
            "pip_count_before": before_internal[name]["pip_count"],
            "pip_count_after": after_count,
            "route_shape_preserved": preserved,
        })
    unrouted_nets = []
    for net in affected:
        if any(not bool(pin.isRouted()) for pin in _as_list(net.getSinkPins())):
            unrouted_nets.append(str(net.getName()))
    return {
        "status": "success",
        "structure_type": payload["structure_type"],
        "content_digest": expected_digest,
        "moved_sites": moved_sites,
        "before_placement_digest": before_placement_digest,
        "after_placement_digest": _placement_digest(after_records),
        "affected_boundary_nets": sorted(str(net.getName()) for net in boundary),
        "affected_nets_requiring_route": sorted(set(unrouted_nets)),
        "boundary_pin_count": boundary_pin_count,
        "unrouted_pin_count": unrouted_pin_count,
        "unrouted_net_count": len(set(unrouted_nets)),
        "internal_route_preservation": {
            "internal_net_count": len(internal),
            "preserved_net_count": preserved_count,
            "all_preserved": preserved_count == len(internal),
            "nets": preservation[:_MAX_BOUNDARY_NETS],
            "nets_truncated": len(preservation) > _MAX_BOUNDARY_NETS,
        },
        "checkpoint_written": False,
    }


def relocate_structure_cluster(
    structure: dict,
    expected_digest: str,
    tile_col_offset: int,
    tile_row_offset: int,
    max_boundary_nets: int = 16,
    max_boundary_pins: int = 128,
) -> Dict[str, Any]:
    """Internal exact-cluster mutation. It is intentionally absent from list_tools()."""
    global _current_design_mutated, _session_valid, _checkpoint_write_blocked
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    if _current_design is None or not _current_design_source or not _current_design_source_sha256:
        return {"error": "No exact checkpoint design loaded. Use read_checkpoint first."}
    if not _session_valid:
        return {"error": "RapidWright session is invalid; reload the exact input checkpoint"}
    if _current_design_mutated:
        return {"error": "relocation is single-use per exact checkpoint load; reload the seed"}
    try:
        if _sha256_file(_current_design_source) != _current_design_source_sha256:
            raise RuntimeError("exact input checkpoint digest changed")
        started = time.monotonic()
        result = _relocate_structure_cluster_for_design(
            _current_design,
            structure,
            expected_digest,
            tile_col_offset,
            tile_row_offset,
            max_boundary_nets,
            max_boundary_pins,
            _load_relocation_tools(),
            _load_design_tools(),
            _load_net_tools(),
        )
        _current_design_mutated = True
        _checkpoint_write_blocked = False
        _route_analysis_certificates.clear()
        result["runtime_s"] = round(time.monotonic() - started, 6)
        return result
    except Exception as exc:
        rolled_back = _reload_current_design_from_seed()
        _checkpoint_write_blocked = True
        return {
            "status": "error",
            "error": str(exc),
            "rolled_back": rolled_back,
            "session_valid": bool(_session_valid),
            "checkpoint_written": False,
            "write_blocked_until_explicit_reload": True,
        }


def selective_unroute_sink_branches(connections: list) -> Dict[str, Any]:
    """Remove only certificate-safe sink branches; never fall back to whole-net unroute."""
    global _current_design_mutated, _session_valid
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}
    if not _session_valid:
        return {"error": "RapidWright session is invalid; reload the exact input checkpoint"}
    if _current_design_mutated:
        return {"error": "selective mutation is single-use per exact checkpoint load; reload the seed"}
    try:
        started = time.monotonic()
        result = _selective_unroute_sink_branches_for_design(
            _current_design, _load_design_tools(), connections
        )
        _current_design_mutated = True
        _route_analysis_certificates.clear()
        result["runtime_s"] = round(time.monotonic() - started, 6)
        return result
    except Exception as exc:
        rolled_back = _reload_current_design_from_seed()
        return {
            "status": "error",
            "error": str(exc),
            "rolled_back": rolled_back,
            "session_valid": bool(_session_valid),
            "checkpoint_written": False,
        }


def report_approx_timing(
    clock_period_ns: Optional[float] = None,
    include_path: bool = True,
    max_path_lines: int = 20,
) -> Dict[str, Any]:
    """
    Report an approximate critical-path timing estimate using RapidWright's timing model.

    Args:
        clock_period_ns: Optional known clock period in nanoseconds. When provided,
            the tool computes an estimated WNS against that period instead of relying
            on RapidWright's internally-derived required time.
        include_path: Include the critical path text in the response.
        max_path_lines: Maximum number of path lines to include when include_path=True.

    Returns:
        Dictionary with approximate delay/slack/Fmax information for the current design.
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}

    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}

    try:
        from com.xilinx.rapidwright.timing import TimingManager

        start_time = time.time()
        design = _current_design
        timing_manager = TimingManager(design)
        timing_graph = timing_manager.getTimingGraph()

        critical_path = timing_graph.getMaxDelayPath()
        if critical_path is None:
            timing_graph.buildGraphPaths()
            critical_path = timing_graph.getMaxDelayPath()

        if critical_path is None:
            return {
                "error": (
                    "RapidWright timing graph did not produce a critical path. "
                    "This can happen if the design is incomplete or graph paths "
                    "could not be built."
                )
            }

        path_delay_ps = timing_graph.getPathDelay(critical_path)
        if path_delay_ps is None:
            path_delay_ps = critical_path.getWeight()

        if path_delay_ps is None:
            return {"error": "RapidWright timing graph returned no path delay"}

        path_delay_ps = float(path_delay_ps)
        path_delay_ns = path_delay_ps / 1000.0

        required_time_ps = None
        estimated_slack_ps = None
        timing_basis = "rapidwright_graph"

        if clock_period_ns is not None:
            required_time_ps = float(clock_period_ns) * 1000.0
            estimated_slack_ps = required_time_ps - path_delay_ps
            timing_basis = "provided_clock_period"
        else:
            try:
                required_time_ps = float(timing_graph.getRequiredTime(critical_path))
            except Exception:
                required_time_ps = None
            try:
                slack_value = timing_graph.getSlack(critical_path)
                if slack_value is not None:
                    estimated_slack_ps = float(slack_value)
            except Exception:
                estimated_slack_ps = None

        estimated_fmax_mhz = None
        if path_delay_ns > 0:
            estimated_fmax_mhz = 1000.0 / path_delay_ns

        result: Dict[str, Any] = {
            "status": "success",
            "timing_basis": timing_basis,
            "path_delay_ps": round(path_delay_ps, 3),
            "path_delay_ns": round(path_delay_ns, 3),
            "estimated_fmax_mhz": round(estimated_fmax_mhz, 2) if estimated_fmax_mhz is not None else None,
            "elapsed_seconds": round(time.time() - start_time, 3),
        }

        if required_time_ps is not None:
            result["required_time_ps"] = round(required_time_ps, 3)
            result["required_time_ns"] = round(required_time_ps / 1000.0, 3)

        if estimated_slack_ps is not None:
            result["estimated_wns_ps"] = round(estimated_slack_ps, 3)
            result["estimated_wns_ns"] = round(estimated_slack_ps / 1000.0, 3)

        try:
            worst_slack = timing_graph.getWorstSlack()
            if worst_slack is not None:
                result["graph_worst_slack_ps"] = round(float(worst_slack), 3)
                result["graph_worst_slack_ns"] = round(float(worst_slack) / 1000.0, 3)
        except Exception:
            pass

        try:
            result["critical_path_vertex_count"] = len(list(critical_path.getVertexList()))
        except Exception:
            pass

        if include_path:
            path_lines = str(critical_path).replace(", ", ",\n").splitlines()
            if max_path_lines > 0 and len(path_lines) > max_path_lines:
                result["critical_path"] = path_lines[:max_path_lines]
                result["critical_path_truncated"] = True
                result["critical_path_total_lines"] = len(path_lines)
            else:
                result["critical_path"] = path_lines

        return result

    except Exception as e:
        logger.error(f"Error reporting approximate timing: {e}")
        return {"error": str(e)}


def get_design_info() -> Dict[str, Any]:
    """
    Get information about the currently loaded design.

    Returns:
        Dictionary with design statistics
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}

    if _current_design is None:
        return {"error": "No design loaded. Use load_design first."}

    try:
        design = _current_design

        # Count cell types
        cell_types = {}
        for cell in design.getCells():
            cell_type = str(cell.getType())
            cell_types[cell_type] = cell_types.get(cell_type, 0) + 1

        # Get top 10 most common cell types
        top_types = sorted(cell_types.items(), key=lambda x: x[1], reverse=True)[:10]

        return {
            "status": "success",
            "design_name": str(design.getName()),
            "device": str(design.getDevice().getName()),
            "part_name": str(design.getPartName()),
            "cell_count": design.getCells().size(),
            "net_count": design.getNets().size(),
            "top_cell_types": dict(top_types),
            "is_netlist_encrypted": design.getNetlist().hasEncryptedCells()
        }

    except Exception as e:
        logger.error(f"Error getting design info: {e}")
        return {"error": str(e)}


def search_cells(pattern: Optional[str] = None,
                cell_type: Optional[str] = None,
                limit: int = 100) -> Dict[str, Any]:
    """
    Search for cells in the current design.

    Args:
        pattern: Name pattern to match (case-insensitive)
        cell_type: Filter by cell type
        limit: Maximum number of results

    Returns:
        Dictionary with matching cells
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}

    if _current_design is None:
        return {"error": "No design loaded. Use load_design first."}

    try:
        design = _current_design
        matching_cells = []
        pattern_lower = pattern.lower() if pattern else None

        for cell in design.getCells():
            if len(matching_cells) >= limit:
                break

            cell_name = str(cell.getName())
            cell_type_str = str(cell.getType())

            # Apply filters
            if pattern_lower and pattern_lower not in cell_name.lower():
                continue
            if cell_type and cell_type != cell_type_str:
                continue

            # Get placement info
            placement = "unplaced"
            if cell.isPlaced():
                site = cell.getSite()
                if site:
                    placement = str(site.getName())

            matching_cells.append({
                "name": cell_name,
                "type": cell_type_str,
                "placement": placement
            })

        return {
            "status": "success",
            "count": len(matching_cells),
            "cells": matching_cells,
            "truncated": len(matching_cells) >= limit
        }

    except Exception as e:
        logger.error(f"Error searching cells: {e}")
        return {"error": str(e)}


def get_tile_info(tile_name: str, device_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Get information about a specific tile.

    Args:
        tile_name: Name of the tile
        device_name: Device name (uses current design's device if not specified)

    Returns:
        Dictionary with tile information
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}

    try:
        from com.xilinx.rapidwright.device import Device

        # Get device
        if device_name:
            device = Device.getDevice(device_name)
        elif _current_design:
            device = _current_design.getDevice()
        else:
            return {"error": "No device specified and no design loaded"}

        tile = device.getTile(tile_name)
        if tile is None:
            return {"error": f"Tile '{tile_name}' not found"}

        # Get sites in this tile
        sites = []
        if tile.getSites():
            for site in tile.getSites():
                sites.append({
                    "name": str(site.getName()),
                    "type": str(site.getSiteTypeEnum())
                })

        return {
            "status": "success",
            "name": str(tile.getName()),
            "type": str(tile.getTileTypeEnum()),
            "row": tile.getRow(),
            "column": tile.getColumn(),
            "site_count": len(sites),
            "sites": sites
        }

    except Exception as e:
        logger.error(f"Error getting tile info: {e}")
        return {"error": str(e)}


def search_sites(site_type: Optional[str] = None,
                device_name: Optional[str] = None,
                limit: int = 50) -> Dict[str, Any]:
    """
    Search for sites on a device.

    Args:
        site_type: Filter by site type (e.g., 'SLICEL', 'DSP48E2')
        device_name: Device name (uses current design's device if not specified)
        limit: Maximum number of results

    Returns:
        Dictionary with matching sites
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}

    try:
        from com.xilinx.rapidwright.device import Device

        # Get device
        if device_name:
            device = Device.getDevice(device_name)
        elif _current_design:
            device = _current_design.getDevice()
        else:
            return {"error": "No device specified and no design loaded"}

        matching_sites = []

        for site in device.getAllSites():
            if len(matching_sites) >= limit:
                break

            site_type_str = str(site.getSiteTypeEnum())

            # Filter by site type if specified
            if site_type and site_type not in site_type_str:
                continue

            tile = site.getTile()
            matching_sites.append({
                "name": str(site.getName()),
                "type": site_type_str,
                "tile": str(tile.getName()) if tile else "unknown"
            })

        return {
            "status": "success",
            "count": len(matching_sites),
            "sites": matching_sites,
            "truncated": len(matching_sites) >= limit
        }

    except Exception as e:
        logger.error(f"Error searching sites: {e}")
        return {"error": str(e)}


def optimize_lut_input_cone(hierarchical_input_pins: list[str]) -> Dict[str, Any]:
    """
    Optimize LUT input cones by combining chained small LUTs into a single larger LUT.

    This optimization reduces logic depth by replacing series of small LUTs with a single
    larger LUT (up to 6 inputs). This is particularly useful for critical paths where
    the delay through multiple LUT levels can be reduced to a single LUT.

    Args:
        hierarchical_input_pins: List of hierarchical input pin names to optimize
                                (e.g., ["module/submodule/inst/pin"])

    Returns:
        Dictionary with optimization results
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}

    if _current_design is None:
        return {"error": "No design loaded. Use load_design first."}

    try:
        from com.xilinx.rapidwright.eco import LUTInputConeOpt

        design = _current_design
        results = []

        logger.info(f"Optimizing {len(hierarchical_input_pins)} LUT input cones")

        for pin_name in hierarchical_input_pins:
            try:
                # Get the hierarchical port instance
                port_inst = design.getNetlist().getHierPortInstFromName(pin_name)

                if port_inst is None:
                    results.append({
                        "pin": pin_name,
                        "status": "error",
                        "message": f"Pin '{pin_name}' not found in design"
                    })
                    continue

                # Attempt optimization
                optimized_cell = LUTInputConeOpt.optimizedLUTInputCone(design, port_inst)

                if optimized_cell is None:
                    results.append({
                        "pin": pin_name,
                        "status": "no_optimization",
                        "message": "No optimization possible for this pin (may not be driven by LUTs or only single LUT in path)"
                    })
                else:
                    cell_info = {
                        "name": str(optimized_cell.getName()),
                        "type": str(optimized_cell.getType()),
                        "placement": "unplaced"
                    }

                    if optimized_cell.isPlaced():
                        site = optimized_cell.getSite()
                        if site:
                            cell_info["placement"] = str(site.getName())

                    results.append({
                        "pin": pin_name,
                        "status": "optimized",
                        "message": "LUT input cone successfully optimized",
                        "new_cell": cell_info
                    })

            except Exception as e:
                logger.error(f"Error optimizing pin {pin_name}: {e}")
                results.append({
                    "pin": pin_name,
                    "status": "error",
                    "message": str(e)
                })

        # Count successful optimizations
        success_count = sum(1 for r in results if r["status"] == "optimized")

        return {
            "status": "success",
            "total_pins": len(hierarchical_input_pins),
            "optimized_count": success_count,
            "results": results
        }

    except Exception as e:
        logger.error(f"Error in LUT input cone optimization: {e}")
        return {"error": str(e)}


def inspect_lut_pin_swap_candidates(cell_pins: list[str]) -> Dict[str, Any]:
    """
    Inspect Vivado LUT input pin paths and report fastest-pin swap candidates.

    This is a read-only screening helper. It does not edit pin mappings, site
    pins, nets, or routing; it only resolves the current physical input and the
    fastest target physical input that swap_lut_pins would use for FASTEST.
    """
    if not _initialized:
        return {
            "status": "error",
            "error": "RapidWright not initialized. Call initialize_rapidwright first.",
            "candidates": [],
            "invalid": [],
        }

    if _current_design is None:
        return {
            "status": "error",
            "error": "No design loaded. Use read_checkpoint first.",
            "candidates": [],
            "invalid": [],
        }

    try:
        from com.xilinx.rapidwright.design.tools import LUTTools

        return _inspect_lut_pin_swap_candidates_for_design(
            cell_pins, _current_design, LUTTools
        )
    except Exception as e:
        logger.error("Error inspecting LUT pin swap candidates: %s", e)
        return {
            "status": "error",
            "error": str(e),
            "candidates": [],
            "invalid": [],
        }


def swap_lut_pins(
    cell_name: str,
    pin_swaps: list,
    unroute_affected: bool = True,
) -> Dict[str, Any]:
    """
    Move LUT input site pins and update the cell's logical-to-physical pin map.

    This is a direct RapidWright cell edit around LUTTools.swapMultipleLutPins().
    It preserves logical function by keeping each logical input attached to the
    same net while changing the physical LUT input pin used inside the SLICE.

    Args:
        cell_name: Hierarchical design cell name of a placed LUT cell.
        pin_swaps: List of [from_pin, to_pin] pairs. Pins may be logical
                   (I0..I5), physical (A1..A6), site pins (A1..H6), or target
                   aliases FASTEST/FASTEST_FREE/AUTO for the destination.
        unroute_affected: If true, unroute nets touched by the pin move so a
                          following Vivado route_design rebuilds legal routing.

    Returns:
        Dictionary with per-pin move details and before/after mappings.
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}

    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}

    try:
        from com.xilinx.rapidwright.design.tools import LUTTools
        from java.util import HashMap

        normalized_swaps = _normalize_lut_pin_swaps(pin_swaps)
        design = _current_design
        cell = design.getCell(cell_name)
        if cell is None:
            return {"error": f"Cell '{cell_name}' not found in design"}

        bel = cell.getBEL()
        if bel is None or not bel.isLUT():
            return {"error": f"Cell '{cell_name}' is not placed on a LUT BEL"}
        if not cell.isPlaced() or cell.getSiteInst() is None:
            return {"error": f"Cell '{cell_name}' is not placed"}

        cell_type = str(cell.getType() or "")
        if not cell_type.startswith("LUT") and not cell.isRoutethru():
            return {
                "error": (
                    f"Cell '{cell_name}' has type {cell_type}; swap_lut_pins "
                    "only supports ordinary LUT cells/routethrus"
                )
            }

        site_inst = cell.getSiteInst()
        java_swaps = HashMap()
        move_details = []
        affected_nets = {}
        before_mapping = _java_string_map_to_dict(cell.getPinMappingsP2L())
        try:
            before_equation = str(LUTTools.getLUTEquation(cell))
        except Exception:
            before_equation = None

        for old_token, new_token in normalized_swaps:
            old_physical = _resolve_lut_physical_pin(cell, old_token)
            new_physical = _resolve_lut_target_physical_pin(
                cell, old_physical, new_token, LUTTools
            )
            old_site_pin = _site_pin_name_for_physical(cell, old_physical)
            new_site_pin = _site_pin_name_for_physical(cell, new_physical)

            if old_site_pin == new_site_pin:
                move_details.append({
                    "from": old_token,
                    "to": new_token,
                    "status": "skipped",
                    "message": "source and target resolve to the same site pin",
                    "site_pin": old_site_pin,
                })
                continue

            old_spi = site_inst.getSitePinInst(old_site_pin)
            if old_spi is None:
                move_details.append({
                    "from": old_token,
                    "to": new_token,
                    "status": "skipped",
                    "message": f"source site pin {old_site_pin} is not present",
                    "old_physical_pin": old_physical,
                    "new_physical_pin": new_physical,
                })
                continue
            if not old_spi.isLUTInputPin():
                move_details.append({
                    "from": old_token,
                    "to": new_token,
                    "status": "skipped",
                    "message": f"source site pin {old_site_pin} is not a LUT input pin",
                })
                continue

            for spi in (old_spi, site_inst.getSitePinInst(new_site_pin)):
                if spi is None:
                    continue
                net = spi.getNet()
                if net is not None:
                    affected_nets[str(net.getName())] = net

            java_swaps.put(old_spi, new_site_pin)
            move_details.append({
                "from": old_token,
                "to": new_token,
                "status": "queued",
                "old_site_pin": old_site_pin,
                "new_site_pin": new_site_pin,
                "old_physical_pin": old_physical,
                "new_physical_pin": new_physical,
                "logical_pin": str(cell.getLogicalPinMapping(old_physical)),
            })

        if java_swaps.isEmpty():
            return {
                "status": "no_optimization",
                "cell": cell_name,
                "message": "No actionable LUT pin swaps were queued",
                "moves": move_details,
            }

        swap_count = int(LUTTools.swapMultipleLutPins(java_swaps))
        unrouted_nets = []
        if unroute_affected:
            for net_name, net in affected_nets.items():
                try:
                    net.unroute()
                    unrouted_nets.append(net_name)
                except Exception as e:
                    logger.warning("Could not unroute affected net %s: %s", net_name, e)

        after_mapping = _java_string_map_to_dict(cell.getPinMappingsP2L())
        try:
            after_equation = str(LUTTools.getLUTEquation(cell))
        except Exception:
            after_equation = None

        for detail in move_details:
            if detail.get("status") == "queued":
                detail["status"] = "swapped"

        return {
            "status": "success",
            "cell": cell_name,
            "cell_type": cell_type,
            "site": str(cell.getSiteName()),
            "bel": str(cell.getBELName()),
            "requested_swaps": len(normalized_swaps),
            "processed_swaps": swap_count,
            "unroute_affected": unroute_affected,
            "unrouted_nets": sorted(unrouted_nets),
            "before_mapping_p2l": before_mapping,
            "after_mapping_p2l": after_mapping,
            "before_equation": before_equation,
            "after_equation": after_equation,
            "moves": move_details,
        }

    except Exception as e:
        logger.error("Error swapping LUT pins on %s: %s", cell_name, e)
        return {"error": str(e)}


def optimize_fanout(net_name: str, split_factor: int) -> Dict[str, Any]:
    """
    Optimize high fanout nets by splitting them into multiple driven nets.

    This optimization reduces fanout by replicating the source driver and dividing
    the loads among multiple copies. This can improve timing and routability for
    nets with very high fanout.

    Args:
        net_name: Name of the high fanout net to optimize
        split_factor: Number of copies to create (k) - net will be split into k parts

    Returns:
        Dictionary with optimization results
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}

    if _current_design is None:
        return {"error": "No design loaded. Use load_design first."}

    try:
        from com.xilinx.rapidwright.eco import FanOutOptimization

        design = _current_design

        # Get the net
        net = design.getNet(net_name)
        if net is None:
            return {"error": f"Net '{net_name}' not found in design"}

        # Pre-checks to avoid java.lang.NullPointerException inside
        # FanOutOptimization.cutFanOutOfRoutedNet. The RapidWright ECO API
        # assumes the net has a replicable logic driver and a placed source
        # site pin; global resources (clock enable buffers, global clock
        # buffers, primary IO buffers) and static nets violate these
        # assumptions and crash with NPE deep inside the library.

        # Static nets (VCC/GND) cannot be replicated
        try:
            if net.isStaticNet():
                return {"error": f"Net '{net_name}' is a static net (VCC/GND); cannot optimize fanout"}
        except Exception:
            pass  # isStaticNet may not exist on older RapidWright versions

        source = net.getSource()
        if source is None:
            return {
                "error": (
                    f"Net '{net_name}' has no source site pin — may be a top-level "
                    f"port, an unrouted net, or a net driven by a dedicated resource "
                    f"without an accessible site pin. Fanout optimization requires a "
                    f"placed, replicable driver."
                )
            }

        try:
            site_inst = source.getSiteInst()
        except Exception:
            site_inst = None
        if site_inst is None:
            return {
                "error": f"Net '{net_name}' source has no placed site instance; cannot replicate driver"
            }

        # FanOutOptimization creates sibling logical nets from the physical
        # net name. In some routed checkpoints that physical hierarchy has no
        # corresponding EDIF parent instance; the Java ECO otherwise moves the
        # original driver before failing in Design.createNet(). Reject that
        # target before any mutation so the private auto selector can safely
        # reload the authoritative seed and try its next live target.
        physical_name = str(net.getName())
        if "/" in physical_name:
            physical_parent = physical_name.rsplit("/", 1)[0]
            try:
                logical_parent = design.getNetlist().getHierCellInstFromName(
                    physical_parent
                )
            except Exception:
                logical_parent = None
            if logical_parent is None:
                return {
                    "error": (
                        "hierarchy_delivery_unavailable: physical net parent "
                        "is absent from the logical EDIF hierarchy"
                    ),
                    "backend_compatibility": "unsupported_for_observed_target",
                    "failure_class": "hierarchy_delivery_unavailable",
                }

        try:
            logical_net = net.getLogicalHierNet()
        except Exception:
            logical_net = None
        if logical_net is None:
            return {
                "error": "hierarchy_delivery_unavailable: net has no logical EDIF net",
                "backend_compatibility": "unsupported_for_observed_target",
                "failure_class": "hierarchy_delivery_unavailable",
            }

        # Mirror the exact RapidWright FanOutOptimization source contract so a
        # non-replicable CARRY/DSP/BRAM target returns a structured capability
        # failure instead of throwing deep inside Java after Luna selected it.
        driver_type = ""
        try:
            for port_inst in logical_net.getLeafHierPortInsts():
                if port_inst.isOutput():
                    driver_cell = port_inst.getPhysicalCell(design)
                    if driver_cell is not None:
                        driver_type = str(driver_cell.getType())
                        break
        except Exception:
            driver_type = ""
        supported_driver_types = {
            "FDRE", "FDSE", "FDCE", "FDPE",
            "LUT1", "LUT2", "LUT3", "LUT4", "LUT5", "LUT6",
        }
        if not driver_type:
            return {
                "error": (
                    "physical_driver_mapping_unavailable: logical net has no "
                    "replicable physical LUT/FF driver"
                ),
                "backend_compatibility": "unsupported_for_observed_target",
                "failure_class": "physical_driver_mapping_unavailable",
            }
        if driver_type and driver_type not in supported_driver_types:
            return {
                "error": (
                    f"Unsupported driver cell type for fanout_opt: {driver_type}; "
                    "this backend supports only FDRE/FDSE/FDCE/FDPE and LUT1..LUT6"
                ),
                "backend_compatibility": "unsupported_for_observed_target",
                "driver_type": driver_type,
            }

        # Reject dedicated resources that aren't replicable as ordinary logic.
        # BUFCE_LEAF and BUFCE are the common culprits for clock-enable nets
        # like 'ccm_inst/CEB1' that previously crashed with NPE.
        try:
            site_type = str(site_inst.getSiteTypeEnum())
        except Exception:
            site_type = ""
        dedicated_markers = (
            "BUFCE", "BUFG", "BUFR", "BUFMR", "BUFGCTRL",
            "IBUF", "OBUF", "IOB", "HPIOB", "HDIOB",
            "MMCM", "PLL", "IBUFDS", "OBUFDS",
        )
        if any(marker in site_type for marker in dedicated_markers):
            return {
                "error": (
                    f"Net '{net_name}' source is a dedicated resource "
                    f"({site_type}); fanout optimization only supports replicable "
                    f"logic drivers (LUT/FF in SLICE sites)."
                )
            }

        original_fanout = net.getFanOut()
        if original_fanout < 2:
            return {"error": f"Net '{net_name}' fanout is {original_fanout}; nothing to split"}
        if split_factor < 2:
            return {"error": f"split_factor must be >= 2, got {split_factor}"}

        logger.info(f"Optimizing net '{net_name}' with fanout {original_fanout} into {split_factor} parts")

        # Perform optimization
        FanOutOptimization.cutFanOutOfRoutedNet(design, net, split_factor)

        # Collect info about the new nets created
        # The optimization creates multiple nets by replicating the source
        new_nets_info = []

        # Try to find the replicated nets (they will have similar names)
        base_name = net_name
        for design_net in design.getNets():
            net_str = str(design_net.getName())
            if base_name in net_str and net_str != net_name:
                new_nets_info.append({
                    "name": net_str,
                    "fanout": design_net.getFanOut()
                })
                if len(new_nets_info) >= split_factor:
                    break

        return {
            "status": "success",
            "net_name": net_name,
            "original_fanout": original_fanout,
            "split_factor": split_factor,
            "new_nets": new_nets_info,
            "message": f"Successfully split net '{net_name}' into {split_factor} parts"
        }

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"Error in fanout optimization for net '{net_name}':\n{tb}")
        return {"error": f"{type(e).__name__}: {e}"}


def analyze_fabric_for_pblock(
    target_lut_count: int,
    target_ff_count: int,
    target_dsp_count: int = 0,
    target_bram_count: int = 0,
    target_uram_count: int = 0,
    device_name: Optional[str] = None
) -> Dict[str, Any]:
    """
    Analyze the FPGA fabric to find the best contiguous region for a pblock.

    Identifies regions that:
    1. Have enough resources (SLICEs, DSPs, BRAMs, URAMs) for the target utilization
    2. Minimize crossing of delay-heavy columns (URAM, IO, etc.)
    3. Are as contiguous as possible

    Args:
        target_lut_count: Required number of LUTs (1.5x current usage)
        target_ff_count: Required number of FFs (1.5x current usage)
        target_dsp_count: Required number of DSPs (1.5x current usage)
        target_bram_count: Required number of BRAMs (1.5x current usage)
        target_uram_count: Required number of URAMs (1.5x current usage)
        device_name: Device name (uses loaded design's device if omitted)

    Returns:
        Dictionary with recommended pblock ranges and analysis
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}

    try:
        from com.xilinx.rapidwright.device import Device, TileTypeEnum

        # Get the device
        if device_name:
            device = Device.getDevice(device_name)
        elif _current_design:
            device = _current_design.getDevice()
        else:
            return {"error": "No device specified and no design loaded"}

        logger.info(f"Analyzing fabric for device: {device.getName()}")

        # Get all tiles
        tiles = device.getAllTiles()

        # Check for delay-heavy tile types by name pattern
        # These tile types cause routing delays when crossed
        def is_delay_heavy_tile(tile_type_enum) -> bool:
            tile_type_name = str(tile_type_enum.name())
            delay_patterns = ['URAM', 'HPIO', 'HDIO', 'HRIO']
            return any(pattern in tile_type_name for pattern in delay_patterns)

        # Map tile columns/rows to resource counts and types
        column_info = {}  # col -> {good_tiles, bad_tiles, resources}
        row_info = {}     # row -> {good_tiles, bad_tiles, resources}
        # Per-tile site counts for later rectangle-accurate counting.
        # The column/row aggregates above are *totals* across the full
        # device axis, so they can't answer "how many DSP sites are in
        # this rectangle" — we need a 2D map.
        tile_sites = {}   # (col, row) -> per-resource site counts

        min_col, max_col = float('inf'), 0
        min_row, max_row = float('inf'), 0

        for tile in tiles:
            tile_type = tile.getTileTypeEnum()
            col = tile.getColumn()
            row = tile.getRow()

            # Track column/row bounds
            min_col = min(min_col, col)
            max_col = max(max_col, col)
            min_row = min(min_row, row)
            max_row = max(max_row, row)

            # Initialize column/row info
            if col not in column_info:
                column_info[col] = {
                    "good_tiles": 0,
                    "bad_tiles": 0,
                    "slice_sites": 0,
                    "dsp_sites": 0,
                    "bram_sites": 0,
                    "uram_sites": 0,
                }

            if row not in row_info:
                row_info[row] = {
                    "good_tiles": 0,
                    "bad_tiles": 0,
                    "slice_sites": 0,
                    "dsp_sites": 0,
                    "bram_sites": 0,
                    "uram_sites": 0,
                }

            # Categorize tile
            is_bad = is_delay_heavy_tile(tile_type)

            if is_bad:
                column_info[col]["bad_tiles"] += 1
                row_info[row]["bad_tiles"] += 1
            else:
                column_info[col]["good_tiles"] += 1
                row_info[row]["good_tiles"] += 1

            # Resource capacity is independent of whether the column is a
            # routing-delay boundary.  In particular URAM tiles are classified
            # as delay-heavy above, so counting sites only in the ``else`` arm
            # both lost their capacity and left ``est_uram_sites`` undefined.
            sites = tile.getSites()
            if sites:
                slice_n = 0
                dsp_n = 0
                bram_n = 0
                uram_n = 0
                for site in sites:
                    site_type_str = str(site.getSiteTypeEnum())

                    if "SLICE" in site_type_str:
                        slice_n += 1
                    elif "DSP" in site_type_str:
                        dsp_n += 1
                    elif "RAMB" in site_type_str or "BRAM" in site_type_str:
                        bram_n += 1
                    elif "URAM" in site_type_str:
                        uram_n += 1

                column_info[col]["slice_sites"] += slice_n
                column_info[col]["dsp_sites"] += dsp_n
                column_info[col]["bram_sites"] += bram_n
                column_info[col]["uram_sites"] += uram_n
                row_info[row]["slice_sites"] += slice_n
                row_info[row]["dsp_sites"] += dsp_n
                row_info[row]["bram_sites"] += bram_n
                row_info[row]["uram_sites"] += uram_n

                if slice_n or dsp_n or bram_n or uram_n:
                    tile_sites[(col, row)] = {
                        "slices": slice_n,
                        "dsps": dsp_n,
                        "brams": bram_n,
                        "urams": uram_n,
                    }

        # Find contiguous column ranges with minimal bad columns
        good_columns = []
        for col in sorted(column_info.keys()):
            info = column_info[col]
            # A "good" column has mostly resource tiles, not delay-heavy tiles
            if info["good_tiles"] > info["bad_tiles"] * 2:  # 2:1 ratio
                good_columns.append(col)

        # xcvu3p's URAM sites occupy dedicated columns, which are deliberately
        # classified as delay-heavy.  A URAM-bearing design must therefore be
        # allowed to span those columns; the hard-capacity loop below still
        # keeps the selected rectangle as tight as possible.
        if target_uram_count > 0:
            good_columns = sorted(column_info.keys())

        # Find the longest contiguous range of good columns
        best_col_range = None
        best_col_resources = {"slices": 0, "dsps": 0, "brams": 0, "urams": 0}

        current_range = []
        current_resources = {"slices": 0, "dsps": 0, "brams": 0, "urams": 0}

        for col in good_columns:
            if not current_range or col == current_range[-1] + 1:
                # Continue the range
                current_range.append(col)
                current_resources["slices"] += column_info[col]["slice_sites"]
                current_resources["dsps"] += column_info[col]["dsp_sites"]
                current_resources["brams"] += column_info[col]["bram_sites"]
                current_resources["urams"] += column_info[col]["uram_sites"]
            else:
                # Gap found - check if current range is better
                if (not best_col_range or
                    len(current_range) > len(best_col_range) or
                    (len(current_range) == len(best_col_range) and
                     current_resources["slices"] > best_col_resources["slices"])):
                    best_col_range = current_range
                    best_col_resources = current_resources.copy()

                # Start new range
                current_range = [col]
                current_resources = {
                    "slices": column_info[col]["slice_sites"],
                    "dsps": column_info[col]["dsp_sites"],
                    "brams": column_info[col]["bram_sites"],
                    "urams": column_info[col]["uram_sites"],
                }

        # Check final range
        if (not best_col_range or
            len(current_range) > len(best_col_range) or
            (len(current_range) == len(best_col_range) and
             current_resources["slices"] > best_col_resources["slices"])):
            best_col_range = current_range
            best_col_resources = current_resources.copy()

        if not best_col_range:
            return {"error": "No suitable contiguous column range found"}

        # Similar analysis for rows
        good_rows = []
        for row in sorted(row_info.keys()):
            info = row_info[row]
            if info["good_tiles"] > info["bad_tiles"] * 2:
                good_rows.append(row)

        # Find best row range
        best_row_range = None
        best_row_resources = {"slices": 0, "dsps": 0, "brams": 0}

        current_range = []
        current_resources = {"slices": 0, "dsps": 0, "brams": 0}

        for row in good_rows:
            if not current_range or row == current_range[-1] + 1:
                current_range.append(row)
                current_resources["slices"] += row_info[row]["slice_sites"]
                current_resources["dsps"] += row_info[row]["dsp_sites"]
                current_resources["brams"] += row_info[row]["bram_sites"]
            else:
                if (not best_row_range or
                    len(current_range) > len(best_row_range) or
                    (len(current_range) == len(best_row_range) and
                     current_resources["slices"] > best_row_resources["slices"])):
                    best_row_range = current_range
                    best_row_resources = current_resources.copy()

                current_range = [row]
                current_resources = {
                    "slices": row_info[row]["slice_sites"],
                    "dsps": row_info[row]["dsp_sites"],
                    "brams": row_info[row]["bram_sites"]
                }

        if (not best_row_range or
            len(current_range) > len(best_row_range) or
            (len(current_range) == len(best_row_range) and
             current_resources["slices"] > best_row_resources["slices"])):
            best_row_range = current_range
            best_row_resources = current_resources.copy()

        if not best_row_range:
            return {"error": "No suitable contiguous row range found"}

        # Calculate center of the best region
        col_center = (best_col_range[0] + best_col_range[-1]) // 2
        row_center = (best_row_range[0] + best_row_range[-1]) // 2

        # Convert logical targets into a conservative site requirement from
        # the current device family.  7-series SLICE sites contain four LUTs
        # and eight FFs; UltraScale/UltraScale+/Versal sites contain eight LUTs
        # and sixteen FFs.  This replaces the old benchmark-sized fixed window.
        normalized_device_name = str(device.getName()).lower()
        if normalized_device_name.startswith("xc7"):
            luts_per_slice, ffs_per_slice = 4, 8
        else:
            luts_per_slice, ffs_per_slice = 8, 16
        required_slices = max(
            (max(0, target_lut_count) + luts_per_slice - 1) // luts_per_slice,
            (max(0, target_ff_count) + ffs_per_slice - 1) // ffs_per_slice,
        )

        # Find actual placed cells to determine center of mass
        center_of_mass_col = col_center
        center_of_mass_row = row_center

        if _current_design:
            placed_cols = []
            placed_rows = []
            for cell in _current_design.getCells():
                if cell.isPlaced():
                    site = cell.getSite()
                    if site:
                        tile = site.getTile()
                        placed_cols.append(tile.getColumn())
                        placed_rows.append(tile.getRow())

            if placed_cols:
                center_of_mass_col = sum(placed_cols) // len(placed_cols)
                center_of_mass_row = sum(placed_rows) // len(placed_rows)
                logger.info(f"Center of mass: col={center_of_mass_col}, row={center_of_mass_row}")

        # Grow one capacity-safe rectangle from the current placement's center
        # of mass.  Every growth choice is scored from exact device resources
        # and the current design's requirements; there are no benchmark-sized
        # windows or imported floorplan dimensions.
        col_start_idx = min(
            range(len(best_col_range)),
            key=lambda index: abs(best_col_range[index] - center_of_mass_col),
        )
        row_start_idx = min(
            range(len(best_row_range)),
            key=lambda index: abs(best_row_range[index] - center_of_mass_row),
        )
        col_left_idx = col_right_idx = col_start_idx
        row_bottom_idx = row_top_idx = row_start_idx

        def count_in_rect(c_min, c_max, r_min, r_max):
            s = d = b = u = 0
            for (c, r), cnt in tile_sites.items():
                if c_min <= c <= c_max and r_min <= r <= r_max:
                    s += cnt["slices"]
                    d += cnt["dsps"]
                    b += cnt["brams"]
                    u += cnt["urams"]
            return s, d, b, u

        def counts_for_indices(left, right, bottom, top):
            return count_in_rect(
                best_col_range[left],
                best_col_range[right],
                best_row_range[bottom],
                best_row_range[top],
            )

        def capacity_score(counts):
            requirements = (
                required_slices,
                max(0, target_dsp_count),
                max(0, target_bram_count),
                max(0, target_uram_count),
            )
            return sum(
                1.0 if required <= 0 else min(1.0, count / float(required))
                for count, required in zip(counts, requirements)
            )

        def capacity_holds(counts):
            return (
                counts[0] >= required_slices
                and counts[1] >= target_dsp_count
                and counts[2] >= target_bram_count
                and counts[3] >= target_uram_count
            )

        actual_slices, actual_dsps, actual_brams, actual_urams = (
            counts_for_indices(
                col_left_idx, col_right_idx, row_bottom_idx, row_top_idx
            )
        )
        max_widen_iters = 2 * (len(best_col_range) + len(best_row_range))
        for _ in range(max_widen_iters):
            current_counts = (
                actual_slices, actual_dsps, actual_brams, actual_urams
            )
            if capacity_holds(current_counts):
                break

            proposals = []
            if col_left_idx > 0:
                proposals.append((col_left_idx - 1, col_right_idx, row_bottom_idx, row_top_idx))
            if col_right_idx < len(best_col_range) - 1:
                proposals.append((col_left_idx, col_right_idx + 1, row_bottom_idx, row_top_idx))
            if row_bottom_idx > 0:
                proposals.append((col_left_idx, col_right_idx, row_bottom_idx - 1, row_top_idx))
            if row_top_idx < len(best_row_range) - 1:
                proposals.append((col_left_idx, col_right_idx, row_bottom_idx, row_top_idx + 1))
            if not proposals:
                break

            ranked = []
            for proposal in proposals:
                counts = counts_for_indices(*proposal)
                left, right, bottom, top = proposal
                area = (right - left + 1) * (top - bottom + 1)
                aspect_imbalance = abs((right - left) - (top - bottom))
                ranked.append((capacity_score(counts), -area, -aspect_imbalance, proposal, counts))
            _, _, _, selected, selected_counts = max(ranked, key=lambda item: item[:3])
            col_left_idx, col_right_idx, row_bottom_idx, row_top_idx = selected
            actual_slices, actual_dsps, actual_brams, actual_urams = selected_counts

        final_col_min = best_col_range[col_left_idx]
        final_col_max = best_col_range[col_right_idx]
        final_row_min = best_row_range[row_bottom_idx]
        final_row_max = best_row_range[row_top_idx]

        # Hard failure: no expansion of the selected fabric can hold the design.
        if actual_slices < required_slices:
            return {
                "error": (
                    f"No contiguous region of the selected fabric can hold "
                    f"{required_slices} SLICE sites (best we could fit: {actual_slices})."
                ),
                "target_requirements": {
                    "luts": target_lut_count,
                    "ffs": target_ff_count,
                    "dsps": target_dsp_count,
                    "brams": target_bram_count,
                    "urams": target_uram_count,
                },
            }
        if target_dsp_count > 0 and actual_dsps < target_dsp_count:
            return {
                "error": (
                    f"No contiguous region of the selected fabric can hold "
                    f"{target_dsp_count} DSPs (best we could fit: {actual_dsps}). "
                    f"The device may have insufficient DSPs or they are all "
                    f"outside the delay-clean fabric region."
                ),
                "target_requirements": {
                    "luts": target_lut_count,
                    "ffs": target_ff_count,
                    "dsps": target_dsp_count,
                    "brams": target_bram_count,
                },
                "best_region_tried": {
                    "col_min": int(final_col_min),
                    "col_max": int(final_col_max),
                    "row_min": int(final_row_min),
                    "row_max": int(final_row_max),
                    "actual_slice_sites": int(actual_slices),
                    "actual_dsp_sites": int(actual_dsps),
                    "actual_bram_sites": int(actual_brams),
                },
            }
        if target_bram_count > 0 and actual_brams < target_bram_count:
            return {
                "error": (
                    f"No contiguous region of the selected fabric can hold "
                    f"{target_bram_count} BRAMs (best we could fit: {actual_brams})."
                ),
                "target_requirements": {
                    "luts": target_lut_count,
                    "ffs": target_ff_count,
                    "dsps": target_dsp_count,
                    "brams": target_bram_count,
                },
                "best_region_tried": {
                    "col_min": int(final_col_min),
                    "col_max": int(final_col_max),
                    "row_min": int(final_row_min),
                    "row_max": int(final_row_max),
                    "actual_slice_sites": int(actual_slices),
                    "actual_dsp_sites": int(actual_dsps),
                    "actual_bram_sites": int(actual_brams),
                },
            }
        if target_uram_count > 0 and actual_urams < target_uram_count:
            return {
                "error": (
                    f"No contiguous region of the selected fabric can hold "
                    f"{target_uram_count} URAMs (best we could fit: {actual_urams})."
                ),
                "target_requirements": {
                    "luts": target_lut_count,
                    "ffs": target_ff_count,
                    "dsps": target_dsp_count,
                    "brams": target_bram_count,
                    "urams": target_uram_count,
                },
                "best_region_tried": {
                    "col_min": int(final_col_min),
                    "col_max": int(final_col_max),
                    "row_min": int(final_row_min),
                    "row_max": int(final_row_max),
                    "actual_slice_sites": int(actual_slices),
                    "actual_dsp_sites": int(actual_dsps),
                    "actual_bram_sites": int(actual_brams),
                    "actual_uram_sites": int(actual_urams),
                },
            }

        # Recompute selected_cols/rows after any widening
        selected_cols = col_right_idx - col_left_idx + 1
        selected_rows = row_top_idx - row_bottom_idx + 1

        # Use actual counts (not projections) in the response
        est_slice_sites = int(actual_slices)
        est_dsp_sites = int(actual_dsps)
        est_bram_sites = int(actual_brams)
        est_uram_sites = int(actual_urams)

        return {
            "status": "success",
            "device": str(device.getName()),
            "fabric_bounds": {
                "min_col": int(min_col),
                "max_col": int(max_col),
                "min_row": int(min_row),
                "max_row": int(max_row)
            },
            "recommended_region": {
                "col_min": int(final_col_min),
                "col_max": int(final_col_max),
                "row_min": int(final_row_min),
                "row_max": int(final_row_max),
                "center_col": int(col_center),
                "center_row": int(row_center),
                "center_of_mass_col": int(center_of_mass_col),
                "center_of_mass_row": int(center_of_mass_row),
                "contiguous_columns": selected_cols,
                "contiguous_rows": selected_rows
            },
            "estimated_resources": {
                "slice_sites": est_slice_sites,
                "dsp_sites": est_dsp_sites,
                "bram_sites": est_bram_sites,
                "uram_sites": est_uram_sites,
                "approx_luts": est_slice_sites * 4,
                "approx_ffs": est_slice_sites * 8
            },
            "target_requirements": {
                "luts": target_lut_count,
                "ffs": target_ff_count,
                "dsps": target_dsp_count,
                "brams": target_bram_count,
                "urams": target_uram_count,
            },
            "message": f"Found region around center of mass: cols {final_col_min}-{final_col_max}, rows {final_row_min}-{final_row_max}"
        }

    except Exception as e:
        logger.error(f"Error analyzing fabric: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e)}


def analyze_critical_path_spread(
    critical_paths_data: list = None,
    input_file: str = None,
) -> Dict[str, Any]:
    """
    Calculate Manhattan distances for cells on critical paths.

    Takes critical path data from Vivado (list of cell names per path) and uses
    RapidWright's device model to get accurate tile coordinates and calculate distances.

    Args:
        critical_paths_data: List of paths, where each path is a list of cell names
        input_file: Optional path to JSON file containing critical_paths_data

    Returns:
        Dictionary with spread analysis including max distances per path

    Note: Either critical_paths_data or input_file must be provided
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}

    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}

    # Load data from file if specified
    if input_file:
        try:
            import json
            with open(input_file, 'r') as f:
                critical_paths_data = json.load(f)
        except Exception as e:
            return {"error": f"Error reading input file: {str(e)}"}

    if not critical_paths_data:
        return {"error": "No critical path data provided. Specify either critical_paths_data or input_file"}

    try:
        design = _current_design
        device = design.getDevice()

        logger.info(f"Analyzing {len(critical_paths_data)} critical paths for cell spread")

        path_results = []
        all_max_distances = []

        for path_idx, cell_names in enumerate(critical_paths_data):
            # Get placements for cells in this path
            cell_locations = []

            for cell_name in cell_names:
                try:
                    cell = design.getCell(cell_name)
                    if cell and cell.isPlaced():
                        site = cell.getSite()
                        if site:
                            tile = site.getTile()
                            cell_locations.append({
                                "cell": str(cell.getName()),
                                "type": str(cell.getType()),
                                "tile": str(tile.getName()),
                                "col": tile.getColumn(),
                                "row": tile.getRow()
                            })
                except Exception as e:
                    logger.debug(f"Could not get location for cell {cell_name}: {e}")
                    continue

            if len(cell_locations) < 2:
                continue

            # Calculate maximum Manhattan distance between SEQUENTIAL cells on this path
            max_distance = 0
            max_pair = None

            for i in range(len(cell_locations) - 1):
                loc1 = cell_locations[i]
                loc2 = cell_locations[i + 1]
                distance = abs(loc1["col"] - loc2["col"]) + abs(loc1["row"] - loc2["row"])

                if distance > max_distance:
                    max_distance = distance
                    max_pair = (loc1, loc2)

            all_max_distances.append(max_distance)

            path_results.append({
                "path_num": path_idx + 1,
                "cell_count": len(cell_locations),
                "max_distance": max_distance,
                "max_pair": max_pair
            })

        if not all_max_distances:
            return {
                "status": "warning",
                "message": "No cell location data found for paths",
                "paths_analyzed": len(critical_paths_data)
            }

        # Calculate statistics
        max_dist = max(all_max_distances)
        avg_dist = sum(all_max_distances) / len(all_max_distances)

        # Sort by distance
        path_results.sort(key=lambda x: -x["max_distance"])

        return {
            "status": "success",
            "paths_analyzed": len(critical_paths_data),
            "max_distance_found": int(max_dist),
            "avg_max_distance": float(avg_dist),
            "path_distances": [int(d) for d in all_max_distances],
            "worst_paths": path_results[:10]  # Top 10 worst
        }

    except Exception as e:
        logger.error(f"Error analyzing critical path spread: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e)}


def compare_design_structure(golden_dcp: str, revised_dcp: str) -> Dict[str, Any]:
    """
    Compare structural properties of two design checkpoints.

    Performs sanity checks to catch obvious errors:
    - Top-level module name
    - I/O port names, directions, and widths
    - Device compatibility

    Cell counts are reported for context but are not pass/fail criteria.

    Args:
        golden_dcp: Path to the golden (reference) DCP file
        revised_dcp: Path to the revised (optimized) DCP file

    Returns:
        Dictionary with comparison results including pass/fail status
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}

    try:
        from com.xilinx.rapidwright.design import Design
        from pathlib import Path
        import json

        golden_path = Path(golden_dcp).expanduser().resolve()
        revised_path = Path(revised_dcp).expanduser().resolve()

        if not golden_path.exists():
            return {"error": f"Golden DCP not found: {golden_dcp}"}
        if not revised_path.exists():
            return {"error": f"Revised DCP not found: {revised_dcp}"}

        logger.info(f"Loading golden design from {golden_path}")
        golden = Design.readCheckpoint(str(golden_path))

        logger.info(f"Loading revised design from {revised_path}")
        revised = Design.readCheckpoint(str(revised_path))

        issues = []
        checks_passed = 0
        checks_total = 0

        # Check 1: Top-level module name
        checks_total += 1
        golden_top = str(golden.getName())
        revised_top = str(revised.getName())
        if golden_top == revised_top:
            checks_passed += 1
        else:
            issues.append(f"Top module name mismatch: '{golden_top}' vs '{revised_top}'")

        # Check 2: Device compatibility
        checks_total += 1
        golden_device = str(golden.getDevice().getName())
        revised_device = str(revised.getDevice().getName())
        if golden_device == revised_device:
            checks_passed += 1
        else:
            issues.append(f"Device mismatch: '{golden_device}' vs '{revised_device}'")

        # Check 3: I/O ports (names, directions, widths)
        golden_netlist = golden.getNetlist()
        revised_netlist = revised.getNetlist()

        golden_top_cell = golden_netlist.getTopCell()
        revised_top_cell = revised_netlist.getTopCell()

        # Get port information
        golden_ports = {}
        for port in golden_top_cell.getPorts():
            port_name = str(port.getName())
            port_dir = str(port.getDirection())
            port_width = port.getWidth()
            golden_ports[port_name] = {"direction": port_dir, "width": port_width}

        revised_ports = {}
        for port in revised_top_cell.getPorts():
            port_name = str(port.getName())
            port_dir = str(port.getDirection())
            port_width = port.getWidth()
            revised_ports[port_name] = {"direction": port_dir, "width": port_width}

        # Compare ports
        checks_total += 1
        port_issues = []

        # Check for missing/added ports
        golden_port_names = set(golden_ports.keys())
        revised_port_names = set(revised_ports.keys())

        missing_ports = golden_port_names - revised_port_names
        added_ports = revised_port_names - golden_port_names

        if missing_ports:
            port_issues.append(f"Missing ports in revised: {', '.join(sorted(missing_ports))}")
        if added_ports:
            port_issues.append(f"Added ports in revised: {', '.join(sorted(added_ports))}")

        # Check common ports for direction/width mismatches
        common_ports = golden_port_names & revised_port_names
        for port_name in sorted(common_ports):
            g_info = golden_ports[port_name]
            r_info = revised_ports[port_name]

            if g_info["direction"] != r_info["direction"]:
                port_issues.append(
                    f"Port '{port_name}' direction mismatch: "
                    f"{g_info['direction']} vs {r_info['direction']}"
                )

            if g_info["width"] != r_info["width"]:
                port_issues.append(
                    f"Port '{port_name}' width mismatch: "
                    f"{g_info['width']} vs {r_info['width']}"
                )

        if not port_issues:
            checks_passed += 1
        else:
            issues.extend(port_issues)

        # Summary - only count real issues (not INFO)
        real_issues = [i for i in issues if not i.startswith("INFO:")]
        all_checks_passed = (checks_passed == checks_total)

        # Cell counts are reported for information only; they are intentionally
        # NOT part of the pass/fail checks above (the cell-count check was
        # removed because optimization legitimately changes cell counts).
        golden_cell_count = golden.getCells().size()
        revised_cell_count = revised.getCells().size()

        result = {
            "status": "success",
            "comparison_result": "PASS" if all_checks_passed else "FAIL",
            "checks_passed": checks_passed,
            "checks_total": checks_total,
            "golden_design": {
                "path": str(golden_path),
                "top_module": golden_top,
                "device": golden_device,
                "cell_count": golden_cell_count,
                "port_count": len(golden_ports)
            },
            "revised_design": {
                "path": str(revised_path),
                "top_module": revised_top,
                "device": revised_device,
                "cell_count": revised_cell_count,
                "port_count": len(revised_ports)
            },
            "issues": issues
        }

        # Restore original design if it was loaded
        global _current_design
        if _current_design:
            _current_design = revised  # Keep revised loaded for potential further use

        return result

    except Exception as e:
        logger.error(f"Error comparing designs: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e)}


def _get_cell_physical_nets(design, cell):
    """
    Get all physical (non-static, non-clock) nets connected to a specific cell.

    Uses EDIFHierPortInst.getRoutedSitePinInst() to resolve each logical pin
    to its physical SitePinInst, then reads the Net from that pin.

    Args:
        design: RapidWright Design object
        cell: RapidWright Cell object (must be placed)

    Returns:
        List of Net objects connected to this cell
    """
    net_names = set()
    nets = []

    hier_cell = cell.getEDIFHierCellInst()
    if hier_cell is None:
        return []
    for ehpi in hier_cell.getHierPortInsts():
        spi = ehpi.getRoutedSitePinInst(design)
        if spi is None:
            continue
        net = spi.getNet()
        if net is not None and not net.isStaticNet() and not net.isClockNet():
            name = str(net.getName())
            if name not in net_names:
                net_names.add(name)
                nets.append(net)
    return nets

def _compute_routed_path_length(net, sink_pin):
    """
    Compute the routed path length from a net's source to a specific sink pin.

    Walks backwards from the sink pin node to the source pin node, summing
    Manhattan tile distances along the way at each PIP.

    Args:
        net: RapidWright Net object (must be routed, i.e. have PIPs)
        sink_pin: RapidWright SitePinInst object for the sink site pin

    Returns:
        Routed path length in tile Manhattan distance units, or -1 if not found
    """
    pips = net.getPIPs()
    if pips is None or pips.size() == 0:
        return -1

    # Build a Node map to use as a lookup as we traverse nodes backwards from the
    # sink pin to the source pin.
    node_map = {}
    for pip in pips:
        if pip.isReversed():
            end_node, start_node = pip.getStartNode(), pip.getEndNode()
        else:
            end_node, start_node = pip.getEndNode(), pip.getStartNode()
        if end_node is not None and start_node is not None:
            node_map[end_node] = start_node

    src_pin = net.getSource()
    if src_pin is None:
        return -1
    source_node = src_pin.getConnectedNode()
    sink_node = sink_pin.getConnectedNode()
    if source_node is None or sink_node is None:
        return -1

    # Traverse backwards (sink to source), accumulate Manhattan node (tile-to-tile) distances along the way
    length = 0
    node = sink_node
    while node is not None and node != source_node:
        prev = node_map.get(node)
        if prev is None:
            return -1
        length += node.getTile().getManhattanDistance(prev.getTile())
        node = prev

    return length if node == source_node else -1

def _detour_ratio(net, sink_pin):
    """Return the detour ratio for the provided sink back to the source of the provided net.

        Args:
        net: RapidWright Net object (must be routed, i.e. have PIPs)
        sink_pin: RapidWright SitePinInst object for the sink site pin
    """
    src_pin = net.getSource()
    if src_pin is None or src_pin.getSite() is None:
        return -1
    sink_site = sink_pin.getSite()
    if sink_site is None:
        return -1

    dist = src_pin.getTile().getManhattanDistance(sink_site.getTile())
    if dist == 0:
        return -1
    routed_length = _compute_routed_path_length(net, sink_pin)
    if routed_length <= 0:
        return -1

    return routed_length / dist

def analyze_net_detour(
    critical_paths_data: list = None,
    detour_threshold: float = 2.0,
    input_file: str = None,
) -> Dict[str, Any]:
    """
    Analysis of routing for large detours on critical paths.

    For each interior cell on a critical path, computes the detour ratio of
    the incoming net (feeding the cell) and the outgoing net (driven by it).
    A high ratio on either side indicates the cell may benefit from
    re-placement closer to its connections.

    Input is a list of pins on the path as produced by Vivado MCP Server's
    extract_critical_path_pins:
        ["src_ff/Q", "lut1/I2", "lut1/O", "lut2/I0", "lut2/O", "dst_ff/D"]

    Args:
        critical_paths_data: List of paths, each a list of pin-path strings
                             from extract_critical_path_pins
        detour_threshold: Flag cells with max detour ratio above this (default: 2.0)
        input_file: Optional JSON file path containing critical_paths_data

    Returns:
        Dictionary with per-cell detour analysis and ranked re-placement candidates
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}

    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}

    if input_file:
        try:
            import json
            with open(input_file, 'r') as f:
                critical_paths_data = json.load(f)
        except Exception as e:
            return {"error": f"Error reading input file: {str(e)}"}

    if not critical_paths_data:
        return {"error": "No critical path data provided. "
                "Specify either critical_paths_data or input_file."}

    try:
        design = _current_design
        netlist = design.getNetlist()

        logger.info(f"Analyzing {len(critical_paths_data)} critical paths for "
                     f"routing detours (threshold={detour_threshold})")

        all_cells = []
        candidates = []

        for path_idx, pin_list in enumerate(critical_paths_data):
            if len(pin_list) < 3:
                continue
            cells_on_path = []
            prev_pin = None

            # Find cells on the path
            for pin_name in pin_list:
                pin = netlist.getHierPortInstFromName(pin_name)
                if pin is None:
                    continue
                # If current pin and previous pin point to the same cell
                if prev_pin is not None and pin.getFullHierarchicalInst().equals(prev_pin.getFullHierarchicalInst()):
                    cells_on_path.append((prev_pin, pin))
                prev_pin = pin

            for (in_pin, out_pin) in cells_on_path:
                ratio = -1
                for pin in (in_pin, out_pin):
                    if pin is not None:
                        net = pin.getRoutedPhysicalNet(design)
                        if net is not None and not net.isStaticNet() and not net.isClockNet():
                            spi = pin.getRoutedSitePinInst(design)
                            if spi is not None:
                                if spi.isOutPin():
                                    for sink_spi in net.getSinkPins():
                                        cr = _detour_ratio(net, sink_spi)
                                        if cr > ratio:
                                            ratio = cr
                                else:
                                    cr = _detour_ratio(net, spi)
                                    if cr > ratio:
                                        ratio = cr
                cell_analysis = {
                    "path": path_idx + 1,
                    "cell": str(in_pin.getFullHierarchicalInst().toString()),
                    "max_detour_ratio": round(ratio, 2),
                }
                all_cells.append(cell_analysis)
                if ratio > detour_threshold:
                    candidates.append(cell_analysis)

        candidates.sort(key=lambda x: -x["max_detour_ratio"])
        all_cells.sort(key=lambda x: -x.get("max_detour_ratio", 0))

        return {
            "status": "success",
            "cells_analyzed": len(all_cells),
            "candidates_found": len(candidates),
            "detour_threshold": detour_threshold,
            "candidates": candidates,
            "all_cells": all_cells,
        }

    except Exception as e:
        logger.error(f"Error analyzing net detours: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e)}

#


def optimize_cell_placement(
    cell_names: list,
    max_candidates: int = 10,
) -> Dict[str, Any]:
    """
    Re-place cells at the centroid of their connections to reduce routing detours.

    For each candidate cell:
      1. Finds all nets connected to the cell
      2. Collects tile coordinates of every pin on those nets
      3. Computes the centroid using ECOPlacementHelper.getCentroidOfPoints()
      4. Spirals outward from the centroid to find an available SLICE site
      5. Unplaces the cell, unroutes affected nets, and re-places at the new site

    After running, write the checkpoint and load it in Vivado to re-route the
    unrouted nets with route_design.

    Args:
        cell_names: List of cell names to re-place
        max_candidates: Maximum number of cells to process (default: 10)

    Returns:
        Dictionary with per-cell re-placement results
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}

    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}

    try:
        from com.xilinx.rapidwright.design import DesignTools
        from com.xilinx.rapidwright.eco import ECOPlacementHelper
        from com.xilinx.rapidwright.placer.blockplacer import Point
        from com.xilinx.rapidwright.device import SiteTypeEnum
        from java.util import ArrayList, EnumSet, HashMap

        design = _current_design
        device = design.getDevice()
        target_site_types = EnumSet.of(SiteTypeEnum.SLICEL, SiteTypeEnum.SLICEM)

        results = []
        cells_to_process = cell_names[:max_candidates]

        for cell_name in cells_to_process:
            cell = design.getCell(cell_name)
            if cell is None:
                results.append({"cell": cell_name, "status": "error",
                                "message": f"Cell '{cell_name}' not found"})
                continue

            if not cell.isPlaced():
                results.append({"cell": cell_name, "status": "error",
                                "message": "Cell is not placed"})
                continue

            old_site = cell.getSite()
            old_tile = old_site.getTile()
            old_placement = str(old_site.getName())
            old_bel = cell.getBEL()
            is_ff = old_bel.isFF() if old_bel is not None else False
            is_lut = old_bel.isLUT() if old_bel is not None else False

            # Gather clock net before unplacing (needed for FF compatibility)
            clk_net = None
            if is_ff:
                try:
                    clk_wire = cell.getSiteWireNameFromLogicalPin("C")
                    if clk_wire:
                        clk_net = cell.getSiteInst().getNetFromSiteWire(clk_wire)
                except Exception:
                    pass

            connected_nets = _get_cell_physical_nets(design, cell)
            if not connected_nets:
                results.append({"cell": cell_name, "status": "skipped",
                                "message": "No connected nets found"})
                continue

            # Collect all pin tile locations for centroid computation
            points = ArrayList()
            for net in connected_nets:
                for pin in net.getPins():
                    try:
                        t = pin.getTile()
                        if t is not None:
                            points.add(Point(t.getColumn(), t.getRow()))
                    except Exception:
                        continue

            if points.size() < 2:
                results.append({"cell": cell_name, "status": "skipped",
                                "message": "Not enough connection points"})
                continue

            centroid_site = ECOPlacementHelper.getCentroidOfPoints(
                device, points, target_site_types
            )
            if centroid_site is None:
                results.append({"cell": cell_name, "status": "error",
                                "message": "Could not compute centroid site"})
                continue

            # --- Perform the move ---
            # 1. Unplace cell (pass None for immediate site wire cleanup)
            DesignTools.fullyUnplaceCell(cell, None)

            # 2. Unroute affected nets
            affected_net_names = []
            for net in connected_nets:
                affected_net_names.append(str(net.getName()))
                try:
                    # Note: this removes all routing on the entire net.
                    #       For incoming nets of a re-placed cell, this will also unroute
                    #       any routing going to other unrelated cells.
                    net.unroute()
                except Exception:
                    pass

            # 3. Find available site spiraling out from centroid
            new_site = None
            new_bel = None
            search_limit = 200

            for idx, candidate in enumerate(
                ECOPlacementHelper.spiralOutFrom(centroid_site)
            ):
                if idx >= search_limit:
                    break
                if design.getSiteInstFromSite(candidate) is None:
                    bel_name = "AFF" if is_ff else "A6LUT" if is_lut else str(
                        old_bel.getName()) if old_bel else "A6LUT"
                    bel = candidate.getBEL(bel_name)
                    if bel is not None:
                        new_site = candidate
                        new_bel = bel
                        break

            if new_site is None or new_bel is None:
                results.append({"cell": cell_name, "status": "error",
                                "message": "No available site near centroid"})
                continue

            # 4. Place cell at new site and route the intra-site wiring
            try:
                design.placeCell(cell, new_site, new_bel)
                cell.getSiteInst().routeSite()
            except Exception as e:
                results.append({"cell": cell_name, "status": "error",
                                "message": f"Placement failed: {e}"})
                continue

            new_placement = str(new_site.getName())
            move_distance = old_tile.getManhattanDistance(new_site.getTile())
            results.append({
                "cell": cell_name,
                "status": "success",
                "old_site": old_placement,
                "new_site": new_placement,
                "distance_moved": int(move_distance),
                "affected_nets": len(affected_net_names),
                "message": f"Moved from {old_placement} to {new_placement}"
            })

        success_count = sum(1 for r in results if r["status"] == "success")

        return {
            "status": "success",
            "cells_processed": len(cells_to_process),
            "cells_moved": success_count,
            "results": results,
            "message": (f"Re-placed {success_count}/{len(cells_to_process)} cells. "
                        "Affected nets need re-routing in Vivado via route_design.")
        }

    except Exception as e:
        logger.error(f"Error in cell placement optimization: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e)}


def optimize_critical_path_detours(
    critical_paths_data: list = None,
    input_file: str = None,
    detour_threshold: float = 2.0,
    max_candidates: int = 5,
    clock_period_ns: Optional[float] = None,
) -> Dict[str, Any]:
    """
    High-level recipe: analyze critical-path routing detours and immediately
    re-place the strongest candidate cells.

    This wraps:
      1. analyze_net_detour()
      2. optimize_cell_placement()
      3. Optional report_approx_timing() before/after

    Intended usage:
      - Vivado extract_critical_path_pins -> JSON file
      - RapidWright optimize_critical_path_detours(input_file=...)
      - RapidWright write_checkpoint()
      - Vivado open_checkpoint + route_design + report_timing_summary
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}

    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}

    detour_analysis = analyze_net_detour(
        critical_paths_data=critical_paths_data,
        detour_threshold=detour_threshold,
        input_file=input_file,
    )
    if "error" in detour_analysis:
        return detour_analysis

    candidates = detour_analysis.get("candidates", [])
    selected_cells = []
    seen = set()
    for candidate in candidates:
        cell_name = candidate.get("cell")
        if not cell_name or cell_name in seen:
            continue
        seen.add(cell_name)
        selected_cells.append(cell_name)
        if len(selected_cells) >= max_candidates:
            break

    result: Dict[str, Any] = {
        "status": "success",
        "detour_threshold": detour_threshold,
        "candidates_found": len(candidates),
        "selected_cells": selected_cells,
    }

    if clock_period_ns is not None:
        before_timing = report_approx_timing(
            clock_period_ns=clock_period_ns,
            include_path=False,
        )
        if "error" not in before_timing:
            result["approx_timing_before"] = before_timing

    if not selected_cells:
        result["message"] = "No detour candidates exceeded the threshold"
        return result

    placement_result = optimize_cell_placement(
        cell_names=selected_cells,
        max_candidates=max_candidates,
    )
    result["placement_result"] = placement_result

    if "error" not in placement_result and clock_period_ns is not None:
        after_timing = report_approx_timing(
            clock_period_ns=clock_period_ns,
            include_path=False,
        )
        if "error" not in after_timing:
            result["approx_timing_after"] = after_timing

    moved_cells = 0
    if "error" not in placement_result:
        moved_cells = placement_result.get("cells_moved", 0)
    result["cells_moved"] = moved_cells
    result["message"] = (
        f"Detour repair processed {len(selected_cells)} selected cells and moved "
        f"{moved_cells}. Re-route in Vivado to measure exact timing."
    )
    return result


def analyze_clock_region_pblock_grid(
    critical_cell_names: Optional[list] = None,
) -> Dict[str, Any]:
    """Return current-device clock regions, capacities, and partition demand.

    All geometry comes from ``Device.getClockRegions()`` and all demand from
    the checkpoint loaded in this RapidWright session.  The result is read-only
    and contains no imported floorplan or benchmark identity.
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}

    try:
        device = _current_design.getDevice()
        normalized_device_name = str(device.getName()).lower()
        if normalized_device_name.startswith("xc7"):
            luts_per_slice, ffs_per_slice = 4, 8
        else:
            luts_per_slice, ffs_per_slice = 8, 16
        records = {}
        for row in device.getClockRegions():
            for clock_region in row:
                if clock_region is None:
                    continue
                match = re.search(r"X(\d+)Y(\d+)$", str(clock_region.getName()))
                if match is None:
                    continue
                x, y = int(match.group(1)), int(match.group(2))
                records[(x, y)] = {
                    "name": f"CLOCKREGION_X{x}Y{y}",
                    "x": x,
                    "y": y,
                    "resources": {
                        "slice_sites": 0,
                        "lut_capacity": 0,
                        "ff_capacity": 0,
                        "dsp_sites": 0,
                        "bram_sites": 0,
                        "uram_sites": 0,
                    },
                    "occupied_cell_count": 0,
                    "critical_cell_count": 0,
                }

        for tile in device.getAllTiles():
            clock_region = tile.getClockRegion()
            if clock_region is None:
                continue
            match = re.search(r"X(\d+)Y(\d+)$", str(clock_region.getName()))
            if match is None:
                continue
            record = records.get((int(match.group(1)), int(match.group(2))))
            if record is None:
                continue
            for site in tile.getSites() or []:
                site_type = str(site.getSiteTypeEnum()).upper()
                resources = record["resources"]
                if "SLICE" in site_type:
                    resources["slice_sites"] += 1
                    resources["lut_capacity"] += luts_per_slice
                    resources["ff_capacity"] += ffs_per_slice
                elif site_type.startswith("DSP"):
                    resources["dsp_sites"] += 1
                elif "RAMB" in site_type or "BRAM" in site_type:
                    resources["bram_sites"] += 1
                elif "URAM" in site_type:
                    resources["uram_sites"] += 1

        critical_names = {
            str(name)
            for name in (critical_cell_names or [])
            if isinstance(name, str) and name
        }
        total = {"lut": 0, "ff": 0, "dsp": 0, "bram": 0, "uram": 0}
        critical = {key: 0 for key in total}

        def resource_kind(cell_type: str) -> Optional[str]:
            value = cell_type.upper()
            if value.startswith("LUT"):
                return "lut"
            if value.startswith("FD") or value.startswith("LD"):
                return "ff"
            if value.startswith(("DSP48", "DSP58")):
                return "dsp"
            if value.startswith("RAMB"):
                return "bram"
            if value.startswith("URAM"):
                return "uram"
            return None

        matched_critical_names = []
        for cell in _current_design.getCells():
            name = str(cell.getName())
            kind = resource_kind(str(cell.getType()))
            if kind is not None:
                total[kind] += 1
                if name in critical_names:
                    critical[kind] += 1
            if not cell.isPlaced():
                continue
            site = cell.getSite()
            tile = site.getTile() if site is not None else None
            clock_region = tile.getClockRegion() if tile is not None else None
            if clock_region is None:
                continue
            match = re.search(r"X(\d+)Y(\d+)$", str(clock_region.getName()))
            if match is None:
                continue
            record = records.get((int(match.group(1)), int(match.group(2))))
            if record is None:
                continue
            record["occupied_cell_count"] += 1
            if name in critical_names:
                record["critical_cell_count"] += 1
                matched_critical_names.append(name)

        remainder = {
            key: max(0, total[key] - critical[key]) for key in total
        }
        return {
            "status": "success",
            "device": str(device.getName()),
            "clock_regions": [
                records[key]
                for key in sorted(records, key=lambda point: (point[1], point[0]))
            ],
            "critical_requirements": critical,
            "remainder_requirements": remainder,
            "critical_cell_count": len(set(matched_critical_names)),
            "matched_critical_cell_names": sorted(set(matched_critical_names)),
        }
    except Exception as e:
        logger.error(f"Error analyzing clock-region grid: {e}")
        return {"error": str(e)}


def convert_fabric_region_to_pblock_ranges(
    col_min: int,
    col_max: int,
    row_min: int,
    row_max: int,
    device_name: Optional[str] = None,
    use_clock_regions: bool = False
) -> Dict[str, Any]:
    """
    Convert fabric region (column/row coordinates) to Vivado pblock range strings.

    Generates a complete pblock string with all site types (SLICE, DSP, BRAM, URAM)
    in the format: "SLICE_X55Y0:SLICE_X109Y179 DSP48E2_X8Y0:DSP48E2_X13Y71 ..."

    Args:
        col_min, col_max: Column range (tile coordinates)
        row_min, row_max: Row range (tile coordinates)
        device_name: Device name (uses loaded design's device if omitted)
        use_clock_regions: If True, use CLOCKREGION ranges (simpler but coarser)

    Returns:
        Dictionary with pblock range strings suitable for Vivado create_pblock
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}

    try:
        from com.xilinx.rapidwright.device import Device, SiteTypeEnum

        # Get the device
        if device_name:
            device = Device.getDevice(device_name)
        elif _current_design:
            device = _current_design.getDevice()
        else:
            return {"error": "No device specified and no design loaded"}

        if use_clock_regions:
            # Use clock region ranges (simpler, coarser granularity)
            cr_x_min = col_min // 60
            cr_x_max = col_max // 60
            cr_y_min = row_min // 60
            cr_y_max = row_max // 60

            pblock_range = f"CLOCKREGION_X{cr_x_min}Y{cr_y_min}:CLOCKREGION_X{cr_x_max}Y{cr_y_max}"

            return {
                "status": "success",
                "pblock_ranges": pblock_range,
                "format": "CLOCKREGION"
            }

        # Use site ranges (finer granularity) - find all site types in region
        # Track min/max coordinates for each site type
        site_bounds = {
            "SLICE": {"min_x": float('inf'), "max_x": 0, "min_y": float('inf'), "max_y": 0, "count": 0},
            "DSP48E2": {"min_x": float('inf'), "max_x": 0, "min_y": float('inf'), "max_y": 0, "count": 0},
            "RAMB18": {"min_x": float('inf'), "max_x": 0, "min_y": float('inf'), "max_y": 0, "count": 0},
            "RAMB36": {"min_x": float('inf'), "max_x": 0, "min_y": float('inf'), "max_y": 0, "count": 0},
            "URAM288": {"min_x": float('inf'), "max_x": 0, "min_y": float('inf'), "max_y": 0, "count": 0},
        }

        # Iterate through all tiles in the region
        for tile in device.getAllTiles():
            col = tile.getColumn()
            row = tile.getRow()

            # Check if tile is within our region
            if not (col_min <= col <= col_max and row_min <= row <= row_max):
                continue

            # Check all sites in this tile
            sites = tile.getSites()
            if not sites:
                continue

            for site in sites:
                site_type = site.getSiteTypeEnum()
                site_type_name = str(site_type.name())
                site_name = str(site.getName())

                # Determine site type category based on name patterns
                site_category = None

                if site_type_name in ['SLICEL', 'SLICEM']:
                    site_category = "SLICE"
                elif 'DSP48E2' in site_type_name:
                    site_category = "DSP48E2"
                elif site_type_name in ['RAMB18E1', 'RAMB181', 'RAMB180', 'RAMB18_L', 'RAMB18_U', 'RAMBFIFO18']:
                    site_category = "RAMB18"
                elif site_type_name in ['RAMB36', 'RAMB36E1', 'RAMBFIFO36', 'RAMBFIFO36E1']:
                    site_category = "RAMB36"
                elif site_type_name == 'URAM288':
                    site_category = "URAM288"

                if site_category and site_category in site_bounds:
                    # Get instance X/Y coordinates from the site
                    try:
                        x = site.getInstanceX()
                        y = site.getInstanceY()

                        bounds = site_bounds[site_category]
                        bounds["min_x"] = min(bounds["min_x"], x)
                        bounds["max_x"] = max(bounds["max_x"], x)
                        bounds["min_y"] = min(bounds["min_y"], y)
                        bounds["max_y"] = max(bounds["max_y"], y)
                        bounds["count"] += 1
                    except:
                        pass

        # Build the pblock range string
        pblock_parts = []

        for site_type in ["SLICE", "DSP48E2", "RAMB18", "RAMB36", "URAM288"]:
            bounds = site_bounds[site_type]
            if bounds["count"] > 0 and bounds["min_x"] != float('inf'):
                min_x = int(bounds["min_x"])
                max_x = int(bounds["max_x"])
                min_y = int(bounds["min_y"])
                max_y = int(bounds["max_y"])

                range_str = f"{site_type}_X{min_x}Y{min_y}:{site_type}_X{max_x}Y{max_y}"
                pblock_parts.append(range_str)

        if not pblock_parts:
            return {"error": "No valid sites found in specified region"}

        pblock_ranges = " ".join(pblock_parts)

        return {
            "status": "success",
            "pblock_ranges": pblock_ranges,
            "format": "SITE",
            "site_counts": {
                "SLICE": site_bounds["SLICE"]["count"],
                "DSP48E2": site_bounds["DSP48E2"]["count"],
                "RAMB18": site_bounds["RAMB18"]["count"],
                "RAMB36": site_bounds["RAMB36"]["count"],
                "URAM288": site_bounds["URAM288"]["count"]
            }
        }

    except Exception as e:
        logger.error(f"Error converting fabric region to pblock: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e)}
