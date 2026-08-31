"""Non-executable, provenance-bound snapshots for offline Decision iteration."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .action_menu import validate_action_menu
from .artifacts import atomic_write_json
from .probe_planner import probe_decision_display, probe_fact_already_available
from .stages import (
    project_action_menu_for_decision,
    validate_public_action_menu,
)


SCHEMA_VERSION = "fdagents-offline-decision-snapshot-v2"
_SHA256 = re.compile(r"[0-9a-f]{64}")


class OfflineDecisionError(ValueError):
    """Raised when an offline snapshot could carry unsafe or stale authority."""


_SAFE_DISPLAY_FIELDS = {
    "action_family",
    "applicability_basis",
    "applicability_signal",
    "available_execution_window_s",
    "backend",
    "ancestor_gain_consumed",
    "basin_opportunity_signal",
    "containment_opportunity_signal",
    "candidate_commit_reserve_s",
    "central_measurement_estimate_s",
    "decision_confidence",
    "destructive_scope",
    "effective_top_k",
    "estimated_runtime_s",
    "exact_parameters_private",
    "execution_state",
    "expected_mechanism",
    "expected_next_wall",
    "expected_wall_effect",
    "family_alias",
    "family_kind",
    "typed_rejection",
    "live_evidence",
    "live_wall_coverage_ppm",
    "live_wall_covered_path_count",
    "lineage_mechanism_repeat",
    "minimum_remaining_s",
    "observation_kind",
    "parameter_policy",
    "parameter_regime",
    "proof_boundary",
    "proof_template_id",
    "ranking_score",
    "ranking_state",
    "regime_ranking_adjustment",
    "required_execution_window_s",
    "resource_request",
    "risk",
    "runtime_estimate_source",
    "same_run_feedback_ranking_adjustment",
    "scope_ranking_adjustment",
    "search_role",
    "selected_runtime_estimate_s",
    "skill",
    "timeout_s",
    "why_now",
}


def _safe_same_run_feedback(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    safe = {
        key: copy.deepcopy(value[key])
        for key in (
            "attempt_count",
            "lineage_attempt_count",
            "broad_search_escalation_bonus",
            "current_seed_residual",
        )
        if key in value
    }
    for source_key in ("recent_attempts", "recent_lineage_attempts"):
        rows = value.get(source_key)
        if not isinstance(rows, list):
            continue
        safe[source_key] = [
            {
                key: copy.deepcopy(row[key])
                for key in (
                    "mechanism_family",
                    "terminal_status",
                    "best_legal_delta_fmax_mhz",
                    "legal_candidate_count",
                    "lineage_distance",
                )
                if key in row
            }
            for row in rows
            if isinstance(row, Mapping)
        ]
    return safe


def _safe_display(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    display = {
        key: copy.deepcopy(item)
        for key, item in value.items()
        if key in _SAFE_DISPLAY_FIELDS
    }
    feedback = _safe_same_run_feedback(value.get("same_run_feedback"))
    if feedback:
        display["same_run_feedback"] = feedback
    return display


def _safe_measured_state(value: Mapping[str, Any]) -> dict[str, Any]:
    allowed_fields = {
        "round",
        "current_clock_period_ns",
        "current_wns",
        "current_fmax_mhz",
        "fmax_gain_mhz",
        "bottleneck",
        "telemetry_epoch",
        "exhausted_probes",
        "exhausted_actions",
        "measured_actions_on_current_lineage",
        "budget",
        "task_counts",
        "run_contract",
        "concurrent_proof_decision_packet",
        "action_menu_epoch_cost",
        "decision_audit_budget",
    }
    state = {
        key: copy.deepcopy(item)
        for key, item in value.items()
        if key in allowed_fields
    }
    # Exact target suggestions remain private current-run parameterization facts.
    state.pop("suggested_targets", None)
    for field in ("exhausted_actions", "measured_actions_on_current_lineage"):
        rows = state.get(field)
        if not isinstance(rows, list):
            continue
        state[field] = [
            {
                key: copy.deepcopy(row[key])
                for key in (
                    "skill",
                    "disposition",
                    "measurements",
                    "accepted_measurements",
                )
                if key in row
            }
            for row in rows
            if isinstance(row, Mapping)
        ]
    return state


def _sanitized_action_census(menu: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Retain rank/refusal evidence while stripping every executable field."""
    census = []
    for entry in menu.get("entries", []):
        blocker_codes = []
        for blocker in entry.get("blockers") or []:
            code = str(blocker).split(":", 1)[0].strip().upper()
            blocker_codes.append(
                code if re.fullmatch(r"[A-Z][A-Z0-9_]{1,95}", code) else "UNTYPED_BLOCKER"
            )
        census.append({
            "entry_id": entry["entry_id"],
            "kind": entry["kind"],
            "eligible": entry["eligible"],
            "proof_status": entry["proof_status"],
            "authorization_class": entry["authorization_class"],
            "display": _safe_display(entry["display"]),
            "blocker_codes": blocker_codes,
        })
    return census


