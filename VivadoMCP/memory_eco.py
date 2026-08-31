"""Fixed Vivado emitter for an exact, DCP-recovered BRAM repack."""

from __future__ import annotations

import base64
import re
from typing import Any


_PROPERTY_RE = re.compile(r"^[A-Z0-9_.-]{1,128}$")
_SITE_RE = re.compile(r"^RAMB(?:18|36)_X\d+Y\d+$")


def _name(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 2048
        or any(char in value for char in "{}\n\r")
        or any(ord(char) < 32 or ord(char) > 126 for char in value)
    ):
        raise ValueError(f"{field} contains unsupported characters: {value!r}")
    return value


def _braced(value: str) -> str:
    if not isinstance(value, str) or any(char in value for char in "{}\n\r"):
        raise ValueError("memory Tcl data contains unsupported characters")
    return "{" + value + "}"


def _property_value(value: str) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("memory property value is invalid")
    try:
        return _braced(value)
    except ValueError:
        encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
        return (
            "[encoding convertfrom utf-8 [binary decode base64 "
            + _braced(encoded)
            + "]]"
        )


def _validated_contract(
    facts: dict[str, Any], authorization: dict[str, Any]
) -> dict[str, Any]:
    try:
        from FDAgents.operator_proof import (
            memory_spec_from_extracted_facts,
            verify_rewrite_authorization,
        )
    except ImportError as exc:
        raise RuntimeError("FDAgents memory proof runtime is unavailable") from exc
    spec = memory_spec_from_extracted_facts(facts)
    if not isinstance(authorization, dict) or not verify_rewrite_authorization(
        authorization,
        expected_dcp_sha256=facts["design_sha256"],
        expected_region_sha256=facts["region_sha256"],
    ):
        raise ValueError("memory authorization failed proof replay")
    boundary = facts.get("boundary")
    properties = facts.get("properties")
    if (
        not isinstance(boundary, dict)
        or set(boundary) != {"cell", "physical", "pins"}
        or not isinstance(properties, dict)
    ):
        raise ValueError("memory facts boundary is incomplete")
    cell = _name(boundary.get("cell"), "memory cell")
    physical = boundary.get("physical")
    if not isinstance(physical, dict) or set(physical) != {
        "cell", "type", "site", "bel"
    }:
        raise ValueError("memory physical binding is incomplete")
    primitive = spec.primitive
    if physical.get("cell") != cell or physical.get("type") != primitive:
        raise ValueError("memory physical binding differs from semantic facts")
    site = physical.get("site")
    if not isinstance(site, str) or _SITE_RE.fullmatch(site) is None:
        raise ValueError("memory site is invalid")
    bel = _name(physical.get("bel"), "memory BEL")
    normalized_properties = []
    for key, value in sorted(properties.items()):
        if not isinstance(key, str) or _PROPERTY_RE.fullmatch(key) is None:
            raise ValueError(f"memory property name is invalid: {key!r}")
        if not isinstance(value, str):
            raise ValueError("memory property value must be a string")
        _property_value(value)
        normalized_properties.append((key, value))
    pins = []
    seen = set()
    raw_pins = boundary.get("pins")
    if not isinstance(raw_pins, list) or not raw_pins:
        raise ValueError("memory pin boundary is unavailable")
    for index, record in enumerate(raw_pins):
        if not isinstance(record, dict) or set(record) != {"pin", "direction", "net"}:
            raise ValueError(f"memory boundary pin {index} has an unsupported schema")
        pin = _name(record.get("pin"), f"memory boundary pin {index}")
        direction = record.get("direction")
        net = record.get("net")
        if direction not in {"input", "output"} or pin in seen:
            raise ValueError("memory boundary pin direction or identity is invalid")
        if net:
            net = _name(net, f"memory boundary net {index}")
        elif net != "":
            raise ValueError("memory boundary net must be a string")
        seen.add(pin)
        pins.append({
            "pin": pin,
            "full_pin": f"{cell}/{pin}",
            "direction": direction,
            "net": net,
        })
    return {
        "cell": cell,
        "primitive": primitive,
        "site": site,
        "bel": bel,
        "properties": normalized_properties,
        "pins": sorted(pins, key=lambda item: item["pin"]),
    }


