"""
critical_net_reroute skill -- route-preserving reroute of small critical net sets.

This is a generic Vivado-only micro ECO. It does not remember benchmark or
instance names; it extracts current critical-path pins, resolves their nets in
the open design, reroutes only small candidate net windows, and accepts only
legal routed checkpoints.
"""

from __future__ import annotations

import json
import hashlib
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

from ..artifacts import sha256_file

from .base import (
    SkillOutput,
    SkillResult,
    candidate_budget_exhausted,
    calculate_fmax,
    parse_route_status_static,
    parse_timing_summary_static,
)

logger = logging.getLogger(__name__)

_DATA_PIN_RE = re.compile(r"/(?:I[0-5]|O|Q|D)$")
_DSP_SUBPRIM_RE = re.compile(r"/DSP_[A-Z_]+_INST/")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_DIRECTIVE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _tcl_quote(value: str) -> str:
    return "{" + value.replace("\\", "\\\\").replace("}", "\\}") + "}"


def _safe_name(value: str) -> str:
    return _SAFE_NAME_RE.sub("_", value).strip("_") or "candidate"


def _path_pins(path: Any) -> list[str]:
    if isinstance(path, list):
        return [str(pin) for pin in path if isinstance(pin, str)]
    if isinstance(path, dict):
        pins = path.get("pins") or path.get("path") or path.get("cell_pins") or []
        if isinstance(pins, list):
            return [str(pin) for pin in pins if isinstance(pin, str)]
    return []


def _candidate_pin_windows(
    critical_paths: list,
    *,
    num_paths: int = 5,
    max_pins: int = 12,
    window_size: int = 4,
    max_candidates: int = 6,
) -> list[list[str]]:
    """Build small ordered pin windows from worst critical paths."""
    windows: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    window_size = max(1, int(window_size))

    for path in critical_paths[: max(1, num_paths)]:
        data_pins = []
        for pin in _path_pins(path):
            if _DSP_SUBPRIM_RE.search(pin):
                continue
            if not _DATA_PIN_RE.search(pin):
                continue
            if pin not in data_pins:
                data_pins.append(pin)
            if len(data_pins) >= max_pins:
                break

        if not data_pins:
            continue
        if len(data_pins) <= window_size:
            candidates = [data_pins]
        else:
            candidates = [
                data_pins[start : start + window_size]
                for start in range(0, len(data_pins) - window_size + 1)
            ]

        for candidate in candidates:
            key = tuple(candidate)
            if key in seen:
                continue
            seen.add(key)
            windows.append(candidate)
            if len(windows) >= max_candidates:
                return windows

    return windows


def _discovery_refusal(output: str) -> str:
    """Return the discovery Tcl's own refusal, which names the blocking check.

    The Tcl distinguishes an ambiguous clock, startpoint pin, source cell or
    net from a clock net, an immutable net and an over-fanout net.  Reporting
    only that no net resolved discards which of those held, so the run records
    a generic refusal for seven unrelated causes.
    """
    for line in reversed(str(output).splitlines()):
        stripped = line.strip()
        if "FDAGENTS " in stripped and "FDAGENTS_" not in stripped:
            return stripped[-200:]
    return ""


def _parse_discovered_nets(output: str) -> list[str]:
    """Parse FDAGENTS_NETS lines emitted by the discovery Tcl."""
    nets: list[str] = []
    for line in str(output).splitlines():
        stripped = line.strip()
        if not stripped.startswith("FDAGENTS_NETS"):
            continue
        _, _, payload = stripped.partition(" ")
        for item in payload.split("|"):
            net = item.strip()
            if net and net not in nets:
                nets.append(net)
    return nets


_TIMING_NET_RE = re.compile(
    r"net\s+\(fo=(?P<fanout>\d+),[^)]*\)\s+"
    r"(?P<delay>-?\d+(?:\.\d+)?)\s+"
    r"-?\d+(?:\.\d+)?\s+"
    r"(?P<name>\S+)\s*$"
)


def _parse_ranked_route_nets(
    timing_report: str,
    *,
    max_nets: int,
    max_fanout: int,
    min_delay_ns: float = 0.0,
) -> list[dict[str, Any]]:
    """Extract route-delay-ranked nets from a textual Vivado timing report."""
    ranked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in str(timing_report).splitlines():
        match = _TIMING_NET_RE.search(line)
        if not match:
            continue
        name = match.group("name")
        if name in seen:
            continue
        fanout = int(match.group("fanout"))
        delay = float(match.group("delay"))
        if fanout > max_fanout or delay < min_delay_ns:
            continue
        if re.search(
            r"(^|/)(clk|clock)(/|$)|bufg|mmcm|pll|wclk|rclk|wrclk|rdclk|txoutclk|rxoutclk|userclk|coreclk",
            name,
            re.IGNORECASE,
        ):
            continue
        seen.add(name)
        ranked.append({"name": name, "fanout": fanout, "delay": delay})

    ranked.sort(key=lambda item: float(item["delay"]), reverse=True)
    return ranked[: max(1, int(max_nets))]


def _build_discovery_tcl(pin_window: list[str], max_fanout: int) -> str:
    pins = " ".join(_tcl_quote(pin) for pin in pin_window)
    loop_body = "; ".join(
        [
            "set pin [get_pins -quiet $pin_name]",
            "if {[llength $pin] == 0} { continue }",
            "set net [get_nets -quiet -of_objects $pin]",
            "if {[llength $net] == 0} { continue }",
            "set net [lindex $net 0]",
            "set net_name [get_property NAME $net]",
            (
                "if {[regexp -nocase "
                "{(^|/)(clk|clock)(/|$)|bufg|mmcm|pll|wclk|rclk|wrclk|rdclk|"
                "txoutclk|rxoutclk|userclk|coreclk} $net_name]} { continue }"
            ),
            "set sinks [filter [get_pins -quiet -of_objects $net] {DIRECTION == IN}]",
            f"if {{[llength $sinks] > {int(max_fanout)}}} {{ continue }}",
            (
                "if {[lsearch -exact $fdagents_net_names $net_name] < 0} "
                "{ lappend fdagents_net_names $net_name }"
            ),
        ]
    )
    return "; ".join(
        [
            "puts {FDAGENTS_DISCOVER_CRITICAL_NET_REROUTE}",
            f"set fdagents_pin_names [list {pins}]",
            "set fdagents_net_names {}",
            f"foreach pin_name $fdagents_pin_names {{ {loop_body} }}",
            "puts \"FDAGENTS_NETS [join $fdagents_net_names {|}]\"",
        ]
    )


