"""Anonymous probe and routed backend for multilevel RAM-control localization."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from FDAgents.multilevel_ram_control_localization import (
    ACTION_TARGET,
    CONTRACT_KIND,
    FAMILY_REF,
    OPERATOR_CLASS,
    PROOF_TEMPLATE,
    build_multilevel_ram_control_localization_tcl,
    recover_multilevel_ram_control_closure,
)
from FDAgents.operator_proof import (
    build_multilevel_ram_control_localization_authorization,
)


_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "probe_multilevel_ram_control_closure.tcl"
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_FAMILY_RE = re.compile(r"multilevel_ram_control_[1-9][0-9]*")
_RUN_ID_RE = re.compile(r"nr-[0-9a-f]{32}")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()


def _brace(value: Path | str) -> str:
    text = str(value)
    if any(char in text for char in ("}", "\r", "\n")):
        raise ValueError("RAM-control localization path is unsafe")
    return "{" + text + "}"


def empty_contract(
    source_dcp_sha256: str,
    *,
    run_id: str,
    refusal_reason: str = "no_complete_multilevel_ram_control_wall",
) -> dict[str, Any]:
    """Return a typed negative boundary when no complete RAM wall exists."""
    if _SHA_RE.fullmatch(source_dcp_sha256) is None:
        raise ValueError("RAM-control localization source identity is invalid")
    if _RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError("RAM-control localization run identity is invalid")
    return {
        "status": "success",
        "kind": CONTRACT_KIND,
        "version": 1,
        "fresh_rediscovery": True,
        "read_only": True,
        "operator_class": OPERATOR_CLASS,
        "run_id": run_id,
        "source_dcp_sha256": source_dcp_sha256,
        "families": [],
        "summary": {
            "family_count": 0,
            "mutation_eligible": False,
            "refusal_reason": refusal_reason,
        },
        "proof": {
            "complete_boolean_dag_recovered": False,
            "exact_lut_truth_tables_and_boundary_sources_bound": False,
            "ram_primitive_property_and_pin_role_bound": False,
            "nonselected_consumer_closure_recovered": False,
            "zero_cycle_combinational_schedule_bound": False,
            "exact_names_are_private": True,
            "does_not_establish_positive_routed_qor": True,
        },
        "action_assessment": {
            "status": refusal_reason,
            "candidate_actions": [],
            "not_a_hard_trigger": True,
            "luna_owns_family_selection": True,
        },
        "private_mutation_manifest_retained": False,
        "proof_status": "negative_read_only_boundary",
    }


def build_probe_script(arguments: Iterable[Path | str]) -> str:
    values = list(arguments)
    argv = " ".join(_brace(item) for item in values)
    return (
        f"set argc {len(values)}\nset argv [list {argv}]\n"
        "set ::fdagents_embedded_probe 1\n"
        + _SCRIPT.read_text(encoding="utf-8")
        + "\nunset -nocomplain ::fdagents_embedded_probe\n"
    )


def recover_and_write_contract(
    extraction_tsv: Path,
    *,
    source_dcp_sha256: str,
    run_id: str,
    manifest_path: Path,
    report_path: Path,
) -> dict[str, Any]:
    if _SHA_RE.fullmatch(source_dcp_sha256) is None:
        raise ValueError("RAM-control localization source identity is invalid")
    if _RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError("RAM-control localization run identity is invalid")
    recovered = recover_multilevel_ram_control_closure(
        extraction_tsv, source_dcp_sha256=source_dcp_sha256, run_id=run_id
    )
    public_contract = recovered["public_contract"]
    private_manifest = recovered["private_manifest"]
    if public_contract.get("mutation_eligible") is not True:
        manifest_path.unlink(missing_ok=True)
        report_path.unlink(missing_ok=True)
        return empty_contract(
            source_dcp_sha256,
            run_id=run_id,
            refusal_reason="live_ram_control_wall_below_top_timing_quartile",
        )
    manifest = {
        "version": 1,
        "run_id": run_id,
        "source_dcp_sha256": source_dcp_sha256,
        "family_ref": FAMILY_REF,
        "private_contract": private_manifest,
        "public_contract": public_contract,
    }
    manifest["manifest_digest"] = _digest(manifest)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    family_fields = (
        "logic_depth",
        "closure_cell_count",
        "boundary_source_count",
        "sink_count",
        "clock_region_count",
        "sink_role",
        "ram_primitive_counts",
        "nonselected_consumer_count",
        "sampled_path_count",
        "covered_path_count",
        "earliest_path_rank",
        "live_timing_rank_per_mille",
        "timing_eligibility_gate",
    )
    family = {
        "family_ref": FAMILY_REF,
        **{key: public_contract.get(key) for key in family_fields},
    }
    report = {
        "status": "success",
        "kind": CONTRACT_KIND,
        "version": 1,
        "fresh_rediscovery": True,
        "read_only": True,
        "operator_class": OPERATOR_CLASS,
        "run_id": run_id,
        "source_dcp_sha256": source_dcp_sha256,
        "families": [family],
        "summary": {"family_count": 1, "mutation_eligible": True},
        "proof": {
            "complete_boolean_dag_recovered": True,
            "exact_lut_truth_tables_and_boundary_sources_bound": True,
            "ram_primitive_property_and_pin_role_bound": True,
            "nonselected_consumer_closure_recovered": True,
            "zero_cycle_combinational_schedule_bound": True,
            "exact_names_are_private": True,
            "does_not_establish_positive_routed_qor": True,
        },
        "action_assessment": {
            "status": "measurement_candidate_available",
            "candidate_actions": [{
                "skill": "operator_rewrite",
                "target": ACTION_TARGET,
                "params": {"family_ref": FAMILY_REF},
            }],
            "expected_mechanism": (
                "clone one complete same-cycle RAM-control Boolean DAG near "
                "same-role sink regions while preserving RAM identity and all "
                "non-selected consumers"
            ),
            "failure_update": (
                "separate incomplete closure, RAM identity change, route "
                "failure, and routed QoR regression"
            ),
            "not_a_hard_trigger": True,
            "luna_owns_family_selection": True,
        },
        "private_mutation_manifest_retained": True,
        "private_manifest_digest": manifest["manifest_digest"],
        "public_contract": public_contract,
    }
    report["contract_digest"] = _digest(report)
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report


def _load(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
        raise ValueError(f"RAM-control localization {label} is unavailable")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"RAM-control localization {label} is malformed")
    return value


def load_private_manifest(
    path: Path, *, expected_source_sha256: str, expected_run_id: str
) -> dict[str, Any]:
    value = _load(path, label="private manifest")
    claimed = value.get("manifest_digest")
    unsigned = json.loads(json.dumps(value))
    unsigned.pop("manifest_digest", None)
    if (
        value.get("version") != 1
        or value.get("run_id") != expected_run_id
        or value.get("source_dcp_sha256") != expected_source_sha256
        or value.get("family_ref") != FAMILY_REF
        or _SHA_RE.fullmatch(str(claimed or "")) is None
        or _digest(unsigned) != claimed
    ):
        raise ValueError("RAM-control private manifest identity changed")
    return value


def load_probe_report(
    path: Path, *, expected_source_sha256: str, expected_run_id: str
) -> dict[str, Any]:
    value = _load(path, label="probe report")
    claimed = value.get("contract_digest")
    unsigned = json.loads(json.dumps(value))
    unsigned.pop("contract_digest", None)
    if (
        value.get("kind") != CONTRACT_KIND
        or value.get("run_id") != expected_run_id
        or value.get("source_dcp_sha256") != expected_source_sha256
        or _SHA_RE.fullmatch(str(claimed or "")) is None
        or _digest(unsigned) != claimed
    ):
        raise ValueError("RAM-control probe identity changed")
    return value


def render_selected_family(
    *,
    seed_dcp: Path,
    output_dcp: Path,
    result_tsv: Path,
    manifest: dict[str, Any],
    family_ref: str,
    pre_repair_sites: list[str] | None = None,
    repair_sites_path: Path | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    if _FAMILY_RE.fullmatch(family_ref) is None or family_ref != FAMILY_REF:
        raise ValueError("RAM-control family_ref is invalid")
    if family_ref != manifest.get("family_ref"):
        raise ValueError("RAM-control family is absent from manifest")
    recovered = {
        "public_contract": manifest.get("public_contract"),
        "private_manifest": manifest.get("private_contract"),
    }
    public = recovered.get("public_contract") or {}
    authorization = build_multilevel_ram_control_localization_authorization(
        str(manifest.get("source_dcp_sha256") or ""), public, family_ref
    )
    script, topology = build_multilevel_ram_control_localization_tcl(
        recovered,
        str(seed_dcp),
        str(output_dcp),
        str(result_tsv),
        pre_repair_sites=pre_repair_sites,
        repair_sites_path=(
            str(repair_sites_path) if repair_sites_path is not None else None
        ),
    )
    return script, topology, authorization


def _metric_values(path: Path) -> dict[str, str]:
    with path.open("r", encoding="utf-8", newline="") as stream:
        return {
            str(row.get("metric") or ""): str(row.get("value") or "")
            for row in csv.DictReader(stream, delimiter="\t")
        }


def parse_mutation_result(
    path: Path,
    *,
    expected_source_sha256: str,
    family_ref: str,
    contract_digest: str,
    topology: dict[str, Any],
    expected_run_id: str,
) -> dict[str, Any]:
    values = _metric_values(path)
    required = {
        "backend", "contract", "family_ref", "source_dcp_sha256",
        "closure_cell_count", "localized_sink_count", "clone_group_count",
        "new_lut_count", "input_loc_fixed_count", "input_bel_fixed_count",
        "output_loc_fixed_count", "output_bel_fixed_count",
        "temporary_loc_fixed_count", "temporary_bel_fixed_count",
        "placement_fix_identity_preserved", "input_period_ns",
        "input_waveform", "input_clock_source", "input_wns_ns",
        "output_period_ns", "output_waveform", "output_clock_source",
        "output_wns_ns", "output_whs_ns", "input_lut_count",
        "output_lut_count", "input_ff_count", "output_ff_count",
        "route_errors", "pulse_violations", "candidate_checkpoint_reopened",
    }
    if not required <= set(values):
        raise ValueError("RAM-control routed report is incomplete")

    def integer(key: str) -> int:
        return int(values[key])

    def number(key: str) -> float:
        return float(values[key])

    if (
        values["backend"] != ACTION_TARGET
        or values["contract"] != PROOF_TEMPLATE
        or values["family_ref"] != family_ref
        or values["source_dcp_sha256"] != expected_source_sha256
        or values["input_period_ns"] != values["output_period_ns"]
        or values["input_waveform"] != values["output_waveform"]
        or values["input_clock_source"] != values["output_clock_source"]
        or integer("closure_cell_count") != topology.get("closure_cell_count")
        or integer("localized_sink_count") != topology.get("localized_sink_count")
        or integer("clone_group_count") != topology.get("clone_group_count")
        or integer("new_lut_count") != topology.get("new_lut_count")
        or integer("input_loc_fixed_count") != integer("output_loc_fixed_count")
        or integer("input_bel_fixed_count") != integer("output_bel_fixed_count")
        or integer("placement_fix_identity_preserved") != 1
        or integer("route_errors") != 0
        or integer("pulse_violations") != 0
        or integer("candidate_checkpoint_reopened") != 1
    ):
        raise ValueError("RAM-control routed identity checks failed")
    return {
        "contract": PROOF_TEMPLATE,
        "backend": ACTION_TARGET,
        "operator_class": OPERATOR_CLASS,
        "run_id": expected_run_id,
        "source_dcp_sha256": expected_source_sha256,
        "contract_digest": contract_digest,
        "family_ref": family_ref,
        "period_ns": number("output_period_ns"),
        "input_wns_ns": number("input_wns_ns"),
        "output_wns_ns": number("output_wns_ns"),
        "output_whs_ns": number("output_whs_ns"),
        "closure_cell_count": integer("closure_cell_count"),
        "localized_sink_count": integer("localized_sink_count"),
        "clone_group_count": integer("clone_group_count"),
        "new_lut_count": integer("new_lut_count"),
        "nonselected_consumer_count": int(
            topology.get("nonselected_consumer_count") or 0
        ),
        "resource_delta": {
            "lut": integer("output_lut_count") - integer("input_lut_count"),
            "ff": integer("output_ff_count") - integer("input_ff_count"),
        },
        "route_errors": 0,
        "pulse_violations": 0,
        "candidate_checkpoint_reopened": True,
        "source_checkpoint_reopened": True,
        "relevant_nets_fully_routed": True,
        "private_manifest_identity_preserved": True,
        "exact_boolean_dag_cloned": True,
        "ram_primitive_and_property_identity_preserved": True,
        "ram_control_pin_role_preserved": True,
        "selected_ram_control_sinks_only_rewired": True,
        "zero_cycle_combinational_schedule_preserved": True,
        "original_generator_and_nonselected_consumers_preserved": True,
        "placement_fix_identity_preserved": True,
        "clock_identity_preserved": True,
        "topology_manifest_sha256": topology.get("manifest_sha256"),
    }


__all__ = [
    "build_probe_script",
    "empty_contract",
    "load_private_manifest",
    "load_probe_report",
    "parse_mutation_result",
    "recover_and_write_contract",
    "render_selected_family",
]
