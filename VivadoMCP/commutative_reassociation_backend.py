"""Name-private recovery and ECO plumbing for commutative reductions."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any, Iterable


_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT_DIR = _ROOT / "scripts"

_PROBE_NEGATIVE_REASONS = (
    "no registered LUT/CARRY reduction found on live paths",
    "selected reduction endpoint word is incomplete",
    "selected endpoint word is not contiguous",
    "selected reduction has fewer than two complete operand words",
)


def probe_no_report_reason(output: str) -> str:
    """Recover an expected semantic miss from Vivado's interactive output."""
    text = str(output or "")
    for reason in _PROBE_NEGATIVE_REASONS:
        if reason in text:
            return reason
    return ""


def _tcl_brace(value: Path | str) -> str:
    text = str(value)
    if any(char in text for char in ("}", "\r", "\n")):
        raise ValueError("commutative reassociation path contains unsafe Tcl characters")
    return "{" + text + "}"


def build_script(script_name: str, arguments: Iterable[Path | str]) -> str:
    if script_name not in {
        "probe_commutative_reduction.tcl",
        "apply_commutative_operand_reassociation.tcl",
    }:
        raise ValueError("unsupported commutative reassociation script")
    source = (_SCRIPT_DIR / script_name).read_text(encoding="utf-8")
    source = re.sub(r"\nexit 0\s*\Z", "\n", source)
    argv = " ".join(_tcl_brace(item) for item in arguments)
    return f"set argv [list {argv}]\n" + source


def invalidate_probe_cache(report: Path) -> None:
    """Remove only the exact derived probe cache for a changed seed."""
    contract_path = report.with_name(report.name + ".contract.json")
    cone_dir = report.with_name(report.name + ".cone")
    report.unlink(missing_ok=True)
    contract_path.unlink(missing_ok=True)
    if cone_dir.exists():
        if cone_dir.is_symlink() or not cone_dir.is_dir():
            raise RuntimeError("commutative reduction cone cache is unsafe")
        if cone_dir.parent != report.parent:
            raise RuntimeError("commutative reduction cone cache escaped run root")
        shutil.rmtree(cone_dir)


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream, delimiter="\t"))


def _details(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in str(value or "").split(";"):
        if "=" in item:
            key, raw = item.split("=", 1)
            result[key] = raw
    return result


def _int(value: Any, field: str) -> int:
    try:
        result = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"commutative reassociation {field} is not an integer") from exc
    if result < 0:
        raise ValueError(f"commutative reassociation {field} is negative")
    return result


def parse_probe_report(path: Path) -> dict[str, Any]:
    rows = _rows(path)
    summary_rows = [row for row in rows if row.get("kind") == "summary"]
    target_rows = [row for row in rows if row.get("kind") == "target"]
    operand_rows = [row for row in rows if row.get("kind") == "operand"]
    if len(summary_rows) != 1 or len(target_rows) != 1 or len(operand_rows) < 2:
        raise ValueError("commutative reduction probe report is incomplete")
    summary = _details(summary_rows[0].get("details", ""))
    target = _details(target_rows[0].get("details", ""))
    width = _int(target_rows[0].get("count"), "word width")
    operand_count = _int(target.get("operand_count"), "operand count")
    if operand_count != len(operand_rows):
        raise ValueError("commutative reduction operand inventory changed")
    stems = [str(row.get("object") or "") for row in operand_rows]
    critical = str(target.get("critical_source") or "")
    if not critical or critical not in stems or len(set(stems)) != len(stems):
        raise ValueError("commutative reduction critical owner is incomplete")
    candidates = [stem for stem in stems if stem != critical]
    return {
        "public": {
            "status": "success",
            "kind": "commutative_reassociation_live_probe",
            "version": 1,
            "read_only": True,
            "fresh_rediscovery": True,
            "operator_class": "REGISTERED_COMMUTATIVE_MODULAR_REDUCTION",
            "relation": "N x W -> W modulo 2^W",
            "word_width": width,
            "operand_count": operand_count,
            "target_path_rank": _int(summary.get("target_rank"), "target path rank"),
            "candidate_pair_count": len(candidates),
            "candidate_identity_private": True,
            "selection_uncertainty": "high_until_changed-net route measurement",
            "risk_tier": "exploratory_measurement",
            "action_assessment": {
                "status": "measurement_candidate_available",
                "skill": "operator_rewrite",
                "target": "commutative_operand_bus_slot_reassociation_v1",
                "expected_mechanism": (
                    "preserve the registered control transaction and exact modular "
                    "reduction while exchanging two complete symmetric operand buses "
                    "between physically unequal internal tree slots"
                ),
                "failure_update": (
                    "retain the seed and record the tested slot pair as negative; "
                    "reprofile the current owner before another family action"
                ),
                "not_a_hard_trigger": True,
                "luna_owns_backend_selection": True,
            },
        },
        "private": {
            "target_stem": str(target_rows[0].get("object") or ""),
            "critical_source_stem": critical,
            "peer_stems": candidates,
        },
    }


def parse_backend_report(path: Path) -> dict[str, Any]:
    values = {
        str(row.get("metric") or ""): str(row.get("value") or "")
        for row in _rows(path)
    }
    required = {
        "contract", "backend", "word_width", "changed_net_count",
        "moved_pin_count", "latency_delta_cycles", "input_period_ns",
        "input_wns_ns", "output_period_ns", "output_wns_ns", "route_errors",
    }
    if not required.issubset(values):
        raise ValueError("commutative reassociation backend report is incomplete")
    if (
        values["contract"] != "COMMUTATIVE_OPERAND_BUS_SLOT_REASSOCIATION"
        or values["backend"] != "commutative_operand_bus_slot_reassociation_v1"
        or values["input_period_ns"] != values["output_period_ns"]
        or _int(values["route_errors"], "route errors") != 0
        or _int(values["latency_delta_cycles"], "latency delta") != 0
    ):
        raise ValueError("commutative reassociation candidate failed route/clock checks")
    return {
        "contract": values["contract"],
        "backend": values["backend"],
        "word_width": _int(values["word_width"], "word width"),
        "changed_net_count": _int(values["changed_net_count"], "changed net count"),
        "moved_pin_count": _int(values["moved_pin_count"], "moved pin count"),
        "latency_delta_cycles": 0,
        "period_ns": float(values["output_period_ns"]),
        "input_wns_ns": float(values["input_wns_ns"]),
        "output_wns_ns": float(values["output_wns_ns"]),
        "route_errors": 0,
        "report_digest": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def load_contract(path: Path) -> dict[str, Any]:
    body = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(body, dict):
        raise ValueError("commutative reassociation contract is malformed")
    return body


__all__ = [
    "build_script", "invalidate_probe_cache", "load_contract",
    "parse_backend_report", "parse_probe_report", "probe_no_report_reason",
]
