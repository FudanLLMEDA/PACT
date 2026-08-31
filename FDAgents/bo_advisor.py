"""Shadow-only learned prediction for one canonical action.

The predictor has no authority over action selection and its output must never
be placed in an LLM prompt. It is retained only for offline calibration.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from .capabilities import ResolvedAction
from .config import Config


PACKAGE_DIR = Path(__file__).resolve().parent

SKILL_ACTION_FAMILY = {
    "clock_tighten": "CLOCK_PERIOD_SEARCH",
    "phys_opt": "VIVADO_DIRECTIVE",
    "fresh_place_route": "PLACEMENT_GUIDANCE",
    "pblock": "PLACEMENT_GUIDANCE",
    "pblock_sweep": "PLACEMENT_GUIDANCE",
    "critical_cluster_anchor": "PLACEMENT_GUIDANCE",
    "fanout_opt": "FANOUT_REPLICATION",
    "force_replicate": "FANOUT_REPLICATION",
    "critical_net_reroute": "ROUTE_REPAIR",
    "per_net_unroute": "ROUTE_REPAIR",
    "post_route_cleanup": "ROUTE_REPAIR",
    "cell_replace": "LOGIC_OPT",
    "lut_merge": "LOGIC_OPT",
    "path_local_lut_reflow": "LOGIC_OPT",
    "lut_pin_swap": "MICRO_ECO",
    "endpoint_bel_move": "MICRO_ECO",
    "hard_macro_move": "MICRO_ECO",
}

RW_SKILL_META = {
    "cell_replace": (
        "rw_cell_detour_move",
        "detour_recipe0",
        "rw_cell_detour_move",
    ),
    "lut_pin_swap": (
        "rw_lut_pin_swap",
        "boolean_automorphism_lut_pin_swap",
        "rw_lut_pin_swap",
    ),
}


def _number(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _fmt(value: Any) -> str:
    number = _number(value)
    return "" if number is None else f"{number:.6f}"


def _nested(data: dict, *keys: str) -> Any:
    node: Any = data
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _route_fraction(profile: dict) -> Optional[float]:
    samples = profile.get("path_samples") or []
    value = samples[0].get("route_pct") if samples and isinstance(samples[0], dict) else None
    if value is None:
        value = profile.get("avg_route_pct")
    result = _number(value)
    if result is not None and result > 1.0:
        result /= 100.0
    return result


def _logic_levels(profile: dict) -> Optional[float]:
    samples = profile.get("path_samples") or []
    if samples and isinstance(samples[0], dict):
        value = _number(samples[0].get("logic_levels"))
        if value is not None:
            return value
    return _number(_nested(profile, "logic_levels", "max"))


def _top_fanout(profile: dict) -> Optional[float]:
    values = [
        value
        for item in (profile.get("top_nets") or [])
        if isinstance(item, dict)
        and (value := _number(item.get("fanout"))) is not None
    ]
    return max(values) if values else None


def _slack_count(profile: dict, width_ns: float) -> Optional[int]:
    values = [
        value
        for item in (profile.get("path_samples") or [])
        if isinstance(item, dict)
        and (value := _number(item.get("slack_ns"))) is not None
    ]
    if not values:
        return None
    worst = min(values)
    return sum(value <= worst + width_ns for value in values)


def _timing_bucket(wns: Any) -> str:
    value = _number(wns)
    if value is None:
        return "unknown_timing"
    if value >= 0.1:
        return "positive_slack"
    if value >= -0.1:
        return "near_zero_slack"
    if value >= -1.0:
        return "setup_failing"
    return "severe_setup_failing"


def _net_bucket(profile: dict) -> str:
    value = _route_fraction(profile)
    if value is None:
        return "unknown_delay"
    if value >= 0.85:
        return "net_dominated_extreme"
    if value >= 0.60:
        return "net_dominated"
    if value >= 0.40:
        return "mixed_delay"
    return "logic_dominated"


def _logic_bucket(profile: dict) -> str:
    value = _logic_levels(profile)
    if value is None:
        return "unknown_logic"
    if value <= 2:
        return "shallow_logic"
    if value <= 5:
        return "medium_logic"
    if value <= 12:
        return "deep_logic"
    return "very_deep_logic"


def _fanout_bucket(profile: dict) -> str:
    value = _top_fanout(profile)
    if value is None:
        return "unknown_path_fanout"
    if value >= 1500:
        return "extreme_path_fanout"
    if value >= 500:
        return "high_path_fanout"
    if value >= 100:
        return "medium_path_fanout"
    return "low_path_fanout"


def _slack_bucket(profile: dict) -> str:
    value = _slack_count(profile, 0.25)
    if value is None:
        return "unknown_slack_wall"
    if value <= 100:
        return "thin_slack_wall"
    if value <= 1000:
        return "moderate_slack_wall"
    return "dense_slack_wall"


def _risk_bucket(profile: dict) -> str:
    route_pct = _number(profile.get("avg_route_pct")) or 50.0
    levels = _number(_nested(profile, "logic_levels", "max")) or 0.0
    fanout = _top_fanout(profile) or 0.0
    if route_pct >= 65 or levels >= 12 or fanout >= 500:
        return "high_path_risk"
    if route_pct >= 50 or levels >= 8 or fanout >= 100:
        return "moderate_path_risk"
    return "low_path_risk"


def _safe_params(node: Any) -> Any:
    if isinstance(node, dict):
        result = {}
        for key, value in node.items():
            name = str(key)
            if name.startswith("_") or name in {
                "input_dcp", "output_dcp", "dcp_path", "command", "script", "tcl"
            }:
                continue
            if name == "recipes" and isinstance(value, list):
                result[name] = [
                    str(item.get("name")) if isinstance(item, dict) else str(item)
                    for item in value
                ]
            else:
                result[name] = _safe_params(value)
        return result
    if isinstance(node, (list, tuple)):
        return [_safe_params(value) for value in node]
    if node is None or isinstance(node, (bool, int, float, str)):
        return node
    return str(node)


def _method(skill: str, target: str) -> str:
    if skill == "pblock":
        return skill
    return f"{skill}:{target or 'auto'}"


def _build_row(memory, action: ResolvedAction, seed: dict) -> dict[str, str]:
    telemetry = memory.current_telemetry or {}
    profile = telemetry.get("timing_profile") or {}
    utilization = telemetry.get("utilization") or {}
    features = telemetry.get("design_features") or {}
    summary = telemetry.get("timing_summary") or {}
    skill = action.skill
    family = SKILL_ACTION_FAMILY[skill]
    rw_meta = RW_SKILL_META.get(skill)
    if rw_meta:
        action_family, method, operation = rw_meta
        source = "rw_candidate"
        granularity = "rw_candidate"
    else:
        action_family = family
        method = _method(skill, action.canonical_target)
        operation = ""
        source = "vivado_action"
        granularity = "vivado_recipe"
    design_id = str(memory.design_id or "unknown")
    seed_id = str(seed.get("candidate_id") or "unknown")
    epoch = telemetry.get("epoch", "unknown")
    group = f"{design_id}:{seed_id}:epoch_{epoch}"
    wns = seed.get("wns_ns")
    period = seed.get("clock_period_ns")
    fmax = seed.get("fmax_mhz")
    route_frac = _route_fraction(profile)
    top_label = str(profile.get("dominant_bottleneck") or "mixed").lower()
    label = {
        "routing": "ROUTING_CONGESTION",
        "fanout": "ROUTING_FANOUT",
        "logic_depth": "LOGIC",
        "logic": "LOGIC",
        "placement": "ROUTING_SPREAD",
        "clock": "CLOCK_SKEW",
    }.get(top_label, "MIXED")
    context = "|".join([
        label,
        f"timing={_timing_bucket(wns)}",
        "stage=routed",
        "routesub=not_route_stage",
    ])
    params_json = json.dumps(
        _safe_params(action.resolved_params), sort_keys=True, separators=(",", ":")
    )
    row = {
        "sample_id": f"{group}:{skill}:{action.canonical_target}",
        "unified_sample_id": f"{group}:{skill}:{action.canonical_target}",
        "benchmark": design_id,
        "benchmark_norm": design_id.lower(),
        "dcp_group_key": design_id.lower(),
        "pair_group": group,
        "split_group": group,
        "candidate_group": group,
        "action_family": action_family,
        "method": method,
        "action_source": source,
        "operation_granularity": granularity,
        "rw_op_family": action_family if rw_meta else "",
        "rw_operation_name": operation,
        "method_portable": "true",
        "direct_recommendable_method": "true",
        "method_evidence_variant": "fdagents_candidate_graph_advisory",
        "bo_params_json": params_json,
        "baseline_fmax_mhz": _fmt(fmax),
        "feat_initial_fmax_mhz": _fmt(fmax),
        "feat_wns_ns": _fmt(wns),
        "feat_target_period_ns": _fmt(period),
        "feat_stage_class": "routed",
        "feat_stage_bucket": "routed",
        "feat_timing_pressure_bucket": _timing_bucket(wns),
        "feat_net_delay_bucket": _net_bucket(profile),
        "feat_logic_depth_bucket": _logic_bucket(profile),
        "feat_fanout_bucket": _fanout_bucket(profile),
        "feat_slack_wall_bucket": _slack_bucket(profile),
        "feat_path_risk_bucket": _risk_bucket(profile),
        "feat_route_stage_subtype": "not_route_stage",
        "feat_context_signature": context,
        "feat_context_signature_coarse": context,
        "feat_avg_logic_levels_topN": _fmt(_nested(profile, "logic_levels", "avg")),
        "feat_max_logic_levels": _fmt(_nested(profile, "logic_levels", "max")),
        "feat_slack_wall_100ps": _fmt(_slack_count(profile, 0.1)),
        "feat_slack_wall_250ps": _fmt(_slack_count(profile, 0.25)),
        "feat_slack_wall_500ps": _fmt(_slack_count(profile, 0.5)),
        "feat_high_fanout_net_count": _fmt(features.get("eligible_hf_count")),
        "feat_max_fanout": _fmt(features.get("max_fanout") or _top_fanout(profile)),
        "feat_top_path_net_delay_frac": _fmt(route_frac),
        "feat_top_path_logic_levels": _fmt(_logic_levels(profile)),
        "feat_top_path_fanout_max": _fmt(_top_fanout(profile)),
        "feat_lut_util_pct": _fmt(utilization.get("lut_util_pct")),
        "feat_ff_util_pct": _fmt(utilization.get("ff_util_pct")),
        "feat_dsp_util_pct": _fmt(utilization.get("dsp_util_pct")),
        "feat_bram_util_pct": _fmt(utilization.get("bram_util_pct")),
        "feat_uram_util_pct": _fmt(utilization.get("uram_util_pct")),
        "feat_failing_endpoints": _fmt(summary.get("failing_endpoints")),
        "feat_route_fully_routed": "true" if seed.get("route_legal") else "false",
        "feat_route_error_nets": "0" if seed.get("route_legal") else "",
        "route_hold_wall_flag": "false" if seed.get("hold_legal") else "true",
        "feature_row_available": "true",
    }
    row["feature_nonempty_count"] = str(sum(bool(str(value)) for value in row.values()))
    if rw_meta:
        row.update({
            "rw_candidate_rank": "1",
            "rw_has_candidate_rank": "true",
            "rw_candidate_pool_size": "1",
            "rw_feature_schema": "fdagents_candidate_graph_advisory",
            "target_is_lut": "true",
            "target_cell_ref_type": "LUT",
        })
        if skill == "lut_pin_swap":
            row.update({"lut_input_count": "6", "lut_precheck_legal": "true"})
        else:
            row.update({
                "detour_threshold": "2.0",
                "detour_candidates_found": "1",
                "detour_cells_analyzed": "1",
            })
    return row


def _resolve_path(value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    for base in (Path.cwd(), PACKAGE_DIR.parent, PACKAGE_DIR):
        candidate = (base / path).resolve()
        if candidate.exists():
            return candidate
    return (PACKAGE_DIR.parent / path).resolve()


def build_bo_advisory(
    *,
    memory,
    resolved_action: ResolvedAction,
    selected_seed: dict,
    cfg: Config,
) -> dict:
    """Return a shadow prediction for offline calibration, never a verdict."""
    base = {
        "version": 1,
        "kind": "learned_action_advisory",
        "shadow_only": True,
        "shown_to_llm": False,
        "informational_only": True,
        "may_veto": False,
        "may_select_action": False,
        "action_fingerprint": resolved_action.action_fingerprint,
        "skill": resolved_action.skill,
        "target": resolved_action.canonical_target,
        "seed_candidate_id": selected_seed.get("candidate_id"),
        "seed_artifact_sha256": selected_seed.get("artifact_sha256"),
        "generated_at": time.time(),
    }
    if not cfg.get("bo.enabled", False):
        return {**base, "status": "disabled"}
    if resolved_action.skill not in SKILL_ACTION_FAMILY:
        return {
            **base,
            "status": "unsupported_action",
            "not_a_negative_prediction": True,
        }
    runtime_python = str(cfg.get("bo.runtime_python", sys.executable)).strip()
    executable = shutil.which(runtime_python) if not Path(runtime_python).is_absolute() else runtime_python
    if not executable or not Path(executable).is_file():
        return {**base, "status": "unavailable", "reason": "predictor runtime missing"}
    qor_root = _resolve_path(str(cfg.require("bo.model_root")))
    command = [
        str(executable),
        "-m",
        "FDAgents.bo_worker",
        "--qor-root",
        str(qor_root),
    ]
    if cfg.get("bo.time.enabled", True):
        command.extend([
            "--runtime-root",
            str(_resolve_path(str(cfg.require("bo.time.model_root")))),
        ])
    package_root = str(PACKAGE_DIR.parent)
    inherited_names = {
        "HOME",
        "LANG",
        "LC_ALL",
        "LD_LIBRARY_PATH",
        "PATH",
        "TEMP",
        "TMP",
        "TMPDIR",
        "VIRTUAL_ENV",
    }
    env = {
        name: value
        for name, value in os.environ.items()
        if name in inherited_names
    }
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONPATH"] = package_root
    request = {"row": _build_row(memory, resolved_action, selected_seed)}
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            input=json.dumps(request, sort_keys=True),
            text=True,
            capture_output=True,
            timeout=float(cfg.get("bo.timeout_s", 120)),
            env=env,
            check=False,
        )
        if completed.returncode != 0:
            reason = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "worker failed"
            return {**base, "status": "unavailable", "reason": reason[:300]}
        prediction = json.loads(completed.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return {**base, "status": "unavailable", "reason": str(exc)[:300]}
    qor = prediction.get("qor") or {}
    runtime = prediction.get("runtime") or {}
    probability = _number(qor.get("improved_route_clean_probability"))
    if probability is not None:
        qor["nonpositive_or_not_route_clean_probability"] = max(
            0.0, min(1.0, 1.0 - probability)
        )
    p90 = _number(runtime.get("p90_s"))
    usable = max(
        0.0,
        float(memory.time_remaining())
        - float(cfg.get("bo.time.reserve_s", 300)),
    )
    if p90 is not None:
        runtime["budget_risk"] = "does_not_fit_p90" if p90 > usable else "fits"
        runtime["usable_s_after_reserve"] = usable
        runtime["budget_risk_is_advisory_only"] = True
    return {
        **base,
        "status": "scored" if qor.get("scope_match") else "unsupported_context",
        "qor": qor,
        "runtime": runtime,
        "qor_package": prediction.get("qor_package") or {},
        "runtime_package": prediction.get("runtime_package") or {},
        "advisor_runtime_s": time.monotonic() - started,
        "model_limitations": [
            "prediction is advisory and may be out of distribution",
            "positive probability combines improvement and route cleanliness",
            "a single scored action has no meaningful within-group rank percentile",
        ],
    }