def _build_worst_source_discovery_tcl(
    max_fanout: int,
    contest_clock: str = "clk_fpl26contest",
) -> str:
    """Resolve the single output net of the current worst setup source cell."""
    clock_literal = _tcl_quote(str(contest_clock or "clk_fpl26contest"))
    return "; ".join(
        [
            "puts {FDAGENTS_DISCOVER_WORST_SOURCE_REROUTE}",
            "set fdagents_all_clocks [get_clocks -quiet]",
            "set fdagents_all_clock_names [get_property NAME $fdagents_all_clocks]",
            (
                "set fdagents_clock_indices [lsearch -all -exact "
                f"$fdagents_all_clock_names {clock_literal}]"
            ),
            (
                "if {[llength $fdagents_clock_indices] != 1} { "
                "error {FDAGENTS worst-source contest clock is missing or ambiguous} }"
            ),
            (
                "set fdagents_clock [lindex $fdagents_all_clocks "
                "[lindex $fdagents_clock_indices 0]]"
            ),
            "unset fdagents_all_clocks fdagents_all_clock_names fdagents_clock_indices",
            (
                "set fdagents_paths "
                "[get_timing_paths -quiet -delay_type max -max_paths 1 "
                "-nworst 1 -sort_by slack -group $fdagents_clock]"
            ),
            "if {[llength $fdagents_paths] != 1} { error {FDAGENTS expected one worst setup path} }",
            "set fdagents_start_name [get_property STARTPOINT_PIN [lindex $fdagents_paths 0]]",
            "set fdagents_all_pins [get_pins -quiet -hierarchical]",
            "set fdagents_all_pin_names [get_property NAME $fdagents_all_pins]",
            (
                "set fdagents_pin_indices "
                "[lsearch -all -exact $fdagents_all_pin_names $fdagents_start_name]"
            ),
            "if {[llength $fdagents_pin_indices] != 1} { error {FDAGENTS worst startpoint pin is ambiguous} }",
            (
                "set fdagents_start_pin [lindex $fdagents_all_pins "
                "[lindex $fdagents_pin_indices 0]]"
            ),
            "unset fdagents_all_pins fdagents_all_pin_names fdagents_pin_indices",
            "set fdagents_source_cell [get_cells -quiet -of_objects $fdagents_start_pin]",
            "if {[llength $fdagents_source_cell] != 1} { error {FDAGENTS worst source cell is ambiguous} }",
            (
                "set fdagents_output_pins [get_pins -quiet -of_objects "
                "$fdagents_source_cell -filter {DIRECTION == OUT}]"
            ),
            "set fdagents_source_nets [get_nets -quiet -of_objects $fdagents_output_pins]",
            "if {[llength $fdagents_source_nets] != 1} { error {FDAGENTS worst source net is ambiguous} }",
            "set fdagents_source_net [lindex $fdagents_source_nets 0]",
            "set fdagents_is_clock 0",
            "catch {set fdagents_is_clock [get_property IS_CLOCK $fdagents_source_net]}",
            (
                "if {$fdagents_is_clock eq {1} || $fdagents_is_clock eq {true} "
                "|| [llength [get_clocks -quiet -of_objects $fdagents_source_net]] > 0} "
                "{ error {FDAGENTS worst source net is a clock} }"
            ),
            "set fdagents_route_fixed 0",
            "catch {set fdagents_route_fixed [get_property IS_ROUTE_FIXED $fdagents_source_net]}",
            "set fdagents_dont_touch 0",
            "catch {set fdagents_dont_touch [get_property DONT_TOUCH $fdagents_source_net]}",
            (
                "if {$fdagents_route_fixed eq {1} || $fdagents_route_fixed eq {true} "
                "|| $fdagents_dont_touch eq {1} || $fdagents_dont_touch eq {true}} "
                "{ error {FDAGENTS worst source net is immutable} }"
            ),
            (
                "set fdagents_sinks [get_pins -quiet -leaf -of_objects "
                "$fdagents_source_net -filter {DIRECTION == IN}]"
            ),
            # Report the measured sink count, because a net over the window by
            # two and one over it by hundreds call for different decisions.
            "set fdagents_sinks_count [llength $fdagents_sinks]",
            (
                f"if {{[llength $fdagents_sinks] > {int(max_fanout)}}} "
                # Brace quoting would emit the variable name rather than its
                # value, so a message that carries a measurement is quoted.
                f'{{ error "FDAGENTS worst source net exceeds max_fanout: '
                f'sinks=$fdagents_sinks_count limit={int(max_fanout)}" }}'
            ),
            "puts \"FDAGENTS_NETS [get_property NAME $fdagents_source_net]\"",
        ]
    )