def _project_current_planner_cards(menu: Mapping[str, Any], memory: object) -> dict:
    """Reapply pure-Python probe planning to old same-run telemetry."""
    projected_source = copy.deepcopy(dict(menu))
    evidence = getattr(memory, "current_telemetry", None)
    evidence = dict(evidence) if isinstance(evidence, Mapping) else {}
    current_best = getattr(memory, "current_best", None)
    graph = getattr(memory, "candidate_graph", None)
    nodes = graph.get("nodes") if isinstance(graph, Mapping) else None
    if isinstance(current_best, Mapping) and isinstance(nodes, Mapping):
        node = nodes.get(str(current_best.get("candidate_id") or ""))
        if isinstance(node, Mapping):
            evidence.setdefault("route_legal", node.get("route_legal"))
    for entry in projected_source.get("entries", []):
        action = entry.get("action") if isinstance(entry, Mapping) else None
        if (
            not isinstance(action, Mapping)
            or action.get("action") != "probe"
            or not isinstance(action.get("tool"), str)
        ):
            continue
        tool = str(action["tool"])
        entry["display"] = probe_decision_display(tool)
        if entry.get("eligible") is True and probe_fact_already_available(tool, evidence):
            entry["eligible"] = False
            entry["authorization_class"] = "offline_fact_already_available"
            entry["blockers"] = ["CURRENT_SEED_FACT_ALREADY_AVAILABLE"]
    return project_action_menu_for_decision(projected_source)


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")).hexdigest()


def _development_provenance(memory: object) -> tuple[str, str]:
    manifest = getattr(memory, "run_manifest", None)
    replay_control = getattr(memory, "replay_control", None)
    if not isinstance(manifest, Mapping):
        raise OfflineDecisionError("offline snapshot requires a run manifest")
    admission = manifest.get("dcp_admission")
    isolation = manifest.get("isolation")
    provenance = manifest.get("no_replay_provenance")
    if (
        manifest.get("strategy_mode") != "autonomous"
        or not isinstance(admission, Mapping)
        or admission.get("execution_scope") != "development"
        or admission.get("training_role") != "development"
        or admission.get("development_allowed") is False
        or not isinstance(isolation, Mapping)
        or isolation.get("cold_isolation") is not True
        or isolation.get("replay_derived_actions_forbidden") is not True
        or replay_control
        or manifest.get("packaged_semantic_replay")
        or not isinstance(provenance, Mapping)
    ):
        raise OfflineDecisionError(
            "offline snapshots are restricted to cold development runs"
        )
    run_id = str(provenance.get("run_id") or "")
    source_sha = str(provenance.get("source_artifact_sha256") or "")
    if not run_id.startswith("nr-") or _SHA256.fullmatch(source_sha) is None:
        raise OfflineDecisionError("offline snapshot provenance is incomplete")
    if (
        source_sha != manifest.get("root_sha256")
        or source_sha != admission.get("source_dcp_sha256")
    ):
        raise OfflineDecisionError("offline snapshot source identity changed")
    return run_id, source_sha


def build_offline_decision_snapshot(
    memory: object,
    state: Mapping[str, Any],
) -> dict[str, Any]:
    """Freeze exactly one public Decision input without executable payloads."""
    run_id, source_sha = _development_provenance(memory)
    if not isinstance(state, Mapping):
        raise OfflineDecisionError("offline decision state must be an object")
    private_menu = validate_action_menu(
        state.get("authorized_action_menu"), cold_isolation=True
    )
    public_menu = _project_current_planner_cards(private_menu, memory)
    for entry in public_menu["entries"]:
        entry["display"] = _safe_display(entry["display"])
    public_menu = validate_public_action_menu(public_menu)
    measured_state = _safe_measured_state(state)
    seed_sha = str(public_menu["seed_artifact_sha256"])
    incumbent_sha = getattr(memory, "current_incumbent_artifact_sha256", None)
    if callable(incumbent_sha) and seed_sha != str(incumbent_sha() or ""):
        raise OfflineDecisionError("offline snapshot menu is not current-seed bound")
    body = {
        "schema_version": SCHEMA_VERSION,
        "diagnostic_only": True,
        "mutation_authority": False,
        "execution_performed": False,
        "execution_entrypoint_present": False,
        "executable_action_payload_present": False,
        "historical_action_order_authority": False,
        "holdout_evidence": False,
        "planner_projection": "current_code_sanitized_cards_v1",
        "source_run_id": run_id,
        "source_artifact_sha256": source_sha,
        "seed_candidate_id": public_menu["seed_candidate_id"],
        "seed_artifact_sha256": seed_sha,
        "telemetry_epoch": measured_state.get("telemetry_epoch"),
        "round": measured_state.get("round"),
        "menu_digest": private_menu["menu_digest"],
        "measured_state": measured_state,
        "public_decision_menu": public_menu,
        "sanitized_action_census": _sanitized_action_census(private_menu),
    }
    return {**body, "snapshot_digest": _digest(body)}


