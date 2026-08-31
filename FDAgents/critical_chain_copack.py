"""Generic, hash-bound critical LUT-chain co-packing tools.

The miner consumes timing/placement facts only.  Cell names are opaque
identities: selection uses primitive type, path order, timing, location, BEL
occupancy, and connectivity implied by the timing path.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


ACTION_CLASS = "critical_chain_copack_v1"
SCHEMA_VERSION = 1
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SLICE = re.compile(r"SLICE_X(?P<x>\d+)Y(?P<y>\d+)")
_LUT_BEL = re.compile(r"(?:[^.]+\.)?(?P<letter>[A-H])[56]LUT")
_MOVABLE_REFS = {"LUT6_2"}
_BEL_ORDER = ("H6LUT", "G6LUT", "F6LUT", "E6LUT", "D6LUT", "C6LUT", "B6LUT", "A6LUT")


class CopackError(ValueError):
    """Typed fail-closed co-pack error."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _stable(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_stable(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(_stable(value), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _xy(site: str) -> tuple[int, int]:
    match = _SLICE.fullmatch(str(site))
    if match is None:
        raise CopackError("COPACK_INVALID_SLICE", f"invalid SLICE location {site!r}")
    return int(match["x"]), int(match["y"])


def _distance(left: str, right: str) -> int:
    lx, ly = _xy(left)
    rx, ry = _xy(right)
    return abs(lx - rx) + abs(ly - ry)


def _read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def load_profile(paths_tsv: Path, cells_tsv: Path, sites_tsv: Path) -> dict[str, Any]:
    paths = _read_tsv(paths_tsv)
    cell_rows = _read_tsv(cells_tsv)
    site_rows = _read_tsv(sites_tsv)
    if not paths or not cell_rows or not site_rows:
        raise CopackError("COPACK_PROFILE_INCOMPLETE", "profile TSVs are incomplete")
    by_path: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in cell_rows:
        by_path[int(row["path_rank"])].append({
            "ordinal": int(row["ordinal"]),
            "cell": row["cell"],
            "owner": row["owner"],
            "ref": row["ref"],
            "owner_ref": row["owner_ref"],
            "loc": row["loc"],
            "bel": row["bel"],
        })
    normalized_paths = []
    for row in paths:
        rank = int(row["rank"])
        normalized_paths.append({
            "rank": rank,
            "slack_ns": float(row["slack_ns"]),
            "datapath_delay_ns": float(row["datapath_delay_ns"]),
            "logic_delay_ns": float(row.get("logic_delay_ns") or 0.0),
            "route_delay_ns": float(row.get("route_delay_ns") or 0.0),
            "startpoint": row["startpoint"],
            "endpoint": row["endpoint"],
            "cells": sorted(by_path[rank], key=lambda item: item["ordinal"]),
        })
    sites: dict[str, dict[str, Any]] = {}
    for row in site_rows:
        site = row["site"]
        entry = sites.setdefault(site, {"site": site, "site_type": row["site_type"], "occupants": []})
        if row["cell"]:
            entry["occupants"].append({
                "cell": row["cell"], "ref": row["ref"], "bel": row["bel"]
            })
    edge_path = sites_tsv.parent / "lut_edges.tsv"
    edges = []
    if edge_path.is_file():
        edges = [
            {
                "source": row["source"], "source_loc": row["source_loc"],
                "sink": row["sink"], "sink_loc": row["sink_loc"], "net": row["net"],
            }
            for row in _read_tsv(edge_path)
        ]
    return {"paths": normalized_paths, "sites": sites, "edges": edges}


