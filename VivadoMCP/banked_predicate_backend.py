"""Generic banked-select predicate recovery and ECO script plumbing.

The Tcl scripts retain exact object names on the trusted side.  These parsers
return only anonymous dimensions, live-wall coverage, control roles, and a
typed measurement action to the Agent.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable


_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT_DIR = _ROOT / "scripts"
_PROBE_CACHE_ABI = 2


def _tcl_brace(value: Path | str) -> str:
    text = str(value)
    if any(char in text for char in ("}", "\r", "\n")):
        raise ValueError("banked predicate path contains unsafe Tcl characters")
    return "{" + text + "}"


def build_script(script_name: str, arguments: Iterable[Path | str]) -> str:
    """Load one audited backend script for an interactive Vivado session."""
    if script_name not in {
        "probe_banked_select_predicate.tcl",
        "apply_banked_select_predicate_update.tcl",
    }:
        raise ValueError("unsupported banked predicate script")
    path = _SCRIPT_DIR / script_name
    source = path.read_text(encoding="utf-8")
    # Batch wrappers terminate Vivado.  MCP owns a persistent process, so the
    # same code returns to the prompt after closing the design.
    source = re.sub(r"\nexit 0\s*\Z", "\n", source)
    argv = " ".join(_tcl_brace(item) for item in arguments)
    return f"set argv [list {argv}]\n" + source


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def _details(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in str(value or "").split(";"):
        if "=" in item:
            key, raw = item.split("=", 1)
            result[key] = raw
    return result


def _integer(value: Any, field: str) -> int:
    try:
        result = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"banked predicate {field} is not an integer") from exc
    if result < 0:
        raise ValueError(f"banked predicate {field} is negative")
    return result


def parse_probe_report(path: Path) -> dict[str, Any]:
    rows = _read_rows(path)
    summaries = [row for row in rows if row.get("kind") == "summary"]
    wall_rows = [row for row in rows if row.get("kind") == "live_wall"]
    banks = [row for row in rows if row.get("kind") == "bank"]
    bank_registers = [
        row for row in rows if row.get("kind") == "bank_read_register"
    ]
    mappings = [
        row for row in rows if row.get("kind") == "bank_selected_input"
    ]
    compare_inputs = [
        row for row in rows if row.get("kind") == "compare_lut_input"
    ]
    global_consumers = [
        row for row in rows if row.get("kind") == "global_consumer"
    ]
    if len(summaries) != 1 or len(wall_rows) != 1 or not banks:
        raise ValueError("banked predicate probe report is incomplete")
    bank_count = _integer(summaries[0].get("count"), "bank_count")
    if bank_count != len(banks) or bank_count < 2:
        raise ValueError("banked predicate bank inventory changed during probe")
    widths = {
        _integer(_details(row.get("details", "")).get("read_registers"), "word_width")
        for row in banks
    }
    if len(widths) != 1:
        raise ValueError("banked predicate registered word widths are inconsistent")
    word_width = widths.pop()
    if word_width == 0:
        raise ValueError("banked predicate registered word width is zero")
    selected_keys = {
        str(row.get("object") or "")
        for row in mappings
        if str(row.get("group")) == "0"
    }
    if not selected_keys or len(mappings) != bank_count * len(selected_keys):
        raise ValueError("banked predicate selected-word mapping is incomplete")
    if any(_integer(row.get("count"), "selected source count") != 1 for row in mappings):
        raise ValueError("banked predicate selected-word source is ambiguous")
    if len(bank_registers) != bank_count * word_width:
        raise ValueError("banked predicate registered-word inventory is incomplete")
    comparator_luts = {
        str(row.get("object") or "") for row in compare_inputs
    }
    if not comparator_luts or not global_consumers:
        raise ValueError("banked predicate transaction closure is incomplete")
    wall = _details(wall_rows[0].get("details", ""))
    top_path_count = _integer(wall.get("top_paths"), "top path count")
    covered_path_count = _integer(
        wall_rows[0].get("count"), "covered path count"
    )
    if top_path_count == 0 or covered_path_count > top_path_count:
        raise ValueError("banked predicate live-wall coverage is inconsistent")
    mutation_eligible = covered_path_count > 0
    contract = {
        "operator_class": "BANKED_SELECT_PREDICATE_UPDATE",
        "data_operator": {
            "source": "banked_registered_ram_words",
            "word_width": word_width,
            "global_operation": "select_then_word_predicate",
            "specialized_operation": "bank_word_predicate_before_select",
        },
        "control_operator": {
            "bank_count": bank_count,
            "local_write_predicate_truth_table_preserved": True,
            "alternate_and_init_cofactors_preserved": True,
            "global_predicate_consumer_preserved": True,
            "clock_ce_reset_state_unchanged": True,
        },
        "recovery": {
            "comparator_lut_count": len(comparator_luts),
            "selected_word_input_count": len(selected_keys),
            "registered_source_count": len(bank_registers),
            "every_selected_input_has_one_bank_local_source": True,
            "preserved_global_consumer_count": len(global_consumers),
        },
        "live_wall": {
            "top_path_sample_count": top_path_count,
            "covered_path_count": covered_path_count,
            "covers_worst_path": wall.get("covers_worst_path") == "1",
            "wns_ns": float(wall.get("wns_ns", "nan")),
            "logic_levels": _integer(wall.get("logic_levels"), "logic levels"),
            "logic_delay_ns": float(wall.get("logic_delay_ns", "nan")),
            "net_delay_ns": float(wall.get("net_delay_ns", "nan")),
        },
    }
    private_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "status": "success" if mutation_eligible else "no_live_wall_overlap",
        "kind": "banked_select_predicate_operator_contract",
        "version": 1,
        "fresh_rediscovery": True,
        "read_only": True,
        "structural_boundary_recovered": True,
        "contract": contract,
        "action_assessment": {
            "status": (
                "measurement_candidate_available"
                if mutation_eligible else "typed_refusal"
            ),
            "authority": "current_dcp_data_and_control_contract",
            "candidate_action": ({
                "skill": "operator_rewrite",
                "target": "bank_local_predicate_specialization_v1",
                "params": {},
            } if mutation_eligible else None),
            "refusal_reasons": (
                [] if mutation_eligible else ["zero_current_live_wall_overlap"]
            ),
            "expected_mechanism": (
                "replace only each bank-local write branch's global selected-word "
                "predicate with the exact predicate of that bank's registered word; "
                "preserve the global consumer and all alternate/init control cofactors"
            ),
            "failure_update": (
                "reprofile the routed result and distinguish semantic mismatch, "
                "new local-predicate route wall, and independent RAM state/control wall"
            ),
            "not_a_hard_trigger": True,
            "luna_owns_final_selection": True,
        },
        "mutation_eligible": mutation_eligible,
        "proof_status": (
            "measurement_probe_only_final_equivalence_required"
            if mutation_eligible else "typed_negative_current_wall"
        ),
        # Exact object identity never enters the LLM response.  The backend
        # uses this only when binding its own fresh recovery to a candidate.
        "_private_contract_digest": private_digest,
    }


def parse_backend_report(path: Path) -> dict[str, Any]:
    rows = _read_rows(path)
    values = {
        str(row.get("metric") or ""): str(row.get("value") or "")
        for row in rows
    }
    required = {
        "contract",
        "backend",
        "bank_count",
        "word_width",
        "comparator_lut_count",
        "selected_word_input_count",
        "preserved_global_consumer_count",
        "input_period_ns",
        "input_wns_ns",
        "output_period_ns",
        "output_wns_ns",
        "route_errors",
    }
    if not required.issubset(values):
        raise ValueError("banked predicate backend report is incomplete")
    if (
        values["contract"] != "BANKED_SELECT_PREDICATE_UPDATE"
        or values["backend"] != "bank_local_predicate_specialization_v1"
        or values["input_period_ns"] != values["output_period_ns"]
        or _integer(values["route_errors"], "route_errors") != 0
    ):
        raise ValueError("banked predicate candidate failed structural route checks")
    return {
        "contract": values["contract"],
        "backend": values["backend"],
        "bank_count": _integer(values["bank_count"], "bank_count"),
        "word_width": _integer(values["word_width"], "word_width"),
        "comparator_lut_count": _integer(
            values["comparator_lut_count"], "comparator_lut_count"
        ),
        "selected_word_input_count": _integer(
            values["selected_word_input_count"], "selected_word_input_count"
        ),
        "preserved_global_consumer_count": _integer(
            values["preserved_global_consumer_count"],
            "preserved_global_consumer_count",
        ),
        "period_ns": float(values["output_period_ns"]),
        "input_wns_ns": float(values["input_wns_ns"]),
        "output_wns_ns": float(values["output_wns_ns"]),
        "route_errors": 0,
        "worst_path": {
            "logic_levels": _integer(
                values.get("worst_logic_levels", 0), "worst_logic_levels"
            ),
            "logic_delay_ns": float(values.get("worst_logic_delay_ns", "nan")),
            "net_delay_ns": float(values.get("worst_net_delay_ns", "nan")),
        },
        "report_digest": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def stable_digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def write_probe_cache(
    report_path: Path,
    source_dcp_sha256: str,
    public_result: dict[str, Any],
) -> Path:
    """Bind an expensive read-only recovery to its exact source and report."""
    if re.fullmatch(r"[0-9a-f]{64}", source_dcp_sha256) is None:
        raise ValueError("banked predicate cache source identity is invalid")
    report_path = report_path.resolve()
    if report_path.is_symlink() or not report_path.is_file():
        raise ValueError("banked predicate cache report is unavailable")
    body = {
        "kind": "banked_select_predicate_probe_cache",
        "probe_cache_abi": _PROBE_CACHE_ABI,
        "source_dcp_sha256": source_dcp_sha256,
        "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        "public_result": public_result,
    }
    body["cache_sha256"] = stable_digest(body)
    cache_path = report_path.with_name(report_path.name + ".cache.json")
    temporary = cache_path.with_name(cache_path.name + ".tmp")
    temporary.write_text(
        json.dumps(body, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(cache_path)
    return cache_path


def load_probe_cache(
    report_path: Path,
    source_dcp_sha256: str,
) -> dict[str, Any] | None:
    """Return only a fully source/report/ABI-bound cached public contract."""
    report_path = report_path.resolve()
    cache_path = report_path.with_name(report_path.name + ".cache.json")
    if (
        report_path.is_symlink()
        or not report_path.is_file()
        or cache_path.is_symlink()
        or not cache_path.is_file()
    ):
        return None
    try:
        body = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    cache_sha256 = body.pop("cache_sha256", None)
    if (
        cache_sha256 != stable_digest(body)
        or body.get("kind") != "banked_select_predicate_probe_cache"
        or body.get("probe_cache_abi") != _PROBE_CACHE_ABI
        or body.get("source_dcp_sha256") != source_dcp_sha256
        or body.get("report_sha256")
        != hashlib.sha256(report_path.read_bytes()).hexdigest()
    ):
        return None
    public_result = body.get("public_result")
    if not isinstance(public_result, dict):
        return None
    replayed = parse_probe_report(report_path)
    replayed.pop("_private_contract_digest", None)
    replayed["read_only_after_restore"] = True
    if replayed != public_result:
        return None
    return public_result


__all__ = [
    "build_script",
    "load_probe_cache",
    "parse_backend_report",
    "parse_probe_report",
    "stable_digest",
    "write_probe_cache",
]
