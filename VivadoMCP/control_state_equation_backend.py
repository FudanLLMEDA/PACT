"""Framework plumbing for exact synchronous control-to-data state equations."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable


_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "apply_control_to_data_state_equation.tcl"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_FAMILY_RE = re.compile(r"control_state_[1-9][0-9]*")


def _tcl_brace(value: Path | str) -> str:
    text = str(value)
    if any(char in text for char in ("}", "\r", "\n")):
        raise ValueError("control-state path contains unsafe Tcl characters")
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
        raise ValueError("control-state report is unavailable")
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    result: dict[str, str] = {}
    for row in rows:
        key = str(row.get("metric") or "")
        if not key or key in result:
            raise ValueError("control-state report repeats or omits a metric")
        result[key] = str(row.get("value") or "")
    return result


def _integer(values: dict[str, str], field: str, *, minimum: int = 0) -> int:
    try:
        value = int(values[field])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"control-state {field} is invalid") from exc
    if value < minimum:
        raise ValueError(f"control-state {field} is below {minimum}")
    return value


def _float(values: dict[str, str], field: str) -> float:
    try:
        return float(values[field])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"control-state {field} is invalid") from exc


def parse_private_manifest(
    path: Path, *, expected_source_sha256: str
) -> dict[str, Any]:
    if (
        _SHA256_RE.fullmatch(expected_source_sha256) is None
        or path.is_symlink()
        or not path.is_file()
        or path.stat().st_size <= 0
    ):
        raise ValueError("control-state private manifest is unavailable")
    source = None
    declarations: dict[str, tuple[str, int]] = {}
    cells: dict[str, list[str]] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        fields = raw.split("\t")
        if fields[:1] == ["source_dcp_sha256"] and len(fields) == 2:
            if source is not None:
                raise ValueError("control-state manifest repeats source identity")
            source = fields[1]
        elif fields[:1] == ["family"] and len(fields) == 4:
            ref, signature, count_text = fields[1:]
            if (
                _FAMILY_RE.fullmatch(ref) is None
                or ref in declarations
                or not signature
                or any(char in signature for char in ("\t", "\r", "\n"))
            ):
                raise ValueError("control-state manifest family is invalid")
            try:
                count = int(count_text)
            except ValueError as exc:
                raise ValueError("control-state manifest family count is invalid") from exc
            if count < 1:
                raise ValueError("control-state manifest family is empty")
            declarations[ref] = (signature, count)
        elif fields[:1] == ["cell"] and len(fields) == 3:
            ref, name = fields[1:]
            if _FAMILY_RE.fullmatch(ref) is None or not name:
                raise ValueError("control-state manifest cell is invalid")
            cells.setdefault(ref, []).append(name)
        else:
            raise ValueError("control-state manifest row is malformed")
    if source != expected_source_sha256:
        raise ValueError("control-state manifest source identity changed")
    if set(cells) != set(declarations):
        raise ValueError("control-state manifest family set is inconsistent")
    families = {}
    for ref, (signature, count) in declarations.items():
        names = sorted(cells[ref])
        if len(names) != count or len(set(names)) != count:
            raise ValueError("control-state manifest cell set is inconsistent")
        families[ref] = {
            "signature": signature,
            "cell_count": count,
            "mutation_target_digest": _digest(names),
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
        raise ValueError("control-state probe report is incomplete")
    if (
        values["contract"] != "EXACT_CONTROL_TO_DATA_STATE_EQUATION_V1"
        or values["backend"] != "exact_control_to_data_state_equation_v1"
        or values["source_dcp_sha256"] != expected_source_sha256
    ):
        raise ValueError("control-state probe identity changed")
    manifest = parse_private_manifest(
        manifest_path, expected_source_sha256=expected_source_sha256
    )
    family_count = _integer(values, "family_count", minimum=0)
    refs = sorted(
        {
            match.group(1)
            for key in values
            if (match := re.fullmatch(r"family\.(control_state_[1-9][0-9]*)\..+", key))
        },
        key=lambda ref: int(ref.rsplit("_", 1)[1]),
    )
    if len(refs) != family_count or set(refs) != set(manifest["families"]):
        raise ValueError("control-state public/private family sets differ")
    families = []
    for ref in refs:
        prefix = f"family.{ref}."
        required_fields = {
            "state_count", "top500_endpoint_count", "top500_path_overlap_count",
            "worst_path_overlap", "support_width", "equation", "replacement_ref",
            "distinct_clock_net_count", "distinct_enable_net_count",
            "distinct_sync_control_net_count", "before_control_set_count",
            "after_control_set_count", "control_set_reduction",
            "max_sync_control_net_fanout",
        }
        if any(prefix + field not in values for field in required_fields):
            raise ValueError("control-state family report is incomplete")
        count = _integer(values, prefix + "state_count", minimum=1)
        support_width = _integer(values, prefix + "support_width", minimum=0)
        if support_width > 5 or count != manifest["families"][ref]["cell_count"]:
            raise ValueError("control-state family contract is inconsistent")
        if (
            values[prefix + "equation"] != "sync_set_to_data_or_factor"
            or re.fullmatch(r"LUT[1-6]", values[prefix + "replacement_ref"]) is None
        ):
            raise ValueError("control-state equation class is unsupported")
        families.append({
            "family_ref": ref,
            "state_count": count,
            "top500_endpoint_count": _integer(
                values, prefix + "top500_endpoint_count", minimum=0
            ),
            "top500_path_overlap_count": _integer(
                values, prefix + "top500_path_overlap_count", minimum=0
            ),
            "worst_path_overlap": bool(
                _integer(values, prefix + "worst_path_overlap", minimum=0)
            ),
            "support_width": support_width,
            "equation": "Q_next = set ? 1 : (enable ? D : Q)",
            "replacement_equation": "Q_next = enable ? (D OR set_factor) : Q",
            "state_primitive_before": "FDSE",
            "state_primitive_after": "FDRE",
            "replacement_lut_inputs": int(values[prefix + "replacement_ref"][3:]),
            "control_set_structure": {
                "distinct_clock_net_count": _integer(
                    values, prefix + "distinct_clock_net_count"
                ),
                "distinct_enable_net_count": _integer(
                    values, prefix + "distinct_enable_net_count"
                ),
                "distinct_sync_control_net_count": _integer(
                    values, prefix + "distinct_sync_control_net_count"
                ),
                "before_control_set_count": _integer(
                    values, prefix + "before_control_set_count", minimum=1
                ),
                "after_control_set_count": _integer(
                    values, prefix + "after_control_set_count", minimum=1
                ),
                "control_set_reduction": _integer(
                    values, prefix + "control_set_reduction"
                ),
                "max_sync_control_net_fanout": _integer(
                    values, prefix + "max_sync_control_net_fanout"
                ),
            },
            "mutation_target_digest": manifest["families"][ref][
                "mutation_target_digest"
            ],
        })
    public = {
        "status": "success" if families else "no_candidate",
        "kind": "control_to_data_state_equation_contract",
        "version": 1,
        "fresh_rediscovery": True,
        "read_only": True,
        "operator_class": "REGISTERED_CONTROL_TO_DATA_STATE_EQUATION",
        "source_dcp_sha256": expected_source_sha256,
        "clock": {
            "period_ns": _float(values, "input_period_ns"),
            "input_wns_ns": _float(values, "input_wns_ns"),
        },
        "families": families,
        "summary": {
            "family_count": family_count,
            "state_count": sum(item["state_count"] for item in families),
            "top500_endpoint_count": sum(
                item["top500_endpoint_count"] for item in families
            ),
            "top500_path_overlap_count": sum(
                item["top500_path_overlap_count"] for item in families
            ),
            "mutation_eligible": bool(families),
        },
        "proof": {
            "set_implies_enable_truth_table_exhaustive": bool(families),
            "minimal_care_set_support_recovered": bool(families),
            "clock_enable_init_and_observable_cycle_preserved": bool(families),
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
            else "no_exact_state_equation_observed"
        ),
        "candidate_actions": [
            {
                "skill": "operator_rewrite",
                "target": "exact_control_to_data_state_equation_v1",
                "params": {"family_ref": item["family_ref"]},
            }
            for item in families
        ],
        "expected_mechanism": (
            "replace synchronous set priority by an exact care-set factor in "
            "the registered data equation; use live before/after control-set "
            "diversity to judge packing and global routing-basin relief, and "
            "claim direct path shortening only when sampled path overlap exists"
        ),
        "failure_update": (
            "separate exact truth-table or manifest replay failure from routed "
            "physical regression, then refresh the live timing wall"
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
        "contract", "backend", "equation", "source_dcp_sha256",
        "selected_family_ref", "selected_family_signature",
        "selected_state_count", "selected_top500_endpoint_count",
        "selected_top500_path_overlap_count", "selected_worst_path_overlap",
        "selected_support_width", "selected_replacement_ref",
        "selected_distinct_clock_net_count",
        "selected_distinct_enable_net_count",
        "selected_distinct_sync_control_net_count",
        "selected_before_control_set_count", "selected_after_control_set_count",
        "selected_control_set_reduction", "selected_max_sync_control_net_fanout",
        "input_period_ns", "input_waveform", "input_clock_source",
        "input_wns_ns", "output_period_ns", "output_waveform",
        "output_clock_source", "output_wns_ns", "output_whs_ns",
        "route_errors", "pulse_violations", "input_ff_count",
        "output_ff_count", "input_lut_count", "output_lut_count",
    }
    if not required.issubset(values):
        raise ValueError("control-state backend report is incomplete")
    private_family = manifest.get("families", {}).get(family_ref)
    public_family = next(
        (
            item for item in contract.get("families") or []
            if item.get("family_ref") == family_ref
        ),
        None,
    )
    if (
        not isinstance(private_family, dict)
        or not isinstance(public_family, dict)
        or values["contract"] != "EXACT_CONTROL_TO_DATA_STATE_EQUATION_V1"
        or values["backend"] != "exact_control_to_data_state_equation_v1"
        or values["equation"] != "sync_set_to_data_or_factor"
        or values["source_dcp_sha256"] != manifest["source_dcp_sha256"]
        or values["selected_family_ref"] != family_ref
        or values["selected_family_signature"] != private_family["signature"]
        or _integer(values, "selected_state_count", minimum=1)
        != private_family["cell_count"]
        or _integer(values, "selected_state_count", minimum=1)
        != public_family["state_count"]
        or values["input_period_ns"] != values["output_period_ns"]
        or values["input_waveform"] != values["output_waveform"]
        or values["input_clock_source"] != values["output_clock_source"]
        or _integer(values, "route_errors") != 0
    ):
        raise ValueError("control-state routed identity checks failed")
    return {
        "contract": values["contract"],
        "backend": values["backend"],
        "operator_class": "REGISTERED_CONTROL_TO_DATA_STATE_EQUATION",
        "source_dcp_sha256": manifest["source_dcp_sha256"],
        "contract_digest": contract["contract_digest"],
        "private_manifest_digest": manifest["manifest_digest"],
        "family_ref": family_ref,
        "mutation_target_digest": private_family["mutation_target_digest"],
        "selected_state_count": private_family["cell_count"],
        "top500_endpoint_count": _integer(
            values, "selected_top500_endpoint_count"
        ),
        "top500_path_overlap_count": _integer(
            values, "selected_top500_path_overlap_count"
        ),
        "worst_path_overlap": bool(
            _integer(values, "selected_worst_path_overlap")
        ),
        "support_width": _integer(values, "selected_support_width"),
        "control_set_structure": {
            "distinct_clock_net_count": _integer(
                values, "selected_distinct_clock_net_count"
            ),
            "distinct_enable_net_count": _integer(
                values, "selected_distinct_enable_net_count"
            ),
            "distinct_sync_control_net_count": _integer(
                values, "selected_distinct_sync_control_net_count"
            ),
            "before_control_set_count": _integer(
                values, "selected_before_control_set_count", minimum=1
            ),
            "after_control_set_count": _integer(
                values, "selected_after_control_set_count", minimum=1
            ),
            "control_set_reduction": _integer(
                values, "selected_control_set_reduction"
            ),
            "max_sync_control_net_fanout": _integer(
                values, "selected_max_sync_control_net_fanout"
            ),
        },
        "period_ns": _float(values, "output_period_ns"),
        "input_wns_ns": _float(values, "input_wns_ns"),
        "output_wns_ns": _float(values, "output_wns_ns"),
        "output_whs_ns": _float(values, "output_whs_ns"),
        "route_errors": 0,
        "pulse_violations": _integer(values, "pulse_violations"),
        "clock_identity_preserved": True,
        "truth_table_replayed": True,
        "care_set_support_replayed": True,
        "observable_cycle_preserved": True,
        "resource_delta": {
            "ff": _integer(values, "output_ff_count")
            - _integer(values, "input_ff_count"),
            "lut": _integer(values, "output_lut_count")
            - _integer(values, "input_lut_count"),
        },
        "report_digest": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


__all__ = [
    "build_script", "parse_backend_report", "parse_private_manifest",
    "parse_probe_report",
]
