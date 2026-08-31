"""Framework plumbing for complete external-product-register DSP absorption."""

from __future__ import annotations

import csv
import hashlib
import re
from pathlib import Path
from typing import Any, Iterable


_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "apply_registered_dsp_lane_rebuild.tcl"


def _tcl_brace(value: Path | str) -> str:
    text = str(value)
    if any(char in text for char in ("}", "\r", "\n")):
        raise ValueError("registered DSP lane path contains unsafe Tcl characters")
    return "{" + text + "}"


def build_script(arguments: Iterable[Path | str]) -> str:
    source = _SCRIPT.read_text(encoding="utf-8")
    source = re.sub(r"\nexit 0\s*\Z", "\n", source)
    argv = " ".join(_tcl_brace(item) for item in arguments)
    return f"set argv [list {argv}]\n" + source


def _integer(values: dict[str, str], field: str, *, minimum: int = 0) -> int:
    try:
        value = int(values[field])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"registered DSP lane {field} is invalid") from exc
    if value < minimum:
        raise ValueError(f"registered DSP lane {field} is below {minimum}")
    return value


def _float(values: dict[str, str], field: str) -> float:
    try:
        return float(values[field])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"registered DSP lane {field} is invalid") from exc


def _values(path: Path) -> dict[str, str]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    return {
        str(row.get("metric") or ""): str(row.get("value") or "")
        for row in rows
    }


def _sibling(values: dict[str, str]) -> tuple[bool, int, float | None]:
    present = _integer(values, "sibling_present") == 1
    rank = _integer(values, "sibling_rank")
    slack = _float(values, "sibling_slack_ns") if present else None
    if present != (rank > 0):
        raise ValueError("registered DSP lane sibling evidence is inconsistent")
    return present, rank, slack


def _clock_footprint(values: dict[str, str], lane_count: int) -> tuple[int, int]:
    already_clocked = _integer(
        values, "selected_dsp_already_on_recovered_clock_count"
    )
    newly_activated = _integer(values, "newly_activated_dsp_clock_site_count")
    legacy_new_count = _integer(values, "new_clocked_dsp_count")
    if (
        already_clocked + newly_activated != lane_count
        or legacy_new_count != newly_activated
    ):
        raise ValueError("registered DSP lane clock-footprint accounting is incomplete")
    return already_clocked, newly_activated


