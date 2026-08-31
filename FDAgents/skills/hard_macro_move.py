"""Proof-gated relocation of one uncascaded hard macro on a critical path.

Only live, anonymous ``auto`` targets are accepted.  Discovery and mutation use
the same bounded Tcl projection; a candidate is bound to the exact source DCP
and must re-resolve immediately before it is moved.
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
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from ..artifacts import sha256_file
from .base import (
    SkillOutput,
    SkillResult,
    calculate_fmax,
    candidate_budget_exhausted,
    parse_route_status_static,
)

logger = logging.getLogger(__name__)

SUPPORTED_PRIMITIVES = ("RAMB18E2", "RAMB36E2", "DSP48E2", "URAM288")
MAX_CANDIDATES = 4
MAX_NUM_PATHS = 50
MAX_SITE_DISTANCE = 32
DEFAULT_NUM_PATHS = 20
DEFAULT_MAX_CANDIDATES = 4
DEFAULT_MAX_SITE_DISTANCE = 6
DEFAULT_ATTEMPT_TIMEOUT_S = 1800.0

_EVIDENCE_PREFIX = "FDAGENTS_HARD_MACRO_SAFETY_V1"
_SUMMARY_PREFIX = "FDAGENTS_HARD_MACRO_SAFETY_SUMMARY_V1"
_TARGET_RE = re.compile(r"^hmv1:(?P<token>[0-9a-f]{64})$")
_ROW_KEYS = (
    "ordinal",
    "primitive",
    "path_rank",
    "source_site_type",
    "target_site_type",
    "old_distance",
    "new_distance",
    "geometric_improvement",
    "cascade_net_count",
    "cell_lock_count",
    "net_lock_count",
    "source_cell_count",
    "target_site_cell_count",
    "target_overlap_count",
    "allowed_sibling_ramb18_count",
    "affected_net_count",
    "resolution_token",
)


class HardMacroRejectReason(str, Enum):
    """Typed fail-closed preflight rejection classes."""

    CASCADE_CONNECTED = "cascade_connected"
    CELL_FIXED = "cell_fixed"
    NET_FIXED = "net_fixed"
    SITE_INCOMPATIBLE = "site_incompatible"
    SOURCE_OCCUPANCY = "source_occupancy"
    TARGET_OCCUPIED = "target_occupied"
    TARGET_OVERLAP = "target_overlap"
    AFFECTED_NETS_INVALID = "affected_nets_invalid"
    GEOMETRY_NOT_IMPROVED = "geometry_not_improved"


class HardMacroMoveReject(ValueError):
    def __init__(self, reason: HardMacroRejectReason, message: str):
        super().__init__(f"{reason.value}: {message}")
        self.reason = reason


def _tcl_quote(value: str) -> str:
    return "{" + str(value).replace("\\", "\\\\").replace("}", "\\}") + "}"


def _site_type_matches(primitive: str, site_type: str) -> bool:
    prefixes = {
        "RAMB18E2": "RAMB18",
        "RAMB36E2": "RAMB36",
        "DSP48E2": "DSP48E2",
        "URAM288": "URAM288",
    }
    expected = prefixes.get(primitive)
    return bool(expected and str(site_type).upper().startswith(expected))


def _reject_reason_for_facts(facts: dict[str, Any]) -> Optional[HardMacroRejectReason]:
    """Validate one complete candidate projection in a stable gate order."""
    primitive = facts.get("primitive")
    source_type = facts.get("source_site_type")
    target_type = facts.get("target_site_type")
    if (
        primitive not in SUPPORTED_PRIMITIVES
        or not isinstance(source_type, str)
        or not isinstance(target_type, str)
        or source_type != target_type
        or not _site_type_matches(str(primitive), source_type)
    ):
        return HardMacroRejectReason.SITE_INCOMPATIBLE
    if facts.get("cascade_net_count") != 0:
        return HardMacroRejectReason.CASCADE_CONNECTED
    if facts.get("cell_lock_count") != 0:
        return HardMacroRejectReason.CELL_FIXED
    if facts.get("net_lock_count") != 0:
        return HardMacroRejectReason.NET_FIXED
    if facts.get("source_cell_count") != 1:
        return HardMacroRejectReason.SOURCE_OCCUPANCY
    if facts.get("target_site_cell_count") != 0:
        return HardMacroRejectReason.TARGET_OCCUPIED
    if facts.get("target_overlap_count") != 0:
        return HardMacroRejectReason.TARGET_OVERLAP
    sibling_count = facts.get("allowed_sibling_ramb18_count")
    if primitive == "RAMB18E2":
        # The other RAMB18 half in the same RAMB36 tile is legal and must not
        # be confused with occupancy of the selected half.
        if sibling_count not in (0, 1):
            return HardMacroRejectReason.TARGET_OVERLAP
    elif sibling_count != 0:
        return HardMacroRejectReason.TARGET_OVERLAP
    affected = facts.get("affected_net_count")
    if isinstance(affected, bool) or not isinstance(affected, int) or not 1 <= affected <= 512:
        return HardMacroRejectReason.AFFECTED_NETS_INVALID
    old_distance = facts.get("old_distance")
    new_distance = facts.get("new_distance")
    improvement = facts.get("geometric_improvement")
    if (
        isinstance(old_distance, bool)
        or isinstance(new_distance, bool)
        or isinstance(improvement, bool)
        or not all(isinstance(value, int) for value in (old_distance, new_distance, improvement))
        or old_distance < 0
        or new_distance < 0
        or improvement <= 0
        or improvement != old_distance - new_distance
    ):
        return HardMacroRejectReason.GEOMETRY_NOT_IMPROVED
    return None


def _hard_macro_discovery_commands(
    num_paths: int,
    max_site_distance: int,
    max_candidates: int,
    contest_clock: str,
) -> list[str]:
    """Build the bounded live discovery shared by evidence and mutation."""
    num_paths = max(1, min(int(num_paths), MAX_NUM_PATHS))
    max_site_distance = max(1, min(int(max_site_distance), MAX_SITE_DISTANCE))
    max_candidates = max(1, min(int(max_candidates), MAX_CANDIDATES))
    clock = _tcl_quote(contest_clock or "clk_fpl26contest")
    return [
        (
            "proc __fda_hm_true {object property} { set value {}; "
            "if {[catch {set value [get_property $property $object]}]} { return 0 }; "
            "return [expr {$value eq {1} || [string equal -nocase $value {true}]}] }"
        ),
        (
            "proc __fda_hm_locked_net {net} { "
            "if {[__fda_hm_true $net IS_LOC_FIXED] || [__fda_hm_true $net IS_BEL_FIXED] || "
            "[__fda_hm_true $net DONT_TOUCH] || [__fda_hm_true $net IS_ROUTE_FIXED]} { return 1 }; "
            "set fixed {}; if {![catch {set fixed [get_property FIXED_ROUTE $net]}] && $fixed ne {}} { return 1 }; "
            "return 0 }"
        ),
        (
            "proc __fda_hm_locked_cell {cell} { "
            "if {[__fda_hm_true $cell IS_LOC_FIXED] || [__fda_hm_true $cell IS_BEL_FIXED] || "
            "[__fda_hm_true $cell DONT_TOUCH] || [__fda_hm_true $cell IS_ROUTE_FIXED]} { return 1 }; "
            "set fixed {}; if {![catch {set fixed [get_property FIXED_ROUTE $cell]}] && $fixed ne {}} { return 1 }; "
            "return 0 }"
        ),
        (
            "proc __fda_hm_xy {site} { set tiles [get_tiles -quiet -of_objects $site]; "
            "if {[llength $tiles] != 1} { return {} }; set tile [lindex $tiles 0]; "
            "set col {}; set row {}; catch {set col [get_property COLUMN $tile]}; "
            "catch {set row [get_property ROW $tile]}; "
            "if {![string is integer -strict $col] || ![string is integer -strict $row]} { return {} }; "
            "return [list $col $row] }"
        ),
        (
            "proc __fda_hm_cascade_pin {primitive leaf} { switch -- $primitive { "
            "DSP48E2 { return [regexp {^(ACIN|ACOUT|BCIN|BCOUT|PCIN|PCOUT|CARRYCASCIN|CARRYCASCOUT|MULTSIGNIN|MULTSIGNOUT)(\\[.*\\])?$} $leaf] } "
            "RAMB18E2 - RAMB36E2 { return [regexp {^CASCADE} $leaf] } "
            "URAM288 { return [regexp {^CAS_} $leaf] } default { return 1 } } }"
        ),
        (
            "proc __fda_hm_compare {left right} { "
            "foreach index {0 1 2 3} mode {integer integer dictionary dictionary} { "
            "set a [lindex $left $index]; set b [lindex $right $index]; "
            "if {$mode eq {integer}} { if {$a < $b} { return -1 }; if {$a > $b} { return 1 } "
            "} else { set result [string compare $a $b]; if {$result != 0} { return $result } } }; return 0 }"
        ),
        "set fdagents_all_clocks [get_clocks -quiet]",
        "set fdagents_all_clock_names [get_property NAME $fdagents_all_clocks]",
        f"set fdagents_clock_indices [lsearch -all -exact $fdagents_all_clock_names {clock}]",
        (
            "if {[llength $fdagents_clock_indices] != 1} { "
            "error {FDAGENTS hard macro contest clock is missing or ambiguous} }"
        ),
        "set fdagents_clock [lindex $fdagents_all_clocks [lindex $fdagents_clock_indices 0]]",
        "unset fdagents_all_clocks fdagents_all_clock_names fdagents_clock_indices",
        (
            "set fdagents_hm_paths [get_timing_paths -quiet -setup "
            f"-max_paths {num_paths} -nworst 1 -sort_by slack -group $fdagents_clock]"
        ),
        "set fdagents_hm_ranked {}",
        "set fdagents_hm_seen_cells {}",
        "set fdagents_hm_path_rank -1",
        (
            "foreach fdagents_hm_path $fdagents_hm_paths { incr fdagents_hm_path_rank; "
            "set fdagents_hm_path_pins [get_pins -quiet -of_objects $fdagents_hm_path]; "
            "set fdagents_hm_path_cells [lsort -dictionary -unique [get_cells -quiet -of_objects $fdagents_hm_path_pins]]; "
            "set fdagents_hm_anchor_col 0; set fdagents_hm_anchor_row 0; set fdagents_hm_anchor_count 0; "
            "foreach fdagents_hm_anchor_cell $fdagents_hm_path_cells { "
            "set fdagents_hm_anchor_site [get_sites -quiet [get_property LOC $fdagents_hm_anchor_cell]]; "
            "if {[llength $fdagents_hm_anchor_site] != 1} { continue }; "
            "set fdagents_hm_anchor_xy [__fda_hm_xy $fdagents_hm_anchor_site]; "
            "if {[llength $fdagents_hm_anchor_xy] != 2} { continue }; "
            "incr fdagents_hm_anchor_col [lindex $fdagents_hm_anchor_xy 0]; "
            "incr fdagents_hm_anchor_row [lindex $fdagents_hm_anchor_xy 1]; incr fdagents_hm_anchor_count }; "
            "if {$fdagents_hm_anchor_count == 0} { continue }; "
            "set fdagents_hm_anchor_col [expr {$fdagents_hm_anchor_col / $fdagents_hm_anchor_count}]; "
            "set fdagents_hm_anchor_row [expr {$fdagents_hm_anchor_row / $fdagents_hm_anchor_count}]; "
            "foreach fdagents_hm_cell $fdagents_hm_path_cells { "
            "if {[lsearch -exact $fdagents_hm_seen_cells $fdagents_hm_cell] >= 0} { continue }; "
            "set fdagents_hm_primitive [get_property REF_NAME $fdagents_hm_cell]; "
            "if {[lsearch -exact {RAMB18E2 RAMB36E2 DSP48E2 URAM288} $fdagents_hm_primitive] < 0} { continue }; "
            "lappend fdagents_hm_seen_cells $fdagents_hm_cell; "
            "set fdagents_hm_cell_locks [__fda_hm_locked_cell $fdagents_hm_cell]; "
            "if {$fdagents_hm_cell_locks != 0} { continue }; "
            "set fdagents_hm_pins [get_pins -quiet -of_objects $fdagents_hm_cell]; "
            "set fdagents_hm_cascade_count 0; "
            "foreach fdagents_hm_pin $fdagents_hm_pins { set fdagents_hm_leaf {}; "
            "catch {set fdagents_hm_leaf [get_property REF_PIN_NAME $fdagents_hm_pin]}; "
            "if {$fdagents_hm_leaf eq {}} { set fdagents_hm_leaf [lindex [split [get_property NAME $fdagents_hm_pin] /] end] }; "
            "if {[__fda_hm_cascade_pin $fdagents_hm_primitive $fdagents_hm_leaf]} { "
            "incr fdagents_hm_cascade_count [llength [get_nets -quiet -of_objects $fdagents_hm_pin]] } }; "
            "if {$fdagents_hm_cascade_count != 0} { continue }; "
            "set fdagents_hm_nets [lsort -dictionary -unique [get_nets -quiet -of_objects $fdagents_hm_pins]]; "
            "set fdagents_hm_net_count [llength $fdagents_hm_nets]; "
            "if {$fdagents_hm_net_count < 1 || $fdagents_hm_net_count > 512} { continue }; "
            "set fdagents_hm_net_locks 0; foreach fdagents_hm_net $fdagents_hm_nets { "
            "incr fdagents_hm_net_locks [__fda_hm_locked_net $fdagents_hm_net] }; "
            "if {$fdagents_hm_net_locks != 0} { continue }; "
            "set fdagents_hm_source_site [get_sites -quiet [get_property LOC $fdagents_hm_cell]]; "
            "if {[llength $fdagents_hm_source_site] != 1} { continue }; set fdagents_hm_source_site [lindex $fdagents_hm_source_site 0]; "
            "set fdagents_hm_source_cells [get_cells -quiet -of_objects $fdagents_hm_source_site]; "
            "if {[llength $fdagents_hm_source_cells] != 1 || [lindex $fdagents_hm_source_cells 0] ne $fdagents_hm_cell} { continue }; "
            "set fdagents_hm_source_type [get_property SITE_TYPE $fdagents_hm_source_site]; "
            "set fdagents_hm_expected_prefix [dict get {RAMB18E2 RAMB18 RAMB36E2 RAMB36 DSP48E2 DSP48E2 URAM288 URAM288} $fdagents_hm_primitive]; "
            "if {![string match ${fdagents_hm_expected_prefix}* [string toupper $fdagents_hm_source_type]]} { continue }; "
            "set fdagents_hm_source_xy [__fda_hm_xy $fdagents_hm_source_site]; if {[llength $fdagents_hm_source_xy] != 2} { continue }; "
            "set fdagents_hm_source_col [lindex $fdagents_hm_source_xy 0]; set fdagents_hm_source_row [lindex $fdagents_hm_source_xy 1]; "
            "set fdagents_hm_old_distance [expr {abs($fdagents_hm_source_col-$fdagents_hm_anchor_col)+abs($fdagents_hm_source_row-$fdagents_hm_anchor_row)}]; "
            "foreach fdagents_hm_target_site [lsort -dictionary [get_sites -quiet -filter \"SITE_TYPE == $fdagents_hm_source_type\"]] { "
            "if {$fdagents_hm_target_site eq $fdagents_hm_source_site} { continue }; "
            "set fdagents_hm_target_xy [__fda_hm_xy $fdagents_hm_target_site]; if {[llength $fdagents_hm_target_xy] != 2} { continue }; "
            "set fdagents_hm_target_col [lindex $fdagents_hm_target_xy 0]; set fdagents_hm_target_row [lindex $fdagents_hm_target_xy 1]; "
            "set fdagents_hm_site_distance [expr {abs($fdagents_hm_target_col-$fdagents_hm_source_col)+abs($fdagents_hm_target_row-$fdagents_hm_source_row)}]; "
            f"if {{$fdagents_hm_site_distance < 1 || $fdagents_hm_site_distance > {max_site_distance}}} {{ continue }}; "
            "set fdagents_hm_new_distance [expr {abs($fdagents_hm_target_col-$fdagents_hm_anchor_col)+abs($fdagents_hm_target_row-$fdagents_hm_anchor_row)}]; "
            "set fdagents_hm_improvement [expr {$fdagents_hm_old_distance-$fdagents_hm_new_distance}]; if {$fdagents_hm_improvement <= 0} { continue }; "
            "set fdagents_hm_target_cells [get_cells -quiet -of_objects $fdagents_hm_target_site]; "
            "set fdagents_hm_target_cell_count [llength $fdagents_hm_target_cells]; if {$fdagents_hm_target_cell_count != 0} { continue }; "
            "set fdagents_hm_target_tiles [get_tiles -quiet -of_objects $fdagents_hm_target_site]; if {[llength $fdagents_hm_target_tiles] != 1} { continue }; "
            "set fdagents_hm_tile_cells [get_cells -quiet -of_objects [get_sites -quiet -of_objects $fdagents_hm_target_tiles]]; "
            "set fdagents_hm_overlap_count 0; set fdagents_hm_sibling_count 0; "
            "foreach fdagents_hm_tile_cell $fdagents_hm_tile_cells { set fdagents_hm_tile_ref [get_property REF_NAME $fdagents_hm_tile_cell]; "
            "if {$fdagents_hm_primitive eq {RAMB18E2}} { if {$fdagents_hm_tile_ref eq {RAMB36E2}} { incr fdagents_hm_overlap_count }; "
            "if {$fdagents_hm_tile_ref eq {RAMB18E2}} { incr fdagents_hm_sibling_count } "
            "} elseif {$fdagents_hm_primitive eq {RAMB36E2} && ($fdagents_hm_tile_ref eq {RAMB18E2} || $fdagents_hm_tile_ref eq {RAMB36E2})} { incr fdagents_hm_overlap_count } }; "
            "if {$fdagents_hm_overlap_count != 0 || ($fdagents_hm_primitive eq {RAMB18E2} && $fdagents_hm_sibling_count > 1)} { continue }; "
            "set fdagents_hm_identity_material [list [get_property NAME $fdagents_hm_cell] $fdagents_hm_source_site $fdagents_hm_target_site "
            "$fdagents_hm_primitive $fdagents_hm_source_type [get_property NAME $fdagents_hm_nets]]; "
            "set fdagents_hm_resolution_token [string trim [binary encode base64 -maxlen 0 [encoding convertto utf-8 $fdagents_hm_identity_material]]]; "
            "set fdagents_hm_cell_name [get_property NAME $fdagents_hm_cell]; set fdagents_hm_target_name [get_property NAME $fdagents_hm_target_site]; "
            "lappend fdagents_hm_ranked [list [expr {-$fdagents_hm_improvement}] $fdagents_hm_new_distance $fdagents_hm_cell_name $fdagents_hm_target_name "
            "$fdagents_hm_cell $fdagents_hm_source_site $fdagents_hm_target_site $fdagents_hm_primitive $fdagents_hm_old_distance "
            "$fdagents_hm_new_distance $fdagents_hm_improvement $fdagents_hm_nets $fdagents_hm_resolution_token "
            "$fdagents_hm_cascade_count $fdagents_hm_cell_locks $fdagents_hm_net_locks [llength $fdagents_hm_source_cells] "
            "$fdagents_hm_target_cell_count $fdagents_hm_overlap_count $fdagents_hm_sibling_count $fdagents_hm_net_count "
            "$fdagents_hm_source_type [get_property SITE_TYPE $fdagents_hm_target_site] $fdagents_hm_path_rank] } } }"
        ),
        "set fdagents_hm_ranked [lsort -command __fda_hm_compare $fdagents_hm_ranked]",
        f"set fdagents_hm_candidates [lrange $fdagents_hm_ranked 0 {max_candidates - 1}]",
    ]


def _build_hard_macro_safety_evidence_tcl(
    num_paths: int = DEFAULT_NUM_PATHS,
    max_site_distance: int = DEFAULT_MAX_SITE_DISTANCE,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    contest_clock: str = "clk_fpl26contest",
) -> str:
    commands = _hard_macro_discovery_commands(
        num_paths, max_site_distance, max_candidates, contest_clock
    )
    commands.extend([
        "set fdagents_hm_ordinal 0",
        (
            "foreach fdagents_hm_candidate $fdagents_hm_candidates { "
            f"puts [join [list {_EVIDENCE_PREFIX} "
            "ordinal=$fdagents_hm_ordinal primitive=[lindex $fdagents_hm_candidate 7] "
            "path_rank=[lindex $fdagents_hm_candidate 23] source_site_type=[lindex $fdagents_hm_candidate 21] "
            "target_site_type=[lindex $fdagents_hm_candidate 22] old_distance=[lindex $fdagents_hm_candidate 8] "
            "new_distance=[lindex $fdagents_hm_candidate 9] geometric_improvement=[lindex $fdagents_hm_candidate 10] "
            "cascade_net_count=[lindex $fdagents_hm_candidate 13] cell_lock_count=[lindex $fdagents_hm_candidate 14] "
            "net_lock_count=[lindex $fdagents_hm_candidate 15] source_cell_count=[lindex $fdagents_hm_candidate 16] "
            "target_site_cell_count=[lindex $fdagents_hm_candidate 17] target_overlap_count=[lindex $fdagents_hm_candidate 18] "
            "allowed_sibling_ramb18_count=[lindex $fdagents_hm_candidate 19] affected_net_count=[lindex $fdagents_hm_candidate 20] "
            "resolution_token=[lindex $fdagents_hm_candidate 12]] {|}]; incr fdagents_hm_ordinal }"
        ),
        f'puts "{_SUMMARY_PREFIX}|count=[llength $fdagents_hm_candidates]"',
    ])
    return "; ".join(commands)


def _parse_hard_macro_safety_evidence(output: str) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    seen: set[int] = set()
    summary: Optional[int] = None
    for raw_line in str(output or "").splitlines():
        line = raw_line.strip()
        if line.startswith(f"{_SUMMARY_PREFIX}|"):
            if summary is not None:
                raise ValueError("duplicate hard macro safety summary")
            fields = line.split("|")
            if len(fields) != 2 or not fields[1].startswith("count="):
                raise ValueError("malformed hard macro safety summary")
            try:
                summary = int(fields[1].partition("=")[2])
            except ValueError as exc:
                raise ValueError("malformed hard macro safety count") from exc
            if not 0 <= summary <= MAX_CANDIDATES:
                raise ValueError("hard macro safety count is out of range")
            continue
        if not line.startswith(f"{_EVIDENCE_PREFIX}|"):
            continue
        values: dict[str, str] = {}
        for field in line.split("|")[1:]:
            key, separator, value = field.partition("=")
            if not separator or not key or not value or key in values:
                raise ValueError("malformed hard macro safety row")
            values[key] = value
        if set(values) != set(_ROW_KEYS):
            raise ValueError("hard macro safety row schema mismatch")
        try:
            ints = {
                key: int(values[key])
                for key in _ROW_KEYS
                if key not in {
                    "primitive", "source_site_type", "target_site_type", "resolution_token"
                }
            }
        except ValueError as exc:
            raise ValueError("hard macro safety row has non-integer facts") from exc
        ordinal = ints["ordinal"]
        if ordinal < 0 or ordinal in seen:
            raise ValueError("hard macro safety ordinal is invalid or duplicated")
        if not 0 <= ints["path_rank"] < MAX_NUM_PATHS:
            raise ValueError("hard macro safety path rank is out of range")
        facts: dict[str, Any] = {
            **ints,
            "primitive": values["primitive"],
            "source_site_type": values["source_site_type"],
            "target_site_type": values["target_site_type"],
        }
        reason = _reject_reason_for_facts(facts)
        if reason is not None:
            raise HardMacroMoveReject(reason, "live safety projection rejected")
        try:
            identity = base64.b64decode(
                values["resolution_token"].encode("ascii"), validate=True
            ).decode("utf-8")
        except (ValueError, UnicodeError, binascii.Error) as exc:
            raise ValueError("hard macro resolution token is malformed") from exc
        if not identity or len(identity) > 65536 or "\x00" in identity:
            raise ValueError("hard macro resolution identity is invalid")
        seen.add(ordinal)
        parsed.append({
            "schema_version": 1,
            "candidate_index": ordinal,
            "primitive": values["primitive"],
            "path_rank": ints["path_rank"],
            "site_types": {
                "source": values["source_site_type"],
                "target": values["target_site_type"],
            },
            "geometry": {
                "old_distance": ints["old_distance"],
                "new_distance": ints["new_distance"],
                "improvement": ints["geometric_improvement"],
            },
            "safety": {
                "cascade_net_count": ints["cascade_net_count"],
                "cell_lock_count": ints["cell_lock_count"],
                "net_lock_count": ints["net_lock_count"],
                "source_cell_count": ints["source_cell_count"],
                "target_site_cell_count": ints["target_site_cell_count"],
                "target_overlap_count": ints["target_overlap_count"],
                "allowed_sibling_ramb18_count": ints["allowed_sibling_ramb18_count"],
                "affected_net_count": ints["affected_net_count"],
            },
            "internal_target_fingerprint": hashlib.sha256(identity.encode()).hexdigest(),
            "_resolution_token": values["resolution_token"],
        })
    parsed.sort(key=lambda item: int(item["candidate_index"]))
    if summary is None:
        raise ValueError("hard macro safety summary is missing")
    if summary != len(parsed) or [item["candidate_index"] for item in parsed] != list(range(summary)):
        raise ValueError("hard macro safety rows do not match summary")
    return parsed


def _freeze_hard_macro_target(source_sha256: str, evidence: dict[str, Any]) -> dict[str, Any]:
    source = str(source_sha256 or "").lower()
    if re.fullmatch(r"[0-9a-f]{64}", source) is None:
        raise ValueError("hard macro source DCP SHA-256 is invalid")
    if not isinstance(evidence, dict) or evidence.get("schema_version") != 1:
        raise ValueError("hard macro evidence is malformed")
    candidate_index = evidence.get("candidate_index")
    if (
        isinstance(candidate_index, bool)
        or not isinstance(candidate_index, int)
        or not 0 <= candidate_index < MAX_CANDIDATES
    ):
        raise ValueError("hard macro candidate index is invalid")
    geometry = evidence.get("geometry") or {}
    safety = evidence.get("safety") or {}
    site_types = evidence.get("site_types") or {}
    facts = {
        "primitive": evidence.get("primitive"),
        "source_site_type": site_types.get("source"),
        "target_site_type": site_types.get("target"),
        "old_distance": geometry.get("old_distance"),
        "new_distance": geometry.get("new_distance"),
        "geometric_improvement": geometry.get("improvement"),
        **safety,
    }
    reason = _reject_reason_for_facts(facts)
    if reason is not None:
        raise HardMacroMoveReject(reason, "frozen safety projection rejected")
    path_rank = evidence.get("path_rank")
    if isinstance(path_rank, bool) or not isinstance(path_rank, int) or not 0 <= path_rank < MAX_NUM_PATHS:
        raise ValueError("hard macro path rank is invalid")
    token = str(evidence.get("_resolution_token") or "")
    try:
        identity = base64.b64decode(token.encode("ascii"), validate=True).decode("utf-8")
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise ValueError("hard macro resolution token is invalid") from exc
    public = {key: value for key, value in evidence.items() if not key.startswith("_")}
    canonical = json.loads(json.dumps(public, sort_keys=True, separators=(",", ":"), allow_nan=False))
    fingerprint = hashlib.sha256(identity.encode()).hexdigest()
    if canonical.get("internal_target_fingerprint") != fingerprint:
        raise ValueError("hard macro identity fingerprint is inconsistent")
    projection = dict(canonical)
    projection.pop("candidate_index", None)
    projection_sha = hashlib.sha256(
        json.dumps(projection, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    target_hash = hashlib.sha256(
        f"hard-macro-move-v1\n{source}\n{fingerprint}\n{projection_sha}".encode("ascii")
    ).hexdigest()
    return {
        "target_id": f"hmv1:{target_hash}",
        "source_dcp_sha256": source,
        "projection_sha256": projection_sha,
        "candidate_index": int(canonical["candidate_index"]),
        "internal_resolution_token": token,
        "evidence": canonical,
    }


def _validate_frozen_hard_macro_target(frozen: dict[str, Any], source_sha256: str) -> dict[str, Any]:
    if not isinstance(frozen, dict):
        raise ValueError("frozen hard macro target is malformed")
    evidence = dict(frozen.get("evidence") or {})
    evidence["_resolution_token"] = frozen.get("internal_resolution_token")
    expected = _freeze_hard_macro_target(source_sha256, evidence)
    if frozen != expected or _TARGET_RE.fullmatch(expected["target_id"]) is None:
        raise ValueError("frozen hard macro target is stale or inconsistent")
    return expected


def _build_frozen_hard_macro_move_tcl(
    frozen: dict[str, Any],
    num_paths: int,
    max_site_distance: int,
    max_candidates: int,
    contest_clock: str,
) -> str:
    validated = _validate_frozen_hard_macro_target(
        frozen, str(frozen.get("source_dcp_sha256") or "")
    )
    commands = _hard_macro_discovery_commands(
        num_paths, max_site_distance, max_candidates, contest_clock
    )
    token = _tcl_quote(validated["internal_resolution_token"])
    commands.extend([
        "set fdagents_hm_reresolved {}",
        (
            "foreach fdagents_hm_live $fdagents_hm_candidates { "
            f"if {{[lindex $fdagents_hm_live 12] eq {token}}} {{ lappend fdagents_hm_reresolved $fdagents_hm_live }} }}"
        ),
        (
            "if {[llength $fdagents_hm_reresolved] != 1} { "
            "error {FDAGENTS frozen hard macro target did not re-resolve exactly once} }"
        ),
        "set fdagents_hm_candidate [lindex $fdagents_hm_reresolved 0]",
    ])
    evidence = validated["evidence"]
    string_projection = {
        7: evidence["primitive"],
        21: evidence["site_types"]["source"],
        22: evidence["site_types"]["target"],
    }
    integer_projection = {
        8: evidence["geometry"]["old_distance"],
        9: evidence["geometry"]["new_distance"],
        10: evidence["geometry"]["improvement"],
        13: evidence["safety"]["cascade_net_count"],
        14: evidence["safety"]["cell_lock_count"],
        15: evidence["safety"]["net_lock_count"],
        16: evidence["safety"]["source_cell_count"],
        17: evidence["safety"]["target_site_cell_count"],
        18: evidence["safety"]["target_overlap_count"],
        19: evidence["safety"]["allowed_sibling_ramb18_count"],
        20: evidence["safety"]["affected_net_count"],
        23: evidence["path_rank"],
    }
    for index, expected in string_projection.items():
        commands.append(
            f"if {{[lindex $fdagents_hm_candidate {index}] ne {_tcl_quote(str(expected))}}} {{ "
            "error {FDAGENTS frozen hard macro structural projection changed} }"
        )
    for index, expected in integer_projection.items():
        commands.append(
            f"if {{![string is integer -strict [lindex $fdagents_hm_candidate {index}]] || "
            f"[lindex $fdagents_hm_candidate {index}] != {int(expected)}}} {{ "
            "error {FDAGENTS frozen hard macro safety projection changed} }"
        )
    commands.extend([
        "set fdagents_hm_cell [lindex $fdagents_hm_candidate 4]",
        "set fdagents_hm_target_site [lindex $fdagents_hm_candidate 6]",
        "set fdagents_hm_route_nets [lindex $fdagents_hm_candidate 11]",
        (
            "if {[llength $fdagents_hm_route_nets] != [lindex $fdagents_hm_candidate 20]} { "
            "error {FDAGENTS hard macro affected net set changed} }"
        ),
        "unplace_cell $fdagents_hm_cell",
        "place_cell $fdagents_hm_cell $fdagents_hm_target_site",
        "route_design -unroute -nets $fdagents_hm_route_nets",
        (
            "if {[catch {route_design -preserve -nets $fdagents_hm_route_nets} fdagents_hm_route_error]} { "
            "error [format {FDAGENTS hard macro affected-net reroute failed: %s} $fdagents_hm_route_error] }"
        ),
        (
            "puts \"FDAGENTS hard_macro_move "
            f"target_id={validated['target_id']} reresolved=1 nets=[llength $fdagents_hm_route_nets]\""
        ),
    ])
    return "; ".join(commands)


class HardMacroMoveSkill:
    name = "hard_macro_move"

    async def execute(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        clock_period: Optional[float],
    ) -> SkillResult:
        run_dir = Path(mcp.run_dir)
        raw_input = Path(params.get("input_dcp") or run_dir / "current_best.dcp").expanduser()
        input_dcp = raw_input.resolve()
        failed_dcp = run_dir / "hard_macro_move_failed.dcp"
        num_paths = max(1, min(int(params.get("num_paths", DEFAULT_NUM_PATHS)), MAX_NUM_PATHS))
        max_candidates = max(1, min(int(params.get("max_candidates", DEFAULT_MAX_CANDIDATES)), MAX_CANDIDATES))
        max_site_distance = max(1, min(int(params.get("max_site_distance", DEFAULT_MAX_SITE_DISTANCE)), MAX_SITE_DISTANCE))
        attempt_timeout = float(params.get("attempt_timeout_s", DEFAULT_ATTEMPT_TIMEOUT_S))
        contest_clock = str(
            params.get("_contest_clock") or getattr(mcp, "target_clock", "") or "clk_fpl26contest"
        ).strip()
        attempts: list[dict[str, Any]] = []
        outputs: list[SkillOutput] = []
        best: Optional[dict[str, Any]] = None
        frozen_targets: list[dict[str, Any]] = []
        budget_stopped = False
        unstarted: list[str] = []

        async def rollback() -> None:
            await mcp.call_vivado(
                "open_checkpoint", {"dcp_path": str(input_dcp)}, timeout=600.0
            )

        try:
            if str(target or "").strip().lower() != "auto":
                raise ValueError("hard_macro_move accepts only anonymous target='auto'")
            if raw_input.is_symlink() or not input_dcp.is_file():
                raise ValueError("hard_macro_move source DCP is unavailable or symlinked")
            source_sha = sha256_file(input_dcp)
            if params.get("_source_artifact_sha256") != source_sha:
                raise ValueError("hard_macro_move source DCP does not match its graph parent")
            await rollback()
            if sha256_file(input_dcp) != source_sha:
                raise RuntimeError("hard_macro_move source DCP changed while opening")
            route = parse_route_status_static(
                await mcp.call_vivado("report_route_status", {}, timeout=120.0)
            )
            if route.get("routed_ok") is not True:
                raise RuntimeError("hard_macro_move requires a fully routed source DCP")
            discovery = await mcp.call_vivado(
                "run_tcl",
                {"command": _build_hard_macro_safety_evidence_tcl(
                    num_paths, max_site_distance, max_candidates, contest_clock
                ), "timeout": min(attempt_timeout, 600.0)},
                timeout=min(attempt_timeout, 600.0),
            )
            frozen_targets = [
                _freeze_hard_macro_target(source_sha, row)
                for row in _parse_hard_macro_safety_evidence(str(discovery))
            ]
            if not frozen_targets:
                raise RuntimeError("no legal uncascaded hard macro move candidates")

            for index, frozen in enumerate(frozen_targets):
                label = f"hard_macro_move variant={index + 1:02d}"[:120]
                if candidate_budget_exhausted(params, completed_attempts=len(attempts)):
                    budget_stopped = True
                    unstarted = [
                        f"hard_macro_move variant={future + 1:02d}"
                        for future in range(index, len(frozen_targets))
                    ]
                    break
                started = time.monotonic()
                candidate_dcp = run_dir / f"hard_macro_move_{index + 1:02d}.dcp"
                record: dict[str, Any] = {
                    "label": label,
                    "frozen_target_id": frozen["target_id"],
                    "primitive": frozen["evidence"]["primitive"],
                    "status": "error",
                }
                try:
                    _validate_frozen_hard_macro_target(frozen, source_sha)
                    if sha256_file(input_dcp) != source_sha:
                        raise RuntimeError("hard_macro_move source DCP changed before action")
                    await rollback()
                    if sha256_file(input_dcp) != source_sha:
                        raise RuntimeError("hard_macro_move source DCP changed while reopening")
                    command = _build_frozen_hard_macro_move_tcl(
                        frozen, num_paths, max_site_distance, max_candidates, contest_clock
                    )
                    tool_output = str(await mcp.call_vivado(
                        "run_tcl", {"command": command, "timeout": attempt_timeout}, timeout=attempt_timeout
                    ))
                    completion = (
                        f"FDAGENTS hard_macro_move target_id={frozen['target_id']} reresolved=1"
                    )
                    if completion not in tool_output or "error:" in tool_output.lower():
                        raise RuntimeError(tool_output[:500])
                    route = parse_route_status_static(
                        await mcp.call_vivado("report_route_status", {}, timeout=120.0)
                    )
                    if route.get("routed_ok") is not True:
                        raise RuntimeError("hard_macro_move affected-net reroute is incomplete")
                    after_wns = await mcp.get_wns(contest_clock, allow_fallback=False)
                    if after_wns is None or not math.isfinite(float(after_wns)):
                        raise RuntimeError("contest-clock WNS unavailable after hard macro move")
                    await mcp.call_vivado(
                        "write_checkpoint", {"dcp_path": str(candidate_dcp.resolve()), "force": True}, timeout=600.0
                    )
                    candidate_sha = sha256_file(candidate_dcp)
                    evidence = {
                        "schema_version": 1,
                        "source_dcp_sha256": source_sha,
                        "candidate_dcp_sha256": candidate_sha,
                        "frozen_target_id": frozen["target_id"],
                        "exactly_one_reresolution": True,
                        "hard_macro_safety": frozen["evidence"],
                    }
                    outputs.append(SkillOutput(candidate_dcp, label, evidence=evidence))
                    record.update({
                        "status": "legal",
                        "after_wns": float(after_wns),
                        "delta_wns": float(after_wns) - before_wns,
                        "route": route,
                    })
                    if best is None or float(after_wns) > best["after_wns"]:
                        best = {**record, "output_dcp": candidate_dcp}
                except Exception as exc:  # each attempt must restore the immutable seed
                    record["error"] = str(exc)
                    try:
                        await rollback()
                    except Exception as rollback_exc:
                        record["rollback_error"] = str(rollback_exc)
                record["runtime_s"] = round(time.monotonic() - started, 6)
                attempts.append(record)

            if best is None:
                await rollback()
                return SkillResult.failure(
                    before_wns, "no legal hard macro moves", failed_dcp,
                    details={"attempts": attempts, "safety_candidate_count": len(frozen_targets),
                             "budget_stopped": budget_stopped, "unstarted": unstarted},
                )
            output_dcp = Path(best["output_dcp"])
            await mcp.call_vivado(
                "open_checkpoint", {"dcp_path": str(output_dcp.resolve())}, timeout=600.0
            )
            after_wns = float(best["after_wns"])
            fmax_before = calculate_fmax(before_wns, clock_period)
            fmax_after = calculate_fmax(after_wns, clock_period)
            summary = f"hard_macro_move {best['primitive']} wns {before_wns:.3f}->{after_wns:.3f}"
            if fmax_before is not None and fmax_after is not None:
                summary += f" fmax {fmax_before:.1f}->{fmax_after:.1f} MHz"
            return SkillResult(
                success=True, before_wns=before_wns, after_wns=after_wns,
                delta_wns=after_wns - before_wns, is_legal=True,
                output_dcp=output_dcp, summary=summary,
                details={"attempts": attempts, "safety_candidate_count": len(frozen_targets),
                         "source_dcp_sha256": source_sha, "budget_stopped": budget_stopped,
                         "unstarted": unstarted},
                candidates=tuple(outputs),
            )
        except Exception as exc:
            logger.error("[hard_macro_move] failed: %s", exc)
            return SkillResult.failure(
                before_wns, str(exc), failed_dcp,
                details={"attempts": attempts, "safety_candidate_count": len(frozen_targets),
                         "budget_stopped": budget_stopped, "unstarted": unstarted},
            )
