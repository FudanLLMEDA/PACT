"""Anonymous probe and routed backend for registered GF(2) reassociation."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from FDAgents.operator_proof import build_registered_parity_authorization
from FDAgents.parity_reassociation import (
    build_registered_parity_reassociation_tcl,
    recover_registered_parity_closures,
)


_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "probe_registered_parity_closures.tcl"
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_FAMILY_RE = re.compile(r"registered_parity_[1-9][0-9]*")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()


def _brace(value: Path | str) -> str:
    text = str(value)
    if any(char in text for char in ("}", "\r", "\n")):
        raise ValueError("registered parity path contains unsafe Tcl characters")
    return "{" + text + "}"


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
    manifest_path: Path,
    report_path: Path,
) -> dict[str, Any]:
    if _SHA_RE.fullmatch(source_dcp_sha256) is None:
        raise ValueError("registered parity source identity is invalid")
    recovered = recover_registered_parity_closures(
        extraction_tsv, source_dcp_sha256=source_dcp_sha256
    )
    public_contract = recovered["public_contract"]
    private_contract = recovered["private_manifest"]
    family_refs = [
        str(item.get("candidate_ref") or "")
        for item in public_contract.get("candidates") or []
    ]
    if any(_FAMILY_RE.fullmatch(ref) is None for ref in family_refs):
        raise ValueError("registered parity recovery produced an invalid family")
    manifest = {
        "version": 1,
        "source_dcp_sha256": source_dcp_sha256,
        "family_refs": family_refs,
        "private_contract": private_contract,
        "public_contract": public_contract,
    }
    manifest["manifest_digest"] = _digest(manifest)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    fields = (
        "candidate_ref", "logic_depth", "closure_cell_count",
        "raw_leaf_occurrence_count", "odd_leaf_count",
        "canceled_leaf_occurrence_count", "registered_endpoint_count",
        "live_registered_endpoint_count", "earliest_live_path_rank",
        "output_polarity",
    )
    families = [
        {
            **{key: item.get(key) for key in fields if key != "candidate_ref"},
            "family_ref": item.get("candidate_ref"),
        }
        for item in public_contract.get("candidates") or []
        if isinstance(item, dict)
    ]
    report = {
        "status": "success",
        "kind": "registered_gf2_parity_reassociation_contract",
        "version": 1,
        "fresh_rediscovery": True,
        "read_only": True,
        "operator_class": "REGISTERED_GF2_PARITY_REDUCTION",
        "source_dcp_sha256": source_dcp_sha256,
        "families": families,
        "summary": {
            "family_count": len(families),
            "mutation_eligible": bool(families),
        },
        "proof": {
            "complete_xor_xnor_closures_recovered": True,
            "all_boundary_multiplicities_replayed_in_gf2": True,
            "registered_data_endpoints_recovered": True,
            "exact_names_are_private": True,
            "does_not_establish_positive_routed_qor": True,
        },
        "action_assessment": {
            "status": (
                "measurement_candidates_available"
                if families else "no_live_registered_parity_closure"
            ),
            "candidate_actions": [
                {
                    "skill": "operator_rewrite",
                    "target": "registered_gf2_parity_reassociation_v1",
                    "params": {"family_ref": item["family_ref"]},
                }
                for item in families
            ],
            "expected_mechanism": (
                "cancel only even GF(2) boundary multiplicities and rebuild a "
                "balanced LUT6 XOR tree for one live registered data endpoint"
            ),
            "failure_update": (
                "distinguish incomplete parity closure, sibling-consumer change, "
                "route failure, and a routed balanced-tree regression"
            ),
            "not_a_hard_trigger": True,
            "luna_owns_family_selection": True,
        },
        "private_mutation_manifest_retained": True,
        "private_manifest_digest": manifest["manifest_digest"],
        "public_contract": public_contract,
    }
    unsigned = json.loads(json.dumps(report, sort_keys=True))
    report["contract_digest"] = _digest(unsigned)
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report


def _load(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
        raise ValueError(f"registered parity {label} is unavailable")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"registered parity {label} is malformed")
    return value


def load_private_manifest(
    path: Path, *, expected_source_sha256: str
) -> dict[str, Any]:
    value = _load(path, label="private manifest")
    claimed = value.get("manifest_digest")
    unsigned = json.loads(json.dumps(value))
    unsigned.pop("manifest_digest", None)
    refs = value.get("family_refs")
    if (
        value.get("version") != 1
        or value.get("source_dcp_sha256") != expected_source_sha256
        or _SHA_RE.fullmatch(str(claimed or "")) is None
        or _digest(unsigned) != claimed
        or not isinstance(refs, list)
        or any(_FAMILY_RE.fullmatch(str(ref)) is None for ref in refs)
    ):
        raise ValueError("registered parity private manifest identity changed")
    return value


def load_probe_report(
    path: Path, *, expected_source_sha256: str
) -> dict[str, Any]:
    value = _load(path, label="probe report")
    claimed = value.get("contract_digest")
    unsigned = json.loads(json.dumps(value))
    unsigned.pop("contract_digest", None)
    if (
        value.get("kind") != "registered_gf2_parity_reassociation_contract"
        or value.get("source_dcp_sha256") != expected_source_sha256
        or _SHA_RE.fullmatch(str(claimed or "")) is None
        or _digest(unsigned) != claimed
    ):
        raise ValueError("registered parity probe identity changed")
    return value


def render_selected_family(
    *,
    seed_dcp: Path,
    output_dcp: Path,
    result_tsv: Path,
    manifest: dict[str, Any],
    family_ref: str,
    implementation_mode: str = "local_preserve",
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    if _FAMILY_RE.fullmatch(family_ref) is None:
        raise ValueError("registered parity family_ref is invalid")
    if family_ref not in (manifest.get("family_refs") or []):
        raise ValueError("registered parity family is absent from manifest")
    recovered = {
        "public_contract": manifest.get("public_contract"),
        "private_manifest": manifest.get("private_contract"),
    }
    public = recovered.get("public_contract") or {}
    authorization = build_registered_parity_authorization(
        str(manifest.get("source_dcp_sha256") or ""), public, family_ref,
        implementation_mode,
    )
    script, topology = build_registered_parity_reassociation_tcl(
        recovered, family_ref, str(seed_dcp), str(output_dcp), str(result_tsv),
        implementation_mode,
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
) -> dict[str, Any]:
    values = _metric_values(path)
    required = {
        "backend", "contract", "implementation_mode", "candidate_ref",
        "rebasin_cell_count", "rebasin_site_count",
        "input_loc_fixed_count", "input_bel_fixed_count",
        "output_loc_fixed_count", "output_bel_fixed_count",
        "temporary_loc_fixed_count", "temporary_bel_fixed_count",
        "placement_fix_identity_preserved",
        "source_dcp_sha256",
        "odd_leaf_count", "new_lut_count", "old_closure_cell_count",
        "registered_endpoint_count", "preserved_sibling_count",
        "old_root_sink_count_before", "nonselected_sink_count_expected",
        "nonselected_sink_count_after_route",
        "nonselected_sink_count_after_reopen",
        "all_nonselected_sink_sets_equal",
        "input_period_ns", "input_waveform", "input_clock_source",
        "input_wns_ns", "output_period_ns", "output_waveform",
        "output_clock_source", "output_wns_ns", "output_whs_ns",
        "input_lut_count", "output_lut_count", "input_ff_count",
        "output_ff_count", "route_errors", "pulse_violations",
        "candidate_checkpoint_reopened",
    }
    if not required <= set(values):
        raise ValueError("registered parity routed report is incomplete")

    def integer(key: str) -> int:
        return int(values[key])

    def number(key: str) -> float:
        return float(values[key])

    if (
        values["backend"] != "registered_gf2_parity_reassociation_v1"
        or values["contract"] != "REGISTERED_GF2_PARITY_REASSOCIATION_V1"
        or values["implementation_mode"] != topology.get("implementation_mode")
        or (
            values["implementation_mode"] == "local_preserve"
            and integer("rebasin_cell_count") != 0
        )
        or (
            values["implementation_mode"] == "global_quick_rebasin"
            and integer("rebasin_cell_count") < 1
        )
        or (
            values["implementation_mode"] == "closure_local_rebasin"
            and (
                integer("rebasin_cell_count") < 1
                or integer("rebasin_site_count") < 1
            )
        )
        or values["candidate_ref"] != family_ref
        or values["source_dcp_sha256"] != expected_source_sha256
        or values["input_period_ns"] != values["output_period_ns"]
        or values["input_waveform"] != values["output_waveform"]
        or values["input_clock_source"] != values["output_clock_source"]
        or integer("odd_leaf_count") != topology.get("odd_leaf_count")
        or integer("new_lut_count") != topology.get("new_lut_count")
        or integer("old_closure_cell_count")
        != topology.get("old_closure_cell_count")
        or integer("registered_endpoint_count")
        != topology.get("registered_endpoint_count")
        or integer("preserved_sibling_count")
        != topology.get("preserved_sibling_count")
        or integer("old_root_sink_count_before")
        != integer("nonselected_sink_count_expected") + 1
        or integer("nonselected_sink_count_after_route")
        != integer("nonselected_sink_count_expected")
        or integer("nonselected_sink_count_after_reopen")
        != integer("nonselected_sink_count_expected")
        or integer("all_nonselected_sink_sets_equal") != 1
        or integer("input_loc_fixed_count") != integer("output_loc_fixed_count")
        or integer("input_bel_fixed_count") != integer("output_bel_fixed_count")
        or integer("placement_fix_identity_preserved") != 1
        or integer("route_errors") != 0
        or integer("pulse_violations") != 0
        or integer("candidate_checkpoint_reopened") != 1
    ):
        raise ValueError("registered parity routed identity checks failed")
    return {
        "contract": values["contract"],
        "backend": values["backend"],
        "operator_class": "REGISTERED_GF2_PARITY_REDUCTION",
        "implementation_mode": values["implementation_mode"],
        "rebasin_cell_count": integer("rebasin_cell_count"),
        "rebasin_site_count": integer("rebasin_site_count"),
        "input_loc_fixed_count": integer("input_loc_fixed_count"),
        "input_bel_fixed_count": integer("input_bel_fixed_count"),
        "temporary_loc_fixed_count": integer("temporary_loc_fixed_count"),
        "temporary_bel_fixed_count": integer("temporary_bel_fixed_count"),
        "source_dcp_sha256": expected_source_sha256,
        "contract_digest": contract_digest,
        "family_ref": family_ref,
        "period_ns": number("output_period_ns"),
        "input_wns_ns": number("input_wns_ns"),
        "output_wns_ns": number("output_wns_ns"),
        "output_whs_ns": number("output_whs_ns"),
        "odd_leaf_count": integer("odd_leaf_count"),
        "new_lut_count": integer("new_lut_count"),
        "old_closure_cell_count": integer("old_closure_cell_count"),
        "registered_endpoint_count": integer("registered_endpoint_count"),
        "preserved_sibling_count": integer("preserved_sibling_count"),
        "old_root_sink_count_before": integer("old_root_sink_count_before"),
        "nonselected_sink_count_expected": integer(
            "nonselected_sink_count_expected"
        ),
        "nonselected_sink_count_after_route": integer(
            "nonselected_sink_count_after_route"
        ),
        "nonselected_sink_count_after_reopen": integer(
            "nonselected_sink_count_after_reopen"
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
        "private_manifest_identity_replayed": True,
        "exact_gf2_expression_replayed": True,
        "balanced_xor_tree_emitted": True,
        "selected_registered_boundary_preserved": True,
        "original_root_and_nonselected_consumers_preserved": True,
        "all_nonselected_consumer_sink_set_preserved": True,
        "placement_fix_identity_preserved": True,
        "clock_identity_preserved": True,
        "topology_manifest_sha256": topology.get("manifest_sha256"),
    }


__all__ = [
    "build_probe_script", "recover_and_write_contract",
    "load_private_manifest", "load_probe_report", "render_selected_family",
    "parse_mutation_result",
]