def parse_probe_report(path: Path) -> dict[str, Any]:
    values = _values(path)
    required = {
        "contract", "backend", "lane_count", "word_width",
        "live_wall_path_count", "sibling_present", "sibling_rank",
        "sibling_slack_ns", "new_clocked_dsp_count", "clock_region_count",
        "selected_dsp_already_on_recovered_clock_count",
        "newly_activated_dsp_clock_site_count",
        "estimated_removed_external_ff_count",
        "retained_reset_history_ff_count", "reset_correction_lut_count",
        "input_period_ns", "input_waveform", "input_clock_source",
        "input_wns_ns", "clock_edge_inverted",
    }
    if not required.issubset(values):
        raise ValueError("registered DSP lane probe report is incomplete")
    if (
        values["contract"]
        != "EXTERNAL_PRODUCT_REGISTER_TO_DSP_PREG_WITH_RESET_HISTORY_CORRECTION"
        or values["backend"] != "registered_dsp_lane_rebuild_v1"
    ):
        raise ValueError("registered DSP lane probe contract changed")
    lane_count = _integer(values, "lane_count", minimum=1)
    word_width = _integer(values, "word_width", minimum=2)
    removed = _integer(values, "estimated_removed_external_ff_count", minimum=1)
    retained = _integer(values, "retained_reset_history_ff_count")
    if removed + retained != lane_count * word_width:
        raise ValueError("registered DSP lane probe FF accounting is incomplete")
    already_clocked, newly_activated = _clock_footprint(values, lane_count)
    sibling_present, sibling_rank, sibling_slack = _sibling(values)
    input_wns = _float(values, "input_wns_ns")
    sibling_margin = (
        max(0.0, float(sibling_slack) - input_wns)
        if sibling_slack is not None else None
    )
    contract = {
        "operator_class": "REGISTERED_DSP_PRODUCT_TRANSACTION",
        "data_operator": {
            "lane_count": lane_count,
            "word_width": word_width,
            "complete_contiguous_p_to_ff_mapping": True,
            "pcout_and_output_side_consumers_closed": True,
            "current_preg_mode": 0,
        },
        "control_operator": {
            "clock_ce_synchronous_reset_word_recovered": True,
            "clock_edge_inverted": bool(_integer(values, "clock_edge_inverted")),
            "observable_cycle_preserved": True,
            "retained_reset_history_ff_count": retained,
            "reset_correction_lut_count": _integer(
                values, "reset_correction_lut_count"
            ),
        },
        "live_wall": {
            "covered_path_count": _integer(
                values, "live_wall_path_count", minimum=1
            ),
            "input_wns_ns": input_wns,
            "independent_sibling_present": sibling_present,
            "independent_sibling_rank": sibling_rank,
            "independent_sibling_slack_ns": sibling_slack,
            "independent_sibling_margin_ns": sibling_margin,
        },
        "physical_risk": {
            "selected_dsp_already_on_recovered_clock_count": already_clocked,
            "newly_activated_dsp_clock_site_count": newly_activated,
            "new_clocked_dsp_count": newly_activated,
            "clock_region_count": _integer(
                values, "clock_region_count", minimum=1
            ),
            "estimated_removed_external_ff_count": removed,
            "sibling_ceiling_and_clock_footprint_are_soft_evidence": True,
        },
    }
    return {
        "status": "success",
        "kind": "registered_dsp_lane_operator_contract",
        "version": 1,
        "fresh_rediscovery": True,
        "read_only": True,
        "structural_boundary_recovered": True,
        "contract": contract,
        "action_assessment": {
            "status": "measurement_candidate_available",
            "authority": "current_dcp_complete_data_control_and_physical_risk",
            "candidate_action": {
                "skill": "operator_rewrite",
                "target": "registered_dsp_lane_rebuild_v1",
                "params": {},
            },
            "expected_mechanism": (
                "replace the complete external registered DSP product word with "
                "the existing DSP PREG while preserving clock, CE, reset word "
                "and observable cycle"
            ),
            "selection_caution": (
                "compare the independent sibling ceiling with newly clocked DSP "
                "count and clock-region span; structural closure proves function, "
                "not positive routed QoR"
            ),
            "does_not_establish": [
                "positive routed QoR",
                "a calibrated gain distribution or regression probability",
                "that the independent sibling improves after route",
                "that broad hard-macro clock activation preserves the routed basin",
            ],
            "failure_update": (
                "separate semantic closure failure from sibling-ceiling or "
                "clock-footprint physical regression, then refresh the live wall"
            ),
            "not_a_hard_trigger": True,
            "luna_owns_final_selection": True,
        },
        "mutation_eligible": True,
        "proof_status": "measurement_probe_only_final_equivalence_required",
        "report_digest": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def parse_backend_report(path: Path) -> dict[str, Any]:
    values = _values(path)
    required = {
        "contract", "backend", "lane_count", "word_width",
        "live_wall_path_count", "sibling_present", "sibling_rank",
        "sibling_slack_ns",
        "new_clocked_dsp_count", "clock_region_count",
        "selected_dsp_already_on_recovered_clock_count",
        "newly_activated_dsp_clock_site_count",
        "removed_external_ff_count", "retained_reset_history_ff_count",
        "reset_correction_lut_count", "input_period_ns", "input_waveform",
        "input_clock_source", "input_wns_ns", "output_period_ns",
        "output_waveform", "output_clock_source", "output_wns_ns",
        "route_errors", "input_dsp_count", "output_dsp_count",
        "input_ff_count", "output_ff_count", "input_lut_count",
        "output_lut_count", "input_carry_count", "output_carry_count",
        "clock_edge_inverted",
    }
    if not required.issubset(values):
        raise ValueError("registered DSP lane backend report is incomplete")
    if (
        values["contract"]
        != "EXTERNAL_PRODUCT_REGISTER_TO_DSP_PREG_WITH_RESET_HISTORY_CORRECTION"
        or values["backend"] != "registered_dsp_lane_rebuild_v1"
        or values["input_period_ns"] != values["output_period_ns"]
        or values["input_waveform"] != values["output_waveform"]
        or values["input_clock_source"] != values["output_clock_source"]
        or _integer(values, "route_errors") != 0
    ):
        raise ValueError("registered DSP lane routed identity checks failed")
    lane_count = _integer(values, "lane_count", minimum=1)
    word_width = _integer(values, "word_width", minimum=2)
    removed = _integer(values, "removed_external_ff_count", minimum=1)
    retained = _integer(values, "retained_reset_history_ff_count")
    if removed + retained != lane_count * word_width:
        raise ValueError("registered DSP lane FF accounting is incomplete")
    already_clocked, newly_activated = _clock_footprint(values, lane_count)
    if _integer(values, "input_dsp_count") != _integer(values, "output_dsp_count"):
        raise ValueError("registered DSP lane changed the DSP instance count")
    if _integer(values, "input_carry_count") != _integer(values, "output_carry_count"):
        raise ValueError("registered DSP lane changed the CARRY instance count")
    sibling_present, sibling_rank, sibling_slack = _sibling(values)
    return {
        "contract": values["contract"],
        "backend": values["backend"],
        "operator_class": "REGISTERED_DSP_PRODUCT_TRANSACTION",
        "lane_count": lane_count,
        "word_width": word_width,
        "live_wall_path_count": _integer(values, "live_wall_path_count", minimum=1),
        "sibling_present": sibling_present,
        "sibling_rank": sibling_rank,
        "sibling_slack_ns": sibling_slack,
        "selected_dsp_already_on_recovered_clock_count": already_clocked,
        "newly_activated_dsp_clock_site_count": newly_activated,
        "new_clocked_dsp_count": newly_activated,
        "clock_region_count": _integer(values, "clock_region_count", minimum=1),
        "removed_external_ff_count": removed,
        "retained_reset_history_ff_count": retained,
        "reset_correction_lut_count": _integer(values, "reset_correction_lut_count"),
        "period_ns": _float(values, "output_period_ns"),
        "input_wns_ns": _float(values, "input_wns_ns"),
        "output_wns_ns": _float(values, "output_wns_ns"),
        "route_errors": 0,
        "clock_identity_preserved": True,
        "complete_p_to_ff_mapping_recovered": True,
        "clock_ce_reset_word_recovered": True,
        "pcout_and_side_consumers_closed": True,
        "observable_cycle_preserved": True,
        "clock_edge_inverted": bool(_integer(values, "clock_edge_inverted")),
        "resource_delta": {
            "dsp": _integer(values, "output_dsp_count") - _integer(values, "input_dsp_count"),
            "ff": _integer(values, "output_ff_count") - _integer(values, "input_ff_count"),
            "lut": _integer(values, "output_lut_count") - _integer(values, "input_lut_count"),
            "carry": _integer(values, "output_carry_count") - _integer(values, "input_carry_count"),
        },
        "worst_path": {
            "startpoint": values.get("worst_startpoint", ""),
            "endpoint": values.get("worst_endpoint", ""),
            "logic_levels": int(values.get("worst_logic_levels") or 0),
            "logic_delay_ns": float(values.get("worst_logic_delay_ns") or "nan"),
            "net_delay_ns": float(values.get("worst_net_delay_ns") or "nan"),
        },
        "report_digest": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


__all__ = ["build_script", "parse_backend_report", "parse_probe_report"]
