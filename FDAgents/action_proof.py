"""Neutral, hash-bound evidence for reviewing one canonical action.

This module deliberately produces no score, probability, recommendation, or
veto. It binds exact prior outcomes and current physical facts to the action
that would execute so an LLM can reason from evidence rather than predictions.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from .artifacts import atomic_write_json
from .action_contracts import contract_for
from .capabilities import ResolvedAction
from .measurement import measurement_legality

try:  # POSIX workers
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    _fcntl = None

try:  # Windows development and tests
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX
    _msvcrt = None


REGISTRY_VERSION = 1
PROOF_VERSION = 1
FAMILY_REGISTRY_VERSION = 1

SKILL_FAMILY = {
    "clock_tighten": "CLOCK_PERIOD_SEARCH",
    "phys_opt": "VIVADO_DIRECTIVE",
    "fresh_place_route": "PLACEMENT_GUIDANCE",
    "pblock": "PLACEMENT_GUIDANCE",
    "pblock_sweep": "PLACEMENT_GUIDANCE",
    "critical_cluster_anchor": "PLACEMENT_GUIDANCE",
    "structure_relocation": "PLACEMENT_GUIDANCE",
    "fanout_opt": "FANOUT_REPLICATION",
    "force_replicate": "FANOUT_REPLICATION",
    "critical_net_reroute": "ROUTE_REPAIR",
    "per_net_unroute": "ROUTE_REPAIR",
    "post_route_cleanup": "ROUTE_REPAIR",
    "selective_branch_reroute": "ROUTE_REPAIR",
    "cell_replace": "LOGIC_OPT",
    "lut_merge": "LOGIC_OPT",
    "path_local_lut_reflow": "LOGIC_OPT",
    "lut_pin_swap": "MICRO_ECO",
    "endpoint_bel_move": "MICRO_ECO",
    "hard_macro_move": "MICRO_ECO",
    "equivalent_source_remap": "MICRO_ECO",
    "operator_rewrite": "LOGIC_OPT",
    "semantic_replay": "LOGIC_OPT",
    "checkpoint_import": "ARTIFACT_REUSE",
    "custom": "OBSERVATION_ONLY",
}

IMPLEMENTATION_RECIPE_FAMILY = {
    "derived_pressure_replace_route_v1": "PLACEMENT_GUIDANCE",
    "full_reimplementation_retime_v1": "PLACEMENT_GUIDANCE",
    "routed_critical_cleanup_v1": "ROUTE_REPAIR",
}

MECHANISM_CLAIMS = {
    "CLOCK_PERIOD_SEARCH": (
        "Search a different contest-clock implementation point and retain only "
        "a centrally measured legal Fmax improvement."
    ),
    "VIVADO_DIRECTIVE": (
        "Apply one fixed Vivado physical-optimization mechanism to the routed seed."
    ),
    "PLACEMENT_GUIDANCE": (
        "Change placement or place-and-route guidance to reduce critical physical "
        "spread, congestion, or route delay."
    ),
    "FANOUT_REPLICATION": (
        "Replicate timing-relevant drivers to reduce critical load and route delay."
    ),
    "ROUTE_REPAIR": (
        "Repair selected routing or post-route structures while preserving the "
        "logical design."
    ),
    "LOGIC_OPT": (
        "Change a bounded critical logic or local placement structure to reduce "
        "logic depth or detour."
    ),
    "MICRO_ECO": (
        "Apply a small, bounded ECO whose benefit must be visible in central timing."
    ),
    "ARTIFACT_REUSE": "Adopt an existing artifact rather than create an intervention.",
    "OBSERVATION_ONLY": "Observe the design without mutation.",
}


def _number(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _nested(data: dict, *keys: str) -> Any:
    node: Any = data
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _stable(node: Any) -> Any:
    if isinstance(node, dict):
        return {str(key): _stable(node[key]) for key in sorted(node, key=str)}
    if isinstance(node, (list, tuple)):
        return [_stable(item) for item in node]
    if node is None or isinstance(node, (bool, int, float, str)):
        return node
    return str(node)


def _digest(node: dict) -> str:
    payload = json.dumps(
        _stable(node), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def verify_action_outcome_record(record: dict) -> bool:
    """Verify hash binding for v2 Outcome Proofs while accepting legacy records."""
    is_outcome = (
        record.get("kind") == "action_outcome_proof"
        or record.get("version") == 2
        or "outcome_sha256" in record
    )
    if not is_outcome:
        return record.get("version") == 1
    if (
        record.get("kind") != "action_outcome_proof"
        or record.get("version") != 2
    ):
        return False
    expected = str(record.get("outcome_sha256") or "")
    if not expected or str(record.get("record_id") or "") != expected:
        return False
    payload = copy.deepcopy(record)
    payload.pop("record_id", None)
    payload.pop("outcome_sha256", None)
    return _digest(payload) == expected


def _public_params(node: Any) -> Any:
    if isinstance(node, dict):
        result = {}
        for key, value in node.items():
            name = str(key)
            if (
                name.startswith("_")
                or name in {
                    "input_dcp",
                    "output_dcp",
                    "dcp_path",
                    "command",
                    "script",
                    "tcl",
                    "tcl_ref",
                }
                or name.endswith("_deadline_monotonic")
            ):
                continue
            if name == "recipes" and isinstance(value, list):
                result[name] = [
                    str(item.get("name")) if isinstance(item, dict) else str(item)
                    for item in value
                ]
            else:
                result[name] = _public_params(value)
        return result
    if isinstance(node, (list, tuple)):
        return [_public_params(item) for item in node]
    if node is None or isinstance(node, (bool, int, float, str)):
        return node
    return str(node)


def resolve_action_family(skill: str, target: str) -> str:
    if skill == "implementation_recipe":
        if target in IMPLEMENTATION_RECIPE_FAMILY:
            return IMPLEMENTATION_RECIPE_FAMILY[target]
        return "CLOCK_PERIOD_SEARCH"
    return SKILL_FAMILY.get(skill, "UNCLASSIFIED")


def resolve_registry_path(value: Any) -> Optional[Path]:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    return path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()


@contextmanager
def _registry_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as stream:
        if _fcntl is not None:
            _fcntl.flock(stream.fileno(), _fcntl.LOCK_EX)
        elif _msvcrt is not None:
            # msvcrt locks a byte range starting at the current file position.
            # Keep one stable byte in the sidecar so every process locks byte 0.
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write("\0")
                stream.flush()
            stream.seek(0)
            _msvcrt.locking(stream.fileno(), _msvcrt.LK_LOCK, 1)
        else:  # pragma: no cover - all supported runtimes have one backend
            raise RuntimeError("no supported file-lock backend")
        try:
            yield
        finally:
            if _fcntl is not None:
                _fcntl.flock(stream.fileno(), _fcntl.LOCK_UN)
            elif _msvcrt is not None:
                stream.seek(0)
                _msvcrt.locking(stream.fileno(), _msvcrt.LK_UNLCK, 1)


def load_action_evidence(path: Optional[Path], limit: int = 512) -> list[dict]:
    if path is None or not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    if payload.get("version") != REGISTRY_VERSION:
        return []
    records = payload.get("records")
    if not isinstance(records, list):
        return []
    return [
        copy.deepcopy(item)
        for item in records[-max(1, int(limit)):]
        if isinstance(item, dict) and verify_action_outcome_record(item)
    ]


def append_action_evidence(
    path: Optional[Path], record: dict, *, max_records: int = 512
) -> None:
    if path is None:
        return
    if not verify_action_outcome_record(record):
        raise ValueError("invalid hash binding for action Outcome Proof")
    with _registry_lock(path):
        records = load_action_evidence(path, limit=max_records)
        record_id = str(record.get("record_id") or "")
        if record_id and any(item.get("record_id") == record_id for item in records):
            return
        records.append(copy.deepcopy(record))
        atomic_write_json(path, {
            "version": REGISTRY_VERSION,
            "updated_at": time.time(),
            "records": records[-max(1, int(max_records)):],
        })


def _measurement(node: dict) -> dict:
    def known_bool(name: str) -> Optional[bool]:
        value = node.get(name)
        return value if isinstance(value, bool) else None

    return {
        "clock_period_ns": node.get("clock_period_ns"),
        "wns_ns": node.get("wns_ns"),
        "fmax_mhz": node.get("fmax_mhz"),
        "tns_ns": node.get("tns_ns"),
        "failing_endpoints": node.get("failing_endpoints"),
        "total_endpoints": node.get("total_endpoints"),
        "whs_ns": node.get("whs_ns"),
        "wpws_ns": node.get("wpws_ns"),
        "route_legal": known_bool("route_legal"),
        "placed_legal": known_bool("placed_legal"),
        "hold_legal": known_bool("hold_legal"),
        "pulse_legal": known_bool("pulse_legal"),
        "measurement_complete": known_bool("measurement_complete"),
        "eligible_for_beam": known_bool("eligible_for_beam"),
        "unplaced_primitives": node.get("unplaced_primitives"),
        "legality_reason": node.get("legality_reason"),
    }


def _records_from_memory(memory, platform_id: str) -> list[dict]:
    terminal_records = [
        copy.deepcopy(proof)
        for transaction in getattr(memory, "action_transactions", [])
        if isinstance(transaction, dict)
        and isinstance((proof := transaction.get("outcome_proof")), dict)
        and proof
        and verify_action_outcome_record(proof)
        and (
            "action_executed" not in (proof.get("execution") or {})
            or (proof.get("execution") or {}).get("action_executed") is True
        )
    ]
    terminal_cycle_keys = {
        (proof.get("round"), proof.get("action_fingerprint"))
        for proof in terminal_records
    }
    cycle_runtime_by_round = {
        item.get("round"): _number(
            (item.get("actual") or {}).get("action_cycle_runtime_s")
        )
        for item in getattr(memory, "score_calibration_history", [])
    }
    groups: dict[tuple, list[dict]] = {}
    for row in memory.history:
        if not row.get("action_executed") or not row.get("candidate_created"):
            continue
        key = (
            row.get("round"),
            row.get("seed_candidate_id"),
            row.get("action_fingerprint"),
        )
        groups.setdefault(key, []).append(row)
    records = []
    for (round_number, seed_id, fingerprint), rows in groups.items():
        if (round_number, fingerprint) in terminal_cycle_keys:
            continue
        seed = memory.candidate_node(str(seed_id)) if seed_id else None
        if not seed or not fingerprint:
            continue
        outcomes = []
        for row in sorted(rows, key=lambda item: item.get("candidate_ordinal") or 0):
            child = memory.candidate_node(str(row.get("produced_candidate_id")))
            if child is None:
                continue
            outcomes.append({
                "candidate_artifact_sha256": child.get("artifact_sha256"),
                "legal": (
                    row.get("candidate_legal")
                    if isinstance(row.get("candidate_legal"), bool)
                    else None
                ),
                "promoted": bool(row.get("accepted")),
                "delta_fmax_mhz": row.get("delta_fmax_mhz"),
                "measured_fmax_mhz": row.get("measured_fmax_mhz"),
            })
        if not outcomes:
            continue
        family = resolve_action_family(str(rows[0].get("skill")), str(rows[0].get("target")))
        cycle = _cycle_result(outcomes)
        identity = {
            "platform_id": platform_id,
            "design_id": memory.design_id,
            "seed_artifact_sha256": seed.get("artifact_sha256"),
            "action_fingerprint": fingerprint,
            "outcome_artifact_sha256": [
                item.get("candidate_artifact_sha256") for item in outcomes
            ],
        }
        records.append({
            "version": 1,
            "record_id": _digest(identity),
            "source": "current_run_memory",
            "platform_id": platform_id,
            "design_id": memory.design_id,
            "seed_artifact_sha256": seed.get("artifact_sha256"),
            "action_fingerprint": fingerprint,
            "family": family,
            "skill": rows[0].get("skill"),
            "target": rows[0].get("target"),
            "round": round_number,
            "execution": {
                "action_runtime_s": max(
                    (_number(row.get("action_runtime_s")) or 0.0) for row in rows
                ),
                "action_cycle_runtime_s": cycle_runtime_by_round.get(round_number),
            },
            "outcomes": outcomes,
            "cycle_result": cycle,
        })
    return [*terminal_records, *records]


def import_memory_action_evidence(
    path: Optional[Path], memory, *, platform_id: str, max_records: int = 512
) -> int:
    """Import measured cycles from an older run memory into the shared registry."""
    records = _records_from_memory(memory, platform_id)
    before = len(load_action_evidence(path, limit=max_records))
    for record in records:
        append_action_evidence(path, record, max_records=max_records)
    after = len(load_action_evidence(path, limit=max_records))
    return max(0, after - before)


def _cycle_result(outcomes: list[dict]) -> dict:
    legal = [item for item in outcomes if item.get("legal") is True]
    legality = [item.get("legal") for item in outcomes]
    if legal:
        no_legal_candidate = False
    elif not outcomes or all(value is False for value in legality):
        no_legal_candidate = True
    else:
        no_legal_candidate = None
    deltas = [
        value
        for item in legal
        if (value := _number(item.get("delta_fmax_mhz"))) is not None
    ]
    return {
        "candidate_count": len(outcomes),
        "legal_candidate_count": len(legal),
        "best_delta_vs_seed_fmax_mhz": max(deltas) if deltas else None,
        "no_legal_candidate": no_legal_candidate,
        "all_legal_candidates_regressed": bool(deltas) and all(value < 0 for value in deltas),
        "promoted_candidate_count": sum(bool(item.get("promoted")) for item in outcomes),
    }


def _prior_summary(record: dict) -> dict:
    return {
        "record_id": record.get("record_id"),
        "source": record.get("source", "cross_run_registry"),
        "round": record.get("round"),
        "skill": record.get("skill"),
        "target": record.get("target"),
        "transaction_id": record.get("transaction_id"),
        "action_proof_sha256": record.get("action_proof_sha256"),
        "terminal_status": record.get("terminal_status"),
        "terminal_reason": record.get("terminal_reason"),
        "cycle_result": copy.deepcopy(record.get("cycle_result") or {}),
        "execution": copy.deepcopy(record.get("execution") or {}),
    }


def _is_incomplete_infrastructure_interruption(record: dict) -> bool:
    """Return whether a terminal record contains no action outcome.

    A worker/process interruption after skill invocation may have consumed a
    large amount of proof or extraction time, but without an emitted Candidate
    it establishes neither semantic incompatibility nor routed QoR.  Keep that
    provenance visible to the reviewer without classifying it as an exact
    action repeat or a non-success outcome.
    """
    if record.get("terminal_status") != "interrupted":
        return False
    outcomes = record.get("outcomes")
    if isinstance(outcomes, list) and outcomes:
        return False
    cycle = record.get("cycle_result") or {}
    candidate_count = cycle.get("candidate_count")
    return candidate_count in {None, 0}


def _fact(fact_id: str, statement: str, status: str, observed: Any) -> dict:
    return {
        "id": fact_id,
        "statement": statement,
        "status": status,
        "observed": observed,
    }


def _mechanism_requirements(family: str, telemetry: dict, exact_binding: bool) -> list[dict]:
    profile = telemetry.get("timing_profile") or {}
    features = telemetry.get("design_features") or {}
    route_pct = _number(profile.get("avg_route_pct"))
    if route_pct is None:
        route_pct = _number(features.get("avg_route_pct"))
    route_fact = _fact(
        "route_delay_share",
        "Observed mean route-delay share on the bounded critical-path sample.",
        "observed" if exact_binding and route_pct is not None else "unknown",
        route_pct if exact_binding else "telemetry_not_bound_to_selected_seed",
    )
    logic_levels = _number(
        _nested(profile, "logic_levels", "max") or features.get("max_logic_levels")
    )
    logic_fact = _fact(
        "maximum_logic_levels",
        "Observed maximum logic levels on the bounded critical-path sample.",
        "observed" if exact_binding and logic_levels is not None else "unknown",
        logic_levels if exact_binding else "telemetry_not_bound_to_selected_seed",
    )
    top_nets = copy.deepcopy(list(profile.get("top_nets") or []))
    fanout_fact = _fact(
        "critical_net_fanout_observations",
        "Observed fanout/path coverage for top nets in the bounded live sample.",
        "observed" if exact_binding and top_nets else "unknown",
        top_nets if exact_binding else "telemetry_not_bound_to_selected_seed",
    )
    spread = copy.deepcopy(profile.get("placement_spread") or {})
    spread_fact = _fact(
        "critical_path_spread_observations",
        "Observed placement spans for the bounded live critical-path sample.",
        "observed" if exact_binding and spread else "unknown",
        spread if exact_binding else "telemetry_not_bound_to_selected_seed",
    )
    if family == "FANOUT_REPLICATION":
        return [fanout_fact, route_fact]
    if family == "ROUTE_REPAIR":
        return [route_fact]
    if family == "PLACEMENT_GUIDANCE":
        return [spread_fact, route_fact]
    if family == "LOGIC_OPT":
        return [logic_fact]
    if family == "MICRO_ECO":
        return [route_fact, logic_fact]
    if family == "CLOCK_PERIOD_SEARCH":
        return [_fact(
            "centrally_measured_clock_frame",
            "The seed has a finite contest-clock period and central Fmax.",
            "observed" if exact_binding else "unknown",
            telemetry.get("clock_period") if exact_binding else "telemetry_not_bound_to_selected_seed",
        )]
    return []


def _matching_probe_evidence(
    memory,
    *,
    resolved_action: ResolvedAction,
    selected_seed: dict,
) -> list[dict]:
    """Return compact same-seed pre-mutation evidence for this exact action.

    Read-only probes are not action outcomes, so they do not belong in the
    cross-run outcome registry.  They can still contain the strongest current
    mechanism evidence (for example an emitted-topology counterfactual).  Bind
    that evidence to both the selected seed and the typed candidate action so
    the final review cannot accidentally reason as if the probe never ran.
    """
    source = (
        getattr(memory, "probe_history", None)
        or getattr(memory, "last_custom_responses", None)
        or []
    )
    seed_id = str(selected_seed.get("candidate_id") or "")
    seed_sha = str(selected_seed.get("artifact_sha256") or "")
    expected_params = _public_params(
        resolved_action.concrete_action.get("params") or {}
    )
    clock_context = {}
    for context_entry in reversed(source):
        if (
            not isinstance(context_entry, dict)
            or context_entry.get("status") != "success"
            or context_entry.get("tool") != "operator_mining"
        ):
            continue
        if seed_id and str(context_entry.get("seed_candidate_id") or "") != seed_id:
            continue
        context_sha = str(context_entry.get("artifact_sha256") or "")
        if seed_sha and context_sha and context_sha != seed_sha:
            continue
        context_response = context_entry.get("response")
        if isinstance(context_response, str):
            try:
                context_response = json.loads(context_response)
            except json.JSONDecodeError:
                continue
        if not isinstance(context_response, dict):
            continue
        footprint = context_response.get("clock_footprint_summary") or {}
        census = context_response.get("resource_census") or {}
        scope = context_response.get("scope") or {}
        if not all(isinstance(item, dict) for item in (footprint, census, scope)):
            continue
        clock_context = {
            "existing_clock_sink_site_count": footprint.get(
                "max_sink_site_count"
            ),
            "existing_hard_macro_clock_sink_site_count": footprint.get(
                "max_hard_macro_sink_site_count"
            ),
            "placed_cell_count": scope.get("placed_cell_count"),
            "used_hard_macros": copy.deepcopy(census.get("used_hard_macros") or {}),
            "source": "same_seed_operator_mining",
        }
        break
    evidence = []
    for entry in reversed(source):
        if not isinstance(entry, dict) or entry.get("status") != "success":
            continue
        if seed_id and str(entry.get("seed_candidate_id") or "") != seed_id:
            continue
        entry_sha = str(entry.get("artifact_sha256") or "")
        if seed_sha and entry_sha and entry_sha != seed_sha:
            continue
        response = entry.get("response")
        if isinstance(response, str):
            try:
                response = json.loads(response)
            except json.JSONDecodeError:
                continue
        if not isinstance(response, dict):
            continue
        assessment = response.get("action_assessment") or {}
        candidate = assessment.get("candidate_action") or {}
        if (
            not isinstance(assessment, dict)
            or not isinstance(candidate, dict)
            or str(candidate.get("skill") or "") != resolved_action.skill
            or str(candidate.get("target") or "")
            != resolved_action.canonical_target
        ):
            continue
        candidate_params = _public_params(candidate.get("params") or {})
        shared_keys = set(expected_params) & set(candidate_params)
        if any(
            expected_params[key] != candidate_params[key]
            for key in shared_keys
        ):
            continue
        counterfactual = assessment.get("topology_counterfactual") or {}
        closure = assessment.get("generating_operator_closure") or {}
        contract = response.get("contract") or {}
        if not isinstance(counterfactual, dict) or not isinstance(closure, dict):
            continue
        if not isinstance(contract, dict):
            contract = {}
        data_operator = contract.get("data_operator") or {}
        control_operator = contract.get("control_operator") or {}
        recovery = contract.get("recovery") or {}
        live_wall = contract.get("live_wall") or {}
        physical_risk = contract.get("physical_risk") or {}
        dead_state_summary = response.get("summary") or {}
        if not isinstance(data_operator, dict):
            data_operator = {}
        if not isinstance(control_operator, dict):
            control_operator = {}
        if not isinstance(recovery, dict):
            recovery = {}
        if not isinstance(live_wall, dict):
            live_wall = {}
        if not isinstance(physical_risk, dict):
            physical_risk = {}
        if not isinstance(dead_state_summary, dict):
            dead_state_summary = {}
        source_cost = counterfactual.get("source") or {}
        emitted_cost = counterfactual.get("emitted") or {}
        if not isinstance(source_cost, dict):
            source_cost = {}
        if not isinstance(emitted_cost, dict):
            emitted_cost = {}
        normalized_clock_context = copy.deepcopy(clock_context)
        existing_hard_sinks = normalized_clock_context.get(
            "existing_hard_macro_clock_sink_site_count"
        )
        new_hard_sinks = physical_risk.get("new_clocked_dsp_count")
        if (
            isinstance(existing_hard_sinks, int)
            and existing_hard_sinks > 0
            and isinstance(new_hard_sinks, int)
            and new_hard_sinks >= 0
        ):
            normalized_clock_context.update({
                "new_to_existing_hard_macro_clock_sink_ratio": (
                    new_hard_sinks / existing_hard_sinks
                ),
                "post_to_pre_hard_macro_clock_sink_ratio": (
                    (existing_hard_sinks + new_hard_sinks) / existing_hard_sinks
                ),
            })
        normalized_clock_context["comparison_is_mechanical_not_qor_prediction"] = True
        evidence.append({
            "kind": "same_seed_pre_mutation_action_assessment",
            "round": entry.get("round"),
            "server": entry.get("server"),
            "tool": entry.get("tool"),
            "action_type": assessment.get("action_type"),
            "authority": assessment.get("authority"),
            "selection_status": assessment.get("selection_status"),
            "complete_operator_rewrite_eligible": assessment.get(
                "complete_operator_rewrite_eligible"
            ),
            "expected_mechanism": assessment.get("expected_mechanism"),
            "failure_update": assessment.get("failure_update"),
            "operator_contract": {
                "operator_class": contract.get("operator_class"),
                "data_operator": {
                    key: data_operator.get(key)
                    for key in (
                        "source", "word_width", "global_operation",
                        "specialized_operation", "lane_count",
                        "complete_contiguous_p_to_ff_mapping",
                        "pcout_and_output_side_consumers_closed",
                        "current_preg_mode",
                    )
                },
                "control_operator": {
                    key: control_operator.get(key)
                    for key in (
                        "bank_count",
                        "local_write_predicate_truth_table_preserved",
                        "alternate_and_init_cofactors_preserved",
                        "global_predicate_consumer_preserved",
                        "clock_ce_reset_state_unchanged",
                        "clock_ce_synchronous_reset_word_recovered",
                        "clock_edge_inverted", "observable_cycle_preserved",
                        "retained_reset_history_ff_count",
                        "reset_correction_lut_count",
                    )
                },
                "recovery": {
                    key: recovery.get(key)
                    for key in (
                        "comparator_lut_count", "selected_word_input_count",
                        "registered_source_count",
                        "every_selected_input_has_one_bank_local_source",
                    )
                },
                "live_wall": {
                    key: live_wall.get(key)
                    for key in (
                        "top_path_sample_count", "covered_path_count",
                        "covers_worst_path", "wns_ns", "logic_levels",
                        "logic_delay_ns", "net_delay_ns",
                        "input_wns_ns", "independent_sibling_present",
                        "independent_sibling_rank",
                        "independent_sibling_slack_ns",
                        "independent_sibling_margin_ns",
                    )
                },
                "physical_risk": {
                    key: physical_risk.get(key)
                    for key in (
                        "new_clocked_dsp_count", "clock_region_count",
                        "selected_dsp_already_on_recovered_clock_count",
                        "newly_activated_dsp_clock_site_count",
                        "estimated_removed_external_ff_count",
                        "sibling_ceiling_and_clock_footprint_are_soft_evidence",
                    )
                },
                "observable_dead_state": {
                    key: dead_state_summary.get(key)
                    for key in (
                        "physical_cell_count", "observable_physical_cell_count",
                        "dead_physical_cell_count", "dead_registered_state_count",
                        "total_dsp_count", "dead_dsp_count",
                        "partial_dead_dsp_parent_count",
                        "mutation_target_cell_count", "mutation_eligible",
                        "dead_type_counts",
                    )
                },
            },
            "same_seed_clock_context": normalized_clock_context,
            "topology_counterfactual": {
                "status": counterfactual.get("status"),
                "serial_carry8_level_delta": counterfactual.get(
                    "serial_carry8_level_delta"
                ),
                "selected_wall_covered_path_count": counterfactual.get(
                    "selected_wall_covered_path_count"
                ),
                "ordered_path_count": counterfactual.get("ordered_path_count"),
                "uncovered_path_count": counterfactual.get(
                    "uncovered_path_count"
                ),
                "earliest_predicted_surviving_path_rank": counterfactual.get(
                    "earliest_predicted_surviving_path_rank"
                ),
                "complete_generating_operator_contract_recovered": (
                    counterfactual.get(
                        "complete_generating_operator_contract_recovered"
                    )
                ),
                "source": {
                    "source_primary_carry_chain_carry8_levels": source_cost.get(
                        "source_primary_carry_chain_carry8_levels"
                    ),
                    "source_carry8_count": source_cost.get(
                        "source_carry8_count"
                    ),
                    "removable_cell_count": source_cost.get(
                        "removable_cell_count"
                    ),
                },
                "emitted": {
                    "backend": emitted_cost.get("backend"),
                    "dsp48e2_count": emitted_cost.get("dsp48e2_count"),
                    "dsp_internal_pipeline_register_count": emitted_cost.get(
                        "dsp_internal_pipeline_register_count"
                    ),
                    "estimated_external_carry8_levels_on_deepest_timing_chain": (
                        emitted_cost.get(
                            "estimated_external_carry8_levels_on_deepest_timing_chain"
                        )
                    ),
                    "estimated_external_adder_stages_on_deepest_timing_chain": (
                        emitted_cost.get(
                            "estimated_external_adder_stages_on_deepest_timing_chain"
                        )
                    ),
                    "estimated_csa_lut_levels_on_deepest_timing_chain": (
                        emitted_cost.get(
                            "estimated_csa_lut_levels_on_deepest_timing_chain"
                        )
                    ),
                    "estimated_new_cell_count": emitted_cost.get(
                        "estimated_new_cell_count"
                    ),
                    "clocked_new_cell_count": emitted_cost.get(
                        "clocked_new_cell_count"
                    ),
                },
            },
            "generating_operator_closure": {
                "status": closure.get("status"),
                "complete_generating_operator_contract_recovered": closure.get(
                    "complete_generating_operator_contract_recovered"
                ),
                "family_count": closure.get("family_count"),
                "wall_group_count": closure.get("wall_group_count"),
                "ordered_path_count": closure.get("ordered_path_count"),
                "open_obligations": copy.deepcopy(
                    closure.get("open_obligations") or []
                ),
            },
            "does_not_establish": copy.deepcopy(
                assessment.get("does_not_establish") or []
            ),
        })
    return list(reversed(evidence[-4:]))


def build_action_proof(
    *,
    memory,
    resolved_action: ResolvedAction,
    selected_seed: dict,
    provisional_action: dict,
    canonicalization_repairs: list[dict],
    cfg,
    registry_path: Optional[Path] = None,
    concurrent_proof_required: bool = False,
) -> dict:
    """Compile neutral facts for one canonical action; never return a verdict."""
    platform_id = str(cfg.get("platform.id", "unknown"))
    family = resolve_action_family(
        resolved_action.skill, resolved_action.canonical_target
    )
    registry_limit = int(cfg.get("action_review.registry_max_records", 512))
    records = load_action_evidence(registry_path, limit=registry_limit)
    records.extend(_records_from_memory(memory, platform_id))
    records = [
        item
        for item in records
        if "action_executed" not in (item.get("execution") or {})
        or (item.get("execution") or {}).get("action_executed") is True
    ]
    incomplete_interruptions = [
        item for item in records
        if _is_incomplete_infrastructure_interruption(item)
    ]
    records = [
        item for item in records
        if not _is_incomplete_infrastructure_interruption(item)
    ]
    unique_records = {
        str(item.get("record_id") or _digest(item)): item for item in records
    }
    seed_sha = str(selected_seed.get("artifact_sha256") or "")
    exact = [
        item for item in unique_records.values()
        if item.get("platform_id") == platform_id
        and item.get("seed_artifact_sha256") == seed_sha
        and item.get("action_fingerprint") == resolved_action.action_fingerprint
    ]
    incomplete_exact = [
        item for item in incomplete_interruptions
        if item.get("platform_id") == platform_id
        and item.get("seed_artifact_sha256") == seed_sha
        and item.get("action_fingerprint") == resolved_action.action_fingerprint
    ]
    same_family = [
        item for item in unique_records.values()
        if item.get("platform_id") == platform_id
        and item.get("design_id") == memory.design_id
        and item.get("family") == family
        and item not in exact
    ]
    exact_limit = int(cfg.get("action_review.exact_history_limit", 8))
    family_limit = int(cfg.get("action_review.family_history_limit", 8))
    exact = exact[-exact_limit:]
    same_family = same_family[-family_limit:]

    telemetry = memory.current_telemetry or {}
    telemetry_candidate = str(telemetry.get("candidate_id") or "")
    report_identity = (telemetry.get("report_snapshot") or {}).get("identity") or {}
    exact_binding = telemetry_candidate == str(selected_seed.get("candidate_id"))
    if report_identity:
        exact_binding = exact_binding and (
            str(report_identity.get("artifact_sha256") or "") == seed_sha
        )
    requirements = _mechanism_requirements(family, telemetry, exact_binding)
    probe_evidence = _matching_probe_evidence(
        memory,
        resolved_action=resolved_action,
        selected_seed=selected_seed,
    )
    contradictions = []
    negative_exact = [
        item for item in exact
        if (item.get("cycle_result") or {}).get("best_delta_vs_seed_fmax_mhz") is not None
        and float(item["cycle_result"]["best_delta_vs_seed_fmax_mhz"]) <= 0.0
    ]
    if negative_exact:
        contradictions.append({
            "kind": "exact_same_seed_action_nonpositive",
            "statement": (
                "The same platform, exact seed artifact, and canonical action "
                "already produced no positive centrally measured Fmax gain."
            ),
            "record_ids": [item.get("record_id") for item in negative_exact],
            "observed_best_deltas_mhz": [
                item["cycle_result"].get("best_delta_vs_seed_fmax_mhz")
                for item in negative_exact
            ],
        })
    failed_exact = [
        item
        for item in exact
        if item.get("terminal_status")
        in {
            "timed_out",
            "execution_failed",
            "measurement_failed",
            "no_candidates",
            "illegal_candidates",
            "interrupted",
        }
        and (
            "action_executed" not in (item.get("execution") or {})
            or (item.get("execution") or {}).get("action_executed") is True
        )
    ]
    if failed_exact:
        contradictions.append({
            "kind": "exact_same_seed_action_terminal_failure",
            "statement": (
                "The same platform, exact seed artifact, and canonical action "
                "already reached an observed non-success terminal state."
            ),
            "record_ids": [item.get("record_id") for item in failed_exact],
            "terminal_statuses": [
                item.get("terminal_status") for item in failed_exact
            ],
        })
    for item in probe_evidence:
        counterfactual = item.get("topology_counterfactual") or {}
        if counterfactual.get("status") != "structurally_negative":
            continue
        contradictions.append({
            "kind": "same_seed_pre_mutation_structurally_negative",
            "statement": (
                "A current same-seed read-only screen for this typed action "
                "found that the proposed emitted serial topology is deeper "
                "than the source topology before mutation."
            ),
            "probe_round": item.get("round"),
            "probe_tool": item.get("tool"),
            "serial_carry8_level_delta": counterfactual.get(
                "serial_carry8_level_delta"
            ),
            "selected_wall_covered_path_count": counterfactual.get(
                "selected_wall_covered_path_count"
            ),
            "ordered_path_count": counterfactual.get("ordered_path_count"),
            "complete_generating_operator_contract_recovered": (
                counterfactual.get(
                    "complete_generating_operator_contract_recovered"
                )
            ),
        })

    observed_runtime = [
        value
        for item in exact
        if (value := _number(
            (item.get("execution") or {}).get("action_cycle_runtime_s")
            or (item.get("execution") or {}).get("action_runtime_s")
        )) is not None
    ]
    family_runtime = [
        value
        for item in same_family
        if (value := _number(
            (item.get("execution") or {}).get("action_cycle_runtime_s")
            or (item.get("execution") or {}).get("action_runtime_s")
        )) is not None
    ]
    configured_runtime = _number(
        cfg.get(f"skills.runtime_estimates_s.{resolved_action.skill}")
    )
    conservative = (
        max(observed_runtime) if observed_runtime
        else max(family_runtime) if family_runtime
        else configured_runtime
    )
    runtime_source = (
        "exact_same_seed_action_observation" if observed_runtime
        else "same_design_family_observation" if family_runtime
        else "configured_skill_estimate" if configured_runtime is not None
        else "unavailable"
    )
    usable = max(
        0.0,
        float(memory.time_remaining())
        - float(cfg.get("run.time_safety_s", 240)),
    )

    target_fmax = _number((memory.hard_target or {}).get("target_fmax_mhz"))
    current_fmax = _number(memory.current_best.get("fmax"))
    gap = (
        max(0.0, target_fmax - current_fmax)
        if target_fmax is not None and current_fmax is not None else None
    )
    prior_positive = [
        value
        for item in [*exact, *same_family]
        if (value := _number(
            (item.get("cycle_result") or {}).get("best_delta_vs_seed_fmax_mhz")
        )) is not None and value > 0.0
    ]

    proof = {
        "version": PROOF_VERSION,
        "kind": "canonical_action_proof",
        "neutral_evidence_only": True,
        "contains_learned_prediction": False,
        "contains_probability": False,
        "contains_recommendation": False,
        "action_identity": {
            "family_registry_version": FAMILY_REGISTRY_VERSION,
            "family": family,
            "mechanism_family": contract_for(
                resolved_action.skill, resolved_action.canonical_target
            ).family,
            "mechanism_claim": MECHANISM_CLAIMS.get(
                family, "No registered mechanism claim is available."
            ),
            "skill": resolved_action.skill,
            "canonical_target": resolved_action.canonical_target,
            "execution_target": resolved_action.execution_target,
            "action_fingerprint_sha256": resolved_action.action_fingerprint,
            "canonical_public_params": _public_params(
                resolved_action.concrete_action.get("params") or {}
            ),
        },
        "source_identity": {
            "platform_id": platform_id,
            "design_id": memory.design_id,
            "root_artifact_sha256": (memory.run_manifest or {}).get("root_sha256"),
            "seed_candidate_id": selected_seed.get("candidate_id"),
            "seed_artifact_sha256": seed_sha,
            "seed_created_round": selected_seed.get("created_round"),
            "seed_measurement": _measurement(selected_seed),
        },
        "evidence_binding": {
            "telemetry_candidate_id": telemetry_candidate or None,
            "telemetry_epoch": telemetry.get("epoch"),
            "report_snapshot_id": (telemetry.get("report_snapshot") or {}).get(
                "snapshot_id"
            ),
            "report_snapshot_identity": copy.deepcopy(report_identity),
            "telemetry_is_exactly_bound_to_seed": exact_binding,
        },
        "canonicalization": {
            "provisional_public_action": _public_params(provisional_action),
            "repairs": copy.deepcopy(canonicalization_repairs),
        },
        "mechanism_requirements": requirements,
        "same_seed_probe_evidence": probe_evidence,
        "prior_evidence": {
            "repeat_classification": "exact_repeat" if exact else "novel_exact_action",
            "exact_same_seed_action": [_prior_summary(item) for item in exact],
            "incomplete_exact_attempts": [
                {
                    **_prior_summary(item),
                    "evidence_scope": (
                        "infrastructure_interruption_without_candidate_or_measurement"
                    ),
                    "does_not_establish_action_outcome": True,
                }
                for item in incomplete_exact[-exact_limit:]
            ],
            "same_design_family": [_prior_summary(item) for item in same_family],
            "contradicting_evidence": contradictions,
        },
        "runtime_evidence": {
            "source": runtime_source,
            "exact_observations_s": observed_runtime,
            "same_design_family_observations_s": family_runtime,
            "configured_skill_estimate_s": configured_runtime,
            "conservative_reference_s": conservative,
            "usable_time_after_finalization_reserve_s": usable,
            "reference_fits_usable_time": (
                conservative <= usable if conservative is not None else None
            ),
            "not_a_runtime_prediction": True,
            "advisory_not_action_gate": True,
        },
        "target_scale_evidence": {
            "gap_to_target_fmax_mhz": gap,
            "positive_measured_comparable_deltas_mhz": prior_positive,
            "largest_positive_measured_comparable_delta_mhz": (
                max(prior_positive) if prior_positive else None
            ),
            "scale_support": (
                "measured_comparable_outcome_exists" if prior_positive else "unknown"
            ),
        },
        "review_constraints": {
            "proof_does_not_select_action": True,
            "proof_does_not_veto_action": True,
            "unknown_is_not_negative_evidence": True,
            "exact_outcomes_precede_analogical_cases": True,
            "different_action_requires_replan": True,
        },
    }
    if resolved_action.canonical_target == "registered_arithmetic_cone_v1":
        fingerprint = str(
            resolved_action.resolved_params.get("family_fingerprint") or ""
        )
        bindings = [
            item
            for item in telemetry.get("registered_arithmetic_authorizations") or []
            if isinstance(item, dict)
            and item.get("family_fingerprint") == fingerprint
            and item.get("source_artifact_sha256") == seed_sha
        ]
        if not exact_binding or len(bindings) != 1:
            raise ValueError(
                "registered arithmetic Action Proof lacks exact current-seed binding"
            )
        binding = bindings[0]
        coverage = binding.get("live_wall_coverage_ppm")
        if (
            isinstance(coverage, bool)
            or not isinstance(coverage, int)
            or coverage <= 0
            or binding.get("missing_obligations") != []
            or binding.get("proof_status") != "proved"
        ):
            raise ValueError("registered arithmetic Action Proof gates are incomplete")
        proof["registered_arithmetic_binding"] = copy.deepcopy(binding)
    if concurrent_proof_required:
        proof["proof_completion"] = {
            "schema_version": 1,
            "required": True,
            "status": "pending",
            "cheap_replay_vectors": 512,
            "candidate_id": None,
            "candidate_artifact_sha256": None,
            "proof_id": None,
            "proof_digest": None,
            "recovered_plan_digest": None,
            "emitted_topology_digest": None,
            "authority": "concurrent_exact_proof_completion_gate_v1",
        }
    proof["proof_sha256"] = _digest(proof)
    return proof


def complete_action_proof(
    action_proof: dict,
    *,
    receipt,
) -> dict:
    """Bind a concurrent proof receipt into the canonical Action Proof."""
    proof = copy.deepcopy(action_proof)
    completion = proof.get("proof_completion")
    if not isinstance(completion, dict) or completion.get("status") != "pending":
        raise ValueError("Action Proof has no pending concurrent proof completion")
    proof.pop("proof_sha256", None)
    completion.update({
        "status": "proved",
        "candidate_id": str(receipt.candidate_id),
        "candidate_artifact_sha256": str(receipt.candidate_sha256),
        "proof_id": str(receipt.proof_id),
        "proof_digest": str(receipt.proof_digest),
        "recovered_plan_digest": str(receipt.recovered_plan_digest),
        "emitted_topology_digest": str(receipt.emitted_topology_digest),
    })
    proof["proof_completion"] = completion
    proof["proof_sha256"] = _digest(proof)
    return proof


def validate_action_proof_completion(
    action_proof: dict,
    *,
    candidate_id: str,
    candidate_artifact_sha256: str,
) -> dict:
    """Fail closed unless central promotion matches a completed proof receipt."""
    if not isinstance(action_proof, dict):
        raise ValueError("concurrent promotion requires an Action Proof")
    digest = action_proof.get("proof_sha256")
    unsigned = copy.deepcopy(action_proof)
    unsigned.pop("proof_sha256", None)
    completion = action_proof.get("proof_completion")
    expected_fields = {
        "schema_version", "required", "status", "cheap_replay_vectors",
        "candidate_id", "candidate_artifact_sha256", "proof_id", "proof_digest",
        "recovered_plan_digest", "emitted_topology_digest", "authority",
    }
    if (
        not isinstance(completion, dict)
        or set(completion) != expected_fields
        or completion.get("schema_version") != 1
        or completion.get("required") is not True
        or completion.get("status") != "proved"
        or completion.get("cheap_replay_vectors") != 512
        or completion.get("candidate_id") != candidate_id
        or completion.get("candidate_artifact_sha256")
        != candidate_artifact_sha256
        or completion.get("authority")
        != "concurrent_exact_proof_completion_gate_v1"
        or any(
            re.fullmatch(r"[0-9a-f]{64}", str(completion.get(field) or "")) is None
            for field in (
                "proof_digest", "recovered_plan_digest", "emitted_topology_digest"
            )
        )
        or not isinstance(completion.get("proof_id"), str)
        or not completion["proof_id"]
        or not isinstance(digest, str)
        or digest != _digest(unsigned)
    ):
        raise ValueError("concurrent proof completion is absent, stale, or misbound")
    return copy.deepcopy(completion)


def build_action_outcome_record(
    *,
    memory,
    action_proof: dict,
    transaction: dict,
    commit_result: Optional[dict] = None,
    terminal_status: str,
    terminal_reason: str,
    recovered: bool = False,
) -> dict:
    """Build one hash-bound Outcome Proof for an outer action transaction."""
    action = action_proof["action_identity"]
    source = action_proof["source_identity"]
    commit_result = commit_result or {}
    outcomes = []
    action_runtime = _number(transaction.get("action_runtime_s"))
    validation_runtime = 0.0
    for item in commit_result.get("outcomes") or []:
        candidate_id = str(item.get("candidate_id"))
        node = memory.candidate_node(candidate_id)
        if node is None:
            continue
        delta = None
        seed_fmax = _number(source.get("seed_measurement", {}).get("fmax_mhz"))
        child_fmax = _number(node.get("fmax_mhz"))
        if seed_fmax is not None and child_fmax is not None:
            delta = child_fmax - seed_fmax
        node_action_runtime = _number(node.get("action_runtime_s"))
        if node_action_runtime is not None:
            action_runtime = max(action_runtime or 0.0, node_action_runtime)
        validation_runtime += _number(node.get("validation_runtime_s")) or 0.0
        measurement = _measurement(node)
        outcomes.append({
            "ordinal": len(outcomes),
            "candidate_artifact_sha256": node.get("artifact_sha256"),
            "legal": measurement_legality(measurement),
            "promoted": bool(node.get("promoted_to_global_best")),
            "delta_fmax_mhz": delta,
            "measured_fmax_mhz": node.get("fmax_mhz"),
            "measurement": measurement,
            "physical_signature": copy.deepcopy(node.get("physical_signature") or {}),
        })
    cycle = _cycle_result(outcomes)
    action_cycle_runtime = None
    created_round = transaction.get("created_round")
    round_number = int(
        created_round if created_round is not None else getattr(memory, "round", 0)
    )
    for calibration in reversed(getattr(memory, "score_calibration_history", [])):
        if (
            calibration.get("transaction_id") == transaction.get("transaction_id")
            or calibration.get("round") == round_number
        ):
            action_cycle_runtime = _number(
                (calibration.get("actual") or {}).get("action_cycle_runtime_s")
            )
            break
    proof = {
        "version": 2,
        "kind": "action_outcome_proof",
        "neutral_measured_evidence_only": True,
        "contains_learned_prediction": False,
        "contains_probability": False,
        "contains_recommendation": False,
        "source": "terminal_action_transaction",
        "recorded_at": time.time(),
        "transaction_id": transaction.get("transaction_id"),
        "action_proof_sha256": action_proof.get("proof_sha256"),
        "review_id": transaction.get("review_id") or None,
        "platform_id": source.get("platform_id"),
        "design_id": source.get("design_id"),
        "root_artifact_sha256": source.get("root_artifact_sha256"),
        "seed_artifact_sha256": source.get("seed_artifact_sha256"),
        "action_fingerprint": action.get("action_fingerprint_sha256"),
        "family": action.get("family"),
        "mechanism_family": action.get("mechanism_family"),
        "skill": action.get("skill"),
        "target": action.get("canonical_target"),
        "canonical_public_params": copy.deepcopy(action.get("canonical_public_params") or {}),
        "round": round_number,
        "terminal_status": str(terminal_status),
        "terminal_reason": str(terminal_reason),
        "recovered": bool(recovered),
        "execution": {
            "action_executed": (
                transaction.get("execution_started")
                if int(transaction.get("version", 1)) >= 5
                and isinstance(transaction.get("execution_started"), bool)
                else None
                if int(transaction.get("version", 1)) >= 5
                else transaction.get("phase") != "prepared"
            ),
            "action_runtime_s": action_runtime,
            "validation_runtime_s": validation_runtime,
            "action_cycle_runtime_s": action_cycle_runtime,
        },
        "outcomes": outcomes,
        "cycle_result": cycle,
        "causal_scope": "single_within_seed_action_cycle",
        "family_generalization": "not_established",
    }
    proof["outcome_sha256"] = _digest(proof)
    proof["record_id"] = proof["outcome_sha256"]
    return proof