def _common_procs(prefix: str) -> list[str]:
    return [
        f"proc {prefix}_one_cell {{name}} {{set value [get_cells -quiet [list $name]]; if {{[llength $value] != 1}} {{error \"memory cell missing: $name\"}}; return $value}}",
        f"proc {prefix}_one_pin {{name}} {{set value [get_pins -quiet [list $name]]; if {{[llength $value] != 1}} {{error \"memory pin missing: $name\"}}; return $value}}",
        f"proc {prefix}_pin_net {{pin expected}} {{set object [{prefix}_one_pin $pin]; set value [get_nets -quiet -of_objects $object]; if {{[llength $value] != 1}} {{error \"memory pin net missing: $pin\"}}; set names [get_property NAME [get_nets -quiet -segments -of_objects $object]]; if {{[lsearch -exact $names $expected] < 0}} {{error \"memory canonical net mismatch: $pin expected=$expected actual=$names\"}}; return $value}}",
        f"proc {prefix}_same_net {{pin net}} {{set actual [get_nets -quiet -segments -of_objects [{prefix}_one_pin $pin]]; set expected [get_nets -quiet -segments -of_objects $net]; foreach item $actual {{if {{[lsearch -exact $expected $item] >= 0}} {{return}}}}; error \"memory pin net mismatch: $pin\"}}",
        f"proc {prefix}_prop {{cell name expected}} {{set actual [string toupper [string trim [get_property $name [{prefix}_one_cell $cell]]]]; if {{$actual ne $expected}} {{error \"memory property mismatch: $name expected=$expected actual=$actual\"}}}}",
    ]


def build_memory_repack_eco_tcl(
    facts: dict[str, Any],
    authorization: dict[str, Any],
    *,
    output_dcp: str,
) -> str:
    contract = _validated_contract(facts, authorization)
    output = _braced(output_dcp)
    cell = contract["cell"]
    lines = _common_procs("fdagents_mem")
    lines.extend([
        "puts {FDAGENTS_MEMORY_ECO_STAGE_00_BEGIN}",
        f"set fdagents_mem_cell [fdagents_mem_one_cell {_braced(cell)}]",
        f"if {{[get_property REF_NAME $fdagents_mem_cell] ne {_braced(contract['primitive'])}}} {{error {{memory primitive mismatch}}}}",
        f"if {{[get_property LOC $fdagents_mem_cell] ne {_braced(contract['site'])}}} {{error {{memory site mismatch}}}}",
        f"if {{![string match {_braced('*' + contract['bel'])} [get_property BEL $fdagents_mem_cell]]}} {{error {{memory BEL mismatch}}}}",
    ])
    for key, value in contract["properties"]:
        lines.append(
            f"fdagents_mem_prop {_braced(cell)} {key} {_property_value(value)}"
        )
    for index, pin in enumerate(contract["pins"]):
        if pin["net"]:
            lines.append(
                f"set fdagents_mem_net_{index:04d} [fdagents_mem_pin_net {_braced(pin['full_pin'])} {_braced(pin['net'])}]"
            )
        else:
            lines.append(
                f"if {{[llength [get_nets -quiet -of_objects [fdagents_mem_one_pin {_braced(pin['full_pin'])}]]] != 0}} {{error {{memory disconnected pin unexpectedly connected}}}}"
            )
    lines.append("puts {FDAGENTS_MEMORY_ECO_STAGE_01_ASSERTED}")
    for index, pin in enumerate(contract["pins"]):
        if pin["net"]:
            lines.append(
                f"disconnect_net -net $fdagents_mem_net_{index:04d} -objects [fdagents_mem_one_pin {_braced(pin['full_pin'])}]"
            )
    lines.extend([
        "remove_cell $fdagents_mem_cell",
        f"if {{[llength [get_cells -quiet [list {_braced(cell)}]]] != 0}} {{error {{memory source removal failed}}}}",
        f"create_cell -reference {contract['primitive']} {_braced(cell)}",
    ])
    for key, value in contract["properties"]:
        lines.append(
            f"set_property {key} {_property_value(value)} [fdagents_mem_one_cell {_braced(cell)}]"
        )
    for index, pin in enumerate(contract["pins"]):
        if pin["net"]:
            lines.append(
                f"connect_net -hierarchical -net $fdagents_mem_net_{index:04d} -objects [fdagents_mem_one_pin {_braced(pin['full_pin'])}]"
            )
    lines.extend([
        f"place_cell [fdagents_mem_one_cell {_braced(cell)}] [get_sites {_braced(contract['site'])}]",
        f"if {{[get_property LOC [fdagents_mem_one_cell {_braced(cell)}]] ne {_braced(contract['site'])}}} {{error {{memory replacement placement failed}}}}",
        "puts {FDAGENTS_MEMORY_ECO_STAGE_02_REBUILT}",
        f"set fdagents_mem_touched [lsort -unique [get_nets -quiet -of_objects [get_pins -quiet -of_objects [fdagents_mem_one_cell {_braced(cell)}]]]]",
        "if {[llength $fdagents_mem_touched] == 0} {error {memory repack touched no nets}}",
        "route_design -nets $fdagents_mem_touched",
        "set fdagents_mem_unrouted [filter -quiet $fdagents_mem_touched {(ROUTE_STATUS == UNROUTED || ROUTE_STATUS == PARTIALLY_ROUTED) && TYPE != GND && TYPE != VCC}]",
        "if {[llength $fdagents_mem_unrouted] != 0} {error {memory repack left unrouted nets}}",
        "puts {FDAGENTS_MEMORY_ECO_STAGE_03_ROUTED}",
        f"write_checkpoint -force {output}",
        "puts {FDAGENTS_MEMORY_ECO_DONE}",
    ])
    return "\n".join(lines)


