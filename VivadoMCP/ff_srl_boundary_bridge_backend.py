"""Framework plumbing for exact direct-FF to SRL boundary bridges."""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable


_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "apply_ff_srl_boundary_bridge.tcl"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_FAMILY_RE = re.compile(r"ff_srl_[1-9][0-9]*")


def _tcl_brace(value: Path | str) -> str:
    text = str(value)
    if any(char in text for char in ("}", "\r", "\n")):
        raise ValueError("FF-to-SRL path contains unsafe Tcl characters")
    return "{" + text + "}"


def build_script(arguments: Iterable[Path | str]) -> str:
    source = _SCRIPT.read_text(encoding="utf-8")
    source = re.sub(r"\nexit 0\s*\Z", "\n", source)
    argv = " ".join(_tcl_brace(item) for item in arguments)
    return f"set argv [list {argv}]\n" + source


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
    ).hexdigest()


def _values(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
        raise ValueError("FF-to-SRL report is unavailable")
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    values: dict[str, str] = {}
    for row in rows:
        key = str(row.get("metric") or "")
        if not key or key in values:
            raise ValueError("FF-to-SRL report repeats or omits a metric")
        values[key] = str(row.get("value") or "")
    return values


def _integer(values: dict[str, str], field: str, *, minimum: int = 0) -> int:
    try:
        value = int(values[field])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"FF-to-SRL {field} is invalid") from exc
    if value < minimum:
        raise ValueError(f"FF-to-SRL {field} is below {minimum}")
    return value


def _float(values: dict[str, str], field: str) -> float:
    try:
        return float(values[field])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"FF-to-SRL {field} is invalid") from exc


def _valid_b64(value: str) -> bool:
    try:
        return bool(value) and base64.b64encode(
            base64.b64decode(value, validate=True)
        ).decode("ascii") == value
    except (ValueError, UnicodeError):
        return False


