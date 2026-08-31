"""Bounded parsers and Tcl builders for read-only physical structure probes."""

from __future__ import annotations

import base64
from collections import defaultdict
import hashlib
import json
import re
from typing import Any


MAX_STRUCTURAL_CELLS = 512
MAX_STRUCTURAL_TIMEOUT_S = 600.0
MAX_STRUCTURE_MEMBERS = 512
MAX_STRUCTURE_SITES = 512
MAX_STRUCTURE_EDGES = 1024
MAX_FAIL_CLOSED_REASONS = 8


def validate_max_cells(max_cells: int) -> int:
    if isinstance(max_cells, bool) or not isinstance(max_cells, int):
        raise ValueError("max_cells must be an integer")
    if not 1 <= max_cells <= MAX_STRUCTURAL_CELLS:
        raise ValueError(f"max_cells must be in 1..{MAX_STRUCTURAL_CELLS}")
    return max_cells


def validate_timeout(timeout: float) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("timeout must be a number")
    timeout = float(timeout)
    if not 1.0 <= timeout <= MAX_STRUCTURAL_TIMEOUT_S:
        raise ValueError(
            f"timeout must be in 1..{int(MAX_STRUCTURAL_TIMEOUT_S)} seconds"
        )
    return timeout


def _decode(value: str) -> str:
    return base64.b64decode(value.encode("ascii"), validate=True).decode("utf-8")


def _parse_bool(value: str) -> bool | None:
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    return None


def _member_record(name, ref_name, loc, bel, loc_fixed, bel_fixed) -> dict[str, Any]:
    return {
        "name": name,
        "ref_name": ref_name,
        "loc": loc,
        "bel": bel,
        "loc_fixed": loc_fixed,
        "bel_fixed": bel_fixed,
    }