def _build_boundary_link_discovery_tcl(
    max_fanout: int,
    num_paths: int = 5,
    contest_clock: str = "clk_fpl26contest",
    required_direction: Optional[str] = None,
    required_hard_kind: Optional[str] = None,
    selected_ordinal: Optional[int] = None,
) -> str:
    """Resolve one unambiguous live arithmetic-to-LUT boundary net.

    Discovery is deliberately read-only and runs only after this skill action
    has been selected. By default, a timing path is eligible only when exactly
    one live, mutable data net joins an exact DSP48E2/CARRY8 physical owner and
    an exact LUT1..LUT6 primitive on that same contest-clock path. A bounded
    experiment may explicitly select a one-based live ordinal without changing
    the default uniqueness contract.
    """
    max_fanout = max(1, int(max_fanout))
    num_paths = max(1, int(num_paths))
    if required_direction not in {None, "lut_to_hard", "hard_to_lut"}:
        raise ValueError("unsupported arithmetic boundary direction")
    if required_hard_kind not in {None, "dsp48e2", "carry8"}:
        raise ValueError("unsupported arithmetic boundary hard kind")
    if selected_ordinal is not None and (
        isinstance(selected_ordinal, bool)
        or not isinstance(selected_ordinal, int)
        or selected_ordinal < 1
        or selected_ordinal > 8
    ):
        raise ValueError("unsupported arithmetic boundary ordinal")
    clock_literal = _tcl_quote(str(contest_clock or "clk_fpl26contest"))
    direction_literal = _tcl_quote(required_direction or "")
    hard_kind_literal = _tcl_quote(required_hard_kind or "")
    ordinal_value = int(selected_ordinal or 0)
    return "; ".join(
        [
            "puts {FDAGENTS_DISCOVER_ARITHMETIC_BOUNDARY_LINK}",
            "set fdagents_all_clocks [get_clocks -quiet]",
            "set fdagents_all_clock_names [get_property NAME $fdagents_all_clocks]",
            (
                "set fdagents_clock_indices [lsearch -all -exact "
                f"$fdagents_all_clock_names {clock_literal}]"
            ),
            (
                "if {[llength $fdagents_clock_indices] != 1} { "
                "error {FDAGENTS arithmetic boundary contest clock is missing or ambiguous} }"
            ),
            (
                "set fdagents_clock [lindex $fdagents_all_clocks "
                "[lindex $fdagents_clock_indices 0]]"
            ),
            "unset fdagents_all_clocks fdagents_all_clock_names fdagents_clock_indices",
            (
                "set fdagents_paths [get_timing_paths -quiet -delay_type max "
                f"-max_paths {num_paths} -nworst 1 -sort_by slack -group $fdagents_clock]"
            ),
            (
                "if {[llength $fdagents_paths] == 0} { "
                "error {FDAGENTS arithmetic boundary has no contest-clock setup paths} }"
            ),
            "set fdagents_selected_net {}",
            "set fdagents_selected_path_index -1",
            "set fdagents_selected_hard_ref {}",
            "set fdagents_selected_lut_ref {}",
            "set fdagents_selected_direction {}",
            "set fdagents_selected_hard_kind {}",
            "set fdagents_selected_fanout 0",
            "set fdagents_selected_matched_net_count 0",
            "set fdagents_selected_ordinal 0",
            f"set fdagents_required_direction {direction_literal}",
            f"set fdagents_required_hard_kind {hard_kind_literal}",
            f"set fdagents_requested_ordinal {ordinal_value}",
            "set fdagents_path_index -1",
            (
                "foreach fdagents_path $fdagents_paths { "
                "incr fdagents_path_index; "
                "set fdagents_path_cells {}; "
                "foreach fdagents_cell [get_cells -quiet -of_objects $fdagents_path] { "
                "if {[llength $fdagents_cell] == 1 "
                "&& [lsearch -exact $fdagents_path_cells $fdagents_cell] < 0} { "
                "lappend fdagents_path_cells $fdagents_cell } }; "
                "set fdagents_path_nets {}; "
                "foreach fdagents_pin [get_pins -quiet -of_objects $fdagents_path] { "
                "foreach fdagents_net [get_nets -quiet -of_objects $fdagents_pin] { "
                "if {[llength $fdagents_net] == 1 "
                "&& [lsearch -exact $fdagents_path_nets $fdagents_net] < 0} { "
                "lappend fdagents_path_nets $fdagents_net } } }; "
                "set fdagents_path_boundary_nets {}; "
                "array unset fdagents_path_hard_ref; "
                "array unset fdagents_path_lut_ref; "
                "array unset fdagents_path_direction; "
                "array unset fdagents_path_hard_kind; "
                "foreach fdagents_net $fdagents_path_nets { "
                "set fdagents_is_clock 0; "
                "catch {set fdagents_is_clock [get_property IS_CLOCK $fdagents_net]}; "
                "if {$fdagents_is_clock eq {1} || $fdagents_is_clock eq {true} "
                "|| [llength [get_clocks -quiet -of_objects $fdagents_net]] > 0} { continue }; "
                "set fdagents_route_fixed 0; "
                "catch {set fdagents_route_fixed [get_property IS_ROUTE_FIXED $fdagents_net]}; "
                "set fdagents_dont_touch 0; "
                "catch {set fdagents_dont_touch [get_property DONT_TOUCH $fdagents_net]}; "
                "if {$fdagents_route_fixed eq {1} || $fdagents_route_fixed eq {true} "
                "|| $fdagents_dont_touch eq {1} || $fdagents_dont_touch eq {true}} { continue }; "
                "set fdagents_drivers [get_pins -quiet -leaf -of_objects $fdagents_net "
                "-filter {DIRECTION == OUT}]; "
                "set fdagents_sinks [get_pins -quiet -leaf -of_objects $fdagents_net "
                "-filter {DIRECTION == IN}]; "
                f"if {{[llength $fdagents_drivers] != 1 || [llength $fdagents_sinks] == 0 "
                f"|| [llength $fdagents_sinks] > {max_fanout}}} {{ continue }}; "
                "set fdagents_driver_cell [get_cells -quiet -of_objects "
                "[lindex $fdagents_drivers 0]]; "
                "if {[llength $fdagents_driver_cell] != 1 "
                "|| [lsearch -exact $fdagents_path_cells $fdagents_driver_cell] < 0} { continue }; "
                "set fdagents_driver_ref [get_property REF_NAME $fdagents_driver_cell]; "
                "set fdagents_driver_loc {}; "
                "catch {set fdagents_driver_loc [get_property LOC $fdagents_driver_cell]}; "
                "set fdagents_driver_dsp [expr {$fdagents_driver_ref eq {DSP48E2} "
                "|| [regexp {^DSP48E2_} $fdagents_driver_loc]}]; "
                "set fdagents_driver_carry [expr {$fdagents_driver_ref eq {CARRY8}}]; "
                "set fdagents_driver_hard [expr {$fdagents_driver_dsp || $fdagents_driver_carry}]; "
                "set fdagents_driver_hard_kind [expr {$fdagents_driver_dsp ? {dsp48e2} : "
                "$fdagents_driver_carry ? {carry8} : {}}]; "
                "set fdagents_driver_lut [regexp {^LUT[1-6]$} $fdagents_driver_ref]; "
                "set fdagents_driver_primitive 0; "
                "catch {set fdagents_driver_primitive "
                "[get_property IS_PRIMITIVE $fdagents_driver_cell]}; "
                "if {$fdagents_driver_lut && !$fdagents_driver_primitive} { continue }; "
                "set fdagents_touches_boundary 0; "
                "set fdagents_hard_ref {}; "
                "set fdagents_lut_ref {}; "
                "set fdagents_boundary_direction {}; "
                "set fdagents_hard_kind {}; "
                "foreach fdagents_sink $fdagents_sinks { "
                "set fdagents_sink_cell [get_cells -quiet -of_objects $fdagents_sink]; "
                "if {[llength $fdagents_sink_cell] != 1 "
                "|| [lsearch -exact $fdagents_path_cells $fdagents_sink_cell] < 0} { continue }; "
                "set fdagents_sink_ref [get_property REF_NAME $fdagents_sink_cell]; "
                "set fdagents_sink_loc {}; "
                "catch {set fdagents_sink_loc [get_property LOC $fdagents_sink_cell]}; "
                "set fdagents_sink_dsp [expr {$fdagents_sink_ref eq {DSP48E2} "
                "|| [regexp {^DSP48E2_} $fdagents_sink_loc]}]; "
                "set fdagents_sink_carry [expr {$fdagents_sink_ref eq {CARRY8}}]; "
                "set fdagents_sink_hard [expr {$fdagents_sink_dsp || $fdagents_sink_carry}]; "
                "set fdagents_sink_hard_kind [expr {$fdagents_sink_dsp ? {dsp48e2} : "
                "$fdagents_sink_carry ? {carry8} : {}}]; "
                "set fdagents_sink_lut [regexp {^LUT[1-6]$} $fdagents_sink_ref]; "
                "set fdagents_sink_primitive 0; "
                "catch {set fdagents_sink_primitive "
                "[get_property IS_PRIMITIVE $fdagents_sink_cell]}; "
                "if {$fdagents_sink_lut && !$fdagents_sink_primitive} { continue }; "
                "if {$fdagents_driver_hard && $fdagents_sink_lut} { "
                "set fdagents_touches_boundary 1; "
                "set fdagents_hard_ref $fdagents_driver_ref; "
                "set fdagents_lut_ref $fdagents_sink_ref; "
                "set fdagents_hard_kind $fdagents_driver_hard_kind; "
                "set fdagents_boundary_direction {hard_to_lut}; break }; "
                "if {$fdagents_driver_lut && $fdagents_sink_hard} { "
                "set fdagents_touches_boundary 1; "
                "set fdagents_hard_ref $fdagents_sink_ref; "
                "set fdagents_lut_ref $fdagents_driver_ref; "
                "set fdagents_hard_kind $fdagents_sink_hard_kind; "
                "set fdagents_boundary_direction {lut_to_hard}; break } }; "
                "if {!$fdagents_touches_boundary} { continue }; "
                "if {$fdagents_required_direction ne {} "
                "&& $fdagents_boundary_direction ne $fdagents_required_direction} { continue }; "
                "if {$fdagents_required_hard_kind ne {} "
                "&& $fdagents_hard_kind ne $fdagents_required_hard_kind} { continue }; "
                "if {[lsearch -exact $fdagents_path_boundary_nets $fdagents_net] < 0} { "
                "lappend fdagents_path_boundary_nets $fdagents_net; "
                "set fdagents_key [llength $fdagents_path_boundary_nets]; "
                "set fdagents_path_hard_ref($fdagents_key) $fdagents_hard_ref; "
                "set fdagents_path_lut_ref($fdagents_key) $fdagents_lut_ref; "
                "set fdagents_path_direction($fdagents_key) $fdagents_boundary_direction; "
                "set fdagents_path_hard_kind($fdagents_key) $fdagents_hard_kind } }; "
                "set fdagents_matched_net_count [llength $fdagents_path_boundary_nets]; "
                "if {$fdagents_requested_ordinal == 0} { "
                "if {$fdagents_matched_net_count != 1} { continue }; "
                "set fdagents_key 1 "
                "} else { "
                "if {$fdagents_matched_net_count < $fdagents_requested_ordinal} { continue }; "
                "set fdagents_key $fdagents_requested_ordinal }; "
                "set fdagents_selected_net "
                "[lindex $fdagents_path_boundary_nets [expr {$fdagents_key - 1}]]; "
                "set fdagents_selected_path_index $fdagents_path_index; "
                "set fdagents_selected_hard_ref $fdagents_path_hard_ref($fdagents_key); "
                "set fdagents_selected_lut_ref $fdagents_path_lut_ref($fdagents_key); "
                "set fdagents_selected_direction $fdagents_path_direction($fdagents_key); "
                "set fdagents_selected_hard_kind $fdagents_path_hard_kind($fdagents_key); "
                "set fdagents_selected_matched_net_count $fdagents_matched_net_count; "
                "set fdagents_selected_ordinal $fdagents_key; "
                "set fdagents_selected_fanout [llength [get_pins -quiet -leaf "
                "-of_objects $fdagents_selected_net -filter {DIRECTION == IN}]]; "
                "break }"
            ),
            (
                "if {[llength $fdagents_selected_net] != 1} { "
                "error {FDAGENTS no path satisfies live arithmetic boundary selection} }"
            ),
            (
                "puts \"FDAGENTS_BOUNDARY_LINK path_index=$fdagents_selected_path_index "
                "hard_ref=$fdagents_selected_hard_ref lut_ref=$fdagents_selected_lut_ref "
                "direction=$fdagents_selected_direction "
                "hard_kind=$fdagents_selected_hard_kind "
                "fanout=$fdagents_selected_fanout "
                "matched_net_count=$fdagents_selected_matched_net_count "
                "selected_ordinal=$fdagents_selected_ordinal exact_net_count=1\""
            ),
            "puts \"FDAGENTS_NETS [get_property NAME $fdagents_selected_net]\"",
        ]
    )


