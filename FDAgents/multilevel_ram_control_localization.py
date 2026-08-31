"""Recover and localize complete same-cycle RAM-control Boolean DAGs."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .multilevel_control_localization import (
    build_multilevel_control_localization_tcl,
)


ACTION_TARGET = "multilevel_ram_control_distribution_localization_v1"
CONTRACT_KIND = "multilevel_ram_control_distribution_contract"
OPERATOR_CLASS = "MULTILEVEL_RAM_CONTROL_DISTRIBUTION"
PROOF_TEMPLATE = "MULTILEVEL_RAM_CONTROL_DAG_CLONE_V1"
FAMILY_REF = "multilevel_ram_control_1"
_RUN_ID_RE = re.compile(r"nr-[0-9a-f]{32}")


class RamControlLocalizationError(ValueError):
    """Raised when a RAM-control closure cannot safely authorize mutation."""


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")).hexdigest()


def _depth(
    output: str,
    by_output: dict[str, dict[str, Any]],
    cache: dict[str, int],
    active: set[str] | None = None,
) -> int:
    if output in cache:
        return cache[output]
    row = by_output.get(output)
    if row is None:
        return 0
    active = set(active or ())
    if output in active:
        raise RamControlLocalizationError("cycle in RAM-control LUT closure")
    active.add(output)
    value = 1 + max(
        (_depth(source, by_output, cache, active) for source in row["inputs"].values()),
        default=0,
    )
    cache[output] = value
    return value


def _parse_properties(value: str) -> dict[str, str]:
    properties: dict[str, str] = {}
    for item in filter(None, value.split(";")):
        if "=" not in item:
            raise RamControlLocalizationError("RAM semantic property is malformed")
        key, property_value = item.split("=", 1)
        if not re.fullmatch(
            r"(?:INIT\w*|RAM_MODE|READ_WIDTH\w*|WRITE_WIDTH\w*|"
            r"DO[A-B]?_REG|WRITE_MODE\w*|EN_ECC\w*|CASCADE_ORDER\w*|"
            r"CLOCK_DOMAINS\w*)",
            key,
        ):
            raise RamControlLocalizationError("RAM semantic property is not allowlisted")
        if not property_value:
            raise RamControlLocalizationError("RAM semantic property is empty")
        properties[key] = property_value
    return properties


def recover_multilevel_ram_control_closure(
    path: Path | str, *, source_dcp_sha256: str, run_id: str
) -> dict[str, Any]:
    """Convert a fresh Tcl trace into a private manifest and anonymous contract."""

    if re.fullmatch(r"[0-9a-f]{64}", source_dcp_sha256) is None:
        raise RamControlLocalizationError("RAM-control source identity is invalid")
    if _RUN_ID_RE.fullmatch(run_id) is None:
        raise RamControlLocalizationError("RAM-control run identity is invalid")
    with Path(path).open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    cells: list[dict[str, Any]] = []
    endpoints: list[dict[str, Any]] = []
    consumers: list[dict[str, str]] = []
    summary: dict[str, str] = {}
    for row in rows:
        kind = str(row.get("kind") or "")
        if kind == "cell":
            bindings: dict[str, str] = {}
            for binding in filter(None, str(row.get("input_bindings") or "").split("|")):
                if "=" not in binding:
                    raise RamControlLocalizationError("RAM-control LUT binding is malformed")
                pin, source = binding.split("=", 1)
                if not re.fullmatch(r"I[0-5]", pin) or not source:
                    raise RamControlLocalizationError("RAM-control LUT binding is invalid")
                bindings[pin] = source
            item = {
                "cell_private": str(row.get("cell_private") or ""),
                "ref": str(row.get("ref") or ""),
                "init": str(row.get("init") or ""),
                "output_source": str(row.get("output_source") or ""),
                "inputs": bindings,
            }
            if (
                not item["cell_private"]
                or re.fullmatch(r"LUT[1-6]", item["ref"]) is None
                or not item["init"]
                or not item["output_source"]
                or not bindings
            ):
                raise RamControlLocalizationError("RAM-control LUT record is incomplete")
            cells.append(item)
        elif kind == "endpoint":
            item = {
                "endpoint_private": str(row.get("endpoint_private") or ""),
                "role": str(row.get("role") or ""),
                "clock_region": str(row.get("clock_region") or ""),
                "loc": str(row.get("loc") or ""),
                "primitive_private": str(row.get("primitive_private") or ""),
                "primitive_ref": str(row.get("primitive_ref") or ""),
                "semantic_properties": _parse_properties(
                    str(row.get("semantic_properties") or "")
                ),
                "root_net_private": str(row.get("root_net_private") or ""),
            }
            if (
                not item["endpoint_private"]
                or re.fullmatch(r"[A-Z][A-Z0-9_]*", item["role"]) is None
                or not item["clock_region"]
                or not item["loc"]
                or not item["primitive_private"]
                or re.match(r"^(?:RAMB|URAM|RAM(?:[0-9]|S|D))", item["primitive_ref"])
                is None
                or not item["root_net_private"]
            ):
                raise RamControlLocalizationError("RAM-control endpoint record is incomplete")
            endpoints.append(item)
        elif kind == "consumer":
            item = {
                "consumer_private": str(row.get("consumer_private") or ""),
                "consumer_ref": str(row.get("consumer_ref") or ""),
                "consumer_role": str(row.get("consumer_role") or ""),
                "root_net_private": str(row.get("root_net_private") or ""),
            }
            if not all(item.values()):
                raise RamControlLocalizationError("non-selected consumer record is incomplete")
            consumers.append(item)
        elif kind == "summary":
            summary[str(row.get("key") or "")] = str(row.get("value") or "")

    if not cells or len(endpoints) < 2:
        raise RamControlLocalizationError("live multilevel RAM-control closure is empty")
    roles = {item["role"] for item in endpoints}
    if len(roles) != 1 or summary.get("root_role") not in roles:
        raise RamControlLocalizationError("mixed RAM-control endpoint roles")
    regions = {item["clock_region"] for item in endpoints}
    if len(regions) < 2:
        raise RamControlLocalizationError("RAM-control endpoints do not span regions")
    root_nets = {
        item["root_net_private"] for item in [*endpoints, *consumers]
    }
    if len(root_nets) != 1:
        raise RamControlLocalizationError("RAM-control consumers do not share one root net")
    if int(summary.get("sink_count", "0") or 0) != len(endpoints):
        raise RamControlLocalizationError("RAM-control sink count changed")
    if int(summary.get("nonselected_consumer_count", "0") or 0) != len(consumers):
        raise RamControlLocalizationError("RAM-control consumer count changed")
    covered_path_count = int(summary.get("covered_path_count", "0") or 0)
    if covered_path_count < 1:
        raise RamControlLocalizationError("RAM-control closure has zero live coverage")
    sampled_path_count = int(summary.get("sampled_path_count", "0") or 0)
    earliest_path_rank = int(summary.get("earliest_path_rank", "0") or 0)
    if (
        sampled_path_count < 1
        or earliest_path_rank < 1
        or earliest_path_rank > sampled_path_count
    ):
        raise RamControlLocalizationError("RAM-control timing rank is invalid")
    live_timing_rank_per_mille = (
        earliest_path_rank * 1000 + sampled_path_count - 1
    ) // sampled_path_count
    timing_eligible = live_timing_rank_per_mille <= 250

    by_output = {item["output_source"]: item for item in cells}
    if len(by_output) != len(cells):
        raise RamControlLocalizationError("RAM-control closure has duplicate LUT outputs")
    root = summary.get("root_output_source", "")
    if root not in by_output:
        raise RamControlLocalizationError("RAM-control closure root is absent")
    reachable: set[str] = set()

    def visit(output: str, active: set[str]) -> None:
        item = by_output.get(output)
        if item is None or output in reachable:
            return
        if output in active:
            raise RamControlLocalizationError("cycle in RAM-control LUT closure")
        active.add(output)
        for source in item["inputs"].values():
            visit(source, active)
        active.remove(output)
        reachable.add(output)

    visit(root, set())
    if len(reachable) != len(cells):
        raise RamControlLocalizationError("RAM-control closure contains unreachable LUTs")
    depth_cache: dict[str, int] = {}
    ordered = sorted(
        cells,
        key=lambda item: (
            _depth(item["output_source"], by_output, depth_cache),
            item["output_source"],
        ),
    )
    boundary = {
        source
        for item in ordered
        for source in item["inputs"].values()
        if source not in by_output
    }
    primitive_counts: dict[str, int] = {}
    for endpoint in endpoints:
        primitive_counts[endpoint["primitive_ref"]] = (
            primitive_counts.get(endpoint["primitive_ref"], 0) + 1
        )
    private = {
        "run_id": run_id,
        "source_dcp_sha256": source_dcp_sha256,
        "candidate_ref": FAMILY_REF,
        "root_output_source_private": root,
        "root_net_private": next(iter(root_nets)),
        "cells_private": ordered,
        "endpoints_private": endpoints,
        "nonselected_consumers_private": consumers,
    }
    public = {
        "kind": "multilevel_ram_control_distribution_proof_input",
        "schema_version": 1,
        "status": "proved_read_only",
        "authority": "multilevel_ram_control_localization_proof_input",
        "mutation_eligible": timing_eligible,
        "operator_class": OPERATOR_CLASS,
        "run_id": run_id,
        "source_dcp_sha256": source_dcp_sha256,
        "candidate_ref": FAMILY_REF,
        "logic_depth": _depth(root, by_output, {}),
        "closure_cell_count": len(ordered),
        "boundary_source_count": len(boundary),
        "sink_count": len(endpoints),
        "clock_region_count": len(regions),
        "sink_role": next(iter(roles)),
        "ram_primitive_counts": primitive_counts,
        "nonselected_consumer_count": len(consumers),
        "sampled_path_count": sampled_path_count,
        "covered_path_count": covered_path_count,
        "earliest_path_rank": earliest_path_rank,
        "live_timing_rank_per_mille": live_timing_rank_per_mille,
        "timing_eligibility_gate": "top_current_seed_timing_quartile",
        "required_before_mutation": [
            "clone every LUT and side input in the complete RAM-control Boolean DAG",
            "preserve each RAM primitive, semantic property, pin role, and cycle boundary",
            "rewire only selected same-role RAM-control sinks",
            "preserve the original generator and every non-selected consumer",
            "route and reopen under the unchanged contest clock",
        ],
        "exact_names_public": False,
    }
    public["private_manifest_sha256"] = _digest(private)
    public["contract_digest"] = _digest(public)
    return {"public_contract": public, "private_manifest": private}


def _tcl_quote(value: str) -> str:
    if any(character in value for character in "\x00\r\n"):
        raise RamControlLocalizationError("unsafe Tcl value")
    return "{" + value.replace("}", "\\}") + "}"


def _identity_assertions(private: dict[str, Any], *, post_mutation: bool) -> list[str]:
    root_key = str(private["root_output_source_private"])
    if not root_key.startswith("PIN:"):
        raise RamControlLocalizationError("RAM-control root is not pin-bound")
    root_pin = root_key[4:]
    lines: list[str] = []
    for endpoint in private["endpoints_private"]:
        pin = _tcl_quote(endpoint["endpoint_private"])
        lines.extend([
            f"set ramctrl_pin [get_pins -quiet {pin}]",
            "if {[llength $ramctrl_pin] != 1} {error {RAM-control endpoint identity changed}}",
            "set ramctrl_cell [get_cells -quiet -of_objects $ramctrl_pin]",
            "if {[llength $ramctrl_cell] != 1} {error {RAM-control primitive identity changed}}",
            f"if {{[safe_prop $ramctrl_cell REF_NAME] ne {_tcl_quote(endpoint['primitive_ref'])}}} {{error {{RAM-control primitive class changed}}}}",
            f"if {{[safe_prop $ramctrl_pin REF_PIN_NAME] ne {_tcl_quote(endpoint['role'])}}} {{error {{RAM-control pin role changed}}}}",
            f"if {{[safe_prop $ramctrl_cell LOC] ne {_tcl_quote(endpoint['loc'])}}} {{error {{RAM-control primitive placement changed}}}}",
        ])
        for name, value in sorted(endpoint["semantic_properties"].items()):
            lines.append(
                f"if {{[safe_prop $ramctrl_cell {_tcl_quote(name)}] ne {_tcl_quote(value)}}} "
                "{error {RAM-control semantic property changed}}"
            )
        if post_mutation:
            lines.append(
                f"ramctrl_assert_selected_rewired {_tcl_quote(root_pin)} {pin}"
            )
    for consumer in private["nonselected_consumers_private"]:
        lines.append(
            f"ramctrl_assert_original_consumer {_tcl_quote(root_pin)} "
            f"{_tcl_quote(consumer['consumer_private'])}"
        )
    return lines


def build_multilevel_ram_control_localization_tcl(
    recovered: dict[str, Any],
    input_dcp: str,
    output_dcp: str,
    result_tsv: str,
    *,
    max_sinks_per_group: int = 64,
    pre_repair_sites: list[str] | None = None,
    repair_sites_path: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build a RAM-specific mutation around the shared DAG-clone lowering."""

    private = recovered.get("private_manifest") or {}
    public = recovered.get("public_contract") or {}
    if (
        public.get("operator_class") != OPERATOR_CLASS
        or public.get("candidate_ref") != FAMILY_REF
        or public.get("mutation_eligible") is not True
        or public.get("private_manifest_sha256") != _digest(private)
        or private.get("candidate_ref") != FAMILY_REF
        or _RUN_ID_RE.fullmatch(str(public.get("run_id") or "")) is None
        or private.get("run_id") != public.get("run_id")
    ):
        raise RamControlLocalizationError("RAM-control mutation manifest is invalid")
    generic_public = dict(public)
    generic_public["candidate_ref"] = "multilevel_control_1"
    script, topology = build_multilevel_control_localization_tcl(
        {"public_contract": generic_public, "private_manifest": private},
        input_dcp,
        output_dcp,
        result_tsv,
        max_sinks_per_group=max_sinks_per_group,
        pre_repair_sites=pre_repair_sites,
        repair_sites_path=repair_sites_path,
    )
    script = script.replace(
        "multilevel_control_distribution_localization_v1", ACTION_TARGET
    ).replace(
        "MULTILEVEL_CONTROL_DAG_CLONE_V1", PROOF_TEMPLATE
    ).replace(
        "multilevel_control_1", FAMILY_REF
    ).replace(
        "fdagents_multictrl_", "fdagents_multiramctrl_"
    )
    helper_lines = [
        "proc ramctrl_shared_segment {left right} {set names {}; foreach net [get_nets -quiet -segments -of_objects $left] {dict set names [get_property NAME $net] 1}; foreach net [get_nets -quiet -segments -of_objects $right] {if {[dict exists $names [get_property NAME $net]]} {return 1}}; return 0}",
        "proc ramctrl_assert_original_consumer {root_name sink_name} {set root [get_pins -quiet [list $root_name]]; set sink [get_pins -quiet [list $sink_name]]; if {[llength $root] != 1 || [llength $sink] != 1 || ![ramctrl_shared_segment $root $sink]} {error {RAM-control non-selected consumer changed}}}",
        "proc ramctrl_assert_selected_rewired {root_name sink_name} {set root [get_pins -quiet [list $root_name]]; set sink [get_pins -quiet [list $sink_name]]; if {[llength $root] != 1 || [llength $sink] != 1 || [ramctrl_shared_segment $root $sink]} {error {RAM-control selected sink was not exclusively rewired}}; set drivers [filter [get_pins -quiet -leaf -of_objects [get_nets -quiet -segments -of_objects $sink]] {DIRECTION == OUT}]; if {[llength $drivers] != 1} {error {RAM-control selected sink driver is ambiguous}}; set driver [get_cells -quiet -of_objects [lindex $drivers 0]]; if {[llength $driver] != 1 || ![string match {LUT*} [safe_prop $driver REF_NAME]]} {error {RAM-control selected sink is not driven by cloned LUT DAG}}}",
    ]
    before_marker = "set input_ff_count [llength [get_cells -quiet -hierarchical -filter {REF_NAME =~ FD*}]]"
    after_marker = "set output_timing [timing_snapshot]"
    if before_marker not in script or after_marker not in script:
        raise RamControlLocalizationError("shared DAG lowering ABI changed")
    script = script.replace(
        before_marker,
        "\n".join([
            before_marker,
            *helper_lines,
            *_identity_assertions(private, post_mutation=False),
        ]),
        1,
    )
    script = script.replace(
        after_marker,
        "\n".join([
            *_identity_assertions(private, post_mutation=True),
            after_marker,
        ]),
        1,
    )
    topology = dict(topology)
    topology.update({
        "backend": ACTION_TARGET,
        "contract": PROOF_TEMPLATE,
        "family_ref": FAMILY_REF,
        "ram_primitive_count": len(private["endpoints_private"]),
        "nonselected_consumer_count": len(
            private["nonselected_consumers_private"]
        ),
    })
    topology.pop("manifest_sha256", None)
    topology["manifest_sha256"] = _digest(topology)
    return script, topology


__all__ = [
    "ACTION_TARGET",
    "CONTRACT_KIND",
    "FAMILY_REF",
    "OPERATOR_CLASS",
    "PROOF_TEMPLATE",
    "RamControlLocalizationError",
    "build_multilevel_ram_control_localization_tcl",
    "recover_multilevel_ram_control_closure",
]
