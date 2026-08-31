"""Vivado Tcl guards for immutable timing-exception source identities."""

from __future__ import annotations


def protect_timing_exception_sources_tcl() -> str:
    """Return Tcl that prevents implementation from cloning exception sources.

    ``write_xdc -exclude_physical`` expands a cloned exact false-path source
    into every replica, which changes the contest timing XDC even though the
    clock itself is unchanged.  Vivado 2025.1 has no public
    ``get_timing_exceptions`` object command, so recover only the exact source
    objects printed by ``report_exceptions``.  Targets are deliberately not
    protected: this is an identity invariant, not a broad optimization gate.
    """

    return "\n".join([
        "set fdagents_exception_report [report_exceptions -return_string]",
        "set fdagents_exception_source_cells [list]",
        "foreach fdagents_exception_line [split $fdagents_exception_report \\n] {",
        "  set fdagents_exception_source {}",
        "  if {[regexp {^\\s*[0-9]+\\s+\\[get_pins\\s+([^\\]]+)\\]} $fdagents_exception_line -> fdagents_exception_source]} {",
        "    set fdagents_exception_source [string trim $fdagents_exception_source \"{}\"]",
        "    foreach fdagents_exception_pin [get_pins -quiet $fdagents_exception_source] {",
        "      foreach fdagents_exception_cell [get_cells -quiet -of_objects $fdagents_exception_pin] {",
        "        lappend fdagents_exception_source_cells $fdagents_exception_cell",
        "      }",
        "    }",
        "  } elseif {[regexp {^\\s*[0-9]+\\s+\\[get_cells\\s+([^\\]]+)\\]} $fdagents_exception_line -> fdagents_exception_source]} {",
        "    set fdagents_exception_source [string trim $fdagents_exception_source \"{}\"]",
        "    foreach fdagents_exception_cell [get_cells -quiet $fdagents_exception_source] {",
        "      lappend fdagents_exception_source_cells $fdagents_exception_cell",
        "    }",
        "  }",
        "}",
        "set fdagents_exception_source_cells [lsort -unique $fdagents_exception_source_cells]",
        "if {[llength $fdagents_exception_source_cells] > 0} {",
        "  set_property DONT_TOUCH true $fdagents_exception_source_cells",
        "}",
        "puts \"FDAGENTS_TIMING_EXCEPTION_SOURCES_PROTECTED=[llength $fdagents_exception_source_cells]\"",
    ])


def inject_timing_exception_source_guard(connect_tcl: str) -> str:
    """Protect exception sources before the connect/cleanup transaction."""

    marker = "open_checkpoint [lindex $argv 0]\n"
    if connect_tcl.count(marker) != 1:
        raise ValueError("connect Tcl has an ambiguous source checkpoint open")
    return connect_tcl.replace(
        marker,
        marker + protect_timing_exception_sources_tcl() + "\n",
        1,
    )


def exact_timing_xdc_capture_tcl(source_dcp: str, timing_xdc: str) -> str:
    """Capture the immutable source timing contract before mutation."""

    return "\n".join([
        f"open_checkpoint {source_dcp}",
        f"write_xdc -force -exclude_physical {timing_xdc}",
        "close_design",
    ])


def exact_timing_xdc_restore_tcl(timing_xdc: str) -> str:
    """Reload the exact source timing contract after implementation."""

    return "\n".join([
        "reset_timing",
        f"read_xdc {timing_xdc}",
        "update_timing",
    ])
