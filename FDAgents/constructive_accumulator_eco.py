"""Certificate-bound ECO for a recovered constant-product accumulator family."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .compressor_proof import ConeGraph, _carry_chain, _digest


CONSTRUCTIVE_IMPLEMENTATION_POLICY = "minimal_current_seed_route_v1"


def _implementation_lines() -> list[str]:
    """Return only the implementation needed to measure this semantic ECO.

    QoR exploration is deliberately deferred until the routed candidate has
    been measured, promoted, and freshly re-profiled.  In particular this
    backend does not inherit a named pblock or append a fixed phys-opt/route
    sequence from a historical design.
    """
    return [
        "place_design -directive Default",
        "write_checkpoint -force [file join $output_dir placed.dcp]",
        "report_timing -delay_type max -max_paths 50 -file [file join $output_dir placed_timing.rpt]",
        "route_design -directive Default",
    ]


def _q(value: str) -> str:
    if not value or any(character in value for character in "{}\n\r"):
        raise ValueError("unsafe Tcl object name")
    return "{" + value + "}"


def _token_kind(token: str) -> tuple[str, str]:
    match = re.fullmatch(r"NOT\((.*)\)", token)
    return ("not", match.group(1)) if match else ("plain", token)


def _site_xy(site: str) -> tuple[int, int]:
    match = re.fullmatch(r"SLICE_X(\d+)Y(\d+)", site)
    if match is None:
        raise ValueError(f"invalid SLICE site {site}")
    return int(match.group(1)), int(match.group(2))


def _verify_certificate(certificate: dict, graph: ConeGraph) -> None:
    claimed = certificate.get("certificate_id")
    payload = dict(certificate)
    payload.pop("certificate_id", None)
    identity = certificate.get("source_identity") or {}
    integration = certificate.get("integration") or {}
    proof = certificate.get("constructive_proof") or {}
    if (
        certificate.get("kind")
        != "constructive_registered_accumulator_lowering"
        or certificate.get("schema_version")
        != "constructive-accumulator-lowering-v2"
        or certificate.get("proof_template_id")
        != "CONSTRUCTIVE_ACCUMULATOR_GENERIC_V2"
        or certificate.get("status") != "proved"
        or certificate.get("mutation_ready") is not True
        or not isinstance(claimed, str)
        or _digest(payload) != claimed
        or identity.get("dcp_sha256") != graph.manifest["dcp_sha256"]
        or identity.get("connectivity_sha256") != graph.connectivity_digest()
        or identity.get("target_stem") != graph.manifest["target_stem"]
        or integration.get("replaceable_old_cells") != sorted(graph.cells)
        or integration.get("side_fanout_count") != 0
        or not isinstance(proof.get("coefficient"), int)
        or proof.get("coefficient", 0) <= 0
        or proof.get("operand_order") not in {"natural", "swapped"}
    ):
        raise ValueError("constructive certificate binding verification failed")


def _aligned_carry_placement(
    graph: ConeGraph, *, width: int, numerator_positions: list[int]
) -> dict:
    """Align recovered numerator bits to the incumbent registered carry slice.

    This derives the lane offset and extension sites from live connectivity and
    LOCs.  It has no fixed width, coefficient, family size, coordinate, or
    target-count assumption.
    """
    chain = _carry_chain(graph)
    if not numerator_positions or len(numerator_positions) != len(graph.targets):
        raise ValueError("constructive numerator positions are incomplete")
    if numerator_positions != list(
        range(numerator_positions[0], numerator_positions[0] + len(numerator_positions))
    ):
        raise ValueError("constructive numerator positions are not contiguous")
    locations = [_site_xy(graph.cells[name].loc) for name in chain["cells"]]
    xs = {row[0] for row in locations}
    ys = [row[1] for row in locations]
    if len(xs) != 1 or ys != list(range(ys[0], ys[0] + len(ys))):
        raise ValueError("incumbent final carry chain is not one consecutive column")
    delta = int(numerator_positions[0]) - int(chain["target_positions"][0])
    lower_extension_sites = max(0, (delta + 7) // 8)
    bit_offset = lower_extension_sites * 8 - delta
    if not 0 <= bit_offset <= 7:
        raise ValueError("constructive carry alignment could not be normalized")
    carry_count = (bit_offset + int(width) + 7) // 8
    base_y = ys[0] - lower_extension_sites
    if base_y < 0:
        raise ValueError("constructive carry alignment extends below the device")
    x = next(iter(xs))
    sites = [f"SLICE_X{x}Y{base_y + index}" for index in range(carry_count)]
    for numerator, old_position in zip(
        numerator_positions, chain["target_positions"]
    ):
        if numerator + bit_offset - lower_extension_sites * 8 != old_position:
            raise ValueError("constructive target-to-carry alignment is inconsistent")
    return {
        "sites": sites,
        "bit_offset": bit_offset,
        "carry_count": carry_count,
        "numerator_to_incumbent_delta": delta,
        "incumbent_chain_cells": list(chain["cells"]),
    }


def _emit_one(
    tag: str, graph: ConeGraph, certificate: dict
) -> tuple[list[str], list[str], dict]:
    prefix = f"fdagents_cacc_{tag}"
    spec = certificate["compressor_spec"]
    width = int(spec["width"])
    numerator_positions = [
        int(value)
        for value in certificate["integration"]["target_numerator_positions"]
    ]
    placement = _aligned_carry_placement(
        graph, width=width, numerator_positions=numerator_positions
    )
    lines: list[str] = [f"puts \"CONSTRUCTIVE_ACCUM_BEGIN {tag}\""]
    new_cells: list[str] = []

    for target in graph.targets:
        lines.extend([
            f"set keep_net [get_nets {_q(target['d_net'])}]",
            "foreach driver [get_pins -quiet -leaf -of_objects $keep_net -filter {DIRECTION == OUT}] { disconnect_net -net $keep_net -objects $driver }",
        ])
    old_names = sorted(graph.cells)
    lines.extend([
        "set old_cells [get_cells -quiet [list "
        + " ".join(_q(name) for name in old_names) + "]]",
        f"if {{[llength $old_cells] != {len(old_names)}}} {{ error {{{tag} old-cell manifest mismatch}} }}",
        "remove_cell $old_cells",
    ])

    false_nets = sorted(net for net, value in graph.static_nets.items() if not value)
    if not false_nets:
        raise ValueError("constructive cone has no extracted constant-zero net")
    gnd_net = false_nets[0]
    vcc_cell = f"{prefix}_vcc"
    vcc_net = f"{prefix}_vcc_net"
    new_cells.append(vcc_cell)
    lines.extend([
        f"create_cell -reference VCC {_q(vcc_cell)}",
        f"create_net {_q(vcc_net)}",
        f"connect_net -hierarchical -net [get_nets {_q(vcc_net)}] -objects [get_pins {_q(vcc_cell + '/P')}]",
    ])

    source_nets = certificate["integration"]["source_nets"]
    token_net: dict[str, str] = {"CONST0": gnd_net, "CONST1": vcc_net, **source_nets}
    all_tokens = {
        token for operation in spec["compressors"] for token in operation["inputs"]
    }
    all_tokens.update(token for row in spec["final_rows"] for token in row)
    for token in sorted(all_tokens):
        kind, source = _token_kind(token)
        if kind != "not":
            token_net.setdefault(token, token)
            continue
        cell = f"{prefix}_not_{sum('_not_' in name for name in new_cells)}"
        net = f"{cell}_net"
        new_cells.append(cell)
        token_net[token] = net
        lines.extend([
            f"create_cell -reference LUT1 {_q(cell)}",
            f"set_property INIT 2'h1 [get_cells {_q(cell)}]",
            f"create_net {_q(net)}",
            f"connect_net -hierarchical -net [get_nets {_q(source_nets[source])}] -objects [get_pins {_q(cell + '/I0')}]",
            f"connect_net -hierarchical -net [get_nets {_q(net)}] -objects [get_pins {_q(cell + '/O')}]",
        ])

    for ordinal, operation in enumerate(spec["compressors"]):
        cell = f"{prefix}_csa_{ordinal}"
        sum_net = f"{cell}_sum"
        carry_net = f"{cell}_carry"
        new_cells.append(cell)
        token_net[operation["sum"]] = sum_net
        if operation["carry"] is not None:
            token_net[operation["carry"]] = carry_net
        lines.extend([
            f"create_cell -reference LUT6_2 {_q(cell)}",
            f"set_property INIT {operation['init']} [get_cells {_q(cell)}]",
            f"create_net {_q(sum_net)}",
            f"connect_net -hierarchical -net [get_nets {_q(sum_net)}] -objects [get_pins {_q(cell + '/O6')}]",
        ])
        if operation["carry"] is not None:
            lines.extend([
                f"create_net {_q(carry_net)}",
                f"connect_net -hierarchical -net [get_nets {_q(carry_net)}] -objects [get_pins {_q(cell + '/O5')}]",
            ])
        for pin_index, token in enumerate(operation["inputs"]):
            lines.append(
                f"connect_net -hierarchical -net [get_nets {_q(token_net[token])}] -objects [get_pins {_q(cell + f'/I{pin_index}')}]"
            )
        for pin_index in range(len(operation["inputs"]), 5):
            lines.append(
                f"connect_net -hierarchical -net [get_nets {_q(gnd_net)}] -objects [get_pins {_q(cell + f'/I{pin_index}')}]"
            )
        lines.append(
            f"connect_net -hierarchical -net [get_nets {_q(vcc_net)}] -objects [get_pins {_q(cell + '/I5')}]"
        )

    carry_cells = []
    for index in range(placement["carry_count"]):
        cell = f"{prefix}_final_carry_{index}"
        carry_cells.append(cell)
        new_cells.append(cell)
        lines.extend([
            f"create_cell -reference CARRY8 {_q(cell)}",
            f"set_property CARRY_TYPE SINGLE_CY8 [get_cells {_q(cell)}]",
            f"connect_net -hierarchical -net [get_nets {_q(gnd_net)}] -objects [get_pins {_q(cell + '/CI_TOP')}]",
        ])
        if index == 0:
            lines.append(
                f"connect_net -hierarchical -net [get_nets {_q(gnd_net)}] -objects [get_pins {_q(cell + '/CI')}]"
            )
        else:
            link = f"{prefix}_final_carry_link_{index - 1}"
            lines.extend([
                f"create_net {_q(link)}",
                f"connect_net -hierarchical -net [get_nets {_q(link)}] -objects [list [get_pins {_q(carry_cells[index - 1] + '/CO[7]')}] [get_pins {_q(cell + '/CI')}]]",
            ])

    target_by_position = dict(zip(numerator_positions, graph.targets))
    occupied_positions = set()
    bit_offset = int(placement["bit_offset"])
    final_majority_nets: list[str] = []
    for position in range(width):
        physical_position = position + bit_offset
        occupied_positions.add(physical_position)
        carry_index, lane = divmod(physical_position, 8)
        carry = carry_cells[carry_index]
        csa = f"{prefix}_final_csa_{position}"
        sum_net = f"{csa}_sum"
        majority_net = f"{csa}_majority"
        new_cells.append(csa)
        final_majority_nets.append(majority_net)
        rows = [spec["final_rows"][index][position] for index in range(3)]
        lines.extend([
            f"create_cell -reference LUT6_2 {_q(csa)}",
            f"set_property INIT 64'h96969696E8E8E8E8 [get_cells {_q(csa)}]",
            f"create_net {_q(sum_net)}",
            f"create_net {_q(majority_net)}",
            f"connect_net -hierarchical -net [get_nets {_q(token_net[rows[0]])}] -objects [get_pins {_q(csa + '/I0')}]",
            f"connect_net -hierarchical -net [get_nets {_q(token_net[rows[1]])}] -objects [get_pins {_q(csa + '/I1')}]",
            f"connect_net -hierarchical -net [get_nets {_q(token_net[rows[2]])}] -objects [get_pins {_q(csa + '/I2')}]",
            f"connect_net -hierarchical -net [get_nets {_q(gnd_net)}] -objects [list [get_pins {_q(csa + '/I3')}] [get_pins {_q(csa + '/I4')}]]",
            f"connect_net -hierarchical -net [get_nets {_q(vcc_net)}] -objects [get_pins {_q(csa + '/I5')}]",
            f"connect_net -hierarchical -net [get_nets {_q(sum_net)}] -objects [get_pins {_q(csa + '/O6')}]",
            f"connect_net -hierarchical -net [get_nets {_q(majority_net)}] -objects [get_pins {_q(csa + '/O5')}]",
        ])
        xor_cell = f"{prefix}_final_xor_{position}"
        select_net = f"{xor_cell}_select"
        shifted_majority = (
            gnd_net if position == 0 else final_majority_nets[position - 1]
        )
        new_cells.append(xor_cell)
        lines.extend([
            f"create_cell -reference LUT2 {_q(xor_cell)}",
            f"set_property INIT 4'h6 [get_cells {_q(xor_cell)}]",
            f"create_net {_q(select_net)}",
            f"connect_net -hierarchical -net [get_nets {_q(sum_net)}] -objects [list [get_pins {_q(xor_cell + '/I0')}] [get_pins {_q(carry + f'/DI[{lane}]')}]]",
            f"connect_net -hierarchical -net [get_nets {_q(shifted_majority)}] -objects [get_pins {_q(xor_cell + '/I1')}]",
            f"connect_net -hierarchical -net [get_nets {_q(select_net)}] -objects [list [get_pins {_q(xor_cell + '/O')}] [get_pins {_q(carry + f'/S[{lane}]')}]]",
        ])
        target = target_by_position.get(position)
        if target is not None:
            lines.append(
                f"connect_net -hierarchical -net [get_nets {_q(target['d_net'])}] -objects [get_pins {_q(carry + f'/O[{lane}]')}]"
            )
    for physical_position in range(placement["carry_count"] * 8):
        if physical_position in occupied_positions:
            continue
        carry_index, lane = divmod(physical_position, 8)
        for pin in ("S", "DI"):
            lines.append(
                f"connect_net -hierarchical -net [get_nets {_q(gnd_net)}] -objects [get_pins {_q(carry_cells[carry_index] + f'/{pin}[{lane}]')}]"
            )

    lines.append("set cacc_loc_available 1")
    for site in placement["sites"]:
        lines.append(
            f"if {{[llength [get_cells -quiet -of_objects [get_sites {_q(site)}]]] != 0}} {{ set cacc_loc_available 0 }}"
        )
    lines.append("if {$cacc_loc_available} {")
    for cell, site in zip(carry_cells, placement["sites"]):
        lines.append(f"  set_property LOC {_q(site)} [get_cells {_q(cell)}]")
    lines.extend(["} else {", f"  puts \"CONSTRUCTIVE_ACCUM_LOC_FALLBACK {tag}\"", "}"])

    for target in graph.targets:
        lines.extend([
            f"set check_net [get_nets {_q(target['d_net'])}]",
            "set check_drivers [get_pins -quiet -leaf -of_objects $check_net -filter {DIRECTION == OUT}]",
            "if {[llength $check_drivers] != 1} { error \"constructive target driver check failed for $check_net: $check_drivers\" }",
        ])
    lines.extend([
        "set emitted_cells [get_cells -quiet [list "
        + " ".join(_q(name) for name in new_cells) + "]]",
        f"if {{[llength $emitted_cells] != {len(new_cells)}}} {{ error {{{tag} new-cell manifest mismatch}} }}",
        "set_property DONT_TOUCH true $emitted_cells",
        "set all_new_cells [concat $all_new_cells $emitted_cells]",
        f"puts \"CONSTRUCTIVE_ACCUM_END {tag} cells={len(new_cells)}\"",
    ])
    return lines, new_cells, placement


def build_constructive_accumulator_eco(
    seed_dcp: Path,
    output_dir: Path,
    specs: list[tuple[str, Path, Path]],
) -> tuple[str, dict]:
    """Render one hash-bound family ECO from fresh private cone certificates."""
    loaded = []
    source_sha = None
    all_old: set[str] = set()
    for tag, cone_root, certificate_path in specs:
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", tag) is None:
            raise ValueError("invalid cone tag")
        graph = ConeGraph.load(cone_root)
        certificate = json.loads(Path(certificate_path).read_text(encoding="utf-8"))
        _verify_certificate(certificate, graph)
        current_sha = graph.manifest["dcp_sha256"]
        source_sha = current_sha if source_sha is None else source_sha
        if current_sha != source_sha:
            raise ValueError("constructive cones do not bind one seed")
        overlap = all_old.intersection(graph.cells)
        if overlap:
            raise ValueError(f"constructive cones overlap: {sorted(overlap)[:3]}")
        all_old.update(graph.cells)
        loaded.append((tag, graph, certificate))
    if not loaded:
        raise ValueError("constructive family is empty")

    lines = [
        f"set seed_dcp {_q(str(seed_dcp.resolve()))}",
        f"set output_dir {_q(str(output_dir.resolve()))}",
        f"set expected_sha {_q(str(source_sha))}",
        "file mkdir $output_dir",
        "set actual_sha [lindex [split [exec sha256sum -- $seed_dcp]] 0]",
        "if {$actual_sha ne $expected_sha} { error {constructive accumulator seed SHA mismatch} }",
        "open_checkpoint $seed_dcp",
        "set all_new_cells {}",
    ]
    total_new = 0
    all_new_names = []
    placements = []
    for tag, graph, certificate in loaded:
        chunk, new_cells, placement = _emit_one(tag, graph, certificate)
        lines.extend(chunk)
        total_new += len(new_cells)
        all_new_names.extend(new_cells)
        placements.append({"tag": tag, **placement})
    lines.extend([
        f"if {{[llength $all_new_cells] != {total_new}}} {{ error {{combined new-cell count mismatch}} }}",
        "set preserved_leaf_cells [get_cells -hierarchical -filter {IS_PRIMITIVE == 1 && LOC != \"\" && PRIMITIVE_LEVEL == LEAF && NAME !~ fdagents_cacc_*}]",
        "set preserved_macro_cells [get_cells -hierarchical -filter {IS_PRIMITIVE == 1 && LOC != \"\" && PRIMITIVE_LEVEL == MACRO && NAME !~ fdagents_cacc_*}]",
        "set_property IS_LOC_FIXED true $preserved_leaf_cells",
        "set_property IS_BEL_FIXED true $preserved_leaf_cells",
        "set_property IS_LOC_FIXED true $preserved_macro_cells",
        "set_property IS_BEL_FIXED true $preserved_macro_cells",
    ])
    lines.extend(_implementation_lines())
    lines.extend([
        "set old_remaining [get_cells -quiet [list "
        + " ".join(_q(name) for name in sorted(all_old)) + "]]",
        "if {[llength $old_remaining] != 0} { error {constructive old cone remains after route} }",
        "set emitted_reopen [get_cells -quiet [list "
        + " ".join(_q(name) for name in all_new_names) + "]]",
        f"if {{[llength $emitted_reopen] != {total_new}}} {{ error {{constructive emitted inventory changed}} }}",
        "set route_status [report_route_status -return_string]",
        "if {![regexp {# of nets with routing errors[^:]*:[[:space:]]*0[[:space:]]*:} $route_status]} { error {constructive candidate is not fully routed} }",
        "set contest_clock [get_clocks -quiet clk_fpl26contest]",
        "if {[llength $contest_clock] != 1} { error {constructive contest clock missing} }",
        "set timing_path [get_timing_paths -quiet -max_paths 1 -setup -to $contest_clock]",
        "if {[llength $timing_path] != 1} { error {constructive setup timing unavailable} }",
        "set wns [get_property SLACK [lindex $timing_path 0]]",
        "report_timing_summary -delay_type min_max -max_paths 100 -file [file join $output_dir timing_summary.rpt]",
        "report_timing -delay_type max -max_paths 300 -input_pins -file [file join $output_dir timing_paths.rpt]",
        "report_route_status -file [file join $output_dir route_status.rpt]",
        "report_drc -file [file join $output_dir drc.rpt]",
        "report_utilization -file [file join $output_dir utilization.rpt]",
        "write_checkpoint -force [file join $output_dir candidate.dcp]",
        "set fp [open [file join $output_dir result.txt] w]",
        "puts $fp \"wns=$wns\"",
        f"puts $fp \"removed_cells={len(all_old)}\"",
        f"puts $fp \"new_cells={total_new}\"",
        "puts $fp \"old_cells_remaining=[llength $old_remaining]\"",
        "close $fp",
        "exit",
        "",
    ])
    topology = {
        "source_sha256": source_sha,
        "family_size": len(loaded),
        "old_cells": sorted(all_old),
        "new_cells": all_new_names,
        "target_d_nets": sorted(
            target["d_net"]
            for _tag, graph, _certificate in loaded
            for target in graph.targets
        ),
        "removed_cell_count": len(all_old),
        "emitted_cell_count": total_new,
        "placements": placements,
        "implementation_policy": {
            "policy_id": CONSTRUCTIVE_IMPLEMENTATION_POLICY,
            "placement": "Default",
            "route": "Default",
            "post_route_qor_actions_deferred_until_fresh_reprofile": True,
            "inherited_pblock": False,
        },
    }
    topology["manifest_sha256"] = _digest(topology)
    return "\n".join(lines), topology


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("seed_dcp", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("output_tcl", type=Path)
    parser.add_argument(
        "--cone", action="append", nargs=3, required=True,
        metavar=("TAG", "CONE_ROOT", "CERTIFICATE"),
    )
    args = parser.parse_args()
    if args.output_tcl.exists():
        raise SystemExit("output Tcl already exists")
    tcl, topology = build_constructive_accumulator_eco(
        args.seed_dcp,
        args.output_dir,
        [(tag, Path(cone), Path(cert)) for tag, cone, cert in args.cone],
    )
    args.output_tcl.write_text(tcl, encoding="utf-8")
    print(json.dumps({
        "cone_count": topology["family_size"],
        "new_cells": topology["emitted_cell_count"],
        "remove_cells": topology["removed_cell_count"],
        "source_sha256": topology["source_sha256"],
        "output_tcl": str(args.output_tcl),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
