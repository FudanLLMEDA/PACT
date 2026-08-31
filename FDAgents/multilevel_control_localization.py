"""Recover and clone complete same-cycle Boolean control generators."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any


class ControlLocalizationError(ValueError):
    """Raised when a control closure is incomplete or unsafe to clone."""


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")).hexdigest()


def _depth(output: str, by_output: dict[str, dict[str, Any]], cache: dict[str, int]) -> int:
    if output in cache:
        return cache[output]
    row = by_output.get(output)
    if row is None:
        return 0
    value = 1 + max((_depth(source, by_output, cache) for source in row["inputs"].values()), default=0)
    cache[output] = value
    return value


def recover_multilevel_control_closure(
    path: Path | str, *, source_dcp_sha256: str = ""
) -> dict[str, Any]:
    """Turn one live-root Tcl trace into an anonymous public contract."""

    with Path(path).open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    cells: list[dict[str, Any]] = []
    endpoints: list[dict[str, str]] = []
    summary: dict[str, str] = {}
    for row in rows:
        kind = row.get("kind", "")
        if kind == "cell":
            bindings: dict[str, str] = {}
            for binding in filter(None, row.get("input_bindings", "").split("|")):
                if "=" not in binding:
                    raise ControlLocalizationError("control LUT binding is malformed")
                pin, source = binding.split("=", 1)
                if not re.fullmatch(r"I[0-5]", pin) or not source:
                    raise ControlLocalizationError("control LUT binding is invalid")
                bindings[pin] = source
            item = {
                "cell_private": row.get("cell_private", ""),
                "ref": row.get("ref", ""),
                "init": row.get("init", ""),
                "output_source": row.get("output_source", ""),
                "inputs": bindings,
            }
            if (
                not item["cell_private"] or not re.fullmatch(r"LUT[1-6]", item["ref"])
                or not item["init"] or not item["output_source"] or not bindings
            ):
                raise ControlLocalizationError("control LUT record is incomplete")
            cells.append(item)
        elif kind == "endpoint":
            item = {
                "endpoint_private": row.get("endpoint_private", ""),
                "role": row.get("role", ""),
                "clock_region": row.get("clock_region", ""),
                "loc": row.get("loc", ""),
            }
            if not item["endpoint_private"] or not item["role"] or not item["clock_region"]:
                raise ControlLocalizationError("control endpoint record is incomplete")
            endpoints.append(item)
        elif kind == "summary":
            summary[row.get("key", "")] = row.get("value", "")
    if not cells or not endpoints:
        raise ControlLocalizationError("live multilevel control closure is empty")
    by_output = {row["output_source"]: row for row in cells}
    if len(by_output) != len(cells):
        raise ControlLocalizationError("control closure has multiple LUT drivers")
    root = summary.get("root_output_source", "")
    if root not in by_output:
        raise ControlLocalizationError("control closure root is absent")
    reachable: set[str] = set()
    active: set[str] = set()

    def visit(output: str) -> None:
        row = by_output.get(output)
        if row is None or output in reachable:
            return
        if output in active:
            raise ControlLocalizationError("cycle in control LUT closure")
        active.add(output)
        for source in row["inputs"].values():
            visit(source)
        active.remove(output)
        reachable.add(output)

    visit(root)
    if len(reachable) != len(cells):
        raise ControlLocalizationError("control closure contains unreachable LUTs")
    ordered = [row for row in cells if row["output_source"] in reachable]
    # Stable topological order: boundary-near cells before their consumers.
    ordered.sort(key=lambda row: (_depth(row["output_source"], by_output, {}), row["output_source"]))
    boundary = {
        source for row in ordered for source in row["inputs"].values()
        if source not in by_output
    }
    role_counts: dict[str, int] = {}
    region_counts: dict[str, int] = {}
    for endpoint in endpoints:
        role_counts[endpoint["role"]] = role_counts.get(endpoint["role"], 0) + 1
        region_counts[endpoint["clock_region"]] = region_counts.get(endpoint["clock_region"], 0) + 1
    if source_dcp_sha256 and re.fullmatch(r"[0-9a-f]{64}", source_dcp_sha256) is None:
        raise ControlLocalizationError("control localization source identity is invalid")
    private = {
        "source_dcp_sha256": source_dcp_sha256 or None,
        "candidate_ref": "multilevel_control_1",
        "root_output_source_private": root,
        "cells_private": ordered,
        "endpoints_private": endpoints,
    }
    public = {
        "kind": "multilevel_registered_control_distribution_contract",
        "schema_version": 1,
        "status": "proved_read_only",
        "authority": "multilevel_control_localization_proof_input",
        "mutation_eligible": True,
        "operator_class": "MULTILEVEL_REGISTERED_CONTROL_DISTRIBUTION",
        "source_dcp_sha256": source_dcp_sha256 or None,
        "candidate_ref": "multilevel_control_1",
        "logic_depth": _depth(root, by_output, {}),
        "closure_cell_count": len(ordered),
        "boundary_source_count": len(boundary),
        "sink_count": len(endpoints),
        "clock_region_count": len(region_counts),
        "sink_role_counts": role_counts,
        "covered_path_count": int(summary.get("covered_path_count", "0") or 0),
        "earliest_path_rank": int(summary.get("earliest_path_rank", "0") or 0),
        "required_before_mutation": [
            "clone every LUT and side input in the complete registered-boundary Boolean DAG",
            "preserve the original generator and every non-selected consumer",
            "move only same-role registered control sinks without changing their clock or cycle",
            "route and reopen under the unchanged contest clock",
        ],
        "exact_names_public": False,
    }
    public["private_manifest_sha256"] = _digest(private)
    unsigned = json.loads(json.dumps(public, sort_keys=True))
    public["contract_digest"] = _digest(unsigned)
    return {"public_contract": public, "private_manifest": private}


def _tcl_quote(value: str) -> str:
    if any(character in value for character in "\x00\r\n"):
        raise ControlLocalizationError("unsafe Tcl value")
    return "{" + value.replace("}", "\\}") + "}"


def build_multilevel_control_localization_tcl(
    recovered: dict[str, Any], input_dcp: str, output_dcp: str,
    result_tsv: str, *, max_sinks_per_group: int = 64,
    pre_repair_sites: list[str] | None = None,
    repair_sites_path: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Clone a complete Boolean control DAG for physical sink groups."""

    if not 1 <= max_sinks_per_group <= 512:
        raise ControlLocalizationError("max_sinks_per_group must be in 1..512")
    pre_repair_sites = sorted(set(pre_repair_sites or []))
    if any(not re.fullmatch(r"SLICE_X\d+Y\d+", site) for site in pre_repair_sites):
        raise ControlLocalizationError("pre-repair site manifest is invalid")
    if repair_sites_path is not None and any(
        character in str(repair_sites_path) for character in "\x00\r\n"
    ):
        raise ControlLocalizationError("unsafe repair-site cache path")
    private = recovered.get("private_manifest") or {}
    public = recovered.get("public_contract") or {}
    cells = list(private.get("cells_private") or [])
    endpoints = list(private.get("endpoints_private") or [])
    root = str(private.get("root_output_source_private") or "")
    if not cells or not endpoints or not root:
        raise ControlLocalizationError("control localization manifest is incomplete")
    source_sha256 = str(public.get("source_dcp_sha256") or "")
    if source_sha256 and re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None:
        raise ControlLocalizationError("control localization source identity is invalid")
    by_output = {row["output_source"]: row for row in cells}
    if root not in by_output:
        raise ControlLocalizationError("control localization root is absent")
    groups: list[tuple[str, list[dict[str, str]]]] = []
    by_region: dict[str, list[dict[str, str]]] = {}
    for endpoint in endpoints:
        by_region.setdefault(endpoint["clock_region"], []).append(endpoint)
    for region in sorted(by_region):
        ordered_endpoints = sorted(by_region[region], key=lambda row: (row.get("loc", ""), row["endpoint_private"]))
        for start in range(0, len(ordered_endpoints), max_sinks_per_group):
            groups.append((region, ordered_endpoints[start:start + max_sinks_per_group]))
    prefix = "fdagents_multictrl_" + _digest({
        "root": root, "cells": cells, "endpoints": endpoints,
        "max_sinks_per_group": max_sinks_per_group,
    })[:12]
    lines = [
        f"set expected_source_sha {_tcl_quote(source_sha256)}",
        f"if {{$expected_source_sha ne {{}}}} {{set actual_source_sha [lindex [split [exec sha256sum -- {_tcl_quote(input_dcp)}]] 0]; if {{$actual_source_sha ne $expected_source_sha}} {{error {{control localization source SHA mismatch}}}}}}",
        f"open_checkpoint {_tcl_quote(input_dcp)}",
        "proc safe_prop {object name} {if {$object eq {}} {return {}}; if {[catch {get_property -quiet $name $object} value]} {return {}}; return $value}",
        "proc timing_snapshot {} {set clock [lindex [get_clocks -quiet clk_fpl26contest] 0]; if {$clock eq {}} {error {clk_fpl26contest missing}}; set setup [lindex [get_timing_paths -quiet -setup -max_paths 1 -nworst 1 -from $clock -to $clock] 0]; if {$setup eq {}} {error {clk_fpl26contest has no setup path}}; set hold [lindex [get_timing_paths -quiet -hold -max_paths 1 -nworst 1 -from $clock -to $clock] 0]; return [dict create period [safe_prop $clock PERIOD] waveform [safe_prop $clock WAVEFORM] source [safe_prop $clock SOURCE_PINS] wns [safe_prop $setup SLACK] whs [expr {$hold eq {} ? {} : [safe_prop $hold SLACK]}]]}",
        "proc control_cell_names {cells} {set names {}; foreach cell $cells {lappend names [get_property NAME $cell]}; return [lsort -unique $names]}",
        "proc control_fixed_cell_names {property} {set names {}; foreach cell [get_cells -quiet -hierarchical -filter \"$property == 1\"] {lappend names [get_property NAME $cell]}; return [lsort -unique $names]}",
        "proc control_originally_movable_cells {cells loc_fixed_names bel_fixed_names} {set movable {}; foreach cell $cells {set name [get_property NAME $cell]; if {[lsearch -exact $loc_fixed_names $name] < 0 && [lsearch -exact $bel_fixed_names $name] < 0} {lappend movable $cell}}; return $movable}",
        "set input_loc_fixed_names [control_fixed_cell_names IS_LOC_FIXED]",
        "set input_bel_fixed_names [control_fixed_cell_names IS_BEL_FIXED]",
        "set input_timing [timing_snapshot]",
        "set input_lut_count [llength [get_cells -quiet -hierarchical -filter {REF_NAME =~ LUT*}]]",
        "set input_ff_count [llength [get_cells -quiet -hierarchical -filter {REF_NAME =~ FD*}]]",
        "proc direct_source_net {key} {",
        "  if {[string match {PIN:*} $key]} {set object [get_pins -quiet [string range $key 4 end]]} elseif {[string match {PORT:*} $key]} {set object [get_ports -quiet [string range $key 5 end]]} elseif {[string match {NET:*} $key]} {set nets [get_nets -quiet [string range $key 4 end]]; if {[llength $nets] != 1} {error {control boundary net is absent or ambiguous}}; return [lindex $nets 0]} else {error {invalid control boundary source key}}",
        "  if {[llength $object] != 1} {error {control boundary object is absent or ambiguous}}",
        "  set nets [get_nets -quiet -of_objects $object]",
        "  if {[llength $nets] != 1} {error {control boundary source net is absent or ambiguous}}",
        "  return [lindex $nets 0]",
        "}",
        "proc region_slice_range {region_name} {",
        "  set region [get_clock_regions -quiet [list $region_name]]",
        "  set sites [get_sites -quiet -of_objects $region -filter {SITE_TYPE =~ SLICE*}]",
        "  set xmin 1000000; set ymin 1000000; set xmax -1; set ymax -1",
        "  foreach site $sites {if {[regexp {SLICE_X([0-9]+)Y([0-9]+)} [get_property NAME $site] -> x y]} {if {$x < $xmin} {set xmin $x}; if {$x > $xmax} {set xmax $x}; if {$y < $ymin} {set ymin $y}; if {$y > $ymax} {set ymax $y}}}",
        "  if {$xmax < 0} {error {control sink region has no SLICE range}}",
        "  return \"SLICE_X${xmin}Y${ymin}:SLICE_X${xmax}Y${ymax}\"",
        "}",
        "set preserved_leaf [get_cells -hierarchical -filter {PRIMITIVE_LEVEL == LEAF && LOC != \"\"}]",
        "set temporary_loc_fixed_cells {}",
        "set temporary_bel_fixed_cells {}",
        "foreach cell $preserved_leaf {set name [get_property NAME $cell]; if {[lsearch -exact $input_loc_fixed_names $name] < 0} {lappend temporary_loc_fixed_cells $cell}; if {[lsearch -exact $input_bel_fixed_names $name] < 0} {lappend temporary_bel_fixed_cells $cell}}",
        "set temporary_loc_fixed_names [control_cell_names $temporary_loc_fixed_cells]",
        "set temporary_bel_fixed_names [control_cell_names $temporary_bel_fixed_cells]",
        "set_property IS_LOC_FIXED true $preserved_leaf",
        "set_property IS_BEL_FIXED true $preserved_leaf",
        "set all_new_cells {}",
    ]
    group_topologies: list[dict[str, Any]] = []
    for group_index, (region, group_endpoints) in enumerate(groups):
        output_vars: dict[str, str] = {}
        group_cells: list[str] = []
        for cell_index, row in enumerate(cells):
            name = f"{prefix}_g{group_index}_c{cell_index}"
            net = f"{name}_n"
            group_cells.append(name)
            lines.extend([
                f"create_cell -reference {row['ref']} {name}",
                f"set_property INIT {_tcl_quote(row['init'])} [get_cells {name}]",
                f"create_net {net}",
            ])
            for pin, source in sorted(row["inputs"].items()):
                source_expr = output_vars.get(source)
                if source_expr is None:
                    source_expr = f"[direct_source_net {_tcl_quote(source)}]"
                lines.append(f"connect_net -hierarchical -net {source_expr} -objects [get_pins {name}/{pin}]")
            lines.append(f"connect_net -hierarchical -net [get_nets {net}] -objects [get_pins -of_objects [get_cells {name}] -filter {{DIRECTION == OUT}}]")
            output_vars[row["output_source"]] = f"[get_nets {net}]"
        root_net = output_vars[root]
        for endpoint in group_endpoints:
            ep = _tcl_quote(endpoint["endpoint_private"])
            lines.extend([
                f"set endpoint [get_pins -quiet {ep}]",
                "if {[llength $endpoint] != 1} {error {control endpoint is absent}}",
                "set old_net [get_nets -quiet -of_objects $endpoint]",
                "if {[llength $old_net] != 1} {error {control endpoint driver is absent or ambiguous}}",
                "disconnect_net -net [lindex $old_net 0] -objects $endpoint",
                f"connect_net -hierarchical -net {root_net} -objects $endpoint",
            ])
        pblock = f"fdagentsMultiCtrl{group_index}"
        lines.extend([
            f"set pb [create_pblock {pblock}]",
            f"resize_pblock $pb -add [list [region_slice_range {_tcl_quote(region)}]]",
            f"add_cells_to_pblock $pb [get_cells {_tcl_quote(' '.join(group_cells))}]",
            f"foreach cell [get_cells {_tcl_quote(' '.join(group_cells))}] {{lappend all_new_cells $cell}}",
        ])
        group_topologies.append({"clock_region": region, "sink_count": len(group_endpoints)})
    lines.extend([
        "set_property DONT_TOUCH true $all_new_cells",
        "set repaired_site_count 0",
    ])
    if pre_repair_sites:
        lines.extend([
            f"set pre_repair_sites [get_sites -quiet {_tcl_quote(' '.join(pre_repair_sites))}]",
            f"if {{[llength $pre_repair_sites] != {len(pre_repair_sites)}}} {{error {{cached pin-map repair site is absent}}}}",
            "set pre_repair_cells [control_originally_movable_cells [get_cells -quiet -of_objects $pre_repair_sites] $input_loc_fixed_names $input_bel_fixed_names]",
            "if {[llength $pre_repair_cells] == 0} {error {cached pin-map repair sites contain no cells}}",
            "route_design -unroute",
            "catch {set_property IS_LOC_FIXED false $pre_repair_cells}",
            "catch {set_property IS_BEL_FIXED false $pre_repair_cells}",
            "unplace_cell $pre_repair_cells",
            "set repaired_site_count [llength $pre_repair_sites]",
        ])
    lines.extend([
        "place_design -directive Quick",
        "set first_route_failed [catch {route_design -directive NoTimingRelaxation} first_route_message]",
        "set first_bad_nets [get_nets -quiet -hierarchical -filter {(ROUTE_STATUS == UNROUTED || ROUTE_STATUS == PARTIALLY_ROUTED) && TYPE != GND && TYPE != VCC}]",
        "if {$first_route_failed || [llength $first_bad_nets]} {",
        "  set repair_site_names [dict create]",
        "  foreach net $first_bad_nets {",
        "    foreach cell [get_cells -quiet -of_objects [get_pins -quiet -leaf -of_objects [get_nets -quiet -segments $net]]] {",
        "      foreach site [get_sites -quiet -of_objects $cell] {",
        "        if {[string match {SLICE*} [get_property SITE_TYPE $site]]} {dict set repair_site_names [get_property NAME $site] 1}",
        "      }",
        "    }",
        "  }",
        "  if {[dict size $repair_site_names] == 0} {error \"control localization first route failed without repairable SLICE sites: $first_route_message\"}",
        "  set repair_sites [get_sites -quiet [dict keys $repair_site_names]]",
        "  set repair_cells [control_originally_movable_cells [get_cells -quiet -of_objects $repair_sites] $input_loc_fixed_names $input_bel_fixed_names]",
        "  if {[llength $repair_cells] == 0} {error {control localization repair sites contain no cells}}",
        "  incr repaired_site_count [llength $repair_sites]",
        *(
            [
                f"  set repair_out [open {_tcl_quote(repair_sites_path)} w]",
                "  foreach site_name [lsort [dict keys $repair_site_names]] {puts $repair_out $site_name}",
                "  close $repair_out",
            ]
            if repair_sites_path is not None else []
        ),
        "  route_design -unroute",
        "  catch {set_property IS_LOC_FIXED false $repair_cells}",
        "  catch {set_property IS_BEL_FIXED false $repair_cells}",
        "  unplace_cell $repair_cells",
        "  place_design -directive Quick",
        "  route_design -directive NoTimingRelaxation",
        "}",
        "if {[llength $temporary_loc_fixed_cells]} {set_property IS_LOC_FIXED false $temporary_loc_fixed_cells}",
        "if {[llength $temporary_bel_fixed_cells]} {set_property IS_BEL_FIXED false $temporary_bel_fixed_cells}",
        "if {[llength $input_loc_fixed_names]} {set_property IS_LOC_FIXED true [get_cells -quiet $input_loc_fixed_names]}",
        "if {[llength $input_bel_fixed_names]} {set_property IS_BEL_FIXED true [get_cells -quiet $input_bel_fixed_names]}",
        "set prewrite_loc_fixed_names [control_fixed_cell_names IS_LOC_FIXED]",
        "set prewrite_bel_fixed_names [control_fixed_cell_names IS_BEL_FIXED]",
        "if {$prewrite_loc_fixed_names ne $input_loc_fixed_names || $prewrite_bel_fixed_names ne $input_bel_fixed_names} {error {control localization temporary placement constraints were not restored before checkpoint write}}",
        f"write_checkpoint -force {_tcl_quote(output_dcp)}",
        "close_design",
        f"open_checkpoint {_tcl_quote(output_dcp)}",
        "set output_loc_fixed_names [control_fixed_cell_names IS_LOC_FIXED]",
        "set output_bel_fixed_names [control_fixed_cell_names IS_BEL_FIXED]",
        "if {$output_loc_fixed_names ne $input_loc_fixed_names || $output_bel_fixed_names ne $input_bel_fixed_names} {error {control localization checkpoint changed placement constraint identity}}",
        "set output_timing [timing_snapshot]",
        "if {[dict get $input_timing period] ne [dict get $output_timing period] || [dict get $input_timing waveform] ne [dict get $output_timing waveform] || [dict get $input_timing source] ne [dict get $output_timing source]} {error {control localization changed contest clock identity}}",
        "set route_text [report_route_status -return_string]",
        "set route_errors 0",
        "if {[regexp -nocase {# of nets with routing errors[^0-9]*([0-9]+)} $route_text -> value]} {set route_errors $value}",
        "set pulse_violations [regexp -all -nocase {VIOLATED} [report_pulse_width -return_string]]",
        "set output_lut_count [llength [get_cells -quiet -hierarchical -filter {REF_NAME =~ LUT*}]]",
        "set output_ff_count [llength [get_cells -quiet -hierarchical -filter {REF_NAME =~ FD*}]]",
        f"set out [open {_tcl_quote(result_tsv)} w]",
        "puts $out {metric\tvalue}",
        "puts $out {backend\tmultilevel_control_distribution_localization_v1}",
        "puts $out {contract\tMULTILEVEL_CONTROL_DAG_CLONE_V1}",
        "puts $out {family_ref\tmultilevel_control_1}",
        "puts $out \"source_dcp_sha256\t$expected_source_sha\"",
        f"puts $out {{closure_cell_count\t{len(cells)}}}",
        f"puts $out {{localized_sink_count\t{len(endpoints)}}}",
        f"puts $out {{clone_group_count\t{len(groups)}}}",
        f"puts $out {{new_lut_count\t{len(cells) * len(groups)}}}",
        "puts $out \"repaired_site_count\t$repaired_site_count\"",
        "puts $out \"input_loc_fixed_count\t[llength $input_loc_fixed_names]\"",
        "puts $out \"input_bel_fixed_count\t[llength $input_bel_fixed_names]\"",
        "puts $out \"output_loc_fixed_count\t[llength $output_loc_fixed_names]\"",
        "puts $out \"output_bel_fixed_count\t[llength $output_bel_fixed_names]\"",
        "puts $out \"temporary_loc_fixed_count\t[llength $temporary_loc_fixed_names]\"",
        "puts $out \"temporary_bel_fixed_count\t[llength $temporary_bel_fixed_names]\"",
        "puts $out {placement_fix_identity_preserved\t1}",
        "puts $out \"input_period_ns\t[dict get $input_timing period]\"",
        "puts $out \"input_waveform\t[dict get $input_timing waveform]\"",
        "puts $out \"input_clock_source\t[dict get $input_timing source]\"",
        "puts $out \"input_wns_ns\t[dict get $input_timing wns]\"",
        "puts $out \"output_period_ns\t[dict get $output_timing period]\"",
        "puts $out \"output_waveform\t[dict get $output_timing waveform]\"",
        "puts $out \"output_clock_source\t[dict get $output_timing source]\"",
        "puts $out \"output_wns_ns\t[dict get $output_timing wns]\"",
        "puts $out \"output_whs_ns\t[dict get $output_timing whs]\"",
        "puts $out \"input_lut_count\t$input_lut_count\"",
        "puts $out \"output_lut_count\t$output_lut_count\"",
        "puts $out \"input_ff_count\t$input_ff_count\"",
        "puts $out \"output_ff_count\t$output_ff_count\"",
        "puts $out \"route_errors\t$route_errors\"",
        "puts $out \"pulse_violations\t$pulse_violations\"",
        "puts $out {candidate_checkpoint_reopened\t1}",
        "close $out",
        "if {![info exists ::fdagents_embedded_mutation] || !$::fdagents_embedded_mutation} {exit 0}",
        "",
    ])
    topology = {
        "schema_version": 1,
        "backend": "multilevel_control_distribution_localization_v1",
        "closure_cell_count": len(cells),
        "localized_sink_count": len(endpoints),
        "clone_group_count": len(groups),
        "new_lut_count": len(cells) * len(groups),
        "groups": group_topologies,
        "pre_repair_site_count": len(pre_repair_sites),
        "repair_site_cache_enabled": repair_sites_path is not None,
    }
    topology["manifest_sha256"] = _digest(topology)
    return "\n".join(lines), topology


__all__ = [
    "ControlLocalizationError", "recover_multilevel_control_closure",
    "build_multilevel_control_localization_tcl",
]
