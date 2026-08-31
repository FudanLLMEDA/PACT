"""Exact recovery and routed emission for registered GF(2) parity closures."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ParityRecoveryError(ValueError):
    """Raised when an extracted parity graph is malformed."""


ACTION_CLASS = "registered_gf2_parity_reassociation_v1"
PROOF_TEMPLATE = "REGISTERED_GF2_PARITY_REASSOCIATION_V1"
SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ParityExpression:
    constant: int
    leaves: frozenset[str]
    cells: frozenset[str]
    raw_leaf_occurrences: int
    depth: int

    def xor(self, other: "ParityExpression") -> "ParityExpression":
        return ParityExpression(
            self.constant ^ other.constant,
            self.leaves.symmetric_difference(other.leaves),
            self.cells | other.cells,
            self.raw_leaf_occurrences + other.raw_leaf_occurrences,
            max(self.depth, other.depth),
        )


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")).hexdigest()


def recover_registered_parity_closures(
    path: Path | str, *, source_dcp_sha256: str = ""
) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    cells: dict[str, dict[str, Any]] = {}
    by_output: dict[str, str] = {}
    endpoints: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("kind") == "cell":
            output = row.get("output_source", "")
            name = row.get("cell_private", "")
            inputs = [item for item in row.get("input_sources", "").split("|") if item]
            if not output or not name or not inputs:
                raise ParityRecoveryError("parity cell record is incomplete")
            if output in by_output:
                raise ParityRecoveryError("parity graph output has multiple drivers")
            cells[name] = {
                "output": output,
                "inputs": inputs,
                "constant": int(row.get("polarity", "-1")),
                "ref": row.get("ref", ""),
                "init": row.get("init", ""),
            }
            if cells[name]["constant"] not in {0, 1}:
                raise ParityRecoveryError("invalid parity polarity")
            by_output[output] = name
        elif row.get("kind") == "endpoint":
            output = row.get("output_source", "")
            endpoint = row.get("endpoint_private", "")
            if output and endpoint:
                rank_text = row.get("live_rank", "")
                slack_text = row.get("live_slack_ns", "")
                endpoints.setdefault(output, []).append({
                    "pin": endpoint,
                    "live_rank": int(rank_text) if rank_text else None,
                    "live_slack_ns": float(slack_text) if slack_text else None,
                })
    cache: dict[str, ParityExpression] = {}
    active: set[str] = set()

    def expression(source: str) -> ParityExpression:
        if source in cache:
            return cache[source]
        cell_name = by_output.get(source)
        if cell_name is None:
            value = ParityExpression(0, frozenset({source}), frozenset(), 1, 0)
            cache[source] = value
            return value
        if source in active:
            raise ParityRecoveryError("cycle in registered parity closure")
        active.add(source)
        cell = cells[cell_name]
        value = ParityExpression(
            cell["constant"], frozenset(), frozenset({cell_name}), 0, 0
        )
        for item in cell["inputs"]:
            value = value.xor(expression(item))
        value = ParityExpression(
            value.constant, value.leaves, value.cells,
            value.raw_leaf_occurrences, value.depth + 1,
        )
        active.remove(source)
        cache[source] = value
        return value

    private_candidates = []
    public_candidates = []
    ranked = []
    for output, endpoint_rows in endpoints.items():
        value = expression(output)
        canceled = value.raw_leaf_occurrences - len(value.leaves)
        root_cell = by_output.get(output)
        if root_cell is None:
            continue
        live_ranks = [row["live_rank"] for row in endpoint_rows if row["live_rank"] is not None]
        # A complete parity closure can occur anywhere in a large netlist.  It
        # becomes a decision candidate only when one of its registered sinks is
        # present in the current live timing sample; this is relevance, not a
        # QoR/slack threshold.
        if not live_ranks:
            continue
        ranked.append((
            min(live_ranks, default=10**9), -value.depth, -len(value.cells),
            -canceled, output, value,
            sorted({row["pin"] for row in endpoint_rows}), root_cell,
            len(live_ranks),
        ))
    ranked.sort()
    for ordinal, (earliest_live_rank, _neg_depth, _neg_cells, _neg_cancel, output, value, endpoint_rows, root, live_count) in enumerate(ranked, 1):
        ref = f"registered_parity_{ordinal}"
        public_candidates.append({
            "candidate_ref": ref,
            "logic_depth": value.depth,
            "closure_cell_count": len(value.cells),
            "raw_leaf_occurrence_count": value.raw_leaf_occurrences,
            "odd_leaf_count": len(value.leaves),
            "canceled_leaf_occurrence_count": value.raw_leaf_occurrences - len(value.leaves),
            "registered_endpoint_count": len(endpoint_rows),
            "live_registered_endpoint_count": live_count,
            "earliest_live_path_rank": (
                earliest_live_rank if earliest_live_rank < 10**9 else None
            ),
            "output_polarity": value.constant,
        })
        private_candidates.append({
            "candidate_ref": ref,
            "root_cell_private": root,
            "output_source_private": output,
            "endpoint_pins_private": endpoint_rows,
            "closure_cells_private": sorted(value.cells),
            "odd_leaf_sources_private": sorted(value.leaves),
            "output_polarity": value.constant,
        })
    if source_dcp_sha256 and re.fullmatch(r"[0-9a-f]{64}", source_dcp_sha256) is None:
        raise ParityRecoveryError("parity source identity is invalid")
    private = {
        "source_dcp_sha256": source_dcp_sha256 or None,
        "candidates": private_candidates,
    }
    public = {
        "kind": "registered_gf2_parity_closure_contract",
        "schema_version": SCHEMA_VERSION,
        "status": "proved_read_only",
        "authority": "registered_parity_reassociation_proof_input",
        "mutation_eligible": bool(public_candidates),
        "operator_class": "REGISTERED_GF2_PARITY_REDUCTION",
        "action_class": ACTION_CLASS,
        "proof_template": PROOF_TEMPLATE,
        "source_dcp_sha256": source_dcp_sha256 or None,
        "candidate_count": len(public_candidates),
        "candidates": public_candidates,
        "required_before_mutation": [
            "select one current-wall registered endpoint from the private same-seed family",
            "replay every XOR or XNOR truth table and cancel only even GF2 leaf multiplicities",
            "emit a balanced XOR tree from every odd-multiplicity leaf",
            "preserve the destination register clock CE reset INIT and observable cycle",
            "preserve the original root and every non-selected consumer",
            "bind emitted topology then route and reopen under the unchanged contest clock",
        ],
        "exact_names_public": False,
    }
    public["private_manifest_sha256"] = _digest(private)
    unsigned = json.loads(json.dumps(public, sort_keys=True))
    public["contract_digest"] = _digest(unsigned)
    return {"public_contract": public, "private_manifest": private}


def _tcl_quote(value: str) -> str:
    if any(character in value for character in "\x00\r\n"):
        raise ParityRecoveryError("unsafe Tcl value")
    return "{" + value.replace("}", "\\}") + "}"


def _parity_init(width: int, polarity: int = 0) -> str:
    if not 1 <= width <= 6 or polarity not in {0, 1}:
        raise ParityRecoveryError("unsupported parity LUT shape")
    value = 0
    for address in range(1 << width):
        bit = (address.bit_count() & 1) ^ polarity
        value |= bit << address
    digits = (1 << width) // 4
    return f"{1 << width}'h{value:0{digits}X}"


def build_registered_parity_reassociation_tcl(
    recovered: dict[str, Any],
    candidate_ref: str,
    input_dcp: Path | str,
    output_dcp: Path | str,
    result_tsv: Path | str,
    implementation_mode: str = "local_preserve",
) -> tuple[str, dict[str, Any]]:
    """Emit one balanced exact-XOR tree into a registered D boundary."""

    if not re.fullmatch(r"registered_parity_\d+", candidate_ref):
        raise ParityRecoveryError("invalid registered parity candidate ref")
    if implementation_mode not in {
        "local_preserve", "closure_local_rebasin", "global_quick_rebasin",
    }:
        raise ParityRecoveryError("unsupported registered parity implementation mode")
    candidates = {
        row["candidate_ref"]: row
        for row in (recovered.get("private_manifest") or {}).get("candidates") or []
    }
    candidate = candidates.get(candidate_ref)
    if candidate is None:
        raise ParityRecoveryError("registered parity candidate is absent")
    leaves = list(candidate.get("odd_leaf_sources_private") or [])
    endpoints = list(candidate.get("endpoint_pins_private") or [])
    if len(leaves) < 2 or not endpoints:
        raise ParityRecoveryError("registered parity candidate is not rewritable")
    prefix = "fdagents_parity_" + _digest({
        "candidate_ref": candidate_ref,
        "implementation_mode": implementation_mode,
        "root": candidate.get("root_cell_private"),
        "leaves": leaves,
        "endpoints": endpoints,
    })[:12]
    public = recovered.get("public_contract") or {}
    source_sha256 = str(public.get("source_dcp_sha256") or "")
    if source_sha256 and re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None:
        raise ParityRecoveryError("parity source identity is invalid")
    lines = [
        f"set expected_source_sha {_tcl_quote(source_sha256)}",
        f"if {{$expected_source_sha ne {{}}}} {{set actual_source_sha [lindex [split [exec sha256sum -- {_tcl_quote(str(input_dcp))}]] 0]; if {{$actual_source_sha ne $expected_source_sha}} {{error {{parity source SHA mismatch}}}}}}",
        f"open_checkpoint {_tcl_quote(str(input_dcp))}",
        "proc safe_prop {object name} {if {$object eq {}} {return {}}; if {[catch {get_property -quiet $name $object} value]} {return {}}; return $value}",
        "proc parity_timing_snapshot {} {set clock [lindex [get_clocks -quiet clk_fpl26contest] 0]; if {$clock eq {}} {error {clk_fpl26contest missing}}; set setup [lindex [get_timing_paths -quiet -setup -max_paths 1 -nworst 1 -from $clock -to $clock] 0]; if {$setup eq {}} {error {clk_fpl26contest has no setup path}}; set hold [lindex [get_timing_paths -quiet -hold -max_paths 1 -nworst 1 -from $clock -to $clock] 0]; return [dict create period [safe_prop $clock PERIOD] waveform [safe_prop $clock WAVEFORM] source [safe_prop $clock SOURCE_PINS] wns [safe_prop $setup SLACK] whs [expr {$hold eq {} ? {} : [safe_prop $hold SLACK]}]]}",
        "proc parity_sink_names {net} {set names {}; foreach pin [get_pins -quiet -leaf -of_objects $net -filter {DIRECTION == IN}] {lappend names [get_property NAME $pin]}; return [lsort -unique $names]}",
        "proc parity_cell_names {cells} {set names {}; foreach cell $cells {lappend names [get_property NAME $cell]}; return [lsort -unique $names]}",
        "proc parity_fixed_cell_names {property} {set names {}; foreach cell [get_cells -quiet -hierarchical -filter \"$property == 1\"] {lappend names [get_property NAME $cell]}; return [lsort -unique $names]}",
        "proc parity_originally_movable_cells {cells loc_fixed_names bel_fixed_names} {set movable {}; foreach cell $cells {set name [get_property NAME $cell]; if {[lsearch -exact $loc_fixed_names $name] < 0 && [lsearch -exact $bel_fixed_names $name] < 0} {lappend movable $cell}}; return $movable}",
        "set input_loc_fixed_names [parity_fixed_cell_names IS_LOC_FIXED]",
        "set input_bel_fixed_names [parity_fixed_cell_names IS_BEL_FIXED]",
        "set input_timing [parity_timing_snapshot]",
        "set input_lut_count [llength [get_cells -quiet -hierarchical -filter {REF_NAME =~ LUT*}]]",
        "set input_ff_count [llength [get_cells -quiet -hierarchical -filter {REF_NAME =~ FD*}]]",
        "puts {FDAGENTS_PARITY_PHASE input_snapshot_complete}",
        "proc direct_source_net {key} {",
        "  if {[string match {PIN:*} $key]} {",
        "    set object [get_pins -quiet [string range $key 4 end]]",
        "  } elseif {[string match {PORT:*} $key]} {",
        "    set object [get_ports -quiet [string range $key 5 end]]",
        "  } elseif {[string match {NET:*} $key]} {",
        "    set nets [get_nets -quiet [string range $key 4 end]]",
        "    if {[llength $nets] != 1} {error {parity boundary net is absent or ambiguous}}",
        "    return [lindex $nets 0]",
        "  } else {error {invalid parity boundary source key}}",
        "  if {[llength $object] != 1} {error {parity boundary object is absent or ambiguous}}",
        "  set nets [get_nets -quiet -of_objects $object]",
        "  if {[llength $nets] != 1} {error {parity boundary source net is absent or ambiguous}}",
        "  return [lindex $nets 0]",
        "}",
        "set leaf_nets {}",
    ]
    for leaf in leaves:
        lines.append(f"lappend leaf_nets [direct_source_net {_tcl_quote(leaf)}]")
    signals = [f"[lindex $leaf_nets {index}]" for index in range(len(leaves))]
    created_cells: list[str] = []
    level = 0
    while len(signals) > 1:
        next_signals = []
        for group_index, start in enumerate(range(0, len(signals), 6)):
            group = signals[start:start + 6]
            if len(group) == 1:
                next_signals.append(group[0])
                continue
            cell = f"{prefix}_l{level}_{group_index}"
            net = f"{cell}_n"
            created_cells.append(cell)
            lines.extend([
                f"create_cell -reference LUT{len(group)} {cell}",
                f"set_property INIT {_parity_init(len(group))} [get_cells {cell}]",
                f"create_net {net}",
            ])
            for input_index, signal in enumerate(group):
                lines.append(
                    f"connect_net -hierarchical -net {signal} -objects [get_pins {cell}/I{input_index}]"
                )
            lines.append(
                f"connect_net -hierarchical -net [get_nets {net}] -objects [get_pins {cell}/O]"
            )
            next_signals.append(f"[get_nets {net}]")
        signals = next_signals
        level += 1
    final_signal = signals[0]
    if int(candidate.get("output_polarity", 0)):
        cell = f"{prefix}_polarity"
        net = f"{cell}_n"
        created_cells.append(cell)
        lines.extend([
            f"create_cell -reference LUT1 {cell}",
            f"set_property INIT {_parity_init(1, 1)} [get_cells {cell}]",
            f"create_net {net}",
            f"connect_net -hierarchical -net {final_signal} -objects [get_pins {cell}/I0]",
            f"connect_net -hierarchical -net [get_nets {net}] -objects [get_pins {cell}/O]",
        ])
        final_signal = f"[get_nets {net}]"
    lines.extend([
        "set selected_endpoint {}",
        "set selected_slack {}",
    ])
    for endpoint in endpoints:
        lines.extend([
            f"set endpoint [get_pins -quiet {_tcl_quote(endpoint)}]",
            "if {[llength $endpoint] != 1} {error {parity endpoint is absent}}",
            "set path [get_timing_paths -quiet -delay_type max -max_paths 1 -nworst 1 -to $endpoint]",
            "if {[llength $path]} {",
            "  set slack [get_property SLACK [lindex $path 0]]",
            "  if {$selected_endpoint eq {} || $slack < $selected_slack} {set selected_endpoint $endpoint; set selected_slack $slack}",
            "}",
        ])
    lines.extend([
        "if {$selected_endpoint eq {}} {error {parity candidate has no timed endpoint}}",
        "puts {FDAGENTS_PARITY_PHASE endpoint_selection_complete}",
        "set selected_endpoint_name [get_property NAME $selected_endpoint]",
        "set old_net [get_nets -quiet -of_objects $selected_endpoint]",
        "if {[llength $old_net] != 1} {error {parity endpoint driver is absent or ambiguous}}",
        "set old_net [lindex $old_net 0]",
        "set old_net_name [get_property NAME $old_net]",
        "set old_net_sink_names_before [parity_sink_names $old_net]",
        "if {[lsearch -exact $old_net_sink_names_before $selected_endpoint_name] < 0} {error {selected parity endpoint is absent from the old root sink set}}",
        "set expected_nonselected_sink_names {}",
        "foreach sink_name $old_net_sink_names_before {if {$sink_name ne $selected_endpoint_name} {lappend expected_nonselected_sink_names $sink_name}}",
        "set expected_nonselected_sink_names [lsort -unique $expected_nonselected_sink_names]",
        "disconnect_net -net $old_net -objects $selected_endpoint",
        f"connect_net -hierarchical -net {final_signal} -objects $selected_endpoint",
        "set old_net_sink_names_after_mutation [parity_sink_names $old_net]",
        "if {$old_net_sink_names_after_mutation ne $expected_nonselected_sink_names} {error {parity mutation changed a non-selected old-root consumer}}",
        "set preserved_sibling_count 0",
    ])
    for endpoint in endpoints:
        lines.extend([
            f"set sibling [get_pins -quiet {_tcl_quote(endpoint)}]",
            "if {[llength $sibling] != 1} {error {parity sibling endpoint is absent}}",
            "if {[get_property NAME $sibling] ne $selected_endpoint_name} {",
            "  set sibling_net [get_nets -quiet -of_objects $sibling]",
            "  if {[llength $sibling_net] != 1 || [lindex $sibling_net 0] ne $old_net} {error {parity non-selected consumer changed}}",
            "  incr preserved_sibling_count",
            "}",
        ])
    lines.extend([
        f"if {{$preserved_sibling_count != {len(endpoints) - 1}}} {{error {{parity sibling preservation count mismatch}}}}",
        f"set new_cells [get_cells -quiet {prefix}_*]",
        f"if {{[llength $new_cells] != {len(created_cells)}}} {{error {{parity emitted cell inventory mismatch}}}}",
        "set_property DONT_TOUCH true $new_cells",
        "set rebasin_cell_count 0",
        "set rebasin_site_count 0",
        "set temporary_loc_fixed_cells {}",
        "set temporary_bel_fixed_cells {}",
        "set temporary_loc_fixed_count 0",
        "set temporary_bel_fixed_count 0",
    ])
    if implementation_mode == "local_preserve":
        lines.extend([
            f"set preserved_leaf [get_cells -hierarchical -filter {{IS_PRIMITIVE == 1 && LOC != \"\" && PRIMITIVE_LEVEL == LEAF && NAME !~ {prefix}_*}}]",
            f"set temporary_loc_fixed_cells [get_cells -quiet -hierarchical -filter {{IS_PRIMITIVE == 1 && LOC != \"\" && PRIMITIVE_LEVEL == LEAF && IS_LOC_FIXED != 1 && NAME !~ {prefix}_*}}]",
            f"set temporary_bel_fixed_cells [get_cells -quiet -hierarchical -filter {{IS_PRIMITIVE == 1 && LOC != \"\" && PRIMITIVE_LEVEL == LEAF && IS_BEL_FIXED != 1 && NAME !~ {prefix}_*}}]",
            "set temporary_loc_fixed_count [llength $temporary_loc_fixed_cells]",
            "set temporary_bel_fixed_count [llength $temporary_bel_fixed_cells]",
            "set_property IS_LOC_FIXED true $preserved_leaf",
            "set_property IS_BEL_FIXED true $preserved_leaf",
        ])
    elif implementation_mode == "closure_local_rebasin":
        closure_names = " ".join(
            _tcl_quote(str(name))
            for name in candidate.get("closure_cells_private") or []
        )
        lines.extend([
            f"set closure_cell_names [list {closure_names}]",
            "set closure_cells [get_cells -quiet $closure_cell_names]",
            f"if {{[llength $closure_cells] != {len(candidate.get('closure_cells_private') or [])}}} {{error {{parity closure cell inventory changed before local re-basin}}}}",
            "set rebasin_sites [get_sites -quiet -of_objects $closure_cells]",
            "set rebasin_site_count [llength $rebasin_sites]",
            "if {$rebasin_site_count == 0} {error {parity closure occupies no placed SLICE site}}",
            "set rebasin_site_cells [get_cells -quiet -of_objects $rebasin_sites -filter {IS_PRIMITIVE == 1 && PRIMITIVE_LEVEL == LEAF && LOC =~ SLICE*}]",
            "set rebasin_cells [parity_originally_movable_cells $rebasin_site_cells $input_loc_fixed_names $input_bel_fixed_names]",
            "set rebasin_cell_count [llength $rebasin_cells]",
            "if {$rebasin_cell_count == 0} {error {parity closure sites contain no movable leaf cells}}",
            "catch {set_property IS_LOC_FIXED false $rebasin_cells}",
            "catch {set_property IS_BEL_FIXED false $rebasin_cells}",
            "unplace_cell $rebasin_cells",
            "puts \"FDAGENTS_PARITY_PHASE rebasin_selection_complete sites=$rebasin_site_count count=$rebasin_cell_count\"",
        ])
    else:
        lines.extend([
            f"set rebasin_cells [get_cells -quiet -hierarchical -filter {{IS_PRIMITIVE == 1 && PRIMITIVE_LEVEL == LEAF && LOC =~ SLICE* && IS_LOC_FIXED != 1 && IS_BEL_FIXED != 1 && NAME !~ {prefix}_*}}]",
            "set rebasin_cell_count [llength $rebasin_cells]",
            "if {$rebasin_cell_count == 0} {error {global parity re-basin found no movable slice cells}}",
            "catch {set_property IS_LOC_FIXED false $rebasin_cells}",
            "catch {set_property IS_BEL_FIXED false $rebasin_cells}",
            "puts \"FDAGENTS_PARITY_PHASE rebasin_selection_complete count=$rebasin_cell_count\"",
        ])
    lines.extend([
        "puts {FDAGENTS_PARITY_PHASE place_start}",
        "place_design -directive Quick",
        "puts {FDAGENTS_PARITY_PHASE place_complete}",
        "set first_route_failed [catch {route_design -directive NoTimingRelaxation} first_route_message]",
        "puts \"FDAGENTS_PARITY_PHASE first_route_complete failed=$first_route_failed\"",
        "set first_bad_nets [get_nets -quiet -hierarchical -filter {(ROUTE_STATUS == UNROUTED || ROUTE_STATUS == PARTIALLY_ROUTED) && TYPE != GND && TYPE != VCC}]",
        "set repaired_site_count 0",
        "if {$first_route_failed || [llength $first_bad_nets]} {",
        "  set repair_site_names [dict create]",
        "  foreach net $first_bad_nets {foreach cell [get_cells -quiet -of_objects [get_pins -quiet -leaf -of_objects [get_nets -quiet -segments $net]]] {foreach site [get_sites -quiet -of_objects $cell] {if {[string match {SLICE*} [get_property SITE_TYPE $site]]} {dict set repair_site_names [get_property NAME $site] 1}}}}",
        "  if {[dict size $repair_site_names] == 0} {error \"parity first route failed without repairable SLICE sites: $first_route_message\"}",
        "  set repair_sites [get_sites -quiet [dict keys $repair_site_names]]",
        "  set repair_cells [parity_originally_movable_cells [get_cells -quiet -of_objects $repair_sites] $input_loc_fixed_names $input_bel_fixed_names]",
        "  if {[llength $repair_cells] == 0} {error {parity repair sites contain no cells}}",
        "  set repaired_site_count [llength $repair_sites]",
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
        "set prewrite_loc_fixed_names [parity_fixed_cell_names IS_LOC_FIXED]",
        "set prewrite_bel_fixed_names [parity_fixed_cell_names IS_BEL_FIXED]",
        "if {$prewrite_loc_fixed_names ne $input_loc_fixed_names || $prewrite_bel_fixed_names ne $input_bel_fixed_names} {error {parity temporary placement constraints were not restored before checkpoint write}}",
        "set old_net_after_route [get_nets -quiet $old_net_name]",
        "if {[llength $old_net_after_route] != 1} {error {parity old root net disappeared after route}}",
        "set old_net_sink_names_after_route [parity_sink_names [lindex $old_net_after_route 0]]",
        "if {$old_net_sink_names_after_route ne $expected_nonselected_sink_names} {error {parity route changed a non-selected old-root consumer}}",
        f"write_checkpoint -force {_tcl_quote(str(output_dcp))}",
        "close_design",
        f"open_checkpoint {_tcl_quote(str(output_dcp))}",
        "set output_loc_fixed_names [parity_fixed_cell_names IS_LOC_FIXED]",
        "set output_bel_fixed_names [parity_fixed_cell_names IS_BEL_FIXED]",
        "if {$output_loc_fixed_names ne $input_loc_fixed_names || $output_bel_fixed_names ne $input_bel_fixed_names} {error {parity checkpoint changed placement constraint identity}}",
        "set reopened_old_net [get_nets -quiet $old_net_name]",
        "if {[llength $reopened_old_net] != 1} {error {parity old root net disappeared after checkpoint reopen}}",
        "set old_net_sink_names_after_reopen [parity_sink_names [lindex $reopened_old_net 0]]",
        "if {$old_net_sink_names_after_reopen ne $expected_nonselected_sink_names} {error {parity checkpoint changed a non-selected old-root consumer}}",
        "set output_timing [parity_timing_snapshot]",
        "if {[dict get $input_timing period] ne [dict get $output_timing period] || [dict get $input_timing waveform] ne [dict get $output_timing waveform] || [dict get $input_timing source] ne [dict get $output_timing source]} {error {parity rewrite changed contest clock identity}}",
        "set route_text [report_route_status -return_string]",
        "set route_errors 0",
        "if {[regexp -nocase {# of nets with routing errors[^0-9]*([0-9]+)} $route_text -> value]} {set route_errors $value}",
        "set pulse_violations [regexp -all -nocase {VIOLATED} [report_pulse_width -return_string]]",
        "set output_lut_count [llength [get_cells -quiet -hierarchical -filter {REF_NAME =~ LUT*}]]",
        "set output_ff_count [llength [get_cells -quiet -hierarchical -filter {REF_NAME =~ FD*}]]",
        f"set out [open {_tcl_quote(str(result_tsv))} w]",
        "puts $out {metric\tvalue}",
        "puts $out {backend\tregistered_gf2_parity_reassociation_v1}",
        "puts $out {contract\tREGISTERED_GF2_PARITY_REASSOCIATION_V1}",
        f"puts $out {{implementation_mode\t{implementation_mode}}}",
        "puts $out \"rebasin_cell_count\t$rebasin_cell_count\"",
        "puts $out \"rebasin_site_count\t$rebasin_site_count\"",
        "puts $out \"input_loc_fixed_count\t[llength $input_loc_fixed_names]\"",
        "puts $out \"input_bel_fixed_count\t[llength $input_bel_fixed_names]\"",
        "puts $out \"output_loc_fixed_count\t[llength $output_loc_fixed_names]\"",
        "puts $out \"output_bel_fixed_count\t[llength $output_bel_fixed_names]\"",
        "puts $out \"temporary_loc_fixed_count\t$temporary_loc_fixed_count\"",
        "puts $out \"temporary_bel_fixed_count\t$temporary_bel_fixed_count\"",
        "puts $out {placement_fix_identity_preserved\t1}",
        f"puts $out {{candidate_ref\t{candidate_ref}}}",
        "puts $out \"source_dcp_sha256\t$expected_source_sha\"",
        f"puts $out {{odd_leaf_count\t{len(leaves)}}}",
        f"puts $out {{new_lut_count\t{len(created_cells)}}}",
        f"puts $out {{old_closure_cell_count\t{len(candidate.get('closure_cells_private') or [])}}}",
        f"puts $out {{registered_endpoint_count\t{len(endpoints)}}}",
        "puts $out \"preserved_sibling_count\t$preserved_sibling_count\"",
        "puts $out \"old_root_sink_count_before\t[llength $old_net_sink_names_before]\"",
        "puts $out \"nonselected_sink_count_expected\t[llength $expected_nonselected_sink_names]\"",
        "puts $out \"nonselected_sink_count_after_route\t[llength $old_net_sink_names_after_route]\"",
        "puts $out \"nonselected_sink_count_after_reopen\t[llength $old_net_sink_names_after_reopen]\"",
        "puts $out {all_nonselected_sink_sets_equal\t1}",
        "puts $out \"repaired_site_count\t$repaired_site_count\"",
        "puts $out \"selected_endpoint_slack_before_ns\t$selected_slack\"",
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
        "backend": "registered_gf2_parity_reassociation_v1",
        "candidate_ref": candidate_ref,
        "implementation_mode": implementation_mode,
        "odd_leaf_count": len(leaves),
        "new_lut_count": len(created_cells),
        "tree_levels": level + int(bool(candidate.get("output_polarity", 0))),
        "registered_endpoint_count": len(endpoints),
        "old_closure_cell_count": len(candidate.get("closure_cells_private") or []),
        "preserved_sibling_count": len(endpoints) - 1,
    }
    topology["manifest_sha256"] = _digest(topology)
    return "\n".join(lines), topology


__all__ = [
    "ParityRecoveryError", "recover_registered_parity_closures",
    "build_registered_parity_reassociation_tcl",
]