def _parse_boundary_link_discovery(
    output: str,
    *,
    max_fanout: int,
) -> Optional[dict[str, Any]]:
    """Validate the exact-topology marker and its single live net projection."""
    markers = [
        line.strip()
        for line in str(output or "").splitlines()
        if line.strip().startswith("FDAGENTS_BOUNDARY_LINK ")
    ]
    nets = _parse_discovered_nets(output)
    if len(markers) != 1 or len(nets) != 1:
        return None
    fields: dict[str, str] = {}
    for item in markers[0].split()[1:]:
        key, separator, value = item.partition("=")
        if not separator or not key or not value or key in fields:
            return None
        fields[key] = value
    if set(fields) != {
        "path_index",
        "hard_ref",
        "lut_ref",
        "direction",
        "hard_kind",
        "fanout",
        "matched_net_count",
        "selected_ordinal",
        "exact_net_count",
    }:
        return None
    try:
        path_index = int(fields["path_index"])
        fanout = int(fields["fanout"])
        matched_net_count = int(fields["matched_net_count"])
        selected_ordinal = int(fields["selected_ordinal"])
        exact_net_count = int(fields["exact_net_count"])
    except ValueError:
        return None
    if (
        path_index < 0
        or fanout < 1
        or fanout > max(1, int(max_fanout))
        or exact_net_count != 1
        or matched_net_count < 1
        or selected_ordinal < 1
        or selected_ordinal > matched_net_count
        or re.fullmatch(r"LUT[1-6]", fields["lut_ref"]) is None
        or fields["direction"] not in {"lut_to_hard", "hard_to_lut"}
        or fields["hard_kind"] not in {"dsp48e2", "carry8"}
        or (
            fields["hard_kind"] == "carry8"
            and fields["hard_ref"] != "CARRY8"
        )
        or (
            fields["hard_kind"] == "dsp48e2"
            and fields["hard_ref"] != "DSP48E2"
            and re.fullmatch(r"DSP_[A-Z0-9_]+", fields["hard_ref"]) is None
        )
    ):
        return None
    return {
        "name": nets[0],
        "evidence": "live_arithmetic_boundary_link",
        "path_index": path_index,
        "hard_ref": fields["hard_ref"],
        "lut_ref": fields["lut_ref"],
        "direction": fields["direction"],
        "hard_kind": fields["hard_kind"],
        "fanout": fanout,
        "matched_net_count": matched_net_count,
        "selected_ordinal": selected_ordinal,
        "exact_net_count": exact_net_count,
    }