def build_memory_repack_postcheck_tcl(
    facts: dict[str, Any],
    consumer_facts: dict[str, Any],
    authorization: dict[str, Any],
) -> str:
    contract = _validated_contract(facts, authorization)
    try:
        from FDAgents.memory_action import validate_memory_consumer_facts
    except ImportError as exc:
        raise RuntimeError("FDAgents memory consumer runtime is unavailable") from exc
    validate_memory_consumer_facts(facts, consumer_facts)
    edges = consumer_facts.get("dependency_edges")
    if not isinstance(edges, list):
        raise ValueError("memory consumer dependency edges are unavailable")
    cell = contract["cell"]
    lines = _common_procs("fdagents_memc")
    lines.extend([
        f"if {{[get_property REF_NAME [fdagents_memc_one_cell {_braced(cell)}]] ne {_braced(contract['primitive'])}}} {{error {{postcheck memory primitive mismatch}}}}",
        f"if {{[get_property LOC [fdagents_memc_one_cell {_braced(cell)}]] ne {_braced(contract['site'])}}} {{error {{postcheck memory site mismatch}}}}",
        f"if {{![string match {_braced('*' + contract['bel'])} [get_property BEL [fdagents_memc_one_cell {_braced(cell)}]]]}} {{error {{postcheck memory BEL mismatch}}}}",
    ])
    for key, value in contract["properties"]:
        lines.append(
            f"fdagents_memc_prop {_braced(cell)} {key} {_property_value(value)}"
        )
    expected_connected = []
    for index, pin in enumerate(contract["pins"]):
        if pin["net"]:
            expected_connected.append(pin["full_pin"])
            lines.append(
                f"set fdagents_memc_net_{index:04d} [fdagents_memc_pin_net {_braced(pin['full_pin'])} {_braced(pin['net'])}]"
            )
        else:
            lines.append(
                f"if {{[llength [get_nets -quiet -of_objects [fdagents_memc_one_pin {_braced(pin['full_pin'])}]]] != 0}} {{error {{postcheck disconnected memory pin unexpectedly connected}}}}"
            )
    lines.extend([
        "set fdagents_memc_connected [list]",
        f"foreach fdagents_memc_pin [get_pins -quiet -of_objects [fdagents_memc_one_cell {_braced(cell)}]] {{if {{[llength [get_nets -quiet -of_objects $fdagents_memc_pin]] > 0}} {{lappend fdagents_memc_connected [get_property NAME $fdagents_memc_pin]}}}}",
        "set fdagents_memc_connected [lsort $fdagents_memc_connected]",
        "set fdagents_memc_expected [lsort [list "
        + " ".join(_braced(pin) for pin in expected_connected) + "]]",
        "if {$fdagents_memc_connected ne $fdagents_memc_expected} {error {postcheck memory connected-pin inventory mismatch}}",
        "puts {FDAGENTS_MEMORY_INVENTORY_OK}",
    ])
    for index, edge in enumerate(edges):
        if not isinstance(edge, dict) or set(edge) != {
            "producer_candidate_id", "source_pin", "net", "consumer_cell",
            "consumer_pin", "consumer_type",
        }:
            raise ValueError("memory consumer dependency edge schema is invalid")
        if edge.get("producer_candidate_id") != facts.get("candidate_id"):
            raise ValueError("memory consumer edge producer is stale")
        source_pin = f"{cell}/{_name(edge.get('source_pin'), 'consumer source pin')}"
        sink_cell = _name(edge.get("consumer_cell"), "consumer cell")
        sink_pin = _name(edge.get("consumer_pin"), "consumer pin")
        net = _name(edge.get("net"), "consumer net")
        full_sink = f"{sink_cell}/{sink_pin}" if sink_cell else sink_pin
        lines.extend([
            f"set fdagents_memc_edge_{index:04d} [fdagents_memc_pin_net {_braced(source_pin)} {_braced(net)}]",
            f"set fdagents_memc_sink_{index:04d} [fdagents_memc_pin_net {_braced(full_sink)} {_braced(net)}]",
        ])
    lines.extend([
        "puts {FDAGENTS_MEMORY_CONSUMERS_OK}",
        f"set fdagents_memc_touched [lsort -unique [get_nets -quiet -of_objects [get_pins -quiet -of_objects [fdagents_memc_one_cell {_braced(cell)}]]]]",
        "set fdagents_memc_unrouted [filter -quiet $fdagents_memc_touched {(ROUTE_STATUS == UNROUTED || ROUTE_STATUS == PARTIALLY_ROUTED) && TYPE != GND && TYPE != VCC}]",
        "if {[llength $fdagents_memc_unrouted] != 0} {error {postcheck memory nets are not fully routed}}",
        "puts {FDAGENTS_MEMORY_ROUTED_OK}",
        "puts {FDAGENTS_MEMORY_POSTCHECK_DONE}",
    ])
    return "\n".join(lines)
