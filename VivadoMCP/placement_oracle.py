"""Bounded transactional Vivado placement oracle for recurrence transport."""

from __future__ import annotations

import base64
import re
from typing import Any


MAX_SRL_CANDIDATES = 8192
MAX_CE_CLONE_CANDIDATES = 128
MAX_ADDRESSES = 4
MAX_PLACE_ATTEMPTS = 8320
MAX_ORACLE_TIMEOUT_S = 600.0
MAX_DSP_SLOT_CANDIDATES = 512
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_NET_RE = re.compile(r"^[A-Za-z0-9_./:+\-\[\]]{1,1024}$")
_SRL_SITE_RE = re.compile(r"^SLICE_X(?P<x>\d+)Y(?P<y>\d+)$")
_CLONE_SITE_RE = re.compile(r"^SLICE_X(?P<x>\d+)Y(?P<y>\d+)$")
_BEL_RE = re.compile(r"^[A-H]6LUT$")
_REGION_RE = re.compile(r"^(?:CLOCKREGION_)?X\d+Y\d+$")
_DSP_SITE_RE = re.compile(r"^DSP48E2_X(?P<x>\d+)Y(?P<y>\d+)$")


def _validate_identity(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _validate_net(value: Any, field: str) -> str:
    if not isinstance(value, str) or _NET_RE.fullmatch(value) is None:
        raise ValueError(f"{field} contains unsupported characters")
    return value


def _validate_slots(
    value: Any,
    *,
    field: str,
    site_pattern: re.Pattern[str],
    maximum: int,
) -> list[dict[str, str]]:
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise ValueError(f"{field} must contain 1..{maximum} slots")
    result = []
    seen = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != {
            "site", "bel", "source_distance", "reinjection_distance",
        }:
            raise ValueError(f"{field}[{index}] has an unsupported slot schema")
        site = item.get("site")
        bel = item.get("bel")
        if not isinstance(site, str) or site_pattern.fullmatch(site) is None:
            raise ValueError(f"{field}[{index}].site is invalid")
        if not isinstance(bel, str) or _BEL_RE.fullmatch(bel) is None:
            raise ValueError(f"{field}[{index}].bel is invalid")
        for distance_field in ("source_distance", "reinjection_distance"):
            distance = item.get(distance_field)
            if (
                isinstance(distance, bool)
                or not isinstance(distance, int)
                or not 0 <= distance <= 100000
            ):
                raise ValueError(f"{field}[{index}].{distance_field} is invalid")
        key = (site, bel)
        if key in seen:
            raise ValueError(f"{field} contains a duplicate site/BEL")
        seen.add(key)
        result.append({
            "site": site,
            "bel": bel,
            "source_distance": item["source_distance"],
            "reinjection_distance": item["reinjection_distance"],
        })
    return result


def validate_recurrence_oracle_request(request: Any) -> dict[str, Any]:
    expected = {
        "source_dcp_sha256", "region_sha256", "data_pin", "clock_pin",
        "ce_source_pin", "addresses", "incumbent_clock_regions",
        "srl_candidates", "ce_clone_candidates", "max_place_attempts", "timeout",
    }
    if not isinstance(request, dict) or set(request) != expected:
        raise ValueError("recurrence placement request has an unsupported schema")
    addresses = request.get("addresses")
    if (
        not isinstance(addresses, list)
        or not 1 <= len(addresses) <= MAX_ADDRESSES
        or any(isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 31 for item in addresses)
        or len(set(addresses)) != len(addresses)
    ):
        raise ValueError("addresses must contain 1..4 unique integers in 0..31")
    regions = request.get("incumbent_clock_regions")
    if (
        not isinstance(regions, list)
        or not 1 <= len(regions) <= 16
        or len(set(regions)) != len(regions)
        or any(not isinstance(item, str) or _REGION_RE.fullmatch(item) is None for item in regions)
    ):
        raise ValueError("incumbent_clock_regions is invalid")
    timeout = request.get("timeout")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= float(timeout) <= MAX_ORACLE_TIMEOUT_S:
        raise ValueError("timeout must be in 1..600 seconds")
    maximum = request.get("max_place_attempts")
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= MAX_PLACE_ATTEMPTS:
        raise ValueError(f"max_place_attempts must be in 1..{MAX_PLACE_ATTEMPTS}")
    srl = _validate_slots(
        request.get("srl_candidates"),
        field="srl_candidates",
        site_pattern=_SRL_SITE_RE,
        maximum=MAX_SRL_CANDIDATES,
    )
    clones = _validate_slots(
        request.get("ce_clone_candidates"),
        field="ce_clone_candidates",
        site_pattern=_CLONE_SITE_RE,
        maximum=MAX_CE_CLONE_CANDIDATES,
    )
    # SRLC32E address changes static connectivity, not BEL placement legality.
    # Probe one representative address; the final joint replay checks all emitted A pins.
    attempts = len(srl) + len(clones)
    if attempts > maximum:
        raise ValueError("requested placement attempts exceed max_place_attempts")
    return {
        "source_dcp_sha256": _validate_identity(request.get("source_dcp_sha256"), "source_dcp_sha256"),
        "region_sha256": _validate_identity(request.get("region_sha256"), "region_sha256"),
        "data_pin": _validate_net(request.get("data_pin"), "data_pin"),
        "clock_pin": _validate_net(request.get("clock_pin"), "clock_pin"),
        "ce_source_pin": _validate_net(request.get("ce_source_pin"), "ce_source_pin"),
        "addresses": list(addresses),
        "incumbent_clock_regions": [item.removeprefix("CLOCKREGION_") for item in regions],
        "srl_candidates": srl,
        "ce_clone_candidates": clones,
        "max_place_attempts": maximum,
        "timeout": float(timeout),
    }


def _tcl_list(items: list[str]) -> str:
    return "[list " + " ".join("{" + item + "}" for item in items) + "]"


def build_connected_recurrence_oracle_tcl(request: dict[str, Any]) -> str:
    value = validate_recurrence_oracle_request(request)
    srl_pairs = " ".join(
        "[list {%s} {%s}]" % (item["site"], item["bel"])
        for item in value["srl_candidates"]
    )
    clone_pairs = " ".join(
        "[list {%s} {%s}]" % (item["site"], item["bel"])
        for item in value["ce_clone_candidates"]
    )
    regions = _tcl_list(value["incumbent_clock_regions"])
    address = value["addresses"][0]
    address_connects = []
    for bit in range(5):
        static = "vcc" if (address >> bit) & 1 else "gnd"
        address_connects.append(
            f"connect_net -net ${{ns}}_{static}_net -objects "
            f"[get_pins -quiet [list \"${{ns}}_srl/A\\[{bit}\\]\"]]"
        )
    return f"""
proc fdagents_b64 {{value}} {{return [binary encode base64 -maxlen 0 "$value"]}}
proc fdagents_disconnect_addresses {{}} {{
  global ns
  for {{set bit 0}} {{$bit < 5}} {{incr bit}} {{
    set pin [get_pins -quiet [list "${{ns}}_srl/A\\[$bit\\]"]]
    set net [get_nets -quiet -of_objects $pin]
    if {{[llength $net] == 1}} {{catch {{disconnect_net -net $net -objects $pin}}}}
  }}
}}
proc fdagents_region {{site}} {{
  set regions [get_clock_regions -quiet -of_objects [get_sites -quiet [list $site]]]
  if {{[llength $regions] != 1}} {{return ""}}
  return [string map {{CLOCKREGION_ ""}} [get_property NAME $regions]]
}}
proc fdagents_has_clock_leaf {{site bel target_segments}} {{
  set wanted_half [expr {{[string first [string index $bel 0] "ABCD"] >= 0 ? 0 : 1}}]
  foreach cell [get_cells -quiet -of_objects [get_sites -quiet [list $site]]] {{
    set cell_bel [get_property -quiet BEL $cell]
    if {{$cell_bel eq ""}} {{continue}}
    set leaf_bel [lindex [split $cell_bel .] end]
    set letter [string index $leaf_bel 0]
    if {{![string match {{[A-H]*}} $leaf_bel]}} {{continue}}
    set cell_half [expr {{[string first $letter "ABCD"] >= 0 ? 0 : 1}}]
    if {{$cell_half != $wanted_half}} {{continue}}
    foreach pin [get_pins -quiet -of_objects $cell -filter {{DIRECTION == IN && (REF_PIN_NAME == C || REF_PIN_NAME == CLK)}}] {{
      foreach segment [get_nets -quiet -segments -of_objects $pin] {{
        if {{[lsearch -exact $target_segments $segment] >= 0}} {{return 1}}
      }}
    }}
  }}
  return 0
}}
set ns __fdagents_recur_oracle
if {{[llength [get_cells -quiet ${{ns}}*]] || [llength [get_nets -quiet ${{ns}}*]]}} {{error {{temporary oracle namespace exists}}}}
set data_pin [get_pins -quiet [list {{{value['data_pin']}}}]]
set clock_pin [get_pins -quiet [list {{{value['clock_pin']}}}]]
set ce_pin [get_pins -quiet [list {{{value['ce_source_pin']}}}]]
if {{[llength $data_pin] != 1}} {{error "oracle data pin missing or ambiguous: {{{value['data_pin']}}}"}}
if {{[llength $clock_pin] != 1}} {{error "oracle clock pin missing or ambiguous: {{{value['clock_pin']}}}"}}
if {{[llength $ce_pin] != 1}} {{error "oracle CE pin missing or ambiguous: {{{value['ce_source_pin']}}}"}}
set data_net [get_nets -quiet -of_objects $data_pin]
set clock_net [get_nets -quiet -of_objects $clock_pin]
set ce_net [get_nets -quiet -of_objects $ce_pin]
if {{[llength $data_net] != 1 || [llength $clock_net] != 1 || [llength $ce_net] != 1}} {{error {{oracle pin net missing or ambiguous}}}}
set target_clock [get_clocks -quiet -of_objects $clock_pin]
if {{[llength $target_clock] != 1}} {{error {{oracle target clock missing or ambiguous}}}}
set target_clock_segments [get_nets -quiet -segments -of_objects $clock_pin]
if {{[llength $target_clock_segments] == 0}} {{error {{oracle target clock segments are missing}}}}
set allowed_regions {regions}
set srl_pairs [list {srl_pairs}]
set clone_pairs [list {clone_pairs}]
create_cell -reference GND ${{ns}}_gnd
create_cell -reference VCC ${{ns}}_vcc
create_cell -reference LUT1 ${{ns}}_ce_clone
create_cell -reference SRLC32E ${{ns}}_srl
create_cell -reference LUT1 ${{ns}}_q_sink
set_property INIT 2'h2 [get_cells ${{ns}}_ce_clone]
set_property INIT 32'h00000000 [get_cells ${{ns}}_srl]
set_property INIT 2'h2 [get_cells ${{ns}}_q_sink]
create_net ${{ns}}_gnd_net
create_net ${{ns}}_vcc_net
create_net ${{ns}}_ce_out
create_net ${{ns}}_q
connect_net -net ${{ns}}_gnd_net -objects [get_pins ${{ns}}_gnd/G]
connect_net -net ${{ns}}_vcc_net -objects [get_pins ${{ns}}_vcc/P]
connect_net -hierarchical -net $ce_net -objects [get_pins ${{ns}}_ce_clone/I0]
connect_net -net ${{ns}}_ce_out -objects [list [get_pins ${{ns}}_ce_clone/O] [get_pins ${{ns}}_srl/CE]]
connect_net -hierarchical -net $data_net -objects [get_pins ${{ns}}_srl/D]
connect_net -hierarchical -net $clock_net -objects [get_pins ${{ns}}_srl/CLK]
connect_net -net ${{ns}}_q -objects [list [get_pins ${{ns}}_srl/Q] [get_pins ${{ns}}_q_sink/I0]]
set srl [get_cells ${{ns}}_srl]
set clone [get_cells ${{ns}}_ce_clone]
array set srl_ok {{}}
foreach pair $srl_pairs {{lassign $pair site bel; set srl_ok(${{site}}/${{bel}}) 1}}
array set diag {{srl_target 0 srl_type 0 srl_clock_leaf 0 srl_place 0 srl_region 0 clone_target 0 clone_place 0 clone_region 0}}
set first_srl_place_error ""
set first_clone_place_error ""
fdagents_disconnect_addresses
{"\n".join(address_connects)}
foreach pair $srl_pairs {{
  lassign $pair site bel
  set key ${{site}}/${{bel}}
  set target [get_bels -quiet [list $key]]
  set site_obj [get_sites -quiet [list $site]]
  if {{[llength $target] != 1}} {{
    incr diag(srl_target)
    set srl_ok($key) 0
    continue
  }}
  if {{[get_property SITE_TYPE $site_obj] ne "SLICEM"}} {{
    incr diag(srl_type)
    set srl_ok($key) 0
    continue
  }}
  set srl_leaf($key) [fdagents_has_clock_leaf $site $bel $target_clock_segments]
  if {{!$srl_leaf($key)}} {{incr diag(srl_clock_leaf)}}
  set place_rc [catch {{place_cell $srl $target}} place_msg]
  if {{$place_rc || [get_property -quiet LOC $srl] ne $site || ![string match "*.$bel" [get_property -quiet BEL $srl]]}} {{
    if {{$first_srl_place_error eq ""}} {{set first_srl_place_error "rc=$place_rc msg=$place_msg loc=[get_property -quiet LOC $srl] bel=[get_property -quiet BEL $srl] target=$key"}}
    incr diag(srl_place)
    set srl_ok($key) 0
  }}
  catch {{unplace_cell $srl}}
  if {{[get_property -quiet LOC $srl] ne ""}} {{error {{SRL oracle unplace failed}}}}
}}
foreach pair $clone_pairs {{
  lassign $pair site bel
  set key ${{site}}/${{bel}}
  set target [get_bels -quiet [list $key]]
  set ok 1
  if {{[llength $target] != 1}} {{set ok 0; incr diag(clone_target)}}
  if {{$ok}} {{
    set clone_rc [catch {{place_cell $clone $target}} clone_msg]
    if {{$clone_rc || [get_property -quiet LOC $clone] ne $site || ![string match "*.$bel" [get_property -quiet BEL $clone]]}} {{
      if {{$first_clone_place_error eq ""}} {{set first_clone_place_error "rc=$clone_rc msg=$clone_msg loc=[get_property -quiet LOC $clone] bel=[get_property -quiet BEL $clone] target=$key"}}
      set ok 0
      incr diag(clone_place)
    }}
  }}
  catch {{unplace_cell $clone}}
  if {{[get_property -quiet LOC $clone] ne ""}} {{error {{CE clone oracle unplace failed}}}}
  set region [fdagents_region $site]
  if {{$ok && $region ne ""}} {{
    puts "FDAGENTS_RECUR_CLONE|[fdagents_b64 $site]|$bel|$region"
    if {{[lsearch -exact $allowed_regions $region] < 0}} {{incr diag(clone_region)}}
  }} elseif {{$ok}} {{
    incr diag(clone_region)
  }}
}}
foreach pair $srl_pairs {{
  lassign $pair site bel
  set key ${{site}}/${{bel}}
  set region [fdagents_region $site]
  if {{$srl_ok($key) && $region ne ""}} {{
    puts "FDAGENTS_RECUR_SRL|[fdagents_b64 $site]|$bel|$region|$srl_leaf($key)"
    if {{[lsearch -exact $allowed_regions $region] < 0}} {{incr diag(srl_region)}}
  }} elseif {{$srl_ok($key)}} {{
    incr diag(srl_region)
  }}
}}
puts "FDAGENTS_RECUR_DIAG|$diag(srl_target)|$diag(srl_type)|$diag(srl_clock_leaf)|$diag(srl_place)|$diag(srl_region)|$diag(clone_target)|$diag(clone_place)|$diag(clone_region)"
puts "FDAGENTS_RECUR_DIAGMSG|[fdagents_b64 $first_srl_place_error]|[fdagents_b64 $first_clone_place_error]"
puts "FDAGENTS_RECUR_DONE|[llength $srl_pairs]|[llength $clone_pairs]"
""".strip()


def parse_connected_recurrence_oracle_output(
    output: str,
    request: dict[str, Any],
) -> dict[str, Any]:
    value = validate_recurrence_oracle_request(request)
    requested_srl = {
        (item["site"], item["bel"]): item for item in value["srl_candidates"]
    }
    requested_clones = {
        (item["site"], item["bel"]): item
        for item in value["ce_clone_candidates"]
    }
    regions = set(value["incumbent_clock_regions"])
    srl = []
    clones = []
    done = None
    diagnostics = None
    seen = set()
    for raw in str(output).splitlines():
        line = raw.strip().replace("\t", "|")
        if line.startswith("FDAGENTS_RECUR_SRL|") or line.startswith("FDAGENTS_RECUR_CLONE|"):
            parts = line.split("|")
            expected_length = 5 if line.startswith("FDAGENTS_RECUR_SRL|") else 4
            if len(parts) != expected_length:
                raise ValueError("oracle emitted a malformed slot record")
            kind, encoded_site, bel, region = parts[:4]
            site = base64.b64decode(encoded_site.encode("ascii"), validate=True).decode("utf-8")
            pattern = _SRL_SITE_RE if kind.endswith("_SRL") else _CLONE_SITE_RE
            match = pattern.fullmatch(site)
            requested = requested_srl if kind.endswith("_SRL") else requested_clones
            normalized_region = region.removeprefix("CLOCKREGION_")
            if (
                match is None
                or _BEL_RE.fullmatch(bel) is None
                or _REGION_RE.fullmatch(region) is None
                or (site, bel) not in requested
            ):
                raise ValueError("oracle emitted an unrequested or invalid slot")
            key = (kind, site, bel)
            if key in seen:
                raise ValueError("oracle emitted a duplicate slot")
            seen.add(key)
            record = {
                "kind": (
                    "connected_srlc32e_placement_oracle_slot"
                    if kind.endswith("_SRL")
                    else "connected_ce_clone_placement_oracle_slot"
                ),
                "version": 2,
                "source_dcp_sha256": value["source_dcp_sha256"],
                "region_sha256": value["region_sha256"],
                "site": site,
                "bel": bel,
                "clock_region": normalized_region,
                "x": int(match.group("x")),
                "y": int(match.group("y")),
                "measured_legal": True,
                "oracle": "vivado_connected_place_cell",
                "incumbent_clock_region": normalized_region in regions,
                "preexisting_clock_leaf": (
                    parts[4] == "1" if kind.endswith("_SRL") else False
                ),
                "source_distance": requested[(site, bel)]["source_distance"],
                "reinjection_distance": requested[(site, bel)][
                    "reinjection_distance"
                ],
            }
            (srl if kind.endswith("_SRL") else clones).append(record)
        elif line.startswith("FDAGENTS_RECUR_DONE|"):
            parts = line.split("|")
            if len(parts) != 3:
                raise ValueError("oracle completion marker is malformed")
            done = (int(parts[1]), int(parts[2]))
        elif line.startswith("FDAGENTS_RECUR_DIAG|"):
            parts = line.split("|")
            if len(parts) != 9:
                raise ValueError("oracle diagnostic marker is malformed")
            diagnostics = dict(zip(
                (
                    "srl_target_missing", "srl_wrong_site_type",
                    "srl_missing_clock_leaf", "srl_place_failed",
                    "srl_region_mismatch", "clone_target_missing",
                    "clone_place_failed", "clone_region_mismatch",
                ),
                (int(item) for item in parts[1:]),
            ))
        elif line.startswith("FDAGENTS_RECUR_DIAGMSG|"):
            parts = line.split("|")
            if len(parts) != 3:
                raise ValueError("oracle diagnostic message marker is malformed")
            diagnostics = diagnostics or {}
            diagnostics.update({
                "first_srl_place_error": base64.b64decode(
                    parts[1].encode("ascii"), validate=True
                ).decode("utf-8"),
                "first_clone_place_error": base64.b64decode(
                    parts[2].encode("ascii"), validate=True
                ).decode("utf-8"),
            })
    if done != (len(requested_srl), len(requested_clones)):
        raise ValueError("oracle did not complete every bounded placement attempt")
    return {
        "status": "success",
        "srl_slots": sorted(srl, key=lambda item: (item["site"], item["bel"])),
        "ce_clone_slots": sorted(clones, key=lambda item: (item["site"], item["bel"])),
        "attempted": {
            "srl": len(requested_srl),
            "ce_clone": len(requested_clones),
        },
        "diagnostics": diagnostics or {},
    }


def validate_dsp_oracle_request(request: Any) -> dict[str, Any]:
    """Strict schema for the transactional DSP48E2 slot oracle."""
    expected = {
        "source_dcp_sha256", "region_sha256", "clock_pin",
        "candidate_sites", "timeout",
    }
    if not isinstance(request, dict) or set(request) != expected:
        raise ValueError("DSP placement request has an unsupported schema")
    timeout = request.get("timeout")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 1 <= float(timeout) <= MAX_ORACLE_TIMEOUT_S
    ):
        raise ValueError("timeout must be in 1..600 seconds")
    sites = request.get("candidate_sites")
    if not isinstance(sites, list) or not 1 <= len(sites) <= MAX_DSP_SLOT_CANDIDATES:
        raise ValueError(
            f"candidate_sites must contain 1..{MAX_DSP_SLOT_CANDIDATES} slots"
        )
    normalized = []
    seen = set()
    for index, item in enumerate(sites):
        if not isinstance(item, dict) or set(item) != {"site", "distance"}:
            raise ValueError(f"candidate_sites[{index}] has an unsupported schema")
        site = item.get("site")
        if not isinstance(site, str) or _DSP_SITE_RE.fullmatch(site) is None:
            raise ValueError(f"candidate_sites[{index}].site is invalid")
        distance = item.get("distance")
        if (
            isinstance(distance, bool)
            or not isinstance(distance, int)
            or not 0 <= distance <= 100000
        ):
            raise ValueError(f"candidate_sites[{index}].distance is invalid")
        if site in seen:
            raise ValueError("candidate_sites contains a duplicate site")
        seen.add(site)
        normalized.append({"site": site, "distance": distance})
    return {
        "source_dcp_sha256": _validate_identity(
            request.get("source_dcp_sha256"), "source_dcp_sha256"
        ),
        "region_sha256": _validate_identity(
            request.get("region_sha256"), "region_sha256"
        ),
        "clock_pin": _validate_net(request.get("clock_pin"), "clock_pin"),
        "candidate_sites": normalized,
        "timeout": float(timeout),
    }


