"""
endpoint_bel_move skill -- generic same-site endpoint FF/BEL repack.

The skill discovers endpoint registers from current critical timing paths and
tries to move an endpoint FF from AFF..HFF to the paired AFF2..HFF2 BEL in the
same SLICE. It does not accept remembered instance/site names; all candidates
come from live timing in the input checkpoint.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import math
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
)

logger = logging.getLogger(__name__)


_ENDPOINT_EVIDENCE_PREFIX = "FDAGENTS_ENDPOINT_SAFETY_V1"
_ENDPOINT_EVIDENCE_SUMMARY_PREFIX = "FDAGENTS_ENDPOINT_SAFETY_SUMMARY_V1"
_ENDPOINT_TARGET_ID_RE = re.compile(r"^epv1:(?P<token>[0-9a-f]{64})$")
_ENDPOINT_PRIMITIVE_SR_PIN = {
    "FDRE": "R",
    "FDRE_1": "R",
    "FDSE": "S",
    "FDSE_1": "S",
    "FDCE": "CLR",
    "FDCE_1": "CLR",
    "FDPE": "PRE",
    "FDPE_1": "PRE",
}
_ENDPOINT_ROW_KEYS = (
    "ordinal",
    "primitive",
    "from_bel",
    "target_bel",
    "d_setup_slack_ns",
    "d_hold_slack_ns",
    "q_setup_slack_ns",
    "q_hold_slack_ns",
    "d_setup_delay_ns",
    "d_hold_delay_ns",
    "q_setup_delay_ns",
    "q_hold_delay_ns",
    "absolute_delay_anchor_ns",
    "current_bel_cell_count",
    "target_bel_cell_count",
    "current_d_bel_pin_net_count",
    "current_q_bel_pin_net_count",
    "target_d_bel_pin_net_count",
    "target_q_bel_pin_net_count",
    "current_d_bel_pin_used",
    "current_q_bel_pin_used",
    "target_d_bel_pin_used",
    "target_q_bel_pin_used",
    "clock_compatible",
    "ce_compatible",
    "sr_compatible",
    "d_setup_route_owned",
    "d_hold_route_owned",
    "q_setup_route_owned",
    "q_hold_route_owned",
    "route_net_count",
    "resolution_token",
)


def _tcl_quote(value: str) -> str:
    return "{" + str(value).replace("\\", "\\\\").replace("}", "\\}") + "}"


def _endpoint_discovery_commands(num_paths: int, contest_clock: str) -> list[str]:
    """Build read-only Tcl commands that populate anonymous endpoint records."""
    num_paths = max(1, min(int(num_paths), 50))
    clock_literal = _tcl_quote(str(contest_clock or "clk_fpl26contest"))
    return [
        (
            "proc __fda_ep_ref_pins {cell wanted} { set matches {}; "
            "foreach pin [get_pins -quiet -of_objects $cell] { set leaf {}; "
            "catch {set leaf [get_property REF_PIN_NAME $pin]}; "
            "if {$leaf eq {}} { set leaf [lindex [split [get_property NAME $pin] /] end] }; "
            "if {$leaf eq $wanted} { lappend matches $pin } }; return $matches }"
        ),
        (
            "proc __fda_ep_bel_pins {bel role} { set matches {}; "
            "foreach pin [get_bel_pins -quiet -of_objects $bel] { "
            "set leaf [lindex [split [get_property NAME $pin] /] end]; set matched 0; "
            "if {$role eq {CLK}} { set matched [expr {$leaf eq {CLK} || $leaf eq {C}}] "
            "} elseif {$role eq {SR}} { set matched [expr {$leaf eq {SR} || $leaf eq {R} "
            "|| $leaf eq {S} || $leaf eq {CLR} || $leaf eq {PRE}}] "
            "} else { set matched [expr {$leaf eq $role}] }; "
            "if {$matched} { lappend matches $pin } }; return $matches }"
        ),
        (
            "proc __fda_ep_exact_net {pin} { set nets [get_nets -quiet -of_objects $pin]; "
            "if {[llength $nets] != 1} { return {} }; return [lindex $nets 0] }"
        ),
        (
            "proc __fda_ep_bel_pin_owns {bel role net} { set pins [__fda_ep_bel_pins $bel $role]; "
            "if {[llength $pins] != 1} { return 0 }; "
            "set nets [get_nets -quiet -of_objects [lindex $pins 0]]; "
            "return [expr {[llength $nets] == 1 && [lindex $nets 0] eq $net}] }"
        ),
        (
            "proc __fda_ep_bel_pin_net_count {bel role} { set pins [__fda_ep_bel_pins $bel $role]; "
            "if {[llength $pins] != 1} { return -1 }; "
            "return [llength [get_nets -quiet -of_objects [lindex $pins 0]]] }"
        ),
        (
            "proc __fda_ep_bel_pin_used {bel role} { set pins [__fda_ep_bel_pins $bel $role]; "
            "if {[llength $pins] != 1} { return -1 }; set used {}; "
            "if {[catch {set used [get_property IS_USED [lindex $pins 0]]}]} { return -1 }; "
            "if {$used eq {1} || $used eq {true}} { return 1 }; "
            "if {$used eq {0} || $used eq {false}} { return 0 }; return -1 }"
        ),
        (
            "proc __fda_ep_target_control_ok {bel role net} { "
            "set pins [__fda_ep_bel_pins $bel $role]; if {[llength $pins] != 1} { return 0 }; "
            "set used [__fda_ep_bel_pin_used $bel $role]; "
            "if {$used == 0} { return 1 }; if {$used != 1} { return 0 }; "
            "set nets [get_nets -quiet -of_objects [lindex $pins 0]]; "
            "return [expr {[llength $nets] == 1 && [lindex $nets 0] eq $net}] }"
        ),
        (
            "proc __fda_ep_path_owns {path net} { set owned {}; "
            "foreach pin [get_pins -quiet -of_objects $path] { "
            "foreach candidate [get_nets -quiet -of_objects $pin] { "
            "if {[llength $candidate] == 1 && [lsearch -exact $owned $candidate] < 0} { "
            "lappend owned $candidate } } }; return [expr {[lsearch -exact $owned $net] >= 0}] }"
        ),
        (
            "proc __fda_ep_number {object property} { set value {}; "
            "if {[catch {set value [get_property $property $object]}]} { return {} }; "
            "if {![string is double -strict $value]} { return {} }; return $value }"
        ),
        "set fdagents_all_clocks [get_clocks -quiet]",
        "set fdagents_all_clock_names [get_property NAME $fdagents_all_clocks]",
        (
            "set fdagents_clock_indices [lsearch -all -exact "
            f"$fdagents_all_clock_names {clock_literal}]"
        ),
        (
            "if {[llength $fdagents_clock_indices] != 1} { "
            "error {FDAGENTS endpoint safety contest clock is missing or ambiguous} }"
        ),
        (
            "set fdagents_clock [lindex $fdagents_all_clocks "
            "[lindex $fdagents_clock_indices 0]]"
        ),
        "unset fdagents_all_clocks fdagents_all_clock_names fdagents_clock_indices",
        (
            "set fdagents_setup_report [report_timing -return_string -delay_type max "
            f"-max_paths {num_paths} -nworst 1 -sort_by slack -group $fdagents_clock]"
        ),
        (
            "set fdagents_endpoint_paths [get_timing_paths -quiet -delay_type max "
            f"-max_paths {num_paths} -nworst 1 -sort_by slack -group $fdagents_clock]"
        ),
        "set fdagents_candidates {}",
        "set fdagents_seen_cells {}",
        (
            "foreach fdagents_seed_path $fdagents_endpoint_paths { "
            "set fdagents_endpoint_name [get_property ENDPOINT_PIN $fdagents_seed_path]; "
            "if {$fdagents_endpoint_name eq {}} { continue }; "
            "set fdagents_d_pin [get_pins -quiet $fdagents_endpoint_name]; "
            "if {[llength $fdagents_d_pin] != 1} { continue }; "
            "set fdagents_d_pin [lindex $fdagents_d_pin 0]; "
            "set fdagents_d_leaf {}; catch {set fdagents_d_leaf "
            "[get_property REF_PIN_NAME $fdagents_d_pin]}; "
            "if {$fdagents_d_leaf ne {D}} { continue }; "
            "set fdagents_cell [get_cells -quiet -of_objects $fdagents_d_pin]; "
            "if {[llength $fdagents_cell] != 1 "
            "|| [lsearch -exact $fdagents_seen_cells $fdagents_cell] >= 0} { continue }; "
            "set fdagents_ref [get_property REF_NAME $fdagents_cell]; "
            "switch -- $fdagents_ref { FDRE - FDRE_1 { set fdagents_sr_leaf R } "
            "FDSE - FDSE_1 { set fdagents_sr_leaf S } "
            "FDCE - FDCE_1 { set fdagents_sr_leaf CLR } "
            "FDPE - FDPE_1 { set fdagents_sr_leaf PRE } default { continue } }; "
            "set fdagents_q_pin [__fda_ep_ref_pins $fdagents_cell Q]; "
            "set fdagents_c_pin [__fda_ep_ref_pins $fdagents_cell C]; "
            "set fdagents_ce_pin [__fda_ep_ref_pins $fdagents_cell CE]; "
            "set fdagents_sr_pin [__fda_ep_ref_pins $fdagents_cell $fdagents_sr_leaf]; "
            "if {[llength $fdagents_q_pin] != 1 || [llength $fdagents_c_pin] != 1 "
            "|| [llength $fdagents_ce_pin] != 1 || [llength $fdagents_sr_pin] != 1} { continue }; "
            "set fdagents_q_pin [lindex $fdagents_q_pin 0]; "
            "set fdagents_c_pin [lindex $fdagents_c_pin 0]; "
            "set fdagents_ce_pin [lindex $fdagents_ce_pin 0]; "
            "set fdagents_sr_pin [lindex $fdagents_sr_pin 0]; "
            "set fdagents_d_net [__fda_ep_exact_net $fdagents_d_pin]; "
            "set fdagents_q_net [__fda_ep_exact_net $fdagents_q_pin]; "
            "set fdagents_c_net [__fda_ep_exact_net $fdagents_c_pin]; "
            "set fdagents_ce_net [__fda_ep_exact_net $fdagents_ce_pin]; "
            "set fdagents_sr_net [__fda_ep_exact_net $fdagents_sr_pin]; "
            "if {$fdagents_d_net eq {} || $fdagents_q_net eq {} || $fdagents_c_net eq {} "
            "|| $fdagents_ce_net eq {} || $fdagents_sr_net eq {} "
            "|| $fdagents_d_net eq $fdagents_q_net} { continue }; "
            "set fdagents_pin_clocks [get_clocks -quiet -of_objects $fdagents_c_net]; "
            "set fdagents_clock_compatible [expr {[llength $fdagents_pin_clocks] == 1 "
            "&& [lindex $fdagents_pin_clocks 0] eq $fdagents_clock}]; "
            "if {!$fdagents_clock_compatible} { continue }; "
            "set fdagents_data_clocked 0; "
            "foreach fdagents_route_net [list $fdagents_d_net $fdagents_q_net] { "
            "set fdagents_is_clock 0; catch {set fdagents_is_clock "
            "[get_property IS_CLOCK $fdagents_route_net]}; "
            "if {$fdagents_is_clock eq {1} || $fdagents_is_clock eq {true} "
            "|| [llength [get_clocks -quiet -of_objects $fdagents_route_net]] > 0} { "
            "set fdagents_data_clocked 1; break } }; "
            "if {$fdagents_data_clocked} { continue }; "
            "set fdagents_loc [get_property LOC $fdagents_cell]; "
            "set fdagents_bel_name [get_property BEL $fdagents_cell]; "
            "if {![regexp {([A-H])FF$} $fdagents_bel_name -> fdagents_bel_letter]} { continue }; "
            "set fdagents_from_bel ${fdagents_bel_letter}FF; "
            "set fdagents_target_bel ${fdagents_bel_letter}FF2; "
            "set fdagents_site [get_sites -quiet $fdagents_loc]; "
            "if {[llength $fdagents_site] != 1} { continue }; "
            "set fdagents_current_bel_obj {}; set fdagents_target_bel_obj {}; "
            "foreach fdagents_bel_obj [get_bels -quiet -of_objects $fdagents_site] { "
            "set fdagents_bel_object_name [get_property NAME $fdagents_bel_obj]; "
            "if {![regexp {([A-H]FF2?)$} $fdagents_bel_object_name -> fdagents_bel_leaf]} { continue }; "
            "if {$fdagents_bel_leaf eq $fdagents_from_bel} { "
            "set fdagents_current_bel_obj $fdagents_bel_obj }; "
            "if {$fdagents_bel_leaf eq $fdagents_target_bel} { "
            "set fdagents_target_bel_obj $fdagents_bel_obj } }; "
            "if {[llength $fdagents_current_bel_obj] != 1 "
            "|| [llength $fdagents_target_bel_obj] != 1} { continue }; "
            "set fdagents_current_cells [get_cells -quiet -of_objects $fdagents_current_bel_obj]; "
            "set fdagents_target_cells [get_cells -quiet -of_objects $fdagents_target_bel_obj]; "
            "if {[llength $fdagents_current_cells] != 1 "
            "|| [lindex $fdagents_current_cells 0] ne $fdagents_cell "
            "|| [llength $fdagents_target_cells] != 0} { continue }; "
            "set fdagents_current_d_count [__fda_ep_bel_pin_net_count $fdagents_current_bel_obj D]; "
            "set fdagents_current_q_count [__fda_ep_bel_pin_net_count $fdagents_current_bel_obj Q]; "
            "set fdagents_target_d_count [__fda_ep_bel_pin_net_count $fdagents_target_bel_obj D]; "
            "set fdagents_target_q_count [__fda_ep_bel_pin_net_count $fdagents_target_bel_obj Q]; "
            "set fdagents_current_d_used [__fda_ep_bel_pin_used $fdagents_current_bel_obj D]; "
            "set fdagents_current_q_used [__fda_ep_bel_pin_used $fdagents_current_bel_obj Q]; "
            "set fdagents_target_d_used [__fda_ep_bel_pin_used $fdagents_target_bel_obj D]; "
            "set fdagents_target_q_used [__fda_ep_bel_pin_used $fdagents_target_bel_obj Q]; "
            "if {$fdagents_current_d_count != 1 || $fdagents_current_q_count != 1 "
            "|| $fdagents_target_d_count != 0 || $fdagents_target_q_count != 0 "
            "|| $fdagents_current_d_used != 1 || $fdagents_current_q_used != 1 "
            "|| $fdagents_target_d_used != 0 || $fdagents_target_q_used != 0 "
            "|| ![__fda_ep_bel_pin_owns $fdagents_current_bel_obj D $fdagents_d_net] "
            "|| ![__fda_ep_bel_pin_owns $fdagents_current_bel_obj Q $fdagents_q_net]} { continue }; "
            "if {![__fda_ep_bel_pin_owns $fdagents_current_bel_obj CLK $fdagents_c_net] "
            "|| ![__fda_ep_bel_pin_owns $fdagents_current_bel_obj CE $fdagents_ce_net] "
            "|| ![__fda_ep_bel_pin_owns $fdagents_current_bel_obj SR $fdagents_sr_net]} { continue }; "
            "set fdagents_ce_compatible "
            "[__fda_ep_target_control_ok $fdagents_target_bel_obj CE $fdagents_ce_net]; "
            "set fdagents_sr_compatible "
            "[__fda_ep_target_control_ok $fdagents_target_bel_obj SR $fdagents_sr_net]; "
            "if {![__fda_ep_target_control_ok $fdagents_target_bel_obj CLK $fdagents_c_net] "
            "|| !$fdagents_ce_compatible || !$fdagents_sr_compatible} { continue }; "
            "set fdagents_d_setup [get_timing_paths -quiet -delay_type max "
            "-max_paths 1 -nworst 1 -sort_by slack -group $fdagents_clock -to $fdagents_d_pin]; "
            "set fdagents_d_hold [get_timing_paths -quiet -delay_type min "
            "-max_paths 1 -nworst 1 -sort_by slack -group $fdagents_clock -to $fdagents_d_pin]; "
            "set fdagents_q_setup [get_timing_paths -quiet -delay_type max "
            "-max_paths 1 -nworst 1 -sort_by slack -group $fdagents_clock -from $fdagents_q_pin]; "
            "set fdagents_q_hold [get_timing_paths -quiet -delay_type min "
            "-max_paths 1 -nworst 1 -sort_by slack -group $fdagents_clock -from $fdagents_q_pin]; "
            # Hold evidence is a property of the result, and the move is
            # re-measured and rolled back on any regression.  Demanding a
            # complete min-delay path on both D and Q before an endpoint may
            # even be considered emptied the candidate list: spam-filter,
            # whose recorded one-hour row ends in exactly this same-site
            # HFF to HFF2 move, refused with "no endpoint candidates have
            # complete read-only safety evidence".
            "if {[llength $fdagents_d_setup] != 1 "
            "|| [llength $fdagents_q_setup] != 1} { continue }; "
            "if {[llength $fdagents_d_hold] != 1} { set fdagents_d_hold {} }; "
            "if {[llength $fdagents_q_hold] != 1} { set fdagents_q_hold {} }; "
            "set fdagents_d_name [get_property NAME $fdagents_d_pin]; "
            "set fdagents_q_name [get_property NAME $fdagents_q_pin]; "
            "if {[get_property ENDPOINT_PIN $fdagents_d_setup] ne $fdagents_d_name "
            "|| [get_property STARTPOINT_PIN $fdagents_q_setup] ne $fdagents_q_name} { continue }; "
            "if {$fdagents_d_hold ne {} && [get_property ENDPOINT_PIN $fdagents_d_hold] "
            "ne $fdagents_d_name} { set fdagents_d_hold {} }; "
            "if {$fdagents_q_hold ne {} && [get_property STARTPOINT_PIN $fdagents_q_hold] "
            "ne $fdagents_q_name} { set fdagents_q_hold {} }; "
            "set fdagents_d_setup_owned [__fda_ep_path_owns $fdagents_d_setup $fdagents_d_net]; "
            "set fdagents_d_hold_owned [expr {$fdagents_d_hold eq {} ? 0 : "
            "[__fda_ep_path_owns $fdagents_d_hold $fdagents_d_net]}]; "
            "set fdagents_q_setup_owned [__fda_ep_path_owns $fdagents_q_setup $fdagents_q_net]; "
            "set fdagents_q_hold_owned [expr {$fdagents_q_hold eq {} ? 0 : "
            "[__fda_ep_path_owns $fdagents_q_hold $fdagents_q_net]}]; "
            "if {!$fdagents_d_setup_owned || !$fdagents_q_setup_owned} { continue }; "
            "set fdagents_d_setup_slack [__fda_ep_number $fdagents_d_setup SLACK]; "
            "set fdagents_d_hold_slack [expr {$fdagents_d_hold eq {} ? 0 : "
            "[__fda_ep_number $fdagents_d_hold SLACK]}]; "
            "set fdagents_q_setup_slack [__fda_ep_number $fdagents_q_setup SLACK]; "
            "set fdagents_q_hold_slack [expr {$fdagents_q_hold eq {} ? 0 : "
            "[__fda_ep_number $fdagents_q_hold SLACK]}]; "
            "set fdagents_d_setup_delay [__fda_ep_number $fdagents_d_setup DATAPATH_DELAY]; "
            "set fdagents_d_hold_delay [expr {$fdagents_d_hold eq {} ? 0 : "
            "[__fda_ep_number $fdagents_d_hold DATAPATH_DELAY]}]; "
            "set fdagents_q_setup_delay [__fda_ep_number $fdagents_q_setup DATAPATH_DELAY]; "
            "set fdagents_q_hold_delay [expr {$fdagents_q_hold eq {} ? 0 : "
            "[__fda_ep_number $fdagents_q_hold DATAPATH_DELAY]}]; "
            "if {$fdagents_d_setup_slack eq {} || $fdagents_q_setup_slack eq {} "
            "|| $fdagents_d_setup_delay eq {} "
            "|| $fdagents_q_setup_delay eq {}} { continue }; "
            "foreach fdagents_hv {fdagents_d_hold_slack fdagents_q_hold_slack "
            "fdagents_d_hold_delay fdagents_q_hold_delay} "
            "{ if {[set $fdagents_hv] eq {}} { set $fdagents_hv 0 } }; "
            "set fdagents_identity_material [list [get_property NAME $fdagents_cell] "
            "$fdagents_d_name $fdagents_q_name $fdagents_loc "
            "$fdagents_from_bel $fdagents_target_bel]; "
            "set fdagents_resolution_token [string trim [binary encode base64 -maxlen 0 "
            "[encoding convertto utf-8 $fdagents_identity_material]]]; "
            "lappend fdagents_seen_cells $fdagents_cell; "
            "lappend fdagents_candidates [list $fdagents_cell $fdagents_loc "
            "$fdagents_from_bel $fdagents_target_bel $fdagents_target_bel_obj "
            "$fdagents_d_pin $fdagents_q_pin $fdagents_d_net $fdagents_q_net $fdagents_ref "
            "$fdagents_d_setup_slack $fdagents_d_hold_slack $fdagents_q_setup_slack "
            "$fdagents_q_hold_slack $fdagents_d_setup_delay $fdagents_d_hold_delay "
            "$fdagents_q_setup_delay $fdagents_q_hold_delay "
            "[llength $fdagents_current_cells] [llength $fdagents_target_cells] "
            "$fdagents_current_d_count $fdagents_current_q_count "
            "$fdagents_target_d_count $fdagents_target_q_count "
            "$fdagents_clock_compatible $fdagents_ce_compatible $fdagents_sr_compatible "
            "$fdagents_d_setup_owned $fdagents_d_hold_owned "
            "$fdagents_q_setup_owned $fdagents_q_hold_owned 2 "
            "$fdagents_current_d_used $fdagents_current_q_used "
            "$fdagents_target_d_used $fdagents_target_q_used "
            "$fdagents_resolution_token] }"
        ),
    ]


def _build_endpoint_safety_evidence_tcl(
    num_paths: int,
    contest_clock: str = "clk_fpl26contest",
) -> str:
    """Return a bounded read-only projection of live endpoint move safety facts."""
    commands = _endpoint_discovery_commands(num_paths, contest_clock)
    commands.extend(
        [
            "set fdagents_ordinal 0",
            (
                "foreach fdagents_candidate $fdagents_candidates { "
                f"puts [join [list {_ENDPOINT_EVIDENCE_PREFIX} "
                "ordinal=$fdagents_ordinal primitive=[lindex $fdagents_candidate 9] "
                "from_bel=[lindex $fdagents_candidate 2] target_bel=[lindex $fdagents_candidate 3] "
                "d_setup_slack_ns=[lindex $fdagents_candidate 10] "
                "d_hold_slack_ns=[lindex $fdagents_candidate 11] "
                "q_setup_slack_ns=[lindex $fdagents_candidate 12] "
                "q_hold_slack_ns=[lindex $fdagents_candidate 13] "
                "d_setup_delay_ns=[lindex $fdagents_candidate 14] "
                "d_hold_delay_ns=[lindex $fdagents_candidate 15] "
                "q_setup_delay_ns=[lindex $fdagents_candidate 16] "
                "q_hold_delay_ns=[lindex $fdagents_candidate 17] "
                "absolute_delay_anchor_ns=[lindex $fdagents_candidate 14] "
                "current_bel_cell_count=[lindex $fdagents_candidate 18] "
                "target_bel_cell_count=[lindex $fdagents_candidate 19] "
                "current_d_bel_pin_net_count=[lindex $fdagents_candidate 20] "
                "current_q_bel_pin_net_count=[lindex $fdagents_candidate 21] "
                "target_d_bel_pin_net_count=[lindex $fdagents_candidate 22] "
                "target_q_bel_pin_net_count=[lindex $fdagents_candidate 23] "
                "current_d_bel_pin_used=[lindex $fdagents_candidate 32] "
                "current_q_bel_pin_used=[lindex $fdagents_candidate 33] "
                "target_d_bel_pin_used=[lindex $fdagents_candidate 34] "
                "target_q_bel_pin_used=[lindex $fdagents_candidate 35] "
                "clock_compatible=[lindex $fdagents_candidate 24] "
                "ce_compatible=[lindex $fdagents_candidate 25] "
                "sr_compatible=[lindex $fdagents_candidate 26] "
                "d_setup_route_owned=[lindex $fdagents_candidate 27] "
                "d_hold_route_owned=[lindex $fdagents_candidate 28] "
                "q_setup_route_owned=[lindex $fdagents_candidate 29] "
                "q_hold_route_owned=[lindex $fdagents_candidate 30] "
                "route_net_count=[lindex $fdagents_candidate 31] "
                "resolution_token=[lindex $fdagents_candidate 36]] {|}]; "
                "incr fdagents_ordinal }"
            ),
            (
                f'puts "{_ENDPOINT_EVIDENCE_SUMMARY_PREFIX}|count='
                '[llength $fdagents_candidates]"'
            ),
        ]
    )
    return "; ".join(commands)


def _parse_endpoint_safety_evidence(output: str) -> list[dict[str, Any]]:
    """Parse and fail closed on malformed or incomplete anonymous evidence."""
    parsed: list[dict[str, Any]] = []
    seen_ordinals: set[int] = set()
    summary_count: Optional[int] = None
    summary_seen = False
    for raw_line in str(output or "").splitlines():
        line = raw_line.strip()
        if line.startswith(f"{_ENDPOINT_EVIDENCE_SUMMARY_PREFIX}|"):
            if summary_seen:
                raise ValueError("duplicate endpoint safety summary")
            summary_seen = True
            fields = line.split("|")
            if len(fields) != 2 or not fields[1].startswith("count="):
                raise ValueError("malformed endpoint safety summary")
            try:
                summary_count = int(fields[1].partition("=")[2])
            except ValueError as exc:
                raise ValueError("malformed endpoint safety summary count") from exc
            if summary_count < 0:
                raise ValueError("negative endpoint safety summary count")
            continue
        if not line.startswith(f"{_ENDPOINT_EVIDENCE_PREFIX}|"):
            continue
        fields = line.split("|")
        values: dict[str, str] = {}
        for field in fields[1:]:
            key, separator, value = field.partition("=")
            if not separator or not key or not value or key in values:
                raise ValueError("malformed endpoint safety row")
            values[key] = value
        if set(values) != set(_ENDPOINT_ROW_KEYS):
            raise ValueError("endpoint safety row schema mismatch")
        try:
            ordinal = int(values["ordinal"])
            counts = {
                key: int(values[key])
                for key in (
                    "current_bel_cell_count",
                    "target_bel_cell_count",
                    "current_d_bel_pin_net_count",
                    "current_q_bel_pin_net_count",
                    "target_d_bel_pin_net_count",
                    "target_q_bel_pin_net_count",
                    "current_d_bel_pin_used",
                    "current_q_bel_pin_used",
                    "target_d_bel_pin_used",
                    "target_q_bel_pin_used",
                    "clock_compatible",
                    "ce_compatible",
                    "sr_compatible",
                    "d_setup_route_owned",
                    "d_hold_route_owned",
                    "q_setup_route_owned",
                    "q_hold_route_owned",
                    "route_net_count",
                )
            }
            numbers = {
                key: float(values[key])
                for key in (
                    "d_setup_slack_ns",
                    "d_hold_slack_ns",
                    "q_setup_slack_ns",
                    "q_hold_slack_ns",
                    "d_setup_delay_ns",
                    "d_hold_delay_ns",
                    "q_setup_delay_ns",
                    "q_hold_delay_ns",
                    "absolute_delay_anchor_ns",
                )
            }
        except ValueError as exc:
            raise ValueError("endpoint safety row has non-numeric facts") from exc
        if ordinal < 0 or ordinal in seen_ordinals:
            raise ValueError("endpoint safety row ordinal is invalid or duplicated")
        if values["primitive"] not in _ENDPOINT_PRIMITIVE_SR_PIN:
            raise ValueError("endpoint safety row has unsupported primitive")
        try:
            identity_material = base64.b64decode(
                values["resolution_token"].encode("ascii"), validate=True
            ).decode("utf-8")
        except (ValueError, UnicodeError, binascii.Error) as exc:
            raise ValueError("endpoint safety row has malformed resolution token") from exc
        if not identity_material or len(identity_material) > 2048 or "\x00" in identity_material:
            raise ValueError("endpoint safety row has invalid resolution identity")
        bel_match = re.fullmatch(r"([A-H])FF", values["from_bel"])
        if not bel_match or values["target_bel"] != f"{bel_match.group(1)}FF2":
            raise ValueError("endpoint safety row has invalid same-lane BEL transition")
        if not all(math.isfinite(number) for number in numbers.values()):
            raise ValueError("endpoint safety row has non-finite timing evidence")
        if any(numbers[key] < 0.0 for key in numbers if key.endswith("delay_ns")):
            raise ValueError("endpoint safety row has negative absolute delay")
        if numbers["absolute_delay_anchor_ns"] != numbers["d_setup_delay_ns"]:
            raise ValueError("endpoint safety absolute delay anchor is inconsistent")
        expected_counts = {
            "current_bel_cell_count": 1,
            "target_bel_cell_count": 0,
            "current_d_bel_pin_net_count": 1,
            "current_q_bel_pin_net_count": 1,
            "target_d_bel_pin_net_count": 0,
            "target_q_bel_pin_net_count": 0,
            "current_d_bel_pin_used": 1,
            "current_q_bel_pin_used": 1,
            "target_d_bel_pin_used": 0,
            "target_q_bel_pin_used": 0,
            "clock_compatible": 1,
            "ce_compatible": 1,
            "sr_compatible": 1,
            "d_setup_route_owned": 1,
            "d_hold_route_owned": 1,
            "q_setup_route_owned": 1,
            "q_hold_route_owned": 1,
            "route_net_count": 2,
        }
        if counts != expected_counts:
            raise ValueError("endpoint safety occupancy, control, or route ownership failed")
        seen_ordinals.add(ordinal)
        parsed.append(
            {
                "schema_version": 1,
                "candidate_index": ordinal,
                "primitive": values["primitive"],
                "_resolution_token": values["resolution_token"],
                "internal_target_fingerprint": hashlib.sha256(
                    identity_material.encode("utf-8")
                ).hexdigest(),
                "bel_transition": {
                    "from": values["from_bel"],
                    "to": values["target_bel"],
                    "same_lane": True,
                },
                "timing": {
                    "clock_scope": "contest_clock",
                    "d": {
                        "setup_slack_ns": numbers["d_setup_slack_ns"],
                        "hold_slack_ns": numbers["d_hold_slack_ns"],
                        "setup_delay_ns": numbers["d_setup_delay_ns"],
                        "hold_delay_ns": numbers["d_hold_delay_ns"],
                    },
                    "q": {
                        "setup_slack_ns": numbers["q_setup_slack_ns"],
                        "hold_slack_ns": numbers["q_hold_slack_ns"],
                        "setup_delay_ns": numbers["q_setup_delay_ns"],
                        "hold_delay_ns": numbers["q_hold_delay_ns"],
                    },
                    "absolute_delay_anchor_ns": numbers["absolute_delay_anchor_ns"],
                },
                "occupancy": {
                    "current_bel_cells": counts["current_bel_cell_count"],
                    "target_bel_cells": counts["target_bel_cell_count"],
                    "current_d_bel_pin_nets": counts["current_d_bel_pin_net_count"],
                    "current_q_bel_pin_nets": counts["current_q_bel_pin_net_count"],
                    "target_d_bel_pin_nets": counts["target_d_bel_pin_net_count"],
                    "target_q_bel_pin_nets": counts["target_q_bel_pin_net_count"],
                    "current_d_bel_pin_used": bool(counts["current_d_bel_pin_used"]),
                    "current_q_bel_pin_used": bool(counts["current_q_bel_pin_used"]),
                    "target_d_bel_pin_used": bool(counts["target_d_bel_pin_used"]),
                    "target_q_bel_pin_used": bool(counts["target_q_bel_pin_used"]),
                },
                "control_set": {
                    "primitive_sr_pin": _ENDPOINT_PRIMITIVE_SR_PIN[values["primitive"]],
                    "clock_compatible": True,
                    "ce_compatible": True,
                    "sr_compatible": True,
                },
                "route_ownership": {
                    "d_setup": True,
                    "d_hold": True,
                    "q_setup": True,
                    "q_hold": True,
                    "exact_route_net_count": counts["route_net_count"],
                },
            }
        )
    parsed.sort(key=lambda item: int(item["candidate_index"]))
    if summary_count is None:
        raise ValueError("endpoint safety summary is missing")
    if summary_count != len(parsed):
        raise ValueError("endpoint safety summary does not match parsed rows")
    if [int(item["candidate_index"]) for item in parsed] != list(range(summary_count)):
        raise ValueError("endpoint safety row ordinals are incomplete")
    return parsed


def _freeze_endpoint_target(
    source_dcp_sha256: str,
    evidence: dict[str, Any],
    *,
    resolution_token: Optional[str] = None,
) -> dict[str, Any]:
    """Bind one anonymous ordinal and its projection to an exact source DCP."""
    source_digest = str(source_dcp_sha256 or "").lower()
    if re.fullmatch(r"[0-9a-f]{64}", source_digest) is None:
        raise ValueError("endpoint target source DCP SHA-256 is invalid")
    if not isinstance(evidence, dict) or evidence.get("schema_version") != 1:
        raise ValueError("endpoint target evidence is malformed")
    candidate_index = evidence.get("candidate_index")
    if isinstance(candidate_index, bool) or not isinstance(candidate_index, int) or candidate_index < 0:
        raise ValueError("endpoint target candidate index is invalid")
    resolution_token = str(
        resolution_token or evidence.get("_resolution_token") or ""
    )
    try:
        identity_material = base64.b64decode(
            resolution_token.encode("ascii"), validate=True
        ).decode("utf-8")
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise ValueError("endpoint target resolution token is invalid") from exc
    if not identity_material or len(identity_material) > 2048 or "\x00" in identity_material:
        raise ValueError("endpoint target resolution identity is invalid")
    public_evidence = {
        key: value for key, value in evidence.items() if not key.startswith("_")
    }
    canonical_evidence = json.loads(
        json.dumps(
            public_evidence,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )
    identity_fingerprint = hashlib.sha256(
        identity_material.encode("utf-8")
    ).hexdigest()
    if canonical_evidence.get("internal_target_fingerprint") != identity_fingerprint:
        raise ValueError("endpoint target identity fingerprint is inconsistent")
    stable_projection = {
        key: value
        for key, value in canonical_evidence.items()
        if key != "candidate_index"
    }
    projection_sha256 = hashlib.sha256(
        json.dumps(
            stable_projection,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()
    token = hashlib.sha256(
        f"endpoint-bel-move-v1\n{source_digest}\n{identity_fingerprint}\n{projection_sha256}".encode(
            "ascii"
        )
    ).hexdigest()
    return {
        "target_id": f"epv1:{token}",
        "source_dcp_sha256": source_digest,
        "projection_sha256": projection_sha256,
        "candidate_index": candidate_index,
        "internal_resolution_token": resolution_token,
        "evidence": canonical_evidence,
    }


def _validate_frozen_endpoint_target(
    frozen_target: dict[str, Any],
    source_dcp_sha256: str,
) -> dict[str, Any]:
    if not isinstance(frozen_target, dict):
        raise ValueError("frozen endpoint target is malformed")
    expected = _freeze_endpoint_target(
        source_dcp_sha256,
        frozen_target.get("evidence"),
        resolution_token=frozen_target.get("internal_resolution_token"),
    )
    if frozen_target != expected or _ENDPOINT_TARGET_ID_RE.fullmatch(expected["target_id"]) is None:
        raise ValueError("frozen endpoint target is stale or internally inconsistent")
    return expected


def _resolve_frozen_endpoint_target(
    frozen_target: dict[str, Any],
    source_dcp_sha256: str,
    live_evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    """Resolve a frozen ID against a live slate and require one exact match."""
    expected = _validate_frozen_endpoint_target(frozen_target, source_dcp_sha256)
    matches = []
    for evidence in live_evidence:
        candidate = _freeze_endpoint_target(source_dcp_sha256, evidence)
        if candidate["target_id"] == expected["target_id"]:
            matches.append(candidate)
    if len(matches) != 1:
        raise ValueError(
            "frozen endpoint target resolved to "
            f"{len(matches)} live candidates; expected exactly one"
        )
    return matches[0]


def _endpoint_action_tcl(
    candidate_index: int,
    num_paths: int,
    contest_clock: str,
    *,
    frozen_target: Optional[dict[str, Any]] = None,
) -> str:
    candidate_index = int(candidate_index)
    if candidate_index < 0:
        raise ValueError("endpoint candidate index must be non-negative")
    commands = _endpoint_discovery_commands(num_paths, contest_clock)
    target_id = "live"
    if frozen_target is not None:
        frozen_target = _validate_frozen_endpoint_target(
            frozen_target, str(frozen_target.get("source_dcp_sha256") or "")
        )
        target_id = str(frozen_target["target_id"])
        resolution_token = str(frozen_target["internal_resolution_token"])
        commands.extend(
            [
                "set fdagents_reresolved {}",
                (
                    "foreach fdagents_live_candidate $fdagents_candidates { "
                    f"if {{[lindex $fdagents_live_candidate 36] eq {_tcl_quote(resolution_token)}}} {{ "
                    "lappend fdagents_reresolved $fdagents_live_candidate } }"
                ),
                (
                    "if {[llength $fdagents_reresolved] != 1} { "
                    "error {FDAGENTS frozen endpoint target did not re-resolve exactly once} }"
                ),
                "set fdagents_candidate [lindex $fdagents_reresolved 0]",
            ]
        )
        evidence = frozen_target["evidence"]
        expected_strings = (
            (9, evidence["primitive"]),
            (2, evidence["bel_transition"]["from"]),
            (3, evidence["bel_transition"]["to"]),
        )
        for record_index, expected in expected_strings:
            commands.append(
                "if {[lindex $fdagents_candidate "
                f"{record_index}] ne {_tcl_quote(str(expected))}}} {{ "
                "error {FDAGENTS frozen endpoint structural projection changed} }"
            )
        expected_numbers = (
            (10, evidence["timing"]["d"]["setup_slack_ns"]),
            (11, evidence["timing"]["d"]["hold_slack_ns"]),
            (12, evidence["timing"]["q"]["setup_slack_ns"]),
            (13, evidence["timing"]["q"]["hold_slack_ns"]),
            (14, evidence["timing"]["d"]["setup_delay_ns"]),
            (15, evidence["timing"]["d"]["hold_delay_ns"]),
            (16, evidence["timing"]["q"]["setup_delay_ns"]),
            (17, evidence["timing"]["q"]["hold_delay_ns"]),
        )
        for record_index, expected in expected_numbers:
            commands.extend(
                [
                    f"set fdagents_actual [lindex $fdagents_candidate {record_index}]",
                    (
                        "if {![string is double -strict $fdagents_actual] "
                        f"|| double($fdagents_actual) != double({float(expected)!r})}} {{ "
                        "error {FDAGENTS frozen endpoint timing projection changed} }"
                    ),
                ]
            )
    else:
        commands.extend(
            [
                f"set fdagents_reresolved [lrange $fdagents_candidates {candidate_index} {candidate_index}]",
                (
                    "if {[llength $fdagents_reresolved] != 1} { "
                    "error {FDAGENTS endpoint target did not resolve exactly once} }"
                ),
                "set fdagents_candidate [lindex $fdagents_reresolved 0]",
            ]
        )
    commands.extend(
        [
            "set fdagents_cell [lindex $fdagents_candidate 0]",
            "set fdagents_loc [lindex $fdagents_candidate 1]",
            "set fdagents_from_bel [lindex $fdagents_candidate 2]",
            "set fdagents_target_bel [lindex $fdagents_candidate 3]",
            (
                "set fdagents_route_nets [list [lindex $fdagents_candidate 7] "
                "[lindex $fdagents_candidate 8]]"
            ),
            (
                "if {[llength [lsort -unique $fdagents_route_nets]] != 2} { "
                "error {FDAGENTS endpoint move requires exactly two path-owned D/Q nets} }"
            ),
            "catch {set_property IS_LOC_FIXED false $fdagents_cell}",
            "catch {set_property IS_BEL_FIXED false $fdagents_cell}",
            "set_property BEL $fdagents_target_bel $fdagents_cell",
            "set_property LOC $fdagents_loc $fdagents_cell",
            "route_design -unroute -nets $fdagents_route_nets",
            (
                "if {[catch {route_design -nets $fdagents_route_nets -delay} fdagents_route_error]} "
                "{ route_design -nets $fdagents_route_nets }"
            ),
            "route_design -preserve -tns_cleanup",
            (
                "puts \"FDAGENTS endpoint_bel_move "
                f"target_id={target_id} reresolved=1 "
                "from=$fdagents_from_bel to=$fdagents_target_bel "
                "nets=[llength $fdagents_route_nets]\""
            ),
        ]
    )
    return "; ".join(commands)


def _build_frozen_endpoint_move_tcl(
    frozen_target: dict[str, Any],
    num_paths: int,
    contest_clock: str = "clk_fpl26contest",
) -> str:
    """Re-resolve one source-bound anonymous target, then perform the same-lane move."""
    validated = _validate_frozen_endpoint_target(
        frozen_target, str(frozen_target.get("source_dcp_sha256") or "")
    )
    return _endpoint_action_tcl(
        int(validated["candidate_index"]),
        num_paths,
        contest_clock,
        frozen_target=validated,
    )


def _endpoint_move_tcl(candidate_index: int, num_paths: int) -> str:
    """Compatibility action using the same live safety projection without a DCP freeze."""
    return _endpoint_action_tcl(
        candidate_index,
        num_paths,
        "clk_fpl26contest",
    )


class EndpointBelMoveSkill:
    """Try same-site FF->FF2 moves for endpoint registers on critical paths."""

    name = "endpoint_bel_move"

    async def execute(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        clock_period: Optional[float],
    ) -> SkillResult:
        run_dir = Path(mcp.run_dir)
        raw_input_dcp = Path(
            params.get("input_dcp") or run_dir / "current_best.dcp"
        ).expanduser()
        input_dcp = raw_input_dcp.resolve()
        num_paths = max(1, min(int(params.get("num_paths", 12)), 50))
        max_candidates = max(1, min(int(params.get("max_candidates", 6)), 20))
        attempt_timeout = float(params.get("attempt_timeout_s", 1800.0))
        contest_clock = str(
            params.get("_contest_clock")
            or getattr(mcp, "target_clock", "")
            or "clk_fpl26contest"
        ).strip()
        output_dcp = run_dir / "endpoint_bel_move.dcp"
        attempts: list[dict] = []
        feedback_attempts: list[dict] = []
        candidate_feedback: list[dict] = []
        candidate_outputs: list[SkillOutput] = []
        best_seen: Optional[dict] = None
        budget_stopped = False
        unstarted: list[str] = []
        frozen_targets: list[dict[str, Any]] = []
        source_dcp_sha256 = ""

        logger.info(
            "[endpoint_bel_move] target=%r paths=%d candidates=%d",
            target,
            num_paths,
            max_candidates,
        )

        try:
            if str(target or "").strip().lower() != "auto":
                raise ValueError(
                    "endpoint_bel_move accepts only anonymous target='auto'"
                )
            if raw_input_dcp.is_symlink() or not input_dcp.is_file():
                raise ValueError("endpoint_bel_move source DCP is unavailable or symlinked")
            source_dcp_sha256 = sha256_file(input_dcp)
            if params.get("_source_artifact_sha256") != source_dcp_sha256:
                raise ValueError(
                    "endpoint_bel_move source DCP does not match its graph parent"
                )
            await mcp.call_vivado(
                "open_checkpoint",
                {"dcp_path": str(input_dcp)},
                timeout=600.0,
            )
            if sha256_file(input_dcp) != source_dcp_sha256:
                raise RuntimeError("endpoint_bel_move source DCP changed while opening")
            input_route_report = await mcp.call_vivado(
                "report_route_status", {}, timeout=120.0
            )
            input_route = parse_route_status_static(input_route_report)
            if input_route.get("routed_ok") is not True:
                raise RuntimeError("endpoint_bel_move requires a fully routed source DCP")
            discovery_output = await mcp.call_vivado(
                "run_tcl",
                {
                    "command": _build_endpoint_safety_evidence_tcl(
                        num_paths,
                        contest_clock,
                    ),
                    "timeout": min(attempt_timeout, 600.0),
                },
                timeout=min(attempt_timeout, 600.0),
            )
            evidence_rows = _parse_endpoint_safety_evidence(str(discovery_output))
            frozen_targets = [
                _freeze_endpoint_target(source_dcp_sha256, evidence)
                for evidence in evidence_rows[:max_candidates]
            ]
            if not frozen_targets:
                raise RuntimeError("no endpoint candidates have complete read-only safety evidence")

            for idx, frozen_target in enumerate(frozen_targets):
                label = f"endpoint_bel_move variant={idx + 1:02d}"[:120]
                if candidate_budget_exhausted(
                    params, completed_attempts=len(feedback_attempts)
                ):
                    budget_stopped = True
                    unstarted = [
                        f"endpoint_bel_move variant={future + 1:02d}"[:120]
                        for future in range(idx, len(frozen_targets))
                    ]
                    break
                started = time.monotonic()
                candidate_dcp = run_dir / f"endpoint_bel_move_{idx + 1:02d}.dcp"
                evidence = frozen_target["evidence"]
                attempt = {
                    "candidate_index": int(frozen_target["candidate_index"]),
                    "frozen_target_id": frozen_target["target_id"],
                    "absolute_delay_anchor_ns": evidence["timing"][
                        "absolute_delay_anchor_ns"
                    ],
                    "output_dcp": str(candidate_dcp),
                }
                try:
                    _validate_frozen_endpoint_target(
                        frozen_target,
                        source_dcp_sha256,
                    )
                    if sha256_file(input_dcp) != source_dcp_sha256:
                        raise RuntimeError("endpoint_bel_move source DCP changed before action")
                    await mcp.call_vivado(
                        "open_checkpoint",
                        {"dcp_path": str(input_dcp)},
                        timeout=600.0,
                    )
                    if sha256_file(input_dcp) != source_dcp_sha256:
                        raise RuntimeError("endpoint_bel_move source DCP changed while reopening")
                    tcl = _build_frozen_endpoint_move_tcl(
                        frozen_target,
                        num_paths,
                        contest_clock,
                    )
                    tool_output = await mcp.call_vivado(
                        "run_tcl",
                        {"command": tcl, "timeout": attempt_timeout},
                        timeout=attempt_timeout,
                    )
                    tool_output_text = str(tool_output)
                    lowered_output = tool_output_text.lower()
                    expected_completion = (
                        "FDAGENTS endpoint_bel_move "
                        f"target_id={frozen_target['target_id']} reresolved=1"
                    )
                    if (
                        "did not re-resolve exactly once" in lowered_output
                        or "frozen endpoint" in lowered_output
                        or "error:" in lowered_output
                        or expected_completion not in tool_output_text
                    ):
                        raise RuntimeError(tool_output_text[:500])

                    route_report = await mcp.call_vivado(
                        "report_route_status", {}, timeout=120.0
                    )
                    route = parse_route_status_static(route_report)
                    is_legal = bool(route.get("routed_ok"))

                    after_wns = await mcp.get_wns(
                        contest_clock, allow_fallback=False
                    )
                    if after_wns is None:
                        raise RuntimeError(
                            "contest-clock WNS unavailable after endpoint BEL move"
                        )

                    attempt.update(
                        {
                            "after_wns": after_wns,
                            "delta_wns": after_wns - before_wns,
                            "is_legal": is_legal,
                            "route": route,
                            "tool_output": tool_output_text[:500],
                        }
                    )

                    if is_legal:
                        await mcp.call_vivado(
                            "write_checkpoint",
                            {"dcp_path": str(candidate_dcp.resolve()), "force": True},
                            timeout=600.0,
                        )
                        candidate_dcp_sha256 = sha256_file(candidate_dcp)
                        candidate_outputs.append(
                            SkillOutput(
                                candidate_dcp,
                                label,
                                evidence={
                                    "schema_version": 1,
                                    "source_dcp_sha256": source_dcp_sha256,
                                    "candidate_dcp_sha256": candidate_dcp_sha256,
                                    "frozen_target_id": frozen_target["target_id"],
                                    "exactly_one_reresolution": True,
                                    "endpoint_safety": evidence,
                                },
                            )
                        )
                        if best_seen is None or after_wns > best_seen["after_wns"]:
                            best_seen = dict(attempt)

                    logger.info(
                        "[endpoint_bel_move] idx=%d legal=%s wns %.3f delta=%+.3f",
                        idx,
                        is_legal,
                        after_wns,
                        after_wns - before_wns,
                    )
                except Exception as e:
                    attempt.update({"error": str(e), "is_legal": False})
                    logger.info("[endpoint_bel_move] idx=%d skipped: %s", idx, e)

                attempts.append(attempt)
                feedback = {
                    "label": label,
                    "status": (
                        "legal" if attempt.get("is_legal") else
                        "error" if "error" in attempt else "illegal"
                    ),
                    "local_metrics": {
                        "wns": attempt.get("after_wns"),
                        "delta_wns": attempt.get("delta_wns"),
                    },
                    "runtime_s": time.monotonic() - started,
                }
                feedback_attempts.append(feedback)
                if attempt.get("is_legal"):
                    candidate_feedback.append(feedback)

            if best_seen is None:
                error = "no legal endpoint BEL moves"
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
                        "safety_candidate_count": len(frozen_targets),
                        "budget_stopped": budget_stopped,
                        "unstarted": unstarted,
                    },
                )

            output_dcp = Path(str(best_seen["output_dcp"]))
            after_wns = float(best_seen["after_wns"])
            await mcp.call_vivado(
                "open_checkpoint",
                {"dcp_path": str(output_dcp.resolve())},
                timeout=600.0,
            )

            fmax_b = calculate_fmax(before_wns, clock_period)
            fmax_a = calculate_fmax(after_wns, clock_period)
            if fmax_b and fmax_a:
                summary = (
                    f"endpoint_bel_move idx={best_seen['candidate_index']} "
                    f"wns {before_wns:.3f}->{after_wns:.3f} "
                    f"fmax {fmax_b:.1f}->{fmax_a:.1f} MHz"
                )
            else:
                summary = (
                    f"endpoint_bel_move idx={best_seen['candidate_index']} "
                    f"wns {before_wns:.3f}->{after_wns:.3f} "
                    f"delta={after_wns - before_wns:+.3f}"
                )
            if budget_stopped:
                summary += (
                    f" budget_stop {len(feedback_attempts)}/{len(frozen_targets)}"
                )

            return SkillResult(
                success=True,
                before_wns=before_wns,
                after_wns=after_wns,
                delta_wns=after_wns - before_wns,
                is_legal=True,
                output_dcp=output_dcp,
                summary=summary,
                details={
                    "attempts": feedback_attempts,
                    "candidates": candidate_feedback,
                    "safety_candidate_count": len(frozen_targets),
                    "source_dcp_sha256": source_dcp_sha256,
                    "budget_stopped": budget_stopped,
                    "unstarted": unstarted,
                },
                candidates=tuple(candidate_outputs),
            )

        except Exception as e:
            logger.error("[endpoint_bel_move] failed: %s", e)
            return SkillResult.failure(
                before_wns,
                str(e),
                output_dcp,
                details={
                    "attempts": feedback_attempts,
                    "candidates": candidate_feedback,
                    "safety_candidate_count": len(frozen_targets),
                    "budget_stopped": budget_stopped,
                    "unstarted": unstarted,
                },
            )