def _deduplicated_cells(path: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    seen_last = None
    for row in path["cells"]:
        owner = row["owner"] or row["cell"]
        if owner == seen_last:
            continue
        seen_last = owner
        result.append({**row, "owner": owner})
    return result


def mine_critical_chains(
    profile: dict[str, Any],
    *,
    source_dcp_sha256: str,
    slack_window_ns: float = 0.100,
    min_lut_stages: int = 3,
) -> dict[str, Any]:
    """Mine anonymous spread LUT chains from near-critical timing paths."""
    if _SHA256.fullmatch(source_dcp_sha256) is None:
        raise CopackError("COPACK_SOURCE_SHA_INVALID", "source SHA256 is malformed")
    paths = list(profile.get("paths") or [])
    if not paths:
        raise CopackError("COPACK_NO_TIMING_PATHS", "timing profile has no paths")
    worst = min(float(path["slack_ns"]) for path in paths)
    descriptors: dict[tuple[str, ...], dict[str, Any]] = {}
    for path in sorted(paths, key=lambda item: (item["slack_ns"], item["rank"])):
        if float(path["slack_ns"]) > worst + slack_window_ns:
            continue
        cells = _deduplicated_cells(path)
        run: list[dict[str, Any]] = []

        def retain(candidate: list[dict[str, Any]]) -> None:
            if len(candidate) < min_lut_stages:
                return
            sites = [item["loc"] for item in candidate]
            if any(_SLICE.fullmatch(site) is None for site in sites):
                return
            distinct = list(dict.fromkeys(sites))
            edge_distance = sum(_distance(a, b) for a, b in zip(sites, sites[1:]))
            if len(distinct) < 2 or edge_distance < 2:
                return
            key = tuple(item["owner"] for item in candidate)
            if key in descriptors:
                descriptors[key]["path_ranks"].append(path["rank"])
                return
            first_index = cells.index(candidate[0])
            last_index = cells.index(candidate[-1])
            source = cells[first_index - 1] if first_index else None
            sink = cells[last_index + 1] if last_index + 1 < len(cells) else None
            route_delay = max(0.0, float(path.get("route_delay_ns") or 0.0))
            datapath = max(1e-12, float(path["datapath_delay_ns"]))
            route_share = route_delay / datapath if route_delay else 0.0
            estimated = max(
                0.001,
                min(
                    route_delay * 0.25 if route_delay else 0.250,
                    0.030 * edge_distance + 0.010 * (len(distinct) - 1),
                ),
            )
            descriptor = {
                "chain_id": "chain:" + _digest({
                    "source": source_dcp_sha256, "cells": key, "path": path["rank"]
                })[:20],
                "path_ranks": [path["rank"]],
                "worst_slack_ns": float(path["slack_ns"]),
                "datapath_delay_ns": datapath,
                "route_delay_ns": route_delay,
                "route_delay_share": route_share,
                "spread_metric": {
                    "distinct_slice_count": len(distinct),
                    "ordered_manhattan_distance": edge_distance,
                },
                "estimated_recovery_ns": estimated,
                "source_context": (
                    None if source is None else {
                        "cell": source["owner"], "ref": source["owner_ref"], "loc": source["loc"]
                    }
                ),
                "sink_context": (
                    None if sink is None else {
                        "cell": sink["owner"], "ref": sink["owner_ref"], "loc": sink["loc"]
                    }
                ),
                "stages": [
                    {
                        "role": f"lut_stage_{index}",
                        "cell": item["owner"],
                        "ref": item["owner_ref"],
                        "loc": item["loc"],
                        "bel": item["bel"],
                    }
                    for index, item in enumerate(candidate)
                ],
            }
            descriptors[key] = descriptor

        for cell in cells:
            if cell["owner_ref"] in _MOVABLE_REFS and _SLICE.fullmatch(cell["loc"] or ""):
                run.append(cell)
            else:
                # A LUT immediately colocated with the following CARRY8 is its
                # carry-input generator, not a spread compressor stage. Keep it
                # as sink context so co-packing cannot break carry alignment.
                if (
                    cell["owner_ref"] == "CARRY8"
                    and run
                    and run[-1]["loc"] == cell["loc"]
                ):
                    retain(run[:-1])
                else:
                    retain(run)
                run = []
        retain(run)
    rows = sorted(
        descriptors.values(),
        key=lambda item: (item["worst_slack_ns"], -item["estimated_recovery_ns"], item["chain_id"]),
    )
    result = {
        "schema_version": SCHEMA_VERSION,
        "action_class": ACTION_CLASS,
        "source_dcp_sha256": source_dcp_sha256,
        "status": "success" if rows else "rejected",
        "failure_reason": None if rows else "COPACK_NO_SPREAD_NEAR_CRITICAL_CHAIN",
        "chain_count": len(rows),
        "chains": rows,
    }
    result["miner_digest"] = _digest(result)
    return result


def _packed_sites(profile: dict[str, Any]) -> list[dict[str, Any]]:
    packs = []
    directed = set((edge["source"], edge["sink"]) for edge in profile.get("edges") or [])
    for site, row in sorted((profile.get("sites") or {}).items()):
        owners = {
            item["cell"] for item in row["occupants"] if item["ref"] == "LUT6_2"
        }
        outgoing: dict[str, set[str]] = {owner: set() for owner in owners}
        for source, sink in directed:
            if source in owners and sink in owners:
                outgoing[source].add(sink)
        chains = set()
        for first in owners:
            for second in outgoing[first]:
                for third in outgoing[second] - {first}:
                    chains.add((first, second, third))
        for chain in sorted(chains):
            packs.append({
                "pack_id": "pack:" + _digest({"site": site, "owners": chain})[:20],
                "site": site,
                "cells": list(chain),
                "structural_rule": "three_stage_directed_colocated_lut6_2_chain",
            })
    return packs


def enumerate_copack_candidates(
    profile: dict[str, Any],
    mining: dict[str, Any],
    *,
    max_chains: int = 2,
    excluded_sites: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Return deterministic, non-authoritative candidate-site proposals."""
    excluded = set(excluded_sites)
    sites = profile.get("sites") or {}
    packed = _packed_sites(profile)
    preserved_cells = {cell for pack in packed for cell in pack["cells"]}
    proposals = []
    for chain in mining.get("chains", [])[:max_chains]:
        stages = chain["stages"]
        moved = {item["cell"] for item in stages}
        if moved & preserved_cells:
            continue
        source_loc = (chain.get("source_context") or {}).get("loc")
        sink_loc = (chain.get("sink_context") or {}).get("loc")
        current_sites = [item["loc"] for item in stages]
        current_proxy = sum(_distance(a, b) for a, b in zip(current_sites, current_sites[1:]))
        if source_loc and _SLICE.fullmatch(source_loc):
            current_proxy += _distance(source_loc, current_sites[0])
        if sink_loc and _SLICE.fullmatch(sink_loc):
            current_proxy += _distance(current_sites[-1], sink_loc)
        for site, site_row in sites.items():
            if site in excluded or _SLICE.fullmatch(site) is None:
                continue
            if min(_distance(site, current) for current in current_sites) > 4:
                continue
            hard_site_refs = {"CARRY8", "MUXF7", "MUXF8", "MUXF9"}
            if any(item["ref"] in hard_site_refs for item in site_row["occupants"]):
                continue
            occupied_letters = {}
            for item in site_row["occupants"]:
                match = _LUT_BEL.fullmatch(item["bel"] or "")
                if match is not None and item["cell"] not in moved:
                    occupied_letters[match["letter"] + "6LUT"] = item["cell"]
            free = [bel for bel in _BEL_ORDER if bel not in occupied_letters]
            if len(free) < len(stages):
                continue
            candidate_proxy = 0.15 * (len(stages) - 1)
            if source_loc and _SLICE.fullmatch(source_loc):
                candidate_proxy += _distance(source_loc, site)
            if sink_loc and _SLICE.fullmatch(sink_loc):
                candidate_proxy += _distance(site, sink_loc)
            estimate = 0.030 * (current_proxy - candidate_proxy) + 0.020 * (len(set(current_sites)) - 1)
            proposals.append({
                "chain_id": chain["chain_id"],
                "site": site,
                "bels": free[:len(stages)],
                "score": estimate,
                "distance_proxy_before": current_proxy,
                "distance_proxy_after": candidate_proxy,
                "assignments": [
                    {
                        "role": stage["role"], "cell": stage["cell"],
                        "source_loc": stage["loc"], "source_bel": stage["bel"],
                        "destination": f"{site}/{bel}",
                    }
                    for stage, bel in zip(stages, free)
                ],
            })
    return sorted(proposals, key=lambda item: (-item["score"], item["site"], item["bels"]))


def _verified_routability(payload: dict[str, Any] | None, mining: dict[str, Any]) -> tuple[dict[tuple[str, tuple[str, ...]], dict[str, Any]], str | None]:
    if payload is None:
        return {}, None
    supplied_digest = payload.get("precheck_digest")
    content = dict(payload)
    content.pop("precheck_digest", None)
    if (
        payload.get("kind") != "critical_chain_copack_route_precheck"
        or payload.get("status") != "complete"
        or payload.get("source_dcp_sha256") != mining.get("source_dcp_sha256")
        or payload.get("miner_digest") != mining.get("miner_digest")
        or supplied_digest != _digest(content)
    ):
        raise CopackError("COPACK_ROUTABILITY_PROOF_INVALID", "route precheck is not digest/source/miner bound")
    rows = {}
    for row in payload.get("candidates") or []:
        key = (row["site"], tuple(row["bels"]))
        if key in rows:
            raise CopackError("COPACK_ROUTABILITY_PROOF_INVALID", "route precheck repeats a candidate")
        rows[key] = row
    return rows, supplied_digest


def allocate_copack_manifest(
    profile: dict[str, Any],
    mining: dict[str, Any],
    *,
    attempt_index: int = 1,
    max_chains: int = 2,
    candidate_rank: int | None = None,
    excluded_sites: Iterable[str] = (),
    routability: dict[str, Any] | None = None,
    require_routability: bool = False,
) -> dict[str, Any]:
    """Allocate conflict-free same-slice packs while preserving dense packs."""
    if mining.get("status") != "success" or not mining.get("chains"):
        raise CopackError("COPACK_NOTHING_TO_ALLOCATE", "miner produced no eligible chains")
    if attempt_index < 1 or (candidate_rank is not None and candidate_rank < 0):
        raise CopackError("COPACK_ATTEMPT_INVALID", "attempt and candidate rank must be nonnegative")
    sites = profile.get("sites") or {}
    packed = _packed_sites(profile)
    preserved_cells = {cell for pack in packed for cell in pack["cells"]}
    verified_routes, precheck_digest = _verified_routability(routability, mining)
    proposals = enumerate_copack_candidates(
        profile, mining, max_chains=max_chains, excluded_sites=excluded_sites
    )
    selected = []
    rejected = []
    for chain in mining["chains"][:max_chains]:
        stages = chain["stages"]
        moved = {item["cell"] for item in stages}
        if moved & preserved_cells:
            # A spread chain may touch one member of a dense pack. Moving it
            # would destroy a structurally detected prior pack.
            rejected.append({
                "chain_id": chain["chain_id"],
                "reason_code": "COPACK_PRESERVATION_VIOLATION",
            })
            continue
        candidates = [item for item in proposals if item["chain_id"] == chain["chain_id"]]
        admitted = []
        for candidate in candidates:
            proof = verified_routes.get((candidate["site"], tuple(candidate["bels"])))
            if require_routability and (proof is None or proof.get("status") != "passed"):
                rejected.append({
                    "chain_id": chain["chain_id"], "site": candidate["site"],
                    "reason_code": "COPACK_EGRESS_PRECHECK_BLOCKED" if proof else "COPACK_EGRESS_PRECHECK_MISSING",
                })
                continue
            admitted.append({**candidate, "routability": proof})
        rank = candidate_rank if candidate_rank is not None else attempt_index - 1
        if rank >= len(admitted):
            rejected.append({
                "chain_id": chain["chain_id"], "reason_code": "COPACK_OCCUPANCY_CONFLICT"
            })
            continue
        candidate = admitted[rank]
        if candidate["score"] <= 0:
            rejected.append({
                "chain_id": chain["chain_id"], "reason_code": "COPACK_NO_POSITIVE_RECOVERY",
                "best_estimated_recovery_ns": candidate["score"],
            })
            continue
        selected.append({
            "chain_id": chain["chain_id"],
            "estimated_recovery_ns": candidate["score"],
            "assignments": candidate["assignments"],
            "distance_proxy_before": candidate["distance_proxy_before"],
            "distance_proxy_after": candidate["distance_proxy_after"],
            "routability_precheck": candidate["routability"],
        })
    if not selected:
        code = rejected[0]["reason_code"] if rejected else "COPACK_ALLOCATION_EMPTY"
        raise CopackError(code, "no chain admitted a conflict-free positive allocation")
    result = {
        "schema_version": SCHEMA_VERSION,
        "action_class": ACTION_CLASS,
        "source_dcp_sha256": mining["source_dcp_sha256"],
        "miner_digest": mining["miner_digest"],
        "attempt_index": attempt_index,
        "status": "allocated",
        "selected_chains": selected,
        "rejected_chains": rejected,
        "preserved_packs": packed,
        "routability_precheck_digest": precheck_digest,
        "boundary_policy": {
            "moved_cells_only": True,
            "incident_nets_only": True,
            "freeze_remainder": True,
            "nonlocal_change_audit_required": True,
        },
    }
    result["manifest_digest"] = _digest(result)
    return result


def render_manifest_tcl(manifest: dict[str, Any]) -> str:
    """Emit hash/source-bound incremental placement Tcl."""
    if manifest.get("status") != "allocated" or _SHA256.fullmatch(
        str(manifest.get("source_dcp_sha256") or "")
    ) is None:
        raise CopackError("COPACK_MANIFEST_INVALID", "manifest is not allocated/hash-bound")
    assignments = [
        item for chain in manifest["selected_chains"] for item in chain["assignments"]
    ]
    if len({item["cell"] for item in assignments}) != len(assignments):
        raise CopackError("COPACK_DUPLICATE_CELL", "manifest moves a cell more than once")
    if len({item["destination"] for item in assignments}) != len(assignments):
        raise CopackError("COPACK_DUPLICATE_BEL", "manifest assigns one BEL more than once")
    q = lambda value: "{" + str(value).replace("}", "\\}") + "}"
    names = " ".join(q(item["cell"]) for item in assignments)
    destinations = " ".join(
        f"{q(item['cell'])} {q(item['destination'])}" for item in assignments
    )
    expected = " ".join(
        f"{q(item['cell'])} [list {q(item['source_loc'])} {q(item['source_bel'])}]"
        for item in assignments
    )
    exclusions = " && ".join(f"NAME !~ {item['cell']}*" for item in assignments)
    return f'''if {{$argc != 2}} {{ error "usage: copack.tcl <parent.dcp> <output_dir>" }}
set parent [file normalize [lindex $argv 0]]
set output_dir [file normalize [lindex $argv 1]]
file mkdir $output_dir
set expected_sha {q(manifest['source_dcp_sha256'])}
set actual_sha [lindex [split [exec sha256sum -- $parent]] 0]
if {{$actual_sha ne $expected_sha}} {{ error "COPACK source SHA mismatch: $actual_sha" }}
open_checkpoint $parent
set destination_map [dict create {destinations}]
set expected_sources [dict create {expected}]
set moved_cells [get_cells -quiet [list {names}]]
if {{[llength $moved_cells] != {len(assignments)}}} {{ error "COPACK cell binding failed" }}
foreach cell $moved_cells {{
  set name [get_property NAME $cell]
  set source [dict get $expected_sources $name]
  if {{[get_property REF_NAME $cell] ne "LUT6_2" || [get_property LOC $cell] ne [lindex $source 0] || [get_property BEL $cell] ne [lindex $source 1]}} {{ error "COPACK source placement mismatch: $name" }}
}}
set incident_nets {{}}
foreach cell $moved_cells {{
  foreach net [get_nets -quiet -of_objects [get_pins -of_objects $cell]] {{
    if {{[lsearch -exact $incident_nets $net] < 0}} {{ lappend incident_nets $net }}
  }}
}}
if {{![llength $incident_nets]}} {{ error "COPACK incident closure empty" }}
set fp [open [file join $output_dir incident_nets.tsv] w]
foreach net [lsort $incident_nets] {{ puts $fp $net }}
close $fp
proc write_boundary_snapshot {{path incident_nets}} {{
  set fp [open $path w]
  foreach cell [lsort [get_cells -hierarchical -filter {{LOC != "" && {exclusions}}}]] {{
    puts $fp "C\t$cell\t[get_property LOC $cell]|[get_property BEL $cell]"
  }}
  foreach net [lsort [get_nets -hierarchical]] {{
    if {{[lsearch -exact $incident_nets $net] >= 0}} {{ continue }}
    # ROUTE values can contain embedded newlines and can be very large.  Keep
    # the audit snapshot bounded while retaining two checksums and byte count.
    set route [get_property ROUTE $net]
    set signature "[string bytelength $route]:[format %08x [zlib crc32 $route]]:[format %08x [zlib adler32 $route]]"
    puts $fp "N\t$net\t$signature"
  }}
  close $fp
}}
write_boundary_snapshot [file join $output_dir boundary_before.tsv] $incident_nets
set preserved_leaf [get_cells -hierarchical -filter {{IS_PRIMITIVE == 1 && LOC != "" && PRIMITIVE_LEVEL == LEAF && {exclusions}}}]
set preserved_macro [get_cells -hierarchical -filter {{IS_PRIMITIVE == 1 && LOC != "" && PRIMITIVE_LEVEL == MACRO && {exclusions}}}]
set_property IS_LOC_FIXED true $preserved_leaf
set_property IS_BEL_FIXED true $preserved_leaf
set_property IS_LOC_FIXED true $preserved_macro
set_property IS_BEL_FIXED true $preserved_macro
set_property IS_LOC_FIXED false $moved_cells
set_property IS_BEL_FIXED false $moved_cells
route_design -unroute -nets $incident_nets
unplace_cell $moved_cells
foreach cell $moved_cells {{
  set name [get_property NAME $cell]
  set destination [dict get $destination_map $name]
  if {{[llength [get_cells -quiet -of_objects [get_bels $destination]]]}} {{ error "COPACK destination occupied: $destination" }}
  place_cell $cell [get_bels $destination]
  set_property IS_LOC_FIXED true $cell
  set_property IS_BEL_FIXED true $cell
}}
set recovery_log [open [file join $output_dir route_recovery.tsv] w]
puts $recovery_log "phase\tstatus\tnets"
if {{[catch {{route_design -nets $incident_nets}} initial_error]}} {{
  set failed_nets {{}}
  foreach net $incident_nets {{
    if {{[get_property ROUTE_STATUS $net] ne "ROUTED"}} {{ lappend failed_nets $net }}
  }}
  puts $recovery_log "initial\tfailed\t[join [lsort $failed_nets] ,]"
  if {{![llength $failed_nets]}} {{ error "COPACK route failed without identifiable incident net: $initial_error" }}
  # Expanded effort: rip up only failed incident nets and retry them directly.
  route_design -unroute -nets $failed_nets
  if {{[catch {{route_design -nets $failed_nets}} retry_error]}} {{
    puts $recovery_log "failed_only_retry\tfailed\t[join [lsort $failed_nets] ,]"
    # Bounded detour fallback: rip up only the manifest closure, route the
    # stubborn nets first, then route the remaining incident nets around them.
    route_design -unroute -nets $incident_nets
    set remaining_nets {{}}
    foreach net $incident_nets {{
      if {{[lsearch -exact $failed_nets $net] < 0}} {{ lappend remaining_nets $net }}
    }}
    if {{[catch {{route_design -nets $failed_nets}} detour_error]}} {{
      close $recovery_log
      error "COPACK bounded stubborn-net route failed: $detour_error"
    }}
    if {{[llength $remaining_nets] && [catch {{route_design -nets $remaining_nets}} remainder_error]}} {{
      close $recovery_log
      error "COPACK bounded incident remainder route failed: $remainder_error"
    }}
    puts $recovery_log "stubborn_first_detour\tpassed\t[join [lsort $failed_nets] ,]"
  }} else {{
    puts $recovery_log "failed_only_retry\tpassed\t[join [lsort $failed_nets] ,]"
  }}
}} else {{
  puts $recovery_log "initial\tpassed\t[join [lsort $incident_nets] ,]"
}}
close $recovery_log
write_boundary_snapshot [file join $output_dir boundary_after.tsv] $incident_nets
set paths [get_timing_paths -max_paths 300 -setup]
set worst [lindex $paths 0]
set wns [get_property SLACK $worst]
report_timing_summary -delay_type min_max -max_paths 100 -file [file join $output_dir timing_summary.rpt]
report_timing -delay_type max -max_paths 300 -input_pins -file [file join $output_dir timing_paths.rpt]
report_route_status -file [file join $output_dir route_status.rpt]
report_drc -file [file join $output_dir drc.rpt]
write_checkpoint -force [file join $output_dir candidate.dcp]
set candidate_sha [lindex [split [exec sha256sum -- [file join $output_dir candidate.dcp]]] 0]
set fp [open [file join $output_dir result.txt] w]
puts $fp "action_class={ACTION_CLASS}"
puts $fp "manifest_digest={manifest['manifest_digest']}"
puts $fp "parent_sha256=$actual_sha"
puts $fp "candidate_sha256=$candidate_sha"
puts $fp "moved_cells=[llength $moved_cells]"
puts $fp "incident_nets=[llength $incident_nets]"
puts $fp "wns=$wns"
puts $fp "fmax_mhz=[expr {{1000.0 / (2.0 - $wns)}}]"
close $fp
exit
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    mine = sub.add_parser("mine")
    mine.add_argument("paths", type=Path)
    mine.add_argument("cells", type=Path)
    mine.add_argument("sites", type=Path)
    mine.add_argument("source_sha256")
    mine.add_argument("output", type=Path)
    allocate = sub.add_parser("allocate")
    allocate.add_argument("paths", type=Path)
    allocate.add_argument("cells", type=Path)
    allocate.add_argument("sites", type=Path)
    allocate.add_argument("mining", type=Path)
    allocate.add_argument("output", type=Path)
    allocate.add_argument("--attempt", type=int, default=1)
    allocate.add_argument("--candidate-rank", type=int)
    allocate.add_argument("--exclude-site", action="append", default=[])
    allocate.add_argument("--routability", type=Path)
    allocate.add_argument("--require-routability", action="store_true")
    emit = sub.add_parser("emit")
    emit.add_argument("manifest", type=Path)
    emit.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "mine":
            profile = load_profile(args.paths, args.cells, args.sites)
            result = mine_critical_chains(profile, source_dcp_sha256=args.source_sha256)
            args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            return 0 if result["status"] == "success" else 3
        if args.command == "allocate":
            profile = load_profile(args.paths, args.cells, args.sites)
            mining = json.loads(args.mining.read_text(encoding="utf-8"))
            route_proof = (
                json.loads(args.routability.read_text(encoding="utf-8"))
                if args.routability else None
            )
            result = allocate_copack_manifest(
                profile, mining, attempt_index=args.attempt,
                candidate_rank=args.candidate_rank,
                excluded_sites=args.exclude_site,
                routability=route_proof,
                require_routability=args.require_routability,
            )
            args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            return 0
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        args.output.write_text(render_manifest_tcl(manifest), encoding="utf-8")
        return 0
    except CopackError as exc:
        print(json.dumps({"status": "rejected", "reason_code": exc.reason_code, "message": str(exc)}, sort_keys=True))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