def parse_private_manifest(
    path: Path, *, expected_source_sha256: str
) -> dict[str, Any]:
    if (
        _SHA256_RE.fullmatch(expected_source_sha256) is None
        or path.is_symlink()
        or not path.is_file()
        or path.stat().st_size <= 0
    ):
        raise ValueError("FF-to-SRL private manifest is unavailable")
    source = None
    declarations: dict[str, tuple[str, int, int, str]] = {}
    chains: dict[str, list[tuple[str, str, str]]] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        fields = raw.split("\t")
        if fields[:1] == ["source_dcp_sha256"] and len(fields) == 2:
            if source is not None:
                raise ValueError("FF-to-SRL manifest repeats source identity")
            source = fields[1]
        elif fields[:1] == ["family"] and len(fields) == 6:
            ref, signature, count_text, length_text, ce_mode = fields[1:]
            if (
                _FAMILY_RE.fullmatch(ref) is None
                or ref in declarations
                or not _valid_b64(signature)
                or ce_mode not in {"constant_high", "dynamic"}
            ):
                raise ValueError("FF-to-SRL manifest family is invalid")
            try:
                count, length = int(count_text), int(length_text)
            except ValueError as exc:
                raise ValueError("FF-to-SRL manifest family shape is invalid") from exc
            if count < 1 or not 3 <= length <= 33:
                raise ValueError("FF-to-SRL manifest family shape is unsupported")
            declarations[ref] = (signature, count, length, ce_mode)
        elif fields[:1] == ["chain"] and len(fields) == 5:
            ref, names, input_net, output_net = fields[1:]
            if (
                _FAMILY_RE.fullmatch(ref) is None
                or any(not _valid_b64(item) for item in (names, input_net, output_net))
            ):
                raise ValueError("FF-to-SRL manifest chain is invalid")
            chains.setdefault(ref, []).append((names, input_net, output_net))
        else:
            raise ValueError("FF-to-SRL manifest row is malformed")
    if source != expected_source_sha256 or set(chains) != set(declarations):
        raise ValueError("FF-to-SRL manifest identity or family set changed")
    families = {}
    for ref, (signature, count, length, ce_mode) in declarations.items():
        records = sorted(chains[ref])
        if len(records) != count or len(set(records)) != count:
            raise ValueError("FF-to-SRL manifest chain set is inconsistent")
        families[ref] = {
            "signature": signature,
            "chain_count": count,
            "chain_length": length,
            "ce_mode": ce_mode,
            "mutation_target_digest": _digest(records),
        }
    return {
        "source_dcp_sha256": source,
        "families": families,
        "manifest_digest": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def parse_probe_report(
    path: Path, *, manifest_path: Path, expected_source_sha256: str
) -> dict[str, Any]:
    values = _values(path)
    required = {
        "contract", "backend", "source_dcp_sha256", "family_count",
        "input_period_ns", "input_waveform", "input_clock_source",
        "input_wns_ns",
    }
    if not required.issubset(values):
        raise ValueError("FF-to-SRL probe report is incomplete")
    if (
        values["contract"] != "FF_SRL_BOUNDARY_BRIDGE_V1"
        or values["backend"] != "ff_srl_boundary_bridge_v1"
        or values["source_dcp_sha256"] != expected_source_sha256
    ):
        raise ValueError("FF-to-SRL probe identity changed")
    manifest = parse_private_manifest(
        manifest_path, expected_source_sha256=expected_source_sha256
    )
    family_count = _integer(values, "family_count")
    refs = sorted(
        {
            match.group(1)
            for key in values
            if (match := re.fullmatch(r"family\.(ff_srl_[1-9][0-9]*)\..+", key))
        },
        key=lambda ref: int(ref.rsplit("_", 1)[1]),
    )
    if len(refs) != family_count or set(refs) != set(manifest["families"]):
        raise ValueError("FF-to-SRL public/private family sets differ")
    families = []
    for ref in refs:
        prefix = f"family.{ref}."
        fields = {
            "chain_count", "chain_length", "ce_mode", "path_count",
            "endpoint_count", "worst_path_overlap",
        }
        if any(prefix + field not in values for field in fields):
            raise ValueError("FF-to-SRL family report is incomplete")
        private = manifest["families"][ref]
        count = _integer(values, prefix + "chain_count", minimum=1)
        length = _integer(values, prefix + "chain_length", minimum=3)
        ce_mode = values[prefix + "ce_mode"]
        if (
            count != private["chain_count"]
            or length != private["chain_length"]
            or length > 33
            or ce_mode != private["ce_mode"]
        ):
            raise ValueError("FF-to-SRL family contract is inconsistent")
        families.append({
            "family_ref": ref,
            "chain_count": count,
            "chain_length": length,
            "ce_mode": ce_mode,
            "top500_path_overlap_count": _integer(values, prefix + "path_count"),
            "top500_endpoint_count": _integer(values, prefix + "endpoint_count"),
            "worst_path_overlap": bool(
                _integer(values, prefix + "worst_path_overlap")
            ),
            "retained_boundary_ff_per_chain": 1,
            "replacement_primitive": "SRLC32E",
            "enabled_cycle_latency": length,
            "mutation_target_digest": private["mutation_target_digest"],
            "risk": (
                "dynamic CE can move the live wall to SRL write-enable routing"
                if ce_mode == "dynamic" else
                "constant-high CE is structurally cheap but may be outside the live wall"
            ),
        })
    public = {
        "status": "success" if families else "no_candidate",
        "kind": "ff_srl_boundary_bridge_contract",
        "version": 1,
        "fresh_rediscovery": True,
        "read_only": True,
        "operator_class": "DIRECT_FF_CHAIN_TO_SRL_BOUNDARY_BRIDGE",
        "source_dcp_sha256": expected_source_sha256,
        "clock": {
            "period_ns": _float(values, "input_period_ns"),
            "input_wns_ns": _float(values, "input_wns_ns"),
        },
        "families": families,
        "summary": {
            "family_count": family_count,
            "chain_count": sum(item["chain_count"] for item in families),
            "timing_wall_family_count": sum(
                1 for item in families if item["top500_path_overlap_count"] > 0
            ),
            "mutation_eligible": bool(families),
        },
        "proof": {
            "maximal_direct_chain_recovered": bool(families),
            "clock_ce_reset_init_uniform": bool(families),
            "intermediate_side_fanout_absent": bool(families),
            "enabled_cycle_latency_preserved": bool(families),
            "exact_cell_names_are_private": True,
            "does_not_establish_positive_routed_qor": True,
        },
        "private_mutation_manifest_retained": True,
        "private_manifest_digest": manifest["manifest_digest"],
    }
    unsigned = json.loads(json.dumps(public))
    public["contract_digest"] = _digest(unsigned)
    public["action_assessment"] = {
        "status": (
            "measurement_candidates_available" if families
            else "no_exact_direct_ff_chain_observed"
        ),
        "candidate_actions": [
            {
                "skill": "operator_rewrite",
                "target": "ff_srl_boundary_bridge_v1",
                "params": {"family_ref": item["family_ref"]},
            }
            for item in families
        ],
        "expected_mechanism": (
            "replace the transport-only tail by one SRLC32E while retaining the "
            "first hierarchy-boundary FF and exact enabled-cycle latency"
        ),
        "failure_update": (
            "if the SRL clock-to-Q or dynamic write-enable wall takes over, "
            "lower this family and refresh live timing rather than blacklisting the class"
        ),
        "not_a_hard_trigger": True,
        "luna_owns_family_selection": True,
    }
    unsigned = json.loads(json.dumps(public))
    unsigned.pop("contract_digest", None)
    public["contract_digest"] = _digest(unsigned)
    return public


def parse_backend_report(
    path: Path,
    *,
    manifest: dict[str, Any],
    family_ref: str,
    contract: dict[str, Any],
) -> dict[str, Any]:
    values = _values(path)
    required = {
        "contract", "backend", "source_dcp_sha256", "selected_family_ref",
        "selected_family_signature", "selected_chain_count",
        "selected_chain_length", "selected_ce_mode",
        "selected_top500_path_overlap_count", "selected_top500_endpoint_count",
        "selected_worst_path_overlap", "input_period_ns", "input_waveform",
        "input_clock_source", "input_wns_ns", "output_period_ns",
        "output_waveform", "output_clock_source", "output_wns_ns",
        "output_whs_ns", "input_srl_count", "output_srl_count",
        "input_ff_count", "output_ff_count", "route_errors",
        "pulse_violations", "candidate_checkpoint_reopened",
    }
    if not required.issubset(values):
        raise ValueError("FF-to-SRL backend report is incomplete")
    private = manifest.get("families", {}).get(family_ref)
    public = next(
        (
            item for item in contract.get("families") or []
            if item.get("family_ref") == family_ref
        ),
        None,
    )
    if (
        not isinstance(private, dict)
        or not isinstance(public, dict)
        or values["contract"] != "FF_SRL_BOUNDARY_BRIDGE_V1"
        or values["backend"] != "ff_srl_boundary_bridge_v1"
        or values["source_dcp_sha256"] != manifest["source_dcp_sha256"]
        or values["selected_family_ref"] != family_ref
        or values["selected_family_signature"] != private["signature"]
        or _integer(values, "selected_chain_count", minimum=1)
        != private["chain_count"]
        or _integer(values, "selected_chain_length", minimum=3)
        != private["chain_length"]
        or values["selected_ce_mode"] != private["ce_mode"]
        or values["input_period_ns"] != values["output_period_ns"]
        or values["input_waveform"] != values["output_waveform"]
        or values["input_clock_source"] != values["output_clock_source"]
        or _integer(values, "route_errors") != 0
        or _integer(values, "pulse_violations") != 0
        or _float(values, "output_whs_ns") < 0.0
        or _integer(values, "candidate_checkpoint_reopened") != 1
    ):
        raise ValueError("FF-to-SRL routed identity checks failed")
    count = private["chain_count"]
    length = private["chain_length"]
    resource_delta = {
        "srl": _integer(values, "output_srl_count")
        - _integer(values, "input_srl_count"),
        "ff": _integer(values, "output_ff_count")
        - _integer(values, "input_ff_count"),
    }
    if resource_delta != {"srl": count, "ff": -count * (length - 1)}:
        raise ValueError("FF-to-SRL resource accounting is inconsistent")
    return {
        "contract": values["contract"],
        "backend": values["backend"],
        "operator_class": "DIRECT_FF_CHAIN_TO_SRL_BOUNDARY_BRIDGE",
        "source_dcp_sha256": manifest["source_dcp_sha256"],
        "contract_digest": contract["contract_digest"],
        "private_manifest_digest": manifest["manifest_digest"],
        "family_ref": family_ref,
        "mutation_target_digest": private["mutation_target_digest"],
        "selected_chain_count": count,
        "selected_chain_length": length,
        "ce_mode": private["ce_mode"],
        "top500_path_overlap_count": _integer(
            values, "selected_top500_path_overlap_count"
        ),
        "top500_endpoint_count": _integer(
            values, "selected_top500_endpoint_count"
        ),
        "worst_path_overlap": bool(
            _integer(values, "selected_worst_path_overlap")
        ),
        "period_ns": _float(values, "output_period_ns"),
        "input_wns_ns": _float(values, "input_wns_ns"),
        "output_wns_ns": _float(values, "output_wns_ns"),
        "output_whs_ns": _float(values, "output_whs_ns"),
        "route_errors": 0,
        "pulse_violations": 0,
        "clock_identity_preserved": True,
        "enabled_cycle_latency_preserved": True,
        "hierarchy_boundary_ff_retained": True,
        "private_manifest_identity_replayed": True,
        "resource_delta": resource_delta,
        "report_digest": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


__all__ = [
    "build_script", "parse_backend_report", "parse_private_manifest",
    "parse_probe_report",
]