def _build_reroute_tcl(
    net_names: list[str],
    *,
    route_mode: str = "delay_group",
    post_phys_opt: Optional[str] = None,
    pressure_path_count: int = 0,
    pressure_value_ns: Optional[float] = None,
    source_xdc: Optional[str] = None,
) -> str:
    nets = " ".join(_tcl_quote(net) for net in net_names)
    route_mode = str(route_mode or "delay_group").strip().lower()
    pressure_path_count = max(0, int(pressure_path_count or 0))

    if route_mode == "isolated_delay":
        route_lines = [
            "route_design -unroute -nets $nets",
            (
                "if {[catch {route_design -nets $nets -delay} fdagents_err]} "
                "{ puts \"FDAGENTS_DELAY_ROUTE_FAILED $fdagents_err\"; "
                "route_design -nets $nets }"
            ),
        ]
    elif route_mode == "explore_group":
        route_lines = [
            "route_design -unroute -nets $nets",
            "route_design -directive Explore",
        ]
    elif route_mode == "auto_group":
        route_lines = [
            "route_design -unroute -nets $nets",
            (
                "if {[catch {route_design -nets $nets -auto_delay} fdagents_err]} "
                "{ puts \"FDAGENTS_AUTO_ROUTE_FAILED $fdagents_err\"; "
                "route_design -nets $nets }"
            ),
            "route_design -preserve -tns_cleanup",
        ]
    elif route_mode == "nt_group":
        route_lines = [
            "route_design -unroute -nets $nets",
            (
                "if {[catch {route_design -directive NoTimingRelaxation -nets $nets} fdagents_err]} "
                "{ puts \"FDAGENTS_NT_ROUTE_FAILED $fdagents_err\"; "
                "route_design -nets $nets }"
            ),
            "route_design -preserve -tns_cleanup",
        ]
    else:
        route_lines = [
            "route_design -unroute -nets $nets",
            (
                "if {[catch {route_design -nets $nets -delay} fdagents_err]} "
                "{ puts \"FDAGENTS_DELAY_ROUTE_FAILED $fdagents_err\"; "
                "route_design -nets $nets }"
            ),
            "route_design -preserve -tns_cleanup",
        ]

    if post_phys_opt:
        directive = str(post_phys_opt).strip()
        if _DIRECTIVE_RE.fullmatch(directive) is None:
            raise ValueError("post_phys_opt is not a safe Vivado directive")
        route_lines.append(f"phys_opt_design -directive {directive}")

    commands = [
        f"set fdagents_net_names [list {nets}]",
        "set nets {}",
        "set fdagents_all_nets [get_nets -quiet -hierarchical]",
        "set fdagents_all_net_names [get_property NAME $fdagents_all_nets]",
        (
            "foreach fdagents_net_name $fdagents_net_names { "
            "set fdagents_indices [lsearch -all -exact $fdagents_all_net_names $fdagents_net_name]; "
            "if {[llength $fdagents_indices] != 1} { "
            "error \"FDAGENTS expected one exact net: $fdagents_net_name\" }; "
            "lappend nets [lindex $fdagents_all_nets [lindex $fdagents_indices 0]] }"
        ),
        "unset fdagents_all_nets fdagents_all_net_names",
    ]
    if pressure_path_count and pressure_value_ns is not None:
        commands.extend(
            [
                "set fdagents_pressure_pairs {}",
                (
                    "foreach path "
                    f"[get_timing_paths -quiet -setup -max_paths {pressure_path_count} -nworst 1] "
                    "{ set sp [get_property STARTPOINT_PIN $path]; "
                    "set ep [get_property ENDPOINT_PIN $path]; "
                    "if {$sp ne \"\" && $ep ne \"\"} { "
                    f"if {{![catch {{set_max_delay {float(pressure_value_ns):.3f} -datapath_only -from $sp -to $ep}}]}} "
                    "{ lappend fdagents_pressure_pairs [list $sp $ep] } "
                    "} }"
                ),
            ]
        )
    commands.extend(route_lines)
    if pressure_path_count and pressure_value_ns is not None:
        commands.append("reset_timing -quiet")
        if source_xdc:
            commands.append(f"read_xdc {_tcl_quote(str(source_xdc))}")
        commands.append("update_timing")
    return "; ".join(commands)