def build_connected_dsp_oracle_tcl(request: dict[str, Any]) -> str:
    """Fixed transactional probe: place one connected temporary DSP48E2."""
    value = validate_dsp_oracle_request(request)
    site_list = _tcl_list([item["site"] for item in value["candidate_sites"]])
    return f"""
proc fdagents_dsp_b64 {{value}} {{return [binary encode base64 -maxlen 0 "$value"]}}
proc fdagents_dsp_region {{site}} {{
  set regions [get_clock_regions -quiet -of_objects [get_sites -quiet [list $site]]]
  if {{[llength $regions] != 1}} {{return ""}}
  return [string map {{CLOCKREGION_ ""}} [get_property NAME $regions]]
}}
set ns __fdagents_fixedpoint_oracle
if {{[llength [get_cells -quiet ${{ns}}*]] || [llength [get_nets -quiet ${{ns}}*]]}} {{error {{temporary oracle namespace exists}}}}
set clock_pin [get_pins -quiet [list {{{value['clock_pin']}}}]]
if {{[llength $clock_pin] != 1}} {{error "oracle clock pin missing or ambiguous: {{{value['clock_pin']}}}"}}
set clock_net [get_nets -quiet -of_objects $clock_pin]
if {{[llength $clock_net] != 1}} {{error {{oracle clock pin net missing or ambiguous}}}}
set clock_regions [list]
foreach region [get_clock_regions -quiet -of_objects [get_sites -quiet -of_objects [get_cells -quiet -of_objects $clock_net -filter {{IS_PRIMITIVE}}]]] {{
  lappend clock_regions [string map {{CLOCKREGION_ ""}} [get_property NAME $region]]
}}
set clock_regions [lsort -unique $clock_regions]
set candidate_sites {site_list}
create_cell -reference DSP48E2 ${{ns}}_dsp
connect_net -hierarchical -net $clock_net -objects [get_pins ${{ns}}_dsp/CLK]
set dsp [get_cells ${{ns}}_dsp]
foreach site $candidate_sites {{
  set target [get_sites -quiet [list $site]]
  set ok 1
  if {{[llength $target] != 1}} {{set ok 0}}
  if {{$ok && [get_property -quiet SITE_TYPE $target] ne "DSP48E2"}} {{set ok 0}}
  if {{$ok}} {{
    set place_rc [catch {{place_cell $dsp $target}} place_msg]
    if {{$place_rc || [get_property -quiet LOC $dsp] ne $site}} {{set ok 0}}
  }}
  catch {{unplace_cell $dsp}}
  if {{[get_property -quiet LOC $dsp] ne ""}} {{error {{DSP oracle unplace failed}}}}
  if {{$ok}} {{
    set region [fdagents_dsp_region $site]
    set clock_ok [expr {{$region ne "" && [lsearch -exact $clock_regions $region] >= 0 ? 1 : 0}}]
    puts "FDAGENTS_FIXEDPOINT_DSP|[fdagents_dsp_b64 $site]|DSP48E2|$clock_ok"
  }}
}}
remove_cell $dsp
if {{[llength [get_cells -quiet ${{ns}}*]]}} {{error {{DSP oracle namespace cleanup failed}}}}
puts "FDAGENTS_FIXEDPOINT_DSP_DONE|[llength $candidate_sites]"
""".strip()