def validate_offline_decision_snapshot(value: object) -> dict[str, Any]:
    """Validate a stored snapshot before any offline LLM call."""
    if not isinstance(value, Mapping):
        raise OfflineDecisionError("offline decision snapshot must be an object")
    snapshot = copy.deepcopy(dict(value))
    required = {
        "schema_version",
        "diagnostic_only",
        "mutation_authority",
        "execution_performed",
        "execution_entrypoint_present",
        "executable_action_payload_present",
        "historical_action_order_authority",
        "holdout_evidence",
        "planner_projection",
        "source_run_id",
        "source_artifact_sha256",
        "seed_candidate_id",
        "seed_artifact_sha256",
        "telemetry_epoch",
        "round",
        "menu_digest",
        "measured_state",
        "public_decision_menu",
        "sanitized_action_census",
        "snapshot_digest",
    }
    if set(snapshot) != required or snapshot.get("schema_version") != SCHEMA_VERSION:
        raise OfflineDecisionError("offline decision snapshot schema mismatch")
    expected_flags = {
        "diagnostic_only": True,
        "mutation_authority": False,
        "execution_performed": False,
        "execution_entrypoint_present": False,
        "executable_action_payload_present": False,
        "historical_action_order_authority": False,
        "holdout_evidence": False,
    }
    if any(snapshot.get(key) is not expected for key, expected in expected_flags.items()):
        raise OfflineDecisionError("offline decision snapshot safety flags changed")
    if snapshot.get("planner_projection") != "current_code_sanitized_cards_v1":
        raise OfflineDecisionError("offline decision snapshot planner projection changed")
    if not str(snapshot.get("source_run_id") or "").startswith("nr-"):
        raise OfflineDecisionError("offline decision snapshot run_id is invalid")
    for field in ("source_artifact_sha256", "seed_artifact_sha256", "menu_digest"):
        if _SHA256.fullmatch(str(snapshot.get(field) or "")) is None:
            raise OfflineDecisionError(f"offline decision snapshot {field} is invalid")
    try:
        public_menu = validate_public_action_menu(snapshot["public_decision_menu"])
    except ValueError as exc:
        raise OfflineDecisionError("offline decision snapshot menu is invalid") from exc
    if (
        public_menu["seed_artifact_sha256"] != snapshot["seed_artifact_sha256"]
        or public_menu["seed_candidate_id"] != snapshot["seed_candidate_id"]
        or not isinstance(snapshot.get("measured_state"), Mapping)
        or "authorized_action_menu" in snapshot["measured_state"]
    ):
        raise OfflineDecisionError("offline decision snapshot bindings changed")
    if (
        _safe_measured_state(snapshot["measured_state"])
        != snapshot["measured_state"]
        or any(
            _safe_display(entry["display"]) != entry["display"]
            for entry in public_menu["entries"]
        )
    ):
        raise OfflineDecisionError("offline decision snapshot contains private fields")
    census = snapshot.get("sanitized_action_census")
    census_fields = {
        "entry_id",
        "kind",
        "eligible",
        "proof_status",
        "authorization_class",
        "display",
        "blocker_codes",
    }
    if not isinstance(census, list) or any(
        not isinstance(entry, Mapping)
        or set(entry) != census_fields
        or _SHA256.fullmatch(str(entry.get("entry_id") or "")) is None
        or not isinstance(entry.get("eligible"), bool)
        or not isinstance(entry.get("display"), Mapping)
        or _safe_display(entry.get("display")) != entry.get("display")
        or not isinstance(entry.get("blocker_codes"), list)
        or any(not isinstance(code, str) for code in entry.get("blocker_codes", []))
        for entry in census
    ):
        raise OfflineDecisionError("offline decision snapshot census is invalid")
    body = {key: item for key, item in snapshot.items() if key != "snapshot_digest"}
    if _digest(body) != snapshot["snapshot_digest"]:
        raise OfflineDecisionError("offline decision snapshot digest mismatch")
    return snapshot


def write_offline_decision_snapshot(
    memory: object,
    state: Mapping[str, Any],
    *,
    run_dir: Path,
) -> tuple[Path, dict[str, Any]]:
    """Write one bounded snapshot into the current cold run workspace."""
    snapshot = build_offline_decision_snapshot(memory, state)
    destination_dir = Path(run_dir).expanduser().resolve() / "offline_decisions"
    destination_dir.mkdir(parents=True, exist_ok=True)
    sequence = len(list(destination_dir.glob("decision_[0-9][0-9][0-9][0-9]_*.json")))
    destination = destination_dir / (
        f"decision_{sequence:04d}_{snapshot['snapshot_digest'][:12]}.json"
    )
    if destination.exists():
        raise OfflineDecisionError("offline decision snapshot path already exists")
    atomic_write_json(destination, snapshot)
    return destination, snapshot


def load_offline_decision_snapshot(path: Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    if not source.is_file() or source.is_symlink():
        raise OfflineDecisionError("offline decision snapshot must be one regular file")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OfflineDecisionError("offline decision snapshot is unreadable") from exc
    return validate_offline_decision_snapshot(value)


__all__ = [
    "OfflineDecisionError",
    "SCHEMA_VERSION",
    "build_offline_decision_snapshot",
    "load_offline_decision_snapshot",
    "validate_offline_decision_snapshot",
    "write_offline_decision_snapshot",
]