def _candidate_output_path(run_dir: Path, idx: int, net_names: list[str]) -> Path:
    digest = hashlib.sha1("\n".join(net_names).encode("utf-8")).hexdigest()[:12]
    return run_dir / f"critical_net_reroute_{idx:02d}_{digest}.dcp"


class CriticalNetRerouteSkill:
    """Sweep small critical net windows with route-preserving Vivado ECO."""

    name = "critical_net_reroute"

    async def execute(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        clock_period: Optional[float],
    ) -> SkillResult:
        run_dir = mcp.run_dir
        input_dcp = params.get("input_dcp") or str(run_dir / "current_best.dcp")
        output_dcp = run_dir / "critical_net_reroute_best.dcp"
        num_paths = int(params.get("num_paths", 5))
        max_pins = int(params.get("max_pins", 12))
        window_size = int(params.get("window_size", 4))
        max_candidates = int(params.get("max_candidates", 6))
        framework_candidate_cap = int(
            params.get("_candidate_output_cap", max_candidates)
        )
        if framework_candidate_cap < 1:
            raise ValueError("_candidate_output_cap must be at least 1")
        max_candidates = min(max_candidates, framework_candidate_cap)
        max_fanout = int(params.get("max_fanout", 80))
        # worst_source reroutes exactly one net under max_candidates=1, the
        # same shape as the boundary target, and this skill already declares
        # boundary_max_fanout as an affordable single-net budget.  fir_systolic
        # refuses its own wall-owning net at fanout 96 against 80 while the
        # boundary bound already stands at 96, so honour the larger of the
        # skill's own declared bounds rather than a narrower one.
        if str(params.get("selection_mode", "")).strip().lower() == "worst_source":
            max_fanout = max(max_fanout, int(params.get("boundary_max_fanout", 0)))
        max_nets = int(params.get("max_nets", max_pins))
        selection_mode = str(params.get("selection_mode", "window")).strip().lower()
        contest_clock = str(
            params.get("_contest_clock")
            or getattr(mcp, "target_clock", "")
            or "clk_fpl26contest"
        ).strip()
        route_mode = str(params.get("route_mode", "delay_group"))
        pressure_path_count = int(params.get("pressure_path_count", 0))
        pressure_value_ns = params.get("pressure_value_ns")
        source_xdc = None
        attempts: list[dict] = []
        feedback_attempts: list[dict] = []
        candidate_feedback: list[dict] = []
        candidate_outputs: list[SkillOutput] = []
        budget_stopped = False
        unstarted: list[str] = []
        boundary_source_sha256 = ""
        boundary_direction = params.get("_boundary_direction")
        if boundary_direction not in {None, "lut_to_hard", "hard_to_lut"}:
            raise ValueError("unsupported arithmetic boundary direction")
        boundary_hard_kind = params.get("_boundary_hard_kind")
        if boundary_hard_kind not in {None, "dsp48e2", "carry8"}:
            raise ValueError("unsupported arithmetic boundary hard kind")
        boundary_ordinal = params.get("_boundary_ordinal")
        if boundary_ordinal is not None and (
            isinstance(boundary_ordinal, bool)
            or not isinstance(boundary_ordinal, int)
            or boundary_ordinal < 1
            or boundary_ordinal > 8
        ):
            raise ValueError("unsupported arithmetic boundary ordinal")

        logger.info(
            "[critical_net_reroute] target=%r paths=%d max_candidates=%d",
            target,
            num_paths,
            max_candidates,
        )

        try:
            if selection_mode == "boundary_link" and target != "boundary_link":
                raise ValueError(
                    "boundary_link selection requires target='boundary_link'"
                )
            if selection_mode == "boundary_link":
                raw_input_dcp = Path(input_dcp).expanduser()
                if raw_input_dcp.is_symlink() or not raw_input_dcp.resolve().is_file():
                    raise ValueError(
                        "boundary-link source DCP is unavailable or symlinked"
                    )
                input_dcp = str(raw_input_dcp.resolve())
                boundary_source_sha256 = sha256_file(Path(input_dcp))
                if (
                    params.get("_source_artifact_sha256")
                    != boundary_source_sha256
                ):
                    raise ValueError(
                        "boundary-link source DCP does not match its graph parent"
                    )
            await mcp.call_vivado(
                "open_checkpoint",
                {"dcp_path": str(input_dcp)},
                timeout=600.0,
            )
            if (
                selection_mode == "boundary_link"
                and sha256_file(Path(input_dcp)) != boundary_source_sha256
            ):
                raise RuntimeError("boundary-link source DCP changed while opening")
            if pressure_path_count and pressure_value_ns is not None:
                source_xdc = str((run_dir / "critical_net_reroute_source.xdc").resolve())
                await mcp.call_vivado(
                    "run_tcl",
                    {
                        "command": f"write_xdc -force {_tcl_quote(source_xdc)}",
                        "timeout": 300,
                    },
                    timeout=360.0,
                )
            direct_net_candidates: list[dict[str, Any]] = []
            windows: list[list[str]] = []
            if selection_mode in {"worst_source", "source"}:
                discovery = await mcp.call_vivado(
                    "run_tcl",
                    {
                        "command": _build_worst_source_discovery_tcl(
                            max_fanout, contest_clock
                        )
                    },
                    timeout=300.0,
                )
                source_nets = _parse_discovered_nets(str(discovery))
                if len(source_nets) != 1:
                    refusal = _discovery_refusal(str(discovery))
                    return SkillResult.failure(
                        before_wns,
                        (
                            "worst-source discovery did not resolve exactly "
                            f"one net: {refusal}"
                            if refusal else
                            "worst-source discovery did not resolve exactly "
                            f"one net (resolved {len(source_nets)})"
                        ),
                        output_dcp,
                        details={
                            "attempts": feedback_attempts,
                            "candidates": candidate_feedback,
                            "budget_stopped": budget_stopped,
                            "unstarted": unstarted,
                            "discovery_refusal": refusal,
                            "discovered_net_count": len(source_nets),
                        },
                    )
                direct_net_candidates = [{"name": source_nets[0]}]
            elif selection_mode == "boundary_link":
                discovery = await mcp.call_vivado(
                    "run_tcl",
                    {
                        "command": _build_boundary_link_discovery_tcl(
                            max_fanout=max_fanout,
                            num_paths=num_paths,
                            contest_clock=contest_clock,
                            required_direction=boundary_direction,
                            required_hard_kind=boundary_hard_kind,
                            selected_ordinal=boundary_ordinal,
                        )
                    },
                    timeout=300.0,
                )
                boundary_candidate = _parse_boundary_link_discovery(
                    str(discovery),
                    max_fanout=max_fanout,
                )
                if boundary_candidate is None:
                    return SkillResult.failure(
                        before_wns,
                        "arithmetic-boundary discovery did not resolve exactly one net",
                        output_dcp,
                        details={
                            "attempts": feedback_attempts,
                            "candidates": candidate_feedback,
                            "budget_stopped": budget_stopped,
                            "unstarted": unstarted,
                        },
                    )
                direct_net_candidates = [boundary_candidate]
            elif selection_mode in {"high_delay", "top3"}:
                timing_report = await mcp.call_vivado(
                    "run_tcl",
                    {
                        "command": (
                            "report_timing -return_string -delay_type max "
                            f"-max_paths {max(1, num_paths)} -nworst 1 -sort_by slack"
                        )
                    },
                    timeout=600.0,
                )
                direct_net_candidates = _parse_ranked_route_nets(
                    str(timing_report),
                    max_nets=max_nets,
                    max_fanout=max_fanout,
                    min_delay_ns=float(params.get("min_route_delay_ns", 0.0)),
                )
                if not direct_net_candidates:
                    return SkillResult.failure(
                        before_wns,
                        f"no ranked route nets found for selection_mode={selection_mode}",
                        output_dcp,
                        details={
                            "attempts": feedback_attempts,
                            "candidates": candidate_feedback,
                            "budget_stopped": budget_stopped,
                            "unstarted": unstarted,
                        },
                    )
            else:
                pins_file = run_dir / "critical_net_reroute_pins.json"
                await mcp.call_vivado(
                    "extract_critical_path_pins",
                    {"num_paths": num_paths, "output_file": str(pins_file)},
                    timeout=600.0,
                )
                if not pins_file.exists():
                    return SkillResult.failure(
                        before_wns,
                        "extract_critical_path_pins produced no file",
                        output_dcp,
                        details={
                            "attempts": feedback_attempts,
                            "candidates": candidate_feedback,
                            "budget_stopped": budget_stopped,
                            "unstarted": unstarted,
                        },
                    )

                critical_paths = json.loads(pins_file.read_text())
                windows = _candidate_pin_windows(
                    critical_paths,
                    num_paths=num_paths,
                    max_pins=max_pins,
                    window_size=window_size,
                    max_candidates=max_candidates,
                )
                if not windows:
                    return SkillResult.failure(
                        before_wns,
                        "no critical data-pin windows found",
                        output_dcp,
                        details={
                            "attempts": feedback_attempts,
                            "candidates": candidate_feedback,
                            "budget_stopped": budget_stopped,
                            "unstarted": unstarted,
                        },
                    )

            best: Optional[dict] = None
            seen_net_sets: set[tuple[str, ...]] = set()

            candidate_specs: list[dict[str, Any]] = []
            if direct_net_candidates:
                candidate_specs.append(
                    {
                        "pins": [],
                        "nets": [str(item["name"]) for item in direct_net_candidates],
                        "ranked_nets": direct_net_candidates,
                    }
                )
            else:
                candidate_specs = [{"pins": window} for window in windows]

            for idx, candidate_spec in enumerate(candidate_specs, 1):
                base_label = (
                    f"critical_net_reroute window={idx:02d} mode={selection_mode}"
                )[:120]
                if candidate_budget_exhausted(
                    params, completed_attempts=len(feedback_attempts)
                ):
                    budget_stopped = True
                    unstarted = [
                        f"critical_net_reroute window={future:02d} mode={selection_mode}"[:120]
                        for future in range(idx, len(candidate_specs) + 1)
                    ]
                    break
                started = time.monotonic()
                pin_window = list(candidate_spec.get("pins") or [])
                await mcp.call_vivado(
                    "open_checkpoint",
                    {"dcp_path": str(input_dcp)},
                    timeout=600.0,
                )
                if selection_mode == "boundary_link":
                    if sha256_file(Path(input_dcp)) != boundary_source_sha256:
                        raise RuntimeError(
                            "boundary-link source DCP changed while reopening"
                        )
                    rediscovery = await mcp.call_vivado(
                        "run_tcl",
                        {
                            "command": _build_boundary_link_discovery_tcl(
                                max_fanout=max_fanout,
                                num_paths=num_paths,
                                contest_clock=contest_clock,
                                required_direction=boundary_direction,
                                required_hard_kind=boundary_hard_kind,
                                selected_ordinal=boundary_ordinal,
                            )
                        },
                        timeout=300.0,
                    )
                    live_boundary = _parse_boundary_link_discovery(
                        str(rediscovery), max_fanout=max_fanout
                    )
                    expected_boundary = candidate_spec["ranked_nets"][0]
                    if live_boundary != expected_boundary:
                        raise RuntimeError(
                            "boundary-link evidence changed while reopening source DCP"
                        )
                if candidate_spec.get("nets"):
                    net_names = list(candidate_spec["nets"])
                else:
                    discovered = await mcp.call_vivado(
                        "run_tcl",
                        {"command": _build_discovery_tcl(pin_window, max_fanout)},
                        timeout=300.0,
                    )
                    net_names = _parse_discovered_nets(discovered)
                net_key = tuple(net_names)
                attempt = {
                    "candidate": idx,
                    "pins": pin_window,
                    "nets": net_names,
                    "selection_mode": selection_mode,
                }
                if candidate_spec.get("ranked_nets"):
                    attempt["ranked_nets"] = candidate_spec["ranked_nets"]
                if not net_names or net_key in seen_net_sets:
                    attempt["skipped"] = "no_nets_or_duplicate"
                    attempts.append(attempt)
                    feedback_attempts.append(
                        {
                            "label": base_label,
                            "status": "skipped",
                            "local_metrics": {"net_count": len(net_names)},
                            "runtime_s": time.monotonic() - started,
                        }
                    )
                    continue
                seen_net_sets.add(net_key)

                candidate_dcp = _candidate_output_path(run_dir, idx, net_names)
                try:
                    await mcp.call_vivado(
                        "run_tcl",
                        {
                            "command": _build_reroute_tcl(
                                net_names,
                                route_mode=route_mode,
                                post_phys_opt=params.get("post_phys_opt"),
                                pressure_path_count=pressure_path_count,
                                pressure_value_ns=pressure_value_ns,
                                source_xdc=source_xdc,
                            )
                        },
                        timeout=float(params.get("route_timeout_s", 1800.0)),
                    )
                    route_report = await mcp.call_vivado(
                        "report_route_status", {}, timeout=120.0
                    )
                    route = parse_route_status_static(route_report)
                    is_legal = bool(route.get("routed_ok"))

                    if selection_mode == "boundary_link":
                        after_wns = await mcp.get_wns(
                            contest_clock, allow_fallback=False
                        )
                    else:
                        after_wns = await mcp.get_wns()
                    if after_wns is None:
                        if selection_mode == "boundary_link":
                            raise RuntimeError(
                                "contest-clock WNS unavailable after boundary-link reroute"
                            )
                        ts = await mcp.call_vivado(
                            "report_timing_summary", {}, timeout=300.0
                        )
                        parsed = parse_timing_summary_static(ts)
                        after_wns = parsed.get("wns") or before_wns

                    await mcp.call_vivado(
                        "write_checkpoint",
                        {"dcp_path": str(candidate_dcp.resolve()), "force": True},
                        timeout=600.0,
                    )
                    candidate_dcp_sha256 = sha256_file(candidate_dcp)
                    attempt.update(
                        {
                            "after_wns": after_wns,
                            "delta_wns": after_wns - before_wns,
                            "is_legal": is_legal,
                            "route": route,
                            "output_dcp": str(candidate_dcp),
                        }
                    )
                    net_identity = ",".join(net_names)
                    label = (
                        f"critical_net_reroute window={idx:02d} "
                        f"mode={selection_mode} nets={net_identity}"
                    )[:120]
                    feedback = {
                        "label": label,
                        "status": "legal" if is_legal else "illegal",
                        "local_metrics": {
                            "wns": after_wns,
                            "delta_wns": after_wns - before_wns,
                            "net_count": len(net_names),
                        },
                        "runtime_s": time.monotonic() - started,
                    }
                    feedback_attempts.append(feedback)
                    if is_legal:
                        output_evidence = None
                        if selection_mode == "boundary_link":
                            boundary = candidate_spec["ranked_nets"][0]
                            output_evidence = {
                                "schema_version": 1,
                                "selection_mode": "boundary_link",
                                "read_only_discovery": True,
                                "source_dcp_sha256": boundary_source_sha256,
                                "candidate_dcp_sha256": candidate_dcp_sha256,
                                "rediscovered_before_mutation": True,
                                "exact_current_net_count": 1,
                                "arithmetic_boundary": {
                                    "path_index": boundary["path_index"],
                                    "hard_ref": boundary["hard_ref"],
                                    "lut_ref": boundary["lut_ref"],
                                    "direction": boundary["direction"],
                                    "hard_kind": boundary["hard_kind"],
                                    "fanout": boundary["fanout"],
                                    "matched_net_count": boundary["matched_net_count"],
                                    "selected_ordinal": boundary["selected_ordinal"],
                                },
                            }
                        candidate_outputs.append(
                            SkillOutput(candidate_dcp, label, output_evidence)
                        )
                        candidate_feedback.append(feedback)
                    if is_legal and (best is None or after_wns > best["after_wns"]):
                        best = attempt
                except Exception as e:
                    attempt.update({"error": str(e), "is_legal": False})
                    feedback_attempts.append(
                        {
                            "label": base_label,
                            "status": "error",
                            "local_metrics": {"net_count": len(net_names)},
                            "runtime_s": time.monotonic() - started,
                        }
                    )
                attempts.append(attempt)

            if best is None:
                error = "no legal critical_net_reroute candidates"
                return SkillResult(
                    success=False,
                    before_wns=before_wns,
                    after_wns=before_wns,
                    delta_wns=0.0,
                    is_legal=False,
                    output_dcp=output_dcp,
                    summary=f"FAILED: {error}",
                    error_msg=error,
                    details={
                        "attempts": feedback_attempts,
                        "candidates": candidate_feedback,
                        "budget_stopped": budget_stopped,
                        "unstarted": unstarted,
                    },
                )

            output_dcp = Path(best["output_dcp"])
            after_wns = float(best["after_wns"])
            delta = after_wns - before_wns
            await mcp.call_vivado(
                "open_checkpoint",
                {"dcp_path": str(output_dcp.resolve())},
                timeout=600.0,
            )

            fmax_b = calculate_fmax(before_wns, clock_period)
            fmax_a = calculate_fmax(after_wns, clock_period)
            if fmax_b and fmax_a:
                summary = (
                    f"critical_net_reroute nets={len(best['nets'])} "
                    f"wns {before_wns:.3f}->{after_wns:.3f} "
                    f"fmax {fmax_b:.1f}->{fmax_a:.1f} MHz delta={delta:+.3f}"
                )
            else:
                summary = (
                    f"critical_net_reroute nets={len(best['nets'])} "
                    f"wns {before_wns:.3f}->{after_wns:.3f} delta={delta:+.3f}"
                )
            if budget_stopped:
                summary += f" budget_stop {len(feedback_attempts)}/{len(candidate_specs)}"

            return SkillResult(
                success=True,
                before_wns=before_wns,
                after_wns=after_wns,
                delta_wns=delta,
                is_legal=True,
                output_dcp=output_dcp,
                summary=summary,
                details={
                    "attempts": feedback_attempts,
                    "candidates": candidate_feedback,
                    "budget_stopped": budget_stopped,
                    "unstarted": unstarted,
                },
                candidates=tuple(candidate_outputs),
            )

        except Exception as e:
            logger.error("[critical_net_reroute] failed: %s", e)
            return SkillResult.failure(
                before_wns,
                str(e),
                output_dcp,
                details={
                    "attempts": feedback_attempts,
                    "candidates": candidate_feedback,
                    "budget_stopped": budget_stopped,
                    "unstarted": unstarted,
                },
            )
