"""Reopen audit Tcl for registered meta-loop ECO candidates."""

from __future__ import annotations

from pathlib import Path


def _q(value: str) -> str:
    if not value or any(char in value for char in "{}\n\r"):
        raise ValueError("unsafe audit object")
    return "{" + value + "}"


def _common(candidate: Path, source: Path, source_sha256: str) -> list[str]:
    return [
        f"set candidate {_q(str(candidate.resolve()))}",
        f"set source {_q(str(source.resolve()))}",
        f"set expected_source_sha {_q(source_sha256)}",
        "open_checkpoint $candidate",
        "set route_status [report_route_status -return_string]",
        "if {![regexp {# of nets with routing errors[^:]*:[[:space:]]*0[[:space:]]*:} $route_status]} {error {candidate reopen routing audit failed}}",
    ]


def _finish(marker: str) -> list[str]:
    return [
        "close_design",
        "if {[lindex [split [exec sha256sum -- $source]] 0] ne $expected_source_sha} {error {source identity changed}}",
        "open_checkpoint $source",
        f"puts {{{marker}}}",
        "close_design",
        "exit",
        "",
    ]


def render_constructive_audit_tcl(
    *, candidate: Path, source: Path, source_sha256: str, topology: dict
) -> str:
    lines = _common(candidate, source, source_sha256)
    lines.extend([
        "set old_cells [get_cells -quiet [list "
        + " ".join(_q(name) for name in topology["old_cells"]) + "]]",
        "if {[llength $old_cells] != 0} {error {constructive old cone survived reopen}}",
        "set new_cells [get_cells -quiet [list "
        + " ".join(_q(name) for name in topology["new_cells"]) + "]]",
        f"if {{[llength $new_cells] != {topology['emitted_cell_count']}}} {{error {{constructive namespace inventory failed}}}}",
    ])
    for net in topology["target_d_nets"]:
        lines.extend([
            f"set drivers [get_pins -quiet -leaf -of_objects [get_nets {_q(net)}] -filter {{DIRECTION == OUT}}]",
            "if {[llength $drivers] != 1 || [string first {fdagents_cacc_} [get_property NAME [get_cells -of_objects $drivers]]] != 0} {error {constructive target driver closure failed}}",
        ])
    lines.extend(_finish("FDAGENTS_CONSTRUCTIVE_REOPEN_AUDIT_OK"))
    return "\n".join(lines)


def render_memory_forward_audit_tcl(
    *, candidate: Path, source: Path, source_sha256: str, topology: dict
) -> str:
    lines = _common(candidate, source, source_sha256)
    lines.extend([
        "set new_cells [get_cells -quiet [list "
        + " ".join(_q(name) for name in topology["new_cells"]) + "]]",
        f"if {{[llength $new_cells] != {len(topology['new_cells'])}}} {{error {{memory-forward namespace inventory failed}}}}",
    ])
    memory = topology["memory"]
    for pin in topology["write_pins"]:
        lines.extend([
            f"set drivers [get_pins -quiet -leaf -of_objects [get_nets -segments -of_objects [get_pins {_q(memory + '/' + pin)}]] -filter {{DIRECTION == OUT}}]",
            "if {[llength $drivers] != 1 || [string first {fdagents_memfwd_wdata_} [get_property NAME [get_cells -of_objects $drivers]]] != 0} {error {memory-forward write boundary failed}}",
        ])
    for pin in topology["read_consumer_pins"]:
        lines.extend([
            f"set drivers [get_pins -quiet -leaf -of_objects [get_nets -segments -of_objects [get_pins {_q(pin)}]] -filter {{DIRECTION == OUT}}]",
            "if {[llength $drivers] != 1 || [string first {fdagents_memfwd_mux_} [get_property NAME [get_cells -of_objects $drivers]]] != 0} {error {memory-forward read consumer closure failed}}",
        ])
    lines.extend(_finish("FDAGENTS_MEMORY_FORWARD_REOPEN_AUDIT_OK"))
    return "\n".join(lines)
