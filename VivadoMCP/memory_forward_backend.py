"""Anonymous recovery and private handoff for memory write-buffer forwarding."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from FDAgents.memory_write_buffer_forward import (
    ForwardedReadPort,
    MemoryWriteBufferForwardContract,
)
from FDAgents.memory_write_buffer_forward_action import (
    MemoryForwardBinding,
    recover_memory_forward_contracts,
    render_memory_forward_eco_tcl,
)
from FDAgents.metaloop_action_proof import build_memory_forward_authorization


_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "extract_memory_forward_candidates.tcl"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_FAMILY_RE = re.compile(r"memory_forward_[1-9][0-9]*")


def _tcl_brace(value: Path | str) -> str:
    text = str(value)
    if any(char in text for char in ("}", "\r", "\n")):
        raise ValueError("memory-forward path contains unsafe Tcl characters")
    return "{" + text + "}"


def build_probe_script(arguments: Iterable[Path | str]) -> str:
    source = _SCRIPT.read_text(encoding="utf-8")
    argv = " ".join(_tcl_brace(item) for item in arguments)
    return f"set argv [list {argv}]\n" + source


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")).hexdigest()


def _float_or_none(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _memory_rows(extraction: Path) -> dict[str, dict[str, str]]:
    path = extraction / "memories.tsv"
    with path.open("r", encoding="utf-8", newline="") as handle:
        return {
            str(row.get("memory") or ""): dict(row)
            for row in csv.DictReader(handle, delimiter="\t")
            if row.get("memory")
        }


def recover_and_write_contract(
    extraction: Path,
    *,
    source_dcp_sha256: str,
    manifest_path: Path,
    report_path: Path,
) -> dict[str, Any]:
    if _SHA256_RE.fullmatch(source_dcp_sha256) is None:
        raise ValueError("memory-forward source identity is invalid")
    rejections: list[dict[str, Any]] = []
    recovered = recover_memory_forward_contracts(
        extraction, source_dcp_sha256, rejection_log=rejections
    )
    rows = _memory_rows(extraction)
    ordered = sorted(
        recovered,
        key=lambda item: (
            int(rows.get(item[1].memory, {}).get("top_path_rank") or 1_000_000),
            item[1].fingerprint(),
        ),
    )
    private_families: dict[str, Any] = {}
    public_families = []
    for ordinal, (contract, binding, proof) in enumerate(ordered, start=1):
        ref = f"memory_forward_{ordinal}"
        live = rows.get(binding.memory) or {}
        authorization = build_memory_forward_authorization(proof)
        private_families[ref] = {
            "contract": asdict(contract),
            "binding": asdict(binding),
            "proof": proof,
            "authorization_id": authorization["certificate_id"],
            "target_digest": binding.fingerprint(),
            "live": {
                "top_path_rank": int(live.get("top_path_rank") or 0),
                "write_worst_slack_ns": _float_or_none(
                    live.get("write_worst_slack_ns")
                ),
                "read_worst_slack_ns": _float_or_none(
                    live.get("read_worst_slack_ns")
                ),
            },
        }
        compare_groups = (contract.address_width - 1 + 2) // 3 + 1
        public_families.append({
            "family_ref": ref,
            "data_width": contract.data_width,
            "address_width": contract.address_width,
            "byte_enable_width": contract.byte_enable_width,
            "write_mode": contract.write_mode,
            "collision_mode": contract.read_address_collision,
            "common_clock": contract.common_clock,
            "read_port": "B",
            "read_latency_cycles": contract.read_ports[0].read_latency_cycles,
            "top_path_rank": int(live.get("top_path_rank") or 0),
            "write_worst_slack_ns": _float_or_none(
                live.get("write_worst_slack_ns")
            ),
            "read_worst_slack_ns": _float_or_none(
                live.get("read_worst_slack_ns")
            ),
            "expected_resource_overhead": {
                "fdre": 2 * contract.data_width + contract.address_width + 3,
                "lut": compare_groups + 1 + contract.data_width,
            },
            "risk": (
                "forwarding removes one write-side serial wall but adds a "
                "registered hit comparator and one mux at every closed read bit"
            ),
        })
    manifest = {
        "version": 1,
        "source_dcp_sha256": source_dcp_sha256,
        "families": private_families,
        "rejections": rejections,
    }
    manifest["manifest_digest"] = _digest(manifest)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    public = {
        "status": "success" if public_families else "no_candidate",
        "kind": "memory_write_buffer_forward_contract",
        "version": 1,
        "fresh_rediscovery": True,
        "read_only": True,
        "operator_class": "MEMORY_WRITE_BUFFER_FORWARD",
        "source_dcp_sha256": source_dcp_sha256,
        "families": public_families,
        "summary": {
            "family_count": len(public_families),
            "shape_rejection_count": len(rejections),
            "mutation_eligible": bool(public_families),
        },
        "proof": {
            "two_cycle_relational_memory_miter_replayed": bool(public_families),
            "adjacent_raw_hit_and_miss_covered": bool(public_families),
            "write_and_read_boundaries_closed": bool(public_families),
            "exact_memory_and_pin_names_are_private": True,
            "does_not_establish_positive_routed_qor": True,
        },
        "private_mutation_manifest_retained": True,
        "private_manifest_digest": manifest["manifest_digest"],
    }
    public["action_assessment"] = {
        "status": (
            "measurement_candidates_available" if public_families
            else "no_supported_live_write_boundary"
        ),
        "candidate_actions": [
            {
                "skill": "operator_rewrite",
                "target": "memory_write_buffer_forward_v1",
                "params": {"family_ref": item["family_ref"]},
            }
            for item in public_families
        ],
        "expected_mechanism": (
            "delay the physical BRAM write by one cycle and preserve same/next-cycle "
            "RAW behavior by forwarding the pending store at the registered read boundary"
        ),
        "failure_update": (
            "separate unsupported memory/control shape from a routed regression "
            "caused by the new read comparator or forwarding mux wall"
        ),
        "not_a_hard_trigger": True,
        "luna_owns_family_selection": True,
    }
    unsigned = json.loads(json.dumps(public))
    public["contract_digest"] = _digest(unsigned)
    report_path.write_text(
        json.dumps(public, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return public


def load_probe_report(
    path: Path, *, expected_source_sha256: str
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
        raise ValueError("memory-forward public probe report is unavailable")
    value = json.loads(path.read_text(encoding="utf-8"))
    digest = value.get("contract_digest")
    unsigned = json.loads(json.dumps(value))
    unsigned.pop("contract_digest", None)
    if (
        value.get("kind") != "memory_write_buffer_forward_contract"
        or value.get("source_dcp_sha256") != expected_source_sha256
        or _SHA256_RE.fullmatch(str(digest or "")) is None
        or _digest(unsigned) != digest
    ):
        raise ValueError("memory-forward public probe identity changed")
    return value


def load_private_manifest(
    path: Path, *, expected_source_sha256: str
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
        raise ValueError("memory-forward private manifest is unavailable")
    value = json.loads(path.read_text(encoding="utf-8"))
    claimed = value.get("manifest_digest")
    unsigned = json.loads(json.dumps(value))
    unsigned.pop("manifest_digest", None)
    if (
        value.get("version") != 1
        or value.get("source_dcp_sha256") != expected_source_sha256
        or _SHA256_RE.fullmatch(str(claimed or "")) is None
        or _digest(unsigned) != claimed
        or not isinstance(value.get("families"), dict)
    ):
        raise ValueError("memory-forward private manifest identity changed")
    return value


def resolve_private_family(
    manifest: dict[str, Any], family_ref: str
) -> tuple[MemoryWriteBufferForwardContract, MemoryForwardBinding, dict, dict]:
    if _FAMILY_RE.fullmatch(str(family_ref or "")) is None:
        raise ValueError("memory-forward family_ref is invalid")
    row = (manifest.get("families") or {}).get(family_ref)
    if not isinstance(row, dict):
        raise ValueError("memory-forward family is absent from the same-seed manifest")
    raw_contract = dict(row.get("contract") or {})
    raw_contract["read_ports"] = tuple(
        ForwardedReadPort(**item) for item in raw_contract.get("read_ports") or []
    )
    contract = MemoryWriteBufferForwardContract(**raw_contract)
    raw_binding = dict(row.get("binding") or {})
    for key in (
        "data_pins", "data_source_nets", "address_pins", "address_source_nets",
        "read_address_pins", "read_address_nets", "wea_pins",
        "read_output_pins", "read_raw_nets", "read_consumer_pins",
    ):
        raw_binding[key] = tuple(raw_binding.get(key) or [])
    binding = MemoryForwardBinding(**raw_binding)
    proof = dict(row.get("proof") or {})
    authorization = build_memory_forward_authorization(proof)
    if (
        binding.fingerprint() != row.get("target_digest")
        or authorization.get("certificate_id") != row.get("authorization_id")
        or contract.source_dcp_sha256 != manifest.get("source_dcp_sha256")
    ):
        raise ValueError("memory-forward private family replay failed")
    return contract, binding, proof, authorization


def render_selected_family(
    *,
    seed_dcp: Path,
    output_dir: Path,
    manifest: dict[str, Any],
    family_ref: str,
) -> tuple[str, dict, dict]:
    contract, binding, proof, authorization = resolve_private_family(
        manifest, family_ref
    )
    script, topology = render_memory_forward_eco_tcl(
        seed_dcp=seed_dcp,
        output_dir=output_dir,
        contract=contract,
        binding=binding,
        proof=proof,
    )
    script = re.sub(r"\nexit\s*\Z", "\n", script)
    return script, topology, authorization


def _metric_values(path: Path) -> dict[str, str]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    return {
        str(row.get("metric") or ""): str(row.get("value") or "")
        for row in rows
    }


def _integer(values: dict[str, str], field: str, *, minimum: int = 0) -> int:
    try:
        value = int(values[field])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"memory-forward {field} is invalid") from exc
    if value < minimum:
        raise ValueError(f"memory-forward {field} is below {minimum}")
    return value


def _float(values: dict[str, str], field: str) -> float:
    try:
        return float(values[field])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"memory-forward {field} is invalid") from exc


def parse_mutation_result(
    path: Path,
    *,
    expected_source_sha256: str,
    family_ref: str,
    topology: dict[str, Any],
) -> dict[str, Any]:
    """Validate routed/reopened measurements without exposing exact objects."""
    values = _metric_values(path)
    required = {
        "contract", "backend", "source_dcp_sha256",
        "target_memory_fingerprint", "input_period_ns", "input_waveform",
        "input_clock_source", "input_wns_ns", "output_period_ns",
        "output_waveform", "output_clock_source", "output_wns_ns",
        "output_whs_ns", "input_fdre_count", "output_fdre_count",
        "input_lut_count", "output_lut_count", "input_dsp_count",
        "output_dsp_count", "input_carry_count", "output_carry_count",
        "new_cells", "new_fdres", "new_luts", "route_errors",
        "pulse_violations", "candidate_checkpoint_reopened",
    }
    if not required.issubset(values):
        raise ValueError("memory-forward routed report is incomplete")
    if (
        values["contract"] != "BRAM_WRITE_BUFFER_FORWARD_LOCAL_MITER_V1"
        or values["backend"] != "memory_write_buffer_forward_v1"
        or values["source_dcp_sha256"] != expected_source_sha256
        or values["target_memory_fingerprint"]
        != topology.get("target_memory_fingerprint")
        or values["input_period_ns"] != values["output_period_ns"]
        or values["input_waveform"] != values["output_waveform"]
        or values["input_clock_source"] != values["output_clock_source"]
        or _integer(values, "route_errors") != 0
        or _integer(values, "pulse_violations") != 0
        or _integer(values, "candidate_checkpoint_reopened") != 1
        or _integer(values, "new_cells", minimum=1)
        != len(topology.get("new_cells") or [])
        or _integer(values, "new_fdres", minimum=1) != topology.get("new_fdres")
        or _integer(values, "new_luts", minimum=1) != topology.get("new_luts")
    ):
        raise ValueError("memory-forward routed identity checks failed")
    resources = {
        "ff": _integer(values, "output_fdre_count")
        - _integer(values, "input_fdre_count"),
        "lut": _integer(values, "output_lut_count")
        - _integer(values, "input_lut_count"),
        "dsp": _integer(values, "output_dsp_count")
        - _integer(values, "input_dsp_count"),
        "carry": _integer(values, "output_carry_count")
        - _integer(values, "input_carry_count"),
    }
    return {
        "contract": values["contract"],
        "backend": values["backend"],
        "operator_class": "MEMORY_WRITE_BUFFER_FORWARD",
        "source_dcp_sha256": expected_source_sha256,
        "target_memory_fingerprint": values["target_memory_fingerprint"],
        "family_ref": family_ref,
        "period_ns": _float(values, "output_period_ns"),
        "input_wns_ns": _float(values, "input_wns_ns"),
        "output_wns_ns": _float(values, "output_wns_ns"),
        "output_whs_ns": _float(values, "output_whs_ns"),
        "new_cells": _integer(values, "new_cells", minimum=1),
        "new_fdres": _integer(values, "new_fdres", minimum=1),
        "new_luts": _integer(values, "new_luts", minimum=1),
        "resource_delta": resources,
        "route_errors": 0,
        "pulse_violations": 0,
        "clock_identity_preserved": True,
        "candidate_checkpoint_reopened": True,
        "topology_manifest_sha256": topology.get("manifest_sha256"),
        "report_digest": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


__all__ = [
    "build_probe_script", "load_private_manifest", "load_probe_report",
    "parse_mutation_result", "recover_and_write_contract", "render_selected_family",
    "resolve_private_family",
]
