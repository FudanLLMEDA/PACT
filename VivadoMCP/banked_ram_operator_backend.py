"""Anonymous banked-RAM data/control recovery and local ECO plumbing."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT_DIR = _ROOT / "scripts"
_PROBE_CACHE_ABI = 2


def _tcl_brace(value: Path | str) -> str:
    text = str(value)
    if any(char in text for char in ("}", "\r", "\n")):
        raise ValueError("banked RAM operator path contains unsafe Tcl characters")
    return "{" + text + "}"


def build_script(script_name: str, arguments: Iterable[Path | str]) -> str:
    if script_name not in {
        "probe_banked_ram_control_packet.tcl",
        "apply_banked_ram_control_packet.tcl",
        "apply_registered_ram_boolean_midpoint.tcl",
    }:
        raise ValueError("unsupported banked RAM operator script")
    source = (_SCRIPT_DIR / script_name).read_text(encoding="utf-8")
    source = re.sub(r"\nexit 0\s*\Z", "\n", source)
    argv = " ".join(_tcl_brace(item) for item in arguments)
    return f"set argv [list {argv}]\n" + source


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


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
        raise ValueError(f"banked RAM operator {field} is not an integer") from exc
    if result < 0:
        raise ValueError(f"banked RAM operator {field} is negative")
    return result


def parse_probe_report(path: Path) -> dict[str, Any]:
    rows = _rows(path)
    summaries = [row for row in rows if row.get("kind") == "summary"]
    data_rows = [row for row in rows if row.get("kind") == "data_operator"]
    packet_rows = [row for row in rows if row.get("kind") == "control_packet"]
    source_rows = [row for row in rows if row.get("kind") == "packet_source"]
    sink_rows = [row for row in rows if row.get("kind") == "packet_sink"]
    if len(summaries) != 1:
        raise ValueError("banked RAM data/control probe report is incomplete")
    summary = _details(summaries[0].get("details", ""))
    if (
        _int(summary.get("data_operator_count", 0), "data operator count")
        != len(data_rows)
        or _int(summary.get("control_packet_count", 0), "control packet count")
        != len(packet_rows)
    ):
        raise ValueError("banked RAM probe inventory changed during recovery")

    data_classes = Counter()
    data_ranks: list[int] = []
    for row in data_rows:
        details = _details(row.get("details", ""))
        if (
            details.get("operator") != "RAM_Q_BOOLEAN_REGISTER"
            or details.get("control_contract") != "endpoint_C_CE_reset_INIT"
        ):
            raise ValueError("banked RAM data/control contract is incomplete")
        data_classes[(str(row.get("ref") or ""), details.get("init", ""))] += 1
        data_ranks.append(_int(details.get("path_rank"), "data path rank"))

    source_count_by_group = Counter(str(row.get("group") or "") for row in source_rows)
    sink_count_by_group = Counter(str(row.get("group") or "") for row in sink_rows)
    packet_summaries = []
    packet_ranks: list[int] = []
    for row in packet_rows:
        details = _details(row.get("details", ""))
        group = str(row.get("group") or "")
        if details.get("operator") != "BANKED_RAM_CONTROL_PACKET":
            raise ValueError("banked RAM control packet class is missing")
        registered_sources = _int(
            details.get("registered_source_count"), "registered source count"
        )
        sink_count = _int(row.get("count"), "packet sink count")
        if (
            source_count_by_group[group] < registered_sources
            or sink_count_by_group[group] != sink_count
        ):
            raise ValueError("banked RAM packet source/sink closure is incomplete")
        rank = _int(details.get("earliest_path_rank"), "packet path rank")
        packet_ranks.append(rank)
        packet_summaries.append({
            "registered_source_count": registered_sources,
            "sink_count": sink_count,
            "region_count": _int(details.get("region_count"), "region count"),
            "covered_path_count": _int(
                details.get("covered_path_count"), "covered path count"
            ),
            "earliest_path_rank": rank,
        })

    sample_count = _int(summaries[0].get("count"), "live path count")
    ram_control_path_count = _int(
        summary.get("ram_control_path_count", 0), "RAM control path count"
    )
    if ram_control_path_count > sample_count:
        raise ValueError("banked RAM control path count exceeds the live sample")

    contract = {
        "operator_class": "BANKED_RAM_DATA_CONTROL_OPERATOR",
        "data_operator": {
            "semantic_class": "RAM_Q_BOOLEAN_REGISTER",
            "operator_count": len(data_rows),
            "truth_table_classes": [
                {"primitive": ref, "init": init, "count": count}
                for (ref, init), count in sorted(data_classes.items())
            ],
            "registered_peer_operand_recovered": bool(data_rows),
            "endpoint_clock_ce_reset_init_recovered": bool(data_rows),
            "architectural_latency_delta_cycles": 0,
            "earliest_live_path_rank": min(data_ranks) if data_ranks else None,
        },
        "control_operator": {
            "semantic_class": "BANKED_RAM_CONTROL_PACKET",
            "packet_count": len(packet_rows),
            "packets": packet_summaries,
            "registered_state_or_static_inputs_only": bool(packet_rows),
            "bank_local_ram_pin_ownership_recovered": bool(packet_rows),
            "architectural_latency_delta_cycles": 0,
            "earliest_live_path_rank": min(packet_ranks) if packet_ranks else None,
        },
        "live_wall": {
            "sample_count": sample_count,
            "period_ns": float(summary.get("period_ns", "nan")),
            "ram_control_path_count": ram_control_path_count,
        },
    }
    actions = []
    if data_rows:
        actions.append({
            "skill": "operator_rewrite",
            "target": "registered_ram_boolean_midpoint_relocation_v1",
            "params": {},
            "evidence": {
                "earliest_live_path_rank": min(data_ranks),
                "operator_count": len(data_rows),
                "truth_table_class_count": len(data_classes),
            },
            "expected_mechanism": (
                "retain the exact RAM/registered-peer Boolean equation and endpoint "
                "register controls while relocating the existing LUT/register pair "
                "to the measured gather/scatter midpoint"
            ),
            "failure_update": (
                "reprofile and distinguish residual RAM-data siblings from a newly "
                "exposed address/enable/control packet wall"
            ),
        })
    if packet_rows:
        actions.append({
            "skill": "operator_rewrite",
            "target": "registered_control_packet_localization_v1",
            "params": {},
            "evidence": {
                "earliest_live_path_rank": min(packet_ranks),
                "packet_count": len(packet_rows),
                "covered_path_count": sum(
                    item["covered_path_count"] for item in packet_summaries
                ),
            },
            "expected_mechanism": (
                "preserve the registered state transaction and replicate only the "
                "exact Boolean generator beside its bank-local RAM control consumers"
            ),
            "failure_update": (
                "reprofile the displaced generator-input and sibling packet walls; "
                "do not clone registered state when its D cone becomes cross-region"
            ),
        })
    has_contract = bool(actions)
    public = {
        "status": "success" if has_contract else "no_supported_contract",
        "kind": "banked_ram_data_control_operator_contract",
        "version": 1,
        "fresh_rediscovery": True,
        "read_only": True,
        "structural_boundary_recovered": has_contract,
        "contract": contract,
        "action_assessment": {
            "status": (
                "measurement_candidates_available"
                if has_contract else "no_supported_contract"
            ),
            "candidate_actions": actions,
            "selection_rule": (
                "Luna selects from current live data/control ownership; candidates "
                "are never automatic and an empty inventory rejects only this "
                "banked-RAM data/control hypothesis"
            ),
            "not_a_hard_trigger": True,
            "luna_owns_final_selection": True,
            "remaining_family_exploration_allowed": True,
            "physical_alternative_allowed": True,
        },
        "mutation_eligible": has_contract,
        "proof_status": (
            "measurement_probe_only_final_equivalence_required"
            if has_contract else "negative_live_banked_ram_data_control_screen"
        ),
    }
    public["_private_contract_digest"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return public


def parse_backend_report(path: Path, expected_backend: str) -> dict[str, Any]:
    values = {
        str(row.get("metric") or ""): str(row.get("value") or "")
        for row in _rows(path)
    }
    common = {
        "contract", "backend", "input_period_ns", "input_wns_ns",
        "output_period_ns", "output_wns_ns", "route_errors",
    }
    if not common.issubset(values):
        raise ValueError("banked RAM backend report is incomplete")
    expected_contract = {
        "registered_control_packet_localization_v1": "BANKED_RAM_CONTROL_PACKET",
        "registered_ram_boolean_midpoint_relocation_v1": "REGISTERED_RAM_BOOLEAN_MIDPOINT",
    }.get(expected_backend)
    if (
        expected_contract is None
        or values["contract"] != expected_contract
        or values["backend"] != expected_backend
        or values["input_period_ns"] != values["output_period_ns"]
        or _int(values["route_errors"], "route errors") != 0
    ):
        raise ValueError("banked RAM candidate failed route/clock checks")
    result: dict[str, Any] = {
        "contract": values["contract"],
        "backend": values["backend"],
        "period_ns": float(values["output_period_ns"]),
        "input_wns_ns": float(values["input_wns_ns"]),
        "output_wns_ns": float(values["output_wns_ns"]),
        "route_errors": 0,
        "report_digest": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    integer_fields = {
        "registered_source_count", "localized_sink_count", "covered_path_count",
        "earliest_path_rank", "candidate_count", "moved_operator_count",
        "proof_failures", "latency_delta_cycles",
    }
    for field in integer_fields:
        if field in values:
            result[field] = _int(values[field], field)
    if result.get("proof_failures", 0) != 0 or result.get("latency_delta_cycles", 0) != 0:
        raise ValueError("banked RAM data/control replay failed")
    return result


def stable_digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def write_probe_cache(
    report_path: Path, source_dcp_sha256: str, public_result: dict[str, Any]
) -> Path:
    if re.fullmatch(r"[0-9a-f]{64}", source_dcp_sha256) is None:
        raise ValueError("banked RAM cache source identity is invalid")
    body = {
        "kind": "banked_ram_operator_probe_cache",
        "probe_cache_abi": _PROBE_CACHE_ABI,
        "source_dcp_sha256": source_dcp_sha256,
        "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        "public_result": public_result,
    }
    body["cache_sha256"] = stable_digest(body)
    cache_path = report_path.with_name(report_path.name + ".cache.json")
    temporary = cache_path.with_name(cache_path.name + ".tmp")
    temporary.write_text(json.dumps(body, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    temporary.replace(cache_path)
    return cache_path


def load_probe_cache(report_path: Path, source_dcp_sha256: str) -> dict[str, Any] | None:
    cache_path = report_path.with_name(report_path.name + ".cache.json")
    if not report_path.is_file() or report_path.is_symlink() or not cache_path.is_file() or cache_path.is_symlink():
        return None
    try:
        body = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    claimed = body.pop("cache_sha256", None) if isinstance(body, dict) else None
    if (
        not isinstance(body, dict)
        or claimed != stable_digest(body)
        or body.get("kind") != "banked_ram_operator_probe_cache"
        or body.get("probe_cache_abi") != _PROBE_CACHE_ABI
        or body.get("source_dcp_sha256") != source_dcp_sha256
        or body.get("report_sha256") != hashlib.sha256(report_path.read_bytes()).hexdigest()
    ):
        return None
    public = body.get("public_result")
    if not isinstance(public, dict):
        return None
    replayed = parse_probe_report(report_path)
    replayed.pop("_private_contract_digest", None)
    replayed["read_only_after_restore"] = True
    return public if replayed == public else None


__all__ = [
    "build_script", "load_probe_cache", "parse_backend_report",
    "parse_probe_report", "write_probe_cache",
]