def structure_content_digest(
    structure_type: str,
    cells: list[dict[str, Any]],
    sites: list[str],
    edges: list[dict[str, Any]],
) -> str:
    member_keys = ("name", "ref_name", "loc", "bel", "loc_fixed", "bel_fixed")
    payload = {
        "structure_type": str(structure_type),
        "cells": sorted(
            ({key: cell.get(key) for key in member_keys} for cell in cells),
            key=lambda item: (str(item["name"]), str(item["ref_name"])),
        ),
        "sites": sorted(str(site) for site in sites),
        "edges": sorted(
            ({str(key): value for key, value in edge.items()} for edge in edges),
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _site_xy(site: str | None) -> tuple[int, int] | None:
    match = re.search(r"_X(-?\d+)Y(-?\d+)$", str(site or ""))
    return (int(match.group(1)), int(match.group(2))) if match else None


def _dedicated_geometry_ok(ordered: list[str], records: dict[str, dict]) -> bool:
    if len(ordered) < 2:
        return True
    coordinates = [_site_xy(records[name].get("loc")) for name in ordered]
    if any(value is None for value in coordinates):
        return False
    directions = []
    for first, second in zip(coordinates, coordinates[1:]):
        assert first is not None and second is not None
        if first[0] != second[0] or abs(second[1] - first[1]) != 1:
            return False
        directions.append(second[1] - first[1])
    return len(set(directions)) == 1


def _bounded_reasons(reasons: list[str]) -> list[str]:
    return list(dict.fromkeys(reasons))[:MAX_FAIL_CLOSED_REASONS]


def _components(names: set[str], edges: list[dict[str, str]]) -> list[list[str]]:
    adjacency: dict[str, set[str]] = {name: set() for name in names}
    for edge in edges:
        source = edge["source"]
        destination = edge["destination"]
        if source in adjacency and destination in adjacency:
            adjacency[source].add(destination)
            adjacency[destination].add(source)

    result = []
    remaining = set(names)
    while remaining:
        root = min(remaining)
        pending = [root]
        component = set()
        while pending:
            current = pending.pop()
            if current in component:
                continue
            component.add(current)
            pending.extend(adjacency[current] - component)
        remaining -= component
        result.append(sorted(component))
    return result


def _ordered_component(
    component: list[str], edges: list[dict[str, str]]
) -> tuple[list[str], bool, bool]:
    members = set(component)
    outgoing: dict[str, list[str]] = defaultdict(list)
    incoming: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        source = edge["source"]
        destination = edge["destination"]
        if source in members and destination in members:
            outgoing[source].append(destination)
            incoming[destination].append(source)

    branched = any(
        len(outgoing[name]) > 1 or len(incoming[name]) > 1 for name in component
    )
    roots = sorted(name for name in component if not incoming[name])
    if branched or len(roots) != 1:
        return component, branched, not roots

    ordered = []
    seen = set()
    current = roots[0]
    while current not in seen:
        ordered.append(current)
        seen.add(current)
        next_cells = outgoing[current]
        if not next_cells:
            break
        current = next_cells[0]
    cycle = len(seen) != len(component)
    return (ordered if not cycle else component), False, cycle


def build_carry_cloud_tcl(max_cells: int) -> str:
    max_cells = validate_max_cells(max_cells)
    return f"""
proc fpl26_b64 {{value}} {{
  return [binary encode base64 -maxlen 0 "$value"]
}}
proc fpl26_prop {{cell prop}} {{
  if {{[catch {{get_property -quiet $prop $cell}} value]}} {{return ""}}
  return $value
}}
array set fpl26_site_occupant_cache {{}}
proc fpl26_emit_carry_member {{owner member}} {{
  global fpl26_site_occupant_cache
  set member_name [get_property NAME $member]
  set member_ref [get_property REF_NAME $member]
  set member_loc [get_property -quiet LOC $member]
  set member_bel [get_property -quiet BEL $member]
  puts "FPL26_CARRY_MEMBER|[fpl26_b64 $owner]|[fpl26_b64 $member_name]|$member_ref|$member_loc|$member_bel|[fpl26_prop $member IS_LOC_FIXED]|[fpl26_prop $member IS_BEL_FIXED]"
  if {{$member_loc ne ""}} {{
    if {{![info exists fpl26_site_occupant_cache($member_loc)]}} {{
      set occupant_records {{}}
      foreach occupant [get_cells -quiet -of_objects [get_sites -quiet $member_loc]] {{
        lappend occupant_records [list [get_property NAME $occupant] [fpl26_prop $occupant IS_LOC_FIXED] [fpl26_prop $occupant IS_BEL_FIXED]]
      }}
      set fpl26_site_occupant_cache($member_loc) $occupant_records
    }}
    foreach occupant_record $fpl26_site_occupant_cache($member_loc) {{
      lassign $occupant_record occupant_name occupant_loc_fixed occupant_bel_fixed
      puts "FPL26_CARRY_SITE_OCC|[fpl26_b64 $owner]|[fpl26_b64 $member_loc]|[fpl26_b64 $occupant_name]|$occupant_loc_fixed|$occupant_bel_fixed"
    }}
  }}
}}
set fpl26_carries [lsort -dictionary [get_cells -quiet -hierarchical -filter {{IS_PRIMITIVE && (REF_NAME == CARRY4 || REF_NAME == CARRY8)}}]]
puts "FPL26_CARRY_COUNT|[llength $fpl26_carries]"
set fpl26_selected [lrange $fpl26_carries 0 {max_cells - 1}]
foreach fpl26_cell $fpl26_selected {{
  set fpl26_name [get_property NAME $fpl26_cell]
  set fpl26_ref [get_property REF_NAME $fpl26_cell]
  set fpl26_loc [get_property -quiet LOC $fpl26_cell]
  set fpl26_bel [get_property -quiet BEL $fpl26_cell]
  set fpl26_loc_fixed [fpl26_prop $fpl26_cell IS_LOC_FIXED]
  set fpl26_bel_fixed [fpl26_prop $fpl26_cell IS_BEL_FIXED]
  set fpl26_cloud_cells {{}}
  set fpl26_data_pins [get_pins -quiet -of_objects $fpl26_cell -filter {{DIRECTION == IN && (REF_PIN_NAME =~ S* || REF_PIN_NAME =~ DI*)}}]
  foreach fpl26_pin $fpl26_data_pins {{
    set fpl26_nets [get_nets -quiet -segments -of_objects $fpl26_pin]
    set fpl26_drivers [get_pins -quiet -leaf -of_objects $fpl26_nets -filter {{DIRECTION == OUT}}]
    foreach fpl26_driver $fpl26_drivers {{
      foreach fpl26_driver_cell [get_cells -quiet -of_objects $fpl26_driver] {{
        if {{$fpl26_driver_cell ne $fpl26_cell}} {{lappend fpl26_cloud_cells $fpl26_driver_cell}}
      }}
    }}
  }}
  set fpl26_luts 0
  set fpl26_ffs 0
  set fpl26_other 0
  foreach fpl26_driver_cell [lsort -unique $fpl26_cloud_cells] {{
    set fpl26_driver_ref [get_property REF_NAME $fpl26_driver_cell]
    if {{[string match "LUT*" $fpl26_driver_ref]}} {{
      incr fpl26_luts
    }} elseif {{[string match "FD*" $fpl26_driver_ref] || [string match "LD*" $fpl26_driver_ref]}} {{
      incr fpl26_ffs
    }} else {{
      incr fpl26_other
    }}
    fpl26_emit_carry_member $fpl26_name $fpl26_driver_cell
  }}
  fpl26_emit_carry_member $fpl26_name $fpl26_cell
  puts "FPL26_CARRY|[fpl26_b64 $fpl26_name]|$fpl26_ref|$fpl26_loc|$fpl26_bel|$fpl26_luts|$fpl26_ffs|$fpl26_other|$fpl26_loc_fixed|$fpl26_bel_fixed"
}}
foreach fpl26_cell $fpl26_selected {{
  set fpl26_source_name [get_property NAME $fpl26_cell]
  set fpl26_outputs [get_pins -quiet -of_objects $fpl26_cell -filter {{DIRECTION == OUT && REF_PIN_NAME =~ CO*}}]
  foreach fpl26_output $fpl26_outputs {{
    set fpl26_nets [get_nets -quiet -segments -of_objects $fpl26_output]
    set fpl26_source_pin [get_property REF_PIN_NAME $fpl26_output]
    set fpl26_sinks [get_pins -quiet -leaf -of_objects $fpl26_nets -filter {{DIRECTION == IN && REF_PIN_NAME =~ CI*}}]
    foreach fpl26_sink $fpl26_sinks {{
      foreach fpl26_destination [get_cells -quiet -of_objects $fpl26_sink] {{
        set fpl26_destination_ref [get_property REF_NAME $fpl26_destination]
        if {{$fpl26_destination_ref eq "CARRY4" || $fpl26_destination_ref eq "CARRY8"}} {{
          set fpl26_dest_pin [get_property REF_PIN_NAME $fpl26_sink]
          set fpl26_dedicated [expr {{[llength [get_pips -quiet -of_objects $fpl26_nets]] == 0}}]
          puts "FPL26_CARRY_EDGE|[fpl26_b64 $fpl26_source_name]|[fpl26_b64 [get_property NAME $fpl26_destination]]|$fpl26_source_pin|$fpl26_dest_pin|$fpl26_dedicated"
        }}
      }}
    }}
  }}
}}
""".strip()


def parse_carry_cloud_output(output: str, max_cells: int) -> dict[str, Any]:
    max_cells = validate_max_cells(max_cells)
    total = 0
    cells: dict[str, dict[str, Any]] = {}
    edges = []
    members: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    member_conflicts: set[str] = set()
    site_occupants: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    parse_errors = []
    count_seen = False
    for line in output.splitlines():
        fields = re.split(r"[\t|]", line.rstrip())
        try:
            if fields[0] == "FPL26_CARRY_COUNT" and len(fields) == 2:
                total = int(fields[1])
                count_seen = True
            elif fields[0] == "FPL26_CARRY" and len(fields) in {8, 10}:
                name = _decode(fields[1])
                cells[name] = {
                    "name": name,
                    "ref_name": fields[2],
                    "loc": fields[3] or None,
                    "bel": fields[4] or None,
                    "cloud": {
                        "lut_driver_count": int(fields[5]),
                        "ff_or_latch_driver_count": int(fields[6]),
                        "other_driver_count": int(fields[7]),
                    },
                    "loc_fixed": _parse_bool(fields[8]) if len(fields) == 10 else None,
                    "bel_fixed": _parse_bool(fields[9]) if len(fields) == 10 else None,
                }
            elif fields[0] == "FPL26_CARRY_MEMBER" and len(fields) == 8:
                owner = _decode(fields[1])
                member = _member_record(
                    _decode(fields[2]), fields[3], fields[4] or None,
                    fields[5] or None, _parse_bool(fields[6]), _parse_bool(fields[7]),
                )
                previous = members[owner].get(member["name"])
                if previous is not None and previous != member:
                    member_conflicts.add(owner)
                members[owner][member["name"]] = member
            elif fields[0] == "FPL26_CARRY_SITE_OCC" and len(fields) == 6:
                site_occupants[_decode(fields[1])][_decode(fields[2])].add(
                    _decode(fields[3])
                )
            elif fields[0] == "FPL26_CARRY_EDGE" and len(fields) in {3, 6}:
                edges.append({
                    "source": _decode(fields[1]),
                    "destination": _decode(fields[2]),
                    "source_pin": fields[3] if len(fields) == 6 else None,
                    "destination_pin": fields[4] if len(fields) == 6 else None,
                    "dedicated": _parse_bool(fields[5]) if len(fields) == 6 else None,
                })
        except (ValueError, UnicodeError) as exc:
            parse_errors.append(str(exc))

    if not count_seen:
        parse_errors.append("carry count marker missing")
    elif len(cells) != min(total, max_cells):
        parse_errors.append("carry records are incomplete")

    edge_by_pair = {}
    for edge in edges:
        if edge["source"] in cells and edge["destination"] in cells:
            edge_by_pair[(edge["source"], edge["destination"])] = edge
    all_known_edges = [edge_by_pair[key] for key in sorted(edge_by_pair)]
    edges_truncated = len(all_known_edges) > MAX_STRUCTURE_EDGES
    known_edges = all_known_edges[:MAX_STRUCTURE_EDGES]
    chains = []
    structures = []
    for component in _components(set(cells), known_edges):
        ordered, branched, cycle = _ordered_component(component, known_edges)
        chains.append({
            "length": len(component),
            "cells": ordered,
            "branched": branched,
            "cycle": cycle,
        })
        structure_members: dict[str, dict[str, Any]] = {}
        reasons = []
        for owner in component:
            structure_members.update(members.get(owner, {}))
            if owner not in structure_members:
                item = cells[owner]
                structure_members[owner] = _member_record(
                    owner, item["ref_name"], item["loc"], item["bel"],
                    item.get("loc_fixed"), item.get("bel_fixed"),
                )
            expected_cloud = sum(cells[owner]["cloud"].values())
            actual_cloud = len(set(members.get(owner, {})) - {owner})
            if actual_cloud != expected_cloud:
                reasons.append("cloud_members_incomplete")
            if owner in member_conflicts:
                reasons.append("mixed_member_placement")
        component_edges = [
            edge for edge in known_edges
            if edge["source"] in component and edge["destination"] in component
        ]
        if branched:
            reasons.append("branched_carry_topology")
        if cycle:
            reasons.append("cyclic_carry_topology")
        if any(edge.get("dedicated") is not True for edge in component_edges):
            reasons.append("broken_or_unknown_dedicated_topology")
        if not branched and not cycle and not _dedicated_geometry_ok(ordered, cells):
            reasons.append("split_or_broken_carry_placement")
        if parse_errors:
            reasons.append("analysis_parse_error")
        if total > max_cells:
            reasons.append("analysis_truncated")
        if edges_truncated:
            reasons.append("edge_limit_exceeded")
        full_member_count = len(structure_members)
        member_list = sorted(structure_members.values(), key=lambda item: item["name"])
        if len(member_list) > MAX_STRUCTURE_MEMBERS:
            reasons.append("member_limit_exceeded")
            member_list = member_list[:MAX_STRUCTURE_MEMBERS]
        if any(
            not item.get("loc") or not item.get("bel")
            or item.get("loc_fixed") is None or item.get("bel_fixed") is None
            for item in member_list
        ):
            reasons.append("unknown_placement_or_fixity")
        if any(item.get("loc_fixed") or item.get("bel_fixed") for item in member_list):
            reasons.append("locked_or_fixed_member")
        sites = sorted({str(item["loc"]) for item in member_list if item.get("loc")})
        occupancy = set()
        occupancy_known = True
        for owner in component:
            owner_occupancy = site_occupants.get(owner, {})
            for site in sites:
                if site not in owner_occupancy and any(
                    item.get("loc") == site for item in members.get(owner, {}).values()
                ):
                    occupancy_known = False
                occupancy.update(owner_occupancy.get(site, set()))
        if not occupancy_known or not site_occupants:
            reasons.append("site_occupancy_unknown")
        elif occupancy - set(structure_members):
            reasons.append("mixed_site_occupancy")
        if len(sites) > MAX_STRUCTURE_SITES:
            reasons.append("site_limit_exceeded")
            sites = sites[:MAX_STRUCTURE_SITES]
        digest = structure_content_digest("carry_cloud", member_list, sites, component_edges)
        structures.append({
            "structure_type": "carry_cloud",
            "structure_id": f"carry_cloud:{digest[:20]}",
            "content_digest": digest,
            "length": len(component),
            "carry_cells": ordered,
            "cells": member_list,
            "sites": sites,
            "edges": component_edges,
            "members_truncated": full_member_count > len(member_list),
            "relocation_eligible": not reasons,
            "fail_closed_reasons": _bounded_reasons(reasons),
        })
    chains.sort(key=lambda item: (-item["length"], item["cells"]))

    cell_list = []
    incoming = defaultdict(int)
    outgoing = defaultdict(int)
    for edge in known_edges:
        outgoing[edge["source"]] += 1
        incoming[edge["destination"]] += 1
    for name in sorted(cells):
        item = cells[name]
        item["carry_in_degree"] = incoming[name]
        item["carry_out_degree"] = outgoing[name]
        cell_list.append(item)

    return {
        "status": "partial" if parse_errors else "success",
        "read_only": True,
        "carry_cell_count": total,
        "analyzed_cell_count": len(cell_list),
        "truncated": total > max_cells,
        "cells": cell_list,
        "carry_edges": known_edges,
        "carry_edges_truncated": edges_truncated,
        "chains": chains,
        "structures": structures,
        "chain_count": sum(1 for chain in chains if chain["length"] > 1),
        "max_chain_length": max((chain["length"] for chain in chains), default=0),
        "cloud_summary": {
            "lut_driver_count": sum(item["cloud"]["lut_driver_count"] for item in cell_list),
            "ff_or_latch_driver_count": sum(
                item["cloud"]["ff_or_latch_driver_count"] for item in cell_list
            ),
            "other_driver_count": sum(item["cloud"]["other_driver_count"] for item in cell_list),
        },
        "parse_errors": parse_errors[:8],
    }


def build_hard_macro_cascade_tcl(max_cells: int) -> str:
    max_cells = validate_max_cells(max_cells)
    return f"""
proc fpl26_b64 {{value}} {{
  return [binary encode base64 -maxlen 0 "$value"]
}}
proc fpl26_prop {{cell prop}} {{
  if {{[catch {{get_property $prop $cell}} value]}} {{return ""}}
  return $value
}}
proc fpl26_is_hard_ref {{ref}} {{
  return [expr {{$ref eq "DSP48E1" || $ref eq "DSP48E2" || $ref eq "DSP58" || $ref eq "RAMB18E1" || $ref eq "RAMB18E2" || $ref eq "RAMB36E1" || $ref eq "RAMB36E2" || $ref eq "URAM288"}}]
}}
proc fpl26_emit_macro_occupants {{owner loc}} {{
  if {{$loc eq ""}} {{return}}
  foreach occupant [get_cells -quiet -of_objects [get_sites -quiet $loc]] {{
    puts "FPL26_MACRO_SITE_OCC|[fpl26_b64 $owner]|[fpl26_b64 $loc]|[fpl26_b64 [get_property NAME $occupant]]|[fpl26_prop $occupant IS_LOC_FIXED]|[fpl26_prop $occupant IS_BEL_FIXED]"
  }}
}}
set fpl26_macros [lsort -dictionary [get_cells -quiet -hierarchical -filter {{IS_PRIMITIVE && (REF_NAME == DSP48E1 || REF_NAME == DSP48E2 || REF_NAME == DSP58 || REF_NAME == RAMB18E1 || REF_NAME == RAMB18E2 || REF_NAME == RAMB36E1 || REF_NAME == RAMB36E2 || REF_NAME == URAM288)}}]]
puts "FPL26_MACRO_COUNT|[llength $fpl26_macros]"
set fpl26_selected [lrange $fpl26_macros 0 {max_cells - 1}]
foreach fpl26_cell $fpl26_selected {{
  set fpl26_name [get_property NAME $fpl26_cell]
  set fpl26_ref [get_property REF_NAME $fpl26_cell]
  set fpl26_loc [get_property -quiet LOC $fpl26_cell]
  set fpl26_bel [get_property -quiet BEL $fpl26_cell]
  set fpl26_loc_fixed [fpl26_prop $fpl26_cell IS_LOC_FIXED]
  set fpl26_bel_fixed [fpl26_prop $fpl26_cell IS_BEL_FIXED]
  set fpl26_props ""
  foreach fpl26_key {{AREG BREG MREG PREG ACASCREG BCASCREG CARRYINREG CARRYINSELREG DOA_REG DOB_REG CASCADE_ORDER_A CASCADE_ORDER_B IREG_PRE_A IREG_PRE_B OREG_A OREG_B}} {{
    set fpl26_value [fpl26_prop $fpl26_cell $fpl26_key]
    if {{$fpl26_value ne ""}} {{lappend fpl26_props "$fpl26_key=$fpl26_value"}}
  }}
  set fpl26_props_b64 [fpl26_b64 [join $fpl26_props ";"]]
  puts "FPL26_MACRO_PIPE|[fpl26_b64 $fpl26_name]|$fpl26_ref|$fpl26_loc|$fpl26_bel|$fpl26_props_b64|$fpl26_loc_fixed|$fpl26_bel_fixed"
  fpl26_emit_macro_occupants $fpl26_name $fpl26_loc
}}
foreach fpl26_cell $fpl26_selected {{
  set fpl26_source_name [get_property NAME $fpl26_cell]
  foreach fpl26_output [get_pins -quiet -of_objects $fpl26_cell -filter {{DIRECTION == OUT}}] {{
    set fpl26_out_ref [get_property REF_PIN_NAME $fpl26_output]
    if {{![regexp {{(^PCOUT|^ACOUT|^BCOUT|CARRYCASCOUT|MULTSIGNOUT|CASCADEOUT|CAS_OUT|CASDOUT)}} $fpl26_out_ref]}} {{continue}}
    set fpl26_nets [get_nets -quiet -segments -of_objects $fpl26_output]
    foreach fpl26_input [get_pins -quiet -leaf -of_objects $fpl26_nets -filter {{DIRECTION == IN}}] {{
      set fpl26_in_ref [get_property REF_PIN_NAME $fpl26_input]
      if {{![regexp {{(^PCIN|^ACIN|^BCIN|CARRYCASCIN|MULTSIGNIN|CASCADEIN|CAS_IN|CASDIN)}} $fpl26_in_ref]}} {{continue}}
      foreach fpl26_destination [get_cells -quiet -of_objects $fpl26_input] {{
        set fpl26_destination_ref [get_property REF_NAME $fpl26_destination]
        if {{[fpl26_is_hard_ref $fpl26_destination_ref]}} {{
          set fpl26_dedicated [expr {{[llength [get_pips -quiet -of_objects $fpl26_nets]] == 0}}]
          puts "FPL26_MACRO_EDGE|[fpl26_b64 $fpl26_source_name]|[fpl26_b64 [get_property NAME $fpl26_destination]]|$fpl26_out_ref|$fpl26_in_ref|$fpl26_dedicated"
        }}
      }}
    }}
  }}
}}
""".strip()


def _parse_properties(value: str) -> dict[str, str]:
    decoded = _decode(value)
    result = {}
    for item in decoded.split(";"):
        if "=" in item:
            key, prop_value = item.split("=", 1)
            result[key] = prop_value
    return result


def _macro_family(ref_name: str) -> str:
    if ref_name.startswith("DSP"):
        return "DSP"
    if ref_name.startswith("RAMB"):
        return "BRAM"
    if ref_name.startswith("URAM"):
        return "URAM"
    return "unknown"


def parse_hard_macro_cascade_output(output: str, max_cells: int) -> dict[str, Any]:
    max_cells = validate_max_cells(max_cells)
    total = 0
    macros: dict[str, dict[str, Any]] = {}
    edges = []
    site_occupants: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    parse_errors = []
    count_seen = False
    for line in output.splitlines():
        fields = re.split(r"[\t|]", line.rstrip())
        try:
            if fields[0] == "FPL26_MACRO_COUNT" and len(fields) == 2:
                total = int(fields[1])
                count_seen = True
            elif fields[0] in {"FPL26_MACRO", "FPL26_MACRO_PIPE"} and len(fields) in {6, 8}:
                name = _decode(fields[1])
                macros[name] = {
                    "name": name,
                    "ref_name": fields[2],
                    "family": _macro_family(fields[2]),
                    "loc": fields[3] or None,
                    "bel": fields[4] or None,
                    "register_properties": _parse_properties(fields[5]),
                    "loc_fixed": _parse_bool(fields[6]) if len(fields) == 8 else None,
                    "bel_fixed": _parse_bool(fields[7]) if len(fields) == 8 else None,
                }
            elif fields[0] == "FPL26_MACRO_SITE_OCC" and len(fields) == 6:
                site_occupants[_decode(fields[1])][_decode(fields[2])].add(
                    _decode(fields[3])
                )
            elif fields[0] == "FPL26_MACRO_EDGE" and len(fields) in {5, 6}:
                edges.append({
                    "source": _decode(fields[1]),
                    "destination": _decode(fields[2]),
                    "source_pin": fields[3],
                    "destination_pin": fields[4],
                    "dedicated": _parse_bool(fields[5]) if len(fields) == 6 else None,
                })
        except (ValueError, UnicodeError) as exc:
            parse_errors.append(str(exc))

    if not count_seen:
        parse_errors.append("hard macro count marker missing")
    elif len(macros) != min(total, max_cells):
        parse_errors.append("hard macro records are incomplete")

    edge_groups: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    for edge in edges:
        if edge["source"] in macros and edge["destination"] in macros:
            edge_groups[(edge["source"], edge["destination"])].add(
                (edge["source_pin"], edge["destination_pin"])
            )
    known_edges = []
    for (source, destination), pin_pairs in sorted(edge_groups.items()):
        ordered_pairs = sorted(pin_pairs)
        known_edges.append({
            "source": source,
            "destination": destination,
            "pin_pair_count": len(ordered_pairs),
            "pin_pairs": [
                {"source_pin": source_pin, "destination_pin": destination_pin}
                for source_pin, destination_pin in ordered_pairs[:8]
            ],
            "pin_pairs_truncated": len(ordered_pairs) > 8,
            "dedicated": all(
                edge.get("dedicated") is True
                for edge in edges
                if edge["source"] == source and edge["destination"] == destination
            ),
        })
    edges_truncated = len(known_edges) > MAX_STRUCTURE_EDGES
    known_edges = known_edges[:MAX_STRUCTURE_EDGES]
    incoming = defaultdict(int)
    outgoing = defaultdict(int)
    for edge in known_edges:
        outgoing[edge["source"]] += 1
        incoming[edge["destination"]] += 1

    macro_list = []
    for name in sorted(macros):
        item = macros[name]
        item["cascade_in_degree"] = incoming[name]
        item["cascade_out_degree"] = outgoing[name]
        macro_list.append(item)

    cascades = []
    structures = []
    for component in _components(set(macros), known_edges):
        if len(component) == 1 and not incoming[component[0]] and not outgoing[component[0]]:
            continue
        ordered, branched, cycle = _ordered_component(component, known_edges)
        cascade = {
            "length": len(component),
            "cells": ordered,
            "families": sorted({macros[name]["family"] for name in component}),
            "branched": branched,
            "cycle": cycle,
        }
        cascades.append(cascade)
        component_edges = [
            edge for edge in known_edges
            if edge["source"] in component and edge["destination"] in component
        ]
        reasons = []
        if branched:
            reasons.append("branched_cascade")
        if cycle:
            reasons.append("cyclic_cascade")
        if any(edge.get("dedicated") is not True for edge in component_edges):
            reasons.append("broken_or_unknown_dedicated_topology")
        if len({macros[name]["family"] for name in component}) != 1:
            reasons.append("mixed_macro_family")
        if not branched and not cycle and not _dedicated_geometry_ok(ordered, macros):
            reasons.append("split_or_broken_cascade_placement")
        if parse_errors:
            reasons.append("analysis_parse_error")
        if total > max_cells:
            reasons.append("analysis_truncated")
        if edges_truncated:
            reasons.append("edge_limit_exceeded")
        member_list = [
            _member_record(
                name, macros[name]["ref_name"], macros[name]["loc"],
                macros[name]["bel"], macros[name].get("loc_fixed"),
                macros[name].get("bel_fixed"),
            )
            for name in sorted(component)
        ]
        if any(
            not item.get("loc") or not item.get("bel")
            or item.get("loc_fixed") is None or item.get("bel_fixed") is None
            for item in member_list
        ):
            reasons.append("unknown_placement_or_fixity")
        if any(item.get("loc_fixed") or item.get("bel_fixed") for item in member_list):
            reasons.append("locked_or_fixed_member")
        sites = sorted({str(item["loc"]) for item in member_list if item.get("loc")})
        occupancy = set()
        occupancy_known = True
        for owner in component:
            for site in sites:
                if macros[owner].get("loc") == site and site not in site_occupants.get(owner, {}):
                    occupancy_known = False
                occupancy.update(site_occupants.get(owner, {}).get(site, set()))
        if not occupancy_known or not site_occupants:
            reasons.append("site_occupancy_unknown")
        elif occupancy - set(component):
            reasons.append("mixed_site_occupancy")
        digest = structure_content_digest(
            "hard_macro_cascade", member_list, sites, component_edges
        )
        structures.append({
            "structure_type": "hard_macro_cascade",
            "structure_id": f"hard_macro_cascade:{digest[:20]}",
            "content_digest": digest,
            "length": len(component),
            "macro_cells": ordered,
            "families": sorted({macros[name]["family"] for name in component}),
            "cells": member_list,
            "sites": sites,
            "edges": component_edges,
            "members_truncated": False,
            "relocation_eligible": not reasons,
            "fail_closed_reasons": _bounded_reasons(reasons),
        })
    cascades.sort(key=lambda item: (-item["length"], item["cells"]))
    family_counts = defaultdict(int)
    for item in macro_list:
        family_counts[item["family"]] += 1

    return {
        "status": "partial" if parse_errors else "success",
        "read_only": True,
        "hard_macro_count": total,
        "analyzed_macro_count": len(macro_list),
        "truncated": total > max_cells,
        "family_counts": dict(sorted(family_counts.items())),
        "macros": macro_list,
        "cascade_edges": known_edges,
        "cascade_edges_truncated": edges_truncated,
        "cascades": cascades,
        "structures": structures,
        "cascade_count": len(cascades),
        "max_cascade_length": max((item["length"] for item in cascades), default=0),
        "parse_errors": parse_errors[:8],
    }
