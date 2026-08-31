"""Framework plumbing for registered observable dead-state elimination."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable


_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "apply_registered_dead_state_manifest.tcl"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _tcl_brace(value: Path | str) -> str:
    text = str(value)
    if any(char in text for char in ("}", "\r", "\n")):
        raise ValueError("registered dead-state path contains unsafe Tcl characters")
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


def parse_private_manifest(
    path: Path, *, expected_source_sha256: str
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
        raise ValueError("registered dead-state private manifest is unavailable")
    scalar: dict[str, str] = {}
    names = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        fields = raw.split("\t")
        if len(fields) != 2:
            raise ValueError("registered dead-state private manifest row is malformed")
        kind, value = fields
        if kind == "cell":
            if not value or any(char in value for char in ("\t", "\r", "\n")):
                raise ValueError("registered dead-state private cell identity is invalid")
            names.append(value)
        elif kind in {
            "source_dcp_sha256", "contract_digest",
            "mutation_target_digest", "cell_count",
        }:
            if kind in scalar:
                raise ValueError("registered dead-state private manifest repeats a scalar")
            scalar[kind] = value
        else:
            raise ValueError("registered dead-state private manifest row kind is unknown")
    if (
        scalar.get("source_dcp_sha256") != expected_source_sha256
        or _SHA256_RE.fullmatch(str(scalar.get("contract_digest") or "")) is None
        or _SHA256_RE.fullmatch(str(scalar.get("mutation_target_digest") or "")) is None
    ):
        raise ValueError("registered dead-state private manifest identity changed")
    try:
        declared = int(scalar["cell_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("registered dead-state private manifest count is invalid") from exc
    if declared <= 0 or declared != len(names) or len(set(names)) != len(names):
        raise ValueError("registered dead-state private manifest cell set is inconsistent")
    if _digest(sorted(names)) != scalar["mutation_target_digest"]:
        raise ValueError("registered dead-state private manifest target digest changed")
    return {
        "source_dcp_sha256": expected_source_sha256,
        "contract_digest": scalar["contract_digest"],
        "mutation_target_digest": scalar["mutation_target_digest"],
        "cell_count": declared,
    }


def _values(path: Path) -> dict[str, str]:
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
        raise ValueError(f"registered dead-state {field} is invalid") from exc
    if value < minimum:
        raise ValueError(f"registered dead-state {field} is below {minimum}")
    return value


def _float(values: dict[str, str], field: str) -> float:
    try:
        return float(values[field])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"registered dead-state {field} is invalid") from exc


def parse_backend_report(
    path: Path, *, manifest: dict[str, Any]
) -> dict[str, Any]:
    values = _values(path)
    required = {
        "contract", "backend", "source_dcp_sha256", "contract_digest",
        "mutation_target_digest", "mutation_target_cell_count",
        "selected_dsp_count", "selected_ff_count", "selected_lut_count",
        "selected_carry_count", "input_period_ns", "input_waveform",
        "input_clock_source", "input_wns_ns", "output_period_ns",
        "output_waveform", "output_clock_source", "output_wns_ns",
        "route_errors", "input_primitive_count", "output_primitive_count",
        "input_dsp_count", "output_dsp_count", "input_ff_count",
        "output_ff_count", "input_lut_count", "output_lut_count",
        "input_carry_count", "output_carry_count",
    }
    if not required.issubset(values):
        raise ValueError("registered dead-state backend report is incomplete")
    if (
        values["contract"] != "REGISTERED_OBSERVABLE_CLOSURE_DEAD_STATE_V1"
        or values["backend"] != "registered_dead_state_elimination_v1"
        or values["source_dcp_sha256"] != manifest["source_dcp_sha256"]
        or values["contract_digest"] != manifest["contract_digest"]
        or values["mutation_target_digest"] != manifest["mutation_target_digest"]
        or _integer(values, "mutation_target_cell_count", minimum=1)
        != manifest["cell_count"]
        or values["input_period_ns"] != values["output_period_ns"]
        or values["input_waveform"] != values["output_waveform"]
        or values["input_clock_source"] != values["output_clock_source"]
        or _integer(values, "route_errors") != 0
    ):
        raise ValueError("registered dead-state routed identity checks failed")
    selected_dsp = _integer(values, "selected_dsp_count")
    selected_ff = _integer(values, "selected_ff_count")
    selected_lut = _integer(values, "selected_lut_count")
    selected_carry = _integer(values, "selected_carry_count")
    if selected_dsp + selected_ff < 1:
        raise ValueError("registered dead-state removed no registered producer")
    resources = {
        "dsp": _integer(values, "output_dsp_count") - _integer(values, "input_dsp_count"),
        "ff": _integer(values, "output_ff_count") - _integer(values, "input_ff_count"),
        "lut": _integer(values, "output_lut_count") - _integer(values, "input_lut_count"),
        "carry": _integer(values, "output_carry_count") - _integer(values, "input_carry_count"),
        "primitive": _integer(values, "output_primitive_count") - _integer(values, "input_primitive_count"),
    }
    if (
        resources["dsp"] != -selected_dsp
        or resources["ff"] != -selected_ff
        or resources["lut"] > -selected_lut
        or resources["carry"] > -selected_carry
        or resources["primitive"] >= 0
    ):
        raise ValueError("registered dead-state resource accounting is inconsistent")
    return {
        "contract": values["contract"],
        "backend": values["backend"],
        "operator_class": "REGISTERED_OBSERVABLE_DEAD_STATE_FAMILY",
        "source_dcp_sha256": values["source_dcp_sha256"],
        "contract_digest": values["contract_digest"],
        "mutation_target_digest": values["mutation_target_digest"],
        "mutation_target_cell_count": manifest["cell_count"],
        "selected_dsp_count": selected_dsp,
        "selected_ff_count": selected_ff,
        "selected_lut_count": selected_lut,
        "selected_carry_count": selected_carry,
        "period_ns": _float(values, "output_period_ns"),
        "input_wns_ns": _float(values, "input_wns_ns"),
        "output_wns_ns": _float(values, "output_wns_ns"),
        "route_errors": 0,
        "clock_identity_preserved": True,
        "resource_delta": resources,
        "worst_path": {
            "startpoint": values.get("worst_startpoint", ""),
            "endpoint": values.get("worst_endpoint", ""),
        },
        "report_digest": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


__all__ = [
    "build_script", "parse_backend_report", "parse_private_manifest",
]