def parse_connected_dsp_oracle_output(
    output: str,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Validate the oracle transcript against the exact requested inventory."""
    value = validate_dsp_oracle_request(request)
    requested = {item["site"]: item for item in value["candidate_sites"]}
    slots = []
    done = None
    seen = set()
    for raw in str(output).splitlines():
        line = raw.strip().replace("\t", "|")
        if line.startswith("FDAGENTS_FIXEDPOINT_DSP|"):
            parts = line.split("|")
            if len(parts) != 4:
                raise ValueError("DSP oracle emitted a malformed slot record")
            _, encoded_site, bel, clock_ok = parts
            site = base64.b64decode(
                encoded_site.encode("ascii"), validate=True
            ).decode("utf-8")
            if (
                _DSP_SITE_RE.fullmatch(site) is None
                or bel != "DSP48E2"
                or clock_ok not in {"0", "1"}
                or site not in requested
            ):
                raise ValueError("DSP oracle emitted an unrequested or invalid slot")
            if site in seen:
                raise ValueError("DSP oracle emitted a duplicate slot")
            seen.add(site)
            slots.append({
                "site": site,
                "bel": "DSP48E2",
                "distance": requested[site]["distance"],
                "clock_route_ok": clock_ok == "1",
                "source_dcp_sha256": value["source_dcp_sha256"],
                "region_sha256": value["region_sha256"],
            })
        elif line.startswith("FDAGENTS_FIXEDPOINT_DSP_DONE|"):
            parts = line.split("|")
            if len(parts) != 2:
                raise ValueError("DSP oracle completion marker is malformed")
            done = int(parts[1])
    if done != len(requested):
        raise ValueError("DSP oracle did not complete every bounded placement attempt")
    return {
        "status": "success",
        "slots": sorted(slots, key=lambda item: item["site"]),
        "attempted": len(requested),
    }
